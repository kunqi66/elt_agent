"""生成链路 HTTP 层：路径注册顺序、DTO 形状、引擎类型复用。

不落 HTTP 请求（那要连 PG，本机没有 Docker）。这里挡的是三类**静默事故**：

1. **路径被动态段吞掉**：``/pipeline-versions/diff`` 排在 ``/{version_id}`` 之后，
   于是 ``diff`` 被当成版本号吃进 int 校验 → 422。前端会把它显示成「参数错误」，
   而真正的差异接口根本没被调用过。断言走 ``app.openapi()["paths"]`` 的**键序**，
   **不能**数 ``app.routes``：新版 Starlette 把 ``include_router`` 的子路由包成
   ``_IncludedRouter``，按路径遍历看不到它们（CLAUDE.md §9 记过这个坑）。
2. **DTO 与前端漂移**：字段名写错不会报错，只会让界面显示空白。
3. **门禁类型被抄成两份**：两份定义 = 两个真相，字段漂移时不会有任何报错。

⚠️ ``services/pipeline/`` 由另一个 agent 并行落地，未落地时 ``main`` 无法 import。
依赖 ``create_app()`` 的断言会显式 skip 并说明原因，**不会**为了跑绿而 stub 掉 service；
不依赖 service 的断言（DTO 形状、引擎复用、源文件注册顺序）任何时候都跑。
"""

from __future__ import annotations

import ast
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

import engines.gate.types as gate_types
import engines.plan.schema as plan_schema
import schemas.pipeline as pipeline_schemas
import schemas.version as version_schemas
from schemas.pipeline import (
    AbortRequest,
    AnswerInput,
    AnswersRequest,
    EtlPlanRead,
    HoconArtifactRead,
    PipelineFilter,
    PipelineRead,
    StartGenerationRequest,
    WorkflowSnapshot,
)
from schemas.version import (
    AutofixRequest,
    FreezeArtifactManifest,
    FreezeRequest,
    GenerationContext,
    PipelineVersionDetail,
    PipelineVersionSummary,
    VersionDiff,
)

BACKEND = Path(__file__).resolve().parents[1]
API_V1 = "/api/v1"

# ------------------------------------------------------------------ 前端样例
#
# 下列数据逐字取自 ``frontend/src/api/mock/fixtures.ts`` 与
# ``frontend/src/api/mock/index.ts``——它们是**线上形态的权威**，
# 用自己编的数据测 DTO 只能证明「自己编的数据能过自己的校验」。

_PIPELINE: dict[str, Any] = {
    "id": 1,
    "project_id": 1,
    "name": "ods_orders_to_dw",
    "description": "订单源表 → 订单宽表（日增量）",
    "status": "succeeded",
    "current_version_id": 101,
    "source_data_source_id": 1,
    "target_data_source_id": 4,
    "created_at": "2026-10-16T10:00:00Z",
    "updated_at": "2026-10-16T12:00:00Z",
}

_SNAPSHOT: dict[str, Any] = {
    "thread_id": "thread-clarify",
    "phase": "awaiting_clarification",
    "phase_message": "需要补充两个口径后才能生成方案",
    "pending_questions": [
        {
            "key": "incremental_column",
            "question": "增量切片使用哪一列？",
            "kind": "single_choice",
            "options": [
                {"value": "updated_at", "label": "updated_at（更新时间）"},
                {"value": "created_at", "label": "created_at（创建时间）"},
            ],
            "required": True,
            "hint": "源表中两者都存在，语义不同",
        },
        {
            "key": "reject_rate_budget",
            "question": "可接受的拒绝率上限是多少？",
            "kind": "threshold",
            "options": None,
            "required": True,
            "hint": None,
            "meta": {"min": 0, "max": 0.2, "unit": "", "ratio": True},
        },
    ],
    "autofix_round": 0,
    "last_error": None,
}

