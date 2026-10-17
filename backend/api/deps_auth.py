"""认证与项目上下文的依赖注入（be-02）。

**本模块是「一个请求算一次主体上下文」的唯一入口。** 它只做校验 + 注入 + 转发，
不含业务逻辑（CLAUDE.md §4 铁律 1）。

关于「信任令牌里的角色」——**不信任**。access 令牌里确实带了 ``tenant_id`` 与
``projects``，但那只是**签发时刻的快照**，用于少查一次库的提示。真正的判定每次都回库：
用户被移出租户、职责槽被回收之后，旧令牌在有效期内仍然合法，信任快照等于让撤权延后生效。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import structlog
from fastapi import Depends, Request
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.db import get_session
from core.errors import ForbiddenError, RoleRequiredError, UnauthenticatedError
from core.redis import KEY_TOKEN_DENY, get_redis
from core.security import decode_token
from models.enums import RoleName, UserStatus
from models.tenant import AppUser, Tenant
from schemas.auth import SubjectContext

logger = structlog.get_logger(__name__)


def _bearer(request: Request) -> str:
    """从 ``Authorization`` 头取出令牌。"""
    raw = request.headers.get("Authorization") or ""
    scheme, _, token = raw.partition(" ")
    # 大小写不敏感：按 RFC 7235，scheme 本身是大小写无关的
    if scheme.lower() != "bearer" or not token:
        raise UnauthenticatedError("缺少或格式错误的 Authorization 头")
    return token


async def get_current_subject(
    request: Request,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
) -> SubjectContext:
    """解析 access 令牌 → 回库确认 → 构造 ``SubjectContext``。"""
    token = _bearer(request)
    claims = decode_token(token, expected_type="access")

    jti = str(claims["jti"])
    if await redis.exists(KEY_TOKEN_DENY.format(jti=jti)):
        raise UnauthenticatedError("令牌已撤销")

    user_id = int(claims["sub"])
    user = (await session.execute(select(AppUser).where(AppUser.id == user_id))).scalar_one_or_none()
    if user is None or user.status is not UserStatus.ACTIVE:
        raise UnauthenticatedError("账号不可用")

    # 会话版本：logout-all 与改口令都把版本 +1，于是所有在途令牌（不管签发于何时）立刻失效。
    # 逐个拉黑 jti 做不到这一点——系统并不保存「该用户当前有哪些未过期令牌」这张表。
    current_version = int(await redis.get(f"auth:token_ver:{user_id}") or 0)
    if int(claims.get("ver", 0)) != current_version:
        raise UnauthenticatedError("会话已失效，请重新登录")

    tenant = (await session.execute(select(Tenant).where(Tenant.id == user.tenant_id))).scalar_one_or_none()
    if tenant is None or tenant.status != "active":
        raise UnauthenticatedError("租户不可用")

    # 令牌里的 tenant_id 与库中不一致 → 用户已被迁移，旧令牌不该再有权限
    if int(claims.get("tenant_id", 0)) != user.tenant_id:
        logger.warning("tenant_mismatch_denied", user_id=user_id, token_tenant=claims.get("tenant_id"))
        raise UnauthenticatedError("令牌与账号所属租户不符")

    return SubjectContext(
        user_id=user.id,
        tenant_id=user.tenant_id,
        username=user.username,
        display_name=user.display_name or user.username,
        request_id=getattr(request.state, "request_id", ""),
    )


def require_role(*roles: RoleName) -> Callable[..., Awaitable[SubjectContext]]:
    """要求主体在**至少一个**项目里持有给定职责槽之一。

    用于「跨项目的入口」（如建项目、看租户级审计）。项目内的判定一律走
    ``services.project.active_roles``，不要用本函数——它不知道当前是哪个项目。
    """
    from services import project as project_service

    async def _dep(
        ctx: SubjectContext = Depends(get_current_subject),
        session: AsyncSession = Depends(get_session),
    ) -> SubjectContext:
        for project_id in await _member_project_ids(session, ctx):
            held = await project_service.active_roles(session, project_id=project_id, user_id=ctx.user_id)
            if held & set(roles):
                return ctx
        raise RoleRequiredError(f"需要以下职责槽之一：{', '.join(r.value for r in roles)}")

    return _dep


async def _member_project_ids(session: AsyncSession, ctx: SubjectContext) -> list[int]:
    from models.enums import MemberStatus
    from models.tenant import ProjectMember

    return list(
        (
            await session.execute(
                select(ProjectMember.project_id).where(
                    ProjectMember.user_id == ctx.user_id,
                    ProjectMember.status == MemberStatus.ACTIVE,
                )
            )
        )
        .scalars()
        .all()
    )


__all__ = ["ForbiddenError", "get_current_subject", "require_role"]
