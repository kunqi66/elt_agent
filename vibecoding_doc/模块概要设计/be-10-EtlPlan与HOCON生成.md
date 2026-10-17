# be-10 EtlPlan 与 HOCON 生成

> 上游：[概要设计总纲.md](../概要设计总纲.md) §6 模型上下文边界 ｜ [PRD.md](../PRD.md) §6.3.2、§12
> 数据：[核心数据字典.md](../核心数据字典.md) §3（`etl_plan`、`hocon_artifact`、`quality_contract`、`runtime_supervision_contract`）
> 代码位置：`backend/engines/plan/`、`backend/engines/hocon/`、`backend/services/pipeline/generation.py` ｜ 里程碑：M2 ｜ 依赖：be-08 be-09

## 1. 职责与边界

- **做什么**：
  - 定义 **EtlPlan DSL**（Pydantic 强类型）：整个生成链路的中间契约，上游是 LLM 输出，下游是渲染器与门禁。
  - `engines/plan/validate.py`：对 Plan 做**结构校验**（引用完整性、DAG 无环、必填项、预算合法性）——纯函数。
  - `engines/hocon/render.py`：EtlPlan → SeaTunnel HOCON 的**确定性**渲染——纯函数，同 Plan 必得同 HOCON。
  - `engines/hocon/parser.py`：HOCON 语法与必填项校验，产出**行列 + 字段路径**的错误定位——纯函数。
  - `engines/hocon/connectors/`：MySQL source / Doris sink / 文件 source 的参数模板与必填项清单。
  - `services/pipeline/generation.py`：编排「取 Profile → 组 prompt（be-08）→ 校验 → 渲染 → 回灌错误」，把草稿写进 Workflow State。
- **不做什么**：
  - **不做**门禁判定（[be-11](be-11-确定性门禁与有限自动修复.md)）；本模块的校验只回答「结构是否自洽」，不回答「是否允许执行」。
  - **不做**风险评级（[be-07](be-07-PDP风险策略引擎.md)）。
  - **不调用** LLM（LLM 只在 `clients/llm/`，由 be-09 的节点调用后把结果传进来）；`engines/` 内出现任何网络/DB/LLM 依赖即违反分层铁律。
  - **不落库**草稿（be-09 §5.4）；`etl_plan` / `hocon_artifact` 由 [be-12](be-12-版本冻结与制品指纹.md) 在冻结时写入。
  - **不做**机密物化：HOCON 里只出现 `${...}` 占位符，真实值由 [be-15](be-15-SeaTunnel客户端与Secret物化.md) 在 worker 内存中注入。
- **所在分层**：`engines/`（纯函数，只依赖 Pydantic + 标准库）+ `services/pipeline/generation.py`（编排）。

## 2. 模块结构

```
backend/engines/plan/
├── __init__.py         # 导出 EtlPlanModel 与 validate_plan
├── schema.py           # Plan DSL 的 Pydantic 定义（唯一契约来源）（待建）
└── validate.py         # 结构校验（纯函数）（待建）

backend/engines/hocon/
├── __init__.py         # 导出 render / parse_and_validate
├── render.py           # EtlPlan → HOCON（确定性渲染）（待建）
├── parser.py           # HOCON 语法与必填项校验 + 定位（待建）
└── connectors/
    ├── __init__.py     # 模板注册表 CONNECTOR_TEMPLATES
    ├── base.py         # ConnectorTemplate Protocol（待建）
    ├── mysql.py        # MySQL JDBC source（待建）
    ├── doris.py        # Doris sink（待建）
    └── file.py         # 文件 / MinIO source（待建）

backend/services/pipeline/
└── generation.py       # 组 prompt、调 be-08、串起校验与渲染（待建）
```

## 3. 对外接口

### 3.1 HTTP

| 方法 | 路径 | 权限 | 幂等 | 说明 |
|---|---|---|---|---|
| GET | `/api/v1/pipelines/{pipeline_id}/candidate` | 项目成员 | — | 读草稿 `{plan, hocon, plan_issues, hocon_issues, gate}`；草稿取自 Workflow State（be-09 §5.4）。本接口**只读**，无副作用 |

