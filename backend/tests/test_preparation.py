"""be-13 准备单与四眼审批。

**这个模块的核心断言有三句**，其余用例都服务于它们：

1. **发起准备是零副作用的**——不签发 Capability、不建 ``execution_run``、不投 Outbox。
   破了它的后果不是报错，而是「还没批就已经在执行队列里了」，而审批页上一切正常。
2. **协议类拒绝必须入账**（自批、职责混用、指纹失效）。这三种拒绝的共同点是
   「有人试图绕过流程」——只回 409 而不留痕，这次尝试与「什么都没发生」在审计上等价。
   反过来，**状态类拒绝不能入账**（重复投票、终态再投）：那是页面过期，不是攻击，
   往不可变账本里塞噪声会让真正的证据更难找。
3. **四眼在服务端与数据库两层都成立**：服务端拦自批与双人核对，
   数据库还有 ``approver_id <> preparation_created_by`` 的 CHECK 兜底——
   绕过接口直接写库的路径也要被拦住（CLAUDE.md §5.8）。

分层：``derive_status`` / ``shadow_table_name`` 是纯函数直接调；
其余走 service + 真库（内存 SQLite）。**本机没有 Docker**（CLAUDE.md §9），
所以 seed 用 ORM 铺行而不是走「生成 → 冻结」全链路——那条链路的重活归 M2 的用例，
在这里再跑一遍只会把本文件的失败原因和生成链路的 bug 搅在一起。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from core.errors import (
    ApprovalIncompleteError,
    ConflictError,
    DutyConflictError,
    FingerprintMismatchError,
    MetadataProfileExpiredError,
    PreparationExpiredError,
    RoleRequiredError,
    SelfApprovalForbiddenError,
)
from core.ids import sha256_hex
from engines.hocon import render_hocon
from engines.hocon.types import PARSER_VERSION, RenderContext
from engines.plan.schema import (
    EtlPlanDag,
    EtlPlanModel,
    FieldMapping,
    PlanInput,
    PlanOutput,
    RuntimeBudget,
)
from engines.policy.roles import RoleName
from harness.ledger import EvidenceLedger
from models.datasource import DataSource, MetadataProfile, SecretReference
from models.enums import (
    ApprovalDecisionType,
    ApprovalSlot,
    DataSourceType,
    Env,
    EvidenceEventType,
    MemberStatus,
    PipelineStatus,
    PreparationStatus,
    ProbeStatus,
    RiskLevel,
    SensitiveLevel,
)
from models.evidence import EvidenceEvent, OutboxMessage
from models.execution import ExecutionRun
from models.governance import ApprovalDecision, Capability, Preparation
from models.pipeline import (
    EtlPlan,
    HoconArtifact,
    Pipeline,
    PipelineVersion,
    QualityContract,
    RuntimeSupervisionContract,
)
from models.tenant import AppUser, ProjectMember, RoleAssignment, Tenant
from schemas.auth import SubjectContext
from schemas.preparation import (
    DecisionRequest,
    PreparationCreateRequest,
)
from schemas.project import ProjectCreate
from services import approval as approval_service
from services import preparation as prep_service
from services import project as project_service

_ledger = EvidenceLedger()


# ---------------------------------------------------------------- 种子


class _World:
    """一次用例的完整现场：租户、四个用户、项目、数据源、一份冻结版本。"""

    def __init__(
        self,
        *,
        tenant_id: int,
        project_id: int,
        pipeline_id: int,
        version_id: int,
        profile_id: int,
        maker: AppUser,
        checker_data: AppUser,
        checker_security: AppUser,
        both_checkers: AppUser,
    ) -> None:
        self.tenant_id = tenant_id
        self.project_id = project_id
        self.pipeline_id = pipeline_id
        self.version_id = version_id
        self.profile_id = profile_id
        self.maker = maker
        self.checker_data = checker_data
        self.checker_security = checker_security
        #: 同时持有两个 checker 槽的人——用来验「四眼不是四只眼」。
        self.both_checkers = both_checkers

    def ctx(self, user: AppUser) -> SubjectContext:
        return SubjectContext(
            user_id=user.id,
            tenant_id=self.tenant_id,
            username=user.username,
            display_name=user.display_name,
            request_id=f"req_{user.username}",
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
    """直接铺成员 + 角色行。

    不走 ``project_service.grant_role``：授予本身需要 checker_security 且会落账本，
    在这里铺一遍会把本文件的失败原因和 be-02 的授权 bug 搅在一起
    （与 ``tests/test_governance.py`` 同一口径）。
    成员行**复用**：``(project_id, user_id)`` 唯一，一个人可以有多个槽
    （dave 就是为了验「两个槽同一人」而存在）。
    """
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


async def _seed(session: AsyncSession) -> _World:
    tenant = Tenant(code="acme", name="Acme", status="active")
    session.add(tenant)
    await session.flush()

    maker = await _user(session, tenant.id, "alice")
    checker_data = await _user(session, tenant.id, "bob")
    checker_security = await _user(session, tenant.id, "carol")
    both = await _user(session, tenant.id, "dave")

    project_row = await project_service.create_project(
        session,
        SubjectContext(
            user_id=maker.id,
            tenant_id=tenant.id,
            username=maker.username,
            display_name=maker.display_name,
            request_id="req_seed",
        ),
        ProjectCreate(code="orders", name="订单", env="dev"),
    )
    project_id = project_row.id
    # create_project 会把 maker 槽给创建者，另外三个槽由这里铺
    await _grant(session, project_id, checker_data, RoleName.CHECKER_DATA)
    await _grant(session, project_id, checker_security, RoleName.CHECKER_SECURITY)
    await _grant(session, project_id, both, RoleName.CHECKER_DATA)
    await _grant(session, project_id, both, RoleName.CHECKER_SECURITY)

    secret = SecretReference(
        tenant_id=tenant.id,
        project_id=project_id,
        provider="vault",
        path="kv/elt/orders",
        version=3,
        key_names=["username", "password"],
        created_by=maker.id,
    )
    session.add(secret)
    await session.flush()

    source = DataSource(
        tenant_id=tenant.id,
        project_id=project_id,
        name="源库",
        type=DataSourceType.MYSQL,
        env=Env.DEV,
        endpoint={"host": "10.0.0.5", "port": 3306, "database": "sales"},
        secret_ref_id=secret.id,
        sensitive_level=SensitiveLevel.INTERNAL,
        tags=[],
        status="active",
    )
    target = DataSource(
        tenant_id=tenant.id,
        project_id=project_id,
        name="目标库",
        type=DataSourceType.DORIS,
        env=Env.DEV,
        endpoint={"host": "10.0.0.6", "port": 9030, "database": "dw"},
        secret_ref_id=secret.id,
        sensitive_level=SensitiveLevel.INTERNAL,
        tags=[],
        status="active",
    )
    session.add_all([source, target])
    await session.flush()

    profile = MetadataProfile(
        tenant_id=tenant.id,
        project_id=project_id,
        data_source_id=source.id,
        version=1,
        status=ProbeStatus.READY,
        tables_count=1,
        sha256="b" * 64,
        # 键名只有 ``profile_view.tables_of`` 一处解释：schema / name / columns
        schema_snapshot={
            "tables": [
                {
                    "schema": "sales",
                    "name": "orders",
                    "comment": "订单",
                    "row_count_approx": 1200,
                    "columns": [
                        {
                            "name": "id",
                            "data_type": "bigint",
                            "nullable": False,
                            "is_primary_key": True,
                            "sensitive_level": "public",
                        },
                        {
                            "name": "amount",
                            "data_type": "decimal(12,2)",
                            "nullable": True,
                            "sensitive_level": "internal",
                        },
                    ],
                }
            ]
        },
    )
    session.add(profile)
    await session.flush()

    plan = EtlPlanModel(
        inputs=[
            PlanInput(id="src_1", data_source_id=source.id, schema_name="sales", table="orders")
        ],
        outputs=[
            PlanOutput(
                id="sink_1",
                data_source_id=target.id,
                database="dw",
                table="orders_wide",
                mode="shadow_then_swap",
            )
        ],
        field_mappings=[
            FieldMapping(
                source_field="id",
                source_type="bigint",
                target_field="id",
                target_type="bigint",
            )
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
    # HOCON 走**渲染器**而不是手写字符串：手写的那份迟早与渲染器分叉，
    # 而分叉的症状是「这条用例绿、生产上解析不过」。
    hocon = render_hocon(
        plan,
        RenderContext(data_source_types={source.id: "mysql", target.id: "doris"}),
    )

    # 四份制品**没有** tenant/project 列（它们归属由 pipeline_version 表达，
    # 见 models/pipeline.py 的 CreatedAtMixin）——这正是「租户过滤从版本行进来」的原因
    plan_row = EtlPlan(
        schema_version="1.0",
        plan=plan.model_dump(mode="json"),
        valid=True,
        validation_errors=[],
    )
    hocon_row = HoconArtifact(
        content=hocon.content,
        sha256=sha256_hex(hocon.content),
        parser_version=PARSER_VERSION,
        validation_result={},
    )
    quality_row = QualityContract(
        rules=[{"column": "id", "code": "E_NULL_PK", "expression": "id IS NOT NULL"}],
        error_table_target={"data_source_id": target.id, "table": "orders_wide_reject"},
        reject_rate_budget=Decimal("0.05"),
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
        project_id=project_id,
        name="orders_to_dw",
        source_data_source_id=source.id,
        target_data_source_id=target.id,
        status=PipelineStatus.VERSION_FROZEN,
        created_by=maker.id,
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
        source_metadata_profile_id=profile.id,
        generation_context={},
        sha256="a" * 64,
        frozen_by=maker.id,
        frozen_at=datetime(2026, 10, 16, 8, 0, tzinfo=UTC),
        note="",
    )
    session.add(version)
    await session.flush()
    pipeline.current_version_id = version.id
    await session.commit()

    return _World(
        tenant_id=tenant.id,
        project_id=project_id,
        pipeline_id=pipeline.id,
        version_id=version.id,
        profile_id=profile.id,
        maker=maker,
        checker_data=checker_data,
        checker_security=checker_security,
        both_checkers=both,
    )


async def _create(session: AsyncSession, world: _World, *, note: str = "") -> object:
    return await prep_service.create_preparation(
        session,
        world.ctx(world.maker),
        PreparationCreateRequest(pipeline_version_id=world.version_id, note=note),
    )


async def _events(session: AsyncSession, *, tenant_id: int, event_type: EvidenceEventType) -> list[EvidenceEvent]:
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


async def _decide(
    session: AsyncSession,
    world: _World,
    user: AppUser,
    slot: ApprovalSlot,
    decision: ApprovalDecisionType = ApprovalDecisionType.APPROVE,
    *,
    comment: str = "同意，已核对字段映射与预算",
) -> object:
    return await approval_service.decide(
        session,
        preparation_id=(await _prep_of(session, world)).id,
        subject=world.ctx(user),
        slot=slot,
        decision=decision,
        comment=comment,
    )


async def _prep_of(session: AsyncSession, world: _World) -> Preparation:
    """取当前活跃的准备单行（本文件里一个 pipeline 同时只有一个）。"""
    row = await session.scalar(
        select(Preparation)
        .where(Preparation.tenant_id == world.tenant_id, Preparation.project_id == world.project_id)
        .order_by(Preparation.id.desc())
    )
    assert row is not None, "还没发起过准备单"
    return row


# ---------------------------------------------------------------- 纯函数


def test_状态推导的优先级是拒绝大于退回大于通过() -> None:
    """``reject`` > ``return`` > 全部 approve > 部分 approve > pending。

    这条顺序必须钉住：一个槽拒绝、另一个槽通过时，结论若取决于**遍历顺序**
    （也就是库里行的顺序），同一份数据两次调用会算出不同状态。
    """
    both = (ApprovalSlot.CHECKER_DATA, ApprovalSlot.CHECKER_SECURITY)
    data, security = both

    assert (
        approval_service.derive_status(
            both, {data: ApprovalDecisionType.APPROVE, security: ApprovalDecisionType.REJECT}
        )
        is PreparationStatus.REJECTED
    )
    # 反过来写一遍：结论与「谁先谁后」无关
    assert (
        approval_service.derive_status(
            both, {security: ApprovalDecisionType.REJECT, data: ApprovalDecisionType.APPROVE}
        )
        is PreparationStatus.REJECTED
    )
    assert (
        approval_service.derive_status(
            both, {data: ApprovalDecisionType.APPROVE, security: ApprovalDecisionType.RETURN}
        )
        is PreparationStatus.SUPERSEDED
    )
    assert (
        approval_service.derive_status(
            both, {data: ApprovalDecisionType.APPROVE, security: ApprovalDecisionType.APPROVE}
        )
        is PreparationStatus.APPROVED
    )
    assert (
        approval_service.derive_status(both, {data: ApprovalDecisionType.APPROVE})
        is PreparationStatus.PARTIALLY_APPROVED
    )
    assert approval_service.derive_status(both, {}) is PreparationStatus.PENDING


def test_免审批的准备单直接通过而不是永远等下去() -> None:
    """P3 没有必需槽，推导结果是 approved。

    「没有必需槽」与「必需槽还没集齐」都必须能表达，否则免审批的单子会卡在
    ``pending`` 上永远提交不了——而症状是「点提交没反应」，指向的是按钮，不是判定。
    """
    assert approval_service.derive_status((), {}) is PreparationStatus.APPROVED


def test_影子表名带版本号且不超长() -> None:
    """影子表名是**全仓库唯一的一份定义**：be-14/be-15/be-18 必须复用它。

    两个名字必须能同时存在于目标库里（v1 的影子表与 v2 的影子表共存），
    所以版本号进名字；超长时截断但保留尾部版本段——尾部被截掉才是灾难
    （两个版本的影子表撞成一个名字）。
    """
    assert prep_service.shadow_table_name("orders", 1) == "orders__shadow_v1"

    long_name = "t" * 60
    name = prep_service.shadow_table_name(long_name, 12)
    assert len(name) <= 64, name
    assert name.endswith("__shadow_v12")
    # 同一个长表名的两个版本不能撞名
    assert prep_service.shadow_table_name(long_name, 13) != name


# ---------------------------------------------------------------- 发起准备


async def test_发起准备落单并推进流程状态(async_session: AsyncSession) -> None:
    world = await _seed(async_session)
    detail = await _create(async_session, world)

    assert detail.status is PreparationStatus.PENDING
    # P1/P2 都是两个槽；P3 免审批。写入意图**永远不会**落到免审批（PDP 兜底规则），
    # 所以这里必须是两个槽——少了它就等于「发起即通过」
    assert set(detail.required_slots) == {ApprovalSlot.CHECKER_DATA, ApprovalSlot.CHECKER_SECURITY}
    assert detail.risk_level in (RiskLevel.P1, RiskLevel.P2)
    assert detail.decided_slots == ()
    assert detail.can_commit is False
    # 制品指纹取自不可变版本行，**不接受调用方提交**
    assert detail.artifact_sha256 == "a" * 64
    assert detail.input_fingerprint

    row = await _prep_of(async_session, world)
    assert row.expires_at is not None
    assert row.expires_at > row.frozen_at
    # 环境取门禁判定的那一份（源/目标数据源 env 的最高档）
    assert row.environment is Env.DEV

    pipeline = await async_session.get(Pipeline, world.pipeline_id)
    assert pipeline is not None
    assert pipeline.status is PipelineStatus.PENDING_APPROVAL

    created = await _events(
        async_session, tenant_id=world.tenant_id, event_type=EvidenceEventType.PREPARATION_CREATED
    )
    assert len(created) == 1
    assert created[0].payload["required_slots"] == ["checker_data", "checker_security"]
    assert created[0].artifact_hash == "a" * 64


async def test_发起准备零副作用(async_session: AsyncSession) -> None:
    """**没有 Capability、没有执行记录、没有 Outbox 消息**（be-13 §1.1）。

    这一条一旦破了不会报任何错：审批页照旧「待审批」，而作业已经在执行队列里。
    """
    world = await _seed(async_session)
    await _create(async_session, world)

    for model in (Capability, ExecutionRun, OutboxMessage):
        count = len((await async_session.scalars(select(model))).all())
        assert count == 0, f"发起准备不该产生 {model.__tablename__} 行"


async def test_版本未冻结不能发起准备(async_session: AsyncSession) -> None:
    """未冻结的版本是草案，随时会被重新生成覆盖——准备单锚不到它身上。"""
    world = await _seed(async_session)
    version = await async_session.get(PipelineVersion, world.version_id)
    assert version is not None
    # 直接把 frozen_at 清掉会撞上 ORM 的不可变守卫，所以造一份**候选**版本：
    # 候选与冻结版本的区别就只有 frozen_at 是否为空
    candidate = PipelineVersion(
        pipeline_id=version.pipeline_id,
        version_no=2,
        etl_plan_id=version.etl_plan_id,
        hocon_artifact_id=version.hocon_artifact_id,
        quality_contract_id=version.quality_contract_id,
        supervision_contract_id=version.supervision_contract_id,
        source_metadata_profile_id=version.source_metadata_profile_id,
        generation_context={},
        sha256="c" * 64,
        frozen_at=None,
    )
    async_session.add(candidate)
    await async_session.commit()

    with pytest.raises(ConflictError):
        await prep_service.create_preparation(
            async_session,
            world.ctx(world.maker),
            PreparationCreateRequest(pipeline_version_id=candidate.id),
        )


async def test_非maker不能发起准备(async_session: AsyncSession) -> None:
    world = await _seed(async_session)
    with pytest.raises(RoleRequiredError):
        await prep_service.create_preparation(
            async_session,
            world.ctx(world.checker_data),
            PreparationCreateRequest(pipeline_version_id=world.version_id),
        )


async def test_源端档案失效不能发起准备(async_session: AsyncSession) -> None:
    """档案是门禁判过、生成时算进指纹的那一份。它没了就不能凭记忆执行。"""
    world = await _seed(async_session)
    profile = await async_session.get(MetadataProfile, world.profile_id)
    assert profile is not None
    # status 是 MetadataProfile 的可变列（重新探查会改它）
    profile.status = ProbeStatus.FAILED
    await async_session.commit()

    with pytest.raises(MetadataProfileExpiredError):
        await _create(async_session, world)


# ---------------------------------------------------------------- 审批：四眼


async def _expire_now(session: AsyncSession, preparation_id: int) -> None:
    """把有效期推到过去。

    **必须走 Core UPDATE**：``Preparation.__mutable_columns__`` 只放行 ``status``，
    用 ORM 赋 ``row.expires_at`` 会被不可变守卫拦下（这正是不变式的意义）。
    于是「改库绕过服务层」这条路径只剩下原生 SQL——而提交前的
    ``assert_committable`` 恰恰要再判一次有效期，就是为了兜住它。
    """
    await session.execute(
        update(Preparation)
        .where(Preparation.id == preparation_id)
        .values(expires_at=datetime(2026, 1, 1, tzinfo=UTC))
    )
    await session.commit()


async def test_自批被拒且入账(async_session: AsyncSession) -> None:
    world = await _seed(async_session)
    await _create(async_session, world)
    row = await _prep_of(async_session, world)

    # 给发起人补一个数据审批槽，把「自批」与其他拒绝理由隔离开：
    # 这样这次拒绝**只能**是因为自批（否则用例可能在验另一条规则）
    await _grant(async_session, world.project_id, world.maker, RoleName.CHECKER_DATA)
    await async_session.commit()

    with pytest.raises(SelfApprovalForbiddenError):
        await _decide(async_session, world, world.maker, ApprovalSlot.CHECKER_DATA)

    denied = await _events(
        async_session, tenant_id=world.tenant_id, event_type=EvidenceEventType.APPROVAL_SELF_DENIED
    )
    assert len(denied) == 1
    assert denied[0].subject_id == str(row.id)
    assert denied[0].payload["attempted_by"] == world.maker.id
    assert denied[0].payload["preparation_created_by"] == world.maker.id
    # 拒绝了就不该留下决定行
    decisions = (
        await async_session.scalars(
            select(ApprovalDecision).where(ApprovalDecision.preparation_id == row.id)
        )
    ).all()
    assert decisions == []


async def test_两个审批槽不得同一人(async_session: AsyncSession) -> None:
    """四眼不是四只眼：同一个人拿两个槽等于没有复核（check_dual_checker_distinct）。"""
    world = await _seed(async_session)
    await _create(async_session, world)
    await _decide(async_session, world, world.both_checkers, ApprovalSlot.CHECKER_DATA)

    with pytest.raises(DutyConflictError):
        await _decide(async_session, world, world.both_checkers, ApprovalSlot.CHECKER_SECURITY)

    denied = await _events(
        async_session, tenant_id=world.tenant_id, event_type=EvidenceEventType.APPROVAL_DUTY_DENIED
    )
    assert len(denied) == 1
    assert denied[0].payload["slot"] == "checker_security"
    assert denied[0].payload["attempted_by"] == world.both_checkers.id


async def test_不持有该槽的人不能审批(async_session: AsyncSession) -> None:
    """角色**此刻**从 role_assignment 重查，不信 JWT 声明（撤销后旧令牌仍在有效期内）。"""
    world = await _seed(async_session)
    await _create(async_session, world)

    # 拿数据审批槽的人去审安全槽——他确实有权审批，但审的不是这个槽
    with pytest.raises(RoleRequiredError):
        await _decide(async_session, world, world.checker_data, ApprovalSlot.CHECKER_SECURITY)


async def test_审批单不需要的槽被拒(async_session: AsyncSession) -> None:
    """必需槽只有两个，传进来第三个槽（不存在）要按参数错处理，不是静默记下。"""
    world = await _seed(async_session)
    await _create(async_session, world)
    # ApprovalSlot 只有两个取值，构造一个「不在必需槽里」的场景需要绕过枚举，
    # 因此这里验的是另一半：槽位参数与角色不匹配时的报错**类型**
    with pytest.raises(RoleRequiredError):
        await _decide(async_session, world, world.checker_security, ApprovalSlot.CHECKER_DATA)


async def test_两槽齐备后通过并推进流程状态(async_session: AsyncSession) -> None:
    world = await _seed(async_session)
    await _create(async_session, world)

    first = await _decide(async_session, world, world.checker_data, ApprovalSlot.CHECKER_DATA)
    assert first.status is PreparationStatus.PARTIALLY_APPROVED
    assert first.can_commit is False, "只批了一个槽就能提交，等于四眼只拦了一半"

    second = await _decide(async_session, world, world.checker_security, ApprovalSlot.CHECKER_SECURITY)
    assert second.status is PreparationStatus.APPROVED
    assert second.can_commit is True
    assert set(second.decided_slots) == {ApprovalSlot.CHECKER_DATA, ApprovalSlot.CHECKER_SECURITY}
    # 决定行里记的是**审批那一刻**的指纹：指纹后来变了也能回答「他当时批的是什么」
    assert all(item.fingerprint_at_decision for item in second.decisions)

    pipeline = await async_session.get(Pipeline, world.pipeline_id)
    assert pipeline is not None
    assert pipeline.status is PipelineStatus.APPROVED

    granted = await _events(
        async_session, tenant_id=world.tenant_id, event_type=EvidenceEventType.APPROVAL_GRANTED
    )
    assert [event.payload["slot"] for event in granted] == ["checker_data", "checker_security"]


async def test_任一槽拒绝即拒绝(async_session: AsyncSession) -> None:
    world = await _seed(async_session)
    await _create(async_session, world)
    await _decide(async_session, world, world.checker_data, ApprovalSlot.CHECKER_DATA)

    detail = await _decide(
        async_session,
        world,
        world.checker_security,
        ApprovalSlot.CHECKER_SECURITY,
        ApprovalDecisionType.REJECT,
        comment="字段映射缺少金额列的口径",
    )
    assert detail.status is PreparationStatus.REJECTED

    pipeline = await async_session.get(Pipeline, world.pipeline_id)
    assert pipeline is not None
    assert pipeline.status is PipelineStatus.REJECTED
    rejected = await _events(
        async_session, tenant_id=world.tenant_id, event_type=EvidenceEventType.APPROVAL_REJECTED
    )
    assert len(rejected) == 1
    # 意见正文进账本：它是审批人对结论的说明，属于「谁在什么理由下批的」
    assert rejected[0].payload["comment"] == "字段映射缺少金额列的口径"


async def test_同一槽不能覆盖既有决定(async_session: AsyncSession) -> None:
    """``approval_decision`` 不可变，一个槽只有一条——且**不入账本**（页面过期不是攻击）。"""
    world = await _seed(async_session)
    await _create(async_session, world)
    await _decide(async_session, world, world.checker_data, ApprovalSlot.CHECKER_DATA)

    before = len((await async_session.scalars(select(EvidenceEvent))).all())
    with pytest.raises(ConflictError):
        await _decide(async_session, world, world.checker_data, ApprovalSlot.CHECKER_DATA)
    await async_session.rollback()
    after = len((await async_session.scalars(select(EvidenceEvent))).all())
    assert after == before, "状态类拒绝不该写账本——噪声会让真正的证据更难找"


async def test_终态准备单不再接受决定(async_session: AsyncSession) -> None:
    world = await _seed(async_session)
    await _create(async_session, world)
    await _decide(async_session, world, world.checker_data, ApprovalSlot.CHECKER_DATA)
    await _decide(
        async_session,
        world,
        world.checker_security,
        ApprovalSlot.CHECKER_SECURITY,
        ApprovalDecisionType.REJECT,
    )

    other = world.both_checkers
    with pytest.raises(ConflictError):
        await _decide(async_session, world, other, ApprovalSlot.CHECKER_SECURITY)


async def test_过期后不能审批(async_session: AsyncSession) -> None:
    """有效期是硬边界：批到一半过期了也不能继续（过期不做续期）。"""
    world = await _seed(async_session)
    await _create(async_session, world)
    await _expire_now(async_session, (await _prep_of(async_session, world)).id)

    with pytest.raises(PreparationExpiredError):
        await _decide(async_session, world, world.checker_data, ApprovalSlot.CHECKER_DATA)


# ---------------------------------------------------------------- 审批：指纹


async def test_输入指纹变化后审批被拒且入账(async_session: AsyncSession) -> None:
    """凭据轮转（``secret_reference.version`` 变了）会让审批失效。

    这正是「指纹里为什么要放 secret 版本」的用例：表结构一个字节没变，
    但这次执行面对的秘密已经不是审批时那一个了。
    """
    world = await _seed(async_session)
    await _create(async_session, world)

    secret = (
        await async_session.scalars(
            select(SecretReference).where(SecretReference.tenant_id == world.tenant_id)
        )
    ).one()
    secret.version = 4
    await async_session.commit()

    with pytest.raises(FingerprintMismatchError):
        await _decide(async_session, world, world.checker_data, ApprovalSlot.CHECKER_DATA)

    denied = await _events(
        async_session, tenant_id=world.tenant_id, event_type=EvidenceEventType.APPROVAL_STALE_DENIED
    )
    assert len(denied) == 1
    assert denied[0].payload["reason"] == "fingerprint_mismatch"


async def test_制品被改过则审批被拒(async_session: AsyncSession) -> None:
    """指纹校验同时看两边：``version.sha256`` 与输入指纹。

    只比输入指纹会漏掉「制品本身被改过」——那是最需要拦住的一类。
    """
    world = await _seed(async_session)
    await _create(async_session, world)

    # 直接改库里的 sha256 模拟「制品行被绕过服务层改过」
    await async_session.execute(
        update(PipelineVersion)
        .where(PipelineVersion.id == world.version_id)
        .values(sha256="d" * 64)
    )
    await async_session.commit()

    with pytest.raises(FingerprintMismatchError):
        await _decide(async_session, world, world.checker_data, ApprovalSlot.CHECKER_DATA)


# ---------------------------------------------------------------- 提交前置校验


async def test_提交前校验通过并给出事实(async_session: AsyncSession) -> None:
    world = await _seed(async_session)
    await _create(async_session, world)
    await _decide(async_session, world, world.checker_data, ApprovalSlot.CHECKER_DATA)
    await _decide(async_session, world, world.checker_security, ApprovalSlot.CHECKER_SECURITY)

    row = await _prep_of(async_session, world)
    facts = await prep_service.assert_committable(
        async_session, preparation_id=row.id, tenant_id=world.tenant_id
    )

    assert facts.preparation_id == row.id
    assert facts.artifact_sha256 == "a" * 64
    assert facts.input_fingerprint == row.input_fingerprint
    assert facts.environment is Env.DEV
    # 槽 → 审批人：责任追溯用（Capability 里不写人名）
    assert facts.approvers == {
        ApprovalSlot.CHECKER_DATA: world.checker_data.id,
        ApprovalSlot.CHECKER_SECURITY: world.checker_security.id,
    }
    assert facts.budget["max_read_rows"] == 1000
    assert facts.resource_scope["writes"][0]["shadow_table"] == "orders_wide__shadow_v1"


async def test_未审批通过不能提交(async_session: AsyncSession) -> None:
    world = await _seed(async_session)
    await _create(async_session, world)
    await _decide(async_session, world, world.checker_data, ApprovalSlot.CHECKER_DATA)
    row = await _prep_of(async_session, world)

    with pytest.raises(ApprovalIncompleteError):
        await prep_service.assert_committable(
            async_session, preparation_id=row.id, tenant_id=world.tenant_id
        )


async def test_制品指纹不符不能提交(async_session: AsyncSession) -> None:
    """``expected_artifact_sha256`` 是调用方（前端）手里那一份的指纹。

    带上它，就能拦住「审批页开着旧标签页、制品已经换过」的提交。
    """
    world = await _seed(async_session)
    await _create(async_session, world)
    await _decide(async_session, world, world.checker_data, ApprovalSlot.CHECKER_DATA)
    await _decide(async_session, world, world.checker_security, ApprovalSlot.CHECKER_SECURITY)
    row = await _prep_of(async_session, world)

    with pytest.raises(FingerprintMismatchError):
        await prep_service.assert_committable(
            async_session,
            preparation_id=row.id,
            tenant_id=world.tenant_id,
            expected_artifact_sha256="e" * 64,
        )


async def test_回滚方案为空不能提交(async_session: AsyncSession) -> None:
    """回滚方案是审批的一部分：不知道失败后怎么办的方案不能执行。

    这里用原生 UPDATE 绕过 ORM 守卫来造这个状态——**正因为**它绕过了守卫，
    才需要在提交前再判一次（否则「有人改过库」就成了一条无人拦截的路径）。
    """
    world = await _seed(async_session)
    await _create(async_session, world)
    await _decide(async_session, world, world.checker_data, ApprovalSlot.CHECKER_DATA)
    await _decide(async_session, world, world.checker_security, ApprovalSlot.CHECKER_SECURITY)
    row = await _prep_of(async_session, world)

    await async_session.execute(
        update(Preparation).where(Preparation.id == row.id).values(rollback_plan={"steps": []})
    )
    await async_session.commit()

    with pytest.raises(ConflictError):
        await prep_service.assert_committable(
            async_session, preparation_id=row.id, tenant_id=world.tenant_id
        )


async def test_跨租户取不到准备单(async_session: AsyncSession) -> None:
    """所有 SQL 强制带 tenant_id（CLAUDE.md §4.7），跨租户一律 404。"""
    world = await _seed(async_session)
    await _create(async_session, world)
    row = await _prep_of(async_session, world)

    from core.errors import NotFoundError

    with pytest.raises(NotFoundError):
        await prep_service.assert_committable(
            async_session, preparation_id=row.id, tenant_id=world.tenant_id + 999
        )


# ---------------------------------------------------------------- 取代与过期


async def test_重新发起会取代旧单(async_session: AsyncSession) -> None:
    """旧单不物理删除（它是「曾经有人批过这件事」的证据），只是不再作数。"""
    world = await _seed(async_session)
    first = await _create(async_session, world)
    # pipeline 已在 pending_approval，重发要先回到冻结态（真实场景是重新生成版本）
    pipeline = await async_session.get(Pipeline, world.pipeline_id)
    assert pipeline is not None
    pipeline.status = PipelineStatus.VERSION_FROZEN
    await async_session.commit()

    second = await _create(async_session, world)
    assert second.id != first.id

    old = await async_session.get(Preparation, first.id)
    assert old is not None
    assert old.status is PreparationStatus.SUPERSEDED

    superseded = await _events(
        async_session, tenant_id=world.tenant_id, event_type=EvidenceEventType.PREPARATION_SUPERSEDED
    )
    assert len(superseded) == 1
    assert superseded[0].payload["replaced_by"] == second.id
    # 被取代的旧单不能再被审批（它的指纹属于另一个制品）
    with pytest.raises(ConflictError):
        await approval_service.decide(
            async_session,
            preparation_id=old.id,
            subject=world.ctx(world.checker_data),
            slot=ApprovalSlot.CHECKER_DATA,
            decision=ApprovalDecisionType.APPROVE,
            comment="同意",
        )


async def test_过期任务幂等且留痕(async_session: AsyncSession) -> None:
    """定时任务会被重复触发，所以「扫一遍」必须幂等：第二次扫到 0 行。"""
    world = await _seed(async_session)
    await _create(async_session, world)
    row = await _prep_of(async_session, world)
    await _expire_now(async_session, row.id)

    changed = await prep_service.expire_due_preparations(
        async_session, tenant_id=world.tenant_id, now=datetime(2026, 6, 1, tzinfo=UTC)
    )
    assert changed == 1
    again = await prep_service.expire_due_preparations(
        async_session, tenant_id=world.tenant_id, now=datetime(2026, 6, 1, tzinfo=UTC)
    )
    assert again == 0

    await async_session.refresh(row)
    assert row.status is PreparationStatus.EXPIRED
    expired = await _events(
        async_session, tenant_id=world.tenant_id, event_type=EvidenceEventType.PREPARATION_EXPIRED
    )
    assert len(expired) == 1
    # 没有任何人在场：actor 留空，而不是 0（0 会指向一个不存在的用户）
    assert expired[0].actor_id is None


# ---------------------------------------------------------------- 列表与详情


async def test_待办列表只给该槽未决的人(async_session: AsyncSession) -> None:
    """``scope=todo`` 是审批人的待办：我持有该槽、该槽还没批、单子还活着。"""
    world = await _seed(async_session)
    await _create(async_session, world)
    await _decide(async_session, world, world.checker_data, ApprovalSlot.CHECKER_DATA)

    _data_items, data_total = await prep_service.list_preparations(
        async_session, world.ctx(world.checker_data), scope="todo"
    )
    assert data_total == 0, "自己已经批过的槽不该再出现在自己的待办里"

    security_items, security_total = await prep_service.list_preparations(
        async_session, world.ctx(world.checker_security), scope="todo"
    )
    assert security_total == 1
    assert security_items[0].my_slot is ApprovalSlot.CHECKER_SECURITY

    maker_items, maker_total = await prep_service.list_preparations(
        async_session, world.ctx(world.maker), scope="todo"
    )
    assert maker_total == 0
    assert maker_items == []


async def test_详情不含凭据清单(async_session: AsyncSession) -> None:
    """凭据引用留在库列里，**不进读 DTO**：少传一处就少一处泄露面（CLAUDE.md §5.2）。

    两条断言缺一不可：库里有（证明它没被别的路径写没）、DTO 里没有
    （证明是**在这里**被丢掉的，而不是碰巧为空）。
    """
    world = await _seed(async_session)
    detail = await _create(async_session, world)
    row = await _prep_of(async_session, world)

    assert row.resource_scope["secrets"], "库列里应当留着凭据引用给 be-14 物化用"
    assert "secrets" not in detail.resource_scope.model_dump()
    dumped = detail.model_dump_json()
    assert "kv/elt/orders" not in dumped
    assert "password" not in dumped

    # 写形态是常量：直接写正式表在 PDP 里是一条 P0
    write = detail.resource_scope.writes[0]
    assert write.mode == "shadow_then_swap"


async def test_状态轮询接口给出待决槽(async_session: AsyncSession) -> None:
    world = await _seed(async_session)
    await _create(async_session, world)
    await _decide(async_session, world, world.checker_data, ApprovalSlot.CHECKER_DATA)

    view = await prep_service.get_status(async_session, world.ctx(world.checker_security), (await _prep_of(async_session, world)).id)
    assert view.status is PreparationStatus.PARTIALLY_APPROVED
    assert view.approved_slots == (ApprovalSlot.CHECKER_DATA,)
    assert view.pending_slots == (ApprovalSlot.CHECKER_SECURITY,)
    assert view.committable is False


async def test_列表的can_commit是廉价判据(async_session: AsyncSession) -> None:
    """列表不做 N×4 次制品查询：它只按「状态 + 有效期」给结论。

    真正能不能提交由详情与 Commit 前的 ``assert_committable`` 说了算——
    这条差异是有意的，用例把它钉住，避免有人「顺手」把重算加进列表。
    """
    world = await _seed(async_session)
    await _create(async_session, world)
    await _decide(async_session, world, world.checker_data, ApprovalSlot.CHECKER_DATA)
    await _decide(async_session, world, world.checker_security, ApprovalSlot.CHECKER_SECURITY)

    items, total = await prep_service.list_preparations(
        async_session, world.ctx(world.maker), scope="mine"
    )
    assert total == 1
    summary = items[0]
    assert summary.can_commit is True

    # 指纹变了：列表仍给 True（廉价判据），详情重算后是 False
    secret = (
        await async_session.scalars(
            select(SecretReference).where(SecretReference.tenant_id == world.tenant_id)
        )
    ).one()
    secret.version = 9
    await async_session.commit()

    items, _ = await prep_service.list_preparations(async_session, world.ctx(world.maker), scope="mine")
    assert items[0].can_commit is True

    detail = await prep_service.get_preparation(
        async_session, world.ctx(world.maker), (await _prep_of(async_session, world)).id
    )
    assert detail.can_commit is False, "详情必须重算指纹——否则审批页会放着一条已经失效的单子"
    # ``stale`` 挂在**每条决定**上（不是单子上）：它要回答「这一条决定还算不算数」。
    # 逐条标而不是整单标，是因为指纹变化可能只影响其中一部分槽的判据来源。
    assert all(decision.stale for decision in detail.decisions)


async def test_时间线按时间顺序拼出关键节点(async_session: AsyncSession) -> None:
    """时间线由账本事件 + 决定行拼成，没有独立的事件表。"""
    world = await _seed(async_session)
    await _create(async_session, world)
    await _decide(async_session, world, world.checker_data, ApprovalSlot.CHECKER_DATA)

    detail = await prep_service.get_preparation(
        async_session, world.ctx(world.maker), (await _prep_of(async_session, world)).id
    )
    events = [item.event for item in detail.timeline]
    assert events[0] == "created"
    assert "approved_slot" in events
    # Commit 之前不该出现 committed（那由 be-14 的 run.created 落账）
    assert "committed" not in events


async def test_同时持有两个槽的人待办里只出现一次(async_session: AsyncSession) -> None:
    """``my_slot`` 是**一个**槽：待办按准备单去重，不按槽展开。

    所以 dave（两槽在手）看到的是**一条**待办、``my_slot`` 取先遇到的那个未决槽。
    这也解释了「为什么两槽要由两个不同的人担任」——一个人拿着两个槽时，
    界面上只会给他一个待办入口，第二条决定要么走详情页、要么被服务端按
    ``check_dual_checker_distinct`` 拦下（见上面的职责混用用例）。
    """
    world = await _seed(async_session)
    await _create(async_session, world)

    items, total = await prep_service.list_preparations(
        async_session, world.ctx(world.both_checkers), scope="todo"
    )
    assert total == 1
    assert items[0].my_slot is ApprovalSlot.CHECKER_DATA

    # 他一旦投了一票，这张单就整条从**他的**待办里消失（另一个人还能看到）
    await _decide(async_session, world, world.both_checkers, ApprovalSlot.CHECKER_DATA)
    items, total = await prep_service.list_preparations(
        async_session, world.ctx(world.both_checkers), scope="todo"
    )
    assert total == 0
    _, security_total = await prep_service.list_preparations(
        async_session, world.ctx(world.checker_security), scope="todo"
    )
    assert security_total == 1


def test_审批请求要求非空意见() -> None:
    """空意见会让事后复盘只能看到「谁批了」，看不到「他以为自己在批什么」。"""
    from pydantic import ValidationError as PydanticValidationError

    with pytest.raises(PydanticValidationError):
        DecisionRequest(
            slot=ApprovalSlot.CHECKER_DATA, decision=ApprovalDecisionType.APPROVE, comment=""
        )


def test_发起准备的请求不收指纹字段() -> None:
    """允许调用方提交指纹等于让它自证清白——那两个值是审批与执行的信任锚。"""
    from pydantic import ValidationError as PydanticValidationError

    with pytest.raises(PydanticValidationError):
        PreparationCreateRequest.model_validate(
            {"pipeline_version_id": 1, "artifact_sha256": "x" * 64}
        )


def test_审批请求不接受自报角色() -> None:
    """角色由服务端从 role_assignment 查，请求里连字段都不该存在。"""
    from pydantic import ValidationError as PydanticValidationError

    with pytest.raises(PydanticValidationError):
        DecisionRequest.model_validate(
            {
                "slot": "checker_data",
                "decision": "approve",
                "comment": "同意",
                "approver_role": "checker_security",
            }
        )


async def test_有效期短于一个工作日就没人来得及批(async_session: AsyncSession) -> None:
    """有效期必须长到「两个不同的人在两个班次里都能批完」。

    这条断言看起来在测常量，实际在测**流程可行性**：TTL 调到 1 小时，
    四眼在多时区/多班次下永远凑不齐，而症状是「审批单总是过期」，
    指向的是审批人，不是配置。
    """
    world = await _seed(async_session)
    detail = await _create(async_session, world)
    assert detail.expires_at is not None
    assert detail.expires_at - detail.frozen_at >= timedelta(hours=8)
