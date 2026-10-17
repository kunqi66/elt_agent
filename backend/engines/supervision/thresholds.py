"""阈值、维度优先级与默认预算（be-16 §3.1 / §6.1 / §6.2）。

## 为什么优先级是常量，而不是「取水位最高的那个维度」

五个维度的量纲各不相同：拒绝率是 0~1 的比率、写入量是字节、时长是秒。
拿 ``ratio_pct`` 比大小意味着「1% 的字节超限」与「99% 的拒绝率超限」被判成同一件事，
而且**分母不同的两个比率谁大**这件事根本不构成业务含义。
所以同 severity 时按业务因果排：先看数据质量（写进去的东西对不对），
再看体量（写了多少），最后才看时长——时长超标往往是前三者的**结果**，
把它报成 reason 会把排查方向指向「作业太慢」而不是「拒绝率爆了」。

## 为什么 warn 边界要带一个相对容差

阈值判定是「观测值 >= 上限 * warn/100」。这几个数都是浮点：
``0.8 * 0.05`` 与 ``0.05 * 80 / 100`` 在二进制里可能差一个 ULP，
于是**恰好打在上限 80% 水位的那个作业**判 ok 还是判 warn，取决于这次乘法怎么舍入。
一个以确定性为招牌的门禁不能有这种性质：容差取值 ``1e-9``，相对量级，
在任何真实阈值（百分比级）上都远小于一次真实的水位变化，
只吞掉浮点噪声，吞不掉「79% 还是 80%」这种 1% 级别的差别。
"""

from __future__ import annotations

from engines.supervision.models import Dimension, Severity, SupervisionContractDTO

#: 同 severity 时的排序依据（越靠前越优先被报成 ``reason``）。
DIMENSION_PRIORITY: tuple[Dimension, ...] = (
    Dimension.REJECT_RATE,
    Dimension.WRITE_BYTES,
    Dimension.AMPLIFICATION,
    Dimension.READ_ROWS,
    Dimension.DURATION,
)

#: severity 的序。``UNKNOWN`` 高于 ``OK``：五个维度里有一个读不到时，
#: 整体结论不该是「一切正常」——那等于替一次没做成的判断签了字。
SEVERITY_RANK: dict[Severity, int] = {
    Severity.OK: 0,
    Severity.UNKNOWN: 1,
    Severity.WARN: 2,
    Severity.INTERRUPT: 3,
}

#: 判定用的相对容差，见模块 docstring。
BOUNDARY_EPSILON_REL = 1e-9

#: 默认预算。取值与 ``core.config.Settings.SUP_*`` 及
#: ``models.pipeline.RuntimeSupervisionContract`` 的列默认值**必须一致**
#: （《需求对齐记录》§四：1e8 / 50 GiB / 7200 / 1.5 / 0.05 / 80）。
#: 三处不一致的后果是「本机单测按 A 判、生产按 B 判」，而两边都自洽、都不报错。
DEFAULT_MAX_READ_ROWS = 100_000_000
DEFAULT_MAX_WRITE_BYTES = 53_687_091_200
DEFAULT_MAX_DURATION_SEC = 7_200
DEFAULT_MAX_AMPLIFICATION_RATIO = 1.5
DEFAULT_MAX_REJECT_RATE = 0.05
DEFAULT_WARN_THRESHOLD_PCT = 80

#: 同一维度 warn 的最短重复间隔（秒）。降频用，见 ``services.supervision``（§6.4）。
WARN_REPEAT_INTERVAL_SEC = 300


def budget_from_settings(
    *,
    max_read_rows: int = DEFAULT_MAX_READ_ROWS,
    max_write_bytes: int = DEFAULT_MAX_WRITE_BYTES,
    max_duration_sec: int = DEFAULT_MAX_DURATION_SEC,
    max_amplification_ratio: float = DEFAULT_MAX_AMPLIFICATION_RATIO,
    max_reject_rate: float = DEFAULT_MAX_REJECT_RATE,
    warn_threshold_pct: int = DEFAULT_WARN_THRESHOLD_PCT,
) -> SupervisionContractDTO:
    """把已读出的标量收成一份契约。

    **入参是标量而不是 ``Settings`` 对象**：engines 层不允许 import ``core.config``
    （CLAUDE.md §4.3），否则这个「纯函数」会把配置与 IO 一起拖进来，
    它也就不再能被单测直接调用。读 ``Settings`` 的那一步在
    ``services.supervision.contract_from_settings``。
    """
    return SupervisionContractDTO(
        max_read_rows=max_read_rows,
        max_write_bytes=max_write_bytes,
        max_duration_sec=max_duration_sec,
        max_amplification_ratio=max_amplification_ratio,
        max_reject_rate=max_reject_rate,
        warn_threshold_pct=warn_threshold_pct,
    )


def priority_index(dimension: Dimension) -> int:
    """维度在 ``DIMENSION_PRIORITY`` 里的位次（不在表里则排到最后）。

    兜底成「排最后」而不是抛错：新增一个维度却忘了登记优先级，
    症状应该是「它的 reason 优先级偏低」，而不是「整个监督环崩了」。
    但 ``tests/test_supervision.py`` 有一条用例逐个维度核这张表，
    漏登记在提交前就会被拦下。
    """
    try:
        return DIMENSION_PRIORITY.index(dimension)
    except ValueError:  # pragma: no cover - 由 tests 的覆盖用例守着
        return len(DIMENSION_PRIORITY)


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
    "budget_from_settings",
    "priority_index",
]
