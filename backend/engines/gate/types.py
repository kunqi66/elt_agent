"""门禁的类型契约（be-11 §3.1 / §4）。

**字段名与取值域逐字对齐 `frontend/src/types/pipeline.ts`**（第 264-306 行），
不按设计文档的写法——前端门禁页已经按这套字段渲染，改字段名等于改前端。
与 be-11 的差异登记在 :data:`GateCategory` 与 :data:`GateConclusion` 的注释里。

三条纪律：

1. **`GateLocator` 是扁平的可辨识联合**，不是「一个对象带多种可选字段」。
   前端要靠 ``locator.kind`` 决定跳去 HOCON 编辑器的某一行、还是 Plan 表单的某个字段，
   所以每种位置是**一个**独立形状，`kind` 是判别键（Pydantic 侧用 discriminator 强制）。
2. **`message` 必须已脱敏**：门禁的文案会被前端原样渲染，
   而 finding 的来源里有列名、表达式等可能含样值的内容 → 由
   :func:`engines.gate.checks.base.safe_message` 统一出口，业务代码不得自行拼。
3. **`finding.id` 必须确定性**：``"{check_id}.{code}.{locator_key}"``。
   自动修复靠它做去抖（``frozenset((check_id, code, locator_key))``，be-11 §6.6 规则 4），
   带随机数或时间戳的 id 会让「同一问题修不掉」被判成「新问题」，于是无限重试。
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

#: 五类检查的固定执行序（be-11 §5.2）：``policy → syntax → schema → mapping → resource``。
#: 字典序**不是**执行序，所以显式写出来，避免有人 ``sorted()`` 一下就把短路语义改了。
GateCheckId = Literal["policy", "syntax", "schema", "mapping", "resource"]

#: 执行序的唯一定义。门禁与自动修复都从这里取，不许各处再排一遍。
CHECK_ORDER: tuple[GateCheckId, ...] = ("policy", "syntax", "schema", "mapping", "resource")

#: 前端 ``GateCategory``。注意检查 id 叫 ``mapping``（be-11 用语）而**类别**叫
#: ``field_mapping``（前端用语，因为 ``mapping`` 在别处会被当成 dict/映射）。
#: 两者只差这一处，用 :func:`check_id_to_category` 单向转换，别在调用点手写字符串。
GateCategory = Literal["syntax", "schema", "field_mapping", "resource", "policy"]

#: 前端 ``GateConclusion``：三档，且**没有** ``passed/blocked`` 这种长拼写。
GateConclusion = Literal["pass", "warn", "block"]

#: 严重度。``info`` **不参与**结论合成（be-11 §5.1）：它只是给人看的提示，
#: 若让 info 参与，一条「建议加注释」就能把结论从 pass 顶成 warn，门禁会失去意义。
GateSeverity = Literal["block", "warn", "info"]

_SEVERITY_RANK: dict[str, int] = {"info": 0, "warn": 1, "block": 2}
_CONCLUSION_BY_RANK: dict[int, GateConclusion] = {0: "pass", 1: "warn", 2: "block"}


def check_id_to_category(check_id: GateCheckId) -> GateCategory:
    """``mapping`` → ``field_mapping``，其余同名。"""
    return "field_mapping" if check_id == "mapping" else check_id


# ---------------------------------------------------------------- 定位


class HoconLocator(BaseModel):
    """HOCON 文本里的位置。``line``/``column`` 从 1 起（与 CodeMirror 一致）。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["hocon"] = "hocon"
    line: int = Field(ge=1)
    column: int = Field(ge=1)
    end_line: int | None = Field(default=None, ge=1)


class PlanLocator(BaseModel):
    """Plan 内的 JSON 路径，如 ``/field_mappings/3/target_field``。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["plan"] = "plan"
    path: str = Field(min_length=1)


class DagNodeLocator(BaseModel):
    """拓扑节点。前端会在地图上高亮该节点。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["dag_node"] = "dag_node"
    node_id: str = Field(min_length=1)


class SourceTableLocator(BaseModel):
    """源端某张表。用于「表不存在 / 越界访问」这类问题。

    ``schema`` 是保留名 → 字段叫 ``schema_name`` 并挂别名（同 ``PlanInput``）。
    """

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    kind: Literal["source_table"] = "source_table"
    data_source_id: int = Field(ge=1)
    schema_name: str = Field(default="", alias="schema")
    table: str = Field(default="", max_length=128)


GateLocator = Annotated[
    HoconLocator | PlanLocator | DagNodeLocator | SourceTableLocator,
    Field(discriminator="kind"),
]


