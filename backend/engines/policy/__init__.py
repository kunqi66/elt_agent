"""策略与判定引擎（纯函数层）。

两层内容：
- `roles.py`：职责槽与权限判定（be-02 起）；
- `models.py` / `rules.py` / `pdp.py`：PDP 风险判定（be-07）。
"""

from engines.policy.condition import (
    FLAT_KEYS,
    compile_flat_condition,
    is_flat,
    normalize_condition,
)
from engines.policy.models import (
    ALWAYS_SENSITIVE_KINDS,
    BudgetDefaults,
    BudgetRequest,
    PolicyDecision,
    ResourceRef,
    ResourceScope,
    RuleHit,
    RuleMergeWarning,
    RuleSpec,
    SensitiveColumn,
    ToolIntent,
    to_policy_kind,
)
from engines.policy.pdp import decide
from engines.policy.roles import (
    can_manage_datasource,
    can_manage_members,
    can_manage_pipeline,
    can_manage_roles,
    can_read_datasource,
    can_read_ledger,
    can_read_pipeline,
    check_dual_checker_distinct,
    check_duty_separation,
    has_role,
    is_assignment_valid,
    is_self_approval,
)
from engines.policy.rules import (
    BUILTIN_RULES,
    FEATURES,
    LEVEL_RANK,
    budget_ceiling,
    build_features,
    effective_budget,
    evaluate_condition,
    merge_rules,
    over_default_ratio,
    validate_condition,
)

__all__ = [
    "ALWAYS_SENSITIVE_KINDS",
    "BUILTIN_RULES",
    "FEATURES",
    "FLAT_KEYS",
    "LEVEL_RANK",
    "BudgetDefaults",
    "BudgetRequest",
    "PolicyDecision",
    "ResourceRef",
    "ResourceScope",
    "RuleHit",
    "RuleMergeWarning",
    "RuleSpec",
    "SensitiveColumn",
    "ToolIntent",
    "build_features",
    "budget_ceiling",
    "can_manage_datasource",
    "can_manage_members",
    "can_manage_pipeline",
    "can_manage_roles",
    "can_read_datasource",
    "can_read_ledger",
    "can_read_pipeline",
    "check_dual_checker_distinct",
    "check_duty_separation",
    "compile_flat_condition",
    "decide",
    "effective_budget",
    "evaluate_condition",
    "has_role",
    "is_assignment_valid",
    "is_flat",
    "is_self_approval",
    "merge_rules",
    "normalize_condition",
    "over_default_ratio",
    "to_policy_kind",
    "validate_condition",
]
