<script setup lang="ts">
/**
 * 通用枚举徽章（fe-01 §6.3）。
 *
 * 全站状态展示的唯一入口。各页面**不得**自己写 `v-if="status === 'running'"` 再配颜色——
 * 那样同一个状态会在列表、详情、时间线里长出三套说法。
 */
import { computed } from 'vue'
import { enumLabel } from '@/utils/enums'
import type { EnumKind } from '@/types'

const props = withDefaults(
  defineProps<{
    kind: EnumKind
    value: string | null | undefined
    size?: 'small' | 'default' | 'large'
    effect?: 'dark' | 'light' | 'plain'
    /** 覆盖显示文案（如时间线里要显示「数据槽 已通过」而非仅「已通过」） */
    text?: string
  }>(),
  { size: 'small', effect: 'light', text: undefined },
)

const resolved = computed(() => enumLabel(props.kind, props.value))
const label = computed(() => props.text ?? resolved.value.label)
</script>

<template>
  <el-tooltip
    :disabled="!resolved.unknown"
    :content="`未知取值「${resolved.label}」，后端可能已升级，请刷新或联系管理员`"
    placement="top"
  >
    <el-tag :type="resolved.tag" :size="size" :effect="effect" disable-transitions>
      {{ label }}
    </el-tag>
  </el-tooltip>
</template>
