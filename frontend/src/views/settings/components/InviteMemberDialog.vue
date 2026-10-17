<script setup lang="ts">
/**
 * 添加成员（fe-08 §5 成员添加）。
 *
 * 候选用户来自后端 `GET /projects/{id}/member-candidates`（本租户内、尚未加入本项目的用户）。
 * 刻意**不**拉全租户用户再前端过滤——那会把全租户用户暴露给普通成员（fe-08 §10）。
 *
 * 操作理由必填：它会随 `project_member` 的写入落证据账本，
 * 自动填「无」会让审计失去意义（fe-08 §8）。
 */
import { computed, ref, watch } from 'vue'
import { Search } from '@element-plus/icons-vue'
import { toDisplayMessage } from '@/utils/errors'
import * as projectApi from '@/api/project'
import type { UserCandidate } from '@/types'

const props = defineProps<{
  modelValue: boolean
  projectId: number
  /** 提交中由父组件控制，避免此处再开一份 loading 状态 */
  submitting: boolean
  /** 父组件捕获到的写失败文案（含后端 message） */
  error: string | null
}>()

const emit = defineEmits<{
  'update:modelValue': [value: boolean]
  submit: [payload: { user_id: number; reason: string }]
}>()

const candidates = ref<UserCandidate[]>([])
const candidatesLoading = ref(false)
const candidatesError = ref<string | null>(null)

const selectedUserId = ref<number | null>(null)
const reason = ref('')

/** 理由最短长度：太短的理由在事后审计时无法还原「为什么加这个人」 */
const MIN_REASON = 5

async function loadCandidates(): Promise<void> {
  candidatesLoading.value = true
  candidatesError.value = null
  try {
    const list = await projectApi.fetchMemberCandidates(props.projectId)
    // 后端理应只返回未加入者；这里再滤一道，避免接口时序造成重复加入
    candidates.value = list.filter((c) => !c.already_member)
  } catch (e) {
    candidatesError.value = toDisplayMessage(e)
  } finally {
    candidatesLoading.value = false
  }
}

// 每次打开重置表单并拉一次候选：上次取消时选的用户不该被残留提交
watch(
  () => props.modelValue,
  (open) => {
    if (open) {
      selectedUserId.value = null
      reason.value = ''
      void loadCandidates()
    }
  },
)

const trimmedReason = computed<string>(() => reason.value.trim())
const reasonInvalid = computed<boolean>(() => trimmedReason.value.length < MIN_REASON)

const canSubmit = computed<boolean>(
  () => selectedUserId.value !== null && !reasonInvalid.value && !props.submitting,
)

function close(): void {
  if (!props.submitting) {
    emit('update:modelValue', false)
  }
}

function submit(): void {
  if (!canSubmit.value || selectedUserId.value === null) {
    return
  }
  emit('submit', { user_id: selectedUserId.value, reason: trimmedReason.value })
}
</script>

<template>
  <el-dialog
    :model-value="modelValue"
    title="添加成员"
    width="500px"
    append-to-body
    :close-on-click-modal="!submitting"
    :close-on-press-escape="!submitting"
    :show-close="!submitting"
    @update:model-value="(v: boolean) => emit('update:modelValue', v)"
  >
    <el-form label-position="top">
      <el-form-item label="选择用户" required>
        <el-select
          v-model="selectedUserId"
          class="invite__select"
          filterable
          :loading="candidatesLoading"
          :disabled="submitting || candidatesLoading"
          placeholder="按用户名 / 姓名搜索本租户内尚未加入的用户"
        >
          <template #prefix>
            <el-icon><Search /></el-icon>
          </template>
          <el-option
            v-for="c in candidates"
            :key="c.id"
            :value="c.id"
            :label="`${c.display_name}（@${c.username}）`"
          >
            <div class="invite__option">
              <span>{{ c.display_name }}</span>
              <span class="invite__option-sub elt-mono">@{{ c.username }} · {{ c.email }}</span>
            </div>
          </el-option>
        </el-select>
        <p v-if="candidatesError !== null" class="invite__cand-error">
          {{ candidatesError }}
          <el-button link type="primary" size="small" @click="loadCandidates">重试</el-button>
        </p>
        <p
          v-else-if="!candidatesLoading && candidates.length === 0"
          class="invite__cand-empty"
        >
          本租户内没有可加入的用户（全部已在项目中，或尚未创建其他账号）。
        </p>
      </el-form-item>

      <el-form-item label="操作理由" required>
        <el-input
          v-model="reason"
          type="textarea"
          :rows="3"
          :maxlength="500"
          show-word-limit
          :disabled="submitting"
          placeholder="说明为什么要把该用户加入本项目，该内容会写入证据账本"
        />
        <p v-if="reasonInvalid && reason.length > 0" class="invite__reason-hint">
          还需 {{ MIN_REASON - trimmedReason.length }} 个字才能提交。
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
        添加
      </el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.invite__select {
  width: 100%;
}

.invite__option {
  display: flex;
  flex-direction: column;
  line-height: 1.3;
}

.invite__option-sub {
  font-size: 11px;
  color: var(--elt-text-muted);
}

.invite__cand-error {
  margin: 4px 0 0;
  font-size: 11px;
  color: var(--elt-danger);
}

.invite__cand-empty {
  margin: 4px 0 0;
  font-size: 11px;
  color: var(--elt-text-muted);
}

.invite__reason-hint {
  margin: 2px 0 0;
  font-size: 11px;
  color: var(--elt-warn);
}
</style>
