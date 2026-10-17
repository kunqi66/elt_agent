"""四眼审批（be-13 §5.2）。

本模块只做一件事：把一个具名审批决定落成**不可变**的一行，并按状态机推进准备单。
所有拒绝都分成两类，处置不同：

- **协议类拒绝**（自批、职责冲突、指纹失效）→ 写证据账本再抛。
  它们是「有人试图绕过流程」，本身就是证据；不记的话这次尝试与「什么都没发生」等价。
- **状态类拒绝**（状态不允许、该槽已批过、不在必需槽里）→ 只写 structlog 不写账本。
  那是客户端拿着过期页面在点按钮，不是攻击，记进不可变账本只会制造噪声
  （而且账本是单入口的哈希链，噪声会让真正的证据更难找）。

职责判定的纯函数一律复用 ``engines.policy.roles``，**本模块不重写**：
两处各写一份「谁算自批」，症状是审批页放行、服务端拒绝（或反过来），而两边都自洽。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession
from structlog import get_logger

from core.errors import (
    ConflictError,
    DutyConflictError,
    FingerprintMismatchError,
    PreparationExpiredError,
    RoleRequiredError,
    SelfApprovalForbiddenError,
    ValidationError,
)
from core.ids import as_utc, utcnow
from engines.plan.schema import EtlPlanModel
from engines.policy import check_dual_checker_distinct, check_duty_separation, has_role, is_self_approval
from harness.ledger import EvidenceLedger, LedgerEvent
from models.enums import (
    ApprovalDecisionType,
    ApprovalSlot,
    EvidenceEventType,
    PipelineStatus,
    PreparationStatus,
    RoleName,
)
from models.governance import ApprovalDecision, Preparation
from models.pipeline import PipelineVersion
from schemas.auth import SubjectContext
from schemas.preparation import PreparationDetail
from services.pipeline import repository
from services.preparation import (
    current_input_fingerprint,
    decisions_of,
    get_preparation,
    load_preparation,
)
from services.project import active_roles

log = get_logger(__name__)

_ledger = EvidenceLedger()

#: 可以继续接受决定的准备单状态（终态一律拒绝，见 §5.3）。
_LIVE_STATUSES: frozenset[PreparationStatus] = frozenset(
    {PreparationStatus.PENDING, PreparationStatus.PARTIALLY_APPROVED}
)

#: 决定类型 → 证据事件类型。三种决定的留痕口径必须与账本枚举一一对应。
_EVENT_OF: dict[ApprovalDecisionType, EvidenceEventType] = {
    ApprovalDecisionType.APPROVE: EvidenceEventType.APPROVAL_GRANTED,
    ApprovalDecisionType.REJECT: EvidenceEventType.APPROVAL_REJECTED,
    ApprovalDecisionType.RETURN: EvidenceEventType.APPROVAL_RETURNED,
}


def derive_status(
    required: Sequence[ApprovalSlot],
    decisions: Mapping[ApprovalSlot, ApprovalDecisionType],
) -> PreparationStatus:
    """状态推导（纯函数，be-13 §6.1）。

    优先级：``reject`` > ``return`` > 全部 approve > 部分 approve > ``pending``。

    「任一槽 reject 就是 rejected」必须排在「全部 approve」之前：否则一个槽拒绝、
    另一个槽通过时，推导结果取决于遍历顺序——而遍历顺序取决于库里行的顺序，
    那是「同一份数据两次算出不同状态」的经典来源。
    """
    if not required:
        # 免审批的准备单（P3）没有槽可等：走到这里就是「无异议」，直接通过。
        # 这与「必需槽没集齐」是两件事，靠调用方传空序列来区分。
        return PreparationStatus.APPROVED
    values = [decisions.get(slot) for slot in required]
    if ApprovalDecisionType.REJECT in values:
        return PreparationStatus.REJECTED
    if ApprovalDecisionType.RETURN in values:
        # 退回不叫「拒绝」：本单作废（要重新准备），但没有给这次变更定性。
        # 状态机里它落在 superseded（见 §5.3）。
        return PreparationStatus.SUPERSEDED
    approved = [value for value in values if value is ApprovalDecisionType.APPROVE]
    if len(approved) == len(required):
        return PreparationStatus.APPROVED
    if approved:
        return PreparationStatus.PARTIALLY_APPROVED
    return PreparationStatus.PENDING


async def decide(
    session: AsyncSession,
    *,
    preparation_id: int,
    subject: SubjectContext,
    slot: ApprovalSlot,
    decision: ApprovalDecisionType,
    comment: str,
    idempotency_key: str = "",
) -> PreparationDetail:
    """记录一条审批决定并推进准备单状态。

    ``slot`` 是对设计签名（§3.2）的**补全**：那条签名漏了槽位，而「批的是哪个槽」
    是这条决定的一半语义——没有它，服务端只能替调用方猜（猜错的后果是
    「数据审批人批的那一条被记成安全槽」）。请求 DTO（``DecisionRequest``）本来就有这个字段。

    ``idempotency_key`` 收下但**不在这里用**：重放保护由 ``api/idempotency.py`` 的中间件
    在更外层完成（同一租户 + 同一路由 + 同一 key 直接回放响应快照），
    在这里再实现一遍，会得到两套幂等语义——而它们对「同 key 不同 body」的处置必然不同。
    """
    now = utcnow()
    row = await load_preparation(session, tenant_id=subject.tenant_id, preparation_id=preparation_id)
    version = await repository.load_version(
        session=session, tenant_id=subject.tenant_id, version_id=row.pipeline_version_id
    )
    pipeline = await repository.load_pipeline(
        session=session, tenant_id=subject.tenant_id, pipeline_id=version.pipeline_id
    )

    # ① 角色：审这个槽的人必须真持有对应职责槽。角色**此刻**从 role_assignment 重查，
    #    不信 JWT 声明（撤销后旧令牌仍在有效期内，信任它等于认可一次越权）。
    role = RoleName(slot.value)
    held = await active_roles(session, project_id=row.project_id, user_id=subject.user_id, at=now)
    if not has_role(held, role):
        raise RoleRequiredError(f"当前用户在该项目不持有 {role.value} 职责槽，不能审批该槽")

    required = [ApprovalSlot(item) for item in row.required_slots]

    # ② 槽匹配：这份准备单需不需要这个槽
    if slot not in required:
        raise ValidationError(
            f"准备单不需要 {slot.value} 槽（必需槽：{'、'.join(item.value for item in required) or '无'}）"
        )

    existing = await _decisions(session, preparation_id=row.id)

    # ③ 自批拦截（协议类）：申请人不得审批自己提交的单。DB 层还有 CHECK 兜底，
    #    所以这一条既拦在服务端、也拦在数据库——两层都要有，前端置灰不算。
    if is_self_approval(row.created_by or 0, subject.user_id):
        await _deny(
            session,
            event_type=EvidenceEventType.APPROVAL_SELF_DENIED,
            row=row,
            subject=subject,
            slot=slot,
            now=now,
            reason="self_approval",
        )
        await session.commit()
        raise SelfApprovalForbiddenError()

    # ④ 职责混用（协议类）：maker 不能兼任 checker；两个 checker 槽不能同人
    other = _other_slot(slot)
    try:
        check_duty_separation(held, granting=role)
        if other in existing:
            # 把「我正要占的这个槽」与「另一槽已经是谁投的」摆成一对比较。
            # 另一槽还没决定时跳过——四眼查的是「两条决定是不是同一个人」，
            # 只有一条决定时这个问题还不存在。
            data_checker = (
                subject.user_id if slot is ApprovalSlot.CHECKER_DATA else existing[other].approver_id
            )
            security_checker = (
                subject.user_id
                if slot is ApprovalSlot.CHECKER_SECURITY
                else existing[other].approver_id
            )
            check_dual_checker_distinct(data_checker, security_checker)
    except ValueError as exc:
        await _deny(
            session,
            event_type=EvidenceEventType.APPROVAL_DUTY_DENIED,
            row=row,
            subject=subject,
            slot=slot,
            now=now,
            reason="duty_conflict",
        )
        await session.commit()
        raise DutyConflictError(str(exc)) from exc

    # ⑤ 指纹一致性（协议类）：制品与输入都必须还是审批页上那一个
    plan_row, _hocon_row, quality_row, supervision_row = await repository.artifact_rows(
        session=session, version=version
    )
    current_fp = await current_input_fingerprint(
        session=session,
        tenant_id=subject.tenant_id,
        version=version,
        plan=_plan_of(plan_row),
        quality=quality_row,
        supervision=supervision_row,
    )
    if version.sha256 != row.artifact_sha256 or current_fp != row.input_fingerprint:
        await _deny(
            session,
            event_type=EvidenceEventType.APPROVAL_STALE_DENIED,
            row=row,
            subject=subject,
            slot=slot,
            now=now,
            reason="fingerprint_mismatch",
        )
        await session.commit()
        raise FingerprintMismatchError(
            "指纹已变化（制品或输入），这条审批不能记录：请重新发起准备后再审批"
        )

    # ⑥ 状态合法性（状态类）：只有活跃态接受决定。终态上再投票是客户端页面过期，
    #    不入账本（见模块 docstring），只留 structlog 便于排查「按钮为什么点不动」。
    if row.status not in _LIVE_STATUSES:
        log.warning(
            "preparation_decide_rejected",
            preparation_id=row.id,
            status=str(row.status),
            slot=slot.value,
            decision=decision.value,
            reason="not_live",
            request_id=subject.request_id,
        )
        raise ConflictError(
            f"准备单当前状态为 {row.status.value}，不再接受审批决定，请刷新后查看"
        )

    # ⑦ 有效期是硬边界：批到一半过期了也不能继续（过期不做续期）。
    #    过 ``as_utc``：库里读出来的时间在 SQLite 上是**裸值**（不保留时区），
    #    直接与带时区的 ``now`` 比较会 TypeError——而那是测试环境才暴露的形态，
    #    生产（timestamptz）永远带时区，所以这个坑只在跑用例时才现形。
    if row.expires_at is not None and as_utc(row.expires_at) <= now:
        raise PreparationExpiredError()

    # ⑧ 不覆盖既有决定：approval_decision 不可变，一个槽只有一条
    if slot in existing:
        log.warning(
            "preparation_decide_rejected",
            preparation_id=row.id,
            status=str(row.status),
            slot=slot.value,
            decision=decision.value,
            reason="slot_already_decided",
            request_id=subject.request_id,
        )
        raise ConflictError(f"{slot.value} 槽已存在审批决定，审批不可覆盖，请刷新后查看")

    # ⑨ 落决定：冗余列 preparation_created_by 让自批拦截在 DB 层也能成立
    record = ApprovalDecision(
        tenant_id=row.tenant_id,
        project_id=row.project_id,
        preparation_id=row.id,
        slot=slot,
        approver_id=subject.user_id,
        approver_role=role.value,
        preparation_created_by=row.created_by or subject.user_id,
        decision=decision,
        comment=comment,
        artifact_sha256=row.artifact_sha256,
        fingerprint_at_decision=current_fp,
        decided_at=now,
    )
    session.add(record)
    await session.flush()

    # ⑩ 状态推导 + 推进（UPDATE preparation.status 走 ORM 的白名单守卫）
    existing[slot] = record
    new_status = derive_status(
        required, {item: entry.decision for item, entry in existing.items()}
    )
    previous = row.status
    row.status = new_status
    await session.flush()

    # ⑪ 流程状态推进：approved/rejected 是流程层面的结论。这里**不用转移表**——
    #    它的出边只覆盖 M2 的生成/冻结段（见 workflow/transitions.py 的 OWNED_STATES），
    #    审批之后的状态归 M3；等 be-14 起把出边补全，这条就该改回 assert_transition。
    if new_status is PreparationStatus.APPROVED:
        await repository.set_pipeline_status(
            session=session, pipeline_id=pipeline.id, status=PipelineStatus.APPROVED
        )
    elif new_status is PreparationStatus.REJECTED:
        await repository.set_pipeline_status(
            session=session, pipeline_id=pipeline.id, status=PipelineStatus.REJECTED
        )

    await _ledger.append(
        session,
        LedgerEvent(
            tenant_id=row.tenant_id,
            project_id=row.project_id,
            actor_id=subject.user_id,
            event_type=_EVENT_OF[decision],
            subject_type="preparation",
            subject_id=str(row.id),
            artifact_hash=row.artifact_sha256,
            payload={
                "slot": slot.value,
                "role": role.value,
                "decision": decision.value,
                # 意见**正文进账本**：它是审批人对自己结论的说明，属于「谁在什么理由下批的」。
                # 审批意见必须是可审计内容，因此 API 层禁止在意见里写口令/密钥
                # （schemas/preparation.py 的长度上限是防滥用，不是防泄密——泄密由
                # 「Secret 只能说引用」这条红线兜底）。
                "comment": comment,
                "previous_status": previous.value,
                "status": new_status.value,
                "fingerprint": current_fp,
            },
            occurred_at=now,
        ),
    )

    log.info(
        "preparation_decided",
        preparation_id=row.id,
        slot=slot.value,
        decision=decision.value,
        previous_status=previous.value,
        status=new_status.value,
        request_id=subject.request_id,
    )
    await session.commit()
    return await get_preparation(session, subject, row.id)


def _other_slot(slot: ApprovalSlot) -> ApprovalSlot:
    """另一个审批槽。四眼的两槽是固定的一对，不是任意集合。"""
    return (
        ApprovalSlot.CHECKER_SECURITY
        if slot is ApprovalSlot.CHECKER_DATA
        else ApprovalSlot.CHECKER_DATA
    )


async def _decisions(
    session: AsyncSession, *, preparation_id: int
) -> dict[ApprovalSlot, ApprovalDecision]:
    """本模块内的短名字，实现归 ``services.preparation.decisions_of``。

    两处各写一遍「这张准备单有哪些决定」，会在「要不要按 decided_at 排序」
    这类细节上分叉——而分叉的症状是审批侧与读侧对同一张单给出不同结论。
    """
    return await decisions_of(session, preparation_id=preparation_id)


async def _deny(
    session: AsyncSession,
    *,
    event_type: EvidenceEventType,
    row: Preparation,
    subject: SubjectContext,
    slot: ApprovalSlot,
    now: datetime,
    reason: str,
) -> None:
    """协议类拒绝的留痕。**不提交**：由调用方在抛异常前 commit——

    把提交放在这里会诱使调用方「反正它自己会提交」而忘记事务边界，
    而账本事件与业务行的原子性正是靠同一个事务保证的。
    """
    await _ledger.append(
        session,
        LedgerEvent(
            tenant_id=row.tenant_id,
            project_id=row.project_id,
            actor_id=subject.user_id,
            event_type=event_type,
            subject_type="preparation",
            subject_id=str(row.id),
            artifact_hash=row.artifact_sha256,
            payload={
                "slot": slot.value,
                "reason": reason,
                "preparation_created_by": row.created_by or 0,
                "attempted_by": subject.user_id,
            },
            occurred_at=now,
        ),
    )


def _plan_of(row: Any) -> EtlPlanModel:
    return EtlPlanModel.model_validate(row.plan)


async def approval_state(
    session: AsyncSession, *, tenant_id: int, preparation: Preparation
) -> tuple[dict[ApprovalSlot, ApprovalDecisionType], PipelineVersion]:
    """给读接口用的小工具：槽 → 决定，附带版本行（详情页要 version_no）。"""
    records = (await _decisions(session, preparation_id=preparation.id)).items()
    decisions = {slot: record.decision for slot, record in records}
    version = await repository.load_version(
        session=session, tenant_id=tenant_id, version_id=preparation.pipeline_version_id
    )
    return decisions, version


__all__ = ["approval_state", "decide", "derive_status"]
