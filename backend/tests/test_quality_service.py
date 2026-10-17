"""be-17 质量报告与数据分流：编排层（终态门 / 幂等 / 租户边界 / 证据 / 错误副本）。

**这个模块的五句核心断言**，其余用例都服务于它们：

1. **结论只有一个产地**：``quality_report`` 由 ``services.quality.finalize_report`` 写，
   be-18 的 Swap 与 ``harness.ToolBroker._check_quality`` 都读这**同一行**。
   重复调用返回同一份（``uq_quality_report_run_id``），不新增行、不改写——
   ``quality_report_id`` 会进 Capability，换一个 id 就说不清那次发布依据的是哪份结论。
2. **租户边界靠回连**：``quality_report`` / ``error_record`` 都**没有** ``tenant_id`` 列，
   所以每次读写都必须 join 回 ``execution_run``。少这个 join 的错法是**静默**的：
   拿别人的 ``run_id`` 就能读到别人的质量结论，而接口一个错都不报。
3. **错误副本只存摘要**（CLAUDE.md §5.7）：``error_record`` 里不许出现违规行原文。
   本文件用一个「哨兵原文」穿过唯一的数据入口（``ErrorTableReader``）来验这件事——
   断言那个串在库、诊断 jsonb、账本载荷、DTO 序列化结果里**一处都找不到**。
4. **不合格不抛异常，而是落结论 + 推状态 + 记账**：抛出会让调用方拿不到
   ``report_id`` 与 ``failed_rules``，而那两样正是「为什么不发布」要写进证据的东西。
   但**质量模块自己不投递 Swap**——投递归 be-18，否则同一次发布有两个入口。
5. **账本载荷里没有 float**（哈希要可复现）：比率一律 4 位定标的**文本**。

分层：``services.quality`` + 真库（内存 SQLite）。目标端读不出来（本机没有 Doris，
CLAUDE.md §9），所以那个端口——也是本模块**唯一**的数据入口——由 ``_FakeReader``
经公开注册入口 ``register_error_table_reader`` 替换。不往服务里塞 ``if testing`` 开关：
那样跑出来的就不是生产那条路径。

⚠️ 一处**只在测试里存在**的适配：``uq_execution_run_active_per_version`` 带
``postgresql_where``，SQLite 上退化成「每版本只许一行」；要「同一版本并存两条运行」的
用例先 ``_allow_multiple_runs`` 删掉它还原 PG 语义（与 ``tests/test_supervision.py`` 同一口径）。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from core.errors import ConflictError, NotFoundError, RoleRequiredError, ValidationError
from core.logging import find_sensitive_paths
from engines.plan.schema import (
    EtlPlanDag,
    EtlPlanModel,
    FieldMapping,
    PlanInput,
    PlanOutput,
    RuntimeBudget,
)
from engines.quality import ErrorSample, payload_digest
from models.datasource import DataSource
from models.enums import (
    DataSourceType,
    Env,
    EvidenceEventType,
    MemberStatus,
    PipelineStatus,
    RiskLevel,
    RoleName,
    RunStatus,
)
from models.evidence import EvidenceEvent, OutboxMessage
from models.execution import ErrorRecord, ExecutionRun, QualityReport
from models.governance import Preparation
from models.pipeline import (
    EtlPlan,
    HoconArtifact,
    Pipeline,
    PipelineVersion,
    QualityContract,
    RuntimeSupervisionContract,
)
from models.tenant import AppUser, Project, ProjectMember, RoleAssignment, Tenant
from schemas.auth import SubjectContext
from services import quality as quality_service

#: 用例里的**唯一基准时刻**。所有断言都用它，不掺「真实现在」——
#: 按时间过滤的用例两端都夹在同一个基准上，才不会在某一天自己变红（CLAUDE.md §9.1）。
_T0 = datetime(2026, 10, 16, 12, 0, 0, tzinfo=UTC)

FORMAL = "orders_wide"
SHADOW = "orders_wide__shadow_v1"
ERROR = "orders_wide_elt_reject"

#: **哨兵原文**：一个只可能来自「违规行的原始值」的串。它走的是唯一的数据入口
#: （``ErrorTableReader`` 回的行），因此「它有没有漏到别处」就是「脱敏有没有被绕过」。
RAW_SENTINEL = "13800138000"

#: 两份真实摘要（用参考实现算，不手写 64 个字符）：摘要本身**允许**落库，原文不允许。
DIGEST_ID = payload_digest({"id": "1"}, ["id"])
DIGEST_PHONE = payload_digest({"phone": RAW_SENTINEL}, ["phone"])

#: 契约里的两条规则：一条 reject（进错误表）、一条 warn（不进错误表，只在影子表上数）。
RULE_NULL: dict[str, Any] = {
    "id": "r_null",
    "field": "id",
    "rule_type": "not_null",
    "params": {},
    "error_code": "E_NULL_PK",
    "on_violation": "reject",
    "enabled": True,
}
RULE_AMOUNT_WARN: dict[str, Any] = {
    "id": "r_amount",
    "field": "amount",
    "rule_type": "range",
    "params": {"min": 0, "max": 1000000},
    "error_code": "E_RANGE_VIOLATION",
    "on_violation": "warn",
    "enabled": True,
}


# ---------------------------------------------------------------- 假的错误表读取器


class _FakeReader:
    """``ErrorTableReader`` 的假实现（be-17 §3.2 的端口，本模块唯一的数据入口）。

    ``count`` 按 ``run_id`` 是否为空分派：影子表整表计（有效行不分运行）、
    错误表按运行计——与端口 docstring 的取舍一致，逼使用例显式区分两者。
    """

    def __init__(
        self,
        *,
        valid_rows: int = 0,
        rejected_rows: int = 0,
        by_code: dict[str, int] | None = None,
        by_rule: dict[str, int] | None = None,
        matching_count: int = 0,
        samples: dict[str, list[ErrorSample]] | None = None,
        records: list[ErrorSample] | None = None,
        fail: set[str] | None = None,
    ) -> None:
        self.valid_rows = valid_rows
        self.rejected_rows = rejected_rows
        self.by_code = dict(by_code or {})
        self.by_rule = dict(by_rule or {})
        self.matching_count = matching_count
        self.samples = dict(samples or {})
        self.records = list(records or [])
        #: 方法名 → 抛异常。用来验「目标端抖一下」时报告还能不能出得来。
        self.fail = set(fail or set())
        #: 每次调用的 (方法名, 参数…) —— 用来验「读接口不碰目标端」这类事实。
        self.calls: list[tuple[Any, ...]] = []

    def methods(self) -> list[str]:
        return [str(call[0]) for call in self.calls]

    async def count(self, table: str, run_id: int | None = None) -> int:
        self.calls.append(("count", table, run_id))
        if "count" in self.fail:
            raise RuntimeError("目标端不可达")
        return self.valid_rows if run_id is None else self.rejected_rows

    async def count_by_code(self, table: str, run_id: int) -> dict[str, int]:
        self.calls.append(("count_by_code", table, run_id))
        return dict(self.by_code)

    async def count_by_rule(self, table: str, run_id: int) -> dict[str, int]:
        self.calls.append(("count_by_rule", table, run_id))
        return dict(self.by_rule)

    async def count_matching(self, table: str, where_sql: str) -> int:
        self.calls.append(("count_matching", table, where_sql))
        if "count_matching" in self.fail:
            raise RuntimeError("影子表不可达")
        return self.matching_count

    async def fetch_digest_samples(
        self, table: str, run_id: int, rule_id: str, limit: int = 3
    ) -> list[ErrorSample]:
        self.calls.append(("fetch_digest_samples", table, run_id, rule_id, limit))
        if "fetch_digest_samples" in self.fail:
            raise RuntimeError("样本取不到")
        return list(self.samples.get(rule_id, []))[:limit]

    async def fetch_records(self, table: str, run_id: int, limit: int) -> list[ErrorSample]:
        self.calls.append(("fetch_records", table, run_id, limit))
        if "fetch_records" in self.fail:
            raise RuntimeError("明细副本取不到")
        return list(self.records)[:limit]


@pytest.fixture
def install_reader(monkeypatch: pytest.MonkeyPatch):
    """把假读取器**经公开注册入口**装上（唯一注入点），用例结束自动还原。

    走 ``register_error_table_reader`` 而不是直接改全局：这样「忘了注册」那条路径
    与生产完全一样，本文件里也就能真的验它（见 ``test_未注册读取器时拒绝``）。
    """

    def _install(**kwargs: Any) -> _FakeReader:
        fake = _FakeReader(**kwargs)
        monkeypatch.setattr(quality_service, "_reader_factory", None)
        # 注册的是**工厂**（签名与生产一致：``(session, run) -> reader``）。
        # 顺带钉住「服务是按运行造读取器」这件事：工厂拿到的那行 run 就是
        # 目标端的解析依据——只给表名无法定位主机与库。
        quality_service.register_error_table_reader(lambda session, run: fake)
        return fake

    return _install


# ---------------------------------------------------------------- 种子


class _World:
    """一次用例的现场：租户 / 用户 / 项目 / 数据源 / 一份冻结版本 / 一张运行单。"""

    def __init__(
        self,
        *,
        tenant_id: int,
        project_id: int,
        pipeline_id: int,
        version_id: int,
        preparation_id: int,
        user_id: int,
        target_id: int | None,
    ) -> None:
        self.tenant_id = tenant_id
        self.project_id = project_id
        self.pipeline_id = pipeline_id
        self.version_id = version_id
        self.preparation_id = preparation_id
        self.user_id = user_id
        self.target_id = target_id

    def ctx(self, user_id: int | None = None) -> SubjectContext:
        return SubjectContext(
            user_id=self.user_id if user_id is None else user_id,
            tenant_id=self.tenant_id,
            username="erin",
            display_name="Erin",
            request_id="req_quality",
        )


async def _user(session: AsyncSession, tenant_id: int, username: str) -> AppUser:
    user = AppUser(
        tenant_id=tenant_id,
        username=username,
        display_name=username.title(),
        email=f"{username}@acme.test",
        password_hash="x",
        status="active",
    )
    session.add(user)
    await session.flush()
    return user


async def _grant(session: AsyncSession, project_id: int, user: AppUser, role: RoleName) -> None:
    """直接铺成员 + 角色行：授权本身要 checker_security 且会落账本，
    在这里走一遍只会把本文件的失败原因和 be-02 的 bug 搅在一起。"""
    member = await session.scalar(
        select(ProjectMember).where(
            ProjectMember.project_id == project_id, ProjectMember.user_id == user.id
        )
    )
    if member is None:
        member = ProjectMember(project_id=project_id, user_id=user.id, status=MemberStatus.ACTIVE)
        session.add(member)
        await session.flush()
    session.add(
        RoleAssignment(
            project_id=project_id,
            member_id=member.id,
            user_id=user.id,
            role=role,
            granted_by=user.id,
            reason="测试",
        )
    )
    await session.flush()


def _plan(*, source_id: int, target_id: int) -> EtlPlanModel:
    """一份**能过 ``EtlPlanModel`` 校验**的最小方案。

    刻意不走「生成 → 冻结」全链路：那条链路的重活归 M2 的用例，在这里再跑一遍
    只会把本文件的失败原因和生成链路的 bug 搅在一起。但方案本身必须**真能反序列化**
    ——``_load_contract`` 会 ``model_validate`` 它，塞 ``{}`` 会让每个用例都红在
    「制品自洽性」上，而不是红在被测的那条路径上。
    """
    return EtlPlanModel(
        inputs=[PlanInput(id="src_1", data_source_id=source_id, schema_name="sales", table="orders")],
        outputs=[
            PlanOutput(
                id="sink_1",
                data_source_id=target_id,
                database="dw",
                table=FORMAL,
                mode="shadow_then_swap",
            )
        ],
        field_mappings=[
            FieldMapping(
                source_field="id", source_type="bigint", target_field="id", target_type="bigint"
            ),
            FieldMapping(
                source_field="phone",
                source_type="varchar",
                target_field="phone",
                target_type="varchar",
            ),
            FieldMapping(
                source_field="amount",
                source_type="decimal",
                target_field="amount",
                target_type="decimal",
            ),
        ],
        dag=EtlPlanDag(),
        budget=RuntimeBudget(
            max_read_rows=1000,
            max_write_bytes=1000,
            max_duration_sec=60,
            max_amplification_ratio=Decimal("1.5"),
            max_reject_rate=Decimal("0.05"),
            warn_threshold_pct=80,
        ),
    )


async def _seed(
    session: AsyncSession,
    *,
    rules: list[dict[str, Any]] | None = None,
    budget: str = "0.05",
    with_target: bool = True,
    target_type: DataSourceType = DataSourceType.DORIS,
) -> _World:
    tenant = Tenant(code="acme", name="Acme", status="active")
    session.add(tenant)
    await session.flush()

    user = await _user(session, tenant.id, "erin")

    project = Project(tenant_id=tenant.id, code="orders", name="订单", env=Env.DEV)
    session.add(project)
    await session.flush()
    await _grant(session, project.id, user, RoleName.OPERATOR)

    source = DataSource(
        tenant_id=tenant.id,
        project_id=project.id,
        name="源库",
        type=DataSourceType.MYSQL,
        env=Env.DEV,
        endpoint={"host": "10.0.0.5", "port": 3306, "database": "sales"},
        sensitive_level="internal",
        tags=[],
        status="active",
    )
    session.add(source)
    await session.flush()

    target_id: int | None = None
    if with_target:
        target = DataSource(
            tenant_id=tenant.id,
            project_id=project.id,
            name="目标库",
            type=target_type,
            env=Env.DEV,
            endpoint={"host": "10.0.0.6", "port": 9030, "database": "dw"},
            sensitive_level="internal",
            tags=[],
            status="active",
        )
        session.add(target)
        await session.flush()
        target_id = target.id

    plan_row = EtlPlan(
        schema_version="1.0",
        plan=_plan(source_id=source.id, target_id=target_id or source.id).model_dump(mode="json"),
        valid=True,
        validation_errors=[],
    )
    hocon_row = HoconArtifact(
        content="env {}", sha256="c" * 64, parser_version="1.0", validation_result={}
    )
    quality_row = QualityContract(
        rules=list(rules) if rules is not None else [RULE_NULL, RULE_AMOUNT_WARN],
        error_table_target={"data_source_id": target_id, "table": ERROR},
        reject_rate_budget=Decimal(budget),
    )
    supervision_row = RuntimeSupervisionContract(
        max_read_rows=1000,
        max_write_bytes=1000,
        max_duration_sec=60,
        max_amplification_ratio=Decimal("1.5"),
        max_reject_rate=Decimal("0.05"),
        warn_threshold_pct=80,
    )
    session.add_all([plan_row, hocon_row, quality_row, supervision_row])
    await session.flush()

    pipeline = Pipeline(
        tenant_id=tenant.id,
        project_id=project.id,
        name="orders_to_dw",
        source_data_source_id=source.id,
        target_data_source_id=target_id,
        status=PipelineStatus.VERSION_FROZEN,
        created_by=user.id,
    )
    session.add(pipeline)
    await session.flush()

    version = PipelineVersion(
        pipeline_id=pipeline.id,
        version_no=1,
        etl_plan_id=plan_row.id,
        hocon_artifact_id=hocon_row.id,
        quality_contract_id=quality_row.id,
        supervision_contract_id=supervision_row.id,
        generation_context={},
        sha256="a" * 64,
        frozen_by=user.id,
        frozen_at=datetime(2026, 10, 16, 8, 0, tzinfo=UTC),
        note="",
    )
    session.add(version)
    await session.flush()
    pipeline.current_version_id = version.id

    # 准备单直接铺行：本模块只用它当外键（``execution_run.preparation_id``），
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

    return _World(
        tenant_id=tenant.id,
        project_id=project.id,
        pipeline_id=pipeline.id,
        version_id=version.id,
        preparation_id=preparation.id,
        user_id=user.id,
        target_id=target_id,
    )


@pytest.fixture
async def world(async_session: AsyncSession) -> _World:
    return await _seed(async_session)


async def _outsider(session: AsyncSession, *, code: str = "other") -> SubjectContext:
    """另一个租户的用户：用来验租户边界（**不是**权限边界，那条走角色）。"""
    tenant = Tenant(code=code, name=code, status="active")
    session.add(tenant)
    await session.flush()
    user = await _user(session, tenant.id, f"{code}_user")
    await session.commit()
    return SubjectContext(
        user_id=user.id,
        tenant_id=tenant.id,
        username=user.username,
        display_name=user.display_name,
        request_id="req_outsider",
    )


async def _make_run(
    session: AsyncSession,
    world: _World,
    *,
    status: RunStatus = RunStatus.RUNNING,
    key: str = "k1",
    read_rows: int = 100,
    shadow: str | None = SHADOW,
    error: str | None = ERROR,
) -> ExecutionRun:
    run = ExecutionRun(
        tenant_id=world.tenant_id,
        project_id=world.project_id,
        pipeline_version_id=world.version_id,
        preparation_id=world.preparation_id,
        idempotency_key=key,
        status=status,
        shadow_table=shadow,
        formal_table=FORMAL,
        error_table=error,
        seatunnel_job_id="job-1",
        started_at=_T0 - timedelta(seconds=10),
        read_rows=read_rows,
        triggered_by=world.user_id,
    )
    session.add(run)
    await session.commit()
    return run


async def _fresh(session: AsyncSession, run_id: int) -> ExecutionRun:
    """绕过 identity map 重读运行行（``apply_transition`` 走的是 Core UPDATE）。"""
    session.expire_all()
    row = await session.scalar(select(ExecutionRun).where(ExecutionRun.id == run_id))
    assert row is not None
    return row


async def _events(
    session: AsyncSession, *, tenant_id: int, event_type: EvidenceEventType
) -> list[EvidenceEvent]:
    return list(
        (
            await session.scalars(
                select(EvidenceEvent)
                .where(
                    EvidenceEvent.tenant_id == tenant_id, EvidenceEvent.event_type == event_type
                )
                .order_by(EvidenceEvent.seq.asc())
            )
        ).all()
    )


async def _count(session: AsyncSession, model: Any) -> int:
    return int(await session.scalar(select(func.count()).select_from(model)) or 0)


async def _allow_multiple_runs(session: AsyncSession) -> None:
    await session.execute(text("DROP INDEX IF EXISTS uq_execution_run_active_per_version"))
    await session.commit()


async def _add_records(
    session: AsyncSession,
    run_id: int,
    count: int,
    *,
    error_code: str = "E_NULL_PK",
    field_name: str = "id",
    rule_id: str = "r_null",
) -> list[int]:
    """直接铺错误副本行：读接口的分页/过滤不该再去跑一遍 ``finalize_report``。"""
    ids: list[int] = []
    for index in range(count):
        row = ErrorRecord(
            run_id=run_id,
            rule_id=rule_id,
            error_code=error_code,
            field_name=field_name,
            payload_digest=DIGEST_ID,
            reason="id 缺失",
            occurred_at=_T0 + timedelta(seconds=index),
        )
        session.add(row)
        await session.flush()
        ids.append(row.id)
    await session.commit()
    return ids


def _sample(**overrides: Any) -> ErrorSample:
    """一条**摘要**样本。``extra="ignore"`` 意味着端口之外的键进不来——
    这正是 ``test_错误副本只写摘要不写原文`` 要用来证明的那件事。"""
    values: dict[str, Any] = {
        "rule_id": "r_null",
        "field_name": "id",
        "error_code": "E_NULL_PK",
        "payload_digest": DIGEST_ID,
        "reason": "id 不能为空",
        "occurred_at": _T0,
    }
    values.update(overrides)
    return ErrorSample(**values)


def _floats_in(value: Any, path: str = "") -> list[str]:
    """递归找出 dict/list 里所有 float 的路径（账本 payload 里不许有 float）。"""
    found: list[str] = []
    if isinstance(value, float):
        found.append(path or "<root>")
    elif isinstance(value, dict):
        for key, item in value.items():
            found.extend(_floats_in(item, f"{path}.{key}"))
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            found.extend(_floats_in(item, f"{path}[{index}]"))
    return found


# ================================================================ 终态与幂等


async def test_最终报告落库并回带报告ID(
    async_session: AsyncSession, world: _World, install_reader: Any
) -> None:
    run = await _make_run(async_session, world)
    install_reader(valid_rows=95, rejected_rows=5, by_rule={"r_null": 5})

    dto = await quality_service.finalize_report(
        async_session, tenant_id=world.tenant_id, run_id=run.id
    )

    assert dto.run_id == run.id
    assert dto.report_id is not None and dto.report_id > 0
    assert (dto.valid_rows, dto.rejected_rows, dto.processed_rows) == (95, 5, 100)
    assert dto.reject_rate == pytest.approx(0.05)
    assert dto.passed is True and dto.blocked_reason is None
    assert dto.failed_rules == ["r_null"]

    row = await async_session.scalar(
        select(QualityReport).where(QualityReport.run_id == run.id)
    )
    assert row is not None and row.id == dto.report_id
    assert (row.valid_rows, row.rejected_rows) == (95, 5)
    # 列是 numeric(6,4)：写进去的比率必须是 4 位定标，不能是 float 的二进制近似
    assert Decimal(str(row.reject_rate)) == Decimal("0.0500")


async def test_报告行只插一次_重复调用返回同一份(
    async_session: AsyncSession, world: _World, install_reader: Any
) -> None:
    """``quality_report_id`` 会进 Capability：换一个 id 就说不清依据的是哪份结论。"""
    run = await _make_run(async_session, world)
    install_reader(valid_rows=95, rejected_rows=5, by_rule={"r_null": 5})

    first = await quality_service.finalize_report(
        async_session, tenant_id=world.tenant_id, run_id=run.id
    )
    second = await quality_service.finalize_report(
        async_session, tenant_id=world.tenant_id, run_id=run.id
    )

    assert first.report_id == second.report_id
    assert first.contract_sha256 == second.contract_sha256
    assert await _count(async_session, QualityReport) == 1
    assert len(await _events(async_session, tenant_id=world.tenant_id, event_type=EvidenceEventType.QUALITY_REPORT_GENERATED)) == 1


async def test_重复调用不再读目标端(
    async_session: AsyncSession, world: _World, install_reader: Any
) -> None:
    """幂等分支必须**在读目标端之前**返回：重投的任务不该再打一次数据面。"""
    run = await _make_run(async_session, world)
    reader = install_reader(valid_rows=95, rejected_rows=5)
    await quality_service.finalize_report(async_session, tenant_id=world.tenant_id, run_id=run.id)
    before = list(reader.calls)

    await quality_service.finalize_report(async_session, tenant_id=world.tenant_id, run_id=run.id)

    assert reader.calls == before


async def test_run_id_必须是正整数(
    async_session: AsyncSession, world: _World, install_reader: Any
) -> None:
    install_reader()
    with pytest.raises(ValidationError):
        await quality_service.finalize_report(async_session, tenant_id=world.tenant_id, run_id=0)


# ================================================================ 租户边界


async def test_跨租户的最终报告回404(
    async_session: AsyncSession, world: _World, install_reader: Any
) -> None:
    """回 404 而不是 403：403 等于承认「这个 id 存在，只是不给你看」。"""
    run = await _make_run(async_session, world)
    other = await _outsider(async_session)
    install_reader(valid_rows=95, rejected_rows=5)

    with pytest.raises(NotFoundError):
        await quality_service.finalize_report(
            async_session, tenant_id=other.tenant_id, run_id=run.id
        )
    assert await _count(async_session, QualityReport) == 0


@pytest.mark.parametrize("reader_name", ["get_report", "error_code_summary", "list_error_records"])
async def test_跨租户的读取回404(
    async_session: AsyncSession, world: _World, install_reader: Any, reader_name: str
) -> None:
    """``quality_report`` / ``error_record`` 没有 ``tenant_id`` 列——边界全靠回连。

    少那个 join 的错法是静默的：拿别人的 ``run_id`` 就能读到别人的质量结论。
    """
    run = await _make_run(async_session, world)
    install_reader(valid_rows=95, rejected_rows=5)
    await quality_service.finalize_report(async_session, tenant_id=world.tenant_id, run_id=run.id)
    other = await _outsider(async_session)

    with pytest.raises(NotFoundError):
        await getattr(quality_service, reader_name)(
            async_session, tenant_id=other.tenant_id, run_id=run.id
        )


# ================================================================ 不该出结论的时刻


@pytest.mark.parametrize(
    "status", [RunStatus.QUEUED, RunStatus.PAUSED, RunStatus.ROLLING_BACK]
)
async def test_未到终态时拒绝并只写一条证据(
    async_session: AsyncSession, world: _World, install_reader: Any, status: RunStatus
) -> None:
    """``paused`` 时数据面**还在写**影子表：此刻算出 ``passed=True`` 会让 be-18
    拿一张还在变的表去 Swap，少的那几行不会有人发现。"""
    run = await _make_run(async_session, world, status=status)
    reader = install_reader(valid_rows=95, rejected_rows=5)

    with pytest.raises(ConflictError):
        await quality_service.finalize_report(
            async_session, tenant_id=world.tenant_id, run_id=run.id
        )

    # 只写账本不落报告：此刻的计数是半成品，落库会让每个读取方都以为它算数。
    assert await _count(async_session, QualityReport) == 0
    assert reader.calls == []
    events = await _events(
        async_session, tenant_id=world.tenant_id, event_type=EvidenceEventType.RUN_FAILED
    )
    assert len(events) == 1
    assert events[0].payload["kind"] == "quality_early"
    assert events[0].payload["status"] == status.value


# ================================================================ 结论与失败收尾


async def test_超预算时运行被推进到failed并写现场与账本(
    async_session: AsyncSession, world: _World, install_reader: Any
) -> None:
    run = await _make_run(async_session, world)
    install_reader(valid_rows=90, rejected_rows=10, by_rule={"r_null": 10})

    dto = await quality_service.finalize_report(
        async_session, tenant_id=world.tenant_id, run_id=run.id
    )

    # ``passed=False`` 不抛异常：抛了调用方就拿不到 report_id 与 failed_rules，
    # 而那两样正是「为什么不发布」要写进证据的东西。
    assert dto.passed is False and dto.blocked_reason == "over_budget"
    assert dto.failed_rules == ["r_null"]
    assert await _count(async_session, QualityReport) == 1

    fresh = await _fresh(async_session, run.id)
    assert fresh.status is RunStatus.FAILED
    snapshot = fresh.failure_snapshot["quality"]
    assert snapshot["report_id"] == dto.report_id
    assert snapshot["blocked_reason"] == "over_budget"
    assert snapshot["failed_rules"] == ["r_null"]
    # 现场里不许有 float：比率一律定标文本，与账本同一口径
    assert snapshot["reject_rate"] == "0.1000"
    assert snapshot["reject_rate_budget"] == "0.0500"

    events = await _events(
        async_session, tenant_id=world.tenant_id, event_type=EvidenceEventType.RUN_FAILED
    )
    assert len(events) == 1
    assert events[0].payload["kind"] == "quality_rejected"
    assert events[0].payload["failed_rules"] == ["r_null"]
    # **报告 id 必须是真 id，不是占位 0**：现场与账本都要能指回那份报告，
    # 否则「为什么拒绝」这条链在事后排查时是断的（而 0 看起来只是个数字）。
    assert dto.report_id > 0
    assert events[0].payload["report_id"] == dto.report_id



async def test_硬拦截错误码直接判不合格(
    async_session: AsyncSession, world: _World, install_reader: Any
) -> None:
    """敏感字段明文出域（P0）哪怕只有一行也不许发布——与拒绝率**无关**。"""
    run = await _make_run(async_session, world)
    install_reader(valid_rows=99, rejected_rows=1, by_code={"E_SENSITIVE_PLAINTEXT": 1})

    dto = await quality_service.finalize_report(
        async_session, tenant_id=world.tenant_id, run_id=run.id
    )

    assert dto.reject_rate == pytest.approx(0.01)  # 远低于预算
    assert dto.passed is False
    assert dto.blocked_reason == "hard_block_code"


async def test_中断的运行保持interrupted_不改成failed(
    async_session: AsyncSession, world: _World, install_reader: Any
) -> None:
    """中断与不合格是两件不同的事：混起来之后「这次为什么停」就没有答案了。"""
    run = await _make_run(async_session, world, status=RunStatus.INTERRUPTED)
    install_reader(valid_rows=90, rejected_rows=10, by_rule={"r_null": 10})

    dto = await quality_service.finalize_report(
        async_session, tenant_id=world.tenant_id, run_id=run.id
    )

    assert dto.passed is False and dto.interrupted is True
    fresh = await _fresh(async_session, run.id)
    assert fresh.status is RunStatus.INTERRUPTED
    # 现场与账本照写：报告是审计事实，不因为状态推不动就不记
    assert fresh.failure_snapshot["quality"]["report_id"] == dto.report_id


async def test_零分流但读到了行判不合格(
    async_session: AsyncSession, world: _World, install_reader: Any
) -> None:
    """全被拒就是有效行 0——此时拒绝率的分母也是 0，光看比率会得出「0 ≤ 预算」的结论。"""
    run = await _make_run(async_session, world, read_rows=100)
    install_reader(valid_rows=0, rejected_rows=0)

    dto = await quality_service.finalize_report(
        async_session, tenant_id=world.tenant_id, run_id=run.id
    )

    assert dto.read_rows == 100
    assert dto.processed_rows == 0
    assert dto.passed is False
    assert dto.blocked_reason == "zero_routed_rows"


async def test_拒绝率等于预算算通过(
    async_session: AsyncSession, world: _World, install_reader: Any
) -> None:
    """预算即闭区间上界：``==`` 算通过。判反了会让一条**守规矩**的运行被拦下。"""
    run = await _make_run(async_session, world)
    install_reader(valid_rows=95, rejected_rows=5)

    dto = await quality_service.finalize_report(
        async_session, tenant_id=world.tenant_id, run_id=run.id
    )

    assert dto.reject_rate_budget == pytest.approx(0.05)
    assert dto.passed is True


async def test_质量不合格不投递任何出箱消息(
    async_session: AsyncSession, world: _World, install_reader: Any
) -> None:
    """本模块**不投递 Swap**：``passed`` 之后的发布归 be-18。
    这里也投递的话，同一次发布就有两个入口，两个都「看起来对」，结果是 Swap 跑两遍。"""
    run = await _make_run(async_session, world)
    install_reader(valid_rows=90, rejected_rows=10, by_rule={"r_null": 10})

    await quality_service.finalize_report(async_session, tenant_id=world.tenant_id, run_id=run.id)

    assert await _count(async_session, OutboxMessage) == 0


async def test_账本载荷里没有float(
    async_session: AsyncSession, world: _World, install_reader: Any
) -> None:
    """账本要对 payload 算哈希，float 的二进制近似会让同一份事实在两个进程上哈希不同。"""
    run = await _make_run(async_session, world)
    install_reader(valid_rows=95, rejected_rows=5, by_rule={"r_null": 5})

    dto = await quality_service.finalize_report(
        async_session, tenant_id=world.tenant_id, run_id=run.id
    )

    events = await _events(
        async_session, tenant_id=world.tenant_id, event_type=EvidenceEventType.QUALITY_REPORT_GENERATED
    )
    assert len(events) == 1
    event = events[0]
    assert _floats_in(event.payload) == []
    assert event.payload["reject_rate"] == "0.0500"
    assert event.payload["reject_rate_budget"] == "0.0500"
    assert event.payload["report_id"] == dto.report_id
    assert event.payload["pipeline_version_id"] == world.version_id
    assert event.payload["stage"] == quality_service.STAGE
    # 账本与接口说的是同一个锚点：两处各算一份，迟早在某次归一化改动后分叉
    assert event.artifact_hash == dto.contract_sha256
    # ``occurred_at`` 的时区不在这一层断言：账本自己的入账校验会拒绝裸 datetime
    # （``LedgerError``），所以这行能落下来就说明传的是带时区的值；
    # 而 SQLite 的 ``DateTime`` 不保留时区，读回来必然是 naive
    # （``as_utc`` 这个存储边界翻译存在的原因）。
    assert event.occurred_at is not None


async def test_账本载荷的键名不撞脱敏正则(
    async_session: AsyncSession, world: _World, install_reader: Any
) -> None:
    """**这是一条回归用例。** 结论原本存成 ``payload["passed"]``，
    而 ``harness.ledger`` 的入账前置校验用的是**包含式**键名正则 ``pass(word|wd)?``——
    ``passed`` 整体命中，于是**每一份**质量报告都被 ``SensitivePayloadError`` 拦下，
    症状是「报告永远生成不出来」（整条 M3 发布链路的前置条件）。

    断言两件事：① 结论**在**账本里（否则审计看不出这次到底放行了没有——
    ``interrupted`` 时 ``passed=False`` 而 ``blocked_reason`` 可能为空，推不出来）；
    ② 载荷整体不命中脱敏正则（键名与值形态都算）。
    """
    run = await _make_run(async_session, world)
    install_reader(valid_rows=95, rejected_rows=5, by_rule={"r_null": 5})

    await quality_service.finalize_report(async_session, tenant_id=world.tenant_id, run_id=run.id)

    events = await _events(
        async_session, tenant_id=world.tenant_id, event_type=EvidenceEventType.QUALITY_REPORT_GENERATED
    )
    payload = events[0].payload
    assert payload["conclusion"] is True
    assert "passed" not in payload
    assert find_sensitive_paths(payload) == []


# ================================================================ warn 规则


async def test_warn命中在影子表上计数且不进failed_rules(
    async_session: AsyncSession, world: _World, install_reader: Any
) -> None:
    """warn 命中**不改变路由**，行照常进影子表，所以错误表里没有它的痕迹——
    唯一能数到它的地方是影子表本身。"""
    run = await _make_run(async_session, world)
    reader = install_reader(
        valid_rows=95, rejected_rows=5, by_rule={"r_null": 5}, matching_count=9
    )

    dto = await quality_service.finalize_report(
        async_session, tenant_id=world.tenant_id, run_id=run.id
    )

    assert dto.rule_hits == {"r_null": 5, "r_amount": 9}
    assert dto.failed_rules == ["r_null"]  # warn 的 9 次命中不进拒绝理由
    counting = [call for call in reader.calls if call[0] == "count_matching"]
    assert len(counting) == 1
    assert counting[0][1] == SHADOW  # 在**影子表**上数，不是在错误表上
    assert "amount" in counting[0][2]  # 谓词里只有列名与规则模板，没有任何行值


async def test_warn计数失败只留警告不影响结论(
    async_session: AsyncSession, world: _World, install_reader: Any
) -> None:
    """warn 是「提示」语义：它的计数缺失只影响诊断面板，
    抛出会让一次**成功**的运行变成「报告都出不来」。"""
    run = await _make_run(async_session, world)
    install_reader(
        valid_rows=95, rejected_rows=5, by_rule={"r_null": 5}, fail={"count_matching"}
    )

    dto = await quality_service.finalize_report(
        async_session, tenant_id=world.tenant_id, run_id=run.id
    )

    assert dto.passed is True
    assert "warn_count_failed:r_amount" in (dto.diagnostics_detail.warnings if dto.diagnostics_detail else [])


# ================================================================ 错误副本：脱敏


async def test_错误副本只写摘要不写原文(
    async_session: AsyncSession, world: _World, install_reader: Any
) -> None:
    """**本文件最重要的一条。** 哨兵原文从唯一的数据入口进来，必须在四处都找不到：

    库列、诊断 jsonb、账本载荷、DTO 的序列化结果。任何一处漏出去，
    「错误表只存受保护摘要」这条红线就破了，而接口不会报任何错。
    """
    run = await _make_run(async_session, world)
    install_reader(
        valid_rows=95,
        rejected_rows=5,
        by_rule={"r_null": 5},
        samples={
            "r_null": [
                _sample(
                    payload_digest=DIGEST_PHONE,
                    # 端口之外的键：``extra="ignore"`` 必须把它丢掉。
                    # 一旦哪天有人把 ``ErrorSample`` 放宽成 extra="allow"，
                    # 或者让 ``_insert_error_records`` 改成逐键拷贝，这里就会红。
                    raw_value=RAW_SENTINEL,
                    source_row={"phone": RAW_SENTINEL},
                )
            ]
        },
        records=[
            _sample(payload_digest=DIGEST_PHONE, raw_value=RAW_SENTINEL),
            _sample(payload_digest=DIGEST_ID, raw_value=RAW_SENTINEL),
        ],
    )

    dto = await quality_service.finalize_report(
        async_session, tenant_id=world.tenant_id, run_id=run.id
    )

    rows = list(
        (await async_session.scalars(select(ErrorRecord).where(ErrorRecord.run_id == run.id))).all()
    )
    assert [row.payload_digest for row in rows] == [DIGEST_PHONE, DIGEST_ID]

    report = await async_session.scalar(
        select(QualityReport).where(QualityReport.run_id == run.id)
    )
    events = await _events(
        async_session, tenant_id=world.tenant_id, event_type=EvidenceEventType.QUALITY_REPORT_GENERATED
    )
    # 先把四份「出网形状」抄成普通 dict，**最后**再读运行现场：``_fresh`` 会
    # ``expire_all()``，那之后再碰上面这些 ORM 实例会触发同步懒加载
    # （异步 session 上直接 ``MissingGreenlet``，读起来像「SQLAlchemy 坏了」）。
    pieces: dict[str, Any] = {
        "error_records": [
            {column.key: str(getattr(row, column.key)) for column in row.__table__.columns}
            for row in rows
        ],
        "diagnostics": report.diagnostics if report is not None else {},
        "ledger": [event.payload for event in events],
        "dto": dto.model_dump(mode="json"),
    }
    pieces["failure_snapshot"] = (await _fresh(async_session, run.id)).failure_snapshot
    haystack = json.dumps(pieces, ensure_ascii=False, default=str)
    assert RAW_SENTINEL not in haystack
    assert DIGEST_PHONE in haystack  # 摘要本身是允许的——它是「这行长什么样」的唯一线索
    # 诊断里的样本也只有摘要与长度
    sample = report.diagnostics["rules"][0]["samples"][0]
    assert sample["payload_digest"] == DIGEST_PHONE
    assert sample["lengths"] == [len(RAW_SENTINEL)]


def test_error_record表没有可放原文的列() -> None:
    """把「只存摘要」钉在表结构上：多一列 ``value`` 就等于开了一条回流通道。"""
    assert {column.key for column in ErrorRecord.__table__.columns} == {
        "id",
        "run_id",
        "rule_id",
        "error_code",
        "field_name",
        "payload_digest",
        "reason",
        "occurred_at",
        "created_at",
    }


async def test_错误副本字段超宽被截断(
    async_session: AsyncSession, world: _World, install_reader: Any
) -> None:
    """列宽（64/32/64/64/255）逐项对齐：超宽不截断的话，症状是
    「数据面写进去了、控制面读不出来」（SQLSTATE 22001）。"""
    run = await _make_run(async_session, world)
    long_digest = "d" * 100
    install_reader(
        valid_rows=95,
        rejected_rows=1,
        by_rule={"r_null": 1},
        records=[
            ErrorSample(
                rule_id="r" * 100,
                field_name="f" * 100,
                error_code="E" * 40,
                payload_digest=long_digest,
                reason="很长的原因模板" * 40,
                occurred_at=None,  # 目标端没回时间 → 用报告时间兜底，而不是插 NULL 失败
            )
        ],
    )

    await quality_service.finalize_report(async_session, tenant_id=world.tenant_id, run_id=run.id)

    row = await async_session.scalar(
        select(ErrorRecord).where(ErrorRecord.run_id == run.id)
    )
    assert row is not None
    assert len(row.rule_id) == 64 and row.rule_id == "r" * 64
    assert len(row.field_name) == 64
    assert len(row.error_code) == 32
    assert len(row.payload_digest) == 64 and row.payload_digest == long_digest[:64]
    assert len(row.reason) == 255
    assert row.occurred_at is not None


async def test_副本被截断时留下警告标记(
    async_session: AsyncSession, world: _World, install_reader: Any
) -> None:
    """控制面副本有上限（错误表可能百万行）：截断这件事必须有据可查，
    否则「报告里的明细齐全」会被当成事实。"""
    run = await _make_run(async_session, world)
    install_reader(
        valid_rows=95, rejected_rows=5, by_rule={"r_null": 5}, records=[_sample(), _sample()]
    )

    dto = await quality_service.finalize_report(
        async_session, tenant_id=world.tenant_id, run_id=run.id
    )

    assert dto.diagnostics_detail is not None
    assert "error_records_truncated" in dto.diagnostics_detail.warnings
    assert await _count(async_session, ErrorRecord) == 2


async def test_明细副本取不到时报告照样出得来(
    async_session: AsyncSession, world: _World, install_reader: Any
) -> None:
    """副本取不到只影响诊断面板：结论本身（``passed`` / 计数 / 比率）来自另一批查询。"""
    run = await _make_run(async_session, world)
    install_reader(
        valid_rows=95, rejected_rows=5, by_rule={"r_null": 5}, fail={"fetch_records"}
    )

    dto = await quality_service.finalize_report(
        async_session, tenant_id=world.tenant_id, run_id=run.id
    )

    assert dto.passed is True
    assert dto.diagnostics_detail is not None
    assert "error_records_unavailable" in dto.diagnostics_detail.warnings
    assert await _count(async_session, ErrorRecord) == 0


# ================================================================ 读接口


async def test_报告未生成时读取回404(
    async_session: AsyncSession, world: _World, install_reader: Any
) -> None:
    """回一份 ``passed=false`` 的空白报告会让界面显示「质量不合格」——
    把「还没算」说成「算出来不合格」，是最容易被信以为真的错法。"""
    run = await _make_run(async_session, world)
    install_reader()

    with pytest.raises(NotFoundError):
        await quality_service.get_report(
            async_session, tenant_id=world.tenant_id, run_id=run.id
        )
    with pytest.raises(NotFoundError):
        await quality_service.error_code_summary(
            async_session, tenant_id=world.tenant_id, run_id=run.id
        )


async def test_读接口不碰目标端(
    async_session: AsyncSession, world: _World, install_reader: Any
) -> None:
    """图表面板不该依赖数据面的存活：作业跑完（或目标端不可达）之后它仍然要可读。"""
    run = await _make_run(async_session, world)
    reader = install_reader(valid_rows=95, rejected_rows=5, by_rule={"r_null": 5})
    await quality_service.finalize_report(async_session, tenant_id=world.tenant_id, run_id=run.id)
    reader.calls.clear()

    await quality_service.get_report(async_session, tenant_id=world.tenant_id, run_id=run.id)
    await quality_service.error_code_summary(
        async_session, tenant_id=world.tenant_id, run_id=run.id
    )
    await quality_service.list_error_records(
        async_session, tenant_id=world.tenant_id, run_id=run.id
    )
    await quality_service.page_error_records(
        async_session, tenant_id=world.tenant_id, run_id=run.id
    )

    assert reader.calls == []


async def test_汇总形状与报告同源(
    async_session: AsyncSession, world: _World, install_reader: Any
) -> None:
    run = await _make_run(async_session, world)
    install_reader(valid_rows=90, rejected_rows=10, by_rule={"r_null": 10}, matching_count=4)
    dto = await quality_service.finalize_report(
        async_session, tenant_id=world.tenant_id, run_id=run.id
    )

    summary = await quality_service.error_code_summary(
        async_session, tenant_id=world.tenant_id, run_id=run.id
    )

    assert summary.report_id == dto.report_id
    assert summary.passed is dto.passed is False
    assert summary.blocked_reason == dto.blocked_reason == "over_budget"
    assert summary.failed_rules == ["r_null"]
    assert (summary.valid_rows, summary.rejected_rows, summary.processed_rows) == (90, 10, 100)
    assert summary.read_rows == 100
    assert summary.reject_rate == pytest.approx(0.1)
    assert summary.reject_rate_budget == pytest.approx(0.05)
    assert summary.contract_sha256 == dto.contract_sha256
    assert summary.rule_hits == {"r_null": 10, "r_amount": 4}


async def test_汇总的字段TopN与副本行数(
    async_session: AsyncSession, world: _World, install_reader: Any
) -> None:
    run = await _make_run(async_session, world)
    install_reader(valid_rows=95, rejected_rows=5, by_rule={"r_null": 5})
    await quality_service.finalize_report(async_session, tenant_id=world.tenant_id, run_id=run.id)
    await _add_records(async_session, run.id, 3, field_name="id")
    await _add_records(async_session, run.id, 1, field_name="phone", error_code="E_REGEX_MISMATCH")

    summary = await quality_service.error_code_summary(
        async_session, tenant_id=world.tenant_id, run_id=run.id
    )

    assert [(item.field_name, item.count) for item in summary.top_fields] == [("id", 3), ("phone", 1)]
    assert summary.error_records_copied == 4
    # 副本 4 行 < 拒绝 5 行 → 明细不全，这件事要说得出来
    assert summary.error_records_truncated is True


async def test_错误副本游标分页(
    async_session: AsyncSession, world: _World, install_reader: Any
) -> None:
    run = await _make_run(async_session, world)
    install_reader()
    ids = await _add_records(async_session, run.id, 5)

    first = await quality_service.list_error_records(
        async_session, tenant_id=world.tenant_id, run_id=run.id, limit=2
    )
    assert [item.id for item in first.items] == ids[:2]
    assert first.next_cursor == quality_service.encode_cursor(ids[1])

    second = await quality_service.list_error_records(
        async_session, tenant_id=world.tenant_id, run_id=run.id, limit=2, cursor=first.next_cursor
    )
    assert [item.id for item in second.items] == ids[2:4]

    third = await quality_service.list_error_records(
        async_session, tenant_id=world.tenant_id, run_id=run.id, limit=2, cursor=second.next_cursor
    )
    assert [item.id for item in third.items] == ids[4:]
    assert third.next_cursor is None  # 到底了


async def test_错误副本游标分页带过滤(
    async_session: AsyncSession, world: _World, install_reader: Any
) -> None:
    run = await _make_run(async_session, world)
    install_reader()
    await _add_records(async_session, run.id, 3, error_code="E_NULL_PK", rule_id="r_null")
    await _add_records(
        async_session, run.id, 2, error_code="E_REGEX_MISMATCH", rule_id="r_phone", field_name="phone"
    )

    by_code = await quality_service.list_error_records(
        async_session, tenant_id=world.tenant_id, run_id=run.id, error_code="E_REGEX_MISMATCH"
    )
    assert len(by_code.items) == 2
    assert {item.rule_id for item in by_code.items} == {"r_phone"}

    by_field = await quality_service.list_error_records(
        async_session, tenant_id=world.tenant_id, run_id=run.id, field_name="id", rule_id="r_null"
    )
    assert len(by_field.items) == 3


async def test_无效游标被拒绝(
    async_session: AsyncSession, world: _World, install_reader: Any
) -> None:
    """游标必须不透明：允许客户端直接传整数，等于把「从任意位置开始翻」当成合法输入，
    而它跳过的那些行不会有人注意到。"""
    run = await _make_run(async_session, world)
    install_reader()

    for bad in ("5", "er:abc", "xx:1", "er:"):
        with pytest.raises(ValidationError):
            await quality_service.list_error_records(
                async_session, tenant_id=world.tenant_id, run_id=run.id, cursor=bad
            )


async def test_游标分页的limit越界报错(
    async_session: AsyncSession, world: _World, install_reader: Any
) -> None:
    run = await _make_run(async_session, world)
    install_reader()

    for bad in (0, quality_service.MAX_LIMIT + 1):
        with pytest.raises(ValidationError):
            await quality_service.list_error_records(
                async_session, tenant_id=world.tenant_id, run_id=run.id, limit=bad
            )


async def test_错误副本页码分页(
    async_session: AsyncSession, world: _World, install_reader: Any
) -> None:
    """前端契约是 ``{items, meta}``（``types/run.ts``），所以这里要回总数。"""
    run = await _make_run(async_session, world)
    install_reader()
    ids = await _add_records(async_session, run.id, 5)

    items, total = await quality_service.page_error_records(
        async_session, tenant_id=world.tenant_id, run_id=run.id, page_no=2, page_size=2
    )
    assert total == 5
    assert [item.id for item in items] == ids[2:4]

    with pytest.raises(ValidationError):
        await quality_service.page_error_records(
            async_session, tenant_id=world.tenant_id, run_id=run.id, page_no=0
        )


async def test_另一条运行的错误副本不会串进来(
    async_session: AsyncSession, world: _World, install_reader: Any
) -> None:
    """同一张错误表可被多次运行共用（``run_id`` 是数据面注入的常量列），
    所以控制面每次读都必须带 ``run_id``。漏了的话两次运行的错误行互相污染，
    而报告上只是「数字大了一点」。"""
    await _allow_multiple_runs(async_session)
    run = await _make_run(async_session, world, key="k1")
    other_run = await _make_run(async_session, world, key="k2")
    install_reader()
    await _add_records(async_session, run.id, 2)
    await _add_records(async_session, other_run.id, 3)

    page = await quality_service.list_error_records(
        async_session, tenant_id=world.tenant_id, run_id=run.id
    )
    assert len(page.items) == 2
    assert {item.run_id for item in page.items} == {run.id}


# ================================================================ 前置条件


@pytest.mark.parametrize("missing", ["shadow", "error"])
async def test_缺少影子表或错误表时拒绝(
    async_session: AsyncSession, world: _World, install_reader: Any, missing: str
) -> None:
    """没有它们的运行根本没经过分流：算出一份「通过」的报告是最坏的结果。"""
    run = await _make_run(
        async_session,
        world,
        shadow=None if missing == "shadow" else SHADOW,
        error=None if missing == "error" else ERROR,
    )
    install_reader(valid_rows=95, rejected_rows=5)

    with pytest.raises(ConflictError):
        await quality_service.finalize_report(
            async_session, tenant_id=world.tenant_id, run_id=run.id
        )


async def test_未注册读取器时拒绝(
    async_session: AsyncSession, world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**不降级成「全部为 0」**：0 是个完全合法的报告值，静默降级会让
    「目标端没接上」伪装成一次干净的空跑。"""
    run = await _make_run(async_session, world)
    monkeypatch.setattr(quality_service, "_reader_factory", None)

    with pytest.raises(ConflictError) as excinfo:
        await quality_service.finalize_report(
            async_session, tenant_id=world.tenant_id, run_id=run.id
        )
    assert "未注册" in str(excinfo.value)
    with pytest.raises(ConflictError):
        quality_service.get_error_table_reader_factory()


