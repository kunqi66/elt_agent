<script setup lang="ts">
/**
 * 证据事件详情（fe-07 §6）。
 *
 * 这一屏是「审计对账」的现场，几个取舍是刻意的：
 * - `prev_hash` 与 `event_hash` **并排**且可复制：人工核对时要一眼看出「这条的 prev
 *   是不是上一条的 hash」，分成上下两行会被滚动条拆散；
 * - 时间以本地时区 `YYYY-MM-DD HH:mm:ss.SSS` 展示、悬停给 UTC——
 *   跨时区排查时两种表达都要能拿到，但不能让 UTC 成为主显示（它与日志对不上）；
 * - payload 只读，且**不做前端二次脱敏猜测**，也不给「显示隐藏字段」开关：
 *   后端已经按脱敏策略写过一遍，前端再猜一次只会在审计时引入第二种真相。
 *
 * 主体跳转只在「目标路由存在且当前身份进得去」时才出现，
 * 否则会出现「点了链接被弹到 403」这种比不给链接更糟的体验。
 */
import { computed } from 'vue'
import { useRouter } from 'vue-router'
import { ArtifactHash, SensitiveText, StatusTag } from '@/components/common'
import { usePermission } from '@/composables/usePermission'
import { DASH, formatDateTimeMs, formatUtc, textOrDash } from '@/utils/format'
import type { EvidenceEventRead, EvidenceLedgerBlockRead } from '@/types'

const props = withDefaults(
  defineProps<{
    event: EvidenceEventRead | null
    block: EvidenceLedgerBlockRead | null
  }>(),
  { block: null },
)

const router = useRouter()
const { canSee } = usePermission()

/**
 * 主体类型 → 目标路由。
 * 只登记「路由参数就是 subject_id」的那些：`pipeline_version` 的详情在 Studio 里，
 * 而 Studio 要的是 pipeline id，硬凑一个跳转只会跳到别人的页面上。
 */
const SUBJECT_ROUTES: Record<string, string> = {
  preparation: 'preparation-detail',
  execution_run: 'run-detail',
  pipeline: 'pipeline-studio',
  datasource: 'datasource-detail',
}

const subjectRoute = computed<{ name: string; id: string; label: string } | null>(() => {
  const event = props.event
  if (event === null) {
    return null
  }
  const name = SUBJECT_ROUTES[event.subject_type]
  if (name === undefined) {
    return null
  }
  const resolved = router.resolve({ name, params: { id: event.subject_id } })
  if (!canSee(resolved.meta.requiredRoles)) {
    return null
  }
  return { name, id: event.subject_id, label: resolved.meta.title ?? event.subject_type }
})

function openSubject(): void {
  const target = subjectRoute.value
  if (target === null) {
    return
  }
  void router.push({ name: target.name, params: { id: target.id } })
}

interface PayloadRow {
  path: string
  value: string
  depth: number
}

function renderValue(value: unknown): string {
  if (value === null || value === undefined) {
    return DASH
  }
  if (Array.isArray(value)) {
    return value.length === 0 ? DASH : value.map((item) => String(item)).join(' / ')
  }
  if (typeof value === 'object') {
    return JSON.stringify(value)
  }
  return String(value)
}

/** 摊平成「路径 → 值」的只读列表；嵌套两层以上直接给 JSON，不再无限下钻 */
function flatten(record: Record<string, unknown>, prefix = '', depth = 0): PayloadRow[] {
  const rows: PayloadRow[] = []
  for (const [key, value] of Object.entries(record)) {
    const path = prefix === '' ? key : `${prefix}.${key}`
    if (value !== null && typeof value === 'object' && !Array.isArray(value) && depth < 2) {
      rows.push(...flatten(value as Record<string, unknown>, path, depth + 1))
      continue
    }
    rows.push({ path, value: renderValue(value), depth })
  }
  return rows
}

const payloadRows = computed<PayloadRow[]>(() => {
  const payload = props.event?.payload
  if (payload === undefined) {
    return []
  }
  return flatten(payload)
})
</script>