_PLAN_BODY: dict[str, Any] = {
    "inputs": [{"id": "src_1", "data_source_id": 1, "schema": "ods", "table": "orders"}],
    "outputs": [
        {
            "id": "sink_1",
            "data_source_id": 4,
            "database": "dw",
            "table": "orders_wide",
            "mode": "shadow_then_swap",
        }
    ],
    "field_mappings": [
        {
            "source_field": "id",
            "source_type": "bigint",
            "target_field": "id",
            "target_type": "bigint",
            "transform": "none",
            "masking": None,
            "quality_rule_ids": ["qk_001"],
            "change": "added",
        }
    ],
    "transforms": [],
    "filters": [
        {
            "id": "flt_1",
            "expression": "updated_at >= '2026-10-15'",
            "description": "日增量",
            "stage": "source",
        }
    ],
    "quality_rules": [
        {
            "id": "qk_001",
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
            {"id": "src_1", "type": "source", "label": "订单源表", "meta": {}, "position": {"x": 0, "y": 80}}
        ],
        "edges": [],
    },
    "budget": {
        "max_read_rows": 5_000_000,
        "max_write_bytes": 2_147_483_648,
        "max_duration_sec": 3600,
        "max_amplification_ratio": 3,
        "max_reject_rate": 0.05,
        "warn_threshold_pct": 80,
    },
}

_PLAN: dict[str, Any] = {
    "id": 101,
    "schema_version": "1.0",
    "plan": _PLAN_BODY,
    "valid": True,
    "validation_errors": [{"path": "/field_mappings/3/target_type", "reason": "类型不兼容"}],
}

_HOCON: dict[str, Any] = {
    "id": 55,
    "content": 'env {\n  job.mode = "BATCH"\n}\n',
    "sha256": "a" * 64,
    "parser_version": "seatunnel-2.3.8",
    "validation_result": {"ok": True, "warnings": []},
}

# 四种 GateLocator 各一条： discriminated union 少认一种，前端就有一类 finding 跳不动
_GATE: dict[str, Any] = {
    "evaluated_at": "2026-10-16T11:00:00Z",
    "conclusion": "block",
    "findings": [
        {
            "id": "g103_1",
            "severity": "block",
            "category": "policy",
            "code": "POLICY_BLOCKED",
            "message": "目标表命中 P0 策略。",
            "locator": {"kind": "dag_node", "node_id": "sink_1"},
            "suggestion": "补齐 checker_security 审批槽。",
        },
        {
            "id": "g103_2",
            "severity": "warn",
            "category": "syntax",
            "code": "HOCON_SYNTAX_ERROR",
            "message": "第 14 行缺少闭合花括号。",
            "locator": {"kind": "hocon", "line": 14, "column": 3, "end_line": 14},
            "suggestion": None,
        },
        {
            "id": "g103_3",
            "severity": "warn",
            "category": "field_mapping",
            "code": "SCHEMA_MISMATCH",
            "message": "来源与目标类型不兼容。",
            "locator": {"kind": "plan", "path": "field_mappings[3].target_type"},
            "suggestion": "增加 cast 转换。",
        },
        {
            "id": "g103_4",
            "severity": "info",
            "category": "schema",
            "code": "SCHEMA_MISMATCH",
            "message": "源表缺少列 updated_at。",
            "locator": {"kind": "source_table", "data_source_id": 1, "schema": "ods", "table": "payment"},
            "suggestion": None,
        },
    ],
    "autofix_rounds": [
        {
            "round": 1,
            "trigger_finding_ids": ["g103_2"],
            "hocon_diff": "--- a/job.conf\n+++ b/job.conf\n",
            "plan_diff": None,
            "resolved_finding_ids": ["g103_2"],
            "created_at": "2026-10-16T11:05:00Z",
        }
    ],
    "autofix_exhausted": False,
}

_MANIFEST: dict[str, Any] = {
    "plan": {"schema_version": "1.0", "valid": True},
    "hocon": {"sha256": "a" * 64, "parser_version": "seatunnel-2.3.8"},
    "quality_contract": {"rule_count": 4, "reject_rate_budget": 0.05},
    "supervision": _PLAN_BODY["budget"],
    "metadata_profile": {"version": 1, "sha256": "b" * 64, "expires_at": "2026-10-19T12:00:00Z"},
}

