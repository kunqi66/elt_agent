"""``api/`` 调用 ``services/`` 时的**关键字名**契约。

## 为什么值得单独写一个文件

分层铁律说「``api/`` 只做校验、注入、转发」，于是 api 层里最常出现的一行是
``await xxx_service.foo(session=..., ctx=..., 一堆关键字...)``。
**这行代码的名字写错不会在 import 期报错**：``api`` 与 ``services`` 都能正常导入、
应用的 OpenAPI 也能正常生成、路由也注册得上——错的名字要到**真的有人请求这个端点**
才炸成 ``TypeError: diff_versions() got an unexpected keyword argument``，
也就是 500。而那往往发生在演示或联调现场。

这个项目里真的发生过一次（``GET /pipeline-versions/diff`` 传 ``from_version_id`` /
``to_version_id``，而 service 的形参叫 ``base_version_id`` / ``target_version_id``），
而当时 ``tests/test_pipeline_api.py`` 全是源码级与 DTO 级断言、**没有一条真的调用它**，
所以测试一直全绿。教训不是「补一条用例」，而是：
**这种错误要用一条覆盖所有端点的静态检查来挡，靠人记得补用例是挡不住的。**

## 它怎么工作

不连库、不起 HTTP：把 ``api/*.py`` 解析成 AST，找出所有
``<别名>.<函数>(...)`` 形态的调用（别名来自 ``from services import x as y`` 这类导入），
再 ``import`` 对方模块、取真实签名 ``inspect.signature``，
最后断言**每个关键字实参都真的在形参表里**。
"""

from __future__ import annotations

import ast
import importlib
import inspect
from pathlib import Path
from typing import Any

import pytest

BACKEND = Path(__file__).resolve().parents[1]
API_DIR = BACKEND / "api"

#: 只查这些前缀下的模块被 api 调用时的关键字名。
#: 判据是「跨层」——``services`` / ``harness`` / ``workflow`` 是 api 的下游；
#: ``schemas`` / ``core`` 的调用点同理适用，但它们的实参大多是位置参数，噪音大于收益。
_CHECKED_PREFIXES: tuple[str, ...] = ("services", "harness", "workflow", "clients")

#: 至少要扫出这么多个调用点。**这条断言是这个文件自己的元测试**：
#: 哪天有人把导入形态改成 ``import services.pipeline.artifacts as x`` 或者干脆不再
#: 从 api 调 service，解析器会静默扫到 0 个调用点然后「全绿」——
#: 那正是本文件要防的那种「不报错但没在检查」。
_MIN_CALL_SITES = 20


def _aliases(tree: ast.Module) -> dict[str, str]:
    """``api`` 模块里的局部名 → 被导入对象的全路径。

    只认 ``from a.b import c as d`` 与 ``from a import b`` 两种形态——本项目里
    api 层调 service 一律是这两种（见 ``grep '^from services' api/*.py``）。
    """
    out: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                local = alias.asname or alias.name
                out[local] = f"{node.module}.{alias.name}"
    return out


def _dotted(node: ast.expr) -> str | None:
    """把 ``a.b.c`` 这种属性链读成字符串；不是名字就回 ``None``。"""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def _resolve(dotted: str) -> Any:
    """``services.pipeline.artifacts.diff_versions`` → 那个函数对象。

    逐段 ``import`` + ``getattr``，用到哪段导哪段——避免为一个名字把整棵子树都导进来。
    """
    parts = dotted.split(".")
    for cut in range(len(parts), 0, -1):
        module_path = ".".join(parts[:cut])
        try:
            obj: Any = importlib.import_module(module_path)
        except ImportError:
            continue
        for attr in parts[cut:]:
            obj = getattr(obj, attr)
        return obj
    raise ImportError(dotted)


