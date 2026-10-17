"""不可变制品与版本路由（be-10 / be-11 / be-12 / fe-04）。

只做参数校验、依赖注入、转发 service、组装响应信封。**所有判定都在 engines / service**：
「有 P0/P1 阻断就不许冻结」「自动修复超上限回 AUTOFIX_EXHAUSTED」都不在这里，
路由收到的要么是成功结果，要么是 ``core.errors`` 的 AppError。

**路径与 DTO 以 ``frontend/src/api/pipeline.ts`` 为准**；差异登记在
``schemas/pipeline.py`` 与 ``schemas/version.py`` 的模块 docstring 里。

**没有任何写接口能更新一个已冻结的版本**（CLAUDE.md §5.4）。``/autofix`` 与 ``/freeze``
是这条规则的唯一两种「继续走」的方式：前者产出**新的一轮修复记录**，
后者把当前制品**冻结成新版本**。两者都不会改已经冻结的那一行。

``Idempotency-Key`` 支持由 ``api/idempotency.py`` 的中间件统一提供（``main.create_app``
已挂载），本文件不需要逐条声明。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps_auth import get_current_subject
from core.db import get_session
from core.response import ApiResponse, ok
from engines.gate.types import AutofixRound, GateResult
from schemas.auth import SubjectContext
from schemas.pipeline import EtlPlanRead, HoconArtifactRead
from schemas.version import (
    AutofixRequest,
    FreezeArtifactManifest,
    FreezeRequest,
    PipelineVersionDetail,
    VersionDiff,
)
from services.pipeline import artifacts as artifact_service

router = APIRouter(prefix="/pipeline-versions", tags=["pipeline-versions"])


# ---------------------------------------------------------------- Diff
#
# ⚠️ ``/diff`` **必须**注册在 ``/{version_id}`` 之前。
# 两者路径深度相同，Starlette 按注册顺序匹配：反过来的话 ``diff`` 会先被
# ``/{version_id}`` 当成版本号吃进 int 校验，接口返回 422 而不是差异清单。
# 前端 mock 里也留着同一条注释（``api/mock/index.ts``："/pipeline-versions/diff
# 必须排在 /:versionId 之前"）——两端各踩过一次的坑。


@router.get("/diff", response_model=ApiResponse[VersionDiff], summary="版本 Diff")
async def diff_versions(
    base_version_id: int = Query(..., ge=1, description="旧版本（diff 的左端）"),
    target_version_id: int = Query(..., ge=1, description="新版本（diff 的右端）"),
    ctx: SubjectContext = Depends(get_current_subject),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[VersionDiff]:
    """方向固定为 ``base → target``，由调用方显式传入，不依赖后端默认值。

    审计场景里方向读反会得出完全相反的结论，所以两个参数都是必填。
    """
    return ok(
        await artifact_service.diff_versions(
            session=session,
            ctx=ctx,
            base_version_id=base_version_id,
            target_version_id=target_version_id,
        )
    )


# ---------------------------------------------------------------- 版本详情


@router.get("/{version_id}", response_model=ApiResponse[PipelineVersionDetail], summary="版本详情")
async def get_version(
    version_id: int,
    ctx: SubjectContext = Depends(get_current_subject),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[PipelineVersionDetail]:
    """摘要 + 冻结清单 + 两份只读制品（plan / hocon）。"""
    return ok(await artifact_service.get_version(session=session, ctx=ctx, version_id=version_id))


# ---------------------------------------------------------------- 只读制品


@router.get("/{version_id}/plan", response_model=ApiResponse[EtlPlanRead], summary="EtlPlan")
async def get_plan(
    version_id: int,
    ctx: SubjectContext = Depends(get_current_subject),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[EtlPlanRead]:
    """DAG 在 ``plan.plan.dag`` 里，不是独立接口。"""
    return ok(await artifact_service.get_plan(session=session, ctx=ctx, version_id=version_id))


@router.get("/{version_id}/hocon", response_model=ApiResponse[HoconArtifactRead], summary="HOCON 制品")
async def get_hocon(
    version_id: int,
    ctx: SubjectContext = Depends(get_current_subject),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[HoconArtifactRead]:
    return ok(await artifact_service.get_hocon(session=session, ctx=ctx, version_id=version_id))


# ---------------------------------------------------------------- 门禁


@router.get("/{version_id}/gate", response_model=ApiResponse[GateResult], summary="门禁结论")
async def get_gate(
    version_id: int,
    ctx: SubjectContext = Depends(get_current_subject),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[GateResult]:
    """结论由 ``engines/gate`` 产出，前端只渲染。

    响应模型直接复用 ``engines.gate.types.GateResult``——**不是**在 schemas 里再抄一份，
    否则字段漂移时不会有任何报错，只会出现「界面说通过、后端说阻断」的分裂。
    """
    return ok(await artifact_service.get_gate(session=session, ctx=ctx, version_id=version_id))


@router.post("/{version_id}/autofix", response_model=ApiResponse[AutofixRound], summary="触发一轮自动修复")
async def run_autofix(
    version_id: int,
    payload: AutofixRequest,
    ctx: SubjectContext = Depends(get_current_subject),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[AutofixRound]:
    """手动跑一轮自动修复，返回**这一轮**的记录（整份历史在 ``/gate`` 的 ``autofix_rounds`` 里）。

    已达上限时由 service 回 ``AUTOFIX_EXHAUSTED``（409）——那条判定不属于路由。

    ``payload.note`` 目前被 service 丢弃（``run_autofix`` 的签名没有该入参），
    见 ``schemas/version.py`` 的差异表；照收是为了前端不因多一个键而吃 422。
    """
    return ok(
        await artifact_service.run_autofix(session=session, ctx=ctx, version_id=version_id),
        message="已执行一轮自动修复",
    )


# ---------------------------------------------------------------- 冻结


@router.post("/{version_id}/freeze", response_model=ApiResponse[FreezeArtifactManifest], summary="冻结版本")
async def freeze_version(
    version_id: int,
    payload: FreezeRequest,
    ctx: SubjectContext = Depends(get_current_subject),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[FreezeArtifactManifest]:
    """把当前制品冻结成**新的不可变版本**；成功后任何修正都只能产生新版本。

    「门禁有 P0/P1 阻断时禁止冻结」由 service 判定并回 ``GATE_BLOCKED``——
    路由不做这个判断，否则前端置灰就是唯一的防线。

    响应体取 ``detail.manifest``：service 返回的是 ``PipelineVersionDetail``（超集），
    而前端二次确认弹窗读的是**顶层**的 ``manifest`` 字段，不投影就会整屏 undefined。
    """
    detail = await artifact_service.freeze_version(
        session=session,
        ctx=ctx,
        version_id=version_id,
        note=payload.note,
    )
    return ok(detail.manifest, message="版本已冻结")
