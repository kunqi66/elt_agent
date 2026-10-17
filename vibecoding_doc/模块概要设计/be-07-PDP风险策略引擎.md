# be-07 PDP 风险策略引擎

> 上游：[概要设计总纲.md](../概要设计总纲.md) §5 Harness 安全执行内核 ｜ [PRD.md](../PRD.md) §6.4.1 Prepare、§6.6 安全治理、§12 关键异常处理
> 数据：[核心数据字典.md](../核心数据字典.md) §8 支撑表（`policy_rule` / `risk_policy_version`）｜ 决策：[需求对齐记录.md](../需求对齐记录.md) §四 风险判定矩阵（已冻结）
> 代码位置：`backend/engines/policy/`、`backend/models/governance.py` ｜ 里程碑：M2 ｜ 依赖：be-01；被 be-09 be-11 be-13 be-20 依赖

## 1. 职责与边界

- **做什么**
  1. 以**纯函数 + 规则表**把「工具意图 + 资源范围 + 环境 + 数据分级」映射为 `RiskLevel`（P0–P3）、`blocked`、`required_slots`、`hardening_required` 与生效预算。
  2. 维护规则的可判定输入词表（特征向量）与条件 DSL，使规则可由安全审批人配置而**不必改代码**。
  3. 把《需求对齐记录》§四的 P0–P3 判定矩阵逐条落成可执行规则函数（本文 §6.3 是唯一权威映射）。
  4. 合并预算默认值（`Settings.SUP_MAX_*`）与项目级覆盖，输出 `BudgetRequest`。
  5. 定义策略版本化与生效语义（规则表变更必须经 `risk_policy_version` 审批后生效；已冻结的 `Preparation` 绑定当时的策略版本，变更**不追溯**），并提供策略查询 / 预检 / 变更 / 审批 / 回滚的接口。
- **不做什么**
  - **绝不允许 LLM 参与判定**：`engines/policy/` 禁止 import 任何 LLM、网络、DB、Redis 模块（CLAUDE.md §5.3、[PRD §1.4](../PRD.md)）。CI 有强制检查（§6.7），违反即构建失败。
  - **不碰 IO**：不查库、不读环境变量、不读文件、不取当前时间。规则集由 service 从 `policy_rule` 读出后传入，`now` 由调用方注入。
  - **不执行阻断**：`decide()` 只输出 `blocked=True` 与理由；真正拒绝由 be-11（门禁）、be-13（Prepare/Commit）`raise`，由 be-05 保证副作用不发生。
  - **不做语法/Schema/字段映射校验**：那是 be-11 门禁的其他门；PDP 只管策略门。
  - **不做运行时监督**：`runtime_supervision_contract` 的执行期比对属 be-16；PDP 只在 Prepare 阶段判断「申报预算是否超过默认阈值 3 倍」。
- **所在分层**：`engines/policy/`（纯函数层，只依赖 Pydantic 与标准库）；写侧编排在 `services/governance/policy_admin.py`，路由在 `api/governance.py`（与 be-06 共用前缀，分文件挂载）。

## 2. 模块结构

```
backend/
├── engines/policy/
│   ├── __init__.py     # 只导出 decide / ToolIntent / ResourceScope / PolicyDecision / BudgetDefaults（待建）
│   ├── models.py       # DTO：ToolIntent / ResourceScope / ResourceRef / SensitiveColumn / BudgetRequest / RuleSpec / RuleHit / PolicyDecision（待建）
│   ├── rules.py        # 内置基线规则表（P0–P3 规则函数）+ 条件 DSL 求值器 + 特征向量构建（待建）
│   └── pdp.py          # decide()：构建特征 → 收集命中 → most_severe → 组装 PolicyDecision（待建）
├── services/governance/
│   └── policy_admin.py # 规则 CRUD、策略版本发布/审批/回滚、预检（待建）
├── api/
│   └── governance.py   # /api/v1/governance/policies*（待建，仅校验 + 权限 + 转发）
└── tests/
    └── test_policy_import_graph.py  # 纯函数层 import 图与时间依赖断言（待建，见 §6.7）
```

`models/governance.py` 中的 `PolicyRule` / `RiskPolicyVersion` **已存在**，本模块只使用不修改；新增列的需求写进 §10。

## 3. 对外接口

### 3.1 HTTP

**路径以 `frontend/src/api/governance.ts` 为准**（前端已实现、已联调过的是事实契约）。
本模块原设计（`/governance/policies*` 的提案/审批/回滚）与前端实际形态差异较大，
逐条记在下表；**本模块本轮只落地 `preview` 一个接口**，其余归治理模块（见 §10.6）。

