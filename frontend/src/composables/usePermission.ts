/**
 * 角色可见性（fe-01 §1 / fe-08 §8）。
 *
 * **这不是权限系统**。它只决定「这个入口要不要渲染出来」，
 * 让用户不至于点进去才发现没权限。真正的鉴权在后端 role_assignment 校验 +
 * DB 约束，前端任何判断都不构成安全边界。
 *
 * 反面教材（禁止）：把 `canApprove` 用来决定「提交按钮发不发请求」。
 * 那样一旦前端逻辑被绕过，服务端就成了唯一但未被测试的防线。
 */

import { computed } from 'vue'
import type { ComputedRef } from 'vue'
import { useProjectStore } from '@/stores/project'
import { hasAnyRole } from '@/utils/enums'
import type { RoleName } from '@/types'

export interface Permission {
  /** 当前项目内我的角色（来自 GET /auth/me，前端不解析 JWT） */
  roles: ComputedRef<RoleName[]>
  /** 是否持有其中任一角色 */
  hasRole: (...required: RoleName[]) => boolean
  /** 用于路由 meta.requiredRoles / 菜单项的角色过滤 */
  canSee: (required: readonly RoleName[] | undefined) => boolean
  isMaker: ComputedRef<boolean>
  isCheckerData: ComputedRef<boolean>
  isCheckerSecurity: ComputedRef<boolean>
  isOperator: ComputedRef<boolean>
  isAuditor: ComputedRef<boolean>
  /** 数据槽与安全槽的「双槽兼任」提示：P1/P2 由后端拒绝，前端只做预告 */
  hasBothCheckerSlots: ComputedRef<boolean>
}

export function usePermission(): Permission {
  const projectStore = useProjectStore()

  const roles = computed<RoleName[]>(() => projectStore.myRoles)

  function hasRole(...required: RoleName[]): boolean {
    return hasAnyRole(roles.value, required)
  }

  function canSee(required: readonly RoleName[] | undefined): boolean {
    return hasAnyRole(roles.value, required)
  }

  return {
    roles,
    hasRole,
    canSee,
    isMaker: computed(() => roles.value.includes('maker')),
    isCheckerData: computed(() => roles.value.includes('checker_data')),
    isCheckerSecurity: computed(() => roles.value.includes('checker_security')),
    isOperator: computed(() => roles.value.includes('operator')),
    isAuditor: computed(() => roles.value.includes('auditor')),
    hasBothCheckerSlots: computed(
      () => roles.value.includes('checker_data') && roles.value.includes('checker_security'),
    ),
  }
}
