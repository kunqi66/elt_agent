"""证据账本的读写契约（be-06）。

**路径与 DTO 以 ``frontend/src/api/governance.ts`` 为准。**与 be-06 §3.1 原设计的差异：

- **`GET /governance/evidence`** 取代 be-06 的 `GET /governance/ledger`。
  用前端的：账本对用户的名字就是「证据」。
- **游标分页取代 `page` / `page_size`**，这条比路径更重要：账本持续 append，
  offset 分页会漏读或重读——正在翻页时新事件插进来，第二页会重复第一页的尾巴。
  游标基于 `seq` 才稳定。
- **`POST /governance/evidence/verify`** 取代 be-06 的 `GET .../verify`。
  用前端的：验链要重算全链，是有代价的动作，不该能被 GET 缓存或预取。
- **导出保留 be-06 的设计**（§6.5 定了格式），前端没有这个入口。
  审计要能拿走一份离线可验的副本，而这件事前端不做。

``broken_reason`` 是前端认识的枚举，``broken_detail`` 是同一件事的散文展开——
两者都留，因为前端按类型显示不同的修复动作，而运维需要具体的 seq 与字段。
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from models.enums import EvidenceEventType

# 与前端 ``LedgerBreakReason`` 逐字一致。**加值要同步前端**：
# 前端拿到未知字符串会走 default 分支显示成「未知错误」，两侧都自洽、互不知情
LedgerBreakReason = Literal["hash_mismatch", "prev_hash_mismatch", "signature_invalid", "missing_event"]

# 一次最多取多少条。账本的 payload 可能含 base64 片段，200 条已经是几百 KB
_MAX_LIMIT = 200


class EvidenceEventRead(BaseModel):
    """一条证据事件。

    ``prev_hash`` / ``event_hash`` 都返回：前端要把链条画出来，
    而「每条都显示了哈希」本身也是给审计的视觉证据——
    只返回一个「已验证」徽标的话，用户无从自行核对。
    """

    id: int
    seq: int
    tenant_id: int
    project_id: int | None = None
    actor_id: int | None = None
    # 前端列上要显示人名。留空串而不是 None：前端模板直接渲染，
    # null 会显示成 "null"，而「操作者已注销」用空串更自然
    actor_name: str = ""
    event_type: str
    subject_type: str
    subject_id: str
    artifact_hash: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)
    prev_hash: str
    event_hash: str
    occurred_at: datetime
    block_seq: int | None = None


class EvidencePage(BaseModel):
    """游标分页。``next_cursor`` 为 None 表示到底了。

    没有 ``total``：账本持续增长，给出总数的代价是一次全表 count，
    而它对「翻下一页」这件事毫无帮助。
    """

    items: list[EvidenceEventRead] = Field(default_factory=list)
    next_cursor: str | None = None


class EvidenceFilter(BaseModel):
    """查询条件。

    ``project_id`` 必填（前端注释：账本按租户 + 项目隔离，缺省查询会被后端拒绝）。
    这一条不是形式主义：不带项目过滤的账本查询等于一个跨项目的操作日志接口，
    而租户内不同项目之间的隔离是本系统多租户模型的一部分。
    """

    model_config = ConfigDict(extra="forbid")

    project_id: int = Field(ge=1)
    event_type: list[EvidenceEventType] | None = None
    subject_id: str | None = Field(default=None, max_length=128)
    pipeline_version_id: int | None = Field(default=None, ge=1)
    execution_run_id: int | None = Field(default=None, ge=1)
    occurred_from: datetime | None = None
    occurred_to: datetime | None = None
    cursor: str | None = Field(default=None, max_length=64)
    limit: int = Field(default=50, ge=1, le=_MAX_LIMIT)

    @model_validator(mode="after")
    def _range_ordered(self) -> EvidenceFilter:
        if self.occurred_from and self.occurred_to and self.occurred_from > self.occurred_to:
            raise ValueError("occurred_from 不能晚于 occurred_to")
        if self.pipeline_version_id and self.execution_run_id:
            # 两者都按 subject 过滤（见 ``_subject_scope``），而一条事件的 subject 只有一个。
            # 同时给出必然是空结果——报错比返回空列表好：空列表看起来像「确实没有这类记录」
            raise ValueError("pipeline_version_id 与 execution_run_id 不能同时使用")
        if self.subject_id and (self.pipeline_version_id or self.execution_run_id):
            raise ValueError("subject_id 与 pipeline_version_id / execution_run_id 不能同时使用")
        return self


class VerifyRequest(BaseModel):
    """验链请求。``from_seq`` 默认 1（从创世条目起）。

    **不接受 project_id 之外的过滤条件**：链是全局的，按项目筛一个子集去验
    会在每个项目边界上报「prev_hash 不一致」——那不是发现了篡改，是筛错了。
    所以 project_id 在这里的作用是**授权**（你必须是该项目的 auditor），不是过滤。
    """

    model_config = ConfigDict(extra="forbid")

    project_id: int = Field(ge=1)
    from_seq: int = Field(default=1, ge=1)
    to_seq: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _range_ordered(self) -> VerifyRequest:
        if self.to_seq is not None and self.to_seq < self.from_seq:
            raise ValueError("to_seq 不能小于 from_seq")
        return self


class LedgerVerifyResult(BaseModel):
    ok: bool
    checked_events: int
    checked_blocks: int
    broken_at_seq: int | None = None
    broken_reason: LedgerBreakReason | None = None
    # 散文版原因。前端类型里没有它，但**断链时不给具体解释等于让人从零开始查**——
    # 额外字段是向后兼容的，而缺了它，报告就只能说「坏了，在某处」
    broken_detail: str | None = None
    # None = 从创世条目起验。非 None = **只验了这一段**，
    # 全绿不代表完整（前端据此在界面上加「部分验证」的提示）
    started_from_block_seq: int | None = None
    finished_at: datetime


class EvidenceExportQuery(BaseModel):
    """导出条件。与查询共用过滤器，另加格式。"""

    model_config = ConfigDict(extra="forbid")

    format: Literal["json", "csv"] = "json"
    max_events: Annotated[int, Field(ge=1, le=100_000)] = 50_000


__all__ = [
    "EvidenceEventRead",
    "EvidenceExportQuery",
    "EvidenceFilter",
    "EvidencePage",
    "LedgerBreakReason",
    "LedgerVerifyResult",
    "VerifyRequest",
]
