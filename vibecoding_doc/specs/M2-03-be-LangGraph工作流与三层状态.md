# M2-03 · be-09 LangGraph 工作流与三层状态

> 状态：**已实现**（`EtlWorkflowState` 单一定义 + 10 节点图 + PG Checkpoint 中断/恢复 + Redis 生成锁 + 状态转移表 + 快照投影 + 生成 service 与 4 条路由）
> 本 spec 为实现后补写，记录**实际落地**的形态；与 be-09 设计文档不一致处已在 §10 登记。
> 期次：M2 ｜ 依赖：be-01（`core`）、be-02（`SubjectContext` / `active_roles`）、be-06（证据账本）、be-08（LLM Provider 抽象层）、be-10（Plan 校验 / HOCON 渲染 / `parts` 指纹）、be-11（门禁与自动修复算法）、be-12（冻结落库，见 §10.15 的边界调整）
> 被依赖：be-12（冻结读图产出的 state）、前端 Studio（`WorkflowSnapshot`）、be-13/be-14/be-16（沿用 `transitions.assert_transition`）
> 设计来源：[be-09-LangGraph工作流与三层状态.md](../模块概要设计/be-09-LangGraph工作流与三层状态.md)
> 数据来源：[核心数据字典.md](../核心数据字典.md) §3（Pipeline 与制品）、§2（`metadata_profile`）、§8（支撑表：`pipeline_conversation` / `langgraph_checkpoint*` / `idempotency_key`）、§7（`evidence_event`）
> 需求来源：[PRD.md](../PRD.md) §6.3.1（中断提问 / Checkpoint 恢复 / 不重复安全探查 / 状态展示）、§8.1（三层状态隔离）、§8.3（防重放）

---

## 1. 模块定位

### 1.1 做什么

1. **Workflow State 的唯一定义处**：`workflow/state.py` 声明 `EtlWorkflowState`（TypedDict，`total=False`）、`StudioPhase`（10 个页面临时阶段，不落库）、`SNAPSHOT_KEYS`（恰好 6 个前端契约字段），以及 JSON 可序列化守卫。**加一个 state 键 = 三处一起改**（`state.py` 的声明、`nodes/__init__.py` 的 `NODE_OUTPUTS`、`services/pipeline/generation._initial_state`）。
2. **编排图**：10 个节点、含条件路由的 DAG（§5.2）。图是纯编排——生成/渲染/门禁/修复的**算法**分别在 be-10 / be-11，本模块只负责「什么时候调、调完去哪」。
3. **中断与恢复**：澄清缺少的必需参数时 `interrupt()`；断点落 PostgreSQL Checkpoint（`thread_id = str(pipeline_id)`），容器重启后仍能接着答（CLAUDE.md §9 明令禁止 `MemorySaver` 假装持久化）。
4. **并发串行化**：同一 Pipeline 的生成由 Redis 红锁 `gen:lock:{pipeline_id}` 挡住，取不到锁立刻 409，不等待、不排队（§5.4）。
5. **幂等**：① 节点级——每个节点进入时先看「目标产物已在 state 且输入指纹未变」则 no-op；② 请求级——`Idempotency-Key` 由中间件按 `(tenant_id, scope, key)` 落 `idempotency_key`。
6. **状态转移守卫**：`workflow/transitions.py` 是 `PipelineStatus` 合法转移的表 + 纯函数；越界即 409 且**不改 `pipeline.status`**。
7. **快照投影**：把 state 裁成 `WorkflowSnapshot`（前端 Studio 的单一状态来源）。
8. **生成 service**：`services/pipeline/generation.py` 负责建流程、凑初始 state（查库填数据源类型/环境/列画像）、驱动一轮、把这一轮的产物落一次。

### 1.2 明确不做

- **不做制品指纹与冻结语义**（be-12）。本模块不决定版本号、不算制品 sha256；它负责「调用 freeze 节点」与「把这一轮的产物落一次」（边界见 §10.15）。
- **不做门禁判定**（be-11）。`gate` 节点只是把 be-11 的纯函数结果写进 state。
- **不做自动修复算法**（be-11 / be-10）。`autofix` 节点只负责「调一轮」，补丁怎么算不在这里。
- **不做 HOCON 渲染**（be-10）。`generate_hocon` 节点只拼 `RenderContext`、调用渲染器、落错误。
- **不做执行与运行监督**（be-14 / be-16 / be-18）。`execution_run` / `execution_metric` / `quality_report` 三张表本模块**只读**，无写路径；state 里**没有任何 `execution_*` 字段**。
- **不做 Prepare / 审批**（be-13）。转移表里登记了 `version_frozen --prepare--> preparing` 一行，但**本模块不驱动它**。
- **不做硬中断**：`abort` 是「用户点取消」，不是 M4 的运行期硬中断——不产生证据事件、不改已冻结的制品（`generation.py:187`）。

