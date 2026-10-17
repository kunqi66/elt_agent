"""EtlPlan 确定性起草器 —— **纯规则兜底，不是 AI**。

存在的理由：整条链路（起草 → 渲染 HOCON → 门禁 → 冻结）在没有 LLM API key 的机器上
也必须能真跑通。演示、CI、以及「DeepSeek 挂了但我还得给人看方案」这三种场景
都需要一个**不依赖网络、同输入同输出**的起草器。

所以这里只有规则，没有模型，也没有任何「猜」：

1. **源表选择**：需求文本里出现哪张候选表的名字（大小写不敏感）就用它，否则用第一张。
2. **目标表名**：需求里 ``到`` / ``->`` / ``→`` / ``to`` 之后的标识符优先；
   其次用调用方给的既有目标表；再否则用 ``{源表名}_wide``（宽表）。
3. **字段映射**：源列逐列映射到**同名**目标字段；目标类型与源类型不同则
   ``transform="cast"`` 并生成一条 ``cast`` 变换（``params`` 带 ``target_type``）。
   ``confidential`` / ``restricted`` 的列**一律**生成 ``mask_middle`` 掩码并把
   ``transform`` 置为 ``mask``——这是安全默认值，不因为「画像更好看」而跳过。
4. **质量规则**：第一个非空列一条 ``not_null``（``E_NULL_PK``）；
   每个映射到目标端的敏感列一条 ``E_SENSITIVE_PLAINTEXT``（``on_violation="reject"``）。
5. **DAG**：``source`` → （有 filter 则 ``filter``）→ （有 transform 则 ``transform``）
   → ``quality`` → ``sink``，节点 id 稳定，``label`` 中文可读，**不给 position**
   （前端按最长路径自行分层）。
6. **预算**由调用方传入，本文件不造默认值——预算默认值是策略（be-07 的 ``budget_defaults``）
   的事，兜底起草器无权替用户定上限。
7. **不使用时间 / 随机数 / 环境变量**：同样输入必须逐字节同样输出
   （回归用例用 :func:`engines.plan.schema.dump_plan` 比较两次结果）。

这里刻意**不**输入任何样本值：样本已经在 be-04 脱敏过，但起草一个字段映射根本不需要看值，
少一份进来的数据就少一处可能被写进日志或提示词的地方。
"""

from __future__ import annotations

import itertools
import re

from pydantic import BaseModel, ConfigDict, Field

from engines.plan.schema import (
    DagEdge,
    DagNode,
    EtlFilter,
    EtlPlanDag,
    EtlPlanModel,
    EtlTransform,
    FieldMapping,
    FieldMasking,
    FieldTransform,
    PlanInput,
    PlanOutput,
    QualityRule,
    RuntimeBudget,
)
from models.enums import ErrorCodeLabel, SensitiveLevel

_BASE = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

#: 稳定 id。写成常量而不是各处拼字符串：DAG 节点 id 是前端高亮的定位键，
#: 也是 finding 的 locator，改一处漏一处的代价是「点了 finding 跳不到节点」。
INPUT_ID = "in_main"
OUTPUT_ID = "out_main"
NODE_SOURCE = "n_source"
NODE_FILTER = "n_filter"
NODE_TRANSFORM = "n_transform"
NODE_QUALITY = "n_quality"
NODE_SINK = "n_sink"

#: 需要强制脱敏的敏感级别（CLAUDE.md §5.7 的口径：public/internal 之外都要脱）。
_PLAN_MASK_LEVELS = frozenset({SensitiveLevel.CONFIDENTIAL, SensitiveLevel.RESTRICTED})

#: 目标表名的触发词。「to」两侧必须是非标识符字符：中文里
#: 「同步to订单表」的 ``to`` 前后都是汉字，Python 的 ``\b`` 在 Unicode 模式下
#: 会把汉字当词字符，于是 ``\bto\b`` **匹配不上**——这正是要绕开的坑。
_TARGET_NAME_RE = re.compile(
    r"(?:到|->|→|(?<![A-Za-z0-9_])to(?![A-Za-z0-9_]))\s*"
    r"([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)",
    re.IGNORECASE,
)

#: 文件类数据源的 ``PlanInput.format`` 取值。用 ``DataSourceType`` 的字符串值比较，
#: 不 import 枚举：本文件是纯函数层，不该为了三个字符串常量拉进一个领域模块。
_FILE_FORMATS: dict[str, str] = {"csv": "csv", "json": "json", "parquet": "parquet"}


# ---------------------------------------------------------------- 输入数据（纯数据，不碰 ORM）


class DraftColumn(BaseModel):
    """一列的画像。字段名与 ``metadata_profile.schema_snapshot`` 的列节点一致。"""

    model_config = _BASE

    name: str = Field(min_length=1, max_length=128)
    data_type: str = Field(min_length=1, max_length=64)
    nullable: bool = True
    is_primary_key: bool = False
    #: 由 be-04 的探判定级而来；``confidential``/``restricted`` 会被强制掩码
    sensitive_level: SensitiveLevel = SensitiveLevel.INTERNAL


