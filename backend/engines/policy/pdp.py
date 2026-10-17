"""`decide()`：确定性策略判定（be-07 §6.4）。

**同输入必同输出**，靠四件事保证（§6.7）：无随机、无缓存、无全局可变状态、
无时间依赖（`now` 由调用方注入）。`PolicyDecision` 是 frozen 模型，
拿到的结果不会被下游改掉之后再被另一个人读到。

**不短路**：命中的规则全量收集，不在第一条命中处停下。
证据的用途是回答「为什么是这个等级」，只报第一条会让审批人
看不到另一条同时成立、且将来可能被单独放宽的规则。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal

from engines.policy.models import (
    BudgetDefaults,
    PolicyDecision,
    ResourceScope,
    RuleHit,
    RuleSpec,
    ToolIntent,
)
from engines.policy.rules import (
    BUILTIN_RULES,
    build_features,
    effective_budget,
    evaluate_condition,
    fallback_rule,
)
from models.enums import ApprovalSlot, Env, RiskLevel, SensitiveLevel


def decide(
    intent: ToolIntent,
    scope: ResourceScope,
    env: Env,
    level: SensitiveLevel,
    *,
    now: datetime,
    rules: Sequence[RuleSpec] | None = None,
    defaults: BudgetDefaults | None = None,
    project_overrides: Mapping[str, str] | None = None,
    policy_version: int = 0,
) -> PolicyDecision:
    """把「意图 + 资源范围 + 环境 + 数据分级」映射为一个 `PolicyDecision`。

    ``rules=None`` 用内置基线。``policy_version=0`` 表示「未加载租户策略集，
    用的是内置基线」——这个值会原样进证据，好让审计能区分
    「按已审批的策略判定」与「按代码里的基线判定」。
    """
    if now.tzinfo is None:
        raise ValueError("now 必须带时区：裸 datetime 在不同时区的主机上给出不同的 evaluated_at")

    active_defaults = defaults or _ZERO_DEFAULTS
    active_rules = tuple(rules) if rules is not None else BUILTIN_RULES

    features = build_features(
        intent,
        scope,
        env,
        level,
        defaults=active_defaults,
        project_overrides=project_overrides,
    )

    hits: list[RuleHit] = []
    for rule in active_rules:
        if not rule.enabled:
            continue
        if not evaluate_condition(rule.condition, features):
            continue
        hits.append(
            RuleHit(
                code=rule.code,
                level=rule.level,
                title=rule.title,
                evidence=_evidence(rule, features),
            )
        )

    if not hits:
        # 兜底（§6.3 P2）：写入意图**永远不会**落到 P3。
        # 表达为 decide() 内部的分支而不是一条 `policy_rule` 行，因为它需要
        # 「其他规则均未命中」这种否定式聚合——DSL 里没有，也不该有。
        rule = fallback_rule(intent)
        hits.append(RuleHit(code=rule.code, level=rule.level, title=rule.title, evidence={}))

    risk_level = RiskLevel.most_severe(*(hit.level for hit in hits))
    blocked = risk_level.must_block

    # P0 不进审批：槽位数没有意义，提前归零而不是留着让下游去解释。
    required_slots: tuple[ApprovalSlot, ...] = ()
    if not blocked and risk_level.min_approval_slots == 2:
        required_slots = (ApprovalSlot.CHECKER_DATA, ApprovalSlot.CHECKER_SECURITY)

    ordered = tuple(sorted(hits, key=lambda hit: (hit.level.rank, hit.code)))
    return PolicyDecision(
        level=risk_level,
        blocked=blocked,
        required_slots=required_slots,
        hardening_required=risk_level.requires_hardening and not blocked,
        budget=effective_budget(intent.budget, active_defaults, project_overrides),
        hits=ordered,
        reasons=tuple(f"[{hit.level.value}] {hit.code}：{hit.title}" for hit in ordered),
        policy_version=policy_version,
        evaluated_at=now,
    )


# 未提供 defaults 时的零点。**不是**「默认预算」——它是一个显式的地板：
# 上线时若忘了注入 Settings，判定不会因为一个隐藏的魔法数而看起来正常。
_ZERO_DEFAULTS = BudgetDefaults(
    max_read_rows=0,
    max_write_bytes=0,
    max_duration_sec=0,
    max_amplification_ratio=Decimal(0),
    max_reject_rate=Decimal(0),
    warn_threshold_pct=0,
)


def _evidence(rule: RuleSpec, features: Mapping[str, str]) -> dict[str, str]:
    """把命中的那几条特征记下来。**只记特征名与字符串化的值**——
    特征表本身就只有枚举/布尔/计数/预算，不含任何样本数据（§8.6）。"""
    referenced: list[str] = []
    _collect_features(rule.condition, referenced)
    return {name: features[name] for name in sorted(set(referenced)) if name in features}


def _collect_features(condition: Mapping[str, object], out: list[str]) -> None:
    for key in ("all", "any"):
        if key in condition:
            body = condition[key]
            if isinstance(body, (list, tuple)):
                for item in body:
                    if isinstance(item, Mapping):
                        _collect_features(item, out)
            return
    if "not" in condition:
        body = condition["not"]
        if isinstance(body, Mapping):
            _collect_features(body, out)
        return
    feature = condition.get("feature")
    if isinstance(feature, str):
        out.append(feature)
