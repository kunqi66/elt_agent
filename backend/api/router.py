"""API 路由聚合。

分组与 PRD §9.2 一一对应；每个分组一个模块，模块内只做「校验 → 权限 → 调 service → 组装响应」。
M1 阶段先挂载已完成的模块，未完成的以注释占位，避免误以为已实现。
"""

from __future__ import annotations

from fastapi import APIRouter

api_router = APIRouter()

# ---------------- 已实现（M1）----------------
from api.auth import router as auth_router  # noqa: E402
from api.datasources import router as datasources_router  # noqa: E402
from api.governance import router as governance_router  # noqa: E402
from api.pipeline_versions import router as pipeline_versions_router  # noqa: E402
from api.pipelines import router as pipelines_router  # noqa: E402
from api.policy import router as policy_router  # noqa: E402
from api.projects import router as projects_router  # noqa: E402

# 前缀写在各自模块的 APIRouter(prefix=...) 里，此处**不再叠加**——
# 叠加会变成 /api/v1/auth/auth/login，而前端请求的是 /auth/login。
api_router.include_router(auth_router)
api_router.include_router(projects_router)
api_router.include_router(datasources_router)
api_router.include_router(governance_router)
api_router.include_router(policy_router)

# M2 生成链路：两个模块按前端的两组路径拆分（/pipelines 与 /pipeline-versions）。
# 挂载顺序无关紧要——各自的前缀互不重叠，`/pipeline-versions/diff` 的优先级
# 约束在 api/pipeline_versions.py 内部解决（见那里的注释）。
api_router.include_router(pipelines_router)
api_router.include_router(pipeline_versions_router)

# M3 治理链路：准备单与四眼审批（be-13）。`/preparations/{id}/commit`（签发 Capability
# 并投递执行）归 be-14，届时扩 api/runs.py 而不是塞进本模块——一个是审批面、
# 一个是执行面，混在一起会让「谁有权批准」与「谁有权执行」的判据长在同一棵代码树上。
from api.preparations import router as preparations_router  # noqa: E402

api_router.include_router(preparations_router)

# M3 执行面（be-14）：Commit / 运行列表与详情 / pause / cancel。
# 回滚路由（``/runs/{id}/rollback``）届时挂在同一前缀下，但实现转发 be-18 的
# ``services.publish.request_rollback``——回滚的编排与 ``target.rollback_cleanup``
# 都归 be-18，在这里重复实现一份只会得到两套「什么时候允许回滚」的判据。
from api.runs import router as runs_router  # noqa: E402

api_router.include_router(runs_router)

# M3 质量面（be-17）：``/runs/{id}/quality-report`` 与 ``/runs/{id}/error-records``
# （前端运行详情轮询）**外加**设计 §3.1 的 ``/monitor/runs/{id}/...``。
# 一个模块挂两组前缀，是因为它们读的是同一份报告，只是调用方不同（见 api/monitor.py 的模块注释）；
# 拆成两个文件会让「同一份报告的两种投影」变成两处实现。
from api.monitor import router as monitor_router  # noqa: E402

api_router.include_router(monitor_router)

# ---------------- 待实现（按里程碑）----------------
# 策略规则的 CRUD、策略版本的提交/审批、预算默认值与 Benchmark（M4，与账本同前缀
# /governance）届时扩 api/policy.py 或另开 api/policy_admin.py，同样不塞进
# governance.py——账本是只读审计面，策略是判定面，混在一起会变成巨文件
