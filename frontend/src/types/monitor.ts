/**
 * 总览工作台聚合视图类型（fe-02）。
 *
 * 注意：fe-02 §2 把 MonitorSummary 放在 views/dashboard/types.ts，
 * 但 api/monitor.ts 也要用它，而 fe-01 §1 禁止 api/ 反向 import 视图层，
 * 因此这里集中定义，dashboard 与 monitor api 共同引用。
 *
 * TODO(be-21)：整个 /monitor/summary 契约为本页建议值，后端未落地；
 * 字段名以 be-21 定稿为准。
 */

import type { DataSourceType } from './datasource'
import type { Env, PageMeta, SensitiveLevel } from './common'
import type { PreparationSummary } from './approval'

export type MonitorRange = '24h' | '7d' | '30d'

export interface MonitorAssets {
  datasource_count: number
  datasource_disabled_count: number
  pipeline_count: number
  pipeline_version_count: number
  metadata_profile_ready_count: number
  /** status='expired'，用于提示「需重新探查」 */
  metadata_profile_expired_count: number
}

export interface AnomalyBrief {
  run_id: number
  pipeline_id: number
  pipeline_name: string
  /** critical = 硬中断/失败；warning = 超预警阈值 */
  severity: 'warning' | 'critical'
  /** 已脱敏的一句话，如「拒绝率 0.2350 超预算 0.2000」 */
  summary: string
  occurred_at: string
}

export interface MonitorHealth {
  window: MonitorRange
  run_total: number
  run_succeeded: number
  run_failed: number
  /** active_states() 的 queued/running/paused/rolling_back 计数 */
  run_running: number
  run_interrupted: number
  /** numeric(6,4)，如 0.85 */
  success_rate: number
  failure_rate: number
  /** 上限 5 条 */
  recent_anomalies: AnomalyBrief[]
}

export interface MonitorMetrics {
  read_rows: number
  written_rows: number
  written_bytes: number
  reject_rows: number
  /** numeric(18,4)，行/秒 */
  avg_throughput: number
  /** numeric(6,4) */
  reject_rate: number
}

export interface MonitorSeriesPoint {
  ts: string
  throughput: number
  reject_rate: number
  read_rows: number
}

export interface MonitorSeries {
  points: MonitorSeriesPoint[]
}

export interface ResourceDistribution {
  by_type: Array<{ type: DataSourceType; count: number }>
  by_env: Array<{ env: Env; count: number }>
  by_sensitive_level: Array<{ sensitive_level: SensitiveLevel; count: number }>
}

/**
 * 待审批条目不自造 DTO，直接复用 fe-05 的 PreparationSummary。
 * fe-02 §10 建议 summary 只给 pending_count、列表统一走 /preparations；
 * 两种响应形状都能渲染（列表非空则以 summary 为准，避免两份数据打架）。
 */
export type PendingPreparationBrief = PreparationSummary

export interface MonitorSummary {
  generated_at: string
  range: MonitorRange
  assets: MonitorAssets
  health: MonitorHealth
  metrics: MonitorMetrics
  series: MonitorSeries
  distribution: ResourceDistribution
  pending_preparations: PendingPreparationBrief[]
}

/* ------------------------------------------------------------------ */
/* 运行维度（fe-06 与 fe-02 共用 api/monitor.ts）                       */
/* ------------------------------------------------------------------ */

import type { Diagnosis, ErrorRecordRead, QualityReport, RunLogLine } from './run'

export interface QualitySummaryBundle {
  report: QualityReport | null
  error_total: number
}

/** 错误明细是有限集合（质量报告已给出总数），用普通分页即可 */
export interface ErrorRecordPage {
  items: ErrorRecordRead[]
  meta: PageMeta
}

/**
 * 日志增量页。
 * 游标是 `seq`（单调递增序号）而不是字符串 cursor：日志在运行中持续追加，
 * 只有序号能保证「不漏行、不重行」。
 */
export interface LogPage {
  items: RunLogLine[]
  /** 本页最后一条的 seq；下次拉取作为 since_seq 传入；无新行时等于传入值 */
  next_seq: number
  /** 服务端因 limit 截断，调用方应立即再拉一次 */
  truncated: boolean
}

export type { Diagnosis, ErrorRecordRead, QualityReport, RunLogLine }
