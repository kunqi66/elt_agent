"""DeepSeek Provider（be-08 §6.1 / §6.2 / §6.4）。

三条纪律，每条都有对应用例或代码注释兜着：

1. **key 只从设置读，永不进日志、不进异常消息**。请求头 ``Authorization``
   在日志里也从不出现——本模块所有日志字段都是「长度 / 状态码 / 计时」这类元信息。
2. **厂商异常一律归一成项目异常**（``LLMUnavailableError`` / ``StructuredOutputError``）。
   ``httpx`` 的异常类型不许漏到上层，它的消息里可能带 URL 与请求体。
3. **出站前必过闸**（:func:`clients.llm.scrub.prepare_outbound`）：先断言禁投喂模式，
   再做 PII 兜底替换。

本文件**不需要真跑通**（当前没有 API key），但它是可读的真实现：
重试与退避、``Retry-After``、任务到模型的分流、结构化输出的围栏剥离与一次修复重试
都在这里。真实调用的集成用例标 ``@pytest.mark.integration``，只在 ECS 上跑。
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import httpx
import structlog
from pydantic import ValidationError as PydanticValidationError

from clients.llm.base import (
    ChatMessage,
    CompletionRequest,
    CompletionResult,
    LLMUnavailableError,
    StructuredOutputError,
    TModel,
    TokenUsage,
    ToolCall,
)
from clients.llm.prompts.fences import strip_code_fences
from clients.llm.scrub import prepare_outbound

log = structlog.get_logger(__name__)

#: 可重试的 HTTP 状态：限流与服务端故障。4xx 的其余取值都是「请求本身不对」，
#: 重试只会放大风控（be-08 §6.2）。
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})

#: 退避上限（秒）。``Retry-After`` 也受它封顶：厂商要求等 10 分钟时，
#: 我们宁可失败让上层决定，也不把一次 HTTP 请求挂成 600 秒。
_MAX_BACKOFF_SEC = 30.0

#: 修复重试时追加的指令。**只描述校验错误，不回显模型原文**（be-08 §6.4 第 4 步）。
_REPAIR_INSTRUCTION = (
    "上一条回复不是合法的 JSON 对象，或字段不符合契约。请只针对下列问题修正后"
    "重新输出**完整**的 JSON 对象，不要解释、不要 Markdown 围栏：\n{paths}"
)


class DeepSeekProvider:
    """``deepseek-chat``（生成/修复）与 ``deepseek-reasoner``（诊断）的统一入口。"""

    name = "deepseek"

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://api.deepseek.com",
        model_generate: str = "deepseek-chat",
        model_diagnose: str = "deepseek-reasoner",
        timeout_sec: float = 90.0,
        max_retry: int = 2,
        client: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        jitter: Callable[[float, float], float] | None = None,
    ) -> None:
        if not api_key:
            raise LLMUnavailableError(
                "DeepSeek 未配置 API key", details={"provider": self.name, "reason": "missing_api_key"}
            )
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model_of: dict[str, str] = {"generate": model_generate, "diagnose": model_diagnose}
        # 连接 10s / 写 10s / 池 5s 固定，读走配置：读超时才是「模型想太久」的那一个
        self._timeout = httpx.Timeout(connect=10.0, read=timeout_sec, write=10.0, pool=5.0)
        self._max_retry = max(0, max_retry)
        self._client = client
        # 注入点：单测要断言「重试了 3 次」而不想真等 1+2 秒的退避
        self._sleep = sleep or asyncio.sleep
        self._jitter = jitter or random.uniform

    # ------------------------------------------------------------ 构造

    @classmethod
    def from_settings(cls, settings: Any | None = None) -> DeepSeekProvider:
        """从全局设置构造。``SecretStr`` 必须显式解包——直接传会把 ``**********`` 当 key 发出去。"""
        if settings is None:
            from core.config import get_settings

            settings = get_settings()
        return cls(
            api_key=settings.LLM_API_KEY.get_secret_value(),
            base_url=settings.LLM_BASE_URL,
            model_generate=settings.LLM_MODEL_GENERATE,
            model_diagnose=settings.LLM_MODEL_DIAGNOSE,
            timeout_sec=float(settings.LLM_TIMEOUT_SEC),
            max_retry=int(settings.LLM_MAX_RETRY),
        )

    async def aclose(self) -> None:
        """由拥有者关闭。外部注入的 client 不在这里关——那不是我们的连接。"""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(base_url=self._base_url, timeout=self._timeout)
        return self._client

    # ------------------------------------------------------------ 契约实现

    async def complete(self, req: CompletionRequest) -> CompletionResult:
        """一次补全。出站前过闸，可重试失败按指数退避重试。"""
        messages, scrubbed_count = prepare_outbound(req.messages)
        if scrubbed_count:
            # 只记次数，不记原文——被替换掉的东西正是不能记的那部分
            log.info(
                "llm_outbound_scrubbed",
                request_id=req.request_id,
                provider=self.name,
                scrubbed_count=scrubbed_count,
            )

        model = self._model_of[req.task]
        payload: dict[str, Any] = {
            "model": model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "temperature": req.temperature,
            "max_tokens": req.max_output_tokens,
            "stream": False,
        }
        if req.json_mode:
            payload["response_format"] = {"type": "json_object"}
        if req.tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.parameters,
                    },
                }
                for tool in req.tools
            ]

        started = time.perf_counter()
        data, attempts = await self._post_with_retry(payload, req=req)
        latency_ms = int((time.perf_counter() - started) * 1000)
        result = self._parse(data, model=model)
        # 审计字段：不含提示词与响应正文（be-08 §8）
        log.info(
            "llm_call",
            request_id=req.request_id,
            provider=self.name,
            task=req.task,
            prompt_version=req.prompt_version,
            model=result.model,
            model_version=result.model_version,
            prompt_tokens=result.usage.prompt_tokens,
            completion_tokens=result.usage.completion_tokens,
            latency_ms=latency_ms,
            attempts=attempts,
            cached=False,
        )
        return result

    async def complete_json(self, req: CompletionRequest, schema: type[TModel]) -> TModel:
        """结构化补全：一次修复重试，仍失败即抛（**不返回兜底对象**，be-08 §6.4 第 6 步）。

        「上次的校验错误路径」只走**返回值**，不存实例属性：provider 是
        ``lru_cache`` 出来的共享实例，把请求级状态挂在 ``self`` 上，两个并发生成
        会互相读到对方的错误路径，于是修复指令指向另一份方案的问题。
        """
        first = await self.complete(req)
        parsed, paths = self._try_parse_json(first.text, schema)
        if parsed is not None:
            return parsed

        repair = req.model_copy(
            update={
                "messages": [
                    *req.messages,
                    _repair_message(_REPAIR_INSTRUCTION.format(paths="\n".join(f"- {p}" for p in paths))),
                ]
            }
        )
        second = await self.complete(repair)
        parsed, paths = self._try_parse_json(second.text, schema)
        if parsed is not None:
            return parsed

        raise StructuredOutputError(
            "模型两次返回的结构化输出都无法解析",
            details={
                "schema": schema.__name__,
                # 只给字段路径与原因，**不回显模型原文**（它可能夹带档案内容）
                "error_paths": paths,
            },
        )

    async def stream(self, req: CompletionRequest) -> AsyncIterator[str]:
        """流式补全。逐块 yield 文本增量；结构化输出不走这里（需要完整文本才能校验）。"""
        messages, _ = prepare_outbound(req.messages)
        model = self._model_of[req.task]
        payload: dict[str, Any] = {
            "model": model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "temperature": req.temperature,
            "max_tokens": req.max_output_tokens,
            "stream": True,
        }
        headers = self._headers()
        async with self._http().stream(
            "POST", "/chat/completions", json=payload, headers=headers
        ) as response:
            if response.status_code != 200:
                raise self._unavailable(status=response.status_code)
            async for line in response.aiter_lines():
                chunk = _stream_delta(line)
                if chunk:
                    yield chunk

    # ------------------------------------------------------------ 内部

    def _headers(self) -> dict[str, str]:
        # 请求头**永不进日志**：这是全仓库唯一拼 Authorization 的地方
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    async def _post_with_retry(
        self, payload: dict[str, Any], *, req: CompletionRequest
    ) -> tuple[dict[str, Any], int]:
        """返回 ``(响应体, 实际尝试次数)``。尝试次数回传而不是记在 ``self`` 上（见 complete_json）。"""
        attempts = 1 + self._max_retry
        last_status: int | None = None
        for attempt in range(1, attempts + 1):
            try:
                response = await self._http().post("/chat/completions", json=payload, headers=self._headers())
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                # 连接错误 / 读超时：可重试。异常文本不回传——它可能带 URL
                if attempt >= attempts:
                    raise self._unavailable(detail=type(exc).__name__) from exc
                await self._backoff(attempt, retry_after=None)
                continue

            if response.status_code == 200:
                try:
                    return response.json(), attempt
                except ValueError as exc:
                    # 200 但不是 JSON：厂商侧异常（网关吐 HTML）。重试后仍如此就放弃
                    if attempt >= attempts:
                        raise self._unavailable(detail="invalid_json_body") from exc
                    await self._backoff(attempt, retry_after=None)
                    continue

            last_status = response.status_code
            if response.status_code not in _RETRYABLE_STATUS:
                # 401/403 是凭据问题、400 是请求非法：重试无意义且会放大风控
                raise self._unavailable(status=response.status_code, detail="non_retryable_status")
            if attempt >= attempts:
                break
            await self._backoff(attempt, retry_after=response.headers.get("Retry-After"))

        raise self._unavailable(status=last_status, detail=f"attempts={attempts}")

    async def _backoff(self, attempt: int, *, retry_after: str | None) -> None:
        """退避：``min(2 ** (attempt-1), 8)`` 秒 ±20% 抖动；``Retry-After`` 优先（上限 30s）。"""
        wait = float(min(2 ** (attempt - 1), 8))
        if retry_after:
            try:
                wait = float(retry_after)
            except ValueError:
                pass
        wait = min(wait, _MAX_BACKOFF_SEC)
        wait *= self._jitter(0.8, 1.2)
        log.info("llm_backoff", attempt=attempt, wait_sec=round(wait, 3))
        await self._sleep(wait)

    def _unavailable(self, *, status: int | None = None, detail: str | None = None) -> LLMUnavailableError:
        """归一化失败。``details`` 里**不放响应体**——它可能回显我们发出去的报文。"""
        details: dict[str, Any] = {"provider": self.name}
        if status is not None:
            details["last_status"] = status
        if detail is not None:
            details["detail"] = detail
        return LLMUnavailableError(details=details)

    def _parse(self, data: dict[str, Any], *, model: str) -> CompletionResult:
        choices = data.get("choices") or []
        if not choices:
            raise StructuredOutputError(
                "厂商响应里没有 choices",
                details={"provider": self.name, "model": model},
            )
        choice = choices[0]
        message = choice.get("message") or {}
        usage = data.get("usage") or {}
        tool_calls = _tool_calls_of(message)
        # reasoner 的 reasoning_content **不进任何持久化字段**，只记长度（be-08 §6.1）
        reasoning = message.get("reasoning_content")
        if reasoning:
            log.debug("llm_reasoning_content", provider=self.name, length=len(str(reasoning)))

        return CompletionResult(
            text=str(message.get("content") or ""),
            tool_calls=tool_calls,
            usage=TokenUsage(
                prompt_tokens=int(usage.get("prompt_tokens") or 0),
                completion_tokens=int(usage.get("completion_tokens") or 0),
                total_tokens=int(usage.get("total_tokens") or 0),
            ),
            model=str(data.get("model") or model),
            model_version=str(data.get("system_fingerprint") or data.get("model") or model),
            finish_reason=str(choice.get("finish_reason") or ""),
            cached=False,
        )

    def _try_parse_json(self, text: str, schema: type[TModel]) -> tuple[TModel | None, list[str]]:
        """剥离围栏后校验。失败时**回传字段路径**（而不是记在实例上），供修复指令使用。"""
        candidate = strip_code_fences(text)
        try:
            return schema.model_validate_json(candidate), []
        except PydanticValidationError as exc:
            paths = [".".join(str(p) for p in err.get("loc", ())) for err in exc.errors()]
            return None, paths


def _tool_calls_of(message: dict[str, Any]) -> list[ToolCall]:
    """挑出形状完整的工具调用。

    缺 ``id`` 或 ``name`` 的条目直接丢掉：``ToolCall`` 的两个字段都有长度下限，
    硬塞空串会在解析响应这一步抛校验错误——而「厂商返回了一条残缺的工具调用」
    应当表现为「这一轮没有可用工具调用」，不是整次生成失败。
    """
    calls: list[ToolCall] = []
    for raw in message.get("tool_calls") or []:
        function = raw.get("function") or {}
        call_id = str(raw.get("id") or "")
        name = str(function.get("name") or "")
        if not call_id or not name:
            continue
        calls.append(
            ToolCall(
                id=call_id,
                name=name,
                arguments_json=str(function.get("arguments") or ""),
            )
        )
    return calls


def _repair_message(content: str) -> ChatMessage:
    return ChatMessage(role="user", content=content)


def _stream_delta(line: str) -> str:
    """解析 SSE 的一行。``[DONE]`` 与空行都返回空串（调用方据此跳过）。

    手解析而不是引 SDK：多一个依赖就多一处必须跟着厂商升级的地方，
    而这里需要的只有 ``choices[0].delta.content`` 一个字段。
    """
    if not line or not line.startswith("data:"):
        return ""
    body = line[len("data:") :].strip()
    if not body or body == "[DONE]":
        return ""
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return ""
    choices = payload.get("choices") or []
    if not choices:
        return ""
    return str((choices[0].get("delta") or {}).get("content") or "")


__all__ = ["DeepSeekProvider"]
