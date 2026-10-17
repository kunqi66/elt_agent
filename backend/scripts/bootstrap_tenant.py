"""租户开张：在一个空库上建出租户、首批用户、首个项目与初始职责槽。

**为什么必须有这个脚本**：系统**没有**注册接口（``api/auth.py`` 只有 login / refresh /
logout / password），用户表也只能由管理员写。而职责槽的首个授予存在一个死锁——

    只有 ``checker_security`` 能发角色（``engines/policy/roles.can_manage_roles``），
    而第一个 ``checker_security`` 还没人持有，于是**任何 API 路径都发不出第一个槽**。

``create_project`` 已经用一个同构的办法解开了 maker 那一侧的死锁（建项者自授 maker，
见 ``services/project.create_project`` 的 docstring），但 checker_security 这一侧解不开：
自授会让同一个人同时持有 maker 与 checker_*，直接违反职责分离。

所以开张这件事只能由**运维在带外**做一次，并且留痕。本脚本就是那个带外入口，
它把「谁在什么时候把第一个安全审批槽给了谁」写进证据账本——这是全系统信任的起点，
没有它，后面所有「四眼审批」都无法回答「第一个审批人是谁任命的」。

**边界**：只在一个**还没开张**的租户上运行。租户下只要有任何一个项目已经发过职责槽，
脚本就拒绝执行并让你走 API——避免它变成一条绕过 ``can_manage_roles`` 的后门。

用法（在 ``backend/`` 下）::

    # 口令从环境变量取（推荐：不进 shell 历史）
    ELT_BOOTSTRAP_PASSWORD='...' uv run python scripts/bootstrap_tenant.py \\
        --tenant-code demo --tenant-name 演示租户 --project-code sales_wide

    # 不传口令则随机生成，只打印这一次
    uv run python scripts/bootstrap_tenant.py --tenant-code demo --project-code sales_wide

    # 先看会做什么，不写库
    uv run python scripts/bootstrap_tenant.py --dry-run ...
"""

from __future__ import annotations

import argparse
import asyncio
import os
import secrets
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from core.db import SessionFactory
from core.security import hash_password
from engines.policy import roles as role_policy
from harness.ledger import EvidenceLedger, LedgerEvent
from models.enums import (
    Env,
    EvidenceEventType,
    RoleName,
    UserStatus,
)
from models.tenant import AppUser, Project, ProjectMember, RoleAssignment, Tenant
from schemas.auth import SubjectContext
from schemas.project import MemberAddRequest, ProjectCreate, RoleGrantRequest
from services import project as project_service

_ledger = EvidenceLedger()

# 首批用户：一人一槽，**刻意不合并**。五个槽五个人是「四眼」可被验证的前提，
# 也保证任何两条审批路径都能找到不同的两个人（PRD §11 的质量标杆要求）。
_SEED_USERS: tuple[tuple[str, str, RoleName], ...] = (
    ("maker1", "需求提出人", RoleName.MAKER),
    ("checker.data1", "数据审批人", RoleName.CHECKER_DATA),
    ("checker.security1", "安全审批人", RoleName.CHECKER_SECURITY),
    ("operator1", "执行操作员", RoleName.OPERATOR),
    ("auditor1", "审计员", RoleName.AUDITOR),
)

# 首个 checker_security 的授予人不是某个用户，而是「带外开张」这件事本身。
# 记进 payload 而不是编造一个 actor_id：伪造一个「是某某批的」比留空更糟。
_BOOTSTRAP_ACTOR = "bootstrap:cli"


async def _get_or_create_tenant(session: AsyncSession, code: str, name: str) -> tuple[Tenant, bool]:
    tenant = (
        await session.execute(select(Tenant).where(Tenant.code == code))
    ).scalar_one_or_none()
    if tenant is not None:
        return tenant, False
    tenant = Tenant(code=code, name=name, status="active")
    session.add(tenant)
    await session.flush()
    return tenant, True


