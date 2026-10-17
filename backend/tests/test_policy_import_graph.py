"""`engines/policy/` 的纯函数性守卫（be-07 §6.7）。

**为什么这个文件存在**：CLAUDE.md §5.3 要求「PDP 必须确定性，严禁 LLM 参与」。
那是一条**可被违反且不会被任何业务测试发现**的约束——某天有人为了「让规则更智能」
在 `rules.py` 里 import 一个 LLM 客户端，所有既有用例照样全绿，
而被破坏的是「P0/P1 拦截率 = 100%」这个可以被证明的性质。

所以这里用 AST 静态检查把它变成构建失败。检查项与 §6.7 一一对应，
唯一一处偏离写在 `_ALLOWED_TOP` 的注释里（`models.enums`）。
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

_POLICY_DIR = Path(__file__).resolve().parents[1] / "engines" / "policy"
_BACKEND_DIR = Path(__file__).resolve().parents[1]

# 允许的顶层模块。
# 与 §6.7 的偏离：**放行 `models.enums`**。§6.7 把整个 `models` 列为禁用，
# 但 DTO 必须引用 `RiskLevel` / `SensitiveLevel` / `ToolName` 等枚举，而
# `models/enums.py` 是纯值定义（只 import enum）——禁掉它只能靠复制一份枚举，
# 那才是真正的危险（两份取值表迟早不一致）。因此规则收紧为
# 「放行 `models.enums`，其余 `models.*` 一律禁用」。
_ALLOWED_TOP = frozenset(
    {
        "__future__",
        "pydantic",
        "dataclasses",
        "datetime",
        "decimal",
        "typing",
        "collections",
        "collections.abc",
        "enum",
        "math",
        "itertools",
        "functools",
        "engines",
    }
)
_ALLOWED_MODULES = frozenset({"models.enums"})

_FORBIDDEN_TOP = frozenset(
    {
        "fastapi",
        "sqlalchemy",
        "redis",
        "httpx",
        "requests",
        "celery",
        "asyncpg",
        "openai",
        "anthropic",
        "socket",
        "urllib",
        "subprocess",
        "os",
        "pathlib",
    }
)

# 时间依赖（§6.7 第 3 项）：`now` 必须由调用方注入，否则同一份证据在不同时刻会得出不同结论。
_TIME_CALLS = ("datetime.now", "datetime.utcnow", "time.time", "date.today")


def _policy_files() -> list[Path]:
    return sorted(_POLICY_DIR.rglob("*.py"))


def test_policy_files_exist() -> None:
    codes = {path.name for path in _policy_files()}
    assert {"pdp.py", "rules.py", "models.py"} <= codes


def test_import_白名单() -> None:
    """AST 扫全部 import，断言顶层模块在允许集合内（§6.7 第 1 项）。"""
    offenders: list[str] = []
    for path in _policy_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level and node.level > 0:
                    offenders.append(f"{path.name}: 相对 import 不在白名单内")
                    continue
                names = [node.module or ""]
            else:
                continue
            for name in names:
                top = name.split(".")[0]
                if name in _ALLOWED_MODULES:
                    continue
                if top in _ALLOWED_TOP:
                    continue
                offenders.append(f"{path.name}: import {name}")
    assert offenders == [], f"engines/policy 引入了不允许的依赖：{offenders}"


def test_禁用依赖确实在禁用名单里() -> None:
    """防止有人把 `_FORBIDDEN_TOP` 删空后本文件仍然全绿。"""
    for name in ("fastapi", "sqlalchemy", "httpx", "openai", "os"):
        assert name in _FORBIDDEN_TOP
        assert name not in _ALLOWED_TOP


def test_无时间依赖() -> None:
    """AST 扫调用名，禁 `datetime.now(` / `time.time(` 等（§6.7 第 3 项）。"""
    offenders: list[str] = []
    for path in _policy_files():
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            dotted = _dotted_name(node.func)
            if dotted in _TIME_CALLS:
                offenders.append(f"{path.name}:{node.lineno} 调用 {dotted}")
    assert offenders == [], f"engines/policy 出现时间依赖：{offenders}"


def test_无文件与网络IO() -> None:
    """AST 扫 `open(`；网络 IO 已由 import 白名单覆盖。"""
    offenders: list[str] = []
    for path in _policy_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _dotted_name(node.func) == "open":
                offenders.append(f"{path.name}:{node.lineno}")
    assert offenders == [], f"engines/policy 出现文件 IO：{offenders}"


def test_纯函数层不引入自己的IO依赖() -> None:
    """§6.7 第 2 项：AST 只能看见**直写**的 import，间接依赖要在这里挡。

    判据是**增量**而不是「`sys.modules` 里没有 sqlalchemy」，这一点必须说清楚：
    `engines/policy` 必然要 import `models.enums`（`RiskLevel` 等枚举的唯一来源），
    而 `models/__init__.py` 按设计「导入本包即导入全部 ORM 模型」以服务 Alembic
    的 autogenerate。因此 `sqlalchemy` 与 `os` 一定会在 `sys.modules` 里，
    **但它不是 `engines/` 引入的**。

    所以断言写成：`import engines.policy.pdp` 之后，相对「只 import `models.enums`」
    的模块**增量**里不得出现任何 IO 能力模块。这样任何一条真实的间接依赖
    （`engines → 某模块 → httpx`）都会被抓住，而设计上既有的那一层不会被误报。
    """
    # `-I` 会同时忽略 PYTHONPATH 与当前目录，所以路径必须显式塞进 sys.path：
    # 这正是我们要的——不借助任何环境变量，只让被测包本身可见。
    watched = ("sqlalchemy", "httpx", "fastapi", "redis", "celery", "asyncpg", "openai", "socket", "urllib")
    code = (
        "import sys, json; "
        f"sys.path.insert(0, {str(_BACKEND_DIR)!r}); "
        "import models.enums; "
        f"before = {{m for m in {watched!r} if m in sys.modules}}; "
        "import engines.policy.pdp; "
        f"after = {{m for m in {watched!r} if m in sys.modules}}; "
        "print(json.dumps(sorted(after - before)))"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-c", code],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"独立解释器 import 失败：{result.stderr.strip()}"
    assert result.stdout.strip() == "[]", (
        f"engines/policy 相对 models.enums 额外引入了 IO 能力模块：{result.stdout.strip()}"
    )


def _dotted_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _dotted_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
