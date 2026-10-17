"""从 ORM metadata 离线生成「初始建表」迁移。

**为什么要有这个脚本，而不是直接用 `alembic revision --autogenerate`**：
autogenerate 必须连上一个**已经建好表的活库**做差集——它比较的是
「metadata vs 库里现状」，空库上跑出来的正是全量建表。但本项目的开发机
没有 Docker、没有 PG（CLAUDE.md §9），空库根本不存在；等到有库的环境
（ECS）上再生成，那份迁移就只存在于那台机器上，评审时看不到。

所以这里换一条路：**直接用 PostgreSQL dialect 编译 metadata 的 DDL**，
把结果冻结进迁移文件。DDL 由 SQLAlchemy 自己的编译器产出，不是手写的，
因此和 `create_all` 走的是同一份可信来源。

**冻结而不是在 upgrade() 里调 `create_all`**：后者看着更省事，但它是
「在建表时读取当前 metadata」——等到第 2 份迁移改动某列时，全新库会先用
**新** metadata 建出**新**结构的表，再执行第 2 份迁移的 ALTER，直接撞车。
迁移必须是某个时刻的结构快照，这条路走不通。

用法（在 `backend/` 下）::

    uv run python scripts/gen_initial_migration.py            # 生成
    uv run python scripts/gen_initial_migration.py --check    # 只比对，不写文件
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# `python scripts/x.py` 只把 scripts/ 放进 sys.path，得手动补上 backend/，
# 否则 import models 失败。与 scripts/verify_ledger.py 用的是同一个办法。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import (
    AddConstraint,
    CreateIndex,
    CreateTable,
    sort_tables_and_constraints,
)

import models  # noqa: F401  导入即注册全部模型
from models.base import Base

REVISION = "0001_initial_schema"
_OUT = Path(__file__).resolve().parent.parent / "alembic" / "versions" / f"{REVISION}.py"


def build_statements() -> list[str]:
    """按依赖顺序产出建表、建索引、补外键三类 DDL。

    ``metadata.sorted_tables`` 已按外键依赖做了拓扑排序，所以直接照序执行即可，
    不需要 ``SET CONSTRAINTS`` 之类的技巧。

    **索引必须单独取**：``CreateTable`` 不含索引 DDL（索引在 PG 里是独立对象），
    漏掉这一步的表现是「表建好了、查询却慢得离谱」，且不报任何错。

    **循环外键必须单独补**：``pipeline`` 与 ``pipeline_version`` 互为外键
    （``pipeline.current_version_id`` 标了 ``use_alter=True``）。这类约束
    没法内联在建表语句里——建 ``pipeline`` 时 ``pipeline_version`` 还不存在。
    ``CreateTable`` 会（正确地）把它排除掉，**但排除之后没人负责补上**，
    于是库里就少一条外键：删除某个版本后 ``current_version_id`` 成了悬空指针，
    不报错，只是数据悄悄不一致。

    ``sort_tables_and_constraints`` 是 ``create_all`` 内部用的同一个函数，
    它把「必须延后补的约束」单独列在 ``table is None`` 的条目里；
    照它补 ALTER，生成结果才与 ``create_all`` 等价。
    """
    dialect = postgresql.dialect()
    statements: list[str] = []
    for table in Base.metadata.sorted_tables:
        statements.append(str(CreateTable(table).compile(dialect=dialect)).strip() + ";")
        for index in sorted(table.indexes, key=lambda i: i.name or ""):
            statements.append(str(CreateIndex(index).compile(dialect=dialect)).strip() + ";")

    for entry in sort_tables_and_constraints(Base.metadata.tables.values()):
        entry_table, constraints = entry
        if entry_table is None:
            for constraint in constraints:
                statements.append(str(AddConstraint(constraint).compile(dialect=dialect)).strip() + ";")
    return statements


def render(statements: list[str]) -> str:
    upgrade_body = "\n".join(f"    {_literal(s)}," for s in statements)
    # 逆序 + CASCADE：先删有外键指向别人的表，`IF EXISTS` 让半途失败的库也能清干净
    downgrade_body = "\n".join(
        f"    {_literal(f'DROP TABLE IF EXISTS {t.name} CASCADE;')},"
        for t in reversed(Base.metadata.sorted_tables)
    )
    tables = len(Base.metadata.sorted_tables)
    return f'''"""初始建表：{tables} 张表。

**本文件由 ``scripts/gen_initial_migration.py`` 生成，请勿手工编辑。**
改了 ORM 模型之后：新增一份迁移（``alembic revision --autogenerate``），
不要回头改这一份——它已经被至少一个库执行过了，改动不会再生效，
只会让「新库」和「老库」的结构不一致。

生成方式见脚本 docstring：DDL 由 PostgreSQL dialect 编译 metadata 得到，
冻结在此，不依赖生成时是否连得上库。

Revision ID: {REVISION}
Revises:
Create Date: 生成于开发机（无 Docker / 无 PG）
"""

# ruff: noqa: E501  生成的 DDL 是长字符串，换行会破坏可读性

from __future__ import annotations

from alembic import op

revision: str = "{REVISION}"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None

# 建表 + 建索引，按外键依赖拓扑排序
UPGRADE_STATEMENTS: tuple[str, ...] = (
{upgrade_body}
)

# 逆序删表
DOWNGRADE_STATEMENTS: tuple[str, ...] = (
{downgrade_body}
)


def upgrade() -> None:
    for statement in UPGRADE_STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE_STATEMENTS:
        op.execute(statement)
'''


def _literal(sql: str) -> str:
    """把一条 DDL 安全地嵌进 Python 源码。

    用 ``repr`` 而不是手写三引号：DDL 里既有单引号（CHECK 的枚举取值）
    也有反斜杠（PG 的 ``\\.`` 模式串），手写引号迟早会漏。
    """
    return repr(sql)


def main() -> int:
    parser = argparse.ArgumentParser(description="生成初始建表迁移")
    parser.add_argument(
        "--check",
        action="store_true",
        help="只比对现有文件与当前模型是否一致，不写盘（CI 用）",
    )
    args = parser.parse_args()

    content = render(build_statements())
    current = _OUT.read_text(encoding="utf-8") if _OUT.exists() else None

    if args.check:
        if current == content:
            print(f"OK: {_OUT.name} 与当前模型一致")
            return 0
        print(f"DRIFT: {_OUT.name} 与当前模型不一致，需新增迁移", file=sys.stderr)
        return 1

    if current is not None and current != content:
        # 不静默覆盖：这份迁移可能已被某个库执行过，改了它会让新库/老库结构分叉
        print(
            f"拒绝覆盖已存在的 {_OUT.name}。模型改动请用 "
            "`alembic revision --autogenerate` 新增一份迁移。",
            file=sys.stderr,
        )
        return 1

    _OUT.parent.mkdir(parents=True, exist_ok=True)
    _OUT.write_text(content, encoding="utf-8")
    n_tables = len(Base.metadata.sorted_tables)
    print(f"已写入 {_OUT.name}：{n_tables} 张表，{len(build_statements())} 条 DDL")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
