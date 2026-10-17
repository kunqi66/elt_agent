<script setup lang="ts">
/**
 * 面包屑（fe-01 §5.3 第 5 条）。
 *
 * 数据来源是 `meta.breadcrumb` + `meta.title`，**不解析 URL 段**：
 * `/datasources/12/edit` 里的 `12` 拿去当面包屑只会显示成「12」，
 * 而用户认的是数据源名字。名字由页面自己写在 meta 或由页面插槽覆盖。
 */
import { computed } from 'vue'
import { useRoute } from 'vue-router'

const route = useRoute()

interface Crumb {
  title: string
  name?: string
}

const crumbs = computed<Crumb[]>(() => {
  const parents = route.meta.breadcrumb ?? []
  const current = route.meta.title
  const list: Crumb[] = parents.map((p) => ({ title: p.title, name: p.name }))
  if (current !== undefined) {
    list.push({ title: current })
  }
  return list
})
</script>

<template>
  <el-breadcrumb v-if="crumbs.length > 1" class="crumbs" separator="/">
    <el-breadcrumb-item v-for="(crumb, index) in crumbs" :key="`${crumb.title}-${index}`">
      <router-link v-if="crumb.name !== undefined && index < crumbs.length - 1" :to="{ name: crumb.name }">
        {{ crumb.title }}
      </router-link>
      <span v-else>{{ crumb.title }}</span>
    </el-breadcrumb-item>
  </el-breadcrumb>
</template>

<style scoped>
.crumbs {
  font-size: 12px;
  line-height: 20px;
}

.crumbs :deep(.el-breadcrumb__inner) {
  color: var(--elt-text-muted);
  font-weight: 400;
}

.crumbs :deep(.el-breadcrumb__inner.is-link:hover) {
  color: var(--elt-accent);
}

.crumbs :deep(.el-breadcrumb__item:last-child .el-breadcrumb__inner) {
  color: var(--elt-text-secondary);
  font-weight: 500;
}
</style>
