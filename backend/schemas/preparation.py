"""准备单与审批 DTO（be-13 / fe-05）。

**契约以 ``frontend/src/types/approval.ts`` 与 ``frontend/src/api/preparation.ts`` 为准。**
前端是这些字段的消费者：少一个字段就是页面上一个空白格，而多一个字段不会被任何人发现。
因此本模块的**读 DTO 一律 ``extra="ignore"``**——库里那份 JSON 列（``resource_scope`` /
``rollback_plan``）存的比前端要的多，给 be-14 留了执行所需的字段（影子表名、凭据引用），
多出来的键在这里被安静丢掉，而不是让整个详情接口 422。

差异登记（与 ``schemas/version.py`` 同一口径）：

- 前端的 ``ResourceScope`` **只有 reads / writes**，而库里那份还含 ``secrets``
  （``secret_reference`` 的 path / version / key_names）。响应只回前两项：
  凭据引用是 be-14 物化时用的，前端拿到也没有用途，而**少传一份凭据清单就少一处泄露面**
  （CLAUDE.md §5.2）。库列里留着它，是为了让审批人能看到「这次要动哪些凭据」——
  那由详情页的另一个区块呈现，不走这个字段。
- 前端的 ``RollbackPlan`` 是「步骤数组 + 两个开关」，而 be-13 §5.1 写的是
  ``{shadow_table, formal_table, cleanup, restore}``。以**前端为准**（它是消费者），
  两个表名作为**额外键**留在库列里给 be-14，读 DTO 不含它们。
- ``risk_reasons``：be-13 §5.1 写的是 PDP 的 ``decision.reasons``（一串人话），
  前端要的是 ``{code, level, title, detail}`` 四元组。**以 PDP 命中项为准**：
  ``RuleHit`` 本来就有前三个字段，``detail`` 由 ``evidence`` 拼；
  只回 ``reasons`` 字符串会让前端无法把理由链回规则编号，等于把审计线索丢掉。
- ``PreparationSummary.my_slot`` / ``can_commit``：**服务端算**，不接受前端传。
  前端 docstring 也写明「一律取后端」——这两个值决定了按钮显示与否，
  前端自算是把「能不能批」变成一次客户端判断（CLAUDE.md §5.8）。
- 时间线 ``timeline``：由证据账本事件 + ``approval_decision`` 行拼成，
  没有独立的事件表。``committed`` 事件由 be-14 落账（``run.created``），
  本模块的时间线在 Commit 之前**不会**出现它。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from models.enums import ApprovalDecisionType, ApprovalSlot, Env, PreparationStatus, RiskLevel, SensitiveLevel

#: 读 DTO：库里/服务里的超集字段安静丢弃。
_READ = ConfigDict(extra="ignore")
#: 写 DTO：多传一个键就是调用方对契约的误解，立刻 422。
_WRITE = ConfigDict(extra="forbid")


# ---------------------------------------------------------------- 影响范围


class ResourceReadRef(BaseModel):
    """读的一张表。``database`` + ``table`` 是定位，**不是**结构快照。"""

    model_config = _READ

    data_source_id: int
    database: str
    table: str


class ResourceWriteRef(BaseModel):
    """写的一张表。

    ``mode`` 目前恒为 ``shadow_then_swap``（PRD §15 已冻结）：
    先写影子表、质量过关再原子改名的模式是本系统唯一允许的写形态，
    直接写正式表在 PDP 里是一条 P0。把它做成字面量而不是可选值，
    是为了让「哪天多了一种写模式」变成一次显式的类型改动，而不是一个悄悄生效的新值。
    """

    model_config = _READ

    data_source_id: int
    database: str
    table: str
    mode: Literal["shadow_then_swap"] = "shadow_then_swap"


class ResourceScopeRead(BaseModel):
    model_config = _READ

    reads: tuple[ResourceReadRef, ...] = ()
    writes: tuple[ResourceWriteRef, ...] = ()


# ---------------------------------------------------------------- 预算与回滚


class ExecutionBudgetRead(BaseModel):
    """生效预算：六项上限，审批人照着它判断「这次动作允许多大」。

    比率字段在引擎侧是 ``Decimal``（精确比较），JSON 序列化后会变成**字符串**；
    前端做算术前要先 ``Number()``（与 ``schemas/version.py`` 里同一处坑）。
    """

    model_config = _READ

    max_read_rows: int
    max_write_bytes: int
    max_duration_sec: int
    max_amplification_ratio: Any
    max_reject_rate: Any
    warn_threshold_pct: int


class RollbackStep(BaseModel):
    model_config = _READ

    order: int
    action: str
    tool: str
    description: str = ""


class RollbackPlanRead(BaseModel):
    model_config = _READ

    steps: tuple[RollbackStep, ...] = ()
    shadow_cleanup: bool = True
    restore_scope: str = ""


# ---------------------------------------------------------------- 风险理由


class RiskReasonRead(BaseModel):
    """PDP 的一条命中。``level`` 是该规则自身的档位，不是本单的最终评级。"""

    model_config = _READ

    code: str
    level: RiskLevel
    title: str
    detail: str = ""


# ---------------------------------------------------------------- 审批决定


class ApprovalDecisionRead(BaseModel):
    model_config = _READ

    slot: ApprovalSlot
    approver_id: int
    approver_name: str
    approver_role: str
    decision: ApprovalDecisionType
    comment: str
    artifact_sha256: str
    fingerprint_at_decision: str
    decided_at: datetime
    #: 当前输入指纹 ≠ 审批那一刻的指纹 → 这条决定已失效（不物理删除，只是不再作数）
    stale: bool = False


# ---------------------------------------------------------------- 时间线


TimelineEvent = Literal[
    "created",
    "approved_slot",
    "rejected_slot",
    "returned_slot",
    "expired",
    "superseded",
    "committed",
]


class PreparationTimelineItem(BaseModel):
    model_config = _READ

    at: datetime
    event: TimelineEvent
    slot: ApprovalSlot | None = None
    actor_name: str | None = None
    detail: str | None = None


# ---------------------------------------------------------------- 准备单


class PreparationSummary(BaseModel):
    model_config = _READ

    id: int
    project_id: int
    pipeline_id: int
    pipeline_name: str
    pipeline_version_id: int
    version_no: int
    artifact_sha256: str
    input_fingerprint: str
    risk_level: RiskLevel
    sensitive_level: SensitiveLevel
    environment: Env
    status: PreparationStatus
    required_slots: tuple[ApprovalSlot, ...] = ()
    decided_slots: tuple[ApprovalSlot, ...] = ()
    created_by: int
    creator_name: str
    frozen_at: datetime
    expires_at: datetime | None = None
    #: 当前用户可审的槽；``None`` = 不可审（不是「没资格看」）
    my_slot: ApprovalSlot | None = None
    #: 服务端算好的提交可见性：状态 approved + 未过期 + 指纹未变 + 必需槽齐
    can_commit: bool = False


class PreparationDetail(PreparationSummary):
    model_config = _READ

    resource_scope: ResourceScopeRead = ResourceScopeRead()
    budget: ExecutionBudgetRead | None = None
    rollback_plan: RollbackPlanRead = RollbackPlanRead()
    risk_reasons: tuple[RiskReasonRead, ...] = ()
    decisions: tuple[ApprovalDecisionRead, ...] = ()
    timeline: tuple[PreparationTimelineItem, ...] = ()


class PreparationStatusView(BaseModel):
    """轻量轮询：审批页轮询它而不是详情——详情要读四份制品 + 账本，代价高一个量级。"""

    model_config = _READ

    status: PreparationStatus
    approved_slots: tuple[ApprovalSlot, ...] = ()
    pending_slots: tuple[ApprovalSlot, ...] = ()
    expires_at: datetime | None = None
    committable: bool = False


# ---------------------------------------------------------------- 请求


class PreparationCreateRequest(BaseModel):
    """发起准备。**前端不得提交任何指纹字段**。

    ``artifact_sha256`` / ``input_fingerprint`` 必须由服务端从不可变版本重算——
    允许调用方提交等于让它自证清白，而这两个值正是审批与执行的信任锚。

    ``project_id`` 收下但**只用于一致性校验**：项目的唯一事实来源是
    ``pipeline_version → pipeline.project_id``，客户端说是哪个项目不算数。
    """

    model_config = _WRITE

    pipeline_version_id: int = Field(ge=1)
    project_id: int | None = Field(default=None, ge=1)
    note: str = Field(default="", max_length=512)


class DecisionRequest(BaseModel):
    """一条审批决定。

    ``comment`` 必填且非空：审批是**具名**结论，一句「同意」也比空字符串强——
    空意见会让事后复盘只能看到「谁批了」，看不到「他以为自己在批什么」。
    """

    model_config = _WRITE

    slot: ApprovalSlot
    decision: ApprovalDecisionType
    comment: str = Field(min_length=1, max_length=2000)


# ---------------------------------------------------------------- 跨模块 DTO


class CommittableFacts(BaseModel):
    """``assert_committable`` 的返回值：Commit 需要知道的全部事实。

    **不含任何 Secret 值**（只有 ``secret_reference`` 的路径与版本，在
    ``resource_scope`` 里）。be-14 用其中的 ``artifact_sha256`` / ``environment`` /
    ``preparation_id`` 签发 Capability——令牌绑定的是这三个事实，而不是「谁点的按钮」。

    入参用 id 而非 ORM 实例，返回值是 Pydantic：跨层只传 DTO（CLAUDE.md §4.6）。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    preparation_id: int
    pipeline_version_id: int
    pipeline_id: int
    project_id: int
    tenant_id: int
    environment: Env
    risk_level: RiskLevel
    sensitive_level: SensitiveLevel
    artifact_sha256: str
    input_fingerprint: str
    required_slots: tuple[ApprovalSlot, ...]
    #: 槽 → 审批人 user_id：责任追溯用（Capability 里不写人名）
    approvers: dict[ApprovalSlot, int]
    budget: dict[str, Any]
    resource_scope: dict[str, Any]
    expires_at: datetime | None = None


__all__ = [
    "ApprovalDecisionRead",
    "CommittableFacts",
    "DecisionRequest",
    "ExecutionBudgetRead",
    "PreparationCreateRequest",
    "PreparationDetail",
    "PreparationStatusView",
    "PreparationSummary",
    "PreparationTimelineItem",
    "ResourceReadRef",
    "ResourceScopeRead",
    "ResourceWriteRef",
    "RiskReasonRead",
    "RollbackPlanRead",
    "RollbackStep",
    "TimelineEvent",
]
