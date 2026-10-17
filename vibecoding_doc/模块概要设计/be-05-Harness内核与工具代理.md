# be-05 Harness 内核与工具代理

> 上游：[概要设计总纲.md](../概要设计总纲.md) §5 Harness 安全执行内核 ｜ [PRD.md](../PRD.md) §6.4.3 Commit、§8.3 防重放、§10.1 安全
> 数据：[核心数据字典.md](../核心数据字典.md) §4 审批与授权 ｜ 决策：[需求对齐记录.md](../需求对齐记录.md) §三
> 代码位置：`backend/harness/` ｜ 里程碑：M1（注册表与 Capability 签发）/ M3（消费侧） ｜ 依赖：be-01；被 be-06 be-14 be-15 be-18 依赖

## 1. 职责与边界

- **做什么**
  1. 在 `harness/tools.py` 维护**唯一的副作用工具注册表**：每个 `ToolName` 对应一条 `ToolSpec`（风险等级、是否要求 Capability、执行者进程、参数模型、是否要求质量通过）。
  2. 提供**全系统副作用唯一出口** `ToolBroker.execute()`：固定 7 步执行序，任一步失败即拒绝且不执行。
  3. 用 Ed25519（EdDSA）签发与验证 Capability JWT，绑定「主体 / 工具 / 环境 / 制品指纹」四项。
  4. 用 `ReplayGuard` 对 `jti` 做**双重原子消费**：Redis `SETNX` 与 PG `capability.jti` 唯一约束**同时**成功才算消费成功。
  5. 把每一次协议类拒绝写成证据事件（经 be-06 的 `EvidenceLedger.append()`，本模块不自建写入路径）。
- **不做什么**
  - **不做风险判定**：`risk_level` 由 be-07 PDP 产出并冻结在 `preparation` 上；本模块只读取并做「工具风险 vs 准备单风险」的一致性校验，不在执行期重跑 PDP。
  - **不实现证据账本**：链式哈希、检查点、验链属 be-06；本模块只调用 `append()`。
  - **不物化 Secret**：Vault 读取与临时文件属 be-15；本模块的 `args` 只允许携带 `secret_reference` 的**引用**（provider/path/version）。
  - **不写 Outbox**：`outbox_message` 由 be-14（`harness/outbox.py`，同层兄弟模块）在业务事务内写入；本模块禁止 `apply_async`。
  - **不实现 SeaTunnel / Doris 协议细节**：那是 `clients/`（be-15 be-18）；本模块只按注册表调用注入的 `executor`。
  - **不出 HTTP**：拒绝语义经 `AppError.code` 由 be-01 的异常处理器转成响应；本模块不 import `fastapi`。
- **所在分层**：`harness/`。允许依赖 `core/`（`errors` `config` `redis` `ids` `logging`）与 `models/`（写 `capability`、读 `preparation`/`execution_run`）与 `clients/` 的**执行器接口**（由调用方注入，不在 import 期绑定具体实现）。**禁止**依赖 `services/` 与 `api/`。

## 2. 模块结构

```
backend/harness/
├── __init__.py      # 只导出 ToolBroker / CapabilityIssuer / CapabilityVerifier / ReplayGuard / ToolName 注册表访问器（待建）
├── tools.py         # ToolSpec 注册表 + 6 个工具的 args Pydantic 模型 + register_tool()/get_tool()/all_tools()（待建）
├── capability.py    # CapabilityIssuer.issue() / CapabilityVerifier.verify() / CapabilityClaims / ExpectedBinding（待建）
├── replay.py        # ReplayGuard.consume()：Redis SETNX + PG 唯一约束双保险（待建）
├── broker.py        # ToolBroker.execute()：7 步执行序与拒绝落账（待建）
└── errors.py        # harness 层异常与 reject_stage 映射，复用 core.errors.ErrorCode（待建）
```

`harness/outbox.py`（be-14）与 `harness/ledger.py`（be-06）不在本文覆盖范围，但同属本层，依赖方向为 `broker → ledger`（单向）。

## 3. 对外接口

### 3.1 HTTP

无。本模块不注册路由；它被 `services/`（be-13 Commit 签发侧）与 `tasks/`（be-14 消费侧）调用。HTTP 状态码由 be-01 的异常处理器按 `AppError.http_status` 产出，见 §7。

### 3.2 Python（跨模块契约）

