<script setup lang="ts">
/**
 * 澄清问答面板（fe-04 §6.1）。
 *
 * 问题的种类、选项、必填性**全部来自后端**（LangGraph 的 interrupt payload），
 * 前端只负责把每一类渲染成合适的控件，并如实回报「用户答了什么」。
 *
 * `field_ref` 刻意不给手工输入：它的取值必须来自元数据探查快照
 * （`GET /datasources/{id}/profiles/{v}/schema`），否则用户会手打出
 * 一个不存在的字段名，把错误推迟到门禁阶段才暴露。
 *
 * `Record<string,string>` 的答案载体决定了 `multi_choice` 用逗号连接——
 * 这是当前契约下唯一能塞进字符串数组的表示，已在报告中登记为待确认项。
 */
import { computed } from 'vue'
import { ArrowRight, Check } from '@element-plus/icons-vue'
import { AsyncTaskPanel, EmptyState, SensitiveText } from '@/components/common'
import type { ClarificationQuestion } from '@/types'

/** 与视图侧构造的字段候选项对齐（跨组件传递，各自声明结构） */
interface FieldOption {
  value: string
  label: string
  group: string
}

const props = withDefaults(
  defineProps<{
    questions: ClarificationQuestion[]
    answers: Record<string, string>
    submitting: boolean
    error: string | null
    fieldOptions: FieldOption[]
    fieldOptionsLoading: boolean
    /** 元数据探查未就绪时给出可执行的下一步，而不是一个空下拉 */
    fieldOptionsError: string | null
  }>(),
  { error: null, fieldOptionsError: null },
)

const emit = defineEmits<{
  answer: [key: string, value: string]
  submit: []
}>()

const requiredLeft = computed<number>(
  () =>
    props.questions.filter((q) => {
      if (!q.required) {
        return false
      }
      const v = props.answers[q.key]
      return v === undefined || v.trim() === ''
    }).length,
)

const canSubmit = computed<boolean>(() => !props.submitting && requiredLeft.value === 0)

/** field_ref 候选按表分组：一个数据源几十张表，平铺下拉根本没法找 */
const fieldGroups = computed<Array<{ group: string; options: FieldOption[] }>>(() => {
  const map = new Map<string, FieldOption[]>()
  for (const o of props.fieldOptions) {
    const list = map.get(o.group)
    if (list === undefined) {
      map.set(o.group, [o])
    } else {
      list.push(o)
    }
  }
  return Array.from(map.entries()).map(([group, options]) => ({ group, options }))
})

/** 已作答项预览：提交前让用户看清「到底会把这些字段发出去」 */
const answered = computed<Array<{ key: string; question: string; value: string }>>(() =>
  props.questions
    .filter((q) => {
      const v = props.answers[q.key]
      return v !== undefined && v.trim() !== ''
    })
    .map((q) => ({ key: q.key, question: q.question, value: props.answers[q.key] ?? '' })),
)

function textValue(key: string): string {
  return props.answers[key] ?? ''
}

function onText(key: string, value: string): void {
  emit('answer', key, value)
}

/** 多选在字符串答案里以逗号分隔存放 */
function multiValue(key: string): string[] {
  const raw = props.answers[key]
  if (raw === undefined || raw.trim() === '') {
    return []
  }
  return raw.split(',').map((s) => s.trim()).filter((s) => s !== '')
}

function onMulti(key: string, values: string[] | string): void {
  const list = Array.isArray(values) ? values : [values]
  emit('answer', key, list.join(','))
}

function numberValue(key: string): number | undefined {
  const raw = props.answers[key]
  if (raw === undefined || raw.trim() === '') {
    return undefined
  }
  const n = Number(raw)
  return Number.isFinite(n) ? n : undefined
}

/** 非数字输入不落库：宁可保留上一个合法值，也不要把 NaN 发进工作流 */
function onNumber(key: string, value: number | undefined): void {
  if (value === undefined || value === null || !Number.isFinite(value)) {
    emit('answer', key, '')
    return
  }
  emit('answer', key, String(value))
}

