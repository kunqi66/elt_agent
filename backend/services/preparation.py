"""准备单与发起准备（be-13）。

**Prepare 是零副作用的**：本模块只写控制面四张表（preparation / evidence_event /
pipeline.status），不签发 Capability、不建 execution_run、不投 outbox_message、
不调用任何 ``ToolName``。这条不是风格问题——「审批之前什么都没发生过」是四眼能成立的前提，
一旦 Prepare 动了数据面，被拒的那次准备就已经产生了不可撤销的后果。

三条职责边界：

1. **判定一律走 ``services.policy.decide_for_project``**（经由 ``build_gate_context``）。
   直接调 ``engines.policy.decide`` 会漏掉预算默认值，把每一项预算都判成超限 0。
2. **资源范围与敏感级别的定义只有一份**：与门禁共用 ``build_gate_context``，
   于是「门禁判定时的 scope」与「准备单上写着的 scope」不可能分叉。
3. **指纹只有一个规范化入口**：``core.ids.fingerprint``（内部走 be-12 的 canonical_bytes），
   禁止在本模块手写 ``json.dumps``。

本层禁止 ``import fastapi``（CLAUDE.md §4.2）。
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from structlog import get_logger

from core.config import settings
from core.errors import (
    ApprovalIncompleteError,
    ConflictError,
    FingerprintMismatchError,
    MetadataProfileExpiredError,
    NotFoundError,
    PolicyBlockedError,
    PreparationExpiredError,
    RoleRequiredError,
    ValidationError,
)
from core.ids import as_utc, fingerprint, utcnow
from engines.plan.schema import EtlPlanModel
from engines.policy import has_role
from harness.ledger import EvidenceLedger, LedgerEvent
from models.datasource import DataSource, MetadataProfile, SecretReference
from models.enums import (
    ApprovalDecisionType,
    ApprovalSlot,
    EvidenceEventType,
    PipelineStatus,
    PreparationStatus,
    ProbeStatus,
    RoleName,
    ToolName,
)
from models.evidence import EvidenceEvent
from models.governance import ApprovalDecision, Preparation
from models.pipeline import (
    Pipeline,
    PipelineVersion,
    QualityContract,
    RuntimeSupervisionContract,
)
from models.tenant import AppUser, Project, ProjectMember
from schemas.auth import SubjectContext
from schemas.preparation import (
    ApprovalDecisionRead,
    CommittableFacts,
    ExecutionBudgetRead,
    PreparationCreateRequest,
    PreparationDetail,
    PreparationStatusView,
    PreparationSummary,
    PreparationTimelineItem,
    ResourceReadRef,
    ResourceScopeRead,
    ResourceWriteRef,
    RiskReasonRead,
    RollbackPlanRead,
    RollbackStep,
)
from services.pipeline import repository
from services.pipeline.artifacts import source_metadata_digest
from services.pipeline.gating import build_gate_context
from services.project import active_roles
from workflow.transitions import assert_transition

log = get_logger(__name__)

_ledger = EvidenceLedger()

#: 谁能发起准备。**只有 maker**（be-13 §5.1 步骤 1）：
#: operator 能冻结（制品可执行性由门禁负责），但「提出这次数据变更」的责任人是 maker，
#: 而四眼拦截的比对面正是 ``preparation.created_by``——放宽发起人等于放宽四眼的定义。
_CREATE_ROLES: frozenset[RoleName] = frozenset({RoleName.MAKER})

#: 可以继续接受审批的状态。终态（approved/rejected/expired/superseded）一律不再接受决定。
_LIVE_STATUSES: tuple[PreparationStatus, ...] = (
    PreparationStatus.PENDING,
    PreparationStatus.PARTIALLY_APPROVED,
)

#: 目标库标识符长度上限（MySQL 64 / Doris 同）。影子表名超长会被目标库**静默截断**
#: 或直接报错，而截断后的名字与 ``resource_scope`` 里审批人看到的名字不一致——
#: 那是「批的是 A、写的是 B」。
_MAX_IDENTIFIER = 64


# ---------------------------------------------------------------- 影子表命名


def shadow_table_name(table: str, version_no: int) -> str:
    """影子表名：``<表>__shadow_v<版本号>``。**全仓库唯一的一份定义**。

    带上版本号而不是只加后缀：同一个正式表会被多个版本先后同步，
    而「清理失败残留的影子表」必须能一眼看出它属于哪一版——只加 ``__shadow``
    会让两个版本的影子表撞名，第二次执行直接写进上一次没清干净的残留。

    be-18（原子发布）与 be-15（SeaTunnel 物化）必须调本函数，不要各拼一次：
    审批人看到的 ``resource_scope.writes[*].shadow_table`` 与执行期真正建的表
    一旦不是同一个名字，等于「批的和写的不是一张表」，而且不报任何错。
    """
    suffix = f"__shadow_v{version_no}"
    if len(table) + len(suffix) <= _MAX_IDENTIFIER:
        return f"{table}{suffix}"
    # 超长时截断表名并补 8 位哈希：截断必须可逆地唯一，否则两张长表名会拼出同一个影子表名
    digest = hashlib.sha256(table.encode("utf-8")).hexdigest()[:8]
    keep = _MAX_IDENTIFIER - len(suffix) - len(digest) - 2
    return f"{table[:keep]}__{digest}{suffix}"


# ---------------------------------------------------------------- 载入


async def load_preparation(session: AsyncSession, *, tenant_id: int, preparation_id: int) -> Preparation:
    """按 id 取准备单，**强制带 tenant_id**（CLAUDE.md §4.7）。跨租户一律 404。"""
    row = await session.scalar(
        select(Preparation).where(
            Preparation.id == preparation_id, Preparation.tenant_id == tenant_id
        )
    )
    if row is None:
        raise NotFoundError(f"准备单 {preparation_id} 不存在")
    return row


def _at(value: datetime | None) -> datetime | None:
    """DTO 出口的时间戳归一化：**从库里读出来的时间一律按 UTC 补时区**。

    ``core.ids.as_utc`` 在生产（PG ``timestamptz``）是空操作，它是为 SQLite 存在的：
    那里读回来是裸值，于是「拿库里的时间与 ``utcnow()`` 比」会 TypeError，
    而「拿两个都是从库里读出的裸值再排序」会通过——**同一张单子的时间戳一半带时区
    一半不带**，症状是排序偶发错乱、或者某条路径崩而另一条不崩。
    所以出口统一：凡进 DTO / 进比较的时间都过这一层，别只修崩掉的那一处。
    """
    return None if value is None else as_utc(value)


async def decisions_for(
    session: AsyncSession, *, preparation_ids: Sequence[int]
) -> dict[int, dict[ApprovalSlot, ApprovalDecision]]:
    """一批准备单的决定：``准备单 id → {槽: 决定行}``。**一次 SELECT**。

    列表页用它替代 ``Preparation.decisions`` 关系集合：关系集合要么是会话快照
    （漏掉同一会话里刚落的那条），要么得靠 ``populate_existing`` 去覆盖它——
    而那种「读的人得记得加一个选项才正确」的正确性，迟早会有人漏加。
    一次 IN 查询没有这个余地，且顺带避开 selectin 的 N+1。
    """
    if not preparation_ids:
        return {}
    rows = (
        await session.scalars(
            select(ApprovalDecision).where(ApprovalDecision.preparation_id.in_(list(preparation_ids)))
        )
    ).all()
    grouped: dict[int, dict[ApprovalSlot, ApprovalDecision]] = {}
    for row in rows:
        grouped.setdefault(row.preparation_id, {})[row.slot] = row
    return grouped


async def decisions_of(
    session: AsyncSession, *, preparation_id: int
) -> dict[ApprovalSlot, ApprovalDecision]:
    """这张准备单已有的审批决定：``槽 → 决定行``。

    **全模块唯一的取法**（``services/approval.py`` 也调它），单张单的短名字，
    实现归 ``decisions_for``。
    """
    grouped = await decisions_for(session, preparation_ids=[preparation_id])
    return grouped.get(preparation_id, {})


async def _load_project(session: AsyncSession, *, tenant_id: int, project_id: int) -> Project:
    row = await session.scalar(
        select(Project).where(Project.id == project_id, Project.tenant_id == tenant_id)
    )
    if row is None:
        raise NotFoundError(f"项目 {project_id} 不存在")
    return row


async def _load_sources(
    session: AsyncSession, *, tenant_id: int, ids: set[int]
) -> dict[int, DataSource]:
    """一次取全本次方案涉及的数据源（``IN ()`` 是语法错误，空集合要提前返回）。"""
    if not ids:
        return {}
    rows = (
        await session.scalars(
            select(DataSource).where(
                DataSource.id.in_(sorted(ids)), DataSource.tenant_id == tenant_id
            )
        )
    ).all()
    return {row.id: row for row in rows}


async def _secret_entries(
    session: AsyncSession, *, tenant_id: int, sources: dict[int, DataSource]
) -> list[dict[str, Any]]:
    """收集本次作业会用到的**凭据引用**（只有路径/版本/字段名，**永远没有值**）。

    ``version`` 进 ``input_fingerprint``：Vault 里轮转过凭据之后，即使表结构没变，
    这次执行面对的秘密也已经不是审批时那一个了——旧审批必须失效。
    """
    ids = sorted({ds.secret_ref_id for ds in sources.values() if ds.secret_ref_id is not None})
    if not ids:
        return []
    rows = (
        await session.scalars(
            select(SecretReference).where(
                SecretReference.id.in_(ids), SecretReference.tenant_id == tenant_id
            )
        )
    ).all()
    by_id = {row.id: row for row in rows}
    entries: list[dict[str, Any]] = []
    for ds_id, ds in sorted(sources.items()):
        ref = by_id.get(ds.secret_ref_id) if ds.secret_ref_id is not None else None
        if ref is None:
            continue
        entries.append(
            {
                "data_source_id": ds_id,
                "secret_reference_id": ref.id,
                "provider": ref.provider,
                "path": ref.path,
                "version": ref.version,
                "key_names": list(ref.key_names or []),
            }
        )
    return entries


def _plan_model(row: Any) -> EtlPlanModel:
    return EtlPlanModel.model_validate(row.plan)


async def _require_fresh_profile(
    session: AsyncSession, *, version: PipelineVersion, now: datetime
) -> MetadataProfile | None:
    """版本钉住的那份源端档案必须**还在、还就绪、（若设了有效期）还没过期**。

    三者缺一都报 ``METADATA_PROFILE_EXPIRED`` 而不是放行：这份档案是门禁判过、
    生成时算进指纹的那一份。它消失（被删/被重探成失败）之后，
    「审批人看到的表结构」在库里已经查不到了，此时放行等于凭记忆执行。

    ``expires_at`` 为 NULL 视为**不设有效期**，不拦——注意这条闸目前实际上是空转的：
    探查侧（``services/metadata.py``）从不写这个列，所以库里全是 NULL。
    把它当「已过期」会拦下每一份准备单；把它当「永不过期」则是当前事实。
    真要让档案 TTL 生效，得先在探查成功时写上 ``now + PROFILE_TTL``（已登记进 spec §10）。

    ``source_metadata_profile_id`` 为空（版本生成时一个源端都没探查成功）时不拦：
    没有钉住的东西可比对。此时指纹仍然会因为该部分为空而对不上——
    拦住的责任在指纹那一层，不在这里造第二个判据。
    """
    if version.source_metadata_profile_id is None:
        log.warning(
            "preparation_profile_not_pinned",
            pipeline_version_id=version.id,
            reason="版本未钉住源端档案，跳过档案新鲜度校验",
        )
        return None
    profile = await session.get(MetadataProfile, version.source_metadata_profile_id)
    if profile is None or profile.status is not ProbeStatus.READY:
        raise MetadataProfileExpiredError(
            "源端元数据档案已不可用（被删除或探查未就绪），请重新探查源表后再发起准备"
        )
    #: ``as_utc``：SQLite 读回来的时间是裸值，与带时区的 ``now`` 直接比较会 TypeError
    if profile.expires_at is not None and as_utc(profile.expires_at) <= now:
        raise MetadataProfileExpiredError("源端元数据档案已过期，请重新探查源表后再发起准备")
    return profile


# ---------------------------------------------------------------- 组装准备单内容


def _build_resource_scope(
    *, plan: EtlPlanModel, sources: dict[int, DataSource], version_no: int, secrets: list[dict[str, Any]]
) -> dict[str, Any]:
    """本次作业会碰到的表与凭据引用。

    ``secrets`` 留在库列里（审批人要能看到「这次会动哪些凭据」），但**不进读 DTO**：
    ``schemas/preparation.py`` 的 ``ResourceScopeRead`` 只有 reads / writes。
    凭据清单是 be-14 物化时用的，前端拿到没有用途，少传一处就少一处泄露面。
    """
    reads = [
        {
            "data_source_id": item.data_source_id,
            "database": item.schema_name,
            "table": item.table,
        }
        for item in plan.inputs
    ]
    writes = [
        {
            "data_source_id": item.data_source_id,
            # 目标端的库名键是 ``database``（源端才是 ``schema_name``，见
            # ``engines/plan/schema.py`` 的 PlanOutput / PlanInput）。
            # 写错的那个属性名不是「取到空值」而是 AttributeError——发起准备直接 500。
            "database": item.database,
            "table": item.table,
            # 写形态是常量而不是可选项：``shadow_then_swap`` 是系统唯一允许的写形态，
            # 直接写正式表在 PDP 里是一条 P0（PRD §15 已冻结）。
            "mode": "shadow_then_swap",
            "formal_table": item.table,
            "shadow_table": shadow_table_name(item.table, version_no),
        }
        for item in plan.outputs
    ]
    return {"reads": reads, "writes": writes, "secrets": secrets}


def _budget_snapshot(supervision: RuntimeSupervisionContract) -> dict[str, Any]:
    """生效预算 = 版本的运行监督契约快照（be-13 §5.1 步骤 8）。

    刻意用**监督契约**而不是 PDP 返回的 ``decision.budget``：后者是策略上限
    （可以被策略表调低的那条线），前者是这份制品真正会带进执行器的阈值。
    审批人要看的是「这次跑起来会在什么数值上被硬中断」——那是监督契约。
    PDP 的上限留在 ``policy.evaluated`` 事件的 payload 里，两者都能追溯到。
    """
    return {
        "max_read_rows": supervision.max_read_rows,
        "max_write_bytes": supervision.max_write_bytes,
        "max_duration_sec": supervision.max_duration_sec,
        "max_amplification_ratio": str(supervision.max_amplification_ratio),
        "max_reject_rate": str(supervision.max_reject_rate),
        "warn_threshold_pct": supervision.warn_threshold_pct,
    }


def _rollback_plan(*, plan: EtlPlanModel, version_no: int) -> dict[str, Any]:
    """回滚方案：影子表 + 原子改名，失败就丢掉影子表。

    形状以**前端**（``frontend/src/types/approval.ts`` 的 ``RollbackPlan``）为准：
    ``{steps, shadow_cleanup, restore_scope}``；两个表名作为额外键留在库列里给 be-14，
    读 DTO 不含它们（``schemas/preparation.py`` 的差异登记里写了原因）。
    """
    steps: list[dict[str, Any]] = []
    shadow_tables: list[str] = []
    formal_tables: list[str] = []
    for index, output in enumerate(plan.outputs, start=1):
        shadow = shadow_table_name(output.table, version_no)
        shadow_tables.append(shadow)
        formal_tables.append(output.table)
        steps.append(
            {
                "order": index,
                "action": "write_shadow",
                "tool": ToolName.SEATUNNEL_SUBMIT.value,
                "description": f"全量写入影子表 {shadow}，正式表 {output.table} 不动",
            }
        )
    order = len(steps) + 1
    steps.append(
        {
            "order": order,
            "action": "publish_swap",
            "tool": ToolName.TARGET_PUBLISH_SWAP.value,
            "description": "质量达标后原子改名：影子表 → 正式表（失败即未发布）",
        }
    )
    steps.append(
        {
            "order": order + 1,
            "action": "rollback_cleanup",
            "tool": ToolName.TARGET_ROLLBACK_CLEANUP.value,
            "description": "失败或超限时清理影子表，正式表保持原样",
        }
    )
    return {
        "steps": steps,
        # 失败路径只清影子表：正式表在 Swap 之前从未被写过，所以「回滚」不是把数据改回去，
        # 而是「什么都没发布」。把这一点写进计划本身，是为了让审批人一眼看到风险边界。
        "shadow_cleanup": True,
        "restore_scope": "shadow_only",
        "shadow_tables": shadow_tables,
        "formal_tables": formal_tables,
    }


def _risk_reasons(decision: Any) -> list[dict[str, Any]]:
    """PDP 命中项 → 前端要的四元组。

    只回 ``decision.reasons`` 那些人话串会让前端无法把理由链回规则编号，
    等于在审批页上丢掉审计线索（``schemas/preparation.py`` 的差异登记）。
    """
    reasons: list[dict[str, Any]] = []
    for hit in decision.hits:
        detail = "；".join(f"{key}={value}" for key, value in sorted(hit.evidence.items()))
        reasons.append(
            {
                "code": hit.code,
                "level": hit.level.value,
                "title": hit.title,
                "detail": detail,
            }
        )
    if not reasons:
        # 一条规则都没命中也要有一行，否则前端显示「无风险理由」，
        # 读者会以为判定没跑（实际是「判定跑了，没有任何规则命中」）
        reasons.append(
            {
                "code": "NO_RULE_HIT",
                "level": decision.level.value,
                "title": "未命中任何风险规则",
                "detail": f"按基线规则集判定为 {decision.level.value}",
            }
        )
    return reasons


def _fingerprint_payload(
    *,
    source_metadata_sha256: str,
    sources: dict[int, DataSource],
    secrets: list[dict[str, Any]],
    quality: QualityContract,
    supervision: RuntimeSupervisionContract,
) -> dict[str, Any]:
    """``input_fingerprint`` 的六个部分（be-13 §5.1 步骤 4）。

    它回答的是「**审批的时候，输入长什么样**」。所以进指纹的必须是会变的东西：
    源表结构（档案摘要）、连接定位（endpoint，**不含凭据**）、凭据引用的版本、
    清洗规则、运行预算。任何一项变了，旧审批就该失效。

    ``endpoint`` 里放的是 host/port/database 这类定位信息——``schemas/datasource.py``
    的校验器已经拒绝含 password/token 的 payload，所以这里不会把口令拼进指纹
    （指纹会落库、会进证据账本，那是最不该出现口令的地方）。
    """
    return {
        # 版本号是**必填**的：给指纹加一层「拿什么规则算的」。
        # 少了它，将来往 payload 里加一个字段，新旧指纹会静默地长得一样。
        "v": 1,
        "source_metadata_sha256": source_metadata_sha256,
        "sources": [
            {
                "data_source_id": ds_id,
                "type": str(ds.type),
                "env": str(ds.env),
                "endpoint": ds.endpoint,
            }
            for ds_id, ds in sorted(sources.items())
        ],
        "secrets": [
            {
                "secret_reference_id": entry["secret_reference_id"],
                "path": entry["path"],
                "version": entry["version"],
            }
            for entry in secrets
        ],
        "quality": {
            "rules": quality.rules,
            "error_table_target": quality.error_table_target,
            # Decimal → str：浮点在不同平台/版本上 JSON 化后的文本可能不同，
            # 而指纹要求逐字节可复现（与 schemas 层「比例字段序列化成字符串」同一个原因）
            "reject_rate_budget": str(quality.reject_rate_budget),
        },
        "supervision": _budget_snapshot(supervision),
    }


async def current_input_fingerprint(
    session: AsyncSession,
    *,
    tenant_id: int,
    version: PipelineVersion,
    plan: EtlPlanModel,
    quality: QualityContract,
    supervision: RuntimeSupervisionContract,
) -> str:
    """**重算**当前输入指纹（审批与 Commit 的比对基准）。

    与 ``create_preparation`` 里的计算共用同一个 ``_fingerprint_payload`` +
    ``core.ids.fingerprint``：两处各写一遍的结果是「刚发起的单立刻报指纹不符」。
    """
    ds_ids = {item.data_source_id for item in plan.inputs} | {
        item.data_source_id for item in plan.outputs
    }
    sources = await _load_sources(session, tenant_id=tenant_id, ids=ds_ids)
    secrets = await _secret_entries(session, tenant_id=tenant_id, sources=sources)
    digest = await source_metadata_digest(session=session, tenant_id=tenant_id, plan=plan)
    return fingerprint(
        _fingerprint_payload(
            source_metadata_sha256=digest,
            sources=sources,
            secrets=secrets,
            quality=quality,
            supervision=supervision,
        )
    )


# ---------------------------------------------------------------- 发起准备


async def create_preparation(
    session: AsyncSession,
    ctx: SubjectContext,
    payload: PreparationCreateRequest,
) -> PreparationDetail:
    """发起准备（be-13 §5.1）。**零外部副作用**，见模块 docstring。"""
    now = utcnow()

    version = await repository.load_version(
        session=session, tenant_id=ctx.tenant_id, version_id=payload.pipeline_version_id
    )
    pipeline = await repository.load_pipeline(
        session=session, tenant_id=ctx.tenant_id, pipeline_id=version.pipeline_id
    )
    if payload.project_id is not None and payload.project_id != pipeline.project_id:
        # 项目的唯一事实来源是 pipeline.project_id；客户端说是哪个项目不算数，
        # 只用来发现「前端拿错了 pipeline_version_id」这类错配
        raise ValidationError("project_id 与版本所属项目不一致，请刷新后重试")
    project = await _load_project(session, tenant_id=ctx.tenant_id, project_id=pipeline.project_id)

    held = await _roles(session, project_id=project.id, user_id=ctx.user_id, now=now)
    if not any(has_role(held, role) for role in _CREATE_ROLES):
        raise RoleRequiredError("发起准备需要 maker 职责槽")

    if version.frozen_at is None:
        raise ConflictError("该版本尚未冻结，不能发起准备（未冻结的版本是草案，随时会被覆盖）")

    # 状态机守卫：只有「冻结后的版本」能进入准备。用转移表判定而不是自己写 if，
    # 是为了让「哪些状态能准备」只有一处定义（be-09 拥有出边）。
    await _enter_preparing(session=session, pipeline=pipeline, ctx=ctx)

    plan_row, hocon_row, quality_row, supervision_row = await repository.artifact_rows(
        session=session, version=version
    )
    await _require_fresh_profile(session=session, version=version, now=now)
    plan = _plan_model(plan_row)

    # 与门禁**共用同一份上下文构造**：资源范围、敏感级别、环境、PDP 判定都出自这里，
    # 因此「门禁判过的」与「准备单上写的」不可能分叉（本模块 docstring 第 2 条）。
    context = await build_gate_context(
        session=session,
        subject=ctx,
        project_id=project.id,
        plan=plan,
        hocon_content=hocon_row.content,
        now=now,
    )
    decision = context.policy_decision
    if decision is None:  # pragma: no cover - build_gate_context 必然给出判定
        raise ConflictError("策略判定缺失，无法发起准备")

    if decision.blocked:
        # 先入账再抛：被拒的这次尝试本身是证据（PRD §12 要求 P0 阻断留痕）
        await _ledger.append(
            session,
            LedgerEvent(
                tenant_id=ctx.tenant_id,
                project_id=project.id,
                actor_id=ctx.user_id,
                event_type=EvidenceEventType.POLICY_BLOCKED,
                subject_type="pipeline_version",
                subject_id=str(version.id),
                artifact_hash=version.sha256,
                payload={
                    "risk_level": decision.level.value,
                    "reason_codes": [hit.code for hit in decision.hits],
                    "stage": "prepare",
                },
                occurred_at=now,
            ),
        )
        await session.commit()
        raise PolicyBlockedError(
            f"策略判定为 {decision.level.value}（高风险），已阻断；不允许发起准备。"
            f"命中规则：{'、'.join(hit.code for hit in decision.hits) or '（无）'}"
        )

    ds_ids = {item.data_source_id for item in plan.inputs} | {
        item.data_source_id for item in plan.outputs
    }
    sources = await _load_sources(session, tenant_id=ctx.tenant_id, ids=ds_ids)
    secrets = await _secret_entries(session, tenant_id=ctx.tenant_id, sources=sources)
    input_fp = fingerprint(
        _fingerprint_payload(
            source_metadata_sha256=context.source_metadata_sha256,
            sources=sources,
            secrets=secrets,
            quality=quality_row,
            supervision=supervision_row,
        )
    )

    row = Preparation(
        tenant_id=ctx.tenant_id,
        project_id=project.id,
        pipeline_version_id=version.id,
        input_fingerprint=input_fp,
        # 制品指纹取自不可变版本行，**不接受调用方提交**（允许自证清白等于没有信任锚）
        artifact_sha256=version.sha256,
        resource_scope=_build_resource_scope(
            plan=plan, sources=sources, version_no=version.version_no, secrets=secrets
        ),
        sensitive_level=context.level,
        budget=_budget_snapshot(supervision_row),
        rollback_plan=_rollback_plan(plan=plan, version_no=version.version_no),
        # 环境取门禁判定的那一份（源/目标数据源 env 的最高档），而不是 project.env：
        # 「判定的环境」与「记录的环境」必须是同一个值，否则审批页显示 dev、判定按 prod。
        environment=context.env,
        risk_level=decision.level,
        risk_reasons=_risk_reasons(decision),
        required_slots=[slot.value for slot in decision.required_slots],
        status=PreparationStatus.PENDING,
        created_by=ctx.user_id,
        frozen_at=now,
        expires_at=now + timedelta(hours=settings.PREPARATION_TTL_HOURS),
    )
    session.add(row)
    await session.flush()

    await supersede_previous(
        session,
        tenant_id=ctx.tenant_id,
        pipeline_id=pipeline.id,
        keep_preparation_id=row.id,
        actor_id=ctx.user_id,
        now=now,
    )

    common = {
        "tenant_id": ctx.tenant_id,
        "project_id": project.id,
        "actor_id": ctx.user_id,
        "subject_type": "preparation",
        "subject_id": str(row.id),
        "artifact_hash": version.sha256,
        "occurred_at": now,
    }
    await _ledger.append(
        session,
        LedgerEvent(
            event_type=EvidenceEventType.PREPARATION_CREATED,
            payload={
                "pipeline_id": pipeline.id,
                "pipeline_version_id": version.id,
                "version_no": version.version_no,
                "risk_level": decision.level.value,
                "required_slots": [slot.value for slot in decision.required_slots],
                "expires_at": row.expires_at.isoformat() if row.expires_at else None,
            },
            **common,
        ),
    )
    await _ledger.append(
        session,
        LedgerEvent(
            event_type=EvidenceEventType.POLICY_EVALUATED,
            payload={
                "stage": "prepare",
                "risk_level": decision.level.value,
                "policy_version": decision.policy_version,
                "required_slots": [slot.value for slot in decision.required_slots],
                "hit_codes": [hit.code for hit in decision.hits],
                # PDP 给出的策略上限（与监督契约快照区分：那是执行期真正会用的阈值）
                "budget_ceiling": decision.budget.model_dump(mode="json"),
            },
            **common,
        ),
    )

    log.info(
        "preparation_created",
        preparation_id=row.id,
        pipeline_id=pipeline.id,
        pipeline_version_id=version.id,
        risk_level=decision.level.value,
        required_slot_count=len(decision.required_slots),
        request_id=ctx.request_id,
    )
    await session.commit()
    return await get_preparation(session, ctx, row.id)


async def _enter_preparing(
    *, session: AsyncSession, pipeline: Pipeline, ctx: SubjectContext
) -> None:
    """把 pipeline 推进到「等待审批」。

    转移表里的边是 ``version_frozen --prepare--> preparing``；``preparing`` 是**瞬时态**
    （只在这个请求里存在），提交时落的是 ``pending_approval``。用 ``assert_transition``
    校验合法而不是自己写 if：非法状态（如已在跑、已通过）要报 409，且这条判定只有一处定义。
    """
    assert_transition(pipeline.status, "prepare", pipeline_id=pipeline.id, request_id=ctx.request_id)
    await repository.set_pipeline_status(
        session=session, pipeline_id=pipeline.id, status=PipelineStatus.PENDING_APPROVAL
    )


async def supersede_previous(
    session: AsyncSession,
    *,
    tenant_id: int,
    pipeline_id: int,
    keep_preparation_id: int,
    actor_id: int | None = None,
    now: datetime | None = None,
) -> list[int]:
    """把同一 pipeline 上仍活跃的旧准备单标记为 ``superseded``，返回被取代的 id 列表。

    **不物理删除**：旧单是「曾经有人批过这件事」的证据，删掉它等于抹掉一段审计历史。
    也不动它的 ``approval_decision`` 行——那些决定属于那个指纹下的那个制品。

    逐行赋值而不是批量 ``update()``：``Preparation`` 的不可变守卫挂在 ORM 的
    ``before_flush`` 上，批量 UPDATE 绕过它——而绕过守卫的代码就是「哪天有人顺手多改一列」
    时唯一不会被拦下的那条路径（CLAUDE.md §5.4）。
    """
    moment = now or utcnow()
    rows = (
        await session.scalars(
            select(Preparation)
            .join(PipelineVersion, PipelineVersion.id == Preparation.pipeline_version_id)
            .where(
                Preparation.tenant_id == tenant_id,
                # 版本行**没有** project_id（归属由 ``pipeline`` 表达，见
                # ``models/pipeline.py``）：范围是靠连接过去的 pipeline_id 收窄的。
                # 想在这里再补一条 project 相等，只会得到一个 AttributeError——
                # 而它的症状是「重发准备单 500」，看起来像重发这个功能坏了。
                PipelineVersion.pipeline_id == pipeline_id,
                Preparation.status.in_([status.value for status in _LIVE_STATUSES]),
                Preparation.id != keep_preparation_id,
            )
        )
    ).all()
    superseded: list[int] = []
    for row in rows:
        row.status = PreparationStatus.SUPERSEDED
        superseded.append(row.id)
        await _ledger.append(
            session,
            LedgerEvent(
                tenant_id=tenant_id,
                project_id=row.project_id,
                actor_id=actor_id,
                event_type=EvidenceEventType.PREPARATION_SUPERSEDED,
                subject_type="preparation",
                subject_id=str(row.id),
                artifact_hash=row.artifact_sha256,
                payload={"replaced_by": keep_preparation_id, "reason": "新准备单发起"},
                occurred_at=moment,
            ),
        )
    if superseded:
        await session.flush()
    return superseded


async def expire_due_preparations(
    session: AsyncSession, *, tenant_id: int | None = None, now: datetime | None = None
) -> int:
    """把过期未批完的准备单标记为 ``expired``（be-13 §5.3）。

    定时任务调用（幂等）：重复跑不会再改已终态的行。**自己提交事务**——
    调用方是 beat 触发的批处理，没有外层事务可借（与 service 写入口一律自己 commit 同一口径）。
    """
    moment = now or utcnow()
    stmt = select(Preparation).where(
        Preparation.status.in_([status.value for status in _LIVE_STATUSES]),
        Preparation.expires_at.is_not(None),
        Preparation.expires_at <= moment,
    )
    if tenant_id is not None:
        stmt = stmt.where(Preparation.tenant_id == tenant_id)
    rows = (await session.scalars(stmt)).all()
    for row in rows:
        row.status = PreparationStatus.EXPIRED
        await _ledger.append(
            session,
            LedgerEvent(
                tenant_id=row.tenant_id,
                project_id=row.project_id,
                # 没有任何人在场：actor 留空（不是 0，0 会指向一个不存在的用户）
                actor_id=None,
                event_type=EvidenceEventType.PREPARATION_EXPIRED,
                subject_type="preparation",
                subject_id=str(row.id),
                artifact_hash=row.artifact_sha256,
                payload={"expires_at": row.expires_at.isoformat() if row.expires_at else None},
                occurred_at=moment,
            ),
        )
    if rows:
        await session.commit()
    return len(rows)


# ---------------------------------------------------------------- Commit 前置校验


async def assert_committable(
    session: AsyncSession,
    *,
    preparation_id: int,
    tenant_id: int,
    expected_artifact_sha256: str | None = None,
    now: datetime | None = None,
) -> CommittableFacts:
    """Commit 的唯一前置校验入口（be-13 §6.5）。任一条不满足即抛，返回事实供 be-14 签发令牌。

    ``tenant_id`` 是对设计签名的**加严**（be-13 §3.2 只写了 ``preparation_id``）：
    本函数自己查库，而「所有 SQL 强制带 tenant_id」是红线（CLAUDE.md §4.7）。
    少了它，一个跨租户的 id 能拿到另一租户的准备单事实。

    十条检查的顺序是**从便宜到昂贵**：先在内存里判状态与槽位，再查库比对指纹——
    失败路径不该为了一次必然的拒绝去读四份制品。
    """
    moment = now or utcnow()
    row = await load_preparation(session, tenant_id=tenant_id, preparation_id=preparation_id)

    # ① 状态：只有 approved 能提交
    if row.status is PreparationStatus.EXPIRED:
        raise PreparationExpiredError()
    if row.status is not PreparationStatus.APPROVED:
        raise ApprovalIncompleteError(
            f"准备单当前状态为 {row.status.value}，只有审批通过（approved）的才能提交执行"
        )

    # ② 有效期是硬边界：批过但过期了也不许执行（``as_utc`` 见 ① 的同一处说明）
    if row.expires_at is not None and as_utc(row.expires_at) <= moment:
        raise PreparationExpiredError()

    # ③ 必需槽必须齐（P1/P2 为两个；P3 免审批时为空集，此时 ① 已保证走过流程）
    required = [ApprovalSlot(slot) for slot in row.required_slots]
    decisions = await decisions_of(session, preparation_id=row.id)
    missing = [slot for slot in required if slot not in decisions]
    if missing:
        raise ApprovalIncompleteError(
            f"缺少审批槽：{'、'.join(slot.value for slot in missing)}"
        )
    rejected = [slot for slot, dec in decisions.items() if dec.decision is not ApprovalDecisionType.APPROVE]
    if rejected:
        raise ApprovalIncompleteError(
            f"存在非通过的决定：{'、'.join(slot.value for slot in sorted(rejected, key=str))}"
        )

    version = await repository.load_version(
        session=session, tenant_id=tenant_id, version_id=row.pipeline_version_id
    )
    pipeline = await repository.load_pipeline(
        session=session, tenant_id=tenant_id, pipeline_id=version.pipeline_id
    )

    # ④ 版本必须仍处于冻结态（草案会被覆盖，冻结点是「这份制品不再变」的承诺）
    if version.frozen_at is None:
        raise ConflictError("版本已不再是冻结态，请重新生成与准备")

    # ⑤ 制品指纹三方一致：准备单记录的 / 版本行上的 / 调用方期望的
    if version.sha256 != row.artifact_sha256:
        raise FingerprintMismatchError("制品指纹与准备单记录的不一致，请重新准备")
    if expected_artifact_sha256 is not None and expected_artifact_sha256 != row.artifact_sha256:
        raise FingerprintMismatchError("提交时提交的制品指纹与准备单不一致，请刷新后重试")

    plan_row, _hocon_row, quality_row, supervision_row = await repository.artifact_rows(
        session=session, version=version
    )
    plan = _plan_model(plan_row)

    # ⑥ 源端档案仍然就绪未过期（过期即说明审批时看到的表结构已不可考）
    await _require_fresh_profile(session=session, version=version, now=moment)

    # ⑦ 输入指纹未变：表结构、凭据版本、清洗规则、预算，任一变化都让旧审批失效
    current_fp = await current_input_fingerprint(
        session=session,
        tenant_id=tenant_id,
        version=version,
        plan=plan,
        quality=quality_row,
        supervision=supervision_row,
    )
    if current_fp != row.input_fingerprint:
        raise FingerprintMismatchError(
            "输入指纹已变化（表结构 / 凭据版本 / 清洗规则 / 预算），原审批失效，请重新准备与审批"
        )

    # ⑧ 每条决定必须是在**当前指纹**下作出的：指纹变了之后补一条新决定，
    #    不能让「上一条在旧指纹下批过」也一起复活
    for slot in required:
        if decisions[slot].fingerprint_at_decision != current_fp:
            raise FingerprintMismatchError(
                f"审批槽 {slot.value} 的审批发生在指纹变化之前，原审批失效，请重新审批"
            )

    # ⑨ 必须有回滚方案：没有回滚路径的写入在 PDP 里是 P1，而这里是不放行的硬闸
    steps = row.rollback_plan.get("steps") if isinstance(row.rollback_plan, dict) else None
    if not steps:
        raise ConflictError("准备单没有回滚方案，不能提交执行")

    # ⑩ 预算与资源范围必须成形：be-14 要靠它们建 execution_run
    if not row.budget or not row.resource_scope.get("writes"):
        raise ConflictError("准备单缺少预算或写入范围，不能提交执行")

    return CommittableFacts(
        preparation_id=row.id,
        pipeline_version_id=row.pipeline_version_id,
        pipeline_id=pipeline.id,
        project_id=row.project_id,
        tenant_id=row.tenant_id,
        environment=row.environment,
        risk_level=row.risk_level,
        sensitive_level=row.sensitive_level,
        artifact_sha256=row.artifact_sha256,
        input_fingerprint=row.input_fingerprint,
        required_slots=tuple(required),
        approvers={slot: decisions[slot].approver_id for slot in required},
        budget=dict(row.budget),
        resource_scope=dict(row.resource_scope),
        expires_at=_at(row.expires_at),
    )


# ---------------------------------------------------------------- 读接口


async def _roles(
    session: AsyncSession, *, project_id: int, user_id: int, now: datetime
) -> set[RoleName]:
    """当前用户在该项目下**此刻**有效的职责槽。

    一律重查 ``role_assignment``，不信 JWT 声明：声明是签发时刻的快照，
    角色被撤销后旧令牌还在有效期内——信任它等于让「已撤销的权限」继续可用。
    """
    return await active_roles(session, project_id=project_id, user_id=user_id, at=now)


def _my_slot(
    *,
    required: Sequence[ApprovalSlot],
    decided: dict[ApprovalSlot, ApprovalDecisionType],
    held: set[RoleName],
    created_by: int | None,
    user_id: int,
) -> ApprovalSlot | None:
    """当前用户可审的槽；``None`` = 不可审。

    三条排除：不持有对应角色、该槽已决定、**自己是发起人**（自批）。
    最后一个必须在服务端算出来并下发给前端——前端置灰只是提示，不是拦截。
    """
    if created_by is not None and created_by == user_id:
        return None
    for slot in required:
        if slot in decided:
            continue
        role = RoleName(slot.value)
        if has_role(held, role):
            return slot
    return None


def _can_commit(
    *,
    status: PreparationStatus,
    required: Sequence[ApprovalSlot],
    decided: dict[ApprovalSlot, ApprovalDecisionType],
    expires_at: datetime | None,
    now: datetime,
    stale: bool = False,
) -> bool:
    if status is not PreparationStatus.APPROVED or stale:
        return False
    # ``as_utc``：入参是**从库里读出来的**时间（SQLite 上是裸值），见 approval.decide ⑦
    if expires_at is not None and as_utc(expires_at) <= now:
        return False
    return all(decided.get(slot) is ApprovalDecisionType.APPROVE for slot in required)


async def _names(session: AsyncSession, ids: set[int]) -> dict[int, str]:
    """批量取显示名。空集合提前返回：``IN ()`` 在部分驱动上是语法错误。"""
    if not ids:
        return {}
    rows = (
        await session.execute(select(AppUser.id, AppUser.display_name).where(AppUser.id.in_(sorted(ids))))
    ).all()
    return {row[0]: row[1] for row in rows}


def _summary(
    *,
    row: Preparation,
    version: PipelineVersion,
    pipeline: Pipeline,
    creator_name: str,
    my_slot: ApprovalSlot | None,
    can_commit: bool,
    decided: Mapping[ApprovalSlot, ApprovalDecisionType],
) -> PreparationSummary:
    """摘要 DTO。``decided`` **由调用方传入**，不在这里从 ``row.decisions`` 现取。

    一度是本函数自己读 ``row.decisions``，于是同一个响应里 ``can_commit`` 用调用方
    新查的决定算、``decided_slots`` 用会话快照算——**两者会互相矛盾**
    （``can_commit=true`` 而 ``decided_slots`` 为空），而且看不出是同一份数据的两份来源。
    摘要里的槽位集合必须是调用方那一份。
    """
    return PreparationSummary(
        id=row.id,
        project_id=row.project_id,
        pipeline_id=pipeline.id,
        pipeline_name=pipeline.name,
        pipeline_version_id=row.pipeline_version_id,
        version_no=version.version_no,
        artifact_sha256=row.artifact_sha256,
        input_fingerprint=row.input_fingerprint,
        risk_level=row.risk_level,
        sensitive_level=row.sensitive_level,
        environment=row.environment,
        status=row.status,
        required_slots=tuple(ApprovalSlot(slot) for slot in row.required_slots),
        decided_slots=tuple(sorted(decided, key=str)),
        created_by=row.created_by or 0,
        creator_name=creator_name,
        frozen_at=_at(row.frozen_at),
        expires_at=_at(row.expires_at),
        my_slot=my_slot,
        can_commit=can_commit,
    )


async def get_preparation(
    session: AsyncSession, ctx: SubjectContext, preparation_id: int
) -> PreparationDetail:
    """准备单详情。指纹会**重算一次**，因此这里的 ``stale`` / ``can_commit`` 是权威值。"""
    now = utcnow()
    row = await load_preparation(session, tenant_id=ctx.tenant_id, preparation_id=preparation_id)
    version = await repository.load_version(
        session=session, tenant_id=ctx.tenant_id, version_id=row.pipeline_version_id
    )
    pipeline = await repository.load_pipeline(
        session=session, tenant_id=ctx.tenant_id, pipeline_id=version.pipeline_id
    )
    held = await _roles(session, project_id=row.project_id, user_id=ctx.user_id, now=now)

    plan_row, _hocon_row, quality_row, supervision_row = await repository.artifact_rows(
        session=session, version=version
    )
    try:
        current_fp = await current_input_fingerprint(
            session=session,
            tenant_id=ctx.tenant_id,
            version=version,
            plan=_plan_model(plan_row),
            quality=quality_row,
            supervision=supervision_row,
        )
    except NotFoundError:
        # 制品被删（RESTRICT 外键理论上不允许）时不该让详情整页 500：
        # 返回一个「与任何决定都不相等」的指纹，于是每条决定都显示为已失效——
        # 这正是事实（无法确认输入没变），而且危险方向是「更严」。
        log.warning("preparation_fingerprint_unavailable", preparation_id=row.id)
        current_fp = "\x00unavailable"

    # 决定列表**重新查一次库**（见 ``decisions_of`` 的 docstring：关系集合是会话快照）
    decisions = await decisions_of(session, preparation_id=row.id)
    decided = {slot: record.decision for slot, record in decisions.items()}
    stale_slots = {
        slot for slot, record in decisions.items() if record.fingerprint_at_decision != current_fp
    }
    required = [ApprovalSlot(slot) for slot in row.required_slots]
    actor_ids = {row.created_by} | {record.approver_id for record in decisions.values()}
    names = await _names(session, {int(item) for item in actor_ids if item is not None})
    summary = _summary(
        row=row,
        version=version,
        pipeline=pipeline,
        creator_name=names.get(row.created_by or 0, ""),
        my_slot=_my_slot(
            required=required,
            decided=decided,
            held=held,
            created_by=row.created_by,
            user_id=ctx.user_id,
        ),
        can_commit=_can_commit(
            status=row.status,
            required=required,
            decided=decided,
            expires_at=row.expires_at,
            now=now,
            stale=bool(stale_slots),
        ),
        decided=decided,
    )
    return PreparationDetail(
        **summary.model_dump(),
        resource_scope=_resource_scope_read(row.resource_scope),
        budget=_budget_read(row.budget),
        rollback_plan=_rollback_read(row.rollback_plan),
        risk_reasons=tuple(RiskReasonRead(**item) for item in row.risk_reasons),
        decisions=tuple(
            ApprovalDecisionRead(
                slot=record.slot,
                approver_id=record.approver_id,
                approver_name=names.get(record.approver_id, ""),
                approver_role=record.approver_role,
                decision=record.decision,
                comment=record.comment,
                artifact_sha256=record.artifact_sha256,
                fingerprint_at_decision=record.fingerprint_at_decision,
                decided_at=_at(record.decided_at),
                stale=record.slot in stale_slots,
            )
            # 排序键同样是「比较」：这次会话刚落的那条是 aware、库里读回的是裸值，
            # 混在一起排序会 TypeError —— 而 dict 里的插入顺序看起来是对的，
            # 所以只有真的排一次才会暴露。
            for record in sorted(
                decisions.values(), key=lambda item: (as_utc(item.decided_at), item.id)
            )
        ),
        timeline=await _timeline(session, row=row, names=names),
    )


async def get_status(
    session: AsyncSession, ctx: SubjectContext, preparation_id: int
) -> PreparationStatusView:
    """轻量轮询视图（be-13 §3.1）：只查准备单 + 决定，不读制品、不重算指纹。"""
    row = await load_preparation(session, tenant_id=ctx.tenant_id, preparation_id=preparation_id)
    required = [ApprovalSlot(slot) for slot in row.required_slots]
    # 与 ``get_preparation`` 同一个理由：决定重新查库，不用会话里的关系集合
    decided = {
        slot: record.decision
        for slot, record in (await decisions_of(session, preparation_id=row.id)).items()
    }
    approved = tuple(
        slot for slot in required if decided.get(slot) is ApprovalDecisionType.APPROVE
    )
    now = utcnow()
    return PreparationStatusView(
        status=row.status,
        approved_slots=approved,
        pending_slots=tuple(slot for slot in required if slot not in decided),
        expires_at=_at(row.expires_at),
        # 这里**不重算指纹**（那要读四份制品，与「轻量」相悖），所以 committable
        # 只是「状态 + 有效期」的结论；真正的闸在 assert_committable（Commit 时再判一次）
        committable=row.status is PreparationStatus.APPROVED
        and (row.expires_at is None or as_utc(row.expires_at) > now),
    )


async def list_preparations(
    session: AsyncSession,
    ctx: SubjectContext,
    *,
    scope: str = "all",
    status: PreparationStatus | None = None,
    risk_level: str | None = None,
    environment: str | None = None,
    slot: ApprovalSlot | None = None,
    pipeline_id: int | None = None,
    keyword: str = "",
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[PreparationSummary], int]:
    """准备单列表（be-13 §3.1）。

    ``scope``：``all`` / ``mine``（我发起的）/ ``todo``（等我审的）/ ``done``（我审过的）。

    ``todo`` 与 ``slot`` 两个筛选在 **Python 侧收口**：它们要判「jsonb 数组里有没有某个槽
    且我还没投过票」，而这个判定在 PG（``@>``）与 SQLite（无 jsonb 操作符，测试跑在它上面）
    下写法不同。硬写成方言相关的 SQL，会让同一段逻辑出现两份实现、其中一份只在生产才跑。
    代价是这两个筛选会先取回候选行——候选被 ``status`` 与项目**收窄过**，
    且 ``total`` 因此是过滤后的准确值（不是 SQL 计数）。数据量上去后的正解是
    把槽位抽成 ``preparation_slot`` 子表，那是一条 migration + 一次口径变更。
    """
    now = utcnow()
    stmt: Select[Any] = (
        select(Preparation, PipelineVersion, Pipeline)
        .join(PipelineVersion, PipelineVersion.id == Preparation.pipeline_version_id)
        .join(Pipeline, Pipeline.id == PipelineVersion.pipeline_id)
        .where(Preparation.tenant_id == ctx.tenant_id, Pipeline.tenant_id == ctx.tenant_id)
    )
    stmt = _apply_filters(
        stmt,
        ctx=ctx,
        scope=scope,
        status=status,
        risk_level=risk_level,
        environment=environment,
        slot=slot,
        pipeline_id=pipeline_id,
        keyword=keyword,
    )
    rows = (await session.execute(stmt.order_by(Preparation.id.desc()))).all()

    held_by_project = await _held_roles_by_project(session, ctx=ctx, now=now)
    creator_ids = {row[0].created_by for row in rows if row[0].created_by is not None}
    names = await _names(session, {int(i) for i in creator_ids})
    # 整页的决定一次查回来（见 decisions_for）：逐行读关系集合要么是会话快照、
    # 要么得靠 populate_existing，而这两种都是「读的人得记得」才正确
    by_preparation = await decisions_for(session, preparation_ids=[row[0].id for row in rows])

    items: list[PreparationSummary] = []
    for preparation, version, pipeline in rows:
        decisions = by_preparation.get(preparation.id, {})
        decided = {slot: record.decision for slot, record in decisions.items()}
        required = [ApprovalSlot(item) for item in preparation.required_slots]
        held = held_by_project.get(preparation.project_id, set())
        mine = preparation.created_by == ctx.user_id
        mine_decided = any(
            decision.approver_id == ctx.user_id for decision in decisions.values()
        )
        if scope == "mine" and not mine:
            continue
        if scope == "done" and not mine_decided:
            continue
        if scope == "todo":
            if preparation.status not in _LIVE_STATUSES or mine_decided:
                continue
            if _my_slot(
                required=required,
                decided=decided,
                held=held,
                created_by=preparation.created_by,
                user_id=ctx.user_id,
            ) is None:
                continue
        if slot is not None:
            # 只看「这个槽还没批」的单：已经批过的单对审批人没有待办意义
            if slot not in required or slot in decided:
                continue

        items.append(
            _summary(
                row=preparation,
                version=version,
                pipeline=pipeline,
                creator_name=names.get(preparation.created_by or 0, ""),
                my_slot=_my_slot(
                    required=required,
                    decided=decided,
                    held=held,
                    created_by=preparation.created_by,
                    user_id=ctx.user_id,
                ),
                # 列表**不重算指纹**（每行一次四份制品读取，20 行就是 80 次查询）。
                # 这里给的是「状态 + 有效期」的结论；权威结论在详情页与 Commit 前校验。
                can_commit=_can_commit(
                    status=preparation.status,
                    required=required,
                    decided=decided,
                    expires_at=preparation.expires_at,
                    now=now,
                ),
                decided=decided,
            )
        )

    total = len(items)
    offset = max(0, (page - 1) * page_size)
    return items[offset : offset + page_size], total


def _apply_filters(
    stmt: Select[Any],
    *,
    ctx: SubjectContext,
    scope: str,
    status: PreparationStatus | None,
    risk_level: str | None,
    environment: str | None,
    slot: ApprovalSlot | None,
    pipeline_id: int | None,
    keyword: str,
) -> Select[Any]:
    """能在 SQL 里判的筛选一律下推（分页才有意义）。"""
    if status is not None:
        stmt = stmt.where(Preparation.status == status.value)
    elif scope == "todo" or slot is not None:
        # 待办只看活跃态：终态的单不该出现在任何人的待办里
        stmt = stmt.where(Preparation.status.in_([item.value for item in _LIVE_STATUSES]))
    if risk_level:
        stmt = stmt.where(Preparation.risk_level == risk_level)
    if environment:
        stmt = stmt.where(Preparation.environment == environment)
    if pipeline_id is not None:
        stmt = stmt.where(PipelineVersion.pipeline_id == pipeline_id)
    if scope == "mine":
        stmt = stmt.where(Preparation.created_by == ctx.user_id)
    if scope == "done":
        stmt = stmt.where(
            Preparation.id.in_(
                select(ApprovalDecision.preparation_id).where(
                    ApprovalDecision.approver_id == ctx.user_id
                )
            )
        )
    if keyword:
        # 关键词按 pipeline 名搜（用户记得住的是流程名，不是准备单 id）
        stmt = stmt.where(Pipeline.name.ilike(f"%{keyword}%"))
    return stmt


async def _held_roles_by_project(
    session: AsyncSession, *, ctx: SubjectContext, now: datetime
) -> dict[int, set[RoleName]]:
    """一次算清「我在这些项目下各持有哪些槽」。

    逐行调 ``active_roles`` 会让 20 行的列表产生 20 次角色查询——而角色判定的语义
    仍然只有 ``services.project.active_roles`` 一份（这里只是把它的结果按项目缓存起来）。
    """
    project_ids = (
        await session.scalars(
            select(ProjectMember.project_id)
            .join(Project, Project.id == ProjectMember.project_id)
            .where(Project.tenant_id == ctx.tenant_id, ProjectMember.user_id == ctx.user_id)
        )
    ).all()
    return {
        project_id: await active_roles(session, project_id=project_id, user_id=ctx.user_id, at=now)
        for project_id in sorted(set(project_ids))
    }


async def _timeline(
    session: AsyncSession, *, row: Preparation, names: dict[int, str]
) -> tuple[PreparationTimelineItem, ...]:
    """时间线 = 准备单创建 + 每条审批决定 + 过期/被取代。

    过期与被取代的**时刻**只有账本里有（status 的当前值只说明结局，不说明时刻），
    所以那两个事件要从 ``evidence_event`` 读回来——这不是「再存一份」，
    而是承认「状态是终态、账本是过程」。
    """
    items: list[PreparationTimelineItem] = [
        PreparationTimelineItem(
            # ``_at``：这一列是**排序键**。库里读出的裸值与刚落的 aware 时间混在一张
            # 列表里时，``sorted`` 会 TypeError（或更糟：两个裸值悄悄按本地时区排）。
            at=_at(row.frozen_at),
            event="created",
            actor_name=names.get(row.created_by or 0, ""),
            detail=f"风险等级 {row.risk_level.value}",
        )
    ]
    # 决定同样**重新查库**（见 ``decisions_of``）：用关系集合时，同一会话里刚落的那条
    # 决定不会进集合，时间线会少一格——而少的那一格恰恰是「刚刚发生了什么」。
    decisions = await decisions_of(session, preparation_id=row.id)
    for decision in sorted(
        decisions.values(), key=lambda item: (as_utc(item.decided_at), item.id)
    ):
        event = {
            ApprovalDecisionType.APPROVE: "approved_slot",
            ApprovalDecisionType.REJECT: "rejected_slot",
            ApprovalDecisionType.RETURN: "returned_slot",
        }[decision.decision]
        items.append(
            PreparationTimelineItem(
                at=_at(decision.decided_at),
                event=event,  # type: ignore[arg-type]
                slot=decision.slot,
                actor_name=names.get(decision.approver_id, ""),
                detail=decision.comment,
            )
        )
    if row.status in (PreparationStatus.EXPIRED, PreparationStatus.SUPERSEDED):
        wanted = (
            EvidenceEventType.PREPARATION_EXPIRED
            if row.status is PreparationStatus.EXPIRED
            else EvidenceEventType.PREPARATION_SUPERSEDED
        )
        events = (
            await session.scalars(
                select(EvidenceEvent).where(
                    EvidenceEvent.tenant_id == row.tenant_id,
                    EvidenceEvent.subject_type == "preparation",
                    EvidenceEvent.subject_id == str(row.id),
                    EvidenceEvent.event_type == wanted.value,
                )
            )
        ).all()
        for event in events:
            items.append(
                PreparationTimelineItem(
                    at=_at(event.occurred_at),
                    event="expired" if row.status is PreparationStatus.EXPIRED else "superseded",
                    actor_name=names.get(event.actor_id or 0, "") or None,
                    detail=None,
                )
            )
        if not events:
            # 账本里没有（例如状态是人工改的）：用过期时刻兜底，前端至少能画出这一格
            items.append(
                PreparationTimelineItem(
                    at=_at(row.expires_at or row.frozen_at),
                    event="expired" if row.status is PreparationStatus.EXPIRED else "superseded",
                )
            )
    return tuple(sorted(items, key=lambda item: item.at))


def _resource_scope_read(raw: dict[str, Any]) -> ResourceScopeRead:
    """库列 → 读 DTO。``secrets`` 在这里被**有意丢弃**（见模块内注释与 DTO 文档）。"""
    return ResourceScopeRead(
        reads=tuple(ResourceReadRef(**item) for item in raw.get("reads", [])),
        writes=tuple(
            ResourceWriteRef(
                data_source_id=item["data_source_id"],
                database=item["database"],
                table=item["table"],
                mode=item.get("mode", "shadow_then_swap"),
            )
            for item in raw.get("writes", [])
        ),
    )


def _budget_read(raw: dict[str, Any]) -> ExecutionBudgetRead | None:
    if not raw:
        return None
    return ExecutionBudgetRead(**raw)


def _rollback_read(raw: dict[str, Any]) -> RollbackPlanRead:
    if not raw:
        return RollbackPlanRead()
    return RollbackPlanRead(
        steps=tuple(RollbackStep(**item) for item in raw.get("steps", [])),
        shadow_cleanup=bool(raw.get("shadow_cleanup", True)),
        restore_scope=str(raw.get("restore_scope", "")),
    )


async def count_open(
    session: AsyncSession, *, tenant_id: int, project_id: int | None = None
) -> int:
    """未终态的准备单条数（概览卡与巡检用）。"""
    stmt = (
        select(func.count())
        .select_from(Preparation)
        .where(
            Preparation.tenant_id == tenant_id,
            Preparation.status.in_([status.value for status in _LIVE_STATUSES]),
        )
    )
    if project_id is not None:
        stmt = stmt.where(Preparation.project_id == project_id)
    return int(await session.scalar(stmt) or 0)