生成动作没有独立路由：它由 `POST /api/v1/pipelines/{id}/messages` 驱动（be-09 §3.1），保证「一次对话 = 一次生成尝试」，避免出现两条并行的生成入口。

### 3.2 Python

```python
# engines/plan/schema.py —— 中间契约（节选见 §6.1）
class EtlPlanModel(BaseModel): ...

# engines/plan/validate.py —— 纯函数
def validate_plan(plan: EtlPlanModel) -> list[PlanIssue]: ...

# engines/hocon/render.py —— 纯函数
def render(plan: EtlPlanModel, ctx: RenderContext) -> RenderedHocon: ...
def render_sql(plan: EtlPlanModel) -> str: ...          # field_mappings+transforms+filters → SELECT

# engines/hocon/parser.py —— 纯函数
def parse_and_validate(content: str) -> HoconValidation: ...

# services/pipeline/generation.py
async def generate_plan(
    *, ctx: RequestContext, profiles: ProfileBundle, params: dict[str, Any]
) -> GenerationOutcome[EtlPlanModel]: ...

async def generate_hocon(
    *, ctx: RequestContext, plan: EtlPlanModel, secrets: Sequence[SecretRefView]
) -> GenerationOutcome[RenderedHocon]: ...
```

**异常契约**：`engines/` 的函数**不抛领域异常**，一律返回问题列表（`list[PlanIssue]` / `HoconValidation`）。映射发生在 service 层：
- `validate_plan` 非空 → `PlanInvalidError`（`PLAN_INVALID`）；
- `parse_and_validate().ok is False` → `HoconSyntaxError`（`HOCON_SYNTAX_ERROR`）；
- service 层把两份问题列表**原样**交给门禁的自动修复回路（§6.6），不转写成自然语言。

## 4. 数据模型

| 表 | 写/读 | 关键列 | 约束与口径 |
|---|---|---|---|
| `etl_plan` | be-12 写（本模块定义内容） | `schema_version`（Plan DSL 版本）、`plan`（jsonb）、`valid`、`validation_errors` | `__immutable__`；`plan` 必须能通过 `EtlPlanModel.model_validate` 强校验（字典 §0：JSON 不裸存） |
| `hocon_artifact` | be-12 写 | `content`（text）、`sha256`（文本指纹）、`parser_version`、`validation_result` | `__immutable__`；`parser_version` 记录校验器版本，**不参与**制品指纹（be-12 §6） |
| `quality_contract` | be-12 写 | `rules`、`error_table_target`、`reject_rate_budget` | `rules[].rule_type` ∈ `not_null/regex/range/enum/type_cast`，`error_code` 取 `ErrorCodeLabel` |
| `runtime_supervision_contract` | be-12 写 | 五个预算 + `warn_threshold_pct` | 由 `PlanBudget` 展开 |
| `metadata_profile` | 读 | `schema_snapshot`、`masked_samples`、`sha256`、`status`、`expires_at` | 生成前必须 `status=ready` 且未过期，否则 `METADATA_PROFILE_EXPIRED` |
| `secret_reference` | 读 `key_names` | 只读键名拼占位符，**不取值** | 占位符映射由 service 构造 `RenderContext` |

## 5. 核心流程

### 5.1 生成一次 EtlPlan

1. be-09 的 `metadata_bind` 已产出 `ProfileBundle`（相关表的 `schema_snapshot` + 统计 + 脱敏样本 + `sha256`）。
2. `generation.py` 构造 `PromptBlock`（priority 见 be-08 §6.3），调 `fit_prompt` 裁剪。
3. `get_provider().complete_json(req, EtlPlanModel)`，得 `EtlPlanModel` 或 `StructuredOutputError`。
4. `validate_plan(plan)`：非空则**不渲染 HOCON**，直接带 `PlanIssue` 进入 `plan_errors`，交 back 给 be-11 的修复回路（或再问模型）。
5. 校验通过 → 写入 `state.draft_plan`。

### 5.2 渲染 HOCON

1. service 读 `secret_reference.key_names`（仅键名）+ `data_source_id`，构造 `RenderContext.placeholders`。
2. `render(plan, ctx)` → `RenderedHocon{content, sha256, placeholders}`。
3. `parse_and_validate(content)`：不通过 → `state.hocon_errors`，交修复回路。
4. 通过 → `state.draft_hocon`，进入门禁节点。

