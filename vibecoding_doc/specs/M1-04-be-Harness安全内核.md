# M1-04 · be-05 Harness 安全内核

> 状态：**已实现**（spike 与代码同批落地；Outbox 的真实实现归 M3，见 §7）
> 期次：M1 ｜ 依赖：be-01（core）、be-02（`SubjectContext`）、be-06（账本是本模块的一部分）
> 被依赖：be-03 be-04（P3 免签工具）、be-12 be-13（Capability 签发与消费）、be-15（执行）
> 设计来源：[be-05-Harness安全内核.md](../模块概要设计/be-05-Harness安全内核.md)
> 数据来源：[核心数据字典.md](../核心数据字典.md) §6（`capability` / `evidence_event` / `evidence_ledger_block`）

---

## 1. 模块定位

### 1.1 做什么

1. **工具注册表**：`ToolName → ToolSpec`（风险等级、是否需 Capability、是否需质量通过、在哪个进程执行）。
2. **`ToolBroker.execute()` 七步执行序**：存在性 → 要求一致性 → 验签 → 绑定 → 原子消费 → 前置条件 → args 与执行。
3. **Capability 签发与验证**：Ed25519（EdDSA）签名，claims 含四项绑定 + 有效期。
4. **`ReplayGuard`**：Redis `SETNX` + PG 条件 `UPDATE` 双保险的单次消费。
5. **拒绝语义**：`RejectStage`（第几步被拒）+ `reject_reason`（为什么），**每次拒绝都入账**。
6. **证据账本**（be-06 的实现体，本 spec 只列接口，细节见 M1-05）。

### 1.2 明确不做

- **不做风险评级**（PDP 归 be-11）。本模块只接受 `ToolSpec.risk` 这个常量。
- **不做审批**（be-13）。本模块只校验 Capability 有效，不管它是怎么来的。
- **不做执行**（be-15）。执行器由 `clients/` 注册，本模块只按注册表分派。
- **不做 Outbox 投递**（M3，见 §7）。
- **不依赖 `services/` 与 `api/`**（CLAUDE.md §4.4）。这是硬约束：
  安全内核若能被业务层反向影响，「谁在什么条件下放行了什么」就说不清。

---

## 2. 分层落点

| 层 | 文件 | 状态 |
|---|---|---|
| `harness/` | `tools.py` 注册表 + 6 个 `ToolSpec` + args 模型基类 | 本次新建 |
| `harness/` | `broker.py` `ToolBroker` / `ExecutionContext` / `ToolResult` | 本次新建 |
| `harness/` | `capability.py` `CapabilityIssuer` / `CapabilityVerifier` / claims / binding | 本次新建 |
| `harness/` | `replay.py` `ReplayGuard` | 本次新建 |
| `harness/` | `ledger.py` `EvidenceLedger`（be-06 主体） | 本次新建 |
| `harness/` | `errors.py` `RejectStage` + 三个异常类 + `reject_stage_of` | 本次新建 |
| `core/` | `core/redis.py` 补 `KEY_CAP_CONSUMED` 键模板 | 本次修改 |
| `models/` | `models/governance.py::Capability` | 📄 已实现，本次不改 |

---

## 3. 涉及的表

| 表 | 动作 | 关键约束 |
|---|---|---|
| `capability` | INSERT（签发）/ UPDATE（消费标记） | `uq_capability_jti`；`consumed_at IS NULL` 是消费的原子条件 |
| `evidence_event` | INSERT | 见 M1-05 |
| `evidence_ledger_block` | INSERT | 见 M1-05 |

### 3.1 🔴 `capability` 一行 = 一张**已签发**的令牌

be-05 §5.1 与 §6.4 互相矛盾：§5.1 说签发时就插行（只落 `token_sha256`），
§6.4 说消费时 INSERT 新行并捕获唯一约束冲突。**以 §5.1 为准**——
按 §6.4 实现的话，每次正常消费都会命中 `uq_capability_jti` 而被判成重放，
系统根本跑不起来。

消费 = 把已存在的行标记为已用，原子性由**单条 UPDATE 的 `WHERE consumed_at IS NULL`**
保证（PG 会对该行加锁，第二个事务影响 0 行 → `REPLAYED`）。

---

## 4. 工具注册表（本模块的对外主契约）