---

## 2. 分层落点

| 层 | 文件 | 状态 |
|---|---|---|
| `workflow/` | `state.py` `EtlWorkflowState` / `StudioPhase` / `SNAPSHOT_KEYS` / 序列化守卫 / `input_fingerprint` / `already_done` / `thread_id_for` / `phase_for` / `snapshot` | 📄 已有 |
| `workflow/` | `graph.py` 装配 + 3 个路由函数 + `_guarded_node` + `generation_lock` + `TurnResult` + `start` / `resume` / `snapshot` / `abort` | 📄 已有 |
| `workflow/` | `transitions.py` `EVENTS` / `TRANSITIONS` / `can_transition` / `assert_transition` / `status_after_gate` | 📄 已有 |
| `workflow/` | `interrupts.py` `interrupt_payload` / `parse_interrupt` / `question_for` / `parse_resume_input` / `merge_resume_params` | 📄 已有 |
| `workflow/` | `checkpointer.py` `to_psycopg_dsn` / `CheckpointerHolder` / `open` / `get` / `close` | 📄 已有 |
| `workflow/nodes/` | `__init__.py` `NODE_OUTPUTS` / `RuntimeMissingError` / `NodeRuntime` / `runtime_of` / `llm_provider` | 📄 已有 |
| `workflow/nodes/` | `intent_parse.py` / `param_check.py` / `clarify.py` / `metadata_bind.py` / `generate_plan.py` / `generate_hocon.py` / `gate.py` / `autofix.py` / `freeze.py` | 📄 已有（9 个节点） |
| `services/pipeline/` | `generation.py` 4 个入口 + `_initial_state` / `_persist_turn` / `_graph` | 📄 已有 |
| `services/pipeline/` | `repository.py` 本轮用到的读/写（`get_or_create_candidate` 的候选唯一不变式等） | 📄 已有 |
| `services/pipeline/` | `parts.py` 指纹拼法（与 `artifacts`、`gating` 共用唯一实现） | 📄 已有 |
| `schemas/` | `pipeline.py` `WorkflowSnapshot` / `StartGenerationRequest` / `AnswersRequest` / `AbortRequest` / `ClarificationQuestion` | 📄 已有 |
| `api/` | `pipelines.py` 4 条生成路由 + 2 条列表/详情路由 | 📄 已有 |
| `services/pipeline/` | `orchestrator.py` / `conversation.py` | 🟡 未建（拆到了 `generation.py` / `repository.py`，见 §10.19） |
| `api/` | `pipelines/` 包（多文件） | 🟡 未建（实际是单文件 `api/pipelines.py`） |

**契约以 `frontend/src/types/pipeline.ts` 为准**：`WorkflowSnapshot` 的 6 个字段（`thread_id / phase / phase_message / pending_questions / autofix_round / last_error`）是前端 Studio 已经渲染的形状，后端改名不报错、只会让界面静默显示空白（`schemas/pipeline.py` 模块 docstring）。

---

## 3. 涉及的表

| 表 | 动作 | 关键约束 |
|---|---|---|
| `pipeline` | 读 `status` / 数据源 id / `current_version_id`；写 `status` | 见字典 §3；查询强制带 `tenant_id` + `project_id` |
| `pipeline_conversation` | 写（发起时一条 user 消息）/ 读 | 见字典 §8（支撑表）；只存「说了什么」，**不含**任何进度/制品字段 |
| `langgraph_checkpoint*`（4 张） | 读写（**经 `langgraph-checkpoint-postgres`**） | 见字典 §8；`thread_id = str(pipeline_id)`；**禁止自建表、禁止进程内字典** |
| `metadata_profile` | 读 | 见字典 §2；须 `status='ready'` 且未过期，否则中断在 `metadata_bind` |
| `idempotency_key` | 读写（**经中间件**） | 见字典 §8；唯一约束兜底（原子性） |
| `etl_plan` / `hocon_artifact` / `quality_contract` / `runtime_supervision_contract` / `pipeline_version` | 写（候选行，见 §5.7 与 §10.15） | 见字典 §3；全部 `__immutable__`；候选 `pipeline_version.frozen_at IS NULL` |
| `evidence_event` | 写（**仅经** `harness/ledger.EvidenceLedger.append`） | 见字典 §7；生成成功写一条 `PIPELINE_GENERATED`（见 §5.8） |

