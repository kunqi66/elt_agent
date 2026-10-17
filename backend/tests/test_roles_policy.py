"""职责槽纯函数的单测（无 IO、无 DB）。

这些用例守的是**四眼**这条不可协商的红线：一个断言写反了，演示时就会放行一次自批，
而界面上看不出任何异常。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from engines.policy.roles import (
    check_dual_checker_distinct,
    check_duty_separation,
    has_role,
    is_assignment_valid,
    is_self_approval,
)
from models.enums import RoleName

NOW = datetime(2026, 10, 16, 12, 0, 0, tzinfo=UTC)


# ---------------- is_assignment_valid ----------------
def test_无界槽位始终有效() -> None:
    assert is_assignment_valid(None, None, at=NOW) is True


def test_valid_to_是开区间_整点即失效() -> None:
    """`at == valid_to` 必须为**无效**：临时授权到 12:00，12:00 整点就该结束。"""
    assert is_assignment_valid(None, NOW, at=NOW) is False
    assert is_assignment_valid(None, NOW + timedelta(seconds=1), at=NOW) is True


def test_valid_from_是闭区间_起点即生效() -> None:
    assert is_assignment_valid(NOW, None, at=NOW) is True
    assert is_assignment_valid(NOW + timedelta(seconds=1), None, at=NOW) is False


def test_裸时间被拒绝() -> None:
    """裸 datetime 会在容器(UTC)与开发机(UTC+8)上算出不同结论，且两边都自洽——入口就拒。"""
    with pytest.raises(ValueError, match="带时区"):
        is_assignment_valid(None, None, at=datetime(2026, 10, 16, 12, 0, 0))


# ---------------- check_duty_separation ----------------
def test_maker_与任一_checker_互斥() -> None:
    for checker in (RoleName.CHECKER_DATA, RoleName.CHECKER_SECURITY):
        with pytest.raises(ValueError, match="职责冲突"):
            check_duty_separation({checker}, granting=RoleName.MAKER)


def test_已持_maker_时不能再授_checker() -> None:
    for checker in (RoleName.CHECKER_DATA, RoleName.CHECKER_SECURITY):
        with pytest.raises(ValueError, match="职责冲突"):
            check_duty_separation({RoleName.MAKER}, granting=checker)


def test_两个_checker_槽之间允许兼任() -> None:
    """checker_data × checker_security 允许——同单不得同人由 be-13 的 check_dual_checker_distinct 管。"""
    check_duty_separation({RoleName.CHECKER_SECURITY}, granting=RoleName.CHECKER_DATA)
    check_duty_separation({RoleName.CHECKER_DATA}, granting=RoleName.CHECKER_SECURITY)


def test_operator_与_auditor_不与任何角色互斥() -> None:
    all_roles = set(RoleName)
    for role in (RoleName.OPERATOR, RoleName.AUDITOR):
        for held in all_roles:
            if held is role:
                continue
            check_duty_separation({held}, granting=role)
        check_duty_separation(all_roles - {role}, granting=role)


def test_授予已持有的同一角色不报错() -> None:
    """重复授予由 UK 约束拦，不该在互斥判定这里报「职责冲突」——那会把 409 说成 403。"""
    check_duty_separation({RoleName.MAKER}, granting=RoleName.MAKER)


# ---------------- check_dual_checker_distinct ----------------
def test_同人担任两个审批槽被拒() -> None:
    with pytest.raises(ValueError, match="四眼失效"):
        check_dual_checker_distinct(7, 7)


def test_不同人担任两个审批槽通过() -> None:
    check_dual_checker_distinct(7, 8)


# ---------------- is_self_approval ----------------
def test_自批判定() -> None:
    assert is_self_approval(3, 3) is True
    assert is_self_approval(3, 4) is False


# ---------------- has_role ----------------
def test_has_role() -> None:
    assert has_role({RoleName.MAKER}, RoleName.MAKER) is True
    assert has_role(set(), RoleName.MAKER) is False
