<script setup lang="ts">
/**
 * 运行指标面板（fe-06 §6 中部指标区）。
 *
 * 两个刻意的实现选择：
 *
 * 1. **时间轴而不是类目轴**。类目轴要「整数组替换」xAxis.data 才能追加，
 *    每 2 秒重建一次上千个字符串；时间轴把横坐标编码在数据点里，
 *    追加只碰 series.data，正是 fe-06 §6 要求的增量 append。
 *
 * 2. **降采样阈值触发时整体重绘一次**。缓冲 > 900 点后按「2 点合 1、取后者」
 *    绘制；采样点位与原始点位不是一一对应，继续按 ts 追加会让折线出现锯齿错位。
 *    因此采样模式切换、或缓冲从头部被裁剪（首个点的 ts 变化）时重建一次，
 *    其余情况只 append。累计量单调递增，取后者不失真。
 *
 * ECharts 6 按需引入（fe-01 §6.5）：只 use 用到的图表与组件，
 * 禁止 `import * as echarts from 'echarts'` 全量打包。实例在 onBeforeUnmount 必须 dispose。
 */
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { BarChart, LineChart } from 'echarts/charts'
import { GridComponent, LegendComponent, TooltipComponent } from 'echarts/components'
import * as echarts from 'echarts/core'
import type { ECharts, EChartsCoreOption } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { SectionCard } from '@/components/common'
import { formatBytes, formatInt, formatRate, formatRatioPercent, formatRows, formatThroughput, formatDuration } from '@/utils/format'
import type { ExecutionBudget, ExecutionMetricPoint, RunDetail } from '@/types'

echarts.use([LineChart, BarChart, GridComponent, TooltipComponent, LegendComponent, CanvasRenderer])

const props = defineProps<{
  run: RunDetail
  /** 视图层维护的采样缓冲（已按上限裁剪） */
  metrics: ExecutionMetricPoint[]
  budget: ExecutionBudget
  /** 缓冲是否已从头部裁剪过（true 时必须显式告知，不能静默截断） */
  omitted: boolean
  /** 本轮追加与最近一次采样之间是否存在缺口（后端采集窗口跳变） */
  gap: boolean
}>()

/** 超过该点数后按 2 点合 1 绘制；原始缓冲不破坏 */
const DOWNSAMPLE_THRESHOLD = 900

/** 一次绘制的展示点：x 为毫秒时间戳，其余是四条线的取值 */
interface DisplayPoint {
  x: number
  throughput: number
  read_rows: number
  written_bytes: number
  /** 百分比（0–100），拒绝率固定 0–100% 轴 */
  reject_pct: number
}

interface MetricCard {
  key: string
  label: string
  value: string
  hint?: string
  danger?: boolean
}

const chartEl = ref<HTMLDivElement | null>(null)
let chart: ECharts | null = null
/** 已绘制的最大 x；只追加严格大于它的点，天然幂等 */
let renderedMaxX = 0
/** 已绘制序列的首个 x 与采样模式；两者之一变化即重建 */
let lastFirstX: number | null = null
let lastSampled = false

/**
 * 从 CSS 令牌取色。**不发明色值**：令牌缺失（如单测的 jsdom 里读不到样式表）时
 * 返回空串，由调用方退回 ECharts 默认调色板。
 */
function token(name: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim()
}

const palette = computed<string[]>(() => {
  const names = ['--elt-accent', '--elt-success', '--elt-warn', '--elt-danger']
  const colors = names.map(token)
  return colors.every((c) => c !== '') ? colors : []
})

const cards = computed<MetricCard[]>(() => {
  // 是否超预算：只用于标红提示，是否已硬中断只认 run.status（fe-06 §6）
  const overReject = props.run.reject_rate > props.budget.max_reject_rate
  return [
    { key: 'throughput', label: '平均吞吐', value: formatThroughput(props.run.avg_throughput) },
    { key: 'read', label: '读取行数', value: formatRows(props.run.read_rows) },
    { key: 'written', label: '写入行数', value: formatRows(props.run.written_rows) },
    {
      key: 'bytes',
      label: '写入字节',
      value: formatBytes(props.run.written_bytes),
      hint: `${formatInt(props.run.written_bytes)} B`,
    },
    { key: 'duration', label: '运行时长', value: formatDuration(props.run.duration_sec) },
    {
      key: 'reject',
      label: '拒绝率',
      value: formatRate(props.run.reject_rate),
      hint: `预算 ${formatRatioPercent(props.budget.max_reject_rate)}`,
      danger: overReject,
    },
    {
      key: 'amplification',
      label: '放大比',
      value: props.run.amplification_ratio.toFixed(2) + '×',
      hint: `上限 ${props.budget.max_amplification_ratio}×`,
    },
  ]
})

