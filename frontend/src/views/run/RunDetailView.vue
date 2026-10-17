<script setup lang="ts">
/**
 * 运行详情（路由 /runs/:id，fe-06 §3/§5 三区布局）。
 *
 * 轮询纪律（fe-01 §5.4 + fe-06 §5）：
 * - 单 tick 顺序执行「详情 → 指标采样 → 日志增量」，**一拍的请求数是常数**，
 *   不随日志行数增长；日志用 store 的 seq 游标增量拉，绝不整段重取。
 * - 进入终态即自停（`isTerminal`），并在页头写明「已停止自动刷新」——
 *   静默停止会让人以为数据不动了。
 * - 连续失败达上限也自停：后端挂了还每 2 秒敲门是纯粹的浪费，
 *   停下并给出「重试」让用户决定何时再打。
 * - 后台标签页暂停、回到前台立刻补一拍，都由 usePolling 负责，这里不自建定时器。
 *
 * 指标折线的数据来自**本视图的采样缓冲**：基座 store 的 `refreshRunning()`
 * 每次用最近 60 点整段替换 `metrics`，直接拿来画图会得到一段不断被截断的曲线；
 * 而 api 没有 `since` 游标。因此这里在首次加载后按 tick 从 detail 追加采样点
 * （detail 里的 read_rows / written_bytes / reject_rate 等就是同一批口径），
 * 缓冲到上限即从头部裁剪，并置 `omitted` 让 MetricsPanel 显式提示「更早数据已省略」。
 */
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  AsyncTaskPanel,
  ConfirmDialog,
  DangerConfirmDialog,
  EmptyState,
  ErrorState,
  PageHeader,
  RunStatusTag,
} from '@/components/common'
import { useAsyncTask } from '@/composables/useAsyncTask'
import { usePermission } from '@/composables/usePermission'
import { usePolling } from '@/composables/usePolling'
import { useProjectStore } from '@/stores/project'
import { LOG_BUFFER_LIMIT, useRunStore } from '@/stores/run'
import { downloadFile } from '@/utils/download'
import { ElMessage } from '@/utils/feedback'
import { DASH, formatBytes, formatDuration, formatRate, formatRows } from '@/utils/format'
import * as runApi from '@/api/run'
import type { ExecutionMetricPoint, RunDetail, RunLogFilter } from '@/types'
import BudgetPanel from './components/BudgetPanel.vue'
import DiagnosisPanel from './components/DiagnosisPanel.vue'
import ErrorDetailTable from './components/ErrorDetailTable.vue'
import ExecutionFlowPanel from './components/ExecutionFlowPanel.vue'
import FailureSnapshotPanel from './components/FailureSnapshotPanel.vue'
import MetricsPanel from './components/MetricsPanel.vue'
import QualityPanel from './components/QualityPanel.vue'
import RollbackPlanPanel from './components/RollbackPlanPanel.vue'
import RunActionBar from './components/RunActionBar.vue'
import RunHeaderPanel from './components/RunHeaderPanel.vue'
import RunLogViewer from './components/RunLogViewer.vue'

/** 详情轮询间隔：与后端监督采集窗口同量级（fe-06 §3.1） */
const DETAIL_POLL_INTERVAL_MS = 2000
/**
 * 失败退避阶梯（fe-06 §6）。定时器间隔保持固定，
 * 退避通过「跳过若干拍」实现——再起一个定时器管理退避等于把轮询逻辑写两遍。
 */
const BACKOFF_STEPS_MS = [2000, 4000, 8000, 16000, 30000]
/** 连续失败上限：约 60 秒仍不可用就停下等人工重试 */
const MAX_POLL_FAILURES = 5
/** 每 2 拍追一次日志（约 4 秒）；日志比状态慢一拍并不影响排障 */
const LOG_POLL_EVERY_N_TICKS = 2
/** 每 5 拍刷一次质量报告（约 10 秒）：报告在运行中变化很慢 */
const QUALITY_POLL_EVERY_N_TICKS = 5
/** 指标缓冲上限（fe-06 §6 MAX_POINTS）；超出的部分由 MetricsPanel 降采样绘制 */
const METRIC_BUFFER_LIMIT = 1800
/** 首次加载的指标历史点数 */
const METRIC_SEED_LIMIT = 200

