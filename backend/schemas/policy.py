"""策略判定预演的请求/响应 DTO（be-07 §3.1）。

**复用引擎 DTO 而不是逐字段镜像**。`ResourceRef` / `SensitiveColumn` /
`PolicyDecision` 都已经是 Pydantic 模型且不含身份字段（`ResourceRef` 里只有
「哪张表、哪个环境」），直接拿来做请求/响应体既省一层复制，也消除了
「镜像 DTO 与引擎 DTO 逐渐不一致」这种只有在复现历史判定时才会暴露的偏差。

唯一需要单独定义的是 `PolicyPreviewScope`：它对应引擎的 `ResourceScope`，
**但拿掉了 `tenant_id` / `project_id`**（理由见该类 docstring）。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from engines.policy import ResourceRef, ResourceScope, SensitiveColumn, ToolIntent
from models.enums import Env, SensitiveLevel


class PolicyPreviewScope(BaseModel):
    """`ResourceScope` 的线上形态：字段一一对应，但没有租户与项目。

    **为什么这两个字段必须缺省**：引擎的 `ResourceScope` 把 tenant_id / project_id
    定为必填（跨租户本身就是一条 P0，不该有默认值）。若把它直接当请求体，
    就等于让客户端从请求里声明自己是哪个租户——预演虽然不改数据，
    但它是判定逻辑的探针：能拿它测出「别的租户的资源会被判成几级」，
    同样是一次信息泄露。所以线上形态**根本不提供**这两个字段，由 API 层用登录态回填。
    """

    model_config = ConfigDict(extra="forbid")

    reads: list[ResourceRef] = Field(default_factory=list)
    writes: list[ResourceRef] = Field(default_factory=list)
    authorized_databases: list[str] = Field(default_factory=list)
    out_of_scope: list[ResourceRef] = Field(default_factory=list)
    cross_tenant_refs: list[ResourceRef] = Field(default_factory=list)
    unregistered_secret_refs: list[int] = Field(default_factory=list)
    writes_formal_table: bool = False
    writes_shadow_table: bool = False
    columns: list[SensitiveColumn] = Field(default_factory=list)

    def to_engine(self, *, tenant_id: int, project_id: int) -> ResourceScope:
        """回填身份后转成引擎 DTO。**这是唯一构造 `ResourceScope` 的地方**，
        于是「租户一定来自登录态」不靠调用方自觉。"""
        return ResourceScope(
            tenant_id=tenant_id,
            project_id=project_id,
            reads=tuple(self.reads),
            writes=tuple(self.writes),
            authorized_databases=frozenset(self.authorized_databases),
            out_of_scope=tuple(self.out_of_scope),
            cross_tenant_refs=tuple(self.cross_tenant_refs),
            unregistered_secret_refs=tuple(self.unregistered_secret_refs),
            writes_formal_table=self.writes_formal_table,
            writes_shadow_table=self.writes_shadow_table,
            columns=tuple(self.columns),
        )


class PolicyPreviewRequest(BaseModel):
    """预演输入：环境 + 数据分级 + 想做什么 + 对哪些表做。"""

    model_config = ConfigDict(extra="forbid")

    project_id: int = Field(ge=1)
    env: Env
    level: SensitiveLevel = SensitiveLevel.INTERNAL
    intent: ToolIntent
    scope: PolicyPreviewScope = Field(default_factory=PolicyPreviewScope)