class DraftTable(BaseModel):
    model_config = _BASE

    name: str = Field(min_length=1, max_length=128)
    columns: list[DraftColumn] = Field(default_factory=list)
    row_count_approx: int | None = Field(default=None, ge=0)


class DraftSourceTable(BaseModel):
    """源端候选表集合。

    ``tables`` 是**列表**（而不是单张表）是为了让「需求里提到哪张就用哪张」
    这条规则有东西可挑：只传一张表时它退化成「就是它」，逻辑仍然自洽。
    顺序即优先级，第一张是兜底。
    """

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True, str_strip_whitespace=True)

    data_source_id: int = Field(ge=1)
    #: 外键名是 ``schema``（与前端/PlanInput 一致）；``schema`` 是 Pydantic 保留名
    schema_name: str = Field(default="", alias="schema", max_length=128)
    tables: list[DraftTable] = Field(min_length=1)


class DraftTargetTable(BaseModel):
    """目标端。``columns`` 可以为空——空表示「按源列形状建宽表」。"""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True, str_strip_whitespace=True)

    data_source_id: int = Field(ge=1)
    database: str = Field(default="", max_length=128)
    #: 调用方指定的既有目标表名；需求文本里显式提到的名字优先于它
    table: str = Field(default="", max_length=128)
    columns: list[DraftColumn] = Field(default_factory=list)


# ---------------------------------------------------------------- 规则实现


def _pick_source_table(requirement: str, source: DraftSourceTable) -> DraftTable:
    """规则 1：需求里点名了哪张候选表就用哪张，否则第一张。"""
    lowered = requirement.lower()
    for table in source.tables:
        if table.name.lower() in lowered:
            return table
    return source.tables[0]


def _pick_target_name(
    requirement: str, source_table: DraftTable, target: DraftTargetTable | None
) -> tuple[str, str]:
    """规则 2：返回 ``(database, table)``。

    优先级：需求里显式提到 → 调用方给的既有目标表 → ``{源表名}_wide``。
    """
    match = _TARGET_NAME_RE.search(requirement)
    if match:
        database, _, table = match.group(1).rpartition(".")
        return database, table
    if target is not None and target.table:
        return target.database, target.table
    return (target.database if target is not None else ""), f"{source_table.name}_wide"


def _target_type_of(name: str, target_columns: list[DraftColumn]) -> str | None:
    """按列名（大小写不敏感）在目标表里找同名列的类型。找不到返回 ``None``。"""
    lowered = name.lower()
    for column in target_columns:
        if column.name.lower() == lowered:
            return column.data_type
    return None


_MASK_RULE_PATTERN = r"^[a-z_]+:[0-9a-f]{12}$"


