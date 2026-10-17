<script setup lang="ts">
/**
 * DAG 画布（Vue Flow，fe-04 §6.2）。
 *
 * 职责边界：
 * - **布局**：后端给了 `position` 就用；没给就按最长路径分层（源层 x=0，逐层 +280）。
 *   布局是几何计算而非业务聚合，允许在前端做；节点数 > 200 时退化为紧凑网格，
 *   因为几百个节点上的分层图在任何屏幕上都不再可读。
 * - **高亮**：只读门禁 finding 的 `severity` 与 `dag_node` 定位，
 *   不做任何「这个节点是否真的有问题」的判断——那是 engines/gate 的结论。
 * - **不做**：拖拽/连线/持久化布局。刷新后回到布局结果（fe-04 §6.2「布局改动不被记忆」）。
 */
import { computed, shallowRef, watch } from 'vue'
import type { Component } from 'vue'
import { MarkerType, VueFlow } from '@vue-flow/core'
import type { Edge, Node } from '@vue-flow/core'
import { Background } from '@vue-flow/background'
import { Controls } from '@vue-flow/controls'
import '@vue-flow/core/dist/style.css'
import { useProjectStore } from '@/stores/project'
import DagNode from './DagNode.vue'
import type { EtlPlanDag, EtlPlanDagNode, GateFinding } from '@/types'

/** 与 DagNode.vue 的 props.data 结构一一对应 */
interface DagNodeData {
  label: string
  kind: EtlPlanDagNode['type']
  summary: string
  blockCount: number
  warnCount: number
}

const props = defineProps<{
  dag: EtlPlanDag
  findings: GateFinding[]
  /** 当前选中的节点 id；由父级持有，本组件不自己记 */
  selectedNodeId: string | null
  /** 门禁面板点「定位」后要求高亮的节点（locator.kind === 'dag_node'） */
  locatedNodeId: string | null
}>()

const emit = defineEmits<{ 'select-node': [nodeId: string | null] }>()

const projectStore = useProjectStore()

/** 节点数超过此值时关闭自动分层：分层图在这个规模下已不可读 */
const COMPACT_THRESHOLD = 200
const LAYER_GAP = 280
const ROW_GAP = 96

const compact = computed<boolean>(() => props.dag.nodes.length > COMPACT_THRESHOLD)

/**
 * 五个 kind 注册到同一个组件（见 DagNode.vue 的说明）。
 * 断言成 Component 是为了对上 vue-flow 的 `NodeComponent`：
 * 它要求节点组件的 props 涵盖整套 `NodeProps`，而自定义节点只会用到其中几个，
 * 结构化比较必然失败——这是该库的已知类型摩擦，不是我们的运行时问题。
 */
const NODE_COMPONENT = DagNode as Component

const nodeTypes: Record<string, Component> = {
  source: NODE_COMPONENT,
  transform: NODE_COMPONENT,
  filter: NODE_COMPONENT,
  quality: NODE_COMPONENT,
  sink: NODE_COMPONENT,
}

/** 每个节点上的阻断/告警条数：给角标用，避免在模板里反复 filter */
const severityByNode = computed<Map<string, { block: number; warn: number }>>(() => {
  const map = new Map<string, { block: number; warn: number }>()
  for (const f of props.findings) {
    if (f.locator.kind !== 'dag_node') {
      continue
    }
    if (f.severity === 'info') {
      continue
    }
    const hit = map.get(f.locator.node_id) ?? { block: 0, warn: 0 }
    if (f.severity === 'block') {
      hit.block += 1
    } else {
      hit.warn += 1
    }
    map.set(f.locator.node_id, hit)
  }
  return map
})

/** meta 摘要：只展示标量值，且截断——meta 里可能塞着大对象 */
function summarize(meta: Record<string, unknown>): string {
  const parts: string[] = []
  for (const [key, value] of Object.entries(meta)) {
    if (typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean') {
      parts.push(`${key}=${String(value)}`)
    }
    if (parts.length === 2) {
      break
    }
  }
  return parts.join(' ')
}

/**
 * 最长路径分层。
 * 图里出现环（脏 Plan 或后端 bug）时靠迭代上限收敛，绝不死循环。
 */
