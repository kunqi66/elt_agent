"""元数据探查与脱敏样本的业务编排（be-04）。

三条不变量：

1. **探查必须走 ToolBroker**。它出网、读别人的库，是 ``metadata.probe`` 工具；
   走 service 直连就绕开了工具注册表与证据账本。
2. **service 层永远碰不到明文样本**。执行器在返回之前就脱敏了，写进
   ``metadata_profile.masked_samples`` 的已经是 ``phone:a3f19c04b27e`` 这种形态。
   本模块因此**没有**任何「解敏」「回显原文」的函数，也不该有——
   原文只存在于执行器那一个栈帧里。
3. **档案只增不改**。重新探查产生新 ``version``，旧版本留作证据链的一环
   （``PipelineVersion`` 用它绑指纹，改了旧版本等于让已冻结的方案指向新事实）。

本层禁止 ``import fastapi``。
"""

from __future__ import annotations

from typing import Any

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.errors import ConflictError, NotFoundError, ValidationError
from engines.masking import merge_policy
from harness.broker import ExecutionContext, ToolBroker
from harness.ledger import LedgerEvent
from models.datasource import MetadataProfile
from models.enums import EvidenceEventType, ProbeStatus, ToolName
from models.tenant import AppUser
from schemas.auth import SubjectContext
from schemas.datasource import (
    CONNECTOR_REGISTRY,
    MetadataProfileBrief,
    MetadataProfileRead,
    ProbeRequest,
    SampleRowsRead,
    SchemaSnapshotRead,
)
from services.datasource import _build_guard, _ledger, _load, _require_project_roles

log = structlog.get_logger(__name__)

# 样本一次最多返回多少行。库里存的是最多 200 行的快照，所以这个上限不是
# 「分页」而是「别让一次请求把整份快照吐给浏览器」
_MAX_SAMPLE_PAGE = 200


async def _next_version(session: AsyncSession, tenant_id: int, data_source_id: int) -> int:
    """下一个版本号。

    ``MAX(version)+1`` 而不是 ``COUNT(*)+1``：档案是只增不改的，
    但如果将来真的删过行，``COUNT`` 会算出一个已存在的版本号，
    然后撞上 ``uq_metadata_profile_data_source_id_version``——
    一个本可以避免的 500。并发下两者都不安全，最终靠唯一约束兜底。
    """
    current = (
        await session.execute(
            select(func.max(MetadataProfile.version)).where(
                MetadataProfile.tenant_id == tenant_id,
                MetadataProfile.data_source_id == data_source_id,
            )
        )
    ).scalar_one_or_none()
    return int(current or 0) + 1


async def start_probe(
    session: AsyncSession, ctx: SubjectContext, data_source_id: int, payload: ProbeRequest
) -> dict[str, Any]:
    """提交一次探查：先建 ``probing`` 档案，再把探测投给 Celery。

    **先建行、再投任务**，顺序不能反：反过来的话，任务可能在 API 提交事务之前
    就跑完并去更新一个还不存在的 profile_id。前端拿到的 ``{profile_id, version}``
    之后可以直接轮询，状态从 ``probing`` 走到 ``ready``。
    """
    row = await _load(session, ctx, data_source_id)
    await _require_project_roles(session, ctx, row.project_id, write=True)

    if row.status != "active":
        # 停用状态当场拒绝，而不是投个任务让它二十秒后在 worker 里失败。
        # 执行器里也有同样的判断，但那是「万一有人绕过 service 直接调工具」的兜底：
        # 两条路径的**用户可见结果**必须一致，否则同一次操作会有两种错误码
        raise ConflictError("数据源已停用，无法探查元数据")

    spec = CONNECTOR_REGISTRY[row.type]
    if not spec.connectable:
        raise ValidationError(f"{spec.label} 连接器尚未启用，无法探查元数据")

    sampling = payload.sampling
    if sampling.scope == "selected_tables":
        tables = list(sampling.tables or [])
        if not tables:
            raise ValidationError("scope=selected_tables 时必须给出 tables")
    else:
        tables = []
    if sampling.scope == "schema_only" and sampling.rows > 0:
        # 不静默忽略：调用方以为拿到了样本，实际一行都没有，
        # 而前端只会显示一张空表——那看起来像探查失败
        raise ValidationError("scope=schema_only 不产出样本，请改用 full_tables 或 selected_tables")

    # 策略在**提交时就落库**，而不是等执行器算完。探查途中失败时，
    # 档案里仍能看到「当时按什么规则脱的」，这是失败档案唯一的诊断价值
    policy = merge_policy(dict(payload.masking_policy))

    profile = MetadataProfile(
        tenant_id=ctx.tenant_id,
        project_id=row.project_id,
        data_source_id=data_source_id,
        version=await _next_version(session, ctx.tenant_id, data_source_id),
        status=ProbeStatus.PROBING,
        schema_snapshot={},
        sampling={
            "rows": sampling.rows,
            "scope": sampling.scope,
            "tables": tables or None,
        },
        masked_samples={},
        masking_policy=policy,
        tables_count=0,
        sha256="",
        created_by=ctx.user_id,
    )
    session.add(profile)
    await session.commit()

    from tasks.metadata import run_metadata_probe

    run_metadata_probe.apply_async(
        args=[
            data_source_id,
            profile.id,
            ctx.tenant_id,
            row.project_id,
            ctx.user_id,
            ctx.request_id,
            sampling.scope,
            tables,
            sampling.rows,
            dict(payload.masking_policy),
        ],
        queue="etl.default",
    )
    log.info(
        "metadata_probe_queued",
        data_source_id=data_source_id,
        profile_id=profile.id,
        version=profile.version,
    )
    return {"profile_id": profile.id, "version": profile.version}


