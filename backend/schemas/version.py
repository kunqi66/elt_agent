"""不可变版本 DTO（be-12 / fe-04 版本与 Diff）。

**契约以 ``frontend/src/api/pipeline.ts`` 与 ``frontend/src/types/pipeline.ts`` 为准。**
差异登记（与 ``schemas/pipeline.py`` 同一口径）：

- 版本列表：前端要 ``Page<PipelineVersionSummary>``，路由用 ``core.response.page()`` 包信封
  —— 前端分页组件读的是 ``meta``，裸数组会让它渲染不出页码。
- 版本列表的 ``total``：service 的 ``list_versions`` 只回本页列表、不回总数，
  所以 ``meta.total`` 只能取**本页条数**。要准确翻页得先给 service 加一个计数返回值。
- ``freezeVersion`` 响应：service 返回 ``PipelineVersionDetail``（超集），而前端二次确认
  弹窗读的是**顶层** ``manifest`` 字段；路由取 ``detail.manifest`` 投影，否则整屏 undefined。
- ``fetchVersionDiff`` 响应：前端的 TS 声明只有 ``entries``，但它的 mock
  （``api/mock/fixtures.ts::versionDiff``）把 ``base_version_id`` / ``target_version_id``
  原样回显。审计场景里方向读反会得出相反结论，回显比让调用方自己记更安全；
  两个字段可空 → service 只给 ``entries`` 也合法。
- ``status_note``：DB 列叫 ``note``，两者之间由 service 映射；对外一律用前端口径。
- ``supervision`` 的比率字段在引擎模型里是 ``Decimal``（精确比较需要它），而前端标注
  ``number`` —— **JSON 里 Decimal 会序列化成字符串**，前端做算术前得先 ``Number()``。
- ``runAutofix`` 的 ``note``：service 的 ``run_autofix`` **签名不收它**，
  该字段只为兼容请求体存在，值会被丢弃。

**没有任何写接口能改一个已冻结的版本**（CLAUDE.md §5.4）：``pipeline_version``
及其四件制品只允许 INSERT，本模块的 DTO 里也就没有任何「更新版本」的入参。
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from engines.plan.schema import RuntimeBudget
from schemas.pipeline import EtlPlanRead, HoconArtifactRead

_DTO = ConfigDict(extra="forbid")


# ---------------------------------------------------------------- 生成上下文


class GenerationContext(BaseModel):
    """这份制品是「谁、哪个模型、哪个提示词模板、修了几轮」生成的。

    全部字段给了默认值：``pipeline_version.generation_context`` 的 DB 默认是 ``{}``，
    若在这里做成必填，一条早期版本就会让**整个列表接口** 500——
    缺元数据是「历史数据不全」，不是「请求失败」。

    **这里刻意不用 ``_DTO``（``extra="forbid"``）**：这一列存的是
    ``clients.llm.base.GenerationContext`` 的完整转储（比前端契约多出
    ``provider`` / ``prompt_versions`` / ``usage_total`` / ``generated_at``
    这些审计项）。DTO 是这份 JSON 的**视图**，视图比底表多一列就报错，
    等于「写进去的行读不出来」——症状出现在**读**的一侧，
    而真因在**写**的一侧多了一个键（早先就多了个 ``plan_issue_count``，
    表现为「生成成功、答题 422：Extra inputs are not permitted」）。
    未知键忽略即可：前端契约以外的东西不需要经过这个 DTO 才能活。
    """

    model_config = ConfigDict(extra="ignore")

    model_name: str = Field(default="", max_length=64)
    model_version: str = Field(default="", max_length=64)
    prompt_template_version: str = Field(default="", max_length=64)
    autofix_rounds: int = Field(default=0, ge=0)


# ---------------------------------------------------------------- 冻结清单


class ManifestPlan(BaseModel):
    model_config = _DTO

    # 宽度跟着 ORM 列走（``etl_plan.schema_version`` / ``hocon_artifact.parser_version``
    # 都是 varchar(64)）：DTO 卡得比库还窄，等于把「库能存下的值」变成
    # 「接口读不出来」——而版本串是自由格式，会随需求变长。
    schema_version: str = Field(min_length=1, max_length=64)
    valid: bool


class ManifestHocon(BaseModel):
    model_config = _DTO

    sha256: str = Field(min_length=1, max_length=64)
    # ``pyhocon+elt-render-1`` 长 20，早先这里限 16：DB 列放宽之后这一处成了新的墙，
    # 症状会出现在冻结（构造清单）而不是落库，排查方向完全不同。
    parser_version: str = Field(min_length=1, max_length=64)


class ManifestQualityContract(BaseModel):
    model_config = _DTO

    rule_count: int = Field(ge=0)
    reject_rate_budget: float = Field(ge=0, le=1)


class ManifestMetadataProfile(BaseModel):
    """冻结时锁定的元数据档案版本。``expires_at`` 可空 = 该档案不设有效期。"""

    model_config = _DTO

    version: int = Field(ge=1)
    sha256: str = Field(min_length=1, max_length=64)
    expires_at: datetime | None = None


class FreezeArtifactManifest(BaseModel):
    """冻结二次确认弹窗要展示的制品清单，与 ``pipeline_version`` 的外键一一对应。

    这是「冻结那一刻到底冻了什么」的**对外承诺**：前端拿它做二次确认，
    审计拿它核对指纹。因此五项都必填——少一项就意味着有个制品没被冻进去。
    """

    model_config = _DTO

    plan: ManifestPlan
    hocon: ManifestHocon
    quality_contract: ManifestQualityContract
    supervision: RuntimeBudget
    metadata_profile: ManifestMetadataProfile


# ---------------------------------------------------------------- 版本


class PipelineVersionSummary(BaseModel):
    """版本列表项：**只有摘要**。整份制品（plan / hocon）走详情接口。

    ``frozen_at`` / ``sha256`` 必填而不是可空：一行没有冻结时间与指纹的记录
    在语义上根本不是一个「已冻结版本」，让它通过校验只会把问题推到前端。
    """

    model_config = _DTO

    id: int
    version_no: int = Field(ge=1)
    sha256: str = Field(min_length=1, max_length=64)
    status_note: str = Field(default="", max_length=512)
    frozen_at: datetime
    frozen_by_display_name: str | None = None
    source_metadata_profile_version: int | None = None
    generation_context: GenerationContext = Field(default_factory=GenerationContext)


class PipelineVersionDetail(PipelineVersionSummary):
    """版本详情 = 摘要 + 清单 + 两份只读制品。"""

    model_config = _DTO

    manifest: FreezeArtifactManifest
    plan: EtlPlanRead
    hocon: HoconArtifactRead


# ---------------------------------------------------------------- 请求体


class FreezeRequest(BaseModel):
    """冻结请求。``note`` 缺省为空串——冻结是**必做动作**，不是「要不要写备注」。"""

    model_config = _DTO

    note: str = Field(default="", max_length=512)


class AutofixRequest(BaseModel):
    """手动触发一轮自动修复。

    ``note`` 目前**被 service 丢弃**（``run_autofix`` 的签名没有这个入参）：
    它存在只是为了不让前端多传一个键就吃 422。见模块 docstring 的差异表。
    """

    model_config = _DTO

    note: str = Field(default="", max_length=512)


# ---------------------------------------------------------------- Diff


class VersionDiffEntry(BaseModel):
    """一条差异。``before`` / ``after`` 的**缺省**就是语义的一部分：
    ``added`` 没有 ``before``，``removed`` 没有 ``after``。
    硬编成空串会让「原来就是空的」与「这条是新增的」看起来一模一样。
    """

    model_config = _DTO

    path: str = Field(min_length=1, max_length=512)
    kind: Literal["added", "removed", "changed"]
    before: str | None = None
    after: str | None = None


class VersionDiff(BaseModel):
    """版本差异。方向固定为 ``base → target``（base 旧、target 新）。

    两个可空的 id 字段是给审计用的回显：见模块 docstring 的差异表。
    """

    model_config = _DTO

    entries: list[VersionDiffEntry] = Field(default_factory=list)
    base_version_id: int | None = None
    target_version_id: int | None = None


__all__ = [
    "AutofixRequest",
    "FreezeArtifactManifest",
    "FreezeRequest",
    "GenerationContext",
    "ManifestHocon",
    "ManifestMetadataProfile",
    "ManifestPlan",
    "ManifestQualityContract",
    "PipelineVersionDetail",
    "PipelineVersionSummary",
    "VersionDiff",
    "VersionDiffEntry",
]
