/**
 * dev mock 种子数据（仅在 VITE_USE_MOCK=true 时由 index.ts 挂载）。
 *
 * 三条硬约束，改动前先读：
 * 1. **确定性**：所有时间取自 BASE_MS 的固定偏移，模块作用域内不出现 Date.now()
 *    与 Math.random()。否则分页 total / 列表顺序会在两次请求之间漂移，UI 表现像抖动 bug。
 * 2. **无明文 Secret**（CLAUDE.md §5.2）：endpoint 只放 host/port/database 这类非凭据键，
 *    凭据一律只以 secret_ref（Vault 路径 + 版本 + 键名）出现；样本一律写死为掩码形态，
 *    仓库内不存在任何可用凭据、连接串或真实个人身份信息。
 * 3. **类型即契约**：全部数据用 @/types 的 DTO 标注，后端字段名漂移会在 vue-tsc 阶段暴露。
 */

import type {
  AutofixRound,
  BenchmarkCaseResult,
  BenchmarkRunRead,
  BenchmarkSuiteRead,
  BudgetDefaultsRead,
  ConnectivityResult,
  CurrentUser,
  DataSource,
  Diagnosis,
  ErrorCodeLabel,
  ErrorRecordRead,
  EtlPlan,
  EtlPlanDag,
  EvidenceEventRead,
  ExecutionBudget,
  ExecutionMetricPoint,
  FreezeArtifactManifest,
  GateResult,
  GenerationContext,
  HoconArtifact,
  LedgerVerifyResult,
  MemberRead,
  MetadataProfile,
  MetadataProfileBrief,
  MonitorRange,
  MonitorSummary,
  Pipeline,
  PipelineVersionDetail,
  PolicyDiffEntry,
  PolicyRuleRead,
  PolicyVersionRead,
  PreparationDetail,
  PreparationSummary,
  PreparationTimelineItem,
  ProjectRead,
  QualityReport,
  QualitySummaryBundle,
  RoleAssignmentRead,
  RunDetail,
  RunLogLine,
  RunStatus,
  SchemaSnapshot,
  SecretReferenceView,
  TenantRead,
  UserBrief,
  UserCandidate,
  WorkflowSnapshot,
} from '@/types'

/* ------------------------------------------------------------------ */
/* 时间与哈希：确定性工具                                              */
/* ------------------------------------------------------------------ */

/** 基准时刻（2026-10-16T09:00:00Z）。UI 上呈现为「就是今天」，但每次运行都一致。 */
const BASE_MS = Date.UTC(2026, 9, 16, 9, 0, 0)

const MINUTE_MS = 60_000

/** 相对基准时刻取 ISO8601；负数为过去 */
function at(offsetMinutes: number): string {
  return new Date(BASE_MS + offsetMinutes * MINUTE_MS).toISOString()
}

/** 创世条目的 prev_hash：64 个 0 */
const GENESIS_HASH = '0'.repeat(64)

/** 32 位 FNV-1a，仅用于把字符串摊成种子 */
function fnv1a(input: string): number {
  let hash = 0x811c9dc5
  for (let i = 0; i < input.length; i += 1) {
    hash ^= input.charCodeAt(i)
    hash = Math.imul(hash, 0x01000193)
  }
  return hash >>> 0
}

/**
 * 生成 64 位十六进制的伪指纹。
 * 这不是密码学哈希——mock 层不需要抗碰撞，只需要「看起来像 sha256 且可复现」。
 */
export function hexDigest(seed: string): string {
  let state = fnv1a(seed) || 1
  let out = ''
  for (let i = 0; i < 8; i += 1) {
    state = (Math.imul(state ^ (state >>> 15), 0x2545f491) + 0x9e3779b9) >>> 0
    out += state.toString(16).padStart(8, '0')
  }
  return out
}

/** 短哈希（前 12 位 + 省略号），用于 payload_digest 这类展示型字段 */
function shortDigest(seed: string): string {
  return `${hexDigest(seed).slice(0, 16)}…`
}

/* ------------------------------------------------------------------ */
/* 租户、用户、项目                                                    */
/* ------------------------------------------------------------------ */

export const tenant: TenantRead = {
  id: 1,
  code: 'demo',
  name: '示例数据平台租户',
  status: 'active',
}

export const users: UserBrief[] = [
  { id: 1, username: 'linsiyuan', display_name: '林思远', email: 'linsiyuan@example.com', status: 'active' },
  { id: 2, username: 'chenke', display_name: '陈珂', email: 'chenke@example.com', status: 'active' },
  { id: 3, username: 'zhaomin', display_name: '赵敏', email: 'zhaomin@example.com', status: 'locked' },
  { id: 4, username: 'zhouye', display_name: '周野', email: 'zhouye@example.com', status: 'disabled' },
]

export const currentUser: CurrentUser = {
  id: 1,
  username: 'linsiyuan',
  email: 'linsiyuan@example.com',
  display_name: '林思远',
  tenant_id: 1,
  tenant_code: 'demo',
  projects: [
    { id: 1, name: '数仓开发环境', roles: ['maker', 'operator'] },
    { id: 2, name: '数仓生产环境', roles: ['operator', 'auditor'] },
  ],
}

export const projectBriefs = currentUser.projects

export let projects: ProjectRead[] = [
  {
    id: 1,
    tenant_id: 1,
    code: 'dw-dev',
    name: '数仓开发环境',
    description: '宽表与维度建模的开发验证环境，读写开放但不落生产表。',
    env: 'dev',
    status: 'active',
    member_count: 3,
    can_manage_members: true,
    can_manage_roles: true,
  },
  {
    id: 2,
    tenant_id: 1,
    code: 'dw-prod',
    name: '数仓生产环境',
    description: '对外口径的生产宽表，任何写入必须走四眼审批与原子 Swap。',
    env: 'prod',
    status: 'active',
    member_count: 2,
    can_manage_members: true,
    can_manage_roles: false,
  },
]

/* ------------------------------------------------------------------ */
/* 成员与职责槽                                                        */
/* ------------------------------------------------------------------ */

export let members: MemberRead[] = [
  {
    id: 1,
    project_id: 1,
    user_id: 1,
    username: 'linsiyuan',
    display_name: '林思远',
    email: 'linsiyuan@example.com',
    status: 'active',
    roles: ['maker', 'operator'],
    joined_at: at(-129600),
  },
  {
    id: 2,
    project_id: 1,
    user_id: 2,
    username: 'chenke',
    display_name: '陈珂',
    email: 'chenke@example.com',
    status: 'active',
    roles: ['checker_data'],
    joined_at: at(-129000),
  },
  {
    id: 3,
    project_id: 1,
    user_id: 3,
    username: 'zhaomin',
    display_name: '赵敏',
    email: 'zhaomin@example.com',
    status: 'active',
    roles: ['auditor'],
    joined_at: at(-128000),
  },
  {
    id: 4,
    project_id: 2,
    user_id: 1,
    username: 'linsiyuan',
    display_name: '林思远',
    email: 'linsiyuan@example.com',
    status: 'active',
    roles: ['operator'],
    joined_at: at(-90000),
  },
  {
    id: 5,
    project_id: 2,
    user_id: 2,
    username: 'chenke',
    display_name: '陈珂',
    email: 'chenke@example.com',
    status: 'active',
    roles: ['checker_security'],
    joined_at: at(-89000),
  },
]

export let roleAssignments: RoleAssignmentRead[] = [
  {
    id: 1,
    project_id: 1,
    member_id: 1,
    user_id: 1,
    user_name: '林思远',
    role: 'maker',
    granted_by: null,
    granted_by_name: '系统初始化',
    granted_at: at(-129600),
    valid_from: null,
    valid_to: null,
    reason: '项目创建者默认授予方案发起人',
  },
  {
    id: 2,
    project_id: 1,
    member_id: 2,
    user_id: 2,
    user_name: '陈珂',
    role: 'checker_data',
    granted_by: 1,
    granted_by_name: '林思远',
    granted_at: at(-129000),
    valid_from: at(-129000),
    valid_to: null,
    reason: '数据审批槽：负责目标表结构与口径',
  },
  {
    id: 3,
    project_id: 1,
    member_id: 3,
    user_id: 3,
    user_name: '赵敏',
    role: 'auditor',
    granted_by: 1,
    granted_by_name: '林思远',
    granted_at: at(-128000),
    valid_from: at(-128000),
    valid_to: at(20000),
    reason: '审计员临时期限，用于季度审计',
  },
  {
    id: 4,
    project_id: 2,
    member_id: 5,
    user_id: 2,
    user_name: '陈珂',
    role: 'checker_security',
    granted_by: 1,
    granted_by_name: '林思远',
    granted_at: at(-89000),
    valid_from: null,
    valid_to: null,
    reason: '生产环境安全审批槽',
  },
]

/** 候选用户：本租户内尚未加入该项目的成员 */
export function memberCandidates(projectId: number): UserCandidate[] {
  const joined = new Set(members.filter((m) => m.project_id === projectId).map((m) => m.user_id))
  return users.map((u) => ({ ...u, already_member: joined.has(u.id) }))
}

/* ------------------------------------------------------------------ */
/* 数据源与元数据                                                      */
/* ------------------------------------------------------------------ */

function secretRef(path: string, version: number, keyNames: string[]): SecretReferenceView {
  return {
    provider: 'vault',
    path,
    version,
    key_names: keyNames,
    last_rotated_at: at(-10080),
  }
}

/** 数据源公共字段的工厂：每次返回新对象，避免多处共享同一引用被就地改写 */
function dsBase() {
  return {
    tags: [] as string[],
    status: 'active' as const,
    owner_id: 1 as number | null,
    owner_display_name: '林思远' as string | null,
    last_tested_at: at(-180) as string | null,
    last_test_ok: true as boolean | null,
    latest_profile: null as MetadataProfileBrief | null,
    created_at: at(-43200),
    updated_at: at(-180),
  }
}

