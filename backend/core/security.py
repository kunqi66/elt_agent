"""认证基础件：口令哈希与 JWT 签发/校验。

注意区分两类令牌：
- **用户令牌**（本模块）：access/refresh，HS256，含 ``jti``，可撤销。
- **Capability 执行令牌**（``harness/capability.py``，M3 实现）：Ed25519，绑定工具/环境/制品指纹，
  5 分钟单次消费。二者用途与签名算法都不同，**不要混用**。
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Literal

import jwt
from pwdlib import PasswordHash

from core.config import settings
from core.errors import TokenExpiredError, UnauthenticatedError
from core.ids import new_uuid, utcnow

_password_hash = PasswordHash.recommended()  # argon2id

_ALGORITHM = "HS256"
_ISSUER = "elt-agent"
TokenType = Literal["access", "refresh"]


# ---------------- 口令 ----------------
def hash_password(plain: str) -> str:
    return _password_hash.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return _password_hash.verify(plain, hashed)
    except Exception:
        # 哈希格式损坏等情况一律视为验证失败，不向外暴露细节
        return False


def verify_and_update_password(plain: str, hashed: str) -> tuple[bool, str | None]:
    """校验口令；若原哈希参数已过时，第二个返回值是新哈希，调用方应落库替换。

    登录路径应使用本函数而非 ``verify_password``，这样 argon2 参数随库升级时能自动迁移。
    """
    try:
        return _password_hash.verify_and_update(plain, hashed)
    except Exception:
        return False, None


# ---------------- JWT ----------------
def _create_token(
    subject: str,
    token_type: TokenType,
    expires: timedelta,
    extra: dict[str, Any] | None = None,
) -> tuple[str, str]:
    """返回 (token, jti)。jti 由调用方写入 Redis 以支持登出/撤销。"""
    jti = new_uuid()
    now = utcnow()
    payload: dict[str, Any] = {
        "sub": subject,
        "type": token_type,
        "jti": jti,
        "iss": _ISSUER,
        "iat": int(now.timestamp()),
        "exp": int((now + expires).timestamp()),
    }
    if extra:
        payload.update(extra)
    token = jwt.encode(payload, settings.SECRET_KEY.get_secret_value(), algorithm=_ALGORITHM)
    return token, jti


def create_access_token(
    subject: str,
    *,
    tenant_id: int,
    project_ids: list[int] | None = None,
    token_version: int = 0,
) -> tuple[str, str]:
    return _create_token(
        subject,
        "access",
        timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES),
        {"tenant_id": tenant_id, "projects": project_ids or [], "ver": token_version},
    )


def create_refresh_token(subject: str, *, token_version: int = 0) -> tuple[str, str]:
    """``token_version`` 是会话代次。

    带它是为了让 ``logout-all`` / 改口令能一次性作废**全部**在途令牌：
    校验侧比对令牌里的 ``ver`` 与 Redis 当前值，不一致即拒。
    逐个拉黑 jti 做不到——系统不保存「该用户当前有哪些未过期令牌」这张表。

    🔴 签发时必须写入**当前**版本号（不是 0）：``logout-all`` 之后重新登录，
    若新令牌仍带 ``ver=0`` 而 Redis 已是 1，用户会被自己的登出永久锁在门外。
    """
    return _create_token(
        subject,
        "refresh",
        timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS),
        {"ver": token_version},
    )


def decode_token(token: str, *, expected_type: TokenType) -> dict[str, Any]:
    try:
        payload = jwt.decode(
            token,
            settings.SECRET_KEY.get_secret_value(),
            algorithms=[_ALGORITHM],
            issuer=_ISSUER,
            options={"require": ["exp", "sub", "jti", "type"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise TokenExpiredError() from exc
    except jwt.InvalidTokenError as exc:
        raise UnauthenticatedError("令牌无效") from exc

    if payload.get("type") != expected_type:
        raise UnauthenticatedError("令牌类型不匹配")
    return payload
