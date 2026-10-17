"""内置基线规则 + 条件 DSL + 特征向量（be-07 §6.1–§6.3、§6.6）。

三条不变量，改这个文件之前先读：

1. **特征白名单是唯一可判定输入面**（§6.1）。规则作者引用表外字段一律
   `ValueError`，不是静默恒假——一条永远不命中的防线比没有防线更危险，
   因为它会出现在「策略已覆盖」的清单里。
2. **基线不可关闭**（§6.6）。`p0.` / `p3.` 前缀的规则即使 DB 里被写成
   `enabled=False` 或降级，载入时也一律忽略覆盖并告警。P0/P1 拦截率 100%
   是验收指标，能被配置关掉就等于不成立。
3. **无时间依赖**（§6.7）。本文件不得取当前时间；`now` 由调用方注入。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from decimal import Decimal, InvalidOperation
from typing import Any

from engines.policy.models import (
    ALWAYS_SENSITIVE_KINDS,
    BudgetDefaults,
    BudgetRequest,
    ResourceScope,
    RuleMergeWarning,
    RuleSpec,
    SensitiveColumn,
    ToolIntent,
)
from models.enums import ApprovalSlot, Env, RiskLevel, SensitiveLevel

# 敏感等级的内部序。**不得用字符串比较**：``"internal" < "confidential"`` 在
# 字典序下是 False，而它们与敏感度的关系恰好不是字典序（§6.3 注）。
LEVEL_RANK: dict[str, int] = {
    SensitiveLevel.PUBLIC.value: 0,
    SensitiveLevel.INTERNAL.value: 1,
    SensitiveLevel.CONFIDENTIAL.value: 2,
    SensitiveLevel.RESTRICTED.value: 3,
}

# 条件 DSL 的算子全集（§6.2）。不在表内的算子 = 规则作者的笔误，必须报错。
_CONDITION_OPS = frozenset(
    {
        "eq",
        "ne",
        "in",
        "not_in",
        "gt",
        "gte",
        "lt",
        "lte",
        "exists",
        "is_true",
        "is_false",
        "is_empty",
        "len_gte",
    }
)

# 特征名 → 类型种类。类型用于：
#   ① 规则引用表外字段时立刻报错（§6.1）；
#   ② 服务层发布策略时做一次「算子与类型是否匹配」的静态检查，
#      比如对布尔特征写 `gt` 是笔误，等到运行时才发现就晚了。
FEATURES: dict[str, str] = {
    "env": "str",
    "level": "str",
    "intent.kind": "str",
    "intent.is_write": "bool",
    "intent.tool": "str",
    "intent.bypass_approval": "bool",
    "intent.disable_audit": "bool",
    "intent.export_plaintext_sensitive": "bool",
    "intent.cross_tenant": "bool",
    "intent.has_rollback_plan": "bool",
    "scope.writes_formal_table": "bool",
    "scope.writes_shadow_table": "bool",
    "scope.out_of_scope_count": "int",
    "scope.cross_tenant_count": "int",
    "scope.unregistered_secret_count": "int",
    "scope.sensitive_plaintext_egress_count": "int",
    "scope.read_table_count": "int",
    "scope.write_table_count": "int",
    "budget.over_default_ratio_max": "decimal",
    "budget.max_read_rows": "int",
    "budget.max_write_bytes": "int",
    "budget.max_duration_sec": "int",
    "budget.max_amplification_ratio": "decimal",
    "budget.max_reject_rate": "decimal",
    "budget.warn_threshold_pct": "int",
}

_MAX_DEPTH = 8

# 数值型特征用 Decimal 比较。**禁止 float**：``0.1 + 0.2`` 不等于 ``0.3``，
# 而「预算是否超过 3 倍」这种判定不该有平台相关的答案。
_NUMERIC_KINDS = frozenset({"int", "decimal"})


def _s(value: object) -> str:
    """特征值一律字符串化。布尔用 ``"true"``/``"false"``，与前端约定一致。"""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Decimal):
        return format(value, "f")
    return str(value)


def _dec(raw: str) -> Decimal:
    try:
        return Decimal(raw)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"特征值不是数值：{raw!r}") from exc


# --------------------------------------------------------------------------- #
# 条件 DSL
# --------------------------------------------------------------------------- #
def validate_condition(condition: Mapping[str, Any], *, _depth: int = 0) -> None:
    """静态校验条件树。发布策略与 `preview` 都先过它，把错误挡在写库之前。

    与 `evaluate_condition` 共用一套规则，但**不依赖特征取值**——
    因此一条只在 prod 才会暴露的笔误也能在发布时被发现。
    """
    if _depth > _MAX_DEPTH:
        raise ValueError(f"条件嵌套超过 {_MAX_DEPTH} 层")
    if not isinstance(condition, Mapping):
        raise ValueError("条件必须是对象")

    keys = set(condition)
    if not keys:
        return  # 空条件 = 恒真，由 RuleSpec 的注释说明

    combinators = keys & {"all", "any", "not"}
    leaves = keys & {"feature", "op", "value"}
    if combinators and leaves:
        raise ValueError("条件节点不能同时是组合子与叶子")
    if len(combinators) > 1:
        raise ValueError("一个条件节点只能有一个组合子")

    if combinators:
        name = next(iter(combinators))
        extra = keys - {name}
        if extra:
            # 例如 {"all": [...], "feature": "env"}：作者显然以为两者会一起生效
            raise ValueError(f"组合子 {name} 上出现多余键：{sorted(extra)}")
        body = condition[name]
        if name == "not":
            if not isinstance(body, Mapping):
                raise ValueError("not 需要单个条件对象")
            validate_condition(body, _depth=_depth + 1)
            return
        if not isinstance(body, (list, tuple)) or not body:
            raise ValueError(f"{name} 需要非空数组")
        for item in body:
            validate_condition(item, _depth=_depth + 1)
        return

    feature = condition.get("feature")
    op = condition.get("op")
    if not isinstance(feature, str) or not isinstance(op, str):
        raise ValueError("叶子条件需要字符串 feature 与 op")
    if feature not in FEATURES:
        raise ValueError(f"条件引用了未定义的特征：{feature!r}")
    if op not in _CONDITION_OPS:
        raise ValueError(f"未知算子：{op!r}")
    need_value = op not in {"exists", "is_true", "is_false", "is_empty"}
    if need_value and "value" not in condition:
        raise ValueError(f"算子 {op} 需要 value")
    if op in {"is_true", "is_false", "gt", "gte", "lt", "lte"} and FEATURES[feature] == "bool":
        # 对布尔写 gt/is_true 混合形态是笔误的常见来源
        if op in {"gt", "gte", "lt", "lte"}:
            raise ValueError(f"布尔特征 {feature} 不能做数值比较")
    if op in {"gt", "gte", "lt", "lte"} and FEATURES[feature] not in _NUMERIC_KINDS:
        raise ValueError(f"特征 {feature} 不是数值型，不能用 {op}")
    if not need_value:
        return
    raw = condition["value"]
    if op in {"in", "not_in"}:
        if not isinstance(raw, (list, tuple)):
            raise ValueError(f"{op} 需要数组 value")
        return
    if not isinstance(raw, (str, int, float, bool)):
        raise ValueError("value 只能是标量或数组")


def evaluate_condition(condition: Mapping[str, Any], features: Mapping[str, str]) -> bool:
    """求值。未定义特征名抛 `ValueError`，**不静默返回 False**（§6.2）。"""
    validate_condition(condition)
    return _eval(condition, features)


def _eval(condition: Mapping[str, Any], features: Mapping[str, str]) -> bool:
    if not condition:
        # 空条件 = 恒真（无条件规则）。`validate_condition` 已放行，求值必须与之一致，
        # 否则一条「无条件 P0」会在发布时通过、在判定时 KeyError——而它本该拦下所有作业。
        return True
    if "all" in condition:
        return all(_eval(item, features) for item in condition["all"])
    if "any" in condition:
        return any(_eval(item, features) for item in condition["any"])
    if "not" in condition:
        return not _eval(condition["not"], features)

    feature = str(condition["feature"])
    op = str(condition["op"])
    if feature not in features:
        raise ValueError(f"特征未被构建：{feature!r}")
    raw = features[feature]

    if op == "is_true":
        return raw == "true"
    if op == "is_false":
        return raw == "false"
    if op == "is_empty":
        # 只判空串。逗号分隔的取值不该在这里被解释——解释权归 build_features。
        return raw == ""

    value = condition.get("value")
    if op == "exists":
        return feature in features
    if op in {"eq", "ne"}:
        left, right = _coerce_pair(feature, raw, value)
        return (left == right) if op == "eq" else (left != right)
    if op in {"in", "not_in"}:
        wanted = [_coerce(feature, item) for item in value]  # type: ignore[union-attr]
        hit = _coerce(feature, raw) in wanted
        return hit if op == "in" else not hit
    if op == "len_gte":
        return len(raw) >= int(condition["value"])

    left, right = _dec(raw), _dec(_s(value))
    if op == "gt":
        return left > right
    if op == "gte":
        return left >= right
    if op == "lt":
        return left < right
    if op == "lte":
        return left <= right
    raise ValueError(f"未实现的算子：{op!r}")  # pragma: no cover


def _coerce(feature: str, value: object) -> object:
    kind = FEATURES[feature]
    raw = _s(value)
    if kind in _NUMERIC_KINDS:
        return _dec(raw)
    if kind == "bool":
        if raw not in {"true", "false"}:
            raise ValueError(f"布尔特征 {feature} 的取值只能是 true/false，得到 {raw!r}")
        return raw == "true"
    return raw


def _coerce_pair(feature: str, raw: str, value: object) -> tuple[object, object]:
    return _coerce(feature, raw), _coerce(feature, value)


# --------------------------------------------------------------------------- #
# 特征向量
# --------------------------------------------------------------------------- #
def _is_p0_sensitive_egress(column: SensitiveColumn) -> bool:
    """§6.3 P0-2 的**逐列**判据。

    两种命中方式：
    ① 分级 >= confidential，且明文、且出域、且未脱敏；
    ② `kind` 在硬编码白名单内（身份证/手机号/邮箱/地址/令牌/银行卡）
       ——**即使被标注成 internal 也命中**。分级是人工标注的结果，
       而「身份证是不是敏感」不是一道需要人来判断的题。
    """
    if not (column.contains_plaintext and column.egress and not column.masked):
        return False
    # 硬白名单优先于分级：即使这一列被标注成 internal，身份证/手机号依然命中。
    # 这一条**不能**退化成「看 level」，否则一次错误的标注就能让凭证类字段出域。
    if column.kind is not None and column.kind in ALWAYS_SENSITIVE_KINDS:
        return True
    level = str(column.level.value if isinstance(column.level, SensitiveLevel) else column.level)
    return LEVEL_RANK.get(level, 0) >= LEVEL_RANK[SensitiveLevel.CONFIDENTIAL.value]


def budget_ceiling(
    defaults: BudgetDefaults,
    project_overrides: Mapping[str, str] | None = None,
) -> BudgetRequest:
    """上限 = 默认值 ⊕ 项目级覆盖（**逐维度**，未覆盖的维度保持默认）。

    逐维度而非整体替换：整体替换意味着调用方漏写一个维度，那一维就变成 0，
    而「预算 0」在下游是一个合法且致命的取值（任何一次写入都会被判超限）。
    """
    overrides = dict(project_overrides or {})
    dims: dict[str, object] = {
        "max_read_rows": defaults.max_read_rows,
        "max_write_bytes": defaults.max_write_bytes,
        "max_duration_sec": defaults.max_duration_sec,
        "max_amplification_ratio": defaults.max_amplification_ratio,
        "max_reject_rate": defaults.max_reject_rate,
    }
    for name in list(dims):
        if overrides.get(name, "") != "":
            dims[name] = overrides[name]
    return BudgetRequest(**dims)  # type: ignore[arg-type]


def effective_budget(
    requested: BudgetRequest | None,
    defaults: BudgetDefaults,
    project_overrides: Mapping[str, str] | None = None,
) -> BudgetRequest:
    """生效预算 = 逐维度 ``min(申报值, 上限)``。

    **为什么是 min 而不是直接取上限**（be-07 §6.4 第 7 步的字面写法是
    「defaults ⊕ project_overrides」）：be-16 的运行监督会拿这份生效预算去硬中断。
    若申报值比上限小、而生效预算被抬到上限，监督就放宽了**用户自己设的那道闸**
    ——用户申报 100 万行，监督却允许 1 亿行，然后在他没预期的地方跑满一小时。
    取 max 则相反：会放宽策略设定的红线，那是安全问题。

    申报缺失（None）时退化为上限：没有申报就不该有比上限更紧的约束。
    """
    ceiling = budget_ceiling(defaults, project_overrides)
    if requested is None:
        return ceiling
    ratios: tuple[tuple[str, Decimal, Decimal], ...] = (
        ("max_read_rows", Decimal(requested.max_read_rows), Decimal(ceiling.max_read_rows)),
        ("max_write_bytes", Decimal(requested.max_write_bytes), Decimal(ceiling.max_write_bytes)),
        ("max_duration_sec", Decimal(requested.max_duration_sec), Decimal(ceiling.max_duration_sec)),
        ("max_amplification_ratio", requested.max_amplification_ratio, ceiling.max_amplification_ratio),
        ("max_reject_rate", requested.max_reject_rate, ceiling.max_reject_rate),
    )
    merged: dict[str, object] = {name: min(a, b) for name, a, b in ratios}
    return BudgetRequest(**merged)  # type: ignore[arg-type]


def over_default_ratio(requested: BudgetRequest | None, defaults: BudgetDefaults) -> Decimal:
    """申报值 / 默认值 的最大比值（§6.5）。申报缺失视为 1.0，不产生超限。"""
    if requested is None:
        return Decimal(1)
    pairs: tuple[tuple[Decimal, Decimal], ...] = (
        (Decimal(requested.max_read_rows), Decimal(defaults.max_read_rows)),
        (Decimal(requested.max_write_bytes), Decimal(defaults.max_write_bytes)),
        (Decimal(requested.max_duration_sec), Decimal(defaults.max_duration_sec)),
        (requested.max_amplification_ratio, defaults.max_amplification_ratio),
        (requested.max_reject_rate, defaults.max_reject_rate),
    )
    ratios = [num / den for num, den in pairs if den > 0]
    return max(ratios) if ratios else Decimal(1)


def build_features(
    intent: ToolIntent,
    scope: ResourceScope,
    env: Env,
    level: SensitiveLevel,
    *,
    defaults: BudgetDefaults,
    project_overrides: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """构建特征向量。**返回的键集与 `FEATURES` 完全一致**——
    少一个键会让引用它的规则在求值时抛 `ValueError`，而那条规则可能正是防线本身。"""
    budget = effective_budget(intent.budget, defaults, project_overrides)
    egress_columns = [col for col in scope.columns if _is_p0_sensitive_egress(col)]
    features: dict[str, str] = {
        "env": env.value,
        "level": level.value,
        "intent.kind": intent.kind,
        "intent.is_write": _s(intent.is_write),
        "intent.tool": intent.tool.value if intent.tool is not None else "",
        "intent.bypass_approval": _s(intent.bypass_approval),
        "intent.disable_audit": _s(intent.disable_audit),
        "intent.export_plaintext_sensitive": _s(intent.export_plaintext_sensitive),
        "intent.cross_tenant": _s(intent.cross_tenant),
        "intent.has_rollback_plan": _s(intent.has_rollback_plan),
        "scope.writes_formal_table": _s(scope.writes_formal_table),
        "scope.writes_shadow_table": _s(scope.writes_shadow_table),
        "scope.out_of_scope_count": _s(len(scope.out_of_scope)),
        "scope.cross_tenant_count": _s(len(scope.cross_tenant_refs)),
        "scope.unregistered_secret_count": _s(len(scope.unregistered_secret_refs)),
        "scope.sensitive_plaintext_egress_count": _s(len(egress_columns)),
        "scope.read_table_count": _s(len(scope.reads)),
        "scope.write_table_count": _s(len(scope.writes)),
        "budget.over_default_ratio_max": _s(over_default_ratio(intent.budget, defaults)),
        "budget.max_read_rows": _s(budget.max_read_rows),
        "budget.max_write_bytes": _s(budget.max_write_bytes),
        "budget.max_duration_sec": _s(budget.max_duration_sec),
        "budget.max_amplification_ratio": _s(budget.max_amplification_ratio),
        "budget.max_reject_rate": _s(budget.max_reject_rate),
        "budget.warn_threshold_pct": _s(defaults.warn_threshold_pct),
    }
    missing = set(FEATURES) ^ set(features)
    if missing:  # pragma: no cover - 由上面的常量保证；留作新增特征时的护栏
        raise AssertionError(f"特征向量与 FEATURES 不一致：{sorted(missing)}")
    return features


# --------------------------------------------------------------------------- #
# 内置基线规则（§6.3 的唯一权威映射）
# --------------------------------------------------------------------------- #
def _leaf(feature: str, op: str, value: object | None = None) -> dict[str, Any]:
    node: dict[str, Any] = {"feature": feature, "op": op}
    if value is not None:
        node["value"] = value
    return node


_TWO_SLOTS = (ApprovalSlot.CHECKER_DATA, ApprovalSlot.CHECKER_SECURITY)

BUILTIN_RULES: tuple[RuleSpec, ...] = (
    # ---- P0：任一命中即阻断，不进审批、不进执行 ----
    RuleSpec(
        code="p0.prod_formal_write",
        level=RiskLevel.P0,
        title="生产环境直写正式表",
        description="目标端为正式表且环境为 prod。生产发布必须走影子表 + 质量分流 + 原子换表。",
        condition={
            "all": [
                _leaf("env", "eq", Env.PROD.value),
                _leaf("scope.writes_formal_table", "is_true"),
            ]
        },
    ),
    RuleSpec(
        code="p0.sensitive_plaintext_egress",
        level=RiskLevel.P0,
        title="高敏字段明文出域",
        description="身份证/手机号/邮箱/地址/令牌等高敏字段以明文离开控制面边界（未脱敏）。",
        condition={"all": [_leaf("scope.sensitive_plaintext_egress_count", "gt", 0)]},
    ),
    RuleSpec(
        code="p0.bypass_intent",
        level=RiskLevel.P0,
        title="意图中包含绕过审批 / 关闭审计 / 明文导出敏感数据",
        description="意图本身要求绕过治理机制。这类请求没有「加审批」的补救方式，只能拒绝。",
        condition={
            "any": [
                _leaf("intent.bypass_approval", "is_true"),
                _leaf("intent.disable_audit", "is_true"),
                _leaf("intent.export_plaintext_sensitive", "is_true"),
            ]
        },
    ),
    RuleSpec(
        code="p0.cross_tenant",
        level=RiskLevel.P0,
        title="跨租户访问",
        description="意图声明跨租户，或资源范围里出现其他租户的表。",
        condition={
            "any": [
                _leaf("intent.cross_tenant", "is_true"),
                _leaf("scope.cross_tenant_count", "gt", 0),
            ]
        },
    ),
    # ---- P1：允许进入审批，强制双槽 + 加固 ----
    RuleSpec(
        code="p1.out_of_scope_resource",
        level=RiskLevel.P1,
        title="资源超出项目授权范围",
        description="涉及跨库 / 跨 schema 的表。",
        condition={"all": [_leaf("scope.out_of_scope_count", "gt", 0)]},
        required_slots=_TWO_SLOTS,
    ),
    RuleSpec(
        code="p1.budget_over_3x",
        level=RiskLevel.P1,
        title="申报预算超过默认阈值 3 倍",
        description="五个预算维度中任一超过默认值的 3 倍。",
        condition={"all": [_leaf("budget.over_default_ratio_max", "gt", 3)]},
        required_slots=_TWO_SLOTS,
    ),
    RuleSpec(
        code="p1.no_rollback_plan",
        level=RiskLevel.P1,
        title="写入意图缺少回滚方案",
        description="**限写入意图**：只读探查不需要回滚方案。",
        condition={
            "all": [
                _leaf("intent.is_write", "is_true"),
                _leaf("intent.has_rollback_plan", "is_false"),
            ]
        },
        required_slots=_TWO_SLOTS,
    ),
    RuleSpec(
        code="p1.unregistered_secret",
        level=RiskLevel.P1,
        title="引用了未登记的密钥引用",
        description="作业引用了 secret_reference 表里不存在的条目。",
        condition={"all": [_leaf("scope.unregistered_secret_count", "gt", 0)]},
        required_slots=_TWO_SLOTS,
    ),
    RuleSpec(
        code="p1.prod_shadow_only",
        level=RiskLevel.P1,
        title="生产环境写入（影子表）",
        description="prod 下仅写影子表：允许但必须双审批与加固。",
        condition={
            "all": [
                _leaf("env", "eq", Env.PROD.value),
                _leaf("scope.writes_shadow_table", "is_true"),
                _leaf("scope.writes_formal_table", "is_false"),
            ]
        },
        required_slots=_TWO_SLOTS,
    ),
    # ---- P2：常规双槽 ----
    RuleSpec(
        code="p2.in_scope_with_rollback",
        level=RiskLevel.P2,
        title="范围内的常规作业（有回滚方案）",
        description="非 prod、无越界资源、预算未超限，且写入意图带回了回滚方案。",
        condition={
            "all": [
                _leaf("env", "ne", Env.PROD.value),
                _leaf("scope.out_of_scope_count", "eq", 0),
                _leaf("budget.over_default_ratio_max", "lte", 1),
                # 🔴 `kind ne readonly_probe` 是**必须的**：没有它，这条规则对
                # 「非写入 + 范围干净 + 预算不超」的只读探查也成立，于是
                # `most_severe(P2, P3) = P2`，**P3 在非 prod 下永远不可达**——
                # 一次 dev 环境的连通性测试会要求两个审批槽。
                # 这条只有真跑起来才看得出来：PROD 下 env ne prod 为假，
                # 规则不命中、P3 正常生效，所以光看 prod 用例会以为是对的。
                _leaf("intent.kind", "ne", "readonly_probe"),
                {
                    "any": [
                        _leaf("intent.is_write", "is_false"),
                        _leaf("intent.has_rollback_plan", "is_true"),
                    ]
                },
            ]
        },
        required_slots=_TWO_SLOTS,
    ),
    # ---- P3：免审批，仅记审计事件 ----
    RuleSpec(
        code="p3.readonly_probe",
        level=RiskLevel.P3,
        title="纯只读探查",
        description="数据源连通性测试与元数据探查。**生产环境的元数据探查同样适用**"
        "（p0.prod_formal_write 要求 writes_formal_table，不冲突）。",
        condition={
            "all": [
                _leaf("intent.kind", "eq", "readonly_probe"),
                _leaf("intent.is_write", "is_false"),
                _leaf(
                    "intent.tool",
                    "in",
                    [
                        "datasource.connectivity_test",
                        "metadata.probe",
                    ],
                ),
            ]
        },
    ),
)


# `p2.default_write` / `p2.default_read` 是**合成规则**：它们不在 DB 里、不可配置
# （§10.5 说明为什么不该用 DSL 表达「其他规则均未命中」）。但仍要以 RuleHit 形式
# 出现在结果里——否则审批人看到的是一条「什么都没命中」的判定，
# 无法区分「确实干净」与「规则表加载失败」。
FALLBACK_HIT_WRITE = "p2.default_write"
FALLBACK_HIT_READ = "p2.default_read"


def fallback_rule(intent: ToolIntent) -> RuleSpec:
    """兜底规则：写入意图永远不落到 P3；非写入意图落 P2 而不是伪装成写入。

    到达这里意味着**一条规则都没命中**。对非写入意图，最常见的成因是
    「自称只读探查、但 `tool` 不在 P3 白名单里」——那正是需要双审批的情形：
    调用方给意图贴的 `kind` 是自我声明，不能当作免审批的凭据。
    """
    if intent.is_write:
        return RuleSpec(
            code=FALLBACK_HIT_WRITE,
            level=RiskLevel.P2,
            title="常规写入（无更高等级规则命中）",
            description="兜底规则，保证写入意图不会因「规则都没命中」而落到 P3。",
            required_slots=_TWO_SLOTS,
        )
    return RuleSpec(
        code=FALLBACK_HIT_READ,
        level=RiskLevel.P2,
        title="常规读取 / 受限操作（无更高等级规则命中）",
        description="兜底规则。非只读探查类意图（如 admin），或自称只读但工具不在白名单内，"
        "都不适用 P3 的免审批。",
        required_slots=_TWO_SLOTS,
    )


def merge_rules(
    builtin: Iterable[RuleSpec],
    overrides: Iterable[RuleSpec],
) -> tuple[tuple[RuleSpec, ...], tuple[RuleMergeWarning, ...]]:
    """把 DB 里的规则行合并到内置基线之上（§6.6）。

    基线（`p0.` / `p3.`）只允许**加严**：
    - ``enabled=False`` → 忽略，告警；
    - ``level`` 比基线松 → 忽略，告警；
    - 条件更长、等级更高 → 采纳。

    非基线规则（`p1.` / `p2.` / 自定义）整条覆盖内置同名规则。
    """
    base = {rule.code: rule for rule in builtin}
    warnings: list[RuleMergeWarning] = []

    for row in overrides:
        current = base.get(row.code)
        if current is None:
            base[row.code] = row
            continue
        if not current.is_baseline:
            base[row.code] = row
            continue
        if not row.enabled:
            warnings.append(RuleMergeWarning(code=row.code, reason="基线规则不可关闭，已忽略 enabled=False"))
            continue
        if row.level.rank > current.level.rank:
            kept = current.level.value
            warnings.append(
                RuleMergeWarning(
                    code=row.code,
                    reason=f"基线规则不可降级（{kept} → {row.level.value}），已保留 {kept}",
                )
            )
            continue
        base[row.code] = row

    ordered = tuple(sorted(base.values(), key=lambda r: (r.level.rank, r.code)))
    return ordered, tuple(warnings)
