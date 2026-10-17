"""be-06 证据账本：查询、游标分页、验链、导出。

**本文件的核心断言有两句**：

1. **拒绝读账本这件事本身要入账**（be-06 §7 第 6 行）。审计日志被越权查询，
   正是审计员最需要看到的事件；只回 403 而没留痕，事后查不出「谁试过翻账本」。
2. **验链的结论必须来自重算，且断链不能变成 HTTP 错误**。
   前者用「改一行 payload、哈希不动」来证伪——如果实现是比对存储的
   ``event_hash``，那个用例会绿，而这正是最需要抓到的一种篡改。

分层：schema 与游标是纯函数，直接调；service 需要 auditor 角色，
用 ORM 直接铺 ``ProjectMember`` + ``RoleAssignment``（不走 ``grant_role``：
授予本身需要 checker_security，那是 be-02 的用例，在这里铺一遍会把
本文件的失败原因和权限模块的 bug 搅在一起）。

⚠️ **断言一律相对 ``_bootstrap`` 之后的账本状态**，不写死 ``seq == 1``：
建项目本身就会落一条 ``project.created``，链首不是测试数据的第一条。
写死绝对值时用例会绿在一个错误的前提上（「账本里只有我塞的这几条」），
而那个前提一改（比如 create_project 将来多落一条事件）就全线飘红。
"""

from __future__ import annotations

import csv
import io
import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from core.errors import RoleRequiredError, ValidationError
from harness.ledger import EvidenceLedger, LedgerEvent
from models.enums import EvidenceEventType, MemberStatus, RoleName
from models.evidence import EvidenceEvent, EvidenceLedgerBlock
from models.tenant import AppUser, Project, ProjectMember, RoleAssignment, Tenant
from schemas.auth import SubjectContext
from schemas.governance import EvidenceFilter, VerifyRequest
from schemas.project import ProjectCreate
from services import governance as gov
from services import project as project_service

