<script setup lang="ts">
/**
 * 运行中心列表（路由 /runs，fe-06 §6）。
 *
 * 这个页面是**只读仪表盘**：不做状态机推演、不直接调 SeaTunnel、
 * 也不逐行轮询详情（禁止把 2s × N 行的请求放大成流量风暴）。
 *
 * 关于列表轮询：仅当列表里存在「非终态」运行时才继续 5s 轮询，
 * 全部终态即自停（`isRunTerminal`），并在页头显式写出「自动刷新已暂停」——
 * 一个静默停掉的自动刷新会让人以为数据死了，进而反复手点刷新。
 */
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { PageHeader, QueryTable, RunStatusTag, SensitiveText } from '@/components/common'
import { usePolling } from '@/composables/usePolling'
import { useProjectStore } from '@/stores/project'
import { useRunStore } from '@/stores/run'
import { enumText } from '@/utils/enums'
import { DASH, formatBytes, formatDateTime, formatDuration, formatInt, formatRate, formatRows } from '@/utils/format'
import { isRunTerminal } from '@/types'
import type { RunFilter, RunSummary } from '@/types'
import RunFilters from './components/RunFilters.vue'

/** 列表轮询：比详情慢（fe-06 §3.1 的 5 s），列表对秒级新鲜度不敏感 */
const LIST_POLL_INTERVAL_MS = 5000

const router = useRouter()
const runStore = useRunStore()
const projectStore = useProjectStore()

/** 是否存在仍在推进的运行；false 时轮询自停 */
const hasActiveRun = computed<boolean>(() => runStore.query.items.some((item) => !isRunTerminal(item.status)))

const pollingPaused = ref(false)

const polling = usePolling(refreshList, {
  intervalMs: LIST_POLL_INTERVAL_MS,
  immediate: false,
  shouldStop: () => {
    const stop = !hasActiveRun.value
    if (stop) {
      pollingPaused.value = true
    }
    return stop
  },
})

const hasFilter = computed<boolean>(() => {
  const filter = runStore.query.filter
  return (
    (filter.status?.length ?? 0) > 0 ||
    filter.pipeline_id !== undefined ||
    filter.from !== undefined ||
    filter.to !== undefined ||
    (filter.q !== undefined && filter.q !== '')
  )
})

async function refreshList(): Promise<void> {
  await runStore.query.reload()
}

async function reload(): Promise<void> {
  await refreshList()
  resumePolling()
}

/** 用户主动刷新或改筛选条件后恢复轮询：这时候又可能有活跃运行了 */
function resumePolling(): void {
  if (polling.active.value) {
    return
  }
  pollingPaused.value = false
  polling.start()
}

async function applyFilter(filters: RunFilter): Promise<void> {
  await runStore.query.patchFilter(filters)
  resumePolling()
}

async function resetFilter(): Promise<void> {
  await runStore.query.reset()
  resumePolling()
}

function openRun(row: RunSummary): void {
  void router.push({ name: 'run-detail', params: { id: String(row.id) } })
}

/**
 * el-table 插槽的 row 静态类型是 Element Plus 的 DefaultRow，与我们的 DTO 无类型关联。
 * 直接读属性不报错（索引签名读出 any），但作为参数传给强类型函数就会失败。
 * 与其放宽那些函数的签名（会让整文件的类型检查一起退化），不如在调用点收口。
 */
function runOf(row: unknown): RunSummary {
  return row as RunSummary
}

function startedTitle(row: RunSummary): string {
  return row.started_at === null ? '尚未开始' : `开始于 ${formatDateTime(row.started_at)}`
}

/**
 * 列表 DTO（`RunSummary`）没有下发预算上限，因此这里**不做阈值着色**：
 * 编一个 5% 的阈值比不着色更糟——它会让人以为那是契约值。
 * 详情页的拒绝率按 `budget.max_reject_rate` 精确着色（fe-06 §10 已登记该字段缺口）。
 */
function rejectText(row: RunSummary): string {
  return row.reject_rows === 0 ? formatRate(0) : formatRate(row.reject_rate)
}

/**
 * 空态文案只在「真的没有数据」时给出：QueryTable 会把 emptyText 作为 EmptyState 的标题，
 * 与 variant 无关——筛选无命中时若还写「还没有运行记录」，就把「筛没了」说成了「没数据」。
 */
const emptyText = computed<string | undefined>(() => (hasFilter.value ? undefined : '还没有运行记录'))
const emptyDescription = computed<string | undefined>(() =>
  hasFilter.value ? undefined : '准备单通过审批并 Commit 后会产生第一条运行记录。',
)

onMounted(async () => {
  // 项目上下文是所有业务请求的前置：没有它，列表可能拉到「上一个项目」的数据
  await projectStore.ensureLoaded()
  await refreshList()
  polling.start()
})
</script>

