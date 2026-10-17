"""EtlPlan 生成模板（be-08 §6.3 / §5.1 第 1~3 步）。

产出的是一条 ``CompletionRequest`` 的**素材链**：``plan_blocks()`` 出块 →
``fit_prompt()`` 裁剪 → ``plan_request()`` 拼成请求。三步分开是为了让裁剪发生在
「组装成消息」之前——一旦先拼成一整段再裁，裁剪器就只能按字符砍，围栏与世界都会断。

**只放结构，不放值**（CLAUDE.md §5.2、be-08 §8）：档案里只有表名、列名、类型、
可空、主键标记与敏感级别。样本值即使已脱敏也只在 ``masked_samples`` 这一块里，
且它挂在最低的优先级上（3），预算一紧就先挨刀。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from clients.llm.base import ChatMessage, CompletionRequest
from clients.llm.prompts import fences
from clients.llm.token_budget import FittedPrompt, PromptBlock
from engines.plan.draft import DraftSourceTable, DraftTargetTable
from engines.plan.schema import RuntimeBudget

#: 版本号。改动本文件的任何提示词文案或输出契约都必须同时改它——
#: 它落进 ``generation_context``，也是 Benchmark 缓存键的一部分（be-08 §6.5）：
#: 模板改了但版本没变，缓存会继续喂旧结论。
PROMPT_VERSION_PLAN = "1.0"

TAG_SYSTEM = "SYSTEM"
TAG_REQUIREMENT = "REQUIREMENT"
TAG_SOURCE_PROFILE = "SOURCE_PROFILE"
TAG_TARGET_PROFILE = "TARGET_PROFILE"
TAG_DATA_SOURCE_TYPES = "DATA_SOURCE_TYPES"
TAG_BUDGET = "BUDGET"
TAG_STATS = "SOURCE_STATS"
TAG_SAMPLES = "MASKED_SAMPLES"
TAG_FIELD_COMMENTS = "FIELD_COMMENTS"

_BLOCK_SYSTEM = "system"
_BLOCK_REQUIREMENT = "requirement"
_BLOCK_SOURCE_PROFILE = "source_profile"
_BLOCK_TARGET_PROFILE = "target_profile"
_BLOCK_DATA_SOURCE_TYPES = "data_source_types"
_BLOCK_BUDGET = "budget"
_BLOCK_SOURCE_STATS = "source_stats"
_BLOCK_SAMPLES = "masked_samples"
_BLOCK_FIELD_COMMENTS = "field_comments"

_SYSTEM_PROMPT = """你是数据集成方案生成器。根据用户需求与给定的表结构画像，输出**一个 JSON
对象**，不要输出解释、不要输出 Markdown 围栏。

输出契约（字段名与取值域必须逐字一致）：
{
  "inputs":  [{"id","data_source_id","schema","table","kind":"table|query|file",
               "query","path","format","incremental"}],
  "outputs": [{"id","data_source_id","database","table","mode":"shadow_then_swap"}],
  "field_mappings": [{"source_field","source_type","target_field","target_type",
                      "transform":"none|cast|trim|concat|mask|hash",
                      "masking":{"method":"mask_middle|hash|truncate","params":{}}|null,
                      "quality_rule_ids":[],"change":"added"}],
  "transforms": [{"id","kind","input_fields","output_field","params"}],
  "filters":    [{"id","expression","description","stage":"source|post_transform"}],
  "quality_rules": [{"id","field","rule_type":"not_null|regex|range|enum|type_cast",
                     "params":{},"error_code":"E_*","on_violation":"reject|warn",
                     "enabled":true}],
  "dag": {"nodes":[{"id","type","label","ref","meta"}],
          "edges":[{"id","source","target","label"}]},
  "budget": {"max_read_rows","max_write_bytes","max_duration_sec",
             "max_amplification_ratio","max_reject_rate","warn_threshold_pct"}
}

规则：
1. 只允许使用给出的表与列；表名、列名**必须逐字照抄**，不要翻译、不要改大小写。
2. 敏感级别为 confidential / restricted 的列**必须**带 masking（method 取 mask_middle），
   且其 transform 取 "mask"。
