# 前端页面编写约定

> 本文件是 `frontend/src/views/` 下所有页面的**施工契约**。基座层（utils / types / api / composables / stores / components/common / layouts / router）已冻结，页面只做消费，不得改动基座。
> 需求事实来源：`vibecoding_doc/模块概要设计/fe-0X-*.md`（各页面自己的那份）+ `fe-01-前端基座与工程约定.md`（全局约定）。

## 0. 铁律（违反即返工）

1. **分层**：`views → stores / api → client → axios`。页面可以直接 import `api/`（读与写都行），但受下面三条约束。

   > 这条原本写的是「写操作必须走 store」。四个页面域并行施工时全都做不到，追下来发现是**规则本身立错了**，故改成上面这句。理由：
   > - 写操作的反馈态（按钮 loading、字段级错误、弹窗内错误）属于「**这一次点击**」，不属于「这个业务域」。封进 store 会让 store 退化成通用请求包装器；同一个接口从两个弹窗调用时，还会互相污染 loading。
   > - 真正需要集中的只有两件事，而它们**已经**集中了：幂等键（`client.ts` 拦截器 + `useIdempotencyKey`）、写后刷新（store 的 `refreshAfterX()` / `query.reload()`）。
   >
   > 于是约束改为：
   > - 写操作**必须**经 `useAsyncTask`（保证 loading 与错误就地保留、不自动消失）；
   > - 写成功后**必须**调 store 的刷新动作，不要自己手搓列表刷新；
   > - **禁止**拼 URL / 直接碰 axios（仍走 `@/api/*`）；
   > - 治理关键动作（审批决定、Commit）**必须**透传 `useIdempotencyKey()` 的 per-intent 键。
   >
   > store 只负责**读取、缓存、筛选、分页**与「写后刷新」。
2. **不做授权判定**：`stores/project.myRoles` 只用于「按钮显不显示」。任何写操作都必须真的发出去、由后端裁决。**禁止** `if (!canApprove) return` 这种「前端替后端拒绝」的写法。
3. **不拼 URL、不碰 axios**：一切请求经 `@/api/*`。
4. **不解析 JWT**：角色/租户/项目只从 `GET /auth/me`（`authStore.profile`）读。
5. **不存敏感值**：localStorage 只允许 `elt-agent.auth`（token + 到期时间 + 用户摘要）与 UI 偏好。
6. **不实现领域算法**：门禁判定、PDP 评级、指纹计算全在后端 `backend/engines/`，前端只展示后端给的结果。
7. **注释写「为什么」不写「是什么」**，中文注释、英文标识符。
8. **全量类型注解**；`noUnusedLocals` / `noUnusedParameters` 已开，**不许**为了消警告而写无意义的 `export type {...}` 再导出。

## 1. 状态渲染的三态纪律

任何异步区块必须显式处理 **loading / empty / error** 三态，且：

- **error 优先于 empty**——加载失败绝不能渲染成「暂无数据」（用户会去新建一个本已存在的东西）；
- **empty 分三种**：`no-data`（该项目真没有）/ `no-match`（筛选没命中）/ `no-permission`（无权限），措辞与下一步引导都不同；
- 首次加载用**块级骨架**（`.elt-skeleton.elt-skeleton--block`），刷新用**顶部细条**（`QueryTable` 已内置）。**禁止全屏遮罩盖住已渲染内容**。

## 2. 通用组件（`@/components/common`）

一律 `import { X } from '@/components/common'`，不要写 `@/components/common/X.vue`。

| 组件 | 用途 | 关键 props |
|---|---|---|
| `PageHeader` | 页头 | `title` `description`；插槽 `actions` `notice` `tabs` |
| `SectionCard` | 内容卡片 | `title` `loading` `collapsible` `padded` `skeletonHeight`；插槽 `extra` |
| `QueryTable` | 列表表格外壳（**泛型**） | `items` `loading` `initialLoading` `error` `requestId` `total` `page` `pageSize` `filtered` `emptyVariant` `emptyText` `emptyDescription` `rowKey` `minHeight`；事件 `retry` `page-change` `page-size-change` `row-click`；插槽 `default`（放 `el-table-column`）`empty-actions` |
| `EmptyState` | 空态 | `variant`（必填）`title` `description`；插槽 `actions` |
| `ErrorState` | 错误态 | `message` `requestId` `title` `retryable`；事件 `retry` |
| `StatusTag` | 通用枚举徽章 | `kind`（`EnumKind`）`value` `size` `effect` `text` |
| `RiskLevelTag` | P0–P3 | `level` `reasons?: RiskReason[]` `size` |
| `RunStatusTag` | 运行状态 | `status` `animated` |
| `EnvBadge` | 环境 | `env` |
| `SensitiveText` | **后端文本的安全渲染出口** | `value` `mono` `multiline` `hostOnly` `placeholder` |
| `ArtifactHash` | 哈希 | `hash` `display`（`short`/`truncate`/`full`）`copyable` `label` |
| `DraftFrozenBadge` | 草稿/不可变 | `frozen` `frozenAt` `frozenBy` `draftText` |
| `ConfirmDialog` | 需留理由的确认 | `v-model` `title` `description` `requireReason` `minReasonLength` `loading`；事件 `confirm(reason)` |
| `DangerConfirmDialog` | 高危二次确认（输入短语） | `v-model` `title` `confirmPhrase` `acknowledge` `requireReason`；事件 `confirm({reason, acknowledged})` |
| `AsyncTaskPanel` | 写操作进度/失败 | `loading` `error` `loadingText` |

