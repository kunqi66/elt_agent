<script setup lang="ts">
/**
 * Benchmark 五项指标与基线对比（fe-07 §6）。
 *
 * 三条刻意定死的规则，不要「顺手优化」掉：
 * 1. `p0p1_block_rate` 只要低于 100% 就判红，**不与基线比较**——
 *    拦截率是安全底线，从 99% 提升到 99.5% 仍然是「有 P0 漏过去了」，
 *    拿它和基线比会得出「有所改善，可以接受」的误导结论。
 * 2. `false_positive_rate` 越低越好，方向与其余四项相反；
 *    方向搞反的话，误杀率上升会被画成绿色箭头。
 * 3. `baseline === null` 时**不画任何箭头**，只给一句「基线尚未写入」——
 *    没有基线却显示「持平」，等于替后端编了一个它没给过的结论。
 *
 * 同时这里只做「展示 + 比较方向」，不做任何分数推算：所有数值都来自后端。
 */
import { computed } from 'vue'
import { DASH, formatRate } from '@/utils/format'
import type { BenchmarkMetrics } from '@/types'

const props = withDefaults(
  defineProps<{
    metrics: BenchmarkMetrics | null
    baseline: BenchmarkMetrics | null
    /** 评测进行中：指标尚未生成，显示 `—` 而不是 0 */
    pending?: boolean
  }>(),
  { pending: false },
)

interface MetricMeta {
  key: keyof BenchmarkMetrics
  label: string
  hint: string
  /** 越高越好；`false` 表示越低越好（只有误杀率是这样） */
  higherIsBetter: boolean
  /** 固定红线：达到才判绿，且不参与基线比较 */
  target?: number
}

const METRICS: MetricMeta[] = [
  {
    key: 'gen_success_rate',
    label: '生成成功率',
    hint: '自然语言到可执行计划的成功率',
    higherIsBetter: true,
  },
  {
    key: 'gate_pass_rate',
    label: '门禁通过率',
    hint: '确定性门禁一次通过的比例',
    higherIsBetter: true,
  },
  {
    key: 'p0p1_block_rate',
    label: 'P0/P1 拦截率',
    hint: '高危用例被拦下的比例，红线 100%',
    higherIsBetter: true,
    target: 1,
  },
  {
    key: 'false_positive_rate',
    label: '误杀率',
    hint: '正常用例被拦下的比例，越低越好',
    higherIsBetter: false,
  },
  {
    key: 'recovery_rate',
    label: '有限自动修复成功率',
    hint: '门禁未过后被自动修复并通过的比例',
    higherIsBetter: true,
  },
]

/** 变化小于半个百分点就当作持平，避免抖动被读成趋势 */
const FLAT_EPSILON = 0.005

type Tone = 'ok' | 'bad' | 'muted' | 'none'

interface MetricView {
  key: string
  label: string
  hint: string
  value: string
  delta: 'up' | 'down' | 'flat' | 'none'
  deltaText: string
  tone: Tone
  note: string
}

function isFiniteNumber(value: number | undefined): value is number {
  return value !== undefined && Number.isFinite(value)
}

