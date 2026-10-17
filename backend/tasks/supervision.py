"""运行监督任务（be-16 §5.1 / §5.2）。

两条入口，都是**薄壳**——业务判定一行都不在这里（``tasks/`` 只做编排，CLAUDE.md §4.5）：

- ``collect_active_runs``：beat 每 30 s 触发，挑出 ``running`` 的运行逐个抄指标；
- ``supervise_run``：``submit_run`` 提交成功后随 Outbox 投一条，立刻做第一次采集
  （不这么做的话，前 30 秒里跑起来的作业没人看着）。

## 为什么不是「每 30 秒给每个 run 入队一条 supervise」

``outbox_message.idempotency_key`` 是唯一约束，周期入队同一个 run 会撞约束；
而真正的驱动方式是**这一轮的采集循环自己往下走**，不是「每 30 秒重新催一次」。
``supervise_run`` 的存在只是为了消掉「提交后到下一个 beat 之间」的那段空窗。

## 三条实现纪律

1. **不用 ``asyncio.run``**，一律 ``run_coro``：``core.db`` / ``core.redis`` 是模块级单例，
   ``asyncio.run`` 每建一个循环就走一次关闭，第 2 个任务起必失败（``tasks/async_runner.py``）。
2. **用 ``SessionFactory()`` 而不是 ``session_scope()``**：事务边界归 service
   （``services.supervision`` 自己要 ``commit``），裹在 ``session.begin()`` 里
   会把那次提交变成「嵌套事务里的提交」，语义与在 ``get_session`` 下调用完全不同。
3. **单个 run 失败不带垮整批**：一次采集里某个 run 的数据完整性有问题
   （例如预算契约缺失）时，其余运行仍然要被抄到——监督环整批中断等于
   **所有**作业都没人看着，而这恰好是最坏的时刻。
"""

from __future__ import annotations

from typing import Any

from celery import shared_task
from sqlalchemy import select

from core.logging import get_logger
from tasks.async_runner import run_coro

log = get_logger(__name__)

#: 一次 beat 触发最多处理多少个运行。上限而不是全量：单轮采集里每个 run 都要
#: 发一次 SeaTunnel HTTP 请求，全量扫会让一轮跑完的时间随活跃运行数线性增长，
#: 最终长过 30 s 的周期，于是「每 30 秒采一次」变成「一直没采完」。
BATCH_LIMIT = 20


@shared_task(name="tasks.supervision.supervise_run", bind=True, ignore_result=False)
def supervise_run(self: Any, run_id: int) -> dict[str, Any]:
    """监督一次运行（提交后的首采，也可以被任何补偿动作重投）。

    幂等：重投时若运行已经是终态，``collect_run`` 会走 ``not_running`` 分支直接返回，
    不写时序点、不改状态。
    """
    return run_coro(_supervise_one(run_id))


@shared_task(name="tasks.supervision.collect_active_runs", bind=True, ignore_result=False)
def collect_active_runs(self: Any) -> dict[str, Any]:
    """周期性清点活跃运行并逐个采集（``beat_schedule`` 的 ``collect-run-metrics``）。

    名字已经写在 ``celery_app.py`` 的 beat 表里，签名与名字都属于既有契约，
    落实现时不许改——改了就是 worker 每 30 s 报一次 ``Received unregistered task``。
    """
    return run_coro(_collect_batch(BATCH_LIMIT))


async def _supervise_one(run_id: int) -> dict[str, Any]:
    from core.db import SessionFactory
    from core.ids import utcnow
    from services import supervision as service

    async with SessionFactory() as session:
        result = await service.collect_run(session, run_id=run_id, now=utcnow())

    log.info(
        "supervision_tick",
        run_id=run_id,
        collected=result.collected,
        status=result.status,
        severity=result.severity.value,
        dimension=result.reason_dimension,
        warned=result.warned,
        interrupted=result.interrupted,
        degraded=result.degraded,
    )
    return result.model_dump(mode="json")


async def _collect_batch(limit: int) -> dict[str, Any]:
    active = await _active_run_ids(limit)
    tally: dict[str, Any] = {
        "active": len(active),
        "collected": 0,
        "warned": 0,
        "interrupted": 0,
        "degraded": 0,
        "failed": 0,
    }

    for run_id in active:
        try:
            item = await _supervise_one(run_id)
        except Exception as exc:  # 单个 run 的问题不该让整批无人监督（见模块 docstring）
            tally["failed"] += 1
            # 只记类型：数据库驱动的异常文本里可能带着连接串与参数值，
            # 而日志是「禁止把敏感值拼进去」的第一现场（CLAUDE.md §6）。
            log.warning(
                "supervision_collect_failed", run_id=run_id, error_type=type(exc).__name__
            )
            continue
        if item.get("collected"):
            tally["collected"] += 1
        if item.get("warned"):
            tally["warned"] += 1
        if item.get("interrupted"):
            tally["interrupted"] += 1
        if item.get("degraded"):
            tally["degraded"] += 1

    log.info("supervision_batch", **tally)
    return tally


async def _active_run_ids(limit: int) -> list[int]:
    """挑出 ``running`` 的 run_id。

    **跨租户查**（beat 没有租户上下文）：这里只取 id，租户在 ``collect_run``
    里从那一行读出来并带进后续每一次查询。

    ``FOR UPDATE SKIP LOCKED`` 只在 PG 上生效（SQLite 会安静地不渲染它），
    而且这里**不能**靠它防重：锁随选中那个事务结束就释放了，采集发生在之后。
    真正保证「同一轮被抄两遍也不出错」的是 ``collect_run`` 自己的幂等
    （时序点按 30 s 取整覆盖 + 条件 UPDATE），不是这把锁。
    """
    from core.db import SessionFactory
    from models.enums import RunStatus
    from models.execution import ExecutionRun

    async with SessionFactory() as session:
        rows = await session.scalars(
            select(ExecutionRun.id)
            .where(ExecutionRun.status == RunStatus.RUNNING)
            .order_by(ExecutionRun.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        return [int(value) for value in rows.all()]


__all__ = ["BATCH_LIMIT", "collect_active_runs", "supervise_run"]
