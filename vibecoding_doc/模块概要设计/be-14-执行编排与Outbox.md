# be-14 执行编排与 Outbox

> 上游：[概要设计总纲.md](../概要设计总纲.md) §2 进程拓扑、§4 主流程时序、§5 Harness 安全执行内核 ｜ [PRD.md](../PRD.md) §6.4.3 Commit、§6.5.1 任务执行、§8.3 防重放、§12 关键异常
> 数据：[核心数据字典.md](../核心数据字典.md) §5 执行与质量、§7 证据与投递、§8 `idempotency_key`
> 代码位置：`backend/harness/outbox.py`、`backend/tasks/`、`backend/api/runs.py`、`backend/services/run.py` ｜ 里程碑：M3 ｜ 依赖：be-05 Harness 内核、be-06 证据账本、be-13 准备单与审批

## 1. 职责与边界

- **做什么**
  - 实现 Commit：在一个数据库事务内完成「校验 → 签发 Capability → `INSERT execution_run` → Outbox 入队」。
  - 实现 `harness.TransactionalOutbox`：`enqueue(session, topic, payload)` 只写库，绝不 `apply_async`。
  - 实现 Outbox 投递器（beat 每 2 s `FOR UPDATE SKIP LOCKED` 扫描）+ 指数退避 + 超限转 `dead`。
  - 定义 topic 清单与各自的消费任务映射，配置 Celery（broker/result 分 DB、`acks_late`、prefetch=1、时限、重试）。
  - 实现 Worker 消费时序：幂等键检查 → `ToolBroker.execute()` → 回写 `execution_run.seatunnel_job_id` / `status`。
  - 维护 `execution_run` 状态机与「同一 pipeline_version 仅一条活跃 run」的并发约束。
  - 提供操作员动作路由（pause / cancel / rollback）与运行查询（含 auditor 只读）。
  - 保留策略、备份、账本检查点补打的周期任务。
- **不做什么**
  - **不实现副作用本身**：submit / stop / swap / rollback_cleanup 的实现体在 `clients/`（be-15、be-18），本模块只经 `ToolBroker` 调用。
  - 不实现 Capability 的签发与验签算法（be-05），不实现账本链计算（be-06）。
  - 不实现审批逻辑（be-13），只调用 `assert_committable`。
  - 不实现质量判定（be-17）与监督判定（be-16）；本模块负责它们的调度入口。
  - 不做业务分支：`tasks/` 只编排，判定与事务下沉 `services/`。
- **所在分层**：`harness/outbox.py`（最低层，禁用 `services`/`api` 依赖）→ `services/run.py`（事务边界、编排）→ `api/runs.py`（转发）；`tasks/*.py` 跨切编排，全部经 `services/` 或 `harness/` 落地。

## 2. 模块结构

```
backend/
├── harness/
│   └── outbox.py            # （待建）TransactionalOutbox.enqueue / Topic / next_available_at / BACKOFF
├── services/
│   └── run.py               # （待建）commit / apply_transition / request_pause|cancel|rollback / list_runs
├── api/
│   └── runs.py              # （待建）Commit 与运行中心路由
└── tasks/
    ├── celery_app.py        # 已存在：应用与 beat_schedule（本模块补 beat 条目与队列路由）
    ├── outbox.py            # 已存在：scan_and_dispatch（当前骨架）
    ├── execution.py         # 已存在：submit_run / stop_run / publish_swap / rollback_run（当前骨架）
    ├── supervision.py       # 已存在：collect_active_runs / diagnose_run（be-16 拥有）
    └── maintenance.py       # 已存在：enforce_retention / checkpoint_ledger（本模块补 backup_database）
```

## 3. 对外接口

### 3.1 HTTP（`api/runs.py`，前缀 `/api/v1/runs`）

| 方法 | 路径 | 权限（角色） | 幂等 | 请求与响应要点 |
|---|---|---|---|---|
| POST | `/api/v1/runs` | `operator` | 必需 `Idempotency-Key` | Commit：body `{preparation_id}`；响应 `RunDetail`；重复同 key 返回首次结果（本路由由 `api/runs.py` 转发 `services/run.commit`） |
| GET | `/api/v1/runs` | 项目成员；`auditor` 只读 | 只读 | 参数 `status?, pipeline_id?, pipeline_version_id?, page, page_size`；响应 `Page[RunSummary]` |
| GET | `/api/v1/runs/{run_id}` | 项目成员；`auditor` 只读 | 只读 | 响应 `RunDetail`（含汇总指标、`failure_snapshot` 脱敏摘要、`seatunnel_job_id`；诊断字段归 be-19、质量字段归 be-17） |
| POST | `/api/v1/runs/{run_id}/pause` | `operator` | 必需 | 停推后续阶段（不调 SeaTunnel）；`running → paused` |
| POST | `/api/v1/runs/{run_id}/cancel` | `operator` | 必需 | 经 `seatunnel.stop`；`running|paused → cancelled`；重复调用返回当前状态 |
| POST | `/api/v1/runs/{run_id}/rollback` | `operator` | 必需 | **转发** be-18 的 `services/publish.request_rollback(...)`（编排与 `target.rollback_cleanup` 归 be-18）；`failed|interrupted|succeeded? → rolling_back → rolled_back` |

