<script setup lang="ts">
/**
 * 两段文本的并排差异视图（@codemirror/merge）。
 *
 * 只做文本比对的**呈现**：差异算法由 CodeMirror 负责，
 * 前端不计算任何「谁比谁新」「改动是否合规」的结论——那是后端制品与门禁的事。
 *
 * MergeView 没有 setDoc 之类的增量接口，改内容只能整体重建；
 * 因此 diff 结果变化时 destroy + new，而不是原地 dispatch。
 */
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { EditorState } from '@codemirror/state'
import { EditorView, lineNumbers } from '@codemirror/view'
import { MergeView } from '@codemirror/merge'

const props = defineProps<{
  a: string
  b: string
  aLabel: string
  bLabel: string
}>()

const host = ref<HTMLDivElement | null>(null)
let merge: MergeView | null = null

function destroy(): void {
  merge?.destroy()
  merge = null
}

function build(): void {
  destroy()
  if (host.value === null) {
    return
  }
  // 两侧共用同一份扩展：只读 + 行号，样式统一，避免左右字体/缩进看起来不一样
  const extensions = [
    EditorState.readOnly.of(true),
    EditorView.editable.of(false),
    lineNumbers(),
    EditorView.theme({ '&': { fontSize: '12px' } }),
  ]
  merge = new MergeView({
    parent: host.value,
    a: { doc: props.a, extensions },
    b: { doc: props.b, extensions },
    highlightChanges: true,
    gutter: true,
    // 大量未改动的行折叠起来，让真正的差异段落在首屏可见
    collapseUnchanged: { margin: 3, minSize: 4 },
    diffConfig: { scanLimit: 3000 },
  })
}

onMounted(build)

onBeforeUnmount(destroy)

watch(
  () => [props.a, props.b],
  () => build(),
)
</script>

<template>
  <div class="merge">
    <div class="merge__labels">
      <span class="merge__label">{{ aLabel }}</span>
      <span class="merge__label">{{ bLabel }}</span>
    </div>
    <div ref="host" class="merge__host" />
  </div>
</template>

<style scoped>
.merge {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
}

.merge__labels {
  display: flex;
  gap: 12px;
  padding-bottom: 4px;
  font-size: 11px;
  color: var(--elt-text-muted);
}

.merge__label {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.merge__host {
  flex: 1;
  min-height: 0;
  overflow: auto;
  border: 1px solid var(--elt-border-subtle);
  border-radius: var(--elt-radius-card);
}

.merge__host :deep(.cm-mergeView) {
  height: 100%;
}

.merge__host :deep(.cm-mergeViewEditors) {
  height: 100%;
}

.merge__host :deep(.cm-editor) {
  height: 100%;
}

.merge__host :deep(.cm-scroller) {
  overflow: auto;
  font-family: var(--elt-mono);
}

.merge__host :deep(.cm-changedLine) {
  background-color: rgba(230, 162, 60, 0.12);
}

.merge__host :deep(.cm-insertedLine) {
  background-color: rgba(103, 194, 58, 0.14);
}

.merge__host :deep(.cm-deletedLine) {
  background-color: rgba(245, 108, 108, 0.14);
}
</style>
