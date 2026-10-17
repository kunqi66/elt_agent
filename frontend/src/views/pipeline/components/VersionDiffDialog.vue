<script setup lang="ts">
/**
 * 版本对比弹窗（fe-04 §5.3 / §6.3）。
 *
 * 两种视角，各有各的用处：
 * - **制品条目**：后端给的路径级差异（`field_mappings[3].target_type` 之类），
 *   结构化、易读，适合回答「两版之间到底改了什么」；
 * - **HOCON 文本**：分别取两版 HOCON 后交给 @codemirror/merge 做并排比对，
 *   适合回答「最后一版提交出去的是什么」。文本差异在前端算，但**不下结论**：
 *   是否合规仍由门禁/审批判定。
 *
 * 两次请求只在用户真的切到文本视图时才发：HOCON 可能几百 KB，
 * 打开弹窗就预取两个会把「看一眼 diff」变成等三秒。
 */
import { computed, defineAsyncComponent, ref, watch } from 'vue'
import { EmptyState, ErrorState, SensitiveText } from '@/components/common'
import { fetchHocon, fetchVersionDiff } from '@/api/pipeline'
import { toDisplayMessage } from '@/utils/errors'
import { formatDateTime, hashShort } from '@/utils/format'
import type { PipelineVersionSummary } from '@/types'

const MergeViewer = defineAsyncComponent(() => import('./MergeViewer.vue'))

interface DiffEntry {
  path: string
  kind: 'added' | 'removed' | 'changed'
  before?: string
  after?: string
}

const props = withDefaults(
  defineProps<{
    modelValue: boolean
    versions: PipelineVersionSummary[]
    /** 当前正在看的版本，作为对比基线默认值 */
    currentVersionId: number | null
  }>(),
  { currentVersionId: null },
)

const emit = defineEmits<{ 'update:modelValue': [value: boolean] }>()

const baseId = ref<number | null>(null)
const targetId = ref<number | null>(null)
const mode = ref<'entries' | 'text'>('entries')

const entriesLoading = ref<boolean>(false)
const entriesError = ref<string | null>(null)
const entries = ref<DiffEntry[]>([])
const compared = ref<boolean>(false)

const textLoading = ref<boolean>(false)
const textError = ref<string | null>(null)
const hoconA = ref<string>('')
const hoconB = ref<string>('')

const open = computed<boolean>({
  get: () => props.modelValue,
  set: (v: boolean) => emit('update:modelValue', v),
})

const KIND_TEXT: Record<DiffEntry['kind'], { label: string; cls: string }> = {
  added: { label: '新增', cls: 'is-added' },
  removed: { label: '删除', cls: 'is-removed' },
  changed: { label: '变更', cls: 'is-changed' },
}

function labelOf(v: PipelineVersionSummary): string {
  return `v${v.version_no} · ${hashShort(v.sha256)} · ${formatDateTime(v.frozen_at)}`
}

const baseVersion = computed<PipelineVersionSummary | null>(
  () => props.versions.find((v) => v.id === baseId.value) ?? null,
)
const targetVersion = computed<PipelineVersionSummary | null>(
  () => props.versions.find((v) => v.id === targetId.value) ?? null,
)

const canCompare = computed<boolean>(
  () => baseId.value !== null && targetId.value !== null && baseId.value !== targetId.value,
)

/** 打开时按「上一版 → 当前版」预置：用户最常问的就是「这一版比上一版改了什么」 */
watch(
  () => props.modelValue,
  (visible) => {
    if (!visible) {
      return
    }
    const list = props.versions
    const current = list.find((v) => v.id === props.currentVersionId) ?? list[0] ?? null
    const currentIndex = current === null ? -1 : list.findIndex((v) => v.id === current.id)
    // 版本号越大越新；列表按时间倒序时「上一版」在 currentIndex + 1
    const previous = currentIndex >= 0 ? list[currentIndex + 1] ?? null : null
    targetId.value = current?.id ?? null
    baseId.value = previous?.id ?? current?.id ?? null
    mode.value = 'entries'
    entries.value = []
    compared.value = false
  },
)

async function compare(): Promise<void> {
  if (!canCompare.value || baseId.value === null || targetId.value === null) {
    return
  }
  const base = baseId.value
  const target = targetId.value
  entriesLoading.value = true
  entriesError.value = null
  try {
    const result = await fetchVersionDiff(base, target)
    entries.value = result.entries
    compared.value = true
  } catch (e) {
    entriesError.value = toDisplayMessage(e)
    entries.value = []
    compared.value = false
  } finally {
    entriesLoading.value = false
  }
}

