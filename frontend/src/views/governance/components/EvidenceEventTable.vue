<script setup lang="ts">
/**
 * 证据账本浏览器（fe-07 §6 / be-06）。
 *
 * 四条审计口径，别改：
 * 1. `event_type` **按代码枚举原样展示**，不翻译成中文。
 *    审计要的是「和代码里那一行逐字一致」，翻成「审批通过」之后就没法和日志、SQL、快照对账了。
 *    颜色由 StatusTag 给，文字仍是原值——颜色是给人扫的，文字是给人对的。
 * 2. 游标分页，不是 offset：账本持续 append，offset 会漏读或重读。
 * 3. 前端缓冲有上限（见 store 的 EVIDENCE_BUFFER_LIMIT）。裁切发生过就必须说出来，
 *    否则用户会以为「翻到底了」，其实是前面被丢了（截断提示由父组件判定后传进来）。
 * 4. 断链不只高亮那一条：从断点起的整段都失去锚定，必须给出「从哪一段开始不可信」。
 *
 * 没有导出按钮：账本导出由后端生成（浏览器拼 CSV 会绕过脱敏与审计留痕），
 * 基座也还没有对应的 api 出口——不在这里硬拼一条 URL。
 */
import { computed, ref, watch } from 'vue'
import { Search } from '@element-plus/icons-vue'
import { ArtifactHash, EmptyState, ErrorState, StatusTag } from '@/components/common'
import { enumOptions } from '@/utils/enums'
import { DASH, formatDateTimeMs, formatUtc, textOrDash } from '@/utils/format'
import type { EvidenceEventRead, EvidenceFilter } from '@/types'

/** 账本查询条件（project_id 由 store 补） */
type EvidenceQuery = Omit<EvidenceFilter, 'project_id'>

const props = withDefaults(
  defineProps<{
    items: EvidenceEventRead[]
    loading?: boolean
    error?: string | null
    filter: EvidenceQuery
    hasMore: boolean
    /** store.brokenEventSeqs：断链所在 seq；空数组 = 链是好的（或还没验） */
    brokenSeqs: number[]
    selectedId: number | null
    /** 前端缓冲是否发生过裁切 */
    truncated: boolean
    /** 裁切后的缓冲条数；用于说明「最多留了多少」 */
    truncatedLimit: number | null
  }>(),
  { loading: false, error: null, truncated: false, truncatedLimit: null },
)

const emit = defineEmits<{
  'update-filter': [patch: Partial<EvidenceQuery>]
  'load-more': []
  retry: []
  select: [event: EvidenceEventRead]
}>()

const eventTypeOptions = enumOptions('EvidenceEventType')

/** 关键字与时间范围先落在本地，按回车/选完再查——避免每敲一个字打一次后端 */
const subjectKeyword = ref<string>(props.filter.subject_id ?? '')
const dateRange = ref<string[] | null>(null)

function numOrDash(value: number | null | undefined): string {
  return value === null || value === undefined ? DASH : String(value)
}

watch(
  () => props.filter.subject_id,
  (next) => {
    subjectKeyword.value = next ?? ''
  },
)

const breakSeq = computed<number | null>(() => props.brokenSeqs[0] ?? null)

const filtered = computed<boolean>(
  () =>
    (props.filter.event_type?.length ?? 0) > 0 ||
    (props.filter.subject_id ?? '') !== '' ||
    props.filter.from !== undefined ||
    props.filter.to !== undefined,
)

/** 首屏骨架只在「一条都还没有」时出；追加加载走底部按钮自己的 loading */
const initialLoading = computed<boolean>(() => props.loading && props.items.length === 0)

const hasError = computed<boolean>(() => props.error !== null && props.error !== '')

const seqRange = computed<string>(() => {
  const list = props.items
  if (list.length === 0) {
    return DASH
  }
  const first = list[0]?.seq
  const last = list[list.length - 1]?.seq
  if (first === undefined || last === undefined) {
    return DASH
  }
  return first === last ? `seq ${first}` : `seq ${first} ~ ${last}`
})

function onEventTypeChange(value: unknown): void {
  const list = Array.isArray(value) ? (value as string[]) : []
  emit('update-filter', { event_type: list.length > 0 ? list : undefined })
}

function onSubjectSearch(): void {
  const keyword = subjectKeyword.value.trim()
  emit('update-filter', { subject_id: keyword === '' ? undefined : keyword })
}

