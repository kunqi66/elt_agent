/**
 * 安全治理：策略、预算、Benchmark、证据账本（fe-07 / be-14 … be-20）。
 *
 * 三条只读纪律：
 * 1. **证据账本 append-only**——只有查询与导出，没有任何写/改/删接口。
 *    这是架构约束，不是「后端还没做」。
 * 2. **策略发布是四眼流程**：草稿 → 提交审批 → 生效，前端不得直接改生效版本。
 * 3. **预算硬上限不可从前端突破**：界面里的 hard_limits 是只读展示项，
 *    用于告诉用户「改到多少会被拒」，不是输入上限。
 */

import { httpDelete, httpGet, httpPost, httpPut } from './client'
import type {
  BenchmarkCaseResult,
  BenchmarkRunRead,
  BenchmarkSuiteRead,
  BudgetDefaults,
  BudgetDefaultsRead,
  EvidencePage,
  EvidenceFilter,
  LedgerVerifyResult,
  Page,
  PolicyDiffEntry,
  PolicyRuleFilter,
  PolicyRuleRead,
  PolicyRuleWrite,
  PolicyVersionRead,
} from '@/types'

/* ------------------------------------------------------------------ */
/* 策略规则（草稿态）                                                   */
/* ------------------------------------------------------------------ */

export function fetchPolicyRules(
  projectId: number,
  filter: PolicyRuleFilter,
): Promise<Page<PolicyRuleRead>> {
  return httpGet<Page<PolicyRuleRead>>('/governance/policy-rules', {
    params: {
      project_id: projectId,
      stage: filter.stage,
      risk_level: filter.risk_level?.join(','),
      enabled: filter.enabled,
      q: filter.q,
      page: filter.page,
      page_size: filter.page_size,
    },
  })
}

/** 新建草稿。生效版本不可直接编辑——编辑的是「下一版草稿」 */
export function createPolicyRule(
  projectId: number,
  payload: PolicyRuleWrite,
): Promise<PolicyRuleRead> {
  return httpPost<PolicyRuleRead>('/governance/policy-rules', {
    ...payload,
    project_id: projectId,
  })
}

export function updatePolicyRule(
  ruleId: number,
  payload: Partial<PolicyRuleWrite>,
): Promise<PolicyRuleRead> {
  return httpPut<PolicyRuleRead>(`/governance/policy-rules/${ruleId}`, payload)
}

export function deletePolicyRule(ruleId: number): Promise<null> {
  return httpDelete<null>(`/governance/policy-rules/${ruleId}`)
}

/** 启用/停用：停用不删除，历史版本仍需可解释 */
export function togglePolicyRule(ruleId: number, enabled: boolean): Promise<PolicyRuleRead> {
  return httpPost<PolicyRuleRead>(`/governance/policy-rules/${ruleId}/toggle`, { enabled })
}

/* ------------------------------------------------------------------ */
/* 策略版本（发布流程）                                                 */
/* ------------------------------------------------------------------ */

export function fetchPolicyVersions(
  projectId: number,
  page = 1,
  pageSize = 20,
): Promise<Page<PolicyVersionRead>> {
  return httpGet<Page<PolicyVersionRead>>('/governance/policy-versions', {
    params: { project_id: projectId, page, page_size: pageSize },
  })
}

/** 相对当前生效版本的结构化 Diff；发布前的最后一道人工确认 */
export function fetchPolicyDiff(
  projectId: number,
  versionId: number,
): Promise<{ entries: PolicyDiffEntry[]; summary: { added: number; removed: number; changed: number } }> {
  return httpGet<{
    entries: PolicyDiffEntry[]
    summary: { added: number; removed: number; changed: number }
  }>('/governance/policy-versions/diff', {
    params: { project_id: projectId, version_id: versionId },
  })
}

