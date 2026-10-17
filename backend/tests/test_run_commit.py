"""be-14 执行编排与 Worker：Commit / 运行查询 / 取消 / 状态机 / Worker 幂等。

**这个模块的核心断言有四句**，其余用例都服务于它们：

1. **Commit 是一个原子包**：Capability / ``execution_run`` / Outbox 消息 / 两条证据
   要么全在、要么全不在；且**令牌原文只落在 Outbox 载荷里**（库与响应都没有它）。
   破了不会报错——只是「一次性授权」变成「任何能看到运行详情的人都能拿到的一张凭证」。
2. **状态机只有一条写路径**（``apply_transition``）：非法转移一律 ``RUN_CONFLICT``；
   并发靠条件 UPDATE 的 ``rowcount`` 裁决，输了**不覆盖别人的状态**。
   写成「先查后写」，两个并发 cancel 会都通过检查、都去调 ``seatunnel.stop``。
3. **Worker 的任务体是幂等的**：``task_acks_late`` 下重投是常态，重复投递一次作业的
   代价是一张被写两遍的影子表——而影子表写两遍**不会报错**（全量覆盖写）。
4. **令牌不进日志、不进 ``failure_snapshot``、不进响应**（CLAUDE.md §5.2）。

分层：``RUN_TRANSITIONS`` / ``assert_transition`` 是纯函数直接调；其余走 service + 真库
（内存 SQLite）。本机没有 Docker（CLAUDE.md §9），所以 seed 用 ORM 铺行而不是走
「生成 → 冻结」全链路——那条链路的重活归 M2 的用例，在这里再跑一遍只会把本文件的
失败原因和生成链路的 bug 搅在一起。

⚠️ 两处**只在测试里存在**的适配，都是 SQLite 的方言差异，不是产品缺陷：

- ``uq_execution_run_active_per_version`` 带 ``postgresql_where``，SQLite 上没有该子句，
  于是它退化成「每版本只许一行」的全量唯一索引。凡是要「同一版本并存多条运行」的用例
  （再次 Commit、列表分页、按状态取详情）先用 ``_allow_multiple_runs`` 删掉这个索引还原
  PG 语义；活跃唯一性由 ``assert_no_active_run`` 在服务层守着（本文件也测它）。
- ``tasks/execution.py`` 走 ``core.db.session_scope()``，本机没有真 PG，
  所以 Worker 用例把 ``core.db.SessionFactory`` 换到内存库，并把 ``_broker`` 换成
  记录型假 broker——``seatunnel.*`` 的执行器要到 be-15 才注册，真 broker 在这里
  必然以「工具未注册」失败（那正是 be-14 默认的、正确的失败形态，见
  ``tests/test_run_worker_default_failure`` 之外的口径）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.config import settings
from core.errors import (
    ApprovalIncompleteError,
    FingerprintMismatchError,
    NotFoundError,
    PreparationExpiredError,
    RoleRequiredError,
    RunConflictError,
    ValidationError,
)
from core.ids import sha256_hex, utcnow
from core.logging import redact_processor
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
from harness.broker import ToolResult
from harness.outbox import Topic
from models.datasource import DataSource, MetadataProfile, SecretReference
from models.enums import (
    ApprovalDecisionType,
    ApprovalSlot,
    DataSourceType,
    Env,
    EvidenceEventType,
    MemberStatus,
    PipelineStatus,
    ProbeStatus,
    RoleName,
    RunStatus,
    SensitiveLevel,
    ToolName,
)
from models.evidence import EvidenceEvent, OutboxMessage
from models.execution import ExecutionRun
from models.governance import Capability, Preparation
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
from schemas.preparation import PreparationCreateRequest
from schemas.project import ProjectCreate
from schemas.run import RunDetail, RunSubmitContext, RunSummary
from services import approval as approval_service
from services import preparation as prep_service
from services import project as project_service
from services import run as run_service

FORMAL = "orders_wide"
SHADOW = "orders_wide__shadow_v1"
ERROR = "orders_wide_elt_reject"

#: **be-14 的两个产品缺陷：已修，标记已摘（2026-10-16）。**
#: 留这段注释是因为它们都不是「某条边缘路径写错了」，而是两类会**静默**出错的形状；
#: 当初它们是带着 ``xfail`` 标记进来的（当成「测试写法问题」），实跑之后才确认是产品缺陷。
#: 摘掉标记之后，本文件里这几条用例就是它们的回归守卫。
#:
#: ① ``services/run.py::get_run`` 组装 ``RunDetail`` 时把 ``seatunnel_job_id`` 传了两次
#:    （``RunSummary.model_dump()`` 里已经有一份）→ 每次调用都 ``TypeError``。
#:    受影响的不是一条边缘路径，而是 ``commit`` / 运行详情 / ``pause`` / ``cancel``
#:    四条主路径**全部 500**，而副作用（运行记录、账本、Outbox、令牌）已经落库——
#:    用户看到的是「操作失败」，库里躺着一次真实运行。
#: ② ``tasks/execution._fail`` 把 ``error_message`` / ``failure_snapshot`` 赋在 ORM 实例上，
#:    紧接着调 ``apply_transition``，而后者末尾原本有一次 ``session.refresh(row)``，
#:    它会按主键重读整行、从而丢弃这些**尚未 flush** 的改动（session 是 autoflush=False）。
#:    结果：运行确实变成 failed，失败现场却是空的——详情页 error_message 为 null、
#:    failure_snapshot 为 ``{}``，而账本里那条 RUN_FAILED 又写着「失败了」。
#:    这类「有结论、没原因」的失败最难排查。
#:
#: ② 的修法是**去掉那次 refresh**（``services/run.py::apply_transition`` 里有完整注释）：
#: 成功路径把刚写下去的字段直接赋回内存实例，抢锁失败那条只读状态列。
#: ⚠️ 不要退回「动状态前先 ``flush([row])``」——那是我试过的弯路，而且走不通：
#: ``flush([row])`` 在 SQLAlchemy 2.x 已弃用，且 ``apply_transition`` 是**故意不提交**的
#: （事务边界归调用方），加 flush 只是把问题从「refresh 吃改动」变成「谁负责提交」两处纠缠。


# ---------------------------------------------------------------- 夹具


@pytest.fixture(scope="module")
def keypair(tmp_path_factory: pytest.TempPathFactory) -> str:
    """一对 Ed25519 密钥，写在临时目录（本机没有 /run/secrets）。

    **公钥也要写**：``verifier_from_settings`` 按「私钥路径换后缀」推公钥路径
    （``private.pem`` → ``private.pub.pem``）。此前本文件只写私钥——因为那时
    Commit 只签发、不验签。现在它在同一事务里执行 ``target.ensure_tables``，
    于是本进程也成了验签方；少写公钥的症状是 ``FileNotFoundError: private.pub.pem``，
    读起来像「密钥文件没挂上」，实际是夹具只铺了一半。
    """
    directory = tmp_path_factory.mktemp("run-capability-keys")
    private = ed25519.Ed25519PrivateKey.generate()
    path = directory / "private.pem"
    path.write_bytes(
        private.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    public_path = directory / "private.pub.pem"
    public_path.write_bytes(
        private.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    )
    return str(path)


@pytest.fixture(autouse=True)
def _signing_key(keypair: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """把签发器指到临时密钥，并重置模块级单例 ``services.run._issuer``。

    ``_issuer`` 是**进程级**懒加载单例：不重置的话，第一个用到它的用例会把
    临时密钥缓存下来，之后「换了密钥」在任何地方都不生效——而症状是验签失败，
    看起来像密钥配错了。monkeypatch 会在用例结束把 ``settings`` 与 ``_issuer``
    都还原，避免污染其它文件（它们用的是各自的密钥或根本不签令牌）。
    """
    monkeypatch.setattr(settings, "CAPABILITY_SIGNING_KEY_PATH", keypair)
    monkeypatch.setattr(run_service, "_issuer", None)
    monkeypatch.setattr(run_service, "_verifier", None)


@pytest.fixture
async def world(async_session: AsyncSession) -> _World:
    return await _seed(async_session)


class _RecordingBroker:
    """记录型假 broker：只记调用、不执行。

    真 broker 在这条路径上会**真的去目标库建表**（``target.ensure_tables``），
    而本机没有 Doris（CLAUDE.md §9）。本文件要测的是「Commit 这个原子包里有什么」，
    七步序与建表算法分别由 ``tests/test_harness_broker.py``、``tests/test_target_provision.py`` 守。
    """

    def __init__(self) -> None:
        self.calls: list[tuple[ToolName, Any, Any]] = []

    async def execute(
        self, tool: ToolName, token: Any, args: Any, ctx: Any, **_: Any
    ) -> ToolResult:
        self.calls.append((tool, args, ctx))
        return ToolResult(tool=tool, ok=True, jti=None, detail={"state": "ready"})


@pytest.fixture(autouse=True)
def ensure_broker(monkeypatch: pytest.MonkeyPatch) -> _RecordingBroker:
    """把 Commit 里的备表步骤接到记录型 broker 上（见 ``_RecordingBroker``）。

    **autouse**：本文件每一个走 ``commit()`` 的用例都会经过备表那一步，
    而真 broker 会去连目标库。逐个用例去请求这个夹具的话，漏掉一个的症状是
    「用例报的是连接失败」，排查方向被带到「测试库配置」上。
    """
    broker = _RecordingBroker()
    monkeypatch.setattr(run_service, "_broker", lambda: broker)
    return broker


@pytest.fixture
def worker_db(
    async_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> async_sessionmaker[AsyncSession]:
    """把 ``core.db.SessionFactory`` 换到内存库，让 Worker 任务体连的是测试库。"""
    from core import db as core_db

    monkeypatch.setattr(core_db, "SessionFactory", async_session_factory)
    return async_session_factory


class _FakeBroker:
    """记录型假 broker：``execute`` 被调用即记一笔，返回预设的 ``ToolResult``。

    真 ``ToolBroker`` 对 ``seatunnel.*`` 会走到「执行器未注册」，而执行器要到
    be-15 才注册；这里要测的是 Worker 拿到结果之后怎么记账，不是 broker 的七步序
    （那由 ``tests/test_harness_broker.py`` 守）。
    """

    def __init__(self, result: ToolResult | None = None) -> None:
        self._result = result
        self.calls: list[tuple[Any, ...]] = []

    async def execute(self, tool: Any, token: Any, args: Any, ctx: Any, **_: Any) -> ToolResult:
        self.calls.append((tool, token, args, ctx))
        if self._result is None:
            raise AssertionError("本次用例不应该调用 broker")
        return self._result


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
        operator: AppUser,
    ) -> None:
        self.tenant_id = tenant_id
        self.project_id = project_id
        self.pipeline_id = pipeline_id
        self.version_id = version_id
        self.profile_id = profile_id
        self.maker = maker
        self.checker_data = checker_data
        self.checker_security = checker_security
        self.operator = operator

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
    """直接铺成员 + 角色行（与 ``tests/test_preparation.py`` 同一口径）。

    不走 ``project_service.grant_role``：授予本身需要 checker_security 且会落账本，
    在这里铺一遍会把本文件的失败原因和 be-02 的授权 bug 搅在一起。
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
    operator = await _user(session, tenant.id, "erin")

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
    # create_project 会把 maker 槽给创建者；执行槽（operator）单独授予，
    # 与审批槽分开——「谁跑的」与「谁批的」是两个不同的职责。
    await _grant(session, project_id, checker_data, RoleName.CHECKER_DATA)
    await _grant(session, project_id, checker_security, RoleName.CHECKER_SECURITY)
    await _grant(session, project_id, operator, RoleName.OPERATOR)

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
                        }
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
                table=FORMAL,
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
    # HOCON 走渲染器而不是手写字符串：手写的那份迟早与渲染器分叉
    hocon = render_hocon(
        plan, RenderContext(data_source_types={source.id: "mysql", target.id: "doris"})
    )

    plan_row = EtlPlan(
        schema_version="1.0", plan=plan.model_dump(mode="json"), valid=True, validation_errors=[]
    )
    hocon_row = HoconArtifact(
        content=hocon.content,
        sha256=sha256_hex(hocon.content),
        parser_version=PARSER_VERSION,
        validation_result={},
    )
    quality_row = QualityContract(
        rules=[{"column": "id", "code": "E_NULL_PK", "expression": "id IS NOT NULL"}],
        error_table_target={"data_source_id": target.id, "table": ERROR},
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
        operator=operator,
    )


