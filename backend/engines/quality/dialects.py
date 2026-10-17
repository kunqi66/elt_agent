"""目标端 SQL 方言适配（be-17 §2）。

方言差异（正则函数名、``CAST`` 失败语义、文本类型名、哈希函数、字面量转义）
**只应出现在这一处**：规则编译与 SQL 渲染共用它，避免在 ``contract.py`` 里散落
``if target_type == DataSourceType.MYSQL``。新增一种目标端 = 只改本文件。

支持的取值只有 ``mysql`` / ``doris``（``DataSourceType`` 里既有的两种演示目标端）。
其它取值**显式抛错**而不是降级成 MySQL 语法：降级出来的 SQL 在生成期拼得出来、
到数据面才报语法错，而那时作业已经跑了一半、影子表里已经写进去半张表的数据。

这里的所有函数都是纯字符串拼接，不认识「行」也不认识「值」，因此没有任何数据泄露面。
"""

from __future__ import annotations

import re
from typing import Final

from engines.quality.errors import ContractError
from models.enums import DataSourceType

#: 本模块支持的目标端。写死在这里而不是从 ``DataSourceType`` 推导：
#: 枚举随时可能加新成员，而「新成员默认被支持」这句话是假的。
SUPPORTED_TARGETS: Final[tuple[DataSourceType, ...]] = (DataSourceType.MYSQL, DataSourceType.DORIS)

#: 支持中缀 ``REGEXP`` / ``NOT REGEXP`` 的目标端。
_REGEXP_CAPABLE: Final[frozenset[DataSourceType]] = frozenset(SUPPORTED_TARGETS)

#: ``CAST(x AS <文本类型>)`` 的目标类型名。
_TEXT_TYPE: Final[dict[DataSourceType, str]] = {
    DataSourceType.MYSQL: "CHAR",
    DataSourceType.DORIS: "STRING",
}

#: NULL 在 ``canon()`` 里的占位：反斜杠 + ``N`` 两个字符。
#: 不能与空串共用占位——「字段是 NULL」与「字段是空串」是两件事，
#: 混成一个占位会让「整列没取到」被读成「整列都是空值」。
NULL_CANON: Final[str] = "\\N"

#: 摘要里字段之间的分隔符：ASCII 0x1F（Unit Separator，不可见）。
#: 用逗号之类的可见字符会让 ``("ab", "c")`` 与 ``("a", "bc")`` 拼出同一个串，
#: 于是两行不同的数据得到同一个摘要——摘要失去区分能力还不报错。
FIELD_SEP_CHARCODE: Final[int] = 31

#: 摘要前缀长度（SHA256 十六进制 64 位取前 56 位）+ 8 位长度后缀 = 64 位。
DIGEST_PREFIX_LEN: Final[int] = 56
DIGEST_LEN: Final[int] = 64
#: 长度后缀的模（与 8 位十六进制同宽）：2**32。
LENGTH_MODULUS: Final[int] = 4_294_967_296

#: ``type_cast`` 的「长得像目标类型」形状表。存的是**去锚点**的内层模式，
#: 因为 strict / 非 strict 两种形态只差一个「允许首尾空白」的外壳。
_TYPECAST_INNER: Final[dict[str, str]] = {
    "tinyint": r"-?[0-9]+",
    "smallint": r"-?[0-9]+",
    "int": r"-?[0-9]+",
    "integer": r"-?[0-9]+",
    "bigint": r"-?[0-9]+",
    "decimal": r"-?[0-9]+(\.[0-9]+)?",
    "numeric": r"-?[0-9]+(\.[0-9]+)?",
    "float": r"-?[0-9]+(\.[0-9]+)?([eE][-+]?[0-9]+)?",
    "double": r"-?[0-9]+(\.[0-9]+)?([eE][-+]?[0-9]+)?",
    "date": r"[0-9]{4}-[0-9]{2}-[0-9]{2}",
    "datetime": r"[0-9]{4}-[0-9]{2}-[0-9]{2}[ T][0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]+)?",
    "timestamp": r"[0-9]{4}-[0-9]{2}-[0-9]{2}[ T][0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]+)?",
    # 文本目标类型：任何值都转得过去，因此形状永远不命中（NULL 由外层守卫管）。
    # 写进来而不是让它抛错，是因为「把字段统一 cast 成字符串」是真实存在的清洗意图。
    "char": r"[\s\S]*",
    "varchar": r"[\s\S]*",
    "string": r"[\s\S]*",
    "text": r"[\s\S]*",
}

#: ``decimal(18,2)`` / ``decimal`` / ``varchar(64)`` 这类类型文本的形状。
_TYPE_TEXT_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z]+(\s*\(\s*[0-9]+\s*(,\s*[0-9]+\s*)?\))?$")


def assert_supported(target_type: DataSourceType) -> None:
    """目标端类型必须在支持清单里。"""
    if target_type not in _REGEXP_CAPABLE:
        supported = ", ".join(t.value for t in SUPPORTED_TARGETS)
        raise ContractError(f"质量分流尚不支持的目标端类型：{target_type.value}（已支持：{supported}）")


