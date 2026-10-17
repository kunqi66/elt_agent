# be-22 部署、备份与 CI/CD

> 上游：[概要设计总纲.md](../概要设计总纲.md) §9 部署拓扑 · §2 进程与模块拓扑 ｜ [PRD.md](../PRD.md) §10 非功能需求 · §16 交付物 · §13 验收标准
> 决策：[需求对齐记录.md](../需求对齐记录.md) §一 交付与环境 · §二.7 指标刷新/告警/保留 · §六 待办
> 代码位置：`docker-compose.yml` `backend/Dockerfile` `backend/.dockerignore` `.env.example` `deploy/` ｜ 里程碑：M1（骨架）/ M4（生产化） ｜ 依赖：全部

## 1. 职责与边界

- **做什么**
  1. 定义单机 Docker Compose 拓扑：三个 profile（默认 / `demo` / `obs`）、网络划分、卷、`healthcheck`、重启策略、资源 limit 与日志轮转。
  2. 用**一份镜像三个 command** 运行 `api` / `worker` / `beat`，规定镜像分层与构建缓存策略、非 root 用户、时区。
  3. 治理 Secret 与配置：`.env` 不进仓库、Ed25519 私钥挂载到 `/run/secrets:ro`、Vault dev↔prod 差异、**生产环境 Settings 自检作为部署门禁**。
  4. 初始化各依赖：PostgreSQL 扩展与审计只读角色、MySQL 演示数据、MinIO 桶、Vault KV v2 + AppRole、Alembic 迁移的执行时机。
  5. 备份与恢复：备份对象清单、脚本、调度、加密与异地、恢复演练（RTO/RPO）、备份失败告警接入 be-21。
  6. CI/CD 流水线（`lint → type → test → build → push ACR → deploy ECS → migrate → smoke → Benchmark 门禁`）、镜像 tag 策略、回滚步骤、密钥注入方式；上线与回滚 SOP，以及「交付前自检」的**可执行命令清单**（落地 CLAUDE.md §10）。
- **不做什么**
  - **不引入编排平台**：不用 K8s / ACK / SAE / Nomad。理由已冻结（`需求对齐记录 §一`）：组件多，K8s 运维成本会挤占工期。
  - **不做多机与高可用**：单机 compose，无跨主机调度、无集群共识；`beat` 单实例（依赖 Outbox 的 `FOR UPDATE SKIP LOCKED` 天然抗重复，但 beat 仍是单点，见 §10）。
  - **不申请域名与 HTTPS**：暂用 `IP:port`，不备案（已冻结）；因此 TLS 终止策略在 §6 只写「预留」，不建证书。
  - **不在仓库保存任何明文 Secret**：`.env`、`secrets/`、`*.pem` 已被 `.gitignore` 排除；`.env.example` 只放键名与占位。
  - **不在流水线里用明文变量注入密钥**：ACR 密码与 SSH 私钥走云效凭据/变量组（日志自动掩码），运行期密钥留在 ECS 上。
  - **不让 `demo` / `obs` 进默认启动集**：Doris 吃 8G+ 内存，`demo` 必须显式 `--profile demo`。

## 2. 模块结构

```
elt_agent/
├── docker-compose.yml            # （已有）单机编排事实来源：默认 + demo + obs 三 profile
├── docker-compose.prod.yml       # （待建）生产覆盖层：去掉 build、镜像改 ACR + tag、资源与日志策略收紧
├── .env.example                  # （已有）键名与占位的 SSOT；.env 由运维在 ECS 上维护（0600）
├── .gitignore                    # （已有）排除 .env / secrets/ / *.pem / node_modules / dist
├── README.md                     # （已有）快速开始
├── secrets/                      # （已有目录）Ed25519 私钥，只读挂载；不进仓库；README.md 说明生成与轮转
├── backend/
│   ├── Dockerfile                # （已有）builder + runtime 两阶段；非 root；TZ=Asia/Shanghai
│   └── .dockerignore             # （已有）排除 .venv / tests / .env / secrets
└── deploy/
    ├── nginx/nginx.conf          # （已有）唯一对外入口：静态前端 + /api 反代 + 限流
    ├── postgres/init/01-init.sql # （已有）pg_trgm 扩展 + elt_auditor 只读角色
    ├── mysql/init/01-demo-schema.sql # （已有）演示源库 orders / users + 脏数据
    ├── prometheus/prometheus.yml # （已有）抓取配置（be-21 补充 job 与 rules）
    ├── grafana/dashboards/       # （已有挂载点，配置待建）
    ├── vault/
    │   └── init.sh               # （待建）KV v2 + policy + AppRole 初始化（幂等）
    ├── backup/
    │   ├── backup.sh             # （待建）PG / MinIO / Vault / 密钥 四类备份 + 加密 + 上 OSS
    │   ├── restore_drill.sh      # （待建）恢复演练：还原到临时目录并验链
    │   └── backup.log            # （运行时产物，不进仓库）
    └── ci/
        ├── flow.yml              # （待建）云效 Flow 流水线定义（阶段见 §6.6）
        ├── benchmark_gate.py     # （待建）Benchmark 质量门判定脚本（exit code 驱动阻断）
        └── smoke.sh              # （待建）部署后冒烟：readyz + 登录 + 关键只读接口
```

## 3. 对外接口

### 3.1 Compose profile 与服务

