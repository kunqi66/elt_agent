"""目标端错误表的**只读端口**（be-17 §3.2）。

设计把实现放在 ``clients/target/errors_table.py``；协议本身放在这里，因为它是
**消费方定义的端口**：``services/quality.py`` 是唯一的调用者，它按自己的需要
（计数、分布、样本、拷贝）描述接口，``clients/`` 侧只要满足它即可。协议放在
``engines/quality/`` 还有一个好处——它是纯类型声明（只依赖 Pydantic 与
``typing.Protocol``），不会把 IO 依赖带进任何一层的 import 图。

三个刻意的取舍：

- ``count`` 的 ``run_id`` 可空：影子表没有 ``run_id`` 列（有效行不分运行），
  错误表有（**同一张错误表可被多次运行共用**——``run_id`` 是数据面注入的常量列）。
  传 ``None`` 表示「整表计数」，传值表示 ``WHERE run_id = ?``。写成一个函数而不是
  两个，是因为两处的错法一样：忘了带 ``run_id`` 会让两次运行的错误行互相污染，
  而报告上只是「数字大了一点」，看不出异常。
- ``count_matching`` 是为 ``warn`` 规则加的：warn 命中**不改变路由**，行照常进影子表，
  所以错误表里没有它的痕迹。唯一能数到它的地方就是影子表本身——
  在影子表上按 warn 谓词 ``COUNT(*)``。
- ``fetch_records`` 是为了把摘要**拷进控制面**（``error_record``）。只拷摘要不拷原文：
  ``ErrorSample`` 里没有「值」这个字段，端口层面就传不出业务数据。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Protocol

from pydantic import BaseModel, ConfigDict


class ErrorSample(BaseModel):
    """错误表 / 影子表里读到的一行**摘要**。

    ``extra="ignore"``：目标端的列可能比这里多（be-18 回滚时不动错误表，
    将来也可能加列）。多出来的列被丢掉是安全的——真正危险的方向是**少**了列，
    而那种情况会让 ``payload_digest`` 为空，报告与错误明细会一起露馅。
    """

    model_config = ConfigDict(extra="ignore")

    rule_id: str = ""
    field_name: str = ""
    error_code: str = ""
    #: 「SHA256 前 56 位 + 8 位长度后缀」，由数据面算出，控制面**原样拷贝**。
    payload_digest: str = ""
    #: 规则模板 + 字段名，不含值。
    reason: str = ""
    occurred_at: datetime | None = None


class ErrorTableReader(Protocol):
    """错误表只读端口。**无副作用**，因此不注册为 ``ToolName``、不经 ``ToolBroker``。

    实现必须满足：只读（``SELECT``），不建表、不改数据；租户校验在
    ``services/quality.py`` 完成（本端口只拿到已经确定属于本次运行的**表名**，
    表名来自 ``execution_run``，而那条 run 已经过租户过滤）。
    """

    async def count(self, table: str, run_id: int | None = None) -> int:
        """``COUNT(*)``。``run_id`` 为 ``None`` 时整表计（影子表），否则 ``WHERE run_id``。"""
        ...

    async def count_by_code(self, table: str, run_id: int) -> Mapping[str, int]:
        """按 ``error_code`` 分组计数 → ``error_code_dist``。"""
        ...

    async def count_by_rule(self, table: str, run_id: int) -> Mapping[str, int]:
        """按 ``rule_id`` 分组计数 → ``rule_hits``。"""
        ...

    async def count_matching(self, table: str, where_sql: str) -> int:
        """在 ``table`` 上按给定谓词计数（``warn`` 规则命中数）。

        ``where_sql`` 由本模块的 ``render_predicate`` 产出，只含列名与规则模板，
        **不含任何行值**——实现方不要把它当成用户输入去二次转义。
        """
        ...

    async def fetch_digest_samples(
        self, table: str, run_id: int, rule_id: str, limit: int = 3
    ) -> Sequence[ErrorSample]:
        """取某条规则的若干条**摘要样本**（最多 ``limit`` 条），用于 ``diagnostics``。"""
        ...

    async def fetch_records(self, table: str, run_id: int, limit: int) -> Sequence[ErrorSample]:
        """取若干条摘要用于**拷贝进控制面** ``error_record``。

        ``limit`` 是必需的：错误表可能有百万行，而控制面副本的用途是统计与追溯，
        不是明细（明细在目标端）。超出的部分由 ``diagnostics.warnings`` 记
        ``error_records_truncated``，让「控制面副本不全」这件事有据可查。
        """
        ...
