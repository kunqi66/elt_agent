"""图的装配、路由、中断往返、幂等与生成锁的用例（be-09 §5.1 / §5.3 / §5.6）。

图结构、条件边、出口守卫、锁释放脚本都是**纯逻辑**，因此不需要 PG、不需要 LLM 凭据。
真需要持久化的地方用 ``InMemorySaver``——它是 LangGraph 官方的检查点实现，
与被测的 ``AsyncPostgresSaver`` 共用同一套 ``BaseCheckpointSaver`` 协议，
所以「中断 → 恢复」的语义在这里是真的（而不是用进程内字典假装）。

**provider 一律不碰**：``intent_parse`` / ``generate_plan`` 会调 LLM，
故这两个节点不出现在本文件的任何用例里（需要它们时用 monkeypatch 顶掉，
但那属于集成测试的活，见 be-08 的用例）。
"""

from __future__ import annotations

from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from clients.llm.fake import FakeProvider
from core.errors import AppError, ConflictError, ErrorCode
from core.redis import KEY_GEN_LOCK
from workflow.graph import (
    GEN_LOCK_TTL_SEC,
    RELEASE_LOCK_SCRIPT,
    GenerationInProgressError,
    _guarded_node,
    build_graph,
    gen_lock_key,
    generation_lock,
    invoke_config,
    route_after_gate,
    route_after_param_check,
    route_after_plan,
)
from workflow.interrupts import parse_interrupt
from workflow.nodes import NODE_OUTPUTS, SHARED_OUTPUT_KEYS, NodeRuntime
from workflow.nodes.clarify import clarify_node
from workflow.nodes.metadata_bind import metadata_bind_node
from workflow.nodes.param_check import (
    REQUIRED_PARAM_ORDER,
    param_check_node,
)
from workflow.state import STATE_KEYS, EtlWorkflowState, StateNotSerializableError, thread_id_for

_EXPECTED_NODES = {
    "intent_parse",
    "param_check",
    "clarify",
    "metadata_bind",
    "generate_plan",
    "generate_hocon",
    "gate",
    "autofix",
    "freeze",
    "terminal",
}

# 设计文档 §5.1 的图结构，逐条写成 (source, target)。
_EXPECTED_EDGES = {
    ("__start__", "intent_parse"),
    ("intent_parse", "param_check"),
    # 澄清回路：还缺就再问、不缺就往前走，由同一个纯函数决定
    ("param_check", "clarify"),
    ("param_check", "metadata_bind"),
    ("clarify", "param_check"),
    ("metadata_bind", "generate_plan"),
    # E 分支：结构非法且轮次用尽 → 终局；其余 → 渲染
    ("generate_plan", "generate_hocon"),
    ("generate_plan", "terminal"),
    ("generate_hocon", "gate"),
    # G 分支：通过 → 冻结；可修 → 修复；策略阻断/轮次用尽 → 终局
    ("gate", "freeze"),
    ("gate", "autofix"),
    ("gate", "terminal"),
    # 修复后**必须重渲染**再重跑门禁
    ("autofix", "generate_hocon"),
    ("freeze", "__end__"),
    ("terminal", "__end__"),
}


class _Ctx:
    """``RequestContext`` 的最小实现（鸭子类型；真实现是 ``schemas.auth.SubjectContext``）。

    字段名逐字对齐真实现：``user_id``（**不是** ``subject_id``——真类里没有这个名字，
    写错的话证据账本里的 ``actor_id`` 会恒为默认值而不是报错）。
    ``project_id`` 真类里也没有，但 ``invoke_config`` 会把它放进 ``NodeRuntime``，
    所以这里带着它，用来验证「显式参数优先于 ctx 上的同名属性」。
    """

    def __init__(self, *, tenant_id: int = 1, project_id: int = 2, user_id: int = 3) -> None:
        self.tenant_id = tenant_id
        self.project_id = project_id
        self.user_id = user_id
        self.request_id = "req-test"


def _compiled():
    return build_graph(InMemorySaver())


def _graph_view(graph):
    view = graph.get_graph()
    nodes = {n for n in view.nodes if n not in ("__start__", "__end__")}
    edges = {(e.source, e.target) for e in view.edges}
    return nodes, edges


# ---------------------------------------------------------------- 装配


def test_compiled_graph_has_the_documented_nodes_and_edges():
    graph = _compiled()
    nodes, edges = _graph_view(graph)
    assert nodes == _EXPECTED_NODES
    assert edges == _EXPECTED_EDGES


def test_build_graph_requires_a_checkpointer():
    """不给默认值：默认 ``None`` 会让 ``interrupt()`` 在图跑了一半时才发现没有检查点。"""
    with pytest.raises(TypeError):
        build_graph()  # type: ignore[call-arg]


def test_graph_assembly_warns_nothing_about_the_config_annotation():
    """``config`` 参数的注解必须是**字符串** ``Optional[RunnableConfig]``。

    这是本仓库最容易「顺手改坏」的一处：``from __future__ import annotations`` 下
    注解到 langgraph 手里就是源码里那串字面量，而它的白名单只认
    ``"Optional[RunnableConfig]"`` 与 ``"RunnableConfig"``——写成
    ``RunnableConfig | None``（类型系统完全等价、ruff 还催你这么改）就会
    在每次 ``add_node`` 打一条 UserWarning，**并且 config 不会被注入**，
    于是所有节点都拿不到 runtime（症状是运行期 ``RuntimeMissingError``，不是启动失败）。
    """
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        _compiled()