/** 采样缺口判据：超过 4 拍的时间没有新样本，说明中间有拍被跳过 */
const METRIC_GAP_MS = DETAIL_POLL_INTERVAL_MS * 4

const route = useRoute()
const router = useRouter()
const store = useRunStore()
const projectStore = useProjectStore()
const permission = usePermission()

const runId = computed<number>(() => {
  const raw = route.params.id
  const value = Array.isArray(raw) ? raw[0] : raw
  return Number(value)
})

const run = computed<RunDetail | null>(() => store.detail)
const initialLoading = computed<boolean>(() => store.detailLoading && store.detail === null)
/** 错误优先于空：拉取失败绝不能渲染成「运行不存在」 */
const loadFailed = computed<boolean>(() => store.detailError !== null && store.detail === null)
const missing = computed<boolean>(
  () => !store.detailLoading && store.detail === null && store.detailError === null,
)
/** 审计员只读：整条操作栏不渲染（fe-06 §5），后端仍会独立鉴权 */
const canOperate = computed<boolean>(() => permission.isOperator.value)

const metricBuffer = ref<ExecutionMetricPoint[]>([])
const metricsOmitted = ref(false)
const metricsGap = ref(false)
/** 缓冲裁剪后允许「加载更早」——但历史只能靠导出，这里只是把窗口放宽 */
let lastSampleAt = 0
let tickCount = 0
let backoffUntil = 0
let terminalSynced = false

const pollStopReason = ref<'terminal' | 'failure' | null>(null)

const logOmitted = computed<boolean>(() => store.logs.length >= LOG_BUFFER_LIMIT)

const hasFailureSnapshot = computed<boolean>(
  () =>
    run.value !== null &&
    ['failed', 'interrupted'].includes(run.value.status) &&
    Object.keys(run.value.failure_snapshot).length > 0,
)

/* ------------------------------------------------------------------ */
/* 加载与轮询                                                          */
/* ------------------------------------------------------------------ */

async function seedMetrics(id: number): Promise<void> {
  await store.loadMetrics(id, METRIC_SEED_LIMIT)
  const all = store.metrics
  const kept = all.length > METRIC_BUFFER_LIMIT ? all.slice(all.length - METRIC_BUFFER_LIMIT) : all
  metricBuffer.value = [...kept]
  metricsOmitted.value = kept.length < all.length
  const last = kept[kept.length - 1]
  lastSampleAt = last === undefined ? 0 : Date.parse(last.ts)
}

async function bootstrap(): Promise<void> {
  const id = runId.value
  if (!Number.isFinite(id) || id <= 0) {
    return
  }
  // 路由参数切换（同实例换 run）时先把旧轮询停掉，否则会带着上一条运行的退避与失败计数
  polling.stop()
  polling.failureCount.value = 0
  store.clearDetail()
  metricBuffer.value = []
  metricsOmitted.value = false
  metricsGap.value = false
  tickCount = 0
  backoffUntil = 0
  terminalSynced = false
  pollStopReason.value = null

  await projectStore.ensureLoaded()
  // 并行取各分区：任一失败都不该拖住其它分区（store 内部各自消化）
  await Promise.all([
    store.loadDetail(id),
    store.loadQuality(id),
    store.loadDiagnosis(id),
    store.loadErrorRecords(id, 1),
    store.resetLogs(id),
    seedMetrics(id),
  ])

  if (store.detail !== null && !store.isTerminal) {
    polling.start()
  } else if (store.isTerminal) {
    pollStopReason.value = 'terminal'
  }
}