**没有**任何写 `execution_*` 的路径——三层状态隔离靠「本模块对该批表无写路径」这一条硬事实保证（PRD §8.1）。

---

## 4. 对外契约

### 4.1 HTTP

本模块的路由注册在 `api/pipelines.py`（`APIRouter(prefix="/pipelines")`，见 `api/pipelines.py:40`）。PRD §9.2 只给 API 分组、不给具体路径，故下表路径以**代码装饰器**为准。

| 方法 | 路径 | 说明 | 出处 |
|---|---|---|---|
| POST | `/api/v1/pipelines/generations` | 发起生成；`project_id` 在 body（写目标，要进幂等指纹） | `api/pipelines.py:102` |
| POST | `/api/v1/pipelines/generations/{thread_id}/answers` | 提交澄清回答；线上形态 `{"answers": {key: value}}` | `api/pipelines.py:129` |
| GET | `/api/v1/pipelines/generations/{thread_id}` | 生成快照（断线恢复，**只读 checkpoint，不跑图**） | `api/pipelines.py:155` |
| POST | `/api/v1/pipelines/generations/{thread_id}/abort` | 用户主动取消 | `api/pipelines.py:169` |

回报：响应体一律是 `ApiResponse[WorkflowSnapshot]`，**每一个**生成相关接口都带 `thread_id`（刷新后前端只能靠它把会话捞回来）。`abort` 的 `reason` 当前被 service 丢弃（`schemas/pipeline.py:173`）。

同文件另注册 `GET /pipelines`（列表）与 `GET /pipelines/{pipeline_id}`（详情）与 `GET /pipelines/{pipeline_id}/versions`，但**归属 be-12 的制品服务**，不属本模块（路由注册顺序在 `test_pipeline_api.py::test_生成会话路由排在动态段之前` 里被守着）。

### 4.2 Python（跨模块契约）

```python
# workflow/state.py
class EtlWorkflowState(TypedDict, total=False): ...
SNAPSHOT_KEYS: frozenset[str]          # 恰好 6 个，等于前端 WorkflowSnapshot
def thread_id_for(pipeline_id: int) -> str: ...
def input_fingerprint(**parts: Any) -> str: ...
def already_done(state, node, fingerprint, *required_fields) -> bool: ...
def snapshot(state: EtlWorkflowState) -> dict[str, Any]: ...

# workflow/graph.py
def build_graph(checkpointer: Any) -> Any: ...      # 无默认值，见 §5.2
async def start(graph, thread_id, *, session, ctx, ...) -> TurnResult: ...
async def resume(graph, thread_id, user_input, *, session, ctx, pipeline_id) -> TurnResult: ...
async def snapshot(graph, thread_id) -> dict[str, Any]: ...
async def abort(graph, thread_id, *, reason: str = "") -> TurnResult: ...

# workflow/transitions.py
def assert_transition(current: PipelineStatus, event: StatusEvent) -> PipelineStatus: ...
def status_after_gate(verdict: Literal["pass", "warn", "block"]) -> PipelineStatus: ...

# services/pipeline/generation.py（api / tasks 的入口）
async def start_generation(*, session, ctx, project_id, requirement, source_data_source_id, target_data_source_id) -> WorkflowSnapshot: ...
async def resume_generation(*, session, ctx, thread_id, answers) -> WorkflowSnapshot: ...
async def get_generation(*, session, ctx, thread_id) -> WorkflowSnapshot: ...
async def abort_generation(*, session, ctx, thread_id) -> WorkflowSnapshot: ...
```

### 4.3 状态机的状态

`pipeline.status` 是 `PipelineStatus`（16 个取值，`models/enums.py:78`）。本模块**拥有出边**的只有 4 个：`draft` / `generated` / `gate_failed` / `version_frozen`。其余 12 个在 `transitions.OUT_OF_SCOPE_STATES` 里显式登记为「本模块无出边」。两者的并集**必须恰好等于** `PipelineStatus` 全量成员——枚举今后加值会让 `test_every_status_is_either_owned_or_out_of_scope` 变红，而不是静默多出一个「谁都能改」的状态。

