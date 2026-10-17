<script setup lang="ts">
/**
 * 审批中心 · 准备单详情（fe-05 §5 / §6 / §7 / §8）。
 *
 * 这个页面的第一原则是「**界面不得比后端更乐观**」：
 * - 能不能审、能不能 Commit，一律取后端下发的 `my_slot` / `can_commit` / `stale`；
 * - 提交后不本地推演 `decided_slots`，而是 `refreshAfterDecision()` 重新拉详情——
 *   两槽是否齐备由服务端 `derive_status` 判定，本地拼出来的结论可能与服务端不一致；
 * - 按钮置灰**必定**附一句「为什么」，灰而不解释会让用户以为页面坏了；
 * - 置灰的同时仍然允许发请求：真正的裁决在服务端（自批、职责互斥、指纹、有效期、幂等），
 *   前端从不「替后端拒绝」。
 *
 * 安全：本页只接收 `PreparationDetail`，其中不含任何凭据；
 * Capability 的令牌全文、`jti` 与签名材料永不下发前端，故页面也从不请求它们。
 */
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ArrowLeft, Refresh } from '@element-plus/icons-vue'
import { ErrorState, PageHeader, RiskLevelTag, SectionCard } from '@/components/common'
import * as preparationApi from '@/api/preparation'
import { usePolling } from '@/composables/usePolling'
import { useAuthStore } from '@/stores/auth'
import { usePreparationStore } from '@/stores/preparation'
import { enumText } from '@/utils/enums'
import { isBizError, toDisplayMessage } from '@/utils/errors'
import { ElMessage } from '@/utils/feedback'
import { remainingMs } from '@/utils/format'
import ApprovalDialog from './components/ApprovalDialog.vue'
import CapabilityPanel from './components/CapabilityPanel.vue'
import CommitConfirmDialog from './components/CommitConfirmDialog.vue'
import FrozenFactsPanel from './components/FrozenFactsPanel.vue'
import PreparationTimeline from './components/PreparationTimeline.vue'
import RiskReasonList from './components/RiskReasonList.vue'
import SlotStatusPanel from './components/SlotStatusPanel.vue'
import type {
  ApprovalDecisionRequest,
  ApprovalDecisionType,
  ApprovalSlot,
  PreparationDetail,
} from '@/types'

/** 详情轮询间隔（fe-05 §3.1：未终态时 5 s） */
const POLL_INTERVAL_MS = 5000

const route = useRoute()
const router = useRouter()
const store = usePreparationStore()
const authStore = useAuthStore()

/** 路由参数是字符串；非法 id 交给后端 404，前端不在这里编造错误 */
const preparationId = computed<number>(() => {
  const raw = route.params.id
  const value = Number(Array.isArray(raw) ? raw[0] : raw)
  return Number.isFinite(value) ? value : 0
})

const detail = computed<PreparationDetail | null>(() => store.detail)

/** 只用于列表标注与四眼置灰；权限判定不在这里做 */
const currentUserId = computed<number>(() => authStore.profile?.id ?? authStore.user?.id ?? 0)

/* ------------------------------------------------------------------ */
/* 弹窗与写操作状态                                                    */
/* ------------------------------------------------------------------ */

const dialogOpen = ref(false)
const dialogSlot = ref<ApprovalSlot | null>(null)
const dialogDecision = ref<ApprovalDecisionType | null>(null)
const submitting = ref(false)
const submitError = ref<string | null>(null)

const commitOpen = ref(false)
const committing = ref(false)
const commitError = ref<string | null>(null)

interface Banner {
  key: string
  type: 'error' | 'warning' | 'info' | 'success'
  text: string
}

/**
 * 写操作被后端按「当前页面看到的形态已经不成立」拒绝时，错误不留在弹窗里，
 * 而是升级成页面级横幅（见 handleWriteError 的说明）。
 */
const opBanner = ref<Banner | null>(null)

/* ------------------------------------------------------------------ */
/* 派生状态（全部来自后端字段，不自行推演）                             */
/* ------------------------------------------------------------------ */

