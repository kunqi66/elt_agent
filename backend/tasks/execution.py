"""数据面执行任务（be-14 §5.4）。

消费 Outbox 投递的命令 → 验签 Capability → 原子消费 jti → 交给 ``ToolBroker`` 执行
→ 回写 ``seatunnel_job_id`` 与状态 → 落账本 → 转监督。

四条不可动摇的规则：

1. **副作用只能经 ``ToolBroker``**。本模块自己**不**碰 SeaTunnel、不碰目标库，
   连客户端都只在 ``clients/seatunnel.py`` 里被执行器使用（CLAUDE.md §5.1）。
   任务体做的是「准备参数 + 调 broker + 记结果」。
2. **令牌从载荷来**。``capability`` 表只存 ``token_sha256``，控制面也没有原文——
   唯一的来源是 Outbox 载荷。所以本模块的入参里必须有它，且**只准在本进程内存里存在**：
   不进日志、不进 ``failure_snapshot``、不回写数据库（有 ``test_run_secrets`` 守着）。
3. **幂等**：``task_acks_late`` 下重投是常态。第 ① 步就把「这个 run 已经在跑/已结束」
   挡掉——重复提交一次作业的代价是一个数据被写两遍的影子表，
   而影子表写两遍恰好**不会**报错（全量覆盖写），只会在 Swap 之后让人怀疑人生。
4. **失败要留证据**：任何异常都写 ``RUN_FAILED`` 事件 + 状态置 failed +
   ``error_message`` 只留脱敏后的类型与摘要（be-15 提供 ``redact`` 后换成它）。

**be-15 尚未落地**：``seatunnel.submit`` 的执行器由 ``clients/seatunnel.register()``
注册，那是 be-15 的交付物。在此之前本任务会以「工具未注册」失败——
这是**正确的失败**：状态、错误信息、账本三者都如实记录，而不是假装成功。
"""

from __future__ import annotations

from typing import Any, Final

from celery import shared_task
from sqlalchemy import select

from core.db import session_scope
from core.errors import AppError
from core.ids import utcnow
from core.logging import get_logger
from harness.broker import ExecutionContext, ToolBroker
from harness.ledger import EvidenceLedger, LedgerEvent
from harness.outbox import Topic, enqueue
from harness.replay import ReplayGuard
from harness.tools import SeatunnelStopArgs, SeatunnelSubmitArgs, SecretRefArg
from models.datasource import DataSource
from models.enums import EvidenceEventType, RunStatus, ToolName
from models.execution import ExecutionRun
from schemas.run import RunSubmitContext
from services import run as run_service
from tasks.async_runner import run_coro

log = get_logger(__name__)

_ledger = EvidenceLedger()

#: 一轮收尾最多处理多少条运行。上限而不是全量，理由与 ``tasks/supervision.py``
#: 的 ``BATCH_LIMIT`` 相同：每条都要发一次数据面 HTTP 请求、还可能各做一次目标端 DDL，
#: 全量扫会让一轮跑不完，于是「每 30 秒收尾一次」变成「一直没收完」。
FINALIZE_BATCH_LIMIT: Final[int] = 10


def _build_guard() -> Any:
    """``ReplayGuard`` 是 broker 的必填件；``seatunnel.*`` 是 P0 工具，确实要用它。"""
    from core.config import settings
    from core.redis import get_redis

    return ReplayGuard(redis=get_redis(), ttl_sec=settings.CAPABILITY_TTL_SEC)


def _broker() -> ToolBroker:
    """worker 侧的 broker：**有验签器、没有签发器**。

    刻意不传 ``issuer``：worker 被攻陷也签不出新令牌（``harness/broker.py`` 的同一取舍）。

    而 ``verifier`` **是必填的**：``_verify`` 在它为 ``None`` 时直接以
    ``INTERNAL_ERROR``「本进程未配置 Capability 校验公钥」拒掉——也就是本模块
    **每一个**工具（提交 / 停止）都会失败。这个缺口的症状特别会骗人：报文说
    「本进程未配置公钥」，读起来像配置漏了环境变量，实际是这个构造少传了一个具名参数；
    而单测用的是直接调执行器的假 broker，不走七步执行序，永远看不见它。
    """
    from harness.capability import verifier_from_settings

    return ToolBroker(guard=_build_guard(), ledger=_ledger, verifier=verifier_from_settings())


