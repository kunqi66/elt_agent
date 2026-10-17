<script setup lang="ts">
/**
 * 「后端文本」的安全渲染出口（CLAUDE.md §5.2 / §5.7）。
 *
 * 用途很具体：凡是**把后端给的字符串直接显示出来**的地方（错误消息、
 * 连通性检测结果、日志行、诊断结论、元数据样本），都走这里。
 *
 * 为什么不直接插值：
 * 后端理论上已在序列化层脱敏（SecretReference 设计如此），但异常栈、
 * 第三方驱动回报的原文是最容易漏掉的一条通道。前端再拦一道，
 * 代价是一次正则扫描，收益是「就算后端漏了也不会漏到审计截图里」。
 *
 * 检测到敏感特征时**整段替换**而不是打码片段——打码会泄露长度、
 * 位置与格式，对连接串来说这些信息足以推断出部署结构。
 */
import { computed } from 'vue'
import { containsSensitive, SENSITIVE_FALLBACK, redactSensitive, maskHost } from '@/utils/mask'
import { textOrDash } from '@/utils/format'

const props = withDefaults(
  defineProps<{
    value: string | number | null | undefined
    /** 等宽字体：哈希、连接目标、SQL 片段用 */
    mono?: boolean
    /** 允许换行（日志、诊断详情）；false 时单行省略 */
    multiline?: boolean
    /** 只对主机名/IP 打码，保留端口与库名（元数据探查的 target 展示用） */
    hostOnly?: boolean
    /** 空值占位符 */
    placeholder?: string
  }>(),
  { mono: false, multiline: false, hostOnly: false, placeholder: undefined },
)

const raw = computed<string>(() => {
  const v = props.value
  if (v === null || v === undefined) {
    return ''
  }
  return String(v)
})

const display = computed<string>(() => {
  if (raw.value === '') {
    return props.placeholder ?? textOrDash(null)
  }
  if (props.hostOnly) {
    return maskHost(raw.value)
  }
  if (containsSensitive(raw.value)) {
    return SENSITIVE_FALLBACK
  }
  // redactSensitive 内部也做一次检测；这里保留调用是为了统一成一条代码路径，
  // 避免将来 hostOnly 分支被删掉后忘记补防。
  return redactSensitive(raw.value, SENSITIVE_FALLBACK, 'SensitiveText')
})

const isRedacted = computed<boolean>(() => display.value === SENSITIVE_FALLBACK)
</script>

<template>
  <span
    class="sensitive-text"
    :class="{
      'elt-mono': mono,
      'sensitive-text--multi': multiline,
      'sensitive-text--redacted': isRedacted,
    }"
  >
    {{ display }}
  </span>
</template>

<style scoped>
.sensitive-text {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.sensitive-text--multi {
  display: block;
  overflow: visible;
  white-space: pre-wrap;
  word-break: break-word;
}

.sensitive-text--redacted {
  color: var(--elt-text-muted);
  font-style: italic;
  white-space: normal;
}
</style>
