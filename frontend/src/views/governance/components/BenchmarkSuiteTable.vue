<script setup lang="ts">
/**
 * Benchmark 套件列表（fe-07 §6 / be-20）。
 *
 * 三层分工用徽章固定展示，不允许只写「套件名」：
 *   L0 静态注入（不触发真实链路）/ L1 模拟故障 / L2 真实链路（消耗真实预算）。
 * 层级不说清楚，「通过率掉了」就会变成无法归因的事故。
 *
 * 基线为 null 的套件不打一句灰字「暂无对比」：
 * `baseline === null` 的语义是「基线尚未写入」，与「基线存在但没有数据」是两回事。
 */
import { QueryTable, SensitiveText, StatusTag } from '@/components/common'
import { formatRate, textOrDash } from '@/utils/format'
import type { BenchmarkSuiteRead } from '@/types'

withDefaults(
  defineProps<{
    items: BenchmarkSuiteRead[]
    loading?: boolean
    initialLoading?: boolean
    selectedId: number | null
  }>(),
  { loading: false, initialLoading: false },
)

const emit = defineEmits<{
  select: [id: number]
  retry: []
}>()

function onRowClick(row: BenchmarkSuiteRead): void {
  emit('select', row.id)
}
</script>

<template>
  <div class="suite">
    <QueryTable
      :items="items"
      :loading="loading"
      :initial-loading="initialLoading"
      :show-pagination="false"
      :min-height="200"
      row-key="id"
      empty-text="该项目还没有评测套件"
      empty-description="套件随评测能力一起发布；若刚部署过而这里为空，请点右上角刷新重试。"
      @row-click="onRowClick"
      @retry="emit('retry')"
    >
      <el-table-column label="套件" min-width="240">
        <template #default="{ row }">
          <div class="suite__name">
            <span>{{ row.name }}</span>
            <span class="suite__version elt-mono">v{{ row.version }}</span>
            <el-tag v-if="row.id === selectedId" size="small" effect="plain">当前</el-tag>
          </div>
          <div class="suite__desc">
            <SensitiveText :value="row.description" multiline />
          </div>
        </template>
      </el-table-column>

      <el-table-column label="层级" width="120">
        <template #default="{ row }">
          <StatusTag kind="BenchmarkLayer" :value="row.layer" size="small" />
        </template>
      </el-table-column>

      <el-table-column label="用例数" width="90">
        <template #default="{ row }">
          <span class="elt-mono">{{ row.case_count }}</span>
        </template>
      </el-table-column>

      <el-table-column label="基线" width="170">
        <template #default="{ row }">
          <span v-if="row.baseline === null" class="suite__no-baseline">基线尚未写入</span>
          <span v-else class="suite__baseline">
            P0/P1 拦截率
            <span class="elt-mono">{{ formatRate(row.baseline.p0p1_block_rate) }}</span>
          </span>
        </template>
      </el-table-column>

      <el-table-column label="创建人" width="110">
        <template #default="{ row }">
          <span class="suite__meta">{{ textOrDash(row.created_by) }}</span>
        </template>
      </el-table-column>
    </QueryTable>

    <p class="suite__foot">
      基线只用于回答「这次比上次好还是差」，本身不参与门禁判定；门禁判定只看当前这次运行的指标。
    </p>
  </div>
</template>

<style scoped>
.suite {
  display: flex;
  flex-direction: column;
}

.suite__name {
  display: flex;
  gap: 6px;
  align-items: center;
  font-size: 13px;
  color: var(--elt-text-primary);
}

.suite__version {
  font-size: 11px;
  color: var(--elt-text-muted);
}

.suite__desc {
  margin-top: 2px;
  font-size: 12px;
  line-height: 1.6;
  color: var(--elt-text-muted);
}

.suite__no-baseline {
  font-size: 12px;
  color: var(--elt-warn);
}

.suite__baseline {
  font-size: 12px;
  color: var(--elt-text-secondary);
}

.suite__meta {
  font-size: 11px;
  color: var(--elt-text-muted);
}

.suite__foot {
  margin: 10px 0 0;
  font-size: 11px;
  line-height: 1.7;
  color: var(--elt-text-muted);
}
</style>
