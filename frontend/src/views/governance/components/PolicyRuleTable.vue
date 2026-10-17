<script setup lang="ts">
/**
 * 策略规则表（fe-07 §6）。
 *
 * 这张表是 PDP 的可读投影：**风险评级由这些规则确定性求值**，
 * 所以列表里出现的每一个条件项都是「后端真的会拿去判定的键」，
 * 界面不得在此之上再补一层自己的推断（比如「P1 大概会拦」这种话）。
 *
 * 关于 stage 筛选：`PolicyRuleFilter.stage` 是后端查询参数（gate / prepare / commit / runtime），
 * 但 `PolicyRuleRead` 当前还没有 stage 字段（types/governance.ts 已注明这是待 be-14 补齐的缺口），
 * 因此这一列在后端补上之前不会出现在表格里，筛选照常发给后端。
 */
import { computed, ref, watch } from 'vue'
import { Search } from '@element-plus/icons-vue'
import { QueryTable, RiskLevelTag, StatusTag } from '@/components/common'
import { enumOptions, enumText } from '@/utils/enums'
import { formatDateTime, textOrDash } from '@/utils/format'
import type { PolicyCondition, PolicyConditionKey, PolicyRuleFilter, PolicyRuleRead } from '@/types'

const props = withDefaults(
  defineProps<{
    items: PolicyRuleRead[]
    loading?: boolean
    initialLoading?: boolean
    error?: string | null
    total?: number
    page?: number
    pageSize?: number
    filter: PolicyRuleFilter
  }>(),
  {
    loading: false,
    initialLoading: false,
    error: null,
    total: 0,
    page: 1,
    pageSize: 50,
  },
)

const emit = defineEmits<{
  'update-filter': [patch: Partial<PolicyRuleFilter>]
  'page-change': [page: number]
  'page-size-change': [size: number]
  retry: []
}>()

/**
 * 生效阶段选项。
 * 为什么写在这里而不是从 `@/utils/enums` 取：基座的 EnumKind 里没有 PolicyStage
 * （策略规则表也还没有 stage 列），全站枚举表已冻结，页面不得改动。
 * 取值与 `PolicyRuleFilter.stage` 的注释逐字一致，后端补上该字段后应迁回枚举表。
 */
const STAGE_OPTIONS: ReadonlyArray<{ value: string; label: string }> = [
  { value: 'gate', label: '生成期门禁（gate）' },
  { value: 'prepare', label: '准备期（prepare）' },
  { value: 'commit', label: '提交期（commit）' },
  { value: 'runtime', label: '运行期监督（runtime）' },
]

/**
 * 条件键的中文名。
 * 同样是因为基座没有 PolicyConditionKey 的枚举表（见 CONVENTIONS §4 的边界：
 * enumText 只覆盖 EnumKind），而把 `writes_formal_table` 原样摆在中文界面里没人读得懂。
 * 未登记的键一律回退成原始键名——后端将来新增条件时，宁可显示英文键，也不能显示错的中文。
 */
const CONDITION_LABELS: Record<PolicyConditionKey, string> = {
  env: '环境',
  writes_formal_table: '写正式表',
  sensitive_plaintext_egress: '敏感字段明文出域',
  bypass_approval_intent: '绕过审批意图',
  cross_tenant_access: '跨租户访问',
  resource_out_of_scope: '资源超出范围',
  budget_over_default_factor: '预算超默认倍数',
  missing_rollback_plan: '缺少回滚方案',
  unregistered_secret_ref: '未登记密钥引用',
  read_only: '只读操作',
}

const riskLevelOptions = enumOptions('RiskLevel')

/**
 * 关键字单独存本地值。
 * 直接把输入框绑到 filter.q 会在每次按键都触发一次列表请求，
 * 而规则编码是给人「想起来一个片段」用的——按回车（或清空）再查就够了。
 */
const keyword = ref<string>(props.filter.q ?? '')

watch(
  () => props.filter.q,
  (next) => {
    keyword.value = next ?? ''
  },
)

