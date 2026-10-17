/**
 * 数据源、元数据探查与脱敏样本（fe-03 / be-03 be-04）。
 *
 * 唯一接收凭据的接口是 `setCredentials`：服务端写 Vault 后只回 secret_ref
 * （路径 + 版本 + 键名），永不回值。前端不得在任何地方缓存凭据。
 */

import { httpDelete, httpGet, httpPost, httpPut } from './client'
import type {
  ConnectivityResult,
  DataSource,
  DataSourceFilter,
  DataSourceUpsertPayload,
  MetadataProfile,
  MetadataProfileBrief,
  Page,
  ProbeRequest,
  SchemaSnapshot,
} from '@/types'

/* ------------------------------------------------------------------ */
/* 数据源 CRUD                                                         */
/* ------------------------------------------------------------------ */

export function fetchDataSources(
  projectId: number,
  filter: DataSourceFilter,
): Promise<Page<DataSource>> {
  return httpGet<Page<DataSource>>('/datasources', {
    params: {
      project_id: projectId,
      // 数组参数用逗号连接：后端按 str 接收后自行 split，避免 axios 默认的 `type[]=` 形态
      type: filter.type?.join(','),
      env: filter.env?.join(','),
      sensitive_level: filter.sensitive_level?.join(','),
      status: filter.status,
      q: filter.q,
      page: filter.page,
      page_size: filter.page_size,
    },
  })
}

export function fetchDataSource(id: number): Promise<DataSource> {
  return httpGet<DataSource>(`/datasources/${id}`)
}

/** 创建：凭据随 body 一次性提交，服务端写 Vault + 建 secret_reference + data_source（单事务） */
export function createDataSource(
  projectId: number,
  payload: DataSourceUpsertPayload,
): Promise<DataSource> {
  return httpPost<DataSource>('/datasources', { ...payload, project_id: projectId })
}

/** 更新：payload 不含 credentials 时后端保留原 secret_ref */
export function updateDataSource(
  id: number,
  payload: Partial<DataSourceUpsertPayload>,
): Promise<DataSource> {
  return httpPut<DataSource>(`/datasources/${id}`, payload)
}

export function disableDataSource(id: number): Promise<DataSource> {
  return httpPost<DataSource>(`/datasources/${id}/disable`)
}

export function enableDataSource(id: number): Promise<DataSource> {
  return httpPost<DataSource>(`/datasources/${id}/enable`)
}

/** 删除：有关联 Pipeline/Preparation 时后端应返回 CONFLICT 并带上引用计数 */
export function deleteDataSource(id: number): Promise<null> {
  return httpDelete<null>(`/datasources/${id}`)
}

/**
 * 重新设置凭据（唯一接收凭据的接口）。
 * 必须提交全部 key_names —— 不允许部分更新，避免旧值与新值混用同一 Vault 版本。
 */
export function setCredentials(
  id: number,
  credentials: Record<string, string>,
): Promise<DataSource> {
  return httpPost<DataSource>(`/datasources/${id}/credentials`, { credentials })
}

/* ------------------------------------------------------------------ */
/* 连通性测试（P3 工具，免 Capability）                                 */
/* ------------------------------------------------------------------ */

/** 触发测试；返回 202 + state='pending'，随后轮询单测结果 */
export function startConnectivityTest(id: number): Promise<ConnectivityResult> {
  return httpPost<ConnectivityResult>(`/datasources/${id}/connectivity-tests`)
}

export function fetchConnectivityTest(id: number, testId: string): Promise<ConnectivityResult> {
  return httpGet<ConnectivityResult>(`/datasources/${id}/connectivity-tests/${testId}`)
}

/* ------------------------------------------------------------------ */
/* 元数据探查（P3 工具）                                                */
/* ------------------------------------------------------------------ */

/** 提交探查；返回 {profile_id, version}，此时 status='probing' */
export function startProbe(
  id: number,
  payload: ProbeRequest,
): Promise<{ profile_id: number; version: number }> {
  return httpPost<{ profile_id: number; version: number }>(`/datasources/${id}/probes`, payload)
}

export function fetchProfiles(id: number): Promise<MetadataProfileBrief[]> {
  return httpGet<MetadataProfileBrief[]>(`/datasources/${id}/profiles`)
}

export function fetchProfile(id: number, version: number): Promise<MetadataProfile> {
  return httpGet<MetadataProfile>(`/datasources/${id}/profiles/${version}`)
}

/** Schema 树；engine 层产出的快照，前端只读渲染 */
export function fetchSchema(
  id: number,
  version: number,
  table?: string,
): Promise<SchemaSnapshot> {
  return httpGet<SchemaSnapshot>(`/datasources/${id}/profiles/${version}/schema`, {
    params: { table },
  })
}

/** 脱敏样本：**只返回已脱敏值**，库里不存在原文，故无解敏接口 */
export function fetchSamples(
  id: number,
  version: number,
  table: string,
  limit = 50,
): Promise<{ rows: Array<Record<string, string>>; truncated: boolean }> {
  return httpGet<{ rows: Array<Record<string, string>>; truncated: boolean }>(
    `/datasources/${id}/profiles/${version}/samples`,
    { params: { table, limit } },
  )
}