```python
# harness/tools.py
@dataclass(frozen=True, slots=True)
class ToolSpec:
    tool: ToolName
    risk: RiskLevel                      # 工具固有风险，与 preparation.risk_level 做一致性校验
    requires_capability: bool
    executor: Literal["api", "worker"]   # 允许在哪个进程执行；错进程一律拒绝
    args_model: type[BaseModel]
    side_effect: str                     # 人可读的副作用描述，仅用于证据 payload 与文档
    idempotent: bool                     # 重复调用是否安全（task_acks_late 下必需）
    requires_quality_pass: bool = False  # 仅 target.publish_swap 为 True

def register_tool(spec: ToolSpec) -> None: ...
def get_tool(tool: ToolName) -> ToolSpec: ...          # 未注册 → RejectedToolError
def all_tools() -> tuple[ToolSpec, ...]: ...           # 顺序按 ToolName 定义序，保证可复现

# harness/capability.py
@dataclass(frozen=True, slots=True)
class CapabilityClaims:
    jti: uuid.UUID
    sub: str                 # 主体标识，形如 f"user:{user_id}"
    subject_id: int
    tenant_id: int
    project_id: int
    tool: ToolName
    env: Env
    artifact_sha256: str
    preparation_id: int
    iss: str
    iat: datetime
    exp: datetime

@dataclass(frozen=True, slots=True)
class ExpectedBinding:
    subject_id: int
    tool: ToolName
    env: Env
    artifact_sha256: str
    tenant_id: int

@dataclass(frozen=True, slots=True)
class IssuedCapability:
    token: str               # 只在返回值的生命周期内存在于内存，禁止落库/落日志
    claims: CapabilityClaims
    token_sha256: str        # 入库的是它

class CapabilityIssuer:
    def __init__(self, *, private_key_path: str, key_id: str, issuer: str, ttl_sec: int) -> None: ...
    def issue(
        self, *, preparation_id: int, subject_id: int, tool: ToolName, env: Env,
        artifact_sha256: str, tenant_id: int, project_id: int, now: datetime,
    ) -> IssuedCapability: ...

class CapabilityVerifier:
    def __init__(self, *, public_key_path: str, issuer: str) -> None: ...
    def verify(self, token: str, expected: ExpectedBinding, *, now: datetime) -> CapabilityClaims: ...

# harness/replay.py
class ConsumptionResult(StrEnum):
    CONSUMED = "consumed"          # Redis 与 PG 都成功
    REPLAYED = "replayed"          # 任一侧发现已存在

class ReplayGuard:
    def __init__(self, *, redis: Redis, ttl_sec: int) -> None: ...
    async def consume(self, claims: CapabilityClaims, session: AsyncSession) -> ConsumptionResult: ...

# harness/broker.py
@dataclass(frozen=True, slots=True)
class ExecutionContext:
    request_id: str
    tenant_id: int
    project_id: int
    actor_id: int | None
    caller: Literal["api", "worker", "beat"]
    run_id: int | None = None

@dataclass(frozen=True, slots=True)
class ToolResult:
    tool: ToolName
    ok: bool
    jti: uuid.UUID | None
    detail: dict[str, Any]        # 已脱敏；只放引用与计数，不放 Secret 与原始行
    evidence_seq: int | None

class ToolBroker:
    def __init__(self, *, issuer: CapabilityIssuer, verifier: CapabilityVerifier,
                 guard: ReplayGuard, ledger: EvidenceLedger) -> None: ...
    async def execute(
        self, tool: ToolName, token: str | None, args: Mapping[str, Any] | BaseModel,
        ctx: ExecutionContext, *, session: AsyncSession,
    ) -> ToolResult: ...
```

**契约与异常**：`execute()` 成功返回 `ToolResult(ok=True)`；所有拒绝都 `raise` 一个 `core.errors.AppError` 子类（码见 §7），且**先写证据事件再抛出**，保证账本与异常一致。`execute()` 内不 commit；事务边界由调用方（be-13 / be-14）控制——这样「业务事实 + Outbox + 证据」才能在同一个事务里落地。

## 4. 数据模型

