<script setup lang="ts">
/**
 * Commit 确认弹窗（fe-05 §5 / §6）。
 *
 * 为什么用逐字输入短语而不是一个「确定」按钮：Commit 会在生产数据面创建执行运行、
 * 由服务端换取一次性 Capability，并把影子表原子 Swap 上正式表。
 * 手指在回车键上连按两下就发出去了，是这个弹窗要防的第一件事；
 * 照抄 Pipeline 名的过程本身就是一次「我批的是哪张表」的复核。
 *
 * 幂等键语义（fe-05 §6）：
 * - 弹窗打开时生成一次；
 * - 同一弹窗内的重复点击 / 网络失败后的重试复用**同一个 key**；
 * - 弹窗关闭（成功或取消）后重置，下次打开是新 key —— 那是新的业务意图。
 *
 * 不展示、也不缓存任何令牌：Capability 全程在服务端，本弹窗只回显绑定事实。
 */
import { computed, watch } from 'vue'
import { ArtifactHash, AsyncTaskPanel, DangerConfirmDialog, SensitiveText } from '@/components/common'
import { useIdempotencyKey } from '@/composables/useIdempotencyKey'
import { enumText } from '@/utils/enums'
import { formatBytes, formatDuration, formatRows, hashShort } from '@/utils/format'
import type { PreparationDetail } from '@/types'

const props = defineProps<{
  open: boolean
  preparation: PreparationDetail | null
  submitting: boolean
  error: string | null
}>()

const emit = defineEmits<{
  /** 携带本次业务意图的幂等键；重试复用同一个 key */
  confirm: [idempotencyKey: string]
  cancel: []
}>()

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

/** 逐字短语：Pipeline 名比 #id 更能让用户读到「我在操作哪条管线」 */
const confirmPhrase = computed<string>(() => {
  const prep = props.preparation
  if (prep === null) {
    return ''
  }
  return prep.pipeline_name === '' ? `#${prep.id}` : prep.pipeline_name
})

const description = computed<string>(() => {
  const prep = props.preparation
  if (prep === null) {
    return ''
  }
  const budget = prep.budget
  return [
    `准备单 #${prep.id} ｜ ${prep.pipeline_name} ｜ v${prep.version_no} ｜ 风险等级 ${enumText('RiskLevel', prep.risk_level)} ｜ 环境 ${enumText('Env', prep.environment)}`,
    `制品指纹 ${hashShort(prep.artifact_sha256)}… ｜ 输入指纹 ${hashShort(prep.input_fingerprint)}…`,
    `预算上限：读 ${formatRows(budget.max_read_rows)} ｜ 写 ${formatBytes(budget.max_write_bytes)} ｜ 时长 ${formatDuration(budget.max_duration_sec)} ｜ 放大比 ≤ ${budget.max_amplification_ratio}×`,
    `写入方式：影子表构建完成后原子 Swap；失败按回滚方案清理影子表（${prep.rollback_plan.shadow_cleanup ? '已启用' : '未启用'}）。`,
    '',
    '提交后服务端签发一次性 Capability 并创建执行运行：该凭据只能消费一次，重复提交会被拒绝。',
    '执行结果与质量分流请在运行中心查看，本页不会同步返回执行结果。',
  ].join('\n')
})

const acknowledge = '我已确认上述影响范围、预算与回滚方案，并知悉 Capability 为一次性凭据，提交后不可重复。'

function onConfirm(payload: { reason: string; acknowledged: boolean }): void {
  // Commit 接口不接收意见字段，因此不要求用户填写理由（收了也只能丢弃）；
  // 审计主体是服务端写入的 run.created 证据事件。这里的摩擦来自逐字短语与勾选项。
  void payload
  emit('confirm', arm())
}

function onVisibleChange(value: boolean): void {
  if (!value) {
    emit('cancel')
  }
}
</script>

<template>
  <DangerConfirmDialog
    :model-value="open"
    title="提交执行（Commit）"
    :description="description"
    :confirm-phrase="confirmPhrase"
    :acknowledge="acknowledge"
    :require-reason="false"
    confirm-text="确认提交执行"
    :loading="submitting"
    width="620px"
    @update:model-value="onVisibleChange"
    @confirm="onConfirm"
  >
    <div v-if="preparation !== null" class="commit-facts">
      <div class="commit-facts__row">
        <span class="commit-facts__label">绑定制品指纹</span>
        <ArtifactHash :hash="preparation.artifact_sha256" display="truncate" copyable label="制品指纹" />
      </div>
      <div class="commit-facts__row">
        <span class="commit-facts__label">写入目标</span>
        <span class="commit-facts__value">
          <template v-for="(w, i) in preparation.resource_scope.writes" :key="`w-${i}`">
            <span class="elt-mono">{{ w.database }}.{{ w.table }}</span>
            <span v-if="i < preparation.resource_scope.writes.length - 1">、</span>
          </template>
          <span v-if="preparation.resource_scope.writes.length === 0" class="commit-facts__muted">—</span>
        </span>
      </div>
      <SensitiveText
        :value="preparation.rollback_plan.restore_scope"
        multiline
        class="commit-facts__hint"
      />
    </div>

    <AsyncTaskPanel :loading="submitting" :error="error" loading-text="正在提交 Commit…" />
  </DangerConfirmDialog>
</template>

<style scoped>
.commit-facts {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 10px;
  margin-bottom: 14px;
  background-color: var(--elt-bg-elevated);
  border: 1px solid var(--elt-border-subtle);
  border-radius: var(--elt-radius-control);
}

.commit-facts__row {
  display: flex;
  gap: 8px;
  align-items: center;
  font-size: 12px;
}

.commit-facts__label {
  flex: none;
  color: var(--elt-text-muted);
}

.commit-facts__value {
  color: var(--elt-text-primary);
  word-break: break-word;
}

.commit-facts__muted {
  color: var(--elt-text-muted);
}

.commit-facts__hint {
  font-size: 11px;
  line-height: 1.6;
  color: var(--elt-text-muted);
}
</style>
