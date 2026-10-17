# CLAUDE.md — elt_agent 工作准则

> 本文件是 Claude Code 在本仓库工作时的**唯一行为准则**。与本文件冲突的做法一律以本文件为准。
> 需求事实来源：[vibecoding_doc/PRD.md](vibecoding_doc/PRD.md)（SSOT）→ [vibecoding_doc/概要设计总纲.md](vibecoding_doc/概要设计总纲.md) → [vibecoding_doc/核心数据字典.md](vibecoding_doc/核心数据字典.md) → `vibecoding_doc/模块概要设计/` → `vibecoding_doc/specs/`。
> 所有已拍板决策见 [vibecoding_doc/需求对齐记录.md](vibecoding_doc/需求对齐记录.md)，**不要重开已冻结的讨论**。

## 1. 项目一句话

自然语言需求 → EtlPlan / SeaTunnel HOCON → 确定性门禁 → 不可变版本 → 四眼审批 → 单次 Capability → Celery + SeaTunnel 执行 → 影子表与质量分流 → 原子 Swap 发布，全程由 Harness 安全内核约束、留哈希链证据。

## 2. 技术栈（已锁定，不得替换）

| 层 | 选型 |
|---|---|
| 语言/包管理 | Python 3.12 + `uv` |
| 控制面 | FastAPI + SQLAlchemy(async, asyncpg) + Alembic + Pydantic v2 |
| 编排 | LangGraph（Checkpoint 存 PostgreSQL） |
| 异步 | Celery（worker + beat），broker/result 用 Redis |
| 控制面库 | PostgreSQL 16 |
| 缓存/锁 | Redis 7 |
| 演示数据源 | MySQL 8（源）、Doris 2.1 all-in-one（目标）、MinIO（文件/对象存储） |
| 数据面 | Apache SeaTunnel（Zeta local 模式，官方镜像） |
| 密钥 | HashiCorp Vault（KV v2 + AppRole） |
| 模型 | DeepSeek（`deepseek-chat` / `deepseek-reasoner`），Provider 抽象层隔离 |
| 令牌 | Ed25519（EdDSA）签名 JWT |
| 可观测 | structlog + Prometheus + Grafana |
| 前端 | Vue 3 + TS(strict) + Vite + Pinia + Element Plus + ECharts + axios + Vue Flow + CodeMirror 6 |
| 部署 | Docker Compose 单机（阿里云 ECS） |

## 3. 目录结构

```
elt_agent/
├── backend/
│   ├── api/          # HTTP 路由层：校验、依赖注入、权限、调用 service、统一响应
│   ├── services/     # 业务编排层
│   ├── engines/      # 纯领域算法层（PDP、门禁、HOCON、指纹、DAG 分析）
│   ├── clients/      # 外部系统封装（LLM Provider、Vault、SeaTunnel、MinIO、各库驱动）
│   ├── harness/      # 安全执行内核（ToolBroker / Capability / ReplayGuard / Outbox / Ledger）
│   ├── workflow/     # LangGraph 状态机与节点
│   ├── tasks/        # Celery 任务编排
│   ├── models/       # SQLAlchemy ORM
│   ├── schemas/      # Pydantic DTO
│   └── core/         # settings / logging / errors / response / security / db / ids / deps
├── frontend/         # Vue 3 控制台
├── deploy/           # nginx、各库 init 脚本、备份脚本、监控配置
├── vibecoding_doc/   # PRD / 概要设计 / 数据字典 / 模块概要设计 / specs
└── docker-compose.yml
```

## 4. 分层铁律（违反即返工）

1. `api/` **禁止**业务逻辑；只做参数校验、依赖注入、权限检查、调用 service、组装响应。
2. `services/` 可调用 `engines/` `clients/` `models/` `harness/`，**禁止** `import fastapi`。
3. `engines/` 是**纯函数层**：可依赖 Pydantic 与标准库，**禁止** `import fastapi`、禁止访问数据库/Redis/网络。数据一律由 service 传入、结果返回给 service。
4. `harness/` **禁止**依赖 `services/` 与 `api/`（单向依赖）；它是所有副作用的唯一出口。
5. `tasks/` 只做编排，业务逻辑下沉 `services/`。
6. 跨层只传 **Pydantic 模型**，不传 ORM 实例。
7. 所有 SQL 查询在 repository/service 层强制带 `tenant_id` 过滤。

## 5. 安全红线（不可协商）

