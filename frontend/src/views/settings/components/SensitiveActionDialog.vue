<script setup lang="ts">
/**
 * 敏感操作二次确认（fe-08 §6.5）。
 *
 * 统一承载「移除成员 / 回收职责槽」这类会改变权限面的动作：
 * - 逐条列出**影响范围**，而不是一句「确定吗」；
 * - **操作理由必填**（≥10 字），随证据事件落账；
 * - 明确告知「本次操作已写入证据账本」，让操作者对留痕有预期。
 *
 * `confirmPhrase` 非空时（如移除成员要抄用户名）额外要求逐字输入，
 * 制造一次认知停顿，专治「点错了行」。它**不是**安全边界。
 */
import { computed, ref, watch } from 'vue'
import { WarningFilled } from '@element-plus/icons-vue'

const props = withDefaults(
  defineProps<{
    modelValue: boolean
    title: string
    /** 影响范围，逐条展示；空数组表示只有理由与留痕提示 */
    impact?: string[]
    /** 需要逐字输入的短语；空串表示不需要 */
    confirmPhrase?: string
    submitText?: string
    loading?: boolean
  }>(),
  { impact: () => [], confirmPhrase: '', submitText: '确认执行', loading: false },
)

const emit = defineEmits<{
  'update:modelValue': [value: boolean]
  confirm: [reason: string]
}>()

/** 理由最短长度：与 fe-08 §6.5 一致 */
const MIN_REASON = 10

const phrase = ref('')
const reason = ref('')

watch(
  () => props.modelValue,
  (open) => {
    if (open) {
      phrase.value = ''
      reason.value = ''
    }
  },
)

const trimmedReason = computed<string>(() => reason.value.trim())
const phraseOk = computed<boolean>(
  () => props.confirmPhrase === '' || phrase.value.trim() === props.confirmPhrase,
)
const reasonOk = computed<boolean>(() => trimmedReason.value.length >= MIN_REASON)
const canSubmit = computed<boolean>(() => phraseOk.value && reasonOk.value && !props.loading)

function close(): void {
  if (!props.loading) {
    emit('update:modelValue', false)
  }
}

function submit(): void {
  if (!canSubmit.value) {
    return
  }
  emit('confirm', trimmedReason.value)
}
</script>

<template>
  <el-dialog
    :model-value="modelValue"
    :width="500"
    append-to-body
    :close-on-click-modal="false"
    :close-on-press-escape="!loading"
    :show-close="!loading"
    @update:model-value="(v: boolean) => emit('update:modelValue', v)"
  >
    <template #header>
      <div class="sensitive__head">
        <el-icon class="sensitive__icon" :size="18"><WarningFilled /></el-icon>
        <span class="sensitive__title">{{ title }}</span>
      </div>
    </template>

    <ul v-if="impact.length > 0" class="sensitive__impact">
      <li v-for="(line, i) in impact" :key="i">{{ line }}</li>
    </ul>

    <div v-if="confirmPhrase !== ''" class="sensitive__block">
      <p class="sensitive__label">
        请输入 <code class="elt-mono sensitive__phrase">{{ confirmPhrase }}</code> 以确认
      </p>
      <el-input v-model="phrase" :disabled="loading" placeholder="逐字输入上方短语" />
    </div>

    <div class="sensitive__block">
      <p class="sensitive__label">
        操作理由 <span class="sensitive__required">*</span>
        <span class="sensitive__sub">（至少 {{ MIN_REASON }} 字，写入证据账本）</span>
      </p>
      <el-input
        v-model="reason"
        type="textarea"
        :rows="3"
        :maxlength="500"
        show-word-limit
        :disabled="loading"
        placeholder="为什么现在必须做这一步？"
      />
      <p v-if="reason.length > 0 && !reasonOk" class="sensitive__hint">
        还需 {{ MIN_REASON - trimmedReason.length }} 个字才能提交。
      </p>
    </div>

    <el-alert
      type="info"
      :closable="false"
      show-icon
      title="本次操作已写入证据账本（操作人 / 时间 / 影响对象 / 理由）"
    />

    <template #footer>
      <el-button :disabled="loading" @click="close">取消</el-button>
      <el-button type="danger" :loading="loading" :disabled="!canSubmit" @click="submit">
        {{ submitText }}
      </el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.sensitive__head {
  display: flex;
  align-items: center;
  gap: 8px;
}

.sensitive__icon {
  color: var(--elt-danger);
}

.sensitive__title {
  font-size: 15px;
  font-weight: 600;
  color: var(--elt-text-primary);
}

.sensitive__impact {
  padding-left: 18px;
  margin: 0 0 14px;
  font-size: 12px;
  line-height: 1.8;
  color: var(--elt-text-secondary);
}

.sensitive__block {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin-bottom: 14px;
}

.sensitive__label {
  margin: 0;
  font-size: 12px;
  font-weight: 600;
  color: var(--elt-text-secondary);
}

.sensitive__phrase {
  padding: 1px 5px;
  color: var(--elt-warn);
  background-color: var(--elt-bg-elevated);
  border-radius: 4px;
}

.sensitive__required {
  color: var(--elt-danger);
}

.sensitive__sub {
  font-weight: 400;
  color: var(--elt-text-muted);
}

.sensitive__hint {
  margin: 0;
  font-size: 11px;
  color: var(--elt-warn);
}
</style>
