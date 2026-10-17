"""门禁判定（be-09 §5.1 / §6.3：只调不判）。

**本节点不含任何门禁规则**——五类检查与结论合成都在 be-11 的 ``engines/gate/``，
数据访问与 PDP 的异步计算在 ``services/pipeline/gating.run_gate``。
本节点做三件事：

1. 调 ``run_gate``（service 负责把 Profile、策略结论、数据源类型组装成 ``GateContext``）；
2. 用 ``transitions.status_after_gate`` 把结论映射成 ``PipelineStatus`` 并**走转移表**——
   非法转移当场 409，而不是把状态硬写进去；
3. 把 ``GateResult`` 以 ``model_dump(mode="json")`` 写进 state。

第 3 步的 ``mode="json"`` 不是可选项：``GateResult.evaluated_at`` 是 ``datetime``、
``AutofixRound.created_at`` 也是，直接 ``model_dump()`` 会把两个 datetime 塞进 state，
LangGraph 写检查点时才炸（或写出一份读不回来的检查点）。be-09 §5.2 的硬性约束
在 :func:`workflow.state.assert_json_serializable` 里也有一道，这里是最常见的触发点。

**只把 ``GateOutcome.result`` 写进 state，不写 ``context``**：ctx 里挂着
``TableProfile``（含列与脱敏样本）、``PolicyDecision`` 等只在本次判定内有效的对象，
它既不是 JSON 可序列化的，也**不该**进检查点——检查点是跨请求存活的东西，
把「那一刻读到的档案快照」冻进去，会让下一次恢复拿着过期档案继续算。
自动修复需要 ctx 时自己在节点内重建一份（见 ``nodes/autofix.py``），
重建的代价是一次读库，换来的是「每一次判定都基于当前事实」。

**门禁阻断不是 HTTP 错误**（be-09 §3.1）：``block`` 只把状态推到 ``gate_failed``，
由自动修复回路接手；只有「阻断态下强行冻结」才是 ``GATE_BLOCKED``（由 freeze 节点判）。
"""

from __future__ import annotations

from typing import Any

from core.errors import AppError, ErrorCode
from core.ids import utcnow
from core.logging import get_logger
from models.enums import PipelineStatus
from workflow.nodes import runtime_of
from workflow.state import input_fingerprint
from workflow.transitions import assert_transition, status_after_gate

__all__ = ["gate_node"]

log = get_logger(__name__)

_NODE = "gate"


def _rounds_of(state: dict[str, Any]) -> list[Any]:
    """state 里的 ``autofix_history`` → ``AutofixRound`` 列表。

    门禁结论里要带「跑了哪几轮修复」，而这条信息在 state 里是**已序列化的 dict**
    （见 ``nodes/autofix.py`` 的 ``_round_summary``）。这里用 ``model_validate``
    而不是就地改 dict：``AutofixRound`` 的字段若变了，这里当场报错，
    而不是把一个缺字段的 dict 塞进结论、由前端渲染时才发现少了 diff。
    """
    from engines.gate.types import AutofixRound

    return [AutofixRound.model_validate(item) for item in (state.get("autofix_history") or [])]


async def gate_node(state: dict[str, Any], config: Any = None) -> dict[str, Any]:
    """图节点：跑五类检查并落结论。"""
    runtime = runtime_of(config)
    plan_dict = state.get("draft_plan")
    hocon = state.get("draft_hocon")
    if not isinstance(plan_dict, dict) or not isinstance(hocon, str):
        raise AppError(
            "草稿制品不完整，无法执行门禁（需要 EtlPlan 与 HOCON 同时存在）",
            code=ErrorCode.PLAN_INVALID,
            http_status=422,
        )

    from engines.plan.schema import EtlPlanModel
    from services.pipeline.gating import run_gate

    fingerprint = input_fingerprint(
        plan=plan_dict,
        hocon=hocon,
        profiles=state.get("profile_sha256") or {},
    )
    # 幂等：**同一份草稿**的门禁结论可以复用。指纹覆盖 plan + hocon + 档案指纹，
    # 所以「修复后重新渲染的 HOCON」一定能重新跑门禁——这正是自动修复回路的基础。
    if isinstance(state.get("gate_result"), dict) and (
        state.get("node_fingerprints", {}).get(_NODE) == fingerprint
    ):
        log.info(
            "gate_skipped",
            request_id=runtime.request_id,
            pipeline_id=runtime.pipeline_id,
            reason="same_artifact",
        )
        return {}

    plan = EtlPlanModel.model_validate(plan_dict)
    # ``now`` 由调用方注入（engines 不许自己取当前时间）：门禁的 evaluated_at
    # 与账本时间因此是同一个时刻，事后对时间线不会错开。
    outcome = await run_gate(
        session=runtime.session,
        subject=runtime.ctx,
        project_id=runtime.project_id,
        plan=plan,
        hocon_content=hocon,
        now=utcnow(),
        autofix_rounds=_rounds_of(state),
    )
    result = outcome.result

    conclusion = str(result.conclusion)
    # 两步走而不是「把 verdict 直接映射成状态」：``status_after_gate`` 负责**验证**
    # 结论取值（未知结论当场 ValueError，而不是被当成 pass），
    # ``assert_transition`` 负责**验证转移合法性**。两者少一个都会让
    # 「引擎多出第四档结论」或「从 version_frozen 重跑门禁」这类错误静默通过。
    target = status_after_gate(conclusion)
    event = "gate_blocked" if target is PipelineStatus.GATE_FAILED else "gate_passed"
    current = PipelineStatus(str(state.get("status") or PipelineStatus.DRAFT))
    new_status = assert_transition(
        current, event, pipeline_id=runtime.pipeline_id, request_id=runtime.request_id
    )

    findings = list(result.findings)
    log.info(
        "gate_evaluated",
        request_id=runtime.request_id,
        tenant_id=runtime.tenant_id,
        project_id=runtime.project_id,
        pipeline_id=runtime.pipeline_id,
        conclusion=conclusion,
        blocking=len(result.blocking),
        warnings=len([f for f in findings if f.severity == "warn"]),
        has_policy_block=result.has_policy_block,
    )
    return {
        "gate_result": result.model_dump(mode="json"),
        "status": str(new_status),
        # 门禁结论**平铺**进 HOCON 制品元信息（渲染段 ⊕ 解析/门禁段 = 完整的
        # ``hocon_artifact.validation_result``）：``save_artifacts`` 落到那里，
        # 而 ``artifacts.freeze_version`` 的「门禁不得有阻塞项」与
        # ``get_gate`` 都是从 ``validation_result["gate"]`` 读回来的——
        # 冻结看的是**落库的**结论，不是内存里的。
        # **别再套一层**（写成 ``{"gate": artifact_meta()}``）：那样
        # ``validation_result["gate"]`` 就变成了信封，``get_gate`` 拿它当
        # ``GateResult`` 解析会报「evaluated_at/conclusion 缺失」，
        # 表面看像门禁没跑，实际是元信息套了两层。
        "hocon_meta": {**(state.get("hocon_meta") or {}), **outcome.artifact_meta()},
        "node_fingerprints": {_NODE: fingerprint},
    }