1. **副作用唯一出口**：任何写外部系统、执行作业、改数据的动作必须经 `harness.ToolBroker`。禁止 `api/`、`services/` 直接调用 SeaTunnel 或目标库 DDL。
2. **无明文 Secret**：Secret 不得出现在日志、API 响应、数据库明文列、LLM 上下文、证据账本、异常堆栈、前端任何接口。仅允许 `SecretReference`（Vault 路径 + 版本）与 Worker 内存中临时物化。
3. **PDP 必须确定性**：风险评级只用 `engines/policy` 的规则表，**严禁**让 LLM 参与。
4. **不可变制品禁止 UPDATE**：`pipeline_version`、`preparation`、`approval_decision`、`capability`、`evidence_event` 只允许 INSERT；修正只能产生新版本。
5. **证据账本单入口**：只能通过 `harness.EvidenceLedger.append()` 写入，写入时自动计算 `prev_hash` / `event_hash`。
6. **Capability 单次消费**：消费必须是原子的（Redis `SETNX` + PG 唯一约束），禁止「先查后写」模式。
7. **脱敏默认开启**：元数据样本、错误行原始值一律脱敏；错误表只存受保护摘要（哈希 + 截断），不存原文。
8. **四眼强制**：审批人不得等于 Preparation 发起人，数据审批槽与安全审批槽必须由不同人担任；校验在**服务端 + DB 约束**兜底，不能只做前端置灰。

## 6. 代码规范

- 全量类型注解；`ruff` 格式化与 lint；`mypy` 检查 `engines/` 与 `harness/`。
- 异步优先（asyncpg / httpx）；Celery 任务内使用同步 session 或显式 `asyncio.run` 包裹。
- 统一错误码与响应信封：`core/errors.py`、`core/response.py`。禁止把数据库异常原样抛给前端。
- 所有可能重放的写接口必须支持 `Idempotency-Key` 请求头。
- 日志一律用 `structlog`，绑定 `request_id / tenant_id / project_id / subject_id / pipeline_version_id / execution_run_id`；脱敏由 logging 处理器统一完成，业务代码不得自行拼接敏感值。
- 注释写「为什么」，不写「是什么」；中文注释，标识符英文。
- 新增依赖必须同步更新 `backend/pyproject.toml`，用 `uv add`，不要手写版本号。

## 7. 常用命令

```bash
# 依赖
cd backend && uv sync

# 本地起基础设施（PG / Redis / MinIO / Vault）
docker compose up -d postgres redis minio vault

# 起全套（含 api / worker / beat / nginx）
docker compose up -d --build

# 演示数据面（MySQL / Doris / SeaTunnel，吃内存，按需起）
docker compose --profile demo up -d

# 迁移：生成新 revision 在本机做；**应用**迁移只在容器里做（compose 的 migrate 服务自动执行）
cd backend && uv run alembic revision --autogenerate -m "msg"
docker compose run --rm migrate          # = alembic upgrade head，ECS 上执行

# 租户开张（空库必备：没有它一个用户都没有，登录无从谈起）
# 口令从 ELT_BOOTSTRAP_PASSWORD 环境变量读；不传则随机生成并只打印一次
ELT_BOOTSTRAP_PASSWORD='<自己定>' docker compose run --rm api \
  python scripts/bootstrap_tenant.py --tenant-code demo --project-code sales_wide

# 测试
cd backend && uv run pytest -q

# 一键演示（真 HTTP + 真 worker；口令只从环境变量进，脚本里没有默认值）
# 目标端默认 doris；数据面起不来时用 --target mysql（表名/列形状一致）
cd backend && ELT_DEMO_PASSWORD='<demo2 的口令>' PYTHONIOENCODING=utf-8 \
  uv run python scripts/demo_run.py --scenario 1 --target mysql

# ---- 本地开发（控制面在本机、基础设施在 ECS）：开发与演示默认走这条 ----
# 隧道把 ECS 的 127.0.0.1 端口映射到本机同号端口；口令只从环境变量进，不写文件
# 注意 Redis 是 6380、演示 MySQL 是 3307（compose 里避开了 6379/3306）
ELT_ECS_PASSWORD='<ECS 口令>' uv run --with paramiko python deploy/tunnel.py 5432 6380 8200 3307
# 终端 2：**必须走 dev_api.py，不要用 `uv run uvicorn main:app`**（Windows 上的事件循环类型，
# 见 §9 的 psycopg/ProactorEventLoop 一条）。没有模型 key 时用 LLM_PROVIDER=fake：
# 它是正式通路（确定性规则起草），不是测试开关；写了 deepseek 而没有 key 会 503。
cd backend && LLM_PROVIDER=fake PYTHONIOENCODING=utf-8 uv run python dev_api.py
cd backend && PYTHONIOENCODING=utf-8 uv run celery -A tasks.celery_app worker -P solo -l info -Q etl.default,etl.execute   # 终端 3（两个队列都要：提交/停止/收尾在 etl.execute，漏了会静默不消费）
cd frontend && npm run dev                                                  # 终端 4（用 localhost 访问）
```

## 8. 进度表

