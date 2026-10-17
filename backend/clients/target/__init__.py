"""目标端原子发布与清理（be-18）。

**对外不导出执行器**（与 ``clients/seatunnel`` 同一取舍）：``register()`` 是唯一的注入入口，
调用点在 ``main.py`` 的 lifespan 与 ``tasks/__init__.py`` 的 ``celeryd_init``。
导出执行器函数会诱使某处绕过注册表直接调用——而绕过注册表等于绕过 ``ToolBroker``
（Capability 校验、jti 单次消费、证据账本），那正是「副作用唯一出口」最不该出现的后门。

本包对外的三类东西：

- **模型与 Protocol**（``base``）：``TargetPublisher`` / ``PublishResult`` / ``CleanupResult`` …
- **构造入口**（``registry``）：``build_publisher`` 与 ``load_run_target``——
  服务层与执行器**共用同一个解析链**（否则两处对「目标端是谁」会有两套答案）
- **对账规则**（``base.reconcile_final_state``）：纯函数，Doris 与 MySQL 共用一份
"""

from __future__ import annotations

from clients.target.base import (
    FINGERPRINT_SCOPE,
    CleanupResult,
    ColumnInfo,
    PublishPlan,
    PublishResult,
    RollbackPlan,
    RunRef,
    TableInfo,
    TargetBinding,
    TargetCleanupError,
    TargetDDLError,
    TargetEndpointRef,
    TargetError,
    TargetPrecheckError,
    TargetPublisher,
    TargetScopeError,
    UnsupportedTargetError,
    assert_columns_replacable,
    assert_publishable,
    column_diffs,
    reconcile_final_state,
)
from clients.target.registry import RunTargetFacts, build_publisher, load_run_target

__all__ = [
    # 模型与规则
    "FINGERPRINT_SCOPE",
    "CleanupResult",
    "ColumnInfo",
    "PublishPlan",
    "PublishResult",
    "RollbackPlan",
    "RunRef",
    "TableInfo",
    "TargetBinding",
    "TargetEndpointRef",
    "assert_columns_replacable",
    "assert_publishable",
    "column_diffs",
    "reconcile_final_state",
    # 异常
    "TargetCleanupError",
    "TargetDDLError",
    "TargetError",
    "TargetPrecheckError",
    "TargetPublisher",
    "TargetScopeError",
    "UnsupportedTargetError",
    # 构造与解析
    "RunTargetFacts",
    "build_publisher",
    "load_run_target",
]
