<script setup lang="ts">
/**
 * Pipeline Studio（fe-04 主页面）。
 *
 * 四区布局（§5.1）：左=输入与澄清，中=制品页签，右=门禁/修复/契约/预算，底=版本条。
 * 三列各自滚动，页面本身不滚——Studio 是要长时间盯着的页面，
 * 页面级滚动会把「冻结按钮」和「阻断项」滚出视野，而那两样恰恰要同时可见。
 *
 * 状态归属（**这是本页最重要的一条约定**）：
 * - 生成流程状态（thread_id / phase / pending_questions / 制品 / 门禁）全部在
 *   `stores/pipeline.ts`，本页只读它、调它的 action；
 * - 本页只持有**纯界面状态**：当前页签、选中节点、列宽、弹窗开关。
 *   这些丢了不影响正确性，所以不进 store，也不进 localStorage（除列宽这个 UI 偏好）。
 *
 * 刷新恢复：thread_id 是后端的工作流线程标识，写进路由 query 后刷新可原样带回来，
 * 由 `restoreFromSnapshot` 从 PG checkpoint 重放状态。**不在组件里缓存任何流程状态**——
 * 那会让「刷新后看到的东西」和「后端认为的进度」悄悄分叉。
 *
 * 制品标识：`frozenVersionId ?? pipeline.current_version_id`。
 * 生成刚完成但服务端还没给出可冻结标识时，本页如实告知并禁用冻结，而不是编一个 id 出来。
 */
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ArrowLeft, Refresh } from '@element-plus/icons-vue'
import {
  ArtifactHash,
  AsyncTaskPanel,
  EmptyState,
  ErrorState,
  PageHeader,
  SectionCard,
  SensitiveText,
  StatusTag,
} from '@/components/common'
import { createPreparation } from '@/api/preparation'
import { fetchProfile, fetchProfiles, fetchSchema } from '@/api/datasource'
import { useDataSourceStore } from '@/stores/datasource'
import { usePipelineStore } from '@/stores/pipeline'
import { useProjectStore } from '@/stores/project'
import { toDisplayMessage } from '@/utils/errors'
import { ElMessage, ElMessageBox } from '@/utils/feedback'
import { formatRatioPercent } from '@/utils/format'
import AutofixPanel from './components/AutofixPanel.vue'
import BudgetPanel from './components/BudgetPanel.vue'
import ClarificationPanel from './components/ClarificationPanel.vue'
import DagTab from './components/DagTab.vue'
import FieldMappingTab from './components/FieldMappingTab.vue'
import FreezeConfirmDialog from './components/FreezeConfirmDialog.vue'
import GatePanel from './components/GatePanel.vue'
import HoconTab from './components/HoconTab.vue'
import PlanTab from './components/PlanTab.vue'
import QualityContractPanel from './components/QualityContractPanel.vue'
import RequirementPanel from './components/RequirementPanel.vue'
import StudioPhaseTag from './components/StudioPhaseTag.vue'
import VersionBar from './components/VersionBar.vue'
import VersionDiffDialog from './components/VersionDiffDialog.vue'
import type { MetadataProfile } from '@/types'
import type { FreezeArtifactManifest, GateLocator, StartGenerationPayload, StudioPhase } from '@/types'

const route = useRoute()
const router = useRouter()
const store = usePipelineStore()
const projectStore = useProjectStore()
const dataSourceStore = useDataSourceStore()

/** be-11 的 LLM_AUTOFIX_MAX_ROUNDS；仅用于显示「第 n / 3 轮」，上限判定始终在后端 */
const AUTOFIX_MAX_ROUNDS = 3

const LAYOUT_KEY = 'elt-agent.studio-layout'
const NARROW_QUERY = '(max-width: 1279px)'

type TabKey = 'plan' | 'hocon' | 'dag' | 'fields' | 'gate'

const pipelineId = computed<number | null>(() => {
  const raw = route.params.id
  const value = Array.isArray(raw) ? raw[0] : raw
  const parsed = Number(value)
  return Number.isInteger(parsed) && parsed > 0 ? parsed : null
})

/* ------------------------------------------------------------------ */
/* 纯界面状态                                                          */
/* ------------------------------------------------------------------ */

const activeTab = ref<TabKey>('plan')
const selectedNodeId = ref<string | null>(null)
const viewingVersionId = ref<number | null>(null)
const narrow = ref<boolean>(false)

const leftWidth = ref<number>(340)
const rightWidth = ref<number>(380)

