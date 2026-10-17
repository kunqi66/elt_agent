<script setup lang="ts">
/**
 * 账号与安全（fe-08 §1.7 / §6.6 / §6.7 / §8）。
 *
 * 几条硬约束：
 * - 身份摘要来自 `GET /auth/me`（`authStore.profile`），**不解析 JWT**（fe-01 §1）；
 * - **不展示任何令牌/密钥明文**：本页不渲染 token，也不从后端取会话设备列表
 *   （refresh 目前只落 Redis 黑名单，无正向台账，见 fe-08 §10）；
 * - 改密请求带 `skipAuthRefresh` / `skipUnauthorizedHandler`，
 *   旧密码输错（401 BAD_CREDENTIALS）**不会**把用户踢出登录（fe-08 §6.6 / §10）。
 */
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { Iphone } from '@element-plus/icons-vue'
import { ElMessage } from '@/utils/feedback'
import { errorMessage, fieldErrors, isBizError, toDisplayMessage } from '@/utils/errors'
import * as authApi from '@/api/auth'
import { useAuthStore } from '@/stores/auth'
import { DangerConfirmDialog, ErrorState, PageHeader, SectionCard } from '@/components/common'
import { ROLE_NAMES, enumText } from '@/utils/enums'
import ChangePasswordForm from './components/ChangePasswordForm.vue'
import type { ChangePasswordRequest, TenantRead } from '@/types'

const router = useRouter()
const authStore = useAuthStore()

/* ---------------- 身份摘要 ---------------- */
const profile = computed(() => authStore.profile)
// 画像为空说明还没拉过（刷新页面场景）：首帧就该出骨架，而不是空卡片
const profileLoading = ref(authStore.profile === null)
const profileError = ref<string | null>(null)

async function ensureProfile(): Promise<void> {
  if (authStore.profile !== null) {
    return
  }
  profileLoading.value = true
  profileError.value = null
  try {
    await authStore.loadProfile()
  } catch (e) {
    profileError.value = toDisplayMessage(e)
  } finally {
    profileLoading.value = false
  }
}

/** profile.projects[].roles 是后端下发的 string[]，收窄为已知角色后再取中文 */
function projectRolesText(roles: readonly string[]): string {
  const known = ROLE_NAMES.filter((r) => roles.includes(r))
  return known.length === 0 ? '无职责槽' : known.map((r) => enumText('RoleName', r)).join(' / ')
}

/* ---------------- 租户（只读） ---------------- */
const tenant = ref<TenantRead | null>(null)
const tenantError = ref<string | null>(null)

async function loadTenant(): Promise<void> {
  tenantError.value = null
  try {
    tenant.value = await authApi.fetchTenant()
  } catch (e) {
    tenantError.value = toDisplayMessage(e)
  }
}

// TenantStatus 未在 utils/enums 登记为 EnumKind，故此处仅做最小展示映射
const tenantStatusText = computed<string>(() =>
  tenant.value?.status === 'suspended' ? '已暂停' : '活跃',
)

onMounted(() => {
  void ensureProfile()
  void loadTenant()
})

/* ---------------- 修改密码 ---------------- */
const pwSubmitting = ref(false)
const pwError = ref<string | null>(null)
const pwFieldErrors = ref<Record<string, string>>({})

async function submitPassword(payload: ChangePasswordRequest): Promise<void> {
  pwSubmitting.value = true
  pwError.value = null
  pwFieldErrors.value = {}
  try {
    await authApi.changePassword(payload)
    // 服务端会吊销全部 refresh token：必须引导重新登录，避免用户继续用旧会话
    ElMessage.success('密码已修改，请使用新密码重新登录')
    await authStore.logout()
    await router.replace({ name: 'login' })
  } catch (e) {
    if (isBizError(e)) {
      switch (e.code) {
        case 'BAD_CREDENTIALS':
          pwFieldErrors.value = { old_password: '当前密码不正确' }
          break
        case 'VALIDATION_ERROR':
          pwFieldErrors.value = fieldErrors(e)
          pwError.value = errorMessage(e)
          break
        case 'ACCOUNT_LOCKED':
          pwError.value = '账号已被临时锁定，请联系管理员。'
          break
        default:
          pwError.value = toDisplayMessage(e)
      }
    } else {
      pwError.value = toDisplayMessage(e)
    }
  } finally {
    pwSubmitting.value = false
  }
}

/* ---------------- 退出全部设备 ---------------- */
const logoutAllOpen = ref(false)
const logoutAllSubmitting = ref(false)

async function confirmLogoutAll(): Promise<void> {
  logoutAllSubmitting.value = true
  try {
    await authApi.logoutAll()
    logoutAllOpen.value = false
    ElMessage.success('已退出全部设备，请重新登录')
    await authStore.logout()
    await router.replace({ name: 'login' })
  } catch (e) {
    ElMessage.error(toDisplayMessage(e))
  } finally {
    logoutAllSubmitting.value = false
  }
}
</script>

