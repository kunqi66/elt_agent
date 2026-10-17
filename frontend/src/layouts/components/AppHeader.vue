<script setup lang="ts">
/**
 * 顶栏（fe-01 §7）。
 *
 * 三件事：面包屑、项目切换、用户菜单。
 * 外加一个 `isBusy` 指示器：它是 app store 里的在途请求计数，
 * 用来提示「你刚点的那个写操作还在跑」——
 * 审批与发布这类动作耗时可能到分钟级，没有这个提示用户会重复点击。
 */
import { computed } from 'vue'
import { Loading, Menu as MenuIcon } from '@element-plus/icons-vue'
import { useAppStore } from '@/stores/app'
import AppBreadcrumbs from './Breadcrumbs.vue'
import ProjectSwitcher from './ProjectSwitcher.vue'
import UserMenu from './UserMenu.vue'

const appStore = useAppStore()

const busy = computed<boolean>(() => appStore.isBusy)
</script>

<template>
  <header class="app-header">
    <el-button
      class="app-header__burger"
      link
      :icon="MenuIcon"
      aria-label="打开菜单"
      @click="appStore.setMobileDrawer(true)"
    />
    <AppBreadcrumbs class="app-header__crumbs" />

    <div class="app-header__right">
      <span v-if="busy" class="app-header__busy" title="有操作正在处理">
        <el-icon class="is-loading" :size="13"><Loading /></el-icon>
        <span>处理中</span>
      </span>
      <ProjectSwitcher />
      <UserMenu />
    </div>
  </header>
</template>

<style scoped>
.app-header {
  display: flex;
  flex: none;
  align-items: center;
  gap: 12px;
  height: var(--elt-navbar-height);
  padding: 0 var(--elt-page-padding);
  background-color: var(--elt-bg-surface);
  border-bottom: 1px solid var(--elt-border-subtle);
}

/* 宽屏隐藏：侧边栏常驻，汉堡键是多余的 */
.app-header__burger {
  display: none;
}

.app-header__crumbs {
  min-width: 0;
  overflow: hidden;
}

.app-header__right {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-left: auto;
}

.app-header__busy {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 2px 8px;
  font-size: 11px;
  color: var(--elt-accent);
  background-color: var(--elt-badge-accent);
  border-radius: var(--elt-radius-pill);
}

@media (max-width: 900px) {
  .app-header__burger {
    display: inline-flex;
  }
}
</style>
