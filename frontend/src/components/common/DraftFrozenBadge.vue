<script setup lang="ts">
/**
 * 草稿 / 不可变 标识（CLAUDE.md §5.4）。
 *
 * `pipeline_version`、`preparation`、`approval_decision`、`capability`、`evidence_event`
 * 只允许 INSERT。用户在界面上必须**随时知道自己看的是哪一种**，
 * 否则会出现「想改一版草稿，结果改的是已冻结版本」的误操作预期。
 *
 * 冻结态显示锁图标 + 「不可变」，并附冻结时间与冻结人。
 */
import { computed } from 'vue'
import { Lock, EditPen } from '@element-plus/icons-vue'
import { formatDateTime } from '@/utils/format'

const props = withDefaults(
  defineProps<{
    frozen: boolean
    frozenAt?: string | null
    frozenBy?: string | null
    /** 未冻结时的文案；不同场景叫法不同（草稿 / 待冻结 / 未冻结） */
    draftText?: string
  }>(),
  { frozenAt: null, frozenBy: null, draftText: '草稿' },
)

const tooltip = computed<string>(() => {
  if (!props.frozen) {
    return '尚未冻结，可继续修改'
  }
  const parts = [`冻结于 ${formatDateTime(props.frozenAt)}`]
  if (props.frozenBy !== null && props.frozenBy !== '') {
    parts.push(`冻结人 ${props.frozenBy}`)
  }
  parts.push('不可变制品，任何修正都只能产生新版本')
  return parts.join(' · ')
})
</script>

<template>
  <el-tooltip :content="tooltip" placement="top">
    <span class="frozen-badge" :class="frozen ? 'frozen-badge--frozen' : 'frozen-badge--draft'">
      <el-icon :size="12">
        <Lock v-if="frozen" />
        <EditPen v-else />
      </el-icon>
      <span>{{ frozen ? '不可变' : draftText }}</span>
    </span>
  </el-tooltip>
</template>

<style scoped>
.frozen-badge {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 1px 7px;
  border: 1px solid transparent;
  border-radius: 999px;
  font-size: 11px;
  font-weight: 600;
  line-height: 16px;
  white-space: nowrap;
}

.frozen-badge--draft {
  color: var(--elt-text-secondary);
  border-color: var(--elt-border-subtle);
  background-color: var(--elt-bg-elevated);
}

.frozen-badge--frozen {
  color: var(--elt-accent);
  border-color: color-mix(in srgb, var(--elt-accent) 45%, transparent);
  background-color: var(--elt-badge-accent);
}
</style>
