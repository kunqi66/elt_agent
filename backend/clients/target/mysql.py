"""MySQL 目标端：单条 ``RENAME TABLE`` 三步交换（be-18 §6.4）。

```sql
RENAME TABLE `db`.`<formal>` TO `db`.`<tmp>`,
             `db`.`<shadow>` TO `db`.`<formal>`,
             `db`.`<tmp>`    TO `db`.`<shadow>`
```

单条语句内的多个 rename 由 MySQL 8 **原子执行**，不存在「改了一半」的中间态；
交换后的终态与 Doris 的 ``swap=true`` 一致（正式表名持有本次数据、影子表名持有
原正式表数据），因此对账逻辑与 Doris 共用（``engine.py``）。

MySQL 特有的三条预检（§6.4 + 加严）：

1. **视图依赖**：视图定义里出现正式表名 → 拒绝。``RENAME`` 不会更新视图里的引用，
   放行等于把「发布」变成「发布 + 一串视图静默失效」。
   判断方式是文本匹配，会有假阳性（同名不同库的引用），方向是安全的：拒绝。
2. **中间名必须空闲**：``<tmp>`` 若已存在（上一次崩在半路的交换留下的），
   ``RENAME`` 会失败；这里提前拒绝并指出这个残留名，让人先去处理它。
3. **外键引用**：MySQL 下被外键约束引用的表不能改名（``ERROR 1217``）。
   这里不逐条探测，改为在 DDL 失败后走对账——那是一次可以自证的失败，
   而探测外键要读 ``information_schema`` 的多个表、还会漏掉存储过程里的引用。
"""

from __future__ import annotations

import re
from typing import Any

from clients.target.base import TableInfo, TargetPrecheckError
from clients.target.engine import MySqlProtocolPublisher
from clients.target.wire import qualified, rename_statement, view_definitions

#: 中间名后缀。与影子表命名规范（``__shadow_v<N>``）无关，长度也要留够：
#: 正式表最多 64 字符，截断后拼上它仍在 64 以内。
_TEMP_SUFFIX = "__swap_tmp"


class MySQLTargetPublisher(MySqlProtocolPublisher):
    """目标端是 MySQL 8 时的发布器。"""

    engine = "mysql"

    def swap_sql(self, *, database: str, shadow: str, formal: str) -> str:
        return rename_statement(database=database, formal=formal, shadow=shadow, temp_suffix=_TEMP_SUFFIX)

    def error_table_sql(self, *, database: str, error_table: str) -> str:
        """错误表 DDL（MySQL）。列与 ``error_record`` 同宽、同义。

        ``run_id + rule_id`` 上建索引：控制面的每一处读取都是
        ``WHERE run_id = ?``（计数、分组、取样），而这张表被多次运行共用，
        没有索引时它会随运行次数线性变慢，且症状只是「报告出得越来越慢」。
        """
        target = qualified(database, error_table)
        columns = (
            "`run_id` BIGINT NOT NULL, "
            "`rule_id` VARCHAR(64) NOT NULL, "
            "`field_name` VARCHAR(64) NOT NULL, "
            "`error_code` VARCHAR(32) NOT NULL, "
            "`payload_digest` VARCHAR(64) NOT NULL, "
            "`reason` VARCHAR(255) NOT NULL, "
            "`occurred_at` DATETIME NOT NULL, "
            "KEY `ix_error_run_rule` (`run_id`, `rule_id`)"
        )
        return f"CREATE TABLE IF NOT EXISTS {target} ({columns}) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"

    async def engine_precheck(
        self, conn: Any, *, database: str, shadow: TableInfo, formal: TableInfo
    ) -> dict[str, Any]:
        views = await view_definitions(conn, database)
        formal_bare = formal.name.split(".")[-1].strip("`")
        # 反引号剥离后按**词边界**匹配：视图定义里的表名是 `db`.`table` 形态，
        # 直接子串匹配会让正式表 `sales` 命中视图里的 `sales_wide`（假阳性，
        # 方向安全但会让人白跑一趟），这里把 `_` 也算进词内字符来排除它。
        pattern = re.compile(rf"(?<![\w]){re.escape(formal_bare)}(?![\w])", re.IGNORECASE)
        hits = [name for name, definition in views.items() if pattern.search(definition.replace("`", ""))]
        if hits:
            raise TargetPrecheckError(
                f"正式表 {formal.name!r} 被视图引用（{'、'.join(sorted(hits))}），拒绝交换："
                "RENAME 不会更新视图里的表名引用",
                details={"reason": "view_dependency", "views": sorted(hits)},
            )
        temp = f"{formal_bare[:40]}{_TEMP_SUFFIX}"
        temp_exists = await self._exists(conn, temp)
        if temp_exists:
            raise TargetPrecheckError(
                f"交换用的中间表名 {temp!r} 已被占用（上一次交换的残留），拒绝执行",
                details={"reason": "temp_name_taken", "temp_table": temp},
            )
        return {"views_scanned": len(views), "temp_table": temp}

    async def _exists(self, conn: Any, table: str) -> bool:
        import aiomysql

        sql = (
            "SELECT table_name AS table_name FROM information_schema.tables "
            "WHERE table_schema = %s AND table_name = %s"
        )
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(sql, (self.endpoint.database, table))
            return await cur.fetchone() is not None


__all__ = ["MySQLTargetPublisher"]
