# fe-07 安全治理与 Benchmark

> 上游：[概要设计总纲.md](../概要设计总纲.md) §5 Harness 安全执行内核 · §7 API 分组 · §10 与 PRD 对应 ｜ [PRD.md](../PRD.md) §6.6 §13.6 §14
> 数据：[核心数据字典.md](../核心数据字典.md) §6 评测 · §7 证据与投递 · §8 支撑表（`policy_rule` / `risk_policy_version`）｜ 默认值：[需求对齐记录.md](../需求对齐记录.md) §二.5 §二.6 §二.8 §四
> 代码位置：`frontend/src/views/governance/` ｜ 里程碑：M4 ｜ 依赖：fe-01（后端 be-06 be-07 be-20，接口待落地）

## 1. 职责与边界

- **做什么**
  1. **策略管理**：查看与编辑 P0–P3 规则表（`policy_rule`），`condition` 的结构化编辑，启用/禁用；变更走「提交变更集 → 审批 → 生效」三步，可见版本历史、生效范围与 Diff。
  2. **预算默认值管理**：展示与编辑项目级预算覆盖，对照 `SUP_MAX_*` 默认值与《需求对齐记录》冻结值给出一致性提示。
  3. **Benchmark**：Suite 列表、Case 列表、触发评测（选 suite → 选 `cache_mode` → 触发 → 进度）、结果页（五项指标卡 + 基线对比 + 逐条 case 结果）、导出。
  4. **证据账本查询**：事件列表与筛选、事件详情（脱敏 payload + `prev_hash`/`event_hash`）、**独立验链**、导出 JSON/CSV。
- **不做什么**
  - **不执行 PDP 判定**：`engines/policy/pdp.py` 是纯函数规则引擎（无 LLM、无网络）。本模块只编辑**规则数据**；界面上的「试算」也必须由后端调用同一纯函数完成，前端不实现任何风险判定逻辑。
  - **不改写账本**：`evidence_event` 只允许 INSERT；本模块全程只读 + 触发验链，不提供编辑、删除、补录入口；`payload` 已是脱敏内容，不做二次加工，也不提供「查看原始事件」开关。
  - **不执行 Benchmark 用例本身**：触发评测是服务端行为（be-20 串联生成/门禁/执行链路），前端只提交 `suite_id` 与 `cache_mode`。
  - 不做告警渠道配置（钉钉 webhook 属 be-21）与 Prometheus/Grafana 面板。
- **所在分层**：前端展示层。只经 `src/api/governance.ts` 调 `/api/v1/governance/*`。

## 2. 模块结构

```
frontend/src/
├── views/governance/
│   ├── PolicyListView.vue            # 策略规则表（路由 /governance/policies）
│   ├── PolicyVersionView.vue         # 策略版本与变更审批（路由 /governance/policy-versions）
│   ├── BudgetDefaultsView.vue        # 预算默认值与项目覆盖（路由 /governance/budget）
│   ├── BenchmarkSuiteListView.vue    # Suite 列表（路由 /governance/benchmark）
│   ├── BenchmarkSuiteDetailView.vue  # Case 列表 + 触发评测（路由 /governance/benchmark/:suiteId）
│   ├── BenchmarkRunView.vue          # 评测结果页（路由 /governance/benchmark/runs/:runId）
│   ├── EvidenceLedgerView.vue        # 证据账本查询（路由 /governance/evidence）
│   └── components/  PolicyRuleTable.vue · PolicyConditionEditor.vue（condition 结构化表单）
│                    PolicyDiffView.vue（并排 Diff）· BudgetOverrideForm.vue（覆盖 + 一致性提示）
│                    BenchmarkMetricsCards.vue（五项指标 + 基线对比）· BenchmarkCaseResultTable.vue（失败高亮）
│                    EvidenceEventTable.vue · EvidenceEventDetail.vue · LedgerVerifyPanel.vue（验链）
├── api/governance.ts                 # /governance 调用唯一出口
└── stores/governance.ts              # 策略、版本、Benchmark、账本四组状态
```