const expired = computed<boolean>(() => {
  const prep = detail.value
  if (prep === null || prep.expires_at === null) {
    return false
  }
  const ms = remainingMs(prep.expires_at)
  return ms !== null && ms <= 0
})

const hasStaleDecision = computed<boolean>(() => store.hasStaleDecision)

/**
 * 「为什么现在不能提交审批决定」。
 * 排序按「最容易被误解」：P0 阻断 → 自批 → 过期 → 指纹失效 → 状态已终局。
 * 前端置灰只是体验，后端 SELF_APPROVAL_FORBIDDEN / DUTY_CONFLICT + DB CHECK 才是兜底。
 */
const blockReason = computed<string | null>(() => {
  const prep = detail.value
  if (prep === null) {
    return '准备单尚未加载完成。'
  }
  if (prep.my_slot === null) {
    return '当前用户不是该准备单任一未决定槽的审批人，因此不能提交决定。'
  }
  if (prep.risk_level === 'P0') {
    return 'P0 是 PDP 规则表的确定性阻断结论（不参与模型判断），任何审批都不能让它通过；请回 Pipeline Studio 调整方案后重新冻结。'
  }
  if (prep.created_by === currentUserId.value) {
    return '申请人不得审批自己提交的准备单（四眼原则）。'
  }
  if (expired.value) {
    return '准备单已超过有效期，过期后任何槽都不能再提交决定，需重新冻结版本。'
  }
  if (hasStaleDecision.value) {
    return '制品或输入指纹已发生变化，原审批全部失效；需重新冻结版本后再审批。'
  }
  if (prep.status !== 'pending' && prep.status !== 'partially_approved') {
    return `准备单当前状态为「${enumText('PreparationStatus', prep.status)}」，不再接受新的审批决定。`
  }
  return null
})

const canDecide = computed<boolean>(() => blockReason.value === null)

/** Commit 入口按 can_commit 显示；指纹失效时即使 can_commit 为真也不再放行重试（必定 409） */
const canCommit = computed<boolean>(() => detail.value?.can_commit === true && !hasStaleDecision.value)

const commitBlockReason = computed<string | null>(() =>
  hasStaleDecision.value ? '指纹已变化，重试 Commit 必然再次被拒；请重新冻结版本。' : null,
)

/** 「重新冻结」是唯一的出路，因此这四种情况下它就是主行动按钮 */
const showRefreeze = computed<boolean>(() => {
  const prep = detail.value
  if (prep === null) {
    return false
  }
  return (
    prep.status === 'rejected' ||
    prep.status === 'expired' ||
    prep.status === 'superseded' ||
    hasStaleDecision.value
  )
})

const headerTitle = computed<string>(() =>
  detail.value === null ? '准备单详情' : `准备单 #${detail.value.id}`,
)

const headerDescription = computed<string>(() => {
  const prep = detail.value
  if (prep === null) {
    return '冻结事实只读；审批决定一经提交不可修改。'
  }
  return `${prep.pipeline_name} · v${prep.version_no} · 提交人 ${prep.creator_name}`
})

const banners = computed<Banner[]>(() => {
  const prep = detail.value
  const list: Banner[] = []
  if (prep !== null) {
    if (prep.risk_level === 'P0') {
      list.push({
        key: 'p0',
        type: 'error',
        text: 'PDP 判定为 P0（已阻断）：该准备单不可被批准，也不存在「继续执行」的入口。这是 engines/policy 规则表的确定性结论，不经过模型判断。',
      })
    }
    if (prep.status === 'rejected') {
      list.push({
        key: 'rejected',
        type: 'error',
        text: '审批已被拒绝：请修正方案后回 Pipeline Studio 重新冻结版本——已落库的决定不能改判。',
      })
    }
    if (prep.status === 'expired') {
      list.push({
        key: 'expired',
        type: 'error',
        text: '准备单已过期：审批与执行入口均已失效，需重新走 Prepare。',
      })
    }
    if (prep.status === 'superseded') {
      list.push({
        key: 'superseded',
        type: 'info',
        text: '该准备单已被同一版本的新准备单取代，此处仅作历史留存。',
      })
    }
    if (hasStaleDecision.value) {
      list.push({
        key: 'stale',
        type: 'error',
        text: '指纹已变化（FINGERPRINT_MISMATCH）：原审批全部失效，本页不提供「忽略并继续」与「重试 Commit」。',
      })
    }
    if (prep.status === 'approved' && prep.can_commit) {
      list.push({
        key: 'approved',
        type: 'success',
        text: '数据槽与安全槽均已通过：可由执行操作员提交执行，Capability 为一次性凭据。',
      })
    }
  }
  // 同一件事只讲一遍：写操作横幅与派生横幅撞车时以派生横幅为准
  if (opBanner.value !== null && !list.some((b) => b.key === opBanner.value?.key)) {
    list.push(opBanner.value)
  }
  return list
})

