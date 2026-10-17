<script setup lang="ts">
/**
 * 执行态数据流（fe-06 §2「DAG 执行态」）。
 *
 * 这是一张**展示图**，不是状态机推演：节点高亮只由后端已下发的字段决定
 * （影子表/错误表名是否存在、正式表是否已写入），
 * 权威状态永远以 `RunStatusTag` 为准——本图不参与任何判定。
 * 之所以要做这一块：三区布局里「影子表 → 质量分流 → Swap → 正式表」这条主线
 * 只靠表格列很难一眼看懂，而它恰恰是本平台最关键的安全设计
 * （先写影子表、分流拒绝行、通过后才原子 Swap）。
 */
import { computed } from 'vue'
import { SectionCard, SensitiveText } from '@/components/common'
import { enumText } from '@/utils/enums'
import type { RunDetail } from '@/types'

const props = defineProps<{ run: RunDetail }>()

type NodeState = 'done' | 'active' | 'blocked' | 'idle'

interface FlowNode {
  key: string
  title: string
  detail: string | null
  state: NodeState
}

/** 终态里「数据已经进主表」的只有成功一种；其余都要看是否回滚 */
const swapped = computed<boolean>(() => props.run.status === 'succeeded')

const nodes = computed<FlowNode[]>(() => {
  const run = props.run
  const running = run.status === 'running'
  const swappedState: NodeState = swapped.value ? 'done' : run.status === 'failed' || run.status === 'interrupted' ? 'blocked' : 'idle'

  return [
    {
      key: 'source',
      title: '源快照',
      detail: `管线 ${run.pipeline_name} · v${run.version_no}`,
      state: run.status === 'queued' ? 'active' : 'done',
    },
    {
      key: 'shadow',
      title: '写入影子表',
      detail: run.shadow_table,
      state: run.shadow_table === null ? 'idle' : running || run.status === 'paused' ? 'active' : 'done',
    },
    {
      key: 'split',
      title: '质量分流',
      detail: `拒绝 ${run.reject_rows} 行 → 错误表`,
      state: run.reject_rows > 0 ? 'blocked' : run.status === 'queued' ? 'idle' : 'done',
    },
    {
      key: 'swap',
      title: '原子 Swap',
      detail: swapped.value ? '已提交到正式表' : '未提交（正式表未被修改）',
      state: swappedState,
    },
    {
      key: 'formal',
      title: '正式表',
      detail: run.formal_table,
      state: swapped.value ? 'done' : 'idle',
    },
  ]
})

const rollbackVisible = computed<boolean>(() =>
  ['rolling_back', 'rolled_back', 'cancelled', 'interrupted', 'failed'].includes(props.run.status),
)

const statusText = computed<string>(() => enumText('RunStatus', props.run.status))
</script>

<template>
  <SectionCard title="执行态数据流" :padded="true">
    <template #extra>
      <span class="flow__hint">仅为展示 · 权威状态以「{{ statusText }}」为准</span>
    </template>

    <div class="flow">
      <div v-for="(node, index) in nodes" :key="node.key" class="flow__cell">
        <div class="flow__node" :class="`flow__node--${node.state}`">
          <span class="flow__node-title">{{ node.title }}</span>
          <SensitiveText
            class="flow__node-detail"
            :value="node.detail"
            mono
            multiline
          />
        </div>
        <span v-if="index < nodes.length - 1" class="flow__arrow" aria-hidden="true">→</span>
      </div>
    </div>

    <div v-if="rollbackVisible" class="flow__rollback">
      <span class="flow__rollback-label">受管回滚</span>
      <span class="flow__rollback-text">
        <template v-if="run.status === 'rolling_back'">回滚进行中：正在执行 <span class="elt-mono">target.rollback_cleanup</span></template>
        <template v-else-if="run.status === 'rolled_back'">回滚已完成：未发布的影子数据已按方案清理</template>
        <template v-else>当前状态可发起受管回滚（回滚不会改动正式表内容，仅清理影子数据与错误表残留）</template>
      </span>
    </div>

    <p class="flow__foot">
      主线语义：数据先进<b>影子表</b>，拒绝行进错误表；只有质量校验通过且原子 Swap 成功，
      正式表才生效。失败与硬中断都会停在「正式表未被修改」的状态。
    </p>
  </SectionCard>
</template>

<style scoped>
.flow__hint {
  font-size: 11px;
  color: var(--elt-text-muted);
}

.flow {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  align-items: center;
}

.flow__cell {
  display: flex;
  gap: 6px;
  align-items: center;
}

.flow__node {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 132px;
  padding: 8px 10px;
  background-color: var(--elt-bg-elevated);
  border: 1px solid var(--elt-border-subtle);
  border-radius: var(--elt-radius-control);
}

.flow__node--done {
  border-color: color-mix(in srgb, var(--elt-success) 45%, transparent);
}

.flow__node--active {
  border-color: color-mix(in srgb, var(--elt-accent) 55%, transparent);
}

.flow__node--blocked {
  border-color: color-mix(in srgb, var(--elt-danger) 45%, transparent);
}

.flow__node-title {
  font-size: 12px;
  font-weight: 600;
  color: var(--elt-text-primary);
}

.flow__node-detail {
  font-size: 11px;
  color: var(--elt-text-muted);
  overflow-wrap: anywhere;
}

.flow__arrow {
  color: var(--elt-text-muted);
}

.flow__rollback {
  display: flex;
  gap: 8px;
  align-items: baseline;
  margin-top: 12px;
  font-size: 12px;
  color: var(--elt-text-secondary);
}

.flow__rollback-label {
  padding: 1px 6px;
  font-size: 11px;
  color: var(--elt-warn);
  background-color: var(--elt-bg-elevated);
  border-radius: var(--elt-radius-pill);
}

.flow__foot {
  margin: 12px 0 0;
  font-size: 11px;
  line-height: 1.7;
  color: var(--elt-text-muted);
}
</style>