```
EtlPlanModel ──render()──▶ HOCON 文本 ──parse_and_validate()──▶ HoconValidation
      │                                                              │
      └──validate_plan()──▶ list[PlanIssue] ─────────────────────────┘
                                    │
                        （两者都作为结构化 findings 回灌自动修复）
```

### 5.3 无状态机

本模块不持有 Pipeline 状态。`PlanIssue` / `HoconIssue` 只描述制品本身；`PipelineStatus` 的转移由 [be-09](be-09-LangGraph工作流与三层状态.md) §5.5 负责。

## 6. 关键算法与规则

### 6.1 EtlPlan DSL 字段定义

顶层（`engines/plan/schema.py`，`model_config = ConfigDict(extra="forbid", frozen=True)`）：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `schema_version` | `Literal["1.0"]` | 是 | DSL 版本，落 `etl_plan.schema_version` |
| `inputs` | `list[PlanInput]`（≥1） | 是 | 源端 |
| `outputs` | `list[PlanOutput]`（≥1） | 是 | 目标端 |
| `field_mappings` | `list[FieldMapping]` | 是 | 源字段 → 目标字段 |
| `transforms` | `list[TransformSpec]` | 否（默认 `[]`） | 派生列与转换 |
| `filters` | `list[FilterSpec]` | 否（默认 `[]`） | 行级过滤 |
| `quality_rules` | `list[QualityRule]` | 否（默认 `[]`） | 展开为 `quality_contract.rules` |
| `dag` | `PlanDag` | 是 | 拓扑视图（前端 Vue Flow 直接消费） |
| `budget` | `PlanBudget` | 是 | 展开为 `runtime_supervision_contract` |

子结构：

| 结构 | 字段（名：类型，必填） |
|---|---|
| `PlanInput` | `id: str`✓（Plan 内唯一）、`data_source_id: int`✓、`kind: Literal["table","file","query"]`✓、`database: str?`、`schema_name: str?`（避开 Pydantic 保留名 `schema`）、`table: str?`、`path: str?`、`format: Literal["csv","json","parquet"]?`、`columns: list[ColumnRef]`✓、`incremental: IncrementalSpec?` |
| `IncrementalSpec` | `column: str`✓、`mode: Literal["append","upsert"]`✓、`start_value: str?` |
| `ColumnRef` | `name: str`✓、`data_type: str`✓（be-04 归一化后的类型名）、`nullable: bool`（默认 `true`）、`primary_key: bool`（默认 `false`） |
| `PlanOutput` | `id: str`✓、`data_source_id: int`✓、`database: str?`、`table: str`✓、`write_mode: Literal["append","upsert","overwrite"]`✓、`primary_keys: list[str]`✓、`columns: list[ColumnRef]`✓ |
| `FieldMapping` | `source_input_id: str`✓、`source_field: str`✓、`target_field: str`✓、`source_type: str`✓、`target_type: str`✓、`transform_ids: list[str]`（默认 `[]`）、`nullable_ok: bool`（默认 `true`） |
| `TransformSpec` | `id: str`✓（Plan 内唯一）、`kind: Literal["cast","concat","mask","hash","truncate","date_format","case_when","constant"]`✓、`inputs: list[str]`✓、`output: str`✓、`params: dict[str, Any]`（默认 `{}`） |
| `FilterSpec` | `id: str`✓、`expr: str`✓（受限表达式：列名 + 比较/逻辑运算符 + 字面量）、`stage: Literal["source","post_transform"]`✓ |
| `QualityRule` | `id: str`✓、`field: str`✓、`rule_type: Literal["not_null","regex","range","enum","type_cast"]`✓、`params: dict[str, Any]`、`error_code: ErrorCodeLabel`✓、`on_violation: Literal["reject","warn"]`（默认 `reject`） |
| `PlanDag` | `nodes: list[DagNode]`✓、`edges: list[DagEdge]`✓ |
| `DagNode` | `id: str`✓、`kind: Literal["source","mapping","transform","filter","quality","sink"]`✓、`ref: str`✓（指向输入/映射/转换/过滤/规则/输出的 id）、`depends_on: list[str]` |
| `DagEdge` | `from_node: str`✓、`to_node: str`✓ |
| `PlanBudget` | `max_read_rows: int`✓、`max_write_bytes: int`✓、`max_duration_sec: int`✓、`max_amplification_ratio: Decimal`✓、`max_reject_rate: Decimal`✓（默认取 `settings.SUP_*` 与项目配置） |

