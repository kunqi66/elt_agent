"""``clients/target`` 的纯函数与 SQL 生成（be-18）。

**这里没有真库**（CLAUDE.md §9：本机没有 Docker、没有 Doris、没有演示 MySQL），
所以本文件断言的是**能被证明的那部分**：

1. 对账表（``reconcile_final_state``）的四条分支 + 一条加严——这是「发布到底成没成功」
   的唯一判据，判错了整条链路的价值为零；
2. 预检（结构逐列一致 / 空影子表 / 表不存在）——它是**正式表不被碰**的第一道闸；
3. 生成的 SQL 文本：``REPLACE WITH TABLE ... swap=true``（Doris）与三步
   ``RENAME TABLE``（MySQL）。**语法与语义未经真引擎验证**（报告里如实说明），
   但「语句长什么样」「标识符有没有被包裹」「跨库名有没有被拦下」是可以在这里钉死的；
4. 标识符安全（反引号注入 / 跨库前缀）——范围校验的第三与第四条。

对账表最容易被写错的一格是「两张表发布前指纹相同」：那时交换**前后不可区分**，
判 published 会让一次根本没生效的 Swap 被记成成功。本文件用一条独立用例把它钉住。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from pydantic import SecretStr

from clients.target import wire
from clients.target.base import (
    FINGERPRINT_SCOPE,
    FINGERPRINT_SCOPE_ROWCOUNT,
    ColumnInfo,
    TableInfo,
    TargetEndpointRef,
    TargetError,
    TargetPrecheckError,
    TargetScopeError,
    assert_columns_replacable,
    assert_publishable,
    assert_unqualified_or_same_database,
    classify_target_error,
    column_diffs,
    reconcile_final_state,
    split_qualified,
)
from clients.target.doris import DorisTargetPublisher
from clients.target.mysql import MySQLTargetPublisher
from clients.target.wire import qualified, quote_ident, rename_statement
from core.errors import ErrorCode

_ENDPOINT = TargetEndpointRef(
    kind="doris",
    data_source_id=1,
    host="127.0.0.1",
    port=9030,
    database="dw",
    username="etl",
    password=SecretStr("不写明文——只在这里占位"),
)


def _cols(*spec: tuple[str, str, bool]) -> tuple[ColumnInfo, ...]:
    return tuple(
        ColumnInfo(name=name, ordinal=index, type_text=type_text, nullable=nullable)
        for index, (name, type_text, nullable) in enumerate(spec)
    )


_SHAPE = (("id", "bigint", False), ("amount", "decimal(12,2)", True))


def _table(
    name: str, *, fingerprint: str = "1:1", rows: int = 1, shape: Any = _SHAPE, exists: bool = True
) -> TableInfo:
    return TableInfo(
        name=name,
        exists=exists,
        columns=_cols(*shape),
        row_count=rows,
        table_fingerprint=fingerprint,
        primary_key_columns=("id",) if exists else (),
    )


# ---------------------------------------------------------------- 对账（§6.2）


def test_reconcile_published_when_two_tables_swapped() -> None:
    """四个观测都取到、且影子与正式确实换了个个儿 → ``published``。"""
    assert (
        reconcile_final_state(pre_shadow="7:100", pre_formal="3:900", post_formal="7:100", post_shadow="3:900")
        == "published"
    )


def test_reconcile_not_published_when_formal_unchanged() -> None:
    """正式表指纹没变 → ``not_published``（DDL 失败最常见的样子）。"""
    assert (
        reconcile_final_state(pre_shadow="7:100", pre_formal="3:900", post_formal="3:900", post_shadow="7:100")
        == "not_published"
    )


def test_reconcile_unknown_when_fingerprint_unreadable() -> None:
    """读不到指纹 → ``unknown``。**不许当成大约成功**：在未知态上叠加第二次 Swap 才是事故。"""
    assert (
        reconcile_final_state(pre_shadow="7:100", pre_formal="3:900", post_formal=None, post_shadow="3:900")
        == "unknown"
    )


def test_reconcile_unknown_when_two_tables_look_alike_before_swap() -> None:
    """加严的一条：发布前两张表指纹相同 → 交换前后**不可区分** → ``unknown``。

    这一格最容易被写成 published（``post_formal == pre_shadow`` 确实成立），
    而那会把「本次数据与正式表里原有的数据恰好同形」记成一次成功发布。
    """
    assert (
        reconcile_final_state(pre_shadow="5:42", pre_formal="5:42", post_formal="5:42", post_shadow="5:42")
        == "unknown"
    )


def test_reconcile_unknown_when_only_half_swapped() -> None:
    """只换了一半（post_formal 既不是旧的也不是影子的）→ ``unknown``，不猜。"""
    assert (
        reconcile_final_state(pre_shadow="7:100", pre_formal="3:900", post_formal="9:1", post_shadow="3:900")
        == "unknown"
    )


# ---------------------------------------------------------------- 预检（§6.1 条件 5）


def test_precheck_rejects_missing_formal_table() -> None:
    with pytest.raises(TargetPrecheckError) as err:
        assert_publishable(_table("s1"), _table("f1", exists=False), allow_empty_shadow=True)
    assert err.value.details["reason"] == "formal_missing"


def test_precheck_rejects_missing_shadow_table() -> None:
    with pytest.raises(TargetPrecheckError) as err:
        assert_publishable(_table("s1", exists=False), _table("f1"), allow_empty_shadow=True)
    assert err.value.details["reason"] == "shadow_missing"


def test_precheck_rejects_column_type_or_nullability_diff() -> None:
    """逐列一致（含类型与 nullable）：差异静默通过等于业务侧表结构被悄悄改掉。"""
    formal = _table("f1", shape=(("id", "bigint", False), ("amount", "decimal(12,2)", True)))
    shadow = _table("s1", shape=(("id", "bigint", False), ("amount", "decimal(12,2)", False)))
    with pytest.raises(TargetPrecheckError) as err:
        assert_publishable(shadow, formal, allow_empty_shadow=True)
    assert err.value.details["reason"] == "column_mismatch"
    assert any("nullable" in diff for diff in err.value.details["diffs"])


def test_precheck_rejects_column_order_change() -> None:
    """列序变化会让 ``SELECT *`` 的调用方读错列，因此也判不一致。"""
    formal = _table("f1")
    shadow = _table("s1", shape=(("amount", "decimal(12,2)", True), ("id", "bigint", False)))
    with pytest.raises(TargetPrecheckError):
        assert_columns_replacable(shadow, formal)
    assert any("列序" in diff for diff in column_diffs(shadow, formal))


def test_precheck_reports_extra_shadow_column() -> None:
    shadow = _table("s1", shape=(*_SHAPE, ("extra", "text", True)))
    diffs = column_diffs(shadow, _table("f1"))
    assert diffs and "extra" in diffs[0]


def test_precheck_rejects_empty_shadow_unless_source_was_empty() -> None:
    """空影子表是最危险的一种「成功」：它会把正式表发布成空表，而质量报告照样 passed。"""
    with pytest.raises(TargetPrecheckError) as err:
        assert_publishable(_table("s1", rows=0, fingerprint="0:0"), _table("f1"), allow_empty_shadow=False)
    assert err.value.details["reason"] == "empty_shadow"

    # read_rows == 0（源端本来就是空的）时才放行
    assert_publishable(_table("s1", rows=0, fingerprint="0:0"), _table("f1"), allow_empty_shadow=True)


def test_fingerprint_scope_exposed_when_no_primary_key() -> None:
    """无主键时指纹退化为行数——这个降级必须能被子调用方看见，否则「行数一致」= 「内容一致」。"""
    without_pk = TableInfo(name="t", exists=True, columns=_cols(*_SHAPE), row_count=3,
                           table_fingerprint="3:0", primary_key_columns=())
    assert without_pk.fingerprint_scope == FINGERPRINT_SCOPE_ROWCOUNT
    assert _table("t").fingerprint_scope == FINGERPRINT_SCOPE


# ---------------------------------------------------------------- 标识符与范围（§6.4）


def test_split_qualified_rejects_three_segments() -> None:
    assert split_qualified("dw.orders") == ("dw", "orders")
    assert split_qualified("orders") == (None, "orders")
    with pytest.raises(TargetScopeError):
        split_qualified("a.b.c")


def test_qualified_rejects_backtick_injection() -> None:
    """反引号能闭合标识符——放行等于把「表名只在服务端产生」交给运气。"""
    with pytest.raises(TargetScopeError) as err:
        quote_ident("orders`; DROP TABLE x; --")
    assert err.value.details["reason"] == "illegal_identifier"


def test_qualified_rejects_cross_database_prefix() -> None:
    """影子表在 A 库、正式表在 B 库：连拼进 SQL 的机会都不给。"""
    with pytest.raises(TargetScopeError) as err:
        qualified("dw", "other.orders_shadow_v1")
    assert err.value.details["reason"] == "cross_database"
    assert_unqualified_or_same_database("dw.orders", database="dw")
    with pytest.raises(TargetScopeError):
        assert_unqualified_or_same_database("other.orders", database="dw")


# ---------------------------------------------------------------- 生成的 SQL


def test_doris_swap_sql_is_replace_with_table_on_shadow() -> None:
    """Doris：**在影子表上**执行 ``REPLACE WITH TABLE 正式表``（不是反过来）。

    写反的后果是「把正式表的数据替换成历史数据」——语句照样成功。
    """
    sql = DorisTargetPublisher(_ENDPOINT).swap_sql(
        database="dw", shadow="orders_shadow_v1", formal="orders_wide"
    )
    assert sql == (
        "ALTER TABLE `dw`.`orders_shadow_v1` REPLACE WITH TABLE `dw`.`orders_wide` "
        'PROPERTIES("swap" = "true")'
    )


def test_mysql_swap_sql_is_single_three_step_rename() -> None:
    """MySQL：**一条**语句里三步 RENAME（单语句即单事务，中间态不对外可见）。"""
    sql = MySQLTargetPublisher(TargetEndpointRef(**{**_ENDPOINT.model_dump(), "kind": "mysql"})).swap_sql(
        database="dw", shadow="orders_shadow_v1", formal="orders_wide"
    )
    assert sql.count("RENAME TABLE") == 1
    assert sql.count(" TO ") == 3
    assert sql == (
        "RENAME TABLE `dw`.`orders_wide` TO `dw`.`orders_wide__swap_tmp`, "
        "`dw`.`orders_shadow_v1` TO `dw`.`orders_wide`, "
        "`dw`.`orders_wide__swap_tmp` TO `dw`.`orders_shadow_v1`"
    )
    assert "DROP" not in sql.upper()


def test_rename_statement_truncates_temp_name_within_identifier_limit() -> None:
    """正式表名可以长到 64 字符，拼上后缀不能越界（越界是语法错，不是数据错）。"""
    long_name = "t" * 64
    sql = rename_statement(database="dw", formal=long_name, shadow="t_shadow_v1", temp_suffix="__swap_tmp")
    first_step = sql.removeprefix("RENAME TABLE ").split(", ")[0]
    temp = first_step.split(" TO ")[1].split(".")[-1].strip("`")
    assert temp == "t" * 40 + "__swap_tmp"
    assert len(temp) <= 64


class _FakeCursor:
    """只记录 SQL、回一行固定结果——够验证拼出来的语句，不需要驱动。"""

    def __init__(self, row: dict[str, Any]) -> None:
        self._row = row
        self.sql: str = ""

    async def __aenter__(self) -> _FakeCursor:
        return self

    async def __aexit__(self, *_: Any) -> None:
        return None

    async def execute(self, sql: str, *params: Any) -> None:
        self.sql = sql

    async def fetchone(self) -> dict[str, Any]:
        return self._row


class _FakeConn:
    def __init__(self, row: dict[str, Any]) -> None:
        self.cursor_obj = _FakeCursor(row)

    def cursor(self, *_args: Any, **_kwargs: Any) -> _FakeCursor:
        return self.cursor_obj


async def test_fingerprint_sql_uses_crc32_over_primary_key() -> None:
    """指纹口径的 SQL 文本：``COUNT(*)`` + ``SUM(CRC32(主键))``，且标识符已包裹。"""
    # DictCursor 由 aiomysql 提供，这里只借它的大小写形态：MySQL 8 回的是大写列名
    conn = _FakeConn({"ROW_COUNT": Decimal("120"), "CHECKSUM": Decimal("4096")})
    rows, checksum = await wire._fingerprint(
        conn,  # type: ignore[arg-type]
        database="dw",
        table="orders_wide",
        columns=_cols(*_SHAPE),
        primary_key=("id",),
    )
    assert (rows, checksum) == (120, 4096)  # Decimal → int：SUM(BIGINT) 在 MySQL 回 Decimal
    assert "COUNT(*) AS row_count" in conn.cursor_obj.sql
    assert "COALESCE(SUM(CRC32(CAST(`id` AS CHAR))), 0) AS checksum" in conn.cursor_obj.sql
    assert "FROM `dw`.`orders_wide`" in conn.cursor_obj.sql


async def test_fingerprint_sql_without_primary_key_degrades_to_rowcount() -> None:
    """无主键 → 指纹只剩行数（``checksum`` 恒 0），这个降级在 SQL 里就看得出来。"""
    conn = _FakeConn({"row_count": 7, "checksum": 0})
    rows, checksum = await wire._fingerprint(
        conn,  # type: ignore[arg-type]
        database="dw",
        table="orders_wide",
        columns=_cols(*_SHAPE),
        primary_key=(),
    )
    assert (rows, checksum) == (7, 0)
    assert "CRC32" not in conn.cursor_obj.sql


# ---------------------------------------------------------------- 驱动异常归类


def test_keyerror_is_our_bug_not_a_connectivity_problem() -> None:
    """``KeyError`` 归 INTERNAL_ERROR：把解析写错的键名译成「连接失败」会把排查带向网络。"""
    translated = classify_target_error(KeyError("TABLE_NAME"))
    assert translated.code is ErrorCode.INTERNAL_ERROR


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (TimeoutError("timed out"), ErrorCode.DATASOURCE_TIMEOUT),
        (RuntimeError("Access denied for user 'etl'@'10.0.0.1'"), ErrorCode.DATASOURCE_AUTH_FAILED),
        (OSError("Connection refused"), ErrorCode.DATASOURCE_UNREACHABLE),
        (RuntimeError("syntax error near 'REPLACE'"), ErrorCode.SWAP_FAILED),
    ],
)
def test_driver_errors_translate_to_the_three_documented_codes(exc: Exception, expected: ErrorCode) -> None:
    assert classify_target_error(exc).code is expected


def test_translated_message_carries_no_connection_string() -> None:
    """驱动异常文本常裹着主机名与连接串（CLAUDE.md §5.2），译文只留类型名。"""
    translated = classify_target_error(RuntimeError("cannot connect to 10.0.0.9:3306"))
    assert "10.0.0.9" not in translated.message
    assert isinstance(translated, TargetError)