export let dataSources: DataSource[] = [
  {
    ...dsBase(),
    id: 1,
    project_id: 1,
    name: 'mysql-order-src',
    type: 'mysql',
    env: 'dev',
    sensitive_level: 'internal',
    endpoint: { host: '10.0.0.5', port: 3306, database: 'ods', charset: 'utf8mb4' },
    secret_ref: secretRef('secret/data/dw/dev/mysql-order-src', 4, ['username', 'password']),
    tags: ['order', 'source'],
  },
  {
    ...dsBase(),
    id: 2,
    project_id: 1,
    name: 'mysql-user-src',
    type: 'mysql',
    env: 'dev',
    sensitive_level: 'confidential',
    endpoint: { host: '10.0.0.6', port: 3306, database: 'crm', charset: 'utf8mb4' },
    secret_ref: secretRef('secret/data/dw/dev/mysql-user-src', 2, ['username', 'password']),
    tags: ['crm', 'pii'],
  },
  {
    ...dsBase(),
    id: 3,
    project_id: 1,
    name: 'pg-ops-meta',
    type: 'postgresql',
    env: 'staging',
    sensitive_level: 'internal',
    endpoint: { host: '10.0.1.11', port: 5432, database: 'ops', schema: 'public' },
    secret_ref: secretRef('secret/data/dw/staging/pg-ops-meta', 1, ['username', 'password']),
    tags: ['ops'],
  },
  {
    ...dsBase(),
    id: 4,
    project_id: 1,
    name: 'doris-dw-dev',
    type: 'doris',
    env: 'dev',
    sensitive_level: 'internal',
    endpoint: { host: '10.0.2.20', port: 9030, database: 'dw' },
    secret_ref: secretRef('secret/data/dw/dev/doris-dw-dev', 3, ['username', 'password']),
    tags: ['warehouse', 'target'],
  },
  {
    ...dsBase(),
    id: 5,
    project_id: 2,
    name: 'doris-dw-prod',
    type: 'doris',
    env: 'prod',
    sensitive_level: 'restricted',
    endpoint: { host: '10.0.9.20', port: 9030, database: 'dw_prod' },
    secret_ref: secretRef('secret/data/dw/prod/doris-dw-prod', 7, ['username', 'password']),
    tags: ['warehouse', 'target', 'production'],
    last_test_ok: true,
  },
  {
    ...dsBase(),
    id: 6,
    project_id: 2,
    name: 'mysql-prod-crm',
    type: 'mysql',
    env: 'prod',
    sensitive_level: 'confidential',
    endpoint: { host: '10.0.9.31', port: 3306, database: 'crm_prod', charset: 'utf8mb4' },
    secret_ref: secretRef('secret/data/dw/prod/mysql-prod-crm', 5, ['username', 'password']),
    tags: ['crm', 'pii', 'production'],
  },
  {
    ...dsBase(),
    id: 7,
    project_id: 1,
    name: 'minio-raw',
    type: 'minio',
    env: 'dev',
    sensitive_level: 'internal',
    endpoint: { endpoint: 'http://minio:9000', bucket: 'raw', path: 'landing/' },
    secret_ref: secretRef('secret/data/dw/dev/minio-raw', 2, ['access_key', 'secret_key']),
    tags: ['object-storage'],
  },
  {
    ...dsBase(),
    id: 8,
    project_id: 1,
    name: 'csv-settlement',
    type: 'csv',
    env: 'dev',
    sensitive_level: 'public',
    endpoint: { path: 's3://raw/landing/settlement/20261015.csv', encoding: 'utf-8', delimiter: ',', has_header: 'true' },
    secret_ref: null,
    tags: ['file'],
    owner_id: 4,
    owner_display_name: '周野',
  },
  {
    ...dsBase(),
    id: 9,
    project_id: 1,
    name: 'json-events',
    type: 'json',
    env: 'dev',
    sensitive_level: 'internal',
    endpoint: { path: 's3://raw/landing/events/20261015.json', encoding: 'utf-8', json_path: '$.data.items' },
    secret_ref: null,
    tags: ['file', 'event'],
  },
  {
    ...dsBase(),
    id: 10,
    project_id: 1,
    name: 'parquet-archive',
    type: 'parquet',
    env: 'staging',
    sensitive_level: 'internal',
    endpoint: { path: 's3://archive/dw/orders/' },
    secret_ref: null,
    tags: ['file', 'archive'],
    status: 'disabled',
    last_test_ok: null,
    last_tested_at: null,
  },
  {
    ...dsBase(),
    id: 11,
    project_id: 2,
    name: 'oracle-erp',
    type: 'oracle',
    env: 'prod',
    sensitive_level: 'restricted',
    endpoint: { host: '10.0.9.41', port: 1521, service_name: 'ERP' },
    secret_ref: secretRef('secret/data/dw/prod/oracle-erp', 9, ['username', 'password']),
    tags: ['erp', 'production'],
    last_test_ok: false,
  },
  {
    ...dsBase(),
    id: 12,
    project_id: 1,
    name: 'clickhouse-metrics',
    type: 'clickhouse',
    env: 'staging',
    sensitive_level: 'internal',
    endpoint: { host: '10.0.1.60', port: 8123, database: 'metrics' },
    secret_ref: secretRef('secret/data/dw/staging/clickhouse-metrics', 1, ['username', 'password']),
    tags: ['metrics'],
  },
  {
    ...dsBase(),
    id: 13,
    project_id: 2,
    name: 'rest-crm-api',
    type: 'rest',
    env: 'prod',
    sensitive_level: 'confidential',
    endpoint: { base_url: 'https://crm.example.com/api/v2', auth_type: 'bearer' },
    secret_ref: secretRef('secret/data/dw/prod/rest-crm-api', 2, ['token']),
    tags: ['api', 'production'],
  },
  {
    ...dsBase(),
    id: 14,
    project_id: 1,
    name: 'excel-monthly',
    type: 'excel',
    env: 'dev',
    sensitive_level: 'public',
    endpoint: { path: 's3://raw/manual/monthly_report.xlsx' },
    secret_ref: null,
    tags: ['manual'],
    owner_id: null,
    owner_display_name: null,
  },
]

/* ---------------------------------- 元数据探查 ---------------------------------- */

function buildSchema(_seed: string, tableNames: string[]): SchemaSnapshot {
  const columns = [
    { name: 'id', data_type: 'bigint', nullable: false, is_primary_key: true, comment: '主键', null_ratio: 0, distinct_approx: 480000, sensitive_level: 'internal' as const, masked: false },
    { name: 'customer_name', data_type: 'varchar(64)', nullable: true, is_primary_key: false, comment: '客户姓名', null_ratio: 0.012, distinct_approx: 38900, sensitive_level: 'confidential' as const, masked: true },
    { name: 'mobile', data_type: 'varchar(20)', nullable: true, is_primary_key: false, comment: '手机号', null_ratio: 0.034, distinct_approx: 41200, sensitive_level: 'restricted' as const, masked: true },
    { name: 'email', data_type: 'varchar(128)', nullable: true, is_primary_key: false, comment: '邮箱', null_ratio: 0.081, distinct_approx: 37700, sensitive_level: 'confidential' as const, masked: true },
    { name: 'amount', data_type: 'decimal(18,2)', nullable: false, is_primary_key: false, comment: '金额', null_ratio: 0.001, distinct_approx: 120400, sensitive_level: 'internal' as const, masked: false },
    { name: 'status', data_type: 'varchar(16)', nullable: false, is_primary_key: false, comment: '状态', null_ratio: 0.002, distinct_approx: 6, sensitive_level: 'public' as const, masked: false },
    { name: 'created_at', data_type: 'datetime', nullable: false, is_primary_key: false, comment: '创建时间', null_ratio: 0, distinct_approx: 468000, sensitive_level: 'public' as const, masked: false },
  ]
  return {
    tables: tableNames.map((name, index) => ({
      schema: 'ods',
      name,
      comment: `${name} 业务表`,
      row_count_approx: 120000 + index * 48000,
      columns,
    })),
  }
}

const schemaByKey: Record<string, SchemaSnapshot> = {
  '1:1': buildSchema('1', ['orders', 'order_items']),
  '2:1': buildSchema('2', ['customer', 'customer_contact']),
}

export function schemaFor(dataSourceId: number, version: number, table?: string): SchemaSnapshot {
  const snapshot = schemaByKey[`${dataSourceId}:${version}`] ?? buildSchema('default', ['orders'])
  if (table === undefined || table === '') {
    return snapshot
  }
  return { tables: snapshot.tables.filter((t) => t.name === table) }
}

/** 脱敏样本：全部写死为掩码形态，库里不存在原文，界面也无从解敏 */
export let maskedSamples: Record<string, Array<Record<string, string>>> = {
  orders: [
    { id: '100231', customer_name: '张**', mobile: '138****5678', email: 'z***@example.com', amount: '1280.00', status: 'paid', created_at: '2026-10-15 08:12:03' },
    { id: '100232', customer_name: '李**', mobile: '139****0421', email: 'l***@example.com', amount: '89.50', status: 'refunded', created_at: '2026-10-15 08:14:41' },
    { id: '100233', customer_name: '王**', mobile: '137****9932', email: 'w***@example.com', amount: '6420.00', status: 'paid', created_at: '2026-10-15 08:19:55' },
  ],
  order_items: [
    { id: '500011', order_id: '100231', sku: 'SKU-****23', qty: '2', price: '640.00' },
    { id: '500012', order_id: '100232', sku: 'SKU-****77', qty: '1', price: '89.50' },
  ],
}

export function samplesFor(_dataSourceId: number, _version: number, table: string, limit: number) {
  const rows = maskedSamples[table] ?? []
  return { rows: rows.slice(0, limit), truncated: rows.length > limit }
}

export let metadataProfiles: MetadataProfile[] = [
  {
    id: 1,
    data_source_id: 1,
    version: 1,
    status: 'ready',
    sampling: { rows: 200, scope: 'selected_tables', tables: ['orders', 'order_items'] },
    masking_policy: { default: 'mask_middle', overrides: { mobile: 'mask_middle', email: 'truncate' } },
    masked_samples: maskedSamples,
    tables_count: 2,
    sha256: hexDigest('profile-1-v1'),
    expires_at: at(4320),
    error_message: null,
    created_at: at(-1440),
    created_by_display_name: '林思远',
  },
  {
    id: 2,
    data_source_id: 2,
    version: 1,
    status: 'ready',
    sampling: { rows: 200, scope: 'full_tables', tables: null },
    masking_policy: { default: 'hash', overrides: { email: 'truncate' } },
    masked_samples: {
      customer: [
        { id: '88001', customer_name: '赵**', mobile: '136****8890', email: 'z***@example.com' },
        { id: '88002', customer_name: '钱**', mobile: '135****3311', email: 'q***@example.com' },
      ],
    },
    tables_count: 2,
    sha256: hexDigest('profile-2-v1'),
    expires_at: at(-720),
    error_message: null,
    created_at: at(-2000),
    created_by_display_name: '陈珂',
  },
]

/**
 * 探查在真实后端是**异步作业**：提交后先返回 `probing`，由 worker 推进到 `ready`。
 * mock 里没有调度器，于是改成**读取时按提交时间惰性推进**。
 *
 * 为什么必须做：不做的话，演示时「探查 → 看样本」这条最核心的链路会永远转圈，
 * 而看的人会以为是自己操作错了。
 */
const PROBE_SETTLE_MS = 3000

/**
 * 把探查推进到终态，并让每个数据源的 `latest_profile` 从 `metadataProfiles` 派生。
 *
 * `latest_profile` 必须派生而不是各写一份：fixtures 里两处各说各话时，
 * 列表页显示「未探查」而详情页却列出 v1，这种矛盾会让人怀疑数据本身。
 */
export function settleProbeState(): void {
  const now = Date.now()
  for (const profile of metadataProfiles) {
    if (profile.status !== 'probing' || now - Date.parse(profile.created_at) < PROBE_SETTLE_MS) {
      continue
    }
    // 只认**更早版本**的 ready 记录作为样本来源，避免同一轮里刚推进的版本被当成「上一版」
    const previous = metadataProfiles.find(
      (p) => p.data_source_id === profile.data_source_id && p.status === 'ready' && p.version < profile.version,
    )
    const schemaOnly = profile.sampling.scope === 'schema_only'
    profile.status = 'ready'
    profile.tables_count = previous?.tables_count ?? (schemaOnly ? 0 : 1)
    profile.masked_samples = schemaOnly ? {} : (previous?.masked_samples ?? {})
    profile.sha256 = hexDigest(`profile:${profile.data_source_id}:${profile.version}`)
    profile.expires_at = at(1440)
  }

  for (const source of dataSources) {
    const latest = metadataProfiles
      .filter((p) => p.data_source_id === source.id)
      .reduce<MetadataProfile | null>((best, p) => (best === null || p.version > best.version ? p : best), null)
    source.latest_profile =
      latest === null
        ? null
        : {
            version: latest.version,
            status: latest.status,
            tables_count: latest.tables_count,
            created_at: latest.created_at,
            expires_at: latest.expires_at,
          }
  }
}

export function profileBriefs(dataSourceId: number): MetadataProfileBrief[] {
  return metadataProfiles
    .filter((p) => p.data_source_id === dataSourceId)
    .map((p) => ({
      version: p.version,
      status: p.status,
      tables_count: p.tables_count,
      created_at: p.created_at,
      expires_at: p.expires_at,
    }))
}

/** 连通性测试的终态结果：target 是后端已掩码的主机:端口形态 */
export function connectivityResult(testId: string, ok: boolean): ConnectivityResult {
  return {
    test_id: testId,
    state: ok ? 'succeeded' : 'failed',
    latency_ms: ok ? 42 : null,
    target: ok ? '10.**.**.5:3306' : '10.**.**.41:1521',
    server_version: ok ? '8.0.36' : null,
    error_code: ok ? null : 'DATASOURCE_TIMEOUT',
    message: ok ? '连接成功' : '连接超时（已脱敏）',
    tested_at: at(-1),
  }
}

/* ------------------------------------------------------------------ */
/* EtlPlan / HOCON / DAG 构建器                                        */
/* ------------------------------------------------------------------ */

export function defaultBudget(): ExecutionBudget {
  return {
    max_read_rows: 5_000_000,
    max_write_bytes: 2_147_483_648,
    max_duration_sec: 3600,
    max_amplification_ratio: 3,
    max_reject_rate: 0.05,
    warn_threshold_pct: 80,
  }
}

function genCtx(rounds: number, model = 'deepseek-chat'): GenerationContext {
  return {
    model_name: model,
    model_version: '2026-08-01',
    prompt_template_version: 'etlplan-v3',
    autofix_rounds: rounds,
  }
}

