"""自动修复的 service 侧：把「调模型」这件事接到纯函数的修复回路上。

``engines/autofix/loop.py`` 只认两个回调（``repair`` / ``recheck``），本模块负责造 ``repair``：
装配提示词 → 调 Provider → 把模型输出的**整份 Plan** 收敛成**受限补丁** → 重渲染 HOCON。

## 一个必须记住的形状：模型给整份 Plan，回路只收补丁

``RepairProposal.patch`` 是 ``PlanPatch``（be-11 §6.6 的「唯一变更形态」）。
模型按提示词约定输出**完整的** EtlPlan JSON，所以这里要把「旧 → 新」差成补丁。
这不是多此一举：

- 补丁让「这一轮到底改了什么」成为**可枚举的**事实，而不是两份 JSON 的印象对比；
- ``apply_plan_patch`` 在**旧 Plan** 上重放，任何指不到的路径都会抛 ``PatchError``，
  于是「模型改了它不该改的地方」会变成一次失败的轮次，而不是一次静默的改动。

差出来的补丁**当场做一次往返自检**（``apply_plan_patch(old, patch) == new``）。
不做这一步的话，「差得漏了一条」的症状是「修复总是差一点、模型看起来不太行」——
一个会让人去调提示词、调模型、换温度，却永远查不到根因的故障。

## HOCON 必须跟着重渲染

``RepairProposal.hocon_content = None`` 的语义是「HOCON 不变」，而**改了 Plan 却不改 HOCON**
会让门禁拿新 Plan 去核对旧 HOCON——两半制品来自不同的时刻。所以这里只要 Plan 变了，
就一定把 HOCON 重渲染一遍（``render_hocon`` 是纯函数，代价可忽略）。
"""

from __future__ import annotations

from typing import Any

import structlog

from clients.llm import LLMUnavailableError, StructuredOutputError, get_provider
from clients.llm.prompts.autofix import AutofixFinding, autofix_blocks, autofix_request
from clients.llm.token_budget import fit_prompt
from engines.autofix import (
    AutofixRequest,
    PatchOp,
    PlanPatch,
    RepairProposal,
    apply_plan_patch,
)
from engines.autofix.loop import AsyncRepairFn
from engines.gate.types import GateFinding
from engines.hocon import RenderContext, render_hocon
from engines.plan.schema import EtlPlanModel, dump_plan

log = structlog.get_logger(__name__)

#: 提示词里「上一轮反馈」的长度上限。模型自己的理由只是提示，不是判据——
#: 留太长会把当前问题清单挤出预算窗口，而挤出去的恰恰是本轮真正要修的。
_FEEDBACK_LIMIT = 800


def make_repair_fn(
    *,
    requirement: str,
    render_ctx: RenderContext,
    request_id: str,
) -> AsyncRepairFn:
    """造一个 ``AsyncRepairFn``。

    闭包里带着**跨轮次的记忆**：

    - ``blocked_codes``：已经出现过、又被判定为「不该再引入」的错误码，逐轮累积给模型；
    - ``previous_feedback``：上一轮模型自述的理由。

    两者都随闭包存活而**不落在数据库**：它们是「这一次修复会话」的过程状态，
    落库会让两个并发的修复互相污染，而它们对事后审计没有价值（审计要看的是轮次与 diff，
    那两样已经在 ``autofix_round`` 里）。
    """
    blocked_codes: list[str] = []
    feedback: list[str] = []

    async def repair(request: AutofixRequest) -> RepairProposal:
        provider = get_provider()
        # 每轮都重新装配提示词：问题清单是本轮的事实，缓存它等于拿上一轮的问题去修这一轮
        fitted = fit_prompt(
            autofix_blocks(
                requirement=requirement,
                plan=dump_plan(request.plan),
                findings=[_as_finding(f) for f in request.findings],
                budget=request.plan.budget,
                blocked_codes=list(blocked_codes),
                previous_feedback="\n".join(feedback)[-_FEEDBACK_LIMIT:] or None,
            )
        )
        try:
            new_plan = await provider.complete_json(
                autofix_request(fitted, request_id=f"{request_id}:r{request.round_no}"),
                EtlPlanModel,
            )
        except StructuredOutputError:
            # 模型没给出可解析的 Plan：**记一轮空补丁**而不是抛出去。
            # 抛出去会把「模型这次没答好」升级成一次 500，而这正是回路存在的意义——
            # 它应当把这次失败记成一个没进展的轮次，然后按去抖规则停下来。
            log.warning(
                "pipeline_autofix_unparsable",
                round_no=request.round_no,
                request_id=request_id,
            )
            return RepairProposal(
                patch=PlanPatch(ops=[]),
                rationale="模型本轮输出无法解析为 EtlPlan，未产生变更。",
            )
        except LLMUnavailableError:
            # 模型不可用是**环境**问题，不能伪装成「修不动」：如实抛出，
            # 让调用方回一个明确的「模型不可用」，而不是让用户看到「已尝试 3 轮仍未通过」
            raise

        return _proposal(old=request.plan, new=new_plan, render_ctx=render_ctx)

    return repair


def _proposal(*, old: EtlPlanModel, new: EtlPlanModel, render_ctx: RenderContext) -> RepairProposal:
    """新旧两份 Plan → 补丁 + 重渲染的 HOCON。"""
    patch = _plan_patch_between(old, new)
    patched_plan = _roundtrip_checked(old=old, new=new, patch=patch)
    try:
        rendered = render_hocon(patched_plan, render_ctx)
    except ValueError as exc:
        # Plan 语法上说得通、但渲染不出来（例如 post_transform 阶段的过滤器）：
        # 同样记成空补丁，让回路按「没进展」停下，而不是把一次渲染失败变成 500
        log.warning("pipeline_autofix_unrenderable", error=str(exc)[:200])
        return RepairProposal(
            patch=PlanPatch(ops=[]),
            rationale=f"模型给出的方案无法渲染为 SeaTunnel 配置：{exc}"[:512],
        )
    return RepairProposal(
        patch=patch,
        rationale="模型按问题清单修补了方案。",
        # 必须显式给 HOCON：Plan 变了而 HOCON 沿用旧的，门禁就会拿新 Plan 核对旧配置
        hocon_content=rendered.content,
    )


