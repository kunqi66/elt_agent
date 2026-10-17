"""数据源路由（be-03 / fe-03）。

只做参数校验、依赖注入、转发 service、组装响应信封。

**路径与 DTO 以 ``frontend/src/api/datasource.ts`` 为准。**与 be-03 §3.1 原设计的差异
已记在 ``schemas/datasource.py`` 的模块 docstring 里（PUT 不是 PATCH、
disable/enable 独立于 DELETE、credentials 不是 secret、连通性测试是 202 + 轮询）。

凭据只从 ``credentials`` 字段进、永不从任何响应出。本文件里没有任何
能把凭据写进日志或响应的语句——这是结构性保证，不是靠 reviewer 盯。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps_auth import get_current_subject
from core.db import get_session
from core.response import ApiResponse, Page, ok, page
from schemas.auth import SubjectContext
from schemas.datasource import (
    DataSourceCreate,
    DataSourceFilter,
    DataSourceRead,
    DataSourceUpdate,
    MetadataProfileBrief,
    MetadataProfileRead,
    ProbeRequest,
    SampleRowsRead,
    SchemaSnapshotRead,
)
from services import datasource as ds_service
from services import metadata as md_service

router = APIRouter(prefix="/datasources", tags=["datasources"])


# ---------------------------------------------------------------- CRUD


@router.get("", response_model=ApiResponse[Page[DataSourceRead]], summary="数据源列表")
async def list_data_sources(
    project_id: int = Query(..., ge=1),
    type: str | None = Query(None, description="逗号分隔，如 mysql,doris"),
    env: str | None = Query(None, description="逗号分隔"),
    sensitive_level: str | None = Query(None, description="逗号分隔"),
    status_: str | None = Query(None, alias="status"),
    q: str | None = Query(None, max_length=128),
    page_no: int = Query(1, alias="page", ge=1),
    page_size: int = Query(20, ge=1, le=200),
    ctx: SubjectContext = Depends(get_current_subject),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[Page[DataSourceRead]]:
    flt = DataSourceFilter(
        project_id=project_id,
        type=type,
        env=env,
        sensitive_level=sensitive_level,
        status=status_,
        q=q,
        page=page_no,
        page_size=page_size,
    )
    items, total = await ds_service.list_data_sources(session, ctx, flt)
    return page(items, total, page_no=flt.page, page_size=flt.page_size)


@router.get("/{data_source_id}", response_model=ApiResponse[DataSourceRead], summary="数据源详情")
async def get_data_source(
    data_source_id: int,
    ctx: SubjectContext = Depends(get_current_subject),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[DataSourceRead]:
    return ok(await ds_service.get_data_source(session, ctx, data_source_id))


@router.post(
    "",
    response_model=ApiResponse[DataSourceRead],
    status_code=status.HTTP_201_CREATED,
    summary="新建数据源",
)
async def create_data_source(
    payload: DataSourceCreate,
    ctx: SubjectContext = Depends(get_current_subject),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[DataSourceRead]:
    """凭据随 body 一次性提交：服务端写 Vault → 建 secret_reference → 建 data_source。

    ``Idempotency-Key`` 由 ``api/idempotency.py`` 的中间件统一提供。
    重放同一 key 不会写出第二份 Vault 版本。
    """
    return ok(
        await ds_service.create_data_source(session, ctx, payload),
        message="数据源已创建",
    )


@router.put("/{data_source_id}", response_model=ApiResponse[DataSourceRead], summary="修改数据源")
async def update_data_source(
    data_source_id: int,
    payload: DataSourceUpdate,
    ctx: SubjectContext = Depends(get_current_subject),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[DataSourceRead]:
    """``type`` 不可改（换类型等于换数据源）；``credentials`` 缺省表示不变更凭据。"""
    return ok(await ds_service.update_data_source(session, ctx, data_source_id, payload))


@router.post("/{data_source_id}/disable", response_model=ApiResponse[DataSourceRead], summary="停用")
async def disable_data_source(
    data_source_id: int,
    ctx: SubjectContext = Depends(get_current_subject),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[DataSourceRead]:
    """停用而非删除：**凭据保留**。停用是「先别用它」，不是「忘掉它」。"""
    return ok(await ds_service.set_status(session, ctx, data_source_id, enable=False), message="已停用")


@router.post("/{data_source_id}/enable", response_model=ApiResponse[DataSourceRead], summary="启用")
async def enable_data_source(
    data_source_id: int,
    ctx: SubjectContext = Depends(get_current_subject),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[DataSourceRead]:
    return ok(await ds_service.set_status(session, ctx, data_source_id, enable=True), message="已启用")


@router.delete(
    "/{data_source_id}",
    response_model=ApiResponse[None],
    status_code=status.HTTP_200_OK,
    summary="删除数据源",
)
async def delete_data_source(
    data_source_id: int,
    ctx: SubjectContext = Depends(get_current_subject),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[None]:
    """真删。被流水线版本引用时回 409 + 引用计数。"""
    await ds_service.delete_data_source(session, ctx, data_source_id)
    return ok(None, message="数据源已删除")


# ---------------------------------------------------------------- 凭据


@router.post(
    "/{data_source_id}/credentials",
    response_model=ApiResponse[DataSourceRead],
    summary="重新设置凭据",
)
async def set_credentials(
    data_source_id: int,
    payload: dict[str, dict[str, str]],
    ctx: SubjectContext = Depends(get_current_subject),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[DataSourceRead]:
    """唯一接收凭据的接口之一。

    形状直接取 ``{"credentials": {...}}`` 而不是套一个 Pydantic 模型：
    凭据的键名是**按数据源类型变化**的，用一个固定模型反而会在模型定义里
    留下 ``password`` 这样的字段名，成为「到底存不存值」的歧义来源。
    键名白名单与全量提交要求在 service 层按类型校验。
    """
    credentials = _extract_credentials(payload)
    return ok(
        await ds_service.set_credentials(session, ctx, data_source_id, credentials),
        message="凭据已更新",
    )


def _extract_credentials(payload: dict[str, dict[str, str]]) -> dict[str, str]:
    from core.errors import ValidationError

    raw = payload.get("credentials")
    if not isinstance(raw, dict) or not raw:
        raise ValidationError("必须提交 credentials 且非空")
    for key, value in raw.items():
        if not isinstance(value, str) or not value.strip():
            raise ValidationError(f"凭据 {key} 必须是非空字符串")
    return {str(k): str(v) for k, v in raw.items()}


# ---------------------------------------------------------------- 连通性测试


@router.post(
    "/{data_source_id}/connectivity-tests",
    response_model=ApiResponse[dict[str, str]],
    status_code=status.HTTP_202_ACCEPTED,
    summary="发起连通性测试",
)
async def start_connectivity_test(
    data_source_id: int,
    response: Response,
    ctx: SubjectContext = Depends(get_current_subject),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[dict[str, str]]:
    """返回 202 + ``{test_id, state: "pending"}``，前端随后轮询。

    这里显式把状态码钉死为 202：``status_code`` 声明在装饰器上，
    但 FastAPI 在返回自定义 ``Response`` 时以它为准——不设的话会退回 200，
    而前端的 ``client.ts`` 按 2xx 判定成功、按 state 判定业务结果，
    两者都对；但 202 能让「这是异步的」这件事在日志与网关层面也看得出来。
    """
    response.status_code = status.HTTP_202_ACCEPTED
    return ok(await ds_service.start_connectivity_test(session, ctx, data_source_id))


@router.get(
    "/{data_source_id}/connectivity-tests/{test_id}",
    response_model=ApiResponse[dict[str, object]],
    summary="查询连通性测试结果",
)
async def get_connectivity_test(
    data_source_id: int,
    test_id: str,
    ctx: SubjectContext = Depends(get_current_subject),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[dict[str, object]]:
    """**业务结果不是请求错误**：连不上也回 200 + ``state="failed"``。

    否则前端分不清「后端挂了」与「目标库挂了」，两种情况的重试策略完全不同。
    """
    return ok(await ds_service.get_connectivity_test(session, ctx, data_source_id, test_id))


# ---------------------------------------------------------------- 元数据探查


@router.post(
    "/{data_source_id}/probes",
    response_model=ApiResponse[dict[str, int]],
    status_code=status.HTTP_202_ACCEPTED,
    summary="提交元数据探查",
)
async def start_probe(
    data_source_id: int,
    payload: ProbeRequest,
    response: Response,
    ctx: SubjectContext = Depends(get_current_subject),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[dict[str, int]]:
    """返回 ``{profile_id, version}``，此时档案状态是 ``probing``。

    与连通性测试不同，这里**同步把档案行建出来**（``services.metadata.start_probe``），
    所以前端拿到 profile_id 就能直接轮询详情，不需要再等一个 test_id。
    """
    response.status_code = status.HTTP_202_ACCEPTED
    return ok(
        await md_service.start_probe(session, ctx, data_source_id, payload),
        message="探查已提交",
    )


@router.get(
    "/{data_source_id}/profiles",
    response_model=ApiResponse[list[MetadataProfileBrief]],
    summary="探查档案列表",
)
async def list_profiles(
    data_source_id: int,
    ctx: SubjectContext = Depends(get_current_subject),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[list[MetadataProfileBrief]]:
    return ok(await md_service.list_profiles(session, ctx, data_source_id))


@router.get(
    "/{data_source_id}/profiles/{version}",
    response_model=ApiResponse[MetadataProfileRead],
    summary="探查档案详情",
)
async def get_profile(
    data_source_id: int,
    version: int,
    ctx: SubjectContext = Depends(get_current_subject),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[MetadataProfileRead]:
    """含 ``masked_samples``。**库里存的就是脱敏值**，没有原文，也没有解敏接口。"""
    return ok(await md_service.get_profile(session, ctx, data_source_id, version))


@router.get(
    "/{data_source_id}/profiles/{version}/schema",
    response_model=ApiResponse[SchemaSnapshotRead],
    summary="表结构快照",
)
async def get_schema(
    data_source_id: int,
    version: int,
    table: str | None = Query(None, max_length=128, description="只取这一张表"),
    ctx: SubjectContext = Depends(get_current_subject),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[SchemaSnapshotRead]:
    return ok(await md_service.get_schema(session, ctx, data_source_id, version, table=table))


@router.get(
    "/{data_source_id}/profiles/{version}/samples",
    response_model=ApiResponse[SampleRowsRead],
    summary="脱敏样本",
)
async def get_samples(
    data_source_id: int,
    version: int,
    table: str = Query(..., min_length=1, max_length=128),
    limit: int = Query(50, ge=1, le=200),
    ctx: SubjectContext = Depends(get_current_subject),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[SampleRowsRead]:
    """返回的值**在入库前就已脱敏**，前端无从解敏，也不需要解。"""
    return ok(await md_service.get_samples(session, ctx, data_source_id, version, table=table, limit=limit))
