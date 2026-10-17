# fe-04 Pipeline Studio

> 上游：[概要设计总纲.md](../概要设计总纲.md) §3 三层状态隔离 · §7 API 分组 · §10 与 PRD 章节的对应关系 ｜ [PRD.md](../PRD.md) §6.3 Pipeline Studio · §11.2 关键布局 · §12 关键异常处理
> 数据：[核心数据字典.md](../核心数据字典.md) §3 Pipeline 与制品 · §8 支撑表（`pipeline_conversation`）
> 代码位置：`frontend/src/views/pipeline/` ｜ 里程碑：M2 ｜ 依赖：fe-01 fe-03（后端依赖 be-09 be-10 be-11 be-12）

## 1. 职责与边界

- **做什么**
  1. 承载「一句话需求 → EtlPlan + HOCON + DAG + 字段映射」的**四区工作台**（PRD §11.2）。
  2. 呈现 LangGraph 中断产生的澄清问题并提交补充，驱动 `resume`；显示生成阶段状态指示。
  3. 展示 EtlPlan 结构化方案、HOCON 全文（高亮/折叠/只读）、DAG 拓扑、字段映射与 Schema Diff。
  4. 展示门禁结果（pass/warn/block）、逐条 finding 的**定位跳转**、有限自动修复的轮次与前后 Diff。
  5. 执行版本冻结（含前置校验与二次确认），冻结后整页转只读；提供「基于此版本生成新版本」。
  6. 提供提交审批入口（跳 fe-05 的 Prepare）。

- **不做什么**
  - **不做任何方案生成与修复**：EtlPlan、HOCON、字段映射、自动修复全部由后端 `engines/plan`、`engines/hocon`、`engines/autofix` 产出；前端**不可编辑** HOCON 与 Plan（本轮不提供手工改配置）。
  - **不判定门禁与风险**：`pass/warn/block`、`RiskLevel`、预算是否超限一律用后端结论；前端不做规则匹配，也不根据 LLM 输出推断风险。
  - **不计算指纹**：`pipeline_version.sha256` 由后端在冻结时按 `core/ids.canonical_json` 计算；前端只展示，绝不预计算或校验。
  - **不覆盖 Execution State**：运行指标、质量报告、日志在 fe-06；本页不展示运行明细（仅展示下游状态标签）。
  - **不直接调 SeaTunnel / 目标库**：无任何执行入口。
  - 不解析 LangGraph Checkpoint（Workflow State 仅服务端读写，前端只拿状态快照）。

- **所在分层**：`views/pipeline/` → `stores/pipeline.ts` → `api/pipeline.ts` → `api/client.ts`；重量级依赖（Vue Flow、CodeMirror）由本路由 `import()` 懒加载，不进首屏 chunk。

## 2. 模块结构

```
frontend/src/views/pipeline/
├── PipelineListView.vue            # Pipeline 列表与新建入口（待建）
├── PipelineStudioView.vue          # 四区布局容器 + 轮询编排 + 只读态判定（待建）
├── components/
│   ├── left/
│   │   ├── RequirementPanel.vue    # 源/目标选择 + 自然语言需求输入（待建）
│   │   └── ClarificationPanel.vue  # 澄清对话：问题表单 + 补充说明 + 历史（待建）
│   ├── center/
│   │   ├── PlanTab.vue             # EtlPlan 结构化视图（inputs/outputs/transforms/filters/quality_rules）（待建）
│   │   ├── HoconTab.vue            # CodeMirror 6 只读 + 行号 + 折叠 + finding 行内标记（待建）
│   │   ├── DagTab.vue              # Vue Flow 拓扑（待建）
│   │   ├── FieldMappingTab.vue     # 字段映射表 + Schema Diff（待建）
│   │   └── HoconDiffDialog.vue     # 版本/修复前后 side-by-side diff（待建）
│   ├── right/
│   │   ├── GatePanel.vue           # 门禁结论 + 按 severity 分组 + 点击定位（待建）
│   │   ├── RiskHintPanel.vue       # 风险提示（见 §10 第 6 条）（待建）
│   │   ├── QualityContractPanel.vue# 字段级规则表（待建）
│   │   └── BudgetPanel.vue         # 运行预算 + 预警阈值（待建）
│   ├── bottom/
│   │   ├── VersionBar.vue          # 版本信息 + SHA256 + 生成历史 + 冻结/提审按钮（待建）
│   │   └── FreezeConfirmDialog.vue # 冻结二次确认：制品清单 + 各指纹（待建）
│   └── StudioPhaseTag.vue          # 解析中/等待澄清/生成中/门禁校验/待提交（待建）
├── composables/
│   ├── useStudioPolling.ts         # 状态快照 2s 轮询 + 阶段切换时停止（待建）
│   └── useGateLocator.ts           # finding → DAG 节点 / HOCON 行列 / 字段行 的联动（待建）
└── types.ts                        # EtlPlan / GateResult / ClarificationQuestion 等（待建）
```

