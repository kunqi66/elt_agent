"""生成链路的持久化层（**只做数据访问，不做业务判断**）。

be-09 §2 明确允许 ``workflow/nodes/*`` 依赖本模块（而**禁止**依赖 orchestrator）——
因为编排节点要落盘 Artifact、要读档案，但「该不该落、落了算不算数」这类判断
属于 service。把数据访问单独一层，是为了让「节点里的 SQL」这件事在架构上就不可能发生。

## 候选版本：本模块唯一有判断味道的地方

``get_or_create_candidate`` 实现的是「**一个 pipeline 最多一行候选**」这条不变式：

- 候选 = ``pipeline_version`` 里 ``frozen_at IS NULL`` 的那一行；
- 已存在就**原地更新**（重生成、自动修复都要改它）；
- 不存在就新建，版本号取 ``MAX(version_no) + 1``。

为什么候选不另建一张表：前端 Studio 在**冻结之前**就要按 id 拉 Plan/HOCON/门禁
（`loadArtifacts(versionId)`），而 ``pipeline.current_version_id`` 是指向
``pipeline_version.id`` 的外键——候选若不在那张表里就没有可寻址的 id。
身份冲突就解决了：**同一行，冻结前是草案，冻结后是历史**。

并发：两个请求同时发现「没有候选」→ 都去建 → 撞上
``uq_pipeline_version_one_candidate``（部分唯一索引，只约束
``frozen_at IS NULL`` 的行）或 ``(pipeline_id, version_no)`` 唯一约束。
这里用**保存点 + 有限重试**兜住，且重试时**重新查一次**候选（be-12 §5.2 的做法）：
重试的意义就是让后到的那个事务看到先到的那个已经建好了。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import structlog
from sqlalchemy import Select, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from core.errors import ConflictError, NotFoundError
from engines.plan.schema import EtlPlanModel, RuntimeBudget, dump_plan
from models.datasource import DataSource, MetadataProfile
from models.enums import PipelineStatus, ProbeStatus
from models.pipeline import (
    EtlPlan,
    HoconArtifact,
    Pipeline,
    PipelineConversation,
    PipelineVersion,
    QualityContract,
    RuntimeSupervisionContract,
)
from services.pipeline.parts import quality_contract_body, supervision_body

log = structlog.get_logger(__name__)

#: 版本号并发冲突的重试次数上限（be-12 §5.2）。超过就抛 409 让用户重试——
#: 无限重试会掩盖「有人在大批量并发生成」这种真问题。
_VERSION_RETRY = 3


@dataclass(frozen=True, slots=True)
class ArtifactIds:
    """一组不可变制品的行 id。

    用 dataclass 而不是裸 dict：这几个 id 顺序相近、类型相同（都是 int），
    传 dict 时 ``ids["hocon_artifact_id"]`` 与 ``ids["etl_plan_id"]`` 写反了
    不会有任何报错，直到执行阶段才发现渲染出来的配置对不上。
    """

    etl_plan_id: int
    hocon_artifact_id: int
    quality_contract_id: int
    supervision_contract_id: int
    source_metadata_profile_id: int | None = None


# ---------------------------------------------------------------- 读


async def load_pipeline(*, session: AsyncSession, tenant_id: int, pipeline_id: int) -> Pipeline:
    """按 id 取流程，**强制带 tenant_id**（CLAUDE.md §4.7）。

    跨租户一律做成 404 而不是 403：403 等于告诉对方「这个 id 存在，只是不归你」，
    而 id 是自增的，那是一条可枚举的信息泄漏。
    """
    row = await session.scalar(
        select(Pipeline).where(Pipeline.id == pipeline_id, Pipeline.tenant_id == tenant_id)
    )
    if row is None:
        raise NotFoundError(f"pipeline {pipeline_id} 不存在")
    return row


async def list_pipelines(
    *, session: AsyncSession, tenant_id: int, project_id: int | None, offset: int, limit: int
) -> tuple[list[Pipeline], int]:
    base: Select[Any] = select(Pipeline).where(Pipeline.tenant_id == tenant_id)
    if project_id is not None:
        base = base.where(Pipeline.project_id == project_id)
    total = await session.scalar(select(func.count()).select_from(base.subquery())) or 0
    rows = (await session.scalars(base.order_by(Pipeline.id.desc()).offset(offset).limit(limit))).all()
    return list(rows), int(total)


async def list_versions(
    *, session: AsyncSession, tenant_id: int, pipeline_id: int, frozen_only: bool = True
) -> list[PipelineVersion]:
    """列出某个 pipeline 的版本。

    ``frozen_only`` 默认 True：**候选行不是版本历史**，它是正在被编辑的草案。
    把它列进「版本」里会让用户看到一条「还没审批就被称作 v3」的记录，
    也会让「回滚到上一版」这类操作指向一个从未生效过的制品。
    """
    stmt = (
        select(PipelineVersion)
        .join(Pipeline, Pipeline.id == PipelineVersion.pipeline_id)
        .where(PipelineVersion.pipeline_id == pipeline_id, Pipeline.tenant_id == tenant_id)
    )
    if frozen_only:
        stmt = stmt.where(PipelineVersion.frozen_at.is_not(None))
    rows = (await session.scalars(stmt.order_by(PipelineVersion.version_no.desc()))).all()
    return list(rows)


async def load_version(*, session: AsyncSession, tenant_id: int, version_id: int) -> PipelineVersion:
    """按 id 取版本行（候选也是版本行，所以这条也能取到草案）。"""
    row = await session.scalar(
        select(PipelineVersion)
        .join(Pipeline, Pipeline.id == PipelineVersion.pipeline_id)
        .where(PipelineVersion.id == version_id, Pipeline.tenant_id == tenant_id)
    )
    if row is None:
        raise NotFoundError(f"pipeline_version {version_id} 不存在")
    return row


async def latest_profile(
    *, session: AsyncSession, tenant_id: int, data_source_id: int
) -> MetadataProfile | None:
    """取数据源最新一份**就绪**的探查档案。

    只认 ``ready``：``probing`` 的快照还是空的，用它当权威会得出
    「这个库一张表都没有」——一个会把所有字段判成不存在的结论。
    """
    return await session.scalar(
        select(MetadataProfile)
        .join(DataSource, DataSource.id == MetadataProfile.data_source_id)
        .where(
            MetadataProfile.data_source_id == data_source_id,
            DataSource.tenant_id == tenant_id,
            MetadataProfile.status == ProbeStatus.READY,
        )
        .order_by(MetadataProfile.version.desc())
        .limit(1)
    )


async def profile_digests(
    *, session: AsyncSession, tenant_id: int, data_source_ids: Iterable[int]
) -> dict[int, str]:
    """一批数据源 → ``{data_source_id: 最新就绪档案的 sha256}``。

    指纹的第五部分靠它重算（见 ``parts.source_metadata_sha256``）。**一次查询**按
    ``(data_source_id, version)`` 取每组最大版本，而不是每个源查一次：
    逐个查的写法在单源下永远看不出问题，多源时就是 N 次往返，
    而 N 由用户在界面上选了几张表决定。

    取不到档案的源**不出现在结果里**（不是空串）——调用方按「跳过」处理，
    与建门禁 ctx 时同一规则。
    """
    ids = sorted(set(data_source_ids))
    if not ids:
        return {}
    newest = (
        select(
            MetadataProfile.data_source_id.label("ds_id"),
            func.max(MetadataProfile.version).label("version"),
        )
        .join(DataSource, DataSource.id == MetadataProfile.data_source_id)
        .where(
            MetadataProfile.data_source_id.in_(ids),
            DataSource.tenant_id == tenant_id,
            MetadataProfile.status == ProbeStatus.READY,
        )
        .group_by(MetadataProfile.data_source_id)
        .subquery()
    )
    rows = await session.execute(
        select(MetadataProfile.data_source_id, MetadataProfile.sha256).join(
            newest,
            (MetadataProfile.data_source_id == newest.c.ds_id)
            & (MetadataProfile.version == newest.c.version),
        )
    )
    return {row[0]: row[1] for row in rows}


async def profile_summary(*, session: AsyncSession, tenant_id: int, data_source_id: int) -> dict[str, Any]:
    """档案概要（给工作流的参数完备性检查用）。"""
    profile = await latest_profile(session=session, tenant_id=tenant_id, data_source_id=data_source_id)
    if profile is None:
        return {"ready": False, "profile_id": None, "version": None, "sha256": "", "tables_count": 0}
    return {
        "ready": True,
        "profile_id": profile.id,
        "version": profile.version,
        "sha256": profile.sha256,
        "tables_count": profile.tables_count,
        "expires_at": profile.expires_at.isoformat() if profile.expires_at else None,
    }


async def conversation(
    *, session: AsyncSession, tenant_id: int, pipeline_id: int, limit: int = 200
) -> list[PipelineConversation]:
    rows = (
        await session.scalars(
            select(PipelineConversation)
            .where(
                PipelineConversation.tenant_id == tenant_id,
                PipelineConversation.pipeline_id == pipeline_id,
            )
            .order_by(PipelineConversation.id.asc())
            .limit(limit)
        )
    ).all()
    return list(rows)


# ---------------------------------------------------------------- 写


async def create_pipeline(
    *,
    session: AsyncSession,
    tenant_id: int,
    project_id: int,
    name: str,
    description: str,
    source_data_source_id: int,
    target_data_source_id: int,
    created_by: int | None,
) -> Pipeline:
    """建流程。``name`` 在项目内唯一，重名时**自动加序号**而不是报错。

    理由：名字是从一句人话里截出来的，用户第二次说「同步订单表」时，
    报「名字重复」等于逼他手写一个名字——而他要的是「再生成一次」。
    """
    name = await _unique_name(session=session, project_id=project_id, base=name)
    row = Pipeline(
        tenant_id=tenant_id,
        project_id=project_id,
        name=name,
        description=description,
        status=PipelineStatus.DRAFT,
        source_data_source_id=source_data_source_id,
        target_data_source_id=target_data_source_id,
        created_by=created_by,
    )
    session.add(row)
    await session.flush()
    return row


async def _unique_name(*, session: AsyncSession, project_id: int, base: str) -> str:
    stem = (base or "未命名同步作业").strip()[:100] or "未命名同步作业"
    taken = set(
        (
            await session.scalars(
                select(Pipeline.name).where(Pipeline.project_id == project_id, Pipeline.name.like(f"{stem}%"))
            )
        ).all()
    )
    if stem not in taken:
        return stem
    for suffix in range(2, 100):
        candidate = f"{stem} ({suffix})"
        if candidate not in taken:
            return candidate[:128]
    raise ConflictError("同名流程过多，请换一个名字")


async def next_version_no(*, session: AsyncSession, pipeline_id: int) -> int:
    """下一个版本号 = 现有最大 + 1（含候选行）。

    刻意**不**用自增序列：版本号要出现在前端标签里（「v3」）并且与
    ``(pipeline_id, version_no)`` 唯一约束配合表达「这是第几版」。
    序列在事务回滚时会留空洞，而版本号的空洞无法解释——
    「v2 去哪了」和「有人删过 v2」在序列下无法区分。
    """
    current = await session.scalar(
        select(func.max(PipelineVersion.version_no)).where(PipelineVersion.pipeline_id == pipeline_id)
    )
    return int(current or 0) + 1


async def save_artifacts(
    *,
    session: AsyncSession,
    tenant_id: int,
    pipeline_id: int,
    plan: EtlPlanModel,
    plan_valid: bool,
    validation_errors: list[dict[str, Any]],
    hocon_content: str,
    hocon_sha256: str,
    parser_version: str,
    hocon_meta: dict[str, Any],
) -> ArtifactIds:
    """插入一组**新的**不可变制品行，返回它们的 id。

    每次生成/每次自动修复都插新行、不复用旧行（CLAUDE.md §5.4）。
    代价是同一份方案来回改会产生若干「没人引用的历史行」——这是有意付的：
    「这一轮修复改了什么」在事后能被完整重建，而「原地更新制品」
    会让审批时看到的制品与执行时的制品可能不是同一份。
    """
    plan_row = EtlPlan(
        schema_version=plan.schema_version,
        plan=dump_plan(plan),
        valid=plan_valid,
        validation_errors=validation_errors,
    )
    hocon_row = HoconArtifact(
        content=hocon_content,
        sha256=hocon_sha256,
        parser_version=parser_version,
        validation_result=hocon_meta,
    )
    # 契约体的构造只在 `parts.py` 里有一份：这里拿它建行，指纹那边拿它算哈希。
    # 两处各写一份键名，就是指纹系统的第一次分叉（见 parts.py 的模块 docstring）
    quality_row = QualityContract(**quality_contract_body(plan, pipeline_id=pipeline_id))
    supervision_row = RuntimeSupervisionContract(**supervision_body(plan))
    session.add_all([plan_row, hocon_row, quality_row, supervision_row])
    await session.flush()

    profile_id = await _lead_profile_id(session=session, tenant_id=tenant_id, plan=plan)
    return ArtifactIds(
        etl_plan_id=plan_row.id,
        hocon_artifact_id=hocon_row.id,
        quality_contract_id=quality_row.id,
        supervision_contract_id=supervision_row.id,
        source_metadata_profile_id=profile_id,
    )


async def _lead_profile_id(*, session: AsyncSession, tenant_id: int, plan: EtlPlanModel) -> int | None:
    """制品绑定的档案 = **第一个源端**的档案。

    一个方案可能读多张表，而 ``pipeline_version`` 只有一个档案外键（既有表结构）。
    取第一个而不是「全部指纹拼一起」：后者需要改表，而「源端档案的任何变化都会
    让指纹失效」这条已经由指纹本身覆盖（指纹里含**全部**源表档案的 sha256，
    见 ``engines.fingerprint``）——外键只是给界面用的「绑定的是哪一份档案」。
    """
    if not plan.inputs:
        return None
    profile = await latest_profile(
        session=session, tenant_id=tenant_id, data_source_id=plan.inputs[0].data_source_id
    )
    return None if profile is None else profile.id


async def get_or_create_candidate(
    *,
    session: AsyncSession,
    tenant_id: int,
    project_id: int,
    pipeline_id: int,
    ids: ArtifactIds,
    sha256: str,
    generation_context: dict[str, Any],
    now: datetime,
) -> PipelineVersion:
    """取得当前候选行（没有就建），并把制品指向 ``ids``。

    ``now`` 由调用方注入：engines/workflow 不许自己取时间，而这里虽然允许取，
    统一注入能让「生成时间」与「账本时间」是同一个时刻，事后对时间线不会错开。
    """
    last_error: IntegrityError | None = None
    for attempt in range(_VERSION_RETRY):
        candidate = await _current_candidate(session=session, pipeline_id=pipeline_id)
        if candidate is not None:
            await replace_candidate_artifacts(
                session=session,
                candidate=candidate,
                ids=ids,
                sha256=sha256,
                generation_context=generation_context,
            )
            return candidate
        try:
            async with session.begin_nested():
                version_no = await next_version_no(session=session, pipeline_id=pipeline_id)
                candidate = PipelineVersion(
                    pipeline_id=pipeline_id,
                    version_no=version_no,
                    etl_plan_id=ids.etl_plan_id,
                    hocon_artifact_id=ids.hocon_artifact_id,
                    quality_contract_id=ids.quality_contract_id,
                    supervision_contract_id=ids.supervision_contract_id,
                    source_metadata_profile_id=ids.source_metadata_profile_id,
                    generation_context=generation_context,
                    sha256=sha256,
                    # 候选行的 frozen_at 必须是 NULL：它是「还没冻结」的唯一标记，
                    # 也是那条部分唯一索引的谓词
                    frozen_at=None,
                    note="",
                )
                session.add(candidate)
            return candidate
        except IntegrityError as exc:
            # 唯一约束撞了：说明另一个请求刚刚建好了候选（或抢走了同一个版本号）。
            # 回滚到保存点后**重新查一次**——见 be-12 §5.2，重试的全部意义就在这里。
            last_error = exc
            log.info(
                "pipeline_candidate_conflict_retry",
                pipeline_id=pipeline_id,
                tenant_id=tenant_id,
                attempt=attempt + 1,
            )
    raise ConflictError("该流程正被另一处生成占用，请稍后重试") from last_error


async def _current_candidate(*, session: AsyncSession, pipeline_id: int) -> PipelineVersion | None:
    return await session.scalar(
        select(PipelineVersion).where(
            PipelineVersion.pipeline_id == pipeline_id, PipelineVersion.frozen_at.is_(None)
        )
    )


async def replace_candidate_artifacts(
    *,
    session: AsyncSession,
    candidate: PipelineVersion,
    ids: ArtifactIds,
    sha256: str,
    generation_context: dict[str, Any],
) -> None:
    """把候选行指向一组新制品。

    只有候选行能走到这里：一旦 ``frozen_at`` 落值，ORM 守卫
    （``models/base.py`` 的 ``__freeze_column__``）会让这次 UPDATE 抛
    ``ImmutableViolationError``——**不需要在这里再判一次状态**，
    少一处判断就少一处「判漏了」的机会。
    """
    candidate.etl_plan_id = ids.etl_plan_id
    candidate.hocon_artifact_id = ids.hocon_artifact_id
    candidate.quality_contract_id = ids.quality_contract_id
    candidate.supervision_contract_id = ids.supervision_contract_id
    candidate.source_metadata_profile_id = ids.source_metadata_profile_id
    candidate.sha256 = sha256
    candidate.generation_context = generation_context
    await session.flush()


async def set_pipeline_status(*, session: AsyncSession, pipeline_id: int, status: PipelineStatus) -> None:
    row = await session.get(Pipeline, pipeline_id)
    if row is None:  # pragma: no cover - 调用方刚取过
        raise NotFoundError(f"pipeline {pipeline_id} 不存在")
    row.status = status
    await session.flush()


async def set_current_version(
    *, session: AsyncSession, pipeline_id: int, version_id: int, status: PipelineStatus
) -> None:
    """冻结时把 pipeline 指向该版本。``pipeline`` 是可变的，版本行不是。"""
    row = await session.get(Pipeline, pipeline_id)
    if row is None:  # pragma: no cover
        raise NotFoundError(f"pipeline {pipeline_id} 不存在")
    row.current_version_id = version_id
    row.status = status
    await session.flush()


async def append_message(
    *,
    session: AsyncSession,
    tenant_id: int,
    project_id: int,
    pipeline_id: int,
    role: str,
    content: str,
    message_kind: str,
    meta: dict[str, Any],
    created_by: int | None,
) -> PipelineConversation:
    """写一条对话消息。

    幂等由调用方保证（工作流恢复会重放节点）：本函数只负责落一行，
    所以元数据里必须带 ``thread_id`` + 节点名，事后能看出是不是重复写了。
    """
    row = PipelineConversation(
        tenant_id=tenant_id,
        project_id=project_id,
        pipeline_id=pipeline_id,
        role=role,
        content=content,
        message_kind=message_kind,
        meta=meta,
        created_by=created_by,
    )
    session.add(row)
    await session.flush()
    return row


async def artifact_rows(
    *, session: AsyncSession, version: PipelineVersion
) -> tuple[EtlPlan, HoconArtifact, QualityContract, RuntimeSupervisionContract]:
    """取版本指向的四份制品。**一次取全**：读接口几乎从不只用其中一份。"""
    plan = await session.get(EtlPlan, version.etl_plan_id)
    hocon = await session.get(HoconArtifact, version.hocon_artifact_id)
    quality = await session.get(QualityContract, version.quality_contract_id)
    supervision = await session.get(RuntimeSupervisionContract, version.supervision_contract_id)
    missing = [
        name
        for name, row in (
            ("etl_plan", plan),
            ("hocon_artifact", hocon),
            ("quality_contract", quality),
            ("runtime_supervision_contract", supervision),
        )
        if row is None
    ]
    if missing:
        # 制品是 RESTRICT 外键，理论上删不掉；真出现说明有人绕过 ORM 动了库，
        # 此时报 500 级错误是对的——静默回一个空方案会让界面显示「方案为空」，
        # 把一次数据损坏伪装成一次业务结果
        raise ConflictError(f"版本 {version.id} 的制品不完整，缺失：{missing}")
    return plan, hocon, quality, supervision


def budget_of(supervision: RuntimeSupervisionContract) -> RuntimeBudget:
    """监督契约 → Plan 预算（回读时的反向映射，类型也从 float 收成 Decimal）。"""
    return RuntimeBudget(
        max_read_rows=supervision.max_read_rows,
        max_write_bytes=supervision.max_write_bytes,
        max_duration_sec=supervision.max_duration_sec,
        max_amplification_ratio=supervision.max_amplification_ratio,
        max_reject_rate=supervision.max_reject_rate,
        warn_threshold_pct=supervision.warn_threshold_pct,
    )
