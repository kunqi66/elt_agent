/**
 * 枚举 → 中文标签 + 徽章配色的**唯一口径**（fe-01 §6.3）。
 *
 * 铁律：全前端只有这一份映射表。各 feature 页不得自行写标签或色值，
 * 否则同一状态会在不同页面出现两套说法/两套颜色。
 *
 * 兜底：未登记的取值返回 { label: 原始取值, tag: 'info', unknown: true }，
 * 由组件加 title="未知取值，后端可能已升级"。**绝不渲染空白、绝不抛异常**。
 */

import type { EnumKind, EnumLabel, RoleName } from '@/types'

type TagType = EnumLabel['tag']

interface Entry {
  label: string
  tag: TagType
}

type Table = Record<string, Entry>

/* ------------------------------------------------------------------ */
/* 基础维度                                                            */
/* ------------------------------------------------------------------ */

const ENV: Table = {
  dev: { label: '开发', tag: 'info' },
  staging: { label: '预发', tag: 'warning' },
  prod: { label: '生产', tag: 'danger' },
}

const ROLE_NAME: Table = {
  maker: { label: '方案发起人', tag: 'primary' },
  checker_data: { label: '数据审批人', tag: 'warning' },
  checker_security: { label: '安全审批人', tag: 'danger' },
  operator: { label: '执行操作员', tag: 'success' },
  auditor: { label: '审计员', tag: 'info' },
}

const USER_STATUS: Table = {
  active: { label: '正常', tag: 'success' },
  locked: { label: '已锁定', tag: 'danger' },
  disabled: { label: '已停用', tag: 'info' },
}

const PROJECT_STATUS: Table = {
  active: { label: '活跃', tag: 'success' },
  archived: { label: '已归档', tag: 'info' },
}

const MEMBER_STATUS: Table = {
  active: { label: '在册', tag: 'success' },
  removed: { label: '已移除', tag: 'info' },
}

/** 四级递进：公开 → 内部 → 机密 → 受限 */
const SENSITIVE_LEVEL: Table = {
  public: { label: '公开', tag: 'info' },
  internal: { label: '内部', tag: 'primary' },
  confidential: { label: '机密', tag: 'warning' },
  restricted: { label: '受限', tag: 'danger' },
}

/** P0 阻断 / P1 高风险 / P2 常规 / P3 只读免审批（PRD §15 判定矩阵） */
const RISK_LEVEL: Table = {
  P0: { label: 'P0 已阻断', tag: 'danger' },
  P1: { label: 'P1 高风险', tag: 'danger' },
  P2: { label: 'P2 常规', tag: 'warning' },
  P3: { label: 'P3 只读', tag: 'info' },
}

const APPROVAL_SLOT: Table = {
  checker_data: { label: '数据审批槽', tag: 'warning' },
  checker_security: { label: '安全审批槽', tag: 'danger' },
}

const APPROVAL_DECISION_TYPE: Table = {
  approve: { label: '通过', tag: 'success' },
  reject: { label: '拒绝', tag: 'danger' },
  return: { label: '退回补充', tag: 'warning' },
}

/* ------------------------------------------------------------------ */
/* 数据源与元数据                                                      */
/* ------------------------------------------------------------------ */

const DATA_SOURCE_TYPE: Table = {
  mysql: { label: 'MySQL', tag: 'primary' },
  postgresql: { label: 'PostgreSQL', tag: 'primary' },
  doris: { label: 'Doris', tag: 'primary' },
  minio: { label: 'MinIO', tag: 'primary' },
  csv: { label: 'CSV', tag: 'info' },
  json: { label: 'JSON', tag: 'info' },
  parquet: { label: 'Parquet', tag: 'info' },
  oracle: { label: 'Oracle', tag: 'info' },
  clickhouse: { label: 'ClickHouse', tag: 'info' },
  excel: { label: 'Excel', tag: 'info' },
  rest: { label: 'REST', tag: 'info' },
}

const PROBE_STATUS: Table = {
  probing: { label: '探查中', tag: 'warning' },
  ready: { label: '就绪', tag: 'success' },
  failed: { label: '探查失败', tag: 'danger' },
  expired: { label: '已过期', tag: 'warning' },
}

/* ------------------------------------------------------------------ */
/* Pipeline 与审批                                                     */
/* ------------------------------------------------------------------ */

