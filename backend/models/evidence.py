"""证据账本与事务 Outbox。

``evidence_event`` 是**哈希链**：``event_hash`` 覆盖自身内容与前一条的 ``event_hash``，
因此篡改任意一条都会让后续所有条目验链失败。写入必须串行化（``harness/ledger.py`` 用
PG advisory lock 保护链尾），否则并发写会分叉。

``outbox_message`` 是可靠投递的载体：与业务事实**同事务**写入，由 beat 扫描后投递，
保证「本地事务提交了但消息没发出去」这种不一致不会发生。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from models.base import Base, CreatedAtMixin, PKMixin, TenantMixin
from models.checks import enum_col
from models.enums import EvidenceEventType, OutboxStatus


class EvidenceEvent(Base, PKMixin, CreatedAtMixin, TenantMixin):
    """链式证据事件。**只允许 INSERT**，且必须经 ``harness/ledger.append()``。"""

    __tablename__ = "evidence_event"
    __table_args__ = (
        UniqueConstraint("seq", name="uq_evidence_event_seq"),
        UniqueConstraint("event_hash", name="uq_evidence_event_event_hash"),
        Index("ix_evidence_event_tenant_id_occurred_at", "tenant_id", "occurred_at"),
        Index("ix_evidence_event_event_type", "event_type"),
        Index("ix_evidence_event_subject", "subject_type", "subject_id"),
    )
    __immutable__ = True

    # 链序：单调递增，链断点检测的依据
    seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    project_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    actor_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("app_user.id", ondelete="SET NULL"), nullable=True
    )
    event_type: Mapped[EvidenceEventType] = mapped_column(
        enum_col(EvidenceEventType, name="evidence_event_type", length=64), nullable=False
    )
    subject_type: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    subject_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    artifact_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    # **已脱敏**的事件详情。写入前由 ledger 强制过一遍脱敏，业务代码不得自行塞原文
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    prev_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    event_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class EvidenceLedgerBlock(Base, PKMixin, CreatedAtMixin, TenantMixin):
    """签名检查点：每 ``LEDGER_CHECKPOINT_INTERVAL`` 条事件打一个 Ed25519 签名。

    有了检查点，验链可以从最近的检查点开始，而不必从创世条目重算；
    同时检查点本身绑定 ``key_id``，支持密钥轮转后仍能验证历史。
    """

    __tablename__ = "evidence_ledger_block"
    __table_args__ = (UniqueConstraint("seq", name="uq_evidence_ledger_block_seq"),)
    __immutable__ = True

    seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    from_event_seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    to_event_seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    chain_head_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    signature: Mapped[str] = mapped_column(Text, nullable=False)  # base64(Ed25519 签名)
    key_id: Mapped[str] = mapped_column(String(64), nullable=False)
    signed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class OutboxMessage(Base, PKMixin, CreatedAtMixin):
    """事务 Outbox。与业务事实同事务写入，beat 每 2s 扫描投递。

    本表**可变**（status/attempts/available_at 会变化），因此不加不可变保护。
    """

    __tablename__ = "outbox_message"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_outbox_message_idempotency_key"),
        Index("ix_outbox_message_status_available_at", "status", "available_at"),
        Index("ix_outbox_message_topic", "topic"),
    )

    topic: Mapped[str] = mapped_column(String(64), nullable=False)
    # 命令载荷：**只含引用**（run_id / 版本 id / secret 引用），不得内嵌 Secret 值
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[OutboxStatus] = mapped_column(
        enum_col(OutboxStatus, name="outbox_status"), nullable=False, default=OutboxStatus.PENDING
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 是否已投递过（至少一次投递下的去重提示；真正的幂等由消费端保证）
    dispatched: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
