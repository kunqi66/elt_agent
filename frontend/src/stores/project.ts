/**
 * 项目上下文（fe-08 / be-02）。
 *
 * 全站所有业务请求都要带 project_id，所以这个 store 是**所有其他业务 store 的前置**：
 * 任何页面在 projectStore.ready 之前拉数据，都会拿到错误项目的列表。
 *
 * 两条纪律：
 * 1. **角色来自 GET /auth/me**，不从本地缓存反推，也不解析 JWT。
 * 2. **切换项目要清空其他 store 的缓存**——否则会把 A 项目的列表短暂显示在 B 项目里。
 */

import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import * as projectApi from '@/api/project'
import { ROLE_NAMES } from '@/utils/enums'
import { toDisplayMessage } from '@/utils/errors'
import { useAuthStore } from './auth'
import type { Env, PersistedProject, ProjectRead, RoleName } from '@/types'

const PROJECT_KEY = 'elt-agent.project'

function readPersistedProject(): PersistedProject | null {
  const raw = localStorage.getItem(PROJECT_KEY)
  if (raw === null) {
    return null
  }
  try {
    const parsed = JSON.parse(raw) as Partial<PersistedProject>
    if (typeof parsed.id !== 'number') {
      return null
    }
    return { id: parsed.id, name: parsed.name ?? '', env: (parsed.env ?? 'dev') as Env }
  } catch {
    return null
  }
}

/** 把后端下发的 string[] 收窄为已知角色；未知取值直接丢弃（后端新增角色时前端不受影响） */
function toRoleNames(raw: readonly string[] | undefined): RoleName[] {
  if (raw === undefined) {
    return []
  }
  return ROLE_NAMES.filter((r) => raw.includes(r))
}

export const useProjectStore = defineStore('project', () => {
  const authStore = useAuthStore()

  const projects = ref<ProjectRead[]>([])
  const currentProjectId = ref<number | null>(readPersistedProject()?.id ?? null)
  const loading = ref(false)
  const error = ref<string | null>(null)
  /** 项目列表是否已成功加载过；路由守卫据此决定要不要先加载再放行 */
  const ready = ref(false)

  const currentProject = computed<ProjectRead | null>(
    () => projects.value.find((p) => p.id === currentProjectId.value) ?? null,
  )

  /**
   * 当前项目内我的角色。
   * 取 /auth/me 的 projects[].roles —— 那是服务端按 role_assignment 时间窗算好的结论。
   */
  const myRoles = computed<RoleName[]>(() => {
    const brief = authStore.profile?.projects.find((p) => p.id === currentProjectId.value)
    return toRoleNames(brief?.roles)
  })

  const currentEnv = computed<Env>(() => currentProject.value?.env ?? 'dev')
  /** 生产环境标识：顶部红条与危险操作的二次确认都依赖它 */
  const isProd = computed<boolean>(() => currentEnv.value === 'prod')
  const canManageMembers = computed<boolean>(() => currentProject.value?.can_manage_members === true)
  const canManageRoles = computed<boolean>(() => currentProject.value?.can_manage_roles === true)
  const hasProject = computed<boolean>(() => currentProjectId.value !== null)

  /** 持久化时只存 id/name/env：角色可能随时被回收，缓存下来只会误导用户 */
  function persist(project: ProjectRead): void {
    localStorage.setItem(
      PROJECT_KEY,
      JSON.stringify({ id: project.id, name: project.name, env: project.env } satisfies PersistedProject),
    )
  }

  function selectProject(id: number): void {
    const target = projects.value.find((p) => p.id === id)
    if (target === undefined) {
      return
    }
    currentProjectId.value = id
    persist(target)
  }

  /**
   * 加载项目列表并决定当前项目。
   * 选择顺序：本地记录的 id → 画像里的第一个项目 → 列表第一个。
   * 本地记录的 id 已失效（被移除成员）时静默回退，不报警——这是正常情况。
   */
  async function loadProjects(): Promise<ProjectRead[]> {
    loading.value = true
    error.value = null
    try {
      const list = await projectApi.fetchProjects()
      projects.value = list

      const persisted = readPersistedProject()
      const candidates = [
        persisted === null ? null : list.find((p) => p.id === persisted.id),
        list.find((p) => p.id === authStore.profile?.projects[0]?.id),
        list[0],
      ]
      const chosen = candidates.find((c): c is ProjectRead => c !== undefined && c !== null) ?? null

      if (chosen !== null) {
        currentProjectId.value = chosen.id
        persist(chosen)
      } else {
        currentProjectId.value = null
      }
      ready.value = true
      return list
    } catch (e) {
      error.value = toDisplayMessage(e)
      throw e
    } finally {
      loading.value = false
    }
  }

  /** 供布局在登录后调用；已有项目上下文时不重复请求 */
  async function ensureLoaded(): Promise<void> {
    if (ready.value) {
      return
    }
    await loadProjects()
  }

  function refreshCurrentProject(): void {
    const target = projects.value.find((p) => p.id === currentProjectId.value)
    if (target !== undefined) {
      persist(target)
    }
  }

  return {
    projects,
    currentProjectId,
    currentProject,
    myRoles,
    currentEnv,
    isProd,
    canManageMembers,
    canManageRoles,
    hasProject,
    loading,
    error,
    ready,
    loadProjects,
    ensureLoaded,
    selectProject,
    refreshCurrentProject,
  }
})
