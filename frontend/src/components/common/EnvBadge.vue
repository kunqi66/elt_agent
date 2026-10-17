<script setup lang="ts">
/**
 * 环境标识（fe-01 §7）。
 *
 * prod 用**实心红底**而不是普通 tag：环境误判是这类平台最贵的错误，
 * 一个「你以为在 dev、其实在 prod」的误操作会直接写生产表。
 * 因此 prod 在视觉上必须一眼可辨，不能和 staging 用同一套浅色。
 */
import { computed } from 'vue'
import { enumText } from '@/utils/enums'
import type { Env } from '@/types'

const props = defineProps<{ env: Env | null | undefined }>()

const safeEnv = computed<Env>(() => props.env ?? 'dev')
const label = computed<string>(() => enumText('Env', safeEnv.value))
</script>

<template>
  <span class="env-badge" :class="`env-badge--${safeEnv}`">
    <span class="env-badge__dot" aria-hidden="true" />
    <span class="env-badge__text">{{ label }}</span>
  </span>
</template>

<style scoped>
.env-badge {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 1px 7px;
  border: 1px solid var(--elt-border-subtle);
  border-radius: 999px;
  font-size: 11px;
  font-weight: 600;
  line-height: 16px;
  white-space: nowrap;
}

.env-badge__dot {
  width: 5px;
  height: 5px;
  border-radius: 50%;
  background-color: currentcolor;
}

.env-badge--dev {
  color: var(--elt-text-secondary);
  background-color: var(--elt-bg-elevated);
}

.env-badge--staging {
  color: var(--elt-warn);
  border-color: color-mix(in srgb, var(--elt-warn) 45%, transparent);
  background-color: var(--elt-badge-warn);
}

.env-badge--prod {
  color: var(--elt-text-primary);
  border-color: color-mix(in srgb, var(--elt-danger) 60%, transparent);
  background-color: var(--elt-danger);
}

/* prod 常驻脉动：静止的红色在满屏暗色里仍会「看习惯了」 */
.env-badge--prod .env-badge__dot {
  animation: env-pulse 1.6s ease-in-out infinite;
}

@keyframes env-pulse {
  0%,
  100% {
    opacity: 1;
  }

  50% {
    opacity: 0.35;
  }
}

@media (prefers-reduced-motion: reduce) {
  .env-badge--prod .env-badge__dot {
    animation: none;
  }
}
</style>
