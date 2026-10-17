"""ORM 基类与混入。

**不可变制品由 ORM 层强制**（CLAUDE.md §5.4）：凡 ``__immutable__ = True`` 的模型，
在任何 Session 上被 UPDATE 或 DELETE 都会抛 ``ImmutableViolationError``。
这是一道「写代码的人忘了」的兜底——修正只能产生新版本，不能改历史。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, Identity, Integer, MetaData, event, func, inspect
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

# 本机单测跑内存 SQLite（CLAUDE.md §9：本机无 Docker）。SQLite 只把 `INTEGER PRIMARY KEY`
# 当 rowid 别名来自增，`BIGINT PRIMARY KEY` 不会——建表能过、插入直接 NOT NULL 失败。
# with_variant 仅在 sqlite dialect 下生效，生产 DDL 依旧是 BIGINT。
AutoPK = BigInteger().with_variant(Integer, "sqlite")

# 统一约束命名：Alembic autogenerate 稳定 diff 的前提
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class ImmutableViolationError(RuntimeError):
    """试图修改或删除不可变制品。"""
    """试图修改或删除不可变制品。"""


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    # 子类置 True 即受不可变保护（pipeline_version / preparation / capability / evidence_event ...）
    __immutable__: bool = False

    # 例外：不可变制品上**允许**变化的少量列。
    # 例：preparation.status 随审批推进、capability.consumed_at 标记消费。
    # 其余列一旦写入即冻结——这正是「审批绑定指纹」能成立的前提。
    __mutable_columns__: frozenset[str] = frozenset()

    # 例外机制之二：**冻结列**。设了它，白名单只在「冻结列还是空」的那段时间生效。
    # 冻结列一旦落值，整行连白名单里的列都不可改——白名单描述的是「草案期可以改什么」，
    # 不是「永远可以改什么」。
    # 例：pipeline_version 的候选行（frozen_at IS NULL）要能反复重生成、自动修复来回改；
    # 一旦 frozen 落值，它就成了绑定指纹的不可变制品，连 note 都不许再改。
    __freeze_column__: str | None = None


class PKMixin:
    id: Mapped[int] = mapped_column(AutoPK, Identity(), primary_key=True)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class CreatedAtMixin:
    """不可变表专用：只有创建时间，没有 updated_at（结构上就不允许「修改时间」）。"""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class TenantMixin:
    tenant_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)


class ProjectScopedMixin(TenantMixin):
    project_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)


@event.listens_for(Session, "before_flush")
def _guard_immutable(session: Session, _flush_context: object, _instances: object) -> None:
    """在任何 flush 之前拦截对不可变实体的修改与删除。

    只放行 ``__mutable_columns__`` 白名单内的列；**逐列**判断而不是整体判断，
    因此报错信息能直接指出是哪个字段被非法修改，排障不用猜。
    """
    for obj in session.deleted:
        cls = type(obj)
        if getattr(cls, "__immutable__", False):
            raise ImmutableViolationError(
                f"{cls.__name__} 是不可变制品，禁止删除（应改为新增版本或标记状态）"
            )

    for obj in session.dirty:
        cls = type(obj)
        if not getattr(cls, "__immutable__", False):
            continue
        allowed: frozenset[str] = getattr(cls, "__mutable_columns__", frozenset())
        state = inspect(obj)
        freeze_col = getattr(cls, "__freeze_column__", None)
        if freeze_col is not None and _is_frozen(obj, state, freeze_col):
            # 已冻结：白名单失效。**不能**只判「本次改动里有没有 freeze_col」——
            # 冻结是单向的，判断依据必须是「库里已经存的值」，否则同一个事务里
            # 先写 frozen_at 再改别的列就能绕过守卫。
            allowed = frozenset()
        changed = {attr.key for attr in state.attrs if attr.history.has_changes()}
        illegal = changed - set(allowed) - {"updated_at"}
        if illegal:
            raise ImmutableViolationError(
                f"{cls.__name__} 是不可变制品，禁止修改列 {sorted(illegal)}（只允许改 {sorted(allowed)}）"
            )


def _is_frozen(obj: object, state: Any, freeze_col: str) -> bool:
    """该行**在库里**是否已冻结。

    ``history.deleted`` 非空表示本次事务里这个列刚被改过——若它的旧值是 ``None``，
    说明这正是「本次把它冻结」的那次写入，必须放行（否则冻结动作自己都做不了）。
    """
    attr = state.attrs.get(freeze_col)
    if attr is None:
        return False
    if attr.history.deleted:
        return attr.history.deleted[0] is not None
    return getattr(obj, freeze_col, None) is not None
