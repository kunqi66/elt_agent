"""策略判定预演路由（be-07 §3.1）。

本文件只有**一个**接口，而且是只读的（`preview` 不落库、不入账）。
策略规则的增删改与策略版本的提交/审批**不在本轮**：前者需要
`policy_rule.stage` 列、后者需要 `risk_policy_version.status/submitted_by/
submitted_at/reject_comment` 四列，都还没进字典与迁移；前端
`src/api/governance.ts` 已经把这两组接口的形状定下来了，
落它们要连带改表，属治理模块（M4）的范围。缺口与所需列记在
``vibecoding_doc/specs/M2-01-be-PDP风险策略引擎.md`` §10。

前缀与 `api/governance.py` 相同（都是 ``/governance``）但**不塞进那个文件**：
账本是只读审计面，策略是判定面，两者的权限口径与变更节奏都不同。
路径不同，两个 router 同前缀挂载不冲突。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps_auth import get_current_subject
from core.db import get_session
from core.response import ApiResponse, ok
from engines.policy import PolicyDecision
from schemas.auth import SubjectContext
from schemas.policy import PolicyPreviewRequest
from services import policy as policy_service

router = APIRouter(prefix="/governance", tags=["policy"])


@router.post(
    "/policies/preview",
    response_model=ApiResponse[PolicyDecision],
    summary="按当前生效策略预演风险判定",
)
async def preview_policy(
    payload: PolicyPreviewRequest,
    ctx: SubjectContext = Depends(get_current_subject),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[PolicyDecision]:
    """给定「意图 + 资源范围」返回等级、命中规则与生效预算。

    **为什么是 POST 而不是 GET**：请求体是一棵结构化的资源范围，
    塞进 query string 会立刻超出长度限制，且转义后的形式没法review。
    它是 POST 但**无副作用**，所以不要求 `Idempotency-Key`。

    判定走的是「当前生效规则集」，所以响应里带 ``policy_version``：
    同一份输入在不同策略版本下得到不同等级是正常的，
    没有这个字段，前端两次调用的差异就无从解释。
    """
    scope = payload.scope.to_engine(tenant_id=ctx.tenant_id, project_id=payload.project_id)
    decision = await policy_service.preview(
        session,
        ctx,
        project_id=payload.project_id,
        intent=payload.intent,
        scope=scope,
        env=payload.env,
        level=payload.level,
    )
    return ok(decision)
