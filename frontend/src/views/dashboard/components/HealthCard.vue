<script setup lang="ts">
/**
 * 运行健康度（fe-02 §6.1 的运行类指标）。
 *
 * 两条容易写错的口径，这里刻意钉住：
 * 1. **成功/失败率用 formatRateOfTotal**：窗口内一次运行都没有时分母为 0，
 *    此时必须显示 '—' 而不是 '0.00%'——后者会被读成「全部失败」。
 * 2. **异常摘要走 SensitiveText**：summary 由后端脱敏后下发，
 *    前端原样渲染、不拼任何原始值（fe-02 §8）。
 */
import { computed } from 'vue'
import { SectionCard, SensitiveText } from '@/components/common'
import { formatDateTime, formatInt, formatRateOfTotal } from '@/utils/format'
import type { AnomalyBrief, MonitorHealth } from '@/types'

const props = defineProps<{
  health: MonitorHealth | null
  loading: boolean
}>()

interface AnomalyRow extends AnomalyBrief {
  /** critical = 硬中断/失败；warning = 超预警阈值。只决定配色，不改文案 */
  tone: 'danger' | 'warning'
  toneText: string
}

const anomalies = computed<AnomalyRow[]>(() => {
  const list = props.health?.recent_anomalies ?? []
  // 只展示前 5 条：这是概览卡，事件明细归 fe-06 的运行详情
  return list.slice(0, 5).map((item) => ({
    ...item,
    tone: item.severity === 'critical' ? 'danger' : 'warning',
    toneText: item.severity === 'critical' ? '严重' : '预警',
  }))
})

const successRate = computed<string>(() =>
  formatRateOfTotal(props.health?.success_rate, props.health?.run_total),
)
const failureRate = computed<string>(() =>
  formatRateOfTotal(props.health?.failure_rate, props.health?.run_total),
)

const runRunning = computed<number>(() => props.health?.run_running ?? 0)
/** 有运行记录才给成功/失败上色：'—' 涂成绿色会被读成「运行很健康」 */
const hasRuns = computed<boolean>(() => (props.health?.run_total ?? 0) > 0)
const hasFailures = computed<boolean>(() => (props.health?.run_failed ?? 0) > 0)

const counts = computed<Array<{ key: string; label: string; value: string }>>(() => {
  const h = props.health
  return [
    { key: 'total', label: '运行总数', value: formatInt(h?.run_total) },
    { key: 'succeeded', label: '成功', value: formatInt(h?.run_succeeded) },
    { key: 'failed', label: '失败', value: formatInt(h?.run_failed) },
    { key: 'interrupted', label: '监督硬中断', value: formatInt(h?.run_interrupted) },
  ]
})
</script>

<template>
  <SectionCard title="运行健康度" :loading="loading" :skeleton-height="212">
    <template #extra>
      <span v-if="health !== null" class="health__window elt-mono" title="统计窗口">
        {{ health.window }}
      </span>
    </template>

    <div class="health">
      <div class="health__rates">
        <div class="rate">
          <p class="rate__value elt-mono" :class="{ 'rate__value--ok': hasRuns }">
            {{ successRate }}
          </p>
          <p class="rate__label">运行成功率</p>
          <p class="rate__sub elt-mono">
            成功 {{ formatInt(health?.run_succeeded) }} / 共 {{ formatInt(health?.run_total) }}
          </p>
        </div>

        <div class="rate">
          <p
            class="rate__value elt-mono"
            :class="hasFailures ? 'rate__value--bad' : 'rate__value--muted'"
          >
            {{ failureRate }}
          </p>
          <p class="rate__label">运行失败率</p>
          <p class="rate__sub elt-mono">失败 {{ formatInt(health?.run_failed) }}</p>
        </div>

        <div class="rate">
          <p class="rate__value elt-mono" :class="runRunning > 0 ? 'rate__value--live' : 'rate__value--muted'">
            <span v-if="runRunning > 0" class="rate__pulse" aria-hidden="true" />
            {{ formatInt(health?.run_running) }}
          </p>
          <p class="rate__label">运行中</p>
          <p class="rate__sub">活跃实例{{ runRunning > 0 ? '（2 秒刷新）' : '（30 秒刷新）' }}</p>
        </div>
      </div>

      <dl class="health__counts">
        <div v-for="count in counts" :key="count.key" class="count">
          <dt class="count__label">{{ count.label }}</dt>
          <dd class="count__value elt-mono">{{ count.value }}</dd>
        </div>
      </dl>

      <div class="health__anomalies">
        <p class="health__subtitle">近期异常</p>

        <!-- 无异常是正面事实，用绿色明说，不留空白让用户猜 -->
        <p v-if="anomalies.length === 0" class="anomalies__clear">
          当前时间窗内没有硬中断或超阈值预警。
        </p>

        <ul v-else class="anomalies">
          <li
            v-for="item in anomalies"
            :key="`${item.run_id}-${item.occurred_at}`"
            class="anomalies__item"
            :class="`anomalies__item--${item.tone}`"
          >
            <span class="anomalies__chip" :class="`anomalies__chip--${item.tone}`">{{ item.toneText }}</span>
            <div class="anomalies__body">
              <p class="anomalies__head">
                <span class="anomalies__pipeline">{{ item.pipeline_name }}</span>
                <span class="anomalies__run elt-mono">run#{{ item.run_id }}</span>
                <span class="anomalies__time" :title="formatDateTime(item.occurred_at)">
                  {{ formatDateTime(item.occurred_at) }}
                </span>
              </p>
              <SensitiveText class="anomalies__summary" :value="item.summary" multiline />
            </div>
          </li>
        </ul>
      </div>
    </div>
  </SectionCard>
