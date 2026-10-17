# M2-04 · be-10 EtlPlan 与 HOCON 生成

> 状态：**已实现**（Plan DSL + 结构校验 + 兜底起草器 + HOCON 确定性渲染/解析 + 连接器模板注册表 + 生成图两节点 + 候选制品落库 + 只读制品接口）
> 期次：M2 ｜ 依赖：be-01（`core`）、be-02（元数据档案 `MetadataProfile`）、be-08（LLM Provider / 提示词块 / token 裁剪）、be-09（LangGraph 状态机与节点契约）、be-11（门禁消费本模块的 Plan 与 HOCON）
> 被依赖：be-11（`syntax` / `schema` / `mapping` 三类检查直接读本模块的产物）、be-12（冻结清单与制品指纹）、be-15（执行期令牌物化）、be-17（质量契约）
> 设计来源：[be-10-EtlPlan与HOCON生成.md](../模块概要设计/be-10-EtlPlan与HOCON生成.md)
> 数据来源：[核心数据字典.md](../核心数据字典.md) §3（`etl_plan` / `hocon_artifact` / `pipeline_version`）
> 需求来源：[PRD.md](../PRD.md) §9.2（Pipeline 分组：生成、门禁、版本冻结、Diff）、§6.3
>
> 本 spec 为**实现后补写**，记录**实际落地**的形态；与 be-10 设计文档不一致处已在 §10 登记（编号接全局清单）。
> 全部结论均由 `file:line` 佐证；凡与设计文档不同之处，一律以代码为准。

---

## 1. 模块定位

### 1.1 做什么

1. **Plan DSL 的类型契约**：`EtlPlanModel`（`engines/plan/schema.py:265`）定义「一份 ETL 方案」的形状——
   源端 / 目标端 / 字段映射 / 派生列 / 过滤 / 质量规则 / 拓扑图 / 运行预算，全部 `frozen=True + extra="forbid"`。
2. **Plan 的**结构**校验**：`validate_plan()`（`engines/plan/validate.py:30`）只判「方案自己有没有自相矛盾」（引用闭合、无环、无重复 id），**不判**「能不能上生产」（那是门禁），返回全量 `PlanIssue`（带 JSON Pointer 路径）。
3. **确定性兜底起草器**：`draft_plan()`（`engines/plan/draft.py:175`）在没有任何模型凭据时，用规则从源表档案直接起草一份合法的 Plan —— 它是 `LLM_PROVIDER=fake` 的**正式通路**，不是测试开关。
4. **HOCON 确定性渲染**：`render_hocon(plan, ctx)`（`engines/hocon/render.py:441`）把 Plan 渲染成 SeaTunnel HOCON，
   **同一份 Plan + 同一份 `RenderContext` 必得逐字节相同的文本**（`tests/test_hocon_render.py:82` 渲染一百次比对）。
5. **HOCON 解析与语法校验**：`parse_and_validate(content)`（`engines/hocon/parser.py:62`）用 `pyhocon` 解析，
   **从不抛异常**——任何输入都变成 `HoconParseResult(ok=False, errors=[...])`，供门禁的 `check_syntax` 直接消费。
6. **连接器模板注册表**：`engines/hocon/connectors/`（三族七种 `ds_type`），`template_for(ds_type, kind)`（`connectors/__init__.py:62`）选插件与键序；**未登记的类型抛错，不回落默认模板**。
7. **生成链路的两个图节点**：`generate_plan_node`（`workflow/nodes/generate_plan.py:206`，调 LLM）+ `generate_hocon_node`（`workflow/nodes/generate_hocon.py:63`，纯函数），以及把产物落成**候选制品行**的 `_persist_turn`（`services/pipeline/generation.py:381`）。
8. **只读制品接口**：按 `version_id` 拉 Plan / HOCON（`api/pipeline_versions.py:87`、`:97`），草案与冻结版都可读。

### 1.2 明确不做

- **不做门禁**（be-11）。本模块只产出「一份语法合法的制品 + 结构问题清单」，`block/warn/pass` 与门禁结论合成全在 `engines/gate/`。
- **不做自动修复**（be-11）。`validate_plan` 的输出是修复回路的**输入**，本模块不消费它。
- **不做冻结与指纹**（be-12）。本模块只提供 `sha256` 与落库形态；`freeze_version`、五项清单、四眼前置条件都在 be-12。
- **不打明文口令**：渲染器对 JDBC 参数**一律写令牌**（`render.py:341-354`），制品里没有 `jdbc_url` / `username` / `password` 的真值（CLAUDE.md §5.2）。
- **不自己取时间、不读库、不联网**：`engines/plan/*` 与 `engines/hocon/*` 是纯函数层，外部事实（数据源类型、档案、预算）一律由 service 注入。
- **不落草案到 `etl_plan`**：草案只活在 Workflow State；只有「出方案」的那一轮由 `_persist_turn` 落库（`workflow/nodes/generate_plan.py:18-19` 的注释说明了为什么中间产物不插不可变行）。