def test_node_outputs_are_declared_state_keys():
    """出口白名单只能引用 state 里真实存在的键，否则守卫会拦掉节点自己的合法产出。"""
    for name, keys in NODE_OUTPUTS.items():
        assert keys <= STATE_KEYS, f"节点 {name} 声明了未声明的 state 键：{sorted(keys - STATE_KEYS)}"
    assert SHARED_OUTPUT_KEYS <= STATE_KEYS
    assert set(NODE_OUTPUTS) == _EXPECTED_NODES


def test_every_graph_node_has_a_declared_output_set():
    """节点清单与 ``NODE_OUTPUTS`` 必须一一对应（少一个 = 装配期就该炸）。"""
    assert set(NODE_OUTPUTS) == _EXPECTED_NODES


# ---------------------------------------------------------------- 出口守卫


@pytest.mark.asyncio
async def test_guarded_node_rejects_undeclared_state_key():
    async def rogue(state, config=None):
        return {"draft_plan": {"name": "x"}, "execution_run_id": 1}

    guarded = _guarded_node("generate_plan", rogue)
    with pytest.raises(ValueError) as excinfo:
        await guarded({"pipeline_id": 1}, None)
    assert "未声明的 state 键" in str(excinfo.value)
    assert "execution_run_id" in str(excinfo.value)


@pytest.mark.asyncio
async def test_guarded_node_rejects_non_dict_and_non_serializable_output():
    async def returns_none(state, config=None):
        return None

    assert await _guarded_node("param_check", returns_none)({}, None) == {}

    async def returns_list(state, config=None):
        return [1, 2]

    with pytest.raises(TypeError):
        await _guarded_node("param_check", returns_list)({}, None)

    async def returns_datetime(state, config=None):
        from datetime import UTC, datetime

        return {"intent": {"created_at": datetime(2026, 10, 16, tzinfo=UTC)}}

    with pytest.raises(StateNotSerializableError):
        await _guarded_node("intent_parse", returns_datetime)({}, None)


@pytest.mark.asyncio
async def test_guarded_node_allows_shared_keys():
    async def fine(state, config=None):
        return {"node_fingerprints": {"x": "y"}, "aborted": False}

    assert await _guarded_node("param_check", fine)({}, None) == {
        "node_fingerprints": {"x": "y"},
        "aborted": False,
    }


# ---------------------------------------------------------------- 路由（纯函数）


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ({"missing_params": []}, "metadata_bind"),
        ({"missing_params": ["primary_key"]}, "clarify"),
        ({}, "metadata_bind"),
    ],
)
def test_route_after_param_check(state, expected):
    assert route_after_param_check(state) == expected


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ({}, "generate_hocon"),
        ({"plan_errors": [{"code": "x"}]}, "generate_hocon"),
        # 结构非法且轮次用尽 → 终局（与设计文档的 E 分支一致）
        ({"plan_errors": [{"code": "x"}], "autofix_rounds": 3}, "terminal"),
        # 没有结构问题、但修复轮次恰好用尽 → 仍然允许渲染（渲染是纯计算，不烧钱）
        ({"autofix_rounds": 3}, "generate_hocon"),
    ],
)
def test_route_after_plan(state, expected):
    assert route_after_plan(state) == expected


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ({"gate_result": {"conclusion": "pass"}}, "freeze"),
        ({"gate_result": {"conclusion": "warn"}}, "freeze"),
        # 可修的阻塞 → 进修复回路
        (
            {
                "gate_result": {
                    "conclusion": "block",
                    "findings": [{"category": "quality", "severity": "block"}],
                }
            },
            "autofix",
        ),
        # 策略阻塞（P0/P1）不修：模型改不掉，让它改只是把「策略拒绝」伪装成「模型没修好」
        (
            {
                "gate_result": {
                    "conclusion": "block",
                    "findings": [{"category": "policy", "severity": "block"}],
                }
            },
            "terminal",
        ),
        # 轮次用尽 → 终局
        (
            {
                "gate_result": {
                    "conclusion": "block",
                    "findings": [{"category": "quality", "severity": "block"}],
                },
                "autofix_rounds": 3,
            },
            "terminal",
        ),
        # 策略阻塞优先于「轮次还没用尽」：先判策略，顺序反了会白烧三轮 LLM
        (
            {
                "gate_result": {
                    "conclusion": "block",
                    "findings": [
                        {"category": "policy", "severity": "block"},
                        {"category": "quality", "severity": "block"},
                    ],
                },
                "autofix_rounds": 0,
            },
            "terminal",
        ),
        # 只有 warn 级 finding 的 block 结论不会出现；这里守的是「没有 findings 时不误判」
        ({"gate_result": {"conclusion": "block"}}, "autofix"),
    ],
)
def test_route_after_gate(state, expected):
    assert route_after_gate(state) == expected


@pytest.mark.parametrize(
    ("source", "state", "expected"),
    [
        ("param_check", {"missing_params": ["x"]}, "clarify"),
        ("param_check", {}, "metadata_bind"),
        ("generate_plan", {}, "generate_hocon"),
        ("generate_plan", {"plan_errors": [{"code": "x"}], "autofix_rounds": 99}, "terminal"),
        ("gate", {"gate_result": {"conclusion": "pass"}}, "freeze"),
        ("gate", {"gate_result": {"conclusion": "block", "findings": []}}, "autofix"),
    ],
)
def test_router_outputs_all_have_a_target_in_the_compiled_graph(source, state, expected):
    """路由函数的返回值必须能对上编译图里的条件边标签。

    这一条守的是「路由改了而装配没跟着改」：LangGraph 对没有目标的标签会在**运行期**
    抛 ``InvalidUpdateError``，而那时图已经跑了一半。
    """
    _nodes, edges = _graph_view(_compiled())
    routers = {
        "param_check": route_after_param_check,
        "generate_plan": route_after_plan,
        "gate": route_after_gate,
    }
    label = routers[source](state)
    assert label == expected
    assert (source, label) in edges


