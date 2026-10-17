"""规则求值与摘要的 **Python 参考实现**（be-17 §3.2）。

它们不是运行路径上的实现——判定与摘要在数据面（SeaTunnel 的 SQL transform + 目标端）
完成。这里存在的理由只有两个：

1. **单测黄金向量**：SQL 那一侧在单测里跑不起来（本机没有 Doris / MySQL），
   于是「同一行数据算出什么摘要」只能靠一份可断言的口径钉住；
2. **L1 假执行器**：Benchmark L1 用假执行器模拟分流，与真链路口径必须一致。

两条纪律：

- 摘要**不进控制面运行路径**：``error_record.payload_digest`` 一律从目标端**原样拷贝**，
  不用本模块重算。两套算法并存本身不是问题，**两套算法都各自被当成权威**才是问题。
- 本实现只在「ASCII 或 UTF-8」值上与 SQL 同语义：摘要的长度后缀按**字节**算
  （两种目标端的 ``LENGTH()`` 都是字节），哈希输入按 UTF-8 编码。
  布尔列的文本形态在两种方言上不同（MySQL ``1`` / Doris ``true``），
  因此**显式拒绝**而不是猜一个——猜错的症状是摘要对不上而不报错。
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from engines.quality import dialects
from engines.quality.contract import CompiledRule


def canonical_field(value: Any) -> str:
    """``canon(f)`` 的 Python 形态（be-17 §6.3）：NULL → ``\\N``；字符串去首尾空白。"""
    if value is None:
        return dialects.NULL_CANON
    if isinstance(value, bool):
        raise TypeError(
            "布尔列的 canon 在两种目标端上不同（MySQL CAST 得 '1'、Doris 得 'true'），"
            "参考实现不猜：请改用字符串/数字列做黄金向量"
        )
    if isinstance(value, datetime):
        return f"{value:%Y-%m-%d %H:%M:%S}.{value.microsecond // 1000:03d}"
    if isinstance(value, date):
        return f"{value:%Y-%m-%d}"
    if isinstance(value, (int, Decimal, float)):
        return str(value)
    if isinstance(value, str):
        return value.strip()
    raise TypeError(f"canon 不支持的类型：{type(value).__name__}")


def canonicalize_row(row: Mapping[str, Any], fields: Sequence[str]) -> str:
    """一行的规范化文本：``canon(f1) \\x1f canon(f2) …``。

    分隔符用 0x1F（与 SQL 侧的 ``CHAR(31)`` 同一口径）：换成逗号会让
    ``("ab", "c")`` 与 ``("a", "bc")`` 撞成同一个串。
    """
    return "\x1f".join(canonical_field(row.get(field)) for field in fields)


def payload_digest(row: Mapping[str, Any], fields: Sequence[str]) -> str:
    """``SHA256(canon) 前 56 位 + LPAD(HEX(Σ 字节长度), 8, '0')``，恰好 64 位。

    长度后缀是**大写**十六进制：MySQL 的 ``HEX()`` 回大写，参考实现用 ``:08X`` 对齐它。
    写成小写的话，「SQL 算的摘要」与「单测断言的字面量」会差 64 个字符中的一个，
    而且只在长度后缀里——这种差异肉眼几乎看不出来，所以这里刻意与 SQL 对齐而不是求好看。
    """
    parts = [canonical_field(row.get(field)) for field in fields]
    body = "\x1f".join(parts)
    total = sum(len(part.encode("utf-8")) for part in parts) % dialects.LENGTH_MODULUS
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[: dialects.DIGEST_PREFIX_LEN] + f"{total:08X}"


def digest_content_length(digest: str) -> int:
    """从摘要里解出「字段总长度」的后 8 位（诊断用它区分被截断的行）。"""
    if len(digest) != dialects.DIGEST_LEN:
        raise ValueError(f"摘要长度必须是 {dialects.DIGEST_LEN}，收到 {len(digest)}")
    return int(digest[dialects.DIGEST_PREFIX_LEN :], 16)


def evaluate_rule(rule: CompiledRule, row: Mapping[str, Any]) -> bool:
    """单条规则对一行的判定，**True == 违规**（与 SQL 谓词同一语义）。

    ``regex`` 用 ``re.search`` 而不是 ``match``：SQL 的 ``REGEXP`` 判的是「串里有没有
    一处匹配」，``^…$`` 是模式自己带的锚。用 ``match`` 会让
    ``'abc123' REGEXP '\\d'`` 这类模式在两边得到相反结论。
    """
    value = row.get(rule.field)

    if rule.rule_type == "not_null":
        if value is None:
            return True
        if rule.params.get("allow_blank"):
            return isinstance(value, str) and value.strip() == ""
        return False

    if value is None:
        # 与 SQL 侧的 ``f IS NOT NULL AND …`` 守卫同一语义：空值的处置归 not_null 规则
        return False

    if rule.rule_type == "regex":
        text = str(value)
        matched = re.search(str(rule.params.get("pattern", "")), text) is not None
        return matched if rule.params.get("expect") == "not_match" else not matched

    if rule.rule_type == "range":
        number = _as_decimal(value)
        if number is None:
            # 与 SQL 的隐式转换同方向：转不动的值交 not_null / type_cast 管，
            # 在区间规则里报违规会把一条类型问题伪装成一条取值问题
            return False
        lower = rule.params.get("min")
        if lower is not None:
            bound = Decimal(str(lower))
            if number < bound or (not rule.params.get("inclusive_min", True) and number == bound):
                return True
        upper = rule.params.get("max")
        if upper is not None:
            bound = Decimal(str(upper))
            if number > bound or (not rule.params.get("inclusive_max", False) and number == bound):
                return True
        return False

    if rule.rule_type == "enum":
        return str(value) not in {str(item) for item in rule.params.get("values", [])}

    if rule.rule_type == "type_cast":
        shape = dialects.typecast_shape(
            str(rule.params.get("target_type", "")), strict=bool(rule.params.get("strict", True))
        )
        return re.fullmatch(shape, str(value)) is None

    # 编译期已经把 rule_type 收进五个取值；走到这里说明传进来的不是编译产物
    return False


def _as_decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() else None