| 表 | 读写 | 本模块用到的列 | 约束 |
|---|---|---|---|
| `capability` | 读写 | 读：`jti`/`preparation_id`/`subject_id`/`tool`/`environment`/`artifact_sha256`/`token_sha256`/`expires_at`/`consumed_at`；写：插入整行（签发侧）、`consumed_at`（+ `consumed_by_run_id`）、`reject_reason`（被拒侧） | `uq_capability_jti`（重放拦截的数据库侧兜底）；`__immutable__ = True` 且 `__mutable_columns__ = {"consumed_at","consumed_by_run_id","reject_reason"}` |
| `preparation` | 只读 | `artifact_sha256`/`environment`/`risk_level`/`required_slots`/`status`/`expires_at`/`project_id` | 只读；不信赖调用方传入的指纹，一律以本表为准 |
| `execution_run` | 只读 | `pipeline_version_id`/`preparation_id`/`status`/`shadow_table`/`formal_table` | 供 `target.publish_swap` 的前置条件校验（质量报告通过 / 未硬中断） |
| `quality_report` | 只读 | `run_id`/`passed` | 仅 `requires_quality_pass=True` 的工具读取 |

`capability.consumed_at` 的「只允许一次由 NULL 变更」在应用层由 `ReplayGuard` 保证、在数据库层由 `uq_capability_jti` 的插入冲突表达（插入新行失败 = 该 jti 已被消费过）。表定义不本文重复，见 [核心数据字典 §4](../核心数据字典.md)。

## 5. 核心流程

### 5.1 签发（api 进程，be-13 调用）

1. Commit 前服务端重算 `pipeline_version.sha256` 与 `preparation.artifact_sha256` 必须一致，否则 `FINGERPRINT_MISMATCH`。
2. 集齐 `preparation.required_slots` 的 `approve` 决定，且每条决定的 `artifact_sha256`/`fingerprint_at_decision` 与当前值一致。
3. `CapabilityIssuer.issue()` 生成 `jti = uuid4()`，`iat = now`，`exp = now + CAPABILITY_TTL_SEC`（默认 300 s），用 `CAPABILITY_SIGNING_KEY_PATH` 私钥签 EdDSA。
4. 同事务插入 `capability` 行：只落 `token_sha256 = sha256(token)`、`signature_alg="EdDSA"`、`key_id`，**不落 token 原文**。
5. 写 `CAPABILITY_ISSUED` 证据事件（payload 只有 `jti`/`tool`/`env`/`preparation_id`/`artifact_sha256`）。
6. 向调用方返回 `IssuedCapability`；token 原文随 outbox 载荷（`etl.seatunnel.submit`）交给 worker，**不进入任何日志与响应体**。

### 5.2 消费（worker 进程，be-14 调用 `ToolBroker.execute`）

```
① 工具存在性      get_tool(tool)                          → 未注册即拒
② 风险与 Capability requirements
                  spec.requires_capability ? token 必填 : token 必为 None
                  spec.executor 与 ctx.caller 的**单向**匹配（§6.6），不符即拒
③ 验签            CapabilityVerifier.verify(token, expected, now=now)
④ 绑定校验（六项） subject_id / tool / env / artifact_sha256 / tenant_id / project_id
                  先比 ctx（无需 DB），再查库重算 expected（preparation + pipeline_version）
⑤ ReplayGuard     原子消费 jti（Redis SETNX + PG 条件 UPDATE；§6.4）
⑥ 前置条件        preparation.status/expires_at、quality_report.passed、run.status
⑦ 执行            spec.args_model 校验 args → executor(spec, args, ctx, session)
                  随后写 CAPABILITY_CONSUMED（成功）/ CAPABILITY_REJECTED（失败）
```

任一步 ①–⑥ 失败：写 `CAPABILITY_REJECTED`（payload 含 `reject_stage`、`jti`（若可解析）、`tool`），然后 `raise`。步骤 ⑦ 失败：写 `CAPABILITY_REJECTED`（`reject_stage="executor"`）后原样抛出执行器异常，**不重试**——重试由 be-14 的 Outbox 幂等语义决定。

### 5.3 并发消费同一 `jti`

```
worker A                              worker B
   │ ① Redis SETNX cap:consumed:{jti}    │ ① Redis SETNX cap:consumed:{jti}
   │◀─ 1（拿到）                          │◀─ 0（已被占）
   │ ② INSERT capability(jti=…)           │ ② 直接判 REPLAYED（不再查库）
   │◀─ 成功，COMMIT                       │ ③ 写 CAPABILITY_REJECTED → 抛 CAPABILITY_REPLAYED
   │ ③ 执行工具                            │
   └─ 正常完成                             └─ 未执行任何副作用
```

