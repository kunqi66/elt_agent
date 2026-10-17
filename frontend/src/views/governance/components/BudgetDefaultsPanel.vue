<script setup lang="ts">
/**
 * 运行预算面板（fe-07 §6）。
 *
 * 三列对照的语义（顺序不能乱，右侧永远比左侧「更近项目」）：
 *   冻结默认值 → SUP_MAX_* 硬上限 → 项目覆盖。
 *
 * 「项目覆盖超出硬上限」这件事**只是提示，不是阻止**：
 * 真正拒绝这次提交的是后端（VALIDATION_ERROR）。前端不把提交按钮禁掉，
 * 是因为上限值会随后端配置变化，前端一旦「替后端拒绝」，两边迟早会漂移——
 * 而漂移的表现是「明明改小了却被前端拦住」，比多一次失败提交难查得多。
 *
 * 同理，越界提示的文案刻意写成「提交后会被后端拒绝」，而不是「已被阻止」——
 * 此刻还没有发出任何请求，说「已被阻止」是在编造一个并未发生的事实。
 */
import { computed, ref, watch } from 'vue'
import { Refresh, Check } from '@element-plus/icons-vue'
import { EmptyState, ErrorState } from '@/components/common'
import {
  DASH,
  formatAmplification,
  formatBytes,
  formatDuration,
  formatRatioPercent,
  formatRows,
} from '@/utils/format'
import type { BudgetDefaults, BudgetDefaultsRead } from '@/types'

const props = withDefaults(
  defineProps<{
    data: BudgetDefaultsRead | null
    loading?: boolean
    error?: string | null
    /** 是否可以改（只读身份下整块禁用，而不是藏起来——审计员要知道预算是怎么定的） */
    editable: boolean
    /** store.budgetOverLimit：**已保存**的覆盖里越界的字段；只用于提示既有状态 */
    overLimit?: string[]
  }>(),
  { loading: false, error: null, overLimit: () => [] },
)

const emit = defineEmits<{
  retry: []
  submit: [payload: BudgetDefaults]
}>()

const ZERO: BudgetDefaults = {
  max_read_rows: 0,
  max_write_bytes: 0,
  max_duration_sec: 0,
  max_amplification_ratio: 0,
  max_reject_rate: 0,
  warn_threshold_pct: 0,
}

interface FieldMeta {
  key: keyof BudgetDefaults
  label: string
  /** 量纲，决定用哪个格式化器与哪种输入控件 */
  kind: 'rows' | 'bytes' | 'seconds' | 'amplification' | 'rate' | 'percent'
  step: number
  hint: string
}

const FIELDS: FieldMeta[] = [
  {
    key: 'max_read_rows',
    label: '单次读取行数上限',
    kind: 'rows',
    step: 100000,
    hint: '源端一次执行允许扫描的行数',
  },
  {
    key: 'max_write_bytes',
    label: '单次写入字节上限',
    kind: 'bytes',
    step: 1,
    hint: '目标端落盘字节数',
  },
  {
    key: 'max_duration_sec',
    label: '单次执行时长上限',
    kind: 'seconds',
    step: 60,
    hint: '超过则触发运行期监督',
  },
  {
    key: 'max_amplification_ratio',
    label: '放大比上限',
    kind: 'amplification',
    step: 0.1,
    hint: '写入行数 ÷ 读取行数',
  },
  {
    key: 'max_reject_rate',
    label: '脏数据拒绝率上限',
    kind: 'rate',
    step: 0.01,
    hint: '0~1 之间的小数；演示环境冻结值为 0.20',
  },
  {
    key: 'warn_threshold_pct',
    label: '预算预警阈值',
    kind: 'percent',
    step: 1,
    hint: '用量占预算的百分比，达到即预警',
  },
]

/**
 * 需求对齐记录里冻结的预警阈值。
 * 只用于「你改的值和当初冻结的口径不一致」这一条提醒；
 * 判定逻辑不在这里，用户仍然可以改，改完由后端决定收不收。
 */
const FROZEN_WARN_PCT = 80

const byteUnits = { MiB: 1024 ** 2, GiB: 1024 ** 3 } as const
type ByteUnit = keyof typeof byteUnits

const byteUnit = ref<ByteUnit>('GiB')

/** 草稿 = 冻结默认值叠加已保存的覆盖；保存成功后由 data 变化重新灌入 */
const draft = ref<BudgetDefaults>({ ...ZERO })

/** 未保存前的对比基准，用来算「有没有改动」 */
const baseline = computed<BudgetDefaults>(() => {
  const current = props.data
  if (current === null) {
    return { ...ZERO }
  }
  return { ...current.defaults, ...(current.override ?? {}) }
})

