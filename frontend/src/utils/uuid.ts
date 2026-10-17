/**
 * 幂等键生成。
 *
 * fe-05 §10 / fe-08 §10 已登记风险：ECS 上若以 `http://IP` 提供前端，
 * 属非安全上下文，`crypto.randomUUID()` 不可用。这里做降级，
 * 保证「生成 UUID v4」这条业务语义在任何上下文都成立。
 *
 * 注意：幂等键不是安全令牌，降级实现用 Math.random 拼装是可接受的——
 * 它的作用是「同一次用户动作复用同一个 key」，不承担不可预测性要求。
 */

/** 优先用原生实现；不可用时用 getRandomValues；再退化为 Math.random */
export function uuidv4(): string {
  const cryptoObj: Crypto | undefined = globalThis.crypto

  if (cryptoObj !== undefined && typeof cryptoObj.randomUUID === 'function') {
    return cryptoObj.randomUUID()
  }

  const bytes = new Uint8Array(16)

  if (cryptoObj !== undefined && typeof cryptoObj.getRandomValues === 'function') {
    cryptoObj.getRandomValues(bytes)
  } else {
    for (let i = 0; i < 16; i += 1) {
      bytes[i] = Math.floor(Math.random() * 256)
    }
  }

  // 按 RFC 4122 置版本位与变体位
  bytes[6] = ((bytes[6] ?? 0) & 0x0f) | 0x40
  bytes[8] = ((bytes[8] ?? 0) & 0x3f) | 0x80

  const hex: string[] = []
  for (let i = 0; i < 16; i += 1) {
    hex.push((bytes[i] ?? 0).toString(16).padStart(2, '0'))
  }
  const s = hex.join('')
  return `${s.slice(0, 8)}-${s.slice(8, 12)}-${s.slice(12, 16)}-${s.slice(16, 20)}-${s.slice(20)}`
}

/**
 * 幂等键持有者：一次用户动作 = 一个 key。
 *
 * 语义（fe-05 §6）：
 * - 弹窗打开时首次生成；
 * - 同一弹窗内的重复点击、网络失败后的「重试」复用同一 key；
 * - 弹窗关闭（成功或取消）后重置，下次打开是新 key —— 那是新的业务意图。
 */
export class IdempotencyKeyHolder {
  private key: string | null = null

  /** 取当前 key，没有就生成一个 */
  ensure(): string {
    this.key ??= uuidv4()
    return this.key
  }

  /** 当前 key；未生成时为 null（用于断言/调试） */
  peek(): string | null {
    return this.key
  }

  /** 重置：弹窗关闭、动作完成或取消时调用 */
  reset(): void {
    this.key = null
  }
}