const freezeDialogVisible = ref<boolean>(false)
const diffDialogVisible = ref<boolean>(false)
const freezeLoading = ref<boolean>(false)
const freezeError = ref<string | null>(null)
const submitLoading = ref<boolean>(false)
const submitError = ref<string | null>(null)
/** 本次会话已提交的准备单号；仅界面态，刷新后由后端「同版本不能重复提」兜底 */
const submittedPreparationId = ref<number | null>(null)

/** store.lastError 没有清除入口（只能被下一次快照覆盖），用一个「已读」标记把它收起来 */
const dismissedError = ref<string | null>(null)

const profile = ref<MetadataProfile | null>(null)
const profileLoading = ref<boolean>(false)
const fieldOptions = ref<Array<{ value: string; label: string; group: string }>>([])
const fieldOptionsLoading = ref<boolean>(false)
const fieldOptionsError = ref<string | null>(null)

/* ------------------------------------------------------------------ */
/* 派生值                                                              */
/* ------------------------------------------------------------------ */

const workingVersionId = computed<number | null>(
  () => store.frozenVersionId ?? store.pipeline?.current_version_id ?? null,
)

const visibleError = computed<string | null>(() =>
  store.lastError !== null && store.lastError !== dismissedError.value ? store.lastError : null,
)

/** 已冻结/已提交 → 全页只读（后端另有 VERSION_IMMUTABLE 兜底，这里只是别让人白改） */
const readonly = computed<boolean>(
  () =>
    store.frozenVersionId !== null ||
    store.phase === 'frozen' ||
    store.phase === 'submitted' ||
    submittedPreparationId.value !== null,
)

const effectivePhase = computed<StudioPhase>(() =>
  submittedPreparationId.value === null ? store.phase : 'submitted',
)

const activeLocator = computed<GateLocator | null>(() => store.activeFinding?.locator ?? null)

/** 门禁定位直接驱动 DAG 高亮（与用户点选分开：定位优先被看见） */
const locatedNodeId = computed<string | null>(() =>
  activeLocator.value?.kind === 'dag_node' ? activeLocator.value.node_id : null,
)

const hoconFindings = computed(() => store.gate?.findings.filter((f) => f.locator.kind === 'hocon') ?? [])
const dagFindings = computed(() => store.gate?.findings.filter((f) => f.locator.kind === 'dag_node') ?? [])
const fieldFindings = computed(
  () => store.gate?.findings.filter((f) => f.locator.kind === 'plan' || f.locator.kind === 'source_table') ?? [],
)

const activePlanPath = computed<string | null>(() =>
  activeLocator.value?.kind === 'plan' ? activeLocator.value.path : null,
)

const activeSourceTable = computed<{ data_source_id: number; schema: string; table: string } | null>(() =>
  activeLocator.value?.kind === 'source_table' ? activeLocator.value : null,
)

/** 冻结清单预览：全部由已有只读制品拼出，前端不算任何哈希 */
const freezePreview = computed<FreezeArtifactManifest | null>(() => {
  const plan = store.plan
  const hocon = store.hocon
  if (plan === null || hocon === null) {
    return null
  }
  const p = profile.value
  return {
    plan: { schema_version: plan.schema_version, valid: plan.valid },
    hocon: { sha256: hocon.sha256, parser_version: hocon.parser_version },
    quality_contract: {
      rule_count: plan.plan.quality_rules.filter((r) => r.enabled).length,
      reject_rate_budget: plan.plan.budget.max_reject_rate,
    },
    supervision: plan.plan.budget,
    metadata_profile: {
      version: p?.version ?? 0,
      sha256: p?.sha256 ?? '',
      expires_at: p?.expires_at ?? null,
    },
  }
})

/** 快照过期只做提醒；是否拒绝由后端（METADATA_PROFILE_EXPIRED）决定 */
const profileExpired = computed<boolean>(() => {
  const expires = profile.value?.expires_at ?? null
  if (expires === null) {
    return false
  }
  return new Date(expires).getTime() < Date.now()
})

const autofixExhausted = computed<boolean>(
  () => store.gate?.autofix_exhausted === true || store.autofixRound >= AUTOFIX_MAX_ROUNDS,
)

