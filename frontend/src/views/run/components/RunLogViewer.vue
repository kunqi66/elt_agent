<script setup lang="ts">
/**
 * 运行日志查看器（fe-06 §5 / §6）。
 *
 * 四条纪律：
 *
 * 1. **游标是 `seq` 而不是 offset**。日志在运行中持续追加，offset 分页会漏行/重行。
 *    本组件只负责渲染与筛选条件的产出，增量拉取由 store 的 `loadMoreLogs()` 承担。
 *
 * 2. **缓冲有上限，且必须显式告知**。`omitted=true` 时列表顶部固定一条提示——
 *    静默截断会让人以为「日志就这么多」，于是拿着半截日志去排障。
 *
 * 3. **DOM 有上限**。这里只渲染最近 N 行的窗口（可手动加宽），
 *    5000 行一次性进 DOM 会让切 tab 卡住好几秒；完整日志走导出。
 *
 * 4. **日志行不二次脱敏**（后端 structlog 处理器已脱敏），仍经 `SensitiveText` 渲染；
 *    前端不做局部打码，也不把 `subject_id` 展开成原文，只做跳转。
 */
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { SectionCard, SensitiveText } from '@/components/common'
import { formatDateTime } from '@/utils/format'
import type { RunLogFilter, RunLogLine } from '@/types'

const props = defineProps<{
  lines: RunLogLine[]
  filters: RunLogFilter
  loading: boolean
  /** 服务端按 limit 截断，仍有更多可追 */
  truncated: boolean
  /** 前端缓冲已从头部裁剪：必须显式提示 */
  omitted: boolean
  /** 单个窗口渲染的最大行数 */
  windowSize?: number
}>()

const emit = defineEmits<{
  'update:filters': [patch: Partial<RunLogFilter>]
  export: []
  reload: []
  'goto-subject': [payload: { subjectType: string; subjectId: string }]
}>()

type LogLevel = RunLogLine['level']

/**
 * 日志级别不在 `utils/enums` 的 EnumKind 注册表内（它是 structlog 的运行时级别，
 * 不是领域枚举），因此这里本地映射；未登记的取值**原样显示**，
 * 与 enumLabel 的兜底口径一致，后端新增级别时界面不会空白。
 */
const LEVEL_TEXT: Record<string, string> = {
  debug: '调试',
  info: '信息',
  warning: '警告',
  error: '错误',
}

function levelText(level: string): string {
  return LEVEL_TEXT[level] ?? level
}

const LEVEL_VALUES: readonly LogLevel[] = ['debug', 'info', 'warning', 'error']

/** 筛选条件的本地镜像；变更后由父级带上 since_seq 归零重拉 */
const level = ref<string>(props.filters.level ?? '')
const keyword = ref<string>(props.filters.q ?? '')
/** 是否自动跟随最新行；用户往上翻时自动关掉，否则会被不断拉回底部 */
const follow = ref(true)
const windowLimit = ref<number>(props.windowSize ?? 300)
const listEl = ref<HTMLDivElement | null>(null)

/** 只渲染尾部窗口：缓冲里的 5000 行不需要同时进 DOM */
const visible = computed<RunLogLine[]>(() => {
  const all = props.lines
  if (all.length <= windowLimit.value) {
    return all
  }
  return all.slice(all.length - windowLimit.value)
})

const hiddenCount = computed<number>(() => Math.max(0, props.lines.length - visible.value.length))

function applyFilters(): void {
  const patch: Partial<RunLogFilter> = {
    level: level.value === '' ? undefined : (level.value as LogLevel),
    q: keyword.value.trim() === '' ? '' : keyword.value.trim(),
  }
  emit('update:filters', patch)
}

function resetFilters(): void {
  level.value = ''
  keyword.value = ''
  applyFilters()
}

function onLevelChange(value: string): void {
  level.value = value
  applyFilters()
}

function widenWindow(): void {
  windowLimit.value += 300
}

function scrollToBottom(): void {
  const el = listEl.value
  if (el !== null) {
    el.scrollTop = el.scrollHeight
  }
}

function onScroll(): void {
  const el = listEl.value
  if (el === null) {
    return
  }
  // 距底部 24px 内视为「在看最新」；往上翻即暂停跟随，避免边看边被拽回底部
  follow.value = el.scrollHeight - el.scrollTop - el.clientHeight < 24
}

watch(
  () => props.lines.length,
  () => {
    if (follow.value) {
      void nextTick(scrollToBottom)
    }
  },
)

// 筛选条件由父级持有（它会带上 since_seq 重新拉取），这里跟随回填
watch(
  () => props.filters,
  (next) => {
    level.value = next.level ?? ''
    keyword.value = next.q ?? ''
  },
)

onMounted(() => {
  void nextTick(scrollToBottom)
})

onBeforeUnmount(() => {
  // 组件卸载时清掉引用，避免残留的 DOM 引用在后续 watch 回调里被使用
  listEl.value = null
})
</script>

