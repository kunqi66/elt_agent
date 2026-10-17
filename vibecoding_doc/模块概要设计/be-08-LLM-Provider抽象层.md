# be-08 LLM Provider 抽象层

> 上游：[概要设计总纲.md](../概要设计总纲.md) §6 ｜ [PRD.md](../PRD.md) §6.3.2
> 数据：[核心数据字典.md](../核心数据字典.md) §3（`pipeline_version.generation_context`）、§6（`benchmark_run`）
> 决策：[需求对齐记录.md](../需求对齐记录.md) §二.2（只真接 DeepSeek，生成 8K token 预算，Benchmark 缓存回放）
> 代码位置：`backend/clients/llm/` ｜ 里程碑：M2 ｜ 依赖：be-01

## 1. 职责与边界

- **做什么**：
  - 定义与模型厂商无关的 `LLMProvider` Protocol，承载「文本补全 / 结构化 JSON 补全 / 流式补全」三种调用形态。
  - 实装 DeepSeek：`deepseek-chat` 负责生成与修复，`deepseek-reasoner` 负责根因诊断，按任务分流。
  - 统一重试、退避、超时、限流退避；把厂商 HTTP 异常归一为 `ErrorCode.LLM_UNAVAILABLE`。
  - 按 token 预算裁剪 prompt（默认 `settings.LLM_PROMPT_TOKEN_BUDGET`，以「相关表 + 统计 + 脱敏样本」为投喂内容）。
  - 结构化输出校验：JSON 解析失败时的围栏剥离、一次修复重试、失败即抛（**不静默返回兜底对象**）。
  - Benchmark 缓存回放：按「prompt 指纹 + 模型 + prompt 模板版本」缓存响应，供 L0/L1 离线可复现评测。
  - 出口侧上下文边界断言：任何出站消息必须过一遍脱敏与禁投喂模式检查。
- **不做什么**：
  - **不做业务决策**：不判断需求是否合理、不决定缺哪些参数（那是 `workflow/` 与 `engines/plan/validate.py`）。
  - **不做风险评级**：P0–P3 由 [be-07](be-07-PDP风险策略引擎.md) 的规则表给出，本模块不允许出现风险分级代码（PRD §1.4）。
  - 不访问数据库 / Redis / Vault；不写证据账本（账本由调用方 [be-09](be-09-LangGraph工作流与三层状态.md) 写）。
  - 不做 EtlPlan / HOCON 的结构合法性判定（那是 [be-10](be-10-EtlPlan与HOCON生成.md) 的纯函数）。
  - 不缓存跨租户可复用的业务结论；缓存只用于 Benchmark 回放，且内容本身不含租户数据以外的信息。
- **所在分层**：`clients/`（外部系统封装）。依赖方向：`clients/llm` → `core/`（config、errors、ids、logging）。**禁止**被 `engines/` 依赖；`services/` `workflow/` `tasks/` 只允许通过 `clients/llm/registry.py` 取 provider，不得直接 `import clients.llm.deepseek`（PRD §6.3.2「业务层不得依赖具体模型实现」）。

## 2. 模块结构

```
backend/clients/llm/
├── __init__.py            # 只导出 registery.get_provider 与 DTO，屏蔽具体实现
├── base.py                # LLMProvider Protocol + 消息/工具/结果 DTO（待建）
├── deepseek.py            # DeepSeekProvider：HTTP、重试、JSON 模式、reasoner 适配（待建）
├── registry.py            # build_provider() / get_provider()，按 Settings.LLM_PROVIDER 选实现（待建）
├── token_budget.py        # 纯函数级 prompt 裁剪（待建）
├── scrub.py               # 出站上下文边界断言（待建）
├── cache.py               # Benchmark 缓存回放的读写（MinIO 对象存储）（待建）
└── prompts/
    ├── __init__.py        # PROMPT_VERSIONS 注册表：模板名 → 版本号（待建）
    ├── intent.py          # 意图解析与参数抽取模板（待建）
    ├── plan.py            # EtlPlan 生成模板（含 JSON 输出契约）（待建）
    ├── hocon.py           # SeaTunnel HOCON 生成模板（待建）
    ├── autofix.py         # 门禁 finding → 补丁模板（待建）
    └── diagnose.py        # 根因诊断模板（reasoner，M4 由 be-19 使用）（待建）
```

