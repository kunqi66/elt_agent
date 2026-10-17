"""be-14 完成段 / 运行收尾：``services.finalize`` + worker 的收尾入口与接线守卫。

**这个模块的五句核心断言**，其余用例都服务于它们：

1. **链路真的能闭合**：作业跑完 → 出质量报告 → （合格且允许时）原子发布 →
   运行变 ``succeeded``。此前 ``finalize_report``（be-17）与 ``publish_run``（be-18）
   **各自都没有调用方**——两份设计 §5 互相指向对方。所以「这条链走到底会怎样」
   必须有一个用例，而且它必须**走真的发布**（内存假发布器 + 真执行器 + 真状态机），
   不然「发布成功了但运行还是 running」这类结论错误不会被发现。
2. **中断的运行永不发布**：硬中断时影子表里是**半份数据**，换到正式表上比不发布坏得多。
   两道判据各有一条用例：be-17 的报告在 ``interrupted`` 时强制不合格（真路径），
   以及本模块的 ``allow_publish=False``（把报告伪造成合格来单验这道闸）。
3. **数据面报失败时不搬原文**：SeaTunnel 的失败文本里常带整段配置与连接串，
   只留 ``classify_failure`` 的分类。用例拿一个带哨兵凭据的失败文本穿过这条路径，
   断言那个串在库、快照、账本载荷里一处都找不到。
4. **查不到作业不改控制面状态**（``orphaned``）：谁停的、为什么没了，控制面推不出来；
   一律写 ``failed`` 会让这个答案**永久丢失**。只告警、留痕、不改。
5. **重复扫到同一条运行 = 空操作**：扫描环是周期性的（beat 40 s），
   「跑完那一刻被扫到」与「下一轮又扫到」必须得到同一个结果——发布是 Swap
   （对合变换），执行两次会把数据**悄悄换回去**，而且不报错。

分层与接缝：本文件走**真库**（内存 SQLite）+ 真 ``services.quality`` + 真
``services.publish``，只替换三处外部依赖，每处都对应一条真实的外部系统：

- 数据面的作业状态（``services.finalize._status_reader``，SeaTunnel REST）；
- 目标端（``tests.test_publish`` 的内存假发布器，Doris）；目标端的**只读**读取器
  （错误表计数）则由 ``tests.test_quality_service`` 的假读取器经公开注册入口替换；
- 密钥与 Redis（``tests.test_publish._wire_seams``，本机没有 ``/run/secrets``）。

复用另外两个模块的夹具是**有意**的：它们是同一批外部接缝的唯一权威假实现，
各写一份的话，「发布侧的假发布器改了、收尾侧没跟」会变成一条查不出来的差异。
"""

from __future__ import annotations

import json
from collections.abc import Awaitable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from clients.seatunnel.models import FailureKind, JobState
from core.config import settings
from harness.capability import CapabilityIssuer
from harness.outbox import Topic
from models.enums import EvidenceEventType, RunStatus
from models.execution import ExecutionRun, QualityReport
from schemas.run import RunSyncResult
from services import finalize as finalize_service
from services import run as run_service
from tests.test_publish import _CALLS as _TARGET_CALLS

# 发布侧的内存假发布器与它的种子（``_wire_seams`` 是那边 autouse 的接缝夹具）。
from tests.test_publish import FORMAL, SHADOW, _fingerprints, _wire_seams, keypair  # noqa: F401 —— 夹具，经 import 进入本模块
from tests.test_publish import _events as _publish_events
from tests.test_publish import _seed as _publish_seed
from tests.test_quality_service import (
    _allow_multiple_runs,
    _events,
    _fresh,
    install_reader,  # noqa: F401 —— 夹具，经 import 进来
)
from tests.test_quality_service import _make_run as _make_quality_run
from tests.test_quality_service import _seed as _quality_seed

#: 哨兵：一个「带凭据的连接串」。它穿过失败路径，断言的是**它没有落进任何地方**。
SECRET_SENTINEL = "jdbc:mysql://etl:S3cr3t-Pa55@10.0.0.9:3306/orders"

#: 默认作业 id。种子里的运行行都带它（收尾要求有 job_id 才去读数据面）。
JOB_ID = "job-1"


