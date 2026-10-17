"""FastAPI 应用入口。

分层：本文件只做**装配**（日志、中间件、异常处理、路由挂载、生命周期），不含任何业务逻辑。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from api.health import router as health_router
from api.idempotency import IdempotencyMiddleware
from api.router import api_router
from clients.datasource_probe import register as register_probe_executors
from clients.seatunnel.executor import register as register_seatunnel_executors
from clients.target.errors_table import build_error_table_reader
from clients.target.executor import register as register_target_executors
from core.config import settings
from core.db import dispose_engine
from core.errors import register_exception_handlers
from core.ids import new_request_id
from core.logging import configure_logging, get_logger
from core.redis import close_redis
from services.quality import register_error_table_reader
from workflow.checkpointer import close_checkpointer, open_checkpointer

log = get_logger(__name__)


class RequestContextMiddleware(BaseHTTPMiddleware):
    """为每个请求绑定关联 ID 与基础上下文，供全链路日志（api → worker）串联。

    ``request_id`` 透传规则：优先沿用上游（Nginx/调用方）传来的 ``X-Request-ID``，
    否则新生成，并在响应头回写，便于用户在页面上报问题时定位日志。
    """

    async def dispatch(self, request: Request, call_next) -> Response:  # type: ignore[no-untyped-def]
        request_id = request.headers.get("X-Request-ID") or new_request_id()
        # 同时挂到 request.state：依赖注入（如 api/deps_auth 构造 SubjectContext）需要它，
        # 而 contextvars 那只对日志处理器可见，业务代码读不到
        request.state.request_id = request_id
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(
            request_id=request_id,
            method=request.method,
            path=request.url.path,
            client_ip=request.client.host if request.client else None,
        )
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        # 响应完成后解绑，避免上下文泄漏到同进程处理的下一个请求
        structlog.contextvars.clear_contextvars()
        return response


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    configure_logging()
    log.info("app_starting", **settings.safe_dump())
    if not settings.is_production:
        log.warning("non_production_mode", app_env=settings.APP_ENV)
    # 工具执行器必须由启动钩子显式注册（harness 注册表禁止覆盖，单测要能注入假的，
    # 所以 clients 刻意不在 import 期自动注册）。API 进程现阶段不直接执行工具，
    # 但漏注册的后果是「运行时才 500、日志里只有一行 UnauthorizedToolError」，
    # 两侧启动路径保持一致比省这一行划算。worker 侧见 tasks/__init__.py。
    register_probe_executors()
    register_seatunnel_executors()
    # 目标端两个工具（原子 Swap / 回滚清理）同样在启动期注册：漏了就是运行期
    # UnauthorizedToolError，而那时已经走到发布这一步了（前面所有门禁都过了）。
    register_target_executors()
    # 质量报告的目标端**只读**读取器（错误表计数/摘要）。它不经 ToolBroker——
    # 没有副作用，所以不需要 Capability（``engines/quality/ports.py`` 的取舍）。
    # 漏注册的症状是每次收尾都报 CONFLICT「读取器未注册」；刻意不降级成「全部为 0」，
    # 因为 0 是一个完全合法的报告值，降级会让质量报告静默变干净。
    register_error_table_reader(build_error_table_reader)
    # LangGraph 的 Checkpoint 存取器也在这里打开：它是生成链路的持久化底座，
    # 而「中断/恢复必须真落 PG」（CLAUDE.md §9）意味着**不能用进程内字典兜底**。
    # 放在启动钩子而不是 service 里懒开，是为了让「PG 连不上 / 缺 langgraph-checkpoint-postgres」
    # 在启动时就炸出来，而不是等用户点了「生成」才收到一个 500。
    await open_checkpointer()
    yield
    log.info("app_stopping")
    # 顺序：checkpointer 持有独立的 psycopg 连接池，先还它再关共享 engine
    await close_checkpointer()
    await close_redis()
    await dispose_engine()


def create_app() -> FastAPI:
    app = FastAPI(
        title="ELT-Agent 控制面",
        description="生产级 ETL-Agent 数据集成平台 · 控制面 API",
        version="0.1.0",
        # 生产环境关闭交互式文档，减少攻击面；开发环境保留
        docs_url=None if settings.is_production else "/docs",
        redoc_url=None if settings.is_production else "/redoc",
        openapi_url=None if settings.is_production else "/openapi.json",
        lifespan=lifespan,
    )

    app.add_middleware(RequestContextMiddleware)
    # 幂等中间件必须**包住路由**（注册在 RequestContext 之后、CORS 之前），
    # 否则它的「同 key 重放」逻辑会绕过路由层直接生效，
    # 或在 CORS 之外导致预检请求也被记账
    app.add_middleware(IdempotencyMiddleware)

    # 开发环境才允许跨域；生产由 Nginx 同源反代，不需要 CORS
    if not settings.is_production and settings.CORS_ORIGINS:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.CORS_ORIGINS,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
            expose_headers=["X-Request-ID"],
        )

    register_exception_handlers(app)

    # 探针挂在根路径（Nginx 直接反代 /healthz，不经过 /api 前缀）
    app.include_router(health_router)
    app.include_router(api_router, prefix=settings.API_V1_PREFIX)

    return app


app = create_app()