async def load_profile_state(
    session: AsyncSession, *, tenant_id: int, profile_id: int
) -> str | None:
    """给 Celery 任务用的轻量查状态。返回 ``None`` 表示档案已不存在。

    刻意只查 ``status``：任务入口只需要知道「该不该干」，把整行（含
    ``schema_snapshot``，可能几百 KB）读进内存纯属浪费。
    """
    status = (
        await session.execute(
            select(MetadataProfile.status).where(
                MetadataProfile.id == profile_id, MetadataProfile.tenant_id == tenant_id
            )
        )
    ).scalar_one_or_none()
    return None if status is None else str(ProbeStatus(status).value)


async def run_probe(
    session: AsyncSession,
    *,
    data_source_id: int,
    profile_id: int,
    tenant_id: int,
    project_id: int,
    user_id: int,
    request_id: str,
    scope: str,
    tables: tuple[str, ...],
    sample_rows: int,
    masking_policy: dict[str, Any],
) -> None:
    """执行一次元数据探查。

    收标量参数而不是 ``SubjectContext``，理由同 ``services.datasource.run_probe``：
    跨了进程边界就别假装还握着请求上下文。

    这里是**兜底**：执行器自己会在失败时把档案置 ``failed``，
    但 broker 可能在任何一步就拒绝（进程不符、args 校验不过），
    那时执行器根本没被调用，档案会永远停在 ``probing``——
    前端轮询到天荒地老也等不到终态。所以本函数对「逃逸出来的任何东西」
    都要落一个终态。
    """
    broker = ToolBroker(guard=_build_guard(), ledger=_ledger)
    exec_ctx = ExecutionContext(
        request_id=request_id,
        tenant_id=tenant_id,
        project_id=project_id,
        actor_id=user_id,
        caller="worker",
    )

    try:
        result = await broker.execute(
            ToolName.METADATA_PROBE,
            None,  # P3 免 Capability
            {
                "data_source_id": data_source_id,
                "profile_id": profile_id,
                "scope": scope,
                "tables": tables,
                "sample_rows": sample_rows,
                "masking_policy": masking_policy,
            },
            exec_ctx,
            session=session,
        )
    except Exception:
        await _mark_failed(session, tenant_id=tenant_id, profile_id=profile_id, message="探查未能完成")
        raise

    # 执行器把「源库读不了」当成一次**成功调用但失败的结果**返回
    # （连不上不是工具的调用错误，是探测的结论），档案已由它置 failed。
    # 账本事件两种情况都写：审计要看到「探过」这件事，包括探失败的
    detail = dict(result.detail)
    await _ledger.append(
        session,
        LedgerEvent(
            tenant_id=tenant_id,
            project_id=project_id,
            actor_id=user_id,
            event_type=EvidenceEventType.METADATA_PROBED,
            subject_type="data_source",
            subject_id=str(data_source_id),
            payload={
                "profile_id": profile_id,
                "version": detail.get("version"),
                "tables_count": detail.get("tables_count"),
                "error_code": detail.get("error_code"),
            },
        ),
    )
    await session.commit()


async def _mark_failed(
    session: AsyncSession, *, tenant_id: int, profile_id: int, message: str
) -> None:
    """把档案置终态。**只对还在 probing 的行生效**。

    不覆盖终态：重复投递的任务若失败一次，不能把一个已经 ready 的档案改成 failed。
    """
    profile = (
        await session.execute(
            select(MetadataProfile).where(
                MetadataProfile.id == profile_id, MetadataProfile.tenant_id == tenant_id
            )
        )
    ).scalar_one_or_none()
    if profile is None or profile.status is not ProbeStatus.PROBING:
        return
    profile.status = ProbeStatus.FAILED
    profile.error_message = message[:512]
    await session.commit()


