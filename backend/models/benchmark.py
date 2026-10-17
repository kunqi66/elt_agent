"""Benchmark 评测：测试集、用例与运行结果。

分层（见《需求对齐记录》§二.6）：L0 静态注入、L1 模拟故障、L2 真实链路。
数据集**版本化**：``(name, version)`` 唯一，基线首次跑完写死，之后只与基线比较。
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
from sqlalchemy.orm import Mapped, mapped_column, relationship

from models.base import Base, CreatedAtMixin, PKMixin, TimestampMixin
from models.checks import enum_col
from models.enums import BenchmarkLayer, BenchmarkRunStatus


class BenchmarkSuite(Base, PKMixin, TimestampMixin):
    __tablename__ = "benchmark_suite"
    __table_args__ = (UniqueConstraint("name", "version", name="uq_benchmark_suite_name_version"),)

    name: Mapped[str] = mapped_column(String(128), nullable=False)
    version: Mapped[str] = mapped_column(String(32), nullable=False)
    layer: Mapped[BenchmarkLayer] = mapped_column(
        enum_col(BenchmarkLayer, name="benchmark_layer", length=4), nullable=False
    )
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # 基线指标（首次跑完写入，之后不再随运行漂移）
    baseline: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_by: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("app_user.id", ondelete="SET NULL"), nullable=True
    )

    cases: Mapped[list[BenchmarkCase]] = relationship(back_populates="suite", lazy="selectin")


class BenchmarkCase(Base, PKMixin, TimestampMixin):
    """单条评测用例。

    ``is_malicious`` 标记的用例专门用于统计 P0/P1 拦截率——按 PRD §1.4 该指标必须 100%，
    因此这些用例的期望结果通常是「被阻断」而不是「生成成功」。
    """

    __tablename__ = "benchmark_case"
    __table_args__ = (Index("ix_benchmark_case_suite_id_category", "suite_id", "category"),)

    suite_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("benchmark_suite.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    # missing_param / ambiguity / schema_mismatch / malicious_p0 / dirty_data / budget_overflow ...
    category: Mapped[str] = mapped_column(String(64), nullable=False)
    input: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    expected: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    is_malicious: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    weight: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    suite: Mapped[BenchmarkSuite] = relationship(back_populates="cases")


class BenchmarkRun(Base, PKMixin, CreatedAtMixin):
    __tablename__ = "benchmark_run"
    __table_args__ = (Index("ix_benchmark_run_suite_id_started_at", "suite_id", "started_at"),)

    suite_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("benchmark_suite.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[BenchmarkRunStatus] = mapped_column(
        enum_col(BenchmarkRunStatus, name="benchmark_run_status"),
        nullable=False,
        default=BenchmarkRunStatus.RUNNING,
    )
    # 可复现性：同一份用例在不同模型/提示词版本下的结果必须能区分
    model_version: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    prompt_version: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    cache_mode: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # {gen_success_rate, gate_pass_rate, p0p1_block_rate, false_positive_rate, recovery_rate}
    metrics: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    case_results: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    triggered_by: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("app_user.id", ondelete="SET NULL"), nullable=True
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
