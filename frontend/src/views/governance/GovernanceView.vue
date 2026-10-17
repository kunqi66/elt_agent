<script setup lang="ts">
/**
 * 安全治理与 Benchmark（fe-07）。
 *
 * 为什么是「一页 + 四标签」而不是四条独立路由：
 * 风险策略 / 运行预算 / Benchmark / 证据账本共用同一个项目上下文与同一套只读纪律，
 * 拆成四条路由会让「换个标签看一眼」变成四次路由跳转 + 四次权限入口刷新，
 * 而路由表里 governance 本来就只登记一次。
 *
 * 本页对「读」的三条硬纪律（与 stores/governance.ts 的注释同源）：
 * 1. 账本只读：没有编辑 / 删除 / 补录入口——append-only 是架构约束，不是功能没做完；
 * 2. 验链结论不缓存：每次都用 store.verify() 现取（理由见 LedgerVerifyPanel）；
 * 3. 预算是提示不是裁决：越界由后端最终判定（理由见 BudgetDefaultsPanel）。
 *
 * 标签页数据按「首次进入才拉」懒加载：审计员可能只想看账本，
 * 没必要在打开页面时替他把策略、预算、评测全拉一遍。
 */
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { Refresh } from '@element-plus/icons-vue'
import { PageHeader, SectionCard, AsyncTaskPanel } from '@/components/common'
import { useAsyncTask } from '@/composables/useAsyncTask'
import { usePermission } from '@/composables/usePermission'
import { usePolling } from '@/composables/usePolling'
import { useGovernanceStore } from '@/stores/governance'
import { useProjectStore } from '@/stores/project'
import * as governanceApi from '@/api/governance'
import { toDisplayMessage } from '@/utils/errors'
import { ElMessage } from '@/utils/feedback'
import type {
  BenchmarkRunStatus,
  BudgetDefaults,
  EvidenceEventRead,
  EvidenceFilter,
  PolicyRuleFilter,
  BenchmarkMetrics,
  BenchmarkSuiteRead,
} from '@/types'
import PolicyRuleTable from './components/PolicyRuleTable.vue'
import PolicyVersionList from './components/PolicyVersionList.vue'
import PolicyDiffView from './components/PolicyDiffView.vue'
import BudgetDefaultsPanel from './components/BudgetDefaultsPanel.vue'
import BenchmarkSuiteTable from './components/BenchmarkSuiteTable.vue'
import BenchmarkRunList from './components/BenchmarkRunList.vue'
import BenchmarkMetricsCards from './components/BenchmarkMetricsCards.vue'
import BenchmarkCaseResultTable from './components/BenchmarkCaseResultTable.vue'
import EvidenceEventTable from './components/EvidenceEventTable.vue'
import EvidenceEventDetail from './components/EvidenceEventDetail.vue'
import LedgerVerifyPanel from './components/LedgerVerifyPanel.vue'

type TabName = 'policy' | 'budget' | 'benchmark' | 'evidence'

const TAB_NAMES: readonly TabName[] = ['policy', 'budget', 'benchmark', 'evidence']

const projectStore = useProjectStore()
const store = useGovernanceStore()
const { isAuditor, isCheckerSecurity } = usePermission()

const projectId = computed<number | null>(() => projectStore.currentProjectId)
const hasProject = computed<boolean>(() => projectId.value !== null)

/**
 * 只读身份：审计员的职责是「看」，本模块不给他任何变更入口。
 * 前端隐藏入口只是体验层——真正的裁决在后端角色校验，因此这里不做任何「替后端拒绝」的判断。
 */
const readOnly = computed<boolean>(() => isAuditor.value && !isCheckerSecurity.value)

const activeTab = ref<TabName>('policy')
const refreshing = ref(false)
/** 已经拉过数的标签；避免来回切标签时反复打后端 */
const entered = reactive<Record<TabName, boolean>>({
  policy: false,
  budget: false,
  benchmark: false,
  evidence: false,
})

/* ------------------------------------------------------------------ */
/* 策略                                                               */
/* ------------------------------------------------------------------ */

const selectedVersion = ref<number | null>(null)

async function onPolicyFilter(patch: Partial<PolicyRuleFilter>): Promise<void> {
  await store.policyRules.patchFilter(patch)
}

