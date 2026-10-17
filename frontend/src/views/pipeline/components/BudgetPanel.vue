<script setup lang="ts">
/**
 * 运行预算面板（fe-04 §5.1 右栏 / §6.5）。
 *
 * 六项预算直接来自 Plan 的 `budget`，也就是将来写进
 * `runtime_supervision_contract` 的那份——它决定运行期何时告警、何时硬中断。
 * 因此这里**不**做单位换算之外的任何加工，也不标「合理/不合理」：
 * 是否超标是门禁与监督器的结论，不是前端的观感。
 */
import { computed } from 'vue'
import { SectionCard } from '@/components/common'
import { DASH, formatAmplification, formatBytes, formatDuration, formatRatioPercent, formatRows } from '@/utils/format'
import type { RuntimeBudget } from '@/types'

const props = defineProps<{ budget: RuntimeBudget | null }>()

interface Row {
  key: string
  label: string
  value: string
  hint: string
}

const rows = computed<Row[]>(() => {
  const b = props.budget
  if (b === null) {
    return []
  }
  return [
    {
      key: 'max_read_rows',
      label: '最大读取行数',
      value: formatRows(b.max_read_rows),
      hint: '超过即硬中断，防止全表扫描把源库拖垮',
    },
    {
      key: 'max_write_bytes',
      label: '最大写入字节',
      value: formatBytes(b.max_write_bytes),
      hint: '写入量上限，防止目标端被写爆',
    },
    {
      key: 'max_duration_sec',
      label: '最长运行时长',
      value: formatDuration(b.max_duration_sec),
      hint: '超时后由监督器中断，不会无声挂住',
    },
    {
      key: 'max_amplification_ratio',
      label: '最大放大比',
      value: formatAmplification(b.max_amplification_ratio),
      hint: '写出/读入 的比值上限，识别笛卡尔积式膨胀',
    },
    {
      key: 'max_reject_rate',
      label: '最大拒绝率',
      value: formatRatioPercent(b.max_reject_rate),
      hint: '质量分流后脏数据占比上限，超限触发硬中断',
    },
    {
      key: 'warn_threshold_pct',
      label: '告警阈值',
      value: `${b.warn_threshold_pct}%`,
      hint: '达到该百分比即先告警，给人工介入留时间',
    },
  ]
})
</script>

<template>
  <SectionCard title="运行预算" :padded="false" collapsible default-collapsed>
    <div v-if="budget === null" class="budget__empty">{{ DASH }} 尚无预算（Plan 未产出）</div>
    <div v-else class="budget">
      <div v-for="r in rows" :key="r.key" class="budget__row">
        <span class="budget__label">{{ r.label }}</span>
        <span class="budget__value elt-mono">{{ r.value }}</span>
        <span class="budget__hint">{{ r.hint }}</span>
      </div>
    </div>
  </SectionCard>
</template>

<style scoped>
.budget {
  display: flex;
  flex-direction: column;
}

.budget__row {
  display: grid;
  grid-template-columns: 1fr auto;
  gap: 2px 8px;
  padding: 5px 10px;
  border-bottom: 1px solid var(--elt-border-subtle);
}

.budget__row:last-child {
  border-bottom: none;
}

.budget__label {
  font-size: 12px;
  color: var(--elt-text-secondary);
}

.budget__value {
  font-size: 12px;
  font-weight: 600;
  color: var(--elt-text-primary);
}

.budget__hint {
  grid-column: 1 / -1;
  font-size: 11px;
  line-height: 15px;
  color: var(--elt-text-muted);
}

.budget__empty {
  padding: 10px;
  font-size: 12px;
  color: var(--elt-text-muted);
}
</style>
