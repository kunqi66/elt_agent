<script setup lang="ts">
/**
 * 内容卡片（fe-01 §7）。
 *
 * 提供三件事，让各页面不必各写一遍：
 * 1. 标题 + 右上角 extra 插槽；
 * 2. `loading` 时的**块级骨架屏**（不是整页遮罩——fe-01 §7 禁止在已渲染内容上盖全屏 mask）；
 * 3. 可选折叠，用于 Studio 这种面板很多、需要用户自己收拢的页面。
 */
import { ref } from 'vue'
import { ArrowDown, ArrowRight } from '@element-plus/icons-vue'

const props = withDefaults(
  defineProps<{
    title?: string
    /** 块级加载态：只替换卡片体，不盖全屏遮罩 */
    loading?: boolean
    /** 折叠初始态；仅在 collapsible 时有意义 */
    collapsible?: boolean
    defaultCollapsed?: boolean
    /** 卡片体是否显示内边距；表格类内容通常自己贴边，传 false */
    padded?: boolean
    /** 骨架高度（px）；内容越高给越大，避免加载完页面跳动 */
    skeletonHeight?: number
  }>(),
  {
    title: undefined,
    loading: false,
    collapsible: false,
    defaultCollapsed: false,
    padded: true,
    skeletonHeight: 120,
  },
)

const collapsed = ref(props.defaultCollapsed)

function toggle(): void {
  if (props.collapsible) {
    collapsed.value = !collapsed.value
  }
}
</script>

<template>
  <section class="section-card" :class="{ 'section-card--padded': padded }">
    <header v-if="title !== undefined || $slots.extra" class="section-card__head">
      <button
        v-if="collapsible"
        type="button"
        class="section-card__toggle"
        :aria-expanded="!collapsed"
        @click="toggle"
      >
        <el-icon :size="12">
          <ArrowDown v-if="!collapsed" />
          <ArrowRight v-else />
        </el-icon>
      </button>
      <h2 v-if="title !== undefined" class="section-card__title" :class="{ 'is-clickable': collapsible }" @click="toggle">
        {{ title }}
      </h2>
      <div class="section-card__extra">
        <slot name="extra" />
      </div>
    </header>

    <div v-show="!collapsed" class="section-card__body">
      <div v-if="loading" class="elt-skeleton elt-skeleton--block" :style="{ height: `${skeletonHeight}px` }" />
      <slot v-else />
    </div>
  </section>
</template>

<style scoped>
.section-card {
  display: flex;
  flex-direction: column;
  overflow: hidden;
  background-color: var(--elt-bg-surface);
  border: 1px solid var(--elt-border-subtle);
  border-radius: var(--elt-radius-card);
}

.section-card__head {
  display: flex;
  align-items: center;
  gap: 6px;
  min-height: 40px;
  padding: 0 14px;
  border-bottom: 1px solid var(--elt-border-subtle);
}

.section-card__toggle {
  display: inline-flex;
  padding: 0;
  color: var(--elt-text-muted);
  cursor: pointer;
  background: none;
  border: none;
}

.section-card__toggle:hover {
  color: var(--elt-text-primary);
}

.section-card__title {
  margin: 0;
  font-size: 13px;
  font-weight: 600;
  line-height: 20px;
  color: var(--elt-text-primary);
}

.section-card__title.is-clickable {
  cursor: pointer;
}

.section-card__extra {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-left: auto;
}

.section-card__body {
  display: flex;
  flex-direction: column;
}

.section-card--padded .section-card__body {
  padding: 14px;
}
</style>
