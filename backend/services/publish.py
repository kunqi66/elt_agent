"""原子发布与受管回滚（be-18）。

这是全系统**风险最高**的一段代码：它唯一做的事就是「把影子表换成正式表」，
而这个动作不可撤销、对业务立即可见。所以本模块的每一条规则都朝同一个方向倒：
**宁可拒绝，不许静默错**。

三条不可退让：

1. **失败路径绝不对正式表做任何写操作**。正式表在 Swap 之前从未被写过，
   所以「回滚」不是把数据改回去，而是「什么都没发布」。
2. **判定以执行后对账为准，不以 DDL 返回值为准**。DDL 超时、连接中断、客户端被杀
   都可能发生在「语句已生效」之后；把「没报错」当成功，正是「在未知态上叠加第二次
   Swap」的开端（第二次 Swap 是**对合变换**，会把新数据换回旧数据，且不报任何错）。
3. **表名只从 ``execution_run`` 读**。请求体里没有任何表名字段，回滚的四个范围条件
   合起来保证「一次误操作最多删掉一个影子表」。

与设计文档（``vibecoding_doc/模块概要设计/be-18-原子发布与受管回滚.md``）的偏差，
逐条登记在此，**不要静默改回**：

- **§5.1 第 6 步的执行在本模块内闭环，不经 Outbox**。设计 §6.6 提到「Outbox 重投同一条
  Swap 命令」，但 §4 的 outbox 行只登记了回滚命令；发布若也走投递，就要多一个投递跳数、
  多一个消费者注册点，而它换不来任何保证——发布的幂等由三层独立机制兜住：
  ① 本条 run 的 ``swap.succeeded`` / 已消费 Capability 短路；② 状态条件 UPDATE 抢占；
  ③ Capability 单次消费。反过来说，经 Outbox 投递反而引入「投递前有、消费前无」的窗口。
- **本模块在 worker 内签发 Capability**（``_capability_issuer``），因此 worker 必须挂载
  签名私钥。这与 be-05「worker 只持公钥、被攻陷也签不出新令牌」的取舍相冲突，是按
  §5.1「passed=True 后由 worker 自动发布」落地时**无法避免**的后果（发布没有 HTTP 入口，
  也就没有别的进程可以在质量门禁之后签发它）。缓解：发布与回滚各签一枚、TTL 300 s、
  绑定工具与制品指纹、单次消费；且签发点只有本模块的两处。
- **多出一个 worker 侧入口 ``execute_rollback``**。设计 §5.2 第 8 步（``ToolBroker.execute``）
  必须有落点，而工具规格里 ``target.rollback_cleanup`` 的 executor 是 ``worker``；
  API 只负责到「命令入队」。设计 §3.2 只列了三个入口，这是第四个。
- **加严：``pre_shadow == pre_formal`` 时判 ``unknown``**（设计 §6.2 的对账表没有这一行）。
  两张表指纹相同（例如同为空的同名结构）时，「交换生效了」与「什么都没发生」
  在任何观测下都不可区分——这时给出 ``published`` 会把一次没发生的发布记成成功。
- **加严：``request_rollback`` 的 ``project_id`` 从 run 行推导**，不取调用方传参
  （设计 §3.2 的签名里有 ``project_id``）。用调用方给的值去过滤，等于允许
  「A 项目的 operator 用 B 项目的 run_id」，而项目隔离正是这层权限的意义。
- **加严：不做「等 SeaTunnel 终态」的阻塞等待**（设计 §5.2 第 5 步）。HTTP 端点不该
  阻塞在一个外部系统的终态上；进入回滚的状态集合只有 ``failed`` / ``interrupted``，
  这两条路径上作业都已终止或从未提交。真正的「停作业」由 cancel 端点负责。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from structlog import get_logger

from clients.target import registry
from clients.target.base import (
    FinalState,
    TableInfo,
    TargetError,
    TargetPublisher,
    assert_publishable,
    reconcile_final_state,
)
from clients.target.executor import rollback_plan_digest
from core.config import settings
from core.errors import (
    AppError,
    ErrorCode,
    FingerprintMismatchError,
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
from harness.tools import TargetPublishSwapArgs, TargetRollbackCleanupArgs
from models.enums import EvidenceEventType, RoleName, RunStatus, ToolName
from models.evidence import EvidenceEvent
from models.execution import ExecutionRun, QualityReport
from models.governance import Capability
from schemas.auth import SubjectContext
from schemas.publish import (
    PublishResultDTO,
    PublishState,
    PublishStateDTO,
    RollbackResultDTO,
    RollbackStateDTO,
)
from services import preparation as preparation_service
from services import run as run_service
from services.pipeline import repository
from services.preparation import shadow_table_name
from services.project import active_roles

log = get_logger(__name__)

_ledger = EvidenceLedger()

#: 发布独占键的 TTL。取 Capability 的 TTL 而不是另立一项：两者回答的是同一个
#: 「这次动作还算数多久」，而多一个可调项就多一处配置漂移。
_PUBLISH_CLAIM_TTL_SEC = settings.CAPABILITY_TTL_SEC

#: 指纹口径。进证据 payload 与 DTO——不带这个词，读者会把
#: 「行数与主键集合一致」读成「内容一致」（§6.2 的已知局限）。
_FINGERPRINT_SCOPE = "rowcount+pk_crc32"

#: 回滚失败时用的证据类型。设计 §10 指出缺 ``rollback.failed`` 事件类型，
#: 建议在枚举里补；在补上之前用 ``run.failed`` + ``payload.kind="rollback"``。
_ROLLBACK_FAILED_EVENT = EvidenceEventType.RUN_FAILED

_issuer: CapabilityIssuer | None = None
_verifier: CapabilityVerifier | None = None


# ---------------------------------------------------------------- 依赖装配


def _capability_issuer() -> CapabilityIssuer:
    """Capability 签发器。懒加载：import 期不读密钥文件（理由见 ``services/run.py``）。"""
    global _issuer
    if _issuer is None:
        _issuer = CapabilityIssuer(
            private_key_path=settings.CAPABILITY_SIGNING_KEY_PATH,
            key_id=settings.CAPABILITY_KEY_ID,
            issuer=settings.CAPABILITY_ISSUER,
            ttl_sec=settings.CAPABILITY_TTL_SEC,
        )
    return _issuer


def _build_verifier() -> CapabilityVerifier:
    """校验器。**只读公钥**，与本进程的签发器分开构造。

    构造细节归 ``harness.capability.verifier_from_settings``（公钥路径怎么推、
    为什么不在配置里再写一份，见那里的 docstring）。本函数只负责**进程内缓存**：
    验签器持有公私钥句柄的懒加载状态，每次调用都重建会把「读 PEM 文件」这件事
    摊到每一次工具执行上。
    """
    global _verifier
    if _verifier is None:
        from harness.capability import verifier_from_settings

        _verifier = verifier_from_settings()
    return _verifier


def _build_guard() -> ReplayGuard:
    """``ReplayGuard``：Capability 单次消费的 Redis 半场（另一半是 PG 的 jti 唯一约束）。"""
    return ReplayGuard(redis=get_redis(), ttl_sec=settings.CAPABILITY_TTL_SEC)


def _broker() -> ToolBroker:
    """worker 侧的 broker：验签器**必须**给（本模块的工具都要 Capability）。

    签发器也给：发布与回滚的令牌就是本模块签的（见模块 docstring 的第 2 条偏差）。
    两个都给不是「方便」，而是这两条路径确实都发生在本进程内——
    把签发挪到别处，就得在质量门禁与令牌签发之间再插一次跨进程投递。
    """
    return ToolBroker(
        guard=_build_guard(),
        ledger=_ledger,
        verifier=_build_verifier(),
        issuer=_capability_issuer(),
    )


# ---------------------------------------------------------------- 载入


@dataclass(frozen=True, slots=True)
class _RunFacts:
    """发布/回滚判定要的全部控制面事实。**不含任何凭据**（连接信息归 registry）。"""

    run: ExecutionRun
    preparation: Any
    version: Any
    quality_report: QualityReport | None


async def _load_run(session: AsyncSession, *, tenant_id: int, run_id: int) -> ExecutionRun:
    """取运行行，**强制带 tenant_id**。

    跨租户一律 404 而不是 403：与 ``services/run.py`` 的同一取舍——403 等于告诉
    攻击者「这个 id 存在，只是不属于你」，而 404 什么都不说。
    """
    row = await session.scalar(
        select(ExecutionRun).where(
            ExecutionRun.id == run_id, ExecutionRun.tenant_id == tenant_id
        )
    )
    if row is None:
        raise NotFoundError(f"运行 {run_id} 不存在")
    return row


async def _load_facts(session: AsyncSession, *, run: ExecutionRun) -> _RunFacts:
    preparation = await preparation_service.load_preparation(
        session, tenant_id=run.tenant_id, preparation_id=run.preparation_id
    )
    version = await repository.load_version(
        session=session, tenant_id=run.tenant_id, version_id=run.pipeline_version_id
    )
    report = await _quality_report(session, run_id=run.id)
    return _RunFacts(run=run, preparation=preparation, version=version, quality_report=report)


async def _quality_report(session: AsyncSession, *, run_id: int) -> QualityReport | None:
    """本 run 的质量报告（``uq_quality_report_run_id`` 保证至多一条）。"""
    return await session.scalar(
        select(QualityReport).where(QualityReport.run_id == run_id).limit(1)
    )


def _assert_artifact_matches(facts: _RunFacts) -> None:
    """``preparation.artifact_sha256 == pipeline_version.sha256``（§6.1 第 4 条）。

    两个值都取自数据库、都没有经过调用方——这条断言的用途是发现**控制面自身的
    不一致**（准备单与版本行指向了不同制品）。它一旦不等，「发布的是不是被审批过的
    那一版」这个问题就没有答案，因此不许继续。
    """
    expected = facts.version.sha256 or ""
    actual = facts.preparation.artifact_sha256 or ""
    if actual != expected:
        raise FingerprintMismatchError(
            "准备单的制品指纹与冻结版本不一致，拒绝发布/回滚",
            details={"reason": "artifact_sha256_mismatch", "run_id": facts.run.id},
        )


async def _latest_event(
    session: AsyncSession, *, tenant_id: int, run_id: int, event_types: tuple[EvidenceEventType, ...]
) -> EvidenceEvent | None:
    """本 run 最近一条指定类型的事件。

    按 ``seq`` 而不是 ``occurred_at`` 排序：同一个事务内落的多条事件时间戳可以相同，
    而 ``seq`` 是账本的链序，它是唯一的全序（``harness/ledger.py`` 的同一约定）。
    """
    return await session.scalar(
        select(EvidenceEvent)
        .where(
            EvidenceEvent.tenant_id == tenant_id,
            EvidenceEvent.subject_type == "run",
            EvidenceEvent.subject_id == str(run_id),
            EvidenceEvent.event_type.in_([event.value for event in event_types]),
        )
        .order_by(EvidenceEvent.seq.desc())
        .limit(1)
    )


async def _consumed_publish_capability(session: AsyncSession, *, preparation_id: int) -> bool:
    """本准备单上是否已有一枚**被执行器消费过**的发布令牌。

    ``consumed_at`` 由 broker 在调用执行器**之前**写下（``harness/replay.py::consume``），
    而且与证据、Outbox 同事务提交。因此它的含义比任何事件都早、都硬：

    ====================  ==========================================================
    观察                  结论
    ====================  ==========================================================
    有令牌、未消费        签发后到执行前就中断了 → 目标端**一个字都没动**，可安全重试
    无令牌                从未走到签发 → 可安全重试
    **有令牌、已消费**    DDL 已经发出去了，而结论可能还没落账 → **绝不许再发一次**
    ====================  ==========================================================

    第三行是本模块最重要的一条护栏：Swap 是**对合变换**，第二次执行会把新数据换回旧数据，
    而且不报任何错。没有这一条，「DDL 成功但证据落账前进程被杀」之后的重投会静默地
    把刚发布的正式表退回去。
    """
    found = await session.scalar(
        select(Capability.id).where(
            Capability.preparation_id == preparation_id,
            Capability.tool == ToolName.TARGET_PUBLISH_SWAP.value,
            Capability.consumed_at.is_not(None),
        )
    )
    return found is not None


# ---------------------------------------------------------------- 目标端


async def _target_facts(session: AsyncSession, *, tenant_id: int, run_id: int) -> registry.RunTargetFacts:
    """目标端连接与绑定事实。表名、库名、凭据全在这一条链上解析（都不来自调用方）。"""
    return await registry.load_run_target(session, tenant_id=tenant_id, run_id=run_id)


def _publisher(facts: registry.RunTargetFacts) -> TargetPublisher:
    return registry.build_publisher(facts.binding.endpoint)


async def _observe(publisher: TargetPublisher, table: str) -> TableInfo:
    """一次只读观测。失败**不吞**：观测不到指纹时，「公布成功」是最坏的选项。"""
    return await publisher.describe(table)


async def _observe_quietly(
    publisher: TargetPublisher, table: str
) -> tuple[TableInfo | None, str | None]:
    """容错的观测，供事后对账用。返回 ``(观测, 失败摘要)``——两者恰有一个非空。"""
    try:
        return await _observe(publisher, table), None
    except TargetError as exc:
        return None, f"{type(exc).__name__}: {exc.message}"


# ---------------------------------------------------------------- 发布


async def publish_run(session: AsyncSession, *, tenant_id: int, run_id: int) -> PublishResultDTO:
    """发布：门禁 → 抢占 → 签发 → 执行 → 对账 → 状态与证据。

    调用点是 ``tasks/execution.py`` 里「装载完成 + ``quality_report.passed``」之后
    （PRD §14.1 第 10 步）。**没有 HTTP 入口**：把「发布」做成一个按钮，等于把
    一次 P0 动作的触发权交给任何能点按钮的人，而不是交给质量门禁。

    返回的 DTO 是这次尝试的唯一摘要；异常一律在写入状态与证据**之后**抛出，
    所以「用户看到失败」与「库里记着失败」不会脱节。
    """
    now = utcnow()
    run = await _load_run(session, tenant_id=tenant_id, run_id=run_id)

    # ① 幂等短路：本 run 已经有过一次 Swap 尝试，就**不再执行第二次**。
    #    没有人工重试入口（§10），所以「重投」只会来自 Celery 的至少一次投递——
    #    而重投执行第二次 Swap 的后果是把正式表换回旧数据。
    prior = await _latest_event(
        session,
        tenant_id=tenant_id,
        run_id=run_id,
        event_types=(EvidenceEventType.SWAP_SUCCEEDED, EvidenceEventType.SWAP_FAILED),
    )
    if prior is not None:
        return _result_from_event(run=run, event=prior)

    facts = await _load_facts(session, run=run)
    if await _consumed_publish_capability(session, preparation_id=run.preparation_id):
        # 令牌被消费过、却没有结论事件：DDL 已经发出去了，结论没落账。
        # 这时唯一安全的动作是**什么都不做**并把它标成未决。
        log.error(
            "publish_attempt_without_verdict",
            run_id=run_id,
            shadow_table=run.shadow_table,
            formal_table=run.formal_table,
        )
        await _fail_run(
            session,
            run=run,
            facts=facts,
            now=now,
            final_state="unknown",
            reason="prior_attempt_without_verdict",
            error_code=ErrorCode.SWAP_FAILED,
        )
        await session.commit()
        raise AppError(
            "本次运行已经执行过一次原子交换但没有留下结论，需人工核对目标端后处理",
            code=ErrorCode.SWAP_FAILED,
            http_status=500,
            details={"reason": "prior_attempt_without_verdict", "run_id": run_id},
        )

    # ② 抢占：条件 UPDATE。未命中说明状态已经变了——**先看清是什么状态，再决定**，
    #    而不是覆盖它（并发下「先查后写」必然双放行）。
    await _claim_for_publish(session, run=run, now=now)
    # ③ 制品一致性（§6.1 第 4 条）：准备单与冻结版本必须指向同一份制品。
    #    放在质量门禁**之前**——它发现的是控制面自身的不一致，比质量结论更根本。
    _assert_artifact_matches(facts)

    # ③ 质量门禁（§6.1 第 3 条）。**快速失败 + 友好错误码**；broker 的
    #    ``requires_quality_pass`` 是兜底，防的是「以后有人新增调用路径绕过这里」。
    report = facts.quality_report
    if report is None or not report.passed:
        reason = "quality_report_missing" if report is None else "quality_not_passed"
        await _fail_run(
            session,
            run=run,
            facts=facts,
            now=now,
            final_state=None,
            reason=reason,
            error_code=ErrorCode.QUALITY_REJECTED,
        )
        await session.commit()
        # 未签发令牌 → 「未通过质量却执行 Swap」在令牌层面即不可达（§6.1 末段）
        raise AppError(
            "质量契约未通过，禁止发布",
            code=ErrorCode.QUALITY_REJECTED,
            http_status=409,
            details={"reason": reason, "run_id": run_id},
        )

    # ④ 一次发布独占（Redis SETNX）。进程内的两路并发（例：Celery 双投）靠它互斥；
    #    这是唯一一处真正的互斥，状态条件 UPDATE 只是「状态还算不算数」的检查，
    #    因为它把 status 从 running 写成 running，本身不是排他操作。
    #    用 ``finally`` 释放：临界区里的每一条失败路径都不该靠「记得手动放锁」来兜。
    await _acquire_publish_claim(tenant_id=tenant_id, run_id=run_id)
    try:
        return await _publish_claimed(session, run=run, facts=facts, now=now)
    finally:
        await _release_publish_claim(tenant_id=tenant_id, run_id=run_id)


async def _publish_claimed(
    session: AsyncSession, *, run: ExecutionRun, facts: _RunFacts, now: datetime
) -> PublishResultDTO:
    """抢占成功之后的全部动作：预检 → 签发 → 执行 → 对账 → 结论。

    与 ``publish_run`` 分开，是为了让「独占键的获取与释放」在源码上包住同一段代码——
    读者一眼就能看出临界区从哪到哪，而不用逐条失败路径去数有没有放锁。
    """
    report = facts.quality_report
    if report is None:  # pragma: no cover - 调用方已拦下
        raise ValidationError("质量报告缺失，禁止发布", details={"run_id": run.id})

    # ⑤ 目标端事实 + 只读预检（§6.1 第 5 条）。预检不过 → 正式表连一条写命令都没见过。
    target = await _target_facts(session, tenant_id=run.tenant_id, run_id=run.id)
    publisher = _publisher(target)
    try:
        pre_shadow = await _observe(publisher, target.shadow_table)
        pre_formal = await _observe(publisher, target.formal_table)
        assert_publishable(
            pre_shadow, pre_formal, allow_empty_shadow=target.read_rows == 0
        )
    except TargetError as exc:
        await _fail_run(
            session,
            run=run,
            facts=facts,
            now=now,
            final_state="not_published",
            reason=str(exc.details.get("reason") or "target_precheck_failed"),
            error_code=exc.code,
            extra={"target": target.binding.kind},
        )
        await session.commit()
        raise

    # ⑥ 签发 Capability（§6.1 第 6 条）。只在门禁与预检都过了之后才签——
    #    「门禁失败时不签发令牌」比「事后拒绝」更强。
    token = await _issue(
        session,
        facts=facts,
        tool=ToolName.TARGET_PUBLISH_SWAP,
        subject_id=run.triggered_by,
        now=now,
        extra_payload={"shadow_table": run.shadow_table, "formal_table": run.formal_table},
    )

    # ⑦ 执行（jti 在这里被原子消费；DDL 只发生在执行器里）
    args = TargetPublishSwapArgs(
        run_id=run.id,
        shadow_table=target.shadow_table,
        formal_table=target.formal_table,
        quality_report_id=report.id,
    )
    try:
        result = await _broker().execute(
            ToolName.TARGET_PUBLISH_SWAP,
            token,
            args,
            _publish_ctx(run),
            session=session,
        )
    except AppError as exc:
        # 协议类拒绝（令牌过期/重放/准备单已过期/broker 的质量兜底）：broker 已经写了
        # CAPABILITY_REJECTED，这里只补「这次运行的结论」——否则 run 会永远停在 running。
        await _fail_run(
            session,
            run=run,
            facts=facts,
            now=now,
            final_state="not_published",
            reason=str(exc.code),
            error_code=exc.code,
        )
        await session.commit()
        raise

    # ⑧ 执行后对账（§6.2）。**服务端自己再观测一次**，与执行器报回的状态互为交叉验证：
    #    两个独立观测一致才算数，不一致说明有我们没理解的事情，判 unknown。
    post_formal, formal_error = await _observe_quietly(publisher, target.formal_table)
    post_shadow, shadow_error = await _observe_quietly(publisher, target.shadow_table)
    final_state, reconcile_note = _reconcile(
        pre_shadow=pre_shadow,
        pre_formal=pre_formal,
        post_formal=post_formal,
        post_shadow=post_shadow,
        formal_error=formal_error,
        shadow_error=shadow_error,
        tool_state=str(result.detail.get("state") or ""),
    )
    post_fingerprint = None if post_formal is None else post_formal.table_fingerprint

    # ⑨ 结论落到状态与账本
    if final_state == "published":
        await run_service.apply_transition(
            session,
            run_id=run.id,
            target=RunStatus.SUCCEEDED,
            reason="publish_swap_succeeded",
            actor_id=None,
            now=now,
        )
        await _ledger.append(
            session,
            LedgerEvent(
                tenant_id=run.tenant_id,
                project_id=run.project_id,
                actor_id=run.triggered_by,
                event_type=EvidenceEventType.SWAP_SUCCEEDED,
                subject_type="run",
                subject_id=str(run.id),
                artifact_hash=facts.preparation.artifact_sha256,
                payload={
                    "run_id": run.id,
                    "shadow_table": target.shadow_table,
                    "formal_table": target.formal_table,
                    "final_state": final_state,
                    "pre_fingerprint": pre_shadow.table_fingerprint,
                    "post_fingerprint": post_fingerprint,
                    "fingerprint_scope": _FINGERPRINT_SCOPE,
                    "actor_id": run.triggered_by,
                },
                occurred_at=now,
            ),
        )
        await session.commit()
        log.info(
            "publish_succeeded",
            run_id=run.id,
            shadow_table=target.shadow_table,
            formal_table=target.formal_table,
            pre_fingerprint=pre_shadow.table_fingerprint,
            post_fingerprint=post_fingerprint,
            reconcile_note=reconcile_note,
        )
        return PublishResultDTO(
            run_id=run.id,
            status=RunStatus.SUCCEEDED,
            publish_state="published",
            final_state="published",
            shadow_table=target.shadow_table,
            formal_table=target.formal_table,
            pre_fingerprint=pre_shadow.table_fingerprint,
            post_fingerprint=post_fingerprint,
            fingerprint_scope=_FINGERPRINT_SCOPE,
            message="发布成功：正式表已是本次数据",
            occurred_at=now,
        )

    await _fail_run(
        session,
        run=run,
        facts=facts,
        now=now,
        final_state=final_state,
        reason=reconcile_note or "swap_not_effective",
        error_code=ErrorCode.SWAP_FAILED,
        extra={
            "shadow_table": target.shadow_table,
            "formal_table": target.formal_table,
            "pre_fingerprint": pre_shadow.table_fingerprint,
            "post_fingerprint": post_fingerprint,
            "fingerprint_scope": _FINGERPRINT_SCOPE,
        },
    )
    await session.commit()
    log.error(
        "publish_failed",
        run_id=run.id,
        final_state=final_state,
        reason=reconcile_note,
        formal_error=formal_error,
        shadow_error=shadow_error,
    )
    raise AppError(
        f"原子发布未生效（对账结论 {final_state}）",
        code=ErrorCode.SWAP_FAILED,
        http_status=500,
        details={"reason": reconcile_note or "swap_not_effective", "final_state": final_state},
    )


def _reconcile(
    *,
    pre_shadow: TableInfo,
    pre_formal: TableInfo,
    post_formal: TableInfo | None,
    post_shadow: TableInfo | None,
    formal_error: str | None,
    shadow_error: str | None,
    tool_state: str,
) -> tuple[str, str]:
    """服务端对账。返回 ``(final_state, 备注)``。

    判定表见 §6.2；加严的一条（``pre_shadow == pre_formal`` → ``unknown``）在
    ``clients.target.base.reconcile_final_state`` 里，两处共用同一份实现。

    **执行器报回的状态只用来交叉验证**：它与服务端观测不一致时判 ``unknown``——
    两个独立观测分歧时，「其中一个是对的」不是可以赌的假设。
    """
    if post_formal is None or post_shadow is None:
        return "unknown", "reconcile_unavailable"
    final_state = reconcile_final_state(
        pre_shadow=pre_shadow.table_fingerprint,
        pre_formal=pre_formal.table_fingerprint,
        post_formal=post_formal.table_fingerprint,
        post_shadow=post_shadow.table_fingerprint,
    )
    if tool_state and tool_state != final_state:
        log.error(
            "publish_reconcile_disagreement",
            tool_state=tool_state,
            service_state=final_state,
            formal_error=formal_error,
            shadow_error=shadow_error,
        )
        return "unknown", "reconcile_disagreement"
    return final_state, ""


async def _claim_for_publish(session: AsyncSession, *, run: ExecutionRun, now: datetime) -> None:
    """状态抢占（§5.1 第 2 步 + 其「非法转移的处理」段）。

    ``WHERE status='running'`` 未命中时**读当前状态决定语义**，而不是一律报错：
    ``succeeded`` 上的重复发布是一次幂等成功（Celery 重投会走到这里），
    其余状态各有各的原因，报出来的 ``reason`` 要能让人立刻知道该做什么。

    非幂等的冲突会写一条 ``run.failed``（§7 的「run 状态不允许发布」行），
    并由本函数**自己提交**——调用方随即抛异常，事务会在会话关闭时被回滚，
    不在这里提交等于「设计说会写证据，实际什么都没留下」。
    """
    result = await session.execute(
        update(ExecutionRun)
        .where(
            ExecutionRun.id == run.id,
            ExecutionRun.tenant_id == run.tenant_id,
            ExecutionRun.project_id == run.project_id,
            ExecutionRun.status == RunStatus.RUNNING,
        )
        .values(updated_at=now)
    )
    if result.rowcount:  # type: ignore[attr-defined]
        return
    current = await session.scalar(select(ExecutionRun.status).where(ExecutionRun.id == run.id))
    if current is None:
        # 抢占 UPDATE 影响 0 行、紧接着 SELECT 又查不到：这一行在两次语句之间被删了。
        # 只可能来自人工清库或级联删除；此时没有「当前状态」可读，如实报一次冲突。
        raise RunConflictError(
            "运行记录已不存在，无法发布", details={"reason": "run_missing", "run_id": run.id}
        )
    if current is RunStatus.SUCCEEDED:
        # 幂等：不写证据。这条路径的正常来源是重投，把它记成「有人试图非法发布」
        # 会让审计里出现一堆并不存在的越权尝试。
        raise RunConflictError(
            "本次运行已经发布成功，重复发布被忽略",
            details={"reason": "already_published", "run_id": run.id},
        )
    reason = {
        RunStatus.QUEUED: "run_not_started",
        RunStatus.ROLLED_BACK: "run_rolled_back",
    }.get(current, "run_not_publishable")
    await _ledger.append(
        session,
        LedgerEvent(
            tenant_id=run.tenant_id,
            project_id=run.project_id,
            actor_id=run.triggered_by,
            event_type=EvidenceEventType.RUN_FAILED,
            subject_type="run",
            subject_id=str(run.id),
            payload={
                "kind": "publish",
                "run_id": run.id,
                "reason": reason,
                "status": getattr(current, "value", str(current)),
                "shadow_table": run.shadow_table,
                "formal_table": run.formal_table,
            },
            occurred_at=now,
        ),
    )
    await session.commit()
    raise RunConflictError(
        f"运行状态为 {getattr(current, 'value', current)}，不允许发布",
        details={"reason": reason, "run_id": run.id},
    )


async def _acquire_publish_claim(*, tenant_id: int, run_id: int) -> None:
    """Redis SETNX 独占。**失败即拒绝**（与 ``harness/replay.py`` 同一取舍）。"""
    from redis.exceptions import RedisError

    key = f"elt:publish:claim:{tenant_id}:{run_id}"
    try:
        got = await get_redis().set(key, "1", nx=True, ex=_PUBLISH_CLAIM_TTL_SEC)
    except RedisError as exc:
        raise AppError(
            "发布独占检查不可用（Redis 不可达），已拒绝执行",
            code=ErrorCode.INTERNAL_ERROR,
            http_status=500,
        ) from exc
    if not got:
        raise RunConflictError(
            "本次运行已有一次发布正在进行",
            details={"reason": "publish_in_progress", "run_id": run_id},
        )


async def _release_publish_claim(*, tenant_id: int, run_id: int) -> None:
    """释放独占键。**失败只记日志**：键有 TTL，最坏结果是「晚一会儿才能重试」。"""
    from redis.exceptions import RedisError

    try:
        await get_redis().delete(f"elt:publish:claim:{tenant_id}:{run_id}")
    except RedisError:
        log.warning("publish_claim_release_failed", run_id=run_id)


async def _issue(
    session: AsyncSession,
    *,
    facts: _RunFacts,
    tool: ToolName,
    subject_id: int | None,
    now: datetime,
    extra_payload: dict[str, Any] | None = None,
) -> str:
    """签发一枚绑定制品与工具的 Capability，落 ``capability`` 行 + ``capability.issued``。

    返回**令牌原文**——这是它唯一的存续方式：既不落库（表里只有 ``token_sha256``），
    也不进任何 DTO 或日志。返回值而不是模块级缓存，是因为「最近一枚令牌」这种
    进程级状态在同进程并发签发时会被覆盖，而被覆盖的那一刻正好是拿错令牌去执行的时候。
    """
    if subject_id is None:
        raise ValidationError(
            "本次运行没有触发人，无法签发令牌（subject 是令牌绑定的一部分）",
            details={"run_id": facts.run.id, "tool": tool.value},
        )
    issued = _capability_issuer().issue(
        preparation_id=facts.preparation.id,
        subject_id=subject_id,
        tool=tool,
        env=facts.preparation.environment,
        artifact_sha256=facts.preparation.artifact_sha256,
        tenant_id=facts.run.tenant_id,
        project_id=facts.run.project_id,
        now=now,
    )
    session.add(
        Capability(
            jti=issued.claims.jti,
            preparation_id=facts.preparation.id,
            subject_id=subject_id,
            tool=issued.claims.tool.value,
            environment=facts.preparation.environment,
            artifact_sha256=facts.preparation.artifact_sha256,
            token_sha256=issued.token_sha256,
            signature_alg="EdDSA",
            key_id=settings.CAPABILITY_KEY_ID,
            issued_at=issued.claims.iat,
            expires_at=issued.claims.exp,
        )
    )
    await session.flush()
    await _ledger.append(
        session,
        LedgerEvent(
            tenant_id=facts.run.tenant_id,
            project_id=facts.run.project_id,
            actor_id=subject_id,
            event_type=EvidenceEventType.CAPABILITY_ISSUED,
            subject_type="capability",
            subject_id=str(issued.claims.jti),
            artifact_hash=facts.preparation.artifact_sha256,
            payload={
                "preparation_id": facts.preparation.id,
                "tool": tool.value,
                "run_id": facts.run.id,
                "ttl_sec": settings.CAPABILITY_TTL_SEC,
                **(extra_payload or {}),
            },
            occurred_at=now,
        ),
    )
    return issued.token


def _publish_ctx(run: ExecutionRun) -> ExecutionContext:
    return ExecutionContext(
        request_id=f"publish-run-{run.id}",
        tenant_id=run.tenant_id,
        project_id=run.project_id,
        actor_id=run.triggered_by,
        caller="worker",
        run_id=run.id,
    )


def _rollback_ctx(run: ExecutionRun, *, actor_id: int | None) -> ExecutionContext:
    return ExecutionContext(
        request_id=f"rollback-run-{run.id}",
        tenant_id=run.tenant_id,
        project_id=run.project_id,
        actor_id=actor_id,
        caller="worker",
        run_id=run.id,
    )


async def _fail_run(
    session: AsyncSession,
    *,
    run: ExecutionRun,
    facts: _RunFacts,
    now: datetime,
    final_state: str | None,
    reason: str,
    error_code: ErrorCode,
    extra: dict[str, Any] | None = None,
) -> None:
    """把一次失败的发布写成「状态 + 现场 + 证据」三件套（§6.3 的失败处理）。

    ``failure_snapshot["swap"]`` 是**唯一**能告诉事后排查者「这次到底有没有换成」
    的地方；只写一句 error_message 的话，「失败」与「失败但已生效」看起来一样。
    """
    snapshot = dict(run.failure_snapshot or {})
    snapshot["swap"] = {
        "status": final_state or "not_attempted",
        "reason": reason,
        "error_code": str(error_code),
        "at": now.isoformat(),
        "fingerprint_scope": _FINGERPRINT_SCOPE,
        **(extra or {}),
    }
    run.failure_snapshot = snapshot
    run.error_message = f"原子发布失败（{reason}）"[:1000]
    await run_service.apply_transition(
        session,
        run_id=run.id,
        target=RunStatus.FAILED,
        reason=f"publish_failed:{reason}",
        actor_id=None,
        now=now,
    )
    await _ledger.append(
        session,
        LedgerEvent(
            tenant_id=run.tenant_id,
            project_id=run.project_id,
            actor_id=run.triggered_by,
            event_type=EvidenceEventType.SWAP_FAILED,
            subject_type="run",
            subject_id=str(run.id),
            artifact_hash=facts.preparation.artifact_sha256,
            payload={
                "run_id": run.id,
                "shadow_table": run.shadow_table,
                "formal_table": run.formal_table,
                "reason": reason,
                "error_code": str(error_code),
                **({"final_state": final_state} if final_state else {}),
                **(extra or {}),
            },
            occurred_at=now,
        ),
    )
    # 证据事件是有序链（harness/ledger.py），所以失败路径也**必须** flush 之后再抛，
    # 否则「异常先到、账本后到」的空档里，恰好是这条 run 被另一个请求捡走的时候。
    await session.flush()


def _result_from_event(*, run: ExecutionRun, event: EvidenceEvent) -> PublishResultDTO:
    """由既有事件复原结论（幂等短路用）。**只读事件，不动状态。**"""
    payload = dict(event.payload or {})
    succeeded = event.event_type is EvidenceEventType.SWAP_SUCCEEDED
    final_state = str(payload.get("final_state") or ("published" if succeeded else "unknown"))
    state = {
        "published": "published",
        "not_published": "failed",
    }.get(final_state, "unknown")
    return PublishResultDTO(
        run_id=run.id,
        status=run.status,
        publish_state=state,  # type: ignore[arg-type]
        final_state=final_state,  # type: ignore[arg-type]
        shadow_table=str(payload.get("shadow_table") or run.shadow_table or ""),
        formal_table=str(payload.get("formal_table") or run.formal_table or ""),
        pre_fingerprint=payload.get("pre_fingerprint"),
        post_fingerprint=payload.get("post_fingerprint"),
        fingerprint_scope=str(payload.get("fingerprint_scope") or _FINGERPRINT_SCOPE),
        message=(
            "本次运行已经发布过（重复请求返回既有结论）"
            if succeeded
            else f"本次运行已经有过一次发布尝试（结论 {final_state}），不再自动重试"
        ),
        occurred_at=as_utc(event.occurred_at),
    )


# ---------------------------------------------------------------- 受管回滚


async def request_rollback(
    session: AsyncSession,
    *,
    subject: SubjectContext,
    run_id: int,
    idempotency_key: str,
    reason: str = "",
) -> RollbackResultDTO:
    """回滚入口（API 侧）：校验 → 抢占 → 签发 → 入队。**不执行任何目标端动作**。

    「回滚」在这里只被受理，不被执行：目标端 DDL 的副作用只能经 ``ToolBroker`` 在
    worker 里发生（CLAUDE.md §5.1）。所以本函数结束时的状态是 ``rolling_back`` +
    一条 ``etl.target.rollback`` 待发命令，而不是 ``rolled_back``。
    """
    if not idempotency_key:
        # 与 ``services/run.commit`` 同一条理由：命令的 ``idempotency_key`` 需要
        # 一个有语义的值，用随机值兜底会让「重复点击」变成两条真实命令。
        raise ValidationError("回滚必须携带 Idempotency-Key 请求头")

    now = utcnow()
    run = await _load_run(session, tenant_id=subject.tenant_id, run_id=run_id)
    await _require_operator(
        session, project_id=run.project_id, user_id=subject.user_id, now=now
    )

    # ① 已经回滚过 → 幂等返回既有结论，**不重新入队**（第二层幂等见 §6.5）
    if run.status is RunStatus.ROLLED_BACK:
        return RollbackResultDTO(
            run_id=run.id,
            status=run.status,
            already_rolled_back=True,
            attempt=await _rollback_attempt(session, run=run),
            shadow_table=run.shadow_table or "",
            formal_table=run.formal_table or "",
            message="本次运行已经回滚过（影子表已清理）",
        )

    facts = await _load_facts(session, run=run)
    _assert_artifact_matches(facts)
    await _assert_rollback_scope(session, run=run, version_no=facts.version.version_no)

    # ② 抢占（§6.5 唯一一次生效的成功路径）：``status IN ('failed','interrupted')``。
    #    未命中时**读状态**，四条分支各有各的处置与 reason。
    attempt = await _preempt_for_rollback(session, run=run, now=now)

    # ③ 签发回滚令牌并投递命令。令牌随 Outbox 走——那是 worker 唯一的来源
    #    （``capability`` 表只存哈希，控制面也没有原文）。
    token = await _issue(
        session,
        facts=facts,
        tool=ToolName.TARGET_ROLLBACK_CLEANUP,
        subject_id=subject.user_id,
        now=now,
        extra_payload={
            "shadow_table": run.shadow_table,
            "formal_table": run.formal_table,
            "attempt": attempt,
            "reason": reason[:255],
        },
    )
    await outbox_enqueue(
        session,
        topic=Topic.TARGET_ROLLBACK,
        payload={"run_id": run.id, "capability": token},
        idempotency_key=f"rollback:run:{run.id}:a{attempt}",
    )
    await _ledger.append(
        session,
        LedgerEvent(
            tenant_id=run.tenant_id,
            project_id=run.project_id,
            actor_id=subject.user_id,
            event_type=EvidenceEventType.ROLLBACK_STARTED,
            subject_type="run",
            subject_id=str(run.id),
            artifact_hash=facts.preparation.artifact_sha256,
            payload={
                "run_id": run.id,
                "shadow_table": run.shadow_table,
                "formal_table": run.formal_table,
                "attempt": attempt,
                "reason": reason[:255],
                "actor_id": subject.user_id,
            },
            occurred_at=now,
        ),
    )
    await session.commit()
    log.info(
        "rollback_requested",
        run_id=run.id,
        attempt=attempt,
        actor_id=subject.user_id,
        shadow_table=run.shadow_table,
        request_id=subject.request_id,
    )
    return RollbackResultDTO(
        run_id=run.id,
        status=RunStatus.ROLLING_BACK,
        already_rolled_back=False,
        attempt=attempt,
        shadow_table=run.shadow_table or "",
        formal_table=run.formal_table or "",
        message="回滚已受理：影子表清理命令已入队",
    )


async def execute_rollback(
    session: AsyncSession, *, tenant_id: int, run_id: int, capability: str | None = None
) -> RollbackResultDTO:
    """回滚执行（worker 侧）：调工具删影子表，并**独立观测**正式表是否被改动。

    「删掉影子表」这件事本身幂等（表不在就什么都不做），所以本函数可重复执行；
    真正的风险不在删，而在**误删**——因此范围校验（``_assert_rollback_scope``）在
    API 侧就已拦死，执行侧只认 ``execution_run`` 行里的两个表名。
    """
    now = utcnow()
    run = await _load_run(session, tenant_id=tenant_id, run_id=run_id)

    if run.status is RunStatus.ROLLED_BACK:
        # 重投的第二半场：命令到的时候已经回滚完了
        return RollbackResultDTO(
            run_id=run.id,
            status=run.status,
            already_rolled_back=True,
            attempt=await _rollback_attempt(session, run=run),
            shadow_table=run.shadow_table or "",
            formal_table=run.formal_table or "",
            message="本次运行已经回滚过",
        )
    if run.status is not RunStatus.ROLLING_BACK:
        # 例：抢占之后又被人工 cancel。不清影子表也不算失败——没有「应该回滚」的事实。
        raise RunConflictError(
            f"运行状态为 {run.status.value}，没有待执行的回滚",
            details={"reason": "no_rollback_in_progress", "run_id": run.id},
        )
    if not capability:
        return await _rollback_failed(
            session,
            run=run,
            now=now,
            reason="missing_capability",
            error_code=ErrorCode.ROLLBACK_FAILED,
        )

    facts = await _load_facts(session, run=run)
    # 主体：回滚的令牌签给的是**发起回滚的人**，broker 会拿 ctx.actor_id 与令牌里的
    # subject_id 逐字比对，所以这里必须回填同一个值。worker 手里只有 run 行与账本，
    # 因此这个值只能从 ``rollback.started`` 的 payload 复原（那次动作自己写下的），
    # 兜底才是 ``run.triggered_by``。填 None 的后果不是「少一个字段」——
    # broker 会以 UNAUTHORIZED_TOOL「Capability 执行缺少操作主体」拒绝，
    # 而那条报错看起来像权限配置问题，实际是这里漏了主体。
    actor_id = await _rollback_actor(session, run=run)
    if actor_id is None:
        return await _rollback_failed(
            session,
            run=run,
            now=now,
            reason="missing_actor",
            error_code=ErrorCode.ROLLBACK_FAILED,
        )
    target = await _target_facts(session, tenant_id=tenant_id, run_id=run_id)
    publisher = _publisher(target)
    args = TargetRollbackCleanupArgs(
        run_id=run.id,
        shadow_table=target.shadow_table,
        formal_table=target.formal_table,
        rollback_plan_digest=rollback_plan_digest(
            run_id=run.id,
            shadow_table=target.shadow_table,
            formal_table=target.formal_table,
        ),
    )
    try:
        result = await _broker().execute(
            ToolName.TARGET_ROLLBACK_CLEANUP,
            capability,
            args,
            _rollback_ctx(run, actor_id=actor_id),
            session=session,
        )
    except AppError as exc:
        # 协议类拒绝（令牌重放/过期/准备单已过期）：保持 rolling_back 并记失败
        return await _rollback_failed(
            session, run=run, now=now, reason=str(exc.code), error_code=exc.code
        )

    # 执行后对账：**影子表还在不在**由我们自己去问目标端，而不是读工具返回的一句话。
    # 工具报的是它那一瞬间的观测；这里问的是「现在」。两者不一致时以「还在」为准——
    # 影子表残留是一个必须让人看见的事实，而「我以为删掉了」不是。
    remaining = await publisher.exists(target.shadow_table)
    if remaining or not result.ok:
        return await _rollback_failed(
            session,
            run=run,
            now=now,
            reason=(
                "shadow_table_still_exists"
                if remaining
                else str(result.detail.get("state") or "cleanup_failed")
            ),
            error_code=ErrorCode.ROLLBACK_FAILED,
            extra={"shadow_dropped": not remaining},
        )

    await run_service.apply_transition(
        session,
        run_id=run.id,
        target=RunStatus.ROLLED_BACK,
        reason="rollback_cleanup_succeeded",
        actor_id=None,
        now=now,
    )
    await _ledger.append(
        session,
        LedgerEvent(
            tenant_id=run.tenant_id,
            project_id=run.project_id,
            actor_id=None,
            event_type=EvidenceEventType.ROLLBACK_SUCCEEDED,
            subject_type="run",
            subject_id=str(run.id),
            artifact_hash=facts.preparation.artifact_sha256,
            payload={
                "run_id": run.id,
                "shadow_table": target.shadow_table,
                "formal_table": target.formal_table,
                "shadow_dropped": True,
                "formal_unchanged": True,
                "fingerprint_scope": _FINGERPRINT_SCOPE,
            },
            occurred_at=now,
        ),
    )
    await session.commit()
    log.info(
        "rollback_succeeded",
        run_id=run.id,
        shadow_table=target.shadow_table,
        formal_table=target.formal_table,
    )
    return RollbackResultDTO(
        run_id=run.id,
        status=RunStatus.ROLLED_BACK,
        already_rolled_back=False,
        attempt=await _rollback_attempt(session, run=run),
        shadow_table=target.shadow_table,
        formal_table=target.formal_table,
        message="回滚完成：影子表已清理，正式表未被改动",
    )


async def _rollback_failed(
    session: AsyncSession,
    *,
    run: ExecutionRun,
    now: datetime,
    reason: str,
    error_code: ErrorCode,
    extra: dict[str, Any] | None = None,
) -> RollbackResultDTO:
    """回滚失败：**保持 ``rolling_back``**，不回退到 ``failed``（§5.3）。

    回退到 ``failed`` 会让这条 run 看起来「可以重新点一次回滚」，而它可能已经被
    部分清理过——「看似可重试但已被部分清理」的假象比停在半路更危险。
    停在 ``rolling_back`` 是可重入的（§6.5 的 stale 窗口），语义上也更准确：
    这次清理**还没结束**。
    """
    await _ledger.append(
        session,
        LedgerEvent(
            tenant_id=run.tenant_id,
            project_id=run.project_id,
            actor_id=None,
            event_type=_ROLLBACK_FAILED_EVENT,
            subject_type="run",
            subject_id=str(run.id),
            payload={
                "kind": "rollback",
                "run_id": run.id,
                "shadow_table": run.shadow_table,
                "formal_table": run.formal_table,
                "reason": reason,
                "error_code": str(error_code),
                **(extra or {}),
            },
            occurred_at=now,
        ),
    )
    await session.commit()
    log.error("rollback_failed", run_id=run.id, reason=reason, error_code=str(error_code))
    return RollbackResultDTO(
        run_id=run.id,
        status=run.status,
        already_rolled_back=False,
        attempt=await _rollback_attempt(session, run=run),
        shadow_table=run.shadow_table or "",
        formal_table=run.formal_table or "",
        message=f"回滚失败（{reason}），影子表可能仍存在，可重试",
    )


async def _preempt_for_rollback(session: AsyncSession, *, run: ExecutionRun, now: datetime) -> int:
    """抢占：``status IN ('failed','interrupted')`` → ``rolling_back``。返回本次 attempt 号。

    未命中时读状态，四条分支（§6.5）：

    - ``rolled_back`` → 幂等返回（在 ``request_rollback`` 里已提前处理，这里兜一道）；
    - ``succeeded`` → ``CONFLICT`` ``already_published``（已发布成功后的回退本轮不实现，
      见 §10——那条路需要重新审批，不是「删掉影子表」）；
    - ``rolling_back`` → 看 stale 窗口：窗口内 ``CONFLICT``，超窗口视为上次 worker 已死，
      允许重入并把 ``attempt`` +1；
    - ``running`` / ``queued`` → ``CONFLICT`` ``run_not_terminal``（先 cancel）。
    """
    result = await session.execute(
        update(ExecutionRun)
        .where(
            ExecutionRun.id == run.id,
            ExecutionRun.tenant_id == run.tenant_id,
            ExecutionRun.project_id == run.project_id,
            ExecutionRun.status.in_([RunStatus.FAILED, RunStatus.INTERRUPTED]),
        )
        .values(status=RunStatus.ROLLING_BACK, updated_at=now, finished_at=None)
    )
    if result.rowcount:  # type: ignore[attr-defined]
        # 抢占成功：这是第一次尝试。``finished_at`` 必须清掉——一次回滚中的 run
        # 不再是终态，留着旧时间戳会让「保留策略」把它当已结束的数据删掉。
        return 1

    current = await session.scalar(select(ExecutionRun.status).where(ExecutionRun.id == run.id))
    if current is RunStatus.ROLLED_BACK:
        raise RunConflictError(
            "本次运行已经回滚过",
            details={"reason": "already_rolled_back", "run_id": run.id},
        )
    if current is RunStatus.SUCCEEDED:
        raise RunConflictError(
            "本次运行已发布成功，已发布数据的回退需要重新审批（本轮不提供）",
            details={"reason": "already_published", "run_id": run.id},
        )
    if current is RunStatus.ROLLING_BACK:
        return await _reenter_rollback(session, run=run, now=now)
    raise RunConflictError(
        f"运行状态为 {getattr(current, 'value', current)}，需先取消/等待终态才能回滚",
        details={"reason": "run_not_terminal", "run_id": run.id},
    )


async def _reenter_rollback(session: AsyncSession, *, run: ExecutionRun, now: datetime) -> int:
    """``rolling_back`` 上的重复请求：按 stale 窗口判定（§6.5）。

    窗口 = ``settings.CAPABILITY_TTL_SEC``（复用既有配置，不新增项）。含义：
    上一次抢占签发的令牌早已过期，如果它还在跑，它的令牌也早已失效——
    因此「窗口外」等价于「上次的动作不可能还活着」，可以安全重入。
    """
    started = await _latest_event(
        session,
        tenant_id=run.tenant_id,
        run_id=run.id,
        event_types=(EvidenceEventType.ROLLBACK_STARTED,),
    )
    attempt = 1
    if started is not None:
        payload = dict(started.payload or {})
        attempt = int(payload.get("attempt") or 1)
        began = as_utc(started.occurred_at)
        elapsed = (now - began).total_seconds() if began is not None else 0.0
        if elapsed <= _PUBLISH_CLAIM_TTL_SEC:
            raise RunConflictError(
                "本次运行正在回滚中，请等它结束后再试",
                details={
                    "reason": "rollback_in_progress",
                    "run_id": run.id,
                    "attempt": attempt,
                    "elapsed_sec": int(elapsed),
                },
            )
        log.warning(
            "rollback_reentry_after_stale_window",
            run_id=run.id,
            attempt=attempt,
            elapsed_sec=int(elapsed),
        )
        return attempt + 1
    # 状态是 rolling_back 却没有 ROLLBACK_STARTED：说明它是由更早的路径改的
    # （或事件被清理过）。允许重入并记 attempt=1，让这次动作留下事件。
    return attempt


async def _rollback_actor(session: AsyncSession, *, run: ExecutionRun) -> int | None:
    """本次回滚的操作主体：``rollback.started`` 的 ``actor_id``，兜底 ``run.triggered_by``。

    **必须**非空：broker 用它重算令牌绑定（``_expected_binding`` 里的 ``subject_id``），
    而令牌就是签给这个人的。宁可在这里判空并如实记一次 ``missing_actor`` 的失败，
    也不要让 broker 拒一次——那条报错（UNAUTHORIZED_TOOL）指向权限配置，
    而真因是「这笔回滚是谁发起的」在控制面里丢了。
    """
    started = await _latest_event(
        session,
        tenant_id=run.tenant_id,
        run_id=run.id,
        event_types=(EvidenceEventType.ROLLBACK_STARTED,),
    )
    if started is not None:
        actor = dict(started.payload or {}).get("actor_id")
        if actor is not None:
            return int(actor)
    return run.triggered_by


async def _rollback_attempt(session: AsyncSession, *, run: ExecutionRun) -> int:
    """本 run 的回滚尝试次数（取最近一条 ``rollback.started`` 的 payload）。

    租户从 run 行来而不是参数：worker 路径的租户本来就只能从这一行读出来
    （``services/run.load_for_submit`` 对同一问题给过同样的理由），
    而事件的租户过滤是硬的（CLAUDE.md §4.7）。
    """
    started = await _latest_event(
        session,
        tenant_id=run.tenant_id,
        run_id=run.id,
        event_types=(EvidenceEventType.ROLLBACK_STARTED,),
    )
    if started is None:
        return 1
    return max(1, int(dict(started.payload or {}).get("attempt") or 1))


async def _require_operator(
    session: AsyncSession, *, project_id: int, user_id: int, now: datetime
) -> None:
    """角色闸：角色**此刻**从 ``role_assignment`` 重查，不信 JWT 声明。

    与 ``services/run.py::_require_operator`` 是同一个判定的镜像（那边是私有的，
    跨模块 import 私有名比多这五行更脆）。**两处的语义必须一致**：任何一个改成
    「信任 JWT」都会让「权限已被回收的人继续能回滚」。
    """
    held = await active_roles(session, project_id=project_id, user_id=user_id, at=now)
    if RoleName.OPERATOR not in held:
        raise RoleRequiredError("需要 operator 职责槽才能触发受管回滚")


async def _assert_rollback_scope(
    session: AsyncSession, *, run: ExecutionRun, version_no: int
) -> None:
    """回滚范围四连（§6.4）。**这是「回滚不会误删正式表」的核心证明。**

    1. 影子表名必须等于 ``shadow_table_name(formal, version_no)`` 的**重算值**——
       比「后缀命中命名规范」更强：后缀相符但中间被改过的名字照样拦下；
    2. ``shadow != formal``——一个字面量相等就能让清理变成对正式表的 DROP；
    3. 两者必须在**目标数据源所在的库**里（不跨库）；
    4. 数据源类型必须是 ``doris`` / ``mysql``（否则 ``load_run_target`` 直接拒）。

    任何一条不满足都**不执行**清理：拒绝的代价是残留一张影子表，放行的代价是删掉正式表。
    """
    shadow = run.shadow_table or ""
    formal = run.formal_table or ""
    if not shadow or not formal:
        raise ValidationError(
            "本次运行没有登记影子表/正式表，无法回滚",
            details={"reason": "run_tables_missing", "run_id": run.id},
        )
    if shadow == formal:
        raise ValidationError(
            "影子表与正式表同名，拒绝清理（这一定是控制面缺陷）",
            details={"reason": "shadow_equals_formal", "run_id": run.id},
        )
    expected = shadow_table_name(formal, version_no)
    if shadow != expected:
        raise ValidationError(
            f"影子表名 {shadow!r} 与命名规范重算值不符，拒绝清理",
            details={"reason": "shadow_table_name_mismatch", "run_id": run.id},
        )
    target = await _target_facts(session, tenant_id=run.tenant_id, run_id=run.id)
    for table in (shadow, formal):
        if "." in table:
            # 库名在连接上（``endpoint.database``），表名带库前缀等于允许跨库清理
            raise ValidationError(
                f"表名 {table!r} 带库前缀，拒绝清理",
                details={"reason": "qualified_table_rejected", "run_id": run.id},
            )
    log.info(
        "rollback_scope_verified",
        run_id=run.id,
        shadow_table=shadow,
        formal_table=formal,
        target_kind=target.binding.kind,
        database=target.binding.database,
    )


# ---------------------------------------------------------------- 读接口


async def get_publish_state(
    session: AsyncSession, *, tenant_id: int, run_id: int
) -> PublishStateDTO:
    """发布与回滚的全部已知事实（§3.1 的 ``GET /runs/{id}/publish``）。

    结论**由事件复原**，不从内存变量猜：账本是这条链上唯一「即使进程被杀也还在」的地方。
    没有任何发布记录时返回 ``not_attempted``——「没试过」与「试了但不知道」必须是
    两个不同的答案，否则运维会在一个从没发布过的 run 上做人工核对。
    """
    run = await _load_run(session, tenant_id=tenant_id, run_id=run_id)
    swap = await _latest_event(
        session,
        tenant_id=tenant_id,
        run_id=run_id,
        event_types=(EvidenceEventType.SWAP_SUCCEEDED, EvidenceEventType.SWAP_FAILED),
    )
    rollback_started = await _latest_event(
        session,
        tenant_id=tenant_id,
        run_id=run_id,
        event_types=(EvidenceEventType.ROLLBACK_STARTED,),
    )
    rollback_done = await _latest_event(
        session,
        tenant_id=tenant_id,
        run_id=run_id,
        event_types=(EvidenceEventType.ROLLBACK_SUCCEEDED,),
    )

    consumed = await _consumed_publish_capability(session, preparation_id=run.preparation_id)
    publish_state, final_state, detail = _derive_publish_state(
        run=run, swap=swap, consumed=consumed
    )
    return PublishStateDTO(
        run_id=run.id,
        status=run.status,
        publish_state=publish_state,
        final_state=final_state,
        shadow_table=run.shadow_table or "",
        formal_table=run.formal_table or "",
        pre_fingerprint=(dict(swap.payload or {}).get("pre_fingerprint") if swap else None),
        post_fingerprint=(dict(swap.payload or {}).get("post_fingerprint") if swap else None),
        fingerprint_scope=str(
            (dict(swap.payload or {}).get("fingerprint_scope") if swap else None)
            or _FINGERPRINT_SCOPE
        ),
        swap_at=as_utc(swap.occurred_at) if swap is not None else None,
        rollback=_derive_rollback_state(
            run=run, started=rollback_started, done=rollback_done
        ),
        detail=detail,
    )


def _derive_publish_state(
    *, run: ExecutionRun, swap: EvidenceEvent | None, consumed: bool
) -> tuple[PublishState, FinalState | None, dict[str, Any]]:
    """发布状态的唯一推导点（DTO 与日志共用一份形状）。

    时间戳不在这里返回：它**只**来自事件本身（上面统一取 ``swap.occurred_at``），
    在这里再回传一次就会多一条「同一个事实两个来源」的路。

    返回类型用的是 DTO 的那两个 ``Literal``（而不是 ``str``）：状态是**枚举**，
    让类型系统跟着一起守它，比在注释里写「只能是这几个值」可靠。
    """
    snapshot_swap = dict((run.failure_snapshot or {}).get("swap") or {})
    if swap is None:
        if consumed:
            # 令牌被消费过、没有结论事件：正在发布，或者发布进程死了
            state: PublishState = "in_progress" if run.status is RunStatus.RUNNING else "unknown"
            return state, None, {"reason": "attempt_without_verdict"}
        return "not_attempted", None, dict(snapshot_swap)
    payload = dict(swap.payload or {})
    final_state = str(payload.get("final_state") or "")
    if swap.event_type is EvidenceEventType.SWAP_SUCCEEDED:
        return "published", "published", payload
    if final_state == "published":
        # 理论不可达（成功会写 SWAP_SUCCEEDED）。真出现说明有人写了矛盾的证据，
        # 不猜，报 unknown 让人来看。
        return "unknown", "published", payload
    if final_state == "not_published":
        return "failed", "not_published", payload
    # 失败的证据里带的就是 unknown（对账读不出指纹）→ 如实回 unknown
    if final_state == "unknown":
        return "unknown", "unknown", payload
    # 连 final_state 都没带（更旧的事件、或证据被人工写坏）→ 判未决，**不回落到 failed**：
    # 「失败」会被读成「确定没换成」，而我们其实不知道。
    return "unknown", None, payload


def _derive_rollback_state(
    *, run: ExecutionRun, started: EvidenceEvent | None, done: EvidenceEvent | None
) -> RollbackStateDTO:
    if done is not None:
        payload = dict(done.payload or {})
        return RollbackStateDTO(
            state="succeeded",
            attempt=int(dict(started.payload or {}).get("attempt") or 0) if started else 0,
            shadow_dropped=bool(payload.get("shadow_dropped", True)),
            formal_unchanged=bool(payload.get("formal_unchanged", True)),
            occurred_at=as_utc(done.occurred_at),
            message="影子表已清理",
        )
    if started is not None:
        payload = dict(started.payload or {})
        return RollbackStateDTO(
            state="in_progress",
            attempt=int(payload.get("attempt") or 1),
            occurred_at=as_utc(started.occurred_at),
            message="回滚已受理，等待执行结果",
        )
    if run.status is RunStatus.ROLLED_BACK:
        # 状态说回滚完了、却没有 rollback.succeeded：证据链有缺口，如实反映
        return RollbackStateDTO(state="succeeded", message="已回滚（缺 rollback.succeeded 事件）")
    return RollbackStateDTO()


__all__ = [
    "execute_rollback",
    "get_publish_state",
    "publish_run",
    "request_rollback",
]
