<script setup lang="ts">
/**
 * 制品哈希展示（fe-04 §6.5 / fe-07 §6）。
 *
 * sha256 是审计对账的锚点，因此：
 * - 表格里显示前 8 位（缩略），悬停给全文，点击可复制；
 * - **一律等宽字体**，否则 0/O、1/l 无法区分，人工核对会出错；
 * - 空值显示 `—` 而不是空串：空串让人以为「有值但没渲染出来」。
 */
import { computed } from 'vue'
import { CopyDocument, Select } from '@element-plus/icons-vue'
import { useCopy } from '@/composables/useCopy'
import { DASH, hashShort, truncateHash } from '@/utils/format'

const props = withDefaults(
  defineProps<{
    hash: string | null | undefined
    /** short = 前 8 位；truncate = 前 8 + 后 8；full = 全文 */
    display?: 'short' | 'truncate' | 'full'
    /** 是否显示复制按钮；详情页用 true，密集表格用 false */
    copyable?: boolean
    label?: string
  }>(),
  { display: 'short', copyable: false, label: '哈希' },
)

const { copied, copy } = useCopy()

const text = computed<string>(() => {
  if (props.hash === null || props.hash === undefined || props.hash === '') {
    return DASH
  }
  if (props.display === 'full') {
    return props.hash
  }
  return props.display === 'truncate' ? truncateHash(props.hash) : hashShort(props.hash)
})

const isCopied = computed<boolean>(
  () => props.hash !== null && props.hash !== undefined && copied.value === props.hash,
)

const hasValue = computed<boolean>(() => props.hash !== null && props.hash !== undefined && props.hash !== '')

/**
 * 复制目标。
 * 按钮只在 `hasValue` 时渲染，因此这里不会是空串——
 * 用 computed 归一化类型，好过在模板里写 `hash!`（非空断言会掩盖将来真的传空的场景）。
 */
const copyTarget = computed<string>(() => props.hash ?? '')
</script>

<template>
  <span class="artifact-hash">
    <el-tooltip :disabled="!hasValue || display === 'full'" :content="hash ?? ''" placement="top">
      <span class="artifact-hash__text elt-mono">{{ text }}</span>
    </el-tooltip>
    <el-button
      v-if="copyable && hasValue"
      class="artifact-hash__copy"
      link
      size="small"
      :title="isCopied ? '已复制' : `复制${label}`"
      @click.stop="copy(copyTarget, label)"
    >
      <el-icon :size="12">
        <Select v-if="isCopied" />
        <CopyDocument v-else />
      </el-icon>
    </el-button>
  </span>
</template>

<style scoped>
.artifact-hash {
  display: inline-flex;
  align-items: center;
  gap: 2px;
  max-width: 100%;
}

.artifact-hash__text {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.artifact-hash__copy {
  flex: none;
  padding: 0 2px;
}
</style>