<template>
  <SectionCard title="运行日志" :padded="false">
    <template #extra>
      <el-button size="small" :loading="loading" @click="emit('reload')">拉取增量</el-button>
      <el-button size="small" @click="emit('export')">导出完整日志</el-button>
    </template>

    <div class="logs__filters">
      <el-select
        :model-value="level"
        class="logs__filter logs__filter--level"
        size="small"
        placeholder="全部级别"
        clearable
        @update:model-value="onLevelChange"
        @clear="onLevelChange('')"
      >
        <el-option v-for="v in LEVEL_VALUES" :key="v" :value="v" :label="levelText(v)" />
      </el-select>

      <!--
        没有「事件类型」下拉：fe-06 §3.1 要求按 event_type 过滤，
        但基座 api/run.ts 的 fetchRunLogs 未把 event_type 透传给后端（只发 since_seq/level/q/limit）。
        放一个筛了不生效的控件比不放更糟——用户会以为「这段时间没有该事件」。
      -->
      <el-input
        v-model="keyword"
        class="logs__filter logs__filter--kw"
        size="small"
        placeholder="关键字（后端按已脱敏文本匹配）"
        clearable
        @keyup.enter="applyFilters"
      />

      <el-button size="small" type="primary" @click="applyFilters">应用筛选</el-button>
      <el-button size="small" @click="resetFilters">清空</el-button>
      <span class="logs__cursor elt-mono">游标 seq={{ filters.since_seq ?? 0 }}</span>
    </div>

    <!-- 缓冲裁剪提示：必须一直可见，不能只弹一次 toast -->
    <el-alert
      v-if="omitted"
      class="logs__notice"
      type="warning"
      :closable="false"
      show-icon
      title="较早日志已从视图移除，需完整日志请导出"
      description="前端只保留最近 5000 行，更早的行已被丢弃（不是「日志只有这些」）。点右上角「导出完整日志」由后端生成文件，不要用本页拼接。"
    />
    <el-alert
      v-if="truncated"
      class="logs__notice"
      type="info"
      :closable="false"
      show-icon
      title="服务端还有更多日志未拉取"
      description="本次响应被后端按 limit 截断，点「拉取增量」继续追。"
    />

    <div ref="listEl" class="logs__list elt-scroll" @scroll.passive="onScroll">
      <p v-if="lines.length === 0 && !loading" class="logs__empty">
        暂无日志：运行尚未产生日志行，或当前筛选条件没有命中。
      </p>

      <div class="logs__window-bar" v-if="hiddenCount > 0">
        <span>窗口内只渲染最近 {{ visible.length }} 行，另有 {{ hiddenCount }} 行未渲染</span>
        <el-button link size="small" @click="widenWindow">再多显示 300 行</el-button>
      </div>

      <ol class="logs__items">
        <li v-for="line in visible" :key="line.seq" class="logs__item" :class="`logs__item--${line.level}`">
          <span class="logs__seq elt-mono">{{ line.seq }}</span>
          <span class="logs__ts elt-mono">{{ formatDateTime(line.ts) }}</span>
          <span class="logs__level">{{ levelText(line.level) }}</span>
          <span class="logs__event elt-mono">
            <SensitiveText :value="line.event_type" mono />
          </span>
          <span class="logs__message">
            <SensitiveText :value="line.message" multiline />
          </span>
          <el-button
            v-if="line.subject_type !== undefined && line.subject_id !== undefined"
            class="logs__subject"
            link
            size="small"
            @click="emit('goto-subject', { subjectType: line.subject_type, subjectId: line.subject_id })"
          >
            {{ line.subject_type }}#{{ line.subject_id }}
          </el-button>
        </li>
      </ol>
    </div>

    <p v-if="loading" class="logs__loading">正在拉取增量…</p>
  </SectionCard>
</template>

<style scoped>
.logs__filters {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
  padding: 10px 14px;
  border-bottom: 1px solid var(--elt-border-subtle);
}

.logs__filter--level {
  width: 130px;
}

.logs__filter--kw {
  width: 240px;
}

.logs__cursor {
  margin-left: auto;
  font-size: 11px;
  color: var(--elt-text-muted);
}

.logs__notice {
  margin: 10px 14px 0;
}

.logs__list {
  max-height: 420px;
  padding: 8px 14px 14px;
  overflow-y: auto;
}

.logs__empty {
  margin: 12px 0;
  font-size: 12px;
  color: var(--elt-text-muted);
}

.logs__window-bar {
  display: flex;
  gap: 8px;
  align-items: center;
  margin-bottom: 6px;
  font-size: 11px;
  color: var(--elt-text-muted);
}

.logs__items {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 0;
  margin: 0;
  list-style: none;
  font-size: 12px;
}

.logs__item {
  display: grid;
  grid-template-columns: 56px 152px 44px minmax(120px, 200px) minmax(0, 1fr) auto;
  gap: 8px;
  align-items: baseline;
  padding: 2px 0;
  border-left: 2px solid transparent;
  border-radius: 2px;
}

.logs__item:hover {
  background-color: var(--elt-bg-elevated);
}

.logs__item--warning {
  border-left-color: var(--elt-warn);
}

.logs__item--error {
  border-left-color: var(--elt-danger);
}

.logs__seq,
.logs__ts {
  color: var(--elt-text-muted);
}

.logs__level {
  font-size: 11px;
}

.logs__item--info .logs__level {
  color: var(--elt-text-secondary);
}

.logs__item--warning .logs__level {
  color: var(--elt-warn);
}

.logs__item--error .logs__level {
  color: var(--elt-danger);
}

.logs__event {
  color: var(--elt-text-secondary);
}

.logs__message {
  color: var(--elt-text-primary);
}

.logs__subject {
  font-size: 11px;
}

.logs__loading {
  margin: 0;
  padding: 6px 14px 10px;
  font-size: 11px;
  color: var(--elt-text-muted);
}

@media (max-width: 1000px) {
  .logs__item {
    grid-template-columns: 48px 140px 40px minmax(0, 1fr);
  }

  .logs__event,
  .logs__subject {
    grid-column: span 2;
  }
}
</style>