# ---------------------------------------------------------------- 提交


async def _submit(run_id: int, capability: str | None) -> dict[str, Any]:
    async with session_scope() as session:
        row = await session.get(ExecutionRun, run_id)
        if row is None:
            # 不抛：消息可能是给一个已被删除的 run 的（保留策略清理过）。
            # 抛出去会让 Celery 反复重投一条永远不可能成功的消息。
            log.warning("run_not_found", run_id=run_id)
            return {"run_id": run_id, "status": "missing"}

        # ① 幂等闸：只有 queued 才真的去提交。
        #    ``running`` → 作业已经在跑；``succeeded``/``failed``/``cancelled`` → 已有结论。
        #    这两种情况都直接返回既有事实，**不**重新签发、**不**重新提交。
        if row.status is not RunStatus.QUEUED:
            log.info("submit_skipped", run_id=run_id, status=row.status.value)
            return {"run_id": run_id, "status": row.status.value, "skipped": True}

        if not capability:
            return await _fail(session, row, "提交载荷里没有 Capability 令牌", EvidenceEventType.RUN_FAILED)

        ctx = await run_service.load_for_submit(session, run_id=run_id)

        # ② 先置 running 再提交：反过来的话，作业已经跑起来而库里还是 queued，
        #    这中间的任何一次重投都会再提交一次（幂等闸看的就是 status）。
        await run_service.apply_transition(
            session,
            run_id=run_id,
            target=RunStatus.RUNNING,
            reason="worker_submit",
            actor_id=None,
        )

        args = await _submit_args(session, ctx)
        broker = _broker()
        result = await broker.execute(
            ToolName.SEATUNNEL_SUBMIT,
            capability,
            args,
            ExecutionContext(
                request_id=f"run-{run_id}",
                tenant_id=ctx.tenant_id,
                project_id=ctx.project_id,
                # 执行主体＝发起这次运行的人。**可以从运行行上读**，因为提交令牌
                # 本来就是签给他的（``services/run.py`` 的 Commit 用 ``subject.user_id``
                # 签、同时把它写进 ``run.triggered_by``），broker 随后会拿它与令牌里的
                # ``subject_id`` 比对——对不上仍然是 binding 拒绝，这里不是「信任自己」。
                # 这与 ``_stop`` 必须从载荷取 ``subject_id`` **不是**同一条规矩：
                # 取消人 ≠ 发起人，在停止那边读 ``triggered_by`` 会把归因记错。
                actor_id=row.triggered_by,
                caller="worker",
                run_id=run_id,
            ),
            session=session,
        )

        if not result.ok:
            # broker 内部已写 ``capability.rejected``（或工具失败）证据，
            # 这里补的是**运行级**的结论，两者是不同的问题。
            detail = str(result.detail.get("error") or result.detail.get("message") or "工具执行失败")
            return await _fail(session, row, detail, EvidenceEventType.RUN_FAILED)

        job_id = str(result.detail.get("job_id") or "")
        row.seatunnel_job_id = job_id or None
        row.error_message = None

        await _ledger.append(
            session,
            LedgerEvent(
                tenant_id=row.tenant_id,
                project_id=row.project_id,
                actor_id=None,
                event_type=EvidenceEventType.RUN_STARTED,
                subject_type="run",
                subject_id=str(row.id),
                artifact_hash=ctx.artifact_sha256,
                payload={
                    "job_id": job_id,
                    # 只记作业名与目标表：它们是排查的起点，且都不是凭据
                    "job_name": run_service.run_job_name(
                        pipeline_id=ctx.pipeline_id,
                        version_no=ctx.version_no,
                        run_id=ctx.run_id,
                    ),
                    "shadow_table": ctx.shadow_table,
                    "formal_table": ctx.formal_table,
                },
                occurred_at=utcnow(),
            ),
        )

        # ③ 转监督：经 Outbox，而不是 ``.delay()``。
        #    直接用 delay 的话，这一步成为唯一一个「进程崩了就永久丢失」的副作用。
        await enqueue(
            session,
            topic=Topic.RUN_SUPERVISE,
            payload={"run_id": run_id},
            idempotency_key=f"supervise:run:{run_id}",
        )

    log.info("run_submitted", run_id=run_id, job_id=job_id)
    return {"run_id": run_id, "status": RunStatus.RUNNING.value, "job_id": job_id}


