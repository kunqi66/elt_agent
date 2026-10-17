"""自动修复（be-09 §6.4：本模块只负责「调」）。

**控制流一行都不在本文件里**。轮次上限、去抖（本轮要修的阻塞项此前出现过就停）、
策略阻塞即停手、补丁是唯一变更形态——这些全在 be-11 的 ``engines/autofix/loop.py``
（纯函数，LLM 与复检都以回调解入）。这里只做四件事：

1. 组装修复回路要的输入：``AutofixRequest``（当前 Plan/HOCON + 待修的阻塞项）；
2. 造两个回调：``repair`` 走 ``services/pipeline/repair.make_repair_fn``（真调 LLM），
   ``recheck`` 在本节点内用**已建好的 GateContext** 换掉 plan/hocon 两个字段重跑
   ``engines.gate.evaluate``；
3. 调 ``arun_autofix_loop``，把结果写回 state；
4. 轮次耗尽 / 去抖停下 → ``terminal_error = AUTOFIX_EXHAUSTED``。

## 为什么 ``recheck`` 是同步的、而它需要的那份 ctx 是异步建的

``RecheckFn`` 的签名由 engines 定死为 ``(plan, hocon) -> GateResult``（engines 不许做 IO），
所以复检只能靠「拿**同一份** GateContext 换掉两个字段再跑一次纯函数」。

那份 ctx 必须在本节点里 ``await build_gate_context(...)`` 建一次（读库 + 跑 PDP）。
**不能每轮重建**：那会在 3 轮修复里读 3 次库、拿到 3 个可能不同的档案快照，
于是「为什么第 2 轮结论变了」变成一道没有答案的题。这与
``services/pipeline/artifacts.run_autofix`` 的做法逐字一致——两处必须一致，
否则「手动修复」与「图内修复」会对同一份制品给出不同结论。

## 为什么 ``findings`` 为空时直接终局而不是进回路

``arun_autofix_loop`` 对空阻塞项会「收敛」返回（0 轮、无 LLM 调用）。
那样也能跑通，但会白写一份 ``autofix_history``（空历史）并让路由误以为修复发生过。
这里提前挡掉，同时也覆盖「预算已耗尽」——两种情况下都不该再调模型。

## 为什么本节点只写 ``draft_plan`` 不写 ``draft_hocon``

修复回路产出的 HOCON 交给紧随其后的 ``generate_hocon`` 节点重新渲染。
两处都写就会出现「谁写的 HOCON 是最终版」的分叉，而 ``hocon_meta`` 里的
``sha256`` 只有一个位置能保证与最终文本一致。
"""

from __future__ import annotations

from typing import Any

from core.errors import ErrorCode
from core.logging import get_logger
from workflow.nodes import runtime_of

__all__ = ["autofix_node"]

log = get_logger(__name__)

_NODE = "autofix"


def _max_rounds() -> int:
    from core.config import settings

    return int(getattr(settings, "LLM_AUTOFIX_MAX_ROUNDS", 3))


