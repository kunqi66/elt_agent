"""``target.ensure_tables`` —— 备表（影子表 + 错误表）的算法与工具契约。

**这是整条链路上第一个会失败在正确位置的地方**：shadow 与 error 两张表若不存在，
SeaTunnel 的 JDBC sink 会以「表不存在」失败（作业层，最难查），而分流的行无处可去
（报告层，错得最安静）。所以本文件守的是「什么时候建、什么时候拒绝、拒绝时做过什么」。

本机没有 Doris / 没有演示 MySQL（CLAUDE.md §9），因此断言分两部分：

1. **算法**：喂假连接与假 ``describe``，验 SQL 形状、状态取值、以及
   **拒绝路径下一条 DDL 都不许发**（拒绝了却已经建了表 = 留下的副作用没人认账）；
2. **工具契约**：``ToolName`` 已注册、执行进程是 ``api``、入参三张表名互不相同。

真的把表建出来、再由 SeaTunnel 写进去，只能在 ECS 上跑（那部分由交付报告记录）。
"""

from __future__ import annotations

import contextlib
import re
from types import SimpleNamespace
from typing import Any

import pytest

from clients.target import engine as engine_mod
from clients.target import executor as target_executor
from clients.target import wire as wire_mod
from clients.target.base import (
    ColumnInfo,
    ProvisionResult,
    RunRef,
    TableInfo,
    TargetEndpointRef,
    TargetPrecheckError,
)
from clients.target.doris import DorisTargetPublisher
from clients.target.mysql import MySQLTargetPublisher
from harness.tools import TargetEnsureTablesArgs, get_tool
from models.enums import RiskLevel, ToolName

_DB = "dw_demo"
_FORMAL = "orders_wide"
_SHADOW = "orders_wide__shadow_v1"
_ERROR = "orders_wide_elt_reject"


# ---------------------------------------------------------------- 假件


def _endpoint(kind: str = "doris") -> TargetEndpointRef:
    return TargetEndpointRef(
        kind=kind,  # type: ignore[arg-type]
        data_source_id=11,
        host="127.0.0.1",
        port=9030,
        database=_DB,
        username="etl",
        password="不写明文——只在这里占位",  # type: ignore[arg-type]
    )


def _columns(names: tuple[str, ...]) -> tuple[ColumnInfo, ...]:
    return tuple(
        ColumnInfo(name=name, ordinal=index + 1, type_text="bigint", nullable=False)
        for index, name in enumerate(names)
    )


def _table(name: str, *, rows: int = 0, cols: tuple[str, ...] = ("order_id", "amount")) -> TableInfo:
    return TableInfo(
        name=name,
        exists=True,
        columns=_columns(cols),
        row_count=rows,
        table_fingerprint=f"{rows}:0",
        primary_key_columns=("order_id",),
    )


def _missing(name: str) -> TableInfo:
    return TableInfo(name=name, exists=False)


class _Harness:
    """把 ``wire.connect`` / ``wire.describe`` / ``wire.execute_ddl`` 换成记录型假件。"""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, tables: dict[str, TableInfo]) -> None:
        self.tables = tables
        self.ddl: list[str] = []
        self.described: list[str] = []
        #: 本进程"建过"的表；建过之后它才"存在"，且默认与正式表同形
        self.created: set[str] = set()
        #: 需要模拟"建出来但与正式表不同形"时按表名覆盖
        self.after_create: dict[str, TableInfo] = {}
        monkeypatch.setattr(wire_mod, "connect", self._connect)
        monkeypatch.setattr(wire_mod, "describe", self._describe)
        monkeypatch.setattr(wire_mod, "execute_ddl", self._execute_ddl)

    @contextlib.asynccontextmanager
    async def _connect(self, _endpoint: Any):  # type: ignore[no-untyped-def]
        yield object()

    async def _describe(self, _conn: Any, *, database: str, table: str) -> TableInfo:
        assert database == _DB
        self.described.append(table)
        if table in self.created:
            return self.after_create.get(table, _table(table))
        return self.tables.get(table, _missing(table))

    async def _execute_ddl(self, _conn: Any, sql: str) -> int:
        self.ddl.append(sql)
        matched = re.search(r"CREATE TABLE IF NOT EXISTS\s+`?(\w+)`?\.`?(\w+)`?", sql)
        assert matched is not None, sql  # 只认两条已知形状的建表语句
        self.created.add(matched.group(2))
        return 0


def _run_ref() -> RunRef:
    return RunRef(
        run_id=142,
        tenant_id=7,
        project_id=3,
        shadow_table=_SHADOW,
        formal_table=_FORMAL,
        error_table=_ERROR,
    )


def _harness(monkeypatch: pytest.MonkeyPatch, tables: dict[str, TableInfo]) -> _Harness:
    return _Harness(monkeypatch, tables)


# ---------------------------------------------------------------- 正常路径