/** 阈值范围：后端没给 min/max 时按「比例 0<x≤1」兜底并显式标注待确认（§6.1） */
function isRatio(q: ClarificationQuestion): boolean {
  if (q.meta?.ratio === true) {
    return true
  }
  return q.meta?.min === undefined && q.meta?.max === undefined
}

function minOf(q: ClarificationQuestion): number {
  if (q.meta?.min !== undefined) {
    return q.meta.min
  }
  return isRatio(q) ? 0 : 1
}

function maxOf(q: ClarificationQuestion): number | undefined {
  if (q.meta?.max !== undefined) {
    return q.meta.max
  }
  return isRatio(q) ? 1 : undefined
}

function stepOf(q: ClarificationQuestion): number {
  return isRatio(q) ? 0.01 : 1
}

function rangeNote(q: ClarificationQuestion): string | null {
  if (q.kind !== 'threshold') {
    return null
  }
  const unit = q.meta?.unit ?? ''
  if (isRatio(q)) {
    return `范围待确认：暂按 0 < 值 ≤ 1 处理${unit === '' ? '' : `（${unit}）`}`
  }
  return null
}
</script>

<template>
  <div class="clar">
    <EmptyState
      v-if="questions.length === 0"
      variant="no-data"
      title="暂无待澄清问题"
      description="需求已解析或在等待上一步完成；出现新问题时这里会自动刷新。"
    />

    <template v-else>
      <div v-for="(q, index) in questions" :key="q.key" class="clar__item">
        <div class="clar__q">
          <span class="clar__no">{{ index + 1 }}</span>
          <span class="clar__text">{{ q.question }}</span>
          <span v-if="q.required" class="clar__star" title="必答">*</span>
        </div>

        <div v-if="q.hint !== null && q.hint !== ''" class="clar__hint">
          <SensitiveText :value="q.hint" multiline />
        </div>

        <!-- text -->
        <el-input
          v-if="q.kind === 'text'"
          :model-value="textValue(q.key)"
          type="textarea"
          :rows="3"
          size="small"
          :placeholder="'请描述：' + q.question"
          @update:model-value="(v: string) => onText(q.key, v)"
        />

        <!-- single_choice -->
        <el-radio-group
          v-else-if="q.kind === 'single_choice'"
          :model-value="textValue(q.key)"
          size="small"
          class="clar__radios"
          @update:model-value="(v: string | number | boolean | undefined) => onText(q.key, String(v ?? ''))"
        >
          <el-radio v-for="o in q.options ?? []" :key="o.value" :value="o.value">{{ o.label }}</el-radio>
        </el-radio-group>

        <!-- multi_choice：答案载体是字符串，故用逗号连接（见文件头注释） -->
        <el-select
          v-else-if="q.kind === 'multi_choice'"
          :model-value="multiValue(q.key)"
          multiple
          collapse-tags
          collapse-tags-tooltip
          size="small"
          class="clar__control"
          placeholder="可多选"
          @update:model-value="(v: string[]) => onMulti(q.key, v)"
        >
          <el-option v-for="o in q.options ?? []" :key="o.value" :value="o.value" :label="o.label" />
        </el-select>

        <!-- field_ref：只允许从探查快照里选，禁止手工输入 -->
        <div v-else-if="q.kind === 'field_ref'" class="clar__field-ref">
          <el-select
            :model-value="textValue(q.key)"
            filterable
            :loading="fieldOptionsLoading"
            :disabled="fieldOptions.length === 0"
            size="small"
            class="clar__control"
            :placeholder="fieldOptions.length === 0 ? '元数据未就绪' : '从探查快照中选择字段'"
            @update:model-value="(v: string) => onText(q.key, v)"
          >
            <el-option-group v-for="g in fieldGroups" :key="g.group" :label="g.group">
              <el-option v-for="o in g.options" :key="o.value" :value="o.value" :label="o.label" />
            </el-option-group>
          </el-select>
          <div v-if="fieldOptionsError !== null" class="clar__hint clar__hint--warn">
            {{ fieldOptionsError }}
          </div>
          <div v-else-if="fieldOptions.length === 0 && !fieldOptionsLoading" class="clar__hint clar__hint--warn">
            该数据源尚无可用探查快照，请先到「数据源」页完成元数据探查后再回来作答。
          </div>
        </div>

        <!-- threshold -->
        <div v-else-if="q.kind === 'threshold'" class="clar__threshold">
          <el-input-number
            :model-value="numberValue(q.key)"
            :min="minOf(q)"
            :max="maxOf(q)"
            :step="stepOf(q)"
            :precision="isRatio(q) ? 2 : 0"
            size="small"
            controls-position="right"
            @update:model-value="(v: number | undefined) => onNumber(q.key, v)"
          />
          <span v-if="q.meta?.unit" class="clar__unit">{{ q.meta.unit }}</span>
          <span v-if="rangeNote(q) !== null" class="clar__note">{{ rangeNote(q) }}</span>
        </div>

        <!-- 未知 kind 兜底成文本，至少让用户能作答，而不是整块渲染不出来 -->
        <el-input
          v-else
          :model-value="textValue(q.key)"
          size="small"
          class="clar__control"
          placeholder="（未知题型，按文本作答）"
          @update:model-value="(v: string) => onText(q.key, v)"
        />
      </div>

      <div v-if="answered.length > 0" class="clar__preview">
        <div class="clar__preview-title">本次将提交的字段</div>
        <div v-for="a in answered" :key="a.key" class="clar__preview-row">
          <span class="clar__preview-key elt-mono">{{ a.key }}</span>
          <el-icon :size="11"><ArrowRight /></el-icon>
          <span class="clar__preview-value elt-mono">{{ a.value }}</span>
        </div>
      </div>

      <div class="clar__actions">
        <el-button type="primary" size="small" :loading="submitting" :disabled="!canSubmit" @click="emit('submit')">
          <el-icon><Check /></el-icon>
          <span>提交澄清</span>
        </el-button>
        <span v-if="requiredLeft > 0" class="clar__left">还有 {{ requiredLeft }} 项必答未填写</span>
      </div>

      <AsyncTaskPanel :loading="submitting" :error="error" loading-text="提交中…" />
    </template>
  </div>