const PIPELINE_STATUS: Table = {
  draft: { label: '草稿', tag: 'info' },
  generated: { label: '已生成', tag: 'primary' },
  gate_failed: { label: '门禁未通过', tag: 'danger' },
  version_frozen: { label: '已冻结版本', tag: 'primary' },
  preparing: { label: '准备中', tag: 'warning' },
  pending_approval: { label: '待审批', tag: 'warning' },
  approved: { label: '已批准', tag: 'success' },
  committable: { label: '可执行', tag: 'success' },
  queued: { label: '排队中', tag: 'info' },
  running: { label: '运行中', tag: 'primary' },
  succeeded: { label: '成功', tag: 'success' },
  failed: { label: '失败', tag: 'danger' },
  interrupted: { label: '监督硬中断', tag: 'danger' },
  rolling_back: { label: '回滚中', tag: 'warning' },
  rolled_back: { label: '已回滚', tag: 'info' },
  rejected: { label: '已拒绝', tag: 'danger' },
}

const PREPARATION_STATUS: Table = {
  pending: { label: '待审批', tag: 'warning' },
  partially_approved: { label: '部分通过', tag: 'warning' },
  approved: { label: '已通过', tag: 'success' },
  rejected: { label: '已拒绝', tag: 'danger' },
  expired: { label: '已过期', tag: 'danger' },
  superseded: { label: '已被取代', tag: 'info' },
}

/* ------------------------------------------------------------------ */
/* 运行与质量                                                          */
/* ------------------------------------------------------------------ */

const RUN_STATUS: Table = {
  queued: { label: '排队中', tag: 'info' },
  running: { label: '运行中', tag: 'primary' },
  succeeded: { label: '成功', tag: 'success' },
  failed: { label: '失败', tag: 'danger' },
  paused: { label: '已暂停', tag: 'warning' },
  cancelled: { label: '已取消', tag: 'info' },
  interrupted: { label: '监督硬中断', tag: 'danger' },
  rolling_back: { label: '回滚中', tag: 'warning' },
  rolled_back: { label: '已回滚', tag: 'info' },
}

/** 质量分流错误码；与 fe-06 §6 的 errorCodeLabel.ts 同一份口径，故并入此处 */
const ERROR_CODE_LABEL: Table = {
  E_TYPE_CAST: { label: '类型转换失败', tag: 'warning' },
  E_NULL_PK: { label: '主键为空', tag: 'danger' },
  E_REGEX_MISMATCH: { label: '正则校验不通过', tag: 'warning' },
  E_RANGE_VIOLATION: { label: '取值范围越界', tag: 'warning' },
  E_ENUM_INVALID: { label: '枚举取值非法', tag: 'warning' },
  E_DUPLICATE_KEY: { label: '主键重复', tag: 'danger' },
  E_SENSITIVE_PLAINTEXT: { label: '敏感字段明文', tag: 'danger' },
  E_SCHEMA_DRIFT: { label: '源端结构漂移', tag: 'danger' },
}

/* ------------------------------------------------------------------ */
/* 治理与 Benchmark                                                    */
/* ------------------------------------------------------------------ */

const BENCHMARK_LAYER: Table = {
  L0: { label: 'L0 静态注入', tag: 'info' },
  L1: { label: 'L1 模拟故障', tag: 'warning' },
  L2: { label: 'L2 真实链路', tag: 'danger' },
}

const BENCHMARK_RUN_STATUS: Table = {
  running: { label: '评测中', tag: 'primary' },
  succeeded: { label: '已完成', tag: 'success' },
  failed: { label: '失败', tag: 'danger' },
}

const POLICY_VERSION_STATUS: Table = {
  pending: { label: '待审批', tag: 'warning' },
  approved: { label: '已生效', tag: 'success' },
  rejected: { label: '已驳回', tag: 'danger' },
}

/* ------------------------------------------------------------------ */
/* ErrorCode（backend/core/errors.py 全量）                             */
/* ------------------------------------------------------------------ */

