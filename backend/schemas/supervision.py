"""运行监督的跨层 DTO（be-16 §3.2）。

只有一件事：把「这一轮采集到底做了什么」如实带回 ``tasks`` 层。监督环是
**静默运行**的（没有请求、没有人看响应），所以它的返回值就是唯一能在
beat 日志/压测输出里看见的结论——把「跳过」和「采到了但没超标」混成一个
空字典，出问题时只能靠翻数据库猜。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from engines.supervision.models import Severity

_FROZEN = ConfigDict(frozen=True, extra="forbid")


class CollectResult(BaseModel):
    """一次 ``collect_run`` 的结果。

    ``collected=False`` 时 ``status`` 说明跳过原因（``not_found`` / ``not_running``）——
    这不是错误：活跃运行的清单是**先查后采**的，中间那次状态跃迁（作业结束了、
    被人取消了）本来就会让这一轮无事可做。把它报成失败会制造假警报。
    """

    model_config = _FROZEN

    run_id: int = Field(ge=1)
    collected: bool = False
    status: str = ""
    severity: Severity = Severity.UNKNOWN
    #: ``reason`` 维度的取值（告警/中断都是因为它）。``None`` 表示这次没有单一因由。
    reason_dimension: str | None = None
    #: 本轮是否真的落了一条 ``supervision.warning``（可能被降频闸拦下）。
    warned: bool = False
    interrupted: bool = False
    #: 五个维度**全部**读不到（§6.3）：指标链路整体不可用，必须告警，
    #: 因为这时监督其实没有在监督。
    degraded: bool = False


__all__ = ["CollectResult"]