| 方法 | 路径 | 权限 | 幂等 | 本轮 | 要点 |
|---|---|---|---|---|---|
| POST | `/api/v1/governance/policies/preview` | 本项目任一职责槽 | 否（只读） | ✅ | 试算：传 `ToolIntent` + 资源范围 + `env` + `level`，返回 `PolicyDecision`；**不落库、不入账**，供审批人预检与 Benchmark 复用 |
| GET | `/api/v1/governance/policy-rules` | auditor / checker_security | 是 | ❌ M4 | 规则列表：`project_id` / `stage` / `risk_level`(CSV) / `enabled` / `q` / 分页 |
| POST | `/api/v1/governance/policy-rules` | checker_security | `Idempotency-Key` 必填 | ❌ M4 | 新建规则（草稿） |
| PUT | `/api/v1/governance/policy-rules/{id}` | checker_security | `Idempotency-Key` 必填 | ❌ M4 | 修改规则 |
| DELETE | `/api/v1/governance/policy-rules/{id}` | checker_security | `Idempotency-Key` 必填 | ❌ M4 | 删除规则 |
| POST | `/api/v1/governance/policy-rules/{id}/toggle` | checker_security | `Idempotency-Key` 必填 | ❌ M4 | 启停（**基线规则必须被服务端拒绝**，见 §6.6） |
| GET | `/api/v1/governance/policy-versions` | auditor | 是 | ❌ M4 | 版本列表（分页） |
| GET | `/api/v1/governance/policy-versions/diff` | auditor | 是 | ❌ M4 | `project_id` + `version_id` → 与上一版的差异 |
| POST | `/api/v1/governance/policy-versions` | checker_security（提案人） | `Idempotency-Key` 必填 | ❌ M4 | `{project_id, note}` → **提交审批**，不是「提交即生效」 |
| GET / PUT | `/api/v1/governance/budget-defaults` | checker_security | 是 / `Idempotency-Key` | ❌ M4 | 预算默认值；写入口一开，`Settings.SUP_MAX_*` 就从唯一来源降级为「初始值」 |

#### 与本文档原设计的差异（对账结论）

| # | 原设计 | 前端实际 | 处理 |
|---|---|---|---|
| 1 | `/governance/policies`（GET 返回生效规则集） | `/governance/policy-rules`（GET 分页 + `q` 搜索） | 以 `/policy-rules` 为准；它是**可编辑的规则库**，不是「当前生效集」快照 |
| 2 | 提案 → 另一人审批 → 生效（三接口） | `policy-versions` 只需 `POST {project_id, note}` 提交，状态字段 `pending/approved/rejected` | 状态机搬到版本表（见缺口 #4），提案与审批的**四眼约束仍由服务端兜底** |
| 3 | `condition` 是嵌套 DSL（§6.2） | `PolicyCondition` 是**扁平命名键对象**（`{env?, writes_formal_table?, …}`，未选中的键不写入） | 两种形式都支持：**DB 存扁平、引擎求值前编译成 DSL**（§6.2） |
| 4 | `policy_rule` 全局表，无 `tenant_id`、无 `stage` | 列表按 `project_id` + `stage` 过滤 | 🔴 缺列：`policy_rule` 需要 `stage`（生成/准备/执行），`risk_policy_version` 需要 `status`/`submitted_by`/`submitted_at`/`reject_comment`、`effective_scope` 需为 `{project_ids, envs}` 结构。进字典 + 迁移后再实现（§10.6） |
| 5 | 规则行 `updated_by` 只有 id | `PolicyRuleRead` 要 `updated_by_name` / `updated_at` | 列表 join `app_user` 取名（`updated_at` 来自 `TimestampMixin`），无需加列 |
| 6 | 无 `toggle` 接口 | `POST /{id}/toggle` | 接受，但**基线规则一律拒绝**（`p0.`/`p3.` 前缀），拒绝理由回给前端——只置灰按钮等于没拦 |

`preview` 只读、不需审批；M4 的写接口一律要求 `Idempotency-Key`（落 `idempotency_key` 表）。

### 3.2 Python（跨模块契约）

```python
# engines/policy/models.py —— 下列 DTO 一律 model_config = ConfigDict(frozen=True, extra="forbid")（逐类省略）
class ToolIntent(BaseModel):
    kind: Literal["etl_write", "readonly_probe", "export", "admin"]
    is_write: bool                       # 是否产生目标端写入；P2 兜底与 P1 回滚规则都用它
    tool: ToolName | None                # 已知工具时可填；生成阶段（尚无工具）为 None
    description_summary: str             # 需求摘要，**已脱敏**，仅用于理由文本与人工复核
    bypass_approval: bool = False; disable_audit: bool = False
    export_plaintext_sensitive: bool = False; cross_tenant: bool = False
    has_rollback_plan: bool = False      # 仅对 is_write 有意义
    budget: BudgetRequest | None = None
class ResourceRef(BaseModel):
    data_source_id: int | None; data_source_type: DataSourceType; database: str
    schema_: str = Field(default="", alias="schema"); table: str; env: Env
class SensitiveColumn(BaseModel):
    name: str; level: SensitiveLevel
    kind: Literal["id_card", "phone", "email", "address", "token", "bank_card", "other"] | None = None
    contains_plaintext: bool             # 计划写入/导出的值未脱敏
    masked: bool                         # 计划做了掩码/哈希/截断
    egress: bool                         # 值离开控制面边界（写非受控目标 / 导出 / 进 LLM 上下文）
class ResourceScope(BaseModel):
    tenant_id: int; project_id: int
    reads: tuple[ResourceRef, ...] = (); writes: tuple[ResourceRef, ...] = ()
    authorized_databases: frozenset[str] = frozenset()   # 项目授权范围（来自 project 侧白名单）
    out_of_scope: tuple[ResourceRef, ...] = ()           # 跨库/跨 schema，超出授权
    cross_tenant_refs: tuple[ResourceRef, ...] = ()
    unregistered_secret_refs: tuple[int, ...] = ()       # 引用了未登记的 secret_reference
    writes_formal_table: bool = False                    # 目标端为正式表（非影子表）
    writes_shadow_table: bool = False
    columns: tuple[SensitiveColumn, ...] = ()            # 涉及字段的分级与脱敏计划
class BudgetRequest(BaseModel):
    max_read_rows: int; max_write_bytes: int; max_duration_sec: int
    max_amplification_ratio: Decimal; max_reject_rate: Decimal
class BudgetDefaults(BaseModel):
    max_read_rows: int; max_write_bytes: int; max_duration_sec: int
    max_amplification_ratio: Decimal; max_reject_rate: Decimal
    warn_threshold_pct: int = 80
class RuleHit(BaseModel):
    code: str; level: RiskLevel; title: str
    evidence: dict[str, str]             # 命中的特征名 → 值的字符串形式（全部非敏感）
class PolicyDecision(BaseModel):
    level: RiskLevel
    blocked: bool                        # level.must_block
    required_slots: tuple[ApprovalSlot, ...]   # P0/P3 为空；P1/P2 为两槽
    hardening_required: bool             # level.requires_hardening and not blocked
    budget: BudgetRequest                # 合并后的生效预算
    hits: tuple[RuleHit, ...]            # 按 (level.rank, code) 升序，稳定输出
    reasons: tuple[str, ...]             # 人类可读理由，顺序与 hits 一致
    policy_version: int                  # 判定所用策略版本
    evaluated_at: datetime               # 由调用方注入的 now

# engines/policy/pdp.py
def decide(
    intent: ToolIntent, scope: ResourceScope, env: Env, level: SensitiveLevel, *,
    now: datetime,                                       # 时间由调用方注入，PDP 内禁取当前时间
    rules: Sequence[RuleSpec] | None = None,             # None → 内置基线规则集（含 P0）
    defaults: BudgetDefaults | None = None,              # None → 由 service 从 Settings 构造
    project_overrides: Mapping[str, str] | None = None,  # 项目级预算覆盖（逐维度）
    policy_version: int = 0,                             # 判定所用策略集版本；0 = 内置基线
) -> PolicyDecision: ...

# engines/policy/rules.py
BUILTIN_RULES: tuple[RuleSpec, ...]            # 内置安全基线（P0 不可关闭 + P1/P2/P3 默认）
def build_features(intent, scope, env, level, *, defaults, project_overrides) -> dict[str, str]: ...
def evaluate_condition(condition: Mapping[str, Any], features: Mapping[str, str]) -> bool: ...
```

