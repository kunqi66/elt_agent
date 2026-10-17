<script setup lang="ts">
/**
 * 评测运行列表（fe-07 §6 / be-20）。
 *
 * 「运行中」的行必须一眼能认出来：评测是长任务，用户最容易犯的错是
 * 看到列表就以为结果已经出完了。所以状态用徽章渲染（running 会带上脉动点），
 * 并且 metrics 处于生成中时显示 `—` 而不是 0——0% 会被读成「全军覆没」。
 */
import { QueryTable, SensitiveText, StatusTag } from '@/components/common'
import { DASH, formatDateTime, formatRate, textOrDash } from '@/utils/format'
import type { BenchmarkRunRead } from '@/types'

withDefaults(
  defineProps<{
    items: BenchmarkRunRead[]
    loading?: boolean
    initialLoading?: boolean
    error?: string | null
    total?: number
    page?: number
    pageSize?: number
    selectedRunId: number | null
  }>(),
  { loading: false, initialLoading: false, error: null, total: 0, page: 1, pageSize: 20 },
)

const emit = defineEmits<{
  select: [run: BenchmarkRunRead]
  'page-change': [page: number]
  'page-size-change': [size: number]
  retry: []
}>()

/** 运行中的指标还在生成，显示 0% 会被误读成「全部失败」，用 `—` 表达「还没有」 */
function rateOrDash(run: BenchmarkRunRead, key: 'gate_pass_rate' | 'p0p1_block_rate'): string {
  if (run.status === 'running') {
    return DASH
  }
  return formatRate(run.metrics[key])
}

function onRowClick(row: BenchmarkRunRead): void {
  emit('select', row)
}

/**
 * el-table 插槽的 row 静态类型是 Element Plus 的 DefaultRow，与我们的 DTO 无类型关联。
 * 直接读属性不报错（索引签名读出 any），但作为参数传给强类型函数就会失败。
 * 与其放宽那些函数的签名（会让整文件的类型检查一起退化），不如在调用点收口。
 */
function runOf(row: unknown): BenchmarkRunRead {
  return row as BenchmarkRunRead
}
</script>

<template>
  <QueryTable
    :items="items"
    :loading="loading"
    :initial-loading="initialLoading"
    :error="error"
    :total="total"
    :page="page"
    :page-size="pageSize"
    :min-height="220"
    row-key="id"
    empty-text="还没有评测运行"
    empty-description="触发评测后会在这里出现运行记录；本页只读，不提供触发入口。"
    @row-click="onRowClick"
    @retry="emit('retry')"
    @page-change="(p: number) => emit('page-change', p)"
    @page-size-change="(s: number) => emit('page-size-change', s)"
  >
    <el-table-column label="运行" width="160">
      <template #default="{ row }">
        <div class="run__id elt-mono">
          #{{ row.id }}
          <el-tag v-if="row.id === selectedRunId" size="small" effect="plain">当前</el-tag>
        </div>
        <div class="run__meta">{{ textOrDash(row.triggered_by_name) }}</div>
      </template>
    </el-table-column>

    <el-table-column label="套件" min-width="180">
      <template #default="{ row }">
        <div class="run__suite">{{ row.suite_name }}</div>
        <div class="run__meta elt-mono">v{{ row.suite_version }}</div>
      </template>
    </el-table-column>

    <el-table-column label="层级" width="100">
      <template #default="{ row }">
        <StatusTag kind="BenchmarkLayer" :value="row.layer" size="small" />
      </template>
    </el-table-column>

    <el-table-column label="状态" width="110">
      <template #default="{ row }">
        <StatusTag kind="BenchmarkRunStatus" :value="row.status" size="small" />
      </template>
    </el-table-column>

    <el-table-column label="门禁通过率" width="110">
      <template #default="{ row }">
        <span class="elt-mono">{{ rateOrDash(runOf(row), 'gate_pass_rate') }}</span>
      </template>
    </el-table-column>

    <el-table-column label="P0/P1 拦截率" width="120">
      <template #default="{ row }">
        <span class="elt-mono">{{ rateOrDash(runOf(row), 'p0p1_block_rate') }}</span>
      </template>
    </el-table-column>

    <el-table-column label="时间" width="200">
      <template #default="{ row }">
        <div class="run__meta">起 {{ formatDateTime(row.started_at) }}</div>
        <div class="run__meta">终 {{ row.finished_at === null ? '—' : formatDateTime(row.finished_at) }}</div>
      </template>
    </el-table-column>

    <el-table-column label="模型 / 提示词" width="150">
      <template #default="{ row }">
        <div class="run__meta elt-mono">{{ textOrDash(row.model_version) }}</div>
        <div class="run__meta elt-mono">{{ textOrDash(row.prompt_version) }}</div>
      </template>
    </el-table-column>

    <el-table-column label="失败原因" min-width="200">
      <template #default="{ row }">
        <SensitiveText v-if="row.error_message" :value="row.error_message" multiline />
        <span v-else class="run__meta">—</span>
      </template>
    </el-table-column>
  </QueryTable>
</template>

<style scoped>
.run__id {
  display: flex;
  gap: 6px;
  align-items: center;
  font-size: 13px;
  color: var(--elt-text-primary);
}

.run__suite {
  font-size: 13px;
  color: var(--elt-text-primary);
}

.run__meta {
  font-size: 11px;
  color: var(--elt-text-muted);
}
</style>
