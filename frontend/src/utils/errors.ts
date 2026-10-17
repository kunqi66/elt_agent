/**
 * 错误码 → 提示方式与文案（fe-01 §6.2）。
 *
 * 分支一律基于 BizError.code，**不用 message 判断**。
 * 未登记的 code 直接展示后端 message —— 后端 message 已是可解释中文，
 * 前端再兜一层「未知错误」只会掩盖信息。
 */

import { BizError } from '@/api/client'

/** 提示方式：行内（表单字段）、toast、整页（403/404 等） */
export type ErrorPresentation = 'inline' | 'toast' | 'page'

export interface ErrorSpec {
  presentation: ErrorPresentation
  message: string
}

const SPECS: Record<string, ErrorSpec> = {
  BAD_CREDENTIALS: { presentation: 'inline', message: '用户名或密码错误' },
  ACCOUNT_LOCKED: { presentation: 'inline', message: '账号已被临时锁定，请稍后重试' },
  TOKEN_EXPIRED: { presentation: 'toast', message: '登录状态已失效，请重新登录' },
  UNAUTHENTICATED: { presentation: 'toast', message: '登录状态已失效，请重新登录' },
  FORBIDDEN: { presentation: 'toast', message: '无权执行该操作' },
  ROLE_REQUIRED: { presentation: 'toast', message: '缺少所需职责槽' },
  TENANT_MISMATCH: { presentation: 'toast', message: '跨租户访问被拒绝' },
  VALIDATION_ERROR: { presentation: 'inline', message: '参数校验失败' },
  NOT_FOUND: { presentation: 'page', message: '资源不存在' },
  CONFLICT: { presentation: 'toast', message: '数据已被修改，请刷新后重试' },
  IDEMPOTENCY_CONFLICT: { presentation: 'toast', message: '请求与已有操作冲突，请刷新后重试' },
  RATE_LIMITED: { presentation: 'toast', message: '操作过于频繁，请稍后重试' },
  INTERNAL_ERROR: { presentation: 'toast', message: '服务内部错误' },
  NETWORK_ERROR: { presentation: 'toast', message: '网络异常，请稍后重试' },

  // 领域类：文案直接取自各 feature 文档的「错误码与拒绝语义」表
  DATASOURCE_UNREACHABLE: { presentation: 'toast', message: '数据源不可达' },
  DATASOURCE_AUTH_FAILED: { presentation: 'toast', message: '认证失败或凭据无最小权限' },
  DATASOURCE_TIMEOUT: { presentation: 'toast', message: '数据源连接超时' },
  METADATA_PROFILE_EXPIRED: { presentation: 'toast', message: '元数据已过期，需重新探查' },
  LLM_UNAVAILABLE: { presentation: 'toast', message: '模型不可用，请稍后重试' },
  PLAN_INVALID: { presentation: 'toast', message: '方案结构校验失败' },
  HOCON_SYNTAX_ERROR: { presentation: 'toast', message: 'HOCON 语法错误' },
  SCHEMA_MISMATCH: { presentation: 'toast', message: '源目标 Schema 不匹配' },
  GATE_BLOCKED: { presentation: 'toast', message: '存在门禁阻断项，无法冻结版本' },
  POLICY_BLOCKED: { presentation: 'toast', message: '命中 P0/P1 策略，操作被阻断' },
  AUTOFIX_EXHAUSTED: { presentation: 'toast', message: '已达自动修复上限，请人工处理' },
  VERSION_IMMUTABLE: { presentation: 'toast', message: '不可变版本禁止修改' },
  SELF_APPROVAL_FORBIDDEN: { presentation: 'toast', message: '申请人不得审批自己提交的准备单' },
  DUTY_CONFLICT: { presentation: 'toast', message: '高风险操作禁止同一人兼任多个审批职责' },
  APPROVAL_INCOMPLETE: { presentation: 'toast', message: '审批槽未集齐' },
  FINGERPRINT_MISMATCH: {
    presentation: 'page',
    message: '制品或输入指纹已变化，原审批全部失效',
  },
  PREPARATION_EXPIRED: { presentation: 'toast', message: '准备单已过期，请重新冻结版本' },
  CAPABILITY_INVALID: { presentation: 'toast', message: 'Capability 无效' },
  CAPABILITY_EXPIRED: { presentation: 'toast', message: 'Capability 已过期' },
  CAPABILITY_REPLAYED: { presentation: 'toast', message: 'Capability 已被消费（重放拦截）' },
  UNAUTHORIZED_TOOL: { presentation: 'toast', message: '未授权的工具调用被拒绝' },
  SUPERVISION_INTERRUPTED: { presentation: 'toast', message: '超出运行预算，已监督硬中断' },
  QUALITY_REJECTED: { presentation: 'toast', message: '质量未达标，禁止发布' },
  RUN_CONFLICT: { presentation: 'toast', message: '同一版本已有活跃运行' },
  SWAP_FAILED: { presentation: 'toast', message: '原子 Swap 失败，正式表未被修改' },
  ROLLBACK_FAILED: { presentation: 'toast', message: '自动回滚失败，需人工介入' },
}

