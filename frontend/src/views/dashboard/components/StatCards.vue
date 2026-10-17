<script setup lang="ts">
/**
 * 资产计数卡（fe-02 §6.1 表的前四行）。
 *
 * 只显示后端返回的计数，前端不做任何聚合。
 * 「0」不是空态而是事实——所以副文案直接给出下一步动作（去新建连接），
 * 而不是留一句「暂无数据」让用户猜该干什么。
 */
import { computed } from 'vue'
import { RouterLink } from 'vue-router'
import { DASH, formatInt } from '@/utils/format'
import type { MonitorAssets } from '@/types'

const props = defineProps<{
  assets: MonitorAssets | null
  loading: boolean
}>()

interface AssetTile {
  key: string
  label: string
  value: string
  hint: string
  /** 需要用户处理的事实（有连接被禁用 / 元数据已过期）才用警示色 */
  warn: boolean
  to: string | null
  linkText: string | null
}

const tiles = computed<AssetTile[]>(() => {
  const assets = props.assets
  if (assets === null) {
    // 无数据时给 '—' 而不是 0：0 是「查过了确实没有」，'—' 才是「还没拿到」
    return [
      { key: 'datasource', label: '数据连接', value: DASH, hint: '尚未取得计数', warn: false, to: null, linkText: null },
      { key: 'pipeline', label: 'Pipeline', value: DASH, hint: '尚未取得计数', warn: false, to: null, linkText: null },
      { key: 'version', label: '不可变版本', value: DASH, hint: '尚未取得计数', warn: false, to: null, linkText: null },
      { key: 'profile', label: '元数据探查', value: DASH, hint: '尚未取得计数', warn: false, to: null, linkText: null },
    ]
  }

  const disabled = assets.datasource_disabled_count
  const expired = assets.metadata_profile_expired_count

  return [
    {
      key: 'datasource',
      label: '数据连接',
      value: formatInt(assets.datasource_count),
      hint: disabled > 0 ? `${formatInt(disabled)} 个已禁用` : '全部可用',
      warn: disabled > 0,
      to: '/datasources/new',
      linkText: assets.datasource_count === 0 ? '新建连接' : '新增',
    },
    {
      key: 'pipeline',
      label: 'Pipeline',
      value: formatInt(assets.pipeline_count),
      hint: assets.pipeline_count === 0 ? '还没有 Pipeline' : '已登记管线',
      warn: false,
      to: '/pipelines',
      linkText: assets.pipeline_count === 0 ? '创建 Pipeline' : '查看',
    },
    {
      key: 'version',
      label: '不可变版本',
      value: formatInt(assets.pipeline_version_count),
      hint: '冻结后不可修改',
      warn: false,
      to: null,
      linkText: null,
    },
    {
      key: 'profile',
      label: '元数据探查',
      value: formatInt(assets.metadata_profile_ready_count),
      hint: expired > 0 ? `${formatInt(expired)} 个已过期，需重新探查` : '无过期探查',
      warn: expired > 0,
      to: null,
      linkText: null,
    },
  ]
})
</script>

<template>
  <div class="stat-cards">
    <!-- 首次加载用块级骨架：卡片高度固定，避免加载完成后整页跳动 -->
    <template v-if="loading">
      <div v-for="n in 4" :key="n" class="stat-card">
        <div class="elt-skeleton elt-skeleton--block" style="height: 74px" />
      </div>
    </template>

    <template v-else>
      <div v-for="tile in tiles" :key="tile.key" class="stat-card">
        <p class="stat-card__label">{{ tile.label }}</p>
        <p class="stat-card__value elt-mono">{{ tile.value }}</p>
        <p class="stat-card__hint" :class="{ 'stat-card__hint--warn': tile.warn }">
          <span>{{ tile.hint }}</span>
          <RouterLink
            v-if="tile.to !== null"
            class="stat-card__link"
            :to="tile.to"
          >
            {{ tile.linkText }}
          </RouterLink>
        </p>
      </div>
    </template>
  </div>
</template>

<style scoped>
.stat-cards {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 14px;
}

.stat-card {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 14px;
  background-color: var(--elt-bg-surface);
  border: 1px solid var(--elt-border-subtle);
  border-radius: var(--elt-radius-card);
}

.stat-card__label {
  margin: 0;
  font-size: 12px;
  line-height: 18px;
  color: var(--elt-text-secondary);
}

.stat-card__value {
  margin: 0;
  font-size: 24px;
  font-weight: 600;
  line-height: 30px;
  color: var(--elt-text-primary);
}

.stat-card__hint {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin: 0;
  font-size: 12px;
  line-height: 18px;
  color: var(--elt-text-muted);
}

.stat-card__hint--warn {
  color: var(--elt-warn);
}

.stat-card__link {
  color: var(--elt-accent);
  text-decoration: none;
}

.stat-card__link:hover {
  text-decoration: underline;
}

@media (max-width: 1200px) {
  .stat-cards {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}

@media (max-width: 640px) {
  .stat-cards {
    grid-template-columns: minmax(0, 1fr);
  }
}
</style>
