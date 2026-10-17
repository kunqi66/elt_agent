<script setup lang="ts">
/**
 * 「成员」标签页（fe-08 §1.4 / §5）。
 *
 * 本组件持有成员列表的三态与三个写操作（添加 / 移除 / 授予职责）。
 * 写操作走 `@/api/project` + 手动 loading/error，是为了按 `BizError.code`
 * 分支到 §7 的拒绝语义（如 CONFLICT →「该用户已是项目成员」）——
 * 这些文案必须按 code 走，不能按 message 文本判断（fe-08 §7）。
 */
import { computed, onMounted, ref, watch } from 'vue'
import { Plus, Refresh } from '@element-plus/icons-vue'
import { ElMessage } from '@/utils/feedback'
import { isBizError, toDisplayMessage } from '@/utils/errors'
import * as projectApi from '@/api/project'
import { useAuthStore } from '@/stores/auth'
import { SectionCard } from '@/components/common'
import { formatInt } from '@/utils/format'
import MemberTable from './MemberTable.vue'
import InviteMemberDialog from './InviteMemberDialog.vue'
import SensitiveActionDialog from './SensitiveActionDialog.vue'
import RoleGrantDialog from './RoleGrantDialog.vue'
import type { MemberRead, ProjectRead, RoleGrantRequest } from '@/types'

const props = defineProps<{ project: ProjectRead }>()

const authStore = useAuthStore()

const members = ref<MemberRead[]>([])
const loading = ref(false)
const initialLoading = ref(true)
const error = ref<string | null>(null)

const currentUserId = computed<number | null>(
  () => authStore.profile?.id ?? authStore.user?.id ?? null,
)

/** 把写操作的失败按 fe-08 §7 的语义映射成可展示文案 */
function writeMessage(e: unknown): string {
  if (isBizError(e)) {
    switch (e.code) {
      case 'CONFLICT':
        return '该用户已是项目成员，或该请求与已有数据冲突。'
      case 'DUTY_CONFLICT':
        return '该授予会触发职责互斥（同一项目内 maker 与 checker_* 互斥），服务端已拒绝。'
      case 'FORBIDDEN':
      case 'ROLE_REQUIRED':
        return '当前职责槽无权管理成员，请联系具备管理能力的人。'
      case 'NOT_FOUND':
        return '用户或项目不存在，可能已被移除。'
      case 'TENANT_MISMATCH':
        return '跨租户访问被拒绝。'
      case 'IDEMPOTENCY_CONFLICT':
        return '请求与已有操作冲突，请刷新后重试。'
      default:
        return toDisplayMessage(e)
    }
  }
  return toDisplayMessage(e)
}

async function load(): Promise<void> {
  loading.value = true
  error.value = null
  try {
    members.value = await projectApi.fetchMembers(props.project.id)
  } catch (e) {
    // 失败绝不退化成空态：会让用户以为「项目里没人」（CONVENTIONS §1）
    error.value = toDisplayMessage(e)
  } finally {
    loading.value = false
    initialLoading.value = false
  }
}

onMounted(() => {
  void load()
})

// 切换项目要重新拉：A 项目的成员不能停在 B 项目的界面上
watch(
  () => props.project.id,
  () => {
    members.value = []
    initialLoading.value = true
    void load()
  },
)

/* ---------------- 添加成员 ---------------- */
const inviteOpen = ref(false)
const inviteSubmitting = ref(false)
const inviteError = ref<string | null>(null)

function openInvite(): void {
  inviteError.value = null
  inviteOpen.value = true
}

async function submitInvite(payload: { user_id: number; reason: string }): Promise<void> {
  inviteSubmitting.value = true
  inviteError.value = null
  try {
    await projectApi.addMember(props.project.id, payload)
    ElMessage.success('成员已添加（已写入证据账本）')
    inviteOpen.value = false
    await load()
  } catch (e) {
    inviteError.value = writeMessage(e)
  } finally {
    inviteSubmitting.value = false
  }
}

