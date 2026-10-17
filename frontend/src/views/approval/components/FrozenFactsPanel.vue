<script setup lang="ts">
/**
 * Prepare 阶段冻结的**全部事实**（fe-05 §1.2 / §8）。
 *
 * `readonly` 在类型上恒为 `true`：这里不存在任何可编辑分支。
 * 不是「暂时不做编辑」，而是 `preparation` 在数据库层只有 `status` 一列可改，
 * 给它留一个输入框就等于给用户一个必然失败的预期。
 *
 * 安全边界：`resource_scope` / `rollback_plan` 只渲染**库表定位、工具名与步骤描述**，
 * 不渲染连接串、不渲染凭据——即使后端误下发了连接串，这里也只按字段取值渲染，
 * 并且描述类文本一律经 `SensitiveText` 过滤。
 */
import { computed } from 'vue'
import {
  ArtifactHash,
  DraftFrozenBadge,
  EnvBadge,
  RiskLevelTag,
  SensitiveText,
  StatusTag,
} from '@/components/common'
import {
  DASH,
  formatAmplification,
  formatBytes,
  formatDateTime,
  formatDuration,
  formatInt,
  formatRate,
} from '@/utils/format'
import type { PreparationDetail } from '@/types'

const props = defineProps<{
  preparation: PreparationDetail
  readonly: true
}>()

/** 预算百分比列：非有限值给 `—`，避免出现「—%」这种半截文案 */
function budgetPct(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) {
    return DASH
  }
  return `${Math.round(value)}%`
}

function scopedTable(dataSourceId: number, database: string, table: string): string {
  return `#${dataSourceId} · ${database}.${table}`
}

// 用 computed 而非一次性取值：详情轮询会替换整个 preparation 对象，
// 直接缓存子对象会让面板停留在上一拍的资源范围上。
const scope = computed(() => props.preparation.resource_scope)
const plan = computed(() => props.preparation.rollback_plan)
</script>

<template>
  <div class="facts" :class="{ 'facts--readonly': readonly }">
    <div class="facts__banner">
      <DraftFrozenBadge
        :frozen="true"
        :frozen-at="preparation.frozen_at"
        :frozen-by="preparation.creator_name"
      />
      <span class="facts__banner-hint">冻结事实，只读；任何修正只能通过新版本产生新准备单。</span>
    </div>

    <dl class="facts__grid">
      <div class="facts__cell">
        <dt>Pipeline / 版本</dt>
        <dd>
          {{ preparation.pipeline_name }}
          <span class="facts__muted">v{{ preparation.version_no }}</span>
        </dd>
      </div>

      <div class="facts__cell">
        <dt>环境</dt>
        <dd><EnvBadge :env="preparation.environment" /></dd>
      </div>

      <div class="facts__cell">
        <dt>敏感级别</dt>
        <dd><StatusTag kind="SensitiveLevel" :value="preparation.sensitive_level" /></dd>
      </div>

      <div class="facts__cell">
        <dt>风险等级</dt>
        <dd><RiskLevelTag :level="preparation.risk_level" size="small" /></dd>
      </div>

      <div class="facts__cell facts__cell--wide">
        <dt>制品指纹（artifact_sha256）</dt>
        <dd>
          <ArtifactHash
            :hash="preparation.artifact_sha256"
            display="truncate"
            copyable
            label="制品指纹"
          />
        </dd>
      </div>

      <div class="facts__cell facts__cell--wide">
        <dt>输入指纹（input_fingerprint）</dt>
        <dd>
          <ArtifactHash
            :hash="preparation.input_fingerprint"
            display="truncate"
            copyable
            label="输入指纹"
          />
        </dd>
      </div>

      <div class="facts__cell">
        <dt>冻结时间</dt>
        <dd class="elt-mono">{{ formatDateTime(preparation.frozen_at) }}</dd>
      </div>

      <div class="facts__cell">
        <dt>有效期至</dt>
        <dd class="elt-mono">
          {{ preparation.expires_at === null ? '不设有效期' : formatDateTime(preparation.expires_at) }}
        </dd>
      </div>
    </dl>

    <section class="facts__section">
      <h3 class="facts__section-title">影响范围（resource_scope）</h3>
      <p class="facts__section-hint">只含库表定位，不含任何凭据。</p>

      <div class="facts__scope">
        <div class="facts__scope-col">
          <span class="facts__scope-label">读取</span>
          <ul v-if="scope.reads.length > 0" class="facts__scope-list">
            <li v-for="(r, i) in scope.reads" :key="`r-${i}`" class="elt-mono">
              {{ scopedTable(r.data_source_id, r.database, r.table) }}
            </li>
          </ul>
          <span v-else class="facts__muted">{{ DASH }}</span>
        </div>

        <div class="facts__scope-col">
          <span class="facts__scope-label">写入</span>
          <ul v-if="scope.writes.length > 0" class="facts__scope-list">
            <li v-for="(w, i) in scope.writes" :key="`w-${i}`" class="elt-mono">
              {{ scopedTable(w.data_source_id, w.database, w.table) }}
              <span class="facts__muted">{{ w.mode === 'shadow_then_swap' ? '影子表后原子 Swap' : w.mode }}</span>
            </li>
          </ul>
          <span v-else class="facts__muted">{{ DASH }}</span>
        </div>
      </div>
    </section>

    <section class="facts__section">
      <h3 class="facts__section-title">执行预算（budget）</h3>
      <p class="facts__section-hint">超出即触发监督硬中断，不是告警阈值。</p>

      <dl class="facts__grid facts__grid--budget">
        <div class="facts__cell">
          <dt>最大读取行数</dt>
          <dd class="elt-mono">{{ formatInt(props.preparation.budget.max_read_rows) }}</dd>
        </div>
        <div class="facts__cell">
          <dt>最大写入字节</dt>
          <dd class="elt-mono">{{ formatBytes(props.preparation.budget.max_write_bytes) }}</dd>
        </div>
        <div class="facts__cell">
          <dt>最长时长</dt>
          <dd class="elt-mono">{{ formatDuration(props.preparation.budget.max_duration_sec) }}</dd>
        </div>
        <div class="facts__cell">
          <dt>最大放大比</dt>
          <dd class="elt-mono">{{ formatAmplification(props.preparation.budget.max_amplification_ratio) }}</dd>
        </div>
        <div class="facts__cell">
          <dt>最大拒绝率</dt>
          <dd class="elt-mono">{{ formatRate(props.preparation.budget.max_reject_rate) }}</dd>
        </div>
        <div class="facts__cell">
          <dt>预警阈值</dt>
          <dd class="elt-mono">{{ budgetPct(props.preparation.budget.warn_threshold_pct) }}</dd>
        </div>
      </dl>
    </section>

    <section class="facts__section">
      <h3 class="facts__section-title">回滚方案（rollback_plan）</h3>
      <p class="facts__section-hint">无回滚方案会由 PDP 判为 P1，未加固不得提交。</p>

      <ol v-if="plan.steps.length > 0" class="facts__steps">
        <li v-for="step in plan.steps" :key="step.order" class="facts__step">
          <span class="facts__step-order elt-mono">{{ step.order }}</span>
          <div class="facts__step-body">
            <span class="facts__step-action">{{ step.action }}</span>
            <span class="facts__step-tool elt-mono">{{ step.tool }}</span>
            <SensitiveText :value="step.description" multiline class="facts__step-desc" />
          </div>
        </li>
      </ol>
      <p v-else class="facts__muted">未提供回滚步骤。</p>

      <dl class="facts__grid facts__grid--budget">
        <div class="facts__cell">
          <dt>失败时清理影子表</dt>
          <dd>{{ plan.shadow_cleanup ? '是' : '否' }}</dd>
        </div>
        <div class="facts__cell facts__cell--wide">
          <dt>恢复范围</dt>
          <dd><SensitiveText :value="plan.restore_scope" /></dd>
        </div>
      </dl>
    </section>
  </div>
