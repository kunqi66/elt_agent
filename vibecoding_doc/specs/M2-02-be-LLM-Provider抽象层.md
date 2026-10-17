# M2-02 · be-08 LLM Provider 抽象层

> 状态：**已实现**（协议 + DTO + DeepSeek 实现 + 确定性 Fake 实现 + 出站闸门 + token 裁剪 + 两个提示词模板；缓存回放与三个模板见 §7/§10）
> 本 spec 为实现后补写，记录**实际落地**的形态；与 be-08 设计文档不一致处已在 §10 登记。
> 期次：M2 ｜ 依赖：be-01（`core/config`、`core/errors`、`core/logging`）
> 被依赖：be-09（`workflow/nodes/*` 全部经本层调模型）、be-10（HOCON 渲染失败归因、自动修复提示词）、be-19（根因诊断，未落地）
> 设计来源：[be-08-LLM-Provider抽象层.md](../模块概要设计/be-08-LLM-Provider抽象层.md)
> 数据来源：本模块**不产生也不存储任何业务数据**（无表、无账本事件；`generation_context` 由 be-12 落库）
> 需求来源：[PRD.md](../PRD.md) §6.3.1、§6.3.2（多 Provider 统一适配 / 业务层不得依赖具体实现 / 展示模型生成版本）、§10.1（Secret 不得进入模型上下文）

---

## 1. 模块定位

### 1.1 做什么

1. **统一补全协议**：`LLMProvider`（`clients/llm/base.py:182`）只有三个方法——`complete` / `complete_json` / `stream`——上层永远依赖协议，不认识 `deepseek` 或 `fake` 这两个具体名字。
2. **按名字构造 + 进程级单例**：`build_provider(name)` 认 `"deepseek"` / `"fake"`，`get_provider()`（`registry.py:80`）是 `lru_cache(maxsize=1)` 单例。单例是为了不丢 `httpx.AsyncClient` 的连接池——每次新建等于每次重做 TLS 握手。
3. **任务到模型的分流**：`CompletionRequest.task`（`generate` / `diagnose`）在**构造期**就拒掉非法组合，见 §5.1。
4. **出站上下文边界**：`prepare_outbound()`（`scrub.py:132`）是**唯一**出站入口——先断言禁投喂模式，再兜底 PII 脱敏。
5. **提示词预算与裁剪**：`fit_prompt()`（`token_budget.py:185`）按优先级 0~4 装配，超预算先丢样本、最后丢档案。
6. **结构化输出**：围栏剥离 + **一次**修复重试，重试提示只回字段路径、不回显模型原文（§5.4）。
7. **确定性离线通路**：`FakeProvider`（`fake.py:268`）按围栏内容分派，产出的方案与修复补丁逐字节可复现——它是**一等公民**，不是测试开关（`registry.py:37` 注释）。

### 1.2 明确不做

- **不做编排**（be-09）。本层只管「发一次请求、拿一次结果」，不进 state、不写库、不落账本。
- **不做门禁与修复决策**（be-11）。本层不判断一份方案「对不对」，只负责把结构校验失败的模型输出**译成异常**。
- **不做 prompt 内容的业务判断**（be-09/be-10）。模板由各调用方模块提供（`prompts/plan.py` 属本层，`autofix` 属 be-10 的回路），本层只提供围栏原语（`fences.py`）与裁剪器。
- **不碰 Vault、不碰 PG、不碰 Redis**：本层唯一的 IO 是到模型厂商的 HTTP。密钥以 `SecretStr` 从 `core.config` 读入，只在 `httpx` 请求头里出现（`core/config.py:111`）。
- **不做模型族的路由与降级**：没有 key 时**显式报 503**，绝不静默换 `fake`（§5.6）。
- **不做 Benchmark 缓存回放**（§7）。

---

## 2. 分层落点

