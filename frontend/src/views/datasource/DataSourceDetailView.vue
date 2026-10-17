<script setup lang="ts">
/**
 * 数据源详情（fe-03 §5.3 / §5.4 / §6.3 / §6.4）。
 *
 * 本页是「凭据不回显 + 样本已脱敏」两条红线的最主要展示面，因此：
 * - 凭据只显示 Vault 引用（路径 / 版本 / 键名 / 最近轮换），无任何值；
 * - 样本表只渲染后端已脱敏的 `masked_samples`，不提供「查看原文」；
 * - 连接参数（endpoint）里的 host/port/bucket 是非凭据键，按明文展示；
 *   错误文案、连通性 target、探查错误则一律经 SensitiveText——那里更容易夹带连接串。
 */
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ArrowLeft, Edit, RefreshRight } from '@element-plus/icons-vue'
import AsyncTaskPanel from '@/components/common/AsyncTaskPanel.vue'
import ArtifactHash from '@/components/common/ArtifactHash.vue'
import EnvBadge from '@/components/common/EnvBadge.vue'
import ErrorState from '@/components/common/ErrorState.vue'
import PageHeader from '@/components/common/PageHeader.vue'
import SectionCard from '@/components/common/SectionCard.vue'
import SensitiveText from '@/components/common/SensitiveText.vue'
import StatusTag from '@/components/common/StatusTag.vue'
import { useAsyncTask } from '@/composables/useAsyncTask'
import { usePermission } from '@/composables/usePermission'
import { usePollingWithDeadline } from '@/composables/usePolling'
import { useDataSourceStore } from '@/stores/datasource'
import * as datasourceApi from '@/api/datasource'
import { toDisplayMessage } from '@/utils/errors'
import { formatDateTime, formatInt, formatRemaining, remainingMs, textOrDash } from '@/utils/format'
import { dataSourceTypeSpec } from '@/types'
import ConnectivityTestPanel from './components/ConnectivityTestPanel.vue'
import DataSourceActionsBar from './components/DataSourceActionsBar.vue'
import FieldDetailPanel from './components/FieldDetailPanel.vue'
import MaskedSampleTable from './components/MaskedSampleTable.vue'
import ProbeForm from './components/ProbeForm.vue'
import ProbeStatusTag from './components/ProbeStatusTag.vue'
import ProfileVersionList from './components/ProfileVersionList.vue'
import SchemaTree from './components/SchemaTree.vue'
import SecretSection from './components/SecretSection.vue'
import type {
  DataSourceStatus,
  ProbeRequest,
  SchemaColumn,
  SchemaSnapshot,
  SchemaTable,
} from '@/types'

const route = useRoute()
const router = useRouter()
const store = useDataSourceStore()
const { isMaker } = usePermission()

const dsId = computed<number | null>(() => {
  const raw = route.params.id
  const value = Number.parseInt(typeof raw === 'string' ? raw : '', 10)
  return Number.isFinite(value) && value > 0 ? value : null
})

const detail = computed(() => store.detail)
const spec = computed(() => dataSourceTypeSpec(detail.value?.type ?? ''))
const connectable = computed<boolean>(() => spec.value.connectable)
const isProd = computed<boolean>(() => detail.value?.env === 'prod')
const isDisabled = computed<boolean>(() => detail.value?.status === 'disabled')
/** 只有「已加载完成且确实没有详情」才算错误；加载中不能显示为不存在 */
const showError = computed<boolean>(
  () => !store.detailLoading && store.detail === null && store.detailError !== null,
)
const conflictMessage = ref<string | null>(null)

/** DataSourceStatus 未登记进 @/utils/enums（EnumKind 缺项），文案暂由本页持有 */
const STATUS_META: Record<DataSourceStatus, { label: string; tag: 'success' | 'info' }> = {
  active: { label: '启用中', tag: 'success' },
  disabled: { label: '已禁用', tag: 'info' },
}
/** SamplingScope 同样未登记，文案与 ProbeForm 保持一致 */
const SCOPE_LABELS: Record<string, string> = {
  full_tables: '全表',
  selected_tables: '指定表',
  schema_only: '仅结构',
}

