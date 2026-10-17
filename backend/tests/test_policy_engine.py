"""PDP 引擎单元测试（be-07 §9 第 1–12 项）。

纯函数、无 IO，本机可跑。三条最重要的断言：

1. **P0 穷举**：对 7 个触发特征做全笛卡尔积，断言 `blocked` 与一个**独立重写的**
   判据逐一致。这把「P0/P1 拦截率 100%」从抽样证明变成维度穷举证明——
   抽样只能说明「我想到的那几种被拦住了」。
2. **单调性**：任一触发特征由假变真，等级不得变松。反例保护，防止将来加规则时
   引入「加了条件反而更宽松」的写法。
3. **基线不可关闭**：把 `p0.*` 写成 `enabled=False` 或降级，`merge_rules` 必须忽略并告警。
"""

from __future__ import annotations

import itertools
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from engines.policy import (
    BUILTIN_RULES,
    BudgetDefaults,
    BudgetRequest,
    ResourceRef,
    ResourceScope,
    RuleSpec,
    SensitiveColumn,
    ToolIntent,
    build_features,
    decide,
    effective_budget,
    evaluate_condition,
    merge_rules,
    validate_condition,
)
from models.enums import (
    ApprovalSlot,
    DataSourceType,
    Env,
    RiskLevel,
    SensitiveLevel,
    ToolName,
)

NOW = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)

DEFAULTS = BudgetDefaults(
    max_read_rows=1_000_000,
    max_write_bytes=10_000_000,
    max_duration_sec=3600,
    max_amplification_ratio=Decimal("1.5"),
    max_reject_rate=Decimal("0.05"),
    warn_threshold_pct=80,
)


def _ref(table: str = "orders", env: Env = Env.DEV) -> ResourceRef:
    return ResourceRef(
        data_source_id=1,
        data_source_type=DataSourceType.MYSQL,
        database="shop",
        schema="public",
        table=table,
        env=env,
    )


def _scope(**over) -> ResourceScope:
    base: dict = {
        "tenant_id": 1,
        "project_id": 7,
        "reads": (_ref(),),
        "writes": (_ref("orders_wide"),),
    }
    base.update(over)
    return ResourceScope(**base)


def _intent(**over) -> ToolIntent:
    base: dict = {
        "kind": "etl_write",
        "is_write": True,
        "has_rollback_plan": True,
        "budget": BudgetRequest(
            max_read_rows=1_000_000,
            max_write_bytes=10_000_000,
            max_duration_sec=3600,
            max_amplification_ratio=Decimal("1.5"),
            max_reject_rate=Decimal("0.05"),
        ),
    }
    base.update(over)
    return ToolIntent(**base)


def _plaintext_phone(**over) -> SensitiveColumn:
    base: dict = {
        "name": "phone",
        "level": SensitiveLevel.INTERNAL,
        "kind": "phone",
        "contains_plaintext": True,
        "masked": False,
        "egress": True,
    }
    base.update(over)
    return SensitiveColumn(**base)


def _decide(intent: ToolIntent | None = None, scope: ResourceScope | None = None, **over):
    kwargs = {
        "intent": intent or _intent(),
        "scope": scope or _scope(),
        "env": Env.DEV,
        "level": SensitiveLevel.INTERNAL,
        "now": NOW,
        "defaults": DEFAULTS,
    }
    kwargs.update(over)
    return decide(**kwargs)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# 1. P0 穷举
# --------------------------------------------------------------------------- #
def _expected_blocked(flags: dict[str, bool], env: Env) -> bool:
    """独立重写的 P0 判据。**刻意不复用引擎里的任何函数**——
    调用引擎去算期望值等于让被测对象自证。"""
    return any(
        (
            env is Env.PROD and flags["writes_formal_table"],
            flags["sensitive_egress"],
            flags["bypass_approval"],
            flags["disable_audit"],
            flags["export_plaintext"],
            flags["intent_cross_tenant"],
            flags["scope_cross_tenant"],
        )
    )