**凡是把后端给的字符串直接显示出来**（错误消息、诊断结论、连通性 target、日志行、元数据样本），一律用 `SensitiveText`，不要裸插值。

## 3. composables

```ts
// 列表页骨架：分页/筛选/清空/项目切换重载
const q = useListQuery<DataSource>(fetchDataSources, { immediate: true, pageSize: 20 })
// q: { items: Ref<T[]>, meta, loading, error, filter, total, totalPages,
//      isEmpty, initialLoading, load, reload, search, patchFilter, reset,
//      setPage, setPageSize, clear }
// 约定：search() / patchFilter() 回到第 1 页；reload() 保持当前页

// 轮询：终态自停 / 后台标签页暂停 / 不重入
const { active, start, stop } = usePolling(fetchOnce, {
  intervalMs: 2000,
  shouldStop: () => isRunTerminal(status.value),
})

// 一次性写操作：防双击 + loading + 错误保留
const task = useAsyncTask(() => store.freezeVersion(id), { successMessage: '已冻结' })
// task: { loading, error, done, run, reset }

// 幂等键：一次用户意图 = 一个 key（超时重试不会产生第二张准备单）
const { arm, config } = useIdempotencyKey()
await httpPost(url, body, config())

// 权限只用于显隐
const { isMaker, isCheckerData, isCheckerSecurity, isOperator, isAuditor, canSee, hasRole } = usePermission()

// 复制
const { copied, copy } = useCopy()
```

> ⚠️ **Pinia 深解包**：在**组件里**用 `useListQuery()`，`q.items` 是 `Ref<T[]>`，模板里自动解包、script 里要写 `q.items.value`。
> 从 **store** 读（如 `datasourceStore.query.items`）时，Pinia 的 `reactive()` 已经递归解包，`items` 直接就是 `T[]`，**不能**再写 `.value`。TypeScript 会抓出写错的那种。

## 4. utils

- `@/utils/enums`：`enumText(kind, v)` 取中文；`enumLabel(kind, v)` 取 `{label, tag, unknown}`；`enumOptions(kind)` 供下拉；`rolesText(roles)`；`hasAnyRole(roles, required)`。**全站枚举文案唯一来源**，页面不得自己写 `{ running: '运行中' }` 这类映射。
- `@/utils/format`：`DASH` `formatInt` `formatRows` `formatBytes` `formatRate` `formatRateOfTotal` `formatDuration` `formatRemaining` `formatCountdown` `formatDateTime` `formatDateTimeMs` `formatUtc` `hashShort` `truncateHash` `truncate` `textOrDash`。**禁止**引入 dayjs。
- `@/utils/errors`：`errorSpec(err)` `errorMessage(err)` `fieldErrors(err)` `toDisplayMessage(e)`。**分支一律判 `BizError.code`，绝不判 message 文本**。
- `@/utils/download`：`downloadFile(url, params, {filenamePrefix, format})`（导出走它，**不得**在浏览器里拼 CSV）、`copyText(text)`。
- `@/utils/feedback`：`ElMessage` / `ElMessageBox` / `ElLoading` **必须**从这里 import（样式已收敛在这里）。
- `@/utils/uuid`：`uuidv4()`。

## 5. 反馈方式的分工

| 场景 | 用什么 |
|---|---|
| 查询失败 | `ElMessage.error(toDisplayMessage(e))` 或区块内 `ErrorState` |
| 写操作失败 | **`AsyncTaskPanel`**（留在原地，不自动消失） |
| 轻确认（确定吗） | `ElMessageBox.confirm` |
| 需留理由的确认 | `ConfirmDialog` |
| 高危操作（发布/回滚/强制放行） | `DangerConfirmDialog`（要求逐字输入短语） |
| 成功提示 | `ElMessage.success` |

审批、拒绝、回滚、强制放行**必须**留下理由（`approval_decision.comment` / `evidence_event` 是审计主体）。

