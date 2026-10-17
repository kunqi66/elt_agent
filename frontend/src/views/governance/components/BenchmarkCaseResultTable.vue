<script setup lang="ts">
/**
 * 逐用例评测结果（fe-07 §6）。
 *
 * 这里是唯一能回答「到底哪条用例漏了」的地方，所以：
 * - 失败行整行浅红 + 左侧红条，扫一眼就能定位；
 * - `is_malicious === true` 且未通过时额外打「安全用例未拦截」徽章——
 *   普通用例失败是质量问题，恶意用例失败是安全问题，两者不能混在一列里；
 * - expected / actual 只挑 2~3 个关键键展示，完整的放 tooltip：
 *   把整个 JSON 铺开会让表格变成不可读的日志墙。
 *
 * 这张表没有用 QueryTable，因为它需要逐行染色（row-class-name），
 * 而 QueryTable 的外壳不转发这个方法；三态仍然按同样的优先级手写：
 * error ＞ 首屏骨架 ＞ 空态 ＞ 数据。
 */
import { computed, ref } from 'vue'
import { EmptyState, ErrorState } from '@/components/common'
import { DASH, textOrDash } from '@/utils/format'
import type { BenchmarkCaseResult } from '@/types'

const props = withDefaults(
  defineProps<{
    items: BenchmarkCaseResult[]
    loading?: boolean
    error?: string | null
    /** 是否已经选过一次运行；没选之前不该说「没有用例」 */
    hasRun: boolean
  }>(),
  { loading: false, error: null },
)

const emit = defineEmits<{
  retry: []
}>()

/** 摘要优先取这几个键：它们直接回答「结论是什么、被哪条规则拦的」 */
const SUMMARY_KEYS = ['conclusion', 'code', 'error_code', 'risk_level'] as const

const onlyFailed = ref(false)
const onlyMalicious = ref(false)

const visible = computed<BenchmarkCaseResult[]>(() =>
  props.items.filter((item) => {
    if (onlyFailed.value && item.passed) {
      return false
    }
    if (onlyMalicious.value && !item.is_malicious) {
      return false
    }
    return true
  }),
)

const initialLoading = computed<boolean>(() => props.loading && props.items.length === 0)
const hasError = computed<boolean>(() => props.error !== null && props.error !== '')

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

function summarize(record: Record<string, unknown>): Array<{ key: string; value: string }> {
  const preferred = SUMMARY_KEYS.filter((key) => record[key] !== undefined)
  const keys: string[] = preferred.length > 0 ? [...preferred].slice(0, 3) : Object.keys(record).slice(0, 2)
  return keys.map((key) => ({ key, value: renderValue(record[key]) }))
}

function fullJson(record: Record<string, unknown>): string {
  return JSON.stringify(record, null, 2)
}

function rowClassName(data: { row: BenchmarkCaseResult }): string {
  return data.row.passed ? '' : 'case-row--failed'
}
</script>

