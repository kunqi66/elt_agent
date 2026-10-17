"""策略载入与判定预演（be-07 的 service 侧）。

引擎（`engines/policy/`）是纯函数：**规则从哪来、判定结果落到哪，都由本层决定**。
本模块只做两件事：

1. `load_active_policy()` —— 把「内置基线 ⊕ 租户已启用的 `policy_rule` 覆盖」
   合成一份可判定的规则集，并把被拒的越权覆盖转成 `RuleMergeWarning`。
   **其它模块（门禁 be-11、Prepare、监管 be-16）判定时的唯一入口**。
2. `preview()` —— 用当前生效的规则集对一份「意图 + 资源范围」做一次**只读**预演。

关于「预演不落库」：`preview` 不写任何表，**也不写证据账本**。
预演是前端在用户勾选配置时的高频调用（每次改一个字段就可能触发），
若每次都入账，账本会被「谁的鼠标动了」这类事件淹没，
真正的审批与执行事件反而被稀释。判定的留痕在**真正做决定的那一刻**
（门禁、Prepare、Commit）由那些模块各自写入。

本层禁止 ``import fastapi``。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import get_settings
from core.errors import RoleRequiredError
from core.ids import utcnow
from engines.policy import (
    BUILTIN_RULES,
    BudgetDefaults,
    PolicyDecision,
    ResourceScope,
    RuleMergeWarning,
    RuleSpec,
    ToolIntent,
    can_read_datasource,
    decide,
    merge_rules,
)
from harness.ledger import EvidenceLedger, LedgerEvent
from models.enums import ApprovalSlot, Env, EvidenceEventType, SensitiveLevel
from models.governance import PolicyRule, RiskPolicyVersion
from schemas.auth import SubjectContext
from services.project import active_roles

_ledger = EvidenceLedger()

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class ActivePolicy:
    """某一刻生效的规则集及其版本号。

    ``version`` 会原样进 `PolicyDecision.policy_version` 与证据，
    用来区分「按已审批的策略判定」与「按代码里的内置基线判定」（后者为 0）。
    """

    rules: tuple[RuleSpec, ...]
    warnings: tuple[RuleMergeWarning, ...]
    version: int


def budget_defaults() -> BudgetDefaults:
    """从 Settings 构造默认预算。

    `float → Decimal` 要经 `str()`：``Decimal(0.05)`` 会得到
    0.05000000000000000277…，而预算比较是精确比较，差这一点就可能让
    「刚好等于上限」的作业被判成超限。
    """
    settings = get_settings()
    return BudgetDefaults(
        max_read_rows=settings.SUP_MAX_READ_ROWS,
        max_write_bytes=settings.SUP_MAX_WRITE_BYTES,
        max_duration_sec=settings.SUP_MAX_DURATION_SEC,
        max_amplification_ratio=Decimal(str(settings.SUP_MAX_AMPLIFICATION_RATIO)),
        max_reject_rate=Decimal(str(settings.SUP_MAX_REJECT_RATE)),
        warn_threshold_pct=settings.SUP_WARN_THRESHOLD_PCT,
    )


async def _current_version(session: AsyncSession, tenant_id: int) -> int:
    """租户当前生效的策略集版本号；一条都没有时为 0（= 内置基线）。"""
    value = (
        await session.execute(
            select(func.max(RiskPolicyVersion.version)).where(
                RiskPolicyVersion.tenant_id == tenant_id,
                RiskPolicyVersion.effective_at.is_not(None),
            )
        )
    ).scalar_one_or_none()
    return int(value or 0)


async def load_active_policy(
    session: AsyncSession, *, tenant_id: int, at: datetime | None = None
) -> ActivePolicy:
    """载入并合成生效规则集。

    ``policy_rule`` 是**全局规则目录**（无 tenant 列）：它是「本产品认哪些风险模式」
    的目录，而「本租户此刻认哪些版本」由 `risk_policy_version` 记录。
    因此这里读全表，再用 `policy_version` 承载租户维度的差异。

    **不做「只取 enabled 行」的过滤**：`enabled=False` 的覆盖行必须被
    `merge_rules` 看见——它正是「关掉某条非基线规则」的表达。
    在 SQL 里过滤掉它，等于把「关掉」变成了「没这条覆盖」，
    于是内置基线又重新生效，用户会发现规则关不掉。
    """
    rows = (await session.execute(select(PolicyRule))).scalars().all()

    overrides: list[RuleSpec] = []
    broken: list[RuleMergeWarning] = []
    for row in rows:
        try:
            overrides.append(
                RuleSpec(
                    code=row.code,
                    level=row.level,
                    title=row.title,
                    description=row.description or "",
                    condition=row.condition or {},
                    required_slots=_slots(row.required_slots),
                    enabled=row.enabled,
                    version=row.version,
                )
            )
        except Exception as exc:
            # 一条存坏的条件（手改过 DB、或旧格式）如果直接抛，
            # 结果是**所有人都无法判定**——一个配置错误升级成一次全站故障。
            # 因此降级为警告 + 忽略该行，并让调用方把警告写进证据。
            broken.append(RuleMergeWarning(code=row.code, reason=f"条件非法，已忽略该行：{exc}"))
            log.warning("policy_rule_invalid", code=row.code, version=row.version, error=str(exc))

    rules, warnings = merge_rules(BUILTIN_RULES, overrides)
    return ActivePolicy(
        rules=rules,
        warnings=tuple(broken) + warnings,
        version=await _current_version(session, tenant_id),
    )


def _slots(raw: list[str] | None) -> tuple[ApprovalSlot, ...]:
    return tuple(ApprovalSlot(item) for item in (raw or []))


async def decide_for_project(
    session: AsyncSession,
    *,
    tenant_id: int,
    project_id: int,
    actor_id: int,
    intent: ToolIntent,
    scope: ResourceScope,
    env: Env,
    level: SensitiveLevel,
    now: datetime | None = None,
) -> PolicyDecision:
    """门禁 / Prepare / Commit 的判定入口（be-11 起调用）。

    一次调用完成「载入策略 → 判定 → 把被拒的越权覆盖入账」。
    读权限**不在本函数内校验**：调用方（门禁、Prepare）已经在各自的
    角色检查里确认过操作者身份，在这里再查一次只会产生第二份权限语义。
    """
    active = await load_active_policy(session, tenant_id=tenant_id)
    decision = decide(
        intent,
        scope,
        env,
        level,
        now=now or utcnow(),
        rules=active.rules,
        defaults=budget_defaults(),
        policy_version=active.version,
    )
    if active.warnings:
        await _ledger_warnings(
            session,
            tenant_id=tenant_id,
            project_id=project_id,
            actor_id=actor_id,
            warnings=active.warnings,
        )
    return decision


async def _ledger_warnings(
    session: AsyncSession,
    *,
    tenant_id: int,
    project_id: int,
    actor_id: int,
    warnings: tuple[RuleMergeWarning, ...],
) -> None:
    """被忽略的越权覆盖（关闭/降级基线规则）必须留痕。

    这是「有人试图关掉一条 P0」的唯一证据来源——不记的话，
    这次尝试与「什么都没发生」在审计上完全等价。
    """
    payload = {"warnings": [{"code": w.code, "reason": w.reason} for w in warnings]}
    await _ledger.append(
        session,
        LedgerEvent(
            tenant_id=tenant_id,
            project_id=project_id,
            actor_id=actor_id,
            event_type=EvidenceEventType.POLICY_EVALUATED,
            subject_type="policy_rule_set",
            subject_id="active",
            payload=payload,
        ),
    )


async def preview(
    session: AsyncSession,
    ctx: SubjectContext,
    *,
    project_id: int,
    intent: ToolIntent,
    scope: ResourceScope,
    env: Env,
    level: SensitiveLevel,
) -> PolicyDecision:
    """只读预演。**不落库、不入账**（见模块 docstring）。"""
    await _require_project_roles(session, ctx, project_id)
    active = await load_active_policy(session, tenant_id=ctx.tenant_id)
    return decide(
        intent,
        scope,
        env,
        level,
        now=utcnow(),
        rules=active.rules,
        defaults=budget_defaults(),
        policy_version=active.version,
    )


async def _require_project_roles(session: AsyncSession, ctx: SubjectContext, project_id: int) -> None:
    """与本项目其它读接口同口径：任一职责槽即可（含 auditor）。

    预演不产生副作用、不泄露凭据，把它限成「只有 checker 能看风险等级」
    只会让 maker 在提交前无法自查，从而到审批阶段才被打回。
    """
    roles = await active_roles(session, project_id=project_id, user_id=ctx.user_id)
    if not can_read_datasource(roles):
        raise RoleRequiredError("查看策略判定需要本项目的任一职责槽")