async def _create(session: AsyncSession, world: _World) -> Preparation:
    await prep_service.create_preparation(
        session,
        world.ctx(world.maker),
        PreparationCreateRequest(pipeline_version_id=world.version_id, note=""),
    )
    return await _prep_of(session, world)


async def _decide(
    session: AsyncSession,
    world: _World,
    user: AppUser,
    slot: ApprovalSlot,
    decision: ApprovalDecisionType = ApprovalDecisionType.APPROVE,
) -> None:
    await approval_service.decide(
        session,
        preparation_id=(await _prep_of(session, world)).id,
        subject=world.ctx(user),
        slot=slot,
        decision=decision,
        comment="同意，已核对字段映射与预算",
    )


async def _approved(session: AsyncSession, world: _World) -> Preparation:
    """一张四眼齐备、当前指纹下批准的可用准备单。"""
    await _create(session, world)
    await _decide(session, world, world.checker_data, ApprovalSlot.CHECKER_DATA)
    await _decide(session, world, world.checker_security, ApprovalSlot.CHECKER_SECURITY)
    await session.refresh(await _prep_of(session, world))
    return await _prep_of(session, world)


async def _prep_of(session: AsyncSession, world: _World) -> Preparation:
    row = await session.scalar(
        select(Preparation)
        .where(Preparation.tenant_id == world.tenant_id, Preparation.project_id == world.project_id)
        .order_by(Preparation.id.desc())
    )
    assert row is not None, "还没发起过准备单"
    return row


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