# ---------------------------------------------------------------- 中断往返


def _mini_loop_graph():
    """``param_check → clarify → param_check`` 的子图（图内澄清回路的最小复刻）。

    ``metadata_bind`` 标签映射到 ``END``：本条用例只验证澄清往返，
    真去绑档案会把用例拖进数据库。
    """
    builder = StateGraph(EtlWorkflowState)
    builder.add_node("param_check", _guarded_node("param_check", param_check_node))
    builder.add_node("clarify", _guarded_node("clarify", clarify_node))
    builder.add_edge(START, "param_check")
    builder.add_conditional_edges(
        "param_check",
        route_after_param_check,
        {"clarify": "clarify", "metadata_bind": END},
    )
    builder.add_edge("clarify", "param_check")
    return builder.compile(checkpointer=InMemorySaver())


def _interrupt_value(result) -> dict:
    raw = result["__interrupt__"]
    first = raw[0] if isinstance(raw, (list, tuple)) else raw
    return getattr(first, "value", first)


@pytest.mark.asyncio
async def test_interrupt_resume_roundtrip_through_param_check_and_clarify():
    graph = _mini_loop_graph()
    thread_id = thread_id_for(7)
    config = invoke_config(thread_id, session=object(), ctx=_Ctx(), pipeline_id=7)

    first = await graph.ainvoke({"pipeline_id": 7, "intent": {}, "collected_params": {}}, config=config)
    payload = parse_interrupt(_interrupt_value(first))
    assert payload is not None
    assert payload["reason"] == "missing_params"
    assert [q["param"] for q in payload["questions"]] == list(REQUIRED_PARAM_ORDER)

    # 问卷必须**已经落检查点**：``interrupt()`` 之前的写入会在恢复时丢弃，
    # 所以问卷只能由不中断的 ``param_check`` 产出（症状是「澄清问题点进去是空的」）
    saved = await graph.aget_state(config)
    assert len(saved.values["pending_questions"]) == len(REQUIRED_PARAM_ORDER)

    answers = {
        "primary_key": "order_id",
        "incremental_column": "updated_at",
        "dedup_rule": "keep_latest",
        "target_table": "orders_wide",
        "quality_threshold": "0.0500",
    }
    resumed = await graph.ainvoke(Command(resume={"answers": answers}), config=config)
    assert resumed["collected_params"] == answers
    assert resumed["missing_params"] == []
    assert resumed["pending_questions"] == []
    assert "__interrupt__" not in resumed


@pytest.mark.asyncio
async def test_clarify_keeps_asking_while_params_are_still_missing():
    """答一半就恢复 → 重新判定仍然缺 → 再问；不变量是「问过的答案不会丢」。"""
    graph = _mini_loop_graph()
    config = invoke_config(thread_id_for(7), session=object(), ctx=_Ctx(), pipeline_id=7)
    await graph.ainvoke({"pipeline_id": 7, "intent": {}, "collected_params": {}}, config=config)

    second = await graph.ainvoke(Command(resume={"answers": {"primary_key": "order_id"}}), config=config)
    payload = parse_interrupt(_interrupt_value(second))
    assert payload is not None
    assert "primary_key" not in [q["param"] for q in payload["questions"]]
    assert second["collected_params"] == {"primary_key": "order_id"}


def test_invoke_config_puts_runtime_outside_the_state():
    """``AsyncSession`` 不可序列化，只能挂在 ``configurable`` 上（运行期事实）。"""
    session = object()
    config = invoke_config("7", session=session, ctx=_Ctx(), pipeline_id=7)
    assert config["configurable"]["thread_id"] == "7"
    runtime = config["configurable"]["runtime"]
    assert isinstance(runtime, NodeRuntime)
    assert runtime.pipeline_id == 7
    assert runtime.tenant_id == 1 and runtime.project_id == 2 and runtime.user_id == 3
    assert runtime.request_id == "req-test"


def test_invoke_config_omits_runtime_when_no_session():
    """``snapshot`` / ``abort`` 不跑节点，因此不需要 runtime。"""
    assert invoke_config("7") == {"configurable": {"thread_id": "7"}}


@pytest.mark.asyncio
async def test_runtime_missing_raises_a_loud_assembly_error():
    """节点拿不到 runtime 是**装配错误**（500），不是用户请求的问题。"""
    from workflow.nodes import RuntimeMissingError, runtime_of

    with pytest.raises(RuntimeMissingError) as excinfo:
        runtime_of({"configurable": {"thread_id": "7"}})
    assert excinfo.value.http_status == 500


# ---------------------------------------------------------------- 幂等 / 不重放探查


class _ProbeCounter:
    """``profile_summary`` 的替身：只数调用次数，返回一份合法摘要。"""

    def __init__(self) -> None:
        self.calls: list[int] = []

    async def __call__(self, *, session, tenant_id, data_source_id):
        self.calls.append(int(data_source_id))
        return {
            "profile_id": 100 + int(data_source_id),
            "version": 1,
            "sha256": f"{data_source_id:064d}",
            "tables_count": 3,
            "ready": True,
            "status": "ready",
        }


