<script setup lang="ts">
/**
 * 授予职责槽（fe-08 §5 职责槽授予 / §6.3 互斥 / §6.4 临时授权 / §6.5 敏感确认）。
 *
 * 三个刻意的设计：
 * 1. **互斥只提示、不禁用提交**。出现 maker × checker_* 组合时给红色提示，
 *    但提交按钮仍可点——真正的拒绝在 service + DB 触发器（fe-08 §6.3）。
 *    禁用会掩盖后端的权威判定，也让前后端规则不一致时无从判断。
 * 2. **授予 checker_security / operator 需逐字输入角色名**（§6.5）。
 * 3. **有效期按浏览器本地时区输入，提交前转 ISO 8601 带偏移量**，
 *    避免服务端按 UTC 解析导致整点偏移（§6.4）。
 */
import { computed, ref, watch } from 'vue'
import { InfoFilled, WarningFilled } from '@element-plus/icons-vue'
import { enumOptions, enumText, rolesText } from '@/utils/enums'
import type { MemberRead, RoleGrantRequest, RoleName } from '@/types'

const props = defineProps<{
  modelValue: boolean
  projectId: number
  /** 预设被授予人；为 null 时由本组件提供成员选择（从「职责槽」页发起时） */
  userId: number | null
  /** 预设被授予人姓名，仅用于标题 */
  userName: string
  /** userId 为 null 时的候选成员 */
  members: MemberRead[]
  /** 已有职责槽，仅用于互斥提示，不用于拦截 */
  existingRoles: RoleName[]
  submitting: boolean
  error: string | null
}>()

const emit = defineEmits<{
  'update:modelValue': [value: boolean]
  submit: [payload: RoleGrantRequest]
}>()

/** 理由最短长度：与 §6.5 的敏感确认一致 */
const MIN_REASON = 10
/** 「永久」授权被写成 2099 年的上限（§6.4） */
const MAX_WINDOW_MS = 365 * 24 * 60 * 60 * 1000

const roleOptions = enumOptions('RoleName')

const userChoice = ref<number | null>(null)
const selectedRole = ref<RoleName | ''>('')
const useValidity = ref(false)
const range = ref<[Date, Date] | null>(null)
const reason = ref('')
const phrase = ref('')

watch(
  () => props.modelValue,
  (open) => {
    if (open) {
      userChoice.value = props.userId
      selectedRole.value = ''
      useValidity.value = false
      range.value = null
      reason.value = ''
      phrase.value = ''
    }
  },
)

/** 从成员列表反查被授予人；成员选择变化时要重算已有职责槽 */
const selectedMember = computed<MemberRead | null>(
  () => props.members.find((m) => m.user_id === userChoice.value) ?? null,
)

const effectiveExistingRoles = computed<RoleName[]>(() =>
  props.userId === null ? (selectedMember.value?.roles ?? []) : props.existingRoles,
)

const targetName = computed<string>(() =>
  props.userId === null ? (selectedMember.value?.display_name ?? '') : props.userName,
)

/**
 * maker 与 checker_data / checker_security 互斥；
 * checker_data 与 checker_security 之间**不**互斥（§6.3 第 3 条）。
 */
const hasConflict = computed<boolean>(() => {
  const role = selectedRole.value
  if (role === '') {
    return false
  }
  const existing = effectiveExistingRoles.value
  const hasMaker = existing.includes('maker')
  const hasChecker = existing.includes('checker_data') || existing.includes('checker_security')
  if (role === 'maker') {
    return hasChecker
  }
  if (role === 'checker_data' || role === 'checker_security') {
    return hasMaker
  }
  return false
})

/** §6.5：授予这两个角色需要手动输入角色名 */
const needsPhrase = computed<boolean>(
  () => selectedRole.value === 'checker_security' || selectedRole.value === 'operator',
)

/**
 * 数据审批槽与安全审批槽「应为不同人」是服务端约束（CLAUDE.md §5.8）。
 * 兼任本身被允许（§6.3 第 3 条：两个槽位仍是独立两次决定），
 * 因此这里只做**提示**，不拦截提交。
 */
