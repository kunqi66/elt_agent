"""be-04 元数据探查与脱敏。

**本文件的核心断言是一句话：明文样本从未离开执行器的那一个栈帧。**
外围用例（版本递增、权限、抽样范围）都服务于它——因为「脱敏默认开启」
是红线里唯一一条**破了之后往回查都查不出来**的：原文一旦落进
``masked_samples``，日志、备份、账本导出里都会跟着有一份。

分层验：

* ``engines.masking`` 是纯函数，直接调，不起库；
* 执行器需要一张 ``data_source`` 行，用假驱动替换 ``_schema_mysql`` / ``_sample_mysql``
  ——真 MySQL 在演示环境里，本机没有 Docker（CLAUDE.md §9），
  而这里要验的是「取回来的值怎么处理」，不是「怎么连 MySQL」。
"""

from __future__ import annotations

from typing import Any

import fakeredis.aioredis
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from clients import datasource_probe
from core.errors import ConflictError, NotFoundError, RoleRequiredError, ValidationError
from engines.masking import (
    SensitiveKind,
    default_masking_policy,
    digest,
    infer_sensitivity,
    mask_rows,
    mask_value,
    merge_policy,
    narrow,
)
from models.datasource import MetadataProfile
from models.enums import (
    DataSourceType,
    Env,
    EvidenceEventType,
    ProbeStatus,
    SensitiveLevel,
)
from models.evidence import EvidenceEvent
from models.tenant import AppUser, Tenant
from schemas.auth import SubjectContext
from schemas.datasource import ProbeRequest, ProbeSampling
from schemas.project import ProjectCreate
from services import datasource as ds
from services import metadata as md
from services import project as project_service

# 一个「一眼能认出来」的明文口令：断言「它不在任何地方」比断言「脱敏值长这样」更强，
# 因为前者能抓到「脱敏没生效」和「脱敏生效了但原文另外抄了一份」两种错
PLAINTEXT = "p@ssw0rd-plaintext-9f2a"
PHONE = "13800138000"
ID_CARD = "110101199003078888"


# ---------------------------------------------------------------- 假 Vault / Redis


class FakeVault:
    """只需要 ``write_secret``：本文件关心的是探查，不是凭据生命周期。"""

    def __init__(self) -> None:
        self.store: dict[str, dict[int, dict[str, str]]] = {}

    async def write_secret(self, *, path: str, values: dict[str, str], cas: int | None = None) -> int:
        versions = self.store.setdefault(path, {})
        version = max(versions, default=0) + 1
        versions[version] = dict(values)
        return version

    async def read_fields(
        self, *, path: str, version: int | None = None, key_names: list[str] | None = None
    ) -> dict[str, str]:
        values = self.store[path][version or max(self.store[path])]
        return {k: v for k, v in values.items() if key_names is None or k in key_names}

    async def delete_metadata(self, *, path: str) -> None:
        self.store.pop(path, None)


@pytest.fixture(autouse=True)
def fake_redis(monkeypatch: pytest.MonkeyPatch) -> fakeredis.aioredis.FakeRedis:
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr("core.redis.get_redis", lambda: client)
    return client


@pytest.fixture(autouse=True)
def fake_vault(monkeypatch: pytest.MonkeyPatch) -> FakeVault:
    """替换 Vault 客户端。

    autouse：``create_data_source`` 一定会写 Vault，漏掉一处就会去连真 Vault
    （本机没有），报错还是连接层的，看起来像测试基础设施坏了。
    """
    vault = FakeVault()
    monkeypatch.setattr("clients.vault.VaultClient.from_settings", classmethod(lambda cls: vault))
    return vault


# ---------------------------------------------------------------- 种子


async def _bootstrap(
    session: AsyncSession, *, roles: tuple[str, ...] = ("maker",)
) -> tuple[Tenant, AppUser, SubjectContext, int]:
    tenant = Tenant(code="acme", name="Acme", status="active")
    session.add(tenant)
    await session.flush()
    user = AppUser(
        tenant_id=tenant.id,
        username="alice",
        display_name="Alice",
        email="alice@acme.test",
        password_hash="x",
        status="active",
    )
    session.add(user)
    await session.flush()
    ctx = SubjectContext(
        user_id=user.id,
        tenant_id=tenant.id,
        username=user.username,
        display_name=user.display_name,
        request_id="req_1",
    )
    project = await project_service.create_project(
        session, ctx, ProjectCreate(code="orders", name="订单", env="dev")
    )
    return tenant, user, ctx, project.id


async def _make_datasource(session: AsyncSession, ctx: SubjectContext, project_id: int) -> int:
    from schemas.datasource import DataSourceCreate

    row = await ds.create_data_source(
        session,
        ctx,
        DataSourceCreate(
            project_id=project_id,
            name="源库",
            type=DataSourceType.MYSQL,
            env=Env.DEV,
            sensitive_level=SensitiveLevel.INTERNAL,
            endpoint={"host": "10.0.0.5", "port": 3306, "database": "orders"},
            tags=["demo"],
            credentials={"username": "etl", "password": PLAINTEXT},
        ),
    )
    return row.id


# ---------------------------------------------------------------- 引擎：判定


def test_列名判定覆盖常见敏感类别() -> None:
    cases = {
        "user_phone": SensitiveKind.PHONE,
        "mobile_no": SensitiveKind.PHONE,
        "email": SensitiveKind.EMAIL,
        "id_card_no": SensitiveKind.ID_CARD,
        "bank_card": SensitiveKind.BANK_CARD,
        "password": SensitiveKind.CREDENTIAL,
        "customer_name": SensitiveKind.NAME,
        "ship_address": SensitiveKind.ADDRESS,
        "remark": SensitiveKind.FREE_TEXT,
        "order_amount": SensitiveKind.NONE,
    }
    for column, expected in cases.items():
        kind, _ = infer_sensitivity(column)
        assert kind is expected, column