---

## 5. 核心逻辑

### 5.1 三层状态隔离（PRD §8.1 的落地）

| 层 | 载体 | 本模块的角色 | 隔离手段 |
|---|---|---|---|
| Conversation State | `pipeline_conversation` | 只在发起生成时写一条 user 消息 | 表里**没有**进度/制品列（`models/pipeline.py:75`） |
| Workflow State | `langgraph_checkpoint*`（PG） | 本模块的所有权对象 | state 里**没有** `execution_*` 字段；`assert_no_execution_keys` 在 `snapshot()` 里把执行键挡下 |
| Execution State | `execution_run` / `execution_metric` / `quality_report` | **只读、无写路径** | 转移表对这 12 个状态没有出边，天然拒绝对它们的转移 |

### 5.2 图结构（实际落地）

```
START → intent_parse → param_check
param_check --缺失--> clarify --(resume)--> param_check   # 缺失就再问，不缺才放行
param_check --齐全--> metadata_bind → generate_plan
generate_plan --轮次用尽且结构仍非法--> terminal
generate_plan --否则--> generate_hocon → gate
gate --pass/warn--> freeze → END
gate --policy 阻塞，或轮次用尽--> terminal
gate --block 且可修--> autofix → generate_hocon   # 修复后必须重新渲染再重跑门禁
```

- `build_graph(checkpointer)` **没有默认值**：默认 `None` 会让 `interrupt()` 在运行期报「没有 checkpointer」，而那时图已经跑了一半。装配期就要拿到它，让「忘了配检查点」变成启动失败而非用户可见的中断失败。
- 每个节点都经 `_guarded_node` 包装：出口核对 `NODE_OUTPUTS` 白名单 + JSON 可序列化，见 §5.6。
- 节点清单与 `NODE_OUTPUTS` 在装配期对账，缺声明即 `ValueError`。

**与设计图的两处偏离**（E 分支与自动冻结）见 §5.3、§10.14、§10.16。

### 5.3 路由函数是纯函数，偏离之处写进 docstring

| 路由 | 返回值 | 判据 |
|---|---|---|
| `route_after_param_check` | `clarify` / `metadata_bind` | `missing_params` 非空 |
| `route_after_plan` | `terminal` / `generate_hocon` | 有 `plan_errors` 且 `autofix_rounds >= LLM_AUTOFIX_MAX_ROUNDS` → 终局；**否则一律去渲染**（偏离设计，见 §10.14） |
| `route_after_gate` | `freeze` / `autofix` / `terminal` | 结论非 `block` → **`freeze`**；有 severity=block 的 `policy` finding → 终局；轮次用尽 → 终局；否则 `autofix`（见 §5.4、§10.16） |

`route_after_gate` 里「策略阻塞不修」写在**路由**而不是节点里，是为了让「它压根没进修复」这件事在图结构上可见：P0/P1 是治理判定，模型改不掉，让它改只是把「策略拒绝」伪装成「模型没修好」，还白烧三轮 LLM 的钱。

### 5.4 事务边界、并发与幂等

- **事务边界归 service**：`core.db.get_session` 只借连接、**不提交**。`start_generation` / `resume_generation` / `abort_generation` 末尾各有一行 `await session.commit()`，位置在**所有写入之后、构造响应之前**（`generation.py:129` / `:169` / `:210`）。缺这一行的症状是「图跑完了、`pipeline` 表 0 行」——因为 LangGraph 的 checkpoint **自己提交**，`checkpoints` 表记录齐全、看起来状态没丢，下一个请求却报「生成会话不存在」。守它的是 `tests/test_service_commit_boundary.py`（扫源码，不只是断言这一次）。
- **图执行在事务之外**：`_persist_turn` 落库在 service，图（含 LLM 调用）在事务之外跑，避免长事务持锁（设计 §5.6.4）。
- **生成锁**：`SET gen:lock:{pipeline_id} <token> NX EX 600`（`GEN_LOCK_TTL_SEC = 600`）；释放用 Lua 比较并删除，避免误删他人锁；取不到锁抛 `GenerationInProgressError`（`CONFLICT` 409，"该 Pipeline 正在生成中"），**不等待、不排队**。
- **节点幂等**：`already_done(state, node, fingerprint, *required_fields)`——目标产物在 state 里**且**输入指纹未变则 no-op。`metadata_bind` 因此在 resume 后不会重新探查；`generate_plan` 在已有合法草稿时不会重复调 LLM。整个判据只认「结果 + 指纹」，不认「跑过没」。
- **请求幂等**：`Idempotency-Key` 由 `api/idempotency.py` 的中间件处理，`scope = f"{method} {path}"`，同 key 同 `request_fingerprint` → 回放 `response_snapshot`；同 key 不同指纹 → `IDEMPOTENCY_CONFLICT` 409（偏离设计，见 §10.17）。
- **重复 resume**：无 `Idempotency-Key` 的重复请求由生成锁兜住（同一时刻只有一次在图里跑）。

