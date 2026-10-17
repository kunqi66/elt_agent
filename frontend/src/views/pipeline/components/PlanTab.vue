<script setup lang="ts">
/**
 * EtlPlan 页签（fe-04 §6.4 之前的总览）：把后端产出的 Plan 原样摊开。
 *
 * 这里是**只读视图**，没有「编辑规则」这类入口：
 * Plan 是 LLM 与确定性校验共同产出的制品，改它只能通过重新生成或自动修复，
 * 前端就地改字段会让「制品指纹」与实际内容脱钩。
 *
 * `valid` 与 `validation_errors` 都来自后端校验器，前端不做二次判断。
 */
import { computed } from 'vue'
import { EmptyState, SectionCard } from '@/components/common'
import { enumText } from '@/utils/enums'
import type { EtlPlan, QualityRuleType } from '@/types'

const props = defineProps<{ plan: EtlPlan | null }>()

/** QualityRuleType 不在 EnumKind 里（它是 Plan 内部词汇，不是全局枚举），只能本地维护文案 */
const RULE_TYPE_TEXT: Record<QualityRuleType, string> = {
  not_null: '非空',
  regex: '正则',
  range: '范围',
  enum: '枚举',
  type_cast: '类型可转',
}

const body = computed(() => props.plan?.plan ?? null)

function paramSummary(params: Record<string, unknown>): string {
  const entries = Object.entries(params)
  if (entries.length === 0) {
    return ''
  }
  return entries.map(([k, v]) => `${k}=${typeof v === 'string' ? v : JSON.stringify(v)}`).join(' ')
}
</script>