const tabItems = computed<Array<{ key: TabKey; label: string; badge: number }>>(() => {
  const items: Array<{ key: TabKey; label: string; badge: number }> = [
    { key: 'plan', label: 'EtlPlan', badge: 0 },
    { key: 'hocon', label: 'HOCON', badge: hoconFindings.value.length },
    { key: 'dag', label: 'DAG', badge: dagFindings.value.length },
    { key: 'fields', label: '字段映射', badge: fieldFindings.value.length },
  ]
  if (narrow.value) {
    items.push({ key: 'gate', label: '门禁', badge: store.blockingFindings.length })
  }
  return items
})

/* ------------------------------------------------------------------ */
/* 加载                                                                */
/* ------------------------------------------------------------------ */

async function loadSourceMetadata(sourceId: number): Promise<void> {
  profileLoading.value = true
  try {
    const briefs = await fetchProfiles(sourceId)
    // 取版本号最大的 ready 快照：探查失败/进行中的版本不能用来对字段
    const ready = briefs
      .filter((b) => b.status === 'ready')
      .sort((a, b) => b.version - a.version)[0]
    if (ready === undefined) {
      profile.value = null
      fieldOptionsError.value = '源数据源尚无可用探查快照，请先到「数据源」页完成元数据探查。'
      return
    }
    profile.value = await fetchProfile(sourceId, ready.version)
    fieldOptionsError.value = null
  } catch (e) {
    profile.value = null
    fieldOptionsError.value = toDisplayMessage(e)
  } finally {
    profileLoading.value = false
  }
}

/** field_ref 型问题只能从探查快照里选值，因此按源/目标两侧的表建候选 */
async function loadFieldOptions(): Promise<void> {
  const ids = [store.pipeline?.source_data_source_id, store.pipeline?.target_data_source_id].filter(
    (id): id is number => typeof id === 'number',
  )
  if (ids.length === 0) {
    return
  }
  fieldOptionsLoading.value = true
  fieldOptionsError.value = null
  try {
    const options: Array<{ value: string; label: string; group: string }> = []
    for (const id of ids) {
      const briefs = await fetchProfiles(id)
      const ready = briefs
        .filter((b) => b.status === 'ready')
        .sort((a, b) => b.version - a.version)[0]
      if (ready === undefined) {
        continue
      }
      const snapshot = await fetchSchema(id, ready.version)
      for (const table of snapshot.tables) {
        const group = `${table.schema}.${table.name}`
        for (const column of table.columns) {
          options.push({
            value: `${group}.${column.name}`,
            label: `${column.name} : ${column.data_type}`,
            group,
          })
        }
      }
    }
    fieldOptions.value = options
    if (options.length === 0) {
      fieldOptionsError.value = '探查快照里没有可用字段，请先完成元数据探查。'
    }
  } catch (e) {
    fieldOptionsError.value = toDisplayMessage(e)
  } finally {
    fieldOptionsLoading.value = false
  }
}

function readLayout(): void {
  try {
    const raw = window.localStorage.getItem(LAYOUT_KEY)
    if (raw === null) {
      return
    }
    const parsed = JSON.parse(raw) as { left?: number; right?: number }
    if (typeof parsed.left === 'number') {
      leftWidth.value = clampLeft(parsed.left)
    }
    if (typeof parsed.right === 'number') {
      rightWidth.value = clampRight(parsed.right)
    }
  } catch {
    // 偏好读不出来就用默认值，不值得为此打断页面
  }
}

function persistLayout(): void {
  try {
    window.localStorage.setItem(
      LAYOUT_KEY,
      JSON.stringify({ left: leftWidth.value, right: rightWidth.value }),
    )
  } catch {
    // 隐私模式下 localStorage 会抛：忽略即可，列宽只是体验
  }
}

function clampLeft(v: number): number {
  return Math.min(420, Math.max(280, Math.round(v)))
}

function clampRight(v: number): number {
  return Math.min(460, Math.max(300, Math.round(v)))
}

/** 开始拖拽列宽；拖拽期间在 document 上听事件，指到窗口外也不会「粘住」 */
function startResize(side: 'left' | 'right', event: MouseEvent): void {
  event.preventDefault()
  const startX = event.clientX
  const startWidth = side === 'left' ? leftWidth.value : rightWidth.value

  function onMove(e: MouseEvent): void {
    const delta = side === 'left' ? e.clientX - startX : startX - e.clientX
    if (side === 'left') {
      leftWidth.value = clampLeft(startWidth + delta)
    } else {
      rightWidth.value = clampRight(startWidth + delta)
    }
  }

  function onUp(): void {
    document.removeEventListener('mousemove', onMove)
    document.removeEventListener('mouseup', onUp)
    persistLayout()
  }

  document.addEventListener('mousemove', onMove)
  document.addEventListener('mouseup', onUp)
}

