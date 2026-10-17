"""Workflow State 契约：字段、归约、JSON 可序列化守卫、前端快照投影（be-09 §4 / §5.2 / §6.1）。

**本模块是 Workflow State 的唯一定义处**。节点返回未在此声明的键会被 ``graph.build_graph``
装配的守卫拦下（LangGraph 对未知键是宽容的，而「宽容」意味着某天有人把 ``execution_run``
的字段塞进来也不会报错——那正是三层状态隔离被打破的起点）。

## 与设计文档 / 前端的差异登记

1. **快照字段以前端为准**。``frontend/src/types/pipeline.ts`` 的 ``WorkflowSnapshot``
   （``thread_id / phase / phase_message / pending_questions / autofix_round / last_error``）
   已经冻结，:data:`SNAPSHOT_KEYS` 是它的逐字复刻；be-09 §3.1 的 ``WorkflowStateSnapshot``
   没有额外字段，二者一致。
2. **``pending_questions`` 是 ``ClarificationQuestion[]``（前端形状），不是 §3.1 的
   ``{param, prompt}[]``**。两份形状都要，各服务一个消费者：
   - 快照（``pending_questions``）→ 前端 Studio 渲染问卷 → 前端形状（``key/question/kind/…``）；
   - 图内 ``interrupt()`` 载荷（``interrupts.interrupt_payload``）→ 后端契约形状（``param/prompt``）。
   两者由同一份 ``MissingParam`` 单向派生，不会漂移。
3. **state 比 §5.2 的字段清单多 16 个字段**（都必需，逐条在下方字段注释里写明理由）：
   ``source_data_source_id`` / ``target_data_source_id``（§5.1 把它们列为 ``metadata_bind``
   的输入）、``intent``（§5.1 把 ``intent`` 列为 ``param_check`` 的输入）、
   ``target_table_hint``（§6.2 的 ``target_table`` 回落分支）、``pending_questions``、
   ``node_fingerprints``（§5.3 的幂等守卫需要「上次的输入指纹」）、
   ``hocon_meta``（冻结时要写 ``parser_version``）、``data_source_types``（渲染上下文，
   engines 不许查库、本模块又没有取数据源类型的 service，只能由 orchestrator 注入）、
   ``aborted``（``abort`` 后的快照要落回 ``idle``，前端 mock 已按此渲染）、
   ``source_profile`` / ``target_profile`` / ``budget``（be-08 ``plan_blocks`` 的三样原料，
   同理由：engines 不许查库、本模块不许自己造预算）、
   ``requirement`` / ``thread_id`` / ``started_at`` / ``profile_summaries``
   （orchestrator 每轮都注入的既定事实，见各自字段注释）。
   一条通用判据：**凡是「engines 不许查、service 又没给」的外部事实，都必须是入图注入**。

   **未声明的输入键会被静默丢掉**（``StateGraph(EtlWorkflowState)`` 只把 schema 里的键
   建成 channel）。这条不是 LangGraph 的怪癖，而是本文件存在的理由：
   ``generation._initial_state`` 注入了 ``requirement`` 而 schema 没声明时，
   节点读到的 ``state["requirement"]`` 是缺省值、报出来的却是
   「需求描述不能为空」这种**业务语义**的错——真因（字段没声明）与症状（需求为空）
   之间隔着一整个模块。回归用例：
   ``tests/test_workflow_graph.py::test_undeclared_input_keys_are_dropped_not_silently_used``。
4. **恢复输入兼容两种形状**：be-09 §5.3 的 ``{"content", "params"}`` 与前端
   ``POST /pipelines/generations/{thread_id}/answers`` 的 ``{"answers": {key: value}}``。
   见 :func:`workflow.interrupts.parse_resume_input`。

## 硬性约束（be-09 §5.2）

state 内所有值必须能 ``json.dumps``：只允许 ``str / int / float / bool / None / list / dict``。
**禁止** ORM 实例、``AsyncSession``、LLMProvider / httpx / Redis 客户端、``datetime`` /
``Decimal`` / ``bytes``。时间一律 ISO-8601 字符串，比率一律字符串定标（``"0.0500"``），
Pydantic 模型一律 ``model_dump(mode="json")`` 之后再入 state。
违反时 LangGraph 要么当场序列化失败、要么写出一份**读不回来**的检查点——
后者更危险，因为它在写的时候是绿的。故 :func:`assert_json_serializable` 在 ``snapshot()``
与每个节点出口都被调用。
"""

