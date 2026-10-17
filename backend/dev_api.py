"""本机起控制面 API 的启动器（**开发用**，容器里跑的是 `uvicorn main:app`）。

## 为什么不直接 `uvicorn main:app`

uvicorn 0.54 在 Windows 上默认建 **ProactorEventLoop**，而 psycopg 的异步模式
拒绝在它上面连接——LangGraph 的 Postgres Checkpointer（`AsyncPostgresSaver`）
走的正是 psycopg 异步，于是应用会在启动钩子里直接退出：

    psycopg.InterfaceError: Psycopg cannot use the 'ProactorEventLoop' ...

**在被 import 的模块里设 policy 救不了**：uvicorn 的 `Server.run()` 先建循环、
后 import `main:app`，等 `main.py` 执行时循环已经建好了。所以只能由启动器
自己决定循环类型——这正是 `core/eventloop.py` 存在的理由。

## 与生产的差异

只有「循环类型」这一处差异，且它只在 Windows 上生效；Linux 上 `loop_factory()`
返回默认行为，本文件与 `uvicorn main:app` 等价。所以容器/ECS 上仍用
`uvicorn main:app`（见 CLAUDE.md §7），本文件只服务本机开发。

用法（在 `backend/` 下）：

    uv run python dev_api.py                 # 127.0.0.1:8000
    uv run python dev_api.py --port 8001
"""

from __future__ import annotations

import argparse
import asyncio
import sys

import uvicorn

from core.eventloop import loop_factory


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="本机控制面 API 启动器")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--log-level", default="info")
    parser.add_argument(
        "--reload",
        action="store_true",
        help="改代码自动重启；**与 psycopg 的循环要求冲突**（reload 由 uvicorn 自己建循环），"
        "本机开发建议手动重启",
    )
    args = parser.parse_args(argv)

    config = uvicorn.Config(
        "main:app",
        host=args.host,
        port=args.port,
        log_level=args.log_level,
        reload=args.reload,
    )
    server = uvicorn.Server(config)

    if args.reload:
        # reload 模式由 uvicorn 的 supervisor 进程建循环，绕不过 Proactor；
        # 与其静默地跑一个「启动即崩」的配置，不如直接说清楚
        print("--reload 在 Windows 上会退回 ProactorEventLoop，psycopg 连不上；请手动重启。", file=sys.stderr)
        return 2

    if sys.platform == "win32":
        # 3.12 起 asyncio.run 支持 loop_factory：不开 policy、不手工管循环生命周期
        asyncio.run(server.serve(), loop_factory=loop_factory())
    else:
        server.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