| 层 | 文件 | 状态 |
|---|---|---|
| `clients/llm/` | `__init__.py` 只导出协议/DTO/选择器，**不含**具体实现 | 📄 已有 |
| `clients/llm/` | `base.py` DTO（`ChatMessage` / `CompletionRequest` / `CompletionResult` / `TokenUsage` / `GenerationContext`）、`LLMProvider` 协议、3 个异常 | 📄 已有 |
| `clients/llm/` | `registry.py` `SUPPORTED_PROVIDERS` / `build_provider` / `get_provider` | 📄 已有 |
| `clients/llm/` | `deepseek.py` `DeepSeekProvider`：重试退避、SSE、`complete_json` 修复重试 | 📄 已有 |
| `clients/llm/` | `fake.py` `FakeProvider` + 6 个确定性修复器 + `apply_autofix` | 📄 已有 |
| `clients/llm/` | `scrub.py` 禁投喂模式表、PII 模式表、`assert_outbound_safe`、`prepare_outbound` | 📄 已有 |
| `clients/llm/` | `token_budget.py` `PromptBlock` / `FittedPrompt` / `estimate_tokens` / `fit_prompt` | 📄 已有 |
| `clients/llm/prompts/` | `__init__.py` `PROMPT_VERSIONS` 注册表 + `prompt_version()` | 📄 已有 |
| `clients/llm/prompts/` | `fences.py` `wrap` / `extract` / `extract_all` / `strip_code_fences` | 📄 已有 |
| `clients/llm/prompts/` | `plan.py` `plan_blocks` / `plan_messages` / `plan_request` + `PROMPT_VERSION_PLAN` | 📄 已有 |
| `clients/llm/prompts/` | `autofix.py` `AutofixFinding` / `autofix_blocks` / `autofix_request` + `PROMPT_VERSION_AUTOFIX` | 📄 已有 |
| `clients/llm/prompts/` | `intent.py` / `hocon.py` / `diagnose.py` | 🟡 未建（见 §7、§10.13） |
| `clients/llm/` | `cache.py` Benchmark 缓存回放 | 🟡 未建（见 §7、§10.11） |
| `core/` | `config.py` 8 个 `LLM_*` 配置项 | 📄 已有，本次不改 |

`clients/llm/__init__.py` 刻意**不导出** `DeepSeekProvider` / `FakeProvider`：于是「业务层不得依赖具体实现」在 review 时是一眼可查的事实，而不是一条约定。

---

## 3. 涉及的表

**无。** 本模块不读写任何数据库表、不写证据账本。

它是 be-09 生成链路唯一允许发起网络请求的地方，除此之外无副作用。`generation_context`（含模型名/版本、提示词版本）由调用方从 `GenerationContext` 取出后在 be-12 冻结时落进 `pipeline_version`（见字典 §3）——本层只负责构造这个模型，不负责落库。

---

## 4. 对外契约

### 4.1 HTTP

**无。** 本模块不注册任何路由，不被前端直接调用。

### 4.2 Python（跨模块契约）

```python
# clients/llm/__init__.py —— 唯一对外导出面
from clients.llm import (
    ChatMessage, CompletionRequest, CompletionResult, GenerationContext,
    LLMContextViolationError, LLMProvider, LLMUnavailableError, MessageRole,
    StructuredOutputError, TaskKind, TokenUsage, ToolCall, ToolSpec,
    SUPPORTED_PROVIDERS, build_provider, get_provider,
)

# clients/llm/base.py
class CompletionRequest(BaseModel):
    task: TaskKind = "generate"            # Literal["generate", "diagnose"]
    messages: list[ChatMessage]            # min_length=1
    tools: list[ToolSpec] = []
    json_mode: bool = False
    temperature: float = 0.0
    max_output_tokens: int = 4096
    prompt_version: str                    # 来自 prompts.PROMPT_VERSIONS
    request_id: str                        # 仅日志关联，不发厂商、不参与缓存键

class GenerationContext(BaseModel):        # frozen + extra="forbid"
    model_name: str; model_version: str; prompt_template_version: str; autofix_rounds: int
    provider: str = ""; prompt_versions: dict[str, str] = {}; usage_total: TokenUsage | None = None
    generated_at: datetime | None = None

class LLMProvider(Protocol):               # 刻意不 runtime_checkable
    name: str
    async def complete(self, req) -> CompletionResult: ...
    async def complete_json(self, req, schema: type[TModel]) -> TModel: ...
    def stream(self, req) -> AsyncIterator[str]: ...

# clients/llm/token_budget.py
def fit_prompt(blocks: Sequence[PromptBlock], *, budget: int = 8000) -> FittedPrompt: ...

# clients/llm/scrub.py
def prepare_outbound(messages: Sequence[ChatMessage]) -> tuple[list[ChatMessage], int]: ...
```

