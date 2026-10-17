<script setup lang="ts">
/**
 * 资源分布（fe-02 §6.2 前三行）：类型环形图 / 环境条形图 / 敏感级别堆叠条。
 *
 * 三张图共用一份数据，因此：
 * - **空数组不初始化 ECharts 实例**，改为 EmptyState（否则会留下一块空画布）；
 * - **一个 ResizeObserver 管三个实例**，避免每张图各挂一个观察器造成泄漏；
 * - 类别名一律走 `enumText`，后端新增类型时显示原值而不是空白（fe-01 §6.3）。
 */
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { init, use } from 'echarts/core'
import { BarChart, PieChart } from 'echarts/charts'
import {
  GridComponent,
  LegendComponent,
  TitleComponent,
  TooltipComponent,
} from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import type { ComposeOption, EChartsType } from 'echarts/core'
import type { BarSeriesOption, PieSeriesOption } from 'echarts/charts'
import type {
  GridComponentOption,
  LegendComponentOption,
  TitleComponentOption,
  TooltipComponentOption,
} from 'echarts/components'
import { EmptyState, SectionCard } from '@/components/common'
import { enumText } from '@/utils/enums'
import { formatInt } from '@/utils/format'
import type { ResourceDistribution, SensitiveLevel } from '@/types'

use([PieChart, BarChart, GridComponent, TooltipComponent, LegendComponent, TitleComponent, CanvasRenderer])

type DistOption = ComposeOption<
  | PieSeriesOption
  | BarSeriesOption
  | GridComponentOption
  | TooltipComponentOption
  | LegendComponentOption
  | TitleComponentOption
>

const props = defineProps<{
  distribution: ResourceDistribution | null
  loading: boolean
}>()

/** 敏感度递进的顺序与配色：从公开到受限逐级加深，颜色全部取自 --elt-* 令牌 */
const LEVEL_ORDER: readonly SensitiveLevel[] = ['public', 'internal', 'confidential', 'restricted']
const LEVEL_COLOR_TOKEN: Record<SensitiveLevel, string> = {
  public: '--elt-text-muted',
  internal: '--elt-accent',
  confidential: '--elt-warn',
  restricted: '--elt-danger',
}

const pieEl = ref<HTMLDivElement | null>(null)
const envEl = ref<HTMLDivElement | null>(null)
const levelEl = ref<HTMLDivElement | null>(null)

const instances = new Map<string, EChartsType>()
let observer: ResizeObserver | null = null

const byType = computed(() => props.distribution?.by_type ?? [])
const byEnv = computed(() => props.distribution?.by_env ?? [])
const byLevel = computed(() =>
  (props.distribution?.by_sensitive_level ?? []).filter((entry) => entry.count > 0),
)

function cssVar(name: string, fallback: string): string {
  const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim()
  return value === '' ? fallback : value
}

/** 三张图共用的一套暗色外观；写成函数是因为令牌值只能在渲染时读取 */
function theme(): {
  border: string
  textMuted: string
  textSecondary: string
  textPrimary: string
  surface: string
  accent: string
  danger: string
} {
  return {
    border: cssVar('--elt-border-subtle', '#27344d'),
    textMuted: cssVar('--elt-text-muted', '#64748b'),
    textSecondary: cssVar('--elt-text-secondary', '#94a6c0'),
    textPrimary: cssVar('--elt-text-primary', '#e6edf7'),
    surface: cssVar('--elt-bg-elevated', '#1a2437'),
    accent: cssVar('--elt-accent', '#3b82f6'),
    danger: cssVar('--elt-danger', '#ef4444'),
  }
}

const pieOption = computed<DistOption | null>(() => {
  const list = byType.value
  if (list.length === 0) {
    return null
  }
  const t = theme()
  // 环形图中心的总数是同一组数值的展示性求和（不是跨数据源的聚合口径）
  const total = list.reduce((sum, entry) => sum + entry.count, 0)

  return {
    animation: false,
    tooltip: {
      trigger: 'item',
      backgroundColor: t.surface,
      borderColor: t.border,
      textStyle: { color: t.textPrimary, fontSize: 12 },
    },
    title: {
      text: formatInt(total),
      subtext: '连接总数',
      left: '30%',
      top: '44%',
      textAlign: 'center',
      textStyle: { color: t.textPrimary, fontSize: 16, fontWeight: 600 },
      subtextStyle: { color: t.textMuted, fontSize: 11 },
    },
    legend: {
      type: 'scroll',
      orient: 'vertical',
      right: 0,
      top: 'middle',
      icon: 'circle',
      itemWidth: 8,
      itemHeight: 8,
      textStyle: { color: t.textSecondary, fontSize: 11 },
    },
    series: [
      {
        type: 'pie',
        radius: ['45%', '70%'],
        center: ['30%', '50%'],
        avoidLabelOverlap: true,
        label: { show: false },
        labelLine: { show: false },
        // 图例点击只做本地高亮，不重新请求（fe-02 §6.2）
        data: list.map((entry) => ({
          name: enumText('DataSourceType', entry.type),
          value: entry.count,
        })),
      },
    ],
  }
})

