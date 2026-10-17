"""be-14 Outbox 内核与投递器（``harness/outbox.py`` + ``tasks/outbox.py``）。

本文件守着两件**不会报错**的事：

1. **入队只写库**（``enqueue`` 不 commit、不投递、不 ``apply_async``）。破了它，
   一次失败的业务事务（唯一约束冲突 / 断言失败 / 连接断开）会留下一条指向
   不存在 ``execution_run`` 的幽灵命令——Worker 收得到、处理不了，退避到 dead；
   而「把已发出的消息撤回」在 Redis broker 上不可靠。断言方式是
   「回滚之后这行不存在」，不是「没报错」。
2. **投递失败必然留下可行动的痕迹**：未知 topic / 载荷缺键当场判 ``dead``
   （重试只是把一次配置错误拖成 N 次安静的重试），瞬时故障按指数退避重试并
   推进 ``available_at``。反过来的写法（先标 sent 再投递）会在投递失败时
   永久丢消息——而 Outbox 存在的全部意义就是「不丢」。

为什么把 ``consumer.delay`` 换成记录型假消费者：``_scan`` 只负责「到点的行 →
一次队列投递」，本文件要断言的是**搬运**本身（谁被投出去、投成什么样、失败后怎么记账），
不是 Celery 能不能连上 broker。真 Redis / ``FOR UPDATE SKIP LOCKED`` 并发那条路
只能在 ECS 的 PG 上验（SQLite 会安静地不渲染该子句，见 ``tasks/outbox.py`` 头注释）。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import tasks.outbox as outbox_tasks
from core.errors import ValidationError
from core.ids import as_utc, utcnow
from harness.outbox import (
    Topic,
    assert_payload_has_no_secret,
    enqueue,
    next_available_at,
)
from models.enums import OutboxStatus
from models.evidence import OutboxMessage

# ---------------------------------------------------------------- 夹具


@pytest.fixture
def worker_db(async_session_factory: async_sessionmaker[AsyncSession], monkeypatch) -> async_sessionmaker[AsyncSession]:
    """把 ``core.db.SessionFactory`` 换到内存库。

    ``tasks/outbox.py`` 走 ``core.db.session_scope()``，它自己 ``SessionFactory()``
    开会话——本机没有 Docker（CLAUDE.md §9），不换的话它会去连配置里的真实 PG，
    而症状是「用例卡在连接超时」，指向的是基础设施而不是被测代码。
    """
    from core import db as core_db

    monkeypatch.setattr(core_db, "SessionFactory", async_session_factory)
    return async_session_factory


class _FakeConsumer:
    """记录型假消费者：``delay`` 被调用即记一笔；可选地抛错以模拟投递失败。"""

    def __init__(self, *, error: Exception | None = None) -> None:
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self._error = error

    def delay(self, *args: Any, **kwargs: Any) -> None:
        self.calls.append((args, kwargs))
        if self._error is not None:
            raise self._error


def _patch_consumers(monkeypatch: pytest.MonkeyPatch, mapping: dict[str, Any]) -> None:
    """替换 ``_consumers()`` 的返回值——否则 ``_scan`` 会把任务真的发到 broker。"""
    monkeypatch.setattr(outbox_tasks, "_consumers", lambda: mapping)


# ---------------------------------------------------------------- 内核：纯函数


def test_退避时刻是纯指数曲线且封顶() -> None:
    """``now + min(2 ** attempts, 300)``，且 ``attempts < 1`` 直接报错。

    封顶这件事必须钉住：没有上限时 ``2 ** attempts`` 会在几十次之后变成天文数字，
    一条永远投不出去的消息会静默地「再也不投」——它与 ``dead`` 的差别只是没人知道。
    ``attempts = 0`` 报错而不是按 1 处理，是因为那说明调用方把「已失败次数」与
    「本次之后的次数」搞混了，两种自洽的约定混用会让退避间隔变成看调用方是谁。
    """
    base = datetime(2026, 10, 16, 8, 0, 0, tzinfo=UTC)

    assert next_available_at(1, now=base) == base + timedelta(seconds=2)
    assert next_available_at(2, now=base) == base + timedelta(seconds=4)
    assert next_available_at(8, now=base) == base + timedelta(seconds=256)
    # 第 9 次是 512 s，越过 300 s 上限 → 封顶值
    assert next_available_at(9, now=base) == base + timedelta(seconds=300)
    assert next_available_at(50, now=base) == base + timedelta(seconds=300)

    with pytest.raises(ValueError):
        next_available_at(0, now=base)


def test_载荷扫描拒绝凭据但放行令牌() -> None:
    """键名与字符串值双重扫描；``capability`` 键整体豁免但**必须**长得像 JWT。

    豁免键不放宽形状校验，否则它就成了绕过扫描的后门——「有人把口令塞进一个叫
    capability 的键」。嵌套也要扫：Secret 藏在 ``{"args": {"password": ...}}``
    里时只扫顶层等于没扫，而那种载荷的形态与正常载荷一模一样。
    """
    jwt = "eyJhbGciOiJFZERTQSJ9.eyJzdWIiOiIxIn0.c2ln"

    with pytest.raises(ValidationError):
        assert_payload_has_no_secret({"password": "x"})
    with pytest.raises(ValidationError):
        assert_payload_has_no_secret({"args": {"api_key": "x"}})  # 嵌套一层
    with pytest.raises(ValidationError):
        assert_payload_has_no_secret({"note": "password=hunter2"})  # 键名干净、值命中
    with pytest.raises(ValidationError):
        assert_payload_has_no_secret({"dsn": "mysql://u:p@10.0.0.5/db"})  # 连接串形态
    with pytest.raises(ValidationError):
        assert_payload_has_no_secret({"capability": "hunter2"})  # 豁免键但不是 JWT

    # 合法载荷：引用与标量 + 一块令牌
    assert_payload_has_no_secret({"run_id": 1, "capability": jwt})


# ---------------------------------------------------------------- 内核：入队


async def test_入队只写库既不提交也不投递(async_session: AsyncSession) -> None:
    """``enqueue`` 只 ``flush``：业务事实与待发命令必须由**同一持事务者**一起提交。

    若 ``enqueue`` 自己 commit，就把它从「同事务」里摘了出去——事务随后回滚时，
    这条命令已经独立落地，指向一个不存在的 run。用例用「回滚后这行不存在」
    证明它没有替调用方提交（断言 flush 后的可见性先证明确实写进去了）。
    """
    handle = await enqueue(
        async_session,
        topic=Topic.RUN_SUPERVISE,
        payload={"run_id": 7},
        idempotency_key="supervise:run:7",
    )
    # StrEnum 的 str() 取的是 value，落库形态与投递器匹配 topic 时的形态一致
    assert handle.topic == "etl.run.supervise"
    assert handle.idempotency_key == "supervise:run:7"

    # 同一事务里可见 → 确实写进去了
    visible = await async_session.scalar(
        select(OutboxMessage).where(OutboxMessage.id == handle.id)
    )
    assert visible is not None
    assert visible.status is OutboxStatus.PENDING
    assert visible.attempts == 0
    assert visible.dispatched is False

    # 回滚之后就不存在了 → enqueue 没有替调用方 commit
    await async_session.rollback()
    assert (
        await async_session.scalar(select(OutboxMessage.id).where(OutboxMessage.id == handle.id))
    ) is None


# ---------------------------------------------------------------- 投递器


async def test_到点的pending行被派发而未来的行不动(
    worker_db: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """到点的 → 交给消费者后标 ``sent``（sent_at / dispatched / attempts 都写）；
    ``available_at`` 在未来的 → 原样留在 ``pending``。

    「未来的不动」是延迟投递（补偿场景）能成立的前提：扫描条件多写一个 ``or``
    就会让延迟投递变成立即投递，而症状是「补跑的命令在错误的时间执行」。
    """
    submit = _FakeConsumer()
    supervise = _FakeConsumer()
    _patch_consumers(
        monkeypatch,
        {Topic.SEATUNNEL_SUBMIT.value: submit, Topic.RUN_SUPERVISE.value: supervise},
    )

    later = utcnow() + timedelta(hours=1)
    async with worker_db() as session:
        due = await enqueue(
            session,
            topic=Topic.SEATUNNEL_SUBMIT,
            payload={"run_id": 11, "capability": "a.b.c"},
            idempotency_key="k-due",
        )
        future = await enqueue(
            session,
            topic=Topic.RUN_SUPERVISE,
            payload={"run_id": 12},
            idempotency_key="k-future",
            available_at=later,
        )
        await session.commit()

    result = await outbox_tasks._scan(50)

    assert result == {"scanned": 1, "sent": 1, "retried": 0, "dead": 0}
    # 载荷里的 capability 被原样透传给消费者（Worker 只能从这儿拿到令牌原文）
    assert submit.calls == [((11,), {"capability": "a.b.c"})]
    assert supervise.calls == []

    async with worker_db() as session:
        due_row = await session.get(OutboxMessage, due.id)
        future_row = await session.get(OutboxMessage, future.id)
        assert due_row is not None and future_row is not None

        assert due_row.status is OutboxStatus.SENT
        assert due_row.dispatched is True
        assert due_row.sent_at is not None
        assert due_row.attempts == 1

        assert future_row.status is OutboxStatus.PENDING
        assert future_row.attempts == 0
        assert future_row.sent_at is None
        # 两端都夹住：与投递时传入的 later 完全相等，不掺入「真实现在」
        assert as_utc(future_row.available_at) == later


async def test_投递失败按退避重试并推进available_at(
    worker_db: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """瞬时故障 → ``failed`` + ``attempts+1`` + ``available_at`` 推到退避时刻。

    退避加法在 ``available_at`` **自身**的时区基准上做（``_aware``），
    而不是在扫描那一刻的 ``now`` 上做——否则一条消息被反复取到时，
    它的重试间隔会随「谁先扫到」而漂移。用例直接断言新的时刻
    ``== 旧时刻 + 2 ** 1 秒``：既不被真实时钟干扰，也不写死一个过去的字面量。
    """
    submit = _FakeConsumer(error=RuntimeError("broker down"))
    _patch_consumers(monkeypatch, {Topic.SEATUNNEL_SUBMIT.value: submit})

    async with worker_db() as session:
        handle = await enqueue(
            session,
            topic=Topic.SEATUNNEL_SUBMIT,
            payload={"run_id": 5},
            idempotency_key="k-retry",
        )
        await session.commit()

    async with worker_db() as session:
        row = await session.get(OutboxMessage, handle.id)
        assert row is not None
        before = as_utc(row.available_at)

    result = await outbox_tasks._scan(50)

    assert result["retried"] == 1
    assert result["sent"] == 0
    assert result["dead"] == 0
    assert submit.calls and submit.calls[0][0] == (5,)

    async with worker_db() as session:
        row = await session.get(OutboxMessage, handle.id)
        assert row is not None
        assert row.status is OutboxStatus.FAILED
        assert row.attempts == 1
        assert "RuntimeError" in (row.last_error or "")
        assert as_utc(row.available_at) == before + timedelta(seconds=2)


async def test_未知topic与缺键当场判死而不是反复重试(
    worker_db: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """未知 topic / 载荷缺必需键 → 立刻 ``dead``，不进入退避。

    这两类不是瞬时故障：重试到上限只是把一次配置错误拖成五次安静的重试，
    最后还是死。当场判死并写清 ``last_error``，排查方向才会落在
    「是谁写错了 topic / 谁漏了一个载荷键」上。缺键那条**根本不被投出去**——
    否则消费任务会收着不匹配的入参跑起来。
    """
    submit = _FakeConsumer()
    _patch_consumers(monkeypatch, {Topic.SEATUNNEL_SUBMIT.value: submit})

    async with worker_db() as session:
        unknown = await enqueue(
            session, topic="etl.nope", payload={"run_id": 1}, idempotency_key="k-unknown"
        )
        missing = await enqueue(
            session, topic=Topic.SEATUNNEL_SUBMIT, payload={}, idempotency_key="k-missing"
        )
        await session.commit()

    result = await outbox_tasks._scan(50)

    assert result["dead"] == 2
    assert result["sent"] == 0
    assert result["retried"] == 0
    assert submit.calls == []  # 缺键的那条连投都没投

    async with worker_db() as session:
        unknown_row = await session.get(OutboxMessage, unknown.id)
        missing_row = await session.get(OutboxMessage, missing.id)
        assert unknown_row is not None and missing_row is not None
        assert unknown_row.status is OutboxStatus.DEAD
        assert "未知 topic" in (unknown_row.last_error or "")
        assert missing_row.status is OutboxStatus.DEAD
        assert "run_id" in (missing_row.last_error or "")


# ---------------------------------------------------------------- 停止命令的载荷契约


def test_停止命令载荷缺执行主体就判死而不是照投() -> None:
    """``subject_id`` 对停止命令是必需键。

    worker 侧的 broker 在 BINDING 阶段要求 ``ctx.actor_id`` 非空
    （``harness/broker.py::_expected_binding``）：缺了它，这条停止命令**注定**被拒。
    如果 ``_args_for`` 照投，消息会被标成 ``sent``——于是 Outbox 表里
    「有人试图停作业但没停成」这件事点痕迹都不留，而排队中的作业还在跑。
    宁可在这里判死（``ValueError`` → ``DEAD`` + ``last_error``），让它出现在告警里。
    """
    with pytest.raises(ValueError, match="subject_id"):
        outbox_tasks._args_for(
            Topic.RUN_STOP.value, {"run_id": 7, "capability": "a.b.c", "reason": "manual_cancel"}
        )


def test_停止命令载荷带上执行主体后照投() -> None:
    """带上主体就能投，且主体被收成 ``int``（载荷是 JSON，可能是字符串）。"""
    args, kwargs = outbox_tasks._args_for(
        Topic.RUN_STOP.value,
        {"run_id": 7, "capability": "a.b.c", "reason": "manual_cancel", "subject_id": "42"},
    )
    assert args == [7]
    assert kwargs["subject_id"] == 42
    assert kwargs["reason"] == "manual_cancel"
    assert kwargs["capability"] == "a.b.c"
