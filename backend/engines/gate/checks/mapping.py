"""``check_mapping``：字段映射完整性与敏感数据出域（be-11 §5.1 第 4 步 / §6.3）。

类别是 ``field_mapping``（见 ``check_id_to_category``——检查 id 叫 ``mapping``，
前端类别叫 ``field_mapping``，别在调用点手写字符串）。

本模块最要紧的一条是 :data:`_SENSITIVE_LEVELS` 的 **``mask_missing_for_sensitive``**：
源列敏感级别是 ``confidential`` / ``restricted``，而映射既没设 ``masking``、
也没走 ``mask`` / ``hash`` 类的派生列 → **block**。
这是「明文敏感数据出域」在**生成期**的静态拦截面（be-11 §6.3 末段）——
不等一行数据被搬出去才由 PDP 在 Prepare 阶段发现。它是本项目 P0/P1 拦截率的单元级证据。

## be-11 §6.3 里在本 DSL 不可表达的码（登记，不实现）

``missing_primary_key`` / ``pk_not_mapped`` / ``dedup_rule_absent`` 都以
``write_mode="upsert"`` + ``primary_keys`` 为前提，而本仓库的 ``PlanOutput.mode``
固定为 ``shadow_then_swap``（``engines/plan/schema.py`` 的差异表：``upsert`` 语义
改由质量契约表达，``missing_primary_key`` 由 ``E_DUPLICATE_KEY`` 承担）。
强行实现只会写出一段永远不触发的死代码。

## 新增/借用的 code（be-11 §6.3 未定义的）

- ``quality_rule_field_missing``（**借用 be-10**）：be-11 §6.3 的表里没有它，
  但 ``engines/plan/schema.py`` 的 ``PlanIssueCode`` 已经逐字定义了这个码。
  复用一个已在仓库里存在的码，好过为同一件事再造一个。
- ``dangling_quality_rule_id``（**新增**）：``FieldMapping.quality_rule_ids`` 指向了
  不存在的规则 id。be-11 §6.3 没定义；按仓库对悬空引用的一贯处置（``PlanIssueCode.dangling_ref``
  是 block）定为 block——它意味着「你以为在保护这个字段的质量规则根本不存在」。
- ``source_field_mapped_multiple_times``（**新增**）：同一个源字段被写进多个目标字段。
  be-11 §6.3 只定义了目标侧的孪生码 ``duplicate_target_field``（那是数据丢失，故 block）；
  源侧这个是放大的常见误配，定为 warn。
"""

from __future__ import annotations

from engines.gate.checks.base import finalize_findings, safe_message
from engines.gate.checks.schema import normalize_type
from engines.gate.context import GateContext, ProfileColumn
from engines.gate.types import GateFinding, PlanLocator, make_finding
from engines.plan.schema import EtlTransform, FieldMapping, QualityRule
from models.enums import SensitiveLevel

#: 触发脱敏红线的最低敏感级别。``internal`` 及以下不拦——
#: 每一列都要求脱敏等于让门禁失去区分度（be-11 §6.3 只点名敏感字段）。
_SENSITIVE_LEVELS = frozenset({SensitiveLevel.CONFIDENTIAL, SensitiveLevel.RESTRICTED})

#: 视为「已脱敏」的字段变换与派生列算子。
_MASKING_TRANSFORMS = frozenset({"mask", "hash"})


def check_mapping(ctx: GateContext) -> list[GateFinding]:
    """字段映射层面的确定性检查。纯函数：只看 ``ctx``。"""
    findings: list[GateFinding] = []
    plan = ctx.plan
    source_columns = ctx.all_columns()
    transforms = list(plan.transforms)

    findings.extend(_duplicate_target_findings(plan.field_mappings))
    findings.extend(_source_amplification_findings(plan.field_mappings))
    findings.extend(_transform_findings(transforms, source_columns, plan.field_mappings))
    findings.extend(_quality_rule_findings(ctx))
    findings.extend(_dangling_quality_rule_id_findings(plan.field_mappings, plan.quality_rules))
    findings.extend(_sensitive_egress_findings(ctx, source_columns, transforms))
    return finalize_findings("mapping", findings)


