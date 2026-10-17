<script setup lang="ts">
/**
 * 凭据分区（fe-03 §5.1 / §5.2）。
 *
 * 红线（CLAUDE.md §5.2）：界面不接收、不缓存、不回显凭据明文。因此：
 * - props 里**没有**任何凭据值，只有引用（Vault 路径 + 版本 + 键名）与键名清单；
 * - 输入框的语义是「写入一个新的 SecretReference」，不是「显示当前密码」，
 *   占位文案与帮助文案都必须把它说清楚，否则会被当成密码回显框；
 * - 输入值只存在于本组件与父级提交体，提交成功后由父级立刻清空。
 *
 * 为什么不把值放到 props 里：props 会出现在 Vue Devtools 的组件面板里，
 * 与「不写进 store」是同一个理由——可被检视的通道就不算安全通道。
 */
import { computed, ref, watch } from 'vue'
import { Key } from '@element-plus/icons-vue'
import SensitiveText from '@/components/common/SensitiveText.vue'
import { formatDateTime } from '@/utils/format'
import type { SecretReferenceView } from '@/types'

const props = withDefaults(
  defineProps<{
    /** 服务端下发的引用；新建时为 null */
    secretRef: SecretReferenceView | null
    /** 本次需要填写的键名（新建=类型要求；编辑=现有 key_names） */
    keyNamesHint: string[]
    /** 是否处于「填写新凭据」状态：新建恒为 true；编辑需用户点「重新设置凭据」 */
    editing: boolean
    /** 是否提供「重新设置凭据」入口（编辑态且已有引用时给） */
    canReset?: boolean
    /** 键名 → 错误文案 */
    errors?: Record<string, string>
  }>(),
  { canReset: false, errors: () => ({}) },
)

const emit = defineEmits<{
  'update:credentials': [values: Record<string, string>]
  'reset-credentials': []
}>()

/** 凭据值的内存副本：仅在本组件与父级提交体之间存在 */
const values = ref<Record<string, string>>({})

const keys = computed<string[]>(() => props.keyNamesHint)

function clear(): void {
  values.value = {}
  emit('update:credentials', {})
}

// 键名集合或编辑态一变就清空：把旧值跟着新键名一起提交，
// 会让上一个类型的凭据被写进这一版 Vault（服务端只按 key_names 落库）
watch(
  () => `${keys.value.join(',')}|${String(props.editing)}`,
  () => {
    clear()
  },
)

function setValue(key: string, value: string): void {
  values.value = { ...values.value, [key]: value }
  emit('update:credentials', { ...values.value })
}

function onReset(): void {
  clear()
  emit('reset-credentials')
}

/** 密码/密钥类键用密文输入；username 这类键名明文即可 */
function isSecretKey(key: string): boolean {
  return /password|passwd|pwd|secret|token|key/i.test(key)
}

function placeholderFor(key: string): string {
  return `输入新的 ${key}（提交后写入 Vault，不回显、不入库）`
}
</script>

