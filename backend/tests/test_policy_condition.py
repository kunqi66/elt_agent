"""扁平条件 ↔ 规范 DSL 适配器的测试。

**为什么这个文件值得单独存在**：这是前端契约与引擎求值格式之间**唯一**的转换点。
它错一个分支，表现就是「界面上勾了某个条件，判定结果与勾选不符」——
而且错的方向可能既包括漏拦（安全问题）也包括误拦（可用性问题），
两者都不会有任何报错。

因此每个键都成对测：写 `true` 命中、写 `false` **不**命中，
且「不写」与「写 false」必须给出**不同**的结果（前者不约束、后者是反向条件）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from engines.policy import (
    BudgetDefaults,
    ResourceRef,
    ResourceScope,
    RuleSpec,
    SensitiveColumn,
    ToolIntent,
    compile_flat_condition,
    decide,
    is_flat,
    normalize_condition,
)
from models.enums import DataSourceType, Env, RiskLevel, SensitiveLevel

NOW = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)
DEFAULTS = BudgetDefaults(
    max_read_rows=1_000_000,
    max_write_bytes=1_000_000,
    max_duration_sec=3600,
    max_amplification_ratio=Decimal("1.5"),
    max_reject_rate=Decimal("0.05"),
)


def _scope(**over) -> ResourceScope:
    base: dict = {
        "tenant_id": 1,
        "project_id": 1,
        "reads": (
            ResourceRef(
                data_source_type=DataSourceType.MYSQL,
                database="shop",
                table="orders",
                env=Env.DEV,
            ),
        ),
    }
    base.update(over)
    return ResourceScope(**base)


def _intent(**over) -> ToolIntent:
    base: dict = {"kind": "etl_write", "is_write": True, "has_rollback_plan": True}
    base.update(over)
    return ToolIntent(**base)


def _flagged(condition: dict, *, intent=None, scope=None, env=Env.DEV):
    """用一条自造规则跑一次判定，返回是否命中。"""
    rule = RuleSpec(
        code="p1.probe",
        level=RiskLevel.P1,
        title="probe",
        condition=condition,
    )
    decision = decide(
        intent or _intent(),
        scope or _scope(),
        env,
        SensitiveLevel.INTERNAL,
        now=NOW,
        defaults=DEFAULTS,
        rules=(rule,),
    )
    return any(hit.code == "p1.probe" for hit in decision.hits)


# --------------------------------------------------------------------------- #
# 形式判别
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("condition", "expected"),
    [
        ({}, True),
        ({"env": ["prod"]}, True),
        ({"writes_formal_table": True}, True),
        ({"all": []}, False),
        ({"any": [{"feature": "env", "op": "eq", "value": "prod"}]}, False),
        ({"feature": "env", "op": "eq", "value": "prod"}, False),
        ({"not": {"feature": "env", "op": "eq", "value": "prod"}}, False),
    ],
)
def test_形式判别(condition, expected) -> None:
    assert is_flat(condition) is expected


def test_规范DSL经过normalize不被改动() -> None:
    dsl = {"all": [{"feature": "env", "op": "eq", "value": "prod"}]}
    assert normalize_condition(dsl) == dsl


def test_未知扁平键报错而不是忽略() -> None:
    with pytest.raises(ValueError, match="未知的扁平条件键"):
        compile_flat_condition({"delete_everything": True})


def test_环境列表为空表示永不命中() -> None:
    """显式空列表是作者的选择，不能被当成「漏写环境」而放行。"""
    compiled = compile_flat_condition({"env": []})
    assert compiled == {"feature": "env", "op": "in", "value": []}
    assert _flagged({"env": []}) is False


def test_多个键之间是与关系() -> None:
    condition = {"env": ["prod"], "writes_formal_table": True}
    assert _flagged(condition, scope=_scope(writes_formal_table=True), env=Env.PROD) is True
    assert _flagged(condition, scope=_scope(writes_formal_table=True), env=Env.DEV) is False
    assert _flagged(condition, scope=_scope(), env=Env.PROD) is False


# --------------------------------------------------------------------------- #
# 逐键：true 命中 / false 是反向条件 / 不写不约束
# --------------------------------------------------------------------------- #
def test_env键() -> None:
    assert _flagged({"env": ["prod"]}, env=Env.PROD) is True
    assert _flagged({"env": ["prod"]}, env=Env.DEV) is False
    assert _flagged({}, env=Env.PROD) is True  # 不写 = 不约束


def test_writes_formal_table键() -> None:
    assert _flagged({"writes_formal_table": True}, scope=_scope(writes_formal_table=True)) is True
    assert _flagged({"writes_formal_table": True}, scope=_scope(writes_formal_table=False)) is False
    assert _flagged({"writes_formal_table": False}, scope=_scope(writes_formal_table=False)) is True
    assert _flagged({"writes_formal_table": False}, scope=_scope(writes_formal_table=True)) is False


def test_sensitive_plaintext_egress键() -> None:
    column = SensitiveColumn(
        name="phone",
        level=SensitiveLevel.CONFIDENTIAL,
        kind="phone",
        contains_plaintext=True,
        masked=False,
        egress=True,
    )
    assert _flagged({"sensitive_plaintext_egress": True}, scope=_scope(columns=(column,))) is True
    assert _flagged({"sensitive_plaintext_egress": True}, scope=_scope()) is False
    assert _flagged({"sensitive_plaintext_egress": False}, scope=_scope()) is True


def test_bypass_approval_intent键覆盖三种绕行意图() -> None:
    condition = {"bypass_approval_intent": True}
    assert _flagged(condition, intent=_intent(bypass_approval=True)) is True
    assert _flagged(condition, intent=_intent(disable_audit=True)) is True
    assert _flagged(condition, intent=_intent(export_plaintext_sensitive=True)) is True
    assert _flagged(condition, intent=_intent()) is False
    # 反向：三者全为假才命中
    assert _flagged({"bypass_approval_intent": False}, intent=_intent(disable_audit=True)) is False
    assert _flagged({"bypass_approval_intent": False}, intent=_intent()) is True


def test_cross_tenant_access键同时看意图与资源() -> None:
    assert _flagged({"cross_tenant_access": True}, intent=_intent(cross_tenant=True)) is True
    other = ResourceRef(
        data_source_type=DataSourceType.MYSQL, database="other", table="t", env=Env.DEV
    )
    assert _flagged({"cross_tenant_access": True}, scope=_scope(cross_tenant_refs=(other,))) is True
    assert _flagged({"cross_tenant_access": True}) is False


def test_resource_out_of_scope键() -> None:
    other = ResourceRef(
        data_source_type=DataSourceType.MYSQL, database="other", table="t", env=Env.DEV
    )
    assert _flagged({"resource_out_of_scope": True}, scope=_scope(out_of_scope=(other,))) is True
    assert _flagged({"resource_out_of_scope": True}) is False
    assert _flagged({"resource_out_of_scope": False}) is True


def test_budget_over_default_factor键() -> None:
    from engines.policy.models import BudgetRequest

    big = BudgetRequest(
        max_read_rows=10_000_000,
        max_write_bytes=1,
        max_duration_sec=1,
        max_amplification_ratio=Decimal("1"),
        max_reject_rate=Decimal("0.01"),
    )
    assert _flagged({"budget_over_default_factor": 3}, intent=_intent(budget=big)) is True
    assert _flagged({"budget_over_default_factor": 20}, intent=_intent(budget=big)) is False
    assert _flagged({"budget_over_default_factor": 3}) is False  # 未申报 → 比值 1.0


def test_missing_rollback_plan键及其补集() -> None:
    assert _flagged({"missing_rollback_plan": True}, intent=_intent(has_rollback_plan=False)) is True
    # 🔴 反向条件必须对只读意图也成立，否则它是一个恒假的约束
    readonly = _intent(kind="readonly_probe", is_write=False, has_rollback_plan=False)
    assert _flagged({"missing_rollback_plan": False}, intent=readonly) is True
    assert _flagged({"missing_rollback_plan": True}, intent=readonly) is False


def test_unregistered_secret_ref键() -> None:
    assert _flagged({"unregistered_secret_ref": True}, scope=_scope(unregistered_secret_refs=(1,))) is True
    assert _flagged({"unregistered_secret_ref": True}) is False


def test_read_only键() -> None:
    readonly = _intent(kind="readonly_probe", is_write=False)
    assert _flagged({"read_only": True}, intent=readonly) is True
    assert _flagged({"read_only": True}) is False
    assert _flagged({"read_only": False}, intent=readonly) is False


def test_非布尔值报错() -> None:
    with pytest.raises(ValueError, match="需要布尔值"):
        compile_flat_condition({"read_only": "yes"})


def test_非数值的预算因子报错() -> None:
    with pytest.raises(ValueError, match="需要数值"):
        compile_flat_condition({"budget_over_default_factor": True})


# --------------------------------------------------------------------------- #
# RuleSpec 自动规整
# --------------------------------------------------------------------------- #
def test_RuleSpec自动把扁平条件编译成DSL() -> None:
    rule = RuleSpec(code="p1.x", level=RiskLevel.P1, title="t", condition={"env": ["prod"]})
    assert rule.condition == {"feature": "env", "op": "in", "value": ["prod"]}


def test_RuleSpec不接受未知键() -> None:
    with pytest.raises(ValidationError):
        RuleSpec(code="p1.x", level=RiskLevel.P1, title="t", condition={"nope": 1})
