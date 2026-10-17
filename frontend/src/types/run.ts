/**
 * 执行与质量类型（fe-06 / be-14 be-16 be-17 be-18 be-19）。
 *
 * 红线：错误原文不可达。error_record 只下发 payload_digest（哈希 + 截断），
 * 前端不提供任何「查看原文 / 导出明细」入口，也不从 failure_snapshot 里提取疑似原文。
 */

import type { ExecutionBudget } from './approval'

export type RunStatus =
  | 'queued'
  | 'running'
  | 'succeeded'
  | 'failed'
  | 'paused'
  | 'cancelled'
  | 'interrupted'
  | 'rolling_back'
  | 'rolled_back'

/** 质量分流错误码，8 值，与 backend/models/enums.py 逐字一致 */
export type ErrorCodeLabel =
  | 'E_TYPE_CAST'
  | 'E_NULL_PK'
  | 'E_REGEX_MISMATCH'
  | 'E_RANGE_VIOLATION'
  | 'E_ENUM_INVALID'
  | 'E_DUPLICATE_KEY'
  | 'E_SENSITIVE_PLAINTEXT'
  | 'E_SCHEMA_DRIFT'

export interface RunSummary {
  id: number
  project_id: number
  pipeline_id: number
  pipeline_name: string
  pipeline_version_id: number
  version_no: number
  artifact_sha256: string
  status: RunStatus
  formal_table: string | null
  triggered_by: number | null
  triggered_by_name: string
  started_at: string | null
  finished_at: string | null
  duration_sec: number
  read_rows: number
  written_rows: number
  written_bytes: number
  reject_rows: number
  reject_rate: number
  /** 服务端算好的前置校验；前端不推演状态机 */
  can_pause: boolean
  can_resume: boolean
  can_cancel: boolean
  can_rollback: boolean
}

export interface RunDetail extends RunSummary {
  preparation_id: number
  seatunnel_job_id: string | null
  shadow_table: string | null
  error_table: string | null
  avg_throughput: number
  amplification_ratio: number
  error_message: string | null
  /** 最后 N 个指标点 + 错误摘要的键值快照；结构待 be-16 冻结 */
  failure_snapshot: Record<string, unknown>
  budget: ExecutionBudget
  rollback_plan: RollbackPlanLite
}

/** 回滚方案在运行详情里只需要步骤与清理开关 */
export interface RollbackPlanLite {
  steps: Array<{ order: number; action: string; tool: string; description: string }>
  shadow_cleanup: boolean
  restore_scope: string
}

export interface ExecutionMetricPoint {
  ts: string
  read_rows: number
  written_rows: number
  written_bytes: number
  duration_sec: number
  throughput: number
  amplification_ratio: number
  reject_rate: number
}

export interface QualityReport {
  run_id: number
  valid_rows: number
  rejected_rows: number
  reject_rate: number
  error_code_dist: Partial<Record<ErrorCodeLabel, number>>
  rule_hits: Record<string, number>
  passed: boolean
  diagnostics: Array<{
    rule_id: string
    field: string
    error_code: ErrorCodeLabel
    summary: string
  }>
  generated_at: string
}

export interface ErrorRecordRead {
  id: number
  rule_id: string
  error_code: ErrorCodeLabel
  field_name: string
  /** 唯一可见的「内容」标识（SHA256 + 字段长度），原文永不下发 */
  payload_digest: string
  reason: string
  occurred_at: string
}

export interface RunLogLine {
  /** 单调递增序号；增量拉取的游标就是它，也是稳定排序的依据 */
  seq: number
  ts: string
  level: 'debug' | 'info' | 'warning' | 'error'
  event_type: string
  /** 已由后端 structlog 脱敏处理器改写 */
  message: string
  subject_type?: string
  subject_id?: string
}

export interface Diagnosis {
  conclusion: string
  confidence: 'deterministic' | 'llm_assisted'
  evidence: Array<{ signal: string; value: string; source_table: string; source_id: string }>
  suggestions: Array<{ action: string; target_route: string | null }>
  /** LLM_UNAVAILABLE 时为 true；此时仍完整渲染 evidence 与 suggestions */
  degraded: boolean
}

export interface RunFilter {
  status?: RunStatus[]
  pipeline_id?: number
  from?: string
  to?: string
  /** 关键字：匹配 pipeline 名 / run_id */
  q?: string
  page: number
  page_size: number
}

/**
 * 日志增量拉取条件。
 * 用 `since_seq` 游标而非 offset：日志在运行中持续追加，
 * offset 分页会在追加时漏行或重行（fe-06 §6）。
 */
export interface RunLogFilter {
  /** 只取 seq 大于该值的行；首次拉取不传 */
  since_seq?: number
  level?: RunLogLine['level']
  event_type?: string
  q?: string
  /** 单次拉取上限；服务端可能截断，用返回的 truncated 判断是否要继续追 */
  limit?: number
}

/** 增量拉取结果：单页可能被服务端截断，用 truncated 提示「还有更多」 */
export interface IncrementalPage<T> {
  items: T[]
  truncated: boolean
}

/** 终态集合：停止轮询的判据 */
export const RUN_TERMINAL_STATES: readonly RunStatus[] = [
  'succeeded',
  'failed',
  'cancelled',
  'interrupted',
  'rolled_back',
]

export function isRunTerminal(status: RunStatus | null | undefined): boolean {
  return status != null && RUN_TERMINAL_STATES.includes(status)
}