function layerNodes(dag: EtlPlanDag): Map<string, number> {
  const depth = new Map<string, number>()
  for (const n of dag.nodes) {
    depth.set(n.id, 0)
  }
  // 最多迭代 |V| 轮：轮数超过节点数就说明有环，此时保留已有结果即可
  for (let round = 0; round < dag.nodes.length; round += 1) {
    let changed = false
    for (const e of dag.edges) {
      const from = depth.get(e.source)
      const to = depth.get(e.target)
      if (from === undefined || to === undefined) {
        continue
      }
      if (to < from + 1) {
        depth.set(e.target, from + 1)
        changed = true
      }
    }
    if (!changed) {
      break
    }
  }
  return depth
}

function buildNodes(): Node[] {
  const dag = props.dag
  const severities = severityByNode.value

  if (compact.value) {
    // 紧凑视图：只保证「每个节点都在画布里且不重叠」，不承诺可读
    return dag.nodes.map((n, index) => ({
      id: n.id,
      type: n.type,
      position: { x: (index % 6) * 180, y: Math.floor(index / 6) * 72 },
      data: toData(n, severities),
      draggable: false,
      connectable: false,
    }))
  }

  const hasPosition = dag.nodes.every((n) => n.position !== undefined)
  if (hasPosition) {
    return dag.nodes.map((n) => ({
      id: n.id,
      type: n.type,
      // position 已在上面的 every() 中确认存在
      position: n.position ?? { x: 0, y: 0 },
      data: toData(n, severities),
      draggable: false,
      connectable: false,
    }))
  }

  const depth = layerNodes(dag)
  const rowInLayer = new Map<number, number>()
  return dag.nodes.map((n) => {
    const layer = depth.get(n.id) ?? 0
    const row = rowInLayer.get(layer) ?? 0
    rowInLayer.set(layer, row + 1)
    return {
      id: n.id,
      type: n.type,
      position: { x: layer * LAYER_GAP, y: row * ROW_GAP + 40 },
      data: toData(n, severities),
      draggable: false,
      connectable: false,
    }
  })
}

function toData(n: EtlPlanDagNode, severities: Map<string, { block: number; warn: number }>): DagNodeData {
  const hit = severities.get(n.id)
  return {
    label: n.label,
    kind: n.type,
    summary: summarize(n.meta),
    blockCount: hit?.block ?? 0,
    warnCount: hit?.warn ?? 0,
  }
}

function buildEdges(): Edge[] {
  // 逐字段构造而不是展开：Edge.label 不接受 null（后端给的是 string|null），
  // 展开会让 null 直接漏进类型里
  return props.dag.edges.map((e) => ({
    id: e.id,
    source: e.source,
    target: e.target,
    label: e.label ?? undefined,
    type: 'smoothstep',
    markerEnd: MarkerType.ArrowClosed,
    class: 'dag-edge',
  }))
}

/**
 * 用 shallowRef 而不是 ref：
 * 1) 图是整批替换的（rebuild 时重新赋数组），不需要深层响应式；
 * 2) `ref<Node[]>` 的 value 类型是 `UnwrapRef<Node[]>`，Vue 会按属性递归展开 Node，
 *    而 vue-flow 的 `Node['class']` 是递归联合，展开过程直接触发 TS2589。
 */
const nodes = shallowRef<Node[]>([])
const edges = shallowRef<Edge[]>([])

/** 高亮的节点：门禁定位优先于用户点选——用户刚要看的正是那条 finding */
const highlightNodeId = computed<string | null>(() => props.locatedNodeId ?? props.selectedNodeId)

/** 与高亮节点相连的边一并描色：只标节点会让用户看不出它接在谁身上 */
const highlightEdgeIds = computed<Set<string>>(() => {
  const id = highlightNodeId.value
  if (id === null) {
    return new Set<string>()
  }
  return new Set(props.dag.edges.filter((e) => e.source === id || e.target === id).map((e) => e.id))
})

function rebuild(): void {
  nodes.value = buildNodes()
  edges.value = buildEdges()
}

// dag 或 finding 变化都要重建：门禁重新评估后角标会变
watch([() => props.dag, () => severityByNode.value], rebuild, { immediate: true, deep: false })

/**
 * 视觉高亮不重建节点，只改 class，避免整图重排导致视图跳动。
 *
 * 这里必须先收窄成普通对象再断言回来：vue-flow 的 `Node['class']` 是
 * `string | string[] | Record | ClassFunc<GraphNode<…>>` 的递归联合
 * （ClassFunc 的参数 GraphNode 又 extends Node），任何与 Node 做交叉的字面量
 * 都会让 TS 无限展开整棵类型并报 TS2589，而不是报真正的类型不匹配。
 */