watch(
  () => props.data,
  (next) => {
    draft.value = { ...(next === null ? ZERO : { ...next.defaults, ...(next.override ?? {}) }) }
  },
  { immediate: true },
)

const dirty = computed<boolean>(() =>
  FIELDS.some((field) => draft.value[field.key] !== baseline.value[field.key]),
)

const overLimitLabels = computed<string[]>(() =>
  FIELDS.filter((field) => props.overLimit.includes(field.key)).map((field) => field.label),
)

function display(field: FieldMeta, value: number | undefined): string {
  if (value === undefined) {
    return DASH
  }
  if (field.kind === 'rows') {
    return formatRows(value)
  }
  if (field.kind === 'bytes') {
    return formatBytes(value)
  }
  if (field.kind === 'seconds') {
    return formatDuration(value)
  }
  if (field.kind === 'amplification') {
    return formatAmplification(value)
  }
  if (field.kind === 'rate') {
    return formatRatioPercent(value)
  }
  return `${value}%`
}

/**
 * 表格里的 `row` 是 el-table 给的无类型对象，直接 `draft[row.key]` 会退化成
 * 「用 any 索引对象」而报错。统一走这三个取值函数：
 * 参数声明成 FieldMeta，表头外的类型就收得住，键的合法性也由 `keyof BudgetDefaults` 保证。
 */
function defaultValueOf(field: FieldMeta): number {
  return props.data === null ? 0 : props.data.defaults[field.key]
}

function hardLimitOf(field: FieldMeta): number {
  return props.data === null ? 0 : props.data.hard_limits[field.key]
}

function draftValueOf(field: FieldMeta): number {
  return draft.value[field.key]
}

/** 覆盖值是否已经越过硬上限；与 store.budgetOverLimit 同一条规则，只是这里算的是草稿 */
function isOverHardLimit(field: FieldMeta): boolean {
  const limits = props.data?.hard_limits
  return limits !== undefined && draft.value[field.key] > limits[field.key]
}

/** 覆盖值是否高于冻结默认值（放宽） */
function isLooserThanDefault(field: FieldMeta): boolean {
  const defaults = props.data?.defaults
  return defaults !== undefined && draft.value[field.key] > defaults[field.key]
}

/** 字节字段的输入值随单位换算，改单位不改变字节本身 */
const byteInputValue = computed<number>(() =>
  Math.round((draft.value.max_write_bytes / byteUnits[byteUnit.value]) * 100) / 100,
)

function onByteInput(value: number | undefined): void {
  draft.value.max_write_bytes = Math.round((value ?? 0) * byteUnits[byteUnit.value])
}

function onNumberInput(field: FieldMeta, value: number | undefined): void {
  draft.value[field.key] = value ?? 0
}

function onReset(): void {
  draft.value = { ...baseline.value }
}

function onSubmit(): void {
  emit('submit', { ...draft.value })
}

/**
 * el-table 插槽的 row 静态类型是 Element Plus 的 DefaultRow，与 FieldMeta 无类型关联。
 * 直接读属性不报错（索引签名读出 any），但作为参数传给上面那些强类型函数就会失败。
 * 与其放宽它们的签名（会让整文件的类型检查一起退化），不如在调用点收口。
 */
function fieldOf(row: unknown): FieldMeta {
  return row as FieldMeta
}
</script>