_P0_FLAG_NAMES = (
    "writes_formal_table",
    "sensitive_egress",
    "bypass_approval",
    "disable_audit",
    "export_plaintext",
    "intent_cross_tenant",
    "scope_cross_tenant",
)


def test_P0穷举_七个触发特征的全笛卡尔积() -> None:
    combos = list(itertools.product([False, True], repeat=len(_P0_FLAG_NAMES)))
    checked = 0
    for values in combos:
        flags = dict(zip(_P0_FLAG_NAMES, values, strict=True))
        for env in (Env.DEV, Env.PROD):
            intent = _intent(
                bypass_approval=flags["bypass_approval"],
                disable_audit=flags["disable_audit"],
                export_plaintext_sensitive=flags["export_plaintext"],
                cross_tenant=flags["intent_cross_tenant"],
            )
            scope = _scope(
                writes_formal_table=flags["writes_formal_table"],
                cross_tenant_refs=(_ref("other_tenant_table"),) if flags["scope_cross_tenant"] else (),
                columns=(_plaintext_phone(),) if flags["sensitive_egress"] else (),
            )
            decision = _decide(intent=intent, scope=scope, env=env)
            assert decision.blocked is _expected_blocked(flags, env), (
                f"组合 {flags} env={env.value} 判定为 {decision.level.value}，"
                f"命中 {[h.code for h in decision.hits]}"
            )
            if decision.blocked:
                assert decision.level is RiskLevel.P0
                # P0 不进审批：槽位数没有意义
                assert decision.required_slots == ()
                assert decision.hardening_required is False
            checked += 1
    assert checked == 256  # 2**7 组合 × 2 环境


def test_P0命中在证据里留下特征取值() -> None:
    decision = _decide(scope=_scope(writes_formal_table=True), env=Env.PROD)
    hit = next(h for h in decision.hits if h.code == "p0.prod_formal_write")
    assert hit.evidence["env"] == "prod"
    assert hit.evidence["scope.writes_formal_table"] == "true"


# --------------------------------------------------------------------------- #
# 2. P1 各自可触发
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("code", "intent", "scope", "env"),
    [
        ("p1.out_of_scope_resource", _intent(), _scope(out_of_scope=(_ref("other_db_t"),)), Env.DEV),
        (
            "p1.budget_over_3x",
            _intent(
                budget=BudgetRequest(
                    max_read_rows=4_000_000,  # 4 倍
                    max_write_bytes=1,
                    max_duration_sec=1,
                    max_amplification_ratio=Decimal("1"),
                    max_reject_rate=Decimal("0.01"),
                )
            ),
            _scope(),
            Env.DEV,
        ),
        ("p1.no_rollback_plan", _intent(has_rollback_plan=False), _scope(), Env.DEV),
        ("p1.unregistered_secret", _intent(), _scope(unregistered_secret_refs=(99,)), Env.DEV),
        (
            "p1.prod_shadow_only",
            _intent(),
            _scope(writes_shadow_table=True, writes_formal_table=False),
            Env.PROD,
        ),
    ],
)
def test_P1规则各自可命中(code, intent, scope, env) -> None:
    decision = _decide(intent=intent, scope=scope, env=env)
    assert code in {hit.code for hit in decision.hits}
    assert decision.level is RiskLevel.P1
    assert decision.required_slots == (ApprovalSlot.CHECKER_DATA, ApprovalSlot.CHECKER_SECURITY)
    assert decision.hardening_required is True
    assert decision.blocked is False


def test_P1_资源越界与预算超限同时命中时等级不变() -> None:
    decision = _decide(
        scope=_scope(out_of_scope=(_ref("t"),)),
        env=Env.PROD,
        intent=_intent(
            budget=BudgetRequest(
                max_read_rows=10_000_000,
                max_write_bytes=1,
                max_duration_sec=1,
                max_amplification_ratio=Decimal("1"),
                max_reject_rate=Decimal("0.01"),
            )
        ),
    )
    codes = {hit.code for hit in decision.hits}
    assert {"p1.out_of_scope_resource", "p1.budget_over_3x"} <= codes
    assert decision.level is RiskLevel.P1


