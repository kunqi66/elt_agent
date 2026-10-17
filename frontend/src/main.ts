import { createApp } from 'vue'
import { createPinia } from 'pinia'

// 样式加载顺序是刻意的：
// 1. Element Plus 官方 dark css-vars —— 先把 --el-* 铺成暗色底；
// 2. 我们的设计令牌 —— 定义 --elt-*，取值与原型图 25 个变量逐字对齐；
// 3. Element 覆盖 —— 把 --el-* 映射到 --elt-*，必须排在 1 之后才能生效；
// 4. 通用状态样式 —— 骨架屏、滚动条、等宽文本。
// 顺序颠倒会让暗色主题被官方浅色变量反覆盖，表现为「部分组件白底」。
import 'element-plus/theme-chalk/dark/css-vars.css'
import '@/styles/variables.scss'
import '@/styles/element-overrides.scss'
import '@/styles/states.scss'

import App from '@/App.vue'
import router from '@/router'
import { setUnauthorizedHandler } from '@/api/client'
import { useAuthStore } from '@/stores/auth'

// 控制台恒为暗色：不提供浅色切换，也不跟随系统偏好——
// 数据平台的配色语义（风险红 / 告警橙 / 成功绿）在暗底上对比度是设计过的，
// 跟随系统会让「P1 橙」在浅底上变得难以辨认。
document.documentElement.classList.add('dark')

const app = createApp(App)
const pinia = createPinia()

app.use(pinia)
app.use(router)

// 401 且静默刷新失败时：清会话并回登录页。
// 回调注册在这里而不是 client.ts 内部，避免 client ↔ router ↔ store 的循环依赖。
setUnauthorizedHandler(() => {
  const authStore = useAuthStore()
  authStore.clearSession()
  if (router.currentRoute.value.name !== 'login') {
    void router.replace({ name: 'login' })
  }
})

/**
 * 启动前按需装载 dev mock。
 *
 * 用动态 import 而非静态引入：生产构建里 `VITE_USE_MOCK` 是字面量 'false'，
 * 条件恒假，Rollup 会把整个分支连同 mock 模块一起消掉——
 * mock 数据绝不能进生产包，否则会与真实证据账本混淆。
 */
async function bootstrap(): Promise<void> {
  if (import.meta.env.VITE_USE_MOCK === 'true') {
    const { installMockAdapter } = await import('@/api/mock')
    installMockAdapter()
  }
  await router.isReady()
  app.mount('#app')
}

void bootstrap()
