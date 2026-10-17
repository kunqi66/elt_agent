"""异步数据库引擎与会话。

约定：
- 控制面只用 PostgreSQL；演示源库（MySQL/Doris）由 ``clients/`` 下的独立连接器管理，
  **不要**在这里注册它们。
- 事务边界由 service 层显式控制（``async with session.begin()``）。
  有副作用的流程（Commit、Swap）必须把「业务事实 + Outbox 消息」放进同一个事务，
  这是 Transactional Outbox 的前提，不得拆成两次提交。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from core.config import settings

engine: AsyncEngine = create_async_engine(
    settings.postgres_dsn,
    echo=settings.POSTGRES_ECHO,
    pool_size=settings.POSTGRES_POOL_SIZE,
    max_overflow=settings.POSTGRES_MAX_OVERFLOW,
    pool_pre_ping=True,  # 防止 ECS↔PG 之间的空闲连接被中间设备静默断开
    pool_recycle=1800,
    future=True,
)

SessionFactory = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,  # 提交后仍可读取 ORM 属性，避免序列化时触发额外查询
    autoflush=False,
)


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI 依赖：请求级会话。不自动提交，由 service 决定事务边界。"""
    async with SessionFactory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Celery 任务与脚本用：进入即开事务，异常自动回滚。"""
    async with SessionFactory() as session:
        async with session.begin():
            yield session


async def dispose_engine() -> None:
    await engine.dispose()