3. 源类型与目标类型不一致的列，transform 取 "cast"，并在 transforms 里补一条 cast。
4. 不要臆造过滤器与派生列；没有就留空数组。
5. budget 必须原样回填给定的数值。
6. 只输出 JSON 对象本身。"""


def _compact(payload: Any) -> str:
    """紧凑 JSON。**单行**是刻意的：裁剪器按行减半时，单行的档案块要么整块在、要么整块走，
    不会出现「JSON 被砍掉一半」这种既解析不了又看起来还在的形状。"""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _profile_of(source: DraftSourceTable, target: DraftTargetTable | None) -> tuple[str, str | None]:
    source_json = _compact(source.model_dump(mode="json", by_alias=True))
    target_json = _compact(target.model_dump(mode="json", by_alias=True)) if target is not None else None
    return source_json, target_json


def plan_blocks(
    *,
    requirement: str,
    source: DraftSourceTable,
    target: DraftTargetTable | None,
    data_source_types: Mapping[int, str],
    budget: RuntimeBudget,
    masked_samples: Mapping[str, Any] | None = None,
    field_comments: Mapping[str, str] | None = None,
) -> list[PromptBlock]:
    """按 be-08 §6.3 的优先级装配提示词块（0 最不可动）。

    ``masked_samples`` 的键是表名、值是**已脱敏**的行列表（be-04 的产物）；
    本函数不脱敏、也不检查——它只负责把它们放到最容易挨刀的位置。
    """
    source_json, target_json = _profile_of(source, target)

    blocks: list[PromptBlock] = [
        PromptBlock(key=_BLOCK_SYSTEM, priority=0, text=fences.wrap(TAG_SYSTEM, _SYSTEM_PROMPT)),
        PromptBlock(
            key=_BLOCK_REQUIREMENT,
            priority=0,
            text=fences.wrap(TAG_REQUIREMENT, requirement.strip()),
        ),
        PromptBlock(
            key=_BLOCK_BUDGET,
            priority=0,
            text=fences.wrap(TAG_BUDGET, _compact(budget.model_dump(mode="json"))),
        ),
        # priority 1：字段名/类型/可空/主键——生成映射的**必需**信息
        PromptBlock(
            key=_BLOCK_SOURCE_PROFILE,
            priority=1,
            text=fences.wrap(TAG_SOURCE_PROFILE, source_json),
        ),
        PromptBlock(
            key=_BLOCK_DATA_SOURCE_TYPES,
            priority=1,
            text=fences.wrap(
                TAG_DATA_SOURCE_TYPES, _compact({str(k): v for k, v in data_source_types.items()})
            ),
        ),
    ]
    if target_json is not None:
        blocks.append(
            PromptBlock(
                key=_BLOCK_TARGET_PROFILE,
                priority=1,
                text=fences.wrap(TAG_TARGET_PROFILE, target_json),
            )
        )

    # priority 2：近似统计——有用，但没有它也能生成映射
    stats = [{"table": table.name, "row_count_approx": table.row_count_approx} for table in source.tables]
    blocks.append(
        PromptBlock(key=_BLOCK_SOURCE_STATS, priority=2, text=fences.wrap(TAG_STATS, _compact(stats)))
    )

    # priority 3：脱敏样本——对生成映射几乎无用，却最占地方
    if masked_samples:
        blocks.append(
            PromptBlock(
                key=_BLOCK_SAMPLES,
                priority=3,
                text=fences.wrap(TAG_SAMPLES, _compact(dict(masked_samples))),
            )
        )

    # priority 4：字段注释——最先整块丢弃
    if field_comments:
        blocks.append(
            PromptBlock(
                key=_BLOCK_FIELD_COMMENTS,
                priority=4,
                text=fences.wrap(TAG_FIELD_COMMENTS, _compact(dict(field_comments))),
            )
        )
    return blocks


def plan_messages(fitted: FittedPrompt) -> list[ChatMessage]:
    """把裁剪结果拼成消息：``system`` 块单独成系统提示，其余合成一条 user 消息。

    需求原文必须留在 **user** 侧：塞进 system 会让模型把「用户的话」当成自己的指令，
    而这句话在项目里是**不可信输入**（它可能包含诱导性内容），
    所以它要和「指令」在位置上分开。
    """
    system_text = fitted.get(_BLOCK_SYSTEM) or ""
    user_parts = [b.text for b in fitted.blocks if b.key != _BLOCK_SYSTEM]
    messages = [ChatMessage(role="system", content=system_text)]
    messages.append(ChatMessage(role="user", content="\n\n".join(user_parts)))
    return messages


def plan_request(
    fitted: FittedPrompt,
    *,
    request_id: str,
    prompt_version: str = PROMPT_VERSION_PLAN,
    max_output_tokens: int = 4096,
) -> CompletionRequest:
    """组装成 ``CompletionRequest``（``json_mode=True``、``temperature=0.0``）。"""
    return CompletionRequest(
        task="generate",
        messages=plan_messages(fitted),
        json_mode=True,
        temperature=0.0,
        max_output_tokens=max_output_tokens,
        prompt_version=prompt_version,
        request_id=request_id,
    )


__all__ = [
    "PROMPT_VERSION_PLAN",
    "plan_blocks",
    "plan_messages",
    "plan_request",
]
