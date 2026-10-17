"""门禁 finding → 补丁模板（be-08 §6.1 的 ``generate`` 能力，be-11 自动修复回路使用）。

输入是「当前方案 + 本轮要修的问题清单」，输出是**修补后的完整方案 JSON**。
刻意不做 diff 格式：让模型产出 unified diff 再解析，等于把「模型算得好不好」
和「diff 上下文行号对不对」两件事揉在一起失败，而后者纯粹是格式噪声。

findings 只带 ``code / severity / 路径 / 文案 / 建议``——**不带原文**：
文案在门禁侧已经脱敏（``engines/gate/checks/base.safe_message``），
这里再截断一次长度，避免一条失控的长文案把预算吃光。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from clients.llm.base import ChatMessage, CompletionRequest
from clients.llm.prompts import fences
from clients.llm.token_budget import FittedPrompt, PromptBlock
from engines.plan.schema import RuntimeBudget

#: 与 ``prompt_versions["autofix"]`` 一致。模板改了必须同步改版本号，否则 Benchmark 缓存会继续用旧结论。
PROMPT_VERSION_AUTOFIX = "1.0"

TAG_SYSTEM = "SYSTEM"
TAG_REQUIREMENT = "REQUIREMENT"
TAG_PLAN = "PLAN"
TAG_BUDGET = "BUDGET"
TAG_FINDINGS = "FINDINGS"
TAG_BLOCKED_CODES = "BLOCKED_CODES"
TAG_FEEDBACK = "PREVIOUS_FEEDBACK"

_BLOCK_SYSTEM = "system"
_BLOCK_REQUIREMENT = "requirement"
_BLOCK_PLAN = "plan"
_BLOCK_BUDGET = "budget"
_BLOCK_FINDINGS = "findings"
_BLOCK_BLOCKED_CODES = "blocked_codes"
_BLOCK_FEEDBACK = "previous_feedback"

_FINDING_MESSAGE_LIMIT = 300

_SYSTEM_PROMPT = """你是数据集成方案修复器。输入是一份 EtlPlan JSON 与一份门禁问题清单，
输出**修补后的完整 EtlPlan JSON 对象**。不要输出解释、不要输出 Markdown 围栏、不要输出 diff。

规则：
1. 只修问题清单里列出的问题；未被点名的字段、过滤器、变换、预算一律**原样保留**。
2. 已被上一轮标记为 blocked 的错误码不得再引入同类问题。
3. 保持字段名与取值域与输入完全一致，不要新增未在契约里的键。
4. 只输出 JSON 对象本身。"""


def _compact(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


class AutofixFinding(BaseModel):
    """一条待修问题（门禁 ``GateFinding`` 的投影）。

    刻意**不复用** ``engines.gate.types.GateFinding``：``clients/`` 不该反向依赖 ``engines/``
    的具体领域类型（be-08 §1 的依赖方向），而自动修复真正需要的只有这五个字段。
    由调用方做这一步投影，顺手就完成了「不要把整条 finding 塞进提示词」的裁剪。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str = Field(min_length=1, max_length=64)
    severity: str = Field(default="block", max_length=16)
    #: JSON Pointer 风格的定位（前端契约里的 ``locator`` 已拍平成一个字符串）
    path: str = Field(default="", max_length=256)
    message: str = Field(default="", max_length=512)
    suggestion: str | None = Field(default=None, max_length=512)


def finding_digest(findings: Sequence[AutofixFinding]) -> str:
    """本轮问题的去抖键（be-11 §6.6 规则 4）。纯函数，供调用方比对「这轮和上轮是不是同一批问题」。"""
    return _compact(sorted(f"{f.code}|{f.path}" for f in findings))


def autofix_blocks(
    *,
    requirement: str,
    plan: Mapping[str, Any],
    findings: Sequence[AutofixFinding],
    budget: RuntimeBudget,
    blocked_codes: Sequence[str] = (),
    previous_feedback: str | None = None,
) -> list[PromptBlock]:
    """装配自动修复的提示词块。

    优先级安排：问题清单与需求是 priority 0（没有它们就无事可做），
    当前方案是 priority 1（必须活着，但它是可压缩的长块），
    上一轮反馈是 priority 2，``blocked_codes`` 是 priority 0
    （它是硬约束，被裁掉等于允许模型原地打转）。
    """
    rendered = [
        {
            "code": f.code,
            "severity": f.severity,
            "path": f.path,
            "message": f.message[:_FINDING_MESSAGE_LIMIT],
            "suggestion": f.suggestion,
        }
        for f in findings
    ]

    blocks: list[PromptBlock] = [
        PromptBlock(key=_BLOCK_SYSTEM, priority=0, text=fences.wrap(TAG_SYSTEM, _SYSTEM_PROMPT)),
        PromptBlock(
            key=_BLOCK_REQUIREMENT, priority=0, text=fences.wrap(TAG_REQUIREMENT, requirement.strip())
        ),
        PromptBlock(key=_BLOCK_FINDINGS, priority=0, text=fences.wrap(TAG_FINDINGS, _compact(rendered))),
        PromptBlock(
            key=_BLOCK_BUDGET,
            priority=0,
            text=fences.wrap(TAG_BUDGET, _compact(budget.model_dump(mode="json"))),
        ),
        PromptBlock(key=_BLOCK_PLAN, priority=1, text=fences.wrap(TAG_PLAN, _compact(dict(plan)))),
    ]
    if blocked_codes:
        blocks.append(
            PromptBlock(
                key=_BLOCK_BLOCKED_CODES,
                priority=0,
                text=fences.wrap(TAG_BLOCKED_CODES, _compact(sorted(set(blocked_codes)))),
            )
        )
    if previous_feedback:
        blocks.append(
            PromptBlock(
                key=_BLOCK_FEEDBACK,
                priority=2,
                text=fences.wrap(TAG_FEEDBACK, previous_feedback.strip()),
            )
        )
    return blocks


def autofix_messages(fitted: FittedPrompt) -> list[ChatMessage]:
    system_text = fitted.get(_BLOCK_SYSTEM) or ""
    user_parts = [b.text for b in fitted.blocks if b.key != _BLOCK_SYSTEM]
    return [
        ChatMessage(role="system", content=system_text),
        ChatMessage(role="user", content="\n\n".join(user_parts)),
    ]


def autofix_request(
    fitted: FittedPrompt,
    *,
    request_id: str,
    prompt_version: str = PROMPT_VERSION_AUTOFIX,
    max_output_tokens: int = 4096,
) -> CompletionRequest:
    return CompletionRequest(
        task="generate",
        messages=autofix_messages(fitted),
        json_mode=True,
        temperature=0.0,
        max_output_tokens=max_output_tokens,
        prompt_version=prompt_version,
        request_id=request_id,
    )


__all__ = [
    "PROMPT_VERSION_AUTOFIX",
    "AutofixFinding",
    "autofix_blocks",
    "autofix_messages",
    "autofix_request",
    "finding_digest",
]