def draft_plan(
    *,
    requirement: str,
    source: DraftSourceTable,
    target: DraftTargetTable | None,
    budget: RuntimeBudget,
    data_source_types: dict[int, str],
) -> EtlPlanModel:
    """按上面 7 条规则起草一份 EtlPlan（**确定性**：同输入逐字节同输出）。

    ``data_source_types`` 是 ``{data_source_id: 类型名}``。它只被用来决定源端
    ``PlanInput.kind``：文件类源（csv/json/parquet）标成 ``kind="file"`` 并带上
    ``format``，其余一律 ``kind="table"``。

    **路径不编造**：文件源的 ``path`` 缺失是要问用户的问题（be-09 的澄清节点），
    不是兜底起草器该猜的东西——猜一个路径出来只会让门禁和用户一起困惑。
    """
    source_table = _pick_source_table(requirement, source)
    database, output_table = _pick_target_name(requirement, source_table, target)
    target_columns = list(target.columns) if target is not None else []

    source_type = data_source_types.get(source.data_source_id, "")
    file_format = _FILE_FORMATS.get(source_type)

    plan_input = PlanInput(
        id=INPUT_ID,
        data_source_id=source.data_source_id,
        # 这里用别名 `schema` 而不是字段名 `schema_name`：mypy 的 dataclass_transform
        # 只认 `Field(alias=...)`，对 `populate_by_name` 一无所知，写字段名会报
        # "Unexpected keyword argument"。两者运行时等价（schema.py 的 _BASE 开了
        # populate_by_name），所以挑让严格类型检查也过得去的那一个。
        schema=source.schema_name,
        table="" if file_format else source_table.name,
        kind="file" if file_format else "table",
        path=None,
        format=file_format,  # type: ignore[arg-type]
    )

    # ---- 规则 3：字段映射 ----
    mappings: list[FieldMapping] = []
    transforms: list[EtlTransform] = []
    sensitive_fields: list[str] = []

    for column in source_table.columns:
        target_type = _target_type_of(column.name, target_columns) or column.data_type
        is_sensitive = column.sensitive_level in _PLAN_MASK_LEVELS

        if is_sensitive:
            # 敏感列：掩码优先。类型不一致时**不再**叠一条 cast——
            # FieldMapping.transform 只有一个槽位，同时表达两种变换会让渲染器
            # 必须自己决定谁先谁后；掩码在前、类型转换由掩码算子自身承担，语义唯一。
            transform: FieldTransform = "mask"
            masking: FieldMasking | None = FieldMasking(method="mask_middle")
            sensitive_fields.append(column.name)
        elif target_type != column.data_type:
            transform = "cast"
            masking = None
            transforms.append(
                EtlTransform(
                    id=f"t_cast_{column.name}",
                    kind="cast",
                    input_fields=[column.name],
                    output_field=column.name,
                    params={"target_type": target_type},
                )
            )
        else:
            transform = "none"
            masking = None

        mappings.append(
            FieldMapping(
                source_field=column.name,
                source_type=column.data_type,
                target_field=column.name,
                target_type=target_type,
                transform=transform,
                masking=masking,
                change="added",
            )
        )

    # ---- 规则 4：质量规则 ----
    rules: list[QualityRule] = []
    by_field: dict[str, list[str]] = {}

    first_not_null = next((c for c in source_table.columns if not c.nullable), None)
    if first_not_null is not None:
        rule_id = f"q_not_null_{first_not_null.name}"
        rules.append(
            QualityRule(
                id=rule_id,
                field=first_not_null.name,
                rule_type="not_null",
                params={},
                error_code=ErrorCodeLabel.E_NULL_PK,
                on_violation="reject",
            )
        )
        by_field.setdefault(first_not_null.name, []).append(rule_id)

    for field_name in sensitive_fields:
        rule_id = f"q_sensitive_{field_name}"
        rules.append(
            QualityRule(
                id=rule_id,
                field=field_name,
                rule_type="regex",
                # 掩码产物的形状是 ``kind:12位十六进制摘要``（engines/masking 的脱敏口径）。
                # 规则断言「必须长得像掩码」——不像就是明文漏出去了，直接拒行。
                # 用正则类型而不是新增一个 rule_type：QualityRuleType 的取值域是前端契约，
                # 加值等于改前端。
                params={"pattern": _MASK_RULE_PATTERN, "expect": "match"},
                error_code=ErrorCodeLabel.E_SENSITIVE_PLAINTEXT,
                on_violation="reject",
            )
        )
        by_field.setdefault(field_name, []).append(rule_id)

    if by_field:
        mappings = [
            m.model_copy(update={"quality_rule_ids": by_field.get(m.target_field, [])}) for m in mappings
        ]

    # ---- 规则 5：DAG ----
    filters: list[EtlFilter] = []  # 兜底起草器不发明过滤条件：它是业务语义，不是默认值
    dag = _build_dag(
        source_label=f"读取源表 {source_table.name}",
        output_label=f"写入目标表 {output_table}",
        source_type=source_type,
        has_filter=bool(filters),
        transform_count=len(transforms),
        mapping_count=len(mappings),
        rule_count=len(rules),
    )

    return EtlPlanModel(
        inputs=[plan_input],
        outputs=[
            PlanOutput(
                id=OUTPUT_ID,
                data_source_id=target.data_source_id if target is not None else source.data_source_id,
                database=database,
                table=output_table,
            )
        ],
        field_mappings=mappings,
        transforms=transforms,
        filters=filters,
        quality_rules=rules,
        dag=dag,
        budget=budget,
    )


def _build_dag(
    *,
    source_label: str,
    output_label: str,
    source_type: str,
    has_filter: bool,
    transform_count: int,
    mapping_count: int,
    rule_count: int,
) -> EtlPlanDag:
    """规则 5：节点与边。``position`` 一律不给（前端按最长路径分层）。"""
    nodes: list[DagNode] = [
        DagNode(
            id=NODE_SOURCE,
            type="source",
            label=source_label,
            ref=INPUT_ID,
            meta={"source_type": source_type} if source_type else {},
        )
    ]
    chain: list[str] = [NODE_SOURCE]

    if has_filter:
        nodes.append(DagNode(id=NODE_FILTER, type="filter", label="过滤无效行", ref=""))
        chain.append(NODE_FILTER)
    if transform_count:
        nodes.append(
            DagNode(
                id=NODE_TRANSFORM,
                type="transform",
                label=f"字段变换（{transform_count} 项）",
                ref="",
                meta={"transform_count": transform_count},
            )
        )
        chain.append(NODE_TRANSFORM)

    nodes.append(
        DagNode(
            id=NODE_QUALITY,
            type="quality",
            label=f"质量校验（{rule_count} 条规则）",
            ref="",
            meta={"rule_count": rule_count},
        )
    )
    chain.append(NODE_QUALITY)
    nodes.append(
        DagNode(
            id=NODE_SINK,
            type="sink",
            label=output_label,
            ref=OUTPUT_ID,
            meta={"mapping_count": mapping_count},
        )
    )
    chain.append(NODE_SINK)

    edges = [
        DagEdge(id=f"e_{left}_{right}", source=left, target=right)
        for left, right in itertools.pairwise(chain)
    ]
    return EtlPlanDag(nodes=nodes, edges=edges)


__all__ = [
    "DraftColumn",
    "DraftSourceTable",
    "DraftTable",
    "DraftTargetTable",
    "draft_plan",
]
