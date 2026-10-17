"""``ToolBroker`` 七步执行序与拒绝语义（be-05）。

本文件对应 CLAUDE.md §10 的验收项：**未授权/重放成功率 = 0**。
每一条「拒绝」都断言三件事：错误码、**执行器没被调用**、账本里有 ``CAPABILITY_REJECTED``。
只断言错误码是不够的——真正要防的是「报了错但还是执行了」。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import fakeredis.aioredis
import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.errors import AppError, ErrorCode
from harness import (
    CapabilityIssuer,
    ExecutionContext,
    ExpectedBinding,
    ReplayGuard,
    ToolBroker,
    register_executor,
)
from harness.broker import ToolResult
from harness.ledger import EvidenceLedger
from harness.tools import (
    MetadataProbeArgs,
    SeatunnelSubmitArgs,
    SecretRefArg,
)
from models.enums import Env, EvidenceEventType, PreparationStatus, RiskLevel, RunStatus, ToolName
from models.evidence import EvidenceEvent
from models.execution import ExecutionRun, QualityReport
from models.governance import Capability, Preparation

TTL = 300
ISSUER = "elt-agent-test"
ARTIFACT = "a" * 64
# 基准时间取**真实时钟往前一分钟**，而不是写死的字面量：
# 验签会拒绝「签发时间在未来」的令牌，写死一个时刻后，只要真实时钟还没走到那一刻，
# 所有忘记传 now= 的用例都会以 CAPABILITY_INVALID 失败——而那看起来像安全逻辑出错，
# 实际只是夹具的时钟没对齐。
NOW = (datetime.now(UTC) - timedelta(minutes=1)).replace(microsecond=0)
TENANT = 7
PROJECT = 11
OPERATOR = 42


# ---------------------------------------------------------------- 夹具


@pytest.fixture(scope="module")
def keypair(tmp_path_factory) -> tuple[str, str]:
    """一对 Ed25519 密钥，写在临时目录（本机没有 /run/secrets）。"""
    directory = tmp_path_factory.mktemp("capability-keys")
    private = ed25519.Ed25519PrivateKey.generate()
    priv_path = directory / "private.pem"
    pub_path = directory / "public.pem"
    priv_path.write_bytes(
        private.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    pub_path.write_bytes(
        private.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    )
    return str(priv_path), str(pub_path)


@pytest.fixture
def issuer(keypair) -> CapabilityIssuer:
    return CapabilityIssuer(private_key_path=keypair[0], key_id="test-1", issuer=ISSUER, ttl_sec=TTL)


@pytest.fixture
def verifier(keypair):
    from harness import CapabilityVerifier

    return CapabilityVerifier(public_key_path=keypair[1], issuer=ISSUER, ttl_sec=TTL)


# 副作用记录器。刻意是**模块级**的：执行器只在进程内注册一次（注册表禁止覆盖，
# 那是有意的安全设计），闭包会被第一个用例捕获——若闭包存的是「当前用例的 calls」，
# 后续用例就会看到一个永远为空的列表，于是「执行器没被调用」这类断言全部假绿。
_CALLS: list[str] = []


@pytest.fixture
def calls() -> list[str]:
    _CALLS.clear()
    return _CALLS


@pytest.fixture(autouse=True)
def fake_executors(monkeypatch: pytest.MonkeyPatch) -> None:
    """注册假执行器。真执行器要连 SeaTunnel / MySQL，本机没有。

    **先把注册表换成本模块私有的空表，跑完还原**：执行器注册表是进程级全局，
    而别的模块会往里面塞真实现（例如 ``tests/test_finalize.py`` 要走真的
    ``target.publish.swap``）。不隔离的话，下面那句「已经有人注册过就整体跳过」
    会因为在**别的文件里**注册过而跳过本模块的假执行器，症状是本模块报
    「工具未注册执行器」——一个与它要验的东西毫无关系、且**取决于文件执行顺序**的失败
    （实测：单跑绿、与 test_finalize 同跑红）。
    旧写法让「哪些工具已注册」成了一个跨文件的隐式事实，这一句把它变成局部状态。
    """
    import harness.tools as tools_module

    monkeypatch.setattr(tools_module, "_EXECUTORS", {})

    async def _submit(args: Any, ctx: Any, session: Any) -> dict[str, Any]:
        _CALLS.append(f"seatunnel.submit:{args.run_id}")
        # 夹带嵌套结构，验证 _safe_detail 会裁掉
        return {"job_id": "job-1", "shadow_table": args.shadow_table, "raw_row": {"x": 1}}

    async def _probe(args: Any, ctx: Any, session: Any) -> dict[str, Any]:
        _CALLS.append("metadata.probe")
        # 夹带一个白名单外的键，验证 _safe_detail 会裁掉
        return {"profile_id": 9, "version": 1, "internal_dsn": "mysql://u:p@h/db"}

    for tool, fn in ((ToolName.SEATUNNEL_SUBMIT, _submit), (ToolName.METADATA_PROBE, _probe)):
        register_executor(tool, fn)


@pytest.fixture
def guard() -> ReplayGuard:
    return ReplayGuard(redis=fakeredis.aioredis.FakeRedis(decode_responses=True), ttl_sec=TTL)


@pytest.fixture
def broker(guard: ReplayGuard, verifier, issuer) -> ToolBroker:
    return ToolBroker(guard=guard, ledger=EvidenceLedger(), verifier=verifier, issuer=issuer)


@pytest.fixture
def ctx() -> ExecutionContext:
    return ExecutionContext(
        request_id="req-1", tenant_id=TENANT, project_id=PROJECT, actor_id=OPERATOR, caller="worker"
    )


async def _make_preparation(session: AsyncSession, **overrides: Any) -> Preparation:
    """新建一张准备单。

    ``preparation`` 是**不可变制品**（``__mutable_columns__ = {"status"}``），
    所以「审批后制品被改动」「审批单已过期」这类场景**不能靠改已有行来构造**——
    ORM 会直接拒绝。这也正是本文件不用「改一行再断言」写法的原因：
    那个写法在真实系统里根本发生不了。
    """
    fields: dict[str, Any] = {
        "tenant_id": TENANT,
        "project_id": PROJECT,
        "pipeline_version_id": 1,
        "input_fingerprint": "b" * 64,
        "artifact_sha256": ARTIFACT,
        "environment": Env.PROD,
        "risk_level": RiskLevel.P1,
        "status": PreparationStatus.APPROVED,
        "created_by": OPERATOR,
        "frozen_at": NOW - timedelta(hours=1),
        "expires_at": NOW + timedelta(hours=1),
    }
    fields.update(overrides)
    row = Preparation(**fields)
    session.add(row)
    await session.flush()
    return row


@pytest.fixture
async def preparation(async_session: AsyncSession) -> Preparation:
    return await _make_preparation(async_session)


def _issue(
    issuer: CapabilityIssuer,
    preparation: Preparation,
    tool: ToolName = ToolName.SEATUNNEL_SUBMIT,
    *,
    artifact_sha256: str | None = None,
):
    return issuer.issue(
        preparation_id=preparation.id,
        subject_id=OPERATOR,
        tool=tool,
        env=preparation.environment,
        artifact_sha256=artifact_sha256 or preparation.artifact_sha256,
        tenant_id=TENANT,
        project_id=PROJECT,
        now=NOW,
    )


async def _register_capability(session: AsyncSession, issued, preparation: Preparation) -> None:
    """把签发事实落库——真实流程里这和签发同事务（be-05 §5.1 步骤 4）。

    少了这行，ReplayGuard 的条件 UPDATE 会更新 0 行，正常消费被误判成重放。
    """
    c = issued.claims
    session.add(
        Capability(
            jti=c.jti,
            preparation_id=c.preparation_id,
            subject_id=c.subject_id,
            tool=c.tool.value,
            environment=c.env,
            artifact_sha256=c.artifact_sha256,
            token_sha256=issued.token_sha256,
            issued_at=c.iat,
            expires_at=c.exp,
        )
    )
    await session.flush()


async def _rejections(session: AsyncSession) -> list[EvidenceEvent]:
    rows = await session.execute(
        select(EvidenceEvent).where(EvidenceEvent.event_type == EvidenceEventType.CAPABILITY_REJECTED)
    )
    return list(rows.scalars())


def _read_pem(path: str) -> bytes:
    """同步读密钥文件。

    刻意包一层：在 ``async def`` 里直接做文件 IO 会被 ASYNC240 判为「阻塞事件循环」，
    这个判断本身是对的——把它挪进同步函数，意图就明确了：这确实是同步 IO，
    只是发生在测试夹具里，不在服务路径上。
    """
    return Path(path).read_bytes()


def _submit_args() -> dict[str, Any]:
    return {
        "run_id": 5,
        "pipeline_version_id": 3,
        "hocon_artifact_id": 2,
        "shadow_table": "dw_demo.orders_shadow",
        "error_table": "dw_demo.orders_err",
    }


# ---------------------------------------------------------------- ① 工具存在性


async def test_未注册的工具被拒(broker, ctx, async_session, calls) -> None:
    with pytest.raises(AppError) as exc:
        await broker.execute("not.a.tool", None, {}, ctx, session=async_session)  # type: ignore[arg-type]
    assert exc.value.code is ErrorCode.UNAUTHORIZED_TOOL
    assert calls == []
    assert len(await _rejections(async_session)) == 1


async def test_工具注册表禁止覆盖() -> None:
    """覆盖式注册让人能在运行期换掉 publish_swap 的实现，而账本仍记同一个工具名。"""
    from harness.tools import ToolSpec, register_tool

    spec = ToolSpec(
        tool=ToolName.TARGET_PUBLISH_SWAP,
        risk=RiskLevel.P0,
        requires_capability=True,
        executor="worker",
        args_model=SeatunnelSubmitArgs,
        side_effect="伪装",
        idempotent=True,
    )
    with pytest.raises(RuntimeError, match="已注册"):
        register_tool(spec)


# ---------------------------------------------------------------- ② 要求一致性


async def test_需签工具不传token被拒(broker, ctx, async_session, calls) -> None:
    with pytest.raises(AppError) as exc:
        await broker.execute(ToolName.SEATUNNEL_SUBMIT, None, _submit_args(), ctx, session=async_session)
    assert exc.value.code is ErrorCode.CAPABILITY_INVALID
    assert calls == []


async def test_免签工具传token被拒(broker, async_session, issuer, calls) -> None:
    """「多带一个 token 更安全」是一种会绕开免签路径的误解。

    必须用 ``api`` 上下文：免签工具的执行进程就是 api，用 worker 会先撞上
    「工具不允许在该进程执行」而测不到本意想测的那条分支。
    """
    issued = issuer.issue(
        preparation_id=1,
        subject_id=OPERATOR,
        tool=ToolName.METADATA_PROBE,
        env=Env.PROD,
        artifact_sha256=ARTIFACT,
        tenant_id=TENANT,
        project_id=PROJECT,
        now=NOW,
    )
    api_ctx = ExecutionContext(
        request_id="r", tenant_id=TENANT, project_id=PROJECT, actor_id=OPERATOR, caller="api"
    )
    with pytest.raises(AppError) as exc:
        await broker.execute(
            ToolName.METADATA_PROBE, issued.token, {"data_source_id": 1}, api_ctx, session=async_session
        )
    assert exc.value.code is ErrorCode.CAPABILITY_INVALID
    assert calls == []


async def test_api进程不能执行worker工具(broker, ctx, async_session, issuer, preparation, calls) -> None:
    """api 进程直接提交 SeaTunnel 是绕过审批的最短路径。"""
    issued = _issue(issuer, preparation)
    await _register_capability(async_session, issued, preparation)
    api_ctx = ExecutionContext(
        request_id="r", tenant_id=TENANT, project_id=PROJECT, actor_id=OPERATOR, caller="api"
    )
    with pytest.raises(AppError) as exc:
        await broker.execute(
            ToolName.SEATUNNEL_SUBMIT, issued.token, _submit_args(), api_ctx, session=async_session
        )
    assert exc.value.code is ErrorCode.UNAUTHORIZED_TOOL
    assert calls == []


# ---------------------------------------------------------------- ③ 验签


async def test_伪造令牌被拒(broker, ctx, async_session, preparation, calls) -> None:
    forged = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1c2VyOjQyIn0.x"
    with pytest.raises(AppError) as exc:
        await broker.execute(ToolName.SEATUNNEL_SUBMIT, forged, _submit_args(), ctx, session=async_session)
    assert exc.value.code is ErrorCode.CAPABILITY_INVALID
    assert calls == []


async def test_hs256降级攻击被拒(broker, ctx, async_session, preparation, calls) -> None:
    """把 EdDSA 换成 HS256、拿公钥当 HMAC 密钥，是 JWT 最经典的绕过手法。

    算法白名单让它连"验签通过但算法不对"这一步都走不到。
    """
    import jwt

    token = jwt.encode(
        {
            "jti": "0f8f0f8f-0f8f-4f8f-8f8f-0f8f0f8f0f8f",
            "sub": f"user:{OPERATOR}",
            "subject_id": OPERATOR,
            "tenant_id": TENANT,
            "project_id": PROJECT,
            "tool": ToolName.SEATUNNEL_SUBMIT.value,
            "env": Env.PROD.value,
            "artifact_sha256": ARTIFACT,
            "preparation_id": preparation.id,
            "iss": ISSUER,
            "iat": int(NOW.timestamp()),
            "exp": int((NOW + timedelta(seconds=TTL)).timestamp()),
        },
        "public-key-as-hmac-secret-0123456789abcdef",  # ≥32 字节，避免 PyJWT 的密钥长度告警
        algorithm="HS256",
    )
    with pytest.raises(AppError) as exc:
        await broker.execute(ToolName.SEATUNNEL_SUBMIT, token, _submit_args(), ctx, session=async_session)
    assert exc.value.code is ErrorCode.CAPABILITY_INVALID
    assert calls == []


async def test_过期令牌被拒(broker, ctx, async_session, issuer, preparation, calls) -> None:
    issued = _issue(issuer, preparation)
    await _register_capability(async_session, issued, preparation)
    later = NOW + timedelta(seconds=TTL + 1)
    with pytest.raises(AppError) as exc:
        await broker.execute(
            ToolName.SEATUNNEL_SUBMIT,
            issued.token,
            _submit_args(),
            ctx,
            session=async_session,
            now=later,
        )
    assert exc.value.code is ErrorCode.CAPABILITY_EXPIRED
    assert calls == []


async def test_有效期被改长的令牌被拒(broker, ctx, async_session, preparation, keypair, calls) -> None:
    """有人用私钥签一张 30 天的令牌 —— 等于把「单次」变成「长期通行证」。"""
    import jwt

    from harness.capability import ALGORITHM

    # PyJWT 直接接受 PEM 字节，不必先 load：少一步也就少一处类型体操
    key = _read_pem(keypair[0])
    token = jwt.encode(
        {
            "jti": "0f8f0f8f-0f8f-4f8f-8f8f-0f8f0f8f0f8f",
            "sub": f"user:{OPERATOR}",
            "subject_id": OPERATOR,
            "tenant_id": TENANT,
            "project_id": PROJECT,
            "tool": ToolName.SEATUNNEL_SUBMIT.value,
            "env": Env.PROD.value,
            "artifact_sha256": ARTIFACT,
            "preparation_id": preparation.id,
            "iss": ISSUER,
            "iat": int(NOW.timestamp()),
            "exp": int((NOW + timedelta(days=30)).timestamp()),
        },
        key,
        algorithm=ALGORITHM,
    )
    with pytest.raises(AppError) as exc:
        await broker.execute(ToolName.SEATUNNEL_SUBMIT, token, _submit_args(), ctx, session=async_session)
    assert exc.value.code is ErrorCode.CAPABILITY_INVALID


# ---------------------------------------------------------------- ④ 绑定


async def test_令牌绑定的工具与本次调用不符(broker, ctx, async_session, issuer, preparation, calls) -> None:
    """拿 A 工具的令牌去调 B 工具：越权，不是参数错误。"""
    issued = _issue(issuer, preparation, tool=ToolName.SEATUNNEL_STOP)
    await _register_capability(async_session, issued, preparation)
    with pytest.raises(AppError) as exc:
        await broker.execute(
            ToolName.SEATUNNEL_SUBMIT, issued.token, _submit_args(), ctx, session=async_session
        )
    assert exc.value.code is ErrorCode.UNAUTHORIZED_TOOL
    assert calls == []


async def test_制品指纹不符被拒(broker, ctx, async_session, issuer, preparation, calls) -> None:
    """令牌签的是制品 X，而 preparation 当前的制品是 Y：令牌必须失效。

    靠签发时给不同的指纹来构造，而不是改 preparation 行——
    后者在真实系统里做不到（不可变制品禁止 UPDATE），拿它当测试会误导实现。
    """
    issued = _issue(issuer, preparation, artifact_sha256="c" * 64)
    await _register_capability(async_session, issued, preparation)

    with pytest.raises(AppError) as exc:
        await broker.execute(
            ToolName.SEATUNNEL_SUBMIT, issued.token, _submit_args(), ctx, session=async_session
        )
    assert exc.value.code is ErrorCode.FINGERPRINT_MISMATCH
    assert calls == []


async def test_跨租户执行被拒(broker, ctx, async_session, issuer, preparation, calls) -> None:
    issued = _issue(issuer, preparation)
    await _register_capability(async_session, issued, preparation)
    other = ExecutionContext(
        request_id="r",
        tenant_id=TENANT + 1,
        project_id=PROJECT,
        actor_id=OPERATOR,
        caller="worker",
    )
    with pytest.raises(AppError) as exc:
        await broker.execute(
            ToolName.SEATUNNEL_SUBMIT, issued.token, _submit_args(), other, session=async_session
        )
    assert exc.value.code is ErrorCode.TENANT_MISMATCH
    assert calls == []


async def test_主体不符被拒(broker, ctx, async_session, issuer, preparation, calls) -> None:
    issued = _issue(issuer, preparation)
    await _register_capability(async_session, issued, preparation)
    other = ExecutionContext(
        request_id="r", tenant_id=TENANT, project_id=PROJECT, actor_id=OPERATOR + 1, caller="worker"
    )
    with pytest.raises(AppError) as exc:
        await broker.execute(
            ToolName.SEATUNNEL_SUBMIT, issued.token, _submit_args(), other, session=async_session
        )
    assert exc.value.code is ErrorCode.CAPABILITY_INVALID
    assert calls == []


# ---------------------------------------------------------------- ⑤ 重放


async def test_同一令牌消费两次只执行一次(broker, ctx, async_session, issuer, preparation, calls) -> None:
    """本系统里唯一「出错就产生业务损失」的地方：两次真实的数据搬运。"""
    issued = _issue(issuer, preparation)
    await _register_capability(async_session, issued, preparation)

    first = await broker.execute(
        ToolName.SEATUNNEL_SUBMIT, issued.token, _submit_args(), ctx, session=async_session, now=NOW
    )
    assert first.ok
    assert calls == ["seatunnel.submit:5"]

    with pytest.raises(AppError) as exc:
        await broker.execute(
            ToolName.SEATUNNEL_SUBMIT, issued.token, _submit_args(), ctx, session=async_session, now=NOW
        )
    assert exc.value.code is ErrorCode.CAPABILITY_REPLAYED
    # 核心断言：第二次没有真的再提交一次
    assert calls == ["seatunnel.submit:5"]


async def test_重放被拒时记账并标reject_reason(broker, ctx, async_session, issuer, preparation) -> None:
    issued = _issue(issuer, preparation)
    await _register_capability(async_session, issued, preparation)
    await broker.execute(
        ToolName.SEATUNNEL_SUBMIT, issued.token, _submit_args(), ctx, session=async_session, now=NOW
    )
    with pytest.raises(AppError):
        await broker.execute(
            ToolName.SEATUNNEL_SUBMIT, issued.token, _submit_args(), ctx, session=async_session, now=NOW
        )

    row = (
        await async_session.execute(select(Capability).where(Capability.jti == issued.claims.jti))
    ).scalar_one()
    assert row.reject_reason == "replay"
    assert row.consumed_at is not None


async def test_未登记的能力令牌无法消费(broker, ctx, async_session, issuer, preparation, calls) -> None:
    """令牌是真的、但库里没有对应行 —— 不放行（PG 是事实来源）。"""
    issued = _issue(issuer, preparation)
    with pytest.raises(AppError) as exc:
        await broker.execute(
            ToolName.SEATUNNEL_SUBMIT, issued.token, _submit_args(), ctx, session=async_session, now=NOW
        )
    assert exc.value.code is ErrorCode.CAPABILITY_REPLAYED
    assert calls == []


# ---------------------------------------------------------------- ⑥ 前置条件


async def test_preparation未批准被拒(broker, ctx, async_session, issuer, calls) -> None:
    # status 是 preparation 唯一可变的列，所以这一条可以就地构造
    prep = await _make_preparation(async_session, status=PreparationStatus.PARTIALLY_APPROVED)
    issued = _issue(issuer, prep)
    await _register_capability(async_session, issued, prep)

    with pytest.raises(AppError) as exc:
        await broker.execute(
            ToolName.SEATUNNEL_SUBMIT, issued.token, _submit_args(), ctx, session=async_session, now=NOW
        )
    assert exc.value.code is ErrorCode.PREPARATION_EXPIRED
    assert calls == []


async def test_preparation过期被拒(broker, ctx, async_session, issuer, calls) -> None:
    prep = await _make_preparation(async_session, expires_at=NOW - timedelta(minutes=1))
    issued = _issue(issuer, prep)
    await _register_capability(async_session, issued, prep)

    with pytest.raises(AppError) as exc:
        await broker.execute(
            ToolName.SEATUNNEL_SUBMIT, issued.token, _submit_args(), ctx, session=async_session, now=NOW
        )
    assert exc.value.code is ErrorCode.PREPARATION_EXPIRED
    assert calls == []


async def test_质量未过禁止发布(async_session, broker, issuer, preparation, calls) -> None:
    """P0 动作在质量未过时放行 = 把脏数据直接推给业务方。"""
    issued = issuer.issue(
        preparation_id=preparation.id,
        subject_id=OPERATOR,
        tool=ToolName.TARGET_PUBLISH_SWAP,
        env=preparation.environment,
        artifact_sha256=preparation.artifact_sha256,
        tenant_id=TENANT,
        project_id=PROJECT,
        now=NOW,
    )
    await _register_capability(async_session, issued, preparation)
    run = ExecutionRun(
        tenant_id=TENANT,
        project_id=PROJECT,
        pipeline_version_id=1,
        preparation_id=preparation.id,
        idempotency_key="k1",
        status=RunStatus.SUCCEEDED,
        shadow_table="s",
        formal_table="f",
    )
    async_session.add(run)
    await async_session.flush()
    async_session.add(
        QualityReport(
            run_id=run.id,
            passed=False,
            generated_at=NOW,
        )
    )
    await async_session.flush()

    swap_ctx = ExecutionContext(
        request_id="r",
        tenant_id=TENANT,
        project_id=PROJECT,
        actor_id=OPERATOR,
        caller="worker",
        run_id=run.id,
    )
    with pytest.raises(AppError) as exc:
        await broker.execute(
            ToolName.TARGET_PUBLISH_SWAP,
            issued.token,
            {
                "run_id": run.id,
                "shadow_table": "s",
                "formal_table": "f",
                "quality_report_id": 1,
            },
            swap_ctx,
            session=async_session,
            now=NOW,
        )
    assert exc.value.code is ErrorCode.POLICY_BLOCKED
    assert calls == []


# ---------------------------------------------------------------- ⑦ args 与执行


async def test_args多余字段被拒且不写账本(broker, ctx, async_session, issuer, preparation) -> None:
    issued = _issue(issuer, preparation)
    await _register_capability(async_session, issued, preparation)
    bad = {**_submit_args(), "conn_str": "mysql://u:p@h/db"}

    with pytest.raises(AppError) as exc:
        await broker.execute(
            ToolName.SEATUNNEL_SUBMIT, issued.token, bad, ctx, session=async_session, now=NOW
        )
    assert exc.value.code is ErrorCode.VALIDATION_ERROR
    assert "mysql://" not in str(exc.value.details)
    # 参数问题是笔误而非协议拒绝，写进去会让不可变账本被噪声淹没
    assert await _rejections(async_session) == []


async def test_args模型拒绝凭据形态的值() -> None:
    """字段清单堵死了 ``password`` 这类键，但堵不住把 DSN 塞进合法键名。

    ``masking_policy`` 是自由形状字段（脱敏规则表），正是最容易被用来夹带的地方。
    """
    with pytest.raises(ValueError, match="凭据"):
        MetadataProbeArgs(
            data_source_id=1, profile_id=1, masking_policy={"loc": {"dsn": "mysql://root:pw@h:3306/db"}}
        )
    # 连接信息本身不在入参里：给了也会被 extra=forbid 顶回去
    with pytest.raises(ValueError, match=r"[Ee]xtra"):
        MetadataProbeArgs(data_source_id=1, profile_id=1, target={"host": "h"})  # type: ignore[call-arg]


async def test_成功路径执行并落账(broker, ctx, async_session, issuer, preparation) -> None:
    issued = _issue(issuer, preparation)
    await _register_capability(async_session, issued, preparation)

    result = await broker.execute(
        ToolName.SEATUNNEL_SUBMIT, issued.token, _submit_args(), ctx, session=async_session, now=NOW
    )
    assert isinstance(result, ToolResult)
    assert result.ok
    assert result.jti == issued.claims.jti
    assert result.evidence_seq is not None
    # detail 白名单：raw_row 这类嵌套/未知键被裁掉
    assert set(result.detail) <= {"job_id", "shadow_table"}

    events = (
        await async_session.execute(
            select(EvidenceEvent).where(EvidenceEvent.event_type == EvidenceEventType.CAPABILITY_CONSUMED)
        )
    ).scalars()
    assert [e.subject_id for e in events] == [str(issued.claims.jti)]


async def test_免签工具成功执行(broker, async_session, calls) -> None:
    api_ctx = ExecutionContext(
        request_id="r", tenant_id=TENANT, project_id=PROJECT, actor_id=OPERATOR, caller="api"
    )
    result = await broker.execute(
        ToolName.METADATA_PROBE, None, {"data_source_id": 1, "profile_id": 1}, api_ctx, session=async_session, now=NOW
    )
    assert result.ok
    assert result.jti is None
    assert calls == ["metadata.probe"]
    # 白名单外的键（含疑似连接串）被裁掉，不会进不可变账本
    assert "internal_dsn" not in result.detail


async def test_每条拒绝都写账本(broker, ctx, async_session, calls) -> None:
    """逐个触发典型拒绝路径，确认账本条目数与拒绝次数一致。"""
    attempts = [
        (ToolName.SEATUNNEL_SUBMIT, None, _submit_args()),  # 缺 token
        (ToolName.METADATA_PROBE, "x.y.z", {"data_source_id": 1, "profile_id": 1}),  # 免签却带 token
    ]
    for tool, token, args in attempts:
        with pytest.raises(AppError):
            await broker.execute(tool, token, args, ctx, session=async_session)

    rows = await _rejections(async_session)
    assert len(rows) == 2
    assert {r.payload["reject_stage"] for r in rows} == {"requirement"}


# ---------------------------------------------------------------- 验签器直接用法


def test_verifier完整校验可独立使用(verifier, issuer, preparation) -> None:
    """``verify`` 是「已握有可信期望值」时的完整入口，ToolBroker 走的是拆开的两步。"""
    issued = issuer.issue(
        preparation_id=1,
        subject_id=OPERATOR,
        tool=ToolName.SEATUNNEL_SUBMIT,
        env=Env.PROD,
        artifact_sha256=ARTIFACT,
        tenant_id=TENANT,
        project_id=PROJECT,
        now=NOW,
    )
    claims = verifier.verify(
        issued.token,
        ExpectedBinding(
            subject_id=OPERATOR,
            tool=ToolName.SEATUNNEL_SUBMIT,
            env=Env.PROD,
            artifact_sha256=ARTIFACT,
            tenant_id=TENANT,
            project_id=PROJECT,
        ),
        now=NOW,
    )
    assert claims.jti == issued.claims.jti
    assert claims.preparation_id == 1


def test_令牌原文不落token_sha256以外(issuer, preparation) -> None:
    issued = _issue(issuer, preparation)
    assert issued.token_sha256 == __import__("hashlib").sha256(issued.token.encode()).hexdigest()
    assert issued.token not in issued.token_sha256


def test_httpx_imported() -> None:
    # 占位：确保 httpx 在本模块可用（集成测试会用到 ASGI 客户端）
    assert httpx is not None


def test_secret_ref_arg不含值字段() -> None:
    """引用型取值的形状必须堵死「顺带把值捎过去」。"""
    ref = SecretRefArg(path="kv/etl/project-11/ds-abcd", version=3, key_names=("username",))
    assert "value" not in ref.model_dump()
    with pytest.raises(ValueError):
        SecretRefArg(path="p", version=1, password="x")  # type: ignore[call-arg]


@pytest.mark.parametrize(
    ("tool", "contract_keys"),
    [
        # 键名取自「执行器返回什么」（clients/datasource_probe.py）与
        # 「下游读什么」（services/datasource.py、tasks/datasource.py）。
        (
            "datasource.connectivity_test",
            {"state", "latency_ms", "target", "server_version", "error_code", "message"},
        ),
        (
            "metadata.probe",
            {"profile_id", "version", "tables_count", "target", "error_code", "message"},
        ),
    ],
)
def test_工具结论字段都在_detail_白名单内(tool: str, contract_keys: set[str]) -> None:
    """白名单少一个词，结论会被**静默改写**成默认值——这条钉住两个工具的契约键。

    真实事故：``state`` 不在白名单里（表里写的是 ``status``），
    ToolBroker 把它丢掉只留一行 ``tool_detail_keys_dropped`` 警告，
    下游 ``detail.get("state", "failed")`` 回落到默认值——MySQL 连上了、
    latency 与 server_version 都拿到了，前端却永远显示 failed。

    为什么单测发现不了：直接调执行器不经过 broker，白名单根本没参与；
    只有把真链路跑起来（API → Celery → ToolBroker → 执行器）才会暴露。
    """
    from harness.broker import _ALLOWED_DETAIL_KEYS

    missing = contract_keys - _ALLOWED_DETAIL_KEYS
    assert not missing, f"{tool} 的这些结论键会被白名单丢弃：{sorted(missing)}"
