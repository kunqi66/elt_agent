"""统一错误码与异常处理。

规则（CLAUDE.md §6）：
- 错误码是**对外契约**，前端按 code 分支，不用 message 做判断。新增码只能追加，不得改动语义。
- 数据库异常、第三方异常**不得**原样透出（可能含连接串/表结构）；一律映射为内部错误 + 请求 ID。
- 面向用户的关键拒绝（自批、重放、P0 阻断）必须可解释：给出 ``code`` + 明确的 ``message``，
  并由调用方额外写证据账本事件。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from core.config import settings
from core.ids import new_request_id
from core.logging import get_logger

log = get_logger(__name__)


class ErrorCode(StrEnum):
    # --- 通用 ---
    OK = "OK"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    NOT_FOUND = "NOT_FOUND"
    CONFLICT = "CONFLICT"
    RATE_LIMITED = "RATE_LIMITED"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"

    # --- 认证与权限 ---
    UNAUTHENTICATED = "UNAUTHENTICATED"
    TOKEN_EXPIRED = "TOKEN_EXPIRED"  # noqa: S105 - 错误码字面量，非口令
    BAD_CREDENTIALS = "BAD_CREDENTIALS"
    ACCOUNT_LOCKED = "ACCOUNT_LOCKED"
    FORBIDDEN = "FORBIDDEN"
    TENANT_MISMATCH = "TENANT_MISMATCH"
    ROLE_REQUIRED = "ROLE_REQUIRED"

    # --- 数据源与元数据 ---
    DATASOURCE_UNREACHABLE = "DATASOURCE_UNREACHABLE"
    DATASOURCE_AUTH_FAILED = "DATASOURCE_AUTH_FAILED"
    DATASOURCE_TIMEOUT = "DATASOURCE_TIMEOUT"
    METADATA_PROFILE_EXPIRED = "METADATA_PROFILE_EXPIRED"

    # --- 生成与门禁 ---
    LLM_UNAVAILABLE = "LLM_UNAVAILABLE"
    PLAN_INVALID = "PLAN_INVALID"
    HOCON_SYNTAX_ERROR = "HOCON_SYNTAX_ERROR"
    SCHEMA_MISMATCH = "SCHEMA_MISMATCH"
    GATE_BLOCKED = "GATE_BLOCKED"
    AUTOFIX_EXHAUSTED = "AUTOFIX_EXHAUSTED"
    VERSION_IMMUTABLE = "VERSION_IMMUTABLE"

    # --- 审批与执行协议 ---
    SELF_APPROVAL_FORBIDDEN = "SELF_APPROVAL_FORBIDDEN"  # 申请人自批
    DUTY_CONFLICT = "DUTY_CONFLICT"  # 职责混用
    APPROVAL_INCOMPLETE = "APPROVAL_INCOMPLETE"
    FINGERPRINT_MISMATCH = "FINGERPRINT_MISMATCH"  # 审批后制品/输入指纹变化
    PREPARATION_EXPIRED = "PREPARATION_EXPIRED"
    CAPABILITY_INVALID = "CAPABILITY_INVALID"
    CAPABILITY_EXPIRED = "CAPABILITY_EXPIRED"
    CAPABILITY_REPLAYED = "CAPABILITY_REPLAYED"
    UNAUTHORIZED_TOOL = "UNAUTHORIZED_TOOL"

    # --- 风险与执行 ---
    POLICY_BLOCKED = "POLICY_BLOCKED"  # P0/P1 阻断
    SUPERVISION_INTERRUPTED = "SUPERVISION_INTERRUPTED"  # 超预算硬中断
    QUALITY_REJECTED = "QUALITY_REJECTED"
    RUN_CONFLICT = "RUN_CONFLICT"  # 同一版本已有活跃运行
    SWAP_FAILED = "SWAP_FAILED"
    ROLLBACK_FAILED = "ROLLBACK_FAILED"


class AppError(Exception):
    """业务异常基类。子类只需给出默认 code / http_status。"""

    code: ErrorCode = ErrorCode.INTERNAL_ERROR
    http_status: int = status.HTTP_400_BAD_REQUEST
    message: str = "请求处理失败"

    def __init__(
        self,
        message: str | None = None,
        *,
        code: ErrorCode | None = None,
        http_status: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.message = message or self.message
        self.code = code or self.code
        self.http_status = http_status or self.http_status
        self.details = details or {}
        super().__init__(self.message)


# ---------------- 通用 ----------------
class ValidationError(AppError):
    code = ErrorCode.VALIDATION_ERROR
    http_status = status.HTTP_422_UNPROCESSABLE_CONTENT
    message = "参数校验失败"


class NotFoundError(AppError):
    code = ErrorCode.NOT_FOUND
    http_status = status.HTTP_404_NOT_FOUND
    message = "资源不存在"


class ConflictError(AppError):
    code = ErrorCode.CONFLICT
    http_status = status.HTTP_409_CONFLICT
    message = "资源状态冲突"


class RateLimitedError(AppError):
    code = ErrorCode.RATE_LIMITED
    http_status = status.HTTP_429_TOO_MANY_REQUESTS
    message = "请求过于频繁"


# ---------------- 认证与权限 ----------------
class UnauthenticatedError(AppError):
    code = ErrorCode.UNAUTHENTICATED
    http_status = status.HTTP_401_UNAUTHORIZED
    message = "未认证或凭据无效"


class TokenExpiredError(AppError):
    code = ErrorCode.TOKEN_EXPIRED
    http_status = status.HTTP_401_UNAUTHORIZED
    message = "登录状态已过期"


class BadCredentialsError(AppError):
    code = ErrorCode.BAD_CREDENTIALS
    http_status = status.HTTP_401_UNAUTHORIZED
    message = "用户名或密码错误"


class AccountLockedError(AppError):
    code = ErrorCode.ACCOUNT_LOCKED
    http_status = status.HTTP_423_LOCKED
    message = "账号已被临时锁定，请稍后重试"


class ForbiddenError(AppError):
    code = ErrorCode.FORBIDDEN
    http_status = status.HTTP_403_FORBIDDEN
    message = "无权执行该操作"


class TenantMismatchError(AppError):
    code = ErrorCode.TENANT_MISMATCH
    http_status = status.HTTP_403_FORBIDDEN
    message = "跨租户访问被拒绝"


class RoleRequiredError(AppError):
    """主体在当前项目上下文中不持有任一所需职责槽。

    与 ``ForbiddenError`` 分开：``FORBIDDEN`` 是「非成员 / 不该看到这个资源」，
    本码是「是成员但职责不够」——前者不该给出资源存在性的信息，后者可以明说差什么角色。
    """

    code = ErrorCode.ROLE_REQUIRED
    http_status = status.HTTP_403_FORBIDDEN
    message = "当前职责槽无权执行该操作"


# ---------------- 协议类（须写证据账本）----------------
class SelfApprovalForbiddenError(AppError):
    code = ErrorCode.SELF_APPROVAL_FORBIDDEN
    http_status = status.HTTP_403_FORBIDDEN
    message = "申请人不得审批自己提交的准备单"


class DutyConflictError(AppError):
    code = ErrorCode.DUTY_CONFLICT
    http_status = status.HTTP_403_FORBIDDEN
    message = "高风险操作禁止同一人兼任多个审批职责"


class FingerprintMismatchError(AppError):
    code = ErrorCode.FINGERPRINT_MISMATCH
    http_status = status.HTTP_409_CONFLICT
    message = "制品或输入指纹已变化，原审批失效，请重新准备与审批"


class CapabilityInvalidError(AppError):
    code = ErrorCode.CAPABILITY_INVALID
    http_status = status.HTTP_403_FORBIDDEN
    message = "执行能力令牌无效"


class CapabilityExpiredError(AppError):
    code = ErrorCode.CAPABILITY_EXPIRED
    http_status = status.HTTP_403_FORBIDDEN
    message = "执行能力令牌已过期"


class CapabilityReplayedError(AppError):
    code = ErrorCode.CAPABILITY_REPLAYED
    http_status = status.HTTP_409_CONFLICT
    message = "执行能力令牌已被消费（疑似重放）"


class PolicyBlockedError(AppError):
    code = ErrorCode.POLICY_BLOCKED
    http_status = status.HTTP_403_FORBIDDEN
    message = "策略判定为高风险，已阻断"


# ---------------- 准备单与审批（be-13）----------------
class MetadataProfileExpiredError(AppError):
    """源端元数据档案过期或已被重新探查。

    与 ``FINGERPRINT_MISMATCH`` 分开：那条是「输入变了但变在哪不知道」，
    这条是「知道变在哪，去重新探查一次就好」。合成一个码会让前端只能给出
    「指纹不一致」这种既不可执行也不准确的提示。
    """

    code = ErrorCode.METADATA_PROFILE_EXPIRED
    http_status = status.HTTP_409_CONFLICT
    message = "元数据档案已过期或已被重新探查，请重新探查后再提交"


class ApprovalIncompleteError(AppError):
    """审批未齐备（或准备单不在可提交状态）。

    刻意**不叫**「未审批」：它同时覆盖「必需槽还差一个」与「状态不是 approved」，
    两者对调用方的动作是同一个——回去把审批走完，而不是去翻状态机。
    """

    code = ErrorCode.APPROVAL_INCOMPLETE
    http_status = status.HTTP_409_CONFLICT
    message = "审批未齐备，不能提交执行"


class PreparationExpiredError(AppError):
    """审批有效期已过（``preparation.expires_at`` 是硬边界）。

    过期**不做续期**：续期等于让一份「当时被批准的事实」在无人复核的情况下继续有效，
    与四眼的意图相反。要执行就重新准备。
    """

    code = ErrorCode.PREPARATION_EXPIRED
    http_status = status.HTTP_409_CONFLICT
    message = "准备单已过期，请重新发起准备与审批"


class SupervisionInterruptedError(AppError):
    code = ErrorCode.SUPERVISION_INTERRUPTED
    http_status = status.HTTP_409_CONFLICT
    message = "运行时监督超限，任务已被硬中断"


class RunConflictError(AppError):
    """同一 ``pipeline_version`` 已有活跃运行，或运行状态转移非法（be-14 §7）。

    两种触发共用一个码，是因为它们的处置完全相同：**不覆盖、不排队，让调用方
    先看清当前状态**。唯一的区别是前者还在数据库层由部分唯一索引兜底
    （``uq_execution_run_active_per_version``）——两处都要拦，服务层的判断
    只是为了让绝大多数请求在写库前就得到一个可读的 409，
    而不是一条 ``IntegrityError``。
    """

    code = ErrorCode.RUN_CONFLICT
    http_status = status.HTTP_409_CONFLICT
    message = "同一版本已有正在进行的运行"


# ---------------- 异常处理器 ----------------
def _payload(
    code: str, message: str, request_id: str, details: dict[str, Any] | None = None
) -> dict[str, Any]:
    body: dict[str, Any] = {"code": code, "message": message, "request_id": request_id}
    if details:
        body["details"] = details
    return body


def _db_error_fields(exc: SQLAlchemyError) -> dict[str, Any]:
    """从驱动异常里抠出**结构性**字段：SQLSTATE、约束名、表名、列名。

    asyncpg 把这些放在 ``exc.orig`` 上（``PostgresError``）；换驱动时抠不到就返回空字典，
    调用方照样能按 ``error_type`` 判断——**不为了这几个字段去 import 具体驱动**。
    """
    orig = getattr(exc, "orig", None)
    fields: dict[str, Any] = {}
    for name in ("sqlstate", "constraint_name", "table_name", "column_name"):
        value = getattr(orig, name, None)
        if value:
            fields[name] = str(value)[:128]
    return fields


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError) -> JSONResponse:
        rid = new_request_id()
        # 协议类拒绝记 warning（要能追溯到人），业务参数类记 info
        log_fn = log.warning if exc.http_status in (401, 403, 409, 423) else log.info
        log_fn(
            "app_error",
            code=exc.code.value,
            message=exc.message,
            http_status=exc.http_status,
            request_id=rid,
        )
        return JSONResponse(
            status_code=exc.http_status,
            content=_payload(exc.code.value, exc.message, rid, exc.details),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        rid = new_request_id()
        # 只回传字段路径与原因，不回显原始输入值（可能含敏感数据）
        details = [
            {"loc": ".".join(str(p) for p in err.get("loc", [])), "reason": err.get("msg", "")}
            for err in exc.errors()
        ]
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content=_payload(ErrorCode.VALIDATION_ERROR.value, "参数校验失败", rid, {"fields": details}),
        )

    @app.exception_handler(PydanticValidationError)
    async def _service_validation_error(_: Request, exc: PydanticValidationError) -> JSONResponse:
        """**服务层**构造 DTO 失败时也回 422，而不是 500。

        `RequestValidationError` 只覆盖「请求解析阶段」的校验。服务层把查询参数
        组装成 Pydantic 模型（如 `EvidenceFilter(occurred_from=…)`）时若失败，
        抛的是 pydantic 自己的 `ValidationError`——它没有处理器，会落到兜底的
        `Exception` 处理器变成 500，于是「参数写错了」在前端显示为「服务器错误」。

        语义上它就是一次参数校验失败，所以映射到同一组码。
        Starlette 按 MRO 查找处理器，`RequestValidationError` 是它的子类，
        因此请求解析期的失败仍优先命中上面那个更具体的处理器。
        """
        rid = new_request_id()
        # 和上面那个处理器一样**只回字段路径与原因，不回显原始输入值**。
        # 早先这里只记了 ``error_count``，症状是「图上一切正常，最后一步回 422
        # 参数校验失败，日志里只有一个数字」——排查时完全无法定位是哪个字段，
        # 只能停下来改代码加日志。数量从不是有用的信息，路径才是。
        fields = [
            {"loc": ".".join(str(p) for p in err.get("loc", [])), "reason": err.get("msg", "")}
            for err in exc.errors()
        ]
        log.info(
            "service_validation_error",
            request_id=rid,
            error_count=exc.error_count(),
            error_paths=[item["loc"] for item in fields],
        )
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content=_payload(
                ErrorCode.VALIDATION_ERROR.value,
                "参数校验失败",
                rid,
                {"errors": exc.error_count(), "fields": fields},
            ),
        )

    @app.exception_handler(IntegrityError)
    async def _integrity_error(_: Request, exc: IntegrityError) -> JSONResponse:
        rid = new_request_id()
        # 唯一约束命中：常见于 Capability 重复消费、幂等键冲突、同版本并发运行
        log.warning("db_integrity_error", request_id=rid, error_type=type(exc).__name__)
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content=_payload(ErrorCode.CONFLICT.value, "数据唯一性冲突，请刷新后重试", rid),
        )

    @app.exception_handler(SQLAlchemyError)
    async def _db_error(_: Request, exc: SQLAlchemyError) -> JSONResponse:
        rid = new_request_id()
        # 明细只在服务端日志。**只记结构字段**（SQLSTATE / 约束名 / 表列名），
        # 不记 ``str(exc)``：驱动的报错串里常常带着 ``Key (col)=(value)`` 这样的实参，
        # 那是数据、可能是秘密（CLAUDE.md §5.2 无明文 Secret）。
        # 光记 ``error_type=DBAPIError`` 等于没记——「哪个约束炸了」才是唯一有用的信息。
        detail = _db_error_fields(exc)
        if settings.APP_DEBUG:
            # dev 下额外留栈：生产开它会把驱动层的完整报错串（含实参）写进日志
            log.exception("db_error", request_id=rid, error_type=type(exc).__name__, **detail)
        else:
            log.error("db_error", request_id=rid, error_type=type(exc).__name__, **detail)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=_payload(ErrorCode.INTERNAL_ERROR.value, "服务内部错误，请稍后重试", rid),
        )

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
        rid = new_request_id()
        log.exception("unhandled_error", request_id=rid, error_type=type(exc).__name__)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=_payload(ErrorCode.INTERNAL_ERROR.value, "服务内部错误，请稍后重试", rid),
        )