## 3. 对外接口

### 3.1 API 调用清单

全部标注「（待后端 be-07 / be-06 / be-20 落地）」。**本模块无 2 s 轮询**：策略、账本、评测结果都是低频只读数据，页面进入时拉一次即可；仅评测 Run 进行中按 5 s 轮询（§5）。

| 方法 | 路径 | 权限 | 幂等 | 说明 |
|---|---|---|---|---|
| GET | `/api/v1/governance/policy-rules` | 全部角色可读 | 天然 | 规则表，支持 `?level=&enabled=` |
| POST / PUT | `/api/v1/governance/policy-rules` ｜ `/{rule_id}` | `checker_security` | 必带 | 新增 / 更新 `condition` `level` `required_slots`（写入待审批变更集） |
| POST | `/api/v1/governance/policy-rules/{rule_id}/enable` ｜ `/disable` | `checker_security` | 必带 | 启用 / 禁用 |
| GET | `/api/v1/governance/policy-versions` ｜ `/{version}/diff` | 全部角色可读 | 天然 | 版本列表（生效时间/范围/审批人）；按 rule code 结构化 Diff |
| POST | `/api/v1/governance/policy-versions` | `checker_security` | 必带 | 提交变更集（后端生成 `diff`，状态待审批） |
| POST | `/api/v1/governance/policy-versions/{version}/approve` ｜ `/reject` | `checker_security`（≠ 提交人） | 必带 | 通过 → 落 `effective_at`；驳回 → 不生效 |
| GET / PUT | `/api/v1/governance/budget-defaults` | 读：全部角色；写：`checker_security` | 写必带 | `SUP_MAX_*` 默认值 + 当前项目覆盖 |
| GET | `/api/v1/governance/benchmark/suites` ｜ `/suites/{suite_id}/cases` | 全部角色可读 | 天然 | Suite / Case 列表 |
| POST | `/api/v1/governance/benchmark/runs` | `checker_security` | 必带 | 触发评测，body `{suite_id, cache_mode}` |
| GET | `/api/v1/governance/benchmark/runs` ｜ `/runs/{run_id}` | 全部角色可读 | 天然 | 评测 Run 列表 / 结果（`metrics` + `case_results`）；running 时轮询 5 s |
| GET | `/api/v1/governance/benchmark/runs/{run_id}/export?format=json\|csv` | 全部角色可读 | 天然 | 结果导出（下载，不走 JSON 信封） |
| GET | `/api/v1/governance/evidence/events` ｜ `/events/{event_id}` | 全部角色可读 | 天然 | 游标分页；筛选 `from` `to` `event_type` `actor_id` `project_id` `cursor` `limit` |
| POST | `/api/v1/governance/evidence/verify` | 全部角色可读 | 天然 | 独立验链，body `{from_seq?, to_seq?}` |
| GET | `/api/v1/governance/evidence/export?format=json\|csv` | 全部角色可读 | 天然 | 账本导出 |

**权限矩阵**：`checker_security` 可写策略与预算覆盖、可触发评测；`maker` / `checker_data` / `operator` 只读；**`auditor` 只读全部**（含账本与验链），无任何写入口。服务端对全部写接口做角色校验，前端隐藏按钮仅为体验。

### 3.2 组件 props / emits

```ts
PolicyRuleTableProps       { rules: PolicyRuleRead[]; loading: boolean; editable: boolean }
  emits: 'toggle'({rule_id, enabled}) / 'edit'(rule_id)
PolicyConditionEditorProps { modelValue: PolicyCondition; fields_available: string[] }   // 可判定字段由后端下发
  emits: 'update:modelValue'(PolicyCondition) / 'validate'(ok: boolean)
PolicyDiffViewProps        { diff: PolicyDiffEntry[]; mode: 'side-by-side' | 'inline' }
BudgetOverrideFormProps    { defaults: BudgetDefaults; override: Partial<BudgetDefaults>; editable: boolean }
  emits: 'update:override'(Partial<BudgetDefaults>) / 'submit'
BenchmarkMetricsCardsProps { metrics: BenchmarkMetrics; baseline: BenchmarkMetrics | null }   // null = 基线未写
BenchmarkCaseResultTableProps { results: BenchmarkCaseResult[]; loading: boolean }
EvidenceEventTableProps    { items: EvidenceEventRead[]; total: number; loading: boolean; filters: EvidenceFilter }
  emits: 'update:filters'(EvidenceFilter) / 'select'(event_id)
EvidenceEventDetailProps   { event: EvidenceEventRead | null; block: EvidenceLedgerBlockRead | null }
LedgerVerifyPanelProps     { result: LedgerVerifyResult | null; verifying: boolean }
  emits: 'verify'({from_seq, to_seq})
```

