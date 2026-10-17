"""质量契约编译（be-17 §6.1 / §6.2）。

``quality_contract.rules``（JSON）→ :class:`CompiledRules`：结构化规则 + 分流元数据 +
下推到数据面的 SQL 片段。**谓词语义恒为「True == 违规」**，这是整套分流的地基：

- ``_quality_route`` 只需要一个 ``CASE WHEN <任一 reject 谓词> THEN 'reject' ELSE 'valid' END``；
- ``_error_code`` / ``_rule_id`` 只需要按 ``order``（= ``rules`` 数组下标）取**第一条**命中的，
  于是「同一份契约 + 同一行数据」的结论是确定的，与谓词书写顺序无关。

为什么整块下推：控制面不搬运业务数据（《概要设计总纲》§1 铁律）。逐行判规则意味着
10⁸ 行数据要过 FastAPI 进程，既不现实，也把敏感原文带进了控制面。

纯函数层：只依赖 Pydantic + 标准库 + ``engines`` 内部模块，不碰 DB / Redis / 网络 / LLM。
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from typing import Any, Final, NamedTuple

from pydantic import BaseModel, ConfigDict, Field

from core.ids import canonical_json
from engines.plan.schema import EtlPlanModel
from engines.quality import dialects
from engines.quality.errors import ContractError
from models.enums import DataSourceType, ErrorCodeLabel

#: ``error_record.rule_id`` 与错误表 ``rule_id`` 列的宽度。规则 id 由 Plan 生成（可能很长），
#: 落库前必须收进列宽——超宽的症状是「数据面写进去了、控制面读不出来」
#: （SQLSTATE 22001，一个被本项目记录过三次的坑）。
MAX_RULE_ID: Final[int] = 64

#: 规则字段名 / 影子表列名的最大长度（``error_record.field_name`` 同宽）。
MAX_FIELD_LEN: Final[int] = 64

#: 注入面收敛（be-17 §8）：enum 值、SQL 里出现的标识符。
MAX_LITERAL_LEN: Final[int] = 128
MAX_PATTERN_LEN: Final[int] = 512
MAX_ENUM_VALUES: Final[int] = 200

#: ``error_record.reason`` / 错误表 ``reason`` 列的宽度。
MAX_REASON_LEN: Final[int] = 255

#: ``rule_type → 默认 error_code``。规则自带 ``error_code`` 时以它为准
#: （例如非主键上的 ``not_null`` 不该报 ``E_NULL_PK``）。
DEFAULT_ERROR_CODE: Final[Mapping[str, ErrorCodeLabel]] = {
    "not_null": ErrorCodeLabel.E_NULL_PK,
    "regex": ErrorCodeLabel.E_REGEX_MISMATCH,
    "range": ErrorCodeLabel.E_RANGE_VIOLATION,
    "enum": ErrorCodeLabel.E_ENUM_INVALID,
    "type_cast": ErrorCodeLabel.E_TYPE_CAST,
}

#: 零容忍错误码（be-17 §6.4）：与拒绝率**无关**，命中即 ``passed=False``。
#: ``E_SENSITIVE_PLAINTEXT`` = 敏感字段明文出域（风险矩阵 P0）；
#: ``E_SCHEMA_DRIFT`` = 源端结构与冻结版本不一致（契约本身失效）。
HARD_BLOCK_CODES: Final[frozenset[ErrorCodeLabel]] = frozenset(
    {ErrorCodeLabel.E_SENSITIVE_PLAINTEXT, ErrorCodeLabel.E_SCHEMA_DRIFT}
)

#: 目标端错误表的列清单（be-17 §4，与 ``error_record`` 对齐 + 一列 ``run_id``）。
ERROR_COLUMNS: Final[tuple[str, ...]] = (
    "run_id",
    "rule_id",
    "field_name",
    "error_code",
    "payload_digest",
    "reason",
    "occurred_at",
)

#: ``tagged`` 中间结果的名字。be-10 装配 HOCON 时 ``plugin_output`` 必须写同一个值——
#: 两个地方各写一次字符串，症状是「SQL 引用的表不存在」而作业已经跑了一半。
TAGGED_TABLE: Final[str] = "tagged"

VALID_ON_VIOLATION: Final[frozenset[str]] = frozenset({"reject", "warn"})
VALID_REGEX_EXPECT: Final[frozenset[str]] = frozenset({"match", "not_match"})

#: 字段名 / 列名：SQL 标识符，且不得超宽。**这是最要紧的一处注入面**——
#: ``field`` 也会被直接拼进 SQL，而它同样是 LLM 生成的。
_IDENTIFIER_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
#: 规则 id 允许的字符集（标识符 + ``.`` ``:`` ``-``，够用且拼进 SQL 字面量也安全）。
_RULE_ID_UNSAFE_RE: Final[re.Pattern[str]] = re.compile(r"[^A-Za-z0-9_.:-]")


# ---------------------------------------------------------------- 输入模型


class QualityRuleSpec(BaseModel):
    """``quality_contract.rules`` 的单个元素。

    取自 :func:`services.pipeline.parts.quality_contract_body`（``QualityRule`` 的
    ``model_dump(mode="json", by_alias=True)``），字段与 ``engines/plan/schema.py`` 一一对应：
    ``{id, field, rule_type, params, error_code, on_violation, enabled}``。

    ``extra="ignore"``：Plan 将来给规则加字段时，本模块不该因为多一个键就整份契约编译失败
    （「写的一侧多一个键、报在读的一侧」是本项目踩过的坑）。反过来，**缺字段**必须报错，
    走 :class:`ContractError` 的显式校验（不用 Pydantic 的 ValidationError，
    那样错误码会变成 422 而不是生成期的 GATE_BLOCKED）。
    """

    model_config = ConfigDict(extra="ignore")

    id: str | None = None
    field: str = ""
    rule_type: str = ""
    params: dict[str, Any] = Field(default_factory=dict)
    error_code: str | None = None
    on_violation: str = "reject"
    enabled: bool = True


class QualityContractSpec(BaseModel):
    """``quality_contract`` 一份契约的体。

    形状与 :func:`services.pipeline.parts.quality_body_from_row` 完全一致——
    **刻意不在这里再从 Plan 现推一份**：那样等于让库里那两行（本该不可变的制品）
    退出信任链，见 ``services/pipeline/parts.py`` 的模块 docstring。
    """

    model_config = ConfigDict(extra="ignore")

    rules: list[QualityRuleSpec] = Field(default_factory=list)
    error_table_target: dict[str, Any] = Field(default_factory=dict)
    reject_rate_budget: Decimal = Field(default=Decimal("0"))

    @property
    def budget(self) -> Decimal:
        """拒绝率预算。**等于预算算通过**（闭区间上界，be-17 §6.4）。"""
        return Decimal(self.reject_rate_budget)


class CompiledRule(BaseModel):
    """编译后的单条规则。``params`` 已归一化（enum 排序去重、range 边界转十进制文本）。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    rule_id: str
    field: str
    rule_type: str
    on_violation: str
    error_code: ErrorCodeLabel
    params: dict[str, Any] = Field(default_factory=dict)
    #: ``rules`` 数组下标。决定命中优先级与 ``_error_code`` 取值——**不是**排序序号。
    order: int