/** 把 detail 的累计量压成一个采样点：口径与 ExecutionMetricPoint 同源 */
function pushMetricSample(): void {
  const detail = store.detail
  if (detail === null) {
    return
  }
  const now = Date.now()
  if (lastSampleAt > 0 && now - lastSampleAt > METRIC_GAP_MS) {
    // 中间有拍被跳过（退避或切后台），折线会把两点直连，必须标注而不是装作连贯
    metricsGap.value = true
  }
  lastSampleAt = now

  const sample: ExecutionMetricPoint = {
    ts: new Date(now).toISOString(),
    read_rows: detail.read_rows,
    written_rows: detail.written_rows,
    written_bytes: detail.written_bytes,
    duration_sec: detail.duration_sec,
    throughput: detail.avg_throughput,
    amplification_ratio: detail.amplification_ratio,
    reject_rate: detail.reject_rate,
  }
  const next = [...metricBuffer.value, sample]
  if (next.length > METRIC_BUFFER_LIMIT) {
    metricBuffer.value = next.slice(next.length - METRIC_BUFFER_LIMIT)
    metricsOmitted.value = true
    return
  }
  metricBuffer.value = next
}

async function pollOnce(): Promise<void> {
  const id = runId.value
  if (!Number.isFinite(id) || id <= 0) {
    return
  }
  // 退避窗口内直接返回：定时器照常走，请求不打出去
  if (Date.now() < backoffUntil) {
    return
  }

  await store.loadDetail(id)
  if (store.detailError !== null && store.detail === null) {
    const step = BACKOFF_STEPS_MS[Math.min(polling.failureCount.value, BACKOFF_STEPS_MS.length - 1)] ?? 2000
    backoffUntil = Date.now() + step
    // store 对详情失败是「记录到 detailError」而不是抛出；这里补一次抛出，
    // 让 usePolling 能累计失败次数并触发自停降级
    throw new Error(store.detailError)
  }
  backoffUntil = 0
  tickCount += 1

  pushMetricSample()

  if (tickCount % LOG_POLL_EVERY_N_TICKS === 0) {
    await store.loadMoreLogs(id)
  }

  if (store.isTerminal && !terminalSynced) {
    // 终态是质量报告与诊断真正成型的时刻：在这里补一次，之后停轮询
    terminalSynced = true
    await Promise.all([
      store.loadQuality(id),
      store.loadDiagnosis(id),
      store.loadErrorRecords(id, store.errorMeta.page),
    ])
    return
  }
  if (!store.isTerminal && tickCount % QUALITY_POLL_EVERY_N_TICKS === 0) {
    await store.loadQuality(id)
  }
}

const polling = usePolling(pollOnce, {
  intervalMs: DETAIL_POLL_INTERVAL_MS,
  // 首屏由 bootstrap 显式加载，避免立即多打一拍
  immediate: false,
  pauseWhenHidden: true,
  shouldStop: () => {
    if (store.isTerminal) {
      pollStopReason.value = 'terminal'
      return true
    }
    if (polling.failureCount.value >= MAX_POLL_FAILURES) {
      pollStopReason.value = 'failure'
      return true
    }
    return false
  },
})

function resumePolling(): void {
  pollStopReason.value = null
  polling.failureCount.value = 0
  backoffUntil = 0
  if (!polling.active.value) {
    polling.start()
  }
}

/* ------------------------------------------------------------------ */
/* 监督动作                                                            */
/* ------------------------------------------------------------------ */

const pauseOpen = ref(false)
const resumeOpen = ref(false)
const cancelOpen = ref(false)
const rollbackOpen = ref(false)
/** 最近一次回滚失败：锁定按钮，改为要求人工确认后再放开 */
const rollbackLocked = ref(false)

async function afterAction(): Promise<void> {
  const id = runId.value
  if (!Number.isFinite(id) || id <= 0) {
    return
  }
  await store.refreshAfterAction(id)
  // 回滚后状态会回到非终态（rolling_back），必须重新开始轮询
  resumePolling()
}

