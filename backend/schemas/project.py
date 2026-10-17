"""项目、成员与职责槽 DTO。

与 `frontend/src/types/project.ts` **逐字对齐**（前端已按该契约实现完毕）。
本文件与 be-02 §3.1 的差异是**有意为之**：be-02 原设计把授权挂在 member 路径下、
且允许改 `env`；fe-08 改为集合资源 `role-assignments` 并禁止改 `env`。
本后端按 fe-08 实现，理由见 spec `M1-01` §4 的漂移表。
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

from models.enums import Env, MemberStatus, ProjectStatus, RoleName, SensitiveLevel  # noqa: F401


class ProjectRead(BaseModel):
    """项目详情。

    `can_manage_members` / `can_manage_roles` 由**服务端算好下发**，
    前端不拿角色自行推导——前端推导意味着「界面能点的按钮」成了授权判定的一部分，
    而授权必须在服务端闭合。
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    tenant_id: int
    code: str
    name: str
    description: str
    env: Env
    status: ProjectStatus
    member_count: int = 0
    can_manage_members: bool = False
    can_manage_roles: bool = False


class ProjectCreate(BaseModel):
    code: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    name: str = Field(min_length=1, max_length=128)
    env: Env = Env.DEV
    description: str = Field(default="", max_length=512)


class ProjectUpdate(BaseModel):
    """`env` 与 `code` **刻意不在此**：它们是 PDP 判定的输入与项目标识。

    允许改 `env` 等于允许把一条 prod 流水线「降级」成 dev 绕过风险判定；
    改 `code` 会让所有引用它的历史证据对不上。要换标记就新建项目。
    """

    name: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=512)


class MemberRead(BaseModel):
    """项目成员。`id` 是 `project_member.id`，**不是** `user_id`。"""

    id: int
    project_id: int
    user_id: int
    username: str
    display_name: str
    email: str
    status: MemberStatus
    roles: list[RoleName] = Field(default_factory=list)
    joined_at: datetime


class MemberAddRequest(BaseModel):
    user_id: int
    # 理由必填并随证据事件落账：成员变更是权限变更，没有理由的权限变更无法审计
    reason: str = Field(min_length=1, max_length=512)


class UserCandidate(BaseModel):
    """本租户内、尚未加入本项目的候选用户。

    之所以要专门一个接口：让前端「拉全租户用户再自己过滤」会把全租户的用户名与邮箱
    暴露给任何一个普通成员。
    """

    id: int
    username: str
    display_name: str
    email: str
    status: str
    already_member: bool


class RoleAssignmentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    project_id: int
    member_id: int
    user_id: int
    user_name: str
    role: RoleName
    granted_by: int | None
    granted_by_name: str
    granted_at: datetime
    valid_from: datetime | None
    valid_to: datetime | None
    reason: str | None = None


class RoleGrantRequest(BaseModel):
    user_id: int
    role: RoleName
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    reason: str = Field(min_length=1, max_length=512)

    @model_validator(mode="after")
    def _check_window(self) -> RoleGrantRequest:
        """时间窗必须带时区且上界大于下界。

        裸 datetime 会在容器(UTC)与开发机(UTC+8)上算出不同的有效期，
        且两边都自洽——所以在这里就拒掉，而不是等 `is_assignment_valid` 里拒。
        """
        for field, value in (("valid_from", self.valid_from), ("valid_to", self.valid_to)):
            if value is not None and (value.tzinfo is None or value.utcoffset() is None):
                raise ValueError(f"{field} 必须是带时区的时间")
        if self.valid_from is not None and self.valid_to is not None and self.valid_to <= self.valid_from:
            raise ValueError("valid_to 必须晚于 valid_from")
        return self


class ReasonOnlyRequest(BaseModel):
    """删除类操作的 body：只有理由，但**必填**。

    删除用 body 传参不算标准（部分代理会丢弃 DELETE 的 body），
    但这里刻意保留：移除成员与回收职责槽都是权限收回，
    没有理由的记录事后无法解释「谁在什么时候收走了谁的权」。
    """

    reason: str = Field(min_length=1, max_length=512)
