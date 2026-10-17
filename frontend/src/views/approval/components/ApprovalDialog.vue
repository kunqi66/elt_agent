<script setup lang="ts">
/**
 * 审批决定弹窗（fe-05 §3.2 / §5）。
 *
 * 三态（通过 / 拒绝 / 退回）共用同一个弹窗，决定由**点下去的那个按钮**决定，
 * 不在弹窗里再放一个下拉——在下拉里改一次决定，就少一次「我到底批了什么」的确认。
 *
 * 二次确认正文回显**不可变事实**（准备单号、Pipeline、版本、两个指纹、风险、环境、敏感级别）：
 * 相似准备单并排打开时，只看标题很容易批错。理由必填且不可修改——
 * `approval_decision` 只允许 INSERT，批错了只能靠新版本重走流程。
 *
 * 这里不做任何「前端替后端判定」：即使按钮在页面上是灰的，
 * 真正的拒绝仍以服务端 SELF_APPROVAL_FORBIDDEN / DUTY_CONFLICT 为准。
 */
import { computed, watch } from 'vue'
import { ArtifactHash, AsyncTaskPanel, ConfirmDialog } from '@/components/common'
import { useIdempotencyKey } from '@/composables/useIdempotencyKey'
import { enumText } from '@/utils/enums'
import { hashShort, textOrDash } from '@/utils/format'
import type { ApprovalDecisionRequest, ApprovalDecisionType, ApprovalSlot, PreparationDetail } from '@/types'

const props = defineProps<{
  open: boolean
  preparation: PreparationDetail | null
  slot: ApprovalSlot | null
  decision: ApprovalDecisionType | null
  submitting: boolean
  /** 上一次提交的失败文案；留在弹窗里不自动消失 */
  error: string | null
}>()

const emit = defineEmits<{
  /** 携带本次业务意图的幂等键；弹窗内重试复用同一个 key */
  submit: [payload: ApprovalDecisionRequest, idempotencyKey: string]
  cancel: []
}>()

/**
 * 一次用户意图 = 一个 key：弹窗打开时 arm，关闭时 reset。
 *
 * 审批决定必须走 per-intent 键，而不是 client.ts 的按请求自动注入：
 * `approval_decision` 只允许 INSERT，超时后用户再点一次若拿到新 key，
 * 服务端会记下第二条决定——不可变表上的重复记录无法撤回，只能靠新版本重走流程。
 */
const { arm, reset } = useIdempotencyKey()

watch(
  () => props.open,
  (open) => {
    if (open) {
      arm()
    } else {
      reset()
    }
  },
)

const decisionText = computed<string>(() => enumText('ApprovalDecisionType', props.decision))

/** 拒绝/退回是破坏性决定，按钮走 danger */
const isDanger = computed<boolean>(() => props.decision !== 'approve')

const title = computed<string>(() => {
  const id = props.preparation === null ? '' : `：准备单 #${props.preparation.id}`
  return `确认${decisionText.value}${id}`
})

/**
 * 二次确认正文（fe-05 §5 的文案模板）。
 * 指纹只给前 8 位，完整值挂在下方 ArtifactHash 上可展开复制——
 * 一整串 64 位十六进制塞进一句话里，反而没人会去比对。
 */
const description = computed<string>(() => {
  const prep = props.preparation
  if (prep === null || props.slot === null) {
    return ''
  }
  const role = enumText('RoleName', props.slot)
  return [
    `我将以 ${role} 身份审批：`,
    `准备单 #${prep.id} ｜ Pipeline ${textOrDash(prep.pipeline_name)} ｜ v${prep.version_no}`,
    `制品指纹 ${hashShort(prep.artifact_sha256)}… ｜ 输入指纹 ${hashShort(prep.input_fingerprint)}…`,
    `风险等级 ${enumText('RiskLevel', prep.risk_level)} ｜ 环境 ${enumText('Env', prep.environment)} ｜ 敏感级别 ${enumText('SensitiveLevel', prep.sensitive_level)}`,
    `本次决定：${decisionText.value}（意见将写入 approval_decision，提交后不可修改）`,
  ].join('\n')
})

function onConfirm(reason: string): void {
  if (props.slot === null || props.decision === null) {
    return
  }
  emit('submit', { slot: props.slot, decision: props.decision, comment: reason }, arm())
}

function onVisibleChange(value: boolean): void {
  if (!value) {
    emit('cancel')
  }
}
</script>

<template>
  <ConfirmDialog
    :model-value="open"
    :title="title"
    :description="description"
    :confirm-text="`确认${decisionText}`"
    :danger="isDanger"
    :require-reason="true"
    reason-label="审批意见"
    reason-placeholder="说明判断依据；该内容会写入 approval_decision，之后不可修改"
    :loading="submitting"
    width="560px"
    @update:model-value="onVisibleChange"
    @confirm="onConfirm"
  >
    <div v-if="preparation !== null" class="approval-facts">
      <div class="approval-facts__row">
        <span class="approval-facts__label">审批时制品指纹</span>
        <ArtifactHash :hash="preparation.artifact_sha256" display="truncate" copyable label="制品指纹" />
      </div>
      <div class="approval-facts__row">
        <span class="approval-facts__label">审批时输入指纹</span>
        <ArtifactHash :hash="preparation.input_fingerprint" display="truncate" copyable label="输入指纹" />
      </div>
      <div class="approval-facts__hint">
        决定落库时会连同上述两个指纹一起记录；提交后若指纹发生变化，本次审批自动失效。
      </div>
    </div>

    <AsyncTaskPanel :loading="submitting" :error="error" loading-text="正在提交审批决定…" />
  </ConfirmDialog>
</template>

<style scoped>
.approval-facts {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 10px;
  margin-bottom: 12px;
  background-color: var(--elt-bg-elevated);
  border: 1px solid var(--elt-border-subtle);
  border-radius: var(--elt-radius-control);
}

.approval-facts__row {
  display: flex;
  gap: 8px;
  align-items: center;
  font-size: 12px;
}

.approval-facts__label {
  flex: none;
  color: var(--elt-text-muted);
}

.approval-facts__hint {
  font-size: 11px;
  line-height: 1.6;
  color: var(--elt-text-muted);
}
</style>
