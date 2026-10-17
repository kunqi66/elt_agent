<script setup lang="ts">
/**
 * 有限自动修复面板（fe-04 §6.5；规则见 be-11）。
 *
 * 轮次上限是**后端**的 `LLM_AUTOFIX_MAX_ROUNDS`，前端只显示 `当前/上限`：
 * 前端自己算「还能修几次」会随着后端调参而说谎，所以上限由调用方传入。
 *
 * diff 逐行渲染而不是整段丢进一个 `<pre>`：
 * 整段过 SensitiveText 时只要有一行命中敏感特征（比如带 url 的那行），
 * 整份 diff 都会被替换成脱敏提示，修复内容就完全看不见了；
 * 逐行处理只会隐去真正有问题的那一行。
 */
import { computed, ref } from 'vue'
import { MagicStick } from '@element-plus/icons-vue'
import { AsyncTaskPanel, ConfirmDialog, EmptyState, SensitiveText } from '@/components/common'
import { formatDateTime } from '@/utils/format'
import type { AutofixRound } from '@/types'

const props = withDefaults(
  defineProps<{
    rounds: AutofixRound[]
    currentRound: number
    maxRounds: number
    exhausted: boolean
    /** 当前有没有可修复的制品（没有 version id 时不能修） */
    canRun: boolean
    loading: boolean
    error: string | null
  }>(),
  { error: null },
)

const emit = defineEmits<{
  run: [note: string]
  'focus-finding': [findingId: string]
}>()

/** 记录被折叠的轮次而不是被展开的：默认全展开（修复内容就是来这一栏的目的），
 *  新增轮次也会自动是展开态，不用再去维护「新轮次要记得展开」 */
const collapsed = ref<number[]>([])

function isOpen(round: number): boolean {
  return !collapsed.value.includes(round)
}

function toggle(round: number): void {
  collapsed.value = collapsed.value.includes(round)
    ? collapsed.value.filter((r) => r !== round)
    : [...collapsed.value, round]
}

interface DiffLine {
  text: string
  kind: 'add' | 'del' | 'hunk' | 'meta' | 'ctx'
}

function parseDiff(diff: string): DiffLine[] {
  return diff.split('\n').map((text) => {
    if (text.startsWith('+++') || text.startsWith('---')) {
      return { text, kind: 'meta' as const }
    }
    if (text.startsWith('@@')) {
      return { text, kind: 'hunk' as const }
    }
    if (text.startsWith('+')) {
      return { text, kind: 'add' as const }
    }
    if (text.startsWith('-')) {
      return { text, kind: 'del' as const }
    }
    return { text, kind: 'ctx' as const }
  })
}

function linesOf(round: AutofixRound): DiffLine[] {
  return parseDiff(round.hocon_diff)
}

function planLines(round: AutofixRound): DiffLine[] {
  return round.plan_diff === null ? [] : parseDiff(round.plan_diff)
}

const confirmVisible = ref<boolean>(false)
const canRequest = computed<boolean>(() => props.canRun && !props.loading && !props.exhausted)
</script>

