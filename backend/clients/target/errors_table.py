"""目标端错误表 / 影子表的**只读**实现（be-17 §3.2 的 ``ErrorTableReader`` 端口）。

## 为什么是「按 run 造」而不是进程级单例

端口只拿到**表名**（``count(table, run_id)`` 系列），而表名定位不了目标端：
``ods_order_err`` 在哪台主机、哪个库，由这次运行的 ``pipeline → target_data_source_id``
决定，**每次运行都可能不同**；更糟的是影子表名会在多个运行之间**复用**
（``orders_wide__shadow`` 这个名字今天给 run 1、明天给 run 2），所以「按表名反查数据源」
会安静地读到另一个库、甚至另一个租户的表——而报告上只是「数字变了」。

目标端因此只能由**这一行 run** 解析：工厂 ``build_error_table_reader(session, run)``
收下 ``(session, run)``，把解析推迟到**第一次查询**。构造本身不发任何查询，
这样 ``finalize_report`` 的前半段（取契约、算 sha）不会因为「目标端此刻连不上」
而多出一处失败点——真正的失败点在读取那一刻，也只有那一刻的结论才需要被怀疑。

## 列名来源（诚实说明）

下面的 ``SELECT`` 与 ``WHERE run_id`` 依据三处，**不是**对着真库量出来的：

- ``engines/quality/contract.py:65-74`` 的 ``ERROR_COLUMNS``——目标端错误表的列清单；
- ``engines/quality/contract.py:750-755`` 的 ``render_route_selects``——**真正把行写进
  错误表的那条 SELECT**（``etl_run_id AS run_id, _rule_id AS rule_id, …``），
  它是这些列名最硬的出处；
- ``vibecoding_doc/模块概要设计/be-17-质量契约与数据分流.md:130-137`` 的 DDL 片段
  （``run_id / rule_id / field_name / error_code / payload_digest / reason / occurred_at``）。

**未对着真库核对**：仓库里没有目标端错误表的物理建表语句——``deploy/`` 下只有源库
（MySQL demo schema 与 PG init），目标端的表由数据面 sink 落库，没有随仓库交付任何
``CREATE TABLE``。所以「列名与列宽」只能以上述三处为准。列名若与之不同，症状是
``_pick`` 抛 ``KeyError`` → 译成 ``INTERNAL_ERROR``（见下），**不会**静默变成 0。

## 读不到 ≠ 读到 0（本模块最危险的失败形态）

「目标端不可达」与「错误表里 0 行」是两个完全不同的事实：前者意味着这份报告不该被相信，
后者意味着这次运行干净。本模块**绝不**把异常降级成 0 / 空集合——真那样做的话，
一次连不上的运行会产出「拒绝率 0、``passed=true``」的报告，然后被 be-18 拿去 Swap
（正式表被换成一份没人核过的数据）。同理，结果集里读不到预期的列也**抛错**：
少一列是我们自己写错了列名，只有 ``COUNT(*)`` 的 0 才是合法值。

## 其它约定

- **只读**：只发 ``SELECT``，不建表、不改数据、不 DROP；无副作用，因此**不**经
  ``ToolBroker``（``engines/quality/ports.py`` 的端口说明）。
- **不搬运业务数据**：只 SELECT 摘要列，**没有「值」这个字段**，也绝不 ``SELECT *``。
- **值一律走绑定参数**（``%s``）；表名/列名是标识符，走 ``wire.qualified`` / ``quote_ident``。
- **一条查询开一条连接、用完即关**：``finalize_report`` 一次运行只跑一遍，
  几条连接的成本可忽略，而缓存连接会引入「谁负责关」的泄漏问题（与 ``wire`` 同一取舍）。
- 日志只记**表名与错误类型**：不记行值、不记连接串、不记口令（CLAUDE.md §5.2）。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, Final

import aiomysql
from sqlalchemy.ext.asyncio import AsyncSession

from clients.target.base import TargetError, TargetScopeError, classify_target_error
from clients.target.registry import RunTargetFacts, load_run_target
from clients.target.wire import _pick, connect, qualified, quote_ident
from core.errors import ErrorCode
from core.logging import get_logger
from engines.quality import ERROR_COLUMNS, ErrorSample, ErrorTableReader
from models.execution import ExecutionRun

log = get_logger(__name__)

#: 错误表里那一列运行标识。由数据面注入为常量列，因此**同一张错误表可被多次运行共用**，
#: 控制面一律以 ``WHERE run_id = ?`` 计数（``engines/quality/ports.py`` 的取舍）。
_RUN_ID: Final[str] = "run_id"

#: 计数列的别名。与 ``wire`` 的 ``COUNT(*) AS row_count`` 同一命名，便于交叉阅读。
_COUNT_ALIAS: Final[str] = "row_count"

#: 摘要样本要读的列（错误表列清单去掉 ``run_id``）。
#:
#: **从 ``ERROR_COLUMNS`` 派生而不是抄一份**：抄一份的话，将来错误表加列/改名时
#: 两边会各走各的，而症状是「摘要永远为空」——报告照出，只是没有诊断材料。
_SAMPLE_COLUMNS: Final[str] = ", ".join(quote_ident(name) for name in ERROR_COLUMNS if name != _RUN_ID)

#: 摘要行的字段名（与 ``ErrorSample`` 一一对应）。取列时按它取，喂给 pydantic 也按它喂。
_SAMPLE_FIELDS: Final[tuple[str, ...]] = tuple(name for name in ERROR_COLUMNS if name != _RUN_ID)


# ---------------------------------------------------------------- 结果集取值


def _picked(row: Mapping[str, Any], *names: str, table: str) -> Any:
    """``wire._pick`` + 把「结果集里没有这一列」译成 ``AppError``。

    ``_pick`` 取不到会抛 ``KeyError``（大小写不敏感是必要的：MySQL 8 回大写
    ``TABLE_NAME``，Doris 回小写，CLAUDE.md §9.1）。这里把那个 ``KeyError``
    在**本模块的边界**上译成 ``INTERNAL_ERROR``——它是我们写错了列名，
    把它当「连接失败」会把排查方向从代码带到网络与凭据上（``base.classify_target_error``
    的说明）。列名进日志：那是我们自己写的常量，不是行值。
    """
    try:
        return _pick(row, *names)
    except KeyError as exc:
        log.warning("errors_table_column_missing", table=table, error=str(exc))
        raise classify_target_error(exc) from exc


def _as_count(value: Any, *, table: str) -> int:
    """``COUNT(*)`` 的返回值 → ``int``。

    ``None`` **抛错**而不是回 0：``COUNT(*)`` 在 SQL 里不可能为 NULL，取到 ``None``
    只可能是「列名对不上 / 结果集形状不对」。而 0 是一个完全合法的报告值
    （「这次运行干净」），两者混起来会让一份读不到数的报告静默变干净。
    """
    if value is None:
        raise TargetError(
            f"错误表 {table!r} 的计数列为空（结果集形状不符）",
            code=ErrorCode.INTERNAL_ERROR,
            http_status=500,
            details={"reason": "count_is_null"},
        )
    return int(value)


def _as_text(value: Any) -> str:
    """摘要里的文本列 → ``str``。``NULL`` → ``""``（``ErrorSample`` 的默认值）。

    这里回空串是安全的：这些列在真表里都是 ``NOT NULL``，回 ``NULL`` 说明行本身
    不正常，而「摘要为空」会被 ``services/quality`` 的 ``payload_digest`` 长度解析
    与报告露馅——与「少一列必须抛错」不同，一个字段缺失不会让整份统计变成假的。
    """
    return "" if value is None else str(value)


def _as_time(value: Any, *, table: str) -> datetime | None:
    """``occurred_at`` → ``datetime``；``NULL`` → ``None``（服务侧会用报告时间兜底）。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    # 目标端在某些路径下可能按文本回时间（Doris 的 DATETIME 走协议时一般是 datetime，
    # 但这里不能赌）。解析不了就抛：把时间悄悄变成 None 等于让「什么时候发生的」消失。
    try:
        return datetime.fromisoformat(str(value))
    except ValueError as exc:
        raise TargetError(
            f"错误表 {table!r} 的 occurred_at 不是可解析的时间",
            code=ErrorCode.INTERNAL_ERROR,
            http_status=500,
            details={"reason": "bad_occurred_at"},
        ) from exc