---

## 2. 分层落点

| 层 | 文件 | 状态 |
|---|---|---|
| `engines/plan/` | `schema.py` Plan DSL 全部模型 + `PlanIssue` / `PlanIssueCode` + `dump_plan()`（序列化唯一出口） | 已实现 |
| `engines/plan/` | `validate.py` `validate_plan()` + 迭代式 DFS 环检测 | 已实现 |
| `engines/plan/` | `draft.py` 兜底起草器（`DraftColumn` / `DraftTable` / `DraftSourceTable` / `DraftTargetTable` + `draft_plan()`） | 已实现 |
| `engines/plan/` | `__init__.py` 导出（`plan/__init__.py:9-23`） | 已实现 |
| `engines/hocon/` | `types.py` `PARSER_VERSION` / 令牌语法与两道闸 / `RenderContext` / `RenderedHocon` / `HoconParseResult` | 已实现 |
| `engines/hocon/` | `render.py` `render_hocon()` / `render_sql()` / `RENDER_ERRORS` | 已实现 |
| `engines/hocon/` | `parser.py` `parse_and_validate()`（永不抛异常） | 已实现 |
| `engines/hocon/` | `connectors/` 注册表 + `base.py`（`BlockTemplate` / `UnknownConnectorError` / 引号与值渲染）+ `jdbc.py` / `file.py` | 已实现 |
| `services/` | `pipeline/generation.py` 生成会话编排 + `_persist_turn` 落制品 | 已实现 |
| `services/` | `pipeline/artifacts.py` 制品门面（`get_plan` / `get_hocon` / `get_gate` / `freeze_version` / `diff_versions`） | 已实现 |
| `services/` | `pipeline/gating.py::render_meta()` 渲染段元信息的唯一生产者 | 已实现 |
| `services/` | `pipeline/parts.py` 制品指纹五部分的唯一拼法（与 be-12 共用） | 已实现 |
| `workflow/` | `nodes/generate_plan.py` / `nodes/generate_hocon.py`；`nodes/__init__.py::NODE_OUTPUTS` 白名单 | 已实现 |
| `schemas/` | `pipeline.py` `WorkflowSnapshot` / `EtlPlanRead` / `HoconArtifactRead` / `PlanValidationError` | 已实现 |
| `api/` | `pipelines.py`（生成会话 + 版本列表）、`pipeline_versions.py`（`/plan` / `/hocon`） | 已实现 |
| `models/` | `models/pipeline.py::EtlPlan`（`:98`）/ `HoconArtifact`（`:114`） | 📄 已有，本模块未改（列宽见 §10.8） |
| `core/` | `core/errors.py::ErrorCode.PLAN_INVALID`（`:55`）/ `HOCON_SYNTAX_ERROR`（`:56`） | 📄 已有，本模块未改 |
| `clients/` | `clients/llm/base.py::GenerationContext`（`:155`）、`clients/llm/prompts/plan.py` 的 `plan_blocks` / `plan_request` | 📄 已有（be-08），本模块只调用 |

§2 中三条 📄 已有项已用 grep 逐条核对（`models/pipeline.py` 的类定义、`core/errors.py` 的两个枚举值、`clients/llm/base.py` 的模型定义），见交付报告。

---

## 3. 涉及的表

| 表 | 动作 | 关键约束 |
|---|---|---|
| `etl_plan` | INSERT（`save_artifacts`） | 🔒 不可变：`schema_version` / `plan`(jsonb) / `valid` / `validation_errors`（字典 §3） |
| `hocon_artifact` | INSERT | 🔒 不可变：`content` / `sha256` / `parser_version` / `validation_result`(jsonb) |
| `pipeline_version` | INSERT / UPDATE（**仅候选行**） | 候选行 `frozen_at IS NULL`；冻结后整行不可改（`models/pipeline.py:199` 的 `__freeze_column__`） |
| `pipeline` | UPDATE（`status`） | 可写；`current_version_id` 只在冻结时被写（见 §10.9） |
| `metadata_profile` | 读 | 生成期读档案做画像注入；指纹第五部分按 Plan 重查（`artifacts.py:523`） |

字段与枚举一律见**字典 §3**，本 spec 不复制字段表。

---

## 4. 对外契约

### 4.1 HTTP