<template>
  <div class="af">
    <div class="af__head">
      <span class="af__title">自动修复</span>
      <span class="af__round elt-mono">{{ currentRound }} / {{ maxRounds }} 轮</span>
      <span class="af__spacer" />
      <el-button
        type="primary"
        size="small"
        :disabled="!canRequest"
        :loading="loading"
        @click="confirmVisible = true"
      >
        <el-icon><MagicStick /></el-icon>
        <span>{{ currentRound === 0 ? '请求修复' : '再修一轮' }}</span>
      </el-button>
    </div>

    <el-alert v-if="exhausted" type="warning" :closable="false" show-icon>
      自动修复已达上限（{{ maxRounds }} 轮），修复按钮已停用。请调整需求后重新生成，或人工核对门禁结论。
    </el-alert>

    <EmptyState
      v-if="rounds.length === 0"
      variant="no-data"
      title="尚未进行自动修复"
      description="门禁发现可修复的问题时，可请求 LLM 做有限轮次的修复；每轮都会留下 diff 供核对。"
    />

    <div v-for="r in rounds" :key="r.round" class="af__round-card">
      <button type="button" class="af__round-head" @click="toggle(r.round)">
        <span class="af__caret">{{ isOpen(r.round) ? '▾' : '▸' }}</span>
        <span class="af__round-no">第 {{ r.round }} 轮</span>
        <span class="af__round-time">{{ formatDateTime(r.created_at) }}</span>
        <span class="af__spacer" />
        <span class="af__round-stat">修复 {{ r.resolved_finding_ids.length }} 项</span>
      </button>

      <div v-if="isOpen(r.round)" class="af__round-body">
        <div class="af__ids">
          <span class="af__ids-label">触发</span>
          <el-tag
            v-for="id in r.trigger_finding_ids"
            :key="id"
            size="small"
            effect="plain"
            class="af__id"
            @click="emit('focus-finding', id)"
          >
            {{ id }}
          </el-tag>
          <span v-if="r.trigger_finding_ids.length === 0" class="af__muted">—</span>
        </div>
        <div class="af__ids">
          <span class="af__ids-label">已修复</span>
          <el-tag
            v-for="id in r.resolved_finding_ids"
            :key="id"
            size="small"
            type="success"
            effect="plain"
            class="af__id"
            @click="emit('focus-finding', id)"
          >
            {{ id }}
          </el-tag>
          <span v-if="r.resolved_finding_ids.length === 0" class="af__muted">—</span>
        </div>

        <div class="af__diff-title">HOCON 变更</div>
        <div class="af__diff">
          <div v-for="(l, i) in linesOf(r)" :key="i" class="af__line" :class="`is-${l.kind}`">
            <SensitiveText :value="l.text" mono />
          </div>
        </div>

        <template v-if="r.plan_diff !== null">
          <div class="af__diff-title">Plan 变更</div>
          <div class="af__diff">
            <div v-for="(l, i) in planLines(r)" :key="i" class="af__line" :class="`is-${l.kind}`">
              <SensitiveText :value="l.text" mono />
            </div>
          </div>
        </template>
      </div>
    </div>

    <AsyncTaskPanel :loading="loading" :error="error" loading-text="修复中（会重新生成 HOCON 与 Plan）…" />

    <!-- 修复会消耗 LLM 配额并改写制品，理由要留痕 -->
    <ConfirmDialog
      v-model="confirmVisible"
      title="请求自动修复"
      description="将让模型基于门禁结论修复 HOCON/Plan，并重新跑门禁。请说明本次修复目标。"
      require-reason
      :min-reason-length="5"
      :loading="loading"
      @confirm="(reason: string) => emit('run', reason)"
    />
  </div>
</template>

<style scoped>
.af {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.af__head {
  display: flex;
  gap: 8px;
  align-items: center;
}

.af__title {
  font-size: 13px;
  font-weight: 600;
  color: var(--elt-text-primary);
}

.af__round {
  font-size: 11px;
  color: var(--elt-text-muted);
}

.af__spacer {
  flex: 1;
}

.af__round-card {
  border: 1px solid var(--elt-border-subtle);
  border-radius: var(--elt-radius-control);
}

.af__round-head {
  display: flex;
  gap: 8px;
  align-items: center;
  width: 100%;
  padding: 5px 8px;
  font-size: 12px;
  color: var(--elt-text-secondary);
  text-align: left;
  cursor: pointer;
  background: none;
  border: none;
}

.af__caret {
  color: var(--elt-text-muted);
}

.af__round-no {
  color: var(--elt-text-primary);
}

.af__round-time,
.af__round-stat {
  font-size: 11px;
  color: var(--elt-text-muted);
}

.af__round-body {
  padding: 0 8px 8px;
}

.af__ids {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  align-items: center;
  margin-bottom: 4px;
  font-size: 11px;
}

.af__ids-label {
  color: var(--elt-text-muted);
}

.af__id {
  cursor: pointer;
}

.af__muted {
  color: var(--elt-text-muted);
}

.af__diff-title {
  margin: 6px 0 2px;
  font-size: 11px;
  color: var(--elt-text-secondary);
}

.af__diff {
  max-height: 200px;
  overflow: auto;
  font-family: var(--elt-mono);
  font-size: 11px;
  line-height: 16px;
  background-color: var(--elt-bg-code);
  border-radius: var(--elt-radius-control);
}

.af__line {
  padding: 0 6px;
  white-space: pre;
}

.af__line.is-add {
  color: var(--elt-success);
  background-color: rgba(103, 194, 58, 0.1);
}

.af__line.is-del {
  color: var(--elt-danger);
  background-color: rgba(245, 108, 108, 0.1);
}

.af__line.is-hunk {
  color: var(--elt-accent);
}

.af__line.is-meta {
  color: var(--elt-text-muted);
}

.af__line.is-ctx {
  color: var(--elt-text-secondary);
}
</style>
