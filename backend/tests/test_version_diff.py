"""版本间 Diff 单元测试（be-12 §6.4）。

DTO 形状以前端 ``fetchVersionDiff`` 为准：``{ entries: [{path, kind, before, after}] }``。
本文件把「加了字段 / 删了字段 / 改了类型」三分支与段过滤钉死，并确认 Diff **不**碰指纹。
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from engines.diff import (
    DiffEntry,
    VersionDiff,
    hocon_unified_diff,
    unified_text_diff,
    version_diff,
)
from engines.fingerprint import ArtifactParts, artifact_sha256

_HOCON = "env {\n  x = 1\n}\n"


def _plan() -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "inputs": [{"id": "src_orders", "data_source_id": 3, "schema": "shop", "table": "orders"}],
        "outputs": [{"id": "sink_wide", "data_source_id": 9, "database": "dwd", "table": "sales_wide"}],
        "field_mappings": [
            {"source_field": "id", "source_type": "int", "target_field": "order_id", "target_type": "bigint"}
        ],
        "transforms": [],
        "filters": [],
        "quality_rules": [
            {"id": "q1", "field": "id", "rule_type": "not_null", "params": {}, "error_code": "E_NULL_PK"}
        ],
        "dag": {"nodes": [], "edges": []},
        # 这一段由 supervision 段表达；放在这里是为了证明 plan 段会跳过它
        "budget": {"max_read_rows": 1000, "warn_threshold_pct": 80},
    }


def _parts(
    *,
    plan: dict[str, Any] | None = None,
    hocon: str = _HOCON,
    quality: dict[str, Any] | None = None,
    supervision: dict[str, Any] | None = None,
    source: str = "a" * 64,
) -> ArtifactParts:
    return ArtifactParts(
        etl_plan=_plan() if plan is None else plan,
        hocon_content=hocon,
        quality_contract=quality if quality is not None else {"reject_rate_budget": "0.0500"},
        supervision=supervision
        if supervision is not None
        else {"max_read_rows": 1000, "warn_threshold_pct": 80},
        source_metadata_sha256=source,
    )


def _entry(entries: list[DiffEntry], path: str) -> DiffEntry:
    matches = [e for e in entries if e.path == path]
    assert len(matches) == 1, f"期望恰好一条 {path!r}，实际 {[e.path for e in entries]}"
    return matches[0]


# ---------------------------------------------------------------- 基本分支


def test_两个版本完全一致则没有条目() -> None:
    result = version_diff(_parts(), _parts())
    assert isinstance(result, VersionDiff)
    assert result.entries == []


def test_加了字段报_added() -> None:
    plan = deepcopy(_plan())
    plan["field_mappings"].append(
        {
            "source_field": "name",
            "source_type": "varchar",
            "target_field": "cust_name",
            "target_type": "varchar",
        }
    )
    result = version_diff(_parts(), _parts(plan=plan))
    entry = _entry(result.entries, "field_mappings[1]")
    assert entry.kind == "added"
    assert entry.before is None
    assert entry.after is not None and "cust_name" in entry.after


def test_删了字段报_removed() -> None:
    plan = deepcopy(_plan())
    plan["filters"] = [{"id": "f1", "expression": "id > 0"}]
    result = version_diff(_parts(plan=plan), _parts())
    entry = _entry(result.entries, "filters[f1]")
    assert entry.kind == "removed"
    assert entry.before is not None and "id > 0" in entry.before
    assert entry.after is None


def test_改了类型报_changed() -> None:
    plan = deepcopy(_plan())
    plan["field_mappings"][0]["target_type"] = "varchar"
    result = version_diff(_parts(), _parts(plan=plan))
    entry = _entry(result.entries, "field_mappings[0].target_type")
    assert entry.kind == "changed"
    assert entry.before == "bigint"
    assert entry.after == "varchar"


def test_标量类型变了也报_changed() -> None:
    result = version_diff(
        _parts(supervision={"max_read_rows": 1000}), _parts(supervision={"max_read_rows": "一千"})
    )
    entry = _entry(result.entries, "supervision.max_read_rows")
    assert entry.kind == "changed"
    assert entry.before == "1000"
    assert entry.after == "一千"


def test_新增的对象整体报一条() -> None:
    plan = deepcopy(_plan())
    plan["inputs"][0]["incremental"] = {"column": "id", "mode": "append"}
    result = version_diff(_parts(), _parts(plan=plan))
    entry = _entry(result.entries, "inputs[src_orders].incremental")
    assert entry.kind == "added"
    assert entry.after is not None and "append" in entry.after


# ---------------------------------------------------------------- path 约定


def test_plan_字段路径无前缀() -> None:
    plan = deepcopy(_plan())
    plan["inputs"][0]["table"] = "orders_v2"
    result = version_diff(_parts(), _parts(plan=plan))
    assert _entry(result.entries, "inputs[src_orders].table").kind == "changed"


def test_plan_段跳过_quality_rules_与_budget() -> None:
    """这两块由专门段表达，Plan 段跳过以免同一处改动出现两次。"""
    plan = deepcopy(_plan())
    plan["quality_rules"][0]["rule_type"] = "regex"
    plan["budget"]["max_read_rows"] = 2000
    plan["inputs"][0]["table"] = "orders_v2"  # 一个**应该**被报出来的改动
    result = version_diff(_parts(), _parts(plan=plan))
    assert [e.path for e in result.entries] == ["inputs[src_orders].table"]


def test_带_id_的数组按_id_匹配() -> None:
    quality = {"rules": [{"id": "E_NULL_PK", "rule_type": "not_null", "params": {"a": 1}}]}
    changed = {"rules": [{"id": "E_NULL_PK", "rule_type": "not_null", "params": {"a": 2}}]}
    result = version_diff(_parts(quality=quality), _parts(quality=changed))
    entry = _entry(result.entries, "quality_contract.rules[E_NULL_PK].params.a")
    assert entry.kind == "changed"
    assert entry.before == "1"
    assert entry.after == "2"


def test_带_id_的数组中间插入不会让后面全部变脏() -> None:
    old = {"rules": [{"id": "A", "v": 1}, {"id": "B", "v": 2}]}
    new = {"rules": [{"id": "Z", "v": 9}, {"id": "A", "v": 1}, {"id": "B", "v": 2}]}
    result = version_diff(_parts(quality=old), _parts(quality=new))
    assert [e.path for e in result.entries] == ["quality_contract.rules[Z]"]


# ---------------------------------------------------------------- HOCON


def test_hocon_改动产出_unified_diff_hunk() -> None:
    result = version_diff(_parts(hocon="a\nb\nc\n"), _parts(hocon="a\nB\nc\n"))
    entry = result.entries[0]
    assert entry.path.startswith("hocon:")
    assert entry.kind == "changed"
    assert entry.before == "b"
    assert entry.after == "B"


def test_hocon_只删不增报_removed() -> None:
    result = version_diff(_parts(hocon="a\nb\n"), _parts(hocon="a\n"))
    entry = result.entries[0]
    assert entry.kind == "removed"
    assert entry.before == "b"
    assert entry.after is None


def test_hocon_相同则无_hocon_条目() -> None:
    result = version_diff(_parts(), _parts())
    assert not any(e.path.startswith("hocon:") for e in result.entries)


def test_hocon_unified_diff_是标准_unified_格式() -> None:
    text = hocon_unified_diff("a\nb\n", "a\nB\n")
    assert text.startswith("--- a/hocon\n+++ b/hocon\n@@ ")
    assert "-b" in text
    assert "+B" in text


def test_unified_text_diff_相同返回空串() -> None:
    assert unified_text_diff("x", "x", fromfile="a", tofile="b") == ""


# ---------------------------------------------------------------- 其余两段


def test_源档案指纹变化成一条_changed() -> None:
    result = version_diff(_parts(), _parts(source="b" * 64))
    entry = _entry(result.entries, "source_metadata_profile_sha256")
    assert entry.kind == "changed"
    assert entry.before == "a" * 64
    assert entry.after == "b" * 64


def test_督导预算逐项比较() -> None:
    result = version_diff(_parts(), _parts(supervision={"max_read_rows": 5000, "warn_threshold_pct": 80}))
    entry = _entry(result.entries, "supervision.max_read_rows")
    assert entry.kind == "changed"
    assert (entry.before, entry.after) == ("1000", "5000")


def test_新增督导项报_added() -> None:
    result = version_diff(
        _parts(supervision={"max_read_rows": 1000}),
        _parts(supervision={"max_read_rows": 1000, "warn_threshold_pct": 90}),
    )
    assert _entry(result.entries, "supervision.warn_threshold_pct").kind == "added"


# ---------------------------------------------------------------- 段过滤与入参


def test_section_过滤只返回该段() -> None:
    plan = deepcopy(_plan())
    plan["inputs"][0]["table"] = "orders_v2"
    result = version_diff(_parts(), _parts(plan=plan, hocon="env {}\n"), section="hocon")
    assert result.entries
    assert all(e.path.startswith("hocon:") for e in result.entries)


def test_section_接受_be12_简写() -> None:
    plan = deepcopy(_plan())
    plan["inputs"][0]["table"] = "orders_v2"
    result = version_diff(_parts(), _parts(plan=plan), section="plan")
    assert [e.path for e in result.entries] == ["inputs[src_orders].table"]


def test_未知_section_抛错() -> None:
    with pytest.raises(ValueError, match="未知的 diff 段"):
        version_diff(_parts(), _parts(), section="不存在")


def test_能接受原始_dict_而非_ArtifactParts() -> None:
    raw = {
        "etl_plan": _plan(),
        "hocon_content": _HOCON,
        "quality_contract": {"reject_rate_budget": "0.0500"},
        "supervision": {"max_read_rows": 1000, "warn_threshold_pct": 80},
        "source_metadata_sha256": "a" * 64,
    }
    assert version_diff(raw, _parts()).entries == []


def test_diff_不回写也不影响指纹() -> None:
    """Diff 只做展示：产物是 entries，制品指纹与入参不受影响。"""
    old = _parts(hocon="a\nb\nc\n")
    new = _parts(hocon="a\nB\nc\n")
    before_old, before_new = artifact_sha256(old), artifact_sha256(new)
    result = version_diff(old, new)
    assert result.entries
    assert artifact_sha256(old) == before_old
    assert artifact_sha256(new) == before_new
