<script setup lang="ts">
/**
 * 根因诊断面板（fe-06 §6）。
 *
 * 三类信息必须能分开读，混在一起就是一个「看起来很确定」的错结论：
 * - `deterministic`：来自 `quality_report.diagnostics`，永远可用，是结论的底座；
 * - `llm_assisted`：模型解读，只做补充说明，**不覆盖任何数值**；
 * - `degraded=true`：模型不可用，面板顶部显式降级提示，但 evidence 与 suggestions
 *   **照常完整渲染**——空白面板会让人以为「没有问题」，那是最坏的结果。
 *
 * 证据与建议里的字符串一律走 SensitiveText：诊断结论是典型的「可能带业务原文」的通道。
 */
import { computed } from 'vue'
import { SectionCard, SensitiveText } from '@/components/common'
import type { Diagnosis } from '@/types'

const props = defineProps<{
  diagnosis: Diagnosis | null
  loading: boolean
  /** 由父级传入（通常取 run.status 的降级信号）；与 diagnosis.degraded 取或 */
  degraded: boolean
  /** 拉取失败文案：诊断不可用不能影响指标与质量面板 */
  error?: string | null
}>()

const emit = defineEmits<{
  retry: []
  'goto-subject': [payload: { subjectType: string; subjectId: string }]
  'goto-route': [routeName: string]
}>()

const isDegraded = computed<boolean>(() => props.degraded || props.diagnosis?.degraded === true)

const confidenceText = computed<string>(() => {
  const confidence = props.diagnosis?.confidence
  if (confidence === 'llm_assisted') {
    return '模型辅助解读（不覆盖确定性数值）'
  }
  return '确定性结论（来自质量规则，始终可用）'
})

const confidenceTag = computed<'danger' | 'info'>(() =>
  props.diagnosis?.confidence === 'llm_assisted' ? 'info' : 'danger',
)
</script>

<template>
  <SectionCard title="根因诊断" :loading="loading && diagnosis === null" :skeleton-height="200">
    <template #extra>
      <el-button size="small" :loading="loading" @click="emit('retry')">重新诊断</el-button>
    </template>

    <!-- 降级提示放在最上面：用户应当先知道「这份结论可能不完整」，再读结论 -->
    <el-alert
      v-if="isDegraded"
      class="diag__degraded"
      type="warning"
      :closable="false"
      show-icon
      title="诊断服务降级，结论可能不完整"
      description="模型解读不可用，以下仅展示确定性结论（证据与建议仍是完整的后端输出）。"
    />

    <el-alert
      v-if="error !== null && error !== undefined && error !== ''"
      class="diag__degraded"
      type="error"
      :closable="false"
      show-icon
      :title="error"
    />

    <div v-if="diagnosis === null && !loading" class="diag__empty">
      <p>暂无诊断结果。</p>
      <p class="diag__empty-desc">
        诊断在运行进入终态后生成；若刚触发硬中断，可点右上角「重新诊断」主动请求一次。
      </p>
      <el-button size="small" :loading="loading" @click="emit('retry')">请求诊断</el-button>
    </div>

    <template v-else-if="diagnosis !== null">
      <div class="diag__head">
        <el-tag :type="confidenceTag" size="small" disable-transitions>{{ confidenceText }}</el-tag>
        <span v-if="isDegraded" class="diag__badge">degraded</span>
      </div>

      <p class="diag__conclusion">
        <SensitiveText :value="diagnosis.conclusion" multiline />
      </p>

      <h3 class="diag__subtitle">证据</h3>
      <el-table :data="diagnosis.evidence" size="small">
        <el-table-column label="信号" min-width="150">
          <template #default="{ row }">
            <SensitiveText :value="row.signal" mono />
          </template>
        </el-table-column>
        <el-table-column label="取值" min-width="140">
          <template #default="{ row }">
            <SensitiveText :value="row.value" mono />
          </template>
        </el-table-column>
        <el-table-column label="来源表" min-width="150">
          <template #default="{ row }">
            <SensitiveText :value="row.source_table" mono />
          </template>
        </el-table-column>
        <el-table-column label="来源 ID" min-width="120">
          <template #default="{ row }">
            <SensitiveText :value="row.source_id" mono />
          </template>
        </el-table-column>
        <el-table-column label="" width="80" align="right">
          <template #default="{ row }">
            <el-button
              link
              size="small"
              @click="emit('goto-subject', { subjectType: row.source_table, subjectId: row.source_id })"
            >
              定位
            </el-button>
          </template>
        </el-table-column>
      </el-table>

      <h3 class="diag__subtitle">建议动作</h3>
      <ul v-if="diagnosis.suggestions.length > 0" class="diag__suggestions">
        <li v-for="(item, index) in diagnosis.suggestions" :key="index" class="diag__suggestion">
          <SensitiveText :value="item.action" multiline />
          <el-button
            v-if="item.target_route !== null && item.target_route !== ''"
            link
            size="small"
            @click="emit('goto-route', String(item.target_route))"
          >
            前往处理
          </el-button>
          <span v-else class="diag__no-route">无可跳转页面</span>
        </li>
      </ul>
      <p v-else class="diag__empty-desc">后端未给出可执行建议，请结合证据表人工判断。</p>

      <p class="diag__foot">
        诊断只解释「可能的原因」，不改变任何数值：指标、质量与运行状态一律以对应面板的原始返回为准。
      </p>
    </template>
  </SectionCard>
</template>

<style scoped>
.diag__degraded {
  margin-bottom: 12px;
}

.diag__head {
  display: flex;
  gap: 8px;
  align-items: center;
}

.diag__badge {
  padding: 1px 6px;
  font-size: 11px;
  color: var(--elt-warn);
  background-color: var(--elt-bg-elevated);
  border-radius: var(--elt-radius-pill);
}

.diag__conclusion {
  margin: 10px 0 0;
  font-size: 13px;
  line-height: 1.8;
  color: var(--elt-text-primary);
}

.diag__subtitle {
  margin: 16px 0 8px;
  font-size: 13px;
  font-weight: 600;
  color: var(--elt-text-primary);
}

.diag__suggestions {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 0;
  margin: 0;
  list-style: none;
}

.diag__suggestion {
  display: flex;
  gap: 8px;
  align-items: center;
  font-size: 12px;
  color: var(--elt-text-secondary);
}

.diag__no-route {
  font-size: 11px;
  color: var(--elt-text-muted);
}

.diag__empty {
  display: flex;
  flex-direction: column;
  gap: 6px;
  align-items: flex-start;
  font-size: 12px;
  color: var(--elt-text-secondary);
}

.diag__empty-desc {
  margin: 0;
  font-size: 12px;
  color: var(--elt-text-muted);
}

.diag__foot {
  margin: 14px 0 0;
  font-size: 11px;
  line-height: 1.7;
  color: var(--elt-text-muted);
}
</style>
