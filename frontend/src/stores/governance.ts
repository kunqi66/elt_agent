/**
 * 安全治理状态：策略、预算、Benchmark、证据账本（fe-07）。
 *
 * 账本部分是本 store 最需要克制的：**只有读与累加，没有写**。
 * 前端不重算 prev_hash/event_hash，不做「本地补一条」的乐观插入，
 * 也不缓存验链结论——验链结果必须每次现取，否则会拿一份过期的「通过」去背书。
 */

import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import * as governanceApi from '@/api/governance'
import { useListQuery } from '@/composables/useListQuery'
import { toDisplayMessage } from '@/utils/errors'
import { useProjectStore } from './project'
import type {
  BenchmarkCaseResult,
  BenchmarkRunRead,
  BenchmarkSuiteRead,
  BudgetDefaults,
  BudgetDefaultsRead,
  EvidenceEventRead,
  EvidenceFilter,
  LedgerVerifyResult,
  PolicyDiffEntry,
  PolicyRuleFilter,
  PolicyRuleRead,
  PolicyVersionRead,
} from '@/types'

/** 账本内最多累积的事件数；账本是无限增长的，前端不能无限存 */
const EVIDENCE_BUFFER_LIMIT = 2000

function makeDefaultFilter(): PolicyRuleFilter {
  return { page: 1, page_size: 50 }
}

/** 账本查询条件里 project_id 由 store 补，调用方不用传 */
type EvidenceQuery = Omit<EvidenceFilter, 'project_id'>

function makeEvidenceQuery(): EvidenceQuery {
  return { limit: 50 }
}

function makeBenchmarkFilter(): { page: number; page_size: number } {
  return { page: 1, page_size: 20 }
}

