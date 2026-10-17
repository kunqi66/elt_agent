<script setup lang="ts">
/**
 * 总览工作台（fe-02）。
 *
 * 这一层只做编排，不做计算：
 * 1. **一次 `GET /monitor/summary`** 喂满资产 / 健康度 / 核心指标 / 趋势 / 分布
 *    （六张卡各发一个请求会把首屏打散，也让轮询变成六个定时器）；
 * 2. **一次 `GET /preparations?scope=todo`** 拿待审批，与 summary 各自维护三态，互不阻塞；
 * 3. **轮询节流**：有运行中实例 2s、空闲 30s（fe-01 §5.4）；403 / 无项目即停，
 *    失败退避回 30s，切后台自动暂停（都在 usePolling 里）。
 *
 * 三态纪律（CONVENTIONS §1）：
 * - 加载失败绝不渲染成空态——首屏失败给整块 ErrorState + 重试；
 * - 轮询失败**不**降级为错误页，保留旧数据 + 顶部黄条（否则 2s 闪一次错误卡片）；
 * - 切项目先清空旧数据，宁可看一帧骨架，也不要把上一个项目的数字留在屏幕上。
 */
import { computed, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { Refresh, WarningFilled } from '@element-plus/icons-vue'
import { BizError } from '@/api/client'
import { fetchMonitorSummary } from '@/api/monitor'
import { fetchPreparations } from '@/api/preparation'
import { EmptyState, ErrorState, PageHeader, SectionCard } from '@/components/common'
import { usePermission } from '@/composables/usePermission'
import { usePolling } from '@/composables/usePolling'
import { useAppStore } from '@/stores/app'
import { useProjectStore } from '@/stores/project'
import { ElMessage } from '@/utils/feedback'
import { isBizError, toDisplayMessage } from '@/utils/errors'
import { formatDateTime, formatUtc } from '@/utils/format'
import type { MonitorRange, MonitorSummary, PendingPreparationBrief } from '@/types'
import CoreMetricsCard from './components/CoreMetricsCard.vue'
import HealthCard from './components/HealthCard.vue'
import PendingPreparationCard from './components/PendingPreparationCard.vue'
import QuickActions from './components/QuickActions.vue'
import ResourceDistributionChart from './components/ResourceDistributionChart.vue'
import StatCards from './components/StatCards.vue'
import ThroughputTrendChart from './components/ThroughputTrendChart.vue'

/** 有活跃运行时的轮询周期；《需求对齐记录》§二.7 定为 2s，不引 WebSocket */
const FAST_INTERVAL_MS = 2000
/** 空闲时的轮询周期（fe-01 §5.4）；也是失败退避后的周期 */
const IDLE_INTERVAL_MS = 30000
/** 待审批列表的重取节奏：它不随健康度一起 2s 一跳 */
const PENDING_REFRESH_MS = 30000
const PENDING_PAGE_SIZE = 5

const RANGE_OPTIONS: ReadonlyArray<{ value: MonitorRange; label: string }> = [
  { value: '24h', label: '24 小时' },
  { value: '7d', label: '7 天' },
  { value: '30d', label: '30 天' },
]

/** 这三类拒绝是终态：重试与轮询都只会再被拒一次（fe-02 §7） */
const TERMINAL_DENY_CODES: readonly string[] = [
  'FORBIDDEN',
  'TENANT_MISMATCH',
  'ROLE_REQUIRED',
  'NOT_FOUND',
]

const projectStore = useProjectStore()
const appStore = useAppStore()
const router = useRouter()
const { roles } = usePermission()

const summary = ref<MonitorSummary | null>(null)
const summaryError = ref<BizError | null>(null)
const summaryLoading = ref(false)
const pendingItems = ref<PendingPreparationBrief[]>([])
const pendingError = ref<BizError | null>(null)
const pendingLoading = ref(false)
/** 轮询失败但仍有数据：黄条提示，不清空、不报错页 */
const staleWarning = ref(false)
/** 切时间窗时的局部 loading，只影响依赖窗口的三张卡 */
const rangeLoading = ref(false)
const refreshing = ref(false)
const consecutiveFailures = ref(0)
const lastErrorCode = ref<string | null>(null)

/** 请求序号：切项目 / 切时间窗时让在途响应作废，避免旧数据落到新界面上 */
let summarySeq = 0
let pendingSeq = 0
let lastPendingAt = 0
/** 已为哪个项目装载过首屏；onMounted 与项目切换都能触发装载，用它去重 */
let screenProjectId: number | null = null

function asBizError(e: unknown): BizError {
  if (isBizError(e)) {
    return e
  }
  // client 只会 reject BizError；兜一层是为了让子组件拿到确定的错误类型
  return new BizError(e instanceof Error ? e.message : String(e), 'INTERNAL_ERROR', '', 0)
}

async function loadSummary(options: { withSkeleton: boolean }): Promise<void> {
  const projectId = projectStore.currentProjectId
  if (projectId === null) {
    return
  }
  const ticket = ++summarySeq
  if (options.withSkeleton) {
    summaryLoading.value = true
  }
  try {
    const data = await fetchMonitorSummary(projectId, appStore.monitorRange)
    if (ticket !== summarySeq) {
      return
    }
    summary.value = data
    summaryError.value = null
    staleWarning.value = false
    consecutiveFailures.value = 0
    lastErrorCode.value = null
  } catch (e) {
    if (ticket !== summarySeq) {
      return
    }
    const error = asBizError(e)
    consecutiveFailures.value += 1
    lastErrorCode.value = error.code
    if (summary.value === null) {
      summaryError.value = error
    } else {
      // 已有数据时保留旧值：2s 一次的整块错误态比旧数据更糟（fe-02 §5.3 非法转移处理）
      staleWarning.value = true
    }
    // range 取值不被后端接受时回退到 24h，否则会一直拿到 422
    if (error.code === 'VALIDATION_ERROR' && appStore.monitorRange !== '24h') {
      ElMessage.warning('时间范围取值不被接受，已回退到 24 小时')
      appStore.setMonitorRange('24h')
    }
  } finally {
    if (ticket === summarySeq) {
      summaryLoading.value = false
    }
  }
}

async function loadPending(): Promise<void> {
  const projectId = projectStore.currentProjectId
  if (projectId === null) {
    return
  }
  const ticket = ++pendingSeq
  if (pendingItems.value.length === 0) {
    pendingLoading.value = true
  }
  try {
    const page = await fetchPreparations(projectId, {
      scope: 'todo',
      page: 1,
      page_size: PENDING_PAGE_SIZE,
    })
    if (ticket !== pendingSeq) {
      return
    }
    pendingItems.value = page.items
    pendingError.value = null
    lastPendingAt = Date.now()
  } catch (e) {
    if (ticket !== pendingSeq) {
      return
    }
    pendingError.value = asBizError(e)
  } finally {
    if (ticket === pendingSeq) {
      pendingLoading.value = false
    }
  }
}

async function loadFirstScreen(): Promise<void> {
  lastPendingAt = Date.now()
  await Promise.all([loadSummary({ withSkeleton: true }), loadPending()])
}

/** 按需装载首屏；onMounted 与项目切换可能同时触发，用项目 id 去重 */
async function ensureScreenLoaded(): Promise<void> {
  const projectId = projectStore.currentProjectId
  if (projectId === null || screenProjectId === projectId) {
    return
  }
  screenProjectId = projectId
  await loadFirstScreen()
  syncPollers()
}

async function pollTick(): Promise<void> {
  await loadSummary({ withSkeleton: false })
  // 待审批列表 30s 一次就够，跟着 2s 的拍子走等于白打 15 次
  if (Date.now() - lastPendingAt >= PENDING_REFRESH_MS) {
    await loadPending()
  }
}

/** 终态自停：没有项目、或后端明确拒绝访问，再轮询只是持续撞墙 */
function shouldStopPolling(): boolean {
  if (projectStore.currentProjectId === null) {
    return true
  }
  return TERMINAL_DENY_CODES.includes(summaryError.value?.code ?? '')
}

const fastPoll = usePolling(pollTick, {
  intervalMs: FAST_INTERVAL_MS,
  immediate: false,
  shouldStop: shouldStopPolling,
})

const idlePoll = usePolling(pollTick, {
  intervalMs: IDLE_INTERVAL_MS,
  immediate: false,
  shouldStop: shouldStopPolling,
})

/** 连续失败时退回 30s：后端已经不稳，2s 一次只会把压力放大（fe-02 §7） */
const shouldPollFast = computed<boolean>(
  () => (summary.value?.health.run_running ?? 0) > 0 && consecutiveFailures.value === 0,
)

/** 两个轮询器互斥，任何时刻只有一个在跑（先停后起，避免同一拍发两个请求） */
function syncPollers(): void {
  if (shouldStopPolling()) {
    fastPoll.stop()
    idlePoll.stop()
    return
  }
  if (shouldPollFast.value) {
    idlePoll.stop()
    fastPoll.start()
  } else {
    fastPoll.stop()
    idlePoll.start()
  }
}

watch(shouldPollFast, syncPollers)

watch(
  () => projectStore.currentProjectId,
  async (next, prev) => {
    if (next === prev) {
      return
    }
    fastPoll.stop()
    idlePoll.stop()
    // 切项目先清空并作废在途响应：绝不能把上一个项目的数字留在屏幕上（fe-02 §5.3）
    summarySeq += 1
    pendingSeq += 1
    summary.value = null
    summaryError.value = null
    pendingItems.value = []
    pendingError.value = null
    staleWarning.value = false
    consecutiveFailures.value = 0
    lastErrorCode.value = null
    screenProjectId = null
    if (next === null) {
      return
    }
    await ensureScreenLoaded()
  },
)

watch(
  () => appStore.monitorRange,
  async () => {
    // 切时间窗是用户动作：立即重取并把依赖窗口的区块置 loading，但不改轮询周期
    rangeLoading.value = true
    try {
      await loadSummary({ withSkeleton: false })
    } finally {
      rangeLoading.value = false
    }
  },
)

onMounted(async () => {
  try {
    await projectStore.ensureLoaded()
  } catch {
    // 项目列表拉不到时按「未选择项目」渲染，别把用户永久卡在骨架屏里
  }
  await ensureScreenLoaded()
})

async function reloadAll(): Promise<void> {
  refreshing.value = true
  consecutiveFailures.value = 0
  try {
    await Promise.all([loadSummary({ withSkeleton: summary.value === null }), loadPending()])
  } finally {
    refreshing.value = false
  }
  syncPollers()
}

async function retryPending(): Promise<void> {
  await loadPending()
}

async function reloadProjects(): Promise<void> {
  try {
    await projectStore.loadProjects()
  } catch (e) {
    ElMessage.error(toDisplayMessage(e))
  }
}

function onRangeChange(value: unknown): void {
  // 前端白名单：非法取值不发出去；后端还有一次 VALIDATION_ERROR 兜底
  const matched = RANGE_OPTIONS.find((option) => option.value === value)
  if (matched === undefined) {
    ElMessage.warning('不支持的时间范围')
    return
  }
  appStore.setMonitorRange(matched.value)
}

function openPreparation(preparationId: number): void {
  void router.push(`/approvals/${preparationId}`)
}

const pageState = computed<'no-project' | 'denied' | 'missing' | 'ready'>(() => {
  if (projectStore.currentProjectId === null) {
    return 'no-project'
  }
  const code = summaryError.value?.code
  if (code === 'FORBIDDEN' || code === 'TENANT_MISMATCH' || code === 'ROLE_REQUIRED') {
    return 'denied'
  }
  if (code === 'NOT_FOUND') {
    return 'missing'
  }
  return 'ready'
})

const summaryInitialLoading = computed<boolean>(
  () => summary.value === null && summaryLoading.value,
)
const pendingInitialLoading = computed<boolean>(
  () => pendingItems.value.length === 0 && pendingLoading.value,
)
const summaryFailed = computed<boolean>(() => summary.value === null && summaryError.value !== null)
const summaryErrorText = computed<string | null>(() =>
  summaryError.value === null ? null : toDisplayMessage(summaryError.value),
)

/** summary 已带待审批列表时以它为准，避免两处口径打架（fe-02 §5.1 第 4 条） */
const pendingFromSummary = computed<PendingPreparationBrief[]>(
  () => summary.value?.pending_preparations ?? [],
)
const visiblePending = computed<PendingPreparationBrief[]>(() =>
  pendingFromSummary.value.length > 0 ? pendingFromSummary.value : pendingItems.value,
)
/** summary 已给出列表时，独立接口的失败不再展示——那是「另一条路」出错，不是这块没数据 */
const pendingDisplayError = computed<BizError | null>(() =>
  pendingFromSummary.value.length > 0 ? null : pendingError.value,
)

const rangeLabel = computed<string>(
  () =>
    RANGE_OPTIONS.find((option) => option.value === appStore.monitorRange)?.label ??
    appStore.monitorRange,
)

const headerDescription = computed<string>(() => {
  const name = projectStore.currentProject?.name ?? '当前项目'
  if (summary.value === null) {
    return `${name} · 指标由服务端聚合，前端只做单位换算`
  }
  return `${name} · 窗口 ${rangeLabel.value} · 数据时间 ${formatDateTime(summary.value.generated_at)}`
})
</script>

<template>
  <div class="dashboard">
    <PageHeader title="总览工作台" :description="headerDescription">
      <template #actions>
        <el-radio-group
          :model-value="appStore.monitorRange"
          size="small"
          :disabled="pageState !== 'ready'"
          @change="onRangeChange"
        >
          <el-radio-button
            v-for="option in RANGE_OPTIONS"
            :key="option.value"
            :value="option.value"
          >
            {{ option.label }}
          </el-radio-button>
        </el-radio-group>
        <el-button
          size="small"
          :icon="Refresh"
          :loading="refreshing"
          :disabled="pageState !== 'ready'"
          @click="reloadAll"
        >
          刷新
        </el-button>
      </template>

      <template #notice>
        <p
          v-if="summary !== null"
          class="dashboard__generated"
          :title="formatUtc(summary.generated_at)"
        >
          数据时间 {{ formatDateTime(summary.generated_at) }}（悬停查看 UTC）
        </p>
        <!-- 轮询失败不清空数据：黄条说明「可能是旧的」，并给出错误码便于对账 -->
        <p v-if="staleWarning" class="dashboard__stale" role="status">
          <el-icon :size="12"><WarningFilled /></el-icon>
          <span>数据可能已过期：最近一次刷新失败，正在按退避周期自动重试。</span>
          <span v-if="lastErrorCode !== null" class="dashboard__stale-code elt-mono">
            {{ lastErrorCode }}
          </span>
        </p>
      </template>
    </PageHeader>

    <!-- 未选择项目：不发任何请求（fe-02 §5.1 第 1 条） -->
    <SectionCard v-if="pageState === 'no-project'" title="总览工作台">
      <EmptyState
        variant="no-data"
        title="尚未选择项目"
        description="总览数据按项目隔离。请先在顶部项目切换器中选择一个项目；若项目列表还没加载出来，可点下方按钮重试。"
      >
        <template #actions>
          <el-button size="small" :loading="projectStore.loading" @click="reloadProjects">
            加载项目列表
          </el-button>
        </template>
      </EmptyState>
    </SectionCard>

    <!-- 403 / 跨租户：只说事实，不给重试按钮（重试只会再被拒一次） -->
    <SectionCard v-else-if="pageState === 'denied'" title="总览工作台">
      <EmptyState
        variant="no-permission"
        title="无权查看该项目"
        description="当前账号在该项目下没有可见范围。如需访问，请联系项目管理员调整成员资格或职责槽。"
      />
    </SectionCard>

    <!-- 项目不存在：页面级空态 + 回项目列表（fe-02 §7） -->
    <SectionCard v-else-if="pageState === 'missing'" title="总览工作台">
      <EmptyState
        variant="no-data"
        title="项目不存在或已不可访问"
        description="该项目可能已被删除，或你的成员资格已被移除。"
      >
        <template #actions>
          <el-button size="small" :loading="projectStore.loading" @click="reloadProjects">
            重新加载项目列表
          </el-button>
        </template>
      </EmptyState>
    </SectionCard>

    <template v-else>
      <StatCards v-if="!summaryFailed" :assets="summary?.assets ?? null" :loading="summaryInitialLoading" />

      <div class="dashboard__grid">
        <template v-if="!summaryFailed">
          <HealthCard
            class="dashboard__span-5"
            :health="summary?.health ?? null"
            :loading="summaryInitialLoading || rangeLoading"
          />
          <CoreMetricsCard
            class="dashboard__span-4"
            :metrics="summary?.metrics ?? null"
            :loading="summaryInitialLoading || rangeLoading"
          />
          <QuickActions
            class="dashboard__span-3"
            :roles="roles"
            :pending-count="visiblePending.length"
          />
          <ThroughputTrendChart
            class="dashboard__span-8"
            :series="summary?.series ?? null"
            :loading="summaryInitialLoading || rangeLoading"
          />
        </template>

        <!-- 首屏失败：整块错误态 + 重试。绝不退化成 0 / '—' 的「空数据」 -->
        <SectionCard v-else class="dashboard__span-8" title="项目总览">
          <ErrorState
            :message="summaryErrorText"
            :request-id="summaryError?.requestId ?? null"
            @retry="reloadAll"
          />
        </SectionCard>

        <!-- 待审批走独立接口：summary 挂了也要能渲染（fe-02 §5.1 第 3 条） -->
        <PendingPreparationCard
          class="dashboard__span-4"
          :items="visiblePending"
          :loading="pendingInitialLoading"
          :error="pendingDisplayError"
          @open="openPreparation"
          @retry="retryPending"
        />

        <ResourceDistributionChart
          v-if="!summaryFailed"
          class="dashboard__span-12"
          :distribution="summary?.distribution ?? null"
          :loading="summaryInitialLoading"
        />
      </div>
    </template>
  </div>
</template>

<style scoped>
.dashboard {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.dashboard__generated {
  margin: 0;
  font-size: 11px;
  line-height: 16px;
  color: var(--elt-text-muted);
}

.dashboard__stale {
  display: flex;
  align-items: center;
  gap: 6px;
  margin: 4px 0 0;
  padding: 6px 10px;
  font-size: 12px;
  line-height: 18px;
  color: var(--elt-warn);
  background-color: var(--elt-badge-warn);
  border-radius: var(--elt-radius-control);
}

.dashboard__stale-code {
  margin-left: auto;
  color: var(--elt-text-secondary);
}

.dashboard__grid {
  display: grid;
  grid-template-columns: repeat(12, minmax(0, 1fr));
  gap: 14px;
}

.dashboard__span-3 {
  grid-column: span 3;
}

.dashboard__span-4 {
  grid-column: span 4;
}

.dashboard__span-5 {
  grid-column: span 5;
}

.dashboard__span-8 {
  grid-column: span 8;
}

.dashboard__span-12 {
  grid-column: span 12;
}

/* 窄屏：先降成两列，再降成单列，避免图表被挤到读不出刻度 */
@media (max-width: 1280px) {
  .dashboard__span-3,
  .dashboard__span-4,
  .dashboard__span-5 {
    grid-column: span 6;
  }

  .dashboard__span-8 {
    grid-column: span 12;
  }
}

@media (max-width: 900px) {
  .dashboard__span-3,
  .dashboard__span-4,
  .dashboard__span-5,
  .dashboard__span-8,
  .dashboard__span-12 {
    grid-column: span 12;
  }
}
</style>
