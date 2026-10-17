"""LLM Provider 抽象层（be-08）。

对外只暴露**协议、DTO 与选择器**：具体实现（``deepseek`` / ``fake``）不在导出面里，
这样「业务层不得依赖具体模型实现」在 review 时是一眼可查的事实，而不是一条约定。

需要按名字构造时用 :func:`build_provider`——它认得 ``"fake"``，
所以没有 API key 的环境也能构造出一个可用的 Provider。
"""

from clients.llm.base import (
    ChatMessage,
    CompletionRequest,
    CompletionResult,
    GenerationContext,
    LLMContextViolationError,
    LLMProvider,
    LLMUnavailableError,
    MessageRole,
    StructuredOutputError,
    TaskKind,
    TokenUsage,
    ToolCall,
    ToolSpec,
)
from clients.llm.registry import SUPPORTED_PROVIDERS, build_provider, get_provider

__all__ = [
    "SUPPORTED_PROVIDERS",
    "ChatMessage",
    "CompletionRequest",
    "CompletionResult",
    "GenerationContext",
    "LLMContextViolationError",
    "LLMProvider",
    "LLMUnavailableError",
    "MessageRole",
    "StructuredOutputError",
    "TaskKind",
    "TokenUsage",
    "ToolCall",
    "ToolSpec",
    "build_provider",
    "get_provider",
]
