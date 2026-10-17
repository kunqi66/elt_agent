<script setup lang="ts">
/**
 * 数据源列表（fe-03 §6.2）。
 *
 * 两条红线在这个页面上最容易被破坏，故在此显式声明：
 * 1. 密钥：列表只显示 `secret_ref` 的存在性（有无凭据），**不显示**任何键值；
 * 2. 脱敏：该页所有值都不是样本原文（样本只在详情页，且已脱敏）。
 *
 * 筛选状态写进 URL（刷新、分享、从详情页返回都能还原）。
 * 只做「URL ← 筛选」单向同步：反向同步会在 router.replace 与 watcher 之间形成回环。
 */
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Plus, Refresh, Search } from '@element-plus/icons-vue'
import EmptyState from '@/components/common/EmptyState.vue'
import EnvBadge from '@/components/common/EnvBadge.vue'
import ErrorState from '@/components/common/ErrorState.vue'
import PageHeader from '@/components/common/PageHeader.vue'
import QueryTable from '@/components/common/QueryTable.vue'
import SectionCard from '@/components/common/SectionCard.vue'
import StatusTag from '@/components/common/StatusTag.vue'
import { usePermission } from '@/composables/usePermission'
import { useDataSourceStore } from '@/stores/datasource'
import { useProjectStore } from '@/stores/project'
import { toDisplayMessage } from '@/utils/errors'
import { enumOptions } from '@/utils/enums'
import { formatDateTime, truncate } from '@/utils/format'
import { DATA_SOURCE_TYPES } from '@/types'
import ConnectivityTestPanel from './components/ConnectivityTestPanel.vue'
import DataSourceActionsBar from './components/DataSourceActionsBar.vue'
import ProbeStatusTag from './components/ProbeStatusTag.vue'
import type { DataSource, DataSourceStatus, DataSourceType, Env, SensitiveLevel } from '@/types'

const route = useRoute()
const router = useRouter()
const store = useDataSourceStore()
const projectStore = useProjectStore()
const { isMaker } = usePermission()

/** 首屏：项目上下文拉取失败要显示错误态，绝不能退化成空列表 */
const bootError = ref<string | null>(null)
const booting = ref(true)
const conflictMessage = ref<string | null>(null)
/** 关键字输入框的本地值：按回车/点搜索才写进 filter，避免每敲一个字打一次请求 */
const keyword = ref('')

const noProject = computed<boolean>(() => projectStore.currentProjectId === null)
const hasActiveFilter = computed<boolean>(() => {
  const f = store.query.filter
  return (
    (f.type?.length ?? 0) > 0 ||
    (f.env?.length ?? 0) > 0 ||
    (f.sensitive_level?.length ?? 0) > 0 ||
    f.status !== undefined ||
    (f.q !== undefined && f.q !== '')
  )
})

const typeOptions = DATA_SOURCE_TYPES.map((s) => ({ value: s.type as string, label: s.label }))
const envOptions = enumOptions('Env')
const levelOptions = enumOptions('SensitiveLevel')
/**
 * DataSourceStatus 没有登记进 `@/utils/enums`（EnumKind 里没有这一项），
 * 故状态文案与标签色暂由本页持有——基座补登记后应改走 enumText，避免两处口径。
 */
const STATUS_META: Record<DataSourceStatus, { label: string; tag: 'success' | 'info' }> = {
  active: { label: '启用中', tag: 'success' },
  disabled: { label: '已禁用', tag: 'info' },
}
const statusOptions = (Object.keys(STATUS_META) as DataSourceStatus[]).map((value) => ({
  value,
  label: STATUS_META[value].label,
}))

/* ------------------------------------------------------------------ */
/* URL ↔ 筛选                                                          */
/* ------------------------------------------------------------------ */

function parseCsv(value: unknown): string[] {
  if (typeof value !== 'string' || value === '') {
    return []
  }
  return value
    .split(',')
    .map((s) => s.trim())
    .filter((s) => s !== '')
}

function parsePage(value: unknown, fallback: number): number {
  const n = Number.parseInt(typeof value === 'string' ? value : '', 10)
  return Number.isFinite(n) && n > 0 ? n : fallback
}

