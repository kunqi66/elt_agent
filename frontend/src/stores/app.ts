/**
 * 应用外壳状态：侧栏折叠、移动端抽屉、全局阻塞态。
 *
 * 刻意**不**放业务数据。这个 store 只服务于布局，任何页面数据都不该住进来——
 * 否则布局一改动就会牵动业务状态。
 */

import { ref, computed } from 'vue'
import { defineStore } from 'pinia'
import type { MonitorRange } from '@/types'

const COLLAPSE_KEY = 'elt-agent.sidebar-collapsed'
const MONITOR_RANGE_KEY = 'elt-agent.monitor-range'

/** 首屏并发请求计数：>0 时顶部出细进度条；不遮罩内容（fe-01 §7） */
let inflight = 0

export const useAppStore = defineStore('app', () => {
  const sidebarCollapsed = ref<boolean>(localStorage.getItem(COLLAPSE_KEY) === '1')
  const mobileDrawerOpen = ref(false)
  const busyCount = ref(0)
  /** 总览工作台的时间窗；跨页面共享，避免每次进工作台都重置 */
  const monitorRange = ref<MonitorRange>(
    (localStorage.getItem(MONITOR_RANGE_KEY) as MonitorRange | null) ?? '24h',
  )

  const isBusy = computed<boolean>(() => busyCount.value > 0)

  function toggleSidebar(): void {
    sidebarCollapsed.value = !sidebarCollapsed.value
    localStorage.setItem(COLLAPSE_KEY, sidebarCollapsed.value ? '1' : '0')
  }

  function setMobileDrawer(open: boolean): void {
    mobileDrawerOpen.value = open
  }

  function setMonitorRange(range: MonitorRange): void {
    monitorRange.value = range
    localStorage.setItem(MONITOR_RANGE_KEY, range)
  }

  /** 进入/离开阻塞态；成对使用，用计数器而非布尔量以免并发时互相清掉 */
  function enterBusy(): void {
    inflight += 1
    busyCount.value = inflight
  }

  function leaveBusy(): void {
    inflight = Math.max(0, inflight - 1)
    busyCount.value = inflight
  }

  return {
    sidebarCollapsed,
    mobileDrawerOpen,
    monitorRange,
    isBusy,
    toggleSidebar,
    setMobileDrawer,
    setMonitorRange,
    enterBusy,
    leaveBusy,
  }
})

/** 供非组件场景（如 api 包装）读取的折叠态——不经过 store 实例 */
export function readSidebarCollapsed(): boolean {
  return localStorage.getItem(COLLAPSE_KEY) === '1'
}

/** 便于测试重置模块级计数 */
export function resetBusyCounter(): void {
  inflight = 0
}
