<script setup lang="ts">
/**
 * 脱敏样本表（fe-03 §6.4）。
 *
 * 红线（CLAUDE.md §5.7）：
 * - 样本值是**后端已脱敏**的，库里不存在原文，故本组件不提供也不链接任何「查看原文」入口；
 * - 每个值仍走 SensitiveText 二次扫描（后端漏了也不会漏到截图里）；
 * - 单元格复制前再判一次 containsSensitive：疑似未脱敏的值拒绝复制，
 *   因为「一键复制」比「看一眼」更容易把数据带出去。
 *
 * NULL 与空字符串必须区分渲染：探查的统计口径（null_ratio）按 NULL 算，
 * 把空串也显示成 NULL 会让用户误判数据质量。
 */
import { computed } from 'vue'
import SensitiveText from '@/components/common/SensitiveText.vue'
import { useCopy } from '@/composables/useCopy'
import { ElMessage } from '@/utils/feedback'
import { containsSensitive } from '@/utils/mask'
import { formatInt } from '@/utils/format'

const props = withDefaults(
  defineProps<{
    /** 列顺序优先用表结构给的顺序；缺省回退到首行的键序 */
    columns?: string[]
    rows: Array<Record<string, string>>
    /** 后端截断标记：为 true 说明还有更多行没返回 */
    truncated?: boolean
    limit?: number
  }>(),
  { columns: () => [], rows: () => [], truncated: false, limit: 50 },
)

const { copy } = useCopy()

const resolvedColumns = computed<string[]>(() => {
  if (props.columns.length > 0) {
    return props.columns
  }
  const first = props.rows[0]
  return first === undefined ? [] : Object.keys(first)
})

/** 返回值：undefined = 该行没有这个键（视作 NULL）；'' = 空字符串 */
function cellValue(row: Record<string, string>, column: string): string | null | undefined {
  if (!Object.prototype.hasOwnProperty.call(row, column)) {
    return undefined
  }
  const value: unknown = row[column]
  if (value === null || value === undefined) {
    return null
  }
  return String(value)
}

async function onCellClick(row: Record<string, string>, column: string): Promise<void> {
  const value = cellValue(row, column)
  if (value === null || value === undefined || value === '') {
    return
  }
  if (containsSensitive(value)) {
    ElMessage.warning('该单元格疑似含未脱敏内容，已阻止复制')
    return
  }
  await copy(value, `${column} 的值`)
}
</script>

<template>
  <div class="sample-table">
    <el-alert type="warning" :closable="false" show-icon title="样本已脱敏">
      <p class="sample-table__notice">
        表中仅显示脱敏后的值（掩码 / 哈希 / 截断），平台不保存原始值，因此也没有「查看原文」入口。
        单元格可点击复制脱敏后的文本。
      </p>
    </el-alert>

    <p v-if="rows.length === 0" class="sample-table__empty">
      该表没有样本：可能是「仅结构」范围，或采样行数为 0。
    </p>

    <el-table v-else :data="rows" size="small" :max-height="340">
      <el-table-column
        v-for="column in resolvedColumns"
        :key="column"
        :label="column"
        min-width="140"
      >
        <template #default="{ row }">
          <span class="sample-table__cell" title="点击复制该单元格（已脱敏值）" @click="onCellClick(row, column)">
            <!-- NULL 与空串分别渲染；两者都不经过脱敏通道（此处无值可泄露） -->
            <span v-if="cellValue(row, column) === undefined || cellValue(row, column) === null" class="sample-table__null">NULL</span>
            <span v-else-if="cellValue(row, column) === ''" class="sample-table__blank">（空字符串）</span>
            <SensitiveText v-else :value="cellValue(row, column)" mono />
          </span>
        </template>
      </el-table-column>
    </el-table>

    <p v-if="rows.length > 0" class="sample-table__footer">
      本页 {{ formatInt(rows.length) }} 行<span v-if="truncated">（已截断，最多返回 {{ formatInt(limit) }} 行）</span>
    </p>
  </div>
</template>

<style scoped>
.sample-table {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.sample-table__notice {
  margin: 0;
  line-height: 18px;
}

.sample-table__empty {
  margin: 0;
  font-size: 12px;
  line-height: 18px;
  color: var(--elt-text-muted);
}

.sample-table__cell {
  display: block;
  max-width: 100%;
  font-family: var(--elt-font-mono);
  cursor: copy;
}

.sample-table__null {
  font-style: italic;
  color: var(--elt-text-muted);
}

.sample-table__blank {
  font-style: italic;
  color: var(--elt-text-muted);
}

.sample-table__footer {
  margin: 0;
  font-size: 11px;
  color: var(--elt-text-muted);
  text-align: right;
}
</style>