/** 从 URL 还原筛选；非法取值一律丢弃而不是原样带进请求（后端会 422 整个列表） */
function hydrateFromQuery(): void {
  const filter = store.query.filter
  const type = parseCsv(route.query.type).filter((t) =>
    DATA_SOURCE_TYPES.some((s) => s.type === t),
  ) as DataSourceType[]
  const env = parseCsv(route.query.env).filter(
    (e): e is Env => e === 'dev' || e === 'staging' || e === 'prod',
  )
  const levels = parseCsv(route.query.sensitive_level).filter(
    (l): l is SensitiveLevel =>
      l === 'public' || l === 'internal' || l === 'confidential' || l === 'restricted',
  )
  const status = parseCsv(route.query.status)[0]
  const q = parseCsv(route.query.q)[0] ?? ''

  filter.type = type.length > 0 ? type : undefined
  filter.env = env.length > 0 ? env : undefined
  filter.sensitive_level = levels.length > 0 ? levels : undefined
  filter.status = status === 'active' || status === 'disabled' ? status : undefined
  filter.q = q === '' ? undefined : q
  keyword.value = q
  filter.page = parsePage(route.query.page, 1)
  const size = parsePage(route.query.page_size, 20)
  filter.page_size = [10, 20, 50, 100].includes(size) ? size : 20
}

const filterSignature = computed<string>(() => {
  const f = store.query.filter
  return [
    f.type?.join(',') ?? '',
    f.env?.join(',') ?? '',
    f.sensitive_level?.join(',') ?? '',
    f.status ?? '',
    f.q ?? '',
    String(f.page),
    String(f.page_size),
  ].join('|')
})

function syncToQuery(): void {
  const f = store.query.filter
  const query: Record<string, string> = {}
  if ((f.type?.length ?? 0) > 0) {
    query.type = (f.type ?? []).join(',')
  }
  if ((f.env?.length ?? 0) > 0) {
    query.env = (f.env ?? []).join(',')
  }
  if ((f.sensitive_level?.length ?? 0) > 0) {
    query.sensitive_level = (f.sensitive_level ?? []).join(',')
  }
  if (f.status !== undefined) {
    query.status = f.status
  }
  if (f.q !== undefined && f.q !== '') {
    query.q = f.q
  }
  if (f.page > 1) {
    query.page = String(f.page)
  }
  if (f.page_size !== 20) {
    query.page_size = String(f.page_size)
  }
  // replace 而不是 push：筛选变化不该在浏览器历史里堆出几十条记录
  void router.replace({ query })
}

watch(filterSignature, () => {
  syncToQuery()
})

/* ------------------------------------------------------------------ */
/* 交互                                                                */
/* ------------------------------------------------------------------ */

onMounted(async () => {
  hydrateFromQuery()
  try {
    await projectStore.ensureLoaded()
  } catch (e) {
    bootError.value = toDisplayMessage(e)
    booting.value = false
    return
  }
  if (projectStore.currentProjectId !== null) {
    await store.query.load()
  }
  booting.value = false
})

function onSearch(): void {
  const q = keyword.value.trim()
  // 走 store 的 filter + search()：search 会把页码复位到 1，否则在第 3 页改关键字会「空列表」
  store.query.filter.q = q === '' ? undefined : q
  void store.query.search()
}

function onPatch(patch: Partial<typeof store.query.filter>): void {
  void store.query.patchFilter(patch)
}

function onTypeChange(value: unknown): void {
  const list = Array.isArray(value) ? (value as string[]) : []
  onPatch({ type: list.length > 0 ? (list as DataSourceType[]) : undefined })
}

function onEnvChange(value: unknown): void {
  const list = Array.isArray(value) ? (value as string[]) : []
  onPatch({ env: list.length > 0 ? (list as Env[]) : undefined })
}

function onLevelChange(value: unknown): void {
  const list = Array.isArray(value) ? (value as string[]) : []
  onPatch({ sensitive_level: list.length > 0 ? (list as SensitiveLevel[]) : undefined })
}

function onResetFilter(): void {
  keyword.value = ''
  conflictMessage.value = null
  void store.query.reset()
}

function onReload(): void {
  conflictMessage.value = null
  void store.query.reload()
}

function onCreate(): void {
  void router.push({ name: 'datasource-create' })
}

function onOpenDetail(row: DataSource, probe: boolean): void {
  void router.push({
    name: 'datasource-detail',
    params: { id: String(row.id) },
    query: probe ? { probe: '1' } : {},
  })
}

function onEdit(row: DataSource): void {
  void router.push({ name: 'datasource-edit', params: { id: String(row.id) } })
}

function onPageChange(page: number): void {
  void store.query.setPage(page)
}

function onPageSizeChange(size: number): void {
  void store.query.setPageSize(size)
}

function onTested(): void {
  // 测试结果会落库（last_tested_at / last_test_ok），刷新当前页让列表与后端一致
  void store.query.reload()
}

function onConflict(message: string): void {
  conflictMessage.value = message
}

/**
 * el-table 插槽的 `row` 静态类型是 Element Plus 的 `DefaultRow`，与我们的 DTO 无类型关联。
 *
 * 直接读 `row.xxx` 不会报错（`DefaultRow` 是索引签名类型，读到的是宽类型），
 * 但一旦把 `row` **作为参数**传给 `DataSource` 签名的函数就会失败。
 * 与其在每个函数签名上放宽类型（那会让整个文件的类型检查一起退化），
 * 不如在调用点显式收口——出错的只会是真正需要把 row 当 DataSource 用的那几处。
 */
