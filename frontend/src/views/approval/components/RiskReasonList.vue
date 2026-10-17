<script setup lang="ts">
/**
 * PDP 命中原因列表（fe-05 §4 `risk_reasons`）。
 *
 * 为什么单列一个组件：风险原因是**确定性策略结论**（`engines/policy` 规则表命中项），
 * 不是模型的解释文本。这里刻意不出现任何「AI 认为 / 模型判断」字样——
 * 一旦这么说，用户就会把 P0/P1 当成可以商量的建议，而它其实是阻断依据。
 */
import { RiskLevelTag, SensitiveText } from '@/components/common'
import type { RiskReason } from '@/types'

defineProps<{ reasons: RiskReason[] }>()
</script>

<template>
  <div class="risk-reasons">
    <p v-if="reasons.length === 0" class="risk-reasons__empty">未命中任何风险规则。</p>

    <ul v-else class="risk-reasons__list">
      <li v-for="reason in reasons" :key="reason.code" class="risk-reasons__item">
        <div class="risk-reasons__head">
          <RiskLevelTag :level="reason.level" size="small" />
          <span class="risk-reasons__code elt-mono">{{ reason.code }}</span>
          <span class="risk-reasons__title">{{ reason.title }}</span>
        </div>
        <!-- detail 是后端下发的自由文本，一律经 SensitiveText 过滤后再渲染 -->
        <SensitiveText :value="reason.detail" multiline class="risk-reasons__detail" />
      </li>
    </ul>

    <p class="risk-reasons__foot">
      以上为 PDP 规则表的确定性判定结果，与模型输出无关；规则原文以安全治理页为准。
    </p>
  </div>
</template>

<style scoped>
.risk-reasons {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.risk-reasons__empty {
  margin: 0;
  font-size: 12px;
  color: var(--elt-text-muted);
}

.risk-reasons__list {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 0;
  margin: 0;
  list-style: none;
}

.risk-reasons__item {
  padding: 8px 10px;
  background-color: var(--elt-bg-elevated);
  border: 1px solid var(--elt-border-subtle);
  border-radius: var(--elt-radius-control);
}

.risk-reasons__head {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  align-items: center;
}

.risk-reasons__code {
  font-size: 11px;
  color: var(--elt-text-muted);
}

.risk-reasons__title {
  font-size: 12px;
  font-weight: 600;
  color: var(--elt-text-primary);
}

.risk-reasons__detail {
  margin-top: 4px;
  font-size: 12px;
  line-height: 1.7;
  color: var(--elt-text-secondary);
}

.risk-reasons__foot {
  margin: 0;
  font-size: 11px;
  color: var(--elt-text-muted);
}
</style>
