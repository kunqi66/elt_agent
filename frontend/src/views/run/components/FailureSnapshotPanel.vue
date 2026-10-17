<script setup lang="ts">
/**
 * 失败快照（`execution_run.failure_snapshot`）的只读键值渲染（fe-06 §10）。
 *
 * 两个约束：
 * - 快照结构尚未由 be-16 冻结，所以这里**不假设任何键名**：原样列出后端给的键值，
 *   并明确标注「字段待后端冻结」，避免下游把它当成稳定契约去写自动化。
 * - 不从中「提取」任何疑似原文来单独展示：取值统一经 SensitiveText 走脱敏出口。
 *   快照里最可能出现未脱敏残片，这里不当例外通道。
 */
import { computed } from 'vue'
import { SectionCard, SensitiveText } from '@/components/common'

const props = defineProps<{
  snapshot: Record<string, unknown>
  /** 是否已经在别处展示过（硬中断时父级会默认展开） */
  title?: string
}>()

interface SnapshotRow {
  key: string
  /** 已序列化的取值；对象/数组压成 JSON 文本，交由 SensitiveText 过筛 */
  text: string
  multiline: boolean
}

function serialize(value: unknown): { text: string; multiline: boolean } {
  if (value === null || value === undefined) {
    return { text: '—', multiline: false }
  }
  if (typeof value === 'string') {
    return { text: value, multiline: value.includes('\n') }
  }
  if (typeof value === 'number' || typeof value === 'boolean') {
    return { text: String(value), multiline: false }
  }
  try {
    const text = JSON.stringify(value, null, 2)
    return { text: text === undefined ? String(value) : text, multiline: true }
  } catch {
    // 循环引用等无法序列化的情况：给一句人话，不要把 [object Object] 抛给用户
    return { text: '（该值无法序列化展示）', multiline: false }
  }
}

const rows = computed<SnapshotRow[]>(() =>
  Object.entries(props.snapshot).map(([key, value]) => {
    const serialized = serialize(value)
    return { key, text: serialized.text, multiline: serialized.multiline }
  }),
)
</script>

<template>
  <SectionCard :title="title ?? '失败快照'" :padded="true">
    <template #extra>
      <span class="snapshot__hint">字段结构待 be-16 冻结 · 只读</span>
    </template>

    <p v-if="rows.length === 0" class="snapshot__empty">
      本次运行没有失败快照（正常结束的运行不会产生快照）。
    </p>

    <dl v-else class="snapshot__list">
      <template v-for="row in rows" :key="row.key">
        <dt class="snapshot__key elt-mono">
          <SensitiveText :value="row.key" mono />
        </dt>
        <dd class="snapshot__value">
          <SensitiveText :value="row.text" mono multiline />
        </dd>
      </template>
    </dl>
  </SectionCard>
</template>

<style scoped>
.snapshot__hint {
  font-size: 11px;
  color: var(--elt-text-muted);
}

.snapshot__empty {
  margin: 0;
  font-size: 12px;
  color: var(--elt-text-muted);
}

.snapshot__list {
  display: grid;
  grid-template-columns: minmax(120px, 200px) minmax(0, 1fr);
  gap: 6px 14px;
  margin: 0;
  font-size: 12px;
}

.snapshot__key {
  color: var(--elt-text-secondary);
  overflow-wrap: anywhere;
}

.snapshot__value {
  margin: 0;
  color: var(--elt-text-primary);
  overflow-wrap: anywhere;
}
</style>
