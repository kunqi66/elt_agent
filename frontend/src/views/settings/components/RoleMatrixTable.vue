<script setup lang="ts">
/**
 * 角色能力矩阵（fe-08 §6.3）。
 *
 * ⚠️ 这里的取值是**手写文案**，会随后端规则漂移（fe-08 §10）。
 * 因此 fe-08 §9 要求一条「逐格比对」的单测作为唯一防线；
 * 在后端下发能力矩阵之前，改动本表必须同步核对后端 `services/`。
 *
 * 表头六列与 §6.3 逐字一致：发起 Prepare / 审数据槽 / 审安全槽 / Commit / 管策略评测 / 查看账本。
 */
import { CircleCheck, CircleClose } from '@element-plus/icons-vue'
import { StatusTag } from '@/components/common'
import type { RoleMatrixRow, RoleName } from '@/types'

const props = withDefaults(defineProps<{ highlightRole?: RoleName | null }>(), {
  highlightRole: null,
})

const rows: RoleMatrixRow[] = [
  { role: 'maker', prepare: true, approve_data: false, approve_security: false, commit: false, manage_policy: false, view_ledger: false },
  { role: 'checker_data', prepare: false, approve_data: true, approve_security: false, commit: false, manage_policy: false, view_ledger: false },
  { role: 'checker_security', prepare: false, approve_data: false, approve_security: true, commit: false, manage_policy: true, view_ledger: false },
  { role: 'operator', prepare: false, approve_data: false, approve_security: false, commit: true, manage_policy: false, view_ledger: false },
  { role: 'auditor', prepare: false, approve_data: false, approve_security: false, commit: false, manage_policy: false, view_ledger: true },
]

/** 行高亮：授予弹窗选中某角色时，把对应行标出来便于对照 */
function rowClassName({ row }: { row: RoleMatrixRow }): string {
  return props.highlightRole !== null && props.highlightRole === row.role
    ? 'role-matrix__row--highlight'
    : ''
}

/**
 * el-table 插槽的 row 静态类型是 Element Plus 的 DefaultRow，与我们的 DTO 无类型关联。
 * 直接读属性不报错（索引签名读出 any），但作为参数传给强类型函数就会失败。
 * 与其放宽那些函数的签名（会让整文件的类型检查一起退化），不如在调用点收口。
 */
function roleOf(row: unknown): RoleMatrixRow {
  return row as RoleMatrixRow
}
</script>

<template>
  <el-table
    :data="rows"
    size="small"
    :row-class-name="rowClassName"
    class="role-matrix"
  >
    <el-table-column label="职责槽" width="140">
      <template #default="{ row }">
        <StatusTag kind="RoleName" :value="roleOf(row).role" />
      </template>
    </el-table-column>

    <el-table-column label="发起 Prepare" align="center">
      <template #default="{ row }">
        <el-icon v-if="roleOf(row).prepare" class="role-matrix__yes"><CircleCheck /></el-icon>
        <el-icon v-else class="role-matrix__no"><CircleClose /></el-icon>
      </template>
    </el-table-column>

    <el-table-column label="审数据槽" align="center">
      <template #default="{ row }">
        <el-icon v-if="roleOf(row).approve_data" class="role-matrix__yes"><CircleCheck /></el-icon>
        <el-icon v-else class="role-matrix__no"><CircleClose /></el-icon>
      </template>
    </el-table-column>

    <el-table-column label="审安全槽" align="center">
      <template #default="{ row }">
        <el-icon v-if="roleOf(row).approve_security" class="role-matrix__yes"><CircleCheck /></el-icon>
        <el-icon v-else class="role-matrix__no"><CircleClose /></el-icon>
      </template>
    </el-table-column>

    <el-table-column label="Commit" align="center">
      <template #default="{ row }">
        <el-icon v-if="roleOf(row).commit" class="role-matrix__yes"><CircleCheck /></el-icon>
        <el-icon v-else class="role-matrix__no"><CircleClose /></el-icon>
      </template>
    </el-table-column>

    <el-table-column label="管策略/评测" align="center">
      <template #default="{ row }">
        <el-icon v-if="roleOf(row).manage_policy" class="role-matrix__yes"><CircleCheck /></el-icon>
        <el-icon v-else class="role-matrix__no"><CircleClose /></el-icon>
      </template>
    </el-table-column>

    <el-table-column label="查看账本" align="center">
      <template #default="{ row }">
        <el-icon v-if="roleOf(row).view_ledger" class="role-matrix__yes"><CircleCheck /></el-icon>
        <el-icon v-else class="role-matrix__no"><CircleClose /></el-icon>
      </template>
    </el-table-column>
  </el-table>
</template>

<style scoped>
.role-matrix__yes {
  color: var(--elt-success);
}

.role-matrix__no {
  color: var(--elt-text-muted);
  opacity: 0.5;
}

.role-matrix :deep(.role-matrix__row--highlight) {
  background-color: var(--elt-accent-soft);
}
</style>