### 6.2 结构校验（`validate_plan`，纯函数）

返回 `list[PlanIssue]`，`PlanIssue{path: str, code: PlanIssueCode, message: str}`。`PlanIssueCode` 是**校验子码**（`Literal`，与 `core.errors.ErrorCode` 无关，不得混用）：

| code | 判定 |
|---|---|
| `duplicate_id` | 同一命名空间（`inputs` / `outputs` / `transforms` / `filters` / `quality_rules` / `dag.nodes`）内 id 重复 |
| `dangling_ref` | `dag` 的 `ref` / `depends_on` / `from_node` / `to_node`，或 `field_mappings.source_input_id` / `transform_ids` 指向不存在的 id |
| `dag_cycle` | `dag` 有向图存在环（拓扑排序失败时报告环上的节点 id） |
| `mapping_source_missing` | `field_mappings.source_field` 不在对应 `PlanInput.columns` |
| `mapping_target_missing` | `field_mappings.target_field` 不在对应 `PlanOutput.columns` |
| `missing_primary_key` | `PlanOutput.write_mode == "upsert"` 且 `primary_keys` 为空 |
| `budget_nonpositive` | `PlanBudget` 任一字段 ≤ 0 |
| `transform_unused` | 某 `TransformSpec` 的 `output` 未被任何 `FieldMapping` 引用（warn 级，见 §6.5） |
| `quality_rule_field_missing` | `QualityRule.field` 不在任何输出列 |

`path` 用 JSON Pointer 风格：`inputs[0].columns[2].name`、`dag.nodes[3].ref`、`field_mappings[5].target_field`。

### 6.3 渲染契约（`render`，纯函数）

**输入 → 输出**：`(EtlPlanModel, RenderContext) → RenderedHocon{content, sha256, placeholders}`。

映射规则：

| Plan 结构 | HOCON 落点 |
|---|---|
| `inputs` | `source { <Plugin> { ... } }`（多输入时 SeaTunnel 侧按 `Sql` transform 的 FROM/JOIN 组织） |
| `outputs` | `sink { <Plugin> { ... } }` |
| `field_mappings` + `transforms` + `filters` | `transform { Sql { query = "<SELECT ...>"; } }` |
| `quality_rules` | 同一条 SELECT 内的 `CASE WHEN` 分流表达式（错误码取自 `ErrorCodeLabel`） |
| `budget` | 不写入 HOCON（它是控制面契约）；仅 `parallelism` 由 `PlanInput` 数量与计划规模推导 |
| 环境 / 作业名 | `env { job.mode = "BATCH"; parallelism = n; job.name = "etl_{pipeline_id}_{plan_sha8}" }` |

**确定性规则**（同 Plan 必得同字节）：

