"""确定性门禁（be-11）的**检查层**：五类纯规则检查 + 一个按执行序排列的 registry。

本包只提供**检查函数**；门禁的组装、短路与结论合成（``evaluate``）由上层 ``gate.py``
负责。这样切分是为了让「每条检查都是纯函数、可以被逐条单测」这件事在结构上成立。

导出两样东西：

- 五个 ``check_*`` 函数（统一签名 ``(GateContext) -> list[GateFinding]``）；
- :data:`CHECKS`：``check_id -> 函数`` 的映射。上层按 ``CHECK_ORDER``
  （``policy → syntax → schema → mapping → resource``，be-11 §5.1）取用，
  **不要**对 key 排序——字典序不是执行序，排一下就把短路语义改了。
"""

from collections.abc import Callable

from engines.gate.checks import (
    check_mapping,
    check_policy,
    check_resource,
    check_schema,
    check_syntax,
)
from engines.gate.context import GateContext, ProfileColumn, TableProfile, context_facts
from engines.gate.types import (
    CHECK_ORDER,
    GateCategory,
    GateCheckId,
    GateConclusion,
    GateFinding,
    GateResult,
    GateSeverity,
    check_id_to_category,
    compose_conclusion,
    make_finding,
)

#: 一条检查的统一签名。
CheckFn = Callable[[GateContext], list[GateFinding]]

#: ``check_id`` → 检查函数。**取用顺序一律来自 ``CHECK_ORDER``**，不要依赖本 dict 的键序。
CHECKS: dict[GateCheckId, CheckFn] = {
    "policy": check_policy,
    "syntax": check_syntax,
    "schema": check_schema,
    "mapping": check_mapping,
    "resource": check_resource,
}

__all__ = [
    "CHECKS",
    "CHECK_ORDER",
    "CheckFn",
    "GateCategory",
    "GateCheckId",
    "GateConclusion",
    "GateContext",
    "GateFinding",
    "GateResult",
    "GateSeverity",
    "ProfileColumn",
    "TableProfile",
    "check_id_to_category",
    "check_mapping",
    "check_policy",
    "check_resource",
    "check_schema",
    "check_syntax",
    "compose_conclusion",
    "context_facts",
    "make_finding",
]