def _sample_of(row: Mapping[str, Any], *, table: str) -> ErrorSample:
    """一行结果集 → :class:`ErrorSample`。**只有摘要，没有「值」**（端口层面就没有那个字段）。"""
    values = {name: _picked(row, name, table=table) for name in _SAMPLE_FIELDS}
    return ErrorSample(
        rule_id=_as_text(values["rule_id"]),
        field_name=_as_text(values["field_name"]),
        error_code=_as_text(values["error_code"]),
        payload_digest=_as_text(values["payload_digest"]),
        reason=_as_text(values["reason"]),
        occurred_at=_as_time(values["occurred_at"], table=table),
    )


# ---------------------------------------------------------------- 实现


class _TargetErrorTableReader:
    """``ErrorTableReader`` 的目标端实现。**构造不发查询**，目标端在第一次查询时解析。

    ``(session, run)`` 都由 :func:`build_error_table_reader` 传进来，``run`` 是
    ``services/quality`` 已经过租户过滤的那一行（``_load_run``），因此这里不做
    第二次租户校验——端口拿到的表名本来就来自那一行。
    """

    def __init__(self, session: AsyncSession, run: ExecutionRun) -> None:
        self._session = session
        self._run = run
        #: 解析结果只缓存**一次**：一次 finalize 里六七个查询共用同一份目标端事实，
        #: 每次都重查会给出「同一轮统计里目标端换过」这种无法解释的可能性。
        self._facts: RunTargetFacts | None = None

    # ------------------------------------------------------------ 惰性解析

    async def _target(self) -> RunTargetFacts:
        """解析目标端（第一次查询时），之后复用。

        异常**原样放行**：``load_run_target`` 抛的 ``NotFoundError`` / ``TargetError``
        都是已经归好类的 ``AppError``，再包一层 classify 会把「运行不存在」译成
        「目标端操作失败」——一个把排查方向带偏的降级。
        """
        if self._facts is None:
            self._facts = await load_run_target(
                self._session, tenant_id=int(self._run.tenant_id), run_id=int(self._run.id)
            )
        return self._facts

    def _qualified(self, database: str, table: str) -> str:
        """``table`` → ``\\`db\\`.\\`tbl\\```（跨库前缀在这里被拦下，见 ``wire.qualified``）。"""
        return qualified(database, table)

    # ------------------------------------------------------------ 查询

    async def _query(self, sql: str, params: Sequence[Any] | None, *, table: str) -> list[Mapping[str, Any]]:
        """发一条只读查询。**每次开一条新连接、用完即关**（见模块 docstring）。

        驱动异常一律译成 ``TargetError`` 家族再抛：aiomysql 的原始异常文本里常裹着
        主机名与连接串（CLAUDE.md §6）。**绝不**在这里返回 0 / 空集合——
        那会让「读不到」与「读到 0」变成同一件事。
        """
        facts = await self._target()
        try:
            async with connect(facts.binding.endpoint) as conn:
                async with conn.cursor(aiomysql.DictCursor) as cur:
                    # 无参数时必须传 ``None`` 而不是空元组：驱动对 args 非 None 的语句做
                    # ``query % args``，而空元组会让语句里任何字面 ``%``（regex 模板里会有）
                    # 变成格式符，报「not enough arguments for format string」。
                    await cur.execute(sql, tuple(params) if params else None)
                    return list(await cur.fetchall())
        except Exception as exc:
            translated = classify_target_error(exc, failure=TargetError)
            log.warning(
                "errors_table_read_failed",
                table=table,
                error=type(exc).__name__,
                error_code=str(translated.code),
            )
            raise translated from exc

    # ------------------------------------------------------------ 端口：六个方法

    async def count(self, table: str, run_id: int | None = None) -> int:
        """``COUNT(*)``。``run_id`` 为 ``None`` 时整表计（影子表没有 ``run_id`` 列）。"""
        target = self._qualified((await self._target()).binding.database, table)
        where = ""
        params: tuple[Any, ...] | None = None
        if run_id is not None:
            where = f" WHERE {quote_ident(_RUN_ID)} = %s"
            params = (int(run_id),)
        rows = await self._query(
            f"SELECT COUNT(*) AS {_COUNT_ALIAS} FROM {target}{where}",  # noqa: S608 - 标识符已包裹并校验
            params,
            table=target,
        )
        return _as_count(
            _picked(_first(rows, table=target), _COUNT_ALIAS, "count", table=target), table=target
        )

    async def count_by_code(self, table: str, run_id: int) -> Mapping[str, int]:
        """按 ``error_code`` 分组计数 → ``error_code_dist``。"""
        return await self._count_grouped(table, run_id, column="error_code")

    async def count_by_rule(self, table: str, run_id: int) -> Mapping[str, int]:
        """按 ``rule_id`` 分组计数 → ``rule_hits``。"""
        return await self._count_grouped(table, run_id, column="rule_id")

    async def _count_grouped(self, table: str, run_id: int, *, column: str) -> Mapping[str, int]:
        """``GROUP BY <column>`` 的公共实现（两个方法只差一个列名）。"""
        target = self._qualified((await self._target()).binding.database, table)
        key = quote_ident(column)
        rows = await self._query(
            f"SELECT {key} AS {key}, COUNT(*) AS {_COUNT_ALIAS} "  # noqa: S608 - 同上
            f"FROM {target} WHERE {quote_ident(_RUN_ID)} = %s GROUP BY {key}",
            (int(run_id),),
            table=target,
        )
        out: dict[str, int] = {}
        for row in rows:
            name = _picked(row, column, table=target)
            out[_as_text(name)] = _as_count(_picked(row, _COUNT_ALIAS, "count", table=target), table=target)
        return out

    async def count_matching(self, table: str, where_sql: str) -> int:
        """在 ``table``（影子表）上按 ``where_sql`` 计数——``warn`` 规则唯一的命中来源。

        ``where_sql`` 由 ``engines.quality.render_predicate`` 产出，**只含列名与规则模板、
        不含任何行值**（列名在编译期过了标识符白名单），因此这里**不二次转义**——
        再包一层 ``quote_ident`` 会把 ``amount IS NULL`` 变成非法标识符。

        整体加括号：``render_predicate`` 的 ``not_null``（allow_blank）分支顶层是一个
        ``OR``，不加括号会被后续拼接的谓词改掉优先级。
        """
        target = self._qualified((await self._target()).binding.database, table)
        predicate = where_sql.strip()
        if not predicate:
            # 空谓词会让 ``WHERE ()`` 变成语法错，而更坏的可能写法（去掉 WHERE）是**全表计数**：
            # 那会把「没人传谓词」记成「整张影子表都命中 warn」。宁可在这里硬失败。
            raise TargetScopeError(
                f"空谓词不会被解释成「全部命中」：table={table!r}",
                details={"reason": "empty_predicate", "table": table},
            )
        rows = await self._query(
            f"SELECT COUNT(*) AS {_COUNT_ALIAS} FROM {target} WHERE ({predicate})",  # noqa: S608
            None,
            table=target,
        )
        return _as_count(
            _picked(_first(rows, table=target), _COUNT_ALIAS, "count", table=target), table=target
        )

    async def fetch_digest_samples(
        self, table: str, run_id: int, rule_id: str, limit: int = 3
    ) -> Sequence[ErrorSample]:
        """某条规则的若干条**摘要样本**（最多 ``limit`` 条），用于 ``diagnostics``。"""
        target = self._qualified((await self._target()).binding.database, table)
        rows = await self._query(
            f"SELECT {_SAMPLE_COLUMNS} FROM {target} "  # noqa: S608 - 标识符已包裹并校验
            f"WHERE {quote_ident(_RUN_ID)} = %s AND {quote_ident('rule_id')} = %s LIMIT %s",
            (int(run_id), str(rule_id), _checked_limit(limit, table=target)),
            table=target,
        )
        return [_sample_of(row, table=target) for row in rows]

    async def fetch_records(self, table: str, run_id: int, limit: int) -> Sequence[ErrorSample]:
        """若干条摘要，用于**拷贝进控制面** ``error_record``（``limit`` 必需，见端口约定）。"""
        target = self._qualified((await self._target()).binding.database, table)
        rows = await self._query(
            f"SELECT {_SAMPLE_COLUMNS} FROM {target} "  # noqa: S608 - 同上
            f"WHERE {quote_ident(_RUN_ID)} = %s LIMIT %s",
            (int(run_id), _checked_limit(limit, table=target)),
            table=target,
        )
        return [_sample_of(row, table=target) for row in rows]


