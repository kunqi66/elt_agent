<script setup lang="ts">
/**
 * 运行状态徽章（fe-06）。
 *
 * 在 StatusTag 之上加两件事：
 * 1. **进行中的状态带脉动点**——运行中心列表里最怕的是「以为跑完了其实是还在跑」，
 *    静止的蓝标签和已完成的绿标签在扫视时容易混。
 * 2. **`interrupted` 用专属措辞**：「监督硬中断」而不是「已中断」。
 *    这是平台的主动保护动作，不是故障；文案混同会让人误判为系统不稳定。
 */
import { computed } from 'vue'
import { enumLabel } from '@/utils/enums'
import type { RunStatus } from '@/types'

const props = withDefaults(
  defineProps<{
    status: RunStatus | null | undefined
    /** 是否显示脉动点；密集表格里可关掉以减少视觉噪声 */
    animated?: boolean
  }>(),
  { animated: true },
)

const resolved = computed(() => enumLabel('RunStatus', props.status))

/** 进行中：这几态会自行变化，用脉动提示「还在动」 */
const isActive = computed<boolean>(() =>
  props.status === 'running' || props.status === 'queued' || props.status === 'rolling_back',
)
</script>

<template>
  <el-tooltip
    :disabled="!resolved.unknown"
    :content="`未知取值「${resolved.label}」，后端可能已升级`"
    placement="top"
  >
    <span class="run-status" :class="`run-status--${resolved.tag}`">
      <span v-if="animated && isActive" class="run-status__dot" aria-hidden="true" />
      <span>{{ resolved.label }}</span>
    </span>
  </el-tooltip>
</template>

<style scoped>
.run-status {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 1px 7px;
  border: 1px solid transparent;
  border-radius: var(--elt-radius-pill);
  font-size: 11px;
  font-weight: 600;
  line-height: 16px;
  white-space: nowrap;
}

.run-status__dot {
  width: 5px;
  height: 5px;
  border-radius: 50%;
  background-color: currentcolor;
  animation: run-pulse 1.4s ease-in-out infinite;
}

.run-status--primary {
  color: var(--elt-accent);
  border-color: color-mix(in srgb, var(--elt-accent) 45%, transparent);
  background-color: var(--elt-badge-accent);
}

.run-status--success {
  color: var(--elt-success);
  border-color: color-mix(in srgb, var(--elt-success) 45%, transparent);
  background-color: var(--elt-badge-success);
}

.run-status--warning {
  color: var(--elt-warn);
  border-color: color-mix(in srgb, var(--elt-warn) 45%, transparent);
  background-color: var(--elt-badge-warn);
}

.run-status--danger {
  color: var(--elt-danger);
  border-color: color-mix(in srgb, var(--elt-danger) 45%, transparent);
  background-color: var(--elt-badge-danger);
}

.run-status--info {
  color: var(--elt-text-secondary);
  border-color: var(--elt-border-subtle);
  background-color: var(--elt-bg-elevated);
}

@keyframes run-pulse {
  0%,
  100% {
    opacity: 1;
    transform: scale(1);
  }

  50% {
    opacity: 0.4;
    transform: scale(0.8);
  }
}

@media (prefers-reduced-motion: reduce) {
  .run-status__dot {
    animation: none;
  }
}
</style>
