"""提示词模板与版本注册表（be-08 §2）。

``PROMPT_VERSIONS`` 是**版本号的事实来源**，会被写进
``pipeline_version.generation_context.prompt_versions``，也是 Benchmark 缓存键的一部分。

本模块**只放注册表、不 import 具体模板**：模板之间会互相引用 ``fences``，
让包 ``__init__`` 再去 import 它们，等于把「import 顺序」变成一条隐式依赖。
代价是版本号在两处出现（这里与模板模块的 ``PROMPT_VERSION_*`` 常量），
由 ``tests/test_llm_provider.py`` 断言两者相等来兜住——比 import 环强。

``intent`` / ``hocon`` / ``diagnose`` 三个模板尚未实现（分别属 be-09 / be-10 / be-19），
**不预先占位**：注册表里写着一个不存在的模板版本，比什么都不写更具误导性。
"""

from __future__ import annotations

#: 模板名 → 版本号。新增模板必须同时登记，否则 ``generation_context`` 里就没有它的版本，
#: 「同一需求为何生成不同方案」也就无从追溯。
PROMPT_VERSIONS: dict[str, str] = {
    "plan": "1.0",
    "autofix": "1.0",
}


def prompt_version(template: str) -> str:
    """取某个模板的版本号。未登记的模板**报错**而不是回默认值——
    回默认值会让新模板悄悄以 ``1.0`` 落库，而它其实一次都没被审计过。"""
    try:
        return PROMPT_VERSIONS[template]
    except KeyError:
        raise KeyError(f"未登记的提示词模板：{template!r}（已登记：{sorted(PROMPT_VERSIONS)}）") from None


__all__ = ["PROMPT_VERSIONS", "prompt_version"]
