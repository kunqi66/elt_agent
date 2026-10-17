<script setup lang="ts">
/**
 * 修改密码表单（fe-08 §6.6）。
 *
 * 边界（刻意为之）：
 * - **不回显任何旧值**，字段不回填；
 * - 前端只做「三方一致」与「新密码不得等于旧密码」两项本地校验；
 *   **强度策略以后端为准**，前端不实现长度/复杂度规则（避免前后端策略漂移），
 *   仅在字段下方展示后端下发的策略说明。
 * - 请求体不进任何 store、不进日志（`client.ts` 不打印请求体）。
 */
import { computed, ref } from 'vue'
import { Lock } from '@element-plus/icons-vue'
import { AsyncTaskPanel } from '@/components/common'

const props = withDefaults(
  defineProps<{
    submitting: boolean
    /** 整体错误（网络/服务端），渲染在表单下方且不自动消失 */
    error?: string | null
    /** 后端 VALIDATION_ERROR / BAD_CREDENTIALS 的字段级原因 */
    fieldErrors?: Record<string, string>
  }>(),
  { error: null, fieldErrors: () => ({}) },
)

const emit = defineEmits<{
  submit: [payload: { old_password: string; new_password: string }]
}>()

const oldPassword = ref('')
const newPassword = ref('')
const confirmPassword = ref('')

const localError = computed<string | null>(() => {
  if (newPassword.value.length > 0 && newPassword.value === oldPassword.value) {
    return '新密码不能与旧密码相同。'
  }
  if (confirmPassword.value.length > 0 && newPassword.value !== confirmPassword.value) {
    return '两次输入的新密码不一致。'
  }
  return null
})

const canSubmit = computed<boolean>(
  () =>
    oldPassword.value.length > 0 &&
    newPassword.value.length > 0 &&
    confirmPassword.value.length > 0 &&
    localError.value === null &&
    !props.submitting,
)

function submit(): void {
  if (!canSubmit.value) {
    return
  }
  emit('submit', {
    old_password: oldPassword.value,
    new_password: newPassword.value,
  })
}
</script>

<template>
  <el-form label-position="top" @submit.prevent="submit">
    <el-form-item label="当前密码" required>
      <el-input
        v-model="oldPassword"
        type="password"
        show-password
        autocomplete="current-password"
        :disabled="submitting"
        placeholder="请输入当前密码"
      />
      <p v-if="fieldErrors.old_password !== undefined" class="pw__field-error">
        {{ fieldErrors.old_password }}
      </p>
    </el-form-item>

    <el-form-item label="新密码" required>
      <el-input
        v-model="newPassword"
        type="password"
        show-password
        autocomplete="new-password"
        :disabled="submitting"
        placeholder="请输入新密码"
      />
      <p v-if="fieldErrors.new_password !== undefined" class="pw__field-error">
        {{ fieldErrors.new_password }}
      </p>
    </el-form-item>

    <el-form-item label="确认新密码" required>
      <el-input
        v-model="confirmPassword"
        type="password"
        show-password
        autocomplete="new-password"
        :disabled="submitting"
        placeholder="请再次输入新密码"
        @keyup.enter="submit"
      />
    </el-form-item>

    <p v-if="localError !== null" class="pw__field-error">{{ localError }}</p>

    <p class="pw__policy">
      <el-icon :size="12"><Lock /></el-icon>
      密码强度策略由服务端下发并校验，前端不自行实现复杂度规则。
    </p>

    <AsyncTaskPanel :loading="submitting" :error="error" loading-text="正在修改密码…" />

    <div class="pw__actions">
      <el-button type="primary" :loading="submitting" :disabled="!canSubmit" @click="submit">
        修改密码
      </el-button>
    </div>
  </el-form>
</template>

<style scoped>
.pw__field-error {
  margin: 2px 0 0;
  font-size: 11px;
  color: var(--elt-danger);
}

.pw__policy {
  display: flex;
  align-items: center;
  gap: 6px;
  margin: 0 0 4px;
  font-size: 11px;
  color: var(--elt-text-muted);
}

.pw__actions {
  margin-top: 14px;
}
</style>
