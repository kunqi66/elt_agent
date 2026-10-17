"""制品指纹单元测试（be-12 §9 的纯函数部分）。

两条最重要的断言：

1. **排除项逐项锁定**：改 ``frozen_by`` / ``note`` / 时间戳 / ``generation_context`` /
   派生校验结果 → 指纹**必须不变**（否则重算指纹永远失败、历史版本全部 verify 不过）。
2. **语义引用必须参与**：换 ``inputs[0].data_source_id`` → 指纹**必须变**。
   把「所有 id 都排除」照字面实现的人会在这里翻车——排除的是**数据库行 id**，
   不是 Plan 里对数据源的引用（be-12 §6.5 的指纹失效场景）。
"""

from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from core.ids import canonical_json
from engines.fingerprint import (
    EXCLUDED_KEYS,
    FINGERPRINT_FORMAT_VERSION,
    ArtifactParts,
    artifact_envelope,
    artifact_sha256,
    fingerprint_of,
    matches,
    normalize_part,
)

#: 一份固定样例的金标准指纹。规范化规则一旦无意改动，这里立刻红——
#: 免得「不小心把 ensure_ascii 打开」这种改动一路漂到生产才发现历史版本全都 verify 不过。
_GOLDEN_SHA256 = "300e84f331a31e4673c8099e90fe2602f8ea4abe1ede16a90ddd340e009cc927"


def _plan() -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "inputs": [
            {
                "id": "src_orders",
                "data_source_id": 3,
                "schema": "shop",
                "table": "orders",
                "kind": "table",
                "query": None,
                "path": None,
                "format": None,
                "incremental": None,
            }
        ],
        "outputs": [
            {
                "id": "sink_wide",
                "data_source_id": 9,
                "database": "dwd",
                "table": "sales_wide",
                "mode": "shadow_then_swap",
            }
        ],
        "field_mappings": [
            {
                "source_field": "id",
                "source_type": "int",
                "target_field": "order_id",
                "target_type": "bigint",
                "transform": "none",
                "masking": None,
                "quality_rule_ids": [],
                "change": "added",
            }
        ],
        "transforms": [
            {
                "id": "t1",
                "kind": "cast",
                "input_fields": ["id"],
                "output_field": "order_id",
                "params": {"to_type": "bigint"},
            }
        ],
        "filters": [{"id": "f1", "expression": "id > 0", "description": "", "stage": "source"}],
        "quality_rules": [
            {
                "id": "q1",
                "field": "id",
                "rule_type": "not_null",
                "params": {},
                "error_code": "E_NULL_PK",
                "on_violation": "reject",
                "enabled": True,
            }
        ],
        "dag": {
            "nodes": [
                {"id": "src_orders", "type": "source", "label": "", "ref": "", "meta": {}, "position": None}
            ],
            "edges": [],
        },
        "budget": {
            "max_read_rows": 1000,
            "max_write_bytes": 2000,
            "max_duration_sec": 60,
            "max_amplification_ratio": Decimal("1.5"),
            "max_reject_rate": Decimal("0.05"),
            "warn_threshold_pct": 80,
        },
    }


def _parts(plan: dict[str, Any] | None = None, **overrides: Any) -> ArtifactParts:
    fields: dict[str, Any] = {
        "etl_plan": _plan() if plan is None else plan,
        "hocon_content": "env {\n  x = 1\n}\n",
        "quality_contract": {"rules": [], "error_table_target": {}, "reject_rate_budget": "0.0500"},
        "supervision": {"max_read_rows": 1000, "warn_threshold_pct": 80},
        "source_metadata_sha256": "a" * 64,
    }
    fields.update(overrides)
    return ArtifactParts(**fields)


def _set(tokens: list[Any], value: Any, plan: dict[str, Any]) -> dict[str, Any]:
    """按路径写值，返回新 dict（用于「只改一个字，别的都不动」）。缺失的中间层自动补空 dict。"""
    mutated = deepcopy(plan)
    cursor: Any = mutated
    for token in tokens[:-1]:
        cursor = cursor[token] if isinstance(cursor, list) else cursor.setdefault(token, {})
    cursor[tokens[-1]] = value
    return mutated


# ---------------------------------------------------------------- 确定性


def test_同一份制品算两次指纹相同() -> None:
    assert artifact_sha256(_parts()) == artifact_sha256(_parts())


