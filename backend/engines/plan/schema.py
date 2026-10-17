"""EtlPlan DSL —— 生成链路的中间契约（be-10 §6.1）。

**这是全项目 Plan 的唯一契约来源**：LLM 的结构化输出、HOCON 渲染器、门禁、指纹、
前端的 Vue Flow 拓扑图，全部消费这一个模型。

## 与 be-10 §6.1 的差异（以冻结的前端为准）

仓库里已有一条先例：``api/datasources.py`` 的路径与 DTO 以 ``frontend/src/api/*.ts``
为准，与设计文档的差异写在 ``schemas`` 的模块 docstring 里。本文件沿用该口径——
``frontend/src/types/pipeline.ts`` 的 ``EtlPlanBody`` 是**已经冻结、且已经在 16 个页面
里渲染**的契约，改它等于改前端；因此 DSL 按前端形状定，差异逐条登记如下：

| be-10 §6.1 的写法 | 本文件的写法 |
|---|---|
| `PlanInput.columns` | **不设** |
| `PlanOutput.write_mode`、`primary_keys` | `mode`（只认 `shadow_then_swap`） |
| `FieldMapping.source_input_id`、`transform_ids` | **不设** |
| `DagNode.kind`、`depends_on` | `DagNode.type`、`meta` |
| `DagEdge.from_node/to_node` | `source`/`target`(+`id`/`label`) |
| `PlanBudget`（5 项） | `RuntimeBudget`（6 项，含 `warn_threshold_pct`） |

上表逐行原因：

1. 列的权威来源是 `metadata_profile.schema_snapshot`（be-04 探查产物）。
   Plan 里再存一份必然与 Profile 漂移，而 be-11 §6.2 的存在性与类型检查本来就以 Profile 为准。
2. 影子表 + 原子 Swap 是本项目固定的交付方式，前端也只认这一个取值；
   `upsert` 语义另经质量契约表达。
3. 前端按「字段对字段」渲染映射表。派生列由 `transforms` 列表 + `EtlTransform.output_field`
   表达，两者靠列名关联。
4. 前端 `DagNodeType` 用 `type`；节点间的依赖已由 `edges` 显式表达，不需要再来一个 `depends_on`。
5. 前端 Vue Flow 的字段名。
6. 与 `runtime_supervision_contract` 的列一一对应，中间少一层翻译。

因此 be-10 §6.2 的三个校验子码在本 DSL 里**不可表达**，去向已登记：
`mapping_source_missing` / `mapping_target_missing` → be-11 的 `check_schema`
（对着 Profile 判，本就比对着 Plan 自判更准）；`missing_primary_key` → 无 `upsert` 写模式，
由质量契约的 `E_DUPLICATE_KEY` 规则承担。

## 确定性纪律（be-10 §6.7）

`frozen=True` + `extra="forbid"`：Plan 一旦构造就不可变，字段名写错会当场报错而不是被静默忽略。
落库与算指纹一律走 :func:`dump_plan`，它固定 `by_alias=True`（`schema` 别名的唯一出口）——
两处各写一次序列化，就是指纹系统的第一次分叉。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from models.enums import ErrorCodeLabel

# ---------------------------------------------------------------- 取值域

SchemaVersion = Literal["1.0"]

#: 源端形态。``table`` 走表名 + 库名；``query`` 直接给受限 SELECT；``file`` 走文件源。
InputKind = Literal["table", "query", "file"]
FileFormat = Literal["csv", "json", "parquet"]

#: 写模式。前端契约里是唯一取值：先写影子表，质量合格后原子 Swap。
OutputMode = Literal["shadow_then_swap"]

#: 单字段变换（前端 ``FieldTransform``）。``none`` 表示原样搬运。
FieldTransform = Literal["none", "cast", "trim", "concat", "mask", "hash"]

#: 该字段相对上一冻结版本的变化。首次生成一律 ``added``。
FieldChange = Literal["same", "added", "removed", "modified"]

MaskingMethod = Literal["mask_middle", "hash", "truncate"]

#: 派生列算子（be-10 §6.1 的 ``TransformSpec.kind``）。
TransformKind = Literal["cast", "concat", "mask", "hash", "truncate", "date_format", "case_when", "constant"]

#: 过滤所在阶段：``source`` 进内层 WHERE，``post_transform`` 进外层。
FilterStage = Literal["source", "post_transform"]

QualityRuleType = Literal["not_null", "regex", "range", "enum", "type_cast"]
OnViolation = Literal["reject", "warn"]

#: 拓扑节点类型（前端 ``DagNodeType``）。注意**没有** ``mapping``：
#: 字段映射是「字段级的边」，画成节点会让图比数据流本身还复杂。
DagNodeType = Literal["source", "transform", "filter", "quality", "sink"]

#: Plan 结构校验子码（be-10 §6.2，剔除不可表达的三项后重排）。
PlanIssueCode = Literal[
    "duplicate_id",
    "dangling_ref",
    "dag_cycle",
    "transform_unused",
    "quality_rule_field_missing",
    "budget_nonpositive",
    "mapping_unmapped_target",
    #: 必需项缺失（整段为空，或某个必填字段为空）。be-10 §6.2 没有这一条，
    #: 因为它的 `PlanInput.columns` 是可空的、`table` 由结构约束保证非空；
    #: 本 DSL 把列的权威交给 Profile 之后，「源表名没填」这类问题就只能在校验期报，
    #: 不能靠 Pydantic 的必填（必填会让**部分生成**的中间态直接构造失败，
    #: 而生成/修复链路恰恰要能拿到「不完整但可描述」的 Plan 才好报错）。
    "required_missing",
]

PlanSeverity = Literal["warn", "block"]


_BASE = ConfigDict(extra="forbid", frozen=True, populate_by_name=True, str_strip_whitespace=True)


# ---------------------------------------------------------------- 子结构


class IncrementalSpec(BaseModel):
    """增量抽取。缺省表示全量。"""

    model_config = _BASE

    column: str
    mode: Literal["append", "upsert"]
    start_value: str | None = None


class PlanInput(BaseModel):
    """源端。``schema_name`` 对外的键名是 ``schema``（与前端一致）。

    用别名而不是直接叫 ``schema``：``schema`` 是 Pydantic 的历史保留名，
    直接做字段名会与 ``BaseModel.schema()`` 撞车。仓库已有的先例见
    ``engines/policy/models.py`` 的 ``ResourceRef.schema_``。
    """

    model_config = _BASE

    id: str = Field(min_length=1, max_length=64)
    data_source_id: int = Field(ge=1)
    schema_name: str = Field(default="", alias="schema", max_length=128)
    table: str = Field(default="", max_length=128)
    kind: InputKind = "table"
    query: str | None = None
    path: str | None = None
    format: FileFormat | None = None
    incremental: IncrementalSpec | None = None


class PlanOutput(BaseModel):
    """目标端。``mode`` 固定为 ``shadow_then_swap``。"""

    model_config = _BASE

    id: str = Field(min_length=1, max_length=64)
    data_source_id: int = Field(ge=1)
    database: str = Field(default="", max_length=128)
    table: str = Field(min_length=1, max_length=128)
    mode: OutputMode = "shadow_then_swap"


class FieldMasking(BaseModel):
    model_config = _BASE

    method: MaskingMethod
    params: dict[str, Any] = Field(default_factory=dict)


class FieldMapping(BaseModel):
    """一个源字段 → 一个目标字段。

    ``change`` 是相对上一冻结版本的变化标记，由生成服务比对得出
    （首次生成一律 ``added``）——它参与制品指纹，所以必须由输入决定而不是随手写死。
    """

    model_config = _BASE

    source_field: str = Field(min_length=1, max_length=128)
    source_type: str = Field(min_length=1, max_length=64)
    target_field: str = Field(min_length=1, max_length=128)
    target_type: str = Field(min_length=1, max_length=64)
    transform: FieldTransform = "none"
    masking: FieldMasking | None = None
    quality_rule_ids: list[str] = Field(default_factory=list)
    change: FieldChange = "added"


class EtlTransform(BaseModel):
    """派生列：由 ``input_fields`` 算出 ``output_field``。"""

    model_config = _BASE

    id: str = Field(min_length=1, max_length=64)
    kind: TransformKind
    input_fields: list[str] = Field(default_factory=list)
    output_field: str = Field(min_length=1, max_length=128)
    params: dict[str, Any] = Field(default_factory=dict)


class EtlFilter(BaseModel):
    model_config = _BASE

    id: str = Field(min_length=1, max_length=64)
    expression: str = Field(min_length=1, max_length=1024)
    description: str = Field(default="", max_length=512)
    stage: FilterStage = "source"


class QualityRule(BaseModel):
    """字段级质量规则，展开为 ``quality_contract.rules``。"""

    model_config = _BASE

    id: str = Field(min_length=1, max_length=64)
    field: str = Field(min_length=1, max_length=128)
    rule_type: QualityRuleType
    params: dict[str, Any] = Field(default_factory=dict)
    error_code: ErrorCodeLabel
    on_violation: OnViolation = "reject"
    enabled: bool = True


class DagNode(BaseModel):
    model_config = _BASE

    id: str = Field(min_length=1, max_length=64)
    type: DagNodeType
    label: str = Field(default="", max_length=128)
    ref: str = Field(default="", max_length=128)
    meta: dict[str, Any] = Field(default_factory=dict)
    #: 前端 Vue Flow 的坐标；后端可以不给，前端会按最长路径自行分层
    position: dict[str, float] | None = None


class DagEdge(BaseModel):
    model_config = _BASE

    id: str = Field(min_length=1, max_length=128)
    source: str = Field(min_length=1, max_length=64)
    target: str = Field(min_length=1, max_length=64)
    label: str | None = None


class EtlPlanDag(BaseModel):
    model_config = _BASE

    nodes: list[DagNode] = Field(default_factory=list)
    edges: list[DagEdge] = Field(default_factory=list)


class RuntimeBudget(BaseModel):
    """运行预算，与 ``runtime_supervision_contract`` 的六列一一对应。

    比率用 ``Decimal`` 而不是 ``float``：预算比较是精确比较，
    ``0.05`` 的二进制浮点近似会让「刚好等于上限」的作业被判成超限。
    """

    model_config = _BASE

    max_read_rows: int = Field(gt=0)
    max_write_bytes: int = Field(gt=0)
    max_duration_sec: int = Field(gt=0)
    max_amplification_ratio: Decimal = Field(gt=0)
    max_reject_rate: Decimal = Field(gt=0, le=1)
    warn_threshold_pct: int = Field(gt=0, le=100)


# ---------------------------------------------------------------- 顶层


class EtlPlanModel(BaseModel):
    """一份完整的 ETL 方案。

    ``inputs`` / ``outputs`` / ``field_mappings`` 允许为空列表（构造期不拦），
    由 :func:`engines.plan.validate.validate_plan` 给出**可定位的**问题清单——
    报「inputs[0] 缺 table」远比 Pydantic 抛「至少 1 项」有用，
    而且门禁的自动修复回路需要结构化的 findings 才能产出补丁。
    """

    model_config = _BASE

    schema_version: SchemaVersion = "1.0"
    inputs: list[PlanInput] = Field(default_factory=list)
    outputs: list[PlanOutput] = Field(default_factory=list)
    field_mappings: list[FieldMapping] = Field(default_factory=list)
    transforms: list[EtlTransform] = Field(default_factory=list)
    filters: list[EtlFilter] = Field(default_factory=list)
    quality_rules: list[QualityRule] = Field(default_factory=list)
    dag: EtlPlanDag = Field(default_factory=EtlPlanDag)
    budget: RuntimeBudget


class PlanIssue(BaseModel):
    """结构校验的一条问题。``path`` 用 JSON Pointer 风格，前端据此高亮字段。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    code: PlanIssueCode
    message: str
    severity: PlanSeverity = "block"


#: 派生列的来源字段集合，供渲染器与校验器共用一份定义。
FieldRefs = Annotated[list[str], Field(default_factory=list)]


def dump_plan(plan: EtlPlanModel) -> dict[str, Any]:
    """Plan → 可落库 / 可算指纹的 dict（**序列化的唯一出口**）。

    ``by_alias=True``：``schema_name`` 必须还原成 ``schema``，否则
    jsonb 里存的和 API 返回的、算指纹的会是三份不同的键名。
    ``exclude_none=False``：``null`` 键**不省略**——省略会让「后续新增字段」
    悄悄改变历史数据的语义（be-12 §6.1）。
    """
    return plan.model_dump(mode="json", by_alias=True, exclude_none=False)