function onMediaChange(e: MediaQueryListEvent): void {
  narrow.value = e.matches
  // 窗口变窄后右侧栏并到了页签里，若当前页签已不存在则退回 EtlPlan
  if (!e.matches && activeTab.value === 'gate') {
    activeTab.value = 'plan'
  }
}

let mql: MediaQueryList | null = null

onMounted(async () => {
  readLayout()
  mql = window.matchMedia(NARROW_QUERY)
  narrow.value = mql.matches
  mql.addEventListener('change', onMediaChange)

  const id = pipelineId.value
  if (id === null) {
    return
  }
  // 进 Studio 先清掉上一轮会话：否则会短暂显示别的 pipeline 的制品
  store.resetStudio()
  dismissedError.value = null
  selectedNodeId.value = null
  viewingVersionId.value = null
  submittedPreparationId.value = null

  await store.loadPipeline(id)
  if (store.pipeline === null) {
    return
  }

  // 源/目标下拉要的是数据源列表；直接从 URL 进 Studio 时它可能还没被任何页面拉过
  if (dataSourceStore.query.items.length === 0) {
    void dataSourceStore.query.load()
  }

  const sourceId = store.pipeline.source_data_source_id
  if (sourceId !== null) {
    void loadSourceMetadata(sourceId)
  }

  // 刷新恢复：thread_id 在 query 里 → 从后端 checkpoint 重放阶段与待澄清问题
  const thread = typeof route.query.thread === 'string' ? route.query.thread : null
  if (thread !== null) {
    const restored = await store.restoreFromSnapshot(thread)
    if (!restored) {
      // 恢复失败要说出来：否则界面会显示成「没进过 Studio」，与用户记忆不符
      ElMessage.warning('未能恢复该工作流线程（可能已结束或已过期），已回退到当前版本制品。')
    }
  }
  // 制品只读拉取：有 current_version_id 才有制品可看
  await syncArtifacts()
  await store.loadVersions(id)
})

onBeforeUnmount(() => {
  mql?.removeEventListener('change', onMediaChange)
})

// 出现 field_ref 型问题时才去拉字段候选：探查 schema 可能很大，不该每次进页都拉
watch(
  () => store.pendingQuestions,
  (questions) => {
    if (questions.some((q) => q.kind === 'field_ref') && fieldOptions.value.length === 0) {
      void loadFieldOptions()
    }
  },
)

/* ------------------------------------------------------------------ */
/* 动作                                                                */
/* ------------------------------------------------------------------ */

/** 把 thread_id 同步进 URL：刷新才能找回同一条工作流线程 */
function syncThreadToUrl(): void {
  const thread = store.threadId
  if (thread === null) {
    return
  }
  void router.replace({ query: { ...route.query, thread } })
}

/**
 * 快照进入「可冻结」后补拉只读制品。
 * 生成刚结束时后端可能还没给出制品标识（制品尚未落库），此时没有东西可拉，
 * 界面会如实显示「尚无 EtlPlan」而不是拿旧版本的制品顶替。
 */
async function syncArtifacts(): Promise<void> {
  const versionId = store.pipeline?.current_version_id ?? null
  if (store.phase !== 'ready_to_freeze' || versionId === null) {
    return
  }
  try {
    await store.loadArtifacts(versionId)
  } catch (e) {
    store.lastError = toDisplayMessage(e)
  }
}

async function onStart(payload: StartGenerationPayload): Promise<void> {
  const projectId = projectStore.currentProjectId
  if (projectId === null) {
    return
  }
  freezeError.value = null
  submitError.value = null
  dismissedError.value = null
  await store.startGeneration(
    projectId,
    payload.requirement,
    payload.source_data_source_id,
    payload.target_data_source_id,
  )
  syncThreadToUrl()
  await syncArtifacts()
}

async function onSubmitAnswers(): Promise<void> {
  await store.submitAnswers()
  syncThreadToUrl()
  await syncArtifacts()
}

async function onAbort(reason: string): Promise<void> {
  await store.abortGeneration(reason)
  syncThreadToUrl()
  ElMessage.info('已中止本次生成')
}

