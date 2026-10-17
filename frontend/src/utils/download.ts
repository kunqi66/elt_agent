/**
 * 文件下载（账本与 Benchmark 导出，fe-07 §6）。
 *
 * 铁律：**前端不在浏览器里拼 CSV/JSON**。列顺序、编码、脱敏口径全部由后端保证，
 * 前端只发起下载请求并透传 Content-Disposition。否则导出文件会与后端口径漂移，
 * 还可能把未脱敏的中间态写进本地文件。
 */

import http from '@/api/client'

/** 从 Content-Disposition 解析文件名；支持 filename*=UTF-8'' 形式 */
function filenameFromDisposition(disposition: string | undefined): string | null {
  if (disposition === undefined || disposition === '') {
    return null
  }
  const star = /filename\*=(?:UTF-8|utf-8)''([^;]+)/i.exec(disposition)
  if (star !== null && star[1] !== undefined) {
    try {
      return decodeURIComponent(star[1])
    } catch {
      return star[1]
    }
  }
  const plain = /filename="?([^";]+)"?/i.exec(disposition)
  return plain !== null && plain[1] !== undefined ? plain[1] : null
}

/** 兜底文件名：{前缀}-{本地时间戳}.{扩展名} */
function fallbackFilename(prefix: string, format: string): string {
  const d = new Date()
  const pad = (n: number): string => String(n).padStart(2, '0')
  const stamp =
    `${d.getFullYear()}${pad(d.getMonth() + 1)}${pad(d.getDate())}` +
    `-${pad(d.getHours())}${pad(d.getMinutes())}${pad(d.getSeconds())}`
  return `${prefix}-${stamp}.${format}`
}

/**
 * 下载后端生成的导出文件。
 *
 * 走裸 `http` 实例而非 httpGet —— 响应体是二进制流，不能被响应拦截器
 * 当信封剥掉。401 静默刷新仍由拦截器负责（错误分支照常抛 BizError）。
 *
 * @param url 相对路径（baseURL 由 client 补全）
 * @param params 查询参数
 * @param opts.filenamePrefix 无 Content-Disposition 时的文件名前缀
 * @param opts.format json | csv，只用于兜底扩展名
 */
export async function downloadFile(
  url: string,
  params: Record<string, unknown>,
  opts: { filenamePrefix: string; format: 'json' | 'csv' },
): Promise<void> {
  const response = await http.get<Blob>(url, {
    params,
    responseType: 'blob',
    // 导出接口返回文件流，没有 JSON 信封，跳过统一错误提示之外的额外处理
    skipErrorToast: false,
  })

  const raw = response as unknown as {
    data: Blob
    headers: Record<string, unknown>
  }
  const disposition = raw.headers['content-disposition']
  const filename =
    filenameFromDisposition(typeof disposition === 'string' ? disposition : undefined) ??
    fallbackFilename(opts.filenamePrefix, opts.format)

  const objectUrl = URL.createObjectURL(raw.data)
  try {
    const anchor = document.createElement('a')
    anchor.href = objectUrl
    anchor.download = filename
    anchor.style.display = 'none'
    document.body.appendChild(anchor)
    anchor.click()
    anchor.remove()
  } finally {
    // 立即释放；click 是同步触发下载的，无需等待
    URL.revokeObjectURL(objectUrl)
  }
}

/** 复制文本到剪贴板；失败时降级为 execCommand（http 非安全上下文无 clipboard API） */
export async function copyText(text: string): Promise<boolean> {
  const nav = navigator as Navigator & { clipboard?: Clipboard }
  if (nav.clipboard !== undefined && typeof nav.clipboard.writeText === 'function') {
    try {
      await nav.clipboard.writeText(text)
      return true
    } catch {
      // 落到下面的降级路径
    }
  }
  try {
    const area = document.createElement('textarea')
    area.value = text
    area.style.position = 'fixed'
    area.style.opacity = '0'
    document.body.appendChild(area)
    area.select()
    const ok = document.execCommand('copy')
    area.remove()
    return ok
  } catch {
    return false
  }
}
