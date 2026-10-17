<script setup lang="ts">
/**
 * 受监督量的预算进度条（fe-06 §6）。
 *
 * 为什么不能只画一根「总进度」：五个受监督量的量纲完全不同（行 / 字节 / 秒 / 比率），
 * 揉成一个百分比后只剩「有没有超」这一位信息，用户看不出到底是哪一项在逼近上限——
 * 而超预算硬中断恰恰是按单项触发的。
 *
 * 为什么颜色不能单独承担语义：「接近预算 / 已超预算」一定同时用文字写出来。
 * 颜色在色弱、投屏、截图传阅这三种场景里都会失效，而这条信息的后果是运行被硬中断。
 * 预警线也在 80% 处显式画一根刻度，而不是靠「到了橙色就是快到 80%」的默契。
 *
 * 是否「已硬中断」不在这里判断：那是 `run.status === 'interrupted'`，
 * 由父级决定；本组件只按当前值/上限算比例，不推演状态机。
 */
import { computed } from 'vue'
import { DASH, formatBytes, formatDuration, formatRatioPercent, formatRows } from '@/utils/format'

const props = defineProps<{
  label: string
  /** 当前消耗量；量纲随 unit 变化 */
  current: number
  /** 预算上限；<=0 视为未设上限（显示为「未设」而不是 0%） */
  limit: number
  unit: 'rows' | 'bytes' | 'sec' | 'ratio'
  /** 预警阈值（百分比，如 80）；预警线就画在这个位置 */
  warn_threshold_pct: number
}>()

/** 上限为 0/负数/非有限值时视为「未设上限」：此时比例无意义，不能算成 0% */
const hasLimit = computed<boolean>(() => Number.isFinite(props.limit) && props.limit > 0)

const ratio = computed<number | null>(() =>
  hasLimit.value ? props.current / props.limit : null,
)

const warnRatio = computed<number>(() => {
  const pct = Number.isFinite(props.warn_threshold_pct) ? props.warn_threshold_pct : 80
  return Math.min(Math.max(pct, 1), 100) / 100
})

type Tier = 'normal' | 'warn' | 'over' | 'unknown'

/**
 * 分级：>= 1 越限；>= 预警线 预警；其余正常。
 * 顺序不能反——恰好落在预警线上（如 0.8）算预警，落在 1.0 算越限，
 * 否则「正好到 80%」会显示成正常，用户就错过了唯一的提前量。
 */
const tier = computed<Tier>(() => {
  if (ratio.value === null) {
    return 'unknown'
  }
  if (ratio.value >= 1) {
    return 'over'
  }
  if (ratio.value >= warnRatio.value) {
    return 'warn'
  }
  return 'normal'
})

/** 填充宽度：越限时封顶 100%，否则条会长出容器 */
const fillWidth = computed<string>(() => {
  if (ratio.value === null) {
    return '0%'
  }
  return `${Math.min(Math.max(ratio.value, 0), 1) * 100}%`
})

const warnTickLeft = computed<string>(() => `${warnRatio.value * 100}%`)

function formatValue(value: number, unit: 'rows' | 'bytes' | 'sec' | 'ratio'): string {
  switch (unit) {
    case 'bytes':
      return formatBytes(value)
    case 'sec':
      return formatDuration(value)
    case 'ratio':
      return formatRatioPercent(value)
    default:
      return formatRows(value)
  }
}

const currentText = computed<string>(() => formatValue(props.current, props.unit))
const limitText = computed<string>(() => (hasLimit.value ? formatValue(props.limit, props.unit) : '未设上限'))
const ratioText = computed<string>(() => (ratio.value === null ? DASH : formatRatioPercent(ratio.value)))

const tierText = computed<string>(() => {
  switch (tier.value) {
    case 'warn':
      return '接近预算'
    case 'over':
      return '已超预算'
    case 'unknown':
      return '未设上限'
    default:
      return ''
  }
})
</script>

<template>
  <div class="budget" :class="`budget--${tier}`">
    <div class="budget__head">
      <span class="budget__label">{{ label }}</span>
      <span class="budget__values">
        <span class="budget__current elt-mono">{{ currentText }}</span>
        <span class="budget__sep">/</span>
        <span class="budget__limit elt-mono">{{ limitText }}</span>
        <span class="budget__pct elt-mono">（{{ ratioText }}）</span>
      </span>
    </div>

    <div class="budget__track" role="progressbar" :aria-valuenow="ratio === null ? 0 : Math.round(ratio * 100)">
      <div class="budget__fill" :style="{ width: fillWidth }" />
      <!-- 预警线：显式刻度，不靠颜色单独表达「快到阈值了」 -->
      <div v-if="hasLimit" class="budget__tick" :style="{ left: warnTickLeft }" :title="`预警线 ${warn_threshold_pct}%`" />
    </div>

    <div v-if="tierText !== ''" class="budget__tier">{{ tierText }}</div>
  </div>
</template>

<style scoped>
.budget {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.budget__head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 10px;
}

.budget__label {
  font-size: 12px;
  color: var(--elt-text-secondary);
}

.budget__values {
  font-size: 12px;
  color: var(--elt-text-secondary);
  white-space: nowrap;
}

.budget__current {
  color: var(--elt-text-primary);
}

.budget__sep,
.budget__pct {
  color: var(--elt-text-muted);
}

.budget__track {
  position: relative;
  height: 6px;
  overflow: hidden;
  background-color: var(--elt-bg-elevated);
  border-radius: var(--elt-radius-pill);
}

.budget__fill {
  height: 100%;
  background-color: var(--elt-accent);
  border-radius: var(--elt-radius-pill);
  transition: width 320ms ease;
}

.budget__tick {
  position: absolute;
  top: 0;
  width: 1px;
  height: 100%;
  background-color: var(--elt-text-muted);
}

.budget--warn .budget__fill {
  background-color: var(--elt-warn);
}

.budget--over .budget__fill {
  background-color: var(--elt-danger);
}

.budget__tier {
  font-size: 11px;
  font-weight: 600;
}

.budget--warn .budget__tier {
  color: var(--elt-warn);
}

.budget--over .budget__tier {
  color: var(--elt-danger);
}

.budget--unknown .budget__tier {
  color: var(--elt-text-muted);
  font-weight: 400;
}

@media (prefers-reduced-motion: reduce) {
  .budget__fill {
    transition: none;
  }
}
</style>
