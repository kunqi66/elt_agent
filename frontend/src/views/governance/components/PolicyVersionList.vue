<script setup lang="ts">
/**
 * 策略版本列表（fe-07 §6）。
 *
 * 刻意只做「看 + 选」：
 * - 不显示「提交发布」「批准」「驳回」按钮——本页是安全治理的只读面，
 *   发布走的是四眼流程，入口在审批台；
 * - `pending` 的变更集对只读身份（审计员）不做任何特殊渲染，
 *   因为审计员本来就没有动作可做，摆出「待你处理」只会误导。
 *
 * 「你提交的」标记用 `authStore.profile.id` 与 `submitted_by` 比对——
 * 四眼规则里严禁自批，这个标记是提醒，不是拦截（拦截在服务端 + DB 约束）。
 */
import { EmptyState, EnvBadge, StatusTag } from '@/components/common'
import { useAuthStore } from '@/stores/auth'
import { formatDateTime, textOrDash } from '@/utils/format'
import type { PolicyVersionRead } from '@/types'

withDefaults(
  defineProps<{
    items: PolicyVersionRead[]
    loading?: boolean
    selected: number | null
  }>(),
  { loading: false },
)

const emit = defineEmits<{
  select: [version: number]
}>()

const authStore = useAuthStore()

function isMine(version: PolicyVersionRead): boolean {
  const me = authStore.profile?.id
  return me !== undefined && version.submitted_by === me
}

function scopeText(version: PolicyVersionRead): string {
  const projects = version.effective_scope.project_ids.length
  return projects === 1 ? '1 个项目' : `${projects} 个项目`
}

function onSelect(version: PolicyVersionRead): void {
  emit('select', version.version)
}
</script>

<template>
  <div class="version-list">
    <div v-if="loading" class="elt-skeleton elt-skeleton--block" :style="{ height: '220px' }" />
    <EmptyState
      v-else-if="items.length === 0"
      variant="no-data"
      title="还没有策略版本"
      description="策略变更经「草稿 → 提交审批 → 生效」产出新版本；若刚发生过发布却看不到版本，请点右上角刷新重试。"
    />
    <ul v-else class="version-list__items">
      <li
        v-for="version in items"
        :key="version.version"
        class="version-list__item"
        :class="{ 'is-active': version.version === selected }"
        @click="onSelect(version)"
      >
        <div class="version-list__head">
          <span class="version-list__no elt-mono">v{{ version.version }}</span>
          <StatusTag kind="PolicyVersionStatus" :value="version.status" size="small" />
          <span v-if="isMine(version)" class="version-list__mine">你提交的</span>
        </div>

        <div class="version-list__meta">
          <span>提交：{{ textOrDash(version.submitted_by_name) }} · {{ formatDateTime(version.submitted_at) }}</span>
          <span v-if="version.status === 'approved'">
            生效：{{ textOrDash(version.approved_by_name) }} · {{ formatDateTime(version.effective_at) }}
          </span>
          <span v-else-if="version.status === 'pending'">等待审批中，生效前不影响线上判定</span>
          <span v-else>已驳回</span>
        </div>

        <div class="version-list__scope">
          <span class="version-list__scope-label">{{ scopeText(version) }}</span>
          <EnvBadge v-for="env in version.effective_scope.envs" :key="env" :env="env" />
        </div>

        <div v-if="version.reject_comment" class="version-list__reject">
          驳回意见：{{ version.reject_comment }}
        </div>
      </li>
    </ul>
  </div>
</template>

<style scoped>
.version-list {
  min-width: 0;
}

.version-list__items {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 0;
  margin: 0;
  list-style: none;
}

.version-list__item {
  padding: 10px 12px;
  cursor: pointer;
  background-color: var(--elt-bg-elevated);
  border: 1px solid var(--elt-border-subtle);
  border-radius: var(--elt-radius-control);
}

.version-list__item:hover {
  border-color: var(--elt-accent-soft);
}

/* 选中态用左边框而不是整块高亮：版本条目里已经有徽章颜色，整块染色会盖掉状态信息 */
.version-list__item.is-active {
  border-color: var(--elt-accent);
  box-shadow: inset 2px 0 0 0 var(--elt-accent);
}

.version-list__head {
  display: flex;
  align-items: center;
  gap: 8px;
}

.version-list__no {
  font-size: 13px;
  font-weight: 600;
  color: var(--elt-text-primary);
}

.version-list__mine {
  font-size: 11px;
  color: var(--elt-text-muted);
}

.version-list__meta {
  display: flex;
  flex-direction: column;
  gap: 2px;
  margin-top: 6px;
  font-size: 12px;
  color: var(--elt-text-secondary);
}

.version-list__scope {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  align-items: center;
  margin-top: 6px;
}

.version-list__scope-label {
  font-size: 11px;
  color: var(--elt-text-muted);
}

.version-list__reject {
  margin-top: 6px;
  font-size: 12px;
  line-height: 1.6;
  color: var(--elt-warn);
}
</style>
