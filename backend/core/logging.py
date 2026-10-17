"""结构化日志 + 统一脱敏。

安全红线（CLAUDE.md §5.2）：Secret、连接串、身份证/手机号/邮箱等敏感值**不得**进入日志。
脱敏在这里集中实现，业务代码只管 ``log.info("...", foo=bar)``，不得自行拼接敏感值、
也不得因为「我已经脱敏了」而绕过本处理器。
"""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import MutableMapping
from typing import Any

import structlog

from core.config import settings

# --- 1) 键名命中即整体掩码 -------------------------------------------------
_SENSITIVE_KEY_RE = re.compile(
    r"(pass(word|wd)?|secret|token|api[_-]?key|apikey|authorization|auth|credential|"
    r"private[_-]?key|signature|salt|session|conn(ection)?[_-]?str)",
    re.IGNORECASE,
)

# --- 2) 值形态命中即替换（键名不敏感但值是密钥的情况）-----------------------
_VALUE_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"sk-[A-Za-z0-9_\-]{8,}"), "[REDACTED_API_KEY]"),
    (re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{8,}", re.IGNORECASE), "Bearer [REDACTED]"),
    (
        re.compile(
            r"\b(?:postgresql|postgres|mysql|redis|mongodb|amqp)(?:\+\w+)?://\S+",
            re.IGNORECASE,
        ),
        "[REDACTED_DSN]",
    ),
    (
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
        "[REDACTED_PRIVATE_KEY]",
    ),
    (re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"), "[REDACTED_JWT]"),
    # 个人信息：身份证 18 位 / 手机号 11 位 / 邮箱
    (re.compile(r"\b\d{17}[\dXx]\b"), "[REDACTED_ID_CARD]"),
    (re.compile(r"\b1[3-9]\d{9}\b"), "[REDACTED_PHONE]"),
    (re.compile(r"\b[\w.+\-]+@[\w\-]+\.[\w.\-]+\b"), "[REDACTED_EMAIL]"),
)

_MASK = "***"
_MAX_DEPTH = 6


def _mask_value(value: str) -> str:
    """保留首尾各 2 个字符，中间掩码；短值全部掩码。"""
    if len(value) <= 6:
        return _MASK
    return f"{value[:2]}{_MASK}{value[-2:]}"


def _scrub(value: Any, depth: int = 0) -> Any:
    if depth > _MAX_DEPTH:
        return "[TRUNCATED]"
    if isinstance(value, str):
        out = value
        for pattern, replacement in _VALUE_RULES:
            out = pattern.sub(replacement, out)
        return out
    if isinstance(value, dict):
        return {
            k: (_MASK if _SENSITIVE_KEY_RE.search(str(k)) else _scrub(v, depth + 1)) for k, v in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        return type(value)(_scrub(v, depth + 1) for v in value)
    if isinstance(value, bytes):
        return f"<bytes:{len(value)}>"
    return value


def find_sensitive_paths(value: Any, *, depth: int = 0, path: str = "$") -> list[str]:
    """扫描 ``value``，返回所有命中脱敏规则的路径（点分表示）。

    与 ``_scrub`` 的区别是**只报告、不修改**：日志处理器把命中值替换成 ``***`` 就够，
    因为日志是易失的；但证据账本必须**拒绝写入**（见 be-06 §6.2）——
    账本永久保留且是审计事实来源，静默替换会产生「看起来很干净」的记录，
    而真正的泄露已经发生了。所以账本要的是「告诉我哪里有问题」，而不是「帮我擦干净」。

    ``_SENSITIVE_KEY_RE`` / ``_VALUE_RULES`` 只此一份：账本另写一套黑名单，
    结果必然是某天只更新了一处，于是日志挡住了的密钥从账本漏了出去。
    """
    if depth > _MAX_DEPTH:
        return []
    hits: list[str] = []
    if isinstance(value, str):
        for pattern, _replacement in _VALUE_RULES:
            if pattern.search(value):
                hits.append(path)
                break
        return hits
    if isinstance(value, dict):
        for key, item in value.items():
            child = f"{path}.{key}"
            if _SENSITIVE_KEY_RE.search(str(key)):
                hits.append(child)
                # 键名已命中就不再往下走：整棵子树都会被掩码，报一个根路径更有用
                continue
            hits.extend(find_sensitive_paths(item, depth=depth + 1, path=child))
        return hits
    if isinstance(value, (list, tuple, set)):
        for index, item in enumerate(value):
            hits.extend(find_sensitive_paths(item, depth=depth + 1, path=f"{path}[{index}]"))
        return hits
    return hits


def redact_processor(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """structlog 处理器：对事件字典做整体脱敏。"""
    for key in list(event_dict.keys()):
        if _SENSITIVE_KEY_RE.search(key):
            event_dict[key] = _MASK
        else:
            event_dict[key] = _scrub(event_dict[key])
    return event_dict


def configure_logging(level: str | None = None) -> None:
    """在应用与 Celery 启动时各调用一次。"""
    log_level = (level or ("INFO" if settings.is_production else "DEBUG")).upper()

    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,  # request_id / tenant_id 等由中间件绑定
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
        redact_processor,  # 必须在渲染之前
    ]

    renderer: Any = (
        structlog.processors.JSONRenderer(ensure_ascii=False)
        if settings.is_production
        else structlog.dev.ConsoleRenderer(colors=True)
    )

    structlog.configure(
        processors=[*shared_processors, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta, renderer],
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(log_level)

    # 第三方库降噪，并统一走脱敏 pipeline
    for noisy, noisy_level in (
        ("uvicorn.access", "WARNING"),
        ("sqlalchemy.engine", "WARNING"),
        ("botocore", "WARNING"),
        ("httpx", "WARNING"),
        ("httpcore", "WARNING"),
    ):
        logging.getLogger(noisy).setLevel(noisy_level)


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    # structlog.get_logger 的返回类型是 Any（它按运行时配置产出不同 binding），
    # 这里显式收敛成本项目实际使用的 BoundLogger，让调用方拿到类型提示
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name) if name else structlog.get_logger()
    return logger
