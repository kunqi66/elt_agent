/**
 * 轮询（fe-02 §5 / fe-04 §5 / fe-06 §5）。
 *
 * 三条纪律，缺一条就会变成「隐形的流量放大器」：
 * 1. **终态即停**：调用方通过 `shouldStop` 声明停止条件（如 run 进入 succeeded/failed）。
 *    没有这个判据的轮询会永远打下去。
 * 2. **切后台即停**：标签页不可见时暂停。演示时没人看的那一页不该占着并发。
 * 3. **不重入**：上一次请求未返回就跳过这一拍，避免慢响应堆积成雪崩。
 */

import { onScopeDispose, ref, watch } from 'vue'
import type { Ref } from 'vue'

export interface UsePollingOptions {
  /** 间隔毫秒 */
  intervalMs: number
  /**
   * 是否继续轮询的判据。每次 tick 后求值；
   * 返回 false 则停止（并触发 onStop）。
   */
  shouldStop?: () => boolean
  /** 立即执行一次；默认 true */
  immediate?: boolean
  /** 页面不可见时暂停；默认 true */
  pauseWhenHidden?: boolean
}

export interface Polling {
  /** 是否正在轮询 */
  active: Ref<boolean>
  /** 连续失败次数；调用方可用它做「后端挂了就停」的降级 */
  failureCount: Ref<number>
  start: () => void
  stop: () => void
  /** 手动跑一拍（不改变轮询状态） */
  tick: () => Promise<void>
}

/**
 * @param task 每一拍执行的动作
 */
export function usePolling(
  task: () => Promise<void>,
  options: UsePollingOptions,
): Polling {
  const interval = Math.max(1000, options.intervalMs)
  const active = ref(false)
  const failureCount = ref(0)

  let timer: ReturnType<typeof setTimeout> | null = null
  /** 重入保护：上一拍未结束时不开启下一拍 */
  let running = false
  let stopped = false

  function clear(): void {
    if (timer !== null) {
      clearTimeout(timer)
      timer = null
    }
  }

  function schedule(): void {
    clear()
    if (!active.value || stopped) {
      return
    }
    timer = setTimeout(() => {
      void tick()
    }, interval)
  }

  async function tick(): Promise<void> {
    if (running) {
      // 上一拍还没回来：跳过，等下一拍
      schedule()
      return
    }
    if (options.pauseWhenHidden !== false && document.hidden) {
      schedule()
      return
    }
    running = true
    try {
      await task()
      failureCount.value = 0
    } catch {
      failureCount.value += 1
      // 错误提示由 client 拦截器统一给出，这里只累计次数
    } finally {
      running = false
    }

    if (options.shouldStop?.() === true) {
      stop()
      return
    }
    schedule()
  }

  function start(): void {
    if (active.value) {
      return
    }
    stopped = false
    active.value = true
    if (options.immediate !== false) {
      void tick()
    } else {
      schedule()
    }
  }

  function stop(): void {
    stopped = true
    active.value = false
    clear()
  }

  // 切回前台立刻补一拍，而不是干等一个完整间隔——
  // 用户回到页面时看到的是最新数据，而不是「上一次看到的旧值再晾 N 秒」
  function onVisibilityChange(): void {
    if (document.hidden || !active.value) {
      return
    }
    void tick()
  }

  if (options.pauseWhenHidden !== false) {
    document.addEventListener('visibilitychange', onVisibilityChange)
  }

  onScopeDispose(() => {
    stop()
    if (options.pauseWhenHidden !== false) {
      document.removeEventListener('visibilitychange', onVisibilityChange)
    }
  })

  return { active, failureCount, start, stop, tick }
}

/**
 * 带超时的轮询包装：超过 maxDurationMs 强制停止。
 * 用于「提交后等待后端推进」这类有明确上限的场景——避免无限等待。
 */
export function usePollingWithDeadline(
  task: () => Promise<void>,
  options: UsePollingOptions & { maxDurationMs: number; onDeadline?: () => void },
): Polling {
  const started = ref(0)
  const polling = usePolling(task, {
    ...options,
    shouldStop: () => {
      if (started.value > 0 && Date.now() - started.value > options.maxDurationMs) {
        options.onDeadline?.()
        return true
      }
      return options.shouldStop?.() ?? false
    },
  })

  // 记录起点：start() 之后才算，避免把「页面闲置时间」算进预算
  watch(polling.active, (isActive) => {
    if (isActive) {
      started.value = Date.now()
    }
  })

  return polling
}
