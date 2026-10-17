"""Pipeline 与不可变制品。

**不可变约定**：``PipelineVersion`` 及其四件制品（EtlPlan / HoconArtifact / QualityContract /
RuntimeSupervisionContract）都置 ``__immutable__ = True``，由 ``models/base.py`` 的 flush 钩子
禁止 UPDATE/DELETE。要改方案只能冻结新版本——这正是「审批结论绑定指纹」能成立的前提。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
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

from models.base import (
    Base,
    CreatedAtMixin,
    ImmutableViolationError,  # noqa: F401  (re-export 便于上层捕获)
    PKMixin,
    ProjectScopedMixin,
    TimestampMixin,
)
from models.checks import enum_col
from models.enums import PipelineStatus


class Pipeline(Base, PKMixin, TimestampMixin, ProjectScopedMixin):
    """逻辑数据集成流程。状态机见 ``PipelineStatus``（PRD §8.2）。"""

    __tablename__ = "pipeline"
    __table_args__ = (
        UniqueConstraint("project_id", "name", name="uq_pipeline_project_id_name"),
        Index("ix_pipeline_project_id_status", "project_id", "status"),
    )

    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    status: Mapped[PipelineStatus] = mapped_column(
        enum_col(PipelineStatus, name="pipeline_status"), nullable=False, default=PipelineStatus.DRAFT
    )
    current_version_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("pipeline_version.id", ondelete="SET NULL", use_alter=True), nullable=True
    )
    source_data_source_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("data_source.id", ondelete="SET NULL"), nullable=True
    )
    target_data_source_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("data_source.id", ondelete="SET NULL"), nullable=True
    )
    created_by: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("app_user.id", ondelete="SET NULL"), nullable=True
    )

    versions: Mapped[list[PipelineVersion]] = relationship(
        back_populates="pipeline",
        lazy="raise",
        order_by="PipelineVersion.version_no.desc()",
        foreign_keys="PipelineVersion.pipeline_id",
    )


class PipelineConversation(Base, PKMixin, TimestampMixin, ProjectScopedMixin):
    """Conversation State：自然语言交互历史（PRD §8.1 第一层）。

    与 Workflow State（LangGraph Checkpoint，由 langgraph-checkpoint-postgres 自建表管理）
    严格分离：这里只存「人和模型说了什么」，不存状态机进度。
    """

    __tablename__ = "pipeline_conversation"
    __table_args__ = (Index("ix_pipeline_conversation_pipeline_id_id", "pipeline_id", "id"),)

    pipeline_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("pipeline.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)  # user / assistant / system / tool
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # 区分「模型输出」与「用户确认内容」，前端要分区展示（PRD §6.3.1）
    message_kind: Mapped[str] = mapped_column(String(32), nullable=False, default="chat")
    meta: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_by: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("app_user.id", ondelete="SET NULL"), nullable=True
    )


class EtlPlan(Base, PKMixin, CreatedAtMixin):
    """结构化 ETL 方案（Plan DSL）。"""

    __tablename__ = "etl_plan"
    __immutable__ = True

    # 长度是 64 而不是 16：版本号是**自由格式的标识串**（现用值是 ``1.0``），
    # 一旦哪天写成 ``plan-dsl-2026.10`` 这种带日期的形式，16 就不够了。
    # 宽度定小了不会在写入时报错——它会在生产上某次「顺手把版本号写详细点」时炸：
    # ``varchar(n)`` 超长是 SQLSTATE 22001，整条请求 500。
    schema_version: Mapped[str] = mapped_column(String(64), nullable=False, default="1.0")
    plan: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    valid: Mapped[bool] = mapped_column(nullable=False, default=False)
    validation_errors: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)


class HoconArtifact(Base, PKMixin, CreatedAtMixin):
    """SeaTunnel HOCON 配置制品。"""

    __tablename__ = "hocon_artifact"
    __immutable__ = True

    content: Mapped[str] = mapped_column(Text, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    # **这一列曾经把整条生成链路打断过**：原宽 16，而 ``engines.hocon.PARSER_VERSION``
    # 的实际取值是 ``pyhocon+elt-render-1``（20 字符），于是「HOCON 渲染成功、
    # 门禁也判完了」，最后落库那一步 500（SQLSTATE 22001 value too long for varchar(16)）。
    # 版本串天生会变长（要记两端版本），所以宽度给足 64。
    parser_version: Mapped[str] = mapped_column(String(64), nullable=False, default="1.0")
    validation_result: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)


class QualityContract(Base, PKMixin, CreatedAtMixin):
    """字段级清洗/过滤规则与错误码标签；数据面据此把未命中规则的数据分流到错误表。"""

    __tablename__ = "quality_contract"
    __immutable__ = True

    rules: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    error_table_target: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    reject_rate_budget: Mapped[float] = mapped_column(Numeric(6, 4), nullable=False, default=0.05)


class RuntimeSupervisionContract(Base, PKMixin, CreatedAtMixin):
    """运行预算。超预警阈值告警，超硬阈值硬中断。"""

    __tablename__ = "runtime_supervision_contract"
    __immutable__ = True

    max_read_rows: Mapped[int] = mapped_column(BigInteger, nullable=False, default=100_000_000)
    max_write_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=53_687_091_200)
    max_duration_sec: Mapped[int] = mapped_column(Integer, nullable=False, default=7200)
    max_amplification_ratio: Mapped[float] = mapped_column(Numeric(6, 4), nullable=False, default=1.5)
    max_reject_rate: Mapped[float] = mapped_column(Numeric(6, 4), nullable=False, default=0.05)
    warn_threshold_pct: Mapped[int] = mapped_column(Integer, nullable=False, default=80)


class PipelineVersion(Base, PKMixin, CreatedAtMixin):
    """不可变 Pipeline 版本。``sha256`` 是全链路（审批、Capability、执行、Swap）的锚点。"""

    __tablename__ = "pipeline_version"
    __table_args__ = (
        UniqueConstraint("pipeline_id", "version_no", name="uq_pipeline_version_pipeline_id_version_no"),
        # 🔴 **不再**对 sha256 做全局唯一（原 uq_pipeline_version_sha256 已删）。
        # be-12 §10.2 的建议 (a) 就是这个：指纹全局唯一会禁掉两个合法场景——
        # ①两条 pipeline 由同一份方案派生（内容相同、指纹相同，本就该允许）；
        # ②冻结过一个版本之后，用同样的需求再生成一次候选，候选与旧版本指纹必然相同。
        # 前者会让第二条 pipeline 直接建不起来，后者会让「重新生成」报 IntegrityError，
        # 而这两个错误都长得像「指纹算错了」，排查方向会被带偏。
        # 真正需要防的是「同一 pipeline 内重复的版本号」，那由上面的 (pipeline_id, version_no) 管。
        #
        # 部分唯一索引：**一个 pipeline 最多一行候选**（frozen_at IS NULL）。
        # 这条不变式放在 DB 上而不是只靠代码：并发两次生成时，
        # 代码里的「先查有没有候选、没有就建」中间有窗口，而索引没有。
        Index(
            "uq_pipeline_version_one_candidate",
            "pipeline_id",
            unique=True,
            postgresql_where=text("frozen_at IS NULL"),
            sqlite_where=text("frozen_at IS NULL"),
        ),
        Index("ix_pipeline_version_pipeline_id_frozen_at", "pipeline_id", "frozen_at"),
    )
    __immutable__ = True
    # 候选行可改（frozen_at 还空着的那段时间）：重生成、自动修复来回改的正是这几列。
    # 不含 pipeline_id / version_no：候选可以改内容，但**不能改身份**——
    # 让它换 pipeline 或换版本号，等于把「版本号连续性」和「归属」交给一次 UPDATE。
    __mutable_columns__ = frozenset(
        {
            "etl_plan_id",
            "hocon_artifact_id",
            "quality_contract_id",
            "supervision_contract_id",
            "source_metadata_profile_id",
            "generation_context",
            "sha256",
            "frozen_by",
            "frozen_at",
            "note",
        }
    )
    __freeze_column__ = "frozen_at"

    pipeline_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("pipeline.id", ondelete="CASCADE"), nullable=False
    )
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)

    etl_plan_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("etl_plan.id", ondelete="RESTRICT"), nullable=False
    )
    hocon_artifact_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("hocon_artifact.id", ondelete="RESTRICT"), nullable=False
    )
    quality_contract_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("quality_contract.id", ondelete="RESTRICT"), nullable=False
    )
    supervision_contract_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("runtime_supervision_contract.id", ondelete="RESTRICT"), nullable=False
    )
    source_metadata_profile_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("metadata_profile.id", ondelete="SET NULL"), nullable=True
    )

    # 生成上下文：模型名/版本、提示词模板版本、修复轮次。
    # **不含**用户原始需求全文以外的敏感内容，也不含任何样本原文。
    generation_context: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    # 制品指纹 = sha256(canonical_json(etl_plan + hocon + quality + supervision + profile_sha256))
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    frozen_by: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("app_user.id", ondelete="SET NULL"), nullable=True
    )
    # NULL = **候选版本**（草案）：门禁还没通过、审批还没提交，但前端要在
    # 「一句话生成」结束后就能拉到 Plan / HOCON / 门禁结果，所以它必须有一个可寻址的 id。
    # 落值即冻结，此后整行不可改（见 __freeze_column__）。
    frozen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    note: Mapped[str] = mapped_column(String(512), nullable=False, default="")

    pipeline: Mapped[Pipeline] = relationship(
        back_populates="versions", foreign_keys=[pipeline_id], lazy="raise"
    )
    etl_plan: Mapped[EtlPlan] = relationship(lazy="joined")
    hocon_artifact: Mapped[HoconArtifact] = relationship(lazy="joined")
    quality_contract: Mapped[QualityContract] = relationship(lazy="joined")
    supervision_contract: Mapped[RuntimeSupervisionContract] = relationship(lazy="joined")