<template>
  <div class="detail">
    <div v-if="event === null" class="detail__placeholder">
      在事件列表里点一行，这里会展开它的完整证据：哈希、前驱、主体与脱敏后的附加详情。
    </div>

    <template v-else>
      <header class="detail__head">
        <div class="detail__title">
          <span class="detail__seq elt-mono">seq {{ event.seq }}</span>
          <span class="detail__block elt-mono">
            签名块 {{ event.block_seq === null ? DASH : `#${event.block_seq}` }}
          </span>
        </div>
        <div class="detail__time" :title="formatUtc(event.occurred_at)">
          {{ formatDateTimeMs(event.occurred_at) }}
        </div>
      </header>

      <dl class="detail__grid">
        <div class="detail__item">
          <dt>事件类型（代码原值）</dt>
          <dd>
            <StatusTag kind="EvidenceEventType" :value="event.event_type" />
          </dd>
        </div>

        <div class="detail__item">
          <dt>操作者</dt>
          <dd>
            <span>{{ textOrDash(event.actor_name) }}</span>
            <span class="detail__muted elt-mono">#{{ event.actor_id === null ? DASH : event.actor_id }}</span>
          </dd>
        </div>

        <div class="detail__item">
          <dt>主体</dt>
          <dd>
            <span class="elt-mono">{{ event.subject_type }}:{{ event.subject_id }}</span>
            <el-button v-if="subjectRoute !== null" link size="small" @click="openSubject">
              打开{{ subjectRoute.label }}
            </el-button>
          </dd>
        </div>

        <div class="detail__item">
          <dt>归属</dt>
          <dd class="elt-mono">
            租户 {{ event.tenant_id }} · 项目 {{ event.project_id === null ? DASH : event.project_id }}
          </dd>
        </div>

        <div class="detail__item">
          <dt>制品哈希</dt>
          <dd>
            <ArtifactHash :hash="event.artifact_hash" display="truncate" copyable label="制品哈希" />
          </dd>
        </div>
      </dl>

      <section class="detail__chain">
        <div class="detail__chain-item">
          <div class="detail__chain-label">prev_hash（前一条的 event_hash）</div>
          <ArtifactHash :hash="event.prev_hash" display="truncate" copyable label="prev_hash" />
        </div>
        <div class="detail__chain-item">
          <div class="detail__chain-label">event_hash（本条）</div>
          <ArtifactHash :hash="event.event_hash" display="truncate" copyable label="event_hash" />
        </div>
      </section>

      <section class="detail__payload">
        <div class="detail__payload-title">附加详情（已脱敏，只读）</div>
        <div v-if="payloadRows.length === 0" class="detail__muted">无附加详情</div>
        <template v-else>
          <div
            v-for="row in payloadRows"
            :key="row.path"
            class="detail__payload-row"
            :style="{ paddingLeft: `${row.depth * 14}px` }"
          >
            <span class="detail__payload-key elt-mono">{{ row.path }}</span>
            <SensitiveText class="detail__payload-value" :value="row.value" mono multiline />
          </div>
        </template>
      </section>

      <section v-if="block !== null" class="detail__block-info">
        <div class="detail__payload-title">所属签名块</div>
        <dl class="detail__grid">
          <div class="detail__item">
            <dt>链头哈希</dt>
            <dd>
              <ArtifactHash :hash="block.chain_head_hash" display="truncate" copyable label="链头哈希" />
            </dd>
          </div>
          <div class="detail__item">
            <dt>密钥 / 签名时间</dt>
            <dd class="elt-mono">
              {{ textOrDash(block.key_id) }} · {{ formatDateTimeMs(block.signed_at) }}
            </dd>
          </div>
          <div class="detail__item">
            <dt>签名</dt>
            <dd>
              <SensitiveText :value="block.signature" mono multiline />
            </dd>
          </div>
        </dl>
      </section>

      <p v-else class="detail__foot">
        本页不展示块签名副本：签名校验由服务端验链接口完成，前端展示一份「本地算出来的结论」
        只会在验链失败时多出一个互相矛盾的说法。
      </p>
    </template>
  </div>
</template>

<style scoped>
.detail {
  display: flex;
  flex-direction: column;
}

.detail__placeholder {
  padding: 32px 0;
  font-size: 12px;
  color: var(--elt-text-muted);
  text-align: center;
}

.detail__head {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  align-items: baseline;
  justify-content: space-between;
}

.detail__title {
  display: flex;
  gap: 10px;
  align-items: baseline;
}

.detail__seq {
  font-size: 13px;
  font-weight: 600;
  color: var(--elt-text-primary);
}

.detail__block {
  font-size: 12px;
  color: var(--elt-text-muted);
}

.detail__time {
  font-size: 12px;
  color: var(--elt-text-secondary);
}

.detail__grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
  gap: 10px;
  margin: 12px 0 0;
}

.detail__item dt {
  font-size: 11px;
  color: var(--elt-text-muted);
}

.detail__item dd {
  display: flex;
  gap: 8px;
  align-items: center;
  margin: 3px 0 0;
  font-size: 12px;
  color: var(--elt-text-primary);
}

.detail__muted {
  font-size: 12px;
  color: var(--elt-text-muted);
}

.detail__chain {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
  gap: 10px;
  padding: 10px 12px;
  margin-top: 14px;
  background-color: var(--elt-bg-elevated);
  border: 1px solid var(--elt-border-subtle);
  border-radius: var(--elt-radius-control);
}

.detail__chain-label {
  margin-bottom: 3px;
  font-size: 11px;
  color: var(--elt-text-muted);
}

.detail__payload {
  margin-top: 14px;
  border-top: 1px solid var(--elt-border-subtle);
  padding-top: 12px;
}

.detail__payload-title {
  margin-bottom: 6px;
  font-size: 12px;
  color: var(--elt-text-secondary);
}

.detail__payload-row {
  display: flex;
  gap: 10px;
  padding-top: 2px;
  padding-bottom: 2px;
  font-size: 12px;
  line-height: 1.7;
}

.detail__payload-key {
  flex: none;
  min-width: 150px;
  color: var(--elt-text-secondary);
}

.detail__payload-value {
  min-width: 0;
  color: var(--elt-text-primary);
  overflow-wrap: anywhere;
}

.detail__block-info {
  padding-top: 12px;
  margin-top: 14px;
  border-top: 1px solid var(--elt-border-subtle);
}

.detail__foot {
  margin: 14px 0 0;
  font-size: 11px;
  line-height: 1.7;
  color: var(--elt-text-muted);
}
</style>
