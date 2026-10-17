/**
 * dev mock 适配器：把 axios 实例的 adapter 换成一张内存路由表，
 * 让整站 UI 在后端 be-02 … be-21 陆续落地前也能完整跑通。
 *
 * 三条设计纪律：
 * 1. **契约对齐**：URL、HTTP 方法、query 参数名与 `src/api/*.ts` 逐字相同。
 *    任何不一致都会让 dev 环境出现「页面静默 404」，因此未匹配路由必须 console.warn 出声。
 * 2. **同形错误**：业务错误不是「返回一个假成功」，而是构造真正的 AxiosError 抛出，
 *    交给 client.ts 的响应拦截器按真实后端错误路径处理（toast、401 刷新、BizError）。
 * 3. **可写**：写接口真实改写 fixtures 数组，UI 后续 refetch 能看到变化；
 *    同时按 Idempotency-Key 去重，复刻后端的幂等语义。
 *
 * 生产环境永不安装（由 .env.production 的 VITE_USE_MOCK=false 与这里的 PROD 兜底共同保证）。
 */

import { AxiosError, AxiosHeaders } from 'axios'
import type { AxiosAdapter, AxiosResponse, InternalAxiosRequestConfig } from 'axios'
import http from '@/api/client'
import type {
  ApprovalDecisionRead,
  ApiErrorResponse,
  BudgetDefaults,
  CommitResult,
  ConnectivityResult,
  DataSource,
  DataSourceType,
  Diagnosis,
  ErrorRecordPage,
  EvidencePage,
  ExecutionMetricPoint,
  GateResult,
  HoconArtifact,
  LogPage,
  MetadataProfile,
  MonitorRange,
  Page,
  PipelineVersionDetail,
  PipelineVersionSummary,
  PreparationDetail,
  PreparationSummary,
  ProbeSampling,
  QualityReport,
  RunDetail,
  RunLogLine,
  RunSummary,
  SamplingScope,
  SchemaSnapshot,
  TokenPair,
  WorkflowSnapshot,
} from '@/types'
import * as fx from './fixtures'
import { hexDigest } from './fixtures'

/* ------------------------------------------------------------------ */
/* 内部错误与上下文                                                    */
/* ------------------------------------------------------------------ */

/** mock 层的业务错误：adapter 捕获后转成非 2xx 响应 + AxiosError */
class MockHttpError extends Error {
  readonly status: number
  readonly code: string
  readonly details: Record<string, unknown>

  constructor(status: number, code: string, message: string, details: Record<string, unknown> = {}) {
    super(message)
    this.name = 'MockHttpError'
    this.status = status
    this.code = code
    this.details = details
  }
}

type Query = Record<string, unknown>

interface HandlerContext {
  params: Record<string, string>
  query: Query
  body: unknown
  config: InternalAxiosRequestConfig
}

type RouteHandler = (ctx: HandlerContext) => unknown | Promise<unknown>

interface RouteDef {
  method: string
  pattern: string
  handler: RouteHandler
}

interface CompiledRoute extends RouteDef {
  regex: RegExp
  keys: string[]
}

/* ------------------------------------------------------------------ */
/* 通用工具                                                            */
/* ------------------------------------------------------------------ */

/** 深拷贝：handler 一律返还副本，避免视图直接改到 fixtures 本体 */
function clone<T>(value: T): T {
  return structuredClone(value)
}

function notFound(what: string): MockHttpError {
  return new MockHttpError(404, 'NOT_FOUND', `${what} 不存在`)
}

function validation(message: string, details: Record<string, unknown> = {}): MockHttpError {
  return new MockHttpError(422, 'VALIDATION_ERROR', message, details)
}

function conflict(message: string): MockHttpError {
  return new MockHttpError(409, 'CONFLICT', message)
}

function toRecord(value: unknown): Record<string, unknown> {
  if (value !== null && typeof value === 'object' && !Array.isArray(value)) {
    return value as Record<string, unknown>
  }
  return {}
}

function num(value: unknown, fallback: number): number {
  const parsed = typeof value === 'number' ? value : Number(value)
  return Number.isFinite(parsed) ? parsed : fallback
}

function intParam(params: Record<string, string>, key: string): number {
  return num(params[key], Number.NaN)
}

/** 逗号分隔的查询参数 → 数组；空值返回 undefined 表示「不过滤」 */
function csvList(value: unknown): string[] | undefined {
  if (typeof value !== 'string' || value.length === 0) {
    return undefined
  }
  return value.split(',').filter((s) => s.length > 0)
}

function paginate<T>(items: T[], query: Query): Page<T> {
  const page = Math.max(1, num(query.page, 1))
  const pageSize = Math.max(1, num(query.page_size, 20))
  const start = (page - 1) * pageSize
  return {
    items: items.slice(start, start + pageSize),
    meta: { page, page_size: pageSize, total: items.length },
  }
}

function matchText(source: string, keyword: unknown): boolean {
  return typeof keyword !== 'string' || keyword === '' || source.toLowerCase().includes(keyword.toLowerCase())
}

/* ------------------------------------------------------------------ */
/* 运行 / 准备单的列表裁剪                                             */
/* ------------------------------------------------------------------ */

function toRunSummary(run: RunDetail): RunSummary {
  return {
    id: run.id,
    project_id: run.project_id,
    pipeline_id: run.pipeline_id,
    pipeline_name: run.pipeline_name,
    pipeline_version_id: run.pipeline_version_id,
    version_no: run.version_no,
    artifact_sha256: run.artifact_sha256,
    status: run.status,
    formal_table: run.formal_table,
    triggered_by: run.triggered_by,
    triggered_by_name: run.triggered_by_name,
    started_at: run.started_at,
    finished_at: run.finished_at,
    duration_sec: run.duration_sec,
    read_rows: run.read_rows,
    written_rows: run.written_rows,
    written_bytes: run.written_bytes,
    reject_rows: run.reject_rows,
    reject_rate: run.reject_rate,
    can_pause: run.can_pause,
    can_resume: run.can_resume,
    can_cancel: run.can_cancel,
    can_rollback: run.can_rollback,
  }
}

function toPrepSummary(prep: PreparationDetail): PreparationSummary {
  return {
    id: prep.id,
    project_id: prep.project_id,
    pipeline_id: prep.pipeline_id,
    pipeline_name: prep.pipeline_name,
    pipeline_version_id: prep.pipeline_version_id,
    version_no: prep.version_no,
    artifact_sha256: prep.artifact_sha256,
    input_fingerprint: prep.input_fingerprint,
    risk_level: prep.risk_level,
    sensitive_level: prep.sensitive_level,
    environment: prep.environment,
    status: prep.status,
    required_slots: prep.required_slots,
    decided_slots: prep.decided_slots,
    created_by: prep.created_by,
    creator_name: prep.creator_name,
    frozen_at: prep.frozen_at,
    expires_at: prep.expires_at,
    my_slot: prep.my_slot,
    can_commit: prep.can_commit,
  }
}

/** 版本列表只给摘要：把 detail 的 manifest/plan/hocon 剔掉，避免列表接口下发整份制品 */
function toVersionSummary(version: PipelineVersionDetail): PipelineVersionSummary {
  return {
    id: version.id,
    version_no: version.version_no,
    sha256: version.sha256,
    status_note: version.status_note,
    frozen_at: version.frozen_at,
    frozen_by_display_name: version.frozen_by_display_name,
    source_metadata_profile_version: version.source_metadata_profile_version,
    generation_context: version.generation_context,
  }
}

/* ------------------------------------------------------------------ */
/* 令牌与就绪状态                                                      */
/* ------------------------------------------------------------------ */

/** 演示用假令牌：刻意做成一眼可辨的假值，不含任何真实凭据形态 */
function tokenPairFor(userId: number): TokenPair {
  const user = fx.users.find((u) => u.id === userId) ?? fx.users[0]
  if (user === undefined) {
    throw notFound('用户')
  }
  return {
    access_token: `mock.access.${user.username}`,
    refresh_token: `mock.refresh.${user.username}`,
    token_type: 'Bearer',
    expires_in: 3600,
    user: {
      id: user.id,
      username: user.username,
      email: user.email,
      tenant_id: fx.tenant.id,
      display_name: user.display_name,
    },
  }
}

/* ------------------------------------------------------------------ */
/* 自增 id 与幂等缓存                                                  */
/* ------------------------------------------------------------------ */