async function onPolicyPageChange(page: number): Promise<void> {
  await store.policyRules.setPage(page)
}

async function onPolicyPageSizeChange(size: number): Promise<void> {
  await store.policyRules.setPageSize(size)
}

async function onPolicyRetry(): Promise<void> {
  await store.policyRules.reload()
}

async function onSelectVersion(version: number): Promise<void> {
  selectedVersion.value = version
  await store.loadPolicyDiff(version)
}

/* ------------------------------------------------------------------ */
/* 预算                                                               */
/* ------------------------------------------------------------------ */

/**
 * 预算写的落点。
 *
 * 为什么这里直连 api 而不是走 store：`stores/governance.ts` 是只读面的状态容器，
 * 它只提供了 `setBudgetOverride()`（改本地值）与 `loadBudget()`（重取），
 * 没有暴露写动作；而基座已冻结、不得修改。按 CONVENTIONS §0.1「页面可以 import api」
 * 完成写请求：幂等键由 api/client 的拦截器统一注入，防重入与失败保留交给 useAsyncTask，
 * 成功后走 store.loadBudget() 重取——不做乐观更新，越界与否由后端说了算。
 */
const budgetTask = useAsyncTask<[BudgetDefaults]>(
  async (payload: BudgetDefaults) => {
    const id = projectId.value
    if (id === null) {
      // 按钮在无项目时是禁用的，走到这里说明状态已经被别处改过，直接拒绝比发一个错请求好
      throw new Error('尚未选择项目')
    }
    await governanceApi.updateBudgetDefaults(id, payload)
    await store.loadBudget()
  },
  { successMessage: '预算覆盖已保存' },
)
const budgetSaving = budgetTask.loading
const budgetSaveError = budgetTask.error

async function onBudgetRetry(): Promise<void> {
  await store.loadBudget()
}

function onBudgetSubmit(payload: BudgetDefaults): void {
  void budgetTask.run(payload)
}

/* ------------------------------------------------------------------ */
/* Benchmark                                                          */
/* ------------------------------------------------------------------ */

const selectedRunId = ref<number | null>(null)

const selectedSuite = computed<BenchmarkSuiteRead | null>(
  () => store.suites.find((item) => item.id === store.selectedSuiteId) ?? null,
)

/** 指标对比的基线取「本次运行所属套件」的基线，而不是当前选中的套件——两者可以不同 */
const runBaseline = computed<BenchmarkMetrics | null>(() => {
  const run = store.activeRun
  if (run === null) {
    return null
  }
  const suite = store.suites.find((item) => item.id === run.suite_id)
  return suite?.baseline ?? null
})

const runMetrics = computed<BenchmarkMetrics | null>(() => store.activeRun?.metrics ?? null)
const generating = computed<boolean>(() => store.activeRun?.status === 'running')

function isTerminal(status: BenchmarkRunStatus | null | undefined): boolean {
  return status === 'succeeded' || status === 'failed'
}

/**
 * 运行中的评测每 2 秒刷新一次，进入终态（succeeded / failed）后由 shouldStop 自停。
 * 只在 benchmark 标签可见时轮询：没人看的那一页不该继续占着并发。
 */
const polling = usePolling(
  async () => {
    const runId = selectedRunId.value
    if (runId === null) {
      return
    }
    await store.loadBenchmarkRun(runId)
    if (isTerminal(store.activeRun?.status)) {
      // 终态这一刻把逐用例结果补齐：指标是汇总，用例才是能追责的证据
      await store.loadCaseResults(runId)
    }
  },
  {
    intervalMs: 2000,
    immediate: false,
    shouldStop: () => !generating.value,
  },
)

function syncPolling(): void {
  const shouldPoll = activeTab.value === 'benchmark' && generating.value && selectedRunId.value !== null
  if (shouldPoll && !polling.active.value) {
    polling.start()
  } else if (!shouldPoll && polling.active.value) {
    polling.stop()
  }
}

async function onSelectRun(run: { id: number }): Promise<void> {
  selectedRunId.value = run.id
  await store.loadBenchmarkRun(run.id)
  await store.loadCaseResults(run.id)
  syncPolling()
}

async function onBenchmarkPageChange(page: number): Promise<void> {
  await store.benchmarkRuns.setPage(page)
}

async function onBenchmarkPageSizeChange(size: number): Promise<void> {
  await store.benchmarkRuns.setPageSize(size)
}

