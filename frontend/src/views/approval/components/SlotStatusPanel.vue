<script setup lang="ts">
/**
 * 双槽状态卡（fe-05 §1.4）：数据槽与安全槽各自的待办 / 决定 / 决定人 / 时间 / 意见。
 *
 * 这里**不推演**「两槽是否齐备」：`required_slots` 与 `decisions` 都是后端下发的事实，
 * 组件只做「槽 × 决定」的一对一展示。四眼是否成立由服务端 `derive_status` 判定——
 * 本地拼出来的结论可能与服务端不一致（例如另一槽刚被退回）。
 *
 * 按钮禁用一律只是**展示层**：禁用时必定给出一句「为什么不能审」，
 * 而不是只把按钮变灰。灰而不解释会让人以为是页面坏了。
 */
import { computed } from 'vue'
import { ArtifactHash, SensitiveText, StatusTag } from '@/components/common'
import { enumTag, enumText } from '@/utils/enums'
import { formatDateTime } from '@/utils/format'
import type { ApprovalDecisionRead, ApprovalSlot, EnumLabel } from '@/types'

const props = defineProps<{
  requiredSlots: ApprovalSlot[]
  decisions: ApprovalDecisionRead[]
  /** 当前用户可审的槽；null = 不可审 */
  mySlot: ApprovalSlot | null
  /** 服务端字段折算出的「当前是否可提交决定」；仅用于按钮 enabled 态 */
  canDecide: boolean
  /** canDecide 为 false 时的原因文案；据此给 tooltip */
  blockReason: string | null
}>()

const emit = defineEmits<{
  approve: [slot: ApprovalSlot]
  reject: [slot: ApprovalSlot]
  return: [slot: ApprovalSlot]
}>()

interface SlotView {
  slot: ApprovalSlot
  decision: ApprovalDecisionRead | null
  /** 未决定且正是当前用户的槽 → 亮出操作按钮 */
  actionable: boolean
}

const slots = computed<SlotView[]>(() =>
  props.requiredSlots.map((slot) => ({
    slot,
    decision: props.decisions.find((d) => d.slot === slot) ?? null,
    actionable: props.mySlot === slot,
  })),
)

/** 已决定的槽按决定类型上色（口径与 utils/enums 的 ApprovalDecisionType 一致） */
function stateTagType(view: SlotView): EnumLabel['tag'] {
  if (view.decision !== null) {
    return enumTag('ApprovalDecisionType', view.decision.decision)
  }
  return 'info'
}

function stateText(view: SlotView): string {
  if (view.decision !== null) {
    return `已${enumText('ApprovalDecisionType', view.decision.decision)}`
  }
  if (view.actionable && props.canDecide) {
    return '待你审批'
  }
  if (view.actionable) {
    return '待审批（当前不可决策）'
  }
  return '待审批'
}
</script>

