"""证据账本查询、验链与导出（be-06）。

三条不变量：

1. **只有 auditor 能读**（``engines.policy.can_read_ledger``）。而且拒绝本身要入账
   （be-06 §7 第 6 行）——审计日志被越权查询，正是审计员最需要看到的事件。
2. **验链由服务端重算，前端不参与**。前端拿到的只是一个结论；
   让它自己算哈希，等于把「是否通过」的判定权交给了可以被改的那一侧。
3. **导出必须自带 verification 报告**（be-06 §6.5）。审计拿走一份副本，
   那份副本要么能证明自己完整，要么就只是一堆无法核对的文本。
   所以导出先验链，把报告写进文件头。

本层禁止 ``import fastapi``。
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Iterator
from datetime import UTC
from typing import Any, cast

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.errors import RoleRequiredError, ValidationError
from core.ids import canonical_json, utcnow
from engines.policy import can_read_ledger
from harness.ledger import EvidenceLedger, LedgerEvent
from models.enums import EvidenceEventType
from models.evidence import EvidenceEvent, EvidenceLedgerBlock
from models.tenant import AppUser
from schemas.auth import SubjectContext
from schemas.governance import (
    EvidenceEventRead,
    EvidenceFilter,
    EvidencePage,
    LedgerBreakReason,
    LedgerVerifyResult,
    VerifyRequest,
)
from services.project import active_roles

log = structlog.get_logger(__name__)

_ledger = EvidenceLedger()

# CSV 固定列序（be-06 §6.5）。写死而不是从模型反射：
# 反射出来的列序会随模型改动而变，而审计拿旧文件来对账时列序必须可预期
_CSV_COLUMNS = (
    "seq",
    "occurred_at",
    "event_type",
    "tenant_id",
    "project_id",
    "actor_id",
    "subject_type",
    "subject_id",
    "artifact_hash",
    "prev_hash",
    "event_hash",
    "payload_json",
)

# 导出总量上限。超过就该走归档而不是这个接口：
# 一次导出几十万条会把响应体撑到 GB 级，而审计真正要的是「这一段」
_MAX_EXPORT = 50_000


# ---------------------------------------------------------------- 权限


async def _require_auditor(
    session: AsyncSession, ctx: SubjectContext, project_id: int
) -> None:
    """auditor 才可读。**拒绝时写账本**（be-06 §7 第 6 行）。

    写账本是尽力而为：账本写不进去（锁等待、连接断了）不能把 403 变成 500——
    用户看到的应当仍是「你没权限」，而不是一个看起来像服务故障的错误。
    写失败只记 structlog，并带上被拒的人，让监控能发现「账本写不动了」。
    """
    roles = await active_roles(session, project_id=project_id, user_id=ctx.user_id)
    if can_read_ledger(roles):
        return

    try:
        await _ledger.append(
            session,
            LedgerEvent(
                tenant_id=ctx.tenant_id,
                project_id=project_id,
                actor_id=ctx.user_id,
                event_type=EvidenceEventType.LEDGER_ACCESS_DENIED,
                subject_type="ledger",
                subject_id=str(project_id),
                payload={"roles": sorted(r.value for r in roles)},
            ),
        )
        await session.commit()
    except Exception:
        # 回滚，否则这个失败的 append 会把会话留在坏事务里，
        # 后面抛 403 时顺带把连接也废了
        await session.rollback()
        log.warning("ledger_access_denied_write_failed", project_id=project_id, actor_id=ctx.user_id)

    raise RoleRequiredError("证据账本仅对 auditor 开放")


# ---------------------------------------------------------------- 查询


def _subject_scope(flt: EvidenceFilter) -> tuple[str, str] | None:
    """把 ``pipeline_version_id`` / ``execution_run_id`` 映射成 subject 条件。

    前端给 ``subject_id`` 的注释就是「形如 ``preparation:42`` / ``pipeline_version:7``」,
    所以 ``pipeline_version_id=N`` 读作 ``subject_type='pipeline_version'``
    + ``subject_id='N'`` 是对该契约的直接解读，而不是我另发明的一个约定。
    """
    if flt.subject_id:
        # 只给了 subject_id 时按前缀拆一次：'pipeline_version:7' → ('pipeline_version','7')。
        # 不拆的话 'pipeline_version:7' 与 '7' 是两回事，而前端两种写法都可能传
        if ":" in flt.subject_id:
            head, _, tail = flt.subject_id.partition(":")
            return head, tail
        return "", flt.subject_id
    if flt.pipeline_version_id:
        return "pipeline_version", str(flt.pipeline_version_id)
    if flt.execution_run_id:
        return "run", str(flt.execution_run_id)
    return None


def _apply_filters(stmt: Any, flt: EvidenceFilter) -> Any:
    if flt.event_type:
        stmt = stmt.where(EvidenceEvent.event_type.in_(flt.event_type))
    scope = _subject_scope(flt)
    if scope is not None:
        head, tail = scope
        stmt = stmt.where(EvidenceEvent.subject_id == tail)
        if head:
            stmt = stmt.where(EvidenceEvent.subject_type == head)
    if flt.occurred_from is not None:
        stmt = stmt.where(EvidenceEvent.occurred_at >= flt.occurred_from)
    if flt.occurred_to is not None:
        stmt = stmt.where(EvidenceEvent.occurred_at <= flt.occurred_to)
    return stmt


def encode_cursor(seq: int) -> str:
    """游标是**不透明**的：``seq`` 前面加个前缀。

    让客户端构造游标（比如直接传个整数）意味着「从任意位置开始翻」这种用法会被
    当成合法输入，而它跳过的那些事件不会有人注意到。加前缀后，
    乱传的游标会被明确拒绝。
    """
    return f"ev:{seq}"


def decode_cursor(cursor: str) -> int:
    head, _, tail = cursor.partition(":")
    if head != "ev" or not tail.isdigit():
        raise ValidationError("游标无效")
    return int(tail)


async def _actor_names(session: AsyncSession, tenant_id: int, actor_ids: set[int]) -> dict[int, str]:
    """一次查完这页出现的所有人名。

    逐条查会变成 N+1：一页 50 条就是 50 次往返，而账本查询本来就是低频操作，
    没必要为了省一次 IN 查询让它慢十倍。
    """
    if not actor_ids:
        return {}
    rows = (
        await session.execute(
            select(AppUser.id, AppUser.display_name).where(
                AppUser.id.in_(actor_ids), AppUser.tenant_id == tenant_id
            )
        )
    ).all()
    return {int(uid): name for uid, name in rows}


async def _block_seqs(session: AsyncSession, lo: int, hi: int) -> list[tuple[int, int, int]]:
    """区间内的检查点 ``(seq, from_event_seq, to_event_seq)``。"""
    rows = (
        await session.execute(
            select(
                EvidenceLedgerBlock.seq,
                EvidenceLedgerBlock.from_event_seq,
                EvidenceLedgerBlock.to_event_seq,
            ).where(
                EvidenceLedgerBlock.from_event_seq <= hi,
                EvidenceLedgerBlock.to_event_seq >= lo,
            )
        )
    ).all()
    return [(int(a), int(b), int(c)) for a, b, c in rows]


def _block_for(seq: int, blocks: list[tuple[int, int, int]]) -> int | None:
    for block_seq, lo, hi in blocks:
        if lo <= seq <= hi:
            return block_seq
    return None


async def list_evidence(
    session: AsyncSession, ctx: SubjectContext, flt: EvidenceFilter
) -> EvidencePage:
    """游标分页查询。

    **一律带 ``tenant_id``**（CLAUDE.md §4.7）。租户条件不参与分页游标，
    所以它不是一个「可以省略的过滤」——省略了就是跨租户读审计日志。
    """
    await _require_auditor(session, ctx, flt.project_id)

    stmt = select(EvidenceEvent).where(
        EvidenceEvent.tenant_id == ctx.tenant_id,
        EvidenceEvent.project_id == flt.project_id,
    )
    stmt = _apply_filters(stmt, flt)
    if flt.cursor:
        stmt = stmt.where(EvidenceEvent.seq > decode_cursor(flt.cursor))

    stmt = stmt.order_by(EvidenceEvent.seq.asc()).limit(flt.limit + 1)
    rows = list((await session.execute(stmt)).scalars().all())

    # 多取一条来判断「还有没有下一页」，而不是再跑一次 COUNT
    has_more = len(rows) > flt.limit
    rows = rows[: flt.limit]

    names = await _actor_names(session, ctx.tenant_id, {int(r.actor_id) for r in rows if r.actor_id})
    blocks = await _block_seqs(session, int(rows[0].seq), int(rows[-1].seq)) if rows else []

    items = [
        EvidenceEventRead(
            id=r.id,
            seq=int(r.seq),
            tenant_id=int(r.tenant_id),
            project_id=None if r.project_id is None else int(r.project_id),
            actor_id=None if r.actor_id is None else int(r.actor_id),
            actor_name=names.get(int(r.actor_id), "") if r.actor_id else "",
            event_type=r.event_type.value,
            subject_type=r.subject_type,
            subject_id=r.subject_id,
            artifact_hash=r.artifact_hash,
            payload=dict(r.payload or {}),
            prev_hash=str(r.prev_hash),
            event_hash=str(r.event_hash),
            occurred_at=r.occurred_at,
            block_seq=_block_for(int(r.seq), blocks),
        )
        for r in rows
    ]
    return EvidencePage(
        items=items,
        next_cursor=encode_cursor(int(rows[-1].seq)) if has_more and rows else None,
    )


# ---------------------------------------------------------------- 验链


_BREAK_KINDS: frozenset[str] = frozenset(
    {"hash_mismatch", "prev_hash_mismatch", "signature_invalid", "missing_event"}
)


def _public_kind(value: str | None) -> LedgerBreakReason | None:
    """把账本给的断链类型收敛到前端认识的取值。

    认不出来时返回 ``None``（即「坏了但类型未知」）而不是把原始字符串透出去：
    前端拿到未知字符串会走 default 分支显示成「未知错误」——那和什么都别说是一样的，
    但额外的坏处是它看起来像前端漏了一个 case。断链本身仍然报出来（``ok=False``），
    真正的诊断信息在 ``broken_detail`` 里，那条不经过枚举。
    """
    if value is None or value not in _BREAK_KINDS:
        return None
    return cast(LedgerBreakReason, value)


async def _started_from_block(session: AsyncSession, from_seq: int) -> int | None:
    """``from_seq`` 落在哪个检查点之后。``None`` 表示从创世条目起验。"""
    if from_seq <= 1:
        return None
    seq = (
        await session.execute(
            select(EvidenceLedgerBlock.seq)
            .where(EvidenceLedgerBlock.to_event_seq < from_seq)
            .order_by(EvidenceLedgerBlock.seq.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return 0 if seq is None else int(seq)


async def verify_ledger(
    session: AsyncSession, ctx: SubjectContext, req: VerifyRequest
) -> LedgerVerifyResult:
    """重算全链（或指定区间）。

    ``project_id`` 在这里**只用于授权**，不参与过滤：链是全局的一条，
    按项目筛一个子集去验会在每个项目边界上报「prev_hash 不一致」——
    那不是发现篡改，是筛错了。前端类型里那个 `started_from_block_seq`
    就是用来提醒用户「这次只验了一段」的。
    """
    await _require_auditor(session, ctx, req.project_id)

    report = await _ledger.verify_chain(session, from_seq=req.from_seq, to_seq=req.to_seq)

    if not report.ok:
        # 断链是需要人工介入的安全事件，必须入账（be-06 §7 第 9 行）。
        # 用 getattr 取 kind：报告变了字段名不该让「记录安全事件」这件事本身失败
        try:
            await _ledger.append(
                session,
                LedgerEvent(
                    tenant_id=ctx.tenant_id,
                    project_id=req.project_id,
                    actor_id=ctx.user_id,
                    event_type=EvidenceEventType.POLICY_BLOCKED,
                    subject_type="ledger",
                    subject_id=str(report.first_broken_seq or 0),
                    payload={
                        "kind": str(report.broken_kind or "unknown"),
                        "checked_events": report.checked_events,
                        "from_seq": req.from_seq,
                    },
                ),
            )
            await session.commit()
        except Exception:
            await session.rollback()
            log.warning("ledger_broken_report_write_failed", broken_seq=report.first_broken_seq)

    return LedgerVerifyResult(
        ok=report.ok,
        checked_events=report.checked_events,
        checked_blocks=report.checked_blocks,
        broken_at_seq=report.first_broken_seq,
        broken_reason=_public_kind(report.broken_kind),
        broken_detail=report.broken_reason,
        started_from_block_seq=await _started_from_block(session, req.from_seq),
        finished_at=report.checked_at,
    )


# ---------------------------------------------------------------- 导出


async def _fetch_for_export(
    session: AsyncSession, ctx: SubjectContext, flt: EvidenceFilter, limit: int
) -> list[EvidenceEventRead]:
    page = await list_evidence(
        session,
        ctx,
        flt.model_copy(update={"limit": min(limit, 200), "cursor": None}),
    )
    items = list(page.items)
    cursor = page.next_cursor
    while cursor is not None and len(items) < limit:
        page = await list_evidence(
            session, ctx, flt.model_copy(update={"limit": min(limit - len(items), 200), "cursor": cursor})
        )
        items.extend(page.items)
        cursor = page.next_cursor
    return items[:limit]


async def export_evidence(
    session: AsyncSession,
    ctx: SubjectContext,
    flt: EvidenceFilter,
    *,
    fmt: str,
    max_events: int = _MAX_EXPORT,
) -> Iterator[bytes]:
    """流式导出。

    JSON 与 CSV 都**先验链再把报告写进去**：一份无法自证完整的副本，
    在审计桌上和被删了几行的打印件没有区别。

    ``max_events`` 而不是复用 ``flt.limit``：后者是**查询分页大小**（一次取多少条），
    前者是**这次导出最多带走多少条**。两个语义共用一个字段时，
    API 传的 ``limit`` 会看起来生效、实际被静默忽略——那是比报错更坏的失败方式。
    """
    await _require_auditor(session, ctx, flt.project_id)
    report = await _ledger.verify_chain(session)
    items = await _fetch_for_export(session, ctx, flt, max_events)
    exported_at = utcnow().isoformat()

    # ⚠️ 导出动作本身**不是**证据事件类型（be-06 §10 已登记该缺口）：
    # 「谁导出了账本」这件事目前无处可记。这里只记 structlog，
    # 它对审计员可见，但不可作为证据链的一环
    log.info(
        "evidence_exported",
        project_id=flt.project_id,
        actor_id=ctx.user_id,
        fmt=fmt,
        events=len(items),
        ledger_ok=report.ok,
    )

    if fmt == "csv":
        # 手写 for 而不是 ``yield from``：本函数是 async generator（``async def`` + ``yield``），
        # 而 ``yield from`` 在 async generator 里是语法错误——写成 ``yield from`` 时
        # 报的是 SyntaxError，整个服务起不来
        for chunk in _csv_chunks(items):
            yield chunk
        return

    # JSON 分块产出：一次性 json.dumps 会把整个列表物化两遍（Python 对象 + 字符串），
    # 五万条时那是几十 MB 的峰值
    yield b'{"events":['
    for idx, item in enumerate(items):
        if idx:
            yield b","
        yield json.dumps(item.model_dump(mode="json"), ensure_ascii=False).encode("utf-8")
    trailer = {
        "report": {
            "ok": report.ok,
            "checked_events": report.checked_events,
            "checked_blocks": report.checked_blocks,
            "first_broken_seq": report.first_broken_seq,
            "broken_kind": report.broken_kind,
            "broken_reason": report.broken_reason,
        },
        "exported_at": exported_at,
        "filter": flt.model_dump(mode="json"),
        "exported_by": ctx.user_id,
    }
    yield ("]," + canonical_json(trailer)[1:]).encode("utf-8")


def _csv_chunks(items: list[EvidenceEventRead]) -> Iterator[bytes]:
    """CSV：固定列序、``payload_json`` 单行 JSON、UTF-8 **带 BOM**。

    BOM 是给 Excel 的：不带的话 Windows 版 Excel 会把 UTF-8 当 GBK 读，
    中文列全变乱码，而审计拿到的就是一份看不懂的文件（be-06 §6.5 明确要求带 BOM）。
    """
    yield "﻿".encode()

    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")  # Excel 认 CRLF
    writer.writerow(_CSV_COLUMNS)
    yield buffer.getvalue().encode("utf-8")
    buffer.seek(0)
    buffer.truncate(0)

    for item in items:
        writer.writerow(
            [
                item.seq,
                item.occurred_at.astimezone(UTC).isoformat(),
                item.event_type,
                item.tenant_id,
                "" if item.project_id is None else item.project_id,
                "" if item.actor_id is None else item.actor_id,
                item.subject_type,
                item.subject_id,
                item.artifact_hash,
                item.prev_hash,
                item.event_hash,
                canonical_json(item.payload),
            ]
        )
        # 每条产出一次：等整表写完再吐等于把导出上限（五万条）全压在内存里
        yield buffer.getvalue().encode("utf-8")
        buffer.seek(0)
        buffer.truncate(0)


__all__ = [
    "decode_cursor",
    "encode_cursor",
    "export_evidence",
    "list_evidence",
    "verify_ledger",
]
