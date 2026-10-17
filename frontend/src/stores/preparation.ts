/**
 * 准备单与审批状态（fe-05）。
 *
 * 审批控制台的核心约束是「**界面不得比后端更乐观**」：
 * `my_slot`、`can_commit`、`stale` 三个字段全部以后端下发为准，
 * store 不做任何推导。前端一旦自己算出「我能审」，就会出现
 * 按钮可点但请求必被 SELF_APPROVAL_FORBIDDEN 拒绝的割裂体验。
 */

import { computed, ref, watch } from 'vue'
import type { ComputedRef } from 'vue'
import { defineStore } from 'pinia'
import * as preparationApi from '@/api/preparation'
import { useListQuery } from '@/composables/useListQuery'
import { toDisplayMessage } from '@/utils/errors'
import { useProjectStore } from './project'
import type {
  PreparationDetail,
  PreparationFilter,
  PreparationSummary,
  PreparationTimelineItem,
  RiskReason,
} from '@/types'

function makeDefaultFilter(): PreparationFilter {
  return {
    // 默认落在「待我审批」：审批人打开控制台最想看到的就是待办，
    // 而不是全项目的历史单据。
    scope: 'todo',
    page: 1,
    page_size: 20,
  }
}

export const usePreparationStore = defineStore('preparation', () => {
  const projectStore = useProjectStore()

  const query = useListQuery<PreparationSummary, PreparationFilter>(
    makeDefaultFilter,
    (filter) => {
      const projectId = projectStore.currentProjectId
      if (projectId === null) {
        return Promise.resolve({ items: [], meta: { page: 1, page_size: filter.page_size, total: 0 } })
      }
      return preparationApi.fetchPreparations(projectId, filter)
    },
    { immediate: false, scope: () => projectStore.currentProjectId },
  )

  const detail = ref<PreparationDetail | null>(null)
  const detailLoading = ref(false)
  const detailError = ref<string | null>(null)
  const timeline = ref<PreparationTimelineItem[]>([])
  const riskReasons = ref<RiskReason[]>([])

  /**
   * 「待我审批」计数：侧栏徽标用。
   *
   * ⚠️ 不能直接取 `query.meta.total`。用户在审批中心把标签页切到「全部」时，
   * 那个 total 是**全项目**的单据数——徽标会显示成几十上百，而用户实际待办可能只有 2 件。
   * 徽标与它指向的列表对不上，用户就会不再信任徽标。
   *
   * 因此：只有当列表当前确实处于 `todo` 作用域时才采信它的 total；
   * 其余时候沿用上一次的 todo 计数，并由 `refreshTodoCount()` 单独补齐。
   */
  const myTodoCount = ref(0)
  const todoCountLoading = ref(false)

  watch(
    () => query.meta.value.total,
    (total) => {
      if (query.filter.value.scope === 'todo') {
        myTodoCount.value = total
      }
    },
  )

  /**
   * 单独拉一次 todo 计数（`page_size=1`，只取 meta.total）。
   *
   * 失败时**静默保持旧值**：徽标是装饰性的，为它弹一条错误、
   * 甚至在侧边栏挂一个红点，会让用户以为系统坏了。
   */
  async function refreshTodoCount(): Promise<void> {
    const projectId = projectStore.currentProjectId
    if (projectId === null) {
      myTodoCount.value = 0
      return
    }
    if (todoCountLoading.value) {
      return
    }
    todoCountLoading.value = true
    try {
      const page = await preparationApi.fetchPreparations(projectId, {
        ...makeDefaultFilter(),
        page: 1,
        page_size: 1,
      })
      myTodoCount.value = page.meta.total
    } catch {
      // 见上方说明：静默失败是刻意的
    } finally {
      todoCountLoading.value = false
    }
  }

  /** 当前详情里是否有决定因指纹变化而失效 */
  const hasStaleDecision: ComputedRef<boolean> = computed(
    () => detail.value?.decisions.some((d) => d.stale) === true,
  )

  async function loadDetail(id: number): Promise<void> {
    detailLoading.value = true
    detailError.value = null
    try {
      const [d, t, r] = await Promise.all([
        preparationApi.fetchPreparation(id),
        preparationApi.fetchPreparationTimeline(id),
        preparationApi.fetchRiskReasons(id),
      ])
      detail.value = d
      timeline.value = t
      riskReasons.value = r.map((item) => ({
        code: item.rule_code,
        level: d.risk_level,
        title: item.rule_name,
        detail: item.detail,
      }))
    } catch (e) {
      detail.value = null
      timeline.value = []
      riskReasons.value = []
      detailError.value = toDisplayMessage(e)
    } finally {
      detailLoading.value = false
    }
  }

  function clearDetail(): void {
    detail.value = null
    timeline.value = []
    riskReasons.value = []
    detailError.value = null
  }

  /**
   * 提交审批决定后重新拉详情。
   * 不本地改 `decided_slots`：四眼规则下「两槽是否齐备」的判定在服务端，
   * 本地拼出来的状态可能与服务端不一致（例如另一槽刚被退回）。
   */
  async function refreshAfterDecision(id: number): Promise<void> {
    await loadDetail(id)
    await query.reload()
  }

  /** Commit 成功后准备单进入已执行态，列表与详情都需要刷新 */
  async function refreshAfterCommit(id: number): Promise<void> {
    await loadDetail(id)
    await query.reload()
  }

  return {
    query,
    detail,
    detailLoading,
    detailError,
    timeline,
    riskReasons,
    myTodoCount,
    todoCountLoading,
    hasStaleDecision,
    loadDetail,
    clearDetail,
    refreshAfterDecision,
    refreshAfterCommit,
    refreshTodoCount,
  }
})
