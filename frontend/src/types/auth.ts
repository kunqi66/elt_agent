/**
 * 认证与身份类型（be-02）。
 * fe-01 §1 明确：前端不解析 JWT，角色与项目一律以 GET /auth/me 为准。
 */

import type { RoleName, UserStatus } from './common'

/* ------------------------------------------------------------------ */
/* 请求体                                                              */
/* ------------------------------------------------------------------ */

/** POST /auth/login；tenant_code 为空时不提交，由后端按默认租户处理 */
export interface LoginRequest {
  username: string
  password: string
  tenant_code?: string
}

/** POST /auth/refresh */
export interface RefreshRequest {
  refresh_token: string
}

/** POST /auth/password */
export interface ChangePasswordRequest {
  old_password: string
  new_password: string
}

/* ------------------------------------------------------------------ */
/* 响应体                                                              */
/* ------------------------------------------------------------------ */

/** 登录接口返回的用户摘要 */
export interface LoginUser {
  id: number
  username: string
  email: string
  tenant_id: number
  display_name: string
}

/** 用户可访问的项目及其角色 */
export interface ProjectBrief {
  id: number
  name: string
  roles: string[]
}

/** GET /auth/me 返回的当前用户画像 */
export interface CurrentUser {
  id: number
  username: string
  email: string
  display_name: string
  tenant_id: number
  tenant_code: string
  projects: ProjectBrief[]
}

/** GET /auth/tenant：当前租户（只读） */
export interface TenantRead {
  id: number
  code: string
  name: string
  status: 'active' | 'suspended'
}

/** 用户简要信息；成员管理与角色矩阵引用 */
export interface UserBrief {
  id: number
  username: string
  display_name: string
  email: string
  status: UserStatus
}

/** 可加入项目的候选用户（本租户内、尚未加入） */
export interface UserCandidate extends UserBrief {
  already_member: boolean
}

/** login / refresh 返回的令牌对 */
export interface TokenPair {
  access_token: string
  refresh_token: string
  token_type: string
  /** 秒 */
  expires_in: number
  user: LoginUser
}

/** 项目内我的角色集合（由 auth store 从 profile.projects 折算） */
export type ProjectRoles = Record<number, RoleName[]>

/* ------------------------------------------------------------------ */
/* 本地持久化（fe-01 §4）                                               */
/* ------------------------------------------------------------------ */

/**
 * localStorage 中保存的会话快照。
 * **不含**任何敏感值：凭据、样本原文永不进任何前端存储。
 */
export interface PersistedAuth {
  accessToken: string
  refreshToken: string
  /** access_token 过期时刻（epoch 毫秒） */
  expiresAt: number
  user: LoginUser | null
}
