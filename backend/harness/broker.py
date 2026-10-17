"""``ToolBroker``：所有副作用的唯一出口（CLAUDE.md §5.1）。

七步执行序（be-05 §5.2），任何一步不通过都**先写证据再抛异常**——
账本与异常必须一致，否则会出现「拒绝了但查不到为什么」。

    ① 工具存在性        get_tool()                        → UNAUTHORIZED_TOOL
    ② 要求一致性        Capability / 进程                 → CAPABILITY_INVALID / UNAUTHORIZED_TOOL
    ③ 验签              verify_signature()                → CAPABILITY_INVALID / EXPIRED
    ④ 绑定比对          assert_binding()                  → TENANT_MISMATCH / FINGERPRINT_MISMATCH
    ⑤ 原子消费          ReplayGuard.consume()             → CAPABILITY_REPLAYED
    ⑥ 前置条件          preparation / quality_report      → PREPARATION_EXPIRED / POLICY_BLOCKED
    ⑦ args 校验与执行   args_model + executor             → VALIDATION_ERROR / 执行器错误

三条刻意的设计取舍：

- ``execute()`` **不 commit**：事务边界归调用方（be-13 / be-14），
  这样「消费标记 + 证据 + Outbox + 业务事实」才能落进同一个事务。
  中途崩溃时整笔回滚、Capability 不被消耗——宁可让调用方重试，
  也不要出现「令牌已烧掉但作业没提交」的死局。
- **所有协议拒绝都走 :meth:`_reject`**：它写 ``CAPABILITY_REJECTED``、
  按 §7 矩阵标 ``reject_reason``，然后抛。写账本失败只记日志、不吞掉原异常：
  拒绝已经生效（工具没执行），此时抛 DB 错误只会掩盖真正的原因。
- **args 校验失败不写账本**：参数问题是调用方笔误，不是协议拒绝；
  写进去会让不可变账本被噪声淹没（§7 第 16 条）。

免签工具（``requires_capability=False``）成功后**不写协议事件**：
「这次连通性探测成功了」属于业务领域词汇（``DATASOURCE_CONNECTIVITY_TESTED``），
该由拥有该词汇的 service 写，安全内核不替它决定事件类型。
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, NoReturn

from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from core.errors import (
    AppError,
    ErrorCode,
    NotFoundError,
    PolicyBlockedError,
    TenantMismatchError,
    ValidationError,
)
from core.logging import get_logger
from harness.capability import (
    CapabilityClaims,
    CapabilityIssuer,
    CapabilityVerifier,
    ExpectedBinding,
)
from harness.errors import (
    CapabilityRequiredError,
    PreparationNotReadyError,
    QualityGateError,
    RejectStage,
    UnauthorizedToolError,
    reject_payload,
    reject_reason_of,
    reject_stage_of,
)
from harness.ledger import EvidenceLedger, LedgerEvent
from harness.replay import ConsumptionResult, ReplayGuard
from harness.tools import ToolSpec, get_executor, get_tool
from models.enums import EvidenceEventType, PreparationStatus, RunStatus, ToolName
from models.execution import ExecutionRun, QualityReport
from models.governance import Capability, Preparation

log = get_logger(__name__)

# 这些运行状态下一律禁止发布类工具：质量未通过只是「数据不好」，
# 而 interrupted/failed 意味着这次运行本身已被判定为不可信。
_DEAD_RUN_STATES = frozenset({RunStatus.INTERRUPTED, RunStatus.FAILED, RunStatus.CANCELLED})

# detail 里允许出现的键。执行器只该回「引用与计数」，
# 出现别的键说明它把原始行或连接信息塞回来了。
_ALLOWED_DETAIL_KEYS = frozenset(
    {
        "status",
        # 探测类工具的**结论**键。少了它的后果不是报错而是「结论被改写」：
        # 键不在表里会被静默丢弃（只留一行 tool_detail_keys_dropped 警告），
        # 下游 `detail.get("state", "failed")` 于是回落到默认值——MySQL 明明连上了、
        # latency 与 server_version 都拿到了，前端却永远显示 failed。
        # 这类「白名单少一个词」的 bug 不会在单测里出现（单测直接调执行器，不经过 broker）。
        "state",
        "ok",
        "latency_ms",
        "server_version",
        "target",
        "affected_rows",
        "shadow_table",
        "formal_table",
        "job_id",
        "row_count",
        "profile_id",
        "version",
        "tables_count",
        "error_code",
        "message",
        "interrupted",
    }
)


@dataclass(frozen=True, slots=True)
class ExecutionContext:
    request_id: str
    tenant_id: int
    project_id: int
    actor_id: int | None
    caller: Literal["api", "worker", "beat"]
    run_id: int | None = None


@dataclass(frozen=True, slots=True)
class ToolResult:
    tool: ToolName
    ok: bool
    jti: uuid.UUID | None
    detail: dict[str, Any] = field(default_factory=dict)
    evidence_seq: int | None = None


class ToolBroker:
    def __init__(
        self,
        *,
        guard: ReplayGuard,
        ledger: EvidenceLedger,
        verifier: CapabilityVerifier | None = None,
        issuer: CapabilityIssuer | None = None,
    ) -> None:
        # issuer 只有 api 进程（be-13 签发侧）需要；worker 侧刻意不持有，
        # 这样即使 worker 被攻陷也签不出新令牌
        self._issuer = issuer
        self._verifier = verifier
        self._guard = guard
        self._ledger = ledger

    @property
    def issuer(self) -> CapabilityIssuer:
        if self._issuer is None:
            raise AppError("本进程不持有 Capability 签发密钥", code=ErrorCode.INTERNAL_ERROR)
        return self._issuer

    # ---------------------------------------------------------------- 主流程

    async def execute(
        self,
        tool: ToolName,
        token: str | None,
        args: Mapping[str, Any] | BaseModel,
        ctx: ExecutionContext,
        *,
        session: AsyncSession,
        now: datetime | None = None,
    ) -> ToolResult:
        # 时间只取一次：验签、过期判定、账本时间戳共用同一个「现在」，
        # 否则跨秒执行时会出现「验签说过期了、账本说还没」这种自相矛盾的证据
        now = (now or datetime.now(UTC)).astimezone(UTC)

        spec = await self._require_tool(tool, session, ctx)  # ①
        await self._check_requirements(spec, token, session, ctx)  # ②

        claims: CapabilityClaims | None = None
        if spec.requires_capability:
            claims = await self._verify(spec, token or "", session, ctx, now)  # ③
            # ④a 先比上下文：claim 与 ctx 都在手上，这一步**不需要查库**。
            # 放到 _expected_binding 之后的话，跨租户调用会先撞上「preparation 不存在」，
            # 拿到 NOT_FOUND 而不是 TENANT_MISMATCH —— 先做便宜且能拒绝的检查
            await self._assert_ctx(claims, spec, session, ctx)
            expected = await self._expected_binding(spec, claims, session, ctx)  # ④b
            await self._assert_binding(claims, expected, spec, session, ctx)

            # ⑤ 原子消费
            result = await self._guard.consume(claims, session, run_id=ctx.run_id)
            if result is ConsumptionResult.REPLAYED:
                await self._reject(
                    AppError("Capability 已被消费", code=ErrorCode.CAPABILITY_REPLAYED),
                    spec=spec,
                    ctx=ctx,
                    session=session,
                    stage=RejectStage.REPLAY,
                    jti=str(claims.jti),
                )

            # ⑥ 前置条件：preparation 状态/有效期 + 质量契约
            await self._check_preconditions(spec, claims.preparation_id, session, ctx, now)
        else:
            await self._check_quality(spec, session, ctx)

        # ⑦ args 校验与执行
        parsed = self._parse_args(spec, args)
        detail = await self._run_executor(spec, parsed, session, ctx)

        evidence_seq: int | None = None
        if claims is not None:
            event = await self._ledger.append(
                session,
                LedgerEvent(
                    tenant_id=ctx.tenant_id,
                    project_id=ctx.project_id,
                    event_type=EvidenceEventType.CAPABILITY_CONSUMED,
                    actor_id=ctx.actor_id,
                    subject_type="capability",
                    subject_id=str(claims.jti),
                    payload={
                        "tool": spec.tool.value,
                        "executor": spec.executor,
                        "preparation_id": claims.preparation_id,
                        "run_id": ctx.run_id,
                        "request_id": ctx.request_id,
                        "detail_keys": sorted(detail),
                    },
                ),
            )
            evidence_seq = event.seq

        return ToolResult(
            tool=spec.tool,
            ok=True,
            jti=claims.jti if claims is not None else None,
            detail=detail,
            evidence_seq=evidence_seq,
        )

    # ---------------------------------------------------------------- ①②

    async def _require_tool(self, tool: ToolName, session: AsyncSession, ctx: ExecutionContext) -> ToolSpec:
        # tool_name 用 getattr 兜住「传了裸字符串」的调用：类型标注说必须是 ToolName，
        # 但账本与拒绝路径**不能建立在调用方守规矩的前提上**——
        # 传错类型应该得到一个 UNAUTHORIZED_TOOL，而不是 AttributeError
        tool_name = getattr(tool, "value", None) or str(tool)
        try:
            return get_tool(tool)
        except UnauthorizedToolError as exc:
            await self._reject(exc, spec=None, ctx=ctx, session=session, tool_name=tool_name)

    async def _check_requirements(
        self, spec: ToolSpec, token: str | None, session: AsyncSession, ctx: ExecutionContext
    ) -> None:
        # 「工具声明在哪个进程执行」是一条**单向**约束，不是双向相等：
        #
        # * **api 进程不得执行 worker 工具** —— 那是绕过审批直接提交作业的最短路径，
        #   这条必须堵死，也是本检查存在的**唯一**理由；
        # * worker 执行声明为 api 的只读工具**不放行也不该拒**：worker 本来就持有
        #   作业提交权，让它多跑一次只读探测不增加任何攻击面，拒掉反而迫使调用方
        #   伪造 ``caller="api"``——那等于让这道检查建立在谎言上，比放宽更糟。
        #
        # P3 的连通性测试与元数据探查声明为 api 工具（语义是「不需要数据面特权」），
        # 实现上却由 Celery 跑（前端契约就是 202 + 轮询，不能在请求里等满超时），
        # 所以本进程允许 worker→api 这一方向是必需的，不是顺手放宽。
        allowed = spec.executor == ctx.caller or (
            ctx.caller == "worker" and spec.executor == "api"
        )
        if not allowed:
            await self._reject(
                UnauthorizedToolError(
                    f"工具 {spec.tool.value} 只允许在 {spec.executor} 进程执行，当前为 {ctx.caller}"
                ),
                spec=spec,
                ctx=ctx,
                session=session,
                stage=RejectStage.REQUIREMENT,
            )
        if spec.requires_capability and token is None:
            await self._reject(
                CapabilityRequiredError("该工具必须携带 Capability"),
                spec=spec,
                ctx=ctx,
                session=session,
                stage=RejectStage.REQUIREMENT,
            )
        if not spec.requires_capability and token is not None:
            # 「多带一个 token 更安全」是一种会绕开免签路径的误解，必须同样被拒
            await self._reject(
                CapabilityRequiredError("该工具免 Capability，不应携带令牌"),
                spec=spec,
                ctx=ctx,
                session=session,
                stage=RejectStage.REQUIREMENT,
            )

    async def _verify(
        self, spec: ToolSpec, token: str, session: AsyncSession, ctx: ExecutionContext, now: datetime
    ) -> CapabilityClaims:
        if self._verifier is None:
            await self._reject(
                AppError("本进程未配置 Capability 校验公钥", code=ErrorCode.INTERNAL_ERROR),
                spec=spec,
                ctx=ctx,
                session=session,
                stage=RejectStage.SIGNATURE,
            )
        try:
            return self._verifier.verify_signature(token, now=now)
        except AppError as exc:
            # 此处通常拿不到可信的 jti（令牌本身可能非法），reject_reason 无法落库
            await self._reject(exc, spec=spec, ctx=ctx, session=session, stage=RejectStage.SIGNATURE)

    # ---------------------------------------------------------------- ④

    async def _expected_binding(
        self, spec: ToolSpec, claims: CapabilityClaims, session: AsyncSession, ctx: ExecutionContext
    ) -> ExpectedBinding:
        """期望值**从 preparation 行重算**，不信调用方传参。

        少了这一步，「比对」就退化成「拿调用方说的值去比调用方给的令牌」。
        """
        preparation = await self._load_preparation(claims.preparation_id, session, ctx)
        if ctx.actor_id is None:
            # 令牌必须绑定到一个具体的人：worker 内部任务也要带上发起人，
            # 否则「谁执行的」在账本上会是空的
            await self._reject(
                UnauthorizedToolError("Capability 执行缺少操作主体"),
                spec=spec,
                ctx=ctx,
                session=session,
                stage=RejectStage.BINDING,
            )
        return ExpectedBinding(
            # 比的是**执行主体**（ctx.actor_id），不是 preparation.created_by：
            # created_by 是申请人，而令牌是签给提交执行的运维的
            subject_id=ctx.actor_id,
            tool=spec.tool,
            env=preparation.environment,
            artifact_sha256=preparation.artifact_sha256,
            tenant_id=preparation.tenant_id,
            project_id=preparation.project_id,
        )

    async def _assert_ctx(
        self, claims: CapabilityClaims, spec: ToolSpec, session: AsyncSession, ctx: ExecutionContext
    ) -> None:
        """ctx 的租户/项目必须与令牌一致，否则是跨租户调用。"""
        if claims.tenant_id != ctx.tenant_id or claims.project_id != ctx.project_id:
            await self._reject(
                TenantMismatchError("Capability 与当前执行上下文不属于同一租户/项目"),
                spec=spec,
                ctx=ctx,
                session=session,
                stage=RejectStage.BINDING,
                jti=str(claims.jti),
            )

    async def _assert_binding(
        self,
        claims: CapabilityClaims,
        expected: ExpectedBinding,
        spec: ToolSpec,
        session: AsyncSession,
        ctx: ExecutionContext,
    ) -> None:
        if self._verifier is None:  # pragma: no cover - _verify 已拦
            return
        try:
            self._verifier.assert_binding(claims, expected)
        except AppError as exc:
            await self._reject(
                exc,
                spec=spec,
                ctx=ctx,
                session=session,
                stage=RejectStage.BINDING,
                jti=str(claims.jti),
            )

    # ---------------------------------------------------------------- ⑥

    async def _check_preconditions(
        self,
        spec: ToolSpec,
        preparation_id: int,
        session: AsyncSession,
        ctx: ExecutionContext,
        now: datetime,
    ) -> None:
        preparation = await self._load_preparation(preparation_id, session, ctx)
        if preparation.status is not PreparationStatus.APPROVED:
            await self._reject(
                PreparationNotReadyError(f"preparation 状态为 {preparation.status.value}"),
                spec=spec,
                ctx=ctx,
                session=session,
                stage=RejectStage.REQUIREMENT,
            )
        if preparation.expires_at is not None and now >= _as_utc(preparation.expires_at):
            await self._reject(
                PreparationNotReadyError("preparation 已过期"),
                spec=spec,
                ctx=ctx,
                session=session,
                stage=RejectStage.REQUIREMENT,
            )
        await self._check_quality(spec, session, ctx)

    async def _check_quality(self, spec: ToolSpec, session: AsyncSession, ctx: ExecutionContext) -> None:
        if not spec.requires_quality_pass:
            return
        if ctx.run_id is None:
            await self._reject(
                QualityGateError("该工具必须绑定一次运行"),
                spec=spec,
                ctx=ctx,
                session=session,
                stage=RejectStage.REQUIREMENT,
            )
        run = (
            await session.execute(
                select(ExecutionRun).where(
                    ExecutionRun.id == ctx.run_id, ExecutionRun.tenant_id == ctx.tenant_id
                )
            )
        ).scalar_one_or_none()
        if run is None:
            await self._reject(
                NotFoundError("运行记录不存在"),
                spec=spec,
                ctx=ctx,
                session=session,
                stage=RejectStage.REQUIREMENT,
            )
        if run.status in _DEAD_RUN_STATES:
            await self._reject(
                PolicyBlockedError(f"运行状态为 {run.status.value}，禁止发布"),
                spec=spec,
                ctx=ctx,
                session=session,
                stage=RejectStage.REQUIREMENT,
            )
        # ``quality_report`` 上没有 tenant_id 列（它的唯一键就是 run_id），
        # 所以租户过滤靠 join 回 execution_run 表达——而不是「反正 run 已经查过了」。
        # 少一个显式条件，下一个人复制这段查询时就可能忘掉租户边界。
        report = (
            await session.execute(
                select(QualityReport)
                .join(ExecutionRun, ExecutionRun.id == QualityReport.run_id)
                .where(QualityReport.run_id == ctx.run_id, ExecutionRun.tenant_id == ctx.tenant_id)
            )
        ).scalar_one_or_none()
        if report is None or not report.passed:
            # P0 动作（影子表换正式表）在质量未过时放行 = 把脏数据直接推给业务方
            await self._reject(
                QualityGateError("质量契约未通过，禁止发布"),
                spec=spec,
                ctx=ctx,
                session=session,
                stage=RejectStage.REQUIREMENT,
            )

    # ---------------------------------------------------------------- ⑦

    def _parse_args(self, spec: ToolSpec, args: Mapping[str, Any] | BaseModel) -> BaseModel:
        try:
            if isinstance(args, BaseModel):
                # 允许传已构造的模型，但仍过一遍目标模型：
                # 传错模型类型（例如把 SubmitArgs 传给 swap）必须当场发现
                return spec.args_model.model_validate(args.model_dump())
            return spec.args_model.model_validate(dict(args))
        except PydanticValidationError as exc:
            # 只回字段路径与原因，不回显原始值（可能夹带疑似凭据）
            fields = [
                {"loc": ".".join(str(p) for p in err.get("loc", ())), "reason": err.get("msg", "")}
                for err in exc.errors()
            ]
            raise ValidationError("工具入参非法", details={"fields": fields}) from exc

    async def _run_executor(
        self, spec: ToolSpec, parsed: BaseModel, session: AsyncSession, ctx: ExecutionContext
    ) -> dict[str, Any]:
        try:
            executor = get_executor(spec.tool)
        except AppError as exc:
            await self._reject(exc, spec=spec, ctx=ctx, session=session, stage=RejectStage.EXECUTOR)
        try:
            out = await executor(parsed, ctx, session)
        except AppError as exc:
            await self._reject(exc, spec=spec, ctx=ctx, session=session, stage=RejectStage.EXECUTOR)
        except Exception as exc:  # 第三方驱动的异常形态不可穷举
            # 执行器抛的第三方异常（驱动、网络）**不原样上抛**：
            # 那些异常消息里常带连接串与主机名（CLAUDE.md §6 禁止把库异常抛给前端）
            log.error(
                "tool_executor_failed",
                tool=spec.tool.value,
                error_type=type(exc).__name__,
                request_id=ctx.request_id,
            )
            await self._reject(
                AppError("工具执行失败", code=ErrorCode.INTERNAL_ERROR),
                spec=spec,
                ctx=ctx,
                session=session,
                stage=RejectStage.EXECUTOR,
            )
        return _safe_detail(out)

    # ---------------------------------------------------------------- 拒绝出口

    async def _reject(
        self,
        exc: AppError,
        *,
        spec: ToolSpec | None,
        ctx: ExecutionContext,
        session: AsyncSession,
        stage: RejectStage | None = None,
        jti: str | None = None,
        tool_name: str | None = None,
    ) -> NoReturn:
        """协议类拒绝的唯一出口：写 ``CAPABILITY_REJECTED`` + 标 ``reject_reason``，再抛。

        返回类型是 ``NoReturn``，所以调用点不必在 ``await self._reject(...)`` 之后
        再补 ``return``——漏写 return 而继续往下执行是这类代码最危险的失误。
        """
        used_stage = stage or reject_stage_of(exc)
        tool_value = spec.tool.value if spec is not None else (tool_name or "unknown")
        try:
            await self._ledger.append(
                session,
                LedgerEvent(
                    tenant_id=ctx.tenant_id,
                    project_id=ctx.project_id,
                    event_type=EvidenceEventType.CAPABILITY_REJECTED,
                    actor_id=ctx.actor_id,
                    subject_type="capability" if jti else "tool",
                    subject_id=jti or tool_value,
                    payload=reject_payload(stage=used_stage, code=exc.code, tool=tool_value, jti=jti),
                ),
            )
            if jti is not None:
                reason = reject_reason_of(exc.code)
                if reason is not None:
                    await session.execute(
                        update(Capability)
                        .where(Capability.jti == uuid.UUID(jti))
                        .values(reject_reason=reason)
                    )
        except Exception:  # 落账失败不改判定，只记录
            log.error(
                "reject_evidence_failed",
                tool=tool_value,
                code=exc.code.value,
                request_id=ctx.request_id,
            )
        raise exc

    # ---------------------------------------------------------------- 载入

    @staticmethod
    async def _load_preparation(
        preparation_id: int, session: AsyncSession, ctx: ExecutionContext
    ) -> Preparation:
        row = (
            await session.execute(
                select(Preparation).where(
                    Preparation.id == preparation_id,
                    # tenant_id 过滤不是可选项（CLAUDE.md §4.7）：
                    # 少了它，一个跨租户的 preparation_id 就能读到别人的审批单
                    Preparation.tenant_id == ctx.tenant_id,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise NotFoundError("preparation 不存在或不属于当前租户")
        return row


def _safe_detail(detail: Mapping[str, Any]) -> dict[str, Any]:
    """裁剪执行器返回的 detail。

    白名单而非黑名单：detail 会进账本（**不可变、不可删**），
    一次泄漏就是永久泄漏。未知键直接丢弃并告警，而不是「看起来不敏感就放过」。
    嵌套结构也丢弃——那是「不小心把整行数据带出来」的典型形态。
    """
    out: dict[str, Any] = {}
    dropped: list[str] = []
    for key, value in detail.items():
        if key not in _ALLOWED_DETAIL_KEYS or isinstance(value, (Mapping, list, tuple, set)):
            dropped.append(key)
            continue
        out[key] = value
    if dropped:
        log.warning("tool_detail_keys_dropped", dropped_keys=sorted(dropped))
    return out


def _as_utc(value: datetime) -> datetime:
    """SQLite 不保留时区，读回来是裸 datetime；PG 上这是恒等变换。"""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