**契约**：`decide()` 无副作用、无异常路径（输入由 Pydantic 强校验，非法输入在构造 DTO 时即失败）。同输入必同输出。`blocked=True` 时调用方**必须**拒绝（be-11 → `GATE_BLOCKED`，be-13 → `POLICY_BLOCKED`），且必须写 `POLICY_BLOCKED` 证据事件。

## 4. 数据模型

| 表 | 读写 | 关键列 | 约束 |
|---|---|---|---|
| `policy_rule` | 读（service 载入后传入 PDP）/ 写（策略提案） | `code`、`version`、`level`、`title`、`description`、`condition`(jsonb)、`required_slots`(jsonb)、`enabled`、`updated_by` | `uq_policy_rule_code_version`；**无 `tenant_id`**（全局表，见 §10.1） |
| `risk_policy_version` | 写（提案/审批/回滚）/ 读（查当前生效版本） | `version`、`diff`(jsonb)、`approved_by`、`effective_scope`(jsonb)、`effective_at` | `uq_risk_policy_version_tenant_id_version`；**无 status 列**——「草案」与「生效」由 `approved_by IS NULL AND effective_at IS NULL` 与两者均非空表达 |
| `preparation` | 只读（Commit 重校验时） | `risk_level`、`required_slots`、`frozen_at`、`artifact_sha256` | 冻结事实，只允许改 `status`（`__mutable_columns__`） |
| `approval_decision` | 只读 | `slot`、`decision`、`approver_id`、`artifact_sha256` | 只读；PDP 不写审批表 |

表定义见 [核心数据字典 §8](../核心数据字典.md)。PDP **不查表**：`policy_rule` 与 `risk_policy_version` 的读取与合并由 `services/governance/policy_admin.py` 完成，再以 `Sequence[RuleSpec]` + `policy_version` 传入。

## 5. 核心流程

### 5.1 三个调用点

```
① 门禁阶段（be-11，M2）：语法/Schema 通过后调 decide(..., now=utcnow())  → blocked → GATE_BLOCKED
② Prepare 阶段（be-13，M3）：Prepare 冻结前调 decide() → 结果落 preparation.risk_level / required_slots
                             → blocked=True 时直接 POLICY_BLOCKED，不创建 Preparation
③ Commit 阶段（be-13，M3）：重算 decide()，按 §5.3 的「冻结优先 + P0 单向加严」规则处理
```

### 5.2 策略变更审批流

| 状态 | 事件 | 新状态 | 非法转移处理 |
|---|---|---|---|
| 草案（`approved_by IS NULL` 且 `effective_at IS NULL`） | 另一名 checker_security 审批通过 | 生效（两列均非空） | 提案人自批 → `SELF_APPROVAL_FORBIDDEN`，写 `APPROVAL_SELF_DENIED` |
| 草案 | 驳回 | 保持草案，`diff` 追加 `rejected_by`/`reason` | 对已生效版本重复审批 → `CONFLICT` |
| 生效 | 回滚 | **产生新版本**（草案 → 审批 → 生效） | 对生效版本直接 UPDATE `diff` → 由 `__immutable__` 语义之外的应用层禁止（`RiskPolicyVersion` 非 `__immutable__`，故必须在 service 层显式断言，见 §10.4） |

