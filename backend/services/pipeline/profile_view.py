"""档案行 →「一张表长什么样」的**唯一**读法。

## 为什么要有这个模块

`metadata_profile.schema_snapshot` 是一坨嵌套 JSON，而它有两个消费者，要的形状不同：

* **门禁**（``gating._snapshot_tables``）：要按 ``(data_source_id, schema, table)``
  **打平**的列表——门禁按 Plan 里写的那个 (库,表) 反查列，还要判「样本没脱敏的敏感列」。
* **提示词**（``engines.plan.draft`` 的 ``DraftSourceTable``）：要**按表分组**的列——
  模型靠主键标记与行数量级决定去重口径与预算。

两边**各写一份 ``profile.schema_snapshot["tables"]`` 的解析**，就是同一份事实的两次
解释：哪天探查侧改了列节点的键名，改一处漏一处，而症状是「门禁说列存在、提示词里
那一列凭空消失」——模型于是猜一个列名，一路走到渲染才炸。所以：

**本模块负责把 JSON 读成下面这两个中立的结构，投影（打平 / 分组）由各自的消费者做。**
它只读事实，不做判断：认不出来的分级一律按 ``INTERNAL``（比 ``PUBLIC`` 保守），
没有列名的列直接丢掉（没有名字的列无法被任何 Plan 引用，留着只会让
「列存不存在」的判断变宽松）。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from models.enums import SensitiveLevel

__all__ = ["RawColumn", "RawTable", "tables_of"]

_BASE = ConfigDict(extra="forbid", frozen=True)


class RawColumn(BaseModel):
    """档案快照里的一列（键名与探查侧写入的节点一致）。"""

    model_config = _BASE

    name: str
    data_type: str
    #: 快照里缺这个键时的默认值取「可空 = True」：把不可空误判成可空，
    #: 后果是门禁少报一条 could-be-null；反过来则会给一条假的「会插空值」结论。
    nullable: bool = True
    is_primary_key: bool = False
    sensitive_level: SensitiveLevel = SensitiveLevel.INTERNAL
    #: 该列的样本在**入库前**是否已脱敏（快照里的 ``masked``）。
    masked: bool = True


class RawTable(BaseModel):
    model_config = _BASE

    schema_name: str = ""
    name: str
    comment: str = ""
    row_count_approx: int | None = None
    columns: tuple[RawColumn, ...] = ()


def _level_of(raw: object) -> SensitiveLevel:
    """快照里的分级 → 枚举。认不出来的一律 ``INTERNAL``。

    **不能**退回 ``PUBLIC``：``PUBLIC`` 等于「随便出域」，而认不出来说明
    「档案里的值我们不认识」——那种情况下保守才是对的。
    """
    try:
        return SensitiveLevel(str(raw))
    except ValueError:
        return SensitiveLevel.INTERNAL


def tables_of(profile: object | None) -> tuple[RawTable, ...]:
    """档案行（或 ``None``）→ 表列表。``None`` 回空元组，不回 ``None``：

    「没有档案」与「档案里没有表」对调用方的处置是一样的（都是「我们对源端一无所知」），
    而多一种返回形状就多一个忘判的分支。
    """
    snapshot = getattr(profile, "schema_snapshot", None) or {}
    out: list[RawTable] = []
    for raw in snapshot.get("tables", []):
        name = str(raw.get("name") or "")
        if not name:
            continue
        columns = tuple(
            RawColumn(
                name=str(col.get("name") or ""),
                data_type=str(col.get("data_type") or ""),
                nullable=bool(col.get("nullable", True)),
                is_primary_key=bool(col.get("is_primary_key", False)),
                sensitive_level=_level_of(col.get("sensitive_level")),
                masked=bool(col.get("masked", True)),
            )
            for col in raw.get("columns", [])
            if col.get("name")
        )
        row_count = raw.get("row_count_approx")
        out.append(
            RawTable(
                schema_name=str(raw.get("schema") or ""),
                name=name,
                comment=str(raw.get("comment") or ""),
                row_count_approx=int(row_count) if isinstance(row_count, (int, float)) else None,
                columns=columns,
            )
        )
    return tuple(out)
