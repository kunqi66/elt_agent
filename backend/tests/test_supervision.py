"""be-16 运行监督与硬中断：判定、采集、告警降频、硬中断。

**这个模块的四句核心断言**，其余用例都服务于它们：

1. **判定是确定性的纯函数**：``engines/supervision`` 的边界语义是 ``>=``，
   同一组数字在任何环境下得出同一个结论。这一层不碰库、不碰网络，
   所以它可以被逐格钉住——硬中断是不可逆动作，它的判据不能靠集成环境验。
2. **读不到 ≠ 没超标**：指标缺失一律判 ``unknown``，绝不落成 0。
   用 0 顶替会让一个指标链路已断的作业看起来像一次正常空跑，而它可能正在灌数据。
3. **停作业只有一条链**：硬中断与人工取消走**同一条**（Capability + Outbox → Worker → broker），
   本模块一行都不碰 SeaTunnel（CLAUDE.md §5.1）。
4. **中断是幂等的、并且抢输不报错**：两个采集周期重叠时，只有一个能置 ``interrupted``
   （条件 UPDATE 的 rowcount 裁决），输的那个静默返回。

分层：``engines.supervision`` 与 ``thresholds`` 纯函数直接调；
``services.supervision`` 与 ``tasks.supervision`` 走真库（内存 SQLite）。
本机没有 Docker（CLAUDE.md §9），所以 seed 用 ORM 直接铺行，不起 SeaTunnel；
指标来源由 ``_reader`` 夹具替换掉（``_metrics_reader`` 是唯一的注入点）。

⚠️ 一处**只在测试里存在**的适配：``ExecutionRun`` 的活跃唯一索引带
``postgresql_where``，SQLite 上退化成「每版本只许一行」；本文件的 ``_quiet``
用例需要同一版本并存两条运行，所以用 ``_allow_multiple_runs`` 删掉它。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from clients.seatunnel.models import JobMetricsDTO
from core.config import settings
from core.errors import AppError, ErrorCode
from core.ids import utcnow
from engines.supervision import (
    DEFAULT_MAX_AMPLIFICATION_RATIO,
    DEFAULT_MAX_DURATION_SEC,
    DEFAULT_MAX_READ_ROWS,
    DEFAULT_MAX_REJECT_RATE,
    DEFAULT_MAX_WRITE_BYTES,
    DEFAULT_WARN_THRESHOLD_PCT,
    DIMENSION_PRIORITY,
    Dimension,
    DimensionVerdict,
    MetricSample,
    Severity,
    SupervisionContractDTO,
    SupervisionVerdict,
    amplification,
    budget_from_settings,
    dimension_verdict,
    evaluate,
    worst,
)
from engines.supervision.thresholds import BOUNDARY_EPSILON_REL
from harness.outbox import Topic
from models.enums import Env, EvidenceEventType, RiskLevel, RunStatus, ToolName
from models.evidence import EvidenceEvent, OutboxMessage
from models.execution import ErrorRecord, ExecutionMetric, ExecutionRun
from models.governance import Capability, Preparation
from models.pipeline import (
    EtlPlan,
    HoconArtifact,
    Pipeline,
    PipelineVersion,
    QualityContract,
    RuntimeSupervisionContract,
)
from models.tenant import AppUser, Project, Tenant
from services import run as run_service
from services import supervision as svc

#: 用例里的**唯一基准时刻**。所有断言都用它，不掺「真实现在」——
#: 按时间过滤的用例两端都夹在同一个基准上，才不会在某一天自己变红（CLAUDE.md §9.1）。
_T0 = datetime(2026, 10, 16, 12, 0, 0, tzinfo=UTC)

FORMAL = "orders_wide"
SHADOW = "orders_wide__shadow_v1"
ERROR = "orders_wide_elt_reject"


# ---------------------------------------------------------------- 夹具


@pytest.fixture(scope="module")
def keypair(tmp_path_factory: pytest.TempPathFactory) -> str:
    """一对 Ed25519 私钥，写在临时目录（本机没有 /run/secrets）。"""
    directory = tmp_path_factory.mktemp("supervision-capability-keys")
    path = directory / "private.pem"
    path.write_bytes(
        ed25519.Ed25519PrivateKey.generate().private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return str(path)


@pytest.fixture(autouse=True)
def _signing_key(keypair: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """把签发器指到临时密钥，并重置 ``services.run._issuer`` 这个进程级单例。

    不重置的话，第一个用例会把临时密钥缓存下来，「换了密钥」在别处就不再生效——
    症状是验签失败，看起来像密钥配错了。
    """
    monkeypatch.setattr(settings, "CAPABILITY_SIGNING_KEY_PATH", keypair)
    monkeypatch.setattr(run_service, "_issuer", None)


@pytest.fixture
def worker_db(
    async_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> async_sessionmaker[AsyncSession]:
    """把 ``core.db.SessionFactory`` 换到内存库，让任务体连的是测试库。"""
    from core import db as core_db

    monkeypatch.setattr(core_db, "SessionFactory", async_session_factory)
    return async_session_factory


def _reader(metrics: JobMetricsDTO | None, seen: list[str] | None = None) -> Callable[[], Any]:
    """假的指标来源工厂（替换 ``services.supervision._metrics_reader``）。

    替换的是**一个函数**而不是往服务里塞分支：如果服务里有一堆
    ``if metrics is None`` 的可测性开关，跑出来的就不是生产那条路径了。
    """

    async def read(job_id: str) -> JobMetricsDTO | None:
        if seen is not None:
            seen.append(job_id)
        return metrics

    return lambda: read


# ---------------------------------------------------------------- 种子


class _World:
    def __init__(self, *, tenant_id: int, project_id: int, version_id: int, user_id: int) -> None:
        self.tenant_id = tenant_id
        self.project_id = project_id
        self.version_id = version_id
        self.user_id = user_id


async def _seed(session: AsyncSession, *, contract: dict[str, Any] | None = None) -> _World:
    """铺一套最小现场：租户 / 用户 / 项目 / 一份冻结版本 / 一张准备单。

    刻意**不铺**数据源与元数据档案：``PipelineVersion.source_metadata_profile_id``
    可空，而监督要的事实（预算契约、准备单的 env 与 artifact_sha256、运行行）
    都在这几行里。铺得越多，本文件的失败原因就越容易和别的模块搅在一起。
    """
    tenant = Tenant(code="acme", name="Acme", status="active")
    session.add(tenant)
    await session.flush()

    user = AppUser(
        tenant_id=tenant.id,
        username="erin",
        display_name="Erin",
        email="erin@acme.test",
        password_hash="x",
        status="active",
    )
    session.add(user)
    await session.flush()

    project = Project(tenant_id=tenant.id, code="orders", name="订单", env=Env.DEV)
    session.add(project)
    await session.flush()

    supervision = RuntimeSupervisionContract(
        max_read_rows=1_000,
        max_write_bytes=1_000,
        max_duration_sec=60,
        max_amplification_ratio=Decimal("1.5"),
        max_reject_rate=Decimal("0.05"),
        warn_threshold_pct=80,
        **(contract or {}),
    )
    plan = EtlPlan(schema_version="1.0", plan={}, valid=True, validation_errors=[])
    hocon = HoconArtifact(content="env {}", sha256="c" * 64, parser_version="1.0", validation_result={})
    quality = QualityContract(rules=[], error_table_target={}, reject_rate_budget=Decimal("0.05"))
    session.add_all([supervision, plan, hocon, quality])
    await session.flush()

    pipeline = Pipeline(
        tenant_id=tenant.id,
        project_id=project.id,
        name="orders_to_dw",
        status="version_frozen",
        created_by=user.id,
    )
    session.add(pipeline)
    await session.flush()

    version = PipelineVersion(
        pipeline_id=pipeline.id,
        version_no=1,
        etl_plan_id=plan.id,
        hocon_artifact_id=hocon.id,
        quality_contract_id=quality.id,
        supervision_contract_id=supervision.id,
        generation_context={},
        sha256="a" * 64,
        frozen_by=user.id,
        frozen_at=datetime(2026, 10, 16, 8, 0, tzinfo=UTC),
        note="",
    )
    session.add(version)
    await session.flush()
    pipeline.current_version_id = version.id

    # 准备单直接铺行：``interrupt_run`` 只要 env 与 artifact_sha256 两项事实，
    # 走审批链只会把本文件与 be-13 的 bug 绑在一起。
    preparation = Preparation(
        tenant_id=tenant.id,
        project_id=project.id,
        pipeline_version_id=version.id,
        input_fingerprint="d" * 64,
        artifact_sha256="a" * 64,
        resource_scope={},
        environment=Env.DEV,
        risk_level=RiskLevel.P2,
        frozen_at=datetime(2026, 10, 16, 9, 0, tzinfo=UTC),
        created_by=user.id,
    )
    session.add(preparation)
    await session.commit()

    return _World(tenant_id=tenant.id, project_id=project.id, version_id=version.id, user_id=user.id)


@pytest.fixture
async def world(async_session: AsyncSession) -> _World:
    return await _seed(async_session)


async def _make_run(
    session: AsyncSession,
    world: _World,
    *,
    status: RunStatus = RunStatus.RUNNING,
    key: str = "k1",
    job_id: str | None = "job-1",
    started_at: datetime | None = _T0 - timedelta(seconds=10),
) -> ExecutionRun:
    preparation_id = int(
        await session.scalar(select(Preparation.id).where(Preparation.tenant_id == world.tenant_id).limit(1))
    )
    run = ExecutionRun(
        tenant_id=world.tenant_id,
        project_id=world.project_id,
        pipeline_version_id=world.version_id,
        preparation_id=preparation_id,
        idempotency_key=key,
        status=status,
        shadow_table=SHADOW,
        formal_table=FORMAL,
        error_table=ERROR,
        seatunnel_job_id=job_id,
        started_at=started_at,
        triggered_by=world.user_id,
    )
    session.add(run)
    await session.commit()
    return run


async def _events(
    session: AsyncSession, *, tenant_id: int, event_type: EvidenceEventType
) -> list[EvidenceEvent]:
    return list(
        (
            await session.scalars(
                select(EvidenceEvent)
                .where(
                    EvidenceEvent.tenant_id == tenant_id,
                    EvidenceEvent.event_type == event_type,
                )
                .order_by(EvidenceEvent.seq.asc())
            )
        ).all()
    )


async def _fresh(session: AsyncSession, run_id: int) -> ExecutionRun:
    """绕过 identity map 重读运行行（``interrupt_run`` 走的是 Core UPDATE）。"""
    session.expire_all()
    row = await session.scalar(select(ExecutionRun).where(ExecutionRun.id == run_id))
    assert row is not None
    return row


async def _allow_multiple_runs(session: AsyncSession) -> None:
    await session.execute(text("DROP INDEX IF EXISTS uq_execution_run_active_per_version"))
    await session.commit()


async def _raise_duration_budget(session: AsyncSession, seconds: int) -> None:
    """把时长预算放宽。

    用例要把「现在」往前推来验证降频间隔，而时长维度看的也是「现在」；
    不放开这一段，第 3 次采集会先撞上时长上限变成硬中断。契约表是不可变表
    （CLAUDE.md §5.4），所以这里走 Core 语句直接改那一行，而不是走 service。
    """
    await session.execute(update(RuntimeSupervisionContract).values(max_duration_sec=seconds))
    await session.commit()


def _metrics(**overrides: Any) -> JobMetricsDTO:
    values: dict[str, Any] = {
        "read_rows": 100,
        "written_rows": 100,
        "written_bytes": 100,
        "reject_rows": 0,
        "duration_sec": 10,
    }
    values.update(overrides)
    return JobMetricsDTO(**values)


def _dim_of(verdict: SupervisionVerdict, dimension: Dimension) -> DimensionVerdict:
    """从整体结论里取出某一维的判定（五维恒齐全，取不到就是用例写错了）。"""
    for item in verdict.dimensions:
        if item.dimension is dimension:
            return item
    raise AssertionError(f"结论里缺少 {dimension} 维")


def _reason(dimension: Dimension, *, observed: float = 10**9, limit: float = 1_000.0) -> DimensionVerdict:
    """构造一个「已超标」的判定。

    给**直接调** ``interrupt_run`` 的用例用：那些用例要验的是中断动作本身
    （签令牌、投命令、写现场），不该顺带把采集环也走一遍。
    """
    return dimension_verdict(dimension=dimension, observed=observed, limit=limit, warn_threshold_pct=80)


# ================================================================ 判定引擎


_CONTRACT = SupervisionContractDTO(
    max_read_rows=1_000,
    max_write_bytes=1_000,
    max_duration_sec=60,
    max_amplification_ratio=1.5,
    max_reject_rate=0.05,
)


@pytest.mark.parametrize(
    ("dimension", "limit"),
    [
        (Dimension.REJECT_RATE, 0.05),
        (Dimension.WRITE_BYTES, 1_000.0),
        (Dimension.AMPLIFICATION, 1.5),
        (Dimension.READ_ROWS, 1_000.0),
        (Dimension.DURATION, 60.0),
    ],
)
@pytest.mark.parametrize(
    ("factor", "expected"),
    [
        (0.79, Severity.OK),
        (0.80, Severity.WARN),
        (0.99, Severity.WARN),
        (1.00, Severity.INTERRUPT),
        (1.01, Severity.INTERRUPT),
    ],
)
def test_边界矩阵_五档乘五维度(dimension: Dimension, limit: float, factor: float, expected: Severity) -> None:
    """``0.8L → warn``、``1.0L → interrupt``（``>=`` 语义）、``0.79L → ok``。

    五档 × 五维度全跑：只测一个维度的话，「某个维度的上限忘了乘 ``warn/100``」
    这类错误会因为那一档恰好不区分而漏掉。
    """
    verdict = dimension_verdict(
        dimension=dimension,
        observed=limit * factor,
        limit=limit,
        warn_threshold_pct=80,
    )
    assert verdict.severity is expected, f"{dimension}@{factor}L 判成了 {verdict.severity}"


def test_warn容差只吃掉浮点噪声() -> None:
    """容差是 ``1e-9`` 量级的相对量：1% 的真实差别必须仍然判得出。"""
    assert BOUNDARY_EPSILON_REL < 1e-6
    low = dimension_verdict(
        dimension=Dimension.REJECT_RATE, observed=0.05 * 79 / 100, limit=0.05, warn_threshold_pct=80
    )
    assert low.severity is Severity.OK


@pytest.mark.parametrize("observed", [None, 0.0, 1.0])
def test_观测值缺失或上限不可用时判unknown(observed: float | None) -> None:
    """``observed=None`` 与 ``limit=None`` / ``limit<=0`` 都只能判 unknown。

    ``limit<=0`` 尤其重要：0 上限的语义是「一有读数就中断」，
    那只会出现在「契约行没建出来、被默认成了 0」，不该被当成一道真的闸。
    """
    assert (
        dimension_verdict(
            dimension=Dimension.READ_ROWS, observed=None, limit=1_000.0, warn_threshold_pct=80
        ).severity
        is Severity.UNKNOWN
    )
    for limit in (None, 0.0, -1.0):
        verdict = dimension_verdict(
            dimension=Dimension.READ_ROWS, observed=observed, limit=limit, warn_threshold_pct=80
        )
        assert verdict.severity is Severity.UNKNOWN
        assert verdict.ratio_pct is None


def test_unknown_不是_ok() -> None:
    """整帧全缺时整体结论必须是 ``unknown``，不是「一切正常」。"""
    verdict = evaluate(MetricSample(), _CONTRACT, None)
    assert verdict.severity is Severity.UNKNOWN
    assert len(verdict.dimensions) == 5
    assert all(item.severity is Severity.UNKNOWN for item in verdict.dimensions)


def test_worst_同severity按维度优先级() -> None:
    """两个维度同为 interrupt 时，取 ``DIMENSION_PRIORITY`` 靠前的那个作 reason。"""

    def item(dimension: Dimension) -> DimensionVerdict:
        return DimensionVerdict(
            dimension=dimension, severity=Severity.INTERRUPT, observed=2.0, limit=1.0, ratio_pct=200.0
        )

    verdict = worst([item(Dimension.READ_ROWS), item(Dimension.REJECT_RATE)])
    assert verdict.reason is not None
    assert verdict.reason.dimension is Dimension.REJECT_RATE
    # 另一个达到 warn 以上的维度仍然进 warnings（前端不会丢信息）
    assert [w.dimension for w in verdict.warnings] == [Dimension.READ_ROWS]


def test_worst_severity优先于维度优先级() -> None:
    """interrupt 优先于 warn——即使 warn 那个维度的优先级更高。

    ``{duration: interrupt, write_bytes: warn}``：duration 在优先级表里垫底，
    但它是唯一硬超限的那个，reason 必须是它。
    """
    verdict = worst(
        [
            DimensionVerdict(
                dimension=Dimension.DURATION,
                severity=Severity.INTERRUPT,
                observed=120.0,
                limit=60.0,
                ratio_pct=200.0,
            ),
            DimensionVerdict(
                dimension=Dimension.WRITE_BYTES,
                severity=Severity.WARN,
                observed=900.0,
                limit=1_000.0,
                ratio_pct=90.0,
            ),
        ]
    )
    assert verdict.severity is Severity.INTERRUPT
    assert verdict.reason is not None
    assert verdict.reason.dimension is Dimension.DURATION
    assert [w.dimension for w in verdict.warnings] == [Dimension.WRITE_BYTES]


def test_维度优先级表覆盖全部维度() -> None:
    """新增一个维度却忘了登记优先级，必须在这里红。

    漏登记的症状是「它的 reason 优先级偏低」（``priority_index`` 兜底排到最后），
    而不是报错——所以要用一条用例把它逼到明面上。
    """
    assert set(DIMENSION_PRIORITY) == set(Dimension)
    assert len(DIMENSION_PRIORITY) == len(Dimension)


def test_默认预算与_settings_一致() -> None:
    """三处默认值（engines 常量 / Settings / 契约表列默认）不许分叉。

    分叉的后果是「本机单测按 A 判、生产按 B 判」，而两边都自洽、都不报错。
    """
    dto = budget_from_settings()
    assert (dto.max_read_rows, dto.max_write_bytes, dto.max_duration_sec) == (
        DEFAULT_MAX_READ_ROWS,
        DEFAULT_MAX_WRITE_BYTES,
        DEFAULT_MAX_DURATION_SEC,
    )
    assert (dto.max_amplification_ratio, dto.max_reject_rate, dto.warn_threshold_pct) == (
        DEFAULT_MAX_AMPLIFICATION_RATIO,
        DEFAULT_MAX_REJECT_RATE,
        DEFAULT_WARN_THRESHOLD_PCT,
    )
    assert dto.max_read_rows == settings.SUP_MAX_READ_ROWS
    assert dto.max_write_bytes == settings.SUP_MAX_WRITE_BYTES
    assert dto.max_duration_sec == settings.SUP_MAX_DURATION_SEC
    assert dto.max_amplification_ratio == settings.SUP_MAX_AMPLIFICATION_RATIO
    assert dto.max_reject_rate == settings.SUP_MAX_REJECT_RATE
    assert dto.warn_threshold_pct == settings.SUP_WARN_THRESHOLD_PCT


def test_时长用控制面自己算的_elapsed_sec() -> None:
    """数据面自报的 ``duration_sec`` 不参与判定。

    作业卡住时它自报的时长也是 0，而硬中断要拦的恰恰是「卡住了」——
    信它等于在最该报警的时刻收到一个「一切正常」。
    """
    sample = MetricSample(
        read_rows=1, written_rows=1, written_bytes=1, reject_rows=0, reject_rate=0.0, duration_sec=0
    )
    ok = evaluate(sample, _CONTRACT, 10.0)
    assert ok.reason is not None
    # 只看时长这一维：采样里的 duration_sec 恒为 0，若判定误用了它，
    # 下面「跑了 120 s」那条断言会照样通过——它才是这条用例要抓的东西。
    assert _dim_of(ok, Dimension.DURATION).severity is Severity.OK
    assert ok.severity is not Severity.INTERRUPT
    stuck = evaluate(sample, _CONTRACT, 120.0)
    assert stuck.severity is Severity.INTERRUPT
    assert stuck.reason is not None
    assert stuck.reason.dimension is Dimension.DURATION


def test_拒绝率按_拒绝除总数_算() -> None:
    """``reject_rate`` 由采样自带；引擎不重算，但 ratio 口径要在服务侧定死。"""
    sample = MetricSample(
        read_rows=1_000, written_rows=900, written_bytes=100, reject_rows=100, reject_rate=0.1
    )
    verdict = evaluate(sample, _CONTRACT, 10.0)
    reject = next(item for item in verdict.dimensions if item.dimension is Dimension.REJECT_RATE)
    assert reject.severity is Severity.INTERRUPT
    assert reject.observed == pytest.approx(0.1)


@pytest.mark.parametrize(("read_bytes", "written_bytes"), [(None, 100), (0, 100)])
def test_放大比读不到时判unknown(read_bytes: int | None, written_bytes: int) -> None:
    """读入字节缺失（或为 0）→ 放大比 unknown。

    为 0 时**不能**当成「无穷大」：读 0 写 N 在真实作业里只可能是读侧没报数，
    把它算成一个天文数字会立刻误判硬中断。
    """
    sample = MetricSample(read_rows=1, written_rows=1, written_bytes=written_bytes, read_bytes=read_bytes)
    assert amplification(sample) is None
    verdict = evaluate(sample, _CONTRACT, 10.0)
    dim = next(item for item in verdict.dimensions if item.dimension is Dimension.AMPLIFICATION)
    assert dim.severity is Severity.UNKNOWN


def test_放大比正常时参与判定() -> None:
    sample = MetricSample(read_bytes=100, written_bytes=300)
    assert amplification(sample) == pytest.approx(3.0)
    verdict = evaluate(sample, _CONTRACT, 10.0)
    dim = next(item for item in verdict.dimensions if item.dimension is Dimension.AMPLIFICATION)
    assert dim.severity is Severity.INTERRUPT


# ================================================================ 采集


async def test_collect_run_写时序点并更新汇总(
    async_session: AsyncSession, world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """正常一轮：时序点落库 + 汇总列前进 + 状态不动。"""
    run = await _make_run(async_session, world)
    monkeypatch.setattr(svc, "_metrics_reader", _reader(_metrics(read_rows=500, written_rows=400)))

    result = await svc.collect_run(async_session, run_id=run.id, now=_T0)

    assert result.collected and not result.warned and not result.interrupted
    # 整体水位是 unknown 而不是 ok：放大比这一维恒不可判（SeaTunnel 的指标里
    # 没有读字节数），而 §6.2 的排序是 interrupt > warn > unknown > ok。
    # 这是设计结果，不是缺陷——它的意义是「有一维我看不见」，而告警闸只看
    # warn 及以上，所以不会因此报假警。
    assert result.severity is Severity.UNKNOWN
    # 但 ``reason_dimension`` 是 None：整体水位 unknown 不等于「有告警」，
    # 这一栏只在真的告警/中断时才填（`_warning_dimension`）。
    assert result.reason_dimension is None
    points = list(
        (await async_session.scalars(select(ExecutionMetric).where(ExecutionMetric.run_id == run.id))).all()
    )
    assert len(points) == 1
    assert points[0].read_rows == 500
    assert points[0].written_rows == 400
    # 汇总列是**覆盖**语义（累计值快照），不是累加
    assert (await _fresh(async_session, run.id)).read_rows == 500
    assert (await _fresh(async_session, run.id)).status is RunStatus.RUNNING
    assert (await _fresh(async_session, run.id)).avg_throughput > 0


async def test_collect_run_同一格重复采集只留一行(
    async_session: AsyncSession, world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """重投（``task_acks_late``）是常态：同一 30 s 格里的第二次采集是覆盖，不是新增。"""
    run = await _make_run(async_session, world)
    monkeypatch.setattr(svc, "_metrics_reader", _reader(_metrics(read_rows=100)))
    await svc.collect_run(async_session, run_id=run.id, now=_T0)
    monkeypatch.setattr(svc, "_metrics_reader", _reader(_metrics(read_rows=700)))
    await svc.collect_run(async_session, run_id=run.id, now=_T0 + timedelta(seconds=3))

    count = await async_session.scalar(
        select(func.count()).select_from(ExecutionMetric).where(ExecutionMetric.run_id == run.id)
    )
    assert count == 1
    assert (await _fresh(async_session, run.id)).read_rows == 700


async def test_collect_run_跨格写新行(
    async_session: AsyncSession, world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = await _make_run(async_session, world)
    monkeypatch.setattr(svc, "_metrics_reader", _reader(_metrics()))
    await svc.collect_run(async_session, run_id=run.id, now=_T0)
    await svc.collect_run(async_session, run_id=run.id, now=_T0 + timedelta(seconds=31))

    count = await async_session.scalar(
        select(func.count()).select_from(ExecutionMetric).where(ExecutionMetric.run_id == run.id)
    )
    assert count == 2


async def test_collect_run_不碰非running的运行(
    async_session: AsyncSession, world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """终态的运行不采（清单是先查后采的，中途结束是常态，不是失败）。"""
    run = await _make_run(async_session, world, status=RunStatus.SUCCEEDED, key="k-done")
    called: list[str] = []
    monkeypatch.setattr(svc, "_metrics_reader", _reader(_metrics(), called))

    result = await svc.collect_run(async_session, run_id=run.id, now=_T0)

    assert result.collected is False
    assert result.status.startswith("not_running")
    assert called == [], "终态的运行不该去读数据面指标"


async def test_collect_run_运行不存在(async_session: AsyncSession, world: _World) -> None:
    result = await svc.collect_run(async_session, run_id=999_999, now=_T0)
    assert result.collected is False
    assert result.status == "not_found"


async def test_collect_run_契约缺失是INTERNAL_ERROR(
    async_session: AsyncSession, world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """预算契约指向的行不存在 → ``INTERNAL_ERROR``，**不是**「没超标」。

    没有预算就无从判定，而「无从判定」绝不能等同于「放行」——
    那等于在一个没有刹车的作业上松手。必须抛出去让人看见（任务层逐条兜住）。
    """
    run = await _make_run(async_session, world)
    monkeypatch.setattr(svc, "_metrics_reader", _reader(_metrics()))
    await async_session.execute(
        update(PipelineVersion)
        .where(PipelineVersion.id == world.version_id)
        .values(supervision_contract_id=999_999)
    )
    await async_session.commit()

    with pytest.raises(AppError) as caught:
        await svc.collect_run(async_session, run_id=run.id, now=_T0)
    assert caught.value.code == ErrorCode.INTERNAL_ERROR


async def test_指标读不到走退化路径并告警(
    async_session: AsyncSession, world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """数据面指标全断 → 四个数据面维度 unknown → 一条 ``degraded`` 告警。

    这条路径最值得守：指标链路断了，监督**其实没在监督**，
    而它恰好是「什么都不报」的样子（所有维度 unknown，没有一处超标）。
    """
    run = await _make_run(async_session, world)
    monkeypatch.setattr(svc, "_metrics_reader", _reader(None))

    result = await svc.collect_run(async_session, run_id=run.id, now=_T0)

    assert result.collected is True
    assert result.degraded is True
    assert result.warned is True
    assert result.reason_dimension == "degraded"
    events = await _events(
        async_session, tenant_id=world.tenant_id, event_type=EvidenceEventType.SUPERVISION_WARNING
    )
    assert len(events) == 1
    assert events[0].payload["dimension"] == "degraded"
    assert events[0].subject_type == "run"
    assert events[0].subject_id == str(run.id)


async def test_指标读取抛异常也只退化成unknown(
    async_session: AsyncSession, world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """只读失败**不能把监督环带走**：异常也要被吞成 ``None``。"""

    async def boom(job_id: str) -> JobMetricsDTO | None:
        raise RuntimeError("socket exploded")

    monkeypatch.setattr(svc, "_metrics_reader", lambda: boom)
    run = await _make_run(async_session, world)

    result = await svc.collect_run(async_session, run_id=run.id, now=_T0)
    assert result.degraded is True


async def test_没有job_id时不读指标(
    async_session: AsyncSession, world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """作业还没提交到 Zeta（``seatunnel_job_id`` 为空）时不去读指标。

    这时读回来的只能是「查无此 job」，把它当成读数会让所有维度变成 0/未知，
    而真正的原因（作业还没起来）被掩盖成「指标链路坏了」。
    """
    run = await _make_run(async_session, world, job_id=None)
    called: list[str] = []
    monkeypatch.setattr(svc, "_metrics_reader", _reader(_metrics(), called))

    result = await svc.collect_run(async_session, run_id=run.id, now=_T0)

    assert called == []
    assert result.degraded is True
    # 时长维度仍然判得出（控制面自己计时）
    verdict = evaluate(MetricSample(), _CONTRACT, 10.0)
    assert verdict.severity is Severity.UNKNOWN


# ================================================================ 告警降频


async def test_告警降频_三档(
    async_session: AsyncSession, world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """①首次写 ②同维度同周期压掉 ③过了间隔再写 ④换维度立刻写。"""
    run = await _make_run(async_session, world)
    # 这条用例要连推 301 s 的「现在」来验证降频间隔；而 duration 维度正好也看
    # 「现在」，不把时长预算放宽的话，第 3 次采集会变成**硬中断**而不是告警。
    await _raise_duration_budget(async_session, 10**6)
    # 读 900 / 上限 1000 = 90% ≥ 80% → warn
    monkeypatch.setattr(svc, "_metrics_reader", _reader(_metrics(read_rows=900)))

    first = await svc.collect_run(async_session, run_id=run.id, now=_T0)
    assert first.warned is True

    # 同一维度、间隔不够：压掉
    second = await svc.collect_run(async_session, run_id=run.id, now=_T0 + timedelta(seconds=60))
    assert second.warned is False

    # 间隔够了：再写一条
    third = await svc.collect_run(async_session, run_id=run.id, now=_T0 + timedelta(seconds=301))
    assert third.warned is True

    events = await _events(
        async_session, tenant_id=world.tenant_id, event_type=EvidenceEventType.SUPERVISION_WARNING
    )
    assert len(events) == 2
    assert {event.payload["dimension"] for event in events} == {"read_rows"}


async def test_告警降频_换维度立刻写(
    async_session: AsyncSession, world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """换了病就要再说一次：间隔没到也写。"""
    run = await _make_run(async_session, world)
    monkeypatch.setattr(svc, "_metrics_reader", _reader(_metrics(read_rows=900)))
    assert (await svc.collect_run(async_session, run_id=run.id, now=_T0)).warned is True

    monkeypatch.setattr(svc, "_metrics_reader", _reader(_metrics(read_rows=100, written_bytes=900)))
    second = await svc.collect_run(async_session, run_id=run.id, now=_T0 + timedelta(seconds=10))
    assert second.warned is True
    assert second.reason_dimension == "write_bytes"

    events = await _events(
        async_session, tenant_id=world.tenant_id, event_type=EvidenceEventType.SUPERVISION_WARNING
    )
    assert [event.payload["dimension"] for event in events] == ["read_rows", "write_bytes"]


async def test_should_emit_warning_无历史为真(async_session: AsyncSession, world: _World) -> None:
    run = await _make_run(async_session, world)
    assert await svc.should_emit_warning(async_session, run_id=run.id, dimension="read_rows", now=_T0)


async def test_告警payload不带float(
    async_session: AsyncSession, world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """账本 payload 不许出现 float（``harness/ledger.py`` 的硬要求）。

    平台相关的 ``repr`` 会让同一语义在两台机器上算出不同哈希，
    而账本是靠哈希链自证的——写成 float 的后果不是报错，是**验链在另一台机器上断**。
    """
    run = await _make_run(async_session, world)
    monkeypatch.setattr(svc, "_metrics_reader", _reader(_metrics(read_rows=900)))
    await svc.collect_run(async_session, run_id=run.id, now=_T0)

    events = await _events(
        async_session, tenant_id=world.tenant_id, event_type=EvidenceEventType.SUPERVISION_WARNING
    )
    payload = events[0].payload
    for key in ("observed", "limit", "ratio_pct"):
        assert isinstance(payload[key], str) and payload[key] != ""
    assert isinstance(payload["also_warned"], list)


# ================================================================ 硬中断


async def _interrupt_setup(
    session: AsyncSession, world: _World, monkeypatch: pytest.MonkeyPatch, **metrics_overrides: Any
) -> ExecutionRun:
    """让下一轮采集必定超限（read_rows 1e9 / 上限 1e3）。"""
    await _allow_multiple_runs(session)
    run = await _make_run(session, world, key="k-int")
    monkeypatch.setattr(svc, "_metrics_reader", _reader(_metrics(read_rows=10**9, **metrics_overrides)))
    return run


async def test_硬中断_端到端(
    async_session: AsyncSession, world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """超硬阈值 → 状态置 interrupted + 停止命令进 Outbox + 现场 + 两条证据。"""
    run = await _interrupt_setup(async_session, world, monkeypatch)

    result = await svc.collect_run(async_session, run_id=run.id, now=_T0)

    assert result.interrupted is True
    assert result.reason_dimension == "read_rows"

    fresh = await _fresh(async_session, run.id)
    assert fresh.status is RunStatus.INTERRUPTED
    assert fresh.finished_at is not None
    assert fresh.error_message and "supervision" in fresh.error_message

    snapshot = fresh.failure_snapshot
    assert snapshot["reason"] == "supervision"
    assert snapshot["reason_dimension"] == "read_rows"
    assert snapshot["limit"] == pytest.approx(1000.0)
    assert snapshot["observed"] == pytest.approx(1e9)
    assert snapshot["job_id"] == "job-1"
    assert snapshot["needs_manual_stop"] is False
    assert snapshot["stop_ok"] is True
    assert len(snapshot["metric_points"]) == 1
    assert snapshot["interrupted_at"]

    # 停止命令：与人工取消同一条链（Capability + Outbox）
    outbox = list(
        (
            await async_session.scalars(
                select(OutboxMessage).where(OutboxMessage.topic == Topic.RUN_STOP.value)
            )
        ).all()
    )
    assert len(outbox) == 1
    assert outbox[0].payload["reason"] == "supervision_interrupt"
    assert outbox[0].idempotency_key == f"supervision-stop:run:{run.id}"
    assert isinstance(outbox[0].payload["capability"], str)

    capabilities = list((await async_session.scalars(select(Capability))).all())
    assert len(capabilities) == 1
    assert capabilities[0].tool == ToolName.SEATUNNEL_STOP.value
    # 硬中断同样要把**执行主体**随载荷带上，且与令牌里签的是同一个：
    # worker 的 broker 在 BINDING 阶段拒收 ``ctx.actor_id is None``——
    # 少了它，明明判了超限却停不下作业，是这条链最坏的形状。
    assert outbox[0].payload["subject_id"] == capabilities[0].subject_id
    assert outbox[0].payload["subject_id"] == run.triggered_by

    interrupted = await _events(
        async_session, tenant_id=world.tenant_id, event_type=EvidenceEventType.RUN_INTERRUPTED
    )
    assert len(interrupted) == 1
    issued = await _events(
        async_session, tenant_id=world.tenant_id, event_type=EvidenceEventType.CAPABILITY_ISSUED
    )
    assert len(issued) == 1


async def test_硬中断_令牌不进快照与日志(
    async_session: AsyncSession, world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """令牌原文只允许落在 Outbox 载荷里（CLAUDE.md §5.2）。

    ``failure_snapshot`` 会进详情页、进诊断、进证据，一旦把令牌写进去，
    「一次性授权」就变成「任何能看到详情的人都能拿到的一张凭证」。
    """
    run = await _interrupt_setup(async_session, world, monkeypatch)
    await svc.collect_run(async_session, run_id=run.id, now=_T0)

    row = await async_session.scalar(select(OutboxMessage).where(OutboxMessage.topic == Topic.RUN_STOP.value))
    assert row is not None
    token = row.payload["capability"]
    assert isinstance(token, str)
    fresh = await _fresh(async_session, run.id)
    assert token not in str(fresh.failure_snapshot)
    assert token not in (fresh.error_message or "")


async def test_硬中断_幂等_重复采集不再是running(
    async_session: AsyncSession, world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """第二次采集看到的是 ``interrupted``：跳过，不重复签发、不重复投递。"""
    run = await _interrupt_setup(async_session, world, monkeypatch)
    await svc.collect_run(async_session, run_id=run.id, now=_T0)
    monkeypatch.setattr(svc, "_metrics_reader", _reader(_metrics(read_rows=10**9)))

    again = await svc.collect_run(async_session, run_id=run.id, now=_T0 + timedelta(seconds=30))

    assert again.interrupted is False
    assert again.status.startswith("not_running")
    outbox = list((await async_session.scalars(select(OutboxMessage))).all())
    assert len(outbox) == 1
    assert len(list((await async_session.scalars(select(Capability))).all())) == 1


async def test_硬中断_并发抢输时静默返回(
    async_session: AsyncSession, world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """两个采集周期重叠时，只有一个能置 ``interrupted``。

    输的那个**不抛异常**：抛出去会让 Outbox 把一个不可能成功的命令重投到死信，
    而死信队列里全是假警报，真警报就没人看了。
    """
    run = await _interrupt_setup(async_session, world, monkeypatch)
    # 抢先把它带离 running（模拟人工取消已经赢了）
    await async_session.execute(
        update(ExecutionRun)
        .where(ExecutionRun.id == run.id)
        .values(status=RunStatus.CANCELLED, finished_at=_T0)
    )
    await async_session.commit()

    reason = DimensionVerdict(
        dimension=Dimension.READ_ROWS,
        severity=Severity.INTERRUPT,
        observed=1e9,
        limit=1000.0,
        ratio_pct=1e8,
    )
    assert await svc.interrupt_run(async_session, run_id=run.id, reason=reason) is False

    assert (await _fresh(async_session, run.id)).status is RunStatus.CANCELLED
    assert list((await async_session.scalars(select(OutboxMessage))).all()) == []
    assert list((await async_session.scalars(select(Capability))).all()) == []


async def test_硬中断_没有作业时不发停止命令(async_session: AsyncSession, world: _World) -> None:
    """``seatunnel_job_id`` 为空：没有要停的东西。

    这条路径**只能**直接调 ``interrupt_run``——没有 job_id 就读不到指标，
    读不到指标就永远超不了限，所以采集环自己走不到这里。但 ``interrupt_run``
    是公开入口（设计 §3.2），必须能处理「作业从未真正起来」这个形状。

    这时 ``needs_manual_stop`` **必须**是 False——否则每一次「提交就失败」
    都会在告警里挂一条「需要人工停作业」的待办，而那个作业根本不存在。
    """
    await _allow_multiple_runs(async_session)
    run = await _make_run(async_session, world, job_id=None, key="k-nojob")

    intervened = await svc.interrupt_run(async_session, run_id=run.id, reason=_reason(Dimension.READ_ROWS))

    assert intervened is True
    fresh = await _fresh(async_session, run.id)
    assert fresh.failure_snapshot["stop_ok"] is True
    assert fresh.failure_snapshot["needs_manual_stop"] is False
    assert list((await async_session.scalars(select(OutboxMessage))).all()) == []
    assert list((await async_session.scalars(select(Capability))).all()) == []


async def test_硬中断_签不出令牌时标记需要人工停(
    async_session: AsyncSession, world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """没有操作主体（``triggered_by`` 为空）→ 停止命令发不出去，如实标记。

    这条路径的价值在「不撒谎」：库里的状态已经是 ``interrupted`` 了，
    而作业大概率还在跑。``needs_manual_stop=True`` 是唯一能让人发现这件事的地方。
    """
    await _allow_multiple_runs(async_session)
    run = await _make_run(async_session, world, key="k-nosubject")
    await async_session.execute(
        update(ExecutionRun).where(ExecutionRun.id == run.id).values(triggered_by=None)
    )
    await async_session.commit()
    monkeypatch.setattr(svc, "_metrics_reader", _reader(_metrics(read_rows=10**9)))

    await svc.collect_run(async_session, run_id=run.id, now=_T0)

    fresh = await _fresh(async_session, run.id)
    assert fresh.status is RunStatus.INTERRUPTED
    assert fresh.failure_snapshot["needs_manual_stop"] is True
    assert fresh.failure_snapshot["stop_ok"] is False
    assert list((await async_session.scalars(select(OutboxMessage))).all()) == []


async def test_硬中断_快照只更新自己的键(
    async_session: AsyncSession, world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``quality`` / ``swap`` / ``diagnosis`` 三个子键归 be-17/18/19，不许被抹掉。"""
    run = await _interrupt_setup(async_session, world, monkeypatch)
    await async_session.execute(
        update(ExecutionRun)
        .where(ExecutionRun.id == run.id)
        .values(failure_snapshot={"quality": {"passed": False}, "swap": {"state": "pending"}})
    )
    await async_session.commit()

    await svc.collect_run(async_session, run_id=run.id, now=_T0)

    snapshot = (await _fresh(async_session, run.id)).failure_snapshot
    assert snapshot["quality"] == {"passed": False}
    assert snapshot["swap"] == {"state": "pending"}
    assert snapshot["reason"] == "supervision"


