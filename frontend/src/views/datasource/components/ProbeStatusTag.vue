<script setup lang="ts">
/**
 * ProbeStatus 徽章 + 过期提示（fe-03 §6.2 / §5.4）。
 *
 * 为什么单独成组件：列表「最近探查」列、版本列表、详情摘要三处都要显示探查状态，
 * 而「已过期」在业务上不是普通状态——它意味着该 Profile 不能用于生成方案
 * （后端会返回 METADATA_PROFILE_EXPIRED）。把这条提示绑在徽章上，
 * 才不会出现「某个列表只标黄、不说要重查」的漏项。
 */
import { computed } from 'vue'
import { StatusTag } from '@/components/common'
import { formatRemaining, remainingMs } from '@/utils/format'
import type { ProbeStatus } from '@/types'

const props = withDefaults(
  defineProps<{
    status: ProbeStatus | null | undefined
    /** 有 expires_at 时给出剩余有效期提示；null 表示该版本不过期 */
    expiresAt?: string | null
    size?: 'small' | 'default' | 'large'
  }>(),
  { expiresAt: null, size: 'small' },
)

/** status 还是 ready 但 expires_at 已过：用时间兜底，避免状态刷新滞后时误用旧 Profile */
const expiredByTime = computed<boolean>(() => {
  const ms = remainingMs(props.expiresAt)
  return ms !== null && ms <= 0
})

const needsReprobe = computed<boolean>(
  () => props.status === 'expired' || (props.status === 'ready' && expiredByTime.value),
)

const tip = computed<string>(() => {
  if (needsReprobe.value) {
    return '元数据已过期，需重新探查后才能用于生成方案（METADATA_PROFILE_EXPIRED）'
  }
  if (props.status === 'probing') {
    return '探查进行中，结果就绪后自动刷新'
  }
  const ms = remainingMs(props.expiresAt)
  if (props.status === 'ready' && ms !== null) {
    return `剩余有效期：${formatRemaining(ms)}`
  }
  return ''
})
</script>

<template>
  <!-- 用原生 title 而不是 el-tooltip：StatusTag 内部已经是一个 tooltip，嵌套会互相吞事件 -->
  <span class="probe-status-tag" :title="tip">
    <StatusTag kind="ProbeStatus" :value="status" :size="size" />
    <span v-if="needsReprobe" class="probe-status-tag__hint">需重新探查</span>
  </span>
</template>

<style scoped>
.probe-status-tag {
  display: inline-flex;
  gap: 6px;
  align-items: center;
}

.probe-status-tag__hint {
  font-size: 11px;
  color: var(--elt-warn);
  white-space: nowrap;
}
</style>
