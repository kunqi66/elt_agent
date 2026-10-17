"""Pipeline 列表、详情与生成会话路由（be-09 / fe-04）。

只做参数校验、依赖注入、转发 service、组装响应信封。**这里没有任何业务判断**：
「门禁没过不能冻结」「自动修复已用尽」「状态不是 ready_to_freeze 要 409」全在
service / engines 里，路由拿到的要么是一次成功调用，要么是一个 ``core.errors`` 的 AppError。

**路径与 DTO 以 ``frontend/src/api/pipeline.ts`` 为准**；与 service 契约的差异登记在
``schemas/pipeline.py`` 与 ``schemas/version.py`` 的模块 docstring 里。

``Idempotency-Key`` 支持由 ``api/idempotency.py`` 的**中间件**统一提供，本文件不需要
逐条声明——中间件按 ``(tenant_id, 方法+路径, key)`` 去重，覆盖所有写动词。

**注册顺序即匹配优先级**：字面量段（``/generations``）一律排在动态段（``/{pipeline_id}``）
之前。Starlette 按顺序匹配，先注册的动态段会把字面量当成 id 吃进 int 校验。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps_auth import get_current_subject
from core.db import get_session
from core.errors import ValidationError
from core.response import ApiResponse, Page, ok, page
from models.enums import PipelineStatus
from schemas.auth import SubjectContext
from schemas.pipeline import (
    AbortRequest,
    AnswersRequest,
    PipelineFilter,
    PipelineRead,
    StartGenerationRequest,
    WorkflowSnapshot,
)
from schemas.version import PipelineVersionSummary
from services.pipeline import artifacts as artifact_service
from services.pipeline import generation as generation_service

router = APIRouter(prefix="/pipelines", tags=["pipelines"])


def _parse_statuses(raw: str | None) -> list[PipelineStatus] | None:
    """``status=draft,gate_failed`` → 枚举列表。

    未知取值回 422 而不是忽略：静默丢掉一个筛选项，用户看到的是一份「看起来完整」
    的失败清单，而它少了一整类——这比报错危险得多。抛 ``core.errors.ValidationError``
    而不是让 ``ValueError`` 冒出去，否则会落到兜底处理器变成 500，
    前端只能显示成「服务器错误」，用户看不出是自己把枚举名拼错了。
    """
    if not raw:
        return None
    try:
        return [PipelineStatus(part.strip()) for part in raw.split(",") if part.strip()]
    except ValueError as exc:
        raise ValidationError(f"未知的流水线状态：{exc}") from exc


# ---------------------------------------------------------------- 列表
#
# 根路径的列表注册在最前：它是唯一不带路径参数的路由，不会与任何动态段竞争。


@router.get("", response_model=ApiResponse[Page[PipelineRead]], summary="流水线列表")
async def list_pipelines(
    project_id: int | None = Query(None, ge=1),
    status_: str | None = Query(None, alias="status", description="逗号分隔，如 draft,gate_failed"),
    q: str | None = Query(None, max_length=128),
    page_no: int = Query(1, alias="page", ge=1),
    page_size: int = Query(20, ge=1, le=200),
    ctx: SubjectContext = Depends(get_current_subject),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[Page[PipelineRead]]:
    """分页信封走 ``core.response.page()``——前端分页组件读的是 ``meta``，不是数组长度。

    ⚠️ service 的 ``list_pipelines`` 目前只收 ``project_id / page / page_size``，
    **``status`` 与 ``q`` 暂时传不进去**（见 ``schemas/pipeline.py`` 的差异表）。
    这里仍然照常校验它们，是为了「非法枚举值照样 422」，而不是换个安静的方式出错。
    """
    flt = PipelineFilter(
        status=_parse_statuses(status_),
        q=q,
        page=page_no,
        page_size=page_size,
    )
    items, total = await artifact_service.list_pipelines(
        session=session,
        ctx=ctx,
        project_id=project_id,
        page=flt.page,
        page_size=flt.page_size,
    )
    return page(items, total, page_no=flt.page, page_size=flt.page_size)


# ---------------------------------------------------------------- 生成会话
#
# 整段排在 ``/{pipeline_id}`` 之前：``/generations/{thread_id}`` 与
# ``/{pipeline_id}/versions`` 段数相同，顺序反了就会出现「字面量被当成 id」。


@router.post(
    "/generations",
    response_model=ApiResponse[WorkflowSnapshot],
    summary="发起生成",
)
async def start_generation(
    payload: StartGenerationRequest,
    ctx: SubjectContext = Depends(get_current_subject),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[WorkflowSnapshot]:
    """响应体**可能已经带 interrupt**（澄清问题），前端不得假定一定有下一步。

    ``project_id`` 从 body 取：它是写目标，放 body 才能进幂等中间件的请求指纹。
    """
    return ok(
        await generation_service.start_generation(
            session=session,
            ctx=ctx,
            project_id=payload.project_id,
            requirement=payload.requirement,
            source_data_source_id=payload.source_data_source_id,
            target_data_source_id=payload.target_data_source_id,
        ),
        message="生成已发起",
    )


@router.post(
    "/generations/{thread_id}/answers",
    response_model=ApiResponse[WorkflowSnapshot],
    summary="提交澄清回答",
)
async def answer_clarification(
    thread_id: str,
    payload: AnswersRequest,
    ctx: SubjectContext = Depends(get_current_subject),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[WorkflowSnapshot]:
    """写回 checkpoint 并继续图执行，返回**新的**快照（可能又问一轮）。

    线上请求体是 ``{"answers": {"<question_key>": "<值>"}}``（一个映射），
    转成 service 要的列表这一步只做形状归一，不含任何解释。
    """
    return ok(
        await generation_service.resume_generation(
            session=session,
            ctx=ctx,
            thread_id=thread_id,
            answers=payload.as_inputs(),
        )
    )


@router.get(
    "/generations/{thread_id}",
    response_model=ApiResponse[WorkflowSnapshot],
    summary="生成快照",
)
async def get_generation(
    thread_id: str,
    ctx: SubjectContext = Depends(get_current_subject),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[WorkflowSnapshot]:
    """断线恢复用：前端刷新后靠它把整个 Studio 界面重建出来。"""
    return ok(await generation_service.get_generation(session=session, ctx=ctx, thread_id=thread_id))


@router.post(
    "/generations/{thread_id}/abort",
    response_model=ApiResponse[WorkflowSnapshot],
    summary="中断生成",
)
async def abort_generation(
    thread_id: str,
    payload: AbortRequest,
    ctx: SubjectContext = Depends(get_current_subject),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[WorkflowSnapshot]:
    """用户点「取消」——**不是硬中断**，不产生证据事件（硬中断见 M4 的运行监督）。

    ``payload.reason`` 目前被 service 丢弃（签名没有该入参），见
    ``schemas/pipeline.py`` 的差异表；照收是为了前端不因多一个键而吃 422。
    """
    return ok(
        await generation_service.abort_generation(session=session, ctx=ctx, thread_id=thread_id),
        message="已中断生成",
    )


# ---------------------------------------------------------------- 详情与版本


@router.get(
    "/{pipeline_id}/versions", response_model=ApiResponse[Page[PipelineVersionSummary]], summary="版本列表"
)
async def list_versions(
    pipeline_id: int,
    page_no: int = Query(1, alias="page", ge=1),
    page_size: int = Query(20, ge=1, le=200),
    ctx: SubjectContext = Depends(get_current_subject),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[Page[PipelineVersionSummary]]:
    """只给摘要。整份制品（plan / hocon）走 ``/pipeline-versions/{id}``。

    ``total`` 取本页条数：service 的 ``list_versions`` 只回列表、不回总数
    （见 ``schemas/version.py`` 的差异表）。要准确翻页得先给 service 加计数返回。
    """
    items = await artifact_service.list_versions(session=session, ctx=ctx, pipeline_id=pipeline_id)
    return page(items, len(items), page_no=page_no, page_size=page_size)


@router.get("/{pipeline_id}", response_model=ApiResponse[PipelineRead], summary="流水线详情")
async def get_pipeline(
    pipeline_id: int,
    ctx: SubjectContext = Depends(get_current_subject),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[PipelineRead]:
    return ok(await artifact_service.get_pipeline(session=session, ctx=ctx, pipeline_id=pipeline_id))
