"""运维维护任务：保留策略清理、证据账本签名检查点、备份。

保留策略（见 .env.example）：ExecutionRun 与运行日志 90 天；质量报告与证据账本永久（0 = 永久）。
证据账本清理必须**成块**进行，且清理前先打签名检查点，否则链会出现断点。

TODO(M2/M4)：账本检查点依赖 harness/ledger.py；备份脚本放 deploy/backup/。
"""

from __future__ import annotations

from typing import Any

from celery import shared_task

from core.config import settings
from core.logging import get_logger

log = get_logger(__name__)


@shared_task(name="tasks.maintenance.enforce_retention", ignore_result=True)
def enforce_retention() -> dict[str, Any]:
    """按保留策略清理历史数据。当前为骨架实现。"""
    log.info(
        "retention_tick",
        run_log_days=settings.RUN_LOG_RETENTION_DAYS,
        quality_report_days=settings.QUALITY_REPORT_RETENTION_DAYS,
        evidence_days=settings.EVIDENCE_RETENTION_DAYS,
        implemented=False,
    )
    return {"deleted": 0, "implemented": False}


@shared_task(name="tasks.maintenance.checkpoint_ledger", ignore_result=True)
def checkpoint_ledger() -> dict[str, Any]:
    """为未签名的证据事件区间生成 Ed25519 签名检查点。当前为骨架实现。"""
    log.info("ledger_checkpoint_tick", interval=settings.LEDGER_CHECKPOINT_INTERVAL, implemented=False)
    return {"blocks_created": 0, "implemented": False}