### 3.3 store actions（`useGovernanceStore`，`src/stores/governance.ts`）

```ts
fetchRules(patch?: Partial<PolicyRuleFilter>): Promise<void>
upsertRule(payload: PolicyRuleWrite): Promise<PolicyRuleRead>
toggleRule(rule_id: number, enabled: boolean): Promise<PolicyRuleRead>
fetchVersions(): Promise<void>;  submitVersion(): Promise<PolicyVersionRead>
approveVersion(version: number, idempotency_key: string): Promise<PolicyVersionRead>
rejectVersion(version: number, comment: string, idempotency_key: string): Promise<PolicyVersionRead>
fetchBudgetDefaults(): Promise<BudgetDefaults>
updateBudgetOverride(patch: Partial<BudgetDefaults>): Promise<BudgetDefaults>
fetchSuites(): Promise<void>;  fetchCases(suite_id: number): Promise<void>
triggerBenchmarkRun(suite_id: number, cache_mode: boolean, idempotency_key: string): Promise<BenchmarkRunRead>
fetchBenchmarkRun(run_id: number): Promise<BenchmarkRunRead>
fetchEvents(patch?: Partial<EvidenceFilter>): Promise<void>;  fetchEvent(event_id: number): Promise<EvidenceEventRead>
verifyLedger(scope: { from_seq: number | null; to_seq: number | null }): Promise<LedgerVerifyResult>
reset(): void
```

## 4. 数据模型

本模块**不拥有任何表**，只读后端 DTO；字段名与《核心数据字典》§6 §7 §8 逐字一致。

| 表 | 动作 | 前端可见列 |
|---|---|---|
| `policy_rule` | 读；写经变更集 | `id` `code` `version` `level` `title` `description` `condition` `required_slots` `enabled` `updated_by` |
| `risk_policy_version` | 读；写经审批 | `version` `diff` `approved_by` `effective_scope` `effective_at` |
| `benchmark_suite` / `benchmark_case` | 只读 | `id` `name` `version` `layer` `description` `baseline` `created_by` / `id` `suite_id` `name` `category` `input` `expected` `is_malicious` `enabled` `weight` |
| `benchmark_run` | 只读 | `id` `suite_id` `status` `model_version` `prompt_version` `cache_mode` `metrics` `case_results` `triggered_by` `started_at` `finished_at` |
| `evidence_event` | 只读 | `id` `seq` `project_id` `actor_id` `event_type` `subject_type` `subject_id` `artifact_hash` `payload` `prev_hash` `event_hash` `occurred_at` |
| `evidence_ledger_block` | 只读 | `seq` `from_event_seq` `to_event_seq` `chain_head_hash` `signature` `key_id` `signed_at` |

前端 DTO（追加到 `src/types/index.ts`，拆分约定同 fe-05 §4）：