def test_键顺序打乱指纹不变() -> None:
    reordered = {key: _plan()[key] for key in reversed(list(_plan()))}
    assert artifact_sha256(_parts(reordered)) == artifact_sha256(_parts())


def test_固定样例命中金标准指纹() -> None:
    assert artifact_sha256(_parts()) == _GOLDEN_SHA256


def test_matches_只认逐字节相等的指纹() -> None:
    parts = _parts()
    assert matches(artifact_sha256(parts), parts) is True
    assert matches("0" * 64, parts) is False


# ---------------------------------------------------------------- 参与项：改一个字节 → 指纹必变


@pytest.mark.parametrize(
    ("tokens", "value"),
    [
        (["schema_version"], "2.0"),
        (["inputs", 0, "table"], "orders_v2"),
        (["inputs", 0, "id"], "src_orders_v2"),  # 语义节点 id：DAG 的边靠它，必须参与
        (["outputs", 0, "database"], "dwd2"),
        (["field_mappings", 0, "target_type"], "varchar"),
        (["transforms", 0, "kind"], "trim"),
        (["transforms", 0, "params", "to_type"], "int"),
        (["filters", 0, "expression"], "id > 1"),
        (["quality_rules", 0, "rule_type"], "regex"),
        (["dag", "nodes", 0, "type"], "transform"),
        (["budget", "max_read_rows"], 2000),
        (["budget", "max_reject_rate"], Decimal("0.06")),
    ],
)
def test_改_plan_里任何一个字段指纹必变(tokens: list[Any], value: Any) -> None:
    assert artifact_sha256(_parts(_set(tokens, value, _plan()))) != artifact_sha256(_parts())


def test_换_data_source_id_指纹必须变() -> None:
    """把源库从 3 换成 4：制品对不上源了，指纹不变就等于放行一份错方案。"""
    swapped = _set(["inputs", 0, "data_source_id"], 4, _plan())
    assert artifact_sha256(_parts(swapped)) != artifact_sha256(_parts())


def test_换目标_data_source_id_指纹必须变() -> None:
    swapped = _set(["outputs", 0, "data_source_id"], 10, _plan())
    assert artifact_sha256(_parts(swapped)) != artifact_sha256(_parts())


def test_改_hocon_内容一个字符指纹就变() -> None:
    base = _parts()
    tweaked = _parts(hocon_content="env {\n  x = 2\n}\n")
    assert artifact_sha256(tweaked) != artifact_sha256(base)


def test_改_hocon_尾随空白也算内容指纹就变() -> None:
    """HOCON 的字节就是它的身份，不做 trim（be-12 §6.3）。"""
    assert artifact_sha256(_parts(hocon_content="env {}\n")) != artifact_sha256(
        _parts(hocon_content="env {}")
    )


def test_改源档案指纹必变() -> None:
    assert artifact_sha256(_parts(source_metadata_sha256="b" * 64)) != artifact_sha256(_parts())


def test_改质量契约指纹必变() -> None:
    changed = _parts(
        quality_contract={"rules": [{"id": "q1"}], "error_table_target": {}, "reject_rate_budget": "0.0500"}
    )
    assert artifact_sha256(changed) != artifact_sha256(_parts())


def test_改督导预算指纹必变() -> None:
    assert artifact_sha256(
        _parts(supervision={"max_read_rows": 1, "warn_threshold_pct": 80})
    ) != artifact_sha256(_parts())


# ---------------------------------------------------------------- 排除项：改它 → 指纹**不变**


@pytest.mark.parametrize(
    ("tokens", "value"),
    [
        (["id"], 999),  # 数据库行 id
        (["valid"], False),
        (["validation_errors"], [{"path": "/x", "reason": "y"}]),
        (["created_at"], "2026-01-01T00:00:00Z"),
        (["updated_at"], "2026-01-02T00:00:00Z"),
        (["frozen_at"], "2026-01-03T00:00:00Z"),
        (["frozen_by"], 42),
        (["note"], "某个备注"),
        (["generation_context"], {"model_name": "deepseek-chat", "model_version": "v9"}),
        (["tenant_id"], 77),
        (["project_id"], 88),
        (["pipeline_id"], 99),
        (["version_no"], 5),
        (["status"], "version_frozen"),
        (["current_version_id"], 123),
    ],
)
def test_排除项改了指纹不变(tokens: list[Any], value: Any) -> None:
    assert artifact_sha256(_parts(_set(tokens, value, _plan()))) == artifact_sha256(_parts())