function buildView(meta: MetricMeta): MetricView {
  const value = props.metrics?.[meta.key]
  const base = 'none'
  if (props.pending || !isFiniteNumber(value)) {
    return {
      key: meta.key,
      label: meta.label,
      hint: meta.hint,
      value: DASH,
      delta: base,
      deltaText: '',
      tone: 'muted',
      note: props.pending ? '评测进行中，指标尚未生成' : '本次运行未给出该指标',
    }
  }

  // 固定红线：与基线无关，只看有没有达到目标
  if (meta.target !== undefined && value < meta.target) {
    return {
      key: meta.key,
      label: meta.label,
      hint: meta.hint,
      value: formatRate(value),
      delta: base,
      deltaText: '',
      tone: 'bad',
      note: '未达 100% 拦截：有高危用例漏过，与基线高低无关',
    }
  }

  const baselineValue = props.baseline?.[meta.key]
  if (!isFiniteNumber(baselineValue)) {
    return {
      key: meta.key,
      label: meta.label,
      hint: meta.hint,
      value: formatRate(value),
      delta: base,
      deltaText: '',
      tone: meta.target !== undefined ? 'ok' : 'none',
      note: meta.target !== undefined ? '红线达标' : '基线尚未写入',
    }
  }

  const diff = value - baselineValue
  if (Math.abs(diff) < FLAT_EPSILON) {
    return {
      key: meta.key,
      label: meta.label,
      hint: meta.hint,
      value: formatRate(value),
      delta: 'flat',
      deltaText: '与基线持平',
      tone: 'muted',
      note: `基线 ${formatRate(baselineValue)}`,
    }
  }

  const improved = meta.higherIsBetter ? diff > 0 : diff < 0
  const arrow = diff > 0 ? 'up' : 'down'
  return {
    key: meta.key,
    label: meta.label,
    hint: meta.hint,
    value: formatRate(value),
    delta: arrow,
    deltaText: `${diff > 0 ? '+' : ''}${(diff * 100).toFixed(2)} pt`,
    tone: improved ? 'ok' : 'bad',
    note: `基线 ${formatRate(baselineValue)}`,
  }
}

const views = computed<MetricView[]>(() => METRICS.map((meta) => buildView(meta)))

const baselineMissing = computed<boolean>(() => props.baseline === null)
</script>

<template>
  <div class="metrics">
    <p v-if="baselineMissing" class="metrics__notice">
      该套件的基线尚未写入，因此只报当前值、不显示任何对比箭头。
    </p>

    <div class="metrics__grid">
      <div
        v-for="item in views"
        :key="item.key"
        class="metrics__card"
        :class="`metrics__card--${item.tone}`"
      >
        <div class="metrics__label">{{ item.label }}</div>
        <div class="metrics__value elt-mono">{{ item.value }}</div>
        <div v-if="item.delta !== 'none'" class="metrics__delta">
          <span v-if="item.delta === 'up'">↑</span>
          <span v-else-if="item.delta === 'down'">↓</span>
          <span v-else>—</span>
          <span>{{ item.deltaText }}</span>
        </div>
        <div class="metrics__note">{{ item.note }}</div>
        <div class="metrics__hint">{{ item.hint }}</div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.metrics {
  display: flex;
  flex-direction: column;
}

.metrics__notice {
  margin: 0 0 10px;
  font-size: 12px;
  color: var(--elt-warn);
}

.metrics__grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 10px;
}

.metrics__card {
  padding: 12px;
  background-color: var(--elt-bg-elevated);
  border: 1px solid var(--elt-border-subtle);
  border-radius: var(--elt-radius-control);
}

/* 未达标用左边框着色而不是整块染红：整块红会把数值本身的对比度吃光 */
.metrics__card--bad {
  border-color: var(--elt-danger);
  box-shadow: inset 2px 0 0 0 var(--elt-danger);
}

.metrics__card--ok {
  border-color: var(--elt-border-subtle);
  box-shadow: inset 2px 0 0 0 var(--elt-success);
}

.metrics__label {
  font-size: 12px;
  color: var(--elt-text-secondary);
}

.metrics__value {
  margin-top: 4px;
  font-size: 17px;
  font-weight: 600;
  color: var(--elt-text-primary);
}

.metrics__delta {
  display: flex;
  gap: 4px;
  align-items: center;
  margin-top: 2px;
  font-size: 12px;
  color: var(--elt-text-secondary);
}

.metrics__card--bad .metrics__delta {
  color: var(--elt-danger);
}

.metrics__card--ok .metrics__delta {
  color: var(--elt-success);
}

.metrics__note {
  margin-top: 4px;
  font-size: 11px;
  line-height: 1.6;
  color: var(--elt-text-muted);
}

.metrics__card--bad .metrics__note {
  color: var(--elt-danger);
}

.metrics__hint {
  margin-top: 4px;
  font-size: 11px;
  line-height: 1.6;
  color: var(--elt-text-muted);
}
</style>