/* ---------------- 移除成员 ---------------- */
const removeTarget = ref<MemberRead | null>(null)
const removeSubmitting = ref(false)

function openRemove(member: MemberRead): void {
  removeTarget.value = member
}

async function confirmRemove(reason: string): Promise<void> {
  const target = removeTarget.value
  if (target === null) {
    return
  }
  removeSubmitting.value = true
  try {
    await projectApi.removeMember(props.project.id, target.id, reason)
    ElMessage.success('成员已移除（已写入证据账本）')
    removeTarget.value = null
    await load()
  } catch (e) {
    ElMessage.error(writeMessage(e))
  } finally {
    removeSubmitting.value = false
  }
}

const removeImpact = computed<string[]>(() => {
  const name = removeTarget.value?.display_name ?? ''
  return [
    `成员「${name}」的全部职责槽将一并失效。`,
    '其历史审批记录保留在证据账本中，不随成员移除而删除。',
    '该成员将立即失去对本项目资源的访问权限。',
  ]
})

/* ---------------- 授予职责 ---------------- */
const grantTarget = ref<MemberRead | null>(null)
const grantSubmitting = ref(false)
const grantError = ref<string | null>(null)

function openGrant(member: MemberRead): void {
  grantError.value = null
  grantTarget.value = member
}

async function submitGrant(payload: RoleGrantRequest): Promise<void> {
  grantSubmitting.value = true
  grantError.value = null
  try {
    await projectApi.grantRole(props.project.id, payload)
    ElMessage.success('职责槽已授予（已写入证据账本）')
    grantTarget.value = null
    await load()
  } catch (e) {
    // 互斥（DUTY_CONFLICT）等由后端裁决；保留弹窗与已填内容供调整（fe-08 §7）
    grantError.value = writeMessage(e)
  } finally {
    grantSubmitting.value = false
  }
}
</script>

<template>
  <SectionCard title="成员" :padded="false">
    <template #extra>
      <span class="members__count">{{ formatInt(members.length) }} 名成员</span>
      <el-button size="small" :icon="Refresh" :loading="loading" @click="load">刷新</el-button>
      <el-button
        v-if="project.can_manage_members"
        size="small"
        type="primary"
        :icon="Plus"
        @click="openInvite"
      >
        添加成员
      </el-button>
    </template>

    <div class="members__body">
      <!-- 错误态由 QueryTable 内置（error 优先于 empty），此处不再重复渲染一份 -->
      <MemberTable
        :members="members"
        :loading="loading"
        :initial-loading="initialLoading"
        :error="error"
        :can-manage-members="project.can_manage_members"
        :can-manage-roles="project.can_manage_roles"
        :current-user-id="currentUserId"
        @retry="load"
        @grant-role="openGrant"
        @remove="openRemove"
      />
    </div>
  </SectionCard>

  <InviteMemberDialog
    v-model="inviteOpen"
    :project-id="project.id"
    :submitting="inviteSubmitting"
    :error="inviteError"
    @submit="submitInvite"
  />

  <SensitiveActionDialog
    :model-value="removeTarget !== null"
    title="移除成员"
    :impact="removeImpact"
    :confirm-phrase="removeTarget?.display_name ?? ''"
    submit-text="移除成员"
    :loading="removeSubmitting"
    @update:model-value="(v: boolean) => { if (!v) removeTarget = null }"
    @confirm="confirmRemove"
  />

  <RoleGrantDialog
    :model-value="grantTarget !== null"
    :project-id="project.id"
    :user-id="grantTarget?.user_id ?? null"
    :user-name="grantTarget?.display_name ?? ''"
    :members="[]"
    :existing-roles="grantTarget?.roles ?? []"
    :submitting="grantSubmitting"
    :error="grantError"
    @update:model-value="(v: boolean) => { if (!v) grantTarget = null }"
    @submit="submitGrant"
  />
</template>

<style scoped>
.members__count {
  font-size: 12px;
  color: var(--elt-text-muted);
}

.members__body {
  padding: 8px 14px 14px;
}
</style>
