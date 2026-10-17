/**
 * 纯展示格式化（fe-02 §6.1）。
 *
 * 边界：只做单位换算与百分比格式化，**不做任何聚合推算**。
 * 成功率、拒绝率、平均吞吐一律用后端返回值；前端不拿分子分母自己除。
 *
 * 任何 null / undefined / NaN 一律显示 '—'，绝不显示 'NaN'。
 * 不引 dayjs 等外部日期库，避免为一个格式化引入运行时依赖。
 */

const DASH = '—'

/* ------------------------------------------------------------------ */
/* 基础                                                                */
/* ------------------------------------------------------------------ */

function isFiniteNumber(v: unknown): v is number {
  return typeof v === 'number' && Number.isFinite(v)
}

/** 空值兜底：非有限数值返回 '—' */
export function formatOrDash(v: number | null | undefined, fmt: (n: number) => string): string {
  if (!isFiniteNumber(v)) {
    return DASH
  }
  return fmt(v)
}

/** 千分位整数 */
export function formatInt(v: number | null | undefined): string {
  return formatOrDash(v, (n) => Math.round(n).toLocaleString('en-US'))
}

/* ------------------------------------------------------------------ */
/* 行数与字节                                                          */
/* ------------------------------------------------------------------ */

/**
 * 行数分级：≥1e8 → 亿；≥1e4 → 万；否则千分位。
 * 例：123456789 → '1.23 亿行'
 */
export function formatRows(v: number | null | undefined): string {
  return formatOrDash(v, (n) => {
    if (Math.abs(n) >= 1e8) {
      return `${(n / 1e8).toFixed(2)} 亿行`
    }
    if (Math.abs(n) >= 1e4) {
      return `${(n / 1e4).toFixed(2)} 万行`
    }
    return `${Math.round(n).toLocaleString('en-US')} 行`
  })
}

const BYTE_UNITS: ReadonlyArray<{ limit: number; suffix: string }> = [
  { limit: 1024 ** 4, suffix: 'TiB' },
  { limit: 1024 ** 3, suffix: 'GiB' },
  { limit: 1024 ** 2, suffix: 'MiB' },
  { limit: 1024, suffix: 'KiB' },
]

/** 字节按 1024 进制、一位小数。例：5368709120 → '5.0 GiB' */
export function formatBytes(v: number | null | undefined): string {
  return formatOrDash(v, (n) => {
    const abs = Math.abs(n)
    for (const unit of BYTE_UNITS) {
      if (abs >= unit.limit) {
        return `${(n / unit.limit).toFixed(1)} ${unit.suffix}`
      }
    }
    return `${Math.round(n)} B`
  })
}

/* ------------------------------------------------------------------ */
/* 比率与百分比                                                        */
/* ------------------------------------------------------------------ */

/** numeric(6,4) → 百分比两位小数。例：0.0321 → '3.21%'；null → '—' */
export function formatRate(v: number | null | undefined): string {
  return formatOrDash(v, (n) => `${(n * 100).toFixed(2)}%`)
}

/**
 * 成功率一类「分母可能为 0」的比率。
 * fe-02 §6.1：无运行记录时显示 '—'，**不显示 0%**（会被误读成「全部失败」）。
 */
export function formatRateOfTotal(
  rate: number | null | undefined,
  total: number | null | undefined,
): string {
  if (total === null || total === undefined || total === 0) {
    return DASH
  }
  return formatRate(rate)
}

/** 预算进度条用的百分比：0.5 → '50.00%' */
export function formatRatioPercent(v: number | null | undefined, digits = 2): string {
  return formatOrDash(v, (n) => `${(n * 100).toFixed(digits)}%`)
}

/** 吞吐：行/秒，保留一位小数 */
export function formatThroughput(v: number | null | undefined): string {
  return formatOrDash(v, (n) => `${n.toLocaleString('en-US', { maximumFractionDigits: 1 })} 行/秒`)
}

/** 放大比：1.5 → '1.50×' */
export function formatAmplification(v: number | null | undefined): string {
  return formatOrDash(v, (n) => `${n.toFixed(2)}×`)
}

/* ------------------------------------------------------------------ */
/* 时长                                                                */
/* ------------------------------------------------------------------ */

/** 秒 → '1h 23m 45s'；不足 1 分钟只显示秒；空值 '—' */
export function formatDuration(sec: number | null | undefined): string {
  return formatOrDash(sec, (n) => {
    const total = Math.max(0, Math.floor(n))
    const h = Math.floor(total / 3600)
    const m = Math.floor((total % 3600) / 60)
    const s = total % 60
    if (h > 0) {
      return `${h}h ${m}m ${s}s`
    }
    if (m > 0) {
      return `${m}m ${s}s`
    }
    return `${s}s`
  })
}