## 3. 对外接口

### 3.1 `stores/pipeline.ts` 状态设计

```ts
export type StudioPhase =
  | 'idle'                  // 未开始
  | 'parsing'               // 解析中
  | 'awaiting_clarification'// 等待澄清（LangGraph 已 interrupt）
  | 'generating'            // 生成中（含自动修复）
  | 'gate_running'          // 门禁校验
  | 'gate_failed'           // 门禁阻断
  | 'ready_to_freeze'       // 门禁通过、可冻结
  | 'frozen'                // 已冻结（整页只读）
  | 'submitted'             // 已提交审批
  | 'error'

interface PipelineStudioState {
  pipeline: Pipeline | null
  draft: { name: string; requirement: string; source_data_source_id: number | null; target_data_source_id: number | null }
  conversation: ConversationMessage[]
  pendingQuestions: ClarificationQuestion[]
  phase: StudioPhase
  phaseMessage: string | null          // 后端给的阶段文案（如「正在关联元数据」）
  plan: EtlPlan | null
  hocon: HoconArtifact | null
  gate: GateResult | null
  versions: PipelineVersionSummary[]
  currentVersionId: number | null      // 非空 = 只读
  selectedNodeId: string | null        // DAG ↔ HOCON ↔ 字段映射 联动锚点
  selectedFindingId: string | null
  dirty: boolean                       // 草稿有未提交改动
  submitting: boolean
}
```

Actions（全部在 store 内调 `api/pipeline.ts`，视图只调 action）：

| action | 签名 | 副作用 |
|---|---|---|
| `loadPipeline` | `(id: number) => Promise<void>` | 载 pipelines/plan/hocon/gate/conversation |
| `saveDraft` | `() => Promise<void>` | `PATCH /pipelines/{id}`，失败回滚草稿 |
| `startGeneration` | `(payload: StartGenerationPayload) => Promise<void>` | 追加用户消息（乐观）+ `POST /generations` |
| `submitClarifications` | `(answers: Record<string, unknown>, note?: string) => Promise<void>` | `POST /generations`（resume 语义），清空 `pendingQuestions` |
| `runAutofix` | `() => Promise<void>` | `POST /gate/autofix`，刷新 gate/hocon/plan |
| `freezeVersion` | `(note: string) => Promise<PipelineVersionDetail>` | `POST /versions`，成功后 `currentVersionId` 赋值 → 只读 |
| `branchNewVersion` | `() => void` | 本地清 `currentVersionId` 并保留当前 plan/hocon 作为新草稿基线 |
| `selectNode` / `selectFinding` | `(id: string \| null) => void` | 仅前端联动，不发请求 |

### 3.2 组件 props / emits

```ts
// RequirementPanel.vue
defineProps<{ draft: PipelineStudioDraft; readonly: boolean; sourceOptions: DataSourceOption[]; targetOptions: DataSourceOption[] }>()
defineEmits<{ (e: 'start', payload: StartGenerationPayload): void; (e: 'update:draft', v: PipelineStudioDraft): void }>()

// ClarificationPanel.vue
defineProps<{ messages: ConversationMessage[]; questions: ClarificationQuestion[]; readonly: boolean; busy: boolean }>()
defineEmits<{ (e: 'answer', answers: Record<string, unknown>, note?: string): void }>()

// HoconTab.vue
defineProps<{ content: string; parserVersion: string; findings: GateFinding[]; readonly: boolean; highlightLine?: number }>()
defineEmits<{ (e: 'finding-click', findingId: string): void }>()

// DagTab.vue
defineProps<{ dag: EtlPlanDag; selectedNodeId: string | null; findings: GateFinding[] }>()
defineEmits<{ (e: 'select-node', nodeId: string | null): void }>()

// GatePanel.vue
defineProps<{ gate: GateResult | null; autofixMaxRounds: number; readonly: boolean }>()
defineEmits<{ (e: 'locate', findingId: string): void; (e: 'autofix'): void }>()

// VersionBar.vue
defineProps<{ versions: PipelineVersionSummary[]; currentVersionId: number | null; phase: StudioPhase; canFreeze: boolean }>()
defineEmits<{ (e: 'freeze'): void; (e: 'submit-approval'): void; (e: 'branch'): void; (e: 'compare', a: number, b: number): void }>()
```

### 3.3 API 调用清单

