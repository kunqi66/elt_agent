/**
 * 列表页共用骨架（fe-01 §2）。
 *
 * 全站 7 个列表页（数据源 / Pipeline / 准备单 / 运行 / 策略 / Benchmark / 账本）
 * 的加载、翻页、筛选、空态、错误态语义完全一致。集中在这里是为了让
 * 「翻页是否重置」这类决定只做一次——否则各页行为会悄悄分叉，
 * 用户在不同页面遇到不同的分页手感。
 *
 * 约定：
 * - `search()` 把 page 重置为 1（换了条件就该看第一页）；
 * - `patchFilter()` 也重置 page，并复用 search 的语义；
 * - `reload()` 保持当前页（操作完成后的刷新，不该把用户弹回第一页）；
 * - 错误**不吞**：写进 `error` 供页面渲染 ErrorState，同时保留 message。
 *
 * ⚠️ 访问方式取决于你在哪一层用：
 * - **组件里直接 `const q = useListQuery(...)`** → `q.items.value`（`q.items` 是 Ref）；
 * - **经 pinia store 暴露出去**（本项目 stores/*.ts 的做法）→ `store.query.items`。
 *   Pinia 把 setup 返回值过了一遍 `reactive()`，ref 会被**深层解包**，
 *   所以 store 上不再有 `.value`。
 * 两种写法 TypeScript 都会替你判对，写错必然报错，不会静默出错。
 */

import { computed, ref, watch } from 'vue'
import type { ComputedRef, Ref } from 'vue'
import type { Page, PageMeta } from '@/types'
import { isBizError, toDisplayMessage } from '@/utils/errors'

/** 分页查询的最小约束：任何筛选条件都必须带分页字段 */
export interface PageableFilter {
  page: number
  page_size: number
}

export interface UseListQueryOptions {
  /** 挂载即加载；默认 true。store 场景传 false，由页面决定何时拉 */
  immediate?: boolean
  /** page_size 初始值；默认取 defaultFilter 里的值 */
  pageSize?: number
  /**
   * 作用域访问器（通常是 `() => projectStore.currentProjectId`）。
   *
   * 作用域变化时必须**清空**列表：否则切换项目后，A 项目的数据会
   * 停留在屏幕上直到 B 项目的响应回来，那一瞬间的展示是错的。
   * 已经加载过的列表在切换后会重新拉取；从未加载过的则不拉（页面还没用到它）。
   */
  scope?: () => unknown
}

export interface ListQuery<T, F extends PageableFilter> {
  items: Ref<T[]>
  meta: Ref<PageMeta>
  loading: Ref<boolean>
  /** 错误文案；空态与错误态互斥，页面据此二选一渲染 */
  error: Ref<string | null>
  /** 最近一次失败的 request_id，供 ErrorState 展示可复制的排查凭据 */
  errorRequestId: Ref<string | null>
  filter: Ref<F>
  total: ComputedRef<number>
  totalPages: ComputedRef<number>
  isEmpty: ComputedRef<boolean>
  /** 首次加载还未完成（用于骨架屏，与 loading 区分：翻页时不该再出骨架） */
  initialLoading: Ref<boolean>
  /** 按当前 filter 拉取 */
  load: () => Promise<void>
  /** 保持当前页刷新 */
  reload: () => Promise<void>
  /** 重置到第一页并拉取 */
  search: () => Promise<void>
  /** 合并筛选条件、回到第一页并拉取 */
  patchFilter: (patch: Partial<F>) => Promise<void>
  /** 回到初始条件并拉取 */
  reset: () => Promise<void>
  setPage: (page: number) => Promise<void>
  setPageSize: (size: number) => Promise<void>
  /** 清空列表但**不**发请求（切换项目时用） */
  clear: () => void
}

/**
 * @param makeFilter 构造初始筛选条件（每次 reset 都会重新调用，避免共享引用）
 * @param fetcher 拉取函数；通常直接传 `(f) => api.fetchXxx(projectId, f)`
 */
export function useListQuery<T, F extends PageableFilter>(
  makeFilter: () => F,
  fetcher: (filter: F) => Promise<Page<T>>,
  options: UseListQueryOptions = {},
): ListQuery<T, F> {
  const initial = makeFilter()
  if (options.pageSize !== undefined) {
    initial.page_size = options.pageSize
  }

  const items = ref<T[]>([]) as Ref<T[]>
  const meta = ref<PageMeta>({ page: initial.page, page_size: initial.page_size, total: 0 })
  const loading = ref(false)
  const error = ref<string | null>(null)
  /**
   * 最近一次失败的 request_id（成功或未失败时为 null）。
   *
   * 单独取出而不是塞进 error 文案里：错误态页面上「请把 request_id 提供给管理员」
   * 这句提示只有在 request_id 能一键复制时才有用，混在一段中文里没法选中复制。
   */
  const errorRequestId = ref<string | null>(null)
  const filter = ref<F>(initial) as Ref<F>
  /** 首屏骨架只出一次；之后翻页用行内 loading，避免整页闪动 */
  const initialLoading = ref(true)

  const total = computed<number>(() => meta.value.total)
  const totalPages = computed<number>(() =>
    meta.value.page_size <= 0 ? 1 : Math.max(1, Math.ceil(meta.value.total / meta.value.page_size)),
  )
  const isEmpty = computed<boolean>(() => !loading.value && error.value === null && items.value.length === 0)

  async function load(): Promise<void> {
    loading.value = true
    error.value = null
    errorRequestId.value = null
    try {
      const page = await fetcher(filter.value)
      items.value = page.items
      meta.value = page.meta
    } catch (e) {
      // 保留原有 items：加载失败时清空列表会让用户以为「数据没了」
      error.value = toDisplayMessage(e)
      errorRequestId.value = isBizError(e) && e.requestId !== '' ? e.requestId : null
    } finally {
      loading.value = false
      initialLoading.value = false
    }
  }

  async function reload(): Promise<void> {
    await load()
  }

  async function search(): Promise<void> {
    filter.value.page = 1
    await load()
  }

  async function patchFilter(patch: Partial<F>): Promise<void> {
    filter.value = { ...filter.value, ...patch, page: 1 }
    await load()
  }

  async function reset(): Promise<void> {
    const fresh = makeFilter()
    if (options.pageSize !== undefined) {
      fresh.page_size = options.pageSize
    }
    filter.value = fresh
    await load()
  }

  async function setPage(page: number): Promise<void> {
    filter.value.page = page
    await load()
  }

  async function setPageSize(size: number): Promise<void> {
    filter.value.page_size = size
    filter.value.page = 1
    await load()
  }

  function clear(): void {
    items.value = []
    meta.value = { page: 1, page_size: filter.value.page_size, total: 0 }
    error.value = null
    errorRequestId.value = null
    // 复位成「未加载态」：下次进入页面应重新出骨架屏，而不是空白表格
    initialLoading.value = true
  }

  if (options.scope !== undefined) {
    watch(options.scope, () => {
      // 先记下是否加载过 —— clear() 会把 initialLoading 复位
      const hasLoaded = !initialLoading.value
      clear()
      if (hasLoaded) {
        void load()
      }
    })
  }

  if (options.immediate !== false) {
    void load()
  }

  return {
    items,
    meta,
    loading,
    error,
    errorRequestId,
    filter,
    total,
    totalPages,
    isEmpty,
    initialLoading,
    load,
    reload,
    search,
    patchFilter,
    reset,
    setPage,
    setPageSize,
    clear,
  }
}
