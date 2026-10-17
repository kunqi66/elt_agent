"""Harness 层拒绝语义与 ``reject_stage`` 映射。

本模块**不自造错误码**（CLAUDE.md §6）：错误码一律取自 ``core.errors.ErrorCode``，
这里只负责两件 core 层不该管的事：

1. 为「工具被拒」补上 core 没有的 ``AppError`` 子类（``UNAUTHORIZED_TOOL`` 等码已存在，
   但缺承载它们的异常类）；
2. 定义 ``RejectStage``，把工具执行序的**第几步**被拒写进证据账本——
   排查「为什么这次执行没跑起来」时，只有错误码是不够的，
   同一个 ``CAPABILITY_INVALID`` 可能来自「没带 token」或「签名不对」或「主体不符」。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from core.errors import AppError, ErrorCode


class RejectStage(StrEnum):
    """``ToolBroker.execute`` 七步执行序中的拒绝位置。

    取值与 ``broker.execute`` 的注释编号一一对应，禁止各写各的。
    """

    LOOKUP = "lookup"  # ① 工具存在性
    REQUIREMENT = "requirement"  # ② Capability / 进程 / 前置条件要求
    SIGNATURE = "signature"  # ③ 验签与 claim 结构
    BINDING = "binding"  # ④ 四项绑定校验
    REPLAY = "replay"  # ⑤ 原子消费
    ARGS = "args"  # ⑥ args 模型校验
    EXECUTOR = "executor"  # ⑦ 执行器本身抛错


class UnauthorizedToolError(AppError):
    """工具未注册，或调用进程不是该工具允许的执行者，或 token 绑定的工具与本次不符。"""

    code = ErrorCode.UNAUTHORIZED_TOOL
    http_status = 403
    message = "工具未授权或不允许在该进程执行"


class CapabilityRequiredError(AppError):
    """该工具要求 Capability 但没带 token，或反过来免签却带了 token。

    两种情形共用一个码是刻意的：调用方要么走签发流程、要么干脆不带，
    「多带一个 token 更安全」是一种会绕开免签路径的误解，必须同样被拒。
    """

    code = ErrorCode.CAPABILITY_INVALID
    http_status = 403
    message = "Capability 使用方式与该工具的要求不符"


class QualityGateError(AppError):
    """``requires_quality_pass=True`` 的工具在质量未通过 / 运行已中断时被调用。"""

    code = ErrorCode.POLICY_BLOCKED
    http_status = 403
    message = "质量契约未通过或运行已中断，禁止执行该工具"


class PreparationNotReadyError(AppError):
    """Preparation 非 ``approved`` 或已过期。"""

    code = ErrorCode.PREPARATION_EXPIRED
    http_status = 409
    message = "Preparation 未批准或已过期，请重新提交审批"


class ExecutorFailedError(AppError):
    """执行器自身失败（区别于「未获授权」）。"""

    code = ErrorCode.INTERNAL_ERROR
    http_status = 500
    message = "工具执行失败"


# ---------------------------------------------------------------- 映射


def reject_stage_of(exc: AppError) -> RejectStage:
    """由异常码反推拒绝阶段。

    之所以能这样反推：每个码在 §7 的拒绝矩阵里**只对应一个阶段**
    （例如 ``CAPABILITY_REPLAYED`` 只可能来自第 ⑤ 步）。
    调用方若知道更精确的阶段（例如 args 校验失败），直接传 ``stage=`` 覆盖，
    不必依赖本函数。
    """
    match exc.code:
        case ErrorCode.UNAUTHORIZED_TOOL:
            return RejectStage.LOOKUP
        case ErrorCode.CAPABILITY_EXPIRED | ErrorCode.CAPABILITY_INVALID:
            return RejectStage.SIGNATURE
        case ErrorCode.TENANT_MISMATCH | ErrorCode.FINGERPRINT_MISMATCH:
            return RejectStage.BINDING
        case ErrorCode.CAPABILITY_REPLAYED:
            return RejectStage.REPLAY
        case ErrorCode.PREPARATION_EXPIRED | ErrorCode.POLICY_BLOCKED:
            return RejectStage.REQUIREMENT
        case ErrorCode.VALIDATION_ERROR:
            return RejectStage.ARGS
    return RejectStage.EXECUTOR


# 拒绝码 → ``capability.reject_reason`` 取值。
# 只允许《核心数据字典》§4 列举的六个值；**没有对应值的码不在此表内**，
# 此时保持 reject_reason 为 NULL —— 硬塞一个不存在的取值会污染该列的取值域。
REJECT_REASON_BY_CODE: dict[ErrorCode, str] = {
    ErrorCode.CAPABILITY_EXPIRED: "expired",
    ErrorCode.CAPABILITY_REPLAYED: "replay",
    ErrorCode.FINGERPRINT_MISMATCH: "fingerprint_mismatch",
    ErrorCode.UNAUTHORIZED_TOOL: "unauthorized",
    ErrorCode.CAPABILITY_INVALID: "unauthorized",
}


def reject_reason_of(code: ErrorCode) -> str | None:
    return REJECT_REASON_BY_CODE.get(code)


def reject_payload(
    *, stage: RejectStage, code: ErrorCode, tool: str, jti: str | None = None, **extra: Any
) -> dict[str, Any]:
    """拒绝事件的账本 payload。

    ``jti`` 只在已知时出现；**绝不含 token 原文、连接串、args 原值**——
    args 可能含引用型取值（表名、id），原样入账本会把业务结构固化进不可变账本，
    所以只记 ``args_keys``。
    """
    payload: dict[str, Any] = {"reject_stage": stage.value, "code": code.value, "tool": tool}
    if jti is not None:
        payload["jti"] = jti
    payload.update(extra)
    return payload