## 6. 视觉

- 只用 `src/styles/variables.scss` 里的 `--elt-*` 令牌，**不得发明色值**。
- 组件用 `<style scoped>`；需要穿透 Element 内部时用 `:deep()`。
- 字号：正文 13px、次要 12px、徽章 11px、区块标题 13px/600、页面标题 17px/600。
- 卡片圆角 `var(--elt-radius-card)`，控件圆角 `var(--elt-radius-control)`。
- 遵守 `prefers-reduced-motion`（动画已在基座组件里处理，新写的动画也要处理）。
- Element Plus 组件在模板里直接用（`<el-table>` / `<el-button>`，vite 按需解析）；图标从 `@element-plus/icons-vue` **显式 import 具体图标**，禁止 `import * as Icons`。

## 7. 目录与命名

```
views/<area>/<Name>View.vue        页面（路由组件，一个文件一个路由）
views/<area>/components/<Name>.vue 该页面域私有组件
```

私有组件放页面自己的 `components/` 子目录，**不要**往 `@/components/common` 里加业务组件。

## 8. 自我验收（提交前必须跑）

```bash
cd frontend && npx vue-tsc --noEmit
```

必须 0 error（`strict` + `noUnusedLocals` + `noUnusedParameters`）。

### 8.1 统计错误数时**不要**用 `| head`

```bash
# ✗ 错：head 打满行数就退出，上游收到 SIGPIPE 提前终止，tee 出来的文件也是截断的，
#      你会得出「只有 N 个错」的结论，而真实数字可能大得多（本项目实测 20 vs 55）。
npx vue-tsc --noEmit | head -30

# ✓ 对：先落盘，再从文件统计
npx vue-tsc --noEmit > /tmp/tsc.txt 2>&1; echo "exit=$?"
grep -c "error TS" /tmp/tsc.txt
```

### 8.2 首次 `vue-tsc` 通过 ≠ 类型安全，必须构建后再跑一次

`vite.config.ts` 里 `AutoImport` / `Components` 的 `dts` 指向 `src/auto-imports.d.ts` 与 `src/components.d.ts`。这两个文件由 **`vite build` 生成**，仓库里不存在。在它们缺失时，模板里的组件与其 props **根本没被类型检查**——`vue-tsc` 会报 0 error，而那是假象。

本项目实测：构建前的「0 error」在构建后变成 **55 个真实错误**（全部在模板里）。

```bash
npx vite build                # 生成两个 .d.ts
npx vue-tsc --noEmit          # 这一遍才是真的
```

## 9. 已知类型陷阱

### 9.1 `el-table` 插槽的 `row`

Element Plus 把 `el-table-column` 插槽的 `row` 标为 `DefaultRow = Record<PropertyKey, any>`，它**不从 `element-plus` 主入口导出**，也与我们的 DTO 无类型关联。两头都堵：

```vue
<!-- ✗ 加注解：插槽是函数类型，strictFunctionTypes 下参数位置逆变检查 → TS2345 -->
<template #default="{ row }: { row: DataSource }">

<!-- ✗ 不加注解：row 是 DefaultRow，作为参数传给 (row: DataSource) → TS2345 -->
<template #default="{ row }">
```

修法是**在文件内包一层具体类型的取值函数**，模板里调用它：

```ts
/** el-table 插槽 row 是 DefaultRow，与 DTO 无关联；在调用点收口，函数签名不放松 */
function dsOf(row: unknown): DataSource {
  return row as DataSource
}
```

```vue
<template #default="{ row }">
  <el-button @click="open(dsOf(row))">{{ dsOf(row).name }}</el-button>
</template>
```

- 只有「把 `row` 当参数传」才需要它；直接读 `row.name` 不报错（索引签名读出 `any`），**不必**逐处包。
- ⚠️ **禁止**在模板里写显式类型实参（`asRow<DataSource>(row)`）：Vue 模板表达式不支持，会引入解析错误。
- ⚠️ **禁止**为了消错把被调函数的签名放宽成 `any` / `unknown`，那会把整个文件的类型检查一起拉平。

### 9.2 Pinia 会把 setup store 的 ref 递归解包

store 内部写 `query.items.value`，组件里拿到的 `store.query.items` **已经是数组**。在 store 自己的代码里用 `.value`，出了 store 就不再用。详见 `src/composables/useListQuery.ts` 的文档注释。

### 9.3 命令式 API 必须显式 import

`ElMessage` / `ElMessageBox` / `ElLoading` 在脚本里是**值导入**，不是模板组件，自动导入声明缺失时是硬类型错误。统一从 `@/utils/feedback` 导入（它同时负责引入那些组件的 `style/css` 子路径）。