def quote_literal(value: str) -> str:
    """SQL 单引号字符串字面量。

    编译期已经拒绝含 ``'`` 与 ``;`` 的值（be-17 §8 的注入面收敛），这里再转义一遍是
    **纵深防御**：万一哪天有人放宽了编译期校验，注入面不会跟着一起打开。
    反斜杠必须先于单引号处理，否则会把刚插入的转义符再转一遍（``\\`` → ``\\\\``）。
    """
    return "'" + value.replace("\\", "\\\\").replace("'", "''") + "'"


def regexp_match(field_expr: str, pattern: str) -> str:
    """``REGEXP(field, pattern)``：**值匹配**模式时返回真。"""
    return f"REGEXP({field_expr}, {quote_literal(pattern)})"


def regexp_not_match(field_expr: str, pattern: str) -> str:
    """``NOT REGEXP(field, pattern)``：值**不**匹配模式时返回真。

    用中缀 ``NOT REGEXP`` 而不是 ``REGEXP(...) = 0``：两种目标端都支持前者，
    而后者在 NULL 上会得到 NULL 而不是「不匹配」——NULL 在 CASE 里走 ELSE 分支，
    于是「没取到值」会被静默判成合规。
    """
    return f"NOT REGEXP({field_expr}, {quote_literal(pattern)})"


def cast_expr(field_expr: str, type_text: str) -> str:
    """``CAST(field AS <type>)``。Doris 转换失败返回 NULL，正是我们要的信号。"""
    return f"CAST({field_expr} AS {type_text})"


def text_expr(field_expr: str, target_type: DataSourceType) -> str:
    """把任意列转成文本（摘要的 ``canon()`` 用）。"""
    return cast_expr(field_expr, _TEXT_TYPE[target_type])


def trim_expr(expr: str) -> str:
    return f"TRIM({expr})"


def blank_predicate(field_expr: str) -> str:
    """「空串或全空白」。``TRIM`` 是必需的：``= ''`` 会让 ``'   '`` 蒙混过关。"""
    return f"TRIM({field_expr}) = ''"


def canonical_expr(field_expr: str, target_type: DataSourceType) -> str:
    """``canon(f)`` 的 SQL 形态（be-17 §6.3）。

    ``CASE WHEN f IS NULL THEN '\\N' ELSE TRIM(CAST(f AS CHAR/STRING)) END``

    NULL 必须显式转成占位符：``CONCAT_WS`` 会**跳过** NULL 参数，
    于是「整行都是 NULL」与「这一行只有两个字段」拼出同一个串。
    """
    text = trim_expr(text_expr(field_expr, target_type))
    return f"CASE WHEN {field_expr} IS NULL THEN {quote_literal(NULL_CANON)} ELSE {text} END"


def length_expr(expr: str) -> str:
    """字节长度。

    用 ``LENGTH`` 而不是 ``CHAR_LENGTH``：摘要是十六进制文本的拼接，
    按字节算才能让「一个中文字符」与「三个 ASCII 字符」在长度后缀上区分开。
    两种目标端的 ``LENGTH`` 都是字节语义。
    """
    return f"LENGTH({expr})"


def sha256_prefix_expr(expr: str) -> str:
    """``SUBSTRING(SHA2(expr, 256), 1, 56)``。

    SHA2 是 MySQL 的内置函数；Doris 2.1 是否有同名函数**尚无实机确认**
    （be-17 §10）。放在本函数一处是为了：真机上验出来没有时，只改这里
    （换成 UDF 或另一套摘要），而不是去改编译逻辑与已落库的报告口径。
    """
    return f"SUBSTRING(SHA2({expr}, 256), 1, {DIGEST_PREFIX_LEN})"


def lpad_hex8_expr(expr: str) -> str:
    """``LPAD(HEX(expr), 8, '0')``。``HEX()`` 回的是**大写**十六进制，见 rules.py。"""
    return f"LPAD(HEX({expr}), 8, '0')"


def concat_expr(parts: list[str]) -> str:
    return f"CONCAT({', '.join(parts)})"


def concat_ws_expr(parts: list[str]) -> str:
    sep = f"CHAR({FIELD_SEP_CHARCODE})"
    return f"CONCAT_WS({sep}, {', '.join(parts)})"


def now_expr() -> str:
    """落错误表时的时间戳。取数据面时钟而不是控制面时钟：分流发生在数据面。"""
    return "NOW()"


def base_type(type_text: str) -> str:
    """``decimal(18,2)`` → ``decimal``；顺带做类型文本的形状校验。"""
    stripped = type_text.strip()
    if not _TYPE_TEXT_RE.match(stripped):
        raise ContractError(f"type_cast 的 target_type 形状非法：{type_text!r}")
    return stripped.split("(", 1)[0].strip().lower()


def typecast_shape(type_text: str, *, strict: bool = True) -> str:
    """目标类型 → 「长得像它」的正则（带锚点）。

    ``strict=True`` 只允许精确形态；``strict=False`` 多容忍首尾空白——
    ``CAST`` 本身是容忍空白的，用严格的形状去卡它会把合法值判成违规。
    """
    base = base_type(type_text)
    inner = _TYPECAST_INNER.get(base)
    if inner is None:
        known = ", ".join(sorted(_TYPECAST_INNER))
        raise ContractError(f"type_cast 尚不支持的目标类型：{base!r}（已支持：{known}）")
    return rf"^\s*{inner}\s*$" if not strict else rf"^{inner}$"


def supported_typecast_types() -> tuple[str, ...]:
    """供契约编译报错时列出可选值。"""
    return tuple(sorted(_TYPECAST_INNER))