/** 门禁定位：按 locator 变体决定跳哪个页签，并让图上/代码上/表上同时亮起来 */
function onLocate(findingId: string): void {
  store.setActiveFinding(findingId)
  const locator = store.activeFinding?.locator ?? null
  if (locator === null) {
    return
  }
  if (locator.kind === 'hocon') {
    activeTab.value = 'hocon'
    return
  }
  if (locator.kind === 'dag_node') {
    activeTab.value = 'dag'
    selectedNodeId.value = locator.node_id
    return
  }
  if (locator.kind === 'source_table') {
    activeTab.value = 'fields'
    return
  }
  // plan 定位：字段映射相关的路径去字段页，其余回 EtlPlan 总览
  activeTab.value = locator.path.startsWith('field_mappings') ? 'fields' : 'plan'
}

function onActivateFinding(findingId: string | null): void {
  store.setActiveFinding(findingId)
  const locator = store.activeFinding?.locator ?? null
  if (locator !== null && locator.kind === 'dag_node') {
    selectedNodeId.value = locator.node_id
  }
}

function onSelectNode(nodeId: string | null): void {
  selectedNodeId.value = nodeId
}

function reloadVersions(): void {
  const id = pipelineId.value
  if (id !== null) {
    void store.loadVersions(id)
  }
}

async function onViewVersion(versionId: number | null): Promise<void> {
  viewingVersionId.value = versionId
  const target = versionId ?? store.pipeline?.current_version_id ?? null
  if (target === null) {
    return
  }
  try {
    await store.loadArtifacts(target)
  } catch (e) {
    store.lastError = toDisplayMessage(e)
  }
}

async function confirmFreeze(reason: string): Promise<void> {
  const versionId = workingVersionId.value
  if (versionId === null) {
    freezeError.value = '服务端尚未返回可冻结的制品标识，无法冻结。'
    return
  }
  freezeLoading.value = true
  freezeError.value = null
  try {
    await store.freezeVersion(versionId, reason)
    freezeDialogVisible.value = false
    ElMessage.success('版本已冻结，此后只能新增版本修正')
  } catch (e) {
    // 失败时保持弹窗打开：理由是用户刚填的，重开一遍等于让他重填
    freezeError.value = toDisplayMessage(e)
  } finally {
    freezeLoading.value = false
  }
}

/**
 * 提交审批。
 *
 * 这里直连 `api/preparation.ts` 而不是走 store：`stores/preparation.ts`
 * 没有 create 动作（准备单列表属于 fe-05 的域），而铁律禁止改基座。
 * 幂等性由 client 的请求拦截器保证（POST 自动带 Idempotency-Key，401 重放复用同一个 key），
 * 双击由 submitLoading + 轻确认拦。
 */
async function onSubmitApproval(): Promise<void> {
  const projectId = projectStore.currentProjectId
  const versionId = workingVersionId.value
  if (projectId === null || versionId === null) {
    return
  }
  try {
    await ElMessageBox.confirm(
      `将为版本 #${versionId} 创建准备单，进入四眼审批流程。审批通过后由执行操作员触发运行。`,
      '提交审批',
      { type: 'warning', confirmButtonText: '创建准备单', cancelButtonText: '再想想' },
    )
  } catch {
    return
  }
  submitLoading.value = true
  submitError.value = null
  try {
    const detail = await createPreparation(projectId, { pipeline_version_id: versionId })
    submittedPreparationId.value = detail.id
    ElMessage.success(`准备单 #${detail.id} 已创建，请在「审批」页查看`)
  } catch (e) {
    submitError.value = toDisplayMessage(e)
  } finally {
    submitLoading.value = false
  }
}

function backToList(): void {
  void router.push({ name: 'pipeline-list' })
}
</script>