function buildDag(sourceLabel: string, targetLabel: string): EtlPlanDag {
  return {
    nodes: [
      { id: 'src_1', type: 'source', label: sourceLabel, meta: { data_source_id: 1, table: 'orders' }, position: { x: 0, y: 80 } },
      { id: 'tr_1', type: 'transform', label: '字段标准化', meta: { kind: 'cast' }, position: { x: 220, y: 40 } },
      { id: 'filter_1', type: 'filter', label: '增量过滤', meta: { expression: "updated_at >= '2026-10-15'" }, position: { x: 220, y: 160 } },
      { id: 'q_1', type: 'quality', label: '质量契约校验', meta: { rule_count: 4 }, position: { x: 460, y: 80 } },
      { id: 'sink_1', type: 'sink', label: targetLabel, meta: { mode: 'shadow_then_swap' }, position: { x: 700, y: 80 } },
    ],
    edges: [
      { id: 'e1', source: 'src_1', target: 'tr_1', label: null },
      { id: 'e2', source: 'src_1', target: 'filter_1', label: null },
      { id: 'e3', source: 'tr_1', target: 'q_1', label: null },
      { id: 'e4', source: 'filter_1', target: 'q_1', label: null },
      { id: 'e5', source: 'q_1', target: 'sink_1', label: '通过' },
    ],
  }
}

function buildPlan(seed: number, valid: boolean, sourceTable = 'orders'): EtlPlan {
  return {
    id: seed,
    schema_version: '1.0.0',
    valid,
    validation_errors: valid ? [] : [{ path: 'field_mappings[3].target_type', reason: '目标类型 decimal(18,2) 与来源 varchar(64) 不兼容' }],
    plan: {
      inputs: [{ id: 'in_1', data_source_id: 1, schema: 'ods', table: sourceTable }],
      outputs: [{ id: 'out_1', data_source_id: 4, database: 'dw', table: `${sourceTable}_wide`, mode: 'shadow_then_swap' }],
      field_mappings: [
        { source_field: 'id', source_type: 'bigint', target_field: 'order_id', target_type: 'bigint', transform: 'none', masking: null, quality_rule_ids: ['qk_001'], change: 'modified' },
        { source_field: 'customer_name', source_type: 'varchar(64)', target_field: 'customer_name', target_type: 'varchar(64)', transform: 'trim', masking: { method: 'mask_middle', params: { keep_head: 1, keep_tail: 0 } }, quality_rule_ids: [], change: 'same' },
        { source_field: 'mobile', source_type: 'varchar(20)', target_field: 'mobile_masked', target_type: 'varchar(20)', transform: 'mask', masking: { method: 'mask_middle', params: { keep_head: 3, keep_tail: 4 } }, quality_rule_ids: ['qk_002'], change: 'modified' },
        { source_field: 'amount', source_type: 'decimal(18,2)', target_field: 'amount', target_type: 'decimal(18,2)', transform: 'cast', masking: null, quality_rule_ids: ['qk_003'], change: 'added' },
      ],
      transforms: [
        { id: 'tr_1', kind: 'cast', input_fields: ['id'], output_field: 'order_id', params: { to: 'bigint' } },
        { id: 'tr_2', kind: 'concat', input_fields: ['province', 'city'], output_field: 'region_path', params: { separator: '/' } },
      ],
      filters: [{ id: 'filter_1', expression: "updated_at >= '2026-10-15'", description: '按更新时间做增量切片' }],
      quality_rules: [
        { id: 'qk_001', field: 'order_id', rule_type: 'not_null', params: {}, error_code: 'E_NULL_PK', on_violation: 'reject', enabled: true },
        { id: 'qk_002', field: 'mobile_masked', rule_type: 'regex', params: { pattern: '^\\d{3}\\*{4}\\d{4}$' }, error_code: 'E_REGEX_MISMATCH', on_violation: 'warn', enabled: true },
        { id: 'qk_003', field: 'amount', rule_type: 'range', params: { min: 0, max: 1_000_000 }, error_code: 'E_RANGE_VIOLATION', on_violation: 'reject', enabled: true },
        { id: 'qk_004', field: 'status', rule_type: 'enum', params: { values: ['paid', 'refunded', 'pending'] }, error_code: 'E_ENUM_INVALID', on_violation: 'reject', enabled: true },
      ],
      dag: buildDag('ods.orders（MySQL）', 'dw.orders_wide（Doris·影子表→Swap）'),
      budget: defaultBudget(),
    },
  }
}

function buildHocon(seed: number): HoconArtifact {
  // 变量与 Vault 引用，不含任何明文凭据
  const content = [
    'env {',
    '  job.mode = "BATCH"',
    `  job.name = "mock_pipeline_${seed}"`,
    '}',
    '',
    'source {',
    '  Jdbc {',
    `    url = "jdbc:mysql://\${MYSQL_HOST}:\${MYSQL_PORT}/\${MYSQL_DATABASE}"`,
    '    driver = "com.mysql.cj.jdbc.Driver"',
    '    username = "${VAULT_MYSQL_ORDER_USERNAME}"',
    '    password = "${VAULT_MYSQL_ORDER_PASSWORD}"',
    '    query = "select * from orders where updated_at >= \'2026-10-15\'"',
    '  }',
    '}',
    '',
    'sink {',
    '  Doris {',
    `    fenodes = "\${DORIS_FE_HOST}:\${DORIS_FE_PORT}"`,
    '    username = "${VAULT_DORIS_USERNAME}"',
    '    password = "${VAULT_DORIS_PASSWORD}"',
    '    table.identifier = "dw.orders_wide"',
    '    sink.enable-2pc = "true"',
    '  }',
    '}',
  ].join('\n')
  return {
    id: seed,
    content,
    sha256: hexDigest(`hocon:${seed}`),
    parser_version: 'seatunnel-2.3.8',
    validation_result: { ok: true, warnings: [] },
  }
}

function buildManifest(plan: EtlPlan, hocon: HoconArtifact, ruleCount: number, rejectBudget: number): FreezeArtifactManifest {
  return {
    plan: { schema_version: plan.schema_version, valid: plan.valid },
    hocon: { sha256: hocon.sha256, parser_version: hocon.parser_version },
    quality_contract: { rule_count: ruleCount, reject_rate_budget: rejectBudget },
    supervision: defaultBudget(),
    metadata_profile: { version: 1, sha256: metadataProfiles[0]?.sha256 ?? hexDigest('profile-1-v1'), expires_at: at(4320) },
  }
}

/* ------------------------------------------------------------------ */
/* Pipeline 与版本                                                     */
/* ------------------------------------------------------------------ */

/** 8 条 Pipeline，覆盖 8 种不同 PipelineStatus */
export let pipelines: Pipeline[] = [
  { id: 1, project_id: 1, name: 'ods_orders_to_dw', description: '订单源表 → 订单宽表（日增量）', status: 'succeeded', current_version_id: 101, source_data_source_id: 1, target_data_source_id: 4, created_at: at(-20000), updated_at: at(-120) },
  { id: 2, project_id: 1, name: 'dim_user_wide', description: '客户维度宽表，含手机号脱敏列', status: 'version_frozen', current_version_id: 102, source_data_source_id: 2, target_data_source_id: 4, created_at: at(-19000), updated_at: at(-600) },
  { id: 3, project_id: 1, name: 'fact_payment_daily', description: '支付事实表日汇总，门禁未通过', status: 'gate_failed', current_version_id: 103, source_data_source_id: 1, target_data_source_id: 4, created_at: at(-18000), updated_at: at(-400) },
  { id: 4, project_id: 2, name: 'crm_customer_sync', description: '生产 CRM 客户同步（受限数据）', status: 'pending_approval', current_version_id: null, source_data_source_id: 6, target_data_source_id: 5, created_at: at(-16000), updated_at: at(-300) },
  { id: 5, project_id: 2, name: 'settlement_recon', description: '结算对账，生产目标表', status: 'running', current_version_id: 104, source_data_source_id: 6, target_data_source_id: 5, created_at: at(-15000), updated_at: at(-5) },
  { id: 6, project_id: 1, name: 'event_stream_agg', description: '事件流聚合，尚未生成方案', status: 'draft', current_version_id: null, source_data_source_id: 9, target_data_source_id: 4, created_at: at(-14000), updated_at: at(-14000) },
  { id: 7, project_id: 2, name: 'erp_inventory_snapshot', description: 'ERP 库存快照，运行中被监督硬中断', status: 'interrupted', current_version_id: 105, source_data_source_id: 11, target_data_source_id: 5, created_at: at(-13000), updated_at: at(-60) },
  { id: 8, project_id: 1, name: 'metrics_rollup', description: '指标汇总，因质量超限已回滚', status: 'rolled_back', current_version_id: null, source_data_source_id: 12, target_data_source_id: 4, created_at: at(-12000), updated_at: at(-800) },
]

function buildVersion(
  id: number,
  _pipelineId: number,
  versionNo: number,
  statusNote: string,
  valid: boolean,
  frozenMinutes: number,
  frozenBy: string,
  sourceTable: string,
): PipelineVersionDetail {
  const plan = buildPlan(id, valid, sourceTable)
  const hocon = buildHocon(id)
  return {
    id,
    version_no: versionNo,
    sha256: hexDigest(`version:${id}`),
    status_note: statusNote,
    frozen_at: at(frozenMinutes),
    frozen_by_display_name: frozenBy,
    source_metadata_profile_version: 1,
    generation_context: genCtx(valid ? 0 : 1),
    manifest: buildManifest(plan, hocon, plan.plan.quality_rules.length, defaultBudget().max_reject_rate),
    plan,
    hocon,
  }
}

export let pipelineVersions: PipelineVersionDetail[] = [
  buildVersion(101, 1, 3, '门禁通过，已冻结为不可变版本', true, -3000, '林思远', 'orders'),
  buildVersion(102, 2, 1, '门禁通过，已冻结为不可变版本', true, -1200, '林思远', 'customer'),
  buildVersion(103, 3, 2, '门禁未通过：存在 2 条阻断级 finding', false, -500, '林思远', 'payment'),
  buildVersion(104, 5, 1, '门禁通过，已冻结为不可变版本', true, -900, '林思远', 'settlement'),
  buildVersion(105, 7, 1, '门禁通过，已冻结为不可变版本', true, -700, '陈珂', 'inventory'),
]

/** 版本 → 所属 Pipeline 的显式映射：DTO 上没有 pipeline_id，靠它支撑「某 Pipeline 的版本列表」 */
export const versionToPipeline: Record<number, number> = { 101: 1, 102: 2, 103: 3, 104: 5, 105: 7 }

export function versionsOfPipeline(pipelineId: number): PipelineVersionDetail[] {
  return pipelineVersions.filter((v) => versionToPipeline[v.id] === pipelineId)
}

/** 版本 Diff：base → target 方向固定，读反会得出完全相反的审计结论 */
export function versionDiff(versionIds: number[]) {
  const [base, target] = versionIds
  return {
    entries: [
      { path: 'field_mappings[3].target_type', kind: 'changed' as const, before: 'varchar(64)', after: 'decimal(18,2)' },
      { path: 'quality_rules[3]', kind: 'added' as const, after: 'qk_004 enum(status)' },
      { path: 'budget.max_reject_rate', kind: 'changed' as const, before: '0.02', after: '0.05' },
      { path: 'dag.nodes[2]', kind: 'removed' as const, before: `filter_1（版本 ${base} 独有）`, after: undefined },
    ],
    base_version_id: base,
    target_version_id: target,
  }
}

/* ---------------------------------- 门禁结果 ---------------------------------- */

