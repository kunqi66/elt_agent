<script setup lang="ts">
/**
 * 按 DataSourceType 渲染 endpoint 字段组（fe-03 §6.1）。
 *
 * 字段表取自 `@/types` 的 DATA_SOURCE_TYPES（前端唯一事实来源）：
 * 这里只负责把 spec 渲染成控件，不另抄一份「必填键」清单——
 * 抄一份就必然在后端新增类型时漏改一处。
 *
 * endpoint 中不得出现凭据键：字段表天然不含 password/token，
 * 提交前的黑名单校验（DataSourceEditView）是第二道防线。
 */
import type { DataSourceEndpoint, DataSourceTypeSpec } from '@/types'

const props = withDefaults(
  defineProps<{
    spec: DataSourceTypeSpec
    modelValue: DataSourceEndpoint
    /** 字段级错误：键为 `endpoint.<key>`，与提交体校验口径一致 */
    errors?: Record<string, string>
    disabled?: boolean
  }>(),
  { errors: () => ({}), disabled: false },
)

const emit = defineEmits<{ 'update:modelValue': [value: DataSourceEndpoint] }>()

/** 少数键是枚举而非自由文本：写成下拉，避免手填 Bearer/BEARER 之类取值被后端 422 */
const ENUM_KEYS: Record<string, Array<{ value: string; label: string }>> = {
  auth_type: [
    { value: 'none', label: '无认证' },
    { value: 'basic', label: 'Basic（用户名 + 密码）' },
    { value: 'bearer', label: 'Bearer Token' },
    { value: 'api_key', label: 'API Key' },
  ],
  has_header: [
    { value: 'true', label: '是' },
    { value: 'false', label: '否' },
  ],
}

function enumChoices(key: string): Array<{ value: string; label: string }> {
  return ENUM_KEYS[key] ?? []
}

function raw(key: string): string | number | undefined {
  const value = props.modelValue[key]
  if (value === undefined || value === null) {
    return undefined
  }
  return value
}

/** 未填时用字段表的 default 兜底展示：3306/5432 这类端口不该让用户手打 */
function textValue(key: string): string {
  const value = raw(key)
  if (value !== undefined) {
    return String(value)
  }
  const field = props.spec.endpointFields.find((f) => f.key === key)
  return field?.default === undefined ? '' : String(field.default)
}

function numberValue(key: string): number | undefined {
  const value = raw(key)
  if (typeof value === 'number') {
    return value
  }
  if (typeof value === 'string' && value.trim() !== '' && Number.isFinite(Number(value))) {
    return Number(value)
  }
  return undefined
}

function errorOf(key: string): string {
  return props.errors[`endpoint.${key}`] ?? ''
}

function setKey(key: string, value: string | number | undefined): void {
  const next: DataSourceEndpoint = { ...props.modelValue }
  if (value === undefined || value === '') {
    // 空值不留在提交体里：后端按「键存在即校验」，留空串会被判为非法取值
    delete next[key]
  } else {
    next[key] = value
  }
  emit('update:modelValue', next)
}
</script>

<template>
  <el-form-item
    v-for="field in spec.endpointFields"
    :key="field.key"
    :label="field.label"
    :required="field.required"
    :error="errorOf(field.key)"
  >
    <el-select
      v-if="enumChoices(field.key).length > 0"
      :model-value="textValue(field.key)"
      :disabled="disabled"
      class="endpoint-field"
      @update:model-value="(v: string) => setKey(field.key, v)"
    >
      <el-option
        v-for="choice in enumChoices(field.key)"
        :key="choice.value"
        :value="choice.value"
        :label="choice.label"
      />
    </el-select>

    <el-input-number
      v-else-if="field.numeric === true"
      :model-value="numberValue(field.key)"
      :disabled="disabled"
      :min="1"
      :max="65535"
      :controls="false"
      class="endpoint-field endpoint-field--number"
      @update:model-value="(v: number | undefined) => setKey(field.key, v)"
    />

    <el-input
      v-else
      :model-value="textValue(field.key)"
      :disabled="disabled"
      :placeholder="field.placeholder"
      class="endpoint-field"
      @update:model-value="(v: string) => setKey(field.key, v)"
    />

    <p v-if="field.numeric === true" class="endpoint-field__hint">TCP 端口（1–65535）</p>
  </el-form-item>
</template>

<style scoped>
.endpoint-field {
  width: 100%;
  max-width: 420px;
}

.endpoint-field--number :deep(.el-input__inner) {
  text-align: left;
}

.endpoint-field__hint {
  width: 100%;
  margin: 2px 0 0;
  font-size: 11px;
  line-height: 16px;
  color: var(--elt-text-muted);
}
</style>