`auditor` 的只读由 `api/` 层的角色依赖统一实施：`auditor` 通过 `GET`，对任何 `POST` 返回 403 `ROLE_REQUIRED`。

### 3.2 Python（跨模块调用）

```python
# harness/outbox.py —— 唯一允许被服务层调用的副作用入队入口
class Topic(StrEnum):
    SEATUNNEL_SUBMIT = "etl.seatunnel.submit"      # 消费：tasks.execution.submit_run
    RUN_SUPERVISE = "etl.run.supervise"            # 消费：tasks.supervision.supervise_run
    RUN_STOP = "etl.run.stop"                      # 消费：tasks.execution.stop_run
    TARGET_PUBLISH_SWAP = "etl.target.publish_swap"  # 消费：tasks.execution.publish_swap（be-17 投递）
    TARGET_ROLLBACK = "etl.target.rollback"        # 消费：tasks.execution.rollback_run（be-18 投递）
    BENCHMARK_RUN = "etl.benchmark.run"            # M4：消费 tasks.benchmark.execute_run（be-20）

@dataclass(frozen=True, slots=True)
class OutboxHandle:
    id: int
    topic: str
    idempotency_key: str

async def enqueue(
    session: AsyncSession,
    *,
    topic: Topic | str,
    payload: Mapping[str, Any],
    idempotency_key: str,
    available_at: datetime | None = None,
) -> OutboxHandle:
    """与业务事实**同事务**写入 outbox_message；不提交、不投递、不 apply_async。"""

def next_available_at(attempts: int, *, now: datetime) -> datetime:
    """纯函数：退避 = min(2 ** attempts, 300) 秒。attempts 为**即将累加后**的值。"""

def assert_payload_has_no_secret(payload: Mapping[str, Any]) -> None:
    """键名/字符串值双重扫描（password|secret|key_value|dsn|conn...）；命中即 raise。
    payload 只允许放**引用与标量**，唯一例外是 `capability` 键（见 §5.1 第 ⑦ 步）。"""

# services/run.py
async def commit(
    session: AsyncSession, *, preparation_id: int, subject: SubjectContext, idempotency_key: str
) -> RunDetail: ...   # 事务边界见 §5.1（单一事务内完成校验/签发/建 run/入队）

async def apply_transition(
    session: AsyncSession, *, run_id: int, target: RunStatus, reason: str, actor_id: int | None
) -> RunSummary: ...   # 状态机唯一写入口；非法转移 → RUN_CONFLICT

async def request_pause(session: AsyncSession, *, run_id: int, subject: SubjectContext) -> RunSummary: ...
async def request_cancel(session: AsyncSession, *, run_id: int, subject: SubjectContext) -> RunSummary: ...
# 回滚的编排在 be-18（services/publish.request_rollback）；api/runs.py 的同名路由是**转发**，不在本模块重复实现
```

`RunStatus` 的合法转移表以 `models/enums.py:RunStatus` 与 `active_states()` 为唯一事实来源，本模块不另抄取值。

## 4. 数据模型

| 表 | 读写 | 关键列 | 约束 |
|---|---|---|---|
| `outbox_message` | 写 + 状态 UPDATE | `topic`、`payload`、`status`、`attempts`、`available_at`、`idempotency_key`、`sent_at`、`last_error`、`dispatched` | `uq_outbox_message_idempotency_key`；`ix_outbox_message_status_available_at`；`ix_outbox_message_topic` |
| `execution_run` | 写 + 状态 UPDATE | `pipeline_version_id`、`preparation_id`、`capability_id`、`idempotency_key`、`seatunnel_job_id`、`status`、`shadow_table`、`formal_table`、`error_table`、`started_at`、`finished_at`、`read_rows`/`written_rows`/`written_bytes`/`reject_rows`/`avg_throughput`、`failure_snapshot`、`triggered_by` | `uq_execution_run_idempotency_key`；部分唯一索引 `uq_execution_run_active_per_version`（`WHERE status IN ('queued','running','paused','interrupted','rolling_back')`）；`ix_execution_run_project_id_status` |
| `idempotency_key` | 写 | `scope`、`key`、`request_fingerprint`、`response_snapshot`、`resource_type`、`resource_id`、`completed`、`expires_at` | `uq_idempotency_key_tenant_id_scope_key` |
| 🔒 `capability` | 写（be-05）/ 只读 | `jti`、`tool`、`expires_at`、`consumed_at`、`consumed_by_run_id` | `uq_capability_jti`；`consumed_at` 仅在 `__mutable_columns__` 白名单内且只写一次 |
| `execution_metric` | 写（be-16） | `run_id`、`ts`、累计值列 | `ix_execution_metric_run_id_ts` |
| 🔒 `evidence_event` | 经 be-06 写 | — | 见 §8 |

