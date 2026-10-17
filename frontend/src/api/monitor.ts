/**
 * 总览工作台与运行监督（fe-02 / be-21，接口待落地）。
 *
 * 关键口径（fe-02 §6.1）：所有聚合指标**一律用后端返回值**。
 * 前端算比率会引入第二套口径——后端按执行批次去重、按时间窗裁剪，
 * 前端拿列表长度去除必然对不上，最终没人知道该信哪个数。
 */

import { httpGet } from './client'
import type {
  MonitorAssets,
  MonitorHealth,
  MonitorMetrics,
  MonitorRange,
  MonitorSeries,
  MonitorSummary,
  QualitySummaryBundle,
  ResourceDistribution,
} from '@/types'

/** 一次拿齐首屏所需的全部卡片数据，避免六个接口并发打散首屏 */
export function fetchMonitorSummary(
  projectId: number,
  range: MonitorRange,
): Promise<MonitorSummary> {
  return httpGet<MonitorSummary>('/monitor/summary', {
    params: { project_id: projectId, range },
  })
}

export function fetchMonitorMetrics(
  projectId: number,
  range: MonitorRange,
): Promise<MonitorMetrics> {
  return httpGet<MonitorMetrics>('/monitor/metrics', {
    params: { project_id: projectId, range },
  })
}

/** 趋势曲线：后端已按 range 决定采样粒度 */
export function fetchMonitorSeries(
  projectId: number,
  range: MonitorRange,
): Promise<MonitorSeries> {
  return httpGet<MonitorSeries>('/monitor/series', {
    params: { project_id: projectId, range },
  })
}

/** 资源分布（风险等级 / 数据源类型 / 环境占比） */
export function fetchResourceDistribution(projectId: number): Promise<ResourceDistribution> {
  return httpGet<ResourceDistribution>('/monitor/resource-distribution', {
    params: { project_id: projectId },
  })
}

/** 服务健康度；degraded 组件前端要显式标黄 */
export function fetchSystemHealth(): Promise<MonitorHealth> {
  return httpGet<MonitorHealth>('/monitor/health')
}

/** 资产计数：数据源 / Pipeline / 版本 / 准备单 */
export function fetchMonitorAssets(projectId: number): Promise<MonitorAssets> {
  return httpGet<MonitorAssets>('/monitor/assets', { params: { project_id: projectId } })
}

/** 质量概览（与 fe-06 共用 DTO；无运行记录时各比率为 null，界面显示 '—'） */
export function fetchQualitySummary(
  projectId: number,
  range: MonitorRange,
): Promise<QualitySummaryBundle> {
  return httpGet<QualitySummaryBundle>('/monitor/quality-summary', {
    params: { project_id: projectId, range },
  })
}
