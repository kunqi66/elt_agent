"""``check_schema``：存在性与类型兼容性（be-11 §5.1 第 3 步 / §6.2）。

**只判断，不改写 Plan**：字段**存不存在、什么类型**由探查档案说了算
（``ctx.source_tables`` / ``ctx.target_table``，是 ``schema_snapshot`` 的纯数据投影），
Plan 里写的 ``source_type`` / ``target_type`` 只是意图。
因此「Plan 声明的源类型与档案不一致」时，用**档案的类型**继续判兼容，并报一条 warn——
反过来（信 Plan）会让「Plan 写 varchar、库里其实是 int」这种漂移被直接放行。

## 兼容矩阵怎么落进代码

be-11 §6.2 的 8×10 表逐格转写成 :data:`_TYPE_COMPAT`，取值三种：
``ok`` 直接兼容 / ``cast`` 需显式 cast / ``no`` 禁止（→ ``type_incompatible`` block）。
矩阵的**行键**（源）与**列键**（目标）都是归一化类型名，两侧各有一张别名表
（:data:`_SOURCE_ROW` / :data:`_TARGET_COL`），把 ``int(11)`` / ``INTEGER`` / ``nvarchar``
这类库方言收敛到 be-11 的行列上。扩展类型时**必须三处一起改**（别名表、矩阵、单测）：
矩阵少一格只会退化成「不判」，不会报错——这是最容易静默失效的地方。

## 新增/借用的 code（be-11 §6.2 未定义的）

- ``source_type_mismatch``（**新增**）：be-11 要求「以档案为权威」，但码表只给了兼容性判定，
  没给「Plan 声明的源类型与档案实际类型不一致」这一条。它是 ``type_incompatible`` 的前置。
- ``profile_expired``（**新增**）：``TableProfile.expires_at`` 过期只做 warn（context.py 明说），
  be-11 §6.2 没有对应码。**参考时刻取自 ``ctx.policy_decision.evaluated_at``**——
  它由 service 注入、与本次门禁同一次判定；engines 内一律不取系统时间。
  拿不到（``policy_decision is None``）时**跳过**，不猜。
- ``unknown_data_type``（**新增**）：归一化后既不在矩阵的行也不在列的未知类型。
  不能当 ``type_incompatible`` 报——「不认识」不等于「不兼容」；但没被判过也是一条风险，
  故报 warn 明说「这一格没校验过」，免得用户以为门禁看过了。
"""

from __future__ import annotations

import re

from engines.gate.checks.base import finalize_findings, safe_message
from engines.gate.context import GateContext, TableProfile
from engines.gate.types import GateFinding, PlanLocator, SourceTableLocator, make_finding
from engines.plan.schema import FieldMapping

# 兼容性取值（be-11 §6.2 的三个符号）。
_OK = "ok"
_CAST = "cast"
_NO = "no"

#: 矩阵的列 = 目标归一化类型（be-11 §6.2 表头的 10 列）。
_TARGET_TYPES: tuple[str, ...] = (
    "int",
    "bigint",
    "decimal",
    "float",
    "varchar",
    "text",
    "date",
    "timestamp",
    "bool",
    "json",
)

