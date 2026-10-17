"""认证编排：登录、刷新、登出、口令变更（be-02）。

本层禁止 ``import fastapi``。Redis 只用来放**易失**的计数与撤销标记，
用户、租户、角色一律以 PG 为准（``core/redis.py`` 的职责边界）。
"""

from __future__ import annotations

import structlog
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from core.errors import (
    AccountLockedError,
    BadCredentialsError,
    ConflictError,
    UnauthenticatedError,
)
from core.ids import utcnow
from core.redis import KEY_LOGIN_FAIL, KEY_TOKEN_DENY
from core.security import (
    TokenType,
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    verify_and_update_password,
)
from harness.ledger import EvidenceLedger, LedgerEvent
from models.enums import EvidenceEventType, MemberStatus, UserStatus
from models.tenant import AppUser, Project, ProjectMember, Tenant
from schemas.auth import CurrentUser, LoginRequest, LoginUser, ProjectBrief, TenantRead, TokenPair
from services import project as project_service

_ledger = EvidenceLedger()
logger = structlog.get_logger(__name__)

# 会话版本号键：``logout-all`` 与改口令都靠它一次性作废全部在途令牌。
# 放在这里而不是 core/redis.py 的键清单里，是因为它只被本模块读写，
# 且未来若加了 be-XX 的会话管理再提升不迟。
_KEY_TOKEN_VERSION = "auth:token_ver:{user_id}"  # noqa: S105 - Redis 键模板，键名里的 ver 触发了误报


def _login_fail_key(tenant_code: str, username: str) -> str:
    """登录失败计数键。

    **必须带 ``tenant_code``**：``app_user`` 的唯一性是 ``UK(tenant_id, username)``，
    键里少了租户维度，攻击者在自己租户内对 ``admin`` 连续失败，就能锁死**所有**租户的
    ``admin``——一次不需要任何越权的跨租户拒绝服务。
    """
    return KEY_LOGIN_FAIL.format(tenant_code=tenant_code, username=username)


async def _resolve_tenant(session: AsyncSession, tenant_code: str) -> Tenant:
    """确定登录落在哪个租户。

    ``tenant_code`` 为空时退化为「唯一租户」：单租户部署下要求用户先填租户码
    纯属自找麻烦；但一旦库里有多于一个租户，就必须显式指定——
    否则同一个用户名可能在多个租户下都存在，系统替用户挑一个等于替攻击者挑一个。
    """
    if tenant_code:
        tenant = (
            await session.execute(select(Tenant).where(Tenant.code == tenant_code, Tenant.status == "active"))
        ).scalar_one_or_none()
        if tenant is None:
            # 不给「租户不存在」这种可枚举的提示
            raise BadCredentialsError()
        return tenant

    tenants = (
        (await session.execute(select(Tenant).where(Tenant.status == "active").limit(2))).scalars().all()
    )
    if len(tenants) != 1:
        raise BadCredentialsError("请提供租户标识")
    return tenants[0]


async def login(session: AsyncSession, redis: Redis, payload: LoginRequest) -> TokenPair:
    """登录：锁定检查 → 校验口令 → 计数清零 → 签发令牌 → 写审计。

    锁定检查在**校验口令之前**：先比对口令再判断是否锁定，会让攻击者在被锁期间
    仍能通过响应耗时区分「口令对但被锁」与「口令错」，等于把锁定当成了口令预言机。
    """
    tenant = await _resolve_tenant(session, payload.tenant_code)
    key = _login_fail_key(tenant.code, payload.username)

    failures = int(await redis.get(key) or 0)
    if failures >= settings.LOGIN_FAIL_MAX_ATTEMPTS:
        await _audit(
            session, tenant.id, None, EvidenceEventType.AUTH_LOGIN_FAILED, payload.username, "locked"
        )
        await session.commit()
        ttl = await redis.ttl(key)
        raise AccountLockedError(f"账号已锁定，请在 {max(ttl, 0)} 秒后重试")

    user = (
        await session.execute(
            select(AppUser).where(AppUser.tenant_id == tenant.id, AppUser.username == payload.username)
        )
    ).scalar_one_or_none()

    # 用户不存在与口令错误返回**同一个**错误：区分开就等于提供了用户名枚举接口
    ok, new_hash = (False, None)
    if user is not None:
        ok, new_hash = verify_and_update_password(payload.password, user.password_hash)

    if user is None or not ok:
        await _bump_fail(session, redis, key, tenant.id, user, payload.username)
        raise BadCredentialsError()

    if user.status is not UserStatus.ACTIVE:
        await _audit(
            session, tenant.id, user.id, EvidenceEventType.AUTH_LOGIN_FAILED, payload.username, "disabled"
        )
        await session.commit()
        raise UnauthenticatedError("账号不可用")

    if new_hash is not None:
        # argon2 参数随库升级时自动迁移，用户无感
        user.password_hash = new_hash
    user.last_login_at = utcnow()
    await redis.delete(key)

    pair = await _issue_pair(session, redis, user, tenant)
    await _audit(session, tenant.id, user.id, EvidenceEventType.AUTH_LOGIN_SUCCEEDED, payload.username, "ok")
    await session.commit()
    return pair