<template>
  <div class="studio">
    <!-- 三态纪律：错误优先于空态，绝不把「加载失败」渲染成「没有这个 pipeline」 -->
    <div v-if="store.studioLoading && store.pipeline === null" class="studio__loading">
      <div class="elt-skeleton elt-skeleton--block studio__skeleton" />
    </div>

    <ErrorState
      v-else-if="store.pipeline === null"
      title="无法打开 Studio"
      :message="store.lastError ?? '未找到该 Pipeline，或它不属于当前项目。'"
      :retryable="true"
      @retry="pipelineId !== null && store.loadPipeline(pipelineId)"
    >
      <template #actions>
        <el-button size="small" :icon="ArrowLeft" @click="backToList">返回列表</el-button>
      </template>
    </ErrorState>

    <template v-else>
      <PageHeader :title="store.pipeline?.name ?? ''" :description="store.pipeline?.description">
        <template #actions>
          <StatusTag kind="PipelineStatus" :value="store.pipeline?.status ?? null" />
          <el-button size="small" :icon="ArrowLeft" @click="backToList">返回列表</el-button>
        </template>
        <template #notice>
          <StudioPhaseTag :phase="effectivePhase" :message="store.phaseMessage" />
        </template>
      </PageHeader>

      <!-- 工作流/写操作失败：留在原地，不自动消失（store.lastError 只能被下一次快照覆盖） -->
      <AsyncTaskPanel
        v-if="visibleError !== null"
        :loading="false"
        :error="visibleError"
      />
      <div v-if="visibleError !== null" class="studio__dismiss">
        <el-button link size="small" @click="dismissedError = store.lastError">收起该提示</el-button>
      </div>

      <div class="studio__grid">
        <aside class="studio__col studio__left" :style="{ width: `${leftWidth}px` }">
          <SectionCard title="输入">
            <RequirementPanel
              :options="store.dataSourceOptions"
              :pipeline="store.pipeline"
              :readonly="readonly"
              :loading="store.studioLoading"
              :error="null"
              :can-abort="store.threadId !== null && !readonly"
              @start="onStart"
              @abort="onAbort"
              @reload-options="dataSourceStore.query.reload()"
            />
          </SectionCard>

          <SectionCard
            title="澄清"
            :padded="true"
            :collapsible="true"
            :default-collapsed="!store.hasQuestions"
          >
            <!-- 失败提示由页面顶部统一展示：同一条错误在两处出现会让人以为发生了两次 -->
            <ClarificationPanel
              :questions="store.pendingQuestions"
              :answers="store.answers"
              :submitting="store.studioLoading"
              :error="null"
              :field-options="fieldOptions"
              :field-options-loading="fieldOptionsLoading"
              :field-options-error="fieldOptionsError"
              @answer="(key: string, value: string) => (store.answers[key] = value)"
              @submit="onSubmitAnswers"
            />
          </SectionCard>
        </aside>

        <div
          class="studio__resizer"
          role="separator"
          aria-label="调整左栏宽度"
          @mousedown="(e: MouseEvent) => startResize('left', e)"
        />

        <main class="studio__center">
          <div class="studio__tabs" role="tablist">
            <button
              v-for="t in tabItems"
              :key="t.key"
              type="button"
              class="studio__tab"
              :class="{ 'is-active': activeTab === t.key }"
              role="tab"
              :aria-selected="activeTab === t.key"
              @click="activeTab = t.key"
            >
              <span>{{ t.label }}</span>
              <span v-if="t.badge > 0" class="studio__tab-badge">{{ t.badge }}</span>
            </button>
            <span class="studio__tabs-spacer" />
            <el-button
              v-if="workingVersionId !== null"
              link
              size="small"
              :icon="Refresh"
              @click="onViewVersion(viewingVersionId)"
            >
              重新拉取制品
            </el-button>
          </div>

          <div class="studio__tab-body">
            <PlanTab v-if="activeTab === 'plan'" :plan="store.plan" />

            <HoconTab
              v-else-if="activeTab === 'hocon'"
              :content="store.hocon?.content ?? ''"
              :findings="store.gate?.findings ?? []"
              :active-finding-id="store.activeFindingId"
              :readonly="true"
              :sha256="store.hocon?.sha256 ?? ''"
              :parser-version="store.hocon?.parser_version ?? ''"
              :version-id="workingVersionId"
              @finding-click="onLocate"
              @request-diff="diffDialogVisible = true"
            />

            <DagTab
              v-else-if="activeTab === 'dag'"
              :dag="store.dag"
              :findings="store.gate?.findings ?? []"
              :selected-node-id="selectedNodeId"
              :located-node-id="locatedNodeId"
              @select-node="onSelectNode"
            />

            <FieldMappingTab
              v-else-if="activeTab === 'fields'"
              :mappings="store.plan?.plan.field_mappings ?? []"
              :inputs="store.plan?.plan.inputs ?? []"
              :active-path="activePlanPath"
              :source-table-locator="activeSourceTable"
            />

            <!-- 窄屏（<1280px）时右栏折到这里的第 5 个页签 -->
            <template v-else>
              <GatePanel
                :gate="store.gate"
                :active-finding-id="store.activeFindingId"
                @locate="onLocate"
                @activate="onActivateFinding"
              />
              <AutofixPanel
                class="studio__stack"
                :rounds="store.autofixRounds"
                :current-round="store.autofixRound"
                :max-rounds="AUTOFIX_MAX_ROUNDS"
                :exhausted="autofixExhausted"
                :can-run="workingVersionId !== null && !readonly"
                :loading="store.studioLoading"
                :error="visibleError"
                @run="(note: string) => store.runAutofix(note)"
                @focus-finding="onLocate"
              />
              <QualityContractPanel
                class="studio__stack"
                :rules="store.plan?.plan.quality_rules ?? []"
                :reject-rate-budget="store.plan?.plan.budget.max_reject_rate ?? null"
              />
              <BudgetPanel class="studio__stack" :budget="store.plan?.plan.budget ?? null" />
            </template>
          </div>
        </main>

        <div
          v-if="!narrow"
          class="studio__resizer"
          role="separator"
          aria-label="调整右栏宽度"
          @mousedown="(e: MouseEvent) => startResize('right', e)"
        />

        <aside v-if="!narrow" class="studio__col studio__right" :style="{ width: `${rightWidth}px` }">
          <GatePanel
            :gate="store.gate"
            :active-finding-id="store.activeFindingId"
            @locate="onLocate"
            @activate="onActivateFinding"
          />

          <AutofixPanel
            :rounds="store.autofixRounds"
            :current-round="store.autofixRound"
            :max-rounds="AUTOFIX_MAX_ROUNDS"
            :exhausted="autofixExhausted"
            :can-run="workingVersionId !== null && !readonly"
            :loading="store.studioLoading"
            :error="visibleError"
            @run="(note: string) => store.runAutofix(note)"
            @focus-finding="onLocate"
          />

          <QualityContractPanel
            :rules="store.plan?.plan.quality_rules ?? []"
            :reject-rate-budget="store.plan?.plan.budget.max_reject_rate ?? null"
          />

          <BudgetPanel :budget="store.plan?.plan.budget ?? null" />

          <SectionCard title="元数据快照" :padded="false" collapsible default-collapsed>
            <div v-if="profileLoading" class="studio__meta-loading elt-skeleton elt-skeleton--block" />
            <EmptyState
              v-else-if="profile === null"
              variant="no-data"
              title="无可用探查快照"
              description="源数据源尚未探查成功；元数据过期会让冻结被后端拒绝。"
            />
            <div v-else class="studio__meta">
              <div class="studio__meta-row">
                <span class="studio__meta-k">版本</span>
                <span class="studio__meta-v elt-mono">v{{ profile.version }}</span>
              </div>
              <div class="studio__meta-row">
                <span class="studio__meta-k">状态</span>
                <span class="studio__meta-v">
                  <StatusTag kind="ProbeStatus" :value="profile.status" size="small" />
                </span>
              </div>
              <div class="studio__meta-row">
                <span class="studio__meta-k">过期时间</span>
                <span class="studio__meta-v elt-mono">{{ profile.expires_at ?? '—' }}</span>
              </div>
              <div v-if="profile.error_message !== null" class="studio__meta-row">
                <span class="studio__meta-k">失败原因</span>
                <span class="studio__meta-v"><SensitiveText :value="profile.error_message" multiline /></span>
              </div>
              <div v-if="profileExpired" class="studio__meta-warn">快照可能已过期，冻结前请先重新探查。</div>
            </div>
          </SectionCard>

          <!-- 已冻结制品：清单以冻结接口返回值为准，前端只展示不再计算 -->
          <SectionCard v-if="store.frozen !== null" title="已冻结制品">
            <div class="studio__meta">
              <div class="studio__meta-row">
                <span class="studio__meta-k">Plan</span>
                <span class="studio__meta-v elt-mono">{{ store.frozen.plan.schema_version }}</span>
              </div>
              <div class="studio__meta-row">
                <span class="studio__meta-k">HOCON</span>
                <span class="studio__meta-v">
                  <ArtifactHash :hash="store.frozen.hocon.sha256" display="truncate" copyable />
                </span>
              </div>
              <div class="studio__meta-row">
                <span class="studio__meta-k">质量契约</span>
                <span class="studio__meta-v">
                  {{ store.frozen.quality_contract.rule_count }} 条规则，拒绝率预算
                  {{ formatRatioPercent(store.frozen.quality_contract.reject_rate_budget) }}
                </span>
              </div>
              <div class="studio__meta-row">
                <span class="studio__meta-k">元数据快照</span>
                <span class="studio__meta-v elt-mono">v{{ store.frozen.metadata_profile.version }}</span>
              </div>
            </div>
          </SectionCard>
        </aside>
      </div>

      <footer class="studio__bottom">
        <VersionBar
          :phase="effectivePhase"
          :working-version-id="workingVersionId"
          :frozen-version-id="store.frozenVersionId"
          :manifest="store.frozen"
          :blocking-count="store.blockingFindings.length"
          :can-freeze="store.canFreeze"
          :versions="store.versions"
          :versions-loading="store.versionsLoading"
          :viewing-version-id="viewingVersionId"
          :freeze-loading="freezeLoading"
          :submit-loading="submitLoading"
          :submit-error="submitError"
          @freeze="freezeDialogVisible = true"
          @submit="onSubmitApproval"
          @view-version="onViewVersion"
          @diff="diffDialogVisible = true"
          @reload-versions="reloadVersions"
        />
      </footer>
    </template>

    <FreezeConfirmDialog
      v-model="freezeDialogVisible"
      :preview="freezePreview"
      :profile-expired="profileExpired"
      :loading="freezeLoading"
      :error="freezeError"
      @confirm="confirmFreeze"
    />

    <VersionDiffDialog
      v-model="diffDialogVisible"
      :versions="store.versions"
      :current-version-id="workingVersionId"
    />
  </div>
