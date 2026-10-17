<script setup lang="ts">
/**
 * 通用确认弹窗（fe-01 §7）。
 *
 * 与 `ElMessageBox.confirm` 的分工：
 * - 只是「确定吗」的轻确认 → 用 ElMessageBox；
 * - **需要留下理由**的确认 → 用本组件。
 *
 * 审批、拒绝、回滚、强制放行都必须留下理由——`approval_decision.comment`
 * 与 `evidence_event` 是这个平台的审计主体，理由缺失等于审计断链。
 * 所以这里把「理由必填」做成组件能力，而不是指望每个页面记得校验。
 */
import { computed, ref, watch } from 'vue'

const props = withDefaults(
  defineProps<{
    modelValue: boolean
    title: string
    /** 支持换行：需要把风险后果讲清楚时用 */
    description?: string
    confirmText?: string
    cancelText?: string
    danger?: boolean
    /** 理由必填；拒绝类操作应为 true */
    requireReason?: boolean
    reasonLabel?: string
    reasonPlaceholder?: string
    /** 理由最短长度；太短的理由在审计时没有价值 */
    minReasonLength?: number
    /** 提交中，禁止重复点击与关闭 */
    loading?: boolean
    width?: string
  }>(),
  {
    description: undefined,
    confirmText: '确定',
    cancelText: '取消',
    danger: false,
    requireReason: false,
    reasonLabel: '理由',
    reasonPlaceholder: '请说明原因，该内容会写入证据账本',
    minReasonLength: 5,
    loading: false,
    width: '480px',
  },
)

const emit = defineEmits<{
  'update:modelValue': [value: boolean]
  confirm: [reason: string]
}>()

const reason = ref('')

// 每次打开都清空：残留上一次的拒绝理由会被误提交，
// 而审批记录是不可变的（CLAUDE.md §5.4），写错了只能作废重来。
watch(
  () => props.modelValue,
  (open) => {
    if (open) {
      reason.value = ''
    }
  },
)

const trimmed = computed<string>(() => reason.value.trim())

const reasonInvalid = computed<boolean>(
  () => props.requireReason && trimmed.value.length < props.minReasonLength,
)

const confirmDisabled = computed<boolean>(() => props.loading || reasonInvalid.value)

function close(): void {
  if (!props.loading) {
    emit('update:modelValue', false)
  }
}

function submit(): void {
  if (confirmDisabled.value) {
    return
  }
  emit('confirm', trimmed.value)
}
</script>

<template>
  <el-dialog
    :model-value="modelValue"
    :title="title"
    :width="width"
    :close-on-click-modal="!loading"
    :close-on-press-escape="!loading"
    :show-close="!loading"
    append-to-body
    @update:model-value="(v: boolean) => emit('update:modelValue', v)"
  >
    <p v-if="description !== undefined" class="confirm-dialog__desc">{{ description }}</p>

    <div v-if="requireReason" class="confirm-dialog__field">
      <label class="confirm-dialog__label" :for="'confirm-reason'">
        {{ reasonLabel }}
        <span class="confirm-dialog__required">*</span>
      </label>
      <el-input
        id="confirm-reason"
        v-model="reason"
        type="textarea"
        :rows="3"
        :maxlength="500"
        show-word-limit
        :placeholder="reasonPlaceholder"
        :disabled="loading"
      />
      <p v-if="reasonInvalid && trimmed.length > 0" class="confirm-dialog__hint">
        还需 {{ minReasonLength - trimmed.length }} 个字才能提交。
      </p>
    </div>

    <slot />

    <template #footer>
      <el-button :disabled="loading" @click="close">{{ cancelText }}</el-button>
      <el-button
        :type="danger ? 'danger' : 'primary'"
        :loading="loading"
        :disabled="confirmDisabled"
        @click="submit"
      >
        {{ confirmText }}
      </el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.confirm-dialog__desc {
  margin: 0 0 12px;
  font-size: 13px;
  line-height: 1.7;
  color: var(--elt-text-secondary);
  white-space: pre-wrap;
}

.confirm-dialog__field {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.confirm-dialog__label {
  font-size: 12px;
  font-weight: 600;
  color: var(--elt-text-secondary);
}

.confirm-dialog__required {
  color: var(--elt-danger);
}

.confirm-dialog__hint {
  margin: 0;
  font-size: 11px;
  color: var(--elt-warn);
}
</style>
