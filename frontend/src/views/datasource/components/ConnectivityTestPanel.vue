<script setup lang="ts">
/**
 * 连通性测试面板（fe-03 §5.3）。
 *
 * 判定全在后端（`datasources/{id}/connectivity-tests`，P3 工具、免 Capability）：
 * 前端只渲染四态，**不**据 state 反推 http 状态、也不自行判定成功。
 * 结果文案一律经 SensitiveText——失败原因是最容易夹带连接串的通道，
 * 后端已脱敏一次，这里是第二道。
 *
 * 同一组件给列表行内（inline）与详情页（full）用：两处若各写一套状态映射，
 * 必然出现「列表说超时、详情说失败」这种自相矛盾。
 */
import { computed, onMounted, onUnmounted, ref } from 'vue'
import type { Component } from 'vue'
import { CircleCheck, CircleClose, Lock, Timer } from '@element-plus/icons-vue'
import SensitiveText from '@/components/common/SensitiveText.vue'
import StatusTag from '@/components/common/StatusTag.vue'
import { useDataSourceStore } from '@/stores/datasource'
import { formatCountdown, formatDateTime, formatInt } from '@/utils/format'
import type { ConnectivityResult, ConnectivityState } from '@/types'

const props = withDefaults(
  defineProps<{
    datasourceId: number
    lastTestedAt: string | null
    lastTestOk: boolean | null
    /** 「仅登记不真连」的类型不允许触发测试与探查 */
    connectable?: boolean
    /** full = 详情页（展开结果细节）；inline = 列表行内（按钮 + 一行状态） */
    variant?: 'full' | 'inline'
    /** 挂载后自动测一次（新建成功回跳 ?test=1 用）；已有结果时不重复测 */
    autoRun?: boolean
  }>(),
  { connectable: true, variant: 'full', autoRun: false },
)

const emit = defineEmits<{ tested: [result: ConnectivityResult] }>()

const store = useDataSourceStore()

/** 与 store.testConnectivity 内部的 30s 预算一致：倒计时归零即说明这一轮也快结束了 */
const BUDGET_MS = 30_000

interface StateMeta {
  label: string
  tag: 'success' | 'danger' | 'warning' | 'info'
  icon: Component
  advice: string
}

const STATE_META: Record<ConnectivityState, StateMeta> = {
  pending: {
    label: '测试中',
    tag: 'info',
    icon: Timer,
    advice: '正在探测目标可达性与认证结果。',
  },
  succeeded: { label: '连接成功', tag: 'success', icon: CircleCheck, advice: '' },
  failed: {
    label: '连接失败',
    tag: 'danger',
    icon: CircleClose,
    advice: '确认主机/端口正确、安全组已放行；网络不可达需联系基础设施处理。',
  },
  timeout: {
    label: '连接超时',
    tag: 'warning',
    icon: Timer,
    advice: '目标可能不可达或负载过高，可稍后重试。',
  },
  denied: {
    label: '认证失败或权限不足',
    tag: 'warning',
    icon: Lock,
    advice: '凭据可能已轮换或账号缺少最小权限，请「重新设置凭据」后重试。',
  },
}

const running = computed<boolean>(() => store.testing.has(props.datasourceId))
const result = computed<ConnectivityResult | null>(
  () => store.connectivity[props.datasourceId] ?? null,
)
const meta = computed<StateMeta | null>(() =>
  result.value === null ? null : STATE_META[result.value.state],
)

const remaining = ref<number>(BUDGET_MS)
const elapsedPercent = computed<number>(() =>
  Math.min(100, Math.round(((BUDGET_MS - remaining.value) / BUDGET_MS) * 100)),
)

let ticker: ReturnType<typeof setInterval> | null = null

function stopTicker(): void {
  if (ticker !== null) {
    clearInterval(ticker)
    ticker = null
  }
}

function startTicker(): void {
  stopTicker()
  const startedAt = Date.now()
  remaining.value = BUDGET_MS
  ticker = setInterval(() => {
    remaining.value = Math.max(0, BUDGET_MS - (Date.now() - startedAt))
    if (remaining.value <= 0) {
      stopTicker()
    }
  }, 500)
}

// 组件卸载必须清掉计时器：列表翻页会频繁卸载行内面板，泄漏的 interval 会越积越多
onUnmounted(stopTicker)

