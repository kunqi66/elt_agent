"""Provider 选择的唯一入口（be-08 §3.2）。

**业务层只能从这里取 Provider**：``services/`` / ``workflow/`` / ``tasks/`` 直接
``import clients.llm.deepseek`` 就等于把业务焊死在某个厂商上（PRD §6.3.2）。

### 没有 API key 时为什么是「报错」而不是「自动降级成假 Provider」

be-08 §7 把「未知 provider 名」定成 ``VALIDATION_ERROR``，但没有定义
「配了 deepseek 却没 key」。两种做法都能自洽，这里选**显式报错**：

* 静默降级会让「线上配错了 key」表现为「方案质量突然变差」——同一个
  ``LLM_PROVIDER=deepseek`` 在不同环境跑出不同来源的方案，而前端与审计里
  一点痕迹都没有（``generation_context.model_name`` 只显示 ``model_name``，
  看的人默认它是真模型）。这正是 CLAUDE.md §5.2「绝不静默」要防的事。
* 报错则把「用哪个 Provider」变成一次**显式决定**：想要确定性离线方案的人
  写 ``build_provider("fake")`` 或设 ``LLM_PROVIDER=fake``，语义清楚、可测、
  且在 ``generation_context`` 里留下 ``provider="fake"``。
* 错误码用 ``LLM_UNAVAILABLE``（503）：对调用方而言，「没 key」与「厂商挂了」
  的处置完全一样——换 Provider 或稍后重试。
"""

from __future__ import annotations

from functools import lru_cache

import structlog

from clients.llm.base import LLMProvider, LLMUnavailableError
from clients.llm.deepseek import DeepSeekProvider
from clients.llm.fake import FakeProvider
from core.config import get_settings
from core.errors import ValidationError

log = structlog.get_logger(__name__)

#: 合法 provider 名。``"fake"`` 是**一等公民**：没有 key 的机器靠它跑通整条链路，
#: 它不是一个测试开关，而是正式支持的运行模式。
SUPPORTED_PROVIDERS: tuple[str, ...] = ("deepseek", "fake")


def build_provider(name: str | None = None) -> LLMProvider:
    """按名字构造 Provider；``name`` 为空时读 ``settings.LLM_PROVIDER``。

    * ``"fake"`` → :class:`FakeProvider`（确定性、离线）
    * ``"deepseek"`` → :class:`DeepSeekProvider`；**无 key 时抛 ``LLM_UNAVAILABLE``**
      （理由见模块 docstring，绝不静默降级）
    * 其它 → ``VALIDATION_ERROR``（422）
    """
    settings = get_settings()
    resolved = (name or settings.LLM_PROVIDER or "").strip().lower()

    if resolved == "fake":
        return FakeProvider()

    if resolved == "deepseek":
        if not settings.LLM_API_KEY.get_secret_value():
            # 明确说清楚「谁降级了、为什么、怎么办」——日志是运维唯一能看到的地方。
            # 只报「已配置/未配置」，绝不打印 key 的任何片段
            log.error(
                "llm_provider_unavailable",
                provider="deepseek",
                reason="missing_api_key",
                llm_api_key="***empty***",
                hint="确定性离线运行请显式 build_provider('fake') 或设 LLM_PROVIDER=fake",
            )
            raise LLMUnavailableError(
                "未配置 LLM_API_KEY，deepseek Provider 不可用；"
                "离线确定性运行请显式使用 build_provider('fake')",
                details={"provider": "deepseek", "reason": "missing_api_key"},
            )
        return DeepSeekProvider.from_settings(settings)

    raise ValidationError(
        f"未知的 LLM Provider：{resolved!r}",
        details={"provider": resolved, "supported": list(SUPPORTED_PROVIDERS)},
    )


@lru_cache(maxsize=1)
def get_provider() -> LLMProvider:
    """进程级单例。Provider 内部只持有 HTTP 客户端，可安全并发复用。

    单例是刻意的：每次调用新建 ``httpx.AsyncClient`` 会丢掉连接池，
    于是每次生成都要重做一次 TLS 握手。
    """
    return build_provider()


__all__ = ["SUPPORTED_PROVIDERS", "build_provider", "get_provider"]
