<script setup lang="ts">
/**
 * 质量分流面板（fe-06 §6）。
 *
 * 三件必须说清楚的事，否则这块面板会被误读：
 *
 * 1. **分流是影子表语义**。通过/拒绝的行数描述的是「影子表里发生了什么」，
 *    数据还没进正式表；只有质量校验通过 + 原子 Swap 成功才算生效。
 *    把这句话放在数字旁边，而不是藏在文档里。
 *
 * 2. **未通过时没有强制发布**。`passed=false` 就是「禁止发布」，
 *    本组件不提供、也不会提供任何「仍然发布」的入口（fe-07 §7）。
 *
 * 3. **分布图用横向条形而不是饼图**：8 个类目且量级常差几个数量级，
 *    饼图会把小类目压成看不见的细缝，而小类目恰恰常是安全信号
 *    （如 E_SENSITIVE_PLAINTEXT）。
 */
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { BarChart } from 'echarts/charts'
import { GridComponent, TooltipComponent } from 'echarts/components'
import * as echarts from 'echarts/core'
import type { ECharts, ECElementEvent, EChartsCoreOption } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { EmptyState, SectionCard, SensitiveText, StatusTag } from '@/components/common'
import { enumLabel, enumText } from '@/utils/enums'
import { formatDateTime, formatInt, formatRate, formatRows } from '@/utils/format'
import type { ErrorCodeLabel, QualityReport } from '@/types'

echarts.use([BarChart, GridComponent, TooltipComponent, CanvasRenderer])

const props = withDefaults(
  defineProps<{
    report: QualityReport | null
    loading: boolean
    /** 影子表名，用于把「分流结果在哪」讲具体 */
    shadowTable?: string | null
    /** 正式表名，用于「尚未进主表」的对照 */
    formalTable?: string | null
    /** 错误表名：拒绝行落在这里（只存受保护摘要） */
    errorTable?: string | null
  }>(),
  { shadowTable: null, formalTable: null, errorTable: null },
)

const emit = defineEmits<{
  /** 点某个错误码 → 跳到错误明细并按其筛选 */
  'open-error-detail': [code: ErrorCodeLabel]
  retry: []
}>()

const chartEl = ref<HTMLDivElement | null>(null)
let chart: ECharts | null = null

/** 计数降序；只保留 > 0 的类目，避免画出 8 根零长条 */
const distribution = computed<Array<{ code: string; count: number }>>(() => {
  const dist = props.report?.error_code_dist ?? {}
  return Object.entries(dist)
    .filter((entry): entry is [string, number] => typeof entry[1] === 'number' && entry[1] > 0)
    .map(([code, count]) => ({ code, count }))
    .sort((a, b) => b.count - a.count)
})

const ruleHits = computed<Array<{ ruleId: string; count: number }>>(() => {
  const hits = props.report?.rule_hits ?? {}
  return Object.entries(hits)
    .map(([ruleId, count]) => ({ ruleId, count }))
    .sort((a, b) => b.count - a.count)
})

/** 未知取值不隐藏：enumText 会回落成原始枚举字符串 */
function codeText(code: string): string {
  return enumText('ErrorCodeLabel', code)
}

function isUnknownCode(code: string): boolean {
  return enumLabel('ErrorCodeLabel', code).unknown
}

function token(name: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim()
}

/**
 * E_SENSITIVE_PLAINTEXT 恒定用警示色：它是 P0 信号，
 * 在量级上往往很小，如果按「量小就画得淡」处理会被彻底忽略。
 */
function barColor(code: string): string | undefined {
  if (code === 'E_SENSITIVE_PLAINTEXT') {
    const danger = token('--elt-danger')
    return danger === '' ? undefined : danger
  }
  const accent = token('--elt-accent')
  return accent === '' ? undefined : accent
}

