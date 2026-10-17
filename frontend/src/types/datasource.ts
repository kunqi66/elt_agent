/**
 * 数据源与元数据探查类型（fe-03 / be-03 be-04）。
 *
 * 红线：本文件不得出现 password / token / secret 形态的**值**字段。
 * 凭据只以 SecretReferenceView（Vault 路径 + 版本 + 键名）出现，
 * 且只在新登记或「重新设置」时单向提交一次。
 */

import type { Env, SensitiveLevel } from './common'

export type DataSourceType =
  | 'mysql'
  | 'postgresql'
  | 'doris'
  | 'minio'
  | 'csv'
  | 'json'
  | 'parquet'
  | 'oracle'
  | 'clickhouse'
  | 'excel'
  | 'rest'

export type DataSourceStatus = 'active' | 'disabled'

export type ProbeStatus = 'probing' | 'ready' | 'failed' | 'expired'

export type SamplingScope = 'full_tables' | 'selected_tables' | 'schema_only'

/* ------------------------------------------------------------------ */
/* 数据源                                                              */
/* ------------------------------------------------------------------ */

/** 服务端只回引用与键名，永不回值 */
export interface SecretReferenceView {
  provider: 'vault'
  path: string
  version: number
  /** 如 ["username","password"]，仅键名 */
  key_names: string[]
  last_rotated_at: string | null
}

/**
 * endpoint 的非凭据键（host/port/database/bucket/path…）。
 * 类型上就堵死凭据键，配合提交前的黑名单校验形成双层防线（fe-03 §6.1）。
 */
export type DataSourceEndpoint = Record<string, string | number>

export interface DataSource {
  id: number
  project_id: number
  name: string
  type: DataSourceType
  env: Env
  sensitive_level: SensitiveLevel
  endpoint: DataSourceEndpoint
  secret_ref: SecretReferenceView | null
  tags: string[]
  owner_id: number | null
  owner_display_name: string | null
  status: DataSourceStatus
  last_tested_at: string | null
  last_test_ok: boolean | null
  /** 列表页「最近探查」列；后端 join 最近一条 Profile 得到 */
  latest_profile: MetadataProfileBrief | null
  created_at: string
  updated_at: string
}

export interface MetadataProfileBrief {
  version: number
  status: ProbeStatus
  tables_count: number
  created_at: string
  expires_at: string | null
}

/** 写请求体：创建/更新共用一个 payload，凭据键可选 */
export interface DataSourceUpsertPayload {
  name: string
  type: DataSourceType
  env: Env
  sensitive_level: SensitiveLevel
  endpoint: DataSourceEndpoint
  tags: string[]
  owner_id: number | null
  /** 仅「新建」或「重新设置凭据」时出现；其余情况整个键不提交（fe-03 §5.2） */
  credentials?: Record<string, string>
}

export interface DataSourceFilter {
  type?: DataSourceType[]
  env?: Env[]
  sensitive_level?: SensitiveLevel[]
  status?: DataSourceStatus
  q?: string
  page: number
  page_size: number
}

/* ------------------------------------------------------------------ */
/* 连通性测试                                                          */
/* ------------------------------------------------------------------ */

/** 四态：业务结果不是请求错误，故用 200 + state 表达（fe-03 §10 建议） */
export type ConnectivityState = 'pending' | 'succeeded' | 'failed' | 'timeout' | 'denied'

export interface ConnectivityResult {
  test_id: string
  state: ConnectivityState
  latency_ms: number | null
  /** 后端已掩码的主机:端口，如 10.**.**.5:3306 */
  target: string | null
  server_version: string | null
  error_code: string | null
  /** 后端已脱敏文案；前端仍会再过滤一次连接串形态 */
  message: string | null
  tested_at: string | null
}

/* ------------------------------------------------------------------ */
/* 元数据探查                                                          */
/* ------------------------------------------------------------------ */

export interface ProbeSampling {
  rows: number
  scope: SamplingScope
  tables: string[] | null
}

export interface MetadataProfile {
  id: number
  data_source_id: number
  version: number
  status: ProbeStatus
  sampling: ProbeSampling
  masking_policy: Record<string, unknown>
  /** 表名 → 行数组；值已在入库前脱敏，前端无从解敏 */
  masked_samples: Record<string, Array<Record<string, string>>>
  tables_count: number
  sha256: string
  expires_at: string | null
  error_message: string | null
  created_at: string
  created_by_display_name: string | null
}

export interface SchemaColumn {
  name: string
  data_type: string
  nullable: boolean
  is_primary_key: boolean
  comment: string | null
  /** numeric(6,4) */
  null_ratio: number | null
  distinct_approx: number | null
  sensitive_level: SensitiveLevel
  /** 该字段的样本是否已脱敏 */
  masked: boolean
}

export interface SchemaTable {
  schema: string
  name: string
  comment: string | null
  row_count_approx: number | null
  columns: SchemaColumn[]
}

export interface SchemaSnapshot {
  tables: SchemaTable[]
}

export interface ProbeRequest {
  sampling: ProbeSampling
  masking_policy: Record<string, unknown>
}

/* ------------------------------------------------------------------ */
/* 类型 → 字段表（fe-03 §6.1，前端唯一事实来源）                        */
/* ------------------------------------------------------------------ */

export interface EndpointFieldSpec {
  key: string
  label: string
  required: boolean
  placeholder?: string
  /** 数字型字段渲染 el-input-number */
  numeric?: boolean
  default?: string | number
}

