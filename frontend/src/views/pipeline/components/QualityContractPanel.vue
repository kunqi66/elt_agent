<script setup lang="ts">
/**
 * 质量契约面板（fe-04 §5.1 右栏；契约本体见 fe-06）。
 *
 * 契约 = 「规则集合 + 拒绝率预算」。这里只做两件事：
 * 把将来会被冻结进 `quality_contract` 的内容摆清楚，
 * 并标注每条规则违规后是 reject（丢进错误表）还是 warn（放行但记证）。
 *
 * 拒绝率预算取自 Plan 的 `budget.max_reject_rate`——与运行监督用的是同一个值，
 * 不另算一份，避免「页面显示 1%，运行按 3% 中断」这种对不上的情况。
 */
import { computed } from 'vue'
import { SectionCard } from '@/components/common'
import { enumText } from '@/utils/enums'
import { formatRatioPercent } from '@/utils/format'
import type { QualityRule } from '@/types'

const props = defineProps<{
  rules: QualityRule[]
  rejectRateBudget: number | null
}>()

const enabledCount = computed<number>(() => props.rules.filter((r) => r.enabled).length)
const rejectCount = computed<number>(
  () => props.rules.filter((r) => r.enabled && r.on_violation === 'reject').length,
)
const warnCount = computed<number>(
  () => props.rules.filter((r) => r.enabled && r.on_violation === 'warn').length,
)
</script>

<template>
  <SectionCard title="质量契约" :padded="false" collapsible default-collapsed>
    <div class="qc__summary">
      <div class="qc__cell">
        <span class="qc__k">生效规则</span>
        <span class="qc__v">{{ enabledCount }} / {{ rules.length }}</span>
      </div>
      <div class="qc__cell">
        <span class="qc__k">超限丢弃</span>
        <span class="qc__v qc__v--danger">{{ rejectCount }}</span>
      </div>
      <div class="qc__cell">
        <span class="qc__k">仅告警</span>
        <span class="qc__v qc__v--warn">{{ warnCount }}</span>
      </div>
      <div class="qc__cell">
        <span class="qc__k">拒绝率预算</span>
        <span class="qc__v">{{ formatRatioPercent(rejectRateBudget) }}</span>
      </div>
    </div>

    <div v-if="rules.length === 0" class="qc__empty">尚无质量规则。</div>

    <div v-else class="qc__list">
      <div v-for="r in rules" :key="r.id" class="qc__item" :class="{ 'is-disabled': !r.enabled }">
        <span class="qc__item-field elt-mono">{{ r.field }}</span>
        <span class="qc__item-rule">{{ enumText('ErrorCodeLabel', r.error_code) }}</span>
        <el-tag
          size="small"
          effect="plain"
          :type="r.on_violation === 'reject' ? 'danger' : 'warning'"
        >
          {{ r.on_violation === 'reject' ? '丢弃' : '告警' }}
        </el-tag>
        <span v-if="!r.enabled" class="qc__item-off">未启用</span>
      </div>
    </div>
  </SectionCard>
</template>

<style scoped>
.qc__summary {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 6px 10px;
  padding: 8px 10px;
  border-bottom: 1px solid var(--elt-border-subtle);
}

.qc__cell {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.qc__k {
  font-size: 11px;
  color: var(--elt-text-muted);
}

.qc__v {
  font-size: 13px;
  font-weight: 600;
  color: var(--elt-text-primary);
}

.qc__v--danger {
  color: var(--elt-danger);
}

.qc__v--warn {
  color: var(--elt-warn);
}

.qc__list {
  max-height: 200px;
  overflow-y: auto;
}

.qc__item {
  display: flex;
  gap: 6px;
  align-items: center;
  padding: 4px 10px;
  font-size: 11px;
  color: var(--elt-text-secondary);
  border-bottom: 1px solid var(--elt-border-subtle);
}

.qc__item:last-child {
  border-bottom: none;
}

.qc__item.is-disabled {
  opacity: 0.55;
}

.qc__item-field {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.qc__item-rule {
  flex: none;
}

.qc__item-off {
  flex: none;
  color: var(--elt-text-muted);
}

.qc__empty {
  padding: 10px;
  font-size: 12px;
  color: var(--elt-text-muted);
}
</style>