async def _count(session: AsyncSession, model: Any) -> int:
    return int(await session.scalar(select(func.count()).select_from(model)) or 0)


async def _expire_now(session: AsyncSession, preparation_id: int) -> None:
    """把有效期推到过去。必须走 Core UPDATE：``Preparation.__mutable_columns__``
    只放行 ``status``，ORM 赋 ``expires_at`` 会被不可变守卫拦下。"""
    await session.execute(
        update(Preparation)
        .where(Preparation.id == preparation_id)
        .values(expires_at=datetime(2026, 1, 1, tzinfo=UTC))
    )
    await session.commit()


async def _allow_multiple_runs(session: AsyncSession) -> None:
    """删掉 SQLite 上退化的「每版本一行」唯一索引（见模块 docstring 的适配说明）。"""
    await session.execute(text("DROP INDEX IF EXISTS uq_execution_run_active_per_version"))
    await session.commit()


async def _make_run(
    session: AsyncSession,
    world: _World,
    *,
    preparation_id: int,
    status: RunStatus = RunStatus.QUEUED,
    key: str,
    job_id: str | None = None,
    triggered_by: int | None = None,
) -> ExecutionRun:
    """直接铺一条运行行：状态机用例的现场只需要「有一条 run 在某个状态」。

    ``triggered_by`` 默认为空（多数用例不关心它），但提交链路**必须**有它——
    它是 Worker 侧提交时的执行主体来源，见
    ``test_worker提交时把发起人作为执行主体``。
    """
    run = ExecutionRun(
        tenant_id=world.tenant_id,
        project_id=world.project_id,
        pipeline_version_id=world.version_id,
        preparation_id=preparation_id,
        capability_id=None,
        idempotency_key=key,
        status=status,
        shadow_table=SHADOW,
        formal_table=FORMAL,
        error_table=ERROR,
        seatunnel_job_id=job_id,
        triggered_by=triggered_by,
    )
    session.add(run)
    await session.flush()
    return run


async def _prepare_running_run(session: AsyncSession, world: _World, *, key: str) -> Preparation:
    """一条 RUNNING 的 run + 它背后可提交的准备单（取消/暂停/停止用例共用）。"""
    prep = await _approved(session, world)
    await _make_run(session, world, preparation_id=prep.id, status=RunStatus.RUNNING, key=key)
    await session.commit()
    return prep


# ================================================================ Commit


async def test_提交成功后台账与令牌落点都在正确的地方(
    async_session: AsyncSession, world: _World, ensure_broker: _RecordingBroker
) -> None:
    """Commit 的完整产出：queued 运行 + 两张 Capability 登记 + Outbox 命令 + 三条证据。

    令牌原文的落点是**唯一**的：``capability`` 表只有 ``token_sha256``，
    运行详情里连字段都没有；原文只随 Outbox 载荷走（Worker 只能从那儿拿到它验签）。
    备表那枚令牌是例外——它在同一个事务里被当场消费掉，原文从不落库也不出进程。
    """
    prep = await _approved(async_session, world)

    detail = await run_service.commit(
        async_session,
        preparation_id=prep.id,
        subject=world.ctx(world.operator),
        idempotency_key="k-happy",
    )

    assert detail.status is RunStatus.QUEUED
    assert detail.shadow_table == SHADOW
    assert detail.formal_table == FORMAL
    # 错误表名由唯一命名定义推出，而不是各处各拼一次
    assert detail.error_table == ERROR
    assert detail.can_cancel is True  # queued → cancelled 是合法出边
    assert detail.capability_id is not None

    # 流程状态推进到 queued
    pipeline = await async_session.get(Pipeline, world.pipeline_id)
    assert pipeline is not None and pipeline.status is PipelineStatus.QUEUED

    # Capability 只存哈希；Outbox 载荷里是原文
    cap = await async_session.get(Capability, detail.capability_id)
    assert cap is not None and cap.tool == ToolName.SEATUNNEL_SUBMIT.value
    message = await async_session.scalar(
        select(OutboxMessage).where(OutboxMessage.topic == Topic.SEATUNNEL_SUBMIT.value)
    )
    assert message is not None
    assert set(message.payload) == {"run_id", "capability"}
    assert message.payload["run_id"] == detail.id
    token = message.payload["capability"]
    assert cap.token_sha256 == sha256_hex(token)

    issued = await _events(
        async_session, tenant_id=world.tenant_id, event_type=EvidenceEventType.CAPABILITY_ISSUED
    )
    created = await _events(
        async_session, tenant_id=world.tenant_id, event_type=EvidenceEventType.RUN_CREATED
    )
    # 两枚令牌各一条事件。**比集合而不是比顺序**：两条都在同一个事务里 append，
    # 账本的 ``seq`` 在 flush 时才定序，把这层实现细节写进断言只会让用例
    # 在换数据库/换批次时莫名其妙地红。
    assert {event.payload["tool"] for event in issued} == {
        ToolName.SEATUNNEL_SUBMIT.value,
        ToolName.TARGET_ENSURE_TABLES.value,
    }
    assert {event.payload["run_id"] for event in issued} == {detail.id}
    assert created[0].subject_id == str(detail.id)

    # 备表这一步**真的被调用了**，且参数就是 run 行上那三个表名。
    # 这条断言防的是「终点函数没人调」：本文件此前只测提交本身，
    # 而备表若从 commit 里被删掉，所有既有断言照样全绿——直到作业在目标库里
    # 因为「影子表不存在」失败。
    assert [call[0] for call in ensure_broker.calls] == [ToolName.TARGET_ENSURE_TABLES]
    _tool, args, ctx = ensure_broker.calls[0]
    assert args.run_id == detail.id
    assert (args.shadow_table, args.formal_table, args.error_table) == (SHADOW, FORMAL, ERROR)
    # 主体必须是令牌里签的那个人（broker 的 BINDING 阶段会逐字比对）
    assert ctx.actor_id == world.operator.id


