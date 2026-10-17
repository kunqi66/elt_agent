"""健康检查。

- ``/healthz``：存活探针。**不查任何依赖**——依赖挂了不该导致容器被反复重启（重启解决不了 PG 掉线）。
- ``/readyz``：就绪探针。查 PG 与 Redis，任一不可用即 503，供网关摘流量使用。
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Response, status
from sqlalchemy import text

from core.config import settings
from core.db import engine
from core.logging import get_logger
from core.redis import get_redis

router = APIRouter(tags=["health"])
log = get_logger(__name__)

# 单项检查的上限。就绪探针是**要被网关和编排器高频调用**的接口，
# 它必须比调用方的超时更快地给出答案。
#
# 不设上限时的真实表现：asyncpg 连一个「路由不可达」的地址会一直等到
# 自己的 60 秒超时，`curl -m 8 /readyz` 拿到的是 000（超时）而不是 503——
# 于是网关既摘不掉流量、也拿不到「未就绪」这个结论，nginx 只会把
# 请求堆在这里，把一次依赖故障放大成一次全站超时。
_ITEM_TIMEOUT_SEC = 3.0


async def _probe(name: str, awaitable: Any, checks: dict[str, str]) -> None:
    """跑一项依赖检查，把结果写进 ``checks``。

    探针必须吞掉**所有**异常并降级为 fail，不能冒泡成 500：
    500 会让调用方以为「探针自身坏了」，而事实是「依赖不可用」，两者该有不同的处置。
    """
    try:
        await asyncio.wait_for(awaitable, timeout=_ITEM_TIMEOUT_SEC)
    except TimeoutError:
        checks[name] = "timeout"
        log.warning("readyz_timeout", dependency=name, timeout_sec=_ITEM_TIMEOUT_SEC)
    except Exception as exc:
        checks[name] = "fail"
        log.warning("readyz_failed", dependency=name, error_type=type(exc).__name__)
    else:
        checks[name] = "ok"


@router.get("/healthz", summary="存活探针")
async def healthz() -> dict[str, Any]:
    return {"status": "ok", "app": settings.APP_NAME, "env": settings.APP_ENV}


@router.get("/readyz", summary="就绪探针")
async def readyz(response: Response) -> dict[str, Any]:
    checks: dict[str, str] = {}

    async def _pg() -> None:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))

    async def _redis() -> None:
        await get_redis().ping()

    # 并发探测：两项各 3 秒上限，串行最坏情况是 6 秒，而它们互不依赖
    await asyncio.gather(_probe("postgres", _pg(), checks), _probe("redis", _redis(), checks))

    healthy = all(v == "ok" for v in checks.values())
    if not healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"status": "ok" if healthy else "degraded", "checks": checks}