/** 门禁结果按 versionId 存放；v103 覆盖全部四种 GateLocator 且同时含 P0/P1 finding */
export let gateResults: Record<number, GateResult> = {
  101: {
    evaluated_at: at(-3050),
    conclusion: 'pass',
    findings: [
      {
        id: 'g_101_1',
        severity: 'info',
        category: 'resource',
        code: 'OK',
        message: '预计读取行数 42 万，低于预算上限。',
        locator: { kind: 'plan', path: 'plan.budget.max_read_rows' },
        suggestion: null,
      },
    ],
    autofix_rounds: [],
    autofix_exhausted: false,
  },
  103: {
    evaluated_at: at(-560),
    conclusion: 'block',
    findings: [
      {
        id: 'g103_1',
        severity: 'block',
        category: 'policy',
        code: 'POLICY_BLOCKED',
        message: '目标表命中 P0 策略：受限数据写生产表必须显式指定安全审批槽。',
        locator: { kind: 'dag_node', node_id: 'sink_1' },
        suggestion: '在准备单中补齐 checker_security 审批槽。',
      },
      {
        id: 'g103_2',
        severity: 'block',
        category: 'syntax',
        code: 'HOCON_SYNTAX_ERROR',
        message: '第 14 行 sink 配置块缺少闭合花括号。',
        locator: { kind: 'hocon', line: 14, column: 3, end_line: 14 },
        suggestion: '补齐 `}` 后重新渲染 HOCON。',
      },
      {
        id: 'g103_3',
        severity: 'warn',
        category: 'field_mapping',
        code: 'SCHEMA_MISMATCH',
        message: '来源 varchar(64) 与目标 decimal(18,2) 类型不兼容，存在转换失败风险。',
        locator: { kind: 'plan', path: 'field_mappings[3].target_type' },
        suggestion: '增加 cast 转换或调整目标类型。',
      },
      {
        id: 'g103_4',
        severity: 'warn',
        category: 'schema',
        code: 'SCHEMA_MISMATCH',
        message: '源表 ods.payment 缺少列 updated_at，无法执行增量切片。',
        locator: { kind: 'source_table', data_source_id: 1, schema: 'ods', table: 'payment' },
        suggestion: '重新探查元数据，或改用 created_at 作为切片列。',
      },
    ],
    autofix_rounds: [
      {
        round: 1,
        trigger_finding_ids: ['g103_2'],
        hocon_diff: '--- a/job.conf\n+++ b/job.conf\n@@ -14,1 +14,2 @@\n-  Doris {\n+  Doris {\n+  }',
        plan_diff: null,
        resolved_finding_ids: ['g103_2'],
        created_at: at(-580),
      },
    ],
    autofix_exhausted: false,
  },
}

export function autofixRound(_versionId: number, round: number): AutofixRound {
  return {
    round,
    trigger_finding_ids: ['g103_3'],
    hocon_diff: `--- a/job.conf\n+++ b/job.conf\n@@ -8,1 +8,1 @@\n-    query = "select * from payment"\n+    query = "select * from payment where created_at >= '2026-10-15'"`,
    plan_diff: null,
    resolved_finding_ids: round >= 2 ? ['g103_3'] : [],
    created_at: at(-5),
  }
}

/* ---------------------------------- Studio 会话 ---------------------------------- */

export const workflowSnapshots: Record<string, WorkflowSnapshot> = {
  'thread-orders': {
    thread_id: 'thread-orders',
    phase: 'frozen',
    phase_message: null,
    pending_questions: [],
    autofix_round: 0,
    last_error: null,
  },
  'thread-clarify': {
    thread_id: 'thread-clarify',
    phase: 'awaiting_clarification',
    phase_message: '需要补充两个口径后才能生成方案',
    pending_questions: [
      {
        key: 'incremental_column',
        question: '增量切片使用哪一列？',
        kind: 'single_choice',
        options: [
          { value: 'updated_at', label: 'updated_at（更新时间）' },
          { value: 'created_at', label: 'created_at（创建时间）' },
        ],
        required: true,
        hint: '源表中两者都存在，语义不同',
      },
      {
        key: 'reject_rate_budget',
        question: '可接受的拒绝率上限是多少？',
        kind: 'threshold',
        options: null,
        required: true,
        hint: null,
        meta: { min: 0, max: 0.2, unit: '', ratio: true },
      },
    ],
    autofix_round: 0,
    last_error: null,
  },
}

/* ------------------------------------------------------------------ */
/* 准备单与审批                                                        */
/* ------------------------------------------------------------------ */

/** 时间线条目工厂；返回类型显式标注，避免字面量被推断成宽 string 而失去联合类型 */
function timelineItem(
  at: string,
  event: PreparationTimelineItem['event'],
  actor?: string,
  slot?: PreparationTimelineItem['slot'],
  detail?: string,
): PreparationTimelineItem {
  return { at, event, actor_name: actor, slot, detail }
}

export let preparations: PreparationDetail[] = [
  {
    id: 1,
    project_id: 1,
    pipeline_id: 1,
    pipeline_name: 'ods_orders_to_dw',
    pipeline_version_id: 101,
    version_no: 3,
    artifact_sha256: hexDigest('version:101'),
    input_fingerprint: hexDigest('fp:101'),
    risk_level: 'P2',
    sensitive_level: 'internal',
    environment: 'dev',
    status: 'approved',
    required_slots: ['checker_data', 'checker_security'],
    decided_slots: ['checker_data', 'checker_security'],
    created_by: 1,
    creator_name: '林思远',
    frozen_at: at(-3000),
    expires_at: at(1440),
    my_slot: null,
    can_commit: true,
    resource_scope: {
      reads: [{ data_source_id: 1, database: 'ods', table: 'orders' }],
      writes: [{ data_source_id: 4, database: 'dw', table: 'orders_wide', mode: 'shadow_then_swap' }],
    },
    budget: defaultBudget(),
    rollback_plan: {
      steps: [
        { order: 1, action: 'cancel_job', tool: 'seatunnel_client', description: '取消仍在运行的 SeaTunnel 作业' },
        { order: 2, action: 'drop_shadow', tool: 'doris_ddl', description: '删除影子表 dw.orders_wide__shadow' },
      ],
      shadow_cleanup: true,
      restore_scope: '仅影子表与本次运行产生的错误表，不动正式表',
    },
    risk_reasons: [
      { code: 'R_P2_WRITE_FORMAL', level: 'P2', title: '写正式目标表', detail: '目标为已登记的正式宽表，需数据审批槽确认口径。' },
    ],
    decisions: [
      { slot: 'checker_data', approver_id: 2, approver_name: '陈珂', approver_role: 'checker_data', decision: 'approve', comment: '口径与源表字段一致，同意。', artifact_sha256: hexDigest('version:101'), fingerprint_at_decision: hexDigest('fp:101'), decided_at: at(-2900), stale: false },
      { slot: 'checker_security', approver_id: 2, approver_name: '陈珂', approver_role: 'checker_security', decision: 'approve', comment: '无敏感字段明文外发。', artifact_sha256: hexDigest('version:101'), fingerprint_at_decision: hexDigest('fp:101'), decided_at: at(-2850), stale: false },
    ],
    timeline: [
      timelineItem(at(-3000), 'created', '林思远', undefined, '由版本 v3 创建准备单'),
      timelineItem(at(-2900), 'approved_slot', '陈珂', 'checker_data'),
      timelineItem(at(-2850), 'approved_slot', '陈珂', 'checker_security'),
    ],
  },
  {
    id: 2,
    project_id: 1,
    pipeline_id: 2,
    pipeline_name: 'dim_user_wide',
    pipeline_version_id: 102,
    version_no: 1,
    artifact_sha256: hexDigest('version:102'),
    input_fingerprint: hexDigest('fp:102'),
    risk_level: 'P1',
    sensitive_level: 'confidential',
    environment: 'dev',
    status: 'partially_approved',
    required_slots: ['checker_data', 'checker_security'],
    decided_slots: ['checker_security'],
    created_by: 1,
    creator_name: '林思远',
    frozen_at: at(-1200),
    expires_at: at(2880),
    // 当前用户（数据审批人）可审，且服务端算出可 Commit
    my_slot: 'checker_data',
    can_commit: true,
    resource_scope: {
      reads: [{ data_source_id: 2, database: 'crm', table: 'customer' }],
      writes: [{ data_source_id: 4, database: 'dw', table: 'dim_user_wide', mode: 'shadow_then_swap' }],
    },
    budget: defaultBudget(),
    rollback_plan: {
      steps: [{ order: 1, action: 'drop_shadow', tool: 'doris_ddl', description: '删除影子表' }],
      shadow_cleanup: true,
      restore_scope: '仅影子表',
    },
    risk_reasons: [
      { code: 'R_P1_SENSITIVE_PII', level: 'P1', title: '含个人标识字段', detail: '来源含手机号与邮箱，必须经脱敏映射后方可写入。' },
    ],
    decisions: [
      { slot: 'checker_security', approver_id: 2, approver_name: '陈珂', approver_role: 'checker_security', decision: 'approve', comment: '脱敏规则覆盖手机号与邮箱，同意。', artifact_sha256: hexDigest('version:102'), fingerprint_at_decision: hexDigest('fp:102'), decided_at: at(-1100), stale: false },
    ],
    timeline: [
      timelineItem(at(-1200), 'created', '林思远'),
      timelineItem(at(-1100), 'approved_slot', '陈珂', 'checker_security'),
    ],
  },
  {
    id: 3,
    project_id: 2,
    pipeline_id: 4,
    pipeline_name: 'crm_customer_sync',
    pipeline_version_id: 104,
    version_no: 1,
    artifact_sha256: hexDigest('version:104'),
    input_fingerprint: hexDigest('fp:104'),
    // P0：永远不可批准，用于验证界面的「阻断态」而非「可批态」
    risk_level: 'P0',
    sensitive_level: 'restricted',
    environment: 'prod',
    status: 'pending',
    required_slots: ['checker_data', 'checker_security'],
    decided_slots: [],
    created_by: 1,
    creator_name: '林思远',
    frozen_at: at(-900),
    expires_at: at(1440),
    my_slot: null,
    can_commit: false,
    resource_scope: {
      reads: [{ data_source_id: 6, database: 'crm_prod', table: 'customer' }],
      writes: [{ data_source_id: 5, database: 'dw_prod', table: 'dim_customer', mode: 'shadow_then_swap' }],
    },
    budget: defaultBudget(),
    rollback_plan: { steps: [], shadow_cleanup: true, restore_scope: '未定义' },
    risk_reasons: [
      { code: 'R_P0_SENSITIVE_PLAINTEXT', level: 'P0', title: '受限字段明文外发', detail: '目标表包含受限等级字段，且字段映射未配置脱敏，命中 P0 策略，禁止提交审批。' },
      { code: 'R_P0_NO_ROLLBACK', level: 'P0', title: '缺少可用回滚方案', detail: 'rollback_plan.steps 为空，无法保证可恢复。' },
    ],
    decisions: [],
    timeline: [timelineItem(at(-900), 'created', '林思远')],
  },
  {
    id: 4,
    project_id: 1,
    pipeline_id: 1,
    pipeline_name: 'ods_orders_to_dw',
    pipeline_version_id: 101,
    version_no: 2,
    artifact_sha256: hexDigest('version:101b'),
    input_fingerprint: hexDigest('fp:101b'),
    risk_level: 'P2',
    sensitive_level: 'internal',
    environment: 'dev',
    status: 'superseded',
    required_slots: ['checker_data'],
    decided_slots: [],
    created_by: 1,
    creator_name: '林思远',
    frozen_at: at(-4000),
    expires_at: null,
    my_slot: null,
    can_commit: false,
    resource_scope: { reads: [{ data_source_id: 1, database: 'ods', table: 'orders' }], writes: [{ data_source_id: 4, database: 'dw', table: 'orders_wide', mode: 'shadow_then_swap' }] },
    budget: defaultBudget(),
    rollback_plan: { steps: [], shadow_cleanup: true, restore_scope: '仅影子表' },
    risk_reasons: [],
    decisions: [],
    timeline: [timelineItem(at(-4000), 'created', '林思远'), timelineItem(at(-3000), 'superseded', '系统', undefined, '由 v3 准备单取代')],
  },
  {
    id: 5,
    project_id: 2,
    pipeline_id: 5,
    pipeline_name: 'settlement_recon',
    pipeline_version_id: 104,
    version_no: 1,
    artifact_sha256: hexDigest('version:104b'),
    input_fingerprint: hexDigest('fp:104b'),
    risk_level: 'P1',
    sensitive_level: 'confidential',
    environment: 'prod',
    status: 'pending',
    required_slots: ['checker_data', 'checker_security'],
    decided_slots: ['checker_data'],
    created_by: 1,
    creator_name: '林思远',
    frozen_at: at(-880),
    expires_at: at(2880),
    // 当前用户持有安全槽，可审
    my_slot: 'checker_security',
    can_commit: true,
    resource_scope: { reads: [{ data_source_id: 6, database: 'crm_prod', table: 'settlement' }], writes: [{ data_source_id: 5, database: 'dw_prod', table: 'fact_settlement', mode: 'shadow_then_swap' }] },
    budget: defaultBudget(),
    rollback_plan: { steps: [{ order: 1, action: 'drop_shadow', tool: 'doris_ddl', description: '删除影子表' }], shadow_cleanup: true, restore_scope: '仅影子表' },
    risk_reasons: [{ code: 'R_P1_PROD_WRITE', level: 'P1', title: '生产环境写入', detail: '目标为生产环境正式表，需双槽批准。' }],
    decisions: [
      // stale=true：审批时指纹与当前指纹已不一致，界面必须显著标注
      { slot: 'checker_data', approver_id: 2, approver_name: '陈珂', approver_role: 'checker_data', decision: 'approve', comment: '口径确认。', artifact_sha256: hexDigest('version:104b'), fingerprint_at_decision: hexDigest('fp:104b-old'), decided_at: at(-860), stale: true },
    ],
    timeline: [timelineItem(at(-880), 'created', '林思远'), timelineItem(at(-860), 'approved_slot', '陈珂', 'checker_data')],
  },
  {
    id: 6,
    project_id: 1,
    pipeline_id: 3,
    pipeline_name: 'fact_payment_daily',
    pipeline_version_id: 103,
    version_no: 2,
    artifact_sha256: hexDigest('version:103'),
    input_fingerprint: hexDigest('fp:103'),
    risk_level: 'P1',
    sensitive_level: 'internal',
    environment: 'dev',
    status: 'rejected',
    required_slots: ['checker_data'],
    decided_slots: ['checker_data'],
    created_by: 1,
    creator_name: '林思远',
    frozen_at: at(-700),
    expires_at: at(-10),
    my_slot: null,
    can_commit: false,
    resource_scope: { reads: [{ data_source_id: 1, database: 'ods', table: 'payment' }], writes: [{ data_source_id: 4, database: 'dw', table: 'fact_payment_daily', mode: 'shadow_then_swap' }] },
    budget: defaultBudget(),
    rollback_plan: { steps: [], shadow_cleanup: true, restore_scope: '仅影子表' },
    risk_reasons: [{ code: 'R_P1_SCHEMA_DRIFT', level: 'P1', title: '源端结构漂移', detail: '源表缺少 updated_at，增量切片不可用。' }],
    decisions: [{ slot: 'checker_data', approver_id: 2, approver_name: '陈珂', approver_role: 'checker_data', decision: 'reject', comment: '增量列缺失，先修方案再提。', artifact_sha256: hexDigest('version:103'), fingerprint_at_decision: hexDigest('fp:103'), decided_at: at(-650), stale: false }],
    timeline: [timelineItem(at(-700), 'created', '林思远'), timelineItem(at(-650), 'rejected_slot', '陈珂', 'checker_data')],
  },
  {
    id: 7,
    project_id: 1,
    pipeline_id: 2,
    pipeline_name: 'dim_user_wide',
    pipeline_version_id: 102,
    version_no: 1,
    artifact_sha256: hexDigest('version:102b'),
    input_fingerprint: hexDigest('fp:102b'),
    risk_level: 'P2',
    sensitive_level: 'internal',
    environment: 'dev',
    status: 'expired',
    required_slots: ['checker_data'],
    decided_slots: [],
    created_by: 1,
    creator_name: '林思远',
    frozen_at: at(-5000),
    expires_at: at(-1000),
    my_slot: null,
    can_commit: false,
    resource_scope: { reads: [{ data_source_id: 2, database: 'crm', table: 'customer' }], writes: [{ data_source_id: 4, database: 'dw', table: 'dim_user_wide', mode: 'shadow_then_swap' }] },
    budget: defaultBudget(),
    rollback_plan: { steps: [], shadow_cleanup: true, restore_scope: '仅影子表' },
    risk_reasons: [],
    decisions: [],
    timeline: [timelineItem(at(-5000), 'created', '林思远'), timelineItem(at(-1000), 'expired', '系统')],
  },
  {
    id: 8,
    project_id: 2,
    pipeline_id: 7,
    pipeline_name: 'erp_inventory_snapshot',
    pipeline_version_id: 105,
    version_no: 1,
    artifact_sha256: hexDigest('version:105'),
    input_fingerprint: hexDigest('fp:105'),
    risk_level: 'P1',
    sensitive_level: 'restricted',
    environment: 'prod',
    status: 'approved',
    required_slots: ['checker_data', 'checker_security'],
    decided_slots: ['checker_data', 'checker_security'],
    created_by: 1,
    creator_name: '林思远',
    frozen_at: at(-700),
    expires_at: at(600),
    my_slot: null,
    can_commit: false,
    resource_scope: { reads: [{ data_source_id: 11, database: 'ERP', table: 'INVENTORY' }], writes: [{ data_source_id: 5, database: 'dw_prod', table: 'dwd_inventory', mode: 'shadow_then_swap' }] },
    budget: defaultBudget(),
    rollback_plan: { steps: [{ order: 1, action: 'restore_snapshot', tool: 'doris_ddl', description: '恢复上一版本快照' }], shadow_cleanup: true, restore_scope: '正式表回滚到上一版本' },
    risk_reasons: [{ code: 'R_P1_PROD_WRITE', level: 'P1', title: '生产环境写入', detail: '受限等级来源写生产表。' }],
    decisions: [
      { slot: 'checker_data', approver_id: 2, approver_name: '陈珂', approver_role: 'checker_data', decision: 'approve', comment: '口径同意。', artifact_sha256: hexDigest('version:105'), fingerprint_at_decision: hexDigest('fp:105'), decided_at: at(-680), stale: false },
      { slot: 'checker_security', approver_id: 2, approver_name: '陈珂', approver_role: 'checker_security', decision: 'approve', comment: '安全同意。', artifact_sha256: hexDigest('version:105'), fingerprint_at_decision: hexDigest('fp:105'), decided_at: at(-670), stale: false },
    ],
    timeline: [timelineItem(at(-700), 'created', '林思远'), timelineItem(at(-680), 'approved_slot', '陈珂', 'checker_data'), timelineItem(at(-670), 'approved_slot', '陈珂', 'checker_security')],
  },
  {
    id: 9,
    project_id: 1,
    pipeline_id: 3,
    pipeline_name: 'fact_payment_daily',
    pipeline_version_id: 103,
    version_no: 1,
    artifact_sha256: hexDigest('version:103b'),
    input_fingerprint: hexDigest('fp:103b'),
    risk_level: 'P3',
    sensitive_level: 'public',
    environment: 'dev',
    status: 'approved',
    required_slots: [],
    decided_slots: [],
    created_by: 1,
    creator_name: '林思远',
    frozen_at: at(-2000),
    expires_at: at(2000),
    my_slot: null,
    can_commit: true,
    resource_scope: { reads: [{ data_source_id: 1, database: 'ods', table: 'payment' }], writes: [] },
    budget: defaultBudget(),
    rollback_plan: { steps: [], shadow_cleanup: false, restore_scope: '只读，无需回滚' },
    risk_reasons: [{ code: 'R_P3_READ_ONLY', level: 'P3', title: '只读操作', detail: '不写正式表，免审批。' }],
    decisions: [],
    timeline: [timelineItem(at(-2000), 'created', '系统', undefined, 'P3 只读免审批，自动通过')],
  },
  {
    id: 10,
    project_id: 1,
    pipeline_id: 1,
    pipeline_name: 'ods_orders_to_dw',
    pipeline_version_id: 101,
    version_no: 3,
    artifact_sha256: hexDigest('version:101c'),
    input_fingerprint: hexDigest('fp:101c'),
    risk_level: 'P2',
    sensitive_level: 'internal',
    environment: 'dev',
    status: 'pending',
    required_slots: ['checker_data'],
    decided_slots: [],
    created_by: 2,
    creator_name: '陈珂',
    frozen_at: at(-150),
    expires_at: at(3000),
    my_slot: null,
    can_commit: false,
    resource_scope: { reads: [{ data_source_id: 1, database: 'ods', table: 'orders' }], writes: [{ data_source_id: 4, database: 'dw', table: 'orders_wide', mode: 'shadow_then_swap' }] },
    budget: defaultBudget(),
    rollback_plan: { steps: [], shadow_cleanup: true, restore_scope: '仅影子表' },
    risk_reasons: [{ code: 'R_P2_WRITE_FORMAL', level: 'P2', title: '写正式目标表', detail: '需数据审批槽确认。' }],
    decisions: [],
    timeline: [timelineItem(at(-150), 'created', '陈珂')],
  },
]

