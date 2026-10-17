"""证据账本：哈希格式、链完整性、篡改检测。

这些用例对应 CLAUDE.md §10 的交付自检项「证据账本验链脚本能从零独立验证全链」。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from harness.ledger import (
    ZERO_HASH,
    EvidenceLedger,
    LedgerEvent,
    SensitivePayloadError,
    as_utc,
    compute_event_hash,
    escape,
    occurred_at_utc_iso,
)
from models.enums import EvidenceEventType
from models.evidence import EvidenceEvent

_T0 = datetime(2026, 10, 16, 8, 30, 0, 123456, tzinfo=UTC)


def _event(**over: object) -> LedgerEvent:
    base: dict[str, object] = {
        "tenant_id": 7,
        "event_type": EvidenceEventType.PROJECT_CREATED,
        "project_id": 42,
        "actor_id": 3,
        "subject_type": "project",
        "subject_id": "42",
        "payload": {"name": "订单宽表", "env": "dev"},
        "occurred_at": _T0,
    }
    base.update(over)
    return LedgerEvent(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------- 哈希格式


def test_时间串固定形态() -> None:
    """微秒为 0 时 ``isoformat()`` 会省略小数部分，固定 6 位可以避免两种写法。"""
    assert occurred_at_utc_iso(datetime(2026, 1, 1, tzinfo=UTC)) == "2026-01-01T00:00:00.000000+00:00"


def test_转义分隔符避免歧义拼接() -> None:
    r"""``|`` 不转义时 ``("a|b","c")`` 与 ``("a","b|c")`` 会拼出同一串。"""
    assert escape("a|b") == r"a\|b"
    # 反斜杠必须先转，否则会把刚插入的转义符再转一次
    assert escape("a\\b") == "a\\\\b"


def test_哈希确定性且输入全参与() -> None:
    """同一输入必得同一值；每个输入都真实参与——逐字段改动都要能改变结果。"""
    kw = dict(
        seq=1,
        tenant_id=7,
        project_id=42,
        actor_id=3,
        event_type="project.created",
        subject_type="project",
        subject_id="42",
        artifact_hash="",
        payload={"a": 1},
        prev_hash=ZERO_HASH,
        occurred_at=_T0,
    )
    baseline = compute_event_hash(**kw)
    assert baseline == compute_event_hash(**kw)  # 确定性

    # 逐字段：任一字段变化都必须改变哈希，否则该字段实际上未被保护
    for field, mutated in [
        ("seq", 2),
        ("tenant_id", 8),
        ("project_id", 43),
        ("actor_id", 4),
        ("event_type", "project.updated"),
        ("subject_type", "other"),
        ("subject_id", "43"),
        ("artifact_hash", "a" * 64),
        ("payload", {"a": 2}),
        ("prev_hash", "b" * 64),
        ("occurred_at", datetime(2026, 10, 16, 8, 30, 0, 123457, tzinfo=UTC)),
    ]:
        assert compute_event_hash(**{**kw, field: mutated}) != baseline, f"{field} 未参与哈希"


def test_payload_键序不影响哈希() -> None:
    """键序差异会让同一语义算出不同哈希，故用 canonical_json 排序。"""
    kw = dict(
        seq=1,
        tenant_id=1,
        project_id=None,
        actor_id=None,
        event_type="x.y",
        subject_type="",
        subject_id="",
        artifact_hash="",
        prev_hash=ZERO_HASH,
        occurred_at=_T0,
    )
    assert compute_event_hash(**kw, payload={"b": 1, "a": 2}) == compute_event_hash(
        **kw, payload={"a": 2, "b": 1}
    )


def test_裸时间视为UTC() -> None:
    """SQLite 读回的裸 datetime 若按本地时区解释，验链在 UTC+8 开发机上必假报断链。"""
    naive = datetime(2026, 10, 16, 8, 30, 0, 123456)
    assert as_utc(naive) == _T0
    assert as_utc(_T0) == _T0  # 带时区的是空操作


# ---------------------------------------------------------------- 入账


async def test_入账维护链式结构(async_session: AsyncSession) -> None:
    ledger = EvidenceLedger()
    assert await ledger.chain_head(async_session) == (0, ZERO_HASH)

    first = await ledger.append(async_session, _event())
    second = await ledger.append(async_session, _event(payload={"name": "第二版"}))

    assert (first.seq, second.seq) == (1, 2)
    assert first.prev_hash == ZERO_HASH
    assert second.prev_hash == first.event_hash  # 链尾衔接
    assert await ledger.chain_head(async_session) == (2, second.event_hash)


async def test_入账不提交事务(async_session: AsyncSession) -> None:
    """事务边界归调用方：账本 commit 会让「业务改了但账本已落」无法回滚。"""
    await EvidenceLedger().append(async_session, _event())
    assert async_session.in_transaction()  # 仍在事务中，未 commit


async def test_拒绝含敏感字段的payload(async_session: AsyncSession) -> None:
    """账本永久保留：静默替换会产生「看起来很干净」的记录，而泄露已经发生。"""
    with pytest.raises(SensitivePayloadError, match="敏感字段"):
        await EvidenceLedger().append(async_session, _event(payload={"db_password": "hunter2"}))

    # 键名不敏感但值是密钥，同样要拦
    with pytest.raises(SensitivePayloadError):
        await EvidenceLedger().append(async_session, _event(payload={"note": "postgresql://u:p@h/db"}))

    # 被拒的事件不得留下任何行
    assert (await async_session.execute(select(EvidenceEvent))).scalars().all() == []


async def test_拒绝裸时间(async_session: AsyncSession) -> None:
    with pytest.raises(Exception, match="带时区"):
        await EvidenceLedger().append(async_session, _event(occurred_at=datetime(2026, 10, 16, 8, 30)))


# ---------------------------------------------------------------- 验链


async def test_完整链校验通过(async_session: AsyncSession) -> None:
    ledger = EvidenceLedger()
    for i in range(5):
        await ledger.append(async_session, _event(payload={"i": i}))

    report = await ledger.verify_chain(async_session)
    assert report.ok
    assert report.checked_events == 5
    assert report.first_broken_seq is None


async def test_检出payload篡改(async_session: AsyncSession) -> None:
    """只比对存储值等于让被检对象自证清白——必须重算。"""
    ledger = EvidenceLedger()
    for i in range(3):
        await ledger.append(async_session, _event(payload={"i": i}))

    await async_session.execute(
        update(EvidenceEvent).where(EvidenceEvent.seq == 2).values(payload={"i": 999})
    )
    report = await ledger.verify_chain(async_session)
    assert not report.ok
    assert report.first_broken_seq == 2
    assert "event_hash 重算不符" in (report.broken_reason or "")


async def test_检出租户归属篡改(async_session: AsyncSession) -> None:
    """🔴 回归用例：tenant_id 原本不参与哈希，改挂租户不会断链。

    多租户账本的核心价值是「谁在哪个租户做了什么」，归属必须可验证。
    """
    ledger = EvidenceLedger()
    for i in range(3):
        await ledger.append(async_session, _event(payload={"i": i}))

    await async_session.execute(update(EvidenceEvent).where(EvidenceEvent.seq == 2).values(tenant_id=999))
    report = await ledger.verify_chain(async_session)
    assert not report.ok, "篡改租户归属必须断链"
    assert report.first_broken_seq == 2


async def test_检出项目归属篡改(async_session: AsyncSession) -> None:
    ledger = EvidenceLedger()
    for i in range(3):
        await ledger.append(async_session, _event(payload={"i": i}))

    await async_session.execute(update(EvidenceEvent).where(EvidenceEvent.seq == 2).values(project_id=999))
    assert not (await ledger.verify_chain(async_session)).ok


async def test_检出删除中间事件(async_session: AsyncSession) -> None:
    """seq 用 MAX+1 而非序列，就是为了让空洞可被识别为删除而非回滚残留。"""
    ledger = EvidenceLedger()
    for i in range(4):
        await ledger.append(async_session, _event(payload={"i": i}))

    await async_session.execute(delete(EvidenceEvent).where(EvidenceEvent.seq == 2))
    report = await ledger.verify_chain(async_session)
    assert not report.ok
    assert report.first_broken_seq == 3
    assert "seq 不连续" in (report.broken_reason or "")


async def test_空链校验通过(async_session: AsyncSession) -> None:
    report = await EvidenceLedger().verify_chain(async_session)
    assert report.ok
    assert report.checked_events == 0


async def test_验链时裸时间不会假报断链(async_session: AsyncSession) -> None:
    """入账用带时区时间，读回（SQLite）成裸值——验链必须仍判通过。"""
    ledger = EvidenceLedger()
    await ledger.append(async_session, _event())
    stored = (await async_session.execute(select(EvidenceEvent).where(EvidenceEvent.seq == 1))).scalar_one()
    # 模拟 SQLite 行为：把时区信息抹掉再验
    stored.occurred_at = stored.occurred_at.replace(tzinfo=None)
    await async_session.flush()

    assert (await ledger.verify_chain(async_session)).ok
