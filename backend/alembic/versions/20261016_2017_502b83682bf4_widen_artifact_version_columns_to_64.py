"""widen artifact version columns to 64

Revision ID: 502b83682bf4
Revises: 813dec0bb4bb
Create Date: 2026-10-16 20:17:11.050504
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "502b83682bf4"
down_revision: str | None = "813dec0bb4bb"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """把两个版本号列从 varchar(16) 放宽到 varchar(64)。

    起因是一个真实的 500：``engines.hocon.PARSER_VERSION`` 的值是
    ``pyhocon+elt-render-1``（20 字符），写进 ``hocon_artifact.parser_version`` 时
    超长（SQLSTATE 22001），而它发生在**整条链路最后一步**——图跑完了、门禁判完了、
    响应却 500，前面对应的日志全是成功的。16 这个宽度对「版本号」本就偏小：
    版本串为了能定位「是哪一端变了」会越写越长，放宽是无损的。
    """

    op.alter_column(
        "etl_plan",
        "schema_version",
        existing_type=sa.VARCHAR(length=16),
        type_=sa.String(length=64),
        existing_nullable=False,
    )
    op.alter_column(
        "hocon_artifact",
        "parser_version",
        existing_type=sa.VARCHAR(length=16),
        type_=sa.String(length=64),
        existing_nullable=False,
    )


def downgrade() -> None:
    """回退到 varchar(16)。

    ⚠️ 只对「库里所有版本串都 ≤ 16 字符」的库安全；否则这一步会因为超长而失败——
    这正是它当初的样子，失败是诚实的报错，不要为了「回退得顺」去截断数据。
    """

    op.alter_column(
        "hocon_artifact",
        "parser_version",
        existing_type=sa.String(length=64),
        type_=sa.VARCHAR(length=16),
        existing_nullable=False,
    )
    op.alter_column(
        "etl_plan",
        "schema_version",
        existing_type=sa.String(length=64),
        type_=sa.VARCHAR(length=16),
        existing_nullable=False,
    )
