<script setup lang="ts">
/**
 * 验链结果面板（fe-07 §6 / be-06）。
 *
 * 这个组件存在的全部理由，是让「验链结论」这件事没有第二个人可以说。
 * 因此：
 *
 * 1. **结论不缓存**。父组件每次点验链都会重新打一次 `store.verify()`；
 *    面板本身不记住上一次结果、不做「距上次验链 3 分钟」这类提示，
 *    也不提供「使用缓存结论」的开关——一份过期的「验链通过」比没有结论更危险。
 * 2. **不在浏览器里重算哈希**。前端能算出来的哈希，也就能被前端改成「通过」；
 *    这里是服务端从创世条目重算后的结论展示，仅此而已。
 *
 * 关于范围选择：`verify()` 目前固定全链核验（从创世条目起），
 * 所以这里不摆一个会被忽略的「起止 seq」输入框——摆出来就是在骗人。
 */
import { computed } from 'vue'
import { SensitiveText } from '@/components/common'
import { formatDateTimeMs } from '@/utils/format'
import type { LedgerBreakReason, LedgerVerifyResult } from '@/types'

const props = withDefaults(
  defineProps<{
    result: LedgerVerifyResult | null
    verifying: boolean
    /** 当前页已加载的事件数：用来提醒「验的是全链，不是这一屏」 */
    checkedEventsInView?: number
  }>(),
  { checkedEventsInView: 0 },
)

const emit = defineEmits<{
  verify: [scope: { from_seq: number | null; to_seq: number | null }]
}>()

/**
 * 断链原因的中文说明。
 * 后端给的是 `LedgerBreakReason` 代码，基座的 EnumKind 里没有它（enumText 取不到），
 * 而这是事故响应要立刻看懂的信息——所以就地补一份说明，并**保留原始代码**在旁边。
 * 未登记的原因一律只显示原值，不猜。
 */
const BREAK_REASON_NOTES: Record<LedgerBreakReason, string> = {
  hash_mismatch: '事件内容被改写：按内容重算出的 event_hash 与原记录对不上',
  prev_hash_mismatch: '前驱指针被改写：本条 prev_hash 与前一条的 event_hash 对不上',
  signature_invalid: '签名块签名校验失败：签名与链头哈希不匹配',
  missing_event: '事件缺失：seq 序列中间有断档',
}

const brokenAt = computed<number | null>(() => props.result?.broken_at_seq ?? null)

const reasonNote = computed<string>(() => {
  const reason = props.result?.broken_reason
  if (reason === null || reason === undefined) {
    return ''
  }
  return BREAK_REASON_NOTES[reason] ?? ''
})

function onVerify(): void {
  // 全链核验：范围参数固定为空（后端语义为「从创世条目起」）
  emit('verify', { from_seq: null, to_seq: null })
}
</script>