部分唯一索引在 PG 与 SQLite 下均生效（`postgresql_where` 在 sqlite dialect 被忽略，因此**并发约束的集成测试必须在 PG 上跑**，本机只测状态机分支）：

```sql
CREATE UNIQUE INDEX uq_execution_run_active_per_version
  ON execution_run (pipeline_version_id)
  WHERE status IN ('queued','running','paused','interrupted','rolling_back');
```

## 5. 核心流程

### 5.1 Commit 事务边界（`services/run.py: commit`）

```python
async def commit(session, *, preparation_id, subject, idempotency_key) -> RunDetail:
    async with session.begin():                     # ① 单事务：下面是全部写入
        # ② 幂等登记（同 key 直接返回 response_snapshot）
        await claim_idempotency(session, scope="run.commit", key=idempotency_key, ...)
        # ③ Commit 前置校验（be-13），只读
        facts = await assert_committable(session, preparation_id=preparation_id)
        # ④ 并发闸门：同版本已有活跃 run → RUN_CONFLICT
        await assert_no_active_run(session, pipeline_version_id=facts.pipeline_version_id)
        # ⑤ 签发 Capability（be-05）：Ed25519，TTL=CAPABILITY_TTL_SEC(300)，落 capability 表
        cap = issuer.issue(preparation_id=facts.preparation_id, subject_id=subject.user_id,
                           tool=ToolName.SEATUNNEL_SUBMIT, env=facts.environment, tenant_id=facts.tenant_id,
                           artifact_sha256=facts.artifact_sha256, project_id=facts.project_id, now=now)
        # ⑥ 创建执行实例
        run = await insert_run(session, facts=facts, capability=cap, subject=subject,
                               idempotency_key=f"run:{idempotency_key}",
                               shadow_table=..., formal_table=..., error_table=...)
        # ⑦ Outbox 入队（同事务）—— 注意是 enqueue，不是 apply_async
        await harness.outbox.enqueue(session, topic=Topic.SEATUNNEL_SUBMIT,
                                     payload={"run_id": run.id, "capability": cap.token},  # 令牌必须随载荷走
                                     idempotency_key=f"submit:run:{run.id}")
        # ⑧ 证据事件（同事务）；occurred_at 由调用方注入，账本内不取当前时间
        await ledger.append(session, LedgerEvent(tenant_id=facts.tenant_id, event_type=CAPABILITY_ISSUED,
            subject_type="capability", subject_id=str(cap.jti), occurred_at=now, project_id=facts.project_id,
            actor_id=subject.user_id, artifact_hash=facts.artifact_sha256,
            payload={"preparation_id": preparation_id, "tool": "seatunnel.submit"}))
        await ledger.append(session, LedgerEvent(...RUN_CREATED, subject_type="run", subject_id=str(run.id)))
    # 事务提交后才返回；投递交给 beat
```

**为什么禁止在事务内 `apply_async`**：Celery 的 `apply_async` 是**事务外的一次网络写入**。若先投递再提交，事务回滚（唯一约束冲突、断言失败、连接断开）会留下一条**已被 Worker 消费的幽灵命令**——命令指向的 `execution_run` 根本不存在，Worker 只能失败并重试到 `dead`，而"撤销已发出的消息"在 Redis broker 上不可靠。Transactional Outbox 把「业务事实」与「待发命令」放进同一个原子提交：提交成功则命令必然最终被投递（至少一次），提交失败则命令根本不存在。这与 `core/db.py` 顶部注释的约定一致。

### 5.2 Outbox 投递器（`tasks/outbox.py: scan_and_dispatch`，beat 每 2 s）