/** 是否已到「无需再轮询」的形态 */
const isSettled = computed<boolean>(() => {
  const prep = detail.value
  if (prep === null) {
    return false
  }
  if (prep.status === 'rejected' || prep.status === 'expired' || prep.status === 'superseded') {
    return true
  }
  // approved 且已不可 Commit = 已被提交执行，后续进度属于运行中心，本页不必再问
  return prep.status === 'approved' && !prep.can_commit
})

const detailPolling = usePolling(
  async () => {
    await store.loadDetail(preparationId.value)
  },
  {
    intervalMs: POLL_INTERVAL_MS,
    // 首拍由 onMounted 触发，避免挂载瞬间连发两次相同请求
    immediate: false,
    shouldStop: () => isSettled.value,
  },
)

/* ------------------------------------------------------------------ */
/* 交互                                                                */
/* ------------------------------------------------------------------ */

const ERROR_GUIDE: Record<string, string> = {
  SELF_APPROVAL_FORBIDDEN: '四眼原则：审批人不得等于准备单发起人，需由另一位审批人处理。',
  DUTY_CONFLICT: '高风险操作禁止同一人兼任多个审批职责；请到项目设置调整角色后重试。',
  APPROVAL_INCOMPLETE: '审批槽尚未集齐，请等待另一槽的决定。',
  FINGERPRINT_MISMATCH: '制品或输入指纹已变化，原审批全部失效；需重新冻结版本。',
  PREPARATION_EXPIRED: '准备单已过期，请回 Pipeline Studio 重新冻结版本。',
  CONFLICT: '准备单状态已被他人变更，已为你刷新最新状态，请重新判断。',
  CAPABILITY_REPLAYED: '该 Capability 已被消费（一次性凭据），重复提交不会产生第二次运行。',
  CAPABILITY_EXPIRED: 'Capability 已过期，需重新走 Prepare / Approve。',
  CAPABILITY_INVALID: 'Capability 无效；请确认绑定的制品指纹是否已变化。',
  IDEMPOTENCY_CONFLICT: '同一幂等键对应了不同请求体，已停止重试；请刷新后重新发起。',
  POLICY_BLOCKED: '命中 P0/P1 策略阻断，执行被拒绝。',
}

/** 这些错误说明「页面看到的形态已经过期」，重试多少次都一样 */
const PAGE_LEVEL_CODES: readonly string[] = Object.keys(ERROR_GUIDE)

/**
 * 写操作失败的两种归处（CONVENTIONS §5）：
 * - 「提交内容本身有问题」→ 留在弹窗里（AsyncTaskPanel），用户改一改再试；
 * - 「页面看到的形态已过期」→ 关弹窗 + 刷新详情 + 顶部横幅。
 *   自批、职责互斥、指纹失效、过期、被他人抢先决定都属于后者，
 *   把用户留在弹窗里反复点「确定」只是在消耗耐心。
 */
async function handleWriteError(e: unknown, keepInDialog: () => void): Promise<void> {
  if (!isBizError(e) || !PAGE_LEVEL_CODES.includes(e.code)) {
    keepInDialog()
    return
  }
  dialogOpen.value = false
  commitOpen.value = false
  const guide = ERROR_GUIDE[e.code]
  opBanner.value = {
    key: e.code,
    type: e.code === 'IDEMPOTENCY_CONFLICT' ? 'warning' : 'error',
    text: guide === undefined ? toDisplayMessage(e) : `${toDisplayMessage(e)}｜${guide}`,
  }
  await store.loadDetail(preparationId.value)
}