<template>
  <div class="secret-section">
    <el-alert type="info" :closable="false" show-icon title="凭据只以 Vault 引用保存">
      <p class="secret-section__notice">
        平台不保存、不回显、不缓存凭据明文。下方填写的内容只用于
        <b>写入一个新的 Secret 版本</b>；提交后界面只显示 Vault 路径与版本号。
      </p>
    </el-alert>

    <!-- 已有引用：路径/版本/键名属于引用信息，不是凭据本身，可以展示 -->
    <dl v-if="secretRef !== null" class="secret-section__ref">
      <div class="secret-section__ref-row">
        <dt>Vault 路径</dt>
        <dd><SensitiveText :value="secretRef.path" mono /></dd>
      </div>
      <div class="secret-section__ref-row">
        <dt>版本</dt>
        <dd class="secret-section__mono">v{{ secretRef.version }}</dd>
      </div>
      <div class="secret-section__ref-row">
        <dt>键名</dt>
        <dd>
          <span
            v-for="key in secretRef.key_names"
            :key="key"
            class="secret-section__key secret-section__mono"
          >{{ key }}</span>
          <span v-if="secretRef.key_names.length === 0" class="secret-section__muted">无（该类型无需凭据）</span>
        </dd>
      </div>
      <div class="secret-section__ref-row">
        <dt>最近轮换</dt>
        <dd>{{ secretRef.last_rotated_at === null ? '未知' : formatDateTime(secretRef.last_rotated_at) }}</dd>
      </div>
    </dl>

    <!-- 非编辑态：只给占位与「重新设置」入口 -->
    <div v-if="!editing" class="secret-section__locked">
      <div v-for="key in keys" :key="key" class="secret-section__locked-row">
        <span class="secret-section__key secret-section__mono">{{ key }}</span>
        <span class="secret-section__dots" aria-label="凭据已设置，不回显">••••••••</span>
      </div>
      <p v-if="keys.length === 0" class="secret-section__muted">该类型无需凭据，Vault 引用为空。</p>

      <el-button v-if="canReset" :icon="Key" @click="onReset">重新设置凭据</el-button>
      <p v-else-if="keys.length > 0" class="secret-section__muted">
        轮换凭据请点「重新设置凭据」后重填<b>全部</b>键名。
      </p>
    </div>

    <!-- 编辑态：填写新的凭据值 -->
    <div v-else class="secret-section__form">
      <p v-if="keys.length === 0" class="secret-section__muted">
        该类型不走凭据（Vault 引用将为空），无需填写。
      </p>
      <el-form-item
        v-for="key in keys"
        :key="key"
        :label="key"
        :required="true"
        :error="errors[key] ?? ''"
      >
        <el-input
          :model-value="values[key] ?? ''"
          :type="isSecretKey(key) ? 'password' : 'text'"
          :show-password="isSecretKey(key)"
          :placeholder="placeholderFor(key)"
          autocomplete="new-password"
          class="secret-section__input"
          @update:model-value="(v: string) => setValue(key, v)"
        />
      </el-form-item>
      <p v-if="keys.length > 0" class="secret-section__hint">
        需一次性填全部 {{ keys.length }} 个键：部分更新会让新旧值混在同一个 Vault 版本里，
        日后无法判断哪一版才是有效凭据。
      </p>
    </div>
  </div>
</template>

<style scoped>
.secret-section {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.secret-section__notice {
  margin: 0;
  line-height: 18px;
}

.secret-section__ref {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 10px 12px;
  margin: 0;
  background: var(--elt-bg-elevated);
  border-radius: var(--elt-radius-control);
}

.secret-section__ref-row {
  display: flex;
  gap: 10px;
  align-items: baseline;
  font-size: 12px;
}

.secret-section__ref-row dt {
  flex: 0 0 72px;
  color: var(--elt-text-muted);
}

.secret-section__ref-row dd {
  margin: 0;
  min-width: 0;
  word-break: break-all;
}

.secret-section__key {
  display: inline-block;
  padding: 0 6px;
  margin-right: 6px;
  font-size: 12px;
  line-height: 18px;
  background: var(--elt-bg-elevated);
  border-radius: 4px;
}

.secret-section__mono {
  font-family: var(--elt-font-mono);
}

.secret-section__locked {
  display: flex;
  flex-direction: column;
  gap: 8px;
  align-items: flex-start;
}

.secret-section__locked-row {
  display: flex;
  gap: 8px;
  align-items: center;
}

.secret-section__dots {
  font-family: var(--elt-font-mono);
  letter-spacing: 2px;
  color: var(--elt-text-muted);
}

.secret-section__input {
  max-width: 420px;
}

.secret-section__muted,
.secret-section__hint {
  margin: 0;
  font-size: 11px;
  line-height: 16px;
  color: var(--elt-text-muted);
}
</style>