<template>
  <div class="run-list">
    <PageHeader
      title="运行中心"
      description="运行只读仪表盘与监督命令入口：副作用由后端 Harness 执行，本页不直接接触数据面。"
    >
      <template #actions>
        <el-button size="small" :loading="runStore.query.loading" @click="reload">刷新</el-button>
      </template>
      <template #notice>
        <div class="run-list__notice">
          <span v-if="hasActiveRun" class="run-list__notice-item run-list__notice-item--active">
            存在进行中的运行，每 {{ LIST_POLL_INTERVAL_MS / 1000 }} 秒自动刷新（切到后台标签页时暂停）
          </span>
          <span v-else-if="pollingPaused" class="run-list__notice-item">
            没有进行中的运行，自动刷新已暂停
            <el-button link size="small" @click="resumePolling">恢复自动刷新</el-button>
          </span>
          <span class="run-list__notice-item run-list__notice-item--muted">
            本页不展示运行日志与指标明细：进入详情后按 2 秒增量轮询，避免列表逐行请求放大流量。
          </span>
        </div>
      </template>
    </PageHeader>

    <RunFilters :filters="runStore.query.filter" @update:filters="applyFilter" @reset="resetFilter" />

    <QueryTable
      class="run-list__table"
      :items="runStore.query.items"
      :loading="runStore.query.loading"
      :initial-loading="runStore.query.initialLoading"
      :error="runStore.query.error"
      :request-id="runStore.query.errorRequestId"
      :total="runStore.query.meta.total"
      :page="runStore.query.meta.page"
      :page-size="runStore.query.meta.page_size"
      :filtered="hasFilter"
      row-key="id"
      :min-height="320"
      :empty-text="emptyText"
      :empty-description="emptyDescription"
      @retry="reload"
      @row-click="openRun"
      @page-change="(p: number) => runStore.query.setPage(p)"
      @page-size-change="(s: number) => runStore.query.setPageSize(s)"
    >
      <el-table-column label="Pipeline" min-width="200">
        <template #default="{ row }">
          <div class="run-list__pipeline">
            <SensitiveText :value="row.pipeline_name" />
            <span class="run-list__version elt-mono">v{{ row.version_no }}</span>
          </div>
        </template>
      </el-table-column>

      <el-table-column label="状态" width="120">
        <template #default="{ row }">
          <RunStatusTag :status="row.status" />
        </template>
      </el-table-column>

      <el-table-column label="目标表" min-width="180">
        <template #default="{ row }">
          <el-tooltip :content="row.formal_table ?? DASH" placement="top" :disabled="row.formal_table === null">
            <span class="run-list__ellipsis">
              <SensitiveText :value="row.formal_table" mono />
            </span>
          </el-tooltip>
        </template>
      </el-table-column>

      <el-table-column label="触发人" width="110">
        <template #default="{ row }">
          <SensitiveText :value="row.triggered_by_name" />
        </template>
      </el-table-column>

      <el-table-column label="开始时间" width="170">
        <template #default="{ row }">
          <span class="elt-mono" :title="startedTitle(runOf(row))">{{ formatDateTime(row.started_at) }}</span>
        </template>
      </el-table-column>

      <el-table-column label="耗时" width="110">
        <template #default="{ row }">
          <span class="elt-mono">{{ formatDuration(row.duration_sec) }}</span>
        </template>
      </el-table-column>

      <el-table-column label="读取行数" width="110" align="right">
        <template #default="{ row }">
          <span class="elt-mono" :title="`${formatInt(row.read_rows)} 行`">{{ formatRows(row.read_rows) }}</span>
        </template>
      </el-table-column>

      <el-table-column label="写入字节" width="110" align="right">
        <template #default="{ row }">
          <span class="elt-mono" :title="`${formatInt(row.written_bytes)} B`">{{ formatBytes(row.written_bytes) }}</span>
        </template>
      </el-table-column>

      <el-table-column label="拒绝" width="130" align="right">
        <template #default="{ row }">
          <span class="elt-mono run-list__reject">
            {{ rejectText(runOf(row)) }}
            <span class="run-list__reject-count">{{ formatRows(row.reject_rows) }}</span>
          </span>
        </template>
      </el-table-column>

      <el-table-column label="" width="86" align="right" fixed="right">
        <template #default="{ row }">
          <el-button link size="small" @click.stop="openRun(runOf(row))">
            详情
          </el-button>
        </template>
      </el-table-column>

      <!-- 空态动作只给「下一步」：有筛选时是清空筛选，无筛选时没什么可点的 -->
      <template #empty-actions>
        <el-button v-if="hasFilter" size="small" @click="resetFilter">清空筛选</el-button>
      </template>
    </QueryTable>

    <p class="run-list__foot">
      列表口径：耗时、行数、字节与拒绝率全部取后端返回值，前端不做分子分母运算。
      状态徽章文案与配色来自统一枚举口径（{{ enumText('RunStatus', 'running') }} / {{ enumText('RunStatus', 'interrupted') }} 等）。
    </p>
  </div>
</template>

<style scoped>
.run-list {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.run-list__notice {
  display: flex;
  flex-direction: column;
  gap: 2px;
  font-size: 11px;
  color: var(--elt-text-secondary);
}

.run-list__notice-item--active {
  color: var(--elt-accent);
}

.run-list__notice-item--muted {
  color: var(--elt-text-muted);
}

.run-list__table {
  padding: 0 14px 12px;
  background-color: var(--elt-bg-surface);
  border: 1px solid var(--elt-border-subtle);
  border-radius: var(--elt-radius-card);
}

.run-list__pipeline {
  display: flex;
  gap: 6px;
  align-items: baseline;
  min-width: 0;
}

.run-list__version {
  flex: none;
  font-size: 11px;
  color: var(--elt-text-muted);
}

.run-list__ellipsis {
  display: inline-block;
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.run-list__reject {
  display: inline-flex;
  flex-direction: column;
  align-items: flex-end;
  line-height: 1.4;
}

.run-list__reject-count {
  font-size: 11px;
  color: var(--elt-text-muted);
}

.run-list__foot {
  margin: 0;
  font-size: 11px;
  line-height: 1.7;
  color: var(--elt-text-muted);
}
</style>
