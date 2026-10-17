<script setup lang="ts">
/**
 * 门禁结论面板（fe-04 §6.5）——右栏的主角。
 *
 * 三条硬约束：
 * 1. 阻断项（severity=block）**不可折叠、不可忽略**：折叠等于给用户一个
 *    「眼不见为净」的按钮，而 P0/P1 的结论只能由后端推翻（重新生成或修复）。
 * 2. 排序固定 block → warn → info，同类按 category 顺序；
 *    后端数组顺序不稳定，直接渲染会让「同样的结论换个顺序」，不利于对账。
 * 3. 每条 finding 都给「定位」，四类 GateLocator 全部覆盖（见 locatorText）。
 */
import { computed, ref } from 'vue'
import { Aim } from '@element-plus/icons-vue'
import { EmptyState, SensitiveText } from '@/components/common'
import { enumLabel } from '@/utils/enums'
import { formatDateTime } from '@/utils/format'
import type { GateCategory, GateConclusion, GateFinding, GateResult, GateSeverity } from '@/types'

const props = withDefaults(
  defineProps<{
    gate: GateResult | null
    activeFindingId: string | null
  }>(),
  { activeFindingId: null },
)

const emit = defineEmits<{
  locate: [findingId: string]
  activate: [findingId: string | null]
}>()

const CONCLUSION_META: Record<GateConclusion, { label: string; tag: 'success' | 'warning' | 'danger'; desc: string }> = {
  pass: { label: '通过', tag: 'success', desc: '无阻断/告警项，可以冻结版本。' },
  warn: { label: '有告警', tag: 'warning', desc: '存在告警项，不阻断冻结，但会随版本进入审批与运行监督。' },
  block: { label: '阻断', tag: 'danger', desc: '存在阻断项，版本无法冻结；请修复或重新生成。' },
}

const SEVERITY_META: Record<GateSeverity, { label: string; cls: string }> = {
  block: { label: '阻断', cls: 'is-block' },
  warn: { label: '告警', cls: 'is-warn' },
  info: { label: '提示', cls: 'is-info' },
}

const CATEGORY_TEXT: Record<GateCategory, string> = {
  syntax: '语法',
  schema: '结构',
  field_mapping: '字段映射',
  resource: '资源',
  policy: '策略',
}

const CATEGORY_ORDER: GateCategory[] = ['syntax', 'schema', 'field_mapping', 'resource', 'policy']
const SEVERITY_ORDER: GateSeverity[] = ['block', 'warn', 'info']

function sortFindings(list: GateFinding[]): GateFinding[] {
  return list.slice().sort((a, b) => {
    const s = SEVERITY_ORDER.indexOf(a.severity) - SEVERITY_ORDER.indexOf(b.severity)
    if (s !== 0) {
      return s
    }
    return CATEGORY_ORDER.indexOf(a.category) - CATEGORY_ORDER.indexOf(b.category)
  })
}

const blocks = computed<GateFinding[]>(() =>
  sortFindings(props.gate?.findings.filter((f) => f.severity === 'block') ?? []),
)
const warns = computed<GateFinding[]>(() =>
  sortFindings(props.gate?.findings.filter((f) => f.severity === 'warn') ?? []),
)
const infos = computed<GateFinding[]>(() =>
  sortFindings(props.gate?.findings.filter((f) => f.severity === 'info') ?? []),
)

const conclusion = computed(() => props.gate?.conclusion ?? null)

/** 定位点的人话描述：四类 GateLocator 全在，缺一类就会有点不动的 finding */
function locatorText(f: GateFinding): string {
  const loc = f.locator
  if (loc.kind === 'hocon') {
    return loc.end_line === undefined
      ? `HOCON L${loc.line}:${loc.column}`
      : `HOCON L${loc.line}:${loc.column}–L${loc.end_line}`
  }
  if (loc.kind === 'plan') {
    return `Plan ${loc.path}`
  }
  if (loc.kind === 'dag_node') {
    return `节点 ${loc.node_id}`
  }
  return `源表 ${loc.schema}.${loc.table}（ds#${loc.data_source_id}）`
}

const infoOpen = ref<boolean>(false)
const warnOpen = ref<boolean>(true)
</script>