### 5.5 中断与恢复

- 触发：`find_missing_params` 返回非空（缺主键 / 增量字段 / 去重规则 / 目标表 / 质量阈值任一）。
- `clarify` 节点调 `interrupt(payload)`；`payload = {"reason": "missing_params", "questions": [...]}`（`interrupts.py:53`）。中断点之后的节点不执行，检查点已落 PG。
- 恢复：`graph.ainvoke(Command(resume=user_input), config)`。`parse_resume_input`（`interrupts.py:125`）**兼容两种形状**——be-09 §5.3 的 `{"content", "params"}` 与前端 `{"answers": {key: value}}`；`merge_resume_params` 保证新一轮答案**不擦掉**旧答案。
- **两种问题形状各司其职**（`state.py` docstring）：图内 `interrupt()` 载荷用后端契约形状（`param/prompt`），快照 `pending_questions` 用前端形状（`key/question/kind/...`）；两者由同一份 `MissingParam` 单向派生，不会漂移。

### 5.6 state 的两个静默通道（§10.18）

1. **入口**：`StateGraph(EtlWorkflowState)` 只把 schema 里声明的键建成 channel，service 注入的**未声明键整体消失**，**不报错**。症状是节点读到空串，报出来的却是业务语义错误（「需求描述不能为空」），排查方向被带到「前端没传」上。
2. **出口**：节点回写 `NODE_OUTPUTS` 白名单外的键会覆盖别人。出口有装配期（节点清单对账）+ 运行期（`_guarded_node`）两道守卫。

**加一个 state 键 = 三处一起改**。守入口的是「样例覆盖每个声明键」的用例（`test_state_sample_covers_every_declared_key`）+ `test_orchestrator_injected_keys_are_all_declared_in_the_state_schema` + `test_undeclared_input_keys_are_dropped_not_silently_used`。

### 5.7 草稿与落库的界线

草稿（`draft_plan` / `draft_hocon`）只活在 Workflow State 里；**service 在一轮结束时把这一轮有的东西整体落一次**（`_persist_turn`，`generation.py:381`）。落库这一步在 service 而不在节点里的理由：中断（澄清）是**正常路径**，图会在 `param_check` 处停下等人回答；若落库写在节点里，用户看到澄清问卷时库里有可能是「有 conversations、没有 plan」这种半截状态。

三个「不落」的分支：① 还没出方案（停在澄清）→ 只推进 `pipeline.status`；② Plan 结构没过 → **仍然落**，`valid=False` 且带问题清单（不落的话前端看到「方案为空」而真实是「有 3 处问题」）；③ 本轮已冻结（`frozen_version_id` 在）→ 只推状态，**不重复插制品**。

### 5.8 证据账本

生成成功写一条 `PIPELINE_GENERATED`（`models/enums.py:265`），经 `harness.ledger.EvidenceLedger.append` **单入口**写入。payload 只放**引用与元数据**（模型名/版本、提示词版本、修复轮次、档案 id、`plan_issue_count`），**不放需求原文、不放任何样本**——账本永久保留。账本写失败 → 本次生成失败（`INTERNAL_ERROR` 500）：生成可以重来，漏掉的那条事件永远补不回来。

### 5.9 DSN 归一与密码不外泄

`to_psycopg_dsn`（`checkpointer.py:66`）把 SQLAlchemy 的 `postgresql+asyncpg://...` 归一成 psycopg 能吃的形式（LangGraph 的 checkpointer 用同步 psycopg）。`CheckpointerHolder` 的 `dsn` 属性**不泄漏口令**（`test_checkpointer_holder_does_not_leak_the_password_in_its_dsn_property`）：日志与异常里只出现脱敏后的连接串。

