"""自动修复单元测试（be-11 §6.5 / §6.6）。

回路是纯函数，三件事都以回调解入，所以这里可以**确定性地**证明四条约束：

1. 轮次不超上限；
2. ``policy`` 类阻塞出现时 ``repair`` **零调用**（P0/P1 拦截率不依赖模型行为）；
3. 补丁无效则该轮白算但轮次照常消耗、Plan 不被推进；
4. 去抖——同一个问题第二轮复现时立刻停手，不烧满 3 轮。
"""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from engines.autofix import (
    AutofixOutcome,
    AutofixRequest,
    PatchError,
    PatchOp,
    PlanPatch,
    RepairProposal,
    apply_plan_patch,
    run_autofix_loop,
)
from engines.autofix.loop import arun_autofix_loop
from engines.gate.types import (
    GateFinding,
    GateResult,
    HoconLocator,
    PlanLocator,
    SourceTableLocator,
    compose_conclusion,
    make_finding,
)
from engines.plan.schema import EtlPlanModel

NOW = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)
_HOCON = "env {\n  a = 1\n}\n"


def _plan(**budget: Any) -> EtlPlanModel:
    return EtlPlanModel.model_validate(
        {
            "schema_version": "1.0",
            "inputs": [{"id": "src", "data_source_id": 1, "schema": "s", "table": "t"}],
            "outputs": [{"id": "out", "data_source_id": 2, "database": "d", "table": "w"}],
            "field_mappings": [
                {"source_field": "a", "source_type": "int", "target_field": "b", "target_type": "int"}
            ],
            "transforms": [],
            "filters": [],
            "quality_rules": [],
            "dag": {"nodes": [], "edges": []},
            "budget": {
                "max_read_rows": budget.get("max_read_rows", 1000),
                "max_write_bytes": 2000,
                "max_duration_sec": 60,
                "max_amplification_ratio": Decimal("1.5"),
                "max_reject_rate": Decimal("0.05"),
                "warn_threshold_pct": 80,
            },
        }
    )


def _syntax_block(code: str = "hocon.missing_key", line: int = 1) -> GateFinding:
    return make_finding(
        check_id="syntax",
        code=code,
        severity="block",
        message="HOCON 缺少必填键",
        locator=HoconLocator(line=line, column=1),
    )


def _policy_block() -> GateFinding:
    return make_finding(
        check_id="policy",
        code="P1.cross_db",
        severity="block",
        message="同一方案内跨库",
        locator=SourceTableLocator(data_source_id=1, schema_name="s", table="t"),
    )


def _result(findings: list[GateFinding]) -> GateResult:
    return GateResult(
        evaluated_at=NOW,
        conclusion=compose_conclusion(findings),
        findings=findings,
    )


def _request(
    findings: list[GateFinding], *, plan: EtlPlanModel | None = None, hocon: str = _HOCON
) -> AutofixRequest:
    return AutofixRequest(
        round_no=1, plan=plan if plan is not None else _plan(), hocon_content=hocon, findings=findings
    )


def _boom(_: Any, __: Any = None) -> GateResult:
    raise AssertionError("这个回调不该被调用")


# ---------------------------------------------------------------- 约束 2：policy 不修


def test_policy_阻塞时一轮都不跑() -> None:
    calls: list[AutofixRequest] = []

    def repair(request: AutofixRequest) -> RepairProposal:
        calls.append(request)
        return RepairProposal()

    outcome = run_autofix_loop(
        request=_request([_policy_block(), _syntax_block()]),
        repair=repair,
        recheck=_boom,
        max_rounds=3,
        now_factory=lambda: NOW,
    )

    assert calls == []  # 零调用，不是「调了但返回空补丁」
    assert outcome.stopped_by_policy is True
    assert outcome.rounds_used == 0
    assert outcome.history == []
    assert outcome.result.conclusion == "block"
    assert outcome.autofix_exhausted is False  # 策略阻断不是「修复失败」


def test_策略阻断优先于普通阻塞() -> None:
    """即使同时有可修的 syntax block，也要先因 policy 停手。"""

    def repair(_: AutofixRequest) -> RepairProposal:
        raise AssertionError("policy 阻断时不该进修复")

    outcome = run_autofix_loop(
        request=_request([_syntax_block(), _policy_block()]),
        repair=repair,
        recheck=_boom,
        max_rounds=3,
        now_factory=lambda: NOW,
    )
    assert outcome.stopped_by_policy is True


# ---------------------------------------------------------------- 约束 4：去抖


