<script setup lang="ts">
/**
 * 主布局（fe-01 §3）。
 *
 * 结构：生产红条 → [侧边栏 | (顶栏 + 内容区)]，窄屏时侧边栏改为抽屉。
 *
 * `<router-view>` 用 `:key="route.path"` 而不是 `fullPath`：
 * - 用 `fullPath` 的话，每次改筛选条件（写进 query）都会重建整个页面，
 *   用户的滚动位置、表格展开态全丢；
 * - 用 `path` 则只在换页面/换 `:id` 时重建，正好解决
 *   「同一个详情组件在 id 变化时不重新 mounted」的问题。
 */
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import { useAppStore } from '@/stores/app'
import AppHeader from './components/AppHeader.vue'
import AppSidebar from './components/AppSidebar.vue'
import ProdBanner from './components/ProdBanner.vue'

const route = useRoute()
const appStore = useAppStore()

const drawerOpen = computed<boolean>(() => appStore.mobileDrawerOpen)
</script>

<template>
  <div class="layout">
    <ProdBanner />

    <div class="layout__body">
      <AppSidebar class="layout__sidebar" />

      <div class="layout__main">
        <AppHeader />
        <main class="layout__content elt-scroll">
          <router-view v-slot="{ Component }">
            <component :is="Component" :key="route.path" />
          </router-view>
        </main>
      </div>
    </div>

    <!-- 窄屏抽屉：与常驻侧边栏共用 AppSidebar，避免两份菜单各自漂移 -->
    <el-drawer
      :model-value="drawerOpen"
      direction="ltr"
      size="240px"
      :with-header="false"
      class="layout__drawer"
      @update:model-value="(v: boolean) => appStore.setMobileDrawer(v)"
    >
      <AppSidebar class="layout__drawer-sidebar" @click="appStore.setMobileDrawer(false)" />
    </el-drawer>
  </div>
</template>

<style scoped>
.layout {
  display: flex;
  flex-direction: column;
  height: 100%;
  overflow: hidden;
}

.layout__body {
  display: flex;
  flex: 1;
  min-height: 0;
}

.layout__main {
  display: flex;
  flex: 1;
  flex-direction: column;
  min-width: 0;
}

.layout__content {
  flex: 1;
  min-height: 0;
  padding: var(--elt-page-padding);
  overflow-y: auto;
  background-color: var(--elt-bg-app);
}

.layout__drawer-sidebar {
  height: 100%;
}

@media (max-width: 900px) {
  .layout__sidebar {
    display: none;
  }
}
</style>

<style>
/* 抽屉内的侧边栏需要撑满，且去掉圆角与内边距 */
.layout__drawer .el-drawer__body {
  padding: 0;
}
</style>