---

## 6. 错误码

| # | 触发条件 | 错误码 | HTTP | 出处 |
|---|---|---|---|---|
| 1 | 同一 Pipeline 正在生成（生成锁被占） | `CONFLICT` | 409 | `graph.py:97` |
| 2 | 非法状态转移（如 `gate_failed` 下请求冻结） | `CONFLICT` | 409 | `transitions.py` `assert_transition` |
| 3 | 同 `Idempotency-Key` 不同请求指纹 | `IDEMPOTENCY_CONFLICT` | 409 | 中间件 |
| 4 | `thread_id` / `pipeline` 查不到 | `NOT_FOUND` | 404 | `generation._pipeline_of_thread` |
| 5 | 调用者在本项目无相应职责槽（写=maker/operator，读=任一槽） | `ROLE_REQUIRED` | 403 | `artifacts.require_roles` |
| 6 | service 注入的上下文缺失/形状不对（`source_profile` / `budget` / `data_source_types`） | `VALIDATION_ERROR` | 422 | `nodes/generate_plan.py` |
| 7 | 尚无 EtlPlan 却要求渲染 HOCON | `PLAN_INVALID` | 422 | `nodes/generate_hocon.py` |
| 8 | state 含不可序列化值 / 节点回写未声明键 | `INTERNAL_ERROR` | 500 | `state.py:314` `StateNotSerializableError` |

错误码全部取自 `core/errors.py` 既有枚举，本模块**未新增**任何码（`ROLE_REQUIRED` / `NOT_FOUND` / `CONFLICT` / `IDEMPOTENCY_CONFLICT` / `VALIDATION_ERROR` / `PLAN_INVALID` / `INTERNAL_ERROR`）。

---

## 7. 异步任务

- **本模块不注册 Celery 任务**：生成链路是**同步 HTTP + 长请求**（图跑在请求里），不走 Celery。执行阶段（SeaTunnel 作业投递、Outbox 派发）才是 Celery 的地界，属 be-14。
- 图是纯异步（`graph.ainvoke`）；Worker 侧若将来要跑图，必须走 `tasks/async_runner.run_coro`（CLAUDE.md §9.1：任务里禁止裸 `asyncio.run`，模块级单例会跨循环失效）。
- Checkpointer 用**同步** psycopg（`CheckpointerHolder`），与 `core.db` 的 asyncpg 引擎是两套连接——这也是 `to_psycopg_dsn` 存在的理由。

---

## 8. 验收标准

对应 PRD §6.3.1 / §8.1 / §8.3：

| 验收点 | 判据 | PRD |
|---|---|---|
| 缺必需参数必中断提问 | `param_check --缺失--> clarify` 路由 + `test_clarify_keeps_asking_while_params_are_still_missing` | §6.3.1 |
| 恢复不重复安全探查 | `test_metadata_bind_binds_once_and_never_re_probes_on_resume` | §6.3.1 |
| Checkpoint 真落 PG | Checkpointer 是 `langgraph-checkpoint-postgres`，`build_graph` 不接受 `None` | §8.1 |
| 三层隔离（执行状态不被对话状态覆盖） | `test_execution_keys_are_rejected` / `test_snapshot_never_leaks_state_beyond_whitelist` | §8.1 |
| 快照字段 = 前端契约 | `test_snapshot_keys_exactly_equal_frontend_contract` | §8.1 |
| 非法转移不改状态 | `test_illegal_transitions_raise_conflict`（断言 `pipeline.status` 不变） | §8.1 |
| 防重放 | 生成锁 + `Idempotency-Key` 中间件 + 节点幂等（§5.4） | §8.3 |
| 事务不留半截 | `test_service_写入口都自己提交事务` | §8.1 |

**没有**独立的 PRD 数字指标归本模块——它提供的是 be-11/be-12 端到端验收的骨架。本轮实测：`tests/test_workflow_state.py` 107 passed、`tests/test_workflow_graph.py` 57 passed、`tests/test_service_commit_boundary.py` 9 passed、`tests/test_pipeline_api.py` 20 passed。

---

## 9. 测试数据

