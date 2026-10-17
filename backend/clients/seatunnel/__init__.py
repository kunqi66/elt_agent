"""SeaTunnel Zeta 客户端与运行期配置渲染（be-15）。

**对外不导出执行器**：``register()`` 是唯一的注入入口，调用点在
``main.py`` 的 lifespan 与 ``tasks/__init__.py`` 的 ``celeryd_init``。
把执行器函数也导出来会诱使某处绕过注册表直接调用——而绕过注册表等于绕过
``ToolBroker``（Capability 校验、jti 消费、证据账本），那正是「副作用唯一出口」
最不该出现的一个后门。
"""

from __future__ import annotations

from clients.seatunnel.client import SeatunnelClient, job_id_for_run
from clients.seatunnel.job import InjectionReport, build_job_name, iter_sink_tables, render_config
from clients.seatunnel.models import (
    FailureKind,
    JobMetricsDTO,
    JobState,
    JobStatusDTO,
    SeatunnelError,
    SeatunnelRejected,
    SeatunnelTimeout,
    SeatunnelUnavailable,
    SubmitResult,
    classify_failure,
    map_state,
)

__all__ = [
    "FailureKind",
    "InjectionReport",
    "JobMetricsDTO",
    "JobState",
    "JobStatusDTO",
    "SeatunnelClient",
    "SeatunnelError",
    "SeatunnelRejected",
    "SeatunnelTimeout",
    "SeatunnelUnavailable",
    "SubmitResult",
    "build_job_name",
    "classify_failure",
    "iter_sink_tables",
    "job_id_for_run",
    "map_state",
    "render_config",
]
