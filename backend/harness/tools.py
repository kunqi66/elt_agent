"""工具注册表：**副作用唯一出口**的清单（CLAUDE.md §5.1）。

全仓库只有这里能把「写外部系统 / 执行作业 / 改数据」登记成工具。
``api/`` 与 ``services/`` 不得直接调用 SeaTunnel、Doris DDL 或目标库驱动——
它们只能 ``ToolBroker.execute(ToolName.X, ...)``。CI 用 import 图断言这条（be-05 §9）。

注册表是**冻结**的：``register_tool`` 重复注册同一工具会抛错，而不是覆盖。
覆盖式注册意味着有人可以在运行期把 ``target.publish_swap`` 的实现换掉，
而账本里留下的仍是同一个工具名——审计会指向错误的执行者。
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from core.errors import ErrorCode
from engines.security import find_credential_values
from harness.errors import UnauthorizedToolError
from models.enums import RiskLevel, ToolName

if TYPE_CHECKING:
    # 只在类型检查期 import：运行期 ``broker`` 反过来 import 本模块，
    # 真 import 会成环。ExecutionContext 的定义归属 broker（它才是执行序的拥有者）
    from harness.broker import ExecutionContext

ExecutorProcess = Literal["api", "worker"]


# ---------------------------------------------------------------- 公共基类


class _ToolArgs(BaseModel):
    """所有工具入参的基类。

    ``frozen`` + ``extra="forbid"`` 是安全要求而非风格偏好：工具 args 会被写进证据账本、
    也会经 outbox 落库，多一个字段就多一处未审计的持久化面。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    @model_validator(mode="after")
    def _no_credentials(self) -> _ToolArgs:
        """值侧兜底。

        字段清单（``extra="forbid"``）已经堵死了 ``password`` 这类**键**，
        但堵不住 ``{"target": {"loc": "mysql://root:pw@h/db"}}``——键名完全合法。
        所以这里只跑**值侧**规则，不跑键名规则：
        ``secret_ref`` 是合法的引用字段名，键名规则会因为含 "secret" 把它误伤，
        那会让每一个带凭据引用的工具都被自己拦住。
        """
        hits = find_credential_values(self.model_dump(mode="json"))
        if hits:
            raise ValueError(f"工具入参禁止携带凭据形态的值：{', '.join(sorted(set(hits)))}")
        return self