# --------------------------------------------------------------------------- #
# 3–4. P3 与兜底
# --------------------------------------------------------------------------- #
def test_P3纯只读探查免审批() -> None:
    decision = _decide(
        intent=_intent(kind="readonly_probe", is_write=False, tool=ToolName.METADATA_PROBE),
    )
    assert decision.level is RiskLevel.P3
    assert decision.required_slots == ()
    assert decision.blocked is False
    assert decision.hardening_required is False


def test_只读探查在prod也仍是P3() -> None:
    """ `p0.prod_formal_write` 要求 `writes_formal_table`，与只读探查不冲突。"""
    decision = _decide(
        intent=_intent(kind="readonly_probe", is_write=False, tool=ToolName.METADATA_PROBE),
        env=Env.PROD,
    )
    assert decision.level is RiskLevel.P3


def test_兜底不漏_干净的写入意图是P2而不是P3() -> None:
    decision = _decide()
    assert decision.level is RiskLevel.P2
    assert decision.blocked is False
    assert [hit.code for hit in decision.hits] == ["p2.in_scope_with_rollback"]
    assert decision.required_slots == (ApprovalSlot.CHECKER_DATA, ApprovalSlot.CHECKER_SECURITY)


def test_兜底_自称只读但工具不在白名单时走兜底而不是P3() -> None:
    """ `kind` 是调用方的**自我声明**，不构成免审批的凭据。"""
    decision = _decide(
        intent=_intent(kind="readonly_probe", is_write=False, tool=ToolName.SEATUNNEL_STOP),
    )
    assert decision.level is RiskLevel.P2
    assert [hit.code for hit in decision.hits] == ["p2.default_read"]
    assert decision.required_slots == (ApprovalSlot.CHECKER_DATA, ApprovalSlot.CHECKER_SECURITY)


def test_兜底_admin类非写入意图不落P3() -> None:
    decision = _decide(intent=_intent(kind="admin", is_write=False, has_rollback_plan=False))
    assert decision.level is RiskLevel.P2
    assert "p3.readonly_probe" not in {hit.code for hit in decision.hits}


# --------------------------------------------------------------------------- #
# 5. 单调性
# --------------------------------------------------------------------------- #
def test_单调性_任一触发特征由假变真等级不放松() -> None:
    base_intent = _intent()
    base_scope = _scope()
    base = _decide(intent=base_intent, scope=base_scope)

    tightenings = [
        {"intent": _intent(bypass_approval=True), "scope": base_scope},
        {"intent": _intent(disable_audit=True), "scope": base_scope},
        {"intent": _intent(export_plaintext_sensitive=True), "scope": base_scope},
        {"intent": _intent(cross_tenant=True), "scope": base_scope},
        {"intent": _intent(has_rollback_plan=False), "scope": base_scope},
        {"intent": _intent(is_write=False, kind="admin"), "scope": base_scope},
        {"intent": base_intent, "scope": _scope(out_of_scope=(_ref("t"),))},
        {"intent": base_intent, "scope": _scope(cross_tenant_refs=(_ref("t"),))},
        {"intent": base_intent, "scope": _scope(unregistered_secret_refs=(1,))},
        {"intent": base_intent, "scope": _scope(columns=(_plaintext_phone(),))},
    ]
    for case in tightenings:
        decision = _decide(intent=case["intent"], scope=case["scope"])
        assert decision.level.rank <= base.level.rank, (
            f"收紧后等级反而放松：{base.level.value} → {decision.level.value}"
        )


def test_单调性_加严环境不放松() -> None:
    dev = _decide(scope=_scope(writes_shadow_table=True), env=Env.DEV)
    prod = _decide(scope=_scope(writes_shadow_table=True), env=Env.PROD)
    assert prod.level.rank <= dev.level.rank


# --------------------------------------------------------------------------- #
# 6. 确定性与顺序无关
# --------------------------------------------------------------------------- #
def test_确定性_同输入一百次输出完全一致() -> None:
    first = _decide().model_dump()
    for _ in range(100):
        assert _decide().model_dump() == first