<template>
  <div class="gate">
    <EmptyState
      v-if="gate === null || conclusion === null"
      variant="no-data"
      title="尚无门禁结论"
      description="制品产出后会自动跑确定性门禁；结论会连同定位信息出现在这里。"
    />

    <template v-else>
      <div class="gate__head">
        <el-tag :type="CONCLUSION_META[conclusion].tag" size="small" effect="dark">
          {{ CONCLUSION_META[conclusion].label }}
        </el-tag>
        <span class="gate__count gate__count--block">阻断 {{ blocks.length }}</span>
        <span class="gate__count gate__count--warn">告警 {{ warns.length }}</span>
        <span class="gate__count">提示 {{ infos.length }}</span>
      </div>
      <div class="gate__desc">{{ CONCLUSION_META[conclusion].desc }}</div>
      <div class="gate__at">评估于 {{ formatDateTime(gate.evaluated_at) }}</div>

      <!-- 阻断项：不折叠。这一块的存在就是为了让人没法忽略它 -->
      <div v-if="blocks.length > 0" class="gate__blocks">
        <el-alert type="error" :closable="false" show-icon>
          存在 {{ blocks.length }} 项阻断问题，冻结按钮已禁用。修复或重新生成后再评估。
        </el-alert>
        <div
          v-for="f in blocks"
          :key="f.id"
          class="gate__card"
          :class="[SEVERITY_META[f.severity].cls, { 'is-active': f.id === activeFindingId }]"
          @click="emit('activate', f.id)"
        >
          <div class="gate__card-head">
            <el-tag type="danger" size="small" effect="dark">{{ enumLabel('ErrorCode', f.code).label }}</el-tag>
            <span class="gate__cat">{{ CATEGORY_TEXT[f.category] }}</span>
            <span class="gate__locator elt-mono">{{ locatorText(f) }}</span>
            <span class="gate__spacer" />
            <el-button link type="primary" size="small" @click.stop="emit('locate', f.id)">
              <el-icon><Aim /></el-icon>
              <span>定位</span>
            </el-button>
          </div>
          <SensitiveText :value="f.message" multiline class="gate__msg" />
          <div v-if="f.suggestion !== null && f.suggestion !== ''" class="gate__suggestion">
            <span class="gate__suggestion-label">建议</span>
            <SensitiveText :value="f.suggestion" multiline />
          </div>
        </div>
      </div>

      <!-- 告警项：默认展开（它会影响运行监督阈值），可折叠 -->
      <div v-if="warns.length > 0" class="gate__section">
        <button type="button" class="gate__section-head" @click="warnOpen = !warnOpen">
          <span class="gate__caret">{{ warnOpen ? '▾' : '▸' }}</span>
          <span>告警（{{ warns.length }}）</span>
        </button>
        <template v-if="warnOpen">
          <div
            v-for="f in warns"
            :key="f.id"
            class="gate__card is-warn"
            :class="{ 'is-active': f.id === activeFindingId }"
            @click="emit('activate', f.id)"
          >
            <div class="gate__card-head">
              <el-tag type="warning" size="small" effect="dark">{{ enumLabel('ErrorCode', f.code).label }}</el-tag>
              <span class="gate__cat">{{ CATEGORY_TEXT[f.category] }}</span>
              <span class="gate__locator elt-mono">{{ locatorText(f) }}</span>
              <span class="gate__spacer" />
              <el-button link type="primary" size="small" @click.stop="emit('locate', f.id)">
                <el-icon><Aim /></el-icon>
                <span>定位</span>
              </el-button>
            </div>
            <SensitiveText :value="f.message" multiline class="gate__msg" />
            <div v-if="f.suggestion !== null && f.suggestion !== ''" class="gate__suggestion">
              <span class="gate__suggestion-label">建议</span>
              <SensitiveText :value="f.suggestion" multiline />
            </div>
          </div>
        </template>
      </div>

      <!-- 提示项：默认收起，避免把阻断项挤出首屏 -->
      <div v-if="infos.length > 0" class="gate__section">
        <button type="button" class="gate__section-head" @click="infoOpen = !infoOpen">
          <span class="gate__caret">{{ infoOpen ? '▾' : '▸' }}</span>
          <span>提示（{{ infos.length }}）</span>
        </button>
        <template v-if="infoOpen">
          <div
            v-for="f in infos"
            :key="f.id"
            class="gate__card is-info"
            :class="{ 'is-active': f.id === activeFindingId }"
            @click="emit('activate', f.id)"
          >
            <div class="gate__card-head">
              <span class="gate__code elt-mono">{{ f.code }}</span>
              <span class="gate__locator elt-mono">{{ locatorText(f) }}</span>
              <span class="gate__spacer" />
              <el-button link type="primary" size="small" @click.stop="emit('locate', f.id)">定位</el-button>
            </div>
            <SensitiveText :value="f.message" multiline class="gate__msg" />
          </div>
        </template>
      </div>
    </template>
  </div>
</template>

<style scoped>
.gate {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.gate__head {
  display: flex;
  gap: 8px;
  align-items: center;
}

.gate__count {
  font-size: 11px;
  color: var(--elt-text-muted);
}

.gate__count--block {
  color: var(--elt-danger);
}

.gate__count--warn {
  color: var(--elt-warn);
}

.gate__desc {
  font-size: 12px;
  line-height: 17px;
  color: var(--elt-text-secondary);
}

.gate__at {
  font-size: 11px;
  color: var(--elt-text-muted);
}

.gate__blocks {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.gate__card {
  padding: 6px 8px;
  cursor: pointer;
  border: 1px solid var(--elt-border-subtle);
  border-left-width: 3px;
  border-radius: var(--elt-radius-control);
}

.gate__card:hover {
  background-color: var(--elt-bg-elevated);
}

.gate__card.is-active {
  background-color: var(--elt-bg-elevated);
  border-color: var(--elt-accent);
}

.gate__card.is-block {
  border-left-color: var(--elt-danger);
}

.gate__card.is-warn {
  border-left-color: var(--elt-warn);
}

.gate__card.is-info {
  border-left-color: var(--elt-text-muted);
}

.gate__card-head {
  display: flex;
  gap: 6px;
  align-items: center;
  font-size: 11px;
}

.gate__cat {
  color: var(--elt-text-muted);
}

.gate__locator {
  color: var(--elt-text-muted);
}

.gate__code {
  color: var(--elt-text-secondary);
}

.gate__spacer {
  flex: 1;
}

.gate__msg {
  margin-top: 4px;
  font-size: 12px;
  line-height: 17px;
  color: var(--elt-text-primary);
}

.gate__suggestion {
  display: flex;
  gap: 6px;
  margin-top: 4px;
  font-size: 12px;
  line-height: 17px;
  color: var(--elt-text-secondary);
}

.gate__suggestion-label {
  flex: none;
  color: var(--elt-text-muted);
}

.gate__section {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.gate__section-head {
  display: flex;
  gap: 6px;
  align-items: center;
  padding: 2px 0;
  font-size: 12px;
  color: var(--elt-text-secondary);
  cursor: pointer;
  background: none;
  border: none;
}

.gate__caret {
  color: var(--elt-text-muted);
}
</style>