</template>

<style scoped>
.studio {
  display: flex;
  flex-direction: column;
  gap: 10px;
  height: 100%;
  min-height: 0;
}

.studio__loading {
  flex: 1;
}

.studio__skeleton {
  height: 100%;
}

.studio__dismiss {
  margin-top: -6px;
  text-align: right;
}

.studio__grid {
  display: flex;
  flex: 1;
  min-height: 0;
}

.studio__col {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding-right: 6px;
  overflow-y: auto;
}

.studio__left {
  flex: none;
}

.studio__right {
  flex: none;
  padding-right: 0;
  padding-left: 6px;
}

.studio__resizer {
  flex: none;
  width: 6px;
  cursor: col-resize;
  background: linear-gradient(
    to right,
    transparent 2px,
    var(--elt-border-subtle) 2px,
    var(--elt-border-subtle) 3px,
    transparent 3px
  );
}

.studio__resizer:hover {
  background: linear-gradient(
    to right,
    transparent 2px,
    var(--elt-accent) 2px,
    var(--elt-accent) 3px,
    transparent 3px
  );
}

.studio__center {
  display: flex;
  flex: 1;
  flex-direction: column;
  min-width: 0;
}

.studio__tabs {
  display: flex;
  flex: none;
  gap: 2px;
  align-items: center;
  padding-bottom: 6px;
  border-bottom: 1px solid var(--elt-border-subtle);
}

