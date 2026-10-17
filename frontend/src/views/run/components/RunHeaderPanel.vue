<script setup lang="ts">
/**
 * 运行详情顶部摘要（fe-06 §6 三区布局第一区）。
 *
 * 只放「判断这是一次什么运行」需要的字段：状态徽章、SeaTunnel job id、
 * 版本摘要（v{n} + 制品哈希）、时间、目标表、触发人。
 * 指标与预算放到下一区——顶部塞满数字会让人找不到状态。
 *
 * 硬中断的措辞是刻意的：`interrupted` 是平台的**主动保护动作**（超预算/超时阈值），
 * 不是故障。混同成「中断了」会让人以为系统不稳，从而去查不存在的问题。
 */
import { computed } from 'vue'
import { ArtifactHash, SectionCard, SensitiveText } from '@/components/common'
import { formatDateTime, formatDuration, formatUtc, DASH } from '@/utils/format'
import type { RunDetail } from '@/types'

const props = defineProps<{ run: RunDetail }>()

const isInterrupted = computed<boolean>(() => props.run.status === 'interrupted')

const startedText = computed<string>(() => formatDateTime(props.run.started_at))
const finishedText = computed<string>(() =>
  props.run.finished_at === null ? '未结束' : formatDateTime(props.run.finished_at),
)

interface Fact {
  key: string
  label: string
  text: string
  mono?: boolean
}

const facts = computed<Fact[]>(() => [
  { key: 'job', label: 'SeaTunnel Job', text: props.run.seatunnel_job_id ?? DASH, mono: true },
  { key: 'version', label: '版本', text: `v${props.run.version_no}`, mono: true },
  { key: 'prep', label: '准备单', text: `#${props.run.preparation_id}`, mono: true },
  { key: 'formal', label: '目标表', text: props.run.formal_table ?? DASH, mono: true },
  { key: 'shadow', label: '影子表', text: props.run.shadow_table ?? DASH, mono: true },
  { key: 'error', label: '错误表', text: props.run.error_table ?? DASH, mono: true },
  { key: 'operator', label: '触发人', text: props.run.triggered_by_name },
  { key: 'duration', label: '耗时', text: formatDuration(props.run.duration_sec), mono: true },
])

/** 悬停给出 UTC，跨时区对账时不会被本地时区误导 */
const startedTitle = computed<string>(() => `本地时区展示；UTC：${formatUtc(props.run.started_at)}`)
</script>

<template>
  <SectionCard :padded="true">
    <template #extra>
      <ArtifactHash :hash="run.artifact_sha256" display="truncate" copyable label="制品哈希" />
    </template>

    <div class="head">
      <div class="head__identity">
        <span class="head__pipeline">
          <SensitiveText :value="run.pipeline_name" />
        </span>
        <span class="head__version elt-mono">v{{ run.version_no }}</span>
        <span class="head__run-id elt-mono">run #{{ run.id }}</span>
      </div>

      <div class="head__times">
        <span class="head__time" :title="startedTitle">开始 {{ startedText }}</span>
        <span class="head__time">结束 {{ finishedText }}</span>
      </div>
    </div>

    <!-- 硬中断说明：先讲清「这是保护动作」，用户才不会去查不存在的问题 -->
    <el-alert
      v-if="isInterrupted"
      class="head__alert"
      type="error"
      :closable="false"
      show-icon
      title="本运行被平台监督内核硬中断"
      description="触发了超预算或超时阈值，属于平台的主动保护动作，不是故障。影子表与错误表已保留：可先看「根因诊断」与「失败快照」，再执行受管回滚。"
    />

    <el-alert
      v-if="run.error_message !== null && run.error_message !== ''"
      class="head__alert"
      type="error"
      :closable="false"
      show-icon
      title="失败信息"
    >
      <SensitiveText :value="run.error_message" multiline />
    </el-alert>

    <div class="head__facts">
      <div v-for="fact in facts" :key="fact.key" class="head__fact">
        <span class="head__fact-label">{{ fact.label }}</span>
        <SensitiveText class="head__fact-value" :value="fact.text" :mono="fact.mono === true" />
      </div>
    </div>
  </SectionCard>
</template>

<style scoped>
.head {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  align-items: center;
  justify-content: space-between;
}

.head__identity {
  display: flex;
  gap: 8px;
  align-items: baseline;
  min-width: 0;
}

.head__pipeline {
  font-size: 15px;
  font-weight: 600;
  color: var(--elt-text-primary);
}

.head__version,
.head__run-id {
  font-size: 12px;
  color: var(--elt-text-secondary);
}

.head__times {
  display: flex;
  gap: 14px;
  font-size: 12px;
  color: var(--elt-text-secondary);
}

.head__time {
  cursor: help;
}

.head__alert {
  margin-top: 12px;
}

.head__facts {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(190px, 1fr));
  gap: 10px 18px;
  margin-top: 14px;
}

.head__fact {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}

.head__fact-label {
  font-size: 11px;
  color: var(--elt-text-muted);
}

.head__fact-value {
  font-size: 12px;
  color: var(--elt-text-primary);
  overflow-wrap: anywhere;
}
</style>
