"""HOCON 层的类型契约（在渲染器与解析器之前定下来，供门禁复用）。

**为什么要单独一个文件**：``render.py``（生成）与 ``parser.py``（校验）天然是一对反函数，
门禁的 ``check_syntax`` 又必须直接消费解析结果。把 ``RenderContext``/``HoconParseResult``
放在这里，渲染器、解析器、门禁三方**不互相 import**，改一边不会牵动另一边。

## 密钥令牌：为什么 HOCON 里不能出现真口令

``hocon_artifact.content`` 会**落库、进证据账本、返回给前端渲染、参与制品指纹**——
四处都是红线（CLAUDE.md §5.2）。所以渲染器**永远不写连接口令**，
只写一个可识别的令牌 ``{{secret:<data_source_id>:<field>}}``：

    url = "{{secret:1:jdbc_url}}"
    user = "{{secret:1:username}}"
    password = "{{secret:1:password}}"

这道设计的价值不在「藏起来」，而在**它可验证**，而且是**两个方向都可验证**：

- 执行前：:func:`assert_no_unresolved_tokens` —— 令牌必须一个不剩（忘了替换 =
  静默地把字面量口令当口令发出去）；
- 冻结前：:func:`secret_token_problems` —— 令牌必须**解得开**
  （指向的数据源还在、还有凭据），否则这份制品审批通过也执行不了。

两道闸判的是两件不同的事，**不能互替**：拿执行前那道去卡冻结，会把每一份合法制品
都拦下（制品本来就该带令牌）。

令牌里带 ``data_source_id`` 而不是数据源名：名字可改，id 不可改，
而 HOCON 是**不可变制品**——用名字做键的话，改个数据源名会让新旧 HOCON 的差异
看起来像「换了一台库」。
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

#: 解析器版本，写进 ``hocon_artifact.parser_version``。
#: 生成端（我们的渲染器）与校验端（pyhocon）的版本都要记——只记一半的话，
#: 「同一份 HOCON 昨天过、今天不过」时无法判断是哪一端变了。
RENDERER_VERSION = "elt-render-1"
PARSER_IMPL = "pyhocon"
PARSER_VERSION = f"{PARSER_IMPL}+{RENDERER_VERSION}"

#: 密钥令牌语法。捕获组 1 = data_source_id，2 = 字段名。
TOKEN_RE = re.compile(r"\{\{secret:(\d+):([a-z_]+)\}\}")

#: 允许被令牌化的字段。白名单而不是「任意字段」：
#: 令牌是**要被执行器替换成真值**的，能令牌化任意字段就等于给了一条
#: 「把任意字符串变成真口令」的通路。
SECRET_FIELDS = ("jdbc_url", "username", "password")


def secret_token(data_source_id: int, field: str) -> str:
    """构造密钥令牌。字段名不在白名单里当场报错，而不是渲染出一个永远不会被替换的令牌。"""
    if field not in SECRET_FIELDS:
        raise ValueError(f"不允许令牌化的字段: {field!r}（白名单 {SECRET_FIELDS}）")
    return "{{secret:" + str(int(data_source_id)) + ":" + field + "}}"


def find_tokens(text: str) -> tuple[str, ...]:
    """按出现顺序返回全文里的令牌（去重后保持首次出现序）。"""
    seen: dict[str, None] = {}
    for match in TOKEN_RE.finditer(text):
        seen.setdefault(match.group(0), None)
    return tuple(seen)


class SecretToken(BaseModel):
    """一枚已解析的令牌。冻结时要用它回答「这枚令牌到时候解得开吗」。"""

    model_config = ConfigDict(frozen=True)

    data_source_id: int
    field: str


def parse_tokens(text: str) -> tuple[SecretToken, ...]:
    """把全文里的令牌解析成结构化对象（去重、保持首次出现序）。

    ``find_tokens`` 给的是字面串，够用于「原样打回给用户看」；
    但凡是**要按令牌里的 data_source_id 去查库**的地方（冻结前的可解析性校验），
    再正则抠一遍就等于第二个解析器——第一个改了它不会跟着改。
    """
    seen: dict[tuple[int, str], None] = {}
    for match in TOKEN_RE.finditer(text):
        seen.setdefault((int(match.group(1)), match.group(2)), None)
    return tuple(SecretToken(data_source_id=ds_id, field=field) for ds_id, field in seen)


def assert_no_unresolved_tokens(text: str, *, where: str) -> None:
    """**执行前**的最后一道闸：还有令牌没被替换就拒绝提交。

    刻意**抛异常**而不是返回 bool：调用方拿 bool 迟早会写成 ``if not ok: log.warning(...)`
    然后继续往下走——而这个故障的后果是拿着字面量 ``{{secret:1:password}}`` 去连库。

    ⚠️ **不要拿它做冻结前的检查**。冻结的制品本来就该带着令牌（令牌是口令不进制品的
    手段），拿「执行前」的闸去卡「执行前很久」的一步，会把**每一份**合法制品都拦下——
    这个误用真的发生过，症状是「门禁全绿、冻结报『仍有未替换的密钥令牌』」，
    读起来像是渲染器忘了替换，实际是拿错了闸。冻结要的是
    ``secret_token_problems``（令牌到时候解得开吗）。
    """
    leftovers = find_tokens(text)
    if leftovers:
        raise ValueError(f"{where} 仍有未替换的密钥令牌 {list(leftovers)}，拒绝提交执行")


def secret_token_problems(text: str, *, resolvable_data_source_ids: frozenset[int]) -> tuple[str, ...]:
    """冻结前的检查：返回令牌里**解不开**的那些（空元组 = 全部可解析）。

    与 ``assert_no_unresolved_tokens`` 是两道不同的闸，判的是两件事：

    - 那道问「替换了没有」（执行前，令牌必须一个不剩）；
    - 这道问「到时候解得开吗」（冻结前，令牌必须一个不少地可解析）。

    两类问题都在这里报：① 字段名不在白名单（手改过制品 / 渲染器写错字）；
    ② ``data_source_id`` 在租户里没有可用凭据（数据源被删、被停用、没配密钥）。
    """
    problems: list[str] = []
    for token in parse_tokens(text):
        label = f"{{{{secret:{token.data_source_id}:{token.field}}}}}"
        if token.field not in SECRET_FIELDS:
            problems.append(f"{label} 的字段不在可令牌化白名单 {SECRET_FIELDS}")
        elif token.data_source_id not in resolvable_data_source_ids:
            problems.append(f"{label} 指向的数据源没有可用凭据（不存在 / 已停用 / 未配密钥）")
    return tuple(problems)


class RenderContext(BaseModel):
    """渲染所需的外部事实，**由 service 注入**（engines 不许自己去查库）。

    ``data_source_types`` 决定选哪个连接器模板（mysql/doris/postgres → Jdbc，minio/csv → 文件族）。
    数据源类型是外部事实而不是 Plan 的一部分：同一份 Plan 换个同类型的数据源，
    Plan 不该变——变成 Plan 字段的话，换数据源就要重新生成 Plan，等于把
    「选哪台库」（治理）和「怎么搬」（方案）搅在一起。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    data_source_types: dict[int, str] = Field(default_factory=dict)
    #: SeaTunnel 并行度。固定 1 是有意的保守默认：并行度 > 1 时 Doris/JDBC sink 的
    #: 写入顺序与 `data_save_mode=APPEND_DATA` 的组合会产生**重复行**，
    #: 而影子表 + Swap 的正确性依赖「写一次就是一次」。
    parallelism: int = Field(default=1, ge=1, le=16)
    #: 附加的非敏感连接参数（如 `serverTimezone`），由 service 从数据源档案里取非密字段。
    extra_jdbc_params: dict[int, dict[str, str]] = Field(default_factory=dict)

    def ds_type(self, data_source_id: int) -> str:
        """取数据源类型。**取不到就抛错**——回落到一个默认模板会让「连错了库类型」
        变成一份语法合法、语义错误的 HOCON（最危险的一类静默错误）。"""
        try:
            return self.data_source_types[data_source_id]
        except KeyError as exc:  # pragma: no cover - 调用方必须给全
            raise KeyError(f"渲染上下文缺少 data_source_id={data_source_id} 的类型") from exc


class RenderedHocon(BaseModel):
    """渲染产物。

    ``tokens`` 一起返回，是为了让 service 在落库前就能断言「令牌集合与预期一致」——
    从 ``content`` 里正则再抠一遍也能得到，但那是**第二个解析器**，
    第一个改了它不会跟着改。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    content: str
    tokens: tuple[str, ...] = ()
    job_name: str = ""
    renderer_version: str = RENDERER_VERSION


class HoconParseError(BaseModel):
    """一条解析错误。``line``/``column`` 从 1 起；解析器给不出位置时 ``column=1``。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    message: str
    line: int = Field(default=1, ge=1)
    column: int = Field(default=1, ge=1)


class HoconParseResult(BaseModel):
    """解析结果。门禁的 ``check_syntax`` 直接消费它，不重新解析一遍。

    ``config`` 是解析出来的**普通 dict**（HOCON 的嵌套结构），供 ``check_schema``
    判断「有没有 source/sink 段」「sink 的 table 是不是空」这类结构问题。
    它是 dict 而不是模型：HOCON 的自由度远大于我们渲染出的那几种形状，
    强类型化会把「用户手改过的 HOCON」直接判成解析失败，而这恰恰是需要报出
    **可定位错误**而不是抛异常的场景。
    """

    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    ok: bool
    errors: tuple[HoconParseError, ...] = ()
    config: dict[str, Any] | None = None
    parser_version: str = PARSER_VERSION
    kind: Literal["hocon"] = "hocon"
