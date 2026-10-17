"""数据源探测任务（队列 etl.default）。

**为什么探测要交给 Celery 而不是在 API 请求里做**：目标库可能不可达，
同步做就要在请求里等满超时。前端契约（fe-03）本身就是 202 + 轮询，
所以请求只需返回 test_id，探测在 worker 里跑完再写回 Redis。

**幂等要求**：``task_acks_late=True`` 下任务可能被重复投递。
重复执行一次只读探测的代价是「多连了一次库」，可以接受；
但要保证**重复执行不会覆盖掉一个已完成的结论**——否则用户会看到
结果从 succeeded 被改回 pending。所以入口先看 Redis 里是否已有终态。
"""

from __future__ import annotations

from typing import Any

from celery import shared_task

from core.logging import get_logger
from tasks.async_runner import run_coro

log = get_logger(__name__)


@shared_task(name="tasks.datasource.run_connectivity_test", bind=True, ignore_result=False)
def run_connectivity_test(
    self: Any,
    data_source_id: int,
    test_id: str,
    tenant_id: int,
    project_id: int,
    user_id: int,
    request_id: str,
) -> dict[str, Any]:
    """执行连通性探测并回写结果。

    返回体只含结论字段，**绝不含连接串**——Celery 的 result backend 是 Redis 的
    普通键，写进去的东西会被任何能访问该 Redis 的人读到。

    参数里没有 scope / tables / sample_rows：探查已经拆成独立任务
    （``tasks.metadata.run_metadata_probe``），连通性测试只答「连得上吗」。
    早先那个「顺带探一下表」的分支会让一次连通性测试悄悄读对方的库。
    """
    # 用 run_coro 而不是 asyncio.run：后者每次新建并关闭循环，第二次执行本任务时
    # 连接池里的连接还挂在已关闭的循环上，直接 Event loop is closed（见 tasks/async_runner.py）
    return run_coro(
        _run(
            data_source_id=data_source_id,
            test_id=test_id,
            tenant_id=tenant_id,
            project_id=project_id,
            user_id=user_id,
            request_id=request_id,
        )
    )


async def _run(
    *,
    data_source_id: int,
    test_id: str,
    tenant_id: int,
    project_id: int,
    user_id: int,
    request_id: str,
) -> dict[str, Any]:
    from core.db import SessionFactory
    from services import datasource as service

    async with SessionFactory() as session:
        existing = await service.load_test_result(test_id)
        if existing is not None and existing.get("state") != "pending":
            # 重复投递：已有终态就原样返回，不重跑、不覆盖
            log.info("connectivity_test_already_done", test_id=test_id, state=existing.get("state"))
            return existing

        try:
            detail = await service.run_probe(
                session,
                data_source_id,
                tenant_id=tenant_id,
                project_id=project_id,
                user_id=user_id,
                request_id=request_id,
            )
        except Exception as exc:
            # 任务本身不能抛：抛了 Celery 会重投，而重投也解决不了
            # 「目标库连不上」这类确定性失败。把结论写成终态更有用。
            log.error("connectivity_test_failed", test_id=test_id, error_type=type(exc).__name__)
            detail = {
                "state": "failed",
                "error_code": "INTERNAL_ERROR",
                "message": "探测未能完成，请查看服务端日志",
            }

        payload = {
            "test_id": test_id,
            "state": detail.get("state", "failed"),
            "latency_ms": detail.get("latency_ms"),
            "target": detail.get("target"),
            "server_version": detail.get("server_version"),
            "error_code": detail.get("error_code"),
            "message": detail.get("message"),
            "tested_at": _now_iso(),
        }
        await service.store_test_result(test_id, payload)
        return payload


def _now_iso() -> str:
    from core.ids import utcnow

    return utcnow().isoformat()