规则行本身（`policy_rule`）不置 `__immutable__`（它带 `updated_at`），变更方式是**新增 `version` 行**并 `enabled=False` 旧行；`effective_scope` 记录这批变更作用的项目集合。

### 5.3 Commit 时的「冻结优先 + P0 单向加严」

1. 以冻结时写入 `preparation.risk_level` / `required_slots` 为准校验审批完整性——**不因策略放松而降低已冻结的审批要求**，也不因策略收紧而要求重新审批。
2. 用当前生效策略重算一次：若重算结果为 `P0`（`blocked=True`），则拒绝 Commit（`POLICY_BLOCKED`）并要求重新 Prepare。理由：P0 是「不得执行」的安全红线，不是「审批要求」；策略新增的 P0 条件必须是硬约束。
3. 重算结果比冻结等级更严（`rank` 更小）但未到 P0：不阻断，写 `POLICY_EVALUATED` 事件（`payload` 含 `frozen_level` 与 `current_level`），供安全审批人复查。

## 6. 关键算法与规则

### 6.1 特征向量（唯一可判定输入面）

规则的条件只能引用下表中的特征；不在表内的输入**不允许**参与判定（避免规则作者引用未定义字段而静默恒假）。

| 特征名 | 类型 | 来源 |
|---|---|---|
| `env` / `level` | `Env` / `SensitiveLevel` 值字符串 | 参数 `env`、`level`（本次涉及的最高敏感等级） |
| `intent.kind` / `intent.is_write` / `intent.tool` | str / bool | `ToolIntent` |
| `intent.bypass_approval` / `intent.disable_audit` / `intent.export_plaintext_sensitive` / `intent.cross_tenant` / `intent.has_rollback_plan` | bool | `ToolIntent` |
| `scope.writes_formal_table` / `scope.writes_shadow_table` | bool | `ResourceScope` |
| `scope.out_of_scope_count` / `scope.cross_tenant_count` / `scope.unregistered_secret_count` | int | `ResourceScope` 各元组长度 |
| `scope.sensitive_plaintext_egress_count` / `scope.read_table_count` / `scope.write_table_count` | int | §6.3 P0-2 判定结果、元组长度 |
| `budget.over_default_ratio_max` | Decimal 的字符串形式 | §6.5 |
| `budget.*`（五维） | int / Decimal 字符串 | 生效预算 |

### 6.2 条件 DSL（`policy_rule.condition`）

**两种形式，一个转换点**（`engines/policy/condition.py`）：

| 形式 | 长什么样 | 谁用 |
|---|---|---|
| **扁平**（存储/线上契约） | `{"env": ["prod"], "writes_formal_table": true}`，键取自 `PolicyCondition`；**未选中的键不写入**（不是写 `false`） | `policy_rule.condition` 列内容、`policy-rules` 接口的收发 |
| **规范 DSL**（引擎求值） | `{"all": [{"feature": "env", "op": "in", "value": ["prod"]}, {"feature": "scope.writes_formal_table", "op": "is_true"}]}` | `BUILTIN_RULES`（代码里直接写）、`evaluate_condition` / `decide` 的输入 |

- `normalize_condition()` 按**键的形状**判别（出现 `all`/`any`/`not`/`feature`/`op`/`value` 即视为 DSL），
  扁平形式编译成「若干叶子的 AND」。`RuleSpec` 构造时自动规整并 `validate_condition`，
  于是「DB 行是否已编译」不需要每个调用点自己记得。
- **显式 `false` ≠ 不写**：不写 = 不约束；写 `false` 是一条**反向条件**，
  编译成真补集（如 `missing_rollback_plan: false` → `any[is_write is_false, has_rollback_plan is_true]`，
  而不是简单翻转一个叶子——后者对只读意图恒假）。
- **不提供 DSL → 扁平的反向转换**：它不是满射，遇到 `any` 会静默丢掉语义，
  而丢掉的那部分正是宽严所在。读取接口原样下发存储内容。
- 组合子：`all` / `any`（数组）、`not`（单条件）；嵌套深度上限 8。
- 算子：`eq` / `ne` / `in` / `not_in` / `gt` / `gte` / `lt` / `lte` / `exists` / `is_true` / `is_false` / `is_empty`（集合/字符串为空）/ `len_gte`。
- 数值比较一律先转 `Decimal`；**禁止**浮点直接比较。
- 未定义特征名 → `evaluate_condition` / `validate_condition` 抛 `ValueError`（不是静默 false）。
  校验点**在 `RuleSpec` 构造处**：坏条件必须停在发布/载入阶段，
  而不是等生产流量第一次执行到它。载入期的坏行降级为 `RuleMergeWarning` 并入账（§6.6）。

### 6.3 P0–P3 判定矩阵（逐条转成规则函数，与《需求对齐记录》§四一致）

内置基线规则以 `code` 标识，`code` 前缀 `p0.` 与 `p3.` 为**不可关闭**集合（§6.6）。

**P0（任一命中 → `blocked=True`，不进审批、不进执行，写账本）**

| `code` | 条件表达式（对特征判定） | 规则函数签名 |
|---|---|---|
| `p0.prod_formal_write` | `env == "prod" and scope.writes_formal_table` | `def p0_prod_formal_write(f: Features) -> RuleHit \| None` |
| `p0.sensitive_plaintext_egress` | `any(col.level_rank >= CONFIDENTIAL and col.contains_plaintext and col.egress and not col.masked for col in scope.columns)` —— 覆盖身份证/手机号/邮箱/地址/Token 等；当 `col.kind in {"id_card","phone","email","address","token"}` 时**即使 `level` 为 `internal` 也命中** | `def p0_sensitive_plaintext_egress(...)` |
| `p0.bypass_intent` | `intent.bypass_approval or intent.disable_audit or intent.export_plaintext_sensitive` | `def p0_bypass_intent(...)` |
| `p0.cross_tenant` | `intent.cross_tenant or scope.cross_tenant_count > 0` | `def p0_cross_tenant(...)` |

