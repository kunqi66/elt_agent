"""支撑表：幂等键与登录尝试。

这两张表都在**控制面内部**使用，不属于业务实体，但同样受租户隔离约束。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, Boolean, DateTime, Index, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from models.base import Base, CreatedAtMixin, PKMixin, TenantMixin


class IdempotencyKey(Base, PKMixin, CreatedAtMixin, TenantMixin):
    """幂等键：同一 ``(tenant_id, scope, key)`` 只执行一次。

    与 Outbox 的分工：Outbox 保证「消息一定发出去」，本表保证「同一个请求只生效一次」。
    两者缺一不可——Outbox 是至少一次投递，重复投递由本表在消费端拦下。
    """

    __tablename__ = "idempotency_key"
    __table_args__ = (
        UniqueConstraint("tenant_id", "scope", "key", name="uq_idempotency_key_tenant_id_scope_key"),
        Index("ix_idempotency_key_expires_at", "expires_at"),
    )

    scope: Mapped[str] = mapped_column(String(64), nullable=False)
    key: Mapped[str] = mapped_column(String(128), nullable=False)
    # 请求指纹：同 key 不同请求体视为冲突（客户端复用 key 却改了内容）
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    # 首次执行的响应快照，重放时直接返回同一结果
    response_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    resource_type: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    resource_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    completed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class LoginAttempt(Base, PKMixin, CreatedAtMixin):
    """登录尝试审计。

    Redis 计数器负责**快速锁定**，本表负责**可追溯**——两者都要有：
    Redis 可能被清空或重启，审计记录不能依赖易失存储。
    """

    __tablename__ = "login_attempt"
    __table_args__ = (
        Index("ix_login_attempt_username_created_at", "username", "created_at"),
        Index("ix_login_attempt_ip_created_at", "ip", "created_at"),
    )

    username: Mapped[str] = mapped_column(String(128), nullable=False)
    ip: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    user_agent: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    success: Mapped[bool] = mapped_column(Boolean, nullable=False)
    # 失败原因码（不含口令相关细节，避免成为爆破提示）
    reason: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