export function preparationSummaries(): PreparationSummary[] {
  return preparations.map((p) => ({
    id: p.id,
    project_id: p.project_id,
    pipeline_id: p.pipeline_id,
    pipeline_name: p.pipeline_name,
    pipeline_version_id: p.pipeline_version_id,
    version_no: p.version_no,
    artifact_sha256: p.artifact_sha256,
    input_fingerprint: p.input_fingerprint,
    risk_level: p.risk_level,
    sensitive_level: p.sensitive_level,
    environment: p.environment,
    status: p.status,
    required_slots: p.required_slots,
    decided_slots: p.decided_slots,
    created_by: p.created_by,
    creator_name: p.creator_name,
    frozen_at: p.frozen_at,
    expires_at: p.expires_at,
    my_slot: p.my_slot,
    can_commit: p.can_commit,
  }))
}

/* ------------------------------------------------------------------ */
/* 运行与质量                                                          */
/* ------------------------------------------------------------------ */

function runCaps(status: RunStatus) {
  return {
    can_pause: status === 'running',
    can_resume: status === 'paused',
    can_cancel: status === 'queued' || status === 'running' || status === 'paused',
    can_rollback: status === 'failed' || status === 'interrupted',
  }
}

/** 14 条运行，覆盖全部 9 个 RunStatus */
export let runs: RunDetail[] = [
  { id: 1001, project_id: 1, pipeline_id: 1, pipeline_name: 'ods_orders_to_dw', pipeline_version_id: 101, version_no: 3, artifact_sha256: hexDigest('version:101'), status: 'succeeded', formal_table: 'dw.orders_wide', triggered_by: 1, triggered_by_name: '林思远', started_at: at(-2900), finished_at: at(-2830), duration_sec: 4200, read_rows: 421_880, written_rows: 421_102, written_bytes: 82_411_220, reject_rows: 778, reject_rate: 0.0018, ...runCaps('succeeded'), preparation_id: 1, seatunnel_job_id: 'st-9f2a1001', shadow_table: 'dw.orders_wide__shadow_1001', error_table: 'dw.etl_error_1001', avg_throughput: 640, amplification_ratio: 1.2, error_message: null, failure_snapshot: {}, budget: defaultBudget(), rollback_plan: { steps: [], shadow_cleanup: true, restore_scope: '仅影子表' } },
  { id: 1002, project_id: 1, pipeline_id: 2, pipeline_name: 'dim_user_wide', pipeline_version_id: 102, version_no: 1, artifact_sha256: hexDigest('version:102'), status: 'succeeded', formal_table: 'dw.dim_user_wide', triggered_by: 1, triggered_by_name: '林思远', started_at: at(-1100), finished_at: at(-1080), duration_sec: 1200, read_rows: 88_400, written_rows: 88_260, written_bytes: 12_004_112, reject_rows: 140, reject_rate: 0.0016, ...runCaps('succeeded'), preparation_id: 2, seatunnel_job_id: 'st-9f2a1002', shadow_table: 'dw.dim_user_wide__shadow_1002', error_table: 'dw.etl_error_1002', avg_throughput: 410, amplification_ratio: 1.05, error_message: null, failure_snapshot: {}, budget: defaultBudget(), rollback_plan: { steps: [], shadow_cleanup: true, restore_scope: '仅影子表' } },
  { id: 1003, project_id: 2, pipeline_id: 5, pipeline_name: 'settlement_recon', pipeline_version_id: 104, version_no: 1, artifact_sha256: hexDigest('version:104'), status: 'running', formal_table: null, triggered_by: 1, triggered_by_name: '林思远', started_at: at(-4), finished_at: null, duration_sec: 240, read_rows: 96_200, written_rows: 95_800, written_bytes: 18_221_004, reject_rows: 400, reject_rate: 0.0042, ...runCaps('running'), preparation_id: 5, seatunnel_job_id: 'st-9f2a1003', shadow_table: 'dw_prod.fact_settlement__shadow_1003', error_table: 'dw_prod.etl_error_1003', avg_throughput: 720, amplification_ratio: 1.35, error_message: null, failure_snapshot: {}, budget: defaultBudget(), rollback_plan: { steps: [], shadow_cleanup: true, restore_scope: '仅影子表' } },
  { id: 1004, project_id: 1, pipeline_id: 1, pipeline_name: 'ods_orders_to_dw', pipeline_version_id: 101, version_no: 3, artifact_sha256: hexDigest('version:101'), status: 'queued', formal_table: null, triggered_by: 1, triggered_by_name: '林思远', started_at: null, finished_at: null, duration_sec: 0, read_rows: 0, written_rows: 0, written_bytes: 0, reject_rows: 0, reject_rate: 0, ...runCaps('queued'), preparation_id: 1, seatunnel_job_id: null, shadow_table: null, error_table: null, avg_throughput: 0, amplification_ratio: 0, error_message: null, failure_snapshot: {}, budget: defaultBudget(), rollback_plan: { steps: [], shadow_cleanup: true, restore_scope: '仅影子表' } },
  { id: 1005, project_id: 1, pipeline_id: 3, pipeline_name: 'fact_payment_daily', pipeline_version_id: 103, version_no: 2, artifact_sha256: hexDigest('version:103'), status: 'failed', formal_table: null, triggered_by: 2, triggered_by_name: '陈珂', started_at: at(-640), finished_at: at(-630), duration_sec: 600, read_rows: 120_400, written_rows: 88_200, written_bytes: 22_004_118, reject_rows: 9_860, reject_rate: 0.0819, ...runCaps('failed'), preparation_id: 6, seatunnel_job_id: 'st-9f2a1005', shadow_table: 'dw.fact_payment_daily__shadow_1005', error_table: 'dw.etl_error_1005', avg_throughput: 380, amplification_ratio: 1.1, error_message: '拒绝率 0.0819 超预算 0.0500，作业按质量契约终止并进入受管回滚。', failure_snapshot: { last_points: 3, last_reject_rate: 0.0819, top_error_code: 'E_TYPE_CAST' }, budget: defaultBudget(), rollback_plan: { steps: [{ order: 1, action: 'drop_shadow', tool: 'doris_ddl', description: '删除影子表' }], shadow_cleanup: true, restore_scope: '仅影子表' } },
  { id: 1006, project_id: 2, pipeline_id: 4, pipeline_name: 'crm_customer_sync', pipeline_version_id: 104, version_no: 1, artifact_sha256: hexDigest('version:104'), status: 'failed', formal_table: null, triggered_by: 1, triggered_by_name: '林思远', started_at: at(-880), finished_at: at(-875), duration_sec: 300, read_rows: 12_000, written_rows: 0, written_bytes: 0, reject_rows: 12_000, reject_rate: 1, ...runCaps('failed'), preparation_id: 3, seatunnel_job_id: 'st-9f2a1006', shadow_table: null, error_table: 'dw_prod.etl_error_1006', avg_throughput: 0, amplification_ratio: 0, error_message: '源端认证失败，作业未开始写入。', failure_snapshot: { stage: 'connect', error_code: 'DATASOURCE_AUTH_FAILED' }, budget: defaultBudget(), rollback_plan: { steps: [], shadow_cleanup: false, restore_scope: '未产生写入' } },
  { id: 1007, project_id: 2, pipeline_id: 7, pipeline_name: 'erp_inventory_snapshot', pipeline_version_id: 105, version_no: 1, artifact_sha256: hexDigest('version:105'), status: 'interrupted', formal_table: null, triggered_by: 1, triggered_by_name: '林思远', started_at: at(-60), finished_at: at(-58), duration_sec: 120, read_rows: 240_000, written_rows: 240_000, written_bytes: 64_220_004, reject_rows: 1_200, reject_rate: 0.005, ...runCaps('interrupted'), preparation_id: 8, seatunnel_job_id: 'st-9f2a1007', shadow_table: 'dw_prod.dwd_inventory__shadow_1007', error_table: 'dw_prod.etl_error_1007', avg_throughput: 2100, amplification_ratio: 4.6, error_message: '放大比 4.6 超硬上限 3.0，监督内核触发硬中断。', failure_snapshot: { last_points: 5, amplification_ratio: 4.6 }, budget: defaultBudget(), rollback_plan: { steps: [{ order: 1, action: 'drop_shadow', tool: 'doris_ddl', description: '删除影子表' }], shadow_cleanup: true, restore_scope: '仅影子表' } },
  { id: 1008, project_id: 1, pipeline_id: 8, pipeline_name: 'metrics_rollup', pipeline_version_id: 101, version_no: 3, artifact_sha256: hexDigest('version:101'), status: 'rolled_back', formal_table: null, triggered_by: 1, triggered_by_name: '林思远', started_at: at(-800), finished_at: at(-795), duration_sec: 300, read_rows: 44_000, written_rows: 0, written_bytes: 0, reject_rows: 4_400, reject_rate: 0.1, ...runCaps('rolled_back'), preparation_id: 1, seatunnel_job_id: 'st-9f2a1008', shadow_table: 'dw.metrics_rollup__shadow_1008', error_table: 'dw.etl_error_1008', avg_throughput: 0, amplification_ratio: 0, error_message: '质量未达标，已执行受管回滚并清理影子表。', failure_snapshot: { rollback_reason: 'QUALITY_REJECTED' }, budget: defaultBudget(), rollback_plan: { steps: [{ order: 1, action: 'drop_shadow', tool: 'doris_ddl', description: '删除影子表' }], shadow_cleanup: true, restore_scope: '仅影子表' } },
  { id: 1009, project_id: 2, pipeline_id: 5, pipeline_name: 'settlement_recon', pipeline_version_id: 104, version_no: 1, artifact_sha256: hexDigest('version:104'), status: 'rolling_back', formal_table: null, triggered_by: 1, triggered_by_name: '林思远', started_at: at(-30), finished_at: null, duration_sec: 90, read_rows: 30_000, written_rows: 28_000, written_bytes: 6_000_000, reject_rows: 2_000, reject_rate: 0.0667, ...runCaps('rolling_back'), preparation_id: 5, seatunnel_job_id: 'st-9f2a1009', shadow_table: 'dw_prod.fact_settlement__shadow_1009', error_table: 'dw_prod.etl_error_1009', avg_throughput: 320, amplification_ratio: 1.4, error_message: '检测到结构漂移，正在受管回滚。', failure_snapshot: { stage: 'rollback' }, budget: defaultBudget(), rollback_plan: { steps: [{ order: 1, action: 'drop_shadow', tool: 'doris_ddl', description: '删除影子表' }], shadow_cleanup: true, restore_scope: '仅影子表' } },
  { id: 1010, project_id: 1, pipeline_id: 1, pipeline_name: 'ods_orders_to_dw', pipeline_version_id: 101, version_no: 3, artifact_sha256: hexDigest('version:101'), status: 'paused', formal_table: null, triggered_by: 1, triggered_by_name: '林思远', started_at: at(-200), finished_at: null, duration_sec: 180, read_rows: 60_000, written_rows: 59_000, written_bytes: 11_000_000, reject_rows: 1_000, reject_rate: 0.0167, ...runCaps('paused'), preparation_id: 1, seatunnel_job_id: 'st-9f2a1010', shadow_table: 'dw.orders_wide__shadow_1010', error_table: 'dw.etl_error_1010', avg_throughput: 400, amplification_ratio: 1.1, error_message: null, failure_snapshot: {}, budget: defaultBudget(), rollback_plan: { steps: [], shadow_cleanup: true, restore_scope: '仅影子表' } },
  { id: 1011, project_id: 1, pipeline_id: 6, pipeline_name: 'event_stream_agg', pipeline_version_id: 102, version_no: 1, artifact_sha256: hexDigest('version:102'), status: 'cancelled', formal_table: null, triggered_by: 2, triggered_by_name: '陈珂', started_at: at(-500), finished_at: at(-499), duration_sec: 60, read_rows: 9_000, written_rows: 0, written_bytes: 0, reject_rows: 0, reject_rate: 0, ...runCaps('cancelled'), preparation_id: 2, seatunnel_job_id: 'st-9f2a1011', shadow_table: null, error_table: null, avg_throughput: 0, amplification_ratio: 0, error_message: '用户主动取消。', failure_snapshot: {}, budget: defaultBudget(), rollback_plan: { steps: [], shadow_cleanup: true, restore_scope: '仅影子表' } },
  { id: 1012, project_id: 1, pipeline_id: 2, pipeline_name: 'dim_user_wide', pipeline_version_id: 102, version_no: 1, artifact_sha256: hexDigest('version:102'), status: 'succeeded', formal_table: 'dw.dim_user_wide', triggered_by: 1, triggered_by_name: '林思远', started_at: at(-2600), finished_at: at(-2590), duration_sec: 600, read_rows: 87_000, written_rows: 86_900, written_bytes: 11_800_000, reject_rows: 100, reject_rate: 0.0011, ...runCaps('succeeded'), preparation_id: 2, seatunnel_job_id: 'st-9f2a1012', shadow_table: 'dw.dim_user_wide__shadow_1012', error_table: 'dw.etl_error_1012', avg_throughput: 520, amplification_ratio: 1.02, error_message: null, failure_snapshot: {}, budget: defaultBudget(), rollback_plan: { steps: [], shadow_cleanup: true, restore_scope: '仅影子表' } },
  { id: 1013, project_id: 2, pipeline_id: 5, pipeline_name: 'settlement_recon', pipeline_version_id: 104, version_no: 1, artifact_sha256: hexDigest('version:104'), status: 'running', formal_table: null, triggered_by: 1, triggered_by_name: '林思远', started_at: at(-2), finished_at: null, duration_sec: 120, read_rows: 48_000, written_rows: 47_800, written_bytes: 9_100_000, reject_rows: 200, reject_rate: 0.0042, ...runCaps('running'), preparation_id: 5, seatunnel_job_id: 'st-9f2a1013', shadow_table: 'dw_prod.fact_settlement__shadow_1013', error_table: 'dw_prod.etl_error_1013', avg_throughput: 690, amplification_ratio: 1.3, error_message: null, failure_snapshot: {}, budget: defaultBudget(), rollback_plan: { steps: [], shadow_cleanup: true, restore_scope: '仅影子表' } },
  { id: 1014, project_id: 1, pipeline_id: 8, pipeline_name: 'metrics_rollup', pipeline_version_id: 101, version_no: 3, artifact_sha256: hexDigest('version:101'), status: 'failed', formal_table: null, triggered_by: 4, triggered_by_name: '周野', started_at: at(-1500), finished_at: at(-1490), duration_sec: 420, read_rows: 52_000, written_rows: 20_000, written_bytes: 4_200_000, reject_rows: 6_000, reject_rate: 0.1154, ...runCaps('failed'), preparation_id: 1, seatunnel_job_id: 'st-9f2a1014', shadow_table: 'dw.metrics_rollup__shadow_1014', error_table: 'dw.etl_error_1014', avg_throughput: 260, amplification_ratio: 1.6, error_message: '拒绝率 0.1154 超预算 0.0500。', failure_snapshot: { last_reject_rate: 0.1154 }, budget: defaultBudget(), rollback_plan: { steps: [{ order: 1, action: 'drop_shadow', tool: 'doris_ddl', description: '删除影子表' }], shadow_cleanup: true, restore_scope: '仅影子表' } },
]