async def test_提交缺少幂等键直接拒绝且零副作用(
    async_session: AsyncSession, world: _World
) -> None:
    """``Idempotency-Key`` 为空 → 422，且不留下任何执行痕迹。

    不给默认值：``execution_run.idempotency_key`` 是唯一约束，用随机值兜底会把
    「重复点按钮」变成两条真实的运行记录——而那两条都长得完全合法。
    """
    prep = await _approved(async_session, world)

    with pytest.raises(ValidationError):
        await run_service.commit(
            async_session,
            preparation_id=prep.id,
            subject=world.ctx(world.operator),
            idempotency_key="",
        )

    assert await _count(async_session, ExecutionRun) == 0
    assert await _count(async_session, Capability) == 0


async def test_没有operator职责槽的人不能提交(async_session: AsyncSession, world: _World) -> None:
    """角色**此刻**从 ``role_assignment`` 重查，不信 JWT 声明。

    maker 有 maker 槽但不是 operator——「能发起准备」不等于「能执行」。
    """
    prep = await _approved(async_session, world)

    with pytest.raises(RoleRequiredError):
        await run_service.commit(
            async_session,
            preparation_id=prep.id,
            subject=world.ctx(world.maker),
            idempotency_key="k-role",
        )

    assert await _count(async_session, ExecutionRun) == 0
    assert await _count(async_session, Capability) == 0


async def test_同版本已有活跃运行时拒绝而终态可再提交(
    async_session: AsyncSession, world: _World
) -> None:
    """并发闸门：活跃运行在 → 409；把它推到终态后 → 放行。

    数据库那侧有部分唯一索引兜底，但**两道都要有**：索引给出的是一条
    ``IntegrityError``（前端只能显示「内部错误」），这里的判断给出的是一条
    能读懂、能行动的 409。
    """
    await _allow_multiple_runs(async_session)
    prep = await _approved(async_session, world)

    first = await run_service.commit(
        async_session,
        preparation_id=prep.id,
        subject=world.ctx(world.operator),
        idempotency_key="k-first",
    )
    assert first.status is RunStatus.QUEUED

    with pytest.raises(RunConflictError):
        await run_service.commit(
            async_session,
            preparation_id=prep.id,
            subject=world.ctx(world.operator),
            idempotency_key="k-second",
        )

    # 推到终态后，同一版本可以再次提交
    await async_session.execute(
        update(ExecutionRun).where(ExecutionRun.id == first.id).values(status=RunStatus.SUCCEEDED)
    )
    await async_session.commit()

    third = await run_service.commit(
        async_session,
        preparation_id=prep.id,
        subject=world.ctx(world.operator),
        idempotency_key="k-third",
    )
    assert third.id != first.id
    assert third.status is RunStatus.QUEUED


async def test_未审批通过的准备单不能提交且零副作用(
    async_session: AsyncSession, world: _World
) -> None:
    """只批了一个槽 → 沿用 be-13 的 ``ApprovalIncompleteError``，不产生执行痕迹。"""
    await _create(async_session, world)
    await _decide(async_session, world, world.checker_data, ApprovalSlot.CHECKER_DATA)
    prep = await _prep_of(async_session, world)

    with pytest.raises(ApprovalIncompleteError):
        await run_service.commit(
            async_session,
            preparation_id=prep.id,
            subject=world.ctx(world.operator),
            idempotency_key="k-incomplete",
        )

    assert await _count(async_session, ExecutionRun) == 0
    assert await _count(async_session, Capability) == 0


async def test_过期的准备单不能提交且零副作用(
    async_session: AsyncSession, world: _World
) -> None:
    """有效期是硬边界：批过但过期了也不许执行（Commit 前会再判一次）。"""
    prep = await _approved(async_session, world)
    await _expire_now(async_session, prep.id)

    with pytest.raises(PreparationExpiredError):
        await run_service.commit(
            async_session,
            preparation_id=prep.id,
            subject=world.ctx(world.operator),
            idempotency_key="k-expired",
        )

    assert await _count(async_session, ExecutionRun) == 0
    assert await _count(async_session, Capability) == 0


async def test_指纹变化的准备单不能提交且零副作用(
    async_session: AsyncSession, world: _World
) -> None:
    """凭据轮转（``secret_reference.version`` 变了）让审批失效 → ``FingerprintMismatchError``。

    表结构一个字节没变，但这次执行面对的秘密已经不是审批时那一个了——
    这正是「指纹里为什么要放 secret 版本」。
    """
    prep = await _approved(async_session, world)

    secret = (
        await async_session.scalars(
            select(SecretReference).where(SecretReference.tenant_id == world.tenant_id)
        )
    ).one()
    secret.version = 4
    await async_session.commit()

    with pytest.raises(FingerprintMismatchError):
        await run_service.commit(
            async_session,
            preparation_id=prep.id,
            subject=world.ctx(world.operator),
            idempotency_key="k-fingerprint",
        )

    assert await _count(async_session, ExecutionRun) == 0
    assert await _count(async_session, Capability) == 0


async def test_令牌原文不出现在运行详情里(async_session: AsyncSession, world: _World) -> None:
    """运行详情**不含**令牌原文，也不含它的任何形态（CLAUDE.md §5.2）。

    控制面响应里出现令牌，等于把「一次性授权」变成「任何能看到这次运行详情的人
    都能拿到的一张凭证」。用例从 Outbox 载荷里取出真正的原文当探针——
    用假字符串探针会漏掉「字段名不叫 token 但确实把原文带出去了」这类。
    """
    prep = await _approved(async_session, world)
    detail = await run_service.commit(
        async_session,
        preparation_id=prep.id,
        subject=world.ctx(world.operator),
        idempotency_key="k-token",
    )

    message = await async_session.scalar(
        select(OutboxMessage).where(OutboxMessage.topic == Topic.SEATUNNEL_SUBMIT.value)
    )
    assert message is not None
    token = message.payload["capability"]
    assert token  # 探针非空，否则下面的断言恒真

    assert token not in detail.model_dump_json()
    assert token not in repr(detail.model_dump())
    # 登记表里也只有哈希
    cap = await async_session.get(Capability, detail.capability_id)
    assert cap is not None and cap.token_sha256 != token


# ================================================================ 状态机