@pytest.mark.asyncio
async def test_metadata_bind_binds_once_and_never_re_probes_on_resume(monkeypatch):
    """be-09 §5.3 的核心承诺：恢复时**不重放**已完成的安全探查。

    做法是把 ``metadata_bind`` 与一个会中断的 ``clarify`` 串成子图：
    第二次 ``ainvoke``（恢复）只会从 ``clarify`` 处继续，
    上游的 ``metadata_bind`` 不得再被执行。计数断言是这条用例的全部意义——
    「档案在不在」之类的间接观察看不出重复探查，它只是慢一点、多留一条审计记录。
    """
    counter = _ProbeCounter()
    monkeypatch.setattr("services.pipeline.repository.profile_summary", counter)

    builder = StateGraph(EtlWorkflowState)
    builder.add_node("metadata_bind", _guarded_node("metadata_bind", metadata_bind_node))
    builder.add_node("clarify", _guarded_node("clarify", clarify_node))
    builder.add_edge(START, "metadata_bind")
    builder.add_edge("metadata_bind", "clarify")
    builder.add_edge("clarify", END)
    graph = builder.compile(checkpointer=InMemorySaver())

    config = invoke_config(thread_id_for(7), session=object(), ctx=_Ctx(), pipeline_id=7)
    initial = {
        "pipeline_id": 7,
        "source_data_source_id": 11,
        "target_data_source_id": 12,
        "pending_questions": [{"key": "primary_key", "question": "请指定去重主键", "kind": "text"}],
    }
    first = await graph.ainvoke(initial, config=config)
    assert counter.calls == [11, 12]
    assert first["profile_ids"] == {"11": 111, "12": 112}
    assert first["profile_sha256"]["11"] == f"{11:064d}"
    assert _interrupt_value(first)["questions"] == [{"param": "primary_key", "prompt": "请指定去重主键"}]

    resumed = await graph.ainvoke(Command(resume={"answers": {"primary_key": "order_id"}}), config=config)
    assert counter.calls == [11, 12], "恢复时重新探查了档案——be-09 §5.3 明令禁止"
    assert resumed["profile_ids"] == {"11": 111, "12": 112}


@pytest.mark.asyncio
async def test_metadata_bind_re_binds_when_the_input_changed(monkeypatch):
    """指纹变了就要重绑：产物在不在不能替代「输入有没有变」。"""
    counter = _ProbeCounter()
    monkeypatch.setattr("services.pipeline.repository.profile_summary", counter)
    node = _guarded_node("metadata_bind", metadata_bind_node)
    runtime = NodeRuntime(session=object(), ctx=_Ctx(), pipeline_id=7)
    config = invoke_config("7", session=runtime.session, ctx=runtime.ctx, pipeline_id=7)

    bound = await node({"pipeline_id": 7, "source_data_source_id": 11, "target_data_source_id": 12}, config)
    assert counter.calls == [11, 12]

    # 同一输入 + 产物齐 → no-op
    assert (
        await node(
            {**bound, "pipeline_id": 7, "source_data_source_id": 11, "target_data_source_id": 12}, config
        )
        == {}
    )
    assert counter.calls == [11, 12]

    # 换了目标数据源 → 必须重新绑定（否则门禁会拿旧目标的档案判列是否存在）
    await node({**bound, "pipeline_id": 7, "source_data_source_id": 11, "target_data_source_id": 99}, config)
    assert counter.calls == [11, 12, 11, 99]


@pytest.mark.asyncio
async def test_metadata_bind_rejects_expired_profile_before_any_llm_call(monkeypatch):
    """档案过期 → ``METADATA_PROFILE_EXPIRED``（409），且发生在 LLM 调用之前。"""

    async def expired(*, session, tenant_id, data_source_id):
        return {"profile_id": 1, "ready": True, "status": "ready", "expires_at": "2020-01-01T00:00:00Z"}

    monkeypatch.setattr("services.pipeline.repository.profile_summary", expired)
    node = _guarded_node("metadata_bind", metadata_bind_node)
    config = invoke_config("7", session=object(), ctx=_Ctx(), pipeline_id=7)
    with pytest.raises(AppError) as excinfo:
        await node({"pipeline_id": 7, "source_data_source_id": 11}, config)
    assert excinfo.value.code is ErrorCode.METADATA_PROFILE_EXPIRED
    assert excinfo.value.http_status == 409
    assert excinfo.value.details["reason"] == "expired"


@pytest.mark.asyncio
async def test_metadata_bind_rejects_not_ready_profile(monkeypatch):
    async def probing(*, session, tenant_id, data_source_id):
        return {"profile_id": 1, "ready": False, "status": "probing"}

    monkeypatch.setattr("services.pipeline.repository.profile_summary", probing)
    node = _guarded_node("metadata_bind", metadata_bind_node)
    config = invoke_config("7", session=object(), ctx=_Ctx(), pipeline_id=7)
    with pytest.raises(AppError) as excinfo:
        await node({"pipeline_id": 7, "source_data_source_id": 11}, config)
    assert excinfo.value.details["reason"] == "not_ready"


# ---------------------------------------------------------------- 生成锁


class _FakeRedis:
    """手写的 Redis 替身。

    **不用 fakeredis**：它的 ``EVAL`` 需要 ``lupa``（本机没装），
    于是「锁的释放脚本到底怎么被调用」这件事在假客户端上直接不可观测。
    而这条正是本模块最需要证据的地方——比较并删除写错了不会报错，
    只会静默删掉别人的锁。
    """

    def __init__(self, *, acquired: bool = True, eval_result: int = 1, eval_raises: bool = False) -> None:
        self.calls: list[tuple] = []
        self._acquired = acquired
        self._eval_result = eval_result
        self._eval_raises = eval_raises

    async def set(self, key, value, *, nx=False, ex=None):
        self.calls.append(("set", key, value, nx, ex))
        return True if self._acquired else None

    async def eval(self, script, numkeys, *args):
        self.calls.append(("eval", script, numkeys, *args))
        if self._eval_raises:
            raise ConnectionError("Error 22 while writing to socket")
        return self._eval_result


def test_gen_lock_key_uses_the_single_key_template():
    assert gen_lock_key(7) == "gen:lock:7"
    assert gen_lock_key(7) == KEY_GEN_LOCK.format(pipeline_id=7)