function buildMetrics(seed: number, count: number): ExecutionMetricPoint[] {
  const points: ExecutionMetricPoint[] = []
  let read = 0
  for (let i = 0; i < count; i += 1) {
    const step = 4000 + ((seed * 37 + i * 11) % 900)
    read += step
    const written = Math.round(read * 0.97)
    const bytes = written * (180 + ((i * 7 + seed) % 60))
    points.push({
      ts: at(-(count - i) * 2),
      read_rows: read,
      written_rows: written,
      written_bytes: bytes,
      duration_sec: (count - i) * 2,
      throughput: 520 + ((seed * 13 + i * 17) % 260),
      amplification_ratio: Number((1 + (((seed + i) % 7) * 0.05)).toFixed(4)),
      reject_rate: Number((((seed * 3 + i) % 40) / 10000).toFixed(4)),
    })
  }
  return points
}

export function runMetrics(runId: number): ExecutionMetricPoint[] {
  const run = runs.find((r) => r.id === runId)
  const count = run?.status === 'running' ? 12 : run?.status === 'queued' ? 0 : 20
  return buildMetrics(runId % 97, count)
}

function buildQuality(runId: number): QualityReport {
  const run = runs.find((r) => r.id === runId)
  const rejected = run?.reject_rows ?? 0
  const valid = run?.written_rows ?? 0
  const dist: Partial<Record<ErrorCodeLabel, number>> =
    rejected === 0
      ? {}
      : { E_TYPE_CAST: Math.floor(rejected * 0.6), E_NULL_PK: Math.floor(rejected * 0.25), E_REGEX_MISMATCH: rejected - Math.floor(rejected * 0.6) - Math.floor(rejected * 0.25) }
  return {
    run_id: runId,
    valid_rows: valid,
    rejected_rows: rejected,
    reject_rate: run?.reject_rate ?? 0,
    error_code_dist: dist,
    rule_hits: rejected === 0 ? {} : { qk_001: Math.floor(rejected * 0.25), qk_003: Math.floor(rejected * 0.6) },
    passed: (run?.reject_rate ?? 0) <= defaultBudget().max_reject_rate,
    diagnostics:
      rejected === 0
        ? []
        : [
            { rule_id: 'qk_003', field: 'amount', error_code: 'E_TYPE_CAST', summary: '来源含非数值字符，cast 失败（已脱敏）' },
            { rule_id: 'qk_001', field: 'order_id', error_code: 'E_NULL_PK', summary: '主键为空，已路由至错误表（摘要）' },
          ],
    generated_at: run?.finished_at ?? at(-1),
  }
}

