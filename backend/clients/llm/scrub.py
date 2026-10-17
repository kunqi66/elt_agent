"""出站上下文边界（be-08 §6.6）。纯函数：无网络、无 IO、无时间。

**一份模式表，两种处置**——这是本模块唯一值得记住的设计：

* :func:`assert_outbound_safe`：命中**凭据类**模式即抛，不可配置关闭。
  这里拒的是「一旦发出去就追不回来」的东西：连接串、口令键值、私钥、Vault Token、
  ``sk-`` 长串。它们不属于「脱敏不彻底」，而属于「本来就不该进提示词」。
* :func:`scrub_text`：把**所有**命中（凭据类 + 个人信息）替换成等长掩码。
  它是「把这段文本洗成可以外发/可以留痕」的通用工具，用在那些不走闸门、
  但同样不能带秘密的场合（日志字段、诊断输出、缓存内容）。

两者共用同一份模式清单是刻意的：分开维护两套，结果必然是某天只更新了一处，
于是被闸门挡住的口令从另一条「只做替换」的路径漏了出去。处置不同（抛 vs 换）、
清单相同，是这里唯一稳的划法。

替换时**保留原文长度**（``*`` 等长覆盖，而**不是**零宽删除）：看日志或调试提示词的人
能立刻发现「这里本来有个东西」。零宽删除会让文本看起来天生如此，把一次泄漏
变成一件看不见的事。

首轮个人信息的脱敏是 be-04 探查阶段的职责，这里只做**兜底**（be-08 §8）——
所以 ``scrub_text`` 对普通业务文本（表名、列名、类型）必须是无损的。
"""

from __future__ import annotations

import re
from collections.abc import Sequence

import structlog

from clients.llm.base import ChatMessage, LLMContextViolationError

log = structlog.get_logger(__name__)

#: 禁投喂模式。每项是（正则, 模式名）——**只记模式名，绝不记命中片段**，
#: 因为命中片段本身就是我们要挡住的那个秘密。
FORBIDDEN_PATTERNS: tuple[tuple[str, str], ...] = (
    # scheme://user:pass@host —— 带凭据的连接串。要求 user:pass 同时出现，
    # 否则 https://api.deepseek.com 这种正常 URL 也会被误伤
    (r"[A-Za-z][A-Za-z0-9+.\-]*://[^\s/:@]+:[^\s/@]+@", "connection_string"),
    # 键值形式的凭据。``token=`` 之类的写法在配置文件片段里极常见
    (
        r"(?i)\b(?:pass(?:word|wd)?|token|api[_-]?key|access[_-]?key|secret[_-]?key|private[_-]?key)"
        r"\s*[:=]\s*\S+",
        "secret_kv",
    ),
    (r"-----BEGIN [A-Z ]*PRIVATE KEY-----", "private_key_block"),
    (r"\bhvs\.[A-Za-z0-9_\-]{8,}", "vault_token"),
    (r"\bsk-[A-Za-z0-9_\-]{8,}", "openai_style_key"),
)

#: 兜底脱敏规则（只替换、不抛错）。顺序即替换顺序，先长后短避免身份证被手机号规则切碎。
PII_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\b\d{17}[\dXx]\b", "id_card"),
    (r"\b\d{16,19}\b", "bank_card"),
    (r"\b1[3-9]\d{9}\b", "phone"),
    (r"\b[\w.+\-]+@[\w\-]+\.[\w.\-]+\b", "email"),
)


def _blot(text: str, pattern: str) -> tuple[str, int]:
    """等长覆盖：把命中片段换成同一长度的 ``*``，并返回替换次数。"""
    found = 0

    def _sub(match: re.Match[str]) -> str:
        nonlocal found
        found += 1
        return "*" * (match.end() - match.start())

    return re.sub(pattern, _sub, text, flags=re.IGNORECASE), found


def scrub_text(text: str) -> str:
    """把文本里所有命中（凭据类 + 个人信息）替换成等长掩码。

    注意它**不是**闸门的替代品：调用方在出站路径上必须先用
    :func:`assert_outbound_safe` 拒绝掉凭据类命中，再让本函数兜底。
    只调本函数的后果是「连接串被抹掉了，但没人知道它曾经出现过」——
    而「有人把连接串拼进了提示词」这件事本身需要被修掉，不是被抹掉。
    """
    return scrub_text_with_count(text)[0]


def scrub_text_with_count(text: str) -> tuple[str, int]:
    """同 :func:`scrub_text`，额外回报替换次数（写日志字段 ``scrubbed_count``，不记原文）。"""
    out = text
    total = 0
    for pattern, _name in (*FORBIDDEN_PATTERNS, *PII_PATTERNS):
        out, count = _blot(out, pattern)
        total += count
    return out, total


def _iter_text(payload: str | Sequence[ChatMessage]) -> list[str]:
    return [payload] if isinstance(payload, str) else [m.content for m in payload]


def assert_outbound_safe(payload: str | Sequence[ChatMessage]) -> None:
    """命中禁投喂模式即抛 :class:`LLMContextViolationError`。

    同时接受**整段文本**与**消息列表**两种入参：be-08 §3.2 的签名是消息列表
    （``assert_outbound_safe(messages)``），而请求级闸门往往只有一段已拼好的文本。
    两种形态各写一份实现，早晚会有一边漏掉一条模式。

    异常 ``details`` 里只放**命中的模式名**：命中片段是秘密，回显它等于二次泄漏，
    而它还会被写进响应与日志。
    """
    texts = _iter_text(payload)
    hits: set[str] = set()
    occurrences = 0
    for text in texts:
        for pattern, name in FORBIDDEN_PATTERNS:
            count = len(re.findall(pattern, text, flags=re.IGNORECASE))
            if count:
                hits.add(name)
                occurrences += count

    if hits:
        # 安全红线被触碰：必须 error 级并告警，不能只当一次参数校验失败
        log.error(
            "llm_context_violation",
            patterns=sorted(hits),
            occurrences=occurrences,
            message_count=len(texts),
        )
        raise LLMContextViolationError(
            "出站上下文命中禁投喂模式，请求已被拦截",
            details={"patterns": sorted(hits), "occurrences": occurrences},
        )


def prepare_outbound(messages: Sequence[ChatMessage]) -> tuple[list[ChatMessage], int]:
    """出站前的**唯一入口**：先断言、再兜底脱敏，返回可安全投喂的消息与替换计数。

    Provider 实现只调这一个函数，而不是「记得先 assert 再 scrub」——
    后者是两条必须记住的纪律，前者只有一条。
    """
    assert_outbound_safe(messages)
    scrubbed: list[ChatMessage] = []
    total = 0
    for message in messages:
        content, count = scrub_text_with_count(message.content)
        total += count
        scrubbed.append(message.model_copy(update={"content": content}) if count else message)
    return scrubbed, total


__all__ = [
    "FORBIDDEN_PATTERNS",
    "PII_PATTERNS",
    "assert_outbound_safe",
    "prepare_outbound",
    "scrub_text",
    "scrub_text_with_count",
]
