"""Workflow State 契约的用例（be-09 §5.2 / §5.3 / §5.5 / §6.1）。

**全是纯函数用例**：state / 转移表 / 中断载荷 / DSN 归一 / 阶段映射都不该碰库，
也不该需要 LLM 凭据——本机（Windows，无 Docker、无 PG、无 libpq）必须能全绿。
凡是用例里出现会话或网络的，都是设计跑偏了。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from core.errors import ConflictError, ErrorCode
from models.enums import PipelineStatus
from workflow import state as S
from workflow.interrupts import (
    CLARIFICATION_KINDS,
    INTERRUPT_REASON_MISSING_PARAMS,
    interrupt_payload,
    merge_resume_params,
    parse_interrupt,
    parse_resume_input,
    question_for,
)
from workflow.nodes.param_check import (
    REQUIRED_PARAM_ORDER,
    EtlIntent,
    find_missing_params,
)
from workflow.transitions import (
    EVENTS,
    OUT_OF_SCOPE_STATES,
    OWNED_STATES,
    TRANSITIONS,
    assert_transition,
    can_transition,
    status_after_gate,
)

# 前端 `WorkflowSnapshot` 的键集合，逐字抄自 frontend/src/types/pipeline.ts。
# 断言写在用例里而不是引用 S.SNAPSHOT_KEYS：引用就等于「自己证明自己」，
# 契约漂移时这份用例必须红。
_CONTRACT_SNAPSHOT_KEYS = {
    "thread_id",
    "phase",
    "phase_message",
    "pending_questions",
    "autofix_round",
    "last_error",
}

# 逐字抄自 frontend/src/types/pipeline.ts 的 StudioPhase。
_CONTRACT_PHASES = (
    "idle",
    "parsing",
    "awaiting_clarification",
    "generating",
    "gate_running",
    "gate_failed",
    "ready_to_freeze",
    "frozen",
    "submitted",
    "error",
)


def _full_state() -> dict:
    """每个已声明字段都给一个代表值（形状刻意用「最坏情况」）。

    这份 state 是**契约样本**：``STATE_KEYS`` 新增字段而样本没跟上时，
    :func:`test_state_sample_covers_every_declared_key` 会红——
    否则新字段可以悄悄做成 datetime 或 ORM 实例而没人发现。
    """
    return {
        "pipeline_id": 7,
        "tenant_id": 1,
        "project_id": 2,
        "user_id": 3,
        "request_id": "req-1",
        # 入图注入的四样（见 state.py）：少了它们，LangGraph 会把键**静默丢掉**
        "thread_id": "7",
        "started_at": "2026-10-16T10:00:00+00:00",
        "requirement": "把 biz_demo.orders 同步进 doris 宽表",
        "source_data_source_id": 11,
        "target_data_source_id": 12,
        "env": "dev",
        "data_source_types": {"11": "mysql", "12": "doris"},
        "profile_summaries": {
            "11": {"data_source_id": 11, "status": "ready", "tables": [{"name": "orders"}]}
        },
        # 这三样是 be-08 ``plan_blocks`` 的原料，由 orchestrator 注入（见 state.py）。
        # 形状取**真实 dump**：``source_profile`` 用 alias ``schema``，
        # ``budget`` 的比率是定标字符串——形状写错的话提示词会拼不出来，
        # 而拼接失败发生在模型那一侧，报错离真因很远。
        "source_profile": {
            "data_source_id": 11,
            "schema": "biz_demo",
            "tables": [
                {
                    "name": "orders",
                    "row_count_approx": 120000,
                    "columns": [
                        {
                            "name": "order_id",
                            "data_type": "bigint",
                            "nullable": False,
                            "is_primary_key": True,
                            "sensitive_level": "internal",
                        }
                    ],
                }
            ],
        },
        "target_profile": None,
        "budget": {
            "max_read_rows": 100000000,
            "max_write_bytes": 53687091200,
            "max_duration_sec": 7200,
            "max_amplification_ratio": "1.5",
            "max_reject_rate": "0.05",
            "warn_threshold_pct": 80,
        },
        "messages": [{"role": "user", "content": "把 orders 同步进 doris"}],
        "intent": {"requirement": "把 orders 同步进 doris", "primary_key": ["order_id"]},
        "collected_params": {"target_table": "orders_wide", "quality_threshold": "0.0500"},
        "target_table_hint": None,
        "missing_params": ["dedup_rule"],
        "pending_questions": [
            {"key": "dedup_rule", "question": "重复数据如何取舍？", "kind": "text", "required": True}
        ],
        "profile_ids": {"11": 101, "12": 102},
        "profile_sha256": {"11": "a" * 64, "12": "b" * 64},
        "draft_plan": {"name": "orders_wide", "inputs": [{"id": "s", "data_source_id": 11}]},
        "plan_errors": [{"code": "mapping_unmapped_target", "message": "目标列缺失"}],
        "draft_hocon": 'env { job.mode = "BATCH" }',
        "hocon_errors": [{"message": "缺少 source 块", "line": 0, "column": 0}],
        "hocon_meta": {"sha256": "c" * 64, "parser_version": "1.0", "job_name": "orders_wide"},
        "gate_result": {"conclusion": "block", "findings": [{"code": "schema_missing_column"}]},
        "autofix_rounds": 1,
        "autofix_history": [
            {
                "round": 1,
                "trigger_finding_ids": ["f1"],
                "hocon_diff": "--- a\n+++ b\n",
                "plan_diff": None,
                "resolved_finding_ids": [],
                "created_at": "2026-10-16T08:00:00+00:00",
            }
        ],
        "status": PipelineStatus.GATE_FAILED.value,
        "terminal_error": None,
        "generation_context": {"model_name": "fake", "model_version": "v1", "autofix_rounds": 1},
        "frozen_version_id": None,
        "node_fingerprints": {"intent_parse": "d" * 64},
        "aborted": False,
    }


# ---------------------------------------------------------------- 序列化守卫


def test_state_sample_covers_every_declared_key():
    """契约样本必须覆盖全部已声明字段（新增字段即红）。"""
    assert set(_full_state()) == set(S.STATE_KEYS)


def test_full_state_is_json_serializable():
    """be-09 §5.2 的硬性约束：state 里每一个值都要能 json.dumps。"""
    payload = _full_state()
    S.assert_json_serializable(payload)
    assert json.loads(json.dumps(payload, ensure_ascii=False))["pipeline_id"] == 7


@pytest.mark.parametrize(
    ("bad", "expected_path"),
    [
        ({"a": {"b": datetime(2026, 10, 16, tzinfo=UTC)}}, "$.a.b（datetime.datetime）"),
        ({"ratio": Decimal("0.05")}, "$.ratio（decimal.Decimal）"),
        ({"blob": b"masked"}, "$.blob（builtins.bytes）"),
        ({"nested": [1, {"deep": object()}]}, "$.nested[1].deep（builtins.object）"),
        ({1: "int key"}, "$.<int 键>（dict 键必须是字符串）"),
        ({"nan": float("nan")}, "$.nan（float 非有限值）"),
        ({"inf": float("inf")}, "$.inf（float 非有限值）"),
    ],
)
def test_find_non_serializable_reports_the_offending_path(bad, expected_path):
    """报错要指出**在哪**——state 有二十多个字段，只报类型等于让人靠猜定位。"""
    assert S.find_non_serializable(bad) == expected_path
    # 路径要能唯一指回那个值，且**不带值本身**（值可能是样本原文或密钥）
    assert "0.05" not in expected_path
    assert "masked" not in expected_path.replace("blob", "")


def test_find_non_serializable_accepts_every_allowed_type():
    ok = {"s": "x", "i": 1, "f": 1.5, "b": True, "n": None, "l": [1, "a", None], "t": (1, 2)}
    assert S.find_non_serializable(ok) is None


def test_assert_json_serializable_raises_internal_error():
    """不可序列化是**实现缺陷**（500），不是用户输入问题（422）。"""
    with pytest.raises(S.StateNotSerializableError) as excinfo:
        S.assert_json_serializable({"created_at": datetime(2026, 10, 16, tzinfo=UTC)})
    assert excinfo.value.http_status == 500
    assert excinfo.value.code is ErrorCode.INTERNAL_ERROR
    # 路径要出现在消息里；**值本身不许出现**（它可能是样本原文或密钥）
    assert "$.created_at" in excinfo.value.message
    assert "2026-10-16" not in excinfo.value.message


@pytest.mark.parametrize(
    "key",
    [
        "run",
        "runs",
        "execution_run",
        "execution_run_id",
        "quality_report",
        "execution_metric",
        "execution_started_at",
        "run_metrics",
    ],
)
def test_execution_keys_are_rejected(key):
    """三层隔离的第一条可检测信号（be-09 §6.1）。"""
    with pytest.raises(S.StateNotSerializableError):
        S.assert_no_execution_keys({"pipeline_id": 1, key: 2})


def test_execution_keys_are_rejected_through_snapshot():
    """快照投影之前就要拦下：快照是前端唯一读到的形状，混进来就再也分不开。"""
    payload = {**_full_state(), "execution_run_id": 9}
    with pytest.raises(S.StateNotSerializableError):
        S.snapshot(payload)


# ---------------------------------------------------------------- 快照契约


def test_snapshot_keys_exactly_equal_frontend_contract():
    snap = S.snapshot(_full_state())
    assert set(snap) == _CONTRACT_SNAPSHOT_KEYS
    assert S.SNAPSHOT_KEYS == frozenset(_CONTRACT_SNAPSHOT_KEYS)


def test_snapshot_never_leaks_state_beyond_whitelist():
    """快照只投影白名单键：草稿制品、参数、档案指纹都不该出现在前端的快照接口里。"""
    snap = S.snapshot(_full_state())
    for leaked in ("draft_plan", "collected_params", "profile_sha256", "intent", "gate_result"):
        assert leaked not in snap


def test_snapshot_shapes_are_frontend_compatible():
    snap = S.snapshot(_full_state())
    assert snap["thread_id"] == "7"
    assert isinstance(snap["phase"], str) and snap["phase"] in _CONTRACT_PHASES
    assert isinstance(snap["pending_questions"], list)
    assert snap["autofix_round"] == 1
    assert snap["last_error"] is None
    assert json.loads(json.dumps(snap))["phase"] == snap["phase"]


def test_snapshot_tolerates_half_written_state_from_an_older_version():
    """检查点里可能是**上一个版本的代码**写下的 state（滚动升级期间正在飞的那次恢复）。"""
    snap = S.snapshot({"pipeline_id": 7})
    assert snap["phase"] == "idle"
    assert snap["autofix_round"] == 0


def test_thread_id_is_the_string_of_pipeline_id():
    """be-09 §5.3：跨进程的恢复钥匙，写成 'pipeline-7' 会让恢复静默从头再跑。"""
    assert S.thread_id_for(7) == "7"
    assert S.thread_id_for("7") == "7"
    assert S.thread_id_for(7) == str(7)


# ---------------------------------------------------------------- 阶段映射


@pytest.mark.parametrize(
    ("state_overrides", "expected"),
    [
        ({}, "idle"),
        ({"aborted": True, "draft_plan": {"name": "x"}}, "idle"),
        ({"requirement": "同步 orders"}, "parsing"),
        ({"messages": [{"role": "user", "content": "同步 orders"}]}, "parsing"),
        ({"missing_params": ["primary_key"]}, "awaiting_clarification"),
        ({"pending_questions": [{"key": "primary_key"}]}, "awaiting_clarification"),
        ({"draft_plan": {"name": "x"}}, "generating"),
        ({"draft_hocon": "env {}", "draft_plan": {"name": "x"}}, "gate_running"),
        ({"gate_result": {"conclusion": "block"}}, "gate_failed"),
        ({"gate_result": {"conclusion": "pass"}}, "ready_to_freeze"),
        ({"gate_result": {"conclusion": "warn"}}, "ready_to_freeze"),
        ({"status": PipelineStatus.VERSION_FROZEN.value}, "frozen"),
        ({"frozen_version_id": 12}, "frozen"),
        ({"status": PipelineStatus.PENDING_APPROVAL.value}, "submitted"),
        ({"status": PipelineStatus.RUNNING.value}, "submitted"),
        ({"status": PipelineStatus.ROLLED_BACK.value}, "submitted"),
        ({"terminal_error": ErrorCode.AUTOFIX_EXHAUSTED.value}, "error"),
        # 终局优先于过程：失败之后 draft_plan 可能还在 state 里，
        # 若先判「有 plan 就是 generating」，用户会看到失败的任务显示「正在生成」
        ({"terminal_error": "X", "draft_plan": {"name": "x"}}, "error"),
        ({"aborted": True, "terminal_error": "X"}, "idle"),
    ],
)
def test_phase_for(state_overrides, expected):
    payload = {"pipeline_id": 7, **state_overrides}
    assert S.phase_for(payload) == expected


def test_phase_mapping_covers_every_studio_phase_value():
    """每一个前端取值都必须能被映射到（且都有文案）。

    ``phase_for`` 的输出空间要与 :data:`STUDIO_PHASES` **相等**：
    - 少一个 → 前端某个阶段永远到不了；
    - 多一个 → 前端拿到一个它不认识的 phase，界面会走到兜底分支。
    """
    assert set(S.STUDIO_PHASES) == set(_CONTRACT_PHASES)
    assert set(S.PHASE_MESSAGES) == set(S.STUDIO_PHASES)
    produced = {S.phase_for({"pipeline_id": 7, **override}) for override, _ in _PHASE_CASES}
    assert produced == set(S.STUDIO_PHASES)


# 与上面 parametrize 同一份样本（重复导出一次是为了让「输出空间」这条断言能直接读它）
_PHASE_CASES = [
    ({}, "idle"),
    ({"aborted": True}, "idle"),
    ({"requirement": "x"}, "parsing"),
    ({"missing_params": ["primary_key"]}, "awaiting_clarification"),
    ({"draft_plan": {"name": "x"}}, "generating"),
    ({"draft_hocon": "env {}"}, "gate_running"),
    ({"gate_result": {"conclusion": "block"}}, "gate_failed"),
    ({"gate_result": {"conclusion": "pass"}}, "ready_to_freeze"),
    ({"status": PipelineStatus.VERSION_FROZEN.value}, "frozen"),
    ({"status": PipelineStatus.PENDING_APPROVAL.value}, "submitted"),
    ({"terminal_error": "X"}, "error"),
]


def test_phase_message_for_aborted_matches_frontend_mock():
    payload = {"pipeline_id": 7, "aborted": True}
    phase = S.phase_for(payload)
    assert phase == "idle"
    assert S.phase_message_for(payload, phase) == S.ABORTED_MESSAGE == "已取消生成"


# ---------------------------------------------------------------- 转移表


def test_every_status_is_either_owned_or_out_of_scope():
    """枚举加值时这条会红，而不是静默多出一个「谁都能把它改掉」的状态。"""
    assert OWNED_STATES | OUT_OF_SCOPE_STATES == set(PipelineStatus)
    assert not (OWNED_STATES & OUT_OF_SCOPE_STATES)


def test_transition_table_only_uses_declared_events_and_legal_states():
    """靶点可以落在本模块**没有出边**的状态上（``prepare → preparing`` 是交给 be-13 的），
    但必须落在 ``PipelineStatus`` 里——写错一个枚举名应该是这里红，而不是运行期。"""
    for current, edges in TRANSITIONS.items():
        assert current in OWNED_STATES
        for event, target in edges.items():
            assert event in EVENTS
            assert target in PipelineStatus


def test_transitions_are_read_only():
    """一张能被调用方就地改写的状态机不是状态机，是一个全局变量。"""
    with pytest.raises(TypeError):
        TRANSITIONS[PipelineStatus.DRAFT]["freeze"] = PipelineStatus.VERSION_FROZEN  # type: ignore[index]


@pytest.mark.parametrize(
    ("current", "event", "target"),
    [
        (PipelineStatus.DRAFT, "gate_passed", PipelineStatus.GENERATED),
        (PipelineStatus.DRAFT, "gate_blocked", PipelineStatus.GATE_FAILED),
        (PipelineStatus.DRAFT, "regenerate", PipelineStatus.DRAFT),
        (PipelineStatus.GENERATED, "gate_passed", PipelineStatus.GENERATED),
        (PipelineStatus.GENERATED, "gate_blocked", PipelineStatus.GATE_FAILED),
        (PipelineStatus.GENERATED, "regenerate", PipelineStatus.DRAFT),
        (PipelineStatus.GENERATED, "freeze", PipelineStatus.VERSION_FROZEN),
        (PipelineStatus.GATE_FAILED, "gate_passed", PipelineStatus.GENERATED),
        (PipelineStatus.GATE_FAILED, "gate_blocked", PipelineStatus.GATE_FAILED),
        (PipelineStatus.GATE_FAILED, "regenerate", PipelineStatus.DRAFT),
        (PipelineStatus.VERSION_FROZEN, "prepare", PipelineStatus.PREPARING),
    ],
)
def test_legal_transitions(current, event, target):
    assert can_transition(current, event) is target
    assert assert_transition(current, event) is target


@pytest.mark.parametrize(
    ("current", "event"),
    [
        # 冻结过就代表版本已存在；再生成必须走新 Pipeline，否则版本号连续性会断
        (PipelineStatus.VERSION_FROZEN, "regenerate"),
        (PipelineStatus.VERSION_FROZEN, "gate_passed"),
        (PipelineStatus.VERSION_FROZEN, "freeze"),
        # 门禁没通过不许冻结
        (PipelineStatus.GATE_FAILED, "freeze"),
        # draft 还没产出制品，谈不上准备
        (PipelineStatus.DRAFT, "prepare"),
        (PipelineStatus.DRAFT, "freeze"),
        # 已交接出去的状态在本模块没有出边
        (PipelineStatus.RUNNING, "freeze"),
        (PipelineStatus.PENDING_APPROVAL, "regenerate"),
        (PipelineStatus.SUCCEEDED, "gate_passed"),
        (PipelineStatus.REJECTED, "freeze"),
    ],
)
def test_illegal_transitions_raise_conflict(current, event):
    assert can_transition(current, event) is None
    with pytest.raises(ConflictError) as excinfo:
        assert_transition(current, event, pipeline_id=7, request_id="req-1")
    assert excinfo.value.http_status == 409
    assert excinfo.value.code is ErrorCode.CONFLICT
    assert excinfo.value.details == {"current": str(current), "event": event}


@pytest.mark.parametrize(
    ("verdict", "expected"),
    [
        ("pass", PipelineStatus.GENERATED),
        ("warn", PipelineStatus.GENERATED),
        ("block", PipelineStatus.GATE_FAILED),
    ],
)
def test_status_after_gate(verdict, expected):
    assert status_after_gate(verdict) is expected


def test_status_after_gate_rejects_unknown_verdict():
    """引擎哪天多出第四档结论时，宁可炸也不要静默地把它当通过或阻断。"""
    with pytest.raises(ValueError):
        status_after_gate("skipped")


# ---------------------------------------------------------------- 中断载荷


def test_interrupt_payload_roundtrip():
    """be-09 §3.1 的后端契约形状，经 ``parse_interrupt`` 往返后必须原样。"""
    questions = [
        question_for(key="primary_key", question="请指定去重主键", kind="multi_choice"),
        question_for(
            key="quality_threshold",
            question="拒绝率上限？",
            kind="threshold",
            meta={"min": 0, "max": 1, "unit": "", "ratio": True},
        ),
    ]
    payload = interrupt_payload(questions)
    assert payload == {
        "reason": INTERRUPT_REASON_MISSING_PARAMS,
        "questions": [
            {"param": "primary_key", "prompt": "请指定去重主键"},
            {"param": "quality_threshold", "prompt": "拒绝率上限？"},
        ],
    }
    assert parse_interrupt(payload) == payload


@pytest.mark.parametrize(
    "value",
    [
        None,
        "missing_params",
        {"reason": 1, "questions": []},
        {"reason": "missing_params"},
        [],
        {"reason": "missing_params", "questions": [{"param": "a"}, {"prompt": "b"}, 3]},
    ],
)
def test_parse_interrupt_tolerates_foreign_shapes(value):
    """载荷可能来自**上一个版本的代码**写的检查点；形状不合就收敛成「没有中断」。"""
    parsed = parse_interrupt(value)
    assert parsed is None or parsed["reason"] == "missing_params"


def test_question_for_matches_frontend_clarification_question():
    item = question_for(key="k", question="q", kind="field_ref", hint="h")
    assert item["key"] == "k"
    assert item["kind"] == "field_ref"
    assert item["required"] is True
    assert item["hint"] == "h"
    # 前端的 meta 是 `meta?: {...}`，塞 null 进去会造成 undefined 与 null 的分叉
    assert "meta" not in item


def test_question_for_rejects_unknown_kind():
    with pytest.raises(ValueError):
        question_for(key="k", question="q", kind="dropdown")
    assert "text" in CLARIFICATION_KINDS


def test_parse_resume_input_accepts_both_shapes():
    assert parse_resume_input({"content": "补充：只同步上月", "params": {"dedup_rule": "latest"}}) == (
        {"dedup_rule": "latest"},
        "补充：只同步上月",
    )
    # 前端 answers 接口：值一律字符串
    params, content = parse_resume_input({"answers": {"primary_key": "order_id", "n": 5}})
    assert params == {"primary_key": "order_id", "n": "5"}
    assert content == ""
    assert params["n"] == "5"  # 不猜类型："0755" 猜成 755 会静默改掉一个区号


def test_parse_resume_input_tolerates_none():
    assert parse_resume_input(None) == ({}, "")
    assert parse_resume_input("not-a-dict") == ({}, "")


def test_merge_resume_params_does_not_erase_previous_answers():
    merged = merge_resume_params(
        {"primary_key": ["order_id"]},
        {"primary_key": None, "dedup_rule": "  ", "target_table": "orders_wide"},
    )
    assert merged == {"primary_key": ["order_id"], "target_table": "orders_wide"}


# ---------------------------------------------------------------- 参数完备性


def test_find_missing_params_order_is_declaration_order():
    missing = find_missing_params(EtlIntent(), {})
    assert [item.param for item in missing] == list(REQUIRED_PARAM_ORDER)
    assert all(item.prompt for item in missing)


def test_find_missing_params_falls_back_to_intent():
    intent = EtlIntent(primary_key=["order_id"], incremental_column="updated_at", target_table="t")
    missing = find_missing_params(intent, {})
    assert [item.param for item in missing] == ["dedup_rule", "quality_threshold"]


def test_find_missing_params_quality_threshold_ignores_intent():
    """阈值是治理参数，必须由人拍板——模型猜出来的不算数。"""
    intent = EtlIntent(quality_threshold="0.9000")
    missing = find_missing_params(intent, {})
    assert "quality_threshold" in [item.param for item in missing]
    complete = {
        "primary_key": ["order_id"],
        "incremental_column": "updated_at",
        "dedup_rule": "latest",
        "target_table": "t",
        "quality_threshold": "0.0500",
    }
    assert find_missing_params(intent, complete) == []


def test_find_missing_params_zero_is_not_blank():
    """``0`` 与 ``False`` 不算空：阈值写成 0（不允许任何拒绝）是合法需求。"""
    intent = EtlIntent(primary_key=["id"], incremental_column="dt", dedup_rule="latest", target_table="t")
    assert find_missing_params(intent, {"quality_threshold": 0}) == []


def test_find_missing_params_target_table_hint_branch():
    """be-09 §6.2 的例外：目标档案里已有同名表、用户又没指定新表时用它。"""
    intent = EtlIntent(primary_key=["id"], incremental_column="dt", dedup_rule="latest")
    # 阈值只认 collected（见上一条用例），所以这里缺的是 target_table + quality_threshold
    assert [i.param for i in find_missing_params(intent, {})] == ["target_table", "quality_threshold"]
    assert [i.param for i in find_missing_params(intent, {}, target_table_hint="orders_wide")] == [
        "quality_threshold"
    ]
    assert find_missing_params(intent, {"quality_threshold": "0.05"}, target_table_hint="orders_wide") == []


def test_missing_param_question_shapes():
    missing = {item.param: item for item in find_missing_params(EtlIntent(), {})}
    assert missing["primary_key"].kind == "multi_choice"
    assert missing["incremental_column"].kind == "field_ref"
    assert missing["quality_threshold"].kind == "threshold"
    assert missing["quality_threshold"].meta == {"min": 0, "max": 1, "unit": "", "ratio": True}
    assert missing["target_table"].meta is None


def test_etl_intent_dump_is_json_safe_and_forbids_extra():
    dumped = EtlIntent(requirement="同步 orders").dump()
    assert json.loads(json.dumps(dumped))["requirement"] == "同步 orders"
    with pytest.raises(ValidationError):
        EtlIntent(nonexistent_field=1)


# ---------------------------------------------------------------- 幂等守卫


def test_already_done_requires_fingerprint_and_product():
    fingerprint = S.input_fingerprint(a=1)
    state = {
        "node_fingerprints": {"intent_parse": fingerprint},
        "intent": {"requirement": "x"},
    }
    assert S.already_done(state, "intent_parse", fingerprint, "intent") is True
    # 输入变了 → 不许 no-op（只看「产物在不在」会让「用户改了参数」复用旧产物）
    assert S.already_done(state, "intent_parse", S.input_fingerprint(a=2), "intent") is False
    # 指纹在但产物还没落盘（恢复的中途态）
    assert S.already_done(state, "intent_parse", fingerprint, "draft_plan") is False
    assert S.already_done({}, "intent_parse", fingerprint, "intent") is False


def test_already_done_counts_zero_and_false_as_present():
    fingerprint = S.input_fingerprint(a=1)
    state = {"node_fingerprints": {"gate": fingerprint}, "autofix_rounds": 0}
    assert S.already_done(state, "gate", fingerprint, "autofix_rounds") is True


def test_input_fingerprint_distinguishes_ambiguous_inputs():
    assert S.input_fingerprint(a="1|2") != S.input_fingerprint(a="1", b="2")
    assert S.input_fingerprint(a=1) == S.input_fingerprint(a=1)


# ---------------------------------------------------------------- DSN 归一


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            "postgresql+asyncpg://elt:p%40ss@127.0.0.1:5432/elt",
            "postgresql://elt:p%40ss@127.0.0.1:5432/elt",
        ),
        (
            "postgresql+psycopg://elt:secret@db.internal:5432/elt",
            "postgresql://elt:secret@db.internal:5432/elt",
        ),
        # 已是原生形态 → 原样（不能多一层改写，否则每调一次就变一次）
        ("postgresql://elt:secret@127.0.0.1:5432/elt", "postgresql://elt:secret@127.0.0.1:5432/elt"),
        ("postgres://elt:secret@127.0.0.1:5432/elt", "postgresql://elt:secret@127.0.0.1:5432/elt"),
    ],
)
def test_to_psycopg_dsn_normalizes_three_input_forms(raw, expected):
    from workflow.checkpointer import to_psycopg_dsn

    assert to_psycopg_dsn(raw) == expected


def test_to_psycopg_dsn_handles_sqlalchemy_url_object():
    """密码里的 ``@`` ``/`` 由 ``make_url`` 解析归位，自己拼字符串会把它们漏出来。"""
    from sqlalchemy.engine import make_url

    from workflow.checkpointer import to_psycopg_dsn

    url = make_url("postgresql+asyncpg://elt:p%40ss%2Fword@127.0.0.1:5432/elt")
    dsn = to_psycopg_dsn(url)
    assert dsn.startswith("postgresql://")
    assert "p%40ss%2Fword" in dsn
    assert "+asyncpg" not in dsn


@pytest.mark.parametrize("raw", ["mysql+pymysql://u:p@h:3306/db", "redis://h:6379/0", "not-a-dsn"])
def test_to_psycopg_dsn_rejects_non_postgres(raw):
    """非 PG 的串**抛错**而不是尽力而为：失败点跑到建表那里会离真因隔一层。"""
    from workflow.checkpointer import to_psycopg_dsn

    with pytest.raises(ValueError):
        to_psycopg_dsn(raw)


def test_to_psycopg_dsn_rejects_unknown_pg_driver():
    from workflow.checkpointer import to_psycopg_dsn

    with pytest.raises(ValueError):
        to_psycopg_dsn("postgresql+asyncpg2://u:p@h:5432/db")


def test_checkpointer_is_not_ready_before_open(monkeypatch):
    """未 ``open()`` 就取 saver 必须抛错，**绝不回落 InMemorySaver**
    ——回落会让容器重启后的恢复失败表现成「图从头再跑一遍」。"""
    from workflow import checkpointer as C

    holder = C.CheckpointerHolder("postgresql://u:p@h:5432/db")
    assert holder.ready is False
    with pytest.raises(C.CheckpointerNotReadyError):
        _ = holder.saver

    monkeypatch.setattr(C, "_holder", None)
    with pytest.raises(C.CheckpointerNotReadyError):
        C.get_checkpointer()


def test_checkpointer_holder_does_not_leak_the_password_in_its_dsn_property():
    """``dsn`` 属性是给 ``from_conn_string`` 用的（必须带密码），
    但**日志**走 ``_redacted``——这是「无明文 Secret」红线的落点。"""
    from workflow.checkpointer import _redacted

    assert _redacted("postgresql://elt:secret@127.0.0.1:5432/elt") == "postgresql://127.0.0.1:5432/elt"
    assert "secret" not in _redacted("postgresql://elt:secret@127.0.0.1:5432/elt")