`CompletionRequest` 的字段形状是**硬契约**：`temperature` 默认 0.0（方案要被门禁逐条检查，模型越随机、同一需求两次生成差异越大）；`request_id` 明确写着「不发往厂商、不参与缓存键」，避免有人把它当缓存盐。

`GenerationContext` 的**前四个字段是前端契约**（`model_name` / `model_version` / `prompt_template_version` / `autofix_rounds`），逐字来自 `frontend/src/types/pipeline.ts`，不可改名、不可删；后四个是 be-08 §4 的审计附加项，前端不渲染但落库口径保留（见 §10.7）。

---

## 5. 核心逻辑

### 5.1 任务到模型的分流（构造期拒绝）

| `task` | 模型 | `tools` | `json_mode` |
|---|---|---|---|
| `generate` | `settings.LLM_MODEL_GENERATE`（默认 `deepseek-chat`） | 允许 | 允许 |
| `diagnose` | `settings.LLM_MODEL_DIAGNOSE`（默认 `deepseek-reasoner`） | **拒绝** 422 | **拒绝** 422 |

拒绝发生在 `CompletionRequest._check_task_capabilities`（`base.py:113`）——**构造期**，不是发请求时。理由（`base.py:90` 注释）：`deepseek-reasoner` 既不接受工具调用也不接受 `response_format`，带着它们发出去只会换回一个 400，而那时已经付了一次网络往返与一次风控计数。这也是 be-08 §5「非法调用在构造请求时即拒绝」的字面落地。

### 5.2 重试与退避

- `_RETRYABLE_STATUS = {429, 500, 502, 503, 504}`（`deepseek.py:47`），其余 4xx 不重试。
- 退避：`min(2 ** (attempt-1), 8)` 秒 ±20% 抖动（`deepseek.py:283`）。
- `Retry-After` 头优先，但**被封顶在 `_MAX_BACKOFF_SEC = 30.0`**（`deepseek.py:51`）：厂商要求等 10 分钟时不能真的等 10 分钟，否则一次生成会把 worker 占住。
- 重试耗尽 → `LLMUnavailableError`（503），`details["last_status"]` 带最后一次状态码给运维，**不进前端文案**（401/403 与 5xx 对用户是同一件事）。
- `httpx.Timeout(connect=10, read=settings.LLM_TIMEOUT_SEC, write=10, pool=5)`：读超时可配（默认 90s），连接/写入/取池各有独立上限。

`max_retry` 与 `sleep` / `jitter` 都可由构造参数注入，用例因此不必真的等——退避序列在 `test_deepseek_retries_5xx_then_succeeds` 里被断言为 `[1.0, 2.0]`。

### 5.3 prompt token 预算与裁剪

- `DEFAULT_PROMPT_BUDGET = 8000`，可由 `settings.LLM_PROMPT_TOKEN_BUDGET` 覆盖。
- `estimate_tokens = ceil(len(text) / 2.2)`（`token_budget.py:87`）——**刻意不含 5% 安全系数**（§10.12）。
- 装配算法（`fit_prompt`）：
  1. `T0` = priority 0 各块之和；`T0 > budget` → 抛 `PromptTooLargeError`，**不发网络请求**（这是「超预算」用例的断言点）。
  2. 从 priority 4 到 1 逐块施加阶梯动作，每步重算总量。
  3. 总量 `<= budget * 0.95` 即停手。
  4. 走完仍超 → 抛 `PromptTooLargeError`，`details` 里给出「可丢块共占多少 token」，供调用方判断是不是该缩短需求。
- 阶梯（`_LADDERS`，`token_budget.py:36`）：priority 4 = 直接丢；3 = 半、再半、丢；2 = 半、丢；1 = 半、半、丢。priority 0（需求 + 输出契约）只有一道闸，没有裁剪动作。
- 该函数用「只读 blocks + 可变 texts 快照」而不是 dict：块 key 重复时 dict 会静默丢块，而重复 key 恰恰是调用方的 bug，不该被吞掉。

### 5.4 结构化输出：围栏剥离 + 一次修复重试