## 3. 对外接口

### 3.1 HTTP

无。本模块不出路由，是纯客户端封装。

### 3.2 Python

```python
# clients/llm/base.py
TaskKind = Literal["generate", "diagnose"]   # 任务分流，决定模型与能力开关

class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str
    tool_call_id: str | None = None

class ToolSpec(BaseModel):
    name: str
    description: str
    parameters: dict[str, Any]          # JSON Schema

class ToolCall(BaseModel):
    id: str
    name: str
    arguments_json: str                 # 厂商原样返回的字符串，由调用方解析

class CompletionRequest(BaseModel):
    task: TaskKind
    messages: list[ChatMessage]
    tools: list[ToolSpec] = []
    json_mode: bool = False             # True 时要求厂商返回 JSON 对象
    temperature: float = 0.0            # 生成类任务默认 0，尽量降低不确定性
    max_output_tokens: int = 4096
    prompt_version: str                 # 来自 prompts.PROMPT_VERSIONS，写入审计
    request_id: str                     # 透传用于日志关联，不发给厂商

class TokenUsage(BaseModel):
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int

class CompletionResult(BaseModel):
    text: str
    tool_calls: list[ToolCall] = []
    usage: TokenUsage
    model: str                          # 厂商返回的实际模型名（如 deepseek-chat）
    model_version: str                  # 厂商返回的版本串，写 generation_context
    finish_reason: str
    cached: bool = False                # 来自 Benchmark 缓存回放

class LLMProvider(Protocol):
    name: str

    async def complete(self, req: CompletionRequest) -> CompletionResult: ...
    async def complete_json(self, req: CompletionRequest, schema: type[TModel]) -> TModel: ...
    def stream(self, req: CompletionRequest) -> AsyncIterator[str]: ...
```

```python
# clients/llm/registry.py
def build_provider(name: str | None = None) -> LLMProvider: ...
def get_provider() -> LLMProvider: ...   # lru_cache，读 settings.LLM_PROVIDER

# clients/llm/token_budget.py —— 全部为纯函数（无 IO、无网络、无时间）
def fit_prompt(blocks: Sequence[PromptBlock], *, budget: int) -> FittedPrompt: ...
def estimate_tokens(text: str) -> int: ...

# clients/llm/scrub.py —— 纯函数
def assert_outbound_safe(messages: Sequence[ChatMessage]) -> None: ...
def scrub_text(text: str) -> str: ...

# clients/llm/cache.py
class LLMCache(Protocol):
    def key_of(self, req: CompletionRequest, model: str) -> str: ...
    async def get(self, key: str) -> CompletionResult | None: ...
    async def put(self, key: str, result: CompletionResult) -> None: ...
```

**参数与异常契约**：

| 函数 | 输入 | 输出 | 抛出的异常（→ 错误码） |
|---|---|---|---|
| `complete()` | `CompletionRequest` | `CompletionResult` | `LLMUnavailableError`（`LLM_UNAVAILABLE`）；`LLMContextViolationError`（`INTERNAL_ERROR`，见 §7） |
| `complete_json()` | 同上 + Pydantic 模型类 | 模型实例 | 上述 + `StructuredOutputError`（由调用方映射为 `PLAN_INVALID` / `HOCON_SYNTAX_ERROR` / `SCHEMA_MISMATCH`） |
| `fit_prompt()` | 块列表 + 预算 | `FittedPrompt(blocks, dropped_keys, token_estimate)` | `PromptTooLargeError`（`VALIDATION_ERROR`） |
| `assert_outbound_safe()` | 消息列表 | `None` | `LLMContextViolationError` |

## 4. 数据模型

本模块**不拥有任何表**，只读两个下游写入点的字段口径：

