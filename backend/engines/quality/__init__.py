"""质量契约与数据分流（be-17）——纯函数层。

对外只有四件事：**编译契约**（:mod:`engines.quality.contract`）、**求值与摘要的参考实现**
（:mod:`engines.quality.rules`）、**合成报告与 ``passed`` 判定**（:mod:`engines.quality.report`）、
以及**目标端只读端口**（:mod:`engines.quality.ports`）。

不 import fastapi / DB / Redis / 网络 / LLM（CLAUDE.md §4.3）：这一层要能在
「没有 PG、没有 Doris、没有隧道」的机器上被单测完整覆盖，而质量与分流的口径
恰恰是最需要被钉死的那部分——它的错法是「报告显示一切正常」。
"""

from __future__ import annotations

from engines.quality.contract import (
    DEFAULT_ERROR_CODE,
    ERROR_COLUMNS,
    HARD_BLOCK_CODES,
    MAX_FIELD_LEN,
    MAX_PATTERN_LEN,
    MAX_REASON_LEN,
    MAX_RULE_ID,
    TAGGED_TABLE,
    CompiledRule,
    CompiledRules,
    QualityContractSpec,
    QualityRuleSpec,
    compile_contract,
    contract_sha256,
    render_digest_expr,
    render_predicate,
    render_reason,
    render_route_selects,
    render_tagged_select,
    target_columns,
)
from engines.quality.dialects import (
    DIGEST_LEN,
    FIELD_SEP_CHARCODE,
    NULL_CANON,
    SUPPORTED_TARGETS,
    quote_literal,
    supported_typecast_types,
)
from engines.quality.errors import ContractError
from engines.quality.ports import ErrorSample, ErrorTableReader
from engines.quality.report import (
    DIAGNOSTICS_VERSION,
    MAX_SAMPLES_PER_RULE,
    SUGGESTIONS,
    TOP_ERROR_CODES,
    BlockedReason,
    QualityDiagnosticCode,
    QualityDiagnosticItem,
    QualityDiagnosticRule,
    QualityDiagnostics,
    QualityDiagnosticSample,
    QualityReportDTO,
    budget_used_ratio_of,
    build_diagnostic_items,
    build_diagnostics,
    build_report,
    decide_passed,
    format_rate,
    reject_rate_of,
)
from engines.quality.rules import (
    canonical_field,
    canonicalize_row,
    digest_content_length,
    evaluate_rule,
    payload_digest,
)

# 按「领域分组」组织（编译 / 方言 / 报告 / 求值 / 端口），不按字母序：
# 分组能让「这一层对外承诺了什么」一眼看完。
__all__ = [
    # --- 异常 ---
    "ContractError",
    # --- 契约与编译 ---
    "CompiledRule",
    "CompiledRules",
    "DEFAULT_ERROR_CODE",
    "ERROR_COLUMNS",
    "HARD_BLOCK_CODES",
    "MAX_FIELD_LEN",
    "MAX_PATTERN_LEN",
    "MAX_REASON_LEN",
    "MAX_RULE_ID",
    "QualityContractSpec",
    "QualityRuleSpec",
    "TAGGED_TABLE",
    "compile_contract",
    "contract_sha256",
    "target_columns",
    # --- SQL 渲染 ---
    "render_digest_expr",
    "render_predicate",
    "render_reason",
    "render_route_selects",
    "render_tagged_select",
    # --- 方言 ---
    "DIGEST_LEN",
    "FIELD_SEP_CHARCODE",
    "NULL_CANON",
    "SUPPORTED_TARGETS",
    "quote_literal",
    "supported_typecast_types",
    # --- 求值与摘要（参考实现）---
    "canonical_field",
    "canonicalize_row",
    "digest_content_length",
    "evaluate_rule",
    "payload_digest",
    # --- 报告与判定 ---
    "BlockedReason",
    "DIAGNOSTICS_VERSION",
    "MAX_SAMPLES_PER_RULE",
    "QualityDiagnosticCode",
    "QualityDiagnosticItem",
    "QualityDiagnosticRule",
    "QualityDiagnosticSample",
    "QualityDiagnostics",
    "QualityReportDTO",
    "SUGGESTIONS",
    "TOP_ERROR_CODES",
    "build_diagnostic_items",
    "build_diagnostics",
    "build_report",
    "budget_used_ratio_of",
    "decide_passed",
    "format_rate",
    "reject_rate_of",
    # --- 目标端端口 ---
    "ErrorSample",
    "ErrorTableReader",
]