Redis 不可用时的**竞态**由 PG 兜底：两侧都 SETNX 失败 → 都会走 INSERT → 只有一侧 `uq_capability_jti` 插入成功，另一侧捕获 `IntegrityError` 判 `REPLAYED`。因此「先查后写」被禁止：`SELECT ... WHERE jti=?` 到 `INSERT` 之间存在窗口，两侧都会读到「不存在」然后都执行。

### 5.4 Capability 行状态机

| 状态 | 事件 | 新状态 | 非法转移处理 |
|---|---|---|---|
| 已签发（`consumed_at IS NULL`） | `ReplayGuard` 消费成功 | 已消费（写 `consumed_at`） | 重复消费 → `CAPABILITY_REPLAYED`，写账本 |
| 已签发 | 验签/绑定/前置条件失败 | 被拒（写 `reject_reason`） | 同一行再次被拒 → 允许（只覆盖 `reject_reason` 之外的列被 ORM 拒绝） |
| 已消费 | 再次消费 | 不变 | 直接拒，不执行 |
| 已过期（`now >= exp`） | 任何消费 | 不变 | `CAPABILITY_EXPIRED`，写账本 |

「被拒」不改变 `consumed_at` 为已消费语义：**拒绝不消耗 jti**，因为拒绝时并未执行副作用；但被拒的 jti 不会再次被验签通过（绑定条件不会变化），实际不可复用。

## 6. 关键算法与规则

### 6.1 工具注册表（6 条，`risk` 与《概要设计总纲》§5 一致）

| `ToolName` | `risk` | `requires_capability` | `executor` | `requires_quality_pass` | `idempotent` | `args_model` 关键字段 |
|---|---|---|---|---|---|---|
| `seatunnel.submit` | P1 | 是 | worker | 否 | 是（按 `run_id`） | `run_id` `pipeline_version_id` `hocon_artifact_id` `secret_refs[]` `shadow_table` `error_table` `supervision_contract_id` |
| `seatunnel.stop` | P2 | 是 | worker | 否 | 是（已停返回同结果） | `run_id` `job_id` `reason` ∈ {supervision_interrupt, manual_cancel, pause} |
| `target.publish_swap` | P0 | 是 | worker | **是** | 是（已 swap 返回同结果） | `run_id` `shadow_table` `formal_table` `quality_report_id` |
| `target.rollback_cleanup` | P1 | 是 | worker | 否 | 是（重复清理无副作用） | `run_id` `shadow_table` `formal_table` `rollback_plan_digest` |
| `datasource.connectivity_test` | P3 | 否 | api | 否 | 是 | `data_source_id` `connect_timeout_sec`（默认 5） |
| `metadata.probe` | P3 | 否 | api | 否 | 是 | `data_source_id` `scope` `sample_rows`（默认 100） |

所有 `args_model` 一律 `model_config = ConfigDict(frozen=True, extra="forbid")`：多传字段即 `VALIDATION_ERROR`，避免调用方塞入未审计字段（例如偷偷带连接串）。字段只允许**引用型**取值（id、表名、`{provider,path,version}`），禁止出现 `password`/`token`/`secret`/`conn_str` 形态的键。

### 6.2 `datasource.connectivity_test` / `metadata.probe` 为什么免 Capability

二者风险为 P3（纯只读探查，见《需求对齐记录》§四），不触碰数据写入，故不签发 Capability。但它们**仍然必须经 `ToolBroker.execute()`**：单一出口的价值是「所有出网/出进程的副作用只有一处可审计」，只读也出网。`requires_capability=False` 时 `token` 必须为 `None`——传了 token 视为调用方理解错误，直接拒（防止有人以为「多带一个 token 更安全」而绕开免签路径）。

### 6.3 Capability JWT claim 结构

