"""Outbox 投递任务（beat 每 2s 触发，be-14 §5.2）。

**它只负责搬运**：把 ``outbox_message`` 里到点的 ``pending`` 行变成一次队列投递。
业务判定一行都不在这里——投递成功不等于业务成功（``sent`` 只表示命令进了 broker）。

三处设计要点：

- ``FOR UPDATE SKIP LOCKED``：多个 beat/worker 实例并发扫描也安全，每条只被一个扫描者取到。
  SQLite 上没有这个语法，SQLAlchemy 会**安静地不渲染**它——所以本机单测跑的是
  「单扫描者」路径，并发那条只能在 PG 上验（见 be-14 §9）。
- **先投递、后改状态**：投递失败的那条要按退避重试。反过来（先标 sent 再投递）
  会在投递失败时把消息永久丢掉——而 Outbox 存在的全部意义就是「不丢」。
- **未知 topic 直接判 ``dead``**：退避重试一个永远匹配不到消费者的 topic 只是
  把一次配置错误拖成五次安静的重试，最后还是死。当场判死 + 写清 ``last_error``，
  排查方向才落在「是谁写错了 topic」上。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from celery import shared_task
from sqlalchemy import select

from core.config import settings
from core.db import session_scope
from core.ids import utcnow
from core.logging import get_logger
from harness.outbox import Topic, next_available_at
from models.enums import OutboxStatus
from models.evidence import OutboxMessage
from tasks.async_runner import run_coro

log = get_logger(__name__)


def _consumers() -> Mapping[str, Callable[..., Any]]:
    """topic → 消费任务。

    惰性 import：``tasks.execution`` 反过来会 import ``services``，
    而本模块在 beat 启动时就被加载——顶层 import 会形成一条启动期的循环。
    """
    from tasks import execution, supervision

    return {
        Topic.SEATUNNEL_SUBMIT.value: execution.submit_run,
        Topic.RUN_STOP.value: execution.stop_run,
        Topic.RUN_SUPERVISE.value: supervision.supervise_run,
        # 回滚命令（``services.publish.request_rollback`` 投的）在 be-18 落地前
        # **没有消费者**：它会被判 ``dead``，库里只留一行 ``last_error=未知 topic``，
        # 而页面上的症状是「点了回滚，影子表没被清理，状态停在进行中」。
        Topic.TARGET_ROLLBACK.value: execution.rollback_run,
        # ``etl.target.publish_swap`` **刻意仍然没有消费者**，而不是忘了加：
        # be-18 的发布不走 Outbox——令牌由 ``services/publish`` 自己签发、执行也在
        # 同一进程内（发布没有 HTTP 入口，见 publish.py 开头第 2 条偏差），
        # 由 ``tasks.execution.scan_finished_runs`` 直接调用。挂一个空实现的话，
        # 消息会被标 ``sent`` 却什么都不做——「发布成功但正式表没换」在库里看不出来。
        # 真要有别的模块投这个 topic，那条消息会在这里被判 ``dead`` 并写明原因。
    }


def _args_for(topic: str, payload: Mapping[str, Any]) -> tuple[list[Any], dict[str, Any]]:
    """载荷 → 任务入参。**逐 topic 显式取键**，不做 ``**payload`` 透传。

    透传会让「消费任务签名改了」与「投递方载荷没改」之间的不匹配变成运行期的
    ``TypeError``，而那时消息已经被标成 ``sent``（投递本身成功了）。
    显式取键把这件事提前到投递前：缺键就是一次可重试的投递失败。
    """
    missing = [key for key in ("run_id",) if key not in payload]
    if missing:
        raise ValueError(f"载荷缺少必需键：{'、'.join(missing)}")
    args: list[Any] = [int(payload["run_id"])]
    kwargs: dict[str, Any] = {}
    capability = payload.get("capability")
    if isinstance(capability, str):
        kwargs["capability"] = capability
    if topic == Topic.RUN_STOP.value:
        # ``subject_id`` 对停止命令是**必需**键，不给默认值：worker 侧 broker
        # 要求 ``ctx.actor_id`` 非空，缺了它这条命令注定被拒。少了键却照投，
        # 等于把一次载荷缺陷延迟成 worker 里一句「缺少操作主体」——
        # 那时消息已被标 ``sent``，而「有人试图停作业但没停成」这件事在
        # Outbox 表里看不到任何痕迹。缺键在这里就判死（下面 ``ValueError``
        # 分支会把消息置 ``DEAD``），让它出现在告警里。
        if "subject_id" not in payload:
            raise ValueError("停止命令载荷缺少必需键：subject_id")
        kwargs["subject_id"] = int(payload["subject_id"])
        kwargs["reason"] = str(payload.get("reason") or "manual")
    return args, kwargs


async def _scan(batch_size: int) -> dict[str, Any]:
    now = utcnow()
    sent = 0
    retried = 0
    dead = 0
    consumers = _consumers()

    async with session_scope() as session:
        rows = (
            await session.scalars(
                select(OutboxMessage)
                .where(
                    OutboxMessage.status == OutboxStatus.PENDING,
                    OutboxMessage.available_at <= now,
                )
                .order_by(OutboxMessage.available_at, OutboxMessage.id)
                .limit(batch_size)
                .with_for_update(skip_locked=True)
            )
        ).all()

        for row in rows:
            # attempts 的语义是「已尝试投递次数」，累加后即为本次失败要用的退避档位
            attempts = row.attempts + 1
            try:
                consumer = consumers.get(row.topic)
                if consumer is None:
                    raise ValueError(f"未知 topic：{row.topic}")
                args, kwargs = _args_for(row.topic, dict(row.payload or {}))
                consumer.delay(*args, **kwargs)
            except Exception as exc:
                row.attempts = attempts
                row.last_error = f"{type(exc).__name__}: {exc}"[:500]
                if attempts >= settings.OUTBOX_MAX_ATTEMPTS or isinstance(exc, ValueError):
                    # 未知 topic / 载荷缺键不是瞬时故障：重试到上限只是把一次配置错误
                    # 拖成 N 次安静的重试。判死，让它出现在告警里。
                    row.status = OutboxStatus.DEAD
                    dead += 1
                else:
                    row.status = OutboxStatus.FAILED
                    row.available_at = next_available_at(attempts, now=_aware(row.available_at))
                    retried += 1
                log.warning(
                    "outbox_dispatch_failed",
                    outbox_id=row.id,
                    topic=row.topic,
                    attempts=attempts,
                    error=row.last_error,
                )
                continue

            row.status = OutboxStatus.SENT
            row.sent_at = now
            row.dispatched = True
            # attempts 也累加：它记录的是「投递动作发生过几次」，
            # 与成功与否无关。只看成功次数的计数在排查「投递了多少轮」时是错的。
            row.attempts = attempts
            sent += 1

    return {"scanned": len(rows), "sent": sent, "retried": retried, "dead": dead}


def _aware(value: datetime) -> datetime:
    """SQLite 读回的 ``available_at`` 是裸值，退避加法要在带时区的基准上做。"""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


@shared_task(name="tasks.outbox.scan_and_dispatch", ignore_result=False)
def scan_and_dispatch(batch_size: int = 50) -> dict[str, Any]:
    """扫一批待投递消息并派发。返回计数字典（便于 beat 日志与压测观察）。"""
    return run_coro(_scan(batch_size))


__all__ = ["scan_and_dispatch"]