| ToolName | risk | 需 Capability | 需质量通过 | executor | 说明 |
|---|---|---|---|---|---|
| `seatunnel.submit` | P1 | ✅ | ❌ | worker | 提交作业 |
| `seatunnel.stop` | P2 | ✅ | ❌ | worker | 停止作业 |
| `target.publish_swap` | **P0** | ✅ | **✅** | worker | 原子换表；质量未过即拒 |
| `target.rollback_cleanup` | P1 | ✅ | ❌ | worker | 回滚清理 |
| `datasource.connectivity_test` | P3 | ❌ | ❌ | api | 只读握手 |
| `metadata.probe` | P3 | ❌ | ❌ | api | 只读探查 |

> 逐条引《需求对齐记录》§二.3 的风险矩阵；**注册表是唯一来源**，
> 任何模块不得在别处再写一份「这个工具要不要 Capability」。

### 4.0 Python 跨模块契约

```python
# harness/tools.py
def register_tool(spec: ToolSpec) -> None
def get_tool(tool: ToolName) -> ToolSpec
def register_executor(tool: ToolName, fn: ToolExecutor) -> None
def get_executor(tool: ToolName) -> ToolExecutor
def missing_executors() -> tuple[ToolName, ...]      # 启动自检用

# harness/broker.py
@dataclass(frozen=True, slots=True)
class ExecutionContext:
    request_id: str; tenant_id: int; project_id: int
    actor_id: int | None; caller: Literal["api", "worker", "beat"]; run_id: int | None

async def ToolBroker.execute(
    tool: ToolName, token: str | None, args: Mapping | BaseModel,
    ctx: ExecutionContext, *, session: AsyncSession, now: datetime | None = None,
) -> ToolResult

# harness/capability.py
async def CapabilityIssuer.issue(*, claims: CapabilityClaims) -> IssuedCapability
async def CapabilityVerifier.verify(token: str, session, ctx) -> CapabilityClaims
def CapabilityVerifier.assert_binding(claims: CapabilityClaims, expected: ExpectedBinding) -> None

# harness/replay.py
async def ReplayGuard.consume(claims, session, *, run_id: int | None = None) -> ConsumptionResult

# harness/errors.py
class RejectStage(StrEnum): LOOKUP REQUIREMENT SIGNATURE BINDING REPLAY ARGS EXECUTOR
def reject_stage_of(exc: AppError) -> RejectStage
def reject_reason_of(code: ErrorCode) -> str | None
```

`ExpectedBinding` 有 **6 项**（`subject_id` / `tool` / `env` / `artifact_sha256` /
`tenant_id` / `project_id`），比 be-05 §3.2 多一个 `project_id`：
§7 拒绝矩阵第 10 条要求「`tenant_id`/`project_id` 与上下文不符 → `TENANT_MISMATCH`」，
而 claim 里两者都在。少了它，**同租户内跨项目**拿令牌就能通过校验——
项目边界在本系统里是数据隔离边界（每个项目独立数据源与目标库）。

---

## 5. 核心逻辑

### 5.1 七步执行序

| 步 | 动作 | 拒绝码 |
|---|---|---|
| ① | `get_tool()` 存在性 | `UNAUTHORIZED_TOOL` |
| ② | 要求一致性（Capability 有无 / 进程） | `CAPABILITY_INVALID` / `UNAUTHORIZED_TOOL` |
| ③ | Ed25519 验签 + claim 结构 + 有效期 | `CAPABILITY_INVALID` / `TOKEN_EXPIRED` |
| ④ | 绑定比对（先 ctx 后 DB 重算） | `TENANT_MISMATCH` / `FINGERPRINT_MISMATCH` |
| ⑤ | 原子消费 | `CAPABILITY_REPLAYED` |
| ⑥ | 前置条件（preparation 状态与有效期、质量契约） | `PREPARATION_EXPIRED` / `POLICY_BLOCKED` |
| ⑦ | args 校验 + 执行器 | `VALIDATION_ERROR` / 执行器异常 |

**顺序不是任意的**：

- **④a 先比上下文再查库**：`_assert_ctx` 不需要 DB；放到 `_expected_binding` 之后的话，
  跨租户调用会先撞上「preparation 不存在」，拿到 `NOT_FOUND` 而不是 `TENANT_MISMATCH`。