| claim | 类型 | 值来源 | 校验点 |
|---|---|---|---|
| `alg`（header） | str | 固定 `EdDSA` | 验签时**显式白名单**，禁止 `none`/`HS*` |
| `jti` | uuid str | `uuid4()` | ReplayGuard 的键 |
| `sub` | str | `f"user:{subject_id}"` | 与 `subject_id` 冗余，便于人工读日志 |
| `subject_id` | int | Commit 操作员 | 与 `ctx.actor_id` 比对 |
| `tenant_id` / `project_id` | int | `preparation` | 与 `ctx` 比对 → 不符 `TENANT_MISMATCH` |
| `tool` | `ToolName` 值 | 本次要执行的工具 | 与 `execute(tool=...)` 严格相等 |
| `env` | `Env` 值 | `preparation.environment` | 与 `preparation.environment` 相等 |
| `artifact_sha256` | hex64 | `preparation.artifact_sha256` | 与当前重算值相等 → 不符 `FINGERPRINT_MISMATCH` |
| `preparation_id` | int | `preparation.id` | 用于回查前置条件 |
| `iss` | str | `CAPABILITY_ISSUER` | 与 verifier 期望值相等 |
| `iat` / `exp` | int（epoch 秒） | `now` / `now + 300` | `exp - iat == CAPABILITY_TTL_SEC`；`now < exp`，且 `now >= iat - 5`（容忍 5 s 时钟漂移） |

私钥文件 `CAPABILITY_SIGNING_KEY_PATH` 由 `core/keys.py` 生成（0600），容器以 `./secrets:/run/secrets:ro` 只读挂载，**永不进仓库**。`capability.token_sha256` 是唯一入库形态。

### 6.4 `ReplayGuard.consume` 与两种降级路径

> 🔴 **2026-10-16 修正：消费是「条件 UPDATE 已签发行」，不是「INSERT 新行 + 捕获唯一约束冲突」。**
> 本节初稿与 §5.1 自相矛盾——§5.1 说**签发时**就插 `capability` 行（只落 `token_sha256`），
> 那么消费时再 INSERT 同一个 `jti` 必然命中 `uq_capability_jti`，
> **每一次正常消费都会被判成重放**，系统根本跑不起来。
> 以 §5.1 为准：表的语义是「一行 = 一张签发过的令牌」，消费就是把这行标记为已用。

```
async def consume(claims, session, *, run_id=None) -> ConsumptionResult:
    key = KEY_CAP_CONSUMED.format(jti=claims.jti)      # "cap:consumed:{jti}"
    try:
        ok = await redis.set(key, "1", nx=True, ex=ttl_sec)   # ttl = CAPABILITY_TTL_SEC + 60
    except RedisError:
        → fail-closed：INTERNAL_ERROR（不执行）
    if not ok:
        return REPLAYED
    # 原子性来自**单条 UPDATE 的 WHERE 条件**，不是应用层的判断：
    #   UPDATE capability SET consumed_at=..., consumed_by_run_id=...
    #    WHERE jti=:jti AND consumed_at IS NULL
    # 并发时 PG 对该行加锁，第二个事务的 UPDATE 影响 0 行 → rowcount == 0 → 判 REPLAYED
    result = await session.execute(update(Capability).where(
        Capability.jti == claims.jti, Capability.consumed_at.is_(None)
    ).values(consumed_at=now, consumed_by_run_id=run_id))
    if result.rowcount == 0:
        return REPLAYED          # 已被消费 / 从未签发 / jti 被篡改成不存在的值（三者处理相同）
    return CONSUMED
```

- **`rowcount == 0` 的三种可能**（已消费 / 从未签发 / `jti` 被篡改）处理相同：判 `REPLAYED`。
  PG 是事实来源，Redis 键可能因过期/TTL 丢失而被前次消费之外的人占用，此时绝不放行。
  Redis 键**不回滚**，保留到 TTL 到期（多留一个短时"毒键"比放行安全）。
- **`consume()` 不 commit**：事务边界归调用方，这样「消费标记 + 证据 + Outbox」
  能在同一个事务里落地。
- **Redis 不可用**：**fail-closed —— 直接拒绝执行**。理由：ReplayGuard 是「未授权/重放成功率 = 0」的
  唯一保证，降级为「只靠 PG」会让消费窗口从「无窗口」退化为「先查后写」的等效形态，
  不符合 [PRD §1.4](../PRD.md) 的成功指标。Redis 短时不可用期的现象是执行排队而非重复执行，可接受。
- **禁止「先查后写」**：`SELECT` 与 `UPDATE` 之间的窗口会被并发方同时穿越，
  两侧都认为自己是首次消费，直接产生两次 SeaTunnel 提交（两次真实数据搬运）。
- 键名取自 `core/redis.py::KEY_CAP_CONSUMED`，**不在这里另写一份模板**：
  两份模板时改一处忘一处的结果是「标记写进 A 键、检查去读 B 键」——重放防护静默失效，
  而且任何测试都不会红。

### 6.5 前置条件校验（步骤 ②）