def _first(rows: Sequence[Mapping[str, Any]], *, table: str) -> Mapping[str, Any]:
    """``COUNT(*)`` 的结果集**必然有且只有一行**；没有行说明目标端回了个空形状。"""
    if not rows:
        raise TargetError(
            f"错误表 {table!r} 的计数查询没有返回任何行",
            code=ErrorCode.INTERNAL_ERROR,
            http_status=500,
            details={"reason": "count_no_rows"},
        )
    return rows[0]


def _checked_limit(limit: int, *, table: str) -> int:
    """``limit`` 必须是正整数。

    ``LIMIT 0`` 会静默回空集合——那正是「读不到」伪装成「没有违规行」的形状，
    比抛错危险得多。负数在 MySQL 上是语法错（会走到 ``TargetError``），但没必要靠它。
    """
    value = int(limit)
    if value < 1:
        raise TargetScopeError(
            f"limit 必须为正整数（收到 {value}）：LIMIT 0 会伪装成「没有违规行」",
            details={"reason": "bad_limit", "table": table, "limit": value},
        )
    return value


def build_error_table_reader(session: AsyncSession, run: ExecutionRun) -> ErrorTableReader:
    """``(session, run) -> ErrorTableReader``。工厂签名必须与 ``services.quality`` 的调用一致。

    注册点在**两处启动钩子**（API 的 lifespan 与 worker 的 ``celeryd_init``）：

    ```python
    from clients.target.errors_table import build_error_table_reader
    services.quality.register_error_table_reader(build_error_table_reader)
    ```

    少注册一次的症状是 ``ConflictError("目标端错误表读取器未注册")``——**刻意的**：
    留在运行期暴露（而不是静默「全部为 0」）正是这个端口不让异常降级的原因。
    """
    return _TargetErrorTableReader(session, run)


__all__ = ["build_error_table_reader"]
