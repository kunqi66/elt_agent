/**
 * 运行中心状态（fe-06）。
 *
 * 日志用 **seq 增量游标**累积，不做全量重取；且内存里保留上限，
 * 超长运行（百万行日志）不应该把浏览器标签页拖死。
 *
 * 三条「不可见」红线同样落在这里：
 * - 错误明细只有 payload_digest，store 不持有原文，也不提供获取入口；
 * - 日志行**不二次脱敏**（后端 structlog 处理器已处理），
 *   若前端再改写会破坏「日志与后端逐字一致」的可对账性；
 *   前端只在渲染前对疑似连接串形态做拦截式过滤（utils/mask）。
 * - 诊断结论必须原样带出 `degraded`，不得在 store 层吞掉。
 */

import { computed, ref } from 'vue'
import type { ComputedRef } from 'vue'
import { defineStore } from 'pinia'
import * as runApi from '@/api/run'
import { useListQuery } from '@/composables/useListQuery'
import { toDisplayMessage } from '@/utils/errors'
import { isRunTerminal } from '@/types'
import { useProjectStore } from './project'
import type {
  Diagnosis,
  ErrorRecordRead,
  ExecutionMetricPoint,
  PageMeta,
  QualityReport,
  RunDetail,
  RunFilter,
  RunLogFilter,
  RunLogLine,
  RunSummary,
} from '@/types'

function makeDefaultFilter(): RunFilter {
  return { page: 1, page_size: 20 }
}

/** 单次日志拉取条数：够看，又不至于一次渲染上千个 DOM 节点 */
const LOG_PAGE_SIZE = 200
/**
 * 内存中最多保留的日志行数；超出后丢弃最旧的（用户已经在往上翻了另说）。
 *
 * 导出而非私有：视图要靠它判断「缓冲是否已被裁剪」并提示用户。
 * 视图若自己镜像一份，改了这个值就会静默失准——日志被悄悄丢弃而界面不吭声，
 * 是排障时最不该出现的一类假象。
 */
export const LOG_BUFFER_LIMIT = 5000

