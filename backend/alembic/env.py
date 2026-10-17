"""Alembic 迁移环境（异步引擎 + asyncpg）。

**为什么不在 alembic.ini 里配 URL**：那份文件在版本库里，写了 URL 就等于把口令
复制一份进去；而且 `.env` 改了 ini 不会跟着变，于是「迁移连开发库、应用连生产库」
这类事故迟早发生。这里统一从 ``core.config.settings`` 现取，和其它所有入口同源。

**为什么用异步而不是 ``postgres_dsn_sync``**：`psycopg` 并不在依赖里
（见 ``pyproject.toml``），为一个只在本地跑几次的迁移命令引入第二个 PG 驱动，
换来的是「两套驱动、两套参数风格」的长期维护面。asyncpg 已经在跑生产流量了。

**为什么离线模式不给 URL**：Python 的 ``ConfigParser`` 会把 ``%`` 当插值符，
口令里带 ``%`` 就会在 `set_main_option` 处炸掉；而用 URL 构造 engine 再取
``engine.url`` 又可能把口令渲染进 `--sql` 的输出（会被重定向到文件或 CI 日志）。
离线生成只做 DSL→SQL 编译，给 dialect 名就够。
"""

from __future__ import annotations

import asyncio

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

import models  # noqa: F401  导入即注册全部模型，autogenerate 才有表可比
from alembic import context
from core.config import settings
from models.base import Base

target_metadata = Base.metadata


def _configure_kwargs() -> dict[str, object]:
    """在线/离线共用的 configure 参数。

    ``compare_type`` 与 ``compare_server_default`` 默认是关的，不打开的话
    「改了列类型却 autogenerate 出空迁移」——迁移文件是空的，检查却通过，
    部署后库里是旧类型。这是 Alembic 最典型的一个静默失败。
    """
    return {
        "target_metadata": target_metadata,
        "compare_type": True,
        "compare_server_default": True,
        # 只迁移 __tablename__ 里的东西：排除 alembic_version 自身与
        # 将来可能引入的外部 schema（如 PostGIS 的 spatial_ref_sys）
        "include_schemas": False,
        # 版本号渲染成带引号的字符串，便于人工阅读 diff
        "version_table": "alembic_version",
    }


def run_migrations_offline() -> None:
    context.configure(
        dialect_name="postgresql",
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        **_configure_kwargs(),
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, **_configure_kwargs())
    with context.begin_transaction():
        context.run_migrations()


async def _run_async_migrations() -> None:
    # NullPool：迁移是一次性短命进程，连接池在这个场景下只增加「退出时还有
    # 未关闭连接」的风险，没有任何收益
    engine = create_async_engine(settings.postgres_dsn, poolclass=pool.NullPool)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(_do_run_migrations)
    finally:
        await engine.dispose()


def run_migrations_online() -> None:
    asyncio.run(_run_async_migrations())


# 刻意**不调** core.logging.configure_logging()：它把日志挂到 stdout，
# 而 `alembic upgrade --sql` 的 SQL 也是写 stdout 的——`> init.sql` 之后
# 文件头会混进三行日志，那份文件既不能直接 `psql -f`，也过不了 PG 语法解析。
# 迁移进程的日志用 alembic.ini 里那套（已指到 stderr）。
if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