let dataSourceSeq = 100
let memberSeq = 100
let assignmentSeq = 100
let preparationSeq = 100
let profileSeq = 100
let policyRuleSeq = 100
let benchmarkRunSeq = 100
let connectivitySeq = 0
let requestSeq = 0
let threadSeq = 0

/** Idempotency-Key 已经处理过的写请求：命中即回放第一次的结果，不再二次变更 */
const idempotencyCache = new Map<string, unknown>()

/* ------------------------------------------------------------------ */
/* 路由表                                                              */
/* ------------------------------------------------------------------ */

const ROUTES: RouteDef[] = [
  /* ------------------------------ 认证 ------------------------------ */
  {
    method: 'post',
    pattern: '/auth/login',
    handler: ({ body }) => {
      const payload = toRecord(body)
      const user = fx.users.find((u) => u.username === payload.username) ?? fx.users[0]
      if (user === undefined) {
        throw notFound('用户')
      }
      // 密码规则刻意留一个可复现的失败分支，方便验证登录表单的行内错误
      if (typeof payload.password !== 'string' || payload.password.length === 0 || payload.password === 'wrong') {
        throw new MockHttpError(401, 'BAD_CREDENTIALS', '用户名或密码错误')
      }
      return tokenPairFor(user.id)
    },
  },
  {
    method: 'post',
    pattern: '/auth/refresh',
    handler: () => tokenPairFor(1),
  },
  { method: 'post', pattern: '/auth/logout', handler: () => null },
  { method: 'post', pattern: '/auth/logout-all', handler: () => null },
  { method: 'get', pattern: '/auth/me', handler: () => clone(fx.currentUser) },
  { method: 'get', pattern: '/auth/tenant', handler: () => clone(fx.tenant) },
  {
    method: 'post',
    pattern: '/auth/password',
    handler: ({ body }) => {
      const payload = toRecord(body)
      if (payload.old_password === 'wrong') {
        throw new MockHttpError(401, 'BAD_CREDENTIALS', '旧密码不正确')
      }
      if (typeof payload.new_password !== 'string' || payload.new_password.length < 8) {
        throw validation('新密码长度不足', { fields: [{ loc: 'new_password', reason: '至少 8 位' }] })
      }
      return null
    },
  },

  /* ------------------------------ 项目 ------------------------------ */
  { method: 'get', pattern: '/projects', handler: () => clone(fx.projects) },
  {
    method: 'get',
    pattern: '/projects/:id',
    handler: ({ params }) => {
      const project = fx.projects.find((p) => p.id === intParam(params, 'id'))
      if (project === undefined) {
        throw notFound('项目')
      }
      return clone(project)
    },
  },
  {
    method: 'put',
    pattern: '/projects/:id',
    handler: ({ params, body }) => {
      const id = intParam(params, 'id')
      const project = fx.projects.find((p) => p.id === id)
      if (project === undefined) {
        throw notFound('项目')
      }
      const patch = toRecord(body)
      if (typeof patch.name === 'string') {
        project.name = patch.name
      }
      if (typeof patch.description === 'string') {
        project.description = patch.description
      }
      return clone(project)
    },
  },
  {
    method: 'get',
    pattern: '/projects/:id/members',
    handler: ({ params }) =>
      clone(fx.members.filter((m) => m.project_id === intParam(params, 'id'))),
  },
  {
    method: 'post',
    pattern: '/projects/:id/members',
    handler: ({ params, body }) => {
      const projectId = intParam(params, 'id')
      const payload = toRecord(body)
      const userId = num(payload.user_id, Number.NaN)
      const user = fx.users.find((u) => u.id === userId)
      if (user === undefined) {
        throw validation('候选用户不存在', { fields: [{ loc: 'user_id', reason: '未知用户' }] })
      }
      if (typeof payload.reason !== 'string' || payload.reason.length === 0) {
        throw validation('操作理由必填', { fields: [{ loc: 'reason', reason: '必填' }] })
      }
      memberSeq += 1
      const member = {
        id: memberSeq,
        project_id: projectId,
        user_id: user.id,
        username: user.username,
        display_name: user.display_name,
        email: user.email,
        status: 'active' as const,
        roles: [],
        joined_at: new Date().toISOString(),
      }
      fx.members.push(member)
      const project = fx.projects.find((p) => p.id === projectId)
      if (project !== undefined) {
        project.member_count = fx.members.filter((m) => m.project_id === projectId).length
      }
      return clone(member)
    },
  },
  {
    method: 'delete',
    pattern: '/projects/:id/members/:memberId',
    handler: ({ params }) => {
      const memberId = intParam(params, 'memberId')
      const index = fx.members.findIndex((m) => m.id === memberId)
      if (index < 0) {
        throw notFound('成员')
      }
      fx.members.splice(index, 1)
      return null
    },
  },
  {
    method: 'get',
    pattern: '/projects/:id/member-candidates',
    handler: ({ params }) => clone(fx.memberCandidates(intParam(params, 'id'))),
  },
  {
    method: 'get',
    pattern: '/projects/:id/role-assignments',
    handler: ({ params }) =>
      clone(fx.roleAssignments.filter((a) => a.project_id === intParam(params, 'id'))),
  },
  {
    method: 'post',
    pattern: '/projects/:id/role-assignments',
    handler: ({ params, body }) => {
      const projectId = intParam(params, 'id')
      const payload = toRecord(body)
      const user = fx.users.find((u) => u.id === num(payload.user_id, Number.NaN))
      if (user === undefined) {
        throw validation('用户不存在', { fields: [{ loc: 'user_id', reason: '未知用户' }] })
      }
      if (typeof payload.role !== 'string') {
        throw validation('职责槽必填', { fields: [{ loc: 'role', reason: '必填' }] })
      }
      assignmentSeq += 1
      const assignment = {
        id: assignmentSeq,
        project_id: projectId,
        member_id: fx.members.find((m) => m.project_id === projectId && m.user_id === user.id)?.id ?? 0,
        user_id: user.id,
        user_name: user.display_name,
        role: payload.role as (typeof fx.roleAssignments)[number]['role'],
        granted_by: 1,
        granted_by_name: '林思远',
        granted_at: new Date().toISOString(),
        valid_from: typeof payload.valid_from === 'string' ? payload.valid_from : null,
        valid_to: typeof payload.valid_to === 'string' ? payload.valid_to : null,
        reason: typeof payload.reason === 'string' ? payload.reason : '',
      }
      fx.roleAssignments.push(assignment)
      return clone(assignment)
    },
  },
  {
    method: 'delete',
    pattern: '/projects/:id/role-assignments/:assignmentId',
    handler: ({ params }) => {
      const index = fx.roleAssignments.findIndex((a) => a.id === intParam(params, 'assignmentId'))
      if (index < 0) {
        throw notFound('职责授予')
      }
      fx.roleAssignments.splice(index, 1)
      return null
    },
  },

  /* ----------------------------- 数据源 ----------------------------- */
  {
    method: 'get',
    pattern: '/datasources',
    handler: ({ query }) => {
      let list = [...fx.dataSources]
      const projectId = num(query.project_id, Number.NaN)
      if (Number.isFinite(projectId)) {
        list = list.filter((d) => d.project_id === projectId)
      }
      const types = csvList(query.type)
      if (types !== undefined) {
        list = list.filter((d) => types.includes(d.type))
      }
      const envs = csvList(query.env)
      if (envs !== undefined) {
        list = list.filter((d) => envs.includes(d.env))
      }
      const levels = csvList(query.sensitive_level)
      if (levels !== undefined) {
        list = list.filter((d) => levels.includes(d.sensitive_level))
      }
      if (typeof query.status === 'string' && query.status.length > 0) {
        list = list.filter((d) => d.status === query.status)
      }
      list = list.filter((d) => matchText(d.name, query.q))
      list.sort((a, b) => a.id - b.id)
      return paginate(list.map(clone), query)
    },
  },
  {
    method: 'get',
    pattern: '/datasources/:id',
    handler: ({ params }) => {
      const source = fx.dataSources.find((d) => d.id === intParam(params, 'id'))
      if (source === undefined) {
        throw notFound('数据源')
      }
      return clone(source)
    },
  },
  {
    method: 'post',
    pattern: '/datasources',
    handler: ({ body }) => {
      const payload = toRecord(body)
      dataSourceSeq += 1
      const id = dataSourceSeq
      const name = typeof payload.name === 'string' ? payload.name : `datasource-${id}`
      // 凭据只落键名：mock 不持有任何值，也不回显值（CLAUDE.md §5.2）
      const keyNames = Object.keys(toRecord(payload.credentials))
      const created: DataSource = {
        id,
        project_id: num(payload.project_id, 1),
        name,
        type: (typeof payload.type === 'string' ? payload.type : 'mysql') as DataSourceType,
        env: (typeof payload.env === 'string' ? payload.env : 'dev') as DataSource['env'],
        sensitive_level: (typeof payload.sensitive_level === 'string' ? payload.sensitive_level : 'internal') as DataSource['sensitive_level'],
        endpoint: toRecord(payload.endpoint) as DataSource['endpoint'],
        secret_ref:
          keyNames.length === 0
            ? null
            : {
                provider: 'vault',
                path: `secret/data/dw/mock/${name}`,
                version: 1,
                key_names: keyNames,
                last_rotated_at: null,
              },
        tags: Array.isArray(payload.tags) ? (payload.tags as string[]) : [],
        owner_id: typeof payload.owner_id === 'number' ? payload.owner_id : 1,
        owner_display_name: '林思远',
        status: 'active',
        last_tested_at: null,
        last_test_ok: null,
        latest_profile: null,
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
      }
      fx.dataSources.push(created)
      return clone(created)
    },
  },
  {
    method: 'put',
    pattern: '/datasources/:id',
    handler: ({ params, body }) => {
      const source = fx.dataSources.find((d) => d.id === intParam(params, 'id'))
      if (source === undefined) {
        throw notFound('数据源')
      }
      const patch = toRecord(body)
      if (typeof patch.name === 'string') {
        source.name = patch.name
      }
      if (typeof patch.env === 'string') {
        source.env = patch.env as DataSource['env']
      }
      if (typeof patch.sensitive_level === 'string') {
        source.sensitive_level = patch.sensitive_level as DataSource['sensitive_level']
      }
      if (patch.endpoint !== undefined) {
        source.endpoint = toRecord(patch.endpoint) as DataSource['endpoint']
      }
      if (Array.isArray(patch.tags)) {
        source.tags = patch.tags as string[]
      }
      if (typeof patch.owner_id === 'number' || patch.owner_id === null) {
        source.owner_id = patch.owner_id as number | null
      }
      const keyNames = Object.keys(toRecord(patch.credentials))
      if (keyNames.length > 0 && source.secret_ref !== null) {
        // 重新设置凭据 = 新版本；依旧只记键名
        source.secret_ref = { ...source.secret_ref, version: source.secret_ref.version + 1, key_names: keyNames }
      }
      source.updated_at = new Date().toISOString()
      return clone(source)
    },
  },
  {
    method: 'post',
    pattern: '/datasources/:id/disable',
    handler: ({ params }) => {
      const source = fx.dataSources.find((d) => d.id === intParam(params, 'id'))
      if (source === undefined) {
        throw notFound('数据源')
      }
      source.status = 'disabled'
      return clone(source)
    },
  },
  {
    method: 'post',
    pattern: '/datasources/:id/enable',
    handler: ({ params }) => {
      const source = fx.dataSources.find((d) => d.id === intParam(params, 'id'))
      if (source === undefined) {
        throw notFound('数据源')
      }
      source.status = 'active'
      return clone(source)
    },
  },
  {
    method: 'delete',
    pattern: '/datasources/:id',
    handler: ({ params }) => {
      const id = intParam(params, 'id')
      const index = fx.dataSources.findIndex((d) => d.id === id)
      if (index < 0) {
        throw notFound('数据源')
      }
      // 有 Pipeline 引用时按真实后端语义拒绝删除（CONFLICT）
      const referenced = fx.pipelines.some(
        (p) => p.source_data_source_id === id || p.target_data_source_id === id,
      )
      if (referenced) {
        throw new MockHttpError(409, 'CONFLICT', '该数据源仍被 Pipeline 引用，无法删除', {
          pipeline_refs: fx.pipelines.filter((p) => p.source_data_source_id === id || p.target_data_source_id === id).map((p) => p.id),
        })
      }
      fx.dataSources.splice(index, 1)
      return null
    },
  },
  {
    method: 'post',
    pattern: '/datasources/:id/credentials',
    handler: ({ params, body }) => {
      const source = fx.dataSources.find((d) => d.id === intParam(params, 'id'))
      if (source === undefined) {
        throw notFound('数据源')
      }
      const credentials = toRecord(toRecord(body).credentials)
      const keyNames = Object.keys(credentials)
      if (keyNames.length === 0) {
        throw validation('凭据键不能为空', { fields: [{ loc: 'credentials', reason: '至少一个键' }] })
      }
      // 只保留键名与版本号，值在此处被丢弃
      source.secret_ref = {
        provider: 'vault',
        path: `secret/data/dw/mock/${source.name}`,
        version: (source.secret_ref?.version ?? 0) + 1,
        key_names: keyNames,
        last_rotated_at: new Date().toISOString(),
      }
      return clone(source)
    },
  },
  {
    method: 'post',
    pattern: '/datasources/:id/connectivity-tests',
    handler: ({ params }) => {
      const source = fx.dataSources.find((d) => d.id === intParam(params, 'id'))
      if (source === undefined) {
        throw notFound('数据源')
      }
      connectivitySeq += 1
      return {
        test_id: `ct-${connectivitySeq}`,
        state: 'pending',
        latency_ms: null,
        target: null,
        server_version: null,
        error_code: null,
        message: null,
        tested_at: null,
      } satisfies ConnectivityResult
    },
  },
  {
    method: 'get',
    pattern: '/datasources/:id/connectivity-tests/:testId',
    handler: ({ params }) => {
      const source = fx.dataSources.find((d) => d.id === intParam(params, 'id'))
      if (source === undefined) {
        throw notFound('数据源')
      }
      const ok = source.last_test_ok !== false
      source.last_tested_at = new Date().toISOString()
      source.last_test_ok = ok
      return clone(fx.connectivityResult(params.testId ?? 'ct', ok))
    },
  },
  {
    method: 'post',
    pattern: '/datasources/:id/probes',
    handler: ({ params, body }) => {
      const source = fx.dataSources.find((d) => d.id === intParam(params, 'id'))
      if (source === undefined) {
        throw notFound('数据源')
      }
      profileSeq += 1
      const version = fx.metadataProfiles.filter((p) => p.data_source_id === source.id).length + 1
      const payload = toRecord(body)
      const rawSampling = toRecord(payload.sampling)
      const sampling: ProbeSampling = {
        rows: num(rawSampling.rows, 200),
        scope: (typeof rawSampling.scope === 'string' ? rawSampling.scope : 'schema_only') as SamplingScope,
        tables: Array.isArray(rawSampling.tables) ? (rawSampling.tables as string[]) : null,
      }
      const profile: MetadataProfile = {
        id: profileSeq,
        data_source_id: source.id,
        version,
        status: 'probing',
        sampling,
        masking_policy: toRecord(payload.masking_policy),
        masked_samples: {},
        tables_count: 0,
        sha256: hexDigest(`profile:${source.id}:${version}:new`),
        expires_at: null,
        error_message: null,
        created_at: new Date().toISOString(),
        created_by_display_name: '林思远',
      }
      fx.metadataProfiles.push(profile)
      // 不在这里写 source.latest_profile：它由 fx.settleProbeState() 从 metadataProfiles 统一派生。
      // 两个写入方必然会在某次改动后分叉。
      return { profile_id: profile.id, version }
    },
  },
  {
    method: 'get',
    pattern: '/datasources/:id/profiles',
    handler: ({ params }) => clone(fx.profileBriefs(intParam(params, 'id'))),
  },
  {
    method: 'get',
    pattern: '/datasources/:id/profiles/:version',
    handler: ({ params }) => {
      const profile = fx.metadataProfiles.find(
        (p) => p.data_source_id === intParam(params, 'id') && p.version === intParam(params, 'version'),
      )
      if (profile === undefined) {
        throw notFound('元数据探查结果')
      }
      return clone(profile)
    },
  },
  {
    method: 'get',
    pattern: '/datasources/:id/profiles/:version/schema',
    handler: ({ params, query }): SchemaSnapshot => {
      const table = typeof query.table === 'string' && query.table.length > 0 ? query.table : undefined
      return clone(fx.schemaFor(intParam(params, 'id'), intParam(params, 'version'), table))
    },
  },
  {
    method: 'get',
    pattern: '/datasources/:id/profiles/:version/samples',
    handler: ({ params, query }) => {
      const table = typeof query.table === 'string' ? query.table : ''
      return clone(fx.samplesFor(intParam(params, 'id'), intParam(params, 'version'), table, num(query.limit, 50)))
    },
  },

  /* ---------------------------- Pipeline ---------------------------- */
  {
    method: 'get',
    pattern: '/pipelines',
    handler: ({ query }) => {
      let list = [...fx.pipelines]
      const projectId = num(query.project_id, Number.NaN)
      if (Number.isFinite(projectId)) {
        list = list.filter((p) => p.project_id === projectId)
      }
      const statuses = csvList(query.status)
      if (statuses !== undefined) {
        list = list.filter((p) => statuses.includes(p.status))
      }
      list = list.filter((p) => matchText(p.name, query.q))
      list.sort((a, b) => a.id - b.id)
      return paginate(list.map(clone), query)
    },
  },
  {
    method: 'post',
    pattern: '/pipelines/generations',
    handler: ({ body }) => {
      const payload = toRecord(body)
      if (typeof payload.requirement !== 'string' || payload.requirement.trim().length === 0) {
        throw validation('需求描述必填', { fields: [{ loc: 'requirement', reason: '必填' }] })
      }
      threadSeq += 1
      const threadId = `thread-${threadSeq}`
      const snapshot: WorkflowSnapshot = {
        thread_id: threadId,
        phase: 'awaiting_clarification',
        phase_message: '需要补充口径后才能生成方案',
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
            hint: null,
          },
        ],
        autofix_round: 0,
        last_error: null,
      }
      fx.workflowSnapshots[threadId] = snapshot
      return clone(snapshot)
    },
  },
  {
    method: 'get',
    pattern: '/pipelines/generations/:threadId',
    handler: ({ params }) => {
      const snapshot = fx.workflowSnapshots[params.threadId ?? '']
      if (snapshot === undefined) {
        throw notFound('生成会话')
      }
      return clone(snapshot)
    },
  },
  {
    method: 'post',
    pattern: '/pipelines/generations/:threadId/answers',
    handler: ({ params }) => {
      const threadId = params.threadId ?? ''
      const snapshot: WorkflowSnapshot = {
        thread_id: threadId,
        phase: 'ready_to_freeze',
        phase_message: '方案已生成，门禁通过，可冻结版本',
        pending_questions: [],
        autofix_round: 0,
        last_error: null,
      }
      fx.workflowSnapshots[threadId] = snapshot
      return clone(snapshot)
    },
  },
  {
    method: 'post',
    pattern: '/pipelines/generations/:threadId/abort',
    handler: ({ params }) => {
      const threadId = params.threadId ?? ''
      const snapshot: WorkflowSnapshot = {
        thread_id: threadId,
        phase: 'idle',
        phase_message: '已取消生成',
        pending_questions: [],
        autofix_round: 0,
        last_error: null,
      }
      fx.workflowSnapshots[threadId] = snapshot
      return clone(snapshot)
    },
  },
  {
    method: 'get',
    pattern: '/pipelines/:id/versions',
    handler: ({ params, query }) => {
      const list = fx.versionsOfPipeline(intParam(params, 'id')).map(toVersionSummary)
      return paginate(list, query)
    },
  },
  {
    method: 'get',
    pattern: '/pipelines/:id',
    handler: ({ params }) => {
      const pipeline = fx.pipelines.find((p) => p.id === intParam(params, 'id'))
      if (pipeline === undefined) {
        throw notFound('Pipeline')
      }
      return clone(pipeline)
    },
  },
  // 注意：/pipeline-versions/diff 必须排在 /:versionId 之前，否则会被参数路由吞掉
  {
    method: 'get',
    pattern: '/pipeline-versions/diff',
    handler: ({ query }) =>
      clone(
        fx.versionDiff([
          num(query.base_version_id, 101),
          num(query.target_version_id, 103),
        ]),
      ),
  },
  {
    method: 'get',
    pattern: '/pipeline-versions/:versionId',
    handler: ({ params }) => {
      const version = fx.pipelineVersions.find((v) => v.id === intParam(params, 'versionId'))
      if (version === undefined) {
        throw notFound('Pipeline 版本')
      }
      return clone(version)
    },
  },
  {
    method: 'get',
    pattern: '/pipeline-versions/:versionId/plan',
    handler: ({ params }) => {
      const version = fx.pipelineVersions.find((v) => v.id === intParam(params, 'versionId'))
      if (version === undefined) {
        throw notFound('Pipeline 版本')
      }
      return clone(version.plan.plan.dag)
    },
  },
  {
    method: 'get',
    pattern: '/pipeline-versions/:versionId/hocon',
    handler: ({ params }): HoconArtifact => {
      const version = fx.pipelineVersions.find((v) => v.id === intParam(params, 'versionId'))
      if (version === undefined) {
        throw notFound('Pipeline 版本')
      }
      return clone(version.hocon)
    },
  },
  {
    method: 'get',
    pattern: '/pipeline-versions/:versionId/gate',
    handler: ({ params }): GateResult => {
      const versionId = intParam(params, 'versionId')
      const gate = fx.gateResults[versionId]
      if (gate === undefined) {
        throw notFound('门禁结果')
      }
      return clone(gate)
    },
  },
  {
    method: 'post',
    pattern: '/pipeline-versions/:versionId/autofix',
    handler: ({ params }) => {
      const versionId = intParam(params, 'versionId')
      const gate = fx.gateResults[versionId]
      if (gate === undefined) {
        throw notFound('门禁结果')
      }
      const rounds = gate.autofix_rounds.length
      if (rounds >= 3) {
        throw new MockHttpError(409, 'AUTOFIX_EXHAUSTED', '自动修复已达上限（3 轮）')
      }
      const round = fx.autofixRound(versionId, rounds + 1)
      gate.autofix_rounds.push(round)
      return clone(round)
    },
  },
  {
    method: 'post',
    pattern: '/pipeline-versions/:versionId/freeze',
    handler: ({ params }) => {
      const version = fx.pipelineVersions.find((v) => v.id === intParam(params, 'versionId'))
      if (version === undefined) {
        throw notFound('Pipeline 版本')
      }
      if (!version.plan.valid) {
        throw new MockHttpError(409, 'GATE_BLOCKED', '方案结构非法或门禁未通过，禁止冻结')
      }
      const pipeline = fx.pipelines.find((p) => fx.versionToPipeline[version.id] === p.id)
      if (pipeline !== undefined) {
        pipeline.status = 'version_frozen'
        pipeline.current_version_id = version.id
        pipeline.updated_at = new Date().toISOString()
      }
      return clone(version.manifest)
    },
  },

  /* ----------------------------- 准备单 ----------------------------- */
  {
    method: 'get',
    pattern: '/preparations',
    handler: ({ query }) => {
      let list = [...fx.preparations]
      const projectId = num(query.project_id, Number.NaN)
      if (Number.isFinite(projectId)) {
        list = list.filter((p) => p.project_id === projectId)
      }
      // scope 由服务端过滤，避免前端二次裁剪导致 total 对不上
      const scope = typeof query.scope === 'string' ? query.scope : 'all'
      if (scope === 'todo') {
        list = list.filter((p) => p.my_slot !== null)
      } else if (scope === 'mine') {
        list = list.filter((p) => p.created_by === 1)
      } else if (scope === 'done') {
        list = list.filter((p) => p.status !== 'pending' && p.status !== 'partially_approved')
      }
      if (typeof query.status === 'string' && query.status.length > 0) {
        list = list.filter((p) => p.status === query.status)
      }
      if (typeof query.risk_level === 'string' && query.risk_level.length > 0) {
        list = list.filter((p) => p.risk_level === query.risk_level)
      }
      if (typeof query.environment === 'string' && query.environment.length > 0) {
        list = list.filter((p) => p.environment === query.environment)
      }
      if (typeof query.slot === 'string' && query.slot.length > 0) {
        list = list.filter((p) => p.required_slots.includes(query.slot as PreparationDetail['required_slots'][number]))
      }
      if (Number.isFinite(num(query.pipeline_id, Number.NaN))) {
        list = list.filter((p) => p.pipeline_id === num(query.pipeline_id, 0))
      }
      list = list.filter((p) => matchText(p.pipeline_name, query.keyword))
      list.sort((a, b) => b.id - a.id)
      return paginate(list.map(toPrepSummary), query)
    },
  },
  {
    method: 'post',
    pattern: '/preparations',
    handler: ({ body }) => {
      const payload = toRecord(body)
      const versionId = num(payload.pipeline_version_id, Number.NaN)
      const version = fx.pipelineVersions.find((v) => v.id === versionId)
      if (version === undefined) {
        throw validation('版本不存在', { fields: [{ loc: 'pipeline_version_id', reason: '未知版本' }] })
      }
      if (!version.plan.valid) {
        throw new MockHttpError(409, 'GATE_BLOCKED', '门禁未通过的版本不可创建准备单')
      }
      const pipelineId = fx.versionToPipeline[version.id] ?? 0
      const pipeline = fx.pipelines.find((p) => p.id === pipelineId)
      preparationSeq += 1
      const now = new Date().toISOString()
      const created: PreparationDetail = {
        id: preparationSeq,
        project_id: num(payload.project_id, pipeline?.project_id ?? 1),
        pipeline_id: pipelineId,
        pipeline_name: pipeline?.name ?? '未知 Pipeline',
        pipeline_version_id: version.id,
        version_no: version.version_no,
        artifact_sha256: version.sha256,
        input_fingerprint: hexDigest(`fp:${version.id}:${preparationSeq}`),
        risk_level: 'P2',
        sensitive_level: 'internal',
        environment: (pipeline?.project_id === 2 ? 'prod' : 'dev') as PreparationDetail['environment'],
        status: 'pending',
        required_slots: ['checker_data'],
        decided_slots: [],
        created_by: 1,
        creator_name: '林思远',
        frozen_at: version.frozen_at,
        expires_at: null,
        my_slot: null,
        can_commit: false,
        resource_scope: {
          reads: version.plan.plan.inputs.map((i) => ({ data_source_id: i.data_source_id, database: i.schema, table: i.table })),
          writes: version.plan.plan.outputs.map((o) => ({ data_source_id: o.data_source_id, database: o.database, table: o.table, mode: 'shadow_then_swap' as const })),
        },
        budget: fx.defaultBudget(),
        rollback_plan: { steps: [], shadow_cleanup: true, restore_scope: '仅影子表' },
        risk_reasons: [{ code: 'R_P2_WRITE_FORMAL', level: 'P2', title: '写正式目标表', detail: '需数据审批槽确认口径。' }],
        decisions: [],
        timeline: [{ at: now, event: 'created', actor_name: '林思远', detail: `由版本 v${version.version_no} 创建` }],
      }
      fx.preparations.push(created)
      return clone(created)
    },
  },
  {
    method: 'get',
    pattern: '/preparations/:id',
    handler: ({ params }) => {
      const prep = fx.preparations.find((p) => p.id === intParam(params, 'id'))
      if (prep === undefined) {
        throw notFound('准备单')
      }
      return clone(prep)
    },
  },
  {
    method: 'get',
    pattern: '/preparations/:id/timeline',
    handler: ({ params }) => {
      const prep = fx.preparations.find((p) => p.id === intParam(params, 'id'))
      if (prep === undefined) {
        throw notFound('准备单')
      }
      return clone(prep.timeline)
    },
  },
  {
    method: 'get',
    pattern: '/preparations/:id/risk-reasons',
    handler: ({ params }) => {
      const prep = fx.preparations.find((p) => p.id === intParam(params, 'id'))
      if (prep === undefined) {
        throw notFound('准备单')
      }
      const weightOf: Record<string, number> = { P0: 100, P1: 60, P2: 30, P3: 5 }
      return clone(
        prep.risk_reasons.map((r) => ({
          rule_code: r.code,
          rule_name: r.title,
          weight: weightOf[r.level] ?? 0,
          detail: r.detail,
        })),
      )
    },
  },
  {
    method: 'post',
    pattern: '/preparations/:id/decisions',
    handler: ({ params, body }): ApprovalDecisionRead => {
      const prep = fx.preparations.find((p) => p.id === intParam(params, 'id'))
      if (prep === undefined) {
        throw notFound('准备单')
      }
      const payload = toRecord(body)
      // unknown 无法被负向收窄，这里显式收敛到联合类型
      const slot = payload.slot === 'checker_data' || payload.slot === 'checker_security' ? payload.slot : null
      const decision =
        payload.decision === 'approve' || payload.decision === 'reject' || payload.decision === 'return'
          ? payload.decision
          : null
      if (slot === null) {
        throw validation('审批槽非法', { fields: [{ loc: 'slot', reason: '未知审批槽' }] })
      }
      if (decision === null) {
        throw validation('审批动作非法', { fields: [{ loc: 'decision', reason: '未知动作' }] })
      }
      // P0 永远不可批准：这是策略阻断态，不是「还没轮到」
      if (prep.risk_level === 'P0') {
        throw new MockHttpError(403, 'POLICY_BLOCKED', 'P0 阻断项禁止提交审批决定')
      }
      if (prep.status !== 'pending' && prep.status !== 'partially_approved') {
        throw conflict(`准备单当前状态（${prep.status}）不接受新的审批决定`)
      }
      if (!prep.required_slots.includes(slot)) {
        throw validation('该准备单不需要此审批槽', { fields: [{ loc: 'slot', reason: '不在 required_slots 中' }] })
      }
      // 四眼：审批人不得等于发起人（服务端拦截，前端置灰只是体验）
      if (decision === 'approve' && prep.created_by === 1) {
        throw new MockHttpError(403, 'SELF_APPROVAL_FORBIDDEN', '审批人不能是准备单发起人')
      }
      const now = new Date().toISOString()
      const record: ApprovalDecisionRead = {
        slot,
        approver_id: 1,
        approver_name: '林思远',
        approver_role: slot,
        decision,
        comment: typeof payload.comment === 'string' ? payload.comment : '',
        artifact_sha256: prep.artifact_sha256,
        fingerprint_at_decision: prep.input_fingerprint,
        decided_at: now,
        stale: false,
      }
      prep.decisions.push(record)
      if (decision === 'approve') {
        if (!prep.decided_slots.includes(slot)) {
          prep.decided_slots = [...prep.decided_slots, slot]
        }
        const allDecided = prep.required_slots.every((s) => prep.decided_slots.includes(s))
        prep.status = allDecided ? 'approved' : 'partially_approved'
        prep.can_commit = allDecided
        prep.my_slot = null
        prep.timeline.push({ at: now, event: 'approved_slot', actor_name: '林思远', slot })
      } else if (decision === 'reject') {
        prep.status = 'rejected'
        prep.can_commit = false
        prep.my_slot = null
        prep.timeline.push({ at: now, event: 'rejected_slot', actor_name: '林思远', slot })
      } else {
        prep.status = 'pending'
        prep.decided_slots = prep.decided_slots.filter((s) => s !== slot)
        prep.my_slot = null
        prep.timeline.push({ at: now, event: 'returned_slot', actor_name: '林思远', slot })
      }
      return clone(record)
    },
  },
  {
    method: 'post',
    pattern: '/preparations/:id/commit',
    handler: ({ params }): CommitResult => {
      const prep = fx.preparations.find((p) => p.id === intParam(params, 'id'))
      if (prep === undefined) {
        throw notFound('准备单')
      }
      if (!prep.can_commit) {
        throw new MockHttpError(409, 'APPROVAL_INCOMPLETE', '审批槽未集齐，不可 Commit')
      }
      prep.can_commit = false
      const now = new Date().toISOString()
      prep.timeline.push({ at: now, event: 'committed', actor_name: '林思远' })
      // Capability 在服务端换取，mock 也不返回任何令牌（前端无从得知）
      return { run_id: 9000 + prep.id, status: 'queued', queued_at: now }
    },
  },

  /* ------------------------------ 运行 ------------------------------ */
  {
    method: 'get',
    pattern: '/runs',
    handler: ({ query }) => {
      let list = [...fx.runs]
      const projectId = num(query.project_id, Number.NaN)
      if (Number.isFinite(projectId)) {
        list = list.filter((r) => r.project_id === projectId)
      }
      const statuses = csvList(query.status)
      if (statuses !== undefined) {
        list = list.filter((r) => statuses.includes(r.status))
      }
      if (Number.isFinite(num(query.pipeline_id, Number.NaN))) {
        list = list.filter((r) => r.pipeline_id === num(query.pipeline_id, 0))
      }
      // 先收敛到局部常量：query 是可变索引对象，收窄在回调里会失效
      const from = typeof query.from === 'string' && query.from.length > 0 ? query.from : null
      if (from !== null) {
        list = list.filter((r) => (r.started_at ?? '') >= from)
      }
      const to = typeof query.to === 'string' && query.to.length > 0 ? query.to : null
      if (to !== null) {
        list = list.filter((r) => (r.started_at ?? '') <= to)
      }
      const keyword = query.q
      if (typeof keyword === 'string' && keyword.length > 0) {
        list = list.filter((r) => matchText(r.pipeline_name, keyword) || String(r.id).includes(keyword))
      }
      list.sort((a, b) => b.id - a.id)
      return paginate(list.map(toRunSummary), query)
    },
  },
  {
    method: 'get',
    pattern: '/runs/:id',
    handler: ({ params }) => {
      const run = fx.runs.find((r) => r.id === intParam(params, 'id'))
      if (run === undefined) {
        throw notFound('运行')
      }
      return clone(run)
    },
  },
  {
    method: 'get',
    pattern: '/runs/:id/metrics',
    handler: ({ params, query }): ExecutionMetricPoint[] => {
      const limit = num(query.limit, 200)
      return clone(fx.runMetrics(intParam(params, 'id')).slice(-limit))
    },
  },
  {
    method: 'get',
    pattern: '/runs/:id/logs',
    handler: ({ params, query }): LogPage => {
      const runId = intParam(params, 'id')
      let lines: RunLogLine[] = fx.runLogs(runId)
      // 游标是 seq（单调递增），不是 offset：日志在运行中持续追加，offset 会漏行/重行
      const sinceSeq = num(query.since_seq, 0)
      if (sinceSeq > 0) {
        lines = lines.filter((l) => l.seq > sinceSeq)
      }
      if (typeof query.level === 'string' && query.level.length > 0) {
        lines = lines.filter((l) => l.level === query.level)
      }
      if (typeof query.q === 'string' && query.q.length > 0) {
        lines = lines.filter((l) => matchText(l.message, query.q))
      }
      if (typeof query.event_type === 'string' && query.event_type.length > 0) {
        lines = lines.filter((l) => matchText(l.event_type, query.event_type))
      }
      const limit = Math.max(1, num(query.limit, 100))
      const truncated = lines.length > limit
      const window = lines.slice(0, limit)
      const last = window[window.length - 1]
      return {
        items: clone(window),
        // 无新行时回落到传入游标，调用方据此判断「追到底了」
        next_seq: last !== undefined ? last.seq : sinceSeq,
        truncated,
      }
    },
  },
  {
    method: 'get',
    pattern: '/runs/:id/quality-report',
    handler: ({ params }): QualityReport => clone(fx.runQuality(intParam(params, 'id'))),
  },
  {
    method: 'get',
    pattern: '/runs/:id/error-records',
    handler: ({ params, query }): ErrorRecordPage => {
      const records = fx.runErrors(intParam(params, 'id'))
      const page = Math.max(1, num(query.page, 1))
      const pageSize = Math.max(1, num(query.page_size, 20))
      const start = (page - 1) * pageSize
      return {
        items: clone(records.slice(start, start + pageSize)),
        meta: { page, page_size: pageSize, total: records.length },
      }
    },
  },
  {
    method: 'post',
    pattern: '/runs/:id/pause',
    handler: ({ params }): RunDetail => {
      const run = fx.runs.find((r) => r.id === intParam(params, 'id'))
      if (run === undefined) {
        throw notFound('运行')
      }
      if (!run.can_pause) {
        throw conflict('当前状态不可暂停')
      }
      run.status = 'paused'
      run.can_pause = false
      run.can_resume = true
      return clone(run)
    },
  },
  {
    method: 'post',
    pattern: '/runs/:id/resume',
    handler: ({ params }): RunDetail => {
      const run = fx.runs.find((r) => r.id === intParam(params, 'id'))
      if (run === undefined) {
        throw notFound('运行')
      }
      if (!run.can_resume) {
        throw conflict('当前状态不可恢复')
      }
      run.status = 'running'
      run.can_resume = false
      run.can_pause = true
      return clone(run)
    },
  },
  {
    method: 'post',
    pattern: '/runs/:id/cancel',
    handler: ({ params }): RunDetail => {
      const run = fx.runs.find((r) => r.id === intParam(params, 'id'))
      if (run === undefined) {
        throw notFound('运行')
      }
      if (!run.can_cancel) {
        throw conflict('当前状态不可取消')
      }
      run.status = 'cancelled'
      run.can_cancel = false
      run.can_pause = false
      run.can_resume = false
      run.finished_at = new Date().toISOString()
      return clone(run)
    },
  },
  {
    method: 'post',
    pattern: '/runs/:id/rollback',
    handler: ({ params }): RunDetail => {
      const run = fx.runs.find((r) => r.id === intParam(params, 'id'))
      if (run === undefined) {
        throw notFound('运行')
      }
      if (!run.can_rollback) {
        throw conflict('当前状态不可回滚')
      }
      run.status = 'rolled_back'
      run.can_rollback = false
      return clone(run)
    },
  },
  {
    method: 'get',
    pattern: '/runs/:id/diagnosis',
    handler: ({ params }): Diagnosis => clone(fx.runDiagnosis(intParam(params, 'id'))),
  },
  {
    method: 'post',
    pattern: '/runs/:id/diagnosis',
    handler: ({ params }): Diagnosis => clone(fx.runDiagnosis(intParam(params, 'id'))),
  },

  /* ----------------------------- 总览工作台 -------------------------- */
  {
    method: 'get',
    pattern: '/monitor/summary',
    handler: ({ query }) => clone(fx.monitorSummaries[monitorRangeOf(query.range)]),
  },
  {
    method: 'get',
    pattern: '/monitor/metrics',
    handler: ({ query }) => clone(fx.monitorSummaries[monitorRangeOf(query.range)].metrics),
  },
  {
    method: 'get',
    pattern: '/monitor/series',
    handler: ({ query }) => clone(fx.monitorSummaries[monitorRangeOf(query.range)].series),
  },
  {
    method: 'get',
    pattern: '/monitor/resource-distribution',
    handler: () => clone(fx.monitorSummaries['24h'].distribution),
  },
  {
    method: 'get',
    pattern: '/monitor/health',
    handler: () => clone(fx.monitorSummaries['24h'].health),
  },
  {
    method: 'get',
    pattern: '/monitor/assets',
    handler: () => clone(fx.monitorSummaries['24h'].assets),
  },
  {
    method: 'get',
    pattern: '/monitor/quality-summary',
    handler: ({ query }) => clone(fx.qualitySummary(monitorRangeOf(query.range))),
  },

  /* ------------------------------ 治理 ------------------------------ */
  {
    method: 'get',
    pattern: '/governance/policy-rules',
    handler: ({ query }) => {
      let list = [...fx.policyRules]
      const levels = csvList(query.risk_level)
      if (levels !== undefined) {
        list = list.filter((r) => levels.includes(r.level))
      }
      if (query.enabled === true || query.enabled === 'true') {
        list = list.filter((r) => r.enabled)
      } else if (query.enabled === false || query.enabled === 'false') {
        list = list.filter((r) => !r.enabled)
      }
      list = list.filter((r) => matchText(`${r.code} ${r.title}`, query.q))
      list.sort((a, b) => a.level.localeCompare(b.level) || a.id - b.id)
      return paginate(list.map(clone), query)
    },
  },
  {
    method: 'post',
    pattern: '/governance/policy-rules',
    handler: ({ body }) => {
      const payload = toRecord(body)
      if (typeof payload.code !== 'string' || payload.code.length === 0) {
        throw validation('规则编码必填', { fields: [{ loc: 'code', reason: '必填' }] })
      }
      policyRuleSeq += 1
      const rule = {
        id: policyRuleSeq,
        code: payload.code,
        version: 1,
        level: (typeof payload.level === 'string' ? payload.level : 'P2') as (typeof fx.policyRules)[number]['level'],
        title: typeof payload.title === 'string' ? payload.title : payload.code,
        description: typeof payload.description === 'string' ? payload.description : '',
        condition: toRecord(payload.condition) as (typeof fx.policyRules)[number]['condition'],
        required_slots: (Array.isArray(payload.required_slots) ? payload.required_slots : []) as (typeof fx.policyRules)[number]['required_slots'],
        enabled: payload.enabled !== false,
        updated_by: 1,
        updated_by_name: '林思远',
        updated_at: new Date().toISOString(),
      }
      fx.policyRules.push(rule)
      return clone(rule)
    },
  },
  {
    method: 'put',
    pattern: '/governance/policy-rules/:ruleId',
    handler: ({ params, body }) => {
      const rule = fx.policyRules.find((r) => r.id === intParam(params, 'ruleId'))
      if (rule === undefined) {
        throw notFound('策略规则')
      }
      const patch = toRecord(body)
      if (typeof patch.title === 'string') {
        rule.title = patch.title
      }
      if (typeof patch.description === 'string') {
        rule.description = patch.description
      }
      if (typeof patch.level === 'string') {
        rule.level = patch.level as (typeof fx.policyRules)[number]['level']
      }
      if (patch.condition !== undefined) {
        rule.condition = toRecord(patch.condition) as (typeof fx.policyRules)[number]['condition']
      }
      if (Array.isArray(patch.required_slots)) {
        rule.required_slots = patch.required_slots as (typeof fx.policyRules)[number]['required_slots']
      }
      if (typeof patch.enabled === 'boolean') {
        rule.enabled = patch.enabled
      }
      rule.version += 1
      rule.updated_at = new Date().toISOString()
      return clone(rule)
    },
  },
  {
    method: 'delete',
    pattern: '/governance/policy-rules/:ruleId',
    handler: ({ params }) => {
      const index = fx.policyRules.findIndex((r) => r.id === intParam(params, 'ruleId'))
      if (index < 0) {
        throw notFound('策略规则')
      }
      fx.policyRules.splice(index, 1)
      return null
    },
  },
  {
    method: 'post',
    pattern: '/governance/policy-rules/:ruleId/toggle',
    handler: ({ params, body }) => {
      const rule = fx.policyRules.find((r) => r.id === intParam(params, 'ruleId'))
      if (rule === undefined) {
        throw notFound('策略规则')
      }
      const payload = toRecord(body)
      rule.enabled = payload.enabled === true
      rule.updated_at = new Date().toISOString()
      return clone(rule)
    },
  },
  {
    method: 'get',
    pattern: '/governance/policy-versions',
    handler: ({ query }) => paginate(fx.policyVersions.map(clone), query),
  },
  {
    method: 'get',
    pattern: '/governance/policy-versions/diff',
    handler: () => {
      const entries = clone(fx.policyDiff)
      return {
        entries,
        summary: {
          added: entries.filter((e) => e.change === 'added').length,
          removed: entries.filter((e) => e.change === 'removed').length,
          changed: entries.filter((e) => e.change === 'modified').length,
        },
      }
    },
  },
  {
    method: 'post',
    pattern: '/governance/policy-versions',
    handler: () => {
      const latest = fx.policyVersions[fx.policyVersions.length - 1]
      if (latest === undefined) {
        throw notFound('策略版本')
      }
      return clone(latest)
    },
  },
  {
    method: 'get',
    pattern: '/governance/budget-defaults',
    handler: () => clone(fx.budgetDefaults),
  },
  {
    method: 'put',
    pattern: '/governance/budget-defaults',
    handler: ({ body }) => {
      const payload = toRecord(body)
      const override: Partial<BudgetDefaults> = {}
      const keys = Object.keys(fx.budgetDefaults.hard_limits) as Array<keyof BudgetDefaults>
      for (const key of keys) {
        const value = payload[key]
        if (typeof value === 'number') {
          const cap = fx.budgetDefaults.hard_limits[key]
          // 硬上限不可从前端突破：越界一律 VALIDATION_ERROR，而不是静默截断
          if (value > cap) {
            throw validation(`字段 ${key} 超出硬上限 ${cap}`, {
              fields: [{ loc: key, reason: `不得超过 ${cap}` }],
            })
          }
          override[key] = value
        }
      }
      fx.budgetDefaults.override = override
      return clone(fx.budgetDefaults)
    },
  },
  {
    method: 'get',
    pattern: '/governance/benchmark/suites',
    handler: () => clone(fx.benchmarkSuites),
  },
  {
    method: 'get',
    pattern: '/governance/benchmark/runs',
    handler: ({ query }) => {
      let list = [...fx.benchmarkRuns]
      if (typeof query.layer === 'string' && query.layer.length > 0) {
        list = list.filter((r) => r.layer === query.layer)
      }
      list.sort((a, b) => b.id - a.id)
      return paginate(list.map(clone), query)
    },
  },
  {
    method: 'post',
    pattern: '/governance/benchmark/runs',
    handler: ({ body }) => {
      const payload = toRecord(body)
      const suite = fx.benchmarkSuites.find((s) => s.id === num(payload.suite_id, Number.NaN))
      if (suite === undefined) {
        throw validation('评测套件不存在', { fields: [{ loc: 'suite_id', reason: '未知套件' }] })
      }
      benchmarkRunSeq += 1
      const run = {
        id: benchmarkRunSeq,
        suite_id: suite.id,
        suite_name: suite.name,
        suite_version: suite.version,
        layer: suite.layer,
        status: 'running' as const,
        model_version: '2026-08-01',
        prompt_version: 'etlplan-v3',
        cache_mode: false,
        metrics: { gen_success_rate: 0, gate_pass_rate: 0, p0p1_block_rate: 0, false_positive_rate: 0, recovery_rate: 0 },
        triggered_by: 1,
        triggered_by_name: '林思远',
        started_at: new Date().toISOString(),
        finished_at: null,
        error_message: null,
      }
      fx.benchmarkRuns.push(run)
      return clone(run)
    },
  },
  {
    method: 'get',
    pattern: '/governance/benchmark/runs/:runId',
    handler: ({ params }) => {
      const run = fx.benchmarkRuns.find((r) => r.id === intParam(params, 'runId'))
      if (run === undefined) {
        throw notFound('评测运行')
      }
      return clone(run)
    },
  },
  {
    method: 'get',
    pattern: '/governance/benchmark/runs/:runId/cases',
    handler: ({ params }) => clone(fx.benchmarkCases(intParam(params, 'runId'))),
  },
  {
    method: 'get',
    pattern: '/governance/evidence',
    handler: ({ query }): EvidencePage => filterEvidence(query),
  },
  {
    method: 'post',
    pattern: '/governance/evidence/verify',
    handler: ({ body }) => {
      const payload = toRecord(body)
      // 项目 2 固定给出断链结果，让界面的「验链失败」态可被看到；其余项目返回正常链
      return clone(num(payload.project_id, 1) === 2 ? fx.ledgerVerificationBroken : fx.ledgerVerification)
    },
  },
]