> `level_rank` 是 `rules.py` 内的局部序 `{public:0, internal:1, confidential:2, restricted:3}`。**不得**用字符串比较——`SensitiveLevel` 的两个值在字典序下与敏感度无关（与 `RiskLevel.rank` 的注释同理）。

**P1（允许进入审批，强制双槽 + 加固提示）**

| `code` | 条件表达式 | 备注 |
|---|---|---|
| `p1.out_of_scope_resource` | `scope.out_of_scope_count > 0`（跨库/跨 schema） | 资源范围超出项目授权 |
| `p1.budget_over_3x` | `budget.over_default_ratio_max > 3` | 五个预算维度任一超过默认阈值 3 倍 |
| `p1.no_rollback_plan` | `intent.is_write and not intent.has_rollback_plan` | **限写入意图**：只读探查无需回滚方案 |
| `p1.unregistered_secret` | `scope.unregistered_secret_count > 0` | 引用未登记的 `secret_reference` |
| `p1.prod_shadow_only` | `env == "prod" and scope.writes_shadow_table and not scope.writes_formal_table` | 生产环境仅写影子表 |

**P2（常规双槽；含兜底）**

| `code` | 条件表达式 | 备注 |
|---|---|---|
| `p2.in_scope_with_rollback` | `intent.kind != "readonly_probe" and env != "prod" and scope.out_of_scope_count == 0 and budget.over_default_ratio_max <= 1 and (not intent.is_write or intent.has_rollback_plan)` | 正条件全满足 |
| `p2.default_write` / `p2.default_read` | `not any_higher_rule_hit`（写入意图 / 非写入意图两个 code） | 兜底，保证写入意图**永远不会**落到 P3；`code` 仅供展示，实现为 `decide()` 内部兜底分支（见 §10.5） |

> 🔴 **`intent.kind != "readonly_probe"` 那个叶子是必须的**（本轮实现时发现的设计缺陷）：
> 没有它，这条规则对「非写入 + 范围干净 + 预算不超」的**只读探查**也成立，
> 于是 `most_severe(P2, P3) = P2`，**`p3.readonly_probe` 在非 prod 环境下永远不可达**——
> 一次开发环境的连通性测试也会被要求两个审批槽。
> 在 prod 下 `env != "prod"` 为假，所以这个缺陷只在非 prod 暴露。

**P3（免审批，仅记审计事件）**

| `code` | 条件表达式 | 备注 |
|---|---|---|
| `p3.readonly_probe` | `intent.kind == "readonly_probe" and not intent.is_write and intent.tool in {"datasource.connectivity_test", "metadata.probe"}` | 纯只读探查；生产环境的元数据探查同样适用（`p0.prod_formal_write` 要求 `writes_formal_table`，不冲突） |

> **兜底不能替代自证**：调用方自报 `kind="readonly_probe"` 但 `tool` 不在白名单内时，
> 命中的是 `p2.default_read` 而不是 P3——「免审批」的凭据是**工具在注册表里的声明**，
> 不是调用方在意图里写的那个字符串。

### 6.4 命中顺序与短路

```
1. features = build_features(intent, scope, env, level, defaults=…, project_overrides=…)
2. hits = [r.hit for r in rules if r.enabled and evaluate_condition(r.condition, features)]
   —— 全量收集，不在第一个命中处短路（证据要能解释「命中了哪几条」）
3. level = RiskLevel.most_severe(*[h.level for h in hits]) if hits else RiskLevel.P2
   —— most_severe 取最严（语义：越大越危险，见 RiskLevel 定义）
4. blocked = level.must_block                    # 仅 P0
5. required_slots = (ApprovalSlot.CHECKER_DATA, ApprovalSlot.CHECKER_SECURITY) \
                    if not blocked and level.min_approval_slots == 2 else ()
6. hardening_required = level.requires_hardening and not blocked
7. hits / reasons 按 (level.rank, code) 升序排序后输出
```

🔴 **第 7 步的预算算式修正为「逐维度 `min(申报值, 上限)`」**（原文写的是 `defaults ⊕ project_overrides`）：
上限 = `defaults` 被 `project_overrides` 逐维度覆盖后的结果；**申报值（`intent.budget`）与上限取 min**。
理由是这份「生效预算」会被 be-16 的运行监督拿去硬中断：把申报值抬到上限，
等于**放宽了用户自己设的那道闸**（用户申报 100 万行，监督却允许 1 亿行）；
取 max 则相反，会放宽策略红线，那是安全问题。申报缺失（`None`）退化为上限。

`blocked=True` 时 `required_slots` 强制为空并提前返回（不计算加固项）——P0 不进审批，槽位数无意义。

### 6.5 预算默认值与项目级覆盖

