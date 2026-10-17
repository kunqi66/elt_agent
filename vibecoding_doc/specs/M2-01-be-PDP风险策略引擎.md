# M2-01 · be-07 PDP 风险策略引擎

> 状态：**已实现**（引擎 + 条件适配 + service 载入 + `preview` 接口；策略管理面见 §7）
> 期次：M2 ｜ 依赖：be-01（`core`）、be-02（`SubjectContext` / `active_roles`）、be-06（证据账本）
> 被依赖：be-11（确定性门禁）、be-13（Prepare/审批）、be-16（预算硬中断）、be-20（Benchmark）
> 设计来源：[be-07-PDP风险策略引擎.md](../模块概要设计/be-07-PDP风险策略引擎.md)
> 数据来源：[核心数据字典.md](../核心数据字典.md) §4.6（`policy_rule` / `risk_policy_version`）
> 需求来源：[PRD.md](../PRD.md) §6.6、§1.4（P0/P1 拦截率 100%）

---

## 1. 模块定位

### 1.1 做什么

1. **确定性风险判定**：`decide(intent, scope, env, level) → PolicyDecision`，
   P0–P3 四档，判定只查规则表（CLAUDE.md §5.3：**严禁 LLM 参与**）。
2. **条件 DSL 与扁平形式的互转**：DB/前端用扁平命名键，引擎用带组合子的嵌套 DSL，
   转换只在 `condition.py` 一处发生。
3. **基线规则集**：11 条内置规则（4 P0 / 5 P1 / 1 P2 / 1 P3），写死在代码里，
   可被加严、**不可被关闭或降级**。
4. **规则载入与合并**：`policy_rule` 覆盖行 ⊕ 内置基线 → 生效规则集；
   被拒的越权覆盖转成告警并**写入证据账本**。
5. **判定预演**：`POST /governance/policies/preview`，只读、不落库、不入账。

### 1.2 明确不做

- **不做审批**（be-13）。本模块只产出「需要哪两个槽位」，不判断谁批。
- **不做门禁**（be-11）。`blocked=True` 时**调用方必须拒绝**，拒绝语义与错误码由调用方决定。
- **不做运行监督**（be-16）。本模块只算出生效预算，不执行硬中断。
- **不做策略管理界面**（规则 CRUD / 版本提交审批 / 预算默认值），见 §7 与 §10.6。
- **不读配置、不碰数据库、不取当前时间**：`now` 与 `BudgetDefaults` 由 service 注入。

---

## 2. 分层落点

| 层 | 文件 | 状态 |
|---|---|---|
| `engines/policy/` | `models.py` 12 个 DTO（全部 frozen + `extra="forbid"`） | 本次新建 |
| `engines/policy/` | `rules.py` 特征白名单 + DSL 求值/校验 + 11 条基线 + 合并/预算 | 本次新建 |
| `engines/policy/` | `condition.py` 扁平 ↔ 规范 DSL（**唯一转换点**） | 本次新建 |
| `engines/policy/` | `pdp.py` `decide()` | 本次新建 |
| `engines/policy/` | `__init__.py` 导出 roles（be-02 起）+ PDP 两套 API | 本次修改 |
| `services/` | `policy.py` 载入/合并/预演/判定入口 | 本次新建 |
| `schemas/` | `policy.py` `PolicyPreviewRequest` / `PolicyPreviewScope` | 本次新建 |
| `api/` | `policy.py` `POST /governance/policies/preview` | 本次新建 |
| `api/` | `router.py` 挂载 `policy_router` | 本次修改 |
| `models/` | `models/governance.py::PolicyRule` / `RiskPolicyVersion` | 📄 已实现，本次不改（缺列见 §10.6） |

`engines/policy/` 的纯函数约束由 `tests/test_policy_import_graph.py` 强制（见 §5.6）。

---

## 3. 涉及的表

| 表 | 动作 | 关键约束 |
|---|---|---|
| `policy_rule` | 读（全表） | `uq_policy_rule_code_version`；**无 `tenant_id`**（全局表）；`condition` 存扁平形式 |
| `risk_policy_version` | 读（`MAX(version) WHERE effective_at IS NOT NULL`） | `uq_risk_policy_version_tenant_id_version` |
| `evidence_event` | INSERT | 仅「被拒的越权覆盖」，见 §5.4 |

**没有任何写动作**（`preview` 与 `decide_for_project` 都不写业务表）。
本模块对 `policy_rule` 的**写**入口属治理模块，本轮不做。

### 3.1 🔴 `policy_rule` 的 `enabled=False` 行必须被载入

