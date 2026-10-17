"""五类确定性门禁检查（be-11 §6）。

每个模块只导出一个纯函数，签名统一为 ``(ctx: GateContext) -> list[GateFinding]``：
不取时间、不做 IO、不调 LLM、不抛业务异常。严格顺序由 ``engines.gate`` 的
``CHECK_ORDER`` 决定，本包不定义顺序（避免两处各排一遍）。
"""

from engines.gate.checks.mapping import check_mapping
from engines.gate.checks.policy import check_policy
from engines.gate.checks.resource import check_resource
from engines.gate.checks.schema import check_schema
from engines.gate.checks.syntax import check_syntax

__all__ = [
    "check_mapping",
    "check_policy",
    "check_resource",
    "check_schema",
    "check_syntax",
]
