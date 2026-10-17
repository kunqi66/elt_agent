/**
 * 安全治理与 Benchmark 类型（fe-07 / be-06 be-07 be-20）。
 *
 * 红线：账本只读。前端不重算哈希、不提供编辑/删除/补录入口；
 * 验链一律由服务端（scripts/verify_ledger.py 同源逻辑）完成，浏览器不是验链的事实来源。
 */

import type { ApprovalSlot, Env, RiskLevel } from './common'

export type BenchmarkLayer = 'L0' | 'L1' | 'L2'

export type BenchmarkRunStatus = 'running' | 'succeeded' | 'failed'

export type PolicyVersionStatus = 'pending' | 'approved' | 'rejected'

/* ------------------------------------------------------------------ */
/* 策略规则                                                            */
/* ------------------------------------------------------------------ */

/**
 * PDP 的触发条件。**未选择的条件键一律不写入**（不是写 false）——
 * false 与「未约束」在 PDP 求值语义上可能不同，前端不得替后端决定。
 */
export interface PolicyCondition {
  env?: Env[]
  writes_formal_table?: boolean
  sensitive_plaintext_egress?: boolean
  bypass_approval_intent?: boolean
  cross_tenant_access?: boolean
  resource_out_of_scope?: boolean
  budget_over_default_factor?: number
  missing_rollback_plan?: boolean
  unregistered_secret_ref?: boolean
  read_only?: boolean
}

/** 可判定的条件键由后端下发，前端不硬编码判定语义 */
export type PolicyConditionKey = keyof PolicyCondition

export interface PolicyRuleRead {
  id: number
  code: string
  version: number
  level: RiskLevel
  title: string
  description: string
  condition: PolicyCondition
  required_slots: ApprovalSlot[]
  enabled: boolean
  updated_by: number | null
  updated_by_name: string
  updated_at: string
}

export interface PolicyRuleWrite {
  code: string
  level: RiskLevel
  title: string
  description: string
  condition: PolicyCondition
  required_slots: ApprovalSlot[]
  enabled: boolean
}

export interface PolicyRuleFilter {
  /**
   * 生效阶段：gate（生成期门禁）/ prepare（准备期）/ commit / runtime（运行期监督）。
   *
   * 注：`policy_rule` 表当前没有 stage 列，这是 6 项跨模块语义缺口之一，
   * 需后端在 be-14 补齐（否则规则无法按阶段分组展示）。
   */
  stage?: string
  risk_level?: RiskLevel[]
  enabled?: boolean
  q?: string
  page: number
  page_size: number
}

export interface PolicyDiffEntry {
  rule_code: string
  change: 'added' | 'removed' | 'modified'
  before: Partial<PolicyRuleRead> | null
  after: Partial<PolicyRuleRead> | null
  changed_fields: string[]
}

export interface PolicyVersionRead {
  version: number
  diff: PolicyDiffEntry[]
  status: PolicyVersionStatus
  approved_by: number | null
  approved_by_name: string
  effective_at: string | null
  effective_scope: { project_ids: number[]; envs: Env[] }
  submitted_by: number
  submitted_by_name: string
  submitted_at: string
  /** 驳回意见；status='rejected' 时有值 */
  reject_comment?: string | null
}

/* ------------------------------------------------------------------ */
/* 预算默认值                                                          */
/* ------------------------------------------------------------------ */

export interface BudgetDefaults {
  max_read_rows: number
  max_write_bytes: number
  max_duration_sec: number
  max_amplification_ratio: number
  max_reject_rate: number
  warn_threshold_pct: number
}

/** 三方对照：冻结默认值 / SUP_MAX_* 硬上限 / 项目覆盖 */
export interface BudgetDefaultsRead {
  defaults: BudgetDefaults
  /** SUP_MAX_* 硬上限；覆盖超过它必须前端拦截 + 后端拒绝 */
  hard_limits: BudgetDefaults
  override: Partial<BudgetDefaults> | null
}

/* ------------------------------------------------------------------ */
/* Benchmark                                                           */
/* ------------------------------------------------------------------ */

export interface BenchmarkMetrics {
  gen_success_rate: number
  gate_pass_rate: number
  p0p1_block_rate: number
  false_positive_rate: number
  recovery_rate: number
}

export interface BenchmarkSuiteRead {
  id: number
  name: string
  version: string
  layer: BenchmarkLayer
  description: string
  /** null = 基线尚未写入，此时不显示任何对比标记 */
  baseline: BenchmarkMetrics | null
  created_by: number | null
  case_count: number
}

export interface BenchmarkCaseRead {
  id: number
  suite_id: number
  name: string
  category: string
  input: Record<string, unknown>
  expected: Record<string, unknown>
  is_malicious: boolean
  enabled: boolean
  weight: number
}

export interface BenchmarkRunRead {
  id: number
  suite_id: number
  suite_name: string
  suite_version: string
  layer: BenchmarkLayer
  status: BenchmarkRunStatus
  model_version: string
  prompt_version: string
  cache_mode: boolean
  metrics: BenchmarkMetrics
  triggered_by: number | null
  triggered_by_name: string
  started_at: string
  finished_at: string | null
  /** status='failed' 时的失败原因 */
  error_message?: string | null
}

export interface BenchmarkCaseResult {
  case_id: number
  case_name: string
  category: string
  is_malicious: boolean
  expected: Record<string, unknown>
  actual: Record<string, unknown>
  passed: boolean
  error_code: string | null
}

/* ------------------------------------------------------------------ */
/* 证据账本                                                            */
/* ------------------------------------------------------------------ */

export interface EvidenceEventRead {
  id: number
  seq: number
  tenant_id: number
  project_id: number | null
  actor_id: number | null
  actor_name: string
  /** EvidenceEventType 取值，**原样展示**，审计口径必须与代码枚举逐字一致 */
  event_type: string
  subject_type: string
  subject_id: string
  artifact_hash: string
  /** 已脱敏 */
  payload: Record<string, unknown>
  prev_hash: string
  event_hash: string
  occurred_at: string
  block_seq: number | null
}

export interface EvidenceLedgerBlockRead {
  seq: number
  from_event_seq: number
  to_event_seq: number
  chain_head_hash: string
  signature: string
  key_id: string
  signed_at: string
}

export type LedgerBreakReason =
  | 'hash_mismatch'
  | 'prev_hash_mismatch'
  | 'signature_invalid'
  | 'missing_event'

export interface LedgerVerifyResult {
  ok: boolean
  checked_events: number
  checked_blocks: number
  broken_at_seq: number | null
  broken_reason: LedgerBreakReason | null
  /** null = 从创世条目起验 */
  started_from_block_seq: number | null
  finished_at: string
}

export interface EvidenceFilter {
  /** 必填：账本按租户 + 项目隔离，缺省查询会被后端拒绝 */
  project_id: number
  event_type?: string[]
  /** 主体标识，形如 `preparation:42` / `pipeline_version:7` */
  subject_id?: string
  pipeline_version_id?: number
  execution_run_id?: number
  from?: string
  to?: string
  /** 游标分页，不用 offset */
  cursor?: string
  limit: number
}

export interface EvidencePage {
  items: EvidenceEventRead[]
  next_cursor: string | null
}
