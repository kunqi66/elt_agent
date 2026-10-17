import { createRouter, createWebHistory } from 'vue-router'
import type { RouteRecordRaw } from 'vue-router'
import DefaultLayout from '@/layouts/DefaultLayout.vue'
import { useAuthStore } from '@/stores/auth'
import { useProjectStore } from '@/stores/project'
import { hasAnyRole } from '@/utils/enums'
import type { RoleName } from '@/types'

declare module 'vue-router' {
  interface RouteMeta {
    /** 免登录页面（登录页） */
    public?: boolean
    /** 页面标题；用于 document.title 与面包屑末级 */
    title?: string
    /**
     * 可见性过滤用的角色集合（fe-01 §3.4）。
     *
     * ⚠️ 这**不是**安全边界。它只决定「菜单里显不显示、直接输 URL 会不会被弹走」。
     * 真正的鉴权在后端 `role_assignment`，前端拿到的 403 只决定展示形态。
     * 任何写操作都必须由后端再校验一次——把这里的判定当成授权会直接违反 CLAUDE.md §5.8。
     */
    requiredRoles?: RoleName[]
    /** 侧边栏高亮项；详情页用它指向所属列表 */
    activeMenu?: string
    /** 面包屑父级链（不含当前页） */
    breadcrumb?: { name: string; title: string }[]
  }
}

const routes: RouteRecordRaw[] = [
  {
    path: '/login',
    name: 'login',
    component: () => import('@/views/LoginView.vue'),
    meta: { public: true, title: '登录' },
  },
  {
    path: '/',
    component: DefaultLayout,
    redirect: { name: 'dashboard' },
    children: [
      {
        path: 'dashboard',
        name: 'dashboard',
        component: () => import('@/views/dashboard/DashboardView.vue'),
        meta: { title: '总览工作台' },
      },

      // ---------------- 数据源（fe-03） ----------------
      {
        path: 'datasources',
        name: 'datasource-list',
        component: () => import('@/views/datasource/DataSourceListView.vue'),
        meta: { title: '数据源' },
      },
      {
        // 新建与编辑共用同一表单组件：两边字段完全一致，
        // 拆成两个组件必然出现「编辑页少校验一条」的漂移。
        path: 'datasources/new',
        name: 'datasource-create',
        component: () => import('@/views/datasource/DataSourceEditView.vue'),
        meta: {
          title: '新建数据源',
          requiredRoles: ['maker'],
          activeMenu: 'datasource-list',
          breadcrumb: [{ name: 'datasource-list', title: '数据源' }],
        },
      },
      {
        path: 'datasources/:id/edit',
        name: 'datasource-edit',
        component: () => import('@/views/datasource/DataSourceEditView.vue'),
        meta: {
          title: '编辑数据源',
          requiredRoles: ['maker'],
          activeMenu: 'datasource-list',
          breadcrumb: [{ name: 'datasource-list', title: '数据源' }],
        },
      },
      {
        path: 'datasources/:id',
        name: 'datasource-detail',
        component: () => import('@/views/datasource/DataSourceDetailView.vue'),
        meta: {
          title: '数据源详情',
          activeMenu: 'datasource-list',
          breadcrumb: [{ name: 'datasource-list', title: '数据源' }],
        },
      },

      // ---------------- Pipeline Studio（fe-04） ----------------
      {
        path: 'pipelines',
        name: 'pipeline-list',
        component: () => import('@/views/pipeline/PipelineListView.vue'),
        meta: { title: '管线', requiredRoles: ['maker'] },
      },
      {
        // 生成与编辑都在 Studio 内完成，不另设 /pipelines/new 之外的入口；
        // thread_id 由后端在创建生成时返回并写进快照，刷新后可原样恢复。
        path: 'pipelines/:id/studio',
        name: 'pipeline-studio',
        component: () => import('@/views/pipeline/PipelineStudioView.vue'),
        meta: {
          title: 'Pipeline Studio',
          requiredRoles: ['maker'],
          activeMenu: 'pipeline-list',
          breadcrumb: [{ name: 'pipeline-list', title: '管线' }],
        },
      },

      // ---------------- 审批中心（fe-05） ----------------
      {
        path: 'approvals',
        name: 'preparation-list',
        component: () => import('@/views/approval/PreparationListView.vue'),
        meta: {
          title: '审批中心',
          requiredRoles: ['checker_data', 'checker_security', 'auditor'],
        },
      },
      {
        path: 'approvals/:id',
        name: 'preparation-detail',
        component: () => import('@/views/approval/PreparationDetailView.vue'),
        meta: {
          title: '准备单详情',
          requiredRoles: ['checker_data', 'checker_security', 'auditor'],
          activeMenu: 'preparation-list',
          breadcrumb: [{ name: 'preparation-list', title: '审批中心' }],
        },
      },

      // ---------------- 运行中心（fe-06） ----------------
      {
        path: 'runs',
        name: 'run-list',
        component: () => import('@/views/run/RunListView.vue'),
        meta: { title: '运行中心', requiredRoles: ['operator', 'auditor'] },
      },
      {
        path: 'runs/:id',
        name: 'run-detail',
        component: () => import('@/views/run/RunDetailView.vue'),
        meta: {
          title: '运行详情',
          requiredRoles: ['operator', 'auditor'],
          activeMenu: 'run-list',
          breadcrumb: [{ name: 'run-list', title: '运行中心' }],
        },
      },

      // ---------------- 安全治理（fe-07） ----------------
      {
        path: 'governance',
        name: 'governance',
        component: () => import('@/views/governance/GovernanceView.vue'),
        meta: { title: '安全治理', requiredRoles: ['checker_security', 'auditor'] },
      },

      // ---------------- 项目设置（fe-08） ----------------
      {
        // 不设 requiredRoles：能进到这一页的人至少是项目成员，
        // 页面内部再按 myRoles 决定「成员/角色」标签页是否可编辑，
        // 写操作的最终裁决仍在后端（fe-08 §10 第 6 条）。
        path: 'settings',
        name: 'settings',
        component: () => import('@/views/settings/SettingsView.vue'),
        meta: { title: '项目设置' },
      },

      // ---------------- 账号 ----------------
      {
        path: 'account',
        name: 'account',
        component: () => import('@/views/settings/AccountView.vue'),
        meta: { title: '账号与安全' },
      },
    ],
  },
  {
    path: '/403',
    name: 'forbidden',
    component: () => import('@/views/error/ForbiddenView.vue'),
    meta: { public: true, title: '无权访问' },
  },
  {
    path: '/404',
    name: 'not-found',
    component: () => import('@/views/error/NotFoundView.vue'),
    meta: { public: true, title: '页面不存在' },
  },
  {
    // 兜底到 404 而不是首页：静默跳首页会让用户以为自己点错了，
    // 而实际是链接失效——这两种情况的处理方式完全不同。
    path: '/:pathMatch(.*)*',
    redirect: { name: 'not-found' },
  },
]

