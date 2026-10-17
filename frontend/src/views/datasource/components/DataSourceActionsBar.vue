<script setup lang="ts">
/**
 * 数据源的启停与删除（fe-03 §6.2）。列表行与详情页共用一份实现。
 *
 * 为什么集中成组件：这两个动作都带确认文案与一个 CONFLICT 分支
 * （仍被 Pipeline 引用时不能删）。各页各写一遍，必然出现
 * 「列表页提示了受影响对象、详情页只说删除失败」这种不一致。
 *
 * 语义说明：
 * - 禁用 = 不能再用于新的方案生成与执行，**不影响**已在跑的作业；
 * - 删除 = 释放 secret_ref 与探查记录；被引用时后端返回 CONFLICT，前端只提示、不擅自解引用。
 */
import { computed } from 'vue'
import { ArrowDown, Delete, Pointer, VideoPause } from '@element-plus/icons-vue'
import { useDataSourceStore } from '@/stores/datasource'
import { useAsyncTask } from '@/composables/useAsyncTask'
import { ElMessageBox } from '@/utils/feedback'
import { isBizError } from '@/utils/errors'
import * as datasourceApi from '@/api/datasource'
import type { DataSource } from '@/types'

const props = withDefaults(
  defineProps<{
    dataSource: DataSource
    /** dropdown = 列表行内；buttons = 详情页页头 */
    variant?: 'dropdown' | 'buttons'
    disabled?: boolean
  }>(),
  { variant: 'dropdown', disabled: false },
)

const emit = defineEmits<{
  /** 状态已变更（父级刷新列表或详情） */
  changed: [row: DataSource]
  /** 已删除 */
  removed: [row: DataSource]
  /** 删除被拒（仍被引用）；父级渲染一条页面级提示 */
  conflict: [message: string]
}>()

const store = useDataSourceStore()

const isActive = computed<boolean>(() => props.dataSource.status === 'active')
const toggleText = computed<string>(() => (isActive.value ? '禁用' : '启用'))
const toggleIcon = computed(() => (isActive.value ? VideoPause : Pointer))

const toggleTask = useAsyncTask(
  async (row: DataSource) => {
    if (row.status === 'active') {
      await datasourceApi.disableDataSource(row.id)
    } else {
      await datasourceApi.enableDataSource(row.id)
    }
    await store.query.reload()
  },
  {},
)

const deleteTask = useAsyncTask(
  async (row: DataSource) => {
    try {
      await datasourceApi.deleteDataSource(row.id)
    } catch (e) {
      // CONFLICT 只在「仍被引用」时出现；把引用数量解析出来比一句「删除失败」有用得多
      if (isBizError(e) && e.code === 'CONFLICT') {
        const refs = e.details['pipeline_refs']
        const count = Array.isArray(refs) ? refs.length : null
        emit(
          'conflict',
          count === null
            ? `数据源「${row.name}」仍被 Pipeline 引用，无法删除。请先在管线列表解除引用。`
            : `数据源「${row.name}」仍被 ${count} 个 Pipeline 引用，无法删除。请先在管线列表解除引用（共 ${count} 处）。`,
        )
      }
      throw e
    }
    await store.query.reload()
  },
  { successMessage: '已删除' },
)

const busy = computed<boolean>(() => props.disabled || toggleTask.loading.value || deleteTask.loading.value)

async function onToggle(): Promise<void> {
  const row = props.dataSource
  if (isActive.value) {
    try {
      await ElMessageBox.confirm(
        `禁用后「${row.name}」不能再用于新的方案生成与执行；已在运行的作业不受影响，可随时重新启用。`,
        `禁用数据源「${row.name}」`,
        { type: 'warning', confirmButtonText: '禁用', cancelButtonText: '取消' },
      )
    } catch {
      return
    }
  }
  const ok = await toggleTask.run(row)
  if (ok) {
    emit('changed', { ...row, status: isActive.value ? 'disabled' : 'active' })
  }
}

async function onDelete(): Promise<void> {
  const row = props.dataSource
  try {
    await ElMessageBox.confirm(
      `删除后「${row.name}」的密钥引用与探查记录将不再可用；若仍被 Pipeline 引用，后端会拒绝删除。` +
        '本操作会写入证据账本。',
      `删除数据源「${row.name}」`,
      {
        type: 'warning',
        confirmButtonText: '删除',
        cancelButtonText: '取消',
        confirmButtonClass: 'el-button--danger',
      },
    )
  } catch {
    return
  }
  const ok = await deleteTask.run(row)
  if (ok) {
    emit('removed', row)
  }
}

function onCommand(command: 'toggle' | 'delete'): void {
  if (command === 'toggle') {
    void onToggle()
  } else {
    void onDelete()
  }
}
</script>

<template>
  <div class="ds-actions">
    <el-dropdown v-if="variant === 'dropdown'" trigger="click" @command="onCommand">
      <el-button link :disabled="busy">
        更多<el-icon class="ds-actions__caret"><ArrowDown /></el-icon>
      </el-button>
      <template #dropdown>
        <el-dropdown-menu>
          <el-dropdown-item command="toggle" :icon="toggleIcon">{{ toggleText }}</el-dropdown-item>
          <el-dropdown-item command="delete" :icon="Delete" divided>删除</el-dropdown-item>
        </el-dropdown-menu>
      </template>
    </el-dropdown>

    <template v-else>
      <el-button :icon="toggleIcon" :loading="toggleTask.loading.value" :disabled="busy" @click="onToggle">
        {{ toggleText }}
      </el-button>
      <el-button
        type="danger"
        plain
        :icon="Delete"
        :loading="deleteTask.loading.value"
        :disabled="busy"
        @click="onDelete"
      >删除</el-button>
    </template>
  </div>
</template>

<style scoped>
.ds-actions {
  display: inline-flex;
  gap: 8px;
  align-items: center;
}

.ds-actions__caret {
  margin-left: 2px;
}
</style>
