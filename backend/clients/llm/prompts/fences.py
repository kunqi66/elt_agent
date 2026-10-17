"""围栏：``<<TAG>>`` … ``<<END>>``。**全仓库唯一一份实现**。

提示词构造器（``prompts/plan.py``、``prompts/autofix.py``）与确定性假 Provider
（``fake.py``）共用本模块，防止两边各写一套正则后慢慢漂移——那种漂移的表现是
「真实模型能答、假 Provider 读不到档案」，而它只会在没有 key 的机器上暴露。

同一文件还负责剥离**代码围栏**（```json … ```）：两者都是「围栏」，都是
「从模型输出里抠出结构化内容」这件事的两半，放一起才不会出现
「洗输出」与「拼提示词」各自演进、互相不认识。
"""

from __future__ import annotations

import re

#: 标签只允许大写字母、数字、下划线：标签会拼进正则，限制字符集是最省事也最可靠的注入防线。
_TAG_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")

_END = "<<END>>"


def _check_tag(tag: str) -> str:
    if not _TAG_RE.fullmatch(tag):
        raise ValueError(f"围栏标签必须是 <<大写字母/数字/下划线>> 形式，收到：{tag!r}")
    return tag


def wrap(tag: str, payload: str) -> str:
    """把 ``payload`` 包进 ``<<TAG>>`` … ``<<END>>``。"""
    return f"<<{_check_tag(tag)}>>\n{payload}\n{_END}"


def _fence_re(tag: str) -> re.Pattern[str]:
    # 换行都是可选的：块被裁剪器「减半」后可能出现围栏紧挨正文、甚至两行围栏相邻的形状。
    # 用固定模板（<<TAG>>\n…\n<<END>>）匹配会让那些形状静默取不到内容，
    # 于是「预算裁过」被误读成「模型没看见档案」
    return re.compile(
        rf"<<{re.escape(_check_tag(tag))}>>[ \t]*\r?\n?(?P<body>.*?)\r?\n?[ \t]*{re.escape(_END)}",
        re.DOTALL,
    )


def extract(text: str, tag: str) -> str | None:
    """取出第一个 ``<<TAG>>`` 块的内容；不存在返回 ``None``。

    返回 ``None`` 而不是空串：调用方要能区分「提示词里根本没有这块」与
    「这块是空的」——前者是拼装缺陷（该报错），后者是合法输入。
    """
    match = _fence_re(tag).search(text)
    return match.group("body") if match else None


def extract_all(text: str, tag: str) -> list[str]:
    """取出所有同名块。同名多块是合法的（如多张源表的档案各一块）。"""
    return [m.group("body") for m in _fence_re(tag).finditer(text)]


# ---------------------------------------------------------------- 代码围栏

#: 带语言标记（```json）与裸围栏（```）都覆盖。
_CODE_FENCE_RE = re.compile(r"```[a-zA-Z0-9_+-]*[ \t]*\r?\n(?P<body>.*?)```", re.DOTALL)


def strip_code_fences(text: str) -> str:
    """从模型输出里抠出 JSON 文本（be-08 §6.4 第 2 步、§9 的四种输入形态）。

    四种输入都要能过：带语言标记的围栏、无语言标记的裸围栏、完全无围栏、
    以及**前后夹带解释文字**（「好的，这是方案：{…} 以上」）。
    最后一种用「第一个 ``{`` 到最后一个 ``}``」兜底——模型在 JSON 前后写人话
    是常态，为此判一次解析失败纯属浪费一轮重试。
    """
    stripped = text.strip()
    match = _CODE_FENCE_RE.search(stripped)
    if match:
        return match.group("body").strip()
    start, end = stripped.find("{"), stripped.rfind("}")
    if start != -1 and end > start:
        return stripped[start : end + 1]
    return stripped


__all__ = ["extract", "extract_all", "strip_code_fences", "wrap"]
