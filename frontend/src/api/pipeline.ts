/**
 * Pipeline 生成、门禁、版本冻结（fe-04 / be-05 be-06 be-07）。
 *
 * 分工铁律：门禁判定、指纹计算、HOCON 渲染全部在 backend/engines/ 里完成。
 * 前端只负责把后端结论渲染出来，**不做任何规则判断**——
 * 前端若自己算一遍，就会出现「界面说通过、后端说阻断」的分裂。
 */

import { httpGet, httpPost } from './client'
import type {
  AutofixRound,
  EtlPlan,
  FreezeArtifactManifest,
  GateResult,
  HoconArtifact,
  Page,
  Pipeline,
  PipelineFilter,
  PipelineVersionDetail,
  PipelineVersionSummary,
  StartGenerationPayload,
  WorkflowSnapshot,
} from '@/types'

/* ------------------------------------------------------------------ */
/* Pipeline 与 Studio 会话                                             */
/* ------------------------------------------------------------------ */

export function fetchPipelines(
  projectId: number,
  filter: PipelineFilter,
): Promise<Page<Pipeline>> {
  return httpGet<Page<Pipeline>>('/pipelines', {
    params: {
      project_id: projectId,
      status: filter.status?.join(','),
      q: filter.q,
      page: filter.page,
      page_size: filter.page_size,
    },
  })
}

export function fetchPipeline(id: number): Promise<Pipeline> {
  return httpGet<Pipeline>(`/pipelines/${id}`)
}

/**
 * 发起生成（fe-04 §6.1）。
 * 后端写 LangGraph checkpoint 后返回 thread_id；响应体可能已带 interrupt
 * （澄清问题），前端不得假定一定有 next 步骤。
 */
export function startGeneration(
  projectId: number,
  payload: StartGenerationPayload,
): Promise<WorkflowSnapshot> {
  return httpPost<WorkflowSnapshot>('/pipelines/generations', {
    ...payload,
    project_id: projectId,
  })
}

/** 澄清回答：写回 checkpoint 并继续图执行 */
export function answerClarification(
  threadId: string,
  answers: Record<string, string>,
): Promise<WorkflowSnapshot> {
  return httpPost<WorkflowSnapshot>(`/pipelines/generations/${threadId}/answers`, { answers })
}

/** 当前工作流快照：**进断线后可据此恢复界面**，是 Studio 的单一状态来源 */
export function fetchWorkflowSnapshot(threadId: string): Promise<WorkflowSnapshot> {
  return httpGet<WorkflowSnapshot>(`/pipelines/generations/${threadId}`)
}

/** 主动中断生成（用户点「取消」）；不是硬中断，不产生证据事件 */
export function abortGeneration(threadId: string, reason: string): Promise<WorkflowSnapshot> {
  return httpPost<WorkflowSnapshot>(`/pipelines/generations/${threadId}/abort`, { reason })
}

/* ------------------------------------------------------------------ */
/* 制品：EtlPlan / HOCON / DAG                                          */
/* ------------------------------------------------------------------ */

/**
 * 只读制品。pipeline_version 不可变，故没有写接口（CLAUDE.md §5.4）。
 * 返回的是完整的 EtlPlan（含 valid / validation_errors / plan 体），
 * DAG 在 `plan.plan.dag` 里，不是一个独立接口。
 */
export function fetchPlan(versionId: number): Promise<EtlPlan> {
  return httpGet<EtlPlan>(`/pipeline-versions/${versionId}/plan`)
}

export function fetchHocon(versionId: number): Promise<HoconArtifact> {
  return httpGet<HoconArtifact>(`/pipeline-versions/${versionId}/hocon`)
}

/* ------------------------------------------------------------------ */
/* 门禁与自动修复                                                      */
/* ------------------------------------------------------------------ */

/** 门禁结论（后端 engines/gate 产出，前端只渲染） */
export function fetchGateResult(versionId: number): Promise<GateResult> {
  return httpGet<GateResult>(`/pipeline-versions/${versionId}/gate`)
}

/** 手动触发一轮自动修复；已达上限时后端返回 AUTOFIX_EXHAUSTED */
export function runAutofix(versionId: number, note: string): Promise<AutofixRound> {
  return httpPost<AutofixRound>(`/pipeline-versions/${versionId}/autofix`, { note })
}

/* ------------------------------------------------------------------ */
/* 版本冻结与 Diff                                                     */
/* ------------------------------------------------------------------ */

/**
 * 冻结版本：仅当门禁无 P0/P1 阻断时允许。
 * 成功后进入不可变状态，任何修正都只能产生新版本。
 */
export function freezeVersion(
  versionId: number,
  note: string,
): Promise<FreezeArtifactManifest> {
  return httpPost<FreezeArtifactManifest>(`/pipeline-versions/${versionId}/freeze`, { note })
}

export function fetchVersions(
  pipelineId: number,
  page = 1,
  pageSize = 20,
): Promise<Page<PipelineVersionSummary>> {
  return httpGet<Page<PipelineVersionSummary>>(`/pipelines/${pipelineId}/versions`, {
    params: { page, page_size: pageSize },
  })
}

export function fetchVersionDetail(versionId: number): Promise<PipelineVersionDetail> {
  return httpGet<PipelineVersionDetail>(`/pipeline-versions/${versionId}`)
}

/**
 * 版本 Diff。
 * 方向固定为 base → target（base 是旧、target 是新），由调用方显式传入，
 * 不依赖后端默认值——审计场景里方向读反会得出完全相反的结论。
 */
export function fetchVersionDiff(
  baseVersionId: number,
  targetVersionId: number,
): Promise<{ entries: Array<{ path: string; kind: 'added' | 'removed' | 'changed'; before?: string; after?: string }> }> {
  return httpGet<{
    entries: Array<{ path: string; kind: 'added' | 'removed' | 'changed'; before?: string; after?: string }>
  }>('/pipeline-versions/diff', {
    params: { base_version_id: baseVersionId, target_version_id: targetVersionId },
  })
}
