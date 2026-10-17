/**
 * 侧边栏导航配置（fe-01 §3.4）。
 *
 * 独立成模块而不是写死在 AppSidebar 里：面包屑、404 页的「你可能想去」
 * 都复用同一份菜单，写在组件里就得从组件往外导出，依赖方向会变别扭。
 *
 * `icon` 是**字符串键**而不是组件本身：这样本文件保持纯数据、可被单测直接断言，
 * 也避免了「菜单文件 import 一堆图标组件」把图标打进首屏包。
 * 键到组件的映射由 AppSidebar 的白名单负责，未登记的键会退化成默认图标而不是崩掉。
 */
import type { RoleName } from '@/types'
import { hasAnyRole } from '@/utils/enums'

export interface NavItem {
  /** 目标路由 name；与 router/index.ts 一一对应 */
  name: string
  title: string
  /** 图标键，见 AppSidebar 的 ICON_MAP */
  icon: string
  /** 缺省 = 所有登录用户可见 */
  requiredRoles?: RoleName[]
  /** 置灰但仍显示（如「即将开放」）；置灰项不可点击 */
  disabled?: boolean
}

export interface NavGroup {
  key: string
  title: string
  items: NavItem[]
}

export const NAV_GROUPS: readonly NavGroup[] = [
  {
    key: 'overview',
    title: '概览',
    items: [{ name: 'dashboard', title: '总览工作台', icon: 'dashboard' }],
  },
  {
    key: 'asset',
    title: '资产',
    items: [
      { name: 'datasource-list', title: '数据源', icon: 'datasource' },
      { name: 'pipeline-list', title: '管线', icon: 'pipeline', requiredRoles: ['maker'] },
    ],
  },
  {
    key: 'governance',
    title: '治理',
    items: [
      {
        name: 'preparation-list',
        title: '审批中心',
        icon: 'approval',
        requiredRoles: ['checker_data', 'checker_security', 'auditor'],
      },
      { name: 'run-list', title: '运行中心', icon: 'run', requiredRoles: ['operator', 'auditor'] },
      {
        name: 'governance',
        title: '安全治理',
        icon: 'policy',
        requiredRoles: ['checker_security', 'auditor'],
      },
    ],
  },
  {
    key: 'system',
    title: '系统',
    items: [{ name: 'settings', title: '项目设置', icon: 'settings' }],
  },
]

/**
 * 按角色过滤菜单。
 *
 * 过滤掉的是**整组为空**的组——留一个空标题在侧边栏里很难看，
 * 也会让用户以为加载失败了。
 */
export function visibleNavGroups(roles: readonly string[]): NavGroup[] {
  const result: NavGroup[] = []
  for (const group of NAV_GROUPS) {
    const items = group.items.filter((item) => hasAnyRole(roles, item.requiredRoles))
    if (items.length > 0) {
      result.push({ key: group.key, title: group.title, items })
    }
  }
  return result
}

/** 当前路由应高亮的菜单项：详情页通过 meta.activeMenu 指回自己的列表 */
export function resolveActiveMenu(routeName: string | null | undefined, activeMenu?: string): string {
  return activeMenu ?? routeName ?? ''
}
