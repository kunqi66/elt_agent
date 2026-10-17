/**
 * 项目、成员与职责槽类型（fe-08 / be-02）。
 * fe-08 §8：按钮可见性一律取后端下发的 can_manage_*，不用前端缓存的角色反推。
 */

import type { Env, MemberStatus, ProjectStatus, RoleName } from './common'

/* ------------------------------------------------------------------ */
/* 项目                                                                */
/* ------------------------------------------------------------------ */

export interface ProjectRead {
  id: number
  tenant_id: number
  code: string
  name: string
  description: string
  env: Env
  status: ProjectStatus
  member_count: number
  /** 服务端算好的能力位；前端不推导 */
  can_manage_members: boolean
  can_manage_roles: boolean
}

/** PUT /projects/{id}；env 与 code 刻意不在其中——改标记必须新建项目（fe-08 §6.2） */
export interface ProjectUpdate {
  name?: string
  description?: string
}

/** 项目上下文持久化快照：只存 id 与名称，不含角色（角色可能已被回收） */
export interface PersistedProject {
  id: number
  name: string
  env: Env
}

/* ------------------------------------------------------------------ */
/* 成员                                                                */
/* ------------------------------------------------------------------ */

export interface MemberRead {
  /** project_member.id，不是 user_id */
  id: number
  project_id: number
  user_id: number
  username: string
  display_name: string
  email: string
  status: MemberStatus
  roles: RoleName[]
  joined_at: string
}

export interface MemberAddRequest {
  user_id: number
  /** 操作理由，必填且随证据事件落账（fe-08 §8） */
  reason: string
}

/* ------------------------------------------------------------------ */
/* 职责槽授予                                                          */
/* ------------------------------------------------------------------ */

/**
 * role_assignment 无 status 列，界面据时间窗派生三个展示态（fe-08 §5）。
 */
export type AssignmentDisplayState = 'effective' | 'scheduled' | 'expired'

export interface RoleAssignmentRead {
  id: number
  project_id: number
  member_id: number
  user_id: number
  user_name: string
  role: RoleName
  granted_by: number | null
  granted_by_name: string
  granted_at: string
  valid_from: string | null
  valid_to: string | null
  reason: string | null
}

export interface RoleGrantRequest {
  user_id: number
  role: RoleName
  valid_from?: string | null
  valid_to?: string | null
  reason: string
}

/** 角色矩阵的一行，用于 RoleMatrixTable 渲染（fe-08 §6.3） */
export interface RoleMatrixRow {
  role: RoleName
  prepare: boolean
  approve_data: boolean
  approve_security: boolean
  commit: boolean
  manage_policy: boolean
  view_ledger: boolean
}