const endpointEntries = computed<Array<{ key: string; value: string }>>(() =>
  Object.entries(detail.value?.endpoint ?? {}).map(([key, value]) => ({
    key,
    value: String(value),
  })),
)

/* ------------------------------------------------------------------ */
/* 探查结果                                                            */
/* ------------------------------------------------------------------ */

const SAMPLE_LIMIT = 50

const schema = ref<SchemaSnapshot | null>(null)
const schemaLoading = ref(false)
const schemaError = ref<string | null>(null)

const selectedTable = ref<SchemaTable | null>(null)
const selectedColumn = ref<SchemaColumn | null>(null)

const samples = ref<{ rows: Array<Record<string, string>>; truncated: boolean } | null>(null)
const samplesLoading = ref(false)
const samplesError = ref<string | null>(null)

const activeProfile = computed(() => store.activeProfile)
const probing = computed<boolean>(() => activeProfile.value?.status === 'probing')
const busyProbe = computed<boolean>(() => store.probingVersion !== null)

const sampleColumns = computed<string[]>(
  () => selectedTable.value?.columns.map((c) => c.name) ?? [],
)

const maskingSummary = computed<string>(() => {
  const policy = activeProfile.value?.masking_policy
  if (policy === null || policy === undefined) {
    return '—'
  }
  const def = typeof policy['default'] === 'string' ? (policy['default'] as string) : '—'
  const overrides = policy['overrides']
  const count =
    overrides !== null && typeof overrides === 'object' && !Array.isArray(overrides)
      ? Object.keys(overrides as Record<string, unknown>).length
      : 0
  return count > 0 ? `${def}（另有 ${count} 个字段级覆盖）` : def
})

const samplingSummary = computed<string>(() => {
  const sampling = activeProfile.value?.sampling
  if (sampling === null || sampling === undefined) {
    return '—'
  }
  const scope = SCOPE_LABELS[sampling.scope] ?? sampling.scope
  if (sampling.scope === 'schema_only') {
    return `${scope}（不采样数据）`
  }
  const tables = sampling.tables ?? []
  const tableText = tables.length > 0 ? `，指定 ${tables.length} 张表` : ''
  return `${scope}，每表最多 ${formatInt(sampling.rows)} 行${tableText}`
})

async function loadSchema(version: number): Promise<void> {
  const id = dsId.value
  if (id === null) {
    return
  }
  schemaLoading.value = true
  schemaError.value = null
  try {
    schema.value = await datasourceApi.fetchSchema(id, version)
  } catch (e) {
    schema.value = null
    schemaError.value = toDisplayMessage(e)
  } finally {
    schemaLoading.value = false
  }
}

async function loadSamples(table: SchemaTable): Promise<void> {
  const id = dsId.value
  const version = store.activeProfileVersion
  if (id === null || version === null) {
    return
  }
  // Profile 里已经带了脱敏样本就直接用：少一次请求，也保证与统计口径同一批数据
  const inline = activeProfile.value?.masked_samples[table.name]
  if (Array.isArray(inline)) {
    samples.value = { rows: inline, truncated: false }
    samplesError.value = null
    return
  }

  samplesLoading.value = true
  samplesError.value = null
  try {
    samples.value = await datasourceApi.fetchSamples(id, version, table.name, SAMPLE_LIMIT)
  } catch (e) {
    samples.value = null
    samplesError.value = toDisplayMessage(e)
  } finally {
    samplesLoading.value = false
  }
}

async function selectVersion(version: number): Promise<void> {
  const id = dsId.value
  if (id === null) {
    return
  }
  selectedTable.value = null
  selectedColumn.value = null
  samples.value = null
  await store.loadProfile(id, version)
  await loadSchema(version)
}

async function onSelectTable(table: SchemaTable): Promise<void> {
  selectedTable.value = table
  selectedColumn.value = null
  samples.value = null
  await loadSamples(table)
}

function onSelectField(payload: { table: SchemaTable; column: SchemaColumn }): void {
  selectedTable.value = payload.table
  selectedColumn.value = payload.column
}

/* ------------------------------------------------------------------ */
/* 探查轮询                                                            */
/* ------------------------------------------------------------------ */

/** 轮询超上限后置位：把「还在跑」与「跑丢了」区分开，否则用户会一直盯着进度条 */
const probeStalled = ref(false)

