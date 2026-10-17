"""确定性门禁的组装与短路（be-11 §5.1/§5.2）。

    evaluate(ctx, now=...) -> GateResult

**这是「让不让上生产」的唯一判定入口，而且它是纯同步函数。**
PDP 判定（要读策略规则表、要 async）由 service 先算好放进 ``ctx``，
所以整个门禁可复现：同一份制品 + 同一份策略 + 同一份档案 → 同一份结论，
不依赖网络、数据库、时钟（``now`` 由调用方注入）。

## 短路规则：遇到第一个阻塞就停

be-11 §5.2 要求 ``policy`` 第一个跑且阻塞时短路，本实现把它推广成一条统一规则：
**任何一类检查产生 block，后面的检查不再跑**。理由：

- 后一类检查的**前提**已被推翻。HOCON 解析失败时还去核对「列存不存在」，
  只会对着一个空配置报出几十条连带错误，把真正的原因埋在噪声里。
- 门禁的读者是人。一次报 3 条相关的、而不是 40 条连带的，才修得动。

代价是「一次只暴露一类问题」，多轮修复。这个代价是有意付的——
自动修复回路本来就是多轮的（≤3 轮），而「一次报一大堆」会让人直接放弃读。

⚠️ ``info`` 与 ``warn`` **不会**触发短路：它们是给人看的提示，不构成「前提被推翻」。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from engines.gate.checks.mapping import check_mapping
from engines.gate.checks.policy import check_policy
from engines.gate.checks.resource import check_resource
from engines.gate.checks.schema import check_schema
from engines.gate.checks.syntax import check_syntax
from engines.gate.context import GateContext
from engines.gate.types import (
    CHECK_ORDER,
    AutofixRound,
    GateCheckId,
    GateFinding,
    GateResult,
    compose_conclusion,
)

#: 检查 id → 实现。**顺序由 ``CHECK_ORDER`` 决定**，不靠本 dict 的插入序——
#: 两个「顺序」同时存在时，改一个忘一个就会让门禁按另一套顺序跑，且测试可能照样绿。
_CHECKS: dict[GateCheckId, Callable[[GateContext], list[GateFinding]]] = {
    "policy": check_policy,
    "syntax": check_syntax,
    "schema": check_schema,
    "mapping": check_mapping,
    "resource": check_resource,
}


def evaluate(
    ctx: GateContext,
    *,
    now: datetime,
    autofix_rounds: list[AutofixRound] | tuple[AutofixRound, ...] = (),
    autofix_exhausted: bool = False,
) -> GateResult:
    """跑完五类检查并合成结论。

    ``autofix_rounds`` / ``autofix_exhausted`` 由调用方传入而不是在这里算：
    门禁只回答「这份制品现在合不合格」，**它不负责修复，也不负责记住修过几轮**。
    把轮次塞进门禁会让「同输入的结论一致」这条不变量失效——轮次是过程状态，
    不是制品的属性。
    """
    findings: list[GateFinding] = []

    for check_id in CHECK_ORDER:
        produced = _CHECKS[check_id](ctx)
        findings.extend(produced)
        if any(f.severity == "block" for f in produced):
            # 前提已被推翻，继续跑只会产出噪声。见模块 docstring 的短路规则。
            break

    return GateResult(
        evaluated_at=now,
        conclusion=compose_conclusion(findings),
        findings=findings,
        autofix_rounds=list(autofix_rounds),
        autofix_exhausted=autofix_exhausted,
    )


def evaluate_all(ctx: GateContext, *, now: datetime) -> GateResult:
    """**不短路**地跑完五类检查。只给诊断用（例如「把问题一次性列全」的后台任务）。

    刻意做成另一个函数而不是给 ``evaluate`` 加开关：门禁的默认路径必须是短路那条，
    否则「生产放行」与「诊断」会共用一条代码路径，某天为了诊断方便把开关打开，
    生产上就会开始一次报 40 条。
    """
    findings: list[GateFinding] = []
    for check_id in CHECK_ORDER:
        findings.extend(_CHECKS[check_id](ctx))
    return GateResult(evaluated_at=now, conclusion=compose_conclusion(findings), findings=findings)