async function onBenchmarkRetry(): Promise<void> {
  await store.benchmarkRuns.reload()
}

/** 逐用例结果没有独立的分页/筛选，重取的动作就是「按当前选中的运行再拉一次」 */
async function onCaseRetry(): Promise<void> {
  const runId = selectedRunId.value
  if (runId === null) {
    return
  }
  await store.loadCaseResults(runId)
}

function onSelectSuite(id: number): void {
  store.selectedSuiteId = id
}

/* ------------------------------------------------------------------ */
/* 证据账本                                                           */
/* ------------------------------------------------------------------ */

const selectedEventId = ref<number | null>(null)

const selectedEvent = computed<EvidenceEventRead | null>(
  () => store.events.find((event) => event.id === selectedEventId.value) ?? null,
)

const brokenSeqs = computed<number[]>(() => store.brokenEventSeqs)

/**
 * 缓冲裁切提示。
 *
 * store 的账本缓冲有上限（EVIDENCE_BUFFER_LIMIT），超出后最早的事件会被裁掉，
 * 界面必须说出来——否则用户以为「翻到底了」，其实是前面被丢了。
 * 判据刻意用「缓冲里最早一条的 seq 有没有变大」，而不是硬编码那个上限值：
 * store 调整上限后这个提示依然成立，不会变成一句过期的假话。
 */
const bufferHeadSeq = ref<number | null>(null)
const bufferTruncated = ref(false)
const bufferLimitHint = ref<number | null>(null)

watch(
  () => (store.events.length > 0 ? (store.events[0]?.seq ?? null) : null),
  (headSeq) => {
    if (headSeq === null) {
      return
    }
    if (bufferHeadSeq.value === null || headSeq < bufferHeadSeq.value) {
      // 换查询条件后重新起步：重新取基准，别把上一轮的裁切算进来
      bufferHeadSeq.value = headSeq
      bufferTruncated.value = false
      return
    }
    if (headSeq > bufferHeadSeq.value) {
      bufferTruncated.value = true
      bufferLimitHint.value = store.events.length
      bufferHeadSeq.value = headSeq
    }
  },
)

async function loadEvidence(): Promise<void> {
  selectedEventId.value = null
  bufferHeadSeq.value = null
  bufferTruncated.value = false
  bufferLimitHint.value = null
  await store.resetEvidence()
}

async function onEvidenceFilter(patch: Partial<Omit<EvidenceFilter, 'project_id'>>): Promise<void> {
  // 条件变了，旧的游标指向的位置就没有意义了——resetEvidence 会清掉游标再取第一页
  await store.resetEvidence(patch)
}

async function onEvidenceLoadMore(): Promise<void> {
  await store.loadMoreEvidence()
}

async function onEvidenceRetry(): Promise<void> {
  await loadEvidence()
}

function onSelectEvent(event: EvidenceEventRead): void {
  selectedEventId.value = event.id
}

/**
 * 验链。
 * store.verify() 会把失败原样抛出（它没有 catch），所以这里必须兜住——
 * 验链失败与「验链发现断链」是两件事：前者是没验成，后者是验成了但结论是断的。
 */
async function onVerify(): Promise<void> {
  try {
    await store.verify()
  } catch (e) {
    ElMessage.error(toDisplayMessage(e))
  }
}

/* ------------------------------------------------------------------ */
/* 标签调度                                                           */
/* ------------------------------------------------------------------ */

async function loadTab(tab: TabName, force = false): Promise<void> {
  if (!force && entered[tab]) {
    return
  }
  entered[tab] = true
  if (projectId.value === null) {
    return
  }
  switch (tab) {
    case 'policy': {
      await store.policyRules.load()
      await store.loadPolicyVersions()
      // 默认选中最近一版，让 Diff 面板一进来就有东西可读
      const latest = store.policyVersions[0]
      if (latest !== undefined) {
        await onSelectVersion(latest.version)
      }
      return
    }
    case 'budget': {
      await store.loadBudget()
      return
    }
    case 'benchmark': {
      await store.loadSuites()
      await store.benchmarkRuns.load()
      const latestRun = store.benchmarkRuns.items[0]
      if (latestRun !== undefined) {
        await onSelectRun(latestRun)
      }
      return
    }
    case 'evidence': {
      await loadEvidence()
      return
    }
  }
}