async def _submit_args(session: Any, ctx: RunSubmitContext) -> SeatunnelSubmitArgs:
    """组装 ``seatunnel.submit`` 的入参。

    ``secret_refs`` 来自**已冻结的准备单制品**（``resource_scope.secrets``），
    执行器会拿它去 Vault 取值。这里额外做一次「每个 ref 都还指向存在的数据源」的校验：
    引用本身可能指向一个已被删除的数据源，而 Vault 路径照样能取到值——
    于是作业带着一个不存在的源的凭据跑起来。这类错要在提交前拦掉。
    """
    ids = sorted({int(item.get("data_source_id") or 0) for item in ctx.secret_refs})
    if ids:
        alive = set(
            (
                await session.scalars(
                    select(DataSource.id).where(
                        DataSource.id.in_(ids), DataSource.tenant_id == ctx.tenant_id
                    )
                )
            ).all()
        )
        missing = [item for item in ids if item not in alive]
        if missing:
            raise AppError(f"凭据引用的数据源已不存在：{missing}")

    refs = tuple(
        SecretRefArg(
            provider=str(item.get("provider") or "vault"),
            path=str(item["path"]),
            version=int(item["version"]),
            key_names=tuple(item.get("key_names") or ()),
        )
        for item in ctx.secret_refs
    )
    return SeatunnelSubmitArgs(
        run_id=ctx.run_id,
        pipeline_version_id=ctx.pipeline_version_id,
        hocon_artifact_id=ctx.hocon_artifact_id,
        secret_refs=refs,
        shadow_table=ctx.shadow_table,
        error_table=ctx.error_table,
        supervision_contract_id=ctx.supervision_contract_id,
    )


async def _fail(
    session: Any, row: ExecutionRun, message: str, event: EvidenceEventType
) -> dict[str, Any]:
    """把一次提交失败如实地记下来：状态 + 消息 + 账本，三处都要有。"""
    row.error_message = message[:2000]
    row.failure_snapshot = {"stage": "submit", "error_type": message.split(":", 1)[0][:64]}
    await run_service.apply_transition(
        session,
        run_id=row.id,
        target=RunStatus.FAILED,
        reason="submit_failed",
        actor_id=None,
    )
    await _ledger.append(
        session,
        LedgerEvent(
            tenant_id=row.tenant_id,
            project_id=row.project_id,
            actor_id=None,
            event_type=event,
            subject_type="run",
            subject_id=str(row.id),
            payload={"stage": "submit", "message": message[:500]},
            occurred_at=utcnow(),
        ),
    )
    log.warning("run_submit_failed", run_id=row.id, message=message)
    return {"run_id": row.id, "status": RunStatus.FAILED.value, "error": message}


# ---------------------------------------------------------------- 停止