def test_顺序无关_打乱规则顺序结果不变() -> None:
    shuffled = tuple(reversed(BUILTIN_RULES))
    assert _decide(rules=shuffled).model_dump() == _decide(rules=BUILTIN_RULES).model_dump()


def test_命中的排序是等级的rank升序() -> None:
    decision = _decide(
        intent=_intent(has_rollback_plan=False),
        scope=_scope(out_of_scope=(_ref("t"),)),
        env=Env.PROD,
    )
    ranks = [hit.level.rank for hit in decision.hits]
    assert ranks == sorted(ranks)
    assert len(decision.reasons) == len(decision.hits)


# --------------------------------------------------------------------------- #
# 7–8. 边界与敏感分级
# --------------------------------------------------------------------------- #
def test_只读探查不需要回滚方案() -> None:
    decision = _decide(
        intent=_intent(
            kind="readonly_probe",
            is_write=False,
            has_rollback_plan=False,
            tool=ToolName.DATASOURCE_CONNECTIVITY_TEST,
        ),
    )
    assert "p1.no_rollback_plan" not in {hit.code for hit in decision.hits}


def test_预算恰好三倍不命中() -> None:
    """ `>` 不是 `>=`：恰好 3 倍属允许范围。"""
    decision = _decide(
        intent=_intent(
            budget=BudgetRequest(
                max_read_rows=3_000_000,
                max_write_bytes=1,
                max_duration_sec=1,
                max_amplification_ratio=Decimal("1"),
                max_reject_rate=Decimal("0.01"),
            )
        ),
    )
    assert "p1.budget_over_3x" not in {hit.code for hit in decision.hits}


def test_敏感分级边界_internal的email明文出域命中P0() -> None:
    decision = _decide(
        scope=_scope(
            columns=(
                SensitiveColumn(
                    name="email",
                    level=SensitiveLevel.INTERNAL,
                    kind="email",
                    contains_plaintext=True,
                    masked=False,
                    egress=True,
                ),
            )
        ),
    )
    assert decision.blocked is True
    assert "p0.sensitive_plaintext_egress" in {hit.code for hit in decision.hits}


def test_敏感分级边界_confidential但已脱敏不命中() -> None:
    decision = _decide(
        scope=_scope(
            columns=(
                SensitiveColumn(
                    name="id_card",
                    level=SensitiveLevel.RESTRICTED,
                    kind="id_card",
                    contains_plaintext=True,
                    masked=True,
                    egress=True,
                ),
            )
        ),
    )
    assert decision.blocked is False


def test_敏感分级边界_未出域不命中() -> None:
    decision = _decide(scope=_scope(columns=(_plaintext_phone(egress=False),)))
    assert decision.blocked is False


def test_敏感分级边界_other类型且internal不出域判定() -> None:
    decision = _decide(scope=_scope(columns=(_plaintext_phone(kind="other", level=SensitiveLevel.INTERNAL),)))
    assert decision.blocked is False


# --------------------------------------------------------------------------- #
# 9. 条件 DSL
# --------------------------------------------------------------------------- #
_FEATURES = {
    "env": "prod",
    "level": "restricted",
    "intent.kind": "etl_write",
    "intent.is_write": "true",
    "intent.tool": "seatunnel.submit",
    "scope.write_table_count": "3",
    "budget.max_reject_rate": "0.0500",
}