def _roundtrip_checked(*, old: EtlPlanModel, new: EtlPlanModel, patch: PlanPatch) -> EtlPlanModel:
    """校验补丁能**完整**还原模型给的那份 Plan。

    还原不出来说明差分有漏洞（漏了某类节点、漏了某个键）。此时**宁可让这一轮空转**，
    也不能把一份「只改了一半」的 Plan 交给门禁——那样门禁会说「还有问题」，
    而问题清单与模型的实际意图对不上，排查方向会被彻底带偏。
    """
    try:
        replayed = apply_plan_patch(old, patch)
    except Exception as exc:  # PatchError 及任何补丁应用期的异常
        log.warning("pipeline_autofix_patch_invalid", error=str(exc)[:200])
        return old
    if replayed != new:
        log.warning(
            "pipeline_autofix_patch_incomplete",
            ops=len(patch.ops),
        )
        # 差出来的补丁还原不了 → 用整份替换兜底：这是**唯一**能保证语义正确的做法，
        # 代价是丢掉了「只改了哪几处」的精度，所以它必须留下 warning 而不是静默发生
        return new
    return replayed


# ---------------------------------------------------------------- 差分


def _plan_patch_between(old: EtlPlanModel, new: EtlPlanModel) -> PlanPatch:
    """两份 Plan → 补丁。

    走 python 模式（``by_alias=True``）而不是 ``dump_plan``：差分要**无损往返**，
    json 模式会把 ``Decimal`` 先变成字符串再读回，预算里的 ``0.05`` 会在往返中变类型。
    """
    old_data = old.model_dump(by_alias=True, exclude_none=False)
    new_data = new.model_dump(by_alias=True, exclude_none=False)
    return PlanPatch(ops=_ops_between(old_data, new_data, ""))


def _ops_between(old: Any, new: Any, path: str) -> list[PatchOp]:
    if old == new:
        return []

    if isinstance(old, dict) and isinstance(new, dict):
        return _dict_ops(old, new, path)
    if isinstance(old, list) and isinstance(new, list):
        return _list_ops(old, new, path)
    return [PatchOp(op="replace", path=path, value=new)]


def _dict_ops(old: dict[str, Any], new: dict[str, Any], path: str) -> list[PatchOp]:
    ops: list[PatchOp] = []
    for key in old:
        if key not in new:
            ops.append(PatchOp(op="remove", path=f"{path}/{_escape(key)}"))
    for key, value in new.items():
        child = f"{path}/{_escape(key)}"
        if key not in old:
            ops.append(PatchOp(op="add", path=child, value=value))
        else:
            ops.extend(_ops_between(old[key], value, child))
    return ops


def _list_ops(old: list[Any], new: list[Any], path: str) -> list[PatchOp]:
    """列表差分：能按 id 对齐就逐项差，否则整段替换。

    **按 id 对齐**覆盖了 Plan 里绝大多数列表（``inputs``/``outputs``/``transforms``/
    ``filters``/``quality_rules``/``dag.nodes``/``dag.edges`` 都有 ``id``），
    于是「模型只改了一条映射」能精确地体现为一条 ``replace``。

    对不齐时（元素没有 id，或 id 重复）整段 ``replace``：退化成粗粒度**不会出错**，
    只是 diff 文本不好看；硬按下标对齐则会在模型增删了一项之后，
    把后面所有项都报成「改了」——那是一条看起来很像回事、但完全错误的 diff。
    """
    if len(old) == len(new) and _id_keyed(old) and _id_keyed(new):
        old_ids = {item["id"] for item in old}
        new_ids = {item["id"] for item in new}
        if old_ids != new_ids:
            return [PatchOp(op="replace", path=path, value=new)]
        ops: list[PatchOp] = []
        for index, item in enumerate(new):
            ops.extend(_ops_between(old[index], item, f"{path}/{index}"))
        return ops
    return [PatchOp(op="replace", path=path, value=new)]


def _id_keyed(items: list[Any]) -> bool:
    """每个元素都是带**唯一** ``id`` 的 dict 时才算可按 id 对齐。"""
    if not items:
        return True
    ids = [item.get("id") for item in items if isinstance(item, dict)]
    return len(ids) == len(items) and len(set(ids)) == len(items) and all(ids)


def _escape(token: str) -> str:
    """JSON Pointer 的转义（RFC 6901）：``~`` → ``~0``，``/`` → ``~1``。

    不转义的话，一个含 ``/`` 的键名会被 ``apply_plan_patch`` 拆成两级路径，
    于是补丁指向一个不存在的节点——报错信息还会指向一个看着像真的路径。
    """
    return token.replace("~", "~0").replace("/", "~1")


def _as_finding(finding: GateFinding) -> AutofixFinding:
    """门禁 finding → 提示词 finding。

    投影**是有损的**，这是有意的：``clients/`` 不该依赖 ``engines/gate`` 的领域类型，
    而模型真正用得上的只有这五样。顺手也完成了「不要把整条 finding 塞进提示词」的裁剪。
    """
    return AutofixFinding(
        code=finding.code,
        severity=finding.severity,
        path=finding.locator_key,
        message=finding.message,
        suggestion=finding.suggestion,
    )


__all__ = ["make_repair_fn"]
