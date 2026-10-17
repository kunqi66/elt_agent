"""``clients/target/errors_table.py`` —— 目标端错误表只读实现（be-17 §3.2）。

**这里没有真库**（CLAUDE.md §9：本机没有 Docker、没有 Doris、没有演示 MySQL），
所以断言的是**能被证明的那部分**：

1. 六个方法各自产出的 **SQL 形状**——特别是「影子表整表计不带 ``WHERE run_id``」
   与「错误表带 ``WHERE run_id = %s`` 且 ``run_id`` 在**参数**里不在 SQL 里」。
   这两件事的错法是同一个症状：把 ``run_id`` 写死进 SQL 会让两次运行的计数互相污染，
   而报告上只是「数字大了一点」。
2. 结果集取值走 ``_pick``：喂**大写键**（MySQL 8 的形态）能取到；「少了一列」抛错，
   **不**回 0 / 空串（``COUNT(*)`` 的 0 与「读不到」是两回事，见模块 docstring）。
3. 摘要样本只 SELECT 摘要列，**没有** ``raw`` / ``value`` 这类列名；``limit`` 走参数。
4. 工厂**惰性解析**：``build_error_table_reader`` 本身不发查询，第一次查询才解析，
   且整个生命周期内只解析一次。
5. 目标端异常 → ``TargetError`` 家族，**不**降级成 0。

``wire.connect`` 由假连接替换（``monkeypatch.setattr`` 到本模块 import 进来的那个名字上），
所以本文件既不需要网络，也不需要驱动。
"""

from __future__ import annotations

import contextlib
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import SecretStr

from clients.target import errors_table
from clients.target.base import TargetBinding, TargetEndpointRef, TargetError, TargetScopeError
from core.errors import ErrorCode

_DB = "dw_demo"
_SHADOW = "orders_wide__shadow_v1"
_ERROR = "ods_order_err"


def _run(*, tenant_id: int = 7, run_id: int = 142) -> Any:
    """只用到 ``.tenant_id`` / ``.id`` 两个属性，因此不必造一整行 ORM 对象。"""
    return SimpleNamespace(tenant_id=tenant_id, id=run_id)


def _facts(*, database: str = _DB) -> Any:
    endpoint = TargetEndpointRef(
        kind="mysql",
        data_source_id=11,
        host="127.0.0.1",
        port=3306,
        database=database,
        username="etl",
        password=SecretStr("不写明文——只在这里占位"),
    )
    binding = TargetBinding(data_source_id=11, kind="mysql", database=database, endpoint=endpoint)
    return SimpleNamespace(
        binding=binding, shadow_table=_SHADOW, error_table=_ERROR, run_id=142, tenant_id=7
    )


class _FakeCursor:
    """只记录 SQL / 参数、回一批固定结果——够验证拼出来的语句与取值。"""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows
        self.sql: str = ""
        self.args: Any = None

    async def __aenter__(self) -> _FakeCursor:
        return self

    async def __aexit__(self, *_: Any) -> None:
        return None

    async def execute(self, sql: str, args: Any = None) -> None:
        self.sql = sql
        self.args = args

    async def fetchall(self) -> list[dict[str, Any]]:
        return list(self._rows)


class _FakeConn:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.cursor_obj = _FakeCursor(rows)

    def cursor(self, *_args: Any, **_kwargs: Any) -> _FakeCursor:
        return self.cursor_obj


class _Harness:
    """把「假连接 / 假解析」装进模块，并记下每条查询的 SQL 与参数。"""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, rows: list[dict[str, Any]]) -> None:
        self.conn = _FakeConn(rows)
        self.connect_calls = 0
        self.resolve_calls: list[tuple[int, int]] = []
        self.connect_error: Exception | None = None
        #: 目标端解析结果（用例可以换成另一个库来验「按 run 解析」）
        self.facts = _facts()
        monkeypatch.setattr(errors_table, "connect", self._connect)
        monkeypatch.setattr(errors_table, "load_run_target", self._resolve)

    @contextlib.asynccontextmanager
    async def _connect(self, _endpoint: Any):  # type: ignore[no-untyped-def]
        self.connect_calls += 1
        if self.connect_error is not None:
            raise self.connect_error
        yield self.conn

    async def _resolve(self, _session: Any, *, tenant_id: int, run_id: int) -> Any:
        self.resolve_calls.append((tenant_id, run_id))
        return self.facts

    @property
    def sql(self) -> str:
        return self.conn.cursor_obj.sql

    @property
    def args(self) -> Any:
        return self.conn.cursor_obj.args


