<script setup lang="ts">
/**
 * 四眼与互斥规则说明（fe-08 §6.3）。
 *
 * 逐条用文字列出，不用图示代替——规则是审计口径的一部分，
 * 图省事画个流程图会让「谁不能审什么」在细节上说不清。
 *
 * 再次强调：这些是**服务端与 DB 约束**，前端只做可视化与提示；
 * 界面上给不给提交入口都不构成拦截（CLAUDE.md §5.8）。
 */
const rules: ReadonlyArray<{ index: number; text: string }> = [
  {
    index: 1,
    text: '同一项目内，maker 与 checker_data、checker_security 互斥：一个人不能既提交又审批。',
  },
  {
    index: 2,
    text: '申请人不得审批自己的 Preparation，即使他通过其他途径拿到了审批角色。',
  },
  {
    index: 3,
    text: 'checker_data 与 checker_security 可由同一人兼任（两个槽位仍是独立的两次决定），但 P1 场景下建议由不同人担任。',
  },
  {
    index: 4,
    text: 'operator 只能对审批已通过且指纹一致的版本执行 Commit，不能修改准备单内容。',
  },
  {
    index: 5,
    text: 'auditor 全程只读，不能执行任何副作用操作。',
  },
]
</script>

<template>
  <div class="four-eyes">
    <ol class="four-eyes__list">
      <li v-for="rule in rules" :key="rule.index" class="four-eyes__item">
        {{ rule.text }}
      </li>
    </ol>
    <p class="four-eyes__note">
      以上规则的判定与拒绝由服务端与数据库约束完成；前端仅在授予时给出提示，
      不禁用提交——禁用会掩盖后端的权威判定。
    </p>
  </div>
</template>

<style scoped>
.four-eyes__list {
  padding-left: 18px;
  margin: 0;
  font-size: 12px;
  line-height: 1.9;
  color: var(--elt-text-secondary);
}

.four-eyes__item + .four-eyes__item {
  margin-top: 2px;
}

.four-eyes__note {
  padding-top: 10px;
  margin: 10px 0 0;
  font-size: 11px;
  line-height: 1.7;
  color: var(--elt-text-muted);
  border-top: 1px solid var(--elt-border-subtle);
}
</style>