@pytest.mark.parametrize(
    ("condition", "expected"),
    [
        ({"feature": "env", "op": "eq", "value": "prod"}, True),
        ({"feature": "env", "op": "eq", "value": "dev"}, False),
        ({"feature": "env", "op": "ne", "value": "dev"}, True),
        ({"feature": "env", "op": "in", "value": ["dev", "prod"]}, True),
        ({"feature": "env", "op": "not_in", "value": ["dev"]}, True),
        ({"feature": "scope.write_table_count", "op": "gt", "value": 2}, True),
        ({"feature": "scope.write_table_count", "op": "gte", "value": 3}, True),
        ({"feature": "scope.write_table_count", "op": "lt", "value": 3}, False),
        ({"feature": "scope.write_table_count", "op": "lte", "value": 3}, True),
        ({"feature": "env", "op": "exists"}, True),
        ({"feature": "intent.is_write", "op": "is_true"}, True),
        ({"feature": "intent.is_write", "op": "is_false"}, False),
        ({"feature": "intent.tool", "op": "is_empty"}, False),
        ({"feature": "scope.write_table_count", "op": "len_gte", "value": 1}, True),
        ({"feature": "budget.max_reject_rate", "op": "lte", "value": "0.05"}, True),
        ({"all": [{"feature": "env", "op": "eq", "value": "prod"}, {"feature": "intent.is_write", "op": "is_true"}]}, True),
        ({"any": [{"feature": "env", "op": "eq", "value": "dev"}, {"feature": "intent.is_write", "op": "is_true"}]}, True),
        ({"not": {"feature": "env", "op": "eq", "value": "dev"}}, True),
        ({}, True),  # 空条件 = 恒真
    ],
)
def test_条件DSL算子逐一正反例(condition, expected) -> None:
    assert evaluate_condition(condition, _FEATURES) is expected


def test_条件DSL_未定义特征抛错而不是静默false() -> None:
    with pytest.raises(ValueError, match="未定义的特征"):
        evaluate_condition({"feature": "intent.secret_flag", "op": "is_true"}, _FEATURES)


def test_条件DSL_特征未构建时抛错() -> None:
    with pytest.raises(ValueError, match="未被构建"):
        evaluate_condition({"feature": "intent.tool", "op": "is_empty"}, {"env": "prod"})


def test_条件DSL_未知算子抛错() -> None:
    with pytest.raises(ValueError, match="未知算子"):
        validate_condition({"feature": "env", "op": "startswith", "value": "p"})


def test_条件DSL_嵌套超深抛错() -> None:
    node: dict = {"feature": "env", "op": "eq", "value": "prod"}
    for _ in range(10):
        node = {"all": [node]}
    with pytest.raises(ValueError, match="嵌套超过"):
        validate_condition(node)


def test_条件DSL_组合子与叶子混用抛错() -> None:
    with pytest.raises(ValueError, match="不能同时是组合子与叶子"):
        validate_condition({"all": [], "feature": "env"})


def test_条件DSL_布尔特征不能做数值比较() -> None:
    with pytest.raises(ValueError, match="不能做数值比较"):
        validate_condition({"feature": "intent.is_write", "op": "gt", "value": 0})


def test_条件DSL_非数组的in抛错() -> None:
    with pytest.raises(ValueError, match="需要数组"):
        validate_condition({"feature": "env", "op": "in", "value": "prod"})


def test_所有内置规则的条件都能通过校验() -> None:
    for rule in BUILTIN_RULES:
        validate_condition(rule.condition)


# --------------------------------------------------------------------------- #
# 10. 基线不可关闭
# --------------------------------------------------------------------------- #
def _override(code: str, **over) -> RuleSpec:
    base = next(rule for rule in BUILTIN_RULES if rule.code == code)
    payload = base.model_dump()
    payload.update(over)
    return RuleSpec(**payload)


def test_基线不可关闭_禁用被忽略并告警() -> None:
    merged, warnings = merge_rules(BUILTIN_RULES, [_override("p0.prod_formal_write", enabled=False)])
    rule = next(r for r in merged if r.code == "p0.prod_formal_write")
    assert rule.enabled is True
    assert [w.code for w in warnings] == ["p0.prod_formal_write"]


def test_基线不可降级() -> None:
    merged, warnings = merge_rules(BUILTIN_RULES, [_override("p0.prod_formal_write", level=RiskLevel.P2)])
    rule = next(r for r in merged if r.code == "p0.prod_formal_write")
    assert rule.level is RiskLevel.P0
    assert warnings


def test_基线可以加严() -> None:
    stricter = _override("p3.readonly_probe", level=RiskLevel.P1)
    merged, warnings = merge_rules(BUILTIN_RULES, [stricter])
    rule = next(r for r in merged if r.code == "p3.readonly_probe")
    assert rule.level is RiskLevel.P1
    assert warnings == ()


