"""职责槽的纯判定函数。

本模块是**纯函数层**：不 import fastapi、不 import core.errors、不碰 DB/Redis/网络。
违规一律抛 ``ValueError``（含可解释中文原因），由调用方（service 层）翻译成业务异常并写证据账本。

为什么判定要独立成纯函数而不是散在 service 里：
「谁不能审批谁」这条规则同时出现在**授予职责槽**（be-02）与**审批受理**（be-13）两个入口，
各写一份的结果是——某天只改了其中一处，于是 grant 时拒绝的组合在审批时被放行。
判定只有一份实现，两个入口都调它。
"""

from __future__ import annotations

from collections.abc import Set as AbstractSet
from datetime import datetime

from models.enums import RoleName

# 与 maker 互斥的职责槽：同一项目内，做过需求的人不能同时做验收人。
# operator / auditor 刻意不在表内——执行与审计不与 maker/checker 构成利益冲突，
# 把它们也塞进来只会让「一个人兼执行与审计」变成不可能，反而没人用。
_MAKER_MUTEX: frozenset[RoleName] = frozenset({RoleName.CHECKER_DATA, RoleName.CHECKER_SECURITY})


def _require_aware(value: datetime, *, field: str) -> None:
    """时间必须带时区。

    裸 ``datetime`` 会被当成「本地时间」参与比较，而库里存的是 ``timestamptz``。
    同一个槽位在容器（UTC）与开发机（UTC+8）上算出**不同**的有效性，
    且两边都自洽——这类 bug 只能靠构造测试用例才发现，所以在入口直接拒绝。
    """
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} 必须是带时区的时间（收到裸 datetime）")


def is_assignment_valid(valid_from: datetime | None, valid_to: datetime | None, *, at: datetime) -> bool:
    """职责槽在 ``at`` 时刻是否有效。

    ``valid_to`` 是**开区间上界**：``at == valid_to`` 视为**已失效**。
    与 ``[valid_from, valid_to)`` 的直觉一致，也让「临时授权到 12:00」在 12:00 整点准时结束。
    """
    _require_aware(at, field="at")
    if valid_from is not None:
        _require_aware(valid_from, field="valid_from")
        if valid_from > at:
            return False
    if valid_to is not None:
        _require_aware(valid_to, field="valid_to")
        if valid_to <= at:
            return False
    return True


def has_role(roles: AbstractSet[RoleName], role: RoleName) -> bool:
    return role in roles


def check_duty_separation(held: AbstractSet[RoleName], *, granting: RoleName) -> None:
    """授予 ``granting`` 前校验是否与 ``held`` 构成职责混用。

    | 待授予 | 现有 | 结果 |
    |---|---|---|
    | maker | 含任一 checker_* | 拒绝 |
    | checker_data / checker_security | 含 maker | 拒绝 |
    | checker_data | 含 checker_security | **允许**——两槽可由不同人担任，同单不得同人归 be-13 判定 |
    | operator / auditor | 任意 | 允许 |
    """
    if granting is RoleName.MAKER:
        conflict = held & _MAKER_MUTEX
        if conflict:
            raise ValueError(
                f"职责冲突：已持有 {sorted(r.value for r in conflict)}，不能再授予 maker"
                "（需求提出人与验收人必须分离）"
            )
        return

    if granting in _MAKER_MUTEX and RoleName.MAKER in held:
        raise ValueError(f"职责冲突：已持有 maker，不能再授予 {granting.value}（需求提出人与验收人必须分离）")


def check_dual_checker_distinct(data_checker: int, security_checker: int) -> None:
    """两个审批槽不得由同一人担任。

    与 ``check_duty_separation`` 的分工：后者管「一个人不能既做 maker 又做 checker」，
    本条管「两个 checker 槽之间也不许合并」。少了这一条，同一个 checker_security
    可以把自己算成数据槽，四眼退化成两眼。
    """
    if data_checker == security_checker:
        raise ValueError("四眼失效：数据审批槽与安全审批槽不能由同一人担任")


def is_self_approval(preparation_created_by: int, approver_id: int) -> bool:
    """申请人不得审批自己提交的准备单。"""
    return preparation_created_by == approver_id


# ---------------- 能力位 ----------------
# 「持有哪个槽才配管成员/发角色」只此一份：API 层拿它算 `can_manage_*` 下发给前端，
# service 层拿它做真正的拦截。两处各写一份的结果是——界面按钮亮着、后端却 403，
# 或者反之，界面以为不能点而后端其实放行。

