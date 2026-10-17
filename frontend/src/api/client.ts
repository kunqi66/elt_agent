import axios from 'axios'
import type {
  AxiosError,
  AxiosInstance,
  AxiosRequestConfig,
  AxiosResponse,
  InternalAxiosRequestConfig,
} from 'axios'
import { ElMessage } from '@/utils/feedback'
import type { ApiEnvelope, ApiErrorResponse, LoginUser, PersistedAuth, TokenPair } from '@/types'
import { uuidv4 } from '@/utils/uuid'

/* ------------------------------------------------------------------ */
/* axios 配置的类型扩展                                                */
/* ------------------------------------------------------------------ */

declare module 'axios' {
  export interface AxiosRequestConfig {
    /** 该请求已因 401 重放过一次，避免刷新死循环 */
    _retry?: boolean
    /** 跳过 401 静默刷新（/auth/login、/auth/refresh 必须置 true，否则会递归） */
    skipAuthRefresh?: boolean
    /** 跳过拦截器的统一错误提示，由调用方自行处理 */
    skipErrorToast?: boolean
    /**
     * 本次动作的幂等键。
     * 由请求拦截器生成并挂在 config 上，401 刷新重放时**沿用同一个 key**，
     * 保证不会因刷新重复创建资源（fe-01 §6.1）。
     */
    _idempotencyKey?: string
    /**
     * 跳过 401 静默刷新**并且**不触发未授权回调（不登出）。
     * 用于「改密码传了旧密码」这类 401——那是业务校验失败，不是会话失效（fe-08 §10）。
     */
    skipUnauthorizedHandler?: boolean
  }
}

/** 需要幂等键的方法：可重放的写接口 */
const IDEMPOTENT_METHODS: readonly string[] = ['post', 'put', 'patch', 'delete']

/* ------------------------------------------------------------------ */
/* 业务错误                                                            */
/* ------------------------------------------------------------------ */

/** 拦截器统一抛出的业务错误：携带后端错误码、request_id 与 HTTP 状态码 */
export class BizError extends Error {
  readonly code: string
  readonly requestId: string
  readonly status: number
  readonly details: Record<string, unknown>

  constructor(
    message: string,
    code: string,
    requestId: string,
    status: number,
    details: Record<string, unknown> = {},
  ) {
    super(message)
    this.name = 'BizError'
    this.code = code
    this.requestId = requestId
    this.status = status
    this.details = details
  }
}

/* ------------------------------------------------------------------ */
/* 会话持久化（localStorage，不依赖 Pinia，供 store 与拦截器共用）      */
/* ------------------------------------------------------------------ */

const AUTH_STORAGE_KEY = 'elt-agent.auth'

/** 读取本地会话快照；格式非法时按未登录处理 */
export function readAuth(): PersistedAuth | null {
  const raw = localStorage.getItem(AUTH_STORAGE_KEY)
  if (raw === null) {
    return null
  }
  try {
    const parsed = JSON.parse(raw) as Partial<PersistedAuth>
    if (typeof parsed.accessToken !== 'string' || typeof parsed.refreshToken !== 'string') {
      return null
    }
    return {
      accessToken: parsed.accessToken,
      refreshToken: parsed.refreshToken,
      expiresAt: typeof parsed.expiresAt === 'number' ? parsed.expiresAt : 0,
      user: (parsed.user ?? null) as LoginUser | null,
    }
  } catch {
    // 本地数据损坏：直接当作未登录，避免整个应用起不来
    return null
  }
}

/** 写入会话快照 */
export function writeAuth(auth: PersistedAuth): void {
  localStorage.setItem(AUTH_STORAGE_KEY, JSON.stringify(auth))
}

/** 清空会话快照 */
export function clearAuth(): void {
  localStorage.removeItem(AUTH_STORAGE_KEY)
}

/* ------------------------------------------------------------------ */
/* 401 处理回调（由 main.ts 注册，避免 client 反向依赖 router/store）   */
/* ------------------------------------------------------------------ */

type UnauthorizedHandler = () => void

let unauthorizedHandler: UnauthorizedHandler | null = null

/** 注册「刷新失败、必须重新登录」时的回调 */
export function setUnauthorizedHandler(handler: UnauthorizedHandler): void {
  unauthorizedHandler = handler
}

/* ------------------------------------------------------------------ */
/* axios 实例与拦截器                                                  */
/* ------------------------------------------------------------------ */

const baseURL: string = import.meta.env.VITE_API_BASE_URL ?? '/api/v1'

export const http: AxiosInstance = axios.create({
  baseURL,
  timeout: 30000,
  headers: { 'Content-Type': 'application/json' },
})

// 请求拦截器：注入 Bearer token 与 Idempotency-Key
http.interceptors.request.use((config: InternalAxiosRequestConfig) => {
  const auth = readAuth()
  if (auth !== null && auth.accessToken.length > 0) {
    config.headers.set('Authorization', `Bearer ${auth.accessToken}`)
  }

  // 幂等键：一次用户动作 = 一个 key。已存在则复用（401 重放走的就是这条路径），
  // 保证「刷新令牌后重放」不会变成第二次创建。
  const method = (config.method ?? 'get').toLowerCase()
  if (IDEMPOTENT_METHODS.includes(method) && !config.headers.has('Idempotency-Key')) {
    config._idempotencyKey ??= uuidv4()
    config.headers.set('Idempotency-Key', config._idempotencyKey)
  }

  return config
})