const ERROR_CODE: Table = {
  OK: { label: '成功', tag: 'success' },
  INTERNAL_ERROR: { label: '服务内部错误', tag: 'danger' },
  VALIDATION_ERROR: { label: '参数校验失败', tag: 'warning' },
  NOT_FOUND: { label: '资源不存在', tag: 'info' },
  CONFLICT: { label: '状态冲突', tag: 'warning' },
  RATE_LIMITED: { label: '请求过于频繁', tag: 'warning' },
  IDEMPOTENCY_CONFLICT: { label: '幂等键冲突', tag: 'warning' },
  UNAUTHENTICATED: { label: '未认证', tag: 'danger' },
  TOKEN_EXPIRED: { label: '令牌过期', tag: 'danger' },
  BAD_CREDENTIALS: { label: '凭据错误', tag: 'danger' },
  ACCOUNT_LOCKED: { label: '账号已锁定', tag: 'danger' },
  FORBIDDEN: { label: '无权限', tag: 'danger' },
  TENANT_MISMATCH: { label: '跨租户访问被拒', tag: 'danger' },
  ROLE_REQUIRED: { label: '缺少职责槽', tag: 'danger' },
  DATASOURCE_UNREACHABLE: { label: '数据源不可达', tag: 'danger' },
  DATASOURCE_AUTH_FAILED: { label: '数据源认证失败', tag: 'warning' },
  DATASOURCE_TIMEOUT: { label: '数据源连接超时', tag: 'warning' },
  METADATA_PROFILE_EXPIRED: { label: '元数据已过期', tag: 'warning' },
  LLM_UNAVAILABLE: { label: '模型不可用', tag: 'warning' },
  PLAN_INVALID: { label: '方案结构非法', tag: 'danger' },
  HOCON_SYNTAX_ERROR: { label: 'HOCON 语法错误', tag: 'danger' },
  SCHEMA_MISMATCH: { label: 'Schema 不匹配', tag: 'danger' },
  GATE_BLOCKED: { label: '门禁阻断', tag: 'danger' },
  AUTOFIX_EXHAUSTED: { label: '自动修复已达上限', tag: 'warning' },
  VERSION_IMMUTABLE: { label: '不可变版本禁止修改', tag: 'danger' },
  SELF_APPROVAL_FORBIDDEN: { label: '不得自批', tag: 'danger' },
  DUTY_CONFLICT: { label: '职责互斥', tag: 'danger' },
  APPROVAL_INCOMPLETE: { label: '审批槽未集齐', tag: 'warning' },
  FINGERPRINT_MISMATCH: { label: '指纹已变化', tag: 'danger' },
  PREPARATION_EXPIRED: { label: '准备单已过期', tag: 'danger' },
  CAPABILITY_INVALID: { label: 'Capability 无效', tag: 'danger' },
  CAPABILITY_EXPIRED: { label: 'Capability 已过期', tag: 'danger' },
  CAPABILITY_REPLAYED: { label: 'Capability 重放', tag: 'danger' },
  UNAUTHORIZED_TOOL: { label: '未授权工具调用', tag: 'danger' },
  POLICY_BLOCKED: { label: 'P0/P1 策略阻断', tag: 'danger' },
  SUPERVISION_INTERRUPTED: { label: '监督硬中断', tag: 'danger' },
  QUALITY_REJECTED: { label: '质量未达标', tag: 'danger' },
  RUN_CONFLICT: { label: '同版本已有活跃运行', tag: 'warning' },
  SWAP_FAILED: { label: '原子 Swap 失败', tag: 'danger' },
  ROLLBACK_FAILED: { label: '回滚失败', tag: 'danger' },
}

/* ------------------------------------------------------------------ */
/* EvidenceEventType                                                   */
/* ------------------------------------------------------------------ */

/**
 * fe-07 §6 明确：`event_type` **原样展示**，不做中文翻译——
 * 审计口径必须与代码枚举逐字一致。因此这里 label 恒等于取值本身，
 * 只提供配色（用于事件类型徽章着色）。全量取自 backend/models/enums.py。
 */
const EVIDENCE_EVENT_TYPES: readonly string[] = [
  'auth.login_succeeded',
  'auth.login_failed',
  'datasource.created',
  'datasource.connectivity_tested',
  'metadata.probed',
  'secret.materialized',
  'pipeline.generated',
  'pipeline.gate_evaluated',
  'pipeline.version_frozen',
  'preparation.created',
  'policy.evaluated',
  'policy.blocked',
  'approval.granted',
  'approval.rejected',
  'approval.returned',
  'approval.self_denied',
  'capability.issued',
  'capability.rejected',
  'capability.consumed',
  'run.created',
  'run.started',
  'run.succeeded',
  'run.failed',
  'run.interrupted',
  'supervision.warning',
  'quality.report_generated',
  'swap.succeeded',
  'swap.failed',
  'rollback.started',
  'rollback.succeeded',
  'benchmark.run_completed',
  'ledger.checkpoint_signed',
]

/** 事件类型的配色分组：失败/阻断类红、安全类橙、成功类绿、其余信息色 */
const EVIDENCE_TAG_BY_PREFIX: ReadonlyArray<{ match: (t: string) => boolean; tag: TagType }> = [
  { match: (t) => /failed|blocked|interrupted|rejected|denied|replayed|invalid|expired/.test(t), tag: 'danger' },
  { match: (t) => /self_denied|capability|secret|supervision/.test(t), tag: 'warning' },
  { match: (t) => /succeeded|granted|frozen|created|signed|probed|evaluated/.test(t), tag: 'success' },
]

const EVIDENCE_EVENT_TYPE: Table = Object.fromEntries(
  EVIDENCE_EVENT_TYPES.map((t) => {
    const hit = EVIDENCE_TAG_BY_PREFIX.find((r) => r.match(t))
    return [t, { label: t, tag: hit === undefined ? 'info' : hit.tag }]
  }),
)

