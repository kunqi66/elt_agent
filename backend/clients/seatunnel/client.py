"""SeaTunnel Zeta REST 客户端（be-15 §3.1 / §6.4）。

**端点集中在模块级常量**：Zeta 各版本的 Hazelcast REST 命名有差异（§10 第 1 条，
本机无 Docker 时无法实机核对）。改常量即可适配，不必动调用点——
这也是为什么这里不做「按版本探测端点」的自适应：探测逻辑会把一次「路径写错」
变成一次「偶发的、取决于版本的失败」，而后者难查得多。

三条重试纪律（§6.4）：

- ``submit`` **只在连接前失败时重试一次**。返回 5xx 时结果不确定（可能已创建），
  此时先 ``find_by_name`` 查重、查到就复用——盲目重试会让同一次运行在 Zeta 里起两个作业，
  两个作业都往同一张影子表写，而这**不会报错**。
- ``stop`` 重试 3 次（退避 1/2/4 s）：停不下来的代价是留下一个正在搬数据的作业。
- ``status`` / ``metrics`` 失败**不抛**：只读失败不该阻塞监督（be-16 走退化路径）。
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from clients.seatunnel.models import (
    JobMetricsDTO,
    JobStatusDTO,
    SeatunnelRejected,
    SeatunnelUnavailable,
    SubmitResult,
    classify_failure,
    map_state,
)
from core.logging import get_logger

log = get_logger(__name__)

#: Zeta REST 路径。``{job_id}`` / ``{job_name}`` 由调用方填充。
SUBMIT_PATH = "/hazelcast/rest/maps/submit-job"
STOP_PATH = "/hazelcast/rest/maps/stop-job"
JOB_INFO_PATH = "/hazelcast/rest/maps/job-info/{job_id}"
JOB_METRICS_PATH = "/hazelcast/rest/maps/job-metrics/{job_id}"
RUNNING_JOBS_PATH = "/hazelcast/rest/maps/running-jobs"

#: ``job_id`` 由 ``run_id`` 派生：同一个 run 无论提交多少次都拿到同一个 id，
#: 于是 Zeta 侧的「同 id 重复提交」天然被拒（防重第三道，§6.4）。
_JOB_ID_NAMESPACE = uuid.UUID("6f9619ff-8b86-d011-b42d-00cf4fc964ff")

_STOP_BACKOFF_SEC = (1, 2, 4)


def job_id_for_run(run_id: int) -> str:
    """``run_id`` → 稳定的 ``job_id``。**确定性**，不含时间与随机数。"""
    return str(uuid.uuid5(_JOB_ID_NAMESPACE, f"run:{int(run_id)}"))


class SeatunnelClient:
    """Zeta 客户端。无状态、可并发（httpx.AsyncClient 内部有连接池）。"""

    def __init__(self, base_url: str, *, timeout_sec: float = 10.0) -> None:
        self._base = base_url.rstrip("/")
        self._timeout = timeout_sec

    def _client(self) -> httpx.AsyncClient:
        # 每次新建：提交是低频操作（一次运行一次），而复用长连接会带来
        # 「隧道抖动后连接池里握着死连接」那一类问题（CLAUDE.md §9）。
        return httpx.AsyncClient(base_url=self._base, timeout=self._timeout)

    # ------------------------------------------------------------ 提交

    async def find_by_name(self, job_name: str) -> str | None:
        """按作业名反查 job_id（对账与防重用）。查不到或读失败都返回 ``None``。"""
        try:
            async with self._client() as client:
                response = await client.get(RUNNING_JOBS_PATH)
                if response.status_code != 200:
                    return None
                return _find_job_id(response.json(), job_name)
        except Exception as exc:
            log.warning("seatunnel_find_by_name_failed", error_type=type(exc).__name__)
            return None

    async def submit(self, config_path: Path, *, job_name: str, job_id: str | None = None) -> SubmitResult:
        """提交作业。**已存在同名作业时复用**（不重复提交）。"""
        existing = await self.find_by_name(job_name)
        if existing is not None:
            log.info("seatunnel_submit_reused", job_name=job_name, job_id=existing)
            return SubmitResult(
                job_id=existing, job_name=job_name, accepted_at=datetime.now(UTC), reused=True
            )

        resolved_id = job_id or job_id_for_run(0)
        # 读文件放到线程里：这是个 async 函数，而 ``Path.read_text`` 是阻塞调用。
        # 单文件读很快，但它是**含真口令的那份文件**的大小成正比的 IO，
        # 与事件循环里的其它任务（同进程的 outbox 扫描）共享同一个循环。
        body = await asyncio.to_thread(config_path.read_text, encoding="utf-8")
        params = {"jobId": resolved_id, "jobName": job_name}

        try:
            async with self._client() as client:
                response = await client.post(
                    SUBMIT_PATH,
                    params=params,
                    content=body.encode("utf-8"),
                    headers={"Content-Type": "text/plain; charset=utf-8"},
                )
        except httpx.HTTPError as exc:
            # 连接前失败：请求没出去，重试一次是安全的（§6.4 的唯一可重试情形）
            log.warning("seatunnel_submit_connect_failed", error_type=type(exc).__name__)
            try:
                async with self._client() as client:
                    response = await client.post(
                        SUBMIT_PATH,
                        params=params,
                        content=body.encode("utf-8"),
                        headers={"Content-Type": "text/plain; charset=utf-8"},
                    )
            except httpx.HTTPError as exc2:
                raise SeatunnelUnavailable(f"提交连接失败（{type(exc2).__name__}）") from None

        if response.status_code >= 500:
            # 5xx：结果**不确定**（可能已创建）。先查一次重名，查不到才报错。
            retry_id = await self.find_by_name(job_name)
            if retry_id is not None:
                return SubmitResult(
                    job_id=retry_id, job_name=job_name, accepted_at=datetime.now(UTC), reused=True
                )
            raise SeatunnelUnavailable(f"提交返回 {response.status_code}")
        if response.status_code >= 400:
            raise SeatunnelRejected(f"提交被拒（{response.status_code}，{_first_line(response.text)[:120]}）")

        payload = _json_or_empty(response.text)
        returned = str(payload.get("jobId") or resolved_id)
        return SubmitResult(job_id=returned, job_name=job_name, accepted_at=datetime.now(UTC), reused=False)

    # ------------------------------------------------------------ 停止

    async def stop(self, job_id: str) -> None:
        """停止作业。**作业不存在视为成功**（幂等）：停止的目的是「让它不再跑」。"""
        last_error: Exception | None = None
        for attempt, delay in enumerate(_STOP_BACKOFF_SEC, start=1):
            try:
                async with self._client() as client:
                    response = await client.post(STOP_PATH, json={"jobId": job_id})
                if response.status_code in (200, 404):
                    return
                if response.status_code < 500:
                    # 4xx 除 404 外是「参数不对/无权限」，重试无用
                    raise SeatunnelRejected(f"停止被拒（{response.status_code}）")
                last_error = SeatunnelUnavailable(f"停止返回 {response.status_code}")
            except httpx.HTTPError as exc:
                last_error = SeatunnelUnavailable(f"停止连接失败（{type(exc).__name__}）")
            if attempt < len(_STOP_BACKOFF_SEC):
                await asyncio.sleep(delay)
        raise last_error or SeatunnelUnavailable("停止失败")

    # ------------------------------------------------------------ 只读

    async def status(self, job_id: str) -> JobStatusDTO:
        """查状态。**失败返回 ``NOT_FOUND`` 而不是抛异常**——调用方（监督/对账）
        需要的是「现在能不能推进」，不是一次异常。"""
        try:
            async with self._client() as client:
                response = await client.get(JOB_INFO_PATH.format(job_id=job_id))
        except httpx.HTTPError:
            return JobStatusDTO(job_id=job_id, state=map_state(None))
        if response.status_code == 404:
            return JobStatusDTO(job_id=job_id, state=map_state(None))
        if response.status_code != 200:
            return JobStatusDTO(job_id=job_id, state=map_state(None))
        payload = _json_or_empty(response.text)
        raw_state = str(payload.get("state") or payload.get("jobStatus") or "")
        error_text = str(payload.get("error") or payload.get("errorMsg") or "") or None
        return JobStatusDTO(
            job_id=job_id,
            state=map_state(raw_state),
            error_summary=error_text[:200] if error_text else None,
        )

    async def metrics(self, job_id: str) -> JobMetricsDTO | None:
        """读指标。**不可用时返回 ``None``**（不是抛异常、也不是全 0 的假 DTO）。

        两者必须分得清：``None`` 表示「这条路走不通，be-16 请走退化路径」，
        而全 0 的 DTO 会被阈值判定当成「读到 0 行」，于是正常作业被误判成空跑。
        """
        try:
            async with self._client() as client:
                response = await client.get(JOB_METRICS_PATH.format(job_id=job_id))
        except httpx.HTTPError:
            return None
        if response.status_code != 200:
            return None
        payload = _json_or_empty(response.text)
        if not payload:
            return None
        dto = JobMetricsDTO(
            read_rows=_as_int(payload.get("readRows") or payload.get("readCount")),
            written_rows=_as_int(payload.get("writtenRows") or payload.get("writeCount")),
            written_bytes=_as_int(payload.get("writtenBytes") or payload.get("writeBytes")),
            read_bytes=_as_int(
                payload.get("readBytes")
                or payload.get("readBytesTotal")
                or payload.get("sourceReceivedBytes")
                # Zeta 按表计的命名法（``TableSourceReceivedBytes`` 之类）会带前缀，
                # 这里退一步在键名里找：找得到就用，找不到就是 0（＝放大比判 unknown）。
                or _find_byte_counter(payload, "receivedbytes")
                or _find_byte_counter(payload, "readbytes")
            ),
            reject_rows=_as_int(payload.get("rejectRows")),
            duration_sec=_as_int(payload.get("durationSec") or payload.get("duration")),
            extra=_numeric_extras(payload),
        )
        if dto.read_bytes == 0 and dto.extra:
            # 读字节没认出来（放大比这一维会判 unknown）。把「引擎实际报了什么键」
            # 记一把——**只记键名、不记值**，值里可能有配置片段（见 ``_numeric_extras``）。
            # 这是下一次对着真 Zeta 校准映射的唯一线索：没有它，唯一的症状是
            # 「这一维永远 unknown」，而那种症状不会指向这个文件。
            log.debug("seatunnel_read_bytes_unmapped", counter_keys=sorted(dto.extra)[:20])
        return dto

    async def failure_kind(self, job_id: str) -> tuple[str, Any]:
        """取失败分类与摘要（be-15 §5.4）。查不到状态时返回 ``UNKNOWN``。"""
        status = await self.status(job_id)
        return classify_failure(status.error_summary), status


def _find_job_id(payload: Any, job_name: str) -> str | None:
    """从 ``running-jobs`` 的响应里找出同名的 job_id。

    响应形状在不同版本间是 ``{"jobs": [...]}`` / 裸数组 / ``[{jobId, jobName}]`` 三种之一，
    所以这里三种都认——认不出的形状返回 ``None``（＝没查到），
    而**不是**抛异常：对账失败不该阻断提交。
    """
    items: list[Any] = []
    if isinstance(payload, dict):
        raw = payload.get("jobs") or payload.get("data") or []
        items = raw if isinstance(raw, list) else []
    elif isinstance(payload, list):
        items = payload
    for item in items:
        if isinstance(item, dict) and str(item.get("jobName") or "") == job_name:
            job_id = item.get("jobId") or item.get("job_id")
            if job_id:
                return str(job_id)
    return None


#: ``metrics()`` 已经映射成具名字段的那批键。剩下的**数字**计数器进 ``extra`` 留档，
#: 它们是把这套映射对着真 Zeta 校准时的唯一线索（本地没有 SeaTunnel，
#: 键名是照 Zeta 的命名习惯与候选清单写的，未经真机核对）。
_MAPPED_METRIC_KEYS = frozenset(
    {
        "readRows",
        "readCount",
        "writtenRows",
        "writeCount",
        "writtenBytes",
        "writeBytes",
        "readBytes",
        "readBytesTotal",
        "sourceReceivedBytes",
        "rejectRows",
        "durationSec",
        "duration",
    }
)


def _numeric_extras(payload: Mapping[str, Any]) -> dict[str, Any]:
    """未识别的计数器（**只收数字**）。

    为什么只收数字：Zeta 的响应里可能回显配置片段或错误原文，
    而 ``extra`` 的用途是「留档给下一次校准映射用」——把字符串收进来，
    就等于给整段配置开了一条进日志 / 进库的路（与 ``seatunnel.stop``
    只记 ``failure_kind`` 不记原文是同一条理由）。
    """
    return {
        key: value
        for key, value in payload.items()
        if key not in _MAPPED_METRIC_KEYS and isinstance(value, (int, float)) and not isinstance(value, bool)
    }


def _find_byte_counter(payload: Mapping[str, Any], needle: str) -> int:
    """按「键名里含某个词」找字节计数器（大小写无关，只认数字值）。

    存在的理由是**版本差异**：Zeta 有的版本把计数器按表名前缀摊平
    （``TableSourceReceivedBytes``），有的给汇总键。认不出就回 0，
    ``extra`` 里留着原始键名供人工核对——比写死一个猜的键名更诚实。
    """
    for key, value in payload.items():
        if needle in key.lower() and isinstance(value, (int, float)) and not isinstance(value, bool):
            return _as_int(value)
    return 0


def _json_or_empty(text: str) -> dict[str, Any]:
    import json

    try:
        payload = json.loads(text)
    except (ValueError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _first_line(text: str) -> str:
    return (text or "").strip().splitlines()[0] if (text or "").strip() else ""


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


__all__ = [
    "JOB_INFO_PATH",
    "JOB_METRICS_PATH",
    "RUNNING_JOBS_PATH",
    "STOP_PATH",
    "SUBMIT_PATH",
    "SeatunnelClient",
    "job_id_for_run",
]
