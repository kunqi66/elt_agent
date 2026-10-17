<script setup lang="ts">
/**
 * 核心指标（fe-02 §6.1 的吞吐类指标）。
 *
 * 单位换算全部交给 utils/format：行数按 万/亿 分级、字节按 1024 进制、
 * 比率 numeric(6,4) 直接 ×100 保留两位小数。
 * 前端**不做**分子分母运算——成功率/拒绝率的分子分母口径在服务端，
 * 前端再算一遍必然与后端对不上（fe-01 §1）。
 *
 * 概览不画预算阈值线：预算是逐张准备单的，聚合到项目维度没有唯一阈值（fe-02 §10）。
 */
import { computed } from 'vue'
import { SectionCard } from '@/components/common'
import { formatBytes, formatRate, formatRows, formatThroughput } from '@/utils/format'
import type { MonitorMetrics } from '@/types'

const props = defineProps<{
  metrics: MonitorMetrics | null
  loading: boolean
}>()

interface MetricTile {
  key: string
  label: string
  value: string
  sub: string
}

const tiles = computed<MetricTile[]>(() => {
  const m = props.metrics
  return [
    {
      key: 'read_rows',
      label: '读取行数',
      value: formatRows(m?.read_rows),
      sub: `写入 ${formatRows(m?.written_rows)}`,
    },
    {
      key: 'written_bytes',
      label: '写入字节',
      value: formatBytes(m?.written_bytes),
      sub: '目标端累计写入量',
    },
    {
      key: 'throughput',
      label: '平均吞吐',
      value: formatThroughput(m?.avg_throughput),
      sub: '窗口内每秒平均处理行数',
    },
    {
      key: 'reject_rate',
      label: '错误分流率',
      value: formatRate(m?.reject_rate),
      sub: `拒绝 ${formatRows(m?.reject_rows)}`,
    },
  ]
})
</script>

<template>
  <SectionCard title="核心指标" :loading="loading" :skeleton-height="212">
    <div class="metrics">
      <div v-for="tile in tiles" :key="tile.key" class="metric">
        <p class="metric__label">{{ tile.label }}</p>
        <p class="metric__value elt-mono">{{ tile.value }}</p>
        <p class="metric__sub">{{ tile.sub }}</p>
      </div>
    </div>

    <p class="metrics__note">
      无运行记录时指标显示为「—」而不是 0。预算阈值线只在单次运行视图中给出。
    </p>
  </SectionCard>
</template>

<style scoped>
.metrics {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
}

.metric {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
  padding: 10px 12px;
  background-color: var(--elt-bg-elevated);
  border-radius: var(--elt-radius-control);
}

.metric__label {
  margin: 0;
  font-size: 11px;
  line-height: 16px;
  color: var(--elt-text-muted);
}

.metric__value {
  margin: 0;
  font-size: 17px;
  font-weight: 600;
  line-height: 24px;
  color: var(--elt-text-primary);
  word-break: break-all;
}

.metric__sub {
  margin: 0;
  font-size: 11px;
  line-height: 16px;
  color: var(--elt-text-secondary);
}

.metrics__note {
  margin: 10px 0 0;
  font-size: 11px;
  line-height: 16px;
  color: var(--elt-text-muted);
}
</style>