| 方法 | 路径 | 权限 | 幂等 | 轮询 | 状态 |
|---|---|---|---|---|---|
| GET | `/pipelines?project_id&status&q&page&page_size` | `maker` `auditor` | — | — | **待后端 be-09 落地** |
| POST | `/pipelines` | `maker` | **是** | — | 同上（建草稿） |
| GET | `/pipelines/{id}` | `maker` `auditor` | — | — | 同上 |
| PATCH | `/pipelines/{id}` | `maker` | **是** | — | 同上（仅草稿态可改） |
| DELETE | `/pipelines/{id}` | `maker` | **是** | — | 同上（仅草稿态可删） |
| GET | `/pipelines/{id}/conversation?page_size=100` | `maker` | — | 生成中 2s | 同上（`pipeline_conversation`） |
| POST | `/pipelines/{id}/generations` | `maker` | **是** | — | **待后端 be-09 落地**；启动或 `resume`（body 含 `answers`） |
| GET | `/pipelines/{id}/generations/current` | `maker` | — | **2s**（活跃阶段） | 同上；返回阶段、`pending_questions`、`autofix_round` |
| GET | `/pipelines/{id}/plan` | `maker` | — | — | **待后端 be-10 落地** |
| GET | `/pipelines/{id}/hocon` | `maker` | — | — | 同上 |
| GET | `/pipelines/{id}/gate` | `maker` | — | — | **待后端 be-11 落地** |
| POST | `/pipelines/{id}/gate/autofix` | `maker` | **是** | — | 同上；超轮次 → `AUTOFIX_EXHAUSTED` |
| POST | `/pipelines/{id}/versions` | `maker` | **是** | — | **待后端 be-12 落地**；冻结（返回 `sha256`） |
| GET | `/pipelines/{id}/versions` | `maker` `auditor` | — | — | 同上 |
| GET | `/pipelines/{id}/versions/{version_id}` | 全部相关角色 | — | — | 同上（只读） |
| GET | `/pipelines/{id}/versions/{version_id}/diff?against={other_id}` | 同上 | — | — | 同上 |
| POST | `/preparations`（body `{pipeline_version_id}`） | `maker` | **是** | — | **待后端 be-13 落地**；接口归属 fe-05，本页只作入口跳转 |

**轮询策略**：只在 `phase ∈ {parsing, generating, gate_running}` 时以 2s 轮询 `GET /generations/current`；进入终态（`awaiting_clarification` / `ready_to_freeze` / `gate_failed` / `error`）立即停轮询。**不引 WebSocket**。

## 4. 数据模型

```ts
export interface Pipeline {
  id: number; project_id: number; name: string; description: string
  status: PipelineStatus                          // 16 值，见 models/enums.py
  current_version_id: number | null
  source_data_source_id: number | null; target_data_source_id: number | null
  created_at: string; updated_at: string
}

export interface ConversationMessage {
  id: number; role: 'user' | 'assistant' | 'system' | 'tool'
  content: string
  message_kind: 'chat' | 'clarification_question' | 'clarification_answer' | 'plan_summary' | 'gate_summary'
  meta: Record<string, unknown>                   // tool/assistant 消息的结构化载荷
  created_at: string; created_by_display_name: string | null
}

export interface ClarificationQuestion {
  key: string                                     // 如 'dedup_keys'、'incremental_field'
  question: string
  kind: 'text' | 'single_choice' | 'multi_choice' | 'field_ref' | 'threshold'
  options: Array<{ value: string; label: string }> | null
  required: boolean
  hint: string | null
}

export interface EtlPlan {
  id: number; schema_version: string              // Plan DSL 版本
  plan: {
    inputs: Array<{ id: string; data_source_id: number; schema: string; table: string }>
    outputs: Array<{ id: string; data_source_id: number; database: string; table: string; mode: 'shadow_then_swap' }>
    field_mappings: FieldMapping[]
    transforms: Array<{ id: string; kind: string; input_fields: string[]; output_field: string; params: Record<string, unknown> }>
    filters: Array<{ id: string; expression: string; description: string }>
    quality_rules: QualityRule[]
    dag: EtlPlanDag
    budget: RuntimeBudget
  }
  valid: boolean; validation_errors: Array<{ path: string; reason: string }>
}

export interface FieldMapping {
  source_field: string; source_type: string
  target_field: string; target_type: string
  transform: 'none' | 'cast' | 'trim' | 'concat' | 'mask' | 'hash'
  masking: { method: 'mask_middle' | 'hash' | 'truncate'; params: Record<string, unknown> } | null
  quality_rule_ids: string[]
  change: 'same' | 'added' | 'removed' | 'modified'     // 由后端比对源/目标 schema 后给出
}

export interface EtlPlanDag {
  nodes: Array<{ id: string; type: 'source' | 'transform' | 'filter' | 'quality' | 'sink'; label: string; meta: Record<string, unknown>; position?: { x: number; y: number } }>
  edges: Array<{ id: string; source: string; target: string; label: string | null }>
}

export interface HoconArtifact { id: number; content: string; sha256: string; parser_version: string; validation_result: Record<string, unknown> }

export interface QualityRule {
  id: string; field: string
  rule_type: 'not_null' | 'regex' | 'range' | 'enum' | 'type_cast'
  params: Record<string, unknown>
  error_code: ErrorCodeLabel
  on_violation: 'reject' | 'warn'
  enabled: boolean
}

export interface RuntimeBudget {                  // 对应 runtime_supervision_contract
  max_read_rows: number; max_write_bytes: number; max_duration_sec: number
  max_amplification_ratio: number; max_reject_rate: number; warn_threshold_pct: number
}

export interface GateResult {
  evaluated_at: string; conclusion: 'pass' | 'warn' | 'block'
  findings: GateFinding[]
  autofix_rounds: AutofixRound[]
  autofix_exhausted: boolean
}

export interface GateFinding {
  id: string
  severity: 'block' | 'warn' | 'info'
  category: 'syntax' | 'schema' | 'field_mapping' | 'resource' | 'policy'
  code: string                                   // ErrorCode 取值，如 HOCON_SYNTAX_ERROR
  message: string                                // 后端已脱敏文案
  locator: GateLocator
  suggestion: string | null
}

/** 定位点：决定「点 finding 跳到哪」 */
export type GateLocator =
  | { kind: 'hocon'; line: number; column: number; end_line?: number }
  | { kind: 'plan'; path: string }                // 如 'field_mappings[3].target_field'
  | { kind: 'dag_node'; node_id: string }
  | { kind: 'source_table'; data_source_id: number; schema: string; table: string }

export interface AutofixRound {
  round: number                                  // 1..LLM_AUTOFIX_MAX_ROUNDS(3)
  trigger_finding_ids: string[]
  hocon_diff: string                             // unified diff 文本
  plan_diff: string | null
  resolved_finding_ids: string[]
  created_at: string
}

export interface PipelineVersionSummary {
  id: number; version_no: number; sha256: string
  status_note: string                            // 冻结时的备注
  frozen_at: string; frozen_by_display_name: string | null
  source_metadata_profile_version: number | null
  generation_context: { model_name: string; model_version: string; prompt_template_version: string; autofix_rounds: number }
}
```

