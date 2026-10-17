"""be-02 的安全红线拦截率证明。

CLAUDE.md §10 要求「P0/P1 拦截率 = 100%、未授权/重放成功率 = 0」由自动化测试证明，
本文件就是这条要求的落点：每个用例对应一条红线，且**断言的是「被拒绝」而非「没报错」**。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import fakeredis.aioredis
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.errors import (
    AccountLockedError,
    BadCredentialsError,
    ConflictError,
    DutyConflictError,
    NotFoundError,
    RoleRequiredError,
    UnauthenticatedError,
)
from core.security import hash_password
from models.enums import EvidenceEventType, MemberStatus, ProjectStatus, RoleName, UserStatus
from models.evidence import EvidenceEvent
from models.tenant import AppUser, Project, ProjectMember, RoleAssignment, Tenant
from schemas.auth import LoginRequest, SubjectContext
from schemas.project import MemberAddRequest, ProjectCreate, ProjectUpdate, RoleGrantRequest
from services import auth as auth_service
from services import project as project_service


@pytest.fixture
def redis() -> fakeredis.aioredis.FakeRedis:
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


async def _seed_tenant(session: AsyncSession, code: str = "acme") -> Tenant:
    tenant = Tenant(code=code, name=f"{code} 公司", status="active")
    session.add(tenant)
    await session.flush()
    return tenant


async def _seed_user(
    session: AsyncSession, tenant: Tenant, username: str, password: str = "correct-horse"
) -> AppUser:
    user = AppUser(
        tenant_id=tenant.id,
        username=username,
        display_name=username.title(),
        email=f"{username}@{tenant.code}.test",
        password_hash=hash_password(password),
        status=UserStatus.ACTIVE,
    )
    session.add(user)
    await session.flush()
    return user


def _ctx(user: AppUser, tenant: Tenant) -> SubjectContext:
    return SubjectContext(
        user_id=user.id,
        tenant_id=tenant.id,
        username=user.username,
        display_name=user.display_name,
        request_id="req_test",
    )


async def _events(session: AsyncSession, event_type: EvidenceEventType) -> list[EvidenceEvent]:
    return list(
        (await session.execute(select(EvidenceEvent).where(EvidenceEvent.event_type == event_type)))
        .scalars()
        .all()
    )


# ================================================================ 认证


async def test_登录成功并签发令牌对(async_session: AsyncSession, redis) -> None:
    tenant = await _seed_tenant(async_session)
    await _seed_user(async_session, tenant, "alice")
    await async_session.commit()

    pair = await auth_service.login(
        async_session, redis, LoginRequest(username="alice", password="correct-horse", tenant_code="acme")
    )
    assert pair.access_token and pair.refresh_token
    assert pair.token_type == "Bearer"
    assert pair.user.username == "alice"
    # 登录成功必须落账：只记失败会让「谁在什么时候进来的」事后无法回溯
    assert len(await _events(async_session, EvidenceEventType.AUTH_LOGIN_SUCCEEDED)) == 1


async def test_口令错误与用户不存在返回同一错误(async_session: AsyncSession, redis) -> None:
    """区分这两者等于提供了一个用户名枚举接口。"""
    tenant = await _seed_tenant(async_session)
    await _seed_user(async_session, tenant, "alice")
    await async_session.commit()

    with pytest.raises(BadCredentialsError):
        await auth_service.login(
            async_session, redis, LoginRequest(username="alice", password="wrong", tenant_code="acme")
        )
    with pytest.raises(BadCredentialsError):
        await auth_service.login(
            async_session, redis, LoginRequest(username="ghost", password="wrong", tenant_code="acme")
        )


async def test_连续失败触发锁定(async_session: AsyncSession, redis) -> None:
    tenant = await _seed_tenant(async_session)
    await _seed_user(async_session, tenant, "alice")
    await async_session.commit()

    for _ in range(5):
        with pytest.raises(BadCredentialsError):
            await auth_service.login(
                async_session, redis, LoginRequest(username="alice", password="wrong", tenant_code="acme")
            )

    # 即使口令正确也被拒——锁定发生在口令校验之前，否则锁定期内的响应差异会变成口令预言机
    with pytest.raises(AccountLockedError):
        await auth_service.login(
            async_session, redis, LoginRequest(username="alice", password="correct-horse", tenant_code="acme")
        )


async def test_锁定不跨租户(async_session: AsyncSession, redis) -> None:
    """🔴 回归用例：锁定键曾无租户维度。

    ``app_user`` 唯一性是 ``UK(tenant_id, username)``。键里少了租户，攻击者在**自己的**
    租户内对 ``admin`` 连续失败，就能锁死其它所有租户的 ``admin``——
    一次不需要任何越权的跨租户拒绝服务。
    """
    tenant_a = await _seed_tenant(async_session, "aaa")
    tenant_b = await _seed_tenant(async_session, "bbb")
    await _seed_user(async_session, tenant_a, "admin")
    await _seed_user(async_session, tenant_b, "admin")
    await async_session.commit()

    for _ in range(5):
        with pytest.raises(BadCredentialsError):
            await auth_service.login(
                async_session, redis, LoginRequest(username="admin", password="wrong", tenant_code="aaa")
            )

    with pytest.raises(AccountLockedError):
        await auth_service.login(
            async_session, redis, LoginRequest(username="admin", password="correct-horse", tenant_code="aaa")
        )

    # 另一个租户的同名账号必须不受影响
    pair = await auth_service.login(
        async_session, redis, LoginRequest(username="admin", password="correct-horse", tenant_code="bbb")
    )
    assert pair.user.tenant_id == tenant_b.id


async def test_登出全部会话后重新登录仍可用(async_session: AsyncSession, redis) -> None:
    """🔴 回归用例：令牌里的会话代次。

    ``logout-all`` 把 Redis 里的代次 +1。若签发时仍写死 ``ver=0``，
    用户重新登录拿到的令牌当场就被自己的登出判为失效——账号被永久锁死。
    """
    tenant = await _seed_tenant(async_session)
    user = await _seed_user(async_session, tenant, "alice")
    await async_session.commit()

    first = await auth_service.login(
        async_session, redis, LoginRequest(username="alice", password="correct-horse", tenant_code="acme")
    )
    await auth_service.logout_all(async_session, redis, user.id)

    # 登出前签发的 refresh 不能再换出可用的 access
    with pytest.raises(UnauthenticatedError):
        await auth_service.refresh(async_session, redis, first.refresh_token)

    # 但重新登录必须能拿到**有效**的新令牌
    second = await auth_service.login(
        async_session, redis, LoginRequest(username="alice", password="correct-horse", tenant_code="acme")
    )
    from core.security import decode_token

    assert decode_token(second.access_token, expected_type="access")["ver"] == 1


async def test_改口令后旧刷新令牌失效(async_session: AsyncSession, redis) -> None:
    tenant = await _seed_tenant(async_session)
    user = await _seed_user(async_session, tenant, "alice")
    await async_session.commit()

    pair = await auth_service.login(
        async_session, redis, LoginRequest(username="alice", password="correct-horse", tenant_code="acme")
    )
    await auth_service.change_password(async_session, redis, user.id, "correct-horse", "new-password-1")

    with pytest.raises(UnauthenticatedError):
        await auth_service.refresh(async_session, redis, pair.refresh_token)


# ================================================================ 项目与成员


async def _bootstrap(session: AsyncSession) -> tuple[Tenant, AppUser, SubjectContext, int]:
    """建租户 + 用户 + 项目（创建者自动成为 maker）。"""
    tenant = await _seed_tenant(session)
    user = await _seed_user(session, tenant, "alice")
    await session.commit()
    ctx = _ctx(user, tenant)
    project = await project_service.create_project(
        session, ctx, ProjectCreate(code="orders", name="订单", env="dev")
    )
    return tenant, user, ctx, project.id


async def test_建项目时创建者自动成为maker(async_session: AsyncSession) -> None:
    """本系统没有 admin 角色；不自动授 maker 则第一个项目永远建不出来（死锁）。"""
    _, user, _, project_id = await _bootstrap(async_session)
    roles = await project_service.active_roles(async_session, project_id=project_id, user_id=user.id)
    assert roles == {RoleName.MAKER}
    assert len(await _events(async_session, EvidenceEventType.PROJECT_CREATED)) == 1


async def test_非成员看不到项目(async_session: AsyncSession) -> None:
    tenant, _, _, project_id = await _bootstrap(async_session)
    outsider = await _seed_user(async_session, tenant, "mallory")
    await async_session.commit()

    # 回 404 而非 403：403 等于确认这个 id 真实存在，可用来枚举别租户的项目
    with pytest.raises(NotFoundError):
        await project_service.get_project(async_session, _ctx(outsider, tenant), project_id)


async def test_跨租户访问被拒(async_session: AsyncSession) -> None:
    """🔴 租户隔离：所有查询都必须带 tenant_id 过滤。"""
    _, _, _, project_id = await _bootstrap(async_session)
    other_tenant = await _seed_tenant(async_session, "other")
    intruder = await _seed_user(async_session, other_tenant, "alice")  # 同名不同租户
    await async_session.commit()

    with pytest.raises(NotFoundError):
        await project_service.get_project(async_session, _ctx(intruder, other_tenant), project_id)


async def test_非checker不能授予职责槽(async_session: AsyncSession) -> None:
    """能力位在服务端闭合，不依赖前端置灰。"""
    _, _, ctx, project_id = await _bootstrap(async_session)
    tenant = (await async_session.execute(select(Tenant))).scalars().first()
    bob = await _seed_user(async_session, tenant, "bob")
    await async_session.commit()

    # ctx 自己只是 maker，不是 checker_security
    with pytest.raises(RoleRequiredError):
        await project_service.grant_role(
            async_session,
            ctx,
            project_id,
            RoleGrantRequest(user_id=bob.id, role=RoleName.CHECKER_DATA, reason="试试"),
        )


async def test_职责混用被拒且落账(async_session: AsyncSession) -> None:
    """maker 与 checker_* 互斥；被拒绝的授权尝试恰恰是审计里最有价值的记录。"""
    tenant, creator, ctx, project_id = await _bootstrap(async_session)
    bob = await _seed_user(async_session, tenant, "bob")
    await async_session.commit()

    # 先把 creator 提为 checker_security 以便他能发角色（会与 maker 冲突？不会——先移除 maker 语义上更干净，
    # 但本用例要的正是「一个人拿了 maker 之后不能再拿 checker」，所以换个方式：
    # 让 creator 保持 maker，另建一个 checker 来发角色是更真实的路径；这里直接用仓库级别的授权行注入。
    async_session.add(
        RoleAssignment(
            project_id=project_id,
            member_id=(
                await async_session.execute(
                    select(ProjectMember.id).where(
                        ProjectMember.project_id == project_id, ProjectMember.user_id == creator.id
                    )
                )
            ).scalar_one(),
            user_id=creator.id,
            role=RoleName.CHECKER_SECURITY,
            granted_by=creator.id,
            reason="测试用直接注入",
        )
    )
    await async_session.flush()

    # bob 先成为 maker
    await project_service.add_member(
        async_session, ctx, project_id, MemberAddRequest(user_id=bob.id, reason="需要提需求")
    )
    await project_service.grant_role(
        async_session, ctx, project_id, RoleGrantRequest(user_id=bob.id, role=RoleName.MAKER, reason="提需求")
    )

    # 再想给他 checker_data → 必须拒
    with pytest.raises(DutyConflictError):
        await project_service.grant_role(
            async_session,
            ctx,
            project_id,
            RoleGrantRequest(user_id=bob.id, role=RoleName.CHECKER_DATA, reason="兼一下"),
        )

    assert len(await _events(async_session, EvidenceEventType.DUTY_CONFLICT_DENIED)) == 1


async def test_移除成员连带撤销职责槽(async_session: AsyncSession) -> None:
    """只改 status 不动职责槽是危险的：任何一处忘记按 status 过滤的查询都会把
    已移除的人仍算作有效审批人。物理删授权行让「已移除的人没有角色」成为结构性事实。"""
    tenant, _, ctx, project_id = await _bootstrap(async_session)
    bob = await _seed_user(async_session, tenant, "bob")
    await async_session.commit()

    member = await project_service.add_member(
        async_session, ctx, project_id, MemberAddRequest(user_id=bob.id, reason="加入")
    )
    # maker 自己就能发 maker 吗？不能——发角色要 checker_security。直接注入授权行。
    async_session.add(
        RoleAssignment(
            project_id=project_id,
            member_id=member.id,
            user_id=bob.id,
            role=RoleName.OPERATOR,
            granted_by=ctx.user_id,
            reason="测试用直接注入",
        )
    )
    await async_session.flush()
    assert await project_service.active_roles(async_session, project_id=project_id, user_id=bob.id)

    await project_service.remove_member(async_session, ctx, project_id, member.id, "离职")

    assert await project_service.active_roles(async_session, project_id=project_id, user_id=bob.id) == set()
    remaining = (
        await async_session.execute(
            select(func.count()).select_from(RoleAssignment).where(RoleAssignment.user_id == bob.id)
        )
    ).scalar_one()
    assert remaining == 0
    assert len(await _events(async_session, EvidenceEventType.MEMBER_STATUS_CHANGED)) == 1


async def test_不能移除自己(async_session: AsyncSession) -> None:
    """否则项目可能被最后一个有权限的人自己搞成无人可管。"""
    _, _, ctx, project_id = await _bootstrap(async_session)
    members = await project_service.list_members(async_session, ctx, project_id)
    with pytest.raises(Exception, match="不能移除自己"):
        await project_service.remove_member(async_session, ctx, project_id, members[0].id, "手滑")


async def test_env不可被修改(async_session: AsyncSession) -> None:
    """``env`` 参与 PDP 风险判定；允许改等于允许把 prod 流水线降级成 dev 绕过闸门。"""
    _, _, ctx, project_id = await _bootstrap(async_session)
    # ProjectUpdate 根本没有 env 字段——传进去也会被忽略
    updated = await project_service.update_project(
        async_session, ctx, project_id, ProjectUpdate(name="订单 v2", description="改个名")
    )
    assert updated.name == "订单 v2"
    assert updated.env.value == "dev"  # 未被改动

    project = (await async_session.execute(select(Project).where(Project.id == project_id))).scalar_one()
    assert project.status is ProjectStatus.ACTIVE
    assert len(await _events(async_session, EvidenceEventType.PROJECT_UPDATED)) == 1


async def test_重复项目标识被拒(async_session: AsyncSession) -> None:
    _, _, ctx, _ = await _bootstrap(async_session)
    with pytest.raises(ConflictError):
        await project_service.create_project(
            async_session, ctx, ProjectCreate(code="orders", name="重复", env="dev")
        )


async def test_临时授权到期后失效(async_session: AsyncSession) -> None:
    """时窗语义只有 ``policy.is_assignment_valid`` 一份实现，故断言走服务层。"""
    tenant, _, ctx, project_id = await _bootstrap(async_session)
    bob = await _seed_user(async_session, tenant, "bob")
    await async_session.commit()
    member = await project_service.add_member(
        async_session, ctx, project_id, MemberAddRequest(user_id=bob.id, reason="临时支援")
    )

    past = datetime(2026, 1, 1, tzinfo=UTC)
    async_session.add(
        RoleAssignment(
            project_id=project_id,
            member_id=member.id,
            user_id=bob.id,
            role=RoleName.AUDITOR,
            granted_by=ctx.user_id,
            valid_from=past - timedelta(days=1),
            valid_to=past + timedelta(hours=1),  # 早已过期
            reason="测试用直接注入",
        )
    )
    await async_session.flush()

    assert await project_service.active_roles(async_session, project_id=project_id, user_id=bob.id) == set()
    # 但在过去那个时刻是有效的
    assert await project_service.active_roles(
        async_session, project_id=project_id, user_id=bob.id, at=past
    ) == {RoleName.AUDITOR}


async def test_每次变更都进证据账本(async_session: AsyncSession) -> None:
    """账本必须覆盖全部权限变更，否则审计查不出「谁给的权」。"""
    tenant, _, ctx, project_id = await _bootstrap(async_session)
    bob = await _seed_user(async_session, tenant, "bob")
    await async_session.commit()

    await project_service.add_member(
        async_session, ctx, project_id, MemberAddRequest(user_id=bob.id, reason="加入")
    )
    await project_service.update_project(async_session, ctx, project_id, ProjectUpdate(name="新名字"))

    total = (await async_session.execute(select(func.count()).select_from(EvidenceEvent))).scalar_one()
    # PROJECT_CREATED + MEMBER_ADDED + PROJECT_UPDATED
    assert total == 3

    from harness.ledger import EvidenceLedger

    assert (await EvidenceLedger().verify_chain(async_session)).ok


async def test_账本拒绝含敏感信息的payload(async_session: AsyncSession) -> None:
    """权限变更的理由若被误填成连接串，必须在入账前炸掉，而不是静默写进永久保留的账本。"""
    _, _, ctx, _ = await _bootstrap(async_session)
    tenant = (await async_session.execute(select(Tenant))).scalars().first()
    bob = await _seed_user(async_session, tenant, "bob")
    await async_session.commit()

    from harness.ledger import SensitivePayloadError

    with pytest.raises(SensitivePayloadError):
        await project_service.add_member(
            async_session,
            ctx,
            (await async_session.execute(select(Project))).scalars().first().id,
            MemberAddRequest(user_id=bob.id, reason="密码是 postgresql://u:p@h/db"),
        )


async def test_成员状态枚举落库准确(async_session: AsyncSession) -> None:
    tenant, _, ctx, project_id = await _bootstrap(async_session)
    bob = await _seed_user(async_session, tenant, "bob")
    await async_session.commit()
    await project_service.add_member(
        async_session, ctx, project_id, MemberAddRequest(user_id=bob.id, reason="加入")
    )
    members = await project_service.list_members(async_session, ctx, project_id)
    by_name = {m.username: m for m in members}
    assert by_name["bob"].status is MemberStatus.ACTIVE
    assert by_name["bob"].roles == []
    assert by_name["alice"].roles == [RoleName.MAKER]
