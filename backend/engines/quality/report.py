"""质量报告合成与 ``passed`` 判定（be-17 §6.4 / §6.5）。

本模块是**纯计算**：计数从目标端查好了传进来，这里只算比率、分布、诊断与结论。
判定的全部依据都在这一处，因为 ``passed`` 是「能不能发布」的**唯一**依据——
be-18 的发布门禁只看它（``harness.ToolBroker._check_quality`` 会再核一遍）。

两条纪律：

- ``reject_rate`` 的**分母口径唯一**：``rejected / (valid + rejected)``。
  监督采样、前端展示、报告、预算判定全都用这一处的结果，禁止任何一方改用
  SeaTunnel 的自报指标再算一遍（两个数会在「影子表还差几行没写完」的时刻不一致）。
- ``suggestion`` 由 rule_type 映射到固定文案表，**不由 LLM 生成**：
  诊断结论必须可复现，同一条规则今天与明天要给同一个建议。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal, DivisionByZero, InvalidOperation
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from engines.quality.contract import HARD_BLOCK_CODES, CompiledRule, QualityContractSpec

#: ``diagnostics`` 的结构版本。be-19 与前端按它分支，改结构必须升版本。
DIAGNOSTICS_VERSION: Final[str] = "v1"

#: 前端只展示 Top-5（be-17 §6.6），完整分布仍在 ``error_code_dist`` 里。
TOP_ERROR_CODES: Final[int] = 5

#: 每条规则最多留几条样本（be-17 §6.5）。
MAX_SAMPLES_PER_RULE: Final[int] = 3

#: 比率的定标：4 位小数，与 ``quality_report.reject_rate`` 的 ``numeric(6,4)`` 同宽。
RATE_QUANT: Final[Decimal] = Decimal("0.0001")

#: 「不过」的原因。**枚举不扩**：中断由 ``interrupted`` 字段单独表达
#: （be-17 §7：中断的报告 ``passed=False``，``diagnostics.interrupted=true``），
#: 硬阻断与超预算各有自己的原因码。多造一个 ``"interrupted"`` 会让
#: 「原因码」这个枚举失去「与预算判断一一对应」的性质。
BlockedReason = Literal["hard_block_code", "over_budget", "zero_routed_rows"]

#: 规则类型 → 固定建议文案。
SUGGESTIONS: Final[Mapping[str, str]] = {
    "not_null": "源端出现空值：确认该字段是否允许为空，或补一条默认值 transform 再收紧规则",
    "regex": "源端存在不符合格式的值：确认放宽正则，还是补一条清洗 transform 再收紧规则",
    "range": "源端存在越界值：确认业务上下界是否变化，或把该字段的异常值纳入清洗范围",
    "enum": "源端出现了枚举之外的取值：确认是否新增了状态码（同步更新规则），或该字段本就不该做枚举约束",
    "type_cast": "源端存在无法解释为目标类型的值：确认源表列类型是否漂移，或补一条显式转换 transform",
}


class QualityDiagnosticSample(BaseModel):
    """一条诊断样本。**只有摘要、长度与原因模板，没有任何原值**（be-17 §6.5）。"""

    model_config = ConfigDict(extra="ignore")

    payload_digest: str = ""
    lengths: list[int] = Field(default_factory=list)
    reason: str = ""


class QualityDiagnosticRule(BaseModel):
    """规则级诊断。``warn`` 与 ``reject`` 都在这里，用 ``on_violation`` 区分。"""

    model_config = ConfigDict(extra="ignore")

    rule_id: str
    field: str
    rule_type: str
    on_violation: str
    error_code: str
    hits: int = 0
    #: 占比：reject 规则的分母是**拒绝行数**（与 §6.5 示例的算术一致），
    #: warn 规则的分母是**处理行数**（它命中的行照常进影子表，不是「拒绝」的一部分）。
    share: float = 0.0
    suggestion: str = ""
    samples: list[QualityDiagnosticSample] = Field(default_factory=list)


class QualityDiagnosticCode(BaseModel):
    model_config = ConfigDict(extra="ignore")

    code: str
    count: int
    share: float = 0.0


class QualityDiagnostics(BaseModel):
    """``quality_report.diagnostics`` 存的整份结构（be-17 §6.5，供 be-19 与前端）。

    相对 §6.5 的结构多了两个键：``failed_rules`` 与 ``contract_sha256``。
    ``quality_report`` 里没有对应的列（表结构已存在，不能改），而**读接口必须能还原
    这两项**——``failed_rules`` 是 be-18 的拒绝理由，``contract_sha256`` 是 be-19 做
    版本对比的锚点。塞进 ``diagnostics`` 是唯一不需要改表的位置。
    它们是**超集键**（``version`` 仍是 ``v1``，消费方按 ``extra="ignore"`` 容忍多键）。
    """

    model_config = ConfigDict(extra="ignore")

    version: str = DIAGNOSTICS_VERSION
    blocked_reason: str | None = None
    interrupted: bool = False
    warnings: list[str] = Field(default_factory=list)
    top_error_codes: list[QualityDiagnosticCode] = Field(default_factory=list)
    rules: list[QualityDiagnosticRule] = Field(default_factory=list)
    #: 命中的 reject 规则 id（按声明序）。与 ``QualityReportDTO.failed_rules`` 同值。
    failed_rules: list[str] = Field(default_factory=list)
    contract_sha256: str = ""
    #: 预算也存进来：``quality_report`` 没有这一列，不存的话**读接口就算不出**
    #: 「预算用了多少」（``budget_used_ratio``），只能回一个看起来像「预算为 0」的假值。
    reject_rate_budget: float = 0.0
    generated_at: datetime


class QualityDiagnosticItem(BaseModel):
    """前端契约里的 ``diagnostics[*]``（``frontend/src/types/run.ts`` 的数组形状）。

    前端在 ``vue-tsc`` 下按这个形状编译，字段名写成 ``summary`` 而不是 ``suggestion``；
    设计与前端的差异用「同一份诊断两处投影」消化：DB 与 be-19 看
    :class:`QualityDiagnostics`，前端看这一份。两份都由本模块产出，不会各写一套。
    """

    model_config = ConfigDict(extra="ignore")

    rule_id: str
    field: str
    error_code: str
    summary: str


class QualityReportDTO(BaseModel):
    """一份质量报告的全部读模型。

    ``report_id`` 是相对 be-17 §3.1 的**追加**：be-18 的
    ``TargetPublishSwapArgs.quality_report_id`` 是必填正整数，而 §3.1 的响应里
    只有 ``run_id``——发布方拿到 run_id 还得再查一次库才能凑出发参。
    ``failed_rules`` 同理：be-18 的回滚理由与账本要写「哪几条规则没过」。
    """

    model_config = ConfigDict(extra="ignore")

    #: ``quality_report.id``。be-18 直接用它填 ``TargetPublishSwapArgs.quality_report_id``。
    report_id: int = 0
    run_id: int
    valid_rows: int = 0
    rejected_rows: int = 0
    #: ``valid + rejected``。与监督侧同源：两处都查目标端，不采信作业自报。
    processed_rows: int = 0
    #: 源端读到的行数（来自``execution_run.read_rows``）：只为识别「整批行无故消失」。
    read_rows: int = 0
    reject_rate: float = 0.0
    reject_rate_budget: float = 0.0
    #: ``reject_rate / budget``；预算为 0 时无意义，回 ``None``（前端按「无预算」显示）。
    budget_used_ratio: float | None = None
    #: **能不能发布的唯一依据**。
    passed: bool = False
    blocked_reason: str | None = None
    #: 命中的 reject 规则 id（按声明序）。be-18 用它写回滚理由与账本。
    failed_rules: list[str] = Field(default_factory=list)
    error_code_dist: dict[str, int] = Field(default_factory=dict)
    rule_hits: dict[str, int] = Field(default_factory=dict)
    #: 前端契约形状（数组）
    diagnostics: list[QualityDiagnosticItem] = Field(default_factory=list)
    #: 设计 §6.5 形状（对象）。与上面是同一份数据的两种投影，be-19 读这一份。
    diagnostics_detail: QualityDiagnostics | None = None
    contract_sha256: str = ""
    #: 作业是被硬中断结束的（be-16 超限）。此时 ``passed`` 恒为 False。
    interrupted: bool = False
    generated_at: datetime


def reject_rate_of(rejected_rows: int, processed_rows: int) -> Decimal:
    """拒绝率：``rejected / processed``，4 位小数、四舍五入；``processed == 0`` → 0。

    用 ``Decimal`` 而不是二进制浮点：两侧同定标（4 位小数）才能让
    「等于预算」这类边界判断稳定——``0.05 <= 0.05`` 在 float 上偶尔会是假。
    """
    if processed_rows <= 0:
        return Decimal("0.0000")
    try:
        ratio = Decimal(rejected_rows) / Decimal(processed_rows)
    except (DivisionByZero, InvalidOperation):  # pragma: no cover - 上面已经挡住 0
        return Decimal("0.0000")
    return ratio.quantize(RATE_QUANT, rounding=ROUND_HALF_UP)


def budget_used_ratio_of(reject_rate: float | Decimal, budget: float | Decimal) -> float | None:
    """``reject_rate / budget``，4 位定标；预算为 0（或负）时回 ``None``。

    单独成一个函数，是因为它有两个消费方：合成报告时算一次、
    从库里**回读**报告时再算一次（这一项没有列）。两处各写一遍的话，
    「同一份报告，第一次读与第二次读的占比不同」这种差异没人查得出来。
    回 ``None`` 而不是 0 或 1：预算为 0 时这个比值没有意义，
    前端按「无预算」显示，而不是显示一个 0%（看起来像"预算还剩很多"）。
    """
    budget_dec = Decimal(str(budget))
    if budget_dec <= 0:
        return None
    return float((Decimal(str(reject_rate)) / budget_dec).quantize(RATE_QUANT, rounding=ROUND_HALF_UP))


def format_rate(value: float | Decimal) -> str:
    """比率 → 定标文本（账本 payload 用）。

    账本的 ``payload`` 不允许出现 float（哈希要可复现），所以比率以字符串入账；
    这里用定标 4 位而不是 ``str(float)``：``str(0.158)`` 是 ``'0.158'``，
    而 ``str(0.1 + 0.2)`` 是 ``'0.30000000000000004'``——同一个语义两个文本，
    链上就会看到两个不一样的值。
    """
    return f"{Decimal(str(value)):.4f}"


def decide_passed(
    reject_rate: Decimal,
    error_code_dist: Mapping[str, int],
    contract: QualityContractSpec,
    *,
    processed_rows: int | None = None,
    read_rows: int | None = None,
) -> tuple[bool, str | None]:
    """``(passed, blocked_reason)``（be-17 §3.2 / §6.4）。

    两条**同时**成立才算通过：

    1. ``reject_rate <= budget``——**等于预算算通过**（预算即闭区间上界）；
    2. ``error_code_dist`` 与 ``hard_block_codes`` 交集为空——零容忍码与拒绝率无关，
       敏感字段明文出域（P0）哪怕只有一行也不许发布。

    三个边界（顺序即优先级，先判的更严重）：

    - ``processed == 0`` 且 ``read_rows > 0``：源端读到了行，分流却一行都没落——
      整批数据在数据面丢了。此时拒绝率是 0、硬阻断码也没有，**只看数字会判通过**，
      所以这条必须排在前面。
    - 硬阻断码命中。
    - 超预算。

    两个关键字参数是相对 §3.2 的追加（带默认值，三个位置参数的调用形态不变）：
    没有它们就无法表达上面第一条边界。
    """
    if processed_rows is not None and processed_rows <= 0 and (read_rows or 0) > 0:
        return False, "zero_routed_rows"

    hard_block_values = {code.value for code in HARD_BLOCK_CODES}
    hit = sorted(
        str(code) for code, count in error_code_dist.items() if count and str(code) in hard_block_values
    )
    if hit:
        return False, "hard_block_code"

    if reject_rate > contract.budget:
        return False, "over_budget"

    return True, None


def build_report(
    run_id: int,
    *,
    valid_rows: int,
    rejected_rows: int,
    read_rows: int,
    error_code_dist: Mapping[str, int],
    rule_hits: Mapping[str, int],
    contract: QualityContractSpec,
    rule_index: Mapping[str, CompiledRule],
    interrupted: bool,
    generated_at: datetime,
    samples: Mapping[str, Sequence[QualityDiagnosticSample]] | None = None,
    contract_sha256: str = "",
    warnings: Sequence[str] = (),
) -> QualityReportDTO:
    """合成一份报告（be-17 §3.2）。

    三个关键字参数是相对 §3.2 的追加（带默认值，原调用形态不变）：

    - ``samples``：样本要从目标端读（``ErrorTableReader.fetch_digest_samples``），
      而 §3.2 的签名里没有 IO 位置。不传就是没有样本，诊断结构本身仍然完整。
    - ``contract_sha256``：取自 ``CompiledRules.contract_sha256``，**不在这里重算**——
      重算的话「报告里的锚点」与「门禁/指纹用的锚点」是两次独立计算，
      哪天规则归一化逻辑变了，两份锚点会悄悄分叉。
    - ``warnings``：调用方掌握而本模块看不到的事实（例如控制面错误明细副本被截断）。
      允许注入而不是让调用方事后改 ``diagnostics``，是为了让**警告只有一个产地**。
    """
    processed_rows = valid_rows + rejected_rows
    rate = reject_rate_of(rejected_rows, processed_rows)
    passed, blocked_reason = decide_passed(
        rate,
        error_code_dist,
        contract,
        processed_rows=processed_rows,
        read_rows=read_rows,
    )
    if interrupted:
        # 被硬中断的作业一律不可发布（be-17 §5 状态机）。原因码**不**另造一个：
        # 中断往往正是「先超了拒绝率」触发的，那个原因比「被中断了」更接近事实。
        passed = False

    notices: list[str] = []
    if read_rows == 0:
        # 空源不视为失败（be-17 §6.4）。注意 §10 第 7 条：空影子表做 Swap 会清空正式表，
        # 产品上是否允许「空源发布」尚未拍板，这里按 §6.4 的现行算法放行并留警告。
        notices.append("no_rows")
    if interrupted:
        notices.append("interrupted")
    if processed_rows == 0 and read_rows > 0:
        notices.append("zero_routed_rows")
    # 调用方注入的警告排在后面并去重：由本模块算出的三条永远在最前，顺序稳定；
    # 同一份事实（例如截断）被两处记过时只留一条，前端不必去重。
    for outer in warnings:
        if outer and outer not in notices:
            notices.append(outer)

    budget = contract.budget
    used_ratio = budget_used_ratio_of(rate, budget)
    failed_rules = [
        rule.rule_id
        for rule in sorted(rule_index.values(), key=lambda item: item.order)
        if rule.on_violation == "reject" and int(rule_hits.get(rule.rule_id, 0)) > 0
    ]

    detail = build_diagnostics(
        contract=contract,
        rule_index=rule_index,
        error_code_dist=error_code_dist,
        rule_hits=rule_hits,
        valid_rows=valid_rows,
        rejected_rows=rejected_rows,
        samples=samples,
        blocked_reason=blocked_reason,
        interrupted=interrupted,
        warnings=notices,
        failed_rules=failed_rules,
        contract_sha256=contract_sha256,
        generated_at=generated_at,
    )

    return QualityReportDTO(
        run_id=run_id,
        valid_rows=valid_rows,
        rejected_rows=rejected_rows,
        processed_rows=processed_rows,
        read_rows=read_rows,
        reject_rate=float(rate),
        reject_rate_budget=float(budget),
        budget_used_ratio=used_ratio,
        passed=passed,
        blocked_reason=blocked_reason,
        failed_rules=failed_rules,
        error_code_dist={str(k): int(v) for k, v in error_code_dist.items() if int(v)},
        rule_hits={str(k): int(v) for k, v in rule_hits.items() if int(v)},
        diagnostics=build_diagnostic_items(detail),
        diagnostics_detail=detail,
        contract_sha256=contract_sha256,
        interrupted=interrupted,
        generated_at=generated_at,
    )


def build_diagnostics(
    *,
    contract: QualityContractSpec,
    rule_index: Mapping[str, CompiledRule],
    error_code_dist: Mapping[str, int],
    rule_hits: Mapping[str, int],
    valid_rows: int,
    rejected_rows: int,
    samples: Mapping[str, Sequence[QualityDiagnosticSample]] | None,
    blocked_reason: str | None,
    interrupted: bool,
    warnings: Sequence[str],
    generated_at: datetime,
    failed_rules: Sequence[str] = (),
    contract_sha256: str = "",
) -> QualityDiagnostics:
    """``diagnostics`` 的整份结构（be-17 §6.5）。"""
    processed_rows = valid_rows + rejected_rows
    return QualityDiagnostics(
        version=DIAGNOSTICS_VERSION,
        blocked_reason=blocked_reason,
        interrupted=interrupted,
        warnings=list(warnings),
        top_error_codes=_top_codes(error_code_dist, rejected_rows),
        rules=_rule_diagnostics(rule_index, rule_hits, processed_rows, rejected_rows, samples),
        failed_rules=list(failed_rules),
        contract_sha256=contract_sha256,
        reject_rate_budget=float(contract.budget),
        generated_at=generated_at,
    )


def _top_codes(error_code_dist: Mapping[str, int], rejected_rows: int) -> list[QualityDiagnosticCode]:
    ranked = sorted(
        ((str(code), int(count)) for code, count in error_code_dist.items() if int(count)),
        key=lambda item: (-item[1], item[0]),
    )[:TOP_ERROR_CODES]
    return [
        QualityDiagnosticCode(code=code, count=count, share=_share(count, rejected_rows))
        for code, count in ranked
    ]


def _rule_diagnostics(
    rule_index: Mapping[str, CompiledRule],
    rule_hits: Mapping[str, int],
    processed_rows: int,
    rejected_rows: int,
    samples: Mapping[str, Sequence[QualityDiagnosticSample]] | None,
) -> list[QualityDiagnosticRule]:
    denominator_of = {"reject": rejected_rows, "warn": processed_rows}
    out: list[QualityDiagnosticRule] = []
    for rule in sorted(rule_index.values(), key=lambda item: item.order):
        hits = int(rule_hits.get(rule.rule_id, 0))
        prepared = list((samples or {}).get(rule.rule_id, ()))[:MAX_SAMPLES_PER_RULE]
        out.append(
            QualityDiagnosticRule(
                rule_id=rule.rule_id,
                field=rule.field,
                rule_type=rule.rule_type,
                on_violation=rule.on_violation,
                error_code=rule.error_code.value,
                hits=hits,
                share=_share(hits, denominator_of.get(rule.on_violation, rejected_rows)),
                suggestion=SUGGESTIONS.get(rule.rule_type, ""),
                samples=prepared,
            )
        )
    return out


def _share(count: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return float(round(count / denominator, 4))


def build_diagnostic_items(detail: QualityDiagnostics) -> list[QualityDiagnosticItem]:
    """``QualityDiagnostics`` → 前端数组（``summary`` 由计数与原因模板拼出，**不含值**）。"""
    items: list[QualityDiagnosticItem] = []
    for rule in detail.rules:
        reason = rule.samples[0].reason if rule.samples else ""
        scope = "违规行" if rule.on_violation == "reject" else "通过行"
        pieces = [
            f"{rule.error_code} 命中 {rule.hits} 行（占{scope} {rule.share:.2%}）",
        ]
        if reason:
            pieces.append(f"判据：{reason}")
        if rule.suggestion:
            pieces.append(rule.suggestion)
        items.append(
            QualityDiagnosticItem(
                rule_id=rule.rule_id,
                field=rule.field,
                error_code=rule.error_code,
                summary="；".join(pieces),
            )
        )
    return items
