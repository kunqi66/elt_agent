<script setup lang="ts">
// 应用根组件：布局与页面全部由路由渲染。
// 这里刻意不放任何业务状态——项目上下文、轮询、鉴权回调都在各自 store/守卫里。
// 顶部「生产环境」红条属于 DefaultLayout：登录页不该显示项目环境标识。
// mock 角标反过来挂在根上：mock 是最容易被误认成「后端就是这么返回的」的状态，
// 登录页也得看得见。
import MockModeBadge from '@/components/common/MockModeBadge.vue'
</script>

<template>
  <router-view />
  <MockModeBadge />
</template>

<style>
/* 全局基线：只有尺寸复位与底色，具体令牌见 styles/variables.scss */
html,
body,
#app {
  height: 100%;
  margin: 0;
  padding: 0;
}

body {
  font-family: var(--elt-font-ui);
  font-size: 13px;
  line-height: 1.5;
  background-color: var(--elt-bg-app);
  color: var(--elt-text-primary);
  -webkit-font-smoothing: antialiased;
  -moz-osx-font-smoothing: grayscale;
}

/* 链接与选中的默认色跟随主题，避免浏览器默认蓝 */
a {
  color: var(--elt-accent);
  text-decoration: none;
}

a:hover {
  text-decoration: underline;
}

::selection {
  background-color: var(--elt-accent-soft);
  color: var(--elt-text-primary);
}

/* Element Plus 的遮罩与弹层挂在 body 下，同样需要暗色底 */
.el-overlay {
  background-color: rgb(3 7 18 / 70%);
}
</style>