`enabled=False` 的覆盖行表达的是「**关掉**这条规则」，不是「没有这条规则」。
若在 SQL 层按 `enabled` 过滤，这条覆盖会消失，内置基线**重新生效**——
用户会发现规则关不掉，而日志里没有任何异常。

因此 `load_active_policy()` 读全表，`enabled` 交给 `merge_rules()` 处理：
- 非基线规则（`p1.` / `p2.`）：`enabled=False` 即从生效集里移除；
- 基线规则（`p0.` / `p3.`）：**忽略该覆盖**并产生 `RuleMergeWarning`。

### 3.2 一条坏规则只能降级为警告

单条 `condition` 存坏（手改过 DB、旧格式）时，`RuleSpec` 构造会抛。
若让它冒泡，结果是**所有人的判定都不可用**——一个配置错误升级成全站故障。
所以逐行 `try/except`：坏行 → `RuleMergeWarning` + `structlog.warning`，
其余规则照常生效。

---

## 4. 对外契约

### 4.1 HTTP

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/v1/governance/policies/preview` | 传 `{project_id, env, level, intent, scope}` → `PolicyDecision` |

请求体里的 `scope` 是 `PolicyPreviewScope`——与引擎的 `ResourceScope` 字段一一对应，
**但没有 `tenant_id` / `project_id`**：租户只能来自登录态。
引擎的 `ResourceScope` 把这两个字段定为必填（跨租户本身就是 P0），
直接拿它当请求体就等于让客户端从请求里声明自己是哪个租户；
预演虽不改数据，但它是判定逻辑的探针——能测出「别的租户的资源会被判几级」，同样是一次信息泄露。

响应体直接复用 `engines.policy.PolicyDecision`（见 §5.7）。

### 4.2 Python（跨模块契约）

```python
# engines/policy/pdp.py
def decide(intent, scope, env, level, *, now, rules=None, defaults=None,
           project_overrides=None, policy_version=0) -> PolicyDecision: ...

# engines/policy/condition.py
def normalize_condition(condition: Mapping[str, Any]) -> dict[str, Any]: ...

# services/policy.py —— 其它模块（门禁/Prepare/Commit）的判定入口
async def decide_for_project(session, *, tenant_id, project_id, actor_id,
                             intent, scope, env, level, now=None) -> PolicyDecision: ...
