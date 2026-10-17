"""认证相关 DTO。

字段与 `frontend/src/types/auth.ts` **逐字对齐**——前端已按该契约实现完毕，
后端改名等于让界面静默读不到值（`undefined` 不会报错，只会显示空白）。
凡与 be-02 §3.1 原文有出入处，以本文件 + 前端为准，差异见 spec `M1-01` §4。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from models.enums import RoleName, UserStatus


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    # 口令只在本模型内瞬时存在：不设 repr、不落日志（logging 层另有兜底脱敏）
    password: str = Field(min_length=1, max_length=256, repr=False)
    # 为空时表示「按默认租户处理」（单租户部署的便捷路径）
    tenant_code: str = Field(default="", max_length=64)


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=1)


class ChangePasswordRequest(BaseModel):
    old_password: str = Field(min_length=1, max_length=256, repr=False)
    new_password: str = Field(min_length=8, max_length=256, repr=False)


class LoginUser(BaseModel):
    """登录响应里的用户摘要。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    email: str
    tenant_id: int
    display_name: str


class ProjectBrief(BaseModel):
    """`/auth/me` 里「用户可访问的项目及其角色」。

    `roles` 是**实时**查出的 active 职责槽，不是登录时快照——
    角色被回收后，界面下次刷 me 就该看到它消失。
    """

    id: int
    name: str
    env: str
    roles: list[RoleName]


class CurrentUser(BaseModel):
    """GET /auth/me。前端不解析 JWT，角色与项目一律以此为准。"""

    id: int
    username: str
    email: str
    display_name: str
    tenant_id: int
    tenant_code: str
    status: UserStatus
    projects: list[ProjectBrief]


class TenantRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    code: str
    name: str
    status: str


class TokenPair(BaseModel):
    """登录与刷新的响应。

    `token_type` 是 OAuth2 惯例字段（恒为 `Bearer`）。前端类型里有它，
    若后端不发，前端 `Authorization` 头会拼成 `undefined <token>` 而全部 401。
    """

    access_token: str
    refresh_token: str
    token_type: str = "Bearer"  # noqa: S105 - OAuth2 方案名，不是口令
    expires_in: int  # 秒
    user: LoginUser


class SubjectContext(BaseModel):
    """每个业务请求的主体上下文（**不对外序列化**，仅供依赖注入内部传递）。

    `tenant_id` 取自数据库而非 JWT：JWT 里的声明是签发时刻的快照，
    用户被移到别的租户后旧令牌还在有效期内，信任它就等于认可一次越权。
    """

    model_config = ConfigDict(frozen=True)

    user_id: int
    tenant_id: int
    username: str
    display_name: str
    request_id: str