export interface DataSourceTypeSpec {
  type: DataSourceType
  label: string
  /** false = 「仅注册不真连」，禁测试与探查，表单下方出黄条 */
  connectable: boolean
  endpointFields: EndpointFieldSpec[]
  /** Vault 内键名；空数组 = 该类型无凭据（如 csv/json） */
  credentialKeys: string[]
}

/**
 * endpoint 键黑名单：这些键属于凭据，必须进 Vault 而不是 endpoint。
 * 后端 schemas/ 同样拒绝，前端在提交前用同一份做防呆校验。
 */
export const CREDENTIAL_KEY_BLACKLIST: readonly string[] = [
  'password',
  'passwd',
  'pwd',
  'token',
  'access_key',
  'secret_key',
  'secret',
  'api_key',
  'bearer',
  'credentials',
]

export const DATA_SOURCE_TYPES: readonly DataSourceTypeSpec[] = [
  {
    type: 'mysql',
    label: 'MySQL',
    connectable: true,
    endpointFields: [
      { key: 'host', label: '主机', required: true, placeholder: '10.0.0.5' },
      { key: 'port', label: '端口', required: true, numeric: true, default: 3306 },
      { key: 'database', label: '数据库', required: true },
      { key: 'charset', label: '字符集（选填）', required: false, placeholder: 'utf8mb4' },
    ],
    credentialKeys: ['username', 'password'],
  },
  {
    type: 'postgresql',
    label: 'PostgreSQL',
    connectable: true,
    endpointFields: [
      { key: 'host', label: '主机', required: true },
      { key: 'port', label: '端口', required: true, numeric: true, default: 5432 },
      { key: 'database', label: '数据库', required: true },
      { key: 'schema', label: 'Schema（选填）', required: false, placeholder: 'public' },
    ],
    credentialKeys: ['username', 'password'],
  },
  {
    type: 'doris',
    label: 'Doris',
    connectable: true,
    endpointFields: [
      { key: 'host', label: 'FE 主机', required: true },
      { key: 'port', label: 'FE 端口', required: true, numeric: true, default: 9030 },
      { key: 'database', label: '数据库', required: true },
      { key: 'warehouse', label: 'Warehouse（选填）', required: false },
    ],
    credentialKeys: ['username', 'password'],
  },
  {
    type: 'minio',
    label: 'MinIO / 对象存储',
    connectable: true,
    endpointFields: [
      { key: 'endpoint', label: 'Endpoint（含 scheme）', required: true, placeholder: 'http://minio:9000' },
      { key: 'bucket', label: 'Bucket', required: true },
      { key: 'path', label: '前缀路径', required: true, placeholder: 'raw/orders/' },
    ],
    credentialKeys: ['access_key', 'secret_key'],
  },
  {
    type: 'csv',
    label: 'CSV 文件',
    connectable: true,
    endpointFields: [
      { key: 'path', label: '文件路径', required: true },
      { key: 'encoding', label: '编码', required: true, default: 'utf-8' },
      { key: 'delimiter', label: '分隔符', required: true, default: ',' },
      { key: 'has_header', label: '含表头（选填）', required: false, default: 'true' },
    ],
    credentialKeys: [],
  },
  {
    type: 'json',
    label: 'JSON 文件',
    connectable: true,
    endpointFields: [
      { key: 'path', label: '文件路径', required: true },
      { key: 'encoding', label: '编码', required: true, default: 'utf-8' },
      { key: 'json_path', label: '根节点定位（选填）', required: false, placeholder: '$.data.items' },
    ],
    credentialKeys: [],
  },
  {
    type: 'parquet',
    label: 'Parquet 文件',
    connectable: false,
    endpointFields: [{ key: 'path', label: '文件路径', required: true }],
    credentialKeys: [],
  },
  {
    type: 'oracle',
    label: 'Oracle',
    connectable: false,
    endpointFields: [
      { key: 'host', label: '主机', required: true },
      { key: 'port', label: '端口', required: true, numeric: true, default: 1521 },
      { key: 'service_name', label: 'Service Name', required: true },
    ],
    credentialKeys: ['username', 'password'],
  },
  {
    type: 'clickhouse',
    label: 'ClickHouse',
    connectable: false,
    endpointFields: [
      { key: 'host', label: '主机', required: true },
      { key: 'port', label: '端口', required: true, numeric: true, default: 8123 },
      { key: 'database', label: '数据库', required: true },
    ],
    credentialKeys: ['username', 'password'],
  },
  {
    type: 'excel',
    label: 'Excel 文件',
    connectable: false,
    endpointFields: [{ key: 'path', label: '文件路径', required: true }],
    credentialKeys: [],
  },
  {
    type: 'rest',
    label: 'REST 接口',
    connectable: false,
    endpointFields: [
      { key: 'base_url', label: 'Base URL', required: true },
      {
        key: 'auth_type',
        label: '认证方式',
        required: true,
        default: 'none',
        placeholder: 'none / basic / bearer / api_key',
      },
    ],
    credentialKeys: ['username', 'password', 'token', 'api_key'],
  },
]

/** 按 type 取字段表；未知 type 回退为「仅注册 + 无字段」，避免后端新增类型时白屏 */
export function dataSourceTypeSpec(type: string): DataSourceTypeSpec {
  const found = DATA_SOURCE_TYPES.find((s) => s.type === type)
  if (found !== undefined) {
    return found
  }
  return { type: type as DataSourceType, label: type, connectable: false, endpointFields: [], credentialKeys: [] }
}