| 表 | 字段 | 本模块的关系 |
|---|---|---|
| `benchmark_run` | `cache_mode: bool`、`model_version`、`prompt_version` | 读：`cache_mode=True` 时启用缓存回放；三个字段是缓存有效性的前提（缓存键含 model 与 prompt_version） |
| `pipeline_version` | `generation_context: jsonb` | 本模块产出 `GenerationContext` DTO，由 [be-12](be-12-版本冻结与制品指纹.md) 在冻结时写入；**不含**任何样本原文 |
| `metadata_profile` | `schema_snapshot`、`masked_samples` | 间接读：由 [be-09](be-09-LangGraph工作流与三层状态.md) 的 `metadata_bind` 节点取出后拼成 `PromptBlock` 传入，本模块不查库 |

`GenerationContext`（本模块返回、be-12 落库）：

```python
class GenerationContext(BaseModel):
    provider: str                 # "deepseek"
    model: str                    # "deepseek-chat"
    model_version: str            # 厂商返回版本串
    prompt_versions: dict[str, str]   # {"plan": "1.0", "hocon": "1.0", "autofix": "1.0"}
    autofix_rounds: int
    usage_total: TokenUsage
    generated_at: datetime        # 仅记录，不参与指纹（见 be-12 §6）
```

## 5. 核心流程

### 5.1 一次生成调用

1. 调用方（be-09 的 `generate_plan` 节点）从 `metadata_profile` 取出**相关表**的 schema、近似统计与脱敏样本，构造 `PromptBlock` 列表。
2. `fit_prompt(blocks, budget=settings.LLM_PROMPT_TOKEN_BUDGET)` 裁剪，得到 `FittedPrompt`。
3. 由 `prompts/plan.py` 组装 `CompletionRequest`（含 `prompt_version`、`json_mode=True`、`temperature=0.0`）。
4. `assert_outbound_safe(req.messages)` —— **出站前的最后一道闸**；命中禁投喂模式即抛，不发出请求。
5. `provider.complete_json(req, EtlPlanModel)`；DeepSeek 侧按 `task` 选模型（§6.1）。
6. 解析失败 → 围栏剥离 → 一次修复重试 → 仍失败抛 `StructuredOutputError`。
7. 返回 `(EtlPlanModel, GenerationContext片段)`，由调用方继续走门禁。

### 5.2 Benchmark 缓存回放

```
cache_mode=False（真实生成）             cache_mode=True（回放）
  ├─ scrub → provider.complete ──┐         ├─ key = cache.key_of(req, model)
  │                              │         ├─ cache.get(key)
  │                              ▼         │    ├─ 命中 → 直接返回（cached=True，不发网络请求）
  │                        cache.put(key)  │    └─ 未命中 → 走真实调用并 put（首次跑基线时自然写入）
  └─ 返回                                  └─ 返回
```

缓存对象存放于 MinIO（`settings.MINIO_BUCKET`，前缀 `llm-cache/{key[:2]}/{key}.json`），不新建表；缓存内容为 `CompletionResult` 的规范化 JSON。

**无状态机**：本模块不持有 Pipeline 状态；`PipelineStatus` 的转移由 be-09 负责，故本节不含状态转移表。非法调用（如 `task="diagnose"` 却传 `tools`）在 `registry` 构造请求时即 `ValidationError` 拒绝。

## 6. 关键算法与规则

### 6.1 任务到模型的分流

| `TaskKind` | 模型（Settings） | 能力开关 | 使用方 |
|---|---|---|---|
| `generate` | `LLM_MODEL_GENERATE`（`deepseek-chat`） | 允许 `json_mode`、`tools`、`temperature` | be-09 意图/参数/生成、be-11 自动修复 |
| `diagnose` | `LLM_MODEL_DIAGNOSE`（`deepseek-reasoner`） | **禁用** `tools` 与 `json_mode`（厂商对推理模型的两者不支持） | be-19 根因诊断（M4） |

`diagnose` 需要结构化结果时，改为「prompt 内嵌 JSON 契约 + 从输出里提取围栏代码块」，提取失败同样走一次修复重试。推理模型返回的 `reasoning_content` **不进入**任何持久化字段，只允许本地日志以 `debug` 级别记录长度。

