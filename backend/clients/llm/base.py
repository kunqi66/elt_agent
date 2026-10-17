"""LLM Provider 的数据契约与异常（be-08 §3.2 / §4）。

**本模块只放纯数据与协议**：不 import ``httpx``、不 import 任何厂商 SDK，
这样「业务层不得依赖具体模型实现」（PRD §6.3.2）在类型层面就成立——
``workflow/`` 与 ``services/`` 可以 `from clients.llm import CompletionRequest` 而完全不碰 deepseek。

``GenerationContext`` 的字段名**以前端为准**（``frontend/src/types/pipeline.ts`` 第 312-317 行）：

    { model_name, model_version, prompt_template_version, autofix_rounds }

be-08 §4 的写法是 ``{provider, model, model_version, prompt_versions, autofix_rounds,
usage_total, generated_at}``。二者冲突时按仓库既定口径处理（见 ``engines/plan/schema.py``
的模块 docstring：前端已冻结的契约优先，差异登记在注释里）：**前端的四个字段逐字保留**，
be-08 的额外字段作为可选附加项并存——多了不影响前端渲染，少了或改名会让版本详情页空白。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any, Literal, Protocol, TypeVar

from pydantic import BaseModel, ConfigDict, Field, model_validator

from core.errors import AppError, ErrorCode

#: 任务分流：``generate`` 用 ``deepseek-chat``，``diagnose`` 用 ``deepseek-reasoner``（be-08 §6.1）。
TaskKind = Literal["generate", "diagnose"]

MessageRole = Literal["system", "user", "assistant", "tool"]

_BASE = ConfigDict(extra="forbid", frozen=True)

TModel = TypeVar("TModel", bound=BaseModel)


# ---------------------------------------------------------------- 消息与工具


class ChatMessage(BaseModel):
    """一条对话消息。``tool`` 角色必须带 ``tool_call_id``。"""

    model_config = _BASE

    role: MessageRole
    content: str
    tool_call_id: str | None = None

    @model_validator(mode="after")
    def _check_tool_call_id(self) -> ChatMessage:
        if self.role == "tool" and not self.tool_call_id:
            raise ValueError("role='tool' 的消息必须带 tool_call_id")
        return self


class ToolSpec(BaseModel):
    """工具声明。``parameters`` 是 JSON Schema 原文。"""

    model_config = _BASE

    name: str = Field(min_length=1, max_length=64)
    description: str = ""
    parameters: dict[str, Any] = Field(default_factory=dict)


class ToolCall(BaseModel):
    """厂商返回的工具调用。

    ``arguments_json`` 是**厂商原样返回的字符串**，由调用方解析：在这里替它 ``json.loads``
    会把「模型拼错了 JSON」变成 provider 层的解析错误，而调用方恰恰需要知道
    「模型没有按 JSON 输出」这件事（它决定要不要走一次修复重试）。
    """

    model_config = _BASE

    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    arguments_json: str = ""


# ---------------------------------------------------------------- 请求与响应


class CompletionRequest(BaseModel):
    """一次补全请求。

    非法组合在**构造期**就拒掉（be-08 §5「非法调用在 registry 构造请求时即拒绝」）：
    ``diagnose`` 走的是推理模型，厂商对 ``tools`` 与 ``json_mode`` 都不支持；
    让它带着工具发出去只会换回一个 400，而那时已经付了一次网络往返与一次风控计数。
    """

    model_config = _BASE

    task: TaskKind = "generate"
    messages: list[ChatMessage] = Field(min_length=1)
    tools: list[ToolSpec] = Field(default_factory=list)
    json_mode: bool = False
    #: 生成类任务默认 0：方案要被门禁逐条检查，模型越随机，同一需求两次生成的差异越大
    temperature: float = 0.0
    max_output_tokens: int = Field(default=4096, gt=0)
    #: 来自 ``prompts.PROMPT_VERSIONS``，写入 generation_context 供追溯
    prompt_version: str = Field(min_length=1)
    #: 仅用于日志关联，**不发往厂商**、**不参与缓存键**（be-08 §6.5）
    request_id: str = Field(min_length=1)

    @model_validator(mode="after")
    def _check_task_capabilities(self) -> CompletionRequest:
        if self.task == "diagnose":
            if self.tools:
                raise AppError(
                    "根因诊断任务不支持 tools（deepseek-reasoner 不接受工具调用）",
                    code=ErrorCode.VALIDATION_ERROR,
                    http_status=422,
                )
            if self.json_mode:
                raise AppError(
                    "根因诊断任务不支持 json_mode；如需结构化结果请改为在提示词内嵌 JSON 契约",
                    code=ErrorCode.VALIDATION_ERROR,
                    http_status=422,
                )
        return self


class TokenUsage(BaseModel):
    model_config = _BASE

    prompt_tokens: int = Field(default=0, ge=0)
    completion_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)

    def plus(self, other: TokenUsage) -> TokenUsage:
        """累加（多轮生成时汇总 usage）。不做原地修改：这是 frozen 模型。"""
        return TokenUsage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            total_tokens=self.total_tokens + other.total_tokens,
        )


class CompletionResult(BaseModel):
    model_config = _BASE

    text: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    usage: TokenUsage = Field(default_factory=TokenUsage)
    #: 厂商返回的**实际**模型名（可能与我们请求的不同，如带着日期后缀）
    model: str = ""
    #: 厂商返回的版本串，落进 generation_context——「同一需求为何生成不同方案」靠它追溯
    model_version: str = ""
    finish_reason: str = ""
    #: 来自 Benchmark 缓存回放（be-08 §5.2）
    cached: bool = False


class GenerationContext(BaseModel):
    """生成溯源信息，由 be-12 在冻结时写进 ``pipeline_version.generation_context``。

    **不含任何样本原文、不含 Secret**——它会被前端原样展示，也会进指纹比对之外的审计视图。
    ``generated_at`` 参与落库但不参与制品指纹（be-12 §6），所以调用方传什么都行，
    关键是**由调用方注入**而不是在 provider 里取时钟：假 Provider 必须在任何时刻都给出同一结果。
    """

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    # ---- 前端契约字段（不可改名 / 不可删）----
    model_name: str = Field(min_length=1)
    model_version: str = Field(min_length=1)
    prompt_template_version: str = Field(min_length=1)
    autofix_rounds: int = Field(default=0, ge=0)

    # ---- be-08 §4 的审计附加项（前端不渲染，但落库口径要保留）----
    provider: str = ""
    #: 模板名 → 版本号，如 ``{"plan": "1.0", "autofix": "1.0"}``
    prompt_versions: dict[str, str] = Field(default_factory=dict)
    usage_total: TokenUsage | None = None
    generated_at: datetime | None = None


# ---------------------------------------------------------------- 协议


class LLMProvider(Protocol):
    """与模型厂商无关的补全接口。

    刻意**不**标 ``runtime_checkable``：协议里带数据成员（``name``）时，
    ``isinstance`` 会在运行期抛 ``TypeError``，而它会诱使人写出「看着能跑」的鸭子检查。
    """

    name: str

    async def complete(self, req: CompletionRequest) -> CompletionResult: ...

    async def complete_json(self, req: CompletionRequest, schema: type[TModel]) -> TModel: ...

    def stream(self, req: CompletionRequest) -> AsyncIterator[str]: ...


# ---------------------------------------------------------------- 异常


class LLMUnavailableError(AppError):
    """重试耗尽 / 连接失败 / 5xx / 429 仍失败 / 凭据无效（be-08 §7）。

    对外的 ``code`` 只有一个（``LLM_UNAVAILABLE``，503）：
    401/403 与 5xx 对**用户**是同一件事——「现在生成不了，稍后再试或找管理员」，
    而具体状态码放在 ``details["last_status"]`` 给运维，不进前端文案。
    """

    code = ErrorCode.LLM_UNAVAILABLE
    http_status = 503
    message = "模型服务不可用，请稍后重试"


class StructuredOutputError(AppError):
    """``complete_json`` 连修复重试都没能拿到合法结构（be-08 §6.4 第 5 步）。

    默认码取 ``PLAN_INVALID`` 只是**兜底**：调用方应按场景覆盖为
    ``HOCON_SYNTAX_ERROR``（HOCON 文本不可解析）或 ``SCHEMA_MISMATCH``（结构不符）。
    ``details`` 里只放 schema 名与**校验错误的字段路径**——模型原文不回显，
    它可能夹带我们在提示词里放进去的档案内容。
    """

    code = ErrorCode.PLAN_INVALID
    http_status = 422
    message = "模型返回的结构化输出无法解析"


class LLMContextViolationError(AppError):
    """出站消息命中禁投喂模式（be-08 §6.6 / §7）。

    这是**安全红线被触碰**的信号，不是普通参数错误：``code`` 用 ``INTERNAL_ERROR``
    （be-08 §10.3 建议新增 ``LLM_CONTEXT_VIOLATION``，但 ``core/errors.py`` 属 be-01 的
    对外契约、本次不改），并要求调用点 ``log.error`` 告警。
    """

    code = ErrorCode.INTERNAL_ERROR
    http_status = 500
    message = "出站上下文命中禁投喂模式，请求已被拦截"


__all__ = [
    "ChatMessage",
    "CompletionRequest",
    "CompletionResult",
    "GenerationContext",
    "LLMContextViolationError",
    "LLMProvider",
    "LLMUnavailableError",
    "MessageRole",
    "StructuredOutputError",
    "TModel",
    "TaskKind",
    "TokenUsage",
    "ToolCall",
    "ToolSpec",
]
