<script setup lang="ts">
/**
 * 冻结版本的二次确认（CLAUDE.md §5.4：不可变制品禁止 UPDATE）。
 *
 * 冻结是**不可逆**的：`pipeline_version` / `preparation` / `capability` 只允许 INSERT，
 * 出了问题只能冻结一个新版本，旧版本永远躺在那里。所以确认框必须把
 * 「即将被冻住的是什么」摊开来给人看，而不是问一句「确定吗」。
 *
 * 清单内容全部是只读数据（Plan 的 schema_version/valid、HOCON 的 sha256、
 * 质量规则数、运行预算、元数据快照版本与 sha256），前端不算任何哈希。
 *
 * 用 ConfirmDialog 而不是 DangerConfirmDialog：冻结要求输入的理由本来就是
 * 版本说明（会随版本留痕），再加一道逐字短语确认只会让高频动作变得讨厌。
 */
import { computed } from 'vue'
import { ArtifactHash, AsyncTaskPanel, ConfirmDialog } from '@/components/common'
import { DASH, formatBytes, formatDuration, formatRatioPercent, formatRows } from '@/utils/format'
import type { FreezeArtifactManifest } from '@/types'

const props = withDefaults(
  defineProps<{
    modelValue: boolean
    /** 冻结前由已有制品拼出的清单预览；取不到就只显示「未知」而不是编造 */
    preview: FreezeArtifactManifest | null
    /** 源数据源的元数据快照是否已过期：过期时冻结会被后端拒（METADATA_PROFILE_EXPIRED） */
    profileExpired: boolean
    loading: boolean
    /**
     * 冻结失败的原文（门禁阻断 409 / 版本不可变 / 快照过期）。
     * 显示在弹窗里而不是关掉弹窗后再提示：理由是用户刚填的，
     * 失败就把弹窗关掉等于让他重填一遍。
     */
    error: string | null
  }>(),
  { preview: null, profileExpired: false, error: null },
)

const emit = defineEmits<{
  'update:modelValue': [value: boolean]
  confirm: [reason: string]
}>()

const rows = computed<Array<{ label: string; value: string }>>(() => {
  const p = props.preview
  if (p === null) {
    return []
  }
  return [
    { label: 'Plan 版本', value: `${p.plan.schema_version}（${p.plan.valid ? '结构校验通过' : '结构校验未通过'}）` },
    { label: 'HOCON parser', value: p.hocon.parser_version },
    { label: '质量规则数', value: String(p.quality_contract.rule_count) },
    { label: '拒绝率预算', value: formatRatioPercent(p.quality_contract.reject_rate_budget) },
    { label: '最大读取行数', value: formatRows(p.supervision.max_read_rows) },
    { label: '最大写入字节', value: formatBytes(p.supervision.max_write_bytes) },
    { label: '最长运行时长', value: formatDuration(p.supervision.max_duration_sec) },
    { label: '元数据快照版本', value: `v${p.metadata_profile.version}` },
  ]
})

/** 过期时间只做原样展示，是否过期以后端结论为准（前端不判定有效期） */
const expiresAt = computed<string>(() => props.preview?.metadata_profile.expires_at ?? DASH)
</script>

<template>
  <ConfirmDialog
    :model-value="modelValue"
    title="冻结版本（不可撤销）"
    description="冻结后 plan / hocon / 质量契约 / 监督预算将作为不可变制品写入版本，只能新增版本修正，不能修改本版本。"
    require-reason
    :min-reason-length="5"
    :loading="loading"
    @update:model-value="(v: boolean) => emit('update:modelValue', v)"
    @confirm="(reason: string) => emit('confirm', reason)"
  >
    <div v-if="preview === null" class="freeze__unknown">
      未能从当前制品拼出冻结清单（服务端尚未返回可冻结的制品标识）；提交后以服务端返回的清单为准。
    </div>

    <div v-else class="freeze__manifest">
      <div v-for="r in rows" :key="r.label" class="freeze__row">
        <span class="freeze__label">{{ r.label }}</span>
        <span class="freeze__value elt-mono">{{ r.value }}</span>
      </div>

      <div class="freeze__row">
        <span class="freeze__label">HOCON sha256</span>
        <span class="freeze__value"><ArtifactHash :hash="preview.hocon.sha256" display="truncate" copyable /></span>
      </div>
      <div class="freeze__row">
        <span class="freeze__label">元数据 sha256</span>
        <span class="freeze__value"><ArtifactHash :hash="preview.metadata_profile.sha256" display="truncate" copyable /></span>
      </div>
      <div class="freeze__row">
        <span class="freeze__label">元数据过期时间</span>
        <span class="freeze__value elt-mono">{{ expiresAt }}</span>
      </div>
    </div>

    <!-- 过期与否以服务端判定为准，这里只做「可能被拒」的提醒 -->
    <el-alert v-if="profileExpired" type="warning" :closable="false" show-icon>
      源端元数据快照可能已过期。过期时后端会以 METADATA_PROFILE_EXPIRED 拒绝冻结，请先重新探查。
    </el-alert>

    <AsyncTaskPanel class="freeze__error" :loading="loading" :error="error" loading-text="正在冻结版本…" />
  </ConfirmDialog>
</template>

<style scoped>
.freeze__manifest {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 8px;
  background-color: var(--elt-bg-elevated);
  border: 1px solid var(--elt-border-subtle);
  border-radius: var(--elt-radius-control);
}

.freeze__row {
  display: flex;
  gap: 10px;
  align-items: center;
  font-size: 12px;
  line-height: 18px;
}

.freeze__label {
  flex: none;
  width: 110px;
  color: var(--elt-text-muted);
}

.freeze__value {
  flex: 1;
  min-width: 0;
  color: var(--elt-text-primary);
  overflow-wrap: anywhere;
}

.freeze__unknown {
  font-size: 12px;
  line-height: 18px;
  color: var(--elt-warn);
}

.freeze__error {
  margin-top: 8px;
}
</style>