`complete_json`（§6.4 的第 5 步）流程：

1. 调 `complete`；取回文本；
2. `strip_code_fences` 去掉模型爱包的 ```json 围栏；
3. 按 `schema.model_validate_json` 校验；
4. 失败 → **恰好一次**修复重试：把校验错误的**字段路径**拼成一条新消息发回；
5. 再失败 → `StructuredOutputError`。

**修复提示不回显模型原文**（`deepseek.py` 的 `_repair_message`，断言在 `test_deepseek_complete_json_repairs_without_echoing_model_output`）：只报「哪个字段、期望什么类型」。回显原文的后果是——模型在上一轮可能把提示词里的档案内容抄进了输出，回显它等于把档案再发一遍，还把 `scrub` 的边界绕开了。

`StructuredOutputError.details` 里只放 schema 名与字段路径，`code` 默认 `PLAN_INVALID`、由调用方按场景覆盖为 `HOCON_SYNTAX_ERROR` 或 `SCHEMA_MISMATCH`。

### 5.5 出站边界：一份模式表，两种处置

`prepare_outbound`（`scrub.py:132`）是**唯一**出站入口，Provider 实现只调它，不「记得先 assert 再 scrub」——后者是两条必须记住的纪律，前者只有一条。

| 步骤 | 函数 | 命中后的处置 |
|---|---|---|
| 1. 禁投喂断言 | `assert_outbound_safe`（`scrub.py:98`） | 解析 `FORBIDDEN_PATTERNS`，命中即 `log.error("llm_context_violation")` 并抛 `LLMContextViolationError` |
| 2. PII 兜底脱敏 | `scrub_text_with_count`（`scrub.py:84`） | 解析 `PII_PATTERNS`，**等长掩码**替换，返回替换计数 |

两类模式分开的理由：禁投喂项（凭据、连接串）**不该被投喂**，掩码后投喂等于「先泄露再打码」；PII 项（姓名、手机号）可以在脱敏后继续走，不必因一个手机号让整个生成失败。

异常 `details` 里只放**命中的模式名与次数**，不放命中片段——命中片段是秘密，回显它是二次泄漏，而且它还会被写进响应与日志。

`assert_outbound_safe` 同时接受整段文本与消息列表两种入参：be-08 §3.2 的签名是消息列表，而请求级闸门往往只有一段已拼好的文本。两种形态各写一份实现，早晚会有一边漏掉一条模式（§10.10）。

### 5.6 确定性：FakeProvider 是正式通路，无 key 时显式 503

- `FakeProvider` 按**围栏内容**分派（`FINDINGS` → 修复补丁 / `SOURCE_PROFILE` → 方案草稿 / `REQUIREMENT` → 意图），产出逐字节可复现：`FAKE_MODEL_NAME = "fake-deterministic"`、`FAKE_MODEL_VERSION = "1.0"`。
- `apply_autofix` 只对 `FIXABLE_CODES` 做确定性修复，对 `UNFIXABLE_CODES` 原样返回——这样「三轮修不好」在离线环境下也真的能跑到 `AUTOFIX_EXHAUSTED`。
- **`deepseek` 无 key 时抛 `LLMUnavailableError`（503），不静默降级 `fake`**（`registry.py:58`）：日志里写 `llm_api_key="***empty***"`（只报「已配置/未配置」，绝不打印 key 的任何片段）并给出 hint「确定性离线运行请设 `LLM_PROVIDER=fake`」。静默降级会让「本机没配 key」与「生产上 key 过期」表现成同一件事。
- 未登记的 provider 名 → `ValidationError`（422），`details["supported"]` 列出合法取值。

### 5.7 `generation_context` 的形状只有一份定义

`GenerationContext` 这个模型就是「生成溯源信息」的唯一定义，由 be-09 的 `generate_plan` 节点**构造模型**后 `model_dump(mode="json")` 入 state。早先该节点手拼 dict 且多拼了一个 `plan_issue_count`，症状是「生成成功、答题 422：Extra inputs are not permitted」——报错指向用户提交的答案，真因却在拼 dict 的那一行。规矩：这类跨模块 JSON 列的形状只有一份定义，写的一侧多一个键，报在读的一侧。

---

## 6. 错误码

| # | 触发条件 | 错误码 | HTTP | 出处 |
|---|---|---|---|---|
| 1 | 重试耗尽 / 连接失败 / 5xx / 429 仍失败 / 凭据无效，`deepseek` 未配 key | `LLM_UNAVAILABLE` | 503 | `base.py:201` |
| 2 | `complete_json` 连修复重试都没拿到合法结构（默认码，调用方可覆盖） | `PLAN_INVALID` | 422 | `base.py:214` |
| 3 | `diagnose` 任务带 `tools` 或 `json_mode` | `VALIDATION_ERROR` | 422 | `base.py:113` |
| 4 | 未知 provider 名 | `VALIDATION_ERROR` | 422 | `registry.py:73` |
| 5 | 提示词装配后仍超预算（含 priority 0 本身就超） | `VALIDATION_ERROR` | 422 | `token_budget.py:45` |
| 6 | 出站消息命中禁投喂模式 | `INTERNAL_ERROR` | 500 | `base.py:228`（红线告警，见 §10.9） |

错误码全部取自 `core/errors.py` 既有枚举，本模块**未新增**任何码。第 6 条用 `INTERNAL_ERROR` 是刻意的：它是「安全红线被触碰」的信号，不是普通参数错误，必须 `log.error` 告警。

---

## 7. 异步任务与尚未实现的部分

- **本模块无 Celery 任务**：它只被 be-09 的图节点在 async 上下文里调用。它是纯异步 IO（`httpx.AsyncClient`），不持有 session、不进 `tasks/`。
- 🟡 **Benchmark 缓存回放**（设计 §5.2 / §6.5）：未实现。缓存键（模型名 ⊕ 提示词版本 ⊕ 规范化 payload 的 sha256）与回放路径都还没有落点；本模块目前每次调用都真的发请求。
- 🟡 **`intent` / `hocon` / `diagnose` 三个模板**：未建，且 `PROMPT_VERSIONS` **不预先占位**（`prompts/__init__.py:15` 注释）——注册表里写着一个不存在的模板版本，比什么都不写更具误导性。be-09 的 `intent_parse` 节点因此自带一个占位版本号常量，待模板落地后改回从注册表读。
- 🟡 **`diagnose` 任务的 Provider 侧支持**：`TaskKind` 有 `diagnose`、合法性校验也在，但 be-19 未落地，没有任何调用方。

---

## 8. 验收标准

对应 PRD §6.3.2 / §10.1：

| 验收点 | 判据 | PRD |
|---|---|---|
| 业务层不依赖具体实现 | `clients/llm/__init__.py` 的导出面里没有 `DeepSeekProvider` / `FakeProvider` | §6.3.2 |
| `generation_context` 可追溯模型版本 | `GenerationContext.model_version` 非空；生成节点取厂商返回的实际版本串 | §6.3.2 |
| Secret 不进模型上下文 | `assert_outbound_safe` 命中凭据模式即拦（`tests/test_llm_provider.py` 的 `scrub` 用例组） | §10.1 |
| 无 key 可离线跑通 | `LLM_PROVIDER=fake` 能跑完整条生成链路（be-09 端到端用例） | §14.1 |

本模块**没有**独立的 PRD 数字指标（如拦截率）——它服务的是 be-09 的端到端验收。本轮实测：`tests/test_llm_provider.py` **59 passed**。

---

## 9. 测试数据

| 文件 | 条数 | 覆盖 |
|---|---|---|
| `tests/test_llm_provider.py` | 59 | 协议/DTO 形状、任务分流、退避重试序列与 `Retry-After` 封顶、`complete_json` 修复重试与不回显、围栏剥离、`fit_prompt` 阶梯与超预算、`FakeProvider` 确定性、出站 `scrub` 断言、注册表与版本号一致 |

关键用例：

| 用例 | 断言点 |
|---|---|
| `test_deepseek_retries_on_retryable_status` | 退避序列 `[1.0, 2.0]`，共 3 次请求 |
| `test_deepseek_retry_exhausted_raises_llm_unavailable` | `details["last_status"] == 503` |
| `test_deepseek_complete_json_repairs_without_echoing_model_output` | 第 2 次请求体里含字段路径、**不含**模型原文 |
| `test_deepseek_complete_json_raises_after_two_failures` | 修复一次后仍失败 → `StructuredOutputError` |
| `test_prompt_version_registry_matches_template_constants` | 版本号两处出现（注册表 + 模板模块）必须一致 |

`GenerationContext` 的读写往返（写进 `pipeline_version.generation_context`、再由 API DTO 读回）由 `tests/test_dto_version_widths.py::test_存进_generation_context_的东西能被_api_dto_读回来` 守着——它跨在 be-08 与 be-12 之间。

---

## 10. 待确认与偏离登记

> 编号接全局清单（M2-01 §10 用了 1–6）。本 spec 从 **7** 起。

| # | 事项 | 建议 | 验证方式 |
|---|---|---|---|
| **7** | **`GenerationContext` 字段以前端为准**：实现把 `model_name / model_version / prompt_template_version / autofix_rounds` 定为不可改的前端契约字段，把 be-08 §4 的 `provider` / `prompt_versions` / `usage_total` / `generated_at` 降为可选附加项（默认空） | 保留现状。前端契约是冻结的，be-08 §4 那份字段清单是设计期设想；落库口径靠附加项补齐即可 | `schemas/pipeline.py` 的 `PipelineVersionSummary.generation_context` 与 `frontend/src/types/pipeline.ts` 逐字段比对；`test_dto_version_widths.py` 的往返用例 |
| **8** | **`deepseek` 无 key → 显式 503，而非静默回落 `fake`**（be-08 §7 未定义该分支，实现选了显式报错） | 保留。静默降级会让「本机没配 key」与「生产 key 过期」表现一致，运维第一次遇到就分不清 | `tests/test_llm_provider.py` 的 `build_provider` 分支用例 |
| **9** | **`LLMContextViolationError` 仍用 `INTERNAL_ERROR`**：be-08 §10.3 建议新增 `LLM_CONTEXT_VIOLATION`，但 `core/errors.py` 属 be-01 的对外契约，本次不改 | 待 be-01 新增该码后切过去（纯改名，语义不变）；在此之前靠 `log.error("llm_context_violation")` 保持可检索 | `core/errors.py` 是否出现新码；`scrub.py:228` 的 `code` 赋值 |
| **10** | **出站闸门是一份模式表、两种处置**：`assert_outbound_safe`（凭据类，抛异常）与 `scrub_text`（PII 类，等长掩码）分开；设计 §6.6 把两类合成一个函数 | 保留。两类模式的正确处置本就不同（一个该拦、一个该打码后放行），合成一个函数无法表达这个区别 | `scrub.py` 的两张模式表 + `prepare_outbound` 的顺序 |
| **11** | **Benchmark 缓存回放（`cache.py`）未实现**（设计 §5.2 / §6.5） | 归 M4 的 Benchmark 模块（be-20）一起做；在那之前每次调用都真的发请求，不影响正确性，只影响成本 | 目录下是否出现 `cache.py`；缓存键是否包含 `prompt_version` 与规范化 payload 哈希 |
| **12** | **`estimate_tokens` 不含 5% 安全系数**：设计 §6.3 的文字把 5% 放在估算里，实现把 5% 交给 `fit_prompt` 的停止线（`total <= budget * 0.95`） | 保留实现。两处各乘一次会把预算白白缩掉约 10%，而「到底按哪个数算」会在单测与生产之间产生分歧 | `token_budget.py:87` 的注释；`fit_prompt` 里的 `target = budget * _HEADROOM` |
| **13** | **priority 1 的裁剪动作退化为「最大 JSON 列表减半 / 文本按行减半」**：设计 §6.3 的规范动作是「只保留用户已提及或已映射的字段」，而块的内容对纯函数不可见（只有 key/priority/text） | 保留。要真正做到「只留已提及字段」，应由调用方在构造 `PromptBlock` 时摘掉未提及字段——那比让裁剪器去猜字段语义可靠 | `fit_prompt` docstring 的「已知简化」段；`_half` / `_shrink_json` |

---

## 11. 变更记录

| 日期 | 变更 | 原因 |
|---|---|---|
| 2026-10-16 | 初版：be-08 协议/DTO/DeepSeek/Fake/scrub/token_budget/prompts 全部落地后补写 | M2 生成链路的模型接入层；偏离登记见 §10（7–13） |