权威列：`pipeline` `pipeline_conversation` `etl_plan` `hocon_artifact` `quality_contract` `runtime_supervision_contract` `pipeline_version`（§3），`metadata_profile`（§2）。`GateResult` / `ClarificationQuestion` / `GateLocator` 为**非表结构**的接口载荷，需 be-09/be-11 定稿（见 §10）。

## 5. 核心流程

### 5.1 四区布局与数据归属（PRD §11.2）

```
┌────────────┬──────────────────────────────────────┬──────────────┐
│ 左 320–380 │ 中 flex-1                            │ 右 340–420   │
│ 源/目标选择 │ [EtlPlan][HOCON][DAG][字段映射] Tab  │ 门禁结果      │
│ 需求输入    │ → 当前 Tab 内容                      │ 风险提示      │
│ 澄清对话    │                                      │ 质量契约      │
│            │                                      │ 运行预算      │
├────────────┴──────────────────────────────────────┴──────────────┤
│ 底部 56–72：版本 v{n} · sha256 · 生成历史 · [冻结版本] [提交审批]  │
└──────────────────────────────────────────────────────────────────┘
```

- 左中右三栏**独立滚动**，页面高度撑满视口（`height: 100vh` + 各栏 `overflow: auto`），底部条固定。
- 窄屏（< 1280px）右栏折到中部 Tab 的第 5 个页签「门禁与预算」；不隐藏信息。
- 栏宽可拖拽，比例存 `localStorage['elt-agent.studio-layout']`（仅尺寸，无业务数据）。

### 5.2 生成与澄清（LangGraph 中断 → resume）

1. 用户选好源/目标、输入需求 → `startGeneration`。
2. 用户消息**乐观插入**对话区（临时 id，标 `pending`）；`POST /generations` 成功 → 用服务端消息替换；失败 → 该条变红 + 「重试」，**不重复插入**。
3. 2s 轮询 `GET /generations/current`：
   - `phase='parsing'`/`'generating'`: `StudioPhaseTag` 显示「解析中」/「生成中」+ 阶段文案；请求进行中的按钮禁用。
   - `phase='awaiting_clarification'`: 返回 `pending_questions` → 左侧 `ClarificationPanel` 渲染**结构化表单**（见 §6.1），同时把对话滚到最新一条提问。
   - `phase='gate_running'`: 显示「门禁校验」+ 不确定进度。
   - `phase='gate_failed'` / `'ready_to_freeze'`: 停止轮询，刷新 plan/hocon/gate。
