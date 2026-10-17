/**
 * 复制到剪贴板（哈希、request_id、run_id 这类需要贴到别处对账的短文本）。
 *
 * 失败时的降级路径在 utils/download.ts 的 copyText 里：
 * ECS 上若以 http://IP 提供前端属非安全上下文，navigator.clipboard 不可用。
 */

import { ref } from 'vue'
import type { Ref } from 'vue'
import { ElMessage } from '@/utils/feedback'
import { copyText } from '@/utils/download'

/** 反馈高亮的持续时间：够看清，又不至于让图标一直闪 */
const FEEDBACK_MS = 1500

export interface Copy {
  /** 最近一次复制成功的文本；组件据此把图标切成「已复制」 */
  copied: Ref<string | null>
  copy: (text: string, label?: string) => Promise<void>
}

export function useCopy(): Copy {
  const copied = ref<string | null>(null)
  let timer: ReturnType<typeof setTimeout> | null = null

  async function copy(text: string | null | undefined, label = '内容'): Promise<void> {
    if (text === null || text === undefined || text === '') {
      return
    }
    const ok = await copyText(text)
    if (!ok) {
      ElMessage.warning(`${label}复制失败，请手动选择后再复制`)
      return
    }
    copied.value = text
    if (timer !== null) {
      clearTimeout(timer)
    }
    timer = setTimeout(() => {
      copied.value = null
      timer = null
    }, FEEDBACK_MS)
  }

  return { copied, copy }
}
