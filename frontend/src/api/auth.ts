import { httpGet, httpPost } from './client'
import type {
  ChangePasswordRequest,
  CurrentUser,
  LoginRequest,
  RefreshRequest,
  TenantRead,
  TokenPair,
} from '@/types'

/**
 * 登录。skipAuthRefresh 必须为 true：
 * 登录接口的 401 语义是「用户名或密码错误」，不是「令牌过期」，
 * 若走刷新逻辑会拿旧 refresh_token 反复刷新，形成死循环。
 */
export function login(payload: LoginRequest): Promise<TokenPair> {
  return httpPost<TokenPair>('/auth/login', payload, { skipAuthRefresh: true })
}

/** 刷新令牌（页面主动续期时使用；401 的重放刷新在 client.ts 拦截器内自动完成） */
export function refresh(refreshToken: string): Promise<TokenPair> {
  const payload: RefreshRequest = { refresh_token: refreshToken }
  return httpPost<TokenPair>('/auth/refresh', payload, { skipAuthRefresh: true })
}

/** 退出登录（需 Bearer） */
export function logout(): Promise<null> {
  return httpPost<null>('/auth/logout')
}

/** 获取当前用户画像（需 Bearer） */
export function fetchMe(): Promise<CurrentUser> {
  return httpGet<CurrentUser>('/auth/me')
}

/** 当前租户（只读，fe-08 §6.7） */
export function fetchTenant(): Promise<TenantRead> {
  return httpGet<TenantRead>('/auth/tenant')
}

/**
 * 修改密码（fe-08 §6.6）。
 *
 * 两个开关都必须给：
 * - skipAuthRefresh：401 的语义是「旧密码错误」（BAD_CREDENTIALS），
 *   不是会话失效；若去刷新会把一次笔误变成一次令牌轮换。
 * - skipUnauthorizedHandler：刷新失败路径会清会话并跳登录，
 *   用户只是打错旧密码不该被踢出去（fe-08 §10 已登记的风险点）。
 */
export function changePassword(payload: ChangePasswordRequest): Promise<null> {
  return httpPost<null>('/auth/password', payload, {
    skipAuthRefresh: true,
    skipUnauthorizedHandler: true,
    skipErrorToast: true,
  })
}

/** 退出全部会话：怀疑凭据泄露时使用（fe-08 §8） */
export function logoutAll(): Promise<null> {
  return httpPost<null>('/auth/logout-all')
}