function renderChart(): void {
  if (chart === null) {
    return
  }
  const items = distribution.value
  if (items.length === 0) {
    chart.clear()
    return
  }
  const option: EChartsCoreOption = {
    animation: false,
    grid: { left: 128, right: 48, top: 8, bottom: 24 },
    tooltip: { trigger: 'item' },
    xAxis: { type: 'value', axisLabel: { fontSize: 10, formatter: (v: number): string => formatInt(v) } },
    yAxis: {
      type: 'category',
      // 反序：ECharts 的类目轴自下而上排列，反序后「最多」显示在最上面
      data: items.map((i) => codeText(i.code)).reverse(),
      axisLabel: { fontSize: 11 },
    },
    series: [
      {
        type: 'bar',
        barMaxWidth: 16,
        data: items
          .map((i) => ({ value: i.count, itemStyle: { color: barColor(i.code) } }))
          .reverse(),
      },
    ],
  }
  chart.setOption(option, { notMerge: true })
}

onMounted(() => {
  if (chartEl.value === null) {
    return
  }
  chart = echarts.init(chartEl.value)
  chart.on('click', (params: ECElementEvent) => {
    // 图上的类目名是中文标签，点击要回到枚举取值再交给父级
    const hit = distribution.value.find((i) => codeText(i.code) === params.name)
    if (hit !== undefined) {
      emit('open-error-detail', hit.code as ErrorCodeLabel)
    }
  })
  renderChart()
})

watch(distribution, renderChart)

onBeforeUnmount(() => {
  // 不 dispose 会随「切换 tab」反复泄漏 canvas
  chart?.dispose()
  chart = null
})
</script>

<template>
  <SectionCard title="质量分流" :loading="loading && report === null" :skeleton-height="220">
    <template #extra>
      <el-button size="small" :loading="loading" @click="emit('retry')">重新加载</el-button>
    </template>

    <EmptyState
      v-if="report === null"
      variant="no-data"
      title="尚未生成质量报告"
      description="质量报告在运行进入终态后由后端生成；运行中或排队中时这里为空是正常的。"
    />

    <template v-else>
      <el-alert v-if="report.passed" type="success" :closable="false" show-icon title="质量校验通过，已允许原子 Swap 发布到正式表" />
      <el-alert
        v-else
        type="error"
        :closable="false"
        show-icon
        title="质量未通过，禁止发布"
        description="拒绝率或规则命中超出契约上限。平台不提供「强制发布」入口：正式表未被修改，可执行受管回滚后修正再重跑。"
      />

      <p class="quality__semantics">
        以下数字来自<b>影子表</b>分流过程：通过的行写入影子表
        <SensitiveText :value="shadowTable" mono />，
        拒绝的行写入错误表 <SensitiveText :value="errorTable" mono />（只保留受保护摘要）。
        <b>数据尚未进入正式表</b>
        <SensitiveText :value="formalTable" mono />，需原子 Swap 成功后才生效。
      </p>

      <div class="quality__stats">
        <div class="quality__stat">
          <span class="quality__stat-label">通过行数</span>
          <span class="quality__stat-value elt-mono">{{ formatRows(report.valid_rows) }}</span>
        </div>
        <div class="quality__stat">
          <span class="quality__stat-label">拒绝行数</span>
          <span class="quality__stat-value elt-mono quality__stat-value--danger">{{ formatRows(report.rejected_rows) }}</span>
        </div>
        <div class="quality__stat">
          <span class="quality__stat-label">拒绝率</span>
          <span class="quality__stat-value elt-mono">{{ formatRate(report.reject_rate) }}</span>
        </div>
        <div class="quality__stat">
          <span class="quality__stat-label">报告生成时间</span>
          <span class="quality__stat-value elt-mono">{{ formatDateTime(report.generated_at) }}</span>
        </div>
      </div>

      <h3 class="quality__subtitle">错误码分布</h3>
      <div v-if="distribution.length === 0" class="quality__placeholder">本次运行没有拒绝行，错误码分布为空。</div>
      <div v-else class="quality__chart-wrap">
        <div ref="chartEl" class="quality__chart" />
        <ul class="quality__codes">
          <li v-for="item in distribution" :key="item.code" class="quality__code-item">
            <button type="button" class="quality__code-link" @click="emit('open-error-detail', item.code as ErrorCodeLabel)">
              <StatusTag kind="ErrorCodeLabel" :value="item.code" />
              <span class="quality__code-label">{{ codeText(item.code) }}</span>
            </button>
            <span class="quality__code-count elt-mono">{{ formatInt(item.count) }}</span>
            <span v-if="isUnknownCode(item.code)" class="quality__code-unknown">未知错误码（后端可能已升级）</span>
          </li>
        </ul>
      </div>

      <h3 class="quality__subtitle">规则命中</h3>
      <div v-if="ruleHits.length === 0" class="quality__placeholder">没有规则命中记录。</div>
      <el-table v-else :data="ruleHits" size="small">
        <el-table-column prop="ruleId" label="规则 ID" min-width="140">
          <template #default="{ row }">
            <SensitiveText :value="row.ruleId" mono />
          </template>
        </el-table-column>
        <el-table-column prop="count" label="命中行数" width="120" align="right">
          <template #default="{ row }">
            <span class="elt-mono">{{ formatInt(row.count) }}</span>
          </template>
        </el-table-column>
      </el-table>

      <h3 class="quality__subtitle">规则诊断摘要</h3>
      <div v-if="report.diagnostics.length === 0" class="quality__placeholder">没有规则级诊断记录。</div>
      <el-table v-else :data="report.diagnostics" size="small">
        <el-table-column prop="rule_id" label="规则" min-width="120">
          <template #default="{ row }">
            <SensitiveText :value="row.rule_id" mono />
          </template>
        </el-table-column>
        <el-table-column prop="field" label="字段" min-width="120">
          <template #default="{ row }">
            <SensitiveText :value="row.field" mono />
          </template>
        </el-table-column>
        <el-table-column label="错误码" width="160">
          <template #default="{ row }">
            <StatusTag kind="ErrorCodeLabel" :value="row.error_code" />
          </template>
        </el-table-column>
        <el-table-column prop="summary" label="摘要（后端已脱敏，不含原始值）" min-width="260">
          <template #default="{ row }">
            <SensitiveText :value="row.summary" multiline />
          </template>
        </el-table-column>
        <el-table-column label="" width="110" align="right">
          <template #default="{ row }">
            <el-button link size="small" @click="emit('open-error-detail', row.error_code as ErrorCodeLabel)">
              错误明细
            </el-button>
          </template>
        </el-table-column>
      </el-table>

      <p class="quality__footnote">
        错误明细只包含受保护摘要（哈希 + 截断），平台不保存错误行原文，
        因此本页不提供「查看原始值 / 下载明细」入口；导出功能导出的也是同一份摘要。
      </p>
    </template>
  </SectionCard>
