"""生成链路的工作流层（be-09）：LangGraph 状态机 + 三层状态隔离。

**本包不依赖 ``api/``，也不依赖 ``services/`` 的编排**（只允许
``services/pipeline/repository.py`` 与 ``gating`` / ``freeze`` 这三个被显式指定的入口）。
依赖方向是单向的 ``api → services → workflow → engines/clients/harness``，
反向依赖会让「service 调用图、图又调用 service」形成循环，
而循环导入在运行期的表现是「某个模块第二次被导入时拿到半成品对象」——最难查的一类。

对外只有六个名字（be-09 §2）：

- :func:`build_graph` —— 装配并编译图（需要 checkpointer）
- :class:`EtlWorkflowState` —— 状态契约
- :func:`start` / :func:`resume` —— 首次发起与澄清后恢复（都自带 Redis 生成锁）
- :func:`snapshot` —— 前端 ``WorkflowSnapshot`` 只读投影
- :func:`abort` —— 用户主动取消

其余（``transitions`` / ``interrupts`` / ``checkpointer`` / ``nodes``）按需直接
从子模块取，**不在这里再导出一遍**：``__init__`` 多导出一个名字，
就多一条「谁都在用的隐式公共接口」，而本模块的接口面积应该小到能一眼看完。
"""

from __future__ import annotations

from workflow.graph import (
    GEN_LOCK_TTL_SEC,
    GenerationInProgressError,
    TurnResult,
    abort,
    build_graph,
    resume,
    snapshot,
    start,
)
from workflow.state import EtlWorkflowState

__all__ = [
    "GEN_LOCK_TTL_SEC",
    "EtlWorkflowState",
    "GenerationInProgressError",
    "TurnResult",
    "abort",
    "build_graph",
    "resume",
    "snapshot",
    "start",
]
