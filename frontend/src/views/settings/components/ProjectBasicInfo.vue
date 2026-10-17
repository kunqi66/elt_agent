<script setup lang="ts">
/**
 * 项目基本信息（fe-08 §1.2 / §6.2）。
 *
 * 两条刻意的约束：
 * 1. **env 与 code 不可编辑**。把 dev 提升为 prod 必须新建项目——
 *    否则可以用「改一下环境标记」绕过生产审批要求（fe-08 §6.2）。
 *    因此界面只展示，不提供任何 env/code 的输入入口。
 * 2. **不推导权限**。PUT /projects/{id} 目前没有独立的能力位下发
 *    （fe-08 §10 未决），这里用「能管成员或能管角色」作为编辑入口的显隐依据，
 *    仅为体验层；真正裁决在服务端，写操作照样发出。
 */
import { computed, ref, watch } from 'vue'
import { EditPen, InfoFilled } from '@element-plus/icons-vue'
import { ElMessage } from '@/utils/feedback'
import { toDisplayMessage } from '@/utils/errors'
import { useAsyncTask } from '@/composables/useAsyncTask'
import * as projectApi from '@/api/project'
import { useProjectStore } from '@/stores/project'
import { EnvBadge, SectionCard, StatusTag } from '@/components/common'
import { formatInt } from '@/utils/format'
import type { ProjectRead, ProjectUpdate } from '@/types'

const props = defineProps<{ project: ProjectRead }>()

const projectStore = useProjectStore()

const canEdit = computed<boolean>(
  () => props.project.can_manage_members || props.project.can_manage_roles,
)

const dialogOpen = ref(false)
const name = ref('')
const description = ref('')

/** 每次打开都从服务端最新值回填：上一次改到一半取消的内容不该残留 */
watch(dialogOpen, (open) => {
  if (open) {
    name.value = props.project.name
    description.value = props.project.description
  }
})

const trimmedName = computed<string>(() => name.value.trim())
const nameInvalid = computed<boolean>(() => trimmedName.value.length === 0)

const { loading: saving, error: saveError, run: save } = useAsyncTask(
  async (patch: ProjectUpdate) => {
    await projectApi.updateProject(props.project.id, patch)
    // 项目名出现在所有列表与账本载荷里，改完必须让全局上下文与列表同步
    await projectStore.loadProjects()
  },
  {
    successMessage: '项目信息已更新',
    onSuccess: () => {
      dialogOpen.value = false
    },
  },
)

function submit(): void {
  if (nameInvalid.value || saving.value) {
    return
  }
  const patch: ProjectUpdate = {
    name: trimmedName.value,
    description: description.value.trim(),
  }
  // 写操作真的发出去，由后端裁决；失败留在弹窗里（AsyncTaskPanel 在模板中）
  save(patch).catch((e: unknown) => {
    ElMessage.error(toDisplayMessage(e))
  })
}
</script>

<template>
  <div class="basic">
    <SectionCard title="项目基本信息" padded>
      <template #extra>
        <el-button
          v-if="canEdit"
          size="small"
          :icon="EditPen"
          @click="dialogOpen = true"
        >
          编辑项目信息
        </el-button>
      </template>

      <el-alert
        v-if="project.env === 'prod'"
        class="basic__prod"
        type="error"
        :closable="false"
        show-icon
        title="生产环境（prod）"
        description="生产环境下的执行需完整四眼审批；写正式表的动作触发 P0 判定。环境标记不可编辑——把 dev 提升为 prod 必须新建项目。"
      />

      <dl class="basic__grid">
        <div class="basic__item">
          <dt>项目名称</dt>
          <dd>{{ project.name }}</dd>
        </div>
        <div class="basic__item">
          <dt>项目编码</dt>
          <dd class="elt-mono">{{ project.code }}</dd>
        </div>
        <div class="basic__item">
          <dt>环境</dt>
          <dd>
            <EnvBadge :env="project.env" />
            <span class="basic__hint">不可编辑</span>
          </dd>
        </div>
        <div class="basic__item">
          <dt>状态</dt>
          <dd><StatusTag kind="ProjectStatus" :value="project.status" /></dd>
        </div>
        <div class="basic__item">
          <dt>成员数</dt>
          <dd>{{ formatInt(project.member_count) }}</dd>
        </div>
        <div class="basic__item basic__item--wide">
          <dt>描述</dt>
          <dd class="basic__desc">{{ project.description === '' ? '—' : project.description }}</dd>
        </div>
      </dl>

      <p class="basic__note">
        <el-icon :size="12"><InfoFilled /></el-icon>
        项目编码与环境是 PDP 判定的关键输入，刻意不可修改；如需提升环境，请新建项目。
      </p>
    </SectionCard>

    <el-dialog
      v-model="dialogOpen"
      title="编辑项目信息"
      width="480px"
      append-to-body
      :close-on-click-modal="!saving"
      :close-on-press-escape="!saving"
      :show-close="!saving"
    >
      <el-form label-position="top">
        <el-form-item label="项目名称" required>
          <el-input
            v-model="name"
            :disabled="saving"
            maxlength="64"
            show-word-limit
            placeholder="项目名称会出现在所有列表与证据账本载荷中"
          />
          <p v-if="nameInvalid && name.length > 0" class="basic__field-error">项目名称不能为空。</p>
        </el-form-item>
        <el-form-item label="描述">
          <el-input
            v-model="description"
            type="textarea"
            :rows="3"
            :disabled="saving"
            maxlength="200"
            show-word-limit
          />
        </el-form-item>
      </el-form>

      <el-alert
        type="info"
        :closable="false"
        show-icon
        title="本次修改已写入证据账本（操作人 / 时间 / 影响对象 / 理由）"
      />

      <el-alert
        v-if="saveError !== null"
        class="basic__dialog-error"
        type="error"
        :closable="false"
        show-icon
        :title="saveError"
      />

      <template #footer>
        <el-button :disabled="saving" @click="dialogOpen = false">取消</el-button>
        <el-button
          type="primary"
          :loading="saving"
          :disabled="nameInvalid"
          @click="submit"
        >
          保存
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.basic__prod {
  margin-bottom: 14px;
}

.basic__grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 14px 24px;
  margin: 0;
}

.basic__item {
  display: flex;
  flex-direction: column;
  gap: 3px;
  min-width: 0;
}

.basic__item--wide {
  grid-column: 1 / -1;
}

.basic__item dt {
  font-size: 11px;
  color: var(--elt-text-muted);
}

.basic__item dd {
  display: flex;
  align-items: center;
  gap: 6px;
  margin: 0;
  font-size: 13px;
  color: var(--elt-text-primary);
}

.basic__hint {
  font-size: 11px;
  color: var(--elt-text-muted);
}

.basic__desc {
  font-size: 12px;
  line-height: 1.6;
  color: var(--elt-text-secondary);
  word-break: break-word;
}

.basic__note {
  display: flex;
  align-items: center;
  gap: 6px;
  margin: 16px 0 0;
  font-size: 11px;
  color: var(--elt-text-muted);
}

.basic__field-error {
  margin: 2px 0 0;
  font-size: 11px;
  color: var(--elt-danger);
}

.basic__dialog-error {
  margin-top: 12px;
}
</style>
