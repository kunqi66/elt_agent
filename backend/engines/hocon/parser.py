"""HOCON 语法解析与校验（be-10 §6.4）。

## 决策：用 pyhocon（0.3.63），不自研

HOCON 是 **HOCON 规范**，不是「有嵌套的 key=value」：``${}`` 替换、``=`` / ``:`` / ``{}``
三种赋值形态混用、含点号的键、三次引号的多行串、``include``、列表拼接——每一条都有
边角。自研解析器的典型故障不是「漏掉一个特性」，而是**把合法配置判成非法**（假阳性），
而门禁的 syntax 检查一旦有假阳性，用户就会被自家门禁挡在门外且无从下手：他手上的配置
在 SeaTunnel 里跑得通，在门禁里过不去，且错误信息指向一个语法根本没错的位置。

pyhocon 是**纯 Python、无 IO**（import 期不读文件、不发网络），因此不破坏
``engines/`` 的纯函数约束（CLAUDE.md §4.3）；本模块只调它的 ``parse_string``，
不碰 ``parse_file`` / ``parse_URL``。

## 关闭变量替换

``resolve=False``：``${...}`` 是**待运行期注入**的东西，在解析期把它求值有两种坏结果——
令牌被替换成空值（``${?X}``）或直接抛 ``ConfigSubstitutionException``（未定义变量）。
用户手写的 ``${ENV}`` 也不该在门禁里被求值：门禁回答的是「语法对不对」，不是「环境变量
配没配」，后者由运行期回答。

## 一个 pyhocon 的坑（实测）

``resolve=False`` 只是**不替换**，替换点会以 ``ConfigValues`` 留在树里；而
``ConfigTree.as_plain_ordered_dict()`` 遇到 ``ConfigValues`` 会抛
``ConfigException('The config tree contains unresolved elements')``——它假定树已完全求值。
于是「解析成功」的树在转普通 dict 时炸掉，而 ``${ENV}`` 是完全合法的 HOCON。
因此 :func:`_plain` 先走 ``as_plain_ordered_dict()``（渲染器自己的产物、以及所有不含
``${}`` 的 HOCON 都走这条快路），仅在它拒绝时才接手，把未求值的替换点还原成
``${...}`` 文本。**这不是第二个解析器**：它不判定合法性，只做 dtype 转换，
合法性判定始终只有 pyhocon 一处。

## 只做语法

必填块 / 插件合法性 / 占位符形态 / 值域属于门禁的 ``check_schema``（be-11 §6），
它消费本模块返回的 ``config``（普通 dict）——本模块不重复实现一遍，否则「同一份
HOCON，gate 说有问题、parser 说没问题」时没人知道该信谁。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from pyhocon import ConfigFactory
from pyhocon.config_tree import ConfigQuotedString, ConfigSubstitution, ConfigTree, ConfigValues

from engines.hocon.types import PARSER_VERSION, HoconParseError, HoconParseResult

#: 错误文案里回显原文时，把看起来像凭据的值抹掉（CLAUDE.md §5.2）。
#: 解析错误会被前端渲染、也可能进 findings，而 HOCON 是**用户手写过的**文本——
#: 他完全可能把真口令写进去（这正是门禁要拦的形态），回显它就等于把口令抄了一遍。
_CREDENTIAL_RE = re.compile(
    r"(?i)\b(password|passwd|pwd|user|username|url|access_key|secret_key)\b(\s*[=:]\s*)(\"[^\"]*\"|\S+)"
)

#: 回显原文的截断长度：够定位，又不至于把一整行超长 SQL 复制进错误文案。
_SNIPPET_MAX = 160


def parse_and_validate(content: str) -> HoconParseResult:
    """解析 HOCON。**对任何输入都不抛异常**——失败就返回 ``ok=False`` 与可定位的错误。

    「不抛异常」是硬要求：调用方是门禁的 ``check_syntax``，它的语义是「产出一条
    finding」而不是「整个门禁崩掉」。``content`` 甚至可能是 ``None``（前端还没填），
    那种输入也要变成一条 issue，而不是让 API 返回 500。
    """
    try:
        tree = ConfigFactory.parse_string(content, resolve=False)
    except Exception as exc:  # 解析器可能抛 pyparsing 的任意子类，全接住是设计而不是偷懒
        return HoconParseResult(ok=False, errors=(_error_from(exc, content),))

    try:
        config = _plain(tree)
    except Exception as exc:  # 同上：转换失败也要变成可定位的错误，而不是一个栈
        return HoconParseResult(ok=False, errors=(_error_from(exc, content),))

    return HoconParseResult(ok=True, config=config, parser_version=PARSER_VERSION)


def _plain(node: Any) -> Any:
    """pyhocon 的树 → 普通 dict。

    ``as_plain_ordered_dict()`` 是主路径；它拒绝「含未求值替换点」的树（见模块 docstring），
    那时退回手写遍历。两条路径产出的都是普通 ``dict`` / ``list`` / 标量——
    ``hocon_artifact.validation_result`` 要落 jsonb，``OrderedDict`` 不该漏到那儿去。
    """
    if isinstance(node, ConfigTree):
        try:
            flat = node.as_plain_ordered_dict()
        except Exception:  # 唯一已知的抛点是「含未求值替换点」
            flat = None
        if flat is None:
            return {key.strip('"'): _plain(value) for key, value in node.items()}
        # as_plain_ordered_dict 自递归但仍返回 OrderedDict，这里再走一遍 _plain 抹平嵌套
        return {str(key): _plain(value) for key, value in flat.items()}
    if isinstance(node, Mapping):
        return {str(key): _plain(value) for key, value in node.items()}
    if isinstance(node, list | tuple):
        return [_plain(item) for item in node]
    if isinstance(node, ConfigValues):
        return _values_text(node)
    return node


def _values_text(values: ConfigValues) -> str:
    """把一组未求值的 token 还原成原文形态（``${X}`` 保留原样，不被求值成空）。"""
    parts: list[str] = []
    for token in values.tokens:
        if isinstance(token, ConfigSubstitution):
            parts.append("${" + ("?" if token.optional else "") + token.variable + "}")
        elif isinstance(token, ConfigQuotedString):
            parts.append(token.value)
        else:
            parts.append(str(token))
    return "".join(parts).strip()


def _error_from(exc: BaseException, content: Any) -> HoconParseError:
    """异常 → 一条带行列的错误。

    pyparsing 的异常带 ``lineno`` / ``col``；pyhocon 自己的 ``ConfigException`` 不带，
    那就落回 1/1——**不编造位置**：一个错的列号会让用户在 CodeMirror 里高亮到无关的行，
    比不定位更糟。
    """
    line = _positive_int(getattr(exc, "lineno", None)) or 1
    column = _positive_int(getattr(exc, "col", None)) or 1
    message = str(exc).strip() or type(exc).__name__
    snippet = _line_snippet(content, line)
    if snippet:
        message = f"{message}；原文：{snippet}"
    return HoconParseError(message=_redact(message), line=line, column=column)


def _positive_int(value: Any) -> int | None:
    return value if isinstance(value, int) and value >= 1 else None


def _line_snippet(content: Any, line: int) -> str:
    if not isinstance(content, str):
        return ""
    lines = content.splitlines()
    if not lines or line > len(lines):
        return ""
    return lines[line - 1].strip()[:_SNIPPET_MAX]


def _redact(text: str) -> str:
    return _CREDENTIAL_RE.sub(lambda match: f'{match.group(1)}{match.group(2)}"***"', text)


__all__ = ["parse_and_validate"]