const pauseTask = useAsyncTask<[string]>(
  async (reason: string) => {
    await runApi.pauseRun(runId.value, reason)
  },
  {
    successMessage: '暂停指令已下发',
    onSuccess: () => {
      pauseOpen.value = false
      void afterAction()
    },
  },
)

const resumeTask = useAsyncTask<[string]>(
  async (reason: string) => {
    await runApi.resumeRun(runId.value, reason)
  },
  {
    successMessage: '恢复指令已下发',
    onSuccess: () => {
      resumeOpen.value = false
      void afterAction()
    },
  },
)

const cancelTask = useAsyncTask<[string]>(
  async (reason: string) => {
    await runApi.cancelRun(runId.value, reason)
  },
  {
    successMessage: '取消指令已下发',
    onSuccess: () => {
      cancelOpen.value = false
      void afterAction()
    },
  },
)

const rollbackTask = useAsyncTask<[string]>(
  async (reason: string) => {
    await runApi.rollbackRun(runId.value, reason)
  },
  {
    successMessage: '受管回滚已进入执行队列',
    onSuccess: () => {
      rollbackOpen.value = false
      void afterAction()
    },
    onError: () => {
      // 失败原因留在页面上（AsyncTaskPanel），同时锁住按钮：
      // 在残留状态上连点第二次回滚，比不操作更危险
      rollbackLocked.value = true
      rollbackOpen.value = false
    },
  },
)

const actionLoading = computed<boolean>(
  () =>
    pauseTask.loading.value ||
    resumeTask.loading.value ||
    cancelTask.loading.value ||
    rollbackTask.loading.value,
)

const actionError = computed<string | null>(
  () =>
    pauseTask.error.value ??
    resumeTask.error.value ??
    cancelTask.error.value ??
    rollbackTask.error.value ??
    null,
)

function rearmRollback(): void {
  pauseTask.reset()
  resumeTask.reset()
  cancelTask.reset()
  rollbackTask.reset()
  rollbackLocked.value = false
  rollbackOpen.value = true
}

function onPauseConfirm(reason: string): void {
  void pauseTask.run(reason)
}

function onResumeConfirm(reason: string): void {
  void resumeTask.run(reason)
}

function onCancelConfirm(reason: string): void {
  void cancelTask.run(reason)
}

function onRollbackConfirm(payload: { reason: string; acknowledged: boolean }): void {
  void rollbackTask.run(payload.reason)
}

/* 弹窗必须展示的内容（fe-06 §5 前置校验表）：不是「确定吗」，
   而是把这次动作的后果与剩余额度摊在用户眼前 */
function remainingText(limit: number, used: number, unit: 'rows' | 'bytes' | 'sec' | 'ratio'): string {
  if (!Number.isFinite(limit) || limit <= 0) {
    return '未设上限'
  }
  const left = limit - used
  const text = (value: number): string => {
    switch (unit) {
      case 'rows':
        return formatRows(value)
      case 'bytes':
        return formatBytes(value)
      case 'ratio':
        return formatRate(value)
      default:
        return formatDuration(value)
    }
  }
  return left < 0 ? `已超 ${text(-left)}` : text(left)
}

const pauseDescription = computed<string>(() => {
  const detail = run.value
  if (detail === null) {
    return ''
  }
  return [
    `运行 #${detail.id} · ${detail.pipeline_name} v${detail.version_no}`,
    `当前进度：已读 ${formatRows(detail.read_rows)} 行，已写 ${formatBytes(detail.written_bytes)}（${formatRows(detail.written_rows)} 行）`,
    '暂停是软动作：影子表与已写入的数据全部保留，不发生回滚，可随时恢复；SeaTunnel 作业由后端经 Harness 暂停。',
  ].join('\n')
})

