"""运行实例 DTO（be-14 / fe-06）。

契约以 ``frontend/src/types/run.ts`` 为准（前端是消费者）。

两条与 be-14 设计文档不同的、**刻意**的取舍：

- ``RunSummary`` **不含** ``capability`` / ``token`` 任何形态的字段。
  Capability 令牌只在两个地方存在：Outbox 载荷（Worker 从中取出，用于验签执行）
  与 Worker 内存。控制面的任何响应里出现它，等于把「一次性授权」变成
  「任何能看到这次运行详情的人都能拿到的一张凭证」。
- ``RunDetail.failure_snapshot`` 是**已脱敏**摘要（错误码计数、指标点、错误类型名），
  由 be-16 写入时就脱敏过一次，这里再套一层 ``extra="ignore"``：
  写的人多一个键，读的人不该整页 422（见 CLAUDE.md §9「同一份语义两处组装」那条）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from models.enums import Env, RunStatus

#: 读 DTO：库里/服务里的超集字段安静丢弃。
_READ = ConfigDict(extra="ignore")
#: 写 DTO：多传一个键就是调用方对契约的误解，立刻 422。
_WRITE = ConfigDict(extra="forbid")


class RunCreateRequest(BaseModel):
    """Commit 请求：只收 ``preparation_id``。

    ``Idempotency-Key`` 走请求头（``api/idempotency.py`` 的中间件统一处理），
    不接受 body 里再传一份——同一个语义两处入口，客户端必然会用其中一个，
    而服务端无法知道哪个才是「客户端认为生效的那个」。
    """

    model_config = _WRITE

    preparation_id: int = Field(ge=1)


class RunSummary(BaseModel):
    """运行列表项。"""

    model_config = _READ

    id: int
    project_id: int
    pipeline_id: int
    pipeline_version_id: int
    version_no: int
    preparation_id: int
    status: RunStatus
    shadow_table: str | None = None
    formal_table: str | None = None
    error_table: str | None = None
    seatunnel_job_id: str | None = None
    triggered_by: int | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime


class RunMetrics(BaseModel):
    """累计计数快照。前端用相邻两点做差算瞬时速率（见 be-16）。"""

    model_config = _READ

    read_rows: int = 0
    written_rows: int = 0
    written_bytes: int = 0
    reject_rows: int = 0
    avg_throughput: float = 0


class RunDetail(BaseModel):
    """运行详情。``can_cancel`` / ``can_rollback`` 由服务端算好下发。

    前端自算等于把「这个动作现在合不合法」变成一次客户端判断，
    而服务端的转移表才是唯一事实来源（CLAUDE.md §5.8 的同一道理）。
    """

    model_config = _READ

    id: int
    project_id: int
    pipeline_id: int
    pipeline_version_id: int
    version_no: int
    preparation_id: int
    capability_id: int | None = None
    status: RunStatus
    shadow_table: str | None = None
    formal_table: str | None = None
    error_table: str | None = None
    seatunnel_job_id: str | None = None
    triggered_by: int | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime
    metrics: RunMetrics
    #: 已脱敏的失败现场（错误码计数 / 指标点 / 错误类型名），不含 HOCON 片段与连接信息
    failure_snapshot: dict[str, Any] = Field(default_factory=dict)
    error_message: str | None = None
    can_cancel: bool = False
    can_rollback: bool = False


class RunActionRequest(BaseModel):
    """操作员动作（pause / cancel / rollback）的请求体。"""

    model_config = _WRITE

    reason: str = Field(default="", max_length=255)


class RunSubmitContext(BaseModel):
    """Worker 提交作业前需要的全部事实（``services/run.load_for_submit`` 的返回）。

    **不是 HTTP DTO**：它只在 worker 进程内流转，跨的是 ``tasks`` → ``services``
    这一步。做成 Pydantic 是因为这一包里含 ``hocon_content`` 与 ``secret_refs``——
    两者都是「只准在这一个进程里存在」的东西，用 dict 传递时没有任何地方能声明这一点。

    ``secret_refs`` 装的是**引用**（Vault 路径 + 版本 + 字段名），不是凭据值；
    物化归 be-15，物化出来的值只进临时文件，不进本 DTO。
    """

    model_config = ConfigDict(extra="ignore")

    run_id: int
    tenant_id: int
    project_id: int
    preparation_id: int
    pipeline_id: int
    pipeline_version_id: int
    version_no: int
    artifact_sha256: str
    environment: Env
    hocon_artifact_id: int
    hocon_content: str
    shadow_table: str
    formal_table: str
    error_table: str
    secret_refs: tuple[dict[str, Any], ...] = ()
    supervision_contract_id: int | None = None
    quality_contract_id: int | None = None


class RunSyncResult(BaseModel):
    """「把一次运行推进到结论」这一趟的结果（``services.finalize.sync_run_outcome``）。

    **不是 HTTP DTO**：它跨的是 ``tasks`` → ``services`` 这一层，用来让 beat 的日志
    一行说清「这一轮对这条运行做了什么」。做成 Pydantic 是因为调用方（任务体）
    要把它 ``model_dump`` 进日志，而 dict 没有任何地方能声明它有哪些键。
    """

    model_config = _READ

    run_id: int
    #: 收尾**之后**的状态（不是收尾前）。
    status: RunStatus
    #: 本次实际做了什么。取值集合是**闭的**，任务体与用例都按它分支：
    #: ``none``（无需推进）/ ``quality_finalized``（出了质量报告）/
    #: ``published``（报告合格 → 原子发布完成）/ ``failed``（数据面失败或无主体）/
    #: ``orphaned``（数据面查不到这次作业，状态**不动**，留给人看）。
    action: str
    report_id: int | None = None
    passed: bool | None = None
    blocked_reason: str | None = None
    published: bool = False
    detail: str = ""
