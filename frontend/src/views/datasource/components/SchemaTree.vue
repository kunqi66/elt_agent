<script setup lang="ts">
/**
 * Schema 快照树（fe-03 §5.4 / §6.4）。
 *
 * 快照由后端 engines 层产出（MySQL information_schema 等价物），
 * 前端只做层级渲染：schema → 表 → 字段。字段的敏感级别用 StatusTag 直接标在树上，
 * 因为「这张表里哪些字段是 confidential」是选表时最需要一眼看到的信息。
 */
import { computed } from 'vue'
import ErrorState from '@/components/common/ErrorState.vue'
import StatusTag from '@/components/common/StatusTag.vue'
import { formatInt } from '@/utils/format'
import type { SchemaColumn, SchemaSnapshot, SchemaTable } from '@/types'

/**
 * 用 type 而不是 interface：el-tree 的 data 是 Record<string, any>[]，
 * 只有对象字面量类型才有隐式索引签名，interface 会被判定为不可赋值。
 */
type SchemaNode = {
  id: string
  label: string
  kind: 'schema' | 'table' | 'field'
  table?: SchemaTable
  column?: SchemaColumn
  sensitiveLevel?: string
  children?: SchemaNode[]
}

const props = withDefaults(
  defineProps<{
    snapshot: SchemaSnapshot | null
    loading?: boolean
    error?: string | null
    /** 当前选中的表名；用于高亮 */
    selectedTableName?: string | null
  }>(),
  { loading: false, error: null, selectedTableName: null },
)

const emit = defineEmits<{
  'select-table': [table: SchemaTable]
  'select-field': [payload: { table: SchemaTable; column: SchemaColumn }]
}>()

const treeProps = { label: 'label', children: 'children' } as const

const nodes = computed<SchemaNode[]>(() => {
  const snapshot = props.snapshot
  if (snapshot === null) {
    return []
  }
  // 按 schema 分组：后端给的是扁平表列表，多库/多 schema 混在一起会看不出归属
  const bySchema = new Map<string, SchemaTable[]>()
  for (const table of snapshot.tables) {
    const key = table.schema === '' ? '（默认库）' : table.schema
    const list = bySchema.get(key)
    if (list === undefined) {
      bySchema.set(key, [table])
    } else {
      list.push(table)
    }
  }

  return [...bySchema.entries()].map(([schema, tables]) => ({
    id: `schema:${schema}`,
    label: `${schema}（${tables.length} 张表）`,
    kind: 'schema' as const,
    children: tables.map((table) => ({
      id: `table:${table.schema}.${table.name}`,
      label: table.name,
      kind: 'table' as const,
      table,
      children: table.columns.map((column) => ({
        id: `field:${table.schema}.${table.name}.${column.name}`,
        label: column.name,
        kind: 'field' as const,
        table,
        column,
        sensitiveLevel: column.sensitive_level,
      })),
    })),
  }))
})

/** 默认展开 schema 层：否则用户要点两次才能看到表 */
const expandedKeys = computed<string[]>(() =>
  nodes.value.map((node) => node.id),
)

const currentNodeKey = computed<string>(() => {
  if (props.selectedTableName === null) {
    return ''
  }
  for (const schema of nodes.value) {
    for (const table of schema.children ?? []) {
      if (table.kind === 'table' && table.table?.name === props.selectedTableName) {
        return table.id
      }
    }
  }
  return ''
})

function onNodeClick(data: SchemaNode): void {
  if (data.kind === 'table' && data.table !== undefined) {
    emit('select-table', data.table)
  }
  if (data.kind === 'field' && data.table !== undefined && data.column !== undefined) {
    emit('select-field', { table: data.table, column: data.column })
  }
}
</script>

<template>
  <div class="schema-tree">
    <div v-if="loading" class="elt-skeleton elt-skeleton--block schema-tree__skeleton" />

    <ErrorState v-else-if="error !== null && error !== ''" :message="error" title="表结构加载失败" />

    <p v-else-if="nodes.length === 0" class="schema-tree__empty">
      该版本没有表结构快照：可能是「仅结构」尚未完成，或采样范围内没有可见的表。
    </p>

    <el-tree
      v-else
      :data="nodes"
      :props="treeProps"
      node-key="id"
      :default-expanded-keys="expandedKeys"
      :current-node-key="currentNodeKey"
      highlight-current
      :expand-on-click-node="false"
      @node-click="onNodeClick"
    >
      <template #default="{ data }">
        <span class="schema-tree__node" :class="`schema-tree__node--${data.kind}`">
          <span class="schema-tree__label">{{ data.label }}</span>
          <span v-if="data.kind === 'table' && data.table?.row_count_approx != null" class="schema-tree__meta">
            ~{{ formatInt(data.table.row_count_approx) }} 行
          </span>
          <StatusTag
            v-if="data.kind === 'field' && data.sensitiveLevel != null"
            kind="SensitiveLevel"
            :value="data.sensitiveLevel"
          />
        </span>
      </template>
    </el-tree>
  </div>
</template>

<style scoped>
.schema-tree__skeleton {
  height: 220px;
}

.schema-tree__empty {
  margin: 0;
  font-size: 12px;
  line-height: 18px;
  color: var(--elt-text-muted);
}

.schema-tree__node {
  display: inline-flex;
  gap: 8px;
  align-items: center;
  font-size: 12px;
}

.schema-tree__node--schema .schema-tree__label {
  font-weight: 600;
}

.schema-tree__node--table .schema-tree__label {
  font-family: var(--elt-font-mono);
}

.schema-tree__node--field .schema-tree__label {
  font-family: var(--elt-font-mono);
  color: var(--elt-text-secondary);
}

.schema-tree__meta {
  font-size: 11px;
  color: var(--elt-text-muted);
}
</style>
