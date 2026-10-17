"""有限自动修复的回路控制流（be-11 §6.6 的四条约束）。

**它是纯函数**：不调 LLM、不渲染、不跑门禁、不取时间。三件事都以回调解入——

- ``repair``：真调 DeepSeek 还是假 Provider 由 service 决定；
- ``recheck``：重新跑门禁（service 侧组装输入、调 ``engines.gate.evaluate``）；
- ``now_factory``：时间戳由调用方给（``engines`` 内不许自己取当前时间）。

这样做的代价是签名长，收益是「修复回路本身可被单测逐条钉死」：注入一个永远原地打转的
``repair`` 就能确定性地验证去抖，注入一个计数器就能证明策略阻断时 ``repair`` 零调用。

四条约束（be-11 §6.6）：

1. 轮次 ≤ ``max_rounds``（默认取 ``settings.LLM_AUTOFIX_MAX_ROUNDS`` = 3，由调用方传入；
   本模块不 import settings——``engines`` 不读配置，否则同一份制品在不同环境修复结果不同）。
2. **``policy`` 类阻塞绝不进入修复**：让模型去「修掉」策略阻断等于把 P0/P1 判定交给 LLM
   （PRD §1.4 明令禁止），故一旦出现就地停手、``repair`` 零调用。
3. **补丁是唯一变更形态**：每轮只记录 ``patch`` 与前后 diff，不在补丁之外偷偷改状态；
   ``apply_plan_patch`` 抛 :class:`PatchError` 时该轮记为无效（不推进 Plan，但轮次照常 +1）。
4. **去抖**：若本轮要修的阻塞项与**此前某轮**完全相同，说明修复没让任何东西前进，
   立刻停下并标记 ``stopped_by_debounce``——不要傻跑满 3 轮烧 token。
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Generator
from datetime import datetime
from typing import cast

from pydantic import BaseModel, ConfigDict, Field

from engines.autofix.patch import PatchError, PlanPatch, RepairProposal, apply_plan_patch
from engines.diff.version_diff import hocon_unified_diff, unified_text_diff
from engines.gate.types import (
    AutofixRound,
    GateFinding,
    GateResult,
    compose_conclusion,
)
from engines.plan.schema import EtlPlanModel

#: 修复回调：给一轮的输入（Plan / HOCON / 待修 finding），产出补丁。由 service 注入。
RepairFn = Callable[["AutofixRequest"], RepairProposal]
#: 异步修复回调：**真实系统走这条**（修复要调大模型，而模型是 ``async`` 的）。
#: 两个签名都留着，是因为回路本体与「修一次要花多久」无关：
#: 同步版给单测和离线批处理用（一次不联网的修复不该被拖进事件循环）。
AsyncRepairFn = Callable[["AutofixRequest"], Awaitable[RepairProposal]]
#: 复检回调：对修复后的 (Plan, HOCON) 重跑门禁。由 service 注入。
RecheckFn = Callable[[EtlPlanModel, str], GateResult]
#: 时间源。由 service 注入（``engines`` 不许自己取当前时间）。
NowFactory = Callable[[], datetime]

#: 去抖签名：只由**稳定的**三元组构成（``id`` 里若混进随机数 / 时间戳，同一个问题会被当成新问题）。
Signature = frozenset[tuple[str, str, str]]


class AutofixRequest(BaseModel):
    """一轮修复的输入（be-11 §6.6）。

    ``hocon_content`` 而不是 ``hocon``：与 ``GateContext`` / ``ArtifactParts`` 的字段名一致，
    免得在 service 里到处改名。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    round_no: int = Field(ge=1)
    plan: EtlPlanModel
    hocon_content: str
    findings: list[GateFinding] = Field(default_factory=list)