async def test_契约编译不过时报409(
    async_session: AsyncSession, install_reader: Any
) -> None:
    """冻结时能过、运行期末尾编译不过，说明制品被绕过 ORM 改过。
    报 409 并留下原文，而不是让一个 ValueError 以 500 的形式冒上去
    （那条栈会被读成「代码有 bug」）。"""
    # 512 位上限之外的 pattern：编译期就该拒
    broken = dict(RULE_NULL, params={"pattern": "a" * 600}, rule_type="regex")
    world = await _seed(async_session, rules=[broken])
    run = await _make_run(async_session, world)
    install_reader(valid_rows=95, rejected_rows=5)

    with pytest.raises(ConflictError) as excinfo:
        await quality_service.finalize_report(
            async_session, tenant_id=world.tenant_id, run_id=run.id
        )
    assert "质量契约无法编译" in str(excinfo.value)


async def test_目标数据源缺失时报409(
    async_session: AsyncSession, install_reader: Any
) -> None:
    """方言取自目标数据源：取不到就没法判断该按 Doris 还是 MySQL 渲染谓词。"""
    world = await _seed(async_session, with_target=False)
    run = await _make_run(async_session, world)
    install_reader(valid_rows=95, rejected_rows=5)

    with pytest.raises(ConflictError) as excinfo:
        await quality_service.finalize_report(
            async_session, tenant_id=world.tenant_id, run_id=run.id
        )
    assert "目标数据源" in str(excinfo.value)


