<script setup lang="ts">
/**
 * 登录页（fe-01 §5.1）。
 *
 * 几处刻意的选择：
 * - **凭据错误内联显示**，不用 toast（见 utils/errors 的 SPECS：BAD_CREDENTIALS → inline）。
 *   凭据报错是用户要对照着改的，3 秒后消失的提示等于没提示。
 * - **不记住密码、不把用户名写进 localStorage**：本平台托管生产数据源的接入凭据，
 *   在这台机器上留下任何登录痕迹都不是好交易。浏览器自己的密码管理器要记是它的事。
 * - `tenant_code` 默认收起：单租户部署下它是噪声，多租户时点一下就能填。
 */
import { computed, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from '@/utils/feedback'
import { errorMessage, isBizError } from '@/utils/errors'
import { useAuthStore } from '@/stores/auth'
import { useProjectStore } from '@/stores/project'
import type { LoginRequest } from '@/types'

const route = useRoute()
const router = useRouter()
const authStore = useAuthStore()
const projectStore = useProjectStore()

const username = ref('')
const password = ref('')
const tenantCode = ref('')
const showTenant = ref(false)
const submitting = ref(false)
/** 凭据类错误的落点：表单内联，而不是弹窗 */
const inlineError = ref<string | null>(null)

// 用 computed 做按钮可用性，而不是在模板里堆一长串 &&，
// 顺便保证「提交中」时按钮一定是禁用的
const canSubmit = computed<boolean>(
  () => username.value.trim().length > 0 && password.value.length > 0 && !submitting.value,
)

const redirectTarget = computed<string>(() => {
  const raw = route.query.redirect
  const value = Array.isArray(raw) ? raw[0] : raw
  // 只接受站内绝对路径：`//evil.com` 这种协议相对地址会被浏览器当成外站，
  // 用它做登录后跳转就是一个开放重定向。
  if (typeof value === 'string' && value.startsWith('/') && !value.startsWith('//')) {
    return value
  }
  return '/dashboard'
})

async function submit(): Promise<void> {
  if (!canSubmit.value) {
    return
  }
  submitting.value = true
  inlineError.value = null

  try {
    const payload: LoginRequest = {
      username: username.value.trim(),
      password: password.value,
    }
    // 空串不提交：后端按「默认租户」处理，提交空串反而会变成一次显式匹配
    if (showTenant.value && tenantCode.value.trim() !== '') {
      payload.tenant_code = tenantCode.value.trim()
    }

    await authStore.login(payload)

    // 画像与项目列表决定菜单与角色可见性，必须在这里就绪，
    // 否则首屏会先渲染出「什么都没权限」的空菜单再闪回来。
    await authStore.loadProfile()
    await projectStore.loadProjects()

    ElMessage.success('登录成功')
    await router.replace(redirectTarget.value)
  } catch (err) {
    // 登录页没有「页面级错误」的落点（此刻用户哪儿也去不了），
    // 因此不论是 inline 还是 page 类错误，一律内联到表单里。
    inlineError.value = errorMessage(err)
    // 非业务异常（网络断开、后端 500）在控制台留一份原始对象便于排查；
    // 业务异常是预期内的（密码错、账号停用），不留噪音。
    if (!isBizError(err)) {
      console.error('[login] 非业务异常', err)
    }
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <div class="login">
    <div class="login__panel">
      <header class="login__brand">
        <span class="login__logo">ELT</span>
        <div>
          <h1 class="login__title">ELT-Agent 控制台</h1>
          <p class="login__subtitle">自然语言 → 可审计的确定性数据集成</p>
        </div>
      </header>

      <el-form class="login__form" label-position="top" @submit.prevent="submit">
        <el-form-item label="用户名">
          <el-input
            v-model="username"
            size="large"
            autocomplete="username"
            placeholder="请输入用户名"
            :disabled="submitting"
            @keyup.enter="submit"
          />
        </el-form-item>

        <el-form-item label="密码">
          <el-input
            v-model="password"
            type="password"
            size="large"
            autocomplete="current-password"
            show-password
            placeholder="请输入密码"
            :disabled="submitting"
            @keyup.enter="submit"
          />
        </el-form-item>

        <el-form-item v-if="showTenant" label="租户编码">
          <el-input
            v-model="tenantCode"
            size="large"
            placeholder="留空则使用默认租户"
            :disabled="submitting"
            @keyup.enter="submit"
          />
        </el-form-item>

        <el-alert
          v-if="inlineError !== null"
          class="login__error"
          type="error"
          :closable="false"
          show-icon
          :title="inlineError"
        />

        <el-button
          class="login__submit"
          type="primary"
          size="large"
          :loading="submitting"
          :disabled="!canSubmit"
          @click="submit"
        >
          登录
        </el-button>

        <button
          v-if="!showTenant"
          type="button"
          class="login__tenant-toggle"
          :disabled="submitting"
          @click="showTenant = true"
        >
          使用指定租户登录
        </button>
      </el-form>

      <footer class="login__foot">
        所有操作留哈希链证据；凭据仅以 Vault 引用形式存储。
      </footer>
    </div>
  </div>
</template>

<style scoped>
.login {
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 100%;
  padding: 24px;
  background:
    radial-gradient(1000px 500px at 50% -10%, #16233c 0%, transparent 60%),
    var(--elt-bg-app);
}

.login__panel {
  width: 100%;
  max-width: 380px;
  padding: 28px 26px 22px;
  background-color: var(--elt-bg-surface);
  border: 1px solid var(--elt-border-subtle);
  border-radius: var(--elt-radius-card);
  box-shadow: 0 18px 48px rgb(0 0 0 / 45%);
}

.login__brand {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 22px;
}

.login__logo {
  display: inline-flex;
  flex: none;
  align-items: center;
  justify-content: center;
  width: 38px;
  height: 38px;
  font-size: 13px;
  font-weight: 800;
  color: #fff;
  letter-spacing: 0.5px;
  background-color: var(--elt-accent);
  border-radius: 10px;
}

.login__title {
  margin: 0;
  font-size: 16px;
  font-weight: 600;
  color: var(--elt-text-primary);
}

.login__subtitle {
  margin: 2px 0 0;
  font-size: 12px;
  color: var(--elt-text-muted);
}

.login__form :deep(.el-form-item__label) {
  font-size: 12px;
  color: var(--elt-text-secondary);
}

.login__error {
  margin-bottom: 14px;
}

.login__submit {
  width: 100%;
}

.login__tenant-toggle {
  display: block;
  width: 100%;
  margin-top: 12px;
  font-size: 12px;
  color: var(--elt-text-muted);
  cursor: pointer;
  background: none;
  border: none;
}

.login__tenant-toggle:hover {
  color: var(--elt-accent);
}

.login__foot {
  padding-top: 16px;
  margin-top: 18px;
  font-size: 11px;
  line-height: 1.6;
  color: var(--elt-text-muted);
  text-align: center;
  border-top: 1px solid var(--elt-border-subtle);
}
</style>
