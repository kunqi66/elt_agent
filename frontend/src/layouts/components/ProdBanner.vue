<script setup lang="ts">
/**
 * 生产环境常驻红条（fe-01 §7）。
 *
 * 只在当前项目 env === 'prod' 时出现，且**不可关闭**——
 * 一个能被点掉的警告条，在真正需要它的那一刻一定已经被点掉了。
 *
 * 文案刻意说「写操作会走 Capability」，而不是「小心操作」：
 * 前者告诉用户系统有保护、哪些动作会被拦，后者只是制造焦虑。
 */
import { computed } from 'vue'
import { useProjectStore } from '@/stores/project'

const projectStore = useProjectStore()

const projectName = computed<string>(() => projectStore.currentProject?.name ?? '当前项目')
</script>

<template>
  <div v-if="projectStore.isProd" class="prod-banner" role="status">
    <span class="prod-banner__dot" aria-hidden="true" />
    <strong class="prod-banner__label">生产环境</strong>
    <span class="prod-banner__sep">·</span>
    <span class="prod-banner__text">
      {{ projectName }} 的发布与回滚均需 Capability 授权并写入证据账本
    </span>
  </div>
</template>

<style scoped>
.prod-banner {
  display: flex;
  flex: none;
  align-items: center;
  gap: 8px;
  height: var(--elt-prodbar-height);
  padding: 0 var(--elt-page-padding);
  font-size: 12px;
  color: #fff;
  background-color: var(--elt-danger);
}

.prod-banner__dot {
  width: 7px;
  height: 7px;
  background-color: #fff;
  border-radius: 50%;
  animation: prod-pulse 1.6s ease-in-out infinite;
}

.prod-banner__label {
  font-weight: 700;
  letter-spacing: 0.5px;
}

.prod-banner__sep {
  opacity: 0.6;
}

.prod-banner__text {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  opacity: 0.95;
}

@keyframes prod-pulse {
  0%,
  100% {
    opacity: 1;
  }

  50% {
    opacity: 0.3;
  }
}

@media (prefers-reduced-motion: reduce) {
  .prod-banner__dot {
    animation: none;
  }
}
</style>
