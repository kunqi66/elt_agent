# ELT-Agent 数据集成平台

面向企业异构数据集成场景的**生产级 ETL-Agent 控制面平台**。用户以自然语言描述数据集成需求，
系统在受管元数据与安全策略约束下生成结构化 EtlPlan 与 SeaTunnel 配置，并经确定性门禁、
不可变版本、四眼审批、单次能力令牌与受管执行，完成从设计到投产的闭环。

**控制面与数据面分离**：本平台负责需求理解、方案生成、策略治理、审批与运行监督；
Apache SeaTunnel 负责数据搬运，控制面不直接传输海量业务数据。

## 核心能力

| 能力域 | 内容 |
|---|---|
| 基础治理 | 用户认证鉴权、多租户隔离、项目成员与职责槽、Vault 受管凭据 |
| 数据源与元数据 | 异构数据库与文件/对象存储接入、只读元数据探查、近似统计、脱敏样本 |
| 智能生成 | 自然语言解析、LangGraph 中断与 Checkpoint 恢复、EtlPlan 与 HOCON 生成、有限自动修复 |
| Harness 安全内核 | PDP（P0–P3 风险）、`prepare → approve → commit` 三阶段、四眼审批、Ed25519 单次 Capability、防重放、事务 Outbox、证据账本 |
| 数据面执行 | Celery 异步调度、SeaTunnel Zeta 执行、影子表与原子 Swap、质量契约与错误分流、运行监督、受管回滚 |
| 评测 | L0 静态注入 / L1 模拟故障 / L2 真实链路分层 Benchmark 与安全进化闭环 |

## 技术栈

FastAPI · LangGraph · Celery · PostgreSQL 16 · Redis 7 · Apache SeaTunnel · Vault · DeepSeek ·
Vue 3 + TypeScript + Element Plus · Docker Compose

详见 [CLAUDE.md](CLAUDE.md)。

## 目录结构

```
backend/          FastAPI 控制面 + Celery Worker/Beat（api / services / engines / clients / harness / workflow / tasks）
frontend/         Vue 3 控制台
deploy/           Nginx、各库初始化脚本、监控与备份配置、本地开发用的 SSH 隧道脚本
secrets/          Ed25519 私钥（不进仓库）
vibecoding_doc/   需求与设计文档（PRD / 概要设计总纲 / 核心数据字典 / 模块概要设计 / specs）
docker-compose.yml
```

## 快速开始

> 运行环境为 Linux 服务器（阿里云 ECS）。开发机（Windows）只写代码与跑纯 Python 单测，
> **不要在 Windows 上尝试 docker compose**。

```bash
# 1) 配置环境变量（.env 是唯一事实来源，不要手写 *_URL）
cp .env.example .env && vi .env

# 2) 生成签名密钥
cd backend
uv sync
uv run python -m core.keys generate --kind capability --out ../secrets/capability_signing_key.pem
uv run python -m core.keys generate --kind ledger     --out ../secrets/ledger_signing_key.pem
cd ..

# 3) 起控制面（PG / Redis / MinIO / Vault / api / worker / beat / nginx）
#    建表由 compose 里的 migrate 一次性服务完成，api 会等它成功后再启动——
#    不需要（也不能）在宿主机上跑 alembic：宿主机没有 PG 客户端，也没有 uv。
docker compose up -d --build

# 4) 租户开张：建出租户 / 首批五个用户 / 首个项目 / 初始职责槽
#    没有这一步，库里一个用户都没有，登录接口无从谈起。
#    口令从环境变量取（不进 shell 历史）；不传则随机生成并只打印一次。
ELT_BOOTSTRAP_PASSWORD='<自己定>' docker compose run --rm api \
    python scripts/bootstrap_tenant.py \
    --tenant-code demo --tenant-name 演示租户 \
    --project-code sales_wide --project-name 销售宽表同步

# 5) 需要演示数据面时再起（MySQL / Doris / SeaTunnel，Doris 吃 8G+ 内存）
docker compose --profile demo up -d

# 6) 验证
curl http://localhost/healthz          # {"status":"ok",...}
curl http://localhost/readyz           # 依赖全通才 200；任一不可用 3 秒内回 503
curl -s -X POST http://localhost/api/v1/auth/login \
     -H 'Content-Type: application/json' \
     -d '{"tenant_code":"demo","username":"maker1","password":"<上面那个口令>"}'
```

> `readyz` 回 503 时看 `checks` 字段：`fail` 是连不上，`timeout` 是 3 秒内没响应。
> 两者都表示依赖不可用，但排查方向不同（前者多半是地址/凭据，后者多半是网络或负载）。

## 本地开发（控制面在本机，基础设施在 ECS）