| Profile | 服务 | 端口绑定 | 说明 |
|---|---|---|---|
| 默认 | `postgres` | `127.0.0.1:5432` | `postgres:16-alpine`，`memory: 2g`，`pg_isready` healthcheck |
| | `redis` | `127.0.0.1:6379` | `redis:7-alpine`，`requirepass` + `appendonly` + `maxmemory 1gb` + `noeviction` |
| | `minio` / `minio-init` | `127.0.0.1:9000/9001` | 制品桶与对象存储；`mc ready local` healthcheck；`minio-init` 一次性建桶 `elt-artifacts`（`--ignore-existing`，`restart: "no"`） |
| | `vault` | `127.0.0.1:8200` | M1 dev 模式；M4 切 server + raft + AppRole（§5.4） |
| | `api` | `127.0.0.1:8000` | `uvicorn main:app`；挂载 `./secrets:/run/secrets:ro`；`memory: 1g` |
| | `worker` | — | `celery -A tasks.celery_app worker -Q etl.default,etl.execute`；`memory: 1536m` |
| | `beat` | — | `celery -A tasks.celery_app beat`；`memory: 512m` |
| | `nginx` | `${HTTP_PORT:-80}` | **唯一对外**；挂载 `nginx.conf:ro` 与 `frontend/dist:ro` |
| | `migrate` | — | （待建）一次性运行 `alembic upgrade head`，`restart: "no"`；`api`/`worker` 依赖它成功（§5.5） |
| `demo` | `mysql` `doris` `seatunnel` | `127.0.0.1:3306` / `9030,8030` / `8080` | Doris `memory: 8g` + `ulimits`（nofile 65536、memlock -1）；SeaTunnel `memory: 4g` |
| `obs` | `prometheus` `grafana` | `127.0.0.1:9090` / `127.0.0.1:3000` | M4 接入；`node-exporter` / `alertmanager` / `alert-bridge`（待建，be-21 §10）不发布端口，仅 `elt_net` 内可达 |

网络：单一自定义 bridge `elt_net`（`172.28.0.0/24`，`doris` 固定 `172.28.0.10`）。除 `nginx` 外**所有服务都不写公网绑定**；数据中间件的 `ports` 一律 `127.0.0.1:...`，其含义是「只允许本机/SSH 隧道」，而不是「安全组就不用改了」。

### 3.2 脚本入口（shell）

```bash
deploy/backup/backup.sh [--kind pg|minio|vault|secrets|all] [--dry-run]   # 备份；成功写 elt_backup_last_success_timestamp_seconds
deploy/backup/restore_drill.sh --from <备份文件|oss://路径> --to <临时目录>   # 恢复演练；不改动生产
deploy/vault/init.sh [--mode dev|prod]                                     # Vault 初始化，幂等可重复执行
deploy/ci/smoke.sh --base-url http://127.0.0.1                             # 部署后冒烟
python deploy/ci/benchmark_gate.py --suite <name:version> --timeout 900    # 质量门；退出码非 0 即阻断发布
```

约定：脚本一律 `set -euo pipefail`；读写路径由参数传入、不硬编码；凭据从**环境变量**或 `/run/secrets` 读取，绝不出现在命令行参数里（`ps` 可见）。

### 3.3 环境变量契约

`.env.example` 是键名 SSOT；`.env` 只在 ECS 上存在（`chmod 600`，`root:root`）。分段字段（`HOST/PORT/USER/PASSWORD/DB`）是事实来源，`*_URL` 一律由 `settings` 计算（be-01 §6.1），**`.env` 里禁止出现 `*_URL`**。

| 段 | 键 | 备注 |
|---|---|---|
| 运行环境 | `APP_ENV` `APP_DEBUG` `SECRET_KEY` `CORS_ORIGINS` | `APP_ENV=prod` 触发启动自检（§6.1） |
| 对外 | `HTTP_PORT` `SERVER_HOST` | `SERVER_HOST=60.205.138.167` 用于告警跳转链接 |
| PG / Redis | `POSTGRES_*` `POSTGRES_POOL_SIZE` `POSTGRES_MAX_OVERFLOW`；`REDIS_*` `REDIS_DB_CACHE/BROKER/RESULT` | 三个 Redis 逻辑库分开，避免 broker 清空影响缓存 |
| Celery/Outbox | `CELERY_WORKER_CONCURRENCY` `CELERY_TASK_TIME_LIMIT` `OUTBOX_SCAN_INTERVAL_SEC` `OUTBOX_MAX_ATTEMPTS` | — |
| MinIO / Vault | `MINIO_*` `VAULT_*` `VAULT_ROLE_ID` `VAULT_SECRET_ID` | `VAULT_ROLE_ID/SECRET_ID` 由 `deploy/vault/init.sh` 产出后写回 `.env` |
| LLM | `LLM_*` | 生产自检要求 `LLM_API_KEY` 非占位 |
| 签名密钥 | `CAPABILITY_SIGNING_KEY_PATH` `LEDGER_SIGNING_KEY_PATH` `*_KEY_ID` `*_TTL_SEC` | 路径指向 `/run/secrets/*.pem` |
| 演示数据面 / 监督预算 | `DEMO_*` `SEATUNNEL_*`；`SUP_MAX_*` `SUP_WARN_THRESHOLD_PCT` | 数据面仅 `demo` profile 用到；预算可被 Pipeline 级契约覆盖 |
| 可观测 | `GRAFANA_ADMIN_PASSWORD` `ALERT_DINGTALK_WEBHOOK` `ALERT_ENABLED`（+ be-21 新增项） | — |
| 保留 | `RUN_LOG_RETENTION_DAYS` `QUALITY_REPORT_RETENTION_DAYS` `EVIDENCE_RETENTION_DAYS` | 0 = 永久 |

## 4. 数据模型