def _with_rows(monkeypatch: pytest.MonkeyPatch, rows: list[dict[str, Any]]) -> _Harness:
    """按用例定制结果集（大写键 / NULL / 空集），并把假连接与假解析装进模块。"""
    return _Harness(monkeypatch, rows)


def _reader() -> Any:
    return errors_table.build_error_table_reader(object(), _run())


# ---------------------------------------------------------------- 惰性解析


async def test_工厂构造不发查询第一次查询才解析目标端(monkeypatch: pytest.MonkeyPatch) -> None:
    """``build_error_table_reader`` 只是把 (session, run) 收下——构造期不该碰目标端。

    ``finalize_report`` 的前半段（取契约、算 sha）不该因为「此刻目标端连不上」
    多出一处失败点：真正的失败点是读取那一刻。
    """
    h = _with_rows(monkeypatch, [{"row_count": 3}])
    reader = _reader()
    assert h.resolve_calls == []  # 构造不发查询
    assert h.connect_calls == 0

    assert await reader.count(_SHADOW) == 3
    assert h.resolve_calls == [(7, 142)]  # 用 run 的 tenant_id / id 解析
    assert h.connect_calls == 1


async def test_目标端只解析一次后续查询复用(monkeypatch: pytest.MonkeyPatch) -> None:
    """一次 finalize 里六七个查询共用同一份目标端事实。

    每次重查会给出「同一轮统计里目标端换过」这种无法解释的可能性——
    而它一旦发生，报告里的分子分母可能来自两个不同的库。
    """
    # 一个结果集喂三个方法：都能取到值即可（这里只关心解析与连接次数）
    h = _with_rows(monkeypatch, [{"row_count": 1, "error_code": "E_X"}])
    reader = _reader()
    await reader.count(_SHADOW)
    await reader.count(_ERROR, 142)
    await reader.count_by_code(_ERROR, 142)
    assert h.resolve_calls == [(7, 142)]
    assert h.connect_calls == 3  # 每条查询一条新连接（用完即关，见模块 docstring）


async def test_表名带库前缀时与目标数据源一致才放行(monkeypatch: pytest.MonkeyPatch) -> None:
    """``db.table`` 形态由 ``wire.qualified`` 校验：跨库前缀一次也不许到达 SQL。"""
    h = _with_rows(monkeypatch, [{"row_count": 5}])
    reader = _reader()
    assert await reader.count(f"{_DB}.{_SHADOW}") == 5
    assert f"`{_DB}`.`{_SHADOW}`" in h.sql

    with pytest.raises(TargetScopeError):
        await reader.count("other_db.orders_err")


# ---------------------------------------------------------------- SQL 形状


async def test_影子表整表计数不带_run_id_过滤(monkeypatch: pytest.MonkeyPatch) -> None:
    """影子表没有 ``run_id`` 列（有效行不分运行）→ ``run_id=None`` 必须整表计。"""
    h = _with_rows(monkeypatch, [{"ROW_COUNT": Decimal("8420")}])
    assert await _reader().count(_SHADOW) == 8420  # Decimal（MySQL 的 COUNT 形态）→ int
    assert h.sql == f"SELECT COUNT(*) AS row_count FROM `{_DB}`.`{_SHADOW}`"
    assert "WHERE" not in h.sql
    assert h.args is None


async def test_错误表计数带_run_id_且值走绑定参数(monkeypatch: pytest.MonkeyPatch) -> None:
    """``run_id`` 必须在**参数**里：拼进 SQL 会让两次运行的计数互相污染。"""
    h = _with_rows(monkeypatch, [{"row_count": 1580}])
    assert await _reader().count(_ERROR, 142) == 1580
    assert h.sql == f"SELECT COUNT(*) AS row_count FROM `{_DB}`.`{_ERROR}` WHERE `run_id` = %s"
    assert h.args == (142,)
    assert "142" not in h.sql


async def test_按错误码分组计数(monkeypatch: pytest.MonkeyPatch) -> None:
    h = _with_rows(
        monkeypatch,
        [
            {"ERROR_CODE": "E_TYPE_CAST", "ROW_COUNT": Decimal("1203")},
            {"ERROR_CODE": "E_NULL_PK", "ROW_COUNT": 377},
        ],
    )
    dist = await _reader().count_by_code(_ERROR, 142)
    assert dist == {"E_TYPE_CAST": 1203, "E_NULL_PK": 377}
    assert "GROUP BY `error_code`" in h.sql
    assert "WHERE `run_id` = %s" in h.sql
    assert h.args == (142,)


