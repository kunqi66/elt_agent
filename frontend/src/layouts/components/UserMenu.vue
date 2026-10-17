<script setup lang="ts">
/**
 * 用户菜单。
 *
 * 下拉里显示**当前项目下的职责槽**，而不是「管理员/普通用户」——
 * 本平台没有 admin 角色（`RoleName` 只有 maker / checker_data /
 * checker_security / operator / auditor），而职责槽是**按项目**授予的。
 * 显示成全局身份会让人误以为自己在别的项目里也有同样的权限。
 */
import { computed } from 'vue'
import { useRouter } from 'vue-router'
import { ArrowDown, Setting, SwitchButton, User } from '@element-plus/icons-vue'
import { useAuthStore } from '@/stores/auth'
import { useProjectStore } from '@/stores/project'
import { rolesText } from '@/utils/enums'

const router = useRouter()
const authStore = useAuthStore()
const projectStore = useProjectStore()

const displayName = computed<string>(() => authStore.displayName)
const initial = computed<string>(() => displayName.value.slice(0, 1).toUpperCase())
const roles = computed<string>(() => rolesText(projectStore.myRoles))

async function handleCommand(command: string): Promise<void> {
  switch (command) {
    case 'account':
      await router.push({ name: 'account' })
      break
    case 'settings':
      await router.push({ name: 'settings' })
      break
    case 'logout':
      authStore.logout()
      await router.replace({ name: 'login' })
      break
    default:
      break
  }
}
</script>

<template>
  <el-dropdown trigger="click" @command="handleCommand">
    <button type="button" class="user-menu">
      <span class="user-menu__avatar">{{ initial }}</span>
      <span class="user-menu__meta">
        <span class="user-menu__name elt-ellipsis">{{ displayName }}</span>
        <span class="user-menu__roles elt-ellipsis" :title="roles">{{ roles }}</span>
      </span>
      <el-icon class="user-menu__caret" :size="12"><ArrowDown /></el-icon>
    </button>

    <template #dropdown>
      <el-dropdown-menu>
        <el-dropdown-item command="account" :icon="User">账号与安全</el-dropdown-item>
        <el-dropdown-item command="settings" :icon="Setting">项目设置</el-dropdown-item>
        <el-dropdown-item divided command="logout" :icon="SwitchButton">退出登录</el-dropdown-item>
      </el-dropdown-menu>
    </template>
  </el-dropdown>
</template>

<style scoped>
.user-menu {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  height: 34px;
  padding: 0 8px 0 4px;
  color: var(--elt-text-primary);
  cursor: pointer;
  background: none;
  border: 1px solid transparent;
  border-radius: var(--elt-radius-control);
}

.user-menu:hover {
  background-color: var(--elt-bg-elevated);
  border-color: var(--elt-border-subtle);
}

.user-menu__avatar {
  display: inline-flex;
  flex: none;
  align-items: center;
  justify-content: center;
  width: 26px;
  height: 26px;
  font-size: 12px;
  font-weight: 700;
  color: #fff;
  background-color: var(--elt-accent-soft);
  border: 1px solid var(--elt-accent);
  border-radius: 50%;
}

.user-menu__meta {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  max-width: 150px;
  line-height: 1.2;
}

.user-menu__name {
  max-width: 150px;
  font-size: 12px;
  font-weight: 600;
}

.user-menu__roles {
  max-width: 150px;
  font-size: 10px;
  color: var(--elt-text-muted);
}

.user-menu__caret {
  flex: none;
  color: var(--elt-text-muted);
}
</style>