| 文件 | 条数 | 覆盖 |
|---|---|---|
| `tests/test_workflow_state.py` | 107 | state 全键覆盖、JSON 可序列化、执行键拒绝、快照白名单与形状、`phase_for` 映射、转移表（合法/非法/只读）、`status_after_gate`、中断载荷往返、`parse_resume_input` 两种形状、`find_missing_params` 顺序与回落、DSN 归一、checkpointer 口令不外泄 |
| `tests/test_workflow_graph.py` | 57 | 节点/边齐全、`build_graph` 需 checkpointer、`NODE_OUTPUTS` 声明完整、守卫拒未声明键/非 dict/不可序列化、三个路由函数、注入键都已在 schema 声明、未声明输入键被丢弃而非静默使用、生成锁（NX EX / 只删自己的 token / 409 / 释放失败不掩盖本体错误）、`metadata_bind` 幂等与过期拦截、`generate_plan` 缺注入即 422 且不调模型 |
| `tests/test_service_commit_boundary.py` | 9 | 扫 `services/` 源码：凡会写的公开异步入口必须含 `commit()`；元测试自证守卫能抓人；写 helper 名单未过期 |
| `tests/test_pipeline_api.py` | 20 | 14 条前端函数的路径/方法逐条核对、路由注册顺序（diff 在 `{version_id}` 前、`generations` 在动态段前）、门禁四型直接复用引擎定义 |

关键用例（按事故来历命名）：

| 用例 | 守的是哪条坑 |
|---|---|
| `test_undeclared_input_keys_are_dropped_not_silently_used` | 入口静默通道（§5.6） |
| `test_orchestrator_injected_keys_are_all_declared_in_the_state_schema` | 加键必须三处一起改 |
| `test_service_写入口都自己提交事务` | 「图跑完了、库里是空的」 |
| `test_snapshot_keys_exactly_equal_frontend_contract` | 快照改字段会让前端静默空白 |
| `test_to_psycopg_dsn_normalizes_three_input_forms` | Windows 下 ProactorEventLoop / DSN 形态不一致 |

---

## 10. 待确认与偏离登记

> 编号接全局清单（M2-01 §10 用了 1–6，M2-02 §10 用了 7–13）。本 spec 从 **14** 起。