4. 用户填完澄清项 → `submitClarifications` → `POST /generations`（`answers` + 可选 `note`）→ 后端在**同一 thread** 上 `resume`，不重复执行已完成的安全探查步骤。提交后清空 `pendingQuestions` 并插入一条用户消息（`message_kind='clarification_answer'`）。
5. 对话历史分页拉取（`page_size=100`），新消息到达时若用户已向上滚动则不强制滚动（显示「有新消息」浮标）。

### 5.3 门禁 →（有限修复）→ 冻结

```
gate.conclusion === 'block'  → 右栏列 findings；底部冻结按钮 disabled
   └─ 用户点「自动修复」→ POST /gate/autofix（受 3 轮上限）
        ├─ 成功 → 新增一条 AutofixRound（展示前后 Diff）+ 重跑门禁结果
        └─ 已 3 轮 → 按钮 disabled + 文案「已达自动修复上限，请人工处理」
gate.conclusion === 'pass'|'warn' → 底部「冻结版本」可用
   └─ 点击 → FreezeConfirmDialog（制品清单 + 各制品指纹）→ 确认 → POST /versions
        ├─ 成功 → 展示返回的 sha256 → 整页只读、StudioPhase='frozen'
        └─ 失败 → VERSION_IMMUTABLE / CONFLICT 等按 §7 处理
冻结后 → 底部按钮变为 [基于此版本生成新版本] [提交审批]
```

「提交审批」经 `POST /api/v1/preparations`（**fe-05 定义的接口**，body `{pipeline_version_id}`）生成 Preparation；本页只负责跳转，不展示审批内容（`risk_level` / `required_slots` / 冻结事实在 fe-05）。

**冻结二次确认展示的制品清单**（与 `pipeline_version` 的外键一一对应）：EtlPlan（`schema_version` + `valid`）、HoconArtifact（`sha256` + `parser_version`）、QualityContract（规则数 + `reject_rate_budget`）、RuntimeSupervisionContract（五项预算）、依据的 MetadataProfile（`version` + `sha256` + `expires_at`）。若 `expires_at` 已过 → 确认按钮禁用并提示「元数据已过期，需重新探查」（`METADATA_PROFILE_EXPIRED`）。

**最终 `pipeline_version.sha256` 不在前端预计算**，只展示后端冻结返回值。

### 5.4 阶段状态机

| 当前 `StudioPhase` | 事件 | 新状态 | 非法转移处理 |
|---|---|---|---|
| `idle` | `startGeneration` | `parsing` | 未选源/目标时按钮 disabled |
| `parsing` `generating` | 快照 `awaiting_clarification` | `awaiting_clarification` | — |
| `awaiting_clarification` | `submitClarifications` | `generating` | 必填项未填 → 前端阻断，不发请求 |
| `generating` | 快照 `gate_running` | `gate_running` | — |
| `gate_running` | `conclusion='block'` | `gate_failed` | — |
| `gate_running` | `conclusion∈{pass,warn}` | `ready_to_freeze` | — |
| `gate_failed` | `runAutofix` | `generating` | `autofix_exhausted` → 保持 `gate_failed` 且按钮禁用 |
| `ready_to_freeze` | `freezeVersion` | `frozen` | `block` 结论下后端应拒（`GATE_BLOCKED`），前端也禁用按钮 |
| `frozen` | `branchNewVersion` | `idle`（保留基线数据） | 生成新版本由后端分配 `version_no + 1` |
| `frozen` | `submit-approval` | `submitted` | 已 `submitted` 时按钮禁用（后端亦有幂等） |
| 任意 | 请求失败且不可恢复 | `error` | 提供「重新加载」回 `idle` 并保留草稿 |

**只读态**：`currentVersionId !== null` 或 `phase ∈ {frozen, submitted}` → 整页输入控件 `disabled`，顶部 `DraftFrozenBadge` 显示「不可变版本 v{n}」+ `ArtifactHash`；HOCON/Plan/DAG 仍可浏览与 Diff。

## 6. 关键算法与规则

### 6.1 澄清项渲染规则

| `kind` | 控件 | 校验 |
|---|---|---|
| `text` | `el-input type=textarea`（3 行） | `required` 时空值阻断 |
| `single_choice` | `el-radio-group` | 必须命中 `options` 中的 `value` |
| `multi_choice` | `el-select multiple` | 至少 1 项 |
| `field_ref` | 复用 fe-03 的 Schema 字段选择树（源/目标按问题语义选一侧） | 必须来自该 Profile 的 `schema_snapshot`，不允许手输（防幻觉字段） |
| `threshold` | `el-input-number` + 单位后缀 | 范围由 `meta.min/max` 给；缺失时按 `0 < x ≤ 1`（比率）或正整数（行数）兜底并在 UI 标注「范围待确认」 |