def test_运行状态转移表合法边放行非法边一律冲突() -> None:
    """``(当前, 目标)`` 全枚举：合法的放行，其余一律 ``RUN_CONFLICT``。

    终态**显式写成空集**而不是省略：``.get()`` 的默认值会让「枚举新增一个状态」
    变成「新状态可以走到任何地方」——那是静默放行。这条用例遍历全表，
    新增一个状态却忘了给出边时，它会在「没有出边的集合恰好是这两个」那条断言上变红。

    ⚠️ 「没有出边」与 ``TERMINAL_STATES`` **不是同一个集合**：后者是「进入即写
    ``finished_at``」，而 ``failed`` / ``succeeded`` 都**有**去 ``rolling_back``
    的出边（回滚上一次已发布的结果）。代码注释把两个概念都叫『终态』，
    是文档上的一个歧义点；这里按**实际语义**分别钉住，避免哪天有人
    「照注释统一一下」把回滚出边删掉。
    """
    no_out_edges = {state for state in RunStatus if not run_service.RUN_TRANSITIONS[state]}
    assert no_out_edges == {RunStatus.CANCELLED, RunStatus.ROLLED_BACK}
    assert run_service.TERMINAL_STATES == {
        RunStatus.SUCCEEDED,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
        RunStatus.ROLLED_BACK,
    }
    # 成功/失败都是「进入即写 finished_at」，但都能进回滚
    assert run_service.RUN_TRANSITIONS[RunStatus.SUCCEEDED] == frozenset({RunStatus.ROLLING_BACK})
    assert run_service.RUN_TRANSITIONS[RunStatus.FAILED] == frozenset({RunStatus.ROLLING_BACK})
    assert RunStatus.FAILED not in RunStatus.active_states()

    all_states = list(RunStatus)
    legal = 0
    for source in all_states:
        for target in all_states:
            if target in run_service.RUN_TRANSITIONS[source]:
                run_service.assert_transition(source, target)  # 不抛
                legal += 1
            else:
                with pytest.raises(RunConflictError):
                    run_service.assert_transition(source, target)
    assert legal > 0

    # 几个最容易写错的边，单独点名
    for bad in (
        (RunStatus.CANCELLED, RunStatus.RUNNING),
        (RunStatus.SUCCEEDED, RunStatus.RUNNING),
        (RunStatus.QUEUED, RunStatus.ROLLED_BACK),
        (RunStatus.QUEUED, RunStatus.PAUSED),
    ):
        with pytest.raises(RunConflictError):
            run_service.assert_transition(*bad)


async def test_把状态设成目标态再转移是幂等的(
    async_session: AsyncSession, world: _World
) -> None:
    """重复点击「取消」不该报错：当前已是目标态时直接返回摘要。

    它必须**先于**转移合法性检查——否则「已经取消了」会被当成
    「cancelled 不能变成 cancelled」而报 409，症状是「取消按钮点第二次就报错」。
    """
    prep = await _create(async_session, world)
    run = await _make_run(
        async_session, world, preparation_id=prep.id, status=RunStatus.RUNNING, key="run:idem"
    )
    await async_session.commit()

    summary = await run_service.apply_transition(
        async_session, run_id=run.id, target=RunStatus.RUNNING, reason="noop", actor_id=None
    )
    assert summary.status is RunStatus.RUNNING
    assert summary.id == run.id

    # 合法转移照常生效，且终态写 finished_at
    moved = await run_service.apply_transition(
        async_session, run_id=run.id, target=RunStatus.SUCCEEDED, reason="done", actor_id=None
    )
    assert moved.status is RunStatus.SUCCEEDED
    assert moved.finished_at is not None


async def test_并发修改后条件更新不改别人的状态(async_session: AsyncSession, world: _World) -> None:
    """条件 UPDATE 输了这一场并发：抛 ``RunConflictError``，**不覆盖**别人的状态。

    这条路径就是「不用先查后写」的理由：两个并发 cancel 里，后到的那个拿到
    ``rowcount == 0``，读回现状发现状态已被推到 ``interrupted``，于是停下——
    而不是把它改回 cancelled 再去调一次 ``seatunnel.stop``。
    """
    prep = await _create(async_session, world)
    run = await _make_run(
        async_session, world, preparation_id=prep.id, status=RunStatus.RUNNING, key="run:race"
    )
    await async_session.commit()

    # 模拟「另一个事务先把它改走了」：原生 SQL 不动本会话的 ORM 身份映射，
    # 于是 apply_transition 读到的仍是内存里的 running，而库里的行已经是 interrupted
    await async_session.execute(
        text("UPDATE execution_run SET status = 'interrupted' WHERE id = :id"), {"id": run.id}
    )

    with pytest.raises(RunConflictError):
        await run_service.apply_transition(
            async_session,
            run_id=run.id,
            target=RunStatus.CANCELLED,
            reason="cancel",
            actor_id=None,
        )

    # 库里的结论仍是「别人写的那个」，没有被这次调用覆盖
    stored = await async_session.scalar(
        select(ExecutionRun.status).where(ExecutionRun.id == run.id)
    )
    assert stored is RunStatus.INTERRUPTED


async def test_取消会签发停止令牌并投递停止命令(
    async_session: AsyncSession, world: _World
) -> None:
    """取消走与提交同一条路：签一张绑定 ``seatunnel.stop`` 的 Capability + Outbox 投递。

    控制面**不直接调 SeaTunnel**——在这里直接调就等于在服务层开了第二个副作用出口
    （CLAUDE.md §5.1）。
    """
    prep = await _prepare_running_run(async_session, world, key="run:cancel")

    detail = await run_service.request_cancel(
        async_session,
        run_id=(await async_session.scalar(select(ExecutionRun.id))),
        subject=world.ctx(world.operator),
        reason="人手取消",
    )

    assert detail.status is RunStatus.CANCELLED
    assert detail.finished_at is not None

    caps = (await async_session.scalars(select(Capability))).all()
    assert [cap.tool for cap in caps] == [ToolName.SEATUNNEL_STOP.value]
    assert caps[0].preparation_id == prep.id

    message = await async_session.scalar(
        select(OutboxMessage).where(OutboxMessage.topic == Topic.RUN_STOP.value)
    )
    assert message is not None
    assert message.payload["reason"] == "manual_cancel"
    assert message.payload["run_id"] == detail.id
    # 停止命令必须随载荷带上**执行主体**，且与令牌里签的那个是同一个：
    # worker 的 broker 要求 ``ctx.actor_id`` 非空，而它只能来自这里。
    # 少了这个键，整条取消会「控制面说取消了、作业其实还在跑」——
    # 这条断言守的是那种安静的分裂。
    assert message.payload["subject_id"] == world.operator.id
    assert caps[0].subject_id == world.operator.id

    issued = await _events(
        async_session, tenant_id=world.tenant_id, event_type=EvidenceEventType.CAPABILITY_ISSUED
    )
    assert [event.payload["tool"] for event in issued] == [ToolName.SEATUNNEL_STOP.value]


async def test_重复取消是幂等的且不再签发令牌(
    async_session: AsyncSession, world: _World
) -> None:
    """已经取消了再点一次：返回当前状态，不新签 Capability、不新投 Outbox。

    重复签发不会报错，只会让一次「取消」在账本里留下两张令牌——
    而其中一张永远没人消费，事后看像「有人取消了两次」。
    """
    await _prepare_running_run(async_session, world, key="run:cancel-idem")
    run_id = int(await async_session.scalar(select(ExecutionRun.id)) or 0)

    first = await run_service.request_cancel(
        async_session, run_id=run_id, subject=world.ctx(world.operator), reason="第一次"
    )
    assert first.status is RunStatus.CANCELLED

    caps_before = await _count(async_session, Capability)
    outbox_before = await _count(async_session, OutboxMessage)

    again = await run_service.request_cancel(
        async_session, run_id=run_id, subject=world.ctx(world.operator), reason="第二次"
    )
    assert again.status is RunStatus.CANCELLED
    assert await _count(async_session, Capability) == caps_before
    assert await _count(async_session, OutboxMessage) == outbox_before


