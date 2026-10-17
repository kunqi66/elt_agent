<script setup lang="ts">
/**
 * 准备单筛选栏（fe-05 §6）。
 *
 * 筛选条件一律**透传给服务端**（`scope` / `status` / `slot` …）：
 * 前端不按 `created_by` 二次裁剪，否则分页 total 会与实际条数对不上，
 * 用户会看到「共 20 条」但只有 7 行。
 *
 * 下拉选项全部取自 `utils/enums` 的枚举表——页面不自己抄一份取值清单，
 * 后端新增状态时这里自动跟上。
 */
import { ref, watch } from 'vue'
import { enumOptions } from '@/utils/enums'
import type { PreparationFilter } from '@/types'

const props = withDefaults(
  defineProps<{
    filter: PreparationFilter
    disabled?: boolean
  }>(),
  { disabled: false },
)

const emit = defineEmits<{
  change: [patch: Partial<PreparationFilter>]
  reset: []
}>()

const statusOptions = enumOptions('PreparationStatus')
const riskOptions = enumOptions('RiskLevel')
const envOptions = enumOptions('Env')
const slotOptions = enumOptions('ApprovalSlot')

const keyword = ref<string>(props.filter.keyword ?? '')

// 外部重置（清空筛选）时把输入框同步回去，否则会留下上一轮的搜索词
watch(
  () => props.filter.keyword,
  (value) => {
    keyword.value = value ?? ''
  },
)

/** el-select 可清空时会给出空串；统一收敛成 undefined，避免把空条件发给后端 */
function orUndefined(value: string): string | undefined {
  return value === '' ? undefined : value
}

function onStatus(value: string): void {
  emit('change', { status: orUndefined(value) as PreparationFilter['status'] })
}

function onRisk(value: string): void {
  emit('change', { risk_level: orUndefined(value) as PreparationFilter['risk_level'] })
}

function onEnv(value: string): void {
  emit('change', { environment: orUndefined(value) as PreparationFilter['environment'] })
}

function onSlot(value: string): void {
  emit('change', { slot: orUndefined(value) as PreparationFilter['slot'] })
}

function applyKeyword(): void {
  const trimmed = keyword.value.trim()
  emit('change', { keyword: trimmed === '' ? undefined : trimmed })
}
</script>

<template>
  <div class="filters">
    <div class="filters__field">
      <label class="filters__label" for="prep-filter-status">状态</label>
      <el-select
        id="prep-filter-status"
        :model-value="filter.status ?? ''"
        :disabled="disabled"
        clearable
        placeholder="全部状态"
        size="small"
        class="filters__control"
        @change="onStatus"
      >
        <el-option v-for="opt in statusOptions" :key="opt.value" :label="opt.label" :value="opt.value" />
      </el-select>
    </div>

    <div class="filters__field">
      <label class="filters__label" for="prep-filter-risk">风险等级</label>
      <el-select
        id="prep-filter-risk"
        :model-value="filter.risk_level ?? ''"
        :disabled="disabled"
        clearable
        placeholder="全部等级"
        size="small"
        class="filters__control"
        @change="onRisk"
      >
        <el-option v-for="opt in riskOptions" :key="opt.value" :label="opt.label" :value="opt.value" />
      </el-select>
    </div>

    <div class="filters__field">
      <label class="filters__label" for="prep-filter-env">环境</label>
      <el-select
        id="prep-filter-env"
        :model-value="filter.environment ?? ''"
        :disabled="disabled"
        clearable
        placeholder="全部环境"
        size="small"
        class="filters__control"
        @change="onEnv"
      >
        <el-option v-for="opt in envOptions" :key="opt.value" :label="opt.label" :value="opt.value" />
      </el-select>
    </div>

    <div class="filters__field">
      <label class="filters__label" for="prep-filter-slot">审批槽</label>
      <el-select
        id="prep-filter-slot"
        :model-value="filter.slot ?? ''"
        :disabled="disabled"
        clearable
        placeholder="全部槽位"
        size="small"
        class="filters__control"
        @change="onSlot"
      >
        <el-option v-for="opt in slotOptions" :key="opt.value" :label="opt.label" :value="opt.value" />
      </el-select>
    </div>

    <div class="filters__field filters__field--grow">
      <label class="filters__label" for="prep-filter-keyword">关键字</label>
      <el-input
        id="prep-filter-keyword"
        v-model="keyword"
        :disabled="disabled"
        clearable
        size="small"
        placeholder="Pipeline 名称或指纹前缀"
        class="filters__control"
        @keyup.enter="applyKeyword"
        @clear="applyKeyword"
      />
    </div>

    <div class="filters__actions">
      <el-button size="small" :disabled="disabled" @click="applyKeyword">查询</el-button>
      <el-button size="small" :disabled="disabled" @click="emit('reset')">重置</el-button>
    </div>
  </div>
</template>

<style scoped>
.filters {
  display: flex;
  flex-wrap: wrap;
  gap: 10px 12px;
  align-items: flex-end;
  padding: 12px 14px;
  background-color: var(--elt-bg-surface);
  border: 1px solid var(--elt-border-subtle);
  border-radius: var(--elt-radius-card);
}

.filters__field {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.filters__field--grow {
  flex: 1 1 220px;
  min-width: 200px;
}

.filters__label {
  font-size: 11px;
  color: var(--elt-text-muted);
}

.filters__control {
  width: 150px;
}

.filters__field--grow .filters__control {
  width: 100%;
}

.filters__actions {
  display: flex;
  gap: 8px;
}
</style>
