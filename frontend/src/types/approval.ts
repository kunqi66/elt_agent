/**
 * 准备单与审批类型（fe-05 / be-13）。
 *
 * 红线：frontend 不做权限判定，不做乐观更新。my_slot / can_commit 一律取后端。
 */

import type { ApprovalSlot, Env, RiskLevel, SensitiveLevel } from './common'

export type PreparationStatus =
  | 'pending'
  | 'partially_approved'
  | 'approved'
  | 'rejected'
  | 'expired'
  | 'superseded'

export type ApprovalDecisionType = 'approve' | 'reject' | 'return'

/** 影响范围：读哪些库表、写哪个目标表；不含任何凭据 */
export interface ResourceScope {
  reads: Array<{ data_source_id: number; database: string; table: string }>
  writes: Array<{
    data_source_id: number
    database: string
    table: string
    mode: 'shadow_then_swap'
  }>
}

export interface ExecutionBudget {
  max_read_rows: number
  max_write_bytes: number
  max_duration_sec: number
  max_amplification_ratio: number
  max_reject_rate: number
  warn_threshold_pct: number
}

export interface RollbackPlan {
  steps: Array<{ order: number; action: string; tool: string; description: string }>
  shadow_cleanup: boolean
  restore_scope: string
}

export interface RiskReason {
  code: string
  level: RiskLevel
  title: string
  detail: string
}

export interface ApprovalDecisionRead {
  slot: ApprovalSlot
  approver_id: number
  approver_name: string
  approver_role: string
  decision: ApprovalDecisionType
  comment: string
  artifact_sha256: string
  fingerprint_at_decision: string
  decided_at: string
  /** 当前指纹 ≠ fingerprint_at_decision 时后端置 true */
  stale: boolean
}

export type PreparationTimelineEvent =
  | 'created'
  | 'approved_slot'
  | 'rejected_slot'
  | 'returned_slot'
  | 'expired'
  | 'superseded'
  | 'committed'

export interface PreparationTimelineItem {
  at: string
  event: PreparationTimelineEvent
  slot?: ApprovalSlot
  actor_name?: string
  detail?: string
}

export interface PreparationSummary {
  id: number
  project_id: number
  pipeline_id: number
  pipeline_name: string
  pipeline_version_id: number
  version_no: number
  artifact_sha256: string
  input_fingerprint: string
  risk_level: RiskLevel
  sensitive_level: SensitiveLevel
  environment: Env
  status: PreparationStatus
  required_slots: ApprovalSlot[]
  decided_slots: ApprovalSlot[]
  created_by: number
  creator_name: string
  frozen_at: string
  expires_at: string | null
  /** 当前用户可审的槽；null = 不可审 */
  my_slot: ApprovalSlot | null
  /** 服务端算好的 Commit 可见性 */
  can_commit: boolean
}

export interface PreparationDetail extends PreparationSummary {
  resource_scope: ResourceScope
  budget: ExecutionBudget
  rollback_plan: RollbackPlan
  risk_reasons: RiskReason[]
  decisions: ApprovalDecisionRead[]
  timeline: PreparationTimelineItem[]
}

export interface ApprovalDecisionRequest {
  slot: ApprovalSlot
  decision: ApprovalDecisionType
  comment: string
}

export interface PreparationFilter {
  scope: 'todo' | 'mine' | 'done' | 'all'
  status?: PreparationStatus
  risk_level?: RiskLevel
  environment?: Env
  slot?: ApprovalSlot
  pipeline_id?: number
  keyword?: string
  page: number
  page_size: number
}

/** POST /preparations/{id}/commit；令牌全文与 jti 永不下发前端 */
export interface CommitResult {
  run_id: number
  status: 'queued'
  queued_at: string
}

export interface CreatePreparationRequest {
  pipeline_version_id: number
}