@pytest.fixture(autouse=True)
def _wire(_wire_seams: None) -> None:  # noqa: F811 —— 参数名就是那个夹具名，不是重复定义
    """把发布那一段的外部接缝接上（复用 test_publish 的那套）。

    autouse 是有意的：本文件**每一条**用例都可能走到发布（走不走取决于报告结论），
    逐条声明只会让人漏掉一条，而漏掉的表现是用例红在一个与它无关的地方
    （``_capability_issuer`` 去读本机不存在的 ``/run/secrets``）。
    """


# ---------------------------------------------------------------- 假数据面


@dataclass
class _Status:
    """SeaTunnel 的状态读取结果：只用到这两个字段。"""

    state: JobState
    error_summary: str | None = None


def _install_status(
    monkeypatch: pytest.MonkeyPatch,
    state: JobState | None = None,
    *,
    error_summary: str | None = None,
    raises: Exception | None = None,
) -> list[str]:
    """替换数据面状态来源（``_status_reader`` 是唯一注入点），返回读过的 job_id 列表。

    ``state=None`` 表示**不预设**：任何一次读取都判用例失败——用来验「这条路径
    根本不该去读数据面」（读到 0 次比读到某个值更值得断言）。
    """
    reads: list[str] = []

    async def _read(job_id: str) -> _Status:
        reads.append(job_id)
        if raises is not None:
            raise raises
        assert state is not None, f"本次用例不应该读数据面，却读了 {job_id}"
        return _Status(state=state, error_summary=error_summary)

    monkeypatch.setattr(finalize_service, "_status_reader", lambda: _read)
    return reads


