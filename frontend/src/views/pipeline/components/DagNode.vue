<script setup lang="ts">
/**
 * DAG 节点（fe-04 §6.2）。
 *
 * 五种 kind **共用一个组件**，由 DagGraph 注册到五个 nodeTypes 键上：
 * 它们的结构完全一致（左侧色条 + 图标 + 标签 + 摘要 + 角标），
 * 拆成 SourceNode/TransformNode/… 五份只会让「新增一个 kind 时漏改样式」
 * 的概率上升。区分靠 kind 派生的 class，而不是靠不同的组件文件。
 *
 * 角标显示的是**后端门禁给的数量**，本组件不做任何严重度判定。
 */
import { computed } from 'vue'
import { Checked, Coin, Filter, SetUp, Upload } from '@element-plus/icons-vue'
import { Handle, Position } from '@vue-flow/core'
import type { DagNodeType } from '@/types'

/** 与 DagGraph.vue 的节点 data 结构一一对应（跨 vue-flow 传递，只能各自声明） */
interface DagNodeData {
  label: string
  kind: DagNodeType
  summary: string
  blockCount: number
  warnCount: number
}

const props = withDefaults(
  defineProps<{
    id: string
    /** vue-flow 的节点 type，取值即 kind */
    type?: string
    data?: DagNodeData
    selected?: boolean
  }>(),
  { type: 'transform', data: undefined, selected: false },
)

/** vue-flow 未传 data 时不炸：退化为「未知节点」而不是整个画布白屏 */
const node = computed<DagNodeData>(
  () => props.data ?? { label: props.id, kind: 'transform', summary: '', blockCount: 0, warnCount: 0 },
)

const KIND_TEXT: Record<DagNodeType, string> = {
  source: '源',
  transform: '转换',
  filter: '过滤',
  quality: '质量',
  sink: '目标',
}

const kindText = computed<string>(() => KIND_TEXT[node.value.kind])

const hasBlock = computed<boolean>(() => node.value.blockCount > 0)
const hasWarn = computed<boolean>(() => !hasBlock.value && node.value.warnCount > 0)
</script>

<template>
  <div
    class="dag-node"
    :class="[
      `dag-node--${node.kind}`,
      {
        'is-selected': selected,
        'is-blocked': hasBlock,
        'is-warned': hasWarn,
      },
    ]"
  >
    <!-- 连线锚点：只读拓扑，禁止拖拽连线，故 connectable 恒为 false -->
    <Handle type="target" :position="Position.Left" :connectable="false" />

    <span class="dag-node__bar" aria-hidden="true" />

    <el-icon class="dag-node__icon" :size="14">
      <Coin v-if="node.kind === 'source'" />
      <SetUp v-else-if="node.kind === 'transform'" />
      <Filter v-else-if="node.kind === 'filter'" />
      <Checked v-else-if="node.kind === 'quality'" />
      <Upload v-else />
    </el-icon>

    <div class="dag-node__body">
      <div class="dag-node__title">{{ node.label }}</div>
      <div v-if="node.summary !== ''" class="dag-node__summary elt-mono">{{ node.summary }}</div>
    </div>

    <span class="dag-node__kind">{{ kindText }}</span>

    <!-- 阻断徽标：右上角，与节点描红叠加，保证「哪几个节点有问题」一眼可辨 -->
    <span v-if="hasBlock" class="dag-node__badge dag-node__badge--block">
      {{ node.blockCount }}
    </span>
    <span v-else-if="hasWarn" class="dag-node__badge dag-node__badge--warn">{{ node.warnCount }}</span>

    <Handle type="source" :position="Position.Right" :connectable="false" />
  </div>
</template>

<style scoped>
.dag-node {
  position: relative;
  display: flex;
  gap: 8px;
  align-items: center;
  width: 210px;
  padding: 8px 10px 8px 0;
  overflow: hidden;
  background-color: var(--elt-bg-surface);
  border: 1px solid var(--elt-border-subtle);
  border-radius: var(--elt-radius-control);
}

.dag-node__bar {
  flex: none;
  width: 3px;
  height: 30px;
  border-radius: 0 2px 2px 0;
  background-color: var(--elt-text-muted);
}

.dag-node__icon {
  flex: none;
  color: var(--elt-text-secondary);
}

.dag-node__body {
  min-width: 0;
  flex: 1;
}

.dag-node__title {
  overflow: hidden;
  font-size: 12px;
  font-weight: 600;
  line-height: 16px;
  color: var(--elt-text-primary);
  text-overflow: ellipsis;
  white-space: nowrap;
}

.dag-node__summary {
  overflow: hidden;
  font-size: 11px;
  line-height: 15px;
  color: var(--elt-text-muted);
  text-overflow: ellipsis;
  white-space: nowrap;
}

.dag-node__kind {
  flex: none;
  padding: 0 5px;
  font-size: 11px;
  line-height: 15px;
  color: var(--elt-text-muted);
  background-color: var(--elt-bg-elevated);
  border-radius: var(--elt-radius-pill);
}

.dag-node__badge {
  position: absolute;
  top: -7px;
  right: -7px;
  min-width: 16px;
  padding: 0 4px;
  font-size: 11px;
  font-weight: 700;
  line-height: 16px;
  color: #fff;
  text-align: center;
  border-radius: var(--elt-radius-pill);
}

.dag-node__badge--block {
  background-color: var(--elt-danger);
}

.dag-node__badge--warn {
  color: #0b1220;
  background-color: var(--elt-warn);
}

/* 按 kind 着色：颜色只表达「这一步在干什么」，严重度一律由徽标与描边表达 */
.dag-node--source .dag-node__bar {
  background-color: var(--elt-accent);
}

.dag-node--transform .dag-node__bar {
  background-color: var(--elt-success);
}

.dag-node--filter .dag-node__bar {
  background-color: var(--elt-risk-p2);
}

.dag-node--quality .dag-node__bar {
  background-color: var(--elt-warn);
}

.dag-node--sink .dag-node__bar {
  background-color: var(--elt-risk-p1);
}

.dag-node.is-selected {
  border-color: var(--elt-accent);
  box-shadow: 0 0 0 2px var(--elt-badge-accent);
}

.dag-node.is-blocked {
  border-color: var(--elt-danger);
  box-shadow: 0 0 0 2px var(--elt-badge-danger);
}

.dag-node.is-warned {
  border-color: var(--elt-warn);
}
</style>
