<script setup lang="ts">
/**
 * 准备单时间线与有效期倒计时（fe-05 §3.2 / §6）。
 *
 * 倒计时**只做本地展示**：每秒刷新的是「距离 expires_at 还有多久」这个差值，
 * 不因此发请求。真实过期以服务端返回的 PREPARATION_EXPIRED 为准——
 * 本地时钟不可信，把它当判据会让用户以为「倒计时没到就一定能提交」。
 */
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { StatusTag, SensitiveText } from '@/components/common'
import { formatCountdown, formatDateTime, formatRemaining, remainingMs } from '@/utils/format'
import type { ApprovalSlot, PreparationTimelineEvent, PreparationTimelineItem } from '@/types'

const props = defineProps<{
  items: PreparationTimelineItem[]
  expiresAt: string | null
}>()

const now = ref<number>(Date.now())
let timer: ReturnType<typeof setInterval> | null = null

onMounted(() => {
  timer = setInterval(() => {
    now.value = Date.now()
  }, 1000)
})

onUnmounted(() => {
  if (timer !== null) {
    clearInterval(timer)
  }
})

const remaining = computed<number | null>(() => {
  // 读一次 now 建立依赖，让倒计时每秒重算；remainingMs 自身是纯计算
  void now.value
  return remainingMs(props.expiresAt)
})

/** 无 expires_at = 不设有效期（后端语义），不是「已过期」 */
const noExpiry = computed<boolean>(() => remaining.value === null)
const expired = computed<boolean>(() => remaining.value !== null && remaining.value <= 0)
/** 不足 1 小时转 warning：给审批人一个「来不及了」的视觉提示 */
const urgent = computed<boolean>(() => remaining.value !== null && remaining.value > 0 && remaining.value < 3_600_000)

const countdownText = computed<string>(() => (noExpiry.value ? '—' : formatCountdown(remaining.value)))
const remainingText = computed<string>(() => (noExpiry.value ? '不设有效期' : formatRemaining(remaining.value)))

/**
 * 时间线事件的中文文案。
 * 基座的 `utils/enums.ts` 没有登记 `PreparationTimelineEvent`（EnumKind 里没有它），
 * 因此这一份映射暂存在本文件；若基座后续登记该枚举，删除此处改用 `enumText`。
 */
const EVENT_TEXT: Record<PreparationTimelineEvent, string> = {
  created: '创建准备单',
  approved_slot: '审批通过',
  rejected_slot: '审批拒绝',
  returned_slot: '退回补充',
  expired: '有效期届满',
  superseded: '被取代 / 作废',
  committed: '提交执行',
}

/** 节点配色只引用设计令牌，不在页面里发明色值（CONVENTIONS §6） */
const EVENT_COLOR: Record<PreparationTimelineEvent, string> = {
  created: 'var(--elt-accent)',
  approved_slot: 'var(--elt-success)',
  rejected_slot: 'var(--elt-danger)',
  returned_slot: 'var(--elt-warn)',
  expired: 'var(--elt-danger)',
  superseded: 'var(--elt-warn)',
  committed: 'var(--elt-success)',
}

function slotLabel(slot: ApprovalSlot | undefined): string | null {
  if (slot === undefined) {
    return null
  }
  return slot === 'checker_data' ? '数据审批槽' : '安全审批槽'
}
</script>

<template>
  <div class="prep-timeline">
    <div class="prep-timeline__expiry">
      <span class="prep-timeline__expiry-label">剩余有效期</span>
      <span
        class="prep-timeline__expiry-value elt-mono"
        :class="{
          'is-danger': expired,
          'is-warn': urgent,
        }"
      >
        {{ expired ? '已过期' : countdownText }}
      </span>
      <span class="prep-timeline__expiry-sub">{{ remainingText }}</span>
    </div>

    <p v-if="items.length === 0" class="prep-timeline__empty">暂无时间线记录。</p>

    <el-timeline v-else class="prep-timeline__list">
      <el-timeline-item
        v-for="(item, index) in items"
        :key="`${item.at}-${index}`"
        :timestamp="formatDateTime(item.at)"
        placement="top"
        size="normal"
        :color="EVENT_COLOR[item.event]"
      >
        <div class="prep-timeline__row">
          <span class="prep-timeline__event">{{ EVENT_TEXT[item.event] }}</span>
          <StatusTag v-if="slotLabel(item.slot) !== null" kind="ApprovalSlot" :value="item.slot" />
          <span v-if="item.actor_name !== undefined && item.actor_name !== ''" class="prep-timeline__actor">
            {{ item.actor_name }}
          </span>
        </div>
        <SensitiveText
          v-if="item.detail !== undefined && item.detail !== ''"
          :value="item.detail"
          multiline
          class="prep-timeline__detail"
        />
      </el-timeline-item>
    </el-timeline>
  </div>
</template>

<style scoped>
.prep-timeline {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.prep-timeline__expiry {
  display: flex;
  gap: 8px;
  align-items: baseline;
  padding: 6px 10px;
  background-color: var(--elt-bg-elevated);
  border: 1px solid var(--elt-border-subtle);
  border-radius: var(--elt-radius-control);
}

.prep-timeline__expiry-label {
  font-size: 12px;
  color: var(--elt-text-secondary);
}

.prep-timeline__expiry-value {
  font-size: 15px;
  font-weight: 700;
  color: var(--elt-text-primary);
}

.prep-timeline__expiry-value.is-warn {
  color: var(--elt-warn);
}

.prep-timeline__expiry-value.is-danger {
  color: var(--elt-danger);
}

.prep-timeline__expiry-sub {
  font-size: 11px;
  color: var(--elt-text-muted);
}

.prep-timeline__empty {
  margin: 0;
  font-size: 12px;
  color: var(--elt-text-muted);
}

.prep-timeline__row {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  align-items: center;
}

.prep-timeline__event {
  font-size: 12px;
  font-weight: 600;
  color: var(--elt-text-primary);
}

.prep-timeline__actor {
  font-size: 12px;
  color: var(--elt-text-secondary);
}

.prep-timeline__detail {
  margin-top: 2px;
  font-size: 12px;
  line-height: 1.6;
  color: var(--elt-text-secondary);
}
</style>
