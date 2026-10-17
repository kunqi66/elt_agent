"""be-07 service 侧：策略载入、覆盖合并、预演与入账。

本文件要证的是**「规则从哪来」这件事的语义**，引擎的判定正确性已在
`test_policy_engine.py` 里穷举过。三件事在这里必须成立：

1. `policy_rule` 里 `enabled=False` 的覆盖行**必须被载入**（它表达的是「关掉」，
   在 SQL 层过滤掉它会让内置基线重新生效，用户会发现规则关不掉）；
2. 一条存坏的条件只降级为警告，不能让整个策略集不可用；
3. 被拒的越权覆盖要进证据账本。
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.errors import RoleRequiredError
from engines.policy import (
    ResourceRef,
    ResourceScope,
    ToolIntent,
    build_features,
    decide,
)
from models.enums import (
    DataSourceType,
    Env,
    EvidenceEventType,
    RiskLevel,
    SensitiveLevel,
)
from models.evidence import EvidenceEvent
from models.governance import PolicyRule
from models.tenant import AppUser, Tenant
from schemas.auth import SubjectContext
from schemas.policy import PolicyPreviewRequest, PolicyPreviewScope
from schemas.project import ProjectCreate
from services import policy as policy_service
from services import project as project_service

# 内置基线里的一条 P1：写入且没有回滚方案
P1_CODE = "p1.no_rollback_plan"
P0_CODE = "p0.cross_tenant"


async def _bootstrap(session: AsyncSession) -> tuple[Tenant, SubjectContext, int]:
    tenant = Tenant(code="acme", name="Acme", status="active")
    session.add(tenant)
    await session.flush()
    user = AppUser(
        tenant_id=tenant.id,
        username="alice",
        display_name="Alice",
        email="alice@acme.test",
        password_hash="x",
        status="active",
    )
    session.add(user)
    await session.flush()
    ctx = SubjectContext(
        user_id=user.id,
        tenant_id=tenant.id,
        username=user.username,
        display_name=user.display_name,
        request_id="req_1",
    )
    project = await project_service.create_project(
        session, ctx, ProjectCreate(code="orders", name="订单", env="dev")
    )
    return tenant, ctx, project.id


def _rule(code: str, level: RiskLevel, condition: dict[str, Any], **over: Any) -> PolicyRule:
    fields: dict[str, Any] = {
        "code": code,
        "version": 1,
        "level": level,
        "title": f"覆盖 {code}",
        "description": "",
        "condition": condition,
        "required_slots": [],
        "enabled": True,
        "updated_by": None,
    }
    fields.update(over)
    return PolicyRule(**fields)


def _intent(**over: Any) -> ToolIntent:
    base: dict[str, Any] = {"kind": "etl_write", "is_write": True, "has_rollback_plan": True}
    base.update(over)
    return ToolIntent(**base)


def _scope(project_id: int, tenant_id: int, **over: Any) -> Any:
    ref = ResourceRef(
        data_source_type=DataSourceType.MYSQL, database="shop", table="orders", env=Env.PROD
    )
    base: dict[str, Any] = {
        "tenant_id": tenant_id,
        "project_id": project_id,
        "writes": (ref,),
        "writes_formal_table": True,
    }
    base.update(over)
    return base


# ---------------------------------------------------------------- 载入


async def test_没有覆盖行时等于内置基线(async_session: AsyncSession) -> None:
    tenant, _, _ = await _bootstrap(async_session)
    active = await policy_service.load_active_policy(async_session, tenant_id=tenant.id)
    assert active.version == 0  # 未审批过任何策略集 → 内置基线
    assert active.warnings == ()
    assert any(rule.code == P1_CODE for rule in active.rules)


async def test_覆盖行替换同码基线规则(async_session: AsyncSession) -> None:
    tenant, _, _ = await _bootstrap(async_session)
    async_session.add(
        _rule(P1_CODE, RiskLevel.P2, {"env": ["prod"]}, title="放宽后的生产写入")
    )
    await async_session.flush()

    active = await policy_service.load_active_policy(async_session, tenant_id=tenant.id)
    row = next(rule for rule in active.rules if rule.code == P1_CODE)
    assert row.title == "放宽后的生产写入"
    assert row.level is RiskLevel.P2


async def test_关闭的覆盖行必须被载入否则关不掉(async_session: AsyncSession) -> None:
    """``enabled=False`` 的覆盖行若在 SQL 层被过滤，内置基线会重新生效。"""
    tenant, _, _ = await _bootstrap(async_session)
    async_session.add(_rule(P1_CODE, RiskLevel.P1, {}, enabled=False))
    await async_session.flush()

    active = await policy_service.load_active_policy(async_session, tenant_id=tenant.id)
    row = next(rule for rule in active.rules if rule.code == P1_CODE)
    assert row.enabled is False


async def test_降级基线被拒并产生警告(async_session: AsyncSession) -> None:
    tenant, _, _ = await _bootstrap(async_session)
    async_session.add(_rule(P0_CODE, RiskLevel.P3, {}))
    await async_session.flush()

    active = await policy_service.load_active_policy(async_session, tenant_id=tenant.id)
    row = next(rule for rule in active.rules if rule.code == P0_CODE)
    assert row.level is RiskLevel.P0  # 仍是 P0
    assert [w.code for w in active.warnings] == [P0_CODE]


async def test_存坏的条件只降级为警告(async_session: AsyncSession) -> None:
    """一条坏行不该让所有人的判定都不可用——配置错误不能升级成全站故障。"""
    tenant, _, _ = await _bootstrap(async_session)
    async_session.add(_rule("p1.broken", RiskLevel.P1, {"feature": "nope.nope", "op": "eq", "value": 1}))
    await async_session.flush()

    active = await policy_service.load_active_policy(async_session, tenant_id=tenant.id)
    assert any(w.code == "p1.broken" for w in active.warnings)
    assert all(rule.code != "p1.broken" for rule in active.rules)


async def test_扁平条件在载入时被编译成DSL(async_session: AsyncSession) -> None:
    tenant, _, _ = await _bootstrap(async_session)
    async_session.add(_rule("p1.only_prod", RiskLevel.P1, {"env": ["prod"]}))
    await async_session.flush()

    active = await policy_service.load_active_policy(async_session, tenant_id=tenant.id)
    row = next(rule for rule in active.rules if rule.code == "p1.only_prod")
    assert row.condition == {"feature": "env", "op": "in", "value": ["prod"]}


# ---------------------------------------------------------------- 判定入口


async def test_decide_for_project带出版本号与已编译规则(async_session: AsyncSession) -> None:
    tenant, ctx, project_id = await _bootstrap(async_session)
    async_session.add(_rule("p1.only_prod", RiskLevel.P1, {"env": ["prod"]}))
    await async_session.flush()

    decision = await policy_service.decide_for_project(
        async_session,
        tenant_id=tenant.id,
        project_id=project_id,
        actor_id=ctx.user_id,
        intent=_intent(),
        scope=ResourceScope(**_scope(project_id, tenant.id)),
        env=Env.PROD,
        level=SensitiveLevel.INTERNAL,
    )
    assert "p1.only_prod" in {hit.code for hit in decision.hits}
    assert decision.policy_version == 0


async def test_被拒的降级覆盖要入账(async_session: AsyncSession) -> None:
    """有人试图关掉/降级一条 P0，这次尝试本身必须留痕。"""
    tenant, ctx, project_id = await _bootstrap(async_session)
    async_session.add(_rule(P0_CODE, RiskLevel.P3, {}))
    await async_session.flush()

    await policy_service.decide_for_project(
        async_session,
        tenant_id=tenant.id,
        project_id=project_id,
        actor_id=ctx.user_id,
        intent=_intent(),
        scope=ResourceScope(**_scope(project_id, tenant.id)),
        env=Env.PROD,
        level=SensitiveLevel.INTERNAL,
    )
    events = (
        (
            await async_session.execute(
                select(EvidenceEvent).where(
                    EvidenceEvent.event_type == EvidenceEventType.POLICY_EVALUATED
                )
            )
        )
        .scalars()
        .all()
    )
    assert any(P0_CODE in str(event.payload) for event in events)


# ---------------------------------------------------------------- 预演


async def test_预演不落库不入账(async_session: AsyncSession) -> None:
    _, ctx, project_id = await _bootstrap(async_session)
    await async_session.commit()
    # 建项目本身会入账（`project.created`），所以基线不是 0 条；
    # 预演要证的是「不新增」——按增量判，不按绝对值判
    before = (await async_session.execute(select(EvidenceEvent))).scalars().all()

    request = PolicyPreviewRequest(
        project_id=project_id,
        env=Env.PROD,
        level=SensitiveLevel.INTERNAL,
        intent=_intent(),
        scope=PolicyPreviewScope(
            writes=[
                ResourceRef(
                    data_source_type=DataSourceType.MYSQL,
                    database="shop",
                    table="orders",
                    env=Env.PROD,
                )
            ],
            writes_formal_table=True,
        ),
    )
    scope = request.scope.to_engine(tenant_id=ctx.tenant_id, project_id=project_id)
    decision = await policy_service.preview(
        async_session,
        ctx,
        project_id=project_id,
        intent=request.intent,
        scope=scope,
        env=request.env,
        level=request.level,
    )
    # 生产环境写正式表本身就是 P0（`p0.prod_formal_write`），会直接拦下——
    # 这里要的是「判定确实发生了」，顺带印证那条基线规则仍然生效
    assert decision.blocked is True
    assert decision.level is RiskLevel.P0
    after = (await async_session.execute(select(EvidenceEvent))).scalars().all()
    assert len(after) == len(before)
    assert all(e.event_type is not EvidenceEventType.POLICY_EVALUATED for e in after)


async def test_预演用登录态回填租户而不是请求体(async_session: AsyncSession) -> None:
    """`PolicyPreviewScope` 里根本没有 tenant_id：线上形态不可能声明别人的租户。"""
    tenant, ctx, project_id = await _bootstrap(async_session)
    request = PolicyPreviewRequest(
        project_id=project_id,
        env=Env.DEV,
        intent=_intent(),
        scope=PolicyPreviewScope(),
    )
    scope = request.scope.to_engine(tenant_id=ctx.tenant_id, project_id=project_id)
    assert scope.tenant_id == tenant.id
    assert scope.project_id == project_id
    assert "tenant_id" not in PolicyPreviewRequest.model_fields["scope"].annotation.model_fields


async def test_预演需要本项目职责槽(async_session: AsyncSession) -> None:
    tenant, _, project_id = await _bootstrap(async_session)
    outsider = AppUser(
        tenant_id=tenant.id,
        username="mallory",
        display_name="Mallory",
        email="m@acme.test",
        password_hash="x",
        status="active",
    )
    async_session.add(outsider)
    await async_session.flush()
    other = SubjectContext(
        user_id=outsider.id,
        tenant_id=tenant.id,
        username="mallory",
        display_name="Mallory",
        request_id="r",
    )
    with pytest.raises(RoleRequiredError):
        await policy_service.preview(
            async_session,
            other,
            project_id=project_id,
            intent=_intent(),
            scope=ResourceScope(**_scope(project_id, tenant.id)),
            env=Env.DEV,
            level=SensitiveLevel.INTERNAL,
        )


# ---------------------------------------------------------------- 预算默认值


def test_预算默认值来自Settings并且是Decimal() -> None:
    defaults = policy_service.budget_defaults()
    assert defaults.max_read_rows > 0
    # Decimal 而不是 float：预算比较是精确比较，float 的二进制误差
    # 会让「刚好等于上限」的作业被判成超限
    assert isinstance(defaults.max_reject_rate, Decimal)
    assert defaults.max_reject_rate == Decimal("0.05")


async def test_未申报预算时按默认上限(async_session: AsyncSession) -> None:
    """服务层注入的默认值是「上限」，未申报 = 用满上限，而不是 0。"""

    _, ctx, project_id = await _bootstrap(async_session)
    scope = ResourceScope(**_scope(project_id, ctx.tenant_id))
    features = build_features(
        _intent(), scope, Env.DEV, SensitiveLevel.INTERNAL, defaults=policy_service.budget_defaults()
    )
    assert features["budget.max_read_rows"] == str(policy_service.budget_defaults().max_read_rows)


def test_判定结果可直接作为响应模型序列化() -> None:
    """响应体复用引擎 DTO，这里验证它能被 FastAPI 正常序列化（无自定义 encoder 需求）。"""
    decision = decide(
        _intent(),
        ResourceScope(tenant_id=1, project_id=1),
        Env.PROD,
        SensitiveLevel.INTERNAL,
        now=datetime(2026, 10, 16, tzinfo=UTC),
        defaults=policy_service.budget_defaults(),
    )
    payload = decision.model_dump(mode="json")
    assert payload["level"] == decision.level.value
    assert isinstance(payload["budget"]["max_read_rows"], int)