def test_非基线规则整条覆盖() -> None:
    merged, warnings = merge_rules(BUILTIN_RULES, [_override("p1.budget_over_3x", enabled=False)])
    rule = next(r for r in merged if r.code == "p1.budget_over_3x")
    assert rule.enabled is False
    assert warnings == ()


def test_自定义规则可新增() -> None:
    extra = RuleSpec(
        code="p1.custom_rule",
        level=RiskLevel.P1,
        title="自定义",
        condition={"feature": "scope.read_table_count", "op": "gt", "value": 5},
    )
    merged, warnings = merge_rules(BUILTIN_RULES, [extra])
    assert "p1.custom_rule" in {r.code for r in merged}
    assert warnings == ()


def test_关闭的基线在判定里仍然生效() -> None:
    merged, _ = merge_rules(BUILTIN_RULES, [_override("p0.cross_tenant", enabled=False)])
    decision = _decide(intent=_intent(cross_tenant=True), rules=merged)
    assert decision.blocked is True


# --------------------------------------------------------------------------- #
# 11. 生效预算
# --------------------------------------------------------------------------- #
def test_生效预算取申报与上限的较小者() -> None:
    requested = BudgetRequest(
        max_read_rows=1,
        max_write_bytes=99,
        max_duration_sec=1,
        max_amplification_ratio=Decimal("9"),
        max_reject_rate=Decimal("0.9"),
    )
    merged = effective_budget(requested, DEFAULTS)
    assert merged.max_write_bytes == 99  # 申报更小 → 用申报
    assert merged.max_read_rows == 1
    assert merged.max_reject_rate == Decimal("0.05")  # 上限更小 → 用上限


def test_生效预算_申报缺失时退化为上限() -> None:
    assert effective_budget(None, DEFAULTS).model_dump() == effective_budget(None, DEFAULTS).model_dump()
    assert effective_budget(None, DEFAULTS).max_read_rows == DEFAULTS.max_read_rows


def test_项目级覆盖逐维度生效() -> None:
    merged = effective_budget(None, DEFAULTS, {"max_duration_sec": "60"})
    assert merged.max_duration_sec == 60
    assert merged.max_read_rows == DEFAULTS.max_read_rows  # 未覆盖的维度保持默认


def test_预算比率的max与Decimal计算() -> None:
    requested = BudgetRequest(
        max_read_rows=1,
        max_write_bytes=1,
        max_duration_sec=1,
        max_amplification_ratio=Decimal("1"),
        max_reject_rate=Decimal("0.15"),  # 3 倍
    )
    features = build_features(
        _intent(budget=requested), _scope(), Env.DEV, SensitiveLevel.INTERNAL, defaults=DEFAULTS
    )
    assert features["budget.over_default_ratio_max"] == "3"


# --------------------------------------------------------------------------- #
# 12. 特征向量完整性
# --------------------------------------------------------------------------- #
def test_特征向量键集与白名单一致() -> None:
    from engines.policy.rules import FEATURES

    features = build_features(_intent(), _scope(), Env.DEV, SensitiveLevel.INTERNAL, defaults=DEFAULTS)
    assert set(features) == set(FEATURES)
    assert all(isinstance(value, str) for value in features.values())


def test_now必须带时区() -> None:
    with pytest.raises(ValueError, match="必须带时区"):
        _decide(now=datetime(2026, 10, 16, 12, 0))


def test_policy_version原样回显() -> None:
    assert _decide(policy_version=7).policy_version == 7
    assert _decide().policy_version == 0


def test_决策模型不可变() -> None:
    decision = _decide()
    with pytest.raises(ValidationError):
        decision.level = RiskLevel.P3  # type: ignore[misc]


def test_证据里不含任何敏感值字段() -> None:
    """RuleHit.evidence 只允许特征名——特征表本身就是枚举/布尔/计数/预算。"""
    from engines.policy.rules import FEATURES

    decision = _decide(scope=_scope(columns=(_plaintext_phone(),)))
    for hit in decision.hits:
        assert set(hit.evidence) <= set(FEATURES)
