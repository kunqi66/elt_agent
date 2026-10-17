"""确定性假 Provider —— **没有 API key 时的正式通路**，不是测试替身。

它做的事只有一件：把提示词里 ``<<…>>`` 围栏中的档案读出来，交给纯函数
:func:`engines.plan.draft.draft_plan` 起草，再把结果序列化成 JSON 返回。
不联网、不取时间、不用随机数——**同输入必然逐字节同输出**，因此它同时充当
Benchmark L0 的确定性基线（同一需求两次跑的方案必须完全一致，否则门禁与指纹
就失去了可比性）。

它**不是** mock：单测里断言的是「真实提示词 → 真实起草 → 真实 Plan 校验」这条链，
所以它一旦与 ``prompts/plan.py`` 的围栏约定漂移，测试会立刻红。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from typing import Any

import structlog
from pydantic import ValidationError as PydanticValidationError

from clients.llm.base import (
    CompletionRequest,
    CompletionResult,
    LLMUnavailableError,
    StructuredOutputError,
    TokenUsage,
)
from clients.llm.prompts.autofix import TAG_FINDINGS, TAG_PLAN, AutofixFinding
from clients.llm.prompts.fences import extract
from clients.llm.prompts.plan import (
    TAG_BUDGET,
    TAG_DATA_SOURCE_TYPES,
    TAG_REQUIREMENT,
    TAG_SOURCE_PROFILE,
    TAG_TARGET_PROFILE,
)
from clients.llm.scrub import assert_outbound_safe
from clients.llm.token_budget import estimate_tokens
from engines.plan.draft import DraftSourceTable, DraftTargetTable, draft_plan
from engines.plan.schema import EtlPlanModel, RuntimeBudget, dump_plan

log = structlog.get_logger(__name__)

#: 固定标识：它会被写进 ``generation_context``，让「这份方案是规则起草的」一眼可见。
FAKE_MODEL_NAME = "fake-deterministic"
FAKE_MODEL_VERSION = "1.0"

#: 确定性自动修复覆盖的问题码 → 修复器。**逐个列出**而不是「凡 code 就当能修」：
#: 名单之外的 code 会被如实报成 unresolved，而不是被静默忽略。
FIXABLE_CODES: tuple[str, ...] = (
    "duplicate_id",
    "dangling_ref",
    "dag_cycle",
    "transform_unused",
    "quality_rule_field_missing",
)

#: 明确**不修**的码及原因（写出来，免得下一个人以为是漏了）：
#: - ``budget_nonpositive``：预算上限是策略（be-07），起草器无权替用户改大改小；
#: - ``mapping_unmapped_target``：补一个映射需要目标表结构，而 Plan 里没有它——
#:   猜一个字段名比留着这条 finding 更危险。
UNFIXABLE_CODES: dict[str, str] = {
    "budget_nonpositive": "预算上限属策略，不由起草器改写",
    "mapping_unmapped_target": "补映射需要目标表结构，缺失时不做猜测",
}

_JSON_KWARGS: dict[str, Any] = {"ensure_ascii": False, "separators": (",", ":"), "sort_keys": True}


def _dumps(payload: Any) -> str:
    return json.dumps(payload, **_JSON_KWARGS)


# ---------------------------------------------------------------- 确定性自动修复


def _dedupe(series: list[dict[str, Any]], id_key: str = "id") -> bool:
    """重复 id 改名（``n_x`` → ``n_x_2``）。

    保留**第一个**、重命名后来的：边（``dag.edges``）引用的是第一个，
    因此改后来者不会把边改成悬空引用。反过来做的话，一条 renaming 就要连带
    重写所有引用，而「引用有没有漏改」正是这类修补最容易出错的地方。
    """
    seen: set[str] = set()
    changed = False
    for item in series:
        raw = str(item.get(id_key) or "")
        if not raw:
            continue
        if raw in seen:
            suffix = 2
            while f"{raw}_{suffix}" in seen:
                suffix += 1
            item[id_key] = f"{raw}_{suffix}"
            seen.add(item[id_key])
            changed = True
        else:
            seen.add(raw)
    return changed


def _fix_duplicate_id(data: dict[str, Any]) -> bool:
    dag = data.get("dag") or {}
    changed = False
    for series in (
        data.get("inputs") or [],
        data.get("outputs") or [],
        data.get("transforms") or [],
        data.get("filters") or [],
        data.get("quality_rules") or [],
        dag.get("nodes") or [],
        dag.get("edges") or [],
    ):
        changed |= _dedupe(series)
    return changed


def _fix_dangling_ref(data: dict[str, Any]) -> bool:
    """悬空引用：删掉指向不存在对象的边、变换与质量规则 id。"""
    dag = data.get("dag") or {}
    nodes = dag.get("nodes") or []
    node_ids = {str(n.get("id")) for n in nodes}
    edges = dag.get("edges") or []
    kept_edges = [e for e in edges if str(e.get("source")) in node_ids and str(e.get("target")) in node_ids]
    changed = len(kept_edges) != len(edges)
    dag["edges"] = kept_edges

    mappings = data.get("field_mappings") or []
    known_fields = {str(m.get("source_field")) for m in mappings} | {
        str(m.get("target_field")) for m in mappings
    }
    transforms = data.get("transforms") or []
    kept_transforms = [
        t for t in transforms if all(str(f) in known_fields for f in (t.get("input_fields") or []))
    ]
    changed |= len(kept_transforms) != len(transforms)
    data["transforms"] = kept_transforms

    valid_rule_ids = {str(q.get("id")) for q in (data.get("quality_rules") or [])}
    for mapping in mappings:
        ids = [str(r) for r in (mapping.get("quality_rule_ids") or [])]
        kept = [r for r in ids if r in valid_rule_ids]
        if kept != ids:
            mapping["quality_rule_ids"] = kept
            changed = True
    return changed


def _fix_dag_cycle(data: dict[str, Any]) -> bool:
    """有向图去环：深搜时删掉回边。

    只删**形成环的那一条边**（最先被深搜撞上的那条），不整段重排拓扑：
    重排会把用户看到的图形状整个换掉，而一条回边通常就是手滑连错的那个。
    """
    dag = data.get("dag") or {}
    nodes = [str(n.get("id")) for n in (dag.get("nodes") or [])]
    edges: list[dict[str, Any]] = list(dag.get("edges") or [])
    adjacency: dict[str, list[tuple[str, dict[str, Any]]]] = {n: [] for n in nodes}
    for edge in edges:
        adjacency.setdefault(str(edge.get("source")), []).append((str(edge.get("target")), edge))

    WHITE, GRAY, BLACK = 0, 1, 2
    colour: dict[str, int] = {n: WHITE for n in nodes}
    to_remove: set[int] = set()

    def _visit(start: str) -> None:
        stack: list[tuple[str, int]] = [(start, 0)]
        colour[start] = GRAY
        while stack:
            node, cursor = stack[-1]
            outgoing = adjacency.get(node, [])
            if cursor >= len(outgoing):
                colour[node] = BLACK
                stack.pop()
                continue
            stack[-1] = (node, cursor + 1)
            target, edge = outgoing[cursor]
            if colour.get(target, BLACK) == GRAY:
                to_remove.add(id(edge))
            elif colour.get(target, WHITE) == WHITE:
                colour[target] = GRAY
                stack.append((target, 0))

    for node in nodes:
        if colour.get(node, WHITE) == WHITE:
            _visit(node)

    if not to_remove:
        return False
    dag["edges"] = [e for e in edges if id(e) not in to_remove]
    return True


def _fix_transform_unused(data: dict[str, Any]) -> bool:
    """无人消费的派生列：既不是任何映射的目标字段，也不是别的变换的输入。"""
    mappings = data.get("field_mappings") or []
    target_fields = {str(m.get("target_field")) for m in mappings}
    transforms = data.get("transforms") or []
    consumed: set[str] = set()
    for transform in transforms:
        consumed |= {str(f) for f in (transform.get("input_fields") or [])}
    kept = [
        t
        for t in transforms
        if str(t.get("output_field")) in target_fields or str(t.get("output_field")) in consumed
    ]
    if len(kept) == len(transforms):
        return False
    data["transforms"] = kept
    return True


def _fix_quality_rule_field_missing(data: dict[str, Any]) -> bool:
    """规则挂在不存在的字段上：删掉规则，而不是造一个字段出来。"""
    mappings = data.get("field_mappings") or []
    known = {str(m.get("source_field")) for m in mappings} | {str(m.get("target_field")) for m in mappings}
    rules = data.get("quality_rules") or []
    kept = [r for r in rules if str(r.get("field")) in known]
    if len(kept) == len(rules):
        return False
    data["quality_rules"] = kept
    return True


_FIXERS: dict[str, Any] = {
    "duplicate_id": _fix_duplicate_id,
    "dangling_ref": _fix_dangling_ref,
    "dag_cycle": _fix_dag_cycle,
    "transform_unused": _fix_transform_unused,
    "quality_rule_field_missing": _fix_quality_rule_field_missing,
}


def apply_autofix(
    plan: EtlPlanModel, findings: Sequence[AutofixFinding]
) -> tuple[EtlPlanModel, list[str], list[str]]:
    """确定性修复：返回 ``(修补后的方案, 修好的 code 列表, 没修好的 code 列表)``。

    修补在 ``dump_plan`` 的 dict 形态上做（Plan 是 frozen 的），最后整体重新校验一次——
    修出来的东西必须是**合法 Plan**，否则这场修复只是把一条 finding 换成了另一条。
    """
    data = dump_plan(plan)
    order: list[str] = []
    for finding in findings:
        if finding.code not in order:
            order.append(finding.code)

    applied: list[str] = []
    unresolved: list[str] = []
    for code in order:
        fixer = _FIXERS.get(code)
        if fixer is None:
            unresolved.append(code)
            continue
        if fixer(data):
            applied.append(code)
        else:
            unresolved.append(code)

    patched = EtlPlanModel.model_validate(data)
    return patched, applied, unresolved


# ---------------------------------------------------------------- Provider


class FakeProvider:
    """离线确定性 Provider。"""

    name = "fake"

    def __init__(self, *, model_name: str = FAKE_MODEL_NAME, model_version: str = FAKE_MODEL_VERSION) -> None:
        self._model_name = model_name
        self._model_version = model_version

    # ---- 契约实现 ----

    async def complete(self, req: CompletionRequest) -> CompletionResult:
        # 不出网，但闸门照过：``assert_outbound_safe`` 是 provider 的**接口**契约，
        # 不是某一个实现的细节。假 Provider 上绕过它，就意味着「本地演示用不到它」，
        # 而本地演示恰恰是提示词拼装错误最先暴露的地方
        assert_outbound_safe(req.messages)
        if req.task == "diagnose":
            raise LLMUnavailableError(
                "确定性假 Provider 不提供根因诊断（diagnose），该任务需要真实模型",
                details={"provider": self.name, "task": req.task},
            )
        text = self._render(req)
        prompt_tokens = estimate_tokens("\n".join(m.content for m in req.messages))
        completion_tokens = estimate_tokens(text)
        return CompletionResult(
            text=text,
            usage=TokenUsage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=prompt_tokens + completion_tokens,
            ),
            model=self._model_name,
            model_version=self._model_version,
            finish_reason="stop",
            cached=False,
        )

    async def complete_json(self, req: CompletionRequest, schema: type[Any]) -> Any:
        result = await self.complete(req)
        try:
            return schema.model_validate_json(result.text)
        except PydanticValidationError as exc:
            raise StructuredOutputError(
                "确定性起草结果不符合目标结构",
                details={
                    "schema": getattr(schema, "__name__", str(schema)),
                    "error_paths": [".".join(str(p) for p in err.get("loc", ())) for err in exc.errors()],
                },
            ) from exc

    async def stream(self, req: CompletionRequest) -> AsyncIterator[str]:
        """假 Provider 一次性吐出全文：流式的价值在于真实模型的等待体验，
        这里没有等待可言，逐字符 yield 只会让调用方多一层缓冲。"""
        result = await self.complete(req)
        yield result.text

    # ---- 内部 ----

    def _render(self, req: CompletionRequest) -> str:
        text = "\n".join(m.content for m in req.messages)
        # 三条分支按**围栏内容**判别而不是按 prompt_version 或 task：
        # 版本号是给人看的审计字段，让它承担控制流会让「升个版本号」变成行为变更。
        # 判据只有「提示词里有没有那种围栏」——提示词自己声明它要什么。
        if extract_findings(text) is not None:
            return self._render_autofix(text)
        if extract_or_none(text, TAG_SOURCE_PROFILE) is not None:
            return self._render_draft(text)
        if extract_or_none(text, TAG_REQUIREMENT) is not None:
            return self._render_intent(text)
        raise StructuredOutputError(
            "提示词里既没有 <<SOURCE_PROFILE>>（起草）、<<FINDINGS>>（修复）、"
            "也没有 <<REQUIREMENT>>（意图解析）围栏，无法确定性产出",
            details={
                "provider": self.name,
                "expected_tags": ["SOURCE_PROFILE", "FINDINGS", "REQUIREMENT"],
            },
        )

    def _render_intent(self, text: str) -> str:
        """意图解析：**只回需求原文，一个参数都不猜**。

        为什么不是「用规则抽出主键/增量列/目标表」：那等于让确定性 Provider 自己
        编一套语义抽取规则，而它与 be-10 的 ``draft_plan``、与澄清问卷会形成**第三份**
        「参数从哪来」的判断。更实际的理由是**猜错了没人拦**：模型猜的参数会被
        ``find_missing_params`` 当成已有值，澄清问卷于是不问这一项，用户就没机会纠正。

        回一个空壳意图的后果是：五项必填参数**全部**进澄清问卷，由人拍板后再往下走。
        这是在无 key 环境下**正确**的那条路径，而不是一条捷径——``intent`` 的定位
        本来就是「模型这一次读到了什么」，它什么都没读到，就该是空的。
        """
        requirement = extract_or_none(text, TAG_REQUIREMENT) or ""
        return _dumps({"requirement": requirement.strip(), "goal": ""})

    def _render_draft(self, text: str) -> str:
        requirement = extract_or_none(text, TAG_REQUIREMENT) or ""
        source_raw = extract_or_none(text, TAG_SOURCE_PROFILE)
        budget_raw = extract_or_none(text, TAG_BUDGET)
        if source_raw is None or budget_raw is None:
            raise StructuredOutputError(
                "提示词里没有 <<SOURCE_PROFILE>> 或 <<BUDGET>> 围栏，无法确定性起草",
                details={"provider": self.name, "expected_tags": ["SOURCE_PROFILE", "BUDGET"]},
            )
        source = _validate(DraftSourceTable, source_raw, "SOURCE_PROFILE")
        budget = _validate(RuntimeBudget, budget_raw, "BUDGET")
        target_raw = extract_or_none(text, TAG_TARGET_PROFILE)
        target = _validate(DraftTargetTable, target_raw, "TARGET_PROFILE") if target_raw else None
        types_raw = extract_or_none(text, TAG_DATA_SOURCE_TYPES)
        data_source_types: dict[int, str] = {}
        if types_raw:
            raw_types = _loads(types_raw, "DATA_SOURCE_TYPES")
            data_source_types = {int(k): str(v) for k, v in dict(raw_types).items()}

        plan = draft_plan(
            requirement=requirement,
            source=source,
            target=target,
            budget=budget,
            data_source_types=data_source_types,
        )
        return _dumps(dump_plan(plan))

    def _render_autofix(self, text: str) -> str:
        plan_raw = extract_or_none(text, TAG_PLAN)
        findings_raw = extract_or_none(text, TAG_FINDINGS)
        if plan_raw is None or findings_raw is None:
            raise StructuredOutputError(
                "自动修复提示词缺少 <<PLAN>> 或 <<FINDINGS>> 围栏",
                details={"provider": self.name, "expected_tags": ["PLAN", "FINDINGS"]},
            )
        requirement = extract_or_none(text, TAG_REQUIREMENT) or ""
        plan = _validate(EtlPlanModel, plan_raw, "PLAN")
        raw_findings = _loads(findings_raw, "FINDINGS")
        findings = [AutofixFinding.model_validate(item) for item in raw_findings]

        patched, applied, unresolved = apply_autofix(plan, findings)
        log.info(
            "fake_autofix_applied",
            provider=self.name,
            applied=sorted(set(applied)),
            unresolved=sorted(set(unresolved)),
            requirement_length=len(requirement),
            reasons=[UNFIXABLE_CODES.get(code, "无确定性修复规则") for code in sorted(set(unresolved))],
        )
        return _dumps(dump_plan(patched))


def extract_or_none(text: str, tag: str) -> str | None:
    """``fences.extract`` 的薄封装（保留一处间接层，方便调用方按语义读代码）。"""
    return extract(text, tag)


def extract_findings(text: str) -> str | None:
    return extract_or_none(text, TAG_FINDINGS)


def _loads(raw: str, tag: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise StructuredOutputError(
            f"围栏 {tag} 的内容不是合法 JSON",
            details={"tag": tag, "error": exc.msg, "line": exc.lineno},
        ) from exc


def _validate(schema: type[Any], raw: str, tag: str) -> Any:
    payload = _loads(raw, tag)
    try:
        return schema.model_validate(payload)
    except PydanticValidationError as exc:
        raise StructuredOutputError(
            f"围栏 {tag} 的内容不符合 {getattr(schema, '__name__', schema)} 结构",
            details={
                "tag": tag,
                "error_paths": [".".join(str(p) for p in err.get("loc", ())) for err in exc.errors()],
            },
        ) from exc


__all__ = [
    "FAKE_MODEL_NAME",
    "FAKE_MODEL_VERSION",
    "FIXABLE_CODES",
    "UNFIXABLE_CODES",
    "FakeProvider",
    "apply_autofix",
]
