<script setup lang="ts">
/**
 * 底部版本栏（fe-04 §5.1 的 56–72px 固定条）。
 *
 * 这一条承担「当前在看哪一版」与「能不能把它冻住」两个问题，因此：
 * - **冻结按钮的禁用原因必须写出来**（tooltip + 按钮旁的文字），
 *   只置灰不说明会让人反复点、然后去问为什么；
 * - 禁用只是体验，真正的拦截在后端（门禁阻断、版本不可变、元数据过期都会返 409/403）；
 * - 提交审批需要先冻结：准备单绑的是 `pipeline_version_id`，没有版本就无单可提。
 */
import { computed } from 'vue'
import { Lock, Promotion } from '@element-plus/icons-vue'
import { ArtifactHash, AsyncTaskPanel, DraftFrozenBadge } from '@/components/common'
import { formatDateTime } from '@/utils/format'
import type { FreezeArtifactManifest, PipelineVersionSummary, StudioPhase } from '@/types'

const props = withDefaults(
  defineProps<{
    phase: StudioPhase
    /** 可冻结/可提交的制品标识（frozenVersionId ?? pipeline.current_version_id） */
    workingVersionId: number | null
    frozenVersionId: number | null
    manifest: FreezeArtifactManifest | null
    blockingCount: number
    canFreeze: boolean
    versions: PipelineVersionSummary[]
    versionsLoading: boolean
    /** 当前制品面板展示的是哪一版（null = 最新） */
    viewingVersionId: number | null
    freezeLoading: boolean
    submitLoading: boolean
    submitError: string | null
  }>(),
  { manifest: null, viewingVersionId: null, submitError: null },
)

const emit = defineEmits<{
  freeze: []
  submit: []
  'view-version': [versionId: number | null]
  diff: []
  'reload-versions': []
}>()

const isFrozen = computed<boolean>(
  () => props.frozenVersionId !== null || props.phase === 'frozen' || props.phase === 'submitted',
)

const frozenVersion = computed<PipelineVersionSummary | null>(
  () => props.versions.find((v) => v.id === props.frozenVersionId) ?? null,
)

const viewing = computed<PipelineVersionSummary | null>(
  () => props.versions.find((v) => v.id === props.viewingVersionId) ?? null,
)

/** 冻结禁用原因：按「先看是否已完成 → 再看阻断 → 再看阶段 → 最后看制品标识」的顺序给最贴切的一条 */
const freezeDisabledReason = computed<string>(() => {
  if (isFrozen.value) {
    return '本版本已冻结，不可重复冻结；如需修正请生成新版本'
  }
  if (props.blockingCount > 0) {
    return `存在 ${props.blockingCount} 项阻断问题，冻结按钮已按门禁结论禁用`
  }
  if (props.phase !== 'ready_to_freeze') {
    return '当前阶段不在「可冻结」，请先完成生成与门禁评估'
  }
  if (props.workingVersionId === null) {
    return '服务端尚未返回可冻结的制品标识，暂不能冻结'
  }
  return ''
})

const freezeDisabled = computed<boolean>(() => freezeDisabledReason.value !== '' || props.freezeLoading)

const submitDisabledReason = computed<string>(() => {
  if (!isFrozen.value) {
    return '需先冻结版本：准备单绑定的是不可变版本，没有版本就无单可提'
  }
  if (props.phase === 'submitted') {
    return '本次会话已提交审批，不可重复提交'
  }
  return ''
})

const submitDisabled = computed<boolean>(
  () => submitDisabledReason.value !== '' || props.submitLoading,
)
</script>

<template>
  <div class="vbar">
    <div class="vbar__left">
      <DraftFrozenBadge
        :frozen="isFrozen"
        :frozen-at="frozenVersion?.frozen_at ?? null"
        :frozen-by="frozenVersion?.frozen_by_display_name ?? null"
      />
      <span v-if="workingVersionId !== null" class="vbar__id elt-mono">制品 #{{ workingVersionId }}</span>
      <ArtifactHash v-if="manifest !== null" :hash="manifest.hocon.sha256" display="truncate" label="HOCON" />
      <el-tooltip v-if="frozenVersion !== null" :content="frozenVersion.status_note" placement="top">
        <span class="vbar__note">{{ frozenVersion.status_note }}</span>
      </el-tooltip>
    </div>

    <div class="vbar__mid">
      <el-select
        :model-value="viewingVersionId"
        size="small"
        class="vbar__select"
        placeholder="查看最新制品"
        clearable
        :loading="versionsLoading"
        @update:model-value="(v: number | null) => emit('view-version', v)"
      >
        <el-option v-for="v in versions" :key="v.id" :value="v.id" :label="`v${v.version_no} · ${formatDateTime(v.frozen_at)}`" />
      </el-select>
      <el-button size="small" :disabled="versions.length < 2" @click="emit('diff')">版本对比</el-button>
      <el-button link size="small" @click="emit('reload-versions')">刷新版本</el-button>
      <span v-if="viewing !== null" class="vbar__viewing">正在查看历史版本 v{{ viewing.version_no }}</span>
    </div>

    <div class="vbar__right">
      <span v-if="freezeDisabledReason !== ''" class="vbar__reason">{{ freezeDisabledReason }}</span>
      <span v-if="submitDisabledReason !== ''" class="vbar__reason">{{ submitDisabledReason }}</span>

      <el-tooltip :content="freezeDisabledReason" :disabled="freezeDisabledReason === ''" placement="top">
        <span>
          <el-button
            type="primary"
            size="small"
            :loading="freezeLoading"
            :disabled="freezeDisabled"
            @click="emit('freeze')"
          >
            <el-icon><Lock /></el-icon>
            <span>{{ isFrozen ? '已冻结' : '冻结版本' }}</span>
          </el-button>
        </span>
      </el-tooltip>

      <el-tooltip :content="submitDisabledReason" :disabled="submitDisabledReason === ''" placement="top">
        <span>
          <el-button size="small" :loading="submitLoading" :disabled="submitDisabled" @click="emit('submit')">
            <el-icon><Promotion /></el-icon>
            <span>提交审批</span>
          </el-button>
        </span>
      </el-tooltip>
    </div>

    <AsyncTaskPanel :loading="submitLoading" :error="submitError" loading-text="创建准备单…" />
  </div>
</template>

<style scoped>
.vbar {
  display: flex;
  gap: 16px;
  align-items: center;
  height: 100%;
  padding: 0 4px;
}

.vbar__left,
.vbar__mid,
.vbar__right {
  display: flex;
  gap: 8px;
  align-items: center;
  min-width: 0;
}

.vbar__left {
  flex: 1;
}

.vbar__mid {
  flex: none;
}

.vbar__right {
  flex: 1;
  justify-content: flex-end;
}

.vbar__id {
  font-size: 11px;
  color: var(--elt-text-muted);
}

.vbar__note {
  max-width: 160px;
  overflow: hidden;
  font-size: 11px;
  color: var(--elt-text-secondary);
  text-overflow: ellipsis;
  white-space: nowrap;
}

.vbar__select {
  width: 200px;
}

.vbar__viewing {
  font-size: 11px;
  color: var(--elt-warn);
}

.vbar__reason {
  font-size: 11px;
  color: var(--elt-warn);
  text-align: right;
}
</style>
