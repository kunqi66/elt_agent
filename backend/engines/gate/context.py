"""门禁的输入上下文（be-11 §3.2）。

门禁是**纯同步函数**：``evaluate(ctx, now=...) -> GateResult``。它需要的全部外部事实
都在这里，尤其是两件最容易搞错的：

1. **PDP 判定结果由 service 先算好再传进来**（``policy_decision``）。
   be-11 的执行序要求 ``policy`` 第一个跑并在阻塞时短路，而 PDP 要读库（策略规则表）、
   还得是 async 的——如果门禁自己去 await 它，整个门禁就变成 async 且不再是纯函数，
   「同一份制品 + 同一份策略 → 同一份结论」这条可复现性也就没了。
   所以：**service 先算 PDP（async、读库），再把结论交给门禁（sync、纯）**。
   ``policy_scope`` 一起传，是因为光有结论码没法向用户解释「你为什么被判越界」。

2. **列信息来自 Profile，不是来自 Plan**（be-10 §6.1 的取舍）。
   Plan 只声明「字段对字段」的意图，字段**存不存在、什么类型**由探查档案说了算。
   于是 :class:`TableProfile` 是 ``metadata_profile.schema_snapshot`` 的纯数据投影——
   service 负责把 ORM 行映射成它，engines 不碰 ORM。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from engines.hocon.types import HoconParseResult
from engines.plan.schema import EtlPlanModel
from engines.policy.models import PolicyDecision, ResourceScope
from models.enums import Env, SensitiveLevel

# ---------------------------------------------------------------- 档案投影


class ProfileColumn(BaseModel):
    """一列的**权威**档案。``data_type`` 是归一化后的类型（见 ``engines/schema/types.py``）。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    data_type: str
    nullable: bool = True
    sensitive_level: SensitiveLevel = SensitiveLevel.PUBLIC
    #: 该列的样本是否已脱敏。探查未脱敏的列**不允许**进入映射的目标端明文位置，
    #: 由 ``check_mapping`` 的 ``mask_missing_for_sensitive`` 判（be-11 §6.3）。
    sample_masked: bool = True


class TableProfile(BaseModel):
    """一张表的档案快照，加它自己的指纹与有效期。

    ``profile_sha256`` 是**制品指纹的输入之一**（be-12 §6.2 第 5 项）：
    「建方案时源表长这样」是方案的一部分，源表结构变了而指纹不变，
    就等于允许一份对不上表的方案继续走到执行。
    ``expires_at`` 过期只做 **warn**：档案过期可能只是没重新探查，
    直接 block 会让「昨天建的方案今天不能冻结」，而 warn 会把它显示在门禁面板上。
    """

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    data_source_id: int
    data_source_type: str
    schema_name: str = Field(default="", alias="schema")
    table: str
    columns: tuple[ProfileColumn, ...] = ()
    profile_sha256: str = ""
    profile_version: int = 0
    expires_at: datetime | None = None
    #: 档案里的列数上限（探查时截断过就记下来）——被截断的档案不能当权威用
    truncated: bool = False

    def column(self, name: str) -> ProfileColumn | None:
        for col in self.columns:
            if col.name == name:
                return col
        return None

    @property
    def column_names(self) -> tuple[str, ...]:
        return tuple(col.name for col in self.columns)


# ---------------------------------------------------------------- 上下文