const poller = usePollingWithDeadline(
  async () => {
    const id = dsId.value
    const version = store.activeProfileVersion
    if (id === null || version === null) {
      return
    }
    await store.loadProfile(id, version)
  },
  {
    intervalMs: 2000,
    immediate: false,
    // 5 分钟上限：探查要过连接、读 Schema、采样、脱敏、落库五步，超时说明后端出了问题
    maxDurationMs: 300_000,
    shouldStop: () => store.activeProfile?.status !== 'probing',
    onDeadline: () => {
      probeStalled.value = true
    },
  },
)

watch(
  () => activeProfile.value?.status,
  (status) => {
    if (status === 'probing') {
      probeStalled.value = false
      poller.start()
      return
    }
    poller.stop()
    // 探查刚出结果：表结构要跟着换到新版本，否则用户看到的是上一版的结构
    if (status === 'ready') {
      const version = store.activeProfileVersion
      const id = dsId.value
      if (version !== null && id !== null) {
        void loadSchema(version)
        void store.loadProfiles(id)
      }
    }
  },
)

/* ------------------------------------------------------------------ */
/* 探查提交                                                            */
/* ------------------------------------------------------------------ */

const probeOpen = ref(false)
const probeError = ref<string | null>(null)
const probeRequest = ref<ProbeRequest>({
  sampling: { rows: 1000, scope: 'full_tables', tables: null },
  masking_policy: { default: 'mask_middle', overrides: {} },
})

const tableOptions = computed<string[]>(() => schema.value?.tables.map((t) => t.name) ?? [])

const probeTask = useAsyncTask(
  async () => {
    const id = dsId.value
    if (id === null) {
      throw new Error('缺少数据源编号')
    }
    const started = await datasourceApi.startProbe(id, probeRequest.value)
    probeOpen.value = false
    probeStalled.value = false
    // refreshAfterProbe 会拉新版本并触发 watch → poller.start()，无需在这里自己轮询
    await store.refreshAfterProbe(id, started.version)
  },
  {},
)

function openProbe(): void {
  probeError.value = null
  probeOpen.value = true
}

function onSubmitProbe(): void {
  const sampling = probeRequest.value.sampling
  if (sampling.scope === 'selected_tables' && (sampling.tables ?? []).length === 0) {
    probeError.value = '采样范围为「指定表」时，至少需要选择一张表'
    return
  }
  probeError.value = null
  void probeTask.run()
}

/* ------------------------------------------------------------------ */
/* 生命周期                                                            */
/* ------------------------------------------------------------------ */

async function refreshAll(): Promise<void> {
  const id = dsId.value
  if (id === null) {
    return
  }
  await store.loadDetail(id)
  if (store.detail === null) {
    return
  }
  await store.loadProfiles(id)
  const latest = store.profiles.reduce<number | null>(
    (acc, p) => (acc === null || p.version > acc ? p.version : acc),
    null,
  )
  if (latest !== null) {
    await store.loadProfile(id, latest)
    await loadSchema(latest)
  }
}

onMounted(async () => {
  await refreshAll()

  // 探查中进入本页（或新建后回跳）要立刻接着轮询
  if (activeProfile.value?.status === 'probing') {
    poller.start()
  }

  // 新建成功后带 ?test=1 回跳：由 ConnectivityTestPanel 自己跑一次连通性测试。
  // 标记在挂载时就读进 autoTest（ref 而非 computed）：否则下面清参数会让它瞬间变 false
  await nextTick()
  if (autoTest.value) {
    stripQuery('test')
  }
  // 列表页「立即探查」带 ?probe=1 进来：直接打开探查弹窗并清掉参数，避免刷新时反复弹
  if (route.query.probe === '1') {
    openProbe()
    stripQuery('probe')
  }
})

function stripQuery(key: string): void {
  if (route.query[key] === undefined) {
    return
  }
  const query = { ...route.query }
  delete query[key]
  void router.replace({ query })
}

/** 挂载瞬间的取值：URL 里的标记会被清掉，但「这次要自动测一次」的意图要留住 */
const autoTest = ref<boolean>(route.query.test === '1')