本节不定义表（表见《核心数据字典》）。`deploy/*/init` 的唯一职责是**DBA 级准备**，不建业务表：

| 初始化对象 | 脚本 | 内容 | 重复执行语义 |
|---|---|---|---|
| PG 扩展与角色 | `deploy/postgres/init/01-init.sql`（已有） | `CREATE EXTENSION pg_trgm`；建 `elt_auditor` 只读角色；`ALTER DEFAULT PRIVILEGES` 授予后续新建表 `SELECT`；`REVOKE INSERT/UPDATE/DELETE` | 幂等（`IF NOT EXISTS` / `DO $$` 包裹） |
| PG 业务表 | Alembic（`backend/alembic/versions/`） | 全部 26 张实体表 + 支撑表 | 由 `alembic upgrade head` 保证幂等；**不由 init 脚本建表** |
| LangGraph 检查点表 | `langgraph-checkpoint-postgres` 自带建表 | Workflow State（`thread_id = pipeline_id`） | 由库的 setup 函数执行；不得自行手写等价表 |
| MySQL 演示数据 | `deploy/mysql/init/01-demo-schema.sql`（已有） | `biz_demo.orders` / `users` + 11 条订单（含 2 条脏数据）+ 5 个用户（含明文敏感字段，用于验证脱敏） | 只在数据卷为空时执行一次 |
| MinIO 桶 | `minio-init` 服务（已有） | `elt-artifacts`（`--ignore-existing`，幂等） | 幂等 |
| Vault | `deploy/vault/init.sh`（待建） | 启用 `kv-v2` 于 `kv/`；写 `etl-worker`（`kv/data/etl/*` 读）与 `etl-api`（拒绝读 values）策略；启用 approle 并建 role（`token_ttl=1h`、`secret_id_ttl=0`）；输出 `role_id`/`secret_id` | 幂等（已存在则跳过并打印现状） |

**备份对象清单**（与上表区分：上表是「初始化」，下表是「日常备份」）：

| 对象 | 方式 | 频率 | 加密 | 异地 |
|---|---|---|---|---|
| 控制面库 `elt_agent` | `pg_dump -Fc`（自定义格式） | 每日 02:00 | AES-256（`openssl enc -aes-256-cbc -pbkdf2 -iter 200000`，口令来自 `/run/secrets/backup_key`） | 阿里云 OSS |
| MinIO 制品 | `mc mirror --overwrite local/elt-artifacts <tmp>` → 打包 | 每日 | 同上 | 同上 |
| Vault 数据 | `vault operator raft snapshot save`（仅 server 模式） | 每日 | 同上 | 同上 |
| 签名密钥 / `.env` | `tar secrets/`（`capability` + `ledger` 私钥）+ `.env` 加密打包 | 密钥或配置变更时 | 同上 | 同上 |
| 证据账本 | **包含在 PG 全量里**（`evidence_event` / `evidence_ledger_block` 永久保留） | 随 PG | 同上 | 同上 |
| 评测数据集 / 看板 / 规则 | 在仓库里（`deploy/benchmark/`、`deploy/grafana/`、`deploy/prometheus/`） | 随代码 | 无需 | 随 Git |

## 5. 核心流程

### 5.1 启动顺序与依赖条件

```
postgres(healthy) ─┬─▶ migrate(completed) ─┬─▶ api(started) ─▶ nginx(started)
redis(healthy)  ───┤                       └─▶ worker(started)
                   ├─▶ minio(healthy) ─▶ minio-init(completed)
                   └─▶ vault(started)
```

- `depends_on` 用 `condition` 而非裸列表：`postgres: {condition: service_healthy}`（已有）、`minio-init: {condition: service_completed_successfully}`（待建的 `migrate` 同理）。
- `api` 的 healthcheck 打 `/healthz`（**存活**，不查依赖）：PG 掉线不应让 api 被反复重启（重启解决不了 PG 掉线）。
- `nginx` healthcheck 用 `nginx -t`；`beat` 无 healthcheck（无 HTTP 端点），靠 `restart: unless-stopped` 与 be-21 的 `up{job="celery-beat"}` 监控。

### 5.2 发布流程（正常上线）

1. CI 构建并推送镜像 `registry.cn-<region>.aliyuncs.com/<ns>/elt-agent-backend:<git-sha>` 与 `:latest`；ECS 上 `cd /opt/elt_agent && git pull`（只拉 compose / 配置 / 前端 dist）。
2. `IMAGE_TAG=<sha> docker compose -f docker-compose.yml -f docker-compose.prod.yml pull`。
3. `docker compose ... up -d`（`migrate` 会先跑完，`api`/`worker`/`beat` 才起）。
4. 验证：`curl -fsS http://127.0.0.1/readyz` 返回 200；`deploy/ci/smoke.sh` 全绿；再打 Benchmark 门禁（`deploy/ci/benchmark_gate.py`），失败则执行 §5.6 回滚。

### 5.3 备份流程

```
02:00（beat 的 tasks.maintenance 触发，或宿主 cron 调 deploy/backup/backup.sh）
  1. pg_dump -Fc → 临时文件（宿主机 /var/backups/elt）
  2. 加密 openssl enc -aes-256-cbc -pbkdf2 → <name>.dump.enc
  3. ossutil cp → oss://<bucket>/elt-backup/YYYY/MM/DD/；回读对象大小 == 本地大小才算成功
  4. 成功 → 写 textfile collector 的 elt_backup_last_success_timestamp_seconds
     失败 → elt_backup_failures_total{kind=...} +1，记 structlog ERROR（be-21 规则 #13 告警）
  5. 清理：本地保留 7 天，OSS 保留 30 天（生命周期规则在 OSS 侧配置）
```