<template>
  <div class="verify">
    <div class="verify__intro">
      <p class="verify__lead">
        验链由服务端独立完成：后端从创世条目起把 prev_hash / event_hash 重算到表尾。
        浏览器不参与哈希计算——前端能算的哈希，也就能被前端改成「通过」。
      </p>
      <p class="verify__lead">
        每次点击都是**重新**验链，本页不复用上一次结论：一份过期的「验链通过」比没有结论更危险。
        验链动作本身也会写入证据账本，因此它可以在账本里被追溯。
      </p>
    </div>

    <div class="verify__actions">
      <el-button type="primary" size="small" :loading="verifying" @click="onVerify">
        立即验链
      </el-button>
      <span class="verify__scope">
        核验范围：全链（从创世条目起）；本页当前只加载了 {{ checkedEventsInView }} 条，结论不限于这一屏。
      </span>
    </div>

    <div v-if="result === null" class="verify__idle">
      还没有验链结论。账本不会自己告诉你它有没有被改过——需要有人真的去验一次。
    </div>

    <div v-else-if="result.ok" class="verify__card verify__card--ok">
      <div class="verify__title">验链通过</div>
      <dl class="verify__facts">
        <div class="verify__fact">
          <dt>已核验事件</dt>
          <dd class="elt-mono">{{ result.checked_events }}</dd>
        </div>
        <div class="verify__fact">
          <dt>已核验签名块</dt>
          <dd class="elt-mono">{{ result.checked_blocks }}</dd>
        </div>
        <div class="verify__fact">
          <dt>起始块</dt>
          <dd class="elt-mono">
            {{ result.started_from_block_seq === null ? '创世条目' : `#${result.started_from_block_seq}` }}
          </dd>
        </div>
        <div class="verify__fact">
          <dt>完成时间</dt>
          <dd>{{ formatDateTimeMs(result.finished_at) }}</dd>
        </div>
      </dl>
      <p class="verify__note">
        这只说明「此刻这条链是自洽的」。链自洽不等于业务动作都对——那是策略与审批要看的事。
      </p>
    </div>

    <div v-else class="verify__card verify__card--bad">
      <div class="verify__title">验链未通过：链上有断点</div>

      <dl class="verify__facts">
        <div class="verify__fact">
          <dt>断裂位置</dt>
          <dd>
            <span class="elt-mono">seq {{ brokenAt === null ? '—' : brokenAt }}</span>
          </dd>
        </div>
        <div class="verify__fact">
          <dt>断裂原因</dt>
          <dd>
            <SensitiveText :value="result.broken_reason" mono />
            <span v-if="reasonNote !== ''" class="verify__reason-note">{{ reasonNote }}</span>
          </dd>
        </div>
        <div class="verify__fact">
          <dt>已核验事件 / 块</dt>
          <dd class="elt-mono">{{ result.checked_events }} / {{ result.checked_blocks }}</dd>
        </div>
        <div class="verify__fact">
          <dt>完成时间</dt>
          <dd>{{ formatDateTimeMs(result.finished_at) }}</dd>
        </div>
      </dl>

      <p v-if="brokenAt !== null" class="verify__untrusted">
        从 <span class="elt-mono">seq {{ brokenAt }}</span> 起不可信：
        后续每一条事件的 prev_hash 都锚定在这一段上，锚点一旦对不上，
        这些记录就无法再证明「未被改动」——在重新验链通过之前，
        <strong>seq ≥ {{ brokenAt }} 的记录不能作为审计依据</strong>；断点之前的记录仍然可信。
      </p>

      <p class="verify__note">
        请立即联系安全负责人，并保留当前结论（本页不提供「忽略」按钮）。
        修复方式只有一种：由后端从可信副本重建账本并重新验链，前端不做任何补偿写入。
      </p>
    </div>
  </div>
</template>

<style scoped>
.verify {
  display: flex;
  flex-direction: column;
}

.verify__intro {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.verify__lead {
  margin: 0;
  font-size: 12px;
  line-height: 1.8;
  color: var(--elt-text-secondary);
}

.verify__actions {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  align-items: center;
  margin-top: 12px;
}

.verify__scope {
  font-size: 11px;
  color: var(--elt-text-muted);
}

.verify__idle {
  padding: 20px 0 6px;
  font-size: 12px;
  color: var(--elt-text-muted);
}

.verify__card {
  padding: 12px 14px;
  margin-top: 12px;
  border: 1px solid var(--elt-border-subtle);
  border-radius: var(--elt-radius-control);
}

.verify__card--ok {
  border-color: var(--elt-success);
  box-shadow: inset 2px 0 0 0 var(--elt-success);
}

.verify__card--bad {
  border-color: var(--elt-danger);
  box-shadow: inset 2px 0 0 0 var(--elt-danger);
}

.verify__title {
  font-size: 13px;
  font-weight: 600;
  color: var(--elt-text-primary);
}

.verify__card--bad .verify__title {
  color: var(--elt-danger);
}

.verify__facts {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 10px;
  margin: 10px 0 0;
}

.verify__fact dt {
  font-size: 11px;
  color: var(--elt-text-muted);
}

.verify__fact dd {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  align-items: center;
  margin: 3px 0 0;
  font-size: 12px;
  color: var(--elt-text-primary);
}

.verify__reason-note {
  font-size: 12px;
  color: var(--elt-text-secondary);
}

.verify__untrusted {
  padding: 10px 12px;
  margin: 12px 0 0;
  font-size: 12px;
  line-height: 1.9;
  color: var(--elt-text-primary);
  background-color: color-mix(in srgb, var(--elt-danger) 10%, var(--elt-bg-app));
  border-radius: var(--elt-radius-control);
}

.verify__note {
  margin: 10px 0 0;
  font-size: 11px;
  line-height: 1.8;
  color: var(--elt-text-muted);
}
</style>