async def _bump_fail(
    session: AsyncSession,
    redis: Redis,
    key: str,
    tenant_id: int,
    user: AppUser | None,
    username: str,
) -> None:
    """失败计数 +1 并落审计。

    ``INCR`` 与 ``EXPIRE`` 分两步：只在**首次**失败时设 TTL，否则攻击者持续失败就能
    不断续期，把「15 分钟窗口」变成一个永不解除的锁定。
    """
    count = await redis.incr(key)
    if count == 1:
        await redis.expire(key, settings.LOGIN_FAIL_WINDOW_SEC)
    await _audit(
        session,
        tenant_id,
        user.id if user else None,
        EvidenceEventType.AUTH_LOGIN_FAILED,
        username,
        "bad_password",
    )
    await session.commit()


async def _audit(
    session: AsyncSession,
    tenant_id: int,
    actor_id: int | None,
    event_type: EvidenceEventType,
    username: str,
    outcome: str,
) -> None:
    """登录成败都落账。

    只记失败会让「成功登录」在事后无法回溯（谁在什么时候从哪里进来的）；
    只记成功则看不到暴破尝试。两件都要。
    ``username`` 是账号标识不是凭据，可以入账；口令永不入账。
    """
    await _ledger.append(
        session,
        LedgerEvent(
            tenant_id=tenant_id,
            actor_id=actor_id,
            event_type=event_type,
            subject_type="user",
            subject_id=username,
            payload={"username": username, "outcome": outcome},
        ),
    )


async def token_version(redis: Redis, user_id: int) -> int:
    """当前会话代次。``logout-all`` 与改口令都把它 +1，从而作废全部在途令牌。"""
    return int(await redis.get(_KEY_TOKEN_VERSION.format(user_id=user_id)) or 0)


async def _issue_pair(session: AsyncSession, redis: Redis, user: AppUser, tenant: Tenant) -> TokenPair:
    """签发令牌对。

    ``project_ids`` 进 access 令牌只作**提示**，真正的鉴权每次都回库查 ``role_assignment``
    （见 ``api/deps_auth`` 的说明）。

    🔴 ``ver`` 必须带**当前**代次：登出全部会话后重新登录，若新令牌仍写 ``ver=0``
    而 Redis 已是 1，用户会被自己的登出永久锁在门外。
    """
    project_ids = (
        (
            await session.execute(
                select(ProjectMember.project_id)
                .join(Project, Project.id == ProjectMember.project_id)
                .where(ProjectMember.user_id == user.id, Project.tenant_id == tenant.id)
            )
        )
        .scalars()
        .all()
    )
    ver = await token_version(redis, user.id)
    access, _ = create_access_token(
        str(user.id), tenant_id=tenant.id, project_ids=list(project_ids), token_version=ver
    )
    refresh, _ = create_refresh_token(str(user.id), token_version=ver)
    return TokenPair(
        access_token=access,
        refresh_token=refresh,
        expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        user=LoginUser.model_validate(user),
    )


async def refresh(session: AsyncSession, redis: Redis, refresh_token: str) -> TokenPair:
    """刷新。

    refresh 令牌里**不含** tenant_id 声明，所以这里必须回库取——
    让刷新路径也走一次「用户当前属于哪个租户」的实时判定，
    而不是相信一个可能已经过期的签发快照。
    """
    claims = decode_token(refresh_token, expected_type="refresh")
    jti = str(claims["jti"])
    if await redis.exists(KEY_TOKEN_DENY.format(jti=jti)):
        raise UnauthenticatedError("令牌已撤销")

    # 代次校验：少了它，``logout-all`` / 改口令之前签发的 refresh 令牌
    # 仍能换出可用的 access——登出就等于没做，用户以为驱逐了入侵者，其实对方会话一直活着。
    if int(claims.get("ver", 0)) != await token_version(redis, int(claims["sub"])):
        raise UnauthenticatedError("会话已失效，请重新登录")

    user = (
        await session.execute(select(AppUser).where(AppUser.id == int(claims["sub"])))
    ).scalar_one_or_none()
    if user is None or user.status is not UserStatus.ACTIVE:
        raise UnauthenticatedError("账号不可用")

    tenant = (await session.execute(select(Tenant).where(Tenant.id == user.tenant_id))).scalar_one()

    # 轮转：旧 refresh 立即作废（TTL 取到其自身过期时刻，标记不必永久保留）
    remaining = int(claims["exp"]) - int(utcnow().timestamp())
    if remaining > 0:
        await redis.set(KEY_TOKEN_DENY.format(jti=jti), "1", ex=remaining)

    return await _issue_pair(session, redis, user, tenant)