class GateContext(BaseModel):
    """一次门禁判决需要的全部输入。**只读**（``frozen=True``）。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    plan: EtlPlanModel
    hocon_content: str
    parse_result: HoconParseResult

    #: key = (data_source_id, schema, table)，小写归一由 service 完成——
    #: 大小写敏感是**各库自己的脾气**（MySQL 在 Linux 上区分表名、Doris 不区分），
    #: 归一是连接器知识，不该让门禁猜。门禁按「档案里怎么写的就怎么查」处理。
    source_tables: dict[tuple[int, str, str], TableProfile] = Field(default_factory=dict)
    target_table: TableProfile | None = None

    policy_decision: PolicyDecision | None = None
    policy_scope: ResourceScope | None = None

    env: Env = Env.DEV

    #: 本次作业触碰的**最高敏感级别**（源列分级与数据源级别中较高的那个）。
    #:
    #: 它是 PDP 的一份输入，而 PDP 的结论（`policy_decision`）在这里——Prepare（be-13）
    #: 要把「碰了多敏感的数据」写进准备单，若由它自己再算一遍，就出现了第二份
    #: 「最高级别」的实现：门禁按 A 判风险、准备单按 B 记录分级，两者不同时
    #: **没有任何报错**，只是审批人看到的级别和实际判定的级别不是一回事。
    #: 所以这个值随上下文一起传出来，而不是留给下游重算。
    level: SensitiveLevel = SensitiveLevel.PUBLIC

    #: 渲染时用的数据源类型表，``check_resource`` 判断「这个连接器是否已注册」用
    data_source_types: dict[int, str] = Field(default_factory=dict)

    #: 本次判定所依据的**源表档案指纹**（be-12 §6.2 指纹的第五部分）。
    #:
    #: 放在 ctx 而不是让 artifacts 另查一次库：档案是在 :func:`build_gate_context`
    #: 里查的，这里再查一遍不但多一次往返，更糟的是**两次查询可能落在不同版本的档案上**
    #: （期间有人重跑了探查）——那会让「门禁判过的档案」与「算进指纹的档案」不是同一份，
    #: 而指纹的全部意义就是钉死「审批时看到的是哪一份」。
    source_metadata_sha256: str = ""

    def find_source_table(self, data_source_id: int, schema_name: str, table: str) -> TableProfile | None:
        direct = self.source_tables.get((data_source_id, schema_name, table))
        if direct is not None:
            return direct
        # 档案里的 schema 可能为空（如 MySQL 的 database 与 schema 同义、探查时只填了一个），
        # 于是退化成按 (data_source_id, table) 找。**只放宽 schema，不放宽表名**：
        # 放宽表名会让「表名写错」变成「找到了别的表」——静默错误。
        for (ds_id, _schema, tbl), profile in self.source_tables.items():
            if ds_id == data_source_id and tbl == table:
                return profile
        return None

    def all_columns(self) -> dict[str, ProfileColumn]:
        """源端全部可引用列（跨表合并，**同名以第一个为准**并保留冲突信息）。

        ``check_mapping`` 需要判断 ``source_field`` 是否存在。多表场景下同名冲突是
        真实风险（两个源表都有 ``id``），所以这里不合并成一个静默覆盖的 dict，
        而是由 :meth:`ambiguous_source_fields` 单独报出来。
        """
        merged: dict[str, ProfileColumn] = {}
        for profile in self.source_tables.values():
            for col in profile.columns:
                merged.setdefault(col.name, col)
        return merged

    def ambiguous_source_fields(self) -> dict[str, int]:
        """出现在**多张源表**里的列名 → 出现次数。用于报「来源不明确」的警告。"""
        counts: dict[str, int] = {}
        for profile in self.source_tables.values():
            for col in profile.columns:
                counts[col.name] = counts.get(col.name, 0) + 1
        return {name: n for name, n in counts.items() if n > 1}

    def target_columns(self) -> dict[str, ProfileColumn]:
        """目标端已有列。目标表**可以不存在**（首次同步），所以空档案是合法输入。"""
        if self.target_table is None:
            return {}
        return {col.name: col for col in self.target_table.columns}


def context_facts(ctx: GateContext) -> dict[str, Any]:
    """给日志/证据用的**脱敏后**事实摘要（只有计数与名字，没有样值）。

    门禁结论要落账本，而账本是不可变证据。放整份 Plan 进去会让账本爆炸，
    放样值进去会违红线——所以只放「判了什么、依据多少列」。
    """
    return {
        "source_tables": len(ctx.source_tables),
        "target_table": None if ctx.target_table is None else ctx.target_table.table,
        "source_columns": len(ctx.all_columns()),
        "target_columns": len(ctx.target_columns()),
        "mappings": len(ctx.plan.field_mappings),
        "policy_version": None if ctx.policy_decision is None else ctx.policy_decision.policy_version,
        "env": str(ctx.env),
    }