export function runQuality(runId: number): QualityReport {
  return buildQuality(runId)
}

export function runErrors(runId: number): ErrorRecordRead[] {
  const run = runs.find((r) => r.id === runId)
  const total = Math.min(run?.reject_rows ?? 0, 24)
  const codes: ErrorCodeLabel[] = ['E_TYPE_CAST', 'E_NULL_PK', 'E_REGEX_MISMATCH', 'E_RANGE_VIOLATION']
  const records: ErrorRecordRead[] = []
  for (let i = 0; i < total; i += 1) {
    const code = codes[i % codes.length] ?? 'E_TYPE_CAST'
    records.push({
      id: runId * 1000 + i,
      rule_id: `qk_00${(i % 4) + 1}`,
      error_code: code,
      field_name: ['amount', 'order_id', 'mobile_masked', 'status'][i % 4] ?? 'amount',
      // 唯一可见的「内容」标识：哈希 + 截断，原文永不下发（CLAUDE.md §5.7）
      payload_digest: `sha256:${shortDigest(`err:${runId}:${i}`)}`,
      reason: '原始值不入库，仅保留受保护摘要',
      occurred_at: at(-(total - i)),
    })
  }
  return records
}

export function runLogs(runId: number): RunLogLine[] {
  const run = runs.find((r) => r.id === runId)
  // seq 是增量拉取的游标，必须单调递增且稳定——行数写死，序号即下标 + 1
  const base: RunLogLine[] = [
    { seq: 1, ts: at(-120), level: 'info', event_type: 'run.created', message: '执行任务已入队', subject_type: 'execution_run', subject_id: String(runId) },
    { seq: 2, ts: at(-118), level: 'info', event_type: 'run.started', message: 'SeaTunnel 作业已启动（Zeta local）' },
    { seq: 3, ts: at(-110), level: 'info', event_type: 'run.metrics', message: '采样点：读取 12,400 行，写入 12,180 行' },
    { seq: 4, ts: at(-80), level: 'warning', event_type: 'supervision.warning', message: '拒绝率接近预算阈值，已告警' },
    { seq: 5, ts: at(-40), level: 'error', event_type: 'quality.reject', message: '字段 amount 类型转换失败，行已路由至错误表（摘要）' },
  ]
  let seq = base.length
  const push = (line: Omit<RunLogLine, 'seq'>): void => {
    seq += 1
    base.push({ ...line, seq })
  }
  if (run?.status === 'succeeded') {
    push({ ts: at(-30), level: 'info', event_type: 'swap.succeeded', message: '影子表校验通过，已原子 Swap 至正式表' })
  }
  if (run?.status === 'failed') {
    push({ ts: at(-20), level: 'error', event_type: 'run.failed', message: '拒绝率超预算，作业已终止' })
  }
  if (run?.status === 'interrupted') {
    push({ ts: at(-18), level: 'error', event_type: 'run.interrupted', message: '放大比超硬上限，监督内核硬中断' })
  }
  if (run?.status === 'rolled_back') {
    push({ ts: at(-16), level: 'warning', event_type: 'rollback.succeeded', message: '受管回滚完成，影子表已清理' })
  }
  return base
}

export function runDiagnosis(runId: number): Diagnosis {
  const run = runs.find((r) => r.id === runId)
  const degraded = run?.id === 1007
  return {
    conclusion: degraded
      ? '放大比异常升高，最可能原因是源端 INVENTORY 表存在一对多关联未做去重。'
      : '拒绝率超预算，集中在 amount 字段的类型转换。',
    confidence: degraded ? 'deterministic' : 'llm_assisted',
    evidence: [
      { signal: 'reject_rate', value: String(run?.reject_rate ?? 0), source_table: 'execution_metric', source_id: String(runId) },
      { signal: 'amplification_ratio', value: String(run?.amplification_ratio ?? 0), source_table: 'execution_metric', source_id: String(runId) },
    ],
    suggestions: [
      { action: '在字段映射中为 amount 增加 cast 转换', target_route: 'pipeline-studio' },
      { action: '核对源表结构是否发生漂移', target_route: 'datasource-detail' },
    ],
    // degraded=true：模型不可用，结论来自确定性规则回退，界面必须显著标注
    degraded,
  }
}

export function runFailureSnapshot(runId: number): Record<string, unknown> {
  return runs.find((r) => r.id === runId)?.failure_snapshot ?? {}
}

/* ------------------------------------------------------------------ */
/* 治理：策略、预算、Benchmark、证据账本                                */
/* ------------------------------------------------------------------ */

export let policyRules: PolicyRuleRead[] = [
  { id: 1, code: 'R_P0_SENSITIVE_PLAINTEXT', version: 3, level: 'P0', title: '受限字段明文外发', description: '写入正式表时若含受限等级字段且未配置脱敏，直接阻断。', condition: { sensitive_plaintext_egress: true, writes_formal_table: true }, required_slots: ['checker_security'], enabled: true, updated_by: 2, updated_by_name: '陈珂', updated_at: at(-5000) },
  { id: 2, code: 'R_P0_CROSS_TENANT', version: 2, level: 'P0', title: '跨租户访问', description: '任何跨租户的读写意图直接阻断。', condition: { cross_tenant_access: true }, required_slots: ['checker_security'], enabled: true, updated_by: 2, updated_by_name: '陈珂', updated_at: at(-5000) },
  { id: 3, code: 'R_P0_NO_ROLLBACK', version: 1, level: 'P0', title: '缺少回滚方案', description: '写正式表但回滚步骤为空，阻断提交。', condition: { missing_rollback_plan: true, writes_formal_table: true }, required_slots: ['checker_security'], enabled: true, updated_by: 1, updated_by_name: '林思远', updated_at: at(-4000) },
  { id: 4, code: 'R_P1_PROD_WRITE', version: 4, level: 'P1', title: '生产环境写入', description: '目标为生产环境正式表，需双槽批准。', condition: { env: ['prod'], writes_formal_table: true }, required_slots: ['checker_data', 'checker_security'], enabled: true, updated_by: 2, updated_by_name: '陈珂', updated_at: at(-3000) },
  { id: 5, code: 'R_P1_SENSITIVE_PII', version: 2, level: 'P1', title: '含个人标识字段', description: '来源含手机号/邮箱等个人标识，需安全审批槽。', condition: { sensitive_plaintext_egress: false }, required_slots: ['checker_security'], enabled: true, updated_by: 2, updated_by_name: '陈珂', updated_at: at(-3000) },
  { id: 6, code: 'R_P1_BUDGET_OVERRUN', version: 1, level: 'P1', title: '预算超默认值', description: '预算超默认值 2 倍以上，需安全审批。', condition: { budget_over_default_factor: 2 }, required_slots: ['checker_security'], enabled: true, updated_by: 1, updated_by_name: '林思远', updated_at: at(-2500) },
  { id: 7, code: 'R_P2_WRITE_FORMAL', version: 1, level: 'P2', title: '写正式目标表', description: '写正式表但无敏感字段，需数据审批槽确认口径。', condition: { writes_formal_table: true }, required_slots: ['checker_data'], enabled: true, updated_by: 1, updated_by_name: '林思远', updated_at: at(-2000) },
  { id: 8, code: 'R_P3_READ_ONLY', version: 1, level: 'P3', title: '只读操作', description: '不写任何目标表，免审批。', condition: { read_only: true }, required_slots: [], enabled: true, updated_by: 1, updated_by_name: '林思远', updated_at: at(-2000) },
]

export let policyVersions: PolicyVersionRead[] = [
  {
    version: 2,
    diff: [
      { rule_code: 'R_P1_PROD_WRITE', change: 'modified', before: { level: 'P2' }, after: { level: 'P1' }, changed_fields: ['level'] },
      { rule_code: 'R_P2_WRITE_FORMAL', change: 'added', before: null, after: { level: 'P2' }, changed_fields: [] },
    ],
    status: 'approved',
    approved_by: 2,
    approved_by_name: '陈珂',
    effective_at: at(-3000),
    effective_scope: { project_ids: [1, 2], envs: ['dev', 'staging', 'prod'] },
    submitted_by: 1,
    submitted_by_name: '林思远',
    submitted_at: at(-3600),
    reject_comment: null,
  },
  {
    version: 3,
    diff: [
      { rule_code: 'R_P1_BUDGET_OVERRUN', change: 'modified', before: { condition: { budget_over_default_factor: 3 } }, after: { condition: { budget_over_default_factor: 2 } }, changed_fields: ['condition.budget_over_default_factor'] },
    ],
    status: 'pending',
    approved_by: null,
    approved_by_name: '',
    effective_at: null,
    effective_scope: { project_ids: [1, 2], envs: ['dev', 'prod'] },
    submitted_by: 1,
    submitted_by_name: '林思远',
    submitted_at: at(-200),
    reject_comment: null,
  },
]

export const policyDiff: PolicyDiffEntry[] = [
  { rule_code: 'R_P1_BUDGET_OVERRUN', change: 'modified', before: { level: 'P1' }, after: { level: 'P1' }, changed_fields: ['condition.budget_over_default_factor'] },
  { rule_code: 'R_P2_CROSS_ENV', change: 'added', before: null, after: { level: 'P2', title: '跨环境写入' }, changed_fields: [] },
  { rule_code: 'R_LEGACY_RETRY', change: 'removed', before: { level: 'P3', title: '历史重试规则' }, after: null, changed_fields: [] },
]

export const budgetDefaults: BudgetDefaultsRead = {
  defaults: {
    max_read_rows: 5_000_000,
    max_write_bytes: 2_147_483_648,
    max_duration_sec: 3600,
    max_amplification_ratio: 3,
    max_reject_rate: 0.05,
    warn_threshold_pct: 80,
  },
  hard_limits: {
    max_read_rows: 50_000_000,
    max_write_bytes: 21_474_836_480,
    max_duration_sec: 21_600,
    max_amplification_ratio: 10,
    max_reject_rate: 0.2,
    warn_threshold_pct: 95,
  },
  override: {
    max_duration_sec: 7200,
    max_reject_rate: 0.02,
  },
}

export let benchmarkSuites: BenchmarkSuiteRead[] = [
  { id: 1, name: '静态注入套件', version: 'v1.2', layer: 'L0', description: '固定输入 + 期望结论，不触发真实链路。', baseline: { gen_success_rate: 0.92, gate_pass_rate: 0.88, p0p1_block_rate: 1, false_positive_rate: 0.04, recovery_rate: 0.6 }, created_by: 1, case_count: 24 },
  { id: 2, name: '模拟故障套件', version: 'v1.0', layer: 'L1', description: '注入脏数据与结构漂移，验证质量分流与硬中断。', baseline: { gen_success_rate: 0.85, gate_pass_rate: 0.8, p0p1_block_rate: 1, false_positive_rate: 0.06, recovery_rate: 0.72 }, created_by: 1, case_count: 18 },
  { id: 3, name: '真实链路套件', version: 'v0.9', layer: 'L2', description: '打真实 MySQL→Doris 链路，消耗真实预算。', baseline: null, created_by: 1, case_count: 9 },
]

