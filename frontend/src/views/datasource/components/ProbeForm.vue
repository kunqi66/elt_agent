<script setup lang="ts">
/**
 * 元数据探查配置（fe-03 §5.4）。
 *
 * 探查是**只读**动作（P3 工具，免 Capability）：不修改源端数据、
 * 不读取超出采样范围的数据，采样值在后端入库前完成脱敏。
 * 表单只收集边界（范围 / 行数 / 掩码策略），不参与任何脱敏计算——
 * 脱敏判定留在后端，前端算一次就等于多一份可能不一致的实现。
 */
import { computed } from 'vue'
import { Delete, Plus } from '@element-plus/icons-vue'
import { formatInt } from '@/utils/format'
import type { ProbeRequest, SamplingScope } from '@/types'

/** 后端上限（fe-03 §10）：前端先夹一道，避免提交必然被拒的请求 */
const ROW_LIMIT = 10_000

const props = withDefaults(
  defineProps<{
    modelValue: ProbeRequest
    submitting?: boolean
    /** 「仅登记不真连」的类型不允许探查 */
    connectable?: boolean
    /** 可选表名（来自当前 Schema 快照）；为空时允许自由输入 */
    tableOptions?: string[]
  }>(),
  { submitting: false, connectable: true, tableOptions: () => [] },
)

const emit = defineEmits<{
  'update:modelValue': [value: ProbeRequest]
  submit: []
  cancel: []
}>()

/**
 * SamplingScope 与掩码方式都未登记进 `@/utils/enums`（不在 EnumKind 里），
 * 故文案暂由本页持有——基座补登记后应改走 enumText，避免两份口径。
 */
const SCOPE_OPTIONS: ReadonlyArray<{ value: SamplingScope; label: string; hint: string }> = [
  { value: 'schema_only', label: '仅结构', hint: '只读表结构，不采样任何业务数据' },
  { value: 'selected_tables', label: '指定表', hint: '只采样勾选的表，读取范围最小' },
  { value: 'full_tables', label: '全表', hint: '采样库内全部表，受下方行数上限约束' },
]

const MASK_METHODS: ReadonlyArray<{ value: string; label: string; hint: string }> = [
  { value: 'mask_middle', label: '中间掩码', hint: '保留首尾字符，如 138****5678' },
  { value: 'hash', label: '哈希', hint: '不可逆，但保留跨表关联性' },
  { value: 'truncate', label: '截断', hint: '只保留前若干字符' },
]

const sampling = computed(() => props.modelValue.sampling)
const scope = computed<SamplingScope>(() => sampling.value.scope)
const tables = computed<string[]>(() => sampling.value.tables ?? [])

const scopeHint = computed<string>(
  () => SCOPE_OPTIONS.find((o) => o.value === scope.value)?.hint ?? '',
)

const maskDefault = computed<string>(() => {
  const raw = props.modelValue.masking_policy['default']
  return typeof raw === 'string' && raw !== '' ? raw : 'mask_middle'
})

const maskHint = computed<string>(
  () => MASK_METHODS.find((m) => m.value === maskDefault.value)?.hint ?? '',
)

/** 字段级覆盖：后端结构是 { default, overrides: { 字段名: 方式 } } */
const overrides = computed<Array<{ column: string; method: string }>>(() => {
  const raw = props.modelValue.masking_policy['overrides']
  if (raw === null || typeof raw !== 'object' || Array.isArray(raw)) {
    return []
  }
  return Object.entries(raw as Record<string, unknown>).map(([column, method]) => ({
    column,
    method: typeof method === 'string' && method !== '' ? method : 'mask_middle',
  }))
})

interface Patch {
  sampling?: Partial<ProbeRequest['sampling']>
  maskDefault?: string
  overrides?: Array<{ column: string; method: string }>
}

function emitPatch(patch: Patch): void {
  const overrideRecord: Record<string, string> = {}
  for (const row of patch.overrides ?? overrides.value) {
    const column = row.column.trim()
    if (column !== '') {
      overrideRecord[column] = row.method
    }
  }
  emit('update:modelValue', {
    sampling: { ...sampling.value, ...(patch.sampling ?? {}) },
    masking_policy: {
      ...props.modelValue.masking_policy,
      default: patch.maskDefault ?? maskDefault.value,
      overrides: overrideRecord,
    },
  })
}

function setRows(value: number | undefined): void {
  // 清空输入框时忽略这次事件（el-input-number 会给 undefined），保留上一次有效值
  if (value === undefined || !Number.isFinite(value)) {
    return
  }
  emitPatch({ sampling: { rows: Math.min(ROW_LIMIT, Math.max(1, Math.round(value))) } })
}

function setScope(value: string | number | boolean | undefined): void {
  const next: SamplingScope =
    value === 'full_tables' || value === 'selected_tables' || value === 'schema_only'
      ? value
      : 'schema_only'
  emitPatch({
    sampling: {
      scope: next,
      // 只有「指定表」才带表名；其余范围留着表名会让后端以为要按表过滤
      tables: next === 'selected_tables' ? tables.value : null,
    },
  })
}

function setTables(value: unknown): void {
  const list = Array.isArray(value)
    ? value.map((v) => String(v).trim()).filter((v) => v !== '')
    : []
  emitPatch({ sampling: { tables: [...new Set(list)] } })
}