async def test_影子表按正式表克隆错误表按平台形状建(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """两张表不存在时各发一条 ``CREATE``；影子表**克隆**正式表而不是手写结构。"""
    h = _harness(monkeypatch, {_FORMAL: _table(_FORMAL, rows=11)})
    result = await DorisTargetPublisher(_endpoint()).provision(_run_ref())

    assert isinstance(result, ProvisionResult)
    assert result.ok is True
    assert result.shadow_state == "created"
    assert result.error_state == "created"
    assert len(h.ddl) == 2
    like, error = h.ddl
    # 影子表：克隆。手写一份 CREATE 等于把正式表结构抄第二遍，两份迟早分叉，
    # 而分叉只在 Swap 那一刻以「列形态不一致」暴露——前面全绿。
    assert like == f"CREATE TABLE IF NOT EXISTS `{_DB}`.`{_SHADOW}` LIKE `{_DB}`.`{_FORMAL}`"
    # 错误表：平台自有形状，键列必须排在列清单最前面，且必须显式分桶
    assert f"CREATE TABLE IF NOT EXISTS `{_DB}`.`{_ERROR}`" in error
    assert "DUPLICATE KEY(`run_id`, `rule_id`, `field_name`)" in error
    assert "DISTRIBUTED BY HASH(`run_id`) BUCKETS 1" in error
    assert 'PROPERTIES("replication_num" = "1")' in error


async def test_影子表已存在且为空时一条建表语句都不发(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """上次失败/中断留下的空影子表可以直接复用——重跑不必先手工清理。"""
    h = _harness(monkeypatch, {_FORMAL: _table(_FORMAL), _SHADOW: _table(_SHADOW)})
    result = await MySQLTargetPublisher(_endpoint("mysql")).provision(_run_ref())

    assert result.shadow_state == "exists_empty"
    assert result.error_state == "created"
    assert [sql.split("`")[3] for sql in h.ddl] == [_ERROR]  # 只建了错误表


async def test_错误表已存在时不重复建(monkeypatch: pytest.MonkeyPatch) -> None:
    """错误表跨运行共用，第二次运行只建影子表。"""
    h = _harness(monkeypatch, {_FORMAL: _table(_FORMAL), _ERROR: _table(_ERROR)})
    result = await MySQLTargetPublisher(_endpoint("mysql")).provision(_run_ref())

    assert result.error_state == "exists"
    assert len(h.ddl) == 1 and _SHADOW in h.ddl[0]


# ---------------------------------------------------------------- 拒绝路径


async def test_正式表不存在时拒绝且不发任何DDL(monkeypatch: pytest.MonkeyPatch) -> None:
    """正式表是影子表的模板、也是 Swap 的另一半。

    这条拒绝要**在发任何 DDL 之前**给出：建出一张没有可交换对象的影子表，
    等于让一次注定发布不了的运行先把作业跑完。
    """
    h = _harness(monkeypatch, {})
    with pytest.raises(TargetPrecheckError) as exc:
        await DorisTargetPublisher(_endpoint()).provision(_run_ref())

    assert exc.value.details["reason"] == "formal_missing"
    assert h.ddl == []


async def test_影子表已有数据时拒绝且不清空(monkeypatch: pytest.MonkeyPatch) -> None:
    """影子表名按版本带序号，同一版本重跑会落到同一个名字上。

    续写会让质量报告里的「有效行」有一半来自上一次运行——报告、指纹、Swap
    全都基于一份来源不明的数据。所以拒绝，且**不**顺手 ``TRUNCATE``/``DROP``：
    那些行的来路要先由人看清（真正的清理入口是受管回滚）。
    """
    h = _harness(
        monkeypatch,
        {_FORMAL: _table(_FORMAL, rows=11), _SHADOW: _table(_SHADOW, rows=4)},
    )
    with pytest.raises(TargetPrecheckError) as exc:
        await DorisTargetPublisher(_endpoint()).provision(_run_ref())

    assert exc.value.details["reason"] == "shadow_not_empty"
    assert exc.value.details["row_count"] == 4
    assert h.ddl == []


async def test_建出来的影子表与正式表不同形时不报成功(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """建完当场对形——不留到 Swap 那一步才发现。

    结构不对的作业能跑完、能出报告，只在最后一步以「列形态不一致」失败，
    那时目标库里已经躺着一份没人看得懂的数据，而且已经白跑一趟。
    """
    h = _harness(monkeypatch, {_FORMAL: _table(_FORMAL, cols=("order_id", "amount"))})
    h.after_create[_SHADOW] = _table(_SHADOW, cols=("order_id",))  # 少一列
    result = await DorisTargetPublisher(_endpoint()).provision(_run_ref())

    assert result.ok is False
    assert result.shadow_matches_formal is False
    assert len(h.ddl) == 2  # 仍把两张表建了出来（错误表是本次运行的诊断落点，值得留下）


# ---------------------------------------------------------------- 执行器


class _FakePublisher:
    def __init__(self, result: ProvisionResult | None = None, error: Exception | None = None):
        self._result = result
        self._error = error

    async def provision(self, run: RunRef) -> ProvisionResult:
        if self._error is not None:
            raise self._error
        assert self._result is not None
        return self._result


def _facts() -> Any:
    return SimpleNamespace(
        run_id=142,
        tenant_id=7,
        project_id=3,
        shadow_table=_SHADOW,
        formal_table=_FORMAL,
        error_table=_ERROR,
    )


def _install_executor_fakes(
    monkeypatch: pytest.MonkeyPatch, publisher: _FakePublisher
) -> list[tuple[str, str, str]]:
    seen: list[tuple[str, str, str]] = []

    async def _load(session: Any, *, tenant_id: int, run_id: int) -> Any:
        seen.append(("load", str(tenant_id), str(run_id)))
        return _facts()

    monkeypatch.setattr(target_executor.registry, "load_run_target", _load)
    monkeypatch.setattr(target_executor, "_publisher_for", lambda _facts: publisher)
    return seen


async def test_执行器只回白名单里的键(monkeypatch: pytest.MonkeyPatch) -> None:
    """broker 会**静默丢弃**未知键，所以执行器只允许回白名单里真实存在的键。

    多回一个 ``error_table`` 看起来无害，实际是「登记过的键才显示」这条规矩的破口：
    前端拿不到它，而日志与账本里也没有——排查时会以为「这个字段没人填」。
    """
    _install_executor_fakes(
        monkeypatch,
        _FakePublisher(
            ProvisionResult(
                ok=True,
                shadow_state="created",
                error_state="created",
                shadow_matches_formal=True,
                detail={"engine": "doris", "formal_rows": 11, "error_table": _ERROR},
            )
        ),
    )
    args = TargetEnsureTablesArgs(
        run_id=142, shadow_table=_SHADOW, formal_table=_FORMAL, error_table=_ERROR
    )
    ctx = SimpleNamespace(tenant_id=7, run_id=142)
    detail = await target_executor.target_ensure_tables_executor(args, ctx, object())

    from harness.broker import _ALLOWED_DETAIL_KEYS

    assert set(detail) <= _ALLOWED_DETAIL_KEYS
    assert detail["ok"] is True and detail["status"] == "success"
    assert detail["shadow_table"] == _SHADOW and detail["formal_table"] == _FORMAL
    assert "影子表 created" in detail["message"]


async def test_执行器在影子表不同形时抛错而不是回成功(monkeypatch: pytest.MonkeyPatch) -> None:
    """``ok=False`` 会被服务层当成一个需要解释的返回值；这里要的是一条能立刻转 4xx 的错误。"""
    _install_executor_fakes(
        monkeypatch,
        _FakePublisher(
            ProvisionResult(
                ok=False,
                shadow_state="created",
                error_state="created",
                shadow_matches_formal=False,
            )
        ),
    )
    args = TargetEnsureTablesArgs(
        run_id=142, shadow_table=_SHADOW, formal_table=_FORMAL, error_table=_ERROR
    )
    ctx = SimpleNamespace(tenant_id=7, run_id=142)
    with pytest.raises(TargetPrecheckError) as exc:
        await target_executor.target_ensure_tables_executor(args, ctx, object())
    assert exc.value.details["reason"] == "column_mismatch"


# ---------------------------------------------------------------- 工具契约


def test_工具注册形状() -> None:
    """工具在注册表里，且**只允许 api 进程**执行。

    这一条与 ``target.publish_swap``（worker）不同，是因为它在 Commit 事务里跑：
    失败要当场变成一条可读的 4xx/5xx，而不是几秒后的一条运行失败。
    broker 允许 worker 执行 api 工具、不允许反过来，所以它不构成绕过审批的通路。
    """
    spec = get_tool(ToolName.TARGET_ENSURE_TABLES)
    assert spec.executor == "api"
    assert spec.requires_capability is True
    assert spec.requires_quality_pass is False
    assert spec.risk is RiskLevel.P2
    assert spec.args_model is TargetEnsureTablesArgs


def test_三张表名不许重名() -> None:
    """影子表 = 正式表意味着 Swap 是空操作；错误表撞名意味着诊断数据写进了业务表。"""
    with pytest.raises(Exception):
        TargetEnsureTablesArgs(
            run_id=1, shadow_table=_FORMAL, formal_table=_FORMAL, error_table=_ERROR
        )
    with pytest.raises(Exception):
        TargetEnsureTablesArgs(
            run_id=1, shadow_table=_SHADOW, formal_table=_FORMAL, error_table=_FORMAL
        )


def test_必须经broker执行() -> None:
    """服务层不许自己拼 ``CREATE TABLE``：这条通路一旦开出来，之后就能写 ``DROP``。

    ``services/run.py`` 里唯一的出口是 ``_broker().execute(ToolName.TARGET_ENSURE_TABLES, ...)``
    ——本断言用 import 图之外的方式守着它：``services/`` 里不得出现 ``CREATE TABLE`` 字面量。
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1] / "services"
    offenders = [
        f"{path.name}:{no}"
        for path in root.rglob("*.py")
        for no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if "CREATE TABLE" in line.upper()
    ]
    assert offenders == []