| # | 事项 | 建议 | 验证方式 |
|---|---|---|---|
| **14** | 🔴 **`E --结构非法--> H`（直接进自动修复）改成「先渲染、由门禁出结论再进修复」**：设计 §5.1 画的是 `generate_plan --结构非法且可修--> autofix`，实现改为一律先去 `generate_hocon` → `gate`，由 `gate --block--> autofix`。理由写在 `workflow/graph.py::route_after_plan` 的 docstring：`autofix` 只吃 `GateResult.findings`，而结构校验产出的是 `PlanIssue`，两者形状不同；直接进修复就得再造一套 `PlanIssue → GateFinding` 映射，那份映射必然与门禁自己的产出口径漂移，于是同一处问题在两条路径上有两个 code，去抖与 `resolved_finding_ids` 全会失准。渲染不会因结构问题失败（渲染异常被 `generate_hocon` 接住落成 `hocon_errors`），所以这条路径永远走得通 | 保留现状。修复回路的输入只有一种形状（`GateFinding`） | `graph.py:159` 的 docstring；`test_route_after_plan` |
| **15** | 🔴 **门禁 pass/warn 后图内自动冻结**：`route_after_gate` 结论非 `block` → `freeze`，而 be-12 描述的是「用户点冻结」（`POST /pipeline-versions/{id}/freeze`）。本模块把冻结放进了图的正常路径 | 保留。M2 的端到端目标是「需求 → 冻结」一次跑通；`freeze` 节点调的是 be-12 的能力（`services/pipeline/artifacts.freeze`），语义没变，只是触发者从「人」变成「门禁通过」。**代价**：用户失去了「看一眼再冻」的机会——若产品要保留人工冻结，需把 `route_after_gate` 的 `freeze` 分支改成回到 `generated` 并等人工调用 | `route_after_gate` 的目标集合；`test_router_outputs_all_have_a_target_in_the_compiled_graph` |
| **16** | **`freeze` 与 `_persist_turn` 存在双重落库风险，已用 `frozen_version_id` 短路**：`freeze` 节点自己落过制品并冻结候选行，若 `_persist_turn` 再走一遍 `save_artifacts` 会插出**第二组**不可变行，且因候选已被冻结、`get_or_create_candidate` 查不到候选行会再新建一个空候选。症状是「冻结一次，版本列表多一个没有制品的候选版本」。修法是 `_persist_turn` 开头判 `values.get("frozen_version_id")` 即只推状态、直接 return（`generation.py:411`） | 保留短路。**根因是「冻结」被两条路径触发**（图内 freeze + service 落库），彻底解法是让 `freeze` 不再由 service 落库路径覆盖，把落库职责收敛到一处 | 造一次「门禁通过 → 冻结」，断言 `pipeline_version` 只有一行、制品只有一组 |
| **17** | **对话幂等改为中间件统一处理，`scope="pipeline.message"` 未落实**：设计 §4/§5.6 要求 `Idempotency-Key` 落 `scope="pipeline.message"`、并要求 `pipeline_conversation.meta.request_id` 去重跳过重复 `ainvoke`。实现用通用中间件（`scope = f"{method} {path}"`），且**没有** `meta.request_id` 去重。重复 resume 目前只由生成锁兜住 | 保留中间件方案（`scope` 用方法+路径比自定义字符串更能隔离不同接口），但**去掉重复 resume 的语义空洞**：要么补 `meta.request_id` 去重，要么在文档里把「重复 resume 由生成锁兜」写成契约 | `api/idempotency.py:88` 的 `scope`；造两次无 key 的相同 resume，断言第二次不重复推进 |
| **18** | 🔴 **`session.commit()` 漏写的坑（硬约束，已固化为用例）**：`core.db.get_session` 只借连接、不提交，事务边界归 service。LangGraph checkpoint **自己提交**，所以漏 `commit()` 时 `checkpoints` 表完整、`pipeline` 表 0 行，报出来的却是下一个请求「生成会话不存在」。修法是给 5 个写入口各补一行，但「补一行」治不了本——`tests/test_service_commit_boundary.py` 改成**扫源码**：凡会写的 service 公开异步入口必须出现 `commit()`，`repository.py` 整模块豁免（它只执行语句） | 保留。任何新写入口都必须自己提交，位置在**所有写入之后、构造响应之前** | 该测试文件本身；`generation.py:129/169/210` |
| **19** | **文件结构偏离设计 §2**：设计列了 `services/pipeline/orchestrator.py`（凑 state）与 `conversation.py`（对话写入），实现把它们合进了 `generation.py` 与 `repository.py`；设计列了 `api/pipelines/` 包，实现是单文件 `api/pipelines.py` | 保留现状。少两个文件、少一层间接；代价是 `generation.py` 承担了「建流程 + 凑 state + 驱动 + 落库」四件事，将来拆执行阶段时再分 | 目录清单 |
| **20** | 🟡 **state 比设计 §5.2 的字段清单多 16 个字段**（都必需）：`source_data_source_id` / `target_data_source_id` / `intent` / `target_table_hint` / `pending_questions` / `node_fingerprints` / `hocon_meta` / `data_source_types` / `aborted` / `source_profile` / `target_profile` / `budget` / `requirement` / `thread_id` / `started_at` / `profile_summaries`。通用判据：**凡是「engines 不许查、service 又没给」的外部事实，都必须是入图注入** | 保留。已由 `test_state_sample_covers_every_declared_key` 与 `test_orchestrator_injected_keys_are_all_declared_in_the_state_schema` 守住「声明与注入一致」 | `workflow/state.py` 的字段注释；两条用例 |
| **21** | 🟡 **`generation_context` 多一个键导致「生成成功、答题 422」**：`generate_plan` 节点一度手拼该 dict 且多拼了 `plan_issue_count`，这份 dict 原样写进 `pipeline_version.generation_context`、读回时过 API DTO（`extra="forbid"`），于是报错指向用户提交的答案（「Extra inputs are not permitted」），真因却在拼 dict 那一行。修法是**用 be-08 的 `GenerationContext` 模型构造**（`nodes/generate_plan.py:298`），审计计数照旧进账本 payload | 保留。这类跨模块 JSON 列的形状**只有一份定义**，消费方对未知键取 `extra="ignore"`；写的一侧多一个键，报在读的一侧 | `tests/test_dto_version_widths.py::test_存进_generation_context_的东西能被_api_dto_读回来`；`nodes/generate_plan.py:298` 的 docstring |

---

## 11. 变更记录

| 日期 | 变更 | 原因 |
|---|---|---|
| 2026-10-16 | 初版：be-09 state 单一定义 / 图 / 中断恢复 / 生成锁 / 转移表 / 快照 / generation service 全部落地后补写 | M2 生成链路的骨架；偏离登记见 §10（14–21） |