<template>
  <div class="budget">
    <ErrorState v-if="error !== null" :message="error" @retry="emit('retry')" />

    <div v-else-if="loading && data === null" class="elt-skeleton elt-skeleton--block" :style="{ height: '220px' }" />

    <EmptyState
      v-else-if="data === null"
      variant="no-data"
      title="该项目还没有预算配置"
      description="预算默认值由后端初始化；请先选择项目，或联系管理员为该项目建设预算配置。"
    />

    <template v-else>
      <div class="budget__bar">
        <span class="budget__state">
          {{ data.override === null ? '当前无项目覆盖，执行按冻结默认值' : '当前存在项目覆盖，右列即生效值' }}
        </span>
        <el-button
          v-if="editable"
          size="small"
          :icon="Refresh"
          :disabled="!dirty"
          @click="onReset"
        >
          还原为当前生效值
        </el-button>
        <el-button
          v-if="editable"
          type="primary"
          size="small"
          :icon="Check"
          :disabled="!dirty"
          @click="onSubmit"
        >
          提交覆盖
        </el-button>
        <span v-else class="budget__readonly">当前身份只读，可查看但不可修改预算</span>
      </div>

      <el-alert
        v-if="overLimitLabels.length > 0"
        class="budget__alert"
        type="warning"
        :closable="false"
        show-icon
        title="已保存的覆盖中有字段超过硬上限"
        :description="`${overLimitLabels.join('、')} 已超出 SUP_MAX_* 硬上限；该覆盖不会被后端接受，请改回上限以内。`"
      />

      <el-table :data="FIELDS" size="small" class="budget__table">
        <el-table-column label="预算项" min-width="180">
          <template #default="{ row }">
            <div class="budget__label">{{ row.label }}</div>
            <div class="budget__hint">{{ row.hint }}</div>
          </template>
        </el-table-column>

        <el-table-column label="冻结默认值" width="140">
          <template #default="{ row }">
            <span class="elt-mono">{{ display(fieldOf(row), defaultValueOf(fieldOf(row))) }}</span>
          </template>
        </el-table-column>

        <el-table-column label="硬上限 SUP_MAX" width="150">
          <template #default="{ row }">
            <div class="elt-mono">{{ display(fieldOf(row), hardLimitOf(fieldOf(row))) }}</div>
            <div class="budget__hint">超出即被后端拒绝</div>
          </template>
        </el-table-column>

        <el-table-column label="项目覆盖" min-width="230">
          <template #default="{ row }">
            <div class="budget__input">
              <el-input-number
                v-if="row.kind === 'bytes'"
                :model-value="byteInputValue"
                :min="0"
                :step="1"
                :disabled="!editable"
                size="small"
                controls-position="right"
                @change="onByteInput"
              />
              <el-select
                v-if="row.kind === 'bytes'"
                v-model="byteUnit"
                size="small"
                class="budget__unit"
                :disabled="!editable"
              >
                <el-option label="MiB" value="MiB" />
                <el-option label="GiB" value="GiB" />
              </el-select>
              <el-input-number
                v-else
                :model-value="draftValueOf(fieldOf(row))"
                :min="0"
                :step="row.step"
                :disabled="!editable"
                size="small"
                controls-position="right"
                @change="(value: number | undefined) => onNumberInput(fieldOf(row), value)"
              />
              <span class="budget__raw elt-mono">{{ display(fieldOf(row), draftValueOf(fieldOf(row))) }}</span>
            </div>

            <div v-if="isOverHardLimit(fieldOf(row))" class="budget__over">
              超出硬上限：提交后会被后端拒绝（VALIDATION_ERROR），本次改动不会生效。
            </div>
            <div v-else-if="isLooserThanDefault(fieldOf(row))" class="budget__loose">
              高于冻结默认值：本项目执行预算被放宽，P1 判定阈值（默认 3 倍）随之变化。
            </div>
          </template>
        </el-table-column>
      </el-table>

      <div v-if="draft.warn_threshold_pct !== FROZEN_WARN_PCT" class="budget__note">
        预警阈值 {{ draft.warn_threshold_pct }}% 与《需求对齐记录》冻结值 {{ FROZEN_WARN_PCT }}% 不一致，
        请确认这是有意调整——口径不同会让「预算预警」与「P1 判定」错位。
      </div>

      <p class="budget__foot">
        硬上限是只读展示项，不是输入上限：它的用途是告诉你「改到多少会被拒」，
        真正的裁决始终在后端。
      </p>
    </template>
  </div>
</template>

<style scoped>
.budget {
  display: flex;
  flex-direction: column;
}

.budget__bar {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  align-items: center;
  margin-bottom: 12px;
}

.budget__state {
  font-size: 12px;
  color: var(--elt-text-secondary);
}

.budget__readonly {
  font-size: 12px;
  color: var(--elt-text-muted);
}

.budget__alert {
  margin-bottom: 12px;
}

.budget__label {
  font-size: 13px;
  color: var(--elt-text-primary);
}

.budget__hint {
  margin-top: 2px;
  font-size: 11px;
  line-height: 1.6;
  color: var(--elt-text-muted);
}

.budget__input {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  align-items: center;
}

.budget__unit {
  width: 84px;
}

.budget__raw {
  font-size: 11px;
  color: var(--elt-text-muted);
}

.budget__over {
  margin-top: 4px;
  font-size: 11px;
  line-height: 1.6;
  color: var(--elt-danger);
}

.budget__loose {
  margin-top: 4px;
  font-size: 11px;
  line-height: 1.6;
  color: var(--elt-risk-p1);
}

.budget__note {
  margin-top: 12px;
  font-size: 12px;
  line-height: 1.7;
  color: var(--elt-warn);
}

.budget__foot {
  margin: 12px 0 0;
  font-size: 11px;
  line-height: 1.7;
  color: var(--elt-text-muted);
}
</style>
