"""安全执行内核。

**所有副作用的唯一出口**（CLAUDE.md §5.1）：写外部系统、执行作业、改数据的动作
一律经 :class:`~harness.broker.ToolBroker`。``api/`` 与 ``services/`` 不得直接调用
SeaTunnel、目标库 DDL 或任何外部写接口。

依赖方向是单向的：``harness`` 可以依赖 ``core`` / ``models`` / ``engines``（纯函数层），
**禁止**依赖 ``services`` 与 ``api``。这条不是洁癖——安全内核若能被业务层反向影响，
「谁在什么条件下放行了什么」就再也说不清了。
"""

from __future__ import annotations

from harness.broker import ExecutionContext, ToolBroker, ToolResult
from harness.capability import (
    CapabilityClaims,
    CapabilityIssuer,
    CapabilityVerifier,
    ExpectedBinding,
    IssuedCapability,
)
from harness.errors import (
    CapabilityRequiredError,
    PreparationNotReadyError,
    QualityGateError,
    RejectStage,
    UnauthorizedToolError,
)
from harness.ledger import EvidenceLedger, LedgerError, LedgerEvent, LedgerSigner, VerifyReport
from harness.replay import ConsumptionResult, ReplayGuard
from harness.tools import (
    ToolSpec,
    all_tools,
    get_executor,
    get_tool,
    register_executor,
    register_tool,
)

__all__ = [
    "CapabilityClaims",
    "CapabilityIssuer",
    "CapabilityRequiredError",
    "CapabilityVerifier",
    "ConsumptionResult",
    "EvidenceLedger",
    "ExecutionContext",
    "ExpectedBinding",
    "IssuedCapability",
    "LedgerError",
    "LedgerEvent",
    "LedgerSigner",
    "PreparationNotReadyError",
    "QualityGateError",
    "RejectStage",
    "ReplayGuard",
    "ToolBroker",
    "ToolResult",
    "ToolSpec",
    "UnauthorizedToolError",
    "VerifyReport",
    "all_tools",
    "get_executor",
    "get_tool",
    "register_executor",
    "register_tool",
]