/** 手动刷新单独一件事：直接绑 detailLoading 会让按钮每 5 秒跟着轮询闪一次 */
const refreshing = ref(false)

async function reload(): Promise<void> {
  refreshing.value = true
  try {
    await store.loadDetail(preparationId.value)
  } finally {
    refreshing.value = false
  }
}

function backToList(): void {
  void router.push({ name: 'preparation-list' })
}

function goStudio(): void {
  const prep = detail.value
  if (prep === null) {
    return
  }
  void router.push({ name: 'pipeline-studio', params: { id: String(prep.pipeline_id) } })
}

function openDecision(slot: ApprovalSlot, decision: ApprovalDecisionType): void {
  dialogSlot.value = slot
  dialogDecision.value = decision
  submitError.value = null
  dialogOpen.value = true
}

function onApprove(slot: ApprovalSlot): void {
  openDecision(slot, 'approve')
}

function onReject(slot: ApprovalSlot): void {
  openDecision(slot, 'reject')
}

function onReturn(slot: ApprovalSlot): void {
  openDecision(slot, 'return')
}

function closeDecisionDialog(): void {
  dialogOpen.value = false
}

async function onSubmitDecision(
  payload: ApprovalDecisionRequest,
  idempotencyKey: string,
): Promise<void> {
  if (submitting.value) {
    return
  }
  submitting.value = true
  submitError.value = null
  try {
    await preparationApi.submitDecision(preparationId.value, payload, {
      headers: { 'Idempotency-Key': idempotencyKey },
    })
    dialogOpen.value = false
    opBanner.value = null
    ElMessage.success('审批决定已提交（approval_decision 只增不改）')
    // 不本地改 decided_slots：两槽是否齐备由服务端 derive_status 判定
    await store.refreshAfterDecision(preparationId.value)
  } catch (e) {
    await handleWriteError(e, () => {
      submitError.value = toDisplayMessage(e)
    })
  } finally {
    submitting.value = false
  }
}

function openCommitDialog(): void {
  commitError.value = null
  commitOpen.value = true
}

function closeCommitDialog(): void {
  commitOpen.value = false
}

/**
 * `idempotencyKey` 由 `CommitConfirmDialog` 按 fe-05 §6 生成（一次用户意图 = 一个 key，
 * 弹窗内重试复用、关闭后重置），在这里透传给接口。
 *
 * 不能用 client.ts 的按请求自动注入代替：那个 key 是「每个 request config 一个」，
 * 用户点提交 → 超时 → 再点一次，是两个 config、两个 key，服务端会当成两次独立意图。
 */
async function onCommit(idempotencyKey: string): Promise<void> {
  if (committing.value) {
    return
  }
  committing.value = true
  commitError.value = null
  try {
    const result = await preparationApi.commitPreparation(preparationId.value, {
      headers: { 'Idempotency-Key': idempotencyKey },
    })
    commitOpen.value = false
    opBanner.value = null
    ElMessage.success('已提交执行，正在前往运行中心…')
    await store.refreshAfterCommit(preparationId.value)
    await router.push({ name: 'run-detail', params: { id: String(result.run_id) } })
  } catch (e) {
    await handleWriteError(e, () => {
      commitError.value = toDisplayMessage(e)
    })
  } finally {
    committing.value = false
  }
}

onMounted(() => {
  void store.loadDetail(preparationId.value)
  detailPolling.start()
})

// 详情含资源范围与敏感分级：离开路由即清空，不落 localStorage / sessionStorage
onUnmounted(() => {
  store.clearDetail()
})
</script>