def _duplicate_target_findings(mappings: list[FieldMapping]) -> list[GateFinding]:
    """两个映射写同一目标字段 → block（静默丢数据的经典成因，be-11 §6.3）。"""
    findings: list[GateFinding] = []
    first_seen: dict[str, int] = {}
    for index, mapping in enumerate(mappings):
        previous = first_seen.get(mapping.target_field)
        if previous is None:
            first_seen[mapping.target_field] = index
            continue
        findings.append(
            make_finding(
                check_id="mapping",
                code="duplicate_target_field",
                severity="block",
                message=safe_message(
                    f"目标字段 {mapping.target_field} 被第 {previous} 个和第 {index} 个映射同时写入，"
                    "后一处会静默覆盖前一处，数据会悄悄丢失。"
                ),
                locator=PlanLocator(path=f"/field_mappings/{index}/target_field"),
                suggestion="合并这两个映射，或给它们各自一个不同的目标字段。",
            )
        )
    return findings


def _source_amplification_findings(mappings: list[FieldMapping]) -> list[GateFinding]:
    """同一源字段写进多个目标字段 → warn（放大的常见误配，be-11 §6.3 未定义，见模块 docstring）。"""
    findings: list[GateFinding] = []
    first_seen: dict[str, int] = {}
    for index, mapping in enumerate(mappings):
        previous = first_seen.get(mapping.source_field)
        if previous is None:
            first_seen[mapping.source_field] = index
            continue
        findings.append(
            make_finding(
                check_id="mapping",
                code="source_field_mapped_multiple_times",
                severity="warn",
                message=safe_message(
                    f"源字段 {mapping.source_field} 同时映射到第 {previous} 与第 {index} 个目标字段，"
                    "若并非有意复制，会放大写出数据量。"
                ),
                locator=PlanLocator(path=f"/field_mappings/{index}/source_field"),
                suggestion="确认这是有意的一对多映射，否则去掉多余的一条。",
            )
        )
    return findings


def _transform_findings(
    transforms: list[EtlTransform],
    source_columns: dict[str, ProfileColumn],
    mappings: list[FieldMapping],
) -> list[GateFinding]:
    """派生列的输入字段存在性，以及 cast 的目标类型是否与目标列一致。"""
    findings: list[GateFinding] = []
    # 可引用集合 = 源端列 ∪ 全部派生列（派生列可以级联引用另一派生列）。
    known = set(source_columns) | {transform.output_field for transform in transforms}
    target_type_by_output = {mapping.source_field: mapping.target_type for mapping in mappings}

    for index, transform in enumerate(transforms):
        path = f"/transforms/{index}"
        if not transform.input_fields:
            findings.append(
                make_finding(
                    check_id="mapping",
                    code="transform_missing_input",
                    severity="block",
                    message=safe_message(
                        f"派生列 {transform.output_field}（{path}）没有任何输入字段，无法计算。"
                    ),
                    locator=PlanLocator(path=f"{path}/input_fields"),
                    suggestion="补上该派生列的输入字段。",
                )
            )
        for offset, field in enumerate(transform.input_fields):
            if field in known:
                continue
            findings.append(
                make_finding(
                    check_id="mapping",
                    code="transform_missing_input",
                    severity="block",
                    message=safe_message(
                        f"派生列 {transform.output_field} 引用的输入字段 {field} 既不在源端列里，"
                        "也不是任何派生列的输出。"
                    ),
                    locator=PlanLocator(path=f"{path}/input_fields/{offset}"),
                    suggestion="核对字段名，或补齐上游派生列。",
                )
            )

        if transform.kind == "cast":
            findings.extend(_cast_target_findings(transform, path, target_type_by_output))
    return findings


def _cast_target_findings(
    transform: EtlTransform,
    path: str,
    target_type_by_output: dict[str, str],
) -> list[GateFinding]:
    """``cast`` 的 ``params.to_type`` 与该派生列最终写向的目标类型是否一致（warn）。"""
    to_type = transform.params.get("to_type")
    if not isinstance(to_type, str) or not to_type.strip():
        return []
    declared_target = target_type_by_output.get(transform.output_field)
    if not declared_target:
        return []

    cast_type, _ = normalize_type(to_type)
    target_type, _ = normalize_type(declared_target)
    if cast_type == target_type:
        return []
    return [
        make_finding(
            check_id="mapping",
            code="transform_kind_mismatch",
            severity="warn",
            message=safe_message(
                f"派生列 {transform.output_field} 的 cast 目标类型为 {to_type}，"
                f"但它写向的目标字段类型是 {declared_target}，两侧不一致。"
            ),
            locator=PlanLocator(path=f"{path}/params/to_type"),
            suggestion="把 cast 的 to_type 对齐目标列类型，避免二次隐式转换。",
        )
    ]


