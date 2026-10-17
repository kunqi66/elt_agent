"""项目、成员与职责槽的业务编排（be-02）。

**`active_roles` 是本仓库「某人此刻在某项目持有哪些职责槽」的唯一实现。**
审批受理（be-13）、生成链路（be-07）、依赖注入（`api/deps_auth.py`）全都调它。
各写一份的症状是「登录时算出的角色集合 ≠ 依赖注入时算出的」——最难查的越权/漏权 bug。

本层禁止 ``import fastapi``；跨层只传 Pydantic 模型与 ``SubjectContext``，不传 ORM 实例。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.errors import (
    ConflictError,
    DutyConflictError,
    ForbiddenError,
    NotFoundError,
    RoleRequiredError,
)
from core.ids import as_utc, utcnow
from engines.policy import roles as policy
from harness.ledger import EvidenceLedger, LedgerEvent
from models.enums import EvidenceEventType, MemberStatus, ProjectStatus, RoleName
from models.tenant import AppUser, Project, ProjectMember, RoleAssignment
from schemas.auth import SubjectContext
from schemas.project import (
    MemberAddRequest,
    MemberRead,
    ProjectCreate,
    ProjectRead,
    ProjectUpdate,
    RoleAssignmentRead,
    RoleGrantRequest,
    UserCandidate,
)

_ledger = EvidenceLedger()


# ---------------------------------------------------------------- 职责槽查询


async def active_roles(
    session: AsyncSession, *, project_id: int, user_id: int, at: datetime | None = None
) -> set[RoleName]:
    """某人此刻在某项目持有的职责槽集合。

    时窗判定走纯函数 ``policy.is_assignment_valid``，**不翻译成 SQL 的 BETWEEN**：
    区间语义（``valid_to`` 是开区间上界）只能有一份实现，写成 SQL 表达式就变成了第二份，
    某天只改一处，于是「临时授权到 12:00」在界面显示已失效、在审批时仍被认作有效。

    因此这里把候选行取回内存再过滤。单个 (项目, 用户) 的授权行是个位数，
    代价可忽略，而它换来的是语义唯一。
    """
    now = at or utcnow()
    rows = (
        (
            await session.execute(
                select(RoleAssignment)
                .join(ProjectMember, ProjectMember.id == RoleAssignment.member_id)
                .where(
                    RoleAssignment.project_id == project_id,
                    RoleAssignment.user_id == user_id,
                    ProjectMember.status == MemberStatus.ACTIVE,
                )
            )
        )
        .scalars()
        .all()
    )

    return {
        row.role
        for row in rows
        # 过 as_utc：SQLite 读回的时窗是裸值，而纯函数只收带时区的时间。
        # 服务层是「存储怪癖 → 纯函数契约」的翻译边界，归一放在这里，
        # 而不是把纯函数的严格性放宽——放宽了它就不再是「任何机器上语义唯一」。
        if policy.is_assignment_valid(
            None if row.valid_from is None else as_utc(row.valid_from),
            None if row.valid_to is None else as_utc(row.valid_to),
            at=now,
        )
    }


async def _require_project(session: AsyncSession, ctx: SubjectContext, project_id: int) -> Project:
    """取项目并确认主体有权看到它。

    「无权」与「不存在」都回 404——回 403 等于告诉攻击者这个 id 真实存在，
    于是可以枚举出别的租户有哪些项目。
    """
    project = (
        await session.execute(
            select(Project).where(Project.id == project_id, Project.tenant_id == ctx.tenant_id)
        )
    ).scalar_one_or_none()
    if project is None:
        raise NotFoundError("项目不存在")

    roles = await active_roles(session, project_id=project_id, user_id=ctx.user_id)
    if not roles:
        raise NotFoundError("项目不存在")
    return project


async def _require_capability(
    session: AsyncSession, ctx: SubjectContext, project_id: int, *, what: str, manage_roles: bool
) -> set[RoleName]:
    """能力位拦截。真正的授权判定在这里闭合，不依赖前端置灰。"""
    roles = await active_roles(session, project_id=project_id, user_id=ctx.user_id)
    allowed = policy.can_manage_roles(roles) if manage_roles else policy.can_manage_members(roles)
    if not allowed:
        needed = "checker_security" if manage_roles else "maker 或 operator"
        raise RoleRequiredError(f"{what}需要 {needed} 职责槽")
    return roles


# ---------------------------------------------------------------- 项目


async def list_projects(session: AsyncSession, ctx: SubjectContext) -> list[ProjectRead]:
    """只返回主体持有 active 职责槽的项目。

    过滤条件放在 SQL 里而不是取回后过滤：后者会把用户不该看到的项目名读进进程内存，
    一旦某处日志把整个列表打出来就泄露了。
    """
    now = utcnow()
    rows = (
        (
            await session.execute(
                select(Project)
                .join(ProjectMember, ProjectMember.project_id == Project.id)
                .join(RoleAssignment, RoleAssignment.member_id == ProjectMember.id)
                .where(
                    Project.tenant_id == ctx.tenant_id,
                    ProjectMember.user_id == ctx.user_id,
                    ProjectMember.status == MemberStatus.ACTIVE,
                )
                .distinct()
            )
        )
        .scalars()
        .all()
    )

    out: list[ProjectRead] = []
    for project in rows:
        # 时窗过滤仍在内存里做：SQL 只能表达「有授权行」，表达不了开区间语义
        project_roles = await active_roles(session, project_id=project.id, user_id=ctx.user_id, at=now)
        if not project_roles:
            continue
        out.append(await _to_read(session, project, project_roles))
    return out


async def get_project(session: AsyncSession, ctx: SubjectContext, project_id: int) -> ProjectRead:
    project = await _require_project(session, ctx, project_id)
    roles = await active_roles(session, project_id=project_id, user_id=ctx.user_id)
    return await _to_read(session, project, roles)


async def _to_read(session: AsyncSession, project: Project, roles: set[RoleName]) -> ProjectRead:
    member_count = (
        await session.execute(
            select(func.count())
            .select_from(ProjectMember)
            .where(ProjectMember.project_id == project.id, ProjectMember.status == MemberStatus.ACTIVE)
        )
    ).scalar_one()
    return ProjectRead(
        id=project.id,
        tenant_id=project.tenant_id,
        code=project.code,
        name=project.name,
        description=project.description,
        env=project.env,
        status=project.status,
        member_count=int(member_count),
        can_manage_members=policy.can_manage_members(roles),
        can_manage_roles=policy.can_manage_roles(roles),
    )


async def create_project(session: AsyncSession, ctx: SubjectContext, payload: ProjectCreate) -> ProjectRead:
    """建项目，并把创建者自动授为 maker。

    为什么允许任意已认证用户建项目：本系统的职责槽只有
    maker/checker_data/checker_security/operator/auditor，**没有 admin**。
    若要求「持有 maker 才能建项目」，而新项目里还没有任何职责槽，就成了死锁——
    第一个项目永远建不出来。故开放建项并在建项时自授 maker；
    这是 be-02 §10 那条未决的落地答案。
    """
    exists = (
        await session.execute(
            select(Project.id).where(Project.tenant_id == ctx.tenant_id, Project.code == payload.code)
        )
    ).scalar_one_or_none()
    if exists is not None:
        raise ConflictError(f"项目标识 {payload.code} 在本租户已存在")

    project = Project(
        tenant_id=ctx.tenant_id,
        code=payload.code,
        name=payload.name,
        description=payload.description,
        env=payload.env,
        status=ProjectStatus.ACTIVE,
    )
    session.add(project)
    await session.flush()

    member = ProjectMember(project_id=project.id, user_id=ctx.user_id, status=MemberStatus.ACTIVE)
    session.add(member)
    await session.flush()

    session.add(
        RoleAssignment(
            project_id=project.id,
            member_id=member.id,
            user_id=ctx.user_id,
            role=RoleName.MAKER,
            granted_by=ctx.user_id,
            reason="项目创建者自授 maker",
        )
    )
    await _ledger.append(
        session,
        LedgerEvent(
            tenant_id=ctx.tenant_id,
            project_id=project.id,
            actor_id=ctx.user_id,
            event_type=EvidenceEventType.PROJECT_CREATED,
            subject_type="project",
            subject_id=str(project.id),
            payload={"code": project.code, "name": project.name, "env": project.env.value},
        ),
    )
    await session.commit()
    return await _to_read(session, project, {RoleName.MAKER})


async def update_project(
    session: AsyncSession, ctx: SubjectContext, project_id: int, payload: ProjectUpdate
) -> ProjectRead:
    """改项目名与描述。

    ``env`` / ``code`` 不在 ``ProjectUpdate`` 里，所以这里无从改起——
    这不是遗漏：``env`` 参与 PDP 风险判定，允许改等于允许把 prod 流水线降级成 dev
    来绕过风险闸门。要换标记就新建项目。
    """
    await _require_capability(session, ctx, project_id, what="修改项目", manage_roles=False)
    project = await _require_project(session, ctx, project_id)

    changed: dict[str, object] = {}
    if payload.name is not None and payload.name != project.name:
        changed["name"] = {"from": project.name, "to": payload.name}
        project.name = payload.name
    if payload.description is not None and payload.description != project.description:
        changed["description"] = {"from": project.description, "to": payload.description}
        project.description = payload.description

    if changed:
        await _ledger.append(
            session,
            LedgerEvent(
                tenant_id=ctx.tenant_id,
                project_id=project.id,
                actor_id=ctx.user_id,
                event_type=EvidenceEventType.PROJECT_UPDATED,
                subject_type="project",
                subject_id=str(project.id),
                payload={"changes": changed},
            ),
        )
    await session.commit()
    return await _to_read(
        session, project, await active_roles(session, project_id=project.id, user_id=ctx.user_id)
    )


# ---------------------------------------------------------------- 成员


async def list_members(session: AsyncSession, ctx: SubjectContext, project_id: int) -> list[MemberRead]:
    await _require_project(session, ctx, project_id)
    rows = (
        await session.execute(
            select(ProjectMember, AppUser)
            .join(AppUser, AppUser.id == ProjectMember.user_id)
            .where(ProjectMember.project_id == project_id)
            .order_by(ProjectMember.id)
        )
    ).all()

    out: list[MemberRead] = []
    for member, user in rows:
        out.append(
            MemberRead(
                id=member.id,
                project_id=member.project_id,
                user_id=member.user_id,
                username=user.username,
                display_name=user.display_name,
                email=user.email,
                status=member.status,
                roles=sorted(
                    await active_roles(session, project_id=project_id, user_id=member.user_id),
                    key=lambda r: r.value,
                ),
                joined_at=member.created_at,
            )
        )
    return out


async def list_candidates(session: AsyncSession, ctx: SubjectContext, project_id: int) -> list[UserCandidate]:
    """本租户内、尚未加入本项目的人。

    专门开一个接口而不是让前端拉全租户再过滤——后者会把全租户的用户名与邮箱
    交给任何一个普通成员。这里只回本租户、且只回必要字段。
    """
    await _require_capability(session, ctx, project_id, what="查看候选成员", manage_roles=False)
    member_user_ids = select(ProjectMember.user_id).where(
        ProjectMember.project_id == project_id, ProjectMember.status == MemberStatus.ACTIVE
    )
    users = (
        (
            await session.execute(
                select(AppUser)
                .where(AppUser.tenant_id == ctx.tenant_id, AppUser.id.not_in(member_user_ids))
                .order_by(AppUser.username)
                .limit(200)
            )
        )
        .scalars()
        .all()
    )
    return [
        UserCandidate(
            id=u.id,
            username=u.username,
            display_name=u.display_name,
            email=u.email,
            status=u.status.value,
            already_member=False,
        )
        for u in users
    ]


async def add_member(
    session: AsyncSession, ctx: SubjectContext, project_id: int, payload: MemberAddRequest
) -> MemberRead:
    await _require_capability(session, ctx, project_id, what="添加成员", manage_roles=False)

    user = (
        await session.execute(
            select(AppUser).where(AppUser.id == payload.user_id, AppUser.tenant_id == ctx.tenant_id)
        )
    ).scalar_one_or_none()
    if user is None:
        # 跨租户的 user_id 也走这条：不回「该用户属于别的租户」，那本身是信息泄露
        raise NotFoundError("用户不存在")

    member = (
        await session.execute(
            select(ProjectMember).where(
                ProjectMember.project_id == project_id, ProjectMember.user_id == payload.user_id
            )
        )
    ).scalar_one_or_none()

    if member is not None and member.status == MemberStatus.ACTIVE:
        raise ConflictError("该用户已是项目成员")
    if member is None:
        member = ProjectMember(project_id=project_id, user_id=payload.user_id, status=MemberStatus.ACTIVE)
        session.add(member)
        await session.flush()
    else:
        # 曾经移除过又重新加入：复用同一行（UK(project_id,user_id) 不允许插第二行），
        # 但**旧的职责槽不恢复**——重新加入不自动继承历史权限
        member.status = MemberStatus.ACTIVE
        member.created_at = utcnow()  # 界面按它显示「加入时间」，应反映本次加入

    await _ledger.append(
        session,
        LedgerEvent(
            tenant_id=ctx.tenant_id,
            project_id=project_id,
            actor_id=ctx.user_id,
            event_type=EvidenceEventType.MEMBER_ADDED,
            subject_type="member",
            subject_id=str(member.id),
            payload={"user_id": payload.user_id, "username": user.username, "reason": payload.reason},
        ),
    )
    await session.commit()
    return MemberRead(
        id=member.id,
        project_id=member.project_id,
        user_id=member.user_id,
        username=user.username,
        display_name=user.display_name,
        email=user.email,
        status=member.status,
        roles=[],
        joined_at=member.created_at,
    )


async def remove_member(
    session: AsyncSession, ctx: SubjectContext, project_id: int, member_id: int, reason: str
) -> None:
    """移除成员，并**连带撤销其全部职责槽**。

    只改 status 而不动职责槽是危险的：``active_roles`` 已经按 member.status 过滤，
    但任何一处忘记带这个过滤（比如某段只查 role_assignment 的 SQL）就会把
    已移除的人仍算作有效审批人。物理删掉授权行，让「已移除的人没有角色」成为
    不依赖查询写法的结构性事实。
    """
    await _require_capability(session, ctx, project_id, what="移除成员", manage_roles=False)

    member = (
        await session.execute(
            select(ProjectMember).where(ProjectMember.id == member_id, ProjectMember.project_id == project_id)
        )
    ).scalar_one_or_none()
    if member is None:
        raise NotFoundError("成员不存在")
    if member.user_id == ctx.user_id:
        raise ForbiddenError("不能移除自己")

    revoked = sorted(
        (await active_roles(session, project_id=project_id, user_id=member.user_id)),
        key=lambda r: r.value,
    )
    await session.execute(
        delete(RoleAssignment).where(
            RoleAssignment.project_id == project_id, RoleAssignment.user_id == member.user_id
        )
    )
    member.status = MemberStatus.REMOVED

    await _ledger.append(
        session,
        LedgerEvent(
            tenant_id=ctx.tenant_id,
            project_id=project_id,
            actor_id=ctx.user_id,
            event_type=EvidenceEventType.MEMBER_STATUS_CHANGED,
            subject_type="member",
            subject_id=str(member.id),
            payload={
                "user_id": member.user_id,
                "to": MemberStatus.REMOVED.value,
                "revoked_roles": [r.value for r in revoked],
                "reason": reason,
            },
        ),
    )
    await session.commit()


# ---------------------------------------------------------------- 职责槽


async def list_role_assignments(
    session: AsyncSession, ctx: SubjectContext, project_id: int
) -> list[RoleAssignmentRead]:
    await _require_project(session, ctx, project_id)
    rows = (
        await session.execute(
            select(RoleAssignment, AppUser)
            .join(AppUser, AppUser.id == RoleAssignment.user_id)
            .where(RoleAssignment.project_id == project_id)
            .order_by(RoleAssignment.id)
        )
    ).all()

    out: list[RoleAssignmentRead] = []
    for assignment, user in rows:
        granter_name = ""
        if assignment.granted_by is not None:
            granter = (
                await session.execute(select(AppUser.display_name).where(AppUser.id == assignment.granted_by))
            ).scalar_one_or_none()
            granter_name = granter or ""
        out.append(
            RoleAssignmentRead(
                id=assignment.id,
                project_id=assignment.project_id,
                member_id=assignment.member_id,
                user_id=assignment.user_id,
                user_name=user.display_name or user.username,
                role=assignment.role,
                granted_by=assignment.granted_by,
                granted_by_name=granter_name,
                granted_at=assignment.created_at,
                valid_from=assignment.valid_from,
                valid_to=assignment.valid_to,
                reason=assignment.reason,
            )
        )
    return out


async def grant_role(
    session: AsyncSession, ctx: SubjectContext, project_id: int, payload: RoleGrantRequest
) -> RoleAssignmentRead:
    """授予职责槽。

    两道校验缺一不可，且都在**服务端**：
    ① 职责混用（maker 与 checker_* 互斥）——``policy.check_duty_separation``；
    ② 授予人自己得有 checker_security——``_require_capability``。
    前端置灰只是体验，绕过它直接打接口时，拦截必须发生在这里。
    """
    await _require_capability(session, ctx, project_id, what="授予职责槽", manage_roles=True)

    member = (
        await session.execute(
            select(ProjectMember).where(
                ProjectMember.project_id == project_id,
                ProjectMember.user_id == payload.user_id,
                ProjectMember.status == MemberStatus.ACTIVE,
            )
        )
    ).scalar_one_or_none()
    if member is None:
        raise NotFoundError("该用户不是本项目成员，请先加入")

    held = await active_roles(session, project_id=project_id, user_id=payload.user_id)
    try:
        policy.check_duty_separation(held, granting=payload.role)
    except ValueError as exc:
        # 冲突要**落账**再抛：被拒绝的授权尝试是审计里最有价值的一类记录，
        # 只回一个 409 而不留痕，事后无法回答「谁试过给自己发 checker 槽」。
        await _ledger.append(
            session,
            LedgerEvent(
                tenant_id=ctx.tenant_id,
                project_id=project_id,
                actor_id=ctx.user_id,
                event_type=EvidenceEventType.DUTY_CONFLICT_DENIED,
                subject_type="member",
                subject_id=str(member.id),
                payload={
                    "target_user_id": payload.user_id,
                    "granting": payload.role.value,
                    "held": sorted(r.value for r in held),
                },
            ),
        )
        await session.commit()
        raise DutyConflictError(str(exc)) from exc

    existing = (
        await session.execute(
            select(RoleAssignment).where(
                RoleAssignment.project_id == project_id,
                RoleAssignment.user_id == payload.user_id,
                RoleAssignment.role == payload.role,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise ConflictError("该职责槽已授予，如需变更有效期请先回收再重新授予")

    assignment = RoleAssignment(
        project_id=project_id,
        member_id=member.id,
        user_id=payload.user_id,
        role=payload.role,
        granted_by=ctx.user_id,
        valid_from=payload.valid_from,
        valid_to=payload.valid_to,
        reason=payload.reason,
    )
    session.add(assignment)
    await session.flush()

    await _ledger.append(
        session,
        LedgerEvent(
            tenant_id=ctx.tenant_id,
            project_id=project_id,
            actor_id=ctx.user_id,
            event_type=EvidenceEventType.MEMBER_ROLE_GRANTED,
            subject_type="role_assignment",
            subject_id=str(assignment.id),
            payload={
                "target_user_id": payload.user_id,
                "role": payload.role.value,
                "valid_from": None if payload.valid_from is None else payload.valid_from.isoformat(),
                "valid_to": None if payload.valid_to is None else payload.valid_to.isoformat(),
                "reason": payload.reason,
            },
        ),
    )
    await session.commit()

    target = (await session.execute(select(AppUser).where(AppUser.id == payload.user_id))).scalar_one()
    return RoleAssignmentRead(
        id=assignment.id,
        project_id=assignment.project_id,
        member_id=assignment.member_id,
        user_id=assignment.user_id,
        user_name=target.display_name or target.username,
        role=assignment.role,
        granted_by=assignment.granted_by,
        granted_by_name=ctx.display_name,
        granted_at=assignment.created_at,
        valid_from=assignment.valid_from,
        valid_to=assignment.valid_to,
        reason=assignment.reason,
    )


async def revoke_role(
    session: AsyncSession, ctx: SubjectContext, project_id: int, assignment_id: int, reason: str
) -> None:
    """回收职责槽。

    物理删除该行（fe-08 §5）：历史由证据账本承载，表里只留当前有效的授权。
    保留 deleted 标记会让 ``active_roles`` 的每个调用点都必须记得过滤它——
    漏一处就是一次越权。
    """
    await _require_capability(session, ctx, project_id, what="回收职责槽", manage_roles=True)

    assignment = (
        await session.execute(
            select(RoleAssignment).where(
                RoleAssignment.id == assignment_id, RoleAssignment.project_id == project_id
            )
        )
    ).scalar_one_or_none()
    if assignment is None:
        raise NotFoundError("职责槽不存在")

    await session.delete(assignment)
    await _ledger.append(
        session,
        LedgerEvent(
            tenant_id=ctx.tenant_id,
            project_id=project_id,
            actor_id=ctx.user_id,
            event_type=EvidenceEventType.MEMBER_ROLE_REVOKED,
            subject_type="role_assignment",
            subject_id=str(assignment_id),
            payload={
                "target_user_id": assignment.user_id,
                "role": assignment.role.value,
                "reason": reason,
            },
        ),
    )
    await session.commit()