/**
 * 非 BizError 的兜底规格：网络断开、后端 500 未被拦截器包成 BizError、
 * 以及代码 bug 都会落到这里。
 *
 * 文案刻意不提「后端」——用户不关心是谁的问题，只关心「要不要我再试一次」。
 */
const UNKNOWN_ERROR: ErrorSpec = {
  presentation: 'toast',
  message: '请求失败，请稍后重试；若持续出现请附 request_id 联系管理员',
}

/**
 * 把 unknown 归一化成 ErrorSpec。
 *
 * 这三个函数收 `unknown` 而不是 `BizError` 是有意的：
 * 调用点永远在 `catch (err)` 里，而 TS 给 catch 变量的类型就是 `unknown`。
 * 要求调用方先 `isBizError` 收窄，等于在**每一个** catch 里重复一遍同样的守卫——
 * 漏写一处就是一个类型错误，而这类错误对使用者毫无信息量。
 */
function specOf(err: unknown): ErrorSpec {
  if (!isBizError(err)) {
    if (err instanceof Error && err.message !== '') {
      return { presentation: 'toast', message: err.message }
    }
    return UNKNOWN_ERROR
  }
  return SPECS[err.code] ?? { presentation: 'toast', message: err.message }
}

/** 取错误码对应的处理规格；未登记则回落到「toast + 后端 message」 */
export function errorSpec(err: unknown): ErrorSpec {
  return specOf(err)
}

/** 取可直接展示给用户的文案（含 request_id 便于与后端 structlog 对账） */
export function errorMessage(err: unknown): string {
  return specOf(err).message
}

/**
 * 把 VALIDATION_ERROR 的 details.fields 映射为「字段 → 原因」。
 * 后端结构：{ fields: [{ loc: string, reason: string }] }
 */
export function fieldErrors(err: unknown): Record<string, string> {
  if (!isBizError(err) || err.code !== 'VALIDATION_ERROR') {
    return {}
  }
  const raw = err.details['fields']
  if (!Array.isArray(raw)) {
    return {}
  }
  const out: Record<string, string> = {}
  for (const item of raw) {
    if (typeof item !== 'object' || item === null) {
      continue
    }
    const loc = (item as { loc?: unknown }).loc
    const reason = (item as { reason?: unknown }).reason
    if (typeof loc === 'string' && typeof reason === 'string') {
      out[loc] = reason
    }
  }
  return out
}

/** 类型守卫：调用方 catch 到的 unknown 是否为 BizError */
export function isBizError(e: unknown): e is BizError {
  return e instanceof BizError
}

/** 把任意 catch 到的东西压成一行可展示文案（含 request_id） */
export function toDisplayMessage(e: unknown): string {
  // 只有 BizError 才带得上 request_id：它是后端响应体里的字段，
  // 网络层异常根本没有这个值，硬拼一个空的 `(request_id: )` 只会误导排查。
  if (isBizError(e) && e.requestId !== '') {
    return `${specOf(e).message}（request_id: ${e.requestId}）`
  }
  return specOf(e).message
}
