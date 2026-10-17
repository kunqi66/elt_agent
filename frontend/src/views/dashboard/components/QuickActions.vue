<script setup lang="ts">
/**
 * 快捷入口（fe-02 §6.4）。
 *
 * `roles` **只用于显隐**，不构成任何安全边界：真正的裁决在后端 role_assignment。
 * 因此这里隐藏入口只是「别让用户点进去才发现没权限」，而不是「前端替后端拒绝」。
 * 入口与目标路径严格照 §6.4 的表，不额外发明跳转目标。
 */
import { computed } from 'vue'
import type { Component } from 'vue'
import { useRouter } from 'vue-router'
import { Connection, MagicStick, Stamp, VideoPlay } from '@element-plus/icons-vue'
import { EmptyState, SectionCard } from '@/components/common'
import { hasAnyRole } from '@/utils/enums'
import type { RoleName } from '@/types'

const props = defineProps<{
  roles: RoleName[]
  /** 审批待办徽标；0 时不显示徽标（0 是个会让人困惑的红色小圆点） */
  pendingCount?: number
}>()

interface QuickAction {
  key: string
  title: string
  description: string
  icon: Component
  path: string
  query: Record<string, string> | null
  /** 体验层可见性；空数组 = 所有登录用户可见 */
  required: RoleName[]
  badge: number
}

const router = useRouter()

const actions = computed<QuickAction[]>(() => {
  const candidates: QuickAction[] = [
    {
      key: 'datasource',
      title: '新建连接',
      description: '登记数据库 / 文件 / 对象存储',
      icon: Connection,
      path: '/datasources/new',
      query: null,
      required: ['maker'],
      badge: 0,
    },
    {
      key: 'pipeline',
      title: '创建 Pipeline',
      description: '用自然语言生成 ETL 方案',
      icon: MagicStick,
      path: '/pipelines',
      query: null,
      required: ['maker'],
      badge: 0,
    },
    {
      key: 'approval',
      title: '审批待办',
      description: '数据槽与安全槽的双人复核',
      icon: Stamp,
      path: '/approvals',
      query: null,
      required: ['checker_data', 'checker_security', 'auditor'],
      badge: props.pendingCount ?? 0,
    },
    {
      key: 'runs',
      title: '查看运行',
      description: '运行中实例与质量分流',
      icon: VideoPlay,
      path: '/runs',
      query: { status: 'running' },
      required: ['operator', 'auditor'],
      badge: 0,
    },
  ]
  return candidates.filter((action) => hasAnyRole(props.roles, action.required))
})

function open(action: QuickAction): void {
  void router.push(action.query === null ? action.path : { path: action.path, query: action.query })
}
</script>

<template>
  <SectionCard title="快捷入口">
    <EmptyState
      v-if="actions.length === 0"
      variant="no-permission"
      title="没有可用的快捷入口"
      description="当前职责槽下没有可直接发起的操作。审批待办与运行中心需要相应的职责槽。"
    />

    <div v-else class="quick">
      <button
        v-for="action in actions"
        :key="action.key"
        type="button"
        class="quick__item"
        @click="open(action)"
      >
        <el-icon class="quick__icon" :size="16">
          <component :is="action.icon" />
        </el-icon>
        <span class="quick__text">
          <span class="quick__title">{{ action.title }}</span>
          <span class="quick__desc">{{ action.description }}</span>
        </span>
        <span v-if="action.badge > 0" class="quick__badge elt-mono">{{ action.badge }}</span>
      </button>
    </div>
  </SectionCard>
</template>

<style scoped>
.quick {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.quick__item {
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
  padding: 10px 12px;
  text-align: left;
  cursor: pointer;
  background-color: var(--elt-bg-elevated);
  border: 1px solid transparent;
  border-radius: var(--elt-radius-control);
  transition: border-color 0.15s ease;
}

.quick__item:hover,
.quick__item:focus-visible {
  border-color: var(--elt-accent);
  outline: none;
}

@media (prefers-reduced-motion: reduce) {
  .quick__item {
    transition: none;
  }
}

.quick__icon {
  flex: none;
  color: var(--elt-accent);
}

.quick__text {
  display: flex;
  flex-direction: column;
  gap: 1px;
  min-width: 0;
}

.quick__title {
  font-size: 13px;
  font-weight: 600;
  color: var(--elt-text-primary);
}

.quick__desc {
  font-size: 11px;
  line-height: 16px;
  color: var(--elt-text-muted);
}

.quick__badge {
  flex: none;
  min-width: 18px;
  margin-left: auto;
  padding: 0 5px;
  font-size: 11px;
  line-height: 18px;
  color: var(--elt-warn);
  text-align: center;
  background-color: var(--elt-badge-warn);
  border-radius: var(--elt-radius-pill);
}
</style>