- `spec.requires_capability=False` 且 `token is not None` → `CAPABILITY_INVALID`。
- `spec.executor != ctx.caller` → `UNAUTHORIZED_TOOL`（例如 api 进程试图直接提交 SeaTunnel）。
- `spec.requires_quality_pass` → 读 `quality_report.passed`，为 `False` 或缺报告 → `POLICY_BLOCKED`；同时 `execution_run.status` 处于 `interrupted`/`failed`/`cancelled` → `POLICY_BLOCKED`。
- `preparation.status` 非 `approved` 或 `now >= preparation.expires_at` → `PREPARATION_EXPIRED`。

### 6.6 🔴 `executor` 检查是**单向**的（2026-10-16 修正）

`spec.executor != ctx.caller → UNAUTHORIZED_TOOL` 这条初稿写成了「必须相等」。
实现时发现它会把两个 P3 只读工具逼到死角：它们声明 `executor="api"`，
实际因超时考虑被投给 Celery，于是 `services/datasource` 只能传 `caller="api"`
——**它在 worker 进程里说了假话**。

采纳的单向规则：

```
allowed = (spec.executor == ctx.caller)
          or (ctx.caller == "worker" and spec.executor == "api")
```

- **api 进程不得执行 worker 工具**（`seatunnel.submit` / `target.publish_swap` 等）：
  那是绕过审批直接提交作业的最短路径。**这条必须堵死，也是该检查存在的唯一理由。**
- **worker 执行声明为 api 的只读工具**：放行。worker 本就持有作业提交权，
  多跑一次只读探测不增加攻击面；拒掉反而迫使调用方伪造 `caller`——
  那等于让这道检查建立在谎言上，比放宽更糟。
- 反向（`beat` 执行任何工具）仍被拒：beat 只该投递任务，不该自己动外部系统。

## 7. 错误码与拒绝语义

错误码全部取自 `core/errors.py` 的 `ErrorCode`，本模块不自造。HTTP 状态由 `AppError.http_status` 决定；worker 侧无 HTTP，`http_status` 仅用于日志分级。**协议类拒绝一律写证据账本**（`CAPABILITY_REJECTED`，`subject_type="capability"`，`subject_id=jti`）。

| # | 触发条件 | 错误码 | HTTP | 写账本 |
|---|---|---|---|---|
| 1 | 工具名未注册 | `UNAUTHORIZED_TOOL` | 403 | 是 |
| 2 | 工具要求 Capability 但 `token` 缺失 | `CAPABILITY_INVALID` | 403 | 是 |
| 3 | 工具免 Capability 却传了 `token` | `CAPABILITY_INVALID` | 403 | 是 |
| 4 | JWT 格式非法 / 签名不通过 / header alg 不在白名单 | `CAPABILITY_INVALID` | 403 | 是 |
| 5 | `now >= exp` | `CAPABILITY_EXPIRED` | 403 | 是 |
| 6 | `subject_id` 与执行主体不符 | `CAPABILITY_INVALID` | 403 | 是 |
| 7 | `tool` 与本次调用工具不符 | `UNAUTHORIZED_TOOL` | 403 | 是 |
| 8 | `env` 与 `preparation.environment` 不符 | `CAPABILITY_INVALID` | 403 | 是 |
| 9 | `artifact_sha256` 与当前制品指纹不符（审批后制品已变） | `FINGERPRINT_MISMATCH` | 409 | 是 |
| 10 | `tenant_id`/`project_id` 与上下文不符 | `TENANT_MISMATCH` | 403 | 是 |
| 11 | `jti` 已被消费（Redis 命中或 PG 唯一冲突） | `CAPABILITY_REPLAYED` | 409 | 是 |
| 12 | Redis 不可用（fail-closed） | `INTERNAL_ERROR` | 500 | 是（尽力写；PG 也可用才写成功） |
| 13 | `preparation` 非 `approved` 或已过期 | `PREPARATION_EXPIRED` | 409 | 是 |
| 14 | `requires_quality_pass` 前置未满足 | `POLICY_BLOCKED` | 403 | 是 |
| 15 | `executor` 与调用进程不符（单向规则见 §6.5） | `UNAUTHORIZED_TOOL` | 403 | 是 |
| 16 | `args` 不符合 `args_model` | `VALIDATION_ERROR` | 422 | 否（参数问题，非协议拒绝；原因见未决） |
| 17 | 执行器本身失败 | 由执行器异常决定（如 `SWAP_FAILED`） | 500 | 是（`CAPABILITY_REJECTED`，`reject_stage="executor"`） |