```ts
export type RiskLevel = 'P0' | 'P1' | 'P2' | 'P3'
export type BenchmarkLayer = 'L0' | 'L1' | 'L2'
export type ApprovalSlot = 'checker_data' | 'checker_security'
export type PolicyVersionStatus = 'pending' | 'approved' | 'rejected'
export type BenchmarkRunStatus = 'running' | 'succeeded' | 'failed'

export interface PolicyRuleRead {
  id: number; code: string; version: number; level: RiskLevel; title: string; description: string
  condition: PolicyCondition; required_slots: ApprovalSlot[]; enabled: boolean
  updated_by: number | null; updated_by_name: string; updated_at: string
}
/** 未选择的条件键一律不写入；值域由后端下发的 fields_available 约束，前端不硬编码判定语义 */
export interface PolicyCondition {
  env?: ('dev' | 'staging' | 'prod')[]; writes_formal_table?: boolean
  sensitive_plaintext_egress?: boolean; bypass_approval_intent?: boolean
  cross_tenant_access?: boolean; resource_out_of_scope?: boolean
  budget_over_default_factor?: number; missing_rollback_plan?: boolean
  unregistered_secret_ref?: boolean; read_only?: boolean
}
export interface PolicyDiffEntry { rule_code: string; change: 'added' | 'removed' | 'modified'
  before: Partial<PolicyRuleRead> | null; after: Partial<PolicyRuleRead> | null; changed_fields: string[] }
export interface PolicyVersionRead {
  version: number; diff: PolicyDiffEntry[]; status: PolicyVersionStatus
  approved_by: number | null; approved_by_name: string; effective_at: string | null
  effective_scope: { project_ids: number[]; envs: ('dev' | 'staging' | 'prod')[] }
  submitted_by: number; submitted_by_name: string; submitted_at: string
}
export interface BudgetDefaults {
  max_read_rows: number; max_write_bytes: number; max_duration_sec: number
  max_amplification_ratio: number; max_reject_rate: number; warn_threshold_pct: number
}
export interface BenchmarkMetrics {
  gen_success_rate: number; gate_pass_rate: number; p0p1_block_rate: number
  false_positive_rate: number; recovery_rate: number
}
export interface BenchmarkSuiteRead {
  id: number; name: string; version: string; layer: BenchmarkLayer; description: string
  baseline: BenchmarkMetrics | null; created_by: number | null; case_count: number
}
export interface BenchmarkCaseRead {
  id: number; suite_id: number; name: string; category: string
  input: Record<string, unknown>; expected: Record<string, unknown>
  is_malicious: boolean; enabled: boolean; weight: number
}
export interface BenchmarkRunRead {
  id: number; suite_id: number; suite_name: string; suite_version: string; layer: BenchmarkLayer
  status: BenchmarkRunStatus; model_version: string; prompt_version: string; cache_mode: boolean
  metrics: BenchmarkMetrics; triggered_by: number | null; triggered_by_name: string
  started_at: string; finished_at: string | null
}
export interface BenchmarkCaseResult {
  case_id: number; case_name: string; category: string; is_malicious: boolean
  expected: Record<string, unknown>; actual: Record<string, unknown>
  passed: boolean; error_code: string | null
}
export interface EvidenceEventRead {
  id: number; seq: number; tenant_id: number; project_id: number | null
  actor_id: number | null; actor_name: string; event_type: string   // EvidenceEventType 取值，原样展示
  subject_type: string; subject_id: string; artifact_hash: string
  payload: Record<string, unknown>                                  // 已脱敏
  prev_hash: string; event_hash: string; occurred_at: string; block_seq: number | null
}
export interface EvidenceLedgerBlockRead { seq: number; from_event_seq: number; to_event_seq: number
  chain_head_hash: string; signature: string; key_id: string; signed_at: string }
export interface LedgerVerifyResult {
  ok: boolean; checked_events: number; checked_blocks: number
  broken_at_seq: number | null; broken_reason: string | null    // hash_mismatch / prev_hash_mismatch / signature_invalid / missing_event
  started_from_block_seq: number | null                         // null = 从创世条目起验
  finished_at: string
}
export interface EvidenceFilter {
  from?: string; to?: string; event_type?: string; actor_id?: number; project_id?: number
  subject_type?: string; cursor?: string; limit: number
}
```

## 5. 核心流程

