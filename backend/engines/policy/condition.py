"""扁平条件 ↔ 规范 DSL 的适配器。

**为什么需要两层格式**：`frontend/src/types/governance.ts` 的 `PolicyCondition`
是一个**扁平的命名条件键**对象（`{env?: Env[], writes_formal_table?: boolean, ...}`），
并且明确约定「未选择的条件键一律不写入」（不是写 false）。而引擎 §6.2 的求值格式
是带组合子的嵌套 DSL。

两者不是同一份设计，但**扁平形式恰好是 DSL 的一个子集**（若干叶子的 AND）。
所以本模块把扁平形式编译成规范 DSL：

- **DB 里存扁平形式**（即前端契约，`policy_rule.condition` 的列内容就是它）；
- **引擎求值前编译成 DSL**（`normalize_condition`），于是两种形式都能被求值，
  内置基线规则（代码里直接写的 DSL）不需要被改写成扁平形式。

反方向的「DSL → 扁平」**刻意不提供**：它不是满射，硬做会在
「规则里有一条 `any`」时静默丢掉语义，而丢掉的那部分正是宽严所在。
读取接口原样下发存储内容，于是前端只可能看到自己写进去的那种形式。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from models.enums import Env

# 扁平条件的合法键。**与前端 `PolicyCondition` 一一对应**，多一个少一个都要同步两边。
FLAT_KEYS: frozenset[str] = frozenset(
    {
        "env",
        "writes_formal_table",
        "sensitive_plaintext_egress",
        "bypass_approval_intent",
        "cross_tenant_access",
        "resource_out_of_scope",
        "budget_over_default_factor",
        "missing_rollback_plan",
        "unregistered_secret_ref",
        "read_only",
    }
)

# 组合子键：出现过即认为是规范 DSL 而非扁平形式。
_DSL_KEYS = frozenset({"all", "any", "not", "feature", "op", "value"})


def _leaf(feature: str, op: str, value: Any = None) -> dict[str, Any]:
    node: dict[str, Any] = {"feature": feature, "op": op}
    if value is not None:
        node["value"] = value
    return node


# 每个扁平键 → 真/假两种取值对应的 DSL 片段。
# **假不是「无关」**：前端的约定是「不写 = 不约束」，而显式写 false 就是
# 一条真实的条件（「这条规则只在**不是**这种情况时命中」），必须编译成 `not`。
_FLAT_COMPILERS: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {
    "writes_formal_table": (
        _leaf("scope.writes_formal_table", "is_true"),
        _leaf("scope.writes_formal_table", "is_false"),
    ),
    "sensitive_plaintext_egress": (
        _leaf("scope.sensitive_plaintext_egress_count", "gt", 0),
        _leaf("scope.sensitive_plaintext_egress_count", "eq", 0),
    ),
    "bypass_approval_intent": (
        {
            "any": [
                _leaf("intent.bypass_approval", "is_true"),
                _leaf("intent.disable_audit", "is_true"),
                _leaf("intent.export_plaintext_sensitive", "is_true"),
            ]
        },
        {
            "all": [
                _leaf("intent.bypass_approval", "is_false"),
                _leaf("intent.disable_audit", "is_false"),
                _leaf("intent.export_plaintext_sensitive", "is_false"),
            ]
        },
    ),
    "cross_tenant_access": (
        {
            "any": [
                _leaf("intent.cross_tenant", "is_true"),
                _leaf("scope.cross_tenant_count", "gt", 0),
            ]
        },
        {
            "all": [
                _leaf("intent.cross_tenant", "is_false"),
                _leaf("scope.cross_tenant_count", "eq", 0),
            ]
        },
    ),
    "resource_out_of_scope": (
        _leaf("scope.out_of_scope_count", "gt", 0),
        _leaf("scope.out_of_scope_count", "eq", 0),
    ),
    # 取「缺失回滚方案」的**真补集**（De Morgan），不是简单地翻转那一个叶子：
    # 写成 `has_rollback_plan is_true` 的话，只读探查（没有回滚方案是正常的）
    # 会连这条「反向条件」都不满足，规则在两种取值下都不命中——一个恒假的约束。
    "missing_rollback_plan": (
        {
            "all": [
                _leaf("intent.is_write", "is_true"),
                _leaf("intent.has_rollback_plan", "is_false"),
            ]
        },
        {
            "any": [
                _leaf("intent.is_write", "is_false"),
                _leaf("intent.has_rollback_plan", "is_true"),
            ]
        },
    ),
    "unregistered_secret_ref": (
        _leaf("scope.unregistered_secret_count", "gt", 0),
        _leaf("scope.unregistered_secret_count", "eq", 0),
    ),
    "read_only": (
        _leaf("intent.is_write", "is_false"),
        _leaf("intent.is_write", "is_true"),
    ),
}


def is_flat(condition: Mapping[str, Any]) -> bool:
    """按**键的形状**判别形式，而不是靠调用方声明。

    判别依据是组合子键，不是「有没有 `feature`」：`{"all": [...]}` 与
    `{}` 都不含 `feature`，但前者是 DSL、后者是「无约束」的扁平形式。
    空对象两种解释等价（恒真），归到扁平一侧，省一次编译。
    """
    if not condition:
        return True
    return not (set(condition) & _DSL_KEYS)


def compile_flat_condition(condition: Mapping[str, Any]) -> dict[str, Any]:
    """扁平形式 → 规范 DSL。未选择（缺键）的维度不产生约束。

    未知键 → `ValueError`：静默忽略一个不认识的键意味着「用户以为他加了一条约束，
    而系统当它不存在」——那正是策略界面最容易骗人的地方。
    """
    unknown = set(condition) - FLAT_KEYS
    if unknown:
        raise ValueError(f"未知的扁平条件键：{sorted(unknown)}")

    parts: list[dict[str, Any]] = []

    if "env" in condition:
        raw = condition["env"]
        values = [raw] if isinstance(raw, (str, Env)) else list(raw)
        if not values:
            # 空环境列表是「任何环境都不命中」= 规则永不生效。允许，但必须是显式的
            # `in []`，不能被当成「缺省」——前者是作者的选择，后者是漏写。
            parts.append(_leaf("env", "in", []))
        else:
            normalised = [item.value if isinstance(item, Env) else str(item) for item in values]
            parts.append(_leaf("env", "in", normalised))

    for key, (truthy, falsy) in _FLAT_COMPILERS.items():
        if key not in condition:
            continue
        flag = condition[key]
        if not isinstance(flag, bool):
            raise ValueError(f"条件键 {key} 需要布尔值，得到 {type(flag).__name__}")
        parts.append(truthy if flag else falsy)

    if "budget_over_default_factor" in condition:
        factor = condition["budget_over_default_factor"]
        if isinstance(factor, bool) or not isinstance(factor, (int, float, str)):
            raise ValueError("budget_over_default_factor 需要数值")
        parts.append(_leaf("budget.over_default_ratio_max", "gt", factor))

    if not parts:
        return {}
    if len(parts) == 1:
        return parts[0]
    return {"all": parts}


def normalize_condition(condition: Mapping[str, Any]) -> dict[str, Any]:
    """把任意一种存储形式规整成规范 DSL。引擎求值前**必须**过这里。"""
    return compile_flat_condition(condition) if is_flat(condition) else dict(condition)