- **`now` 只取一次**：验签、过期、账本时间戳共用一个「现在」。跨秒执行时会出现
  「验签说过期了、账本说还没到」这种自相矛盾的证据。
- **⑤ 在 ⑥ 之前**：消费必须在任何可能有副作用的前置检查之前完成占位，
  否则「质量检查通过 → 消费」之间有一个窗口能被并发插入。

### 5.2 🔴 本次修正：进程检查是**单向**的

原实现要求 `spec.executor == ctx.caller`。但两个 P3 只读工具声明 `executor="api"`，
实际却因超时考虑投给 Celery，于是 `services/datasource.run_probe` 只能传
`caller="api"`——**它在 worker 进程里说了假话**。

单向化后的规则：

```
allowed = (spec.executor == ctx.caller) or (ctx.caller == "worker" and spec.executor == "api")
```

- **api 进程不得执行 worker 工具**：那是绕过审批直接提交作业的最短路径，
  这条必须堵死，也是本检查存在的**唯一理由**。
- **worker 执行声明为 api 的只读工具**：不放行也不该拒。worker 本来就持有作业提交权，
  让它多跑一次只读探测不增加任何攻击面；拒掉反而迫使调用方伪造 `caller`
  ——那等于让这道检查建立在谎言上，比放宽更糟。

### 5.3 拒绝必须入账

每次拒绝都追加一条 `CAPABILITY_REJECTED`（或对应类型）事件，payload 含
`reject_stage` 与 `reject_reason`。只回一个 403 而不留痕，事后无法回答
「谁在什么时候试过用哪张令牌做什么」——而那正是 PRD §1.4
「未授权/重放成功率 = 0」需要能自证的东西。

`_safe_detail` 的 key 白名单（`row_count` / `profile_id` / `version` / `tables_count` /
`error_code` / `message` / `interrupted`）刻意**不含任何能承载远端数据或凭据的键**，
且任何 Mapping/list/tuple/set 值一律丢弃。

### 5.4 启动自检

`missing_executors()` 在应用启动时被调用：注册了 `ToolSpec` 却没有执行器的工具
会让 `execute()` 在最后一步才炸，而那时消费标记已经写下去了——
一张被消费掉却什么也没做的令牌。启动即失败比运行到第 7 步才发现好得多。

### 5.5 安全红线落地

| 红线 | 本模块怎么做 |
|---|---|
| 副作用唯一出口 | 本模块**就是**那个出口。`api/`/`services/` 只能经 `execute()` |
| PDP 必须确定性 | `ToolSpec.risk` 是常量表，不接受运行时输入；PDP 在 be-11 |
| Capability 单次消费 | Redis `SETNX` + PG 条件 `UPDATE`，无检查-写入窗口；Redis 不可达**直接拒**（fail-closed） |
| 证据账本单入口 | 拒绝与执行都经 `ledger.append()` |
| 无明文 Secret | `_ToolArgs._no_credentials` 对**值形态**做校验（不是键名，否则合法字段 `secret_ref` 会被自己拦下）；`_safe_detail` 白名单 |

---

## 6. 错误码

| 触发 | 码 | HTTP | 写账本 |
|---|---|---|---|
| 工具未注册 | `UNAUTHORIZED_TOOL` | 403 | 是 |
| 该带令牌却没带 / 免签却带了 | `CAPABILITY_INVALID` | 403 | 是 |
| 签名不符 / 结构坏 / 签发者不符 / TTL 不符 | `CAPABILITY_INVALID` | 403 | 是 |
| 令牌过期 | `TOKEN_EXPIRED` | 401 | 是 |
| 租户或项目与上下文不符 | `TENANT_MISMATCH` | 403 | 是 |
| 指纹（`artifact_sha256`）与当前不可变制品不符 | `FINGERPRINT_MISMATCH` | 409 | 是 |
| 重放 | `CAPABILITY_REPLAYED` | 409 | 是 |
| preparation 过期 / 未批准 | `PREPARATION_EXPIRED` | 409 | 是 |
| 质量未过 / 已中断却调用需质量通过的工具 | `POLICY_BLOCKED` | 403 | 是 |
| args 模型校验失败 | `VALIDATION_ERROR` | 422 | 是 |

**全部 12 条都写账本**。这不是「重要才写」的取舍，而是 `_reject()` 是唯一出口
的结构性结果。

---

## 7. 异步任务与尚未实现的部分

