"""图的装配、Redis 生成锁与调用门面（be-09 §5.1 / §5.6）。

## 图结构（be-09 §5.1）

```
START → intent_parse → param_check ─(缺参数)→ clarify ─┐
                          │                            │（Command(resume) 回到 param_check）
                          └─(参数齐全)→ metadata_bind → generate_plan
                                              ├─(结构非法且轮次用尽)→ terminal → END
                                              └─(其余)→ generate_hocon → gate
                                                                     ├─(pass/warn)→ freeze → END
                                                                     ├─(block 且可修)→ autofix
                                                                     └─(block 不可修)→ terminal → END
                                                       autofix ──→ generate_hocon（重渲染后再判）
```

修复回路是**唯一**的入口：门禁结论 → 修复 → 重新渲染 → 重新判定，
没有第二条「从结构校验直接进修复」的通路（理由见 :func:`route_after_plan`）。

条件边全部由**纯函数**判定（:func:`route_after_param_check` / :func:`route_after_gate` /
:func:`route_after_plan`），它们只看 state 里的值——因此「在什么情况下会去修复」
这件事可以在没有 PG、没有 LLM 的机器上被单测。

## 装配期的两道守卫

1. **节点出口白名单**：每个节点被包一层 :func:`_guarded_node`，
   回写的键必须落在 ``NODE_OUTPUTS`` 内。LangGraph 对未知键是宽容的，
   而「宽容」等于「执行态字段混进工作流状态不会被发现」（be-09 §6.1 第 2 条
   要求这条由显式 schema 收敛）。
2. **可序列化**：出口处再过一遍 :func:`workflow.state.assert_json_serializable`。
   这个检查放在**节点出口**而不是只在 ``snapshot()``，是因为检查点是在节点返回时写的——
   等 ``snapshot()`` 才发现，坏检查点已经落库了。

## 生成锁（be-09 §5.6 第 1 条）

``gen:lock:{pipeline_id}``：``SET NX EX 600``；释放用 Lua **比较并删除**，
只删自己那把锁（``GET`` 出来的 token 与自己的相等才 ``DEL``）。
不比较就删会遇到这个场景：A 的锁 TTL 到期被 Redis 清掉，B 拿到了同名锁，
此时 A 收尾时一句裸 ``DEL`` 就把 B 的锁删了——B 的互斥保护凭空消失，
而两个请求都返回成功，日志里什么都没有。所以「比较」不是锦上添花，
它和「加锁」同等重要。改不了 token 必须唯一（``new_uuid``），否则比较恒真。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Final, Optional

from langchain_core.runnables import RunnableConfig

from core.errors import ConflictError, ErrorCode, NotFoundError
from core.ids import new_uuid
from core.logging import get_logger
from core.redis import KEY_GEN_LOCK
from models.enums import PipelineStatus
from workflow.nodes import NODE_OUTPUTS, SHARED_OUTPUT_KEYS
from workflow.state import (
    STATE_KEYS,
    EtlWorkflowState,
    assert_json_serializable,
    thread_id_for,
)
from workflow.state import (
    snapshot as state_snapshot,
)
from workflow.transitions import assert_transition

__all__ = [
    "GEN_LOCK_TTL_SEC",
    "GenerationInProgressError",
    "TurnResult",
    "abort",
    "build_graph",
    "gen_lock_key",
    "generation_lock",
    "invoke_config",
    "resume",
    "snapshot",
    "start",
]

log = get_logger(__name__)

#: 生成锁 TTL（be-09 §10.2 暂定值）。兜底「容器崩溃来不及释放」的场景：
#: TTL 太长则崩溃后要等很久才能重试，太短则一次长生成会被并发穿透。
GEN_LOCK_TTL_SEC: Final[int] = 600

#: 比较并删除。返回 1 = 删掉了自己的锁，0 = 锁已不是自己的（被别人重取或已过期）。
#: **不要**改成 ``DEL`` 或 ``SETNX`` 的组合——见模块 docstring。
RELEASE_LOCK_SCRIPT: Final[str] = (
    "if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) else return 0 end"
)


class GenerationInProgressError(ConflictError):
    """同一 Pipeline 已有一次生成在图里跑。

    取不到锁**立刻失败、不等待、不排队**（be-09 §5.6 第 1 条）：
    排队会让「用户连点两下」变成两条串行的 LLM 生成，第二条基于第一条之前的
    旧 state 起步（它读到的检查点是拿锁之前的那一刻），产出一份谁都没看过的方案。
    """

    code = ErrorCode.CONFLICT
    http_status = 409
    message = "该 Pipeline 正在生成中，请稍后重试"


def gen_lock_key(pipeline_id: int) -> str:
    """``core.redis.KEY_GEN_LOCK`` 的填充点（键模板只有一份，见该模块的键空间规范）。"""
    return KEY_GEN_LOCK.format(pipeline_id=int(pipeline_id))


@asynccontextmanager
async def generation_lock(redis: Any, pipeline_id: int, *, ttl: int = GEN_LOCK_TTL_SEC) -> AsyncIterator[str]:
    """串行化同一 Pipeline 的生成。持有期间返回 token（供日志关联）。"""
    key = gen_lock_key(pipeline_id)
    token = new_uuid()
    acquired = await redis.set(key, token, nx=True, ex=ttl)
    if not acquired:
        log.warning("generation_lock_busy", pipeline_id=pipeline_id, lock_key=key)
        raise GenerationInProgressError(details={"pipeline_id": int(pipeline_id)})
    try:
        yield token
    finally:
        # 释放失败**不能**盖住 body 里的异常：`finally` 里抛错会把「生成失败」
        # 变成「释放锁失败」，而后者是个运维噪音。TTL 是这条路径的兜底。
        try:
            await redis.eval(RELEASE_LOCK_SCRIPT, 1, key, token)
        except Exception as exc:
            log.warning(
                "generation_lock_release_failed",
                pipeline_id=pipeline_id,
                lock_key=key,
                error_type=type(exc).__name__,
            )


# ---------------------------------------------------------------- 路由（纯函数）


def _max_autofix_rounds() -> int:
    """``settings.LLM_AUTOFIX_MAX_ROUNDS``（默认 3）。

    惰性取而不是模块级常量：``core.config`` 的导入会触发 .env 读取与生产自检，
    让 ``import workflow.graph`` 就去做这件事，会把纯函数单测变成一次配置校验。
    """
    from core.config import settings

    return int(getattr(settings, "LLM_AUTOFIX_MAX_ROUNDS", 3))


def route_after_param_check(state: EtlWorkflowState) -> str:
    """参数齐全 → 绑档案；否则 → 澄清。"""
    return "clarify" if state.get("missing_params") else "metadata_bind"


def route_after_plan(state: EtlWorkflowState) -> str:
    """结构非法且轮次用尽 → 终局；否则一律去渲染（be-09 §5.1 的 E 分支）。

    **与设计文档的一处偏离**：文档画的是 ``E -- 结构非法且可修 --> H``（直接进修复），
    这里改成先渲染、由门禁出结论再进修复。理由是 ``autofix`` 的输入是
    ``GateResult.findings``（be-11 的回路只吃 finding），而结构校验的输出是
    ``PlanIssue``——两者形状不同。直接进修复就得在节点里再造一套
    「PlanIssue → GateFinding」的映射，那份映射必然与门禁自己的产出漂移，
    于是「同一处问题」在两条路径上有两个 code，去抖与 ``resolved_finding_ids`` 全会失准。
    渲染不会因为结构问题而失败（``generate_hocon`` 接住渲染异常并落成
    ``hocon_errors``，见该节点），所以这条路径永远走得通。
    """
    if state.get("plan_errors") and int(state.get("autofix_rounds") or 0) >= _max_autofix_rounds():
        return "terminal"
    return "generate_hocon"


def route_after_gate(state: EtlWorkflowState) -> str:
    """门禁结论 → 冻结 / 修复 / 终局（be-09 §5.1 的 G 分支）。

    **策略阻塞不修**（``has_policy_block``）：P0/P1 是治理判定，
    模型改不掉，让它改只是把「策略拒绝」伪装成「模型没修好」，
    还白烧三轮 LLM 的钱。这一条与 be-11 §6.6 规则 2 是同一个约束的两处实现，
    放在**路由**而不是节点里，是为了让「它压根没进修复」这件事在图结构上可见。
    """
    gate = state.get("gate_result") or {}
    if str(gate.get("conclusion") or "") != "block":
        return "freeze"
    if bool(gate.get("findings")) and any(
        isinstance(f, dict) and f.get("category") == "policy" and f.get("severity") == "block"
        for f in gate["findings"]
    ):
        return "terminal"
    if int(state.get("autofix_rounds") or 0) >= _max_autofix_rounds():
        return "terminal"
    return "autofix"


# ---------------------------------------------------------------- 装配


def _guarded_node(name: str, fn: Any) -> Any:
    """给节点套出口守卫。见模块 docstring 的「装配期的两道守卫」。"""
    allowed = frozenset(NODE_OUTPUTS[name]) | SHARED_OUTPUT_KEYS
    undeclared = allowed - STATE_KEYS
    if undeclared:  # pragma: no cover - 装配期立刻暴露的配置错误
        raise ValueError(f"NODE_OUTPUTS[{name}] 声明了 EtlWorkflowState 里没有的键：{sorted(undeclared)}")

    # 注解逐字用 ``Optional[RunnableConfig]``（而不是 ``RunnableConfig | None``）：
    # LangGraph 的 ``KWARGS_CONFIG_KEYS`` 白名单里认的是**字符串** ``"Optional[RunnableConfig]"``，
    # 而本文件有 ``from __future__ import annotations``，注解到它手里就是那个字符串——
    # 写成 ``RunnableConfig | None`` 时类型系统等价、白名单却对不上，于是每次 ``add_node``
    # 都打一条 UserWarning（且 config 根本不会被注入）。这不是风格问题，
    # 所以这一行要顶着 UP045 保留旧写法（tests/test_workflow_graph.py 有用例守着）。
    async def wrapper(
        state: Any,
        config: Optional[RunnableConfig] = None,  # noqa: UP045 - 见上，必须是这个字符串形态
    ) -> dict[str, Any]:
        out = await fn(state, config)
        if out is None:
            return {}
        if not isinstance(out, dict):
            raise TypeError(f"节点 {name} 必须返回 dict，实得 {type(out).__name__}")
        unknown = sorted(set(out) - allowed)
        if unknown:
            raise ValueError(
                f"节点 {name} 回写了未声明的 state 键 {unknown}；"
                f"请改 workflow/nodes/__init__.py 的 NODE_OUTPUTS 或去掉这些键"
            )
        assert_json_serializable(out, where=f"节点 {name} 的输出")
        return out

    # 名字与签名都要保持：LangGraph 按参数名判断是否注入 config
    wrapper.__name__ = name
    wrapper.__doc__ = fn.__doc__
    return wrapper


async def terminal_node(state: EtlWorkflowState, config: Any = None) -> dict[str, Any]:
    """终局节点：把一个「没修好」的候选停在 ``gate_failed``。

    ``terminal_error`` 若已由 ``autofix`` 节点写好（``AUTOFIX_EXHAUSTED``）就保留，
    否则填 ``AUTOFIX_EXHAUSTED``——本节点只会被「无路可走」的路由选中，
    而「无路可走」在当前图里的唯一原因就是修复轮次用尽或策略阻断。
    """
    from workflow.nodes import runtime_of

    runtime = runtime_of(config)
    code = state.get("terminal_error") or ErrorCode.AUTOFIX_EXHAUSTED.value
    current = PipelineStatus(str(state.get("status") or PipelineStatus.DRAFT))
    new_status = assert_transition(
        current, "gate_blocked", pipeline_id=runtime.pipeline_id, request_id=runtime.request_id
    )
    log.info(
        "workflow_terminal",
        request_id=runtime.request_id,
        tenant_id=runtime.tenant_id,
        project_id=runtime.project_id,
        pipeline_id=runtime.pipeline_id,
        terminal_error=str(code),
        autofix_rounds=int(state.get("autofix_rounds") or 0),
    )
    return {"terminal_error": str(code), "status": str(new_status)}


def build_graph(checkpointer: Any) -> Any:
    """装配并编译图（be-09 §3.2 的签名）。

    ``checkpointer`` 由调用方传入（生产：``workflow.checkpointer.get_checkpointer().saver``；
    测试：``InMemorySaver``）。**不给默认值**——默认 ``None`` 会让 ``interrupt()``
    在运行期报一个「没有 checkpointer」的错，而那时图已经跑了一半；
    装配期就要拿到它，让「忘了配检查点」变成启动失败而不是用户可见的中断失败。
    """
    from langgraph.graph import END, START, StateGraph

    from workflow.nodes.autofix import autofix_node
    from workflow.nodes.clarify import clarify_node
    from workflow.nodes.freeze import freeze_node
    from workflow.nodes.gate import gate_node
    from workflow.nodes.generate_hocon import generate_hocon_node
    from workflow.nodes.generate_plan import generate_plan_node
    from workflow.nodes.intent_parse import intent_parse_node
    from workflow.nodes.metadata_bind import metadata_bind_node
    from workflow.nodes.param_check import param_check_node

    registry: dict[str, Any] = {
        "intent_parse": intent_parse_node,
        "param_check": param_check_node,
        "clarify": clarify_node,
        "metadata_bind": metadata_bind_node,
        "generate_plan": generate_plan_node,
        "generate_hocon": generate_hocon_node,
        "gate": gate_node,
        "autofix": autofix_node,
        "freeze": freeze_node,
        "terminal": terminal_node,
    }
    missing = sorted(set(registry) - set(NODE_OUTPUTS))
    if missing:  # pragma: no cover - 装配期立刻暴露
        raise ValueError(f"节点清单与 NODE_OUTPUTS 不一致，缺少声明：{missing}")

    builder = StateGraph(EtlWorkflowState)
    for name, fn in registry.items():
        builder.add_node(name, _guarded_node(name, fn))

    builder.add_edge(START, "intent_parse")
    builder.add_edge("intent_parse", "param_check")
    builder.add_conditional_edges(
        "param_check",
        route_after_param_check,
        {"clarify": "clarify", "metadata_bind": "metadata_bind"},
    )
    # 澄清之后回到 param_check 重新判定 —— 「还缺就再问、不缺就往前走」
    # 由同一个纯函数决定，避免「问过了就放行」这类隐形降级。
    builder.add_edge("clarify", "param_check")
    builder.add_edge("metadata_bind", "generate_plan")
    builder.add_conditional_edges(
        "generate_plan",
        route_after_plan,
        {"generate_hocon": "generate_hocon", "terminal": "terminal"},
    )
    builder.add_edge("generate_hocon", "gate")
    builder.add_conditional_edges(
        "gate",
        route_after_gate,
        {"freeze": "freeze", "autofix": "autofix", "terminal": "terminal"},
    )
    # 修复后**必须重新渲染再重跑门禁**：只改 Plan 不重渲染会让门禁检查的是旧 HOCON，
    # 于是「问题已修」与「门禁仍报错」同时出现，而人工排查会从渲染层开始找。
    builder.add_edge("autofix", "generate_hocon")
    builder.add_edge("freeze", END)
    builder.add_edge("terminal", END)

    return builder.compile(checkpointer=checkpointer)


# ---------------------------------------------------------------- 调用门面


@dataclass(frozen=True, slots=True)
class TurnResult:
    """一次 ``start`` / ``resume`` 的结果（service 层装配响应时就取这四个）。"""

    #: 终态 state（含所有草稿与结论）
    values: dict[str, Any]
    #: 前端 ``WorkflowSnapshot``（``workflow.state.snapshot`` 的产物）
    snapshot: dict[str, Any]
    #: 后端契约的简版中断载荷 ``{"reason", "questions"}``；无中断时 ``None``
    interrupt: dict[str, Any] | None
    #: ``PipelineStatus`` 的 value
    status: str


def invoke_config(
    thread_id: str,
    *,
    session: Any = None,
    ctx: Any = None,
    pipeline_id: int | None = None,
    project_id: int | None = None,
) -> dict[str, Any]:
    """组装 ``ainvoke`` 的 config。

    ``runtime`` 挂在 ``configurable`` 上而不是放 state：``AsyncSession`` 不可序列化，
    放 state 会让检查点写不进去；而 ``configurable`` 是运行期事实，
    LangGraph 只把 ``thread_id`` / ``checkpoint_ns`` 一类的键写进检查点。

    ``project_id`` 的取值顺序是「显式参数 → ``ctx`` 上恰好有的 → 0」：
    ``SubjectContext`` 没有这个字段，而租户/项目过滤少一个值会让节点**查不到任何东西**
    却不报错，所以它必须是显式传的（``start`` 从初始 state 里取，``resume`` 由
    orchestrator 从 pipeline 上取）。
    """
    configurable: dict[str, Any] = {"thread_id": thread_id}
    if session is not None:
        from workflow.nodes import NodeRuntime

        resolved_project = project_id if project_id is not None else getattr(ctx, "project_id", 0)
        configurable["runtime"] = NodeRuntime(
            session=session,
            ctx=ctx,
            pipeline_id=int(pipeline_id if pipeline_id is not None else thread_id),
            project_id=int(resolved_project or 0),
        )
    return {"configurable": configurable}


def _first_interrupt(result: dict[str, Any]) -> dict[str, Any] | None:
    """从 ``ainvoke`` 的结果里取出中断载荷（be-09 §5.3）。"""
    from workflow.interrupts import parse_interrupt

    raw = result.get("__interrupt__")
    if not raw:
        return None
    first = raw[0] if isinstance(raw, (list, tuple)) else raw
    value = getattr(first, "value", first)
    return parse_interrupt(value)


async def _run(
    graph: Any,
    payload: Any,
    config: dict[str, Any],
    *,
    redis: Any,
    pipeline_id: int,
) -> TurnResult:
    """加锁 → ``ainvoke`` → 装配结果。锁在这一层，节点里看不到它。"""
    if redis is None:
        from core.redis import get_redis

        redis = get_redis()
    async with generation_lock(redis, pipeline_id):
        result = await graph.ainvoke(payload, config=config)
    values = dict(result or {})
    return TurnResult(
        values=values,
        snapshot=state_snapshot(values),
        interrupt=_first_interrupt(values),
        status=str(values.get("status") or ""),
    )


async def start(
    graph: Any,
    state: EtlWorkflowState,
    *,
    session: Any = None,
    ctx: Any = None,
    thread_id: str | None = None,
    redis: Any = None,
) -> TurnResult:
    """首次发起：``thread_id = str(pipeline_id)``，跑到中断或终止。"""
    pipeline_id = int(state.get("pipeline_id") or 0)
    if not pipeline_id:
        raise ValueError("初始 state 缺少 pipeline_id：thread_id 由它派生（be-09 §5.3）")
    config = invoke_config(
        thread_id or thread_id_for(pipeline_id),
        session=session,
        ctx=ctx,
        pipeline_id=pipeline_id,
        # 初始 state 里就有 project_id（orchestrator 注入），它比「猜 ctx 上有这个字段」可靠：
        # SubjectContext 里根本没有 project_id（见 nodes.RequestContext 的说明）。
        project_id=int(state.get("project_id") or 0),
    )
    return await _run(graph, dict(state), config, redis=redis, pipeline_id=pipeline_id)


async def resume(
    graph: Any,
    thread_id: str,
    resume_input: Any,
    *,
    session: Any = None,
    ctx: Any = None,
    pipeline_id: int | None = None,
    project_id: int | None = None,
    redis: Any = None,
) -> TurnResult:
    """澄清后恢复：``Command(resume=<用户输入>)``。

    恢复输入的形状归一在 ``clarify`` 节点里做（``parse_resume_input``），
    这里**原样透传**：多一处归一就多一处可能与节点内不一致的翻译。

    ``project_id`` 未显式给出时从**检查点里读**：它不是令牌声明（``SubjectContext``
    上没有），但 checkpoint 的 state 里有——而恢复这一路没有初始 state 可查。
    读它不是为了省一次调用，而是因为节点会把 ``project_id`` 写进证据账本：
    写错项目等于把一次生成记到别的项目名下。
    """
    from langgraph.types import Command

    resolved_id = int(pipeline_id if pipeline_id is not None else thread_id)
    if project_id is None:
        values = await _values_of(graph, thread_id)
        project_id = int(values.get("project_id") or 0)
    config = invoke_config(
        thread_id, session=session, ctx=ctx, pipeline_id=resolved_id, project_id=project_id
    )
    return await _run(graph, Command(resume=resume_input), config, redis=redis, pipeline_id=resolved_id)


async def _values_of(graph: Any, thread_id: str) -> dict[str, Any]:
    """读检查点里的 state；线程不存在时抛 ``NOT_FOUND``。"""
    state = await graph.aget_state({"configurable": {"thread_id": thread_id}})
    values = dict(getattr(state, "values", None) or {})
    if not values:
        # 空 values 与「图跑过但什么都没写」在现实中不可能同时成立
        # （intent_parse 一定会写 intent），所以它就是「这个 thread 不存在」。
        raise NotFoundError(
            "生成会话不存在或已被清理，请重新发起生成",
            code=ErrorCode.NOT_FOUND,
            details={"thread_id": thread_id},
        )
    return values


async def snapshot(graph: Any, thread_id: str) -> dict[str, Any]:
    """只读快照（前端 ``WorkflowSnapshot``）。断线恢复用的就是它。"""
    return state_snapshot(await _values_of(graph, thread_id))


async def abort(graph: Any, thread_id: str, *, reason: str = "") -> TurnResult:
    """用户主动取消（前端 ``/generations/{thread_id}/abort``）。

    **不是硬中断**，不产生证据事件（be-09/fe-04：硬中断属于运行监督，见 be-16）。
    实现方式是往检查点写一个 ``aborted`` 标记：图本身可能停在中断点上不动，
    而快照随即回到 ``idle``（前端 mock 也是这个形状，phase_message =「已取消生成」）。
    只打标记、**不删检查点**：用户取消之后又想看历史时，草稿还在。
    """
    values = await _values_of(graph, thread_id)
    pipeline_id = int(values.get("pipeline_id") or thread_id)
    # 只给 thread_id：`aupdate_state` 不跑节点，因此不需要 runtime；
    # 顺手清掉可能残留的中断标记，让「取消」这个动作是收敛的。
    config: dict[str, Any] = {"configurable": {"thread_id": thread_id}}
    await graph.aupdate_state(config, {"aborted": True, "terminal_error": None})
    log.info(
        "workflow_aborted",
        thread_id=thread_id,
        pipeline_id=pipeline_id,
        # 取消原因只进日志：它可能是用户随手填的一句抱怨，不适合进快照与检查点
        reason_chars=len(reason),
    )
    fresh = await _values_of(graph, thread_id)
    return TurnResult(
        values=fresh,
        snapshot=state_snapshot(fresh),
        interrupt=None,
        status=str(fresh.get("status") or ""),
    )