async def autofix_node(state: dict[str, Any], config: Any = None) -> dict[str, Any]:
    """图节点：门禁问题 → 新的 ``draft_plan``（HOCON 由下一个节点重渲染）。"""
    runtime = runtime_of(config)
    plan_dict = state.get("draft_plan")
    if not isinstance(plan_dict, dict):
        raise _missing_plan()

    gate = state.get("gate_result") or {}
    blocking = _blocking_findings(gate)
    used = int(state.get("autofix_rounds") or 0)
    budget = _max_rounds()
    remaining = budget - used

    if not blocking:
        # 没有阻塞项却走到了修复：图的路由不该这样走。**不落终局错误**——
        # 没有可修的东西不是失败，把它记成 ``AUTOFIX_EXHAUSTED`` 会让用户
        # 看到一条「修复没修好」的报错，而实际上门禁可能是 pass。原样返回，
        # 让后面的 ``gate`` 用真实结论收尾。
        return _noop(runtime, used, reason="no_blocking_findings")
    if remaining <= 0:
        return _stop(runtime, used, budget, reason="budget_exhausted")

    from core.ids import utcnow
    from engines.autofix import AutofixRequest, arun_autofix_loop
    from engines.gate.gate import evaluate
    from engines.gate.types import GateFinding
    from engines.hocon import RenderContext, parse_and_validate
    from engines.plan.schema import EtlPlanModel, dump_plan
    from engines.plan.validate import validate_plan
    from services.pipeline.gating import build_gate_context
    from services.pipeline.repair import make_repair_fn

    plan = EtlPlanModel.model_validate(plan_dict)
    hocon = str(state.get("draft_hocon") or "")
    findings = [GateFinding.model_validate(item) for item in blocking]
    now = utcnow()

    context = await build_gate_context(
        session=runtime.session,
        subject=runtime.ctx,
        project_id=runtime.project_id,
        plan=plan,
        hocon_content=hocon,
        now=now,
    )
    render_ctx = RenderContext(
        data_source_types=dict(context.data_source_types),
        parallelism=int((state.get("generation_context") or {}).get("parallelism", 1) or 1),
    )

    def recheck(candidate: EtlPlanModel, candidate_hocon: str) -> Any:
        """复检：**纯函数**，不碰库（用本次判定那份 ctx 换两个字段）。"""
        refreshed = context.model_copy(
            update={
                "plan": candidate,
                "hocon_content": candidate_hocon,
                "parse_result": parse_and_validate(candidate_hocon),
            }
        )
        return evaluate(refreshed, now=now)

    outcome = await arun_autofix_loop(
        request=AutofixRequest(round_no=1, plan=plan, hocon_content=hocon, findings=findings),
        repair=make_repair_fn(
            requirement=_requirement(state),
            render_ctx=render_ctx,
            # request_id 带上节点名与轮次基准：一次图运行里修复可能被进入多次
            # （修完门禁还报错 → 再修一轮），日志里必须能分辨是第几次进入。
            request_id=f"{runtime.request_id}:autofix:{used}",
        ),
        recheck=recheck,
        max_rounds=remaining,
        # 时间固定成同一个时刻：一轮修复里的 created_at 全都一样，
        # 「这轮花了多久」不该从 state 里倒推（那是指标的活，见 be-16）。
        now_factory=lambda: now,
    )

    new_plan = outcome.plan
    issues = validate_plan(new_plan)
    rounds = [*_renumber(outcome.history, offset=used)]
    total_rounds = used + len(rounds)
    history = [*(state.get("autofix_history") or []), *[_round_summary(r) for r in rounds]]

    log.info(
        "autofix_finished",
        request_id=runtime.request_id,
        tenant_id=runtime.tenant_id,
        project_id=runtime.project_id,
        pipeline_id=runtime.pipeline_id,
        rounds_this_pass=len(rounds),
        rounds_total=total_rounds,
        max_rounds=budget,
        converged=outcome.converged,
        exhausted=outcome.exhausted,
        stopped_by_debounce=outcome.stopped_by_debounce,
    )

    result: dict[str, Any] = {
        "draft_plan": dump_plan(new_plan),
        "plan_errors": [issue.model_dump(mode="json") for issue in issues],
        "autofix_rounds": total_rounds,
        "autofix_history": history,
        # 清掉旧的门禁结论：它对应的是**修复前**的制品，留着会让 ``gate`` 节点的
        # 幂等守卫之外的读者（如路由判定）拿旧结论做判断。
        "gate_result": None,
    }
    # ``autofix_exhausted`` 的口径与前端一致：轮次耗尽**或**去抖终止都算「没修好」
    # （``AutofixOutcome.autofix_exhausted`` 的注释）。只有「没修好」才落终局错误；
    # 修好了就交给后面的 ``generate_hocon`` → ``gate`` 正常收尾。
    if outcome.autofix_exhausted:
        result["terminal_error"] = ErrorCode.AUTOFIX_EXHAUSTED.value
    return result


def _missing_plan() -> Any:
    from core.errors import AppError

    return AppError(
        "尚未生成 EtlPlan，无法执行自动修复",
        code=ErrorCode.PLAN_INVALID,
        http_status=422,
    )


def _requirement(state: dict[str, Any]) -> str:
    """给修复提示词用的需求文本。

    取 ``requirement``（入图时由 service 注入）；再兜底 ``intent.requirement``。
    两者都取不到时给一句中性描述而**不是空串**：提示词里少掉「要做什么」，
    模型会开始凭 Plan 猜意图，而猜出来的改动往往是对的但没必要的。
    """
    text = str(state.get("requirement") or "").strip()
    if text:
        return text
    intent = state.get("intent") or {}
    return str(intent.get("requirement") or "").strip() or "按当前 EtlPlan 的意图修复列出的问题"


