"""Celery 应用绑定的回归测试。

存在理由：``@shared_task`` 绑定的是 Celery 的**默认应用**，而不是 ``tasks.celery_app``
里那个配好 broker 的实例。worker 用 ``-A tasks.celery_app`` 启动时会把 current_app
设对，于是**worker 侧永远看不出问题**，就绪探针查的又是 Redis 本身也正常；
真正出问题的只有「API 进程发起投递」这一条路径——它静默走到空 broker 上，
回落到 Celery 内置的 ``amqp://localhost:5672``。

这个错在真环境里表现为「点测试连通性 → 500」，报错栈在 kombu/pyamqp 里，
与数据源、与 Redis 配置都看不出关系。所以用不依赖 broker 的断言把它钉住。
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest

from core.config import settings
from tasks.async_runner import run_coro
from tasks.celery_app import celery_app


def test_默认应用就是配置好的那个() -> None:
    """缺 `set_default()` 时 current_app 是匿名默认应用，broker 为空。

    `current_app` 是 Celery 的 Proxy（线程/协程本地），必须 `_get_current_object()`
    解包后才能比身份——直接 `is` 会失败，而两个对象的 repr 与 id 看起来一模一样，
    报错信息会误导向「明明相等」。
    """
    from celery import current_app

    assert current_app._get_current_object() is celery_app, (
        "current_app 不是 tasks.celery_app.celery_app：shared_task 会投到空 broker"
    )
    assert celery_app.conf.broker_url, "默认应用的 broker 为空会回落到 amqp://localhost"


@pytest.mark.parametrize(
    "module_name",
    ["tasks.datasource", "tasks.metadata", "tasks.outbox"],
)
def test_共享任务都绑定到同一个应用(module_name: str) -> None:
    """任务对象持有的 app 必须是有 broker 的那个，否则 apply_async 投错地方。"""
    import importlib

    module = importlib.import_module(module_name)
    tasks = [obj for obj in vars(module).values() if hasattr(obj, "apply_async")]
    assert tasks, f"{module_name} 里没有任务对象，测试本身失效了"
    for task in tasks:
        assert task.app is celery_app, f"{module_name}.{task.name} 绑到了别的应用"


def test_broker_指向配置里的_redis() -> None:
    """不能是 Celery 内置的 amqp 默认值——那正是「投错 broker」的症状。"""
    assert celery_app.conf.broker_url == settings.celery_broker_url
    assert celery_app.conf.broker_url.startswith("redis://")
    assert "localhost:5672" not in celery_app.conf.broker_url
    assert celery_app.conf.result_backend == settings.celery_result_backend


def test_只允许_json_序列化() -> None:
    """pickle 反序列化等于远程代码执行，这条不接受任何例外。"""
    assert celery_app.conf.task_serializer == "json"
    assert celery_app.conf.accept_content == ["json"]


def test_任务协程复用同一个事件循环() -> None:
    """``asyncio.run()`` 每次新建并关闭循环，第 2 个任务就会取到挂在死循环上的连接。

    这条对应真实事故：worker 进程里第 1 个任务成功、第 2 个报
    ``Event loop is closed``（或 ``AttributeError: 'NoneType' object has no attribute 'send'``）。
    """
    loops: list[asyncio.AbstractEventLoop] = []

    async def first() -> object:
        loops.append(asyncio.get_running_loop())
        return asyncio.Event()  # 绑定在本次循环上的资源，跨循环使用即失效

    async def second(event: object) -> bool:
        loops.append(asyncio.get_running_loop())
        event.set()  # type: ignore[attr-defined]
        return True

    event = run_coro(first())
    assert run_coro(second(event)) is True
    assert loops[0] is loops[1], "两次任务跑在不同的循环上——连接池里的连接会失效"
    assert not loops[0].is_closed(), "循环被关掉了，下一个任务会直接炸"


def test_任务模块不得直接用_asyncio_run() -> None:
    """新增任务模块若写 ``asyncio.run``，就复现了「第 2 个任务必失败」那个 bug。"""
    offenders: list[str] = []
    for path in sorted((Path(__file__).resolve().parents[1] / "tasks").glob("*.py")):
        if path.name == "async_runner.py":
            continue  # 它自己的 docstring 就要解释「为什么不能用 asyncio.run」
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            code = line.split("#", 1)[0]  # 注释里会提到 asyncio.run，不算违规
            if "asyncio.run(" in code:
                offenders.append(f"{path.name}:{lineno}")
    assert not offenders, f"这些位置用了 asyncio.run，应改用 tasks.async_runner.run_coro：{offenders}"


def test_worker_加载完_app_后任务里的惰性导入仍可解析() -> None:
    """在子进程里复刻 worker 的 sys.path 处境，验证惰性导入活了下来。

    复刻的顺序就是 Celery 真实做的事：``cwd_in_path()`` 进入（cwd 进 sys.path）
    → 加载 ``tasks.celery_app`` → 退出（cwd 被 ``remove`` 掉）→ Worker 构造发出
    ``celeryd_init`` → 任务体执行 ``from services import ...``。

    这条测试**必须**在子进程里跑：本进程的 sys.path 已被 pytest 改过，且 ``services``
    等模块早在 sys.modules 里，直接断言会永远通过——那样等于没测。
    """
    backend_root = Path(__file__).resolve().parents[1]
    script = (
        "import os, sys\n"
        "cwd = os.getcwd()\n"
        "sys.path[:] = [p for p in sys.path if p not in ('', cwd, '.')]\n"
        "sys.path.insert(0, cwd)\n"                 # cwd_in_path() 进入
        "import tasks.celery_app\n"                 # celery 在此加载 app
        "assert cwd in sys.path\n"
        "sys.path.remove(cwd)\n"                    # cwd_in_path() 退出，撤掉 cwd
        "assert cwd not in sys.path, '前提不成立：cwd 没被撤掉，测不出问题'\n"
        "from celery.signals import celeryd_init\n"
        "celeryd_init.send(sender='probe', instance=None, conf=None, options={})\n"
        "import services\n"                         # 任务函数里的惰性导入
        "print('OK')\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(backend_root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, (
        "复刻 worker 情形后惰性导入失败——tasks/__init__.py 的 sys.path 引导或 "
        "celeryd_init 补丁可能被删了：\n"
        f"{proc.stdout}\n{proc.stderr}"
    )
    assert "OK" in proc.stdout
