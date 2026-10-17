<script setup lang="ts">
/**
 * Studio 阶段指示器（fe-04 §5.4）。
 *
 * `StudioPhase` 是**页面临时阶段、不落库**（types/pipeline.ts 顶部注释），
 * 因此它不在 `EnumKind` 里，`@/utils/enums` 也拿不到它的中文——
 * 阶段文案只能由本页维护（这是刻意的例外，不是漏用枚举工具）。
 *
 * 命名走「过程」而不是「结果」：`frozen` 表示本次请求周期已经冻结出制品，
 * 管道的业务状态要看 `PipelineStatus`，两者不可互相推导。
 */
import { computed } from 'vue'
import { Refresh } from '@element-plus/icons-vue'
import type { StudioPhase } from '@/types'

type Tone = 'info' | 'primary' | 'success' | 'warning' | 'danger'

interface PhaseMeta {
  label: string
  tone: Tone
  /** 在步骤条上的位置；-1 表示不属于线性流程（idle / error） */
  step: number
  /** 是否处于「进行中」（用于加转圈动画） */
  busy: boolean
}

const props = withDefaults(
  defineProps<{
    phase: StudioPhase
    /** 后端给的阶段说明（fe-04 §5.4：必须原样展示，便于对不上时排查） */
    message?: string | null
    /** 紧凑模式：只出徽章，不出步骤条（底部版本栏用） */
    compact?: boolean
  }>(),
  { message: null, compact: false },
)

const STEPS: string[] = ['解析需求', '澄清', '生成', '门禁', '待冻结', '已冻结', '已提交']

const PHASE_META: Record<StudioPhase, PhaseMeta> = {
  idle: { label: '未开始', tone: 'info', step: -1, busy: false },
  parsing: { label: '解析需求中', tone: 'primary', step: 0, busy: true },
  awaiting_clarification: { label: '待澄清', tone: 'warning', step: 1, busy: false },
  generating: { label: '生成中', tone: 'primary', step: 2, busy: true },
  gate_running: { label: '门禁评估中', tone: 'primary', step: 3, busy: true },
  gate_failed: { label: '门禁未通过', tone: 'danger', step: 3, busy: false },
  ready_to_freeze: { label: '可冻结', tone: 'success', step: 4, busy: false },
  frozen: { label: '已冻结', tone: 'success', step: 5, busy: false },
  submitted: { label: '已提交审批', tone: 'success', step: 6, busy: false },
  error: { label: '异常中断', tone: 'danger', step: -1, busy: false },
}

/** 未知阶段兜底：后端加了新枚举而前端没跟上时，宁可显示原文也不要显示空白 */
const meta = computed<PhaseMeta>(
  () => PHASE_META[props.phase] ?? { label: String(props.phase), tone: 'info', step: -1, busy: false },
)

function stepClass(index: number): string {
  const current = meta.value.step
  if (current < 0) {
    return 'is-pending'
  }
  if (index < current) {
    return 'is-done'
  }
  if (index === current) {
    return meta.value.tone === 'danger' ? 'is-failed' : 'is-active'
  }
  return 'is-pending'
}
</script>

<template>
  <div class="phase">
    <div class="phase__head">
      <el-tag :type="meta.tone === 'primary' ? 'primary' : meta.tone" size="small" effect="dark">
        {{ meta.label }}
      </el-tag>
      <el-icon v-if="meta.busy" class="phase__spin" :size="12"><Refresh /></el-icon>
    </div>

    <!-- phase_message 是后端对「为什么停在这一步」的说明，原样展示不做任何加工 -->
    <div v-if="message !== null && message !== '' && !compact" class="phase__message">
      {{ message }}
    </div>

    <ol v-if="!compact" class="phase__steps">
      <li v-for="(s, i) in STEPS" :key="s" class="phase__step" :class="stepClass(i)">
        <span class="phase__dot" />
        <span class="phase__label">{{ s }}</span>
      </li>
    </ol>
  </div>
</template>

<style scoped>
.phase {
  min-width: 0;
}

.phase__head {
  display: flex;
  gap: 6px;
  align-items: center;
}

.phase__spin {
  color: var(--elt-accent);
  animation: phase-spin 1.4s linear infinite;
}

@keyframes phase-spin {
  to {
    transform: rotate(360deg);
  }
}

@media (prefers-reduced-motion: reduce) {
  .phase__spin {
    animation: none;
  }
}

.phase__message {
  margin-top: 4px;
  font-size: 12px;
  line-height: 16px;
  color: var(--elt-text-secondary);
}

.phase__steps {
  display: flex;
  gap: 0;
  align-items: center;
  padding: 0;
  margin: 8px 0 0;
  list-style: none;
}

.phase__step {
  display: flex;
  gap: 4px;
  align-items: center;
  font-size: 11px;
  color: var(--elt-text-muted);
}

/* 步骤之间用连线表示「有先后」，只做视觉，不代表进度百分比 */
.phase__step + .phase__step::before {
  width: 12px;
  height: 1px;
  margin: 0 4px;
  content: '';
  background-color: var(--elt-border-subtle);
}

.phase__dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background-color: var(--elt-border-subtle);
}

.phase__step.is-done .phase__dot {
  background-color: var(--elt-success);
}

.phase__step.is-active .phase__dot {
  background-color: var(--elt-accent);
  box-shadow: 0 0 0 3px var(--elt-badge-accent);
}

.phase__step.is-failed .phase__dot {
  background-color: var(--elt-danger);
  box-shadow: 0 0 0 3px var(--elt-badge-danger);
}

.phase__step.is-active .phase__label,
.phase__step.is-failed .phase__label {
  color: var(--elt-text-primary);
}
</style>
