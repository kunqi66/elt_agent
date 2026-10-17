"""Pipeline 生成链路 DTO（be-09 / be-10 / be-11 / fe-04）。

**契约以 ``frontend/src/api/pipeline.ts`` 与 ``frontend/src/types/pipeline.ts`` 为准**——
与 ``api/datasources.py`` 是同一条先例。前端 Studio 已按这套字段渲染，后端改名不会报错，
只会让界面静默显示空白。

## 差异登记（前端 / service 契约 vs 本文件）

- **门禁四型**（``GateResult`` / ``GateFinding`` / ``GateLocator`` / ``AutofixRound``）与
  **Plan 体**（``EtlPlanBody``）直接 import ``engines`` 里的同名模型，不在本文件重抄一份：
  两份定义 = 两个真相，字段漂移时不会有任何报错。
- ``EtlPlan.validation_errors`` 前端是 ``{path, reason}``，而 ``engines.plan.schema.PlanIssue``
  是 ``{path, code, message, severity}``——**形状不同**，service 必须显式映射一次，
  不能把 PlanIssue 直接塞进 ``EtlPlanRead``。
- ``EtlPlan.plan`` 内层会多出一个 ``schema_version``（``EtlPlanModel`` 自带）：前端按 TS
  结构取用，多一个不读的键无害；换来「落库形态 = API 形态 = 指纹输入」三处一致。
- ``ClarificationQuestion.meta`` 可空，等价于 TS 的 ``?``；``hint`` 同理可为 null。
- ``answers`` 请求体在前端是 ``{answers: Record<string,string>}``（映射），service 要的却是
  ``list[AnswerInput]``——由 ``AnswersRequest.as_inputs()`` 转换，只做形状归一，不含判断。
- ``abortGeneration`` 的 ``reason``：service 的 ``abort_generation`` **签名不收它**，
  该字段目前只为兼容请求体而存在，值会被丢弃。
- ``PipelineFilter.status`` 前端是数组，线上是逗号分隔的 query 串，由路由拆分并逐个校验
  （未知取值回 422，不静默丢掉一个筛选项）。

## 红线

本模块里没有任何字段能承载明文 Secret（CLAUDE.md §5.2）。HOCON 制品里的
``"${VAULT_xxx}"`` 是**占位符**，其值只存在于 worker 内存。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from engines.gate.types import AutofixRound, GateFinding, GateLocator, GateResult
from engines.plan.schema import EtlPlanModel
from models.enums import PipelineStatus

# service 契约里用的是这两个名字。**是别名而不是子类**——子类会立刻变成第二份定义，
# 而门禁字段漂移时不会有任何报错，那正是本模块要杜绝的事。
GateResultRead = GateResult
AutofixRoundRead = AutofixRound

_DTO = ConfigDict(extra="forbid")


# ---------------------------------------------------------------- Studio 会话

#: 页面临时阶段（**不落库**），与前端 ``StudioPhase`` 逐字一致。
#: 持久状态一律用 ``PipelineStatus``，二者映射由后端给，前端不自行推导。
StudioPhase = Literal[
    "idle",
    "parsing",
    "awaiting_clarification",
    "generating",
    "gate_running",
    "gate_failed",
    "ready_to_freeze",
    "frozen",
    "submitted",
    "error",
]

ClarificationKind = Literal["text", "single_choice", "multi_choice", "field_ref", "threshold"]


class ClarificationOption(BaseModel):
    model_config = _DTO

    value: str = Field(min_length=1, max_length=128)
    label: str = Field(min_length=1, max_length=256)


class ClarificationMeta(BaseModel):
    """``threshold`` 型问题的取值范围。

    三个字段都可缺省：前端在缺省时按 §6.1 兜底并标注「范围待确认」，
    后端把「不知道」表达成缺省比编一个 0 更诚实。
    """

    model_config = _DTO

    min: float | None = None
    max: float | None = None
    unit: str | None = None
    ratio: bool | None = None


class ClarificationQuestion(BaseModel):
    """澄清问题。``key`` 是**回填答案时的主键**（见 :class:`AnswerInput`）。"""

    model_config = _DTO

    key: str = Field(min_length=1, max_length=64)
    question: str = Field(min_length=1, max_length=1024)
    kind: ClarificationKind
    # 非选择题时为 null；空数组与 null 是两回事（后者表示「这类问题没有选项」）
    options: list[ClarificationOption] | None = None
    required: bool = True
    hint: str | None = Field(default=None, max_length=512)
    meta: ClarificationMeta | None = None


class WorkflowSnapshot(BaseModel):
    """生成工作流快照——**Studio 的单一状态来源**。

    带 ``thread_id`` 是刻意的：刷新页面后前端只能靠它把会话捞回来，
    所以**每一个**生成相关接口的响应都必须带上它，包括发起与中断。
    """

    model_config = _DTO

    thread_id: str = Field(min_length=1, max_length=128)
    phase: StudioPhase
    phase_message: str | None = Field(default=None, max_length=512)
    pending_questions: list[ClarificationQuestion] = Field(default_factory=list)
    autofix_round: int = Field(default=0, ge=0)
    last_error: str | None = Field(default=None, max_length=1024)


# ---------------------------------------------------------------- 请求体


class StartGenerationRequest(BaseModel):
    """发起生成。

    ``project_id`` 由前端在 body 里补上（``{...payload, project_id}``），
    与 query 参数不同——它是**写目标**，放 body 才能被幂等中间件的请求指纹覆盖。
    """

    model_config = _DTO

    project_id: int = Field(ge=1)
    # 前端 textarea 的 maxlength 是 2000；这里只做「非空 + 不超长」，
    # 不照抄前端的「至少 5 个字符」——那是 UX 提示，不是接口约束
    requirement: str = Field(min_length=1, max_length=2000)
    source_data_source_id: int = Field(ge=1)
    target_data_source_id: int = Field(ge=1)


class AnswerInput(BaseModel):
    """一条澄清回答。

    ``question_id`` 即 :attr:`ClarificationQuestion.key`。**这是给 service 用的形态**；
    线上的形态是 ``AnswersRequest`` 里的映射（见模块 docstring 的差异表）。
    """

    model_config = _DTO

    question_id: str = Field(min_length=1, max_length=64)
    value: str = Field(max_length=2000)


class AnswersRequest(BaseModel):
    """澄清回答的**线上形态**：``{"answers": {"<question_key>": "<值>"}}``。

    前端直接序列化一个 ``Record<string,string>``，所以这里不能声明成数组——
    改了形状就是 422，而前端只会看到「参数校验失败」。
    """

    model_config = _DTO

    answers: dict[str, str] = Field(default_factory=dict)

    def as_inputs(self) -> list[AnswerInput]:
        """映射 → service 要的列表。只做形状归一，不解释 ``value`` 的含义。"""
        return [AnswerInput(question_id=key, value=value) for key, value in self.answers.items()]


class AbortRequest(BaseModel):
    """主动中断生成。

    ``reason`` 目前**被 service 丢弃**（``abort_generation`` 的签名没有这个入参）：
    它存在只是为了不让前端多传一个键就吃 422。待 service 补齐入参后再接上——
    见模块 docstring 的差异表。
    """

    model_config = _DTO

    reason: str = Field(default="", max_length=512)


# ---------------------------------------------------------------- Pipeline


class PipelineRead(BaseModel):
    """流水线摘要。``status`` 的 16 个取值与 ``frontend/src/types/pipeline.ts`` 逐字一致。"""

    model_config = _DTO

    id: int
    project_id: int
    name: str
    description: str = ""
    status: PipelineStatus
    current_version_id: int | None = None
    source_data_source_id: int | None = None
    target_data_source_id: int | None = None
    created_at: datetime
    updated_at: datetime


class PipelineFilter(BaseModel):
    """列表筛选。

    ``status`` 是**数组**（前端的多选），线上则是逗号分隔的 query 串，
    由路由 ``_parse_statuses`` 拆分后构造本模型——未知取值在那里变成 422，
    而不是被静默忽略：少显示一批失败流水线，用户会以为「没有失败过」。
    """

    model_config = _DTO

    status: list[PipelineStatus] | None = None
    q: str | None = Field(default=None, max_length=128)
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=200)


# ---------------------------------------------------------------- 制品


class PlanValidationError(BaseModel):
    """Plan 结构校验的一条问题。

    ``reason`` 是**给人看的文案**，与 ``engines.plan.schema.PlanIssue.message`` 对齐；
    ``code`` / ``severity`` 不在前端契约里，若前端日后要按子码分类再补字段。
    """

    model_config = _DTO

    path: str = Field(min_length=1, max_length=512)
    reason: str = Field(min_length=1, max_length=1024)


class EtlPlanRead(BaseModel):
    """``GET /pipeline-versions/{id}/plan`` 的响应。

    DAG 在 ``plan.plan.dag`` 里，**不是独立接口**（前端如此约定）。
    """

    model_config = _DTO

    id: int
    # 宽度与 ORM 列一致（``etl_plan.schema_version`` 是 varchar(64)）：
    # DTO 卡得比库窄，就是把「存得下」变成「读不出来」，而报错会出现在
    # 读接口上、指向一个数据库里明明存着的值。
    schema_version: str = Field(min_length=1, max_length=64)
    plan: EtlPlanModel
    valid: bool
    validation_errors: list[PlanValidationError] = Field(default_factory=list)


class HoconArtifactRead(BaseModel):
    """SeaTunnel HOCON 制品。

    ``content`` 里只可能出现 ``"${VAULT_...}"`` 形式的**占位符**，
    真值由 worker 在执行前从 Vault 物化进内存（CLAUDE.md §5.2）。
    """

    model_config = _DTO

    id: int
    content: str
    sha256: str = Field(min_length=1, max_length=64)
    # ``pyhocon+elt-render-1`` = 20 字符。这堵墙在库列放宽之后又立了一次：
    # 同一个值，DB 装得下、DTO 读不出，症状是「拉 HOCON 制品 422」。
    parser_version: str = Field(min_length=1, max_length=64)
    validation_result: dict[str, Any] = Field(default_factory=dict)


__all__ = [
    "AbortRequest",
    "AnswerInput",
    "AnswersRequest",
    "AutofixRound",
    "AutofixRoundRead",
    "ClarificationKind",
    "ClarificationMeta",
    "ClarificationOption",
    "ClarificationQuestion",
    "EtlPlanRead",
    "GateFinding",
    "GateLocator",
    "GateResult",
    "GateResultRead",
    "HoconArtifactRead",
    "PipelineFilter",
    "PipelineRead",
    "PipelineStatus",
    "PlanValidationError",
    "StartGenerationRequest",
    "StudioPhase",
    "WorkflowSnapshot",
]