const router = createRouter({
  history: createWebHistory(import.meta.env.BASE_URL),
  routes,
  scrollBehavior: () => ({ top: 0 }),
})

router.beforeEach(async (to) => {
  const authStore = useAuthStore()

  // 1. 登录态
  if (to.meta.public !== true && !authStore.isAuthenticated) {
    return { name: 'login', query: { redirect: to.fullPath } }
  }
  if (to.name === 'login' && authStore.isAuthenticated) {
    return { name: 'dashboard' }
  }
  if (to.meta.public === true) {
    return true
  }

  // 2. 角色可见性过滤。
  //    先确保 profile 到位——刷新页面时 store 里只有 token，profile 为空，
  //    此时直接比对角色会把有权用户误判成 403（fe-01 §5.3 第 3 条）。
  const required = to.meta.requiredRoles
  if (required !== undefined && required.length > 0) {
    if (authStore.profile === null) {
      try {
        await authStore.loadProfile()
      } catch {
        // profile 拉不到（token 失效等）→ 交回登录页，由登录流程重新建立会话
        return { name: 'login', query: { redirect: to.fullPath } }
      }
    }

    const projectStore = useProjectStore()
    try {
      await projectStore.ensureLoaded()
    } catch {
      return { name: 'login', query: { redirect: to.fullPath } }
    }

    if (!hasAnyRole(projectStore.myRoles, required)) {
      // 明确跳 403，不静默回首页：静默跳转会表现为「点了没反应」，
      // 用户会反复点击并怀疑系统坏了（fe-01 §5.3 第 4 条）。
      return { name: 'forbidden' }
    }
  }

  return true
})

router.afterEach((to) => {
  const title = to.meta.title
  document.title = title === undefined ? 'ELT-Agent 数据集成平台' : `${title} · ELT-Agent`
})

export default router