/**
 * 账本查询：游标分页（与 `EvidenceFilter` 的字段一一对应）。
 * 独立成函数而非内联，是因为它的过滤项比其他列表接口多，值得单独看。
 */
function filterEvidence(query: Query): EvidencePage {
  let list = [...fx.evidenceEvents]
  const projectId = num(query.project_id, Number.NaN)
  if (Number.isFinite(projectId)) {
    list = list.filter((e) => e.project_id === projectId)
  }
  const eventTypes = csvList(query.event_type)
  if (eventTypes !== undefined) {
    list = list.filter((e) => eventTypes.includes(e.event_type))
  }
  if (typeof query.subject_id === 'string' && query.subject_id.length > 0) {
    list = list.filter((e) => e.subject_id === query.subject_id)
  }
  if (Number.isFinite(num(query.pipeline_version_id, Number.NaN))) {
    const wanted = num(query.pipeline_version_id, 0)
    list = list.filter((e) => e.subject_type === 'pipeline_version' && Number(e.subject_id) === wanted)
  }
  if (Number.isFinite(num(query.execution_run_id, Number.NaN))) {
    const wanted = num(query.execution_run_id, 0)
    list = list.filter((e) => e.subject_type === 'execution_run' && Number(e.subject_id) === wanted)
  }
  // 同上：收窄到局部常量，回调内才拿得到窄化后的类型
  const from = typeof query.from === 'string' && query.from.length > 0 ? query.from : null
  if (from !== null) {
    list = list.filter((e) => e.occurred_at >= from)
  }
  const to = typeof query.to === 'string' && query.to.length > 0 ? query.to : null
  if (to !== null) {
    list = list.filter((e) => e.occurred_at <= to)
  }
  list.sort((a, b) => a.seq - b.seq)
  const cursor = num(query.cursor, 0)
  if (cursor > 0) {
    list = list.filter((e) => e.seq > cursor)
  }
  const limit = Math.max(1, num(query.limit, 20))
  const window = list.slice(0, limit)
  const last = window[window.length - 1]
  const hasMore = list.length > window.length
  return {
    items: clone(window),
    next_cursor: hasMore && last !== undefined ? String(last.seq) : null,
  }
}