| 里程碑 | 范围 | 状态 |
|---|---|---|
| **M1 环境与骨架** | PRD 恢复、CLAUDE.md、概要设计总纲、核心数据字典、compose 骨架、后端分层骨架、登录鉴权、多租户、数据源管理、元数据探查、脱敏样本 | ✅ **后端已完成**（specs M1-01…M1-05；前端控制台另计） |
| **M2 生成链路** | LangGraph 澄清/中断/Checkpoint 恢复、EtlPlan + HOCON 生成、确定性门禁、有限自动修复、不可变版本冻结与 Diff | ✅ **后端已完成并端到端跑通**（需求 → 5 个澄清问题 → Plan 10 条映射 → HOCON 885 字符 → 门禁 `pass` → `frozen`；重复冻结被 409 拒）。spec 目前只有 M2-01（PDP），be-09/10/11/12 先落地代码、spec 待补 |
| **M3 治理链路** | Prepare/Approve/Commit 三阶段、四眼审批、Ed25519 Capability、ReplayGuard、Outbox、Celery + SeaTunnel、影子表备表、质量契约分流、原子 Swap | 🟢 **代码与单测齐、链路已接通**：前半段（准备单 / 四眼审批 / 提交执行 / Outbox / Capability）此前已落地，后半段（be-16 监督与硬中断 / be-17 质量契约与分流 / be-18 原子发布与受管回滚 / `services/finalize.py` 收尾编排）已补上；本轮新增**提交时的目标端备表** `target.ensure_tables`（同请求内建影子表与错误表，走 ToolBroker 七步）。⚠️ **三件事必须如实说**：① 这条链在真基础设施上一次都没跑过（`preparation` / `execution_run` / `capability` / `outbox_message` / `quality_report` / `error_record` / `execution_metric` 全 0 行，ECS 未起 demo profile，镜像拉取未完成）；② **分流未接线**——路由 SQL 三个函数都在、都有用例，但 `render_route_selects` **零生产调用方**，渲染器仍是单源单 sink，违规行不会被真的分出去（改法见 `项目通读/10-做到哪一步了.md` §4.1）；③ spec 只到 M2-01，be-13…be-18 均为代码先行 |
| **M4 生产化** | 根因诊断、证据账本签名检查点、Benchmark L0/L1/L2、监控告警与指标折线、备份、CI/CD、交付文档 | ⬜（运行监督硬中断与受管回滚已提前在 M3 落地；策略管理面 `policy-rules` / `policy-versions` / `budget-defaults` 归此，需先补表列，见 specs/M2-01 §10.6。仍**零引用**的只剩 benchmark 三张表与 `login_attempt`（`evidence_ledger_block` 只有读、没有写——签名检查点仍未实现）） |

## 9. 已知坑与注意事项

- **本机（Windows 开发机）没有 Docker、没有 WSL、没有 Java**；容器操作一律在阿里云 ECS `60.205.138.167` 上执行。本地写代码、跑纯 Python 单测、并跑控制面进程（API / worker / 前端），基础设施经 SSH 隧道连过去。
- **SeaTunnel 需要 JDK**（镜像内自带），不要在 ECS 上裸装。
- **Doris all-in-one 吃 8G+ 内存**，ECS 规格未确认前用 compose profile 隔离，不要默认启动。
- LangGraph 的中断/恢复必须真正落 PG Checkpoint；禁止用进程内字典假装持久化。
- Celery 任务重投是常态（Outbox 至少一次投递），**所有任务必须幂等**。
- **前端 `Element Plus` 按需引入，避免全量打包拖慢首屏。**
- **`vite build` 报 `memory allocation of N bytes failed` 是机器内存，不是代码**：报错来自 Rust 侧（Vite 8 的 rolldown）**abort**，失败的那次分配只有几十 KB——即「到墙了」，不是「要得多」。本机 16G 物理内存 + VS Code，提交量（commit）很容易顶到上限；实测把两个跑着的 vite dev server 里多余的那个杀掉（它自己占 3.7G commit）后，`npm run build` 立刻在 1.36s 内成功。判据：任务管理器看**提交大小**而不是工作集；先关多余的 dev server 再重试，别去改构建配置。`npm run type-check`（`vue-tsc --noEmit`）与 build 是两步，前者过了不代表后者过了。

### 9.1 真机才暴露的坑（单测看不见，均已加回归用例）