async def test_暂停只改状态不调SeaTunnel(async_session: AsyncSession, world: _World) -> None:
    """暂停是「不再推进后续阶段」，作业继续跑——所以不签令牌、不投停止命令。"""
    await _prepare_running_run(async_session, world, key="run:pause")
    run_id = int(await async_session.scalar(select(ExecutionRun.id)) or 0)

    detail = await run_service.request_pause(
        async_session, run_id=run_id, subject=world.ctx(world.operator), reason="看指标"
    )

    assert detail.status is RunStatus.PAUSED
    assert detail.finished_at is None  # 非终态不写 finished_at
    assert await _count(async_session, OutboxMessage) == 0


async def test_排队中的运行不能暂停(async_session: AsyncSession, world: _World) -> None:
    """``queued → paused`` 不在转移表里（还没起跑，谈不上「暂停推进」）。"""
    prep = await _create(async_session, world)
    run = await _make_run(
        async_session, world, preparation_id=prep.id, status=RunStatus.QUEUED, key="run:q-pause"
    )
    await async_session.commit()

    with pytest.raises(RunConflictError):
        await run_service.request_pause(
            async_session, run_id=run.id, subject=world.ctx(world.operator), reason=""
        )


async def test_详情按转移表下发can_cancel与can_rollback(
    async_session: AsyncSession, world: _World
) -> None:
    """``can_cancel`` / ``can_rollback`` 由服务端按转移表算好，前端只负责置灰。

    前端自算等于把「这个动作现在合不合法」变成一次客户端判断，
    而服务端的转移表才是唯一事实来源（CLAUDE.md §5.8 的同一道理）。
    """
    await _allow_multiple_runs(async_session)
    prep = await _create(async_session, world)
    cases = {
        RunStatus.QUEUED: (True, False),
        RunStatus.RUNNING: (True, False),
        RunStatus.SUCCEEDED: (False, True),
    }
    runs = {}
    for status in cases:
        runs[status] = await _make_run(
            async_session,
            world,
            preparation_id=prep.id,
            status=status,
            key=f"run:detail-{status.value}",
        )
    await async_session.commit()

    for status, (expect_cancel, expect_rollback) in cases.items():
        detail = await run_service.get_run(
            async_session, world.ctx(world.operator), runs[status].id
        )
        assert detail.can_cancel is expect_cancel, status
        assert detail.can_rollback is expect_rollback, status


async def test_运行列表分页与筛选且跨租户不可见(
    async_session: AsyncSession, world: _World
) -> None:
    """``total`` 是**全量**条数（不是本页条数），筛选按状态/流程/版本各自生效，
    且所有查询强制带 ``tenant_id``（CLAUDE.md §4.7）。

    分页把 ``total`` 写成 ``len(rows)`` 的症状是「前端分页控件永远只有一页」。
    跨租户可见的症状更隐蔽：列表本身看起来完全正常，只是多了一条别人的运行。
    """
    await _allow_multiple_runs(async_session)
    prep = await _create(async_session, world)
    for index, status in enumerate((RunStatus.QUEUED, RunStatus.QUEUED, RunStatus.FAILED)):
        await _make_run(
            async_session,
            world,
            preparation_id=prep.id,
            status=status,
            key=f"run:list-{index}",
        )
    foreign = ExecutionRun(
        tenant_id=world.tenant_id + 999,
        project_id=world.project_id,
        pipeline_version_id=world.version_id,
        preparation_id=prep.id,
        capability_id=None,
        idempotency_key="run:foreign",
        status=RunStatus.QUEUED,
        shadow_table=SHADOW,
        formal_table=FORMAL,
        error_table=ERROR,
    )
    async_session.add(foreign)
    await async_session.commit()

    items, total = await run_service.list_runs(
        async_session, world.ctx(world.operator), page_size=2
    )
    assert total == 3, "total 必须是全量条数，不是本页条数"
    assert len(items) == 2

    _, queued_total = await run_service.list_runs(
        async_session, world.ctx(world.operator), status=RunStatus.QUEUED
    )
    assert queued_total == 2

    _, by_pipeline = await run_service.list_runs(
        async_session, world.ctx(world.operator), pipeline_id=world.pipeline_id
    )
    assert by_pipeline == 3

    _, by_version = await run_service.list_runs(
        async_session, world.ctx(world.operator), pipeline_version_id=world.version_id
    )
    assert by_version == 3

    # 跨租户取详情一律 404（不带 tenant_id 的路径只有 Worker 的 load_for_submit）
    with pytest.raises(NotFoundError):
        await run_service.get_run(async_session, world.ctx(world.operator), foreign.id)


# ================================================================ Worker


async def test_重复投递的提交任务跳过且不再调broker(
    async_session: AsyncSession, world: _World, worker_db: async_sessionmaker, monkeypatch
) -> None:
    """幂等闸：run 已经不是 queued → 直接返回既有事实，不重签、不重提交。

    ``task_acks_late`` 下重投是常态。重复提交一次作业的代价是一张被写两遍的影子表——
    而影子表写两遍**不会报错**（全量覆盖写），只会在 Swap 之后让人怀疑人生。
    """
    from tasks import execution as tasks_execution

    prep = await _create(async_session, world)
    run = await _make_run(
        async_session, world, preparation_id=prep.id, status=RunStatus.RUNNING, key="run:redo"
    )
    await async_session.commit()

    broker = _FakeBroker()  # 没有预设结果：一旦被调用就 AssertionError
    monkeypatch.setattr(tasks_execution, "_broker", lambda: broker)

    result = await tasks_execution._submit(run.id, "a.b.c")

    assert result["skipped"] is True
    assert result["status"] == RunStatus.RUNNING.value
    assert broker.calls == []


