"""安全治理路由（be-06 账本部分 / fe-07）。

本文件只聚合证据账本的三个只读入口，**不写任何逻辑**：策略规则、预算、
Benchmark 归 M4 的其它模块，届时在 ``api/router.py`` 里与本文并排挂载即可。

路径以 ``frontend/src/api/governance.ts`` 为准。与 be-06 §3.1 原设计的差异
（`/governance/ledger` → `/governance/evidence`、offset → 游标分页、
`GET verify` → `POST verify`）记在 ``schemas/governance.py`` 的模块 docstring 里。

**没有任何写接口**。账本 append-only，写入口只有服务端内部的
``harness.EvidenceLedger.append()``——不对外暴露，是架构约束不是「还没做」。
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps_auth import get_current_subject
from core.db import get_session
from core.errors import ValidationError
from core.response import ApiResponse, ok
from models.enums import EvidenceEventType
from schemas.auth import SubjectContext
from schemas.governance import EvidenceFilter, EvidencePage, LedgerVerifyResult, VerifyRequest
from services import governance as gov_service

router = APIRouter(prefix="/governance", tags=["governance"])


def _csv_types(raw: str | None) -> list[EvidenceEventType] | None:
    """``event_type=run.failed,swap.failed`` → 枚举列表。

    未知取值直接 422 而不是忽略：静默丢掉一个筛选项，用户看到的是一份
    「看起来完整」的结果，而它其实少了一整类事件——审计场景下这比报错危险得多。

    抛 `core.errors.ValidationError` 而**不是**让 `Enum(...)` 的 `ValueError` 冒出去：
    `ValueError` 没有对应的处理器，会一路落到兜底 `Exception` 处理器变成 500，
    而 500 在前端只能显示成「服务器错误」——用户看不出是自己把枚举名拼错了。
    """
    if not raw:
        return None
    try:
        return [EvidenceEventType(part.strip()) for part in raw.split(",") if part.strip()]
    except ValueError as exc:
        raise ValidationError(f"未知的事件类型：{exc}") from exc


@router.get("/evidence", response_model=ApiResponse[EvidencePage], summary="证据账本查询")
async def list_evidence(
    project_id: int = Query(..., ge=1),
    event_type: str | None = Query(None, description="逗号分隔，取值见 EvidenceEventType"),
    subject_id: str | None = Query(None, max_length=128),
    pipeline_version_id: int | None = Query(None, ge=1),
    execution_run_id: int | None = Query(None, ge=1),
    from_: datetime | None = Query(None, alias="from", description="occurred_at 下界（ISO8601）"),
    to: datetime | None = Query(None, description="occurred_at 上界（ISO8601）"),
    cursor: str | None = Query(None, max_length=64),
    limit: int = Query(50, ge=1, le=200),
    ctx: SubjectContext = Depends(get_current_subject),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[EvidencePage]:
    flt = EvidenceFilter(
        project_id=project_id,
        event_type=_csv_types(event_type),
        subject_id=subject_id,
        pipeline_version_id=pipeline_version_id,
        execution_run_id=execution_run_id,
        occurred_from=from_,
        occurred_to=to,
        cursor=cursor,
        limit=limit,
    )
    return ok(await gov_service.list_evidence(session, ctx, flt))


@router.post("/evidence/verify", response_model=ApiResponse[LedgerVerifyResult], summary="验链")
async def verify_ledger(
    payload: VerifyRequest,
    ctx: SubjectContext = Depends(get_current_subject),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[LedgerVerifyResult]:
    """重算链条并返回结论。**断链不是 HTTP 错误**（be-06 §7 第 9 行）：
    200 + ``ok=False``，因为「查到了断链」是一次成功的查询，
    而前端要拿这个结果去渲染具体的断点位置，500 会把它变成一句「服务器错误」。
    """
    return ok(await gov_service.verify_ledger(session, ctx, payload))


@router.get("/evidence/export", summary="导出账本（JSON / CSV）")
async def export_evidence(
    project_id: int = Query(..., ge=1),
    format: str = Query("json", pattern="^(json|csv)$"),
    event_type: str | None = Query(None),
    subject_id: str | None = Query(None, max_length=128),
    pipeline_version_id: int | None = Query(None, ge=1),
    execution_run_id: int | None = Query(None, ge=1),
    from_: datetime | None = Query(None, alias="from"),
    to: datetime | None = Query(None),
    limit: int = Query(50_000, ge=1, le=50_000, description="本次导出最多带走多少条"),
    ctx: SubjectContext = Depends(get_current_subject),
    session: AsyncSession = Depends(get_session),
) -> StreamingResponse:
    """审计拿走一份**离线可自证**的副本。

    不套 ``ApiResponse`` 信封：这是一份要存盘归档的文件，
    外面裹一层 JSON 信封会让 ``csv`` 格式直接失效（Excel 打不开）。
    整个响应体由 service 流式产出，服务端不在内存里攒整份文件。
    """
    flt = EvidenceFilter(
        project_id=project_id,
        event_type=_csv_types(event_type),
        subject_id=subject_id,
        pipeline_version_id=pipeline_version_id,
        execution_run_id=execution_run_id,
        occurred_from=from_,
        occurred_to=to,
    )
    media = "text/csv; charset=utf-8" if format == "csv" else "application/json; charset=utf-8"
    filename = f"evidence-project{project_id}.{format}"
    return StreamingResponse(
        gov_service.export_evidence(session, ctx, flt, fmt=format, max_events=limit),
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
