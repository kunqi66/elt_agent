/**
 * 通用组件统一出口（fe-01 §2）。
 *
 * 各业务页面**只能**从这里取这些组件，不得在自己目录下重建同名实现——
 * 「每个页面各写一个状态标签」正是这一层的存在理由。
 *
 * 命名导出而非默认导出：`import { StatusTag } from '@/components/common'`
 * 比一串 `import X from '@/components/common/X.vue'` 更能暴露「我引入了什么」，
 * 也让新增组件时只需改这一处。
 */

export { default as ArtifactHash } from './ArtifactHash.vue'
export { default as AsyncTaskPanel } from './AsyncTaskPanel.vue'
export { default as ConfirmDialog } from './ConfirmDialog.vue'
export { default as DangerConfirmDialog } from './DangerConfirmDialog.vue'
export { default as DraftFrozenBadge } from './DraftFrozenBadge.vue'
export { default as EmptyState } from './EmptyState.vue'
export { default as EnvBadge } from './EnvBadge.vue'
export { default as ErrorState } from './ErrorState.vue'
export { default as PageHeader } from './PageHeader.vue'
export { default as QueryTable } from './QueryTable.vue'
export { default as RiskLevelTag } from './RiskLevelTag.vue'
export { default as RunStatusTag } from './RunStatusTag.vue'
export { default as SectionCard } from './SectionCard.vue'
export { default as SensitiveText } from './SensitiveText.vue'
export { default as StatusTag } from './StatusTag.vue'
