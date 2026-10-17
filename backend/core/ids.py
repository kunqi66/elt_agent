"""ID 与指纹工具。

- **实体 ID 是整数**（`BIGSERIAL`）：字典总则、`models/base.py#PKMixin`、
  前端 DTO 三方一致，改动等同破坏兼容。本模块不生成实体 ID。
- `new_uuid()` 只用于**不可枚举的令牌标识**（JWT 的 `jti`、Capability 的 `jti`），
  它们需要「猜不到」而不是「可读」。
- 请求关联 ID 用短随机串，写入日志与响应体，便于跨 api/worker 追踪。
- 指纹统一用 SHA256 十六进制小写；**规范化 JSON** 是计算指纹的唯一输入形式，
  否则字典顺序/空格差异会让「同一份制品」算出不同指纹，直接破坏不可变性校验。
"""

from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from datetime import UTC, datetime
from typing import Any


def new_uuid() -> str:
    return str(uuid.uuid4())


def new_request_id() -> str:
    return f"req_{secrets.token_hex(8)}"


def new_idempotency_key() -> str:
    return f"idem_{secrets.token_hex(16)}"


def canonical_json(payload: Any) -> str:
    """规范化 JSON：键排序、无多余空白、非 ASCII 原样保留。指纹计算的唯一入口。"""
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)


def sha256_hex(data: str | bytes) -> str:
    raw = data.encode("utf-8") if isinstance(data, str) else data
    return hashlib.sha256(raw).hexdigest()


def fingerprint(payload: Any) -> str:
    """对任意可序列化对象计算 SHA256 指纹。"""
    return sha256_hex(canonical_json(payload))


def as_utc(value: datetime) -> datetime:
    """把裸 ``datetime`` 视为 UTC。

    生产是**空操作**：PG 的 ``timestamptz`` 读回来永远带时区。
    它是为 SQLite 而存在的——SQLite 不保留时区，读回来是裸值，
    而 ``astimezone`` 会把裸值当**本地时间**解释，于是同一条记录在 UTC 容器与
    UTC+8 开发机上算出不同结果，且两边都自洽（本地必假报、生产必真过）。

    凡是「从库里读出时间再交给纯函数判定」的路径都要过这一层：
    纯函数应当只接受带时区的时间（这样它在任何机器上语义唯一），
    归一化是服务层作为存储边界该做的翻译。
    """
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value


def utcnow() -> datetime:
    return datetime.now(UTC)
