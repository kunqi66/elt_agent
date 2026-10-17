/**
 * 一次性异步动作（提交、冻结、审批、回滚…）。
 *
 * 存在的理由：这些动作散落在十几个弹窗里，如果每处各写一遍
 * `loading / error / disabled / 成功提示`，很快就会出现在某些弹窗里
 * **按钮没置灰导致重复提交**。集中一处，就不会漏。
 *
 * 注意：幂等键由 api/client.ts 的请求拦截器自动带，
 * 这里的「防重入」是体验层的第二道保险，不替代服务端幂等。
 */

import { ref } from 'vue'
import type { Ref } from 'vue'
import { ElMessage } from '@/utils/feedback'
import { toDisplayMessage } from '@/utils/errors'

export interface UseAsyncTaskOptions {
  /** 成功后是否弹 success 提示；给了才弹 */
  successMessage?: string
  /** 成功后回调（关弹窗、刷新列表…） */
  onSuccess?: () => void
  /** 失败后回调；默认什么都不做（拦截器已提示过） */
  onError?: (message: string) => void
  /** 失败时是否再弹一次；默认 false —— 拦截器已经弹过了 */
  toastOnError?: boolean
}

export interface AsyncTask<A extends unknown[]> {
  loading: Ref<boolean>
  /** 最近一次失败文案；渲染在弹窗底部做行内提示 */
  error: Ref<string | null>
  /** 是否成功完成过（用于「重试」按钮的文案切换） */
  done: Ref<boolean>
  run: (...args: A) => Promise<boolean>
  reset: () => void
}

/**
 * @param task 实际动作；抛错即视为失败
 */
export function useAsyncTask<A extends unknown[]>(
  task: (...args: A) => Promise<void>,
  options: UseAsyncTaskOptions = {},
): AsyncTask<A> {
  const loading = ref(false)
  const error = ref<string | null>(null)
  const done = ref(false)

  async function run(...args: A): Promise<boolean> {
    if (loading.value) {
      // 防重入：双击提交按钮时第二次直接忽略
      return false
    }
    loading.value = true
    error.value = null
    try {
      await task(...args)
      done.value = true
      if (options.successMessage !== undefined) {
        ElMessage.success(options.successMessage)
      }
      options.onSuccess?.()
      return true
    } catch (e) {
      const message = toDisplayMessage(e)
      error.value = message
      if (options.toastOnError === true) {
        ElMessage.error(message)
      }
      options.onError?.(message)
      return false
    } finally {
      loading.value = false
    }
  }

  function reset(): void {
    loading.value = false
    error.value = null
    done.value = false
  }

  return { loading, error, done, run, reset }
}