`capability.reject_reason` 取值只允许用 [核心数据字典 §4](../核心数据字典.md) 列出的 `expired` / `replay` / `subject_mismatch` / `env_mismatch` / `fingerprint_mismatch` / `unauthorized`；映射：#5→`expired`，#11→`replay`，#6→`subject_mismatch`，#8→`env_mismatch`，#9→`fingerprint_mismatch`，#1/#2/#3/#4/#7/#15→`unauthorized`，#12 与 #13 当前无对应取值（见 §10）。

## 8. 安全与合规约束

1. **副作用唯一出口**：`seatunnel.submit` / `seatunnel.stop` / `target.publish_swap` / `target.rollback_cleanup` 只允许在 `harness/tools.py` 注册一次；全仓 grep 校验 `api/` 与 `services/` 不得直接 import `clients.seatunnel` / `clients.doris` 的执行方法（CI 断言，见 §9）。
2. **无明文 Secret**：`args` 只允许引用；`ToolResult.detail` 只放计数与 id；证据 payload 经 be-06 的脱敏前置校验。日志只记 `tool` / `jti`（jti 是随机 UUID，非凭据）/ `run_id` / `reject_stage`，**永不记 token 原文**。
3. **私钥边界**：Ed25519 私钥只在 `CapabilityIssuer` 与 `CapabilityVerifier` 的构造期从路径加载；`CapabilityIssuer` 在 worker 进程**不实例化**（worker 只有公钥，见 be-15）。
4. **不可变制品**：`capability` 置 `__immutable__`，`consumed_at`/`consumed_by_run_id`/`reject_reason` 是白名单列；任何修改 `jti`/`tool`/`artifact_sha256` 的尝试由 `models/base.py` 的 flush 钩子抛 `ImmutableViolationError`。
5. **租户隔离**：`ExecutionContext` 必带 `tenant_id`；读 `preparation`/`execution_run` 一律带 `tenant_id` 过滤，跨租户 → `TENANT_MISMATCH` 并写账本。
6. **审计**：`CAPABILITY_ISSUED` / `CAPABILITY_CONSUMED` / `CAPABILITY_REJECTED` 三类事件构成本模块的完整审计面；拒绝事件必须能回答「谁、在哪个进程、用哪个 jti、对哪个工具、卡在哪一步」。

## 9. 测试要点

**单元测试（纯逻辑，SQLite 内存库 + fakeredis 可跑）**
1. `all_tools()` 恰好 6 条且与 `ToolName` 成员一一对应（多一个少一个都失败）。
2. 每个 `args_model` 的 `extra="forbid"` 反例：塞入 `password` 字段 → `ValidationError`。
3. `CapabilityVerifier.verify` 反例矩阵：过期 / alg 篡改 / 主体不符 / 工具不符 / 环境不符 / 指纹不符 / iss 不符 / exp-iat ≠ TTL —— 每条断言错误码与 `reject_stage`。
4. `ReplayGuard.consume` 的三条路径：正常消费、Redis 命中、PG 唯一冲突（构造已存在行）——分别断言 `CONSUMED`/`REPLAYED`/`REPLAYED`。
5. 确定性：同输入两次 `execute`（mock 执行器）产出相同 `ToolResult.detail` 键集合。

**集成测试（真实 PG + Redis，标 `@pytest.mark.integration`）**
6. **并发消费同一 jti**：`asyncio.gather` 20 个协程（各自独立 session）同时消费同一 token，断言恰好 1 个 `CONSUMED`、19 个 `CAPABILITY_REPLAYED`，且 mock 执行器只被调用 1 次。
7. **Redis 不可用 fail-closed**：关掉 Redis 连接池 → 断言拒绝且执行器零调用。
8. **Redis 成功、PG 冲突**：预置同 jti 行后消费 → 断言 `REPLAYED` 且执行器零调用。
9. **P0 工具未经质量通过**：`target.publish_swap` 在 `quality_report.passed=false` 下 → `POLICY_BLOCKED`。
10. **免 Capability 工具仍走 Broker**：以 `api` 身份执行 `metadata.probe`，断言证据事件 `METADATA_PROBED` 存在且执行器被调用；再断言绕过 Broker 直接调用执行器在 import 图检查中被拦截。
11. **执行序**：任一步失败时断言执行器被调用次数为 0（含 ①–⑤ 全部步骤的参数化用例）。
12. **账本一致性**：每次拒绝后 `evidence_event` 新增一条 `CAPABILITY_REJECTED`，且 `payload.reject_stage` 与预期一致。