### 6.2 重试与退避

- 总尝试次数 `1 + settings.LLM_MAX_RETRY`（默认 3）。仅对**可重试**失败重试：连接错误、读超时、HTTP 429、HTTP 5xx。
- **不重试**：HTTP 401/403（凭据问题，重试无意义且会放大风控）、HTTP 400（请求非法）、`StructuredOutputError` 的首次失败（由 `complete_json` 内部的修复重试单独处理，不叠加）。
- 退避：`wait = min(2 ** (attempt - 1), 8)` 秒，叠加 ±20% 抖动；HTTP 429 若带 `Retry-After` 则以该值为准（上限 30 s）。
- 单次请求超时：`httpx.Timeout(connect=10, read=settings.LLM_TIMEOUT_SEC, write=10, pool=5)`；`LLM_TIMEOUT_SEC` 默认 90。
- 重试耗尽 → `LLMUnavailableError`，`details={"attempts": n, "last_status": code}`（**不含**响应体，避免回显可能含凭据的报文）。

### 6.3 prompt token 预算与裁剪

输入 `blocks: Sequence[PromptBlock]`（每块含 `key / priority / text`），预算默认 8000。裁剪是**纯函数**：同输入必得同输出，便于单测与 Benchmark 复现。

保留优先级（0 最不可动）：

| priority | 内容 | 裁剪动作 |
|---|---|---|
| 0 | system 指令、输出 JSON 契约、用户需求原文、`collected_params` | 永不裁剪；若其和已超预算 → `PromptTooLargeError` |
| 1 | 源/目标相关表的字段名、类型、可空、主键 | 最后压缩：只保留用户已提及或已映射的字段 |
| 2 | 相关表的近似统计（`row_count_approx` / `null_ratio` / `distinct_approx`） | 先丢统计，再丢整块 |
| 3 | 脱敏样本行（`masked_samples`） | 逐表对半削减行数（5→3→2→1），再整块丢 |
| 4 | 字段注释、枚举值列表 | 最先整块丢弃 |

算法：

1. 计算 `T0` = priority 0 各块之和；`T0 > budget` → 抛 `PromptTooLargeError`（调用方应缩短需求或减少绑定表）。
2. 按 priority 从高到低（4 → 3 → 2 → 1）逐块施加「裁剪动作」，每轮重算总量。
3. 当 `total <= budget * 0.95` 时停止（留 5% 余量给输出契约与厂商侧差异）。
4. 走完全部动作仍超 → 抛 `PromptTooLargeError`。

`estimate_tokens(text)` 采用启发式：`ceil(len(text) / 2.2)`（对中文为主的文本偏保守），再加 5% 安全系数。**不引第三方 tokenizer**——见 §10。

### 6.4 结构化输出校验（`complete_json`）

1. 调 `complete()` 取得 `text`。
2. 剥离常见围栏（带 `json` 语言标记的围栏、无语言标记的裸围栏），去首尾空白。
3. `schema.model_validate_json(text)`；Pydantic 校验含类型、必填、枚举取值。
4. 失败 → 追加一条 `role="user"` 的修复指令（**只描述校验错误的字段路径与原因，不回显模型原文**），重试一次。
5. 二次失败 → `StructuredOutputError(details={"schema": schema.__name__, "error_paths": [...]})`；由调用方决定映射为 `PLAN_INVALID`（plan schema 不符）或 `HOCON_SYNTAX_ERROR`（HOCON 文本不可解析）。
6. **禁止**用空对象、默认值或截断结果兜底——那会把「模型没答对」伪装成「生成成功」，直接破坏门禁的意义。

### 6.5 缓存键

```
key = sha256(canonical_json({
    "v": 1,
    "model": model,                      # deepseek-chat / deepseek-reasoner
    "prompt_version": req.prompt_version,
    "task": req.task,
    "json_mode": req.json_mode,
    "temperature": req.temperature,
    "messages": [{"role": m.role, "content": m.content} for m in req.messages],
}))
```

