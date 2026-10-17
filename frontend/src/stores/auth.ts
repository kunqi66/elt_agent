import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import * as authApi from '@/api/auth'
import { clearAuth, readAuth, writeAuth } from '@/api/client'
import type { CurrentUser, LoginRequest, LoginUser, TokenPair } from '@/types'

/**
 * 认证会话 store。
 * token 自己读写 localStorage（不引入持久化插件），刷新页面后仍保持登录态。
 */
export const useAuthStore = defineStore('auth', () => {
  const snapshot = readAuth()

  const accessToken = ref<string>(snapshot?.accessToken ?? '')
  const refreshToken = ref<string>(snapshot?.refreshToken ?? '')
  const expiresAt = ref<number>(snapshot?.expiresAt ?? 0)
  const user = ref<LoginUser | null>(snapshot?.user ?? null)
  /** /auth/me 返回的完整画像，含租户编码与项目角色 */
  const profile = ref<CurrentUser | null>(null)

  const isAuthenticated = computed<boolean>(() => accessToken.value.length > 0)
  const displayName = computed<string>(
    () => profile.value?.display_name ?? user.value?.display_name ?? user.value?.username ?? '',
  )

  /** 把 token 对写入内存与 localStorage */
  function applyTokenPair(pair: TokenPair): void {
    accessToken.value = pair.access_token
    refreshToken.value = pair.refresh_token
    expiresAt.value = Date.now() + pair.expires_in * 1000
    user.value = pair.user
    writeAuth({
      accessToken: pair.access_token,
      refreshToken: pair.refresh_token,
      expiresAt: expiresAt.value,
      user: pair.user,
    })
  }

  /** 登录成功即建立会话 */
  async function login(payload: LoginRequest): Promise<void> {
    const pair = await authApi.login(payload)
    applyTokenPair(pair)
  }

  /** 拉取当前用户画像（项目列表、角色等） */
  async function loadProfile(): Promise<CurrentUser> {
    const me = await authApi.fetchMe()
    profile.value = me
    return me
  }

  /** 清空内存与本地会话；401 刷新失败时由拦截器回调触发 */
  function clearSession(): void {
    accessToken.value = ''
    refreshToken.value = ''
    expiresAt.value = 0
    user.value = null
    profile.value = null
    clearAuth()
  }

  /** 退出登录：服务端注销失败也必须清掉本地会话，否则用户会卡在登录态 */
  async function logout(): Promise<void> {
    try {
      await authApi.logout()
    } catch {
      // 忽略：本地会话清理是无条件要做的
    }
    clearSession()
  }

  return {
    accessToken,
    refreshToken,
    expiresAt,
    user,
    profile,
    isAuthenticated,
    displayName,
    login,
    loadProfile,
    logout,
    clearSession,
  }
})