def test_gen_lock_ttl_matches_spec():
    """be-09 §10.2 的暂定值：600 s。"""
    assert GEN_LOCK_TTL_SEC == 600


@pytest.mark.asyncio
async def test_generation_lock_acquires_with_nx_ex_and_releases_only_its_own_token():
    redis = _FakeRedis()
    async with generation_lock(redis, 7) as token:
        assert isinstance(token, str) and token
        kind, key, value, nx, ex = redis.calls[0]
        assert (kind, key, nx, ex) == ("set", "gen:lock:7", True, 600)
        # 释放发生在退出之后
        assert [c[0] for c in redis.calls] == ["set"]

    kind, script, numkeys, key, arg_token = redis.calls[1]
    assert kind == "eval"
    assert script == RELEASE_LOCK_SCRIPT
    assert numkeys == 1
    assert key == "gen:lock:7"
    # 「只删自己那把锁」的全部实现就是这个相等判断；token 撞了它就恒真
    assert arg_token == value


@pytest.mark.asyncio
async def test_generation_lock_does_not_del_when_the_token_is_not_ours():
    """锁已被 TTL 清掉、又被人重新取走时，释放脚本返回 0——不能抛、也不能删别人的锁。"""
    redis = _FakeRedis(eval_result=0)
    async with generation_lock(redis, 7):
        pass
    assert redis.calls[-1][0] == "eval"


@pytest.mark.asyncio
async def test_generation_lock_unavailable_raises_conflict_409():
    redis = _FakeRedis(acquired=False)
    with pytest.raises(GenerationInProgressError) as excinfo:
        async with generation_lock(redis, 7):
            pytest.fail("拿不到锁却进了临界区")
    assert excinfo.value.http_status == 409
    assert excinfo.value.code is ErrorCode.CONFLICT
    assert isinstance(excinfo.value, ConflictError)
    assert excinfo.value.details == {"pipeline_id": 7}
    # 取不到锁就**立刻失败**：不等待、不排队、更不释放（我们根本没拿到）
    assert [c[0] for c in redis.calls] == ["set"]


@pytest.mark.asyncio
async def test_generation_lock_release_failure_does_not_mask_the_body_error():
    """``finally`` 里抛错会把「生成失败」变成「释放锁失败」——后者只是运维噪音。"""
    redis = _FakeRedis(eval_raises=True)
    with pytest.raises(ValueError):
        async with generation_lock(redis, 7):
            raise ValueError("生成失败")


@pytest.mark.asyncio
async def test_generation_lock_release_failure_alone_is_swallowed():
    """释放失败不抛：TTL 是这条路径的兜底，而把成功的一次生成改成 500 是纯损失。"""
    redis = _FakeRedis(eval_raises=True)
    async with generation_lock(redis, 7, ttl=30) as token:
        assert token
    assert redis.calls[0][4] == 30


# ---------------------------------------------------------------- 入图注入的 seam（未声明键会被丢掉）


@pytest.mark.asyncio
async def test_undeclared_input_keys_are_dropped_not_silently_used(monkeypatch):
    """未在 ``EtlWorkflowState`` 里声明的输入键**不会成为 channel，会被整键丢掉**。

    静默丢弃比报错难查：节点读到的是空串，报出来的却是业务语义的错
    （「需求描述不能为空」），于是真因（schema 漏声明了 ``requirement``）与症状
    （需求为空）之间隔着一整个模块——service 那边注入得好好的，
    接口这边却一口咬定「用户没填需求」。

    这条用例走**真图**（``build_graph`` + ``InMemorySaver``），因为要守的就是
    「StateGraph 从 schema 建 channel」这个行为本身；用假图去断言假图的 schema
    等于什么都没守。LLM 用回显桩顶掉：桩返回的需求原文就是从围栏里读出来的那串，
    所以「节点确实读到了注入进来的需求」是被证明的，而不是被假定的。
    """
    from clients.llm.prompts import fences
    from clients.llm.prompts.plan import TAG_REQUIREMENT
    from workflow.nodes.param_check import EtlIntent

    class _EchoIntentProvider:
        """把围栏里的需求原样回显进 ``EtlIntent.requirement``。"""

        name = "echo-intent"

        async def complete_json(self, req, schema):
            user = next(m for m in req.messages if m.role == "user")
            payload = fences.extract(user.content, TAG_REQUIREMENT) or ""
            return EtlIntent(requirement=payload.splitlines()[0].removeprefix("需求："), goal="")

    monkeypatch.setattr("workflow.nodes.intent_parse.llm_provider", _EchoIntentProvider)

    requirement = "把 biz_demo.orders 同步进 doris 宽表"
    graph = _compiled()
    config = invoke_config("7", session=object(), ctx=_Ctx(), pipeline_id=7)
    state = {
        "pipeline_id": 7,
        # 已声明的注入键：必须活到结果里
        "requirement": requirement,
        "thread_id": "7",
        "started_at": "2026-10-16T10:00:00+00:00",
        "user_id": 3,
        # 未声明的键：必须被丢掉（这是本条用例的断言对象）
        "batch_size_hint": 5000,
        "operator": "alice",
    }
    await graph.ainvoke(state, config)

    values = (await graph.aget_state(config)).values
    # 未声明的键一个都不该活下来（`operator` 是其中最危险的一类：
    # 它长得像操作者，于是「谁是操作者」在 state 里有两个键，其中一个永远是空的）
    assert "batch_size_hint" not in values
    assert "operator" not in values
    assert values["user_id"] == 3
    # 已声明的注入键必须在，且节点确实读到了它
    assert values["requirement"] == requirement
    assert values["intent"]["requirement"] == requirement
    assert values["thread_id"] == "7"
    # 走到澄清断点（说明 param_check 拿到的是一个「参数全缺」的意图——与需求是否被读到无关，
    # 这里只用来确认图没在半路炸掉）
    assert values["pending_questions"]

    # 反面：同一份输入**去掉**已声明的 requirement 就报业务错——这正是漏声明时看到的那条错，
    # 而它的措辞把排查方向指向了用户输入。
    with pytest.raises(AppError) as excinfo:
        await graph.ainvoke(
            {k: v for k, v in state.items() if k != "requirement"},
            invoke_config("8", session=object(), ctx=_Ctx(), pipeline_id=8),
        )
    assert excinfo.value.http_status == 422
    assert "需求描述不能为空" in excinfo.value.message


