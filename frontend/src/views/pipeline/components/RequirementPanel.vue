<script setup lang="ts">
/**
 * 左栏「输入区」（fe-04 §5.1 / §5.2）：源、目标、自然语言需求 → 发起生成。
 *
 * 表单值（需求原文、两个下拉）是**待提交的入参**而不是 Studio 状态：
 * store 的 `startGeneration(projectId, requirement, sourceId, targetId)` 收的就是它们，
 * 因此只能由本组件持有——也正因为如此，**它们不进 localStorage**：
 * 需求原文里可能带表名/字段名甚至样例数据，落盘会绕过全站的脱敏口径（fe-04 §8）。
 * 代价是刷新后要重填（或从后端快照恢复），这是刻意选择。
 *
 * 本组件不做任何「哪个类型能当 sink」的判断（store 的注释已说明由后端裁决），
 * 只做「必填 + 非空」这类纯表单校验。
 */
import { computed, ref, watch } from 'vue'
import { MagicStick } from '@element-plus/icons-vue'
import { AsyncTaskPanel, ConfirmDialog, EnvBadge } from '@/components/common'
import { enumText } from '@/utils/enums'
import type { DataSourceOption, Env, Pipeline, StartGenerationPayload } from '@/types'

const props = withDefaults(
  defineProps<{
    options: DataSourceOption[]
    pipeline: Pipeline | null
    /** 已冻结/已提交后整页只读：输入区不再可编辑 */
    readonly: boolean
    loading: boolean
    /** 写操作失败信息（原样来自 store.lastError，已脱敏） */
    error: string | null
    /** 有没有可中止的生成线程 */
    canAbort: boolean
  }>(),
  { error: null, canAbort: false },
)

const emit = defineEmits<{
  start: [payload: StartGenerationPayload]
  abort: [reason: string]
  'reload-options': []
}>()

const requirement = ref<string>('')
const sourceId = ref<number | null>(null)
const targetId = ref<number | null>(null)
const abortVisible = ref<boolean>(false)

/**
 * 只在「用户还没动过」时用 Pipeline 上已记录的值预填。
 * 若每次都同步，用户改完下拉再刷新一次列表就会被悄悄改回去。
 */
const seeded = ref<boolean>(false)
watch(
  () => props.pipeline,
  (p) => {
    if (p === null || seeded.value) {
      return
    }
    sourceId.value = p.source_data_source_id
    targetId.value = p.target_data_source_id
    seeded.value = true
  },
  { immediate: true },
)

const sourceName = computed<string>(
  () => props.options.find((o) => o.id === sourceId.value)?.name ?? '',
)

/** 源目标同名只是「值得提醒」，拦不拦由后端门禁结论说话（前端不预设合法性） */
const sameEndpoint = computed<boolean>(
  () => sourceId.value !== null && sourceId.value === targetId.value,
)

/**
 * DataSourceOption.env 是 string（后端枚举按 string 收），
 * 而 EnvBadge 只认 Env 三值。这里断言是刻意的：拿不准的取值会走
 * EnvBadge 内部的兜底渲染，好过为了消类型错误把整个下拉改成文本。
 */
function asEnv(env: string): Env {
  return env as Env
}

const canSubmit = computed<boolean>(
  () =>
    !props.readonly &&
    !props.loading &&
    requirement.value.trim().length >= 5 &&
    sourceId.value !== null &&
    targetId.value !== null,
)

function submit(): void {
  if (!canSubmit.value || sourceId.value === null || targetId.value === null) {
    return
  }
  emit('start', {
    requirement: requirement.value.trim(),
    source_data_source_id: sourceId.value,
    target_data_source_id: targetId.value,
  })
}
</script>

