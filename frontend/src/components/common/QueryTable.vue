<script setup lang="ts" generic="T extends object">
/**
 * 列表页表格外壳（fe-01 §7）。全站 7 个列表共用。
 *
 * 为什么用泛型 SFC 而不是 `items: any[]`：
 * 各页面把 `DataSource[]` / `PipelineVersion[]` 传进来后，`#default` 插槽里的
 * `row` 就是**精确类型**，`row.endpoint` 拼错会立刻报错。
 * 用 any 的话这类拼写错误要等运行时才暴露成空白单元格。
 *
 * 状态优先级（刻意如此排列）：error ＞ initialLoading ＞ empty ＞ data。
 * 加载失败**绝不能**退化成空态——见 ErrorState 的说明。
 */
import { computed } from 'vue'
import { Refresh } from '@element-plus/icons-vue'
import EmptyState from './EmptyState.vue'
import ErrorState from './ErrorState.vue'

const props = withDefaults(
  defineProps<{
    items: T[]
    loading?: boolean
    /** 首次加载（尚无任何数据）：显示骨架；后续加载只显示顶部细条 */
    initialLoading?: boolean
    error?: string | null
    requestId?: string | null
    total?: number
    page?: number
    pageSize?: number
    /** 当前筛选是否生效；用于在「无数据」与「无命中」之间自动选措辞 */
    filtered?: boolean
    emptyVariant?: 'no-data' | 'no-match' | 'no-permission'
    emptyText?: string
    emptyDescription?: string
    rowKey?: string
    minHeight?: number
    showPagination?: boolean
  }>(),
  {
    loading: false,
    initialLoading: false,
    error: null,
    requestId: null,
    total: 0,
    page: 1,
    pageSize: 20,
    filtered: false,
    emptyVariant: undefined,
    emptyText: undefined,
    emptyDescription: undefined,
    rowKey: undefined,
    minHeight: 240,
    showPagination: true,
  },
)

const emit = defineEmits<{
  retry: []
  'page-change': [page: number]
  'page-size-change': [size: number]
  /** 行点击：由外壳转发，页面不必自己去够内部的 el-table */
  'row-click': [row: T]
}>()

const variant = computed<'no-data' | 'no-match' | 'no-permission'>(
  () => props.emptyVariant ?? (props.filtered ? 'no-match' : 'no-data'),
)

const hasError = computed<boolean>(() => props.error !== null && props.error !== '')

/** 首次加载显示骨架；后续（翻页/筛选/轮询）只走顶部细条，避免整表闪白 */
const showSkeleton = computed<boolean>(() => props.initialLoading && props.items.length === 0)
const showRefreshBar = computed<boolean>(() => props.loading && !props.initialLoading)

const showPagination = computed<boolean>(() => props.showPagination && props.total > 0)
</script>

<template>
  <div class="query-table">
    <!-- 顶部细进度条：翻页/刷新时给「在动」的反馈，但不遮挡已有数据 -->
    <div v-if="showRefreshBar" class="query-table__bar" role="progressbar" aria-label="加载中" />

    <ErrorState
      v-if="hasError"
      :message="error"
      :request-id="requestId"
      @retry="emit('retry')"
    />

    <template v-else>
      <div v-if="showSkeleton" class="elt-skeleton elt-skeleton--block" :style="{ height: `${minHeight}px` }" />
      <el-table
        v-else
        :data="items"
        :row-key="rowKey"
        :style="{ minHeight: `${minHeight}px` }"
        size="small"
        @row-click="(row: T) => emit('row-click', row)"
      >
        <slot />

        <template #empty>
          <EmptyState :variant="variant" :title="emptyText" :description="emptyDescription">
            <template v-if="$slots['empty-actions']" #actions>
              <slot name="empty-actions" />
            </template>
          </EmptyState>
        </template>
      </el-table>

      <footer v-if="showPagination" class="query-table__footer">
        <span class="query-table__count">
          共 <strong>{{ total }}</strong> 条
        </span>
        <el-pagination
          :current-page="page"
          :page-size="pageSize"
          :total="total"
          :page-sizes="[10, 20, 50, 100]"
          layout="sizes, prev, pager, next, jumper"
          background
          size="small"
          @current-change="(p: number) => emit('page-change', p)"
          @size-change="(s: number) => emit('page-size-change', s)"
        />
        <el-button size="small" :icon="Refresh" :loading="loading" @click="emit('retry')">刷新</el-button>
      </footer>
    </template>
  </div>
</template>

<style scoped>
.query-table {
  position: relative;
  display: flex;
  flex-direction: column;
}

.query-table__bar {
  position: absolute;
  top: 0;
  left: 0;
  z-index: 3;
  width: 100%;
  height: 2px;
  overflow: hidden;
  background-color: var(--elt-accent-soft);
}

.query-table__bar::after {
  display: block;
  width: 35%;
  height: 100%;
  content: '';
  background-color: var(--elt-accent);
  animation: query-bar 1.1s ease-in-out infinite;
}

@keyframes query-bar {
  0% {
    transform: translateX(-100%);
  }

  100% {
    transform: translateX(320%);
  }
}

.query-table__footer {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 10px 2px 0;
}

.query-table__count {
  font-size: 12px;
  color: var(--elt-text-secondary);
}

.query-table__footer :deep(.el-pagination) {
  margin-left: auto;
}

@media (prefers-reduced-motion: reduce) {
  .query-table__bar::after {
    animation: none;
    transform: translateX(0);
  }
}
</style>