interface ConditionItem {
  key: string
  label: string
  text: string
}

/**
 * 条件 → 可读条目。
 * 只渲染**实际出现的键**：PDP 语义里「未选择」与「显式 false」不同，
 * 所以这里不替后端补默认值，也不把缺失的键显示成「否」。
 */
function conditionItems(condition: PolicyCondition): ConditionItem[] {
  const items: ConditionItem[] = []
  for (const [key, raw] of Object.entries(condition) as Array<[PolicyConditionKey, unknown]>) {
    if (raw === null || raw === undefined) {
      continue
    }
    const label = CONDITION_LABELS[key] ?? key
    let text: string
    if (Array.isArray(raw)) {
      text = (raw as unknown[]).map((item) => enumText('Env', String(item))).join(' / ')
    } else if (typeof raw === 'boolean') {
      text = raw ? '是' : '否'
    } else {
      text = String(raw)
    }
    items.push({ key, label, text })
  }
  return items
}

const enabledFilter = computed<string>(() => {
  const value = props.filter.enabled
  if (value === true) {
    return 'true'
  }
  if (value === false) {
    return 'false'
  }
  return 'all'
})

/** 有任一筛选条件生效；用于把空态文案从「还没有数据」切成「没有符合条件的记录」 */
const filtered = computed<boolean>(
  () =>
    props.filter.stage !== undefined ||
    (props.filter.risk_level?.length ?? 0) > 0 ||
    props.filter.enabled !== undefined ||
    (props.filter.q ?? '') !== '',
)

function onStageChange(value: string | number | boolean | null): void {
  const stage = typeof value === 'string' && value !== '' ? value : undefined
  emit('update-filter', { stage })
}

function onRiskLevelChange(value: unknown): void {
  const list = Array.isArray(value) ? (value as string[]) : []
  emit('update-filter', { risk_level: list.length > 0 ? (list as PolicyRuleFilter['risk_level']) : undefined })
}

function onEnabledChange(value: string | number | boolean | null): void {
  if (value === 'true') {
    emit('update-filter', { enabled: true })
    return
  }
  if (value === 'false') {
    emit('update-filter', { enabled: false })
    return
  }
  emit('update-filter', { enabled: undefined })
}

function onKeywordEnter(): void {
  const q = keyword.value.trim()
  emit('update-filter', { q: q === '' ? undefined : q })
}
</script>