</template>

<style scoped>
.clar {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.clar__item {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding-bottom: 10px;
  border-bottom: 1px solid var(--elt-border-subtle);
}

.clar__item:last-of-type {
  border-bottom: none;
}

.clar__q {
  display: flex;
  gap: 6px;
  align-items: baseline;
  font-size: 12px;
  font-weight: 600;
  line-height: 17px;
  color: var(--elt-text-primary);
}

.clar__no {
  flex: none;
  min-width: 16px;
  font-size: 11px;
  color: var(--elt-text-muted);
}

.clar__text {
  flex: 1;
  min-width: 0;
}

.clar__star {
  color: var(--elt-danger);
}

.clar__hint {
  font-size: 11px;
  line-height: 16px;
  color: var(--elt-text-muted);
}

.clar__hint--warn {
  color: var(--elt-warn);
}

.clar__control {
  width: 100%;
}

.clar__radios {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 12px;
}

.clar__threshold {
  display: flex;
  gap: 8px;
  align-items: center;
}

.clar__unit {
  font-size: 12px;
  color: var(--elt-text-secondary);
}

.clar__note {
  font-size: 11px;
  color: var(--elt-warn);
}

.clar__preview {
  padding: 8px;
  background-color: var(--elt-bg-elevated);
  border: 1px dashed var(--elt-border-subtle);
  border-radius: var(--elt-radius-control);
}

.clar__preview-title {
  margin-bottom: 4px;
  font-size: 11px;
  color: var(--elt-text-secondary);
}

.clar__preview-row {
  display: flex;
  gap: 6px;
  align-items: center;
  font-size: 11px;
  line-height: 16px;
  color: var(--elt-text-secondary);
}

.clar__preview-key {
  flex: none;
  max-width: 40%;
  overflow: hidden;
  color: var(--elt-text-muted);
  text-overflow: ellipsis;
  white-space: nowrap;
}

.clar__preview-value {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  color: var(--elt-text-primary);
  text-overflow: ellipsis;
  white-space: nowrap;
}

.clar__actions {
  display: flex;
  gap: 8px;
  align-items: center;
}

.clar__left {
  font-size: 11px;
  color: var(--elt-warn);
}
</style>
