/**
 * 敏感内容拦截式过滤（fe-03 §8 / fe-04 §8）。
 *
 * 这是**双层防御的前端一侧**：后端 core/logging 的 redact_processor 是第一层，
 * 这里假设后端可能漏脱敏，对要渲染给用户的文案做一次形态检查，
 * 命中连接串/凭据/身份标识就整条替换为通用提示，而不是局部打码
 * （局部打码容易漏掉相邻片段，整条替换更安全）。
 *
 * 只用于**展示前**的文案，不改动任何要提交给后端的值。
 */

/** 命中任一即视为含敏感形态 */
const SENSITIVE_PATTERNS: ReadonlyArray<{ name: string; re: RegExp }> = [
  // 任何带 scheme 的 URL：http://、mysql://、jdbc:mysql:// 都能覆盖
  { name: 'url', re: /[a-z][a-z0-9+.-]*:\/\//i },
  { name: 'jdbc', re: /jdbc:/i },
  // user:pass@host 形态（凭据内联在连接串里）
  { name: 'credentials', re: /[^\s:@/]+:[^\s:@/]+@/ },
  { name: 'bearer', re: /bearer\s+[A-Za-z0-9._-]{8,}/i },
  // 常见密钥前缀
  { name: 'api_key', re: /\b(sk|pk|ghp|glpat|xox[baprs])-[A-Za-z0-9_-]{8,}/ },
  { name: 'aws_key', re: /\bAKIA[0-9A-Z]{16}\b/ },
  // 中国居民身份证（18 位，末位可为 X）
  { name: 'id_card', re: /\b\d{17}[\dXx]\b/ },
  // 中国大陆手机号
  { name: 'phone', re: /\b1[3-9]\d{9}\b/ },
  // key=value 形态的敏感键
  { name: 'kv_secret', re: /\b(password|passwd|pwd|secret|token|api_?key)\s*[=:]\s*\S{4,}/i },
]

/**
 * 文本是否含敏感形态。
 * 用于断言性检查（如错误文案、日志行）以及决定是否上报 console.error。
 */
export function containsSensitive(text: string | null | undefined): boolean {
  if (text === null || text === undefined || text === '') {
    return false
  }
  return SENSITIVE_PATTERNS.some((p) => p.re.test(text))
}

/** 默认通用提示：不透露原文任何片段，避免「打码后仍可推断」 */
export const SENSITIVE_FALLBACK = '详情含敏感信息，已隐去。请查看后端日志（按 request_id 关联）'

/**
 * 展示前过滤：命中敏感形态则整条替换为 fallback。
 *
 * @param text 后端下发的文案（可能漏脱敏）
 * @param fallback 替换文案；默认通用提示
 * @param context 上报时的定位信息，便于排障（只打日志，不把原文写进日志）
 */
export function redactSensitive(
  text: string | null | undefined,
  fallback: string = SENSITIVE_FALLBACK,
  context?: string,
): string {
  if (text === null || text === undefined || text === '') {
    return ''
  }
  if (!containsSensitive(text)) {
    return text
  }
  // 刻意不把原文写进 console —— 否则「防泄露」的过滤反而成了泄露出口
  console.error(`[mask] 检出疑似未脱敏内容，已整条隐去${context === undefined ? '' : ` @ ${context}`}`)
  return fallback
}

/**
 * 主机掩码：10.0.0.5 → 10.**.**.5，保留首尾段以便定位、隐去网段细节。
 * 只用于展示，不参与任何判定。
 */
export function maskHost(host: string | null | undefined): string {
  if (host === null || host === undefined || host === '') {
    return ''
  }
  const parts = host.split('.')
  if (parts.length !== 4) {
    // 非 IPv4（域名/主机名）：只保留首段
    const segs = host.split(/[.:]/).filter((s) => s !== '')
    if (segs.length <= 1) {
      return host
    }
    return `${segs[0]}.**.**`
  }
  return `${parts[0]}.**.**.${parts[3]}`
}

/**
 * 单值掩码（掩码中间保留首尾各 1 字符）。
 * 仅用于**后端已明确标注可展示的脱敏样本**的兜底渲染；
 * 平台不保存样本原文，因此本函数解不出任何原文。
 */
export function maskMiddle(value: string): string {
  if (value.length <= 2) {
    return '*'.repeat(value.length)
  }
  return `${value.slice(0, 1)}${'*'.repeat(Math.max(1, value.length - 2))}${value.slice(-1)}`
}
