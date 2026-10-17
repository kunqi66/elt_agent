"""绑定元数据档案（be-09 §5.1 / §5.3 / §7）。

**这个节点是「恢复时跳过已完成的安全探查」的样板**，也是 §5.3 里被点名的那一步：
``metadata.probe`` 是**要连源库**的副作用动作（``harness.ToolBroker`` 的工具之一），
在恢复时重放它既慢又会在数据源上留下多余的连接与审计记录。

做法就是 §5.3 的那一条：进入时先看「目标产物（``profile_ids``）在不在 **且**
输入指纹（源/目标 id）没变」，是则整节点 no-op。
只看「产物在不在」不够——用户改了目标数据源再生成时，产物也在，但它是旧的。

档案过期（``expires_at`` 已过或 ``status != ready``）当场拒绝并给
``METADATA_PROFILE_EXPIRED``（409），且**不能**走到 LLM：§9 的反例用例要求
「档案过期时未发生 LLM 调用」，而在本节点抛错天然满足（它排在 ``generate_plan`` 之前）。

本节点通过 ``services/pipeline/repository.profile_summary`` 读库——
**只调这一个纯数据访问函数**，不碰任何编排（be-09 §2 的依赖方向要求）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Final

from core.errors import AppError, ErrorCode
from core.logging import get_logger
from workflow.nodes import runtime_of
from workflow.state import already_done, input_fingerprint

__all__ = ["metadata_bind_node", "profile_datasource_ids"]

log = get_logger(__name__)

_NODE = "metadata_bind"

#: 档案必须处于的状态（``ProbeStatus.READY`` 的 value；不 import 枚举是因为
#: ``profile_summary`` 返回的是 dict，取的是同一个字符串契约）。
_READY: Final[str] = "ready"

#: 拒绝的理由，用于 ``details``（前端要按它区分「过期」与「没探查过」）。
_REASON_EXPIRED: Final[str] = "expired"
_REASON_NOT_READY: Final[str] = "not_ready"


def profile_datasource_ids(state: dict[str, Any]) -> list[int]:
    """需要绑档案的数据源 id（源在前、目标在后，去掉空值）。

    目标数据源**必须**绑定：门禁的 ``check_schema`` 要对着目标档案判
    「目标列是否存在/类型是否兼容」，没有它就只能跳过整类检查。
    """
    out: list[int] = []
    for key in ("source_data_source_id", "target_data_source_id"):
        value = state.get(key)
        if isinstance(value, int):
            out.append(value)
    return out


def _assert_profile_usable(datasource_id: int, summary: dict[str, Any]) -> None:
    """档案可用性判定。**只信明确信号，不猜**：

    - ``expires_at`` 存在且已过去 → 过期（**先判它**：一个过期的档案
      ``ready`` 仍是 True——``latest_profile`` 只按 ``status == ready`` 取行，
      不看 ``expires_at``。先判 ``ready`` 会让「档案过期」永远走不到，
      而它恰恰是本节点存在的主要理由，见 be-09 §7）；
    - ``ready`` 存在且为假 → 没探查过/不可用；
    - ``status`` 存在且不等于 ``ready`` → 不可用（兼容按状态名回报的实现）。

    三样都缺时不拒绝：``profile_summary`` 的返回形状由 service 定义，
    缺字段意味着「该 service 不提供这个信号」，而不是「档案有问题」——
    把「读不到」当成「过期」会让整条链路在 service 尚未定型时全部失败。
    """
    expires_at = summary.get("expires_at")
    if expires_at:
        moment = _parse_moment(expires_at)
        if moment is not None and moment <= datetime.now(UTC):
            raise AppError(
                f"数据源 {datasource_id} 的元数据档案已过期，请重新探查",
                code=ErrorCode.METADATA_PROFILE_EXPIRED,
                http_status=409,
                details={"data_source_id": datasource_id, "reason": _REASON_EXPIRED},
            )
    ready = summary.get("ready")
    status = summary.get("status")
    unusable = (ready is False) or (status is not None and str(status) != _READY)
    if unusable:
        raise AppError(
            f"数据源 {datasource_id} 的元数据档案不可用，请重新探查",
            code=ErrorCode.METADATA_PROFILE_EXPIRED,
            http_status=409,
            details={"data_source_id": datasource_id, "reason": _REASON_NOT_READY},
        )


def _parse_moment(value: Any) -> datetime | None:
    """把 ISO-8601 串/带时区 datetime 归一成 UTC 时间；解析不了就返回 ``None``。

    ``datetime.fromisoformat`` 在 3.11+ 能解析 ``Z`` 后缀，但**解析不了**
    无时区的时间串——那会被当成 naive，与 UTC 比较时抛 ``TypeError``。
    所以 naive 的结果统一按 UTC 解释（存档时间一律以 UTC 落库，见 ``core/ids.as_utc``）。
    """
    if isinstance(value, datetime):
        moment = value
    else:
        try:
            moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    return moment.astimezone(UTC) if moment.tzinfo else moment.replace(tzinfo=UTC)


async def metadata_bind_node(state: dict[str, Any], config: Any = None) -> dict[str, Any]:
    """图节点：给每个数据源绑定 ``metadata_profile``。"""
    runtime = runtime_of(config)
    datasource_ids = profile_datasource_ids(state)
    fingerprint = input_fingerprint(datasource_ids=datasource_ids)
    if already_done(state, _NODE, fingerprint, "profile_ids", "profile_sha256"):
        log.info(
            "metadata_bind_skipped",
            request_id=runtime.request_id,
            pipeline_id=runtime.pipeline_id,
            reason="already_bound",
        )
        return {}

    from services.pipeline.repository import profile_summary

    if not datasource_ids:
        raise AppError(
            "Pipeline 未指定源/目标数据源，无法绑定元数据档案",
            code=ErrorCode.VALIDATION_ERROR,
            http_status=422,
        )

    profile_ids: dict[str, int] = {}
    profile_sha256: dict[str, str] = {}
    for datasource_id in datasource_ids:
        summary = await profile_summary(
            session=runtime.session,
            tenant_id=runtime.tenant_id,
            data_source_id=datasource_id,
        )
        _assert_profile_usable(datasource_id, summary)
        profile_id = summary.get("profile_id") or summary.get("id")
        if profile_id is None:
            # 「档案不存在」与「档案过期」是两种不同的修复动作（前者要探查、
            # 后者要重新探查），所以分开报，而不是合并成一个笼统的 409。
            raise AppError(
                f"数据源 {datasource_id} 尚未探查元数据，请先执行探查",
                code=ErrorCode.METADATA_PROFILE_EXPIRED,
                http_status=409,
                details={"data_source_id": datasource_id, "reason": _REASON_NOT_READY},
            )
        profile_ids[str(datasource_id)] = int(profile_id)
        profile_sha256[str(datasource_id)] = str(summary.get("sha256") or "")

    # 档案快照含列名与脱敏样本，一律不进日志——只记计数（be-09 §8）。
    log.info(
        "metadata_bound",
        request_id=runtime.request_id,
        tenant_id=runtime.tenant_id,
        pipeline_id=runtime.pipeline_id,
        datasource_count=len(datasource_ids),
    )
    return {
        "profile_ids": profile_ids,
        "profile_sha256": profile_sha256,
        "node_fingerprints": {_NODE: fingerprint},
    }
