"""证据账本：哈希链的唯一写入者与验链实现（be-06）。

**为什么只能有一个写入者**：``prev_hash`` 必须等于当前链尾、``seq`` 必须连续，
这两件事只有在「锁链尾 + 算哈希 + 插入」这一处原子完成才成立。任何模块自己 INSERT 会
① 拿不到一致的 ``seq``（并发下必冲突或分叉）；② 极易忘记 ``event_hash`` 要覆盖 ``prev_hash``，
导致断链在很久以后才被发现；③ 绕过 :func:`_assert_payload_clean` 的脱敏前置。

``models/evidence.py`` 的 ``__immutable__`` 只保证「不改历史」，
**「只能从一处写」是纪律**——ORM 层表达不了「这张表只有一个合法写入者」。

本模块属 ``harness/``：禁止依赖 ``services/`` 与 ``api/``，只依赖 ``models/`` 与 ``core/``。
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from core.ids import as_utc as _as_utc
from core.ids import canonical_json, sha256_hex, utcnow
from core.logging import find_sensitive_paths
from models.enums import EvidenceEventType
from models.evidence import EvidenceEvent, EvidenceLedgerBlock

ZERO_HASH = "0" * 64

# 空表时的串行化闸门。锁的是「链尾那一行」，表空时无行可锁，
# 两个并发 append 会同时读到 None 而算出同一个 seq——用顾问锁补上这个窗口。
# 常量本身无意义，只要求全库一致。
_EMPTY_CHAIN_LOCK_KEY = 0xE1D_EACE

RETRYABLE_SQLSTATES = frozenset({"40001", "40P01"})
MAX_APPEND_RETRIES = 5


class LedgerError(RuntimeError):
    """账本自身的完整性错误（断链、脱敏命中、签名不可用）。"""


class SensitivePayloadError(LedgerError):
    """payload 命中脱敏规则，**拒绝写入**。

    与日志处理器「替换成 ``***``」不同，这里是抛异常：账本永久保留且是审计事实来源，
    静默替换会产生「看起来很干净」的记录，而真正的泄露已经发生了。让调用方在开发期就炸掉。
    """


class Signer(Protocol):
    """签名器契约。Ed25519 私钥永不离开实现内部。"""

    key_id: str

    def sign(self, message: bytes) -> bytes: ...


@dataclass(frozen=True, slots=True)
class LedgerEvent:
    """待落账的事件。

    与 ``EvidenceEvent`` ORM 的区别：本对象**没有 seq / prev_hash / event_hash**——
    那三个值由账本在锁内算出，调用方无从提供，也就无从算错。
    """

    tenant_id: int
    event_type: EvidenceEventType
    project_id: int | None = None
    actor_id: int | None = None
    subject_type: str = ""
    subject_id: str = ""
    artifact_hash: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    occurred_at: datetime | None = None


def escape(value: str) -> str:
    """转义分隔符。

    JSON 允许出现 ``|``，不转义会产生歧义拼接——两个不同事件可能拼出同一串，
    于是「改了 payload 但哈希不变」。反斜杠必须先转，否则会把已插入的转义符再转一次。
    """
    return value.replace("\\", "\\\\").replace("|", "\\|")


def occurred_at_utc_iso(value: datetime) -> str:
    """固定形态的 UTC 时间串。

    不能用 ``isoformat()``：微秒为 0 时它会省略小数部分，时区也可能输出 ``Z`` 或 ``+00:00``
    两种形态——同一时刻两种写法，验链在另一台机器上就会误报断链。
    """
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f+00:00")


# as_utc 现由 core/ids.py 提供（服务层的时间归一也要用它），此处转出以保持既有导入路径可用。
as_utc = _as_utc


def compute_event_hash(
    *,
    seq: int,
    tenant_id: int,
    project_id: int | None,
    actor_id: int | None,
    event_type: str,
    subject_type: str,
    subject_id: str,
    artifact_hash: str,
    payload: dict[str, Any],
    prev_hash: str,
    occurred_at: datetime,
) -> str:
    """按 be-06 §6.1 计算 ``event_hash``。

    🔴 ``tenant_id`` / ``project_id`` **必须参与**（2026-10-16 修正）：原式把它们留在覆盖之外，
    后果是「把一条事件改挂到别的租户名下，验链照样通过」——多租户账本的核心价值恰恰是
    「谁在哪个租户做了什么」，而它的归属却不可验证。

    另有两条**调用方必须保证**的前提（见 be-06 §6.1 规则表）：
    ``payload`` 内不得出现 float 与 datetime；比率用字符串（如 ``"0.0500"``）。
    ``canonical_json`` 的 ``default=str`` 会把 datetime 变成平台相关格式，
    从而让同一语义在两台机器上算出不同哈希。
    """
    parts = [
        str(seq),
        str(tenant_id),
        "0" if project_id is None else str(project_id),
        "0" if actor_id is None else str(actor_id),
        event_type,
        subject_type or "",
        escape(subject_id or ""),
        artifact_hash or "",
        escape(canonical_json(payload)),
        prev_hash,
        occurred_at_utc_iso(occurred_at),
    ]
    return sha256_hex("|".join(parts).encode("utf-8"))


def _assert_payload_clean(event: LedgerEvent) -> None:
    """写入前的脱敏前置校验（be-06 §6.2），命中即拒。"""
    hits = find_sensitive_paths(event.payload)
    if hits:
        raise SensitivePayloadError(
            f"证据事件 payload 命中敏感字段，拒绝入账：{', '.join(sorted(hits))}。"
            "账本永久保留，密钥一旦写入只能靠删链才能消除——请在调用方改为传引用标识。"
        )
    # subject_id 例外白名单：jti 是 UUID 形态，不该被当密钥拦下；artifact_hash 是 hex64，不检测。
    if event.subject_type in {"secret", "vault"} and event.subject_id:
        raise SensitivePayloadError(
            "subject_type=secret/vault 时 subject_id 只允许是 Vault 路径，不允许携带凭据"
        )


@dataclass(frozen=True, slots=True)
class VerifyReport:
    """验链结果。

    ``first_broken_seq`` 为 ``None`` 且 ``ok`` 为真时，才代表整条链自洽。
    只报「第几条坏了」而不抛异常：审计场景要的是完整报告，不是第一个错误就中断。

    ``broken_reason`` 给人看（为什么坏），``broken_kind`` 给机器看（哪一类坏）。
    两者都要：只有散文，前端没法按类型展示不同的修复动作；
    只有枚举，运维看到 ``hash_mismatch`` 也不知道具体是哪一条的哪个字段。
    """

    ok: bool
    checked_events: int
    checked_blocks: int
    first_broken_seq: int | None = None
    broken_reason: str | None = None
    # 取值与前端 LedgerBreakReason 一致；**加值要同步前端**，
    # 否则前端拿到未知字符串会走 default 分支，把它显示成「未知错误」
    broken_kind: str | None = None
    checked_at: datetime = field(default_factory=utcnow)


class EvidenceLedger:
    """哈希链账本。

    ``append`` **不 commit、不开事务**——事务边界归调用方。理由是账本几乎从不单独写入：
    它总是与「业务表的那次写入」同生共死，否则会出现「业务改了但没留证据」或反之。
    事务归属只有一处（service 层），账本不替它做主。
    """

    def __init__(self, *, signer: Signer | None = None, checkpoint_interval: int = 1000) -> None:
        if checkpoint_interval <= 0:
            raise ValueError("checkpoint_interval 必须为正整数")
        self._signer = signer
        self._checkpoint_interval = checkpoint_interval

    # ---------------- 读 ----------------

    async def chain_head(self, session: AsyncSession) -> tuple[int, str]:
        """返回 ``(链尾 seq, 链尾 event_hash)``；空链返回 ``(0, ZERO_HASH)``。"""
        row = (
            await session.execute(
                select(EvidenceEvent.seq, EvidenceEvent.event_hash)
                .order_by(EvidenceEvent.seq.desc())
                .limit(1)
            )
        ).first()
        if row is None:
            return 0, ZERO_HASH
        return int(row[0]), str(row[1])

    # ---------------- 写 ----------------

    async def append(self, session: AsyncSession, event: LedgerEvent) -> EvidenceEvent:
        """落一条事件，返回**已 flush**（未 commit）的 ORM 对象。

        调用方负责：① 事务边界；② 事务隔离级别为 SERIALIZABLE
        （串行化失败时由调用方重试，见 :func:`is_retryable`）。
        隔离级别不能在这里设——进到本函数时事务往往已经 BEGIN 了。
        """
        _assert_payload_clean(event)
        seq, prev_hash = await self._lock_tail_and_next_seq(session)
        occurred_at = event.occurred_at or utcnow()
        if occurred_at.tzinfo is None or occurred_at.utcoffset() is None:
            # 裸 datetime 会按本地时区解释，于是同一事件在容器(UTC)与开发机(UTC+8)上哈希不同
            raise LedgerError("occurred_at 必须是带时区的时间")

        event_hash = compute_event_hash(
            seq=seq,
            tenant_id=event.tenant_id,
            project_id=event.project_id,
            actor_id=event.actor_id,
            event_type=event.event_type.value,
            subject_type=event.subject_type,
            subject_id=event.subject_id,
            artifact_hash=event.artifact_hash,
            payload=event.payload,
            prev_hash=prev_hash,
            occurred_at=occurred_at,
        )
        row = EvidenceEvent(
            seq=seq,
            tenant_id=event.tenant_id,
            project_id=event.project_id,
            actor_id=event.actor_id,
            event_type=event.event_type,
            subject_type=event.subject_type,
            subject_id=event.subject_id,
            artifact_hash=event.artifact_hash,
            payload=event.payload,
            prev_hash=prev_hash,
            event_hash=event_hash,
            occurred_at=occurred_at,
        )
        session.add(row)
        await session.flush()

        if self._signer is not None and seq % self._checkpoint_interval == 0:
            await self.sign_checkpoint(session, to_event_seq=seq)
        return row

    async def append_many(self, session: AsyncSession, events: Sequence[LedgerEvent]) -> list[EvidenceEvent]:
        """按给定顺序连续落账。

        逐条走 ``append``（每条都重新锁链尾）。不做「一次算完再批量插」的优化：
        批量插入会让两个并发批次交错写同一段 seq 区间，而锁只保护单条——
        正确性优先于这点吞吐。
        """
        return [await self.append(session, e) for e in events]

    async def _lock_tail_and_next_seq(self, session: AsyncSession) -> tuple[int, str]:
        """在锁内取链尾并算出下一个 ``seq``。

        ``seq`` 是 ``MAX(seq)+1`` 而**不是**序列/自增：序列在回滚时会留空洞，
        而验链要求 seq 连续，一个空洞会让「有人删了一行」与「有人回滚了一次」无法区分。
        """
        # 在 PG 上加行锁：两个并发 append 会争同一把锁，从而串行化「读链尾 → 算 seq → 插入」。
        # 这是链正确性的**主要**保证（READ COMMITTED 下即成立）；
        # be-06 §6.4 要求的 SERIALIZABLE 是额外的纵深防御，不是唯一依赖。
        on_pg = session.bind is not None and session.bind.dialect.name == "postgresql"
        if on_pg:
            # 表空时无行可锁，两个并发 append 会同时读到 None 而算出同一个 seq=1。
            # 顾问锁把这段窗口补上，且它是事务级的：随 commit/rollback 自动释放。
            await session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _EMPTY_CHAIN_LOCK_KEY})

        # 逐段构造而不显式标注类型：``with_for_update()`` 会把 Select 的泛型参数
        # 收窄成具体的 (int, str)，手写 Select[Any] 反而与之冲突。
        stmt = select(EvidenceEvent.seq, EvidenceEvent.event_hash)
        if on_pg:
            stmt = stmt.with_for_update()
        row = (await session.execute(stmt.order_by(EvidenceEvent.seq.desc()).limit(1))).first()
        if row is None:
            return 1, ZERO_HASH
        return int(row[0]) + 1, str(row[1])

    # ---------------- 检查点 ----------------

    async def sign_checkpoint(self, session: AsyncSession, *, to_event_seq: int) -> EvidenceLedgerBlock:
        """为已落账区间签一个检查点。

        签的是 ``chain_head_hash|from|to``：只签链尾哈希等于只签了「最后一条」，
        中间被整段替换仍能通过——而区间端点一起签，替换任意一段都会让签名对不上。
        """
        if self._signer is None:
            raise LedgerError("未配置签名器，无法生成检查点")

        head = (
            await session.execute(select(EvidenceEvent.event_hash).where(EvidenceEvent.seq == to_event_seq))
        ).scalar_one()
        prev_block = (
            await session.execute(
                select(EvidenceLedgerBlock).order_by(EvidenceLedgerBlock.seq.desc()).limit(1)
            )
        ).scalar_one_or_none()
        from_seq = 1 if prev_block is None else int(prev_block.to_event_seq) + 1

        message = f"{head}|{from_seq}|{to_event_seq}".encode()
        signature = self._signer.sign(message).hex()
        block = EvidenceLedgerBlock(
            seq=to_event_seq,
            from_event_seq=from_seq,
            to_event_seq=to_event_seq,
            chain_head_hash=str(head),
            signature=signature,
            key_id=self._signer.key_id,
            signed_at=utcnow(),
        )
        session.add(block)
        await session.flush()
        return block

    # ---------------- 验链 ----------------

    async def verify_chain(
        self, session: AsyncSession, *, from_seq: int = 1, to_seq: int | None = None
    ) -> VerifyReport:
        """从零独立重算全链（或 ``from_seq`` 起的这一段）。

        重算而非「比对存储值」：只比对存储值等于相信被检验的对象自证清白，
        真正的篡改（改 payload 同时改 event_hash）会被判为合法。

        ``from_seq > 1`` 时**不校验首条的 ``prev_hash``**：那一段的链条本就缺了头，
        拿它去对 ``ZERO_HASH`` 必然失败，而这会让「只想复核最近一千条」变成一个
        永远返回断链的功能。报告的 ``ok`` 此时只代表**这一段**自洽，
        调用方必须把 ``from_seq`` 一起展示出去（前端 DTO 有 ``started_from_block_seq``）。
        """
        if from_seq < 1:
            raise ValueError("from_seq 必须 >= 1")
        if to_seq is not None and to_seq < from_seq:
            raise ValueError("to_seq 不能小于 from_seq")

        stmt = select(EvidenceEvent).where(EvidenceEvent.seq >= from_seq).order_by(EvidenceEvent.seq.asc())
        if to_seq is not None:
            stmt = stmt.where(EvidenceEvent.seq <= to_seq)
        rows = (await session.execute(stmt)).scalars().all()

        expected_seq = from_seq
        # 从中间起验时 prev_hash 未知 —— 用 None 表示「这一条不校验前序」
        prev_hash: str | None = ZERO_HASH if from_seq == 1 else None
        checked = 0

        for row in rows:
            if int(row.seq) != expected_seq:
                return VerifyReport(
                    ok=False,
                    checked_events=checked,
                    checked_blocks=0,
                    first_broken_seq=int(row.seq),
                    broken_reason=f"seq 不连续：期望 {expected_seq}，实到 {row.seq}（有事件被删除）",
                    broken_kind="missing_event",
                )
            if prev_hash is not None and str(row.prev_hash) != prev_hash:
                return VerifyReport(
                    ok=False,
                    checked_events=checked,
                    checked_blocks=0,
                    first_broken_seq=int(row.seq),
                    broken_reason=f"prev_hash 与前一条的 event_hash 不一致（seq={row.seq} 之前被改动或插入）",
                    broken_kind="prev_hash_mismatch",
                )
            recomputed = compute_event_hash(
                seq=int(row.seq),
                tenant_id=int(row.tenant_id),
                project_id=None if row.project_id is None else int(row.project_id),
                actor_id=None if row.actor_id is None else int(row.actor_id),
                event_type=row.event_type.value,
                subject_type=row.subject_type or "",
                subject_id=row.subject_id or "",
                artifact_hash=row.artifact_hash or "",
                payload=row.payload or {},
                prev_hash=str(row.prev_hash),
                occurred_at=as_utc(row.occurred_at),
            )
            if recomputed != str(row.event_hash):
                return VerifyReport(
                    ok=False,
                    checked_events=checked,
                    checked_blocks=0,
                    first_broken_seq=int(row.seq),
                    broken_reason=f"event_hash 重算不符（seq={row.seq} 的内容被改动，含租户或项目归属）",
                    broken_kind="hash_mismatch",
                )
            prev_hash = str(row.event_hash)
            expected_seq += 1
            checked += 1

        # 只统计**完全落在已验区间内**的检查点。判据是两边都夹住：
        # 只要 from_event_seq 就够的话，一个覆盖 1..1000 的块在「只验了 3 条」时也会被算进来，
        # 于是报告显示「验了 3 条事件、覆盖 1 个检查点」——而那个检查点里的 997 条谁都没看过
        verified_to = expected_seq - 1
        block_stmt = select(EvidenceLedgerBlock).where(
            EvidenceLedgerBlock.from_event_seq >= from_seq,
            EvidenceLedgerBlock.to_event_seq <= verified_to,
        )
        blocks = (await session.execute(block_stmt)).scalars().all()
        return VerifyReport(ok=True, checked_events=checked, checked_blocks=len(blocks))


def is_retryable(exc: BaseException) -> bool:
    """判断是否为可重试的串行化冲突。

    SQLSTATE 从驱动异常上逐层往下找：asyncpg 把它放在 ``sqlstate``，
    psycopg 放在 ``pgcode``，且都可能被 SQLAlchemy 包一层 ``.orig``。
    """
    seen: set[int] = set()
    node: BaseException | None = exc
    while node is not None and id(node) not in seen:
        seen.add(id(node))
        for attr in ("sqlstate", "pgcode"):
            if getattr(node, attr, None) in RETRYABLE_SQLSTATES:
                return True
        node = node.__cause__ or node.__context__
    return False


async def retry_delay(attempt: int) -> None:
    """指数退避 + 抖动。抖动只允许影响**等待时长**，绝不允许进入任何参与哈希的值。"""
    delay = min(2**attempt * 0.01, 0.5) + random.uniform(0.0, 0.01)  # noqa: S311 - 退避抖动无需密码学强度
    await asyncio.sleep(delay)


class LedgerSigner:
    """Ed25519 检查点签名器。

    私钥只在此对象内物化，不暴露 getter、不落日志。``key_id`` 取私钥文件名
    （不含扩展名）——轮换密钥时新文件即新 key_id，历史检查点的 key_id 保持不变，
    于是验签方知道该用哪把公钥。
    """

    def __init__(self, private_key_path: Path | str, *, key_id: str | None = None) -> None:
        self._path = Path(private_key_path)
        self.key_id = key_id or self._path.stem
        self._key: Any = None

    def _load(self) -> Any:
        if self._key is None:
            from cryptography.hazmat.primitives.serialization import load_pem_private_key

            self._key = load_pem_private_key(self._path.read_bytes(), password=None)
        return self._key

    def sign(self, message: bytes) -> bytes:
        return bytes(self._load().sign(message))


__all__ = [
    "MAX_APPEND_RETRIES",
    "RETRYABLE_SQLSTATES",
    "ZERO_HASH",
    "EvidenceLedger",
    "LedgerError",
    "LedgerEvent",
    "LedgerSigner",
    "SensitivePayloadError",
    "VerifyReport",
    "as_utc",
    "compute_event_hash",
    "escape",
    "is_retryable",
    "occurred_at_utc_iso",
    "retry_delay",
]