| 组件 | 状态 | 说明 |
|---|---|---|
| `harness/outbox.py` | ❌ **未实现** | CLAUDE.md §3 把它列在 `harness/` 下，M1 未落。当前只有 `tasks/outbox.py` 的骨架（beat 每 2s，`implemented=False`） |
| Outbox 投递 | ⏳ M3 | 落地时必须用 `SELECT ... FOR UPDATE SKIP LOCKED` 扫表，并按 `available_at` 退避 |
| 账本检查点签名验证 | ⏳ M4 | `verify_chain` 只重算哈希链，**不验 `evidence_ledger_block.signature`**；`LedgerBreakReason` 里的 `signature_invalid` 因此暂无产生路径 |
| 断链重试 | ✅ 已实现 | `is_retryable()` + `retry_delay()`，串行化失败时调用方重试 |

`tasks/outbox.py` 的 docstring 必须保留 `TODO(M3)` 标记：一个静默 no-op 的 beat 任务
看起来像「投递正常，只是没有消息」，而真实原因是它压根没实现。

---

## 8. 验收标准

1. **七步逐一可拒**：每一步都有一条用例构造出该步的失败，并断言 `reject_stage` 与错误码。
2. **重放成功率 = 0**：同一张令牌连续 `execute()` 两次，第二次必 `CAPABILITY_REPLAYED`；
   并发两个协程同时消费，只有一个 `CONSUMED`。
3. **Redis 不可达 → 拒绝**（不是退化成只查 PG）。
4. **api 进程执行 worker 工具被拒**；worker 执行 api 只读工具放行（§5.2）。
5. **`_safe_detail` 丢弃非白名单键**与一切 Mapping/序列值——用一个塞了嵌套字典的执行器验证。
6. **不依赖 services/api**：`mypy` + 一条「harness 不 import services」的静态断言。
7. 免签工具带 token 也被拒（防止「多带一个更安全」的误解绕过免签路径）。
8. 启动自检：注册无执行器的工具后 `missing_executors()` 非空。

---

## 9. 测试数据

- 一对临时 Ed25519 密钥（测试内生成，**不入库、不进仓库**）；
- `ExecutionContext(caller="api"|"worker")` 两种；
- 一个「塞了嵌套 dict 与 list」的假执行器，用于验 `_safe_detail`。

---

## 10. 待确认

| # | 事项 | 现状 | 处置 |
|---|---|---|---|
| 1 | Outbox 未实现 | `harness/outbox.py` 不存在 | M3 落地；当前 `tasks/outbox.py` 是显式骨架 |
| 2 | 检查点签名不验 | `verify_chain` 只重算哈希 | M4 加 Ed25519 验签，届时 `signature_invalid` 才有产生路径 |
| 3 | `_safe_detail` 白名单 | 7 个键硬编码 | 新增工具若需要新键，**先问「这个键会不会承载远端数据」**，再决定加不加 |
| 4 | `capability` 行的清理 | 无 TTL 清理任务 | 令牌过期后行仍留着（审计需要）；M4 评估归档 |
| 5 | 进程检查的枚举 | `api`/`worker`/`beat` 三个 | 若将来有独立调度进程，`allowed` 规则要重新过一遍（§5.2） |

---

## 11. 变更记录

| 日期 | 变更 | 原因 |
|---|---|---|
| 2026-10-16 | 初版：be-05 落成 spec 并实现 | M1 安全内核落地 |
| 2026-10-16 | **修正 §6.4 的消费实现**：条件 UPDATE 而非 INSERT + 唯一约束冲突 | §6.4 与 §5.1 矛盾，照 §6.4 实现则每次正常消费都被判重放 |
| 2026-10-16 | **进程检查单向化** | 原「必须相等」迫使 worker 里的调用方伪造 `caller="api"`（§5.2） |
| 2026-10-16 | `ExpectedBinding` 增 `project_id` | 缺它则同租户跨项目可用同一张令牌 |
| 2026-10-16 | `_assert_ctx` 提到 `_expected_binding` 之前 | 跨租户调用原本拿到 `NOT_FOUND` 而非 `TENANT_MISMATCH` |
| 2026-10-16 | `_ToolArgs._no_credentials` 改为只校验**值**形态 | 校验键名时合法字段 `secret_ref` 会被自己拦下 |
