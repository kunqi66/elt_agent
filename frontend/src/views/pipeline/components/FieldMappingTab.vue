<script setup lang="ts">
/**
 * 字段映射 / Schema 对照页签（fe-04 §6.4）。
 *
 * 这一页承担两类门禁定位：
 * - `plan` 定位（如 `field_mappings[3].target_type`）→ 高亮对应行；
 * - `source_table` 定位（源表在探查快照里找不到/结构漂移）→ 顶部横幅指出是哪张源表，
 *   因为这类问题没有「行」可指，指到表更实在。
 *
 * 默认**不**只看差异：刚生成的 Plan 里绝大多数行是 `same`，
 * 默认过滤掉会让人以为「没生成任何映射」。
 *
 * `FieldTransform` / `FieldChange` / `MaskingMethod` 不在 EnumKind 内
 * （它们是 Plan 内部词汇），文案只能本地维护——与 StudioPhase 同理。
 */
import { computed, ref } from 'vue'
import { EmptyState } from '@/components/common'
import type { EtlPlanInput, FieldChange, FieldMapping, FieldTransform, MaskingMethod } from '@/types'

const props = defineProps<{
  mappings: FieldMapping[]
  inputs: EtlPlanInput[]
  /** 当前激活 finding 的 plan 路径；用于行高亮 */
  activePath: string | null
  /** 当前激活 finding 的 source_table 定位 */
  sourceTableLocator: { data_source_id: number; schema: string; table: string } | null
}>()

const CHANGE_META: Record<FieldChange, { label: string; cls: string }> = {
  same: { label: '未变', cls: 'is-same' },
  added: { label: '新增', cls: 'is-added' },
  removed: { label: '删除', cls: 'is-removed' },
  modified: { label: '变更', cls: 'is-modified' },
}

const TRANSFORM_TEXT: Record<FieldTransform, string> = {
  none: '直传',
  cast: '类型转换',
  trim: '去空白',
  concat: '拼接',
  mask: '脱敏',
  hash: '哈希',
}

const MASKING_TEXT: Record<MaskingMethod, string> = {
  mask_middle: '中间打码',
  hash: '哈希',
  truncate: '截断',
}

const onlyDiff = ref<boolean>(false)
const onlySensitive = ref<boolean>(false)
const keyword = ref<string>('')

/** `field_mappings[3].target_type` → 3；解析不出来就当没有定位 */
const activeIndex = computed<number | null>(() => {
  const path = props.activePath
  if (path === null) {
    return null
  }
  const m = /^field_mappings\[(\d+)\]/.exec(path)
  return m === null ? null : Number(m[1])
})

const rows = computed<Array<{ mapping: FieldMapping; index: number }>>(() =>
  props.mappings
    .map((mapping, index) => ({ mapping, index }))
    .filter(({ mapping }) => {
      if (onlyDiff.value && mapping.change === 'same') {
        return false
      }
      if (onlySensitive.value && mapping.masking === null) {
        return false
      }
      const kw = keyword.value.trim().toLowerCase()
      if (kw === '') {
        return true
      }
      return (
        mapping.source_field.toLowerCase().includes(kw) || mapping.target_field.toLowerCase().includes(kw)
      )
    }),
)

const diffCount = computed<number>(() => props.mappings.filter((m) => m.change !== 'same').length)
const sensitiveCount = computed<number>(() => props.mappings.filter((m) => m.masking !== null).length)

const inputTables = computed<string[]>(() =>
  props.inputs.map((i) => `${i.schema}.${i.table}（ds#${i.data_source_id}）`),
)
</script>

