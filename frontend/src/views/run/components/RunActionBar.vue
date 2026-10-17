<script setup lang="ts">
/**
 * 运行监督动作栏（fe-06 §5）。
 *
 * 按钮可用性**只取服务端下发的 `can_*`**，前端不推演状态机：
 * 合法转移由后端保证。前端若自己写一份「running → 可暂停」的规则，
 * 迟早会与后端状态机分叉，后果是用户点了按钮却被 409 拒绝。
 * `can_* === false` 时置灰并给出「当前状态不允许」的 tooltip，让用户知道原因。
 *
 * `rollbackBlocked`（上一次回滚失败）时禁用重复点击——状态叠加比不操作更危险：
 * 第二次回滚可能落在第一次的残留上。
 */
import { computed } from 'vue'
import { enumText } from '@/utils/enums'
import { SectionCard } from '@/components/common'
import type { RunDetail } from '@/types'

const props = withDefaults(
  defineProps<{
    run: RunDetail
    /** 只读视图（如 auditor）：整条栏渲染为禁用态 */
    readonly: boolean
    /** 有写操作在途：全部按钮禁用，防止并发下发两个监督动作 */
    submitting: boolean
    /** 上一次回滚失败（ROLLBACK_FAILED）：禁止重复点击 */
    rollbackBlocked?: boolean
  }>(),
  { rollbackBlocked: false },
)

const emit = defineEmits<{
  pause: []
  resume: []
  cancel: []
  rollback: []
}>()

const statusText = computed<string>(() => enumText('RunStatus', props.run.status))

interface ActionButton {
  key: 'pause' | 'resume' | 'cancel' | 'rollback'
  label: string
  type: 'primary' | 'warning' | 'danger' | 'default'
  allowed: boolean
  /** 置灰原因；allowed 时应为空 */
  reason: string
}

function reasonOf(allowed: boolean, extra: string): string {
  if (props.readonly) {
    return '只读视图：当前职责槽不可执行运行监督动作（服务端同样会拒绝）'
  }
  if (allowed) {
    return ''
  }
  return extra
}

const buttons = computed<ActionButton[]>(() => [
  {
    key: 'pause',
    label: '暂停',
    type: 'warning',
    allowed: props.run.can_pause,
    reason: reasonOf(props.run.can_pause, `当前状态「${statusText.value}」不允许暂停（仅运行中可暂停）`),
  },
  {
    key: 'resume',
    label: '恢复',
    type: 'primary',
    allowed: props.run.can_resume,
    reason: reasonOf(props.run.can_resume, `当前状态「${statusText.value}」不允许恢复（仅已暂停可恢复）`),
  },
  {
    key: 'cancel',
    label: '取消',
    type: 'danger',
    allowed: props.run.can_cancel,
    reason: reasonOf(props.run.can_cancel, `当前状态「${statusText.value}」不允许取消（仅排队中/运行中/已暂停可取消）`),
  },
  {
    key: 'rollback',
    label: '受管回滚',
    type: 'danger',
    allowed: props.run.can_rollback && !props.rollbackBlocked,
    reason: props.rollbackBlocked
      ? '上一次回滚失败，已禁用重复点击：请按故障提示联系运维人工介入'
      : reasonOf(props.run.can_rollback, `当前状态「${statusText.value}」不允许回滚（仅失败/硬中断/已取消/成功可回滚）`),
  },
])

function isDisabled(button: ActionButton): boolean {
  return props.readonly || props.submitting || !button.allowed
}

function onAction(button: ActionButton): void {
  if (isDisabled(button)) {
    return
  }
  switch (button.key) {
    case 'pause':
      emit('pause')
      return
    case 'resume':
      emit('resume')
      return
    case 'cancel':
      emit('cancel')
      return
    case 'rollback':
      emit('rollback')
  }
}
</script>

<template>
  <SectionCard title="监督操作" :padded="true">
    <template #extra>
      <span class="run-actions__audit">所有操作写入证据账本并记录操作人</span>
    </template>

    <div class="run-actions">
      <el-tooltip
        v-for="button in buttons"
        :key="button.key"
        :disabled="button.reason === ''"
        :content="button.reason"
        placement="top"
      >
        <!-- 包一层 span：disabled 的 el-button 不派发鼠标事件，tooltip 会失效 -->
        <span class="run-actions__wrap">
          <el-button
            :type="button.type === 'default' ? undefined : button.type"
            size="small"
            :disabled="isDisabled(button)"
            :loading="submitting && !readonly && button.allowed"
            @click="onAction(button)"
          >
            {{ button.label }}
          </el-button>
        </span>
      </el-tooltip>

      <span class="run-actions__state">当前状态：{{ statusText }}</span>
    </div>

    <p class="run-actions__note">
      暂停是软动作：不回收影子表，可随时恢复；取消与回滚会经 Harness 的
      <span class="elt-mono">seatunnel.stop</span> /
      <span class="elt-mono">target.rollback_cleanup</span> 工具执行，
      前端不直接调用 SeaTunnel 或目标库。
    </p>
  </SectionCard>
</template>

<style scoped>
.run-actions__audit {
  font-size: 11px;
  color: var(--elt-text-muted);
}

.run-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
}

.run-actions__wrap {
  display: inline-flex;
}

.run-actions__state {
  margin-left: auto;
  font-size: 12px;
  color: var(--elt-text-secondary);
}

.run-actions__note {
  margin: 10px 0 0;
  font-size: 11px;
  line-height: 1.7;
  color: var(--elt-text-muted);
}
</style>