_VERSION_SUMMARY: dict[str, Any] = {
    "id": 101,
    "version_no": 3,
    "sha256": "c" * 64,
    "status_note": "门禁通过，已冻结为不可变版本",
    "frozen_at": "2026-10-16T09:40:00Z",
    "frozen_by_display_name": "林思远",
    "source_metadata_profile_version": 1,
    "generation_context": {
        "model_name": "deepseek-chat",
        "model_version": "2026-08-01",
        "prompt_template_version": "etlplan-v3",
        "autofix_rounds": 0,
    },
}


# ------------------------------------------------------------------ 路径注册顺序


def _openapi_paths() -> dict[str, Any]:
    """取应用的真实路由表。service 未落地时 skip，而不是 stub。"""
    try:
        from main import create_app
    except ImportError as exc:  # pragma: no cover - 取决于并行开发的落地时机
        pytest.skip(f"services/pipeline 尚未落地，端到端路由断言待补：{exc}")
    return create_app().openapi()["paths"]


def test_diff必须注册在version_id之前() -> None:
    """顺序反了，``diff`` 会被当成 ``version_id=diff`` 吃进 int 校验并返回 422。"""
    keys = list(_openapi_paths())
    diff = f"{API_V1}/pipeline-versions/diff"
    detail = f"{API_V1}/pipeline-versions/{{version_id}}"
    assert diff in keys, "Diff 路由没挂上"
    assert detail in keys, "版本详情路由没挂上"
    assert keys.index(diff) < keys.index(detail)


def test_源文件里diff装饰器排在version_id之前() -> None:
    """上一条要等 service 落地才能跑；这条只读源码，任何时候都能挡住顺序被改回去。

    Starlette 按**注册顺序**匹配，顺序即优先级，所以文件里的先后就是运行时行为。
    """
    src = (BACKEND / "api" / "pipeline_versions.py").read_text(encoding="utf-8")
    assert src.index('router.get("/diff"') < src.index('router.get("/{version_id}"')


def test_生成会话路由排在动态段之前() -> None:
    """``/generations/{thread_id}`` 与 ``/{pipeline_id}/versions`` 段数相同，
    顺序反了 ``generations`` 会被当成 ``pipeline_id`` 吃进 int 校验。"""
    keys = list(_openapi_paths())
    generations = f"{API_V1}/pipelines/generations/{{thread_id}}"
    versions = f"{API_V1}/pipelines/{{pipeline_id}}/versions"
    assert keys.index(generations) < keys.index(versions)


def test_前端十四个函数都有对应路由() -> None:
    """``frontend/src/api/pipeline.ts`` 的 14 个函数逐个核对（少一个就是静默 404）。"""
    # 保留 dict（不是 set）：下面既要用路径做成员判断，又要读它挂的 HTTP 方法。
    # 转成 set 之后 `keys[path]` 会抛 TypeError——而那条报错完全看不出是「测试写错了」，
    # 看起来像是路由表本身有问题
    keys = _openapi_paths()
    expected = {
        f"{API_V1}/pipelines": "get",
        f"{API_V1}/pipelines/{{pipeline_id}}": "get",
        f"{API_V1}/pipelines/{{pipeline_id}}/versions": "get",
        f"{API_V1}/pipelines/generations": "post",
        f"{API_V1}/pipelines/generations/{{thread_id}}": "get",
        f"{API_V1}/pipelines/generations/{{thread_id}}/answers": "post",
        f"{API_V1}/pipelines/generations/{{thread_id}}/abort": "post",
        f"{API_V1}/pipeline-versions/diff": "get",
        f"{API_V1}/pipeline-versions/{{version_id}}": "get",
        f"{API_V1}/pipeline-versions/{{version_id}}/plan": "get",
        f"{API_V1}/pipeline-versions/{{version_id}}/hocon": "get",
        f"{API_V1}/pipeline-versions/{{version_id}}/gate": "get",
        f"{API_V1}/pipeline-versions/{{version_id}}/autofix": "post",
        f"{API_V1}/pipeline-versions/{{version_id}}/freeze": "post",
    }
    assert len(expected) == 14
    for path, method in expected.items():
        assert path in keys, f"路由未挂载：{path}"
        assert method in keys[path], f"{path} 的 HTTP 方法不对"