<template>
  <div class="prep-detail">
    <PageHeader :title="headerTitle" :description="headerDescription">
      <template #actions>
        <el-button size="small" :icon="ArrowLeft" @click="backToList">返回列表</el-button>
        <el-button size="small" :icon="Refresh" :loading="refreshing" @click="reload">
          刷新
        </el-button>
        <el-button v-if="showRefreeze" size="small" type="primary" plain @click="goStudio">
          返回 Pipeline Studio 重新冻结
        </el-button>
        <!-- Commit 只在后端下发 can_commit 时出现；不能点的时候必须说明原因 -->
        <el-tooltip
          v-if="detail !== null && detail.can_commit"
          :disabled="canCommit"
          :content="commitBlockReason ?? ''"
          placement="bottom"
        >
          <span class="prep-detail__action">
            <el-button type="primary" size="small" :disabled="!canCommit" @click="openCommitDialog">
              提交执行（Commit）
            </el-button>
          </span>
        </el-tooltip>
      </template>

      <template #notice>
        <div v-if="banners.length > 0" class="prep-detail__banners">
          <el-alert
            v-for="banner in banners"
            :key="banner.key"
            class="prep-detail__banner"
            :type="banner.type"
            :closable="false"
            show-icon
          >
            {{ banner.text }}
          </el-alert>
        </div>
      </template>
    </PageHeader>

    <!-- 三态：错误优先于空态；加载失败绝不退化成「没有这张准备单」 -->
    <div v-if="store.detailError !== null && detail === null" class="prep-detail__error">
      <ErrorState :message="store.detailError" @retry="reload" />
      <el-button size="small" @click="backToList">返回列表</el-button>
    </div>

    <div v-else-if="detail === null" class="prep-detail__columns">
      <div class="prep-detail__main">
        <div class="elt-skeleton elt-skeleton--block" style="height: 260px" />
        <div class="elt-skeleton elt-skeleton--block" style="height: 200px" />
      </div>
      <div class="prep-detail__side">
        <div class="elt-skeleton elt-skeleton--block" style="height: 320px" />
      </div>
    </div>

    <div v-else class="prep-detail__columns">
      <div class="prep-detail__main">
        <SectionCard title="冻结事实（只读）">
          <FrozenFactsPanel :preparation="detail" :readonly="true" />
        </SectionCard>

        <SectionCard title="审批槽状态（双槽齐备后方可提交执行）">
          <SlotStatusPanel
            :required-slots="detail.required_slots"
            :decisions="detail.decisions"
            :my-slot="detail.my_slot"
            :can-decide="canDecide"
            :block-reason="blockReason"
            @approve="onApprove"
            @reject="onReject"
            @return="onReturn"
          />
        </SectionCard>

        <SectionCard title="风险判定与命中原因">
          <template #extra>
            <RiskLevelTag :level="detail.risk_level" :reasons="store.riskReasons" size="small" />
          </template>
          <RiskReasonList :reasons="store.riskReasons" />
        </SectionCard>

        <SectionCard title="Capability（一次性执行凭据）">
          <CapabilityPanel :preparation="detail" />
        </SectionCard>
      </div>

      <div class="prep-detail__side">
        <SectionCard title="时间线" collapsible :skeleton-height="320">
          <PreparationTimeline :items="store.timeline" :expires-at="detail.expires_at" />
        </SectionCard>
      </div>
    </div>

    <ApprovalDialog
      :open="dialogOpen"
      :preparation="detail"
      :slot="dialogSlot"
      :decision="dialogDecision"
      :submitting="submitting"
      :error="submitError"
      @submit="onSubmitDecision"
      @cancel="closeDecisionDialog"
    />

    <CommitConfirmDialog
      :open="commitOpen"
      :preparation="detail"
      :submitting="committing"
      :error="commitError"
      @confirm="onCommit"
      @cancel="closeCommitDialog"
    />
  </div>
</template>

<style scoped>
.prep-detail {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.prep-detail__banners {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.prep-detail__banner :deep(.el-alert__content) {
  font-size: 12px;
  line-height: 1.7;
}

.prep-detail__action {
  display: inline-flex;
}

.prep-detail__error {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 8px;
}

.prep-detail__columns {
  display: grid;
  grid-template-columns: minmax(0, 2.2fr) minmax(0, 1fr);
  gap: 12px;
  align-items: start;
}

.prep-detail__main,
.prep-detail__side {
  display: flex;
  flex-direction: column;
  gap: 12px;
  min-width: 0;
}

@media (max-width: 1180px) {
  .prep-detail__columns {
    grid-template-columns: minmax(0, 1fr);
  }
}
</style>
