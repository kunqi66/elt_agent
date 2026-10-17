"""Redis 客户端与键命名规范。

职责边界（见 CLAUDE.md §2）：Redis 只承担
1. Celery broker / result
2. 读缓存
3. 登录失败计数
4. **Capability 消费标记**（与 PG 唯一约束构成双保险）

Redis 里**不得**出现业务事实的唯一副本（状态机、审批结论一律以 PG 为准）。
"""

from __future__ import annotations

from functools import lru_cache

from redis.asyncio import Redis
from redis.asyncio.retry import Retry
from redis.backoff import ExponentialWithJitterBackoff
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError

from core.config import settings

# 显式导出：``harness/`` 等模块要按 ``from core.redis import Redis`` 取类型标注，
# 没有 __all__ 时 mypy 不认这种再导出
__all__ = [
    "KEY_CAP_CONSUMED",
    "KEY_GEN_LOCK",
    "KEY_LOGIN_FAIL",
    "KEY_META_CACHE",
    "KEY_TOKEN_DENY",
    "Redis",
    "close_redis",
    "get_redis",
]

# --- 键空间规范：{模块}:{实体}:{标识}，禁止随手起名 ---
# 登录失败计数**必须带 tenant_code**：app_user 的唯一性是 UK(tenant_id, username)，
# 键里少了租户维度，攻击者在自己租户内对 admin 连续失败就能锁死其它所有租户的 admin，
# 且不需要任何越权即可成立（跨租户拒绝服务）。
KEY_LOGIN_FAIL = "auth:login_fail:{tenant_code}:{username}"  # 值=失败次数，TTL 15min
KEY_TOKEN_DENY = "auth:denied_jti:{jti}"  # noqa: S105 - 键模板非口令；TTL=令牌剩余有效期
KEY_CAP_CONSUMED = "cap:consumed:{jti}"  # Capability 消费标记，TTL=令牌 TTL+余量
KEY_META_CACHE = "meta:profile:{profile_id}"  # 元数据 Profile 只读缓存
KEY_GEN_LOCK = "gen:lock:{pipeline_id}"  # 同一 Pipeline 生成串行化


@lru_cache(maxsize=1)
def get_redis() -> Redis:
    """缓存库客户端（DB 0）。Celery 用各自的 DB。

    🔴 ``health_check_interval`` **必须给**，不是可选的调优项：

    连接池里的连接一旦被中间设备静默掐断（实测：本机→ECS 的 SSH 隧道重启后，
    双向都在，但对端 socket 已失效），redis-py 默认**不会**在做下一步之前检查它，
    于是复用死连接的调用直接抛 ``ConnectionError: Error 22 while writing to socket``。
    而 worker 进程复用一个事件循环、客户端又是 ``lru_cache`` 单例，
    这条死连接**永远不会被换掉**——症状是「隧道断过一次之后，worker 就再也不干活了，
    重启才好」，而日志里只有一句网络错误，看不出是连接池的问题。
    给了这个间隔后，闲置超过该秒数的连接会先 PING，失败即丢弃重建。

    再加一层**重试**：``health_check_interval`` 只兜「闲置超过 30 秒」的连接，
    而隧道刚断就到的请求，其连接可能只闲置了几秒——照样撞上死连接。
    重试让这一次调用自己换个连接完成，调用方不必感知。

    重试对 ``SETNX``（Capability 消费）是**失败即安全**的：万一第一次已经生效，
    重试会拿到「已被消费」，也就是拒掉一次合法消费，属于可用性损失而非安全漏洞；
    反过来（把已消费的当成没消费）在重试语义下不可能发生。

    ``pool_pre_ping`` 在 SQLAlchemy 那边承担同一职责（见 ``core/db.py``），
    两者是同一个坑的两个面，改一处时记得看另一处。
    """
    # 显式注解：``Redis.from_url`` 的返回类型是 Any（它按 URL scheme 分派），
    # 直接 return 会被 mypy 判成 no-any-return，也让调用方丢掉类型信息
    client: Redis = Redis.from_url(
        settings.redis_url(settings.REDIS_DB_CACHE),
        decode_responses=True,
        health_check_interval=30,
        retry=Retry(ExponentialWithJitterBackoff(base=0.05, cap=1.0), retries=3),
        retry_on_error=[RedisConnectionError, RedisTimeoutError],
    )
    return client


async def close_redis() -> None:
    client = get_redis()
    await client.aclose()