## 10. 未决与风险

1. **`capability.reject_reason` 取值不足**：[核心数据字典 §4](../核心数据字典.md) 列出 6 个取值，覆盖不到「Redis 不可用 fail-closed」与「preparation 过期」两种拒绝。当前实现置 `NULL`、只由账本事件表达。建议在数据字典追加 `dependency_unavailable` / `preparation_expired` 两个取值（需 owner 拍板）。
2. **`VALIDATION_ERROR` 是否写账本待定**：参数校验失败不计入协议类拒绝，但若外部反复构造畸形 `args` 也具备审计价值。当前选择不写，理由是高频噪声会稀释账本信号。验证方式：M3 观察实际拒绝分布后决定。
3. **时钟漂移容忍度**：`iat` 的 5 s 容忍是基于「api 与 worker 同宿主机 Compose」的假设；若未来跨机部署需改为 NTP 同步 + 更宽窗口。验证方式：M3 在 ECS 上实测 `iat` 与服务端时间差分布。
4. **Capability 私钥轮转**：`CAPABILITY_KEY_ID` 已在签发时落库，但 verifier 只加载单个公钥文件，轮转期间新旧公钥并存需要多公钥加载路径。当前约定「无活跃执行时轮转」（见 `core/keys.py` 提示）。验证方式：M4 演练一次轮转。
5. **`seed`/`dev` 环境是否也强制 Capability**：当前实现对所有环境统一要求（P1/P2 工具），不做 dev 例外。若开发者体验受限，可加「dev 环境放宽」开关——但**不与本模块的安全语义绑死**，需单独拍板。
6. **`harness/errors.py` 与 `core/errors.py` 的边界**：本模块只新增异常类（复用既有 `ErrorCode`），不新增错误码字面量。若后续发现必须新增码，只能在 `core/errors.py` 追加。

## 11. 变更记录

| 日期 | 变更 | 原因 |
|---|---|---|
| 2026-10-16 | 初稿：定义 6 条 `ToolSpec`、7 步执行序、Capability claim 结构、ReplayGuard 双重原子消费与降级取舍、17 条拒绝语义 | M1/M3 设计落地，需与既有 `ToolName` / `ErrorCode` / `capability` 表对齐 |
| 2026-10-16 | **§6.4 消费实现改写**：INSERT + 捕获唯一约束冲突 → 幂等的条件 `UPDATE ... WHERE consumed_at IS NULL`。同步补充「`consume()` 不 commit」「键名取自 `core/redis.py`，不另写模板」 | 原 §6.4 与 §5.1 矛盾（§5.1 签发即插行），照 §6.4 实现则每次正常消费都被判重放 |
| 2026-10-16 | **新增 §6.6**：`executor` 检查单向化；拒绝矩阵 #15 标注引用 §6.6 | 原「必须相等」迫使 worker 里的调用方伪造 `caller="api"` |
| 2026-10-16 | §5.2 第 ④ 步的绑定项由四项改为**清单**：`subject_id` / `tool` / `env` / `artifact_sha256` / `tenant_id` / `project_id` | 拒绝矩阵 #10 要求校验 `project_id`，而四项里没有它；缺它则同租户跨项目可用同一张令牌 |
| 2026-10-16 | §5.2 执行序中 `_assert_ctx`（上下文比对）提到 `_expected_binding`（查库取绑定）之前 | 跨租户调用原本先撞「preparation 不存在」，拿到 `NOT_FOUND` 而不是 `TENANT_MISMATCH` |
| 2026-10-16 | 🔴 `_ALLOWED_DETAIL_KEYS` 补 `state`，并新增「每个工具的 detail 键都在白名单内」的参数化用例 | 白名单外未知键被**静默丢弃**（只留一行 warning），于是连通性测试明明连上了却回报 `failed`——业务结论被一层「安全过滤」悄悄改写。这类键漏登记不会有报错、单测也照过 |
| 2026-10-16 | 工具执行器注册改为**显式启动钩子**（worker 的 `celeryd_init` + API 的 `lifespan` 各一次） | 原先没有任何调用点，真机执行工具直接 `UnauthorizedToolError`；而单测里模块被 import 过，永远发现不了 |