def _quality_rule_findings(ctx: GateContext) -> list[GateFinding]:
    """质量规则作用的字段必须真的存在于结果集（源列 ∪ 派生列 ∪ 目标列）。"""
    plan = ctx.plan
    result_fields = (
        set(ctx.all_columns())
        | {transform.output_field for transform in plan.transforms}
        | {mapping.target_field for mapping in plan.field_mappings}
    )
    findings: list[GateFinding] = []
    for index, rule in enumerate(plan.quality_rules):
        if rule.field in result_fields:
            continue
        findings.append(
            make_finding(
                check_id="mapping",
                code="quality_rule_field_missing",
                severity="block",
                message=safe_message(
                    f"质量规则 {rule.id} 作用的字段 {rule.field} 不在结果集"
                    "（源列 / 派生列 / 目标列）中，该规则永远不会生效。"
                ),
                locator=PlanLocator(path=f"/quality_rules/{index}/field"),
                suggestion="把规则挂到实际存在的字段上，或去掉这条规则。",
            )
        )
    return findings


def _dangling_quality_rule_id_findings(
    mappings: list[FieldMapping],
    rules: list[QualityRule],
) -> list[GateFinding]:
    """``FieldMapping.quality_rule_ids`` 指向的规则必须存在。"""
    rule_ids = {rule.id for rule in rules}
    findings: list[GateFinding] = []
    for index, mapping in enumerate(mappings):
        for offset, rule_id in enumerate(mapping.quality_rule_ids):
            if rule_id in rule_ids:
                continue
            findings.append(
                make_finding(
                    check_id="mapping",
                    code="dangling_quality_rule_id",
                    severity="block",
                    message=safe_message(
                        f"映射 /field_mappings/{index}（{mapping.source_field} → "
                        f"{mapping.target_field}）引用的质量规则 {rule_id} 不存在。"
                    ),
                    locator=PlanLocator(path=f"/field_mappings/{index}/quality_rule_ids/{offset}"),
                    suggestion="改成已定义的规则 id，或在 quality_rules 里补上该规则。",
                )
            )
    return findings


def _sensitive_egress_findings(
    ctx: GateContext,
    source_columns: dict[str, ProfileColumn],
    transforms: list[EtlTransform],
) -> list[GateFinding]:
    """敏感字段明文出域的静态拦截（be-11 §6.3 的 ``mask_missing_for_sensitive``）。

    触发条件三条同时成立：源列有档案、敏感级别 ≥ confidential、映射没有任何脱敏动作。
    """
    findings: list[GateFinding] = []
    for index, mapping in enumerate(ctx.plan.field_mappings):
        column = source_columns.get(mapping.source_field)
        if column is None or column.sensitive_level not in _SENSITIVE_LEVELS:
            continue
        if _is_masked(mapping, transforms):
            continue
        findings.append(
            make_finding(
                check_id="mapping",
                code="mask_missing_for_sensitive",
                severity="block",
                message=safe_message(
                    f"映射 /field_mappings/{index}：源字段 {mapping.source_field} 的敏感级别为"
                    f" {column.sensitive_level}，但该映射没有任何脱敏（mask/hash），"
                    f"明文敏感数据会出域到目标字段 {mapping.target_field}。"
                ),
                locator=PlanLocator(path=f"/field_mappings/{index}"),
                suggestion="为该映射设置 masking（mask/hash），或改用 mask/hash 类派生列。",
            )
        )
    return findings


def _is_masked(mapping: FieldMapping, transforms: list[EtlTransform]) -> bool:
    """映射本身、或产出该字段的派生列，任一做了脱敏即算已脱敏。"""
    if mapping.masking is not None:
        return True
    if mapping.transform in _MASKING_TRANSFORMS:
        return True
    return any(
        transform.output_field == mapping.source_field and transform.kind in _MASKING_TRANSFORMS
        for transform in transforms
    )


__all__ = ["check_mapping"]
