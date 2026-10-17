"""两套 ``SensitiveKind`` 词表之间的翻译（回归用例）。

**这条用例是有来历的**：脱敏引擎（``engines/masking/policy.py``）与 PDP
（``engines/policy/models.py``）各有一个 ``SensitiveKind``，成员并不一样——
前者有 ``credential`` / ``free_text``，后者有 ``token`` / ``other``。
生成链路曾经把前者的值 ``str()`` 之后直接塞进 ``SensitiveColumn``，于是
「源表里有一列被判成 credential」时，整条生成在**最后一步**回 422 参数校验失败，
而图上的每一步日志都是成功的（plan 生成了、HOCON 渲染了），
错误信息里只有 ``{"errors": 1}``，看不到是哪个字段。

所以这里守两件事：
1. 脱敏词表的**每个成员**都在映射表里（将来新增类别时这条会红，提醒补映射，
   否则会静默降级成 other，判定强度悄悄变弱）；
2. 翻译结果一定能被 ``SensitiveColumn`` 接受（而不是像从前那样在运行期才炸）。
"""

from __future__ import annotations

from typing import get_args

from engines.masking.policy import SensitiveKind as MaskingKind
from engines.policy.models import (
    _MASKING_TO_POLICY_KIND,
    SensitiveColumn,
    to_policy_kind,
)
from engines.policy.models import SensitiveKind as PolicyKind
from models.enums import SensitiveLevel

_POLICY_KINDS = set(get_args(PolicyKind))


def test_脱敏词表里每个类别都有映射() -> None:
    """漏一个 = 静默降级成 other（判定变弱且不报错），所以必须逐个覆盖。"""
    # ``none`` 不在映射表里是**故意的**：它是「不敏感」，翻译结果是 None 而不是某个 kind
    missing = [kind.value for kind in MaskingKind if kind is not MaskingKind.NONE]
    missing = [value for value in missing if value not in _MASKING_TO_POLICY_KIND]
    assert missing == [], f"脱敏词表新增了类别但没补映射：{missing}"


def test_映射的目标值都在计划侧词表里() -> None:
    """映射写错一个字（比如 ``token`` 写成 ``Token``）会让校验在运行期才失败。"""
    unknown = {value for value in _MASKING_TO_POLICY_KIND.values() if value not in _POLICY_KINDS}
    assert unknown == set(), f"映射到了计划侧不存在的类别：{unknown}"


def test_凭据类翻译成_token_而不是_other() -> None:
    """``credential`` 翻错档的后果不是判错级别，而是**放走一条 P0**：
    ``token`` 在 ``ALWAYS_SENSITIVE_KINDS`` 白名单里，``other`` 不在。"""
    assert to_policy_kind(MaskingKind.CREDENTIAL) == "token"


def test_不敏感翻译成_None() -> None:
    assert to_policy_kind(MaskingKind.NONE) is None
    assert to_policy_kind(None) is None
    assert to_policy_kind("none") is None


def test_未知类别降级成_other_而不是抛错() -> None:
    """新增类别时宁可这一列降档，也不要让整次生成做不成。"""
    assert to_policy_kind("brand_new_kind") == "other"


def test_翻译结果能直接构造_SensitiveColumn() -> None:
    """这才是线上真正发生的事：``build_gate_context`` 拿翻译结果建冻结模型。"""
    for kind in [*list(MaskingKind), None, "未来的新类别"]:
        column = SensitiveColumn(
            name="phone",
            level=SensitiveLevel.CONFIDENTIAL,
            kind=to_policy_kind(kind),
            contains_plaintext=False,
            masked=True,
            egress=True,
        )
        assert column.kind is None or column.kind in _POLICY_KINDS
