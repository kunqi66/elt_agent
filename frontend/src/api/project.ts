/**
 * 项目、成员与职责槽（fe-08 / be-02）。全部接口「待后端 be-02 落地」。
 *
 * 幂等：写接口一律带 Idempotency-Key（由 client.ts 拦截器自动注入），
 * 这里不手工传，保证「一次用户动作 = 一个 key」的语义只有一处实现。
 */

import { httpDelete, httpGet, httpPost, httpPut } from './client'
import type {
  MemberAddRequest,
  MemberRead,
  ProjectRead,
  ProjectUpdate,
  RoleAssignmentRead,
  RoleGrantRequest,
  UserCandidate,
} from '@/types'

/* ------------------------------------------------------------------ */
/* 项目                                                                */
/* ------------------------------------------------------------------ */

export function fetchProjects(): Promise<ProjectRead[]> {
  return httpGet<ProjectRead[]>('/projects')
}

export function fetchProject(projectId: number): Promise<ProjectRead> {
  return httpGet<ProjectRead>(`/projects/${projectId}`)
}

/** 只接受 name / description；env 与 code 刻意不可改（fe-08 §6.2） */
export function updateProject(projectId: number, patch: ProjectUpdate): Promise<ProjectRead> {
  return httpPut<ProjectRead>(`/projects/${projectId}`, patch)
}

/* ------------------------------------------------------------------ */
/* 成员                                                                */
/* ------------------------------------------------------------------ */

export function fetchMembers(projectId: number): Promise<MemberRead[]> {
  return httpGet<MemberRead[]>(`/projects/${projectId}/members`)
}

export function addMember(projectId: number, payload: MemberAddRequest): Promise<MemberRead> {
  return httpPost<MemberRead>(`/projects/${projectId}/members`, payload)
}

/** 移除成员；reason 必填并随证据事件落账 */
export function removeMember(
  projectId: number,
  memberId: number,
  reason: string,
): Promise<null> {
  return httpDelete<null>(`/projects/${projectId}/members/${memberId}`, { data: { reason } })
}

/**
 * 候选用户：本租户内、尚未加入本项目。
 * 不走「拉全租户用户再前端过滤」——那会把全租户用户暴露给普通成员（fe-08 §10）。
 */
export function fetchMemberCandidates(projectId: number): Promise<UserCandidate[]> {
  return httpGet<UserCandidate[]>(`/projects/${projectId}/member-candidates`)
}

/* ------------------------------------------------------------------ */
/* 职责槽                                                              */
/* ------------------------------------------------------------------ */

export function fetchRoleAssignments(projectId: number): Promise<RoleAssignmentRead[]> {
  return httpGet<RoleAssignmentRead[]>(`/projects/${projectId}/role-assignments`)
}

export function grantRole(
  projectId: number,
  payload: RoleGrantRequest,
): Promise<RoleAssignmentRead> {
  return httpPost<RoleAssignmentRead>(`/projects/${projectId}/role-assignments`, payload)
}

/** 回收角色：物理删除行，历史由证据账本承载（fe-08 §5） */
export function revokeRole(
  projectId: number,
  assignmentId: number,
  reason: string,
): Promise<null> {
  return httpDelete<null>(`/projects/${projectId}/role-assignments/${assignmentId}`, {
    data: { reason },
  })
}