_MEMBER_MANAGERS: frozenset[RoleName] = frozenset({RoleName.MAKER, RoleName.OPERATOR})
_ROLE_MANAGERS: frozenset[RoleName] = frozenset({RoleName.CHECKER_SECURITY})


def can_manage_members(roles: AbstractSet[RoleName]) -> bool:
    """maker 能拉人（他要提需求就得先有人），operator 能拉人（他要执行）。"""
    return bool(roles & _MEMBER_MANAGERS)


def can_manage_roles(roles: AbstractSet[RoleName]) -> bool:
    """只有 checker_security 能发角色。

    刻意不给 maker：让需求提出人自己指定验收人，职责分离就只是纸面约定。
    """
    return bool(roles & _ROLE_MANAGERS)


# ---------------- 数据源 ----------------

_DATASOURCE_MANAGERS: frozenset[RoleName] = frozenset(
    {RoleName.MAKER, RoleName.OPERATOR, RoleName.CHECKER_SECURITY}
)


def can_manage_datasource(roles: AbstractSet[RoleName]) -> bool:
    """谁能建/改/停用数据源与重设凭据。

    maker 要提需求就得先登记源库，operator 要跑作业就得维护目标库，
    checker_security 要看住凭据的流向。**auditor 刻意不在内**：
    他的职责是事后核证，能改配置就无法保证「他看到的账本与当时的事实一致」。
    """
    return bool(roles & _DATASOURCE_MANAGERS)


def can_read_datasource(roles: AbstractSet[RoleName]) -> bool:
    """能看一眼数据源清单：任何在本项目持有职责槽的人。

    而 `checker_data` / `auditor` 只读不写——他们的价值恰恰在于
    「没参与过配置，所以能中立地核」。所以读权限比写权限宽，是有意的。
    """
    return bool(roles)


# ---------------- 证据账本 ----------------


def can_read_ledger(roles: AbstractSet[RoleName]) -> bool:
    """读证据账本（be-06 §3.1：auditor 只读）。

    **刻意不放开给 maker / operator**：账本记录的是「谁在什么时候批了什么」，
    而他们正是被记录的对象。只读本身不算越权，但查询接口能按 ``event_type`` /
    ``subject_id`` 检索，放开就等于给了一个「翻查同事操作记录」的入口——
    那不是审计员工作台该有的形状。auditor 的职责恰恰是事后核证，所以只有他。

    这是**比数据源读权限更窄**的一条：那边的理由是「他们没参与配置，能中立地核」，
    这里多一层「被记录者不参与检索」。
    """
    return has_role(roles, RoleName.AUDITOR)


# ---------------- 流程与生成（be-09 / be-11 / be-12） ----------------

_PIPELINE_MANAGERS: frozenset[RoleName] = frozenset({RoleName.MAKER, RoleName.OPERATOR})


def can_manage_pipeline(roles: AbstractSet[RoleName]) -> bool:
    """谁能生成 / 改 / 提交审批 / 停止一个流程。

    **checker 与 auditor 刻意不在内**，而且这里的理由比数据源那条硬得多：

    - 两个 checker 槽（数据 / 安全）的全部价值在于「**没写过这份方案**」。
      让他能改 Plan、能跑自动修复，就等于让他能把一份有问题的方案修成
      「看起来能过」的样子再自己批——CLAUDE.md §5.8 的四眼约束在服务端拦的是
      「审批人 ≠ 发起人」，而**发起人身份是冻结时记录的**：checker 若能改草案，
      他改的正是那份即将被记成「maker 发起」的东西。那是绕过四眼的一条侧门。
    - auditor 同理：能改制品的人不该同时是核证制品的人。

    这不是空泛的「权限收窄换安全」：checker 想参与生成，得先退出审批槽
    （``check_duty_separation`` 会拦下同时持有 maker 与 checker 的授予）。
    """
    return bool(roles & _PIPELINE_MANAGERS)


def can_read_pipeline(roles: AbstractSet[RoleName]) -> bool:
    """能看流程列表 / Plan / HOCON / 门禁结论：本项目里**任何**持有职责槽的人。

    比写权限宽，与 :func:`can_read_datasource` 同一个口径：checker 不看着方案就没法批，
    auditor 不看着制品就没法事后核证。读的边界由租户与项目划、**不靠角色划**——
    按角色藏内容会让审批人看到一份「字段不全」的方案，然后照着它批了。
    """
    return bool(roles)
