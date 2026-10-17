"""冻结候选版本（be-09 §5.1 / §5.4：只调 service 并回写 state）。

## 顺序（不能换）

冻结的 service 入口是 ``services/pipeline/artifacts.freeze_version(version_id=...)``，
它要求**制品已经落库、且候选行上的指纹与落库制品一致**（它自己会重算一次比对）。
所以本节点按下面四步走，前三步都是 `repository` 的纯数据访问：

1. ``repository.save_artifacts`` —— 插一组**新的**不可变制品行（etl_plan /
   hocon_artifact / quality_contract / supervision_contract），返回 ``ArtifactIds``；
2. 算指纹 —— ``parts.fingerprint(parts.parts_from_plan(...))``，与
   ``services/pipeline/generation._persist_turn`` **同一个拼法**（唯一实现在 ``parts``）；
3. ``repository.get_or_create_candidate`` —— 取（或建）候选行并让它指向这组制品，
   同时把指纹写进 ``pipeline_version.sha256``；
4. ``artifacts.freeze_version`` —— 落 ``frozen_at``，整行从此不可改，
   并把 ``pipeline.current_version_id`` 指过去、状态转 ``version_frozen``。

第 2 步的指纹**必须**与第 4 步的重算对得上，否则 ``freeze_version`` 会以
「制品在生成之后被改动过」拒冻。这不是重复劳动：一边写、一边验，
是「审批看到的」与「执行拿到的」是同一份东西的唯一保证。

## 与 ``generation._persist_turn`` 的关系（已知重复）

``_persist_turn`` 在**每一轮结束**（含中断）之后也做第 1–3 步。图内的冻结必须
自己再做一遍，因为图执行期间 service 还没机会落库——第一次生成就是「一个 turn 从
需求直接走到冻结」，那时库里一条制品行都还没有。

代价是：冻结那一轮结束后 ``_persist_turn`` 会再插一组制品行，并（因为候选行已冻结、
``_current_candidate`` 查不到）新建一个空候选。**这是 service 侧的整合问题**，
不在本模块的改动范围内（本模块不许改 ``services/``），已记入交付说明：
正确的收敛方式是 ``_persist_turn`` 在 ``frozen_version_id`` 已存在时直接返回。

## 为什么不在本节点写 ``PIPELINE_VERSION_FROZEN`` 证据

be-09 §4 的「本模块读/写」表只把 ``PIPELINE_GENERATED`` 记在本模块名下，
冻结的事件归 be-12 的 service（``freeze_version`` 的事务内）。两处都写会产出两条
语义相同的事件，而账本是哈希链——重复不是「多一份保险」，是审计口径被稀释。
"""

from __future__ import annotations

from typing import Any

from core.errors import AppError, ErrorCode
from core.ids import utcnow
from core.logging import get_logger
from models.enums import PipelineStatus
from workflow.nodes import runtime_of
from workflow.transitions import assert_transition

__all__ = ["freeze_node"]

log = get_logger(__name__)

_NODE = "freeze"


async def freeze_node(state: dict[str, Any], config: Any = None) -> dict[str, Any]:
    """图节点：候选 → 不可变版本。"""
    runtime = runtime_of(config)

    gate = state.get("gate_result") or {}
    if str(gate.get("conclusion") or "") == "block":
        # 门禁已阻断却请求冻结：这是「绕过门禁」的尝试，必须硬拒并留下痕迹。
        # ``freeze_version`` 自己也会拦（它读落库的结论），但那要等到制品落库之后；
        # 在内存这一层先拦，能让「有人试图绕过」这件事在日志里一眼可见。
        log.warning(
            "freeze_rejected_gate_blocked",
            request_id=runtime.request_id,
            tenant_id=runtime.tenant_id,
            project_id=runtime.project_id,
            pipeline_id=runtime.pipeline_id,
        )
        raise AppError(
            "门禁结论为阻断，不允许冻结版本",
            code=ErrorCode.GATE_BLOCKED,
            http_status=409,
            details={"conclusion": "block"},
        )

    current = PipelineStatus(str(state.get("status") or PipelineStatus.DRAFT))
    new_status = assert_transition(
        current, "freeze", pipeline_id=runtime.pipeline_id, request_id=runtime.request_id
    )

    plan_dict = state.get("draft_plan")
    hocon = state.get("draft_hocon")
    if not isinstance(plan_dict, dict) or not isinstance(hocon, str) or not hocon:
        raise AppError(
            "草稿制品不完整，无法冻结版本（需要 EtlPlan 与非空 HOCON 同时存在）",
            code=ErrorCode.PLAN_INVALID,
            http_status=422,
        )

    from engines.plan.schema import EtlPlanModel
    from services.pipeline import parts, repository
    from services.pipeline.artifacts import freeze_version

    plan = EtlPlanModel.model_validate(plan_dict)
    meta = dict(state.get("hocon_meta") or {})
    now = utcnow()

    ids = await repository.save_artifacts(
        session=runtime.session,
        tenant_id=runtime.tenant_id,
        pipeline_id=runtime.pipeline_id,
        plan=plan,
        plan_valid=not (state.get("plan_errors") or []),
        validation_errors=list(state.get("plan_errors") or []),
        hocon_content=hocon,
        hocon_sha256=str(meta.get("sha256") or ""),
        parser_version=str(meta.get("parser_version") or ""),
        hocon_meta=meta,
    )

    digests = await repository.profile_digests(
        session=runtime.session,
        tenant_id=runtime.tenant_id,
        data_source_ids=[item.data_source_id for item in plan.inputs],
    )
    sha256 = parts.fingerprint(
        parts.parts_from_plan(
            plan=plan,
            hocon_content=hocon,
            pipeline_id=runtime.pipeline_id,
            source_metadata_sha256=parts.source_metadata_sha256(digests.values()),
        )
    )

    candidate = await repository.get_or_create_candidate(
        session=runtime.session,
        tenant_id=runtime.tenant_id,
        project_id=runtime.project_id,
        pipeline_id=runtime.pipeline_id,
        ids=ids,
        sha256=sha256,
        generation_context=dict(state.get("generation_context") or {}),
        now=now,
    )

    frozen = await freeze_version(
        session=runtime.session,
        ctx=runtime.ctx,
        version_id=int(candidate.id),
        note=f"由生成工作流冻结（修复轮次 {int(state.get('autofix_rounds') or 0)}）",
    )
    version_id = int(frozen.id)
    log.info(
        "version_frozen",
        request_id=runtime.request_id,
        tenant_id=runtime.tenant_id,
        project_id=runtime.project_id,
        pipeline_id=runtime.pipeline_id,
        pipeline_version_id=version_id,
        version_no=int(frozen.version_no),
        autofix_rounds=int(state.get("autofix_rounds") or 0),
    )
    # 转移表说 ``freeze`` 的靶点是 ``version_frozen``；对不上就说明表被改过而
    # 本节点没跟着改。宁可当场炸，也不要写一个与转移表不符的状态值。
    if str(new_status) != PipelineStatus.VERSION_FROZEN:
        raise AppError(
            f"冻结后状态异常：转移表给出 {new_status}，期望 {PipelineStatus.VERSION_FROZEN}",
            code=ErrorCode.INTERNAL_ERROR,
            http_status=500,
        )
    return {"frozen_version_id": version_id, "status": str(new_status)}