```sql
-- ① 取一批待投递消息（同一事务内）
SELECT id, topic, payload, idempotency_key, attempts
FROM outbox_message
WHERE status = 'pending' AND available_at <= now()
ORDER BY available_at, id
LIMIT :batch_size            -- 默认 50
FOR UPDATE SKIP LOCKED;      -- 多 beat/worker 实例并发安全：每条只被一个扫描者取到

-- ② 成功：置 sent
UPDATE outbox_message SET status='sent', sent_at=now(), attempts=attempts+1, dispatched=true WHERE id=:id;

-- ③ 失败：退避重试
UPDATE outbox_message
   SET status = CASE WHEN attempts + 1 >= :max_attempts THEN 'dead' ELSE 'failed' END,
       attempts = attempts + 1,
       available_at = :next_available_at,     -- next_available_at(attempts+1) = min(2**(n+1), 300) 秒
       last_error = :error_summary            -- 脱敏后的摘要，严禁写连接串/配置全文
 WHERE id = :id;
```

- `OUTBOX_MAX_ATTEMPTS = 5`：`attempts + 1 >= 5` 即转 `dead`（取值见 `OutboxStatus`）。`dead` 只做告警（Prometheus 指标 + 钉钉 webhook），**不自动复活**，需人工介入（§10 第 6 条）。
- 幂等：投递侧 `FOR UPDATE SKIP LOCKED` + `status` 转移；消费侧靠 `outbox_message.idempotency_key` 与 `execution_run.idempotency_key` 两个唯一索引。**至少一次投递是常态**，重复消费必须无副作用（见 §5.4）。
- 单条消息的投递 = 调用消费任务的 `delay()`。`sent` 只表示「命令已入 broker」，不代表业务成功。

**topic 清单**

| topic | 消费者任务 | 队列 | 载体工具（ToolName） | 风险 |
|---|---|---|---|---|
| `etl.seatunnel.submit` | `tasks.execution.submit_run(run_id)` | `etl.execute` | `seatunnel.submit` | P1 |
| `etl.run.stop` | `tasks.execution.stop_run(run_id, reason)` | `etl.execute` | `seatunnel.stop` | P2 |
| `etl.run.supervise` | `tasks.supervision.supervise_run(run_id)` | `etl.default` | 无（只读采集 + 判定） | — |
| `etl.target.publish_swap` | `tasks.execution.publish_swap(run_id)` → `services/publish.publish_run`（be-18） | `etl.execute` | `target.publish_swap` | P0 |
| `etl.target.rollback` | `tasks.execution.rollback_run(run_id, actor_id)` → `services/publish`（be-18） | `etl.execute` | `target.rollback_cleanup` | P1 |

`payload` 只允许 `run_id` / `actor_id` / `reason` / `preparation_id` 这类**引用与标量**；任何 Secret、连接串、HOCON 全文都不进 payload（`assert_payload_has_no_secret` 强制）。**唯一例外**是 `etl.seatunnel.submit` 的 `capability` 键（见 §5.1 第 ⑦ 步）：Capability 令牌不是凭据（无 Secret 值、TTL 300 s、单次消费），它必须随载荷到达 Worker——`capability` 表只存 `token_sha256`，原文无法重建。

### 5.3 Celery 配置（`tasks/celery_app.py`）

| 配置 | 值 | 理由 |
|---|---|---|
| `broker` | `settings.celery_broker_url`（Redis DB 1） | 与缓存库（DB 0）隔离，避免 `FLUSHDB` 误伤缓存 |
| `backend` | `settings.celery_result_backend`（Redis DB 2） | result 有 TTL，和 broker 分开便于单独清理 |
| `task_serializer` / `accept_content` | 仅 `json` | 禁止 pickle：反序列化即远程代码执行 |
| `task_acks_late` | `True` | Worker 崩溃时消息回到队列，不丢任务 |
| `task_reject_on_worker_lost` | `True` | Worker 被 OOM/Kill 时重新投递 |
| `worker_prefetch_multiplier` | `1` | 长任务不预取，避免一个 Worker 攥着 N 条消息饿死其他 Worker |
| `task_time_limit` | `CELERY_TASK_TIME_LIMIT`(7200) | 硬时限略大于监督预算上限，留出清理与证据落库时间 |
| `task_soft_time_limit` | `CELERY_TASK_SOFT_TIME_LIMIT`(6900) | 软时限用于「先把 run 标 failed 再退出」，不留悬挂的 running |
| `task_routes` | `tasks.execution.* → etl.execute`，其余 `etl.default` | 执行队列与调度队列隔离，慢任务不阻塞 beat 触发的扫描 |
| `beat_schedule` 已有 | `scan-outbox`(2 s)、`collect-run-metrics`(30 s)、`retention-cleanup`(6 h) | 见 §5.5 |

任务幂等契约（重投是常态，不是异常）：每个消费任务入口必须先做三条检查，任一命中即**直接返回既有结果、不重复副作用**：