def test_发现项与上一轮完全相同时立即停下并标耗尽() -> None:
    calls: list[AutofixRequest] = []
    block = _syntax_block()

    def repair(request: AutofixRequest) -> RepairProposal:
        calls.append(request)
        return RepairProposal(patch=PlanPatch(), rationale="空补丁，什么都没改")

    outcome = run_autofix_loop(
        request=_request([block]),
        repair=repair,
        recheck=lambda plan, hocon: _result([block]),
        max_rounds=3,
        now_factory=lambda: NOW,
    )

    assert len(calls) == 1  # 只调了一次，没有傻跑满 3 轮
    assert outcome.rounds_used == 1
    assert outcome.stopped_by_debounce is True
    assert outcome.exhausted is False
    assert outcome.converged is False
    assert outcome.autofix_exhausted is True


def test_发现项在循环里绕回来也会被去抖拦住() -> None:
    """A → B → C → A：循环第 3 轮时发现项回到 A，与第 1 轮同签名 → 原地打转，停手。"""
    calls: list[AutofixRequest] = []

    def repair(request: AutofixRequest) -> RepairProposal:
        calls.append(request)
        target = {1000: 2000, 2000: 3000, 3000: 1000}[request.plan.budget.max_read_rows]
        return RepairProposal(
            patch=PlanPatch(ops=[PatchOp(op="replace", path="/budget/max_read_rows", value=target)])
        )

    def recheck(plan: EtlPlanModel, hocon: str) -> GateResult:
        code = {1000: "A", 2000: "B", 3000: "C"}[plan.budget.max_read_rows]
        return _result([_syntax_block(code=f"hocon.issue_{code}")])

    outcome = run_autofix_loop(
        request=_request([_syntax_block(code="hocon.issue_A")], plan=_plan(max_read_rows=1000)),
        repair=repair,
        recheck=recheck,
        max_rounds=5,
        now_factory=lambda: NOW,
    )

    assert len(calls) == 3  # 第 3 轮的输入回到 A，第 4 轮不再开跑（上限 5 也没用满）
    assert outcome.stopped_by_debounce is True
    assert outcome.rounds_used == 3
    assert outcome.exhausted is False


# ---------------------------------------------------------------- 约束 1：轮次上限


def test_轮次用满时标记耗尽() -> None:
    calls: list[AutofixRequest] = []

    def repair(request: AutofixRequest) -> RepairProposal:
        calls.append(request)
        return RepairProposal(
            patch=PlanPatch(
                ops=[PatchOp(op="replace", path="/budget/max_read_rows", value=1000 + len(calls))]
            )
        )

    def recheck(plan: EtlPlanModel, hocon: str) -> GateResult:
        # 每轮换一个新的 code，绕开去抖，逼它把轮次跑满
        return _result([_syntax_block(code=f"hocon.issue_{plan.budget.max_read_rows}")])

    outcome = run_autofix_loop(
        request=_request([_syntax_block(code="hocon.issue_0")]),
        repair=repair,
        recheck=recheck,
        max_rounds=2,
        now_factory=lambda: NOW,
    )

    assert outcome.rounds_used == 2
    assert outcome.exhausted is True
    assert outcome.converged is False
    assert outcome.stopped_by_debounce is False
    assert outcome.autofix_exhausted is True
    assert len(outcome.history) == 2
    assert [r.round for r in outcome.history] == [1, 2]


# ---------------------------------------------------------------- 收敛


def test_修好了就收敛且_resolved_finding_ids_正确() -> None:
    block = _syntax_block()
    fixed_hocon = "env {\n  a = 2\n}\n"

    def repair(request: AutofixRequest) -> RepairProposal:
        return RepairProposal(
            patch=PlanPatch(ops=[PatchOp(op="replace", path="/budget/max_read_rows", value=2000)]),
            rationale="把预算调大以覆盖预估行数",
            hocon_content=fixed_hocon,
        )

    def recheck(plan: EtlPlanModel, hocon: str) -> GateResult:
        done = plan.budget.max_read_rows == 2000 and hocon == fixed_hocon
        return _result([] if done else [block])

    outcome = run_autofix_loop(
        request=_request([block]),
        repair=repair,
        recheck=recheck,
        max_rounds=3,
        now_factory=lambda: NOW,
    )

    assert outcome.converged is True
    assert outcome.exhausted is False
    assert outcome.autofix_exhausted is False
    assert outcome.rounds_used == 1
    assert outcome.plan.budget.max_read_rows == 2000
    assert outcome.hocon_content == fixed_hocon
    assert outcome.result.findings == []

    round0 = outcome.history[0]
    assert round0.round == 1
    assert round0.trigger_finding_ids == [block.id]
    assert round0.resolved_finding_ids == [block.id]
    assert round0.created_at == NOW
    # 补丁之外没有第二条变更通路：Diff 里能看到这两处改动
    assert "-  a = 1" in round0.hocon_diff and "+  a = 2" in round0.hocon_diff
    assert round0.plan_diff is not None and "max_read_rows" in round0.plan_diff