- 默认值来源：`Settings.SUP_MAX_READ_ROWS` / `SUP_MAX_WRITE_BYTES` / `SUP_MAX_DURATION_SEC` / `SUP_MAX_AMPLIFICATION_RATIO` / `SUP_MAX_REJECT_RATE` / `SUP_WARN_THRESHOLD_PCT`（`core/config.py` 既有字段）。由 service 构造 `BudgetDefaults` 后注入，`engines/` 不读 settings（会引入配置 IO）。
- 项目级覆盖：`project_overrides` 为「维度名 → 字符串值」的映射，按维度**逐项**覆盖，未覆盖的维度保持默认（不做整体替换，避免漏写一个维度就变 0）。
- `over_default_ratio_max = max(申报值 / 默认值)`，比率维度按 `Decimal` 计算；申报缺失时视为 `1.0`（不产生超限）。
- 演示场景「脏数据超限」的 20% 拒绝率由 `quality_contract.reject_rate_budget` 承载（be-11 / be-17），**不是** PDP 的预算职责；PDP 只判断「申报拒绝率是否超过默认 5% 的 3 倍」。

### 6.6 规则版本化与不可关闭的基线

- **两级结构**：`risk_policy_version` 是"一批变更的审批与生效记录"（租户级），`policy_rule` 是"规则行"（全局表）。生效策略 = 当前生效 `risk_policy_version.version` 对应的一组 `policy_rule.version` 行。
- **基线不可关闭**：`code` 以 `p0.` / `p3.` 开头的规则，若数据库里出现 `enabled=False` 或 `level` 被下调，service 在载入时**忽略该覆盖**并写告警证据事件。理由：P0/P1 拦截率 100% 是验收指标（[PRD §1.4](../PRD.md)），能被配置关闭就等于不成立。加严（新增条件、新增更高等级规则）允许。
- **不追溯**：`Preparation` 冻结时的 `risk_level` / `required_slots` 不再变化；策略变更只对**新** Preparation 生效，例外见 §5.3 第 2 条（P0 单向加严）。

### 6.7 确定性保证与 CI 强制检查

| 保证 | 做法 |
|---|---|
| 同输入必同输出 | 无随机、无缓存、无全局可变状态；`PolicyDecision` 为 frozen 模型 |
| 无时间依赖 | `now` 由调用方注入；`engines/policy/` 内禁止 `datetime.now(` / `datetime.utcnow(` / `time.time(` |
| 无字典序依赖 | 特征字典按 key 排序后再用于 `all`/`any` 求值；`hits`/`reasons` 显式排序 |
| 无数值歧义 | 比率用 `Decimal`；参与比较的整数字段不做隐式 float 转换 |
| 无环境依赖 | 不读环境变量、locale、文件、`sys.argv`；无 `os` / `pathlib` import |
| 顺序无关 | 打乱 `rules` 顺序输出不变（`hits` 排序保证） |

CI 检查（`backend/tests/test_policy_import_graph.py`，随 be-22 进流水线）：

1. **AST import 白名单**：解析 `engines/policy/**/*.py` 的全部 `Import`/`ImportFrom`，断言顶层模块名 ∈ `{pydantic, dataclasses, datetime, decimal, typing, collections, collections.abc, enum, math, itertools, functools, engines}`；命中禁用集合（`fastapi`、`sqlalchemy`、`redis`、`httpx`、`requests`、`celery`、`asyncpg`、`openai`、`anthropic`、`langchain*`、`socket`、`urllib`、`subprocess`、`os`、`pathlib`、`core`、`clients`、`services`、`harness`、`models`）即失败。
   **唯一豁免**：`models.enums`（纯值定义、无 IO）。它虽在执行 `models/__init__.py` 时会带出 `sqlalchemy`，
   但那是「为 Alembic 注册全部模型」这一既有设计的连带后果。把枚举复制一份到 `engines/`
   才是真正的危险——两份词表一旦分叉，`kind` 拼写不同就会漏判 P0。
2. **运行期交叉验证（改增量断言）**：原文的 `assert 'sqlalchemy' not in sys.modules` 与豁免 #1 冲突
   （导入 `models.enums` 必然带出 sqlalchemy）。改为**增量**检查：分别记录
   「import `models.enums` 之后」与「import `engines.policy.pdp` 之后」的模块集合，
   断言**差集为空**。这样仍然能抓住任何真正的间接 IO 依赖，
   又不会因为既有的 sqlalchemy 基线而假红。实现见 `backend/tests/test_policy_import_graph.py`。
3. **时间依赖**：AST 扫描 `engines/policy/` 中不得出现 `datetime.now(`、`datetime.utcnow(`、`time.time(`、`date.today(`。
4. **无 IO**：AST 扫描 `engines/policy/` 中不得出现 `open(`。
5. `mypy --strict`（`pyproject.toml` 的 `files` 已含 `engines`）零错误。

## 7. 错误码与拒绝语义

| # | 触发条件 | 错误码 | HTTP | 写账本 |
|---|---|---|---|---|
| 1 | 门禁阶段命中 P0/P1 | `GATE_BLOCKED` | 409 | 是（`PIPELINE_GATE_EVALUATED`） |
| 2 | Prepare 阶段命中 P0 | `POLICY_BLOCKED` | 403 | 是（`POLICY_BLOCKED`） |
| 3 | Commit 重校验发现当前策略下为 P0（§5.3） | `POLICY_BLOCKED` | 403 | 是（`POLICY_BLOCKED`） |
| 4 | 非 checker_security 试图提交/审批策略变更 | `ROLE_REQUIRED` | 403 | 是（由 be-02 写权限拒绝事件） |
| 5 | 提案人自批策略变更 | `SELF_APPROVAL_FORBIDDEN` | 403 | 是（`APPROVAL_SELF_DENIED`） |
| 6 | 策略变更未经审批即被用于判定（`approved_by IS NULL`） | `FORBIDDEN` | 403 | 是 |
| 7 | 重复审批同一已生效版本 | `CONFLICT` | 409 | 否（幂等重放，非协议拒绝） |
| 8 | 回滚到不存在的版本 | `NOT_FOUND` | 404 | 否 |
| 9 | 规则的 `condition` 语法非法 / 引用未定义特征 | `VALIDATION_ERROR` | 422 | 否 |
| 10 | 新增 `risk_policy_version.version` 与既有冲突 | `CONFLICT` | 409 | 否 |
| 11 | 提示性命中（未到 P0、等级未变严） | 不报错 | 200 | 是（`POLICY_EVALUATED`） |