- 结构化表单与自由文本**并存**：表单下方固定一个「补充说明（可选）」textarea，内容随 `note` 一起提交；自由文本不参与结构化校验。
- **模型输出与用户确认内容的视觉区分**：模型输出左侧蓝色竖线与 `assistant` 标签；用户输入右侧灰底与 `user` 标签；`clarification_answer` 消息额外显示「已提交的补充参数」结构化摘要（key: value 列表），与模型原文分块呈现。
- 每次提交前展示「本次将提交的字段」预览，避免用户误以为自由文本会覆盖结构化答案。

### 6.2 DAG 渲染（Vue Flow）

- 映射：`dag.nodes[].type` → 自定义节点组件（`SourceNode`/`TransformNode`/`FilterNode`/`QualityNode`/`SinkNode`），统一注册为 `nodeTypes`；`edges` 直接映射为 Vue Flow `edges`（`type: 'smoothstep'`，`markerEnd: arrowclosed`）。
- 布局：优先用节点自带的 `position`；缺失时按**最长路径分层**（源层 x=0，逐层 +280px，同层按索引 y 递增）在本页计算——这是**布局算法而非业务聚合**，允许在前端做；节点数 > 200 时关闭自动布局，提示「拓扑过大，使用紧凑视图」。
- 交互：点击节点 → `selectNode(id)` → 中部切到该节点相关视图（字段映射 Tab 过滤该节点字段；HOCON 滚到该节点 `locator`）；含 `severity='block'` finding 的节点描红边 + 右上角徽标。
- 只读：`nodesDraggable=false`、`nodesConnectable=false`、`elementsSelectable=true`；布局改动不被记忆（刷新即回布局结果）。

### 6.3 HOCON 视图（CodeMirror 6）

| 能力 | 实现 |
|---|---|
| 只读 | `EditorState.readOnly.of(true)`、`EditorView.editable.of(false)` |
| 语法高亮 | **自研 `StreamLanguage` 简易 tokenizer**（键 / 字符串 / 数字 / 布尔 / 注释 / `{}` / `=`）；`@codemirror/lang-json` 不适用于 HOCON（允许 `key = value`、省略逗号、`#` 注释） |
| 行号 + 折叠 | `lineNumbers()` + `foldGutter()`（按 `{}` 折叠） |
| finding 行内标记 | `GateLocator.kind==='hocon'` → 用 `Decoration.line({class: 'cm-finding-block|warn'})` 标行 + 行号槽 `gutterMarker`；点击标记 → emit `finding-click` |
| 定位跳转 | `editor.dispatch({ selection: { anchor: posAtLine(line, column) }, effects: EditorView.scrollIntoView(pos) })` |
| 版本/修复对比 | `HoconDiffDialog` 用 `@codemirror/merge` 的 side-by-side（或 `unifiedMergeView`），左右标题分别为「v{n}」「v{n-1}」/「修复前」「修复后」 |

**不做 HOCON 语法校验**：校验器版本 `parser_version` 来自后端；前端 tokenizer 只负责着色，**绝不**因着色失败报错（tokenizer 异常时降级为纯文本，不阻断页面）。

### 6.4 字段映射与 Schema Diff 表

| 列 | 字段 | 说明 |
|---|---|---|
| 源字段 | `source_field` + `source_type` | 等宽字体；`change='removed'` 时红色删除线 |
| 目标字段 | `target_field` + `target_type` | `change='added'` 时绿色高亮 |
| 转换 | `transform` | `cast/trim/concat/mask/hash`，`none` 显示 `—` |
| 脱敏 | `masking.method` | 有脱敏时显示锁形图标 + 方法名（悬停显示 `params` 的非敏感部分） |
| 质量规则 | `quality_rule_ids` | 显示规则数，悬停列出 `rule_type`；点击跳右栏质量契约并高亮 |
| Diff | `change` | `modified` 行整行浅黄底，悬停显示「源类型 → 目标类型」变化 |

顶部提供「仅看差异」开关（默认关）与「仅看敏感字段」快捷筛选（`masking !== null`）。

### 6.5 门禁结果面板

- 三态结论：`pass`（绿）/`warn`（橙）/`block`（红）；`block` 时面板顶部固定一条 `Alert` 说明「存在阻断项，无法冻结版本」并禁用底部冻结按钮。
- 分组顺序固定：`block` → `warn` → `info`；组内按 `category`（syntax / schema / field_mapping / resource / policy）二级分组。
- 每条 finding 渲染 `code` 标签（`enumLabel('ErrorCode')`，未知码显示原文）、`message`、`suggestion`（若有）与「定位」按钮 → `useGateLocator` 按 `locator.kind` 跳转（hocon → 行；plan → 高亮 EtlPlan 对应路径；dag_node → 选中节点；source_table → 切到字段映射并过滤该表）。
- 自动修复区：每条 `AutofixRound` 一张折叠卡（默认收起），标题为「第 {round} 轮 · 解决 {n} 项」，展开显示 `hocon_diff` / `plan_diff` 的 unified diff，并提供「与当前版本对比」。