# ---------------------------------------------------------------- 结论


class GateFinding(BaseModel):
    """一条问题。

    ``suggestion`` 允许 ``None``（前端契约是 ``string | null``）：
    「这里有问题」和「我知道怎么改」是两件事，把后者硬编成空串会让人以为后者也存在。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1)
    severity: GateSeverity
    category: GateCategory
    code: str = Field(min_length=1, max_length=64)
    message: str = Field(min_length=1, max_length=512)
    locator: GateLocator
    suggestion: str | None = Field(default=None, max_length=512)

    @property
    def locator_key(self) -> str:
        """去抖用的稳定键：同一次生成的同一处问题，键相同。"""
        loc = self.locator
        if isinstance(loc, HoconLocator):
            return f"hocon:{loc.line}:{loc.column}"
        if isinstance(loc, PlanLocator):
            return f"plan:{loc.path}"
        if isinstance(loc, DagNodeLocator):
            return f"dag_node:{loc.node_id}"
        return f"source_table:{loc.data_source_id}:{loc.schema_name}.{loc.table}"


class AutofixRound(BaseModel):
    """一轮自动修复的记录（前端 ``AutofixRound``）。

    ``hocon_diff`` / ``plan_diff`` 是 **unified diff 文本**——存文本而不是存两个版本对象，
    因为它的用途是「给人看这一轮改了什么」，而人看 diff 比看两份 JSON 快得多。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    round: int = Field(ge=1)
    trigger_finding_ids: list[str] = Field(default_factory=list)
    hocon_diff: str = ""
    plan_diff: str | None = None
    resolved_finding_ids: list[str] = Field(default_factory=list)
    created_at: datetime


class GateResult(BaseModel):
    """门禁结论。

    ``evaluated_at`` 由**调用方注入**（``evaluate(..., now=...)``）：
    engines 层不许自己取时间——否则同一份制品在不同时刻门禁结论不同，
    「校验过」这件事就无法复现（be-11 §7 硬约束）。

    ``autofix_rounds`` 是**数组**不是计数：前端要渲染每一轮的 diff。
    ``autofix_exhausted`` 单独给一个布尔，因为「三轮用完了还没修好」
    与「一轮没用就通过了」在 ``len(rounds)`` 上无法区分（都是 3 vs 0，
    但语义是「还有救」与「没救了」）。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    evaluated_at: datetime
    conclusion: GateConclusion
    findings: list[GateFinding] = Field(default_factory=list)
    autofix_rounds: list[AutofixRound] = Field(default_factory=list)
    autofix_exhausted: bool = False

    @property
    def blocking(self) -> list[GateFinding]:
        """阻塞项。``policy`` 阻塞时自动修复必须停手（be-11 §6.6 规则 2）。"""
        return [f for f in self.findings if f.severity == "block"]

    @property
    def has_policy_block(self) -> bool:
        return any(f.severity == "block" and f.category == "policy" for f in self.findings)


def compose_conclusion(findings: list[GateFinding]) -> GateConclusion:
    """结论合成：**阻塞 > 警告 > 通过**，``info`` 不影响结论。

    写成 ``max(severity)`` 而不是「有 block 就 block，否则有 warn 就 warn」：
    后者漏掉 ``info`` 那一档时看着一样，但加第四档严重度时会静默错。
    """
    rank = max((_SEVERITY_RANK[f.severity] for f in findings), default=0)
    return _CONCLUSION_BY_RANK[rank]


def finding_id(check_id: GateCheckId, code: str, locator_key: str) -> str:
    """确定性 finding id。见模块 docstring 第 3 条。"""
    return f"{check_id}.{code}.{locator_key}"


def make_finding(
    *,
    check_id: GateCheckId,
    code: str,
    severity: GateSeverity,
    message: str,
    locator: GateLocator,
    suggestion: str | None = None,
) -> GateFinding:
    """构造 finding 的**唯一出口**——id 与 category 由 check_id 派生，调用方不许自己拼。"""
    probe = GateFinding(
        id="probe",
        severity=severity,
        category=check_id_to_category(check_id),
        code=code,
        message=message,
        locator=locator,
        suggestion=suggestion,
    )
    return probe.model_copy(update={"id": finding_id(check_id, code, probe.locator_key)})


def as_json(result: GateResult) -> dict[str, Any]:
    """给落库/API 用的序列化出口（``schema`` 别名与 ``datetime`` 一并处理）。"""
    return result.model_dump(mode="json", by_alias=True, exclude_none=False)
