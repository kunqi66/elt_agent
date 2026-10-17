"""Doris 目标端：``REPLACE WITH TABLE ... PROPERTIES("swap" = "true")``（be-18 §6.3）。

```sql
ALTER TABLE `db`.`<shadow>` REPLACE WITH TABLE `db`.`<formal>` PROPERTIES("swap" = "true")
```

``swap=true`` 是**交换**而不是替换：交换后原正式表的数据留在影子表名下，
于是「再交换一次」在理论上就是回退手段，而 §5.2 的受管回滚退化成「把影子表丢掉」。
那份留在影子名下的旧数据是这次发布唯一的退路，所以清理动作在本模块里是一等公民。

Doris 特有的硬性前提（§6.3）在这里补两条（列形态与同库已由 ``wire`` / ``base`` 管住）：

1. 引擎、键模型、分桶属性一致；
2. 影子表非空（``base.assert_publishable``）。

第 1 条靠解析 ``SHOW CREATE TABLE`` 拿到，取不到就**拒绝**而不是放行——
最坏结果是一次可以人工复核的假拒绝，而不是一次静默的结构变更。
**本机没有 Doris，这段解析未经真机验证**（见交付报告）：正则匹配的是 Doris 2.x
的 DDL 文本形态，若实际输出形态不同，表现是「预检报 table_properties_unverifiable」，
也就是失败在安全的一侧。副本数与分区属性尚未纳入比对（DDL 文本里没有稳定表达）。
"""

from __future__ import annotations

import re
from typing import Any

import aiomysql

from clients.target.base import TableInfo, TargetPrecheckError
from clients.target.engine import MySqlProtocolPublisher
from clients.target.wire import qualified

#: 交换语句的属性段。``swap=true`` 是冻结决策原文，不是可选参数。
_SWAP_PROPERTIES = 'PROPERTIES("swap" = "true")'

_RE_ENGINE = re.compile(r"ENGINE\s*=\s*(\w+)", re.IGNORECASE)
_RE_KEY_MODEL = re.compile(r"\b(UNIQUE|DUPLICATE|AGGREGATE)\s+KEY\b", re.IGNORECASE)
_RE_DISTRIBUTED = re.compile(
    r"DISTRIBUTED\s+BY\s+(.+?)(?:\s+PROPERTIES|\s*$|\s*;)", re.IGNORECASE | re.DOTALL
)


class DorisTargetPublisher(MySqlProtocolPublisher):
    """目标端是 Doris 时的发布器。无状态，每次动作自带一条连接。"""

    engine = "doris"

    def swap_sql(self, *, database: str, shadow: str, formal: str) -> str:
        return (
            f"ALTER TABLE {qualified(database, shadow)} "
            f"REPLACE WITH TABLE {qualified(database, formal)} {_SWAP_PROPERTIES}"
        )

    def error_table_sql(self, *, database: str, error_table: str) -> str:
        """错误表 DDL（Doris）。列与 ``error_record`` 同宽、同义。

        三条 Doris 特有的约束，写错一条的报错都很难读：

        1. **键列必须排在列清单最前面**，所以 ``DUPLICATE KEY`` 取的是前三个文本列。
           错误行本来就是「同一行可能命中多条规则」的多值事实，不需要去重语义，
           因此用 ``DUPLICATE KEY`` 而不是 ``UNIQUE KEY``（后者要求写前合并，
           而分流的行是追加写的）；
        2. **必须显式 ``DISTRIBUTED BY``**，且演示环境只有一台 BE，``BUCKETS 1``
           与 ``replication_num = 1`` 一起写死——默认副本数在单 BE 上会让建表直接失败；
        3. ``VARCHAR`` 必须带长度：它由 ``engines/quality/contract.py`` 的列宽常量决定，
           这里逐个写死并与之对齐（``rule_id``/``field_name`` 64、``error_code`` 32、
           ``payload_digest`` 64、``reason`` 255）。
        """
        target = qualified(database, error_table)
        columns = (
            "`run_id` BIGINT NOT NULL, "
            "`rule_id` VARCHAR(64) NOT NULL, "
            "`field_name` VARCHAR(64) NOT NULL, "
            "`error_code` VARCHAR(32) NOT NULL, "
            "`payload_digest` VARCHAR(64) NOT NULL, "
            "`reason` VARCHAR(255) NOT NULL, "
            "`occurred_at` DATETIME NOT NULL"
        )
        return (
            f"CREATE TABLE IF NOT EXISTS {target} ({columns}) ENGINE=OLAP "
            "DUPLICATE KEY(`run_id`, `rule_id`, `field_name`) "
            'DISTRIBUTED BY HASH(`run_id`) BUCKETS 1 PROPERTIES("replication_num" = "1")'
        )

    async def engine_precheck(
        self, conn: Any, *, database: str, shadow: TableInfo, formal: TableInfo
    ) -> dict[str, Any]:
        properties: dict[str, str] = {}
        for table in (shadow.name, formal.name):
            ddl = await self._show_create(conn, database=database, table=table)
            engine = _RE_ENGINE.search(ddl)
            distributed = _RE_DISTRIBUTED.search(ddl)
            if engine is None or distributed is None:
                raise TargetPrecheckError(
                    f"无法从 SHOW CREATE TABLE 解析 {table!r} 的引擎/分桶属性，拒绝交换",
                    details={"reason": "table_properties_unverifiable", "table": table},
                )
            key_model = _RE_KEY_MODEL.search(ddl)
            properties[table] = "|".join(
                (
                    f"engine={engine.group(1).upper()}",
                    f"key={key_model.group(1).upper() if key_model else 'NONE'}",
                    f"distributed={' '.join(distributed.group(1).split())}",
                )
            )
        values = list(properties.values())
        if len(set(values)) != 1:
            raise TargetPrecheckError(
                "两张表的引擎/键模型/分桶属性不一致，拒绝交换",
                details={"reason": "table_properties_mismatch", "properties": properties},
            )
        if not values[0].startswith("engine=OLAP"):
            # REPLACE WITH TABLE 只在 OLAP 表之间有定义；其他引擎上它的行为不是我们审批过的语义
            raise TargetPrecheckError(
                "目标表不是 OLAP 引擎，不支持 REPLACE WITH TABLE",
                details={"reason": "not_olap", "properties": properties},
            )
        return properties

    async def _show_create(self, conn: Any, *, database: str, table: str) -> str:
        """取表的建表语句。列名在 MySQL 里是 ``Create Table``、在 Doris 里是 ``Create Table``。"""
        sql = f"SHOW CREATE TABLE {qualified(database, table)}"
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(sql)
            row = await cur.fetchone()
        if row is None:
            raise TargetPrecheckError(
                f"SHOW CREATE TABLE 没有返回行：{table!r}", details={"reason": "table_missing"}
            )
        for key, value in row.items():
            normalized = str(key).lower().replace(" ", "_")
            if normalized in ("create_table", "table_create"):
                return str(value)
        raise TargetPrecheckError(
            f"SHOW CREATE TABLE 的结果里找不到 DDL 列：{table!r}",
            details={"reason": "show_create_unparsable", "columns": sorted(map(str, row))},
        )


__all__ = ["DorisTargetPublisher"]
