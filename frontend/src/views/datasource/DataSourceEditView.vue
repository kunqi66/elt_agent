<script setup lang="ts">
/**
 * 新建 / 编辑数据源（fe-03 §5.1 / §5.2）。两个路由共用本组件，按 route.name 区分。
 *
 * 为什么要共用：两边字段完全一致，拆成两个组件必然出现
 * 「编辑页少校验一条」的漂移，而这类漏检直接导致把非法连接参数写进库。
 *
 * 凭据的三条铁律（CLAUDE.md §5.2）：
 * 1. 编辑态**不回显**任何凭据值，只显示 Vault 路径与版本；
 * 2. 只有「新建」或用户显式点「重新设置凭据」时，credentials 才出现在提交体里；
 * 3. 提交成功后立刻清空内存副本——它不该比 Cookie 活得久。
 */
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ArrowLeft } from '@element-plus/icons-vue'
import AsyncTaskPanel from '@/components/common/AsyncTaskPanel.vue'
import ErrorState from '@/components/common/ErrorState.vue'
import PageHeader from '@/components/common/PageHeader.vue'
import SectionCard from '@/components/common/SectionCard.vue'
import { useAsyncTask } from '@/composables/useAsyncTask'
import { useDataSourceStore } from '@/stores/datasource'
import { useProjectStore } from '@/stores/project'
import { ElMessage } from '@/utils/feedback'
import { fieldErrors, isBizError, toDisplayMessage } from '@/utils/errors'
import { CREDENTIAL_KEY_BLACKLIST, dataSourceTypeSpec } from '@/types'
import * as datasourceApi from '@/api/datasource'
import DataSourceForm from './components/DataSourceForm.vue'
import SecretSection from './components/SecretSection.vue'
import type { DataSourceEndpoint, DataSourceType, DataSourceUpsertPayload } from '@/types'

const route = useRoute()
const router = useRouter()
const store = useDataSourceStore()
const projectStore = useProjectStore()

const isCreate = computed<boolean>(() => route.name === 'datasource-create')
const dsId = computed<number | null>(() => {
  const raw = route.params.id
  const value = Number.parseInt(typeof raw === 'string' ? raw : '', 10)
  return Number.isFinite(value) && value > 0 ? value : null
})

const spec = computed(() => dataSourceTypeSpec(form.value.type))
const loadError = ref<string | null>(null)
const loading = ref(true)

/** 凭据值的内存副本：只在这里与 SecretSection 之间流动 */
const credentials = ref<Record<string, string>>({})
const credentialsEditing = ref<boolean>(false)
/** 服务端下发的引用（编辑态有值）；仅用于展示，不含任何值 */
const secretRef = computed(() => store.detail?.secret_ref ?? null)

const errors = ref<Record<string, string>>({})

function defaultPayload(): DataSourceUpsertPayload {
  const type: DataSourceType = 'mysql'
  return {
    name: '',
    type,
    env: 'dev',
    sensitive_level: 'internal',
    endpoint: endpointDefaults(type),
    tags: [],
    owner_id: null,
  }
}

function endpointDefaults(type: DataSourceType): DataSourceEndpoint {
  const out: DataSourceEndpoint = {}
  for (const field of dataSourceTypeSpec(type).endpointFields) {
    if (field.default !== undefined) {
      out[field.key] = field.default
    }
  }
  return out
}

const form = ref<DataSourceUpsertPayload>(defaultPayload())

/* ------------------------------------------------------------------ */
/* 凭据键名                                                            */
/* ------------------------------------------------------------------ */

/**
 * rest 类型的凭据键随认证方式变化（basic 两键 / bearer 单 token），
 * 所以不能只看类型字段表——那会把四个键全列出来，逼用户填用不上的键。
 */
function credentialKeysFor(type: DataSourceType, endpoint: DataSourceEndpoint): string[] {
  const typeSpec = dataSourceTypeSpec(type)
  if (type !== 'rest') {
    return typeSpec.credentialKeys
  }
  switch (String(endpoint['auth_type'] ?? 'none')) {
    case 'basic':
      return ['username', 'password']
    case 'bearer':
      return ['token']
    case 'api_key':
      return ['api_key']
    default:
      return []
  }
}

const credentialKeys = computed<string[]>(() => {
  // 已有引用时以服务端 key_names 为准：Vault 里到底存了哪些键，只有后端知道
  const existing = secretRef.value?.key_names ?? []
  if (!isCreate.value && existing.length > 0) {
    return existing
  }
  return credentialKeysFor(form.value.type, form.value.endpoint)
})