**策略变更（提交 → 审批 → 生效）**：① `checker_security` 用结构化表单编辑规则 → `POST/PUT policy-rules`（写暂存变更）；② 提交变更集 → `POST policy-versions`（后端生成 `diff`，`status=pending`）；③ 另一位 `checker_security` 在 `/governance/policy-versions` 查看并排 Diff；④ approve → 写 `approved_by` 与 `effective_at`；reject → 不生效，列表标 `rejected`；⑤ 生效后 `policy-rules` 回显新 `version`，PDP（be-07）从新规则集求值。

**策略版本状态**：`pending` →approve→ `approved`（徽章 success，展示 `effective_at`，Diff 转只读）；`pending` →reject→ `rejected`（徽章 danger，展示驳回意见，「重新提交」回编辑页）；`approved` **不可再变更**——修改必须新建变更集（不可变原则）。**提交人不得自批**：版本列表对自提版本隐藏「通过」按钮并给出 reason tooltip，服务端以 `SELF_APPROVAL_FORBIDDEN` 兜底。

**Benchmark 触发与查看**：① `/governance/benchmark` 列出 Suite（layer / version / 基线 / case 数）；② 进入详情查看 Case（category / `is_malicious` / input / expected）；③ 「触发评测」→ 弹窗选 `cache_mode`（默认开启缓存回放，与《需求对齐记录》§二.2 一致）→ 生成 `Idempotency-Key` → `POST benchmark/runs`；④ `running` 时 5 s 轮询，`succeeded` 后渲染指标卡与结果表，`failed` 展示失败原因；⑤ 导出 JSON / CSV。

**证据账本查询与验链**：① `/governance/evidence` 按 `occurred_at` 倒序、**游标分页**（`cursor`，不用 offset）；② 过滤时间范围 / `event_type` / `actor_id` / `project_id` / `subject_type`；③ 点击事件 → 详情展示脱敏 payload、`prev_hash`/`event_hash`、所属 `block_seq` 与检查点签名信息；④ 「验链」→ `POST evidence/verify`（可限定 `from_seq`/`to_seq`）→ 链完整（绿）或断链位置（红，标出 `broken_at_seq` 与 `broken_reason`）；⑤ 导出 JSON / CSV（与当前筛选同口径）。

**验链必须由服务端独立完成**：前端只展示 `LedgerVerifyResult`，**绝不在浏览器内重算哈希**——浏览器无法访问 Ed25519 公钥与完整事件序列，前端重算得出的「通过」没有证据价值；独立验链脚本是 `scripts/verify_ledger.py`（be-06）。

## 6. 关键算法与规则

**`PolicyConditionEditor` 的结构化表单**：`condition` 是 `jsonb`，**不允许**在界面直接编辑 JSON 文本（易写出 PDP 无法求值的结构）。控件映射：`env` → 多选（`dev`/`staging`/`prod`）；`writes_formal_table` / `sensitive_plaintext_egress` / `bypass_approval_intent` / `cross_tenant_access` / `resource_out_of_scope` / `missing_rollback_plan` / `unregistered_secret_ref` / `read_only` → 开关（boolean）；`budget_over_default_factor` → 数字输入（> 0）。

- **未选择的条件键一律不写入 `condition`**（不是写 `false`）——`false` 与「未约束」在 PDP 求值语义上可能不同，前端不得替后端决定。
- 允许的键来自后端 `fields_available`；出现未知键时以「未知条件项（只读 JSON）」只读展示并阻止提交，避免静默丢弃策略。
- `required_slots` 只允许 `checker_data` / `checker_security` 的组合，且 `P3` 应无必填槽（与 `RiskLevel.min_approval_slots` 一致）；不一致时给警告（不阻断，最终由后端校验）。