async def list_profiles(
    session: AsyncSession, ctx: SubjectContext, data_source_id: int
) -> list[MetadataProfileBrief]:
    """某数据源的全部档案概要，**倒序**（前端列表按版本降序展示）。

    只返回到概要字段，不带 ``schema_snapshot`` / ``masked_samples``：
    一个 50 张表的快照是几百 KB，而列表页一列都不显示它。
    """
    row = await _load(session, ctx, data_source_id)
    await _require_project_roles(session, ctx, row.project_id, write=False)

    rows = (
        await session.execute(
            select(MetadataProfile)
            .where(
                MetadataProfile.tenant_id == ctx.tenant_id,
                MetadataProfile.data_source_id == data_source_id,
            )
            .order_by(MetadataProfile.version.desc())
        )
    ).scalars()
    return [
        MetadataProfileBrief(
            version=p.version,
            status=p.status,
            tables_count=p.tables_count,
            created_at=p.created_at,
            expires_at=p.expires_at,
        )
        for p in rows
    ]


async def _load_profile(
    session: AsyncSession, ctx: SubjectContext, data_source_id: int, version: int
) -> MetadataProfile:
    """按 (数据源, 版本) 取档案。**带 tenant 过滤**，理由同 ``_load``。"""
    row = await _load(session, ctx, data_source_id)
    await _require_project_roles(session, ctx, row.project_id, write=False)
    profile = (
        await session.execute(
            select(MetadataProfile).where(
                MetadataProfile.tenant_id == ctx.tenant_id,
                MetadataProfile.data_source_id == data_source_id,
                MetadataProfile.version == version,
            )
        )
    ).scalar_one_or_none()
    if profile is None:
        raise NotFoundError("元数据档案不存在")
    return profile


async def get_profile(
    session: AsyncSession, ctx: SubjectContext, data_source_id: int, version: int
) -> MetadataProfileRead:
    """档案详情。

    ``probing`` 状态下 ``schema_snapshot`` 还是空的，这是**正常的中间态**——
    不报 404，否则前端的轮询会在第一个瞬间就当成失败。
    """
    profile = await _load_profile(session, ctx, data_source_id, version)

    creator: str | None = None
    if profile.created_by is not None:
        creator = (
            await session.execute(
                select(AppUser.display_name).where(
                    AppUser.id == profile.created_by, AppUser.tenant_id == ctx.tenant_id
                )
            )
        ).scalar_one_or_none()

    return MetadataProfileRead(
        id=profile.id,
        data_source_id=profile.data_source_id,
        version=profile.version,
        status=profile.status,
        sampling=dict(profile.sampling or {}),
        masking_policy=dict(profile.masking_policy or {}),
        # 只允许 str→str。库里存的就是脱敏后的字符串，但**不假设**：
        # 若历史数据里混进了嵌套结构，这里会把它挡在响应之外，
        # 而不是原样透给前端
        masked_samples={
            str(table): [
                {str(k): str(v) for k, v in row.items()} for row in rows if isinstance(row, dict)
            ]
            for table, rows in (profile.masked_samples or {}).items()
            if isinstance(rows, list)
        },
        tables_count=profile.tables_count,
        sha256=profile.sha256,
        expires_at=profile.expires_at,
        error_message=profile.error_message,
        created_at=profile.created_at,
        created_by_display_name=creator,
    )


async def get_schema(
    session: AsyncSession,
    ctx: SubjectContext,
    data_source_id: int,
    version: int,
    *,
    table: str | None = None,
) -> SchemaSnapshotRead:
    """结构树。``table`` 给了就只回那一张（前端点开某张表时用）。"""
    profile = await _load_profile(session, ctx, data_source_id, version)
    if profile.status is ProbeStatus.FAILED:
        # 失败档案的结构必然是空的，返回空树会让前端显示「这个库没有任何表」，
        # 而真相是「这次没探成功」
        raise ConflictError(
            profile.error_message or "该次探查未能完成",
            details={"status": profile.status.value},
        )

    tables = list((profile.schema_snapshot or {}).get("tables") or [])
    if table is not None:
        tables = [t for t in tables if t.get("name") == table]
    return SchemaSnapshotRead.model_validate({"tables": tables})


async def get_samples(
    session: AsyncSession,
    ctx: SubjectContext,
    data_source_id: int,
    version: int,
    *,
    table: str,
    limit: int,
) -> SampleRowsRead:
    """脱敏样本。

    **没有对应的解敏接口**，也不会有：库里存的就是脱敏值，
    解密所需的原文从未被持久化过。
    """
    profile = await _load_profile(session, ctx, data_source_id, version)
    if profile.status is not ProbeStatus.READY:
        raise ConflictError(
            profile.error_message or "该次探查尚未完成",
            details={"status": profile.status.value},
        )

    rows = (profile.masked_samples or {}).get(table)
    if rows is None:
        # 「这张表有结构但没有样本」是常见情形（schema_only、或超出抽样上限），
        # 与「表名打错了」在响应上无法区分，所以明说是哪一种
        known = sorted((profile.masked_samples or {}).keys())
        raise NotFoundError(
            "该表没有抽样数据（可能未纳入本次抽样范围）",
            details={"sampled_tables": known},
        )
    page = max(1, min(limit, _MAX_SAMPLE_PAGE))
    return SampleRowsRead(rows=rows[:page], truncated=len(rows) > page)