async function run(): Promise<void> {
  if (running.value || !props.connectable) {
    return
  }
  startTicker()
  try {
    const tested = await store.testConnectivity(props.datasourceId)
    emit('tested', tested)
  } finally {
    stopTicker()
    remaining.value = BUDGET_MS
  }
}

const lastLabel = computed<string>(() => {
  if (props.lastTestedAt === null) {
    return '尚未测试'
  }
  const ok = props.lastTestOk === true ? '成功' : props.lastTestOk === false ? '失败' : '结果未知'
  return `${formatDateTime(props.lastTestedAt)}（${ok}）`
})

// 只有详情页会传 autoRun；结果已在 store 里就不再打扰后端（页面重挂载时会出现这种情况）
onMounted(() => {
  if (props.autoRun && props.connectable && result.value === null && !running.value) {
    void run()
  }
})
</script>

<template>
  <div class="conn-panel" :class="`conn-panel--${variant}`">
    <div class="conn-panel__bar">
      <el-button
        size="small"
        :type="variant === 'full' ? 'primary' : 'default'"
        plain
        :loading="running"
        :disabled="!connectable"
        @click="run"
      >{{ running ? '测试中' : '测试连接' }}</el-button>

      <span class="conn-panel__last">最近测试：{{ lastLabel }}</span>

      <el-tag v-if="running" size="small" type="info">
        剩余 {{ formatCountdown(remaining) }}
      </el-tag>
    </div>

    <el-alert
      v-if="!connectable"
      type="warning"
      :closable="false"
      show-icon
      title="该类型仅登记接口，未接入真实连接器"
      description="无法执行连通性测试与元数据探查；登记信息只用于方案编排与审计。"
    />

    <template v-if="running && variant === 'full'">
      <el-progress
        :percentage="elapsedPercent"
        :show-text="false"
        :stroke-width="4"
        striped
        striped-flow
      />
      <p class="conn-panel__countdown">
        正在探测（剩余 {{ formatCountdown(remaining) }}，单次预算 30 秒）…
      </p>
    </template>

    <!-- 结果区：状态永远显示；细节只在详情页展开 -->
    <div v-if="result !== null && !running" class="conn-panel__result">
      <el-tag :type="meta?.tag ?? 'info'" size="small">{{ meta?.label ?? result.state }}</el-tag>
      <span v-if="result.latency_ms !== null" class="conn-panel__latency">
        {{ formatInt(result.latency_ms) }} ms
      </span>
      <StatusTag v-if="variant === 'full' && result.error_code !== null" kind="ErrorCode" :value="result.error_code" />
      <span v-if="variant === 'full' && result.target !== null" class="conn-panel__meta">
        目标：<SensitiveText :value="result.target" mono host-only />
      </span>
      <span v-if="variant === 'full' && result.server_version !== null" class="conn-panel__meta">
        服务端：<SensitiveText :value="result.server_version" mono />
      </span>
    </div>

    <div v-if="variant === 'full' && result !== null && !running" class="conn-panel__detail">
      <p v-if="result.message !== null" class="conn-panel__message">
        <SensitiveText :value="result.message" multiline />
      </p>
      <p v-if="meta !== null && meta.advice !== ''" class="conn-panel__advice">{{ meta.advice }}</p>
      <p v-if="result.tested_at !== null" class="conn-panel__muted">
        测试时间：{{ formatDateTime(result.tested_at) }}
      </p>
    </div>
  </div>
</template>

<style scoped>
.conn-panel {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.conn-panel--inline {
  gap: 6px;
}

.conn-panel__bar {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
}

.conn-panel__last,
.conn-panel__muted {
  font-size: 11px;
  line-height: 16px;
  color: var(--elt-text-muted);
  white-space: nowrap;
}

.conn-panel__result {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
}

.conn-panel__latency,
.conn-panel__meta {
  font-size: 12px;
  color: var(--elt-text-secondary);
}

.conn-panel__countdown,
.conn-panel__message,
.conn-panel__advice {
  margin: 0;
  font-size: 12px;
  line-height: 18px;
}

.conn-panel__message {
  color: var(--elt-text-secondary);
}

.conn-panel__advice {
  color: var(--elt-warn);
}

.conn-panel__detail {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
</style>
