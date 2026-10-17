"""项目、成员与职责槽路由（be-02 / fe-08）。

只做参数校验、依赖注入、转发 service、组装响应信封。

**路径与 DTO 以 ``frontend/src/api/project.ts`` 为准**——前端已按 fe-08 建成，
后端改名会让界面静默读不到值。与 be-02 §3.1 原设计的差异见
``vibecoding_doc/specs/M1-01-be-认证租户与职责槽.md`` §4.0。

⚠️ **尚未接线**：写接口的 ``Idempotency-Key`` 支持（CLAUDE.md §6）由
``api/idempotency.py`` 的中间件提供，已在 ``main.create_app`` 挂载。
"""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps_auth import get_current_subject
from core.db import get_session
from core.response import ApiResponse, ok
from schemas.auth import SubjectContext
from schemas.project import (
    MemberAddRequest,
    MemberRead,
    ProjectCreate,
    ProjectRead,
    ProjectUpdate,
    ReasonOnlyRequest,
    RoleAssignmentRead,
    RoleGrantRequest,
    UserCandidate,
)
from services import project as project_service

router = APIRouter(prefix="/projects", tags=["projects"])


# ---------------------------------------------------------------- 项目


@router.get("", response_model=ApiResponse[list[ProjectRead]], summary="我的项目")
async def list_projects(
    ctx: SubjectContext = Depends(get_current_subject),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[list[ProjectRead]]:
    return ok(await project_service.list_projects(session, ctx))


@router.post("", response_model=ApiResponse[ProjectRead], summary="新建项目")
async def create_project(
    payload: ProjectCreate,
    ctx: SubjectContext = Depends(get_current_subject),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[ProjectRead]:
    return ok(await project_service.create_project(session, ctx, payload), message="项目已创建")


@router.get("/{project_id}", response_model=ApiResponse[ProjectRead], summary="项目详情")
async def get_project(
    project_id: int,
    ctx: SubjectContext = Depends(get_current_subject),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[ProjectRead]:
    return ok(await project_service.get_project(session, ctx, project_id))


@router.put("/{project_id}", response_model=ApiResponse[ProjectRead], summary="修改项目")
async def update_project(
    project_id: int,
    payload: ProjectUpdate,
    ctx: SubjectContext = Depends(get_current_subject),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[ProjectRead]:
    """仅 ``name`` / ``description``。``env`` / ``code`` 不可改——见 ``ProjectUpdate`` 的说明。"""
    return ok(await project_service.update_project(session, ctx, project_id, payload))


# ---------------------------------------------------------------- 成员


@router.get("/{project_id}/members", response_model=ApiResponse[list[MemberRead]], summary="成员列表")
async def list_members(
    project_id: int,
    ctx: SubjectContext = Depends(get_current_subject),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[list[MemberRead]]:
    return ok(await project_service.list_members(session, ctx, project_id))


@router.post("/{project_id}/members", response_model=ApiResponse[MemberRead], summary="添加成员")
async def add_member(
    project_id: int,
    payload: MemberAddRequest,
    ctx: SubjectContext = Depends(get_current_subject),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[MemberRead]:
    return ok(await project_service.add_member(session, ctx, project_id, payload), message="成员已加入")


@router.delete("/{project_id}/members/{member_id}", response_model=ApiResponse[None], summary="移除成员")
async def remove_member(
    project_id: int,
    member_id: int,
    payload: ReasonOnlyRequest,
    ctx: SubjectContext = Depends(get_current_subject),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[None]:
    await project_service.remove_member(session, ctx, project_id, member_id, payload.reason)
    return ok(None, message="成员已移除")


@router.get(
    "/{project_id}/member-candidates",
    response_model=ApiResponse[list[UserCandidate]],
    summary="候选成员",
)
async def list_candidates(
    project_id: int,
    ctx: SubjectContext = Depends(get_current_subject),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[list[UserCandidate]]:
    return ok(await project_service.list_candidates(session, ctx, project_id))


# ---------------------------------------------------------------- 职责槽


@router.get(
    "/{project_id}/role-assignments",
    response_model=ApiResponse[list[RoleAssignmentRead]],
    summary="职责槽列表",
)
async def list_role_assignments(
    project_id: int,
    ctx: SubjectContext = Depends(get_current_subject),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[list[RoleAssignmentRead]]:
    return ok(await project_service.list_role_assignments(session, ctx, project_id))


@router.post(
    "/{project_id}/role-assignments",
    response_model=ApiResponse[RoleAssignmentRead],
    summary="授予职责槽",
)
async def grant_role(
    project_id: int,
    payload: RoleGrantRequest,
    ctx: SubjectContext = Depends(get_current_subject),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[RoleAssignmentRead]:
    return ok(await project_service.grant_role(session, ctx, project_id, payload), message="已授予")


@router.delete(
    "/{project_id}/role-assignments/{assignment_id}",
    response_model=ApiResponse[None],
    summary="回收职责槽",
)
async def revoke_role(
    project_id: int,
    assignment_id: int,
    payload: ReasonOnlyRequest = Body(...),
    ctx: SubjectContext = Depends(get_current_subject),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[None]:
    await project_service.revoke_role(session, ctx, project_id, assignment_id, payload.reason)
    return ok(None, message="已回收")
