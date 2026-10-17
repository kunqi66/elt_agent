"""be-18 原子发布与受管回滚（服务层）。

这是**全系统风险最高的一次动作**（影子表换正式表 = P0），所以本文件的断言几乎都在
证明「没做成」而不是「做成了」。四条主线：

1. **发布只可能发生一次**。Swap 是**对合变换**：执行两次会把刚发布的正式表悄悄换回
   旧数据，而且不报任何错。所以「重投不再执行第二次」「令牌被消费过就不再发第二次」
   这类用例是本文件的红心——它们守的是「静默错」。
2. **失败路径不许碰正式表**。预检不过、DDL 失败、对账 unknown，三条路都断言
   正式表指纹**一字未变**。这是本模块唯一的不可协商项。
3. **判定以执行后对账为准**，不以 DDL 返回值或工具自报为凭。因此有用例专门让
   publisher 撒谎（报 published 却没换、报成功却留着影子表），断言我们**不信它**。
4. **回滚范围四连**：请求体里没有表名，服务端从运行行读出并重算后缀，
   断言「一次误操作最多删掉一个影子表」。

目标端是**内存假发布器**（``_TARGET``）：本机没有 Doris、没有演示 MySQL（CLAUDE.md §9），
真引擎上的 DDL 语义**未经验证**（见交付报告）。假发布器按真实语义换表
（把两张表的观测互换），因此对账逻辑、幂等短路、范围校验这些**与引擎无关**的部分
是真的被验证过的；只有「``REPLACE WITH TABLE`` 在 Doris 上到底通不通」没被验证。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import fakeredis.aioredis
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from pydantic import ValidationError as ValidationErr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from clients.target import registry as target_registry
from clients.target.base import (
    FINGERPRINT_SCOPE,
    CleanupResult,
    ColumnInfo,
    PublishResult,
    TableInfo,
    TargetError,
    TargetPrecheckError,
)
from clients.target.executor import register as register_target_executors
from core.config import settings
from core.errors import (
    AppError,
    ErrorCode,
    FingerprintMismatchError,
    RoleRequiredError,
    RunConflictError,
    ValidationError,
)
from engines.policy.roles import RoleName
from harness.capability import CapabilityIssuer, CapabilityVerifier
from harness.ledger import EvidenceLedger, LedgerEvent
from models.datasource import DataSource
from models.enums import (
    DataSourceType,
    Env,
    EvidenceEventType,
    MemberStatus,
    PipelineStatus,
    PreparationStatus,
    RiskLevel,
    RunStatus,
    SensitiveLevel,
    ToolName,
)
from models.evidence import EvidenceEvent, OutboxMessage
from models.execution import ExecutionRun, QualityReport
from models.governance import Capability, Preparation
from models.pipeline import Pipeline, PipelineVersion
from models.tenant import AppUser, ProjectMember, RoleAssignment, Tenant
from schemas.auth import SubjectContext
from schemas.publish import RollbackRequest
from services import publish as publish_service
from services.preparation import shadow_table_name

TENANT = 1
ISSUER = settings.CAPABILITY_ISSUER
ARTIFACT = "a" * 64
FORMAL = "orders_wide"
SHADOW = shadow_table_name(FORMAL, 1)
REJECT = "orders_wide_reject"
# 基准时间取真实时钟往前一分钟（与 tests/test_harness_broker 同一理由）：
# 令牌验签会拒绝「签发时间在未来」，写死一个未来时刻会让所有用例以 CAPABILITY_INVALID 红。
NOW = (datetime.now(UTC) - timedelta(minutes=1)).replace(microsecond=0)


# ---------------------------------------------------------------- 内存目标端


@dataclass
class _FakeTable:
    """一张表在本进程里的「物理形态」：指纹与列形态。"""

    fingerprint: str
    rows: int
    columns: tuple[ColumnInfo, ...]


def _columns() -> tuple[ColumnInfo, ...]:
    return (
        ColumnInfo(name="id", ordinal=0, type_text="bigint", nullable=False),
        ColumnInfo(name="amount", ordinal=1, type_text="decimal(12,2)", nullable=True),
    )


#: 目标端的**模块级**状态：publisher 每次动作都会被重新构造（``build_publisher``），
#: 若状态挂在实例上，「换完之后再观测」看到的会是另一个世界——那正是本文件要验的东西。
_TARGET: dict[str, _FakeTable] = {}
#: 副作用记录器。与 tests/test_harness_broker 同一取舍：执行器与 publisher 只在进程内
#: 被引用一次，闭包捕获「当前用例」的列表会让后续用例永远看到空列表，
#: 于是「没调用过」这类断言全部假绿。
_CALLS: list[str] = []
#: 故障注入开关（§9 的测试点）。
_CONTROL: dict[str, Any] = {}
#: 假的 Redis。独占键与令牌消费标记都落在它上面——这两条**必须**被替换成真的实现，
#: 因为它们本身就是要验的对象（fail-closed、SETNX 语义）。
_REDIS: dict[str, Any] = {}
#: 账本。与 ``services/publish`` 里那个模块级实例是同一个类，互不干扰。
_LEDGER = EvidenceLedger()


def _reset_target() -> None:
    _TARGET.clear()
    _TARGET[SHADOW] = _FakeTable(fingerprint="10:550", rows=10, columns=_columns())
    _TARGET[FORMAL] = _FakeTable(fingerprint="4:120", rows=4, columns=_columns())
    _CALLS.clear()
    _CONTROL.clear()


class FakeTargetPublisher:
    """按真实语义工作的假发布器。**只做两件事**：观测、互换。"""

    def __init__(self, endpoint: Any) -> None:
        self.endpoint = endpoint

    async def describe(self, table: str) -> TableInfo:
        if _CONTROL.get("unreadable"):
            # 「连得上但读不出指纹」：对账只能判 unknown（§6.2 第三行）
            raise TargetError("目标端不可读", code=ErrorCode.DATASOURCE_UNREACHABLE)
        row = _TARGET.get(table)
        if row is None:
            return TableInfo(name=table, exists=False)
        return TableInfo(
            name=table,
            exists=True,
            columns=row.columns,
            row_count=row.rows,
            table_fingerprint=row.fingerprint,
            primary_key_columns=("id",),
        )

    async def exists(self, table: str) -> bool:
        return table in _TARGET

    async def publish(self, run: Any, plan: Any) -> PublishResult:
        _CALLS.append(f"publish:{plan.shadow_table}")
        if plan.shadow_table not in _TARGET:
            raise TargetPrecheckError(
                "影子表不存在", details={"reason": "shadow_missing", "table": plan.shadow_table}
            )
        mode = _CONTROL.get("publish_mode", "swap")
        if mode == "unreadable_after":
            # DDL 报成功、目标端随后就读不出指纹：只有**发布之后**不可读，
            # 预检阶段仍然读得到（否则这道闸会在预检就拦下，验不到 §6.2 第三行）
            _CONTROL["unreadable"] = True
            return PublishResult(
                ok=True,
                final_state="published",
                published_table=plan.formal_table,
                previous_data_table=plan.shadow_table,
                detail={"state": "published", "fingerprint_scope": FINGERPRINT_SCOPE},
            )
        if mode == "lie_published":
            # 报成功但**没换**：服务端必须靠自己的观测量出不生效，而不是信这句话
            return PublishResult(
                ok=True,
                final_state="published",
                published_table=plan.formal_table,
                previous_data_table=plan.shadow_table,
                detail={"state": "published", "fingerprint_scope": FINGERPRINT_SCOPE},
            )
        pre_shadow = _TARGET[plan.shadow_table].fingerprint
        _TARGET[plan.shadow_table], _TARGET[plan.formal_table] = (
            _TARGET[plan.formal_table],
            _TARGET[plan.shadow_table],
        )
        ddl_error = _CONTROL.get("ddl_error")
        return PublishResult(
            ok=True,
            final_state="published",
            published_table=plan.formal_table,
            previous_data_table=plan.shadow_table,
            pre_fingerprint=pre_shadow,
            post_fingerprint=pre_shadow,
            detail={
                "state": "published",
                "fingerprint_scope": FINGERPRINT_SCOPE,
                "shadow_table": plan.shadow_table,
                "formal_table": plan.formal_table,
                "affected_rows": 0,
                "post_formal_rows": _TARGET[plan.formal_table].rows,
                **({"ddl_error": ddl_error} if ddl_error else {}),
            },
        )

    async def rollback_cleanup(self, run: Any, plan: Any) -> CleanupResult:
        _CALLS.append(f"rollback:{plan.shadow_table}")
        if _CONTROL.get("rollback_mode") == "lie_dropped":
            # 报成功但没删：服务端要自己去问「影子表还在不在」
            return CleanupResult(
                ok=True,
                shadow_dropped=True,
                formal_unchanged=True,
                detail={"state": "dropped", "shadow_table": plan.shadow_table},
            )
        dropped = _TARGET.pop(plan.shadow_table, None) is not None
        return CleanupResult(
            ok=True,
            shadow_dropped=dropped,
            formal_unchanged=True,
            detail={
                "state": "dropped" if dropped else "already_absent",
                "shadow_table": plan.shadow_table,
                "formal_table": plan.formal_table,
            },
        )


# ---------------------------------------------------------------- 夹具


@pytest.fixture(scope="module")
def keypair(tmp_path_factory: pytest.TempPathFactory) -> tuple[str, str]:
    """一对 Ed25519 密钥（本机没有 ``/run/secrets``）。"""
    directory = tmp_path_factory.mktemp("publish-keys")
    private = ed25519.Ed25519PrivateKey.generate()
    priv_path = directory / "private.pem"
    pub_path = directory / "public.pem"
    priv_path.write_bytes(
        private.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    pub_path.write_bytes(
        private.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    )
    return str(priv_path), str(pub_path)


@pytest.fixture(autouse=True)
def _wire_seams(monkeypatch: pytest.MonkeyPatch, keypair: tuple[str, str]) -> None:
    """把服务层的六个外部接缝换成进程内实现。

    逐一列出是有意的：每一个都对应一条真实的外部依赖（目标库、Redis、Vault、私钥文件），
    而**没有被替换的那些**才是本文件真正在验的东西（broker、执行器、证据链、状态机）。
    """
    register_target_executors()  # 真执行器 + 假 publisher：验的是工具的整条链路
    _reset_target()
    _REDIS.clear()
    _REDIS["client"] = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(publish_service, "get_redis", lambda: _REDIS["client"])
    monkeypatch.setattr(target_registry, "build_publisher", lambda endpoint: FakeTargetPublisher(endpoint))

    async def _creds(row: Any) -> dict[str, str]:
        return {"username": "etl", "password": "测试口令（不写真实值）"}

    monkeypatch.setattr(target_registry, "materialize_credentials", _creds)
    monkeypatch.setattr(
        publish_service,
        "_capability_issuer",
        lambda: CapabilityIssuer(
            private_key_path=keypair[0],
            key_id=settings.CAPABILITY_KEY_ID,
            issuer=ISSUER,
            ttl_sec=settings.CAPABILITY_TTL_SEC,
        ),
    )
    monkeypatch.setattr(
        publish_service,
        "_build_verifier",
        lambda: CapabilityVerifier(
            public_key_path=keypair[1], issuer=ISSUER, ttl_sec=settings.CAPABILITY_TTL_SEC
        ),
    )


# ---------------------------------------------------------------- 种子


@dataclass
class _World:
    tenant_id: int
    project_id: int
    pipeline_id: int
    version_id: int
    preparation_id: int
    run_id: int
    operator: AppUser
    outsider: AppUser
    calls: list[str] = field(default_factory=lambda: _CALLS)

    def ctx(self, user: AppUser | None = None) -> SubjectContext:
        who = user or self.operator
        return SubjectContext(
            user_id=who.id,
            tenant_id=self.tenant_id,
            username=who.username,
            display_name=who.display_name,
            request_id=f"req_{who.username}",
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


async def _seed(
    session: AsyncSession,
    *,
    run_status: RunStatus = RunStatus.RUNNING,
    passed: bool = True,
    with_report: bool = True,
    artifact_mismatch: bool = False,
    read_rows: int = 10,
) -> _World:
    """一次「装载已跑完、质量已判定」的现场。**全部走 ORM 铺行**。

    不重跑生成/冻结/提交三条链路：那些链路的 bug 会与本文件的失败原因混在一起
    （与 ``tests/test_preparation.py`` 同一口径）。
    """
    tenant = Tenant(code="acme", name="Acme", status="active")
    session.add(tenant)
    await session.flush()

    operator = await _user(session, tenant.id, "olive")
    outsider = await _user(session, tenant.id, "nobody")

    from schemas.project import ProjectCreate
    from services import project as project_service

    project = await project_service.create_project(
        session,
        SubjectContext(
            user_id=operator.id,
            tenant_id=tenant.id,
            username=operator.username,
            display_name=operator.display_name,
            request_id="req_seed",
        ),
        ProjectCreate(code="orders", name="订单", env="dev"),
    )
    await _grant(session, project.id, operator, RoleName.OPERATOR)
    await _grant(session, project.id, outsider, RoleName.AUDITOR)

    target = DataSource(
        tenant_id=tenant.id,
        project_id=project.id,
        name="目标库",
        type=DataSourceType.DORIS,
        env=Env.DEV,
        endpoint={"host": "10.0.0.6", "port": 9030, "database": "dw"},
        secret_ref_id=None,
        sensitive_level=SensitiveLevel.INTERNAL,
        tags=[],
        status="active",
    )
    session.add(target)
    await session.flush()

    pipeline = Pipeline(
        tenant_id=tenant.id,
        project_id=project.id,
        name="orders_to_dw",
        source_data_source_id=target.id,
        target_data_source_id=target.id,
        status=PipelineStatus.VERSION_FROZEN,
        created_by=operator.id,
    )
    session.add(pipeline)
    await session.flush()

    version = PipelineVersion(
        pipeline_id=pipeline.id,
        version_no=1,
        etl_plan_id=0,
        hocon_artifact_id=0,
        quality_contract_id=0,
        supervision_contract_id=0,
        source_metadata_profile_id=0,
        generation_context={},
        # 制品指纹与准备单必须一致（§6.1 条件 4）。造一份不一致的现场来验那条闸。
        sha256=("b" * 64) if artifact_mismatch else ARTIFACT,
        frozen_by=operator.id,
        frozen_at=NOW.replace(tzinfo=None),
        note="",
    )
    session.add(version)
    await session.flush()
    pipeline.current_version_id = version.id

    preparation = Preparation(
        tenant_id=tenant.id,
        project_id=project.id,
        pipeline_version_id=version.id,
        input_fingerprint="c" * 64,
        artifact_sha256=ARTIFACT,
        environment=Env.DEV,
        risk_level=RiskLevel.P1,
        status=PreparationStatus.APPROVED,
        created_by=operator.id,
        frozen_at=NOW.replace(tzinfo=None),
        expires_at=(NOW + timedelta(hours=1)).replace(tzinfo=None),
    )
    session.add(preparation)
    await session.flush()

    run = ExecutionRun(
        tenant_id=tenant.id,
        project_id=project.id,
        pipeline_version_id=version.id,
        preparation_id=preparation.id,
        idempotency_key="idem-seed",
        status=run_status,
        shadow_table=SHADOW,
        formal_table=FORMAL,
        error_table=REJECT,
        read_rows=read_rows,
        triggered_by=operator.id,
    )
    session.add(run)
    await session.flush()

    if with_report:
        session.add(
            QualityReport(
                run_id=run.id,
                valid_rows=read_rows,
                rejected_rows=0,
                reject_rate=0,
                passed=passed,
                generated_at=NOW.replace(tzinfo=None),
            )
        )
    await session.commit()

    return _World(
        tenant_id=tenant.id,
        project_id=project.id,
        pipeline_id=pipeline.id,
        version_id=version.id,
        preparation_id=preparation.id,
        run_id=run.id,
        operator=operator,
        outsider=outsider,
    )


async def _reload(session: AsyncSession, run_id: int) -> ExecutionRun:
    """从库里重取运行行（不走缓存的旧对象）。

    ``populate_existing`` 而不是 ``expire_all()``：后者会把现场里的 AppUser 等对象
    一并过期，之后任何 ``world.operator.id`` 都会触发一次惰性加载，
    而惰性加载在异步会话里是 MissingGreenlet 错误——排查方向会被带到 ORM 配置上。
    """
    return await session.scalar(
        select(ExecutionRun)
        .where(ExecutionRun.id == run_id)
        .execution_options(populate_existing=True)
    )


async def _events(
    session: AsyncSession, *, tenant_id: int, event_type: EvidenceEventType
) -> list[EvidenceEvent]:
    rows = await session.scalars(
        select(EvidenceEvent)
        .where(EvidenceEvent.tenant_id == tenant_id, EvidenceEvent.event_type == event_type.value)
        .order_by(EvidenceEvent.seq.asc())
    )
    return list(rows.all())


def _fingerprints() -> dict[str, str]:
    return {name: table.fingerprint for name, table in _TARGET.items()}


# ---------------------------------------------------------------- 成功路径


async def test_publish_swaps_tables_and_records_evidence(async_session: AsyncSession) -> None:
    world = await _seed(async_session)
    before = _fingerprints()

    dto = await publish_service.publish_run(
        async_session, tenant_id=world.tenant_id, run_id=world.run_id
    )

    assert dto.publish_state == "published"
    assert dto.final_state == "published"
    assert dto.pre_fingerprint == before[SHADOW]
    assert dto.post_fingerprint == before[SHADOW]  # 正式表拿到了本次数据
    assert dto.fingerprint_scope == FINGERPRINT_SCOPE
    assert _fingerprints() == {FORMAL: before[SHADOW], SHADOW: before[FORMAL]}
    assert (await _reload(async_session, world.run_id)).status is RunStatus.SUCCEEDED

    events = await _events(
        async_session, tenant_id=world.tenant_id, event_type=EvidenceEventType.SWAP_SUCCEEDED
    )
    assert len(events) == 1
    payload = dict(events[0].payload)
    assert payload["final_state"] == "published"
    assert payload["pre_fingerprint"] == before[SHADOW]
    assert payload["post_fingerprint"] == before[SHADOW]
    # 指纹口径必须随结论一起落账：只覆盖行数与主键集合，不覆盖全部列
    assert payload["fingerprint_scope"] == FINGERPRINT_SCOPE
    assert payload["actor_id"] == world.operator.id
    assert events[0].artifact_hash == ARTIFACT


async def test_second_publish_attempt_is_short_circuited(async_session: AsyncSession) -> None:
    """**本文件最重要的一条**：Swap 是对合变换，执行第二次会把数据悄悄换回去。"""
    world = await _seed(async_session)
    await publish_service.publish_run(async_session, tenant_id=world.tenant_id, run_id=world.run_id)
    after_first = _fingerprints()
    assert world.calls.count(f"publish:{SHADOW}") == 1

    dto = await publish_service.publish_run(
        async_session, tenant_id=world.tenant_id, run_id=world.run_id
    )

    assert world.calls.count(f"publish:{SHADOW}") == 1  # 没有第二次副作用
    assert _fingerprints() == after_first  # 数据没被换回去
    assert dto.publish_state == "published"
    assert dto.final_state == "published"
    assert len(
        await _events(
            async_session, tenant_id=world.tenant_id, event_type=EvidenceEventType.SWAP_SUCCEEDED
        )
    ) == 1


async def test_publish_is_blocked_when_a_consumed_capability_has_no_verdict(
    async_session: AsyncSession,
) -> None:
    """「DDL 已发出、结论没落账」之后的重投：**绝不许再发一次**。

    这一格是「令牌被消费过」比任何事件都硬的原因——它由 broker 在调用执行器**之前**
    写入，所以它一定早于任何结论；而重投执行第二次 Swap 的后果是静默回滚。
    """
    world = await _seed(async_session)
    # 伪造一枚「签发过且已消费」的令牌（真实流程里由 broker 的 consume 写 consumed_at）
    async_session.add(
        Capability(
            jti=uuid.UUID("00000000-0000-0000-0000-0000000000a1"),
            preparation_id=world.preparation_id,
            subject_id=world.operator.id,
            tool=ToolName.TARGET_PUBLISH_SWAP.value,
            environment=Env.DEV,
            artifact_sha256=ARTIFACT,
            token_sha256="d" * 64,
            signature_alg="EdDSA",
            key_id="test",
            issued_at=NOW.replace(tzinfo=None),
            expires_at=(NOW + timedelta(minutes=5)).replace(tzinfo=None),
            consumed_at=NOW.replace(tzinfo=None),
        )
    )
    await async_session.commit()
    before = _fingerprints()

    with pytest.raises(AppError) as err:
        await publish_service.publish_run(
            async_session, tenant_id=world.tenant_id, run_id=world.run_id
        )

    assert str(err.value.code) == str(ErrorCode.SWAP_FAILED)
    assert err.value.details["reason"] == "prior_attempt_without_verdict"
    assert world.calls == []  # 一次副作用都没发生
    assert _fingerprints() == before  # 正式表一个字节都没动

    run = await _reload(async_session, world.run_id)
    assert run.status is RunStatus.FAILED
    # 未决态必须写进现场：只看「失败」两字无法知道数据到底换没换成
    assert run.failure_snapshot["swap"]["status"] == "unknown"
    assert run.failure_snapshot["swap"]["reason"] == "prior_attempt_without_verdict"


# ---------------------------------------------------------------- 不发布（失败路径）


async def test_quality_gate_blocks_publish_with_zero_publisher_calls(
    async_session: AsyncSession,
) -> None:
    """质量未过：**没有令牌、没有 publisher 调用、正式表原样**（§6.1 条件 3）。"""
    world = await _seed(async_session, passed=False)
    before = _fingerprints()

    with pytest.raises(AppError) as err:
        await publish_service.publish_run(
            async_session, tenant_id=world.tenant_id, run_id=world.run_id
        )

    assert err.value.code is ErrorCode.QUALITY_REJECTED
    assert world.calls == []  # 连只读预检都没走到：门禁在预检之前
    assert _fingerprints() == before
    assert await _events(
        async_session, tenant_id=world.tenant_id, event_type=EvidenceEventType.CAPABILITY_ISSUED
    ) == []

    run = await _reload(async_session, world.run_id)
    assert run.status is RunStatus.FAILED
    assert run.failure_snapshot["swap"]["reason"] == "quality_not_passed"
    events = await _events(
        async_session, tenant_id=world.tenant_id, event_type=EvidenceEventType.SWAP_FAILED
    )
    assert len(events) == 1 and dict(events[0].payload)["error_code"] == str(ErrorCode.QUALITY_REJECTED)


async def test_missing_quality_report_is_treated_as_failure(async_session: AsyncSession) -> None:
    """没有质量报告 ≠ 通过。**缺报告**与**报告不通过**必须是同一条路。"""
    world = await _seed(async_session, with_report=False)
    with pytest.raises(AppError) as err:
        await publish_service.publish_run(
            async_session, tenant_id=world.tenant_id, run_id=world.run_id
        )
    assert err.value.details["reason"] == "quality_report_missing"
    assert world.calls == []


async def test_precheck_failure_leaves_formal_untouched(async_session: AsyncSession) -> None:
    """结构性预检不过（影子表为空）：**没有执行任何 DDL**，正式表原样。

    空影子表是最危险的一种「成功」：装载一条没读进来时，Swap 会把正式表换成空表，
    而质量报告可能照样 passed（0 行没有违约行）。
    """
    world = await _seed(async_session, read_rows=5)  # read_rows>0 ⇒ 不允许空影子表
    _TARGET[SHADOW] = _FakeTable(fingerprint="0:0", rows=0, columns=_columns())
    before = _fingerprints()

    with pytest.raises(TargetPrecheckError) as err:
        await publish_service.publish_run(
            async_session, tenant_id=world.tenant_id, run_id=world.run_id
        )

    assert err.value.details["reason"] == "empty_shadow"
    assert _fingerprints() == before
    run = await _reload(async_session, world.run_id)
    assert run.status is RunStatus.FAILED
    assert run.failure_snapshot["swap"]["status"] == "not_published"


async def test_artifact_mismatch_is_refused_before_any_token(async_session: AsyncSession) -> None:
    """准备单与冻结版本指向不同制品（§6.1 条件 4）：问不出「发布的是不是被审批的那一版」就不许继续。"""
    world = await _seed(async_session, artifact_mismatch=True)
    with pytest.raises(FingerprintMismatchError):
        await publish_service.publish_run(
            async_session, tenant_id=world.tenant_id, run_id=world.run_id
        )
    assert world.calls == []


async def test_unknown_state_when_reconcile_cannot_read_fingerprints(
    async_session: AsyncSession,
) -> None:
    """发布后读不到指纹 → ``unknown`` 并落 ``failure_snapshot.swap.status``（§6.2 第三行）。

    未决态是**必须让运维看见**的：这时正式表可能已经被换过，只是我们读不出来。
    """
    world = await _seed(async_session)
    _CONTROL["publish_mode"] = "unreadable_after"

    with pytest.raises(AppError) as err:
        await publish_service.publish_run(
            async_session, tenant_id=world.tenant_id, run_id=world.run_id
        )

    assert err.value.code is ErrorCode.SWAP_FAILED
    run = await _reload(async_session, world.run_id)
    assert run.status is RunStatus.FAILED
    swap = run.failure_snapshot["swap"]
    assert swap["status"] == "unknown"
    assert swap["reason"] == "reconcile_unavailable"
    assert swap["fingerprint_scope"] == FINGERPRINT_SCOPE


async def test_disagreement_with_tool_verdict_degrades_to_unknown(
    async_session: AsyncSession,
) -> None:
    """工具说 published、服务端观测到没换 → ``unknown``（两个独立观测分歧时不赌）。"""
    world = await _seed(async_session)
    _CONTROL["publish_mode"] = "lie_published"
    before = _fingerprints()

    with pytest.raises(AppError):
        await publish_service.publish_run(
            async_session, tenant_id=world.tenant_id, run_id=world.run_id
        )

    assert _fingerprints() == before  # 数据确实没换
    run = await _reload(async_session, world.run_id)
    assert run.status is RunStatus.FAILED
    assert run.failure_snapshot["swap"]["status"] == "unknown"
    assert run.failure_snapshot["swap"]["reason"] == "reconcile_disagreement"


async def test_ddl_error_that_took_effect_is_reported_as_published(
    async_session: AsyncSession,
) -> None:
    """DDL 报错但**确实生效**：结论以对账为准，记成功；DDL 报错文本进日志与现场。

    这正是「超时但已生效」：若按 DDL 的返回值判定，这次真实的发布会被记成失败，
    于是运维会对着一张已经换过的正式表去「重试」——第二次 Swap 就是静默回滚。
    """
    world = await _seed(async_session)
    _CONTROL["ddl_error"] = "Lost connection to MySQL server during query"
    before = _fingerprints()

    dto = await publish_service.publish_run(
        async_session, tenant_id=world.tenant_id, run_id=world.run_id
    )

    assert dto.publish_state == "published"
    assert _fingerprints() == {FORMAL: before[SHADOW], SHADOW: before[FORMAL]}
    assert (await _reload(async_session, world.run_id)).status is RunStatus.SUCCEEDED


# ---------------------------------------------------------------- 状态机与并发


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (RunStatus.QUEUED, "run_not_started"),
        (RunStatus.ROLLED_BACK, "run_rolled_back"),
        (RunStatus.ROLLING_BACK, "run_not_publishable"),
    ],
)
async def test_publish_refuses_non_running_states(
    async_session: AsyncSession, status: RunStatus, reason: str
) -> None:
    """非法转移：抢占的**条件 UPDATE 未命中**时才去读状态——不是先查后写。"""
    world = await _seed(async_session, run_status=status)
    with pytest.raises(RunConflictError) as err:
        await publish_service.publish_run(
            async_session, tenant_id=world.tenant_id, run_id=world.run_id
        )
    assert err.value.details["reason"] == reason
    assert world.calls == []
    # 状态类拒绝**不写 SWAP_FAILED**（那是页面过期，不是攻击）；只留一条 run.failed
    assert await _events(
        async_session, tenant_id=world.tenant_id, event_type=EvidenceEventType.SWAP_FAILED
    ) == []
    assert (await _reload(async_session, world.run_id)).status is status


async def test_publish_on_succeeded_run_is_idempotent_without_evidence(
    async_session: AsyncSession,
) -> None:
    """``succeeded`` 上的重复发布：幂等冲突，**不写证据**。

    把它记成「有人试图非法发布」会让审计里出现一堆并不存在的越权尝试
    （这条路径的正常来源是 Celery 重投）。
    """
    world = await _seed(async_session, run_status=RunStatus.SUCCEEDED)
    with pytest.raises(RunConflictError) as err:
        await publish_service.publish_run(
            async_session, tenant_id=world.tenant_id, run_id=world.run_id
        )
    assert err.value.details["reason"] == "already_published"
    assert await _events(
        async_session, tenant_id=world.tenant_id, event_type=EvidenceEventType.RUN_FAILED
    ) == []


async def test_concurrent_publish_is_excluded_by_the_redis_claim(
    async_session: AsyncSession,
) -> None:
    """独占键已存在 → 拒绝。状态条件 UPDATE 把 running 写成 running，**本身不排他**。"""
    world = await _seed(async_session)
    await publish_service.get_redis().set(
        f"elt:publish:claim:{world.tenant_id}:{world.run_id}", "1", nx=True, ex=300
    )

    with pytest.raises(RunConflictError) as err:
        await publish_service.publish_run(
            async_session, tenant_id=world.tenant_id, run_id=world.run_id
        )

    assert err.value.details["reason"] == "publish_in_progress"
    assert world.calls == []


async def test_publish_claim_is_released_after_a_failed_attempt(
    async_session: AsyncSession,
) -> None:
    """临界区里的失败路径也必须放锁（``finally``）：不放锁会让一次失败把这条 run 锁到 TTL。

    这里用**预检失败**而不是门禁失败：门禁在临界区**之前**，它失败时锁还没被拿过。
    """
    world = await _seed(async_session, read_rows=5)
    _TARGET[SHADOW] = _FakeTable(fingerprint="0:0", rows=0, columns=_columns())

    with pytest.raises(TargetPrecheckError):
        await publish_service.publish_run(
            async_session, tenant_id=world.tenant_id, run_id=world.run_id
        )

    assert await publish_service.get_redis().get(
        f"elt:publish:claim:{world.tenant_id}:{world.run_id}"
    ) is None


# ---------------------------------------------------------------- 回滚


async def test_rollback_cleans_shadow_and_never_touches_formal(async_session: AsyncSession) -> None:
    world = await _seed(async_session, run_status=RunStatus.FAILED)
    formal_before = _fingerprints()[FORMAL]

    accepted = await publish_service.request_rollback(
        async_session,
        subject=world.ctx(),
        run_id=world.run_id,
        idempotency_key="idem-rb-1",
        reason="装载失败",
    )
    assert accepted.status is RunStatus.ROLLING_BACK
    assert accepted.already_rolled_back is False
    assert accepted.attempt == 1
    # 受理**不等于**执行：受理阶段不碰目标端
    assert world.calls == []
    assert (await _reload(async_session, world.run_id)).status is RunStatus.ROLLING_BACK

    message = await async_session.scalar(
        select(OutboxMessage).where(OutboxMessage.topic == "etl.target.rollback")
    )
    assert message is not None
    assert message.idempotency_key == f"rollback:run:{world.run_id}:a1"
    token = dict(message.payload)["capability"]
    # 命令载荷**只含引用**：run_id + 令牌，不含任何连接信息
    assert set(dict(message.payload)) == {"run_id", "capability"}

    done = await publish_service.execute_rollback(
        async_session, tenant_id=world.tenant_id, run_id=world.run_id, capability=token
    )

    assert done.status is RunStatus.ROLLED_BACK
    assert done.already_rolled_back is False
    assert SHADOW not in _TARGET  # 影子表被清理
    assert _fingerprints() == {FORMAL: formal_before}  # 正式表一字未变
    assert world.calls == [f"rollback:{SHADOW}"]

    events = await _events(
        async_session, tenant_id=world.tenant_id, event_type=EvidenceEventType.ROLLBACK_SUCCEEDED
    )
    assert len(events) == 1
    assert dict(events[0].payload)["formal_unchanged"] is True


async def test_repeated_rollback_requests_are_idempotent(async_session: AsyncSession) -> None:
    """两层幂等：已回滚 → 幂等返回；正在回滚（窗口内）→ 409 而不是再删一次。"""
    world = await _seed(async_session, run_status=RunStatus.FAILED)
    await publish_service.request_rollback(
        async_session, subject=world.ctx(), run_id=world.run_id, idempotency_key="idem-rb-2"
    )

    with pytest.raises(RunConflictError) as err:
        await publish_service.request_rollback(
            async_session, subject=world.ctx(), run_id=world.run_id, idempotency_key="idem-rb-3"
        )
    assert err.value.details["reason"] == "rollback_in_progress"
    assert err.value.details["attempt"] == 1
    # 只入队了一条命令：重复点击不会产生第二条真实命令
    messages = (
        await async_session.scalars(select(OutboxMessage).where(OutboxMessage.topic == "etl.target.rollback"))
    ).all()
    assert len(messages) == 1

    await publish_service.execute_rollback(
        async_session,
        tenant_id=world.tenant_id,
        run_id=world.run_id,
        capability=dict(messages[0].payload)["capability"],
    )
    again = await publish_service.request_rollback(
        async_session, subject=world.ctx(), run_id=world.run_id, idempotency_key="idem-rb-4"
    )
    assert again.already_rolled_back is True
    assert again.status is RunStatus.ROLLED_BACK


async def test_rollback_reentry_after_stale_window_bumps_attempt(
    async_session: AsyncSession,
) -> None:
    """上次 worker 死掉留下的 ``rolling_back``：超窗口后可重入，``attempt`` +1（§6.5）。

    窗口 = Capability TTL：窗口外意味着上次那张令牌早已过期，它的动作不可能还活着。
    窗口内的重入会在上一个用例里被 409 拦下——两者是同一个判定的两面。
    """
    world = await _seed(async_session, run_status=RunStatus.ROLLED_BACK)
    # 造一个「上一次回滚开始于窗口之外」的现场：状态推到 rolling_back + 一条旧事件。
    # 不改事件的已写字段（账本是只追加的），而是**新写**一条时间戳在过去的入口事件。
    run = await _reload(async_session, world.run_id)
    run.status = RunStatus.ROLLING_BACK
    await _LEDGER.append(
        async_session,
        LedgerEvent(
            tenant_id=world.tenant_id,
            project_id=world.project_id,
            actor_id=world.operator.id,
            event_type=EvidenceEventType.ROLLBACK_STARTED,
            subject_type="run",
            subject_id=str(world.run_id),
            payload={"run_id": world.run_id, "attempt": 1},
            occurred_at=NOW - timedelta(seconds=settings.CAPABILITY_TTL_SEC + 60),
        ),
    )
    await async_session.commit()

    again = await publish_service.request_rollback(
        async_session, subject=world.ctx(), run_id=world.run_id, idempotency_key="idem-rb-6"
    )

    assert again.attempt == 2
    assert again.status is RunStatus.ROLLING_BACK
    assert world.calls == []  # 仍然只是受理


async def test_rollback_requires_operator_role(async_session: AsyncSession) -> None:
    """角色**此刻**从 ``role_assignment`` 重查：权限被回收的人不能继续回滚。"""
    world = await _seed(async_session, run_status=RunStatus.FAILED)
    with pytest.raises(RoleRequiredError):
        await publish_service.request_rollback(
            async_session,
            subject=world.ctx(world.outsider),
            run_id=world.run_id,
            idempotency_key="idem-rb-7",
        )
    assert (await _reload(async_session, world.run_id)).status is RunStatus.FAILED
    assert world.calls == []


async def test_rollback_refuses_a_published_run(async_session: AsyncSession) -> None:
    """已发布成功 → 409。**已发布数据的回退需要重新审批**，不是「删掉影子表」。"""
    world = await _seed(async_session)
    await publish_service.publish_run(async_session, tenant_id=world.tenant_id, run_id=world.run_id)

    with pytest.raises(RunConflictError) as err:
        await publish_service.request_rollback(
            async_session, subject=world.ctx(), run_id=world.run_id, idempotency_key="idem-rb-8"
        )
    assert err.value.details["reason"] == "already_published"
    assert world.calls.count(f"rollback:{SHADOW}") == 0


async def test_rollback_requires_idempotency_key(async_session: AsyncSession) -> None:
    world = await _seed(async_session, run_status=RunStatus.FAILED)
    with pytest.raises(AppError) as err:
        await publish_service.request_rollback(
            async_session, subject=world.ctx(), run_id=world.run_id, idempotency_key=""
        )
    assert err.value.code is ErrorCode.VALIDATION_ERROR


async def test_rollback_scope_check_rejects_a_forged_shadow_name(
    async_session: AsyncSession,
) -> None:
    """**回滚范围四连的核心**：影子表名必须等于 ``shadow_table_name(formal, version_no)`` 的重算值。

    这里把影子表名改成正式表名（最直接的误删姿势），断言拒绝、且目标端一次都没被调用。
    拒绝的代价是残留一张影子表；放行的代价是删掉正式表。
    """
    world = await _seed(async_session, run_status=RunStatus.FAILED)
    run = await _reload(async_session, world.run_id)
    run.shadow_table = FORMAL  # 伪造：影子表 == 正式表
    await async_session.commit()

    with pytest.raises(ValidationError) as err:
        await publish_service.request_rollback(
            async_session, subject=world.ctx(), run_id=world.run_id, idempotency_key="idem-rb-9"
        )
    assert err.value.details["reason"] == "shadow_equals_formal"
    assert world.calls == []
    assert FORMAL in _TARGET  # 正式表还在


async def test_rollback_scope_check_rejects_a_shadow_name_off_spec(
    async_session: AsyncSession,
) -> None:
    """后缀不符重算值也拒（比「命中命名规范」更强：中间被改过的名字照样拦下）。"""
    world = await _seed(async_session, run_status=RunStatus.FAILED)
    run = await _reload(async_session, world.run_id)
    run.shadow_table = "orders_wide__shadow_v9"
    await async_session.commit()

    with pytest.raises(ValidationError) as err:
        await publish_service.request_rollback(
            async_session, subject=world.ctx(), run_id=world.run_id, idempotency_key="idem-rb-10"
        )
    assert err.value.details["reason"] == "shadow_table_name_mismatch"
    assert world.calls == []


async def test_rollback_scope_check_rejects_a_qualified_table_name(
    async_session: AsyncSession,
) -> None:
    """带库前缀的表名一律拒：库名在连接上，接受前缀等于允许跨库清理。

    前缀加在**两张表**上：这样影子表名与重算值仍然相等（``shadow_table_name`` 不剥前缀），
    校验会走到最后那一条——否则先撞上「与重算值不符」，
    这条用例就变成在重复上一条用例，跨库那一格永远没人验。
    """
    world = await _seed(async_session, run_status=RunStatus.FAILED)
    run = await _reload(async_session, world.run_id)
    run.shadow_table = f"dw.{SHADOW}"
    run.formal_table = f"dw.{FORMAL}"
    await async_session.commit()

    with pytest.raises(ValidationError) as err:
        await publish_service.request_rollback(
            async_session, subject=world.ctx(), run_id=world.run_id, idempotency_key="idem-rb-11"
        )
    assert err.value.details["reason"] == "qualified_table_rejected"
    assert world.calls == []
    assert SHADOW in _TARGET


async def test_rollback_failure_keeps_rolling_back(async_session: AsyncSession) -> None:
    """回滚失败：**保持 ``rolling_back``**，不回退到 ``failed``。

    回退会让这条 run 看起来「可以重新点一次回滚」，而它可能已经被部分清理过。
    """
    world = await _seed(async_session, run_status=RunStatus.FAILED)
    await publish_service.request_rollback(
        async_session, subject=world.ctx(), run_id=world.run_id, idempotency_key="idem-rb-12"
    )
    message = await async_session.scalar(
        select(OutboxMessage).where(OutboxMessage.topic == "etl.target.rollback")
    )
    token = dict(message.payload)["capability"]
    _CONTROL["rollback_mode"] = "lie_dropped"  # 工具报成功但表还在

    dto = await publish_service.execute_rollback(
        async_session, tenant_id=world.tenant_id, run_id=world.run_id, capability=token
    )

    assert dto.status is RunStatus.ROLLING_BACK
    assert "重试" in dto.message
    assert SHADOW in _TARGET  # 我们自己去问过目标端，没有被「报告说删了」骗过
    failures = await _events(
        async_session, tenant_id=world.tenant_id, event_type=EvidenceEventType.RUN_FAILED
    )
    assert dict(failures[-1].payload)["reason"] == "shadow_table_still_exists"
    assert dict(failures[-1].payload)["kind"] == "rollback"


async def test_rollback_failure_without_capability_is_recorded(async_session: AsyncSession) -> None:
    """命令里没有令牌 → 记失败并保持 rolling_back，而不是静默什么都不做。"""
    world = await _seed(async_session, run_status=RunStatus.FAILED)
    await publish_service.request_rollback(
        async_session, subject=world.ctx(), run_id=world.run_id, idempotency_key="idem-rb-13"
    )

    dto = await publish_service.execute_rollback(
        async_session, tenant_id=world.tenant_id, run_id=world.run_id, capability=None
    )

    assert dto.status is RunStatus.ROLLING_BACK
    assert world.calls == []
    failures = await _events(
        async_session, tenant_id=world.tenant_id, event_type=EvidenceEventType.RUN_FAILED
    )
    assert dict(failures[-1].payload)["reason"] == "missing_capability"


async def test_rollback_execution_is_idempotent_after_success(async_session: AsyncSession) -> None:
    """同一枚回滚命令重投（Celery 至少一次投递的常态）：幂等返回，**不重复清理**。

    注意这条短路发生在**协议校验之前**（状态已是 ``rolled_back``）：
    这既避免了重放拒绝噪声，也保证了「第二次不会再去动目标端」——
    对已经清过的影子表再执行一次清理，语义上无害，但它会让账本里出现
    一次并不存在的清理动作。
    """
    world = await _seed(async_session, run_status=RunStatus.FAILED)
    await publish_service.request_rollback(
        async_session, subject=world.ctx(), run_id=world.run_id, idempotency_key="idem-rb-14"
    )
    message = await async_session.scalar(
        select(OutboxMessage).where(OutboxMessage.topic == "etl.target.rollback")
    )
    token = dict(message.payload)["capability"]
    await publish_service.execute_rollback(
        async_session, tenant_id=world.tenant_id, run_id=world.run_id, capability=token
    )
    # 已回滚，重投的令牌走幂等短路（不执行、不报协议错）
    again = await publish_service.execute_rollback(
        async_session, tenant_id=world.tenant_id, run_id=world.run_id, capability=token
    )
    assert again.already_rolled_back is True
    assert world.calls.count(f"rollback:{SHADOW}") == 1


# ---------------------------------------------------------------- 读接口


async def test_publish_state_is_not_attempted_before_any_publish(
    async_session: AsyncSession,
) -> None:
    """「没试过」与「试了但不知道」必须是两个答案：前者不需要人工核对。"""
    world = await _seed(async_session)
    dto = await publish_service.get_publish_state(
        async_session, tenant_id=world.tenant_id, run_id=world.run_id
    )
    assert dto.publish_state == "not_attempted"
    assert dto.final_state is None
    assert dto.rollback.state == "not_attempted"
    assert dto.shadow_table == SHADOW and dto.formal_table == FORMAL


async def test_publish_state_reports_published_and_rollback_progress(
    async_session: AsyncSession,
) -> None:
    world = await _seed(async_session)
    await publish_service.publish_run(async_session, tenant_id=world.tenant_id, run_id=world.run_id)

    dto = await publish_service.get_publish_state(
        async_session, tenant_id=world.tenant_id, run_id=world.run_id
    )
    assert dto.publish_state == "published"
    assert dto.final_state == "published"
    assert dto.pre_fingerprint and dto.post_fingerprint
    assert dto.fingerprint_scope == FINGERPRINT_SCOPE
    assert dto.swap_at is not None
    assert dto.rollback.state == "not_attempted"


async def test_publish_state_reports_in_progress_when_token_consumed_without_verdict(
    async_session: AsyncSession,
) -> None:
    """令牌被消费、无结论、run 还在 running → ``in_progress``；run 已终态 → ``unknown``。"""
    world = await _seed(async_session)
    async_session.add(
        Capability(
            jti=uuid.UUID("00000000-0000-0000-0000-0000000000a2"),
            preparation_id=world.preparation_id,
            subject_id=world.operator.id,
            tool=ToolName.TARGET_PUBLISH_SWAP.value,
            environment=Env.DEV,
            artifact_sha256=ARTIFACT,
            token_sha256="e" * 64,
            signature_alg="EdDSA",
            key_id="test",
            issued_at=NOW.replace(tzinfo=None),
            expires_at=(NOW + timedelta(minutes=5)).replace(tzinfo=None),
            consumed_at=NOW.replace(tzinfo=None),
        )
    )
    await async_session.commit()

    dto = await publish_service.get_publish_state(
        async_session, tenant_id=world.tenant_id, run_id=world.run_id
    )
    assert dto.publish_state == "in_progress"
    assert dto.detail["reason"] == "attempt_without_verdict"

    run = await _reload(async_session, world.run_id)
    run.status = RunStatus.INTERRUPTED
    await async_session.commit()
    dto = await publish_service.get_publish_state(
        async_session, tenant_id=world.tenant_id, run_id=world.run_id
    )
    assert dto.publish_state == "unknown"


async def test_publish_state_shows_rollback_in_progress_then_succeeded(
    async_session: AsyncSession,
) -> None:
    world = await _seed(async_session, run_status=RunStatus.FAILED)
    await publish_service.request_rollback(
        async_session, subject=world.ctx(), run_id=world.run_id, idempotency_key="idem-rb-15"
    )
    dto = await publish_service.get_publish_state(
        async_session, tenant_id=world.tenant_id, run_id=world.run_id
    )
    assert dto.rollback.state == "in_progress"
    assert dto.rollback.attempt == 1

    message = await async_session.scalar(
        select(OutboxMessage).where(OutboxMessage.topic == "etl.target.rollback")
    )
    await publish_service.execute_rollback(
        async_session,
        tenant_id=world.tenant_id,
        run_id=world.run_id,
        capability=dict(message.payload)["capability"],
    )
    dto = await publish_service.get_publish_state(
        async_session, tenant_id=world.tenant_id, run_id=world.run_id
    )
    assert dto.rollback.state == "succeeded"
    assert dto.rollback.shadow_dropped is True
    assert dto.rollback.formal_unchanged is True
    assert dto.publish_state == "not_attempted"


async def test_publish_and_rollback_are_tenant_scoped(async_session: AsyncSession) -> None:
    """跨租户一律 404：能区分「不存在」与「别人的」，就等于给了后者一个信息。"""
    world = await _seed(async_session)
    with pytest.raises(AppError) as err:
        await publish_service.get_publish_state(
            async_session, tenant_id=world.tenant_id + 1, run_id=world.run_id
        )
    assert err.value.code is ErrorCode.NOT_FOUND
    with pytest.raises(AppError):
        await publish_service.publish_run(
            async_session, tenant_id=world.tenant_id + 1, run_id=world.run_id
        )
    assert world.calls == []


def test_rollback_request_body_carries_no_table_name() -> None:
    """**契约**：回滚请求体里只有一个理由。表名能来自请求体，就等于让客户端选删哪张表。"""
    assert set(RollbackRequest.model_fields) == {"reason"}
    with pytest.raises(ValidationErr):
        RollbackRequest.model_validate({"shadow_table": "orders_wide"})