写入的事件类型只取 `EvidenceEventType` 既有成员：`POLICY_EVALUATED`、`POLICY_BLOCKED`、`PIPELINE_GATE_EVALUATED`、`APPROVAL_SELF_DENIED`。**不新增事件类型、不新增错误码**。

## 8. 安全与合规约束

1. **判定确定性 = 安全前提**：LLM 参与即无法证明「P0/P1 拦截率 100%」。§6.7 的 CI 检查是这条红线的机器化表达，不是文档承诺。
2. **不可关闭**：P0 基线与 P3 定义不可被配置禁用或降级（§6.6）。P1/P2 的阈值可调，调整本身要过审批流。
3. **审批权限**：策略变更只允许 `checker_security` 提交与审批，且**审批人 ≠ 提案人**（四眼在策略自身的变更上也适用）。权限判定查 `role_assignment`，不用 JWT 角色声明。
4. **不可变制品**：PDP 不写任何不可变表；`preparation.risk_level` / `required_slots` 一经冻结不可改（只允许改 `status`）。
5. **租户隔离**：`ResourceScope.tenant_id` / `project_id` 是必填字段；跨租户资源命中 `p0.cross_tenant`（既是安全拦截也是隔离校验）。
6. **无敏感值进判定**：特征向量只接受枚举、布尔、计数与预算（全部非敏感）；`description_summary` 只用于理由文本且必须已脱敏，**不参与**任何条件判定。
7. **证据**：每次判定（含未阻断）都写 `POLICY_EVALUATED`（payload 含 `policy_version`/`level`/`hits[].code`），阻断写 `POLICY_BLOCKED`。两者都不含字段值与样本。

## 9. 测试要点

**单元测试（纯函数，无 IO，可直接在本机跑）**
1. **P0 穷举（拦截率 100% 的证明方式）**：对 7 个 P0 相关布尔/计数特征（`writes_formal_table`、`cross_tenant`、`bypass_approval`、`disable_audit`、`export_plaintext_sensitive`、`sensitive_plaintext_egress_count>0`、`env==prod` 与 `writes_formal_table` 的组合）做**全笛卡尔积**枚举，凡含任一 P0 触发条件的组合断言 `blocked is True` 且 `required_slots == ()`。这把「100%」从抽样变成特征维度上的穷举。
2. **P1 至少一命中即 P1**：构造 5 条 P1 规则各自的触发输入，断言 `level is RiskLevel.P1` 且 `required_slots` 为两槽、`hardening_required is True`。
3. **P3 只读**：`metadata.probe` + `readonly_probe` + 无写 → `level is RiskLevel.P3`、`required_slots == ()`、`blocked is False`。
4. **兜底不漏**：写入意图在全部 P0/P1 均未命中时必须为 P2（构造一个"干净"写意图，断言不是 P3）。
5. **单调性（反例保护）**：对任意基线输入，逐项把某个触发特征置为触发态，断言等级**不变得更松**（`rank` 不增大）。
6. **确定性与顺序无关**：同输入调用 100 次输出完全一致（`model_dump()` 相等）；`rules` 打乱后 `hits` 顺序与内容不变。
7. **边界反例**：只读探查无回滚方案 → **不**命中 `p1.no_rollback_plan`（写入才要求回滚）；预算恰好等于 3 倍 → **不**命中（`>` 不是 `>=`）；`level_rank` 边界（`internal` vs `confidential`）。
8. **敏感分级反例**：`internal` + `email` 明文出域 → 命中 `p0.sensitive_plaintext_egress`（`kind` 白名单生效）；`confidential` + `masked=True` → **不**命中。
9. **条件 DSL**：15 个算子各一条正反例；未定义特征名 → `ValueError`；嵌套超深 → `ValueError`。
10. **基线不可关闭**：把 `p0.prod_formal_write` 的 `enabled` 置 `False` 后仍命中 P0。
11. **不追溯**：以旧策略冻结的准备单（`risk_level=P2`、两槽）在当前策略升为 P1 后，`required_slots` 不变；仅当重算为 P0 时 Commit 被拒。
12. **import 图与时间依赖**：执行 §6.7 的 5 项检查（尤其是独立解释器的 `sys.modules` 断言）。

**集成测试（真实 PG/Redis，标 `@pytest.mark.integration`）**
13. **策略变更审批流与回滚**：提案（草案 `approved_by IS NULL`）→ 自批被拒（`SELF_APPROVAL_FORBIDDEN` + 账本事件）→ 他人审批 → 生效（生效前判定仍用旧版本）；回滚生成新版本而非改动历史行，两个版本的 `diff` 都可查。
14. **Benchmark L0 对接**：`benchmark_case.category="malicious_p0"`、`is_malicious=True` 的用例逐条跑：`expected` 声明 `{"risk_level":"P0","blocked":true,"error_code":"POLICY_BLOCKED"}`，统计 `p0p1_block_rate` 必须为 `1.0`（[PRD §13.6](../PRD.md)）。误报率用 `category` 非恶意用例的 `blocked=True` 比例统计。

