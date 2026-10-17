"""外部客户端单例的**存活配置**（core/db.py 与 core/redis.py）。

存在理由：这类参数（连接健康检查、重试）在功能上是「什么都不发生」，
删掉/漏掉不会有任何测试报错、本地也复现不出来——**只有在连接被中间设备掐断时才现形**。
实测过一次：本机→ECS 的 SSH 隧道重启后，API 侧靠 SQLAlchemy 的 ``pool_pre_ping``
自愈了，worker 侧却永久卡死（客户端是 ``lru_cache`` 单例、循环又是进程级复用的，
那条死连接再也不会被换掉），日志里只有 `Error 22 while writing to socket`，
看不出是连接池配置的问题。

所以这里断言的是**配置本身**，而不是「能不能连上」——后者在单测里没有真 Redis。
"""

from __future__ import annotations

from redis.asyncio.retry import Retry

from core.config import settings
from core.db import engine
from core.redis import get_redis


def test_redis_客户端带健康检查与重试() -> None:
    """缺 ``health_check_interval`` 时死连接会被无限复用；缺重试则「刚断就到的请求」仍失败。"""
    client = get_redis()
    kwargs = client.connection_pool.connection_kwargs

    assert kwargs.get("health_check_interval"), "没开健康检查：隧道抖动一次，worker 就永久卡死"
    assert kwargs["health_check_interval"] <= 60, "间隔太长等于没开"
    assert isinstance(client.get_retry(), Retry)

    # 重试必须覆盖连接类错误，否则等于配了个空重试
    retry_on_error = kwargs.get("retry_on_error") or []
    names = {cls.__name__ for cls in retry_on_error}
    assert {"ConnectionError", "TimeoutError"} <= names, f"重试没覆盖连接错误：{names}"


def test_redis_客户端是单例且指向配置里的库() -> None:
    """不是单例的话，每个模块各拿一个池，健康检查的意义会被摊薄成「看运气」。"""
    assert get_redis() is get_redis()
    assert get_redis().connection_pool.connection_kwargs["db"] == settings.REDIS_DB_CACHE


def test_sqlalchemy_连接池带_pre_ping() -> None:
    """与 Redis 那条是同一个坑的两个面：DB 侧靠它自愈，缺了同样会「断一次就永久坏」。"""
    # 读的是私有属性：SQLAlchemy 没有公开读法，而「配了 pre_ping」本身就是契约，
    # 这个断言正是为了让它在重构里丢掉时报红
    assert engine.pool._pre_ping is True