### 5.4 Vault：dev 与 prod 的差异

| 项 | M1（`dev`） | M4（`prod`） |
|---|---|---|
| 启动 | `vault server -dev`（已有），内存存储，重启即失 | `vault server -config`，**raft 存储** + 持久卷 |
| 认证 | 静态 `VAULT_TOKEN` | **AppRole**（`VAULT_ROLE_ID` / `VAULT_SECRET_ID`），token TTL 1h 自动续期 |
| Unseal | 不适用 | 自动 unseal（阿里云 KMS / 本地封存的 unseal key，二者取一，见 §10） |
| TLS | 明文（仅回环） | 生产自检要求 HTTPS 或回环；单机场景下继续用回环 + 网络隔离 |
| 备份 | 无（不可备份） | `raft snapshot save` 纳入每日备份 |

**切换动作**：`vault` 服务改 `command: server -config=/vault/config/vault.hcl` + 卷 `vault_data`；跑 `deploy/vault/init.sh --mode prod`；把 `role_id`/`secret_id` 写回 `.env`；重启 `worker` 验证 Secret 物化路径（be-15）仍通。dev → prod 切换会**丢弃 dev 里的所有 Secret**，因此必须在切换前把演示数据源的凭据重新写入 KV。

### 5.5 Alembic 迁移的执行时机（推荐做法）

**推荐：独立的一次性 `migrate` 服务**（`profile: 默认`，`restart: "no"`），`api`/`worker` 用 `depends_on: migrate: {condition: service_completed_successfully}` 串起来。

```yaml
migrate:
  image: ${BACKEND_IMAGE:-elt-agent-backend:local}
  command: sh -c "alembic upgrade head"
  env_file: [.env]
  environment: {APP_ENV: ${APP_ENV:-dev}}
  depends_on: {postgres: {condition: service_healthy}}
  restart: "no"
  networks: [elt_net]
```

理由（三条，缺一不可）：(1) **单一执行者**——`api` 有多个 worker 进程、`worker` 有并发 4，若各自启动时跑迁移会在 Alembic 版本表上竞争（有 advisory lock，但「谁先谁后」不确定，失败者以非零退出码崩掉容器）；(2) **失败必须阻断**——新代码配旧 schema 会以最难查的方式失败（运行时 `UndefinedColumn`），用 `service_completed_successfully` 让迁移失败**直接阻止** api/worker 启动；(3) **可观测**——一次迁移一个容器一份日志，`docker compose logs migrate` 就是完整迁移记录。

反面做法（不采用）：把 `alembic upgrade head` 塞进 `api` 的启动命令（`sh -c "alembic upgrade head && uvicorn ..."`）——多副本下会并发迁移，且失败与业务启动混在同一条日志里。

迁移本身仍遵守 be-01 §5.5：`env.py` 从 `settings.postgres_dsn_sync` 取 URL、`import models`、生成后**人工审阅**、已 apply 的 revision 不编辑。生产**不执行 `alembic downgrade`**（见 §6.3 的 expand-contract 规则）。

### 5.6 回滚 SOP

| 场景 | 动作 | 验证点 |
|---|---|---|
| 新版本启动失败（readyz 一直 503） | `IMAGE_TAG=<上一个 sha> docker compose ... up -d api worker beat` | `docker compose ps` 全 `healthy`；`/readyz` 200 |
| 冒烟失败 | 同上；同时 `git checkout <上一个 tag> -- docker-compose.yml deploy/` | `deploy/ci/smoke.sh` 全绿 |
| 迁移引入了不兼容变更 | **不能**靠回滚代码解决：按 expand-contract 设计（先加列 → 双写 → 切读 → 删旧列），回滚代码时 schema 保持向前兼容 | 回滚后旧代码能读新 schema |
| Benchmark 门禁失败 | 阻断发布（不 `up -d` 新 tag），保留上一版本运行 | `benchmark_gate.py` 退出码为 0 |
| 数据被错误发布 | 走产品内的**受管回滚**（`target.rollback_cleanup`，be-18），不要用数据库备份恢复单条业务数据 | 运行中心出现 `rolled_back` 状态 + 账本事件 |

回滚命令统一走 `IMAGE_TAG` 变量，避免手改 compose 文件：

```bash
docker compose ps --format '{{.Service}}\t{{.Image}}'                                     # 查看当前版本
IMAGE_TAG=<prev-sha> docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d api worker beat   # 回滚
```

## 6. 关键算法与规则

### 6.1 生产环境 Settings 自检 = 部署门禁

`Settings`（`core/config.py` 已有）在 `APP_ENV=prod` 时执行 `_validate_by_env()`，违反即抛 `ValueError` → **进程起不来**。部署时必须把它当门禁用：

| 门禁项 | 规则 | 违规后果 |
|---|---|---|
| 占位/空密钥 | `SECRET_KEY`、`POSTGRES_PASSWORD`、`REDIS_PASSWORD`、`MINIO_ROOT_PASSWORD`、`VAULT_TOKEN`、`LLM_API_KEY` 不得为空或以 `change-me` / `changeme` / `your-` / `sk-your` 开头 | `ValueError`，api 启动失败 |
| 调试开关 | `APP_DEBUG` 必须为 `false` | 同上 |
| CORS | `CORS_ORIGINS` 不得含 `*` | 同上 |
| Vault | `VAULT_ADDR` 必须是 HTTPS 或含 `127.0.0.1` | 同上 |
| 安全组兜底（人工） | `6379/5432/8200/9000/9001/9090/3000/3306/9030/8030/8080` 入方向规则必须删除 | 见 §8 |

