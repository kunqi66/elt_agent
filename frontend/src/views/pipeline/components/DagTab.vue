<script setup lang="ts">
/**
 * DAG 页签（fe-04 §6.2）：画布 + 图例 + 选中节点详情。
 *
 * 节点详情放在画布下方而不是弹出抽屉：门禁定位会同时高亮节点与它的入/出边，
 * 抽屉一盖住画布就看不见了。
 *
 * 选中状态由父级持有（`selectedNodeId`），本组件只转发点击——
 * 因为门禁面板的「定位」要改同一个选中值，状态留在子组件里就会出现两套真相。
 */
import { computed, defineAsyncComponent } from 'vue'
import { EmptyState } from '@/components/common'
import { enumLabel } from '@/utils/enums'
import type { DagNodeType, EtlPlanDag, EtlPlanDagNode, GateFinding } from '@/types'

const DagGraph = defineAsyncComponent(() => import('./DagGraph.vue'))

const props = withDefaults(
  defineProps<{
    dag: EtlPlanDag | null
    findings: GateFinding[]
    selectedNodeId: string | null
    /** 门禁「定位」指定的节点；与选中值分开是为了让定位优先被看见 */
    locatedNodeId: string | null
    /** 拓扑过大时画布会退化，页签上也提示一次 */
    compactThreshold?: number
  }>(),
  { compactThreshold: 200 },
)

const emit = defineEmits<{ 'select-node': [nodeId: string | null] }>()

const selected = computed<EtlPlanDagNode | null>(
  () => props.dag?.nodes.find((n) => n.id === props.selectedNodeId) ?? null,
)

/** 命中该节点的 finding（含非 dag_node 定位的？不——只有能定位到节点才算命中） */
const selectedFindings = computed<GateFinding[]>(() => {
  if (props.selectedNodeId === null) {
    return []
  }
  return props.findings.filter(
    (f) => f.locator.kind === 'dag_node' && f.locator.node_id === props.selectedNodeId,
  )
})

const KIND_LEGEND: DagNodeType[] = ['source', 'transform', 'filter', 'quality', 'sink']

const KIND_TEXT: Record<DagNodeType, string> = {
  source: '源',
  transform: '转换',
  filter: '过滤',
  quality: '质量',
  sink: '目标',
}

const metaEntries = computed<Array<{ key: string; value: string }>>(() => {
  const node = selected.value
  if (node === null) {
    return []
  }
  return Object.entries(node.meta).map(([key, value]) => ({
    key,
    value: typeof value === 'string' ? value : JSON.stringify(value),
  }))
})
</script>

<template>
  <div class="dag-tab">
    <EmptyState
      v-if="dag === null || dag.nodes.length === 0"
      variant="no-data"
      title="尚无执行拓扑"
      description="EtlPlan 生成后，这里会画出节点与连线；门禁命中的节点会被描红并带角标。"
    />

    <template v-else>
      <div class="dag-tab__bar">
        <span class="dag-tab__legend-label">图例</span>
        <span v-for="k in KIND_LEGEND" :key="k" class="dag-tab__legend">
          <i class="dag-tab__dot" :class="`dag-tab__dot--${k}`" />
          {{ KIND_TEXT[k] }}
        </span>
        <span class="dag-tab__spacer" />
        <span class="dag-tab__count">{{ dag.nodes.length }} 节点 / {{ dag.edges.length }} 边</span>
      </div>

      <div class="dag-tab__canvas">
        <Suspense>
          <DagGraph
            :dag="dag"
            :findings="findings"
            :selected-node-id="selectedNodeId"
            :located-node-id="locatedNodeId"
            @select-node="(id: string | null) => emit('select-node', id)"
          />
          <template #fallback>
            <div class="elt-skeleton elt-skeleton--block dag-tab__loading" />
          </template>
        </Suspense>
      </div>

      <div v-if="selected !== null" class="dag-tab__detail">
        <div class="dag-tab__detail-head">
          <span class="dag-tab__detail-title">{{ selected.label }}</span>
          <el-tag size="small" effect="plain">{{ KIND_TEXT[selected.type] }}</el-tag>
          <span class="dag-tab__detail-id elt-mono">{{ selected.id }}</span>
          <span class="dag-tab__spacer" />
          <el-button link size="small" @click="emit('select-node', null)">取消选择</el-button>
        </div>

        <div v-if="metaEntries.length > 0" class="dag-tab__meta">
          <span v-for="m in metaEntries" :key="m.key" class="dag-tab__meta-item">
            <span class="dag-tab__meta-key">{{ m.key }}</span>
            <span class="dag-tab__meta-value elt-mono">{{ m.value }}</span>
          </span>
        </div>

        <div v-for="f in selectedFindings" :key="f.id" class="dag-tab__finding" :class="`is-${f.severity}`">
          <!-- code 走 ErrorCode 表；未登记的取值由 enumLabel 回退成原文（后端升级时不至于显示空白） -->
          <el-tag size="small" type="danger" effect="dark">{{ enumLabel('ErrorCode', f.code).label }}</el-tag>
          <span class="dag-tab__finding-msg">{{ f.message }}</span>
        </div>
      </div>
    </template>
  </div>
</template>

<style scoped>
.dag-tab {
  display: flex;
  flex-direction: column;
  gap: 8px;
  height: 100%;
  min-height: 0;
}

.dag-tab__bar {
  display: flex;
  gap: 10px;
  align-items: center;
  font-size: 11px;
  color: var(--elt-text-muted);
}

.dag-tab__legend {
  display: inline-flex;
  gap: 4px;
  align-items: center;
}

.dag-tab__dot {
  width: 8px;
  height: 8px;
  border-radius: 2px;
}

.dag-tab__dot--source {
  background-color: var(--elt-accent);
}

.dag-tab__dot--transform {
  background-color: var(--elt-success);
}

.dag-tab__dot--filter {
  background-color: var(--elt-risk-p2);
}

.dag-tab__dot--quality {
  background-color: var(--elt-warn);
}

.dag-tab__dot--sink {
  background-color: var(--elt-risk-p1);
}

.dag-tab__spacer {
  flex: 1;
}

.dag-tab__canvas {
  flex: 1;
  min-height: 0;
}

.dag-tab__loading {
  height: 100%;
}

.dag-tab__detail {
  flex: none;
  max-height: 168px;
  padding: 8px;
  overflow-y: auto;
  background-color: var(--elt-bg-elevated);
  border: 1px solid var(--elt-border-subtle);
  border-radius: var(--elt-radius-card);
}

.dag-tab__detail-head {
  display: flex;
  gap: 8px;
  align-items: center;
}

.dag-tab__detail-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--elt-text-primary);
}

.dag-tab__detail-id {
  font-size: 11px;
  color: var(--elt-text-muted);
}

.dag-tab__meta {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 12px;
  margin-top: 6px;
}

.dag-tab__meta-item {
  display: inline-flex;
  gap: 4px;
  font-size: 11px;
}

.dag-tab__meta-key {
  color: var(--elt-text-muted);
}

.dag-tab__meta-value {
  color: var(--elt-text-secondary);
}

.dag-tab__finding {
  display: flex;
  gap: 6px;
  align-items: center;
  margin-top: 6px;
  font-size: 12px;
  color: var(--elt-text-secondary);
}

.dag-tab__finding.is-block .dag-tab__finding-msg {
  color: var(--elt-danger);
}

.dag-tab__finding-msg {
  flex: 1;
  min-width: 0;
}
</style>
