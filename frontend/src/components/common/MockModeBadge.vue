<script setup lang="ts">
/**
 * dev mock 标识（只在本机开发出现，生产包里恒不渲染）。
 *
 * **为什么要有它**：`.env.development` 默认 `VITE_USE_MOCK=true`，此时
 * `src/api/mock` 会用一个 axios 适配器接管**全部** `/api/v1` 请求
 * （见 `src/api/mock/index.ts`），页面看起来完全正常，但**数据一条都不是后端的**。
 * 演示时把 mock 数据当成真实结果讲，是这类平台最贵的一种错——
 * 所以 mock 状态必须像 `EnvBadge` 的 prod 红条一样**一眼可辨**，而不是靠记配置。
 *
 * 判据是编译期字面量：Vite 会把 `import.meta.env.VITE_USE_MOCK` 直接替换成
 * `"true"` / `"false"`，生产构建里条件恒假，这个角标与 mock 模块一起被摇掉，
 * mock 数据绝不会进生产包。
 */
const isMock = import.meta.env.VITE_USE_MOCK === 'true'
</script>

<template>
  <div v-if="isMock" class="mock-badge" role="status">
    <span class="mock-badge__dot" aria-hidden="true" />
    DEV MOCK —— 页面数据来自前端假数据，不是后端
  </div>
</template>

<style scoped>
.mock-badge {
  position: fixed;
  right: 12px;
  bottom: 12px;
  z-index: 3000;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 5px 10px;
  border: 1px solid color-mix(in srgb, var(--elt-warn) 55%, transparent);
  border-radius: 6px;
  color: var(--elt-warn);
  background-color: var(--elt-badge-warn);
  font-size: 11px;
  font-weight: 600;
  line-height: 16px;
  letter-spacing: 0.02em;
  pointer-events: none;
}
</style>