CI 里的显式验证（在部署阶段之前）：`APP_ENV=prod docker compose run --rm --no-deps api python -c "from core.config import settings; print(settings.APP_ENV)"` —— 若 `.env` 还是占位值，这一步就会非零退出，**在部署前**拦住。

### 6.2 Compose 的服务级规则

| 规则 | 取值 | 理由 |
|---|---|---|
| `restart` | `unless-stopped`（`migrate` / `minio-init` 为 `"no"`） | 常驻服务自愈；一次性任务不重启 |
| 日志 | `driver: json-file`，`max-size: 20m`，`max-file: 5`（统一 `x-logging` 锚点） | 单服务约 100 MB 滚动；不配的话宿主机磁盘会被日志吃满 |
| 资源 limit / `ulimits` | postgres 2g / redis 1280m / api 1g / worker 1536m / beat 512m / mysql 2g / **doris 8g**（`nofile 65536`、`memlock -1`）/ seatunnel 4g | Doris all-in-one 单机实测吃 8G+，必须硬限并隔离在 `demo` profile；ulimits 是 Doris FE/BE 启动的前置要求 |
| healthcheck | 每个有副作用的依赖都要有；`start_period` 给足（Doris 120s） | `depends_on: service_healthy` 才有意义 |
| 时区 | `TZ=Asia/Shanghai`（镜像已 `ln -snf /etc/localtime`） | 日志与调度（Celery `timezone`）一致 |
| 非 root | 镜像 `USER app` | 安全红线；数据卷权限由 Dockerfile 内的 `chown -R app:app` 保证 |

### 6.3 一份镜像、三个进程

- `backend/Dockerfile` 两阶段：builder（`uv sync`，依赖清单先拷贝以吃满构建缓存）→ runtime（`python:3.12-slim` + `curl`/`tzdata`）。构建缓存策略：`COPY pyproject.toml uv.lock* ./` 在 `COPY . .` 之前，并用 `--mount=type=cache,target=/root/.cache/uv`。
- `api` / `worker` / `beat` 共用 `image: elt-agent-backend:local`，只在 compose 里覆盖 `command`；**禁止**为 worker 另建 Dockerfile（三份依赖必然漂移）。
- 前端**不进后端镜像**：`frontend/dist` 由 CI 构建（`npm ci && npm run build`，`vite build` 输出 `dist`）后以只读卷挂进 `nginx`——后端镜像重建频率远高于前端。
- `.dockerignore` 排除 `.venv/`、`tests/`、`.env`、`secrets/`，避免把私钥塞进镜像层（镜像层不可撤销，`docker history` 能翻出来）。

### 6.4 镜像 tag 策略

| tag | 用途 | 可变性 |
|---|---|---|
| `<git-sha>`（短 sha 或全 sha） | 部署与回滚的**唯一依据** | 不可变（同一 sha 永远对应同一代码） |
| `latest` | 便于人肉 `docker pull` 查看最新 | 可变，**不得**用于生产部署 |

`docker-compose.prod.yml` 里镜像写 `${BACKEND_IMAGE:-elt-agent-backend:local}` + `${IMAGE_TAG:-local}`，由流水线注入 `IMAGE_TAG=<sha>`；`docker-compose.yml` 保留 `build:` 供本地/ECS 手工构建。ACR 开启「镜像版本不可变」与镜像清理策略（保留最近 30 个 tag）。

### 6.5 备份加密与恢复目标

- 加密：`openssl enc -aes-256-cbc -pbkdf2 -iter 200000 -salt`，口令存 `/run/secrets/backup_key`（0600，纳入密钥备份）。**不使用** `gpg`（引入额外密钥管理面）。
- 异地：阿里云 OSS，与 ECS **不同可用区**（若同区则在 ECS 所在区之外开 bucket）。OSS 侧配生命周期：30 天后转低频、180 天后删除。
- **RPO ≤ 24 h**：控制面库丢失当日数据可接受（质量报告与证据账本按 PRD 永久保留，但每日全量已覆盖上限 24h 的窗口）；若验收方要求更低，需开 WAL 归档（`archive_mode=on` + `archive_command`），复杂度抬高（见 §10）。
- **RTO ≤ 2 h**：恢复演练步骤 —— 起临时 PG 容器 → `openssl enc -d` 解密 → `pg_restore` → `alembic upgrade head`（幂等）→ `uv run python -m scripts.verify_ledger --from-genesis` 验链 → `curl /readyz`。演练**每月一次**，结果记入 `deploy/backup/drill-log.md`（含耗时，用于校准 RTO 声明）。

### 6.6 CI/CD 流水线（云效 Flow）

| 阶段 | 命令 / 动作 | 失败处理 | 产物 |
|---|---|---|---|
| `lint` | `uv run ruff check . && uv run ruff format --check .` | 阻断，不推送镜像 | — |
| `type` | `uv run mypy engines harness` | 阻断 | mypy 报告 |
| `test` | `uv run pytest -q`（integration 用例连流水线内的 PG/Redis 容器服务） | 阻断 | JUnit + 覆盖率 |
| `build-fe` | `npm ci && npm run build`（`frontend/`） | 阻断 | `frontend/dist`（归档为流水线产物） |
| `build-image` / `push` | `docker build -t $ACR/$NS/elt-agent-backend:$SHA backend/` → `docker push`（`:${SHA}` + `:latest`） | 阻断；push **重试 1 次**（ACR 偶发网络抖动） | 镜像两个 tag |
| `deploy` | SSH 到 ECS：`cd /opt/elt_agent && git pull && IMAGE_TAG=$SHA docker compose ... pull && up -d` | 失败则按 §5.6 回滚到上一 tag | 部署记录 |
| `migrate` | 由 `migrate` 服务在 `up -d` 中自动完成；流水线检查 `docker compose logs migrate` 无异常 | 失败即 `up -d` 回滚代码 | 迁移日志 |
| `smoke` | `deploy/ci/smoke.sh --base-url http://127.0.0.1` | 失败即回滚 | 冒烟报告 |
| `benchmark-gate` | `python deploy/ci/benchmark_gate.py --suite l0:1.0.0` | **阻断发布**（`p0p1_block_rate` 必须 100%） | Benchmark 报告 + `run_id` |
| `notify` | 钉钉 webhook（be-21 的同一通道） | 通知失败不阻断 | — |

