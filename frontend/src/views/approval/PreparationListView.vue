<script setup lang="ts">
/**
 * 审批中心 · 准备单列表（fe-05 §5 步骤 1–2 / §6）。
 *
 * 三件事必须由**后端**回答，页面只负责把答案画出来：
 * 1. `scope` 过滤（待我审批 / 我发起的 / 我处理过的 / 全部）——前端不按
 *    `created_by` 二次裁剪，否则分页 total 会与实际条数对不上；
 * 2. `my_slot`（该行是否归我审）——不在这里重算四眼规则，前端算出来的
 *    「我能审」与后端不一致时，用户会看到一个必然被拒的按钮；
 * 3. `risk_level` / `status` 的文案与配色——统一走 `utils/enums`。
 *
 * 5 秒轮询的目的只有一个：别人批完之后，我这边不用手动刷新就能看到变化。
 * 切到后台标签页会自动暂停（`usePolling` 内置），不占用不必要的并发。
 */
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { Refresh } from '@element-plus/icons-vue'
import { PageHeader, QueryTable } from '@/components/common'
import { usePolling } from '@/composables/usePolling'
import { useAuthStore } from '@/stores/auth'
import { usePreparationStore } from '@/stores/preparation'
import PreparationFilters from './components/PreparationFilters.vue'
import PreparationTable from './components/PreparationTable.vue'
import type { ApprovalSlot, PreparationFilter, PreparationSummary } from '@/types'

const router = useRouter()
const store = usePreparationStore()
const authStore = useAuthStore()

/** 列表轮询间隔（fe-05 §3.1：列表 5 s） */
const POLL_INTERVAL_MS = 5000

const SCOPE_TABS: ReadonlyArray<{ name: PreparationFilter['scope']; label: string }> = [
  { name: 'todo', label: '待我审批' },
  { name: 'mine', label: '我发起的' },
  { name: 'done', label: '我处理过的' },
  { name: 'all', label: '全部' },
]

/** 各 scope 下「一条都没有」的真实含义不同，文案也跟着不同 */
const SCOPE_EMPTY: Record<PreparationFilter['scope'], { title: string; description: string }> = {
  todo: {
    title: '当前没有待你审批的准备单',
    description: '只有落在你职责槽（数据槽 / 安全槽）且未过期的准备单会出现在这里。',
  },
  mine: {
    title: '你还没有提交过准备单',
    description: '在 Pipeline Studio 冻结版本后会自动生成准备单。',
  },
  done: {
    title: '你还没有处理过准备单',
    description: '审批过的准备单会保留在这里，供后续对账。',
  },
  all: {
    title: '当前项目还没有准备单',
    description: '在 Pipeline Studio 冻结版本后会自动生成准备单。',
  },
}

const scope = computed<PreparationFilter['scope']>(() => store.query.filter.scope)

/** 当前用户 id 只用于列表上标注「我发起」；权限判定一律不在这里做 */
const currentUserId = computed<number>(() => authStore.profile?.id ?? authStore.user?.id ?? 0)

/** 是否叠加了 scope 之外的筛选条件；决定空态说「没数据」还是「没命中」 */
const filtered = computed<boolean>(() => {
  const f = store.query.filter
  return (
    f.status !== undefined ||
    f.risk_level !== undefined ||
    f.environment !== undefined ||
    f.slot !== undefined ||
    f.pipeline_id !== undefined ||
    (f.keyword !== undefined && f.keyword !== '')
  )
})

/** 筛选生效时空态交给基座的「没有符合条件的记录」，不再说 scope 的事 */
const emptyText = computed<string | undefined>(() =>
  filtered.value ? undefined : SCOPE_EMPTY[scope.value].title,
)
const emptyDescription = computed<string | undefined>(() =>
  filtered.value ? undefined : SCOPE_EMPTY[scope.value].description,
)

const listPolling = usePolling(() => store.query.reload(), {
  intervalMs: POLL_INTERVAL_MS,
  // 首拍由 onMounted 的 load() 负责，避免挂载瞬间连发两次相同的列表请求
  immediate: false,
})