async def _stop(run_id: int, capability: str | None, reason: str, *, subject_id: int) -> dict[str, Any]:
    async with session_scope() as session:
        row = await session.get(ExecutionRun, run_id)
        if row is None:
            log.warning("run_not_found", run_id=run_id)
            return {"run_id": run_id, "status": "missing"}

        # 已经在终态的：什么都不做。停止是「为了让作业不再跑」，
        # 作业已经没了还去调一次 stop，SeaTunnel 那边只会回一句「查无此 job」——
        # 而那句话会被记成一次失败，把一条正常的取消写成了异常。
        if row.status in run_service.TERMINAL_STATES:
            return {"run_id": run_id, "status": row.status.value, "skipped": True}

        if not row.seatunnel_job_id:
            # 没有 job_id 说明作业根本没起来（提交失败 / 还没提交）。
            # 这时不需要也不能调 seatunnel.stop；状态由控制面那侧已经置成 cancelled。
            log.info("stop_no_job", run_id=run_id)
            return {"run_id": run_id, "status": row.status.value, "skipped": True, "reason": "no_job"}

        if not capability:
            return await _fail(session, row, "停止载荷里没有 Capability 令牌", EvidenceEventType.RUN_FAILED)

        result = await _broker().execute(
            ToolName.SEATUNNEL_STOP,
            capability,
            SeatunnelStopArgs(
                run_id=run_id,
                job_id=row.seatunnel_job_id,
                reason=_stop_reason(reason),
            ),
            ExecutionContext(
                request_id=f"stop-{run_id}",
                tenant_id=row.tenant_id,
                project_id=row.project_id,
                # 执行主体来自载荷（签发方写的就是令牌里签的那个 subject）。
                # 这里一度是 ``None``，而 broker 在 BINDING 阶段要求它非空——
                # 于是**人工取消与监督硬中断两条路发出的停作业命令全都会被拒收**。
                # 之所以一直没暴露：SeaTunnel 没起、这条链在真环境里没跑过，
                # 而单测用的是直接调执行器的假 broker，不走七步执行序。
                actor_id=subject_id,
                caller="worker",
                run_id=run_id,
            ),
            session=session,
        )
        if not result.ok:
            detail = str(result.detail.get("error") or result.detail.get("message") or "停止失败")
            # 停止失败**不改状态**：控制面已经把它标成 cancelled 了，而这次调用没能让
            # 作业真的停下——库里的结论与事实不一致，这个矛盾要留给人看（告警），
            # 而不是在这里悄悄把状态改回 running。
            log.warning("stop_failed", run_id=run_id, message=detail)
            return {"run_id": run_id, "status": row.status.value, "error": detail}

    log.info("run_stopped", run_id=run_id)
    return {"run_id": run_id, "status": "stopped"}


def _stop_reason(reason: str) -> Any:
    """把载荷里的自由文本收成 ``SeatunnelStopArgs.reason`` 的三个枚举值。

    收窄而不是原样透传：``reason`` 会进 SeaTunnel 停止命令的参数，也是监督判定
    「这次是谁要停的」的依据。自由文本一旦写进去，下游就只能靠字符串比较来分支。
    """
    if reason in ("supervision_interrupt", "manual_cancel", "pause"):
        return reason
    return "manual_cancel"


# ---------------------------------------------------------------- 收尾与回滚


async def _finalize_run(run_id: int) -> dict[str, Any]:
    """收尾一条运行：读数据面状态 → 出质量报告 →（合格时）原子发布。

    业务判定全在 ``services.finalize``（``tasks/`` 只做编排，CLAUDE.md §4.5）：
    这里只负责开一次会话、调一次、把结果打成日志。
    """
    from core.db import SessionFactory
    from services import finalize as finalize_service

    async with SessionFactory() as session:
        result = await finalize_service.sync_run_outcome(session, run_id=run_id)

    log.info(
        "run_finalize_tick",
        run_id=result.run_id,
        status=result.status.value,
        action=result.action,
        report_id=result.report_id,
        passed=result.passed,
        published=result.published,
        detail=result.detail,
    )
    return result.model_dump(mode="json")


async def _scan_finished(limit: int) -> dict[str, Any]:
    """扫一批待收尾的运行，逐个收尾。

    **不过 Outbox**：与 ``supervise_run``（提交后那一次必须不丢的一次性提示）不同，
    这里的触发条件是「运行还在 running/interrupted」这一**可重复观测的事实**——
    丢一次投递，下一轮 30 s 后照样会发现它。所以不需要 Outbox 的持久化，
    也就不用为它新造一个 topic（少一个 topic = 少一处「拼错了永远匹配不到消费者」）。
    """
    from services import finalize as finalize_service

    run_ids = await finalize_service.list_pending_run_ids(limit)
    tally: dict[str, Any] = {
        "pending": len(run_ids),
        "finalized": 0,
        "published": 0,
        "failed": 0,
        "orphaned": 0,
        "errors": 0,
    }
    for run_id in run_ids:
        try:
            item = await _finalize_run(run_id)
        except Exception as exc:
            # 单条运行的问题不带垮整批：一次收尾里既有数据库写也有目标端 DDL，
            # 让异常冒出循环外等于「这一轮排在后面的运行全部没人管」。
            tally["errors"] += 1
            log.warning("run_finalize_failed", run_id=run_id, error_type=type(exc).__name__)
            continue
        action = item.get("action")
        if action == "published":
            tally["published"] += 1
        elif action == "failed":
            tally["failed"] += 1
        elif action == "orphaned":
            tally["orphaned"] += 1
        if action in ("quality_finalized", "published", "failed"):
            tally["finalized"] += 1

    log.info("run_finalize_batch", **tally)
    return tally