**预算默认值与一致性提示**。三方对照：冻结默认值（`max_read_rows` `1e8` / `max_write_bytes` `50 GiB`=53687091200 / `max_duration_sec` `7200` / `max_amplification_ratio` `1.5` / `max_reject_rate` `0.05`（演示场景 `0.20`） / `warn_threshold_pct` `80`）、`SUP_MAX_*` 默认值（同左）、项目覆盖（可空）。提示规则（**提示而非拦截**，最终由后端与 PDP 判定）：① 覆盖 **> 冻结默认值** → 橙色提示「高于冻结默认值，将放宽本项目执行预算，P1 判定阈值（默认 3 倍）随之变化」；② 覆盖 **< `SUP_MAX_*` 硬上限** → 允许（收紧是安全方向）；③ 覆盖 **> `SUP_MAX_*`** → 红色提示并禁止提交（前端拦截 + 后端拒绝双保险）；④ `warn_threshold_pct` 偏离 `80` → 提示「与《需求对齐记录》冻结值不一致」。字节类字段提供 MiB/GiB 单位选择，提交前统一换算为 `bigint` 字节，界面始终回显人类可读值与原始字节数。

**Benchmark 五项指标卡与基线对比**

| 指标 | 含义 | 越高越好 | 对比标记规则 |
|---|---|---|---|
| `gen_success_rate` | 生成成功率 | 是 | 低于基线红↓ / 持平（±0.5%）灰— / 高于绿↑ |
| `gate_pass_rate` | 门禁通过率 | 是 | 同上 |
| `p0p1_block_rate` | P0/P1 拦截率 | 是 | **目标 100%**：`< 1.0` 恒红并加「未达 100% 拦截」，**不参与基线比较** |
| `false_positive_rate` | 误报率 | **否** | 高于基线红↑ |
| `recovery_rate` | 恢复成功率 | 是 | 同 `gen_success_rate` |

`baseline === null`（首次跑完前）不显示对比标记，只显示当前值并标注「基线尚未写入」——不伪造对比结论。

**逐条 case 结果表**：列 = Case 名称 / `category` / `is_malicious` 标记 / 期望摘要 / 实际摘要 / 结果 / `error_code`。`passed === false` 整行浅红底 + 左侧红色竖条；`is_malicious === true` 且失败额外加「安全用例未拦截」角标（这是拦截率不达标的直接原因）。期望与实际各取 2–3 个关键键（`should_block` / `error_code` / `should_interrupt`）以键值对渲染，完整对象放 tooltip 避免表格横向爆炸。表头提供「只看失败」「只看恶意用例」筛选。

**证据事件详情展示规范**：`seq` 等宽字体并与所属 `block_seq` 并列；`event_type` **原样展示**（如 `approval.granted`），**不做中文翻译**——审计口径必须与代码枚举逐字一致；`actor_name` / `actor_id` 并列；`subject_type` / `subject_id` 可点击跳转（目标路由存在时）；`artifact_hash` 前 8 位 + 悬停完整值；`payload` 为只读键值树（**已脱敏**），空对象显示「无附加详情」；`prev_hash` / `event_hash` 等宽、可复制并并排展示以便肉眼比对链尾；`occurred_at` 为 `YYYY-MM-DD HH:mm:ss.SSS`（本地时区），悬停显示 UTC。前端**不做** payload 关键字的二次脱敏猜测，也不提供「显示隐藏字段」开关。

**导出**：`format=json` / `format=csv` 均由**后端生成**（列顺序、编码、脱敏口径由后端保证），前端只发起下载请求并透传 `Content-Disposition`。前端**不在浏览器内拼 CSV**，避免列顺序与后端不一致、也避免把未脱敏的中间态写进本地文件。导出按钮点击后提示「导出将写入证据账本（导出行为本身也被审计）」。

## 7. 错误码与拒绝语义

