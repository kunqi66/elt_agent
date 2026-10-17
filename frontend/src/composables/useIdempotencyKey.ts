/**
 * 弹窗级幂等键（CLAUDE.md §6 / fe-05 §6）。
 *
 * 为什么不能只靠 client.ts 的自动注入：
 * 请求拦截器是「每个 request config 生成一个 key」。用户在弹窗里点提交、
 * 网络超时、再点一次提交，是**两个不同的 config**，于是拿到两个 key——
 * 服务端会认为这是两次独立意图，创建出两张准备单。这恰恰是幂等键要防的场景。
 *
 * 正确语义：**一次用户意图 = 一个 key**。
 * 弹窗打开时 arm()，期间无论重试多少次都用同一个 key，关闭时 reset()。
 */

import { ref } from 'vue'
import type { Ref } from 'vue'
import type { AxiosRequestConfig } from 'axios'
import { IdempotencyKeyHolder } from '@/utils/uuid'

export interface IdempotencyHandle {
  /** 当前生效的 key；未 arm 时为 null */
  key: Ref<string | null>
  /** 弹窗打开（或首次提交）时调用，返回本次意图的 key */
  arm: () => string
  /** 直接传给 httpPost/httpPut 的 config，带上 Idempotency-Key */
  config: () => AxiosRequestConfig
  /** 弹窗关闭（成功或取消）时调用；下次打开是新意图 */
  reset: () => void
}

export function useIdempotencyKey(): IdempotencyHandle {
  const holder = new IdempotencyKeyHolder()
  const key = ref<string | null>(null)

  function arm(): string {
    const current = holder.ensure()
    key.value = current
    return current
  }

  function config(): AxiosRequestConfig {
    return { headers: { 'Idempotency-Key': arm() } }
  }

  function reset(): void {
    holder.reset()
    key.value = null
  }

  return { key, arm, config, reset }
}
