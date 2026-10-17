"""质量报告与错误分流路由（be-17 §3.1）。

只做参数校验、依赖注入、转发 service、组装响应信封。**所有判定都在 service**：
「报告有没有生成」「这次运行属于谁」「谁有权看」都不在这里。

两组路径**故意并存**，各有各的调用方：

| 方法 | 路径 | 调用方 | 分页 |
|---|---|---|---|
| GET | ``/runs/{run_id}/quality-report`` | 前端运行详情（``frontend/src/api/run.ts``，2s 轮询） | — |
| GET | ``/runs/{run_id}/error-records`` | 前端运行详情 | 页码（``{items, meta}``） |
| GET | ``/monitor/runs/{run_id}/quality-report`` | 设计 §3.1 的监督面 | — |
| GET | ``/monitor/runs/{run_id}/errors`` | 设计 §3.1；按 ``error_code`` 等过滤 | 游标 |
| GET | ``/monitor/runs/{run_id}/errors/summary`` | 图表面板（错误码分布 / 规则命中 / 字段 Top-N） | — |

为什么不合一：前端契约（``types/run.ts`` 的 ``ErrorRecordPage``）是 ``{items, meta}``，
而设计 §3.1 要的是游标分页——**两个调用方已经各自冻结**。底层只有一份实现
（``services/quality.py::_filter_records``），所以这里的分叉只是「形状」，
不是「口径」。

角色闸在 service（``require_read_for_run``）而不是路由依赖上：路由级依赖判的是
「在**任一**项目里持有该角色」，而这里要判的是「在**这个运行所属**项目里持有」——
跨项目场景下两者结论不同。所以**每个路由的第一行都是那一次显式调用**，
写在 service 之前（先判权限再读数据，避免「先读出来再发现没权限」）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps_auth import get_current_subject
from core.db import get_session
from core.response import ApiResponse, Page, ok, page
from schemas.auth import SubjectContext
from schemas.quality import (
    CursorPage,
    ErrorCodeSummaryDTO,
    ErrorRecordDTO,
    QualityReportDTO,
)
from services import quality as quality_service

#: 前端路径组（``/api/v1/runs/...``）。
runs_router = APIRouter(prefix="/runs", tags=["quality"])

#: 监督路径组（``/api/v1/monitor/...``，设计 §3.1）。
monitor_router = APIRouter(prefix="/monitor", tags=["monitor"])

#: 本模块对外的单一出口：``api/router.py`` 只 include 这一个，前缀在内部区分。
router = APIRouter()


# ---------------------------------------------------------------- 前端路径组


@runs_router.get(
    "/{run_id}/quality-report",
    response_model=ApiResponse[QualityReportDTO],
    summary="质量报告（前端运行详情轮询）",
)
async def get_run_quality_report(
    run_id: int,
    ctx: SubjectContext = Depends(get_current_subject),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[QualityReportDTO]:
    """报告未生成时 404（前端把它读作「统计中」并继续轮询，见 ``stores/run.ts``）。"""
    await quality_service.require_read_for_run(session, ctx, run_id)
    return ok(await quality_service.get_report(session, tenant_id=ctx.tenant_id, run_id=run_id))


@runs_router.get(
    "/{run_id}/error-records",
    response_model=ApiResponse[Page[ErrorRecordDTO]],
    summary="错误行摘要（分页）",
)
async def list_run_error_records(
    run_id: int,
    page_no: int = Query(1, ge=1, alias="page"),
    page_size: int = Query(20, ge=1, le=quality_service.MAX_LIMIT),
    error_code: str | None = Query(None, max_length=32, description="按错误码过滤"),
    field_name: str | None = Query(None, max_length=64),
    rule_id: str | None = Query(None, max_length=64),
    ctx: SubjectContext = Depends(get_current_subject),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[Page[ErrorRecordDTO]]:
    """只回 ``payload_digest`` 与 ``reason``，**没有原文列**（CLAUDE.md §5.7）。"""
    await quality_service.require_read_for_run(session, ctx, run_id)
    items, total = await quality_service.page_error_records(
        session,
        tenant_id=ctx.tenant_id,
        run_id=run_id,
        error_code=error_code,
        field_name=field_name,
        rule_id=rule_id,
        page_no=page_no,
        page_size=page_size,
    )
    return page(items, total, page_no=page_no, page_size=page_size)


# ---------------------------------------------------------------- 监督路径组


@monitor_router.get(
    "/runs/{run_id}/quality-report",
    response_model=ApiResponse[QualityReportDTO],
    summary="质量报告（含分流诊断）",
)
async def monitor_quality_report(
    run_id: int,
    ctx: SubjectContext = Depends(get_current_subject),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[QualityReportDTO]:
    """与前端路径同源同构；分开挂是因为前缀属于**不同调用方**，而不是不同数据。"""
    await quality_service.require_read_for_run(session, ctx, run_id)
    return ok(await quality_service.get_report(session, tenant_id=ctx.tenant_id, run_id=run_id))


@monitor_router.get(
    "/runs/{run_id}/errors",
    response_model=ApiResponse[CursorPage[ErrorRecordDTO]],
    summary="错误行摘要（游标分页）",
)
async def monitor_error_records(
    run_id: int,
    error_code: str | None = Query(None, max_length=32),
    field_name: str | None = Query(None, max_length=64),
    rule_id: str | None = Query(None, max_length=64),
    limit: int = Query(50, ge=1, le=quality_service.MAX_LIMIT),
    cursor: str | None = Query(None, description="上一页返回的 next_cursor，原样回传"),
    ctx: SubjectContext = Depends(get_current_subject),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[CursorPage[ErrorRecordDTO]]:
    """游标是不透明串：客户端只该回传 ``next_cursor``，不该自己构造。"""
    await quality_service.require_read_for_run(session, ctx, run_id)
    return ok(
        await quality_service.list_error_records(
            session,
            tenant_id=ctx.tenant_id,
            run_id=run_id,
            error_code=error_code,
            field_name=field_name,
            rule_id=rule_id,
            limit=limit,
            cursor=cursor,
        )
    )


@monitor_router.get(
    "/runs/{run_id}/errors/summary",
    response_model=ApiResponse[ErrorCodeSummaryDTO],
    summary="错误码分布 / 规则命中 / 违规字段 Top-N",
)
async def monitor_error_summary(
    run_id: int,
    ctx: SubjectContext = Depends(get_current_subject),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[ErrorCodeSummaryDTO]:
    """报告未生成时 404——没有报告就没有 ``passed``，用 0 冒充「通过」更危险。"""
    await quality_service.require_read_for_run(session, ctx, run_id)
    return ok(await quality_service.error_code_summary(session, tenant_id=ctx.tenant_id, run_id=run_id))


router.include_router(runs_router)
router.include_router(monitor_router)