/** 把 axios 异常规整成 BizError（错误信封可能缺失，例如网络错误） */
function toBizError(error: AxiosError<ApiErrorResponse>): BizError {
  const status = error.response?.status ?? 0
  const body = error.response?.data
  const code = body?.code ?? (status === 0 ? 'NETWORK_ERROR' : `HTTP_${status}`)
  const message = body?.message ?? (status === 0 ? '网络异常，请稍后重试' : '请求失败，请稍后重试')
  return new BizError(message, code, body?.request_id ?? '', status, body?.details ?? {})
}

/** 正在进行的刷新请求；并发 401 共用同一次刷新，避免刷新风暴 */
let refreshing: Promise<boolean> | null = null

/** 用 refresh_token 静默刷新；裸 axios 发送以绕开拦截器，防止 401 递归 */
async function runRefresh(): Promise<boolean> {
  const auth = readAuth()
  if (auth === null || auth.refreshToken.length === 0) {
    return false
  }
  try {
    const response = await axios.post<ApiEnvelope<TokenPair>>(
      `${baseURL}/auth/refresh`,
      { refresh_token: auth.refreshToken },
      { timeout: 15000, headers: { 'Content-Type': 'application/json' } },
    )
    const pair = response.data?.data
    if (pair === undefined || pair.access_token.length === 0) {
      return false
    }
    writeAuth({
      accessToken: pair.access_token,
      refreshToken: pair.refresh_token,
      expiresAt: Date.now() + pair.expires_in * 1000,
      user: pair.user ?? auth.user,
    })
    return true
  } catch {
    // 刷新失败（refresh_token 过期/被吊销）：交给调用方走重新登录
    return false
  }
}

// 响应拦截器：成功剥信封，失败抛 BizError；401 先静默刷新一次再重放
http.interceptors.response.use(
  (response: AxiosResponse) => {
    const envelope = response.data as Partial<ApiEnvelope<unknown>> | null
    if (envelope !== null && typeof envelope === 'object' && typeof envelope.code === 'string' && 'data' in envelope) {
      if (envelope.code !== 'OK') {
        // HTTP 200 但业务失败：当前后端处理器均返回非 2xx，这里是防御性兜底——
        // 若漏判会把失败响应当成功数据渲染，是比多一条分支严重得多的错误。
        const bizError = new BizError(
          typeof envelope.message === 'string' && envelope.message !== ''
            ? envelope.message
            : '请求失败，请稍后重试',
          envelope.code,
          typeof envelope.request_id === 'string' ? envelope.request_id : '',
          response.status,
        )
        if (response.config.skipErrorToast !== true) {
          ElMessage.error(bizError.message)
        }
        return Promise.reject(bizError)
      }
      // 统一信封：业务层只关心 data
      return envelope.data as unknown as AxiosResponse
    }
    return response
  },
  async (error: AxiosError<ApiErrorResponse>) => {
    const config = error.config
    const status = error.response?.status ?? 0

    if (
      status === 401 &&
      config !== undefined &&
      config._retry !== true &&
      config.skipAuthRefresh !== true &&
      config.skipUnauthorizedHandler !== true
    ) {
      config._retry = true
      if (refreshing === null) {
        refreshing = runRefresh().finally(() => {
          refreshing = null
        })
      }
      const pending = refreshing
      const refreshed = await pending

      if (refreshed) {
        // 刷新成功：重放原请求（请求拦截器会带上新 token）
        return await http.request(config)
      }

      clearAuth()
      unauthorizedHandler?.()
      const sessionError = new BizError(
        '登录状态已失效，请重新登录',
        'UNAUTHORIZED',
        error.response?.data?.request_id ?? '',
        401,
      )
      ElMessage.error(sessionError.message)
      return await Promise.reject(sessionError)
    }

    const bizError = toBizError(error)
    if (config?.skipErrorToast !== true) {
      ElMessage.error(bizError.message)
    }
    return await Promise.reject(bizError)
  },
)

/* ------------------------------------------------------------------ */
/* 便捷请求方法                                                        */
/*                                                                     */
/* 响应拦截器已把 `{code,message,data}` 信封剥成 data，所以运行时的值   */
/* 就是 T，而 axios 的静态签名仍描述「AxiosResponse」那一层。           */
/* 这里用一次 `as unknown as` 显式收口这处类型落差，而不是给每个调用点 */
/* 都写 `.then(r => r.data)` —— 信封在哪剥、类型在哪收口，都只此一处。  */
/* ------------------------------------------------------------------ */

export function httpGet<T>(url: string, config?: AxiosRequestConfig): Promise<T> {
  return http.get(url, config) as unknown as Promise<T>
}

export function httpPost<T>(url: string, data?: unknown, config?: AxiosRequestConfig): Promise<T> {
  return http.post(url, data, config) as unknown as Promise<T>
}

export function httpPut<T>(url: string, data?: unknown, config?: AxiosRequestConfig): Promise<T> {
  return http.put(url, data, config) as unknown as Promise<T>
}

export function httpDelete<T>(url: string, config?: AxiosRequestConfig): Promise<T> {
  return http.delete(url, config) as unknown as Promise<T>
}

export default http
