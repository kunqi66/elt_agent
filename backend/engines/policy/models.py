"""PDP 的输入输出 DTO（be-07 §3.2）。

**全部 frozen + extra=forbid**：
- ``frozen`` 保证 `decide()` 的输入在判定过程中不会被谁改掉——同输入必同输出的一半靠它；
- ``extra="forbid"`` 保证规则作者或调用方塞进来一个「看起来该生效」的新字段时**立刻报错**，
  而不是被静默忽略、然后在某次审计里被发现「这个字段从来没参与过判定」。

本模块与 `rules.py` / `pdp.py` 一样属**纯函数层**：只依赖 Pydantic、标准库与
`models.enums`（纯值定义，无 IO）。禁 import fastapi / sqlalchemy / httpx / core / clients…
（CI 由 `tests/test_policy_import_graph.py` 强制）。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator

from models.enums import (
    ApprovalSlot,
    DataSourceType,
    Env,
    RiskLevel,
    SensitiveLevel,
    ToolName,
)

_FROZEN = ConfigDict(frozen=True, extra="forbid")

# 敏感列的类型词表。**与 be-04 的脱敏词表必须一致**（be-07 §10.3）：
# 两边不一致的后果是「身份证明文出域」这种 P0 会因为 kind 拼写不同而漏判。
SensitiveKind = Literal[
    "id_card",
    "phone",
    "email",
    "address",
    "token",
    "bank_card",
    "name",
    "other",
]

# 命中 P0 的 kind 白名单：这些即使被标成 internal 也一律视为高敏（§6.3 P0-2）。
# 理由：分级是人工标注的结果，而「身份证是不是敏感」不是一道需要人来判断的题。
ALWAYS_SENSITIVE_KINDS: frozenset[str] = frozenset(
    {"id_card", "phone", "email", "address", "token", "bank_card"}
)

#: 脱敏引擎的类别 → 本模块（计划/PDP）的类别。
#:
#: **必须翻译，不能透传**：两个 ``SensitiveKind`` 是两套词表——
#: 脱敏那边按「用什么规则脱」分（``credential`` 走口令规则、``free_text`` 走泛化规则），
#: PDP 这边按「判定要多严」分（``token`` 进 P0 白名单、``other`` 不进）。
#: 直接 ``str(kind)`` 塞进来的后果不是判错档，而是 **Pydantic 校验直接炸**：
#: ``SensitiveColumn`` 是冻结模型，词表外的值会让整个请求在最后一步回 422
#: 「参数校验失败」——图上一切正常，只有响应构造失败，非常难查。
_MASKING_TO_POLICY_KIND: dict[str, str] = {
    "name": "name",
    "phone": "phone",
    "email": "email",
    "id_card": "id_card",
    "bank_card": "bank_card",
    "address": "address",
    # 凭据类在计划侧叫 token，而且它在 P0 白名单里——翻译错了等于放走一条 P0
    "credential": "token",
    # 长文本不是「已知的敏感类型」，只能归到兜底档；它**不在** P0 白名单里，
    # 所以这一档的判定强度依赖人工分级（``declared``），不能靠名字猜
    "free_text": "other",
}


def to_policy_kind(kind: Any) -> SensitiveKind | None:
    """把脱敏引擎判出的类别翻成计划/PDP 的类别；``None``/``none`` 翻成 ``None``。

    未知值一律落到 ``other``（而不是抛错）：调用方传进来的若是将来新增的脱敏类别，
    让它降级成「未分类」比让整条生成链路失败合理——降级只影响这一列的判定强度，
    而抛错影响的是「这次生成做不成」，且症状（422）与真因（词表不一致）隔得很远。
    ``tests/test_policy_kind_vocabulary.py`` 有一条用例守着「脱敏词表里每个成员都在映射表里」，
    所以真新增了类别时会有用例变红提醒补映射，而不是静默降级。
    """
    if kind is None:
        return None
    raw = getattr(kind, "value", kind)
    text = str(raw)
    if text == "none":
        return None
    mapped = _MASKING_TO_POLICY_KIND.get(text, "other")
    return cast("SensitiveKind", mapped)


class BudgetRequest(BaseModel):
    """申报预算。五项都是「本次作业最多允许多少」，不是「预计会用多少」。"""

    model_config = _FROZEN

    max_read_rows: int = Field(ge=0)
    max_write_bytes: int = Field(ge=0)
    max_duration_sec: int = Field(ge=0)
    max_amplification_ratio: Decimal = Field(ge=0)
    max_reject_rate: Decimal = Field(ge=0, le=1)


class BudgetDefaults(BaseModel):
    """生效预算的默认值（来自 `Settings.SUP_MAX_*`，由 service 注入）。"""

    model_config = _FROZEN

    max_read_rows: int = Field(ge=0)
    max_write_bytes: int = Field(ge=0)
    max_duration_sec: int = Field(ge=0)
    max_amplification_ratio: Decimal = Field(ge=0)
    max_reject_rate: Decimal = Field(ge=0, le=1)
    warn_threshold_pct: int = Field(default=80, ge=0, le=100)


class ToolIntent(BaseModel):
    """「想做什么」。生成阶段（be-09）还没有具体工具，`tool` 允许为 None。"""

    model_config = _FROZEN

    kind: Literal["etl_write", "readonly_probe", "export", "admin"]
    is_write: bool
    tool: ToolName | None = None
    # 需求摘要，**必须已脱敏**（PDP 自己不做脱敏，§10.7）。只进 reasons 与证据，不参与判定。
    description_summary: str = Field(default="", max_length=512)
    bypass_approval: bool = False
    disable_audit: bool = False
    export_plaintext_sensitive: bool = False
    cross_tenant: bool = False
    has_rollback_plan: bool = False
    budget: BudgetRequest | None = None


class ResourceRef(BaseModel):
    # ``populate_by_name``：``schema`` 是 SQL 关键字，Python 侧只能叫 ``schema_``，
    # 但对外的字段名必须仍是 ``schema``（前端与证据 payload 都用它）。
    # 两个名字都收，避免调用方因为「到底传哪个」而踩一次 422。
    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    data_source_id: int | None = None
    data_source_type: DataSourceType
    database: str
    schema_: str = Field(default="", alias="schema")
    table: str
    env: Env


class SensitiveColumn(BaseModel):
    """涉及字段的分级与**计划中的**脱敏动作。

    ``masked`` 说的是「计划做了掩码/哈希/截断」，不是「已经做了」——
    PDP 在生成阶段就要判定，那时还没有任何一行数据被处理过。
    """

    model_config = _FROZEN

    name: str
    level: SensitiveLevel
    kind: SensitiveKind | None = None
    contains_plaintext: bool
    masked: bool
    egress: bool


class ResourceScope(BaseModel):
    """「对哪些表做」。租户与项目是必填——跨租户本身就是一条 P0。"""

    model_config = _FROZEN

    tenant_id: int
    project_id: int
    reads: tuple[ResourceRef, ...] = ()
    writes: tuple[ResourceRef, ...] = ()
    authorized_databases: frozenset[str] = frozenset()
    out_of_scope: tuple[ResourceRef, ...] = ()
    cross_tenant_refs: tuple[ResourceRef, ...] = ()
    unregistered_secret_refs: tuple[int, ...] = ()
    writes_formal_table: bool = False
    writes_shadow_table: bool = False
    columns: tuple[SensitiveColumn, ...] = ()


class RuleSpec(BaseModel):
    """一条规则。``condition`` 由 `evaluate_condition` 求值。

    **构造时自动规整条件形式**（`condition.normalize_condition`）：
    DB 里存的是前端契约的**扁平**形式（`{"env": ["prod"], ...}`），
    而引擎求值用**规范 DSL**（`{"all": [{"feature": "env", "op": "in", ...}]}`）。
    在唯一的入口处转换一次，好过在每个调用点提醒自己「记得先编译」。

    ``condition={}`` 表示**恒真**（无条件规则）——这是合法形态，
    但发布策略时必须显式确认，否则一条空条件的 P0 会让全部作业被拦。
    """

    model_config = _FROZEN

    code: str = Field(min_length=1, max_length=64)
    level: RiskLevel
    title: str = Field(min_length=1, max_length=255)
    description: str = ""
    condition: dict[str, Any] = Field(default_factory=dict)
    required_slots: tuple[ApprovalSlot, ...] = ()
    enabled: bool = True
    version: int = Field(default=1, ge=1)

    @field_validator("condition")
    @classmethod
    def _normalize(cls, value: dict[str, Any]) -> dict[str, Any]:
        """规整形式 + **就地校验**。

        校验放在构造处而不是求值处：一条引用了不存在特征的条件，
        若等到判定时才抛，那是在**生产流量上**第一次执行到它的时候。
        在这里抛，坏规则在发布/载入阶段就被挡住（service 会把它降级成
        `RuleMergeWarning`），判定路径永远不会遇到它。
        """
        from engines.policy.condition import normalize_condition
        from engines.policy.rules import validate_condition

        normalised = normalize_condition(value)
        validate_condition(normalised)
        return normalised

    @property
    def is_baseline(self) -> bool:
        """基线规则（`p0.` / `p3.` 前缀）不可被配置关闭或降级（§6.6）。"""
        return self.code.startswith(("p0.", "p3."))


class RuleHit(BaseModel):
    """一条命中。``evidence`` 只含**非敏感**的特征名与字符串化取值。"""

    model_config = _FROZEN

    code: str
    level: RiskLevel
    title: str
    evidence: dict[str, str] = Field(default_factory=dict)


class PolicyDecision(BaseModel):
    model_config = _FROZEN

    level: RiskLevel
    blocked: bool
    required_slots: tuple[ApprovalSlot, ...]
    hardening_required: bool
    budget: BudgetRequest
    hits: tuple[RuleHit, ...]
    reasons: tuple[str, ...]
    policy_version: int
    evaluated_at: datetime


class RuleMergeWarning(BaseModel):
    """载入策略时被忽略的越权覆盖（§6.6）。service 必须把它写进证据账本。"""

    model_config = _FROZEN

    code: str
    reason: str
