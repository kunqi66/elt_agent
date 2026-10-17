"""节点包的公共运行时（be-09 §2 / §5.2）。

**这里不放任何节点实现**——节点各自一个文件（§2 的清单），
本文件只提供三样东西，供所有节点共用：

1. :class:`NodeRuntime`：一次图运行需要的外部依赖（``session`` / ``ctx`` / ``pipeline_id``）。
   它**不进 state**（``AsyncSession`` 不可序列化），而是挂在
   ``config["configurable"]["runtime"]`` 上随 ``ainvoke`` 传入——
   LangGraph 只把 ``thread_id`` / ``checkpoint_ns`` 一类的键写进检查点，
   ``configurable`` 里挂的对象是运行期事实，不会被序列化。
2. :func:`runtime_of`：从节点收到的 ``config`` 里取出它。
3. :data:`NODE_OUTPUTS`：每个节点**允许回写**的 state 键。``graph.build_graph``
   据此给每个节点套一层出口守卫——LangGraph 对未知键是宽容的，
   而宽容意味着「执行态字段混进工作流状态」这类错误不会被任何业务用例发现。

``ctx`` 是**鸭子类型**（:class:`RequestContext`）而不是具体类：它由鉴权侧定义
（``schemas/auth.py`` 的 ``SubjectContext``），本模块只声明它必须提供的属性。
写成 Protocol 而不是 ``Any``，是为了让「谁读错了字段名」在类型检查期就能被看见——
而这里的字段名**不是猜的**：逐个对着 ``SubjectContext`` 抄（曾经写成
``core/deps.py`` 里一个并不存在的 ``RequestContext``，于是 ``ctx.user_id`` 读成了
``ctx.subject_id``，症状是证据账本里的 ``actor_id`` 恒为默认值）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final, Protocol, runtime_checkable

from core.errors import AppError, ErrorCode

__all__ = [
    "NODE_OUTPUTS",
    "SHARED_OUTPUT_KEYS",
    "NodeRuntime",
    "RequestContext",
    "RuntimeMissingError",
    "llm_provider",
    "runtime_of",
]


def llm_provider() -> Any:
    """取 LLM Provider（be-08 的 ``get_provider()``）。

    **惰性**在函数体里 import：``clients/llm`` 的包入口由 be-08 提供，
    模块级 import 会让「workflow 这个包能不能被 import」取决于另一个并行模块的进度，
    而本机（无 PG、无 LLM 凭据）还要用它跑纯函数单测。

    ``ImportError`` 与 ``AttributeError`` **原样抛出**，不翻译成领域错误：
    缺模块是装配/部署问题，翻译成 ``LLM_UNAVAILABLE`` 会让「依赖没装」看起来像
    「厂商挂了」，排查方向直接被带到网络与凭据上。
    """
    from clients.llm import get_provider

    return get_provider()


@runtime_checkable
class RequestContext(Protocol):
    """请求上下文的**最小形状**（be-09 §3.2；实现是 be-01 的 ``schemas.auth.SubjectContext``）。

    属性名**逐字取自真实现**，一个字都不能改：
    ``tenant_id`` 用于租户过滤、``user_id`` 用于证据账本的 ``actor_id``、
    ``request_id`` 用于日志与对话去重。

    ``project_id`` **不在这个协议里**：``SubjectContext`` 里根本没有它
    （项目是 pipeline 的属性而不是令牌声明），所以它由调用方显式传给
    :class:`NodeRuntime`（见 ``graph.invoke_config``）。早先按 ``ctx.project_id``
    读它会得到一个 ``AttributeError``——而它读的位置在日志绑定与账本写入里，
    症状是「生成到一半 500」，真因是「上下文的字段名单是猜的」。
    """

    tenant_id: int
    user_id: int
    request_id: str


class RuntimeMissingError(AppError):
    """节点被调用时没带上运行期依赖。

    500 而不是 4xx：这是**装配错误**（orchestrator 忘了往 config 里放 runtime），
    不是用户请求的问题。用 ``INTERNAL_ERROR`` 并明确写出「装配错误」，
    免得排查方向被引到用户输入上。
    """

    code = ErrorCode.INTERNAL_ERROR
    http_status = 500
    message = "工作流节点缺少运行期依赖"


@dataclass(slots=True)
class NodeRuntime:
    """一次图运行的外部依赖。

    ``session`` 与 ``ctx`` 都是**借来的**：事务边界归 orchestrator（be-09 §5.6 第 4 条
    「图执行在事务之外，避免长事务持锁」），节点只用不提交、不关闭。

    ``project_id`` 是**独立字段**而不是 ``ctx`` 的代理：项目 id 不在 ``SubjectContext``
    上（它是 pipeline 的属性），由调用方从 state/编排层显式交进来。
    """

    session: Any
    ctx: Any
    pipeline_id: int
    #: 租户过滤与项目过滤都要它；取不到就是 0，节点拿 0 去过滤会「什么都查不到」，
    #: 所以 0 只可能是装配漏填——而不是一个合法的项目。
    project_id: int = 0

    @property
    def tenant_id(self) -> int:
        return int(self.ctx.tenant_id)

    #: 操作者 id。字段名与 ``SubjectContext.user_id`` 一致（证据账本的 ``actor_id``）。
    @property
    def user_id(self) -> int:
        return int(self.ctx.user_id)

    @property
    def request_id(self) -> str:
        return str(self.ctx.request_id)


def runtime_of(config: Any) -> NodeRuntime:
    """从节点的第二个参数 ``config`` 里取运行期依赖。"""
    configurable = (config or {}).get("configurable") or {}
    runtime = configurable.get("runtime")
    if not isinstance(runtime, NodeRuntime):
        raise RuntimeMissingError(
            "节点调用缺少 runtime：请把 NodeRuntime 挂在 config['configurable']['runtime'] 上"
        )
    return runtime


#: ``节点名 -> 允许回写的 state 键``。既用于出口守卫，也是一份「谁产出什么」的清单：
#: 想让某个节点多写一个键，改这里与被写的那个事实同时发生，评审时看得见。
#:
#: ``node_fingerprints`` 每个节点都可以写（幂等标记），故不逐个列出，
#: 由守卫统一放行。
NODE_OUTPUTS: Final[dict[str, frozenset[str]]] = {
    "intent_parse": frozenset({"intent", "collected_params", "missing_params"}),
    "param_check": frozenset({"missing_params", "pending_questions"}),
    "clarify": frozenset({"collected_params"}),
    "metadata_bind": frozenset({"profile_ids", "profile_sha256"}),
    "generate_plan": frozenset({"draft_plan", "plan_errors", "generation_context"}),
    "generate_hocon": frozenset({"draft_hocon", "hocon_errors", "hocon_meta"}),
    # ``hocon_meta``：门禁结论要挂到 HOCON 制品上（``hocon_artifact.validation_result``
    # 是它唯一的落点），冻结时的「门禁不得有阻塞项」就是从那里读回来的。
    "gate": frozenset({"gate_result", "status", "hocon_meta"}),
    # HOCON 不在本节点的产出里：修复后的文本交给紧随其后的 ``generate_hocon``
    # 重新渲染，两处都写就会分叉（见 ``autofix.py`` 的模块 docstring）。
    "autofix": frozenset(
        {
            "draft_plan",
            "plan_errors",
            "gate_result",
            "autofix_rounds",
            "autofix_history",
            "terminal_error",
        }
    ),
    "freeze": frozenset({"frozen_version_id", "status"}),
    "terminal": frozenset({"terminal_error", "status"}),
}

#: 守卫统一放行的键（幂等标记与终止标记，任何节点都可能写）。
SHARED_OUTPUT_KEYS: Final[frozenset[str]] = frozenset({"node_fingerprints", "aborted"})
