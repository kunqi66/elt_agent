"""service 的写入口必须自己提交事务——「图跑完了、库里是空的」那一类事故的守卫。

**这条用例的来历**（真事）：M2 打通生成链路时，端到端跑完图一切正常、日志齐全，
但 `pipeline` 表 **0 行**。原因是 LangGraph 的 checkpoint **自己提交**，
所以 `checkpoints` 表记录完整、看起来「状态没丢」，而我们的业务写入
压在 ``core.db.get_session`` 那个**只借连接、不提交**的会话上，
请求一结束就整批回滚。症状极具误导性：下一个请求报「生成会话不存在」。

修法是给 5 个写入口各补一行 ``await session.commit()``。但「补一行」治不了本——
**下一个人写第 6 个入口时同样会忘，而且忘掉的表现依然是全绿**。
所以这里改成扫源码：凡**会写**的 service 公开函数，函数体里必须出现 ``commit()``。

判据（为什么这么定）：
- 只查 ``services/`` 下**公开**的异步函数——它们是 api/tasks 的入口，
  也就是事务边界该落在的地方；下划线开头的是内部步骤，由调用方负责提交；
- ``repository.py`` 整模块豁免：它按设计只「在会话上执行语句」，提交归 service
  （见 ``services/pipeline/repository.py`` 与 ``core/db.get_session`` 的 docstring）；
- 「会不会写」用**调用链**判断（调了写 helper 也算会写），不是只看 ``session.add``——
  写语句大多藏在 repository 里，只看本函数体会漏掉全部真实入口。
"""

from __future__ import annotations

import ast
import pathlib

import pytest

SERVICES_DIR = pathlib.Path(__file__).resolve().parent.parent / "services"

#: 整模块豁免：只执行语句、不负责提交的那一层。
EXEMPT_MODULES = {"repository.py"}

#: 逐函数豁免：**公开但由调用方持有事务**的内部步骤。
#:
#: 它们必须是公开的（跨模块契约，见 ``be-13 §3.2`` 的函数清单），但绝不能自己 ``commit()``——
#: 那会把「新建准备单 + 取代旧单 + 两条账本事件」**一个原子动作**劈成两个事务，
#: 中间崩溃就留下「新单在、旧单还活着」的双活状态，而两份单都能被审批。
#:
#: 进来必须同时满足两条：① 只有一个模块内调用方；② 那个调用方自己 ``commit()``。
#: 条目各自的这两条由 :func:`test_豁免项真的存在且被自己人调用` 守着。
TRANSACTION_INTERNAL: set[tuple[str, str]] = {
    ("preparation.py", "supersede_previous"),
}

#: 直白的写语句。
WRITE_OPS = (
    "session.add",
    "session.flush",
    "session.delete",
    "session.execute(insert",
    "session.execute(update",
    "session.execute(delete",
)

#: 会写库的 repository 入口（跨模块，所以要按名字认）。
WRITE_HELPERS = (
    "create_pipeline",
    "save_artifacts",
    "get_or_create_candidate",
    "replace_candidate_artifacts",
    "set_pipeline_status",
    "set_current_version",
    "append_message",
)


def _source(text: str, node: ast.AST) -> str:
    return ast.get_source_segment(text, node) or ""


def _writes(text: str, node: ast.AST) -> bool:
    source = _source(text, node)
    return any(op in source for op in WRITE_OPS) or any(f"{h}(" in source for h in WRITE_HELPERS)


def _audit(path: pathlib.Path) -> list[str]:
    """返回该模块里「会写但没提交」的公开函数名。"""
    text = path.read_text(encoding="utf-8")
    funcs = [n for n in ast.walk(ast.parse(text)) if isinstance(n, ast.AsyncFunctionDef)]

    writers = {n.name for n in funcs if _writes(text, n)}
    # 一层传递闭包：调用了写 helper 的函数也算会写（写语句通常藏在下一层）
    for node in funcs:
        source = _source(text, node)
        if any(f"{name}(" in source for name in writers):
            writers.add(node.name)

    return [
        node.name
        for node in funcs
        if node.name in writers
        and not node.name.startswith("_")
        and (path.name, node.name) not in TRANSACTION_INTERNAL
        and "commit()" not in _source(text, node)
    ]


def test_service_写入口都自己提交事务() -> None:
    offenders: list[str] = []
    checked = 0
    for path in sorted(SERVICES_DIR.rglob("*.py")):
        if path.name in EXEMPT_MODULES:
            continue
        missing = _audit(path)
        if missing:
            offenders.append(f"{path.relative_to(SERVICES_DIR.parent)}: {missing}")
        checked += 1

    assert not offenders, (
        "这些 service 写入口没有 commit()，请求结束会整批回滚"
        "（症状是「图跑完了、库里是空的」，因为外部框架的 checkpoint 自己提交，看不出异常）：\n  "
        + "\n  ".join(offenders)
    )
    assert checked > 0, "一个模块都没扫到——守卫的路径写错了，等于没有守卫"


def test_守卫自己认得出来违规(tmp_path: pathlib.Path) -> None:
    """元测试：把「忘了提交」的样例喂给守卫，它必须报出来。

    守卫最容易的坏法不是报错，而是**悄悄退化成永远通过**（比如 ``WRITE_HELPERS``
    改名后再也匹配不上）。所以这里反过来验一次它能抓人。
    """
    sample = tmp_path / "sample.py"
    sample.write_text(
        "async def bad_entry(session):\n"
        "    await save_artifacts(session=session, plan=None)\n"
        "    return 1\n"
        "\n"
        "async def good_entry(session):\n"
        "    await save_artifacts(session=session, plan=None)\n"
        "    await session.commit()\n",
        encoding="utf-8",
    )
    assert _audit(sample) == ["bad_entry"]


@pytest.mark.parametrize("helper", WRITE_HELPERS)
def test_写_helper_名单没写错字(helper: str) -> None:
    """名单里的每个 helper 都要真的存在——名单写错字会让守卫静默失效。"""
    found = any(
        helper in path.read_text(encoding="utf-8") for path in SERVICES_DIR.rglob("*.py")
    )
    assert found, f"{helper!r} 在 services/ 里找不到，守卫名单过期了"


@pytest.mark.parametrize(("module", "name"), sorted(TRANSACTION_INTERNAL))
def test_豁免项真的存在且被自己人调用(module: str, name: str) -> None:
    """豁免项是**减弱的守卫**，所以它自己也要被守着。

    两条：① 函数真的定义在那个模块里（改名后豁免自动失效就成了「无声放行」）；
    ② 模块内至少有一处调用——「只有一个模块内调用方」是它进名单的前提，
    函数变成没人调的死代码时，它凭什么豁免就该重新审一遍。
    """
    path = SERVICES_DIR / module
    assert path.exists(), f"豁免项指向的模块不存在：{module}"
    text = path.read_text(encoding="utf-8")
    assert f"async def {name}(" in text, f"{module} 里没有 {name}，豁免名单过期了"
    assert text.count(f"{name}(") >= 2, (
        f"{name} 在 {module} 内没有调用方——它已经不是「被调用方提交事务的内部步骤」，"
        "豁免理由不成立，请从 TRANSACTION_INTERNAL 移除"
    )