function decorateNode(node: Node): Node {
  const cls = [
    node.id === highlightNodeId.value ? 'is-highlighted' : '',
    node.id === props.locatedNodeId ? 'is-located' : '',
  ]
    .filter((c) => c !== '')
    .join(' ')
  const copy = { ...(node as unknown as Record<string, unknown>) }
  copy.class = cls
  return copy as unknown as Node
}

const decoratedNodes = computed<Node[]>(() => nodes.value.map(decorateNode))

const decoratedEdges = computed<Edge[]>(() =>
  edges.value.map((e) => ({ ...e, class: highlightEdgeIds.value.has(e.id) ? 'dag-edge is-highlighted' : 'dag-edge' })),
)

// 项目环境只影响底纹，不参与任何判定；此处仅用于让画布与全站暗色令牌一致
const isProd = computed<boolean>(() => projectStore.isProd)
</script>

<template>
  <div class="dag-graph">
    <el-alert v-if="compact" type="warning" :closable="false" show-icon class="dag-graph__notice">
      <span>拓扑过大（{{ dag.nodes.length }} 个节点），已切换到紧凑视图，仅供总览。</span>
    </el-alert>

    <VueFlow
      :nodes="decoratedNodes"
      :edges="decoratedEdges"
      :node-types="nodeTypes"
      :nodes-draggable="false"
      :nodes-connectable="false"
      :elements-selectable="true"
      :fit-view-on-init="true"
      :min-zoom="0.15"
      :max-zoom="2"
      :zoom-on-double-click="false"
      class="dag-graph__canvas"
      :class="{ 'is-prod': isProd }"
      @node-click="(e: { node: { id: string } }) => emit('select-node', e.node.id)"
      @pane-click="() => emit('select-node', null)"
    >
      <Background pattern-color="#27344d" :gap="18" :size="1" />
      <Controls :show-interactive="false" />
    </VueFlow>
  </div>
</template>

<style scoped>
.dag-graph {
  position: relative;
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 320px;
}

.dag-graph__notice {
  margin: 0 0 8px;
}

.dag-graph__canvas {
  flex: 1;
  min-height: 0;
  background-color: var(--elt-bg-code);
  border: 1px solid var(--elt-border-subtle);
  border-radius: var(--elt-radius-card);
}

/* prod 环境给画布加一层极淡的红底，与全站「生产必须一眼可辨」的口径一致 */
.dag-graph__canvas.is-prod {
  background-color: color-mix(in srgb, var(--elt-bg-code) 92%, var(--elt-danger));
}

.dag-graph__canvas :deep(.vue-flow__edge-path) {
  stroke: var(--elt-border-subtle);
  stroke-width: 1.5;
}

.dag-graph__canvas :deep(.vue-flow__edge.is-highlighted .vue-flow__edge-path) {
  stroke: var(--elt-accent);
  stroke-width: 2;
}

.dag-graph__canvas :deep(.vue-flow__edge-text) {
  fill: var(--elt-text-secondary);
  font-size: 10px;
}

.dag-graph__canvas :deep(.vue-flow__edge-textbg) {
  fill: var(--elt-bg-app);
}

.dag-graph__canvas :deep(.vue-flow__node.is-highlighted) {
  z-index: 5;
}

/* 控件与 Vue Flow 默认浅色皮肤对齐到本项目的暗色令牌 */
.dag-graph__canvas :deep(.vue-flow__controls) {
  overflow: hidden;
  border: 1px solid var(--elt-border-subtle);
  border-radius: var(--elt-radius-control);
  box-shadow: none;
}

.dag-graph__canvas :deep(.vue-flow__controls-button) {
  color: var(--elt-text-secondary);
  background-color: var(--elt-bg-elevated);
  border-bottom: 1px solid var(--elt-border-subtle);
}

.dag-graph__canvas :deep(.vue-flow__controls-button:hover) {
  color: var(--elt-text-primary);
  background-color: var(--elt-bg-surface);
}

.dag-graph__canvas :deep(.vue-flow__controls-button svg) {
  fill: currentcolor;
}
</style>
