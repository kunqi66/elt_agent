"""质量报告合成与错误分流的编排（be-17 §3.2 / §5 / §7）。

本模块是质量结论的**唯一入口**：be-18 的发布链路（``services/publish.py``）
在 Swap 之前读它给出的 ``passed``；``harness.ToolBroker._check_quality`` 会再独立核一遍
库里的那一行——两道闸看的是同一张 ``quality_report``，所以「报告是谁写的」必须是唯一的一处。

五条纪律：

- **租户边界靠回连**：``quality_report`` / ``error_record`` 都**没有** ``tenant_id`` 列
  （它们只外键到 ``execution_run``），因此每次读写都必须 join 回 ``execution_run``
  过滤租户——与 ``harness/broker.py::_check_quality`` 同一手法。少这个 join 就是
  「拿别人的 run_id 读别人的质量报告」，而接口不会报任何错。
- **不可变**：``quality_report`` / ``error_record`` 只 INSERT（CLAUDE.md §5.4）。
  重投的任务走幂等分支返回**同一份**报告——be-18 的 ``quality_report_id`` 会进 Capability，
  换一个 id 就说不清那次发布依据的到底是哪份结论。
- **不搬运业务数据**：违规行原文只在数据面。``error_record`` 拷的是目标端算好的
  ``payload_digest`` 与 ``reason``（规则模板 + 字段名），本模块**不重算摘要**，也不碰任何原始值。
- **不投递 Swap**：``passed=True`` 之后的发布归 be-18（``services/publish.publish_run``）。
  本模块也投递的话，同一次发布就有两个入口，两个都「看起来对」，而结果是 Swap 跑两遍。
- **比率**：对外是 ``float``（前端契约是 ``number``），对内一律 ``Decimal``——
  边界判断（``reject_rate <= budget``）必须两边同定标，float 上偶尔会判反。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any, Final, NamedTuple

from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.errors import ConflictError, NotFoundError, ValidationError
from core.ids import as_utc, utcnow
from core.logging import get_logger
from engines.plan.schema import EtlPlanModel
from engines.quality import (
    CompiledRule,
    ContractError,
    ErrorSample,
    ErrorTableReader,
    QualityContractSpec,
    QualityDiagnostics,
    QualityDiagnosticSample,
    QualityReportDTO,
    QualityRuleSpec,
    budget_used_ratio_of,
    build_diagnostic_items,
    build_report,
    compile_contract,
    contract_sha256,
    digest_content_length,
    format_rate,
    render_predicate,
)
from harness.ledger import EvidenceLedger, LedgerEvent
from models.datasource import DataSource
from models.enums import DataSourceType, EvidenceEventType, RunStatus
from models.execution import ErrorRecord, ExecutionRun, QualityReport
from schemas.auth import SubjectContext
from schemas.quality import CursorPage, ErrorCodeSummaryDTO, ErrorRecordDTO, FieldHitDTO
from services.pipeline import repository as pipeline_repo

_ledger = EvidenceLedger()
log = get_logger(__name__)

#: 控制面错误副本的上限。错误表可能有百万行，而控制面副本的用途是统计与追溯，
#: 不是明细（明细在目标端）。超出的部分在 ``diagnostics.warnings`` 记
#: ``error_records_truncated``，让「副本不全」这件事有据可查。
ERROR_RECORD_COPY_LIMIT: Final[int] = 1000

#: 违规字段 Top-N 的 N。
TOP_FIELDS: Final[int] = 10

#: 错误明细一页的上限（游标分页）。
MAX_LIMIT: Final[int] = 200

#: **还不该出报告**的运行状态。
#:
#: 设计 §7 只写了 ``queued``；``paused`` 与 ``rolling_back`` 是同一类危险，
#: 一并拦下：``paused`` 时 SeaTunnel 的作业**还在写**影子表，此刻算出的 ``passed=True``
#: 会让 be-18 拿一张还在变的影子表去 Swap（少的那几行不会有人发现）；
#: ``rolling_back`` 说明世界正在被另一次操作改写，结论没有意义。
NOT_FINALIZABLE: Final[frozenset[RunStatus]] = frozenset(
    {RunStatus.QUEUED, RunStatus.PAUSED, RunStatus.ROLLING_BACK}
)

#: ``failed_rules`` 的由来（进账本与 ``failure_snapshot``，供前端与审计定位）。
STAGE: Final[str] = "quality"

#: 目标端读取器的**工厂**：``(session, run) -> ErrorTableReader``。
#:
#: 注册的是工厂而**不是**实例：端口只拿到表名，而表名定位不了目标端——
#: 「`ods_order_err` 在哪台主机、哪个库」由这次运行的 ``resource_scope`` 决定，
#: 每次运行都可能不同。做成进程级单例的话，实现方只能去猜（或按表名反查，
#: 而影子表名在多个运行之间是复用的，反查会拿错库）。
#: 工厂在 ``finalize_report`` 里被调用一次，拿到的是**已经过租户过滤的那一行 run**。
type ErrorTableReaderFactory = Callable[[Any, Any], ErrorTableReader]


_reader_factory: ErrorTableReaderFactory | None = None


def register_error_table_reader(factory: ErrorTableReaderFactory) -> None:
    """注册目标端只读实现的**工厂**（**进程启动时**调一次；见模块末尾的接入说明）。

    与 ``harness`` 的工具执行器同一取舍：真正的实现要拿目标端连接、要读 worker 内存里
    物化的凭据，而本模块只想依赖一个 Protocol。放在启动钩子里注册而不是「用的时候再 import」，
    是为了让「忘了注册」在启动时暴露——运行期才发现的话，症状是所有计数都是 0，
    而 0 是个完全合法的报告值。
    """
    global _reader_factory
    _reader_factory = factory
    log.info("quality_error_table_reader_registered", impl=getattr(factory, "__qualname__", str(factory)))


def get_error_table_reader_factory() -> ErrorTableReaderFactory:
    """取已注册的工厂；没注册就报错，**不**降级成「全部为 0」。"""
    if _reader_factory is None:
        raise ConflictError(
            "目标端错误表读取器未注册：请在两处启动钩子（API lifespan / worker celeryd_init）"
            "调用 services.quality.register_error_table_reader"
        )
    return _reader_factory


# ---------------------------------------------------------------- 快照与载入


class _Stats(NamedTuple):
    """一轮目标端查询的原始计数（还没变成报告）。"""

    valid_rows: int
    rejected_rows: int
    error_code_dist: dict[str, int]
    rule_hits: dict[str, int]
    samples: dict[str, list[QualityDiagnosticSample]]
    records: list[ErrorSample]
    warnings: list[str]


def _require_positive(value: int, name: str) -> int:
    if value < 1:
        raise ValidationError(f"{name} 必须为正整数")
    return value


async def _load_run(session: AsyncSession, *, tenant_id: int, run_id: int) -> ExecutionRun:
    """按 ``(tenant_id, run_id)`` 取运行。

    跨租户**回 404 而不是 403**：403 等于承认「这个 id 存在，只是不给你看」，
    于是 run_id 的枚举可以被用来探测别人的运行数。与 ``harness/broker.py`` 同一取舍。
    """
    row = await session.scalar(
        select(ExecutionRun).where(ExecutionRun.id == run_id, ExecutionRun.tenant_id == tenant_id)
    )
    if row is None:
        raise NotFoundError(f"运行 {run_id} 不存在")
    return row


async def _load_report(session: AsyncSession, *, tenant_id: int, run_id: int) -> QualityReport | None:
    """取该运行的报告（**join 回 execution_run 做租户过滤**，见模块 docstring）。"""
    return await session.scalar(
        select(QualityReport)
        .join(ExecutionRun, ExecutionRun.id == QualityReport.run_id)
        .where(QualityReport.run_id == run_id, ExecutionRun.tenant_id == tenant_id)
    )


async def _load_contract(
    session: AsyncSession, *, tenant_id: int, run: ExecutionRun
) -> tuple[QualityContractSpec, str, DataSourceType, Mapping[str, CompiledRule]]:
    """取本次运行依据的**冻结制品**：质量契约 → (契约, 锚点, 目标端方言, 规则索引)。

    契约从 ``pipeline_version → quality_contract`` 读，**不从请求里带**：
    报告必须回答「这次的结论依据的是哪份规则」。
    """
    version = await pipeline_repo.load_version(
        session=session, tenant_id=tenant_id, version_id=run.pipeline_version_id
    )
    plan_row, _hocon, quality_row, _supervision = await pipeline_repo.artifact_rows(
        session=session, version=version
    )
    EtlPlanModel.model_validate(plan_row.plan)  # 制品自洽性：方案读不出来就别再往下算

    spec = QualityContractSpec(
        # 逐条显式校验：``quality_contract.rules`` 是 jsonb，形状对不对只有到这一步才知道。
        # 让 pydantic 处理（而不是 ``compile_contract`` 里再判一遍）能给出带下标的报错。
        rules=[QualityRuleSpec.model_validate(item) for item in (quality_row.rules or [])],
        error_table_target=quality_row.error_table_target or {},
        # 列是 ``numeric(6,4)``，读回来是 float；契约里的预算用 Decimal（判边界要同定标）。
        reject_rate_budget=Decimal(str(quality_row.reject_rate_budget)),
    )
    # 编译只为拿两样东西：规则的**声明序与归一化形态**（诊断结构要按它排），
    # 以及 ``contract_sha256``——报告里的锚点与门禁/指纹用的锚点必须是同一个串，
    # 所以用 ``compile_contract`` 的产物，不另算一份。
    #
    # 刻意**不**传 ``valid_columns``：那项校验的代价是「规则引用了影子表里没有的列」时
    # 直接编译失败，而此刻数据面**已经跑完**（SQL 早就渲染并执行过了）。在运行期末尾
    # 把一份已经跑完的作业判成「连报告也出不来」，等于把一次可诊断的数据面问题
    # 变成一次没有诊断的黑灯——列校验留在生成期（be-10 渲染那一步），运行期不做。
    try:
        compiled = compile_contract(spec)
    except ContractError as exc:
        # 冻结时能过、运行期末尾编译不过，说明制品被绕过 ORM 改过。报 409 并留下原文，
        # 而不是让一个 ValueError 以 500 的形式冒上去（那条栈会被读成「代码有 bug」）。
        raise ConflictError(f"运行 {run.id} 的质量契约无法编译：{exc}") from exc
    target_type = await _target_type(session, tenant_id=tenant_id, pipeline_id=version.pipeline_id)
    return spec, compiled.contract_sha256, target_type, compiled.index()


async def _target_type(session: AsyncSession, *, tenant_id: int, pipeline_id: int) -> DataSourceType:
    """目标数据源类型 = 影子表/错误表所在端的方言。"""
    pipeline = await pipeline_repo.load_pipeline(
        session=session, tenant_id=tenant_id, pipeline_id=pipeline_id
    )
    if pipeline.target_data_source_id is None:
        raise ConflictError(f"pipeline {pipeline_id} 没有配置目标数据源，无法确定分流方言")
    ds = await session.get(DataSource, pipeline.target_data_source_id)
    if ds is None:
        raise ConflictError(f"pipeline {pipeline_id} 的目标数据源已不存在，无法确定分流方言")
    return ds.type


def _table_of(run: ExecutionRun) -> tuple[str, str]:
    """``(影子表, 错误表)``。缺一不可——没有它们的运行根本没经过分流。"""
    shadow, error = (run.shadow_table or "").strip(), (run.error_table or "").strip()
    if not shadow or not error:
        raise ConflictError(
            f"运行 {run.id} 缺少影子表/错误表名（shadow={shadow or '空'}，error={error or '空'}），"
            "无法统计分流结果"
        )
    return shadow, error


# ---------------------------------------------------------------- 目标端统计


async def _collect(
    *,
    reader: ErrorTableReader,
    shadow: str,
    error: str,
    run_id: int,
    compiled_rules: Sequence[CompiledRule],
    target_type: DataSourceType,
) -> _Stats:
    """把目标端的计数一次性取齐。

    顺序有讲究：先取整表计数，再取分布。两次查询之间数据面可能还在写（正常结束时不会，
    但这里是运行期末尾，宁可按「可能不一致」处理）——所以拒绝率的分子分母
    **只来自同一对计数**（``valid`` / ``rejected``），分布与规则命中只用于解释，
    不参与 ``passed`` 判定。这样即便某一时刻快照不一致，结论也不会依赖那个不一致。
    """
    warnings: list[str] = []
    valid_rows = await reader.count(shadow)
    rejected_rows = await reader.count(error, run_id)
    error_code_dist = {str(k): int(v) for k, v in (await reader.count_by_code(error, run_id)).items()}
    rule_hits = {str(k): int(v) for k, v in (await reader.count_by_rule(error, run_id)).items()}

    # ``warn`` 规则的命中**不在错误表里**（命中不改路由，行照常进影子表）。
    # 唯一能数到它的地方是影子表本身：在影子表上按同一条谓词 COUNT。
    for rule in compiled_rules:
        if rule.on_violation != "warn":
            continue
        try:
            where_sql = render_predicate(rule, target_type)
        except ContractError as exc:  # pragma: no cover - 生成期已编译过同一条规则
            log.warning("quality_warn_predicate_failed", rule_id=rule.rule_id, error=str(exc))
            continue
        try:
            hits = await reader.count_matching(shadow, where_sql)
        except Exception as exc:
            # 这里**刻意**吞掉异常：warn 是「提示」语义，它的计数缺失只影响诊断面板，
            # 而抛出会让一次成功的运行变成「报告都出不来」。记进 warnings 让人看得见。
            warnings.append(f"warn_count_failed:{rule.rule_id}")
            log.warning("quality_warn_count_failed", run_id=run_id, rule_id=rule.rule_id, error=str(exc))
            continue
        rule_hits[rule.rule_id] = int(hits)

    # 样本：每条 reject 规则最多 3 条**摘要**（不含任何原值）。
    samples: dict[str, list[QualityDiagnosticSample]] = {}
    for rule in compiled_rules:
        if rule.on_violation != "reject" or not rule_hits.get(rule.rule_id):
            continue
        try:
            got = await reader.fetch_digest_samples(error, run_id, rule.rule_id, 3)
        except Exception as exc:
            warnings.append(f"sample_fetch_failed:{rule.rule_id}")
            log.warning("quality_sample_failed", run_id=run_id, rule_id=rule.rule_id, error=str(exc))
            continue
        samples[rule.rule_id] = [_sample_of(item) for item in got]

    # 控制面副本：只拷摘要，且**有上限**。
    try:
        records = list(await reader.fetch_records(error, run_id, ERROR_RECORD_COPY_LIMIT))
    except Exception as exc:
        warnings.append("error_records_unavailable")
        log.warning("quality_records_failed", run_id=run_id, error=str(exc))
        records = []
    if len(records) < rejected_rows:
        warnings.append("error_records_truncated")

    return _Stats(
        valid_rows=int(valid_rows),
        rejected_rows=int(rejected_rows),
        error_code_dist=error_code_dist,
        rule_hits=rule_hits,
        samples=samples,
        records=records,
        warnings=warnings,
    )


def _sample_of(item: ErrorSample) -> QualityDiagnosticSample:
    """摘要样本 → 诊断样本。**只搬运摘要与长度，不碰任何原始值**。"""
    lengths: list[int] = []
    if item.payload_digest:
        try:
            lengths.append(digest_content_length(item.payload_digest))
        except ValueError:
            # 目标端回了形状不对的摘要：长度解不出来就不给长度，但摘要照抄
            # （改写或丢弃都会让「报告里看不到的违规行」变成没人知道的事）
            log.warning("quality_digest_malformed", digest_len=len(item.payload_digest))
    return QualityDiagnosticSample(
        payload_digest=item.payload_digest[:64], lengths=lengths, reason=item.reason[:255]
    )


# ---------------------------------------------------------------- 落库


def _insert_report_row(session: AsyncSession, *, run: ExecutionRun, dto: QualityReportDTO) -> QualityReport:
    """INSERT 报告行（不可变，只插不改）。"""
    detail = dto.diagnostics_detail
    row = QualityReport(
        run_id=run.id,
        valid_rows=dto.valid_rows,
        rejected_rows=dto.rejected_rows,
        # 列是 ``numeric(6,4)``；DTO 里的 float 已经是 4 位定标的 Decimal 的精确投影，
        # ``str()`` 往返不丢精度。直接传 float 也可以，但那样精度取决于驱动。
        reject_rate=Decimal(str(dto.reject_rate)),
        error_code_dist=dict(dto.error_code_dist),
        rule_hits=dict(dto.rule_hits),
        passed=bool(dto.passed),
        # ``mode="json"``：``diagnostics`` 里有时间戳，JSONB 列在 SQLite 上走 json.dumps，
        # 不转成 JSON 原生类型会在写入那一刻抛 TypeError（而模型本身完全自洽）。
        diagnostics=detail.model_dump(mode="json") if detail is not None else {},
        generated_at=as_utc(dto.generated_at),
    )
    session.add(row)
    return row


def _insert_error_records(
    session: AsyncSession, *, run_id: int, records: Sequence[ErrorSample], fallback_at: datetime
) -> int:
    """INSERT 控制面错误副本。**只有摘要**：原文既没被查出来，也无处可放。"""
    count = 0
    for item in records:
        session.add(
            ErrorRecord(
                run_id=run_id,
                # 列宽（64/32/64/64/255）逐项对齐，避免 SQLSTATE 22001 在一条无关的更名
                # 之后才暴露。摘要长度恒为 64，超出的只可能是目标端的脏数据 —— 那种情况下
                # 保留前 64 位比整行插入失败有用（其余字段仍然构成一条可用线索）。
                rule_id=item.rule_id[:64],
                error_code=(item.error_code or "E_UNKNOWN")[:32],
                field_name=item.field_name[:64],
                payload_digest=item.payload_digest[:64],
                reason=item.reason[:255],
                # 目标端没回时间就用报告时间：occurred_at 是 NOT NULL，
                # 而「不知道几点发生的」不该让整份报告写不进去。
                occurred_at=as_utc(item.occurred_at) if item.occurred_at else fallback_at,
            )
        )
        count += 1
    return count


# ---------------------------------------------------------------- 对外：写


async def finalize_report(session: AsyncSession, *, tenant_id: int, run_id: int) -> QualityReportDTO:
    """**运行期末尾的入口**：统计分流结果 → 落一份不可变的质量报告 → 给出 ``passed``。

    这是 be-18（原子发布与受管回滚）依赖的跨模块入口，语义按它两个需要在设计上收紧：

    1. 返回的 :class:`QualityReportDTO` 里带**已落库**的 ``report_id``——
       ``TargetPublishSwapArgs.quality_report_id`` 是必填正整数，发布方需要它；
    2. 返回**明确的过/不过**（``passed``）以及不过时**哪几条规则**（``failed_rules``），
       供回滚理由与账本使用。

    ``passed=False`` **不抛异常**：异常一旦抛出，调用方就拿不到 ``report_id`` 与
    ``failed_rules``，而那两样正是「为什么不发布」要写进证据的东西。
    把它变成异常的是**发布侧**（be-18 的门禁），不是结论的产出侧。

    幂等：同一 run 重复调用返回既有报告（``uq_quality_report_run_id``），不报错、不改写。
    """
    _require_positive(run_id, "run_id")
    run = await _load_run(session, tenant_id=tenant_id, run_id=run_id)

    existing = await _load_report(session, tenant_id=tenant_id, run_id=run_id)
    if existing is not None:
        log.info("quality_report_reused", run_id=run_id, report_id=existing.id)
        return _report_from_row(existing, run=run)

    if run.status in NOT_FINALIZABLE:
        # 先把「有人在不该出结论的时候要了结论」记进证据，再拒绝。
        # 只写账本不落报告：此刻的计数是**半成品**，落库会让后面每个读取方都以为它算数。
        await _ledger.append(
            session,
            LedgerEvent(
                tenant_id=tenant_id,
                project_id=run.project_id,
                actor_id=None,
                event_type=EvidenceEventType.RUN_FAILED,
                subject_type="run",
                subject_id=str(run.id),
                payload={"stage": STAGE, "kind": "quality_early", "status": run.status.value},
                occurred_at=utcnow(),
            ),
        )
        await session.commit()
        raise ConflictError(f"运行 {run_id} 处于 {run.status.value}，尚未进入可统计的终态，无法生成质量报告")

    spec, sha, target_type, rule_index = await _load_contract(session, tenant_id=tenant_id, run=run)
    shadow, error_table = _table_of(run)
    # 工厂在这里被调一次：本次运行的目标端（主机/库）由这一行 run 决定，
    # 而 run 已经过租户过滤（``_load_run``）。
    reader = get_error_table_reader_factory()(session, run)

    stats = await _collect(
        reader=reader,
        shadow=shadow,
        error=error_table,
        run_id=run.id,
        # 按**声明序**而不是字典序：诊断样本与警告的顺序要可复现，
        # 否则同一份数据两次跑出两个不同顺序的 diagnostics（diff 会看起来像变更）。
        compiled_rules=sorted(rule_index.values(), key=lambda item: item.order),
        target_type=target_type,
    )
    now = utcnow()
    interrupted = run.status is RunStatus.INTERRUPTED
    dto = build_report(
        run.id,
        valid_rows=stats.valid_rows,
        rejected_rows=stats.rejected_rows,
        read_rows=int(run.read_rows or 0),
        error_code_dist=stats.error_code_dist,
        rule_hits=stats.rule_hits,
        contract=spec,
        rule_index=rule_index,
        interrupted=interrupted,
        generated_at=now,
        samples=stats.samples,
        contract_sha256=sha,
        warnings=stats.warnings,
    )

    row = _insert_report_row(session, run=run, dto=dto)
    await session.flush()
    # 报告一拿到 id 就把结论 DTO **定稿**：下面 ``_mark_failed`` 的现场与
    # ``run.failed`` 账本都要引用它。
    #
    # 晚这一步（等返回前再 ``model_copy``）会让那两处记下 ``report_id=0``——
    # 一个指向不存在报告的 id。它不报错、不抛异常，只是在失败现场里显示
    # 「report_id: 0」，而事后排查者会以为「报告没生成」；账本里的
    # ``quality_rejected`` 也会指向同一个空 id，于是「为什么拒绝」这条链断了。
    dto = dto.model_copy(update={"report_id": row.id})
    copied = _insert_error_records(session, run_id=run.id, records=stats.records, fallback_at=now)

    await _ledger.append(
        session,
        LedgerEvent(
            tenant_id=tenant_id,
            project_id=run.project_id,
            actor_id=None,
            event_type=EvidenceEventType.QUALITY_REPORT_GENERATED,
            subject_type="run",
            subject_id=str(run.id),
            artifact_hash=sha,
            # 账本 payload **不允许 float**（哈希要可复现），比率一律定标文本。
            #
            # 结论这一项**不能**叫 ``passed``：``harness.ledger`` 的入账前置校验
            # （``core/logging.find_sensitive_paths``）用的是一条**包含式**的键名正则
            # ``pass(word|wd)?``，``passed`` 整体命中，于是**每一份**报告都会被
            # ``SensitivePayloadError`` 拦下——症状是「质量报告永远生成不出来」。
            # 换名而不是放宽那条正则：那条正则守的是「密钥不进账本」，
            # 为了一个业务布尔去放宽它，等于在所有模块的账本上开一道口子。
            # DTO 里的 ``passed`` 保持不变（那是对外契约），只账本换词。
            payload={
                "stage": STAGE,
                "report_id": dto.report_id,
                "pipeline_version_id": run.pipeline_version_id,
                "valid_rows": dto.valid_rows,
                "rejected_rows": dto.rejected_rows,
                "processed_rows": dto.processed_rows,
                "read_rows": dto.read_rows,
                "reject_rate": format_rate(dto.reject_rate),
                "reject_rate_budget": format_rate(dto.reject_rate_budget),
                "conclusion": dto.passed,
                "blocked_reason": dto.blocked_reason or "",
                "failed_rules": list(dto.failed_rules),
                "error_code_dist": dict(dto.error_code_dist),
                "contract_sha256": sha,
            },
            occurred_at=now,
        ),
    )

    if not dto.passed:
        await _mark_failed(session, run=run, dto=dto, now=now)

    # 事务边界在 service（``core/db.get_session`` 只借连接不提交）：
    # 报告、错误副本、账本三处必须在同一次提交里出现，否则会出现
    # 「账本说有报告、库里查不到」这种最费时间的对不上。
    await session.commit()

    log.info(
        "quality_report_generated",
        run_id=run.id,
        report_id=dto.report_id,
        # 同样不能叫 ``passed``：日志处理器与账本共用一份键名正则，叫 ``passed``
        # 会让这条日志里的结论被脱敏成 ``***``（读起来像「有敏感值被掩掉了」）。
        conclusion=dto.passed,
        blocked_reason=dto.blocked_reason,
        rejected_rows=dto.rejected_rows,
        processed_rows=dto.processed_rows,
        copied_records=copied,
    )
    return dto


async def _mark_failed(
    session: AsyncSession, *, run: ExecutionRun, dto: QualityReportDTO, now: datetime
) -> None:
    """``passed=False`` 的收尾：现场快照 + 账本 + 状态推进到 ``failed``。

    状态推进**只在合法时**做（``RUN_TRANSITIONS`` 是唯一权威）：一次被硬中断的运行
    已经落在 ``interrupted``，它不该因为「质量没过」变成 ``failed``——
    中断与不合格是两件不同的事，混起来之后「这次为什么停」就没有答案了。
    并发改走的场景（另一个进程刚把它推成别的状态）不覆盖、也不静默：
    报告照写（它是审计事实），只记一条 warning。
    """
    run.failure_snapshot = {
        **(run.failure_snapshot or {}),
        STAGE: {
            "report_id": dto.report_id,
            "passed": False,
            "blocked_reason": dto.blocked_reason or "",
            "failed_rules": list(dto.failed_rules),
            "reject_rate": format_rate(dto.reject_rate),
            "reject_rate_budget": format_rate(dto.reject_rate_budget),
        },
    }
    await _ledger.append(
        session,
        LedgerEvent(
            tenant_id=run.tenant_id,
            project_id=run.project_id,
            actor_id=None,
            event_type=EvidenceEventType.RUN_FAILED,
            subject_type="run",
            subject_id=str(run.id),
            artifact_hash=dto.contract_sha256,
            payload={
                "stage": STAGE,
                "kind": "quality_rejected",
                "report_id": dto.report_id,
                "blocked_reason": dto.blocked_reason or "",
                "failed_rules": list(dto.failed_rules),
                "reject_rate": format_rate(dto.reject_rate),
                "reject_rate_budget": format_rate(dto.reject_rate_budget),
                "error_code_dist": dict(dto.error_code_dist),
            },
            occurred_at=now,
        ),
    )
    if run.status not in (RunStatus.RUNNING, RunStatus.PAUSED):
        return
    # 延迟导入打破模块环：``services.run`` 不需要本模块，但本模块依赖它的状态机，
    # 顶层导入会让「谁 import 谁」变成一道每次都要重新推理的题。
    from services import run as run_service

    try:
        await run_service.apply_transition(
            session,
            run_id=run.id,
            target=RunStatus.FAILED,
            reason=f"{STAGE}:{dto.blocked_reason or 'rejected'}",
            actor_id=None,
            now=now,
        )
    except ConflictError as exc:
        # 状态被别人并发改走了：不覆盖，报告照写。这里不 re-raise ——
        # 抛出去会把一次已经算出结论的质量报告连同一个已落库的 report 一起回滚，
        # 而「谁改了状态」与「这次数据合不合格」是两件事。
        log.warning("quality_mark_failed_conflict", run_id=run.id, error=str(exc))


# ---------------------------------------------------------------- 对外：读


async def require_read_for_run(session: AsyncSession, ctx: SubjectContext, run_id: int) -> None:
    """**HTTP 侧的读闸**：请求者必须是该运行所属项目的职责槽。

    为什么不是每个读函数自带 ``ctx``：设计 §3.2 把读接口的签名定成 ``*, tenant_id, run_id``
    ——worker（be-18 / be-19 的任务）也要调它们，而任务里没有 ``SubjectContext``。
    所以这里把「租户边界」（在 SQL 里，不可绕过）与「项目角色」（只对 HTTP 有意义）
    分开：前者在 service 内部，后者由 API 显式先调一次本函数。**API 必须先调它**。

    复用 ``services/pipeline/artifacts.py::require_roles`` 而不在这里再写一遍判定——
    同一个判定两处实现，迟早会在「读放行、写拦截」上分叉。
    """
    from services.pipeline.artifacts import require_roles

    run = await _load_run(session, tenant_id=ctx.tenant_id, run_id=run_id)
    await require_roles(session, ctx, run.project_id, write=False)


async def get_report(session: AsyncSession, *, tenant_id: int, run_id: int) -> QualityReportDTO:
    """**读**一份已生成的质量报告（be-17 §3.1）。

    报告尚未生成 → ``NOT_FOUND``（前端据此显示「统计中」并继续轮询）：
    回一份 ``passed=false`` 的空白报告会让界面显示「质量不合格」——
    把「还没算」说成「算出来不合格」，是最容易被信以为真的错法。
    """
    run = await _load_run(session, tenant_id=tenant_id, run_id=run_id)
    row = await _load_report(session, tenant_id=tenant_id, run_id=run_id)
    if row is None:
        raise NotFoundError(f"运行 {run_id} 还没有生成质量报告")
    return _report_from_row(row, run=run)


async def list_error_records(
    session: AsyncSession,
    *,
    tenant_id: int,
    run_id: int,
    error_code: str | None = None,
    field_name: str | None = None,
    rule_id: str | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> CursorPage[ErrorRecordDTO]:
    """错误行的控制面副本，游标分页（be-17 §3.1 的 ``…/errors``）。

    ``cursor`` 是**不透明字符串**（:func:`encode_cursor` 产出的 ``er:{id}``），不是 §3.2 里
    的 ``int``：DTO 回给客户端的 ``next_cursor`` 就是这个串，客户端要能原样传回来。
    收 int 的话客户端必须先拆开这个串——而「不许客户端构造游标」正是加前缀的理由。
    """
    if not 1 <= limit <= MAX_LIMIT:
        raise ValidationError(f"limit 必须在 1..{MAX_LIMIT} 之间")
    await _load_run(session, tenant_id=tenant_id, run_id=run_id)

    stmt = select(ErrorRecord).join(ExecutionRun, ExecutionRun.id == ErrorRecord.run_id)
    stmt = _filter_records(stmt, tenant_id, run_id, error_code, field_name, rule_id)
    if cursor:
        stmt = stmt.where(ErrorRecord.id > decode_cursor(cursor))
    rows = list((await session.execute(stmt.order_by(ErrorRecord.id.asc()).limit(limit + 1))).scalars().all())
    has_more = len(rows) > limit
    page_rows = rows[:limit]
    return CursorPage[ErrorRecordDTO](
        items=[_record_to_dto(item) for item in page_rows],
        next_cursor=encode_cursor(page_rows[-1].id) if has_more and page_rows else None,
    )


async def page_error_records(
    session: AsyncSession,
    *,
    tenant_id: int,
    run_id: int,
    error_code: str | None = None,
    field_name: str | None = None,
    rule_id: str | None = None,
    page_no: int = 1,
    page_size: int = 20,
) -> tuple[list[ErrorRecordDTO], int]:
    """同一份数据的**页码分页**形态（前端 ``GET /runs/{id}/error-records`` 的契约）。

    与游标形态并存不是重复：前端契约（``frontend/src/types/run.ts``）里错误明细是
    ``{items, meta}``，而设计 §3.1 的监督接口要的是游标。两种形状各自服务一个调用方，
    但底层的过滤条件只有一份实现（:func:`_filter_records`）。
    """
    if page_no < 1 or not 1 <= page_size <= MAX_LIMIT:
        raise ValidationError(f"page_no 必须 ≥1，page_size 必须在 1..{MAX_LIMIT} 之间")
    await _load_run(session, tenant_id=tenant_id, run_id=run_id)

    base = _filter_records(
        select(ErrorRecord).join(ExecutionRun, ExecutionRun.id == ErrorRecord.run_id),
        tenant_id,
        run_id,
        error_code,
        field_name,
        rule_id,
    )
    total = int((await session.execute(select(func.count()).select_from(base.subquery()))).scalar_one())
    rows = list(
        (
            await session.execute(
                base.order_by(ErrorRecord.id.asc()).offset((page_no - 1) * page_size).limit(page_size)
            )
        )
        .scalars()
        .all()
    )
    return [_record_to_dto(item) for item in rows], total


async def error_code_summary(session: AsyncSession, *, tenant_id: int, run_id: int) -> ErrorCodeSummaryDTO:
    """错误码分布 + 规则命中 + 违规字段 Top-N（be-17 §3.1 的 ``…/errors/summary``）。

    数据全部来自**已落库**的报告与控制面副本：这个接口不碰目标端，
    因此作业跑完（或目标端不可达）之后它仍然可读——图表面板不该依赖数据面的存活。
    """
    run = await _load_run(session, tenant_id=tenant_id, run_id=run_id)
    row = await _load_report(session, tenant_id=tenant_id, run_id=run_id)
    if row is None:
        raise NotFoundError(f"运行 {run_id} 还没有生成质量报告")

    report = _report_from_row(row, run=run)
    grouped = (
        await session.execute(
            select(ErrorRecord.field_name, func.count())
            .join(ExecutionRun, ExecutionRun.id == ErrorRecord.run_id)
            .where(ErrorRecord.run_id == run_id, ExecutionRun.tenant_id == tenant_id)
            .group_by(ErrorRecord.field_name)
            .order_by(func.count().desc(), ErrorRecord.field_name.asc())
            .limit(TOP_FIELDS)
        )
    ).all()
    copied = int(
        (
            await session.execute(
                select(func.count())
                .select_from(ErrorRecord)
                .join(ExecutionRun, ExecutionRun.id == ErrorRecord.run_id)
                .where(ErrorRecord.run_id == run_id, ExecutionRun.tenant_id == tenant_id)
            )
        ).scalar_one()
    )
    return ErrorCodeSummaryDTO(
        run_id=run_id,
        report_id=report.report_id,
        passed=report.passed,
        blocked_reason=report.blocked_reason,
        failed_rules=list(report.failed_rules),
        valid_rows=report.valid_rows,
        rejected_rows=report.rejected_rows,
        processed_rows=report.processed_rows,
        read_rows=report.read_rows,
        reject_rate=report.reject_rate,
        reject_rate_budget=report.reject_rate_budget,
        budget_used_ratio=report.budget_used_ratio,
        error_code_dist=dict(report.error_code_dist),
        rule_hits=dict(report.rule_hits),
        top_fields=[FieldHitDTO(field_name=str(name or ""), count=int(n)) for name, n in grouped],
        error_records_copied=copied,
        error_records_truncated=report.rejected_rows > copied,
        interrupted=report.interrupted,
        contract_sha256=report.contract_sha256,
        generated_at=report.generated_at,
    )


# ---------------------------------------------------------------- 形状转换


def _filter_records(
    stmt: Any,
    tenant_id: int,
    run_id: int,
    error_code: str | None,
    field_name: str | None,
    rule_id: str | None,
) -> Any:
    """错误副本的公共过滤条件（两种分页形态共用，避免两份 where 慢慢长歪）。"""
    stmt = stmt.where(ErrorRecord.run_id == run_id, ExecutionRun.tenant_id == tenant_id)
    if error_code:
        stmt = stmt.where(ErrorRecord.error_code == error_code)
    if field_name:
        stmt = stmt.where(ErrorRecord.field_name == field_name)
    if rule_id:
        stmt = stmt.where(ErrorRecord.rule_id == rule_id)
    return stmt


def _record_to_dto(row: ErrorRecord) -> ErrorRecordDTO:
    """错误副本行 → DTO。时间在**存储边界**归一（``as_utc``）。"""
    return ErrorRecordDTO(
        id=row.id,
        run_id=row.run_id,
        rule_id=row.rule_id,
        error_code=row.error_code,
        field_name=row.field_name,
        payload_digest=row.payload_digest,
        reason=row.reason,
        occurred_at=as_utc(row.occurred_at),
    )


def _report_from_row(row: QualityReport, *, run: ExecutionRun) -> QualityReportDTO:
    """报告行 → DTO。

    ``quality_report`` 只存了结论本身（没有 ``read_rows`` / ``failed_rules`` /
    ``contract_sha256`` 列——表结构已存在且不可改），所以这些从
    ``diagnostics``（本模块写入的完整结构）与 ``execution_run`` 回读；
    ``diagnostics`` 结构损坏时**降级但不失败**：``passed`` 与比率仍来自真实列，
    前端仍然渲染得出结论，而损坏这件事进日志。
    """
    detail = _diagnostics_of(row)
    failed_rules = list(detail.failed_rules) if detail is not None else []
    # ``generated_at`` 是 timestamptz 列：PG 读回来带时区，SQLite 不带 → 统一到 UTC。
    generated = as_utc(row.generated_at)
    rate = float(row.reject_rate)
    budget = detail.reject_rate_budget if detail is not None else 0.0
    return QualityReportDTO(
        report_id=row.id,
        run_id=row.run_id,
        valid_rows=int(row.valid_rows),
        rejected_rows=int(row.rejected_rows),
        processed_rows=int(row.valid_rows) + int(row.rejected_rows),
        read_rows=int(run.read_rows or 0),
        reject_rate=rate,
        reject_rate_budget=budget,
        budget_used_ratio=budget_used_ratio_of(rate, budget),
        passed=bool(row.passed),
        blocked_reason=detail.blocked_reason if detail is not None else None,
        failed_rules=failed_rules,
        error_code_dist={str(k): int(v) for k, v in (row.error_code_dist or {}).items()},
        rule_hits={str(k): int(v) for k, v in (row.rule_hits or {}).items()},
        diagnostics=build_diagnostic_items(detail) if detail is not None else [],
        diagnostics_detail=detail,
        contract_sha256=detail.contract_sha256 if detail is not None else "",
        interrupted=detail.interrupted if detail is not None else run.status is RunStatus.INTERRUPTED,
        generated_at=generated,
    )


def _diagnostics_of(row: QualityReport) -> QualityDiagnostics | None:
    """``diagnostics`` jsonb → 结构；形状不对时**降级**并留下日志。

    读接口不该因为「诊断段坏了」而整体 500：``passed`` 与各项计数都来自真实列，
    那些才是发布门禁的依据。抓到的是 **Pydantic 的** ValidationError
    （与 ``core.errors.ValidationError`` 同名不同物，故在导入处改名）。
    """
    if not row.diagnostics:
        return None
    try:
        return QualityDiagnostics.model_validate(row.diagnostics)
    except PydanticValidationError as exc:  # pragma: no cover - 只有库被绕过 ORM 改过才会到
        log.warning("quality_diagnostics_corrupt", run_id=row.run_id, error=str(exc))
        return None


def encode_cursor(record_id: int) -> str:
    """游标是**不透明**的：``record_id`` 前加前缀。

    允许客户端直接传整数意味着「从任意位置开始翻」被当成合法输入，
    而它跳过的那些行不会有人注意到。与 ``services/governance.py`` 同一取舍。
    """
    return f"er:{record_id}"


def decode_cursor(cursor: str) -> int:
    head, _, tail = cursor.partition(":")
    if head != "er" or not tail.isdigit():
        raise ValidationError("游标无效")
    return int(tail)


# ``contract_sha256`` 在本模块被 re-export 的场景只有一个：调用方（be-18 / 测试）
# 想核对「报告里的锚点」与「契约自己的锚点」。保持导入可用，不额外包装。
__all__ = [
    "ERROR_RECORD_COPY_LIMIT",
    "MAX_LIMIT",
    "NOT_FINALIZABLE",
    "STAGE",
    "TOP_FIELDS",
    "contract_sha256",
    "decode_cursor",
    "encode_cursor",
    "error_code_summary",
    "finalize_report",
    "get_error_table_reader_factory",
    "get_report",
    "list_error_records",
    "page_error_records",
    "register_error_table_reader",
    "require_read_for_run",
]
