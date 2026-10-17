/**
 * 运行中心、质量报告与根因诊断（fe-06 / be-11 be-12 be-13）。
 *
 * 两条读取纪律：
 * 1. **日志走增量拉取**（since_seq 游标），不做全量重取——全量会把大日志拖垮浏览器。
 * 2. **错误明细只有受保护摘要**（payload_digest），原文不存在于平台，
 *    因此**没有「查看原始错误值」接口**，前端也不要提供这样的入口。
 */

import { httpGet, httpPost } from './client'
import type {
  Diagnosis,
  ErrorRecordPage,
  ExecutionMetricPoint,
  IncrementalPage,
  LogPage,
  Page,
  QualityReport,
  RunDetail,
  RunFilter,
  RunLogFilter,
  RunLogLine,
  RunSummary,
} from '@/types'

/* ------------------------------------------------------------------ */
/* 列表与详情                                                          */
/* ------------------------------------------------------------------ */

export function fetchRuns(projectId: number, filter: RunFilter): Promise<Page<RunSummary>> {
  return httpGet<Page<RunSummary>>('/runs', {
    params: {
      project_id: projectId,
      status: filter.status?.join(','),
      pipeline_id: filter.pipeline_id,
      from: filter.from,
      to: filter.to,
      q: filter.q,
      page: filter.page,
      page_size: filter.page_size,
    },
  })
}

export function fetchRun(id: number): Promise<RunDetail> {
  return httpGet<RunDetail>(`/runs/${id}`)
}

/** 指标时序（采样点，后端已按窗口聚合） */
export function fetchRunMetrics(
  id: number,
  limit = 200,
): Promise<ExecutionMetricPoint[]> {
  return httpGet<ExecutionMetricPoint[]>(`/runs/${id}/metrics`, { params: { limit } })
}

/** 只读原始日志行，**已脱敏**；原文不落平台 */
export function fetchRunLogs(
  id: number,
  filter: RunLogFilter,
): Promise<LogPage> {
  return httpGet<LogPage>(`/runs/${id}/logs`, {
    params: {
      since_seq: filter.since_seq,
      level: filter.level,
      // event_type 必须一并透传：`RunLogFilter` 里有这个字段却没发出去，
      // 就等于给调用方一个「筛了但没生效」的陷阱——用户会得出「这段时间没有该事件」的结论。
      event_type: filter.event_type,
      q: filter.q,
      limit: filter.limit,
    },
  })
}

/* ------------------------------------------------------------------ */
/* 质量与错误分流                                                      */
/* ------------------------------------------------------------------ */

export function fetchQualityReport(id: number): Promise<QualityReport> {
  return httpGet<QualityReport>(`/runs/${id}/quality-report`)
}

/** 错误行摘要分页：只含 payload_digest（哈希 + 截断），不含原文 */
export function fetchErrorRecords(
  id: number,
  page = 1,
  pageSize = 20,
): Promise<ErrorRecordPage> {
  return httpGet<ErrorRecordPage>(`/runs/${id}/error-records`, {
    params: { page, page_size: pageSize },
  })
}

/* ------------------------------------------------------------------ */
/* 监督动作（经 Harness，非直接操作 SeaTunnel）                         */
/* ------------------------------------------------------------------ */

/** 暂停：软动作，可恢复 */
export function pauseRun(id: number, reason: string): Promise<RunDetail> {
  return httpPost<RunDetail>(`/runs/${id}/pause`, { reason })
}

export function resumeRun(id: number, reason: string): Promise<RunDetail> {
  return httpPost<RunDetail>(`/runs/${id}/resume`, { reason })
}

export function cancelRun(id: number, reason: string): Promise<RunDetail> {
  return httpPost<RunDetail>(`/runs/${id}/cancel`, { reason })
}

/**
 * 受管回滚（target.rollback_cleanup，P1）。
 * reason 必填：回滚是不可逆动作，必须留下「为什么回滚」的证据。
 */
export function rollbackRun(id: number, reason: string): Promise<RunDetail> {
  return httpPost<RunDetail>(`/runs/${id}/rollback`, { reason })
}

/* ------------------------------------------------------------------ */
/* 根因诊断                                                            */
/* ------------------------------------------------------------------ */

/**
 * 根因诊断。
 * degraded=true 表示模型不可用、结论来自确定性规则回退——
 * 界面必须显著标注，不能让用户把回退结论当成模型分析。
 */
export function fetchDiagnosis(id: number): Promise<Diagnosis> {
  return httpGet<Diagnosis>(`/runs/${id}/diagnosis`)
}

/** 重新请求诊断（模型恢复后手动重试） */
export function requestDiagnosis(id: number): Promise<Diagnosis> {
  return httpPost<Diagnosis>(`/runs/${id}/diagnosis`)
}

/** 类型再导出，便于视图层从单一入口引用 */
export type { IncrementalPage, RunLogLine }
