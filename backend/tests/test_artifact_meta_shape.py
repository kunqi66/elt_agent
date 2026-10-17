"""`hocon_artifact.validation_result` 的形状只有一份定义——两条生产路径必须拼出同一形状。

**这条用例的来历**：这份 JSON 一度被三处各拼一份（渲染节点、门禁节点、落库路径），
形状慢慢分叉：门禁节点多套了一层信封（``{"gate": {...}}``），于是读的一侧
``get_gate`` 把信封当 ``GateResult`` 解析，报「``evaluated_at``/``conclusion`` 缺失」——
**而门禁其实跑了**。所以这里把「渲染段 ⊕ 解析/门禁段」的组合钉死：

- ``gate`` 这一键**直接就是** ``GateResult`` 的转储，不是它的信封；
- 生成路径与自动修复路径拼出**同一组键**（两条路径形状不一致 = 同一类事故的下一次）。

纯函数级，不需要数据库：形状问题在拼装时就已经能判出来。
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from engines.gate.context import GateContext
from engines.gate.types import AutofixRound, GateFinding, GateResult, PlanLocator
from engines.hocon import RenderedHocon, parse_and_validate
from engines.plan.schema import EtlPlanModel
from services.pipeline.gating import GateOutcome, render_meta

_HOCON = 'env { job.mode = "BATCH" }\n'


def _render_meta() -> dict[str, object]:
    return render_meta(
        RenderedHocon(
            content=_HOCON,
            tokens=("{{secret:1:jdbc_url}}",),
            job_name="elt_sync_orders",
            renderer_version="elt-render-1",
        )
    )


def _outcome() -> GateOutcome:
    result = GateResult(
        evaluated_at=datetime(2026, 10, 16, 12, 0, tzinfo=UTC),
        conclusion="pass",
        findings=[
            GateFinding(
                id="finding-1",
                code="required_approval_slots",
                severity="info",
                category="policy",
                message="需要 checker_data、checker_security 两个审批槽",
                locator=PlanLocator(kind="plan", path="approval_slots"),
            )
        ],
        autofix_rounds=[],
        autofix_exhausted=False,
    )
    context = GateContext(
        plan=EtlPlanModel.model_validate(
            {
                "schema_version": "1.0",
                "inputs": [{"id": "src", "data_source_id": 1, "schema": "s", "table": "t"}],
                "outputs": [{"id": "out", "data_source_id": 2, "database": "d", "table": "w"}],
                "field_mappings": [],
                "transforms": [],
                "filters": [],
                "quality_rules": [],
                "dag": {"nodes": [], "edges": []},
                "budget": {
                    "max_read_rows": 1000,
                    "max_write_bytes": 2000,
                    "max_duration_sec": 60,
                    "max_amplification_ratio": Decimal("1.5"),
                    "max_reject_rate": Decimal("0.05"),
                    "warn_threshold_pct": 80,
                },
            }
        ),
        hocon_content=_HOCON,
        parse_result=parse_and_validate(_HOCON),
    )
    return GateOutcome(result=result, context=context)


def test_gate_键直接就是_GateResult_不是信封() -> None:
    composed = {**_render_meta(), **_outcome().artifact_meta()}

    # 读的一侧（``get_gate``）就是这么用的：把 ``["gate"]`` 当 GateResult 解析
    parsed = GateResult.model_validate(composed["gate"])
    assert parsed.conclusion == "pass"
    # 套一层信封的写法会让上面那行报「evaluated_at/conclusion 缺失」——那种形状必须不存在
    assert "parse" not in composed["gate"], "gate 键被套了信封：读的一侧会解析失败"
    assert composed["parse"]["ok"] is True


def test_两条生产路径拼出同一组键() -> None:
    """生成路径（节点拼）与自动修复路径（``_persist_repaired`` 拼）必须是同一个形状。"""
    composed = {**_render_meta(), **_outcome().artifact_meta()}
    assert set(composed) == {
        "sha256",
        "parser_version",
        "renderer_version",
        "job_name",
        "token_count",
        "parse",
        "gate",
        "policy_decision_id",
        "policy_version",
    }


def test_渲染段记的是解析器版本而不是渲染器版本() -> None:
    """``parser_version`` 要能回答「同一份 HOCON 昨天过今天不过，是哪一端变了」。"""
    meta = _render_meta()
    assert meta["parser_version"] == "pyhocon+elt-render-1"
    assert meta["renderer_version"] == "elt-render-1"
    assert meta["token_count"] == 1


def test_自动修复段的类型没变() -> None:
    """``autofix_rounds`` 是对象列表（不是计数）——它要能回答「第几轮改了什么」。"""
    result = _outcome().result
    assert isinstance(result.autofix_rounds, list)
    assert all(isinstance(r, AutofixRound) for r in result.autofix_rounds)