function toDisplay(p: ExecutionMetricPoint): DisplayPoint | null {
  const x = Date.parse(p.ts)
  if (!Number.isFinite(x)) {
    // 时间戳解析失败的点直接丢弃：画在 0 会让折线穿到 1970 年
    return null
  }
  return {
    x,
    throughput: p.throughput,
    read_rows: p.read_rows,
    written_bytes: p.written_bytes,
    reject_pct: p.reject_rate * 100,
  }
}

/** 展示序列：超阈值时按 2 点合 1（取后者），否则原样 */
function displayPoints(): DisplayPoint[] {
  const raw = props.metrics
  const sampled = raw.length > DOWNSAMPLE_THRESHOLD
  const source: ExecutionMetricPoint[] = []
  if (!sampled) {
    source.push(...raw)
  } else {
    for (let i = 1; i < raw.length; i += 2) {
      const item = raw[i]
      if (item !== undefined) {
        source.push(item)
      }
    }
    const tail = raw[raw.length - 1]
    // 点数为奇数时最后一个点没有配对对象，单独保留，否则最新点会丢失
    if (raw.length % 2 === 1 && tail !== undefined) {
      source.push(tail)
    }
  }

  const out: DisplayPoint[] = []
  for (const item of source) {
    const point = toDisplay(item)
    if (point !== null) {
      out.push(point)
    }
  }
  return out
}

function seriesOf(points: DisplayPoint[], pick: (p: DisplayPoint) => number): Array<[number, number]> {
  return points.map((p) => [p.x, pick(p)])
}

function baseOption(): EChartsCoreOption {
  const border = token('--elt-border-subtle')
  const axisCommon = {
    axisLine: { lineStyle: { color: border === '' ? undefined : border } },
    axisLabel: { fontSize: 10 },
    splitLine: { lineStyle: { color: border === '' ? undefined : border, type: 'dashed' as const } },
  }
  return {
    color: palette.value.length > 0 ? palette.value : undefined,
    animation: false,
    grid: { left: 74, right: 92, top: 34, bottom: 24 },
    legend: { top: 0, textStyle: { fontSize: 11, color: token('--elt-text-secondary') || undefined } },
    tooltip: { trigger: 'axis' },
    xAxis: {
      type: 'time',
      axisLine: axisCommon.axisLine,
      axisLabel: {
        fontSize: 10,
        formatter: (value: number): string => {
          // 横轴只给时刻；完整时间在 tooltip 里（避免每隔 2 秒一个标签挤成一片）
          return new Date(value).toLocaleTimeString('zh-CN', { hour12: false })
        },
      },
    },
    yAxis: [
      {
        type: 'value',
        name: '吞吐 行/秒',
        position: 'left',
        ...axisCommon,
        axisLabel: { fontSize: 10, formatter: (v: number): string => formatInt(v) },
      },
      {
        type: 'value',
        name: '读取行数',
        position: 'left',
        offset: 58,
        ...axisCommon,
        axisLabel: { fontSize: 10, formatter: (v: number): string => formatInt(v) },
      },
      {
        type: 'value',
        name: '写入字节',
        position: 'right',
        ...axisCommon,
        axisLabel: { fontSize: 10, formatter: (v: number): string => formatBytes(v) },
      },
      {
        type: 'value',
        name: '拒绝率 %',
        position: 'right',
        offset: 58,
        min: 0,
        max: 100,
        ...axisCommon,
        axisLabel: { fontSize: 10, formatter: (v: number): string => `${v}%` },
      },
    ],
    series: [
      { name: '吞吐 行/秒', type: 'line', yAxisIndex: 0, showSymbol: false, data: [] },
      { name: '读取行数', type: 'line', yAxisIndex: 1, showSymbol: false, data: [] },
      { name: '写入字节', type: 'line', yAxisIndex: 2, showSymbol: false, data: [] },
      { name: '拒绝率 %', type: 'line', yAxisIndex: 3, showSymbol: false, data: [] },
    ],
  }
}

function fullOption(points: DisplayPoint[]): EChartsCoreOption {
  const option = baseOption()
  return {
    ...option,
    series: [
      { name: '吞吐 行/秒', type: 'line', yAxisIndex: 0, showSymbol: false, data: seriesOf(points, (p) => p.throughput) },
      { name: '读取行数', type: 'line', yAxisIndex: 1, showSymbol: false, data: seriesOf(points, (p) => p.read_rows) },
      { name: '写入字节', type: 'line', yAxisIndex: 2, showSymbol: false, data: seriesOf(points, (p) => p.written_bytes) },
      { name: '拒绝率 %', type: 'line', yAxisIndex: 3, showSymbol: false, data: seriesOf(points, (p) => p.reject_pct) },
    ],
  }
}

function rebuild(points: DisplayPoint[]): void {
  if (chart === null) {
    return
  }
  chart.setOption(fullOption(points), { notMerge: false, lazyUpdate: true })
  const last = points[points.length - 1]
  renderedMaxX = last === undefined ? 0 : last.x
  lastFirstX = points[0]?.x ?? null
}