export const useGovernanceStore = defineStore('governance', () => {
  const projectStore = useProjectStore()

  /* ---------------------------------------------------------------- */
  /* 策略规则                                                          */
  /* ---------------------------------------------------------------- */

  const policyRules = useListQuery<PolicyRuleRead, PolicyRuleFilter>(
    makeDefaultFilter,
    (filter) => {
      const projectId = projectStore.currentProjectId
      if (projectId === null) {
        return Promise.resolve({ items: [], meta: { page: 1, page_size: filter.page_size, total: 0 } })
      }
      return governanceApi.fetchPolicyRules(projectId, filter)
    },
    { immediate: false, scope: () => projectStore.currentProjectId },
  )

  const policyVersions = ref<PolicyVersionRead[]>([])
  const policyVersionsLoading = ref(false)
  const policyDiff = ref<PolicyDiffEntry[]>([])
  const policyDiffSummary = ref<{ added: number; removed: number; changed: number } | null>(null)

  async function loadPolicyVersions(): Promise<void> {
    const projectId = projectStore.currentProjectId
    if (projectId === null) {
      return
    }
    policyVersionsLoading.value = true
    try {
      const page = await governanceApi.fetchPolicyVersions(projectId)
      policyVersions.value = page.items
    } catch {
      policyVersions.value = []
    } finally {
      policyVersionsLoading.value = false
    }
  }

  async function loadPolicyDiff(versionId: number): Promise<void> {
    const projectId = projectStore.currentProjectId
    if (projectId === null) {
      return
    }
    try {
      const result = await governanceApi.fetchPolicyDiff(projectId, versionId)
      policyDiff.value = result.entries
      policyDiffSummary.value = result.summary
    } catch {
      policyDiff.value = []
      policyDiffSummary.value = null
    }
  }

  /* ---------------------------------------------------------------- */
  /* 运行预算                                                          */
  /* ---------------------------------------------------------------- */

  const budget = ref<BudgetDefaultsRead | null>(null)
  const budgetLoading = ref(false)
  const budgetError = ref<string | null>(null)

  /**
   * 覆盖值是否越界。
   * 只用于**提前告知**用户会被拒（体验），真正的拒绝在后端——
   * 因此这里即使判 true 也照常提交，以服务端结论为准。
   */
  const budgetOverLimit = computed<string[]>(() => {
    const current = budget.value
    if (current === null || current.override === null) {
      return []
    }
    const over: string[] = []
    for (const key of Object.keys(current.hard_limits) as Array<keyof BudgetDefaults>) {
      const value = current.override[key]
      if (value !== undefined && value > current.hard_limits[key]) {
        over.push(key)
      }
    }
    return over
  })

  /**
   * 加载预算三方对照（默认值 / 硬上限 / 项目覆盖）。
   * 保存成功后也走这条路径重取——不做乐观更新：越界与否由后端判定，
   * 本地先改会让界面显示成「已生效」。
   */
  async function loadBudget(): Promise<void> {
    const projectId = projectStore.currentProjectId
    if (projectId === null) {
      return
    }
    budgetLoading.value = true
    budgetError.value = null
    try {
      budget.value = await governanceApi.fetchBudgetDefaults(projectId)
    } catch (e) {
      budget.value = null
      budgetError.value = toDisplayMessage(e)
    } finally {
      budgetLoading.value = false
    }
  }

  /* ---------------------------------------------------------------- */
  /* Benchmark                                                         */
  /* ---------------------------------------------------------------- */

  const suites = ref<BenchmarkSuiteRead[]>([])
  const suitesLoading = ref(false)
  const selectedSuiteId = ref<number | null>(null)

  const benchmarkRuns = useListQuery<BenchmarkRunRead, { page: number; page_size: number }>(
    makeBenchmarkFilter,
    (filter) => {
      const projectId = projectStore.currentProjectId
      if (projectId === null) {
        return Promise.resolve({ items: [], meta: { page: 1, page_size: filter.page_size, total: 0 } })
      }
      return governanceApi.fetchBenchmarkRuns(projectId, undefined, filter.page, filter.page_size)
    },
    { immediate: false, scope: () => projectStore.currentProjectId },
  )

  const activeRun = ref<BenchmarkRunRead | null>(null)
  const caseResults = ref<BenchmarkCaseResult[]>([])
  const caseResultsLoading = ref(false)

  async function loadSuites(): Promise<void> {
    const projectId = projectStore.currentProjectId
    if (projectId === null) {
      return
    }
    suitesLoading.value = true
    try {
      suites.value = await governanceApi.fetchBenchmarkSuites(projectId)
      if (selectedSuiteId.value === null && suites.value.length > 0) {
        selectedSuiteId.value = suites.value[0]?.id ?? null
      }
    } catch {
      suites.value = []
    } finally {
      suitesLoading.value = false
    }
  }

  async function loadBenchmarkRun(runId: number): Promise<void> {
    try {
      activeRun.value = await governanceApi.fetchBenchmarkRun(runId)
    } catch {
      activeRun.value = null
    }
  }

  async function loadCaseResults(runId: number): Promise<void> {
    caseResultsLoading.value = true
    try {
      caseResults.value = await governanceApi.fetchBenchmarkCaseResults(runId)
    } catch {
      caseResults.value = []
    } finally {
      caseResultsLoading.value = false
    }
  }

  /* ---------------------------------------------------------------- */
  /* 证据账本                                                          */
  /* ---------------------------------------------------------------- */

  const events = ref<EvidenceEventRead[]>([])
  const evidenceQuery = ref<EvidenceQuery>(makeEvidenceQuery())
  const nextCursor = ref<string | null>(null)
  const evidenceLoading = ref(false)
  const evidenceError = ref<string | null>(null)
  /** 是否还有下一页；null 游标 = 到底了 */
  const hasMoreEvidence = computed<boolean>(() => nextCursor.value !== null)

  const verifyResult = ref<LedgerVerifyResult | null>(null)
  const verifying = ref(false)

  const brokenEventSeqs = computed<number[]>(() => {
    const result = verifyResult.value
    // 只在链断时高亮：验链通过时全部事件都是「正常」，没有高亮可言
    if (result === null || result.ok || result.broken_at_seq === null) {
      return []
    }
    return [result.broken_at_seq]
  })

  /**
   * 重新查询账本（条件变化或点「刷新」时调用）。
   * 必须清空游标：条件变了，旧的 cursor 指向的位置已经没有意义。
   */
  async function resetEvidence(query?: Partial<EvidenceQuery>): Promise<void> {
    if (query !== undefined) {
      evidenceQuery.value = { ...evidenceQuery.value, ...query }
    }
    events.value = []
    nextCursor.value = null
    await loadMoreEvidence()
  }

  async function loadMoreEvidence(): Promise<void> {
    const projectId = projectStore.currentProjectId
    const alreadyExhausted = events.value.length > 0 && !hasMoreEvidence.value
    if (projectId === null || evidenceLoading.value || alreadyExhausted) {
      return
    }
    evidenceLoading.value = true
    evidenceError.value = null
    try {
      const page = await governanceApi.fetchEvidence({
        ...evidenceQuery.value,
        project_id: projectId,
        cursor: nextCursor.value ?? undefined,
      })
      events.value = [...events.value, ...page.items]
      if (events.value.length > EVIDENCE_BUFFER_LIMIT) {
        // 账本只增不减，前端缓冲必须有上限；被裁掉的历史靠「重新查询」找回
        events.value = events.value.slice(events.value.length - EVIDENCE_BUFFER_LIMIT)
      }
      nextCursor.value = page.next_cursor
    } catch (e) {
      evidenceError.value = toDisplayMessage(e)
    } finally {
      evidenceLoading.value = false
    }
  }

  /**
   * 触发服务端验链。
   * 每次都现取，不复用上一次结果——一份过期的「验链通过」比没有结论更危险。
   */
  async function verify(): Promise<void> {
    const projectId = projectStore.currentProjectId
    if (projectId === null) {
      return
    }
    verifying.value = true
    try {
      verifyResult.value = await governanceApi.verifyLedger(projectId)
    } finally {
      verifying.value = false
    }
  }

  function setBudgetOverride(next: Partial<BudgetDefaults> | null): void {
    if (budget.value === null) {
      return
    }
    budget.value = { ...budget.value, override: next }
  }

  function reset(): void {
    events.value = []
    evidenceQuery.value = makeEvidenceQuery()
    nextCursor.value = null
    verifyResult.value = null
    policyVersions.value = []
    policyDiff.value = []
    policyDiffSummary.value = null
    budget.value = null
    suites.value = []
    activeRun.value = null
    caseResults.value = []
  }

  return {
    policyRules,
    policyVersions,
    policyVersionsLoading,
    policyDiff,
    policyDiffSummary,
    budget,
    budgetLoading,
    budgetError,
    budgetOverLimit,
    suites,
    suitesLoading,
    selectedSuiteId,
    benchmarkRuns,
    activeRun,
    caseResults,
    caseResultsLoading,
    events,
    evidenceQuery,
    nextCursor,
    evidenceLoading,
    evidenceError,
    hasMoreEvidence,
    verifyResult,
    verifying,
    brokenEventSeqs,
    loadPolicyVersions,
    loadPolicyDiff,
    loadBudget,
    setBudgetOverride,
    loadSuites,
    loadBenchmarkRun,
    loadCaseResults,
    resetEvidence,
    loadMoreEvidence,
    verify,
    reset,
  }
})