def test_已经通过就一次都不修() -> None:
    warn = make_finding(
        check_id="mapping",
        code="mapping.dedup_rule_absent",
        severity="warn",
        message="建议补去重",
        locator=PlanLocator(path="/field_mappings/0"),
    )

    def repair(_: AutofixRequest) -> RepairProposal:
        raise AssertionError("没有阻塞项时不该进修复")

    outcome = run_autofix_loop(
        request=_request([warn]),
        repair=repair,
        recheck=_boom,
        max_rounds=3,
        now_factory=lambda: NOW,
    )

    assert outcome.converged is True
    assert outcome.rounds_used == 0
    assert outcome.result.conclusion == "warn"
    assert outcome.autofix_exhausted is False


def test_hocon_覆写为_None_时沿用当前_hocon() -> None:
    block = _syntax_block()

    def repair(_: AutofixRequest) -> RepairProposal:
        return RepairProposal(
            patch=PlanPatch(ops=[PatchOp(op="replace", path="/budget/max_read_rows", value=2000)])
        )

    outcome = run_autofix_loop(
        request=_request([block]),
        repair=repair,
        recheck=lambda plan, hocon: _result([] if plan.budget.max_read_rows == 2000 else [block]),
        max_rounds=3,
        now_factory=lambda: NOW,
    )
    assert outcome.hocon_content == _HOCON
    assert outcome.history[0].hocon_diff == ""


# ---------------------------------------------------------------- 约束 3：补丁无效


def test_补丁路径不存在时该轮白算但计轮次且不推进_plan() -> None:
    block = _syntax_block()
    checked: list[EtlPlanModel] = []

    def repair(_: AutofixRequest) -> RepairProposal:
        return RepairProposal(
            patch=PlanPatch(ops=[PatchOp(op="replace", path="/根本不存在的/路径", value=1)])
        )

    def recheck(plan: EtlPlanModel, hocon: str) -> GateResult:
        checked.append(plan)
        return _result([block])

    outcome = run_autofix_loop(
        request=_request([block]),
        repair=repair,
        recheck=recheck,
        max_rounds=1,
        now_factory=lambda: NOW,
    )

    assert outcome.rounds_used == 1
    # 补丁无效 → Plan 没变 → 发现项与上一轮完全相同 → 去抖（而不是傻等轮次耗尽）
    assert outcome.stopped_by_debounce is True
    assert outcome.autofix_exhausted is True
    assert outcome.plan.budget.max_read_rows == 1000  # Plan 没被推进
    assert all(p.budget.max_read_rows == 1000 for p in checked)
    assert outcome.history[0].plan_diff is None  # 无效轮没有 Plan diff


def test_补丁无效时下一轮拿到的是原样的_findings() -> None:
    """补丁无效不改 Plan，于是下一轮要修的发现项与本轮一模一样——去抖会立刻拦住它。"""
    block = _syntax_block()
    seen_findings: list[list[str]] = []

    def repair(request: AutofixRequest) -> RepairProposal:
        seen_findings.append([f.id for f in request.findings])
        return RepairProposal(patch=PlanPatch(ops=[PatchOp(op="remove", path="/没有这个键")]))

    def recheck(plan: EtlPlanModel, hocon: str) -> GateResult:
        return _result([block])

    outcome = run_autofix_loop(
        request=_request([block]),
        repair=repair,
        recheck=recheck,
        max_rounds=3,
        now_factory=lambda: NOW,
    )
    assert seen_findings == [[block.id]]  # 一模一样的输入在下一轮触发去抖，repair 不再被调
    assert outcome.stopped_by_debounce is True


# ---------------------------------------------------------------- apply_plan_patch


def test_apply_plan_patch_返回新对象且原对象不变() -> None:
    plan = _plan()
    patched = apply_plan_patch(
        plan, PlanPatch(ops=[PatchOp(op="replace", path="/field_mappings/0/target_type", value="varchar")])
    )

    assert patched is not plan
    assert patched.field_mappings[0].target_type == "varchar"
    assert plan.field_mappings[0].target_type == "int"  # 原对象纹丝不动


def test_apply_plan_patch_支持_add_与_remove() -> None:
    plan = _plan()
    added = apply_plan_patch(
        plan,
        PlanPatch(
            ops=[
                PatchOp(
                    op="add",
                    path="/filters/-",
                    value={"id": "f1", "expression": "a > 0", "description": "", "stage": "source"},
                )
            ]
        ),
    )
    assert [f.id for f in added.filters] == ["f1"]

    removed = apply_plan_patch(added, PlanPatch(ops=[PatchOp(op="remove", path="/filters/0")]))
    assert removed.filters == []


