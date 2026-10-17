"""运行监督判定引擎（be-16，纯函数层）。

「抄指标 → 判阈值 → 决定要不要停作业」里的**判定**部分：没有任何 IO，
输入是采样与预算，输出是结论。这样同一组数字在任何环境下必得同一个结论，
而硬中断这种不可逆动作正需要这个性质。
"""

from engines.supervision.evaluate import amplification, dimension_verdict, evaluate, worst
from engines.supervision.models import (
    Dimension,
    DimensionVerdict,
    MetricSample,
    Severity,
    SupervisionContractDTO,
    SupervisionVerdict,
)
from engines.supervision.thresholds import (
    BOUNDARY_EPSILON_REL,
    DEFAULT_MAX_AMPLIFICATION_RATIO,
    DEFAULT_MAX_DURATION_SEC,
    DEFAULT_MAX_READ_ROWS,
    DEFAULT_MAX_REJECT_RATE,
    DEFAULT_MAX_WRITE_BYTES,
    DEFAULT_WARN_THRESHOLD_PCT,
    DIMENSION_PRIORITY,
    SEVERITY_RANK,
    WARN_REPEAT_INTERVAL_SEC,
    budget_from_settings,
    priority_index,
)

__all__ = [
    "BOUNDARY_EPSILON_REL",
    "DEFAULT_MAX_AMPLIFICATION_RATIO",
    "DEFAULT_MAX_DURATION_SEC",
    "DEFAULT_MAX_READ_ROWS",
    "DEFAULT_MAX_REJECT_RATE",
    "DEFAULT_MAX_WRITE_BYTES",
    "DEFAULT_WARN_THRESHOLD_PCT",
    "DIMENSION_PRIORITY",
    "SEVERITY_RANK",
    "WARN_REPEAT_INTERVAL_SEC",
    "Dimension",
    "DimensionVerdict",
    "MetricSample",
    "Severity",
    "SupervisionContractDTO",
    "SupervisionVerdict",
    "amplification",
    "budget_from_settings",
    "dimension_verdict",
    "evaluate",
    "priority_index",
    "worst",
]