- 阶段顺序固定；`benchmark-gate` 放在 `smoke` **之后**（先保可用，再保安全质量），但二者任一失败都回滚。
- `smoke.sh` 内容：`/readyz` 200 → 用测试账号登录拿 JWT → `GET /api/v1/governance/benchmark/suites`（只读、无副作用）→ `GET /api/v1/monitor/health`。**不**在冒烟里触发执行（会产生真实副作用）。
- 密钥注入：ACR 密码、ECS SSH 私钥存云效「凭据」，以环境变量注入构建/部署步骤；云效对变量日志自动掩码。`.env` **不由流水线生成**——它在 ECS 上人工维护（0600），流水线只 `git pull` 与 `compose pull`。
- 分支策略：`main` 保护分支，合入即触发 `lint→type→test`；打 tag（`v*`）触发完整链路含部署。

### 6.7 TLS 与 CORS（当前形态）

- 外网只有 `nginx:80`。TLS 终止**预留但未启用**：不申请域名、不备案（已冻结）。若改为启用，只需在 `nginx.conf` 加 443 server 块 + Let's Encrypt（certbot）或阿里云免费证书，并把 `CORS_ORIGINS` 从空改为前端来源域名。
- `CORS_ORIGINS` 当前为空：生产由 Nginx 同源反代（前端与 `/api` 同源），因此**不需要 CORS**；只有在前端独立域名部署时才需要，且生产自检禁止 `*`。保留的安全响应头：`X-Content-Type-Options` / `X-Frame-Options: DENY` / `Referrer-Policy: no-referrer`（已在 `nginx.conf`），`server_tokens off`。

## 7. 错误码与拒绝语义

部署与运维侧**不使用 `ErrorCode`**（那是运行时 HTTP 契约）。拒绝语义由**退出码 + 日志**表达：

| 触发条件 | 表现 | 是否阻断发布 |
|---|---|---|
| `APP_ENV=prod` 且密钥为占位值 | `ValueError`，api/worker 容器退出非 0 | 是（`up -d` 后立刻发现） |
| `alembic upgrade head` 失败 | `migrate` 容器非 0 退出；`api`/`worker` 因 `service_completed_successfully` **不启动** | 是 |
| `minio-init` 或 `deploy/vault/init.sh` 失败 | 一次性容器非 0；制品上传 / Secret 物化路径不可用 | 是（冒烟会发现） |
| `ruff` / `mypy` / `pytest` 失败 | 流水线阶段非 0 | 是（不推送镜像） |
| `smoke.sh` / `benchmark_gate.py` 失败 | 非 0（后者因 `p0p1_block_rate != 1.0` 或存在回归） | 是（触发回滚 / 阻断发布） |
| `nginx -t` 失败 | healthcheck 失败，容器被标记 unhealthy | 是（发现问题即修） |
| 备份失败 | 脚本非 0 + `elt_backup_failures_total` +1 | 否（但 be-21 规则 #13 告警） |
| 安全组未收紧 | compose 无报错，**静默暴露**（例如 `6379` 仍对外） | 无法自动发现——靠 §9 的端口检查与人工核对 |

唯一与运行时错误码相关的点：`/readyz` 返回 503（`{status: "degraded", checks: {...}}`），与 be-01 约定一致；**部署脚本必须把非 200 视为失败**，不能只看「进程在跑」。

## 8. 安全与合规约束

- **网络与安全组**：外网只开 `22`（限管理员 IP）+ `80`（+ 未来 `443`）。**必须删除**的入方向规则：`6379`（已知待办，`需求对齐记录 §六` 记录过 2026-10-14 探测到 Redis 公网可连）、`5432`、`8200`、`9000`、`9001`、`9090`、`3000`、`3306`、`9030`、`8030`、`8080`。核对方式见 §9。
- **Secret**：`secrets/` 与 `.env` 不进仓库；私钥 0600、容器内 `/run/secrets:ro`；Secret 值不得出现在日志/响应/账本/模型上下文（be-01/be-03/be-15 落实）；备份文件加密后才出本机。
- **镜像与准入**：非 root 运行；`.dockerignore` 排除密钥；镜像来自 ACR（私有仓库，需鉴权）；生产 tag 用不可变 sha。`APP_ENV=prod` 的自检（§6.1）是唯一的应用层准入；**不做** `latest` 自动部署。
- **审计**：部署动作（谁在何时上了哪个 tag）在流水线记录 + 钉钉通知；产品内的关键动作走 `EvidenceLedger`（be-06）。
- **数据**：`deploy/mysql/init/01-demo-schema.sql` 里的手机号/身份证是**演示造假数据**（`13800000001` 类），仍会在探查时被脱敏，用于验证红线；不得用真实客户数据替换。
- **合规**：证据账本与质量报告永久保留（`EVIDENCE_RETENTION_DAYS=0`），导出 JSON + CSV 并附独立验链脚本（PRD §16）。