def _blocking_findings(gate: dict[str, Any]) -> list[dict[str, Any]]:
    """取**阻塞**项。

    策略类不在这里排除——那是 ``engines/autofix/loop.py`` 的约束 2（出现策略阻塞
    就地停手、repair 零调用），在路由层还有一道（``route_after_gate`` 直接去 terminal）。
    本节点再排除一遍等于把同一个判据写第三份，而三份里改漏一份的表现是
    「策略阻断悄悄进了修复」，恰恰是那条约束要防的事。
    """
    return [
        item
        for item in (gate.get("findings") or [])
        if isinstance(item, dict) and str(item.get("severity")) == "block"
    ]


def _renumber(rounds: list[Any], *, offset: int) -> list[Any]:
    """把本轮的 ``round`` 序号接到历史之后。

    回路每次进入都从 ``round_no=1`` 开始（它只负责一次调用内的编号）。
    不接续的话，一次图运行里的两轮修复会在 ``autofix_history`` 里出现两条 ``round=1``，
    前端时间线看起来像「同一轮被记了两次」。
    """
    if not offset:
        return list(rounds)
    return [r.model_copy(update={"round": offset + int(r.round)}) for r in rounds]


def _round_summary(round_obj: Any) -> dict[str, Any]:
    """一轮修复记录 → state 里的一行（**形状对齐前端 ``AutofixRound``**）。

    前端 ``AutofixRound`` 的字段名逐字保留（``round`` / ``trigger_finding_ids`` /
    ``hocon_diff`` / ``plan_diff`` / ``resolved_finding_ids`` / ``created_at``），
    因为 Studio 的修复时间线直接渲染它。``created_at`` 一律转成 ISO 字符串——
    ``engines/gate/types.py`` 的 ``AutofixRound.created_at`` 是 ``datetime``，
    直接塞进 state 会让检查点写不进去（be-09 §5.2）。
    """
    from datetime import UTC, datetime

    created_at = round_obj.created_at
    created_at_str = (
        created_at.astimezone(UTC).isoformat() if isinstance(created_at, datetime) else str(created_at)
    )
    return {
        "round": int(round_obj.round),
        "trigger_finding_ids": [str(x) for x in round_obj.trigger_finding_ids],
        # diff 可能很长，但它是「这一轮改了什么」的唯一留痕，不截断。
        # 截断会让审计看到的 diff 与应用过的补丁不是一回事。
        "hocon_diff": str(round_obj.hocon_diff or ""),
        "plan_diff": None if round_obj.plan_diff is None else str(round_obj.plan_diff),
        "resolved_finding_ids": [str(x) for x in round_obj.resolved_finding_ids],
        "created_at": created_at_str,
    }


def _noop(runtime: Any, used: int, *, reason: str) -> dict[str, Any]:
    """无可修项 → 原样返回。

    ``autofix_rounds`` 显式带回：LangGraph 的 state 合并是按键的，
    少写一个键会让「本节点碰过它」这件事看不出来（而它确实是本节点的产出）。
    """
    log.info(
        "autofix_noop",
        request_id=runtime.request_id,
        tenant_id=runtime.tenant_id,
        pipeline_id=runtime.pipeline_id,
        rounds_used=used,
        reason=reason,
    )
    return {"autofix_rounds": used}


def _stop(runtime: Any, used: int, budget: int, *, reason: str) -> dict[str, Any]:
    """预算用尽 → 终局。

    这条分支不调 LLM，也就不会产生新的 ``autofix_history``——历史里保留的
    是「实际发生的修复」，而不是「本来打算修复的」。
    """
    log.info(
        "autofix_stopped",
        request_id=runtime.request_id,
        tenant_id=runtime.tenant_id,
        pipeline_id=runtime.pipeline_id,
        rounds_used=used,
        max_rounds=budget,
        reason=reason,
    )
    return {
        "autofix_rounds": used,
        "terminal_error": ErrorCode.AUTOFIX_EXHAUSTED.value,
    }