/* ------------------------------------------------------------------ */
/* 查表                                                                */
/* ------------------------------------------------------------------ */

const TABLES: Record<EnumKind, Table> = {
  Env: ENV,
  RoleName: ROLE_NAME,
  UserStatus: USER_STATUS,
  ProjectStatus: PROJECT_STATUS,
  MemberStatus: MEMBER_STATUS,
  SensitiveLevel: SENSITIVE_LEVEL,
  RiskLevel: RISK_LEVEL,
  ApprovalSlot: APPROVAL_SLOT,
  ApprovalDecisionType: APPROVAL_DECISION_TYPE,
  DataSourceType: DATA_SOURCE_TYPE,
  ProbeStatus: PROBE_STATUS,
  PipelineStatus: PIPELINE_STATUS,
  PreparationStatus: PREPARATION_STATUS,
  RunStatus: RUN_STATUS,
  ErrorCode: ERROR_CODE,
  ErrorCodeLabel: ERROR_CODE_LABEL,
  BenchmarkLayer: BENCHMARK_LAYER,
  BenchmarkRunStatus: BENCHMARK_RUN_STATUS,
  PolicyVersionStatus: POLICY_VERSION_STATUS,
  EvidenceEventType: EVIDENCE_EVENT_TYPE,
}

/** 未登记取值的兜底标签；unknown=true 时组件应加「后端可能已升级」提示 */
function fallback(value: string): EnumLabel {
  return { label: value, tag: 'info', unknown: true }
}

/**
 * 取枚举的中文标签与徽章配色。
 * @param kind 枚举种类
 * @param value 后端返回的原始取值（可能为空）
 */
export function enumLabel(kind: EnumKind, value: string | null | undefined): EnumLabel {
  if (value === null || value === undefined || value === '') {
    return { label: '—', tag: 'info', unknown: false }
  }
  const hit = TABLES[kind][value]
  if (hit === undefined) {
    if (import.meta.env.DEV) {
      // 未知取值不阻断渲染，但开发期要能发现，避免映射表长期漂移
      console.warn(`[enums] ${kind} 未登记取值：${value}`)
    }
    return fallback(value)
  }
  return { label: hit.label, tag: hit.tag, unknown: false }
}

/** 只要徽章配色时的便捷方法 */
export function enumTag(kind: EnumKind, value: string | null | undefined): TagType {
  return enumLabel(kind, value).tag
}

/** 只要中文标签时的便捷方法 */
export function enumText(kind: EnumKind, value: string | null | undefined): string {
  return enumLabel(kind, value).label
}

/** 已登记的全部角色；用于把后端下发的 `string[]` 收窄为 RoleName[] */
export const ROLE_NAMES: readonly RoleName[] = [
  'maker',
  'checker_data',
  'checker_security',
  'operator',
  'auditor',
]

/** 下拉选项结构；value 用 string 而非枚举，兼容「按后端取值直接回填」 */
export interface EnumOption {
  value: string
  label: string
  tag: TagType
}

/**
 * 枚举 → 下拉选项。
 * 筛选器一律由此生成，避免各页面手抄一遍取值列表而漏掉新状态。
 */
export function enumOptions(kind: EnumKind): EnumOption[] {
  return Object.entries(TABLES[kind]).map(([value, entry]) => ({
    value,
    label: entry.label,
    tag: entry.tag,
  }))
}

/**
 * 过滤出后端确实下发过的取值，并保留登记顺序。
 * 用于「按状态筛选」这类下拉：只列出当前项目真实存在的状态，
 * 而不是把 16 个 PipelineStatus 全列出来让用户白选。
 */
export function enumOptionsOf(kind: EnumKind, present: Iterable<string>): EnumOption[] {
  const set = new Set(present)
  return enumOptions(kind).filter((o) => set.has(o.value))
}

/** 角色集合 → 「A / B」形式；空集合显示「无职责槽」 */
export function rolesText(roles: readonly RoleName[] | null | undefined): string {
  if (roles === null || roles === undefined || roles.length === 0) {
    return '无职责槽'
  }
  return roles.map((r) => enumText('RoleName', r)).join(' / ')
}

/**
 * 判断当前角色集合是否命中 requiredRoles。
 * **仅用于菜单/路由的可见性过滤（体验优化）**；真正的鉴权在后端 role_assignment。
 * requiredRoles 为空数组 = 所有登录用户可见。
 */
export function hasAnyRole(
  roles: readonly string[],
  required: readonly string[] | undefined,
): boolean {
  if (required === undefined || required.length === 0) {
    return true
  }
  return required.some((r) => roles.includes(r))
}
