"""发布/清理的**共用算法**，只留交换语句与各引擎特有的预检给子类（be-18 §6.2）。

Doris 与 MySQL 的终态约定是**一致**的（交换后正式表名持有本次数据、影子表名下持有
原正式表数据，§6.3 / §6.4 都写明了这一点），所以对账、预检顺序、失败路径的处理
只该有一份实现。两份实现的好处是「看起来各管一摊」，代价是**某一边悄悄放宽**——
这个模块里最不能出错的一段恰好就是「这次 Swap 到底生效了没有」。

**与设计文件清单的一处差异**：设计只列了 ``doris.py`` / ``mysql.py``，
本实现多出 ``engine.py`` 把共用部分提出来。理由同上：§6.2 的对账表、
§6.4 的清理断言如果各写一遍，两边的严格度迟早会分叉，而分叉只会在真机上暴露。

失败路径的三条分级（决定调用方看到什么）：

| 情况 | 处置 | 调用方看到 |
|---|---|---|
| 预检不过（结构/空表/引擎属性） | 抛 ``TargetPrecheckError`` | ``not_published``（**没有执行任何 DDL**） |
| DDL 报错但连接还在 | **继续对账** | 对账说了算：可能 ``published``（超时但生效） |
| DDL 后连不上、读不到指纹 | 返回 ``unknown`` | ``state=unknown`` → 人工核对 |
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, ClassVar, Literal

from clients.target import wire
from clients.target.base import (
    CleanupResult,
    ProvisionResult,
    PublishPlan,
    PublishResult,
    RollbackPlan,
    RunRef,
    TableInfo,
    TargetCleanupError,
    TargetEndpointRef,
    TargetError,
    TargetPrecheckError,
    assert_columns_replacable,
    assert_publishable,
    reconcile_final_state,
)
from core.logging import get_logger

log = get_logger(__name__)

#: detail 里 DDL 报错文本的截断长度。驱动错误常把整条语句回显出来——
#: 语句里只有表名（不是凭据），但进日志仍要有界。
_DDL_ERROR_MAX = 300


class MySqlProtocolPublisher(ABC):
    """``clients/target/base.TargetPublisher`` 在 MySQL 线协议上的骨架实现。"""

    #: 引擎标识，进 detail 与日志（排查「这条命令是哪条路走的」）
    engine: ClassVar[str] = ""

    def __init__(self, endpoint: TargetEndpointRef) -> None:
        self._endpoint = endpoint

    @property
    def endpoint(self) -> TargetEndpointRef:
        """只读访问器。**不含口令的消费点**：``password`` 只在 ``wire.connect`` 里取出。"""
        return self._endpoint

    # ------------------------------------------------------------ 子类要给的

    @abstractmethod
    def swap_sql(self, *, database: str, shadow: str, formal: str) -> str:
        """交换语句。两个实现都必须是**单条原子语句**（§6.3 / §6.4）。"""

    @abstractmethod
    def error_table_sql(self, *, database: str, error_table: str) -> str:
        """错误表的 ``CREATE TABLE``（**两张引擎各写一份**，因为键模型/引擎属性是方言）。

        影子表不走这个方法：它靠 ``wire.create_like_sql`` 克隆正式表，
        只有错误表需要手写形状——它的列由 ``render_route_selects`` 的违规行 SELECT
        决定（``run_id / rule_id / field_name / error_code / payload_digest / reason /
        occurred_at``），与控制面的 ``error_record`` 同宽，是一份平台自有的契约。
        """

    def shadow_table_sql(self, *, database: str, shadow: str, formal: str) -> str:
        """影子表的 ``CREATE TABLE``。默认克隆正式表（``CREATE TABLE ... LIKE``）。"""
        return wire.create_like_sql(database=database, target=shadow, source=formal)

    async def engine_precheck(
        self, conn: Any, *, database: str, shadow: TableInfo, formal: TableInfo
    ) -> dict[str, Any]:
        """引擎特有的交换前预检。返回的内容进 ``PublishResult.detail``（审计用）。

        默认什么都不查。子类里任何**不确定**的判断都必须选择拒绝：
        这一层的存在意义是提前给出「为什么不允许交换」，而不是替代服务端的判定。
        """
        return {}

    # ------------------------------------------------------------ 读

    async def describe(self, table: str) -> TableInfo:
        async with wire.connect(self._endpoint) as conn:
            return await wire.describe(conn, database=self._endpoint.database, table=table)

    async def exists(self, table: str) -> bool:
        return (await self.describe(table)).exists

    # ------------------------------------------------------------ 备表

    async def provision(self, run: RunRef) -> ProvisionResult:
        """建影子表与错误表。**不碰正式表的任何数据**（只读它的结构当模板）。

        顺序与判据（每一条都朝「宁可拒绝」倒）：

        1. **正式表必须存在**——它是影子表的模板，也是 Swap 的另一半。不存在说明
           目标端还没由仓管建表，这时建出来的影子表跑完也发布不了；
        2. **影子表已有数据就拒绝**。影子表名按版本带序号（``__shadow_v<N>``），
           同一个版本重跑会落到同一个名字上；上一轮写了一半的行留在那里，
           续写会让质量报告说「有效行 N 条」而其中一半来自上一次运行
           ——报告、指纹、Swap 全都基于一份来源不明的数据；
        3. **错误表 ``IF NOT EXISTS``，永不清空**。它是本次运行唯一的诊断材料，
           且按 ``run_id`` 区分多次运行（``error_record`` 的控制面副本同理）。
        """
        database = self._endpoint.database
        async with wire.connect(self._endpoint) as conn:
            formal = await wire.describe(conn, database=database, table=run.formal_table)
            if not formal.exists:
                raise TargetPrecheckError(
                    f"正式表 {run.formal_table} 在目标库 {database} 中不存在，无法准备影子表",
                    details={"reason": "formal_missing", "table": run.formal_table},
                )

            shadow = await wire.describe(conn, database=database, table=run.shadow_table)
            if shadow.exists and shadow.row_count > 0:
                raise TargetPrecheckError(
                    f"影子表 {run.shadow_table} 中已有 {shadow.row_count} 行数据，拒绝复用",
                    details={
                        "reason": "shadow_not_empty",
                        "shadow_table": run.shadow_table,
                        "row_count": shadow.row_count,
                    },
                )
            shadow_state: Literal["created", "exists_empty"] = "exists_empty"
            if not shadow.exists:
                await wire.execute_ddl(
                    conn,
                    self.shadow_table_sql(
                        database=database, shadow=run.shadow_table, formal=run.formal_table
                    ),
                )
                shadow_state = "created"

            error_state: Literal["created", "exists"] = "exists"
            if not (await wire.describe(conn, database=database, table=run.error_table)).exists:
                await wire.execute_ddl(
                    conn,
                    self.error_table_sql(database=database, error_table=run.error_table),
                )
                error_state = "created"

            # 建完**立刻核对**影子表与正式表同形，而不是留到 Swap 那一步：
            # 结构不对的作业能跑完、能出报告，只在最后一步以「列形态不一致」失败——
            # 那时目标库里已经躺着一份没人看得懂的数据。
            post_shadow = await wire.describe(conn, database=database, table=run.shadow_table)
            try:
                assert_columns_replacable(post_shadow, formal)
                matches = True
            except TargetPrecheckError:
                log.error(
                    "target_shadow_shape_mismatch",
                    run_id=run.run_id,
                    engine=self.engine,
                    shadow_table=run.shadow_table,
                    formal_table=run.formal_table,
                )
                matches = False

        log.info(
            "target_tables_provisioned",
            run_id=run.run_id,
            engine=self.engine,
            shadow_table=run.shadow_table,
            formal_table=run.formal_table,
            error_table=run.error_table,
            shadow_state=shadow_state,
            error_state=error_state,
            shadow_matches_formal=matches,
        )
        return ProvisionResult(
            ok=matches,
            shadow_state=shadow_state,
            error_state=error_state,
            shadow_matches_formal=matches,
            detail={
                "engine": self.engine,
                "shadow_table": run.shadow_table,
                "formal_table": run.formal_table,
                "error_table": run.error_table,
                "formal_rows": formal.row_count,
                "shadow_columns": len(post_shadow.columns),
                "formal_columns": len(formal.columns),
            },
        )

    # ------------------------------------------------------------ 发布

    async def publish(self, run: RunRef, plan: PublishPlan) -> PublishResult:
        """交换影子表与正式表。``final_state`` **只由执行后对账得出**（§6.2）。"""
        if plan.shadow_table == plan.formal_table:
            # 在客户端就堵死：交换一张表与它自己，任何一个引擎的行为都不值得赌
            raise TargetPrecheckError(
                "影子表与正式表相同，拒绝发布",
                details={"reason": "shadow_equals_formal", "table": plan.formal_table},
            )

        database = self._endpoint.database
        async with wire.connect(self._endpoint) as conn:
            # ① 先看：预检不过就**在这里返回**，正式表连一条写命令都没见过
            pre_shadow = await wire.describe(conn, database=database, table=plan.shadow_table)
            pre_formal = await wire.describe(conn, database=database, table=plan.formal_table)
            assert_publishable(pre_shadow, pre_formal, allow_empty_shadow=plan.allow_empty_shadow)
            extra = await self.engine_precheck(conn, database=database, shadow=pre_shadow, formal=pre_formal)

            # ② 交换：单条原子语句。**返回值不参与判定**——只记下来备查
            sql = self.swap_sql(database=database, shadow=plan.shadow_table, formal=plan.formal_table)
            ddl_error, affected = await self._run_ddl(conn, sql)

            # ③ 后看：即使 ② 报错也照样看（「超时但已生效」是真实存在的）
            try:
                post_formal = await wire.describe(conn, database=database, table=plan.formal_table)
                post_shadow = await wire.describe(conn, database=database, table=plan.shadow_table)
            except TargetError as exc:
                # 读不到指纹就只能报 unknown。**不许**把「DDL 没报错」当成 published：
                # 那正是「在未知态上叠加第二次 Swap」的开端。
                log.warning(
                    "target_reconcile_unavailable",
                    run_id=run.run_id,
                    engine=self.engine,
                    shadow_table=plan.shadow_table,
                    formal_table=plan.formal_table,
                    error=type(exc).__name__,
                )
                return PublishResult(
                    ok=False,
                    final_state="unknown",
                    pre_fingerprint=pre_shadow.table_fingerprint,
                    detail={
                        "engine": self.engine,
                        "affected_rows": affected,
                        "ddl_error": ddl_error,
                        "reconcile_error": f"{type(exc).__name__}: {exc.message}"[:_DDL_ERROR_MAX],
                        "fingerprint_scope": pre_shadow.fingerprint_scope,
                    },
                )

        final_state = reconcile_final_state(
            pre_shadow=pre_shadow.table_fingerprint,
            pre_formal=pre_formal.table_fingerprint,
            post_formal=post_formal.table_fingerprint,
            post_shadow=post_shadow.table_fingerprint,
        )
        published = final_state == "published"
        if ddl_error:
            log.warning(
                "target_swap_ddl_error",
                run_id=run.run_id,
                engine=self.engine,
                final_state=final_state,
                ddl_error=ddl_error,
            )
        return PublishResult(
            ok=published,
            final_state=final_state,
            published_table=plan.formal_table if published else None,
            previous_data_table=plan.shadow_table if published else None,
            pre_fingerprint=pre_shadow.table_fingerprint,
            post_fingerprint=post_formal.table_fingerprint,
            detail={
                "engine": self.engine,
                "affected_rows": affected,
                "ddl_error": ddl_error,
                "fingerprint_scope": pre_shadow.fingerprint_scope,
                "pre_shadow_rows": pre_shadow.row_count,
                "pre_formal_rows": pre_formal.row_count,
                "post_formal_rows": post_formal.row_count,
                "post_shadow_rows": post_shadow.row_count,
                "post_shadow_fingerprint": post_shadow.table_fingerprint,
                "engine_precheck": extra,
            },
        )

    async def _run_ddl(self, conn: Any, sql: str) -> tuple[str | None, int]:
        """执行交换语句，返回 ``(错误摘要或 None, rowcount)``。

        **不把异常抛给调用方**是这里的关键：DDL 报错与「没生效」是两件事，
        而唯一能区分它们的方法是继续对账。抛出去就等于放弃了这次对账的机会，
        把一次「超时但已生效」记成失败——正式表已经换成新数据，库里却说没换。
        """
        try:
            return None, await wire.execute_ddl(conn, sql)
        except TargetError as exc:
            return f"{type(exc).__name__}: {exc.message}"[:_DDL_ERROR_MAX], 0

    # ------------------------------------------------------------ 清理

    async def rollback_cleanup(self, run: RunRef, plan: RollbackPlan) -> CleanupResult:
        """只删影子表，并**证明正式表没被动过**。

        正式表在这次动作里必须是常量：清理前后各取一次指纹，不等即说明**第三方在动
        正式表**——比「影子表没删掉」严重得多，因此用不同的 ``state`` 报出去，
        让告警能区分这两件事。

        幂等：影子表本来就不在 → ``ok=True`` + ``shadow_dropped=False``。
        这让「中途失败的清理」可以直接重试（§6.5 第三层）。
        """
        database = self._endpoint.database
        async with wire.connect(self._endpoint) as conn:
            pre_formal = await wire.describe(conn, database=database, table=plan.formal_table)
            pre_shadow = await wire.describe(conn, database=database, table=plan.shadow_table)

            if not pre_shadow.exists:
                return CleanupResult(
                    ok=True,
                    shadow_dropped=False,
                    formal_unchanged=True,
                    detail={
                        "engine": self.engine,
                        "state": "already_absent",
                        "shadow_table": plan.shadow_table,
                        "formal_fingerprint": pre_formal.table_fingerprint,
                    },
                )

            affected = await self._drop(conn, plan.shadow_table)
            post_formal = await wire.describe(conn, database=database, table=plan.formal_table)
            post_shadow = await wire.describe(conn, database=database, table=plan.shadow_table)

        formal_unchanged = pre_formal.table_fingerprint == post_formal.table_fingerprint
        shadow_dropped = not post_shadow.exists
        detail = {
            "engine": self.engine,
            "affected_rows": affected,
            "shadow_table": plan.shadow_table,
            "formal_table": plan.formal_table,
            "shadow_dropped": shadow_dropped,
            "formal_unchanged": formal_unchanged,
            "pre_formal_fingerprint": pre_formal.table_fingerprint,
            "post_formal_fingerprint": post_formal.table_fingerprint,
            "fingerprint_scope": pre_formal.fingerprint_scope,
            "keep_error_rows": plan.keep_error_rows,
        }
        if not formal_unchanged:
            # 正式表被改过：这不是「这次清理没做干净」，而是**没人授权过的写操作**。
            # 返回 ok=False 而不是抛异常：调用方（服务层）要把它记成一条独立的证据，
            # 异常会被当成技术故障，而这件事需要人立刻看。
            log.error(
                "target_formal_changed_during_cleanup",
                run_id=run.run_id,
                engine=self.engine,
                formal_table=plan.formal_table,
            )
            return CleanupResult(
                ok=False,
                shadow_dropped=shadow_dropped,
                formal_unchanged=False,
                detail={**detail, "state": "formal_changed"},
            )
        if not shadow_dropped:
            return CleanupResult(
                ok=False,
                shadow_dropped=False,
                formal_unchanged=True,
                detail={**detail, "state": "drop_incomplete"},
            )
        return CleanupResult(
            ok=True,
            shadow_dropped=True,
            formal_unchanged=True,
            detail={**detail, "state": "dropped"},
        )

    async def _drop(self, conn: Any, table: str) -> int:
        """``DROP TABLE``（不做 IF EXISTS：存不存在由调用方先看一眼，结论更准）。"""
        sql = f"DROP TABLE {wire.qualified(self._endpoint.database, table)}"
        try:
            return await wire.execute_ddl(conn, sql)
        except TargetError as exc:
            # 换码：清理失败的对外语义是 ROLLBACK_FAILED，而不是 SWAP_FAILED
            raise TargetCleanupError(
                f"影子表清理失败：{exc.message}",
                code=exc.code,
                details={"reason": "drop_failed", "shadow_table": table},
            ) from exc


__all__ = ["MySqlProtocolPublisher"]
