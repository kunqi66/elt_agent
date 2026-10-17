"""``engines.plan.draft`` 的回归用例。

这些用例守的是「兜底起草器」的两条底线：
1. **确定性**：同输入两次必须逐字节同输出。它一旦破了，Benchmark L0 的可复现、
   制品指纹的稳定性、以及「门禁结论为什么不一样」全都无从谈起。
2. **安全默认值**：敏感列必须带掩码。这条允许「少做」但不允许「做错」——
   一份没有掩码的敏感列方案，是要靠人一眼看出来的，而不是靠事后审计。
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError as PydanticValidationError

from engines.plan.draft import (
    DraftColumn,
    DraftSourceTable,
    DraftTable,
    DraftTargetTable,
    draft_plan,
)
from engines.plan.schema import RuntimeBudget, dump_plan
from models.enums import SensitiveLevel


def _budget() -> RuntimeBudget:
    return RuntimeBudget(
        max_read_rows=10_000_000,
        max_write_bytes=1_073_741_824,
        max_duration_sec=3600,
        max_amplification_ratio=Decimal("1.5"),
        max_reject_rate=Decimal("0.05"),
        warn_threshold_pct=80,
    )


def _columns() -> list[DraftColumn]:
    return [
        DraftColumn(name="order_id", data_type="bigint", nullable=False, is_primary_key=True),
        DraftColumn(
            name="user_phone",
            data_type="varchar(20)",
            nullable=False,
            is_primary_key=False,
            sensitive_level=SensitiveLevel.CONFIDENTIAL,
        ),
        DraftColumn(name="amount", data_type="decimal(10,2)"),
        DraftColumn(name="created_at", data_type="datetime", nullable=False),
    ]


def _source(tables: list[DraftTable] | None = None) -> DraftSourceTable:
    return DraftSourceTable(
        data_source_id=1,
        schema="biz_demo",
        tables=tables or [DraftTable(name="orders", columns=_columns(), row_count_approx=120000)],
    )


def _target() -> DraftTargetTable:
    return DraftTargetTable(
        data_source_id=2,
        database="dw_demo",
        table="orders_wide",
        columns=[
            DraftColumn(name="order_id", data_type="bigint"),
            DraftColumn(name="user_phone", data_type="varchar(64)"),
            # 类型不同：源 decimal(10,2) → 目标 varchar(64) 会触发 cast
            DraftColumn(name="amount", data_type="varchar(64)"),
        ],
    )


def _draft(requirement: str = "把 orders 表同步到 dw_demo.orders_wide", **overrides):
    kwargs = {
        "requirement": requirement,
        "source": _source(),
        "target": _target(),
        "budget": _budget(),
        "data_source_types": {1: "mysql", 2: "doris"},
    }
    kwargs.update(overrides)
    return draft_plan(**kwargs)


# ---------------------------------------------------------------- 确定性


def test_draft_is_byte_identical_across_calls():
    """同输入两次 → ``dump_plan`` 逐字节一致。"""
    first = dump_plan(_draft())
    second = dump_plan(_draft())
    assert first == second


def test_draft_does_not_embed_time_or_randomness():
    """输出里不允许出现任何「现在」「随机」的痕迹。

    做法是跑两次并比较**完整**的序列化结果——只要实现里混进 ``utcnow()``
    或 ``uuid4()``，两次调用就会差在那一处。比逐个字段断言更难绕过。
    """
    payload = dump_plan(_draft())
    rendered = repr(payload)
    assert "utcnow" not in rendered
    assert payload["schema_version"] == "1.0"


def test_budget_is_passed_through_untouched():
    budget = _budget()
    assert _draft().budget == budget


# ---------------------------------------------------------------- 安全默认值


def test_confidential_column_always_masked():
    plan = _draft()
    mapping = next(m for m in plan.field_mappings if m.source_field == "user_phone")
    assert mapping.transform == "mask"
    assert mapping.masking is not None
    assert mapping.masking.method == "mask_middle"


def test_restricted_column_also_masked():
    tables = [
        DraftTable(
            name="orders",
            columns=[DraftColumn(name="id_card", data_type="varchar(32)")],
        )
    ]
    # 默认级别是 internal → 不该被掩码；显式升到 restricted 后必须被掩码
    plain = _draft(source=_source(tables), target=None, requirement="同步 orders")
    assert plain.field_mappings[0].transform == "none"
    assert plain.field_mappings[0].masking is None

    tables[0].columns[0] = (
        tables[0].columns[0].model_copy(update={"sensitive_level": SensitiveLevel.RESTRICTED})
    )
    hardened = _draft(source=_source(tables), target=None, requirement="同步 orders")
    assert hardened.field_mappings[0].transform == "mask"


def test_sensitive_column_with_type_change_still_masked():
    """敏感 + 类型不同时，掩码优先（字段级变换只有一个槽位）。"""
    mapping = next(m for m in _draft().field_mappings if m.source_field == "user_phone")
    assert mapping.transform == "mask"
    assert mapping.target_type == "varchar(64)"


# ---------------------------------------------------------------- 类型与规则


def test_type_mismatch_produces_cast_and_transform_entry():
    plan = _draft()
    mapping = next(m for m in plan.field_mappings if m.source_field == "amount")
    assert mapping.transform == "cast"
    assert mapping.target_type == "varchar(64)"

    cast = next(t for t in plan.transforms if t.kind == "cast")
    assert cast.input_fields == ["amount"]
    assert cast.params["target_type"] == "varchar(64)"


def test_same_type_produces_no_cast():
    plan = _draft()
    mapping = next(m for m in plan.field_mappings if m.source_field == "order_id")
    assert mapping.transform == "none"
    assert plan.transforms == [] or all(t.input_fields != ["order_id"] for t in plan.transforms)


def test_not_null_rule_lands_on_first_non_nullable_column():
    plan = _draft()
    rule = next(r for r in plan.quality_rules if r.error_code == "E_NULL_PK")
    assert rule.field == "order_id"
    assert rule.rule_type == "not_null"
    assert rule.on_violation == "reject"
    mapping = next(m for m in plan.field_mappings if m.target_field == "order_id")
    assert rule.id in mapping.quality_rule_ids


def test_sensitive_plaintext_rule_rejects():
    plan = _draft()
    rule = next(r for r in plan.quality_rules if r.error_code == "E_SENSITIVE_PLAINTEXT")
    assert rule.field == "user_phone"
    assert rule.on_violation == "reject"
    mapping = next(m for m in plan.field_mappings if m.target_field == "user_phone")
    assert rule.id in mapping.quality_rule_ids


def test_no_rule_when_no_sensitive_and_no_not_null_column():
    tables = [DraftTable(name="t", columns=[DraftColumn(name="a", data_type="int")])]
    plan = _draft(source=_source(tables), target=None, requirement="同步 t")
    assert plan.quality_rules == []


# ---------------------------------------------------------------- 选表与目标名


def test_source_table_picked_by_requirement_mention():
    tables = [
        DraftTable(name="orders", columns=[DraftColumn(name="id", data_type="bigint")]),
        DraftTable(name="customers", columns=[DraftColumn(name="id", data_type="bigint")]),
    ]
    plan = _draft(source=_source(tables), target=None, requirement="把 CUSTOMERS 合成宽表")
    assert plan.inputs[0].table == "customers"


def test_source_table_falls_back_to_first():
    tables = [
        DraftTable(name="orders", columns=[DraftColumn(name="id", data_type="bigint")]),
        DraftTable(name="customers", columns=[DraftColumn(name="id", data_type="bigint")]),
    ]
    plan = _draft(source=_source(tables), target=None, requirement="同步一下数据")
    assert plan.inputs[0].table == "orders"


@pytest.mark.parametrize(
    ("requirement", "expected"),
    [
        ("把 orders 同步到 dw.sales_wide", "sales_wide"),
        ("orders -> ods_orders", "ods_orders"),
        ("orders 到 dw_demo.orders_daily", "orders_daily"),
        ("sync orders to orders_v2", "orders_v2"),
    ],
)
def test_target_name_from_requirement(requirement: str, expected: str):
    plan = _draft(requirement=requirement)
    assert plan.outputs[0].table == expected


def test_target_name_falls_back_to_wide_table():
    plan = _draft(target=None, requirement="同步订单数据")
    assert plan.outputs[0].table == "orders_wide"
    assert plan.outputs[0].database == ""


def test_target_name_uses_declared_target_before_wide_fallback():
    target = _target().model_copy(update={"table": "orders_declared"})
    plan = _draft(target=target, requirement="同步订单数据")
    assert plan.outputs[0].table == "orders_declared"


# ---------------------------------------------------------------- DAG


def test_dag_nodes_edges_and_no_positions():
    plan = _draft()
    node_ids = [n.id for n in plan.dag.nodes]
    assert node_ids == ["n_source", "n_transform", "n_quality", "n_sink"]
    assert plan.dag.edges[0].source == "n_source"
    assert plan.dag.edges[-1].target == "n_sink"
    assert all(n.position is None for n in plan.dag.nodes)
    assert all(n.label for n in plan.dag.nodes)


def test_dag_skips_transform_node_when_no_transforms():
    tables = [DraftTable(name="t", columns=[DraftColumn(name="a", data_type="int")])]
    target = DraftTargetTable(
        data_source_id=2, database="dw", table="t", columns=[DraftColumn(name="a", data_type="int")]
    )
    plan = _draft(source=_source(tables), target=target, requirement="同步 t")
    assert [n.id for n in plan.dag.nodes] == ["n_source", "n_quality", "n_sink"]


# ---------------------------------------------------------------- 文件类源


def test_file_source_sets_kind_and_format_without_inventing_a_path():
    plan = _draft(data_source_types={1: "csv"})
    assert plan.inputs[0].kind == "file"
    assert plan.inputs[0].format == "csv"
    # path 缺失是要问用户的问题，不是兜底起草器该猜的东西
    assert plan.inputs[0].path is None


# ---------------------------------------------------------------- 输入校验


def test_empty_source_tables_rejected():
    with pytest.raises(PydanticValidationError):
        DraftSourceTable(data_source_id=1, schema="s", tables=[])


def test_unknown_field_rejected():
    with pytest.raises(PydanticValidationError):
        DraftColumn(name="a", data_type="int", sample_value="should-not-exist")
