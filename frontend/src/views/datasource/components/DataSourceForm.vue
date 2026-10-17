<script setup lang="ts">
/**
 * 数据源表单（fe-03 §5.1 / §6.1）。新建与编辑共用。
 *
 * 为什么类型在编辑态禁用：类型决定了 endpoint 的键集合与 Vault 里的键名
 * （mysql 的 host/port 换到 minio 上完全不是一回事），后端也没有「换类型」的语义。
 * 要换类型只能新建一个数据源——这比让用户把一半字段改得似是而非更安全。
 *
 * 本组件不接收也不渲染任何凭据值：凭据区由父级通过 `credentials` 插槽传入
 * （见 SecretSection.vue 的说明）。
 */
import { computed, onMounted, ref } from 'vue'
import { fetchMembers } from '@/api/project'
import { useProjectStore } from '@/stores/project'
import { enumOptions } from '@/utils/enums'
import { DATA_SOURCE_TYPES, dataSourceTypeSpec } from '@/types'
import EndpointFields from './EndpointFields.vue'
import type { DataSourceEndpoint, DataSourceUpsertPayload, DataSourceType, Env, SensitiveLevel } from '@/types'

const props = withDefaults(
  defineProps<{
    modelValue: DataSourceUpsertPayload
    mode: 'create' | 'edit'
    submitting?: boolean
    /** 键为 `name` / `env` / `endpoint.<key>`，与提交体校验口径一致 */
    errors?: Record<string, string>
  }>(),
  { submitting: false, errors: () => ({}) },
)

const emit = defineEmits<{
  'update:modelValue': [value: DataSourceUpsertPayload]
  submit: []
  cancel: []
}>()

const projectStore = useProjectStore()

const typeOptions = DATA_SOURCE_TYPES.map((spec) => ({
  value: spec.type,
  label: spec.connectable ? spec.label : `${spec.label}（仅登记）`,
}))
const envOptions = enumOptions('Env')
const levelOptions = enumOptions('SensitiveLevel')

const spec = computed(() => dataSourceTypeSpec(props.modelValue.type))
const typeLocked = computed<boolean>(() => props.mode === 'edit')

/** 负责人候选来自项目成员；拉不到不阻塞保存（owner 只用于展示与通知） */
const ownerOptions = ref<Array<{ value: number; label: string }>>([])
const ownerLoadFailed = ref(false)

onMounted(() => {
  void loadOwners()
})

async function loadOwners(): Promise<void> {
  const projectId = projectStore.currentProjectId
  if (projectId === null) {
    return
  }
  try {
    const members = await fetchMembers(projectId)
    ownerOptions.value = members
      .filter((m) => m.status === 'active')
      .map((m) => ({ value: m.user_id, label: `${m.display_name}（${m.username}）` }))
  } catch {
    ownerLoadFailed.value = true
  }
}

/** 字段表的 default 落进提交体：端口这类必填项不能只靠占位展示 */
function endpointDefaults(type: DataSourceType): DataSourceEndpoint {
  const out: DataSourceEndpoint = {}
  for (const field of dataSourceTypeSpec(type).endpointFields) {
    if (field.default !== undefined) {
      out[field.key] = field.default
    }
  }
  return out
}

function patch(next: Partial<DataSourceUpsertPayload>): void {
  emit('update:modelValue', { ...props.modelValue, ...next })
}

function setType(value: string | number | boolean | undefined): void {
  if (typeof value !== 'string') {
    return
  }
  const type = value as DataSourceType
  if (type === props.modelValue.type) {
    return
  }
  // 换类型必须同时重置 endpoint：否则 mysql 的 host/port 会被原样带到 minio 上，
  // 后端只能看到一个「键名合法但语义全错」的连接参数
  patch({ type, endpoint: endpointDefaults(type) })
}

function setTags(value: unknown): void {
  const list = Array.isArray(value)
    ? value
        .map((v) => String(v).trim())
        .filter((v) => v !== '')
        .map((v) => v.slice(0, 32))
    : []
  // 限制 20 个：标签只用于检索，无上限会让筛选器被自定义标签撑爆
  patch({ tags: [...new Set(list)].slice(0, 20) })
}

function setOwner(value: string | number | boolean | undefined): void {
  if (value === undefined || value === '') {
    patch({ owner_id: null })
    return
  }
  const id = Number(value)
  patch({ owner_id: Number.isFinite(id) ? id : null })
}
</script>