- **`@shared_task` 绑的是 Celery 的默认应用**，不是 `tasks/celery_app.py` 里那个配好 broker 的实例。少一句 `celery_app.set_default()`、或 `tasks/__init__.py` 没导入 app 模块，**API 侧投递会静默走到内置的 `amqp://localhost:5672`**，症状是「点一下按钮就 500」，报错栈在 kombu 里。worker 侧因为 `-A` 已经把 current_app 设对了，永远看不出问题 → 回归用例：`tests/test_celery_app.py`。
- **worker 里 `sys.path` 会被 Celery 撤掉**：`cwd_in_path()` 加载完 app 就把 cwd 从 `sys.path` 里 `remove` 掉，于是任务体的惰性导入（`from services import ...`）报 `ModuleNotFoundError`。修法是**无条件 remove-then-insert**（`if not in sys.path` 保护会失效：celery 插入的就是同一个字符串），并在 `celeryd_init` 里再补一次 → `tasks/__init__.py`。
- **任务里禁止 `asyncio.run`**：每次新建并关闭循环，而 `core.db.engine` / `core.redis` 是模块级单例，第 2 个任务起必失败（`Event loop is closed`，或伪装成 `AttributeError: 'NoneType' object has no attribute 'send'`，栈指向 `session.execute`）。一律用 `tasks/async_runner.run_coro` → 有用例守着这条。
- **工具执行器要在启动钩子里注册**（worker 的 `celeryd_init` + API 的 lifespan 各一次）。漏了就是运行期 `UnauthorizedToolError`，而单测因为直接 import 模块永远发现不了。
- **`ToolBroker` 的 `detail` 白名单会静默丢弃未知键**（只留一行 warning）。少登记一个键 = 前端看到的结论被悄悄改写（例如 `state` 没登记，连通性明明成功却显示 failed）→ 有用例逐个工具核白名单。
- **aiomysql 的 `DictCursor` 键名大小写敏感**：MySQL 8 回的是大写 `TABLE_NAME`，写 `row["table_name"]` 直接 `KeyError`。凡从结果集取值一律走 `clients/datasource_probe.py::_pick`，它取不到会**抛错**而不是回 `None`（回 `None` 会被 `str()` 变成表名 `"None"`，档案状态却是 ready——错得安静最危险）。
- **归因不要冤枉目标库**：`_classify` 只把驱动异常译成字典 §5.7 的三个码；`KeyError`/`TypeError`/`AttributeError`/`IndexError` 是我们自己的代码缺陷，归 `INTERNAL_ERROR` 并留栈——否则真因在解析代码里，排查方向却被「连接失败」带去查网络与凭据。
- **psycopg 异步拒绝 Windows 的 `ProactorEventLoop`**：报 `Psycopg cannot use the 'ProactorEventLoop' to run in async mode`，症状是**应用启动即退出**（很容易误判成「PG 连不上」）。根因是循环类型，而且**在 app 模块里设 policy 无效**——uvicorn 0.5x 先建循环、后 import app，policy 来得太晚；它用的是 loop **factory** 且没有 CLI 开关。做法：循环类型的唯一决定点是 `core/eventloop.py`，本地起 API 走 `dev_api.py`（`asyncio.run(server.serve(), loop_factory=...)`），worker 侧 `tasks/async_runner.run_coro` 也用它 → 不要在本机用 `uvicorn main:app`，也不要加 `--reload`（会退回 Proactor）。
- **`psycopg[binary]` 要显式声明**：只有纯 Python 的 `psycopg`（作为传递依赖）时装包能过、import 也能过，但连库时报 `no pq wrapper available`。已在 `backend/pyproject.toml` 里用 `uv add` 钉上。
- **Vite 只监听 IPv6 localhost**：`http://127.0.0.1:5173` 连不上，用 `http://localhost:5173`。反过来，**vite 的代理目标要写 `127.0.0.1:8000`**（后端只监听 IPv4），写 `localhost` 时 Node 可能先试 `::1`，症状是「页面能开、接口全失败」。
- **前端 `.env.development` 默认 `VITE_USE_MOCK=true`**：`src/api/mock` 用**浏览器端的 axios 适配器**接管全部 `/api/v1`，于是页面每个功能都像好的，**数据一条都不是后端的**——而 `curl` 打 dev server 看不出这层（mock 不在服务端）。真连后端：`VITE_USE_MOCK=false npm run dev -- --port 5175`。为了不再靠记配置，加了 `components/common/MockModeBadge.vue` 角标（判据是编译期常量 `import.meta.env.VITE_USE_MOCK`，生产构建里条件恒假）：实测生产 `dist` 里 `grep -rl "installMockAdapter"` 与 `grep -rl "DEV MOCK"` **均 0 命中**，`--mode development` 构建则两处都命中——**假数据进不了生产包**这件事要能这样验，而不是靠读代码相信。
- **隧道断了 ≠ 端口关着**：表现为「能连上但不通」，症状是登录超时。先看隧道进程，再看 ECS 容器。
- **重启隧道后的第一个请求可能 503**：API/worker 连接池里握着一批指向旧隧道的死连接，`pool_pre_ping` 要等它们被逐个淘汰。重试一次即恢复——这是自愈，不是故障，别去改 `pool_recycle`。
- **Redis 客户端必须显式开健康检查与重试**（`core/redis.py` 的 `health_check_interval=30` + `Retry`）：redis-py 默认不做 pre-ping，而客户端是 `lru_cache` 单例、worker 又复用进程级事件循环，于是隧道抖动一次之后**那条死连接永远不会被换掉**——症状是「worker 从此不干活，重启才好」，日志只有一句 `Error 22 while writing to socket`，看不出是连接池的问题。SQLAlchemy 侧靠 `pool_pre_ping` 自愈，两者是同一个坑的两个面 → 回归用例：`tests/test_core_clients.py`（断言的是**配置本身**，因为没有真 Redis 也能守）。
- **LangGraph 的 state 有两个静默通道**：入口——`StateGraph(EtlWorkflowState)` 按 TypedDict 建 channel，**service 注入的键若没在 schema 里声明就整体消失**（不是报错）：症状是节点读到空串，报出来却是业务语义的错（`需求描述不能为空`），排查方向被带到「前端没传」上；出口——节点回写白名单（`NODE_OUTPUTS`）外的键会覆盖别人。出口已有装配期 + 运行期两道守卫（`workflow/graph.py` 的 `_guarded_node`），入口靠「样例覆盖每个声明键」的用例守着（`tests/test_workflow_state.py`）。**加一个 state 键 = 三处一起改**：`state.py` 的声明、`NODE_OUTPUTS`（谁写它）、`_initial_state`（谁注入它）。
- **按时间过滤的用例两端都要夹住**：fixture 里走服务代码落的事件用的是**真实当前时间**（`utcnow()`），而用例拿硬编码的 `_T0` 做**下界**过滤——于是「这条事件算不算命中」取决于**跑测试的钟点**。`tests/test_governance.py` 的两条用例因此在 `2026-10-16 16:01 CST`（真实时钟越过 `_T0 + 1min` 那一刻）集体变红，而此前一直是绿的：**这不是产品缺陷，是用例在某个时刻自己引爆**。凡涉及时间窗的用例，要么两端都夹，要么让种子与过滤**共用同一个基准时刻**，别把「真实现在」和「硬编码过去」混在一个断言里。
- **service 不 `commit` = 「图跑完了、库里是空的」**：`core/db.get_session` 只借连接不提交，事务边界归 service。漏掉的症状极具误导性——LangGraph 的 checkpoint **自己提交**，所以 `checkpoints` 表里记录齐全、`pipeline` 表一行没有，下一个请求报「生成会话不存在」，看起来像状态丢了。凡 `services/pipeline/*` 里的写入口（发起 / 答题 / 中止 / 自动修复 / 冻结）末尾都必须有一行 `await session.commit()`，位置在**所有写入之后、构造响应之前**。守它的是 `tests/test_service_commit_boundary.py`：AST 扫 `services/` 全部公开异步函数，「会不会写」按调用链判断（写语句多半藏在 repository 里），会写就必须有 `commit()`；另有一条元用例反过来喂它一个「忘了提交」的样例，防止守卫悄悄退化成永远通过。
- **`assert_no_unresolved_tokens` 是「执行前」的闸，不是「冻结前」的**：HOCON 制品本来就该带着 `{{secret:N:field}}` 令牌（令牌正是「口令不进制品」的手段），拿执行前那道闸去卡冻结，会把**每一份合法制品**都拦下，报错文案还是「仍有未替换的密钥令牌」——读起来像渲染器漏了替换。冻结要的是 `secret_token_problems`（令牌到时候解得开吗：数据源还在、还有凭据）。两道闸判两件事，见 `engines/hocon/types.py`。
- **同一份语义两处组装，迟早形状不一致，而且报在别处**：`hocon_artifact.validation_result` 由「渲染段 ⊕ 解析/门禁段」拼成。一度由渲染节点、门禁节点、落库路径各拼一份，结果门禁节点多套了一层信封（`{"gate": {...}}`），于是 `get_gate` 把信封当 `GateResult` 解析、报「`evaluated_at`/`conclusion` 缺失」——而门禁**其实跑了**。同理 `generation_context` 手拼 dict 时多了一个 `plan_issue_count`，症状是「生成成功、答题 422」：**写的一侧多一个键，报在读的一侧**。规矩：这类 JSON 列的形状**只有一份定义**（`gating.render_meta` / `GateOutcome.artifact_meta` / `clients.llm.base.GenerationContext`），消费方的 DTO 对未知键一律 `extra="ignore"`。形状由 `tests/test_artifact_meta_shape.py` 钉住（`gate` 这一键**直接是** `GateResult`，不是它的信封）。
- **同一个版本串撞了三堵墙，症状各不相同**：`PARSER_VERSION = "pyhocon+elt-render-1"`（20 字符）先后撞上 ① `hocon_artifact.parser_version` `varchar(16)`（落库 SQLSTATE 22001，表现为「图跑完、门禁判完、请求 500」）② `ManifestHocon.parser_version` 限 16（**冻结时**报校验错）③ `HoconArtifactRead.parser_version` 限 16（拉制品 422，而库里那行好好存着）。共同点是**写进去的东西读不出来**。守它的是两条用例：`tests/test_version_columns_fit.py`（引擎串 ≤ 列宽）与 `tests/test_dto_version_widths.py`（扫全部 DTO 的 `*_version` 字段，不得窄于实际取值 + `generation_context` 的读写往返）。
- **PDP 必须走 `services.policy` 的入口**：`engines.policy.decide()` 不传 `defaults` 就落到 `_ZERO_DEFAULTS`（上限 0），于是**每一条**预算判断都超限——症状是门禁报「超过 PDP 批复的上限 0」，看起来像策略表没配，实际是调用方少传了预算来源。门禁一律用 `services.policy.decide_for_project`（它注入 `budget_defaults()` 与策略版本、把告警落账本）。
- **两套 `SensitiveKind` 词表**：脱敏侧是 `name/phone/email/id_card/bank_card/address/credential/free_text/none`，策略侧是 `id_card/phone/email/address/token/bank_card/name/other`。名字不同、语义有交集，`credential` 对应 `token`（**在 P0 白名单里**），翻错了等于**放走一条 P0**。转换只在 `engines/policy/models.py::to_policy_kind`，`tests/test_policy_kind_vocabulary.py` 守着全覆盖（新增一种脱敏类别而忘了映射，用例会红）。
- **alembic autogenerate 会想删 LangGraph 的 checkpoint 表**：`checkpoints` / `checkpoint_blobs` / `checkpoint_writes` / `checkpoint_migrations` 是 LangGraph 自己的 `setup()` 建的，不在我们的 metadata 里，于是 autogenerate 把它们当成「多余的孤儿表」提议 `DROP TABLE`。生成的 migration **必须手工删掉这几段 DROP**，只留本次真正要改的语句——照单执行等于每次迁移都清空所有生成会话的断点。
- **`pytest` 的 `addopts` 里已经有 `-q`**：命令行再加一个 `-q` 就是 `-qq`，**汇总行（`N passed`）会消失**，只剩进度点。看总数用不带 `-q` 的 `uv run pytest`，或 `--collect-only -q | awk` 求和。
- **`ExecutionContext(actor_id=None)` 会让带 Capability 的工具永远执行不了**：`harness/broker.py::_expected_binding` 在 BINDING 阶段就拒空 actor，而**假 broker 的单测根本不走那七步**——于是「人工取消」与「监督硬中断」两条 `seatunnel.stop` 路径同时是死的，用例全绿。它还有第二层伪装：`execution_run` 零行、SeaTunnel 没起，所以「命令投出去了、作业没停」永远等不到人发现。**同一个坑修了一次还会漏第二次**：`_stop` 修好之后 `_submit` 仍然传 `actor_id=None`（报 `Capability 执行缺少操作主体`），提交同样一步都走不了——修这类缺陷必须**横向扫同形状的调用点**，不能只修报错那一处。**主体没有统一答案，只有同一条判据**（`ctx.actor_id` == 令牌里签的 `subject_id`）：`seatunnel.submit` 取运行行 `triggered_by`（令牌本来就签给发起人，broker 之后照样比对）；`seatunnel.stop` 取 Outbox 载荷的 `subject_id`（**取消人 ≠ 发起人**，读 `triggered_by` 会把归因永久记错）；`target.rollback_cleanup` 取账本 `rollback.started` 的载荷、兜底 `triggered_by`（`services/publish.py::_rollback_actor`）→ 回归用例：`tests/test_run_commit.py`、`tests/test_outbox.py`、`tests/test_supervision.py`、`tests/test_finalize.py`。
- **「配着、绿着、永不生效」的哑闸门比缺功能更危险**：be-16 的放大比维度（写字节 / 读字节）要 `read_bytes` 做分母，而 Zeta 报的计数器**键名没有标准**——`clients/seatunnel/client.py::metrics` 依次试 `readBytes` / `readBytesTotal` / `sourceReceivedBytes` / 键名含 `readbytes`，**一个都不中就落 0**，`services/supervision._sample` 再把 0 翻成 `None`（**必须翻**：`written_bytes / 0` 会算出「读进来零」的无穷大，把一次正常运行误报成失控）。结果是这一维**长期判 `unknown`、阈值永远不会开火**，而它唯一拦得住的是「笛卡尔积式 join 写错」（不涨行数、不涨拒绝率，只涨写出的字节量）。**判据**：一条阈值维度的**来源字段**拿不到时，要么如实判 `unknown` 并**留下唯一能校准它的线索**（这里是一行 `seatunnel_read_bytes_unmapped` 的 debug 日志，**只记键名、不记值**——值里可能有配置片段），要么就当缺陷修，**不许用 0 顶替**（0 把「不知道」伪装成「正常」）→ 回归用例：`tests/test_seatunnel_metrics.py`、`tests/test_supervision.py`。
- **worker 侧的 `ToolBroker` 少传 `verifier` = 整条执行链在 worker 里一步都走不了**：`harness/broker.py` 第 ③ 步（验签）在 `_verifier is None` 时**直接拒绝**，文案却是「**本进程未配置 Capability 校验公钥**」——读起来像部署漏配，真因是这个进程**永远执行不了任何带令牌的工具**（worker 里要执行的正是 `seatunnel.submit` / `seatunnel.stop` / `target.publish_swap` / `target.rollback_cleanup` 四个）。单测全绿，是因为它们用的是**绕过七步序的假 broker**。修法：公钥路径 = 私钥路径换后缀，这条推导**只留一处**（`harness/capability.py::verifier_from_settings()`，所有进程都调它）；守它的用例不能只断言属性非 `None`，要**拿同一把密钥签一张令牌、验签必须过得去**（断言「配上了」≠ 断言「能用」）。
- **进程级单例 vs 需要上下文的端口：注册形状错了，接口在物理上无法实现**：`engines/quality/ports.py::ErrorTableReader` 的方法全是「表名进、计数出」，而**影子表名会跨运行复用、光有表名定位不到主机与库**（租户/数据源只在那行 `execution_run` 上）。所以注册的必须是**工厂** `(session, run) -> reader`（`clients/target/errors_table.py::build_error_table_reader`），不是进程级实例。配套的一条：目标端读不到时**抛错，不许降级成 0**——0 是合法的报告值，一份「看着干净」的报告会被下游直接拿去换表。
- **本地 worker 必须同时订阅 `etl.execute`**：`tasks.execution.*`（提交 / 停止 / 收尾）路由到该队列，只订阅 `etl.default` 的 worker **永远不消费这几条消息，而且日志里一行都不打**（不报错，只是沉默）。`docker-compose.yml:220` 是对的（`-Q etl.default,etl.execute`），本地手起的命令别抄漏（见 §7）。
- **Celery 的 `crontab` 没有秒字段**：两条 beat 环周期都是浮点、相位跟着 beat 启动时刻走，**同周期 + 同时启动 = 永久锁步**（同一秒里对每条活跃运行各发一次数据面请求）。可用的错开手段只有**不同周期**（30 s 监督 vs 40 s 收尾，最小公倍数 120 s），不是 `crontab(second=...)`。
- **账本载荷的键名撞上脱敏正则 = 整条写入被拒**：`core/logging.py::_SENSITIVE_KEY_RE` 含 `pass(word|wd)?`，于是载荷里一个叫 `passed` 的键让**每一份质量报告都写不出账本**（`SensitivePayloadError`，不是被脱敏成 `***`）。修法是**只改账本里的键名**（`conclusion`），DTO 的对外字段 `passed` 不动——脱敏按键名匹配，命名里带 `pass`/`token`/`secret` 的**非敏感**字段都会撞上。
- **`expire_on_commit=False` 的会话在别人提交之后会「读过时值」**：`core/db.py` 为了序列化方便关掉了提交后过期，于是下游服务 `commit()` 之后，内存里的 ORM 对象**还是进来时的旧值**。症状是「明明发布了，回报的 status 还是 `running`」——**库里是对的，回报是错的**。收尾回报状态一律**从库里重读**（`services/finalize.py::_result` 用 `session.get(ExecutionRun, run.id, populate_existing=True)`）。
- **「单测永远不会发现没人调我」**：`finalize_report`（出质量报告）与 `publish_run`（换表）都写完了、用例都绿，但两份设计**各自把收尾划给了对方**，于是两个函数的**调用方数量都是 0**——作业跑完永远不会出报告，报告合格也永远不会换表。用例天然只测「我这个函数对不对」，不测「有没有人调它」。补的是 `services/finalize.py`（**不拥有任何算法**，只做分派与顺序），由 beat 每 40 s 的 `tasks.execution.scan_finished_runs` 驱动。**为什么不走 Outbox**：扫描每轮都会重新发现同一个待办，丢一次投递不会永久丢失；走 Outbox 就得把「谁发现作业跑完了」也变成需要持久化的状态。收尾侧的四条纪律：只推进 `running`/`interrupted`；**中断的运行永不发布**（两道独立判据——硬中断意味着影子表里是半份数据）；查不到作业**不动状态**只记 `orphaned`（可能是别人正在取消，一律写 failed 会让「谁停的」永久失去答案）；失败**只留 `classify_failure` 的分类**不搬原文（失败文本常带整段配置与连接串）→ 回归用例：`tests/test_finalize.py`。
- **共享全局状态的测试夹具必须自己隔离**：`tests/test_harness_broker.py` 的假执行器夹具里写着 `if registered_executors(): return`（别人注册过就整体跳过），新写的 `test_finalize.py` 先注册了**真**执行器 → 本模块的 `seatunnel.submit` 变成「未注册」，四条用例红，**单跑绿、同跑红**，报错还指向本模块毫不相干的地方。夹具要**换成本模块私有的空表、跑完还原**（`monkeypatch.setattr(harness.tools, "_EXECUTORS", {})`），不能指望「别人没动过这个全局」。
- **工具执行器与端口实现在启动钩子里注册**（worker 的 `celeryd_init` + API 的 lifespan 各一次）：漏了就是运行期才报错/才 500，单测因为直接 import 模块**永远发现不了**。启动日志里能自查：`quality_error_table_reader_registered impl=build_error_table_reader`——**改完这两处要真的重启一次 API 与 worker 读日志**，而不是相信代码看起来对。
- **改了代码不重启 = 你测的是旧进程**：本轮实测，`/openapi.json` 里的 `ToolName` 还缺 `target.ensure_tables`，而代码里早已加上；api 日志的启动时间比文件 mtime 早一小时。症状是「明明注册了，请求仍 500 / 枚举值对不上」。判据：**比对启动日志时间与代码 mtime**，并用 `/openapi.json` 核对新枚举/新路由是否出现——不要用「我改过文件」当依据。
- **Jdbc sink 的 `generate_sink_sql` 必须是 `true`**：写成 `false` 且没有 `query` 时，SeaTunnel 作业提交后会在 writer 初始化阶段失败（sink 没有可执行的 INSERT）。曾经写 `false` 是把「不想 upsert」误当成了这个键的语义——它只决定 INSERT 由谁拼，写语义由影子表 + 原子 Swap 负责。配套的一条：**源端表形态必须同时输出 `database`**，因为内层 SELECT 用的是 `` `db`.`tbl` ``，而 Jdbc source 按 `TablePath.of(database, table)` 注册上游表；少一个键的报错是「表不存在」，极像库名写错 → `engines/hocon/render.py`（两处都有注释）。**这两条都未经真 SeaTunnel 验证**，改完必须用真机提交一次看 Zeta 状态。
- **「两个模块都写完、各自全绿」≠「接上了」**：`render_route_selects`（路由 SQL）有完整实现、有用例，但**生产代码里一个调用方都没有**——渲染器仍是单源单 sink，所以违规行不会真的分流，质量报告会读到 0 条、显示「很干净」。这与 `finalize_report` / `publish_run` 是同一类缺陷。**交付前对每个新的「终点函数」grep 一遍调用方**，单测不会告诉你「没人调我」。修法（运行期令牌 `{{runtime:run_id}}` + tagged/valid/rejected + 双 sink + `_assert_writes_covered` 放行错误表）写在 `项目通读/10-做到哪一步了.md` §4.1。
- **脱敏断言的正则与数据面掩码不是同一个形状**（未修，登记）：起草器用 `^[a-z_]+:[0-9a-f]{12}$`（探查期 Python 掩码 `phone:3f2a1b4c9d0e`），而 `engines/hocon/render.py::_masking_expr` 的 `mask_middle` 产出 `13****01`。分流接线之后，含敏感列的流水线会 **100% 命中 `E_SENSITIVE_PLAINTEXT`**（零容忍 → 硬中断）。现在没炸只是因为分流没接线——**两个缺陷互相掩盖**。修法：掩码形状收成 `engines/masking` 里的一份定义，渲染器与规则都从它取；更根本的是按 be-17 第 331 行把该码交给 P0 门禁（制品属性），字段规则只作补充。
- **ECS 只有 `docker.1panel.live` 镜像代理，且磁盘只剩约 9.9 G**：Doris 能拉到的 tag 是 `2.1.9-all`，Doris + SeaTunnel 解压后要占 6~7 G，`demo` profile 不一定起得来。数据面起不来时用 `scripts/demo_run.py --target mysql`（目标端是 `dw_demo.orders_wide`，与 Doris 的表名/列形状一致，见 `deploy/mysql/init/02-target-schema.sql`）。**不要把「镜像没拉下来」误判成代码缺陷**。
- **本机进程可能被系统整体杀掉（exit 107 / 背景任务 exit 4）**：症状是脚本零输出、随后 API/worker/隧道一起退出，连 `echo alive` 都返回 107。这不是代码错误，先看内存/提交大小与残留进程（CLAUDE.md §9 的 Vite OOM 是同一类），恢复后重启隧道与两个进程再跑——**不要在这个状态下改业务代码**。

## 10. 交付前自检

- [ ] `pytest` 全绿，且协议类用例（自批拦截、Capability 重放、指纹失效、超预算硬中断）有集成测试覆盖
- [ ] **新增的每个「终点函数」都 grep 过调用方**（绿着的单测不会告诉你「没人调我」）
- [ ] **改过启动钩子 / 注册点之后，真的重启过 API 与 worker 并读过启动日志**（漏挂钩子只在运行期暴露）
- [ ] 全仓库 grep 不到任何明文密钥、密码、连接串
- [ ] `P0/P1 拦截率 = 100%`、`未授权/重放成功率 = 0` 由自动化测试证明
- [ ] 证据账本验链脚本能从零独立验证全链
- [ ] 演示场景一（MySQL→Doris 宽表同步）可从空环境一键跑通
- [ ] 演示场景二（脏数据超限硬中断 + 受管回滚）可一键复现