1. `outbox_message.idempotency_key` 是否已 `sent` 且该 key 的消费已完成（查 `idempotency_key` 表 `completed`）。
2. `execution_run.status` 是否已越过目标状态（如 `submit_run` 在 `running`/`succeeded` 时直接返回）。
3. 副作用工具调用**至多一次**：`ToolBroker.execute()` 内部由 ReplayGuard 原子消费 `jti`，重复调用必然 `CAPABILITY_REPLAYED`——因此调用前必须先做第 1、2 条检查，把重投拦在 broker 之前。

`autoretry`：只对**瞬时错误**（网络超时、Vault 暂时不可达、SeaTunnel 5xx）使用 `autoretry_for=(TransientToolError,)`，`retry_backoff=True`、`retry_jitter=True`、`max_retries=3`；**业务拒绝类异常（`PolicyBlockedError` / `FingerprintMismatchError` / `CapabilityReplayedError` / `RUN_CONFLICT`）绝不重试**，直接落 `error_message` 并把 run 标 `failed`。

### 5.4 Worker 消费时序（以 `submit_run` 为例）

```
① 取消息（topic=etl.seatunnel.submit，payload={run_id}）
② 幂等检查：run.status != queued → 直接 return（重投）
③ services.run.load_for_submit(run_id) → SubmitContext（含 run/version/preparation/contract，均为 Pydantic DTO）
④ Secret 物化（be-15）：解析 resource_scope 里的 secret 引用 → Vault 读取 → 临时文件 0600
   ── 失败则**不消费 jti**，异常上抛交给 outbox 退避重试（此时未产生任何外部副作用）
⑤ harness.ToolBroker.execute(ToolName.SEATUNNEL_SUBMIT, token, args, ctx, session=session)
   ├─ CapabilityVerifier.verify(token)：验签 + 过期 + 主体 + 工具 + 环境 + artifact_sha256
   ├─ ReplayGuard.consume(jti)：Redis SETNX + capability.jti 唯一约束 + consumed_at 一次性
   ├─ 工具注册表校验风险等级匹配（该工具须 consumes_capability）→ 调 clients/seatunnel 的 submit(config_path, job_name)
   └─ ctx = ExecutionContext(caller="worker", tenant_id=..., project_id=..., actor_id=None, run_id=run_id)
⑥ 回写：execution_run.seatunnel_job_id = job_id，status = running，started_at = now()
⑦ 证据事件：capability.consumed + run.started；再入队 Topic.RUN_SUPERVISE（同事务）
   ── 监督用 Outbox 入队而非 beat 轮询发现，避免 run 提交后最长 30 s 无监督的窗口
```

各命令与工具/风险的对应见 §5.2 表。**`ToolBroker` 是这些客户端方法唯一的调用者**：`services/` 与 `tasks/` 不得直接 `import clients.seatunnel` 的写方法（红线 §1）。

### 5.5 生命周期状态机（`RunStatus`）

| 当前 | 事件 | 新状态 | 写入者 |
|---|---|---|---|
| — | Commit | `queued` | `services/run.commit` |
| `queued` | submit 成功 | `running` | `tasks.execution.submit_run` |
| `queued` | submit 业务性失败（验签失败、HOCON 被拒） | `failed` | `tasks.execution.submit_run` |
| `running` | 作业正常结束 + 质量通过 + Swap 成功（be-18） | `succeeded` | `tasks.execution.publish_swap` |
| `running` | 作业异常结束 / 拒绝率超限 | `failed` | `tasks.execution.submit_run` / be-16 |
| `running` | 监督判定 interrupt | `interrupted` | `services/supervision.py`（be-16） |
| `running` | 操作员 pause | `paused` | `services/run.request_pause` |
| `running` / `paused` | 操作员 cancel | `cancelled` | `services/run.request_cancel`（经 `seatunnel.stop`） |
| `failed` / `interrupted` / `succeeded` | 操作员 rollback | `rolling_back` | `services/run.request_rollback` |
| `rolling_back` | `target.rollback_cleanup` 成功 | `rolled_back` | `tasks.execution.rollback_run` |
| `rolling_back` | 清理失败 | `failed` | `tasks.execution.rollback_run` |

活跃态（`queued` / `running` / `paused` / `interrupted` / `rolling_back`）中，同一 `pipeline_version_id` 只允许一条；`succeeded` / `failed` / `cancelled` / `rolled_back` 是终态，不占用唯一索引。

**非法转移处理**：`apply_transition()` 是状态机唯一写入口，先校验 `(current, target)` 在表内（表为 `services/run.py` 的模块级常量 `RUN_TRANSITIONS: Mapping[RunStatus, frozenset[RunStatus]]`），不在表内 → `RUN_CONFLICT`（409）。并发安全：写操作用条件 UPDATE 保证原子性——

```sql
UPDATE execution_run SET status = :target, finished_at = CASE WHEN :terminal THEN now() END
 WHERE id = :run_id AND status = :expected_current;   -- rowcount = 0 → RUN_CONFLICT
```