async def test_按规则分组计数(monkeypatch: pytest.MonkeyPatch) -> None:
    h = _with_rows(monkeypatch, [{"rule_id": "orders.amount.range", "row_count": 812}])
    assert await _reader().count_by_rule(_ERROR, 142) == {"orders.amount.range": 812}
    assert "GROUP BY `rule_id`" in h.sql
    assert h.args == (142,)


async def test_影子表上按谓词计数不带任何参数(monkeypatch: pytest.MonkeyPatch) -> None:
    """``where_sql`` 由 ``render_predicate`` 产出（只含列名与模板），实现方不二次转义。

    整体加括号是必须的：``not_null`` 的 ``allow_blank`` 分支顶层是一个 ``OR``。
    """
    h = _with_rows(monkeypatch, [{"row_count": 6}])
    assert await _reader().count_matching(_SHADOW, "remark IS NULL OR TRIM(remark) = ''") == 6
    assert h.sql == (
        f"SELECT COUNT(*) AS row_count FROM `{_DB}`.`{_SHADOW}` "
        "WHERE (remark IS NULL OR TRIM(remark) = '')"
    )
    # 谓词里可能有字面 ``%``（regex 模板）：不传参数才不会撞上驱动的 ``query % args``
    assert h.args is None


async def test_空谓词不会被当成整表命中(monkeypatch: pytest.MonkeyPatch) -> None:
    """空谓词若退化成「不带 WHERE」，整张影子表都会被记成 warn 命中。"""
    h = _with_rows(monkeypatch, [{"row_count": 6}])
    with pytest.raises(TargetScopeError):
        await _reader().count_matching(_SHADOW, "   ")
    assert h.connect_calls == 0


async def test_摘要样本只取摘要列且limit走参数(monkeypatch: pytest.MonkeyPatch) -> None:
    """样本行**不含任何原值**：端口层面就没有「值」这个字段，SQL 里也不许出现那种列。"""
    h = _with_rows(
        monkeypatch,
        [
            {
                "rule_id": "orders.amount.range",
                "field_name": "amount",
                "error_code": "E_RANGE",
                "payload_digest": "a" * 64,
                "reason": "amount NOT BETWEEN 0 AND 100000",
                "occurred_at": None,
            }
        ],
    )
    got = await _reader().fetch_digest_samples(_ERROR, 142, "orders.amount.range", 3)
    assert len(got) == 1
    assert got[0].payload_digest == "a" * 64
    assert got[0].field_name == "amount"
    assert "AND `rule_id` = %s" in h.sql
    assert h.args == (142, "orders.amount.range", 3)  # limit 进参数，不进 SQL
    assert "LIMIT %s" in h.sql
    assert "*" not in h.sql
    lowered = h.sql.lower()
    for banned in ("raw", "value", "`src_", "origin"):
        assert banned not in lowered


async def test_控制面副本查询只取摘要列(monkeypatch: pytest.MonkeyPatch) -> None:
    h = _with_rows(
        monkeypatch,
        [
            {
                "RULE_ID": "phone.regex",
                "FIELD_NAME": "phone",
                "ERROR_CODE": "E_REGEX_MISMATCH",
                "PAYLOAD_DIGEST": "b" * 64,
                "REASON": "NOT REGEXP(phone, '^1[3-9]\\d{9}$')",
                "OCCURRED_AT": None,
            }
        ],
    )
    got = await _reader().fetch_records(_ERROR, 142, 1000)
    assert [item.rule_id for item in got] == ["phone.regex"]
    assert h.sql == (
        f"SELECT `rule_id`, `field_name`, `error_code`, `payload_digest`, `reason`, `occurred_at` "
        f"FROM `{_DB}`.`{_ERROR}` WHERE `run_id` = %s LIMIT %s"
    )
    assert h.args == (142, 1000)


@pytest.mark.parametrize("limit", [0, -1])
async def test_limit_非正数直接拒绝(monkeypatch: pytest.MonkeyPatch, limit: int) -> None:
    """``LIMIT 0`` 会静默回空集合——那是「读不到」伪装成「没有违规行」的形状。"""
    h = _with_rows(monkeypatch, [])
    with pytest.raises(TargetScopeError):
        await _reader().fetch_records(_ERROR, 142, limit)
    assert h.connect_calls == 0