/** 手动刷新单独一件事：直接绑 query.loading 会让按钮每 5 秒跟着轮询闪一次 */
const refreshing = ref(false)

async function onRefresh(): Promise<void> {
  refreshing.value = true
  try {
    await store.query.reload()
  } finally {
    refreshing.value = false
  }
}

/**
 * 改条件前先停一拍轮询再重启：否则「旧条件下的轮询响应」可能后到，
 * 把新条件的查询结果盖掉，用户会看到筛选框与列表对不上的诡异画面。
 */
async function runQuery(action: () => Promise<void>): Promise<void> {
  listPolling.stop()
  try {
    await action()
  } finally {
    listPolling.start()
  }
}

function onScopeChange(name: string | number): void {
  void runQuery(() => store.query.patchFilter({ scope: String(name) as PreparationFilter['scope'] }))
}

function onFilterChange(patch: Partial<PreparationFilter>): void {
  void runQuery(() => store.query.patchFilter(patch))
}

function onResetFilter(): void {
  void runQuery(() => store.query.reset())
}

/**
 * 行点击 / 审批按钮都进详情页。
 * `slot` 只用于区分「用户点的是审批入口」，目标页仍以后端下发的 `my_slot` 决定哪些槽可操作。
 */
function onReview(payload: { preparation_id: number; slot: ApprovalSlot }): void {
  void router.push({ name: 'preparation-detail', params: { id: String(payload.preparation_id) } })
}

function onRowClick(row: PreparationSummary): void {
  void router.push({ name: 'preparation-detail', params: { id: String(row.id) } })
}

onMounted(() => {
  // store 的 useListQuery 以 immediate:false 构造，何时拉取由页面决定
  void store.query.load()
  listPolling.start()
})
</script>

<template>
  <div class="prep-list">
    <PageHeader
      title="审批中心"
      description="按职责槽汇总待审与已审的准备单；冻结事实只读，审批决定一经提交不可修改。"
    >
      <template #actions>
        <el-button size="small" :icon="Refresh" :loading="refreshing" @click="onRefresh">
          刷新
        </el-button>
      </template>

      <template #tabs>
        <el-tabs class="prep-list__tabs" :model-value="scope" @tab-change="onScopeChange">
          <el-tab-pane v-for="tab in SCOPE_TABS" :key="tab.name" :label="tab.label" :name="tab.name" />
        </el-tabs>
      </template>
    </PageHeader>

    <div class="prep-list__filters">
      <!-- 不绑 query.loading：5 秒轮询会让筛选栏周期性变灰，用户正打字时会被打断 -->
      <PreparationFilters :filter="store.query.filter" @change="onFilterChange" @reset="onResetFilter" />
    </div>

    <QueryTable
      :items="store.query.items"
      :loading="store.query.loading"
      :initial-loading="store.query.initialLoading"
      :error="store.query.error"
      :request-id="store.query.errorRequestId"
      :total="store.query.total"
      :page="store.query.meta.page"
      :page-size="store.query.meta.page_size"
      :filtered="filtered"
      :empty-text="emptyText"
      :empty-description="emptyDescription"
      row-key="id"
      :min-height="280"
      @retry="store.query.reload()"
      @page-change="store.query.setPage"
      @page-size-change="store.query.setPageSize"
      @row-click="onRowClick"
    >
      <PreparationTable :current-user-id="currentUserId" @review="onReview" />
    </QueryTable>
  </div>
</template>

<style scoped>
.prep-list {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.prep-list__filters {
  display: flex;
}

.prep-list__filters > * {
  flex: 1;
}

/* PageHeader 的 tabs 槽自带下边框，el-tabs 也会画一条，去掉后者避免双线 */
.prep-list__tabs :deep(.el-tabs__header) {
  margin: 0;
}

.prep-list__tabs :deep(.el-tabs__nav-wrap::after) {
  height: 0;
}
</style>
