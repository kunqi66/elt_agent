<script setup lang="ts">
/**
 * Capability 面板（fe-05 §1.5 / §8）。
 *
 * 这里展示的是 **Capability 的引用与状态**，不是 Capability 本身：
 * 令牌全文、`jti` 与任何签名材料**永不下发前端**（Commit 只由服务端换取，
 * 响应里只有 `run_id`）。因此本组件：
 * - 不请求、不缓存、不渲染任何令牌字段（DTO 里也没有这类字段，故不存在「有值不显示」）；
 * - 只把前端**确实知道**的事实摆出来：作用域、环境、有效期、当前是否具备换取条件；
 * - 明确写出「一次性消费」，让用户对「点了就没有第二次」有预期
 *   ——消费在 Redis `SETNX` + PG 唯一约束上原子完成，重放会被 ReplayGuard 拒绝。
 */
import { computed } from 'vue'
import { ArtifactHash, SensitiveText } from '@/components/common'
import { formatDateTime, formatRemaining, remainingMs } from '@/utils/format'
import type { PreparationDetail } from '@/types'

const props = defineProps<{ preparation: PreparationDetail }>()

/** 前端可见的三态：未到条件 / 具备换取条件 / 不可换取；「已消费」由服务端状态体现 */
type CapabilityState = 'not-ready' | 'ready' | 'unavailable'

const state = computed<CapabilityState>(() => {
  if (props.preparation.status !== 'approved') {
    return 'not-ready'
  }
  return props.preparation.can_commit ? 'ready' : 'unavailable'
})

const stateText = computed<string>(() => {
  switch (state.value) {
    case 'ready':
      return '已具备换取条件（Commit 时由服务端签发）'
    case 'unavailable':
      return '不可换取（可能已被消费、已失效或已被取代）'
    default:
      return '未到签发条件（需必需审批槽全部通过）'
  }
})

const stateTag = computed<'success' | 'info' | 'warning'>(() => {
  switch (state.value) {
    case 'ready':
      return 'success'
    case 'unavailable':
      return 'warning'
    default:
      return 'info'
  }
})

const reads = computed(() => props.preparation.resource_scope.reads)
const writes = computed(() => props.preparation.resource_scope.writes)

/** Capability 的作用域不会超出准备单的有效期；这里只展示，不据此做任何判定 */
const expiryText = computed<string>(() =>
  props.preparation.expires_at === null
    ? '不设有效期'
    : `${formatDateTime(props.preparation.expires_at)}（${formatRemaining(remainingMs(props.preparation.expires_at))}）`,
)
</script>

<template>
  <div class="cap">
    <div class="cap__status">
      <el-tag :type="stateTag" size="small" effect="light" disable-transitions>{{ stateText }}</el-tag>
      <el-tag type="danger" size="small" effect="plain" disable-transitions>一次性消费</el-tag>
    </div>

    <dl class="cap__grid">
      <div class="cap__cell cap__cell--wide">
        <dt>绑定制品指纹</dt>
        <dd>
          <ArtifactHash :hash="preparation.artifact_sha256" display="truncate" copyable label="绑定指纹" />
        </dd>
      </div>

      <div class="cap__cell">
        <dt>环境</dt>
        <dd>{{ preparation.environment }}</dd>
      </div>

      <div class="cap__cell">
        <dt>有效期</dt>
        <dd class="elt-mono">{{ expiryText }}</dd>
      </div>

      <div class="cap__cell cap__cell--wide">
        <dt>作用域（读取）</dt>
        <dd>
          <ul v-if="reads.length > 0" class="cap__list">
            <li v-for="(r, i) in reads" :key="`r-${i}`" class="elt-mono">
              #{{ r.data_source_id }} · {{ r.database }}.{{ r.table }}
            </li>
          </ul>
          <span v-else class="cap__muted">—</span>
        </dd>
      </div>

      <div class="cap__cell cap__cell--wide">
        <dt>作用域（写入）</dt>
        <dd>
          <ul v-if="writes.length > 0" class="cap__list">
            <li v-for="(w, i) in writes" :key="`w-${i}`" class="elt-mono">
              #{{ w.data_source_id }} · {{ w.database }}.{{ w.table }}
            </li>
          </ul>
          <span v-else class="cap__muted">—</span>
        </dd>
      </div>
    </dl>

    <SensitiveText
      value="令牌内容、jti 与签名材料由服务端保管，前端不接收、不缓存、不展示。Commit 成功后该凭据即被消费，重复提交将被拒绝；执行结果请在运行中心查看。"
      multiline
      class="cap__note"
    />
  </div>
</template>

<style scoped>
.cap {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.cap__status {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  align-items: center;
}

.cap__grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
  gap: 10px 16px;
  margin: 0;
}

.cap__cell--wide {
  grid-column: 1 / -1;
}

.cap__cell dt {
  font-size: 11px;
  color: var(--elt-text-muted);
}

.cap__cell dd {
  margin: 2px 0 0;
  font-size: 13px;
  color: var(--elt-text-primary);
  word-break: break-word;
}

.cap__list {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 0;
  margin: 0;
  font-size: 12px;
  list-style: none;
}

.cap__muted {
  color: var(--elt-text-muted);
}

.cap__note {
  padding: 8px 10px;
  font-size: 12px;
  line-height: 1.7;
  color: var(--elt-text-secondary);
  background-color: var(--elt-bg-elevated);
  border: 1px dashed var(--elt-border-subtle);
  border-radius: var(--elt-radius-control);
}
</style>