async def test_occurred_at_文本形态也能解析(monkeypatch: pytest.MonkeyPatch) -> None:
    """目标端若按文本回时间，要解析出来而不是丢掉——丢了就说不清「什么时候发生的」。"""
    _with_rows(
        monkeypatch,
        [
            {
                "rule_id": "r",
                "field_name": "f",
                "error_code": "E",
                "payload_digest": "c" * 64,
                "reason": "x",
                "occurred_at": "2026-10-16T09:20:11",
            }
        ],
    )
    got = await _reader().fetch_records(_ERROR, 142, 1)
    assert got[0].occurred_at is not None
    assert got[0].occurred_at.year == 2026


# ---------------------------------------------------------------- 取列：少一列必须抛错


async def test_结果集缺列时抛错而不是回零(monkeypatch: pytest.MonkeyPatch) -> None:
    """「少了一列」是我们自己写错了列名（CLAUDE.md §9.1），不是「这次运行很干净」。"""
    h = _with_rows(monkeypatch, [{"SOMETHING_ELSE": 1}])
    with pytest.raises(TargetError) as caught:
        await _reader().count(_ERROR, 142)
    assert caught.value.code is ErrorCode.INTERNAL_ERROR
    assert h.sql  # 查询确实发出去了，只是结果集形状不对


async def test_计数列为_null_时抛错而不是回零(monkeypatch: pytest.MonkeyPatch) -> None:
    """``COUNT(*)`` 不可能为 NULL：取到 NULL 说明结果集形状不符，回 0 等于谎报「干净」。"""
    h = _with_rows(monkeypatch, [{"row_count": None}])
    with pytest.raises(TargetError) as caught:
        await _reader().count(_SHADOW)
    assert caught.value.code is ErrorCode.INTERNAL_ERROR
    assert h.connect_calls == 1


async def test_计数查询没有返回行时抛错(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_rows(monkeypatch, [])
    with pytest.raises(TargetError) as caught:
        await _reader().count(_SHADOW)
    assert caught.value.code is ErrorCode.INTERNAL_ERROR


async def test_摘要里的_null_字段退化为空串而不抛(monkeypatch: pytest.MonkeyPatch) -> None:
    """与「缺列」不同：一个字段为 NULL 不会让整份统计变成假的，空串是忠实表达。"""
    h = _with_rows(
        monkeypatch,
        [
            {
                "rule_id": None,
                "field_name": "amount",
                "error_code": None,
                "payload_digest": None,
                "reason": None,
                "occurred_at": None,
            }
        ],
    )
    got = await _reader().fetch_records(_ERROR, 142, 1)
    assert got[0].payload_digest == ""
    assert got[0].error_code == ""  # ErrorSample 里没有「值」字段，退化的只是摘要
    assert h.args == (142, 1)


# ---------------------------------------------------------------- 异常：读不到 ≠ 读到 0


async def test_目标端不可达抛_TargetError_而不是回零(monkeypatch: pytest.MonkeyPatch) -> None:
    """本模块最危险的失败形态是「连不上 → 返回 0」：那会产出 passed=true 的报告。"""
    h = _with_rows(monkeypatch, [])
    h.connect_error = OSError("Connection refused")
    with pytest.raises(TargetError) as caught:
        await _reader().count(_ERROR, 142)
    assert caught.value.code is ErrorCode.DATASOURCE_UNREACHABLE


async def test_查询本身报错也抛_TargetError_而不是回零(monkeypatch: pytest.MonkeyPatch) -> None:
    """表不存在 / 语法错都发生在 ``execute`` 阶段，同样不许降级成 0。"""

    class _BoomCursor:
        async def __aenter__(self) -> Any:
            return self

        async def __aexit__(self, *_: Any) -> None:
            return None

        async def execute(self, *_: Any, **__: Any) -> None:
            raise RuntimeError("Table 'dw_demo.ods_order_err' doesn't exist")

        async def fetchall(self) -> list[dict[str, Any]]:
            return []

    h = _with_rows(monkeypatch, [])
    h.conn.cursor_obj = _BoomCursor()  # type: ignore[assignment]
    with pytest.raises(TargetError):
        await _reader().count(_ERROR, 142)


async def test_解析失败时原样抛_AppError_不二次包装(monkeypatch: pytest.MonkeyPatch) -> None:
    """``load_run_target`` 抛的已经是归好类的 ``AppError``：再包一层会把「不存在」译成
    「目标端操作失败」——一个把排查方向带偏的降级。"""
    h = _with_rows(monkeypatch, [])

    async def _boom(_session: Any, *, tenant_id: int, run_id: int) -> Any:
        raise TargetError("运行不存在")

    monkeypatch.setattr(errors_table, "load_run_target", _boom)
    with pytest.raises(TargetError) as caught:
        await _reader().count(_ERROR, 142)
    assert caught.value.message == "运行不存在"
    assert h.connect_calls == 0