<template>
  <div class="cases">
    <div class="cases__bar">
      <el-checkbox v-model="onlyFailed" size="small">只看失败</el-checkbox>
      <el-checkbox v-model="onlyMalicious" size="small">只看恶意用例</el-checkbox>
      <span class="cases__count">
        共 {{ items.length }} 条，当前显示 {{ visible.length }} 条
      </span>
      <el-button size="small" :loading="loading" @click="emit('retry')">刷新</el-button>
    </div>

    <div v-if="hasError" class="cases__body">
      <ErrorState :message="error" @retry="emit('retry')" />
    </div>

    <div v-else-if="!hasRun" class="cases__body">
      <EmptyState
        variant="no-data"
        title="尚未选择运行"
        description="先在上方的运行列表里选一次评测，这里会显示它逐条用例的判定结果。"
      />
    </div>

    <div
      v-else-if="initialLoading"
      class="cases__body elt-skeleton elt-skeleton--block"
      :style="{ height: '220px' }"
    />

    <div v-else-if="items.length === 0" class="cases__body">
      <EmptyState
        variant="no-data"
        title="这次运行没有逐用例结果"
        description="评测在生成用例结果之前就失败了，或该次运行仍在进行中；先看运行列表里的状态与失败原因。"
      />
    </div>

    <div v-else-if="visible.length === 0" class="cases__body">
      <EmptyState
        variant="no-match"
        title="没有符合条件的用例"
        description="当前筛选（只看失败 / 只看恶意用例）没有命中任何一条，清空筛选即可看到全部结果。"
      />
    </div>

    <el-table
      v-else
      :data="visible"
      size="small"
      :row-class-name="rowClassName"
      class="cases__table"
    >
      <el-table-column label="用例" min-width="220">
        <template #default="{ row }">
          <div class="cases__name">
            <span>{{ row.case_name }}</span>
            <el-tag v-if="row.is_malicious" type="danger" size="small" effect="plain">恶意用例</el-tag>
            <el-tag v-if="row.is_malicious && !row.passed" type="danger" size="small" effect="dark">
              安全用例未拦截
            </el-tag>
          </div>
          <div class="cases__meta elt-mono">{{ textOrDash(row.category) }}</div>
        </template>
      </el-table-column>

      <el-table-column label="期望" min-width="180">
        <template #default="{ row }">
          <el-tooltip :content="fullJson(row.expected)" placement="top">
            <div class="cases__kv">
              <span v-for="item in summarize(row.expected)" :key="item.key" class="cases__pair">
                <span class="cases__key elt-mono">{{ item.key }}</span>
                <span class="cases__value elt-mono">{{ item.value }}</span>
              </span>
            </div>
          </el-tooltip>
        </template>
      </el-table-column>

      <el-table-column label="实际" min-width="180">
        <template #default="{ row }">
          <el-tooltip :content="fullJson(row.actual)" placement="top">
            <div class="cases__kv">
              <span v-for="item in summarize(row.actual)" :key="item.key" class="cases__pair">
                <span class="cases__key elt-mono">{{ item.key }}</span>
                <span class="cases__value elt-mono">{{ item.value }}</span>
              </span>
            </div>
          </el-tooltip>
        </template>
      </el-table-column>

      <el-table-column label="结论" width="90">
        <template #default="{ row }">
          <el-tag :type="row.passed ? 'success' : 'danger'" size="small" effect="plain">
            {{ row.passed ? '通过' : '未通过' }}
          </el-tag>
        </template>
      </el-table-column>

      <el-table-column label="错误码" width="150">
        <template #default="{ row }">
          <span class="elt-mono cases__meta">{{ textOrDash(row.error_code) }}</span>
        </template>
      </el-table-column>

      <el-table-column label="用例 ID" width="90">
        <template #default="{ row }">
          <span class="elt-mono cases__meta">#{{ row.case_id }}</span>
        </template>
      </el-table-column>
    </el-table>
  </div>
</template>

<style scoped>
.cases {
  display: flex;
  flex-direction: column;
}

.cases__bar {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  align-items: center;
  padding: 12px 16px 4px;
}

.cases__count {
  font-size: 11px;
  color: var(--elt-text-muted);
}

.cases__body {
  padding: 8px 16px 16px;
}

.cases__table {
  --el-table-row-hover-bg-color: var(--elt-bg-elevated);
}

.cases__name {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  align-items: center;
  font-size: 13px;
  color: var(--elt-text-primary);
}

.cases__meta {
  font-size: 11px;
  color: var(--elt-text-muted);
}

.cases__kv {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.cases__pair {
  display: flex;
  gap: 6px;
  font-size: 12px;
}

.cases__key {
  flex: none;
  min-width: 68px;
  color: var(--elt-text-secondary);
}

.cases__value {
  min-width: 0;
  color: var(--elt-text-primary);
  overflow-wrap: anywhere;
}

/* 失败行：浅红底 + 左侧红条。用 mix 从令牌派生，不引入新色值 */
:deep(.case-row--failed) td {
  background-color: color-mix(in srgb, var(--elt-danger) 8%, var(--elt-bg-app));
}

:deep(.case-row--failed) td:first-child {
  box-shadow: inset 2px 0 0 0 var(--elt-danger);
}
</style>