## 10. 未决与风险

1. **`policy_rule` 缺 `tenant_id`**：该表是全局表（`models/governance.py` 未继承 `TenantMixin`），而 `risk_policy_version` 是租户级。多租户下会出现「A 租户审批的策略变更直接影响 B 租户」。当前按「单租户部署 + 全局安全基线」理解。建议：给 `policy_rule` 加 `tenant_id` 并把基线规则下沉为代码常量（DB 只存覆盖）。验证方式：M4 演示双租户后决定。
2. **项目级预算覆盖无独立载体**：目前只能塞进 `risk_policy_version.effective_scope`（jsonb）。建议新增 `project_supervision_default` 表（按 `project_id` 存五维预算）。验证方式：M3 Prepare 阶段接入时评估。
3. **`SensitiveColumn.kind` 的词表待与 be-04 对齐**：`id_card` / `phone` / `email` / `address` / `token` / `bank_card` 这组取值需与 be-04 的 `metadata_profile.sampling.masking_policy` 及字段分级词表统一，否则「身份证明文出域」的判定会漏。验证方式：be-04 文档定稿后做一次词表比对，必要时把 `kind` 改为引用 be-04 的类型枚举。
4. **`RiskPolicyVersion` 无不可变保护**：`models/governance.py` 里该模型未置 `__immutable__`（带 `updated_at`）。因此「已生效版本的 `diff` 不可改」只能靠 service 层断言。建议改为 `__immutable__ = True` + `__mutable_columns__ = {"approved_by", "effective_at"}`（需 migration 与 ORM 改动）。
5. **`p2.default_write` 的表达方式**：DSL 里没有「其他规则均未命中」这种否定式聚合（也不该有）。实现上它是 `decide()` 内部的兜底分支而非一条 `policy_rule` 行——因此它在 DB 里**不可见、不可配**。好处是绝不会被误关，坏处是审批人看不到这条规则。需确认是否要在 `policy_rule` 里插一条 `enabled=True` 的说明性行（不参与求值，仅展示）。
6. **Commit 时「P0 单向加严」需产品确认**：§5.3 第 2 条会让「审批完成后策略新增 P0 条件」导致 Commit 失败并需重新 Prepare。这是安全优先的取舍，但与「策略变更不追溯」存在张力。需 owner 拍板一次。
7. **`description_summary` 的脱敏责任方**：PDP 要求它已脱敏，但 PDP 自己不做脱敏（engines 层无脱敏依赖）。需在 be-09/be-13 的调用点显式经脱敏后再传入，否则会把用户原始需求写进证据事件。
8. **`p2.in_scope_with_rollback` 与 P3 的重叠**（本轮实现时发现并已修）：见 §6.3 的修正说明。留下一问：`p3.readonly_probe` 的 `tool` 白名单与 `harness/tools.py` 的工具注册表是**两份**声明（前者在 `engines/policy/rules.py`，后者在 `harness/`）。`engines/` 不能 import `harness/`（单向依赖），所以两份必然存在。建议：`harness/tools.py` 里给只读探查类工具打 `risk=P3` 标记，由 be-11 在构造 `ToolIntent` 时**从注册表读**，`kind` 字段不允许调用方自报——这样白名单就只有一份。
9. **策略变更的留痕粒度**：`decide_for_project()` 每次载入发现越权覆盖都会写一条 `POLICY_EVALUATED` 证据。若一行「被拒的降级覆盖」长期留在库里，每次门禁/准备都会重复入账，账本会被同一条事实刷满。建议按 `(policy_version, warning_code)` 去重（Redis `SETNX` + 长 TTL），或改为「在策略**提案/审批**时入账一次」。
10. **`policy_rule` 的 `stage` 与版本状态列缺失**：见 §3.1 对账表第 4 行，落地前需先改字典与迁移。

## 11. 变更记录

| 日期 | 变更 | 原因 |
|---|---|---|
| 2026-10-16 | 初稿：`decide()` 契约与 7 个 DTO、P0–P3 逐条规则映射（含 P1 回滚规则限写入意图、P2 兜底规则）、条件 DSL 与特征白名单、预算合并、基线不可关闭、`Commit` 冻结优先 + P0 单向加严、import 图与时间依赖的 CI 检查 | M2 设计落地，需与既有 `RiskLevel` / `SensitiveLevel` / `policy_rule` / `risk_policy_version` 对齐 |
| 2026-10-16 | 实现对账：§3.1 改为以前端 `governance.ts` 为准（新增 6 行差异表，本轮只落地 `preview`）；§6.2 补「扁平 ↔ 规范 DSL 两形式 + 唯一转换点」；§6.3 补 `p2.in_scope_with_rollback` 的 `kind != readonly_probe` 叶子（否则 P3 在非 prod 不可达）、兜底拆成 `p2.default_write`/`p2.default_read`；§6.4 第 7 步预算算式修正为逐维度 `min`；§6.7 的 import 豁免 `models.enums` 并把运行期断言改为增量；§10 新增 8–10 | be-07 落地时与前端契约、与既有 `models/` 结构的三处冲突（详见 `specs/M2-01-be-PDP风险策略引擎.md`） |