class CompiledRules(BaseModel):
    """编译产物。``frozen`` + 元组：它是「一次冻结版本的质量契约」的内存形态，
    运行期被多层读取，谁都不该改它。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    rules: tuple[CompiledRule, ...] = ()
    reject_rules: tuple[CompiledRule, ...] = ()
    warn_rules: tuple[CompiledRule, ...] = ()
    #: 影子表列清单。**显式**列出，不用 ``SELECT *``：``_quality_route`` / ``_error_code`` /
    #: ``etl_run_id`` 是内部列，一旦落进影子表，Swap 时的列一致性校验必然失败。
    valid_columns: tuple[str, ...] = ()
    error_columns: tuple[str, ...] = ERROR_COLUMNS
    #: ``sha256(canonical_json(rules))``：报告与 diff 的锚点。它只覆盖**规则**，
    #: 不覆盖列清单——列清单变了是 Plan 变了，那是另一条指纹（``pipeline_version.sha256``）的事。
    contract_sha256: str = ""
    hard_block_codes: frozenset[ErrorCodeLabel] = HARD_BLOCK_CODES

    def index(self) -> dict[str, CompiledRule]:
        """``rule_id → 规则``。传给 :func:`engines.quality.report.build_report` 用。"""
        return {rule.rule_id: rule for rule in self.rules}


# ---------------------------------------------------------------- 编译


def compile_contract(
    contract: QualityContractSpec,
    *,
    valid_columns: Sequence[str] = (),
    target_type: DataSourceType | None = None,
) -> CompiledRules:
    """契约 → 结构化规则 + 分流元数据（be-17 §3.2）。

    非法规则（enum 值含引号、``min > max``、regex 不可编译、``type_cast`` 目标类型未知）
    抛 :class:`ContractError`；生成期由门禁翻译成 ``GATE_BLOCKED``，版本不允许冻结。

    两个**关键字**参数是相对 §3.2 的追加（都带默认值，``compile_contract(spec)`` 的调用形态不变）：

    - ``valid_columns``：影子表列清单（= Plan 的映射目标字段 + 派生列，见 :func:`target_columns`）。
      给了就顺带校验每条规则的 ``field`` 在清单里——规则挂在结果集不存在的列上，
      渲染出的 SQL 只会在数据面报「列不存在」，那是作业跑到一半才发现的错。
    - ``target_type``：给了就顺带做方言可用性校验（目前只影响 ``type_cast`` 的形状表）。
      不传时谓词渲染阶段再按目标端校验，两处都报 :class:`ContractError`。
    """
    used_ids: set[str] = set()
    compiled: list[CompiledRule] = []
    for index, spec in enumerate(contract.rules):
        if not spec.enabled:
            # 关掉的规则**不编译**：留着它会让「前端看到的规则数」与
            # 「数据面真正执行的规则数」不一致，而两份数字都长得很有道理。
            continue
        compiled.append(_compile_rule(spec, index=index, used_ids=used_ids))

    columns = _check_columns(valid_columns)
    reject_rules = tuple(rule for rule in compiled if rule.on_violation == "reject")
    warn_rules = tuple(rule for rule in compiled if rule.on_violation == "warn")

    if columns:
        known = set(columns)
        for rule in compiled:
            if rule.field not in known:
                raise ContractError(
                    f"规则挂在列 {rule.field!r} 上，但它不在影子表列清单里"
                    f"（共 {len(columns)} 列）——渲染出的 SQL 会去引用一个不存在的列",
                    rule_index=rule.order,
                    rule_type=rule.rule_type,
                )

    return CompiledRules(
        rules=tuple(compiled),
        reject_rules=reject_rules,
        warn_rules=warn_rules,
        valid_columns=columns,
        contract_sha256=contract_sha256(compiled),
        hard_block_codes=HARD_BLOCK_CODES,
    )


def target_columns(plan: EtlPlanModel) -> tuple[str, ...]:
    """影子表列清单 = 映射的目标字段（声明序）+ 派生列，按首次出现去重。

    这是**全项目唯一的一份**「结果集有哪些列」定义：``valid_columns``、错误表摘要的
    ``canon`` 字段顺序、影子表 sink 的显式列清单都取自它。三处各推一次的症状是
    「摘要按 A 顺序算、sink 按 B 顺序写」——摘要对不上还不报错。
    """
    columns: list[str] = []
    for mapping in plan.field_mappings:
        if mapping.target_field and mapping.target_field not in columns:
            columns.append(mapping.target_field)
    for transform in plan.transforms:
        if transform.output_field and transform.output_field not in columns:
            columns.append(transform.output_field)
    return tuple(columns)


def contract_sha256(rules: Sequence[CompiledRule]) -> str:
    """规则集 → 契约指纹。归一化后计算，因此「同一份语义换个写法」指纹相同。"""
    payload = [rule.model_dump(mode="json") for rule in rules]
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def _compile_rule(spec: QualityRuleSpec, *, index: int, used_ids: set[str]) -> CompiledRule:
    rule_type = spec.rule_type.strip()
    if rule_type not in DEFAULT_ERROR_CODE:
        known = ", ".join(sorted(DEFAULT_ERROR_CODE))
        raise ContractError(
            f"未知的 rule_type：{rule_type!r}（已支持：{known}）",
            rule_index=index,
            rule_type=rule_type,
        )

    field = _check_field(spec.field, index=index, rule_type=rule_type)

    on_violation = spec.on_violation.strip()
    if on_violation not in VALID_ON_VIOLATION:
        raise ContractError(
            f"on_violation 只能是 reject / warn，收到 {on_violation!r}",
            rule_index=index,
            rule_type=rule_type,
        )

    error_code = _resolve_error_code(spec.error_code, rule_type=rule_type, index=index)
    params = _normalize_params(rule_type, spec.params, index=index)
    rule_id = _resolve_rule_id(spec, field=field, rule_type=rule_type, used_ids=used_ids)

    return CompiledRule(
        rule_id=rule_id,
        field=field,
        rule_type=rule_type,
        on_violation=on_violation,
        error_code=error_code,
        params=params,
        order=index,
    )


def _check_field(value: str, *, index: int, rule_type: str) -> str:
    field = value.strip()
    if not field:
        raise ContractError("规则缺少 field", rule_index=index, rule_type=rule_type)
    if not _IDENTIFIER_RE.match(field):
        raise ContractError(
            f"field 不是合法的 SQL 标识符（或超过 {MAX_FIELD_LEN} 字节）：{field!r}",
            rule_index=index,
            rule_type=rule_type,
        )
    return field


def _resolve_error_code(value: str | None, *, rule_type: str, index: int) -> ErrorCodeLabel:
    if value is None or not value.strip():
        return DEFAULT_ERROR_CODE[rule_type]
    code = value.strip()
    try:
        return ErrorCodeLabel(code)
    except ValueError as exc:
        known = ", ".join(label.value for label in ErrorCodeLabel)
        raise ContractError(
            f"error_code 不在冻结的错误码表里：{code!r}（可选：{known}）",
            rule_index=index,
            rule_type=rule_type,
        ) from exc


def _resolve_rule_id(spec: QualityRuleSpec, *, field: str, rule_type: str, used_ids: set[str]) -> str:
    """稳定标识：优先用 Plan 给的 ``id``，否则 ``f"{field}.{rule_type}"``。

    三条规则：① 字符集收进标识符安全集（它会拼进 SQL 字面量，也会落进
    ``error_record.rule_id``）；② 超宽截断 + 哈希后缀（收进列宽，且两个长 id 不会撞名）；
    ③ 同名冲突追加序号，保证同一份 ``rules`` 里 ``rule_id → 规则`` 是单射——
    否则 ``rule_hits`` 会把两条规则的命中数加在一起，而报告上看不出异常。
    """
    raw = (spec.id or "").strip() or f"{field}.{rule_type}"
    base = _RULE_ID_UNSAFE_RE.sub("_", raw) or f"{field}.{rule_type}"
    if len(base) > MAX_RULE_ID:
        digest = hashlib.sha256(base.encode("utf-8")).hexdigest()[:8]
        base = f"{base[: MAX_RULE_ID - len(digest) - 1]}_{digest}"
    candidate = base
    seq = 2
    while candidate in used_ids:
        candidate = f"{base}#{seq}"
        seq += 1
    used_ids.add(candidate)
    return candidate


def _normalize_params(rule_type: str, params: Mapping[str, Any], *, index: int) -> dict[str, Any]:
    """按 rule_type 归一化参数。

    归一化的意义不只是校验：``params`` 会进 ``contract_sha256`` 与 ``diagnostics``，
    把 ``0`` / ``0.0`` / ``"0"`` 收成同一个十进制文本，才能让「同一份契约」有两份
    一模一样的指纹——否则「重新生成一次得到不同指纹」会被当成制品被篡改。
    """
    if rule_type == "not_null":
        return {"allow_blank": _as_bool(params.get("allow_blank", False), index=index, name="allow_blank")}

    if rule_type == "regex":
        pattern = params.get("pattern")
        if not isinstance(pattern, str) or not pattern:
            raise ContractError("regex 规则缺少 pattern", rule_index=index, rule_type=rule_type)
        _check_literal(pattern, index=index, rule_type=rule_type, name="pattern")
        if len(pattern) > MAX_PATTERN_LEN:
            raise ContractError(
                f"pattern 超过 {MAX_PATTERN_LEN} 字符（len={len(pattern)}）",
                rule_index=index,
                rule_type=rule_type,
            )
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ContractError(
                f"pattern 不是可编译的正则：{exc}", rule_index=index, rule_type=rule_type
            ) from exc
        expect = str(params.get("expect") or "match").strip()
        if expect not in VALID_REGEX_EXPECT:
            raise ContractError(
                f"regex 的 expect 只能是 match / not_match，收到 {expect!r}",
                rule_index=index,
                rule_type=rule_type,
            )
        return {"pattern": pattern, "expect": expect}

    if rule_type == "range":
        lower = _as_decimal(params.get("min"), index=index, name="min")
        upper = _as_decimal(params.get("max"), index=index, name="max")
        if lower is None and upper is None:
            raise ContractError(
                "range 的 min 与 max 不能同时为空——两侧都无界的规则永远不命中，"
                "它只会让前端显示一条规则、数据面什么都不做",
                rule_index=index,
                rule_type=rule_type,
            )
        if lower is not None and upper is not None and lower > upper:
            raise ContractError(
                "range 的 min 大于 max：任何值都违规，拒绝率会直接顶到 1",
                rule_index=index,
                rule_type=rule_type,
            )
        return {
            "min": None if lower is None else _decimal_text(lower),
            "max": None if upper is None else _decimal_text(upper),
            "inclusive_min": _as_bool(params.get("inclusive_min", True), index=index, name="inclusive_min"),
            "inclusive_max": _as_bool(params.get("inclusive_max", False), index=index, name="inclusive_max"),
        }

    if rule_type == "enum":
        raw_values = params.get("values")
        if not isinstance(raw_values, (list, tuple)) or not raw_values:
            raise ContractError(
                "enum 规则缺少 values（或给了一个空集合：空集合意味着所有值都违规）",
                rule_index=index,
                rule_type=rule_type,
            )
        values: list[str] = []
        for item in raw_values:
            text = _as_enum_value(item, index=index, rule_type=rule_type)
            _check_literal(text, index=index, rule_type=rule_type, name="values")
            if text not in values:
                values.append(text)
        if len(values) > MAX_ENUM_VALUES:
            raise ContractError(
                f"enum 的取值超过 {MAX_ENUM_VALUES} 个（{len(values)}）——"
                "这不是枚举而是自由文本，请改用 regex",
                rule_index=index,
                rule_type=rule_type,
            )
        # 排序去重：同一份契约换个写法要产出**同一条 SQL 字面量**（SQL 可缓存、Diff 稳定）
        return {"values": sorted(values)}

    # type_cast
    target = params.get("target_type")
    if not isinstance(target, str) or not target.strip():
        raise ContractError("type_cast 规则缺少 target_type", rule_index=index, rule_type=rule_type)
    strict = _as_bool(params.get("strict", True), index=index, name="strict")
    # 形状表在这里就先查一遍：未知目标类型要在**生成期**报出来（GATE_BLOCKED），
    # 而不是等到渲染 SQL 时才发现——那时版本已经冻结了
    dialects.typecast_shape(target.strip(), strict=strict)
    return {"target_type": target.strip(), "strict": strict}


def _check_columns(columns: Sequence[str]) -> tuple[str, ...]:
    out: list[str] = []
    for column in columns:
        name = str(column).strip()
        if not _IDENTIFIER_RE.match(name):
            raise ContractError(f"列名不是合法的 SQL 标识符（或超过 {MAX_FIELD_LEN} 字节）：{name!r}")
        if name not in out:
            out.append(name)
    return tuple(out)


def _check_literal(value: str, *, index: int, rule_type: str, name: str) -> None:
    """注入面收敛（be-17 §8）：值不得含单引号或分号，且不得超长。"""
    if "'" in value or ";" in value:
        raise ContractError(
            f"{name} 含有单引号或分号——这两个字符会逃出 SQL 字面量",
            rule_index=index,
            rule_type=rule_type,
        )
    if len(value) > MAX_LITERAL_LEN:
        raise ContractError(
            f"{name} 超过 {MAX_LITERAL_LEN} 字符（len={len(value)}）",
            rule_index=index,
            rule_type=rule_type,
        )


def _as_bool(value: Any, *, index: int, name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes"}:
            return True
        if lowered in {"false", "0", "no", ""}:
            return False
    raise ContractError(f"{name} 需要布尔值，收到 {type(value).__name__}", rule_index=index, rule_type="")


def _as_decimal(value: Any, *, index: int, name: str) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ContractError(f"{name} 不能是布尔值", rule_index=index)
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ContractError(f"{name} 不是数字：{value!r}", rule_index=index) from exc
    if not parsed.is_finite():
        raise ContractError(f"{name} 不是有限数：{value!r}", rule_index=index)
    return parsed


def _decimal_text(value: Decimal) -> str:
    """十进制文本的**唯一**形态：``0`` / ``10.5`` / ``-3``（无指数、无多余零）。"""
    return format(value.normalize(), "f")


def _as_enum_value(value: Any, *, index: int, rule_type: str) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ContractError(
            f"enum 的取值只能是字符串或数字，收到 {type(value).__name__}",
            rule_index=index,
            rule_type=rule_type,
        )
    return str(value).strip()


# ---------------------------------------------------------------- 谓词渲染


def render_predicate(rule: CompiledRule, target_type: DataSourceType) -> str:
    """单条规则 → 「True == 违规」的布尔表达式（be-17 §6.1）。

    每种 rule_type 都先判 ``IS NOT NULL``（``not_null`` 自己除外）：SQL 的三值逻辑里
    ``NULL NOT REGEXP 'x'`` 是 NULL 而不是真，直接把它写进 ``CASE WHEN`` 会走 ELSE 分支，
    于是「没取到值」被静默判成合规。空值的处置归 ``not_null`` 规则，不归格式规则。
    """
    dialects.assert_supported(target_type)
    field = rule.field

    if rule.rule_type == "not_null":
        if rule.params.get("allow_blank"):
            return f"{field} IS NULL OR {dialects.blank_predicate(field)}"
        return f"{field} IS NULL"

    if rule.rule_type == "regex":
        pattern = str(rule.params.get("pattern", ""))
        if rule.params.get("expect") == "not_match":
            return f"{field} IS NOT NULL AND {dialects.regexp_match(field, pattern)}"
        return f"{field} IS NOT NULL AND {dialects.regexp_not_match(field, pattern)}"

    if rule.rule_type == "range":
        return f"{field} IS NOT NULL AND ({_range_core(rule, field)})"

    if rule.rule_type == "enum":
        values = [str(item) for item in rule.params.get("values", [])]
        literals = ", ".join(dialects.quote_literal(item) for item in values)
        return f"{field} IS NOT NULL AND {field} NOT IN ({literals})"

    if rule.rule_type == "type_cast":
        return f"{field} IS NOT NULL AND ({_typecast_core(rule, field, target_type)})"

    # 编译期已经把 rule_type 收进了五个取值，走到这里说明 CompiledRule 不是本模块造的
    raise ContractError(
        f"规则 {rule.rule_id} 的 rule_type 不是编译期产物：{rule.rule_type!r}",
        rule_index=rule.order,
        rule_type=rule.rule_type,
    )


def _range_core(rule: CompiledRule, field: str) -> str:
    """区间核心（不含 NULL 守卫）。默认 ``[min, max)``：min 闭、max 开。"""
    parts: list[str] = []
    lower = rule.params.get("min")
    if lower is not None:
        op = "<" if rule.params.get("inclusive_min", True) else "<="
        parts.append(f"{field} {op} {lower}")
    upper = rule.params.get("max")
    if upper is not None:
        op = ">" if rule.params.get("inclusive_max", False) else ">="
        parts.append(f"{field} {op} {upper}")
    return " OR ".join(parts)


def _typecast_core(rule: CompiledRule, field: str, target_type: DataSourceType) -> str:
    """类型转换核心（不含 NULL 守卫）。

    两种方言的判法不同，原因是**转换失败的语义不同**：

    - Doris：``CAST`` 失败返回 NULL，于是「转不动」天然等价于 ``CAST(...) IS NULL``。
    - MySQL 8：``CAST`` 对不可转换的值**直接报错**，一个坏值会让整个作业崩掉，
      而我们要的是「把这一行分流到错误表」。所以 MySQL 用前置 ``REGEXP`` 形状校验，
      根本不调 ``CAST``——这也顺带绕开了「``OR`` 两侧不保证短路」的问题。
    """
    target = str(rule.params.get("target_type", ""))
    strict = bool(rule.params.get("strict", True))
    if target_type is DataSourceType.DORIS and strict:
        return dialects.cast_expr(field, target)
    return dialects.regexp_not_match(field, dialects.typecast_shape(target, strict=strict))


def render_reason(rule: CompiledRule, target_type: DataSourceType) -> str:
    """规则 → 落错误表 ``reason`` 列的人话模板。

    **只含规则模板与字段名，不含任何字段值**（be-17 §6.3 的不可协商项）：
    这一列会随 ``error_record`` 进控制面库、进前端表格，是「错误行长什么样」
    在控制面唯一的可读线索。截到 ``MAX_REASON_LEN``——``reason`` 是 varchar(255)，
    超宽的症状是数据面写得进、控制面读不出。
    """
    predicate = render_predicate(rule, target_type)
    text = _strip_null_guard(predicate, rule.field)
    if len(text) <= MAX_REASON_LEN:
        return text
    return text[: MAX_REASON_LEN - 1] + "…"


def _strip_null_guard(predicate: str, field: str) -> str:
    """去掉 ``<f> IS NOT NULL AND`` 前缀与最外层括号：``reason`` 要描述「值哪里不对」。"""
    guard = f"{field} IS NOT NULL AND "
    blank_prefix = f"{field} IS NULL OR "
    if predicate.startswith(blank_prefix):
        return predicate[len(blank_prefix) :]
    if predicate.startswith(guard):
        rest = predicate[len(guard) :]
        if rest.startswith("(") and rest.endswith(")"):
            return rest[1:-1]
        return rest
    return predicate


def render_digest_expr(columns: Sequence[str], target_type: DataSourceType) -> str:
    """``payload_digest`` 的 SQL 表达式（be-17 §6.3）。

    ``SHA256 前 56 位 + LPAD(HEX(长度和), 8, '0')`` = 恰好 64 位。长度后缀让
    「同哈希不同长度」不可能混淆（哈希碰撞之外的第二道区分），也便于诊断区分被截断的行。

    **控制面只原样拷贝这个摘要，永不重算**（``engines/quality/rules.py`` 的 Python 实现
    仅作单测黄金向量）——两套算法漂移的症状是「控制面算的摘要与错误表里的对不上」，
    而两边都各有一套看似正确的实现。
    """
    dialects.assert_supported(target_type)
    if not columns:
        raise ContractError("摘要至少要有一个字段：空字段集会让每行算出同一个摘要")
    canons = [dialects.canonical_expr(column, target_type) for column in columns]
    lengths = " + ".join(dialects.length_expr(expr) for expr in canons)
    if len(canons) > 1:
        lengths = f"({lengths})"
    return dialects.concat_expr(
        [
            dialects.sha256_prefix_expr(dialects.concat_ws_expr(canons)),
            dialects.lpad_hex8_expr(f"{lengths} % {dialects.LENGTH_MODULUS}"),
        ]
    )


# ---------------------------------------------------------------- 分流 SQL


def render_tagged_select(
    source_sql: str,
    compiled: CompiledRules,
    *,
    run_id: int,
    target_type: DataSourceType,
) -> str:
    """源查询外套 CASE WHEN，产出带路由列的中间结果 ``tagged``（be-17 §6.2）。

    设计要点：

    - ``etl_run_id`` 是**常量列**（本次运行的 id）。错误表可被多次运行共用，
      控制面一律 ``WHERE run_id = ?`` 计数——没有这一列，两次运行的错误行会互相污染，
      而报告上看不出异常（数字偏大一点点，像是脏数据多了）。
    - 路由列用 ``reject`` 规则算，**``warn`` 规则一条都不进来**：warn 命中不改变路由，
      行照常进影子表。warn 的命中数由 ``services`` 侧在影子表上单独计数。
    - 优先级：``CASE WHEN`` 的顺序就是 ``order``（``rules`` 声明序），于是取到的是
      声明序最小的那条命中规则，结论确定。
    """
    dialects.assert_supported(target_type)
    if run_id <= 0:
        raise ContractError(f"run_id 必须是正整数，收到 {run_id!r}")

    branches = _reject_branches(compiled, target_type)

    projection: list[str] = [
        *compiled.valid_columns,
        f"{run_id} AS etl_run_id",
        f"{branches.error_code} AS _error_code",
        f"{branches.route} AS _quality_route",
        f"{branches.rule_id} AS _rule_id",
        # reason 与 field_name 也一并打进 tagged：违规行 SELECT 要落这两列，
        # 而它们依赖「哪条规则命中」。在 tagged 里算一次，比在下游再按谓词重算一遍可靠
        # （下游重算 = 同一份 CASE 出现两次，改一处忘一处就得到「错误码对、原因不对」）。
        f"{branches.reason} AS _reason",
        f"{branches.field_name} AS _field_name",
    ]

    # S608 在本模块一律是误报：拼接的每一段都来自**已编译的契约**——列名/字段名过标识符
    # 白名单（`_IDENTIFIER_RE`），字面量过 `_check_literal` + `quote_literal`。
    # 这里没有「字符串拼接出来的 SQL」这个风险，只有「把校验过的东西拼成 SQL」这个事实。
    return f"SELECT {', '.join(projection)} FROM ({source_sql}) AS src_tagged"  # noqa: S608


class _RejectBranches(NamedTuple):
    """四组 CASE 表达式的容器（``tagged`` 的路由列）。"""

    error_code: str
    rule_id: str
    route: str
    reason: str
    field_name: str


def _reject_branches(compiled: CompiledRules, target_type: DataSourceType) -> _RejectBranches:
    """路由列的四条 CASE 表达式：错误码 / 规则 id / 路由 / 原因 / 命中字段。

    没有 reject 规则时返回**常量**而不是空 CASE——``CASE WHEN ... END`` 没有分支是语法错，
    而一份只有 warn 规则的契约是完全合法的。这一情形下 ``_quality_route`` 必须是字符串
    ``'valid'`` 而不是布尔 ``FALSE``：MySQL 会把 ``FALSE = 'valid'`` 隐式转成 ``0 = 0``
    从而**判真**——一句「碰巧能跑」的 SQL，换到 Doris 上就是类型错。
    """
    rules = compiled.reject_rules
    if not rules:
        return _RejectBranches("NULL", "NULL", "'valid'", "''", "''")

    codes: list[str] = []
    ids: list[str] = []
    reasons: list[str] = []
    fields: list[str] = []
    predicates: list[str] = []
    for rule in rules:
        predicate = render_predicate(rule, target_type)
        predicates.append(f"({predicate})")
        codes.append(f"WHEN {predicate} THEN {dialects.quote_literal(rule.error_code.value)}")
        ids.append(f"WHEN {predicate} THEN {dialects.quote_literal(rule.rule_id)}")
        reasons.append(f"WHEN {predicate} THEN {dialects.quote_literal(render_reason(rule, target_type))}")
        fields.append(f"WHEN {predicate} THEN {dialects.quote_literal(rule.field)}")

    return _RejectBranches(
        error_code=f"CASE {' '.join(codes)} ELSE NULL END",
        rule_id=f"CASE {' '.join(ids)} ELSE NULL END",
        route=f"CASE WHEN {' OR '.join(predicates)} THEN 'reject' ELSE 'valid' END",
        reason=f"CASE {' '.join(reasons)} ELSE '' END",
        field_name=f"CASE {' '.join(fields)} ELSE '' END",
    )


def render_route_selects(
    compiled: CompiledRules, *, valid_columns: Sequence[str], target_type: DataSourceType
) -> tuple[str, str]:
    """``tagged`` → (有效行 SELECT, 违规行 SELECT)（be-17 §3.2）。

    **有效行显式列列**，不带任何内部路由列：``_quality_route`` / ``_error_code`` /
    ``etl_run_id`` / ``_rule_id`` / ``_reason`` / ``_field_name`` 一旦落进影子表，
    Swap 时的列一致性校验必然失败——而且失败在最后一步，前面全绿。

    违规行直接给 ``tagged`` 的内部列**改名**（``_rule_id AS rule_id`` 等），不再重算一遍
    谓词：同一份 CASE 出现两次，改一处忘一处就会得到「错误码对、原因不对」的错误行。
    ``run_id`` 取自 ``tagged.etl_run_id``（这正是 ``render_tagged_select`` 注入常量列的原因），
    因此本函数与 §3.2 一样**不需要** run_id 参数。

    ``target_type`` 是相对 §3.2 的必需追加：摘要表达式要用方言的文本类型名
    （``CHAR`` / ``STRING``）与哈希函数名，签名里漏了它就只能猜一个方言。
    """
    dialects.assert_supported(target_type)
    columns = _check_columns(valid_columns)
    if not columns:
        raise ContractError("valid_columns 不能为空：影子表 sink 必须显式列列")

    # S608 是误报：拼接段全部来自**已编译契约**——列名过标识符白名单（``_IDENTIFIER_RE``），
    # 字面量过 ``_check_literal`` + ``quote_literal``。本模块存在的意义就是产出 SQL 文本。
    valid_sql = (
        f"SELECT {', '.join(columns)} FROM {TAGGED_TABLE} "  # noqa: S608
        f"WHERE _quality_route = 'valid'"
    )

    digest = render_digest_expr(columns, target_type)
    rejected_sql = (
        f"SELECT etl_run_id AS run_id, _rule_id AS rule_id, _field_name AS field_name, "  # noqa: S608
        f"_error_code AS error_code, {digest} AS payload_digest, _reason AS reason, "
        f"{dialects.now_expr()} AS occurred_at "
        f"FROM {TAGGED_TABLE} WHERE _quality_route = 'reject'"
    )
    return valid_sql, rejected_sql