</template>

<style scoped>
.quality__semantics {
  margin: 12px 0 0;
  font-size: 12px;
  line-height: 1.8;
  color: var(--elt-text-secondary);
}

.quality__stats {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
  gap: 10px;
  margin-top: 12px;
}

.quality__stat {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 8px 10px;
  background-color: var(--elt-bg-elevated);
  border-radius: var(--elt-radius-control);
}

.quality__stat-label {
  font-size: 11px;
  color: var(--elt-text-muted);
}

.quality__stat-value {
  font-size: 14px;
  font-weight: 600;
  color: var(--elt-text-primary);
}

.quality__stat-value--danger {
  color: var(--elt-danger);
}

.quality__subtitle {
  margin: 18px 0 8px;
  font-size: 13px;
  font-weight: 600;
  color: var(--elt-text-primary);
}

.quality__placeholder {
  font-size: 12px;
  color: var(--elt-text-muted);
}

.quality__chart-wrap {
  display: grid;
  grid-template-columns: minmax(0, 1.2fr) minmax(240px, 1fr);
  gap: 16px;
  align-items: start;
}

.quality__chart {
  width: 100%;
  height: 220px;
}

.quality__codes {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 0;
  margin: 0;
  list-style: none;
}

.quality__code-item {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
  font-size: 12px;
}

.quality__code-link {
  display: inline-flex;
  gap: 6px;
  align-items: center;
  padding: 0;
  cursor: pointer;
  background: none;
  border: none;
}

.quality__code-label {
  color: var(--elt-text-secondary);
}

.quality__code-count {
  color: var(--elt-text-primary);
}

.quality__code-unknown {
  font-size: 11px;
  color: var(--elt-warn);
}

.quality__footnote {
  margin: 12px 0 0;
  font-size: 11px;
  color: var(--elt-text-muted);
}

@media (max-width: 900px) {
  .quality__chart-wrap {
    grid-template-columns: 1fr;
  }
}
</style>
