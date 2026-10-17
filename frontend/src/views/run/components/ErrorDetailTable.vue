<script setup lang="ts">
/**
 * 错误记录明细表（fe-06 §8）。
 *
 * 这条红线必须写在代码里，而不只是写在文档里：
 * **错误原文不可达**。`error_record` 只存 `payload_digest`（SHA256 + 字段长度），
 * 后端不下发原文。因此这里：
 * - 没有任何「查看原文 / 复制原文」按钮，也不接受以调试模式为理由新增；
 * - 导出的也是同一份摘要（后端生成文件，前端不拼 CSV）；
 * - 摘要仍走 `SensitiveText` 渲染——它是后端文本的统一安全出口。
 */
import { computed } from 'vue'
import { QueryTable, SectionCard, SensitiveText, StatusTag } from '@/components/common'
import { formatDateTime, formatInt } from '@/utils/format'
import type { ErrorRecordRead } from '@/types'

const props = defineProps<{
  items: ErrorRecordRead[]
  loading: boolean
  total: number
  page: number
  pageSize: number
}>()

const emit = defineEmits<{
  'page-change': [page: number]
  export: []
  reload: []
}>()

/**
 * store 层对错误明细的失败是「静默置空」的（未生成报告不算错误），
 * 于是「报告说有拒绝行、列表却一条都没有」就成为唯一可观测的失败信号。
 * 只对第 1 页判定：翻到超出范围的页码本来就可能是空的。
 */
const loadError = computed<string | null>(() => {
  if (props.page !== 1 || props.loading) {
    return null
  }
  if (props.total > 0 && props.items.length === 0) {
    return '错误明细为空但总数不为 0，可能加载失败或后端尚未生成明细，请重试。'
  }
  return null
})
</script>

<template>
  <SectionCard title="错误明细（仅受保护摘要）" :padded="true">
    <template #extra>
      <span class="err-table__hint">原文不入库，故不提供查看/下载原文</span>
      <el-button size="small" @click="emit('export')">导出摘要</el-button>
    </template>

    <QueryTable
      :items="items"
      :loading="loading"
      :error="loadError"
      :total="total"
      :page="page"
      :page-size="pageSize"
      row-key="id"
      :min-height="200"
      empty-variant="no-data"
      empty-text="没有拒绝行"
      empty-description="本次运行的错误表为空：质量分流没有拒绝任何行。"
      @page-change="(p: number) => emit('page-change', p)"
      @retry="emit('reload')"
    >
      <el-table-column label="错误码" width="170">
        <template #default="{ row }">
          <StatusTag kind="ErrorCodeLabel" :value="row.error_code" />
        </template>
      </el-table-column>
      <el-table-column label="规则 ID" min-width="120">
        <template #default="{ row }">
          <SensitiveText :value="row.rule_id" mono />
        </template>
      </el-table-column>
      <el-table-column label="字段" min-width="130">
        <template #default="{ row }">
          <SensitiveText :value="row.field_name" mono />
        </template>
      </el-table-column>
      <el-table-column label="受保护摘要" min-width="240">
        <template #default="{ row }">
          <SensitiveText :value="row.payload_digest" mono />
        </template>
      </el-table-column>
      <el-table-column label="原因摘要" min-width="220">
        <template #default="{ row }">
          <SensitiveText :value="row.reason" />
        </template>
      </el-table-column>
      <el-table-column label="发生时间" width="170">
        <template #default="{ row }">
          <span class="elt-mono">{{ formatDateTime(row.occurred_at) }}</span>
        </template>
      </el-table-column>
    </QueryTable>

    <p class="err-table__foot">
      共 <span class="elt-mono">{{ formatInt(total) }}</span> 条拒绝行摘要。
      <span class="elt-mono">payload_digest</span> 由后端对原始值做 SHA256 并截断生成，
      用于与后端日志对账；它不可逆推原文，平台亦不留存原文。
    </p>
  </SectionCard>
</template>

<style scoped>
.err-table__hint {
  font-size: 11px;
  color: var(--elt-text-muted);
}

.err-table__foot {
  margin: 10px 0 0;
  font-size: 11px;
  line-height: 1.7;
  color: var(--elt-text-muted);
}
</style>