const resumeDescription = computed<string>(() => {
  const detail = run.value
  if (detail === null) {
    return ''
  }
  const budget = detail.budget
  return [
    `恢复后将继续写入影子表 ${detail.shadow_table ?? DASH}`,
    `剩余预算：读取 ${remainingText(budget.max_read_rows, detail.read_rows, 'rows')} · 写入 ${remainingText(budget.max_write_bytes, detail.written_bytes, 'bytes')} · 时长 ${remainingText(budget.max_duration_sec, detail.duration_sec, 'sec')}`,
    `拒绝率剩余额度 ${remainingText(budget.max_reject_rate, detail.reject_rate, 'ratio')}`,
    '任一预算触顶时，监督内核会硬中断本次运行（interrupted）——这是保护动作，不是故障。',
  ].join('\n')
})

const cancelDescription = computed<string>(() => {
  const detail = run.value
  if (detail === null) {
    return ''
  }
  return [
    `取消会停止 SeaTunnel 作业（${detail.seatunnel_job_id ?? DASH}），运行进入终态 cancelled`,
    `影子表 ${detail.shadow_table ?? DASH} 与错误表 ${detail.error_table ?? DASH} 会留在原地，**不会自动清理**`,
    '正式表不受影响：本次运行尚未完成原子 Swap 时，主表数据没有任何变化',
    '取消后如不再需要这批中间结果，可在本页执行「受管回滚」按后端方案清理（步骤见回滚弹窗）。',
  ].join('\n')
})

const rollbackDescription = computed<string>(() => {
  const detail = run.value
  if (detail === null) {
    return ''
  }
  return [
    `影响范围：影子表 ${detail.shadow_table ?? DASH} · 错误表 ${detail.error_table ?? DASH}`,
    `是否清理未发布影子数据：${detail.rollback_plan.shadow_cleanup ? '是（rollback_plan.shadow_cleanup = true）' : '否'}`,
    `回滚步骤共 ${detail.rollback_plan.steps.length} 步，全部由后端经 Harness 工具执行，前端不下发任何 DDL 或作业指令。`,
    detail.status === 'succeeded'
      ? `注意：本次运行已成功并完成原子 Swap，正式表 ${detail.formal_table ?? DASH} 的内容不会被回滚改动（回滚只清理影子数据与错误表残留）。`
      : `正式表 ${detail.formal_table ?? DASH} 未被本次运行修改（未完成 Swap 或已回滚）。`,
  ].join('\n')
})

const rollbackPhrase = computed<string>(() => `ROLLBACK-${runId.value}`)

/* ------------------------------------------------------------------ */
/* 日志与导出                                                          */
/* ------------------------------------------------------------------ */

async function onLogFilter(patch: Partial<RunLogFilter>): Promise<void> {
  const id = runId.value
  if (!Number.isFinite(id) || id <= 0) {
    return
  }
  // 换筛选条件必须把游标归零重拉：带着旧 since_seq 会漏掉此前被过滤掉的行
  await store.resetLogs(id, patch)
}

async function reloadLogs(): Promise<void> {
  await store.loadMoreLogs(runId.value)
}

/**
 * 导出走 `@/utils/download.downloadFile`，由后端生成文件。
 * **前端不拼 CSV**：列顺序、脱敏口径都在后端，否则导出件会与页面对不上。
 */
function exportLogs(): void {
  void downloadFile(
    `/runs/${runId.value}/logs/export`,
    { level: store.logFilter.level, q: store.logFilter.q },
    { filenamePrefix: `run-${runId.value}-logs`, format: 'json' },
  ).catch(() => {
    // 失败提示由 client 拦截器统一给出，这里只吞掉 Promise
  })
}

function exportErrorRecords(): void {
  void downloadFile(
    `/runs/${runId.value}/error-records/export`,
    {},
    { filenamePrefix: `run-${runId.value}-error-records`, format: 'csv' },
  ).catch(() => {
    // 同上
  })
}

/* ------------------------------------------------------------------ */
/* 诊断面板交互                                                        */
/* ------------------------------------------------------------------ */

const diagnosisTask = useAsyncTask<[]>(
  async () => {
    await store.requestDiagnosis(runId.value)
  },
  { successMessage: '已重新请求诊断' },
)

