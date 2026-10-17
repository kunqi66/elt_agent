<script setup lang="ts">
/**
 * 403（fe-01 §5.3 第 4 条）。
 *
 * 守卫在角色不匹配时**跳到这里**而不是静默回首页：
 * 静默跳转会表现为「点了没反应」，用户会反复点击并怀疑系统坏了。
 *
 * 页面必须回答三个问题：为什么被拦、我当前是什么身份、下一步找谁。
 * 只说「无权访问」会让人以为是 bug。
 */
import { computed } from 'vue'
import { useRouter } from 'vue-router'
import { Lock } from '@element-plus/icons-vue'
import { useProjectStore } from '@/stores/project'
import { rolesText } from '@/utils/enums'

const router = useRouter()
const projectStore = useProjectStore()

const projectName = computed<string>(() => projectStore.currentProject?.name ?? '当前项目')
const roles = computed<string>(() => rolesText(projectStore.myRoles))

function goBack(): void {
  // 用 back 而不是 push 首页：用户多半是从某个能访问的页面点过来的，
  // 返回来处比他重新找路更符合预期。没有历史时回落到首页。
  if (window.history.length > 1) {
    router.back()
  } else {
    void router.replace({ name: 'dashboard' })
  }
}
</script>

<template>
  <div class="errpage">
    <div class="errpage__card">
      <el-icon class="errpage__icon" :size="34"><Lock /></el-icon>
      <h1 class="errpage__code">403</h1>
      <p class="errpage__title">当前职责槽无权访问该页面</p>

      <dl class="errpage__facts">
        <div class="errpage__fact">
          <dt>项目</dt>
          <dd>{{ projectName }}</dd>
        </div>
        <div class="errpage__fact">
          <dt>你的职责槽</dt>
          <dd>{{ roles }}</dd>
        </div>
      </dl>

      <p class="errpage__hint">
        职责槽是按项目授予的，换个项目你可能就有权限。
        若确需访问，请联系项目管理员在「项目设置 → 职责槽」中调整。
      </p>

      <div class="errpage__actions">
        <el-button @click="goBack">返回上一页</el-button>
        <el-button type="primary" @click="router.replace({ name: 'dashboard' })">回到工作台</el-button>
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
  color: var(--elt-warn);
}

.errpage__code {
  margin: 8px 0 0;
  font-size: 30px;
  font-weight: 800;
  line-height: 1;
  color: var(--elt-text-primary);
}

.errpage__title {
  margin: 8px 0 18px;
  font-size: 13px;
  color: var(--elt-text-secondary);
}

.errpage__facts {
  display: flex;
  gap: 10px;
  margin: 0 0 16px;
}

.errpage__fact {
  flex: 1;
  padding: 8px 10px;
  text-align: left;
  background-color: var(--elt-bg-elevated);
  border-radius: var(--elt-radius-control);
}

.errpage__fact dt {
  font-size: 11px;
  color: var(--elt-text-muted);
}

.errpage__fact dd {
  margin: 3px 0 0;
  font-size: 12px;
  font-weight: 600;
  color: var(--elt-text-primary);
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
