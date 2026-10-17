<script setup lang="ts">
/**
 * 「职责槽」标签页（fe-08 §1.5 / §1.6 / §5）。
 *
 * `role_assignment` 没有 status 列，只有 `valid_from` / `valid_to`；
 * 界面据时间窗派生三个**展示态**（生效中 / 未生效 / 已失效），不落库（fe-08 §5）。
 * 回收是物理删除行，历史由证据账本承载——二次确认里会写明这一点。
 */
import { computed, onMounted, ref, watch } from 'vue'
import { Delete, Plus, Refresh } from '@element-plus/icons-vue'
import { ElMessage } from '@/utils/feedback'
import { isBizError, toDisplayMessage } from '@/utils/errors'
import { formatDate, formatDateTime, formatRemaining, remainingMs } from '@/utils/format'
import * as projectApi from '@/api/project'
import { QueryTable, SectionCard, StatusTag } from '@/components/common'
import RoleMatrixTable from './RoleMatrixTable.vue'
import FourEyesExplainer from './FourEyesExplainer.vue'
import RoleGrantDialog from './RoleGrantDialog.vue'
import SensitiveActionDialog from './SensitiveActionDialog.vue'
import type {
  AssignmentDisplayState,
  MemberRead,
  ProjectRead,
  RoleAssignmentRead,
  RoleGrantRequest,
} from '@/types'

const props = defineProps<{ project: ProjectRead }>()

const assignments = ref<RoleAssignmentRead[]>([])
const loading = ref(false)
const initialLoading = ref(true)
const error = ref<string | null>(null)

const members = ref<MemberRead[]>([])