/* ------------------------------------------------------------------ */
/* 其它交互                                                            */
/* ------------------------------------------------------------------ */

const reloadTask = useAsyncTask(async () => {
  await refreshAll()
}, {})

function onEdit(): void {
  const id = dsId.value
  if (id === null) {
    return
  }
  void router.push({ name: 'datasource-edit', params: { id: String(id) } })
}

function onRemoved(): void {
  void router.replace({ name: 'datasource-list' })
}

function onChanged(): void {
  void refreshAll()
}

function onConflict(message: string): void {
  conflictMessage.value = message
}

function onTested(): void {
  // 测试结果会落库（last_tested_at / last_test_ok），重新读一次详情让页头与卡片一致
  const id = dsId.value
  if (id !== null) {
    void store.loadDetail(id)
  }
}

function onRetrySamples(): void {
  if (selectedTable.value !== null) {
    void loadSamples(selectedTable.value)
  }
}

function expiresText(expiresAt: string | null): string {
  const ms = remainingMs(expiresAt)
  if (ms === null) {
    return '不过期'
  }
  return ms <= 0 ? `${formatDateTime(expiresAt)}（已过期）` : `剩余 ${formatRemaining(ms)}`
}
</script>

<template>
  <div class="ds-detail">
    <PageHeader
      :title="detail?.name ?? '数据源详情'"
      :description="detail === null ? undefined : `${spec.label} · ${endpointEntries.map((e) => e.value).join(' / ')}`"
    >
      <template #actions>
        <el-button :icon="ArrowLeft" @click="router.push({ name: 'datasource-list' })">返回列表</el-button>
        <el-button :icon="RefreshRight" :loading="reloadTask.loading.value" @click="reloadTask.run()">
          刷新
        </el-button>
        <el-button v-if="isMaker && detail !== null" type="primary" :icon="Edit" @click="onEdit">编辑</el-button>
        <DataSourceActionsBar
          v-if="isMaker && detail !== null"
          :data-source="detail"
          variant="buttons"
          @conflict="onConflict"
          @removed="onRemoved"
          @changed="onChanged"
        />
      </template>
    </PageHeader>

    <el-alert
      v-if="conflictMessage !== null"
      type="error"
      show-icon
      :closable="true"
      :title="conflictMessage"
      @close="conflictMessage = null"
    />
    <el-alert
      v-if="isProd"
      type="warning"
      :closable="false"
      show-icon
      title="这是生产环境（prod）的数据源"
      description="引用它生成的方案会走更严格的审批；连通性测试与探查仍是只读操作，不会改动源端数据。"
    />
    <el-alert
      v-if="isDisabled"
      type="info"
      :closable="false"
      show-icon
      title="该数据源已禁用"
      description="不能用于新的方案生成与执行；已开始的运行不受影响，可重新启用。"
    />

    <SectionCard v-if="store.detailLoading && detail === null" :skeleton-height="200" loading>
      <div />
    </SectionCard>

    <ErrorState
      v-else-if="showError"
      :message="store.detailError"
      title="数据源加载失败"
      @retry="refreshAll"
    />

    <!-- 加载结束、没有错误、也没有详情：只可能是 URL 里的编号无效，别留一块白屏 -->
    <ErrorState
      v-else-if="detail === null && !store.detailLoading"
      message="URL 中的数据源编号无效，或该数据源已被删除。"
      title="数据源不存在"
      :retryable="false"
    />

    <template v-else-if="detail !== null">
      <SectionCard title="基本信息">
        <template #extra>
          <el-button :icon="RefreshRight" link size="small" @click="refreshAll">重新读取</el-button>
        </template>
        <dl class="ds-detail__list">
          <div class="ds-detail__row">
            <dt>类型</dt>
            <dd>
              <StatusTag kind="DataSourceType" :value="detail.type" />
              <span v-if="!connectable" class="ds-detail__muted">仅登记接口，不支持测试与探查</span>
            </dd>
          </div>
          <div class="ds-detail__row">
            <dt>环境 / 敏感级别</dt>
            <dd>
              <EnvBadge :env="detail.env" />
              <StatusTag kind="SensitiveLevel" :value="detail.sensitive_level" />
            </dd>
          </div>
          <div class="ds-detail__row">
            <dt>状态</dt>
            <dd>
              <el-tag size="small" :type="STATUS_META[detail.status].tag">
                {{ STATUS_META[detail.status].label }}
              </el-tag>
            </dd>
          </div>
          <div class="ds-detail__row">
            <dt>负责人</dt>
            <dd>{{ textOrDash(detail.owner_display_name) }}</dd>
          </div>
          <div class="ds-detail__row">
            <dt>标签</dt>
            <dd>{{ detail.tags.length > 0 ? detail.tags.join(' / ') : '无' }}</dd>
          </div>
          <div class="ds-detail__row">
            <dt>创建 / 更新</dt>
            <dd>{{ formatDateTime(detail.created_at) }} / {{ formatDateTime(detail.updated_at) }}</dd>
          </div>
        </dl>
      </SectionCard>

      <SectionCard title="连接参数与凭据引用">
        <dl class="ds-detail__list">
          <div v-for="entry in endpointEntries" :key="entry.key" class="ds-detail__row">
            <dt class="ds-detail__mono">{{ entry.key }}</dt>
            <!-- endpoint 的非凭据键按明文展示：它们本就用于人工核对连接目标，
                 而凭据键在提交前已被黑名单拦住，不会出现在这里 -->
            <dd class="ds-detail__mono">{{ entry.value }}</dd>
          </div>
        </dl>
        <el-divider />
        <SecretSection
          :secret-ref="detail.secret_ref"
          :key-names-hint="detail.secret_ref?.key_names ?? []"
          :editing="false"
        />
      </SectionCard>

      <SectionCard title="连通性测试">
        <ConnectivityTestPanel
          :datasource-id="detail.id"
          :last-tested-at="detail.last_tested_at"
          :last-test-ok="detail.last_test_ok"
          :connectable="connectable"
          :auto-run="autoTest"
          variant="full"
          @tested="onTested"
        />
      </SectionCard>

      <SectionCard title="元数据探查">
        <template #extra>
          <el-button
            v-if="isMaker"
            type="primary"
            size="small"
            :disabled="!connectable || busyProbe"
            @click="openProbe"
          >{{ busyProbe ? '探查进行中' : '发起探查' }}</el-button>
        </template>

        <p class="ds-detail__hint">
          探查是只读操作：不修改源端数据，采样值在后端入库前完成脱敏。
          元数据有有效期，过期后需重新探查才能生成方案。
        </p>

        <div v-if="probing" class="ds-detail__probing">
          <el-progress :percentage="100" indeterminate :duration="2" :show-text="false" :stroke-width="4" />
          <p class="ds-detail__hint">
            探查进行中：连接 → 读取 Schema → 采样 → 脱敏 → 落库。完成后本页自动刷新（每 2 秒轮询）。
          </p>
          <el-alert
            v-if="probeStalled"
            type="warning"
            :closable="false"
            show-icon
            title="探查已超过 5 分钟仍未完成，已停止自动刷新"
            description="后端任务可能已失败或卡住。可手动刷新查看最新状态；持续如此请带 request_id 联系管理员。"
          >
          </el-alert>
        </div>

        <div v-if="activeProfile !== null" class="ds-detail__profile">
          <dl class="ds-detail__list">
            <div class="ds-detail__row">
              <dt>当前版本</dt>
              <dd>
                <span class="ds-detail__mono">v{{ activeProfile.version }}</span>
                <ProbeStatusTag :status="activeProfile.status" :expires-at="activeProfile.expires_at" />
              </dd>
            </div>
            <div class="ds-detail__row">
              <dt>表数 / 快照哈希</dt>
              <dd>
                <span>{{ formatInt(activeProfile.tables_count) }} 张表</span>
                <ArtifactHash :hash="activeProfile.sha256" display="truncate" copyable label="快照哈希" />
              </dd>
            </div>
            <div class="ds-detail__row">
              <dt>采样范围</dt>
              <dd>{{ samplingSummary }}</dd>
            </div>
            <div class="ds-detail__row">
              <dt>脱敏策略</dt>
              <dd>{{ maskingSummary }}</dd>
            </div>
            <div class="ds-detail__row">
              <dt>有效期</dt>
              <dd>{{ expiresText(activeProfile.expires_at) }}</dd>
            </div>
            <div class="ds-detail__row">
              <dt>探查时间</dt>
              <dd>{{ formatDateTime(activeProfile.created_at) }} · {{ textOrDash(activeProfile.created_by_display_name) }}</dd>
            </div>
          </dl>
          <p v-if="activeProfile.error_message !== null" class="ds-detail__error">
            <SensitiveText :value="activeProfile.error_message" multiline />
          </p>
        </div>

        <el-divider />

        <p class="ds-detail__hint">历史版本（生成方案时引用的是具体版本号）：</p>
        <ProfileVersionList
          :profiles="store.profiles"
          :loading="store.profilesLoading"
          :error="store.profilesError"
          :active-version="store.activeProfileVersion"
          @select="selectVersion"
        />
      </SectionCard>

      <SectionCard title="探查结果">
        <div class="ds-detail__result">
          <div class="ds-detail__tree">
            <SchemaTree
              :snapshot="schema"
              :loading="schemaLoading"
              :error="schemaError"
              :selected-table-name="selectedTable?.name ?? null"
              @select-table="onSelectTable"
              @select-field="onSelectField"
            />
          </div>
          <div class="ds-detail__field">
            <FieldDetailPanel :column="selectedColumn" :table-name="selectedTable?.name ?? null" />
          </div>
        </div>

        <el-divider />

        <template v-if="selectedTable !== null">
          <p class="ds-detail__hint">
            表 <span class="ds-detail__mono">{{ selectedTable.name }}</span> 的脱敏样本：
          </p>
          <el-skeleton v-if="samplesLoading" :rows="4" animated />
          <ErrorState
            v-else-if="samplesError !== null"
            :message="samplesError"
            title="样本加载失败"
            @retry="onRetrySamples"
          />
          <MaskedSampleTable
            v-else
            :columns="sampleColumns"
            :rows="samples?.rows ?? []"
            :truncated="samples?.truncated ?? false"
            :limit="SAMPLE_LIMIT"
          />
        </template>
        <p v-else class="ds-detail__hint">在左侧表结构中点选一张表，查看它的字段与脱敏样本。</p>
      </SectionCard>
    </template>

    <el-dialog v-model="probeOpen" title="发起元数据探查" width="680px" :close-on-click-modal="false">
      <ProbeForm
        v-model="probeRequest"
        :submitting="probeTask.loading.value"
        :connectable="connectable"
        :table-options="tableOptions"
        @submit="onSubmitProbe"
        @cancel="probeOpen = false"
      />
      <el-alert v-if="probeError !== null" type="warning" :closable="false" show-icon :title="probeError" />
      <AsyncTaskPanel
        :loading="probeTask.loading.value"
        :error="probeTask.error.value"
        loading-text="正在提交探查…"
      />
    </el-dialog>
  </div>
