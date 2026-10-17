"""任务包。

**这个包在被 import 时要做两件事**，都不是可有可无的副作用，而是任务能被正确
投递、执行的前提。两件事都源自同一类问题：**worker 进程的导入环境与 API 进程不同**，
而这类问题在单测里完全看不见（单测直接调任务函数，不经过 broker 也不经过 worker）。

第一件：把 Celery 应用设为**默认应用**，见 ``tasks/celery_app.py``。

第二件：把后端根目录压进 ``sys.path``，见下方 ``ensure_backend_root_importable``。
"""

from __future__ import annotations

import sys
from pathlib import Path

from celery.signals import celeryd_init

_BACKEND_ROOT = str(Path(__file__).resolve().parents[1])


def ensure_backend_root_importable() -> None:
    """把后端根目录放到 ``sys.path`` 首位，让 worker 里的惰性导入能解析。

    症状：``celery -A tasks.celery_app worker`` **启动完全正常**，可任务一被执行就
    ``ModuleNotFoundError: No module named 'services'``（实测踩到，见 ``tasks/datasource.py``
    里的 ``from services import datasource``）。根因在 Celery 加载 ``-A`` 指定 app 的方式：
    ``celery/utils/imports.py::import_from_cwd`` 用 ``cwd_in_path()`` 上下文管理器，
    **进入时把 cwd 插到 sys.path 首位、退出时再删掉**。于是 worker 起来的那一刻 cwd 还在
    （``tasks.celery_app`` 与它牵连的 ``core``/``models`` 都进了 ``sys.modules``），
    进程真正开始干活时 cwd 已被撤走——``sys.path[0]`` 实为 ``.venv/Scripts/celery.exe``。
    所以只有「任务运行期才第一次导入」的模块会炸：顶层导入过的早就在 ``sys.modules`` 里，
    怎么都不会报错。API 进程反而没事，因为 uvicorn 会把 ``''``（即 cwd）插进 ``sys.path``。

    为什么必须**无条件删掉再压首位**，不能用 ``if _BACKEND_ROOT not in sys.path`` 守卫：
    Celery 插进来的那个 cwd 与 ``_BACKEND_ROOT`` 是**同一个字符串**（worker 的 cwd 就是
    后端根目录），守卫会认为「已经在里面了」而跳过，紧接着 Celery 就把它撤掉了——
    修复静默失效（实测 ``present_before=True``，白名单式的写法在这里是个陷阱）。

    放在 ``tasks/__init__.py`` 而不是 ``celery_app.py``：任何 ``tasks.*`` 的导入都必然先执行
    本文件，而 ``celery_app.py`` 自身要 ``from core.config import settings``——先补路径，
    再谈导入。
    """
    while _BACKEND_ROOT in sys.path:
        sys.path.remove(_BACKEND_ROOT)
    sys.path.insert(0, _BACKEND_ROOT)


# 脚本 / 单测 / uvicorn 这些入口本来就把 cwd 放在 sys.path 上，这里再显式压一次，
# 让「谁在跑」不再影响导入结果。
ensure_backend_root_importable()

# 这里才有 E402：先修 sys.path，再导入依赖它的模块——顺序不能反。
from tasks.celery_app import celery_app  # noqa: E402


@celeryd_init.connect
def _worker_bootstrap(**_kwargs: object) -> None:
    """worker 启动引导：先补 ``sys.path``，再注册工具执行器。**顺序不能反。**

    补路径：上面那次插入发生在 Celery 的 ``cwd_in_path()`` 之内，退出时会被它一并清掉
    （它 ``sys.path.remove(cwd)``，而两者是同一个字符串）。``celeryd_init`` 由
    ``celery/apps/worker.py`` 在 Worker 构造早期发出，此时 app 已加载完毕、尚未开始
    消费队列，正是把路径**永久**补回来的时候。

    注册执行器：``harness`` 的工具注册表**没有执行器就等于拒绝执行**
    （``get_executor`` 抛 ``UnauthorizedToolError``，这是刻意的「宁可拒绝也不假成功」）。
    真实实现由 ``clients/datasource_probe.py`` 提供，且**刻意不在 import 期自动注册**
    （注册表禁止覆盖，单测要能先注入假执行器），所以必须有这么一处显式调用。
    漏掉的症状与路径问题同一副面孔：**API 一切正常，一连通性测试就 failed/500**，
    只在 worker 日志里留一行 ``UnauthorizedToolError``。先补路径再注册——注册要 import
    ``clients`` 这个顶层包，路径不对那一步就炸。
    """
    ensure_backend_root_importable()

    from clients.datasource_probe import register as register_probe_executors
    from clients.seatunnel.executor import register as register_seatunnel_executors
    from clients.target.errors_table import build_error_table_reader
    from clients.target.executor import register as register_target_executors
    from services.quality import register_error_table_reader

    register_probe_executors()
    # 作业提交 / 停止的执行器：与 API 侧同一份注册，两个进程都必须有——
    # 只注册探查那两个的话，症状是「点执行 → run 变 failed」，日志里一行
    # UnauthorizedToolError，而单测因为直接 import 模块永远发现不了。
    register_seatunnel_executors()
    # 目标端（原子 Swap / 回滚清理）：**只有 worker 真的会执行它们**
    # （发布与回滚都从 Outbox 投过来），API 侧那次注册纯属两侧对齐。
    register_target_executors()
    # 质量报告的目标端只读读取器：**只有 worker 会用到它**（运行收尾在 worker 里跑），
    # 但两侧都注册——「哪个进程需要它」将来会变，而漏注册的症状是运行期
    # CONFLICT「读取器未注册」，那时已经走完整条链路了。
    register_error_table_reader(build_error_table_reader)


__all__ = ["celery_app", "ensure_backend_root_importable"]