/** MonitorRange 校验：非法值回落 24h，避免脏 query 让页面白屏 */
function monitorRangeOf(value: unknown): MonitorRange {
  if (value === '7d' || value === '30d') {
    return value
  }
  return '24h'
}

/* ------------------------------------------------------------------ */
/* 路由编译与匹配                                                      */
/* ------------------------------------------------------------------ */

function compile(pattern: string): { regex: RegExp; keys: string[] } {
  const keys: string[] = []
  const source = pattern
    .split('/')
    .map((segment) => {
      if (segment.startsWith(':')) {
        keys.push(segment.slice(1))
        return '([^/]+)'
      }
      // 静态段转义，避免 '.' 之类的字符被当成通配
      return segment.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
    })
    .join('/')
  return { regex: new RegExp(`^${source}$`), keys }
}

const COMPILED: CompiledRoute[] = ROUTES.map((route) => ({ ...route, ...compile(route.pattern) }))

/** 去掉 baseURL 与 query，得到用于匹配的纯路径 */
function pathOf(config: InternalAxiosRequestConfig): string {
  const raw = config.url ?? ''
  const base = config.baseURL ?? ''
  let path = raw
  if (base.length > 0 && path.startsWith(base)) {
    path = path.slice(base.length)
  }
  const queryAt = path.indexOf('?')
  if (queryAt >= 0) {
    path = path.slice(0, queryAt)
  }
  const hashAt = path.indexOf('#')
  if (hashAt >= 0) {
    path = path.slice(0, hashAt)
  }
  if (path.length === 0) {
    path = '/'
  }
  return path.startsWith('/') ? path : `/${path}`
}

