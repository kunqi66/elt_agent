# 模块概要设计 · 索引

> 上游：[概要设计总纲.md](../概要设计总纲.md) ｜ 事实来源：[PRD.md](../PRD.md)（SSOT）｜ 数据：[核心数据字典.md](../核心数据字典.md) ｜ 决策：[需求对齐记录.md](../需求对齐记录.md)
> 写作规范：[00-模板与全局约定.md](00-模板与全局约定.md)

本目录把《概要设计总纲》里的**结构、边界与协议**落到**可实现的模块**粒度：每个模块一份文档，写清职责边界、依赖方向、对外接口签名、涉及的表、核心流程/状态机、错误码、安全约束与测试要点。

**这里不重写 PRD，也不重复数据字典的表定义**——只写「这个模块怎么做、和谁交互、边界在哪」。

---

## 1. 阅读顺序

```
00-模板与全局约定           ← 先读，所有文档的公共词汇与格式
  │
  ├── 第一层：地基（M1）      be-01 → be-02 / be-03 / be-04
  ├── 第二层：安全内核（M1/M3）be-05 → be-06 / be-07
  ├── 第三层：生成链路（M2）   be-08 → be-09 → be-10 → be-11 → be-12
  ├── 第四层：治理链路（M3）   be-13 → be-14 → be-15 → be-16
  ├── 第五层：发布与诊断（M3/M4）be-17 → be-18 → be-19
  └── 第六层：评测与运维（M4） be-20 / be-21 / be-22
  前端横切                    fe-01 → fe-02 … fe-08
```

后端按**数据流方向**读；前端按**页面出现顺序**读。跨模块只通过 Pydantic DTO 与 Harness 工具契约耦合，读单个模块时顺着「依赖」一栏跳转即可。

---

## 2. 后端模块清单

| 编号 | 模块 | 主要代码位置 | 里程碑 | 依赖（后端） |
|---|---|---|---|---|
| [be-01](be-01-基座与横切约定.md) | 基座与横切约定 | `core/` `api/router.py` `main.py` | M1 | — |
| [be-02](be-02-认证租户与职责槽.md) | 认证 · 租户 · 项目 · 职责槽 | `api/auth*` `services/auth` `models/tenant.py` | M1 | be-01 |
| [be-03](be-03-数据源与密钥引用.md) | 数据源与密钥引用 | `api/datasources` `services/datasource` `clients/vault` | M1 | be-01 be-02 |
| [be-04](be-04-元数据探查与脱敏.md) | 元数据探查与脱敏 | `engines/metadata` `clients/db` | M1 | be-03 |
| [be-05](be-05-Harness内核与工具代理.md) | Harness 内核与工具代理 | `harness/broker.py` `harness/capability.py` `harness/replay.py` | M1/M3 | be-01 |
| [be-06](be-06-证据账本与验链.md) | 证据账本与验链 | `harness/ledger.py` `models/evidence.py` `scripts/verify_ledger.py` | M1/M3 | be-01 be-05 |
| [be-07](be-07-PDP风险策略引擎.md) | PDP 风险策略引擎 | `engines/policy/` | M2 | be-01 |
| [be-08](be-08-LLM-Provider抽象层.md) | LLM Provider 抽象层 | `clients/llm/` | M2 | be-01 |
| [be-09](be-09-LangGraph工作流与三层状态.md) | LangGraph 工作流与三层状态 | `workflow/` `api/pipelines` | M2 | be-04 be-07 be-08 |
| [be-10](be-10-EtlPlan与HOCON生成.md) | EtlPlan 与 HOCON 生成 | `engines/plan` `engines/hocon` | M2 | be-08 be-09 |
| [be-11](be-11-确定性门禁与有限自动修复.md) | 确定性门禁与有限自动修复 | `engines/gate` `engines/autofix` | M2 | be-07 be-10 |
| [be-12](be-12-版本冻结与制品指纹.md) | 版本冻结与制品指纹 | `engines/fingerprint` `services/version` | M2 | be-06 be-11 |
| [be-13](be-13-准备单与四眼审批.md) | 准备单与四眼审批 | `services/preparation` `services/approval` `api/preparations` | M3 | be-02 be-07 be-12 |
| [be-14](be-14-执行编排与Outbox.md) | 执行编排与 Outbox | `harness/outbox.py` `tasks/` `api/runs` | M3 | be-05 be-06 be-13 |
| [be-15](be-15-SeaTunnel客户端与Secret物化.md) | SeaTunnel 客户端与 Secret 物化 | `clients/seatunnel` `clients/secrets` | M3 | be-03 be-14 |
| [be-16](be-16-运行监督与硬中断.md) | 运行监督与硬中断 | `engines/supervision` `tasks/supervision.py` | M3 | be-14 be-15 |
| [be-17](be-17-质量契约与数据分流.md) | 质量契约与数据分流 | `engines/quality` `models/execution.py` | M3 | be-15 be-16 |
| [be-18](be-18-原子发布与受管回滚.md) | 原子发布与受管回滚 | `clients/target/` `services/publish` | M3 | be-06 be-17 |
| [be-19](be-19-根因诊断.md) | 根因诊断 | `services/diagnosis` `engines/diagnosis` | M4 | be-08 be-16 be-17 |
| [be-20](be-20-Benchmark评测.md) | Benchmark 评测 | `services/benchmark` `models/benchmark.py` | M4 | be-09 be-11 be-16 |
| [be-21](be-21-可观测性日志指标告警.md) | 可观测性：日志 / 指标 / 告警 | `core/logging.py` `core/metrics.py` `deploy/prometheus` | M4 | be-01 |
| [be-22](be-22-部署备份与CICD.md) | 部署、备份与 CI/CD | `docker-compose.yml` `deploy/` `backend/Dockerfile` | M1/M4 | 全部 |