<template>
  <el-form label-width="132px" label-position="right" @submit.prevent>
    <el-form-item label="名称" required :error="errors.name ?? ''">
      <el-input
        :model-value="modelValue.name"
        :disabled="submitting"
        maxlength="64"
        show-word-limit
        placeholder="项目内唯一，如 mysql-order-src"
        class="ds-form__control"
        @update:model-value="(v: string) => patch({ name: v })"
      />
      <p class="ds-form__hint">仅用于展示与审计，不参与建立连接。</p>
    </el-form-item>

    <el-form-item label="类型" required>
      <el-select
        :model-value="modelValue.type"
        :disabled="submitting || typeLocked"
        class="ds-form__control"
        @update:model-value="setType"
      >
        <el-option v-for="t in typeOptions" :key="t.value" :value="t.value" :label="t.label" />
      </el-select>
      <p class="ds-form__hint">
        类型决定连接参数字段与凭据键名；编辑态不可更改，需要换类型请新建。
      </p>
    </el-form-item>

    <el-form-item label="环境" required :error="errors.env ?? ''">
      <el-select
        :model-value="modelValue.env"
        :disabled="submitting"
        @update:model-value="(v: string) => patch({ env: v as Env })"
      >
        <el-option v-for="o in envOptions" :key="o.value" :value="o.value" :label="o.label" />
      </el-select>
      <p class="ds-form__hint">prod 环境的数据源参与生成时会走更严格的审批与门禁。</p>
    </el-form-item>

    <el-form-item label="敏感级别" required :error="errors.sensitive_level ?? ''">
      <el-select
        :model-value="modelValue.sensitive_level"
        :disabled="submitting"
        @update:model-value="(v: string) => patch({ sensitive_level: v as SensitiveLevel })"
      >
        <el-option v-for="o in levelOptions" :key="o.value" :value="o.value" :label="o.label" />
      </el-select>
      <p class="ds-form__hint">≥ confidential 的字段强制脱敏，且不会进入 LLM 上下文。</p>
    </el-form-item>

    <el-form-item label="负责人">
      <el-select
        :model-value="modelValue.owner_id === null ? '' : modelValue.owner_id"
        :disabled="submitting"
        :loading="ownerOptions.length === 0 && !ownerLoadFailed"
        clearable
        filterable
        class="ds-form__control"
        placeholder="选择项目成员"
        @update:model-value="setOwner"
      >
        <el-option v-for="m in ownerOptions" :key="m.value" :value="m.value" :label="m.label" />
      </el-select>
      <p v-if="ownerLoadFailed" class="ds-form__hint ds-form__hint--warn">
        成员列表加载失败，可先保存后再补负责人。
      </p>
    </el-form-item>

    <el-form-item label="标签">
      <el-select
        multiple
        filterable
        allow-create
        default-first-option
        :model-value="modelValue.tags"
        :disabled="submitting"
        class="ds-form__control"
        placeholder="回车创建标签，最多 20 个"
        @update:model-value="setTags"
      >
        <el-option v-for="tag in modelValue.tags" :key="tag" :value="tag" :label="tag" />
      </el-select>
    </el-form-item>

    <el-divider content-position="left">连接参数</el-divider>

    <EndpointFields
      :spec="spec"
      :model-value="modelValue.endpoint"
      :errors="errors"
      :disabled="submitting"
      @update:model-value="(v: DataSourceEndpoint) => patch({ endpoint: v })"
    />

    <!-- 凭据区由父级插入：它持有明文输入的内存副本，不能经由本组件中转 -->
    <slot name="credentials" />

    <div class="ds-form__actions">
      <el-button :disabled="submitting" @click="emit('cancel')">取消</el-button>
      <el-button type="primary" :loading="submitting" @click="emit('submit')">保存</el-button>
    </div>
  </el-form>
</template>

<style scoped>
.ds-form__control {
  width: 100%;
  max-width: 420px;
}

.ds-form__hint {
  width: 100%;
  margin: 2px 0 0;
  font-size: 11px;
  line-height: 16px;
  color: var(--elt-text-muted);
}

.ds-form__hint--warn {
  color: var(--elt-warn);
}

.ds-form__actions {
  display: flex;
  gap: 8px;
  justify-content: flex-end;
  padding-top: 4px;
}
</style>
