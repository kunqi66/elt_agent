"""Windows 上「用哪种事件循环」的**唯一**决定处。

## 为什么需要这个模块

`psycopg` 的异步模式**拒绝在 ProactorEventLoop 上工作**，连接时直接抛：

    psycopg.InterfaceError: Psycopg cannot use the 'ProactorEventLoop' to run in async
    mode. Please use a compatible event loop, for instance by running
    'asyncio.run(..., loop_factory=asyncio.SelectorEventLoop(...))'

而 Windows 上「新建事件循环」的默认结果就是 Proactor：

* `asyncio.new_event_loop()` → Proactor（默认策略）；
* `uvicorn` 0.54 起用 **loop factory** 而不是 policy，`win32 且非 subprocess` 时
  它自己就返回 `ProactorEventLoop`，**没有 CLI 开关可以改**（`--loop asyncio` 也是它）；
* 更关键的是时序：uvicorn 的 `Server.run()` 先建好循环，之后才 import `main:app`，
  所以**在被 import 的模块里设 policy 是无效的**——那时循环已经建出来了。

LangGraph 的 Postgres Checkpointer 走的正是 `psycopg` 异步连接
（`AsyncPostgresSaver`），于是「本机 Windows 起 API」会在启动钩子那一步直接退出。
这不是可选优化，而是不这么写就跑不起来。

## 这个模块负责什么、不负责什么

* 负责：把「该建哪种循环」收敛成一个函数，让 API 启动器与 worker 的任务执行器
  用**同一份判断**。两处各写一遍 `if sys.platform == "win32"` 的话，哪天 Linux
  上真出问题、或哪天 psycopg 改了限制，就得改两处并祈祷没漏。
* 不负责：不是「跨平台抽象层」。非 Windows 上一律返回默认行为，不多加一层。

## 为什么不干脆换个 Checkpointer

* `AsyncPostgresSaver` 是异步应用的正确选择（同步 saver 在 async 图上是错配）；
* 绕开它的办法只有「把同步 saver 丢线程池」，那等于在一个异步图里塞回同步 IO，
  把「中断/恢复真落 PG」这条硬约束变成一堆线程同步代码，更糟。
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable

#: 建循环的工厂类型（与 `asyncio.run(..., loop_factory=...)` 的入参一致）。
LoopFactory = Callable[[], asyncio.AbstractEventLoop]


def loop_factory() -> LoopFactory:
    """返回本机该用的循环工厂。

    Windows → `SelectorEventLoop`（psycopg 异步的硬要求）；
    其它平台 → `asyncio.new_event_loop`（保持默认，不改变生产行为）。
    """
    if sys.platform == "win32":
        return asyncio.SelectorEventLoop
    return asyncio.new_event_loop


def new_loop() -> asyncio.AbstractEventLoop:
    """按上面的规则建一个循环（调用方负责关闭）。"""
    return loop_factory()()


__all__ = ["LoopFactory", "loop_factory", "new_loop"]