async function loadText(): Promise<void> {
  if (!canCompare.value || baseId.value === null || targetId.value === null) {
    return
  }
  const base = baseId.value
  const target = targetId.value
  textLoading.value = true
  textError.value = null
  try {
    const [a, b] = await Promise.all([fetchHocon(base), fetchHocon(target)])
    hoconA.value = a.content
    hoconB.value = b.content
  } catch (e) {
    textError.value = toDisplayMessage(e)
    hoconA.value = ''
    hoconB.value = ''
  } finally {
    textLoading.value = false
  }
}

watch(mode, (m) => {
  if (m === 'text' && hoconA.value === '' && hoconB.value === '' && !textLoading.value) {
    void loadText()
  }
})

function close(): void {
  open.value = false
}
</script>

<template>
  <el-dialog v-model="open" title="版本对比" width="1080px" top="6vh" append-to-body>
    <div class="vd">
      <div class="vd__bar">
        <el-select v-model="baseId" size="small" filterable class="vd__select" placeholder="基线版本">
          <el-option v-for="v in versions" :key="v.id" :value="v.id" :label="labelOf(v)" />
        </el-select>
        <span class="vd__arrow">→</span>
        <el-select v-model="targetId" size="small" filterable class="vd__select" placeholder="对比版本">
          <el-option v-for="v in versions" :key="v.id" :value="v.id" :label="labelOf(v)" />
        </el-select>
        <el-radio-group v-model="mode" size="small">
          <el-radio-button value="entries">制品条目</el-radio-button>
          <el-radio-button value="text">HOCON 文本</el-radio-button>
        </el-radio-group>
        <el-button type="primary" size="small" :loading="entriesLoading" :disabled="!canCompare" @click="compare">
          对比
        </el-button>
      </div>

      <div class="vd__body">
        <!-- 制品条目 -->
        <template v-if="mode === 'entries'">
          <ErrorState
            v-if="entriesError !== null"
            title="对比失败"
            :message="entriesError"
            retryable
            @retry="compare"
          />
          <EmptyState
            v-else-if="!compared"
            variant="no-data"
            title="尚未对比"
            description="选择两个版本后点「对比」，这里会列出路径级差异。"
          />
          <EmptyState
            v-else-if="entries.length === 0"
            variant="no-data"
            title="两个版本无差异"
            description="制品条目完全一致（哈希不同也可能只是生成上下文变了）。"
          />
          <el-table v-else :data="entries" size="small" border height="100%">
            <el-table-column label="变更" width="70">
              <template #default="{ row }">
                <span class="vd__kind" :class="KIND_TEXT[row.kind as DiffEntry['kind']].cls">
                  {{ KIND_TEXT[row.kind as DiffEntry['kind']].label }}
                </span>
              </template>
            </el-table-column>
            <el-table-column label="路径" min-width="260">
              <template #default="{ row }">
                <span class="elt-mono">{{ row.path }}</span>
              </template>
            </el-table-column>
            <el-table-column label="变更前" min-width="200">
              <template #default="{ row }">
                <SensitiveText :value="row.before ?? '—'" mono />
              </template>
            </el-table-column>
            <el-table-column label="变更后" min-width="200">
              <template #default="{ row }">
                <SensitiveText :value="row.after ?? '—'" mono />
              </template>
            </el-table-column>
          </el-table>
        </template>

        <!-- HOCON 文本并排 -->
        <template v-else>
          <ErrorState
            v-if="textError !== null"
            title="HOCON 读取失败"
            :message="textError"
            retryable
            @retry="loadText"
          />
          <div v-else-if="textLoading" class="elt-skeleton elt-skeleton--block vd__loading" />
          <EmptyState
            v-else-if="hoconA === '' && hoconB === ''"
            variant="no-data"
            title="尚未加载 HOCON"
            description="选择两个版本后切换到此视图会自动拉取两侧 HOCON。"
          />
          <Suspense v-else>
            <MergeViewer
              :a="hoconA"
              :b="hoconB"
              :a-label="baseVersion === null ? '基线' : labelOf(baseVersion)"
              :b-label="targetVersion === null ? '对比' : labelOf(targetVersion)"
            />
            <template #fallback>
              <div class="elt-skeleton elt-skeleton--block vd__loading" />
            </template>
          </Suspense>
        </template>
      </div>
    </div>

    <template #footer>
      <el-button size="small" @click="close">关闭</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.vd {
  display: flex;
  flex-direction: column;
  gap: 10px;
  height: 66vh;
}

.vd__bar {
  display: flex;
  gap: 8px;
  align-items: center;
}

.vd__select {
  width: 260px;
}

.vd__arrow {
  color: var(--elt-text-muted);
}

.vd__body {
  flex: 1;
  min-height: 0;
  overflow: hidden;
}

.vd__loading {
  height: 100%;
}

.vd__kind.is-added {
  color: var(--elt-success);
}

.vd__kind.is-removed {
  color: var(--elt-danger);
}

.vd__kind.is-changed {
  color: var(--elt-warn);
}
</style>