<template>
  <div class="plan-tab">
    <EmptyState
      v-if="plan === null || body === null"
      variant="no-data"
      title="尚无 EtlPlan"
      description="发起生成并通过澄清后，工作流会产出结构化的 EtlPlan。"
    />

    <template v-else>
      <div class="plan-tab__head">
        <span class="plan-tab__schema elt-mono">{{ plan.schema_version }}</span>
        <el-tag :type="plan.valid ? 'success' : 'danger'" size="small" effect="dark">
          {{ plan.valid ? '结构校验通过' : '结构校验未通过' }}
        </el-tag>
        <span class="plan-tab__kv">输入 {{ body.inputs.length }}</span>
        <span class="plan-tab__kv">输出 {{ body.outputs.length }}</span>
        <span class="plan-tab__kv">字段映射 {{ body.field_mappings.length }}</span>
        <span class="plan-tab__kv">质量规则 {{ body.quality_rules.length }}</span>
      </div>

      <!-- 校验错误来自后端校验器；原样列出，不做「合并同类项」的加工 -->
      <el-alert v-if="plan.validation_errors.length > 0" type="error" :closable="false" show-icon>
        <div class="plan-tab__err-title">结构校验错误（{{ plan.validation_errors.length }}）</div>
        <div v-for="(e, i) in plan.validation_errors" :key="i" class="plan-tab__err">
          <span class="elt-mono">{{ e.path }}</span>
          <span class="plan-tab__err-reason">{{ e.reason }}</span>
        </div>
      </el-alert>

      <SectionCard title="输入 / 输出" collapsible>
        <div class="plan-tab__cols">
          <div class="plan-tab__col">
            <div class="plan-tab__col-title">输入</div>
            <div v-for="i in body.inputs" :key="i.id" class="plan-tab__row">
              <span class="elt-mono plan-tab__row-id">{{ i.id }}</span>
              <span class="plan-tab__row-text">{{ i.schema }}.{{ i.table }}</span>
              <span class="plan-tab__row-muted">ds#{{ i.data_source_id }}</span>
            </div>
            <div v-if="body.inputs.length === 0" class="plan-tab__row-muted">—</div>
          </div>
          <div class="plan-tab__col">
            <div class="plan-tab__col-title">输出</div>
            <div v-for="o in body.outputs" :key="o.id" class="plan-tab__row">
              <span class="elt-mono plan-tab__row-id">{{ o.id }}</span>
              <span class="plan-tab__row-text">{{ o.database }}/{{ o.table }}</span>
              <span class="plan-tab__row-muted">{{ o.mode }}</span>
            </div>
            <div v-if="body.outputs.length === 0" class="plan-tab__row-muted">—</div>
          </div>
        </div>
      </SectionCard>

      <SectionCard v-if="body.transforms.length > 0" :title="`转换（${body.transforms.length}）`" collapsible>
        <div v-for="t in body.transforms" :key="t.id" class="plan-tab__row">
          <span class="elt-mono plan-tab__row-id">{{ t.id }}</span>
          <el-tag size="small" effect="plain">{{ t.kind }}</el-tag>
          <span class="plan-tab__row-text">{{ t.input_fields.join(' + ') }} → {{ t.output_field }}</span>
          <span class="plan-tab__row-muted elt-mono">{{ paramSummary(t.params) }}</span>
        </div>
      </SectionCard>

      <SectionCard v-if="body.filters.length > 0" :title="`过滤（${body.filters.length}）`" collapsible>
        <div v-for="f in body.filters" :key="f.id" class="plan-tab__row plan-tab__row--column">
          <span class="elt-mono plan-tab__row-id">{{ f.id }}</span>
          <span class="plan-tab__row-text elt-mono">{{ f.expression }}</span>
          <span class="plan-tab__row-muted">{{ f.description }}</span>
        </div>
      </SectionCard>

      <SectionCard v-if="body.quality_rules.length > 0" :title="`质量规则（${body.quality_rules.length}）`" collapsible>
        <el-table :data="body.quality_rules" size="small" border>
          <el-table-column prop="id" label="规则" width="120" />
          <el-table-column prop="field" label="字段" min-width="140" />
          <el-table-column label="类型" width="90">
            <template #default="{ row }">{{ RULE_TYPE_TEXT[row.rule_type as QualityRuleType] ?? row.rule_type }}</template>
          </el-table-column>
          <el-table-column label="参数" min-width="160">
            <template #default="{ row }">
              <span class="elt-mono plan-tab__row-muted">{{ paramSummary(row.params) }}</span>
            </template>
          </el-table-column>
          <el-table-column label="违规处理" width="100">
            <template #default="{ row }">{{ enumText('ErrorCodeLabel', row.error_code) }} · {{ row.on_violation }}</template>
          </el-table-column>
          <el-table-column label="启用" width="70">
            <template #default="{ row }">{{ row.enabled ? '是' : '否' }}</template>
          </el-table-column>
        </el-table>
      </SectionCard>
    </template>
  </div>
</template>

<style scoped>
.plan-tab {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding-right: 4px;
}

.plan-tab__head {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
  font-size: 11px;
}

.plan-tab__schema {
  color: var(--elt-text-secondary);
}

.plan-tab__kv {
  color: var(--elt-text-muted);
}

.plan-tab__err-title {
  font-weight: 600;
}

.plan-tab__err {
  display: flex;
  gap: 8px;
  font-size: 12px;
  line-height: 18px;
}

.plan-tab__err-reason {
  color: var(--elt-text-primary);
}

.plan-tab__cols {
  display: flex;
  gap: 16px;
}

.plan-tab__col {
  flex: 1;
  min-width: 0;
}

.plan-tab__col-title {
  margin-bottom: 4px;
  font-size: 11px;
  color: var(--elt-text-muted);
}

.plan-tab__row {
  display: flex;
  gap: 8px;
  align-items: center;
  padding: 2px 0;
  font-size: 12px;
  line-height: 18px;
}

.plan-tab__row--column {
  flex-wrap: wrap;
}

.plan-tab__row-id {
  flex: none;
  min-width: 56px;
  color: var(--elt-text-muted);
}

.plan-tab__row-text {
  flex: 1;
  min-width: 0;
  color: var(--elt-text-primary);
  overflow-wrap: anywhere;
}

.plan-tab__row-muted {
  color: var(--elt-text-muted);
}
</style>
