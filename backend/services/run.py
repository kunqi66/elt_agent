"""执行编排（be-14）。

本模块是「批准」与「真的开始搬数据」之间那道闸，也是**唯一**能把一次审批变成
一条可执行命令的地方。它做三件事：

1. **Commit**：前置校验（be-13）→ 签发 Capability → 建 ``execution_run`` →
   Outbox 入队，**全部在同一个事务里**。事务外不投递（理由见 ``harness/outbox.py``）。
2. **状态机唯一写入口** ``apply_transition``：``(当前, 目标)`` 不在表内一律 ``RUN_CONFLICT``；
   并发靠条件 UPDATE 的 ``rowcount`` 裁决，不靠「先查后写」。
3. **运行查询**：列表 / 详情，均带 ``tenant_id``。

三条不可退让的规则：

- **事务边界归本模块**：``harness.outbox.enqueue`` 只写库不提交，是因为
  「业务事实与待发命令同生共死」这件事只有持事务的人才能保证。
- **令牌原文不落库、不进响应**：``capability`` 表只存 ``token_sha256``；
  令牌唯一的去处是 Outbox 载荷（Worker 从那里取出来验签）。
- **不重复实现幂等**：``Idempotency-Key`` 由 ``api/idempotency.py`` 的中间件统一处理。
  这里额外要求它非空，是为了给 ``execution_run.idempotency_key`` 一个**有语义**的值——
  否则两次不同请求会撞上同一个唯一约束，报出来的却是「键冲突」这种看不懂的错。
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from structlog import get_logger

from core.config import settings
from core.errors import (
    NotFoundError,
    RoleRequiredError,
    RunConflictError,
    ValidationError,
)
from core.ids import as_utc, utcnow
from core.redis import get_redis
from harness.broker import ExecutionContext, ToolBroker
from harness.capability import CapabilityIssuer, CapabilityVerifier
from harness.ledger import EvidenceLedger, LedgerEvent
from harness.outbox import Topic
from harness.outbox import enqueue as outbox_enqueue
from harness.replay import ReplayGuard
from harness.tools import TargetEnsureTablesArgs
from models.enums import (
    EvidenceEventType,
    PipelineStatus,
    RoleName,
    RunStatus,
    ToolName,
)
from models.execution import ExecutionRun
from models.governance import Capability
from models.pipeline import PipelineVersion
from schemas.auth import SubjectContext
from schemas.run import RunDetail, RunMetrics, RunSubmitContext, RunSummary
from services import preparation as preparation_service
from services.pipeline import repository
from services.pipeline.parts import error_table_name
from services.project import active_roles

log = get_logger(__name__)

_ledger = EvidenceLedger()

#: 运行状态机的唯一事实来源（be-14 §5.5）。
#:
#: 刻意**不**与 ``RunStatus.active_states()`` 合并：那个回答的是「哪些状态算活跃」
#: （并发约束用），这个回答的是「从哪个状态能走到哪个状态」（转移合法性）。
#: 两个问题恰好都用到同一批枚举值，但改动它们的理由完全不同——
#: 合成一个函数，会让「新增一个终态」这种改动顺手改到并发索引的语义上。
RUN_TRANSITIONS: Mapping[RunStatus, frozenset[RunStatus]] = {
    RunStatus.QUEUED: frozenset({RunStatus.RUNNING, RunStatus.FAILED, RunStatus.CANCELLED}),
    RunStatus.RUNNING: frozenset(
        {
            RunStatus.SUCCEEDED,
            RunStatus.FAILED,
            RunStatus.PAUSED,
            RunStatus.CANCELLED,
            RunStatus.INTERRUPTED,
        }
    ),
    RunStatus.PAUSED: frozenset({RunStatus.RUNNING, RunStatus.CANCELLED, RunStatus.FAILED}),
    # 中断与失败都能进回滚；``succeeded`` 也在内，但那条路径的语义是
    # 「回滚上一次发布」，与 be-18 的 ``rollback_plan.restore`` 取值集合绑定，见 §10 第 5 条。
    RunStatus.INTERRUPTED: frozenset({RunStatus.ROLLING_BACK, RunStatus.FAILED}),
    RunStatus.FAILED: frozenset({RunStatus.ROLLING_BACK}),
    RunStatus.SUCCEEDED: frozenset({RunStatus.ROLLING_BACK}),
    RunStatus.ROLLING_BACK: frozenset({RunStatus.ROLLED_BACK, RunStatus.FAILED}),
    # 终态没有出边。写成显式空集而不是省略：``.get()`` 的默认值会让
    # 「枚举新增一个状态」变成「新状态可以走到任何地方」——那是静默放行。
    RunStatus.CANCELLED: frozenset(),
    RunStatus.ROLLED_BACK: frozenset(),
}

#: 终态：进入即写 ``finished_at``。列表页与保留策略都按它筛。
TERMINAL_STATES: frozenset[RunStatus] = frozenset(
    {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED, RunStatus.ROLLED_BACK}
)

_issuer: CapabilityIssuer | None = None
_verifier: CapabilityVerifier | None = None


def _build_verifier() -> CapabilityVerifier:
    """校验器。**本模块此前只需要签发侧**（提交执行只签发令牌，执行在 worker 里），
    现在多了一个在本进程内执行、因而也要验签的动作（``target.ensure_tables``），
    所以公钥也必须挂上——构造细节归 ``harness.capability.verifier_from_settings``。
    """
    global _verifier
    if _verifier is None:
        from harness.capability import verifier_from_settings

        _verifier = verifier_from_settings()
    return _verifier


def _broker() -> ToolBroker:
    """本进程的 broker：验签器与签发器**都要**。

    验签器缺一个的症状不是「少一层保护」，而是整条提交链在本进程内一步都走不了
    （``harness/broker.py`` 第 ③ 步直接拒绝，文案却像部署漏配）。
    """
    return ToolBroker(
        guard=ReplayGuard(redis=get_redis(), ttl_sec=settings.CAPABILITY_TTL_SEC),
        ledger=_ledger,
        verifier=_build_verifier(),
        issuer=_capability_issuer(),
    )


def _capability_issuer() -> CapabilityIssuer:
    """Capability 签发器（**只有 api 进程需要**，worker 侧只持公钥）。

    懒加载：模块 import 期不读密钥文件，否则单测与 alembic 都会因为
    「本机没有 /run/secrets」而直接起不来（与 ``harness/capability.py`` 同一取舍）。
    """
    global _issuer
    if _issuer is None:
        _issuer = CapabilityIssuer(
            private_key_path=settings.CAPABILITY_SIGNING_KEY_PATH,
            key_id=settings.CAPABILITY_KEY_ID,
            issuer=settings.CAPABILITY_ISSUER,
            ttl_sec=settings.CAPABILITY_TTL_SEC,
        )
    return _issuer


def assert_transition(current: RunStatus, target: RunStatus) -> None:
    """转移合法性（纯函数）。非法即 ``RUN_CONFLICT``。"""
    if target not in RUN_TRANSITIONS.get(current, frozenset()):
        raise RunConflictError(f"运行状态不能从 {current.value} 变为 {target.value}")


# ---------------------------------------------------------------- Commit


async def commit(
    session: AsyncSession,
    *,
    preparation_id: int,
    subject: SubjectContext,
    idempotency_key: str,
) -> RunDetail:
    """把一张**审批通过**的准备单变成一条排队中的运行。**唯一能产生副作用入口的地方**。

    返回前把整个事务提交掉：``execution_run`` / ``outbox_message`` / ``capability`` /
    证据事件四者要么全在、要么全不在。投递由 beat 扫描 Outbox 完成，
    本函数**不** ``apply_async``（理由见 be-14 §5.1）。
    """
    if not idempotency_key:
        # 不给默认值：``execution_run.idempotency_key`` 是唯一约束，
        # 用随机值兜底会让「重复点按钮」变成两条真实的运行记录。
        raise ValidationError("Commit 必须携带 Idempotency-Key 请求头")

    now = utcnow()
    # ① 角色先查：它只需要准备单的 ``project_id``，比指纹校验便宜一个量级
    head = await preparation_service.load_preparation(
        session, tenant_id=subject.tenant_id, preparation_id=preparation_id
    )
    await _require_operator(session, project_id=head.project_id, user_id=subject.user_id, now=now)

    # ② be-13 的前置校验（指纹 / 审批齐 / 未过期），返回签发令牌所需的事实
    facts = await preparation_service.assert_committable(
        session, preparation_id=preparation_id, tenant_id=subject.tenant_id, now=now
    )

    # ③ 并发闸门：同一版本任一时刻只允许一条活跃运行
    await assert_no_active_run(session, pipeline_version_id=facts.pipeline_version_id)

    shadow_table, formal_table, error_table = _tables_of(facts.resource_scope)

    # ④ 签发 Capability。绑定的是**制品事实**（指纹 / 环境 / 租户 / 工具），
    #    不是「谁点的按钮」——按钮点两次不该产生两张令牌，但同一份制品在两个环境
    #    执行必须是两张（环境在绑定里）。
    issued = _capability_issuer().issue(
        preparation_id=facts.preparation_id,
        subject_id=subject.user_id,
        tool=ToolName.SEATUNNEL_SUBMIT,
        env=facts.environment,
        artifact_sha256=facts.artifact_sha256,
        tenant_id=facts.tenant_id,
        project_id=facts.project_id,
        now=now,
    )
    cap_row = _capability_row(issued=issued, facts=facts, subject_id=subject.user_id)
    session.add(cap_row)
    await session.flush()

    # ⑤ 运行实例
    run = ExecutionRun(
        tenant_id=facts.tenant_id,
        project_id=facts.project_id,
        pipeline_version_id=facts.pipeline_version_id,
        preparation_id=facts.preparation_id,
        capability_id=cap_row.id,
        idempotency_key=f"run:{idempotency_key}",
        status=RunStatus.QUEUED,
        shadow_table=shadow_table,
        formal_table=formal_table,
        error_table=error_table,
        triggered_by=subject.user_id,
    )
    session.add(run)
    await session.flush()

    # ⑤b 目标端备表（影子表 + 错误表）。**必须在作业提交之前**：SeaTunnel 的
    #     JDBC sink 默认只插数据、不建表，没有影子表作业会以「表不存在」失败；
    #     错误表更是分流行的唯一落点——缺了它，被拒的行无处可去而报告照出。
    #
    #    放在本事务里而不是 Outbox 里的理由：它**没有需要持久化的待办状态**
    #    （表名就在刚 flush 的 run 行上），而一次失败的建表应该当场变成一条
    #    可读的 4xx/5xx，而不是几秒后的一条运行失败——那时排查者要跨越
    #    API→outbox→worker 三层才知道「是目标库里根本没这张表」。
    await _ensure_target_tables(
        session,
        facts=facts,
        run_id=run.id,
        shadow_table=shadow_table,
        formal_table=formal_table,
        error_table=error_table,
        subject=subject,
        now=now,
    )

    # ⑥ Outbox 入队（同事务）。令牌必须随载荷走：capability 表只存哈希，
    #    Worker 没有别的途径拿到原文；而它又不是凭据（TTL 300 s、单次消费、绑定制品）。
    await outbox_enqueue(
        session,
        topic=Topic.SEATUNNEL_SUBMIT,
        payload={"run_id": run.id, "capability": issued.token},
        idempotency_key=f"submit:run:{run.id}",
    )

    # ⑦ 证据（同事务）。两件事分开记：一张令牌被签发，与一次运行被创建，
    #    是审计上两个不同的问题（「谁批的」与「谁跑的」）。
    await _ledger.append(
        session,
        LedgerEvent(
            tenant_id=facts.tenant_id,
            project_id=facts.project_id,
            actor_id=subject.user_id,
            event_type=EvidenceEventType.CAPABILITY_ISSUED,
            subject_type="capability",
            subject_id=str(issued.claims.jti),
            artifact_hash=facts.artifact_sha256,
            payload={
                "preparation_id": facts.preparation_id,
                "tool": ToolName.SEATUNNEL_SUBMIT.value,
                "run_id": run.id,
                "ttl_sec": settings.CAPABILITY_TTL_SEC,
            },
            occurred_at=now,
        ),
    )
    await _ledger.append(
        session,
        LedgerEvent(
            tenant_id=facts.tenant_id,
            project_id=facts.project_id,
            actor_id=subject.user_id,
            event_type=EvidenceEventType.RUN_CREATED,
            subject_type="run",
            subject_id=str(run.id),
            artifact_hash=facts.artifact_sha256,
            payload={
                "preparation_id": facts.preparation_id,
                "pipeline_version_id": facts.pipeline_version_id,
                "shadow_table": shadow_table,
                "formal_table": formal_table,
            },
            occurred_at=now,
        ),
    )

    # ⑧ 流程状态推进。这里**不用** workflow.transitions 的转移表：它的出边只覆盖
    #    M2 的生成/冻结段（OWNED_STATES），审批之后的状态归 M3，
    #    与 ``services/approval.py`` 的同一处取舍。
    await repository.set_pipeline_status(
        session=session, pipeline_id=facts.pipeline_id, status=PipelineStatus.QUEUED
    )

    await session.commit()
    log.info(
        "run_committed",
        run_id=run.id,
        preparation_id=facts.preparation_id,
        pipeline_version_id=facts.pipeline_version_id,
        request_id=subject.request_id,
    )
    return await get_run(session, subject, run.id)


def _capability_row(*, issued: Any, facts: Any, subject_id: int) -> Capability:
    """令牌 → ``capability`` 行。**只存哈希**：库被读走也拿不到可直接执行的令牌。

    两处签发（提交执行 / 目标端备表）共用本函数：行上的列一旦不同，
    受影响的是**消费侧**的原子 UPDATE（它按 ``jti`` 找行），
    而那种不一致的症状是「令牌明明刚签发，却报已被消费」。
    """
    return Capability(
        jti=issued.claims.jti,
        preparation_id=facts.preparation_id,
        subject_id=subject_id,
        tool=issued.claims.tool.value,
        environment=facts.environment,
        artifact_sha256=facts.artifact_sha256,
        token_sha256=issued.token_sha256,
        signature_alg="EdDSA",
        key_id=settings.CAPABILITY_KEY_ID,
        issued_at=issued.claims.iat,
        expires_at=issued.claims.exp,
    )


async def _ensure_target_tables(
    session: AsyncSession,
    *,
    facts: Any,
    run_id: int,
    shadow_table: str,
    formal_table: str,
    error_table: str,
    subject: SubjectContext,
    now: datetime,
) -> None:
    """签发一枚 ``target.ensure_tables`` 令牌并**当场**执行（同事务、同请求）。

    为什么不用提交执行那枚令牌顺手建表：Capability 的粒度是**工具**，
    一枚令牌只能被消费一次且只能执行它绑定的那个工具。让 ``seatunnel.submit``
    顺带建表，等于把「建表」藏进另一个工具的语义里——审计上那次 DDL 就没有主了。

    为什么这一步在 API 进程里执行而不是 worker：见 ``harness/tools.py`` 的
    ``TARGET_ENSURE_TABLES`` 工具规格（失败要当场可见，且它不碰正式表的数据）。
    """
    issued = _capability_issuer().issue(
        preparation_id=facts.preparation_id,
        subject_id=subject.user_id,
        tool=ToolName.TARGET_ENSURE_TABLES,
        env=facts.environment,
        artifact_sha256=facts.artifact_sha256,
        tenant_id=facts.tenant_id,
        project_id=facts.project_id,
        now=now,
    )
    session.add(_capability_row(issued=issued, facts=facts, subject_id=subject.user_id))
    await session.flush()
    await _ledger.append(
        session,
        LedgerEvent(
            tenant_id=facts.tenant_id,
            project_id=facts.project_id,
            actor_id=subject.user_id,
            event_type=EvidenceEventType.CAPABILITY_ISSUED,
            subject_type="capability",
            subject_id=str(issued.claims.jti),
            artifact_hash=facts.artifact_sha256,
            payload={
                "preparation_id": facts.preparation_id,
                "tool": ToolName.TARGET_ENSURE_TABLES.value,
                "run_id": run_id,
                "ttl_sec": settings.CAPABILITY_TTL_SEC,
            },
            occurred_at=now,
        ),
    )
    await _broker().execute(
        ToolName.TARGET_ENSURE_TABLES,
        issued.token,
        TargetEnsureTablesArgs(
            run_id=run_id,
            shadow_table=shadow_table,
            formal_table=formal_table,
            error_table=error_table,
        ),
        # ``actor_id`` 必须等于令牌里的 subject_id（broker 的 BINDING 阶段逐字比对）：
        # 这里传 ``subject.user_id``，签发时传的也是它，两边是**同一个**来源
        ctx=ExecutionContext(
            request_id=subject.request_id,
            tenant_id=facts.tenant_id,
            project_id=facts.project_id,
            actor_id=subject.user_id,
            caller="api",
            run_id=run_id,
        ),
        session=session,
        now=now,
    )


def _tables_of(resource_scope: Mapping[str, Any]) -> tuple[str, str, str]:
    """从准备单的写入范围取出影子表 / 正式表 / 错误表名。

    多目标表的作业在这里只记**主表**（``resource_scope.writes[0]``）：
    ``execution_run`` 只有三个标量列。完整清单仍在准备单的 ``resource_scope`` 里，
    be-15 渲染 HOCON 时按它逐表替换——**不是**「只处理第一张表」，
    而是「这一行列的是这次运行的代表性目标」。
    """
    writes = list(resource_scope.get("writes") or [])
    if not writes:
        raise ValidationError("准备单的写入范围为空，无法确定影子表与正式表")
    head = writes[0]
    formal = str(head.get("formal_table") or head.get("table") or "")
    shadow = str(head.get("shadow_table") or "")
    if not formal or not shadow:
        raise ValidationError("准备单的写入范围缺少 formal_table / shadow_table")
    return shadow, formal, error_table_name(formal)


async def assert_no_active_run(session: AsyncSession, *, pipeline_version_id: int) -> None:
    """同一版本不得有第二条活跃运行。

    数据库那边有部分唯一索引 ``uq_execution_run_active_per_version`` 兜底，
    但**两道都要有**：索引给出的是一条 ``IntegrityError``（前端只能显示「内部错误」），
    这里的判断给出的是一条能读懂、能行动的 409。反过来只留服务层判断也不行——
    「先查后写」在并发下必然双放行（CLAUDE.md §5.6 对 Capability 的同一道理）。
    """
    active = [status.value for status in RunStatus.active_states()]
    existing = await session.scalar(
        select(ExecutionRun.id).where(
            ExecutionRun.pipeline_version_id == pipeline_version_id,
            ExecutionRun.status.in_(active),
        )
    )
    if existing is not None:
        raise RunConflictError(f"版本 {pipeline_version_id} 已有正在进行的运行（run {existing}）")


async def _require_operator(session: AsyncSession, *, project_id: int, user_id: int, now: datetime) -> None:
    """动作接口的角色闸。角色**此刻**从 ``role_assignment`` 重查，不信 JWT 声明。"""
    held = await active_roles(session, project_id=project_id, user_id=user_id, at=now)
    if RoleName.OPERATOR not in held:
        raise RoleRequiredError("需要 operator 职责槽才能提交或干预执行")


# ---------------------------------------------------------------- 状态机


async def apply_transition(
    session: AsyncSession,
    *,
    run_id: int,
    target: RunStatus,
    reason: str,
    actor_id: int | None,
    now: datetime | None = None,
) -> RunSummary:
    """状态机的**唯一写入口**。非法转移 → ``RUN_CONFLICT``。

    并发安全靠条件 UPDATE：``WHERE id=:id AND status=:expected``，
    ``rowcount == 0`` 说明状态已被别人改走——此时**不报错也不覆盖**，
    读回当前状态返回（若已经是目标状态，这就是一次幂等成功）。
    拼「先查后写」的话，两个并发 cancel 会都通过检查、都去调 ``seatunnel.stop``，
    于是 SeaTunnel 收到两次停止命令——第二次的成功/失败取决于实现，不可预测。
    """
    moment = now or utcnow()
    row = await _load_run(session, run_id=run_id)
    current = row.status
    if current is target:
        # 幂等：重复点击「取消」不该报错
        return await _summary(session, row)

    assert_transition(current, target)

    values: dict[str, Any] = {
        "status": target,
        "updated_at": moment,
    }
    if target in TERMINAL_STATES:
        values["finished_at"] = moment
    if target is RunStatus.RUNNING and row.started_at is None:
        values["started_at"] = moment

    result = await session.execute(
        update(ExecutionRun).where(ExecutionRun.id == run_id, ExecutionRun.status == current).values(**values)
    )
    if result.rowcount == 0:  # type: ignore[attr-defined]
        # 输了这场并发：读回**状态列**。若已经是目标状态，那正是这次调用想要的结果；
        # 若不是，说明有人把它推到了别的状态，交给调用方按当前事实处置。
        # 这里只取状态列而不是 ``refresh`` 整行，理由与下面的成功路径相同。
        state = (
            await session.execute(
                select(ExecutionRun.status, ExecutionRun.finished_at, ExecutionRun.started_at).where(
                    ExecutionRun.id == run_id
                )
            )
        ).one_or_none()
        if state is None:
            raise RunConflictError(f"运行 {run_id} 在本次转移过程中已被删除")
        row.status, row.finished_at, row.started_at = state
        if row.status is target:
            return await _summary(session, row)
        raise RunConflictError(
            f"运行 {run_id} 的状态已被并发修改为 {row.status.value}，本次目标为 {target.value}"
        )

    # 成功路径把刚写下去的字段同步到内存实例上，**刻意不用** ``session.refresh(row)``。
    # 理由是那条 refresh 会按主键重读整行，从而**丢弃调用方尚未 flush 的改动**——
    # 实测：``row.error_message = "x"; await session.refresh(row)`` 之后属性变回库里的旧值。
    # ``tasks/execution._fail`` 正是这个形状：它先把 error_message / failure_snapshot
    # 写在 ORM 实例上、紧接着调本函数把状态推成 failed，于是「失败了」这件事记下了，
    # **失败现场却是空的**：运行详情里 error_message 为 null、failure_snapshot 为 ``{}``，
    # 而账本里那条 RUN_FAILED 又写着确实失败了——「有结论、没原因」的失败最难排查。
    # 值就是我们刚写进去的那几个，直接赋值既准确，又省掉一次整行回读。
    for column, value in values.items():
        setattr(row, column, value)

    log.info(
        "run_transition",
        run_id=run_id,
        previous=current.value,
        status=target.value,
        reason=reason,
        actor_id=actor_id,
    )
    return await _summary(session, row)


async def request_pause(
    session: AsyncSession, *, run_id: int, subject: SubjectContext, reason: str = ""
) -> RunDetail:
    """暂停：只停「后续阶段推进」，**不调 ``seatunnel.stop``**（作业继续跑）。

    这个语义是 be-14 §10 第 4 条挂着的未决项；本实现按「单向暂停」落地，
    且**不提供 resume**——恢复的正确做法是取消后重新 Commit（那份制品仍在，
    重新走一次审批与签发，代价是一条准备单而不是一次不受控的续跑）。
    """
    run = await _load_run(session, run_id=run_id, tenant_id=subject.tenant_id)
    await _require_operator(session, project_id=run.project_id, user_id=subject.user_id, now=utcnow())
    await apply_transition(
        session,
        run_id=run_id,
        target=RunStatus.PAUSED,
        reason=f"pause:{reason}" if reason else "pause",
        actor_id=subject.user_id,
    )
    await session.commit()
    return await get_run(session, subject, run_id)


async def request_cancel(
    session: AsyncSession, *, run_id: int, subject: SubjectContext, reason: str = ""
) -> RunDetail:
    """取消：经 ``seatunnel.stop`` 真正停作业。

    停止是**外部副作用**，因此走的是与提交同一条路：签一张绑定 ``seatunnel.stop``
    的 Capability，随 Outbox 投给 Worker，由 ``ToolBroker`` 去停。
    控制面在这里只改状态——如果在这里直接调 SeaTunnel，就等于在服务层开了第二个
    副作用出口（CLAUDE.md §5.1）。
    """
    now = utcnow()
    run = await _load_run(session, run_id=run_id, tenant_id=subject.tenant_id)
    await _require_operator(session, project_id=run.project_id, user_id=subject.user_id, now=now)

    if run.status is RunStatus.CANCELLED:
        return await get_run(session, subject, run_id)
    assert_transition(run.status, RunStatus.CANCELLED)

    facts = await preparation_service.assert_committable(
        session, preparation_id=run.preparation_id, tenant_id=subject.tenant_id, now=now
    )
    issued = _capability_issuer().issue(
        preparation_id=facts.preparation_id,
        subject_id=subject.user_id,
        tool=ToolName.SEATUNNEL_STOP,
        env=facts.environment,
        artifact_sha256=facts.artifact_sha256,
        tenant_id=facts.tenant_id,
        project_id=facts.project_id,
        now=now,
    )
    cap_row = Capability(
        jti=issued.claims.jti,
        preparation_id=facts.preparation_id,
        subject_id=subject.user_id,
        tool=issued.claims.tool.value,
        environment=facts.environment,
        artifact_sha256=facts.artifact_sha256,
        token_sha256=issued.token_sha256,
        signature_alg="EdDSA",
        key_id=settings.CAPABILITY_KEY_ID,
        issued_at=issued.claims.iat,
        expires_at=issued.claims.exp,
    )
    session.add(cap_row)
    await session.flush()

    await outbox_enqueue(
        session,
        topic=Topic.RUN_STOP,
        payload={
            "run_id": run.id,
            "capability": issued.token,
            "reason": "manual_cancel",
            # 执行主体必须随载荷走：worker 侧的 broker 在 BINDING 阶段要求
            # ``ctx.actor_id`` 非空（``harness/broker.py::_expected_binding``），
            # 少了它整条停止命令会被拒收；而它**不能**由 worker 自己猜成
            # ``run.triggered_by``——那会把「运维取消的」记成「申请人停的」，
            # 账本归因就错了。
            # 载荷里的这个值**不是**可信声明：broker 会拿它与令牌里签好的
            # ``subject_id`` 比对，对不上就是一次 binding 拒绝。
            "subject_id": subject.user_id,
        },
        idempotency_key=f"stop:run:{run.id}",
    )
    await apply_transition(
        session,
        run_id=run_id,
        target=RunStatus.CANCELLED,
        reason=f"cancel:{reason}" if reason else "cancel",
        actor_id=subject.user_id,
        now=now,
    )
    await _ledger.append(
        session,
        LedgerEvent(
            tenant_id=run.tenant_id,
            project_id=run.project_id,
            actor_id=subject.user_id,
            event_type=EvidenceEventType.CAPABILITY_ISSUED,
            subject_type="capability",
            subject_id=str(issued.claims.jti),
            artifact_hash=facts.artifact_sha256,
            payload={
                "preparation_id": facts.preparation_id,
                "tool": ToolName.SEATUNNEL_STOP.value,
                "run_id": run.id,
                "reason": reason,
            },
            occurred_at=now,
        ),
    )
    await session.commit()
    return await get_run(session, subject, run_id)


# ---------------------------------------------------------------- Worker 侧


async def load_for_submit(session: AsyncSession, *, run_id: int) -> RunSubmitContext:
    """Worker 提交前要的全部事实（``tasks.execution.submit_run`` 调它）。

    **按 run_id 直查、不带 tenant_id 参数**：这条路径由 Outbox 投递触发，
    run_id 来自我们自己写进载荷的标量，不是客户端可控输入。租户从**这一行**读出来，
    下游每一次查询都带它——把 tenant_id 做成参数反而会让调用方有机会传错一个值，
    而「传错租户」的失败形态是数据静默写进另一租户的表。
    """
    row = await session.get(ExecutionRun, run_id)
    if row is None:
        raise NotFoundError(f"运行 {run_id} 不存在")
    version = await repository.load_version(
        session=session, tenant_id=row.tenant_id, version_id=row.pipeline_version_id
    )
    preparation = await preparation_service.load_preparation(
        session, tenant_id=row.tenant_id, preparation_id=row.preparation_id
    )
    _plan_row, hocon_row, quality_row, supervision_row = await repository.artifact_rows(
        session=session, version=version
    )
    scope = dict(preparation.resource_scope or {})
    return RunSubmitContext(
        run_id=row.id,
        tenant_id=row.tenant_id,
        project_id=row.project_id,
        preparation_id=row.preparation_id,
        pipeline_id=version.pipeline_id,
        pipeline_version_id=version.id,
        version_no=version.version_no,
        artifact_sha256=version.sha256 or "",
        environment=preparation.environment,
        hocon_artifact_id=hocon_row.id,
        hocon_content=hocon_row.content,
        shadow_table=row.shadow_table or "",
        formal_table=row.formal_table or "",
        error_table=row.error_table or "",
        secret_refs=tuple(scope.get("secrets") or ()),
        supervision_contract_id=supervision_row.id,
        quality_contract_id=quality_row.id,
    )


def run_job_name(*, pipeline_id: int, version_no: int, run_id: int) -> str:
    """SeaTunnel 作业名（be-14 §6 第 4 条）。

    **只是转发**：真正的定义在 ``clients/seatunnel/job.py::build_job_name``，
    因为执行器（提交给 Zeta、按名查重）与这里（落证据事件 payload）必须拿到
    同一个字符串。两处各拼一份的话，改一处漏一处，症状是「重投时查不到自己的作业」
    ——同一次运行在 Zeta 里起了两个作业，两个都往同一张影子表写，而这不会报错。
    """
    from clients.seatunnel.job import build_job_name

    return build_job_name(pipeline_id=pipeline_id, version_no=version_no, run_id=run_id)


# ---------------------------------------------------------------- 读接口


async def list_runs(
    session: AsyncSession,
    ctx: SubjectContext,
    *,
    status: RunStatus | None = None,
    pipeline_id: int | None = None,
    pipeline_version_id: int | None = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[RunSummary], int]:
    """运行列表。``auditor`` 与普通成员同样只读，权限差别在写接口上。"""
    stmt = (
        select(ExecutionRun, PipelineVersion)
        .join(PipelineVersion, PipelineVersion.id == ExecutionRun.pipeline_version_id)
        .where(ExecutionRun.tenant_id == ctx.tenant_id)
    )
    if status is not None:
        stmt = stmt.where(ExecutionRun.status == status)
    if pipeline_version_id is not None:
        stmt = stmt.where(ExecutionRun.pipeline_version_id == pipeline_version_id)
    if pipeline_id is not None:
        stmt = stmt.where(PipelineVersion.pipeline_id == pipeline_id)

    rows = (
        await session.execute(
            stmt.order_by(ExecutionRun.id.desc()).offset((page - 1) * page_size).limit(page_size)
        )
    ).all()
    total = await _count(
        session,
        ctx=ctx,
        status=status,
        pipeline_id=pipeline_id,
        pipeline_version_id=pipeline_version_id,
    )
    return [
        _summary_of(
            run, pipeline_id=version.pipeline_id, version_id=version.id, version_no=version.version_no
        )
        for run, version in rows
    ], total


async def get_run(session: AsyncSession, ctx: SubjectContext, run_id: int) -> RunDetail:
    """运行详情。**不含令牌原文**（见 ``schemas/run.py`` 的模块 docstring）。"""
    row = await _load_run(session, run_id=run_id, tenant_id=ctx.tenant_id)
    version = await repository.load_version(
        session=session, tenant_id=ctx.tenant_id, version_id=row.pipeline_version_id
    )
    summary = _summary_of(
        row,
        pipeline_id=version.pipeline_id,
        version_id=version.id,
        version_no=version.version_no,
    )
    return RunDetail(
        # ``summary.model_dump()`` 里**已经**有 ``seatunnel_job_id``（RunSummary 与 RunDetail
        # 共有那几个字段），所以这里只补详情独有的。再显式传一次同名参数会直接 TypeError——
        # 而它炸的不是一条边缘路径：``commit`` / 运行详情 / ``pause`` / ``cancel``
        # 四条主路径全部 500，副作用却已经落库（用户看到「失败」，库里躺着一次运行）。
        **summary.model_dump(),
        capability_id=row.capability_id,
        metrics=RunMetrics(
            read_rows=row.read_rows,
            written_rows=row.written_rows,
            written_bytes=row.written_bytes,
            reject_rows=row.reject_rows,
            avg_throughput=float(row.avg_throughput or 0),
        ),
        failure_snapshot=dict(row.failure_snapshot or {}),
        error_message=row.error_message,
        can_cancel=row.status in RUN_TRANSITIONS and RunStatus.CANCELLED in RUN_TRANSITIONS[row.status],
        can_rollback=RunStatus.ROLLING_BACK in RUN_TRANSITIONS.get(row.status, frozenset()),
    )


async def _count(
    session: AsyncSession,
    *,
    ctx: SubjectContext,
    status: RunStatus | None,
    pipeline_id: int | None,
    pipeline_version_id: int | None,
) -> int:
    """总数单独查一次，而不是 ``len(rows)``：后者是**本页**的条数，
    前端分页控件会因此永远只有一页。"""
    from sqlalchemy import func

    stmt = (
        select(func.count())
        .select_from(ExecutionRun)
        .join(PipelineVersion, PipelineVersion.id == ExecutionRun.pipeline_version_id)
        .where(ExecutionRun.tenant_id == ctx.tenant_id)
    )
    if status is not None:
        stmt = stmt.where(ExecutionRun.status == status)
    if pipeline_version_id is not None:
        stmt = stmt.where(ExecutionRun.pipeline_version_id == pipeline_version_id)
    if pipeline_id is not None:
        stmt = stmt.where(PipelineVersion.pipeline_id == pipeline_id)
    return int(await session.scalar(stmt) or 0)


def _summary_of(row: ExecutionRun, *, pipeline_id: int, version_id: int, version_no: int) -> RunSummary:
    """行 → 摘要。``pipeline_id`` / ``version_no`` 必须由调用方从**版本行**带进来。

    一度想从 ``row`` 上「猜」出所属流程，结果是 0 或另一条流程的 id——
    前端会把两次不同流程的运行画进同一张列表，而列表本身看起来完全正常。
    """
    return RunSummary(
        id=row.id,
        project_id=row.project_id,
        pipeline_id=pipeline_id,
        pipeline_version_id=version_id,
        version_no=version_no,
        preparation_id=row.preparation_id,
        status=row.status,
        shadow_table=row.shadow_table,
        formal_table=row.formal_table,
        error_table=row.error_table,
        seatunnel_job_id=row.seatunnel_job_id,
        triggered_by=row.triggered_by,
        started_at=_at(row.started_at),
        finished_at=_at(row.finished_at),
        created_at=_at(row.created_at) or utcnow(),
    )


def _at(value: datetime | None) -> datetime | None:
    """库读出的时间一律按 UTC 补时区（SQLite 上是裸值，见 ``preparation._at``）。"""
    return None if value is None else as_utc(value)


async def _summary(session: AsyncSession, row: ExecutionRun) -> RunSummary:
    version = await repository.load_version(
        session=session, tenant_id=row.tenant_id, version_id=row.pipeline_version_id
    )
    return _summary_of(
        row, pipeline_id=version.pipeline_id, version_id=version.id, version_no=version.version_no
    )


async def _load_run(session: AsyncSession, *, run_id: int, tenant_id: int | None = None) -> ExecutionRun:
    """取运行行。给了 ``tenant_id`` 就强制过滤（所有面向用户的路径都必须给）。

    不给的只有 Worker 路径（``load_for_submit``）：那里 run_id 来自我们自己写的
    Outbox 载荷，而租户正是要从这一行读出来的东西——用一个还不存在的值去过滤，
    只会得到「永远查不到」。
    """
    stmt = select(ExecutionRun).where(ExecutionRun.id == run_id)
    if tenant_id is not None:
        stmt = stmt.where(ExecutionRun.tenant_id == tenant_id)
    row = await session.scalar(stmt)
    if row is None:
        raise NotFoundError(f"运行 {run_id} 不存在")
    return row


__all__ = [
    "RUN_TRANSITIONS",
    "TERMINAL_STATES",
    "apply_transition",
    "assert_no_active_run",
    "assert_transition",
    "commit",
    "get_run",
    "list_runs",
    "load_for_submit",
    "request_cancel",
    "request_pause",
    "run_job_name",
]
