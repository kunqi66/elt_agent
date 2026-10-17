<script setup lang="ts">
/**
 * 待审批准备单 + 双槽状态（fe-02 §6.3）。
 *
 * 两个刻意的实现选择：
 * 1. **槽位只按 required_slots 渲染**，不做「补齐」——缺槽说明后端数据异常，
 *    前端补一个占位只会把异常藏起来（fe-02 §9 的反例）。
 * 2. **槽位状态用「已决定 / 待审批」而不是「已通过」**：槽里出了决定不等于准备单放行
 *    （可能是拒绝或退回），用绿色「已通过」会被误读成可以执行了。
 */
import { computed } from 'vue'
import { RouterLink } from 'vue-router'
import {
  ArtifactHash,
  EmptyState,
  EnvBadge,
  ErrorState,
  RiskLevelTag,
  SectionCard,
  StatusTag,
} from '@/components/common'
import { toDisplayMessage } from '@/utils/errors'
import { formatDateTime, remainingMs } from '@/utils/format'
import type { BizError } from '@/api/client'
import type { ApprovalSlot, PendingPreparationBrief } from '@/types'

const props = defineProps<{
  items: PendingPreparationBrief[]
  loading: boolean
  error: BizError | null
}>()

const emit = defineEmits<{
  (e: 'open', preparationId: number): void
  /** 非权限类失败（5xx / 网络）允许原地重试；403 不给重试按钮（fe-02 §7） */
  (e: 'retry'): void
}>()

/** 这三类拒绝是「不该重试」的终态：重试只会再被拒一次 */
const DENIED_CODES: readonly string[] = ['FORBIDDEN', 'ROLE_REQUIRED', 'TENANT_MISMATCH']

const denied = computed<boolean>(
  () => props.error !== null && DENIED_CODES.includes(props.error.code),
)

interface SlotRow {
  slot: ApprovalSlot
  decided: boolean
}

interface PendingRow {
  item: PendingPreparationBrief
  slots: SlotRow[]
  expire: { label: string; tone: 'danger' | 'warn' } | null
}

/** 距过期不足 24 小时才提示：提前太久会让「即将过期」失去警示意义 */
const EXPIRE_SOON_MS = 24 * 60 * 60 * 1000

function expireOf(item: PendingPreparationBrief): PendingRow['expire'] {
  const remaining = remainingMs(item.expires_at)
  if (remaining === null) {
    return null
  }
  if (remaining <= 0) {
    return { label: '已过期', tone: 'danger' }
  }
  if (remaining < EXPIRE_SOON_MS) {
    return { label: '即将过期', tone: 'warn' }
  }
  return null
}

const rows = computed<PendingRow[]>(() =>
  props.items.slice(0, 5).map((item) => ({
    item,
    slots: item.required_slots.map((slot) => ({
      slot,
      decided: item.decided_slots.includes(slot),
    })),
    expire: expireOf(item),
  })),
)

const isEmpty = computed<boolean>(() => props.items.length === 0 && props.error === null)
</script>

<template>
  <SectionCard title="待审批准备单" :loading="loading" :skeleton-height="264">
    <template #extra>
      <span v-if="rows.length > 0" class="pending__count elt-mono">{{ rows.length }}</span>
      <RouterLink v-if="rows.length > 0" class="pending__all" to="/approvals">全部待办</RouterLink>
    </template>

    <!-- error 优先于 empty：抓取失败时绝不能显示「没有待审批」 -->
    <EmptyState
      v-if="denied"
      variant="no-permission"
      title="无审批权限"
      description="你的职责槽读不到审批待办。如需处理审批，请联系项目管理员授予数据审批槽或安全审批槽。"
    />

    <ErrorState
      v-else-if="error !== null"
      :message="toDisplayMessage(error)"
      :request-id="error.requestId"
      @retry="emit('retry')"
    />

    <EmptyState
      v-else-if="isEmpty"
      variant="no-data"
      title="当前没有待审批的准备单"
      description="当有准备单进入待审批、且需要你的职责槽时，会出现在这里。"
    />

    <ul v-else class="pending">
      <li v-for="row in rows" :key="row.item.id" class="pending__item">
        <button type="button" class="pending__hit" @click="emit('open', row.item.id)">
          <span class="pending__head">
            <RiskLevelTag :level="row.item.risk_level" />
            <span class="pending__name" :title="row.item.pipeline_name">{{ row.item.pipeline_name }}</span>
            <span class="pending__version elt-mono">v{{ row.item.version_no }}</span>
            <span
              v-if="row.expire !== null"
              class="pending__expire"
              :class="`pending__expire--${row.expire.tone}`"
            >
              {{ row.expire.label }}
            </span>
          </span>

          <span class="pending__meta">
            <ArtifactHash :hash="row.item.artifact_sha256" display="short" />
            <EnvBadge :env="row.item.environment" />
            <StatusTag kind="PreparationStatus" :value="row.item.status" />
            <StatusTag kind="SensitiveLevel" :value="row.item.sensitive_level" />
          </span>

          <span class="pending__slots">
            <span v-for="slot in row.slots" :key="slot.slot" class="slot">
              <StatusTag kind="ApprovalSlot" :value="slot.slot" />
              <el-tag
                size="small"
                :type="slot.decided ? 'info' : 'warning'"
                disable-transitions
              >
                {{ slot.decided ? '已决定' : '待审批' }}
              </el-tag>
            </span>
          </span>

          <span class="pending__foot">
            {{ row.item.creator_name }} 发起 · 冻结于 {{ formatDateTime(row.item.frozen_at) }}
          </span>
        </button>
      </li>
    </ul>
  </SectionCard>
</template>

<style scoped>
.pending__count {
  font-size: 12px;
  color: var(--elt-text-secondary);
}

.pending__all {
  font-size: 12px;
  color: var(--elt-accent);
  text-decoration: none;
}

.pending__all:hover {
  text-decoration: underline;
}

.pending {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin: 0;
  padding: 0;
  list-style: none;
}

.pending__item {
  margin: 0;
}

.pending__hit {
  display: flex;
  flex-direction: column;
  gap: 6px;
  width: 100%;
  padding: 10px;
  text-align: left;
  cursor: pointer;
  background-color: var(--elt-bg-elevated);
  border: 1px solid transparent;
  border-radius: var(--elt-radius-control);
  transition: border-color 0.15s ease;
}

.pending__hit:hover,
.pending__hit:focus-visible {
  border-color: var(--elt-accent);
  outline: none;
}

@media (prefers-reduced-motion: reduce) {
  .pending__hit {
    transition: none;
  }
}

.pending__head {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
}

.pending__name {
  overflow: hidden;
  font-size: 13px;
  font-weight: 600;
  color: var(--elt-text-primary);
  text-overflow: ellipsis;
  white-space: nowrap;
}

.pending__version {
  flex: none;
  font-size: 11px;
  color: var(--elt-text-muted);
}

.pending__expire {
  flex: none;
  margin-left: auto;
  padding: 0 6px;
  font-size: 11px;
  line-height: 16px;
  border-radius: var(--elt-radius-pill);
}

.pending__expire--danger {
  color: var(--elt-danger);
  background-color: var(--elt-badge-danger);
}

.pending__expire--warn {
  color: var(--elt-warn);
  background-color: var(--elt-badge-warn);
}

.pending__meta {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
}

.pending__slots {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
}

.slot {
  display: inline-flex;
  align-items: center;
  gap: 4px;
}

.pending__foot {
  font-size: 11px;
  line-height: 16px;
  color: var(--elt-text-muted);
}
</style>
