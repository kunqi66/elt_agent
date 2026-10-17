"""证据账本独立验链脚本（be-06）。

**本脚本刻意不复用 ``harness.ledger``。**这是它存在的全部意义：

    用被审计对象的代码去验证被审计对象，等于让它自证清白。
    ``compute_event_hash`` 里任何一处改动（漏掉一个字段、换个分隔符、
    规范 JSON 的方式变了）都会同时改「写入」与「验证」两侧，
    验链照样全绿——而链已经不再是原来那条。

所以这里按 be-06 §6.1 的规则表**重写一遍**规范化与拼接逻辑，
只用标准库 + 数据库驱动。两边算出的哈希必须逐字节相同，
有差异就说明有一侧改了而另一侧没跟上——那正是要报出来的东西。

用法（在 ``backend/`` 下）::

    uv run python scripts/verify_ledger.py                 # 读 settings 里的库
    uv run python scripts/verify_ledger.py --dsn postgresql://...
    uv run python scripts/verify_ledger.py --from-seq 1000 # 只验一段
    uv run python scripts/verify_ledger.py --json          # 机器可读输出

退出码：``0`` 全链自洽，``1`` 断链，``2`` 用法或连接错误。
CI 与交付验收按退出码判定，**不要靠肉眼读中文输出**（CLAUDE.md §9：Windows 控制台是 GBK）。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

# 空链的起点。写成字面量而不是 import：这个常量一旦被改动，
# 从零开始的整条链会整体重算，而脚本必须能独立地发现这件事发生了
ZERO_HASH = "0" * 64

# be-06 §6.1 的字段顺序。多一个、少一个、换个位置都会算出不同的哈希——
# 这正是「顺序即契约」，所以它在这里被显式抄一遍
_FIELD_ORDER = (
    "seq",
    "tenant_id",
    "project_id",
    "actor_id",
    "event_type",
    "subject_type",
    "subject_id",
    "artifact_hash",
    "payload",
    "prev_hash",
    "occurred_at",
)


def escape(value: str) -> str:
    """转义分隔符。反斜杠必须先转，否则会把已插入的转义符再转一次。"""
    return value.replace("\\", "\\\\").replace("|", "\\|")


def canonical_json(payload: object) -> str:
    """规范化 JSON：键排序、无空白、非 ASCII 原样保留、其余类型 ``str()``。"""
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)


def utc_iso(value: datetime) -> str:
    """固定形态的 UTC 时间串，微秒定长。

    ``isoformat()`` 在微秒为 0 时省略小数部分，同一时刻会有两种写法——
    拿它去验链会在「整秒事件」上误报断链。
    """
    if value.tzinfo is None:
        # 裸值按 UTC 解释：生产是 timestamptz，不会有裸值；
        # 这里兜的是「有人拿 sqlite 导出的小样本」这种排查场景
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f+00:00")


def event_hash(row: dict[str, object]) -> str:
    """按 be-06 §6.1 重算 ``event_hash``。"""
    parts = [
        str(row["seq"]),
        str(row["tenant_id"]),
        "0" if row["project_id"] is None else str(row["project_id"]),
        "0" if row["actor_id"] is None else str(row["actor_id"]),
        str(row["event_type"]),
        str(row["subject_type"] or ""),
        escape(str(row["subject_id"] or "")),
        str(row["artifact_hash"] or ""),
        escape(canonical_json(row["payload"] or {})),
        str(row["prev_hash"]),
        utc_iso(row["occurred_at"]),  # type: ignore[arg-type]
    ]
    assert len(parts) == len(_FIELD_ORDER), "字段数变了，拼接契约已改动"
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


_SELECT = (
    "SELECT seq, tenant_id, project_id, actor_id, event_type, subject_type, subject_id, "
    "       artifact_hash, payload, prev_hash, event_hash, occurred_at "
    "FROM evidence_event WHERE seq >= $1 ORDER BY seq ASC"
)


def check(rows: list[dict[str, object]], *, start_seq: int = 1) -> tuple[bool, int, str | None]:
    """重算全链。返回 ``(是否自洽, 检查条数, 断链原因)``。

    **不比对存储的 event_hash 就算通过**：那只证明「有人算过一次」。
    这里的判据是「重算结果必须等于存储值」，也就是必须同时满足
    「内容没被改」与「写入侧用的算法与我这份独立实现一致」。
    """
    expected_seq = start_seq
    prev_hash = ZERO_HASH if start_seq == 1 else None
    checked = 0

    for row in rows:
        seq = int(row["seq"])  # type: ignore[arg-type]
        if seq != expected_seq:
            return False, checked, f"seq 不连续：期望 {expected_seq}，实到 {seq}（有事件被删除或插入）"
        if prev_hash is not None and str(row["prev_hash"]) != prev_hash:
            return (
                False,
                checked,
                f"seq={seq} 的 prev_hash 与前一条的 event_hash 不一致（链在此处被截断重接）",
            )
        recomputed = event_hash(row)
        if recomputed != str(row["event_hash"]):
            return False, checked, f"seq={seq} 的 event_hash 重算不符（该条内容已被改动）"
        prev_hash = str(row["event_hash"])
        expected_seq = seq + 1
        checked += 1

    return True, checked, None


def fetch(dsn: str, start_seq: int, limit: int | None) -> list[dict[str, object]]:
    """直连读取。**不经过 ORM 与 service**：多一层就多一处可能掩盖问题的翻译。

    用 ``asyncpg`` 而不是 psycopg：psycopg **不在** ``pyproject.toml`` 的依赖里
    （它的同步 DSN 属性 ``postgres_dsn_sync`` 没有任何消费者），裸装还要求本机有
    libpq——实测在开发机上直接 ``libpq library not found``。而 asyncpg 是本项目的
    生产驱动、处处都在。脚本的「独立性」要求落在**验链算法**上（见模块 docstring），
    不是落在一个用不到的第二个驱动上。
    """
    import asyncpg

    sql = _SELECT + (" LIMIT $2" if limit else "")
    params: tuple[object, ...] = (start_seq, limit) if limit else (start_seq,)

    async def run() -> list[dict[str, object]]:
        conn = await asyncpg.connect(dsn)
        try:
            # 🔴 jsonb 必须显式解成 dict：asyncpg 裸用会把 jsonb 当**文本**回传
            # （SQLAlchemy 那边是它自己注册的 codec，所以 ORM 里看不出来）。
            # 漏了这一步的后果不是报错而是**误报断链**——payload 变成带引号的字符串，
            # 规范化后的哈希自然对不上，脚本会一口咬定"该条内容已被改动"。
            for type_name in ("json", "jsonb"):
                await conn.set_type_codec(
                    type_name,
                    encoder=json.dumps,
                    decoder=json.loads,
                    schema="pg_catalog",
                )
            return [dict(r) for r in await conn.fetch(sql, *params)]
        finally:
            await conn.close()

    return asyncio.run(run())


def resolve_dsn(explicit: str | None) -> str:
    """优先用参数；否则读 ``core.config`` 里的库地址。

    这里允许 import 项目代码——取连接串与「验证账本算法」是两件事，
    独立性的要求落在后者。反过来把 DSN 也硬编码进脚本，才是真的不安全。
    """
    if explicit:
        return explicit
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from core.config import settings

    # 属性名是 ``postgres_dsn``（不是 ``DATABASE_URL``）：写错时 Pydantic Settings 只抛
    # AttributeError，被下面的 ``except`` 译成一句「读取账本失败：AttributeError」，
    # 于是「脚本根本跑不起来」伪装成「连不上库」——验收时最容易被放过的一类失败
    dsn = settings.postgres_dsn
    return str(dsn).replace("postgresql+asyncpg://", "postgresql://")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="证据账本独立验链（不依赖被验代码）")
    parser.add_argument("--dsn", default=None, help="覆盖连接串；默认读 settings")
    parser.add_argument("--from-seq", type=int, default=1, help="从第几条开始（默认 1）")
    parser.add_argument("--limit", type=int, default=None, help="最多读多少条")
    parser.add_argument("--json", action="store_true", help="输出 JSON（给 CI 用）")
    args = parser.parse_args(argv)

    if args.from_seq < 1:
        print("--from-seq 必须 >= 1", file=sys.stderr)
        return 2

    try:
        rows = fetch(resolve_dsn(args.dsn), args.from_seq, args.limit)
    except (AttributeError, TypeError, ImportError, NameError) as exc:
        # 这几种只可能来自脚本自身的缺陷（属性名写错、少个 import），不可能来自网络。
        # 带上原始消息：它们的文本里没有连接串，而「读取账本失败：AttributeError」
        # 会把「脚本根本跑不起来」伪装成「连不上库」——验收时最容易被放过的失败
        print(f"脚本自身出错：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        # 连接类异常只回类型：消息里可能带连接串（进而带口令）
        print(f"读取账本失败：{type(exc).__name__}", file=sys.stderr)
        return 2

    ok, checked, reason = check(rows, start_seq=args.from_seq)

    if args.json:
        print(json.dumps({"ok": ok, "checked": checked, "reason": reason}, ensure_ascii=False))
    elif ok:
        # 断链时把「从第几条开始验」也印出来：从 1000 起验的链短一截，
        # 全绿只说明这一段自洽，不说明前面没被删
        print(f"OK checked={checked} from_seq={args.from_seq}")
    else:
        print(f"BROKEN checked={checked} at_seq={args.from_seq + checked} reason={reason}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