`rowcount == 0` 说明状态已被别人改走（例如两个并发 cancel），此时**不报错也不覆盖**，读取当前状态返回；若当前状态已是目标状态则视为幂等成功。

### 5.6 周期任务（`tasks/maintenance.py`）

| 任务 | 调度 | 内容 |
|---|---|---|
| `tasks.maintenance.enforce_retention` | 每 6 h（已有） | 删除 `finished_at < now() - RUN_LOG_RETENTION_DAYS`(90) 的 `execution_run`（级联删 `execution_metric`、`error_record`）；`QUALITY_REPORT_RETENTION_DAYS`/`EVIDENCE_RETENTION_DAYS` 为 0 表示永久，**不得删**；同时调用 `services/preparation.expire_due_preparations()` |
| `tasks.maintenance.checkpoint_ledger` | 每 6 h（已有） | 为未签名的证据区间补打 Ed25519 检查点（`LEDGER_CHECKPOINT_INTERVAL = 100`），写 `evidence_ledger_block` + `ledger.checkpoint_signed` |
| `tasks.maintenance.backup_database`（待加） | 每日 03:00 | 调 `deploy/backup/` 脚本（`pg_dump` + MinIO 制品归档），上传对象存储，失败告警 |

备份与账本检查点都必须在**成块/一致快照**的前提下做：清理历史 run 之前先确认其关联证据已落在已签名检查点区间内，否则链会出现不可独立验证的区间。

## 6. 关键算法与规则

1. **退避函数**：`next_available_at(attempts, now) = now + timedelta(seconds=min(2 ** attempts, 300))`。`attempts` 为累加后的值（第一次失败取 1 → 2 s），上限 300 s 防止长时间挂死。纯函数，放 `harness/outbox.py`（`mypy --strict` 覆盖）。
2. **活跃运行判定**：`RunStatus.active_states()`（`models/enums.py`）是唯一事实来源；service 与部分唯一索引的 `WHERE` 子句必须与它一致（索引 DDL 里的状态字面量需与枚举同步，改动时两处一起改）。违反 → `RUN_CONFLICT`。
3. **幂等键命名规范**（`idempotency_key` 表的 `scope` 列）：
   | 场景 | `scope` | `key` |
   |---|---|---|
   | Commit | `run.commit` | 客户端 `Idempotency-Key` |
   | Outbox 消费 | `outbox.consume` | `outbox_message.idempotency_key` |
   | 操作员动作 | `run.action` | 客户端 `Idempotency-Key` |
   `execution_run.idempotency_key` 存 `f"run:{client_key}"`，与 `outbox_message.idempotency_key`（`f"submit:run:{run_id}"`）分离——两者语义不同：前者防同一请求建两条 run，后者防同一条命令被两个 Worker 各提交一次作业。
4. **SeaTunnel 作业名**：`etl-{pipeline_id}-v{version_no}-run{run_id}`，由 be-15 生成；Worker 提交前按 `job_name` 查重（见 be-15 §6），防止重投产生两个作业。
5. **失败快照写入时机**：只有「不可重试的业务终结路径」才写 `failure_snapshot`（be-16 硬中断、`submit_run` 永久失败）；瞬时错误不写，避免快照被重试噪声污染。写入的 `reason` 取 `supervision` / `manual_stop` / `cancel`（与 be-19 的读取约定一致），且只更新自己负责的子键（be-16 → 监督键、be-17 → `quality`、be-18 → `swap`、be-19 → `diagnosis`）。
6. **`dead` 消息的处置**：不自动重投。运维需人工判断后重置 `status='pending'`、`attempts=0`（提供 `scripts/outbox_requeue.py`，M3 可后置）；每次转 `dead` 都写 `run.failed`（若已关联 run）或仅告警。

## 7. 错误码与拒绝语义