# ------------------------------------------------------------------ 引擎类型复用


def test_门禁与自动修复类型直接复用引擎定义() -> None:
    """``is`` 同一是刻意的：抄一份副本不会有任何报错，只会在某天字段漂移时分裂。"""
    assert pipeline_schemas.GateResult is gate_types.GateResult
    assert pipeline_schemas.GateFinding is gate_types.GateFinding
    assert pipeline_schemas.GateLocator is gate_types.GateLocator
    assert pipeline_schemas.AutofixRound is gate_types.AutofixRound
    # service 契约里的名字是别名，不是子类
    assert pipeline_schemas.GateResultRead is gate_types.GateResult
    assert pipeline_schemas.AutofixRoundRead is gate_types.AutofixRound
    # Plan：响应模型里嵌的就是引擎那个模型，不是它的复制品
    assert pipeline_schemas.EtlPlanRead.model_fields["plan"].annotation is plan_schema.EtlPlanModel
    # 比率字段保持 Decimal：换成 float 会让「刚好等于上限」被判成超限
    assert (
        version_schemas.FreezeArtifactManifest.model_fields["supervision"].annotation
        is plan_schema.RuntimeBudget
    )


def test_schemas里没有第二份门禁或Plan定义() -> None:
    """AST 层面挡住「顺手抄一份」：新增同名 class 会让上面那条断言失效。"""
    forbidden = {"GateResult", "GateFinding", "GateLocator", "AutofixRound", "EtlPlanModel", "RuntimeBudget"}
    for name in ("pipeline.py", "version.py"):
        tree = ast.parse((BACKEND / "schemas" / name).read_text(encoding="utf-8"))
        declared = {node.name for node in tree.body if isinstance(node, ast.ClassDef)}
        assert not (declared & forbidden), f"schemas/{name} 里出现了引擎类型的副本：{declared & forbidden}"


# ------------------------------------------------------------------ DTO 形状


def test_流水线DTO接受前端形状() -> None:
    read = PipelineRead.model_validate(_PIPELINE)
    assert read.status.value == "succeeded"
    assert read.current_version_id == 101


def test_流水线筛选的status是数组() -> None:
    """前端多选后传的是逗号串，但 DTO 的口径是数组——拆串是路由的事。"""
    flt = PipelineFilter.model_validate(
        {"status": ["failed", "interrupted"], "q": "orders", "page": 1, "page_size": 20}
    )
    assert [s.value for s in flt.status or []] == ["failed", "interrupted"]


def test_工作流快照接受前端形状() -> None:
    snap = WorkflowSnapshot.model_validate(_SNAPSHOT)
    assert snap.phase == "awaiting_clarification"
    assert snap.pending_questions[1].kind == "threshold"
    assert snap.pending_questions[1].meta is not None
    assert snap.pending_questions[1].meta.ratio is True
    # 非选择题的 options 是 null 而不是空数组：两者语义不同
    assert snap.pending_questions[1].options is None


def test_发起生成的请求体含project_id且禁止多余字段() -> None:
    payload = StartGenerationRequest.model_validate(
        {
            "project_id": 1,
            "requirement": "把 ods.orders 同步到 dw.orders_wide",
            "source_data_source_id": 1,
            "target_data_source_id": 4,
        }
    )
    assert payload.project_id == 1
    with pytest.raises(ValidationError):
        StartGenerationRequest.model_validate(
            {
                "project_id": 1,
                "requirement": "x",
                "source_data_source_id": 1,
                "target_data_source_id": 4,
                "tenant_id": 9,  # 租户只能来自登录态，多传一个键就该当场报错
            }
        )


def test_澄清回答的线上形状是映射() -> None:
    """前端序列化的是 ``Record<string,string>``，声明成数组就是 422。"""
    req = AnswersRequest.model_validate({"answers": {"incremental_column": "updated_at"}})
    assert req.as_inputs() == [AnswerInput(question_id="incremental_column", value="updated_at")]
    with pytest.raises(ValidationError):
        AnswersRequest.model_validate({"answers": [{"question_id": "a", "value": "b"}]})


