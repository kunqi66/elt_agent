"""初始迁移与 ORM 模型的一致性。

**这些用例守的是一件事**：那份迁移是**冻结**的 DDL 快照（生成脚本见
``scripts/gen_initial_migration.py``）。它一旦被手工编辑，就可能出现
「新库按迁移建出来的结构」与「测试里 `create_all` 建出来的结构」不一致——
测试全绿，而生产库里少了一张表或一个索引。这类差异不会报错，只会让某个
查询慢、或某条唯一约束不存在，等到线上出问题才被发现。

本文件**不连数据库**：只比对 SQL 文本与 metadata，所以本地无 PG 也能跑。
至于「PG 是否接受这份 DDL」，靠 `alembic upgrade head --sql` 的离线渲染 +
PG 语法解析器验证（见模块文档）。
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from types import ModuleType

import models  # noqa: F401  导入即注册全部模型
from models.base import Base

_VERSIONS_DIR = Path(__file__).resolve().parent.parent / "alembic" / "versions"


def _load_migration() -> ModuleType:
    """按文件路径加载迁移模块。

    ``alembic/versions/`` 不是 Python 包（alembic 自己也是按文件路径执行的），
    所以用 importlib 显式加载，而不是 `import versions.xxx`。
    """
    files = sorted(_VERSIONS_DIR.glob("0001_*.py"))
    assert files, f"初始迁移不存在：{_VERSIONS_DIR}"
    spec = importlib.util.spec_from_file_location("migration_0001", files[0])
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_all_migrations() -> list[tuple[ModuleType, str]]:
    """**全部**迁移：``(模块, 源码文本)``，按文件名排序（= 按 revision 链顺序）。

    ``0001`` 之后的迁移会合法地增删 DDL（例如 ``frozen_at`` 改可空 + 部分唯一索引），
    所以「索引集合与模型一致」必须在**整条链**上成立，而不是只看初始快照。
    只看 0001 的写法会在第二次迁移落地时立刻变红，而红的原因不是缺 DDL——
    是这条断言问错了对象。想核对初始快照本身用 ``MIGRATION``（下面那个）。

    **为什么要连源码一起返回**：只有 0001 是手写的、带 ``UPGRADE_STATEMENTS`` 文本元组；
    之后的迁移是 alembic 自动生成的 ``op.create_index(...)`` 调用，**没有**那份文本。
    所以索引名要从源码里正则取——这不是偷懒，是因为「迁移文件本身」才是事实，
    再去连库渲染一遍 SQL 会让这条用例从「本地无 PG 也能跑」变成需要 PG。
    """
    modules: list[tuple[ModuleType, str]] = []
    for path in sorted(_VERSIONS_DIR.glob("*.py")):
        spec = importlib.util.spec_from_file_location(f"migration_{path.stem}", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        modules.append((module, path.read_text(encoding="utf-8")))
    assert modules, f"一个迁移都没有：{_VERSIONS_DIR}"
    return modules


MIGRATION = _load_migration()
ALL_MIGRATIONS = _load_all_migrations()

#: 整条链上的全部手写 DDL 语句（只有 0001 有这种文本形态）。
_ALL_UPGRADE_STATEMENTS = [
    stmt
    for module, _source in ALL_MIGRATIONS
    for stmt in getattr(module, "UPGRADE_STATEMENTS", ())
]

#: ``CREATE [UNIQUE] INDEX <name>``——从手写语句里取索引名。
_TEXT_INDEX = re.compile(r"CREATE (?:UNIQUE )?INDEX (\w+)", re.IGNORECASE)
#: ``op.create_index('name', ...)`` / ``op.create_index("name", ...)``——从自动生成的迁移里取。
_OP_INDEX = re.compile(r"op\.create_index\(\s*[\"'](\w+)[\"']")
#: ``op.drop_index('name', ...)``——整条链上被删掉的索引（建了又删的不该再算数）。
_OP_DROP_INDEX = re.compile(r"op\.drop_index\(\s*[\"'](\w+)[\"']")


def _created_tables() -> set[str]:
    return {
        match.group(1)
        for stmt in _ALL_UPGRADE_STATEMENTS
        if (match := re.match(r"CREATE TABLE (\w+)", stmt))
    }


def _created_index_names() -> set[str]:
    """整条链建出来、且**没有被后续迁移删掉**的索引名。

    比的是**名字集合**而不是条数：条数相等但建错了一个，条数断言照样绿——
    而「少建了那个部分唯一索引、多建了一个普通索引」正是最容易发生的走样。
    """
    created: set[str] = set()
    dropped: set[str] = set()
    for module, source in ALL_MIGRATIONS:
        created |= set(_TEXT_INDEX.findall("\n".join(getattr(module, "UPGRADE_STATEMENTS", ()))))
        # 只看 ``upgrade()`` 那一段：同一份文件里的 ``downgrade()`` 会把本文件建的东西再删一遍，
        # 不切开会得出「建了又删 = 没建」——而 0001 的 downgrade 会把**所有**索引 drop 掉，
        # 于是整条链的索引集合变成空集
        upgrade_body = source.split("def downgrade", 1)[0]
        created |= set(_OP_INDEX.findall(upgrade_body))
        dropped |= set(_OP_DROP_INDEX.findall(upgrade_body))
    return created - dropped


def test_每个模型表都出现在迁移里() -> None:
    """漏一张表 = 该表在真库里根本不存在，而 `create_all` 的测试察觉不到。"""
    expected = set(Base.metadata.tables)
    actual = _created_tables()
    assert actual == expected, f"迁移缺: {expected - actual}；迁移多: {actual - expected}"


def test_索引集合与模型一致() -> None:
    """索引是独立 DDL，`CreateTable` 不包含它们——漏掉的表现是「不报错但查询极慢」。

    部分唯一索引（``uq_pipeline_version_one_candidate``）漏掉的后果更重：
    它是「一个 pipeline 最多一行候选」的**唯一**兜底，代码里的「先查后建」有并发窗口。
    """
    expected = {index.name for table in Base.metadata.tables.values() for index in table.indexes}
    actual = _created_index_names()
    assert actual == expected, f"迁移缺: {sorted(expected - actual)}；迁移多: {sorted(actual - expected)}"


def test_不产生原生枚举类型() -> None:
    """枚举列一律落地为 VARCHAR + CHECK（models/checks.py 的 enum_col）。

    一旦有人给某列写了 `native_enum=True`，这里会变成 CREATE TYPE，
    而 PG 的枚举新增取值要 ALTER TYPE——在事务与并发下很别扭（enum_col 的 docstring 有说明）。
    """
    assert not any("CREATE TYPE" in stmt for stmt in MIGRATION.UPGRADE_STATEMENTS)


def test_降级覆盖全部表且带CASCADE() -> None:
    """少一张表的 downgrade 会让 `alembic downgrade base` 半途失败。"""
    drops = MIGRATION.DOWNGRADE_STATEMENTS
    assert len(drops) == len(Base.metadata.tables)
    assert all("DROP TABLE IF EXISTS" in stmt and "CASCADE" in stmt for stmt in drops)
    assert {re.search(r"DROP TABLE IF EXISTS (\w+)", s).group(1) for s in drops} == set(  # type: ignore[union-attr]
        Base.metadata.tables
    )


def _table_order() -> dict[str, int]:
    return {
        match.group(1): i
        for i, stmt in enumerate(MIGRATION.UPGRADE_STATEMENTS)
        if (match := re.match(r"CREATE TABLE (\w+)", stmt))
    }


def test_每条外键都被建出来() -> None:
    """外键要么内联在建表语句里，要么用 ALTER 补上——**一条都不能少**。

    这条守的是 ``pipeline.current_version_id`` 那种**循环外键**（标了
    ``use_alter=True``）：``CreateTable`` 会（正确地）把它排除掉，因为建
    ``pipeline`` 时 ``pipeline_version`` 还不存在。排除之后如果没人补 ALTER，
    库里就少一条外键——不报错，只是删版本后 ``current_version_id`` 悬空。
    生成脚本第一版就是这样，被这条用例挡下来的。
    """
    total = sum(len(t.foreign_key_constraints) for t in Base.metadata.tables.values())
    inline = sum(stmt.count("REFERENCES") for stmt in MIGRATION.UPGRADE_STATEMENTS if stmt.startswith("CREATE TABLE")
    )
    alter = sum(1 for stmt in MIGRATION.UPGRADE_STATEMENTS if re.match(r"ALTER TABLE \w+ ADD CONSTRAINT", stmt))
    assert inline + alter == total, f"模型 {total} 条外键，DDL 里内联 {inline} + 补 ALTER {alter}"


def test_内联外键的目标表先建() -> None:
    """内联外键引用的表必须已经建好。

    顺序错了的表现是 `alembic upgrade head` 在真库上报
    `relation "xxx" does not exist`——本地无 PG，所以只能在这里静态守住。
    标了 ``use_alter`` 的外键不参与本检查：它们本就该走 ALTER。
    """
    order = _table_order()
    for table in Base.metadata.tables.values():
        for fkc in table.foreign_key_constraints:
            if fkc.use_alter:
                continue
            target = next(iter(fkc.elements)).column.table.name
            if target == table.name:  # 自引用
                continue
            assert order[target] < order[table.name], (
                f"{table.name} 建在它依赖的 {target} 之前"
            )


def test_延后外键补在所有建表之后() -> None:
    """ALTER 补外键必须排在最后——它引用的表得先存在。"""
    alter_positions = [
        i for i, s in enumerate(MIGRATION.UPGRADE_STATEMENTS) if s.startswith("ALTER TABLE")
    ]
    assert alter_positions, "模型里有 use_alter 外键，却没有对应的 ALTER 语句"
    assert min(alter_positions) > max(_table_order().values())


def test_迁移链头是无前驱的() -> None:
    assert MIGRATION.down_revision is None
    assert MIGRATION.revision == "0001_initial_schema"