# ================================================================ 读闸


async def test_没有项目角色的用户读不了(
    async_session: AsyncSession, world: _World, install_reader: Any
) -> None:
    """读闸在 service 而不是路由依赖上：路由级依赖判的是「在**任一**项目里持有该角色」，
    而这里要判的是「在**这个运行所属**项目里持有」。"""
    run = await _make_run(async_session, world)
    stranger = await _user(async_session, world.tenant_id, "stranger")
    await async_session.commit()
    install_reader()

    with pytest.raises(RoleRequiredError):
        await quality_service.require_read_for_run(async_session, world.ctx(stranger.id), run.id)

    # 项目里的职责槽（operator）能读
    await quality_service.require_read_for_run(async_session, world.ctx(), run.id)


async def test_跨租户的读闸回404(
    async_session: AsyncSession, world: _World, install_reader: Any
) -> None:
    run = await _make_run(async_session, world)
    other = await _outsider(async_session)

    with pytest.raises(NotFoundError):
        await quality_service.require_read_for_run(async_session, other, run.id)


# ================================================================ 路由


def test_五个路由都在openapi上() -> None:
    """不数 ``app.routes``：FastAPI 用惰性包裹的子路由，按 ``route.path`` 遍历看不到它们。

    这里用 openapi 文档（它把子路由拍平），空实现或漏挂载都会在这里红。
    """
    from main import app

    paths = app.openapi()["paths"]
    for path in (
        "/api/v1/runs/{run_id}/quality-report",
        "/api/v1/runs/{run_id}/error-records",
        "/api/v1/monitor/runs/{run_id}/quality-report",
        "/api/v1/monitor/runs/{run_id}/errors",
        "/api/v1/monitor/runs/{run_id}/errors/summary",
    ):
        assert path in paths, f"{path} 没挂上"


def test_错误明细DTO里没有原文列() -> None:
    """把「只存摘要」也钉在**出网形状**上：DTO 多一个字段就是一条回流通道。"""
    from schemas.quality import ErrorRecordDTO

    assert set(ErrorRecordDTO.model_fields) == {
        "id",
        "run_id",
        "rule_id",
        "error_code",
        "field_name",
        "payload_digest",
        "reason",
        "occurred_at",
    }