const hasBothCheckerHint = computed<boolean>(() => {
  const role = selectedRole.value
  const existing = effectiveExistingRoles.value
  return (
    (role === 'checker_security' && existing.includes('checker_data')) ||
    (role === 'checker_data' && existing.includes('checker_security'))
  )
})

const trimmedReason = computed<string>(() => reason.value.trim())
const reasonInvalid = computed<boolean>(() => trimmedReason.value.length < MIN_REASON)

const windowError = computed<string | null>(() => {
  if (!useValidity.value) {
    return null
  }
  const r = range.value
  if (r === null) {
    return '请选择生效时间段，或取消「设置有效期」。'
  }
  const [from, to] = r
  if (to.getTime() <= from.getTime()) {
    return '结束时间必须晚于开始时间。'
  }
  if (to.getTime() > Date.now() + MAX_WINDOW_MS) {
    return '有效期不得超过 365 天。'
  }
  return null
})

const phraseOk = computed<boolean>(
  () => !needsPhrase.value || phrase.value.trim() === selectedRole.value,
)

const canSubmit = computed<boolean>(
  () =>
    userChoice.value !== null &&
    selectedRole.value !== '' &&
    !reasonInvalid.value &&
    phraseOk.value &&
    windowError.value === null &&
    !props.submitting,
)

/** 本地时区 Date → 带偏移量的 ISO 8601（避免服务端按 UTC 解析产生偏移） */
function toIsoWithOffset(d: Date): string {
  const offsetMin = -d.getTimezoneOffset()
  const sign = offsetMin >= 0 ? '+' : '-'
  const abs = Math.abs(offsetMin)
  const pad = (n: number): string => String(n).padStart(2, '0')
  const wall = new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 19)
  return `${wall}${sign}${pad(Math.floor(abs / 60))}:${pad(abs % 60)}`
}

function close(): void {
  if (!props.submitting) {
    emit('update:modelValue', false)
  }
}

function submit(): void {
  if (!canSubmit.value || userChoice.value === null || selectedRole.value === '') {
    return
  }
  const r = range.value
  const payload: RoleGrantRequest = {
    user_id: userChoice.value,
    role: selectedRole.value,
    valid_from: useValidity.value && r !== null ? toIsoWithOffset(r[0]) : null,
    valid_to: useValidity.value && r !== null ? toIsoWithOffset(r[1]) : null,
    reason: trimmedReason.value,
  }
  emit('submit', payload)
}

function onRangeUpdate(value: unknown): void {
  if (Array.isArray(value) && value.length === 2 && value[0] instanceof Date && value[1] instanceof Date) {
    range.value = [value[0], value[1]]
  } else {
    range.value = null
  }
}
</script>

