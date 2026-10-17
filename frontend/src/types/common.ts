/**
 * 跨域通用类型：响应信封、分页、以及 fe-01 §6.3 预登记的全局枚举。
 * 命名与后端 Pydantic DTO / models/enums.py 逐字对齐，不做字段名映射。
 */

/* ------------------------------------------------------------------ */
/* 统一响应信封（core/response.py）                                     */
/* ------------------------------------------------------------------ */

/** 成功信封：{ code: "OK", message: "success", data: T, request_id: "req_xxx" } */
export interface ApiEnvelope<T> {
  code: string
  message: string
  data: T
  request_id: string
}

/** 错误响应体（HTTP 非 2xx）：{ code, message, request_id, details } */
export interface ApiErrorResponse {
  code: string
  message: string
  request_id?: string
  details?: Record<string, unknown>
}

/** 分页元信息（core/response.py 的 PageMeta）；字段名用 page / page_size，不用 page_no */
export interface PageMeta {
  page: number
  page_size: number
  total: number
}

/** 分页信封：列表接口统一返回 { items, meta } */
export interface Page<T> {
  items: T[]
  meta: PageMeta
}

/** 分页查询公共参数 */
export interface PageQuery {
  page?: number
  page_size?: number
}

/* ------------------------------------------------------------------ */
/* 全局枚举（fe-01 §6.3 预登记）                                        */
/* ------------------------------------------------------------------ */

export type Env = 'dev' | 'staging' | 'prod'

export type RoleName = 'maker' | 'checker_data' | 'checker_security' | 'operator' | 'auditor'

export type UserStatus = 'active' | 'locked' | 'disabled'

export type ProjectStatus = 'active' | 'archived'

export type MemberStatus = 'active' | 'removed'

export type SensitiveLevel = 'public' | 'internal' | 'confidential' | 'restricted'

export type RiskLevel = 'P0' | 'P1' | 'P2' | 'P3'

/** 审批槽：只有数据槽与安全槽两个，四眼由此保证 */
export type ApprovalSlot = 'checker_data' | 'checker_security'

/**
 * 接口返回的枚举一律按 string 接收。
 * 后端新增取值时前端不能白屏，展示层经 utils/enums.ts 的 enumLabel 兜底为原始取值。
 */
export type RawEnum = string

/** enumLabel 的返回结构；unknown=true 表示映射表里没有该取值 */
export interface EnumLabel {
  label: string
  /** Element Plus tag type */
  tag: 'primary' | 'success' | 'warning' | 'danger' | 'info'
  unknown: boolean
}

/** enumLabel 支持的枚举种类 */
export type EnumKind =
  | 'Env'
  | 'RoleName'
  | 'UserStatus'
  | 'ProjectStatus'
  | 'MemberStatus'
  | 'SensitiveLevel'
  | 'RiskLevel'
  | 'ApprovalSlot'
  | 'ApprovalDecisionType'
  | 'DataSourceType'
  | 'ProbeStatus'
  | 'PipelineStatus'
  | 'PreparationStatus'
  | 'RunStatus'
  | 'ErrorCode'
  | 'ErrorCodeLabel'
  | 'BenchmarkLayer'
  | 'BenchmarkRunStatus'
  | 'PolicyVersionStatus'
  | 'EvidenceEventType'

/** 归属某项目的资源都带这两个字段 */
export interface ProjectScoped {
  project_id: number
}

/** 时间戳一律为 ISO8601 字符串（后端 UTC 下发，前端按本地时区展示） */
export type IsoDateTime = string

/* ------------------------------------------------------------------ */
/* 视图层                                                              */
/* ------------------------------------------------------------------ */

/** 左侧导航菜单项 */
export interface NavMenuItem {
  /** 路由 name；占位项用未注册的标识，配合 disabled */
  name: string
  title: string
  /** Element Plus 图标组件名；由 AppSidebar 白名单解析，不接受任意字符串 */
  icon: string
  /** 可见性过滤用的角色集合；空 = 所有登录用户可见（体验层，非安全边界） */
  requiredRoles: RoleName[]
  disabled?: boolean
}