async def _rollback(run_id: int, capability: str | None) -> dict[str, Any]:
    """回滚执行（``etl.target.rollback`` 的消费者）。

    租户从**运行行**读出（Outbox 载荷里没有它，而 beat/worker 也没有租户上下文）——
    与 ``run_service`` 里每一次「先取行、再用行上的 tenant_id 查其余」同一条规矩：
    让调用方传租户，就给了它一次传错的机会。
    """
    from core.db import SessionFactory
    from services import publish as publish_service

    async with SessionFactory() as session:
        row = await session.get(ExecutionRun, run_id)
        if row is None:
            # 消息可能指向一个已被保留策略清掉的运行：不抛，否则 Celery 会反复重投。
            log.warning("rollback_run_not_found", run_id=run_id)
            return {"run_id": run_id, "status": "missing"}
        tenant_id = row.tenant_id
        result = await publish_service.execute_rollback(
            session, tenant_id=tenant_id, run_id=run_id, capability=capability
        )

    log.info(
        "rollback_executed",
        run_id=run_id,
        status=result.status.value,
        already_rolled_back=result.already_rolled_back,
        attempt=result.attempt,
    )
    return result.model_dump(mode="json")


# ---------------------------------------------------------------- Celery 入口


@shared_task(name="tasks.execution.submit_run", bind=True, ignore_result=False)
def submit_run(self: Any, run_id: int, capability: str | None = None) -> dict[str, Any]:
    """提交一次受管执行（be-14 §5.4）。重复投递为幂等空操作。"""
    return run_coro(_submit(run_id, capability))


@shared_task(name="tasks.execution.stop_run", bind=True, ignore_result=False)
def stop_run(
    self: Any,
    run_id: int,
    reason: str = "manual_cancel",
    capability: str | None = None,
    subject_id: int | None = None,
) -> dict[str, Any]:
    """停作业（人工取消 / 监督硬中断）。

    ``subject_id`` 由投递方随载荷给出（``tasks/outbox.py::_args_for`` 缺它就
    不投）。这里给个默认 ``None`` 只是为了让 Celery 的签名可读，真走到
    ``_stop`` 时它是**必需**的——没有执行主体的停止命令在 broker 那关过不去。
    """
    if subject_id is None:
        raise ValueError("停止命令缺少执行主体 subject_id，拒绝执行")
    return run_coro(_stop(run_id, capability, reason, subject_id=subject_id))


@shared_task(name="tasks.execution.finalize_run", bind=True, ignore_result=False)
def finalize_run(self: Any, run_id: int) -> dict[str, Any]:
    """收尾一条运行（幂等：重复调用只会复用既有报告、短路既有 Swap 结论）。"""
    return run_coro(_finalize_run(run_id))


@shared_task(name="tasks.execution.scan_finished_runs", bind=True, ignore_result=False)
def scan_finished_runs(self: Any) -> dict[str, Any]:
    """周期性收尾（``beat_schedule`` 的 ``finalize-finished-runs``）。

    「作业跑完了」这件事只有轮询能发现——SeaTunnel 不会回调我们。这也是本任务
    与 ``collect_active_runs`` 的区别：那个抄的是**过程**指标，这个推的是**结论**。
    """
    return run_coro(_scan_finished(FINALIZE_BATCH_LIMIT))


@shared_task(name="tasks.execution.rollback_run", bind=True, ignore_result=False)
def rollback_run(self: Any, run_id: int, capability: str | None = None) -> dict[str, Any]:
    """执行受管回滚（``etl.target.rollback`` 的消费者）。

    在这之前这个 topic **没有消费者**：``services.publish.request_rollback`` 投出的
    命令会被 ``tasks/outbox.py`` 判成「未知 topic」→ ``dead``，而库里只留下一行
    ``last_error``。症状是「点了回滚，影子表没被清理，页面停在进行中」。
    """
    return run_coro(_rollback(run_id, capability))


__all__ = [
    "FINALIZE_BATCH_LIMIT",
    "finalize_run",
    "rollback_run",
    "scan_finished_runs",
    "stop_run",
    "submit_run",
]
