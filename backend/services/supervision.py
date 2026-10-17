"""运行监督服务（be-16 §3.2 / §5 / §6）。

**本模块是监督环的事务边界**：抄一次指标 → 写时序点 → 判阈值 → （超限）触发硬中断，
四个动作的提交都归这里。``harness.outbox.enqueue`` 与 ``harness.ledger.append``
都只写库不提交（见各自模块 docstring），原因也是这个。

## 三条不可退让的规则

1. **停作业只有一条链**：与人工取消（``services/run.py::request_cancel``）**同一条**——
   签一张绑定 ``seatunnel.stop`` 的单次 Capability，随 Outbox 投给 Worker，
   由 ``ToolBroker`` 去停。本模块**不碰 SeaTunnel、不碰目标库**（CLAUDE.md §5.1）。
   Worker 侧的 broker 刻意不带签发器（``tasks/execution._broker``），
   所以这里也不能「顺手直接调一次 stop」——那会绕开 Capability 校验、
   jti 原子消费与证据账本，正是「副作用唯一出口」最不该出现的后门。
2. **缺失值不许变成 0**（§6.3）：读不到的维度判 ``unknown``。
   用 0 顶替会让一个指标链路已断的作业看起来像一次正常空跑，
   而它可能正在往影子表里灌数据。
3. **判定的输入只有数字**：阈值比大小、维度优先级排序全部在
   ``engines/supervision``（纯函数层）。本模块不写一行 ``if observed > limit``——
   一旦有了第二处阈值判断，两处迟早不一致，而「哪里判错了」会变得无从定位。

## 与设计文档的偏差（be-16 §5.4）

- 设计 §5.4 第 2.3 步写的是「签发 Capability 后交给 ``ToolBroker`` 直接执行」。
  本实现改为 **Capability + Outbox**（与 ``request_cancel`` 一致，见规则 1）：
  监督运行在 Worker 里，而 Worker 的 broker 没有签发器也没有验签器；
  在 Worker 里自建一个带签发器的 broker 等于给「被攻陷的 Worker」发签名能力。
  代价是停止与状态写入**不再是同一个原子动作**——库里的 ``interrupted``
  先于作业真的停下，中间靠 Outbox 的至少一次投递兜住；
  ``failure_snapshot.needs_manual_stop`` 就是为了把这个中间态显式暴露给人。
- ``interrupt_run`` 收 ``DimensionVerdict`` 而不是 ``Dimension``（§3.2）：
  失败现场要记 ``observed`` / ``limit`` / ``ratio_pct``，只传维度名会逼出第二处重算。
- 告警/中断事件的 ``subject_type`` 用 ``"run"``（§6.4 写作 ``execution_run``）。
  理由是 ``services/governance.py`` 把 ``execution_run_id`` 过滤映射成
  ``subject_type="run"``——写成 ``execution_run`` 的话，这些事件在
  「这次运行到底发生了什么」的时间线里**一条都看不到**。
- ``stop_ok`` / ``needs_manual_stop`` 是**同步可判定**的那部分：
  ``stop_ok=True`` 表示停止命令已经进了 Outbox（或本来就没有作业可停），
  **不表示作业已经停了**——投递与执行是异步的。真的停下由
  ``capability.consumed`` / ``run.stopped`` 那两条证据来证明。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from structlog import get_logger

from clients.seatunnel.models import JobMetricsDTO
from core.config import settings
from core.errors import AppError, ErrorCode, NotFoundError
from core.ids import as_utc, utcnow
from engines.supervision import (
    Dimension,
    DimensionVerdict,
    MetricSample,
    Severity,
    SupervisionContractDTO,
    SupervisionVerdict,
    amplification,
    evaluate,
)
from engines.supervision.thresholds import WARN_REPEAT_INTERVAL_SEC
from harness.ledger import EvidenceLedger, LedgerEvent
from harness.outbox import Topic
from harness.outbox import enqueue as outbox_enqueue
from models.enums import EvidenceEventType, RunStatus, ToolName
from models.evidence import EvidenceEvent
from models.execution import ErrorRecord, ExecutionMetric, ExecutionRun
from models.governance import Capability, Preparation
from models.pipeline import Pipeline, PipelineVersion, RuntimeSupervisionContract
from schemas.supervision import CollectResult
from services import preparation as preparation_service
from services import run as run_service

log = get_logger(__name__)

_ledger = EvidenceLedger()

#: 指标点的取整粒度（秒）。beat 的 ``collect-run-metrics`` 就是这个周期。
#: 取整而不是记精确时刻：``task_acks_late`` 下重投是常态，两次投递相差几十毫秒
#: 会写成两条几乎重合的点，前端画出来的曲线带一串毛刺，而指标其实没变过。
#: 落在同一格里的第二次采集**覆盖**那一行（§6.5 幂等）。
METRIC_BUCKET_SEC = 30

#: 降频闸用的 subject_type，与 ``services/governance.py`` 的运行过滤一致（见模块 docstring）。
_RUN_SUBJECT_TYPE = "run"

#: 中断原因落进 ``execution_run.error_message`` 的脱敏摘要。
_INTERRUPT_MESSAGE = "运行预算超标，已硬中断（supervision_interrupt）"


# ---------------------------------------------------------------- 读事实


async def _load_run(
    session: AsyncSession, *, run_id: int, tenant_id: int | None = None
) -> ExecutionRun | None:
    """取运行行。给了 ``tenant_id`` 就强制过滤。

    不给 ``tenant_id`` 的只有监督环自己（``collect_active_runs`` 先跨租户挑出
    ``running`` 的 id，真正的事实——租户、项目、作业 id——都从**这一行**读出来，
    下游每次查询都带上它）。把 tenant_id 做成入参只会让调用方有机会传错，
    而「传错租户」的失败形态是数据静默写到另一个租户名下。
    """
    stmt = select(ExecutionRun).where(ExecutionRun.id == run_id)
    if tenant_id is not None:
        stmt = stmt.where(ExecutionRun.tenant_id == tenant_id)
    return await session.scalar(stmt)


async def _load_contract(
    session: AsyncSession, *, run: ExecutionRun
) -> RuntimeSupervisionContract | None:
    """取这次运行适用的预算契约。

    经 ``pipeline`` 回一次表，是为了让租户过滤真的落在这条 SQL 上
    （``pipeline_version`` 与 ``runtime_supervision_contract`` 自身都不带 tenant_id，
    只按 version_id 查等于少了一道跨租户的闸）。
    """
    return await session.scalar(
        select(RuntimeSupervisionContract)
        .join(
            PipelineVersion,
            PipelineVersion.supervision_contract_id == RuntimeSupervisionContract.id,
        )
        .join(Pipeline, Pipeline.id == PipelineVersion.pipeline_id)
        .where(
            PipelineVersion.id == run.pipeline_version_id,
            Pipeline.tenant_id == run.tenant_id,
        )
    )


def _metrics_reader() -> Callable[[str], Awaitable[JobMetricsDTO | None]]:
    """当前进程的指标来源。

    收在一个函数里，是为了让单测**只替换一处**就能把 SeaTunnel 摘掉
    （本机没有 Docker，集成环境也不是每个用例都有）。散着 ``build_client()``
    调用的话，「这里也在偷偷读 SeaTunnel」会变得很难发现。
    """
    from clients.seatunnel.client import SeatunnelClient
    from core.config import settings

    return SeatunnelClient(settings.seatunnel_api_base).metrics


async def _read_metrics(job_id: str | None) -> JobMetricsDTO | None:
    """读一次指标。**任何失败都退化成 ``None``**，绝不把监督环带走。

    只读失败阻塞监督是拿一个坏结果换一个更坏的结果：作业照跑，
    而「谁在看着它」没了。客户端自己已经在 HTTP 层回 ``None``，
    这里再兜一层是防「解析出的新异常」——它不该让 beat 任务整批失败。
    """
    if not job_id:
        return None
    try:
        return await _metrics_reader()(job_id)
    except Exception as exc:  # 退化路径是设计的一部分（§6.3）：宽口捕获是有意的
        log.warning("supervision_metrics_error", job_id=job_id, error_type=type(exc).__name__)
        return None


def _elapsed_sec(run: ExecutionRun, now: datetime) -> float | None:
    """已运行时长。**由控制面自己算**，与数据面自述无关（见 ``engines.supervision``）。

    ``started_at`` 为空返回 ``None``（→ 时长维度 unknown），不返回 0：
    「还没开始跑」与「跑了 0 秒」是两件事，前者不该被当成一个正常的读数。
    """
    if run.started_at is None:
        return None
    delta = (now - as_utc(run.started_at)).total_seconds()
    return delta if delta >= 0 else 0.0


def _ratio(reject: int | None, accepted: int | None) -> float | None:
    """拒绝率 = 拒绝行 / （拒绝行 + 成功写入行）。

    分母为 0（一行都还没走到）时返回 ``None``：``0/0`` 不是 0%，
    而是「这一次无话可说」。判成 0 会让刚启动的作业带一条「拒绝率正常」的记录。
    """
    if reject is None or accepted is None:
        return None
    total = reject + accepted
    if total <= 0:
        return None
    return reject / total


def _sample(metrics: JobMetricsDTO | None, elapsed_sec: float | None) -> MetricSample:
    """SeaTunnel 指标 → 判定用的采样。

    ``read_bytes`` 里 ``0`` 要翻成 ``None``：DTO 用 ``0`` 表示「这个计数器没报」
    （它的字段刻意都可缺省为 0），而 0 做分母没有意义——放大比要的是
    「读到了多少字节」，不是「读到了 0 字节」。翻错方向的后果是
    放大比明明算不出来却被算成「写了很多、读为零」的无穷大，直接误报中断。

    其余字段照原值传：DTO 的 0 是**数据面报的 0**，保持不变。
    """
    if metrics is None:
        return MetricSample(duration_sec=elapsed_sec or 0.0)
    return MetricSample(
        read_rows=metrics.read_rows,
        written_rows=metrics.written_rows,
        written_bytes=metrics.written_bytes,
        read_bytes=metrics.read_bytes or None,
        reject_rows=metrics.reject_rows,
        valid_rows=metrics.written_rows,
        reject_rate=_ratio(metrics.reject_rows, metrics.written_rows),
        duration_sec=elapsed_sec or 0.0,
    )


# ---------------------------------------------------------------- 写时序


def _bucket(now: datetime) -> datetime:
    """把时刻落到 30 秒的格子上（``METRIC_BUCKET_SEC``）。"""
    epoch = int(now.timestamp())
    return datetime.fromtimestamp(epoch - epoch % METRIC_BUCKET_SEC, tz=UTC)


def _int0(value: int | None) -> int:
    """时序列是 NOT NULL 的，读不到只能写 0。

    「读不到」这件事**没有被丢掉**：它留在本轮的 ``SupervisionVerdict`` 里
    （``unknown``），也留在 ``failure_snapshot`` 里（``metric_points`` 的相邻点看不出来，
    但 ``reason_dimension`` 会指出来）。列上没有 NULL 可选是表结构决定的，
    所以这里按 0 落，并在 run 汇总上**不回退**（见 :func:`_apply_summary`）。
    """
    return 0 if value is None else int(value)


def _throughput(written_rows: int | None, elapsed_sec: float | None) -> float:
    if written_rows is None or not elapsed_sec or elapsed_sec <= 0:
        return 0.0
    return round(written_rows / elapsed_sec, 4)


async def _latest_metric(session: AsyncSession, *, run_id: int) -> ExecutionMetric | None:
    """最后一条时序点（判「这一格是不是已经写过了」）。"""
    return await session.scalar(
        select(ExecutionMetric)
        .where(ExecutionMetric.run_id == run_id)
        .order_by(ExecutionMetric.ts.desc(), ExecutionMetric.id.desc())
        .limit(1)
    )


async def _record_metric(
    session: AsyncSession,
    *,
    run: ExecutionRun,
    sample: MetricSample,
    elapsed_sec: float | None,
    now: datetime,
) -> ExecutionMetric:
    """落一条时序点。同一格内重复采集**覆盖**那一行，不新增。

    用「读最近一条、在 Python 里比时间」而不是 ``WHERE ts = :ts``：
    SQLite 读回的是裸 datetime，两种形态（带/不带时区）在 SQL 里比不出相等，
    于是本机单测永远走「新增」分支，而幂等那一条在容器里才生效——
    这种「本机绿、线上才发现」的差异正是要避免的。
    """
    ts = _bucket(now)
    point = {
        "read_rows": _int0(sample.read_rows),
        "written_rows": _int0(sample.written_rows),
        "written_bytes": _int0(sample.written_bytes),
        "duration_sec": int(elapsed_sec or 0),
        "throughput": _throughput(sample.written_rows, elapsed_sec),
        "amplification_ratio": amplification(sample) or 0,
        "reject_rate": sample.reject_rate or 0,
    }
    existing = await _latest_metric(session, run_id=run.id)
    if existing is not None and as_utc(existing.ts) == ts:
        for column, value in point.items():
            setattr(existing, column, value)
        return existing

    row = ExecutionMetric(run_id=run.id, ts=ts, **point)
    session.add(row)
    await session.flush()
    return row


def _apply_summary(run: ExecutionRun, *, sample: MetricSample, elapsed_sec: float | None) -> None:
    """把本轮读数盖到 ``execution_run`` 的汇总列上。

    **读不到的维度不回退成 0**（§6.3）：这一轮读不到，就保持上一轮的值。
    回退成 0 的后果是「指标链路抖一下，前端看到搬运量掉回 0」，
    而那个 0 会被后续任何按汇总列做的判断（前端、诊断）当成事实。
    """
    if sample.read_rows is not None:
        run.read_rows = sample.read_rows
    if sample.written_rows is not None:
        run.written_rows = sample.written_rows
        if elapsed_sec and elapsed_sec > 0:
            run.avg_throughput = round(sample.written_rows / elapsed_sec, 4)
    if sample.written_bytes is not None:
        run.written_bytes = sample.written_bytes
    if sample.reject_rows is not None:
        run.reject_rows = sample.reject_rows


# ---------------------------------------------------------------- 告警


async def _latest_warning(
    session: AsyncSession, *, run: ExecutionRun, run_id: int
) -> EvidenceEvent | None:
    return await session.scalar(
        select(EvidenceEvent)
        .where(
            EvidenceEvent.tenant_id == run.tenant_id,
            EvidenceEvent.event_type == EvidenceEventType.SUPERVISION_WARNING,
            EvidenceEvent.subject_type == _RUN_SUBJECT_TYPE,
            EvidenceEvent.subject_id == str(run_id),
        )
        .order_by(EvidenceEvent.seq.desc())
        .limit(1)
    )


async def should_emit_warning(
    session: AsyncSession, *, run_id: int, dimension: str, now: datetime
) -> bool:
    """这一轮要不要落一条 ``supervision.warning``（§6.4 的三道闸）。

    闸① 同 run 同维度还没有过告警 → 写；
    闸② 最近一条是**别的维度** → 写（换了病，就要再说一次）；
    闸③ 同维度但已经过了 ``WARN_REPEAT_INTERVAL_SEC`` → 写，否则压掉。

    ``dimension`` 收字符串而不是 ``Dimension``：还有一个 ``"degraded"``
    的哨兵值（§6.3：五个维度全读不到）。它不是判定维度，不该进
    ``Dimension`` 词表——否则前端与规则表都会被迫认识一个永远不会被「超标」的维度。

    **降频状态只看账本，不新增 Redis 键**：账本是持久事实、天然带时间与维度，
    用 Redis 做这件事等于把「发过没发过」拆到两个可能不同步的存储里，
    而其中一个（Redis）重启即失忆——失忆的表现是告警风暴。
    """
    run = await _load_run(session, run_id=run_id)
    if run is None:
        # 运行已经不在（保留策略清过）。能少发一条就少发一条。
        return True

    last = await _latest_warning(session, run=run, run_id=run_id)
    if last is None:
        return True
    payload = dict(last.payload or {})
    if str(payload.get("dimension") or "") != dimension:
        return True

    gap = (now - as_utc(last.occurred_at)).total_seconds()
    if gap < 0:
        # 时钟回拨（或库里那行是裸值被当成本地时区）。宁可多记一条，
        # 也不要因为算出一个负数就**永久**压掉这个维度的告警。
        return True
    return gap >= WARN_REPEAT_INTERVAL_SEC


def _text_of(value: float | None) -> str:
    """账本 payload 里的数值一律转字符串（``harness.ledger`` 的硬要求）。

    float 进 payload 会让同一语义在两台机器上算出不同哈希（``repr`` 受平台影响），
    而账本是靠哈希链自证的。比率与字节数在这里都无损——
    它们的值域远小于 ``str(float)`` 能精确表示的范围。
    """
    return "" if value is None else str(float(value))


async def _append_warning(
    session: AsyncSession,
    *,
    run: ExecutionRun,
    verdict: SupervisionVerdict,
    dimension: str,
    now: datetime,
) -> None:
    reason = verdict.reason
    await _ledger.append(
        session,
        LedgerEvent(
            tenant_id=run.tenant_id,
            project_id=run.project_id,
            actor_id=None,
            event_type=EvidenceEventType.SUPERVISION_WARNING,
            subject_type=_RUN_SUBJECT_TYPE,
            subject_id=str(run.id),
            payload={
                "run_id": run.id,
                "dimension": dimension,
                "severity": verdict.severity.value,
                "observed": _text_of(reason.observed) if reason else "",
                "limit": _text_of(reason.limit) if reason else "",
                "ratio_pct": _text_of(reason.ratio_pct) if reason else "",
                # 其余达到 warn 的维度：只看 reason 会把「同时超标的写入量」盖掉，
                # 而它往往才是根因（拒绝率超标是果）。
                "also_warned": [item.dimension.value for item in verdict.warnings],
            },
            occurred_at=now,
        ),
    )


def _is_degraded(verdict: SupervisionVerdict) -> bool:
    """**数据面**的四个维度全部读不到（§6.3）——指标链路断了，监督其实没在监督。

    判据里刻意**不含** ``DURATION``：时长由控制面自己计时（``now - started_at``），
    无论 SeaTunnel 答不答话都读得到。把它算进来，``全部 unknown`` 就永远不会成立，
    于是「指标全断」这条最该报警的退化路径反而安静无声。

    也不是「有任意一个 unknown 就算」：只断一路时其余三路仍在看着，
    那样发告警只会把真警报淹掉。
    """
    datalink = [item for item in verdict.dimensions if item.dimension is not Dimension.DURATION]
    if not datalink:  # pragma: no cover - evaluate 恒返回五个维度
        return True
    return all(item.severity is Severity.UNKNOWN for item in datalink)


# ---------------------------------------------------------------- 采集


async def collect_run(
    session: AsyncSession, *, run_id: int, now: datetime | None = None
) -> CollectResult:
    """抄一次指标、写时序、判阈值、按结论告警或硬中断。

    顺序不可换：「先写时序点、再判阈值」而不是反过来。反过来的话，
    判定依赖的那个读数没有落库，事后复盘时**拿不到**中断现场那一帧的数据，
    只剩一句「当时超了」。
    """
    moment = now or utcnow()
    run = await _load_run(session, run_id=run_id)
    if run is None:
        log.warning("supervision_run_not_found", run_id=run_id)
        return CollectResult(run_id=run_id, collected=False, status="not_found")

    if run.status is not RunStatus.RUNNING:
        # 活跃清单是先查后采的：中间被人工取消 / 作业自己结束了，这一轮就无事可做。
        # 报成失败会制造假警报（每次取消都会留一条"监督失败"）。
        return CollectResult(run_id=run_id, collected=False, status=f"not_running:{run.status.value}")

    contract = await _load_contract(session, run=run)
    if contract is None:
        # 契约行缺失是**数据完整性错误**，不是业务拒绝：没有预算就无从判定，
        # 而「无从判定」绝不能被当成「没超标」——那等于在一个没有刹车的作业上放行。
        raise AppError(
            f"运行 {run_id} 的版本没有运行预算契约（supervision_contract_id 指向的行不存在）",
            code=ErrorCode.INTERNAL_ERROR,
        )

    elapsed_sec = _elapsed_sec(run, moment)
    metrics = await _read_metrics(run.seatunnel_job_id)
    sample = _sample(metrics, elapsed_sec)
    verdict = evaluate(sample, _contract_dto(contract), elapsed_sec)

    await _record_metric(session, run=run, sample=sample, elapsed_sec=elapsed_sec, now=moment)
    _apply_summary(run, sample=sample, elapsed_sec=elapsed_sec)
    await session.commit()

    if verdict.severity is Severity.INTERRUPT:
        reason = verdict.reason
        assert reason is not None  # severity 由 reason 决定，二者不会不一致
        interrupted = await interrupt_run(session, run_id=run_id, reason=reason, actor_id=None)
        return CollectResult(
            run_id=run_id,
            collected=True,
            # 报 ``interrupt_run`` 的**实际结果**，不是「我打算中断」：
            # 并发的另一个采集周期可能已经把它带走了（抢输返回 False）。
            # 这里想当然地写 True，日志与压测输出就会显示一次并没有发生的中断。
            status=RunStatus.INTERRUPTED.value if interrupted else "race_lost",
            severity=verdict.severity,
            reason_dimension=reason.dimension.value,
            interrupted=interrupted,
            degraded=_is_degraded(verdict),
        )

    dimension = _warning_dimension(verdict)
    warned = False
    if dimension is not None and await should_emit_warning(
        session, run_id=run_id, dimension=dimension, now=moment
    ):
        await _append_warning(session, run=run, verdict=verdict, dimension=dimension, now=moment)
        await session.commit()
        warned = True
        log.warning(
            "supervision_warning",
            run_id=run_id,
            dimension=dimension,
            severity=verdict.severity.value,
        )

    return CollectResult(
        run_id=run_id,
        collected=True,
        status=RunStatus.RUNNING.value,
        severity=verdict.severity,
        reason_dimension=dimension,
        warned=warned,
        degraded=_is_degraded(verdict),
    )


def _warning_dimension(verdict: SupervisionVerdict) -> str | None:
    """这一轮该以哪个「维度名」发告警（``None`` = 不发）。

    整体退化（全 unknown）优先：那时 ``severity`` 恰好也是 ``unknown``，
    但它是**哨兵**而不是某个维度的判定，混用会让前端把「指标链路不可用」
    显示成「duration 读不到」。
    """
    if _is_degraded(verdict):
        return "degraded"
    if verdict.severity is Severity.WARN and verdict.reason is not None:
        return verdict.reason.dimension.value
    return None


def _contract_dto(row: RuntimeSupervisionContract) -> SupervisionContractDTO:
    """ORM 行 → 纯函数层的 DTO。

    ``Numeric`` 列在 PG 上是 ``Decimal``、在 SQLite 上是 ``float``——
    这里统一转 ``float``：让 ``Decimal`` 进纯函数层，判定结果就会随
    「从哪种库读出来」而变（``Decimal('0.05') >= 0.04`` 与 ``0.05 >= 0.04``
    在边界上是两种结论）。
    """
    return SupervisionContractDTO(
        max_read_rows=int(row.max_read_rows),
        max_write_bytes=int(row.max_write_bytes),
        max_duration_sec=int(row.max_duration_sec),
        max_amplification_ratio=float(row.max_amplification_ratio),
        max_reject_rate=float(row.max_reject_rate),
        warn_threshold_pct=int(row.warn_threshold_pct),
    )


# ---------------------------------------------------------------- 硬中断


async def interrupt_run(
    session: AsyncSession,
    *,
    run_id: int,
    reason: DimensionVerdict | None,
    actor_id: int | None = None,
) -> bool:
    """硬中断一次运行：置 ``interrupted`` + 发停止命令 + 留现场 + 落两条证据。

    ``True`` 表示**这一次调用真的完成了中断**；``False`` 表示这次是空操作
    （运行不存在，或并发中抢输了——状态已经不是 ``running``）。空操作不是错误，
    所以不抛异常：抛出去会让 Outbox 把一个不可能成功的命令重投到死。

    幂等/并发靠**条件 UPDATE 的 rowcount**（§6.5），不靠「先查状态再写」——
    先查后写在两次采集周期重叠时会让两个执行者都以为自己是唯一的中断者。
    """
    moment = utcnow()
    run = await _load_run(session, run_id=run_id)
    if run is None:
        log.warning("interrupt_run_not_found", run_id=run_id)
        return False

    result = await session.execute(
        update(ExecutionRun)
        .where(ExecutionRun.id == run_id, ExecutionRun.status == RunStatus.RUNNING)
        .values(
            status=RunStatus.INTERRUPTED,
            finished_at=moment,
            error_message=_INTERRUPT_MESSAGE,
        )
        .execution_options(synchronize_session=False)
    )
    if result.rowcount == 0:
        log.info("interrupt_run_noop", run_id=run_id)
        return False

    # 条件 UPDATE 走的是 Core 语句，**不会**改动已在同一 session 里的那个 ORM 实例。
    # 不补这一行的话，同一个 session 里第二次读到的 `run.status` 还是 running——
    # 表现是「库里已经是 interrupted、代码却继续按 running 处理」，
    # 而这一类「内存与库不一致」的错会在离现场很远的地方报出来。
    run.status = RunStatus.INTERRUPTED

    # 停作业与留证据。步骤 ②③ 的顺序：先发命令再写现场——
    # 反过来的话，发命令那一步抛异常就只留下一个「已中断」而没有停止命令，
    # 而作业还在跑。
    needs_manual_stop, stop_ok, artifact_hash = await _dispatch_stop(
        session, run=run, actor_id=actor_id, now=moment
    )

    snapshot = await save_failure_snapshot(
        session,
        run_id=run_id,
        reason=reason,
        needs_manual_stop=needs_manual_stop,
        stop_ok=stop_ok,
    )
    if snapshot:
        # 只 merge，不整体覆盖：``quality`` / ``swap`` / ``diagnosis`` 三个子键
        # 分别归 be-17/18/19，整体赋值会把它们的现场一起抹掉（§8）。
        run.failure_snapshot = {**(run.failure_snapshot or {}), **snapshot}

    await _ledger.append(
        session,
        LedgerEvent(
            tenant_id=run.tenant_id,
            project_id=run.project_id,
            actor_id=actor_id,
            event_type=EvidenceEventType.RUN_INTERRUPTED,
            subject_type=_RUN_SUBJECT_TYPE,
            subject_id=str(run_id),
            artifact_hash=artifact_hash,
            payload={
                "run_id": run_id,
                "reason": "supervision",
                "dimension": reason.dimension.value if reason else "",
                "observed": _text_of(reason.observed) if reason else "",
                "limit": _text_of(reason.limit) if reason else "",
                "job_id": run.seatunnel_job_id or "",
                "needs_manual_stop": needs_manual_stop,
            },
            occurred_at=moment,
        ),
    )
    await session.commit()

    log.warning(
        "run_interrupted",
        run_id=run_id,
        dimension=reason.dimension.value if reason else "",
        job_id=run.seatunnel_job_id,
        needs_manual_stop=needs_manual_stop,
    )
    return True


async def _dispatch_stop(
    session: AsyncSession, *, run: ExecutionRun, actor_id: int | None, now: datetime
) -> tuple[bool, bool, str]:
    """签发停止 Capability 并随 Outbox 投给 Worker（与 ``request_cancel`` 同一条链）。

    返回 ``(needs_manual_stop, stop_ok, artifact_hash)``：

    - ``needs_manual_stop=True`` 表示**停止命令没能发出去**——作业大概率还在跑，
      必须有人去看。触发它的两种情况：令牌签不出来（没有操作主体 / 准备单没了）。
      把它当成普通失败会让「金额继续往目标表灌」这件事只留一行日志。
    - ``stop_ok`` 只表示「命令已经进了 Outbox（或本来就没有作业可停）」，
      **不表示作业已经停了**（见模块 docstring）。
    """
    if not run.seatunnel_job_id:
        # 作业根本没起来（提交失败 / 还没提交到 Zeta）。没有要停的东西，
        # 也没有停止命令要发——这时 needs_manual_stop 必须是 False，
        # 否则每一次「提交就失败」都会挂上一条「需要人工停作业」的待办。
        log.info("interrupt_no_job", run_id=run.id)
        return False, True, ""

    subject_id = actor_id if actor_id is not None else run.triggered_by
    preparation = await _load_preparation(session, run=run)
    if subject_id is None or preparation is None:
        # 没有操作主体就签不出令牌；broker 也会拒绝没有主体的令牌
        # （``harness/broker.py::_expected_binding``）。这时**不能**假装停过了。
        log.error(
            "interrupt_stop_not_dispatched",
            run_id=run.id,
            job_id=run.seatunnel_job_id,
            has_subject=subject_id is not None,
            has_preparation=preparation is not None,
        )
        return True, False, preparation.artifact_sha256 if preparation else ""

    issued = run_service._capability_issuer().issue(
        preparation_id=preparation.id,
        subject_id=subject_id,
        tool=ToolName.SEATUNNEL_STOP,
        env=preparation.environment,
        artifact_sha256=preparation.artifact_sha256,
        tenant_id=run.tenant_id,
        project_id=run.project_id,
        now=now,
    )
    session.add(
        Capability(
            jti=issued.claims.jti,
            preparation_id=preparation.id,
            subject_id=subject_id,
            tool=issued.claims.tool.value,
            environment=preparation.environment,
            artifact_sha256=preparation.artifact_sha256,
            token_sha256=issued.token_sha256,
            signature_alg="EdDSA",
            key_id=settings.CAPABILITY_KEY_ID,
            issued_at=issued.claims.iat,
            expires_at=issued.claims.exp,
        )
    )
    await session.flush()

    # ``idempotency_key`` 与人工取消**故意不同**：同一个 run 先被人取消、
    # 又被监督判超标时，两条命令都要各投一次（它们各自绑定一张令牌），
    # 用同一个键会让第二条撞唯一约束、静默丢掉。
    await outbox_enqueue(
        session,
        topic=Topic.RUN_STOP,
        payload={
            "run_id": run.id,
            "capability": issued.token,
            "reason": "supervision_interrupt",
            # 与令牌里签的 ``subject_id`` 同值（监督无人类操作人时回落到
            # ``run.triggered_by``，即「这条运行的责任主体」）。
            # worker 侧 broker 要求 ``ctx.actor_id`` 非空，缺了它这条硬中断
            # 会在 BINDING 阶段被拒——而那是「明知超限却拦不住」的最坏形状。
            "subject_id": subject_id,
        },
        idempotency_key=f"supervision-stop:run:{run.id}",
    )
    await _ledger.append(
        session,
        LedgerEvent(
            tenant_id=run.tenant_id,
            project_id=run.project_id,
            actor_id=subject_id,
            event_type=EvidenceEventType.CAPABILITY_ISSUED,
            subject_type="capability",
            subject_id=str(issued.claims.jti),
            artifact_hash=preparation.artifact_sha256,
            payload={
                "preparation_id": preparation.id,
                "tool": ToolName.SEATUNNEL_STOP.value,
                "run_id": run.id,
                "reason": "supervision_interrupt",
            },
            occurred_at=now,
        ),
    )
    return False, True, preparation.artifact_sha256


async def _load_preparation(session: AsyncSession, *, run: ExecutionRun) -> Preparation | None:
    """取准备单（令牌的 env / artifact_sha256 必须**从行上重算**，不信调用方传参）。

    刻意**不用** ``preparation_service.assert_committable``：那个入口会重新校验
    审批完整性与有效期，而中断的时候恰好可能是「准备单刚过期」——
    拿它当门槛会让一次已经判定必须停止的作业因为一份过期的单据而**继续跑**。
    这里只要事实（env / sha256），不要结论。
    """
    try:
        return await preparation_service.load_preparation(
            session, tenant_id=run.tenant_id, preparation_id=run.preparation_id
        )
    except NotFoundError:
        # 准备单被保留策略清掉了。不停就是错的（作业在跑），但签不出令牌也不能硬签——
        # 由调用方转成 needs_manual_stop 交给告警。
        return None


# ---------------------------------------------------------------- 失败现场


async def _metric_points(
    session: AsyncSession, *, run_id: int, last_points: int
) -> list[dict[str, Any]]:
    """最后若干条时序点（时间正序，便于前端直接连成折线）。"""
    rows = (
        await session.scalars(
            select(ExecutionMetric)
            .where(ExecutionMetric.run_id == run_id)
            .order_by(ExecutionMetric.ts.desc(), ExecutionMetric.id.desc())
            .limit(last_points)
        )
    ).all()
    return [
        {
            "ts": as_utc(row.ts).isoformat(),
            "read_rows": int(row.read_rows),
            "written_rows": int(row.written_rows),
            "written_bytes": int(row.written_bytes),
            "duration_sec": int(row.duration_sec),
            "reject_rate": float(row.reject_rate),
        }
        for row in reversed(rows)
    ]


async def _error_code_dist(
    session: AsyncSession, *, run_id: int, tenant_id: int
) -> dict[str, int]:
    """错误码分布。**只有码与计数**，没有行原文（红线 §5.7）。

    ``error_record`` 是控制面里的受保护摘要副本（只存 ``payload_digest``），
    所以这里取它就已经是「不含原文」的；仍然回一次 ``execution_run`` 是为了
    让租户过滤落在 SQL 上——那张表自己没有 ``tenant_id`` 列。
    """
    rows = await session.execute(
        select(ErrorRecord.error_code, func.count())
        .join(ExecutionRun, ExecutionRun.id == ErrorRecord.run_id)
        .where(ErrorRecord.run_id == run_id, ExecutionRun.tenant_id == tenant_id)
        .group_by(ErrorRecord.error_code)
    )
    return {str(code): int(count) for code, count in rows.all()}


async def save_failure_snapshot(
    session: AsyncSession,
    *,
    run_id: int,
    reason: DimensionVerdict | None,
    last_points: int = 20,
    needs_manual_stop: bool = False,
    stop_ok: bool = True,
) -> dict[str, Any]:
    """组装中断现场（**已脱敏**），返回可写进 ``execution_run.failure_snapshot`` 的字典。

    本函数**只组装、不落库也不提交**——事务边界归调用方（``interrupt_run``），
    因为「置中断 + 发命令 + 留现场 + 落证据」必须是一次原子写入。
    名字里的 ``save`` 指的是它在整条链里的角色，不表示它自己提交。

    返回的键**只有 §8 白名单里的那些**：多写一个键会把邻模块（be-17/18/19）
    往同一列里放的现场挤掉或混淆，而 ``failure_snapshot`` 是排查时的第一现场。
    """
    run = await _load_run(session, run_id=run_id)
    if run is None:  # pragma: no cover - 调用方刚刚读过同一行
        return {}

    return {
        "reason": "supervision",
        "reason_dimension": reason.dimension.value if reason else "",
        "observed": reason.observed if reason else None,
        "limit": reason.limit if reason else None,
        "ratio_pct": reason.ratio_pct if reason else None,
        "interrupted_at": utcnow().isoformat(),
        "job_id": run.seatunnel_job_id,
        "metric_points": await _metric_points(session, run_id=run_id, last_points=last_points),
        "error_code_dist": await _error_code_dist(
            session, run_id=run_id, tenant_id=run.tenant_id
        ),
        "needs_manual_stop": needs_manual_stop,
        "stop_ok": stop_ok,
    }


__all__ = [
    "METRIC_BUCKET_SEC",
    "collect_run",
    "interrupt_run",
    "save_failure_snapshot",
    "should_emit_warning",
]
