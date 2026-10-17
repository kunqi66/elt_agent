<script setup lang="ts">
/**
 * 404。
 *
 * 路由兜底指向这里而不是首页：静默跳首页会让用户以为自己点错了，
 * 而实际是链接失效——这两种情况的处理方式完全不同（前者改习惯，后者改链接）。
 *
 * 因此把**实际访问的路径**显示出来，用户才能判断是链接问题还是自己记错了。
 */
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Search } from '@element-plus/icons-vue'
import { useAuthStore } from '@/stores/auth'

const route = useRoute()
const router = useRouter()
const authStore = useAuthStore()

const attemptedPath = computed<string>(() => route.fullPath)
const loggedIn = computed<boolean>(() => authStore.isAuthenticated)
</script>

<template>
  <div class="errpage">
    <div class="errpage__card">
      <el-icon class="errpage__icon" :size="34"><Search /></el-icon>
      <h1 class="errpage__code">404</h1>
      <p class="errpage__title">没有找到这个页面</p>

      <p class="errpage__path elt-mono elt-ellipsis" :title="attemptedPath">{{ attemptedPath }}</p>

      <p class="errpage__hint">
        链接可能已失效，或对应的记录已被删除。
        如果你是从站内点过来的，说明那个入口指向了一个不存在的路由。
      </p>

      <div class="errpage__actions">
        <el-button v-if="loggedIn" @click="router.replace({ name: 'dashboard' })">回到工作台</el-button>
        <el-button v-else type="primary" @click="router.replace({ name: 'login' })">去登录</el-button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.errpage {
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 100%;
  padding: 24px;
  background-color: var(--elt-bg-app);
}

.errpage__card {
  width: 100%;
  max-width: 460px;
  padding: 30px;
  text-align: center;
  background-color: var(--elt-bg-surface);
  border: 1px solid var(--elt-border-subtle);
  border-radius: var(--elt-radius-card);
}

.errpage__icon {
  color: var(--elt-text-muted);
}

.errpage__code {
  margin: 8px 0 0;
  font-size: 30px;
  font-weight: 800;
  line-height: 1;
  color: var(--elt-text-primary);
}

.errpage__title {
  margin: 8px 0 14px;
  font-size: 13px;
  color: var(--elt-text-secondary);
}

.errpage__path {
  display: block;
  padding: 8px 10px;
  margin: 0 0 16px;
  font-size: 12px;
  color: var(--elt-warn);
  text-align: left;
  background-color: var(--elt-bg-code);
  border: 1px solid var(--elt-border-subtle);
  border-radius: var(--elt-radius-control);
}

.errpage__hint {
  margin: 0 0 20px;
  font-size: 12px;
  line-height: 1.7;
  color: var(--elt-text-muted);
  text-align: left;
}

.errpage__actions {
  display: flex;
  gap: 8px;
  justify-content: center;
}
</style>