#: be-11 §6.2 的类型兼容矩阵，逐格照抄。行键 = 源归一化类型的等价类。
_TYPE_COMPAT: dict[str, dict[str, str]] = {
    # int / bigint
    "int_bigint": {
        "int": _OK,
        "bigint": _OK,
        "decimal": _OK,
        "float": _OK,
        "varchar": _CAST,
        "text": _CAST,
        "date": _NO,
        "timestamp": _NO,
        "bool": _CAST,
        "json": _NO,
    },
    # decimal
    "decimal": {
        "int": _CAST,
        "bigint": _CAST,
        "decimal": _OK,
        "float": _OK,
        "varchar": _CAST,
        "text": _CAST,
        "date": _NO,
        "timestamp": _NO,
        "bool": _NO,
        "json": _NO,
    },
    # float / double
    "float_double": {
        "int": _CAST,
        "bigint": _CAST,
        "decimal": _CAST,
        "float": _OK,
        "varchar": _CAST,
        "text": _CAST,
        "date": _NO,
        "timestamp": _NO,
        "bool": _NO,
        "json": _NO,
    },
    # varchar / text
    "varchar_text": {
        "int": _CAST,
        "bigint": _CAST,
        "decimal": _CAST,
        "float": _CAST,
        "varchar": _OK,
        "text": _OK,
        "date": _CAST,
        "timestamp": _CAST,
        "bool": _CAST,
        "json": _CAST,
    },
    # date
    "date": {
        "int": _NO,
        "bigint": _NO,
        "decimal": _NO,
        "float": _NO,
        "varchar": _CAST,
        "text": _CAST,
        "date": _OK,
        "timestamp": _OK,
        "bool": _NO,
        "json": _NO,
    },
    # timestamp
    "timestamp": {
        "int": _NO,
        "bigint": _NO,
        "decimal": _NO,
        "float": _NO,
        "varchar": _CAST,
        "text": _CAST,
        "date": _CAST,
        "timestamp": _OK,
        "bool": _NO,
        "json": _NO,
    },
    # bool
    "bool": {
        "int": _CAST,
        "bigint": _CAST,
        "decimal": _NO,
        "float": _NO,
        "varchar": _CAST,
        "text": _CAST,
        "date": _NO,
        "timestamp": _NO,
        "bool": _OK,
        "json": _NO,
    },
    # json
    "json": {
        "int": _NO,
        "bigint": _NO,
        "decimal": _NO,
        "float": _NO,
        "varchar": _CAST,
        "text": _OK,
        "date": _NO,
        "timestamp": _NO,
        "bool": _NO,
        "json": _OK,
    },
}

#: 源类型别名 → 矩阵行键。be-04 应产出归一化名，这里是**防御性**的方言收敛。
_SOURCE_ROW: dict[str, str] = {}
for _alias in ("int", "integer", "int32", "smallint", "tinyint", "mediumint", "bigint", "int64", "long"):
    _SOURCE_ROW[_alias] = "int_bigint"
for _alias in ("decimal", "numeric", "number", "money"):
    _SOURCE_ROW[_alias] = "decimal"
for _alias in ("float", "double", "real", "float32", "float64"):
    _SOURCE_ROW[_alias] = "float_double"
for _alias in ("varchar", "nvarchar", "char", "nchar", "string", "text", "longtext", "clob", "varchar2"):
    _SOURCE_ROW[_alias] = "varchar_text"
_SOURCE_ROW["date"] = "date"
for _alias in ("timestamp", "datetime", "timestamptz", "datetime2", "timestamp_tz"):
    _SOURCE_ROW[_alias] = "timestamp"
for _alias in ("bool", "boolean", "bit"):
    _SOURCE_ROW[_alias] = "bool"
for _alias in ("json", "jsonb"):
    _SOURCE_ROW[_alias] = "json"

#: 目标类型别名 → 矩阵列名。注意 int 与 bigint 在**目标侧是两个不同的列**（不像源侧合并）。
_TARGET_COL: dict[str, str] = {}
for _alias in ("int", "integer", "int32", "smallint", "tinyint", "mediumint"):
    _TARGET_COL[_alias] = "int"
for _alias in ("bigint", "int64", "long"):
    _TARGET_COL[_alias] = "bigint"
for _alias in ("decimal", "numeric", "number", "money"):
    _TARGET_COL[_alias] = "decimal"
for _alias in ("float", "double", "real", "float32", "float64"):
    _TARGET_COL[_alias] = "float"
for _alias in ("varchar", "nvarchar", "char", "nchar", "string", "varchar2"):
    _TARGET_COL[_alias] = "varchar"
for _alias in ("text", "longtext", "clob"):
    _TARGET_COL[_alias] = "text"
_TARGET_COL["date"] = "date"
for _alias in ("timestamp", "datetime", "timestamptz", "datetime2", "timestamp_tz"):
    _TARGET_COL[_alias] = "timestamp"
for _alias in ("bool", "boolean", "bit"):
    _TARGET_COL[_alias] = "bool"
for _alias in ("json", "jsonb"):
    _TARGET_COL[_alias] = "json"

#: ``varchar(255)`` / ``decimal(10,2)`` → 基础名 + 首个数字（长度或精度）。
_PRECISION_RE = re.compile(r"^(?P<base>.+?)\(\s*(?P<digits>\d+)(?:\s*,\s*\d+)?\s*\)$")


