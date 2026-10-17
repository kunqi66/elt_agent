<script setup lang="ts">
/**
 * 策略版本 Diff（fe-07 §6）。
 *
 * 这是发布前的最后一道人工确认：把「改了哪一条规则的哪个字段」摆到一屏里。
 * 因此这里**不做归类归纳**——不合并、不折叠、不排序，后端给什么顺序就什么顺序，
 * 免得用户在「看起来只有两处改动」的视图里漏掉第三处。
 *
 * `changed_fields` 是点分路径（如 `condition.budget_over_default_factor`），
 * 所以取值要按路径下钻，而不是当成对象的一级键。
 */
import { EmptyState } from '@/components/common'
import { DASH, textOrDash } from '@/utils/format'
import type { PolicyDiffEntry, PolicyRuleRead } from '@/types'

withDefaults(
  defineProps<{
    version: number | null
    entries: PolicyDiffEntry[]
    summary: { added: number; removed: number; changed: number } | null
  }>(),
  { summary: null },
)

/** 变更类型的中文名；这是后端 Diff 的枚举，不在 @/utils/enums 的 EnumKind 里 */
const CHANGE_LABELS: Record<PolicyDiffEntry['change'], string> = {
  added: '新增',
  removed: '删除',
  modified: '修改',
}

const CHANGE_TYPES: Record<PolicyDiffEntry['change'], 'success' | 'danger' | 'warning'> = {
  added: 'success',
  removed: 'danger',
  modified: 'warning',
}

/** 按点分路径取值；路径不存在时返回 undefined，由渲染层显示 `—` */
function resolveField(rule: Partial<PolicyRuleRead> | null, field: string): unknown {
  if (rule === null) {
    return undefined
  }
  let cursor: unknown = rule
  for (const segment of field.split('.')) {
    if (cursor === null || typeof cursor !== 'object') {
      return undefined
    }
    cursor = (cursor as Record<string, unknown>)[segment]
  }
  return cursor
}

function renderValue(value: unknown): string {
  if (value === null || value === undefined) {
    return DASH
  }
  if (Array.isArray(value)) {
    return value.length === 0 ? DASH : value.map((item) => String(item)).join(' / ')
  }
  if (typeof value === 'object') {
    return JSON.stringify(value)
  }
  return String(value)
}

/**
 * 新增 / 删除条目没有 changed_fields（后端只给整条草稿），
 * 这时把对象本身摊开成「键: 值」，总比显示一个 `[object Object]` 强。
 */
function objectRows(rule: Partial<PolicyRuleRead> | null): Array<{ key: string; value: string }> {
  if (rule === null) {
    return []
  }
  return Object.entries(rule).map(([key, value]) => ({ key, value: renderValue(value) }))
}

/**
 * el-table 的 `:data` 要求每一行是有键的对象（Element Plus 的 `DefaultRow`），
 * 而这里每行的数据本质是「字段路径」字符串，直接喂 `string[]` 会被类型门禁拒绝。
 * 包一层 `{ field }` 既贴合表格的行模型，也给插槽一个可收口的具名类型。
 */
type DiffFieldRow = { field: string }

function fieldRows(fields: string[]): DiffFieldRow[] {
  return fields.map((field) => ({ field }))
}

/** 插槽 row 的静态类型是 DefaultRow，与 DiffFieldRow 无类型关联，在调用点收口 */
function fieldOf(row: unknown): DiffFieldRow {
  return row as DiffFieldRow
}
</script>