### 6.6 乐观更新与失败回滚边界

| 操作 | 乐观更新 | 失败处理 |
|---|---|---|
| 草稿字段（名称/需求文本） | 是（本地即时） | 保留本地值 + 标 `dirty` + 顶部「未保存」提示，可重试 |
| 追加用户消息 | 是（临时 id，标 pending） | 该条转失败态 + 「重试」；不重复插入 |
| 生成 / 暂停 / 自动修复 / 冻结 / 提交审批 | **否**（服务端结果不可预测） | 按钮回可点 + toast 错误 + `request_id`；`gate`/`hocon` 等数据保持上一次成功值，不置空 |

## 7. 错误码与拒绝语义

| 触发条件 | 错误码 | HTTP | 本页行为 |
|---|---|---|---|
| EtlPlan 结构校验失败 | `PLAN_INVALID` | 422 | 门禁面板显示 `validation_errors[].path`，定位到 EtlPlan 视图对应路径 |
| HOCON 语法错误 | `HOCON_SYNTAX_ERROR` | 422 | HOCON Tab 行内标红 + 自动滚到首个错误行 |
| Schema 不匹配 | `SCHEMA_MISMATCH` | 409 | 字段映射 Tab 打开并高亮差异行 |
| 门禁阻断（含 P0/P1 策略命中） | `GATE_BLOCKED` / `POLICY_BLOCKED` | 403 | 冻结按钮禁用；展示门禁结论与 P0/P1 说明；**不提供「仍然冻结」的绕过入口** |
| 自动修复超轮次 | `AUTOFIX_EXHAUSTED` | 409 | 修复按钮禁用 + 「请人工处理」，提示修改需求或源/目标后重新生成 |
| 修改不可变版本 | `VERSION_IMMUTABLE` | 409 | toast + 整页强制转只读（重新拉版本详情） |
| Metadata Profile 过期 | `METADATA_PROFILE_EXPIRED` | 409 | 阻断冻结与生成；跳 fe-03 重新探查 |
| 模型不可用 | `LLM_UNAVAILABLE` | 503 | 生成阶段错误态 + 「重试生成」；不静默降级为手工 |
| 同幂等键不同 body | `IDEMPOTENCY_CONFLICT` | 409 | 提示刷新后重试；**不自动重发**（避免重复冻结版本） |
| 非草稿态发起修改 | `CONFLICT` | 409 | 重新加载 pipeline，静默对齐后端状态 |
| 无 `maker` 角色 | `ROLE_REQUIRED` `FORBIDDEN` | 403 | 只读浏览 + 顶部无权限条；不展示写按钮 |
| 资源不存在 | `NOT_FOUND` | 404 | 返回 Pipeline 列表 |

前端不写证据账本；`pipeline.generated` / `pipeline.gate_evaluated` / `pipeline.version_frozen` 由后端写入。

## 8. 安全与合规约束

- **无明文 Secret**：本页不出现任何凭据；源/目标只以 `name + type + env` 展示；`DataSourceOption` **不含** `endpoint` 与 `secret_ref`。
- **模型上下文边界**：对话区只渲染后端下发的文案；若后端消息中检出凭据/样本原文形态（`://`、`user:pass@`、`sk-`、18 位身份证、手机号），前端按 fe-03 §8 的过滤规则**丢弃并替换**为通用提示，并上报 `console.error` 供排障。
- **脱敏默认开启**：字段映射与样本展示中的敏感字段默认掩码；本页不提供解敏入口。
- **不可变**：冻结后 `VERSION_IMMUTABLE` 是最后一道防线；前端只读态是体验层，**不得**被视为安全措施（后端拒绝是唯一依据）。
- **P0/P1 不可绕过**：Studio 不提供任何「忽略阻断」「强制冻结」按钮；`POLICY_BLOCKED` 只展示解释与建议。
- **租户过滤**：所有请求带 `project_id`；`source_data_source_id` / `target_data_source_id` 只能从当前项目的数据源选项中选（后端仍会校验 `TENANT_MISMATCH`）。
- **审计可见**：冻结确认弹窗明确列出「本次冻结将写入 `pipeline.version_frozen` 证据事件，内容为制品指纹与提交主体」，与后端账本一致。
- **副作用**：本页无任何副作用动作（生成是只读推理，冻结是本地写制品），因此不经 ToolBroker；执行相关副作用全部在 fe-05/fe-06。

## 9. 测试要点