from __future__ import annotations

import json
from typing import Annotated, Any, Literal, TypedDict

from core.errors import AppError, ErrorCode
from models.enums import PipelineStatus

__all__ = [
    "ABORTED_MESSAGE",
    "PHASE_MESSAGES",
    "SNAPSHOT_KEYS",
    "STATE_FORBIDDEN_KEY_PREFIXES",
    "STATE_FORBIDDEN_KEYS",
    "STATE_KEYS",
    "STUDIO_PHASES",
    "ChatMessageDict",
    "EtlWorkflowState",
    "StateNotSerializableError",
    "already_done",
    "assert_json_serializable",
    "assert_no_execution_keys",
    "find_non_serializable",
    "input_fingerprint",
    "phase_for",
    "phase_message_for",
    "snapshot",
    "thread_id_for",
]

# ---------------------------------------------------------------- 前端临时阶段


#: 逐字来自 ``frontend/src/types/pipeline.ts`` 的 ``StudioPhase``（fe-04 §10）。
#: **新增取值必须同步前端**；本元组的键集由 ``PHASE_MESSAGES`` 的完整性用例守着。
StudioPhase = Literal[
    "idle",
    "parsing",
    "awaiting_clarification",
    "generating",
    "gate_running",
    "gate_failed",
    "ready_to_freeze",
    "frozen",
    "submitted",
    "error",
]

STUDIO_PHASES: tuple[StudioPhase, ...] = (
    "idle",
    "parsing",
    "awaiting_clarification",
    "generating",
    "gate_running",
    "gate_failed",
    "ready_to_freeze",
    "frozen",
    "submitted",
    "error",
)

#: 每个阶段给前端的一句话。``idle`` / ``error`` 是 ``None``：
#: 前者是「什么都没发生」，后者是「错了」——错误详情走 ``last_error``，
#: 在 ``phase_message`` 里再说一遍会让同一个错误显示两次。
PHASE_MESSAGES: dict[StudioPhase, str | None] = {
    "idle": None,
    "parsing": "正在解析需求",
    "awaiting_clarification": "需要补充口径后才能生成方案",
    "generating": "正在生成方案",
    "gate_running": "正在执行门禁检查",
    "gate_failed": "门禁未通过，请调整方案或参数",
    "ready_to_freeze": "方案已生成，门禁通过，可冻结版本",
    "frozen": "版本已冻结",
    "submitted": "版本已提交，进入准备与审批",
    "error": None,
}

#: ``abort`` 之后的文案。前端 mock（``src/api/mock/index.ts``）已按这一句渲染，
#: 且它**不落 state**——``aborted=True`` 时阶段一定是 ``idle``，文案随之固定。
ABORTED_MESSAGE = "已取消生成"

#: 冻结之后（准备 / 审批 / 执行）在 Studio 里统一呈现为 ``submitted``：
#: 这些状态的细粒度进度由 be-13/be-14 的接口与页面承担，Studio 只表示「已经交出去了」。
_SUBMITTED_STATUSES: frozenset[str] = frozenset(
    {
        PipelineStatus.PREPARING,
        PipelineStatus.PENDING_APPROVAL,
        PipelineStatus.APPROVED,
        PipelineStatus.COMMITTABLE,
        PipelineStatus.QUEUED,
        PipelineStatus.RUNNING,
        PipelineStatus.SUCCEEDED,
        PipelineStatus.FAILED,
        PipelineStatus.INTERRUPTED,
        PipelineStatus.ROLLING_BACK,
        PipelineStatus.ROLLED_BACK,
        PipelineStatus.REJECTED,
    }
)

#: 快照字段集合 = 前端 ``WorkflowSnapshot`` 的键集合（**恰好等于**，多一个都不行）。
SNAPSHOT_KEYS: frozenset[str] = frozenset(
    {
        "thread_id",
        "phase",
        "phase_message",
        "pending_questions",
        "autofix_round",
        "last_error",
    }
)

#: 执行态字段的键前缀/全名（be-09 §6.1 第 1 条）。快照里出现它们就说明
#: 「执行状态」被混进了「工作流状态」，这是三层隔离被打破的**可检测**信号。
STATE_FORBIDDEN_KEY_PREFIXES: tuple[str, ...] = ("execution_", "executionrun", "run_")
STATE_FORBIDDEN_KEYS: frozenset[str] = frozenset(
    {"run", "runs", "execution_run", "execution_run_id", "quality_report", "execution_metric"}
)