async def test_worker提交成功回写job_id落账并转监督(
    async_session: AsyncSession, world: _World, worker_db: async_sessionmaker, monkeypatch
) -> None:
    """提交成功：先置 running 再提交 → 回写 ``seatunnel_job_id`` → 落 ``RUN_STARTED``
    → 经 Outbox 转监督（而不是 ``.delay()``）。

    先置 running 是刻意的：反过来的话，作业已经跑起来而库里还是 queued，
    这中间的任何一次重投都会再提交一次（幂等闸看的就是 status）。
    """
    from tasks import execution as tasks_execution

    prep = await _approved(async_session, world)
    run = await _make_run(
        async_session, world, preparation_id=prep.id, status=RunStatus.QUEUED, key="run:submit"
    )
    await async_session.commit()

    broker = _FakeBroker(
        ToolResult(tool=ToolName.SEATUNNEL_SUBMIT, ok=True, jti=None, detail={"job_id": "job-1"})
    )
    monkeypatch.setattr(tasks_execution, "_broker", lambda: broker)

    result = await tasks_execution._submit(run.id, "a.b.c")

    assert result == {"run_id": run.id, "status": RunStatus.RUNNING.value, "job_id": "job-1"}
    assert len(broker.calls) == 1
    assert broker.calls[0][0] is ToolName.SEATUNNEL_SUBMIT

    async with worker_db() as session:
        row = await session.get(ExecutionRun, run.id)
        assert row is not None
        assert row.status is RunStatus.RUNNING
        assert row.seatunnel_job_id == "job-1"
        assert row.started_at is not None
        assert row.error_message is None

        supervise = await session.scalar(
            select(OutboxMessage).where(OutboxMessage.topic == Topic.RUN_SUPERVISE.value)
        )
        assert supervise is not None and supervise.payload["run_id"] == run.id

        started = await _events(
            session, tenant_id=world.tenant_id, event_type=EvidenceEventType.RUN_STARTED
        )
        assert [event.payload["job_id"] for event in started] == ["job-1"]


