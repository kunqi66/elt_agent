"""事务 Outbox（be-14 §5.1 / §5.2）。

本模块只解决一个问题：**让「已经发生的事实」与「待发出的命令」在同一个原子提交里落地**。

为什么不能直接 ``apply_async``：它是**事务外的一次网络写入**。若先投递再提交，
事务回滚（唯一约束冲突、断言失败、连接断开）会留下一张**已被 Worker 收到的幽灵命令**——
命令指向的 ``execution_run`` 根本不存在，Worker 只能失败并重试到 ``dead``；
而「把已发出的消息撤回」在 Redis broker 上不可靠（消息可能已被取走）。
Outbox 把两者放进同一个提交：提交成功则命令**最终必然**被投递（至少一次），
提交失败则命令**根本不存在**。

三条不可退让的规则：

1. ``enqueue()`` **只写库**，绝不投递、绝不 ``apply_async``、绝不 commit
   （事务边界归调用方，否则「同事务」这个前提就没了）。
2. ``payload`` 只允许放**引用与标量**（``run_id`` / ``actor_id`` / ``reason`` …），
   入队前 ``assert_payload_has_no_secret`` 强制扫描。唯一例外是 ``capability`` 键：
   令牌不是凭据（不含 Secret 值、TTL 300 s、单次消费），且 ``capability`` 表只存
   ``token_sha256``，原文无法重建——但**必须随载荷走**，否则 Worker 无从验签。
3. 退避是**纯函数**（``next_available_at``）：投递器只负责搬值，
   指数曲线不写在 SQL 里，这样它能被单测直接钉住。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from core.errors import ValidationError
from core.ids import utcnow
from models.enums import OutboxStatus
from models.evidence import OutboxMessage

#: 退避上限。没有上限时 ``2 ** attempts`` 会在几十次之后变成天文数字，
#: 一条永远重投不成功的消息会静默地「再也不投」——那和 ``dead`` 的差别只是没人知道。
BACKOFF_CAP_SEC = 300

#: 唯一允许携带非标量值的载荷键，见模块 docstring 第 2 条。
CAPABILITY_KEY = "capability"

#: 键名与字符串值双重扫描的敏感词。**宁可误报也不放过**：
#: 误报的代价是调用方换个键名（并被迫想清楚「这里到底为什么需要它」），
#: 漏报的代价是连接串永久留在 ``outbox_message.payload`` 里。
_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"password",
        r"passwd",
        r"\bpwd\b",
        r"secret",
        r"token",  # capability 键单独豁免；其余任何叫 token 的东西都不该进载荷
        r"credential",
        r"dsn\b",
        r"connection[_-]?string",
        r"conn[_-]?str",
        r"private[_-]?key",
        r"key[_-]?value",
        r"access[_-]?key",
        r"api[_-]?key",
        r"://[^/\s:@]+:[^/\s@]+@",  # 形如 scheme://user:pass@host 的连接串
    )
)

#: JWT 形状：三段 base64url 用点分隔。用来确认 ``capability`` 键里放的真是令牌，
#: 而不是「有人把口令塞进了一个被豁免的键」。
_JWT_SHAPE = re.compile(r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$")


class Topic(StrEnum):
    """命令主题。取值即契约：写入 ``outbox_message.topic`` 后由投递器按表派发。

    用枚举而不是裸字符串，是因为「topic 拼错」的失败形态特别难查——
    消息进库了、状态是 pending、投递器也在跑，只是**永远匹配不到消费者**，
    于是它安静地退避到 ``dead``，而没有任何一处报错。
    """

    SEATUNNEL_SUBMIT = "etl.seatunnel.submit"
    RUN_SUPERVISE = "etl.run.supervise"
    RUN_STOP = "etl.run.stop"
    TARGET_PUBLISH_SWAP = "etl.target.publish_swap"
    TARGET_ROLLBACK = "etl.target.rollback"
    BENCHMARK_RUN = "etl.benchmark.run"


@dataclass(frozen=True, slots=True)
class OutboxHandle:
    id: int
    topic: str
    idempotency_key: str


def next_available_at(attempts: int, *, now: datetime) -> datetime:
    """退避时刻：``now + min(2 ** attempts, 300)`` 秒。

    ``attempts`` 是**即将累加后**的值（第一次失败传 1 → 2 s），
    这样「第 n 次失败后等多久」只由一次调用决定，不用调用方自己做加法。
    """
    if attempts < 1:
        # 传 0 是调用方把「已失败次数」与「本次之后的次数」搞混了。
        # 不悄悄按 1 处理：那会让第一次重试的间隔取决于谁传的参数，而两边都自洽。
        raise ValueError("next_available_at 的 attempts 必须 >= 1（传即将累加后的值）")
    return now + timedelta(seconds=min(2**attempts, BACKOFF_CAP_SEC))


def assert_payload_has_no_secret(payload: Mapping[str, Any]) -> None:
    """扫描载荷：键名与字符串值都查，命中即拒绝入队。

    递归进 dict/list：Secret 藏在 ``{"args": {"password": ...}}`` 这种嵌套里时，
    只扫顶层等于没扫。``capability`` 键整体豁免，但**要求取值长得像 JWT**——
    否则「豁免键」就成了绕过扫描的后门。
    """
    for path, value in _walk(payload):
        leaf = path.rsplit(".", 1)[-1]
        if leaf == CAPABILITY_KEY:
            if not isinstance(value, str) or not _JWT_SHAPE.match(value):
                raise ValidationError(f"载荷 {path} 必须是 Capability 令牌原文（JWT 形状）")
            continue
        if _looks_sensitive(leaf):
            raise ValidationError(f"载荷键 {path} 看起来承载了凭据，Outbox 只允许放引用与标量")
        if isinstance(value, str) and _looks_sensitive(value):
            raise ValidationError(f"载荷 {path} 的值里出现了连接串/口令形态的内容")


def _looks_sensitive(text: str) -> bool:
    return any(pattern.search(text) for pattern in _SECRET_PATTERNS)


def _walk(payload: Any, prefix: str = "") -> list[tuple[str, Any]]:
    """拍平成 ``(路径, 叶子值)``。路径只为报错时能指出是哪一项。"""
    found: list[tuple[str, Any]] = []
    if isinstance(payload, Mapping):
        for key, value in payload.items():
            child = f"{prefix}.{key}" if prefix else str(key)
            found.extend(_walk(value, child))
    elif isinstance(payload, Sequence) and not isinstance(payload, str | bytes):
        for index, value in enumerate(payload):
            found.extend(_walk(value, f"{prefix}[{index}]"))
    else:
        found.append((prefix, payload))
    return found


async def enqueue(
    session: AsyncSession,
    *,
    topic: Topic | str,
    payload: Mapping[str, Any],
    idempotency_key: str,
    available_at: datetime | None = None,
) -> OutboxHandle:
    """把一条待发命令写进 Outbox。**不提交、不投递**（见模块 docstring 第 1 条）。

    ``available_at`` 默认取当前时刻；显式传入只用于「延迟投递」这种测试与补偿场景。
    """
    assert_payload_has_no_secret(payload)
    row = OutboxMessage(
        topic=str(topic),
        payload=dict(payload),
        status=OutboxStatus.PENDING,
        attempts=0,
        available_at=available_at or utcnow(),
        idempotency_key=idempotency_key,
    )
    session.add(row)
    await session.flush()
    return OutboxHandle(id=row.id, topic=row.topic, idempotency_key=row.idempotency_key)


__all__ = [
    "BACKOFF_CAP_SEC",
    "CAPABILITY_KEY",
    "OutboxHandle",
    "Topic",
    "assert_payload_has_no_secret",
    "enqueue",
    "next_available_at",
]
