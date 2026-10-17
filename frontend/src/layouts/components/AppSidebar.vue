<script setup lang="ts">
/**
 * 侧边导航（fe-01 §7）。
 *
 * 图标用**白名单映射**而不是 `import * as Icons`：
 * 后者会把 Element Plus 全部图标打进首屏包（约 200+ 个组件），
 * 而这里实际只用到 8 个。未登记的键回落到默认图标，不会崩。
 */
import { computed, watch } from 'vue'
import { useRoute } from 'vue-router'
import type { Component } from 'vue'
import {
  Coin,
  DataBoard,
  Fold,
  Lock,
  Monitor,
  Expand,
  Setting,
  Share,
  Stamp,
} from '@element-plus/icons-vue'
import { useAppStore } from '@/stores/app'
import { useProjectStore } from '@/stores/project'
import { usePreparationStore } from '@/stores/preparation'
import { resolveActiveMenu, visibleNavGroups } from '@/router/nav'
import { hasAnyRole } from '@/utils/enums'
import type { RoleName } from '@/types'

const route = useRoute()
const appStore = useAppStore()
const projectStore = useProjectStore()
const preparationStore = usePreparationStore()

/** 能进审批中心的人才需要这个徽标；其余角色拉了也是白拉 */
const APPROVER_ROLES: RoleName[] = ['checker_data', 'checker_security', 'auditor']
const canSeeApprovals = computed<boolean>(() => hasAnyRole(projectStore.myRoles, APPROVER_ROLES))

// 依赖有两个：角色要到 `/auth/me` 回来才就绪（刷新页面时是空的），
// 切项目也会换掉整个待办集合。任一变化都重新拉一次。
watch(
  () => [projectStore.currentProjectId, canSeeApprovals.value] as const,
  () => {
    if (canSeeApprovals.value) {
      void preparationStore.refreshTodoCount()
    }
  },
  { immediate: true },
)

/** 待办徽标只在审批中心那一项上出现 */
const todoCount = computed<number>(() => (canSeeApprovals.value ? preparationStore.myTodoCount : 0))

const ICON_MAP: Record<string, Component> = {
  dashboard: DataBoard,
  datasource: Coin,
  pipeline: Share,
  approval: Stamp,
  run: Monitor,
  policy: Lock,
  settings: Setting,
}

function iconOf(key: string): Component {
  return ICON_MAP[key] ?? DataBoard
}

const groups = computed(() => visibleNavGroups(projectStore.myRoles))

const activeName = computed<string>(() =>
  resolveActiveMenu(typeof route.name === 'string' ? route.name : null, route.meta.activeMenu),
)

const collapsed = computed<boolean>(() => appStore.sidebarCollapsed)
</script>

<template>
  <aside class="sidebar" :class="{ 'sidebar--collapsed': collapsed }">
    <div class="sidebar__brand">
      <span class="sidebar__logo">ELT</span>
      <span v-if="!collapsed" class="sidebar__brand-text">Agent 控制台</span>
    </div>

    <nav class="sidebar__nav elt-scroll">
      <div v-for="group in groups" :key="group.key" class="sidebar__group">
        <p v-if="!collapsed" class="sidebar__group-title">{{ group.title }}</p>
        <router-link
          v-for="item in group.items"
          :key="item.name"
          class="sidebar__item"
          :class="{ 'is-active': activeName === item.name, 'is-disabled': item.disabled }"
          :to="item.disabled ? '' : { name: item.name }"
          :title="collapsed ? item.title : undefined"
          @click="item.disabled && $event.preventDefault()"
        >
          <el-icon class="sidebar__icon" :size="16"><component :is="iconOf(item.icon)" /></el-icon>
          <span v-if="!collapsed" class="sidebar__label elt-ellipsis">{{ item.title }}</span>
          <span
            v-if="item.name === 'preparation-list' && todoCount > 0"
            class="sidebar__badge"
            :class="{ 'sidebar__badge--dot': collapsed }"
            :title="collapsed ? `${todoCount} 件待我审批` : undefined"
          >
            {{ collapsed ? '' : todoCount }}
          </span>
        </router-link>
      </div>
    </nav>

    <button type="button" class="sidebar__collapse" @click="appStore.toggleSidebar()">
      <el-icon :size="14">
        <Expand v-if="collapsed" />
        <Fold v-else />
      </el-icon>
      <span v-if="!collapsed">收起</span>
    </button>
  </aside>