<template>
  <div class="fm">
    <EmptyState
      v-if="mappings.length === 0"
      variant="no-data"
      title="尚无字段映射"
      description="EtlPlan 生成后，这里逐字段列出源→目标的类型、转换与脱敏。"
    />

    <template v-else>
      <!-- source_table 定位没有「行」可指，用横幅把表和 Plan 记录的表一起摆出来 -->
      <el-alert v-if="sourceTableLocator !== null" type="error" :closable="false" show-icon>
        <div class="fm__banner">
          <span>门禁定位到源表：</span>
          <span class="elt-mono">
            {{ sourceTableLocator.schema }}.{{ sourceTableLocator.table }}
            （ds#{{ sourceTableLocator.data_source_id }}）
          </span>
        </div>
        <div class="fm__banner-muted">Plan 记录的输入表：{{ inputTables.join('，') || '—' }}</div>
      </el-alert>

      <div class="fm__bar">
        <el-input v-model="keyword" size="small" placeholder="字段名筛选" clearable class="fm__search" />
        <el-checkbox v-model="onlyDiff" size="small">仅看差异（{{ diffCount }}）</el-checkbox>
        <el-checkbox v-model="onlySensitive" size="small">仅看敏感字段（{{ sensitiveCount }}）</el-checkbox>
        <span class="fm__spacer" />
        <span class="fm__count">{{ rows.length }} / {{ mappings.length }} 行</span>
      </div>

      <el-table
        :data="rows"
        size="small"
        border
        :row-class-name="({ row }: { row: { index: number } }) =>
          activeIndex !== null && row.index === activeIndex ? 'fm__row--active' : ''
        "
        class="fm__table"
        height="100%"
      >
        <el-table-column label="源字段" min-width="180">
          <template #default="{ row }">
            <span class="elt-mono">{{ row.mapping.source_field }}</span>
            <span class="fm__type">{{ row.mapping.source_type }}</span>
          </template>
        </el-table-column>
        <el-table-column label="" width="40">
          <template #default>
            <span class="fm__arrow">→</span>
          </template>
        </el-table-column>
        <el-table-column label="目标字段" min-width="180">
          <template #default="{ row }">
            <span class="elt-mono">{{ row.mapping.target_field }}</span>
            <span class="fm__type">{{ row.mapping.target_type }}</span>
          </template>
        </el-table-column>
        <el-table-column label="转换" width="100">
          <template #default="{ row }">
            {{ TRANSFORM_TEXT[row.mapping.transform as FieldTransform] ?? row.mapping.transform }}
          </template>
        </el-table-column>
        <el-table-column label="脱敏" width="110">
          <template #default="{ row }">
            <span v-if="row.mapping.masking === null" class="fm__muted">—</span>
            <el-tag v-else size="small" type="warning" effect="plain">
              {{ MASKING_TEXT[row.mapping.masking.method as MaskingMethod] ?? row.mapping.masking.method }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="质量规则" width="90">
          <template #default="{ row }">
            <span :class="row.mapping.quality_rule_ids.length === 0 ? 'fm__muted' : ''">
              {{ row.mapping.quality_rule_ids.length }}
            </span>
          </template>
        </el-table-column>
        <el-table-column label="变更" width="80">
          <template #default="{ row }">
            <span class="fm__change" :class="CHANGE_META[row.mapping.change as FieldChange].cls">
              {{ CHANGE_META[row.mapping.change as FieldChange].label }}
            </span>
          </template>
        </el-table-column>
      </el-table>
    </template>
  </div>
</template>

<style scoped>
.fm {
  display: flex;
  flex-direction: column;
  gap: 8px;
  height: 100%;
  min-height: 0;
}

.fm__banner {
  display: flex;
  gap: 6px;
  align-items: baseline;
  font-size: 12px;
}

.fm__banner-muted {
  font-size: 11px;
  color: var(--elt-text-secondary);
}

.fm__bar {
  display: flex;
  gap: 12px;
  align-items: center;
}

.fm__search {
  width: 200px;
}

.fm__spacer {
  flex: 1;
}

.fm__count {
  font-size: 11px;
  color: var(--elt-text-muted);
}

.fm__table {
  flex: 1;
  min-height: 0;
}

.fm__type {
  margin-left: 6px;
  font-size: 11px;
  color: var(--elt-text-muted);
}

.fm__arrow {
  color: var(--elt-text-muted);
}

.fm__muted {
  color: var(--elt-text-muted);
}

.fm__change.is-same {
  color: var(--elt-text-muted);
}

.fm__change.is-added {
  color: var(--elt-success);
}

.fm__change.is-removed {
  color: var(--elt-danger);
}

.fm__change.is-modified {
  color: var(--elt-warn);
}

/* 门禁定位到的那一行：描边而不是换底色，避免和「变更」的语义色互相干扰 */
.fm :deep(.fm__row--active) {
  box-shadow: inset 3px 0 0 var(--elt-accent);
  background-color: var(--elt-bg-elevated);
}
</style>
