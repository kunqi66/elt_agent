"""认证路由（be-02）。

只做参数校验、依赖注入、转发 service、组装响应信封（CLAUDE.md §4 铁律 1）。
**本文件不得出现任何 SQL 或业务判断**——它们全在 ``services/auth.py``。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps_auth import get_current_subject
from core.db import get_session
from core.errors import UnauthenticatedError
from core.redis import get_redis
from core.response import ApiResponse, ok
from schemas.auth import (
    ChangePasswordRequest,
    CurrentUser,
    LoginRequest,
    RefreshRequest,
    SubjectContext,
    TenantRead,
    TokenPair,
)
from services import auth as auth_service

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=ApiResponse[TokenPair], summary="登录")
async def login(
    payload: LoginRequest,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
) -> ApiResponse[TokenPair]:
    return ok(await auth_service.login(session, redis, payload), message="登录成功")


@router.post("/refresh", response_model=ApiResponse[TokenPair], summary="刷新令牌")
async def refresh(
    payload: RefreshRequest,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
) -> ApiResponse[TokenPair]:
    return ok(await auth_service.refresh(session, redis, payload.refresh_token))


@router.post("/logout", response_model=ApiResponse[None], summary="登出当前会话")
async def logout(
    request: Request,
    payload: RefreshRequest | None = None,
    ctx: SubjectContext = Depends(get_current_subject),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
) -> ApiResponse[None]:
    """撤销当前 access（以及可选的 refresh）。

    body 可不传：前端「退出登录」只保证把本地令牌清掉，
    此时仍应让服务端把 access 拉黑——否则一个被丢弃的令牌在有效期内仍然可用。
    """
    access = (request.headers.get("Authorization") or "").partition(" ")[2]
    if not access:
        raise UnauthenticatedError("缺少 Authorization 头")
    await auth_service.logout(session, redis, access, payload.refresh_token if payload else None)
    return ok(None, message="已退出")


@router.post("/logout-all", response_model=ApiResponse[None], summary="登出全部会话")
async def logout_all(
    ctx: SubjectContext = Depends(get_current_subject),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
) -> ApiResponse[None]:
    await auth_service.logout_all(session, redis, ctx.user_id)
    return ok(None, message="已退出全部会话")


@router.get("/me", response_model=ApiResponse[CurrentUser], summary="当前用户")
async def me(
    ctx: SubjectContext = Depends(get_current_subject),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[CurrentUser]:
    return ok(await auth_service.current_user(session, user_id=ctx.user_id, tenant_id=ctx.tenant_id))


@router.get("/tenant", response_model=ApiResponse[TenantRead], summary="当前租户")
async def tenant(
    ctx: SubjectContext = Depends(get_current_subject),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[TenantRead]:
    return ok(await auth_service.current_tenant(session, tenant_id=ctx.tenant_id))


@router.post("/password", response_model=ApiResponse[None], summary="修改口令")
async def change_password(
    payload: ChangePasswordRequest,
    ctx: SubjectContext = Depends(get_current_subject),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
) -> ApiResponse[None]:
    await auth_service.change_password(
        session, redis, ctx.user_id, payload.old_password, payload.new_password
    )
    return ok(None, message="口令已修改，请重新登录")
