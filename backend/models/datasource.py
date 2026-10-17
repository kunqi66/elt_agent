"""数据源、Secret 引用与元数据 Profile。

**本文件里没有任何可以存放凭据的列**——这是刻意的结构性保证：凭据只能存在于 Vault，
库里只有 ``secret_reference``（路径 + 版本 + 键名）。想存明文就得先改表结构，改不动。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from models.base import Base, CreatedAtMixin, PKMixin, ProjectScopedMixin, TimestampMixin
from models.checks import enum_col
from models.enums import DataSourceType, Env, ProbeStatus, SensitiveLevel


class SecretReference(Base, PKMixin, TimestampMixin, ProjectScopedMixin):
    """Vault 引用。**只有键名，没有键值。**"""

    __tablename__ = "secret_reference"
    __table_args__ = (
        UniqueConstraint("provider", "path", "version", name="uq_secret_reference_provider_path_version"),
    )

    provider: Mapped[str] = mapped_column(String(32), nullable=False, default="vault")
    path: Mapped[str] = mapped_column(String(255), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # 如 ["username", "password"]；仅用于前端提示「这个引用里有哪些字段」，不含值
    key_names: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    created_by: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("app_user.id", ondelete="SET NULL"), nullable=True
    )
    last_rotated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class DataSource(Base, PKMixin, TimestampMixin, ProjectScopedMixin):
    __tablename__ = "data_source"
    __table_args__ = (
        UniqueConstraint("project_id", "name", name="uq_data_source_project_id_name"),
        Index("ix_data_source_project_id_type", "project_id", "type"),
    )

    name: Mapped[str] = mapped_column(String(128), nullable=False)
    type: Mapped[DataSourceType] = mapped_column(
        enum_col(DataSourceType, name="data_source_type"), nullable=False
    )
    env: Mapped[Env] = mapped_column(enum_col(Env, name="env"), nullable=False, default=Env.DEV)
    # 连接定位信息（host/port/database/bucket/path/base_url...），**禁止**出现凭据键。
    # schemas/datasource.py 的校验器会拒绝含 password/token/secret 等键的 payload。
    endpoint: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    secret_ref_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("secret_reference.id", ondelete="RESTRICT"), nullable=True
    )
    sensitive_level: Mapped[SensitiveLevel] = mapped_column(
        enum_col(SensitiveLevel, name="sensitive_level"), nullable=False, default=SensitiveLevel.INTERNAL
    )
    tags: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    owner_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("app_user.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    last_tested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_test_ok: Mapped[bool | None] = mapped_column(nullable=True)

    secret_ref: Mapped[SecretReference | None] = relationship(lazy="joined")
    profiles: Mapped[list[MetadataProfile]] = relationship(
        back_populates="data_source", lazy="raise", order_by="MetadataProfile.version.desc()"
    )


class MetadataProfile(Base, PKMixin, CreatedAtMixin, ProjectScopedMixin):
    """只读探查产物，**版本递增、只增不改**。

    ``sha256`` 是 Profile 的指纹，PipelineVersion 生成时绑定它；
    Profile 被重新探查后指纹变化，旧版本方案会被门禁提示重新生成（PRD §12「Metadata Profile 过期」）。
    """

    __tablename__ = "metadata_profile"
    __table_args__ = (
        UniqueConstraint("data_source_id", "version", name="uq_metadata_profile_data_source_id_version"),
        Index("ix_metadata_profile_data_source_id_sha256", "data_source_id", "sha256"),
    )

    data_source_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("data_source.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[ProbeStatus] = mapped_column(
        enum_col(ProbeStatus, name="probe_status"), nullable=False, default=ProbeStatus.PROBING
    )
    # 表/字段/类型/可空/主键/近似统计（row_count_approx、null_ratio、distinct_approx...）
    schema_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    sampling: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    # 脱敏后的样本。写入前已脱敏，**原文不落库、不进日志、不进模型上下文**
    masked_samples: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    masking_policy: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    tables_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_by: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("app_user.id", ondelete="SET NULL"), nullable=True
    )

    data_source: Mapped[DataSource] = relationship(back_populates="profiles", lazy="joined")
