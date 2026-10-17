/**
 * 准备单、审批与 Commit（fe-05 / be-08 be-09 be-10）。
 *
 * 三条不可越过的边界：
 * 1. **审批人不得等于发起人**——前端置灰只是体验，真正的拦截在后端 + DB 约束；
 *    因此前端必须如实展示后端返回的 SELF_APPROVAL_FORBIDDEN，不得自行「猜」能否审批。
 * 2. **Capability 不可见**——Commit 由后端在服务端换取 Capability，
 *    命令响应里绝不返回 capability_id/token，前端也不请求它。
 * 3. **Decision 不可撤销**——approval_decision 只允许 INSERT，
 *    因此没有「取消审批」接口，改主意只能重新走准备单。
 */

import { httpGet, httpPost } from './client'
import type { AxiosRequestConfig } from 'axios'
import type {
  ApprovalDecisionRead,
  ApprovalDecisionRequest,
  CommitResult,
  CreatePreparationRequest,
  Page,
  PreparationDetail,
  PreparationFilter,
  PreparationSummary,
  PreparationTimelineItem,
} from '@/types'

/* ------------------------------------------------------------------ */
/* 列表与详情                                                          */
/* ------------------------------------------------------------------ */

export function fetchPreparations(
  projectId: number,
  filter: PreparationFilter,
): Promise<Page<PreparationSummary>> {
  return httpGet<Page<PreparationSummary>>('/preparations', {
    params: {
      project_id: projectId,
      // scope 决定「待我审批 / 我发起的 / 已处理 / 全部」，是服务端过滤条件，
      // 不在前端按 created_by 二次裁剪——否则分页 total 会与实际条数对不上。
      scope: filter.scope,
      status: filter.status,
      risk_level: filter.risk_level,
      environment: filter.environment,
      slot: filter.slot,
      pipeline_id: filter.pipeline_id,
      keyword: filter.keyword,
      page: filter.page,
      page_size: filter.page_size,
    },
  })
}

export function fetchPreparation(id: number): Promise<PreparationDetail> {
  return httpGet<PreparationDetail>(`/preparations/${id}`)
}

/** 时间线（谁在何时做了什么），与证据账本同源 */
export function fetchPreparationTimeline(id: number): Promise<PreparationTimelineItem[]> {
  return httpGet<PreparationTimelineItem[]>(`/preparations/${id}/timeline`)
}

/* ------------------------------------------------------------------ */
/* 创建与审批                                                          */
/* ------------------------------------------------------------------ */

/**
 * 由已冻结版本创建准备单（Be-08 Prepare 阶段）。
 * 服务端在此计算制品/输入指纹；前端**不得**提交任何指纹字段——
 * 指纹必须由服务端从不可变版本重新计算，否则等于让调用方自证清白。
 */
export function createPreparation(
  projectId: number,
  payload: CreatePreparationRequest,
): Promise<PreparationDetail> {
  return httpPost<PreparationDetail>('/preparations', {
    ...payload,
    project_id: projectId,
  })
}

/**
 * 提交审批决定。
 * 四眼校验在服务端 + DB 约束兜底：自批、职责互斥都会在这里被拒。
 */
export function submitDecision(
  preparationId: number,
  payload: ApprovalDecisionRequest,
  config?: AxiosRequestConfig,
): Promise<ApprovalDecisionRead> {
  return httpPost<ApprovalDecisionRead>(`/preparations/${preparationId}/decisions`, payload, config)
}

/**
 * Commit（Be-10）：审批齐备后由操作员触发。
 * 返回 run_id 与 queued 状态；**不同步返回执行结果**——执行在 Celery，
 * 前端拿到 run_id 后跳运行中心。
 *
 * `config` 用于透传 `useIdempotencyKey()` 的 per-intent 幂等键。
 * 这里**必须**允许透传，而不是只靠 client.ts 的按请求自动注入：
 * Commit 会消费一次性 Capability 并创建执行，超时后用户重试若拿到新 key，
 * 服务端会当成第二次独立意图——这是全站唯一「重复一次就真的执行两次」的动作。
 */
export function commitPreparation(
  preparationId: number,
  config?: AxiosRequestConfig,
): Promise<CommitResult> {
  return httpPost<CommitResult>(`/preparations/${preparationId}/commit`, undefined, config)
}

/* ------------------------------------------------------------------ */
/* 只读附件                                                            */
/* ------------------------------------------------------------------ */

/** 风险评级理由（PDP 规则表命中项，确定性结论，非模型输出） */
export function fetchRiskReasons(
  preparationId: number,
): Promise<Array<{ rule_code: string; rule_name: string; weight: number; detail: string }>> {
  return httpGet<Array<{ rule_code: string; rule_name: string; weight: number; detail: string }>>(
    `/preparations/${preparationId}/risk-reasons`,
  )
}
