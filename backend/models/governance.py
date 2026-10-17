"""风险策略、准备单、审批决定与单次能力令牌。

这是 Harness 内核的数据基础。三条硬约束在此落地：
1. **自批拦截**：``approval_decision`` 反范式存 ``preparation_created_by`` 并加 CHECK，
   使「审批人 ≠ 申请人」成为数据库层面的事实，任何绕过 service 的写入都会被拒绝。
2. **一个职责槽只有一条决定**：唯一约束 ``(preparation_id, slot)``。
3. **令牌不可重放**：``capability.jti`` 唯一且只落哈希，``consumed_at`` 仅允许一次由 NULL 变更。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from models.base import Base, CreatedAtMixin, PKMixin, ProjectScopedMixin, TenantMixin, TimestampMixin
from models.checks import enum_col
from models.enums import ApprovalDecisionType, ApprovalSlot, Env, PreparationStatus, RiskLevel, SensitiveLevel


class PolicyRule(Base, PKMixin, TimestampMixin):
    """P0–P3 判定规则表（PDP 的数据源）。

    ``condition`` 是**声明式**判定条件（如 ``{"env": ["prod"], "writes_formal_table": true}``），
    由 ``engines/policy/pdp.py`` 纯函数求值——规则可配、可版本化，但判定过程绝不引入 LLM。
    """

    __tablename__ = "policy_rule"
    __table_args__ = (UniqueConstraint("code", "version", name="uq_policy_rule_code_version"),)

    code: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    level: Mapped[RiskLevel] = mapped_column(enum_col(RiskLevel, name="risk_level"), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    condition: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    required_slots: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    updated_by: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("app_user.id", ondelete="SET NULL"), nullable=True
    )


class RiskPolicyVersion(Base, PKMixin, CreatedAtMixin, TenantMixin):
    """策略集版本：安全策略变更必须审批并记录生效范围（PRD §6.6）。"""

    __tablename__ = "risk_policy_version"
    __table_args__ = (
        UniqueConstraint("tenant_id", "version", name="uq_risk_policy_version_tenant_id_version"),
    )

    version: Mapped[int] = mapped_column(Integer, nullable=False)
    diff: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    approved_by: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("app_user.id", ondelete="SET NULL"), nullable=True
    )
    effective_scope: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    effective_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Preparation(Base, PKMixin, CreatedAtMixin, ProjectScopedMixin):
    """准备单：Prepare 阶段冻结的全部事实。

    Prepare **不产生外部副作用**——本表只写库，不触发任何数据面动作。
    """

    __tablename__ = "preparation"
    __table_args__ = (
        Index("ix_preparation_pipeline_version_id", "pipeline_version_id"),
        Index("ix_preparation_project_id_status", "project_id", "status"),
    )
    __immutable__ = True

    pipeline_version_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("pipeline_version.id", ondelete="RESTRICT"), nullable=False
    )
    input_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    artifact_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_scope: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    sensitive_level: Mapped[SensitiveLevel] = mapped_column(
        enum_col(SensitiveLevel, name="sensitive_level"), nullable=False, default=SensitiveLevel.INTERNAL
    )
    budget: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    rollback_plan: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    environment: Mapped[Env] = mapped_column(enum_col(Env, name="env"), nullable=False, default=Env.DEV)
    risk_level: Mapped[RiskLevel] = mapped_column(enum_col(RiskLevel, name="risk_level"), nullable=False)
    risk_reasons: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    required_slots: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    # 冻结事实全部只读；唯一例外是 status —— 审批推进需要它变化，且它只反映流程位置，
    # 不影响任何被审批的内容。白名单在 __mutable_columns__ 声明，其余列改动一律被 ORM 拒绝。
    __mutable_columns__ = frozenset({"status"})

    status: Mapped[PreparationStatus] = mapped_column(
        enum_col(PreparationStatus, name="preparation_status"),
        nullable=False,
        default=PreparationStatus.PENDING,
    )
    created_by: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("app_user.id", ondelete="SET NULL"), nullable=True
    )
    frozen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    decisions: Mapped[list[ApprovalDecision]] = relationship(back_populates="preparation", lazy="selectin")


class ApprovalDecision(Base, PKMixin, CreatedAtMixin, ProjectScopedMixin):
    """具名审批决定。一个职责槽只允许一条。"""

    __tablename__ = "approval_decision"
    __table_args__ = (
        UniqueConstraint("preparation_id", "slot", name="uq_approval_decision_preparation_id_slot"),
        # 自批拦截的数据库级兜底：审批人不得等于准备单发起人
        CheckConstraint("approver_id <> preparation_created_by", name="no_self_approval"),
        Index("ix_approval_decision_preparation_id_decision", "preparation_id", "decision"),
    )
    __immutable__ = True

    preparation_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("preparation.id", ondelete="CASCADE"), nullable=False
    )
    slot: Mapped[ApprovalSlot] = mapped_column(enum_col(ApprovalSlot, name="approval_slot"), nullable=False)
    approver_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("app_user.id", ondelete="RESTRICT"), nullable=False
    )
    approver_role: Mapped[str] = mapped_column(String(32), nullable=False)
    # 冗余列：与 preparation.created_by 比对用，使自批拦截可在 DB 层表达
    preparation_created_by: Mapped[int] = mapped_column(BigInteger, nullable=False)
    decision: Mapped[ApprovalDecisionType] = mapped_column(
        enum_col(ApprovalDecisionType, name="approval_decision_type"), nullable=False
    )
    comment: Mapped[str] = mapped_column(Text, nullable=False)
    # 审批时看到的指纹；Commit 时与当前值比对，不一致即审批失效（不物理删除）
    artifact_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    fingerprint_at_decision: Mapped[str] = mapped_column(String(64), nullable=False)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    preparation: Mapped[Preparation] = relationship(back_populates="decisions")


class Capability(Base, PKMixin, CreatedAtMixin):
    """单次执行能力令牌的**登记记录**。

    真正的令牌是签发给 Worker 的 JWT，本表只存其哈希与绑定事实。
    ``consumed_at`` 的一次性由 ``ReplayGuard`` 保证：Redis SETNX + 本表 jti 唯一约束双保险。
    """

    __tablename__ = "capability"
    __table_args__ = (
        UniqueConstraint("jti", name="uq_capability_jti"),
        Index("ix_capability_preparation_id", "preparation_id"),
    )
    __immutable__ = True
    # 签发事实不可变；只允许标记「已被消费」或「被拒绝」，且这两者都只写一次
    __mutable_columns__ = frozenset({"consumed_at", "consumed_by_run_id", "reject_reason"})

    jti: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    preparation_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("preparation.id", ondelete="RESTRICT"), nullable=False
    )
    subject_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("app_user.id", ondelete="RESTRICT"), nullable=False
    )
    tool: Mapped[str] = mapped_column(String(64), nullable=False)
    environment: Mapped[Env] = mapped_column(enum_col(Env, name="env"), nullable=False)
    artifact_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    # 令牌全文哈希，**不存令牌本身**：库被读走也无法直接用于执行
    token_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    signature_alg: Mapped[str] = mapped_column(String(16), nullable=False, default="EdDSA")
    key_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    consumed_by_run_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    reject_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
