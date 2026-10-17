"""凭据形态的**纯函数**识别器。

它回答一个问题：这段结构化数据里，**有没有本不该出现的凭据**？
用在三处，共用同一份规则——各写一份必然出现「A 处拦了 B 处放行」：

- ``schemas/datasource.py``：登记数据源时校验 ``endpoint``；
- ``harness/tools.py``：工具 args 落 Broker 前兜底（防止有人用 args 夹带连接串）；
- ``services/governance``：Commit 前复核准备载荷。

放在 ``engines/`` 而不是 ``schemas/``：这是**领域规则**而非 DTO 校验，
而且 ``harness/`` 要能用它——``harness`` 依赖 ``engines``（纯函数层）是允许的，
依赖 ``schemas`` 则会让「安全内核」反过来被业务 DTO 的形状牵着走。

⚠️ 本模块**不修改**输入，只报告命中位置。静默剔除比拒绝更危险：
调用方会以为凭据被接受了（只是被洗掉了），而实际上它已经在进程内存里走了一圈。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

# 键名命中即视为凭据。刻意宽松：`pass`、`pwd`、`key` 这类短名宁可误伤，
# 因为漏放的代价是明文口令落进 jsonb 列，而误伤的代价只是让调用方换个键名。
_CREDENTIAL_KEY_RE = re.compile(
    r"(pass(word|wd)?|pwd|secret|token|credential|api[_-]?key|private[_-]?key"
    r"|access[_-]?key|secret[_-]?key|bearer|conn(ection)?[_-]?str)",
    re.IGNORECASE,
)

# 值侧兜底：键名可以随便起（`url`、`loc`、`dsn`），但值长得像连接串就藏不住。
# 只列真实会用到的协议，不做通用「://」匹配——那会把 MinIO 的 http:// 也误伤。
_DSN_VALUE_RE = re.compile(
    r"\b(mysql|mariadb|postgres|postgresql|psycopg|oracle|clickhouse|doris|redis"
    r"|mongodb|mssql|sqlserver|jdbc:[a-z]+)://[^\s]*:[^\s/@]*@",
    re.IGNORECASE,
)
# `jdbc:mysql://host:3306/db?user=u&password=p` 这种把凭据放进查询串的形态
_DSN_QUERY_KEY_RE = re.compile(r"[?&](password|pwd|passwd|user|username|token|secret)=", re.IGNORECASE)


class CredentialLeakError(ValueError):
    """命中了凭据形态。``paths`` 只给**路径**，不回显值。"""

    def __init__(self, paths: Iterable[str], *, where: str) -> None:
        self.paths = sorted(set(paths))
        self.where = where
        super().__init__(f"{where} 出现疑似凭据字段：{', '.join(self.paths)}")


def find_credential_paths(value: Any, *, where: str, path: str = "$", depth: int = 0) -> list[str]:
    """递归找出 ``value`` 中疑似凭据的**路径**（如 ``$.endpoint.password``）。

    深度上限防止自引用结构把校验变成无限递归；超过即停止下钻并**视为命中**——
    一个正常的 endpoint 不会嵌套 12 层，超深本身就是异常信号。
    """
    if depth > 12:
        return [path]

    hits: list[str] = []

    if isinstance(value, Mapping):
        for raw_key, item in value.items():
            key = str(raw_key)
            child = f"{path}.{key}"
            if _CREDENTIAL_KEY_RE.search(key):
                hits.append(child)
                # 命中键名后不再下钻：整个子树都不可信，报一个路径足够定位
                continue
            hits.extend(find_credential_paths(item, where=where, path=child, depth=depth + 1))
        return hits

    if isinstance(value, str):
        if _DSN_VALUE_RE.search(value) or _DSN_QUERY_KEY_RE.search(value):
            hits.append(path)
        return hits

    # list/tuple/set：用下标定位，便于前端把错误指到具体那一项
    if isinstance(value, (list, tuple, set, frozenset)):
        for index, item in enumerate(value):
            hits.extend(find_credential_paths(item, where=where, path=f"{path}[{index}]", depth=depth + 1))

    return hits


def assert_no_credential_keys(value: Any, *, where: str) -> None:
    """命中即抛 :class:`CredentialLeakError`，异常消息里**只有路径没有值**。"""
    paths = find_credential_paths(value, where=where)
    if paths:
        raise CredentialLeakError(paths, where=where)


def find_credential_values(value: Any, *, path: str = "$", depth: int = 0) -> list[str]:
    """**只**跑值侧规则，不管键名。

    ``harness.tools`` 的 args 模型需要这一半：它们的字段清单是封闭的
    （``extra="forbid"``），不可能出现 ``password`` 这样的键；
    但 ``secret_ref`` 这个**合法的**引用字段名会被键名规则命中
    （含 "secret"），一旦用它，所有带凭据引用的工具都会被自己拦住。
    这里要抓的是「把 DSN 塞进某个合法键名」的那种夹带。
    """
    if depth > 12:
        return [path]
    hits: list[str] = []
    if isinstance(value, Mapping):
        for raw_key, item in value.items():
            hits.extend(find_credential_values(item, path=f"{path}.{raw_key}", depth=depth + 1))
        return hits
    if isinstance(value, str):
        if _DSN_VALUE_RE.search(value) or _DSN_QUERY_KEY_RE.search(value):
            hits.append(path)
        return hits
    if isinstance(value, (list, tuple, set, frozenset)):
        for index, item in enumerate(value):
            hits.extend(find_credential_values(item, path=f"{path}[{index}]", depth=depth + 1))
    return hits
