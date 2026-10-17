"""原子发布与受管回滚的 DTO（be-18 §3.1）。

三条取舍写在这里，因为它们决定了这些字段**为什么长这样**：

- **没有任何表名字段来自请求体**。``RollbackRequest`` 只有一个 ``reason``；
  影子表 / 正式表名一律由服务端从 ``execution_run`` 读出后放进响应里给前端看。
  请求体里出现表名，等于把「删哪张表」变成一个客户端可控的输入。
- **指纹是字符串，不是数值**。``pre_fingerprint`` / ``post_fingerprint`` 的形态是
  ``"<行数>:<主键 CRC32 之和>"``——它就是**一个不可拆分的观测值**，拆成两个字段后
  前端会忍不住去比较行数，而「行数相同、主键和不同」的两次观测会被判成同一次。
- **``fingerprint_scope`` 必须下发**。指纹只覆盖行数与主键集合（§6.2 的已知局限），
  不带上这个词，前端与审计会把「rowcount+pk_crc32 一致」读成「内容一致」。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from models.enums import RunStatus

#: 读 DTO：库里/服务里的超集字段安静丢弃（写的一侧多一个键，读的一侧不该整页 422）。
_READ = ConfigDict(extra="ignore")
#: 写 DTO：多传一个键就是调用方对契约的误解，立刻 422。
_WRITE = ConfigDict(extra="forbid")

#: 发布状态。``not_attempted`` 与 ``in_progress`` 都**不是** ``final_state``——
#: 前两者是「还没走到对账」，后者是「对账给出了结论」，混在一个枚举里会让
#: 「没试过」与「试了但不知道」看起来一样。
PublishState = Literal["not_attempted", "in_progress", "published", "failed", "unknown"]

#: 回滚状态。``failed`` 表示「执行过且没成功」，与「没执行过」（``not_attempted``）分开。
RollbackState = Literal["not_attempted", "in_progress", "succeeded", "failed"]


class RollbackRequest(BaseModel):
    """回滚请求体：只有一个理由。

    ``Idempotency-Key`` 走请求头（``api/idempotency.py`` 的中间件统一处理），
    不接受 body 里再传一份——同一个语义两处入口，服务端无法知道哪个才是
    「客户端认为生效的那个」。
    """

    model_config = _WRITE

    reason: str = Field(default="", max_length=255)


class PublishResultDTO(BaseModel):
    """一次发布尝试的结论（``publish_run`` 的返回）。

    发布**无 HTTP 触发入口**（§3.1），所以它只跨 ``tasks`` → ``services`` 这一步，
    但仍是 Pydantic：它带着两个指纹与最终态，是「这次发布到底发生了什么」的唯一摘要，
    worker 侧的日志与测试都读它。
    """

    model_config = _READ

    run_id: int
    status: RunStatus
    publish_state: PublishState
    #: 执行后对账的结论。``None`` 只在 ``already_published``（幂等短路）时出现
    final_state: Literal["published", "not_published", "unknown"] | None = None
    shadow_table: str = ""
    formal_table: str = ""
    pre_fingerprint: str | None = None
    post_fingerprint: str | None = None
    fingerprint_scope: str = ""
    #: 人话摘要。写进 ``execution_run.error_message`` 的就是它，前端直接展示
    message: str = ""
    occurred_at: datetime | None = None


class RollbackResultDTO(BaseModel):
    """一次回滚请求的结果。

    ``request_rollback`` 只走到「命令已入队」这一步，因此 ``shadow_dropped`` 之类的
    **执行结果**在这里是 ``None``：它要等 worker 执行完、再看 ``GET /publish``。
    让请求接口返回一个「我以为会删掉」的布尔值，比不返回更糟——前端会把它当事实。
    """

    model_config = _READ

    run_id: int
    status: RunStatus
    #: 已经回滚过（``rolled_back`` 上的重复请求）→ 不重复执行，幂等返回既有结论
    already_rolled_back: bool = False
    #: 第几次尝试。超 stale 窗口后的重入会 +1（§6.5）
    attempt: int = 1
    shadow_table: str = ""
    formal_table: str = ""
    message: str = ""


class RollbackStateDTO(BaseModel):
    """回滚侧的状态（``GET /runs/{id}/publish`` 的嵌套块）。"""

    model_config = _READ

    state: RollbackState = "not_attempted"
    attempt: int = 0
    shadow_dropped: bool | None = None
    formal_unchanged: bool | None = None
    occurred_at: datetime | None = None
    message: str = ""


class PublishStateDTO(BaseModel):
    """发布与回滚的**全部已知事实**（``GET /runs/{id}/publish``）。

    读接口，权限是「本项目任一职责槽」（maker / operator / auditor 都能看）：
    发布状态不是秘密，而它恰恰是审计最需要的证据视图。
    """

    model_config = _READ

    run_id: int
    status: RunStatus
    publish_state: PublishState
    final_state: Literal["published", "not_published", "unknown"] | None = None
    shadow_table: str = ""
    formal_table: str = ""
    pre_fingerprint: str | None = None
    post_fingerprint: str | None = None
    fingerprint_scope: str = ""
    swap_at: datetime | None = None
    rollback: RollbackStateDTO = Field(default_factory=RollbackStateDTO)
    #: 诊断用（``reason`` / ``error_code`` / ``engine`` / ``ddl_error``）。
    #: 刻意是自由字典：它是**诊断信息**，不是契约字段；给它定死字段等于每加一条
    #: 诊断就要改一次契约，而读方（前端）只展示不解析。
    detail: dict[str, Any] = Field(default_factory=dict)


__all__ = [
    "PublishResultDTO",
    "PublishState",
    "PublishStateDTO",
    "RollbackRequest",
    "RollbackResultDTO",
    "RollbackState",
    "RollbackStateDTO",
]
