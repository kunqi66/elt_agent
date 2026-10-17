<script setup lang="ts">
/**
 * 项目设置（fe-08）。
 *
 * 为什么用页内 el-tabs 而不是三条子路由：
 * 三块内容（基本信息 / 成员 / 职责槽）共享同一个项目上下文，
 * 拆成子路由会把「切标签」变成一次整页重载，丢掉成员筛选与滚动位置。
 *
 * ⚠️ 本页**不是授权边界**（fe-08 §8 / CLAUDE.md §5.8）：
 * `can_manage_members` / `can_manage_roles` 只决定按钮显不显示；
 * 写操作照样发出去，由服务端裁决。禁止「前端觉得不行就不发请求」。
 *
 * 另外：本平台没有 admin 角色——RoleName 只有 maker / checker_data /
 * checker_security / operator / auditor，且职责槽是**按项目**授予的，
 * 同一个用户在不同项目可以持有不同职责槽。
 */
import { computed, ref } from 'vue'
import { ElMessage } from '@/utils/feedback'
import { toDisplayMessage } from '@/utils/errors'
import { useProjectStore } from '@/stores/project'
import {
  EmptyState,
  EnvBadge,
  ErrorState,
  PageHeader,
  SectionCard,
  StatusTag,
} from '@/components/common'
import ProjectBasicInfo from './components/ProjectBasicInfo.vue'
import MembersTab from './components/MembersTab.vue'
import RolesTab from './components/RolesTab.vue'

const projectStore = useProjectStore()

/** 默认落在基本信息：成员/职责槽是低频操作，先确认「我在哪个项目里改」 */
const activeTab = ref<'basic' | 'members' | 'roles'>('basic')

const project = computed(() => projectStore.currentProject)
const loading = computed<boolean>(() => projectStore.loading)
/** 加载失败优先于空态：拉不到项目 ≠「确实没有项目」（CONVENTIONS §1） */
const loadError = computed<string | null>(() => projectStore.error)

type ViewState = 'error' | 'loading' | 'empty' | 'ready'

const state = computed<ViewState>(() => {
  if (loadError.value !== null) {
    return 'error'
  }
  if (project.value !== null) {
    return 'ready'
  }
  // 还没有项目且正在拉列表 → 骨架；拉完了仍为空 → 空态
  return loading.value ? 'loading' : 'empty'
})

function retry(): void {
  // store 会把失败写进 error 供本页渲染错误态；这里只补一条 toast 说明「点过了」
  projectStore.loadProjects().catch((e: unknown) => {
    ElMessage.error(toDisplayMessage(e))
  })
}
</script>

<template>
  <div class="settings">
    <PageHeader
      title="项目设置"
      description="职责槽按项目授予：同一用户在不同项目可以持有不同职责槽。本页按钮显隐不代表授权，写操作一律由服务端裁决。"
    >
      <template v-if="project !== null" #notice>
        <div class="settings__current">
          <span class="settings__current-label">当前项目</span>
          <span class="settings__current-name">{{ project.name }}</span>
          <span class="settings__current-code elt-mono">{{ project.code }}</span>
          <EnvBadge :env="project.env" />
          <StatusTag kind="ProjectStatus" :value="project.status" />
        </div>
      </template>
    </PageHeader>

    <ErrorState
      v-if="state === 'error'"
      title="项目设置加载失败"
      :message="loadError"
      @retry="retry"
    />

    <SectionCard
      v-else-if="state === 'loading'"
      title="项目设置"
      :loading="true"
      :skeleton-height="320"
    />

    <EmptyState
      v-else-if="state === 'empty'"
      variant="no-data"
      title="没有可访问的项目"
      description="你还没有被加入任何项目。请联系某个项目的职责槽持有者把你加入后再回来设置。"
    />

    <el-tabs v-else v-model="activeTab" class="settings__tabs">
      <el-tab-pane label="基本信息" name="basic" lazy>
        <ProjectBasicInfo v-if="project !== null" :project="project" />
      </el-tab-pane>
      <el-tab-pane label="成员" name="members" lazy>
        <MembersTab v-if="project !== null" :project="project" />
      </el-tab-pane>
      <el-tab-pane label="职责槽" name="roles" lazy>
        <RolesTab v-if="project !== null" :project="project" />
      </el-tab-pane>
    </el-tabs>
  </div>
</template>

<style scoped>
.settings__current {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  color: var(--elt-text-secondary);
}

.settings__current-label {
  color: var(--elt-text-muted);
}

.settings__current-name {
  font-weight: 600;
  color: var(--elt-text-primary);
}

.settings__current-code {
  color: var(--elt-text-muted);
}

.settings__tabs :deep(.el-tabs__header) {
  margin-bottom: 14px;
}

.settings__tabs :deep(.el-tabs__item) {
  font-size: 13px;
}
</style>
