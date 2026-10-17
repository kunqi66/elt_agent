<script setup lang="ts">
/**
 * 异步写操作的进度 / 失败面板（配合 `useAsyncTask`）。
 *
 * 解决一个具体问题：写操作（冻结版本、提交审批、发起回滚）失败时，
 * 用 `ElMessage` 弹一条 toast 会在 3 秒后消失，而用户可能正低头看别处。
 * 涉及审批与回滚的动作，失败原因**必须留在原地直到用户处理**。
 *
 * 因此约定：查询类失败用 toast，写操作失败用本组件。
 */
import { computed } from 'vue'
import { Loading, WarningFilled } from '@element-plus/icons-vue'
import { redactSensitive } from '@/utils/mask'

const props = withDefaults(
  defineProps<{
    loading: boolean
    error?: string | null
    /** 进行中的说明，如「正在冻结版本…」 */
    loadingText?: string
  }>(),
  { error: null, loadingText: '处理中…' },
)

const safeError = computed<string | null>(() => {
  if (props.error === null || props.error === '') {
    return null
  }
  return redactSensitive(props.error, '操作失败，详情含疑似敏感信息，已隐去', 'AsyncTaskPanel')
})
</script>

<template>
  <!-- 成功态不在这里渲染：成功后应当由页面自己切换形态（如刷新列表、关闭弹窗），
       留一条「成功了」的横幅只会占地方。 -->
  <div v-if="loading || safeError !== null" class="async-panel">
    <el-alert v-if="loading" type="info" :closable="false" show-icon>
      <template #icon>
        <el-icon class="is-loading"><Loading /></el-icon>
      </template>
      <span>{{ loadingText }}</span>
    </el-alert>

    <el-alert v-else-if="safeError !== null" type="error" :closable="false" show-icon>
      <template #icon>
        <el-icon><WarningFilled /></el-icon>
      </template>
      <span>{{ safeError }}</span>
    </el-alert>
  </div>
</template>

<style scoped>
.async-panel {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin-top: 12px;
}
</style>
