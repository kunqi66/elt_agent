"""准备单与审批路由（be-13 / fe-05）。

只做参数校验、依赖注入、转发 service、组装响应信封。**所有判定都在 service**：
「谁不能批自己的单」「指纹变没变」「哪些槽还差」都不在这里——路由收到的要么是成功结果，
要么是 ``core.errors`` 的 AppError。

路径与 DTO 以 ``frontend/src/api/preparation.ts`` 为准（差异登记在
``schemas/preparation.py`` 的模块 docstring 里）：

| 方法 | 路径 | 归属 |
|---|---|---|
| POST | ``/preparations`` | 本模块（发起准备） |
| GET | ``/preparations`` | 本模块（列表） |
| GET | ``/preparations/{id}`` | 本模块（详情） |
| GET | ``/preparations/{id}/status`` | 本模块（轻量轮询） |
| POST | ``/preparations/{id}/decisions`` | 本模块（投一条审批决定） |
| POST | ``/preparations/{id}/commit`` | **be-14**（签发 Capability 并执行），不在此文件 |

``Idempotency-Key`` 由 ``api/idempotency.py`` 的中间件统一处理（``main.create_app`` 已挂载），
所以审批接口天生支持重放保护，不需要逐条声明——这也是 ``services/approval.decide``
收下 ``idempotency_key`` 却不自己实现幂等的原因（两套幂等语义必然对「同 key 不同 body」
给出不同结论）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps_auth import get_current_subject
from core.db import get_session
from core.response import ApiResponse, Page, ok
from models.enums import ApprovalSlot, Env, PreparationStatus, RiskLevel
from schemas.auth import SubjectContext
from schemas.preparation import (
    DecisionRequest,
    PreparationCreateRequest,
    PreparationDetail,
    PreparationStatusView,
    PreparationSummary,
)
from services import approval as approval_service
from services import preparation as preparation_service

router = APIRouter(prefix="/preparations", tags=["preparations"])


@router.post("", response_model=ApiResponse[PreparationDetail], summary="发起准备")
async def create_preparation(
    payload: PreparationCreateRequest,
    ctx: SubjectContext = Depends(get_current_subject),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[PreparationDetail]:
    """把一个**已冻结**的版本提交为准备单：算指纹、跑 PDP、定必需审批槽。

    零副作用：不签发 Capability、不建执行记录、不投 Outbox（be-13 §5.1 步骤 13）。
    P0 会被拒（403 POLICY_BLOCKED）且**不落准备单**，但这次尝试会进证据账本。
    """
    return ok(await preparation_service.create_preparation(session, ctx, payload))


@router.get("", response_model=ApiResponse[Page[PreparationSummary]], summary="准备单列表")
async def list_preparations(
    scope: str = Query("all", pattern="^(all|mine|todo|done)$", description="all/mine/todo/done"),
    status: PreparationStatus | None = Query(None, description="按状态筛选"),
    risk_level: RiskLevel | None = Query(None, description="按风险等级筛选"),
    environment: Env | None = Query(None, description="按环境筛选"),
    slot: ApprovalSlot | None = Query(None, description="只看该审批槽待处理的"),
    pipeline_id: int | None = Query(None, ge=1),
    keyword: str = Query("", max_length=128, description="按流程名搜索"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    ctx: SubjectContext = Depends(get_current_subject),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[Page[PreparationSummary]]:
    """``scope=todo`` 是审批人的待办：我持有该槽、该槽还没批、且单子还活着。

    ``my_slot`` / ``can_commit`` 由服务端算好下发，前端只负责显示与置灰
    （前端自算等于把「能不能批」变成一次客户端判断，CLAUDE.md §5.8）。
    """
    items, total = await preparation_service.list_preparations(
        session,
        ctx,
        scope=scope,
        status=status,
        risk_level=risk_level.value if risk_level else None,
        environment=environment.value if environment else None,
        slot=slot,
        pipeline_id=pipeline_id,
        keyword=keyword.strip(),
        page=page,
        page_size=page_size,
    )
    return page(items, total, page_no=page, page_size=page_size)


@router.get("/{preparation_id}", response_model=ApiResponse[PreparationDetail], summary="准备单详情")
async def get_preparation(
    preparation_id: int,
    ctx: SubjectContext = Depends(get_current_subject),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[PreparationDetail]:
    """详情里的 ``stale`` 与 ``can_commit`` 是**重算指纹之后**的权威值。

    列表接口为了不做 N×4 次制品查询，只按「状态 + 有效期」给结论；
    真正能不能提交由本接口与 Commit 前的 ``assert_committable`` 说了算。
    """
    return ok(await preparation_service.get_preparation(session, ctx, preparation_id))


@router.get(
    "/{preparation_id}/status",
    response_model=ApiResponse[PreparationStatusView],
    summary="准备单状态（轮询）",
)
async def get_preparation_status(
    preparation_id: int,
    ctx: SubjectContext = Depends(get_current_subject),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[PreparationStatusView]:
    """审批页轮询它而不是详情：详情要读四份制品 + 账本，代价高一个量级。"""
    return ok(await preparation_service.get_status(session, ctx, preparation_id))


@router.post(
    "/{preparation_id}/decisions",
    response_model=ApiResponse[PreparationDetail],
    summary="提交审批决定",
)
async def submit_decision(
    preparation_id: int,
    payload: DecisionRequest,
    ctx: SubjectContext = Depends(get_current_subject),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[PreparationDetail]:
    """投一条具名决定（approve / reject / return）。

    四眼与自批拦截都在服务端（``services/approval.py``），数据库还有
    ``approver_id <> preparation_created_by`` 的 CHECK 兜底——两处都拦，
    前端置灰只算提示（CLAUDE.md §5.8）。
    """
    return ok(
        await approval_service.decide(
            session,
            preparation_id=preparation_id,
            subject=ctx,
            slot=payload.slot,
            decision=payload.decision,
            comment=payload.comment,
        )
    )