function setOverrideAt(index: number, patch: Partial<{ column: string; method: string }>): void {
  emitPatch({
    overrides: overrides.value.map((row, i) => (i === index ? { ...row, ...patch } : row)),
  })
}

function addOverride(): void {
  const rows = overrides.value
  // 新增的空白行不参与提交（emitPatch 会过滤空字段名），只是给用户一个输入位
  emitPatch({ overrides: [...rows, { column: '', method: maskDefault.value }] })
}

function removeOverride(index: number): void {
  emitPatch({ overrides: overrides.value.filter((_, i) => i !== index) })
}
</script>

<template>
  <div class="probe-form">
    <el-alert type="info" :closable="false" show-icon title="只读探查">
      <p class="probe-form__notice">
        探查不修改源端数据，也不读取超出采样范围的业务数据；
        采样值在后端入库前完成脱敏，平台不保存原始值。
      </p>
    </el-alert>

    <el-form label-width="110px" label-position="right" @submit.prevent>
      <el-form-item label="采样范围" required>
        <el-radio-group
          :model-value="scope"
          :disabled="submitting"
          @update:model-value="setScope"
        >
          <el-radio-button v-for="o in SCOPE_OPTIONS" :key="o.value" :value="o.value">
            {{ o.label }}
          </el-radio-button>
        </el-radio-group>
        <p class="probe-form__hint">{{ scopeHint }}</p>
      </el-form-item>

      <el-form-item v-if="scope === 'selected_tables'" label="采样表" required>
        <el-select
          multiple
          filterable
          allow-create
          default-first-option
          :model-value="tables"
          :disabled="submitting"
          class="probe-form__control"
          placeholder="选择或输入表名"
          @update:model-value="setTables"
        >
          <el-option v-for="t in tableOptions" :key="t" :value="t" :label="t" />
        </el-select>
        <p v-if="tables.length === 0" class="probe-form__hint probe-form__hint--warn">
          至少选择一张表。
        </p>
      </el-form-item>

      <el-form-item label="采样行数">
        <el-input-number
          :model-value="sampling.rows"
          :min="1"
          :max="ROW_LIMIT"
          :step="100"
          :controls="false"
          :disabled="submitting || scope === 'schema_only'"
          class="probe-form__number"
          @update:model-value="setRows"
        />
        <p class="probe-form__hint">
          每张表最多采样 {{ formatInt(ROW_LIMIT) }} 行；「仅结构」不采样。
        </p>
      </el-form-item>

      <el-form-item label="掩码方式" required>
        <el-select
          :model-value="maskDefault"
          :disabled="submitting"
          @update:model-value="(v: string) => emitPatch({ maskDefault: v })"
        >
          <el-option v-for="m in MASK_METHODS" :key="m.value" :value="m.value" :label="m.label" />
        </el-select>
        <p class="probe-form__hint">{{ maskHint }}</p>
      </el-form-item>

      <el-form-item label="字段级覆盖">
        <div class="probe-form__overrides">
          <div v-for="(row, index) in overrides" :key="`${index}-${row.column}`" class="probe-form__override-row">
            <el-input
              :model-value="row.column"
              :disabled="submitting"
              placeholder="字段名，如 mobile"
              @update:model-value="(v: string) => setOverrideAt(index, { column: v })"
            />
            <el-select
              :model-value="row.method"
              :disabled="submitting"
              @update:model-value="(v: string) => setOverrideAt(index, { method: v })"
            >
              <el-option v-for="m in MASK_METHODS" :key="m.value" :value="m.value" :label="m.label" />
            </el-select>
            <el-button :icon="Delete" link :disabled="submitting" @click="removeOverride(index)">
              移除
            </el-button>
          </div>
          <el-button :icon="Plus" size="small" :disabled="submitting" @click="addOverride">
            添加字段覆盖
          </el-button>
          <p class="probe-form__hint">
            不填则由后端按字段类型与敏感级别选择掩码方式（sensitive_level ≥ confidential 强制脱敏）。
          </p>
        </div>
      </el-form-item>
    </el-form>

    <el-alert
      v-if="!connectable"
      type="warning"
      :closable="false"
      show-icon
      title="该类型仅登记接口"
      description="未接入真实连接器，无法发起探查。"
    />

    <div class="probe-form__actions">
      <el-button :disabled="submitting" @click="emit('cancel')">取消</el-button>
      <el-button type="primary" :loading="submitting" :disabled="!connectable" @click="emit('submit')">
        发起探查
      </el-button>
    </div>
  </div>
</template>

<style scoped>
.probe-form {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.probe-form__notice {
  margin: 0;
  line-height: 18px;
}

.probe-form__control {
  width: 100%;
  max-width: 460px;
}

.probe-form__number {
  width: 140px;
}

.probe-form__number :deep(.el-input__inner) {
  text-align: left;
}

.probe-form__hint {
  width: 100%;
  margin: 2px 0 0;
  font-size: 11px;
  line-height: 16px;
  color: var(--elt-text-muted);
}

.probe-form__hint--warn {
  color: var(--elt-warn);
}

.probe-form__overrides {
  display: flex;
  flex-direction: column;
  gap: 8px;
  width: 100%;
  max-width: 620px;
}

.probe-form__override-row {
  display: flex;
  gap: 8px;
  align-items: center;
}

.probe-form__actions {
  display: flex;
  gap: 8px;
  justify-content: flex-end;
}
</style>
