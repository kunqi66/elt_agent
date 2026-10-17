"""元数据探查任务（队列 etl.default）。

**为什么单独一个任务而不是塞进连通性测试**：两者回答的问题不同
（「连得上吗」vs「里面长什么样」），失败语义也不同——连通性失败时档案
根本不该生成。早先那种「连通性测试顺带探一下表」的写法会让一次只读探测
悄悄读对方的库，且 scope 参数藏在连通性接口的请求体里，前端无从表达。

**幂等要求**：``task_acks_late=True`` 下任务可能重复投递。重复探查的代价是
「多读一次源库」，可以接受；但**不能覆盖已是终态的档案**——否则用户会看到
状态从 ready 被改回 probing。所以入口先看档案状态。
"""

from __future__ import annotations

from typing import Any

from celery import shared_task

from core.logging import get_logger
from tasks.async_runner import run_coro

log = get_logger(__name__)


@shared_task(name="tasks.metadata.run_metadata_probe", bind=True, ignore_result=True)
def run_metadata_probe(
    self: Any,
    data_source_id: int,
    profile_id: int,
    tenant_id: int,
    project_id: int,
    user_id: int,
    request_id: str,
    scope: str = "schema_only",
    tables: list[str] | None = None,
    sample_rows: int = 100,
    masking_policy: dict[str, Any] | None = None,
) -> None:
    """执行探查并落档案。

    ``ignore_result=True``：结论写在 ``metadata_profile`` 行里，不在 result backend。
    把一份 schema 快照塞进 Redis 的 result 键，既会撑爆内存，
    也会让「谁能读到探查结果」多出一个不受 tenant 过滤约束的出口。
    """
    # 与 tasks/datasource.py 同理：不能用 asyncio.run——每次关循环会让第 2 个任务
    # 取到挂在已关闭循环上的连接（见 tasks/async_runner.py）
    run_coro(
        _run(
            data_source_id=data_source_id,
            profile_id=profile_id,
            tenant_id=tenant_id,
            project_id=project_id,
            user_id=user_id,
            request_id=request_id,
            scope=scope,
            tables=tuple(tables or ()),
            sample_rows=sample_rows,
            masking_policy=dict(masking_policy or {}),
        )
    )


async def _run(
    *,
    data_source_id: int,
    profile_id: int,
    tenant_id: int,
    project_id: int,
    user_id: int,
    request_id: str,
    scope: str,
    tables: tuple[str, ...],
    sample_rows: int,
    masking_policy: dict[str, Any],
) -> None:
    from core.db import SessionFactory
    from services import metadata as service

    async with SessionFactory() as session:
        state = await service.load_profile_state(session, tenant_id=tenant_id, profile_id=profile_id)
        if state is None:
            # 档案被删了（数据源被删会级联删档案）。任务照跑只会写一条孤儿行
            log.warning("metadata_probe_profile_missing", profile_id=profile_id)
            return
        if state != "probing":
            log.info("metadata_probe_already_done", profile_id=profile_id, state=state)
            return

        try:
            await service.run_probe(
                session,
                data_source_id=data_source_id,
                profile_id=profile_id,
                tenant_id=tenant_id,
                project_id=project_id,
                user_id=user_id,
                request_id=request_id,
                scope=scope,
                tables=tables,
                sample_rows=sample_rows,
                masking_policy=masking_policy,
            )
        except Exception:
            # 任务不抛：抛了 Celery 会重投，而重投解决不了「源库读不了」这类确定性失败。
            # 真正的错误细节由 service 层记日志——那里才拿得到会话与档案行
            log.error("metadata_probe_task_failed", profile_id=profile_id)
