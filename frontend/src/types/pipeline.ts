/**
 * Pipeline Studio 类型（fe-04 / be-09 be-10 be-11 be-12）。
 *
 * 红线：前端不生成、不修复、不校验、不计算指纹。EtlPlan / HOCON / 字段映射
 * 全部只读呈现；sha256 只展示后端冻结时返回的值。
 */

import type { ErrorCodeLabel } from './run'

/** 16 值，与 backend/models/enums.py 的 PipelineStatus 逐字一致 */
export type PipelineStatus =
  | 'draft'
  | 'generated'
  | 'gate_failed'
  | 'version_frozen'
  | 'preparing'
  | 'pending_approval'
  | 'approved'
  | 'committable'
  | 'queued'
  | 'running'
  | 'succeeded'
  | 'failed'
  | 'interrupted'
  | 'rolling_back'
  | 'rolled_back'
  | 'rejected'

/**
 * 页面临时阶段（不落库）。fe-04 §10：phase 只表达「当前请求周期」的过程态，
 * 持久状态一律用 PipelineStatus，二者映射由后端给，前端不自行推导。
 */
export type StudioPhase =
  | 'idle'
  | 'parsing'
  | 'awaiting_clarification'
  | 'generating'
  | 'gate_running'
  | 'gate_failed'
  | 'ready_to_freeze'
  | 'frozen'
  | 'submitted'
  | 'error'

/* ------------------------------------------------------------------ */
/* Pipeline 与对话                                                     */
/* ------------------------------------------------------------------ */

export interface Pipeline {
  id: number
  project_id: number
  name: string
  description: string
  status: PipelineStatus
  current_version_id: number | null
  source_data_source_id: number | null
  target_data_source_id: number | null
  created_at: string
  updated_at: string
}

/** 源/目标下拉选项：**不含** endpoint 与 secret_ref（fe-04 §8） */
export interface DataSourceOption {
  id: number
  name: string
  type: string
  env: string
}

export type ConversationMessageKind =
  | 'chat'
  | 'clarification_question'
  | 'clarification_answer'
  | 'plan_summary'
  | 'gate_summary'

export interface ConversationMessage {
  id: number
  role: 'user' | 'assistant' | 'system' | 'tool'
  content: string
  message_kind: ConversationMessageKind
  meta: Record<string, unknown>
  created_at: string
  created_by_display_name: string | null
}

export type ClarificationKind =
  | 'text'
  | 'single_choice'
  | 'multi_choice'
  | 'field_ref'
  | 'threshold'

export interface ClarificationQuestion {
  key: string
  question: string
  kind: ClarificationKind
  options: Array<{ value: string; label: string }> | null
  required: boolean
  hint: string | null
  /** threshold 型的取值范围；缺失时按 §6.1 兜底并标注「范围待确认」 */
  meta?: { min?: number; max?: number; unit?: string; ratio?: boolean }
}

export interface PipelineStudioDraft {
  name: string
  requirement: string
  source_data_source_id: number | null
  target_data_source_id: number | null
}

export interface StartGenerationPayload {
  requirement: string
  source_data_source_id: number
  target_data_source_id: number
}

/**
 * 生成工作流快照（契约待 be-09 冻结）。
 *
 * 带 `thread_id` 是刻意的：发起生成的响应必须能告诉前端「这是哪条线程」，
 * 否则刷新页面后无从恢复。恢复时用同一个 thread_id 再取一次这个结构。
 */
export interface WorkflowSnapshot {
  thread_id: string
  phase: StudioPhase
  phase_message: string | null
  pending_questions: ClarificationQuestion[]
  autofix_round: number
  last_error: string | null
}

/* ------------------------------------------------------------------ */
/* EtlPlan                                                             */
/* ------------------------------------------------------------------ */

export interface EtlPlanInput {
  id: string
  data_source_id: number
  schema: string
  table: string
}

export interface EtlPlanOutput {
  id: string
  data_source_id: number
  database: string
  table: string
  mode: 'shadow_then_swap'
}

export type FieldTransform = 'none' | 'cast' | 'trim' | 'concat' | 'mask' | 'hash'

export type FieldChange = 'same' | 'added' | 'removed' | 'modified'

export type MaskingMethod = 'mask_middle' | 'hash' | 'truncate'

export interface FieldMasking {
  method: MaskingMethod
  params: Record<string, unknown>
}

export interface FieldMapping {
  source_field: string
  source_type: string
  target_field: string
  target_type: string
  transform: FieldTransform
  masking: FieldMasking | null
  quality_rule_ids: string[]
  change: FieldChange
}

export interface EtlTransform {
  id: string
  kind: string
  input_fields: string[]
  output_field: string
  params: Record<string, unknown>
}

