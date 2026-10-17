"""敏感字段判定与脱敏（纯函数，be-04）。

**脱敏默认开启，而且没有「关掉」这个选项**（CLAUDE.md §5.7）。本模块提供的是
「脱到什么程度」，不是「脱不脱」——一个可以被关闭的脱敏开关，在真实项目里
迟早会因为某次排查被临时关掉，然后再也没打开。

三条实现约束：

1. **不可逆**。脱敏值 = 加盐哈希的前 N 位，不保留任何可还原的变换（如掩码中间的星号
   配合长度就能缩小搜索空间）。要让人看懂「这是同一个值」，看哈希前缀即可；
   要还原，本系统不提供这个能力。
2. **同值同显**。同一列同一个原值，脱敏结果必须一致——否则「按手机号分组统计」
   这类演示场景会得到一堆长度为一的组，看起来很假。所以盐按列固定。
3. **长度不泄漏**。截断到固定长度，不保留原值长度。
"""

from __future__ import annotations

import hashlib
import re
from enum import StrEnum
from typing import Any

from models.enums import SensitiveLevel

# 脱敏值统一取哈希前 12 个十六进制字符（48 bit）。
# 再短会撞（百万行样本里出现重复就没有分辨力），再长没有必要（它不是标识符，只是显示值）
_DIGEST_LEN = 12


class SensitiveKind(StrEnum):
    """字段的敏感类别。决定用什么规则判定，也决定脱敏文案怎么起头。"""

    NAME = "name"
    PHONE = "phone"
    EMAIL = "email"
    ID_CARD = "id_card"
    BANK_CARD = "bank_card"
    ADDRESS = "address"
    CREDENTIAL = "credential"
    FREE_TEXT = "free_text"
    NONE = "none"


# 列名 → (类别, 敏感级别)。顺序敏感：先匹配到的先赢，
# 所以更具体的规则要排在更宽泛的前面（如 "id_card" 必须排在 "id" 之前）。
_NAME_RULES: tuple[tuple[re.Pattern[str], SensitiveKind, SensitiveLevel], ...] = (
    (
        re.compile(r"(id[_]?card|identity|证件|身份证)", re.I),
        SensitiveKind.ID_CARD,
        SensitiveLevel.RESTRICTED,
    ),
    (
        re.compile(r"(bank[_]?card|card[_]?no|账号|卡号)", re.I),
        SensitiveKind.BANK_CARD,
        SensitiveLevel.RESTRICTED,
    ),
    (
        re.compile(r"(password|passwd|pwd|secret|token|api[_]?key|口令|密钥)", re.I),
        SensitiveKind.CREDENTIAL,
        SensitiveLevel.RESTRICTED,
    ),
    (
        re.compile(r"(phone|mobile|tel|手机|电话|联系方式)", re.I),
        SensitiveKind.PHONE,
        SensitiveLevel.CONFIDENTIAL,
    ),
    (
        re.compile(r"(email|mail|邮箱|邮件)", re.I),
        SensitiveKind.EMAIL,
        SensitiveLevel.CONFIDENTIAL,
    ),
    (
        re.compile(r"(address|addr|street|地址|住址)", re.I),
        SensitiveKind.ADDRESS,
        SensitiveLevel.CONFIDENTIAL,
    ),
    (
        re.compile(r"(name|姓名|联系人|客户名)", re.I),
        SensitiveKind.NAME,
        SensitiveLevel.CONFIDENTIAL,
    ),
    (
        re.compile(r"(remark|comment|note|desc|备注|说明|描述)", re.I),
        SensitiveKind.FREE_TEXT,
        SensitiveLevel.INTERNAL,
    ),
)

# 数字/字符串类型也能提供线索，但只作为兜底：列名说不是敏感，就不敏感。
# 反过来（靠类型推断）会把所有 varchar 都变成可疑字段，等于没有判定。
_LEVEL_ORDER = {
    SensitiveLevel.PUBLIC: 0,
    SensitiveLevel.INTERNAL: 1,
    SensitiveLevel.CONFIDENTIAL: 2,
    SensitiveLevel.RESTRICTED: 3,
}


def infer_sensitivity(
    column_name: str, *, comment: str | None = None, declared: SensitiveLevel | None = None
) -> tuple[SensitiveKind, SensitiveLevel]:
    """按列名（和注释）判定敏感类别与级别。

    ``declared`` 是人工在数据源上显式标注的级别，优先于规则——
    规则只能猜，「业务上这个字段就是不能给运营看」只有人知道。
    取两者**较高**的那个，而不是让人工标注覆盖规则：
    覆盖意味着一次误标就能把手机号降级成 public，而取较高者最坏是过于保守。
    """
    kind = SensitiveKind.NONE
    level = SensitiveLevel.INTERNAL
    haystack = f"{column_name} {comment or ''}"
    for pattern, rule_kind, rule_level in _NAME_RULES:
        if pattern.search(haystack):
            kind, level = rule_kind, rule_level
            break

    if declared is not None and _LEVEL_ORDER[declared] > _LEVEL_ORDER[level]:
        # 人工标注更高：跟随它，但类别仍按列名（类别决定脱敏形态，级别决定要不要脱）
        level = declared
    return kind, level