const envOption = computed<DistOption | null>(() => {
  const list = byEnv.value
  if (list.length === 0) {
    return null
  }
  const t = theme()

  return {
    animation: false,
    grid: { left: 4, right: 28, top: 6, bottom: 2, containLabel: true },
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'shadow' },
      backgroundColor: t.surface,
      borderColor: t.border,
      textStyle: { color: t.textPrimary, fontSize: 12 },
    },
    xAxis: {
      type: 'value',
      axisLine: { show: false },
      axisLabel: { color: t.textMuted, fontSize: 11 },
      splitLine: { lineStyle: { color: t.border, type: 'dashed' } },
    },
    yAxis: {
      type: 'category',
      data: list.map((entry) => enumText('Env', entry.env)),
      axisTick: { show: false },
      axisLine: { lineStyle: { color: t.border } },
      axisLabel: { color: t.textSecondary, fontSize: 12 },
    },
    series: [
      {
        type: 'bar',
        barWidth: 14,
        // prod 用警示色：环境判断错一次就是写生产表
        data: list.map((entry) => ({
          value: entry.count,
          itemStyle: {
            color: entry.env === 'prod' ? t.danger : t.accent,
            borderRadius: [0, 4, 4, 0],
          },
          emphasis: { itemStyle: { color: entry.env === 'prod' ? t.danger : t.accent } },
        })),
        label: { show: true, position: 'right', color: t.textSecondary, fontSize: 11 },
      },
    ],
  }
})

const levelOption = computed<DistOption | null>(() => {
  const list = byLevel.value
  if (list.length === 0) {
    return null
  }
  const t = theme()
  const countOf = new Map(list.map((entry) => [entry.sensitive_level, entry.count]))

  return {
    animation: false,
    grid: { left: 4, right: 8, top: 10, bottom: 26, containLabel: true },
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'shadow' },
      backgroundColor: t.surface,
      borderColor: t.border,
      textStyle: { color: t.textPrimary, fontSize: 12 },
    },
    legend: {
      bottom: 0,
      icon: 'circle',
      itemWidth: 8,
      itemHeight: 8,
      textStyle: { color: t.textSecondary, fontSize: 11 },
    },
    xAxis: { type: 'value', show: false },
    yAxis: { type: 'category', data: ['敏感级别'], show: false },
    series: LEVEL_ORDER.filter((level) => (countOf.get(level) ?? 0) > 0).map((level) => ({
      name: enumText('SensitiveLevel', level),
      type: 'bar' as const,
      stack: 'level',
      barWidth: 22,
      data: [countOf.get(level) ?? 0],
      itemStyle: { color: cssVar(LEVEL_COLOR_TOKEN[level], t.textMuted) },
    })),
  }
})

/** option 为 null 表示「该维度没有数据」，此时销毁实例并交给 EmptyState */
function syncChart(key: string, element: HTMLDivElement | null, option: DistOption | null): void {
  const existing = instances.get(key)
  if (option === null || element === null) {
    if (element !== null) {
      observer?.unobserve(element)
    }
    existing?.dispose()
    instances.delete(key)
    return
  }

  let instance = existing
  if (instance === undefined) {
    instance = init(element)
    instances.set(key, instance)
    observer?.observe(element)
  }
  instance.setOption(option, true)
}

function disposeAll(): void {
  for (const instance of instances.values()) {
    instance.dispose()
  }
  instances.clear()
  observer?.disconnect()
  observer = null
}

function render(): void {
  if (props.loading) {
    disposeAll()
    return
  }
  if (observer === null) {
    observer = new ResizeObserver(() => {
      for (const instance of instances.values()) {
        instance.resize()
      }
    })
  }
  syncChart('by_type', pieEl.value, pieOption.value)
  syncChart('by_env', envEl.value, envOption.value)
  syncChart('by_level', levelEl.value, levelOption.value)
}

onMounted(render)
watch([() => props.distribution, () => props.loading], render, { flush: 'post' })
onBeforeUnmount(() => {
  disposeAll()
})
</script>

<template>
  <SectionCard title="资源分布" :loading="loading" :skeleton-height="264">
    <div class="dist">
      <section class="dist__cell">
        <h3 class="dist__title">数据源类型</h3>
        <div v-show="pieOption !== null" ref="pieEl" class="dist__canvas" />
        <EmptyState
          v-if="pieOption === null"
          variant="no-data"
          title="暂无数据源类型分布"
          description="登记数据源后，这里会按类型展示连接占比。"
        />
      </section>

      <section class="dist__cell">
        <h3 class="dist__title">环境分布</h3>
        <div v-show="envOption !== null" ref="envEl" class="dist__canvas" />
        <EmptyState
          v-if="envOption === null"
          variant="no-data"
          title="暂无环境分布"
          description="登记数据源后，这里会按 dev / staging / prod 展示连接数。"
        />
      </section>

      <section class="dist__cell">
        <h3 class="dist__title">敏感级别分布</h3>
        <div v-show="levelOption !== null" ref="levelEl" class="dist__canvas" />
        <EmptyState
          v-if="levelOption === null"
          variant="no-data"
          title="暂无敏感级别分布"
          description="数据源声明敏感级别后，这里会按级别展示构成比例。"
        />
      </section>
    </div>
  </SectionCard>
</template>

<style scoped>
.dist {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 14px;
}

.dist__cell {
  min-width: 0;
}

.dist__title {
  margin: 0 0 6px;
  font-size: 12px;
  font-weight: 600;
  line-height: 18px;
  color: var(--elt-text-secondary);
}

.dist__canvas {
  width: 100%;
  height: 200px;
}

@media (max-width: 1100px) {
  .dist {
    grid-template-columns: minmax(0, 1fr);
  }
}
</style>
