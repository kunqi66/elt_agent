<script setup lang="ts">
/**
 * 准备单列表的列定义（fe-05 §6）。
 *
 * 只负责「怎么显示一行」与「点了审批去哪」两件事：
 * 数据加载、分页、空态、错误态都由外层 `QueryTable` + store 承担。
 * 这样列表页换数据源（轮询 / 手动刷新）时，列定义一行都不用改。
 *
 * 「可审批」只认后端下发的 `my_slot`，不在这里重算四眼规则——
 * 前端算出来的「我能审」必然与后端不一致，那才是真正的坑。
 */
import { onUnmounted, ref } from 'vue'
import { ArtifactHash, EnvBadge, RiskLevelTag, StatusTag } from '@/components/common'
import { formatDateTime, formatRemaining, remainingMs } from '@/utils/format'
import type { ApprovalSlot, PreparationSummary } from '@/types'

const props = defineProps<{ currentUserId: number }>()

const emit = defineEmits<{
  review: [payload: { preparation_id: number; slot: ApprovalSlot }]
}>()

/** 30 秒一拍的本地时钟：只驱动「剩余有效期」列，不必每秒重绘整表 */
const now = ref<number>(Date.now())
const timer = setInterval(() => {
  now.value = Date.now()
}, 30_000)
onUnmounted(() => clearInterval(timer))

function remainingLabel(expiresAt: string | null): string {
  void now.value
  const ms = remainingMs(expiresAt)
  // null = 后端不设有效期，与「已过期」是两回事，不能都显示成 0
  return ms === null ? '—' : formatRemaining(ms)
}

function remainingClass(expiresAt: string | null): string {
  void now.value
  const ms = remainingMs(expiresAt)
  if (ms === null) {
    return ''
  }
  if (ms <= 0) {
    return 'prep-remaining--danger'
  }
  if (ms < 3_600_000) {
    return 'prep-remaining--warn'
  }
  return ''
}

function isMine(createdBy: number): boolean {
  return props.currentUserId > 0 && createdBy === props.currentUserId
}

/**
 * 插槽 `row` 的静态类型是 Element Plus 的 `DefaultRow`，与我们的 DTO 没有类型关联。
 *
 * 为什么不用 `#default="{ row }: { row: PreparationSummary }"` 直接注解：
 * 插槽类型是函数类型，在 `strictFunctionTypes` 下参数位置**逆变**检查，
 * 而 `DefaultRow` 无法赋给 `PreparationSummary`，注解必然报 TS2345。
 *
 * 为什么也不干脆不加注解：那样 `row` 会退化成 Element Plus 的行类型，
 * 格子里写错字段名不会有人拦。在这里收一次口，格子内仍是完整类型检查。
 */
function prepOf(row: unknown): PreparationSummary {
  return row as PreparationSummary
}

function onReview(row: PreparationSummary): void {
  if (row.my_slot === null) {
    return
  }
  emit('review', { preparation_id: row.id, slot: row.my_slot })
}
</script>

<template>
  <el-table-column label="准备单" width="92">
    <template #default="{ row }">
      <span class="prep-id elt-mono">#{{ prepOf(row).id }}</span>
    </template>
  </el-table-column>

  <el-table-column label="Pipeline" min-width="180" show-overflow-tooltip>
    <template #default="{ row }">
      <span class="prep-name">{{ prepOf(row).pipeline_name }}</span>
    </template>
  </el-table-column>

  <el-table-column label="版本" width="76">
    <template #default="{ row }">
      <span class="elt-mono">v{{ prepOf(row).version_no }}</span>
    </template>
  </el-table-column>

  <el-table-column label="制品指纹" width="112">
    <template #default="{ row }">
      <!-- 悬停给完整 64 位：审计对账靠它，缩略只是为了让表格能放下 -->
      <span :title="prepOf(row).artifact_sha256">
        <ArtifactHash :hash="prepOf(row).artifact_sha256" display="short" />
      </span>
    </template>
  </el-table-column>

  <el-table-column label="风险" width="112">
    <template #default="{ row }">
      <RiskLevelTag :level="prepOf(row).risk_level" size="small" />
    </template>
  </el-table-column>

  <el-table-column label="敏感级别" width="96">
    <template #default="{ row }">
      <StatusTag kind="SensitiveLevel" :value="prepOf(row).sensitive_level" />
    </template>
  </el-table-column>

  <el-table-column label="环境" width="88">
    <template #default="{ row }">
      <EnvBadge :env="prepOf(row).environment" />
    </template>
  </el-table-column>

  <el-table-column label="状态" width="104">
    <template #default="{ row }">
      <StatusTag kind="PreparationStatus" :value="prepOf(row).status" />
    </template>
  </el-table-column>

  <el-table-column label="提交人" width="120">
    <template #default="{ row }">
      <span class="prep-creator">
        {{ prepOf(row).creator_name }}
        <el-tag
          v-if="isMine(prepOf(row).created_by)"
          size="small"
          type="info"
          effect="plain"
          disable-transitions
        >
          我发起
        </el-tag>
      </span>
    </template>
  </el-table-column>

  <el-table-column label="冻结时间" width="168">
    <template #default="{ row }">
      <span class="elt-mono prep-time">{{ formatDateTime(prepOf(row).frozen_at) }}</span>
    </template>
  </el-table-column>

  <el-table-column label="剩余有效期" width="126">
    <template #default="{ row }">
      <span class="prep-remaining" :class="remainingClass(prepOf(row).expires_at)">
        {{ remainingLabel(prepOf(row).expires_at) }}
      </span>
    </template>
  </el-table-column>

  <el-table-column label="操作" width="96" fixed="right">
    <template #default="{ row }">
      <!-- 行点击进详情；这里只在后端说「该槽归你」时才多给一个直达按钮 -->
      <el-button
        v-if="prepOf(row).my_slot !== null"
        link
        type="primary"
        size="small"
        @click.stop="onReview(prepOf(row))"
      >
        审批
      </el-button>
      <span v-else class="prep-muted">—</span>
    </template>
  </el-table-column>
</template>

<style scoped>
.prep-id {
  font-size: 12px;
  color: var(--elt-text-secondary);
}

.prep-name {
  font-size: 13px;
  color: var(--elt-text-primary);
}

.prep-creator {
  display: inline-flex;
  gap: 4px;
  align-items: center;
  font-size: 12px;
  color: var(--elt-text-primary);
}

.prep-time {
  font-size: 12px;
  color: var(--elt-text-secondary);
}

.prep-remaining {
  font-size: 12px;
  color: var(--elt-text-secondary);
}

.prep-remaining--warn {
  font-weight: 600;
  color: var(--elt-warn);
}

.prep-remaining--danger {
  font-weight: 600;
  color: var(--elt-danger);
}

.prep-muted {
  color: var(--elt-text-muted);
}
</style>