| 触发条件 | 错误码 | HTTP | 写证据账本 |
|---|---|---|---|
| 同版本已有活跃 run | `RUN_CONFLICT` | 409 | **是** |
| 非法状态转移（含并发输掉的一方） | `RUN_CONFLICT` | 409 | 否（并发退让不是协议拒绝；若确为非法请求则记 structlog） |
| 前置校验不通过（指纹 / 审批 / 过期 / 策略） | 由 be-13 `assert_committable` 抛出（`FINGERPRINT_MISMATCH` / `APPROVAL_INCOMPLETE` / `PREPARATION_EXPIRED` / `POLICY_BLOCKED`） | 403/409 | 是 |
| Capability 验签失败 / 主体、工具、环境、指纹不匹配 | `CAPABILITY_INVALID`（be-05 的 verifier 返回原因码） | 403 | **是**（`capability.rejected`，`reject_reason` 落表） |
| Capability 过期 | `CAPABILITY_EXPIRED` | 403 | 是 |
| jti 重复消费 | `CAPABILITY_REPLAYED` | 409 | 是 |
| 工具未被 Capability 授权 | `UNAUTHORIZED_TOOL` | 403 | 是 |
| 同 key 不同请求体 | `IDEMPOTENCY_CONFLICT` | 409 | 否 |
| 非 operator 调动作接口 | `ROLE_REQUIRED` | 403 | 否 |
| run 不存在 / 跨租户 | `NOT_FOUND` / `TENANT_MISMATCH` | 404/403 | 否 |
| 回滚清理失败 / Swap 失败（均归 be-18 执行，码由本模块透传） | `ROLLBACK_FAILED` / `SWAP_FAILED` | 409 | 是（`rollback.started` / `swap.*`，失败补 `run.failed`） |
| SeaTunnel 超时/5xx，或 Vault 不可达 / Secret 物化失败 | `INTERNAL_ERROR`（fail-closed） | — （仅 Worker 内部） | 前者记 `run.failed`；后者不写（未消费 jti，无外部副作用）；**均无专用码，见 §10 第 1 条** |

## 8. 安全与合规约束

- **副作用唯一出口**：`tasks/` 与 `services/` 中出现 submit / stop / swap / rollback 时，必须写明是经 `ToolBroker.execute(tool, token, args)`；`clients/` 的方法不得被旁路调用。仓库级验证：全仓 grep `clients.seatunnel` 的写方法调用点，只允许出现在 `harness/broker.py` 的工具注册表内。
- **无明文 Secret**：`outbox_message.payload` 只含引用与标量，唯一例外是 `capability` 令牌（无 Secret 值、TTL 300 s、单次消费，且**严禁**出现在日志、响应体与指标标签里；`dead` 消息亦不得导出）。`assert_payload_has_no_secret` 在入队前扫描键名与字符串值。`execution_run.failure_snapshot` 只存**脱敏**摘要（错误码计数、指标点、错误类型名），不存 HOCON 片段与连接信息。
- **不可变制品**：本模块不写 `pipeline_version` / `preparation` / `approval_decision` / `capability` / `evidence_event`；`capability` 的 `consumed_at` / `consumed_by_run_id` 由 be-05 在 `__mutable_columns__` 白名单内写一次。
- **证据账本**：Commit 写 `capability.issued` + `run.created`；消费写 `capability.consumed`；执行结束写 `run.started` / `run.succeeded` / `run.failed` / `run.interrupted` / `rollback.started` / `rollback.succeeded`。全部经 `harness.EvidenceLedger.append()`（单入口，自动算 `prev_hash` / `event_hash`）。
- **租户过滤**：`list_runs` / `get_run` / `apply_transition` 均带 `tenant_id`；`execution_run` 经 `ProjectScopedMixin` 携带 `tenant_id` / `project_id`。
- **审计**：`auditor` 只读；`structlog` 绑定 `request_id / tenant_id / project_id / execution_run_id / pipeline_version_id`；日志不拼连接信息。
- **fail-closed**：Capability 验签、ReplayGuard 消费、Secret 物化任一失败即拒绝执行；**没有任何"降级为放行"的分支**。

## 9. 测试要点

**单元测试（本机内存 SQLite）**

- `next_available_at`：`attempts=1..6` 的返回值与 300 s 上限；边界 `attempts=0`。`assert_payload_has_no_secret`：正例（`{"run_id": 1}` / `{"capability": "<jwt>"}`）、反例（`{"password": "x"}`、`{"dsn": "mysql://u:p@h/db"}`）。
- `RUN_TRANSITIONS` 全表：每条合法转移通过；每条非法转移（如 `succeeded → running`）抛 `RUN_CONFLICT`。
- `commit` 的事务回滚：mock `assert_committable` 抛异常后，断言 `outbox_message` / `execution_run` / `capability` **计数均为 0**（幽灵命令的直接反例）。

**集成测试（真实 PG + Redis，标 `integration`）**