def digest(value: str, *, column: str, salt: str) -> str:
    """加盐哈希的前 ``_DIGEST_LEN`` 位。

    盐按**列**固定而不是全局：全局盐会让「手机号列」与「备注列」里
    同一条内容产生同一个摘要，跨列关联就成立了；
    而按列加盐既保留列内可分辨性，又切断了跨列关联。
    """
    payload = f"{salt}\x1f{column}\x1f{value}".encode()
    return hashlib.sha256(payload).hexdigest()[:_DIGEST_LEN]


def mask_value(
    value: Any,
    *,
    column: str,
    kind: SensitiveKind,
    level: SensitiveLevel,
    salt: str,
    keep_public_plain: bool = True,
) -> str:
    """把单个值变成可展示、不可还原的字符串。

    形态刻意统一成 ``类别:摘要``（如 ``phone:a3f19c04b27e``）：
    看的人能一眼知道这列是什么，但看不到值，也推不出长度。
    """
    if value is None:
        return ""
    text = str(value)
    if level is SensitiveLevel.PUBLIC and keep_public_plain:
        # public 列原样保留——脱敏不是「全部打码」，那会让样本失去演示价值。
        # 但**保留与否由策略决定**（``merge_policy`` 允许把它收紧成 False），
        # 所以这里读参数而不是写死 True：写死会让策略里那句 keep_public_plain=false 变成谎话
        return text
    prefix = kind.value if kind is not SensitiveKind.NONE else "masked"
    return f"{prefix}:{digest(text, column=column, salt=salt)}"


def mask_rows(
    rows: list[dict[str, Any]],
    *,
    columns: dict[str, tuple[SensitiveKind, SensitiveLevel]],
    salt: str,
    keep_public_plain: bool = True,
) -> list[dict[str, str]]:
    """批量脱敏。

    ``columns`` 必须显式给出每一列的判定结果，而不是在这里现算：
    同一张表的每一行必须用同一套判定，否则会出现「第一行的 name 脱了、
    第二行的 name 没脱」——而那看起来像是脱敏随机失效。
    """
    masked: list[dict[str, str]] = []
    for row in rows:
        masked.append(
            {
                key: mask_value(
                    value,
                    column=key,
                    kind=columns.get(key, (SensitiveKind.NONE, SensitiveLevel.INTERNAL))[0],
                    level=columns.get(key, (SensitiveKind.NONE, SensitiveLevel.INTERNAL))[1],
                    salt=salt,
                    keep_public_plain=keep_public_plain,
                )
                for key, value in row.items()
            }
        )
    return masked


def default_masking_policy() -> dict[str, Any]:
    """默认策略。存进 ``metadata_profile.masking_policy``，让「当时按什么规则脱的」可追溯。"""
    return {
        "version": 1,
        "mode": "hash_prefix",
        "digest_len": _DIGEST_LEN,
        # 明确记下 public 不脱：审计看到某个字段是明文时，
        # 要能分清「策略这么定的」与「脱敏漏了」
        "keep_public_plain": True,
        "rules": [
            {"pattern": p.pattern, "kind": k.value, "level": lvl.value} for p, k, lvl in _NAME_RULES
        ],
    }


def merge_policy(override: dict[str, Any] | None) -> dict[str, Any]:
    """把调用方给的策略合进默认策略。

    **只允许更严，不允许更松**：``keep_public_plain`` 若被改成 false 是收紧（可以），
    但除此之外没有「把 confidential 降成 public」的入口——
    脱敏策略如果能由请求体放松，那它就是可选的，而红线要求它是默认且不可关的。
    """
    policy = default_masking_policy()
    if not override:
        return policy
    if override.get("keep_public_plain") is False:
        policy["keep_public_plain"] = False
    # 记下调用方提交了什么，便于审计对账（但不据此放松规则）
    policy["requested"] = {k: v for k, v in override.items() if k != "keep_public_plain"}
    return policy


def narrow(value: Any) -> Any:
    """把值压成 JSON 可存、且**不含长文本**的形状。

    超长文本本身就可能是一整段业务数据（备注、日志），把它整个写进
    ``schema_snapshot`` 等于把样本换了个地方存。截断到 128 字符。
    """
    if value is None or isinstance(value, (int, float, bool)):
        return value
    text = str(value)
    return text[:128]


__all__ = [
    "SensitiveKind",
    "default_masking_policy",
    "digest",
    "infer_sensitivity",
    "mask_rows",
    "mask_value",
    "merge_policy",
    "narrow",
]
