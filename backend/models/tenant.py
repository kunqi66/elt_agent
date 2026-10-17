"""租户、用户、项目、成员与职责槽。

``role_assignment`` 是**职责分离的唯一事实来源**：审批时校验「审批人是否持有该槽」
与「是否为申请人」都查它，而不是查用户表上的静态角色。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from models.base import Base, PKMixin, TenantMixin, TimestampMixin
from models.checks import enum_col
from models.enums import Env, MemberStatus, ProjectStatus, RoleName, UserStatus


class Tenant(Base, PKMixin, TimestampMixin):
    __tablename__ = "tenant"

    code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")


class AppUser(Base, PKMixin, TimestampMixin, TenantMixin):
    __tablename__ = "app_user"
    __table_args__ = (
        UniqueConstraint("tenant_id", "username", name="uq_app_user_tenant_id_username"),
        Index("ix_app_user_email", "email"),
    )

    username: Mapped[str] = mapped_column(String(64), nullable=False)
    display_name: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    email: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    # argon2id 哈希；明文永不落库、永不进日志（logging 层另有兜底脱敏）
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[UserStatus] = mapped_column(
        enum_col(UserStatus, name="user_status"), nullable=False, default=UserStatus.ACTIVE
    )
    # 显式写 DateTime(timezone=True)：只写 `Mapped[datetime | None]` 时 SQLAlchemy
    # 会从注解推断出**不带时区**的 TIMESTAMP，而调用方一律传 datetime.now(UTC)，
    # asyncpg 会在参数绑定处直接拒绝（"can't subtract offset-naive and offset-aware
    # datetimes"）。SQLite 单测容忍这种错配，所以这个 bug 只在真 PG 上暴露。
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    memberships: Mapped[list[ProjectMember]] = relationship(back_populates="user", lazy="selectin")


class Project(Base, PKMixin, TimestampMixin, TenantMixin):
    __tablename__ = "project"
    __table_args__ = (UniqueConstraint("tenant_id", "code", name="uq_project_tenant_id_code"),)

    code: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    # 环境是 PDP 判定的关键输入：prod 意味着写正式表属于 P0 触发条件之一
    env: Mapped[Env] = mapped_column(enum_col(Env, name="env"), nullable=False, default=Env.DEV)
    status: Mapped[ProjectStatus] = mapped_column(
        enum_col(ProjectStatus, name="project_status"), nullable=False, default=ProjectStatus.ACTIVE
    )

    members: Mapped[list[ProjectMember]] = relationship(back_populates="project", lazy="selectin")


class ProjectMember(Base, PKMixin, TimestampMixin):
    __tablename__ = "project_member"
    __table_args__ = (UniqueConstraint("project_id", "user_id", name="uq_project_member_project_id_user_id"),)

    project_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("project.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[MemberStatus] = mapped_column(
        enum_col(MemberStatus, name="member_status"), nullable=False, default=MemberStatus.ACTIVE
    )

    project: Mapped[Project] = relationship(back_populates="members", lazy="joined")
    user: Mapped[AppUser] = relationship(back_populates="memberships", lazy="joined")
    role_assignments: Mapped[list[RoleAssignment]] = relationship(back_populates="member", lazy="selectin")


class RoleAssignment(Base, PKMixin, TimestampMixin):
    """职责槽分配。

    约束说明：``role`` 与 ``maker`` 互斥（同一项目内）属于**跨行约束**，CHECK 表达不了，
    由 ``services/project.py`` 在授予时校验，并在同事务内写审计事件。

    ``reason`` 在字典里没有、但必须有：授予与回收都是**权限变更**，
    没有理由的记录事后无法解释「谁在什么时候出于什么原因给了谁这份权」。
    它同时随证据事件落账，两处都留是为了让界面不必回查账本就能显示理由。
    """

    __tablename__ = "role_assignment"
    __table_args__ = (
        UniqueConstraint("project_id", "user_id", "role", name="uq_role_assignment_project_id_user_id_role"),
        Index("ix_role_assignment_project_id_role", "project_id", "role"),
    )

    project_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("project.id", ondelete="CASCADE"), nullable=False
    )
    member_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("project_member.id", ondelete="CASCADE"), nullable=False
    )
    # 冗余存 user_id：审批时的「自批校验」是高频路径，避免多一跳 join 也能少一个出错面
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[RoleName] = mapped_column(enum_col(RoleName, name="role_name"), nullable=False)
    granted_by: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("app_user.id", ondelete="SET NULL"), nullable=True
    )
    # 同上：职责槽的有效期一律带时区，否则「过期判定」在真 PG 上参数绑定即失败
    valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reason: Mapped[str] = mapped_column(String(512), nullable=False, default="")

    member: Mapped[ProjectMember] = relationship(back_populates="role_assignments")