# ---------------------------------------------------------------- state 字段


ChatMessageDict = dict[str, Any]


def _append(left: list[Any] | None, right: list[Any] | None) -> list[Any]:
    """追加归约（be-09 §5.2 的 ``messages`` / ``autofix_history``）。

    两个 ``None`` 都要容忍：LangGraph 首次调用节点时，未被写过的通道传进来的是 ``None``
    而不是 ``[]``（``total=False`` 的坑），写 ``left + right`` 会直接 TypeError。
    """
    return list(left or []) + list(right or [])


class EtlWorkflowState(TypedDict, total=False):
    """LangGraph 的工作流状态。落 ``langgraph_checkpoint*``（PG），``thread_id = str(pipeline_id)``。

    ``total=False`` 是刻意的：这个状态是**增量累积**的（每个节点只回写自己产出的键），
    要求全字段到齐才能进图会让「意图还没解析完」的第一次调用无法进行。
    代价是「取不存在的键不会报错」——所以凡是读 state 的地方一律 ``state.get(...)``，
    由 :func:`already_done` 一类的守卫把「缺失」显式化。
    """

    # --- 标识（进入图前固定，节点不得修改）---
    pipeline_id: int
    tenant_id: int
    project_id: int
    #: 操作者。名字**取自它唯一的产出方**（``schemas.auth.SubjectContext.user_id``，
    #: 也就是 orchestrator 注入时用的键）：state 里的键名必须与注入方的键名逐字相同，
    #: 否则键会被静默丢掉、而读它的节点只会看到「没有操作者」。
    #: 证据账本里它填 ``actor_id``——``LedgerEvent.subject_id`` 是**事件主体**（资源），
    #: 两处是不同语义，不要因为名字像就互相赋值。
    user_id: int
    request_id: str
    #: ``= str(pipeline_id)``（be-09 §5.3）。orchestrator 手里本来就有它（恢复接口按它寻址），
    #: 声明它是为了让「恢复用的线程号」与「快照里的线程号」是**同一个值**而不是两次推导。
    thread_id: str
    #: 本轮开始的时刻（ISO-8601 字符串）。前端时间线与「生成耗时」都从它起算；
    #: 存字符串而不是 ``datetime``：state 里不许出现 ``datetime``（见模块 docstring 的硬约束）。
    started_at: str
    #: 需求原文。``intent_parse`` 与 ``generate_plan`` 都要读它拼提示词。
    #: **它不进日志、不进证据账本**（be-09 §8）——只记长度。
    requirement: str

    #: 源/目标数据源 id。§5.1 明确把它们列为 ``metadata_bind`` 的输入、
    #: §5.2 的字段清单里却漏了；没有它们，档案无从绑定、渲染上下文也拼不出来。
    source_data_source_id: int | None
    target_data_source_id: int | None

    # --- 环境与渲染上下文（orchestrator 注入）---
    #: ``data_source_id(str) -> profile_summary() 的返回``。orchestrator 拼
    #: ``target_table_hint``（be-09 §6.2 的回落分支）时要看的目标库表清单就在这里。
    #: ``metadata_bind`` **仍然自己再读一次**：它的职责是「现在这份档案可用吗」，
    #: 用一份入图时抓的快照去回答「现在」是不可靠的（档案可能刚刚过期）。
    profile_summaries: dict[str, Any]
    #: ``Env`` 的 value。PDP 对 prod 的判定显著不同，所以它是**入图的入参**而不是节点产物。
    env: str
    #: ``data_source_id(str) -> 数据源类型``。渲染 HOCON 需要它（``RenderContext``），
    #: 而 engines 层不许查库、本模块又没有「读数据源类型」的 service —— 故由 orchestrator
    #: 一次性注入，避免节点自己去拼一条跨越数据访问层的近路。
    data_source_types: dict[str, str]
    #: 源端候选表画像：``engines.plan.draft.DraftSourceTable`` 的
    #: ``model_dump(mode="json", by_alias=True)``。be-08 的 ``plan_blocks`` 要它才产得出
    #: ``<<SOURCE_PROFILE>>`` 围栏（列名与类型必须给，否则模型只能猜列名）。
    #: engines 不许查库、本模块不许读 ``metadata_profile.schema_snapshot``（那是 be-04 的
    #: 数据访问），所以只能由 orchestrator 注入。
    source_profile: dict[str, Any] | None
    #: 目标端：``DraftTargetTable`` 的同款 dump。**允许 ``None``**——新建宽表时目标表
    #: 还不存在，这是常态而不是缺注入（``plan_blocks`` 也把它当可选项）。
    target_profile: dict[str, Any] | None
    #: ``engines.plan.schema.RuntimeBudget`` 的 dump（orchestrator 从
    #: ``services.policy.budget_defaults()`` 取）。**预算默认值是策略（be-07）**，
    #: 节点自己造一个数就是把治理参数写死在业务代码里，且 PDP 的「超默认倍数」
    #: 判定会拿一个来路不明的基准去比。
    budget: dict[str, Any] | None

    # --- 对话（归约：追加）---
    #: 用 ``dict`` 而不是 ``ChatMessage`` 模型：state 必须 JSON 可序列化，
    #: 存 Pydantic 模型要么被 LangGraph 的序列化器转成 dict（静默）、要么报错，
    #: 不如在**写入点**就 ``model_dump(mode="json")``，全靠字符串与内建类型说话。
    messages: Annotated[list[ChatMessageDict], _append]

    # --- 意图与参数 ---
    #: §5.1 把它列为 ``param_check`` 的输入，§5.2 的字段清单里却漏了它。
    #: 没有它，``find_missing_params`` 的第二个回退来源就不存在，缺参数会永远报「全缺」。
    intent: dict[str, Any]
    collected_params: dict[str, Any]
    #: ``target_table`` 的回落来源（be-09 §6.2 的例外分支）：目标库档案里已存在的表名，
    #: 由 orchestrator 注入。``metadata_bind`` 在 ``param_check`` **之后**才跑，
    #: 所以这个分支不可能由「后一个节点」提供——只能作为入图事实注入。
    #: 缺省（``None``）时 ``target_table`` 按必需项处理，不会静默取一个猜测的表名。
    target_table_hint: str | None
    missing_params: list[str]
    #: 前端 ``ClarificationQuestion[]``（快照直接投影）。由 ``param_check`` 产出——
    #: **不能**由 ``clarify`` 产出：``interrupt()`` 之前的写入不会被持久化（节点在恢复时整体重跑）。
    pending_questions: list[dict[str, Any]]

    # --- 元数据档案 ---
    profile_ids: dict[str, int]
    profile_sha256: dict[str, str]

    # --- 草稿制品（冻结前只活在 state 里，be-09 §5.4）---
    draft_plan: dict[str, Any] | None
    plan_errors: list[dict[str, Any]]
    draft_hocon: str | None
    hocon_errors: list[dict[str, Any]]
    #: HOCON 的渲染/解析元信息：``{sha256, parser_version, job_name, renderer_version}``。
    #: 冻结时 ``save_artifacts`` 要 ``hocon_sha256`` 与 ``parser_version``，
    #: 两者都是「渲染那一刻的事实」，事后重算等价于把事实交给第二次计算。
    hocon_meta: dict[str, Any]

    # --- 门禁与自动修复 ---
    gate_result: dict[str, Any] | None
    autofix_rounds: int
    autofix_history: Annotated[list[dict[str, Any]], _append]

    # --- 状态机与终止 ---
    #: ``PipelineStatus`` 的 value。
    status: str
    #: ``ErrorCode`` 的 value。生成类失败落这里并**正常返回 200**（be-09 §3.2）。
    terminal_error: str | None
    generation_context: dict[str, Any]
    frozen_version_id: int | None

    # --- 幂等守卫（be-09 §5.3：跳过已完成步骤的机制 = 节点幂等）---
    #: ``node_name -> 该节点本次输入指纹``。恢复会把已完成节点重放，
    #: 所以「目标产物已在 state 中 **且** 输入指纹未变」才允许 no-op。
    node_fingerprints: dict[str, str]
    #: ``abort`` 之后的标记：快照回到 ``idle``，且不再继续跑图。
    aborted: bool


