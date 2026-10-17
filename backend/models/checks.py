"""列类型辅助。

``enum_col`` 是全部枚举列的唯一构造入口：落地为 **VARCHAR + CHECK**（``native_enum=False``），
而不是 PostgreSQL 原生 enum 类型——后者新增取值要 ``ALTER TYPE``，在事务与并发下很别扭。
同时在 Python 侧保留真正的枚举类型（``Mapped[RoleName]``），SQLAlchemy 会自动做值校验与转换。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from sqlalchemy import Enum as SAEnum


def enum_col(enum_cls: type[StrEnum], *, name: str, length: int = 32, **kwargs: Any) -> SAEnum:
    """构造一个落地为 VARCHAR + CHECK 的枚举列类型。

    - ``values_callable`` 让库里存的是枚举的 ``value``（如 ``checker_data``）而不是成员名，
      这样数据库里的值与 API/前端契约完全一致，排查问题时不用做心智转换。
    - ``validate_strings=True`` 使传入非法字符串时立刻报错，而不是等数据库 CHECK 拒绝。
    """
    return SAEnum(
        enum_cls,
        name=name,
        native_enum=False,
        length=length,
        validate_strings=True,
        values_callable=lambda cls: [member.value for member in cls],
        **kwargs,
    )
