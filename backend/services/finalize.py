"""运行收尾：把「数据面已经跑完」这件事推进成控制面的结论（be-14 完成段）。

## 为什么需要这个模块（而不是到处补一行调用）

``services.quality.finalize_report``（be-17）与 ``services.publish.publish_run``（be-18）
落地时**都没有调用方**。原因写在两份设计的交界处：be-17 §5 把「报告合格之后投递 Swap」
划给发布侧，be-18 §5 把「跑完且报告合格」划给执行编排——两边各自指向对方，
于是这条链的两端都在等对方先动：

- 作业跑完 → 没有人出质量报告 → ``quality_report`` 永远 0 行；
- 报告合格 → 没有人发布 → 正式表永远不换。

本模块就是那个「谁」。它读**一次**数据面状态，然后按事实把运行推进到结论。
``tasks/`` 只做编排（取一批 run_id、逐个调本模块、记日志），串接三个模块是业务逻辑。

## 四条实现纪律

1. **只在 ``running`` / ``interrupted`` 上推进**。``queued``（作业还没起来）、
   ``paused``（作业还在写影子表）、``rolling_back``（世界正在被另一次操作改写）
   一律不碰——理由与 ``services.quality.NOT_FINALIZABLE`` 同一批：算出来的结论不算数。
2. **中断的运行永不发布**。硬中断意味着影子表里是**半份数据**，把它换到正式表上
   比不发布坏得多。be-17 的报告在 ``interrupted=True`` 时已强制 ``passed=False``，
   这里再按运行状态闸一次：两道判据都能拦住它，而漏了一道的代价是一次错误的 Swap。
3. **查不到作业不改状态**（``orphaned``）。``stopped`` / ``not_found`` 说明数据面那边
   这个作业没了，但**控制面为什么该变成什么状态，这里推不出来**：可能是人工在 Zeta
   里 kill 的、可能是 SeaTunnel 重启丢了内存态、也可能是别人正在取消（状态还没落库）。
   把三种情况一律写成 ``failed`` 会让「谁停的」永久失去答案，所以只告警、留痕、不改。
4. **本模块不自己 commit 之外的事务**：``finalize_report`` 与 ``publish_run`` 各自
   ``commit``（事务边界归 service，``tests/test_service_commit_boundary.py`` 守着）。
   本模块因此不在外面裹事务——裹了就把那两次提交变成嵌套提交。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from clients.seatunnel.models import JobState, classify_failure
from core.ids import utcnow
from core.logging import get_logger
from harness.ledger import EvidenceLedger, LedgerEvent
from models.enums import EvidenceEventType, RunStatus
from models.execution import ExecutionRun
from schemas.run import RunSyncResult
from services import publish as publish_service
from services import quality as quality_service
from services import run as run_service

_ledger = EvidenceLedger()
log = get_logger(__name__)

#: 这些状态下**不推进**。与 ``services.quality.NOT_FINALIZABLE`` 刻意分开写：
#: 那张表回答「能不能出质量报告」，这张回答「这一趟要不要动」——
#: 将来若某状态允许出报告但仍不许发布，改这里而不是改那里。
NOT_ADVANCEABLE: frozenset[RunStatus] = frozenset(
    {RunStatus.QUEUED, RunStatus.PAUSED, RunStatus.ROLLING_BACK}
)

#: 已收尾（无出边）的状态。重复扫到就是空操作——扫描是周期性的，
#: 「跑完那一刻刚好被扫到」与「下一轮又扫到」必须得到同一个结果。
_SETTLED: frozenset[RunStatus] = run_service.TERMINAL_STATES | {RunStatus.ROLLED_BACK}


def _status_reader() -> Callable[[str], Awaitable[Any]]:
    """当前进程的作业状态来源。

    与 ``services.supervision._metrics_reader`` 同一取舍：收在一个函数里，
    单测**只替换这一处**就能把 SeaTunnel 摘掉；散着 ``SeatunnelClient(...)``
    的话，「这里也在偷偷读数据面」会很难发现。
    """
    from clients.seatunnel.client import SeatunnelClient
    from core.config import settings

    return SeatunnelClient(settings.seatunnel_api_base).status


async def _read_state(job_id: str) -> Any:
    """读一次作业状态。**失败不抛**：返回 ``UNKNOWN``，本趟就不推进。

    只读失败不该把「收尾」这件事判成失败——那会把一次网络抖动记成一次运行失败，
    而下一轮（30 s 后）还会再读一次，本来自己会好。
    """
    try:
        return await _status_reader()(job_id)
    except Exception as exc:  # 宽口是有意的：只读的退化路径，见 docstring
        log.warning("finalize_status_error", job_id=job_id, error_type=type(exc).__name__)
        return None


async def sync_run_outcome(session: AsyncSession, *, run_id: int) -> RunSyncResult:
    """读一次数据面事实，把这次运行推进到结论。**幂等**。

    幂等来自下游而不是这里：``finalize_report`` 靠 ``uq_quality_report_run_id``
    复用既有报告，``publish_run`` 靠 ``swap.succeeded`` / ``swap.failed`` 事件短路。
    所以扫描器每 30 s 重复扫到同一条运行是安全的——这也是「扫到即处理」不必走
    Outbox 的原因（发现本身每轮都会重来，丢一次投递不会永久丢失）。
    """
    run = await _load_run(session, run_id=run_id)
    if run.status in _SETTLED or run.status in NOT_ADVANCEABLE:
        return RunSyncResult(
            run_id=run_id, status=run.status, action="none", detail="状态无需推进"
        )

    if run.status is RunStatus.INTERRUPTED:
        # 硬中断的运行：作业已经（或正在）被停，结论不再取决于数据面自述。
        # 出报告是为了让「这次为什么没通过」有据可查（be-17 的报告带 interrupted=True）。
        return await _finalize_quality(session, run=run, allow_publish=False)

    if run.status is not RunStatus.RUNNING:  # pragma: no cover - 枚举新增状态时的兜底
        return RunSyncResult(
            run_id=run_id, status=run.status, action="none", detail=f"未处理的状态 {run.status.value}"
        )

    if not run.seatunnel_job_id:
        # 提交任务先把状态推成 running、再去调工具；中途崩掉就会留下这个形状。
        # 它是一条**卡住的运行**（没有作业在跑，也没有任何东西会再推进它），
        # 用 error 级日志让它可被搜到（§10 待办：M4 的硬中断/对账要给它一个自动处置）。
        log.error("run_running_without_job", run_id=run_id, project_id=run.project_id)
        return RunSyncResult(
            run_id=run_id, status=run.status, action="orphaned", detail="running 但没有 job_id"
        )

    status = await _read_state(run.seatunnel_job_id)
    state = getattr(status, "state", JobState.UNKNOWN)
    if state is JobState.FINISHED:
        return await _finalize_quality(session, run=run, allow_publish=True)
    if state is JobState.FAILED:
        return await _fail_from_data_plane(session, run=run, status=status)
    if state in (JobState.STOPPED, JobState.NOT_FOUND):
        log.warning(
            "run_orphaned_in_data_plane",
            run_id=run_id,
            job_id=run.seatunnel_job_id,
            job_state=state.value,
        )
        return RunSyncResult(
            run_id=run_id,
            status=run.status,
            action="orphaned",
            detail=f"数据面作业状态为 {state.value}，控制面不改状态",
        )
    # ``running`` / ``unknown``：还没跑完，或者状态读不出来。下一轮再看。
    return RunSyncResult(
        run_id=run_id, status=run.status, action="none", detail=f"作业状态 {state.value}"
    )


async def _finalize_quality(
    session: AsyncSession, *, run: ExecutionRun, allow_publish: bool
) -> RunSyncResult:
    """出质量报告；（合格且允许时）执行原子发布。"""
    report = await quality_service.finalize_report(
        session, tenant_id=run.tenant_id, run_id=run.id
    )
    report_id = report.report_id
    if not report.passed:
        # ``finalize_report`` 自己已经把「不过」写成了 run.failed + 现场 + 账本
        # （be-17 §5.4），这里只回报结论——不重复记账，也不改它写下的状态。
        return await _result(
            session,
            run=run,
            action="quality_finalized",
            report_id=report_id,
            passed=False,
            blocked_reason=report.blocked_reason,
        )

    if not allow_publish:
        # 走到了这里说明报告说「合格」而运行当时是 interrupted ——当前实现不该出现
        # （``build_report`` 在 interrupted 时强制 passed=False）。留一条 error：
        # 一旦有人放宽了那条判定，这条日志就是唯一的提示。
        log.error("interrupted_run_reported_as_passed", run_id=run.id, report_id=report_id)
        return await _result(
            session,
            run=run,
            action="quality_finalized",
            report_id=report_id,
            passed=True,
            detail="中断的运行报告为合格，按纪律不发布",
        )

    try:
        published = await publish_service.publish_run(
            session, tenant_id=run.tenant_id, run_id=run.id
        )
    except Exception as exc:
        # 发布失败**已经**由 publish 侧写成了失败结论 + 账本（它自己 commit）。
        # 这里记一条 warning 让它在日志里也留下痕迹，不吞、也不改它写下的结论。
        log.warning(
            "publish_failed",
            run_id=run.id,
            report_id=report_id,
            error_type=type(exc).__name__,
        )
        return await _result(
            session,
            run=run,
            action="failed",
            report_id=report_id,
            passed=True,
            detail=f"发布未完成：{type(exc).__name__}",
        )
    return await _result(
        session,
        run=run,
        action="published",
        report_id=report_id,
        passed=True,
        published=bool(getattr(published, "published", True)),
    )


async def _fail_from_data_plane(session: AsyncSession, *, run: ExecutionRun, status: Any) -> RunSyncResult:
    """数据面报作业失败 → 把运行推成 ``failed``，并留下**可归因**的现场。

    错误原文**不落库、不进日志**：SeaTunnel 的失败文本里常带整段配置与连接串
    （``clients/seatunnel/client.py`` 的模块 docstring 记着这条），所以只留
    ``classify_failure`` 的分类——它才是「要不要重试、要不要扩容」的判据，
    原文在数据面自己的日志里。这与 ``seatunnel.stop`` 只记 ``failure_kind`` 是同一条规矩。
    """
    kind = classify_failure(getattr(status, "error_summary", None))
    run.error_message = f"数据面作业失败（{kind.value}）"
    run.failure_snapshot = {
        **(run.failure_snapshot or {}),
        "stage": "data_plane",
        "job_state": str(getattr(getattr(status, "state", None), "value", "")),
        "failure_kind": kind.value,
        "retryable": bool(getattr(kind, "retryable", False)),
    }
    await run_service.apply_transition(
        session,
        run_id=run.id,
        target=RunStatus.FAILED,
        reason="data_plane_failed",
        actor_id=None,
    )
    await _ledger.append(
        session,
        LedgerEvent(
            tenant_id=run.tenant_id,
            project_id=run.project_id,
            actor_id=None,
            event_type=EvidenceEventType.RUN_FAILED,
            subject_type="run",
            subject_id=str(run.id),
            # 不填 artifact_hash：``execution_run`` 上没有指纹列（它在 preparation 上），
            # 为了一条事件再查一次 preparation 不值当。事件本身带 run_id，
            # 从 run 反查到制品是一次 join 的事。
            artifact_hash="",
            payload={
                "stage": "data_plane",
                "job_id": run.seatunnel_job_id or "",
                "failure_kind": kind.value,
                "retryable": bool(getattr(kind, "retryable", False)),
            },
            occurred_at=utcnow(),
        ),
    )
    await session.commit()
    log.warning("run_failed_in_data_plane", run_id=run.id, failure_kind=kind.value)
    return await _result(session, run=run, action="failed", detail=f"数据面失败：{kind.value}")


async def _load_run(session: AsyncSession, *, run_id: int) -> ExecutionRun:
    """按 id 取运行行（**跨租户**，只有扫描器会这么调）。

    租户不做入参：本模块的每一个下游调用都从**这一行**读出 tenant_id，
    把租户做成入参只会给调用方一次传错的机会，而「传错租户」的失败形态是
    数据静默写到另一个租户名下。
    """
    run = await session.scalar(select(ExecutionRun).where(ExecutionRun.id == run_id))
    if run is None:
        raise run_service.RunConflictError(f"运行 {run_id} 不存在")
    return run


async def _result(
    session: AsyncSession,
    *,
    run: ExecutionRun,
    action: str,
    report_id: int | None = None,
    passed: bool | None = None,
    blocked_reason: str | None = None,
    published: bool = False,
    detail: str = "",
) -> RunSyncResult:
    """组装结果，**状态从库里重读**。

    重读不是多余的一次查询：会话是 ``expire_on_commit=False``（``core/db.py``），
    下游服务提交之后，内存里的 ``run.status`` 还是进来时的旧值——照它回报，
    会得到「发布成功了但 status 还是 running」这种结论，而库里其实是对的。
    """
    fresh = await session.get(ExecutionRun, run.id, populate_existing=True)
    status = fresh.status if fresh is not None else run.status
    return RunSyncResult(
        run_id=run.id,
        status=status,
        action=action,
        report_id=report_id,
        passed=passed,
        blocked_reason=blocked_reason,
        published=published,
        detail=detail,
    )


async def list_pending_run_ids(limit: int) -> list[int]:
    """跨租户取一批**待收尾**的运行 id（只取 id，租户在 ``sync_run_outcome`` 里读）。

    ``running`` 与 ``interrupted`` 两类都要：前者在等数据面跑完，后者是硬中断之后
    **还没有出报告**的那一批（报告正是「为什么回滚」的依据——M3 演示场景二
    「脏数据超限硬中断 + 受管回滚」里，中断之后若不收尾，回滚理由就是空的）。

    三个过滤条件各有理由：

    - ``seatunnel_job_id IS NOT NULL``：还没提交的运行读不出状态，逐轮扫它只是浪费一次
      数据库查询（这种运行的卡住是另一类问题，``sync_run_outcome`` 里有对应的 error 日志）；
    - 状态已终态的排除：扫描器每 30 s 跑一次，不收窄的话，库里所有历史运行每轮都要被
      加载一次，只为得到一句「无需推进」；
    - 已出报告的中断运行排除（``NOT EXISTS``）：中断的运行**永远停在 interrupted**
      （be-17 §5.4 刻意不把它推成 failed——「被中断」与「不合格」是两件不同的事），
      于是它会一直留在 ``interrupted`` 里；不排掉它，每一轮都会重复读一次报告。

    幂等最终仍由下游保证（``uq_quality_report_run_id`` / ``swap.*`` 事件短路），
    这里的过滤只是让扫描器别做无用功。
    """
    from core.db import SessionFactory
    from models.execution import QualityReport

    async with SessionFactory() as session:
        values = await session.scalars(
            select(ExecutionRun.id)
            .where(
                ExecutionRun.status.in_((RunStatus.RUNNING, RunStatus.INTERRUPTED)),
                ExecutionRun.seatunnel_job_id.is_not(None),
                ~select(QualityReport.id)
                .where(QualityReport.run_id == ExecutionRun.id)
                .exists(),
            )
            .order_by(ExecutionRun.id)
            .limit(limit)
        )
        return [int(value) for value in values.all()]


__all__ = [
    "NOT_ADVANCEABLE",
    "list_pending_run_ids",
    "sync_run_outcome",
]