class SecretRefArg(BaseModel):
    """Vault 引用（**只有定位信息，没有值**）。

    与 ``schemas.datasource.SecretRefView`` 分开定义是刻意的：这是「传给执行器让它自己去取」
    的输入，多一个字段都可能被误用成「顺带把值捎过去」的通道。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: Literal["vault"] = "vault"
    path: str = Field(min_length=1, max_length=255)
    version: int = Field(ge=1)
    key_names: tuple[str, ...] = ()


# ---------------------------------------------------------------- 六个工具的入参


class SeatunnelSubmitArgs(_ToolArgs):
    """提交一次 SeaTunnel 作业。``run_id`` 是幂等键（重复提交同一 run 返回同结果）。"""

    run_id: int = Field(ge=1)
    pipeline_version_id: int = Field(ge=1)
    hocon_artifact_id: int = Field(ge=1)
    # 提交作业要物化的密钥清单。⚠️ 这里保留调用方指定路径的能力，
    # 是因为 refs 来自已冻结的 preparation 制品；执行器**必须**校验它们与
    # preparation 行上记录的 refs 一致，否则就是同一类「读任意 Vault 路径」问题
    # （见 be-03 对 connectivity/probe 两个工具的处理：那里直接不给这个字段）。
    # 该校验归 be-07，本模块只把约束记在这里，不留给实现者自行判断。
    secret_refs: tuple[SecretRefArg, ...] = ()
    shadow_table: str = Field(min_length=1, max_length=128)
    error_table: str = Field(min_length=1, max_length=128)
    supervision_contract_id: int | None = Field(default=None, ge=1)


class SeatunnelStopArgs(_ToolArgs):
    run_id: int = Field(ge=1)
    job_id: str = Field(min_length=1, max_length=128)
    reason: Literal["supervision_interrupt", "manual_cancel", "pause"]


class TargetEnsureTablesArgs(_ToolArgs):
    """为目标端准备**本次运行要写入的两张表**：影子表与错误表。

    **为什么这件事必须是一个工具而不是服务里的一段 DDL**：它是目标库上的写操作
    （``CREATE TABLE``），而「任何改数据的动作必须经 ``ToolBroker``」是红线
    （CLAUDE.md §5.1）。少了这道登记，服务层就会为「只是建张空表」直接开一条驱动通路，
    而建表与改表在权限上没有区别——同一条通路之后就能写 ``DROP``。

    三张表名都**必须来自 ``execution_run`` 行**（执行器逐字比对），入参里给不出别的表名。
    错误表也在这里建：它是本次运行唯一的诊断材料存放处，缺了它，被拒的行没有落点，
    质量报告会以「拒绝 0 行」通过——一次有脏数据的运行看起来干干净净。
    """

    run_id: int = Field(ge=1)
    shadow_table: str = Field(min_length=1, max_length=128)
    formal_table: str = Field(min_length=1, max_length=128)
    error_table: str = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def _tables_differ(self) -> TargetEnsureTablesArgs:
        if self.shadow_table == self.formal_table:
            raise ValueError("shadow_table 与 formal_table 不能相同")
        if self.error_table in (self.shadow_table, self.formal_table):
            raise ValueError("error_table 不能与影子表或正式表同名")
        return self


class TargetPublishSwapArgs(_ToolArgs):
    """影子表 → 正式表的原子改名。**本系统风险最高的动作**（P0）。"""

    run_id: int = Field(ge=1)
    shadow_table: str = Field(min_length=1, max_length=128)
    formal_table: str = Field(min_length=1, max_length=128)
    quality_report_id: int = Field(ge=1)

    @model_validator(mode="after")
    def _tables_differ(self) -> TargetPublishSwapArgs:
        if self.shadow_table == self.formal_table:
            raise ValueError("shadow_table 与 formal_table 不能相同")
        return self


class TargetRollbackCleanupArgs(_ToolArgs):
    run_id: int = Field(ge=1)
    shadow_table: str = Field(min_length=1, max_length=128)
    formal_table: str = Field(min_length=1, max_length=128)
    rollback_plan_digest: str = Field(min_length=8, max_length=128)


class DatasourceConnectivityTestArgs(_ToolArgs):
    """只读探测。

    **入参里没有任何连接信息**——没有 ``target``，也没有 ``secret_ref``。
    连接串与 Vault 路径一律由执行器按 ``data_source_id`` 从库里取（带 tenant 过滤）。
    让调用方指定 Vault 路径，等于把一个 P3 的只读探测工具变成
    「读任意 Vault 路径」的原语；让调用方指定 target，则等于把探测变成 SSRF 探针。
    两个字段都不给，这类攻击面就不存在。
    """

    data_source_id: int = Field(ge=1)
    connect_timeout_sec: int = Field(default=5, ge=1, le=60)


class MetadataProbeArgs(_ToolArgs):
    """同上：连接信息只来自库，调用方只能说「探什么、探多少」。

    多出的 ``profile_id`` 是「往哪个档案里写」。探查结果**不走工具 detail**
    （白名单只放扁平的摘要键，这是有意的），而是由执行器直接落 ``metadata_profile``，
    所以它需要一个明确的目标行——而不是自己新建一行让调用方猜 id。
    """

    data_source_id: int = Field(ge=1)
    profile_id: int = Field(ge=1)
    scope: Literal["full_tables", "selected_tables", "schema_only"] = "schema_only"
    tables: tuple[str, ...] = ()
    sample_rows: int = Field(default=100, ge=0, le=1000)
    masking_policy: Mapping[str, Any] = Field(default_factory=dict)
    connect_timeout_sec: int = Field(default=5, ge=1, le=60)

    @model_validator(mode="after")
    def _selected_tables_required(self) -> MetadataProbeArgs:
        # 声明「只探查这几张表」却一张都没给，会静默退化成「什么都没探」，
        # 而调用方以为探查成功——宁可当场拒绝
        if self.scope == "selected_tables" and not self.tables:
            raise ValueError("scope=selected_tables 时必须给出 tables")
        return self


# ---------------------------------------------------------------- 注册表


@dataclass(frozen=True, slots=True)
class ToolSpec:
    tool: ToolName
    risk: RiskLevel
    requires_capability: bool
    executor: ExecutorProcess
    args_model: type[_ToolArgs]
    side_effect: str
    idempotent: bool
    requires_quality_pass: bool = False


_TOOL_SPECS: dict[ToolName, ToolSpec] = {}


def register_tool(spec: ToolSpec) -> None:
    if spec.tool in _TOOL_SPECS:
        raise RuntimeError(f"工具已注册，禁止覆盖：{spec.tool.value}")
    _TOOL_SPECS[spec.tool] = spec


def get_tool(tool: ToolName) -> ToolSpec:
    try:
        return _TOOL_SPECS[tool]
    except KeyError as exc:
        # 未注册的工具一律拒绝，而不是「找不到就当只读处理」
        raise UnauthorizedToolError(f"未注册的工具：{tool}") from exc


def all_tools() -> tuple[ToolSpec, ...]:
    """按 ``ToolName`` 定义序返回，保证账本与文档里的顺序可复现。"""
    return tuple(_TOOL_SPECS[t] for t in ToolName if t in _TOOL_SPECS)


_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        tool=ToolName.SEATUNNEL_SUBMIT,
        risk=RiskLevel.P1,
        requires_capability=True,
        executor="worker",
        args_model=SeatunnelSubmitArgs,
        side_effect="向 SeaTunnel Zeta 提交作业，开始真实数据搬运",
        idempotent=True,
    ),
    ToolSpec(
        tool=ToolName.SEATUNNEL_STOP,
        risk=RiskLevel.P2,
        requires_capability=True,
        executor="worker",
        args_model=SeatunnelStopArgs,
        side_effect="中止正在运行的 SeaTunnel 作业",
        idempotent=True,
    ),
    ToolSpec(
        tool=ToolName.TARGET_ENSURE_TABLES,
        # P2：它建的是**本次运行自己的**两张空表，不碰正式表的数据，也不删任何东西。
        # 比 ``target.rollback_cleanup``（P1，会 DROP）低一档，正是这个差别。
        risk=RiskLevel.P2,
        requires_capability=True,
        # **api 进程**：它在 Commit 事务提交之前跑（见 ``services/run.py::commit``），
        # 此时还没有任何作业在跑，失败要当场变成一次可读的 4xx/5xx，
        # 而不是一条几秒后才出现的运行失败。broker 允许 worker 执行 api 工具、
        # 不允许 api 执行 worker 工具，所以这个取值不构成「绕过审批提交作业」的通路。
        executor="api",
        args_model=TargetEnsureTablesArgs,
        side_effect="在目标库创建本次运行的影子表与错误表（不触碰正式表数据）",
        idempotent=True,
    ),
    ToolSpec(
        tool=ToolName.TARGET_PUBLISH_SWAP,
        risk=RiskLevel.P0,
        requires_capability=True,
        executor="worker",
        args_model=TargetPublishSwapArgs,
        side_effect="原子交换影子表与正式表，正式数据对外可见",
        idempotent=True,
        requires_quality_pass=True,
    ),
    ToolSpec(
        tool=ToolName.TARGET_ROLLBACK_CLEANUP,
        risk=RiskLevel.P1,
        requires_capability=True,
        executor="worker",
        args_model=TargetRollbackCleanupArgs,
        side_effect="回滚正式表并清理影子表，删除本次运行产生的数据",
        idempotent=True,
    ),
    ToolSpec(
        tool=ToolName.DATASOURCE_CONNECTIVITY_TEST,
        risk=RiskLevel.P3,
        requires_capability=False,
        executor="api",
        args_model=DatasourceConnectivityTestArgs,
        side_effect="向数据源发起一次只读连通性探测（出网）",
        idempotent=True,
    ),
    ToolSpec(
        tool=ToolName.METADATA_PROBE,
        risk=RiskLevel.P3,
        requires_capability=False,
        executor="api",
        args_model=MetadataProbeArgs,
        side_effect="只读探查源库元数据与少量样本（出网）",
        idempotent=True,
    ),
)

for _spec in _SPECS:
    register_tool(_spec)
del _spec


# ---------------------------------------------------------------- 执行器注入


ToolExecutor = Callable[[BaseModel, "ExecutionContext", AsyncSession], Awaitable[Mapping[str, Any]]]


_EXECUTORS: dict[ToolName, ToolExecutor] = {}


def register_executor(tool: ToolName, fn: ToolExecutor) -> None:
    """由 ``clients/`` 在 import 期注入真实实现。

    be-05 §1 定的是「按注册表调用**注入的** executor」——不让 ``harness/`` 在 import 期
    绑定 SeaTunnel / Doris 的具体驱动，否则安全内核会被拖进数据面的依赖里，
    也无法在单测里替换成假执行器。
    """
    if tool in _EXECUTORS:
        raise RuntimeError(f"执行器已注册，禁止覆盖：{tool.value}")
    if not inspect.iscoroutinefunction(fn):
        # 同步执行器会在事件循环里阻塞，而它做的全是网络 IO
        raise TypeError(f"执行器必须是 async def：{tool.value}")
    _EXECUTORS[tool] = fn


def get_executor(tool: ToolName) -> ToolExecutor:
    try:
        return _EXECUTORS[tool]
    except KeyError as exc:
        # 缺执行器时**不放行**：宁可拒绝，也不要出现「校验通过但什么都没做」的假成功
        raise UnauthorizedToolError(
            f"工具 {tool.value} 未注册执行器（第 ⑦ 步会失败）", code=ErrorCode.UNAUTHORIZED_TOOL
        ) from exc


def registered_executors() -> frozenset[ToolName]:
    return frozenset(_EXECUTORS)


def missing_executors() -> tuple[ToolName, ...]:
    """已注册但缺执行器的工具。启动自检用它把「配了工具却没人实现」暴露在启动期。"""
    return tuple(spec.tool for spec in all_tools() if spec.tool not in _EXECUTORS)


__all__ = [
    "DatasourceConnectivityTestArgs",
    "ExecutorProcess",
    "MetadataProbeArgs",
    "SeatunnelStopArgs",
    "SeatunnelSubmitArgs",
    "SecretRefArg",
    "TargetPublishSwapArgs",
    "TargetRollbackCleanupArgs",
    "ToolExecutor",
    "ToolSpec",
    "all_tools",
    "get_executor",
    "get_tool",
    "register_executor",
    "register_tool",
    "registered_executors",
]
