<script setup lang="ts">
/**
 * 项目切换器（fe-01 §3）。
 *
 * 切换项目会改变**几乎所有列表的数据范围**，因此：
 * - 每项都显示环境徽标——选错环境是这类平台最贵的错误；
 * - 切换后由各 store 的 `scope` 侦听器自动清空并重取，本组件不做数据搬运；
 * - 项目名过长时截断，但 tooltip 给全名（项目名常带环境后缀，截断后无法区分）。
 */
import { computed } from 'vue'
import { ArrowDown } from '@element-plus/icons-vue'
import EnvBadge from '@/components/common/EnvBadge.vue'
import StatusTag from '@/components/common/StatusTag.vue'
import { useProjectStore } from '@/stores/project'
import type { ProjectStatus } from '@/types'

const projectStore = useProjectStore()

const projects = computed(() => projectStore.projects)
const current = computed(() => projectStore.currentProject)

/** 停用项目仍可见（历史数据要看），但不能切换到它上面 */
function isSelectable(status: ProjectStatus): boolean {
  return status === 'active'
}

function handleSelect(id: number): void {
  if (id === projectStore.currentProjectId) {
    return
  }
  projectStore.selectProject(id)
}
</script>

<template>
  <el-dropdown trigger="click" @command="handleSelect">
    <button type="button" class="switcher" :disabled="projects.length === 0">
      <span class="switcher__label">项目</span>
      <span class="switcher__name elt-ellipsis">
        {{ current?.name ?? (projectStore.loading ? '加载中…' : '未选择') }}
      </span>
      <el-icon class="switcher__caret" :size="12"><ArrowDown /></el-icon>
    </button>

    <template #dropdown>
      <el-dropdown-menu>
        <el-dropdown-item v-for="p in projects" :key="p.id" :command="p.id" :disabled="!isSelectable(p.status)">
          <div class="switcher__item">
            <span class="switcher__item-name elt-ellipsis" :title="p.name">{{ p.name }}</span>
            <EnvBadge :env="p.env" />
            <StatusTag v-if="p.status !== 'active'" kind="ProjectStatus" :value="p.status" />
          </div>
        </el-dropdown-item>
        <el-dropdown-item v-if="projects.length === 0" disabled>没有可访问的项目</el-dropdown-item>
      </el-dropdown-menu>
    </template>
  </el-dropdown>
</template>

<style scoped>
.switcher {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  max-width: 240px;
  height: 30px;
  padding: 0 10px;
  color: var(--elt-text-primary);
  cursor: pointer;
  background-color: var(--elt-bg-elevated);
  border: 1px solid var(--elt-border-subtle);
  border-radius: var(--elt-radius-control);
}

.switcher:hover:not(:disabled) {
  border-color: var(--elt-accent);
}

.switcher:disabled {
  cursor: not-allowed;
  opacity: 0.6;
}

.switcher__label {
  flex: none;
  font-size: 11px;
  color: var(--elt-text-muted);
}

.switcher__name {
  min-width: 0;
  font-size: 12px;
  font-weight: 600;
}

.switcher__caret {
  flex: none;
  color: var(--elt-text-muted);
}

.switcher__item {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 220px;
}

.switcher__item-name {
  flex: 1;
  min-width: 0;
}
</style>