## 3. 前端模块清单

| 编号 | 模块 | 主要代码位置 | 里程碑 | 依赖（前端） |
|---|---|---|---|---|
| [fe-01](fe-01-前端基座与工程约定.md) | 前端基座与工程约定 | `src/main.ts` `src/api/` `src/stores/` `src/layouts/` | M1 | — |
| [fe-02](fe-02-总览工作台.md) | 总览工作台 | `src/views/dashboard/` | M1 | fe-01 |
| [fe-03](fe-03-数据源与资产管理.md) | 数据源与资产管理 | `src/views/datasource/` | M1 | fe-01 |
| [fe-04](fe-04-Pipeline-Studio.md) | Pipeline Studio | `src/views/pipeline/` | M2 | fe-01 fe-03 |
| [fe-05](fe-05-准备单与审批控制台.md) | 准备单与审批控制台 | `src/views/approval/` | M3 | fe-01 fe-04 |
| [fe-06](fe-06-运行中心与质量诊断.md) | 运行中心与质量诊断 | `src/views/run/` | M3 | fe-01 fe-05 |
| [fe-07](fe-07-安全治理与Benchmark.md) | 安全治理与 Benchmark | `src/views/governance/` | M4 | fe-01 |
| [fe-08](fe-08-项目设置与成员权限.md) | 项目设置与成员权限 | `src/views/settings/` | M1 | fe-01 |

---

## 4. 分层依赖（一图流）

```
                    ┌───────────────────────────────┐
    前端  fe-xx ────▶│ api/        （只做校验/注入/权限/转发）│
                    └───────────────┬───────────────┘
                                    ▼
                    ┌───────────────────────────────┐
                    │ services/   （编排，禁 import fastapi）│
                    └───┬───────────┬───────────┬───┘
                        ▼           ▼           ▼
              ┌──────────────┐ ┌──────────┐ ┌──────────────┐
              │ engines/     │ │ clients/ │ │ harness/     │
              │ 纯函数算法层  │ │ 外部系统  │ │ 副作用唯一出口 │
              │ 无 IO/无 LLM │ │ Vault/ST │ │ Broker/Ledger│
              └──────────────┘ └──────────┘ └──────────────┘
                        ▲           ▲           ▲
                        └───────────┴───────────┘
                                    │
                    ┌───────────────┴───────────────┐
                    │ workflow/（LangGraph）          │
                    │ tasks/（Celery 只做编排）        │
                    └───────────────────────────────┘
```

铁律（违反即返工，见 CLAUDE.md §4）：`api` 无业务逻辑；`services` 不 import fastapi；`engines` 是纯函数、不碰 DB/Redis/网络/LLM；`harness` 不依赖 `services`/`api`；跨层只传 Pydantic。

---

## 5. 安全红线在模块间的落点

| 红线 | 落点模块 | 验证方式 |
|---|---|---|
| 副作用唯一出口 | be-05 ToolBroker | 全仓 grep 无绕过调用 |
| 无明文 Secret | be-03 be-15 | 日志/响应/账本样例扫描 |
| PDP 确定性（无 LLM） | be-07 | 纯函数单测 + import 图检查 |
| 不可变制品禁止 UPDATE | be-01 ORM 守卫 be-12 be-13 | `test_model_guards` |
| 证据账本单入口 | be-06 | `verify_ledger.py` 独立验链 |
| Capability 单次消费 | be-05 | 并发消费集成测试 |
| 脱敏默认开启 | be-04 be-17 | 样本与错误行断言 |
| 四眼 + 自批拦截 | be-02 be-13 | DB CHECK + 服务端双保险集成测试 |

---

## 6. 维护约定

1. **一个模块一份文档**，编号不复用；模块拆分/合并时新增编号，并在本条记录变更。
2. 文档与代码**同一次提交**更新：改了接口签名/表结构/错误码，文档必须同改。
3. 表名、字段名、枚举值一律以《核心数据字典》与 `backend/models/enums.py` 为准，**文档不得自造**。
4. 未决事项写进文档最后一节「未决与风险」，不要用「大概/可能」把不确定写成结论。
5. 本目录是**概要设计**（模块级），落到具体类/函数的实现细节时进入 `../specs/`。
