<script setup lang="ts">
/**
 * 字段详情（fe-03 §6.4 左/下栏）。
 *
 * 只展示后端下发的统计结果（null_ratio / distinct_approx 等由探查期算出），
 * 前端不做任何统计推断——「自己数一遍样本」会得出与后端不一致的数字，
 * 而方案生成时用的是后端的值。
 */
import { computed } from 'vue'
import SensitiveText from '@/components/common/SensitiveText.vue'
import StatusTag from '@/components/common/StatusTag.vue'
import { formatInt, formatRate, textOrDash } from '@/utils/format'
import type { SchemaColumn } from '@/types'

const props = withDefaults(
  defineProps<{
    column: SchemaColumn | null
    tableName?: string | null
  }>(),
  { tableName: null },
)

const nullableText = computed<string>(() => (props.column?.nullable === true ? '允许' : 'NOT NULL'))
const pkText = computed<string>(() => (props.column?.is_primary_key === true ? '是' : '否'))
const maskedText = computed<string>(() => (props.column?.masked === true ? '已脱敏' : '未脱敏'))
</script>

<template>
  <div class="field-detail">
    <p v-if="column === null" class="field-detail__empty">在左侧表结构中点选一个字段，查看它的类型与敏感级别。</p>

    <dl v-else class="field-detail__list">
      <div class="field-detail__row">
        <dt>所属表</dt>
        <dd class="field-detail__mono">{{ textOrDash(tableName) }}</dd>
      </div>
      <div class="field-detail__row">
        <dt>字段名</dt>
        <dd class="field-detail__mono">{{ column.name }}</dd>
      </div>
      <div class="field-detail__row">
        <dt>类型</dt>
        <dd class="field-detail__mono">{{ column.data_type }}</dd>
      </div>
      <div class="field-detail__row">
        <dt>可空 / 主键</dt>
        <dd>{{ nullableText }} / {{ pkText }}</dd>
      </div>
      <div class="field-detail__row">
        <dt>敏感级别</dt>
        <dd><StatusTag kind="SensitiveLevel" :value="column.sensitive_level" /></dd>
      </div>
      <div class="field-detail__row">
        <dt>空值率</dt>
        <dd>{{ formatRate(column.null_ratio) }}</dd>
      </div>
      <div class="field-detail__row">
        <dt>基数值（近似）</dt>
        <dd>{{ formatInt(column.distinct_approx) }}</dd>
      </div>
      <div class="field-detail__row">
        <dt>样本</dt>
        <dd>
          {{ maskedText }}
          <span v-if="column.masked" class="field-detail__muted">（只能看到脱敏后的值）</span>
        </dd>
      </div>
      <div v-if="column.comment !== null && column.comment !== ''" class="field-detail__row">
        <dt>注释</dt>
        <dd><SensitiveText :value="column.comment" multiline /></dd>
      </div>
    </dl>
  </div>
</template>

<style scoped>
.field-detail__empty {
  margin: 0;
  font-size: 12px;
  line-height: 18px;
  color: var(--elt-text-muted);
}

.field-detail__list {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin: 0;
}

.field-detail__row {
  display: flex;
  gap: 10px;
  align-items: baseline;
  font-size: 12px;
  line-height: 18px;
}

.field-detail__row dt {
  flex: 0 0 96px;
  color: var(--elt-text-muted);
}

.field-detail__row dd {
  display: flex;
  gap: 6px;
  align-items: baseline;
  min-width: 0;
  margin: 0;
  word-break: break-all;
}

.field-detail__mono {
  font-family: var(--elt-font-mono);
}

.field-detail__muted {
  font-size: 11px;
  color: var(--elt-text-muted);
}
</style>