1. **键顺序**：按连接器模板中**声明顺序**输出，既不排序也不依赖 `dict` 迭代顺序（`dict` 顺序是 Py3.7+ 的实现细节，不构成契约）。
2. **缩进**：2 空格；块头 `Name {` 独占一行。
3. **注释剥离**：生成物不含 `#` / `//` 注释；模板里也不写注释，避免「格式化时被吞」造成指纹漂移。
4. **数值**：整数原样；`Decimal` 按定标输出（`0.0500`，保留 4 位）；布尔小写 `true/false`；字符串双引号包裹并对 `"`、`\` 转义。
5. **数组**：保持 Plan 中的顺序（**不排序**）——顺序即语义（SELECT 的列顺序、DAG 依赖顺序）。
6. **无时间/随机**：不输出时间戳、UUID、计数器。
7. **换行**：`\n`，文件末尾恰好一个换行。

**占位符注入**（机制说明，值不在此产生）：

- 需要凭据的位置一律输出 `${ETL_SECRET_<secret_ref_id>_<KEY_NAME>}`，例如 `password = "${ETL_SECRET_3_PASSWORD}"`。
- 合法形态：`^\$\{[A-Z][A-Z0-9_]{0,63}\}$`。**禁止**默认值语法 `${X:-y}`、禁止嵌套、禁止大小写混用。
- `RenderContext.placeholders` 由 service 依据 `secret_reference.key_names` 生成（只有键名）；渲染器只负责**写出名字**，不知道值。
- 真实值由 [be-15](be-15-SeaTunnel客户端与Secret物化.md) 在 worker 内存中物化为环境变量后注入，作业结束即销毁（总纲 §6）。
- `RenderedHocon.placeholders` 是去重后的占位符名单，供 be-15 校验「物化了全部所需变量」。

### 6.4 语法与必填项校验（`parse_and_validate`，纯函数）

**解析器选择**：优先使用 `pyhocon` 做语法解析（需新增依赖，见 §10），并**关闭变量替换**（`ConfigFactory.parse_string(text, resolve=False)`），否则 `${...}` 会被当成未定义变量报错。若依赖不可得，退化为自研轻量校验：扫描块配平、键值形态、引号闭合、占位符正则。两条路径都必须产出同一套 `HoconIssue`。

校验维度：

| 维度 | 检查内容 | issue code |
|---|---|---|
| 语法 | 括号/引号配平、键值形态、非法字符 | `syntax` |
| 必需块 | `env` / `source` / `sink` 必须存在；`transform` 允许缺省 | `missing_block` |
| 连接器参数完备性 | 每个 plugin 块的 `required_keys`（§6.5）逐项存在且非空 | `missing_key` |
| 插件合法性 | plugin 名在 `CONNECTOR_TEMPLATES` 中 | `unknown_plugin` |
| 占位符合法性 | 形态匹配 §6.3 的正则；且名字出现在允许清单内 | `bad_placeholder` |
| 值类型 | 端口/并行度为正整数、比率在 `(0, 1]` | `bad_value` |

**定位粒度**（PRD §6.3.3「用户必须能定位每个门禁错误对应的配置、字段或拓扑节点」）：`HoconIssue{line: int, col: int, path: str, code, severity, message}`，其中
- `line` / `col` 从 1 开始，指向**该键所在行**；
- `path` 是 HOCON 路径，用点号连接并用引号包裹含特殊字符的段：`sink.Doris.table.identifier`、`source.Jdbc.url`；
- 前端据此在 CodeMirror 6 中高亮行并滚动定位。

### 6.5 连接器模板

`connectors/base.py`：

```python
class ConnectorTemplate(Protocol):
    plugin_name: str                         # "Jdbc" / "Doris" / "LocalFile"
    kind: Literal["source", "sink"]
    key_order: tuple[str, ...]               # 渲染的键顺序（确定性来源）
    required_keys: frozenset[str]
    def render(self, part: Mapping[str, Any], ctx: RenderContext) -> str: ...
    def validate(self, part: Mapping[str, Any], path: str) -> list[HoconIssue]: ...
```

| 模板 | plugin | 必填 | 关键可选 |
|---|---|---|---|
| MySQL source | `Jdbc` | `url`、`driver`、`user`、`password`、`query`（或 `table`） | `partition_column`、`partition_num`、`where_condition`、`connection_check_timeout_sec`、`fetch_size` |
| Doris sink | `Doris` | `fenodes`、`table.identifier`、`user`、`password` | `sink.label-prefix`、`sink.enable-2pc`、`sink.enable-delete`、`sink.batch.size`、`sink.properties.*` |
| 文件 source | `LocalFile` / `S3File`（MinIO） | `path`、`file_format_type` | `delimiter`、`read_columns`、`schema`、`encoding`、`skip_header_row_number` |

`password` 与 `user` 由 `secret_reference.key_names` 决定是否渲染：若该数据源无 `secret_ref_id`（公开文件源），不渲染这两个键。这条规则由模板的 `validate()` 交叉检查，避免「有引用却漏渲染」。

### 6.6 校验失败如何回灌自动修复

传给 [be-11](be-11-确定性门禁与有限自动修复.md) 的是**结构化列表**，不是自然语言描述：

```python
class Finding(BaseModel):            # be-11 定义，此处消费
    check_id: str
    severity: Literal["info", "warn", "block"]
    locator: Locator                 # hocon(行/列) | dag(节点) | field(字段路径)
    expected: str | None
    actual: str | None
    message: str                     # 人类可读，仅作辅助；不作为唯一载体