def _orchestrator_injected_keys() -> set[str]:
    """读 ``generation._initial_state`` 的**源码**，取出它返回的那个 dict 的键。

    用 AST 而不是正则：正则会把函数体里别的字符串当成键（比如嵌套 dict 的取值），
    而这里要的是「编排层到底注入了哪些键」这一个精确答案。

    不用「跑一遍 ``_initial_state``」：它要 session、要 repository、要 sources，
    于是这条用例会变成服务的集成测试——而它要守的是**键名**，
    键名在源码里就是全部的真相。
    """
    import ast
    import inspect
    import textwrap

    from services.pipeline import generation

    tree = ast.parse(textwrap.dedent(inspect.getsource(generation._initial_state)))
    returns = [node for node in ast.walk(tree) if isinstance(node, ast.Return)]
    assert returns, "generation._initial_state 没有 return，用例需要跟着改"
    value = returns[-1].value
    assert isinstance(value, ast.Dict), "generation._initial_state 不再直接 return 字面量 dict"
    keys = {k.value for k in value.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)}
    assert keys, "没能从 generation._initial_state 里读出任何键"
    return keys


def test_orchestrator_injected_keys_are_all_declared_in_the_state_schema():
    """编排层注入的键**必须**都在 ``EtlWorkflowState`` 里声明，否则会被静默丢掉。

    这是本文件里最该有的一条：``StateGraph(EtlWorkflowState)`` 只把 schema 里声明的键
    建成 channel，未声明的输入键既不报错也不留痕——service 那边注入得好好的，
    节点这边读到的是缺省值，报出来的却是「需求描述不能为空」这种业务错。
    清单不写死在用例里，而是从**产出方**的源码读：这样「service 加了新注入键」
    会立刻在用例上暴露，而不是等到某天某条链路莫名其妙地拿不到值。
    """
    injected = _orchestrator_injected_keys()
    missing = sorted(injected - STATE_KEYS)
    assert not missing, (
        f"编排层注入了 state 未声明的键，它们会被静默丢掉：{missing}；请在 workflow/state.py 里声明并写明理由"
    )
    # 操作者是这两侧最容易各写一个名字的地方（`user_id` / `subject_id` / `actor_id`）：
    # 名字不一致时键被丢掉，账本里的 actor_id 就恒为默认值——静默错，不报错。
    assert "user_id" in injected
    assert "user_id" in STATE_KEYS


def test_node_runtime_identity_reads_the_same_facts_as_the_state():
    """``NodeRuntime`` 的身份字段与 state 的同名键必须**同值**。

    守的是「重命名只改了一边」：节点里读的是 ``runtime.user_id``，
    而 state/账本里写的是 ``state["user_id"]``；只要有一边漂了，
    轻则 ``AttributeError``（生成到落事件时 500），重则写进账本的 actor 变成默认值。
    ``subject_id`` 曾经就是这么一个漂掉的键名。
    """
    # ``project_id`` 显式传 42（``ctx`` 上是 2）：用来证明它是**独立字段**
    # 而不是 ``ctx.project_id`` 的代理——真 ``SubjectContext`` 上根本没有这个属性。
    expected = {"tenant_id": 1, "project_id": 42, "pipeline_id": 7, "user_id": 3}
    runtime_attrs = {a for a in dir(NodeRuntime) if a.endswith("_id")}
    assert runtime_attrs == set(expected) | {"request_id"}, (
        f"NodeRuntime 的身份字段集合变了：{sorted(runtime_attrs)}；改了名字就要同步改 state 的键名与这条断言"
    )
    missing = sorted(set(expected) - STATE_KEYS)
    assert not missing, f"身份字段里有 state 未声明的键：{missing}"

    runtime = NodeRuntime(
        session=object(),
        ctx=_Ctx(),
        pipeline_id=expected["pipeline_id"],
        project_id=expected["project_id"],
    )
    for name, value in sorted(expected.items()):
        assert getattr(runtime, name) == value, f"{name} 在 runtime 与 state 上不同值"
    assert runtime.request_id == "req-test"


# ---------------------------------------------------------------- 提示词接 be-08（无 key 也能跑通）


class _CapturingProvider:
    """记下请求、再交给真的 ``FakeProvider`` 应答。

    只记不应答的话，用例就只证明了「请求拼出来了」，证明不了「拼出来的东西
    假 Provider 读得懂」——而后者才是这次改动的目的。
    """

    name = "capturing"

    def __init__(self) -> None:
        self.requests: list = []
        self._fake = FakeProvider()

    async def complete_json(self, req, schema):
        self.requests.append(req)
        return await self._fake.complete_json(req, schema)


class _BombProvider:
    """被调用即让用例失败：用来证明「这条路径**不该**走到 LLM」。"""

    name = "bomb"

    async def complete_json(self, req, schema):  # pragma: no cover - 走到就是失败
        raise AssertionError("这条路径不该调用 LLM")


