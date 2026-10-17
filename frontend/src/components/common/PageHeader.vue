<script setup lang="ts">
/**
 * 页头（fe-01 §7）。
 *
 * 统一承载「页面叫什么 / 一句话说明 / 右侧主操作」，
 * 让 8 个页面在标题字号、描述色、操作区对齐上完全一致——
 * 各页各写一遍必然会在某页出现 16px 与 18px 混用。
 */
withDefaults(
  defineProps<{
    title: string
    description?: string
    /** 标题下方的补充信息区（如「本页统计口径」提示条） */
  }>(),
  { description: undefined },
)
</script>

<template>
  <header class="page-header">
    <div class="page-header__main">
      <div class="page-header__titles">
        <h1 class="page-header__title">{{ title }}</h1>
        <p v-if="description !== undefined && description !== ''" class="page-header__desc">
          {{ description }}
        </p>
      </div>
      <div v-if="$slots.actions" class="page-header__actions">
        <slot name="actions" />
      </div>
    </div>
    <div v-if="$slots.notice" class="page-header__notice">
      <slot name="notice" />
    </div>
    <div v-if="$slots.tabs" class="page-header__tabs">
      <slot name="tabs" />
    </div>
  </header>
</template>

<style scoped>
.page-header {
  display: flex;
  flex-direction: column;
  gap: 10px;
  margin-bottom: 16px;
}

.page-header__main {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
}

.page-header__titles {
  min-width: 0;
}

.page-header__title {
  margin: 0;
  font-size: 17px;
  font-weight: 600;
  line-height: 24px;
  color: var(--elt-text-primary);
}

.page-header__desc {
  margin: 3px 0 0;
  font-size: 12px;
  line-height: 18px;
  color: var(--elt-text-secondary);
}

.page-header__actions {
  display: flex;
  flex: none;
  align-items: center;
  gap: 8px;
}

.page-header__tabs {
  border-bottom: 1px solid var(--elt-border-subtle);
}
</style>
