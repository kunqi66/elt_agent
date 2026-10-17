"""SeaTunnel 客户端的 DTO 与内部异常（be-15 §3.1 / §6.5 / §7）。

两类东西放一起是因为它们必须**同时**改：状态映射表（§6.5）决定了哪些原始字符串
算「已完成」，而异常分类（§5.4）决定了失败该不该重试——两处都从 `JobState` 派生。

本模块的自定义异常**不属于** ``core.errors.ErrorCode`` 契约：它们不出现在任何 API 响应里，
只在 worker 进程内流转，由 ``tasks.execution`` 映射成 run 状态与证据事件。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class JobState(StrEnum):
    """作业状态（内部枚举）。Zeta 的原始字符串只在这里出现一次。"""

    RUNNING = "running"
    FINISHED = "finished"
    FAILED = "failed"
    STOPPED = "stopped"
    NOT_FOUND = "not_found"
    UNKNOWN = "unknown"


class FailureKind(StrEnum):
    """失败分类（be-15 §5.4）。决定「要不要重试」而不是「怎么报错」。"""

    CONNECT_FAILED = "CONNECT_FAILED"
    CONFIG_REJECTED = "CONFIG_REJECTED"
    RESOURCE_INSUFFICIENT = "RESOURCE_INSUFFICIENT"
    KILLED = "KILLED"
    UNKNOWN = "UNKNOWN"

    @property
    def retryable(self) -> bool:
        """只有连接类失败值得重试。

        配置类**不可重试**：配置来自已冻结的制品，同样的字节重投多少次都是同样被拒；
        重试它只是把一次明确的配置错误拖成 5 次退避，最后还是死。
        """
        return self is FailureKind.CONNECT_FAILED


class SeatunnelError(Exception):
    """SeaTunnel 客户端的内部异常基类（不映射到 ErrorCode）。"""

    def __init__(self, message: str, *, kind: FailureKind = FailureKind.UNKNOWN) -> None:
        super().__init__(message)
        self.kind = kind

    def summary(self, limit: int = 200) -> str:
        """脱敏截断的摘要，供 ``execution_run.error_message`` 使用。

        截断而不是整段入库：Zeta 的错误里常带整段 HOCON 与连接串，
        而 ``error_message`` 是会进列表接口、会被人直接看到的那一列。
        """
        return str(self)[:limit]


class SeatunnelUnavailable(SeatunnelError):
    """连不上 Zeta（连接超时、DNS、5xx）。提交前的连接失败**可重试一次**。"""

    def __init__(self, message: str = "SeaTunnel 不可达") -> None:
        super().__init__(message, kind=FailureKind.CONNECT_FAILED)


class SeatunnelRejected(SeatunnelError):
    """Zeta 明确拒绝（4xx / 配置解析失败）。**不重试。**"""

    def __init__(self, message: str = "SeaTunnel 拒绝了本次提交") -> None:
        super().__init__(message, kind=FailureKind.CONFIG_REJECTED)


class SeatunnelTimeout(SeatunnelError):
    """状态/指标查询超时。只读操作超时不该阻塞监督（be-16 走退化路径）。"""

    def __init__(self, message: str = "SeaTunnel 响应超时") -> None:
        super().__init__(message, kind=FailureKind.CONNECT_FAILED)


#: Zeta 原始状态 → 内部状态。**未命中的一律 UNKNOWN**（记 warning，不自动推进状态）——
#: 猜一个「大概是失败吧」会把一次正常作业写成失败，而 in-flight 状态恰恰是最常见的。
_STATE_MAP: dict[str, JobState] = {
    "RUNNING": JobState.RUNNING,
    "SCHEDULED": JobState.RUNNING,
    "CREATED": JobState.RUNNING,
    "FINISHED": JobState.FINISHED,
    "FAILED": JobState.FAILED,
    "CANCELED": JobState.STOPPED,
    "CANCELLED": JobState.STOPPED,
}


def map_state(raw: str | None) -> JobState:
    """原始状态 → 内部枚举。空值按 ``NOT_FOUND``（作业不存在与「查不到状态」同义）。"""
    if not raw:
        return JobState.NOT_FOUND
    return _STATE_MAP.get(raw.strip().upper(), JobState.UNKNOWN)


def classify_failure(text: str | None) -> FailureKind:
    """错误文本 → 失败分类（be-15 §5.4 的判据表）。

    **先查具体再查通用**：`OutOfMemoryError` 也含 `Error`，而资源不足与未知故障的
    处置完全不同（一个要告警扩容，一个要读栈）。顺序写反会让所有分类都退化成 UNKNOWN。
    """
    if not text:
        return FailureKind.UNKNOWN
    lower = text.lower()
    if "outofmemory" in lower or "no space" in lower or "container" in lower:
        return FailureKind.RESOURCE_INSUFFICIENT
    if "connect" in lower or "unknownhost" in lower or "connection refused" in lower:
        return FailureKind.CONNECT_FAILED
    if "parseexception" in lower or "config" in lower or "syntax" in lower:
        return FailureKind.CONFIG_REJECTED
    return FailureKind.UNKNOWN


@dataclass(frozen=True, slots=True)
class SubmitResult:
    """提交结果。``reused`` 为真表示这次没真的提交，而是复用了已存在的同名作业。"""

    job_id: str
    job_name: str
    accepted_at: datetime
    reused: bool = False


@dataclass(frozen=True, slots=True)
class JobStatusDTO:
    job_id: str
    state: JobState
    started_at: datetime | None = None
    finished_at: datetime | None = None
    #: 已截断的原文摘要；``state`` 是结论，这个只是给人看的线索
    error_summary: str | None = None


@dataclass(frozen=True, slots=True)
class JobMetricsDTO:
    """作业指标。**字段全部可缺省**：Zeta 各版本给的计数口径不一致（§10 第 6 条）。

    缺省用 0 而不是 None：be-16 的阈值判定要拿它与预算比，
    而「读不到」与「读到了 0」在判定上都是「没有证据证明超限」——两者都该保持原状而不是中断。
    真正的「指标完全不可用」由 ``metrics()`` 返回 ``None`` 表达。
    """

    read_rows: int = 0
    written_rows: int = 0
    written_bytes: int = 0
    # 读入字节数：be-16 的**放大比**维度（写字节 / 读字节）唯一的来源。
    # 少了它，放大比恒判 ``unknown``——一条永远不会开火的阈值，
    # 而放大比正是「笛卡尔积 / join 写错」这类逻辑错误唯一会露头的地方。
    # ⚠️ 字段名没有对着**真 Zeta** 校过（本地没起 SeaTunnel），
    # 所以解析处接受几个候选键，未识别的计数器一律进 ``extra`` 留档。
    read_bytes: int = 0
    reject_rows: int = 0
    duration_sec: int = 0
    extra: dict[str, Any] = field(default_factory=dict)


__all__ = [
    "FailureKind",
    "JobMetricsDTO",
    "JobState",
    "JobStatusDTO",
    "SeatunnelError",
    "SeatunnelRejected",
    "SeatunnelTimeout",
    "SeatunnelUnavailable",
    "SubmitResult",
    "classify_failure",
    "map_state",
]