function onTabChange(name: string | number): void {
  if (typeof name !== 'string' || !(TAB_NAMES as readonly string[]).includes(name)) {
    return
  }
  activeTab.value = name as TabName
  void loadTab(activeTab.value)
  syncPolling()
}

async function refreshActiveTab(): Promise<void> {
  refreshing.value = true
  try {
    await loadTab(activeTab.value, true)
  } catch (e) {
    ElMessage.error(toDisplayMessage(e))
  } finally {
    refreshing.value = false
  }
}

// 项目切换：账本、预算、套件都是按项目隔离的，必须清干净再取，
// 否则上一个项目的事件会停在屏幕上直到新响应回来——那一瞬间的展示是错的。
watch(projectId, (id) => {
  store.reset()
  selectedVersion.value = null
  selectedRunId.value = null
  selectedEventId.value = null
  bufferHeadSeq.value = null
  bufferTruncated.value = false
  bufferLimitHint.value = null
  polling.stop()
  if (id !== null) {
    void loadTab(activeTab.value, true)
  }
})

onMounted(() => {
  void loadTab('policy')
})
</script>

<template>
  <div class="gov">
    <PageHeader
      title="安全治理与 Benchmark"
      description="风险策略与运行预算的事实来源、Benchmark 三层评测，以及只增不改的证据账本。"
    >
      <template #actions>
        <el-button :icon="Refresh" :loading="refreshing" @click="refreshActiveTab">刷新</el-button>
      </template>
      <template #notice>
        <el-alert
          v-if="!hasProject"
          type="warning"
          :closable="false"
          show-icon
          title="尚未选择项目"
          description="策略、预算、评测与账本都按项目隔离，请先在顶部选择一个项目。"
        />
        <el-alert
          v-else-if="readOnly"
          type="info"
          :closable="false"
          show-icon
          title="当前身份在本模块只读"
          description="审计员可查看全部策略、预算、评测与账本记录；本页不显示任何变更入口，也不会把他人尚未审批的变更集摆出来。"
        />
      </template>
    </PageHeader>

    <el-tabs :model-value="activeTab" class="gov__tabs" @tab-change="onTabChange">
      <el-tab-pane label="风险策略" name="policy" lazy>
        <div class="gov__stack">
          <SectionCard title="策略规则（草稿态）" :padded="false">
            <template #extra>
              <span class="gov__hint">
                风险评级只由这张规则表的条件求值决定，是确定性的；模型不参与评级。
              </span>
            </template>
            <PolicyRuleTable
              :items="store.policyRules.items"
              :loading="store.policyRules.loading"
              :initial-loading="store.policyRules.initialLoading"
              :error="store.policyRules.error"
              :total="store.policyRules.total"
              :page="store.policyRules.filter.page"
              :page-size="store.policyRules.filter.page_size"
              :filter="store.policyRules.filter"
              @update-filter="onPolicyFilter"
              @page-change="onPolicyPageChange"
              @page-size-change="onPolicyPageSizeChange"
              @retry="onPolicyRetry"
            />
          </SectionCard>

          <SectionCard title="策略版本与变更 Diff">
            <div class="gov__split">
              <PolicyVersionList
                :items="store.policyVersions"
                :loading="store.policyVersionsLoading"
                :selected="selectedVersion"
                @select="onSelectVersion"
              />
              <PolicyDiffView
                :version="selectedVersion"
                :entries="store.policyDiff"
                :summary="store.policyDiffSummary"
              />
            </div>
          </SectionCard>
        </div>
      </el-tab-pane>

      <el-tab-pane label="预算" name="budget" lazy>
        <SectionCard title="运行预算（冻结默认值 / 硬上限 / 项目覆盖）">
          <BudgetDefaultsPanel
            :data="store.budget"
            :loading="store.budgetLoading"
            :error="store.budgetError"
            :editable="!readOnly"
            :over-limit="store.budgetOverLimit"
            @retry="onBudgetRetry"
            @submit="onBudgetSubmit"
          />
          <AsyncTaskPanel
            v-if="budgetSaving || budgetSaveError !== null"
            :loading="budgetSaving"
            :error="budgetSaveError"
            loading-text="正在提交预算覆盖…"
          />
        </SectionCard>
      </el-tab-pane>

      <el-tab-pane label="Benchmark" name="benchmark" lazy>
        <div class="gov__stack">
          <SectionCard title="评测套件（L0 / L1 / L2）">
            <BenchmarkSuiteTable
              :items="store.suites"
              :loading="store.suitesLoading"
              :selected-id="store.selectedSuiteId"
              @select="onSelectSuite"
            />
            <p class="gov__hint">
              L0 固定输入比对结论；L1 注入脏数据与结构漂移，验证质量分流与硬中断；L2 打真实链路，消耗真实预算。
              基线尚未写入的套件不做对比，只报当前值。
            </p>
          </SectionCard>

          <SectionCard title="评测运行" :padded="false">
            <template #extra>
              <span v-if="generating" class="gov__polling">评测进行中，每 2 秒自动刷新，进入终态后自动停止</span>
              <span v-if="selectedSuite !== null" class="gov__hint">当前套件：{{ selectedSuite.name }}</span>
            </template>
            <BenchmarkRunList
              :items="store.benchmarkRuns.items"
              :loading="store.benchmarkRuns.loading"
              :initial-loading="store.benchmarkRuns.initialLoading"
              :error="store.benchmarkRuns.error"
              :total="store.benchmarkRuns.total"
              :page="store.benchmarkRuns.filter.page"
              :page-size="store.benchmarkRuns.filter.page_size"
              :selected-run-id="selectedRunId"
              @select="onSelectRun"
              @page-change="onBenchmarkPageChange"
              @page-size-change="onBenchmarkPageSizeChange"
              @retry="onBenchmarkRetry"
            />
          </SectionCard>

          <SectionCard
            title="本次运行指标"
            :loading="generating && store.activeRun === null"
            :skeleton-height="120"
          >
            <BenchmarkMetricsCards
              :metrics="runMetrics"
              :baseline="runBaseline"
              :pending="generating"
            />
          </SectionCard>

          <SectionCard title="逐用例结果" :padded="false">
            <BenchmarkCaseResultTable
              :items="store.caseResults"
              :loading="store.caseResultsLoading"
              :has-run="selectedRunId !== null"
              @retry="onCaseRetry"
            />
          </SectionCard>
        </div>
      </el-tab-pane>

      <el-tab-pane label="证据账本" name="evidence" lazy>
        <div class="gov__stack">
          <SectionCard title="完整性验链">
            <LedgerVerifyPanel
              :result="store.verifyResult"
              :verifying="store.verifying"
              :checked-events-in-view="store.events.length"
              @verify="onVerify"
            />
          </SectionCard>

          <SectionCard title="证据事件" :padded="false">
            <template #extra>
              <span class="gov__hint">
                账本 append-only：本页只有查询与验链，没有任何编辑、删除或补录入口。
              </span>
            </template>
            <EvidenceEventTable
              :items="store.events"
              :loading="store.evidenceLoading"
              :error="store.evidenceError"
              :filter="store.evidenceQuery"
              :has-more="store.hasMoreEvidence"
              :broken-seqs="brokenSeqs"
              :selected-id="selectedEventId"
              :truncated="bufferTruncated"
              :truncated-limit="bufferLimitHint"
              @update-filter="onEvidenceFilter"
              @load-more="onEvidenceLoadMore"
              @retry="onEvidenceRetry"
              @select="onSelectEvent"
            />
          </SectionCard>

          <SectionCard title="事件详情">
            <EvidenceEventDetail :event="selectedEvent" :block="null" />
          </SectionCard>
        </div>
      </el-tab-pane>
    </el-tabs>
  </div>
</template>

<style scoped>
.gov {
  display: flex;
  flex-direction: column;
}

.gov__tabs :deep(.el-tabs__item) {
  font-size: 13px;
}

.gov__stack {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.gov__split {
  display: grid;
  grid-template-columns: minmax(260px, 340px) minmax(0, 1fr);
  gap: 16px;
}

/* 窄屏下并排会挤成两条竖线，直接改成上下堆叠 */
@media (width <= 1080px) {
  .gov__split {
    grid-template-columns: minmax(0, 1fr);
  }
}

.gov__hint {
  font-size: 12px;
  line-height: 1.7;
  color: var(--elt-text-muted);
}

.gov__polling {
  font-size: 12px;
  color: var(--elt-accent);
}
</style>

