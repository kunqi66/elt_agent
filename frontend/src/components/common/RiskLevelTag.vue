<script setup lang="ts">
/**
 * 风险等级徽章（P0–P3）。
 *
 * 不复用 StatusTag 的 Element tag 配色：P1 与 P2 在 Element 里都会落到
 * danger/warning，而原型图给了它们**各自专属的橙（#f97316）与黄（#eab308）**。
 * 风险等级是 PDP 的确定性结论，配色必须能与「运行失败红」区分开，
 * 否则一张列表里 P1 和「失败」会长得一样。
 *
 * P0 是「已被阻断」，不是「高风险」——文案上必须让用户看出它不会被放行。
 */
import { computed } from 'vue'
import { enumText } from '@/utils/enums'
import type { RiskLevel, RiskReason } from '@/types'

const props = withDefaults(
  defineProps<{
    level: RiskLevel | null | undefined
    /** 命中原因；给了就在悬停时列出，让用户不必跳详情就知道为什么是 P1 */
    reasons?: RiskReason[]
    size?: 'small' | 'default'
  }>(),
  { reasons: () => [], size: 'small' },
)

const label = computed<string>(() => enumText('RiskLevel', props.level))
const levelClass = computed<string>(() => `risk-tag--${props.level ?? 'unknown'}`)

const tooltip = computed<string | null>(() => {
  if (props.reasons.length === 0) {
    return null
  }
  return props.reasons.map((r) => `${r.code} ${r.title}`).join('\n')
})

/** 无原因时不挂 tooltip，避免出现空浮层 */
const hasReasons = computed<boolean>(() => props.reasons.length > 0)
</script>

<template>
  <el-tooltip :disabled="!hasReasons" placement="top" :content="tooltip ?? ''">
    <span class="risk-tag" :class="[levelClass, `risk-tag--${size}`]">
      {{ label }}
    </span>
  </el-tooltip>
</template>

<style scoped>
.risk-tag {
  display: inline-flex;
  align-items: center;
  padding: 1px 7px;
  border: 1px solid transparent;
  border-radius: var(--elt-radius-pill);
  font-size: 11px;
  font-weight: 700;
  line-height: 16px;
  white-space: nowrap;
}

.risk-tag--default {
  padding: 2px 10px;
  font-size: 12px;
}

.risk-tag--P0 {
  color: #fff;
  border-color: var(--elt-danger);
  background-color: var(--elt-danger);
}

.risk-tag--P1 {
  color: var(--elt-risk-p1);
  border-color: color-mix(in srgb, var(--elt-risk-p1) 50%, transparent);
  background-color: var(--elt-badge-p1);
}

.risk-tag--P2 {
  color: var(--elt-risk-p2);
  border-color: color-mix(in srgb, var(--elt-risk-p2) 50%, transparent);
  background-color: var(--elt-badge-p2);
}

.risk-tag--P3 {
  color: var(--elt-text-secondary);
  border-color: var(--elt-border-subtle);
  background-color: var(--elt-bg-elevated);
}

.risk-tag--fallback {
  color: var(--elt-text-muted);
  border-color: var(--elt-border-subtle);
  background-color: var(--elt-bg-elevated);
}
</style>