/** 结论里指向的对象：能直达的直达，在本页展示的就地滚动，其余要求人工检索 */
const ON_PAGE_SUBJECTS: Record<string, string> = {
  execution_run: 'run-section-header',
  execution_metric: 'run-section-metrics',
  quality_report: 'run-section-quality',
  error_record: 'run-section-errors',
}

function scrollToSection(elementId: string): void {
  const el = document.getElementById(elementId)
  if (el !== null) {
    el.scrollIntoView({ behavior: 'smooth', block: 'start' })
  }
}

function onGotoSubject(payload: { subjectType: string; subjectId: string }): void {
  const type = payload.subjectType.toLowerCase()
  const section = ON_PAGE_SUBJECTS[type]
  if (section !== undefined) {
    scrollToSection(section)
    return
  }
  if (type === 'preparation' || type === 'preparation_record') {
    void router.push({ name: 'preparation-detail', params: { id: payload.subjectId } })
    return
  }
  if (type === 'run') {
    void router.push({ name: 'run-detail', params: { id: payload.subjectId } })
    return
  }
  // 不猜路由：subject 的 id 语义在各表之间不一致，猜错会跳到另一条记录上
  ElMessage.warning(`${payload.subjectType} #${payload.subjectId} 没有直达页面，请在对应中心按该标识检索`)
}

const KNOWN_ROUTES: readonly string[] = [
  'dashboard',
  'datasource-list',
  'datasource-detail',
  'pipeline-list',
  'pipeline-studio',
  'preparation-list',
  'preparation-detail',
  'run-list',
  'run-detail',
  'governance',
  'settings',
  'account',
]

function onGotoRoute(routeName: string): void {
  if (!KNOWN_ROUTES.includes(routeName)) {
    ElMessage.warning(`建议的目标页面「${routeName}」不在本控制台，无法跳转`)
    return
  }
  void router.push({ name: routeName })
}

/* ------------------------------------------------------------------ */

/**
 * 点错误码分布 → 滚到错误明细。
 * 这里**不按错误码过滤**：基座 `fetchErrorRecords(id, page, pageSize)` 没有错误码参数，
 * 若前端自行过滤，用户看到的「该错误码的记录」就不是全量（分页之外的可能漏掉），
 * 反而会得出错误结论。因此只做定位，不假装筛选。
 */
function onOpenErrorDetail(): void {
  scrollToSection('run-section-errors')
}

/** store 的质量报告加载是静默的（「未生成」与「失败」都置空），加载态只能由视图自己持有 */
const qualityLoading = ref(false)

async function reloadQuality(): Promise<void> {
  qualityLoading.value = true
  try {
    await store.loadQuality(runId.value)
  } finally {
    qualityLoading.value = false
  }
}

async function reloadDiagnosis(): Promise<void> {
  await store.loadDiagnosis(runId.value)
}

function backToList(): void {
  void router.push({ name: 'run-list' })
}

watch(runId, () => {
  void bootstrap()
})

// 组件卸载即清空：运行详情含大量执行数据，留在内存里没有意义（fe-06 §8）
onBeforeUnmount(() => {
  store.clearDetail()
})

void bootstrap()
</script>

