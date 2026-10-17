<script setup lang="ts">
/**
 * 回滚方案全文（fe-06 §5 / §8）。
 *
 * 为什么要把「方案」单独做一块而不是一句提示：
 * 回滚是操作员对生产数据面下的指令，他必须**看到自己要执行的东西**——
 * 有几步、每步走哪个 Harness 工具、清理哪些影子数据、恢复到什么范围。
 * 只在弹窗里写「确定要回滚吗」等于让他盲签。
 *
 * 步骤为空时**不伪造方案**：明确写出「后端未提供步骤」并提示先与运维确认，
 * 而不是渲染一个空表格让人以为「没有步骤 = 很安全」。
 */
import { computed } from 'vue'
import { SensitiveText } from '@/components/common'
import type { RollbackPlanLite } from '@/types'

const props = defineProps<{ plan: RollbackPlanLite }>()

const steps = computed(() => [...props.plan.steps].sort((a, b) => a.order - b.order))
</script>

<template>
  <div class="rb-plan">
    <el-alert
      v-if="steps.length === 0"
      type="warning"
      :closable="false"
      show-icon
      title="后端未提供回滚步骤"
      description="rollback_plan.steps 为空：执行前请与运维确认清理范围，不要凭本页信息直接操作。"
    />

    <el-table v-else :data="steps" size="small">
      <el-table-column prop="order" label="#" width="52">
        <template #default="{ row }">
          <span class="elt-mono">{{ row.order }}</span>
        </template>
      </el-table-column>
      <el-table-column label="动作" min-width="160">
        <template #default="{ row }">
          <SensitiveText :value="row.action" />
        </template>
      </el-table-column>
      <el-table-column label="Harness 工具" min-width="180">
        <template #default="{ row }">
          <SensitiveText :value="row.tool" mono />
        </template>
      </el-table-column>
      <el-table-column label="说明" min-width="220">
        <template #default="{ row }">
          <SensitiveText :value="row.description" multiline />
        </template>
      </el-table-column>
    </el-table>

    <div class="rb-plan__meta">
      <div class="rb-plan__meta-item">
        <span class="rb-plan__meta-label">影子数据清理</span>
        <span class="rb-plan__meta-value">{{ plan.shadow_cleanup ? '是（清理未发布影子数据）' : '否（保留影子表）' }}</span>
      </div>
      <div class="rb-plan__meta-item">
        <span class="rb-plan__meta-label">恢复范围</span>
        <SensitiveText :value="plan.restore_scope" />
      </div>
    </div>
  </div>
</template>

<style scoped>
.rb-plan {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.rb-plan__meta {
  display: flex;
  flex-wrap: wrap;
  gap: 18px;
  font-size: 12px;
}

.rb-plan__meta-item {
  display: flex;
  gap: 6px;
  align-items: baseline;
}

.rb-plan__meta-label {
  color: var(--elt-text-muted);
}

.rb-plan__meta-value {
  color: var(--elt-text-primary);
}
</style>
