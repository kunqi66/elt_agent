<script setup lang="ts">
/**
 * 成员表（fe-08 §1.4）。
 *
 * 纯展示组件：数据与三态由 MembersTab 传入，操作以事件抛出。
 * 「操作」列的按钮显隐取自后端下发的 can_manage_*，不是权限判定——
 * 即便隐藏了按钮，真正的拒绝仍在服务端（fe-08 §8）。
 */
import { Delete, Key } from '@element-plus/icons-vue'
import { QueryTable, StatusTag } from '@/components/common'
import { formatDateTime } from '@/utils/format'
import type { MemberRead } from '@/types'

const props = defineProps<{
  members: MemberRead[]
  loading: boolean
  initialLoading: boolean
  error: string | null
  canManageMembers: boolean
  canManageRoles: boolean
  /** 当前登录用户 id；用于把「移除自己」的入口置灰并提示 */
  currentUserId: number | null
}>()

const emit = defineEmits<{
  retry: []
  'grant-role': [member: MemberRead]
  remove: [member: MemberRead]
}>()

function isSelf(member: MemberRead): boolean {
  return props.currentUserId !== null && member.user_id === props.currentUserId
}

/** 移除自己通常会被后端拒绝；这里只做提示，不代替服务端判断 */
const selfHint = '不能移除自己'

function handleRemove(member: MemberRead): void {
  if (isSelf(member)) {
    return
  }
  emit('remove', member)
}

/**
 * el-table 插槽的 row 静态类型是 Element Plus 的 DefaultRow，与我们的 DTO 无类型关联。
 * 直接读属性不报错（索引签名读出 any），但作为参数传给强类型函数就会失败。
 * 与其放宽那些函数的签名（会让整文件的类型检查一起退化），不如在调用点收口。
 */
function memberOf(row: unknown): MemberRead {
  return row as MemberRead
}
</script>

<template>
  <QueryTable
    :items="members"
    :loading="loading"
    :initial-loading="initialLoading"
    :error="error"
    :total="members.length"
    :show-pagination="false"
    row-key="id"
    :min-height="200"
    empty-variant="no-data"
    empty-text="还没有成员"
    empty-description="把本租户内已存在的用户加入项目后，这里会显示成员及其职责槽。"
    @retry="emit('retry')"
  >
    <el-table-column label="成员" min-width="150">
      <template #default="{ row }">
        <div class="member-cell">
          <span class="member-cell__name">{{ memberOf(row).display_name }}</span>
          <span class="member-cell__account elt-mono">@{{ memberOf(row).username }}</span>
        </div>
      </template>
    </el-table-column>

    <el-table-column label="邮箱" min-width="180">
      <template #default="{ row }">
        <span class="member-cell__email">{{ memberOf(row).email }}</span>
      </template>
    </el-table-column>

    <el-table-column label="职责槽" min-width="180">
      <template #default="{ row }">
        <div class="role-tags">
          <StatusTag
            v-for="role in memberOf(row).roles"
            :key="role"
            kind="RoleName"
            :value="role"
          />
          <span v-if="memberOf(row).roles.length === 0" class="role-tags__none">无职责槽</span>
        </div>
      </template>
    </el-table-column>

    <el-table-column label="加入时间" width="160">
      <template #default="{ row }">
        <span class="member-cell__time">{{ formatDateTime(memberOf(row).joined_at) }}</span>
      </template>
    </el-table-column>

    <el-table-column label="状态" width="90">
      <template #default="{ row }">
        <StatusTag kind="MemberStatus" :value="memberOf(row).status" />
      </template>
    </el-table-column>

    <el-table-column label="操作" width="170" fixed="right">
      <template #default="{ row }">
        <div class="member-actions">
          <el-button
            v-if="canManageRoles"
            link
            type="primary"
            size="small"
            :icon="Key"
            @click.stop="emit('grant-role', memberOf(row))"
          >
            授予职责
          </el-button>
          <el-tooltip
            :disabled="!isSelf(memberOf(row))"
            :content="selfHint"
            placement="top"
          >
            <span>
              <el-button
                v-if="canManageMembers"
                link
                type="danger"
                size="small"
                :icon="Delete"
                :disabled="isSelf(memberOf(row))"
                @click.stop="handleRemove(memberOf(row))"
              >
                移除
              </el-button>
            </span>
          </el-tooltip>
        </div>
      </template>
    </el-table-column>
  </QueryTable>
</template>

<style scoped>
.member-cell {
  display: flex;
  flex-direction: column;
  line-height: 1.3;
}

.member-cell__name {
  font-size: 13px;
  color: var(--elt-text-primary);
}

.member-cell__account,
.member-cell__email,
.member-cell__time {
  font-size: 11px;
  color: var(--elt-text-muted);
}

.role-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}

.role-tags__none {
  font-size: 11px;
  color: var(--elt-text-muted);
}

.member-actions {
  display: flex;
  gap: 8px;
}
</style>