<template>
  <div class="slots">
    <article
      v-for="view in slots"
      :key="view.slot"
      class="slots__card"
      :class="{
        'slots__card--stale': view.decision?.stale === true,
        'slots__card--actionable': view.actionable && view.decision === null,
      }"
    >
      <header class="slots__head">
        <StatusTag kind="ApprovalSlot" :value="view.slot" />
        <el-tag :type="stateTagType(view)" size="small" effect="light" disable-transitions>
          {{ stateText(view) }}
        </el-tag>
        <el-tag
          v-if="view.decision?.stale === true"
          type="danger"
          size="small"
          effect="plain"
          disable-transitions
        >
          原审批已失效（指纹变化）
        </el-tag>
      </header>

      <template v-if="view.decision !== null">
        <dl class="slots__meta">
          <div><dt>决定人</dt><dd>{{ view.decision.approver_name }}</dd></div>
          <div>
            <dt>决定时职责</dt>
            <dd><StatusTag kind="RoleName" :value="view.decision.approver_role" /></dd>
          </div>
          <div><dt>决定时间</dt><dd class="elt-mono">{{ formatDateTime(view.decision.decided_at) }}</dd></div>
          <div class="slots__meta-wide">
            <dt>审批时制品指纹</dt>
            <dd><ArtifactHash :hash="view.decision.artifact_sha256" display="truncate" copyable label="审批时制品指纹" /></dd>
          </div>
          <div class="slots__meta-wide">
            <dt>审批时输入指纹</dt>
            <dd><ArtifactHash :hash="view.decision.fingerprint_at_decision" display="truncate" copyable label="审批时输入指纹" /></dd>
          </div>
        </dl>

        <div class="slots__comment">
          <span class="slots__comment-label">意见</span>
          <SensitiveText :value="view.decision.comment" multiline class="slots__comment-text" />
        </div>
      </template>

      <template v-else>
        <p class="slots__pending">
          {{
            view.actionable
              ? '该槽由你担任审批人；决定一经提交即不可修改（approval_decision 只允许新增）。'
              : '该槽尚未决定，由担任该职责槽的审批人处理。'
          }}
        </p>

        <div v-if="view.actionable" class="slots__actions">
          <!-- 禁用的按钮不解释原因，用户只会以为页面坏了；因此包一层 span 挂 tooltip -->
          <span class="slots__action">
            <el-tooltip :disabled="canDecide" :content="blockReason ?? ''" placement="top">
              <el-button
                type="primary"
                size="small"
                :disabled="!canDecide"
                @click="emit('approve', view.slot)"
              >
                通过
              </el-button>
            </el-tooltip>
          </span>
          <span class="slots__action">
            <el-tooltip :disabled="canDecide" :content="blockReason ?? ''" placement="top">
              <el-button
                type="danger"
                size="small"
                plain
                :disabled="!canDecide"
                @click="emit('reject', view.slot)"
              >
                拒绝
              </el-button>
            </el-tooltip>
          </span>
          <span class="slots__action">
            <el-tooltip :disabled="canDecide" :content="blockReason ?? ''" placement="top">
              <el-button size="small" :disabled="!canDecide" @click="emit('return', view.slot)">
                退回补充
              </el-button>
            </el-tooltip>
          </span>
        </div>

        <p v-else-if="view.actionable === false" class="slots__hint">
          当前用户不是该槽的审批人。
        </p>
      </template>

      <!-- 失效态蒙层：只压视觉，不拦点击开关（开关由 canDecide 统一控制） -->
      <span v-if="view.decision?.stale === true" class="slots__veil" aria-hidden="true" />
    </article>
  </div>
</template>

<style scoped>
.slots {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
  gap: 12px;
}

.slots__card {
  position: relative;
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 12px;
  overflow: hidden;
  background-color: var(--elt-bg-elevated);
  border: 1px solid var(--elt-border-subtle);
  border-radius: var(--elt-radius-control);
}

.slots__card--actionable {
  border-color: color-mix(in srgb, var(--elt-accent) 45%, transparent);
}

.slots__card--stale {
  background-color: var(--elt-bg-app);
}

.slots__head {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  align-items: center;
}

.slots__meta {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(150px, 1fr));
  gap: 8px 14px;
  margin: 0;
}

.slots__meta-wide {
  grid-column: 1 / -1;
}

.slots__meta dt {
  font-size: 11px;
  color: var(--elt-text-muted);
}

.slots__meta dd {
  margin: 2px 0 0;
  font-size: 12px;
  color: var(--elt-text-primary);
}

.slots__comment {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 8px 10px;
  background-color: var(--elt-bg-surface);
  border: 1px solid var(--elt-border-subtle);
  border-radius: var(--elt-radius-control);
}

.slots__comment-label {
  font-size: 11px;
  color: var(--elt-text-muted);
}

.slots__comment-text {
  font-size: 12px;
  line-height: 1.7;
  color: var(--elt-text-primary);
}

.slots__pending {
  margin: 0;
  font-size: 12px;
  line-height: 1.7;
  color: var(--elt-text-secondary);
}

.slots__hint {
  margin: 0;
  font-size: 11px;
  color: var(--elt-text-muted);
}

.slots__actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.slots__action {
  display: inline-flex;
}

.slots__veil {
  position: absolute;
  inset: 0;
  pointer-events: none;
  background-color: color-mix(in srgb, var(--elt-bg-app) 55%, transparent);
}
</style>