</template>

<style scoped>
.facts {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.facts__banner {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
}

.facts__banner-hint {
  font-size: 11px;
  color: var(--elt-text-muted);
}

.facts__grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
  gap: 10px 16px;
  margin: 0;
}

.facts__grid--budget {
  grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
}

.facts__cell {
  min-width: 0;
}

.facts__cell--wide {
  grid-column: 1 / -1;
}

.facts__cell dt {
  font-size: 11px;
  color: var(--elt-text-muted);
}

.facts__cell dd {
  margin: 2px 0 0;
  font-size: 13px;
  color: var(--elt-text-primary);
  word-break: break-word;
}

.facts__muted {
  color: var(--elt-text-muted);
}

.facts__section {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding-top: 12px;
  border-top: 1px solid var(--elt-border-subtle);
}

.facts__section-title {
  margin: 0;
  font-size: 13px;
  font-weight: 600;
  color: var(--elt-text-primary);
}

.facts__section-hint {
  margin: 0;
  font-size: 11px;
  color: var(--elt-text-muted);
}

.facts__scope {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
  gap: 12px;
}

.facts__scope-col {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.facts__scope-label {
  font-size: 11px;
  font-weight: 600;
  color: var(--elt-text-secondary);
}

.facts__scope-list {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 0;
  margin: 0;
  font-size: 12px;
  color: var(--elt-text-primary);
  list-style: none;
}

.facts__steps {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 0;
  margin: 4px 0 0;
  list-style: none;
}

.facts__step {
  display: flex;
  gap: 8px;
  align-items: flex-start;
}

.facts__step-order {
  flex: none;
  width: 18px;
  height: 18px;
  font-size: 11px;
  line-height: 18px;
  color: var(--elt-text-secondary);
  text-align: center;
  background-color: var(--elt-bg-elevated);
  border-radius: 50%;
}

.facts__step-body {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  align-items: baseline;
  min-width: 0;
}

.facts__step-action {
  font-size: 12px;
  font-weight: 600;
  color: var(--elt-text-primary);
}

.facts__step-tool {
  font-size: 11px;
  color: var(--elt-text-muted);
}

.facts__step-desc {
  flex-basis: 100%;
  font-size: 12px;
  color: var(--elt-text-secondary);
}
</style>