def test_apply_plan_patch_改预算无损往返() -> None:
    """Decimal 不能在 patch 往返里丢精度。"""
    plan = _plan()
    patched = apply_plan_patch(
        plan, PlanPatch(ops=[PatchOp(op="replace", path="/budget/max_reject_rate", value="0.05")])
    )
    assert patched.budget.max_reject_rate == Decimal("0.05")
    assert patched.budget.max_amplification_ratio == Decimal("1.5")


@pytest.mark.parametrize(
    "path",
    [
        "",  # 空 Pointer = 替换整份 Plan，不是补丁
        "budget/max_read_rows",  # 缺前导斜杠
        "/budget/没有这个键",  # 键不存在
        "/budget/max_read_rows/deeper",  # 穿过标量
        "/inputs/9/table",  # 数组下标越界
        "/inputs/abc/table",  # 下标不是整数
        "/inputs/-/table",  # '-' 只能用于 add
    ],
)
def test_apply_plan_patch_非法路径抛_PatchError(path: str) -> None:
    with pytest.raises(PatchError):
        apply_plan_patch(_plan(), PlanPatch(ops=[PatchOp(op="replace", path=path, value=1)]))


def test_apply_plan_patch_remove_不存在的键抛错() -> None:
    with pytest.raises(PatchError):
        apply_plan_patch(_plan(), PlanPatch(ops=[PatchOp(op="remove", path="/budget/max_read_rows_typo")]))


def test_apply_plan_patch_空补丁等价于原样() -> None:
    plan = _plan()
    assert apply_plan_patch(plan, PlanPatch()).model_dump() == plan.model_dump()


def test_apply_plan_patch_不就地修改调用方传入的_patch() -> None:
    patch = PlanPatch(ops=[PatchOp(op="replace", path="/budget/max_read_rows", value=99)])
    snapshot = deepcopy(patch.model_dump())
    apply_plan_patch(_plan(), patch)
    assert patch.model_dump() == snapshot


# ---------------------------------------------------------------- outcome 形态


def test_outcome_的_history_与_result_autofix_rounds_一致() -> None:
    block = _syntax_block()

    outcome = run_autofix_loop(
        request=_request([block]),
        repair=lambda _: RepairProposal(patch=PlanPatch()),
        recheck=lambda plan, hocon: _result([block]),
        max_rounds=3,
        now_factory=lambda: NOW,
    )
    assert isinstance(outcome, AutofixOutcome)
    assert outcome.history == outcome.result.autofix_rounds
    assert outcome.result.autofix_exhausted is True


# ---------------------------------------------------------------- 异步驱动
#
# 真实系统里 repair 要调大模型（async），所以回路另有一个异步驱动。两个驱动共用同一份
# 控制流（``_drive``），这里只需证明「异步这条路上四条约束一模一样」——否则会出现
# 「单测绿、线上烧满 3 轮」这种最难受的分裂。


async def test_异步驱动能收敛() -> None:
    calls: list[int] = []
    block = _syntax_block()

    async def repair(request: AutofixRequest) -> RepairProposal:
        calls.append(request.round_no)
        return RepairProposal(
            patch=PlanPatch(ops=[PatchOp(op="replace", path="/budget/max_read_rows", value=2000)])
        )

    outcome = await arun_autofix_loop(
        request=_request([block]),
        repair=repair,
        recheck=lambda plan, hocon: _result([] if plan.budget.max_read_rows == 2000 else [block]),
        max_rounds=3,
        now_factory=lambda: NOW,
    )

    assert calls == [1]
    assert outcome.converged is True
    assert outcome.autofix_exhausted is False


async def test_异步驱动下策略阻断依旧零调用() -> None:
    async def repair(_: AutofixRequest) -> RepairProposal:
        raise AssertionError("policy 阻断时不该进修复")

    outcome = await arun_autofix_loop(
        request=_request([_policy_block()]),
        repair=repair,
        recheck=_boom,
        max_rounds=3,
        now_factory=lambda: NOW,
    )

    assert outcome.stopped_by_policy is True
    assert outcome.rounds_used == 0


async def test_异步驱动下去抖与轮次上限同样生效() -> None:
    block = _syntax_block()
    calls: list[int] = []

    async def repair(request: AutofixRequest) -> RepairProposal:
        calls.append(request.round_no)
        return RepairProposal(patch=PlanPatch())

    outcome = await arun_autofix_loop(
        request=_request([block]),
        repair=repair,
        recheck=lambda plan, hocon: _result([block]),
        max_rounds=3,
        now_factory=lambda: NOW,
    )

    assert calls == [1]  # 停手，没跑满 3 轮
    assert outcome.stopped_by_debounce is True
    assert outcome.autofix_exhausted is True