- **单元测试**
  - 阶段状态机：穷举 §5.4 的每个转移，非法事件（如 `gate_failed` 下点冻结）**不改变**状态且按钮 disabled。
  - 澄清表单：`field_ref` 只能选 Profile 内字段；`single_choice` 传入非 `options` 值 → 校验失败；`required` 未填 → 不发请求。
  - `GateLocator` 联动：三种 `locator.kind` 分别把 `selectedNodeId` / `highlightLine` / 字段过滤设对（快照或 spy 断言）。
  - HOCON：tokenizer 遇到非法字符不抛异常（降级纯文本）；`highlightLine` 变化触发 `scrollIntoView` 一次。
  - DAG 布局：无 `position` 的 5 节点链 → 分层 x 递增 280；201 节点 → 走紧凑分支且提示出现。
  - 乐观更新：`startGeneration` 失败后对话不含重复消息且失败条目可重试。
  - 冻结：`expires_at` 已过 → 确认按钮 disabled；成功返回后 `currentVersionId` 赋值且整页 `disabled`。
- **集成测试**（真后端）：一句话需求 → 中断提问 → 补充 → 生成 → 门禁 block → 自动修复 1 轮 → 门禁 pass → 冻结，全程 UI 阶段与后端一致；`version.sha256` 展示值与 `GET /versions/{id}` 返回值逐字相同（**前端无重算**）。
- **必须覆盖的反例**：门禁 `block` 时冻结按钮不可点且直接调接口被后端 `GATE_BLOCKED` 拒绝；第 4 次点自动修复被拒（`AUTOFIX_EXHAUSTED`）；冻结后编辑草稿字段 → 输入 disabled 且后端 `VERSION_IMMUTABLE` 兜底；Profile 过期后冻结被拦截。

## 10. 未决与风险

| 事项 | 现状 | 建议 / 验证方式 |
|---|---|---|
| `GET /generations/current` 快照结构未冻结 | 阶段判定需要 `phase` + `pending_questions`，但 LangGraph 的 `interrupt` 载荷映射到 API 未定义 | 建议由 be-09 定义 `WorkflowSnapshot{phase, pending_questions, autofix_round, last_error}`；前端阶段枚举按此对齐，落地前用 mock |
| `StudioPhase` 与 `PipelineStatus` 的关系 | 前者是页面临时阶段，后者是持久状态（16 值） | 建议：`phase` 只表达**当前请求周期**的过程态，落库状态一律用 `PipelineStatus`；两者映射表由 be-09 给，前端不自行推导 |
| `GateResult` / `GateLocator` 为全新接口载荷 | `engines/gate` 的输出结构未在文档冻结 | 需 be-11 定稿并保证 `locator` 覆盖四类定位（hocon/plan/dag_node/source_table）；否则「点 finding 跳转」实现不了（PRD §6.3.3 硬要求） |
| `dag.nodes[].position` 是否由后端给 | 未定 | 若后端不给，前端做分层布局（已在 §6.2 定义）；若给，则前端只做兜底 |
| 自动修复的 Diff 格式 | 未定 unified diff 还是结构化 patch | 建议 unified diff 文本（前端用 `@codemirror/merge` 渲染）；需 be-11 确认 |
| Studio 是否展示风险等级（PDP） | PDP 在 Prepare 阶段才输出 `risk_level`（§四.3 判定矩阵入口是 Prepare） | 本页「风险提示」若展示，必须来自 `engines/policy` 的**纯规则**预检（不得用 LLM 输出）；是否提供该预检需拍板，否则右栏该区块留空并注明「风险评级在提交审批时给出」 |
| HOCON 高亮方案 | 无官方 HOCON 语言包 | 自研 `StreamLanguage` tokenizer；若 M2 发现维护成本高，退化为纯文本 + 行号（不阻断功能） |
| 对话历史分页与「生成中」轮询叠加 | 生成中每 2s 可能追加消息 | 建议 2s 轮询只取 `since_id` 增量；需 be-09 支持 `after_id` 参数，否则整表重拉在长对话下会明显卡顿 |
| 冻结确认里能否预览最终 sha256 | 目前不预览（后端计算） | 若产品要求「确认前看到 sha256」，需后端提供 `POST /versions/preview` 返回**将**计算的指纹；不建议前端预计算（规范化规则在后端 `canonical_json`） |
| 左栏栏宽与四区在 1366px 下的可用性 | 右栏 340px + 左栏 320px 后中部仅约 700px | M2 需在 1366×768 实测；必要时右栏改抽屉式 |
| 无手工编辑 HOCON 的能力 | 本期不做 | 若后续开放，必须配「编辑后重跑门禁 + 指纹失效 + 审批自动失效」的完整链路，不能只加个编辑器 |

## 11. 变更记录

| 日期 | 变更 | 原因 |
|---|---|---|
| 2026-10-16 | 初稿：四区布局、澄清中断/恢复交互、DAG 与 HOCON 定位联动、门禁分组、冻结前置与只读态、store 与接口清单 | M2 Pipeline Studio 落地 |