/** 毫秒 → '3 分 12 秒'（剩余有效期这类短时长文案） */
export function formatRemaining(ms: number | null | undefined): string {
  if (!isFiniteNumber(ms)) {
    return DASH
  }
  if (ms <= 0) {
    return '已过期'
  }
  const total = Math.floor(ms / 1000)
  const d = Math.floor(total / 86400)
  const h = Math.floor((total % 86400) / 3600)
  const m = Math.floor((total % 3600) / 60)
  if (d > 0) {
    return `${d} 天 ${h} 小时`
  }
  if (h > 0) {
    return `${h} 小时 ${m} 分`
  }
  return `${m} 分 ${total % 60} 秒`
}

/** 倒计时 mm:ss / hh:mm:ss */
export function formatCountdown(ms: number | null | undefined): string {
  if (!isFiniteNumber(ms) || ms <= 0) {
    return '00:00'
  }
  const total = Math.floor(ms / 1000)
  const h = Math.floor(total / 3600)
  const m = Math.floor((total % 3600) / 60)
  const s = total % 60
  const pad = (x: number): string => String(x).padStart(2, '0')
  return h > 0 ? `${pad(h)}:${pad(m)}:${pad(s)}` : `${pad(m)}:${pad(s)}`
}

/* ------------------------------------------------------------------ */
/* 时间                                                                */
/* ------------------------------------------------------------------ */

function pad2(n: number): string {
  return String(n).padStart(2, '0')
}

function pad3(n: number): string {
  return String(n).padStart(3, '0')
}

function parseDate(iso: string | null | undefined): Date | null {
  if (iso === null || iso === undefined || iso === '') {
    return null
  }
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? null : d
}

/** ISO8601 → 本地时区 'YYYY-MM-DD HH:mm:ss'；非法值 '—' */
export function formatDateTime(iso: string | null | undefined): string {
  const d = parseDate(iso)
  if (d === null) {
    return DASH
  }
  return (
    `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())} ` +
    `${pad2(d.getHours())}:${pad2(d.getMinutes())}:${pad2(d.getSeconds())}`
  )
}

/** 账本事件用的毫秒精度：'YYYY-MM-DD HH:mm:ss.SSS'（fe-07 §6） */
export function formatDateTimeMs(iso: string | null | undefined): string {
  const d = parseDate(iso)
  if (d === null) {
    return DASH
  }
  return `${formatDateTime(iso)}.${pad3(d.getMilliseconds())}`
}

/** 只到日：'YYYY-MM-DD' */
export function formatDate(iso: string | null | undefined): string {
  const d = parseDate(iso)
  if (d === null) {
    return DASH
  }
  return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`
}

/** 悬停展示 UTC，避免跨时区对账时误读 */
export function formatUtc(iso: string | null | undefined): string {
  const d = parseDate(iso)
  if (d === null) {
    return DASH
  }
  return `${d.toISOString().slice(0, 19).replace('T', ' ')} UTC`
}

/** 距离某时刻的剩余毫秒；无 expires_at 返回 null（= 永不过期） */
export function remainingMs(expiresAt: string | null | undefined): number | null {
  const d = parseDate(expiresAt)
  if (d === null) {
    return null
  }
  return d.getTime() - Date.now()
}

/* ------------------------------------------------------------------ */
/* 指纹与标识                                                          */
/* ------------------------------------------------------------------ */

/** 取前 8 位，用于列表里的指纹缩略 */
export function hashShort(hash: string | null | undefined): string {
  if (hash === null || hash === undefined || hash === '') {
    return DASH
  }
  return hash.slice(0, 8)
}

/** 前 8 + 后 8，中间省略；短于 20 位则原样返回（fe-01 §6.4） */
export function truncateHash(hash: string | null | undefined): string {
  if (hash === null || hash === undefined || hash === '') {
    return DASH
  }
  if (hash.length <= 20) {
    return hash
  }
  return `${hash.slice(0, 8)}…${hash.slice(-8)}`
}

/** 长文本表格列截断，配合 title 展示全文 */
export function truncate(text: string | null | undefined, max = 32): string {
  if (text === null || text === undefined || text === '') {
    return DASH
  }
  return text.length > max ? `${text.slice(0, max)}…` : text
}

/** 空字符串按空值处理，避免表格里出现「有值但看起来是空的」单元格 */
export function textOrDash(text: string | null | undefined): string {
  if (text === null || text === undefined || text.trim() === '') {
    return DASH
  }
  return text
}

export { DASH }
