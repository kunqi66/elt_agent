"""执行状态层：运行实例、时序指标、质量报告与错误分流记录（PRD §8.1 第三层）。

与 Workflow State 严格隔离：执行状态**不得**被对话状态覆盖。同一 PipelineVersion
任一时刻只允许一条活跃运行，由部分唯一索引在数据库层强制（见 ``execution_run``）。
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
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from models.base import Base, CreatedAtMixin, PKMixin, ProjectScopedMixin, TimestampMixin
from models.checks import enum_col
from models.enums import RunStatus


class ExecutionRun(Base, PKMixin, TimestampMixin, ProjectScopedMixin):
    """一次实际执行实例。状态可变，因此不是不可变制品。"""

    __tablename__ = "execution_run"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_execution_run_idempotency_key"),
        # 同一版本同时只允许一条活跃运行：排队/运行/暂停/中断/回滚中互斥
        Index(
            "uq_execution_run_active_per_version",
            "pipeline_version_id",
            unique=True,
            postgresql_where=text("status IN ('queued','running','paused','interrupted','rolling_back')"),
        ),
        Index("ix_execution_run_project_id_status", "project_id", "status"),
    )

    pipeline_version_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("pipeline_version.id", ondelete="RESTRICT"), nullable=False
    )
    preparation_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("preparation.id", ondelete="RESTRICT"), nullable=False
    )
    capability_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("capability.id", ondelete="SET NULL"), nullable=True
    )
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    seatunnel_job_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

    status: Mapped[RunStatus] = mapped_column(
        enum_col(RunStatus, name="run_status"), nullable=False, default=RunStatus.QUEUED
    )
    # 影子表与正式表全名：原子 Swap 的依据（Doris REPLACE / MySQL RENAME 都用它们）
    shadow_table: Mapped[str | None] = mapped_column(String(255), nullable=True)
    formal_table: Mapped[str | None] = mapped_column(String(255), nullable=True)
    error_table: Mapped[str | None] = mapped_column(String(255), nullable=True)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    read_rows: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    written_rows: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    written_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    reject_rows: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    avg_throughput: Mapped[float] = mapped_column(Numeric(18, 4), nullable=False, default=0)

    triggered_by: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("app_user.id", ondelete="SET NULL"), nullable=True
    )
    # 硬中断/失败现场：最后若干指标点 + 错误摘要（**已脱敏**），用于根因诊断
    failure_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    metrics: Mapped[list[ExecutionMetric]] = relationship(back_populates="run", lazy="raise")
    quality_report: Mapped[QualityReport | None] = relationship(
        back_populates="run", lazy="joined", uselist=False
    )


class ExecutionMetric(Base, PKMixin, CreatedAtMixin):
    """时序指标，**增量上报**（PRD §10.4：避免高频全量刷新）。"""

    __tablename__ = "execution_metric"
    __table_args__ = (Index("ix_execution_metric_run_id_ts", "run_id", "ts"),)

    run_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("execution_run.id", ondelete="CASCADE"), nullable=False
    )
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # 均为**累计值快照**，前端用相邻两点的差值算瞬时速率
    read_rows: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    written_rows: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    written_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    duration_sec: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    throughput: Mapped[float] = mapped_column(Numeric(18, 4), nullable=False, default=0)
    amplification_ratio: Mapped[float] = mapped_column(Numeric(6, 4), nullable=False, default=0)
    reject_rate: Mapped[float] = mapped_column(Numeric(6, 4), nullable=False, default=0)

    run: Mapped[ExecutionRun] = relationship(back_populates="metrics")


class QualityReport(Base, PKMixin, CreatedAtMixin):
    """质量契约命中与分流结果。``passed`` 决定能否进入原子发布。"""

    __tablename__ = "quality_report"
    __table_args__ = (UniqueConstraint("run_id", name="uq_quality_report_run_id"),)
    __immutable__ = True

    run_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("execution_run.id", ondelete="CASCADE"), nullable=False
    )
    valid_rows: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    rejected_rows: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    reject_rate: Mapped[float] = mapped_column(Numeric(6, 4), nullable=False, default=0)
    # {"E_TYPE_CAST": 1203, "E_NULL_PK": 88}
    error_code_dist: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    rule_hits: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # 可解释诊断：规则 → 字段 → 样本摘要（脱敏）
    diagnostics: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    run: Mapped[ExecutionRun] = relationship(back_populates="quality_report")


class ErrorRecord(Base, PKMixin, CreatedAtMixin):
    """错误表分流记录的**控制面副本**。

    只存 ``payload_digest``（受保护摘要），**不存违规行原文**——错误表明细在目标端，
    控制面保留摘要用于统计与追溯，避免敏感值随错误数据回流到控制面。
    """

    __tablename__ = "error_record"
    __table_args__ = (Index("ix_error_record_run_id_error_code", "run_id", "error_code"),)
    __immutable__ = True

    run_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("execution_run.id", ondelete="CASCADE"), nullable=False
    )
    rule_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    error_code: Mapped[str] = mapped_column(String(32), nullable=False)
    field_name: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    payload_digest: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    reason: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