> 上一节是**部署**路径（全部在 ECS 上用 compose 跑起来）。开发与演示走下面这条：
> PG / Redis / Vault / 演示 MySQL 继续以容器形式留在 ECS，**API 与 worker 在本机跑**，
> 改一行代码重启进程即可，不必重建镜像。两者读同一份 `.env`，差别只在地址是不是隧道端口。

```bash
# 0) 前置：ECS 上的基础设施已起（在 ECS 上执行）
docker compose up -d postgres redis vault

# 1) 隧道：把 ECS 的 127.0.0.1 端口映射到本机同号端口
#    口令只从环境变量进（不写文件、不进命令行参数），这个进程要一直开着
ELT_ECS_PASSWORD='<ECS 登录口令>' uv run --with paramiko python deploy/tunnel.py 5432 6380 8200 3307
#    看到 READY 5432 6380 8200 3307 才算好

# 2) 本机 .env 按隧道端口填：Redis 是 6380、演示 MySQL 是 3307（不是 6379 / 3306）
cp .env.example .env    # 改 POSTGRES_HOST / REDIS_PORT / VAULT_ADDR / DEMO_MYSQL_*

# 3) 控制面（两个终端）
cd backend && uv run uvicorn main:app --host 127.0.0.1 --port 8000
cd backend && uv run celery -A tasks.celery_app worker -P solo -l info -Q etl.default

# 4) 前端控制台（/api 由 vite 代理到 127.0.0.1:8000）
cd frontend && npm install && npm run dev
```

首次还需要租户开张——库里没有用户时登录无从谈起：

```bash
cd backend && ELT_BOOTSTRAP_PASSWORD='<自己定>' uv run python scripts/bootstrap_tenant.py \
    --tenant-code demo --project-code sales_wide
```

三条验证都要过：

```bash
curl http://127.0.0.1:8000/healthz
curl http://127.0.0.1:8000/readyz     # 依赖全通才 200
curl -s -X POST http://127.0.0.1:8000/api/v1/auth/login -H 'Content-Type: application/json' \
     -d '{"tenant_code":"demo","username":"maker1","password":"<开张时的口令>"}'
```

本机（Windows）已知坑：

- **Vite 只监听 IPv6 localhost**：`http://127.0.0.1:5173` 连不上，用 `http://localhost:5173`。
- 控制台是 GBK：跑 Python 脚本加 `PYTHONIOENCODING=utf-8`；本机没有系统 `python`，一律 `cd backend && uv run python`。
- **Celery worker 必须 `-P solo`**（Windows 上默认的 prefork 池不可用）。
- 隧道断掉的表现是「端口能连上但不通」，症状是登录超时而不是连接被拒——先确认隧道进程还在。

## 文档地图

| 文档 | 作用 |
|---|---|
| [需求分析.md](需求分析.md) | 题面（选题 2.8） |
| [vibecoding_doc/PRD.md](vibecoding_doc/PRD.md) | **需求 SSOT**：16 章，含成功指标、状态机、实体清单、验收标准 |
| [vibecoding_doc/需求对齐记录.md](vibecoding_doc/需求对齐记录.md) | 所有已拍板决策与理由（含 P0–P3 判定矩阵） |
| [vibecoding_doc/概要设计总纲.md](vibecoding_doc/概要设计总纲.md) | 架构、时序、Harness 模块、安全边界、部署拓扑 |
| [vibecoding_doc/核心数据字典.md](vibecoding_doc/核心数据字典.md) | 26 个实体的字段、约束与关系 |
| [CLAUDE.md](CLAUDE.md) | 分层铁律、安全红线、代码规范、进度表 |

## 里程碑

- **M1 环境与骨架**：环境、分层骨架、认证、多租户、数据源管理、元数据探查与脱敏样本
- **M2 生成链路**：LangGraph 澄清/中断/恢复、EtlPlan 与 HOCON 生成、确定性门禁、不可变版本
- **M3 治理链路**：PDP、三阶段协议、四眼审批、Capability、Outbox、Celery + SeaTunnel、质量分流、原子 Swap
- **M4 生产化**：运行监督与硬中断、根因诊断、受管回滚、证据账本检查点、Benchmark、监控告警备份 CI/CD

## 安全约定

- 所有副作用经 Harness ToolBroker 唯一出口，禁止绕过
- Secret 只经 Vault 引用与物化，日志/响应/账本/模型上下文全链路无明文
- PDP 为纯规则引擎，不引入 LLM（P0/P1 拦截率必须 100%）
- 不可变制品由 ORM 层强制禁止 UPDATE / DELETE
- 外网仅暴露 Nginx；PG / Redis / MinIO / Vault 端口只绑 `127.0.0.1`，ECS 安全组需同步收紧
