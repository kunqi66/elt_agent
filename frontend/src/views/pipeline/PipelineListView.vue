<script setup lang="ts">
/**
 * Pipeline 列表（fe-04 §4 / §5.2）。
 *
 * 列表状态**全部住在 store 里**（`stores/pipeline.ts` 的 `query`）：
 * 从 Studio 返回列表时不该重新拉一遍、也不该丢掉筛选条件与页码——
 * 这是把 query 放进 store 而不是页面局部的原因。
 *
 * 这里不提供「新建 Pipeline」：后端目前没有创建接口（api/pipeline.ts 只有
 * 生成/澄清/制品/冻结），Pipeline 由后端流程产生。宁可没有入口，
 * 也不要放一个点了会报 404 的按钮。
 */
import { computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { Refresh, Search } from '@element-plus/icons-vue'
import { PageHeader, QueryTable, StatusTag } from '@/components/common'
import { usePipelineStore } from '@/stores/pipeline'
import { useProjectStore } from '@/stores/project'
import { enumOptionsOf } from '@/utils/enums'
import { DASH, formatDateTime } from '@/utils/format'
import type { Pipeline, PipelineStatus } from '@/types'

const router = useRouter()
const store = usePipelineStore()
const projectStore = useProjectStore()

onMounted(() => {
  // store 里 immediate:false：由页面决定何时拉，避免「没进入页面就发请求」
  void store.query.load()
})

const statusOptions = computed(() => enumOptionsOf('PipelineStatus', store.query.items.map((p) => p.status)))

/** 有关键字或状态筛选时，空态要说「没命中」而不是「没有数据」 */
const filtered = computed<boolean>(
  () => (store.query.filter.q ?? '').trim() !== '' || (store.query.filter.status?.length ?? 0) > 0,
)

/** 当前项目名：列表是「项目内」视角，标题栏上要能看见自己在哪个项目里 */
const projectName = computed<string>(() => projectStore.currentProject?.name ?? '')

/** 数据源只展示 name/type/env（store 已剔除 endpoint 与 secret_ref） */
function dataSourceName(id: number | null): string {
  if (id === null) {
    return DASH
  }
  return store.dataSourceOptions.find((o) => o.id === id)?.name ?? `#${id}`
}

/**
 * 表格插槽给出的 row 是 Element Plus 的 DefaultRow（Record 型，不是我们的 Pipeline）。
 * 这里显式收窄一次：不让 any 顺着调用链继续往下传，后面接手的人才知道类型从哪来。
 */
function openStudio(row: Pipeline | Record<string, unknown>): void {
  const pipeline = row as Pipeline
  void router.push({ name: 'pipeline-studio', params: { id: String(pipeline.id) } })
}

function onStatusChange(): void {
  // 换条件必须回到第一页，否则会停在越界页上看到空白
  void store.query.search()
}

function onKeywordSearch(): void {
  void store.query.search()
}

function resetFilter(): void {
  void store.query.reset()
}
</script>

<template>
  <div class="pl">
    <PageHeader
      title="Pipeline Studio"
      description="自然语言需求生成、门禁评估、版本冻结与提交审批。点击任意一行进入 Studio。"
    >
      <template #actions>
        <el-button size="small" :icon="Refresh" :loading="store.query.loading" @click="store.query.reload()">
          刷新
        </el-button>
      </template>
    </PageHeader>

    <div class="pl__filter">
      <el-input
        :model-value="store.query.filter.q ?? ''"
        class="pl__search"
        size="small"
        placeholder="按名称 / 描述搜索"
        clearable
        :prefix-icon="Search"
        @update:model-value="(v: string) => (store.query.filter.q = v)"
        @keyup.enter="onKeywordSearch"
        @clear="onKeywordSearch"
      />
      <el-select
        :model-value="store.query.filter.status ?? []"
        class="pl__status"
        size="small"
        multiple
        collapse-tags
        collapse-tags-tooltip
        placeholder="状态"
        @update:model-value="
          (v: PipelineStatus[]) => {
            store.query.filter.status = v
            onStatusChange()
          }
        "
      >
        <el-option v-for="o in statusOptions" :key="o.value" :value="o.value" :label="o.label" />
      </el-select>
      <el-button size="small" @click="onKeywordSearch">查询</el-button>
      <el-button size="small" :disabled="!filtered" @click="resetFilter">重置</el-button>
      <span class="pl__spacer" />
      <span v-if="projectName !== ''" class="pl__project">项目：{{ projectName }}</span>
    </div>

    <QueryTable
      :items="store.query.items"
      :loading="store.query.loading"
      :initial-loading="store.query.initialLoading"
      :error="store.query.error"
      :request-id="store.query.errorRequestId"
      :total="store.query.total"
      :page="store.query.filter.page"
      :page-size="store.query.filter.page_size"
      :filtered="filtered"
      row-key="id"
      empty-text="当前项目还没有 Pipeline"
      empty-description="Pipeline 由生成流程产出；先到「数据源」页确认已有可用数据源与元数据探查结果。"
      @retry="store.query.reload()"
      @page-change="(p: number) => store.query.setPage(p)"
      @page-size-change="(s: number) => store.query.setPageSize(s)"
      @row-click="openStudio"
    >
      <el-table-column label="名称" min-width="220">
        <template #default="{ row }">
          <div class="pl__name">{{ row.name }}</div>
          <div v-if="row.description !== ''" class="pl__desc">{{ row.description }}</div>
        </template>
      </el-table-column>

      <el-table-column label="状态" width="120">
        <template #default="{ row }">
          <StatusTag kind="PipelineStatus" :value="row.status" size="small" />
        </template>
      </el-table-column>

      <el-table-column label="当前版本" width="110">
        <template #default="{ row }">
          <span v-if="row.current_version_id === null" class="pl__muted">{{ DASH }}</span>
          <span v-else class="elt-mono">#{{ row.current_version_id }}</span>
        </template>
      </el-table-column>

      <el-table-column label="源 → 目标" min-width="220">
        <template #default="{ row }">
          <span class="pl__flow">
            <span>{{ dataSourceName(row.source_data_source_id) }}</span>
            <span class="pl__muted">→</span>
            <span>{{ dataSourceName(row.target_data_source_id) }}</span>
          </span>
        </template>
      </el-table-column>

      <el-table-column label="更新时间" width="170">
        <template #default="{ row }">
          <span class="pl__muted">{{ formatDateTime(row.updated_at) }}</span>
        </template>
      </el-table-column>

      <el-table-column label="操作" width="110" align="right">
        <template #default="{ row }">
          <el-button link type="primary" size="small" @click.stop="openStudio(row)">进入 Studio</el-button>
        </template>
      </el-table-column>

      <template #empty-actions>
        <el-button size="small" :icon="Refresh" @click="store.query.reload()">重新加载</el-button>
      </template>
    </QueryTable>
  </div>
</template>

<style scoped>
.pl {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.pl__filter {
  display: flex;
  gap: 8px;
  align-items: center;
}

.pl__search {
  width: 260px;
}

.pl__status {
  width: 220px;
}

.pl__spacer {
  flex: 1;
}

.pl__project {
  font-size: 11px;
  color: var(--elt-text-muted);
}

.pl__name {
  font-size: 13px;
  color: var(--elt-text-primary);
}

.pl__desc {
  overflow: hidden;
  font-size: 11px;
  line-height: 16px;
  color: var(--elt-text-muted);
  text-overflow: ellipsis;
  white-space: nowrap;
}

.pl__flow {
  display: inline-flex;
  gap: 6px;
  align-items: center;
  font-size: 12px;
  color: var(--elt-text-secondary);
}

.pl__muted {
  color: var(--elt-text-muted);
}
</style>
