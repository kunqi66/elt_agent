"""``Idempotency-Key`` 支持（CLAUDE.md §6）。

**为什么做成中间件而不是每条路由的依赖**：需求是「所有可能重放的写接口都要支持」，
做成依赖就得改十几条路由的签名，而漏掉一条的后果是**静默的**——
那条接口重复提交会真的执行两次，且没有任何迹象。中间件一处实现、自动全覆盖。

**原子性靠数据库唯一约束，不是「先查后写」**：``UNIQUE(tenant_id, scope, key)``
是唯一的裁决者。先查再插在并发下必然双写（两个请求同时查到「不存在」），
而这类事故只在高并发时出现，测试环境永远复现不了。
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import structlog
from fastapi import Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from core.config import settings
from core.db import SessionFactory
from core.errors import ErrorCode
from core.ids import canonical_json, new_request_id, sha256_hex, utcnow
from core.security import decode_token
from models.support import IdempotencyKey

logger = structlog.get_logger(__name__)

IDEMPOTENCY_HEADER = "Idempotency-Key"

# 只有会改状态的动词需要幂等。GET/HEAD 天然幂等，给它们记账纯属浪费一次写库。
_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# 流式响应无法在中间件里安全地缓冲后重放，本 API 全是 JSON，这里显式声明前提。
_BUFFERED_MEDIA = "application/json"


def _claims(request: Request) -> tuple[int, int] | None:
    """从令牌取 ``(user_id, tenant_id)``；取不到就返回 ``None``。

    取不到**不报错**：未认证的写请求应当由路由层回 401（那里能给出正确的错误码），
    中间件在这里抢先报错只会把认证失败的语义弄乱。
    """
    raw = request.headers.get("Authorization") or ""
    _, _, token = raw.partition(" ")
    if not token:
        return None
    try:
        claims = decode_token(token, expected_type="access")
    except Exception:
        return None
    try:
        return int(claims["sub"]), int(claims.get("tenant_id", 0))
    except (KeyError, TypeError, ValueError):
        return None


class IdempotencyMiddleware(BaseHTTPMiddleware):
    """按 ``(tenant_id, scope, key)`` 去重写请求。

    ``scope`` 取 ``方法 + 路径``，于是同一个 key 用在两个不同接口上互不干扰——
    客户端可能全局复用一个 UUID 生成器，这不该被判成冲突。
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        key = request.headers.get(IDEMPOTENCY_HEADER)
        if request.method not in _MUTATING_METHODS or not key:
            return await call_next(request)

        identity = _claims(request)
        if identity is None:
            return await call_next(request)
        user_id, tenant_id = identity

        # 读 body 会让下游收不到数据（BaseHTTPMiddleware 的已知陷阱），
        # 必须把它重新灌回 receive，否则处理器拿到空 body。
        body = await request.body()

        async def _replay_body() -> dict[str, Any]:
            return {"type": "http.request", "body": body, "more_body": False}

        request._receive = _replay_body  # type: ignore[attr-defined]

        scope = f"{request.method} {request.url.path}"
        fingerprint = sha256_hex(
            canonical_json(
                {
                    "method": request.method,
                    "path": request.url.path,
                    "user": user_id,
                    "body": body.decode("utf-8", "replace"),
                }
            ).encode("utf-8")
        )

        async with SessionFactory() as session:
            reserved = await self._reserve(session, tenant_id, scope, key, fingerprint)
            if not reserved:
                return await self._replay_or_reject(session, tenant_id, scope, key, fingerprint)

        try:
            response = await call_next(request)
        except Exception:
            # 处理器抛异常（500）：占位必须释放，否则这个 key 被永久卡在「处理中」
            await self._release(tenant_id, scope, key)
            raise

        # 只有成功响应才「粘住」。失败时**释放占位**而不是记一个失败结果——
        # 记失败会让客户端永远拿同一个 key 重试却始终看到第一次的错误；
        # 不释放（只留 completed=False 的占位）更糟：那个 key 会一直被判为「处理中」，
        # 客户端改用同一个 key 重试永远得到 409，等于把一次 400 升级成永久失败。
        if 200 <= response.status_code < 300 and _is_json(response):
            return await self._complete_and_return(response, tenant_id=tenant_id, scope=scope, key=key)

        await self._release(tenant_id, scope, key)
        return response

    async def _release(self, tenant_id: int, scope: str, key: str) -> None:
        """删除自己刚占下的、尚未完成的行，让该 key 可被重新使用。"""
        async with SessionFactory() as session:
            row = (
                await session.execute(
                    select(IdempotencyKey).where(
                        IdempotencyKey.tenant_id == tenant_id,
                        IdempotencyKey.scope == scope,
                        IdempotencyKey.key == key,
                        IdempotencyKey.completed.is_(False),
                    )
                )
            ).scalar_one_or_none()
            if row is not None:
                await session.delete(row)
                await session.commit()

    # ---------------- 内部 ----------------

    async def _reserve(self, session: Any, tenant_id: int, scope: str, key: str, fingerprint: str) -> bool:
        """原子占位。返回 ``True`` 表示本次是首次执行。"""
        session.add(
            IdempotencyKey(
                tenant_id=tenant_id,
                scope=scope,
                key=key,
                request_fingerprint=fingerprint,
                response_snapshot={},
                completed=False,
                expires_at=utcnow() + timedelta(hours=settings.IDEMPOTENCY_TTL_HOURS),
            )
        )
        try:
            await session.commit()
            return True
        except IntegrityError:
            # 唯一约束挡下 → 并发或重放。靠异常而不是先查，才不会有 TOCTOU 窗口。
            await session.rollback()
            return False

    async def _replay_or_reject(
        self, session: Any, tenant_id: int, scope: str, key: str, fingerprint: str
    ) -> Response:
        row = (
            await session.execute(
                select(IdempotencyKey).where(
                    IdempotencyKey.tenant_id == tenant_id,
                    IdempotencyKey.scope == scope,
                    IdempotencyKey.key == key,
                )
            )
        ).scalar_one_or_none()

        if row is None:
            # 约束冲突却没查到行：只可能是并发事务尚未提交。让客户端重试，
            # 比替它猜一个结果安全——猜错就是一次重复执行。
            return _error(ErrorCode.IDEMPOTENCY_CONFLICT, "同一请求正在处理中，请稍后重试")

        if row.request_fingerprint != fingerprint:
            # 同 key 不同内容：客户端复用了 key。按约定这必须拒绝，
            # 否则第二次请求的内容会被第一次的响应悄悄吞掉。
            return _error(ErrorCode.IDEMPOTENCY_CONFLICT, "Idempotency-Key 已用于不同的请求内容")

        if not row.completed:
            return _error(ErrorCode.IDEMPOTENCY_CONFLICT, "同一请求正在处理中，请稍后重试")

        body = row.response_snapshot or {}
        return JSONResponse(content=body.get("body", {}), status_code=int(body.get("status_code", 200)))

    async def _complete_and_return(
        self, response: Response, *, tenant_id: int, scope: str, key: str
    ) -> Response:
        raw = b"".join([chunk async for chunk in response.body_iterator])  # type: ignore[attr-defined]
        try:
            import json

            parsed = json.loads(raw.decode("utf-8"))
        except Exception:
            parsed = None

        if parsed is not None:
            async with SessionFactory() as session:
                row = (
                    await session.execute(
                        select(IdempotencyKey).where(
                            IdempotencyKey.tenant_id == tenant_id,
                            IdempotencyKey.scope == scope,
                            IdempotencyKey.key == key,
                        )
                    )
                ).scalar_one_or_none()
                if row is not None:
                    row.response_snapshot = {"status_code": response.status_code, "body": parsed}
                    row.completed = True
                    await session.commit()

        headers = {k: v for k, v in response.headers.items() if k.lower() != "content-length"}
        return Response(
            content=raw,
            status_code=response.status_code,
            headers=headers,
            media_type=response.media_type,
        )


def _is_json(response: Response) -> bool:
    return _BUFFERED_MEDIA in (response.headers.get("content-type") or "")


def _error(code: ErrorCode, message: str) -> JSONResponse:
    """与 ``core.errors`` 的处理器保持同一信封形状。

    这里不抛 ``AppError`` 是因为它会被异常处理器接管，而中间件拿不到
    「已认证主体」的完整上下文去构造 request_id 一致的信封。
    """
    return JSONResponse(
        status_code=409,
        content={"code": code.value, "message": message, "request_id": new_request_id(), "details": {}},
    )
