"""Capability 单次消费（CLAUDE.md §5.6）。

**必须原子**。「先查后写」在并发下会双放行：两个 worker 同时 SELECT 都看不到已消费标记，
于是都认为自己是首次消费，结果 SeaTunnel 被提交两次——**两次真实的数据搬运**。
这是本系统里唯一一个「出错就产生业务损失而非报错」的地方，所以宁可 fail-closed。

双保险：

- **Redis ``SETNX``**：快，挡住绝大多数重复投递（Celery ``task_acks_late`` 下重投是常态）。
- **PG 条件 UPDATE**：慢，但是事实来源。Redis 键可能因 TTL 过期、主从切换、误删而丢失，
  此时唯一能兜住的就是数据库那一行的 ``consumed_at IS NULL`` 条件。

🔴 **为什么不照 be-05 §6.4 写成「INSERT 新行 + 捕获唯一约束冲突」**：
``capability`` 行在**签发时**就已插入（be-05 §5.1 步骤 4，只落 ``token_sha256`` 不落原文）。
消费时再 INSERT 同一个 ``jti`` 必然命中 ``uq_capability_jti``，
于是**每一次正常消费都会被判成重放**，系统根本跑不起来。§6.4 与 §5.1 是自相矛盾的，
以 §5.1 为准：表的语义是「一行 = 一张签发过的令牌」，消费就是把这行标记为已用。

原子性由**单条 UPDATE 的 WHERE 条件**保证，不是靠应用层的判断：

    UPDATE capability SET consumed_at=... WHERE jti=... AND consumed_at IS NULL

并发下 PG 会对该行加锁，第二个事务的 UPDATE 影响 0 行 → 判 ``REPLAYED``。
这与 Redis SETNX 语义等价，且没有检查-写入窗口。

Redis 不可用时**直接拒绝执行**（fail-closed），不退化成「只靠 PG」：
那会把「无窗口」退化成事实上的「先查后写」，与 PRD §1.4 的
「未授权/重放成功率 = 0」直接冲突。代价是 Redis 抖动期间执行排队，可以接受。
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from redis.exceptions import RedisError
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from core.errors import AppError, CapabilityReplayedError, ErrorCode
from core.redis import KEY_CAP_CONSUMED, Redis
from harness.capability import CapabilityClaims
from models.governance import Capability

# 消费标记的保留时长。比 Capability 本身多活 60 s：令牌刚过期那几秒里
# 一次延迟到达的重投仍应被判定为重放，而不是因为 Redis 键先过期而放行。
_TTL_SLACK_SEC = 60


class ConsumptionResult(StrEnum):
    CONSUMED = "consumed"
    REPLAYED = "replayed"


def consumed_key(jti: object) -> str:
    """键名取自 ``core.redis`` 的键空间规范，**不在这里另写一份**。

    同一个语义有两份键模板时，改一处忘一处的结果是：消费标记写进了 A 键、
    重放检查去读 B 键——重放防护静默失效，而且任何测试都不会红。
    """
    return KEY_CAP_CONSUMED.format(jti=jti)


class ReplayGuard:
    def __init__(self, *, redis: Redis, ttl_sec: int) -> None:
        self._redis = redis
        self._ttl = ttl_sec + _TTL_SLACK_SEC

    async def consume(
        self, claims: CapabilityClaims, session: AsyncSession, *, run_id: int | None = None
    ) -> ConsumptionResult:
        """占位 + 标记已消费。**不 commit**——事务边界归调用方，
        这样「消费标记 + 证据 + Outbox」能在同一个事务里落地。"""
        key = consumed_key(claims.jti)

        try:
            ok = await self._redis.set(key, "1", nx=True, ex=self._ttl)
        except RedisError as exc:
            # fail-closed：判不出「用过没有」时，唯一安全的选择是不执行
            raise AppError(
                "重放防护不可用（Redis 不可达），已拒绝执行",
                code=ErrorCode.INTERNAL_ERROR,
                http_status=500,
            ) from exc

        if not ok:
            return ConsumptionResult.REPLAYED

        now = datetime.now(UTC)
        result = await session.execute(
            update(Capability)
            .where(Capability.jti == claims.jti, Capability.consumed_at.is_(None))
            .values(consumed_at=now, consumed_by_run_id=run_id)
        )
        if result.rowcount == 0:  # type: ignore[attr-defined]
            # 三种可能，处理方式相同：该行已被消费 / 从未签发 / jti 被篡改成不存在的值。
            # **Redis 键不回滚**：留一个短时「毒键」比放行安全，
            # 大不了让调用方重新签发 Capability。
            return ConsumptionResult.REPLAYED

        return ConsumptionResult.CONSUMED

    @staticmethod
    def assert_consumed(result: ConsumptionResult) -> None:
        """把 ``REPLAYED`` 转成协议异常（账本与 ``reject_reason`` 由 broker 写）。"""
        if result is ConsumptionResult.REPLAYED:
            raise CapabilityReplayedError()
