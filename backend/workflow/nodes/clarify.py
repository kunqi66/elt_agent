"""澄清中断：缺参数时 ``interrupt()`` 提问（be-09 §5.1 / §5.3）。

本节点**故意极薄**，因为 ``interrupt()`` 的语义很苛刻：

1. ``interrupt(payload)`` 抛的是内部控制流异常，节点执行到此中断，
   **它之前的所有写入都会在恢复时被丢弃**（恢复 = 从这个节点的开头整体重跑）。
   所以「先把问卷写进 state 再中断」是不成立的——问卷由前一个节点
   （``param_check``）产出并已落检查点。
2. 恢复时 ``interrupt(...)`` **返回**用户的输入而不是再抛，于是同一个函数体
   在两次调用里走不同的分支。因此本节点只做两件事：中断取答案、把答案合并进
   ``collected_params``，然后交给 ``param_check`` 重新判定是否还缺。

**幂等性**由「不写任何东西」保证：这个节点没有副作用可重放。
"""

from __future__ import annotations

from typing import Any

from core.logging import get_logger
from workflow.interrupts import (
    interrupt_payload,
    merge_resume_params,
    parse_resume_input,
)
from workflow.nodes import runtime_of

__all__ = ["clarify_node"]

log = get_logger(__name__)


async def clarify_node(state: dict[str, Any], config: Any = None) -> dict[str, Any]:
    """图节点：问 → 等 → 收答案。

    ``interrupt`` 从 ``langgraph.types`` 惰性导入（与 ``nodes/__init__.py`` 的
    ``llm_provider`` 同理）：langgraph 是运行时依赖而不是 import 期依赖，
    纯函数用例不该因为它没装就整包不可用。
    """
    from langgraph.types import interrupt

    runtime = runtime_of(config)
    questions = list(state.get("pending_questions") or [])
    if not questions:
        # 走到这里说明装配错了（``param_check`` 没算出缺失项却被路由到这里）。
        # 不静默返回空 dict —— 静默会让图继续往前跑，把一个「没问出来的问题」
        # 变成「模型猜的参数」，而那正是这条链路最不该出现的降级。
        raise RuntimeError("clarify 被调用但 pending_questions 为空，检查 param_check 的路由")

    log.info(
        "clarify_interrupt",
        request_id=runtime.request_id,
        tenant_id=runtime.tenant_id,
        project_id=runtime.project_id,
        pipeline_id=runtime.pipeline_id,
        question_count=len(questions),
    )

    # 中断点：恢复时这里返回用户输入，函数从头再跑一遍（上面的日志会再打一次，
    # 这是有意的——它同时是「恢复确实发生了」的观测点）。
    answer = interrupt(interrupt_payload(questions))

    params, _content = parse_resume_input(answer)
    merged = merge_resume_params(state.get("collected_params"), params)
    log.info(
        "clarify_resumed",
        request_id=runtime.request_id,
        pipeline_id=runtime.pipeline_id,
        answered=len(params),
        total_params=len(merged),
    )
    return {"collected_params": merged}
