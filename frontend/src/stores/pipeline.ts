/**
 * Pipeline 与 Studio 状态（fe-04）。
 *
 * **Studio 的全部状态住在这里**，组件不留本地副本。
 * 理由：生成过程跨多个请求（发起 → 澄清 → 生成 → 门禁 → 修复），
 * 用户还可能在澄清中途刷新页面。状态若散在组件里，刷新即丢；
 * 集中一处才能靠 `restoreFromSnapshot()` 从后端 checkpoint 恢复。
 *
 * 本 store 不做任何领域计算：不解析 HOCON、不推 DAG 布局、不算指纹、
 * 不判断门禁是否通过。以上全部来自后端只读制品。
 */

import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import * as pipelineApi from '@/api/pipeline'
import { useListQuery } from '@/composables/useListQuery'
import { toDisplayMessage } from '@/utils/errors'
import { useDataSourceStore } from './datasource'
import { useProjectStore } from './project'
import type {
  AutofixRound,
  ClarificationQuestion,
  DataSourceOption,
  EtlPlan,
  FreezeArtifactManifest,
  GateFinding,
  GateResult,
  HoconArtifact,
  Pipeline,
  PipelineFilter,
  PipelineVersionSummary,
  StudioPhase,
  WorkflowSnapshot,
} from '@/types'

function makeDefaultFilter(): PipelineFilter {
  return { page: 1, page_size: 20 }
}