</template>

<style scoped>
.health__window {
  font-size: 11px;
  color: var(--elt-text-muted);
}

.health {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.health__rates {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 10px;
}

.rate {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}

.rate__value {
  display: flex;
  align-items: center;
  gap: 5px;
  margin: 0;
  font-size: 22px;
  font-weight: 600;
  line-height: 28px;
  color: var(--elt-text-secondary);
}

.rate__value--ok {
  color: var(--elt-success);
}

.rate__value--bad {
  color: var(--elt-danger);
}

.rate__value--live {
  color: var(--elt-accent);
}

.rate__value--muted {
  color: var(--elt-text-muted);
}

.rate__pulse {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background-color: currentcolor;
  animation: health-pulse 1.4s ease-in-out infinite;
}

@keyframes health-pulse {
  0%,
  100% {
    opacity: 1;
    transform: scale(1);
  }

  50% {
    opacity: 0.35;
    transform: scale(0.75);
  }
}

@media (prefers-reduced-motion: reduce) {
  .rate__pulse {
    animation: none;
  }
}

.rate__label {
  margin: 0;
  font-size: 12px;
  line-height: 18px;
  color: var(--elt-text-secondary);
}

.rate__sub {
  margin: 0;
  font-size: 11px;
  line-height: 16px;
  color: var(--elt-text-muted);
}

.health__counts {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 8px;
  margin: 0;
  padding: 10px 12px;
  background-color: var(--elt-bg-elevated);
  border-radius: var(--elt-radius-control);
}

.count {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}

.count__label {
  margin: 0;
  font-size: 11px;
  line-height: 16px;
  color: var(--elt-text-muted);
}

.count__value {
  margin: 0;
  font-size: 13px;
  font-weight: 600;
  line-height: 20px;
  color: var(--elt-text-primary);
}

.health__subtitle {
  margin: 0 0 6px;
  font-size: 12px;
  font-weight: 600;
  color: var(--elt-text-secondary);
}

.anomalies__clear {
  margin: 0;
  padding: 8px 10px;
  font-size: 12px;
  line-height: 18px;
  color: var(--elt-success);
  background-color: var(--elt-badge-success);
  border-radius: var(--elt-radius-control);
}

.anomalies {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin: 0;
  padding: 0;
  list-style: none;
}

.anomalies__item {
  display: flex;
  gap: 8px;
  padding: 8px 10px;
  background-color: var(--elt-bg-elevated);
  border-left: 2px solid var(--elt-border-subtle);
  border-radius: var(--elt-radius-control);
}

.anomalies__item--danger {
  border-left-color: var(--elt-danger);
}

.anomalies__item--warning {
  border-left-color: var(--elt-warn);
}

.anomalies__chip {
  flex: none;
  height: 16px;
  padding: 0 6px;
  font-size: 11px;
  line-height: 16px;
  border-radius: var(--elt-radius-pill);
}

.anomalies__chip--danger {
  color: var(--elt-danger);
  background-color: var(--elt-badge-danger);
}

.anomalies__chip--warning {
  color: var(--elt-warn);
  background-color: var(--elt-badge-warn);
}

.anomalies__body {
  min-width: 0;
}

.anomalies__head {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin: 0;
  font-size: 12px;
  line-height: 16px;
}

.anomalies__pipeline {
  font-weight: 600;
  color: var(--elt-text-primary);
}

.anomalies__run,
.anomalies__time {
  color: var(--elt-text-muted);
}

.anomalies__time {
  margin-left: auto;
}

.anomalies__summary {
  margin: 2px 0 0;
  font-size: 12px;
  line-height: 17px;
  color: var(--elt-text-secondary);
}
</style>