async def _get_or_create_user(
    session: AsyncSession, *, tenant_id: int, username: str, display_name: str, password: str
) -> tuple[AppUser, bool]:
    user = (
        await session.execute(
            select(AppUser).where(AppUser.tenant_id == tenant_id, AppUser.username == username)
        )
    ).scalar_one_or_none()
    if user is not None:
        # 已存在就**不动口令**：重置口令是另一件事（走 /auth/password 或专门的
        # 运维流程），在这里顺手改会让「重跑一次开张脚本」变成一次静默的凭据轮换。
        return user, False
    user = AppUser(
        tenant_id=tenant_id,
        username=username,
        display_name=display_name,
        email="",
        password_hash=hash_password(password),
        status=UserStatus.ACTIVE,
    )
    session.add(user)
    await session.flush()
    return user, True


async def _guard_not_provisioned(session: AsyncSession, tenant_id: int) -> None:
    """租户下已发过职责槽就拒绝执行。

    「已经开张」的判据用**职责槽**而不是项目数：项目可能建了又删，
    而职责槽一经授予就是权限事实。已有槽还允许本脚本跑，它就成了后门。
    """
    issued = (
        await session.execute(
            select(func.count())
            .select_from(RoleAssignment)
            .join(Project, Project.id == RoleAssignment.project_id)
            .where(Project.tenant_id == tenant_id)
        )
    ).scalar_one()
    if issued:
        raise SystemExit(
            f"租户 {tenant_id} 已开张（{issued} 条职责槽）。"
            "后续加人请用 API（由 checker_security 授予），本脚本只负责第一次。"
        )


async def _grant_first_security_slot(
    session: AsyncSession, *, tenant_id: int, project_id: int, user_id: int, member_id: int
) -> None:
    """带外授予首个 ``checker_security``。

    **这是全脚本唯一一处不走 service 的写入**，因为 service 那条路径正需要
    这个槽才能通行（见模块 docstring 里的死锁说明）。其余四个槽全部走
    ``services.project.grant_role``，由它做职责分离校验并落账。

    即便如此，职责分离的判定仍然调的是**同一份纯函数**
    （``engines.policy.roles.check_duty_separation``）——规则只有一处实现，
    带外不等于绕过。
    """
    role_policy.check_duty_separation(frozenset(), granting=RoleName.CHECKER_SECURITY)

    session.add(
        RoleAssignment(
            project_id=project_id,
            member_id=member_id,
            user_id=user_id,
            role=RoleName.CHECKER_SECURITY,
            granted_by=None,  # 无人类授予人：见 _BOOTSTRAP_ACTOR 的说明
            reason="租户开张：首个安全审批槽必须带外授予（服务端存在鸡生蛋死锁）",
        )
    )
    await session.flush()
    await _ledger.append(
        session,
        LedgerEvent(
            tenant_id=tenant_id,
            project_id=project_id,
            actor_id=None,
            event_type=EvidenceEventType.MEMBER_ROLE_GRANTED,
            subject_type="app_user",
            subject_id=str(user_id),
            payload={
                "role": RoleName.CHECKER_SECURITY.value,
                "out_of_band": True,
                "by": _BOOTSTRAP_ACTOR,
                # 记「哪台机器、什么时候」：这是信任链的起点，出问题时要知道是谁开的张
                "operator_host": os.environ.get("COMPUTERNAME") or os.environ.get("HOSTNAME") or "unknown",
                "at": datetime.now(UTC).isoformat(),
            },
        ),
    )


def _ctx_for(user: AppUser, tenant_id: int) -> SubjectContext:
    """把某个种子用户当成「操作者」，用于调用 service 层。

    这里的 request_id 是个固定值而不是 UUID：开张期的账本条目应当能一眼看出
    「这批操作来自同一次带外开张」，而不是伪装成五个人各自的正常操作。
    """
    return SubjectContext(
        user_id=user.id,
        tenant_id=tenant_id,
        username=user.username,
        display_name=user.display_name,
        request_id="bootstrap",
    )


