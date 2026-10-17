"""独立验链脚本与账本算法的一致性（be-06）。

对应 CLAUDE.md §10 的交付自检项「证据账本验链脚本能从零独立验证全链」。

**这里验的是一件容易被忽略的事**：``scripts/verify_ledger.py`` 按 be-06 §6.1
**重写了一遍**哈希算法（刻意不 import ``harness.ledger``——用被审计的代码验证
被审计的对象，等于让它自证清白）。两份实现必须逐字节一致，
所以本文件用同一批输入同时喂给两边做对拍。

对拍之外还有一个反例用例：**篡改必须被检出**。一个永远返回 ok 的验链器
在所有正例上都绿灯，而它毫无价值。
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from harness.ledger import EvidenceLedger, LedgerEvent, compute_event_hash
from models.enums import EvidenceEventType

# 直接按路径加载，而不是 ``import scripts.verify_ledger``：
# 后者要求 ``scripts/`` 是个包，而那会让它被当作业务代码的一部分装上——
# 它是个一次性运维脚本，不该被打包
_SPEC = importlib.util.spec_from_file_location(
    "verify_ledger_script", Path(__file__).resolve().parents[1] / "scripts" / "verify_ledger.py"
)
assert _SPEC and _SPEC.loader
script = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = script
_SPEC.loader.exec_module(script)


_T0 = datetime(2026, 10, 16, 8, 30, 0, 123456, tzinfo=UTC)


def _row(**over: Any) -> dict[str, Any]:
    """一行「像是从库里读出来的」事件。

    ``event_type`` 给的是枚举：SQLAlchemy 的枚举列读回来就是枚举实例，
    脚本里用的是 ``str(row["event_type"])``。两者对 ``StrEnum`` 必须一致——
    如果不一致，脚本会在**所有**事件上报断链，而这看起来像全链被篡改。
    """
    base: dict[str, Any] = {
        "seq": 1,
        "tenant_id": 7,
        "project_id": 42,
        "actor_id": 3,
        "event_type": EvidenceEventType.PROJECT_CREATED,
        "subject_type": "project",
        "subject_id": "42",
        "artifact_hash": "",
        "payload": {"name": "订单宽表", "env": "dev"},
        "prev_hash": script.ZERO_HASH,
        "occurred_at": _T0,
    }
    base.update(over)
    return base


CASES: list[tuple[str, dict[str, Any]]] = [
    ("基准", _row()),
    ("无可选字段", _row(project_id=None, actor_id=None, subject_type="", subject_id="")),
    ("subject_id 含分隔符", _row(subject_id="a|b", payload={"k": "v|w"})),
    ("subject_id 含反斜杠", _row(subject_id=r"a\b", payload={"k": r"v\w"})),
    ("payload 含反斜杠加竖线", _row(subject_id=r"\|", payload={"k": r"\|"})),
    ("payload 嵌套且键序不同", _row(payload={"b": 1, "a": {"z": [1, 2], "y": None}})),
    ("payload 非 ASCII", _row(payload={"名称": "订单宽表", "emojis": "🙂"})),
    ("payload 大整数", _row(payload={"n": 10**30})),
    ("artifact_hash 非空", _row(artifact_hash="a" * 64)),
    ("整秒时间", _row(occurred_at=datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC))),
    ("非 UTC 时区", _row(occurred_at=datetime(2026, 10, 16, 16, 30, 0, 123456, tzinfo=UTC).astimezone())),
    ("负数 seq 之外的大 seq", _row(seq=999999)),
]


@pytest.mark.parametrize(("label", "row"), CASES, ids=[c[0] for c in CASES])
def test_脚本与账本算出的哈希一致(label: str, row: dict[str, Any]) -> None:
    """独立实现必须逐字节等于账本的实现。

    任何一侧改了规范化方式（分隔符、字段序、时间格式、JSON 键序），
    这里就会红——而**在线验链不会红**，因为写入侧和读取侧都被同一处改动覆盖了。
    本用例是唯一能抓住那种改动的地方。
    """
    from_ledger = compute_event_hash(
        seq=row["seq"],
        tenant_id=row["tenant_id"],
        project_id=row["project_id"],
        actor_id=row["actor_id"],
        event_type=row["event_type"].value,
        subject_type=row["subject_type"],
        subject_id=row["subject_id"],
        artifact_hash=row["artifact_hash"],
        payload=row["payload"],
        prev_hash=row["prev_hash"],
        occurred_at=row["occurred_at"],
    )
    assert script.event_hash(row) == from_ledger, label


def test_字段顺序即契约() -> None:
    """改动字段顺序会改变所有历史哈希——所以它必须是显式常量，不能是「碰巧的列表」。"""
    assert script._FIELD_ORDER == (
        "seq",
        "tenant_id",
        "project_id",
        "actor_id",
        "event_type",
        "subject_type",
        "subject_id",
        "artifact_hash",
        "payload",
        "prev_hash",
        "occurred_at",
    )


def test_黄金哈希() -> None:
    """钉死一个具体值。

    上面那批用例是「两份实现互相对拍」，两者一起改错仍会一起通过；
    这个字面量挡的正是那种情况——**预期值是人写下来的**，
    算法一旦变动就必须有人显式改它，并在评审里回答：历史账本怎么办？
    哈希链只能追加，算法一改，此前所有 ``event_hash`` 都验不过，
    那不是「修 bug」，那是宣布之前的全部证据作废。
    """
    actual = script.event_hash(_row())
    assert len(_GOLDEN) == 64
    assert actual == _GOLDEN, (
        "账本哈希算法与本文件记录的不符。若这是有意的算法变更，"
        "必须同时给出存量账本的迁移方案，而不是改掉这个字面量。"
    )


# 由 scripts/verify_ledger.py 对上面那条基准事件算出的值。
# 它是**独立实现**的输出，不是从账本抄来的——两边都改了才会同时变动，
# 而那时这个字面量会红
_GOLDEN = "d782b745ed94067d77c7f6ed0abce062d0c19039a63dee15212a38151cc8a8b9"


# ---------------------------------------------------------------- 端到端：真链


async def _append_chain(session: AsyncSession, n: int = 3) -> list[LedgerEvent]:
    ledger = EvidenceLedger()
    events = [
        LedgerEvent(
            tenant_id=7,
            event_type=EvidenceEventType.PROJECT_CREATED,
            project_id=42,
            actor_id=3,
            subject_type="project",
            subject_id=str(42 + i),
            payload={"i": i, "name": f"项目{i}"},
            occurred_at=datetime(2026, 10, 16, 8, 30, i, tzinfo=UTC),
        )
        for i in range(n)
    ]
    await ledger.append_many(session, events)
    await session.commit()
    return events


async def _rows(session: AsyncSession) -> list[dict[str, Any]]:
    from sqlalchemy import select

    from models.evidence import EvidenceEvent

    rows = (await session.execute(select(EvidenceEvent).order_by(EvidenceEvent.seq))).scalars().all()
    return [
        {
            "seq": int(r.seq),
            "tenant_id": int(r.tenant_id),
            "project_id": None if r.project_id is None else int(r.project_id),
            "actor_id": None if r.actor_id is None else int(r.actor_id),
            "event_type": r.event_type,
            "subject_type": r.subject_type,
            "subject_id": r.subject_id,
            "artifact_hash": r.artifact_hash,
            "payload": r.payload,
            "prev_hash": str(r.prev_hash),
            "event_hash": str(r.event_hash),
            "occurred_at": r.occurred_at,
        }
        for r in rows
    ]


async def test_脚本能验通真实账本(async_session: AsyncSession) -> None:
    """账本写出来的链，独立脚本必须能验通。

    两边算法一致只是必要条件；真实落库还多两件事：枚举怎么读回来、
    时间从 SQLite 读回来是否带时区。这两件事都会被这个用例覆盖。
    """
    await _append_chain(async_session)
    ok, checked, reason = script.check(await _rows(async_session))
    assert ok, reason
    assert checked == 3


async def test_脚本检出payload篡改(async_session: AsyncSession) -> None:
    """永远返回 ok 的验链器在所有正例上都绿灯，而它毫无价值。"""
    await _append_chain(async_session)
    rows = await _rows(async_session)
    rows[1]["payload"] = {"i": 1, "name": "被改过的项目名"}
    ok, checked, reason = script.check(rows)
    assert not ok
    assert checked == 1
    assert "2" in (reason or "")


async def test_脚本检出断链(async_session: AsyncSession) -> None:
    await _append_chain(async_session)
    rows = await _rows(async_session)
    del rows[1]
    ok, _, reason = script.check(rows)
    assert not ok
    assert "不连续" in (reason or "")


async def test_脚本从中间起验时不检查前序哈希(async_session: AsyncSession) -> None:
    """``--from-seq`` 的语义是「只验这一段」。

    从中间起验时链条本就缺了头，此时拿第一行的 ``prev_hash`` 去对 ZERO_HASH 必然失败。
    所以只报「这一段自洽」，并在输出里带上 ``from_seq``——
    否则运维会以为「全绿 = 整条链没问题」，而实际上前面可能被删了一截。
    """
    await _append_chain(async_session)
    rows = await _rows(async_session)
    ok, checked, reason = script.check(rows[1:], start_seq=2)
    assert ok, reason
    assert checked == 2


def test_空链通过() -> None:
    ok, checked, reason = script.check([])
    assert ok and checked == 0 and reason is None


def test_退出码语义(capsys: pytest.CaptureFixture[str]) -> None:
    """CI 与交付验收按**退出码**判定。

    Windows 控制台是 GBK，中文输出会乱码（CLAUDE.md §9）——
    所以判定条件不能是「肉眼读输出」。
    """
    assert script.main(["--from-seq", "0"]) == 2
    assert "必须 >= 1" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# 脚本自身的「能不能跑」：这两条在 SQLite 单测里全绿，却让脚本在真机上跑不动
# ---------------------------------------------------------------------------


class _FakeConn:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows
        self.codecs: list[tuple[str, str, Any]] = []
        self.sql = ""
        self.params: tuple[Any, ...] = ()
        self.closed = False

    async def set_type_codec(self, name: str, *, encoder: Any, decoder: Any, schema: str) -> None:
        self.codecs.append((name, schema, decoder))

    async def fetch(self, sql: str, *params: Any) -> list[dict[str, Any]]:
        self.sql, self.params = sql, params
        return self._rows

    async def close(self) -> None:
        self.closed = True


class _FakeAsyncpg:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn
        self.dsn = ""

    async def connect(self, dsn: str) -> _FakeConn:
        self.dsn = dsn
        return self._conn


def test_fetch_必须注册_jsonb_解码器(monkeypatch: pytest.MonkeyPatch) -> None:
    """asyncpg 裸用会把 jsonb 当**文本**回传，而 payload 是哈希覆盖的字段之一。

    漏注册 codec 的后果不是报错，而是**误报断链**：payload 变成带引号的字符串，
    规范化后的哈希必然对不上，脚本会一口咬定「该条内容已被改动」。
    这事真发生过一次（脚本从 psycopg 移植到 asyncpg 时），当时的输出是
    ``BROKEN checked=0 at_seq=1``，看上去像账本被篡改，实际是读取层的缺陷。
    """
    conn = _FakeConn([{"seq": 1, "payload": {"a": 1}}])
    fake = _FakeAsyncpg(conn)
    monkeypatch.setitem(sys.modules, "asyncpg", fake)

    rows = script.fetch("postgresql://u@h/db", 1, None)

    assert rows == [{"seq": 1, "payload": {"a": 1}}]
    registered = {name for name, _, _ in conn.codecs}
    assert registered == {"json", "jsonb"}, f"缺 jsonb 解码器，payload 会以文本回传：{registered}"
    assert all(schema == "pg_catalog" for _, schema, _ in conn.codecs)
    # 解码器要真的能解，而不是登记了一个占位
    decoder = next(d for name, _, d in conn.codecs if name == "jsonb")
    assert decoder('{"a": 1}') == {"a": 1}
    assert "ORDER BY seq ASC" in conn.sql and "$1" in conn.sql, "占位符风格必须与 asyncpg 一致"
    assert conn.closed, "连接没关：长跑脚本会漏连接"


def test_默认连接串取自_settings_而不是猜属性名() -> None:
    """``settings.DATABASE_URL`` **不存在**（真名是 ``postgres_dsn``）。

    写错的症状是 ``AttributeError`` 被 ``main()`` 译成一句「读取账本失败：AttributeError」，
    也就是「脚本根本跑不起来」伪装成「连不上库」——交付验收时最容易被放过的一类失败。
    这里要求默认路径取到的必须是能给 asyncpg 直接用的 DSN。
    """
    dsn = script.resolve_dsn(None)
    assert dsn.startswith("postgresql://"), f"asyncpg 不认 SQLAlchemy 的方言前缀：{dsn[:20]}"
    assert "+asyncpg" not in dsn and "+psycopg" not in dsn


def test_脚本自身缺陷不伪装成连接失败(capsys: pytest.CaptureFixture[str]) -> None:
    """AttributeError / ImportError 只可能来自脚本自己，消息要带上，别只说「读取失败」。"""

    def boom(_dsn: str, _start: int, _limit: int | None) -> list[dict[str, Any]]:
        raise AttributeError("'Settings' object has no attribute 'DATABASE_URL'")

    original = script.fetch
    script.fetch = boom  # type: ignore[assignment]
    try:
        code = script.main([])
    finally:
        script.fetch = original  # type: ignore[assignment]

    err = capsys.readouterr().err
    assert code == 2
    assert "脚本自身出错" in err and "DATABASE_URL" in err, err