def test_派生的校验结果键被剥掉() -> None:
    """``sha256`` / ``parser_version`` / ``validation_result`` 在**任意深度**都剥（be-12 §6.2）。"""
    raw = {
        "plan": {"valid": True, "validation_errors": [{"path": "/x"}]},
        "hocon_artifact": {
            "sha256": "deadbeef",
            "parser_version": "pyhocon+elt-render-9",
            "validation_result": {"ok": True},
            "content": "keep",
        },
    }
    assert normalize_part(raw) == {
        "plan": {},
        "hocon_artifact": {"content": "keep"},
    }


def test_顶层行级_id_排除但_plan_内的语义_id_保留() -> None:
    envelope = artifact_envelope(_parts())
    plan_part = envelope["etl_plan"]
    assert "id" not in plan_part  # 行级 id 剥掉
    assert plan_part["inputs"][0]["id"] == "src_orders"  # 语义节点 id 保留
    assert plan_part["inputs"][0]["data_source_id"] == 3  # 语义引用保留


def test_excluded_keys_覆盖_be12_清单() -> None:
    expected = {
        "id",
        "tenant_id",
        "project_id",
        "version_id",
        "created_at",
        "updated_at",
        "frozen_at",
        "frozen_by",
        "note",
        "generation_context",
        "valid",
        "validation_errors",
        "sha256",
        "parser_version",
        "validation_result",
        "current_version_id",
        "status",
    }
    assert expected <= EXCLUDED_KEYS


# ---------------------------------------------------------------- 数值规范化


def test_decimal_0_10_与_0_1_规范化后一致() -> None:
    left = normalize_part({"a": Decimal("0.10")})
    right = normalize_part({"a": Decimal("0.1")})
    assert left == right == {"a": "0.1000"}


def test_decimal_量化为四位小数字符串() -> None:
    assert normalize_part({"rate": Decimal("0.05")}) == {"rate": "0.0500"}
    assert normalize_part({"rate": Decimal("1")}) == {"rate": "1.0000"}


def test_float_先过_str_中转不留二进制误差() -> None:
    # 直接 Decimal(0.1) 会得到 0.1000000000000000055511151231257827021181583404541015625
    assert normalize_part({"rate": 0.1}) == {"rate": "0.1000"}
    assert normalize_part({"rate": 0.05}) == {"rate": "0.0500"}


def test_整数与布尔不被当成小数() -> None:
    assert normalize_part({"n": 1, "flag": True}) == {"n": 1, "flag": True}


def test_null_键不省略() -> None:
    """键不省略：省略会让「后续加字段」悄悄改变历史数据的语义（be-12 §6.1）。"""
    assert normalize_part({"a": None}) == {"a": None}
    assert "a" in normalize_part({"a": None})


def test_中文原样保留不转义() -> None:
    """``ensure_ascii=False``：转成 ``\\uXXXX`` 会让同一份制品因编码选择而算出不同指纹。"""
    assert "销售宽表" in canonical_json(normalize_part({"desc": "销售宽表"}))


# ---------------------------------------------------------------- 形态与契约


def test_artifact_envelope_键集合完整() -> None:
    envelope = artifact_envelope(_parts())
    assert set(envelope) == {
        "v",
        "etl_plan",
        "hocon",
        "quality_contract",
        "supervision_contract",
        "source_metadata_profile_sha256",
    }
    assert envelope["v"] == FINGERPRINT_FORMAT_VERSION


def test_artifact_parts_不可变() -> None:
    parts = _parts()
    with pytest.raises(ValidationError):
        parts.hocon_content = "别的"  # type: ignore[misc]


def test_artifact_parts_拒绝未知字段() -> None:
    with pytest.raises(ValidationError):
        _parts(未知="x")


def test_fingerprint_of_便捷入口与规范路径一致() -> None:
    got = fingerprint_of(
        _plan(),
        "env {\n  x = 1\n}\n",
        {"rules": [], "error_table_target": {}, "reject_rate_budget": "0.0500"},
        {"max_read_rows": 1000, "warn_threshold_pct": 80},
        "a" * 64,
    )
    assert got == artifact_sha256(_parts())