#: ``_LedgerSpy`` 收下的事件。放模块级而不是类属性：类属性是可变的默认值，
#: 用例之间会互相污染（上一条的账落进下一条的断言里）。
_LEDGER_EVENTS: list[Any] = []


class _LedgerSpy:
    """``EvidenceLedger`` 的替身（真账本要 PG 事务）。"""

    async def append(self, session: Any, event: Any) -> None:
        _LEDGER_EVENTS.append(event)


def _plan_state(**overrides):
    """一份可以被 ``generate_plan`` 直接消费的 state（含三样注入画像）。"""
    state = {
        "pipeline_id": 7,
        "requirement": "把 biz_demo.orders 同步到 doris 宽表",
        "collected_params": {
            "target_table": "orders_wide",
            "primary_key": "order_id",
            "quality_threshold": "0.0500",
        },
        "profile_sha256": {"11": "a" * 64},
        "source_profile": {
            "data_source_id": 11,
            "schema": "biz_demo",
            "tables": [
                {
                    "name": "orders",
                    "row_count_approx": 120000,
                    "columns": [
                        {
                            "name": "order_id",
                            "data_type": "bigint",
                            "nullable": False,
                            "is_primary_key": True,
                            "sensitive_level": "internal",
                        },
                        {
                            "name": "amount",
                            "data_type": "decimal(10,2)",
                            "nullable": True,
                            "sensitive_level": "internal",
                        },
                    ],
                }
            ],
        },
        "target_profile": None,
        "budget": {
            "max_read_rows": 100000000,
            "max_write_bytes": 53687091200,
            "max_duration_sec": 7200,
            "max_amplification_ratio": "1.5",
            "max_reject_rate": "0.05",
            "warn_threshold_pct": 80,
        },
        "data_source_types": {"11": "mysql", "12": "doris"},
    }
    state.update(overrides)
    return state


def _plan_config():
    return invoke_config("7", session=object(), ctx=_Ctx(), pipeline_id=7)


@pytest.mark.asyncio
async def test_intent_parse_wraps_the_user_message_in_the_requirement_fence(monkeypatch):
    """be-08 的假 Provider 按**围栏内容**分派，手写的散文消息它读不懂。"""
    from clients.llm.prompts import fences
    from clients.llm.prompts.plan import TAG_REQUIREMENT
    from workflow.nodes.intent_parse import intent_parse_node
    from workflow.nodes.param_check import EtlIntent

    class _IntentProvider:
        name = "intent-stub"

        def __init__(self) -> None:
            self.requests: list = []

        async def complete_json(self, req, schema):
            self.requests.append(req)
            return EtlIntent(requirement="把 orders 同步进 doris", goal="")

    provider = _IntentProvider()
    monkeypatch.setattr("workflow.nodes.intent_parse.llm_provider", lambda: provider)

    state = {"pipeline_id": 7, "requirement": "把 orders 同步进 doris", "source_data_source_id": 11}
    out = await intent_parse_node(state, _plan_config())

    request = provider.requests[0]
    system, user = request.messages[0], request.messages[1]
    assert system.role == "system"
    assert user.role == "user"
    # 围栏必须在，且内容就是本轮的提示词正文
    payload = fences.extract(user.content, TAG_REQUIREMENT)
    assert payload is not None
    assert "把 orders 同步进 doris" in payload
    assert "11" in payload
    # 需求原文是**不可信输入**，必须留在 user 侧（be-08 §6.3）
    assert "把 orders 同步进 doris" not in system.content
    assert out["intent"]["requirement"] == "把 orders 同步进 doris"
    assert set(out["node_fingerprints"]) == {"intent_parse"}


@pytest.mark.asyncio
async def test_generate_plan_builds_the_prompt_through_be08(monkeypatch):
    """请求必须由 be-08 的三步拼出来：围栏齐全、json_mode、temperature=0、版本号来自 be-08。"""
    from clients.llm.prompts.plan import (
        PROMPT_VERSION_PLAN,
        TAG_BUDGET,
        TAG_DATA_SOURCE_TYPES,
        TAG_REQUIREMENT,
        TAG_SOURCE_PROFILE,
    )
    from workflow.nodes.generate_plan import generate_plan_node

    provider = _CapturingProvider()
    monkeypatch.setattr("workflow.nodes.generate_plan.llm_provider", lambda: provider)
    monkeypatch.setattr("harness.ledger.EvidenceLedger", _LedgerSpy)

    out = await generate_plan_node(_plan_state(), _plan_config())

    request = provider.requests[0]
    text = "\n".join(m.content for m in request.messages)
    for tag in (TAG_REQUIREMENT, TAG_SOURCE_PROFILE, TAG_BUDGET, TAG_DATA_SOURCE_TYPES):
        assert f"<<{tag}>>" in text, f"提示词里缺少 {tag} 围栏——be-08 的假 Provider 会直接拒答"
    # 目标表画像缺失是**正常**的（新建宽表），此时不应出现 TARGET_PROFILE 围栏
    assert "<<TARGET_PROFILE>>" not in text
    assert request.json_mode is True
    assert request.temperature == 0.0
    assert request.prompt_version == PROMPT_VERSION_PLAN

    assert out["draft_plan"]["field_mappings"]
    assert out["plan_errors"] == []
    assert out["generation_context"]["prompt_template_version"] == PROMPT_VERSION_PLAN
    # 溯源字段要从 provider 上取，取不到才回落配置——空串会让 frontend 的非空字段构造失败
    assert out["generation_context"]["model_version"]
    assert _LEDGER_EVENTS[-1].payload["prompt_versions"] == {"plan": PROMPT_VERSION_PLAN}
    _LEDGER_EVENTS.clear()