async def test_硬中断_快照带错误码分布与指标点(
    async_session: AsyncSession, world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """现场要能回答「拒绝的都是哪一类」——只有码与计数，没有行原文。"""
    run = await _interrupt_setup(async_session, world, monkeypatch)
    async_session.add_all(
        [
            ErrorRecord(
                run_id=run.id,
                rule_id="r1",
                error_code="E_NULL_PK",
                field_name="id",
                payload_digest="f" * 64,
                reason="主键为空",
                occurred_at=_T0,
            )
            for _ in range(3)
        ]
        + [
            ErrorRecord(
                run_id=run.id,
                rule_id="r2",
                error_code="E_TYPE_CAST",
                field_name="amount",
                payload_digest="e" * 64,
                reason="金额无法解析",
                occurred_at=_T0,
            )
        ]
    )
    await async_session.commit()

    await svc.collect_run(async_session, run_id=run.id, now=_T0)

    snapshot = (await _fresh(async_session, run.id)).failure_snapshot
    assert snapshot["error_code_dist"] == {"E_NULL_PK": 3, "E_TYPE_CAST": 1}
    assert snapshot["metric_points"][0]["read_rows"] == 10**9


async def test_中断证据payload不带float(
    async_session: AsyncSession, world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = await _interrupt_setup(async_session, world, monkeypatch)
    await svc.collect_run(async_session, run_id=run.id, now=_T0)

    events = await _events(
        async_session, tenant_id=world.tenant_id, event_type=EvidenceEventType.RUN_INTERRUPTED
    )
    payload = events[0].payload
    assert payload["dimension"] == "read_rows"
    for key in ("observed", "limit"):
        assert isinstance(payload[key], str)
    assert payload["needs_manual_stop"] is False


async def test_中断只发一条停止命令(
    async_session: AsyncSession, world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """重复调 ``interrupt_run`` 不再签发第二张令牌。"""
    run = await _interrupt_setup(async_session, world, monkeypatch)
    reason = DimensionVerdict(
        dimension=Dimension.READ_ROWS,
        severity=Severity.INTERRUPT,
        observed=1e9,
        limit=1000.0,
        ratio_pct=1e8,
    )
    assert await svc.interrupt_run(async_session, run_id=run.id, reason=reason) is True
    assert await svc.interrupt_run(async_session, run_id=run.id, reason=reason) is False
    assert len(list((await async_session.scalars(select(OutboxMessage))).all())) == 1


async def test_硬中断_放大比这一维真的能开火(
    async_session: AsyncSession, world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """放大比（写字节 / 读字节）超限 → 硬中断，且 ``reason_dimension`` 是它。

    这条用来看守一个**曾经不可能发生**的中断：``JobMetricsDTO`` 起初不带读字节，
    ``services/supervision._sample`` 里就写死了 ``read_bytes=None``，
    于是这一维**从落地起恒判 unknown**——阈值配着、用例绿着、永远不会开火。
    而「笛卡尔积 / join 写错」这类逻辑错误不涨行数（读 100 行还是 100 行）、
    不涨拒绝率（数据本身合法），只涨**写出去的字节量**，正是它唯一拦得住的东西。

    参数取法：其余四维全部压在 80% 警戒线以下（读 100 行 / 上限 1000，
    写 500 字节 / 上限 1000，拒绝率 0，时长 10 s / 上限 60），
    只有放大比 500/100 = 5.0 越过了上限 1.5——这样 ``reason_dimension``
    只可能是它，用例不会因为别的维度先越线而变成假绿。
    """
    await _allow_multiple_runs(async_session)
    run = await _make_run(async_session, world, key="k-amp")
    monkeypatch.setattr(
        svc,
        "_metrics_reader",
        _reader(_metrics(read_rows=100, written_bytes=500, read_bytes=100)),
    )

    result = await svc.collect_run(async_session, run_id=run.id, now=_T0)

    assert result.interrupted is True
    assert result.reason_dimension == "amplification_ratio"

    fresh = await _fresh(async_session, run.id)
    assert fresh.status is RunStatus.INTERRUPTED
    assert fresh.failure_snapshot["reason_dimension"] == "amplification_ratio"


async def test_硬中断_读字节缺失时放大比判未知而不是无穷大(
    async_session: AsyncSession, world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同一组指标、只把读字节抽掉 → **不中断**，放大比判 ``unknown``。

    这是上一条的**反面对照**，也是本次改动的风险点所在：``JobMetricsDTO`` 用
    ``0`` 表示「这个计数器没报」，如果服务侧不把 ``0`` 翻成 ``None``，
    ``written_bytes / 0`` 会算成「写了很多、读进来零」的无穷大——
    那是一次**凭空的硬中断**，比漏报更糟（正常作业被无辜杀掉，且原因写着放大比异常）。
    """
    await _allow_multiple_runs(async_session)
    run = await _make_run(async_session, world, key="k-amp-none")
    monkeypatch.setattr(svc, "_metrics_reader", _reader(_metrics(read_rows=100, written_bytes=500)))

    result = await svc.collect_run(async_session, run_id=run.id, now=_T0)

    assert result.interrupted is False
    fresh = await _fresh(async_session, run.id)
    assert fresh.status is not RunStatus.INTERRUPTED


# ================================================================ 任务层


async def test_采集批次扫到running的run(
    async_session: AsyncSession,
    world: _World,
    monkeypatch: pytest.MonkeyPatch,
    worker_db: async_sessionmaker[AsyncSession],
) -> None:
    """beat 那条入口：挑出 ``running``（跨租户）、逐个采集、汇总计数。"""
    from tasks import supervision as task

    await _allow_multiple_runs(async_session)
    # 这条路径经 ``_supervise_one`` → 服务里的 ``utcnow()``，起点要贴着真实时钟
    await _make_run(async_session, world, key="k-batch", started_at=utcnow() - timedelta(seconds=10))
    await _make_run(async_session, world, status=RunStatus.SUCCEEDED, key="k-done2")
    monkeypatch.setattr(svc, "_metrics_reader", _reader(_metrics(read_rows=900)))

    tally = await task._collect_batch(20)

    assert tally["active"] == 1
    assert tally["collected"] == 1
    assert tally["warned"] == 1
    assert tally["interrupted"] == 0
    assert tally["failed"] == 0


async def test_采集批次_单个失败不带垮整批(
    async_session: AsyncSession,
    world: _World,
    monkeypatch: pytest.MonkeyPatch,
    worker_db: async_sessionmaker[AsyncSession],
) -> None:
    """一条 run 的数据完整性问题（契约缺失）不能让整批无人监督。"""
    from tasks import supervision as task

    await _allow_multiple_runs(async_session)
    good = await _make_run(async_session, world, key="k-good")
    bad = await _make_run(async_session, world, key="k-bad")
    monkeypatch.setattr(svc, "_metrics_reader", _reader(_metrics()))
    # 让 bad 那条没有预算契约（按运行行逐条区分，而不是全局地破坏版本）
    original = svc._load_contract

    async def picky(session: AsyncSession, *, run: ExecutionRun) -> Any:
        if run.id == bad.id:
            return None
        return await original(session, run=run)

    monkeypatch.setattr(svc, "_load_contract", picky)

    tally = await task._collect_batch(20)

    assert tally["active"] == 2
    assert tally["failed"] == 1
    assert tally["collected"] == 1
    assert good.id != bad.id


async def test_supervise_run_任务体返回可序列化结论(
    async_session: AsyncSession,
    world: _World,
    monkeypatch: pytest.MonkeyPatch,
    worker_db: async_sessionmaker[AsyncSession],
) -> None:
    """返回体要进 Redis result backend，必须是纯 JSON——枚举一律转字符串。"""
    from tasks import supervision as task

    # 这条路径真的走 ``_supervise_one`` → 服务里的 ``utcnow()``（真实的「现在」）。
    # 若沿用固定基准 _T0，时长维度会算出几小时而直接硬中断——用例就变成
    # 「取决于跑测试的钟点」了（CLAUDE.md §9.1 那条时间窗教训）。
    run = await _make_run(async_session, world, key="k-task", started_at=utcnow() - timedelta(seconds=10))
    monkeypatch.setattr(svc, "_metrics_reader", _reader(_metrics(read_rows=900)))

    payload = await task._supervise_one(run.id)

    assert payload["run_id"] == run.id
    assert payload["severity"] == "warn"
    assert payload["reason_dimension"] == "read_rows"
    assert payload["warned"] is True
    assert isinstance(payload["collected"], bool)


async def test_活跃清单只含running(
    async_session: AsyncSession,
    world: _World,
    worker_db: async_sessionmaker[AsyncSession],
) -> None:
    from tasks import supervision as task

    await _allow_multiple_runs(async_session)
    running = await _make_run(async_session, world, key="k-r")
    await _make_run(async_session, world, status=RunStatus.QUEUED, key="k-q")
    await _make_run(async_session, world, status=RunStatus.CANCELLED, key="k-c")

    ids = await task._active_run_ids(20)

    assert ids == [running.id]


def test_任务名与签名是既有契约() -> None:
    """任务名写在 ``celery_app.py`` 的 beat 表里，改了就是 worker 每 30 s 报一次未注册。"""
    from tasks import supervision as task

    assert task.collect_active_runs.name == "tasks.supervision.collect_active_runs"
    assert task.supervise_run.name == "tasks.supervision.supervise_run"
    assert task.supervise_run.run.__code__.co_varnames[:2] == ("self", "run_id")
    assert task.collect_active_runs.run.__code__.co_varnames[:1] == ("self",)


def test_任务层不用asyncio_run() -> None:
    """``asyncio.run`` 会让第 2 个任务必失败（模块级单例循环被关掉）。

    扫源码而不是靠自觉：这个坑的表现是「偶发 Event loop is closed」，
    单测很难复现，而线上是「worker 偶尔不干活」（CLAUDE.md §9.1）。
    """
    import pathlib

    source = (pathlib.Path(__file__).resolve().parent.parent / "tasks" / "supervision.py").read_text(
        encoding="utf-8"
    )
    assert "asyncio.run(" not in source
    assert "run_coro(" in source


def test_服务层不含第二处阈值判断() -> None:
    """阈值比大小只允许出现在 ``engines/supervision``。

    两处判断迟早不一致，而「哪里判错了」会变得无从定位。
    这里扫的是可疑表达式（``observed >=`` / ``> limit`` 之类），不是关键字。
    """
    import ast
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent

    def comparisons(relative: str) -> list[str]:
        tree = ast.parse((root / relative).read_text(encoding="utf-8"))
        return [
            ast.unparse(node)
            for node in ast.walk(tree)
            if isinstance(node, ast.Compare) and _thresholdy(node)
        ]

    def _thresholdy(node: ast.Compare) -> bool:
        # 只看**操作数里带 limit / observed** 的比较——纯词扫描会被注释与文档字符串
        # 骗过（本模块 docstring 里就写着 ``if observed > limit`` 这句反例）。
        text = ast.unparse(node)
        return "limit" in text or "observed" in text

    assert comparisons("services/supervision.py") == []
    # 反向验证这条检查不是永远通过的：引擎层必须有这种比较
    assert comparisons("engines/supervision/evaluate.py")
