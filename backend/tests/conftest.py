"""测试基础设施。

**本机没有 Docker、没有 WSL**（CLAUDE.md §9），因此凡是**纯 ORM 行为**（不可变拦截、
约束命名、枚举落库形态）都在内存 SQLite 上验证；只有真正依赖 PostgreSQL 语义的
（JSONB 查询、部分唯一索引、advisory lock、CHECK 生效）才标 ``integration`` 等 ECS 上跑。

SQLAlchemy 会把 ``JSONB``、``BigInteger + Identity`` 降级适配到 SQLite，因此同一套
模型定义两边都能建表——生产 DDL 仍然是 PG 原生的，降级只发生在 sqlite dialect 下。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator

import pytest
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

import models  # noqa: F401  导入即注册全部模型，create_all 才有表可建
from models import Base


@pytest.fixture(scope="session")
def sqlite_engine():
    """整个测试会话共用一个内存库；StaticPool 保证连接不换、库不丢。"""
    engine = create_engine(
        "sqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def session(sqlite_engine) -> Iterator[Session]:
    """每个用例一个事务作用域，用例结束回滚，互不污染。"""
    factory = sessionmaker(bind=sqlite_engine, expire_on_commit=False, autoflush=False)
    with factory() as s:
        yield s
        s.rollback()


@pytest.fixture
async def sqlite_async_engine():
    """异步侧的内存库，**按用例新建**。

    刻意不用会话级：service 层会真的 ``commit()``（它拥有事务边界），
    而 ``StaticPool`` + 内存库意味着同一个 engine 就是一个长期存活的库——
    会话级共享会让上一个用例提交的数据出现在下一个用例里，
    于是「跨租户隔离」这类用例会因为残留数据而假绿。

    与同步 fixture 也不共用 engine：asyncpg 与 sqlite3 驱动对连接状态的假设不同，
    混用会让「同一张表在两边看到不同数据」这类假象难以排查。
    """
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
def async_session_factory(sqlite_async_engine) -> async_sessionmaker[AsyncSession]:
    """绑定测试库的 session 工厂。

    存在的理由是**中间件**：``api/idempotency.py`` 不走依赖注入，
    它自己 ``SessionFactory()`` 开会话。要测它就必须能把工厂换掉，
    否则它会去连配置里的真实 PG——本机没有 Docker，测试直接连不上。
    """
    return async_sessionmaker(bind=sqlite_async_engine, expire_on_commit=False, autoflush=False)


@pytest.fixture
async def async_session(async_session_factory) -> AsyncIterator[AsyncSession]:
    """异步用例的事务作用域。

    服务层与 harness 全是 ``async``（CLAUDE.md §6 异步优先），
    所以它们的用例必须走这个 fixture，而不是同步的 ``session``。
    """
    async with async_session_factory() as s:
        yield s
        await s.rollback()