</template>

<style scoped>
.sidebar {
  display: flex;
  flex: none;
  flex-direction: column;
  width: var(--elt-sidebar-width);
  background-color: var(--elt-bg-surface);
  border-right: 1px solid var(--elt-border-subtle);
  transition: width 0.18s ease;
}

.sidebar--collapsed {
  width: 56px;
}

.sidebar__brand {
  display: flex;
  flex: none;
  align-items: center;
  gap: 9px;
  height: var(--elt-navbar-height);
  padding: 0 14px;
  border-bottom: 1px solid var(--elt-border-subtle);
}

.sidebar__logo {
  display: inline-flex;
  flex: none;
  align-items: center;
  justify-content: center;
  width: 26px;
  height: 26px;
  font-size: 10px;
  font-weight: 800;
  color: #fff;
  letter-spacing: 0.5px;
  background-color: var(--elt-accent);
  border-radius: 7px;
}

.sidebar__brand-text {
  font-size: 13px;
  font-weight: 600;
  color: var(--elt-text-primary);
  white-space: nowrap;
}

.sidebar__nav {
  flex: 1;
  padding: 10px 8px;
  overflow-y: auto;
}

.sidebar__group + .sidebar__group {
  margin-top: 12px;
}

.sidebar__group-title {
  margin: 0 0 4px;
  padding: 0 8px;
  font-size: 10px;
  font-weight: 600;
  color: var(--elt-text-muted);
  letter-spacing: 0.6px;
  text-transform: uppercase;
}

.sidebar__item {
  position: relative;
  display: flex;
  align-items: center;
  gap: 9px;
  height: 32px;
  padding: 0 8px;
  margin-bottom: 2px;
  font-size: 12px;
  color: var(--elt-text-secondary);
  text-decoration: none;
  border-radius: var(--elt-radius-control);
}

.sidebar__item:hover {
  color: var(--elt-text-primary);
  text-decoration: none;
  background-color: var(--elt-bg-elevated);
}

.sidebar__item.is-active {
  color: #fff;
  background-color: var(--elt-accent-soft);
}

.sidebar__item.is-disabled {
  cursor: not-allowed;
  opacity: 0.45;
}

.sidebar__icon {
  flex: none;
}

.sidebar__label {
  min-width: 0;
}

/* 待办徽标：收起时退化成一个点，避免在 56px 宽里挤成乱码 */
.sidebar__badge {
  display: inline-flex;
  flex: none;
  align-items: center;
  justify-content: center;
  min-width: 18px;
  height: 16px;
  padding: 0 5px;
  margin-left: auto;
  font-size: 10px;
  font-weight: 700;
  line-height: 1;
  color: #fff;
  background-color: var(--elt-danger);
  border-radius: var(--elt-radius-pill);
}

.sidebar__badge--dot {
  position: absolute;
  top: 6px;
  right: 8px;
  min-width: 7px;
  width: 7px;
  height: 7px;
  padding: 0;
  margin: 0;
}

.sidebar__collapse {
  display: flex;
  flex: none;
  align-items: center;
  justify-content: center;
  gap: 6px;
  height: 36px;
  font-size: 12px;
  color: var(--elt-text-muted);
  cursor: pointer;
  background: none;
  border: none;
  border-top: 1px solid var(--elt-border-subtle);
}

.sidebar__collapse:hover {
  color: var(--elt-text-primary);
  background-color: var(--elt-bg-elevated);
}
</style>
