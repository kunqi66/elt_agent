"""门禁五类检查共用的底座：脱敏文案出口、结果去重与截断、密钥令牌扫描。

放在这里的都是**每个检查都会用到**的东西，避免五份 check 各写一遍导致口径漂移：

1. :func:`safe_message`：``GateFinding.message`` 有 512 长度上限，且会被前端**原样渲染**。
   「不回显原文」靠调用点纪律——检查只往里放字段名、行号、类型名，**不放样值与口令**；
   这个函数负责的是机械的两件事：把长度收进上限、把换行与控制字符抹平。
   必须在构造 finding **之前**调用：文案超长会让 Pydantic 在 ``make_finding`` 里抛错，
   而门禁检查不许抛业务异常。
2. :func:`finalize_findings`：去重（同一个 id 只留第一条）+ 截断（上限 50 条，
   越界时**追加一条 warn 说明被截断**）。静默截断会让人以为「就这些了」，
   从而漏掉后面那批真问题。
3. :func:`scan_plaintext_secret_fields` / :func:`iter_secret_tokens`：
   HOCON 渲染纪律的**同一份行级扫描器**——``check_syntax`` 用来判令牌形态，
   ``check_resource`` 用来判令牌登记。两处各写一遍就会出现「一边认、一边不认」的令牌。

## 本模块新增的 code（be-11 未定义）

- ``findings_truncated``：be-11 §6.1-§6.4 的码表里没有「结果被截断」这一种。
  它是**检查框架**层面的告警（不是业务问题），所以在此登记而不是散在某个 check 里。
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from engines.gate.types import GateCheckId, GateFinding, PlanLocator, make_finding
from engines.hocon.types import SECRET_FIELDS, TOKEN_RE

#: 单条检查的 finding 上限（be-11 未规定，取一个模块级常量）。
#: 一份 500 字段的 Plan 若每列都错，前端会被几百条 finding 刷屏，
#: 而真正要先修的永远是前几条。
MAX_FINDINGS_PER_CHECK = 50

#: ``GateFinding.message`` 的硬上限（types.py 的 ``Field(max_length=512)``）。留 0 余量。
MESSAGE_MAX_LEN = 512

#: 截断告警的 code（见模块 docstring）。
TRUNCATION_CODE = "findings_truncated"

#: 整个 Plan 的位置。用于「没有更细的定位」的问题（如预算、策略命中）。
PLAN_ROOT = PlanLocator(path="/")

_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_WS_RE = re.compile(r"\s+")

#: ``key = value`` / ``key: value``。只取「行内第一个赋值」，够用：
#: 我们要找的是连接字段（password/url/username），它们不会被写在同一行的表达式里。
_SECRET_ASSIGN_RE = re.compile(r"^\s*(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*[=:]\s*(?P<val>.*)$")

#: 需要盯着「值必须是令牌」的键名。
#:
#: **不能只用 SECRET_FIELDS**：``SECRET_FIELDS`` 描述的是**令牌中间的字段名**
#: （``{{secret:1:jdbc_url}}`` 里的 ``jdbc_url``），而 HOCON 里的**键名**是连接器
#: 自己的叫法（渲染器写的是 ``url`` / ``user`` / ``password``，见 hocon/types.py 的示例）。
#: 只按 ``SECRET_FIELDS`` 找键名会**一个都找不到**——这条安全红线会静默失效。
_SUSPECT_SECRET_KEYS = frozenset({*SECRET_FIELDS, "url", "user", "pass", "passwd", "pwd"})

#: 值「看起来想写令牌但没写对」的前缀。不是口令，是令牌的起始形式，S105 是误报。
_TOKEN_ATTEMPT_PREFIX = "{{secret:"  # noqa: S105

_EMPTY_MESSAGE = "（该问题无可展示的细节，请查看对应位置的配置）"


def safe_message(text: str, *, limit: int = MESSAGE_MAX_LEN) -> str:
    """所有 finding 文案的**唯一出口**：抹平空白/控制字符并收进长度上限。

    不给默认值以外的行为：它**不做**内容层面的脱敏（它无从知道哪个词是口令），
    那由调用点保证——检查只把字段名与行号拼进来。
    """
    cleaned = _WS_RE.sub(" ", _CONTROL_RE.sub("", text)).strip()
    if not cleaned:
        # 全是控制字符时不能返回空串：GateFinding.message 有 min_length=1。
        return _EMPTY_MESSAGE
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1] + "…"


def finalize_findings(check_id: GateCheckId, findings: Sequence[GateFinding]) -> list[GateFinding]:
    """每条检查返回前的统一收口：去重 + 截断。

    去重按 ``finding.id``：id 由 ``(check_id, code, locator)`` 派生，
    两条 id 相同的 finding 在去抖（be-11 §6.6 规则 4）眼里本就是同一条问题，
    留着反而会让「问题数」虚高。
    """
    deduped: list[GateFinding] = []
    seen: set[str] = set()
    for finding in findings:
        if finding.id in seen:
            continue
        seen.add(finding.id)
        deduped.append(finding)

    if len(deduped) <= MAX_FINDINGS_PER_CHECK:
        return deduped

    capped = deduped[:MAX_FINDINGS_PER_CHECK]
    capped.append(
        make_finding(
            check_id=check_id,
            code=TRUNCATION_CODE,
            severity="warn",
            message=safe_message(
                f"本类检查共命中 {len(deduped)} 条问题，超过展示上限"
                f" {MAX_FINDINGS_PER_CHECK} 条，已截断；请先修复列出的问题后重跑门禁。"
            ),
            locator=PLAN_ROOT,
        )
    )
    return capped


# ---------------------------------------------------------------- HOCON 扫描


def iter_secret_tokens(text: str) -> tuple[tuple[int, int, str], ...]:
    """逐行扫出密钥令牌 → ``(line, data_source_id, field)``。

    ``field`` 是令牌中间的字段名（``{{secret:1:password}}`` → ``password``）。
    ``line`` 从 1 起，供 :class:`HoconLocator` 定位。
    """
    found: list[tuple[int, int, str]] = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        for match in TOKEN_RE.finditer(raw):
            found.append((lineno, int(match.group(1)), match.group(2)))
    return tuple(found)


def scan_plaintext_secret_fields(text: str) -> tuple[tuple[int, str, str], ...]:
    """扫出「值的形态不对」的连接字段 → ``(line, key, kind)``。

    ``kind`` ∈ ``{"plaintext_secret", "bad_placeholder"}``：
    值既不是合法令牌也不是空 → 要么是明文凭据，要么是写坏的令牌。

    **不校验令牌里的字段名是否在白名单**——那由 :func:`iter_secret_tokens` 的使用方判，
    因为「值不是令牌」与「令牌字段非法」是两种不同的处置（前者是红线，后者是配置错误）。
    """
    offenders: list[tuple[int, str, str]] = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        match = _SECRET_ASSIGN_RE.match(raw)
        if match is None:
            continue
        key = match.group("key")
        if key not in _SUSPECT_SECRET_KEYS:
            continue
        value = _unquote(match.group("val"))
        if not value or TOKEN_RE.fullmatch(value):
            continue
        kind = "bad_placeholder" if value.startswith(_TOKEN_ATTEMPT_PREFIX) else "plaintext_secret"
        offenders.append((lineno, key, kind))
    return tuple(offenders)


def _unquote(raw: str) -> str:
    """去掉包裹引号；无引号时截掉 ``#`` 之后的注释。

    只做这一层：HOCON 的值语法远比这复杂，但**连接字段的值**要么是带引号的令牌、
    要么是带引号的明文，真正需要还原的场景就在这两类里。
    """
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1].strip()
    hash_at = value.find("#")
    if hash_at >= 0:
        value = value[:hash_at]
    return value.strip()
