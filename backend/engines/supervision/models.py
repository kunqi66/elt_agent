"""运行监督的输入输出 DTO（be-16 §3.1）。

**纯函数层**：只依赖 Pydantic 与标准库（CLAUDE.md §4.3）。禁 import
fastapi / sqlalchemy / httpx / core / clients / models —— 判定必须能在没有库、
没有网络的地方被单测逐格钉住，否则「超预算判定」这件事就只能靠集成环境验。

**全部 frozen + extra="forbid"**：
- ``frozen`` 是「同输入必同输出」的一半——判定过程中输入被谁改掉，结论就不再有意义；
- ``extra="forbid"`` 让「多塞了一个看起来该生效的字段」当场报错。这一层吃过亏：
  写的一侧多一个键、读的一侧解析失败，报错却出现在离现场很远的地方。

**缺失值一律用 ``None``，绝不用 0 顶替**（§6.3）。0 是一个**合法读数**，
拿它代替「读不到」会让一个指标不可达的作业看起来像一次空跑——恰好躲过所有下限判断，
而它可能正在往影子表里灌数据。
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

_FROZEN = ConfigDict(frozen=True, extra="forbid")


class Dimension(StrEnum):
    """判定维度。取值即契约：进告警事件载荷、进前端时间线、进日志字段。

    用 ``StrEnum`` 而不是裸字符串：维度名会被写进证据账本的 ``payload.dimension``，
    拼错的话降频闸「本次维度 != 上次维度」永远成立，于是**每次采集都发一条告警**——
    一个拼写错误就这样变成了告警风暴。
    """

    REJECT_RATE = "reject_rate"
    WRITE_BYTES = "write_bytes"
    AMPLIFICATION = "amplification_ratio"
    READ_ROWS = "read_rows"
    DURATION = "duration"


class Severity(StrEnum):
    """单维度严重度。顺序语义见 ``thresholds.SEVERITY_RANK``。"""

    OK = "ok"
    UNKNOWN = "unknown"
    WARN = "warn"
    INTERRUPT = "interrupt"


class MetricSample(BaseModel):
    """一次采集到的指标快照。

    全部字段可缺省为 ``None``：SeaTunnel 的 ``/metrics`` 端点在某些版本/状态下
    答不上来，客户端会回 ``None`` 而不是回一个全 0 的 DTO（``clients/seatunnel``）。
    到这一层就是「某些维度读不到」，判定必须能带着空洞继续走。

    ``duration_sec`` 是唯一有默认值的：时长由控制面自己计时（``elapsed_sec``），
    与 SeaTunnel 答不答得上来无关。
    """

    read_rows: int | None = None
    written_rows: int | None = None
    written_bytes: int | None = None
    read_bytes: int | None = None
    reject_rows: int | None = None
    valid_rows: int | None = None
    reject_rate: float | None = None
    duration_sec: float = 0.0


class SupervisionContractDTO(BaseModel):
    """一次运行的预算上限（``runtime_supervision_contract`` 行的镜像）。

    上限一律要求 ``> 0``：0 上限的语义是「这个维度一有读数就中断」，
    没有哪个业务意图是这个意思，出现它只可能是「契约行没建出来」被默认成了 0。
    宁可在这里当场拒绝，也不要让每个维度都判成 interrupt。
    """

    max_read_rows: int = Field(gt=0)
    max_write_bytes: int = Field(gt=0)
    max_duration_sec: int = Field(gt=0)
    max_amplification_ratio: float = Field(gt=0)
    max_reject_rate: float = Field(gt=0)
    warn_threshold_pct: int = Field(default=80, ge=0, le=100)


class DimensionVerdict(BaseModel):
    """单维度的判定结果。

    ``ratio_pct`` 是展示用的水位（``observed / limit * 100``），**不参与判定**——
    判定用的是与上限的直接比较，水位只在告警里给人看。把它当成判定依据会引入
    一次除法带来的浮点误差，而误差恰好落在阈值上的那个作业就取决于机器了。
    """

    dimension: Dimension
    severity: Severity
    observed: float | None = None
    limit: float | None = None
    ratio_pct: float | None = None


class SupervisionVerdict(BaseModel):
    """一次采集的整体结论。

    ``reason`` 是「最该看的那个维度」：severity 最高的若干维度里，
    按 ``DIMENSION_PRIORITY`` 排第一的那个（同 severity 时比总量纲没有意义，
    见 ``thresholds``）。``warnings`` 是**除 reason 之外**所有达到 warn 的维度——
    只报一个的话，「拒绝率超标」会把同时超标的「写入量」盖掉，而后者才是根因。
    """

    severity: Severity
    reason: DimensionVerdict | None = None
    warnings: tuple[DimensionVerdict, ...] = ()
    #: 五个维度的原始判定（顺序固定为 ``DIMENSION_PRIORITY``）。
    #: 诊断「到底哪几个维度读不到」需要它——只看 severity 分不出
    #: 「全部 unknown」与「有 ok 也有 unknown」这两件事。
    dimensions: tuple[DimensionVerdict, ...] = ()


__all__ = [
    "Dimension",
    "DimensionVerdict",
    "MetricSample",
    "Severity",
    "SupervisionContractDTO",
    "SupervisionVerdict",
]