<template>
  <div class="req">
    <div class="req__row">
      <label class="req__label">源数据源</label>
      <el-select
        v-model="sourceId"
        class="req__select"
        size="small"
        filterable
        placeholder="选择源"
        :disabled="readonly"
      >
        <el-option
          v-for="o in options"
          :key="o.id"
          :value="o.id"
          :label="o.name"
        >
          <span class="req__opt">
            <span class="req__opt-name">{{ o.name }}</span>
            <span class="req__opt-meta">{{ enumText('DataSourceType', o.type) }}</span>
            <EnvBadge :env="asEnv(o.env)" />
          </span>
        </el-option>
      </el-select>
    </div>

    <div class="req__row">
      <label class="req__label">目标数据源</label>
      <el-select
        v-model="targetId"
        class="req__select"
        size="small"
        filterable
        placeholder="选择目标"
        :disabled="readonly"
      >
        <el-option
          v-for="o in options"
          :key="o.id"
          :value="o.id"
          :label="o.name"
        >
          <span class="req__opt">
            <span class="req__opt-name">{{ o.name }}</span>
            <span class="req__opt-meta">{{ enumText('DataSourceType', o.type) }}</span>
            <EnvBadge :env="asEnv(o.env)" />
          </span>
        </el-option>
      </el-select>
    </div>

    <!-- 选项为空说明数据源列表没加载或都被停用了：给出下一步而不是空下拉 -->
    <div v-if="options.length === 0" class="req__hint">
      <span>暂无可用数据源。</span>
      <el-button link type="primary" size="small" @click="emit('reload-options')">重新加载</el-button>
    </div>

    <div class="req__row req__row--block">
      <label class="req__label">自然语言需求</label>
      <el-input
        v-model="requirement"
        type="textarea"
        :rows="5"
        size="small"
        resize="vertical"
        maxlength="2000"
        show-word-limit
        :disabled="readonly"
        placeholder="例：把 ods.payment 的支付流水同步到 doris 的 dwd_payment_daily，金额脱敏，只保留最近 30 天"
      />
      <div class="req__hint req__hint--muted">
        需求原文不会被保存到浏览器；发起后由后端工作流持有，刷新用线程快照恢复。
      </div>
    </div>

    <!-- 源目标相同大概率是选错，但放行并如实说明由后端裁决 -->
    <el-alert v-if="sameEndpoint && !readonly" type="warning" :closable="false" show-icon>
      源与目标选择了同一个数据源（{{ sourceName }}）。是否合法以门禁结论为准。
    </el-alert>

    <div v-if="pipeline && pipeline.description !== ''" class="req__row req__row--block">
      <label class="req__label">已登记的需求</label>
      <div class="req__saved">{{ pipeline.description }}</div>
    </div>

    <div class="req__actions">
      <el-button
        type="primary"
        size="small"
        :loading="loading"
        :disabled="!canSubmit"
        @click="submit"
      >
        <el-icon><MagicStick /></el-icon>
        <span>{{ pipeline && pipeline.current_version_id !== null ? '重新生成' : '发起生成' }}</span>
      </el-button>
      <el-button
        v-if="canAbort"
        size="small"
        :disabled="readonly || loading"
        @click="abortVisible = true"
      >
        中止本次生成
      </el-button>
    </div>

    <AsyncTaskPanel :loading="loading" :error="error" loading-text="工作流执行中…" />

    <!-- 中止会丢弃当前进度，必须留理由（进入证据账本） -->
    <ConfirmDialog
      v-model="abortVisible"
      title="中止本次生成"
      description="中止后当前线程不再推进，已生成的中间结果不进入版本历史。请填写中止理由。"
      require-reason
      :min-reason-length="5"
      @confirm="(reason: string) => emit('abort', reason)"
    />
  </div>
</template>

<style scoped>
.req {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.req__row {
  display: flex;
  gap: 8px;
  align-items: center;
}

.req__row--block {
  flex-direction: column;
  align-items: stretch;
  gap: 4px;
}

.req__label {
  flex: none;
  width: 70px;
  font-size: 12px;
  color: var(--elt-text-secondary);
}

.req__select {
  flex: 1;
  min-width: 0;
}

.req__opt {
  display: flex;
  gap: 6px;
  align-items: center;
}

.req__opt-name {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
}

.req__opt-meta {
  font-size: 11px;
  color: var(--elt-text-muted);
}

.req__hint {
  display: flex;
  gap: 6px;
  align-items: center;
  font-size: 11px;
  color: var(--elt-warn);
}

.req__hint--muted {
  color: var(--elt-text-muted);
}

.req__saved {
  max-height: 96px;
  overflow: auto;
  font-size: 12px;
  line-height: 18px;
  color: var(--elt-text-secondary);
  white-space: pre-wrap;
}

.req__actions {
  display: flex;
  gap: 8px;
}
</style>