async def _ensure_project(
    *, ctx: SubjectContext, code: str, name: str, env: Env
) -> tuple[int, bool]:
    """建项目（走 service，顺带把创建者授为 maker 并落账）。"""
    async with SessionFactory() as session:
        existing = (
            await session.execute(
                select(Project).where(Project.tenant_id == ctx.tenant_id, Project.code == code)
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing.id, False
        created = await project_service.create_project(
            session, ctx, ProjectCreate(code=code, name=name, env=env)
        )
        return created.id, True


async def _ensure_member(
    *, ctx: SubjectContext, project_id: int, user_id: int, reason: str
) -> tuple[int, bool]:
    async with SessionFactory() as session:
        existing = (
            await session.execute(
                select(ProjectMember).where(
                    ProjectMember.project_id == project_id, ProjectMember.user_id == user_id
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing.id, False
        member = await project_service.add_member(
            session, ctx, project_id, MemberAddRequest(user_id=user_id, reason=reason)
        )
        return member.id, True


async def _ensure_role(
    *, ctx: SubjectContext, project_id: int, user_id: int, role: RoleName
) -> bool:
    """授予职责槽（走 service，含职责分离校验 + 落账 + 跨行唯一约束兜底）。"""
    async with SessionFactory() as session:
        existing = (
            await session.execute(
                select(RoleAssignment).where(
                    RoleAssignment.project_id == project_id,
                    RoleAssignment.user_id == user_id,
                    RoleAssignment.role == role,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            return False
        await project_service.grant_role(
            session,
            ctx,
            project_id,
            RoleGrantRequest(user_id=user_id, role=role, reason="租户开张：初始职责槽"),
        )
        return True


async def bootstrap(
    *,
    tenant_code: str,
    tenant_name: str,
    project_code: str,
    project_name: str,
    env: Env,
    password: str,
    dry_run: bool,
) -> int:
    async with SessionFactory() as session:
        tenant, tenant_created = await _get_or_create_tenant(session, tenant_code, tenant_name)
        await _guard_not_provisioned(session, tenant.id)

        users: dict[str, AppUser] = {}
        created_users: list[str] = []
        for username, display_name, _role in _SEED_USERS:
            user, is_new = await _get_or_create_user(
                session,
                tenant_id=tenant.id,
                username=username,
                display_name=display_name,
                password=password,
            )
            users[username] = user
            if is_new:
                created_users.append(username)

        print(f"租户 {tenant.code}(id={tenant.id}) {'新建' if tenant_created else '已存在'}")
        print(f"用户：新建 {len(created_users)} 个，复用 {len(_SEED_USERS) - len(created_users)} 个")

        if dry_run:
            # 不提交，进程退出即回滚——dry-run 不该在库里留下任何痕迹
            await session.rollback()
            print("[dry-run] 已回滚，库里没有变化")
            return 0

        # 先落用户，后面每一步 service 调用都要有自己的事务
        await session.commit()
        tenant_id = tenant.id

    # 槽 → 人：一人一槽，下面按槽取人，不再按用户名硬编码
    by_role = {role: users[username] for username, _display, role in _SEED_USERS}

    maker_ctx = _ctx_for(by_role[RoleName.MAKER], tenant_id)
    project_id, project_created = await _ensure_project(
        ctx=maker_ctx, code=project_code, name=project_name, env=env
    )
    print(f"项目 {project_code}(id={project_id}) {'新建' if project_created else '已存在'}")

    # ① 成员：maker 有权拉人（can_manage_members）。maker 建项时已自动入组，跳过
    added = 0
    for role, user in by_role.items():
        if role is RoleName.MAKER:
            continue
        _member_id, is_new = await _ensure_member(
            ctx=maker_ctx,
            project_id=project_id,
            user_id=user.id,
            reason="租户开张：加入首个项目",
        )
        added += int(is_new)
    print(f"项目成员：新增 {added} 个")

    # ② 首个 checker_security **带外**授予（服务端死锁，见模块 docstring）
    security_user = by_role[RoleName.CHECKER_SECURITY]
    async with SessionFactory() as session:
        held = (
            await session.execute(
                select(RoleAssignment).where(
                    RoleAssignment.project_id == project_id,
                    RoleAssignment.user_id == security_user.id,
                    RoleAssignment.role == RoleName.CHECKER_SECURITY,
                )
            )
        ).scalar_one_or_none()
        security_member = (
            await session.execute(
                select(ProjectMember).where(
                    ProjectMember.project_id == project_id, ProjectMember.user_id == security_user.id
                )
            )
        ).scalar_one()
        if held is None:
            await _grant_first_security_slot(
                session,
                tenant_id=tenant_id,
                project_id=project_id,
                user_id=security_user.id,
                member_id=security_member.id,
            )
            await session.commit()
            print("首个 checker_security：带外授予并落账（actor 留空，payload 记 by/host/at）")
        else:
            print("首个 checker_security：已存在，跳过")

    # ③ 其余槽由刚上任的 checker_security 授予（can_manage_roles）
    #
    # maker 也放在这里「确保存在」而不是跳过：项目已存在时 create_project 不会被调用，
    # 那条自授路径也就不会发生。跳过它的后果是「重跑一次脚本，maker 槽反而没了」——
    # 存在性检查让这一步在正常情况下是空操作。
    security_ctx = _ctx_for(security_user, tenant_id)
    granted = 0
    for role, user in by_role.items():
        if role is RoleName.CHECKER_SECURITY:
            continue  # ② 已带外发放
        granted += int(
            await _ensure_role(ctx=security_ctx, project_id=project_id, user_id=user.id, role=role)
        )
    print(f"职责槽：补齐 {granted} 个")

    print("\n开张完成。可登录用户：" + "、".join(u for u, _d, _r in _SEED_USERS))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="租户开张（建租户/用户/项目/初始职责槽）")
    parser.add_argument("--tenant-code", required=True)
    parser.add_argument("--tenant-name", default="")
    parser.add_argument("--project-code", required=True)
    parser.add_argument("--project-name", default="")
    parser.add_argument("--env", default=Env.DEV.value, choices=[e.value for e in Env])
    parser.add_argument("--dry-run", action="store_true", help="只校验与打印，不写库")
    parser.add_argument(
        "--debug", action="store_true", help="连不上库时打印完整异常（含堆栈）"
    )
    args = parser.parse_args(argv)

    password = os.environ.get("ELT_BOOTSTRAP_PASSWORD") or secrets.token_urlsafe(16)
    generated = "ELT_BOOTSTRAP_PASSWORD" not in os.environ

    try:
        code = asyncio.run(
            bootstrap(
                tenant_code=args.tenant_code,
                tenant_name=args.tenant_name or args.tenant_code,
                project_code=args.project_code,
                project_name=args.project_name or args.project_code,
                env=Env(args.env),
                password=password,
                dry_run=args.dry_run,
            )
        )
    except SystemExit:
        raise  # 「已开张」这类拒绝带着明确的中文原因，原样抛出
    except Exception as exc:
        if args.debug:
            raise
        # 不打印 `str(exc)`：数据库驱动与 SQLAlchemy 的异常消息里可能带连接串
        # （口令通常会被打码，但「通常」不是一条可以依赖的保证），
        # 而 CLAUDE.md §6 要求数据库异常不得原样外泄。只给类型 + 目标主机端口。
        print(
            f"开张失败：{type(exc).__name__}（连接 {settings.POSTGRES_HOST}:{settings.POSTGRES_PORT}/"
            f"{settings.POSTGRES_DB}）。加 --debug 看完整异常。",
            file=sys.stderr,
        )
        return 2  # 与 scripts/verify_ledger.py 一致：0 成功、1 业务失败、2 用法或连接错误

    if generated and code == 0 and not args.dry_run:
        # 只打印一次，不落任何文件。**不进日志、不进证据账本**（CLAUDE.md §5.2）
        print(f"\n初始口令（只显示这一次，请立即分发并让各人自行修改）：{password}", file=sys.stderr)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