- `canonical_json` 复用 `core/ids.py`（键排序、无空白、UTF-8）。
- **必须**排除 `request_id`、时间戳、`max_output_tokens` 之外的运行期参数；缓存键一旦含时间就会永远不命中。
- 缓存仅在 `benchmark_run.cache_mode=True` 时读写；真实生成路径不读缓存（避免跨租户复用旧结论）。

### 6.6 出站上下文边界（`scrub.py`）

两道检查，均为纯函数、无网络：

1. **禁投喂模式**（命中即抛，不可配置关闭）：连接串（`scheme://user:pass@host`）、`password=`/`passwd=`/`token=`/`secret_key=`/`access_key=` 形式的键值、`-----BEGIN ... PRIVATE KEY-----`、Vault Token 前缀、DeepSeek 风格的 `sk-` 开头长串、以及 `secret_reference.path` 之外的任何 Vault 路径。
2. **脱敏兜底**：把疑似手机号（11 位数字）、身份证（18 位）、邮箱、银行卡号替换为掩码后再送出；替换计数写入日志字段 `scrubbed_count`（不记原文）。

规则：**任何 provider 实现的 `complete()` 入口都必须先调用 `assert_outbound_safe()`**；这是 provider 的通用前置，不依赖调用方自觉。若因特殊场景确实需要放宽，只能在 `PromptBlock` 构造阶段完成降敏，而不是关掉断言。

## 7. 错误码与拒绝语义

| 触发条件 | 错误码（`core/errors.ErrorCode`） | HTTP | 写证据账本 |
|---|---|---|---|
| 重试耗尽 / 连接失败 / 5xx / 429 仍失败 / 401、403 | `LLM_UNAVAILABLE` | 503 | 否（由 be-09 决定是否记为生成失败） |
| `fit_prompt` 在 priority 0 阶段即超预算 | `VALIDATION_ERROR` | 422 | 否 |
| `complete_json` 两次解析失败 | 由调用方映射：`PLAN_INVALID` / `HOCON_SYNTAX_ERROR` / `SCHEMA_MISMATCH` | 422 / 422 / 409 | 否 |
| 出站消息命中禁投喂模式 | `INTERNAL_ERROR`（**建议新增专用码 `LLM_CONTEXT_VIOLATION`**，见 §10） | 500 | 否；但必须 `log.error` 并告警——这是安全红线被触碰的信号 |
| `task="diagnose"` 却携带 tools / 未知 provider 名 | `VALIDATION_ERROR` | 422 | 否 |

本模块**不写证据账本**：LLM 调用不产生对外部系统的写副作用，也未注册为 `ToolName`（《概要设计总纲》§5 工具注册表只有 6 个工具）。与之相关的事件由上层写入：生成成功由 be-09 写 `PIPELINE_GENERATED`，门禁由 be-11 写 `PIPELINE_GATE_EVALUATED`，冻结由 be-12 写 `PIPELINE_VERSION_FROZEN`。

## 8. 安全与合规约束

- **无明文 Secret**：`settings.LLM_API_KEY` 是 `SecretStr`；日志只允许出现 `***configured***`（复用 `settings.safe_dump()` 口径）。请求头 `Authorization` 永不进日志。
- **上下文边界**（PRD §10.1、总纲 §6）：只投喂 Schema、字段类型、近似统计与**已脱敏**样本；连接串、Secret、`secret_reference` 值、未脱敏样本一律禁止。落点即 §6.6 的 `assert_outbound_safe()`。
- **脱敏来源**：`masked_samples` 在探查阶段已脱敏（[be-04](be-04-元数据探查与脱敏.md)），本模块只做**二次兜底**，不承担首轮脱敏责任。
- **审计**：每次调用记 `structlog` 一条，字段含 `request_id / tenant_id / project_id / prompt_version / model / model_version / prompt_tokens / completion_tokens / latency_ms / attempts / cached`；**不记 prompt 与响应正文**。
- **可复现性**：`model_version` 与 `prompt_version` 必须落到 `pipeline_version.generation_context`，使「同一需求为何生成不同方案」可被追溯（PRD §6.3.2）。
- **租户隔离**：缓存与日志按 `tenant_id` 分域；跨租户复用缓存属于越权，禁止。

