"""任务里的异步入口：**整个 worker 进程复用一个事件循环**。

## 为什么不能用 ``asyncio.run()``

``asyncio.run()`` 每次都新建一个事件循环，**跑完就关**。而 ``core.db.engine``、
``core.redis`` 的客户端都是**模块级单例**：连接池里的连接绑定在「第一次那个循环」上。
第二个任务再取用这些连接时，驱动往一个已关闭的循环上投递回调，于是报
``RuntimeError: Event loop is closed``——或者更绕的形态
``AttributeError: 'NoneType' object has no attribute 'send'``
（greenlet 桥接在循环关闭后的表现，报错栈会指向 ``session.execute``，
看上去像 SQLAlchemy 的问题，其实与 SQL 无关）。

## 症状为什么难查

**worker 进程里第 1 个任务永远成功，第 2 个起必定失败**（实测：连通性测试 succeeded
→ 元数据探查 ``AttributeError`` → 再点连通性 ``Event loop is closed``）。
于是它表现为「手动点一次是好的，跑第二次就 500」，而下面这些场景全都看不出问题：

- 单测：每个用例自建循环，压根不复用连接池；
- 冒烟：只发一个任务，正好落在「第 1 个」；
- API 进程：uvicorn 全程一个循环，从来不会关。

复用一个循环而不是「每个任务结束后 dispose engine」：后者要逐个记得释放
engine / redis / vault 客户端，漏一个就复发，而且失败形态与这里一模一样；
复用一个循环是一次性的、对整类单例都成立的解法。
``core/db.py`` 已经开了 ``pool_pre_ping`` 与 ``pool_recycle=1800``，
所以长期存活的连接不会被中间设备静默断开的问题。

**新增任务模块一律用 ``run_coro``，不要写 ``asyncio.run``**（有用例守着这条）。

## Windows 上要挑循环类型

`asyncio.new_event_loop()` 在 Windows 上建的是 ProactorEventLoop，而 **psycopg 的
异步模式在它上面直接拒绝连接**（LangGraph 的 Postgres Checkpointer 就是这个组合）。
所以这里走 ``core.eventloop.new_loop()``——它是「该建哪种循环」的唯一决定处，
API 启动器 `dev_api.py` 用的是同一个判断。
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any

from core.eventloop import new_loop

# 进程级单例：Celery 的 prefork/solo 都是「每进程一份模块全局」，所以每个 worker
# 子进程各自持有一个循环，互不干扰。
_loop: asyncio.AbstractEventLoop | None = None


def run_coro[T](coro: Coroutine[Any, Any, T]) -> T:
    """在进程内复用的循环上把协程跑到完成，返回其结果。

    循环一旦建好就不再关闭，直到进程退出。
    """
    global _loop
    loop = _loop
    if loop is None or loop.is_closed():
        loop = new_loop()
        # 同时设为当前循环：库内部用 asyncio.get_event_loop() 取循环时才能拿到同一个
        asyncio.set_event_loop(loop)
        _loop = loop
    return loop.run_until_complete(coro)


__all__ = ["run_coro"]