def normalize_type(raw: str) -> tuple[str, int | None]:
    """``varchar(255)`` → ``("varchar", 255)``；``INT(11)`` → ``("int", 11)``。"""
    text = raw.strip().lower()
    match = _PRECISION_RE.match(text)
    if match:
        return match.group("base").strip(), int(match.group("digits"))
    return text, None


def check_schema(ctx: GateContext) -> list[GateFinding]:
    """对着档案判字段存在性与类型兼容性。纯函数：只看 ``ctx``。"""
    findings: list[GateFinding] = []
    source_columns = ctx.all_columns()
    # 派生列也是合法的「源字段」（schema.py：靠列名关联），否则好好的映射会被误判成字段缺失。
    derived_fields = {transform.output_field for transform in ctx.plan.transforms}
    target_columns = ctx.target_columns()
    target_exists = ctx.target_table is not None

    for index, mapping in enumerate(ctx.plan.field_mappings):
        path = f"/field_mappings/{index}"
        source_column = source_columns.get(mapping.source_field)

        if source_column is None and mapping.source_field not in derived_fields:
            findings.append(
                make_finding(
                    check_id="schema",
                    code="source_field_absent",
                    severity="block",
                    message=safe_message(
                        f"映射 {path} 的源字段 {mapping.source_field} 在源表档案里不存在；"
                        "可能是列名拼错，或源表结构已变化而档案未重新探查。"
                    ),
                    locator=PlanLocator(path=f"{path}/source_field"),
                    suggestion="核对源表列名，或重新探查源表档案后重跑门禁。",
                )
            )
            # 源列都不存在，后面的类型判定没有权威依据，不再叠报。
            continue

        declared_source, _ = normalize_type(mapping.source_type)
        profile_length: int | None = None
        if source_column is not None:
            profile_source, profile_length = normalize_type(source_column.data_type)
            if profile_source != declared_source:
                findings.append(
                    make_finding(
                        check_id="schema",
                        code="source_type_mismatch",
                        severity="warn",
                        message=safe_message(
                            f"{path} 声明的源类型为 {mapping.source_type}，"
                            f"而档案里 {mapping.source_field} 实际是 {source_column.data_type}；"
                            "以档案为准继续判定兼容性。"
                        ),
                        locator=PlanLocator(path=f"{path}/source_type"),
                        suggestion=f"把 source_type 改成档案里的 {source_column.data_type}。",
                    )
                )
            effective_source = profile_source
        else:
            effective_source = declared_source

        target_column = target_columns.get(mapping.target_field)
        if target_exists and target_column is None:
            findings.append(
                make_finding(
                    check_id="schema",
                    code="target_field_absent",
                    severity="block",
                    message=safe_message(
                        f"{path} 的目标字段 {mapping.target_field} 在目标表"
                        f" {ctx.target_table.table if ctx.target_table else ''} 的档案里不存在。"
                    ),
                    locator=PlanLocator(path=f"{path}/target_field"),
                    suggestion="核对目标表列名，或先让目标表结构就位（首次同步时目标表可不存在）。",
                )
            )
        effective_target = normalize_type(
            target_column.data_type if target_column is not None else mapping.target_type
        )[0]

        findings.extend(
            _type_findings(
                path=path,
                mapping=mapping,
                effective_source=effective_source,
                effective_target=effective_target,
                source_display=source_column.data_type if source_column else mapping.source_type,
                target_display=target_column.data_type if target_column else mapping.target_type,
            )
        )

        # 可空性与长度截断都要求两侧档案都在手，缺一边就不猜。
        if source_column is not None and target_column is not None:
            if (
                source_column.nullable
                and not target_column.nullable
                and not _has_not_null_rule(ctx, mapping.target_field)
            ):
                findings.append(
                    make_finding(
                        check_id="schema",
                        code="nullability_violation",
                        severity="warn",
                        message=safe_message(
                            f"{path}：源字段 {mapping.source_field} 可空，目标字段"
                            f" {mapping.target_field} 非空，且没有 not_null 质量规则兜底，"
                            "空值会在写入时被拒。"
                        ),
                        locator=PlanLocator(path=f"{path}/target_field"),
                        suggestion="给该目标字段补一条 not_null 质量规则，或允许目标列可空。",
                    )
                )
            target_length = normalize_type(target_column.data_type)[1]
            if profile_length is not None and target_length is not None and profile_length > target_length:
                findings.append(
                    make_finding(
                        check_id="schema",
                        code="length_truncation_risk",
                        severity="warn",
                        message=safe_message(
                            f"{path}：源字段 {mapping.source_field} 的长度上限 {profile_length}"
                            f" 大于目标字段 {mapping.target_field} 的 {target_length}，超长值会被截断。"
                        ),
                        locator=PlanLocator(path=f"{path}/target_field"),
                        suggestion="放宽目标列长度，或在映射上补 trim/truncate 处理。",
                    )
                )

    findings.extend(_expired_profile_findings(ctx))
    return finalize_findings("schema", findings)