/**
 * 取查询参数。
 * axios 各版本对「query 是留在 config.params 还是拼进 url」处理不同，这里两处都读，保证一致。
 */
function queryOf(config: InternalAxiosRequestConfig): Query {
  const out: Query = {}
  const params = config.params
  if (params !== null && typeof params === 'object') {
    for (const [key, value] of Object.entries(params as Record<string, unknown>)) {
      out[key] = value
    }
  }
  const raw = config.url ?? ''
  const queryAt = raw.indexOf('?')
  if (queryAt >= 0) {
    const search = new URLSearchParams(raw.slice(queryAt + 1))
    search.forEach((value, key) => {
      if (out[key] === undefined) {
        out[key] = value
      }
    })
  }
  return out
}

function bodyOf(config: InternalAxiosRequestConfig): unknown {
  const data = config.data
  if (typeof data === 'string' && data.length > 0) {
    try {
      return JSON.parse(data)
    } catch {
      return data
    }
  }
  return data
}

function idempotencyKeyOf(config: InternalAxiosRequestConfig): string | null {
  const fromConfig = config._idempotencyKey
  if (typeof fromConfig === 'string' && fromConfig.length > 0) {
    return fromConfig
  }
  const headers = config.headers
  if (headers !== undefined && typeof headers.get === 'function') {
    const value = headers.get('Idempotency-Key')
    if (typeof value === 'string' && value.length > 0) {
      return value
    }
  }
  return null
}