function onDateRangeChange(value: unknown): void {
  if (Array.isArray(value) && value.length === 2) {
    const [from, to] = value as unknown[]
    if (from != null && to != null) {
      emit('update-filter', { from: String(from), to: String(to) })
      return
    }
  }
  emit('update-filter', { from: undefined, to: undefined })
}

function onClear(): void {
  subjectKeyword.value = ''
  dateRange.value = null
  emit('update-filter', {
    event_type: undefined,
    subject_id: undefined,
    from: undefined,
    to: undefined,
  })
}

function rowClassName(data: { row: EvidenceEventRead }): string {
  const seq = breakSeq.value
  if (seq === null) {
    return ''
  }
  if (data.row.seq === seq) {
    return 'ev-row--broken'
  }
  if (data.row.seq > seq) {
    return 'ev-row--untrusted'
  }
  return ''
}
</script>

<template>
  <div class="ev">
    <el-alert
      v-if="truncated"
      class="ev__alert"
      type="warning"
      :closable="false"
      show-icon
      title="前端缓冲已到上限，更早的记录已移出视图"
      :description="`页面为控制内存只保留最近加载的 ${truncatedLimit ?? '若干'} 条记录，最早的那部分已经被裁掉——列表里没有，不代表账本里没有。要回看更早的段，请用时间范围或事件类型缩小条件后重新查询。`"
    />

    <el-alert
      v-if="breakSeq !== null"
      class="ev__alert"
      type="error"
      :closable="false"
      show-icon
      :title="`验链未通过：seq ${breakSeq} 处的哈希链对不上`"
      :description="`从 seq ${breakSeq} 起，后续事件的 prev_hash 都锚定在这一段上，因此 seq ≥ ${breakSeq} 的记录在重新验链通过之前都不能作为审计依据；seq ${breakSeq} 之前的部分仍然可信。原因见上方验链结果。`"
    />

    <div class="ev__filters">
      <el-select
        :model-value="filter.event_type ?? []"
        class="ev__types"
        multiple
        collapse-tags
        collapse-tags-tooltip
        clearable
        size="small"
        placeholder="事件类型（原值）"
        @change="onEventTypeChange"
      >
        <el-option
          v-for="option in eventTypeOptions"
          :key="option.value"
          :label="option.label"
          :value="option.value"
        />
      </el-select>

      <el-date-picker
        v-model="dateRange"
        class="ev__range"
        type="datetimerange"
        size="small"
        start-placeholder="起始时间"
        end-placeholder="结束时间"
        range-separator="至"
        format="YYYY-MM-DD HH:mm"
        value-format="YYYY-MM-DDTHH:mm:ssZ"
        @change="onDateRangeChange"
      />

      <el-input
        v-model="subjectKeyword"
        class="ev__subject"
        size="small"
        clearable
        placeholder="主体标识，如 preparation:42"
        :prefix-icon="Search"
        @keyup.enter="onSubjectSearch"
        @clear="onSubjectSearch"
      />

      <el-button size="small" :disabled="!filtered" @click="onClear">清空筛选</el-button>
    </div>

    <div v-if="hasError" class="ev__body">
      <ErrorState :message="error" @retry="emit('retry')" />
    </div>

    <div
      v-else-if="initialLoading"
      class="ev__body elt-skeleton elt-skeleton--block"
      :style="{ height: '320px' }"
    />

    <div v-else-if="items.length === 0" class="ev__body">
      <EmptyState
        :variant="filtered ? 'no-match' : 'no-data'"
        :title="filtered ? '没有符合条件的事件' : '账本里还没有事件'"
        :description="
          filtered
            ? '当前筛选条件没有命中任何事件，清空筛选即可看到全部记录。'
            : '账本由平台自身写入：登录、数据源变更、门禁、审批、执行都会留下记录；若刚完成过这些动作，请点刷新重试。'
        "
      />
    </div>

    <template v-else>
      <el-table
        :data="items"
        size="small"
        :row-class-name="rowClassName"
        row-key="id"
        class="ev__table"
        highlight-current-row
        @row-click="(row: EvidenceEventRead) => emit('select', row)"
      >
        <el-table-column label="seq" width="110">
          <template #default="{ row }">
            <div class="ev__seq elt-mono">
              {{ row.seq }}
              <el-tag v-if="row.seq === breakSeq" type="danger" size="small" effect="dark">断点</el-tag>
              <el-tag
                v-else-if="breakSeq !== null && row.seq > breakSeq"
                type="danger"
                size="small"
                effect="plain"
              >
                不可信
              </el-tag>
            </div>
            <div class="ev__meta elt-mono">block {{ numOrDash(row.block_seq) }}</div>
          </template>
        </el-table-column>

        <el-table-column label="事件类型（代码原值）" min-width="210">
          <template #default="{ row }">
            <StatusTag kind="EvidenceEventType" :value="row.event_type" size="small" />
            <div class="ev__meta" :title="formatUtc(row.occurred_at)">
              {{ formatDateTimeMs(row.occurred_at) }}
            </div>
          </template>
        </el-table-column>

        <el-table-column label="操作者" width="150">
          <template #default="{ row }">
            <div class="ev__actor">{{ textOrDash(row.actor_name) }}</div>
            <div class="ev__meta elt-mono">#{{ numOrDash(row.actor_id) }}</div>
          </template>
        </el-table-column>

        <el-table-column label="主体" min-width="170">
          <template #default="{ row }">
            <div class="ev__subject-cell elt-mono">{{ row.subject_type }}:{{ row.subject_id }}</div>
          </template>
        </el-table-column>

        <el-table-column label="制品哈希" width="130">
          <template #default="{ row }">
            <ArtifactHash :hash="row.artifact_hash" display="short" label="制品哈希" />
          </template>
        </el-table-column>

        <el-table-column label="事件哈希" width="130">
          <template #default="{ row }">
            <ArtifactHash :hash="row.event_hash" display="short" label="事件哈希" />
          </template>
        </el-table-column>
      </el-table>

      <footer class="ev__foot">
        <span class="ev__count">
          已加载 <strong>{{ items.length }}</strong> 条（{{ seqRange }}）
        </span>
        <span class="ev__meta">
          {{ hasMore ? '按 seq 继续向后加载，追加到列表末尾' : '游标已到底，没有更多记录' }}
        </span>
        <el-button
          size="small"
          :loading="loading"
          :disabled="!hasMore"
          @click="emit('load-more')"
        >
          加载更多
        </el-button>
      </footer>

      <p class="ev__hint">
        事件类型按后端枚举原值展示，不做中文翻译——审计口径必须与代码逐字一致。
        prev_hash / event_hash 的完整值与复制入口在下方事件详情里。
      </p>
    </template>
  </div>