@pytest.mark.asyncio
async def test_generate_plan_runs_end_to_end_with_the_deterministic_fake_provider(monkeypatch):
    """``LLM_PROVIDER=fake`` 是**正式离线通路**：没有模型凭据时这条链必须能走完。

    这条用例的意义就是「本机（无 key）能不能生成出方案」，所以它用真 Provider 而不是替身。
    """
    from workflow.nodes.generate_plan import generate_plan_node

    monkeypatch.setattr("workflow.nodes.generate_plan.llm_provider", lambda: FakeProvider())
    monkeypatch.setattr("harness.ledger.EvidenceLedger", _LedgerSpy)

    out = await generate_plan_node(_plan_state(), _plan_config())

    plan = out["draft_plan"]
    assert plan["field_mappings"], "假 Provider 没能起草出字段映射"
    assert plan["budget"]["max_read_rows"] == 100000000
    assert out["plan_errors"] == []
    assert _LEDGER_EVENTS[-1].event_type.value == "pipeline.generated"
    _LEDGER_EVENTS.clear()


@pytest.mark.asyncio
async def test_generate_plan_generates_again_when_the_injected_profile_changed(monkeypatch):
    """输入变了就不许 no-op：列变了而方案不变 = 拿一份对不上表的方案往下走。"""
    from workflow.nodes.generate_plan import generate_plan_node

    provider = _CapturingProvider()
    monkeypatch.setattr("workflow.nodes.generate_plan.llm_provider", lambda: provider)
    monkeypatch.setattr("harness.ledger.EvidenceLedger", _LedgerSpy)

    state = _plan_state()
    first = await generate_plan_node(state, _plan_config())
    assert len(provider.requests) == 1

    # 同一输入 + 产物齐 → no-op
    assert await generate_plan_node({**state, **first}, _plan_config()) == {}
    assert len(provider.requests) == 1

    # 预算变了 → 必须重算（否则冻结的版本会带着一份过期预算）
    changed_budget = {**state["budget"], "max_read_rows": 1000}
    again = await generate_plan_node({**state, **first, "budget": changed_budget}, _plan_config())
    assert again["draft_plan"], "预算变了却复用了旧方案"
    assert len(provider.requests) == 2
    assert again["draft_plan"]["budget"]["max_read_rows"] == 1000
    _LEDGER_EVENTS.clear()


@pytest.mark.parametrize(
    ("missing", "field"),
    [
        ("source_profile", "source_profile"),
        ("budget", "budget"),
        ("data_source_types", "data_source_types"),
    ],
)
@pytest.mark.asyncio
async def test_generate_plan_reports_missing_injected_context_without_calling_the_model(
    monkeypatch, missing, field
):
    """缺注入 → 422 且**一次 LLM 都不调**。

    静默兜底（自己造预算、跳过档案块）会让模型在缺列的情况下猜列名，
    而猜出来的列名一路走到 HOCON，直到门禁才报——排查方向被带到模型质量上。
    """
    from workflow.nodes.generate_plan import generate_plan_node

    monkeypatch.setattr("workflow.nodes.generate_plan.llm_provider", _BombProvider)
    monkeypatch.setattr("harness.ledger.EvidenceLedger", _LedgerSpy)
    _LEDGER_EVENTS.clear()

    state = _plan_state(**{missing: None})
    with pytest.raises(AppError) as excinfo:
        await generate_plan_node(state, _plan_config())
    assert excinfo.value.http_status == 422
    assert excinfo.value.code is ErrorCode.VALIDATION_ERROR
    assert excinfo.value.details.get("missing") == field
    assert "编排层未注入" in excinfo.value.message
    # 没生成成功就没有证据事件：「生成过」的账不能记
    assert _LEDGER_EVENTS == []


@pytest.mark.asyncio
async def test_generate_plan_rejects_a_malformed_injected_profile(monkeypatch):
    """注入的画像形状不对 → 422 并指出错在哪个字段，而不是让它走到模型那一侧才炸。"""
    from workflow.nodes.generate_plan import generate_plan_node

    monkeypatch.setattr("workflow.nodes.generate_plan.llm_provider", _BombProvider)
    state = _plan_state(source_profile={"data_source_id": 11, "tables": []})  # tables 不能为空
    with pytest.raises(AppError) as excinfo:
        await generate_plan_node(state, _plan_config())
    assert excinfo.value.http_status == 422
    assert excinfo.value.details["field"] == "source_profile"
    assert excinfo.value.details["error_paths"]

    with pytest.raises(AppError):
        await generate_plan_node(_plan_state(target_profile="orders_wide"), _plan_config())


@pytest.mark.asyncio
async def test_generate_plan_rejects_non_numeric_data_source_type_keys(monkeypatch):
    """键空间混了数据源名而不是 id：报错而不是丢弃——少一个类型会静默渲染成默认插件。"""
    from workflow.nodes.generate_plan import generate_plan_node

    monkeypatch.setattr("workflow.nodes.generate_plan.llm_provider", _BombProvider)
    state = _plan_state(data_source_types={"mysql_main": "mysql"})
    with pytest.raises(AppError) as excinfo:
        await generate_plan_node(state, _plan_config())
    assert excinfo.value.http_status == 422
    assert excinfo.value.details["field"] == "data_source_types"
    assert "数字" in excinfo.value.message


@pytest.mark.asyncio
async def test_generate_plan_requires_a_requirement(monkeypatch):
    """需求为空是装配/调用错误：没有需求，模型只能凭空造一份方案。"""
    from workflow.nodes.generate_plan import generate_plan_node

    monkeypatch.setattr("workflow.nodes.generate_plan.llm_provider", _BombProvider)
    state = _plan_state(requirement="")
    with pytest.raises(AppError) as excinfo:
        await generate_plan_node(state, _plan_config())
    assert excinfo.value.http_status == 422
