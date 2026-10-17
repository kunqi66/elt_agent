"""租户开张脚本。

**这是「能不能跑起来」的直接证据**：在没有 PG 的本机上，用同一个脚本、
同一套 service 调用，在一个空库上走完「建租户 → 建用户 → 建项目 → 发职责槽」，
并检查四个槽各归其人、首个安全槽留了痕、明文口令一个字都没落库。

跑的不是简化版：脚本里对 service 的调用（``create_project`` / ``add_member`` /
``grant_role``）与 API 路径调的是同一批函数，所以这些用例绿了，
说明那三段服务端逻辑在空库上是通的。
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import scripts.bootstrap_tenant as boot
from models.enums import Env, EvidenceEventType, RoleName
from models.evidence import EvidenceEvent
from models.tenant import AppUser, Project, ProjectMember, RoleAssignment, Tenant

TENANT = "demo"
PROJECT = "sales_wide"
PASSWORD = "pw-demo-must-not-be-stored-1234"


@pytest.fixture
def patched(monkeypatch: pytest.MonkeyPatch, async_session_factory: async_sessionmaker[AsyncSession]):
    """把脚本自己开的会话换成测试库。

    脚本内部 ``SessionFactory()`` 直接开会话（service 的事务边界归它自己），
    所以必须替换模块级名字，而不是替换 ``core.db.SessionFactory``——
    后者在导入时就已经绑定到脚本里了。
    """
    monkeypatch.setattr(boot, "SessionFactory", async_session_factory)
    return async_session_factory


async def _run(**overrides: object) -> int:
    kwargs: dict[str, object] = {
        "tenant_code": TENANT,
        "tenant_name": "演示租户",
        "project_code": PROJECT,
        "project_name": "销售宽表同步",
        "env": Env.DEV,
        "password": PASSWORD,
        "dry_run": False,
    }
    kwargs.update(overrides)
    return await boot.bootstrap(**kwargs)  # type: ignore[arg-type]


async def test_空库上开张出完整的初始状态(patched, async_session: AsyncSession) -> None:
    assert await _run() == 0

    tenant = (
        await async_session.execute(select(Tenant).where(Tenant.code == TENANT))
    ).scalar_one()
    project = (
        await async_session.execute(select(Project).where(Project.code == PROJECT))
    ).scalar_one()
    assert project.tenant_id == tenant.id

    users = (
        await async_session.execute(select(AppUser).where(AppUser.tenant_id == tenant.id))
    ).scalars().all()
    assert {u.username for u in users} == {u for u, _d, _r in boot._SEED_USERS}

    # 五个槽各归其人：这是「四眼」能被验证的前提，合并任何两个都会让某条审批路径
    # 找不到不同的两个人
    rows = (
        await async_session.execute(
            select(RoleAssignment).where(RoleAssignment.project_id == project.id)
        )
    ).scalars().all()
    by_role = {r.role: r.user_id for r in rows}
    assert set(by_role) == {
        RoleName.MAKER,
        RoleName.CHECKER_DATA,
        RoleName.CHECKER_SECURITY,
        RoleName.OPERATOR,
        RoleName.AUDITOR,
    }
    assert len(set(by_role.values())) == len(by_role), "一个槽只能给一个人"

    members = (
        await async_session.execute(
            select(ProjectMember).where(ProjectMember.project_id == project.id)
        )
    ).scalars().all()
    assert len(members) == len(boot._SEED_USERS)


async def test_职责分离按构造成立(patched, async_session: AsyncSession) -> None:
    """没有人同时持有 maker 与 checker_*。

    ``grant_role`` 里那道校验本就该拦住，这里再核一遍**结果**：
    校验被绕过（比如日后有人直接插库）时，只有查结果才能发现。
    """
    await _run()
    rows = (await async_session.execute(select(RoleAssignment))).scalars().all()
    held: dict[int, set[RoleName]] = {}
    for row in rows:
        held.setdefault(row.user_id, set()).add(row.role)

    for user_id, roles in held.items():
        if RoleName.MAKER in roles:
            assert not (roles & {RoleName.CHECKER_DATA, RoleName.CHECKER_SECURITY}), (
                f"用户 {user_id} 同时持有 maker 与 checker：{roles}"
            )


async def test_首个安全槽留痕且无人类授予人(patched, async_session: AsyncSession) -> None:
    """开张是全系统信任的起点，必须能回答「第一个安全审批人是谁任命的」。"""
    await _run()
    events = (
        await async_session.execute(
            select(EvidenceEvent).where(
                EvidenceEvent.event_type == EvidenceEventType.MEMBER_ROLE_GRANTED
            )
        )
    ).scalars().all()

    out_of_band = [e for e in events if e.payload.get("out_of_band")]
    assert len(out_of_band) == 1, "带外授予应当有且只有一条"
    event = out_of_band[0]
    # actor_id 留空而不是编一个用户：伪造「是某某批的」比留空更糟
    assert event.actor_id is None
    assert event.payload["role"] == RoleName.CHECKER_SECURITY.value
    assert event.payload["by"] == "bootstrap:cli"
    assert event.payload["operator_host"]
    assert event.payload["at"]

    # 其余四个槽都有人类授予人（首个安全槽那位）
    human = [e for e in events if not e.payload.get("out_of_band")]
    assert len(human) == 3, "checker_data / operator / auditor 各一条；maker 由建项自授"
    assert all(e.actor_id is not None for e in human)


async def test_明文口令不落任何一张表(patched, async_session: AsyncSession) -> None:
    """CLAUDE.md §5.2：口令不得出现在数据库明文列、日志、证据账本里。"""
    await _run()
    users = (await async_session.execute(select(AppUser))).scalars().all()
    for user in users:
        assert user.password_hash.startswith("$argon2"), "必须是 argon2id 哈希"
        assert PASSWORD not in user.password_hash

    for event in (await async_session.execute(select(EvidenceEvent))).scalars().all():
        assert PASSWORD not in str(event.payload), "口令不能随证据事件落账"


async def test_已开张的租户拒绝再跑(patched) -> None:
    """否则它就成了绕过 ``can_manage_roles`` 的后门。"""
    await _run()
    with pytest.raises(SystemExit, match="已开张"):
        await _run()


async def test_dry_run不写库(patched, async_session: AsyncSession) -> None:
    assert await _run(dry_run=True) == 0
    assert (await async_session.execute(select(Tenant))).scalars().all() == []


async def test_重跑能补齐被清掉的槽(patched, async_session: AsyncSession) -> None:
    """开张中途失败（槽没发完）后重跑，应当收敛到目标状态而不是报错。

    这里手工模拟「槽丢了、用户和项目还在」：这正是一次中途失败留下的样子。
    脚本第一版会在这种情形下**漏掉 maker**——因为 maker 只在「建项目」那一步
    自授，而项目已经存在，那一步就被跳过了。存在性检查 + 统一补发修掉了它。
    """
    await _run()
    for row in (await async_session.execute(select(RoleAssignment))).scalars().all():
        await async_session.delete(row)
    await async_session.commit()

    assert await _run() == 0
    rows = (await async_session.execute(select(RoleAssignment))).scalars().all()
    assert {r.role for r in rows} == {
        RoleName.MAKER,
        RoleName.CHECKER_DATA,
        RoleName.CHECKER_SECURITY,
        RoleName.OPERATOR,
        RoleName.AUDITOR,
    }