</template>

<style scoped>
.ds-detail {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.ds-detail__list {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin: 0;
}

.ds-detail__row {
  display: flex;
  gap: 10px;
  align-items: baseline;
  font-size: 12px;
  line-height: 18px;
}

.ds-detail__row dt {
  flex: 0 0 130px;
  color: var(--elt-text-muted);
}

.ds-detail__row dd {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  align-items: baseline;
  min-width: 0;
  margin: 0;
  word-break: break-all;
}

.ds-detail__mono {
  font-family: var(--elt-font-mono);
}

.ds-detail__muted,
.ds-detail__hint {
  font-size: 11px;
  line-height: 16px;
  color: var(--elt-text-muted);
}

.ds-detail__hint {
  margin: 0 0 8px;
}

.ds-detail__error {
  padding: 8px 10px;
  margin: 8px 0 0;
  background: var(--elt-bg-elevated);
  border-radius: var(--elt-radius-control);
}

.ds-detail__probing {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin-bottom: 8px;
}

.ds-detail__profile {
  margin-bottom: 4px;
}

.ds-detail__result {
  display: flex;
  gap: 16px;
  align-items: flex-start;
}

.ds-detail__tree {
  flex: 1 1 60%;
  min-width: 0;
  max-height: 420px;
  overflow: auto;
}

.ds-detail__field {
  flex: 1 1 40%;
  min-width: 0;
}
</style>
