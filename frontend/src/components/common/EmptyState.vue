<script setup lang="ts">
/**
 * 空态（fe-01 §7）。
 *
 * 三种空态必须**分开说**，否则用户不知道该做什么：
 * - `no-data`：这个项目还没有数据 → 引导「新建」
 * - `no-match`：有数据但筛选没命中 → 引导「清空筛选」
 * - `no-permission`：没权限看 → 只说事实，不给无用的操作按钮
 *
 * 因此 `variant` 是必填的。默认一个「暂无数据」会让后两种情况都指向错误的下一步。
 */
import { computed } from 'vue'
import { Box, Filter, Lock } from '@element-plus/icons-vue'

const props = withDefaults(
  defineProps<{
    variant: 'no-data' | 'no-match' | 'no-permission'
    title?: string
    description?: string
  }>(),
  { title: undefined, description: undefined },
)

const preset = computed<{ title: string; description: string }>(() => {
  switch (props.variant) {
    case 'no-match':
      return { title: '没有符合条件的记录', description: '试试放宽筛选条件，或清空后重新查询。' }
    case 'no-permission':
      return { title: '无权查看', description: '当前职责槽不可见该范围的数据。如需访问，请联系项目管理员调整职责槽。' }
    default:
      return { title: '还没有数据', description: '创建第一条记录后，这里会显示列表。' }
  }
})

const finalTitle = computed<string>(() => props.title ?? preset.value.title)
const finalDescription = computed<string>(() => props.description ?? preset.value.description)
</script>

<template>
  <div class="empty-state">
    <el-icon class="empty-state__icon" :size="28">
      <Lock v-if="variant === 'no-permission'" />
      <Filter v-else-if="variant === 'no-match'" />
      <Box v-else />
    </el-icon>
    <p class="empty-state__title">{{ finalTitle }}</p>
    <p class="empty-state__desc">{{ finalDescription }}</p>
    <!-- 操作按钮由页面给：空态本身不知道「下一步」是什么 -->
    <div v-if="$slots.actions" class="empty-state__actions">
      <slot name="actions" />
    </div>
  </div>
</template>

<style scoped>
.empty-state {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 6px;
  padding: 40px 20px;
  text-align: center;
}

.empty-state__icon {
  color: var(--elt-text-muted);
  opacity: 0.7;
}

.empty-state__title {
  margin: 6px 0 0;
  font-size: 13px;
  font-weight: 600;
  color: var(--elt-text-secondary);
}

.empty-state__desc {
  max-width: 420px;
  margin: 0;
  font-size: 12px;
  line-height: 1.6;
  color: var(--elt-text-muted);
}

.empty-state__actions {
  margin-top: 8px;
}
</style>
