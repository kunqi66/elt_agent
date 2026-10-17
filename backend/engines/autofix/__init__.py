"""有限自动修复（be-11 §6.5 / §6.6）。

``patch.py`` 定义受限补丁（唯一变更形态）与 ``apply_plan_patch``；
``loop.py`` 定义回路控制流（LLM / 门禁 / 时间全部以回调解入，保持纯函数）。

本包**不** import ``clients``——真调模型是 service 的事（be-11 §1「不做什么」）。
"""

from engines.autofix.loop import (
    AsyncRepairFn,
    AutofixOutcome,
    AutofixRequest,
    NowFactory,
    RecheckFn,
    RepairFn,
    arun_autofix_loop,
    run_autofix_loop,
)
from engines.autofix.patch import (
    PatchError,
    PatchOp,
    PatchOperation,
    PlanPatch,
    RepairProposal,
    apply_plan_patch,
)

__all__ = [
    "AsyncRepairFn",
    "AutofixOutcome",
    "AutofixRequest",
    "NowFactory",
    "PatchError",
    "PatchOp",
    "PatchOperation",
    "PlanPatch",
    "RecheckFn",
    "RepairFn",
    "RepairProposal",
    "apply_plan_patch",
    "arun_autofix_loop",
    "run_autofix_loop",
]
