"""``target.publish_swap`` / ``target.rollback_cleanup`` 的工具执行器（be-18 §5.1 / §5.2）。

**整个仓库里唯一真正对正式表动刀的地方**（``ALTER`` / ``RENAME`` / ``DROP`` 都在这条路径上），
所以这里的纪律与别处不同：

1. **不信任调用方**：入参里的表名必须与 ``execution_run`` 行**逐字相同**。
   服务层是从同一行读出来再拼进 args 的，所以「不一致」只可能来自伪造或串号——
   这种情况下正确的反应是拒绝，而不是「以入参为准」。
2. **状态门禁不在这里**。谁在什么时候抢到这次发布，由服务层的条件 UPDATE
   独占判定（唯一一次生效的抢占）；执行器再判一次会把「谁赢」变成两个地方的事。
   重复投递与并发由另外两层兜住：``swap.succeeded`` 幂等短路 + Capability 单次消费。
3. **领域失败不抛异常**（返回 ``ok=False`` + ``state``）。抛出去会被 broker 记成
   ``capability.rejected``——而这张令牌是**合法签发且确实被消费**的，
   该记的是「这次发布没成功」。这一条差别直接决定了事后能不能看清「令牌被谁用了」。
4. **技术失败才抛**（连不上、参数不符、publisher 构造失败）：那些确实没走到执行阶段。
5. **白名单内的键才回**：``harness/broker.py::_ALLOWED_DETAIL_KEYS`` 会静默丢掉未知键，
   所以这里只用白名单里真实存在的键（``status/state/ok/affected_rows/row_count/
   shadow_table/formal_table/message/error_code``）。指纹这类细节进 ``detail`` 后会被
   丢掉，因此**指纹由服务层自己观测**（它本来就要为对账取一次），
   完整 detail 走 ``structlog``（长尾信息的正确去处是日志，不是账本）。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from clients.target import registry
from clients.target.base import (
    CleanupResult,
    PublishPlan,
    PublishResult,
    RollbackPlan,
    RunRef,
    TargetError,
    TargetPrecheckError,
    TargetPublisher,
    UnsupportedTargetError,
)
from core.errors import ValidationError
from core.logging import get_logger
from harness.tools import (
    TargetEnsureTablesArgs,
    TargetPublishSwapArgs,
    TargetRollbackCleanupArgs,
    ToolExecutor,
    register_executor,
)
from models.enums import ToolName
from models.execution import QualityReport

log = get_logger(__name__)

#: ``message`` 的长度上限。它会进 ``execution_run.error_message``（Text，但前端要展示）
#: 与证据 payload；一条驱动报错可以很长，而里面有用的是开头那几句。
_MESSAGE_MAX = 300


# ---------------------------------------------------------------- 公共部分


async def _facts(
    args: TargetEnsureTablesArgs | TargetPublishSwapArgs | TargetRollbackCleanupArgs,
    ctx: Any,
    session: AsyncSession,
) -> registry.RunTargetFacts:
    """取目标端事实并做「入参 vs 运行记录」的一致性校验。"""
    facts = await registry.load_run_target(session, tenant_id=ctx.tenant_id, run_id=args.run_id)
    if ctx.run_id is not None and ctx.run_id != facts.run_id:
        # 账本上下文说的是另一个 run：这不是「参数写错了」，是有人在把一次动作
        # 记到别人的账上。宁可在这里硬失败。
        raise ValidationError(
            "执行上下文里的 run_id 与入参不一致",
            details={"ctx_run_id": ctx.run_id, "args_run_id": args.run_id},
        )
    if args.shadow_table != facts.shadow_table or args.formal_table != facts.formal_table:
        raise ValidationError(
            "入参表名与运行记录不一致，拒绝执行",
            details={
                "reason": "table_name_mismatch",
                "run_shadow_table": facts.shadow_table,
                "run_formal_table": facts.formal_table,
            },
        )
    if getattr(args, "error_table", facts.error_table) != facts.error_table:
        # 错误表同样逐字比对：它是本次运行唯一的诊断落点，被指向别的表
        # 意味着「被拒的行写进了别处」，而报告照旧按本次的 error_table 去数——
        # 数出来是 0，一次有脏数据的运行会以 passed 收场
        raise ValidationError(
            "入参错误表名与运行记录不一致，拒绝执行",
            details={"reason": "table_name_mismatch", "run_error_table": facts.error_table},
        )
    return facts


def _run_ref(facts: registry.RunTargetFacts) -> RunRef:
    return RunRef(
        run_id=facts.run_id,
        tenant_id=facts.tenant_id,
        project_id=facts.project_id,
        shadow_table=facts.shadow_table,
        formal_table=facts.formal_table,
        error_table=facts.error_table,
    )


def _publisher_for(facts: registry.RunTargetFacts) -> TargetPublisher:
    try:
        return registry.build_publisher(facts.binding.endpoint)
    except UnsupportedTargetError:
        # 类型不支持是**生成期门禁**该拦下的东西（§7 最后一行），不是运行期的意外
        log.error(
            "target_unsupported",
            run_id=facts.run_id,
            data_source_id=facts.target_data_source_id,
            kind=facts.binding.kind,
        )
        raise


# ---------------------------------------------------------------- 备表


async def target_ensure_tables_executor(args: Any, ctx: Any, session: AsyncSession) -> Mapping[str, Any]:
    """``target.ensure_tables``：建影子表与错误表（**不碰正式表的数据**）。

    领域失败的处置与发布不同，这里**抛异常**而不是回 ``ok=False``：
    它在 Commit 事务里同步执行（``services/run.py``），失败意味着「这次运行根本
    起不来」，调用方要的是一条能立刻转成 4xx/5xx 的错误——而 ``ok=False`` 会被
    服务层当成一个需要解释的返回值，多一条分支就多一处可能被忽略的地方。
    抛出去由 broker 记 ``capability.rejected`` + 写拒绝原因，审计上仍然是完整的。
    """
    if not isinstance(args, TargetEnsureTablesArgs):  # pragma: no cover - 注册表已保证
        raise ValidationError("执行器收到的入参类型不符")

    facts = await _facts(args, ctx, session)
    publisher = _publisher_for(facts)
    try:
        result = await publisher.provision(_run_ref(facts))
    except TargetPrecheckError as exc:
        log.warning(
            "target_provision_refused",
            run_id=facts.run_id,
            reason=exc.details.get("reason"),
            error_code=str(exc.code),
        )
        raise
    if not result.shadow_matches_formal:
        # 影子表建出来了但与正式表不同形：作业能写、报告能出，只在 Swap 那一步
        # 被 ``assert_columns_replacable`` 拦下——那时已经白跑一趟。
        # 在这里就失败，代价是一次没有副作用的建表。
        raise TargetPrecheckError(
            "影子表与正式表的列形态不一致，拒绝继续",
            details={
                "reason": "column_mismatch",
                "shadow_table": facts.shadow_table,
                "formal_table": facts.formal_table,
            },
        )
    log.info("target_ensure_tables_done", run_id=facts.run_id, **result.detail)
    return {
        "ok": True,
        "status": "success",
        "state": str(result.detail.get("state") or "ready"),
        "shadow_table": facts.shadow_table,
        "formal_table": facts.formal_table,
        "row_count": int(result.detail.get("formal_rows") or 0),
        "affected_rows": None,
        "message": (
            f"影子表 {result.shadow_state}；错误表 {result.error_state}；"
            f"影子表与正式表同形 {result.shadow_matches_formal}"
        )[:_MESSAGE_MAX],
    }


# ---------------------------------------------------------------- 发布


async def target_publish_swap_executor(args: Any, ctx: Any, session: AsyncSession) -> Mapping[str, Any]:
    """``target.publish_swap``：交换影子表与正式表。"""
    if not isinstance(args, TargetPublishSwapArgs):  # pragma: no cover - 注册表已保证
        raise ValidationError("执行器收到的入参类型不符")

    facts = await _facts(args, ctx, session)
    await _assert_report_belongs_to_run(args, facts, session)

    plan = PublishPlan(
        run_id=facts.run_id,
        shadow_table=facts.shadow_table,
        formal_table=facts.formal_table,
        # 期望列/主键留给 publisher 用「正式表当前形态」表达（见 base.PublishPlan 的说明）：
        # 执行器为此多读一次目标库，只是把同一个判断做第二遍，而两边一旦不同就没人知道该信谁
        allow_empty_shadow=facts.read_rows == 0,
    )
    publisher = _publisher_for(facts)
    try:
        result = await publisher.publish(_run_ref(facts), plan)
    except TargetPrecheckError as exc:
        # 预检不过：**没有执行任何 DDL**，正式表原样 → not_published（§6.2 的第二行）
        log.warning(
            "target_precheck_failed",
            run_id=facts.run_id,
            shadow_table=facts.shadow_table,
            formal_table=facts.formal_table,
            reason=exc.details.get("reason"),
        )
        return _failed_detail("not_published", exc.message, error_code=str(exc.code))
    except TargetError as exc:
        # 连接/凭据/权限这类技术失败发生在预检之前，因此正式表同样没被碰过。
        # 记 not_published 是**如实**的：我们确实没换成，而不是「不知道换没换成」。
        log.warning(
            "target_publish_unavailable",
            run_id=facts.run_id,
            error_code=str(exc.code),
            error=type(exc).__name__,
        )
        return _failed_detail("not_published", exc.message, error_code=str(exc.code))

    detail = _published_detail(result)
    log.info("target_publish_done", run_id=facts.run_id, **detail)
    if result.detail.get("ddl_error"):
        # DDL 报错但对账说是 published：这正是「超时但已生效」。
        # 记成成功是对的（结论以对账为准），但这条日志必须显眼——
        # 它意味着目标端有一条我们没预期到的错误信息值得回看。
        log.warning(
            "target_publish_ddl_error_but_published",
            run_id=facts.run_id,
            ddl_error=result.detail.get("ddl_error"),
        )
    return detail


def _published_detail(result: PublishResult) -> dict[str, Any]:
    """白名单内的键 + 人话摘要。"""
    scope = str(result.detail.get("fingerprint_scope") or "")
    affected = result.detail.get("affected_rows")
    rows = result.detail.get("post_formal_rows")
    summary = (
        f"最终态 {result.final_state}；正式表 {result.published_table or '-'}；"
        f"行数 {rows if rows is not None else '-'}；指纹口径 {scope or '-'}"
    )
    return {
        "ok": result.ok,
        "status": "published" if result.ok else "failed",
        "state": result.final_state,
        "shadow_table": result.previous_data_table or "",
        "formal_table": result.published_table or "",
        "row_count": int(rows) if isinstance(rows, int) else None,
        "affected_rows": int(affected) if isinstance(affected, int) else None,
        "message": summary[:_MESSAGE_MAX],
    }


def _failed_detail(state: str, message: str, *, error_code: str) -> dict[str, Any]:
    return {
        "ok": False,
        "status": "failed",
        "state": state,
        "row_count": None,
        "affected_rows": None,
        "error_code": error_code,
        "message": message[:_MESSAGE_MAX],
    }


async def _assert_report_belongs_to_run(
    args: TargetPublishSwapArgs, facts: registry.RunTargetFacts, session: AsyncSession
) -> None:
    """质量报告的 id 必须属于这个 run。

    broker 的 ``_check_quality`` 已经兜住了「这个 run 的报告是不是 passed」，
    但那一层不检查**入参里给的 id 是哪一个 run 的**。两件事都成立才叫发布前的质量门禁；
    少了这一条，「另一次通过了的运行」的报告 id 就能拿来给这次发布背书。
    """
    report_id = (
        await session.execute(select(QualityReport.id).where(QualityReport.run_id == facts.run_id))
    ).scalar_one_or_none()
    if report_id is None or int(report_id) != args.quality_report_id:
        raise ValidationError(
            "入参的质量报告 id 与运行不对应",
            details={"reason": "quality_report_mismatch", "run_id": facts.run_id},
        )


# ---------------------------------------------------------------- 回滚清理


async def target_rollback_cleanup_executor(args: Any, ctx: Any, session: AsyncSession) -> Mapping[str, Any]:
    """``target.rollback_cleanup``：删除影子表并证明正式表没被动过（§5.2 第 9 步）。"""
    if not isinstance(args, TargetRollbackCleanupArgs):  # pragma: no cover - 注册表已保证
        raise ValidationError("执行器收到的入参类型不符")

    facts = await _facts(args, ctx, session)
    plan = RollbackPlan(shadow_table=facts.shadow_table, formal_table=facts.formal_table)
    digest = rollback_plan_digest(
        run_id=facts.run_id,
        shadow_table=plan.shadow_table,
        formal_table=plan.formal_table,
        keep_error_rows=plan.keep_error_rows,
    )
    if not digest.startswith(args.rollback_plan_digest):
        # 摘要是「这次清理针对的是什么」的承诺。不一致说明入参与事实对不上——
        # 唯一可能的来源是伪造或串号，两种都不该被执行。
        raise ValidationError(
            "回滚摘要与运行记录不一致，拒绝执行",
            details={"reason": "rollback_digest_mismatch", "run_id": facts.run_id},
        )

    publisher = _publisher_for(facts)
    try:
        result = await publisher.rollback_cleanup(_run_ref(facts), plan)
    except TargetError as exc:
        # 技术失败（DROP 报错、连不上）：抛出去，由服务层保持 rolling_back 并写 ROLLBACK_FAILED。
        # 这里**不能**吞掉——「影子表还在」是一个必须让人看见的事实。
        log.error(
            "target_rollback_failed",
            run_id=facts.run_id,
            error_code=str(exc.code),
            error=type(exc).__name__,
        )
        raise

    detail = _cleanup_detail(result)
    if result.ok:
        log.info("target_rollback_done", run_id=facts.run_id, **detail)
    else:
        log.error("target_rollback_incomplete", run_id=facts.run_id, **detail)
    return detail


def _cleanup_detail(result: CleanupResult) -> dict[str, Any]:
    state = str(result.detail.get("state") or ("dropped" if result.ok else "failed"))
    shadow = str(result.detail.get("shadow_table") or "")
    formal = str(result.detail.get("formal_table") or "")
    summary = (
        f"影子表 {status_label(result, state)}；正式表"
        f"{'未被改动' if result.formal_unchanged else '已被改动（需人工核对）'}"
    )
    return {
        "ok": result.ok,
        "status": "success" if result.ok else "failed",
        "state": state,
        "shadow_table": shadow,
        "formal_table": formal,
        "row_count": None,
        "affected_rows": None,
        "message": summary[:_MESSAGE_MAX],
    }


def status_label(result: CleanupResult, state: str) -> str:
    """人话描述，供 ``message`` 使用（前端与日志都直接读它）。"""
    if state == "already_absent":
        return "本来就不存在"
    return "已删除" if result.shadow_dropped else "仍存在（删除未生效）"


def rollback_plan_digest(
    *, run_id: int, shadow_table: str, formal_table: str, keep_error_rows: bool = True
) -> str:
    """回滚计划的稳定摘要。

    ``sort_keys`` + 固定分隔符：同一个计划两次算出的摘要必须一样，
    否则「这次清理针对的是哪个计划」这件事无法被验证。

    **公开**（不带下划线）是因为服务层组装的工具入参里必须有它：签发回滚命令的是
    ``services/publish.py``，执行的是这里。摘要若两处各算一份，任何一次改动
    （多一个字段、换个分隔符）都会让**每一条**回滚命令在执行器被判为「入参与事实不符」
    ——而那时的表现是「回滚永远失败」，排查方向会被带到权限或连接上。
    """
    payload = {
        "run_id": run_id,
        "shadow_table": shadow_table,
        "formal_table": formal_table,
        "restore_scope": "shadow_only",
        "keep_error_rows": keep_error_rows,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


# ---------------------------------------------------------------- 注册

_EXECUTORS: tuple[tuple[ToolName, ToolExecutor], ...] = (
    (ToolName.TARGET_ENSURE_TABLES, target_ensure_tables_executor),
    (ToolName.TARGET_PUBLISH_SWAP, target_publish_swap_executor),
    (ToolName.TARGET_ROLLBACK_CLEANUP, target_rollback_cleanup_executor),
)


def register() -> None:
    """注入执行器。由 ``main`` / ``celery`` 的启动钩子各调一次（CLAUDE.md §9.1）。

    少这一次调用的症状是运行期的 ``UnauthorizedToolError``（工具未注册），
    而单测因为直接 import 本模块永远发现不了——所以两个钩子都必须有。
    """
    from harness.tools import registered_executors

    done = registered_executors()
    for tool, fn in _EXECUTORS:
        if tool not in done:
            register_executor(tool, fn)


__all__ = [
    "register",
    "rollback_plan_digest",
    "target_ensure_tables_executor",
    "target_publish_swap_executor",
    "target_rollback_cleanup_executor",
]