async def test_worker提交时把发起人作为执行主体(
    async_session: AsyncSession,
    world: _World,
    worker_db: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """提交走 broker 时必须带上 ``actor_id``，而它只能来自运行行。

    ``ToolBroker`` 的第 ④b 步会拿 ``ctx.actor_id`` 去比令牌里的 ``subject_id``；
    ``actor_id=None`` 的后果是**直接拒绝**（``Capability 执行缺少操作主体``），
    也就是「整条链路一步都走不了」，而单测用的是绕过七步序的假 broker——
    所以这个缺陷在假 broker 下永远不可见，必须**显式断言**这一格。

    为什么是 ``run.triggered_by`` 而不是别的：提交令牌签给的就是这个人
    （``services/run.py`` 的 Commit 用它签 ``subject``），运行行上也记着同一个值。
    停止**不能**照抄这条（取消者 ≠ 发起者），它的主体来自 Outbox 载荷——
    见 ``test_停止成功...`` 那条用例末尾对 ``actor_id`` 的断言。
    """
    from tasks import execution as tasks_execution

    prep = await _approved(async_session, world)
    run = await _make_run(
        async_session,
        world,
        preparation_id=prep.id,
        status=RunStatus.QUEUED,
        key="run:actor",
        triggered_by=world.operator.id,
    )
    await async_session.commit()

    broker = _FakeBroker(
        ToolResult(tool=ToolName.SEATUNNEL_SUBMIT, ok=True, jti=None, detail={"job_id": "job-1"})
    )
    monkeypatch.setattr(tasks_execution, "_broker", lambda: broker)

    await tasks_execution._submit(run.id, "a.b.c")

    ctx = broker.calls[0][3]
    assert ctx.actor_id == run.triggered_by
    # 现场本身要立得住：发起人就是被授权的那个人，否则上面那条断言只是自证
    assert run.triggered_by == world.operator.id


async def test_worker提交失败落失败结论与脱敏快照(
    async_session: AsyncSession, world: _World, worker_db: async_sessionmaker, monkeypatch
) -> None:
    """broker 返回失败 → 状态 failed + ``failure_snapshot`` + ``RUN_FAILED`` 三者都有。

    失败**只留摘要**：``error_message`` 截断到 2000 字，快照里只放阶段与错误类型名。
    真实的失败现场可能带连接信息，把原文塞进快照等于把凭据写进了控制面。
    """
    from tasks import execution as tasks_execution

    prep = await _approved(async_session, world)
    run = await _make_run(
        async_session, world, preparation_id=prep.id, status=RunStatus.QUEUED, key="run:fail"
    )
    await async_session.commit()

    broker = _FakeBroker(
        ToolResult(
            tool=ToolName.SEATUNNEL_SUBMIT,
            ok=False,
            jti=None,
            detail={"error": "工具未注册：seatunnel.submit"},
        )
    )
    monkeypatch.setattr(tasks_execution, "_broker", lambda: broker)

    result = await tasks_execution._submit(run.id, "a.b.c")

    assert result["status"] == RunStatus.FAILED.value
    assert "工具未注册" in result["error"]

    async with worker_db() as session:
        row = await session.get(ExecutionRun, run.id)
        assert row is not None
        assert row.status is RunStatus.FAILED
        assert row.finished_at is not None
        assert row.failure_snapshot.get("stage") == "submit"
        assert "工具未注册" in (row.error_message or "")

        failed = await _events(
            session, tenant_id=world.tenant_id, event_type=EvidenceEventType.RUN_FAILED
        )
        assert failed and failed[-1].subject_id == str(run.id)


async def test_停止任务在终态与无job时是空操作(
    async_session: AsyncSession, world: _World, worker_db: async_sessionmaker, monkeypatch
) -> None:
    """终态 / 没有 ``seatunnel_job_id`` → 直接返回，不调 ``seatunnel.stop``。

    作业已经没了还去调一次 stop，SeaTunnel 只会回「查无此 job」——
    而那句话会被记成一次失败，把一条正常的取消写成异常。
    """
    from tasks import execution as tasks_execution

    # 先删掉退化的唯一索引，再插第二条同版本运行
    await _allow_multiple_runs(async_session)
    prep = await _create(async_session, world)
    terminal = await _make_run(
        async_session,
        world,
        preparation_id=prep.id,
        status=RunStatus.CANCELLED,
        key="run:stop-terminal",
        job_id="job-1",
    )
    no_job = await _make_run(
        async_session,
        world,
        preparation_id=prep.id,
        status=RunStatus.RUNNING,
        key="run:stop-nojob",
    )
    await async_session.commit()

    broker = _FakeBroker()  # 被调用即失败
    monkeypatch.setattr(tasks_execution, "_broker", lambda: broker)

    assert (
        await tasks_execution._stop(terminal.id, "a.b.c", "manual_cancel", subject_id=world.operator)
    )["skipped"] is True
    no_job_result = await tasks_execution._stop(
        no_job.id, "a.b.c", "manual_cancel", subject_id=world.operator.id
    )
    assert no_job_result["skipped"] is True
    assert no_job_result["reason"] == "no_job"
    assert broker.calls == []


async def test_停止失败不改状态(
    async_session: AsyncSession, world: _World, worker_db: async_sessionmaker, monkeypatch
) -> None:
    """stop 失败**不改状态**：控制面已置 cancelled，而这次调用没能让作业真停下。

    这个矛盾要留给人看（告警），而不是在这里悄悄把状态改回 running——
    否则「库里的结论」与「作业还在跑」这两件事会看起来一致。
    """
    from tasks import execution as tasks_execution

    prep = await _create(async_session, world)
    run = await _make_run(
        async_session,
        world,
        preparation_id=prep.id,
        status=RunStatus.RUNNING,
        key="run:stop-fail",
        job_id="job-9",
    )
    await async_session.commit()

    broker = _FakeBroker(
        ToolResult(
            tool=ToolName.SEATUNNEL_STOP, ok=False, jti=None, detail={"error": "job not found"}
        )
    )
    monkeypatch.setattr(tasks_execution, "_broker", lambda: broker)

    result = await tasks_execution._stop(
        run.id, "a.b.c", "manual_cancel", subject_id=world.operator.id
    )

    assert result["error"] == "job not found"
    assert len(broker.calls) == 1
    # 停止载荷里的 reason 收窄到三个枚举值之一
    assert broker.calls[0][2].reason == "manual_cancel"
    # 执行主体必须真的进了 ctx：真 broker 在 BINDING 阶段要求它非空
    # （``harness/broker.py::_expected_binding``），这里曾是 ``None`` 而假 broker
    # 不看它，所以「停作业命令永远被拒」这条缺陷在单测里完全不显形。
    assert broker.calls[0][3].actor_id == world.operator.id

    async with worker_db() as session:
        row = await session.get(ExecutionRun, run.id)
        assert row is not None and row.status is RunStatus.RUNNING


async def test_令牌不进失败快照也不进日志管线(
    async_session: AsyncSession, world: _World, worker_db: async_sessionmaker, monkeypatch
) -> None:
    """令牌原文的活动范围只有两处：Outbox 载荷与 Worker 内存。

    这里用**真实签发的令牌**当探针（不是假字符串）：走一遍失败提交，
    断言它没落进 ``failure_snapshot`` / ``error_message``；
    再把它喂给日志脱敏处理器，断言日志管线会把它掩码成 ``[REDACTED_JWT]``——
    即使某条路径真把它写进了日志，落盘的也是掩码后的形态。
    """
    from tasks import execution as tasks_execution

    prep = await _approved(async_session, world)
    run = await _make_run(
        async_session, world, preparation_id=prep.id, status=RunStatus.QUEUED, key="run:secret"
    )
    await async_session.commit()

    token = run_service._capability_issuer().issue(
        preparation_id=prep.id,
        subject_id=world.operator.id,
        tool=ToolName.SEATUNNEL_SUBMIT,
        env=Env.DEV,
        artifact_sha256="a" * 64,
        tenant_id=world.tenant_id,
        project_id=world.project_id,
        now=utcnow(),
    ).token
    assert token.count(".") == 2  # 真的是 JWT 形态的探针

    broker = _FakeBroker(
        ToolResult(
            tool=ToolName.SEATUNNEL_SUBMIT,
            ok=False,
            jti=None,
            detail={"error": "工具未注册", "capability": token},
        )
    )
    monkeypatch.setattr(tasks_execution, "_broker", lambda: broker)

    await tasks_execution._submit(run.id, token)

    async with worker_db() as session:
        row = await session.get(ExecutionRun, run.id)
        assert row is not None
        # 先证明现场确实被记下来了：否则「快照里没有令牌」只是因为它压根是空的
        assert row.failure_snapshot, "失败现场没落库时，下面那条断言会恒真（假绿）"
        assert token not in repr(row.failure_snapshot)
        assert token not in (row.error_message or "")

    # 日志管线必须把 JWT 形态的值掩码掉
    scrubbed = redact_processor(None, "info", {"event": "x", "payload": {"capability": token}})
    assert token not in repr(scrubbed)
    assert "[REDACTED_JWT]" in repr(scrubbed)


# ================================================================ 契约


async def test_worker提交上下文的字段集合只装worker要的东西(
    async_session: AsyncSession, world: _World
) -> None:
    """``RunSubmitContext`` 与 HTTP DTO 是两套字段集合，各自的边界都要钉住。

    - Worker 侧要 ``hocon_content`` 与 ``secret_refs``（**只准在这一个进程里存在**），
      用 Pydantic 而不是 dict 传递就是为了能声明这一点。
    - HTTP 侧的 ``RunSummary`` / ``RunDetail`` 不含任何 ``token`` 字段；
      ``RunDetail.capability_id`` 只是登记行的 id，不是令牌。
    """
    prep = await _create(async_session, world)
    run = await _make_run(
        async_session, world, preparation_id=prep.id, status=RunStatus.QUEUED, key="run:ctx"
    )
    await async_session.commit()

    ctx = await run_service.load_for_submit(async_session, run_id=run.id)

    assert isinstance(ctx, RunSubmitContext)
    assert set(RunSubmitContext.model_fields) == {
        "run_id",
        "tenant_id",
        "project_id",
        "preparation_id",
        "pipeline_id",
        "pipeline_version_id",
        "version_no",
        "artifact_sha256",
        "environment",
        "hocon_artifact_id",
        "hocon_content",
        "shadow_table",
        "formal_table",
        "error_table",
        "secret_refs",
        "supervision_contract_id",
        "quality_contract_id",
    }
    assert ctx.environment is Env.DEV
    assert ctx.artifact_sha256 == "a" * 64
    assert ctx.shadow_table == SHADOW
    # 凭据以引用形式流转：路径 + 版本 + 字段名，没有值
    assert ctx.secret_refs[0]["path"] == "kv/elt/orders"
    assert ctx.secret_refs[0]["version"] == 3

    http_fields = set(RunSummary.model_fields) | set(RunDetail.model_fields)
    assert not any("token" in name for name in http_fields)


def test_运行路由挂在v1前缀下() -> None:
    """五个运行路由的路径与挂在 ``API_V1_PREFIX`` 下（be-14 §4 的接口表）。

    路径拼错不会报错，只会让前端 404——而 404 会被当成「后端没实现」，
    排查方向被带到里程碑进度上。用 ``create_app()`` 的真实 openapi 断言，
    而不是读 ``api/runs.py`` 的常量（那测的是常量，不是挂载）。
    """
    from main import create_app

    paths = create_app().openapi()["paths"]
    prefix = settings.API_V1_PREFIX

    assert f"{prefix}/runs" in paths
    assert {"get", "post"} <= set(paths[f"{prefix}/runs"])
    assert "get" in paths[f"{prefix}/runs/{{run_id}}"]
    assert "post" in paths[f"{prefix}/runs/{{run_id}}/pause"]
    assert "post" in paths[f"{prefix}/runs/{{run_id}}/cancel"]
