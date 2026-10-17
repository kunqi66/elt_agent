"""EtlPlan 领域层（纯函数）。

- ``schema.py``：Plan DSL 契约与唯一序列化出口 ``dump_plan``；
- ``draft.py``：确定性兜底起草器（没有 LLM key 时整条链路仍能跑通）。

本模块只是导出面，不含逻辑——逻辑放各自的文件里，便于按模块做 mypy 与单测。
"""

from engines.plan.draft import DraftColumn, DraftSourceTable, DraftTable, DraftTargetTable, draft_plan
from engines.plan.schema import (
    EtlFilter,
    EtlPlanModel,
    EtlTransform,
    FieldMapping,
    FieldMasking,
    PlanInput,
    PlanOutput,
    QualityRule,
    RuntimeBudget,
    dump_plan,
)

__all__ = [
    "DraftColumn",
    "DraftSourceTable",
    "DraftTable",
    "DraftTargetTable",
    "EtlFilter",
    "EtlPlanModel",
    "EtlTransform",
    "FieldMapping",
    "FieldMasking",
    "PlanInput",
    "PlanOutput",
    "QualityRule",
    "RuntimeBudget",
    "dump_plan",
    "draft_plan",
]