```

`PlanIssue` → `locator = {"kind": "field", "path": ...}`；`HoconIssue` → `locator = {"kind": "hocon", "line": ..., "col": ..., "path": ...}`；`dag_cycle` 额外产出 `{"kind": "dag", "node_id": ...}`。修复模板（be-08 `prompts/autofix.py`）只接收这些字段，模型据此输出补丁（`PlanPatch`），**不允许**让模型「自由重写整份 plan」——那会掩盖真实缺陷且无法做 Diff。

### 6.7 为什么必须可复现

`pipeline_version.sha256` 的输入包含 HOCON 全文（be-12 §6）。若渲染存在任何非确定性（字典序漂移、时间戳、随机 job 名），同一份 Plan 在不同时刻会算出不同指纹，直接破坏三件事：不可变版本的唯一性、审批绑定指纹的有效性、Capability 的指纹校验。故本节所有确定性规则都是**契约**，不是实现细节，改动等同于破坏兼容。

## 7. 错误码与拒绝语义

| 触发条件 | 错误码 | HTTP | 写证据账本 |
|---|---|---|---|
| `validate_plan` 返回非空 | `PLAN_INVALID` | 422 | 否（属生成过程失败，由 be-09 决定是否记 `PIPELINE_GENERATED` 的失败变体） |
| `parse_and_validate().ok is False` | `HOCON_SYNTAX_ERROR` | 422 | 否 |
| Plan 引用的表/字段在源或目标 Profile 中不存在（be-11 的 Schema 检查，此处仅产生 `mapping_*_missing`） | `SCHEMA_MISMATCH` | 409 | 否 |
| 生成前 `metadata_profile.status=expired` 或已过 `expires_at` | `METADATA_PROFILE_EXPIRED` | 409 | 否 |
| 模型两次结构化输出均不合规 | `PLAN_INVALID` | 422 | 否 |
| 占位符引用了未登记的 `secret_reference` | `PLAN_INVALID`（附带 `locator` 指向该键） | 422 | 否 |

本模块**不直接写证据账本**；生成成功的事件由 be-09 的 `generate_plan` / `generate_hocon` 节点汇总后写 `PIPELINE_GENERATED`。

## 8. 安全与合规约束

- **无明文 Secret**：渲染器只产出 `${...}` 占位符；`RenderContext` 里只有键名与占位符名，**没有**任何值。单测断言：对渲染产物做正则扫描，不得出现 `password = "` 后跟非 `${` 的内容。
- **模型上下文边界**：`generation.py` 只把 `schema_snapshot`（字段名/类型/可空/主键）、近似统计与 `masked_samples` 交给 be-08；连接串取自 `data_source.endpoint`（本就不含凭据），且出站前仍过 be-08 §6.6 的断言。
- **脱敏**：`masked_samples` 已由 [be-04](be-04-元数据探查与脱敏.md) 脱敏；本模块不做二次脱敏，但**不得**绕过 `masked_samples` 去读源端。
- **租户隔离**：`data_source_id` / `metadata_profile` / `secret_reference` 的读取全部经 `services/pipeline/repository.py` 并带 `tenant_id + project_id` 过滤。
- **不可变**：`etl_plan` / `hocon_artifact` 是 `__immutable__` 制品，写路径只在 be-12 的冻结事务内；本模块的草稿不落库，天然无从 UPDATE。
- **审计**：渲染与校验是纯计算，不写账本；但 `parser_version` 随制品落库，使「这份 HOCON 由哪个版本的校验器判定通过」可追溯。

## 9. 测试要点