## 9. 测试要点

**单元测试（纯函数，无网络）**
- `fit_prompt`：priority 0 超预算必抛；裁剪顺序符合 4→3→2→1；裁剪后总量 ≤ `budget*0.95`；同输入两次调用结果逐字节相同。
- `estimate_tokens`：空串为 0；纯 ASCII 与纯中文的单调性；不出现负数。
- `cache.key_of`：同一请求稳定；改动 `request_id`/时间不影响键；改动 `messages`/`model`/`prompt_version` 必变键（**反例**：只改 `request_id` 导致缓存永不命中）。
- `scrub.assert_outbound_safe`：连接串、`password=xxx`、PEM 私钥、`sk-` 长串、Vault Token 各命中一次；正常 Schema 文本不误伤。
- 围栏剥离：带语言标记的围栏、无语言标记的裸围栏、无围栏、前后夹带解释文字，四种输入都要能解析。

**集成测试（httpx.MockTransport，无真实网络）**
- 429 带 `Retry-After` → 按该值退避；5xx 两次后成功 → `attempts=3`；三次均失败 → `LLM_UNAVAILABLE`。
- 401 不重试（断言只发一次请求）。
- `complete_json` 首次非法 JSON → 修复重试后成功，且修复消息**不含**原文回显。

**必须覆盖的反例**
- **超预算**：需求原文本身超过 8000 token → `VALIDATION_ERROR`，且**不发出**网络请求。
- **禁投喂**：prompt 中混入连接串 → 抛异常且请求未出站（用 MockTransport 断言零调用）。
- **缓存回放**：`cache_mode=True` 且缓存命中时，MockTransport 断言零网络调用且 `result.cached is True`。
- 换 `prompt_version` 后缓存不再命中（防止「模板改了还在吃旧缓存」）。
- 真实 DeepSeek 调用标 `@pytest.mark.integration`，默认跳过，仅在 ECS 上跑。

## 10. 未决与风险

1. **token 估算精度**：启发式 `len/2.2` 与 DeepSeek 实际计数存在偏差，预算 8000 是保守值。**验证方式**：M2 在 ECS 上跑 10 条 Benchmark L0 用例，对比 `usage.prompt_tokens` 与估算值，偏差 > 25% 则改为按字符类别加权估算，或引入轻量 tokenizer。
2. **`deepseek-reasoner` 的能力边界**：其不支持 `tools` 与 `json_mode` 是基于当前 API 文档的假设。**验证方式**：M4 接 be-19 时用一次真实调用确认；若厂商新增支持，`registry` 的能力开关表同步更新。
3. **缺少上下文越界专用错误码**：当前只有 `INTERNAL_ERROR` 可用，语义不明确且会污染 500 比例指标。**建议新增 `ErrorCode.LLM_CONTEXT_VIOLATION`**（HTTP 500，必须告警）。该项需拍板后由 be-01 落实。
4. **是否纳入 ToolBroker**：LLM 调用是外部网络 IO 但**不改变外部系统状态**，故未注册为 `ToolName`。若后续要求「所有出网调用可审计可熔断」，需在 [be-05](be-05-Harness内核与工具代理.md) 增设只读工具类型，本模块改为经 broker 调用。**验证方式**：安全评审结论。
5. **整体超时预算**：单次 90 s × 3 次尝试 ≈ 270 s，可能超过 be-09 节点超时。**待定**：由 be-09 定义节点级超时并回传剩余预算，本模块按剩余时间缩短重试。验证方式：M2 用慢响应 MockTransport 压测。
6. **缓存对象存储位置**：现选 MinIO。若 Benchmark 需要离线（无 MinIO）复现，需退化为本地文件缓存目录。

## 11. 变更记录

| 日期 | 变更 | 原因 |
|---|---|---|
| 2026-10-16 | 首版 | M2 生成链路设计 |