#: 全部已声明字段（``build_graph`` 的节点出口守卫与用例都读它）。
STATE_KEYS: frozenset[str] = frozenset(EtlWorkflowState.__annotations__.keys())


# ---------------------------------------------------------------- 序列化守卫


class StateNotSerializableError(AppError):
    """state 里出现了不能进检查点的值。

    用 ``INTERNAL_ERROR``（500）而不是 422：它是**实现缺陷**，不是用户输入的问题。
    报错信息里带路径（``$.messages[0].created_at``）与类型名，但不带值本身——
    值可能是样本原文或密钥，而这条异常会进日志与错误响应。
    """

    code = ErrorCode.INTERNAL_ERROR
    http_status = 500
    message = "工作流状态含不可序列化的值"


def find_non_serializable(value: Any, *, path: str = "$", depth: int = 0) -> str | None:
    """返回第一个不可序列化值的路径，全部合法则返回 ``None``。

    自己走一遍而不是只靠 ``json.dumps`` 抛异常：``dumps`` 的报错只说
    「Object of type datetime is not JSON serializable」，不说**在哪**——
    而 state 里字段有二十多个，靠猜定位等于把排查成本推给下一个人。
    """
    if depth > 64:  # 防御性：循环引用
        return f"{path}（嵌套过深，疑似循环引用）"
    if value is None or isinstance(value, (str, bool, int)):
        return None
    if isinstance(value, float):
        # NaN / Infinity 能过 json.dumps（产出非标准 JSON）但过不了真序列化器，
        # 且 LangGraph 的检查点写进 PG 的 jsonb 会被拒——在写入前就拦下。
        if value != value or value in (float("inf"), float("-inf")):
            return f"{path}（float 非有限值）"
        return None
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                return f"{path}.<{type(key).__name__} 键>（dict 键必须是字符串）"
            hit = find_non_serializable(item, path=f"{path}.{key}", depth=depth + 1)
            if hit is not None:
                return hit
        return None
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            hit = find_non_serializable(item, path=f"{path}[{index}]", depth=depth + 1)
            if hit is not None:
                return hit
        return None
    return f"{path}（{type(value).__module__}.{type(value).__qualname__}）"


