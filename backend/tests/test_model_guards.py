"""不可变制品保护与枚举不变量的单元测试。

不可变拦截必须**落库之后**再验证：只有 persistent 对象才进 ``session.dirty``，
pending 对象改字段只是构造过程，拦它反而会误伤正常写入。所以这里用内存 SQLite
真插一行再改，而不是靠 mock。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import DateTime, select
from sqlalchemy.orm import Session

from models import (
    ApprovalDecision,
    Capability,
    EvidenceEvent,
    ExecutionRun,
    ImmutableViolationError,
    PipelineVersion,
    Preparation,
    QualityReport,
)
from models.base import Base
from models.enums import ApprovalSlot, PreparationStatus, RiskLevel, RunStatus

NOW = datetime(2026, 10, 14, 12, 0, 0, tzinfo=UTC)


def _preparation(**overrides: object) -> Preparation:
    fields: dict[str, object] = {
        "tenant_id": 1,
        "project_id": 1,
        "pipeline_version_id": 1,
        "input_fingerprint": "a" * 64,
        "artifact_sha256": "b" * 64,
        "risk_level": RiskLevel.P2,
        "created_by": 7,
        "frozen_at": NOW,
    }
    fields.update(overrides)
    return Preparation(**fields)


def _quality_report(**overrides: object) -> QualityReport:
    fields: dict[str, object] = {"run_id": 1, "generated_at": NOW}
    fields.update(overrides)
    return QualityReport(**fields)


# ---------------------------------------------------------------- 不可变拦截


@pytest.mark.parametrize(
    "model",
    [PipelineVersion, Preparation, ApprovalDecision, Capability, EvidenceEvent, QualityReport],
)
def test_不可变制品已标记(model: type) -> None:
    assert getattr(model, "__immutable__", False) is True


def test_修改不可变制品的非白名单列被拦截并报出列名(session: Session) -> None:
    report = _quality_report()
    session.add(report)
    session.commit()

    report.passed = True  # 试图篡改已出具的质量结论
    with pytest.raises(ImmutableViolationError) as ei:
        session.flush()
    assert "passed" in str(ei.value)


def test_白名单列允许修改且能持久化(session: Session) -> None:
    """Preparation.status 是唯一放行的列——审批推进必须能改它。"""
    prep = _preparation()
    session.add(prep)
    session.commit()

    prep.status = PreparationStatus.APPROVED
    session.commit()

    session.expire_all()
    fresh = session.execute(select(Preparation).where(Preparation.id == prep.id)).scalar_one()
    assert fresh.status is PreparationStatus.APPROVED


def test_白名单以外的列同时被改时仍然拦截(session: Session) -> None:
    """放行 status 不等于放行整行：夹带篡改必须被抓。"""
    prep = _preparation()
    session.add(prep)
    session.commit()

    prep.status = PreparationStatus.APPROVED
    prep.budget = {"max_read_rows": 1}
    with pytest.raises(ImmutableViolationError) as ei:
        session.flush()
    # 非法列清单里只能有 budget；status 只能出现在「允许修改」那一侧
    assert "['budget']" in str(ei.value)
    assert "['status']" in str(ei.value)


def test_删除不可变制品被拦截(session: Session) -> None:
    event = EvidenceEvent(
        tenant_id=1,
        seq=1,
        event_type="run.succeeded",
        prev_hash="0" * 64,
        event_hash="1" * 64,
        occurred_at=NOW,
    )
    session.add(event)
    session.commit()

    session.delete(event)
    with pytest.raises(ImmutableViolationError) as ei:
        session.flush()
    assert "证据" in str(ei.value) or "EvidenceEvent" in str(ei.value)


def test_可变模型不受不可变保护(session: Session) -> None:
    assert getattr(ExecutionRun, "__immutable__", False) is False

    run = ExecutionRun(
        tenant_id=1,
        project_id=1,
        pipeline_version_id=1,
        preparation_id=1,
        idempotency_key="k-1",
    )
    session.add(run)
    session.commit()

    run.status = RunStatus.RUNNING
    run.read_rows = 12345
    session.commit()  # 不应抛异常

    session.expire_all()
    fresh = session.execute(select(ExecutionRun).where(ExecutionRun.id == run.id)).scalar_one()
    assert fresh.read_rows == 12345


# ---------------------------------------------------------------- 枚举不变量


def test_p0_必须阻断其余不阻断() -> None:
    """P0 无审批通道；P1 走双审批加固，因此「不阻断」不等于「放行」。"""
    assert RiskLevel.P0.must_block is True
    assert [RiskLevel.P1.must_block, RiskLevel.P2.must_block, RiskLevel.P3.must_block] == [
        False,
        False,
        False,
    ]


def test_p0p1_需要加固() -> None:
    assert RiskLevel.P0.requires_hardening is True
    assert RiskLevel.P1.requires_hardening is True
    assert RiskLevel.P2.requires_hardening is False
    assert RiskLevel.P3.requires_hardening is False


def test_审批槽位数量符合判定矩阵() -> None:
    assert RiskLevel.P0.min_approval_slots == 0  # 已阻断
    assert RiskLevel.P1.min_approval_slots == 2  # 数据 + 安全
    assert RiskLevel.P2.min_approval_slots == 2
    assert RiskLevel.P3.min_approval_slots == 0  # 只读探查，免审批


def test_风险比较按严重程度而非字符串序() -> None:
    # 越大越危险：P0 是最严的一档
    assert RiskLevel.P0 > RiskLevel.P1 > RiskLevel.P2 > RiskLevel.P3
    assert RiskLevel.P0.rank < RiskLevel.P1.rank  # rank 仍是「越小越危险」的编号
    # 关键用例：max() 必须给出最危险的一档。若退回 StrEnum 的字符串比较会返回 P2。
    assert max(RiskLevel.P0, RiskLevel.P2) is RiskLevel.P0
    assert RiskLevel.most_severe(RiskLevel.P3, RiskLevel.P1, RiskLevel.P2) is RiskLevel.P1
    assert RiskLevel.P0 >= RiskLevel.P0
    assert RiskLevel.P0 >= RiskLevel.P1  # 「至少和 P1 一样严」
    assert (RiskLevel.P3 >= RiskLevel.P1) is False  # 括号必需：避免写成链式比较


def test_most_severe_空入参报错而非返回默认值() -> None:
    with pytest.raises(ValueError, match="至少需要一个"):
        RiskLevel.most_severe()


def test_活跃运行状态与部分唯一索引口径一致() -> None:
    """索引里硬编码的状态字符串必须与枚举同步，否则并发保护会静默失效。"""
    assert {s.value for s in RunStatus.active_states()} == {
        "queued",
        "running",
        "paused",
        "interrupted",
        "rolling_back",
    }
    assert RunStatus.SUCCEEDED not in RunStatus.active_states()
    assert RunStatus.CANCELLED not in RunStatus.active_states()


def test_审批槽位为两个不同职责() -> None:
    assert ApprovalSlot.CHECKER_DATA.value == "checker_data"
    assert ApprovalSlot.CHECKER_SECURITY.value == "checker_security"
    assert len({s.value for s in ApprovalSlot}) == 2


# ---------------------------------------------------------------- 时间列不变量


def test_所有时间列都必须带时区() -> None:
    """没有 timezone=True 的 DateTime 会在真 PG 上直接炸，而 SQLite 单测看不出来。

    失败过一次的形态是 ``last_login_at: Mapped[datetime | None] = mapped_column(nullable=True)``
    ——不写类型时 SQLAlchemy 从注解推断出**不带时区**的 TIMESTAMP，而调用方一律传
    ``datetime.now(UTC)``。asyncpg 在绑定参数那一刻报
    "can't subtract offset-naive and offset-aware datetimes"，
    表现为登录接口 500。SQLite 对 aware/naive 混用是宽容的，所以本机 371 个测试全绿、
    真库上一登录就挂——这条不变量就是用来把这种「只有真库才暴露」的错配钉死在模型层。

    正确写法有两种：显式 ``DateTime(timezone=True)``，或走 ``TimestampMixin``。
    """
    naive: list[str] = []
    for table in Base.metadata.sorted_tables:
        for column in table.columns:
            if isinstance(column.type, DateTime) and not column.type.timezone:
                naive.append(f"{table.name}.{column.name}")
    assert not naive, "以下时间列缺 timezone=True，会在真 PG 上拒绝带时区的取值：" + "、".join(naive)


# ---------------------------------------------------------------- 候选版本（冻结列）


def _pipeline_version(**overrides: object) -> PipelineVersion:
    """候选版本：``frozen_at`` 为空即「草案」。

    ``pipeline_id`` 由调用方给且**必须各用例不同**：本文件的 ``sqlite_engine`` 是
    session 级共享、函数级 ``session`` 只回滚不删表，所以两个用例用同一个
    ``pipeline_id`` 就会撞上「一个 pipeline 最多一行候选」那条部分唯一索引——
    而那个 IntegrityError 会伪装成被测逻辑的失败。
    """
    fields: dict[str, object] = {
        "pipeline_id": 101,
        "version_no": 1,
        "etl_plan_id": 1,
        "hocon_artifact_id": 1,
        "quality_contract_id": 1,
        "supervision_contract_id": 1,
        "generation_context": {},
        "sha256": "c" * 64,
        "frozen_at": None,
    }
    fields.update(overrides)
    return PipelineVersion(**fields)


def test_候选版本允许改内容列(session: Session) -> None:
    """frozen_at 还空着的行是**草案**：重生成、自动修复要来改它，必须放行。"""
    version = _pipeline_version(pipeline_id=102)
    session.add(version)
    session.commit()

    version.sha256 = "d" * 64
    version.etl_plan_id = 2
    session.commit()  # 不应抛异常

    session.expire_all()
    fresh = session.execute(select(PipelineVersion).where(PipelineVersion.id == version.id)).scalar_one()
    assert fresh.sha256 == "d" * 64
    assert fresh.etl_plan_id == 2


def test_候选版本不允许改身份列(session: Session) -> None:
    """可以改内容，不能改身份：换 pipeline/换版本号等于把归属交给一次 UPDATE。"""
    version = _pipeline_version(pipeline_id=103)
    session.add(version)
    session.commit()

    version.version_no = 99
    with pytest.raises(ImmutableViolationError) as ei:
        session.flush()
    assert "version_no" in str(ei.value)


def test_冻结之后连白名单列也不可改(session: Session) -> None:
    """冻结是单向的：写进 frozen_at 的那一刻，整行连白名单都失效。

    这条是「审批绑定指纹」的前提——冻结后的版本若能改 note 或 sha256，
    那么审批时看到的制品和执行的制品就可能不是同一份。
    """
    version = _pipeline_version(pipeline_id=104)
    session.add(version)
    session.commit()

    version.frozen_at = NOW
    session.commit()  # 冻结这一次本身必须放行，否则没人冻得了

    version.note = "偷偷补一句"
    with pytest.raises(ImmutableViolationError) as ei:
        session.flush()
    assert "note" in str(ei.value)


def test_一个pipeline最多一行候选_由数据库兜住(session: Session) -> None:
    """并发两次生成时「先查再写」中间有窗口，不变式必须落在索引上。"""
    from sqlalchemy.exc import IntegrityError

    session.add(_pipeline_version(pipeline_id=105, version_no=1))
    session.commit()

    session.add(_pipeline_version(pipeline_id=105, version_no=2))  # 同一 pipeline 的第二行候选
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()
