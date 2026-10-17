# dev mock 适配层

后端 be-02 … be-21 尚未全部落地时，让整站 UI 可以先跑起来。做法是把共享 axios 实例
（`src/api/client.ts` 导出的 `http`）的 **adapter** 换成一张内存路由表——
请求的 URL、HTTP 方法、query 参数名、响应信封与真实后端**逐字一致**，
所以切回真后端不需要改任何业务代码。

```
src/api/mock/
├── index.ts      # axios adapter + 路由表 + 幂等缓存
├── fixtures.ts   # 确定性种子数据（唯一事实来源）
└── README.md
```

## 怎么开关

开关是环境变量，不是代码改动：

| 文件 | 取值 | 效果 |
|---|---|---|
| `.env.development` | `VITE_USE_MOCK=true` | 全部请求被 mock 接管，**后端可以完全不在线** |
| `.env.production` | `VITE_USE_MOCK=false` | 一律走真实接口 |

`src/main.ts` 的 `bootstrap()` 按需动态引入：

```ts
if (import.meta.env.VITE_USE_MOCK === 'true') {
  const { installMockAdapter } = await import('@/api/mock')
  installMockAdapter()
}
```

用**动态** `import` 是刻意的：生产构建里该条件恒为字面量假，Rollup 会整段摇掉，
mock 数据不会进生产包——否则它会与真实证据账本混淆，污染审计口径。

后端按模块逐个落地时，把 `VITE_USE_MOCK` 切成 `false` 即可整体真连；
若只想真连一部分，需要拆成按模块的开关（当前未提供，避免过度设计）。

### 双击保险

`installMockAdapter()` 自带两道护栏：

1. **幂等**：重复调用只装一次。
2. **生产拒绝**：`import.meta.env.PROD` 为真时直接 `console.error` 并返回，
   即便有人误把 `VITE_USE_MOCK=true` 带进生产构建，也不会接管请求。

## 被 mock 了什么

覆盖 `src/api/*.ts` 的全部 89 个函数（auth / project / datasource / pipeline /
preparation / run / monitor / governance）：

- **认证**：登录（含可复现的密码错误分支）、刷新、登出、当前用户、租户、改密。
- **项目**：项目 CRUD、成员增删、候选用户、职责槽授予/回收。
- **数据源**：CRUD、启用/停用、删除冲突检测（被 Pipeline 引用时返回 `CONFLICT`）、
  重新设置凭据、连通性测试、元数据探查、schema 快照、脱敏样本。
- **Pipeline**：列表/详情、生成会话（澄清中断态 → 生成完成态 → 取消）、
  EtlPlan / HOCON / 门禁结果（**四种 `GateLocator` 全覆盖**，含 P0 阻断与 P1 告警）、
  自动修复、版本冻结、版本列表、版本 Diff。
- **准备单**：列表（服务端 scope 过滤）、详情、时间线、创建、风险理由、
  提交审批决定、Commit。**P0 永远不可批准**，自批返回 `SELF_APPROVAL_FORBIDDEN`，
  审批槽未集齐时 Commit 返回 `APPROVAL_INCOMPLETE`。
- **运行**：列表/详情、指标时序、日志（`seq` 游标增量拉取）、质量报告、
  错误摘要分页、暂停/恢复/取消/回滚、根因诊断（含 `degraded=true` 的模型不可用回退态）。
- **总览**：`24h / 7d / 30d` 三个 range 的 summary / metrics / series / 分布 / 健康度 /
  资产计数 / 质量概览。
- **治理**：策略规则 CRUD 与启停、策略版本与 Diff、预算默认值（越硬上限返回
  `VALIDATION_ERROR`）、Benchmark 套件/运行/用例（L0 / L1 / L2）、
  证据账本游标分页查询与验链（项目 2 固定返回断链结果，便于查看失败态）。