async def _deny(redis: Redis, token: str, *, expected_type: TokenType) -> None:
    """把令牌的 jti 拉黑，TTL 取其剩余有效期。

    TTL 取剩余有效期而非固定值：固定值要么短于令牌寿命（拉黑失效），
    要么远长于它（Redis 里堆积一堆早已过期的 jti）。
    """
    claims = decode_token(token, expected_type=expected_type)  # type: ignore[arg-type]
    remaining = int(claims["exp"]) - int(utcnow().timestamp())
    if remaining > 0:
        await redis.set(KEY_TOKEN_DENY.format(jti=str(claims["jti"])), "1", ex=remaining)


async def logout(session: AsyncSession, redis: Redis, access_token: str, refresh_token: str | None) -> None:
    await _deny(redis, access_token, expected_type="access")
    if refresh_token:
        try:
            await _deny(redis, refresh_token, expected_type="refresh")
        except Exception as exc:
            # refresh 已过期/不合法不影响登出成功——用户要的是「把我弄出去」，
            # 不能因为一个附带的坏令牌而拒绝。但仍记 debug 日志：
            # 频繁出现说明前端传错了字段，静默吞掉就永远发现不了。
            logger.debug("logout_refresh_deny_failed", error=str(exc))


async def logout_all(session: AsyncSession, redis: Redis, user_id: int) -> None:
    """让该用户**全部**会话失效。

    实现方式是把 ``token_version`` 记进 Redis 而不是逐个拉黑 jti——
    逐个拉黑需要先枚举出所有未过期令牌，而系统并不保存这张表（无状态 JWT 的代价）。
    校验侧比对令牌里的 ``ver`` 与 Redis 当前值，不一致即拒。
    """
    user = (await session.execute(select(AppUser).where(AppUser.id == user_id))).scalar_one_or_none()
    if user is None:
        raise UnauthenticatedError("账号不可用")
    await redis.incr(_KEY_TOKEN_VERSION.format(user_id=user_id))
    await _audit(session, user.tenant_id, user_id, EvidenceEventType.AUTH_LOGOUT_ALL, user.username, "ok")
    await session.commit()


async def change_password(
    session: AsyncSession, redis: Redis, user_id: int, old_password: str, new_password: str
) -> None:
    """改口令，并强制全端下线。

    改完不踢下线等于没改：旧的 refresh 令牌还能换出新 access，
    用户以为已经把入侵者赶走了，实际上对方的会话一直活着。
    """
    user = (await session.execute(select(AppUser).where(AppUser.id == user_id))).scalar_one_or_none()
    if user is None:
        raise UnauthenticatedError("账号不可用")

    ok, _ = verify_and_update_password(old_password, user.password_hash)
    if not ok:
        raise BadCredentialsError("原口令不正确")
    if old_password == new_password:
        raise ConflictError("新口令不能与原口令相同")

    user.password_hash = hash_password(new_password)
    await redis.incr(_KEY_TOKEN_VERSION.format(user_id=user_id))
    await _audit(
        session, user.tenant_id, user_id, EvidenceEventType.AUTH_PASSWORD_CHANGED, user.username, "ok"
    )
    await session.commit()


# ---------------------------------------------------------------- 读


async def current_user(session: AsyncSession, *, user_id: int, tenant_id: int) -> CurrentUser:
    """``GET /auth/me``。

    角色**实时查库**而不是读令牌里的快照：职责槽被回收后，用户下次刷新页面
    就该看到入口消失。若信任令牌快照，一个被撤权的人能靠旧令牌继续操作到令牌过期为止。
    """
    user = (
        await session.execute(select(AppUser).where(AppUser.id == user_id, AppUser.tenant_id == tenant_id))
    ).scalar_one_or_none()
    if user is None:
        # 租户不匹配也走这里：令牌里的 tenant_id 与库里不符说明用户被迁移过，
        # 旧令牌不该再有权限
        raise UnauthenticatedError("账号不可用")

    tenant = (await session.execute(select(Tenant).where(Tenant.id == tenant_id))).scalar_one()

    rows = (
        (
            await session.execute(
                select(Project)
                .join(ProjectMember, ProjectMember.project_id == Project.id)
                .where(
                    Project.tenant_id == tenant_id,
                    ProjectMember.user_id == user_id,
                    ProjectMember.status == MemberStatus.ACTIVE,
                )
                .distinct()
            )
        )
        .scalars()
        .all()
    )

    briefs: list[ProjectBrief] = []
    for project in rows:
        roles = await project_service.active_roles(session, project_id=project.id, user_id=user_id)
        if roles:
            briefs.append(
                ProjectBrief(
                    id=project.id,
                    name=project.name,
                    env=project.env.value,
                    roles=sorted(roles, key=lambda r: r.value),
                )
            )

    return CurrentUser(
        id=user.id,
        username=user.username,
        email=user.email,
        display_name=user.display_name,
        tenant_id=tenant_id,
        tenant_code=tenant.code,
        status=user.status,
        projects=briefs,
    )


async def current_tenant(session: AsyncSession, *, tenant_id: int) -> TenantRead:
    tenant = (await session.execute(select(Tenant).where(Tenant.id == tenant_id))).scalar_one_or_none()
    if tenant is None:
        raise UnauthenticatedError("租户不可用")
    return TenantRead.model_validate(tenant)