function writeMessage(e: unknown): string {
  if (isBizError(e)) {
    switch (e.code) {
      case 'CONFLICT':
        return '数据已被修改或该用户已持有此职责，请刷新后重试。'
      case 'DUTY_CONFLICT':
        return '职责互斥：同一项目内 maker 与 checker_* 不能由同一人担任，服务端已拒绝。'
      case 'FORBIDDEN':
      case 'ROLE_REQUIRED':
        return '当前职责槽无权授予或回收职责。'
      case 'NOT_FOUND':
        return '目标记录或用户不存在，可能已被回收。'
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
    assignments.value = await projectApi.fetchRoleAssignments(props.project.id)
  } catch (e) {
    error.value = toDisplayMessage(e)
  } finally {
    loading.value = false
    initialLoading.value = false
  }
}

/** 授予需要成员列表（选择被授予人）；失败不阻塞列表展示，只在打开弹窗时为空 */
async function loadMembers(): Promise<void> {
  try {
    members.value = await projectApi.fetchMembers(props.project.id)
  } catch {
    members.value = []
  }
}

onMounted(() => {
  void load()
  void loadMembers()
})

watch(
  () => props.project.id,
  () => {
    assignments.value = []
    initialLoading.value = true
    void load()
    void loadMembers()
  },
)

/* ---------------- 展示态派生（不落库） ---------------- */
function displayState(a: RoleAssignmentRead): AssignmentDisplayState {
  const now = Date.now()
  const to = a.valid_to === null ? null : new Date(a.valid_to).getTime()
  const from = a.valid_from === null ? null : new Date(a.valid_from).getTime()
  if (to !== null && to <= now) {
    return 'expired'
  }
  if (from !== null && from > now) {
    return 'scheduled'
  }
  return 'effective'
}

function stateText(a: RoleAssignmentRead): string {
  const state = displayState(a)
  if (state === 'scheduled') {
    return `未生效（${formatDate(a.valid_from)} 起）`
  }
  if (state === 'expired') {
    return '已失效'
  }
  return '生效中'
}

function stateTagType(a: RoleAssignmentRead): 'success' | 'info' {
  return displayState(a) === 'effective' ? 'success' : 'info'
}

/** 临时授权的到期倒计时；仅在生效中且设置了 valid_to 时展示 */
function expiresHint(a: RoleAssignmentRead): string {
  if (displayState(a) !== 'effective' || a.valid_to === null) {
    return ''
  }
  const ms = remainingMs(a.valid_to)
  if (ms === null) {
    return ''
  }
  const soon = ms < 7 * 24 * 3600 * 1000
  return `剩余 ${formatRemaining(ms)}${soon ? ' · 即将到期' : ''}`
}

function isExpiringSoon(a: RoleAssignmentRead): boolean {
  if (displayState(a) !== 'effective' || a.valid_to === null) {
    return false
  }
  const ms = remainingMs(a.valid_to)
  return ms !== null && ms < 7 * 24 * 3600 * 1000
}

function windowText(a: RoleAssignmentRead): string {
  if (a.valid_from === null && a.valid_to === null) {
    return '永久'
  }
  const from = a.valid_from === null ? '立即' : formatDate(a.valid_from)
  const to = a.valid_to === null ? '长期' : formatDate(a.valid_to)
  return `${from} ~ ${to}`
}

/** 当前两个审批槽的持有者（生效中的 checker_data / checker_security） */
const dataSlotHolders = computed<string[]>(() =>
  assignments.value
    .filter((a) => a.role === 'checker_data' && displayState(a) === 'effective')
    .map((a) => a.user_name),
)

const securitySlotHolders = computed<string[]>(() =>
  assignments.value
    .filter((a) => a.role === 'checker_security' && displayState(a) === 'effective')
    .map((a) => a.user_name),
)

const slotConflict = computed<boolean>(() => {
  if (dataSlotHolders.value.length === 0 || securitySlotHolders.value.length === 0) {
    return false
  }
  return dataSlotHolders.value.some((n) => securitySlotHolders.value.includes(n))
})

/* ---------------- 授予 ---------------- */
const grantOpen = ref(false)
const grantSubmitting = ref(false)
const grantError = ref<string | null>(null)

function openGrant(): void {
  grantError.value = null
  if (members.value.length === 0) {
    void loadMembers()
  }
  grantOpen.value = true
}

async function submitGrant(payload: RoleGrantRequest): Promise<void> {
  grantSubmitting.value = true
  grantError.value = null
  try {
    await projectApi.grantRole(props.project.id, payload)
    ElMessage.success('职责槽已授予（已写入证据账本）')
    grantOpen.value = false
    await load()
  } catch (e) {
    grantError.value = writeMessage(e)
  } finally {
    grantSubmitting.value = false
  }
}

/* ---------------- 回收 ---------------- */
const revokeTarget = ref<RoleAssignmentRead | null>(null)
const revokeSubmitting = ref(false)

function openRevoke(a: RoleAssignmentRead): void {
  revokeTarget.value = a
}

/**
 * el-table 插槽的 row 静态类型是 Element Plus 的 DefaultRow，与我们的 DTO 无类型关联。
 * 直接读属性不报错（索引签名读出 any），但作为参数传给强类型函数就会失败。
 * 与其放宽那些函数的签名（会让整文件的类型检查一起退化），不如在调用点收口。
 */
function assignmentOf(row: unknown): RoleAssignmentRead {
  return row as RoleAssignmentRead
}

async function confirmRevoke(reason: string): Promise<void> {
  const target = revokeTarget.value
  if (target === null) {
    return
  }
  revokeSubmitting.value = true
  try {
    await projectApi.revokeRole(props.project.id, target.id, reason)
    ElMessage.success('职责槽已回收（已写入证据账本）')
    revokeTarget.value = null
    await load()
  } catch (e) {
    ElMessage.error(writeMessage(e))
  } finally {
    revokeSubmitting.value = false
  }
}

const revokeImpact = computed<string[]>(() => {
  const t = revokeTarget.value
  if (t === null) {
    return []
  }
  const lines = [`收回「${t.user_name}」的职责槽：${t.role}。`]
  if (t.role === 'checker_data' || t.role === 'checker_security') {
    lines.push('该用户在该项目立即失去对应槽位的审批能力。')
    lines.push('其在途的待审批准备单需由他人接手。')
  }
  lines.push('回收为物理删除该记录；历史仅存于证据账本。')
  return lines
})
</script>

<template>
  <div class="roles">
    <SectionCard title="四眼规则与职责矩阵">
      <FourEyesExplainer />
      <div class="roles__matrix">
        <RoleMatrixTable />
      </div>
      <el-alert
        v-if="slotConflict"
        class="roles__slot-conflict"
        type="warning"
        :closable="false"
        show-icon
        title="数据审批槽与安全审批槽当前由同一人担任"
        description="两个槽位仍会各自独立审批，但 P1 场景下建议改由不同人担任（fe-08 §6.3）。"
      />
      <div class="roles__slots">
        <div class="roles__slot">
          <span class="roles__slot-label">数据审批槽</span>
          <span class="roles__slot-user">{{ dataSlotHolders.length === 0 ? '暂无' : dataSlotHolders.join('、') }}</span>
        </div>
        <div class="roles__slot">
          <span class="roles__slot-label">安全审批槽</span>
          <span class="roles__slot-user">{{ securitySlotHolders.length === 0 ? '暂无' : securitySlotHolders.join('、') }}</span>
        </div>
      </div>
    </SectionCard>

    <SectionCard class="roles__assignments" title="职责槽分配" :padded="false">
      <template #extra>
        <el-button size="small" :icon="Refresh" :loading="loading" @click="load">刷新</el-button>
        <el-button
          v-if="project.can_manage_roles"
          size="small"
          type="primary"
          :icon="Plus"
          @click="openGrant"
        >
          授予职责
        </el-button>
      </template>

      <div class="roles__table">
        <QueryTable
          :items="assignments"
          :loading="loading"
          :initial-loading="initialLoading"
          :error="error"
          :total="assignments.length"
          :show-pagination="false"
          row-key="id"
          :min-height="200"
          empty-variant="no-data"
          empty-text="还没有职责槽授予"
          empty-description="从成员列表或上方「授予职责」把职责槽分配给已加入的成员。"
          @retry="load"
        >
          <el-table-column label="成员" min-width="140">
            <template #default="{ row }">
              <span class="roles__cell-strong">{{ assignmentOf(row).user_name }}</span>
            </template>
          </el-table-column>

          <el-table-column label="职责槽" width="140">
            <template #default="{ row }">
              <StatusTag kind="RoleName" :value="assignmentOf(row).role" />
            </template>
          </el-table-column>

          <el-table-column label="授予人" min-width="120">
            <template #default="{ row }">
              <span class="roles__cell-muted">{{ assignmentOf(row).granted_by_name === '' ? '—' : assignmentOf(row).granted_by_name }}</span>
            </template>
          </el-table-column>

          <el-table-column label="授予时间" width="160">
            <template #default="{ row }">
              <span class="roles__cell-muted">{{ formatDateTime(assignmentOf(row).granted_at) }}</span>
            </template>
          </el-table-column>

          <el-table-column label="有效期" min-width="190">
            <template #default="{ row }">
              <div class="roles__window">
                <span>{{ windowText(assignmentOf(row)) }}</span>
                <span v-if="expiresHint(assignmentOf(row)) !== ''" class="roles__window-hint" :class="{ 'is-soon': isExpiringSoon(assignmentOf(row)) }">
                  {{ expiresHint(assignmentOf(row)) }}
                </span>
              </div>
            </template>
          </el-table-column>

          <el-table-column label="状态" min-width="140">
            <template #default="{ row }">
              <el-tag
                :type="stateTagType(assignmentOf(row))"
                size="small"
                disable-transitions
                :class="{ 'roles__expired': displayState(assignmentOf(row)) === 'expired' }"
              >
                {{ stateText(assignmentOf(row)) }}
              </el-tag>
            </template>
          </el-table-column>

          <el-table-column label="操作" width="90" fixed="right">
            <template #default="{ row }">
              <el-button
                v-if="project.can_manage_roles && displayState(assignmentOf(row)) !== 'expired'"
                link
                type="danger"
                size="small"
                :icon="Delete"
                @click.stop="openRevoke(assignmentOf(row))"
              >
                回收
              </el-button>
            </template>
          </el-table-column>
        </QueryTable>
      </div>
    </SectionCard>

    <RoleGrantDialog
      v-model="grantOpen"
      :project-id="project.id"
      :user-id="null"
      user-name=""
      :members="members"
      :existing-roles="[]"
      :submitting="grantSubmitting"
      :error="grantError"
      @submit="submitGrant"
    />

    <SensitiveActionDialog
      :model-value="revokeTarget !== null"
      title="回收职责槽"
      :impact="revokeImpact"
      submit-text="回收职责槽"
      :loading="revokeSubmitting"
      @update:model-value="(v: boolean) => { if (!v) revokeTarget = null }"
      @confirm="confirmRevoke"
    />
  </div>
</template>

<style scoped>
.roles__matrix {
  margin-top: 14px;
}

.roles__slot-conflict {
  margin-top: 12px;
}

.roles__slots {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
  margin-top: 14px;
}

.roles__slot {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 10px 12px;
  background-color: var(--elt-bg-elevated);
  border-radius: var(--elt-radius-control);
}

.roles__slot-label {
  font-size: 11px;
  color: var(--elt-text-muted);
}

.roles__slot-user {
  font-size: 13px;
  color: var(--elt-text-primary);
}

.roles__assignments {
  margin-top: 14px;
}

.roles__table {
  padding: 8px 14px 14px;
}

.roles__cell-strong {
  font-size: 13px;
  color: var(--elt-text-primary);
}

.roles__cell-muted {
  font-size: 12px;
  color: var(--elt-text-secondary);
}

.roles__window {
  display: flex;
  flex-direction: column;
  line-height: 1.3;
  font-size: 12px;
  color: var(--elt-text-secondary);
}

.roles__window-hint {
  font-size: 11px;
  color: var(--elt-text-muted);
}

.roles__window-hint.is-soon {
  color: var(--elt-warn);
}

.roles__expired {
  text-decoration: line-through;
}
</style>