async function dispatch(config: InternalAxiosRequestConfig): Promise<unknown> {
  const method = (config.method ?? 'get').toLowerCase()
  const path = pathOf(config)

  // 在每个请求前统一推进异步态，而不是逐个 handler 里补：
  // 漏掉任何一个读取探查状态的接口，都会重现「永远 probing」或「列表与详情互相矛盾」。
  fx.settleProbeState()

  const idempotencyKey = idempotencyKeyOf(config)
  const cacheKey = idempotencyKey === null ? null : `${method} ${path} ${idempotencyKey}`

  if (cacheKey !== null && idempotencyCache.has(cacheKey)) {
    // 命中即回放第一次的响应，绝不再执行一次副作用
    return idempotencyCache.get(cacheKey)
  }

  for (const route of COMPILED) {
    if (route.method !== method) {
      continue
    }
    const matched = route.regex.exec(path)
    if (matched === null) {
      continue
    }
    const params: Record<string, string> = {}
    route.keys.forEach((key, index) => {
      params[key] = decodeURIComponent(matched[index + 1] ?? '')
    })
    const payload = await route.handler({
      params,
      query: queryOf(config),
      body: bodyOf(config),
      config,
    })
    if (cacheKey !== null) {
      idempotencyCache.set(cacheKey, payload)
    }
    return payload
  }

  // 未匹配必须出声：静默 404 会让人以为是后端没实现，而不是 mock 漏了路由
  console.warn(`[mock] 未匹配到路由：${method.toUpperCase()} ${path}（请检查 src/api/mock/index.ts 的 ROUTES）`)
  throw new MockHttpError(404, 'NOT_FOUND', `mock 未实现该接口：${method.toUpperCase()} ${path}`)
}