<template>
  <div class="diff">
    <div v-if="version === null" class="diff__placeholder">
      从左侧选一个版本，查看它与当前生效版本的结构化差异。
    </div>

    <template v-else>
      <div class="diff__head">
        <span class="diff__version elt-mono">v{{ version }}</span>
        <template v-if="summary !== null">
          <el-tag type="success" size="small" effect="plain">新增 {{ summary.added }}</el-tag>
          <el-tag type="warning" size="small" effect="plain">修改 {{ summary.changed }}</el-tag>
          <el-tag type="danger" size="small" effect="plain">删除 {{ summary.removed }}</el-tag>
        </template>
      </div>

      <EmptyState
        v-if="entries.length === 0"
        variant="no-data"
        title="与当前生效版本没有差异"
        description="该版本的内容与线上一致，无需人工确认。"
      />

      <div v-else class="diff__entries">
        <section v-for="entry in entries" :key="`${entry.rule_code}-${entry.change}`" class="diff__entry">
          <header class="diff__entry-head">
            <span class="diff__rule elt-mono">{{ entry.rule_code }}</span>
            <el-tag :type="CHANGE_TYPES[entry.change]" size="small" effect="dark">
              {{ CHANGE_LABELS[entry.change] }}
            </el-tag>
            <span class="diff__fields">
              {{ entry.changed_fields.length > 0 ? entry.changed_fields.join('、') : '整条规则' }}
            </span>
          </header>

          <el-table
            v-if="entry.changed_fields.length > 0"
            :data="fieldRows(entry.changed_fields)"
            size="small"
            border
            class="diff__table"
          >
            <el-table-column label="字段" width="240">
              <template #default="{ row }">
                <span class="elt-mono">{{ fieldOf(row).field }}</span>
              </template>
            </el-table-column>
            <el-table-column label="变更前" min-width="160">
              <template #default="{ row }">
                <span class="elt-mono">{{ textOrDash(renderValue(resolveField(entry.before, fieldOf(row).field))) }}</span>
              </template>
            </el-table-column>
            <el-table-column label="变更后" min-width="160">
              <template #default="{ row }">
                <span class="elt-mono">{{ textOrDash(renderValue(resolveField(entry.after, fieldOf(row).field))) }}</span>
              </template>
            </el-table-column>
          </el-table>

          <div v-else class="diff__whole">
            <div class="diff__side">
              <div class="diff__side-title">变更前</div>
              <div v-if="objectRows(entry.before).length === 0" class="diff__empty">{{ DASH }}</div>
              <div v-for="item in objectRows(entry.before)" :key="`b-${item.key}`" class="diff__row">
                <span class="diff__key elt-mono">{{ item.key }}</span>
                <span class="diff__value elt-mono">{{ item.value }}</span>
              </div>
            </div>
            <div class="diff__side">
              <div class="diff__side-title">变更后</div>
              <div v-if="objectRows(entry.after).length === 0" class="diff__empty">{{ DASH }}</div>
              <div v-for="item in objectRows(entry.after)" :key="`a-${item.key}`" class="diff__row">
                <span class="diff__key elt-mono">{{ item.key }}</span>
                <span class="diff__value elt-mono">{{ item.value }}</span>
              </div>
            </div>
          </div>
        </section>
      </div>

      <p class="diff__foot">
        Diff 只说明结构差异，不代表已经生效；生效与否只以四眼审批后的版本状态为准。
      </p>
    </template>
  </div>
</template>

<style scoped>
.diff {
  min-width: 0;
}

.diff__placeholder {
  padding: 32px 0;
  font-size: 12px;
  color: var(--elt-text-muted);
  text-align: center;
}

.diff__head {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
  margin-bottom: 12px;
}

.diff__version {
  font-size: 13px;
  font-weight: 600;
  color: var(--elt-text-primary);
}

.diff__entries {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.diff__entry {
  padding: 10px 12px;
  background-color: var(--elt-bg-elevated);
  border: 1px solid var(--elt-border-subtle);
  border-radius: var(--elt-radius-control);
}

.diff__entry-head {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
  margin-bottom: 8px;
}

.diff__rule {
  font-size: 12px;
  color: var(--elt-text-primary);
}

.diff__fields {
  font-size: 11px;
  color: var(--elt-text-muted);
}

.diff__table {
  background-color: transparent;
}

.diff__whole {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  gap: 12px;
}

@media (width <= 900px) {
  .diff__whole {
    grid-template-columns: minmax(0, 1fr);
  }
}

.diff__side-title {
  margin-bottom: 4px;
  font-size: 11px;
  color: var(--elt-text-muted);
}

.diff__row {
  display: flex;
  gap: 8px;
  font-size: 12px;
  line-height: 1.7;
}

.diff__key {
  flex: none;
  min-width: 96px;
  color: var(--elt-text-secondary);
}

.diff__value {
  min-width: 0;
  color: var(--elt-text-primary);
  overflow-wrap: anywhere;
}

.diff__empty {
  font-size: 12px;
  color: var(--elt-text-muted);
}

.diff__foot {
  margin: 12px 0 0;
  font-size: 11px;
  line-height: 1.7;
  color: var(--elt-text-muted);
}
</style>