def assert_json_serializable(value: Any, *, where: str = "workflow state") -> None:
    """state 出口的唯一守卫。见模块 docstring 的「硬性约束」。

    额外做一次 ``json.dumps`` 而不只是依赖上面的遍历：遍历是我们自己写的，
    而真正要满足的契约是标准库序列化器的行为——两者都过才算过。
    """
    hit = find_non_serializable(value)
    if hit is not None:
        raise StateNotSerializableError(f"{where} 含不可序列化的值：{hit}")
    try:
        json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:  # pragma: no cover - 上面的遍历已覆盖
        raise StateNotSerializableError(f"{where} 无法 JSON 序列化：{type(exc).__name__}") from exc


def assert_no_execution_keys(state: dict[str, Any]) -> None:
    """拒绝执行态字段混入工作流状态（be-09 §6.1）。

    「执行状态不得被对话状态覆盖」这条协议靠三件事兜底，这是其中一件可自动验证的：
    另一个是前端只认白名单键（``SNAPSHOT_KEYS``），第三个是 ``transitions`` 对
    ``running/succeeded/...`` 没有出边。三者都在，是因为任何一处单独失效都不该致命。
    """
    for key in state:
        if key in STATE_FORBIDDEN_KEYS or key.startswith(STATE_FORBIDDEN_KEY_PREFIXES):
            raise StateNotSerializableError(
                f"工作流状态不得包含执行态字段：{key!r}。执行状态请查 /api/v1/runs（be-09 §6.1）"
            )


# ---------------------------------------------------------------- 指纹与幂等


def input_fingerprint(**parts: Any) -> str:
    """节点输入指纹。用 ``core.ids.fingerprint``（规范化 JSON）而非拼字符串：
    拼字符串时 ``{"a": "1|2"}`` 与 ``{"a": "1", "b": "2"}`` 会产生同一个指纹，
    而「输入没变就跳过」正是建立在指纹能区分输入之上。"""
    from core.ids import fingerprint as _fingerprint

    return _fingerprint(parts)


def already_done(state: EtlWorkflowState, node: str, fingerprint: str, *required_fields: str) -> bool:
    """该节点在本轮输入下是否已完成（→ 直接 no-op）。

    两个条件缺一不可：
    1. **输入指纹未变**——只看「产物在不在」会让「用户改了参数」复用旧产物；
    2. **产物确实存在**——只看指纹会在检查点刚写入、产物还没落盘时误判（恢复的中途态）。

    ``required_fields`` 里的空值判定：``None`` / ``[]`` / ``{}`` / ``""`` 都算「没有」。
    ``0`` 和 ``False`` **不算**——把它们当空会让「autofix_rounds=0」这类合法值永远重跑。
    """
    if state.get("node_fingerprints", {}).get(node) != fingerprint:
        return False
    for field in required_fields:
        value = state.get(field)
        if value is None or (isinstance(value, (list, dict, str)) and len(value) == 0):
            return False
    return True