<template>
  <div class="account">
    <PageHeader
      title="账号与安全"
      description="身份与租户来自服务端 /auth/me；改密会作废该账号在所有设备上的登录会话。"
    />

    <el-alert
      v-if="tenant !== null && tenant.status === 'suspended'"
      class="account__suspended"
      type="error"
      :closable="false"
      show-icon
      title="租户已暂停，部分功能不可用"
      description="只读数据仍然可见以便排查；如需恢复请联系平台管理员。"
    />

    <div class="account__grid">
      <SectionCard title="当前身份" :loading="profileLoading">
        <ErrorState
          v-if="profileError !== null"
          title="身份信息加载失败"
          :message="profileError"
          @retry="ensureProfile"
        />
        <template v-else-if="profile !== null">
          <dl class="account__facts">
            <div class="account__fact">
              <dt>用户名</dt>
              <dd class="elt-mono">{{ profile.username }}</dd>
            </div>
            <div class="account__fact">
              <dt>显示名称</dt>
              <dd>{{ profile.display_name }}</dd>
            </div>
            <div class="account__fact">
              <dt>邮箱</dt>
              <dd>{{ profile.email }}</dd>
            </div>
            <div class="account__fact">
              <dt>租户</dt>
              <dd>
                <span>{{ tenant?.name ?? '—' }}</span>
                <span class="elt-mono account__muted">{{ tenant?.code ?? profile.tenant_code }}</span>
              </dd>
            </div>
          </dl>

          <p class="account__section-label">职责槽（按项目授予）</p>
          <ul class="account__projects">
            <li v-for="p in profile.projects" :key="p.id" class="account__project">
              <span class="account__project-name">{{ p.name }}</span>
              <span class="account__project-roles">{{ projectRolesText(p.roles) }}</span>
            </li>
            <li v-if="profile.projects.length === 0" class="account__project-empty">
              当前没有被授予任何项目职责槽。
            </li>
          </ul>
          <p class="account__hint">
            本平台没有全局「管理员」角色；职责槽按项目授予，同一用户名下不同项目可以持有不同职责槽。
          </p>
        </template>
      </SectionCard>

      <SectionCard title="租户信息（只读）">
        <ErrorState
          v-if="tenantError !== null"
          title="租户信息加载失败"
          :message="tenantError"
          @retry="loadTenant"
        />
        <dl v-else class="account__facts">
          <div class="account__fact">
            <dt>租户名称</dt>
            <dd>{{ tenant?.name ?? '—' }}</dd>
          </div>
          <div class="account__fact">
            <dt>租户编码</dt>
            <dd class="elt-mono">{{ tenant?.code ?? '—' }}</dd>
          </div>
          <div class="account__fact">
            <dt>状态</dt>
            <dd>
              <el-tag
                :type="tenant?.status === 'suspended' ? 'danger' : 'success'"
                size="small"
                disable-transitions
              >
                {{ tenantStatusText }}
              </el-tag>
            </dd>
          </div>
        </dl>
      </SectionCard>
    </div>

    <SectionCard class="account__password" title="修改密码">
      <ChangePasswordForm
        :submitting="pwSubmitting"
        :error="pwError"
        :field-errors="pwFieldErrors"
        @submit="submitPassword"
      />
      <p class="account__hint">
        修改成功后服务端会吊销该账号的全部 refresh token，你需要用新密码在所有设备上重新登录。
      </p>
    </SectionCard>

    <SectionCard class="account__sessions" title="会话与设备">
      <p class="account__hint">
        出于安全考虑，本页不展示任何令牌明文；后端当前也未向控制台暴露逐设备的登录台账。
        怀疑凭据泄露时，请使用下方操作一次性吊销所有设备的登录会话。
      </p>
      <el-button
        type="danger"
        :icon="Iphone"
        :disabled="logoutAllSubmitting"
        @click="logoutAllOpen = true"
      >
        退出全部设备
      </el-button>
    </SectionCard>

    <DangerConfirmDialog
      v-model="logoutAllOpen"
      title="退出全部设备"
      description="该操作会吊销本账号在所有设备上的登录会话（含当前会话）。仅在你怀疑凭据泄露或丢失设备时使用。"
      confirm-phrase="退出全部设备"
      acknowledge="我已了解：所有设备上的登录会话都会被吊销，需要重新登录"
      :require-reason="false"
      :loading="logoutAllSubmitting"
      @confirm="confirmLogoutAll"
    />
  </div>
</template>

<style scoped>
.account__suspended {
  margin-bottom: 14px;
}

.account__grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 14px;
  margin-bottom: 14px;
}

@media (max-width: 900px) {
  .account__grid {
    grid-template-columns: minmax(0, 1fr);
  }
}

.account__facts {
  display: flex;
  flex-direction: column;
  gap: 10px;
  margin: 0;
}

.account__fact {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.account__fact dt {
  font-size: 11px;
  color: var(--elt-text-muted);
}

.account__fact dd {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 0;
  font-size: 13px;
  color: var(--elt-text-primary);
}

.account__muted {
  font-size: 12px;
  color: var(--elt-text-muted);
}

.account__section-label {
  margin: 16px 0 6px;
  font-size: 11px;
  font-weight: 600;
  color: var(--elt-text-secondary);
}

.account__projects {
  padding: 0;
  margin: 0;
  list-style: none;
}

.account__project {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 6px 0;
  font-size: 12px;
  border-bottom: 1px solid var(--elt-border-subtle);
}

.account__project-name {
  color: var(--elt-text-primary);
}

.account__project-roles {
  color: var(--elt-text-secondary);
}

.account__project-empty {
  font-size: 12px;
  color: var(--elt-text-muted);
}

.account__hint {
  margin: 12px 0 0;
  font-size: 11px;
  line-height: 1.7;
  color: var(--elt-text-muted);
}

.account__password,
.account__sessions {
  margin-bottom: 14px;
}
</style>