</template>

<style scoped>
.ev {
  display: flex;
  flex-direction: column;
}

.ev__alert {
  margin: 12px 16px 0;
  width: auto;
}

.ev__filters {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  padding: 12px 16px 0;
}

.ev__types {
  width: 300px;
}

.ev__range {
  width: 340px;
}

.ev__subject {
  width: 220px;
}

.ev__body {
  padding: 8px 16px 16px;
}

.ev__seq {
  display: flex;
  gap: 4px;
  align-items: center;
  font-size: 12px;
  color: var(--elt-text-primary);
}

.ev__meta {
  font-size: 11px;
  color: var(--elt-text-muted);
}

.ev__actor {
  font-size: 12px;
  color: var(--elt-text-primary);
}

.ev__subject-cell {
  font-size: 12px;
  color: var(--elt-text-secondary);
}

.ev__foot {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  align-items: center;
  padding: 10px 16px;
  border-top: 1px solid var(--elt-border-subtle);
}

.ev__count {
  font-size: 12px;
  color: var(--elt-text-secondary);
}

.ev__hint {
  padding: 0 16px 14px;
  margin: 0;
  font-size: 11px;
  line-height: 1.7;
  color: var(--elt-text-muted);
}

/* 断点行：整行红底 + 左红条，一眼定位 */
:deep(.ev-row--broken) td {
  background-color: color-mix(in srgb, var(--elt-danger) 14%, var(--elt-bg-app));
}

:deep(.ev-row--broken) td:first-child {
  box-shadow: inset 2px 0 0 0 var(--elt-danger);
}

/* 断点之后的整段：比断点轻一档，表达「同一段里都不再可信」而不是「这些行本身有问题」 */
:deep(.ev-row--untrusted) td {
  background-color: color-mix(in srgb, var(--elt-danger) 5%, var(--elt-bg-app));
}
</style>
