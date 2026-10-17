<script setup lang="ts">
/**
 * 吞吐与拒绝率趋势（fe-02 §6.2 第四行）。
 *
 * 实现要点：
 * 1. **按需注册 ECharts**（fe-01 §6.5）：只 import 线图与所需组件，禁止 `import * as echarts`。
 * 2. **颜色取 `--elt-*` 令牌**：ECharts 的 canvas 读不到 CSS 变量，所以在渲染前
 *    用 getComputedStyle 取一次真实值，保证图表与界面同一套暗色配色。
 * 3. **概览不画预算阈值虚线**：预算逐张准备单不同，聚合到项目维度没有唯一阈值（fe-02 §10）；
 *    阈值线留在 fe-06 的单次运行视图里。
 */
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { init, use } from 'echarts/core'
import { LineChart } from 'echarts/charts'
import { GridComponent, LegendComponent, TooltipComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import type { ComposeOption, EChartsType } from 'echarts/core'
import type { LineSeriesOption } from 'echarts/charts'
import type {
  GridComponentOption,
  LegendComponentOption,
  TooltipComponentOption,
} from 'echarts/components'
import { EmptyState, SectionCard } from '@/components/common'
import { formatDateTime, formatRate, formatThroughput } from '@/utils/format'
import type { MonitorSeries, MonitorSeriesPoint } from '@/types'

use([LineChart, GridComponent, TooltipComponent, LegendComponent, CanvasRenderer])

type TrendOption = ComposeOption<
  LineSeriesOption | GridComponentOption | TooltipComponentOption | LegendComponentOption
>

const props = defineProps<{
  series: MonitorSeries | null
  loading: boolean
}>()

const THROUGHPUT_NAME = '吞吐（行/秒）'
const REJECT_NAME = '拒绝率'

const container = ref<HTMLDivElement | null>(null)
const points = computed<MonitorSeriesPoint[]>(() => props.series?.points ?? [])
const hasData = computed<boolean>(() => points.value.length > 0)

let chart: EChartsType | null = null
let observer: ResizeObserver | null = null

function cssVar(name: string, fallback: string): string {
  const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim()
  return value === '' ? fallback : value
}

/** 令牌是 hex，ECharts 的填充需要 rgba 才能半透明；非 hex 时原样返回，不做猜测 */
function withAlpha(color: string, alpha: number): string {
  const matched = /^#([0-9a-f]{6})$/i.exec(color.trim())
  const hex = matched?.[1]
  if (hex === undefined) {
    return color
  }
  const int = Number.parseInt(hex, 16)
  return `rgba(${(int >> 16) & 255}, ${(int >> 8) & 255}, ${int & 255}, ${alpha})`
}

/** 跨天看「日 时:分」，同一天只看「时:分」——24h 窗口下每格都带日期只会互相压叠 */
const showDate = computed<boolean>(() => {
  const list = points.value
  const first = list[0]
  const last = list[list.length - 1]
  if (first === undefined || last === undefined) {
    return false
  }
  const span = new Date(last.ts).getTime() - new Date(first.ts).getTime()
  return Number.isFinite(span) && span > 48 * 60 * 60 * 1000
})

function axisLabelOf(ts: string): string {
  const text = formatDateTime(ts)
  return showDate.value ? text.slice(5, 16) : text.slice(11, 16)
}

interface TooltipItem {
  seriesName?: string
  value?: unknown
  axisValueLabel?: string
}

function buildOption(): TrendOption {
  const accent = cssVar('--elt-accent', '#3b82f6')
  const warn = cssVar('--elt-warn', '#f59e0b')
  const border = cssVar('--elt-border-subtle', '#27344d')
  const textMuted = cssVar('--elt-text-muted', '#64748b')
  const textSecondary = cssVar('--elt-text-secondary', '#94a6c0')
  const surface = cssVar('--elt-bg-elevated', '#1a2437')
  const textPrimary = cssVar('--elt-text-primary', '#e6edf7')
  const list = points.value

  return {
    // 2 秒一轮询下动画会让曲线一直处于过渡态，读数反而更慢
    animation: false,
    grid: { left: 4, right: 6, top: 30, bottom: 2, containLabel: true },
    tooltip: {
      trigger: 'axis',
      backgroundColor: surface,
      borderColor: border,
      textStyle: { color: textPrimary, fontSize: 12 },
      // 两个 y 轴量纲不同，tooltip 里分别格式化，避免出现「0.0101」这种读不出来的值
      formatter: (raw: unknown): string => {
        const items: TooltipItem[] = Array.isArray(raw) ? (raw as TooltipItem[]) : []
        const head = items[0]?.axisValueLabel ?? ''
        const lines = items.map((item) => {
          const value = typeof item.value === 'number' ? item.value : null
          const text = item.seriesName === REJECT_NAME ? formatRate(value) : formatThroughput(value)
          return `${item.seriesName ?? ''}　${text}`
        })
        return [head, ...lines].join('<br/>')
      },
    },
    legend: {
      top: 0,
      right: 0,
      icon: 'roundRect',
      itemWidth: 10,
      itemHeight: 10,
      textStyle: { color: textSecondary, fontSize: 12 },
      data: [THROUGHPUT_NAME, REJECT_NAME],
    },
    xAxis: {
      type: 'category',
      boundaryGap: false,
      data: list.map((point) => axisLabelOf(point.ts)),
      axisLine: { lineStyle: { color: border } },
      axisTick: { show: false },
      axisLabel: { color: textMuted, fontSize: 11, hideOverlap: true },
    },
    yAxis: [
      {
        type: 'value',
        name: '行/秒',
        nameTextStyle: { color: textMuted, fontSize: 11 },
        axisLine: { show: false },
        axisLabel: { color: textMuted, fontSize: 11 },
        splitLine: { lineStyle: { color: border, type: 'dashed' } },
      },
      {
        type: 'value',
        name: '拒绝率',
        nameTextStyle: { color: textMuted, fontSize: 11 },
        axisLine: { show: false },
        // numeric(6,4) 的比率轴直接按百分比刻度，省得用户自己换算
        axisLabel: {
          color: textMuted,
          fontSize: 11,
          formatter: (value: number) => `${(value * 100).toFixed(2)}%`,
        },
        splitLine: { show: false },
      },
    ],
    series: [
      {
        name: THROUGHPUT_NAME,
        type: 'line',
        smooth: true,
        showSymbol: false,
        data: list.map((point) => point.throughput),
        lineStyle: { width: 2, color: accent },
        itemStyle: { color: accent },
        areaStyle: { color: withAlpha(accent, 0.16) },
      },
      {
        name: REJECT_NAME,
        type: 'line',
        yAxisIndex: 1,
        smooth: true,
        showSymbol: false,
        data: list.map((point) => point.reject_rate),
        lineStyle: { width: 2, color: warn },
        itemStyle: { color: warn },
      },
    ],
  }
}

function dispose(): void {
  observer?.disconnect()
  observer = null
  chart?.dispose()
  chart = null
}

function render(): void {
  // 空数据不初始化实例（fe-02 §6.2）：画布留在那里只会显示成「加载不动」
  if (!hasData.value || props.loading) {
    dispose()
    return
  }
  const element = container.value
  if (element === null) {
    return
  }
  if (chart === null) {
    chart = init(element)
    // 容器尺寸变化由 ResizeObserver 驱动，不用 window.resize（侧栏折叠也会改变宽度）
    observer = new ResizeObserver(() => {
      chart?.resize()
    })
    observer.observe(element)
  }
  chart.setOption(buildOption(), true)
}

onMounted(render)
watch([() => props.series, () => props.loading], render, { flush: 'post' })
onBeforeUnmount(dispose)
</script>

<template>
  <SectionCard title="吞吐与拒绝率趋势" :loading="loading" :skeleton-height="264">
    <template #extra>
      <span v-if="hasData" class="trend__points elt-mono">{{ points.length }} 采样点</span>
    </template>

    <div v-show="hasData" ref="container" class="trend__canvas" />

    <!-- 空的是「这个时间窗」而不是「系统里没有运行」：属于筛选未命中，用 no-match 引导换窗口 -->
    <EmptyState
      v-if="!hasData"
      variant="no-match"
      title="当前时间窗内没有吞吐数据"
      description="趋势按所选时间范围聚合。可切到 7 天或 30 天再看；若仍未出现，说明这段时间内没有运行记录。"
    />
  </SectionCard>
</template>

<style scoped>
.trend__points {
  font-size: 11px;
  color: var(--elt-text-muted);
}

.trend__canvas {
  width: 100%;
  height: 224px;
}
</style>