def test_身份证规则排在姓名规则之前() -> None:
    """``id_card`` 同时命中 ``id``/``card``/``name`` 一类的宽泛规则。

    规则表是有序的，先命中先赢——顺序错了会把身份证判成 NAME，
    级别从 restricted 掉到 confidential。
    """
    _, level = infer_sensitivity("user_id_card")
    assert level is SensitiveLevel.RESTRICTED


def test_普通列判定为内部级别而不是公开() -> None:
    """兜底是 internal 而不是 public。

    public 意味着**明文原样进样本**，把它当默认值等于默认不脱敏。
    """
    kind, level = infer_sensitivity("order_amount")
    assert kind is SensitiveKind.NONE
    assert level is SensitiveLevel.INTERNAL


def test_人工标注只能提高级别() -> None:
    """取两者较高者，不允许人工标注把手机号降级成 public。

    降级一旦可行，一次误标就能让整列明文进样本；取较高者最坏是过于保守。
    """
    _, raised = infer_sensitivity("order_amount", declared=SensitiveLevel.RESTRICTED)
    assert raised is SensitiveLevel.RESTRICTED
    _, kept = infer_sensitivity("user_phone", declared=SensitiveLevel.PUBLIC)
    assert kept is SensitiveLevel.CONFIDENTIAL


def test_注释也能触发判定() -> None:
    kind, _ = infer_sensitivity("col_a", comment="客户手机号")
    assert kind is SensitiveKind.PHONE


# ---------------------------------------------------------------- 引擎：脱敏


def _mask(value: Any, column: str = "user_phone") -> str:
    return mask_value(
        value,
        column=column,
        kind=SensitiveKind.PHONE,
        level=SensitiveLevel.CONFIDENTIAL,
        salt="s",
    )


def test_脱敏结果不可逆且不含原文() -> None:
    out = _mask(PHONE)
    assert PHONE not in out
    assert out.startswith("phone:")


def test_同值同显不同值可分辨() -> None:
    """同列同值必须得到同一个摘要。

    否则「按手机号分组」的演示场景会得到一堆长度为一的组，
    而演示数据看起来像随机噪声，评审会以为脱敏把数据毁了。
    """
    assert _mask(PHONE) == _mask(PHONE)
    assert _mask(PHONE) != _mask("13900139000")


def test_脱敏值不泄漏原值长度() -> None:
    short = _mask("1")
    long = _mask("1" * 64)
    assert len(short) == len(long)
    assert short != long


def test_跨列不可关联() -> None:
    """同一个值在不同列得到不同摘要。

    否则「phone 列里某个值」与「remark 列里同一串数字」能对上，
    跨列关联就成立了——脱敏的目的是切断关联，不只是遮住显示。
    """
    assert digest(PHONE, column="a", salt="s") != digest(PHONE, column="b", salt="s")


def test_空值脱敏为空串而不是哈希() -> None:
    """NULL 必须保持 NULL 语义。

    把 None 哈希成 ``phone:xxxx`` 会让前端分不清「这个字段是空的」
    与「这个字段有值但脱敏了」，而这两件事对取数方案的影响完全不同。
    """
    assert _mask(None) == ""


def test_公开列保留明文但可被策略收紧() -> None:
    args = {"column": "region", "kind": SensitiveKind.NONE, "level": SensitiveLevel.PUBLIC, "salt": "s"}
    assert mask_value("华东", **args) == "华东"
    tightened = mask_value("华东", **args, keep_public_plain=False)
    assert "华东" not in tightened


def test_整行脱敏沿用列级判定() -> None:
    rows = [{"phone": PHONE, "region": "华东"}, {"phone": "13900139000", "region": "华南"}]
    columns = {
        "phone": (SensitiveKind.PHONE, SensitiveLevel.CONFIDENTIAL),
        "region": (SensitiveKind.NONE, SensitiveLevel.PUBLIC),
    }
    out = mask_rows(rows, columns=columns, salt="s")
    assert out[0]["phone"] != out[1]["phone"]
    assert out[0]["region"] == "华东"
    assert all(isinstance(v, str) for row in out for v in row.values())


def test_策略只能收紧不能放松() -> None:
    policy = merge_policy({"keep_public_plain": False, "mode": "plaintext", "rules": []})
    assert policy["keep_public_plain"] is False
    # mode 与 rules 仍是默认值：请求体不能把脱敏模式换成明文
    assert policy["mode"] == default_masking_policy()["mode"]
    assert policy["rules"] == default_masking_policy()["rules"]
    # 但调用方提交了什么要留痕，审计才能对账
    assert policy["requested"]["mode"] == "plaintext"


def test_长文本被截断() -> None:
    assert len(str(narrow("x" * 500))) == 128
    assert narrow(7) == 7
    assert narrow(None) is None


# ---------------------------------------------------------------- service：提交探查