class AutofixOutcome(BaseModel):
    """修复回路的产出。

    ``result`` 是**最后一轮**的 ``GateResult``，其 ``autofix_rounds`` 与
    ``autofix_exhausted`` 已被本回路填好——service 直接把它当门禁响应返回即可。

    三个停止原因分开记（``converged`` / ``exhausted`` / ``stopped_by_debounce``），
    因为它们在运维上完全不同：收敛是成功，轮次耗尽是「模型没辙了」，去抖是「模型在原地打转」。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    plan: EtlPlanModel
    hocon_content: str
    result: GateResult
    rounds_used: int = 0
    converged: bool = False
    exhausted: bool = False
    stopped_by_debounce: bool = False
    stopped_by_policy: bool = False

    @property
    def history(self) -> list[AutofixRound]:
        """逐轮记录（== ``result.autofix_rounds``），前端用它渲染每轮 diff。"""
        return self.result.autofix_rounds

    @property
    def autofix_exhausted(self) -> bool:
        """前端口径：**自动修复没能修好**（轮次耗尽或去抖终止）。

        前端 ``GateResult`` 只有 ``autofix_exhausted`` 一个布尔，没有 ``stopped_by_debounce``，
        所以这里把两种「没修好」都算进去；要区分原因读 ``exhausted`` / ``stopped_by_debounce``。
        """
        return self.exhausted or self.stopped_by_debounce


def _drive(
    *,
    request: AutofixRequest,
    recheck: RecheckFn,
    max_rounds: int,
    now_factory: NowFactory,
) -> Generator[AutofixRequest, RepairProposal, AutofixOutcome]:
    """回路本体：**一个生成器，被两个驱动共用**（同步 / 异步各一个）。

    为什么写成生成器：修复这一步在真实系统里要调大模型（``async``），
    而回路本体是同步纯逻辑。若为此把本函数改成 ``async def``，
    Celery 里那些纯同步的调用方就被迫套一层事件循环；
    若改成两处各写一遍控制流，「最多 3 轮 / 去重 / 策略短路」这些约束
    就会出现两份实现——一份改了另一份没改，**而且不会有任何报错**。

    生成器的 ``yield`` 正好是「把请求交出去、等一个提案回来」这个语义：
    同步驱动用 ``repair(req)``，异步驱动用 ``await repair(req)``，
    收敛判据、去重、轮次消耗全部只有这一份。
    """
    plan = request.plan
    hocon = request.hocon_content
    findings = list(request.findings)

    # 没有阻塞项就没什么可修的：直接收敛（不调 repair，也别谎报 exhausted）
    if not _has_block(findings):
        return _finish(
            plan=plan,
            hocon=hocon,
            findings=findings,
            rounds=[],
            used=0,
            converged=True,
            exhausted=False,
            debounce=False,
            policy=False,
            now_factory=now_factory,
        )

    # 约束 2：策略类阻塞出现 → 就地停手，repair 一次都不调
    if _has_policy_block(findings):
        return _finish(
            plan=plan,
            hocon=hocon,
            findings=findings,
            rounds=[],
            used=0,
            converged=False,
            exhausted=False,
            debounce=False,
            policy=True,
            now_factory=now_factory,
        )

    rounds: list[AutofixRound] = []
    seen: set[Signature] = {_signature(findings)}
    converged = False
    debounce = False
    last_result: GateResult | None = None

    for round_no in range(1, max_rounds + 1):
        proposal = yield AutofixRequest(
            round_no=round_no, plan=plan, hocon_content=hocon, findings=list(findings)
        )
        # 约束 3：apply 失败则该轮无效——Plan 原地不动，但轮次照常消耗
        try:
            patched = apply_plan_patch(plan, proposal.patch)
        except PatchError:
            patched = plan
        next_hocon = hocon if proposal.hocon_content is None else proposal.hocon_content

        last_result = recheck(patched, next_hocon)
        next_findings = list(last_result.findings)
        # 补丁无效（PatchError）时 Plan 未推进，plan_diff 记 None；
        # 有效但改了个空（值恰好相同）时文本 diff 为空串，也归 None（「没有文本变化」）。
        plan_diff = _plan_diff_text(plan, patched) if patched is not plan else ""
        rounds.append(
            AutofixRound(
                round=round_no,
                trigger_finding_ids=[f.id for f in findings],
                hocon_diff=hocon_unified_diff(hocon, next_hocon),
                plan_diff=plan_diff or None,
                resolved_finding_ids=_resolved_ids(findings, next_findings),
                created_at=now_factory(),
            )
        )
        plan, hocon, findings = patched, next_hocon, next_findings

        if not _has_block(findings):
            converged = True
            break
        # 约束 4：下一轮要修的阻塞项若此前出现过 → 修复在原地打转，停下
        signature = _signature(findings)
        if signature in seen:
            debounce = True
            break
        seen.add(signature)

    return _finish(
        plan=plan,
        hocon=hocon,
        findings=findings,
        rounds=rounds,
        used=len(rounds),
        converged=converged,
        exhausted=not converged and not debounce,
        debounce=debounce,
        policy=False,
        now_factory=now_factory,
        last_result=last_result,
    )


# ---------------------------------------------------------------- 内部


def _has_block(findings: list[GateFinding]) -> bool:
    return any(f.severity == "block" for f in findings)


def _has_policy_block(findings: list[GateFinding]) -> bool:
    """与 ``GateResult.has_policy_block`` 同一判据（此处只有 findings，故重写一遍）。"""
    return any(f.severity == "block" and f.category == "policy" for f in findings)


def _signature(findings: list[GateFinding]) -> Signature:
    """本轮**待修的阻塞项**签名。

    只取 ``block`` 项：去抖的语义是「该修的没前进」，而 ``warn`` 残留是允许的
    （收敛判据本身就只要求没有 ``block``），把 warn 算进去会让「warn 数量稳定」
    被误判成「修复原地打转」。
    """
    return frozenset((f.category, f.code, f.locator_key) for f in findings if f.severity == "block")


def _resolved_ids(before: list[GateFinding], after: list[GateFinding]) -> list[str]:
    """本轮消掉的 finding id：本轮输入里有、输出里没有的（按 id 认，不看文案）。"""
    after_ids = {f.id for f in after}
    return [f.id for f in before if f.id not in after_ids]


def _plan_diff_text(old_plan: EtlPlanModel, new_plan: EtlPlanModel) -> str:
    """Plan 前后的 unified diff 文本（给人看这一轮改了哪几行）。

    ``default=str`` 是必需的：python 模式下 ``budget`` 里的比率是 ``Decimal``，
    ``json.dumps`` 不认识它。
    """
    old_text = json.dumps(
        old_plan.model_dump(by_alias=True, exclude_none=False),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
        default=str,
    )
    new_text = json.dumps(
        new_plan.model_dump(by_alias=True, exclude_none=False),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
        default=str,
    )
    return unified_text_diff(old_text, new_text, fromfile="a/plan", tofile="b/plan")


def _finish(
    *,
    plan: EtlPlanModel,
    hocon: str,
    findings: list[GateFinding],
    rounds: list[AutofixRound],
    used: int,
    converged: bool,
    exhausted: bool,
    debounce: bool,
    policy: bool,
    now_factory: NowFactory,
    last_result: GateResult | None = None,
) -> AutofixOutcome:
    # 有复检结果就沿用它的 evaluated_at（那是门禁真正跑过的时刻）；
    # 策略阻断 / 无阻塞这两种「一次都没复检」的情况才需要自己造一个时刻。
    if last_result is not None:
        result = last_result.model_copy(
            update={
                "findings": list(findings),
                "autofix_rounds": list(rounds),
                "autofix_exhausted": exhausted or debounce,
            }
        )
    else:
        result = GateResult(
            evaluated_at=now_factory(),
            conclusion=compose_conclusion(list(findings)),
            findings=list(findings),
            autofix_rounds=list(rounds),
            autofix_exhausted=exhausted or debounce,
        )
    return AutofixOutcome(
        plan=plan,
        hocon_content=hocon,
        result=result,
        rounds_used=used,
        converged=converged,
        exhausted=exhausted,
        stopped_by_debounce=debounce,
        stopped_by_policy=policy,
    )


def run_autofix_loop(
    *,
    request: AutofixRequest,
    repair: RepairFn,
    recheck: RecheckFn,
    max_rounds: int,
    now_factory: NowFactory,
) -> AutofixOutcome:
    """跑有限自动修复（同步驱动）。全部约束见模块 docstring。

    修复回调是同步的：适合单测与离线批处理——一次不联网的修复不该被拖进事件循环。
    真实系统用 :func:`arun_autofix_loop`（模型是 ``async`` 的）。
    """
    driver = _drive(request=request, recheck=recheck, max_rounds=max_rounds, now_factory=now_factory)
    try:
        outgoing = next(driver)
        while True:
            outgoing = driver.send(repair(outgoing))
    except StopIteration as stop:
        # 生成器的返回值就是结果（``_drive`` 的每条 return 都是 ``_finish(...)``）
        return cast("AutofixOutcome", stop.value)


async def arun_autofix_loop(
    *,
    request: AutofixRequest,
    repair: AsyncRepairFn,
    recheck: RecheckFn,
    max_rounds: int,
    now_factory: NowFactory,
) -> AutofixOutcome:
    """跑有限自动修复（**异步驱动**，service 走这条）。

    与 :func:`run_autofix_loop` 共用 :func:`_drive`：收敛判据、去重、轮次消耗、
    策略短路全部只有一份实现。两个驱动的差别只有一行——``repair(outgoing)``
    还是 ``await repair(outgoing)``。

    刻意不在这里做超时/重试：那属于「调模型」这件事的策略，归
    ``clients/llm``（那边已经有 token 预算与重试）。回路只负责**轮次**，
    两种「次数」混在一起会让「为什么停了」变得无法归因。
    """
    driver = _drive(request=request, recheck=recheck, max_rounds=max_rounds, now_factory=now_factory)
    try:
        outgoing = next(driver)
        while True:
            outgoing = driver.send(await repair(outgoing))
    except StopIteration as stop:
        return cast("AutofixOutcome", stop.value)


__all__ = [
    "AsyncRepairFn",
    "AutofixOutcome",
    "AutofixRequest",
    "NowFactory",
    "PlanPatch",
    "RecheckFn",
    "RepairFn",
    "RepairProposal",
    "arun_autofix_loop",
    "run_autofix_loop",
]