async def load_active_policy(session, *, tenant_id, at=None) -> ActivePolicy: ...
def budget_defaults() -> BudgetDefaults: ...
```

`decide_for_project()` **不校验读权限**：调用方（门禁、Prepare）各自已经做过角色检查，
在这里再查一次只会产生第二份权限语义。`preview()` 会校验（它是面向用户的接口）。

---

## 5. 核心逻辑

### 5.1 判定流程

沿用设计 §6.4 的七步：`build_features` → 全量收集命中（**不短路**）→ `most_severe`
→ `blocked` → `required_slots` → `hardening_required` → 排序输出。
无命中时走 `decide()` 内部的兜底分支（`p2.default_write` / `p2.default_read`）。

### 5.2 🔴 本次修正：`p2.in_scope_with_rollback` 让 P3 在非 prod 不可达

原规则表达式为

```
env != "prod" and out_of_scope_count == 0 and over_default_ratio_max <= 1
and (not is_write or has_rollback_plan)
```

它对「**非写入** + 范围干净 + 预算不超」的**只读探查**也成立，于是
`most_severe(P2, P3) = P2`——**`p3.readonly_probe` 在非 prod 环境下永远不可达**，
一次 dev 环境的连通性测试会被要求两个审批槽。

**这个缺陷只在非 prod 暴露**：prod 下 `env != "prod"` 为假，规则不命中、P3 正常生效，
所以只跑 prod 用例会以为设计是对的。修法：加一个叶子
`intent.kind != "readonly_probe"`（`rules.py` 内有注释说明为何必须有它）。

### 5.3 🔴 本次修正：生效预算 = 逐维度 `min(申报值, 上限)`

设计 §6.4 第 7 步原文是「`defaults ⊕ project_overrides`」，字面实现等于**忽略申报值**。
取 `min` 的理由：这份生效预算会被 be-16 的运行监督拿去硬中断，
抬到上限就是**放宽用户自己设的那道闸**；取 `max` 则放宽策略红线，是安全问题。
申报缺失（`None`）退化为上限。

### 5.4 越权覆盖必须入账

`decide_for_project()` 载入时若发现被拒的降级/关闭覆盖，写一条 `POLICY_EVALUATED`，
payload 为 `{"warnings": [{"code", "reason"}]}`。不记的话，
「有人试图关掉一条 P0」与「什么都没发生」在审计上完全等价。
（重复入账问题见 §10.9。）

### 5.5 扁平 → DSL 的编译规则

| 扁平键 | 真值编译为 | 假值编译为 |
|---|---|---|
| `env: Env[]` | `env in [...]` | 无（缺省即不约束；**显式空数组**编译成 `in []` = 永不命中） |
| `writes_formal_table` | `scope.writes_formal_table is_true` | `... is_false` |
| `sensitive_plaintext_egress` | `scope.sensitive_plaintext_egress_count > 0` | `== 0` |
| `bypass_approval_intent` | `any[bypass_approval, disable_audit, export_plaintext_sensitive]` | 三者全 `is_false` |
| `cross_tenant_access` | `any[intent.cross_tenant, cross_tenant_count > 0]` | 两者皆假 |
| `resource_out_of_scope` | `out_of_scope_count > 0` | `== 0` |
| `budget_over_default_factor: number` | `budget.over_default_ratio_max > N` | — |
| `missing_rollback_plan` | `all[is_write, not has_rollback_plan]` | `any[not is_write, has_rollback_plan]`（**真补集**） |
| `unregistered_secret_ref` | `unregistered_secret_count > 0` | `== 0` |
| `read_only` | `intent.is_write is_false` | `intent.is_write is_true` |

三条纪律：

1. **未知键 → `ValueError`**，不静默忽略（「用户以为加了一条约束，系统当它不存在」
   是策略界面最容易骗人的地方）；
2. **假值取真补集**，不是翻转单个叶子——`missing_rollback_plan: false` 若写成
   `has_rollback_plan is_true`，对只读意图恒假；
3. **不提供反向转换**：DSL → 扁平不是满射，遇到 `any` 会静默丢语义。

### 5.6 确定性保证（CLAUDE.md §5.3 的落地）

| 保证 | 做法 |
|---|---|
| 同输入必同输出 | 无随机、无缓存、无全局可变状态；12 个 DTO 全部 `frozen=True` |
| 无时间依赖 | `now` 由调用方注入；`decide()` 对裸 `datetime` 直接 `ValueError`；AST 扫描禁 `datetime.now(`/`utcnow(`/`time.time(`/`date.today(` |
| 无 LLM | `engines/policy/` 的 import 白名单里没有任何 provider 客户端 |
| 顺序无关 | `hits` 按 `(level.rank, code)` 排序；打乱 `rules` 顺序输出不变 |
| 无数值歧义 | 比率用 `Decimal`；`float → Decimal` 一律经 `str()`（`Decimal(0.05)` 会带上二进制尾数） |
| 分级不靠字典序 | 敏感等级走 `LEVEL_RANK` 局部序，**禁止**字符串比较 |
| import 图 | AST 白名单 + **增量**运行期断言（`models.enums` 是唯一豁免，理由见设计 §6.7） |

### 5.7 响应体复用引擎 DTO

不造镜像 DTO：镜像一份的必然结果是两边字段逐渐不一致，
而「判定结果里少一个字段」只有在审计要求复现历史判定时才暴露。
`PolicyDecision` 能直接序列化（`model_dump(mode="json")` 有测试保证）。

---

## 6. 错误码

| # | 触发条件 | 错误码 | HTTP | 写账本 |
|---|---|---|---|---|
| 1 | `preview` 调用者在本项目无任何职责槽 | `ROLE_REQUIRED` | 403 | 否 |
| 2 | 请求体含未知字段 / 非法 `ResourceRef` | `VALIDATION_ERROR` | 422 | 否 |
| 3 | 规则条件引用未定义特征（载入期） | 不抛给前端：转 `RuleMergeWarning` + `POLICY_EVALUATED` | — | 是 |
| 4 | 规则条件非法（发布期，M4） | `VALIDATION_ERROR` | 422 | 否 |

门禁/Prepare/Commit 侧的 `GATE_BLOCKED` / `POLICY_BLOCKED` 由 be-11 / be-13 定义，
本模块只产出 `blocked=True`。

---

## 7. 异步任务与尚未实现的部分

- **无 Celery 任务**，无异步边界需要处理。
- ❌ **策略管理面**：`policy-rules` 的 CRUD/toggle、`policy-versions` 的列表/diff/提交审批、
  `budget-defaults` 的读写、Benchmark 的 `malicious_p0` 用例对接。前端
  `src/api/governance.ts` 已定形，落它们需要先加列（§10.6）。
- ❌ **`project_overrides` 的载体**：`decide()` 已支持，但没有表/字段存它（设计 §10.2）。
- ❌ **`p2.default_*` 在 DB 里不可见**：它是 `decide()` 内部兜底分支，审批人看不到
  （设计 §10.5，需产品确认是否插一条说明性行）。

---

## 8. 验收标准

对应 PRD §1.4 / A 系列：

| 验收点 | 用例 |
|---|---|
| P0 全覆盖、零漏判 | `test_P0穷举_七个触发特征的全笛卡尔积` —— 7 个触发特征 × 2 环境 = **256 组**，与独立重写的判据 `_expected_blocked` 对拍（判据不复用引擎的任何函数，否则只是自己证明自己） |
| 加严单调性 | `test_单调性_任一触发特征由假变真等级不放松`、`test_单调性_加严环境不放松` |
| 确定性 | `test_确定性_同输入一百次输出完全一致`、`test_顺序无关_打乱规则顺序结果不变` |
| 基线不可关闭 | `test_基线不可关闭_禁用被忽略并告警`、`test_基线不可降级`、`test_关闭的基线在判定里仍然生效`、`test_基线可以加严` |
| 只读探查免审批 | `test_P3纯只读探查免审批`、`test_只读探查在prod也仍是P3`（本轮修 P2 重叠后才成立） |
| 无网络/无时间/无 IO | `test_policy_import_graph.py`（6 条） |
| 预演不落库不入账 | `test_policy_service.py::test_预演不落库不入账` |
| 租户来自登录态 | `test_policy_api.py::test_预演请求体不含租户字段` |

本轮实测：`ruff` 通过、`mypy engines harness clients` 21 文件零错误、**354 passed**。

---

## 9. 测试数据

| 文件 | 条数 | 覆盖 |
|---|---|---|
| `tests/test_policy_engine.py` | 66 | P0 穷举、逐规则正反例、单调性、确定性、DSL 算子、基线不可关闭、特征完整性 |
| `tests/test_policy_condition.py` | 25 | 形式判别、逐键的真/假/缺省三态、未知键、De Morgan 补集 |
| `tests/test_policy_import_graph.py` | 6 | import 白名单、增量运行期断言、时间/IO 扫描 |
| `tests/test_policy_service.py` | 14 | 载入、覆盖合并、坏行降级、入账、预演不落库、预算默认值 |
| `tests/test_policy_api.py` | 3 | 路由已挂载、请求体无租户、响应体含 `policy_version` |

---

## 10. 待确认

> 编号接全局清单。本 spec 新发现 4 条（1–4 与设计 §10.8–§10.10 对应）。

| # | 事项 | 建议 | 验证方式 |
|---|---|---|---|
| **1** | `p3.readonly_probe` 的工具白名单与 `harness/tools.py` 注册表是两份声明（`engines/` 不能 import `harness/`，单向依赖决定了两份必然存在） | `harness/tools.py` 给只读探查类工具打 `risk=P3` 标记，be-11 构造 `ToolIntent` 时**从注册表读**，`kind` 不允许调用方自报——白名单只剩一份 | be-11 落地时比对两处清单 |
| **2** | 越权覆盖的告警每次判定都重复入账，长期留在库里会把账本刷满 | 按 `(policy_version, warning_code)` 去重（Redis `SETNX` + 长 TTL），或改为在策略提案/审批时入账一次 | 造一条被拒覆盖 + 连续两次门禁，断言只有一条事件 |
| **3** | `policy_rule` 缺 `stage`；`risk_policy_version` 缺 `status` / `submitted_by` / `submitted_at` / `reject_comment`；`effective_scope` 需为 `{project_ids, envs}` | 进字典 §4.6 + 迁移后实现策略管理面（M4）。**在此之前不实现这些接口**，避免出现「接受了一个过滤参数但忽略它」的静默错误 | 前端 `types/governance.ts` 的字段逐条对表 |
| **4** | `SensitiveColumn.kind` 的词表（8 个取值）需与 be-04 的脱敏词表一致，不一致的后果是「身份证明文出域」漏判 | be-04 定稿后做一次词表比对，必要时把 `kind` 改为引用 be-04 的类型枚举 | 两处词表逐值比对（设计 §10.3） |
| **5** | `policy_rule` 无 `tenant_id`（全局表），而 `risk_policy_version` 是租户级 | 单租户部署下按「全局安全基线」理解；加列并把基线下沉为代码常量（DB 只存覆盖） | M4 演示双租户后决定（设计 §10.1） |
| **6** | Commit 时「P0 单向加严」与「策略变更不追溯」存在张力 | 需 owner 拍板一次（设计 §10.6） | be-13 落地前确认 |

---

## 11. 变更记录

| 日期 | 变更 | 原因 |
|---|---|---|
| 2026-10-16 | 初版：be-07 引擎 + 条件适配 + service 载入 + `preview` 接口落地 | M2 第一个模块；设计文档 §3.1 / §6.2 / §6.3 / §6.4 / §6.7 已同步修正 |