<template>
  <div class="rule-table">
    <div class="rule-table__filters">
      <el-select
        :model-value="filter.stage ?? ''"
        class="rule-table__stage"
        placeholder="生效阶段"
        clearable
        size="small"
        @change="onStageChange"
      >
        <el-option
          v-for="option in STAGE_OPTIONS"
          :key="option.value"
          :label="option.label"
          :value="option.value"
        />
      </el-select>

      <el-select
        :model-value="filter.risk_level ?? []"
        class="rule-table__level"
        placeholder="风险等级"
        multiple
        collapse-tags
        clearable
        size="small"
        @change="onRiskLevelChange"
      >
        <el-option
          v-for="option in riskLevelOptions"
          :key="option.value"
          :label="option.label"
          :value="option.value"
        />
      </el-select>

      <el-select
        :model-value="enabledFilter"
        class="rule-table__enabled"
        size="small"
        @change="onEnabledChange"
      >
        <el-option label="全部状态" value="all" />
        <el-option label="仅启用" value="true" />
        <el-option label="仅停用" value="false" />
      </el-select>

      <el-input
        v-model="keyword"
        class="rule-table__q"
        size="small"
        clearable
        placeholder="规则编码 / 名称"
        :prefix-icon="Search"
        @keyup.enter="onKeywordEnter"
        @clear="onKeywordEnter"
      />
    </div>

    <QueryTable
      :items="items"
      :loading="loading"
      :initial-loading="initialLoading"
      :error="error"
      :total="total"
      :page="page"
      :page-size="pageSize"
      :filtered="filtered"
      empty-text="还没有策略规则"
      empty-description="规则由后端策略版本发布流程维护；当前项目尚无任何规则记录。"
      :min-height="260"
      row-key="code"
      @retry="emit('retry')"
      @page-change="(p: number) => emit('page-change', p)"
      @page-size-change="(s: number) => emit('page-size-change', s)"
    >
      <el-table-column label="等级" width="76">
        <template #default="{ row }">
          <RiskLevelTag :level="row.level" size="small" />
        </template>
      </el-table-column>

      <el-table-column label="规则" min-width="220">
        <template #default="{ row }">
          <div class="rule-table__code elt-mono">{{ row.code }}</div>
          <div class="rule-table__title">{{ row.title }}</div>
          <div v-if="row.description !== ''" class="rule-table__desc">{{ row.description }}</div>
        </template>
      </el-table-column>

      <el-table-column label="判定条件" min-width="260">
        <template #default="{ row }">
          <div v-if="conditionItems(row.condition).length === 0" class="rule-table__none">
            无附加条件
          </div>
          <div v-else class="rule-table__conditions">
            <span
              v-for="item in conditionItems(row.condition)"
              :key="item.key"
              class="rule-table__condition"
            >
              <span class="rule-table__condition-label">{{ item.label }}</span>
              <span class="rule-table__condition-value">{{ item.text }}</span>
            </span>
          </div>
        </template>
      </el-table-column>

      <el-table-column label="审批槽" width="150">
        <template #default="{ row }">
          <span v-if="row.required_slots.length === 0" class="rule-table__none">未指定</span>
          <template v-else>
            <StatusTag
              v-for="slot in row.required_slots"
              :key="slot"
              kind="ApprovalSlot"
              :value="slot"
              size="small"
            />
          </template>
        </template>
      </el-table-column>

      <el-table-column label="状态" width="80">
        <template #default="{ row }">
          <el-tag :type="row.enabled ? 'success' : 'info'" size="small" effect="plain">
            {{ row.enabled ? '启用' : '停用' }}
          </el-tag>
        </template>
      </el-table-column>

      <el-table-column label="版本 / 更新" width="180">
        <template #default="{ row }">
          <div class="rule-table__meta">v{{ row.version }}</div>
          <div class="rule-table__meta">
            {{ textOrDash(row.updated_by_name) }} · {{ formatDateTime(row.updated_at) }}
          </div>
        </template>
      </el-table-column>
    </QueryTable>

    <p class="rule-table__foot">
      停用的规则不删除：历史版本仍需可解释。未出现的条件项表示「未约束」，与显式设为否不是一回事。
    </p>
  </div>
</template>

<style scoped>
.rule-table {
  display: flex;
  flex-direction: column;
}

.rule-table__filters {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  padding: 12px 16px 0;
}

.rule-table__stage {
  width: 190px;
}

.rule-table__level {
  width: 190px;
}

.rule-table__enabled {
  width: 130px;
}

.rule-table__q {
  width: 220px;
}

.rule-table__code {
  font-size: 12px;
  color: var(--elt-text-secondary);
}

.rule-table__title {
  font-size: 13px;
  color: var(--elt-text-primary);
}

.rule-table__desc {
  margin-top: 2px;
  font-size: 12px;
  line-height: 1.6;
  color: var(--elt-text-muted);
}

.rule-table__conditions {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}

.rule-table__condition {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 1px 6px;
  font-size: 11px;
  background-color: var(--elt-bg-elevated);
  border: 1px solid var(--elt-border-subtle);
  border-radius: var(--elt-radius-pill);
}

.rule-table__condition-label {
  color: var(--elt-text-secondary);
}

.rule-table__condition-value {
  color: var(--elt-text-primary);
}

.rule-table__none {
  font-size: 12px;
  color: var(--elt-text-muted);
}

.rule-table__meta {
  font-size: 11px;
  color: var(--elt-text-muted);
}

.rule-table__foot {
  padding: 10px 16px 14px;
  margin: 0;
  font-size: 11px;
  line-height: 1.7;
  color: var(--elt-text-muted);
}
</style>
