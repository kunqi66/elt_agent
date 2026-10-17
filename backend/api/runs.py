"""Commit 与运行中心路由（be-14 / fe-06）。

只做参数校验、依赖注入、转发 service、组装响应信封。**所有判定都在 service**：
「能不能提交」「这个动作现在合不合法」「是不是 operator」都不在这里。

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | ``/runs`` | Commit：把一张审批通过的准备单变成一条排队中的运行 |
| GET | ``/runs`` | 运行列表 |
| GET | ``/runs/{id}`` | 运行详情（**不含令牌原文**） |
| POST | ``/runs/{id}/pause`` | 暂停推进（不调 SeaTunnel） |
| POST | ``/runs/{id}/cancel`` | 取消：经 ``seatunnel.stop`` |
| POST | ``/runs/{id}/rollback`` | 受管回滚（be-18）：清理影子表，正式表不动 |
| GET | ``/runs/{id}/publish`` | 发布状态与对账明细（be-18）：**只读** |

发布**没有** HTTP 触发入口（be-18 §3.1）：``passed=True`` 之后由 worker 自动发布。
做成按钮等于把一次 P0 动作的触发权从质量门禁转移给「谁能点按钮」。

``auditor`` 的只读是**天然**的：本模块没有任何给只读角色的写入口——
写接口的角色判定在 ``services/run.py::_require_operator``（operator 槽）。
不在路由上加 ``require_role`` 依赖，是因为那个依赖判的是「在**任一**项目里持有」，
而这里要判的是「在**这个**项目里持有」——两者在跨项目场景下结论不同。

``Idempotency-Key`` 由 ``api/idempotency.py`` 的中间件统一处理。
Commit 额外要求它非空（见 ``services/run.commit``）：``execution_run.idempotency_key``
是唯一约束，头里没有值时那次插入会得到一个没有语义的键。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps_auth import get_current_subject
from api.idempotency import IDEMPOTENCY_HEADER
from core.db import get_session
from core.response import ApiResponse, Page, ok
from models.enums import RunStatus
from schemas.auth import SubjectContext
from schemas.publish import (
    PublishStateDTO,
    RollbackRequest,
    RollbackResultDTO,
)
from schemas.run import RunActionRequest, RunCreateRequest, RunDetail, RunSummary
from services import publish as publish_service
from services import run as run_service

router = APIRouter(prefix="/runs", tags=["runs"])


def _idempotency_key(request: Request) -> str:
    """取幂等键交给 service 记账（去重本身由中间件负责，见模块 docstring）。"""
    return (request.headers.get(IDEMPOTENCY_HEADER) or "").strip()


@router.post("", response_model=ApiResponse[RunDetail], summary="提交执行（Commit）")
async def commit_run(
    request: Request,
    payload: RunCreateRequest,
    ctx: SubjectContext = Depends(get_current_subject),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[RunDetail]:
    """签发 Capability 并排入执行队列。

    同版本已有活跃运行时 409 ``RUN_CONFLICT``，且这次尝试会进证据账本——
    「谁在什么时候试图重复提交同一个版本」是审计要回答的问题。
    """
    return ok(
        await run_service.commit(
            session,
            preparation_id=payload.preparation_id,
            subject=ctx,
            idempotency_key=_idempotency_key(request),
        )
    )


@router.get("", response_model=ApiResponse[Page[RunSummary]], summary="运行列表")
async def list_runs(
    status: RunStatus | None = Query(None, description="按运行状态筛选"),
    pipeline_id: int | None = Query(None, ge=1),
    pipeline_version_id: int | None = Query(None, ge=1),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    ctx: SubjectContext = Depends(get_current_subject),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[Page[RunSummary]]:
    items, total = await run_service.list_runs(
        session,
        ctx,
        status=status,
        pipeline_id=pipeline_id,
        pipeline_version_id=pipeline_version_id,
        page=page,
        page_size=page_size,
    )
    return page(items, total, page_no=page, page_size=page_size)


@router.get("/{run_id}", response_model=ApiResponse[RunDetail], summary="运行详情")
async def get_run(
    run_id: int,
    ctx: SubjectContext = Depends(get_current_subject),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[RunDetail]:
    """``can_cancel`` / ``can_rollback`` 由服务端按转移表算好下发（前端只负责置灰）。"""
    return ok(await run_service.get_run(session, ctx, run_id))


@router.post("/{run_id}/rollback", response_model=ApiResponse[RollbackResultDTO], summary="受管回滚")
async def rollback_run(
    request: Request,
    run_id: int,
    payload: RollbackRequest,
    ctx: SubjectContext = Depends(get_current_subject),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[RollbackResultDTO]:
    """清理影子表，**正式表不动**（be-18 §5.2）。

    这里只受理，不执行：目标端 DDL 是副作用，必须经 ``ToolBroker`` 在 Worker 里发生。
    因此返回时状态是 ``rolling_back``，执行结果看 ``GET /runs/{id}/publish``。

    请求体里**没有任何表名**——影子表与正式表一律由服务端从运行行读出，
    并被四处范围校验（后缀重算 / 不等 / 同库 / 引擎受支持）钉住：
    这四处合起来保证「一次误操作最多删掉一个影子表，永远删不到正式表」。
    """
    return ok(
        await publish_service.request_rollback(
            session,
            subject=ctx,
            run_id=run_id,
            idempotency_key=_idempotency_key(request),
            reason=payload.reason,
        )
    )


@router.get("/{run_id}/publish", response_model=ApiResponse[PublishStateDTO], summary="发布状态")
async def get_publish_state(
    run_id: int,
    ctx: SubjectContext = Depends(get_current_subject),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[PublishStateDTO]:
    """发布/回滚的已知事实：最终态、两个指纹、指纹口径、回滚进展。

    结论由**证据账本**复原，不是内存状态：进程被杀之后，「这次到底发布没发布」
    只能从账本回答。``publish_state="not_attempted"`` 与 ``"unknown"`` 是两个不同的答案，
    前端据此决定是「还没走到发布」还是「要人工核对目标端」。
    """
    return ok(await publish_service.get_publish_state(session, tenant_id=ctx.tenant_id, run_id=run_id))


@router.post("/{run_id}/pause", response_model=ApiResponse[RunDetail], summary="暂停运行")
async def pause_run(
    run_id: int,
    payload: RunActionRequest,
    ctx: SubjectContext = Depends(get_current_subject),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[RunDetail]:
    """暂停**不调 SeaTunnel**：作业继续跑，只是不再推进后续阶段（be-14 §10 第 4 条）。"""
    return ok(
        await run_service.request_pause(session, run_id=run_id, subject=ctx, reason=payload.reason)
    )


@router.post("/{run_id}/cancel", response_model=ApiResponse[RunDetail], summary="取消运行")
async def cancel_run(
    run_id: int,
    payload: RunActionRequest,
    ctx: SubjectContext = Depends(get_current_subject),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[RunDetail]:
    """取消会真的停作业：另签一张绑定 ``seatunnel.stop`` 的 Capability 交给 Worker 执行。

    重复调用返回当前状态（幂等），不会因为「已经取消了」报错。
    """
    return ok(
        await run_service.request_cancel(session, run_id=run_id, subject=ctx, reason=payload.reason)
    )