| 触发条件 | 错误码（`ErrorCode`） | HTTP | 前端表现 | 写账本 |
|---|---|---|---|---|
| 提交自己提交的策略变更审批 | `SELF_APPROVAL_FORBIDDEN` | 403 | 「通过」按钮本已隐藏；若服务端拒绝则提示原因 | 是 |
| 同一人同时持提交与审批职责 | `DUTY_CONFLICT` | 403 | 提示职责冲突，引导去 `/settings/roles` 查看角色矩阵 | 是 |
| P0/P1 规则被禁用或改写为不阻断 | `POLICY_BLOCKED` | 403 | 红色警示条「禁止削弱 P0/P1 拦截能力」；变更集不提交 | 是（`policy.blocked`） |
| `condition` 结构 PDP 无法求值 | `VALIDATION_ERROR` | 422 | 表单定位到具体条件项，`details.fields` 逐项渲染 | 否 |
| 项目预算覆盖超 `SUP_MAX_*` | `VALIDATION_ERROR` | 422 | 前端已拦截；收到该码说明前后端上限不一致 → 提示并刷新默认值 | 否 |
| 版本不存在 / 已生效后修改 | `NOT_FOUND` / `VERSION_IMMUTABLE` | 404 / 409 | 提示「已生效版本不可修改，请新建变更集」 | 是 |
| 变更集已被他人审批 | `CONFLICT` | 409 | 提示状态已变更并刷新版本列表 | 是 |
| 非 `checker_security` 触发评测 | `FORBIDDEN` / `ROLE_REQUIRED` | 403 | 「触发评测」按钮隐藏；页面其余部分只读可见 | 是 |
| 评测触发的幂等键冲突 | `IDEMPOTENCY_CONFLICT` | 409 | 提示冲突并停止重试 | 否 |
| 评测依赖的 LLM 不可用 | `LLM_UNAVAILABLE` | 503 | 评测 Run 标 failed、`case_results` 为空；提示「模型不可用，请开启缓存回放后重试」 | 是 |
| 验链发现断链 | 无错误码（`LedgerVerifyResult.ok = false`） | 200 | 红色结果卡：`broken_at_seq` + `broken_reason` + 联系安全负责人引导；导出仍可用 | 由 be-06 决定 |
| 事件不属于当前租户 | `TENANT_MISMATCH` | 403 | 事件详情关闭，列表回退到本租户 | 是 |
| 导出范围过大被限流 | `RATE_LIMITED` | 429 | 提示缩小时间范围后重试 | 否 |

分支基于 `BizError.code`，**不用 `message` 判断**。

## 8. 安全与合规约束

- **策略编辑是安全敏感操作**：`checker_security` 是唯一可写角色；写操作一律带 `Idempotency-Key`；变更集必须经第二人审批才生效。提交前界面展示「本次变更影响的环境与项目范围」并要求填写变更理由。
- **禁止在界面内直接编辑 JSON**：`PolicyConditionEditor` 只出结构化控件；任何「高级模式 / 直接改 JSON」入口都会让策略绕过结构校验，属设计外。
- **账本不可变**：对 `evidence_event` 与 `evidence_ledger_block` 只有 GET；前端不得提供编辑、删除、补录、重算哈希的入口。
- **脱敏不可绕过**：`payload` 是后端脱敏后的结果，前端不做反转、不做拼接、不猜测被掩码的字段；导出文件同样只含脱敏内容。
- **审计可见**：导出、验链、策略审批都必须在界面上明示「本次操作已/将被写入证据账本」。
- **角色显式化**：`auditor` 访问本模块时全页面只读（无写按钮、无「触发评测」），页面顶部以信息条说明当前为只读身份；服务端对全部写接口仍做角色校验。
- **未生效的变更集不外泄**：只读角色可见已生效规则清单（便于审计），但不展示 pending 变更集内容，避免提前泄露策略调整意图。

## 9. 测试要点

