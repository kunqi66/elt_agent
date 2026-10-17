<script setup lang="ts">
/**
 * 预算消耗面板：把 `runtime_supervision_contract` 的 5 个受监督量各画一条
 * `BudgetProgress`（fe-06 §6）。
 *
 * 为什么把「当前值」从 `run` 里取而不是从指标点取：
 * `execution_run` 上的 read_rows / written_bytes 是后端写库的终值，
 * 指标点只是采样；用采样值会让进度条在两次采样之间「倒退」或跳动，
 * 而预算条最怕的就是数字反复变化让人以为算错了。
 */
import { computed } from 'vue'
import BudgetProgress from './BudgetProgress.vue'
import { SectionCard } from '@/components/common'
import type { RunDetail } from '@/types'

const props = defineProps<{ run: RunDetail }>()

interface BudgetRow {
  key: string
  label: string
  current: number
  limit: number
  unit: 'rows' | 'bytes' | 'sec' | 'ratio'
}

const rows = computed<BudgetRow[]>(() => [
  {
    key: 'read_rows',
    label: '读取行数',
    current: props.run.read_rows,
    limit: props.run.budget.max_read_rows,
    unit: 'rows',
  },
  {
    key: 'write_bytes',
    label: '写入字节',
    current: props.run.written_bytes,
    limit: props.run.budget.max_write_bytes,
    unit: 'bytes',
  },
  {
    key: 'duration',
    label: '运行时长',
    current: props.run.duration_sec,
    limit: props.run.budget.max_duration_sec,
    unit: 'sec',
  },
  {
    key: 'amplification',
    label: '放大比',
    current: props.run.amplification_ratio,
    limit: props.run.budget.max_amplification_ratio,
    unit: 'ratio',
  },
  {
    key: 'reject_rate',
    label: '拒绝率',
    current: props.run.reject_rate,
    limit: props.run.budget.max_reject_rate,
    unit: 'ratio',
  },
])
</script>

<template>
  <SectionCard title="预算消耗" :padded="true">
    <template #extra>
      <span class="budget-panel__hint">
        预警线 {{ run.budget.warn_threshold_pct }}% · 越限即触发监督硬中断
      </span>
    </template>

    <div class="budget-panel__rows">
      <BudgetProgress
        v-for="row in rows"
        :key="row.key"
        :label="row.label"
        :current="row.current"
        :limit="row.limit"
        :unit="row.unit"
        :warn_threshold_pct="run.budget.warn_threshold_pct"
      />
    </div>
  </SectionCard>
</template>

<style scoped>
.budget-panel__hint {
  font-size: 11px;
  color: var(--elt-text-muted);
}

.budget-panel__rows {
  display: flex;
  flex-direction: column;
  gap: 14px;
}
</style>