## 9. 测试要点

### 9.1 交付前自检清单（CLAUDE.md §10 的可执行落地）

| 自检项 | 可执行命令 | 通过判据 |
|---|---|---|
| compose 语法与变量齐备 | `docker compose config -q && docker compose --profile demo --profile obs config -q` | 退出码 0 |
| 全仓无明文密钥 | `git grep -nEi '(password\|passwd\|secret\|token\|api[_-]?key)[[:space:]]*[:=][[:space:]]*[^ "'"'"'$]{6,}' -- . ':(exclude).env.example' ':(exclude)vibecoding_doc/**' ':(exclude)frontend/package-lock.json'`；建议再引 `gitleaks detect --no-git` | 无输出 / gitleaks 无泄漏 |
| 生产自检生效 | `APP_ENV=prod docker compose run --rm --no-deps api python -c "from core.config import settings; print(settings.APP_ENV)"` | 占位 `.env` 时**非零退出** |
| `ruff` / `mypy` | `cd backend && uv run ruff check . && uv run mypy engines harness` | 全绿 |
| 单测 + 协议类集成测试 | `cd backend && uv run pytest -q`；`uv run pytest -q -m integration -k "self_approval or capability_replay or fingerprint or budget"` | 全绿；`P0/P1 拦截率 = 100%`、`未授权/重放成功率 = 0` 由用例证明 |
| 独立验链 | `cd backend && uv run python -m scripts.verify_ledger --from-genesis` | 输出「链完整」，退出码 0 |
| `/metrics` 不对外 | `curl -s -o /dev/null -w '%{http_code}\n' http://60.205.138.167/metrics` | `403`（Nginx `deny all`），**不得为 200** |
| 端口未暴露公网 | 在 ECS 上 `ss -lntp \| grep -E ':(5432\|6379\|8200\|9000\|9001\|9090\|3000|3306\|9030)\b'`；再在**外网**用 `nc -zv 60.205.138.167 6379` | 前者只见 `127.0.0.1`；后者必须 `refused` |
| 探针与冒烟 | `curl -fsS http://127.0.0.1/healthz && curl -fsS http://127.0.0.1/readyz`；`deploy/ci/smoke.sh --base-url http://127.0.0.1` | 均 200 且 `readyz.checks` 全 `ok`；冒烟退出码 0 |
| 备份可跑通/可恢复 | `deploy/backup/backup.sh --dry-run && deploy/backup/backup.sh`；`deploy/backup/restore_drill.sh --from <最新备份> --to /tmp/drill` | 生成加密对象并上传成功、指标更新；还原成功 + 验链通过 |
| 演示场景一（MySQL→Doris 宽表） | `docker compose --profile demo up -d` → 按 PRD §14.1 走一遍（脚本化后一条命令） | 从空环境一键跑通，出现 `succeeded` + `swap.succeeded` |
| 演示场景二（脏数据硬中断 + 回滚） | 触发 `orders` 脏数据用例（拒绝率 20%） | `interrupted` → 诊断 → `rolled_back` 可复现 |
| Benchmark 报告可出 | `python deploy/ci/benchmark_gate.py --suite l0:1.0.0` | 退出码 0，报告含五项指标 |

### 9.2 单元 / 集成测试

- **单元**：`Settings` 生产自检四类违规（占位密钥 / `APP_DEBUG=true` / CORS `*` / Vault 非 HTTPS 非回环）→ 分别抛 `ValueError`；`safe_dump()` 无明文。
- **集成**：`docker compose config` 渲染无未定义变量；`depends_on` 的 `condition` 在目标服务不健康时确实延迟启动（用 `postgres` 造启动失败验证）；`migrate` 未完成时 `api` 容器不得启动。
- **必须覆盖的反例**：占位 `.env` 配 `APP_ENV=prod` 必须启动失败；`migrate` 失败必须阻止 api 启动；回滚到上一 tag 后 `/readyz` 必须恢复 200。本机（Windows，无 Docker）只能做 YAML 语法与变量引用静态检查（`python -c "import yaml; yaml.safe_load(open('docker-compose.yml'))"`）+ `Settings` 自检单测；其余标 `@pytest.mark.integration` 在 ECS 上跑。

## 10. 未决与风险