function dsOf(row: unknown): DataSource {
  return row as DataSource
}

/** 连通性测试通过才有必要探查；未测试过的给一次机会而不是直接禁用 */
function canProbe(row: DataSource): boolean {
  const spec = DATA_SOURCE_TYPES.find((s) => s.type === row.type)
  return spec?.connectable === true
}

function credentialText(row: DataSource): string {
  const ref = row.secret_ref
  if (ref === null) {
    return '无凭据'
  }
  return `Vault v${ref.version}`
}
</script>

<template>
  <div class="ds-list">
    <PageHeader
      title="数据源"
      description="登记源端/目标端的连接信息与凭据引用；凭据只以 Vault 引用保存，探查样本一律脱敏。"
    >
      <template #notice>
        <el-alert type="info" :closable="false" show-icon title="密钥与脱敏红线">
          <p class="ds-list__notice">
            平台不保存、不回显凭据明文：列表只显示 Vault 引用是否存在；
            元数据样本在后端入库前完成脱敏，界面不提供任何「查看原文」入口。
          </p>
        </el-alert>
      </template>
      <template #actions>
        <el-button :icon="Refresh" :loading="store.query.loading" @click="onReload">刷新</el-button>
        <el-button v-if="isMaker" type="primary" :icon="Plus" @click="onCreate">新建连接</el-button>
      </template>
    </PageHeader>

    <el-alert
      v-if="conflictMessage !== null"
      type="error"
      show-icon
      class="ds-list__conflict"
      :title="conflictMessage"
      @close="conflictMessage = null"
    />

    <SectionCard title="筛选" class="ds-list__filter-card">
      <div class="ds-list__filters">
        <el-input
          v-model="keyword"
          class="ds-list__keyword"
          placeholder="按名称 / 标签搜索"
          clearable
          :prefix-icon="Search"
          @keyup.enter="onSearch"
          @clear="onSearch"
        />
        <el-select
          :model-value="store.query.filter.type ?? []"
          multiple
          collapse-tags
          clearable
          placeholder="类型"
          class="ds-list__select"
          @update:model-value="onTypeChange"
        >
          <el-option v-for="o in typeOptions" :key="o.value" :value="o.value" :label="o.label" />
        </el-select>
        <el-select
          :model-value="store.query.filter.env ?? []"
          multiple
          collapse-tags
          clearable
          placeholder="环境"
          class="ds-list__select ds-list__select--narrow"
          @update:model-value="onEnvChange"
        >
          <el-option v-for="o in envOptions" :key="o.value" :value="o.value" :label="o.label" />
        </el-select>
        <el-select
          :model-value="store.query.filter.sensitive_level ?? []"
          multiple
          collapse-tags
          clearable
          placeholder="敏感级别"
          class="ds-list__select"
          @update:model-value="onLevelChange"
        >
          <el-option v-for="o in levelOptions" :key="o.value" :value="o.value" :label="o.label" />
        </el-select>
        <el-select
          :model-value="store.query.filter.status ?? ''"
          clearable
          placeholder="状态"
          class="ds-list__select ds-list__select--narrow"
          @update:model-value="(v: string) => onPatch({ status: v === '' ? undefined : (v as DataSourceStatus) })"
        >
          <el-option v-for="o in statusOptions" :key="o.value" :value="o.value" :label="o.label" />
        </el-select>
        <el-button type="primary" :icon="Search" @click="onSearch">搜索</el-button>
        <el-button :disabled="!hasActiveFilter" @click="onResetFilter">重置</el-button>
      </div>
    </SectionCard>

    <SectionCard :padded="false" class="ds-list__table-card">
      <ErrorState
        v-if="bootError !== null"
        :message="bootError"
        title="项目上下文加载失败"
        @retry="onReload"
      />

      <EmptyState
        v-else-if="noProject && !booting"
        variant="no-data"
        title="尚未选择项目"
        description="数据源按项目隔离。请先在页面顶部切换到目标项目，再查看该项目的连接。"
      />

      <QueryTable
        v-else
        :items="store.query.items"
        :loading="store.query.loading"
        :initial-loading="store.query.initialLoading"
        :error="store.query.error"
        :request-id="store.query.errorRequestId"
        :total="store.query.total"
        :page="store.query.filter.page"
        :page-size="store.query.filter.page_size"
        :filtered="hasActiveFilter"
        row-key="id"
        :min-height="320"
        empty-text="该项目还没有数据源"
        empty-description="登记一个源端或目标端连接后，才能在方案生成里引用它。"
        @retry="onReload"
        @page-change="onPageChange"
        @page-size-change="onPageSizeChange"
      >
        <template #empty-actions>
          <el-button v-if="isMaker" type="primary" :icon="Plus" @click="onCreate">新建连接</el-button>
        </template>

        <el-table-column label="名称" min-width="200">
          <template #default="{ row }">
            <el-button link type="primary" @click="onOpenDetail(dsOf(row), false)">
              {{ truncate(row.name, 40) }}
            </el-button>
            <div class="ds-list__sub">
              <span class="ds-list__mono">{{ credentialText(dsOf(row)) }}</span>
              <span v-if="row.tags.length > 0" class="ds-list__tags">
                {{ (row.tags as string[]).join(' / ') }}
              </span>
            </div>
          </template>
        </el-table-column>

        <el-table-column label="类型" width="110">
          <template #default="{ row }">
            <StatusTag kind="DataSourceType" :value="row.type" />
          </template>
        </el-table-column>

        <el-table-column label="环境" width="90">
          <template #default="{ row }"><EnvBadge :env="row.env" /></template>
        </el-table-column>

        <el-table-column label="敏感级别" width="120">
          <template #default="{ row }">
            <StatusTag kind="SensitiveLevel" :value="row.sensitive_level" />
          </template>
        </el-table-column>

        <el-table-column label="负责人" width="130">
          <template #default="{ row }">{{ row.owner_display_name ?? '未指定' }}</template>
        </el-table-column>

        <el-table-column label="状态" width="96">
          <template #default="{ row }">
            <el-tag size="small" :type="STATUS_META[row.status as DataSourceStatus].tag">
              {{ STATUS_META[row.status as DataSourceStatus].label }}
            </el-tag>
          </template>
        </el-table-column>

        <el-table-column label="最近探查" width="190">
          <template #default="{ row }">
            <template v-if="row.latest_profile !== null">
              <ProbeStatusTag :status="row.latest_profile.status" :expires-at="row.latest_profile.expires_at" />
              <div class="ds-list__sub">
                <span class="ds-list__mono">v{{ row.latest_profile.version }}</span>
                <span>{{ formatDateTime(row.latest_profile.created_at) }}</span>
              </div>
            </template>
            <template v-else>
              <span class="ds-list__muted">未探查</span>
              <div class="ds-list__sub">
                <el-button
                  v-if="canProbe(dsOf(row))"
                  link
                  type="primary"
                  size="small"
                  @click="onOpenDetail(dsOf(row), true)"
                >立即探查</el-button>
                <span v-else class="ds-list__muted">该类型不可探查</span>
              </div>
            </template>
          </template>
        </el-table-column>

        <el-table-column label="连通性" width="250">
          <template #default="{ row }">
            <ConnectivityTestPanel
              :datasource-id="row.id"
              :last-tested-at="row.last_tested_at"
              :last-test-ok="row.last_test_ok"
              :connectable="canProbe(dsOf(row))"
              variant="inline"
              @tested="onTested"
            />
          </template>
        </el-table-column>

        <el-table-column label="操作" width="150" fixed="right">
          <template #default="{ row }">
            <div class="ds-list__actions">
              <el-button v-if="isMaker" link type="primary" size="small" @click="onEdit(dsOf(row))">编辑</el-button>
              <el-button
                v-if="isMaker"
                link
                size="small"
                :disabled="!canProbe(dsOf(row))"
                @click="onOpenDetail(dsOf(row), true)"
              >探查</el-button>
              <!-- 非 maker 直接不显示（可见性），写操作的最终裁决仍在后端 -->
              <DataSourceActionsBar
                v-if="isMaker"
                :data-source="dsOf(row)"
                variant="dropdown"
                @conflict="onConflict"
                @removed="onReload"
                @changed="onReload"
              />
            </div>
          </template>
        </el-table-column>
      </QueryTable>
    </SectionCard>
  </div>
</template>

<style scoped>
.ds-list {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.ds-list__notice {
  margin: 0;
  line-height: 18px;
}

.ds-list__conflict {
  margin: 0;
}

.ds-list__filters {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
}

.ds-list__keyword {
  width: 240px;
}

.ds-list__select {
  width: 190px;
}

.ds-list__select--narrow {
  width: 130px;
}

.ds-list__sub {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
  margin-top: 2px;
  font-size: 11px;
  line-height: 16px;
  color: var(--elt-text-muted);
}

.ds-list__mono {
  font-family: var(--elt-font-mono);
}

.ds-list__tags {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.ds-list__muted {
  font-size: 12px;
  color: var(--elt-text-muted);
}

.ds-list__actions {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  align-items: center;
}
</style>