# ---------------------------------------------------------------- 快照投影


def thread_id_for(pipeline_id: int) -> str:
    """``thread_id = str(pipeline_id)``（be-09 §5.3）。

    写成函数而不是各处 ``str(...)``：它是**跨进程的恢复钥匙**——api 与 worker、
    今天的调用与明天的调用，只要 pipeline_id 相同就必须落到同一条检查点线程。
    任何一处写成 ``f"pipeline-{id}"`` 都会让恢复静默失败（图从头再跑一遍，
    用户看到「答过的澄清问题又问了」）。
    """
    return str(int(pipeline_id))


def phase_for(state: EtlWorkflowState) -> StudioPhase:
    """Workflow State → 前端 ``StudioPhase``（映射的唯一实现，前端不自行推导）。

    判定顺序即优先级，从「终局」往「过程」走：
    ``abort → 终止错误 → 冻结/已提交 → 待澄清 → 门禁结论 → 制品进度 → 解析``。
    把终局放前面是必须的：一次失败之后 ``draft_plan`` 可能还留在 state 里，
    若先判「有 plan 就是 generating」，用户会看到失败的任务显示「正在生成」。
    """
    if state.get("aborted"):
        return "idle"
    if state.get("terminal_error"):
        return "error"
    status = str(state.get("status") or "")
    if status == PipelineStatus.VERSION_FROZEN or state.get("frozen_version_id") is not None:
        return "frozen"
    if status in _SUBMITTED_STATUSES:
        return "submitted"
    if state.get("missing_params") or state.get("pending_questions"):
        return "awaiting_clarification"
    gate = state.get("gate_result")
    if isinstance(gate, dict):
        return "gate_failed" if gate.get("conclusion") == "block" else "ready_to_freeze"
    if state.get("draft_hocon"):
        return "gate_running"
    if state.get("draft_plan") is not None:
        return "generating"
    # ``requirement`` 与 ``messages`` 都算「已经进来了、正在解析」：
    # 服务层注入的是 ``requirement``（见 ``services/pipeline/generation._initial_state``），
    # 而 be-09 §5.2 的对话数组是 ``messages``。只认后者会让整段解析期都显示 ``idle``。
    if state.get("requirement") or state.get("messages"):
        return "parsing"
    return "idle"


def phase_message_for(state: EtlWorkflowState, phase: StudioPhase) -> str | None:
    """阶段文案。``aborted`` 时固定为「已取消生成」（与前端 mock 一致）。"""
    if state.get("aborted"):
        return ABORTED_MESSAGE
    return PHASE_MESSAGES[phase]


def snapshot(state: EtlWorkflowState) -> dict[str, Any]:
    """Workflow State → 前端快照（**键集合恰等于** :data:`SNAPSHOT_KEYS`）。

    ``state`` 由 LangGraph 从检查点读回，形状可能来自**上一个版本的代码**
    （容器滚动升级期间正在飞的那次恢复）。所以这里对每个字段都做类型兜底，
    且绝不直接把 state 整个返回出去——只投影白名单键，这是三层隔离里
    「执行状态不得被工作流状态覆盖」的第一道实现。
    """
    assert_no_execution_keys(dict(state))
    phase = phase_for(state)
    # ``thread_id`` 优先取注入进来的那个值（恢复接口就是按它寻址的），
    # 取不到才由 ``pipeline_id`` 推导——两者必须相等，所以推导只是兜底而不是第二种算法。
    declared = str(state.get("thread_id") or "").strip()
    result: dict[str, Any] = {
        "thread_id": declared or thread_id_for(int(state.get("pipeline_id") or 0)),
        "phase": phase,
        "phase_message": phase_message_for(state, phase),
        "pending_questions": [dict(q) for q in (state.get("pending_questions") or [])],
        "autofix_round": int(state.get("autofix_rounds") or 0),
        "last_error": state.get("terminal_error"),
    }
    assert_json_serializable(result, where="工作流快照")
    return result
