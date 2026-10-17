"""``PipelineStatus`` 的合法转移表（be-09 §5.5 / §9）。

**纯数据 + 纯函数，无 IO、无 PG**：转移判定是「协议」而不是「业务」，
它必须能在没有数据库的机器上被单独验证（本机就是那台机器）。
所以这里只有一张表与两个函数，判非法时就抛 :class:`ConflictError`（409），
**绝不改动 ``pipeline.status``**——「拒绝」和「回滚到旧值」是两件事，
后者在并发下会把别人刚写的新状态覆盖掉。

## 本模块拥有的转移（只有 4 个状态有出边）

| 当前 | 事件 | 新状态 | 触发者 |
|---|---|---|---|
| ``draft`` | ``gate_passed`` | ``generated`` | ``gate`` 节点 |
| ``draft`` | ``gate_blocked`` | ``gate_failed`` | ``gate`` 节点 |
| ``draft`` / ``generated`` / ``gate_failed`` | ``regenerate`` | ``draft`` | 用户重新生成 |
| ``generated`` | ``gate_passed`` | ``generated``（自环） | 重跑门禁且通过 |
| ``gate_failed`` | ``gate_passed`` | ``generated`` | ``autofix`` + ``gate`` |
| ``generated`` / ``gate_failed`` | ``gate_blocked`` | ``gate_failed``（含自环） | ``gate`` 节点 |
| ``generated`` | ``freeze`` | ``version_frozen`` | ``freeze`` 节点 |
| ``version_frozen`` | ``prepare`` | ``preparing`` | be-13（本模块只登记，不驱动） |

**为什么有自环**：``gate_passed`` 从 ``generated`` 回到 ``generated`` 不是「不动」，
而是「同一状态下的重复校验通过」——把它判成非法会让自动修复后的第二次门禁
（``gate_failed --gate_passed--> generated``）与手动重跑门禁的语义分裂成两套。
``regenerate → draft`` 同理：重新生成意味着丢掉当前候选、回到待生成，
这比「保持原状态」更能表达「候选已被丢弃」这一事实。

**为什么是这几条**：be-09 §5.5 只列了前五行，``prepare`` 一行来自同节末尾
「后续转移由 be-13/be-14 驱动，本模块只提供公共转移表」。**越界即错**：
``gate_failed`` 下 ``freeze`` → 409、``draft`` 下 ``prepare`` → 409、
``version_frozen`` 下 ``regenerate`` → 409（冻结过就代表版本已存在，
再生成必须走新 Pipeline，否则「版本号连续性」这条不变式会被一次 UPDATE 破坏）。

:data:`OUT_OF_SCOPE_STATES` 把剩下 12 个状态显式登记为「本模块无出边」。
它与 :data:`OWNED_STATES` 的并集**必须恰好等于** ``PipelineStatus`` 的全部成员——
枚举将来加值时这条不变式会红，而不是静默地多出一个「谁都能把它改掉」的状态。
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Final, Literal

from core.errors import ConflictError, ErrorCode
from core.logging import get_logger
from models.enums import PipelineStatus

__all__ = [
    "EVENTS",
    "OUT_OF_SCOPE_STATES",
    "OWNED_STATES",
    "TRANSITIONS",
    "assert_transition",
    "can_transition",
    "status_after_gate",
]

log = get_logger(__name__)

#: 事件词汇。取值即对外契约（写进日志与错误 details），改名等同于改审计口径。
StatusEvent = Literal["gate_passed", "gate_blocked", "freeze", "prepare", "regenerate"]

EVENTS: Final[tuple[StatusEvent, ...]] = (
    "gate_passed",
    "gate_blocked",
    "freeze",
    "prepare",
    "regenerate",
)

#: 本模块**拥有**出边的状态（be-09 §1「守卫 PipelineStatus 在本模块职责范围内的合法转移」）。
OWNED_STATES: Final[frozenset[PipelineStatus]] = frozenset(
    {
        PipelineStatus.DRAFT,
        PipelineStatus.GENERATED,
        PipelineStatus.GATE_FAILED,
        PipelineStatus.VERSION_FROZEN,
    }
)

#: 其余状态：本模块**没有**出边。列出来是为了「枚举加值即报错」，
#: 而不是为了让 ``can_transition`` 少写几个分支（后者用 ``.get`` 就够了）。
OUT_OF_SCOPE_STATES: Final[frozenset[PipelineStatus]] = frozenset(
    {
        PipelineStatus.PREPARING,
        PipelineStatus.PENDING_APPROVAL,
        PipelineStatus.APPROVED,
        PipelineStatus.COMMITTABLE,
        PipelineStatus.QUEUED,
        PipelineStatus.RUNNING,
        PipelineStatus.SUCCEEDED,
        PipelineStatus.FAILED,
        PipelineStatus.INTERRUPTED,
        PipelineStatus.ROLLING_BACK,
        PipelineStatus.ROLLED_BACK,
        PipelineStatus.REJECTED,
    }
)

_G: PipelineStatus = PipelineStatus.GENERATED
_F: PipelineStatus = PipelineStatus.GATE_FAILED
_D: PipelineStatus = PipelineStatus.DRAFT
_V: PipelineStatus = PipelineStatus.VERSION_FROZEN

#: 转移表。``MappingProxyType`` 让它只读——一张能被调用方就地改写的状态机
#: 不是一个状态机，而是一个全局变量。
TRANSITIONS: Final[MappingProxyType[PipelineStatus, MappingProxyType[str, PipelineStatus]]] = (
    MappingProxyType(
        {
            _D: MappingProxyType(
                {"gate_passed": _G, "gate_blocked": _F, "regenerate": _D},
            ),
            _G: MappingProxyType(
                {"gate_passed": _G, "gate_blocked": _F, "regenerate": _D, "freeze": _V},
            ),
            _F: MappingProxyType(
                {"gate_passed": _G, "gate_blocked": _F, "regenerate": _D},
            ),
            _V: MappingProxyType(
                {"prepare": PipelineStatus.PREPARING},
            ),
        }
    )
)


def can_transition(current: PipelineStatus, event: str) -> PipelineStatus | None:
    """合法则返回新状态，否则返回 ``None``（不抛）。

    与 :func:`assert_transition` 分开是给**只读展示**用的：前端要能画
    「这个状态下有哪些动作可用」，那种调用不该靠捕获异常来判空。
    """
    edges = TRANSITIONS.get(current)
    if edges is None:
        return None
    return edges.get(event)


def assert_transition(
    current: PipelineStatus,
    event: str,
    *,
    pipeline_id: int | None = None,
    request_id: str | None = None,
) -> PipelineStatus:
    """合法则返回新状态；非法抛 :class:`ConflictError`（``CONFLICT`` / 409）。

    ``pipeline_id`` / ``request_id`` 是**可选**的：给了就绑进 warning 日志，
    没给也不影响判定（纯函数的部分不因日志而依赖调用上下文）。
    """
    nxt = can_transition(current, event)
    if nxt is not None:
        return nxt
    # 键名**不能**用 ``event``：structlog 把 ``event`` 留给了日志消息本身，
    # 传 ``event=`` 会撞成 ``TypeError: got multiple values for argument 'event'``——
    # 于是「非法转移」这个本该 409 的判定变成一条 TypeError（500），
    # 而真正的错因（状态机拒绝）在栈里一个字都看不到。
    log.warning(
        "illegal_status_transition",
        pipeline_id=pipeline_id,
        request_id=request_id,
        current=str(current),
        status_event=event,
    )
    raise ConflictError(
        f"当前状态 {current} 不允许执行 {event}，请刷新后重试",
        code=ErrorCode.CONFLICT,
        details={"current": str(current), "event": event},
    )


def status_after_gate(verdict: str) -> PipelineStatus:
    """门禁结论 → 状态（be-09 §6.3）。

    ``pass`` / ``warn`` 都算「可继续」：``warn`` 是给人看的提醒（如档案过期），
    把它当阻断会让「今天不能再冻结版本」，而门禁面板已经把 warn 显示出来了。
    未知 verdict 直接抛 ``ValueError`` 而不是回落到某一档——
    回落意味着引擎哪天多出第四档结论时，我们会**静默地**把它当通过或阻断。
    """
    if verdict in ("pass", "warn"):
        return PipelineStatus.GENERATED
    if verdict == "block":
        return PipelineStatus.GATE_FAILED
    raise ValueError(f"未知的门禁结论：{verdict!r}")
