# elt_agent 前端控制台

Vue 3 + TypeScript(strict) + Vite + Pinia + Element Plus + ECharts + Vue Flow + CodeMirror 6。

页面编写约定见 [CONVENTIONS.md](./CONVENTIONS.md)；各页面需求见 `vibecoding_doc/模块概要设计/fe-0X-*.md`；**受接口限制而不能闭环的事项、契约漂移、需后端补的能力**见 [已知缺口与待办.md](./已知缺口与待办.md)。

## 快速开始

```bash
cd frontend
npm install
cp .env.example .env.development   # 两个变量见下；漏了它开发环境不会走 mock
npm run dev                        # http://localhost:5173
```

`frontend/.env.*` 不在版本控制里（仓库根 `.gitignore` 排除 `.env.*`，防密钥入库），
所以本地必须自己 `cp` 一次。漏了也能跑——`client.ts` 的 baseURL 有 `?? '/api/v1'` 兜底——
但会变成「连真实后端、不开 mock」。

开发服把 `/api` 透传到 `http://localhost:8000`（见 `vite.config.ts`），因此本地起后端后无需处理跨域。

## mock 开关

界面在 `be-09`…`be-20` 落地前也能完整查看——请求路径与 DTO 全是真的，只是响应由本地适配器伪造。

| 文件（由 `.env.example` 复制） | `VITE_USE_MOCK` | 行为 |
|---|---|---|
| `.env.development` | `true` | 走 `src/api/mock/`，无需后端 |
| `.env.production` | `false` | 走真实接口 |

改成 `false` 即切到真实后端，**不需要改任何页面代码**。

> 生产**永不开 mock**：伪造数据会与真实证据账本混淆，审计口径不可接受。
> `main.ts` 用 `import.meta.env.VITE_USE_MOCK === 'true'` 做静态判定 + 动态 `import()`，
> 生产构建会把整个 mock 模块摇掉，不进包；`installMockAdapter()` 自己还有一层
> `import.meta.env.PROD` 拒绝安装的兜底。**「产物里搜不到 mock」已实测验证**（`grep dist/`）。

## 命令

```bash
npm run dev          # 开发服
npm run type-check   # vue-tsc --noEmit + tsc --noEmit -p tsconfig.node.json
npm run build        # 先 type-check，再 vite build
npm run preview      # 预览 dist
```

`npm run build` 刻意把 `type-check` 串在前面：类型不过就不该产出可部署的包。

## 目录

```
src/
├── api/            # HTTP 出口：client.ts 是唯一 axios 实例；mock/ 是 dev 适配器
├── stores/         # Pinia：认证、项目上下文、各业务域
├── composables/    # useListQuery / usePolling / useAsyncTask / usePermission …
├── components/common/  # 全站共用组件（状态徽章、查询表格、三态、确认弹窗…）
├── layouts/        # DefaultLayout + 侧边栏/顶栏/项目切换/用户菜单
├── views/          # 按页面域分目录，一个文件一个路由
├── router/         # 路由表 + 守卫 + 导航配置
├── types/          # 与后端 DTO 逐字对齐的类型
├── utils/          # enums / format / errors / mask / download / feedback（无依赖叶子层）
└── styles/         # variables.scss 是设计令牌唯一来源
```

依赖方向单向：`views/components → stores → api → client → axios`，`types/`、`utils/` 是无依赖叶子层。反向依赖（`api/` import `stores/`、`stores/` import 视图）一律禁止。

## 前端不做的事

- **不做授权判定**：`meta.requiredRoles` 与按钮置灰只是可见性优化，真正的裁决在后端 `role_assignment`。
- **不解析 JWT**：角色、租户、项目只从 `GET /auth/me` 读。
- **不存敏感值**：localStorage 只有 `elt-agent.auth`（token + 到期时间 + 用户摘要）与 UI 偏好。
- **不实现领域算法**：门禁、PDP 评级、制品指纹全在 `backend/engines/`。
- **不展示密钥明文**：界面只出现 `SecretReference`（Vault 路径 + 版本）。

## 已知环境注意

- 本机（Windows 开发机）没有 Docker / WSL / Java，容器相关操作在 ECS 上做；前端只需 Node。
- `typescript` 锁在 `^5.9.3`：`vue-tsc@3.3.13` 声明的 peer 范围是 `>=5.0.0`，但它加载的是 TS 旧的包布局（`typescript/lib/tsc`），TS 7 移除了该子路径，装 7.x 会直接崩。