<template>
  <el-dialog
    :model-value="modelValue"
    title="授予职责槽"
    width="560px"
    append-to-body
    :close-on-click-modal="false"
    :close-on-press-escape="!submitting"
    :show-close="!submitting"
    @update:model-value="(v: boolean) => emit('update:modelValue', v)"
  >
    <el-form label-position="top">
      <el-form-item v-if="userId === null" label="被授予人" required>
        <el-select
          v-model="userChoice"
          class="grant__full"
          filterable
          :disabled="submitting"
          placeholder="选择项目成员"
        >
          <el-option
            v-for="m in members"
            :key="m.id"
            :value="m.user_id"
            :label="`${m.display_name}（@${m.username}）`"
          >
            <div class="grant__option">
              <span>{{ m.display_name }}</span>
              <span class="grant__option-sub">{{ rolesText(m.roles) }}</span>
            </div>
          </el-option>
        </el-select>
      </el-form-item>

      <el-form-item v-else label="被授予人">
        <span class="grant__target">{{ targetName }}</span>
      </el-form-item>

      <el-form-item label="职责槽" required>
        <el-select
          v-model="selectedRole"
          class="grant__full"
          :disabled="submitting"
          placeholder="选择要授予的职责槽"
        >
          <el-option
            v-for="opt in roleOptions"
            :key="opt.value"
            :value="opt.value"
            :label="opt.label"
          />
        </el-select>
        <p class="grant__existing">
          当前已有：{{ rolesText(effectiveExistingRoles) }}
        </p>
      </el-form-item>

      <!-- 互斥提示：红色，但不禁用提交（fe-08 §6.3） -->
      <el-alert
        v-if="hasConflict"
        class="grant__conflict"
        type="error"
        :closable="false"
        show-icon
      >
        <template #title>
          <el-icon :size="13"><WarningFilled /></el-icon>
          职责互斥：服务端将拒绝该授予
        </template>
        同一项目内 maker 与 checker_data / checker_security 互斥——一个人不能既提交又审批。
        可在「职责槽」标签页的角色能力矩阵中查看完整规则。本提示仅供参考，是否放行由服务端裁决。
      </el-alert>

      <el-alert
        v-if="hasBothCheckerHint"
        class="grant__conflict"
        type="warning"
        :closable="false"
        show-icon
        title="数据审批槽与安全审批槽应为不同人"
      >
        两个槽位仍是独立的两次决定，兼任在服务端被允许；但 P1 场景下建议由不同人担任（fe-08 §6.3）。本提示仅供参考。
      </el-alert>

      <el-form-item>
        <el-checkbox v-model="useValidity" :disabled="submitting">
          设置有效期（临时授权，留空则为永久授权）
        </el-checkbox>
      </el-form-item>

      <el-form-item v-if="useValidity" label="生效时间段">
        <el-date-picker
          :model-value="range"
          class="grant__full"
          type="datetimerange"
          range-separator="至"
          start-placeholder="生效时间"
          end-placeholder="失效时间"
          :disabled="submitting"
          @update:model-value="onRangeUpdate"
        />
        <p v-if="windowError !== null" class="grant__err">{{ windowError }}</p>
        <p v-else class="grant__hint">按浏览器本地时区输入，提交时转为带偏移量的 ISO 8601。</p>
      </el-form-item>

      <el-form-item label="操作理由" required>
        <el-input
          v-model="reason"
          type="textarea"
          :rows="3"
          :maxlength="500"
          show-word-limit
          :disabled="submitting"
          placeholder="说明为什么授予该职责槽，该内容会写入证据账本（至少 10 字）"
        />
        <p v-if="reason.length > 0 && reasonInvalid" class="grant__hint">
          还需 {{ MIN_REASON - trimmedReason.length }} 个字才能提交。
        </p>
      </el-form-item>

      <el-form-item v-if="needsPhrase" :label="`确认角色：输入「${selectedRole === '' ? '' : enumText('RoleName', selectedRole)}」的角色标识`" required>
        <el-input
          v-model="phrase"
          class="elt-mono"
          :disabled="submitting"
          :placeholder="selectedRole === '' ? '' : selectedRole"
        />
        <p class="grant__hint">
          <el-icon :size="12"><InfoFilled /></el-icon>
          授予该职责槽将显著扩大被授予人的能力范围，需手动输入角色标识以确认。
        </p>
      </el-form-item>
    </el-form>

    <el-alert
      v-if="error !== null"
      type="error"
      :closable="false"
      show-icon
      :title="error"
    />

    <template #footer>
      <el-button :disabled="submitting" @click="close">取消</el-button>
      <el-button type="primary" :loading="submitting" :disabled="!canSubmit" @click="submit">
        授予
      </el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.grant__full {
  width: 100%;
}

.grant__target {
  font-size: 13px;
  color: var(--elt-text-primary);
}

.grant__existing,
.grant__hint {
  margin: 2px 0 0;
  font-size: 11px;
  color: var(--elt-text-muted);
}

.grant__err {
  margin: 2px 0 0;
  font-size: 11px;
  color: var(--elt-danger);
}

.grant__conflict {
  margin-bottom: 14px;
}

.grant__option {
  display: flex;
  justify-content: space-between;
  gap: 12px;
}

.grant__option-sub {
  font-size: 11px;
  color: var(--elt-text-muted);
}
</style>