_ledger = EvidenceLedger()
_T0 = datetime(2026, 10, 16, 8, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------- 种子


async def _user(session: AsyncSession, tenant_id: int, username: str) -> AppUser:
    user = AppUser(
        tenant_id=tenant_id,
        username=username,
        display_name=username.title(),
        email=f"{username}@acme.test",
        password_hash="x",
        status="active",
    )
    session.add(user)
    await session.flush()
    return user


def _ctx(user: AppUser, *, request_id: str = "req_1") -> SubjectContext:
    return SubjectContext(
        user_id=user.id,
        tenant_id=user.tenant_id,
        username=user.username,
        display_name=user.display_name,
        request_id=request_id,
    )


async def _grant(session: AsyncSession, project_id: int, user: AppUser, role: RoleName) -> None:
    member = ProjectMember(project_id=project_id, user_id=user.id, status=MemberStatus.ACTIVE)
    session.add(member)
    await session.flush()
    session.add(
        RoleAssignment(
            project_id=project_id,
            member_id=member.id,
            user_id=user.id,
            role=role,
            granted_by=user.id,
            reason="测试",
        )
    )
    await session.flush()


class _Seed:
    """一次用例的账本现场：项目、三个用户、以及链上已经存在的事件。"""

    def __init__(
        self, *, tenant_id: int, project_id: int, maker: AppUser, auditor: AppUser, outsider: AppUser
    ) -> None:
        self.tenant_id = tenant_id
        self.project_id = project_id
        self.maker = maker
        self.auditor = auditor
        self.outsider = outsider

    async def seed(
        self,
        session: AsyncSession,
        count: int,
        *,
        subject_type: str = "preparation",
        subject_id_base: int = 100,
    ) -> list[int]:
        """按顺序落 ``count`` 条事件，返回它们的 seq。"""
        events = [
            LedgerEvent(
                tenant_id=self.tenant_id,
                project_id=self.project_id,
                actor_id=self.maker.id,
                event_type=EvidenceEventType.APPROVAL_GRANTED,
                subject_type=subject_type,
                subject_id=str(subject_id_base + idx),
                payload={"idx": idx},
                occurred_at=_T0 + timedelta(minutes=idx),
            )
            for idx in range(count)
        ]
        rows = await _ledger.append_many(session, events)
        await session.commit()
        return [int(r.seq) for r in rows]

    async def all_seqs(self, session: AsyncSession) -> list[int]:
        """本项目在链上的全部 seq（含建项目时落的那条）。"""
        rows = (
            await session.execute(
                select(EvidenceEvent.seq)
                .where(
                    EvidenceEvent.tenant_id == self.tenant_id,
                    EvidenceEvent.project_id == self.project_id,
                )
                .order_by(EvidenceEvent.seq.asc())
            )
        ).scalars()
        return [int(s) for s in rows]


async def _bootstrap(session: AsyncSession) -> _Seed:
    """租户 + 项目 + maker（项目创建人）/ auditor / outsider（无任何项目成员身份）。"""
    tenant = Tenant(code="acme", name="Acme", status="active")
    session.add(tenant)
    await session.flush()

    maker = await _user(session, tenant.id, "maker")
    auditor = await _user(session, tenant.id, "auditor")
    outsider = await _user(session, tenant.id, "outsider")
    ctx = _ctx(maker)
    project = await project_service.create_project(
        session, ctx, ProjectCreate(code="orders", name="订单", env="dev")
    )
    await _grant(session, project.id, auditor, RoleName.AUDITOR)
    await session.commit()
    return _Seed(
        tenant_id=tenant.id,
        project_id=project.id,
        maker=maker,
        auditor=auditor,
        outsider=outsider,
    )


# ---------------------------------------------------------------- schema 与游标


def test_两种主体过滤不能同时给出() -> None:
    """两者都按 subject 过滤，而一条事件的 subject 只有一个。

    报错比返回空列表好：空列表看起来像「确实没有这类记录」。
    """
    with pytest.raises(ValueError, match="不能同时使用"):
        EvidenceFilter(project_id=1, pipeline_version_id=7, execution_run_id=9)


def test_subject_id_与结构化过滤不能同时给出() -> None:
    with pytest.raises(ValueError, match="不能同时使用"):
        EvidenceFilter(project_id=1, subject_id="preparation:1", pipeline_version_id=7)


def test_时间区间反了就拒绝() -> None:
    with pytest.raises(ValueError, match="occurred_from"):
        EvidenceFilter(
            project_id=1,
            occurred_from=datetime(2026, 1, 2, tzinfo=UTC),
            occurred_to=datetime(2026, 1, 1, tzinfo=UTC),
        )


def test_验链区间反了就拒绝() -> None:
    with pytest.raises(ValueError, match="to_seq"):
        VerifyRequest(project_id=1, from_seq=5, to_seq=4)


def test_project_id_必填() -> None:
    """前端注释写明「缺省查询会被后端拒绝」——不带项目过滤等于一个跨项目操作日志接口。"""
    with pytest.raises(ValueError):
        EvidenceFilter()  # type: ignore[call-arg]


def test_游标往返一致() -> None:
    assert gov.decode_cursor(gov.encode_cursor(42)) == 42


@pytest.mark.parametrize("bad", ["42", "", "ev:", "ev:abc", "xx:1", "ev:-1"])
def test_游标必须是不透明串(bad: str) -> None:
    """让客户端能构造游标，就意味着「从任意位置开始翻」会被当成合法输入，
    而它跳过的那些事件不会有人注意到。
    """
    with pytest.raises(ValidationError):
        gov.decode_cursor(bad)


def test_未知断链类型不透给前端() -> None:
    """前端遇到不认识的字符串会走 default 分支显示成「未知错误」，
    看起来像它漏了一个 case。宁可只说「断了」，具体原因走 broken_detail。
    """
    assert gov._public_kind("hash_mismatch") == "hash_mismatch"
    assert gov._public_kind("something_new") is None
    assert gov._public_kind(None) is None


# ---------------------------------------------------------------- 权限


async def test_auditor_能读账本(async_session: AsyncSession) -> None:
    seed = await _bootstrap(async_session)
    await seed.seed(async_session, 3)

    page = await gov.list_evidence(
        async_session, _ctx(seed.auditor), EvidenceFilter(project_id=seed.project_id)
    )
    assert [item.seq for item in page.items] == await seed.all_seqs(async_session)


async def test_maker_读账本被拒且拒绝本身入账(async_session: AsyncSession) -> None:
    """be-06 §7 第 6 行：``ROLE_REQUIRED`` 且**写账本**。

    这条断言的价值在于「被拒之后账本多了一条」——只断言抛异常，
    一个从不写账本的实现也能通过。
    """
    seed = await _bootstrap(async_session)
    await seed.seed(async_session, 1)

    with pytest.raises(RoleRequiredError):
        await gov.list_evidence(
            async_session, _ctx(seed.maker), EvidenceFilter(project_id=seed.project_id)
        )

    rows = (
        (
            await async_session.execute(
                select(EvidenceEvent)
                .where(EvidenceEvent.event_type == EvidenceEventType.LEDGER_ACCESS_DENIED)
                .order_by(EvidenceEvent.seq.asc())
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].actor_id == seed.maker.id
    assert rows[0].project_id == seed.project_id


async def test_无项目成员身份者读账本被拒(async_session: AsyncSession) -> None:
    """tenant 对但项目里没有任何职责槽 —— 与「租户不对」是两回事，
    两条都要走到 ``RoleRequiredError``，而不是漏成 500。
    """
    seed = await _bootstrap(async_session)
    with pytest.raises(RoleRequiredError):
        await gov.list_evidence(
            async_session, _ctx(seed.outsider), EvidenceFilter(project_id=seed.project_id)
        )


async def test_验链同样只对_auditor_开放(async_session: AsyncSession) -> None:
    seed = await _bootstrap(async_session)
    with pytest.raises(RoleRequiredError):
        await gov.verify_ledger(
            async_session, _ctx(seed.maker), VerifyRequest(project_id=seed.project_id)
        )


# ---------------------------------------------------------------- 查询


async def test_跨租户事件不可见(async_session: AsyncSession) -> None:
    """租户条件一律带（CLAUDE.md §4.7）——本项目里有一条别租户的事件，
    它不能出现在结果里。
    """
    seed = await _bootstrap(async_session)

    other = Tenant(code="globex", name="Globex", status="active")
    async_session.add(other)
    await async_session.flush()
    other_user = await _user(async_session, other.id, "mallory")
    await _ledger.append(
        async_session,
        LedgerEvent(
            tenant_id=other.id,
            project_id=seed.project_id,  # 同一个 project_id，不同租户
            actor_id=other_user.id,
            event_type=EvidenceEventType.APPROVAL_GRANTED,
            subject_type="preparation",
            subject_id="999",
            payload={},
            occurred_at=_T0,
        ),
    )
    await async_session.commit()
    await seed.seed(async_session, 2)

    page = await gov.list_evidence(
        async_session, _ctx(seed.auditor), EvidenceFilter(project_id=seed.project_id)
    )
    # 结果恰好是「本项目全部事件」——那条 project_id 相同但租户不同的记录不在里面。
    # 若查询漏了 tenant_id 条件，它会插在中间而这条断言会失败
    assert [item.seq for item in page.items] == await seed.all_seqs(async_session)
    assert all(item.tenant_id == seed.tenant_id for item in page.items)


async def test_游标分页不漏不重(async_session: AsyncSession) -> None:
    """账本持续 append，offset 分页会漏读或重读。

    断言的是**并集与顺序**，而不是「每页长度」——后者在实现「每次都返回第一页」
    时也是绿的。
    """
    seed = await _bootstrap(async_session)
    await seed.seed(async_session, 5)

    ctx = _ctx(seed.auditor)
    seen: list[int] = []
    cursor: str | None = None
    for _ in range(20):  # 上限防死循环：游标不前进时会在这里挂掉而不是永不返回
        page = await gov.list_evidence(
            async_session,
            ctx,
            EvidenceFilter(project_id=seed.project_id, limit=2, cursor=cursor),
        )
        seen.extend(item.seq for item in page.items)
        cursor = page.next_cursor
        if cursor is None:
            break
    assert seen == await seed.all_seqs(async_session)
    assert cursor is None


async def test_整页正好取完时游标为空(async_session: AsyncSession) -> None:
    """恰好取满一页时必须判断为「到底了」。

    实现若靠「返回条数 == limit」判断，这里会是 None 还是游标取决于运气；
    正确做法是多取一条探路（本用例就是为那个 +1 而写的）。
    """
    seed = await _bootstrap(async_session)
    await seed.seed(async_session, 2)
    total = len(await seed.all_seqs(async_session))

    page = await gov.list_evidence(
        async_session, _ctx(seed.auditor), EvidenceFilter(project_id=seed.project_id, limit=total)
    )
    assert len(page.items) == total
    assert page.next_cursor is None


async def test_没有命中记录时返回空页(async_session: AsyncSession) -> None:
    """空结果不是错误，但游标必须是 None——给一个游标会让前端以为还有下一页，
    于是它会拿着这个游标再问一次，永远问不完。
    """
    seed = await _bootstrap(async_session)
    await seed.seed(async_session, 2)

    page = await gov.list_evidence(
        async_session,
        _ctx(seed.auditor),
        # 同样夹住上界：只给下界时，本用例的成立与否取决于「真实时钟有没有走到
        # _T0 + 365 天」——那是个一年后才引爆的同类定时炸弹（fixture 的建项目事件用真实时间）。
        EvidenceFilter(
            project_id=seed.project_id,
            occurred_from=_T0 + timedelta(days=365),
            occurred_to=_T0 + timedelta(days=366),
        ),
    )
    assert page.items == []
    assert page.next_cursor is None


async def test_按事件类型过滤(async_session: AsyncSession) -> None:
    seed = await _bootstrap(async_session)
    await seed.seed(async_session, 2)
    run_seqs = await seed.seed(async_session, 1, subject_type="run", subject_id_base=7)
    # 把这条改成 run.failed，好与上面两条 approval.granted 区分开
    await async_session.execute(
        update(EvidenceEvent)
        .where(EvidenceEvent.seq == run_seqs[0])
        .values(event_type=EvidenceEventType.RUN_FAILED)
    )
    await async_session.commit()

    page = await gov.list_evidence(
        async_session,
        _ctx(seed.auditor),
        EvidenceFilter(project_id=seed.project_id, event_type=[EvidenceEventType.RUN_FAILED]),
    )
    assert [item.event_type for item in page.items] == ["run.failed"]


async def test_带前缀的_subject_id_按前缀拆开(async_session: AsyncSession) -> None:
    """前端字段注释写的是「形如 ``preparation:42``」，
    所以这条必须真的按前缀匹配，而不是把整串当 subject_id 去比。
    """
    seed = await _bootstrap(async_session)
    await seed.seed(async_session, 3)  # preparation:100..102
    run_seqs = await seed.seed(async_session, 1, subject_type="run", subject_id_base=100)

    page = await gov.list_evidence(
        async_session,
        _ctx(seed.auditor),
        EvidenceFilter(project_id=seed.project_id, subject_id="run:100"),
    )
    assert [item.seq for item in page.items] == run_seqs


async def test_结构化主体过滤等价于_subject_id(async_session: AsyncSession) -> None:
    seed = await _bootstrap(async_session)
    await seed.seed(async_session, 3, subject_type="pipeline_version")

    by_id = await gov.list_evidence(
        async_session,
        _ctx(seed.auditor),
        EvidenceFilter(project_id=seed.project_id, pipeline_version_id=101),
    )
    by_subject = await gov.list_evidence(
        async_session,
        _ctx(seed.auditor),
        EvidenceFilter(project_id=seed.project_id, subject_id="pipeline_version:101"),
    )
    assert len(by_id.items) == 1
    assert [i.seq for i in by_id.items] == [i.seq for i in by_subject.items]


async def test_按发生时间过滤(async_session: AsyncSession) -> None:
    seed = await _bootstrap(async_session)
    seqs = await seed.seed(async_session, 3)

    page = await gov.list_evidence(
        async_session,
        _ctx(seed.auditor),
        # 上界不能省：``_bootstrap`` 里的 ``create_project`` 会落一条账本事件，
        # 而它走的是服务代码、用**真实当前时间**（``utcnow()``）。
        # 只给下界的话，那条事件的时刻与 ``_T0`` 的大小关系取决于**跑测试的钟点**——
        # 真实时钟一旦越过 ``_T0 + 1min``，它就会混进结果，本用例会在某个时刻突然变红
        # （实测：2026-10-16 16:01 CST 起必红，此前一直是绿的）。
        # 两侧都夹住之后，那条事件无论在哪个时间点跑都被排除在外。
        EvidenceFilter(
            project_id=seed.project_id,
            occurred_from=_T0 + timedelta(minutes=1),
            occurred_to=_T0 + timedelta(minutes=3),
        ),
    )
    assert [item.seq for item in page.items] == seqs[1:]


async def test_操作者姓名一次查出(async_session: AsyncSession) -> None:
    """``actor_id`` 是外键且可空（用户注销后置 NULL），
    两种情形都要给出可直接渲染的字符串，而不是让前端处理 null。
    """
    seed = await _bootstrap(async_session)
    await seed.seed(async_session, 1)
    system_seq = (
        await _ledger.append(
            async_session,
            LedgerEvent(
                tenant_id=seed.tenant_id,
                project_id=seed.project_id,
                actor_id=None,  # 系统动作
                event_type=EvidenceEventType.RUN_SUCCEEDED,
                subject_type="run",
                subject_id="1",
                payload={},
                occurred_at=_T0,
            ),
        )
    ).seq
    await async_session.commit()

    page = await gov.list_evidence(
        async_session, _ctx(seed.auditor), EvidenceFilter(project_id=seed.project_id)
    )
    by_seq = {item.seq: item for item in page.items}
    assert by_seq[system_seq].actor_id is None
    assert by_seq[system_seq].actor_name == ""
    assert by_seq[max(by_seq)].actor_name == ""  # 上一条即是系统事件
    maker_event = by_seq[min(by_seq)]  # 建项目那条，actor 就是 maker
    assert maker_event.actor_name == "Maker"


async def test_没有检查点时_block_seq_为空(async_session: AsyncSession) -> None:
    """检查点每 N 条才打一个，早期事件本就没有覆盖它的块。
    给一个编造的块号比给 null 更糟——前端会显示成「已被签名保护」。
    """
    seed = await _bootstrap(async_session)
    await seed.seed(async_session, 1)

    page = await gov.list_evidence(
        async_session, _ctx(seed.auditor), EvidenceFilter(project_id=seed.project_id)
    )
    assert all(item.block_seq is None for item in page.items)


async def test_检查点区间内的_block_seq_有值(async_session: AsyncSession) -> None:
    seed = await _bootstrap(async_session)
    await seed.seed(async_session, 2)
    seqs = await seed.all_seqs(async_session)

    async_session.add(
        EvidenceLedgerBlock(
            tenant_id=seed.tenant_id,
            seq=1,
            from_event_seq=seqs[-1],
            to_event_seq=seqs[-1],
            chain_head_hash="a" * 64,
            signature="sig",
            key_id="k1",
            signed_at=_T0,
        )
    )
    await async_session.commit()

    page = await gov.list_evidence(
        async_session, _ctx(seed.auditor), EvidenceFilter(project_id=seed.project_id)
    )
    by_seq = {item.seq: item for item in page.items}
    assert by_seq[seqs[-1]].block_seq == 1
    assert by_seq[seqs[0]].block_seq is None


# ---------------------------------------------------------------- 验链


async def test_完整链校验通过(async_session: AsyncSession) -> None:
    seed = await _bootstrap(async_session)
    await seed.seed(async_session, 4)
    total = len(await seed.all_seqs(async_session))

    result = await gov.verify_ledger(
        async_session, _ctx(seed.auditor), VerifyRequest(project_id=seed.project_id)
    )
    assert result.ok is True
    assert result.checked_events == total
    assert result.broken_at_seq is None
    # None = 从创世条目起验，即「这次是全验」
    assert result.started_from_block_seq is None


async def test_改内容但不动哈希会被抓到(async_session: AsyncSession) -> None:
    """**这条用例是验链实现是否真的在重算的判据。**

    只改 ``payload``、留着原来的 ``event_hash`` 与 ``prev_hash``：
    一个「比对存储值」的实现会报 ok=True，而真正重算的实现必须报断链。
    """
    seed = await _bootstrap(async_session)
    seqs = await seed.seed(async_session, 3)
    target = seqs[1]

    await async_session.execute(
        update(EvidenceEvent)
        .where(EvidenceEvent.seq == target)
        .values(payload={"idx": 999, "tampered": True})
    )
    await async_session.commit()

    result = await gov.verify_ledger(
        async_session, _ctx(seed.auditor), VerifyRequest(project_id=seed.project_id)
    )
    assert result.ok is False
    assert result.broken_at_seq == target
    assert result.broken_reason == "hash_mismatch"
    # 散文版必须给出具体位置：只说「hash_mismatch」的话，运维还得自己找是哪一条
    assert str(target) in (result.broken_detail or "")


async def test_改归属也会被哈希抓到(async_session: AsyncSession) -> None:
    """``tenant_id`` / ``project_id`` / ``actor_id`` 都在哈希输入里。

    不参与哈希的字段等于「可以随便改的元数据」——
    把一条审批事件的 actor 换成别人，链条照样自洽，那账本就失去了证明力。
    """
    seed = await _bootstrap(async_session)
    seqs = await seed.seed(async_session, 2)

    await async_session.execute(
        update(EvidenceEvent).where(EvidenceEvent.seq == seqs[0]).values(actor_id=None)
    )
    await async_session.commit()

    result = await gov.verify_ledger(
        async_session, _ctx(seed.auditor), VerifyRequest(project_id=seed.project_id)
    )
    assert result.ok is False
    assert result.broken_at_seq == seqs[0]
    assert result.broken_reason == "hash_mismatch"


async def test_断链本身要入账(async_session: AsyncSession) -> None:
    """be-06 §7 第 9 行：断链是需要人工介入的安全事件，必须写账本。"""
    seed = await _bootstrap(async_session)
    seqs = await seed.seed(async_session, 2)

    await async_session.execute(
        update(EvidenceEvent).where(EvidenceEvent.seq == seqs[0]).values(artifact_hash="deadbeef")
    )
    await async_session.commit()

    await gov.verify_ledger(
        async_session, _ctx(seed.auditor), VerifyRequest(project_id=seed.project_id)
    )

    rows = (
        (
            await async_session.execute(
                select(EvidenceEvent)
                .where(EvidenceEvent.event_type == EvidenceEventType.POLICY_BLOCKED)
                .order_by(EvidenceEvent.seq.asc())
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].payload["kind"] == "hash_mismatch"
    assert rows[0].subject_type == "ledger"


async def test_删掉中间事件报不连续(async_session: AsyncSession) -> None:
    seed = await _bootstrap(async_session)
    seqs = await seed.seed(async_session, 4)

    await async_session.execute(EvidenceEvent.__table__.delete().where(EvidenceEvent.seq == seqs[1]))
    await async_session.commit()

    result = await gov.verify_ledger(
        async_session, _ctx(seed.auditor), VerifyRequest(project_id=seed.project_id)
    )
    assert result.ok is False
    assert result.broken_at_seq == seqs[2]
    assert result.broken_reason == "missing_event"


async def test_从中段起验不误报断链(async_session: AsyncSession) -> None:
    """``from_seq > 1`` 时那一段本就缺了头，拿它去对 ``ZERO_HASH`` 必然失败。

    报 ``ok=False`` 会让「只想复核最近一千条」变成一个永远返回断链的功能——
    而一个总是报警的告警等于没有告警。
    """
    seed = await _bootstrap(async_session)
    seqs = await seed.seed(async_session, 5)
    start = seqs[2]

    result = await gov.verify_ledger(
        async_session, _ctx(seed.auditor), VerifyRequest(project_id=seed.project_id, from_seq=start)
    )
    assert result.ok is True
    assert result.checked_events == len(seqs) - 2
    # 非 None 表示「只验了一段」。前端据此加「部分验证」提示——
    # 全绿不等于完整
    assert result.started_from_block_seq == 0


async def test_指定区间只验这一段(async_session: AsyncSession) -> None:
    seed = await _bootstrap(async_session)
    seqs = await seed.seed(async_session, 5)

    result = await gov.verify_ledger(
        async_session,
        _ctx(seed.auditor),
        VerifyRequest(project_id=seed.project_id, from_seq=seqs[1], to_seq=seqs[3]),
    )
    assert result.ok is True
    assert result.checked_events == 3


async def test_区间外的事件被改不影响这一段(async_session: AsyncSession) -> None:
    """区间语义要真的生效：否则前端「只看这一段」的按钮会报出一个远处的断链，
    用户无从判断那是不是自己关心的范围。
    """
    seed = await _bootstrap(async_session)
    seqs = await seed.seed(async_session, 5)

    await async_session.execute(
        update(EvidenceEvent).where(EvidenceEvent.seq == seqs[0]).values(payload={"x": 1})
    )
    await async_session.commit()

    result = await gov.verify_ledger(
        async_session,
        _ctx(seed.auditor),
        VerifyRequest(project_id=seed.project_id, from_seq=seqs[1]),
    )
    assert result.ok is True


async def test_检查点计数只算完全落在已验区间内的(async_session: AsyncSession) -> None:
    """一个覆盖 1..1000 的块，在「只验了 3 条」时不能算进来：
    报告会显示「验了 3 条事件、覆盖 1 个检查点」，而那个检查点里的 997 条谁都没看过。
    """
    seed = await _bootstrap(async_session)
    seqs = await seed.seed(async_session, 5)

    async_session.add(
        EvidenceLedgerBlock(
            tenant_id=seed.tenant_id,
            seq=1,
            from_event_seq=seqs[0],
            to_event_seq=seqs[-1],
            chain_head_hash="a" * 64,
            signature="sig",
            key_id="k1",
            signed_at=_T0,
        )
    )
    await async_session.commit()

    partial = await gov.verify_ledger(
        async_session,
        _ctx(seed.auditor),
        VerifyRequest(project_id=seed.project_id, to_seq=seqs[1]),
    )
    assert partial.checked_blocks == 0

    whole = await gov.verify_ledger(
        async_session, _ctx(seed.auditor), VerifyRequest(project_id=seed.project_id)
    )
    assert whole.checked_blocks == 1


# ---------------------------------------------------------------- 导出


async def _export(
    session: AsyncSession,
    ctx: SubjectContext,
    flt: EvidenceFilter,
    fmt: str,
    *,
    max_events: int = 50_000,
) -> bytes:
    return b"".join(
        [
            chunk
            async for chunk in gov.export_evidence(session, ctx, flt, fmt=fmt, max_events=max_events)
        ]
    )


async def test_导出_JSON_自带验链报告(async_session: AsyncSession) -> None:
    """一份无法自证完整的副本，在审计桌上和被删了几行的打印件没有区别。"""
    seed = await _bootstrap(async_session)
    await seed.seed(async_session, 3)

    body = await _export(
        async_session,
        _ctx(seed.auditor),
        EvidenceFilter(project_id=seed.project_id),
        "json",
    )
    total = len(await seed.all_seqs(async_session))
    payload = json.loads(body.decode("utf-8"))
    assert len(payload["events"]) == total
    assert payload["report"]["ok"] is True
    assert payload["report"]["checked_events"] == total
    assert payload["filter"]["project_id"] == seed.project_id
    assert payload["exported_by"] == seed.auditor.id
    assert "exported_at" in payload


async def test_导出_CSV_带_BOM_且列序固定(async_session: AsyncSession) -> None:
    """不带 BOM 时 Windows 版 Excel 会把 UTF-8 当 GBK 读，中文列全乱码——
    而审计拿到的就是一份看不懂的文件。列序固定是为了拿旧文件对账时可预期。
    """
    seed = await _bootstrap(async_session)
    await seed.seed(async_session, 2)

    body = await _export(
        async_session,
        _ctx(seed.auditor),
        EvidenceFilter(project_id=seed.project_id),
        "csv",
    )
    assert body.startswith(b"\xef\xbb\xbf")
    # 用 csv.reader 而不是 split(",")：payload_json 里的逗号与转义引号正是
    # 这个格式最容易出错的地方，手工切分等于把「解析器」也一起重写一遍
    rows = list(csv.reader(io.StringIO(body.decode("utf-8-sig"))))
    assert rows[0] == [
        "seq",
        "occurred_at",
        "event_type",
        "tenant_id",
        "project_id",
        "actor_id",
        "subject_type",
        "subject_id",
        "artifact_hash",
        "prev_hash",
        "event_hash",
        "payload_json",
    ]
    # 表头 + 建项目 1 条 + 种子 2 条
    assert len(rows) == 1 + len(await seed.all_seqs(async_session))
    # payload_json 是最后一列，且是能被解析回去的单行 JSON
    assert json.loads(rows[-1][-1]) == {"idx": 1}
    # 每行都是 12 列：列数对不上说明有字段里混进了裸换行
    assert all(len(row) == 12 for row in rows)


async def test_导出只对_auditor_开放(async_session: AsyncSession) -> None:
    seed = await _bootstrap(async_session)
    with pytest.raises(RoleRequiredError):
        await _export(
            async_session,
            _ctx(seed.maker),
            EvidenceFilter(project_id=seed.project_id),
            "json",
        )


async def test_导出遵守过滤条件(async_session: AsyncSession) -> None:
    seed = await _bootstrap(async_session)
    seqs = await seed.seed(async_session, 3)

    body = await _export(
        async_session,
        _ctx(seed.auditor),
        # 与 ``test_按发生时间过滤`` 同一个成因：fixture 里那条「建项目」事件用的是真实
        # 当前时间，只给下界会让本用例随跑测试的钟点变红。两端夹住才与时钟无关。
        EvidenceFilter(
            project_id=seed.project_id,
            occurred_from=_T0 + timedelta(minutes=2),
            occurred_to=_T0 + timedelta(minutes=3),
        ),
        "json",
    )
    payload = json.loads(body.decode("utf-8"))
    assert [e["seq"] for e in payload["events"]] == [seqs[-1]]


async def test_导出超过上限会被截断而不是打满内存(async_session: AsyncSession) -> None:
    """``limit`` 是**导出上限**而非分页大小：超过就该走归档。

    这里只验「上限真的被执行」（用 1 条上限 + 3 条数据），
    真跑五万条不在单测里做。
    """
    seed = await _bootstrap(async_session)
    await seed.seed(async_session, 3)

    body = await _export(
        async_session,
        _ctx(seed.auditor),
        EvidenceFilter(project_id=seed.project_id),
        "json",
        max_events=1,
    )
    payload = json.loads(body.decode("utf-8"))
    assert len(payload["events"]) == 1
    # 截断了就**不是**完整账本，报告必须仍然如实反映链的状态（链本身是全验的）
    assert payload["report"]["ok"] is True


# ---------------------------------------------------------------- 路由挂载


def test_三条账本路由都已挂载() -> None:
    """**用 ``openapi()["paths"]`` 断言，不要数 ``app.routes``。**

    Starlette 1.7 的 ``include_router`` 不再把子路由摊平进 ``app.routes``，
    而是包成 ``_IncludedRouter``——数 routes 会漏报，于是「路由没挂上」
    这类问题在测试里看不出来，只在联调时暴露。
    """
    from main import create_app

    paths = create_app().openapi()["paths"]
    assert set(paths["/api/v1/governance/evidence"]) == {"get"}
    assert set(paths["/api/v1/governance/evidence/verify"]) == {"post"}
    assert set(paths["/api/v1/governance/evidence/export"]) == {"get"}


def test_账本没有任何写接口() -> None:
    """append-only 是架构约束，不是「还没做」：写入口只有服务端的
    ``EvidenceLedger.append()``，不对外暴露，所以账本路径下不该有
    PUT/PATCH/DELETE。
    """
    from main import create_app

    paths = create_app().openapi()["paths"]
    ledger_paths = {path: ops for path, ops in paths.items() if path.startswith("/api/v1/governance")}
    methods = {m for ops in ledger_paths.values() for m in ops}
    assert methods <= {"get", "post"}
    assert not any(m in {"put", "patch", "delete"} for m in methods)


def test_project_模型可用() -> None:
    """``_bootstrap`` 依赖 ``Project`` 与 ``create_project`` 的返回形状。

    单独一条极小用例：模型搬家（比如挪去 ``models/project.py``）时，
    失败信息会直接指出是这里，而不是散在二十个用例的 ``AttributeError`` 里。
    """
    assert Project.__tablename__ == "project"


# ---------------------------------------------------------------- 参数错误的码型


def test_未知事件类型是422而不是500() -> None:
    """``Enum(...)`` 的 `ValueError` 会一路落到兜底处理器变成 500。

    500 在前端只能显示成「服务器错误」，用户看不出是自己把枚举名拼错了——
    而 `_csv_types` 的 docstring 明确承诺「未知取值直接 422 而不是忽略」。
    这里锁住这个承诺：抛的必须是带 422 的 `ValidationError`。
    """
    from api.governance import _csv_types
    from core.errors import ValidationError

    with pytest.raises(ValidationError) as excinfo:
        _csv_types("run.failed,not.a.real.event")
    assert excinfo.value.http_status == 422


def test_服务层pydantic校验失败映射为422() -> None:
    """服务层构造 DTO 失败（如 `from=` 传了非法时间）也必须回 422。

    `RequestValidationError` 只覆盖请求解析阶段；服务层抛的是 pydantic 自己的
    `ValidationError`。没有这个处理器时它会落到兜底 `Exception` → 500。
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from pydantic import BaseModel
    from pydantic import ValidationError as PydanticValidationError

    from core.errors import register_exception_handlers

    class _Dto(BaseModel):
        n: int

    class _Bad:
        """真造一个 pydantic 校验错误，而不是凭空构造一个异常对象。"""

        n = "not-an-int"

    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/boom")
    async def _boom() -> dict[str, int]:  # pragma: no cover - 只为触发处理器
        try:
            _Dto(n=_Bad.n)  # type: ignore[arg-type]
        except PydanticValidationError as exc:
            raise exc
        raise AssertionError("上面的构造本应失败")

    resp = TestClient(app, raise_server_exceptions=False).get("/boom")
    assert resp.status_code == 422
    assert resp.json()["code"] == "VALIDATION_ERROR"
