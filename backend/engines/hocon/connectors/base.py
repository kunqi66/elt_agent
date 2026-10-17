"""连接器模板的契约与 HOCON 文本原语（be-10 §6.5）。

**为什么 key_order 必须是模板里写死的字面量**：§6.3 规则 1 要求「键顺序来自模板的
声明顺序」。只要有一处改成遍历参数字典（``for k, v in params.items()``），顺序就会
退化成「Plan 里字段的书写顺序」或「dict 插入序」——两者都不是契约，同一份 Plan 换个
构造路径就可能换一份字节，而 HOCON 全文参与制品指纹（§6.7）。所以顺序是**代码里的
一行字面量**，不是从数据里推出来的。

**为什么未声明的键要抛错**：CLAUDE.md §9.1 记着同一个坑——``ToolBroker`` 的 detail
白名单会**静默丢弃**未知键，于是「连通性明明成功却显示 failed」。渲染器同理：
某个键被静默丢掉，得到的 HOCON 语法合法、语义少了一项配置，而且没有任何信号。
宁可在渲染期抛错，也不产出「少了一条的配置」。

**为什么 required_keys 与 key_order 分开**：``key_order`` 是**输出顺序表**，
``required_keys`` 是**必填项**。两者的关系是 ``required_keys ⊆ key_order``
（顺序表漏了必填键 = 必填项永远不渲染；这句由单测守着）。
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Literal, Protocol

from engines.hocon.types import RenderContext

#: 插件在 HOCON 里扮演的角色。同一个 ds_type 在 source 与 sink 下可以是**不同插件**
#: （MinIO 做源是 ``S3File``、做目标是 ``LocalFile``），所以注册表的键是 (类型, 角色)。
ConnectorKind = Literal["source", "sink"]

#: HOCON 值的取值域。``dict`` 表示嵌套块（如 ``properties { ... }``）。
HoconScalar = str | int | bool | Decimal
HoconValue = HoconScalar | list[HoconScalar] | dict[str, HoconScalar]

#: §6.3 规则 2：缩进 2 空格。
INDENT = "  "

#: §6.3 规则 4：``Decimal`` 保留 4 位。用 ``Decimal`` 而不是 ``float``：
#: ``0.1 + 0.2`` 那类二进制噪声会让「同一份预算」算出两种文本。
_SCALE4 = Decimal("0.0001")


class UnknownConnectorError(ValueError):
    """``ds_type`` 没有对应模板。**不许回落默认模板**：那会把「连错了库类型」
    变成一份语法合法、语义错误的 HOCON——最危险的一类静默错误。
    """


def quote(value: str) -> str:
    """字符串字面量（§6.3 规则 4）。

    含换行的值（内层 SQL）优先用 ``\"\"\"...\"\"\"``：pyhocon 的三引号走的是**原样**分支
    （不经过转义表），所以 SQL 里的 ``\\`` 与 ``%`` 不必二次转义，CodeMirror 里也仍然是
    可读的多行。仅当值自身含 ``\"\"\"`` 或以 ``"`` 结尾时才退回 ``\\n`` 转义的单行形态——
    那两种情况会让三引号提前闭合（前者是语法歧义，后者是 pyparsing 的 ``"*`` 回溯吃掉
    结尾引号）。
    """
    if "\n" in value and '"""' not in value and not value.endswith('"'):
        return '"""' + value + '"""'
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return '"' + escaped + '"'


def render_value(value: HoconValue) -> str:
    """单个标量/数组 → HOCON 文本（§6.3 规则 4、5）。"""
    # bool 必须在 int 之前判断：Python 里 bool 是 int 的子类，顺序反了会渲染成 1/0
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, Decimal):
        return str(value.quantize(_SCALE4))
    if isinstance(value, str):
        return quote(value)
    if isinstance(value, list | tuple):
        # §6.3 规则 5：数组保持原序，**不排序**（顺序即语义）
        return "[" + ", ".join(render_value(item) for item in value) + "]"
    raise TypeError(f"不支持的 HOCON 值类型: {type(value).__name__}")


def render_key(key: str, value: HoconValue, indent: int) -> str:
    """``key = value`` 或 ``key { ... }``，按 ``indent`` 缩进。"""
    pad = INDENT * indent
    if isinstance(value, dict):
        # 嵌套块内的键序没有「Plan 原序」可依（它来自用户参数表，没有顺序契约），
        # 按名字排序是这里唯一能给出的确定来源。
        inner = "\n".join(render_key(k, value[k], indent + 1) for k in sorted(value))
        return f"{pad}{key} {{\n{inner}\n{pad}}}"
    return f"{pad}{key} = {render_value(value)}"


class ConnectorTemplate(Protocol):
    """连接器模板（be-10 §6.5）。

    仓库现状的偏离：契约里**没有** ``validate()``。设计文档 §6.5 让它返回
    ``list[HoconIssue]``，但本仓库的类型契约（``engines/hocon/types.py``）里没有
    ``HoconIssue``——必填项/插件合法性属于门禁的 ``check_schema``（be-11），
    由它消费 ``HoconParseResult.config`` 判定。模板这一侧只负责**渲染**，
    不该长出第二套问题列表。
    """

    plugin_name: str
    kind: ConnectorKind
    key_order: tuple[str, ...]
    required_keys: frozenset[str]
    #: 需要令牌化（而非字面量）的键。渲染器据此决定写入 ``{{secret:...}}`` 还是普通值。
    secret_keys: tuple[str, ...]

    def render(self, params: Mapping[str, HoconValue], ctx: RenderContext, *, indent: int = 0) -> str:
        """渲染 ``PluginName { ... }`` 块。``indent`` 是插件块自身的缩进层级。"""
        ...


class BlockTemplate:
    """按 ``key_order`` 输出键的通用模板。

    子类**只声明常量、不写渲染逻辑**：一旦某个模板自己拼字符串，确定性就散落在各处，
    「改一处缩进 = 制品指纹全线漂移」这件事再也守不住。
    """

    plugin_name: str = ""
    kind: ConnectorKind = "source"
    key_order: tuple[str, ...] = ()
    required_keys: frozenset[str] = frozenset()
    secret_keys: tuple[str, ...] = ()

    def render(self, params: Mapping[str, HoconValue], ctx: RenderContext, *, indent: int = 0) -> str:
        """渲染 ``PluginName { ... }`` 块。``ctx`` 供子类扩展，本实现不需要它。"""
        missing = sorted(key for key in self.required_keys if key not in params)
        if missing:
            raise ValueError(f"{self.plugin_name} 缺少必填参数 {missing}")
        unknown = sorted(set(params) - set(self.key_order))
        if unknown:
            raise ValueError(
                f"{self.plugin_name} 出现未声明的参数 {unknown}；"
                "要么改 key_order（会改变制品指纹），要么去掉它——静默丢弃是不允许的"
            )
        pad = INDENT * indent
        body = "\n".join(render_key(key, params[key], indent + 1) for key in self.key_order if key in params)
        if not body:  # 无参数的插件块：`Name {` 与 `}` 之间不留空行
            return f"{pad}{self.plugin_name} {{\n{pad}}}"
        return f"{pad}{self.plugin_name} {{\n{body}\n{pad}}}"
