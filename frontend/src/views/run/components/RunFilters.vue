<script setup lang="ts">
/**
 * 运行列表筛选栏（fe-06 §6）。
 *
 * 状态是多选：排障时最常见的诉求是「把失败的和硬中断的都列出来」，
 * 单选会逼用户查两次并在脑子里做并集——而人脑做并集正是漏项的地方。
 *
 * 状态下拉的取值来自 `enumOptions('RunStatus')`（唯一口径），
 * 收窄回 `RunStatus[]` 时用同一份取值做类型守卫，不手抄枚举。
 */
import { computed, ref, watch } from 'vue'
import { enumOptions } from '@/utils/enums'
import type { RunFilter, RunStatus } from '@/types'

const props = defineProps<{ filters: RunFilter }>()

const emit = defineEmits<{
  'update:filters': [filters: RunFilter]
  reset: []
}>()

/** 取值列表的唯一来源：utils/enums 的 RunStatus 映射表 */
const RUN_STATUS_VALUES: readonly string[] = enumOptions('RunStatus').map((o) => o.value)

function toRunStatuses(values: string[]): RunStatus[] {
  return values.filter((v): v is RunStatus => RUN_STATUS_VALUES.includes(v))
}

const status = ref<string[]>([...(props.filters.status ?? [])])
const pipelineId = ref<string>(props.filters.pipeline_id === undefined ? '' : String(props.filters.pipeline_id))
const keyword = ref<string>(props.filters.q ?? '')
const range = ref<[string, string] | null>(
  props.filters.from !== undefined && props.filters.to !== undefined
    ? [props.filters.from, props.filters.to]
    : null,
)

// 父级可能因「清空筛选」而重置 filters，这里跟随回填，避免界面与实际条件不一致
watch(
  () => props.filters,
  (next) => {
    status.value = [...(next.status ?? [])]
    pipelineId.value = next.pipeline_id === undefined ? '' : String(next.pipeline_id)
    keyword.value = next.q ?? ''
    range.value = next.from !== undefined && next.to !== undefined ? [next.from, next.to] : null
  },
)

const statusOptions = computed(() => enumOptions('RunStatus'))

function build(): RunFilter {
  const parsedPipeline = Number(pipelineId.value)
  return {
    status: status.value.length > 0 ? toRunStatuses(status.value) : undefined,
    pipeline_id: pipelineId.value.trim() !== '' && Number.isFinite(parsedPipeline) ? parsedPipeline : undefined,
    from: range.value?.[0],
    to: range.value?.[1],
    q: keyword.value.trim() === '' ? undefined : keyword.value.trim(),
    page: 1,
    page_size: props.filters.page_size,
  }
}

function submit(): void {
  // 父级收到新条件即会重新查询（useListQuery.patchFilter 内含一次 load），
  // 因此不需要额外的 submit 事件
  emit('update:filters', build())
}

function reset(): void {
  status.value = []
  pipelineId.value = ''
  keyword.value = ''
  range.value = null
  emit('reset')
}
</script>

<template>
  <div class="run-filters">
    <el-select
      v-model="status"
      class="run-filters__field run-filters__field--status"
      size="small"
      multiple
      collapse-tags
      collapse-tags-tooltip
      clearable
      placeholder="全部状态"
    >
      <el-option v-for="opt in statusOptions" :key="opt.value" :value="opt.value" :label="opt.label" />
    </el-select>

    <el-input
      v-model="pipelineId"
      class="run-filters__field run-filters__field--pid"
      size="small"
      placeholder="管线 ID"
      clearable
    />

    <el-date-picker
      v-model="range"
      class="run-filters__field run-filters__field--range"
      size="small"
      type="datetimerange"
      value-format="YYYY-MM-DDTHH:mm:ss"
      start-placeholder="开始时间"
      end-placeholder="结束时间"
      :teleported="false"
    />

    <el-input
      v-model="keyword"
      class="run-filters__field run-filters__field--kw"
      size="small"
      placeholder="管线名 / run id"
      clearable
      @keyup.enter="submit"
    />

    <el-button size="small" type="primary" @click="submit">查询</el-button>
    <el-button size="small" @click="reset">清空</el-button>
  </div>
</template>

<style scoped>
.run-filters {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
  padding: 12px 14px;
  background-color: var(--elt-bg-surface);
  border: 1px solid var(--elt-border-subtle);
  border-radius: var(--elt-radius-card);
}

.run-filters__field--status {
  width: 240px;
}

.run-filters__field--pid {
  width: 120px;
}

.run-filters__field--range {
  width: 340px;
}

.run-filters__field--kw {
  width: 200px;
}

@media (max-width: 900px) {
  .run-filters__field--range {
    width: 100%;
  }
}
</style>
