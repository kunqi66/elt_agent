/**
 * 数据源与元数据探查状态（fe-03）。
 *
 * 凭据红线：这个 store **只持有 secret_ref（路径 + 版本 + 键名）**。
 * 表单里输入的凭据值走 `useAsyncTask` 的局部变量直接提交，
 * 绝不写进 store、localStorage 或任何日志——store 是可被 devtools 检视的。
 */

import { computed, ref } from 'vue'
import type { ComputedRef } from 'vue'
import { defineStore } from 'pinia'
import * as datasourceApi from '@/api/datasource'
import { useListQuery } from '@/composables/useListQuery'
import { toDisplayMessage } from '@/utils/errors'
import { useProjectStore } from './project'
import type {
  ConnectivityResult,
  DataSource,
  DataSourceFilter,
  MetadataProfile,
  MetadataProfileBrief,
  ProbeStatus,
} from '@/types'

function makeDefaultFilter(): DataSourceFilter {
  return { page: 1, page_size: 20 }
}

export const useDataSourceStore = defineStore('datasource', () => {
  const projectStore = useProjectStore()

  const query = useListQuery<DataSource, DataSourceFilter>(
    makeDefaultFilter,
    (filter) => {
      const projectId = projectStore.currentProjectId
      if (projectId === null) {
        // 没有项目上下文时不该发请求：那会打到上一个项目的 id 或直接 400
        return Promise.resolve({ items: [], meta: { page: 1, page_size: filter.page_size, total: 0 } })
      }
      return datasourceApi.fetchDataSources(projectId, filter)
    },
    { immediate: false, scope: () => projectStore.currentProjectId },
  )

  /** 详情缓存：进入列表后点开详情不必再等一次网络 */
  const detail = ref<DataSource | null>(null)
  const detailLoading = ref(false)
  const detailError = ref<string | null>(null)

  /** 连通性测试结果，按数据源 id 存；四态由后端下发，前端不推断 */
  const connectivity = ref<Record<number, ConnectivityResult>>({})
  /** 正在测试中的数据源 id 集合 */
  const testing = ref<Set<number>>(new Set())

  // --- 元数据探查 ---
  const profiles = ref<MetadataProfileBrief[]>([])
  const profilesLoading = ref(false)
  /** 探查版本列表的加载失败；与「确实没有探查记录」是两回事，不可混为同一个空态 */
  const profilesError = ref<string | null>(null)
  /** 当前查看的 profile 版本 */
  const activeProfileVersion = ref<number | null>(null)
  const activeProfile = ref<MetadataProfile | null>(null)
  const profileLoading = ref(false)
  const profileError = ref<string | null>(null)

  const hasItems: ComputedRef<boolean> = computed(() => query.items.value.length > 0)

  /** 探查中的版本（列表页显示「探查中」时禁止再点探查） */
  const probingVersion: ComputedRef<number | null> = computed(() => {
    const hit = profiles.value.find((p) => p.status === 'probing')
    return hit?.version ?? null
  })

  async function loadDetail(id: number): Promise<void> {
    detailLoading.value = true
    detailError.value = null
    try {
      detail.value = await datasourceApi.fetchDataSource(id)
    } catch (e) {
      detail.value = null
      detailError.value = toDisplayMessage(e)
    } finally {
      detailLoading.value = false
    }
  }

  function clearDetail(): void {
    detail.value = null
    detailError.value = null
    activeProfile.value = null
    activeProfileVersion.value = null
    profiles.value = []
    profilesError.value = null
  }

  /**
   * 触发连通性测试并轮询到终态。
   * 返回四态结果；调用方据 state 决定提示文案——
   * `denied`（权限不足）与 `failed`（连不上）要给不同的处置建议。
   */
  async function testConnectivity(id: number): Promise<ConnectivityResult> {
    testing.value = new Set(testing.value).add(id)
    try {
      const started = await datasourceApi.startConnectivityTest(id)
      connectivity.value = { ...connectivity.value, [id]: started }

      // 已终态（后端同步返回）就直接用，不必轮询
      if (started.state !== 'pending') {
        return started
      }

      const deadline = Date.now() + 30_000
      let latest = started
      while (latest.state === 'pending' && Date.now() < deadline) {
        await new Promise((resolve) => setTimeout(resolve, 1200))
        latest = await datasourceApi.fetchConnectivityTest(id, started.test_id)
        connectivity.value = { ...connectivity.value, [id]: latest }
      }
      return latest
    } finally {
      const next = new Set(testing.value)
      next.delete(id)
      testing.value = next
    }
  }

  async function loadProfiles(dataSourceId: number): Promise<void> {
    profilesLoading.value = true
    profilesError.value = null
    try {
      profiles.value = await datasourceApi.fetchProfiles(dataSourceId)
    } catch (e) {
      // 版本列表拉不到不该让整页失败，但**也不能清空**：
      // 清空会让页面显示「无探查记录」，而详情页的其他区块同时列着 v1——
      // 两处互相矛盾时，用户会开始怀疑数据本身，而不是怀疑这一条请求失败了。
      // 保留上一份已知列表，并把失败暴露出去。
      profilesError.value = toDisplayMessage(e)
    } finally {
      profilesLoading.value = false
    }
  }

  async function loadProfile(dataSourceId: number, version: number): Promise<void> {
    profileLoading.value = true
    profileError.value = null
    try {
      activeProfile.value = await datasourceApi.fetchProfile(dataSourceId, version)
      activeProfileVersion.value = version
    } catch (e) {
      activeProfile.value = null
      profileError.value = toDisplayMessage(e)
    } finally {
      profileLoading.value = false
    }
  }

  /** 探查完成后由详情页调用：刷新版本列表并切到新版本 */
  async function refreshAfterProbe(dataSourceId: number, version: number): Promise<void> {
    await loadProfiles(dataSourceId)
    await loadProfile(dataSourceId, version)
    await query.reload()
  }

  /** 探查状态徽章用：取最近一个 profile 的状态 */
  function latestProbeStatus(dataSourceId: number): ProbeStatus | null {
    const ds = query.items.value.find((d) => d.id === dataSourceId)
    return ds?.latest_profile?.status ?? null
  }

  return {
    query,
    hasItems,
    detail,
    detailLoading,
    detailError,
    connectivity,
    testing,
    profiles,
    profilesLoading,
    profilesError,
    activeProfileVersion,
    activeProfile,
    profileLoading,
    profileError,
    probingVersion,
    loadDetail,
    clearDetail,
    testConnectivity,
    loadProfiles,
    loadProfile,
    refreshAfterProbe,
    latestProbeStatus,
  }
})