- **单元测试（Vitest）**：`PolicyConditionEditor` 在「未选择」状态下该键从 `modelValue` **移除**而非置 `false`，出现未知条件键时表单只读并阻止提交；`BenchmarkMetricsCards` 在 `p0p1_block_rate = 0.98` 时恒红且显示「未达 100% 拦截」，`baseline === null` 时不渲染任何箭头；`false_positive_rate` 对比方向与其他四项相反（高于基线为红↑）；`BudgetOverrideForm` 覆盖值超 `SUP_MAX_*` 时提交禁用 + 红色提示，字节换算正确（`1 GiB → 1073741824`）；`LedgerVerifyPanel` 在 `ok=false` 时渲染 `broken_at_seq` 与 `broken_reason`，`ok=true` 时展示 `checked_events` / `checked_blocks` / `started_from_block_seq`；`EvidenceEventDetail` 的 `event_type` 原样展示（断言 DOM 中不含中文译名），`payload` 为 `{}` 时显示「无附加详情」；导出不产生任何本地拼接的 CSV 字符串（断言只发请求）。
- **集成测试（真实 PG + 后端，ECS 上跑）**：**策略变更四眼**（A 提交 → A 自批 403 → B 审批通过后 `effective_at` 落值、`policy_rule.version` 递增、PDP 用新规则求值）；**P0 削弱拦截**（把写正式表的 P0 规则改为 `disabled` → 变更集提交被拒 `POLICY_BLOCKED`）；**账本验链**（正常链 `ok=true`；篡改某条 `payload` → `broken_at_seq` 精确定位；删除中间事件 → `broken_reason = missing_event`）；**签名检查点**（写满 100 条后存在 `evidence_ledger_block`，其 `chain_head_hash` 等于第 100 条的 `event_hash` 且签名可用公钥验证）；**Benchmark L0**（含 `is_malicious=true` 的 suite → `p0p1_block_rate` 计算正确、`case_results` 长度等于启用 case 数、`cache_mode=true` 时两次跑指标一致）；**越权**（auditor 触发评测 → 403 且界面无按钮）；**幂等**（同 key 连点触发 → 一条 `benchmark_run`）。
- **必须覆盖的反例**：策略自批、职责混用、削弱 P0 拦截、断链检测、越权触发评测、账本 payload 无敏感明文（响应体连续扫描断言）。
- **测试基建**：验链集成用例断言 API 返回与 `scripts/verify_ledger.py`（be-06）结果一致，证明**浏览器不是验链的事实来源**。

## 10. 未决与风险

| 事项 | 现状 | 建议 / 验证方式 |
|---|---|---|
| 策略变更的审批人资格 | PRD §6.6 只说「必须经过审批」，未指明谁批 | 建议另一名 `checker_security`（同槽不同人），与四眼一致；若允许 `auditor` 审批需先改 PRD |
| `risk_policy_version` 缺 `status` 列 | 数据字典与 ORM 均无该列（只有 `approved_by` / `effective_at`） | 前端需要三态；建议后端由 `approved_by is null` + 是否落 `effective_at` 折算下发，或新增 `status` 列（需 migration） |
| 预算覆盖的存储位置与是否需审批 | 无对应表；预算放宽直接影响 P1 判定阈值，但 PRD 只对「安全策略变更」要求审批 | 待 be-07 明确响应形状；建议与策略变更走同一变更集流程，若不要求则前端至少做二次确认 + 变更理由输入 |
| `PolicyCondition` 值域 | 本文按《需求对齐记录》§四的触发条件枚举 10 个键 | 需 be-07 下发权威 `fields_available` 并从代码生成，避免前端硬编码漂移 |
| Benchmark 触发后的进度粒度 | `benchmark_run` 无进度字段，只有 `status` | 前端先做「running 转圈 + 已用时」；若要逐条进度需后端加进度列（建议用已完成 case 数） |
| `benchmark_run` 无 `tenant_id` / `project_id` | ORM 中该表未继承 `TenantMixin` | 多租户下「谁能看到哪次评测」需后端明确过滤口径；前端不做假设 |
| 导出范围与超时 | 账本永久保留，全量导出可能极大 | 建议后端强制时间范围上限（如 30 天）并支持流式下载；前端在未选时间范围时禁用导出 |
| `event_type` 是否翻译 | 本文按审计口径不翻译 | 若可用性验证表明需要，建议在 tooltip 补中文说明，主字段保持枚举原值 |
| 前端是否需要验签 | 不需要，验链由独立脚本完成 | M4 交付时随账本导出附验链脚本，前端只展示结果 |

## 11. 变更记录

| 日期 | 变更 | 原因 |
|---|---|---|
| 2026-10-16 | 初稿：策略结构化编辑与变更审批、预算覆盖一致性提示、Benchmark 五项指标与基线对比、账本查询与独立验链 | M4 生产化前端落地 |