export const useRunStore = defineStore('run', () => {
  const projectStore = useProjectStore()

  const query = useListQuery<RunSummary, RunFilter>(
    makeDefaultFilter,
    (filter) => {
      const projectId = projectStore.currentProjectId
      if (projectId === null) {
        return Promise.resolve({ items: [], meta: { page: 1, page_size: filter.page_size, total: 0 } })
      }
      return runApi.fetchRuns(projectId, filter)
    },
    { immediate: false, scope: () => projectStore.currentProjectId },
  )

  const detail = ref<RunDetail | null>(null)
  const detailLoading = ref(false)
  const detailError = ref<string | null>(null)

  const metrics = ref<ExecutionMetricPoint[]>([])
  const quality = ref<QualityReport | null>(null)
  const diagnosis = ref<Diagnosis | null>(null)
  const diagnosisLoading = ref(false)

  const errorRecords = ref<ErrorRecordRead[]>([])
  const errorMeta = ref<PageMeta>({ page: 1, page_size: 20, total: 0 })
  const errorLoading = ref(false)

  const logs = ref<RunLogLine[]>([])
  /** 增量游标：我只持有「已拿到的最新 seq」 */
  const logCursor = ref(0)
  const logLoading = ref(false)
  const logTruncated = ref(false)
  const logFilter = ref<RunLogFilter>({ limit: LOG_PAGE_SIZE })

  /** 终态即停止轮询，这是 usePolling 的 shouldStop 判据 */
  const isTerminal: ComputedRef<boolean> = computed(() => isRunTerminal(detail.value?.status))

  /** 当前运行是否允许各监督动作；全部取后端下发的能力位 */
  const canPause: ComputedRef<boolean> = computed(() => detail.value?.can_pause === true)
  const canResume: ComputedRef<boolean> = computed(() => detail.value?.can_resume === true)
  const canCancel: ComputedRef<boolean> = computed(() => detail.value?.can_cancel === true)
  const canRollback: ComputedRef<boolean> = computed(() => detail.value?.can_rollback === true)

  async function loadDetail(id: number): Promise<void> {
    detailLoading.value = true
    detailError.value = null
    // 只在「看的是另一次运行」时先清空：否则上一次运行的数据会在新请求飞行期间
    // 冒充成当前运行显示出来（用户看到的是 run A 的状态，地址栏是 run B）。
    // 同一次运行的**刷新**失败则保留旧值，只出错误提示 —— 运行详情是排障时唯一在盯的页面，
    // 一次瞬时 500 就把它清成错误页，恰恰在最需要看到「刚才还好好的那批指标」时把它拿走了。
    if (detail.value !== null && detail.value.id !== id) {
      detail.value = null
    }
    try {
      detail.value = await runApi.fetchRun(id)
    } catch (e) {
      detailError.value = toDisplayMessage(e)
    } finally {
      detailLoading.value = false
    }
  }

  /** 只刷新状态与指标，用于轮询（不必每次重拉详情里的静态部分） */
  async function refreshRunning(id: number): Promise<void> {
    const [d, m] = await Promise.all([
      runApi.fetchRun(id),
      runApi.fetchRunMetrics(id, 60),
    ])
    detail.value = d
    metrics.value = m
  }

  async function loadMetrics(id: number, limit = 200): Promise<void> {
    try {
      metrics.value = await runApi.fetchRunMetrics(id, limit)
    } catch {
      // 保留上一份指标而不是清成空：占满整页的曲线突然空掉，会被读成
      // 「这次运行没有产生指标」，而真实情况往往只是一次请求失败。
      // 指标本就由详情轮询持续补齐，不需要在这里把旧值抹掉。
    }
  }

  async function loadQuality(id: number): Promise<void> {
    try {
      quality.value = await runApi.fetchQualityReport(id)
    } catch {
      // 未生成报告不是错误（运行还没结束），静默为空
      quality.value = null
    }
  }

  async function loadErrorRecords(id: number, page = 1): Promise<void> {
    errorLoading.value = true
    try {
      const result = await runApi.fetchErrorRecords(id, page, errorMeta.value.page_size)
      errorRecords.value = result.items
      errorMeta.value = result.meta
    } catch {
      errorRecords.value = []
    } finally {
      errorLoading.value = false
    }
  }

  /** 首次加载日志：清空缓冲后从头拉 */
  async function resetLogs(id: number, filter?: Partial<RunLogFilter>): Promise<void> {
    logs.value = []
    logCursor.value = 0
    logTruncated.value = false
    logFilter.value = { ...logFilter.value, ...filter }
    await loadMoreLogs(id)
  }

  /**
   * 拉取增量日志。
   * 可能被服务端截断，此时连续调用直到追平——否则用户会看到
   * 「日志停在中间」却没有任何提示。
   */
  async function loadMoreLogs(id: number): Promise<void> {
    if (logLoading.value) {
      return
    }
    logLoading.value = true
    try {
      let guard = 0
      do {
        const page = await runApi.fetchRunLogs(id, {
          ...logFilter.value,
          since_seq: logCursor.value === 0 ? undefined : logCursor.value,
        })
        if (page.items.length > 0) {
          logs.value = [...logs.value, ...page.items]
          logCursor.value = page.next_seq
          if (logs.value.length > LOG_BUFFER_LIMIT) {
            // 只丢最旧的：滚动位置在底部时用户不会察觉到被裁掉的是历史
            logs.value = logs.value.slice(logs.value.length - LOG_BUFFER_LIMIT)
          }
        }
        logTruncated.value = page.truncated
        guard += 1
        // 最多连追 5 页，避免后端持续吐日志时卡住这一次调用
      } while (logTruncated.value && guard < 5)
    } catch (e) {
      detailError.value = detailError.value ?? toDisplayMessage(e)
    } finally {
      logLoading.value = false
    }
  }

  async function loadDiagnosis(id: number): Promise<void> {
    diagnosisLoading.value = true
    try {
      diagnosis.value = await runApi.fetchDiagnosis(id)
    } catch {
      // 诊断能力不可用不等于运行详情不可用；degraded 由后端在成功响应里表达
      diagnosis.value = null
    } finally {
      diagnosisLoading.value = false
    }
  }

  async function requestDiagnosis(id: number): Promise<void> {
    diagnosisLoading.value = true
    try {
      diagnosis.value = await runApi.requestDiagnosis(id)
    } finally {
      diagnosisLoading.value = false
    }
  }

  /** 监督动作后刷新详情与列表；运行状态可能已跨过终态 */
  async function refreshAfterAction(id: number): Promise<void> {
    await loadDetail(id)
    await query.reload()
  }

  function clearDetail(): void {
    detail.value = null
    detailError.value = null
    metrics.value = []
    quality.value = null
    diagnosis.value = null
    errorRecords.value = []
    errorMeta.value = { page: 1, page_size: 20, total: 0 }
    logs.value = []
    logCursor.value = 0
    logTruncated.value = false
    logFilter.value = { limit: LOG_PAGE_SIZE }
  }

  return {
    query,
    detail,
    detailLoading,
    detailError,
    metrics,
    quality,
    diagnosis,
    diagnosisLoading,
    errorRecords,
    errorMeta,
    errorLoading,
    logs,
    logCursor,
    logLoading,
    logTruncated,
    logFilter,
    isTerminal,
    canPause,
    canResume,
    canCancel,
    canRollback,
    loadDetail,
    refreshRunning,
    loadMetrics,
    loadQuality,
    loadErrorRecords,
    resetLogs,
    loadMoreLogs,
    loadDiagnosis,
    requestDiagnosis,
    refreshAfterAction,
    clearDetail,
  }
})