async def test_提交探查先建probing档案再投任务(
    async_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """顺序不能反：反了的话任务可能先跑完，去更新一个还不存在的 profile_id。"""
    _, _, ctx, project_id = await _bootstrap(async_session)
    data_source_id = await _make_datasource(async_session, ctx, project_id)

    queued: list[list[Any]] = []
    monkeypatch.setattr(
        "tasks.metadata.run_metadata_probe.apply_async",
        lambda **kw: queued.append(kw["args"]),
    )

    out = await md.start_probe(
        async_session,
        ctx,
        data_source_id,
        ProbeRequest(sampling=ProbeSampling(scope="schema_only", rows=0)),
    )

    row = (
        await async_session.execute(select(MetadataProfile).where(MetadataProfile.id == out["profile_id"]))
    ).scalar_one()
    assert row.status is ProbeStatus.PROBING
    assert row.version == 1
    assert queued and queued[0][1] == out["profile_id"]


async def test_版本号递增(async_session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    _, _, ctx, project_id = await _bootstrap(async_session)
    data_source_id = await _make_datasource(async_session, ctx, project_id)
    monkeypatch.setattr("tasks.metadata.run_metadata_probe.apply_async", lambda **kw: None)

    payload = ProbeRequest(sampling=ProbeSampling(scope="schema_only", rows=0))
    first = await md.start_probe(async_session, ctx, data_source_id, payload)
    second = await md.start_probe(async_session, ctx, data_source_id, payload)
    assert (first["version"], second["version"]) == (1, 2)


async def test_schema_only不允许要样本(async_session: AsyncSession) -> None:
    """不静默忽略：调用方以为拿到了样本，前端只会显示一张空表。"""
    _, _, ctx, project_id = await _bootstrap(async_session)
    data_source_id = await _make_datasource(async_session, ctx, project_id)
    with pytest.raises(ValidationError):
        await md.start_probe(
            async_session,
            ctx,
            data_source_id,
            ProbeRequest(sampling=ProbeSampling(scope="schema_only", rows=100)),
        )


async def test_选中表但没给表名被拒(async_session: AsyncSession) -> None:
    _, _, ctx, project_id = await _bootstrap(async_session)
    data_source_id = await _make_datasource(async_session, ctx, project_id)
    with pytest.raises(ValidationError):
        # ProbeSampling 自身也会拦，但 service 不能依赖调用方一定走过 DTO
        await md.start_probe(
            async_session,
            ctx,
            data_source_id,
            ProbeRequest.model_construct(
                sampling=ProbeSampling.model_construct(scope="selected_tables", tables=None, rows=10),
                masking_policy={},
            ),
        )


def test_auditor不在数据源管理者之列() -> None:
    """auditor 的职责是事后核证。

    能改被审计对象的人就无法保证「他看到的账本与当时的事实一致」——
    这条判定在纯函数层，服务端与前端置灰都引它，只有一份实现。
    """
    from engines.policy import can_manage_datasource, can_read_datasource
    from models.enums import RoleName

    assert can_manage_datasource({RoleName.AUDITOR}) is False
    assert can_read_datasource({RoleName.AUDITOR}) is True
    assert can_manage_datasource({RoleName.MAKER, RoleName.CHECKER_DATA}) is True


async def test_项目外的人不能提交探查(async_session: AsyncSession) -> None:
    """跨租户已经在 _load 里做成 404；能走到职责判定说明「项目里确实没有你」。"""
    tenant, _, ctx, project_id = await _bootstrap(async_session)
    data_source_id = await _make_datasource(async_session, ctx, project_id)

    outsider = AppUser(
        tenant_id=tenant.id,
        username="mallory",
        display_name="Mallory",
        email="mallory@acme.test",
        password_hash="x",
        status="active",
    )
    async_session.add(outsider)
    await async_session.flush()
    outsider_ctx = SubjectContext(
        user_id=outsider.id,
        tenant_id=tenant.id,
        username=outsider.username,
        display_name=outsider.display_name,
        request_id="req_2",
    )

    with pytest.raises(RoleRequiredError):
        await md.start_probe(
            async_session,
            outsider_ctx,
            data_source_id,
            ProbeRequest(sampling=ProbeSampling(scope="schema_only", rows=0)),
        )
    with pytest.raises(RoleRequiredError):
        await md.list_profiles(async_session, outsider_ctx, data_source_id)


# ---------------------------------------------------------------- service：读取


async def _seed_ready_profile(
    session: AsyncSession,
    ctx: SubjectContext,
    data_source_id: int,
    *,
    status: ProbeStatus = ProbeStatus.READY,
    samples: dict[str, Any] | None = None,
    error: str | None = None,
) -> MetadataProfile:
    """直接落一行档案。

    不走执行器是为了让读取用例只验读取路径——否则一个失败会同时指向
    「执行器没写成」和「读取没读对」，排查时要先做一遍二分。
    """
    row = MetadataProfile(
        tenant_id=ctx.tenant_id,
        project_id=1,
        data_source_id=data_source_id,
        version=1,
        status=status,
        schema_snapshot={
            "tables": [
                {
                    "schema": "orders",
                    "name": "customer",
                    "comment": "客户表",
                    "row_count_approx": 1200,
                    "columns": [
                        {
                            "name": "phone",
                            "data_type": "varchar(20)",
                            "nullable": False,
                            "is_primary_key": False,
                            "comment": None,
                            "sensitive_level": "confidential",
                            "masked": True,
                        }
                    ],
                },
                {
                    "schema": "orders",
                    "name": "orders",
                    "comment": None,
                    "row_count_approx": 50000,
                    "columns": [],
                },
            ]
        },
        masked_samples=samples if samples is not None else {"customer": [{"phone": "phone:abc123"}]},
        sampling={"rows": 100, "scope": "full_tables", "tables": None},
        masking_policy=default_masking_policy(),
        tables_count=2,
        sha256="f" * 64,
        error_message=error,
        created_by=ctx.user_id,
    )
    session.add(row)
    await session.commit()
    return row


async def test_档案列表按版本倒序(async_session: AsyncSession) -> None:
    _, _, ctx, project_id = await _bootstrap(async_session)
    data_source_id = await _make_datasource(async_session, ctx, project_id)
    first = await _seed_ready_profile(async_session, ctx, data_source_id)
    second = MetadataProfile(
        tenant_id=ctx.tenant_id,
        project_id=first.project_id,
        data_source_id=data_source_id,
        version=2,
        status=ProbeStatus.PROBING,
        schema_snapshot={},
        masked_samples={},
        sampling={},
        masking_policy={},
        tables_count=0,
        sha256="",
    )
    async_session.add(second)
    await async_session.commit()

    briefs = await md.list_profiles(async_session, ctx, data_source_id)
    assert [b.version for b in briefs] == [2, 1]


async def test_probing档案详情不是404(async_session: AsyncSession) -> None:
    """中间态要能查到，否则前端轮询第一下就当失败。"""
    _, _, ctx, project_id = await _bootstrap(async_session)
    data_source_id = await _make_datasource(async_session, ctx, project_id)
    await _seed_ready_profile(async_session, ctx, data_source_id, status=ProbeStatus.PROBING)

    detail = await md.get_profile(async_session, ctx, data_source_id, 1)
    assert detail.status is ProbeStatus.PROBING
    assert detail.created_by_display_name == "Alice"


async def test_跨租户读档案404(async_session: AsyncSession) -> None:
    """tenant 过滤少了这一个条件，猜 id 就能读到别人的库结构。"""
    _, _, ctx, project_id = await _bootstrap(async_session)
    data_source_id = await _make_datasource(async_session, ctx, project_id)

    other_tenant = Tenant(code="globex", name="Globex", status="active")
    async_session.add(other_tenant)
    await async_session.flush()
    other = SubjectContext(
        user_id=ctx.user_id, tenant_id=other_tenant.id, username="bob", display_name="Bob", request_id="r2"
    )
    with pytest.raises(NotFoundError):
        await md.get_schema(async_session, other, data_source_id, 1)


async def test_失败档案读结构报冲突而不是空树(async_session: AsyncSession) -> None:
    """空树会被前端渲染成「这个库没有任何表」，真相是「这次没探成功」。"""
    _, _, ctx, project_id = await _bootstrap(async_session)
    data_source_id = await _make_datasource(async_session, ctx, project_id)
    await _seed_ready_profile(
        async_session, ctx, data_source_id, status=ProbeStatus.FAILED, error="目标拒绝连接"
    )
    with pytest.raises(ConflictError):
        await md.get_schema(async_session, ctx, data_source_id, 1)


async def test_结构可按表名过滤(async_session: AsyncSession) -> None:
    _, _, ctx, project_id = await _bootstrap(async_session)
    data_source_id = await _make_datasource(async_session, ctx, project_id)
    await _seed_ready_profile(async_session, ctx, data_source_id)

    snapshot = await md.get_schema(async_session, ctx, data_source_id, 1, table="customer")
    assert [t.name for t in snapshot.tables] == ["customer"]
    assert snapshot.tables[0].columns[0].sensitive_level is SensitiveLevel.CONFIDENTIAL
    assert snapshot.tables[0].columns[0].null_ratio is None


async def test_样本分页与截断标记(async_session: AsyncSession) -> None:
    _, _, ctx, project_id = await _bootstrap(async_session)
    data_source_id = await _make_datasource(async_session, ctx, project_id)
    rows = [{"phone": f"phone:{i:012d}"} for i in range(10)]
    await _seed_ready_profile(async_session, ctx, data_source_id, samples={"customer": rows})

    page = await md.get_samples(async_session, ctx, data_source_id, 1, table="customer", limit=4)
    assert len(page.rows) == 4
    assert page.truncated is True
    full = await md.get_samples(async_session, ctx, data_source_id, 1, table="customer", limit=50)
    assert full.truncated is False


async def test_未抽样的表给出已抽样清单(async_session: AsyncSession) -> None:
    """「有结构没样本」与「表名打错了」在响应上要能区分。"""
    _, _, ctx, project_id = await _bootstrap(async_session)
    data_source_id = await _make_datasource(async_session, ctx, project_id)
    await _seed_ready_profile(async_session, ctx, data_source_id)

    with pytest.raises(NotFoundError) as exc:
        await md.get_samples(async_session, ctx, data_source_id, 1, table="orders", limit=10)
    assert exc.value.details["sampled_tables"] == ["customer"]


async def test_未完成的档案不给样本(async_session: AsyncSession) -> None:
    _, _, ctx, project_id = await _bootstrap(async_session)
    data_source_id = await _make_datasource(async_session, ctx, project_id)
    await _seed_ready_profile(async_session, ctx, data_source_id, status=ProbeStatus.PROBING)
    with pytest.raises(ConflictError):
        await md.get_samples(async_session, ctx, data_source_id, 1, table="customer", limit=10)


# ---------------------------------------------------------------- 执行器：明文不出栈帧


CUSTOMER_COLUMNS = [
    {
        "schema": "orders",
        "name": "customer",
        "comment": "客户表",
        "row_count_approx": 2,
        "columns": [
            {
                "name": "id",
                "data_type": "bigint",
                "nullable": False,
                "is_primary_key": True,
                "comment": None,
            },
            {
                "name": "phone",
                "data_type": "varchar(20)",
                "nullable": True,
                "is_primary_key": False,
                "comment": "手机号",
            },
            {
                "name": "region",
                "data_type": "varchar(16)",
                "nullable": True,
                "is_primary_key": False,
                "comment": None,
            },
        ],
    }
]


RAW_ROWS = {
    "customer": [
        {"id": 1, "phone": PHONE, "region": "华东"},
        {"id": 2, "phone": "13900139000", "region": "华南"},
    ]
}


@pytest.fixture
def fake_driver(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """替换掉「怎么连 MySQL」，只留「拿到值之后怎么处理」。

    驱动本身不在这里测：真 MySQL 在演示环境（CLAUDE.md §9），
    而 ``_sample_mysql`` 的职责只有「取回来」，它没有脱敏能力——
    脱敏必须发生在**所有驱动之上**，否则新增一个驱动就等于新增一个漏点。
    """
    captured: dict[str, Any] = {"samples_returned": None}

    async def fake_schema(*_: Any, **__: Any) -> list[dict[str, Any]]:
        # 深拷贝：执行器会就地把判定结果写进列字典，共享字面量会让下一个用例
        # 看到上一个用例留下的 sensitive_level
        import copy

        return copy.deepcopy(CUSTOMER_COLUMNS)

    async def fake_sample(*_: Any, **__: Any) -> dict[str, list[dict[str, Any]]]:
        import copy

        return copy.deepcopy(RAW_ROWS)

    async def fake_creds(row: Any) -> dict[str, str]:
        # 返回明文凭据。执行器拿到它之后应当只用它连库、绝不外传
        return {"username": "etl", "password": PLAINTEXT}

    monkeypatch.setattr(datasource_probe, "_schema_mysql", fake_schema)
    monkeypatch.setattr(datasource_probe, "_sample_mysql", fake_sample)
    monkeypatch.setattr(datasource_probe, "_materialize_credentials", fake_creds)
    return captured


async def _run_executor(
    session: AsyncSession, ctx: SubjectContext, data_source_id: int, profile_id: int
) -> dict[str, Any]:
    from harness.broker import ExecutionContext
    from harness.tools import MetadataProbeArgs

    exec_ctx = ExecutionContext(
        request_id="r",
        tenant_id=ctx.tenant_id,
        project_id=1,
        actor_id=ctx.user_id,
        caller="worker",
    )
    return dict(
        await datasource_probe.metadata_probe_executor(
            MetadataProbeArgs(
                data_source_id=data_source_id,
                profile_id=profile_id,
                scope="full_tables",
                sample_rows=100,
            ),
            exec_ctx,
            session,
        )
    )


async def test_执行器落档案且样本已脱敏(async_session: AsyncSession, fake_driver: dict[str, Any]) -> None:
    _, _, ctx, project_id = await _bootstrap(async_session)
    data_source_id = await _make_datasource(async_session, ctx, project_id)
    profile = await _seed_ready_profile(
        async_session, ctx, data_source_id, status=ProbeStatus.PROBING, samples={}
    )

    out = await _run_executor(async_session, ctx, data_source_id, profile.id)

    await async_session.refresh(profile)
    assert profile.status is ProbeStatus.READY
    assert out["profile_id"] == profile.id
    # 返回值只有扁平键：broker 的 detail 白名单会把嵌套值整个丢掉，
    # 而表结构/样本走领域表（这是有意的，账本不该装一棵 schema 树）
    assert set(out) == {"profile_id", "version", "tables_count", "target"}

    dumped = str(profile.masked_samples)
    assert PHONE not in dumped
    assert "13900139000" not in dumped
    assert dumped.count("phone:") == 2

    # 同一列同值同显：两条不同的手机号必须得到两个不同的摘要
    phones = [row["phone"] for row in profile.masked_samples["customer"]]
    assert len(set(phones)) == 2


async def test_内部级别列也必须脱敏(async_session: AsyncSession, fake_driver: dict[str, Any]) -> None:
    """只有 public 才保留明文，internal 同样要脱。

    这条最容易写错：判定的默认级别是 internal，而「没命中敏感词」不等于「可以明文」——
    否则整张表的普通字段会全部以原文进样本，脱敏形同虚设。
    """
    _, _, ctx, project_id = await _bootstrap(async_session)
    data_source_id = await _make_datasource(async_session, ctx, project_id)
    profile = await _seed_ready_profile(
        async_session, ctx, data_source_id, status=ProbeStatus.PROBING, samples={}
    )
    await _run_executor(async_session, ctx, data_source_id, profile.id)
    await async_session.refresh(profile)

    regions = [row["region"] for row in profile.masked_samples["customer"]]
    assert all(r.startswith("masked:") for r in regions)
    assert "华东" not in regions and "华南" not in regions
    # 同列不同值仍可分辨，同值同显
    assert len(set(regions)) == 2
    salt = f"{ctx.tenant_id}:{data_source_id}:1"
    assert regions[0] == "masked:" + digest("华东", column="region", salt=salt)


async def test_凭据不出现在档案与返回值(async_session: AsyncSession, fake_driver: dict[str, Any]) -> None:
    _, _, ctx, project_id = await _bootstrap(async_session)
    data_source_id = await _make_datasource(async_session, ctx, project_id)
    profile = await _seed_ready_profile(
        async_session, ctx, data_source_id, status=ProbeStatus.PROBING, samples={}
    )

    out = await _run_executor(async_session, ctx, data_source_id, profile.id)
    await async_session.refresh(profile)

    blob = f"{out} {profile.schema_snapshot} {profile.masked_samples} {profile.masking_policy}"
    # 口令来自假 Vault 的读回值，从头到尾只该活在执行器的局部变量里
    assert PLAINTEXT not in blob
    assert "etl" not in blob


async def test_结构快照里存的是判定结果不是值(
    async_session: AsyncSession, fake_driver: dict[str, Any]
) -> None:
    _, _, ctx, project_id = await _bootstrap(async_session)
    data_source_id = await _make_datasource(async_session, ctx, project_id)
    profile = await _seed_ready_profile(
        async_session, ctx, data_source_id, status=ProbeStatus.PROBING, samples={}
    )
    await _run_executor(async_session, ctx, data_source_id, profile.id)
    await async_session.refresh(profile)

    table = profile.schema_snapshot["tables"][0]
    by_name = {c["name"]: c for c in table["columns"]}
    assert by_name["phone"]["sensitive_level"] == "confidential"
    assert by_name["phone"]["masked"] is True
    # id 是主键，列名不含敏感词 → internal → 仍要脱（internal 不等于 public）
    assert by_name["id"]["masked"] is True
    assert by_name["phone"]["comment"] == "手机号"
    # 内部键不得落库
    assert all(not k.startswith("_") for c in table["columns"] for k in c)


async def test_快照指纹稳定(async_session: AsyncSession, fake_driver: dict[str, Any]) -> None:
    """同一个库两次探查必须得到同一个 sha256。

    否则「元数据没变」永远无法证明，PipelineVersion 绑定的指纹也就失去了意义。
    """
    _, _, ctx, project_id = await _bootstrap(async_session)
    data_source_id = await _make_datasource(async_session, ctx, project_id)
    digests: list[str] = []
    for version in (1, 2):
        profile = MetadataProfile(
            tenant_id=ctx.tenant_id,
            project_id=1,
            data_source_id=data_source_id,
            version=version,
            status=ProbeStatus.PROBING,
            schema_snapshot={},
            masked_samples={},
            sampling={},
            masking_policy={},
            tables_count=0,
            sha256="",
        )
        async_session.add(profile)
        await async_session.commit()
        await _run_executor(async_session, ctx, data_source_id, profile.id)
        await async_session.refresh(profile)
        digests.append(profile.sha256)

    assert digests[0] == digests[1]
    assert len(digests[0]) == 64


async def test_全库抽样只取前二十张表(async_session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """``full_tables`` 只抽前 20 张：全库抽样在宽表场景下会读掉几百万行。

    断言的是**传给驱动的是哪几张表**，而不是「结果里有几行」——
    后者取决于驱动桩给了什么，验不出上限。
    """
    import copy

    _, _, ctx, project_id = await _bootstrap(async_session)
    data_source_id = await _make_datasource(async_session, ctx, project_id)
    profile = await _seed_ready_profile(
        async_session, ctx, data_source_id, status=ProbeStatus.PROBING, samples={}
    )

    tables = [
        {
            "schema": "orders",
            "name": f"t{i:02d}",
            "comment": None,
            "row_count_approx": 0,
            "columns": [
                {
                    "name": "id",
                    "data_type": "bigint",
                    "nullable": False,
                    "is_primary_key": True,
                    "comment": None,
                }
            ],
        }
        for i in range(30)
    ]
    asked: list[list[str]] = []

    async def fake_schema(*_: Any, **__: Any) -> list[dict[str, Any]]:
        return copy.deepcopy(tables)

    async def fake_sample(*args: Any, **__: Any) -> dict[str, list[dict[str, Any]]]:
        asked.append(list(args[3]))
        return {}

    async def fake_creds(row: Any) -> dict[str, str]:
        return {}

    monkeypatch.setattr(datasource_probe, "_schema_mysql", fake_schema)
    monkeypatch.setattr(datasource_probe, "_sample_mysql", fake_sample)
    monkeypatch.setattr(datasource_probe, "_materialize_credentials", fake_creds)

    await _run_executor(async_session, ctx, data_source_id, profile.id)
    await async_session.refresh(profile)

    assert len(asked) == 1
    assert len(asked[0]) == 20
    assert asked[0][0] == "t00"
    assert profile.tables_count == 30


# ---------------------------------------------------------------- 执行器：失败路径


async def test_源库读不了时档案落failed(async_session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """执行器把「连不上」当成一次成功的调用 + 失败的结论，而不是抛异常。

    抛异常的话档案会停在 probing，前端轮询到天荒地老也等不到终态。
    """
    _, _, ctx, project_id = await _bootstrap(async_session)
    data_source_id = await _make_datasource(async_session, ctx, project_id)
    profile = await _seed_ready_profile(
        async_session, ctx, data_source_id, status=ProbeStatus.PROBING, samples={}
    )

    async def boom(*_: Any, **__: Any) -> Any:
        raise datasource_probe.ProbeFailure(datasource_probe.ErrorCode.DATASOURCE_UNREACHABLE, "目标拒绝连接")

    async def fake_creds(row: Any) -> dict[str, str]:
        return {}

    monkeypatch.setattr(datasource_probe, "_schema_mysql", boom)
    monkeypatch.setattr(datasource_probe, "_materialize_credentials", fake_creds)

    out = await _run_executor(async_session, ctx, data_source_id, profile.id)
    await async_session.refresh(profile)

    assert profile.status is ProbeStatus.FAILED
    assert profile.error_message == "目标拒绝连接"
    assert out["error_code"] == "DATASOURCE_UNREACHABLE"
    # 失败也要留下「当时按什么规则脱的」——那是失败档案唯一的诊断价值
    assert profile.masking_policy["mode"] == "hash_prefix"


async def test_驱动原生异常也被收敛成失败终态(
    async_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """只捕 ProbeFailure 是不够的：连上之后的游标报错会以驱动原生异常逃逸。"""
    _, _, ctx, project_id = await _bootstrap(async_session)
    data_source_id = await _make_datasource(async_session, ctx, project_id)
    profile = await _seed_ready_profile(
        async_session, ctx, data_source_id, status=ProbeStatus.PROBING, samples={}
    )

    async def boom(*_: Any, **__: Any) -> Any:
        raise RuntimeError("(1142, \"SELECT command denied to user 'etl'@'10.0.0.9'\")")

    async def fake_creds(row: Any) -> dict[str, str]:
        return {}

    monkeypatch.setattr(datasource_probe, "_schema_mysql", boom)
    monkeypatch.setattr(datasource_probe, "_materialize_credentials", fake_creds)

    out = await _run_executor(async_session, ctx, data_source_id, profile.id)
    await async_session.refresh(profile)

    assert profile.status is ProbeStatus.FAILED
    # 驱动原文不进 error_message：它可能带用户名与主机，而 error_message 会进 API 响应
    assert "etl" not in (profile.error_message or "")
    assert out["error_code"] == "DATASOURCE_UNREACHABLE"


async def test_目标档案不属于本租户时拒绝写入(async_session: AsyncSession) -> None:
    _, _, ctx, project_id = await _bootstrap(async_session)
    data_source_id = await _make_datasource(async_session, ctx, project_id)
    with pytest.raises(Exception, match="元数据档案"):
        await _run_executor(async_session, ctx, data_source_id, 99999)


async def test_服务层兜底把逃逸的档案置终态(
    async_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """broker 在执行器之前就拒绝时，档案不能永远停在 probing。"""
    _, _, ctx, project_id = await _bootstrap(async_session)
    data_source_id = await _make_datasource(async_session, ctx, project_id)
    profile = await _seed_ready_profile(
        async_session, ctx, data_source_id, status=ProbeStatus.PROBING, samples={}
    )

    async def boom(*_: Any, **__: Any) -> Any:
        raise RuntimeError("broker 在拿执行器之前就报了")

    monkeypatch.setattr("harness.broker.ToolBroker.execute", boom)
    with pytest.raises(RuntimeError):
        await md.run_probe(
            async_session,
            data_source_id=data_source_id,
            profile_id=profile.id,
            tenant_id=ctx.tenant_id,
            project_id=1,
            user_id=ctx.user_id,
            request_id="r",
            scope="schema_only",
            tables=(),
            sample_rows=0,
            masking_policy={},
        )
    await async_session.refresh(profile)
    assert profile.status is ProbeStatus.FAILED


async def test_探查写账本(async_session: AsyncSession) -> None:
    """账本事件在 service 层写：执行器拿不到 ledger，也不该拿到。"""
    from harness.ledger import LedgerEvent

    _, _, ctx, project_id = await _bootstrap(async_session)
    data_source_id = await _make_datasource(async_session, ctx, project_id)

    await md._ledger.append(
        async_session,
        LedgerEvent(
            tenant_id=ctx.tenant_id,
            project_id=1,
            actor_id=ctx.user_id,
            event_type=EvidenceEventType.METADATA_PROBED,
            subject_type="data_source",
            subject_id=str(data_source_id),
            payload={"profile_id": 1, "tables_count": 2},
        ),
    )
    await async_session.commit()

    events = list(
        (
            await async_session.execute(
                select(EvidenceEvent).where(EvidenceEvent.event_type == EvidenceEventType.METADATA_PROBED)
            )
        )
        .scalars()
        .all()
    )
    assert len(events) == 1
    assert events[0].event_hash


async def test_数据源被删除后任务空转(async_session: AsyncSession) -> None:
    """档案随数据源级联删除；任务照跑只会写一条孤儿行，所以要早退。"""
    state = await md.load_profile_state(async_session, tenant_id=1, profile_id=424242)
    assert state is None


async def test_数据源停用后拒绝探查(async_session: AsyncSession) -> None:
    _, _, ctx, project_id = await _bootstrap(async_session)
    data_source_id = await _make_datasource(async_session, ctx, project_id)
    await ds.set_status(async_session, ctx, data_source_id, enable=False)
    with pytest.raises(ConflictError):
        await md.start_probe(
            async_session,
            ctx,
            data_source_id,
            ProbeRequest(sampling=ProbeSampling(scope="schema_only", rows=0)),
        )


async def test_行数上限被入参模型拦住() -> None:
    """``sample_rows`` 的上限在 args 模型上，而不是在执行器里 if 一下。

    模型是 broker 的第 ⑦ 步闸门，越界请求根本到不了执行器。
    """
    from pydantic import ValidationError as PydanticValidationError

    from harness.tools import MetadataProbeArgs

    with pytest.raises(PydanticValidationError):
        MetadataProbeArgs(data_source_id=1, profile_id=1, sample_rows=100000)


# ---------------------------------------------------------------------------
# 真机才暴露的一类 bug：驱动回显的列名大小写
# ---------------------------------------------------------------------------


class _FakeCursor:
    """假 DictCursor：按 SQL 关键字回不同的行，行的键名可以指定大小写。"""

    def __init__(self, tables: list[dict[str, Any]], columns: list[dict[str, Any]]) -> None:
        self._tables = tables
        self._columns = columns
        self._current: list[dict[str, Any]] = []

    async def execute(self, sql: str, params: object = None) -> None:
        self._current = self._columns if "information_schema.columns" in sql else self._tables

    async def fetchall(self) -> list[dict[str, Any]]:
        return list(self._current)

    async def __aenter__(self) -> _FakeCursor:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


class _FakeConn:
    def __init__(self, cursor: _FakeCursor) -> None:
        self._cursor = cursor

    def cursor(self, _kind: object) -> _FakeCursor:
        return self._cursor

    def close(self) -> None:
        """必须同步关：aiomysql 的 ``close()`` 就是同步的，写 async 会让真代码 `await` 不到。"""


class _FakeAiomysql:
    DictCursor = object()

    def __init__(self, cursor: _FakeCursor) -> None:
        self._cursor = cursor

    async def connect(self, **_kwargs: Any) -> _FakeConn:
        return _FakeConn(self._cursor)


async def test_真解析代码吃得下大写列名(monkeypatch: pytest.MonkeyPatch) -> None:
    """MySQL 8 的 DictCursor 回的是大写键名，``_schema_mysql`` 必须照样解析出表与列。

    这条守的是一个**在本机单测里完全看不见**的 bug：原先写的是
    ``row["table_name"] or row["TABLE_NAME"]``，第一个下标在 MySQL 8 上直接 KeyError
    （dict 的键名大小写敏感，``or`` 的兜底根本没机会执行），而它又被 ``_classify``
    归成「数据源不可达 / 连接失败」——排查方向被带去查网络、凭据、Vault，真因在解析代码里。

    为什么既有用例拦不住：它们把 ``_schema_mysql`` 整个替换成假函数，
    于是**从没执行过真解析代码**。本用例把假驱动下沉一层（假 aiomysql 模块），
    跑的是真的 ``_schema_mysql``。
    """
    import sys

    cursor = _FakeCursor(
        tables=[
            {"TABLE_NAME": "orders", "TABLE_COMMENT": "订单", "TABLE_ROWS": 11},
            {"TABLE_NAME": "users", "TABLE_COMMENT": None, "TABLE_ROWS": 5},
        ],
        columns=[
            {
                "TABLE_NAME": "orders",
                "COLUMN_NAME": "id",
                "COLUMN_TYPE": "bigint",
                "IS_NULLABLE": "NO",
                "COLUMN_KEY": "PRI",
                "COLUMN_COMMENT": "主键",
            },
            {
                "TABLE_NAME": "orders",
                "COLUMN_NAME": "email",
                "COLUMN_TYPE": "varchar(64)",
                "IS_NULLABLE": "YES",
                "COLUMN_KEY": "",
                "COLUMN_COMMENT": None,
            },
            {
                "TABLE_NAME": "users",
                "COLUMN_NAME": "phone",
                "COLUMN_TYPE": "varchar(20)",
                "IS_NULLABLE": "YES",
                "COLUMN_KEY": "",
                "COLUMN_COMMENT": "手机号",
            },
        ],
    )
    monkeypatch.setitem(sys.modules, "aiomysql", _FakeAiomysql(cursor))

    tables = await datasource_probe._schema_mysql(
        {"host": "127.0.0.1", "port": 3307, "database": "demo"},
        {"username": "u", "password": "p"},
        5,
        (),
    )

    assert [t["name"] for t in tables] == ["orders", "users"], "表名解析不出来或被写成了 None"
    assert [c["name"] for c in tables[0]["columns"]] == ["id", "email"]
    assert tables[0]["comment"] == "订单"
    assert tables[0]["row_count_approx"] == 11
    # 主键判定与可空判定都依赖另外几个键，一并钉住
    assert tables[0]["columns"][0]["is_primary_key"] is True
    assert tables[0]["columns"][0]["nullable"] is False
    assert tables[0]["columns"][1]["nullable"] is True


async def test_列名对不上时抛错而不是悄悄写_None(monkeypatch: pytest.MonkeyPatch) -> None:
    """两个大小写都取不到时必须抛错。

    返回 ``None`` 会被 ``str()`` 变成表名 ``"None"``：档案状态是 ready、结构树里
    却挂着一张名叫 None 的表——**错得安静**比错得响亮危险得多。
    """
    import sys

    cursor = _FakeCursor(
        tables=[{"ALIAS": "renamed"}],
        columns=[],
    )
    monkeypatch.setitem(sys.modules, "aiomysql", _FakeAiomysql(cursor))

    with pytest.raises(KeyError):
        await datasource_probe._schema_mysql(
            {"host": "127.0.0.1", "port": 3307, "database": "demo"},
            {"username": "u", "password": "p"},
            5,
            (),
        )


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (KeyError("table_name"), "INTERNAL_ERROR"),
        (TypeError("bad"), "INTERNAL_ERROR"),
        (AttributeError("nope"), "INTERNAL_ERROR"),
        (IndexError("idx"), "INTERNAL_ERROR"),
    ],
)
def test_自己的代码缺陷不冒充数据源不可达(exc: Exception, expected: str) -> None:
    """解析代码里的 KeyError 与「目标库连不上」是两回事，不能共用一个错误码。

    混为一谈的代价是实测过的：真因在 ``_schema_mysql`` 的键名大小写上，
    前端与日志却写着「连接失败」，于是排查跑去了网络与凭据。
    """
    failure = datasource_probe._classify(exc)
    assert failure.error_code.value == expected
    # 消息里只准有异常类型，不能带异常文本——文本里可能裹着源库的值
    assert type(exc).__name__ in failure.message
    assert str(exc) not in failure.message