def _type_findings(
    *,
    path: str,
    mapping: FieldMapping,
    effective_source: str,
    effective_target: str,
    source_display: str,
    target_display: str,
) -> list[GateFinding]:
    """查兼容矩阵，把一格翻译成 0~1 条 finding。"""
    row = _SOURCE_ROW.get(effective_source)
    column = _TARGET_COL.get(effective_target)
    if row is None or column is None:
        return [
            make_finding(
                check_id="schema",
                code="unknown_data_type",
                severity="warn",
                message=safe_message(
                    f"{path} 的类型组合（源 {source_display} → 目标 {target_display}）"
                    "有一侧不在门禁的兼容矩阵内，这一格没有被校验过。"
                ),
                locator=PlanLocator(path=f"{path}/target_type"),
                suggestion="确认该类型是否受目标库支持；若是新类型，请扩展门禁矩阵并补单测。",
            )
        ]

    verdict = _TYPE_COMPAT[row][column]
    if verdict == _NO:
        return [
            make_finding(
                check_id="schema",
                code="type_incompatible",
                severity="block",
                message=safe_message(
                    f"{path} 的类型不兼容：源 {source_display} 不能写入目标 {target_display}。"
                ),
                locator=PlanLocator(path=f"{path}/target_type"),
                suggestion="改用兼容的目标类型，或在映射前用 cast 派生列转换。",
            )
        ]
    if verdict == _CAST and mapping.transform != "cast":
        return [
            make_finding(
                check_id="schema",
                code="type_needs_cast",
                severity="warn",
                message=safe_message(
                    f"{path} 的类型需显式转换：源 {source_display} → 目标 {target_display}，"
                    "但该映射的 transform 不是 cast。"
                ),
                locator=PlanLocator(path=f"{path}/transform"),
                suggestion="把该映射的 transform 设为 cast，或改用 cast 派生列。",
            )
        ]
    return []


def _has_not_null_rule(ctx: GateContext, target_field: str) -> bool:
    return any(
        rule.enabled and rule.field == target_field and rule.rule_type == "not_null"
        for rule in ctx.plan.quality_rules
    )


def _expired_profile_findings(ctx: GateContext) -> list[GateFinding]:
    """档案过期 → warn。参考时刻取自 PDP 判定时刻（engines 不许取系统时间）。"""
    decision = ctx.policy_decision
    if decision is None:
        return []
    now = decision.evaluated_at

    findings: list[GateFinding] = []
    ordered: list[TableProfile] = [
        profile for _key, profile in sorted(ctx.source_tables.items(), key=lambda kv: kv[0])
    ]
    if ctx.target_table is not None:
        ordered.append(ctx.target_table)

    for profile in ordered:
        if profile.expires_at is None:
            continue
        # 时区信息不一致时无法比较（TypeError），跳过一个不可判的 warn 好过让门禁抛异常。
        if (profile.expires_at.tzinfo is None) != (now.tzinfo is None):
            continue
        if profile.expires_at >= now:
            continue
        findings.append(
            make_finding(
                check_id="schema",
                code="profile_expired",
                severity="warn",
                message=safe_message(
                    f"表 {profile.schema_name}.{profile.table} 的元数据档案已于"
                    f" {profile.expires_at.isoformat()} 过期，字段存在性与类型的判定依据可能已经陈旧。"
                ),
                locator=_table_locator(profile),
                suggestion="重新探查该表，刷新档案后再冻结版本。",
            )
        )
    return findings


def _table_locator(profile: TableProfile) -> SourceTableLocator:
    return SourceTableLocator(
        data_source_id=profile.data_source_id,
        schema=profile.schema_name,
        table=profile.table,
    )


__all__ = ["check_schema", "normalize_type"]