def _call_sites() -> list[tuple[str, int, str, dict[str, int]]]:
    """扫全 ``api/``，回 ``(文件, 行号, 被调对象全路径, {关键字: 行号})``。"""
    found: list[tuple[str, int, str, dict[str, int]]] = []
    for path in sorted(API_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        aliases = _aliases(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            target = _dotted(node.func)
            if target is None:
                continue
            head = target.split(".")[0]
            if head not in aliases:
                continue
            full = f"{aliases[head]}.{target.split('.', 1)[1]}" if "." in target else aliases[head]
            if not full.startswith(_CHECKED_PREFIXES):
                continue
            keywords = {kw.arg: kw.value.lineno for kw in node.keywords if kw.arg is not None}
            found.append((path.name, node.lineno, full, keywords))
    return found


def test_扫到的调用点数量没有塌成零() -> None:
    """元测试：检查器自己不许「静默不检查」。"""
    sites = _call_sites()
    assert len(sites) >= _MIN_CALL_SITES, (
        f"只扫到 {len(sites)} 个 api→下层 的调用点（期待 ≥ {_MIN_CALL_SITES}）。"
        "要么有人改了导入形态、要么解析器失效了——这时下面那条断言会因为扫不到东西而假绿。"
    )


@pytest.mark.parametrize(
    ("filename", "lineno", "target", "keywords"),
    [(f, line, target, kws) for f, line, target, kws in _call_sites()],
    ids=lambda value: str(value) if not isinstance(value, dict) else "",
)
def test_关键字实参都在被调函数的形参表里(
    filename: str, lineno: int, target: str, keywords: dict[str, int]
) -> None:
    """核心断言：名字写错就该在这里红，而不是在演示现场炸成 500。"""
    try:
        func = _resolve(target)
    except (ImportError, AttributeError) as exc:  # pragma: no cover - 真出现即缺陷
        pytest.fail(f"{filename}:{lineno} 调用了不存在的 {target}（{exc}）")
    signature = inspect.signature(func)
    parameters = signature.parameters
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values()):
        pytest.skip(f"{target} 用 **kwargs 收任意关键字，签名挡不住名字写错")

    unknown = sorted(name for name in keywords if name not in parameters)
    assert not unknown, (
        f"{filename}:{lineno} 调用 {target} 时传了它没有的形参 {unknown}；"
        f"它的形参是 {sorted(parameters)}。"
        "这种错误 import 期不报、路由照注册，只在真的有人请求这个端点时才炸成 500。"
    )


def test_这条守卫真的抓得住那个历史缺陷() -> None:
    """把那段真实的错名代码喂给检查逻辑，确认它会红。

    没有这条，上面两条断言可能因为「别名表扫不到东西」而永远绿——
    **守卫自己也要被守**（CLAUDE.md §9.1 的用法）。这里刻意用一段**独立源码字符串**
    而不是去改真实文件：既证明检查器有效，又不会让本文件依赖某一版 api 代码。
    """
    historical = """
from services.pipeline import artifacts as artifact_service

async def diff_versions(base_version_id, target_version_id, ctx, session):
    return await artifact_service.diff_versions(
        session=session,
        ctx=ctx,
        from_version_id=base_version_id,
        to_version_id=target_version_id,
    )
"""
    tree = ast.parse(historical)
    aliases = _aliases(tree)
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
    assert len(calls) == 1
    call = calls[0]
    full = f"{aliases['artifact_service']}.{call.func.attr}"  # type: ignore[attr-defined]
    parameters = inspect.signature(_resolve(full)).parameters
    unknown = sorted(kw.arg for kw in call.keywords if kw.arg not in parameters)
    assert unknown == ["from_version_id", "to_version_id"], (
        f"检查逻辑没认出历史缺陷（实际算出 {unknown}）——那它对真实代码也一样没在检查"
    )


def test_参数用到的模块真的存在于仓库里() -> None:
    """补一条「解析器认得全」的断言：别名表里的模块必须都能 import 成功。

    解析失败会被 ``_resolve`` 的逐段回退悄悄吞掉一部分（例如把
    ``services.pipeline.artifacts.diff_versions`` 解析成 ``services.pipeline.artifacts``），
    于是签名比对比了个模块对象，``**kwargs`` 判断永远不成立、断言永远绿。
    """
    aliases = _aliases(ast.parse((API_DIR / "pipeline_versions.py").read_text(encoding="utf-8")))
    assert aliases["artifact_service"] == "services.pipeline.artifacts"
    resolved = _resolve("services.pipeline.artifacts.diff_versions")
    assert callable(resolved)
    assert not inspect.ismodule(resolved)