def test_中断请求可用空reason() -> None:
    """``reason`` 只是请求体兼容字段（service 目前不收它），不能因为缺它而 422。"""
    assert AbortRequest.model_validate({}).reason == ""


def test_Plan制品接受引擎形状() -> None:
    plan = EtlPlanRead.model_validate(_PLAN)
    assert plan.valid is True
    assert plan.validation_errors[0].reason == "类型不兼容"
    # 别名出口：``schema`` 必须还原成 ``schema``（不是 ``schema_name``）
    dumped = plan.model_dump(mode="json", by_alias=True)
    assert dumped["plan"]["inputs"][0]["schema"] == "ods"


def test_HOCON制品接受前端形状() -> None:
    hocon = HoconArtifactRead.model_validate(_HOCON)
    assert hocon.parser_version == "seatunnel-2.3.8"
    assert hocon.validation_result["ok"] is True


def test_门禁结果四种定位点都能解析() -> None:
    result = gate_types.GateResult.model_validate(_GATE)
    assert result.conclusion == "block"
    kinds = {f.locator.kind for f in result.findings}
    assert kinds == {"dag_node", "hocon", "plan", "source_table"}
    assert result.autofix_rounds[0].round == 1
    # 结论与 finding 一致：block 就是有一条第 P0 级阻塞项
    assert result.has_policy_block is True
    assert len(result.blocking) == 1


def test_版本摘要与详情接受前端形状() -> None:
    summary = PipelineVersionSummary.model_validate(_VERSION_SUMMARY)
    assert summary.status_note == "门禁通过，已冻结为不可变版本"
    assert summary.generation_context.model_name == "deepseek-chat"

    detail = PipelineVersionDetail.model_validate(
        {**_VERSION_SUMMARY, "manifest": _MANIFEST, "plan": _PLAN, "hocon": _HOCON}
    )
    assert detail.manifest.hocon.parser_version == "seatunnel-2.3.8"
    assert detail.plan.id == 101
    assert detail.hocon.id == 55


def test_版本摘要容忍空的生成上下文() -> None:
    """``pipeline_version.generation_context`` 的 DB 默认是 ``{}``：
    缺元数据是「历史数据不全」，不该让整个列表接口 500。"""
    summary = PipelineVersionSummary.model_validate({**_VERSION_SUMMARY, "generation_context": {}})
    assert summary.generation_context.autofix_rounds == 0


def test_冻结清单与请求体接受前端形状() -> None:
    manifest = FreezeArtifactManifest.model_validate(_MANIFEST)
    assert manifest.quality_contract.rule_count == 4
    assert manifest.supervision.max_reject_rate == Decimal("0.05")

    assert FreezeRequest.model_validate({"note": "首次冻结"}).note == "首次冻结"
    assert FreezeRequest.model_validate({}).note == ""
    assert AutofixRequest.model_validate({"note": "手工再修一轮"}).note == "手工再修一轮"


def test_版本Diff接受前端形状() -> None:
    diff = VersionDiff.model_validate(
        {
            "entries": [
                {
                    "path": "field_mappings[3].target_type",
                    "kind": "changed",
                    "before": "varchar(64)",
                    "after": "decimal(18,2)",
                },
                {"path": "quality_rules[3]", "kind": "added", "after": "qk_004 enum(status)"},
                {"path": "dag.nodes[2]", "kind": "removed", "before": "filter_1（版本 101 独有）"},
            ],
            "base_version_id": 101,
            "target_version_id": 103,
        }
    )
    assert [entry.kind for entry in diff.entries] == ["changed", "added", "removed"]
    # added 没有 before、removed 没有 after——缺省就是语义本身，不是空串
    assert diff.entries[1].before is None
    assert diff.entries[2].after is None
    assert diff.base_version_id == 101


def test_生成上下文单独可用() -> None:
    ctx = GenerationContext.model_validate({"model_name": "deepseek-reasoner", "autofix_rounds": 2})
    assert ctx.model_name == "deepseek-reasoner"
    assert ctx.prompt_template_version == ""
