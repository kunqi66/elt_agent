<script setup lang="ts">
/**
 * 元数据 Profile 版本列表（fe-03 §5.4）。
 *
 * 为什么保留历史版本：探查结果是「某时刻的元数据快照」，
 * 生成方案时引用的是**具体版本号**，事后核对必须能回到那一版，
 * 所以这里不做「只显示最新」的简化。
 *
 * 注意：store.loadProfiles 失败时**保留上一份列表**并把失败写进 `error`（不清空），
 * 因为空列表会被读成「无探查记录」——而详情页其他区块可能同时在列着 v1，
 * 两处矛盾时用户会怀疑数据本身。因此这里有独立的 error 态：加载失败 ≠ 没有记录。
 */
import { computed } from 'vue'
import { formatDateTime, formatInt, formatRemaining, remainingMs } from '@/utils/format'
import ProbeStatusTag from './ProbeStatusTag.vue'
import type { MetadataProfileBrief } from '@/types'

const props = withDefaults(
  defineProps<{
    profiles: MetadataProfileBrief[]
    loading?: boolean
    /** 加载失败文案；与「确实没有探查记录」是两种不同的空态 */
    error?: string | null
    /** 当前查看的版本；用于把「查看」按钮换成「查看中」 */
    activeVersion?: number | null
  }>(),
  { loading: false, error: null, activeVersion: null },
)

const emit = defineEmits<{ select: [version: number] }>()

const sorted = computed<MetadataProfileBrief[]>(() =>
  [...props.profiles].sort((a, b) => b.version - a.version),
)

function expiresText(expiresAt: string | null): string {
  const ms = remainingMs(expiresAt)
  if (ms === null) {
    return '不过期'
  }
  return ms <= 0 ? `${formatDateTime(expiresAt)}（已过期）` : `剩余 ${formatRemaining(ms)}`
}
</script>

<template>
  <div class="profile-list">
    <div v-if="loading" class="elt-skeleton elt-skeleton--block profile-list__skeleton" />
    <!-- error 优先于空态：加载失败时列表可能是空的，但那是失败，不是「没有记录」 -->
    <p v-else-if="error !== null" class="profile-list__empty profile-list__empty--error">
      {{ error }}
    </p>
    <p v-else-if="sorted.length === 0" class="profile-list__empty">
      还没有探查记录。点击「发起探查」获取表结构，探查完成前不能生成方案。
    </p>

    <el-table v-else :data="sorted" size="small" :border="false">
      <el-table-column label="版本" width="84">
        <template #default="{ row }">
          <el-button
            link
            type="primary"
            size="small"
            @click="emit('select', row.version as number)"
          >v{{ row.version }}</el-button>
          <span v-if="row.version === activeVersion" class="profile-list__active">查看中</span>
        </template>
      </el-table-column>
      <el-table-column label="状态" width="140">
        <template #default="{ row }">
          <ProbeStatusTag :status="row.status" :expires-at="row.expires_at" />
        </template>
      </el-table-column>
      <el-table-column label="表数" width="80" align="right">
        <template #default="{ row }">{{ formatInt(row.tables_count) }}</template>
      </el-table-column>
      <el-table-column label="探查时间" width="170">
        <template #default="{ row }">{{ formatDateTime(row.created_at) }}</template>
      </el-table-column>
      <el-table-column label="有效期">
        <template #default="{ row }">{{ expiresText(row.expires_at) }}</template>
      </el-table-column>
    </el-table>
  </div>
</template>

<style scoped>
.profile-list__skeleton {
  height: 72px;
}

.profile-list__empty {
  margin: 0;
  font-size: 12px;
  line-height: 18px;
  color: var(--elt-text-muted);
}

/* 失败用告警色：与「还没有记录」共用同一段文案样式会让人把它当成正常空态读过去 */
.profile-list__empty--error {
  color: var(--elt-danger);
}

.profile-list__active {
  margin-left: 4px;
  font-size: 11px;
  color: var(--elt-text-muted);
}
</style>
