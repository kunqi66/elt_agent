"""LangGraph Checkpointer 的生命周期与 DSN 归一（be-09 §6.5 / §10.1）。

三件事，每件都有一个「不做就静默出错」的坑：

1. **DSN 必须是 psycopg 原生形态**。``settings.postgres_dsn`` 是 ``postgresql+asyncpg://``，
   而 ``langgraph-checkpoint-postgres`` 走 psycopg——它不认 ``+asyncpg``，
   会把它当成一个**未知的 scheme** 去解析，报出来的错是解析层的，
   看起来像「连接串写错了」，实际是「拿 SQLAlchemy 的串喂了 psycopg」。
   归一逻辑是纯函数 :func:`to_psycopg_dsn`，因此能在没有 PG 的机器上单测。

2. **``.setup()`` 只需且必须调用一次**（幂等 ``CREATE TABLE IF NOT EXISTS``）。
   调用它需要先建立连接，而建立连接需要 libpq——**本机（Windows 开发机）没有**。
   所以所有 ``import psycopg`` / ``langgraph.checkpoint.postgres`` 都放在
   函数体里惰性执行：否则连 ``import workflow`` 都会炸，而那只让我们在本地
   什么也验证不了。

3. **连接池是进程级的，不能每次请求新建**。``AsyncPostgresSaver.from_conn_string``
   是个 async context manager，它内部建一条连接（不是池）。每次 ``ainvoke`` 都
   ``async with`` 一次，等于每次请求新建/销毁一条 PG 连接——在 ECS 上是可见的连接数抖动，
   在隧道场景下还会撞上「重启隧道后的第一个请求 503」（CLAUDE.md §9）。
   所以 :class:`CheckpointerHolder` 在应用启动时 ``open()`` 一次，
   在关停时 ``close()``，进程内共享同一个 saver。

用法（``main.py`` 的 lifespan）：

    holder = await open_checkpointer()      # 幂等，重复调用返回同一个 holder
    ...
    await close_checkpointer()
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.engine import URL, make_url

from core.logging import get_logger

__all__ = [
    "CheckpointerHolder",
    "CheckpointerNotReadyError",
    "close_checkpointer",
    "get_checkpointer",
    "open_checkpointer",
    "to_psycopg_dsn",
]

log = get_logger(__name__)

#: psycopg 认的 scheme。``postgres`` 是它的历史别名，一并接受。
_PSYCOPG_SCHEMES = frozenset({"postgresql", "postgres"})

#: SQLAlchemy 的 driver 后缀 → 直接丢掉（psycopg 的 DSN 里没有 driver 段）。
_STRIPPED_DRIVERS = frozenset({"asyncpg", "psycopg", "psycopg2", "psycopg2cffi"})


class CheckpointerNotReadyError(RuntimeError):
    """在 ``open_checkpointer()`` 之前取了 saver。

    刻意抛 ``RuntimeError`` 而不是回落到 ``InMemorySaver``：回落会让
    「容器重启后恢复失败」变成「图从头再跑一遍」——用户看到的是澄清问题重问，
    而运维看到的是一切正常（CLAUDE.md §9 明令禁止用内存假装持久化）。
    """


def to_psycopg_dsn(value: str | URL) -> str:
    """把 SQLAlchemy 的连接串/URL 归一成 psycopg 原生 DSN。

    输入三种形态都要处理（be-09 §10.1 的验证项）：
    - ``postgresql+asyncpg://...`` → ``postgresql://...``
    - ``postgresql+psycopg://...`` → ``postgresql://...``
    - ``postgresql://...``（已是原生）→ 原样返回
    - ``URL`` 对象 → 交给 SQLAlchemy 自己渲染（密码里的 ``@`` ``/`` 等已在
      ``make_url`` 解析时归位，自己拼字符串会把它们漏出来）

    非 PostgreSQL 的串**抛错**而不是尽力而为：一个 ``mysql://`` 串被塞进
    langgraph 的建表语句，失败点会跑到建表那里，离真正的原因隔了一层。
    """
    if isinstance(value, URL):
        return _url_to_psycopg(value)
    text = value.strip()
    if "://" not in text:
        raise ValueError("不是合法的数据库 DSN（缺少 scheme，如 postgresql://）")
    scheme, rest = text.split("://", 1)
    base = scheme.split("+", 1)[0].lower()
    if base not in _PSYCOPG_SCHEMES:
        raise ValueError(f"不是 PostgreSQL DSN：{base}://")
    driver = scheme.split("+", 1)[1].lower() if "+" in scheme else ""
    if driver and driver not in _STRIPPED_DRIVERS:
        raise ValueError(f"未知的 PostgreSQL driver：{scheme}")
    return f"postgresql://{rest}"


def _url_to_psycopg(url: URL) -> str:
    """``URL`` → 原生 DSN。"""
    base = url.drivername.split("+", 1)[0].lower()
    if base not in _PSYCOPG_SCHEMES:
        raise ValueError(f"不是 PostgreSQL DSN：{url.drivername}")
    # hide_password=False：dsn 本来就要带密码交给 psycopg（它不在日志里出现，
    # 见 _redacted）；默认的 True 会把密码换成 ``***``，连库直接认证失败。
    return url.set(drivername="postgresql").render_as_string(hide_password=False)


def _redacted(dsn: str) -> str:
    """日志用的脱敏 DSN：只留 scheme 与 host:port/db（CLAUDE.md §5.2 无明文 Secret）。"""
    try:
        url = make_url(dsn)
    except Exception:
        return "<unparsable>"
    host = url.host or ""
    port = f":{url.port}" if url.port else ""
    return f"{url.drivername}://{host}{port}/{url.database or ''}"


class CheckpointerHolder:
    """进程级 saver 持有者。``open()`` / ``close()`` 成对，且都幂等。

    ``_saver`` 与 ``_ctx`` 分开持有：``from_conn_string`` 返回的 async context
    manager 一旦进入就不能提前退出，用 ``__aenter__``/``__aexit__`` 手动管理
    生命周期（而不是 ``async with``），因为它的存活期跨越整个应用而不是一个请求。
    """

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._ctx: Any = None
        self._saver: Any = None

    @property
    def dsn(self) -> str:
        return self._dsn

    @property
    def saver(self) -> Any:
        if self._saver is None:
            raise CheckpointerNotReadyError(
                "Checkpointer 尚未就绪：请在应用启动（lifespan）中先 await open_checkpointer()"
            )
        return self._saver

    @property
    def ready(self) -> bool:
        return self._saver is not None

    async def open(self) -> CheckpointerHolder:
        """建立连接 + ``.setup()``（幂等建表）。重复调用直接返回。"""
        if self._saver is not None:
            return self
        # 惰性导入：本机没有 libpq，模块级导入会让 workflow 整包不可用
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

        ctx = AsyncPostgresSaver.from_conn_string(self._dsn)
        saver = await ctx.__aenter__()
        try:
            await saver.setup()
        except BaseException:
            # 建表失败必须把连接还回去，否则异常路径上留一条悬空连接
            await ctx.__aexit__(None, None, None)
            raise
        self._ctx = ctx
        self._saver = saver
        log.info("checkpointer_ready", dsn=_redacted(self._dsn))
        return self

    async def close(self) -> None:
        ctx, self._ctx = self._ctx, None
        self._saver = None
        if ctx is not None:
            await ctx.__aexit__(None, None, None)
            log.info("checkpointer_closed", dsn=_redacted(self._dsn))


#: 进程内单例。**故意是模块级全局**：检查点线程是跨请求的，
#: 每次请求新建一个 saver 就等于放弃「同一 thread_id 落到同一处」这个前提。
_holder: CheckpointerHolder | None = None


def _default_dsn() -> str:
    # 惰性取 settings：config 的导入会读 .env 与应用生产自检，
    # 让 import workflow 触发它会把「本机跑个纯函数单测」也变成一次配置校验。
    from core.config import settings

    return to_psycopg_dsn(settings.postgres_dsn)


async def open_checkpointer(dsn: str | None = None) -> CheckpointerHolder:
    """打开（或复用）进程级 Checkpointer，并完成 ``.setup()``。"""
    global _holder
    if _holder is not None and _holder.ready:
        return _holder
    _holder = CheckpointerHolder(dsn or _default_dsn())
    return await _holder.open()


def get_checkpointer() -> CheckpointerHolder:
    """取当前 holder（尚未 ``open()`` 时抛 :class:`CheckpointerNotReadyError`）。"""
    if _holder is None:
        raise CheckpointerNotReadyError(
            "Checkpointer 尚未初始化：请在应用启动（lifespan）中先 await open_checkpointer()"
        )
    return _holder


async def close_checkpointer() -> None:
    """关停时释放。幂等：重复调用是空操作。"""
    global _holder
    if _holder is None:
        return
    await _holder.close()
    _holder = None