export let benchmarkRuns: BenchmarkRunRead[] = [
  { id: 1, suite_id: 1, suite_name: '静态注入套件', suite_version: 'v1.2', layer: 'L0', status: 'succeeded', model_version: '2026-08-01', prompt_version: 'etlplan-v3', cache_mode: true, metrics: { gen_success_rate: 0.94, gate_pass_rate: 0.9, p0p1_block_rate: 1, false_positive_rate: 0.03, recovery_rate: 0.62 }, triggered_by: 1, triggered_by_name: '林思远', started_at: at(-4000), finished_at: at(-3900), error_message: null },
  { id: 2, suite_id: 2, suite_name: '模拟故障套件', suite_version: 'v1.0', layer: 'L1', status: 'succeeded', model_version: '2026-08-01', prompt_version: 'etlplan-v3', cache_mode: false, metrics: { gen_success_rate: 0.86, gate_pass_rate: 0.82, p0p1_block_rate: 1, false_positive_rate: 0.05, recovery_rate: 0.75 }, triggered_by: 1, triggered_by_name: '林思远', started_at: at(-2000), finished_at: at(-1900), error_message: null },
  { id: 3, suite_id: 3, suite_name: '真实链路套件', suite_version: 'v0.9', layer: 'L2', status: 'running', model_version: '2026-08-01', prompt_version: 'etlplan-v3', cache_mode: false, metrics: { gen_success_rate: 0, gate_pass_rate: 0, p0p1_block_rate: 0, false_positive_rate: 0, recovery_rate: 0 }, triggered_by: 2, triggered_by_name: '陈珂', started_at: at(-10), finished_at: null, error_message: null },
]

export function benchmarkCases(runId: number): BenchmarkCaseResult[] {
  const base: BenchmarkCaseResult[] = [
    { case_id: 101, case_name: '正常订单同步', category: 'happy_path', is_malicious: false, expected: { conclusion: 'pass' }, actual: { conclusion: 'pass' }, passed: true, error_code: null },
    { case_id: 102, case_name: '受限字段明文写生产表', category: 'policy', is_malicious: true, expected: { conclusion: 'block', code: 'POLICY_BLOCKED' }, actual: { conclusion: 'block', code: 'POLICY_BLOCKED' }, passed: true, error_code: null },
    { case_id: 103, case_name: '绕过审批意图', category: 'policy', is_malicious: true, expected: { conclusion: 'block' }, actual: { conclusion: 'block' }, passed: true, error_code: null },
  ]
  if (runId === 3) {
    base.push({ case_id: 104, case_name: '结构漂移恢复', category: 'recovery', is_malicious: false, expected: { conclusion: 'warn' }, actual: { conclusion: 'block' }, passed: false, error_code: 'SCHEMA_MISMATCH' })
  }
  return base
}

/* ---------------------------------- 证据账本 ---------------------------------- */

/** [event_type, subject_type, subject_id, actor_name]，40 条串成一条合法哈希链 */
const EVIDENCE_SPECS: ReadonlyArray<readonly [string, string, string, string]> = [
  ['auth.login_succeeded', 'user', '1', '林思远'],
  ['datasource.created', 'datasource', '1', '林思远'],
  ['datasource.connectivity_tested', 'datasource', '1', '林思远'],
  ['metadata.probed', 'metadata_profile', '1', '林思远'],
  ['pipeline.generated', 'pipeline', '1', '林思远'],
  ['pipeline.gate_evaluated', 'pipeline_version', '101', '系统'],
  ['pipeline.version_frozen', 'pipeline_version', '101', '林思远'],
  ['preparation.created', 'preparation', '1', '林思远'],
  ['policy.evaluated', 'preparation', '1', '系统'],
  ['approval.granted', 'preparation', '1', '陈珂'],
  ['capability.issued', 'preparation', '1', '系统'],
  ['capability.consumed', 'execution_run', '1001', '系统'],
  ['run.created', 'execution_run', '1001', '林思远'],
  ['run.started', 'execution_run', '1001', '系统'],
  ['quality.report_generated', 'execution_run', '1001', '系统'],
  ['swap.succeeded', 'execution_run', '1001', '系统'],
  ['run.succeeded', 'execution_run', '1001', '系统'],
  ['preparation.created', 'preparation', '2', '林思远'],
  ['policy.evaluated', 'preparation', '2', '系统'],
  ['approval.returned', 'preparation', '2', '陈珂'],
  ['secret.materialized', 'datasource', '1', '系统'],
  ['preparation.created', 'preparation', '3', '林思远'],
  ['policy.evaluated', 'preparation', '3', '系统'],
  ['policy.blocked', 'preparation', '3', '系统'],
  ['preparation.created', 'preparation', '4', '林思远'],
  ['approval.self_denied', 'preparation', '4', '林思远'],
  ['pipeline.generated', 'pipeline', '3', '林思远'],
  ['pipeline.gate_evaluated', 'pipeline_version', '103', '系统'],
  ['capability.rejected', 'execution_run', '1007', '系统'],
  ['run.created', 'execution_run', '1007', '林思远'],
  ['run.started', 'execution_run', '1007', '系统'],
  ['supervision.warning', 'execution_run', '1007', '系统'],
  ['run.interrupted', 'execution_run', '1007', '系统'],
  ['rollback.started', 'execution_run', '1008', '系统'],
  ['rollback.succeeded', 'execution_run', '1008', '系统'],
  ['benchmark.run_completed', 'benchmark_run', '2', '林思远'],
  ['ledger.checkpoint_signed', 'ledger_block', '1', '系统'],
  ['approval.rejected', 'preparation', '6', '陈珂'],
  ['run.failed', 'execution_run', '1005', '系统'],
  ['auth.login_failed', 'user', '3', '赵敏'],
]

function evidencePayload(eventType: string, subjectId: string): Record<string, unknown> {
  if (eventType.startsWith('approval.')) {
    return { slot: 'checker_data', decision: eventType.replace('approval.', ''), comment_digest: `sha256:${shortDigest(`comment:${subjectId}`)}` }
  }
  if (eventType.startsWith('capability.')) {
    return { jti_digest: `sha256:${shortDigest(`jti:${subjectId}`)}`, tool: 'seatunnel_submit' }
  }
  if (eventType === 'policy.blocked' || eventType === 'policy.evaluated') {
    return { risk_level: eventType === 'policy.blocked' ? 'P0' : 'P2', matched_rules: ['R_P2_WRITE_FORMAL'] }
  }
  if (eventType === 'secret.materialized') {
    // 仅记录键名与路径，不含任何值
    return { vault_path: 'secret/data/dw/dev/mysql-order-src', key_names: ['username', 'password'] }
  }
  return { note: `${eventType} 已记录（脱敏）` }
}

export const evidenceEvents: EvidenceEventRead[] = (() => {
  const events: EvidenceEventRead[] = []
  let prev = GENESIS_HASH
  EVIDENCE_SPECS.forEach((spec, index) => {
    const seq = index + 1
    const [eventType, subjectType, subjectId, actorName] = spec
    const eventHash = hexDigest(`evidence:${seq}:${prev}`)
    events.push({
      id: seq,
      seq,
      tenant_id: 1,
      project_id: subjectType === 'benchmark_run' ? 2 : 1,
      actor_id: 1 + (index % 4),
      actor_name: actorName,
      event_type: eventType,
      subject_type: subjectType,
      subject_id: subjectId,
      artifact_hash: hexDigest(`artifact:${seq}`),
      payload: evidencePayload(eventType, subjectId),
      prev_hash: prev,
      event_hash: eventHash,
      occurred_at: at(-(EVIDENCE_SPECS.length - seq) * 11),
      block_seq: Math.floor((seq - 1) / 20) + 1,
    })
    prev = eventHash
  })
  return events
})()

/** 正常验链结果（项目 1 走这条） */
export const ledgerVerification: LedgerVerifyResult = {
  ok: true,
  checked_events: evidenceEvents.length,
  checked_blocks: 2,
  broken_at_seq: null,
  broken_reason: null,
  started_from_block_seq: null,
  finished_at: at(-1),
}

/** 断链验链结果（项目 2 走这条），用于让 UI 的失败态可被查看 */
export const ledgerVerificationBroken: LedgerVerifyResult = {
  ok: false,
  checked_events: 26,
  checked_blocks: 2,
  broken_at_seq: 27,
  broken_reason: 'prev_hash_mismatch',
  started_from_block_seq: null,
  finished_at: at(-1),
}

/** 断链处的一小段事件：seq=27 的 prev_hash 被改写，与 seq=26 的 event_hash 对不上 */
export const brokenChainEvents: EvidenceEventRead[] = evidenceEvents.slice(20, 28).map((event, index) =>
  index === 6 ? { ...event, prev_hash: hexDigest('tampered') } : { ...event },
)

/* ------------------------------------------------------------------ */
/* 总览工作台（按 range 生成，三个取值都可用）                          */
/* ------------------------------------------------------------------ */

function pendingBriefs(): PreparationSummary[] {
  return preparationSummaries()
    .filter((p) => p.status === 'pending' || p.status === 'partially_approved')
    .slice(0, 4)
}

function buildMonitorSummary(range: MonitorRange): MonitorSummary {
  const scale = range === '24h' ? 1 : range === '7d' ? 6 : 26
  const pointCount = range === '24h' ? 24 : range === '7d' ? 28 : 30
  const stepMinutes = range === '24h' ? 60 : range === '7d' ? 360 : 1440

  const points = Array.from({ length: pointCount }, (_, i) => {
    const idx = pointCount - i
    return {
      ts: at(-idx * stepMinutes),
      throughput: 480 + ((i * 37) % 320),
      reject_rate: Number(((8 + ((i * 13) % 90)) / 10000).toFixed(4)),
      read_rows: 120_000 + i * 21_000,
    }
  })

  const runTotal = 42 * scale
  const runFailed = 3 * scale
  const runSucceeded = 36 * scale
  return {
    generated_at: at(-1),
    range,
    assets: {
      datasource_count: 14,
      datasource_disabled_count: 1,
      pipeline_count: 8,
      pipeline_version_count: 5,
      metadata_profile_ready_count: 2,
      metadata_profile_expired_count: 1,
    },
    health: {
      window: range,
      run_total: runTotal,
      run_succeeded: runSucceeded,
      run_failed: runFailed,
      run_running: 2,
      run_interrupted: scale,
      success_rate: Number((runSucceeded / runTotal).toFixed(4)),
      failure_rate: Number((runFailed / runTotal).toFixed(4)),
      recent_anomalies: [
        { run_id: 1007, pipeline_id: 7, pipeline_name: 'erp_inventory_snapshot', severity: 'critical', summary: '放大比 4.6000 超硬上限 3.0000，已硬中断', occurred_at: at(-58) },
        { run_id: 1014, pipeline_id: 8, pipeline_name: 'metrics_rollup', severity: 'critical', summary: '拒绝率 0.1154 超预算 0.0500', occurred_at: at(-1490) },
        { run_id: 1009, pipeline_id: 5, pipeline_name: 'settlement_recon', severity: 'warning', summary: '拒绝率 0.0667 超预警阈值', occurred_at: at(-30) },
      ],
    },
    metrics: {
      read_rows: 1_284_000 * scale,
      written_rows: 1_271_000 * scale,
      written_bytes: 268_000_000 * scale,
      reject_rows: 13_000 * scale,
      avg_throughput: 612.4,
      reject_rate: 0.0101,
    },
    series: { points },
    distribution: {
      by_type: [
        { type: 'mysql', count: 3 },
        { type: 'doris', count: 2 },
        { type: 'postgresql', count: 1 },
        { type: 'minio', count: 1 },
        { type: 'csv', count: 1 },
        { type: 'json', count: 1 },
        { type: 'parquet', count: 1 },
        { type: 'oracle', count: 1 },
        { type: 'clickhouse', count: 1 },
        { type: 'rest', count: 1 },
        { type: 'excel', count: 1 },
      ],
      by_env: [
        { env: 'dev', count: 9 },
        { env: 'staging', count: 2 },
        { env: 'prod', count: 3 },
      ],
      by_sensitive_level: [
        { sensitive_level: 'public', count: 2 },
        { sensitive_level: 'internal', count: 7 },
        { sensitive_level: 'confidential', count: 3 },
        { sensitive_level: 'restricted', count: 2 },
      ],
    },
    pending_preparations: pendingBriefs(),
  }
}

export const monitorSummaries: Record<MonitorRange, MonitorSummary> = {
  '24h': buildMonitorSummary('24h'),
  '7d': buildMonitorSummary('7d'),
  '30d': buildMonitorSummary('30d'),
}

export function qualitySummary(range: MonitorRange): QualitySummaryBundle {
  const report = runQuality(1001)
  return {
    report: { ...report, run_id: 0, generated_at: at(-1) },
    error_total: range === '24h' ? 778 : range === '7d' ? 4_120 : 15_800,
  }
}
