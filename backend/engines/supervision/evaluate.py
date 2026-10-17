"""阈值判定（be-16 §3.1 / §6.1 / §6.2）。纯函数，无 IO。

三件事，各自只做一件事：

- :func:`dimension_verdict` —— 单个维度：观测值、上限 → 严重度；
- :func:`evaluate` —— 一次采样 + 契约 + 已运行时长 → 整体结论；
- :func:`worst` —— 若干维度结论 → 整体结论（severity 优先，其次维度优先级）。

**边界语义是 ``>=``**（§6.1）：观测值 **等于** 上限即判定超标。写成 ``>`` 的后果是
「恰好打满预算」的作业被判成没超——而预算是硬性的，打满就是到顶了。
"""

from __future__ import annotations

from collections.abc import Iterable

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
    SEVERITY_RANK,
    priority_index,
)


def dimension_verdict(
    *,
    dimension: Dimension,
    observed: float | None,
    limit: float | None,
    warn_threshold_pct: int,
) -> DimensionVerdict:
    """单维度判定。

    ``observed`` / ``limit`` 任一为 ``None``（或上限非正）→ ``unknown``。
    **unknown 不是 ok**：ok 的含义是「判断过了，没超标」，unknown 的含义是
    「这一次根本没有判断」——把后者说成前者，就是在一个读不到指标的作业上
    盖了一个「一切正常」的章（§6.3）。
    """
    if observed is None or limit is None or limit <= 0:
        return DimensionVerdict(
            dimension=dimension,
            severity=Severity.UNKNOWN,
            observed=observed,
            limit=limit,
            ratio_pct=None,
        )

    ratio_pct = observed / limit * 100.0
    warn_limit = limit * warn_threshold_pct / 100.0
    if observed >= limit:
        severity = Severity.INTERRUPT
    elif observed >= warn_limit - abs(warn_limit) * BOUNDARY_EPSILON_REL:
        # 容差只加在 warn 这一侧，理由见 thresholds 模块 docstring：
        # interrupt 那一侧的两个测试档位（0.99L / 1.0L）相差 1%，浮点噪声够不着。
        severity = Severity.WARN
    else:
        severity = Severity.OK

    return DimensionVerdict(
        dimension=dimension,
        severity=severity,
        observed=observed,
        limit=limit,
        ratio_pct=ratio_pct,
    )


def amplification(sample: MetricSample) -> float | None:
    """放大比 = 写入字节 / 读取字节。

    ``read_bytes`` 为 ``None`` 或 0 时返回 ``None``（→ 该维度 unknown）。
    0 是**除不了**而不是「比值为 0 或无穷」：读 0 字节写 N 字节在真实作业里
    只可能是「读侧没报数」，把它当成一个天文数字的放大比会立刻误判硬中断。

    注意：当前 ``clients.seatunnel.JobMetricsDTO`` 不携带读入字节（Zeta 的 metrics
    端点只回 writtenBytes 一系），所以**这条维度实际恒为 unknown**，直到数据面
    补上读入字节为止。这是设计里就写明的退化路径（§6.3），不是遗漏。
    """
    if sample.written_bytes is None or not sample.read_bytes:
        return None
    return sample.written_bytes / sample.read_bytes


def evaluate(
    sample: MetricSample,
    contract: SupervisionContractDTO,
    elapsed_sec: float | None,
) -> SupervisionVerdict:
    """一次采样的整体判定。

    时长**不取** ``sample.duration_sec``：那是数据面自报的数（SeaTunnel 的
    ``durationSec`` 在作业未启动时回 0），而硬中断要拦的恰好是「作业卡住了」。
    卡住时它自报的时长也是 0。所以时长一律用控制面自己算的 ``elapsed_sec``
    （``now - run.started_at``），与作业自述无关。
    """
    verdicts: tuple[DimensionVerdict, ...] = (
        dimension_verdict(
            dimension=Dimension.REJECT_RATE,
            observed=sample.reject_rate,
            limit=contract.max_reject_rate,
            warn_threshold_pct=contract.warn_threshold_pct,
        ),
        dimension_verdict(
            dimension=Dimension.WRITE_BYTES,
            observed=_as_float(sample.written_bytes),
            limit=float(contract.max_write_bytes),
            warn_threshold_pct=contract.warn_threshold_pct,
        ),
        dimension_verdict(
            dimension=Dimension.AMPLIFICATION,
            observed=amplification(sample),
            limit=contract.max_amplification_ratio,
            warn_threshold_pct=contract.warn_threshold_pct,
        ),
        dimension_verdict(
            dimension=Dimension.READ_ROWS,
            observed=_as_float(sample.read_rows),
            limit=float(contract.max_read_rows),
            warn_threshold_pct=contract.warn_threshold_pct,
        ),
        dimension_verdict(
            dimension=Dimension.DURATION,
            observed=elapsed_sec,
            limit=float(contract.max_duration_sec),
            warn_threshold_pct=contract.warn_threshold_pct,
        ),
    )
    return worst(verdicts)


def worst(verdicts: Iterable[DimensionVerdict]) -> SupervisionVerdict:
    """若干维度结论 → 整体结论。

    先比 ``severity``，同级再比 ``DIMENSION_PRIORITY``。**不新增第二条排序规则**：
    例如「谁先超的」需要历史，而 ``worst`` 是纯函数，手里只有这一帧。

    ``warnings`` 是**除 reason 之外**达到 warn 的维度。把 reason 也塞进去的话，
    「本次警告了哪个维度」这件事就有了两个来源（``reason`` 与 ``warnings``），
    而消费方只需要一个。
    """
    ordered = sorted(
        verdicts,
        key=lambda item: (-SEVERITY_RANK[item.severity], priority_index(item.dimension)),
    )
    if not ordered:  # pragma: no cover - evaluate 恒返回 5 个维度
        return SupervisionVerdict(severity=Severity.UNKNOWN)

    top = ordered[0]
    warnings = tuple(
        item
        for item in ordered[1:]
        if SEVERITY_RANK[item.severity] >= SEVERITY_RANK[Severity.WARN]
    )
    return SupervisionVerdict(
        severity=top.severity,
        reason=top,
        warnings=warnings,
        # ``dimensions`` 固定按 DIMENSION_PRIORITY 排（与 severity 无关）：
        # 它是「这一帧读了哪几个维度」的清单，顺序变了就不好与上一帧对比。
        dimensions=tuple(sorted(ordered, key=lambda item: priority_index(item.dimension))),
    )


def _as_float(value: int | None) -> float | None:
    """整数字段转浮点。``None`` 原样保留（不要变成 ``0.0``）。"""
    return None if value is None else float(value)


__all__ = ["amplification", "dimension_verdict", "evaluate", "worst"]