.studio__tab {
  display: inline-flex;
  gap: 5px;
  align-items: center;
  padding: 4px 10px;
  font-size: 12px;
  color: var(--elt-text-secondary);
  cursor: pointer;
  background: none;
  border: none;
  border-radius: var(--elt-radius-control);
}

.studio__tab:hover {
  color: var(--elt-text-primary);
  background-color: var(--elt-bg-elevated);
}

.studio__tab.is-active {
  color: var(--elt-text-primary);
  background-color: var(--elt-bg-elevated);
  box-shadow: inset 0 -2px 0 var(--elt-accent);
}

.studio__tab-badge {
  min-width: 15px;
  padding: 0 4px;
  font-size: 10px;
  line-height: 15px;
  color: var(--elt-text-primary);
  text-align: center;
  background-color: var(--elt-badge-danger);
  border-radius: var(--elt-radius-pill);
}

.studio__tabs-spacer {
  flex: 1;
}

.studio__tab-body {
  flex: 1;
  min-height: 0;
  padding-top: 10px;
  overflow: auto;
}

.studio__stack {
  margin-top: 10px;
}

.studio__bottom {
  flex: none;
  height: 64px;
  padding: 0 4px;
  background-color: var(--elt-bg-surface);
  border: 1px solid var(--elt-border-subtle);
  border-radius: var(--elt-radius-card);
}

.studio__meta {
  padding: 8px 10px;
}

.studio__meta-row {
  display: flex;
  gap: 8px;
  align-items: center;
  padding: 2px 0;
  font-size: 12px;
}

.studio__meta-k {
  flex: none;
  width: 74px;
  color: var(--elt-text-muted);
}

.studio__meta-v {
  flex: 1;
  min-width: 0;
  color: var(--elt-text-primary);
  overflow-wrap: anywhere;
}

.studio__meta-warn {
  margin-top: 4px;
  font-size: 11px;
  color: var(--elt-warn);
}

.studio__meta-loading {
  height: 60px;
  margin: 8px 10px;
}
</style>