export const usePipelineStore = defineStore('pipeline', () => {
  const projectStore = useProjectStore()
  const dataSourceStore = useDataSourceStore()

  /* ---------------------------------------------------------------- */
  /* 列表                                                              */
  /* ---------------------------------------------------------------- */

  const query = useListQuery<Pipeline, PipelineFilter>(
    makeDefaultFilter,
    (filter) => {
      const projectId = projectStore.currentProjectId
      if (projectId === null) {
        return Promise.resolve({ items: [], meta: { page: 1, page_size: filter.page_size, total: 0 } })
      }
      return pipelineApi.fetchPipelines(projectId, filter)
    },
    { immediate: false, scope: () => projectStore.currentProjectId },
  )

  /* ---------------------------------------------------------------- */
  /* Studio                                                            */
  /* ---------------------------------------------------------------- */

  const pipelineId = ref<number | null>(null)
  const pipeline = ref<Pipeline | null>(null)
  const threadId = ref<string | null>(null)
  const phase = ref<StudioPhase>('idle')
  const phaseMessage = ref<string | null>(null)
  const lastError = ref<string | null>(null)

  const pendingQuestions = ref<ClarificationQuestion[]>([])
  /** 澄清答案：key → 用户作答；提交前为空表示「还没答完」 */
  const answers = ref<Record<string, string>>({})

  const plan = ref<EtlPlan | null>(null)
  const hocon = ref<HoconArtifact | null>(null)
  const gate = ref<GateResult | null>(null)
  const autofixRounds = ref<AutofixRound[]>([])
  const autofixRound = ref(0)

  /** 已冻结版本的制品清单；非空表示当前会话已产出不可变版本 */
  const frozen = ref<FreezeArtifactManifest | null>(null)
  const frozenVersionId = ref<number | null>(null)

  const versions = ref<PipelineVersionSummary[]>([])
  const versionsLoading = ref(false)

  const studioLoading = ref(false)

  /**
   * 跨面板联动的高亮锚点。
   * 点门禁 finding → HOCON 行高亮 + DAG 节点高亮 + 字段映射行高亮，
   * 三处都读这一个 id，不需要互相传事件。
   */
  const activeFindingId = ref<string | null>(null)

  const dag = computed(() => plan.value?.plan.dag ?? null)
  /** 阻断级 finding 数：冻结按钮的可用性判据（真正的判定仍在服务端） */
  const blockingFindings = computed<GateFinding[]>(
    () => gate.value?.findings.filter((f) => f.severity === 'block') ?? [],
  )
  const canFreeze = computed<boolean>(
    () => phase.value === 'ready_to_freeze' && blockingFindings.value.length === 0,
  )
  const hasQuestions = computed<boolean>(() => pendingQuestions.value.length > 0)
  const unansweredRequired = computed<ClarificationQuestion[]>(() =>
    pendingQuestions.value.filter((q) => {
      if (!q.required) {
        return false
      }
      const value = answers.value[q.key]
      return value === undefined || value.trim() === ''
    }),
  )

  /**
   * 源/目标下拉选项。
   * 只取 id/name/type/env —— **不含 endpoint 与 secret_ref**（fe-04 §8）：
   * 生成请求里带连接信息没有任何用处，却会把凭据引用扩散到更多地方。
   *
   * 合法组合（哪些类型能当 sink）由后端判定，前端不预设白名单；
   * 选错了会在门禁阶段得到一条 source_table/plan 定位的 finding。
   */
  const dataSourceOptions = computed<DataSourceOption[]>(() =>
    // 注意：读的是 pinia store 上的 query，Pinia 的 reactive 已把内层 ref 解包，
    // 所以这里是 `items` 而不是 `items.value`（在 store 内部用自己的局部变量时才需要 .value）
    dataSourceStore.query.items
      .filter((d) => d.status === 'active')
      .map((d) => ({ id: d.id, name: d.name, type: d.type, env: d.env })),
  )

  const activeFinding = computed<GateFinding | null>(
    () => gate.value?.findings.find((f) => f.id === activeFindingId.value) ?? null,
  )

  function setActiveFinding(id: string | null): void {
    activeFindingId.value = id
  }

  /* ---------------------------------------------------------------- */
  /* Studio 动作                                                       */
  /* ---------------------------------------------------------------- */

  async function loadPipeline(id: number): Promise<void> {
    studioLoading.value = true
    try {
      pipeline.value = await pipelineApi.fetchPipeline(id)
      pipelineId.value = id
    } catch (e) {
      pipeline.value = null
      lastError.value = toDisplayMessage(e)
    } finally {
      studioLoading.value = false
    }
  }

  function applySnapshot(snapshot: WorkflowSnapshot): void {
    // thread_id 是后续所有恢复请求的钥匙，快照回来就立刻记下
    threadId.value = snapshot.thread_id
    phase.value = snapshot.phase
    phaseMessage.value = snapshot.phase_message
    pendingQuestions.value = snapshot.pending_questions
    autofixRound.value = snapshot.autofix_round
    lastError.value = snapshot.last_error ?? null
    // 快照回来时清空上一轮的作答：问题集合可能已经变了，
    // 留着旧答案会出现「答了但没被问到」的错位
    answers.value = {}
  }

  /** 发起生成 */
  async function startGeneration(
    projectId: number,
    requirement: string,
    sourceId: number,
    targetId: number,
  ): Promise<void> {
    studioLoading.value = true
    lastError.value = null
    try {
      const snapshot = await pipelineApi.startGeneration(projectId, {
        requirement,
        source_data_source_id: sourceId,
        target_data_source_id: targetId,
      })
      applySnapshot(snapshot)
    } catch (e) {
      lastError.value = toDisplayMessage(e)
      phase.value = 'error'
    } finally {
      studioLoading.value = false
    }
  }

  /** 提交澄清答案并继续图执行 */
  async function submitAnswers(): Promise<void> {
    if (threadId.value === null) {
      return
    }
    studioLoading.value = true
    lastError.value = null
    try {
      const snapshot = await pipelineApi.answerClarification(threadId.value, answers.value)
      applySnapshot(snapshot)
    } catch (e) {
      lastError.value = toDisplayMessage(e)
    } finally {
      studioLoading.value = false
    }
  }

  /**
   * 断线/刷新后恢复：从后端 checkpoint 拉当前快照。
   * 这是「刷新不丢进度」的唯一实现方式，也是 fe-01 §1 禁止用进程内字典
   * 假装持久化的原因。
   */
  async function restoreFromSnapshot(id: string): Promise<boolean> {
    try {
      const snapshot = await pipelineApi.fetchWorkflowSnapshot(id)
      threadId.value = id
      applySnapshot(snapshot)
      return true
    } catch {
      return false
    }
  }

  async function abortGeneration(reason: string): Promise<void> {
    if (threadId.value === null) {
      return
    }
    try {
      applySnapshot(await pipelineApi.abortGeneration(threadId.value, reason))
    } catch (e) {
      lastError.value = toDisplayMessage(e)
    }
  }

  /* ---------------------------------------------------------------- */
  /* 制品（只读）                                                       */
  /* ---------------------------------------------------------------- */

  async function loadArtifacts(versionId: number): Promise<void> {
    const [p, h, g] = await Promise.all([
      pipelineApi.fetchPlan(versionId),
      pipelineApi.fetchHocon(versionId),
      pipelineApi.fetchGateResult(versionId),
    ])
    plan.value = p
    hocon.value = h
    gate.value = g
    autofixRounds.value = g.autofix_rounds
    autofixRound.value = g.autofix_rounds.length
  }

  async function loadVersions(id: number): Promise<void> {
    versionsLoading.value = true
    try {
      const page = await pipelineApi.fetchVersions(id)
      versions.value = page.items
    } catch {
      versions.value = []
    } finally {
      versionsLoading.value = false
    }
  }

  async function runAutofix(note: string): Promise<void> {
    if (frozenVersionId.value === null && pipeline.value?.current_version_id == null) {
      return
    }
    const versionId = frozenVersionId.value ?? pipeline.value?.current_version_id
    if (versionId === null || versionId === undefined) {
      return
    }
    studioLoading.value = true
    try {
      const round = await pipelineApi.runAutofix(versionId, note)
      autofixRounds.value = [...autofixRounds.value, round]
      autofixRound.value = round.round
      // 修复会改动 HOCON 与 Plan，必须重取制品而不是本地拼接 diff
      await loadArtifacts(versionId)
    } catch (e) {
      lastError.value = toDisplayMessage(e)
    } finally {
      studioLoading.value = false
    }
  }

  /**
   * 冻结版本。成功后进入不可变状态——此后任何修正都只能产生新版本，
   * 因此这里要把 frozenVersionId 记下来，供后续页面展示。
   */
  async function freezeVersion(versionId: number, note: string): Promise<FreezeArtifactManifest> {
    const manifest = await pipelineApi.freezeVersion(versionId, note)
    frozen.value = manifest
    frozenVersionId.value = versionId
    phase.value = 'frozen'
    return manifest
  }

  function resetStudio(): void {
    pipelineId.value = null
    pipeline.value = null
    threadId.value = null
    phase.value = 'idle'
    phaseMessage.value = null
    lastError.value = null
    pendingQuestions.value = []
    answers.value = {}
    plan.value = null
    hocon.value = null
    gate.value = null
    autofixRounds.value = []
    autofixRound.value = 0
    frozen.value = null
    frozenVersionId.value = null
    activeFindingId.value = null
  }

  return {
    query,
    pipelineId,
    pipeline,
    threadId,
    phase,
    phaseMessage,
    lastError,
    pendingQuestions,
    answers,
    plan,
    hocon,
    gate,
    autofixRounds,
    autofixRound,
    frozen,
    frozenVersionId,
    versions,
    versionsLoading,
    studioLoading,
    activeFindingId,
    activeFinding,
    dag,
    blockingFindings,
    canFreeze,
    hasQuestions,
    unansweredRequired,
    dataSourceOptions,
    setActiveFinding,
    loadPipeline,
    startGeneration,
    submitAnswers,
    restoreFromSnapshot,
    abortGeneration,
    loadArtifacts,
    loadVersions,
    runAutofix,
    freezeVersion,
    resetStudio,
  }
})