/** 提交发布审批；不是直接生效 */
export function submitPolicyVersion(
  projectId: number,
  note: string,
): Promise<PolicyVersionRead> {
  return httpPost<PolicyVersionRead>('/governance/policy-versions', {
    project_id: projectId,
    note,
  })
}

/* ------------------------------------------------------------------ */
/* 运行预算默认值                                                       */
/* ------------------------------------------------------------------ */

export function fetchBudgetDefaults(projectId: number): Promise<BudgetDefaultsRead> {
  return httpGet<BudgetDefaultsRead>('/governance/budget-defaults', {
    params: { project_id: projectId },
  })
}

/**
 * 更新预算默认值。
 * 后端会逐项与 hard_limits 比对，越界返回 VALIDATION_ERROR；
 * 前端不做上限校验（那会与后端硬上限漂移）。
 */
export function updateBudgetDefaults(
  projectId: number,
  payload: BudgetDefaults,
): Promise<BudgetDefaultsRead> {
  return httpPut<BudgetDefaultsRead>('/governance/budget-defaults', {
    project_id: projectId,
    ...payload,
  })
}

/* ------------------------------------------------------------------ */
/* Benchmark（L0 / L1 / L2）                                            */
/* ------------------------------------------------------------------ */

export function fetchBenchmarkSuites(projectId: number): Promise<BenchmarkSuiteRead[]> {
  return httpGet<BenchmarkSuiteRead[]>('/governance/benchmark/suites', {
    params: { project_id: projectId },
  })
}

export function fetchBenchmarkRuns(
  projectId: number,
  layer?: string,
  page = 1,
  pageSize = 20,
): Promise<Page<BenchmarkRunRead>> {
  return httpGet<Page<BenchmarkRunRead>>('/governance/benchmark/runs', {
    params: { project_id: projectId, layer, page, page_size: pageSize },
  })
}

export function fetchBenchmarkRun(runId: number): Promise<BenchmarkRunRead> {
  return httpGet<BenchmarkRunRead>(`/governance/benchmark/runs/${runId}`)
}

export function fetchBenchmarkCaseResults(runId: number): Promise<BenchmarkCaseResult[]> {
  return httpGet<BenchmarkCaseResult[]>(`/governance/benchmark/runs/${runId}/cases`)
}

/**
 * 触发评测。
 * L2 打的是真实链路，后端会要求二次确认与显式预算；前端只传 layer。
 */
export function startBenchmarkRun(
  projectId: number,
  suiteId: number,
  layer: string,
): Promise<BenchmarkRunRead> {
  return httpPost<BenchmarkRunRead>('/governance/benchmark/runs', {
    project_id: projectId,
    suite_id: suiteId,
    layer,
  })
}

/* ------------------------------------------------------------------ */
/* 证据账本（只读 + 验链）                                              */
/* ------------------------------------------------------------------ */

/**
 * 账本查询，**游标分页**而非 offset。
 * 账本持续 append，offset 分页会漏读或重读；游标基于 event_seq 才稳定。
 */
export function fetchEvidence(filter: EvidenceFilter): Promise<EvidencePage> {
  return httpGet<EvidencePage>('/governance/evidence', {
    params: {
      project_id: filter.project_id,
      event_type: filter.event_type?.join(','),
      subject_id: filter.subject_id,
      pipeline_version_id: filter.pipeline_version_id,
      execution_run_id: filter.execution_run_id,
      from: filter.from,
      to: filter.to,
      cursor: filter.cursor,
      limit: filter.limit,
    },
  })
}

/**
 * 验链：后端从创世块重算 prev_hash/event_hash 直至表尾。
 * 这是独立性检查——前端不参与哈希计算，也就无从「帮忙通过」。
 */
export function verifyLedger(
  projectId: number,
  range?: { from_seq?: number; to_seq?: number },
): Promise<LedgerVerifyResult> {
  return httpPost<LedgerVerifyResult>('/governance/evidence/verify', {
    project_id: projectId,
    ...range,
  })
}