- **Commit 同事务**：注入一个在 `enqueue` 之后、事务提交之前的异常，断言 `outbox_message` 无新增；正常路径断言 `execution_run` 与 `outbox_message` 同批出现。
- **Outbox 并发投递**：并发两个 `scan_and_dispatch`，断言同一条消息只被投递一次（`sent` 计数 = 1）；`SKIP LOCKED` 行为必须真机验证。
- **退避与 dead**：mock 消费任务持续抛错，断言 `attempts` 递增、`available_at` 按 2/4/8/16 s 递增、第 5 次转 `dead` 并产生告警指标。
- **协议类反例（必测）**
  1. **Capability 重放**：同一 token 调 `ToolBroker.execute` 两次，第二次 `CAPABILITY_REPLAYED`，且 `evidence_event` 有 `capability.rejected`。
  2. **任务重投幂等**：同一 `submit_run` 连续执行两次（模拟 `acks_late` 重投），断言 `seatunnel_job_id` 只被写一次、SeaTunnel 只收到一次 submit。
  3. **同一版本并发两次 Commit**：两个并发请求，一个成功一个 `RUN_CONFLICT`，断言部分唯一索引也在 DB 层拦住（绕过 service 直插第二条活跃 run 应被 PG 拒绝）。
  4. **越权与并发**：`auditor` 调 `POST /runs/{id}/cancel` → 403、跨租户 `GET /runs/{id}` → 403 `TENANT_MISMATCH`；同一 run 并发两个 cancel 只有一个写入状态、另一个幂等返回，SeaTunnel 只收到一次 stop。
- **保留策略**：造 91 天前的 run 与 `error_record`，跑 `enforce_retention`，断言 run 与其指标被删、`quality_report` 与 `evidence_event` **未被删**（永久）。

## 10. 未决与风险

1. **缺少执行侧的细分错误码**：SeaTunnel 不可达、Vault 不可达、Secret 物化失败目前只能落 `INTERNAL_ERROR`，前端无法给出可操作提示。建议在 `core/errors.py` 追加 `SEATUNNEL_UNAVAILABLE` / `SECRET_UNAVAILABLE` 两码（本轮不自造）。验证方式：M3 联调 fe-06 的错误提示文案需要区分时提出。
2. **`capability` 令牌随 Outbox 载荷走带来的过期风险**：令牌 TTL 300 s，而 Outbox 退避可达 300 s（§6 第 1 条）。若消息在 5 分钟内未投递成功（broker 抖动、`scan_and_dispatch` 停顿），Worker 侧必然 `CAPABILITY_EXPIRED`，run 落 `failed`。取舍：把令牌写库会永久留下可重放凭证（更不可接受）；重签令牌需要新的 Capability 签发入口。当前策略是「过期即 `failed` + 告警，由 operator 换 `Idempotency-Key` 重新 Commit」，并在 `etl.seatunnel.submit` 的 `available_at` 上限上留出余量（首次投递失败即告警，不等退避到 300 s）。验证方式：M3 用「停 worker 6 分钟再启动」复现，断言 run 落 `failed` 且 `error_message` 为 `CAPABILITY_EXPIRED`，并确认无 SeaTunnel 作业残留。
3. **`etl.run.supervise` 的入队策略**：本文改为「submit 成功后同事务入队一次监督命令」而非纯 beat 轮询，理由见 §5.4 第 ⑦ 步。需与 be-16 对齐：若 be-16 仍用 `collect_active_runs` 轮询兜底，则两者并存（轮询负责续采、命令负责首采），必须保证不重复写 `execution_metric`。
4. **`paused` 的语义未拍板**：本文定义为「暂停后续阶段推进（不调 `seatunnel.stop`，作业继续跑）」，且**无 resume 接口**。若产品要求 pause 真正停作业，则应为 `pause` 调 `seatunnel.stop` 并引入 resume（SeaTunnel Zeta local 模式是否有 resume 能力**未验证**）。验证方式：M3 实机确认 Zeta REST 是否支持作业恢复；不支持则明确 pause 为「单向暂停，恢复靠 cancel 后重新 Commit」。
5. **`succeeded → rolling_back` 是否允许**：本文在 §5.5 表中以 `succeeded?` 标注为待定。若允许对已发布版本回滚，实际执行的是"回滚上一次发布"（`target.rollback_cleanup` 的语义需覆盖已 Swap 的表恢复）。验证方式：与 be-18 定稿 `rollback_plan.restore` 的取值集合，二者必须一致。
6. **`batch_size=50` 与 beat 2 s 的吞吐上限**：理论 25 msg/s。演示场景足够，但若一次 Commit 产生的事件量大（含监督命令）需评估。验证方式：M3 压测 `scan_and_dispatch` 单轮耗时，必要时并行化（多 beat 实例已由 `SKIP LOCKED` 支撑）。
7. **`dead` 的重投脚本**（`scripts/outbox_requeue.py`）**尚未实现**，目前只能人工改库；且 `retention-cleanup` 要求「先确认证据已入已签名区间再删 run」的前置查询（需 be-06 提供 `is_seq_signed(event_seq) -> bool`）也尚未设计。验证方式：M4 交付前补脚本、补该调用并各加一条断言用例。

## 11. 变更记录

| 日期 | 变更 | 原因 |
|---|---|---|
| 2026-10-16 | 初版 | M3 治理链路模块概要设计 |