/* ------------------------------------------------------------------ */
/* 适配器本体                                                          */
/* ------------------------------------------------------------------ */

const STATUS_TEXT: Record<number, string> = {
  400: 'Bad Request',
  401: 'Unauthorized',
  403: 'Forbidden',
  404: 'Not Found',
  409: 'Conflict',
  422: 'Unprocessable Entity',
  423: 'Locked',
  500: 'Internal Server Error',
}

function envelopeHeaders(): AxiosHeaders {
  return new AxiosHeaders({ 'Content-Type': 'application/json' })
}

/** 模拟网络往返耗时；这一处 Math.random 只影响时序，不影响数据 */
function delay(): Promise<void> {
  return new Promise((resolve) => {
    setTimeout(resolve, 120 + Math.random() * 180)
  })
}

export const mockAdapter: AxiosAdapter = async (config: InternalAxiosRequestConfig) => {
  await delay()
  requestSeq += 1
  const requestId = `req_mock_${requestSeq.toString(36)}`

  try {
    const payload = await dispatch(config)
    const response: AxiosResponse = {
      data: { code: 'OK', message: 'ok', request_id: requestId, data: payload },
      status: 200,
      statusText: 'OK',
      headers: envelopeHeaders(),
      config,
    }
    return response
  } catch (error) {
    if (error instanceof MockHttpError) {
      const body: ApiErrorResponse = {
        code: error.code,
        message: error.message,
        request_id: requestId,
        details: error.details,
      }
      const response: AxiosResponse<ApiErrorResponse> = {
        data: body,
        status: error.status,
        statusText: STATUS_TEXT[error.status] ?? 'Error',
        headers: envelopeHeaders(),
        config,
      }
      // 构造真正的 AxiosError：client.ts 的响应拦截器读 error.config / error.response
      const axiosError = new AxiosError<ApiErrorResponse>(
        error.message,
        error.code,
        config,
        undefined,
        response,
      )
      axiosError.isAxiosError = true
      throw axiosError
    }
    throw error
  }
}

/* ------------------------------------------------------------------ */
/* 安装                                                                */
/* ------------------------------------------------------------------ */

let installed = false

/**
 * 把 mock 适配器挂到共享的 axios 实例上。
 * 幂等：重复调用不会叠加；生产构建下直接拒绝安装（mock 数据不得混进审计视图）。
 */
export function installMockAdapter(): void {
  if (installed) {
    return
  }
  if (import.meta.env.PROD) {
    console.error('[mock] 生产构建下拒绝安装 mock 适配器；请检查 VITE_USE_MOCK 配置。')
    return
  }
  http.defaults.adapter = mockAdapter
  installed = true
  console.info('[mock] 已接管全部 /api/v1 请求（VITE_USE_MOCK=true）')
}

export default installMockAdapter