**单元测试（纯函数，无 IO）**
- `EtlPlanModel`：`extra="forbid"` 拒绝未知键；缺少 `inputs`/`outputs` 拒绝；`frozen=True` 下赋值抛错。
- `validate_plan`：九类 `PlanIssueCode` 各一条正例；`dag_cycle` 能报出环上的节点集合；多重缺陷一次返回（不早退）。
- `render` 确定性：同一 Plan 连续渲染 100 次，`content` 逐字节相同、`sha256` 相同；把 `plan` 的 dict 键顺序打乱后重新构造模型，渲染结果不变（**反例**：字典序漂移）。
- `render_sql`：列顺序严格等于 `field_mappings` 顺序；`filter.stage="source"` 进 WHERE、`"post_transform"` 进外层。
- `parse_and_validate`：正常样例 `ok=True`；缺 `sink` 块报 `missing_block` 且 `line` 指向文件末尾；`url = "jdbc:mysql://u:p@h/db"` 中的明文口令形态报 `bad_value`/`syntax`（避免模型把真凭据写进 HOCON）。
- `connectors`：三张模板的 `required_keys` 与 `key_order` 完全一致（顺序表漏键会导致必填项静默不渲染）；无 `secret_ref` 时 `user`/`password` 不渲染。
- 定位精度：构造一行错键，断言 `line`/`col` 精确到该行的键起始列。

**集成测试（内存 SQLite + mock provider）**
- `generate_plan`：provider 返回非法 JSON 两次 → `PLAN_INVALID`，且**未渲染** HOCON（断言 `render` 未被调用）。
- `generate_hocon`：占位符集合与 `RenderContext.placeholders` 完全一致（不多不少），供 be-15 校验。
- `GET /candidate`：草稿为空时返回 `{plan: null}` 而非 500。

**必须覆盖的反例**
- **明文泄漏**：在 `masked_samples` 中混入一个未脱敏的手机号 → 断言出站断言拦截（be-08 §6.6），生成中止。
- **指纹漂移**：修改渲染器缩进规则后，历史冻结版本的 `sha256` 变化 → 断言 CI 中存在「渲染器变更必须伴随 docs 变更」的检查（可以是人工评审项）。
- **跨租户**：引用他项目的 `data_source_id` → 404/`TENANT_MISMATCH`。

## 10. 未决与风险

1. **HOCON 解析库未定**：`pyhocon` 需新增依赖（`uv add pyhocon`）；其 `resolve=False` 行为与错误定位（行列号）需实测。**验证方式**：M2 用 10 份真实 SeaTunnel 配置跑一遍，确认行列号与预期一致；若不可接受则走自研轻量校验路径。**在定案前不要开始实现 `parser.py`。**
2. **SeaTunnel 版本与 HOCON 方言**：本模块按 2.3.x 的「块套插件名」写法（`source { Jdbc { ... } }`）。镜像版本一换，插件名与键名可能变化。**验证方式**：M3 用锁定镜像版本跑通演示场景一，把镜像 tag 写进 `docker-compose.yml` 并在此记录。
3. **`transform { Sql { query } }` 的能力上限**：窗口函数、复杂 JOIN 是否全放在一条 SQL 里，取决于 SeaTunnel `Sql` 插件对函数的支持面。**未定**：若覆盖面不足，需引入 `FieldMapper` / `Filter` 等多插件组合，`render.py` 的映射表随之扩展。
4. **`PlanBudget` 与项目级配置的优先级**：`settings.SUP_*` 是全局默认，项目级覆盖（《需求对齐记录》§四「默认预算阈值可被项目级配置覆盖」）的载体尚未建模。**建议**在 `project` 或独立配置表落一处；在定案前 `PlanBudget` 由生成服务按全局默认填充。
5. **`quality_rules` → 错误分流表达式的完整语义**由 [be-17](be-17-质量契约与数据分流.md) 最终定义；本模块当前只保证「规则 → `CASE WHEN` 表达式 → 错误码标签」的语法层映射，两侧口径需在 M3 对齐。
6. **大 plan 的 HOCON 行数**：未设上限。若单份 HOCON 超过 5000 行，前端 CodeMirror 只读渲染可能卡顿。**验证方式**：fe-04 用最大规模演示用例实测。

## 11. 变更记录

| 日期 | 变更 | 原因 |
|---|---|---|
| 2026-10-16 | 首版 | M2 生成链路设计 |