> 路径与 DTO 以 `frontend/src/api/pipeline.ts` 为准，全部路由已在 `backend/api/*.py` 的装饰器上核对。

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/v1/pipelines/generations` | 发起生成（`api/pipelines.py:102`），返回 `WorkflowSnapshot` |
| POST | `/api/v1/pipelines/generations/{thread_id}/answers` | 提交澄清回答并继续（`api/pipelines.py:129`） |
| GET | `/api/v1/pipelines/generations/{thread_id}` | 生成快照（断线恢复，`api/pipelines.py:155`） |
| POST | `/api/v1/pipelines/generations/{thread_id}/abort` | 中断生成（`api/pipelines.py:169`） |
| GET | `/api/v1/pipeline-versions/{version_id}/plan` | 读 Plan（**草案也能读**，`api/pipeline_versions.py:87`） |
| GET | `/api/v1/pipeline-versions/{version_id}/hocon` | 读 HOCON 制品（`api/pipeline_versions.py:97`） |

⚠️ **设计文档里的 `GET /api/v1/pipelines/{pipeline_id}/candidate` 不存在**（be-10 §3.1）。草案制品的对外读取走
`/pipeline-versions/{id}/{plan,hocon,gate}`——因为候选行本身就是一条 `pipeline_version` 行（`frozen_at IS NULL`），
`_load_bundle`（`artifacts.py:481`）对草案不加限制。登记见 §10.13。

`WorkflowSnapshot`（`schemas/pipeline.py:107`）刻意**只有 6 个键**（`thread_id / phase / phase_message / pending_questions / autofix_round / last_error`）：
它是「三层状态隔离」（PRD §8.1）的第一道实现，`snapshot()` 只投影白名单键（`workflow/state.py:476`），
Plan / HOCON / 门禁结论**都不进快照**——它们按 id 单独拉取（`tests/test_workflow_state.py:252` 守着这条）。

### 4.2 Python（跨模块契约）

```python
# engines/plan/schema.py
def dump_plan(plan: EtlPlanModel) -> dict[str, Any]: ...          # :302 序列化唯一出口（by_alias + 不省 null）
# engines/plan/validate.py
def validate_plan(plan: EtlPlanModel) -> list[PlanIssue]: ...     # :30  不短路、不抛异常
# engines/plan/draft.py
def draft_plan(*, requirement, source, target, budget, data_source_types) -> EtlPlanModel: ...  # :175
# engines/hocon/render.py
def render_hocon(plan: EtlPlanModel, ctx: RenderContext) -> RenderedHocon: ...   # :441 纯函数
def render_sql(plan: EtlPlanModel, ctx: RenderContext) -> str: ...               # :273 内层 SELECT
# engines/hocon/parser.py
def parse_and_validate(content: str) -> HoconParseResult: ...      # :62  永不抛
# engines/hocon/types.py
def assert_no_unresolved_tokens(text: str, *, where: str) -> None: ...          # :92  执行前的闸
def secret_token_problems(text: str, *, resolvable_data_source_ids: frozenset[int]) -> tuple[str, ...]: ...  # :109 冻结前的闸
```

`dump_plan()` 是**唯一的序列化出口**：落库形态、API 形态、指纹输入三处必须是同一个键名集合，
`by_alias=True` 把 `schema_name` 还原成 `schema`，`exclude_none=False` 保证新增字段不会悄悄改变历史数据语义（`schema.py:302-310`）。

---

## 5. 核心逻辑

### 5.1 Plan DSL 的形状（与设计 §6.1 的差异见 §10.14）

| 段 | 模型 | 要点 |
|---|---|---|
| `inputs[]` | `PlanInput`（`schema.py:123`） | `id / data_source_id / schema / table / kind(table\|query\|file) / query / path / format / incremental`；**没有 `columns`** ——列的权威归 Profile |
| `outputs[]` | `PlanOutput`（`:144`） | `id / data_source_id / database / table / mode`，`mode` 只有 `shadow_then_swap` |
| `field_mappings[]` | `FieldMapping`（`:163`） | `source_field / source_type / target_field / target_type / transform / masking / quality_rule_ids / change` |
| `transforms[]` | `EtlTransform`（`:182`） | 派生列：`kind(cast\|concat\|mask\|hash\|truncate\|date_format\|case_when\|constant)` |
| `filters[]` / `quality_rules[]` | `EtlFilter`（`:194`）/ `QualityRule`（`:203`） | 过滤分 `source` / `post_transform` 两阶段 |
| `dag` | `EtlPlanDag`（`:238`） | `nodes[].type` 只有 `source/transform/filter/quality/sink`（**没有 `mapping`**，`:83-85` 注释说明） |
| `budget` | `RuntimeBudget`（`:245`） | 六项，全部 `gt=0`；比率 `Decimal`（换 `float` 会让「刚好等于上限」判成超限） |

### 5.2 结构校验与门禁的分工（别把两者混在一起）

| | `validate_plan` | 门禁 `engines.gate` |
|---|---|---|
| 回答 | Plan **自洽**吗（引用闭合、无环、无重复） | 这方案**能上生产**吗（列存在、类型兼容） |
| 需要外部信息 | **不需要** | 需要档案、PDP 结论、HOCON 解析结果 |
| 时机 | 生成后立刻（LLM 输出的第一道闸） | 渲染出 HOCON 之后 |

`validate_plan` **返回全量问题而不短路**（`validate.py:32-36`）：这些问题的修复成本很低（大多是 LLM 一次重生成），一次列全能让重生成提示词带上完整反馈。

八个 `PlanIssueCode`（`schema.py:88-102`）：`duplicate_id` / `dangling_ref` / `dag_cycle` / `transform_unused` / `quality_rule_field_missing` / `budget_nonpositive` / `mapping_unmapped_target` / `required_missing`（末条为设计里没有、实现新增，理由见该处注释）。

两条纪律：

- **没有 path 的问题不许报**（`validate.py:17-18`）。报一条「Plan 有问题」给不出落点，用户除了重生成没有别的选择。
- **环检测用迭代式 DFS**（`validate.py:239`）：Plan 由 LLM 生成，节点数不可控，递归深度会把进程打崩，而 `RecursionError` 是未捕获的编程错误，会被上层当成 500 而不是「方案有问题」。

### 5.3 🔴 制品里只有令牌，没有口令

`_jdbc_params()`（`render.py:341`）对每个 JDBC 数据源一律写三枚令牌：

```
url      = {{secret:<ds_id>:jdbc_url}}
user     = {{secret:<ds_id>:username}}
password = {{secret:<ds_id>:password}}
```

- 白名单 `SECRET_FIELDS = ("jdbc_url", "username", "password")`（`types.py:52`）。**白名单而不是任意字段**：令牌是要被执行器替换成真值的，能令牌化任意字段就等于给了一条「把任意字符串变真口令」的通路。
- 渲染器**不做**「有没有 `secret_ref` 才写令牌」的分支：`RenderContext` 里根本没有这个事实（`render.py:344-347`）。缺凭据由门禁的 `check_resource` 报 `unregistered_secret`。
- `render_hocon` 返回的 `tokens` 是**从最终文本正则抠出来的**（`render.py:444-446`），不是渲染过程中顺手记的——后者会在某条分支忘了登记时少一枚令牌，而少一枚的后果是执行期拿着字面量 `{{secret:...}}` 去连库。

### 5.4 🔴 两道令牌闸判的是两件事，用错的那次让每一份合法制品都冻不了

| 闸 | 函数 | 问的问题 | 用在哪 |
|---|---|---|---|
| 执行前的闸 | `assert_no_unresolved_tokens`（`types.py:92`） | **替换了没有**？令牌必须一个不剩 | be-15 提交执行前 |
| 冻结前的闸 | `secret_token_problems`（`types.py:109`） | **到时候解得开吗**？令牌必须一个不少地可解析 | `freeze_version`（`artifacts.py:308`） |

冻结发生在执行之前很久，**制品本来就该带着令牌**（令牌正是「口令不进制品」的手段）。
早先冻结误用了执行前那道闸，后果是**每一份合法制品都冻不了**，报错文案还写着「仍有未替换的密钥令牌」——
读起来像渲染器漏了替换，实际是拿错了判据。`types.py:98-102` 与 `artifacts.py:316-326` 各留了一段注释守这条。

`secret_token_problems` 判两件事：① 字段名在白名单里（制品被人手改过 / 渲染器写错字）；
② 令牌指向的数据源在本租户里**存在 + 启用 + 配了密钥引用**（三者缺一条，执行期都取不到口令 = 「审批通过也跑不起来」）。
用例：`tests/test_hocon_secret_gates.py:40/50/59/65`。

### 5.5 🔴 `PARSER_VERSION` 撞到的三堵墙（「写进去的东西读不出来」）

`PARSER_VERSION = f"{PARSER_IMPL}+{RENDERER_VERSION}"` = `"pyhocon+elt-render-1"`，**20 字符**（`types.py:42-44`）。
它先后面临三处宽度限制，**每处的症状都不在同一个地方**：

1. `hocon_artifact.parser_version` 曾是 `varchar(16)` → 落库 SQLSTATE 22001，表现为「图跑完、门禁判完、请求 500」；
2. `schemas/version.py::ManifestHocon.parser_version` 曾限 16 → **冻结时**构造清单报校验错；
3. `schemas/pipeline.py::HoconArtifactRead.parser_version` 曾限 16 → 拉制品 422，而库里那一行**好好存着**。

现状：ORM 列已放宽到 `String(64)`（`models/pipeline.py:108` / `:126`），两个 DTO 已放宽到 `max_length=64`，
并由迁移 `alembic/versions/20261016_2017_502b83682bf4_widen_artifact_version_columns_to_64.py` 落地。
守着它的两条用例：`tests/test_version_columns_fit.py:40`（引擎版本串 ≤ 列宽，列宽从 ORM 元数据读，不写死数字）
与 `tests/test_dto_version_widths.py`（扫全部 `schemas/` DTO，凡 `*_version` 结尾的字段不得窄于实际取值）。
**字典 §3 仍写 `varchar(16)`，是陈旧的**，见 §10.8。

### 5.6 渲染确定性（同一份 Plan 必得同一份字节）

| 保证 | 做法 |
|---|---|
| 段顺序固定 | `env → source → transform → sink`（`render.py:454-459`） |
| 结尾换行恰好一个 | `"\n".join(blocks) + "\n"`（`render.py:460`） |
| 两空格缩进、块头独占一行 | 用例 `test_hocon_render.py:121` |
| 不含注释 | 用例 `test_hocon_render.py:113` |
| `Decimal` 按四位定标输出 | 用例 `test_hocon_render.py:133` |
| 数组保序、不排序 | 用例 `test_hocon_render.py:142` |
| `job_name` 只由表名派生 | `_job_name()`（`render.py:320`），不含行 id 与时间；用例 `:158` |
| 标识符注入当场抛错 | `_ident()`（`render.py:62`）+ 用例 `:323` |
| 无时间、无随机、无 IO | `engines/` 纯函数层；`draft_plan` 另有用例 `test_plan_draft.py:test_draft_is_byte_identical_across_calls` / `..._does_not_embed_time_or_randomness` |

并行度**固定 1 是保守默认**：并行度 > 1 时 Doris/JDBC sink 的写入顺序与 `data_save_mode=APPEND_DATA` 的组合会产生重复行，
而影子表 + Swap 的正确性依赖「写一次就是一次」（`types.py:142-145`）。

### 5.7 解析器契约：从不抛异常

`parse_and_validate` 把任何输入都变成 `HoconParseResult`（`parser.py:62`；用例 `test_hocon_parser.py:124` 覆盖「任何输入都不抛异常」）：

- 合法 → `ok=True`，`config` 是**普通 dict**（供门禁 `check_schema` 判「有没有 source/sink 段」）；
- 非法 → `ok=False` + 可定位的 `HoconParseError(line, column)`；
- 错误行里含疑似凭据时**被抹掉**（`parser.py:149` `_redact`；用例 `test_hocon_parser.py:114`）。

`RENDER_ERRORS = (ValueError, UnknownConnectorError, KeyError)`（`render.py:465`）是**渲染失败**的异常集：
`generate_hocon_node` 接住它、落一份**空 HOCON + 一条错误**（`generate_hocon.py:107-126`），
让门禁按「制品缺失」报 finding，而不是把一次 500 抛给用户。空串而不是 `None`：`gate` 与 `freeze` 都以 `isinstance(hocon, str)` 判制品在不在。

### 5.8 连接器模板注册表

键是 `(ds_type, 角色)` 而不是 `ds_type`：同一个类型在两端可以是不同插件（MinIO 做源是 `S3File`，做目标另一套键序）。
三族：`jdbc`(`mysql/postgres/postgresql/doris/starrocks`) / `local_file`(`csv/file/json/parquet`) / `s3_file`(`minio/s3`)（`connectors/__init__.py:32-42`）。
别名（`postgres` / `s3`）是显式登记的，不是兜底匹配前缀；**未登记的类型一律抛 `UnknownConnectorError`**——
`oracle` / `clickhouse` / `excel` / `rest` 在枚举里存在但「仅注册接口，不真连」，回落 JDBC 模板会渲染出一份看起来能跑的 HOCON（`connectors/__init__.py:11-13`）。
`RenderContext.ds_type()` 取不到类型时**也抛错**（`types.py:149`）：回落 MySQL 模板去写 Doris 会产生一份语法合法、语义错误的配置。

> 与设计 §6.5 的差异：设计给模板加了 `validate()` 方法，实现里 `ConnectorTemplate` / `BlockTemplate`（`connectors/base.py:92` / `:114`）**没有** `validate()`——
> 连接器参数校验归门禁的 `check_resource` / `check_schema`（对着档案与 `secret_reference` 判），模板保持纯数据。

### 5.9 制品形状的唯一出口：`render_meta ⊕ artifact_meta`

`hocon_artifact.validation_result` 由两段拼成，**每段只有一份实现**：

- 渲染段 `render_meta(rendered)`（`services/pipeline/gating.py:72`）：`sha256 / parser_version / renderer_version / job_name / token_count`；
- 解析/门禁段 `GateOutcome.artifact_meta()`（`gating.py:113`）：`{parse: {...}, gate: <GateResult>, policy_version}`。

生成路径（`generate_hocon.py:106` + `gate.py:143`）与修复路径（`artifacts.py:723`，其门禁结论由 `gating.run_gate` 在 `:698` 重算）拼出的是**同一组键**（用例 `tests/test_artifact_meta_shape.py:94`）。
早先两处各拼一份，形状慢慢分叉，症状是 `get_gate` 读到的是套了一层的信封（见 M2-05 §5.8）。
`parser_version` 记的是**解析器**版本而不是渲染器版本——落地后要能回答「同一份 HOCON 昨天过今天不过，是哪一端变了」（用例 `test_artifact_meta_shape.py:110`）。

### 5.10 候选行的不变式与事务边界

- **一个 pipeline 最多一行候选**：部分唯一索引 `uq_pipeline_version_one_candidate`（`repository.py:21`），
  `get_or_create_candidate`（`repository.py:362`）撞唯一约束时**回滚到保存点再重查一次**（`repository.py:410-419`）——
  重试的全部意义就在这里，重投是常态（Celery 至少一次投递）。
- **候选行可读但不出现在版本列表里**：`list_versions(frozen_only=True)`（`artifacts.py:154`）只列冻结版本；
  草案的完整内容走 `/plan` + `/hocon` + `/gate`。
- **`get_version` 拒绝草案**：`PipelineVersionSummary.frozen_at` 是必填，草案硬塞默认值等于伪造「已冻结」身份（`artifacts.py:170`）。
- **事务边界归 service**：`_persist_turn`（`generation.py:381`）末尾由调用方提交；`core/db.get_session` 只借连接不提交。
  漏提交的症状极具误导性——LangGraph 的 checkpoint 自己提交，于是 `checkpoints` 表有记录、`pipeline` 表一行没有。

---

## 6. 错误码

| # | 触发条件 | 错误码 | HTTP | 依据 |
|---|---|---|---|---|
| 1 | 生成时缺注入（`source_profile` / `budget` / `data_source_types` / `requirement`） | `VALIDATION_ERROR` | 422 | `generate_plan.py:63-143`（**缺注入不静默兜底**，报错文案带「编排层未注入」） |
| 2 | 未生成 Plan 就渲染 HOCON / 制品不完整就过门禁 | `PLAN_INVALID` | 422 | `generate_hocon.py:73`、`workflow/nodes/gate.py:65` |
| 3 | 渲染期 Plan 表达不出 HOCON（未知连接器、缺类型、标识符注入） | 不抛给用户：落成 `hocon_errors` 让门禁报 | — | `render.py:465` + `generate_hocon.py:107` |
| 4 | 数据源类型未登记 | `UnknownConnectorError`（引擎内部 `ValueError`） | 由调用方译 | `connectors/__init__.py:59` |
| 5 | 拉制品遇到没有落库门禁结论的行 | `CONFLICT` | 409 | `artifacts.py:213` |
| 6 | 读草案的版本详情 | `CONFLICT` | 409 | `artifacts.py:171` |

`HOCON_SYNTAX_ERROR`（`core/errors.py:56`）目前**没有生产方**：语法错误一律走「落 `parse.ok=False` + 门禁报 `syntax` finding」，
不抛 HTTP 错误。是否要保留该码见 §10。

---

## 7. 异步任务

**本模块无异步任务（无 Celery task）。** 生成链路的两个节点在 LangGraph 图内**同步执行**（`generate_plan_node` 是 `async def`，但由图调度，不投 Celery），
理由是：生成必须能「当场中断 / 当场提问 / 断线后按 checkpoint 恢复」，而这三件事的载体是 `langgraph_checkpoint*`（字典 §8），
投 Celery 会让「图跑到哪一步」出现两个真相。

唯一的异步边界是 `generate_plan_node` 里的 `await provider.complete_json(...)`（`generate_plan.py:268`），它由 httpx 承担，不经过 Celery。
Celery 的用途从 M3 的执行链路开始（Outbox → SeaTunnel 提交），与本模块无关。

---

## 8. 验收标准

| 验收点 | 用例 |
|---|---|
| 同一 Plan 渲染逐字节相同 | `tests/test_hocon_render.py:82`（渲染一百次比对） |
| 渲染产物只含令牌、无明文口令 | `tests/test_hocon_render.py:168`、`:185`（未替换令牌执行前硬失败） |
| 两道令牌闸判两件事 | `tests/test_hocon_secret_gates.py:40`（执行前只认令牌在不在）、`:50`（带令牌制品可被冻结检查放行） |
| 解析器从不抛异常 | `tests/test_hocon_parser.py:124`、`:132` |
| 解析自己的产物 `ok=True`（反函数） | `tests/test_hocon_parser.py:67` |
| 版本串装得下列宽 | `tests/test_version_columns_fit.py:40`、`tests/test_dto_version_widths.py` |
| 制品形状两段一致、`gate` 不是信封 | `tests/test_artifact_meta_shape.py:83/94/110` |
| 兜底起草器确定、不发明字段/路径 | `tests/test_plan_draft.py`（21 条） |
| 状态键与快照白名单（加 state 键 = 三处一起改） | `tests/test_workflow_state.py:test_state_sample_covers_every_declared_key`、`test_snapshot_keys_exactly_equal_frontend_contract` |
| 路由与前端函数一一对应 | `tests/test_pipeline_api.py:300`（前端十四个函数都有对应路由） |

> ❌ `validate_plan` 的八个 `PlanIssueCode` **没有独立用例**，见 §10.11。

---

## 9. 测试数据

| 文件 | 条数 | 覆盖 |
|---|---|---|
| `tests/test_hocon_render.py` | 28 | 逐字节 determinism、段顺序/缩进/换行、`Decimal` 定标、令牌白名单、`render_sql` 的 cast/truncate/hash/concat/case_when、标识符注入拦截 |
| `tests/test_hocon_parser.py` | 9 | 自产解析回环、点号键展开、行列号定位、凭据抹除、任何输入都不抛 |
| `tests/test_hocon_secret_gates.py` | 6 | 两道闸的正反例、数据源无凭据、字段不在白名单、令牌视图一致 |
| `tests/test_plan_draft.py` | 21 | 确定性与无时间、预算透传、敏感列必脱敏、类型不一致产生 cast、质量规则落点、源表/目标表选取、DAG 形状、空源表拒绝、未知字段拒绝 |
| `tests/test_version_columns_fit.py` | 2 | 引擎版本串 ≤ ORM 列宽（列宽从元数据读） |
| `tests/test_dto_version_widths.py` | 3 | 扫全部 DTO 的 `*_version` 字段；`generation_context` 读写往返 |
| `tests/test_artifact_meta_shape.py` | 4 | `gate` 键直接是 `GateResult`、两条生产路径同形、渲染段记解析器版本 |
| `tests/test_pipeline_api.py` | 20 | 路由注册顺序（`/diff` 先于 `/{version_id}`）、前端函数与路由对齐、DTO 接受前端形状、引擎类型复用 |
| `tests/test_workflow_state.py` | 45 | state 键覆盖、快照白名单、转移表、`status_after_gate`、澄清问答形状 |
| `tests/test_llm_provider.py` | 47 | 假 Provider 的分派、`dump_plan` 往返、修复回路（与 be-08 共有） |

---

## 10. 待确认与偏离登记

> 编号接全局清单：字典 §9 无编号条目，M1-01…M1-05 与 M2-01 的 §10 最大号为 7，故本 spec 起于 **8**。
> ⚠️ 同期撰写的 M2-02 / M2-03 / M2-06 由并行任务产出，各自独立推导起号（7 / 14 / 7），**区间与本 spec 重叠**；
> 合并时需做一次全量重编号。本 spec 内部自洽，跨 spec 引用见各条末尾的指向。
> 每条都给出「偏离在哪 / 为什么 / 代价」。

| # | 事项 | 处置建议 | 验证方式 |
|---|---|---|---|
| **8** | **字典 §3 陈旧**：仍写 `etl_plan.schema_version varchar(16)`、`hocon_artifact.parser_version varchar(16)`，而 ORM 已放宽到 `String(64)`（`models/pipeline.py:108`/`:126`），迁移 `20261016_2017_..._widen_artifact_version_columns_to_64.py` 已执行 | 回写字典 §3 两处类型为 `varchar(64)`（本 spec 不改文档，只登记） | 迁移文件 + `tests/test_version_columns_fit.py` |
| **9** | **候选版本 id 没有对外出口**：Studio 用 `pipeline.current_version_id` 定位制品（`frontend/src/views/pipeline/PipelineStudioView.vue:116`），而该列**只在冻结时**被写（`services/pipeline/repository.py:464` `set_current_version`）。生成成功后前端拿不到候选 `version_id`，`/plan` / `/hocon` / `/gate` 无从调用（假数据里由 `api/mock/index.ts:958` 补上这一跳） | 候选行落库时把 `pipeline.current_version_id` 指向它，或在 `WorkflowSnapshot` 加一个候选 id 字段（前者不动前端契约） | 造一次生成，断言 `/pipelines/{id}` 的 `current_version_id` 指向候选行且能拉到 Plan |
| **10** | **`POST /pipeline-versions/{id}/autofix` 的响应模型与 service 返回值不相容**（详见 M2-05 §10.22，同一处缺陷登记在门禁 spec） | 二选一：service 回「最后一条 `AutofixRound`」，或路由 `response_model` 改回 `GateResult`。前端 `api/pipeline.ts:108` 的签名是 **`AutofixRound`**，倾向改 service | 直接 `TestClient` 打一次 `/autofix`，当前会 500（响应校验期） |
| **11** | **`validate_plan` 无独立单测**：全仓库 grep 不到测试调用 `validate_plan(`；八个 `PlanIssueCode` 只有间接覆盖（`tests/test_llm_provider.py:513` 仅列 code 名做修复回路桩） | 补 `tests/test_plan_validate.py`：每个 code 一条正例 + 一条「干净 Plan 零问题」+ 环检测用迭代 DFS 的深图用例 | 新增用例后 `pytest -k plan_validate` |
| **12** | **设计文档的 `GET /pipelines/{id}/candidate` 未实现**（be-10 §3.1）。草案制品改走 `/pipeline-versions/{id}/{plan,hocon,gate}` | 回写设计文档 §3.1；若要保留 `/candidate`，它只能是 `/plan`+`/hocon`+`/gate` 的聚合读，不引入新语义 | 对比 `be-10-...md` §3.1 与 `api/pipeline_versions.py` 的路由表 |
| **13** | **Plan DSL 与设计 §6.1 的差异**：① `PlanInput.columns` **取消**（列权威归 Profile，`schema.py:96-101` 说明「源表名没填」这类问题只能在校验期报）；② 新增 `transforms` / `filters` / `dag.position`；③ 新增 `PlanIssueCode.required_missing`；④ `DagNodeType` 去掉 `mapping` | 回写设计 §6.1 / §6.2，把「列的权威在 Profile」写成显式契约 | 逐段比对 `engines/plan/schema.py` 与设计 §6.1 |
| **14** | **`draft.py` 兜底起草器是设计里没有的新增件**。它存在的理由是「没有模型凭据也要能跑通生成链路」（`LLM_PROVIDER=fake` 是正式通路，不是测试开关） | 给它补一段设计说明（be-10 §5.x）或归入 be-08 的 Provider 抽象 | `tests/test_plan_draft.py` 21 条 + `clients/llm` 的假 Provider 分派用例 |
| **15** | `schemas/pipeline.py` 模块 docstring（`:28`）仍写「HOCON 制品里的 `"${VAULT_xxx}"` 是占位符」，与渲染器实际产出的 `{{secret:N:field}}`（`types.py:47`）不一致 | 改 docstring 一处文案（不影响行为） | grep `VAULT_` 与 `TOKEN_RE` 的实际取值 |
| **16** | `HOCON_SYNTAX_ERROR`（`core/errors.py:56`）没有生产方：语法错误走「落 `parse.ok=False` + 门禁报 `syntax` finding」，不抛 HTTP | 保留（执行期 be-15 可能用）；若 M3 结束仍无生产方则删 | grep 全仓库 `HOCON_SYNTAX_ERROR` 的引用 |

### 与设计文档 §10 的对应

- 设计 §10.1（HOCON 解析库未定）→ **已定案**：`pyhocon`，`resolve=False` 行为与行号定位已由 `tests/test_hocon_parser.py` 覆盖。
- 设计 §10.2（SeaTunnel 版本与方言）→ **未决**，归 M3 演示场景一（锁镜像 tag）。
- 设计 §10.4（`PlanBudget` 与项目级配置的优先级）→ 本实现取 `services.policy.budget_defaults()`（Settings）为默认，项目级覆盖仍无载体（与 M2-01 §10.6 同源）。
- 设计 §10.5（`quality_rules` → 错误分流语义）→ 归 be-17。
- 设计 §10.6（大 plan 的 HOCON 行数上限）→ **未定**，仍是风险。

---

## 11. 变更记录

| 日期 | 变更 | 原因 |
|---|---|---|
| 2026-10-16 | 初版：实现后补写，登记 Plan DSL / 结构校验 / 兜底起草器 / HOCON 渲染与解析 / 连接器模板 / 两道令牌闸 / 版本列宽三堵墙 / 制品形状唯一出口 / 候选行不变式；§10 登记 9 条（8–16） | 补历史欠账；代码已端到端跑通（需求 → 澄清 → Plan → HOCON → 门禁 → 冻结） |
