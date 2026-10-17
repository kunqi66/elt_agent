"""MySQL 线协议上的公共动作：连接、表结构、指纹、DDL（be-18 §6.3 / §6.4）。

Doris FE 与 MySQL 8 走的是**同一条线协议**（``clients/datasource_probe.py`` 的同一前提），
所以「连上去 / 看表 / 算指纹 / 执行一条 DDL」这四件事只实现一次：
``doris.py`` 与 ``mysql.py`` 的差别收敛为**交换语句**与**预检的严格程度**，
对账（§6.2）与指纹（§6.2 的判据）完全共用——两份实现各写一套对账，
迟早会在某一边悄悄放宽，而那是整个模块里最不能出错的一段。

四条纪律：

1. **标识符不能参数化**，只能包裹 + 拒绝包裹符本身（与 ``datasource_probe`` 同一处理）。
   这不是「忘了用参数」；``S608`` 在这里是误报，注释写在各处。
2. **``autocommit=True``**：DDL 在 MySQL 协议下隐式提交，而 Doris 的 ``REPLACE``
   若落在未提交的事务里，连接一断就可能回滚——那会让「执行成功」与「生效」
   之间多出一个我们无法观测的窗口。
3. **不给 DDL 设客户端超时**：客户端主动放弃时，服务端可能已经把交换做完了。
   我们控制不了服务端，但可以不制造这种「未知态」的来源；判定始终交给对账。
4. **连接只在动作期间存在**：每次 ``describe`` / ``publish`` 自带一条连接，
   不做连接池。发布是低频且每个动作都要求「读到的就是此刻的」，池化换不来什么，
   却会引入「事务里拿着一条陈旧的元数据」这种更难查的问题。
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator, Mapping
from decimal import Decimal
from typing import Any

import aiomysql

from clients.target.base import (
    ColumnInfo,
    TableInfo,
    TargetEndpointRef,
    TargetError,
    TargetScopeError,
    classify_target_error,
    split_qualified,
)

#: information_schema 里表结构的读法。``ordinal_position`` 参与比对：
#: 列顺序变化会让 ``SELECT *`` 的调用方读错列（§6.3 要求列顺序一致）。
_COLUMNS_SQL = (
    "SELECT column_name AS column_name, ordinal_position AS ordinal_position, "
    "column_type AS type_text, is_nullable AS is_nullable "
    "FROM information_schema.columns WHERE table_schema = %s AND table_name = %s "
    "ORDER BY ordinal_position"
)

_PK_SQL = (
    "SELECT column_name AS column_name FROM information_schema.key_column_usage "
    "WHERE table_schema = %s AND table_name = %s AND constraint_name = 'PRIMARY' "
    "ORDER BY ordinal_position"
)

_VIEWS_SQL = (
    "SELECT table_name AS table_name, view_definition AS view_definition "
    "FROM information_schema.views WHERE table_schema = %s"
)


def quote_ident(name: str) -> str:
    """包裹标识符。反引号本身直接拒绝——这是唯一一处拼 SQL 的入口，必须在这里堵死。"""
    if not name or "`" in name or "\x00" in name:
        raise TargetScopeError(f"非法标识符：{name!r}", details={"reason": "illegal_identifier"})
    return f"`{name}`"


def qualified(database: str, table: str) -> str:
    """``table`` → ``\\`db\\`.\\`table\\```。

    表名若自带库前缀，必须与目标数据源的库**一致**（§6.4 范围校验之三）：
    「影子表在 A 库、正式表在 B 库」这种输入连拼进 SQL 的机会都不给。
    """
    prefix, bare = split_qualified(table)
    if prefix is not None and prefix != database:
        raise TargetScopeError(
            f"表 {table!r} 的库名与目标数据源不一致（期望 {database!r}）",
            details={"reason": "cross_database", "table": table, "expected_database": database},
        )
    return f"{quote_ident(database)}.{quote_ident(bare)}"


def _pick(row: Mapping[str, Any], *names: str) -> Any:
    """取结果集里的一个值，**取不到就抛**。

    大小写不敏感是必要的：MySQL 8 回的是大写 ``TABLE_NAME``，Doris 回小写；
    而「取不到返回 ``None``」会让下一行的 ``str()`` 把它变成表名 ``"None"``——
    错得安静最危险（CLAUDE.md §9.1）。
    """
    for name in names:
        if name in row:
            return row[name]
    lowered = {str(key).lower(): value for key, value in row.items()}
    for name in names:
        if name.lower() in lowered:
            return lowered[name.lower()]
    raise KeyError(f"结果集里没有这些列：{'/'.join(names)}；实际列：{sorted(map(str, row))}")


def _as_int(value: Any) -> int:
    """``COUNT`` / ``SUM(CRC32)`` 的返回值统一成 int。

    MySQL 对 ``SUM(BIGINT)`` 回 ``Decimal``，Doris 可能回 ``int``；两者直接比字符串
    会在 ``Decimal('42')`` 与 ``"42"`` 之间产生假不等——那是「内容没变却报不一致」，
    排查方向会被带到目标库上去。
    """
    if value is None:
        return 0
    if isinstance(value, Decimal):
        return int(value)
    return int(value)


@contextlib.asynccontextmanager
async def connect(endpoint: TargetEndpointRef) -> AsyncIterator[aiomysql.Connection]:
    """建立一条目标端连接。**口令只在这里被取出一次**（``SecretStr`` 的唯一出口）。"""
    try:
        conn = await aiomysql.connect(
            host=endpoint.host,
            port=int(endpoint.port),
            user=endpoint.username,
            password=endpoint.password.get_secret_value(),
            db=endpoint.database,
            charset=endpoint.charset,
            connect_timeout=endpoint.connect_timeout_sec,
            autocommit=True,
        )
    except Exception as exc:
        raise classify_target_error(exc, failure=TargetError) from exc
    try:
        yield conn
    finally:
        # aiomysql 的 close() 是同步的（probe 里同一处理）；ensure_closed() 才是协程
        conn.close()


# ---------------------------------------------------------------- 读


async def describe(conn: aiomysql.Connection, *, database: str, table: str) -> TableInfo:
    """表结构 + 行数 + 指纹，**一次观测**（见 ``base.TableInfo`` 的说明）。"""
    _prefix, bare = split_qualified(table)
    columns = await _columns(conn, database=database, table=bare)
    if not columns:
        # information_schema 里没有列 = 这张表不存在。不抛：调用方要靠 `exists`
        # 区分「表没了」与「库连不上」，前者是业务结论，后者才是故障。
        return TableInfo(name=table, exists=False)
    primary_key = await _primary_key(conn, database=database, table=bare)
    row_count, checksum = await _fingerprint(
        conn, database=database, table=bare, columns=columns, primary_key=primary_key
    )
    return TableInfo(
        name=table,
        exists=True,
        columns=columns,
        row_count=row_count,
        table_fingerprint=f"{row_count}:{checksum}",
        primary_key_columns=primary_key,
    )


async def _columns(conn: aiomysql.Connection, *, database: str, table: str) -> tuple[ColumnInfo, ...]:
    try:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(_COLUMNS_SQL, (database, table))
            rows = list(await cur.fetchall())
    except Exception as exc:
        raise classify_target_error(exc) from exc
    return tuple(
        ColumnInfo(
            name=str(_pick(row, "column_name")),
            ordinal=_as_int(_pick(row, "ordinal_position")),
            type_text=str(_pick(row, "type_text", "column_type")).lower(),
            nullable=str(_pick(row, "is_nullable")).upper() == "YES",
        )
        for row in rows
    )


async def _primary_key(conn: aiomysql.Connection, *, database: str, table: str) -> tuple[str, ...]:
    try:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(_PK_SQL, (database, table))
            rows = list(await cur.fetchall())
    except Exception as exc:
        raise classify_target_error(exc) from exc
    return tuple(str(_pick(row, "column_name")) for row in rows)


async def _fingerprint(
    conn: aiomysql.Connection,
    *,
    database: str,
    table: str,
    columns: tuple[ColumnInfo, ...],
    primary_key: tuple[str, ...],
) -> tuple[int, int]:
    """``COUNT(*)`` 与 ``SUM(CRC32(主键))``。

    ``CAST(... AS CHAR)`` 而不是设计里写的 ``VARCHAR``：MySQL 8 的 ``CAST`` 不接受
    ``VARCHAR`` 目标类型（报 1064），Doris 接受 ``CHAR``。两库都认 ``CHAR``，
    而 CRC32 只关心字节形态，因此口径不变、跨实现可比。
    多列主键用 ``CONCAT_WS('|')`` 拼成一个整体再取 CRC32——分开求和会让
    ``(1,2)`` 与 ``(2,1)`` 得到同一个值。
    """
    if primary_key:
        available = {column.name for column in columns}
        missing = [name for name in primary_key if name not in available]
        if missing:
            raise TargetError(
                f"主键列 {missing} 不在表 {table!r} 的列清单里（元数据自相矛盾）",
                details={"reason": "pk_not_in_columns"},
            )
        key_expr = (
            f"CAST({quote_ident(primary_key[0])} AS CHAR)"
            if len(primary_key) == 1
            else "CONCAT_WS('|', " + ", ".join(f"CAST({quote_ident(c)} AS CHAR)" for c in primary_key) + ")"
        )
        # 表名是标识符不能参数化（无参数可用），只能包裹 + 拒绝包裹符本身 → S608 在此是误报
        sql = (
            f"SELECT COUNT(*) AS row_count, COALESCE(SUM(CRC32({key_expr})), 0) AS checksum "  # noqa: S608
            f"FROM {qualified(database, table)}"
        )
    else:
        # 无主键：指纹退化为行数。降级**必须显式**（TableInfo.fingerprint_scope 会暴露它），
        # 否则「只有行数的一致」看起来和「内容一致」一模一样。
        sql = (
            f"SELECT COUNT(*) AS row_count, 0 AS checksum "  # noqa: S608 - 同上，标识符已包裹并校验
            f"FROM {qualified(database, table)}"
        )
    try:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(sql)
            row = await cur.fetchone()
    except Exception as exc:
        raise classify_target_error(exc) from exc
    if row is None:
        raise TargetError(f"指纹查询没有返回行：{table!r}")
    return _as_int(_pick(row, "row_count")), _as_int(_pick(row, "checksum"))


async def view_definitions(conn: aiomysql.Connection, database: str) -> dict[str, str]:
    """该库的全部视图定义。供 MySQL 的「视图依赖正式表」预检使用（§6.4）。"""
    try:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(_VIEWS_SQL, (database,))
            rows = list(await cur.fetchall())
    except Exception as exc:
        raise classify_target_error(exc) from exc
    return {str(_pick(row, "table_name")): str(_pick(row, "view_definition") or "") for row in rows}


# ---------------------------------------------------------------- 写


async def execute_ddl(conn: aiomysql.Connection, sql: str) -> int:
    """执行一条 DDL，返回 ``rowcount``。

    **返回值只是记账用的**：调用方不许拿它判定成败（模块 docstring 第 3 条）。
    ``rowcount`` 在 Doris 的 ``REPLACE WITH TABLE`` 上是 0，取它当判据会把
    每一次成功的发布都记成失败。
    """
    try:
        async with conn.cursor() as cur:
            await cur.execute(sql)
            return int(cur.rowcount or 0)
    except Exception as exc:
        raise classify_target_error(exc) from exc


def create_like_sql(*, database: str, target: str, source: str) -> str:
    """``CREATE TABLE IF NOT EXISTS <target> LIKE <source>``。

    ``LIKE`` 不只复制列，还复制索引、键模型与分桶属性——这正是影子表能与正式表
    通过 Swap 的结构预检的原因：**手工写一份 ``CREATE TABLE`` 等于把正式表的
    结构抄了第二遍**，而两份结构的分叉只在 Swap 那一刻以「列形态不一致，拒绝交换」
    的形式暴露出来（前面全绿）。所以影子表一律克隆，错误表才手写 DDL
    （它是平台自有形状，不需要与谁同形）。
    """
    return f"CREATE TABLE IF NOT EXISTS {qualified(database, target)} LIKE {qualified(database, source)}"


def rename_statement(*, database: str, formal: str, shadow: str, temp_suffix: str) -> str:
    """MySQL 的三步交换，**单条语句**（单语句即单事务，中间态不对外可见）。

    ``RENAME TABLE a TO x, b TO a, x TO b`` 按序执行，整条语句要么全部生效要么
    全部不生效；这正是它与「先 DROP 再 RENAME」的区别，也是 §6.4 选择它的原因。

    ``temp`` 名字由正式表名截断加后缀拼成：``RENAME`` 的中间名必须是**不存在**的，
    所以截断位留够（MySQL 标识符上限 64 字符），且预检会先确认它真的不存在——
    上一次崩在半路的交换会留下这个中间名，那时必须拒绝而不是覆盖。
    """
    bare_formal = split_qualified(formal)[1]
    bare_shadow = split_qualified(shadow)[1]
    temp = f"{bare_formal[:40]}{temp_suffix}"
    steps = [
        f"{qualified(database, bare_formal)} TO {qualified(database, temp)}",
        f"{qualified(database, bare_shadow)} TO {qualified(database, bare_formal)}",
        f"{qualified(database, temp)} TO {qualified(database, bare_shadow)}",
    ]
    return "RENAME TABLE " + ", ".join(steps)


__all__ = [
    "connect",
    "create_like_sql",
    "describe",
    "execute_ddl",
    "qualified",
    "quote_ident",
    "rename_statement",
    "view_definitions",
]
