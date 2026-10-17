<script setup lang="ts">
/**
 * HOCON 页签（fe-04 §6.3）：制品元信息 + 门禁定位列表 + 只读编辑器。
 *
 * 编辑器（CodeMirror）体量不小，用 defineAsyncComponent 拆成独立 chunk：
 * 用户打开 Studio 多半是去看门禁结论，不该为没点开的页签付下载代价。
 *
 * 这里**不做** HOCON 语法校验：合法性以 `hocon_parser` 的 validation_result 为准，
 * 前端只展示行号与 finding 的对应关系（tokenizer 挂了就降级成纯文本）。
 */
import { computed, defineAsyncComponent, ref } from 'vue'
import { CopyDocument, Files } from '@element-plus/icons-vue'
import { ArtifactHash, EmptyState, SensitiveText } from '@/components/common'
import { useCopy } from '@/composables/useCopy'
import type { GateFinding, GateSeverity } from '@/types'

/** 与 HoconEditor 的标记结构对齐 */
interface HoconMark {
  id: string
  line: number
  severity: GateSeverity
}

const HoconEditor = defineAsyncComponent(() => import('./HoconEditor.vue'))

const props = withDefaults(
  defineProps<{
    content: string
    findings: GateFinding[]
    activeFindingId: string | null
    readonly: boolean
    sha256: string
    parserVersion: string
    versionId: number | null
  }>(),
  { versionId: null },
)

const emit = defineEmits<{
  'finding-click': [id: string]
  'request-diff': []
}>()

const { copied, copy } = useCopy()
const cursorLine = ref<number>(1)

/** 只有带行号的定位才能画到代码上；其余定位（plan/source_table）去别的页签 */
const hoconFindings = computed<GateFinding[]>(() =>
  props.findings
    .filter((f) => f.locator.kind === 'hocon')
    .slice()
    .sort((a, b) => lineOf(a) - lineOf(b)),
)

function lineOf(f: GateFinding): number {
  return f.locator.kind === 'hocon' ? f.locator.line : 0
}

const marks = computed<HoconMark[]>(() =>
  hoconFindings.value.map((f) => ({ id: f.id, line: lineOf(f), severity: f.severity })),
)

const activeLine = computed<number | null>(() => {
  const hit = hoconFindings.value.find((f) => f.id === props.activeFindingId)
  return hit === undefined ? null : lineOf(hit)
})

const lineCount = computed<number>(() => props.content.split('\n').length)

const SEVERITY_LABEL: Record<GateSeverity, string> = { block: '阻断', warn: '告警', info: '提示' }
</script>

<template>
  <div class="hocon-tab">
    <div class="hocon-tab__meta">
      <ArtifactHash :hash="sha256" display="truncate" label="sha256" copyable />
      <span class="hocon-tab__sep">·</span>
      <span class="hocon-tab__kv">parser</span>
      <span class="hocon-tab__mono">{{ parserVersion }}</span>
      <span class="hocon-tab__sep">·</span>
      <span class="hocon-tab__kv">{{ lineCount }} 行</span>
      <span class="hocon-tab__sep">·</span>
      <span class="hocon-tab__badge" title="生成结果不可手工修改">只读</span>

      <span class="hocon-tab__spacer" />

      <el-button size="small" :disabled="content === ''" @click="copy(content)">
        <el-icon><CopyDocument /></el-icon>
        <span>{{ copied === content ? '已复制' : '复制' }}</span>
      </el-button>
      <el-button size="small" :disabled="versionId === null" @click="emit('request-diff')">
        <el-icon><Files /></el-icon>
        <span>版本对比</span>
      </el-button>
    </div>

    <div v-if="hoconFindings.length > 0" class="hocon-tab__findings">
      <button
        v-for="f in hoconFindings"
        :key="f.id"
        type="button"
        class="hocon-tab__finding"
        :class="[`is-${f.severity}`, { 'is-active': f.id === activeFindingId }]"
        @click="emit('finding-click', f.id)"
      >
        <span class="hocon-tab__finding-line elt-mono">L{{ lineOf(f) }}</span>
        <span class="hocon-tab__finding-sev">{{ SEVERITY_LABEL[f.severity] }}</span>
        <SensitiveText :value="f.message" class="hocon-tab__finding-msg" />
      </button>
    </div>

    <div class="hocon-tab__editor">
      <EmptyState
        v-if="content === ''"
        variant="no-data"
        title="尚无 HOCON 制品"
        description="工作流跑到生成阶段后，这里会出现由 EtlPlan 编译出的 SeaTunnel HOCON。"
      />
      <Suspense v-else>
        <HoconEditor
          :content="content"
          :readonly="readonly"
          :marks="marks"
          :active-line="activeLine"
          @finding-click="(id: string) => emit('finding-click', id)"
          @line-change="(n: number) => (cursorLine = n)"
        />
        <template #fallback>
          <div class="elt-skeleton elt-skeleton--block hocon-tab__loading" />
        </template>
      </Suspense>
    </div>

    <div v-if="content !== ''" class="hocon-tab__status">光标：第 {{ cursorLine }} 行</div>
  </div>
</template>

<style scoped>
.hocon-tab {
  display: flex;
  flex-direction: column;
  gap: 8px;
  height: 100%;
  min-height: 0;
}

.hocon-tab__meta {
  display: flex;
  gap: 6px;
  align-items: center;
  font-size: 11px;
  color: var(--elt-text-secondary);
}

.hocon-tab__sep {
  color: var(--elt-border-subtle);
}

.hocon-tab__kv {
  color: var(--elt-text-muted);
}

.hocon-tab__mono {
  font-family: var(--elt-mono);
  color: var(--elt-text-secondary);
}

.hocon-tab__badge {
  padding: 0 5px;
  line-height: 15px;
  color: var(--elt-text-muted);
  border: 1px solid var(--elt-border-subtle);
  border-radius: var(--elt-radius-pill);
}

.hocon-tab__spacer {
  flex: 1;
}

.hocon-tab__findings {
  display: flex;
  flex-direction: column;
  gap: 2px;
  max-height: 108px;
  overflow-y: auto;
}

.hocon-tab__finding {
  display: flex;
  gap: 8px;
  align-items: center;
  padding: 3px 6px;
  font-size: 11px;
  color: var(--elt-text-secondary);
  text-align: left;
  cursor: pointer;
  background-color: transparent;
  border: 1px solid transparent;
  border-left-width: 3px;
  border-radius: var(--elt-radius-control);
}

.hocon-tab__finding:hover {
  background-color: var(--elt-bg-elevated);
}

.hocon-tab__finding.is-active {
  background-color: var(--elt-bg-elevated);
  border-color: var(--elt-accent);
}

.hocon-tab__finding.is-block {
  border-left-color: var(--elt-danger);
}

.hocon-tab__finding.is-warn {
  border-left-color: var(--elt-warn);
}

.hocon-tab__finding.is-info {
  border-left-color: var(--elt-text-muted);
}

.hocon-tab__finding-line {
  flex: none;
  min-width: 36px;
  color: var(--elt-text-muted);
}

.hocon-tab__finding-sev {
  flex: none;
}

.hocon-tab__finding-msg {
  flex: 1;
  min-width: 0;
}

.hocon-tab__editor {
  flex: 1;
  min-height: 0;
}

.hocon-tab__loading {
  height: 100%;
}

.hocon-tab__status {
  font-size: 11px;
  color: var(--elt-text-muted);
}
</style>