/* ------------------------------------------------------------------ */
/* 加载                                                                */
/* ------------------------------------------------------------------ */

onMounted(async () => {
  try {
    await projectStore.ensureLoaded()
  } catch (e) {
    loadError.value = toDisplayMessage(e)
    loading.value = false
    return
  }

  if (isCreate.value) {
    // 新建：不预填任何凭据，进入即为「填写新凭据」状态
    credentialsEditing.value = true
    loading.value = false
    return
  }

  const id = dsId.value
  if (id === null) {
    loadError.value = 'URL 中缺少有效的数据源编号'
    loading.value = false
    return
  }
  await store.loadDetail(id)
  if (store.detail === null) {
    loadError.value = store.detailError ?? '数据源不存在或已被删除'
    loading.value = false
    return
  }

  form.value = {
    name: store.detail.name,
    type: store.detail.type,
    env: store.detail.env,
    sensitive_level: store.detail.sensitive_level,
    endpoint: { ...store.detail.endpoint },
    tags: [...store.detail.tags],
    owner_id: store.detail.owner_id,
  }
  // 编辑态默认不回显、也不重写凭据；要改必须先点「重新设置凭据」
  credentialsEditing.value = store.detail.secret_ref === null
  loading.value = false
})

/* ------------------------------------------------------------------ */
/* 校验与提交                                                          */
/* ------------------------------------------------------------------ */

function validate(): Record<string, string> {
  const found: Record<string, string> = {}
  const current = form.value

  const name = current.name.trim()
  if (name === '') {
    found.name = '名称必填'
  } else if (name.length > 64) {
    found.name = '名称不得超过 64 个字符'
  }

  const endpoint = buildEndpoint()
  for (const field of spec.value.endpointFields) {
    const value = endpoint[field.key]
    if (field.required && (value === undefined || String(value).trim() === '')) {
      found[`endpoint.${field.key}`] = `${field.label}必填`
    }
  }

  // 凭据键出现在 endpoint 里 = 明文凭据将要落库；这里必须拦住（后端也会拒）
  for (const key of Object.keys(endpoint)) {
    if (CREDENTIAL_KEY_BLACKLIST.includes(key.toLowerCase())) {
      found[`endpoint.${key}`] = '该键属于凭据，必须走 Vault 引用，不能写进连接参数'
    }
  }

  if (credentialsEditing.value) {
    for (const key of credentialKeys.value) {
      const value = credentials.value[key] ?? ''
      if (value.trim() === '') {
        found[`credentials.${key}`] = '必填（需一次性提交全部键名）'
      }
    }
  }

  return found
}

/** 把字段表的默认值补进提交体：必填项不能只靠占位符展示 */
function buildEndpoint(): DataSourceEndpoint {
  const out: DataSourceEndpoint = { ...form.value.endpoint }
  for (const field of spec.value.endpointFields) {
    const value = out[field.key]
    if ((value === undefined || value === '') && field.default !== undefined) {
      out[field.key] = field.default
    }
  }
  return out
}

function buildPayload(): DataSourceUpsertPayload {
  const payload: DataSourceUpsertPayload = {
    name: form.value.name.trim(),
    type: form.value.type,
    env: form.value.env,
    sensitive_level: form.value.sensitive_level,
    endpoint: buildEndpoint(),
    tags: form.value.tags,
    owner_id: form.value.owner_id,
  }
  // 不回显、不重写：只有「新建」或用户显式重置时才带 credentials 键
  if (credentialsEditing.value && credentialKeys.value.length > 0) {
    const values: Record<string, string> = {}
    for (const key of credentialKeys.value) {
      values[key] = credentials.value[key] ?? ''
    }
    payload.credentials = values
  }
  return payload
}

