"""Celery 应用与可靠性配置。

可靠性要点（对应 PRD §10.2）：
- ``task_acks_late`` + ``prefetch_multiplier=1``：任务失败/Worker 崩溃时消息回到队列，不丢。
- 因此**所有任务必须幂等**——重复投递是常态，不是异常。
- 投递方一律走 Transactional Outbox（``harness/outbox.py``），禁止在事务里直接 ``apply_async``。
"""

from __future__ import annotations

from celery import Celery
from celery.signals import after_setup_logger, after_setup_task_logger

from core.config import settings
from core.logging import configure_logging

celery_app = Celery(
    "elt_agent",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=[
        "tasks.outbox",
        "tasks.execution",
        "tasks.supervision",
        "tasks.maintenance",
        # 漏写任务模块的症状是 worker 报 "Received unregistered task"，
        # **任务不执行且业务层看不到任何错误**——新增任务模块必须同步加到这里
        "tasks.datasource",
        "tasks.metadata",
    ],
)

# 把自己设为**默认应用**。这一行不是可选项，缺了它 API 进程派发的任务会全部投错 broker。
#
# 所有任务模块都用 `@shared_task`，而 shared_task 绑定的是 Celery 的「默认应用」，
# **不是**这个模块里的 celery_app。默认应用若从未被设置过，它的 broker_url 是空的，
# 于是回落到 Celery 内置的 `amqp://localhost:5672`，表现为
# `ConnectionRefusedError: [WinError 10061] 目标计算机积极拒绝`。
#
# 为什么这个错很难发现：worker 侧的 `celery -A tasks.celery_app worker` 会把
# current_app 设成 celery_app，所以 **worker 一切正常、日志干净、就绪探针也绿**；
# 只有从 API 进程发起的投递才会走到空 broker 上。测试同理——单测直接调任务函数，
# 不经过 broker，因此全绿。第一次暴露是在真环境里点「测试连通性」。
celery_app.set_default()

celery_app.conf.update(
    # --- 序列化（只允许 JSON，禁止 pickle：pickle 反序列化即远程代码执行）---
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    # --- 时区 ---
    timezone="Asia/Shanghai",
    enable_utc=True,
    # --- 可靠性 ---
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_track_started=True,
    result_expires=3600 * 24,
    # --- 时限（硬时限略大于契约预算，留出清理与证据落库时间）---
    task_time_limit=settings.CELERY_TASK_TIME_LIMIT,
    task_soft_time_limit=settings.CELERY_TASK_SOFT_TIME_LIMIT,
    # --- 队列 ---
    task_default_queue="etl.default",
    task_routes={
        "tasks.outbox.*": {"queue": "etl.default"},
        "tasks.supervision.*": {"queue": "etl.default"},
        "tasks.maintenance.*": {"queue": "etl.default"},
        "tasks.execution.*": {"queue": "etl.execute"},
    },
    # --- 定时任务（beat）---
    beat_schedule={
        "scan-outbox": {
            "task": "tasks.outbox.scan_and_dispatch",
            "schedule": float(settings.OUTBOX_SCAN_INTERVAL_SEC),
            "options": {"expires": settings.OUTBOX_SCAN_INTERVAL_SEC * 2},
        },
        "collect-run-metrics": {
            "task": "tasks.supervision.collect_active_runs",
            "schedule": 30.0,
            "options": {"expires": 25},
        },
        # 运行收尾：发现「作业已经跑完」并把运行推到结论（质量报告 → 原子发布）。
        #
        # 周期取 40 s 而不是与监督环相同的 30 s：两条环都是浮点周期，相位跟着
        # beat 进程的启动时刻走——**同周期又同时起跑就是永久锁步**，于是同一秒里
        # 两条环各对每条活跃运行发一次数据面 HTTP 请求。beat 没有「错开 n 秒」的
        # 配置（Celery 的 ``crontab`` 只到分钟，没有秒），所以错开只能靠**不同周期**：
        # 40 与 30 的最小公倍数是 120 s，两条环每两分钟就对不齐一次，长期看是真错开的。
        # 代价是「跑完到出报告」最多晚 40 s——对一个每 30 s 才采一次指标的监督环来说，
        # 这点延迟换掉一次周期性突发是划算的。
        "finalize-finished-runs": {
            "task": "tasks.execution.scan_finished_runs",
            "schedule": 40.0,
            "options": {"expires": 35},
        },
        "retention-cleanup": {
            "task": "tasks.maintenance.enforce_retention",
            "schedule": 3600.0 * 6,
        },
    },
    # --- 日志交给 structlog（含脱敏）---
    worker_hijack_root_logger=False,
)


@after_setup_logger.connect
@after_setup_task_logger.connect
def _configure_celery_logging(**_kwargs: object) -> None:
    configure_logging()