/** 只追加新点：四种取值的口径见 fullOption，此处保持一致 */
function append(points: DisplayPoint[]): void {
  if (chart === null) {
    return
  }
  const fresh = points.filter((p) => p.x > renderedMaxX)
  if (fresh.length === 0) {
    return
  }
  chart.setOption(
    {
      series: [
        { data: seriesOf(fresh, (p) => p.throughput) },
        { data: seriesOf(fresh, (p) => p.read_rows) },
        { data: seriesOf(fresh, (p) => p.written_bytes) },
        { data: seriesOf(fresh, (p) => p.reject_pct) },
      ],
    },
    { notMerge: false, lazyUpdate: true },
  )
  const last = fresh[fresh.length - 1]
  if (last !== undefined) {
    renderedMaxX = last.x
  }
}

function sync(): void {
  const points = displayPoints()
  const sampled = props.metrics.length > DOWNSAMPLE_THRESHOLD
  const firstX = points[0]?.x ?? null
  // 采样模式切换、或缓冲头部被裁剪（首点变化）时必须整体重建，否则追加会错位
  const needRebuild = lastFirstX !== firstX || sampled !== lastSampled
  lastSampled = sampled

  if (needRebuild) {
    rebuild(points)
    return
  }
  append(points)
}

function onResize(): void {
  chart?.resize()
}

onMounted(() => {
  if (chartEl.value === null) {
    return
  }
  chart = echarts.init(chartEl.value)
  chart.setOption(baseOption())
  sync()
  window.addEventListener('resize', onResize)
})

watch(() => props.metrics, sync)
watch(() => props.run.id, () => {
  // 同一个组件实例被复用到另一条运行（路由 params 变化）时必须清空重画，
  // 否则会把上一条运行的折线接着画下去
  renderedMaxX = 0
  lastFirstX = null
  lastSampled = false
  chart?.clear()
  if (chart !== null) {
    chart.setOption(baseOption())
  }
  sync()
})

onBeforeUnmount(() => {
  window.removeEventListener('resize', onResize)
  // 不 dispose 会在 SPA 里累积实例与 canvas，长会话下内存一路涨
  chart?.dispose()
  chart = null
})
</script>

<template>
  <SectionCard title="运行指标" :padded="true">
    <template #extra>
      <span v-if="omitted" class="metrics__notice">更早数据已省略（仅保留最近缓冲）</span>
      <span v-if="gap" class="metrics__notice metrics__notice--warn">采样存在缺口，折线未连贯</span>
      <span class="metrics__count">{{ metrics.length }} 点</span>
    </template>

    <div class="metrics__cards">
      <div v-for="card in cards" :key="card.key" class="metrics__card">
        <span class="metrics__card-label">{{ card.label }}</span>
        <span class="metrics__card-value elt-mono" :class="{ 'is-danger': card.danger }">{{ card.value }}</span>
        <span v-if="card.hint !== undefined" class="metrics__card-hint elt-mono">{{ card.hint }}</span>
      </div>
    </div>

    <div class="metrics__chart-wrap">
      <div ref="chartEl" class="metrics__chart" />
      <!-- 缓冲裁剪的说明同时放在图内左上的灰底块上：图被截图传阅时这条信息不能丢 -->
      <span v-if="omitted" class="metrics__chart-omitted">更早数据已省略</span>
      <p v-if="metrics.length === 0" class="metrics__empty">
        暂无采样点：运行尚未开始，或后端采集尚未产生第一批指标。
      </p>
    </div>
  </SectionCard>
</template>

<style scoped>
.metrics__notice {
  padding: 1px 6px;
  font-size: 11px;
  color: var(--elt-text-secondary);
  background-color: var(--elt-bg-elevated);
  border-radius: var(--elt-radius-pill);
}

.metrics__notice--warn {
  color: var(--elt-warn);
}

.metrics__count {
  font-size: 11px;
  color: var(--elt-text-muted);
}

.metrics__cards {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(132px, 1fr));
  gap: 10px;
  margin-bottom: 14px;
}

.metrics__card {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 8px 10px;
  background-color: var(--elt-bg-elevated);
  border-radius: var(--elt-radius-control);
}

.metrics__card-label {
  font-size: 11px;
  color: var(--elt-text-muted);
}

.metrics__card-value {
  font-size: 14px;
  font-weight: 600;
  color: var(--elt-text-primary);
}

.metrics__card-value.is-danger {
  color: var(--elt-danger);
}

.metrics__card-hint {
  font-size: 11px;
  color: var(--elt-text-muted);
}

.metrics__chart-wrap {
  position: relative;
}

.metrics__chart {
  width: 100%;
  height: 260px;
}

.metrics__chart-omitted {
  position: absolute;
  top: 4px;
  left: 4px;
  padding: 1px 6px;
  font-size: 11px;
  color: var(--elt-text-muted);
  background-color: var(--elt-bg-elevated);
  border-radius: var(--elt-radius-control);
  pointer-events: none;
}

.metrics__empty {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  margin: 0;
  pointer-events: none;
  font-size: 12px;
  color: var(--elt-text-muted);
}
</style>
