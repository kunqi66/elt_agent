"""``check_policy``：消费 PDP 的判定结论（be-11 §5.1 第 1 步 / §6.4）。

**不自己跑 PDP**：``ctx.policy_decision`` 由 service 先算好（async、读策略规则表），
门禁只做「把结论翻译成 finding」。理由见 ``engines/gate/context.py``——
门禁一旦自己去 await PDP，就不再是纯函数，「同制品同策略必同结论」这条可复现性随即失效。

三条判定：

1. **没有结论就不放行**（默认拒绝）：``policy_decision is None`` → block。
   be-11 §6.4 没写这种情况怎么办，但「没判定过」绝不能等于「通过」——
   否则只要 service 少传一个字段，P0/P1 拦截率就不是 100% 了。
2. **逐条消费 hits**：P0/P1 → block（be-11 §6.4 明文规定，门禁阶段的规则集只含
   「制品内可判定」的规则）；P2/P3 → 不产出 finding（不做二次解释）。
3. **预算不得被放宽**：``plan.budget`` 任一项超过 ``decision.budget``（PDP 的治理硬上限）→ block。
   Plan 想收紧自己可以，想放宽不行。

## 新增的 code（be-11 §6.4 未定义）

- ``missing_policy_decision``：见上面第 1 条。
- ``policy_blocked``：``decision.blocked=True`` 但没有一条 P0/P1 hit 覆盖它时的兜底，
  保证「blocked 必然留下 block finding」这个不变量成立。
- ``budget_exceeds_policy``：见上面第 3 条。be-11 §6.4 的表里没有预算维度。
- ``required_approval_slots``（severity=``info``）：把 ``decision.required_slots``
  转成「需要哪些审批槽」的提示。``info`` **不参与结论合成**（be-11 §5.1），
  它只在前端面板上告诉用户「这单后面要谁批」。

## 与 be-11 §3.2 的偏离（登记）

be-11 的 ``Finding`` 有 ``rule_ref`` 字段用来承载命中的 ``policy_rule.code``，
而 ``engines/gate/types.py``（前端已冻结的契约）没有这个字段。因此把规则码
**放进 ``code`` 本身**（``code = hit.code``，逐字即 ``policy_rule.code``）——
这样规则码仍然逐字可见、可被证据 payload 的 ``codes`` 统计到，不丢信息。
"""

from __future__ import annotations

from engines.gate.checks.base import PLAN_ROOT, finalize_findings, safe_message
from engines.gate.context import GateContext
from engines.gate.types import GateFinding, PlanLocator, make_finding
from models.enums import RiskLevel

#: 门禁阶段视为阻断的风险等级（be-11 §6.4）。
_BLOCK_LEVELS = (RiskLevel.P0, RiskLevel.P1)

#: ``BudgetRequest`` 的五项（``RuntimeBudget`` 多一个 ``warn_threshold_pct``，不在上限之列）。
_BUDGET_FIELDS = (
    "max_read_rows",
    "max_write_bytes",
    "max_duration_sec",
    "max_amplification_ratio",
    "max_reject_rate",
)


def check_policy(ctx: GateContext) -> list[GateFinding]:
    """把 PDP 判定翻译成 finding。纯函数：只看 ``ctx``。"""
    decision = ctx.policy_decision
    if decision is None:
        return [
            make_finding(
                check_id="policy",
                code="missing_policy_decision",
                severity="block",
                message=safe_message(
                    "本次门禁没有拿到 PDP 的判定结论；没有策略判定就不能放行（默认拒绝，而不是默认通过）。"
                ),
                locator=PLAN_ROOT,
                suggestion="先完成策略判定（be-07），再把结论交给门禁重跑。",
            )
        ]

    findings: list[GateFinding] = []
    for hit in decision.hits:
        if hit.level not in _BLOCK_LEVELS:
            continue
        findings.append(
            make_finding(
                check_id="policy",
                code=hit.code,
                severity="block",
                message=safe_message(
                    f"命中策略规则 [{hit.level.value}] {hit.code}：{hit.title}。"
                    "策略阻断由确定性规则给出，不会进入自动修复。"
                ),
                locator=PLAN_ROOT,
                suggestion="按规则要求调整方案；策略类阻断必须人工处置，模型不得绕过。",
            )
        )

    if decision.blocked and not any(finding.severity == "block" for finding in findings):
        findings.append(
            make_finding(
                check_id="policy",
                code="policy_blocked",
                severity="block",
                message=safe_message(
                    f"PDP 判定为阻断（风险等级 {decision.level.value}），但没有对应的命中规则明细。"
                ),
                locator=PLAN_ROOT,
                suggestion="查看策略判定记录，确认阻断原因后人工处置。",
            )
        )

    findings.extend(_budget_findings(ctx))

    if decision.required_slots:
        slots = "、".join(slot.value for slot in decision.required_slots)
        findings.append(
            make_finding(
                check_id="policy",
                code="required_approval_slots",
                severity="info",
                message=safe_message(f"本次方案后续需要这些审批槽：{slots}。"),
                locator=PLAN_ROOT,
                suggestion="提前安排对应的数据审批人与安全审批人。",
            )
        )

    return finalize_findings("policy", findings)


def _budget_findings(ctx: GateContext) -> list[GateFinding]:
    """Plan 的预算不得超过 PDP 批复的上限（治理硬上限，Plan 不能自行放宽）。"""
    decision = ctx.policy_decision
    if decision is None:  # 由调用方的 None 分支保证，这里只是让类型收窄
        return []
    plan_budget = ctx.plan.budget
    policy_budget = decision.budget

    findings: list[GateFinding] = []
    for name in _BUDGET_FIELDS:
        plan_value = getattr(plan_budget, name)
        policy_value = getattr(policy_budget, name)
        if plan_value <= policy_value:
            continue
        findings.append(
            make_finding(
                check_id="policy",
                code="budget_exceeds_policy",
                severity="block",
                message=safe_message(
                    f"Plan 的 /budget/{name} = {plan_value}，超过 PDP 批复的上限 {policy_value}；"
                    "Plan 不能自行放宽治理预算。"
                ),
                locator=PlanLocator(path=f"/budget/{name}"),
                suggestion="把预算收敛到策略允许的上限内，或走策略调整流程。",
            )
        )
    return findings


__all__ = ["check_policy"]
