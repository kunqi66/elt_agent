<script setup lang="ts">
/**
 * 错误态（fe-01 §7）。
 *
 * 与空态严格互斥：**加载失败绝不能被渲染成「暂无数据」**。
 * 那会让用户以为「这个项目真的没有数据源」，从而去新建一个本已存在的东西。
 *
 * 展示规则：
 * - 文案走 utils/mask 过滤后才渲染 —— 后端错误消息理论上已脱敏，
 *   但错误消息是最容易漏脱敏的通道（异常里常带连接串），前端再拦一道；
 * - 带上 request_id，用户报障时可直接与后端 structlog 对齐。
 */
import { computed } from 'vue'
import { Refresh, WarningFilled } from '@element-plus/icons-vue'
import { redactSensitive } from '@/utils/mask'

const props = withDefaults(
  defineProps<{
    /** 已展示化的错误文案（通常是 store.error） */
    message?: string | null
    /** 后端 request_id，用于与日志对账 */
    requestId?: string | null
    title?: string
    /** 是否显示重试按钮；写操作失败时通常由弹窗自己处理，不需要这里重试 */
    retryable?: boolean
  }>(),
  { message: null, requestId: null, title: '加载失败', retryable: true },
)

const emit = defineEmits<{ retry: [] }>()

const safeMessage = computed<string>(() =>
  redactSensitive(props.message, '详情含疑似敏感信息，已隐去。请按 request_id 查看后端日志', 'ErrorState'),
)
</script>

<template>
  <div class="error-state">
    <el-icon class="error-state__icon" :size="26">
      <WarningFilled />
    </el-icon>
    <p class="error-state__title">{{ title }}</p>
    <p v-if="safeMessage !== ''" class="error-state__message">{{ safeMessage }}</p>
    <p v-if="requestId" class="error-state__rid elt-mono">request_id: {{ requestId }}</p>
    <div class="error-state__actions">
      <el-button v-if="retryable" size="small" :icon="Refresh" @click="emit('retry')">重试</el-button>
      <slot name="actions" />
    </div>
  </div>
</template>

<style scoped>
.error-state {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 6px;
  padding: 40px 20px;
  text-align: center;
}

.error-state__icon {
  color: var(--elt-danger);
  opacity: 0.85;
}

.error-state__title {
  margin: 6px 0 0;
  font-size: 13px;
  font-weight: 600;
  color: var(--elt-text-primary);
}

.error-state__message {
  max-width: 520px;
  margin: 0;
  font-size: 12px;
  line-height: 1.6;
  color: var(--elt-text-secondary);
  word-break: break-word;
}

.error-state__rid {
  margin: 0;
  font-size: 11px;
  color: var(--elt-text-muted);
}

.error-state__actions {
  display: flex;
  gap: 8px;
  margin-top: 8px;
}
</style>