@pytest.fixture
def worker_db(
    async_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> async_sessionmaker[AsyncSession]:
    """把 ``core.db.SessionFactory`` 换到内存库：``list_pending_run_ids`` 自己开会话。"""
    from core import db as core_db

    monkeypatch.setattr(core_db, "SessionFactory", async_session_factory)
    return async_session_factory


async def _set_job(session: AsyncSession, run_id: int, job_id: str | None) -> ExecutionRun:
    """给运行行塞一个（或清掉）作业 id —— 收尾的第一个分叉就看它。"""
    run = await session.get(ExecutionRun, run_id)
    assert run is not None
    run.seatunnel_job_id = job_id
    await session.commit()
    return run


def _sync(session: AsyncSession, run_id: int) -> Awaitable[RunSyncResult]:
    return finalize_service.sync_run_outcome(session, run_id=run_id)


# ================================================================ 主线：闭合


async def test_作业跑完且报告合格_真换表并变succeeded(
    async_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**本文件的红心**：跑完 → 报告合格 → 真的换了表 → 运行 succeeded。

    发布这一段的证据是**目标端的物理形态**（两张表的指纹互换），不是服务自报的
    ``published`` 字段——一个只报「我发布了」的实现能骗过断言，骗不过指纹。
    """
    world_ = await _publish_seed(async_session)  # 种子里已有一份 passed 的质量报告
    await _set_job(async_session, world_.run_id, JOB_ID)
    before = _fingerprints()
    reads = _install_status(monkeypatch, JobState.FINISHED)

    result = await _sync(async_session, world_.run_id)

    assert reads == [JOB_ID]  # 读了一次，且读的是运行行上的那个 job_id
    assert result.action == "published"
    assert result.published is True
    # 状态必须**从库里重读**：会话是 expire_on_commit=False，下游服务提交之后
    # 内存里的 run.status 还是 running，照它回报就会得出「发布成功但状态没变」。
    assert result.status is RunStatus.SUCCEEDED
    assert result.passed is True and result.report_id is not None
    assert (await _fresh(async_session, world_.run_id)).status is RunStatus.SUCCEEDED
    # 正式表拿到了本次数据（影子表的内容），影子表拿到了旧的正式表内容
    assert _fingerprints() == {FORMAL: before[SHADOW], SHADOW: before[FORMAL]}
    assert (
        len(
            await _publish_events(
                async_session,
                tenant_id=world_.tenant_id,
                event_type=EvidenceEventType.SWAP_SUCCEEDED,
            )
        )
        == 1
    )


async def test_重复扫到同一条运行是空操作(
    async_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """扫描环每 40 s 扫一次，「刚跑完」与「下一轮又扫到」必须得到同一个结果。

    Swap 是对合变换：再执行一次会把数据换回旧版本，而且**不报任何错**。
    """
    world_ = await _publish_seed(async_session)
    await _set_job(async_session, world_.run_id, JOB_ID)
    _install_status(monkeypatch, JobState.FINISHED)

    first = await _sync(async_session, world_.run_id)
    after = _fingerprints()
    second = await _sync(async_session, world_.run_id)

    assert first.action == "published"
    assert second.action == "none"  # 已终态：连数据面都不再读
    assert _fingerprints() == after
    assert _TARGET_CALLS.count(f"publish:{SHADOW}") == 1  # 副作用只发生过一次


async def test_报告不合格时不发布(
    async_session: AsyncSession,
    install_reader: Any,  # noqa: F811 —— 参数名就是那个夹具名，不是重复定义
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """报告判「不合格」：运行由 be-17 推成 ``failed``，**一张表都不动**。"""
    world = await _quality_seed(async_session)
    run = await _make_quality_run(async_session, world)
    reads = _install_status(monkeypatch, JobState.FINISHED)
    # 拒绝率 50%：远超默认预算，判定必然不合格
    install_reader(valid_rows=50, rejected_rows=50, by_rule={"r_null": 50})
    before = _fingerprints()

    result = await _sync(async_session, run.id)

    assert reads == [JOB_ID]
    assert result.action == "quality_finalized"
    assert result.passed is False
    assert result.blocked_reason  # 「为什么没通过」一定要有话说
    assert result.published is False
    assert result.status is RunStatus.FAILED
    assert _fingerprints() == before
    assert (
        len(
            await _publish_events(
                async_session,
                tenant_id=world.tenant_id,
                event_type=EvidenceEventType.SWAP_SUCCEEDED,
            )
        )
        == 0
    )


async def test_中断的运行出报告但绝不发布(
    async_session: AsyncSession,
    install_reader: Any,  # noqa: F811 —— 参数名就是那个夹具名，不是重复定义
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """硬中断之后收尾：报告是为了让「为什么回滚」有据可查，但绝不发布。

    这是第一道判据（真路径）：be-17 的 ``build_report`` 在 ``interrupted`` 时
    强制 ``passed=False``——影子表里是半份数据。
    """
    world = await _quality_seed(async_session)
    run = await _make_quality_run(async_session, world, status=RunStatus.INTERRUPTED)
    _install_status(monkeypatch, JobState.FINISHED)
    install_reader(valid_rows=10, rejected_rows=0)  # 目标端看起来一切正常

    result = await _sync(async_session, run.id)

    assert result.action == "quality_finalized"
    assert result.passed is False  # 中断的运行即便读到了干净数据也不合格
    assert result.published is False
    assert result.status is RunStatus.INTERRUPTED  # 刻意不推成 failed：「被中断」≠「不合格」
    assert await async_session.scalar(select(QualityReport).where(QualityReport.run_id == run.id))


async def test_报告说合格但运行是中断的_第二道判据拦下发布(
    async_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """把报告**伪造成合格**，单验 ``allow_publish=False`` 这道闸。

    正常情况下第一道判据就拦下了，所以这条闸在真路径上「永远不触发」——
    也正因为它不触发，一旦有人放宽了第一道判据，它就成了唯一还站着的那道。
    用例直接替换 ``finalize_report`` 的返回值来验它，而不是靠真数据凑出这个形状。
    """
    world = await _quality_seed(async_session)
    run = await _make_quality_run(async_session, world, status=RunStatus.INTERRUPTED)
    _install_status(monkeypatch, JobState.FINISHED)
    before = _fingerprints()

    async def _passed(*_args: Any, **_kwargs: Any) -> Any:
        # 只给收尾读过的那三个字段，形状由 ``QualityReportDTO`` 保证
        return type(
            "_DTO",
            (),
            {"report_id": 4242, "passed": True, "blocked_reason": None},
        )()

    async def _must_not_publish(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("中断的运行不应该走到发布")

    monkeypatch.setattr(finalize_service.quality_service, "finalize_report", _passed)
    monkeypatch.setattr(finalize_service.publish_service, "publish_run", _must_not_publish)

    result = await _sync(async_session, run.id)

    assert result.action == "quality_finalized"
    assert result.passed is True
    assert result.published is False
    assert "不发布" in result.detail
    assert result.report_id == 4242
    assert _fingerprints() == before


# ================================================================ 数据面失败


async def test_数据面报失败_判failed并留现场但不搬原文(
    async_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """失败只留**分类**，不留原文。

    SeaTunnel 的错误文本里常带整段配置与连接串（``clients/seatunnel/client.py``
    的模块 docstring 记着这条），把原文塞进 ``error_message`` 或账本载荷
    等于把凭据写进了控制面。分类才是「要不要重试」的判据。
    """
    world = await _quality_seed(async_session)
    run = await _make_quality_run(async_session, world)
    _install_status(
        monkeypatch, JobState.FAILED, error_summary=f"ConnectException: {SECRET_SENTINEL}"
    )

    result = await _sync(async_session, run.id)

    assert result.action == "failed"
    assert result.status is RunStatus.FAILED
    row = await _fresh(async_session, run.id)
    assert row.status is RunStatus.FAILED
    snapshot = dict(row.failure_snapshot or {})
    assert snapshot["stage"] == "data_plane"
    assert snapshot["failure_kind"] == FailureKind.CONNECT_FAILED.value
    assert snapshot["retryable"] is True  # 连接类失败值得重试
    assert snapshot["job_state"] == JobState.FAILED.value
    assert FailureKind.CONNECT_FAILED.value in (row.error_message or "")

    # 哨兵原文：库、快照、账本载荷，一处都不能有
    events = await _events(
        async_session, tenant_id=world.tenant_id, event_type=EvidenceEventType.RUN_FAILED
    )
    haystack = json.dumps(
        {
            "error_message": row.error_message,
            "snapshot": snapshot,
            "payloads": [dict(event.payload) for event in events],
        },
        ensure_ascii=False,
    )
    assert "S3cr3t-Pa55" not in haystack
    assert SECRET_SENTINEL not in haystack
    assert [event.payload["failure_kind"] for event in events] == [FailureKind.CONNECT_FAILED.value]


@pytest.mark.parametrize("state", [JobState.STOPPED, JobState.NOT_FOUND])
async def test_数据面找不到作业时只告警不改控制面状态(
    async_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, state: JobState
) -> None:
    """``stopped`` / ``not_found`` → ``orphaned``：控制面推不出「该变成什么」。

    可能是人工在 Zeta 里 kill 的、可能是 SeaTunnel 重启丢了内存态、也可能是别人
    正在取消（状态还没落库）。一律写 ``failed`` 会让「谁停的」永久失去答案。
    """
    run = await _make_quality_run(async_session, await _quality_seed(async_session))
    _install_status(monkeypatch, state)

    result = await _sync(async_session, run.id)

    assert result.action == "orphaned"
    assert result.status is RunStatus.RUNNING  # 一个字节都没改
    assert (await _fresh(async_session, run.id)).status is RunStatus.RUNNING
    assert state.value in result.detail
    # 不改状态、也不出报告：结论的产地只有一个，不能在这里凭空造一份
    assert await async_session.scalar(select(QualityReport).where(QualityReport.run_id == run.id)) is None


async def test_读作业状态失败时本趟不推进(
    async_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """只读失败不该把「收尾」判成失败：一次网络抖动被记成一次运行失败，
    而下一轮本来自己会好。"""
    run = await _make_quality_run(async_session, await _quality_seed(async_session))
    _install_status(monkeypatch, raises=RuntimeError("SeaTunnel REST 连不上"))

    result = await _sync(async_session, run.id)

    assert result.action == "none"
    assert result.status is RunStatus.RUNNING
    assert await async_session.scalar(select(QualityReport).where(QualityReport.run_id == run.id)) is None


async def test_作业还在跑时不推进(
    async_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``running`` / ``unknown`` 都算「还没跑完」：下一轮再看。"""
    run = await _make_quality_run(async_session, await _quality_seed(async_session))
    reads = _install_status(monkeypatch, JobState.RUNNING)

    result = await _sync(async_session, run.id)

    assert reads == [JOB_ID]
    assert result.action == "none"
    assert result.status is RunStatus.RUNNING


async def test_running但没有job_id记orphaned且不读数据面(
    async_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """提交任务先把状态推成 running、再去调工具；中途崩掉就会留下这个形状。

    它是一条**卡住的运行**（没有作业在跑，也没有任何东西会再推进它）。
    这里只记一条 error 级日志让它可被搜到——自动处置属于 M4 的对账，
    现在替它编一个状态只会掩盖「提交链路崩在半路」这个事实。
    """
    run = await _make_quality_run(async_session, await _quality_seed(async_session))
    await _set_job(async_session, run.id, None)
    reads = _install_status(monkeypatch)  # 不预设状态：读了就判失败

    result = await _sync(async_session, run.id)

    assert reads == []  # 没有 job_id 就不该打数据面
    assert result.action == "orphaned"
    assert result.status is RunStatus.RUNNING


# ================================================================ 幂等与状态闭集


@pytest.mark.parametrize(
    "status",
    [
        RunStatus.SUCCEEDED,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
        RunStatus.ROLLED_BACK,
        RunStatus.QUEUED,
        RunStatus.PAUSED,
        RunStatus.ROLLING_BACK,
    ],
)
async def test_不该由收尾推进的状态一律空操作(
    async_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, status: RunStatus
) -> None:
    """``queued``（作业还没起来）、``paused``（正在写影子表）、``rolling_back``
    （世界正在被另一次操作改写）算出来的结论都不算数；已终态则无需再算。"""
    run = await _make_quality_run(
        async_session, await _quality_seed(async_session), status=status
    )
    reads = _install_status(monkeypatch)

    result = await _sync(async_session, run.id)

    assert reads == []
    assert result.action == "none"
    assert (await _fresh(async_session, run.id)).status is status


def test_收尾覆盖全部运行状态() -> None:
    """**闭集守卫**：每个 ``RunStatus`` 都必须落在「会推进」或「不推进」其中一边。

    将来加了新状态而忘了归位，``sync_run_outcome`` 会落到那句带
    ``# pragma: no cover`` 的兜底里**静默返回 none**——一条永远不动的运行，
    而日志里一句提示都没有。这条元用例让加状态的人必须做一次选择。
    """
    covered = set(finalize_service.NOT_ADVANCEABLE) | set(run_service.TERMINAL_STATES) | {
        RunStatus.ROLLED_BACK,  # 已回滚：终态集合里没有它，但同样没有出边
        RunStatus.RUNNING,  # 收尾唯一真正推进的状态
        RunStatus.INTERRUPTED,  # 硬中断之后仍要出报告
    }
    assert covered == set(RunStatus), f"未归位的状态：{sorted(set(RunStatus) - covered)}"


# ================================================================ 取批：list_pending_run_ids


async def test_取批只要待收尾的运行(
    async_session: AsyncSession, worker_db: async_sessionmaker[AsyncSession]
) -> None:
    """三个过滤条件各有一条反例：无 job_id、已终态、已出报告。"""
    await _allow_multiple_runs(async_session)
    world_ = await _quality_seed(async_session)
    pending = await _make_quality_run(async_session, world_, key="k-pending")
    interrupted = await _make_quality_run(
        async_session, world_, status=RunStatus.INTERRUPTED, key="k-interrupted"
    )
    no_job = await _make_quality_run(async_session, world_, key="k-nojob")
    await _set_job(async_session, no_job.id, None)
    done = await _make_quality_run(
        async_session, world_, status=RunStatus.SUCCEEDED, key="k-done"
    )
    reported = await _make_quality_run(async_session, world_, key="k-reported")
    async_session.add(
        QualityReport(
            run_id=reported.id,
            valid_rows=10,
            rejected_rows=0,
            reject_rate=0,
            passed=True,
            generated_at=datetime(2026, 10, 16, 12, 0, 0),
        )
    )
    await async_session.commit()

    ids = await finalize_service.list_pending_run_ids(10)

    assert ids == [pending.id, interrupted.id]  # 按 id 升序，且只这两条
    assert done.id not in ids and no_job.id not in ids and reported.id not in ids


async def test_取批遵守上限(
    async_session: AsyncSession, worker_db: async_sessionmaker[AsyncSession]
) -> None:
    """一次只取 ``limit`` 条：扫描环每 40 s 一轮，不能把整个库拖进来。"""
    await _allow_multiple_runs(async_session)
    world_ = await _quality_seed(async_session)
    created = [
        (await _make_quality_run(async_session, world_, key=f"k-{index}")).id for index in range(3)
    ]

    assert await finalize_service.list_pending_run_ids(2) == created[:2]


# ================================================================ 接线守卫


def test_beat_里有收尾环且周期与监督环不同() -> None:
    """收尾必须有东西周期性地推它走；且周期不能与监督环相同。

    两条环都是浮点周期、相位跟着 beat 启动时刻走：**同周期又同时起跑就是永久锁步**，
    于是同一秒里两条环各对每条活跃运行发一次数据面请求。beat 没有「错开 n 秒」
    这种配置（Celery 的 crontab 只到分钟），错开只能靠不同周期。
    """
    from tasks.celery_app import celery_app

    schedule = celery_app.conf.beat_schedule
    assert schedule["finalize-finished-runs"]["task"] == "tasks.execution.scan_finished_runs"
    assert schedule["finalize-finished-runs"]["schedule"] != schedule["collect-run-metrics"]["schedule"]


def test_收尾任务已注册到应用() -> None:
    """``@shared_task`` 的名字要与 beat/Outbox 里写的字符串一致。

    对不上的症状是 ``Received unregistered task``——**任务不执行，业务层看不到错误**。
    """
    import tasks.execution as tasks_execution  # noqa: F401 —— 导入即注册
    from tasks.celery_app import celery_app

    names = set(celery_app.tasks)
    assert "tasks.execution.scan_finished_runs" in names
    assert "tasks.execution.finalize_run" in names
    assert "tasks.execution.rollback_run" in names


def test_回滚主题有消费者且载荷取键对得上() -> None:
    """``etl.target.rollback`` 曾经**没有消费者**：回滚请求投出去只会被记成 dead。

    取键也要对得上：``_args_for`` 逐 topic 显式取键，签名改了而这里没改的后果是
    运行期 ``TypeError``，而那时消息已被标 ``sent``。
    """
    from tasks import outbox as tasks_outbox

    consumers = tasks_outbox._consumers()
    assert Topic.TARGET_ROLLBACK.value in consumers
    assert consumers[Topic.TARGET_ROLLBACK.value].name == "tasks.execution.rollback_run"

    args, kwargs = tasks_outbox._args_for(
        Topic.TARGET_ROLLBACK.value, {"run_id": 7, "capability": "a.b.c"}
    )
    assert args == [7] and kwargs == {"capability": "a.b.c"}


def test_worker侧的broker带得上Capability校验公钥() -> None:
    """**这条守的是一个「整条链路一步都走不了」的缺陷。**

    ``ToolBroker`` 的第 ③ 步在 ``verifier is None`` 时**直接拒绝**，报的还是
    「本进程未配置 Capability 校验公钥」——读起来像配置问题。而 worker 侧的
    broker 一度就是这么构造的：提交、停止、发布、回滚四个工具在 worker 里
    全部一步都过不去，而单测用的是**绕过七步序**的假 broker，永远看不见。

    这里碰私有属性是有意的：缺陷本身就是「构造时少传一个具名参数」，
    没有任何公开接口能观察到它；但断言是**行为**的——拿同一把密钥签出来的
    令牌必须验得过，而不是「这个属性不是 None」。
    """
    from core.ids import utcnow
    from harness.broker import ToolBroker
    from models.enums import Env, ToolName
    from tasks import execution as tasks_execution

    broker = tasks_execution._broker()
    assert isinstance(broker, ToolBroker)

    issuer = CapabilityIssuer(
        private_key_path=settings.CAPABILITY_SIGNING_KEY_PATH,
        key_id=settings.CAPABILITY_KEY_ID,
        issuer=settings.CAPABILITY_ISSUER,
        ttl_sec=settings.CAPABILITY_TTL_SEC,
    )
    issued = issuer.issue(
        preparation_id=1,
        subject_id=1,
        tool=ToolName.SEATUNNEL_SUBMIT,
        env=Env.DEV,
        artifact_sha256="a" * 64,
        tenant_id=1,
        project_id=1,
        now=utcnow(),
    )

    verifier = broker._verifier
    assert verifier is not None, "worker 侧的 broker 没有校验公钥，第 ③ 步会拒绝一切"
    claims = verifier.verify_signature(issued.token, now=utcnow())
    assert claims.preparation_id == 1 and claims.subject_id == 1