export interface EtlFilter {
  id: string
  expression: string
  description: string
}

export type QualityRuleType = 'not_null' | 'regex' | 'range' | 'enum' | 'type_cast'

export interface QualityRule {
  id: string
  field: string
  rule_type: QualityRuleType
  params: Record<string, unknown>
  error_code: ErrorCodeLabel
  on_violation: 'reject' | 'warn'
  enabled: boolean
}

export type DagNodeType = 'source' | 'transform' | 'filter' | 'quality' | 'sink'

export interface EtlPlanDagNode {
  id: string
  type: DagNodeType
  label: string
  meta: Record<string, unknown>
  /** 后端可能不给；不给时前端按最长路径分层布局（§6.2） */
  position?: { x: number; y: number }
}

export interface EtlPlanDagEdge {
  id: string
  source: string
  target: string
  label: string | null
}

export interface EtlPlanDag {
  nodes: EtlPlanDagNode[]
  edges: EtlPlanDagEdge[]
}

/** 对应 runtime_supervision_contract 的六项 */
export interface RuntimeBudget {
  max_read_rows: number
  max_write_bytes: number
  max_duration_sec: number
  max_amplification_ratio: number
  max_reject_rate: number
  warn_threshold_pct: number
}

export interface EtlPlanBody {
  inputs: EtlPlanInput[]
  outputs: EtlPlanOutput[]
  field_mappings: FieldMapping[]
  transforms: EtlTransform[]
  filters: EtlFilter[]
  quality_rules: QualityRule[]
  dag: EtlPlanDag
  budget: RuntimeBudget
}

export interface EtlPlan {
  id: number
  schema_version: string
  plan: EtlPlanBody
  valid: boolean
  validation_errors: Array<{ path: string; reason: string }>
}

export interface HoconArtifact {
  id: number
  content: string
  sha256: string
  parser_version: string
  validation_result: Record<string, unknown>
}

/* ------------------------------------------------------------------ */
/* 门禁                                                                */
/* ------------------------------------------------------------------ */

export type GateConclusion = 'pass' | 'warn' | 'block'

export type GateSeverity = 'block' | 'warn' | 'info'

export type GateCategory = 'syntax' | 'schema' | 'field_mapping' | 'resource' | 'policy'

/** 四类定位点：决定「点 finding 跳到哪」 */
export type GateLocator =
  | { kind: 'hocon'; line: number; column: number; end_line?: number }
  | { kind: 'plan'; path: string }
  | { kind: 'dag_node'; node_id: string }
  | { kind: 'source_table'; data_source_id: number; schema: string; table: string }

export interface GateFinding {
  id: string
  severity: GateSeverity
  category: GateCategory
  /** ErrorCode 取值，如 HOCON_SYNTAX_ERROR；未知码展示原文 */
  code: string
  /** 后端已脱敏文案 */
  message: string
  locator: GateLocator
  suggestion: string | null
}

export interface AutofixRound {
  /** 1..LLM_AUTOFIX_MAX_ROUNDS(3) */
  round: number
  trigger_finding_ids: string[]
  /** unified diff 文本 */
  hocon_diff: string
  plan_diff: string | null
  resolved_finding_ids: string[]
  created_at: string
}

export interface GateResult {
  evaluated_at: string
  conclusion: GateConclusion
  findings: GateFinding[]
  autofix_rounds: AutofixRound[]
  autofix_exhausted: boolean
}

/* ------------------------------------------------------------------ */
/* 版本                                                                */
/* ------------------------------------------------------------------ */

export interface GenerationContext {
  model_name: string
  model_version: string
  prompt_template_version: string
  autofix_rounds: number
}

export interface PipelineVersionSummary {
  id: number
  version_no: number
  sha256: string
  status_note: string
  frozen_at: string
  frozen_by_display_name: string | null
  source_metadata_profile_version: number | null
  generation_context: GenerationContext
}

/** 冻结二次确认弹窗展示的制品清单（与 pipeline_version 外键一一对应） */
export interface FreezeArtifactManifest {
  plan: { schema_version: string; valid: boolean }
  hocon: { sha256: string; parser_version: string }
  quality_contract: { rule_count: number; reject_rate_budget: number }
  supervision: RuntimeBudget
  metadata_profile: { version: number; sha256: string; expires_at: string | null }
}

export interface PipelineVersionDetail extends PipelineVersionSummary {
  manifest: FreezeArtifactManifest
  plan: EtlPlan
  hocon: HoconArtifact
}

/** Pipeline 列表筛选 */
export interface PipelineFilter {
  /** 多选：一次看多个状态（如「失败 + 硬中断」） */
  status?: PipelineStatus[]
  q?: string
  page: number
  page_size: number
}