| 事项 | 现状 | 建议 / 验证方式 |
|---|---|---|
| ECS 环境未勘验 | SSH 公钥未配置（`需求对齐记录 §六`） | 首件事：`cat /etc/os-release`、`nproc && free -g`、`df -h /`、`docker --version && docker compose version`、`java -version`、`ls /opt`。**Docker Compose 版本必须 ≥ 2.17**（`service_completed_successfully` 与 `profiles` 需要）；低版本需改回 `depends_on` 列表 + 脚本串行 |
| 域名与 HTTPS | 已冻结为「不需要」 | 维持当前；若改为需要，立即启动备案（2–3 周）并重做 §6.7 |
| Doris 内存实测 | compose 写死 `8g` limit，未实测 | `demo` profile 起后 `docker stats elt-doris` 观察 24h；若 ECS < 16G，考虑跑完演示即 `stop` |
| ACR / 云效账号与命名空间 | 未创建 | 需项目负责人开通并给出 `ACR_NS` / `REGION`；确认私有仓库鉴权方式（固定密码 vs 临时 token） |
| `beat` 是单点 | 无高可用 | 可接受（单机）；若需容灾，靠 compose `restart: unless-stopped` + be-21 的 `up{job="celery-beat"}` 告警；**不要**起两个 beat（Outbox 的 SKIP LOCKED 能抗重复投递，但重复调度会放大负载） |
| WAL 归档 / PITR | 未启用 | 当前 RPO 24h；若验收要求更严，开 `archive_mode=on` 并把 `archive_command` 指向 OSS 挂载点 |
| Vault prod 的 unseal 方式 | 未定（KMS vs 手工 unseal key） | 单机建议：unseal key 封存后纳入密钥备份 + 启动时由 `deploy/vault/init.sh` 读 `/run/secrets` 自动 unseal；KMS 会增加一次外部依赖，谨慎 |
| 云效 Flow 的配置文件路径与格式 | 未接入 | `deploy/ci/flow.yml` 作为仓库内的**事实来源**；具体接入时确认云效期望的路径（可视化流水线或 YAML 导入），把等价步骤对齐即可 |
| `docker-compose.prod.yml` 未建 | 生产部署目前只能用 `build` | M4 补：去掉 `build:`、镜像指向 ACR、`prometheus`/`grafana` 资源收紧、`api` 加 `read_only: true` 实验项 |
| 备份口令 `backup_key` 的存放 | 未定义 | 建议与 Ed25519 密钥分离存放（不同 U 盘/不同账号），否则「备份与钥匙一起丢」；纳入 §9.1 的自检项；gitleaks 未引入，建议加 `.gitleaks.toml` + 流水线 `lint` 阶段一步（或 pre-commit 钩子） |
| `frontend/dist` 的部署方式 | 由卷挂载（`./frontend/dist`） | 若改为多机/不可变部署，应把前端打进一个 nginx 镜像而非挂卷；当前单机挂卷足够 |
| 本机无 Docker 导致的验证缺口 | compose 相关改动只能在 ECS 验证 | 约定：每次改 compose 后在 ECS 上跑一次 §9.1 的 compose/探针检查，并把结果贴回验收记录 |

## 11. 变更记录

| 日期 | 变更 | 原因 |
|---|---|---|
| 2026-10-16 | 初稿：固化三 profile 拓扑与端口矩阵、一份镜像三进程、Secret 与配置治理、初始化职责划分、备份/恢复目标（RPO 24h / RTO 2h）、云效流水线九阶段与 `IMAGE_TAG` 回滚、交付前自检命令清单 | M1 骨架与 M4 生产化落地 |
| 2026-10-16 | **提前落地 §5.5 的 `migrate` 服务与 §4 的 Alembic 环境**（本属 M4） | 实机核对发现：`docker-compose.yml` 的 api 直接起 `uvicorn`、**没有任何迁移步骤**，而仓库里连 `alembic.ini` / `alembic/env.py` / `versions/` 都不存在——compose 起来后 PG 是个空库，32 张表一张都没有，登录接口必然 500。**「能不能跑起来」的答案是「不能」，且卡点不在业务代码**。故把这两件从 M4 提前到现在 |
| 2026-10-16 | 🔴 **`env.py` 用异步 DSN（asyncpg），§5.5 原文写的 `settings.postgres_dsn_sync`（psycopg）未采纳** | `psycopg` 并不在 `pyproject.toml` 的依赖里：`postgres_dsn_sync` 这个属性**没有消费者**。为一个只在本地跑几次的运维命令引入第二个 PG 驱动，换来的是「两套驱动、两套参数风格、两套类型适配」的长期维护面，而 asyncpg 已经在跑生产流量。若日后确实需要同步驱动（备份脚本 §6.2），应在**引入它的那次**一并提交，而不是让一个悬空属性决定架构 |
| 2026-10-16 | 初始迁移由 `backend/scripts/gen_initial_migration.py` **离线生成并冻结**，不用 `alembic revision --autogenerate` | autogenerate 必须连一个**已建表**的活库做差集；开发机没有 Docker/PG（CLAUDE.md §9），等到有库的环境上生成，那份迁移就只存在于那台机器上、评审时看不到。改为用 PostgreSQL dialect 直接编译 metadata 的 DDL 并冻结（生成器拒绝覆盖已存在的文件）。已用 PG 语法解析器验证过整份输出；**首次 `alembic upgrade head` 仍需在 ECS 上确认**（本机无 PG 可执行） |
| 2026-10-16 | **`alembic.ini` 全文必须 ASCII**（注释用英文，理由写在 `env.py` 的中文 docstring 里） | Alembic 用 `configparser` + 平台默认编码读这份文件，中文 Windows 上是 GBK：文件里任何一个非 ASCII 字节都会让 `alembic` 在跑第一条迁移之前就 `UnicodeDecodeError`。同理 `env.py` **不可**调 `core.logging.configure_logging()`——它把日志挂到 stdout，会污染 `alembic upgrade --sql > init.sql` 的输出（文件头混进 3 行日志，既不能 `psql -f` 也过不了语法解析） |
| 2026-10-16 | 新增 §「本地开发（控制面在本机，基础设施在 ECS）」的运行路径与 `deploy/tunnel.py`（口令只从 `ELT_ECS_PASSWORD` 读，不进文件/参数） | 开发与演示的实际形态是「容器留在 ECS、API/worker/前端在本机跑」，而文档原先只有 compose 一条路。隧道脚本此前只存在于开发机临时目录，演示不可复现 |
| 2026-10-16 | 端口矩阵补充：本地经隧道时 Redis 是 **6380**、演示 MySQL 是 **3307**；Vite 只监听 IPv6 localhost（`127.0.0.1:5173` 连不上） | 与 compose 的默认端口不同，照默认值填 `.env` 会连到本机别的服务上；Vite 的现象是「端口没人监听」而不是报错 |