数据是**确定性**的：所有时间取自 `fixtures.ts` 里的固定基准时刻，模块作用域内
不出现 `Date.now()`；分页 `total` 与列表顺序在两次请求之间不漂移，
不会出现「列表自己抖动」这种假 bug。

### 写接口是真的会写

`POST/PUT/DELETE` 会就地改写内存里的 fixtures 数组，写完之后重新拉列表能看到变化——
这才能验证「创建完跳列表」「审批完状态变化」这类流程，而不是永远看同一份静态快照。

同时复刻了后端的 **幂等语义**：同一个 `Idempotency-Key` 再次到达时，直接回放第一次的
响应体，**不会二次执行副作用**（否则 401 静默刷新后的重放会变成创建两条数据）。

### 错误是「同形」的

业务失败不是伪造一个假成功，而是抛出真正的 `AxiosError`（带上非 2xx 的响应体
`{code, message, request_id, details}`），由 `client.ts` 的响应拦截器按真实错误路径处理：
弹 toast、走 401 静默刷新、抛 `BizError`。因此页面的错误分支、`skipErrorToast`、
`skipUnauthorizedHandler` 这些行为都能在 dev 环境被真实触发。

未匹配到的路由会 `console.warn` 出具体的 `METHOD /path`，并返回 404 `NOT_FOUND`——
静默 404 会让人误以为「后端没实现」，而不是「mock 漏了路由」。新增接口时看控制台即可。

## 故意没有 mock 的东西

以下都**不是**这一层能替代的，遇到相关需求请指向真环境：

- **Vault**：没有任何密钥读取逻辑。`secret_ref` 只是一个展示用的 `{path, version, key_names}`
  结构，不存在可用的凭据值，也不会有「物化到内存」这一步。
- **SeaTunnel / Doris / MySQL**：没有真实作业提交与 DDL。运行状态、指标、日志是造出来的，
  `POST /runs/:id/rollback` 只改内存状态，不会碰任何影子表或正式表。
- **Capability / ReplayGuard / Outbox / 证据账本签名**：`POST /preparations/:id/commit`
  只返回 `{run_id, status:'queued', queued_at}`，不签发也不返回任何令牌；
  验链结果由 fixtures 直接给出，前端不做哈希计算（真实验链在服务端）。
- **PDP 判定**：风险等级、`required_slots`、`my_slot`、`can_commit` 全部写死在 fixtures 里，
  mock 不复刻规则引擎——这些值在真实系统里必须由后端确定性算出。
- **脱敏与解敏**：样本本来就是掩码形态（`138****5678`、`u***@example.com`），
  mock 不提供「查看原文」的路径，也不存在原文。

## 红线（改这个目录前先读）

1. **不放任何明文凭据或连接串**（CLAUDE.md §5.2）。`endpoint` 只放 host/port/database
   这类非凭据键；HOCON 里只用 `${VAULT_*}` / `${MYSQL_HOST}` 变量占位。
2. **不放真实个人身份信息**。示例姓名、手机号、邮箱全部是掩码形态或显而易见的占位值。
3. **不接收也不回显凭据值**。`credentials` 请求体只取 `Object.keys()` 存进 `secret_ref.key_names`，
   值当场丢弃；任何响应都不含凭据。
4. **类型即契约**。fixtures 全部用 `@/types` 的 DTO 标注，后端字段名漂移会在
   `vue-tsc` 阶段报错，而不是等到运行时白屏。
5. **确定性**。不要在 fixtures 里引入 `Math.random()` / `Date.now()`
   （`index.ts` 里唯一的 `Math.random()` 只用于模拟网络延迟，不影响数据）。

## 自检

```bash
cd frontend && npx vue-tsc --noEmit
```

mock 目录应当零错误。若新增了 `src/api/*.ts` 的函数而这里没跟上，
控制台会出现 `[mock] 未匹配到路由：…`，据此补 `index.ts` 的 `ROUTES` 即可。