<template>
  <div class="run-detail">
    <PageHeader
      :title="run === null ? '运行详情' : `运行详情 #${run.id}`"
      description="只读运行视图：状态、预算消耗、质量分流与日志。监督动作经后端 Harness 执行，前端不接触数据面。"
    >
      <template #actions>
        <el-button size="small" @click="backToList">返回列表</el-button>
        <el-button size="small" :loading="store.detailLoading" @click="resumePolling">刷新</el-button>
      </template>
      <template #notice>
        <div class="run-detail__notice">
          <RunStatusTag v-if="run !== null" :status="run.status" />
          <span v-if="pollStopReason === 'terminal'" class="run-detail__notice-item">
            运行已进入终态，自动刷新已停止（日志仍可手动「拉取增量」）
          </span>
          <span v-else-if="pollStopReason === 'failure'" class="run-detail__notice-item run-detail__notice-item--danger">
            连续 {{ MAX_POLL_FAILURES }} 次拉取失败，自动刷新已停止
            <el-button link size="small" @click="resumePolling">重试</el-button>
          </span>
          <span v-else class="run-detail__notice-item run-detail__notice-item--active">
            每 {{ DETAIL_POLL_INTERVAL_MS / 1000 }} 秒自动刷新（切到后台标签页时暂停，回来立即补一拍）
          </span>
        </div>
      </template>
    </PageHeader>

    <div v-if="initialLoading" class="run-detail__skeleton">
      <el-skeleton :rows="8" animated />
    </div>

    <ErrorState
      v-else-if="loadFailed"
      title="运行详情加载失败"
      :message="store.detailError"
      @retry="bootstrap"
    />

    <EmptyState
      v-else-if="missing"
      variant="no-data"
      title="找不到这条运行"
      description="运行记录不存在，或已被项目隔离（TENANT_MISMATCH）。返回列表确认当前项目后再试。"
    >
      <template #actions>
        <el-button size="small" @click="backToList">返回运行中心</el-button>
      </template>
    </EmptyState>

    <template v-else-if="run !== null">
      <div id="run-section-header">
        <RunHeaderPanel :run="run" />
      </div>

      <!-- fe-06 §5：auditor 整条操作栏不渲染（而不是渲染成禁用态），
           由 usePermission 决定可见性；真正的拒绝仍在后端 role_assignment -->
      <div v-if="canOperate" class="run-detail__section">
        <RunActionBar
          :run="run"
          :readonly="false"
          :submitting="actionLoading"
          :rollback-blocked="rollbackLocked"
          @pause="pauseOpen = true"
          @resume="resumeOpen = true"
          @cancel="cancelOpen = true"
          @rollback="rollbackOpen = true"
        />
        <!-- 回滚失败后不再自动放开：由操作员显式点这里重新武装，避免连点 -->
        <div v-if="rollbackLocked" class="run-detail__locked">
          <el-alert type="error" :closable="false" show-icon title="上一次回滚未成功">
            <p class="run-detail__locked-text">
              为避免在残留状态上叠加第二次清理，受管回滚按钮已锁定。请先确认后端故障原因
              （见下方失败信息与诊断结论），再由操作员显式重新发起。
            </p>
            <el-button size="small" type="danger" plain @click="rearmRollback">我已确认，重新发起回滚</el-button>
          </el-alert>
        </div>
        <AsyncTaskPanel
          v-if="actionError !== null"
          :loading="actionLoading"
          :error="actionError"
          loading-text="监督动作下发中…"
        />
      </div>

      <ExecutionFlowPanel :run="run" class="run-detail__section" />

      <div id="run-section-metrics" class="run-detail__grid">
        <MetricsPanel
          :run="run"
          :metrics="metricBuffer"
          :budget="run.budget"
          :omitted="metricsOmitted"
          :gap="metricsGap"
        />
        <BudgetPanel :run="run" />
      </div>

      <div id="run-section-quality" class="run-detail__section">
        <QualityPanel
          :report="store.quality"
          :loading="qualityLoading"
          :shadow-table="run.shadow_table"
          :formal-table="run.formal_table"
          :error-table="run.error_table"
          @retry="reloadQuality"
          @open-error-detail="onOpenErrorDetail"
        />
      </div>

      <div id="run-section-errors" class="run-detail__section">
        <ErrorDetailTable
          :items="store.errorRecords"
          :loading="store.errorLoading"
          :total="store.errorMeta.total"
          :page="store.errorMeta.page"
          :page-size="store.errorMeta.page_size"
          @page-change="(p: number) => store.loadErrorRecords(runId, p)"
          @reload="() => store.loadErrorRecords(runId, store.errorMeta.page)"
          @export="exportErrorRecords"
        />
      </div>

      <div class="run-detail__section">
        <DiagnosisPanel
          :diagnosis="store.diagnosis"
          :loading="store.diagnosisLoading || diagnosisTask.loading.value"
          :degraded="false"
          :error="diagnosisTask.error.value"
          @retry="() => { void reloadDiagnosis(); void diagnosisTask.run() }"
          @goto-subject="onGotoSubject"
          @goto-route="onGotoRoute"
        />
      </div>

      <FailureSnapshotPanel
        v-if="hasFailureSnapshot"
        :snapshot="run.failure_snapshot"
        class="run-detail__section"
      />

      <div class="run-detail__section">
        <RunLogViewer
          :lines="store.logs"
          :filters="store.logFilter"
          :loading="store.logLoading"
          :truncated="store.logTruncated"
          :omitted="logOmitted"
          @update:filters="onLogFilter"
          @reload="reloadLogs"
          @export="exportLogs"
          @goto-subject="onGotoSubject"
        />
      </div>

      <p class="run-detail__foot">
        本页不提供任何「查看错误原文 / 下载错误明细」入口：错误行原文不落平台，
        只保留受保护摘要（哈希 + 截断）。日志与错误记录的导出文件均由后端生成，前端不拼接。
      </p>

      <ConfirmDialog
        v-model="pauseOpen"
        title="暂停运行"
        :description="pauseDescription"
        confirm-text="暂停"
        require-reason
        reason-label="暂停理由"
        reason-placeholder="为什么需要暂停？该理由会写入证据账本"
        :loading="pauseTask.loading.value"
        @confirm="onPauseConfirm"
      />

      <ConfirmDialog
        v-model="resumeOpen"
        title="恢复运行"
        :description="resumeDescription"
        confirm-text="恢复"
        require-reason
        reason-label="恢复理由"
        reason-placeholder="是否已确认剩余预算足够？该理由会写入证据账本"
        :loading="resumeTask.loading.value"
        @confirm="onResumeConfirm"
      />

      <ConfirmDialog
        v-model="cancelOpen"
        title="取消运行"
        :description="cancelDescription"
        confirm-text="取消运行"
        danger
        require-reason
        reason-label="取消理由"
        reason-placeholder="为什么需要取消？该理由会写入证据账本"
        :loading="cancelTask.loading.value"
        @confirm="onCancelConfirm"
      />

      <DangerConfirmDialog
        v-model="rollbackOpen"
        title="受管回滚确认"
        :description="rollbackDescription"
        :confirm-phrase="rollbackPhrase"
        confirm-text="执行回滚"
        acknowledge="我已阅读回滚方案，并确认清理范围只涉及本次运行的影子数据与错误表残留"
        :loading="rollbackTask.loading.value"
        width="620px"
        @confirm="onRollbackConfirm"
      >
        <RollbackPlanPanel :plan="run.rollback_plan" />
      </DangerConfirmDialog>
    </template>
  </div>
</template>

<style scoped>
.run-detail {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.run-detail__notice {
  display: flex;
  gap: 10px;
  align-items: center;
  font-size: 11px;
  color: var(--elt-text-secondary);
}

.run-detail__notice-item--active {
  color: var(--elt-accent);
}

.run-detail__notice-item--danger {
  color: var(--elt-danger);
}

.run-detail__skeleton {
  padding: 16px;
  background-color: var(--elt-bg-surface);
  border: 1px solid var(--elt-border-subtle);
  border-radius: var(--elt-radius-card);
}

.run-detail__section {
  display: block;
}

.run-detail__grid {
  display: grid;
  grid-template-columns: minmax(0, 2fr) minmax(280px, 1fr);
  gap: 14px;
  align-items: start;
}

.run-detail__locked {
  margin-top: 10px;
}

.run-detail__locked-text {
  margin: 4px 0 8px;
  font-size: 12px;
  line-height: 1.7;
}

.run-detail__foot {
  margin: 0;
  font-size: 11px;
  line-height: 1.7;
  color: var(--elt-text-muted);
}

@media (max-width: 1100px) {
  .run-detail__grid {
    grid-template-columns: minmax(0, 1fr);
  }
}
</style>
