<script setup lang="ts">
/**
 * 高危操作二次确认（fe-01 §7）。
 *
 * `target.publish_swap`（P0）、受管回滚、强制放行这类动作会改变生产数据面。
 * 一个「确定 / 取消」的弹窗对它们是不够的——手指在回车键上连按两下就发出去了。
 *
 * 这里用**输入指定短语**的方式制造一次认知停顿：
 * 用户必须把要操作的对象名照抄一遍，抄的过程中自然会读到「这是哪个管线、哪个环境」。
 * 这不是安全边界（后端仍会做 PDP 与 Capability 校验），
 * 而是防误触的摩擦，专治「点错了行」。
 */
import { computed, ref, watch } from 'vue'
import { WarningFilled } from '@element-plus/icons-vue'

const props = withDefaults(
  defineProps<{
    modelValue: boolean
    title: string
    description?: string
    /** 用户必须逐字输入的短语，通常是管线名或版本号 */
    confirmPhrase: string
    confirmText?: string
    /** 附加勾选项（如「我已确认目标表已有备份」） */
    acknowledge?: string
    /** 是否要求理由；高危操作默认要求 */
    requireReason?: boolean
    minReasonLength?: number
    loading?: boolean
    width?: string
  }>(),
  {
    description: undefined,
    confirmText: '确认执行',
    acknowledge: undefined,
    requireReason: true,
    minReasonLength: 5,
    loading: false,
    width: '520px',
  },
)

const emit = defineEmits<{
  'update:modelValue': [value: boolean]
  confirm: [payload: { reason: string; acknowledged: boolean }]
}>()

const typed = ref('')
const reason = ref('')
const acknowledged = ref(false)

watch(
  () => props.modelValue,
  (open) => {
    if (open) {
      typed.value = ''
      reason.value = ''
      acknowledged.value = false
    }
  },
)

const phraseOk = computed<boolean>(() => typed.value.trim() === props.confirmPhrase)
const reasonOk = computed<boolean>(
  () => !props.requireReason || reason.value.trim().length >= props.minReasonLength,
)
const ackOk = computed<boolean>(() => props.acknowledge === undefined || acknowledged.value)

const disabled = computed<boolean>(() => props.loading || !phraseOk.value || !reasonOk.value || !ackOk.value)

function close(): void {
  if (!props.loading) {
    emit('update:modelValue', false)
  }
}

function submit(): void {
  if (disabled.value) {
    return
  }
  emit('confirm', { reason: reason.value.trim(), acknowledged: acknowledged.value })
}
</script>

<template>
  <el-dialog
    :model-value="modelValue"
    :width="width"
    :close-on-click-modal="false"
    :close-on-press-escape="!loading"
    :show-close="!loading"
    append-to-body
    class="danger-dialog"
    @update:model-value="(v: boolean) => emit('update:modelValue', v)"
  >
    <template #header>
      <div class="danger-dialog__head">
        <el-icon class="danger-dialog__icon" :size="18"><WarningFilled /></el-icon>
        <span class="danger-dialog__title">{{ title }}</span>
      </div>
    </template>

    <p v-if="description !== undefined" class="danger-dialog__desc">{{ description }}</p>
    <slot />

    <div class="danger-dialog__block">
      <p class="danger-dialog__label">
        请输入 <code class="elt-mono danger-dialog__phrase">{{ confirmPhrase }}</code> 以确认
      </p>
      <el-input v-model="typed" :disabled="loading" placeholder="逐字输入上方短语" />
    </div>

    <label v-if="acknowledge !== undefined" class="danger-dialog__ack">
      <el-checkbox v-model="acknowledged" :disabled="loading" />
      <span>{{ acknowledge }}</span>
    </label>

    <div v-if="requireReason" class="danger-dialog__block">
      <p class="danger-dialog__label">
        执行理由 <span class="danger-dialog__required">*</span>
        <span class="danger-dialog__sub">（写入证据账本，不可修改）</span>
      </p>
      <el-input
        v-model="reason"
        type="textarea"
        :rows="2"
        :maxlength="500"
        show-word-limit
        :disabled="loading"
        placeholder="为什么现在必须执行这一步？"
      />
    </div>

    <template #footer>
      <el-button :disabled="loading" @click="close">取消</el-button>
      <el-button type="danger" :loading="loading" :disabled="disabled" @click="submit">
        {{ confirmText }}
      </el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.danger-dialog__head {
  display: flex;
  align-items: center;
  gap: 8px;
}

.danger-dialog__icon {
  color: var(--elt-danger);
}

.danger-dialog__title {
  font-size: 15px;
  font-weight: 600;
  color: var(--elt-text-primary);
}

.danger-dialog__desc {
  margin: 0 0 14px;
  font-size: 13px;
  line-height: 1.7;
  color: var(--elt-text-secondary);
  white-space: pre-wrap;
}

.danger-dialog__block {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin-bottom: 14px;
}

.danger-dialog__label {
  margin: 0;
  font-size: 12px;
  font-weight: 600;
  color: var(--elt-text-secondary);
}

.danger-dialog__phrase {
  padding: 1px 5px;
  color: var(--elt-warn);
  background-color: var(--elt-bg-elevated);
  border-radius: 4px;
}

.danger-dialog__required {
  color: var(--elt-danger);
}

.danger-dialog__sub {
  font-weight: 400;
  color: var(--elt-text-muted);
}

.danger-dialog__ack {
  display: flex;
  gap: 8px;
  align-items: center;
  margin-bottom: 14px;
  font-size: 12px;
  color: var(--elt-text-secondary);
  cursor: pointer;
}
</style>