const saveTask = useAsyncTask(
  async () => {
    const payload = buildPayload()
    try {
      if (isCreate.value) {
        const projectId = projectStore.currentProjectId
        if (projectId === null) {
          throw new Error('尚未选择项目，无法创建数据源')
        }
        const created = await datasourceApi.createDataSource(projectId, payload)
        await store.query.reload()
        // 建完直接进详情并自动跑一次连通性测试：新建的连接最需要立刻验证
        await router.replace({
          name: 'datasource-detail',
          params: { id: String(created.id) },
          query: { test: '1' },
        })
      } else {
        const id = dsId.value
        if (id === null) {
          throw new Error('缺少数据源编号')
        }
        const updated = await datasourceApi.updateDataSource(id, payload)
        await store.query.reload()
        await store.loadDetail(updated.id)
        await router.replace({ name: 'datasource-detail', params: { id: String(updated.id) } })
      }
      // 提交成功后不再需要内存副本；失败时保留，免得用户重打一遍
      credentials.value = {}
    } catch (e) {
      if (isBizError(e) && e.code === 'CONFLICT') {
        errors.value = { ...errors.value, name: '同名数据源已存在，请换一个名称' }
      } else if (isBizError(e) && e.code === 'VALIDATION_ERROR') {
        // 后端字段名可能带 body. 前缀，剥掉后才与本地错误键对齐
        const mapped: Record<string, string> = {}
        for (const [loc, reason] of Object.entries(fieldErrors(e))) {
          mapped[loc.startsWith('body.') ? loc.slice(5) : loc] = reason
        }
        errors.value = { ...errors.value, ...mapped }
      }
      throw e
    }
  },
  { successMessage: isCreate.value ? '数据源已创建' : '已保存' },
)

async function onSubmit(): Promise<void> {
  const found = validate()
  errors.value = found
  if (Object.keys(found).length > 0) {
    ElMessage.warning('请先修正表单中标红的问题')
    return
  }
  await saveTask.run()
}

function onFormUpdate(next: DataSourceUpsertPayload): void {
  form.value = next
}

function onCredentials(values: Record<string, string>): void {
  credentials.value = values
}

function onResetCredentials(): void {
  credentialsEditing.value = true
}

function onCancel(): void {
  if (isCreate.value) {
    void router.push({ name: 'datasource-list' })
    return
  }
  const id = dsId.value
  if (id === null) {
    void router.push({ name: 'datasource-list' })
    return
  }
  void router.push({ name: 'datasource-detail', params: { id: String(id) } })
}
</script>

<template>
  <div class="ds-edit">
    <PageHeader
      :title="isCreate ? '新建数据源' : '编辑数据源'"
      :description="
        isCreate
          ? '登记连接参数与凭据引用。凭据只以 Vault 引用保存，提交后不可回读。'
          : '修改连接参数不会自动重写凭据；需要轮换请点「重新设置凭据」。'
      "
    >
      <template #actions>
        <el-button :icon="ArrowLeft" @click="onCancel">返回</el-button>
      </template>
      <template #notice>
        <el-alert type="warning" :closable="false" show-icon title="凭据输入即写库，且不可回读">
          <p class="ds-edit__notice">
            输入框里填的是<b>新的凭据</b>（会写入 Vault 的一个新版本），不是当前密码的回显。
            提交成功后平台只保留「Vault 路径 + 版本 + 键名」，明文不会出现在页面、日志或证据账本里。
          </p>
        </el-alert>
      </template>
    </PageHeader>

    <SectionCard>
      <ErrorState
        v-if="loadError !== null"
        :message="loadError"
        title="无法打开数据源"
        @retry="onCancel"
      />

      <div v-else-if="loading" class="elt-skeleton elt-skeleton--block ds-edit__skeleton" />

      <DataSourceForm
        v-else
        :model-value="form"
        :mode="isCreate ? 'create' : 'edit'"
        :submitting="saveTask.loading.value"
        :errors="errors"
        @update:model-value="onFormUpdate"
        @submit="onSubmit"
        @cancel="onCancel"
      >
        <template #credentials>
          <el-divider content-position="left">凭据（Vault 引用）</el-divider>
          <SecretSection
            :secret-ref="secretRef"
            :key-names-hint="credentialKeys"
            :editing="credentialsEditing"
            :can-reset="!isCreate && secretRef !== null"
            :errors="errors"
            @update:credentials="onCredentials"
            @reset-credentials="onResetCredentials"
          />
        </template>
      </DataSourceForm>

      <AsyncTaskPanel
        :loading="saveTask.loading.value"
        :error="saveTask.error.value"
        loading-text="正在保存（凭据写入 Vault）…"
      />
    </SectionCard>
  </div>
</template>

<style scoped>
.ds-edit {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.ds-edit__notice {
  margin: 0;
  line-height: 18px;
}

.ds-edit__skeleton {
  height: 360px;
}
</style>
