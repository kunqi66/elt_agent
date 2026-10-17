"""be-03 数据源与密钥引用。

**本文件的核心断言只有一句：凭据值不出现在任何返回值、账本条目或异常消息里。**
其余用例（路径、权限、引用计数）都是围绕它的外围保证——因为「无明文 Secret」
是唯一一条一旦破了就无法事后补救的红线（值一旦落进账本或日志，就永远在那里了）。

Vault 用内存假实现替换。真 Vault 在 ECS 上，本机没有 Docker；
而这里要验的是**我们怎么用它**（写在前、读字段白名单、删在库之后），不是 Vault 本身。
"""

from __future__ import annotations

from typing import Any

import fakeredis.aioredis
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.errors import ConflictError, NotFoundError, RoleRequiredError, ValidationError
from models.datasource import DataSource, MetadataProfile, SecretReference
from models.enums import DataSourceType, Env, EvidenceEventType, SensitiveLevel
from models.evidence import EvidenceEvent
from models.pipeline import Pipeline
from models.tenant import AppUser, Tenant
from schemas.auth import SubjectContext
from schemas.datasource import (
    DataSourceCreate,
    DataSourceFilter,
    DataSourceUpdate,
    validate_endpoint,
)
from schemas.project import ProjectCreate
from services import datasource as ds
from services import project as project_service

SECRET_PATH_MARKER = "elt/"


# ---------------------------------------------------------------- 假 Vault


class FakeVault:
    """内存 Vault，只为验证调用契约。

    ``writes`` 记录每次写入，用来断言「库提交失败时 Vault 里多了什么」这类顺序性质。
    """

    def __init__(self) -> None:
        self.store: dict[str, dict[int, dict[str, str]]] = {}
        self.writes: list[tuple[str, dict[str, str]]] = []
        self.deleted: list[str] = []
        self.fail_write: bool = False

    async def write_secret(self, *, path: str, values: dict[str, str], cas: int | None = None) -> int:
        if self.fail_write:
            raise RuntimeError("vault down")
        self.writes.append((path, dict(values)))
        versions = self.store.setdefault(path, {})
        version = max(versions, default=0) + 1
        versions[version] = dict(values)
        return version

    async def read_secret(self, *, path: str, version: int | None = None) -> dict[str, Any]:
        versions = self.store.get(path, {})
        chosen = version or max(versions, default=0)
        if chosen not in versions:
            raise KeyError(path)
        return {"data": dict(versions[chosen]), "version": chosen}

    async def read_fields(
        self, *, path: str, version: int | None = None, key_names: tuple[str, ...] | list[str]
    ) -> dict[str, str]:
        payload = await self.read_secret(path=path, version=version)
        return {k: payload["data"][k] for k in key_names}

    async def delete_metadata(self, *, path: str) -> None:
        self.deleted.append(path)
        self.store.pop(path, None)


@pytest.fixture
def vault(monkeypatch: pytest.MonkeyPatch) -> FakeVault:
    fake = FakeVault()
    # 替换 from_settings：service 每次调用都现构造客户端，所以换工厂就够，
    # 不需要把 fake 注入到某个对象属性上
    monkeypatch.setattr("clients.vault.VaultClient.from_settings", classmethod(lambda cls: fake))
    return fake


@pytest.fixture(autouse=True)
def fake_redis(monkeypatch: pytest.MonkeyPatch) -> fakeredis.aioredis.FakeRedis:
    """连通性结果缓存在 Redis；本机没有 Redis 服务，必须换掉。

    patch 的是 ``core.redis.get_redis``（而不是某个模块的局部引用）：
    service 里一律写成函数内 ``from core.redis import get_redis``，
    正是为了让「当前连接的 Redis」始终只有一个可替换的入口。
    """
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr("core.redis.get_redis", lambda: client)
    return client


# ---------------------------------------------------------------- 种子


async def _bootstrap(session: AsyncSession) -> tuple[Tenant, AppUser, SubjectContext, int]:
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


def _create(project_id: int, **overrides: Any) -> DataSourceCreate:
    fields: dict[str, Any] = {
        "project_id": project_id,
        "name": "源库",
        "type": DataSourceType.MYSQL,
        "env": Env.DEV,
        "sensitive_level": SensitiveLevel.INTERNAL,
        "endpoint": {"host": "10.0.0.5", "port": 3306, "database": "orders"},
        "tags": ["demo"],
        "credentials": {"username": "etl", "password": "s3cr3t-value"},
    }
    fields.update(overrides)
    return DataSourceCreate(**fields)


async def _events(session: AsyncSession, event_type: EvidenceEventType) -> list[EvidenceEvent]:
    return list(
        (await session.execute(select(EvidenceEvent).where(EvidenceEvent.event_type == event_type)))
        .scalars()
        .all()
    )


# ---------------------------------------------------------------- 无明文 Secret


async def test_响应里没有凭据值(async_session: AsyncSession, vault: FakeVault) -> None:
    """最核心的一条。凭据一旦漏进响应或账本，事后无法补救。"""
    _, _, ctx, project_id = await _bootstrap(async_session)
    await async_session.commit()

    read = await ds.create_data_source(async_session, ctx, _create(project_id))
    dumped = read.model_dump_json()

    assert "s3cr3t-value" not in dumped
    assert "etl" not in dumped.replace("elt/", "")  # username 也不回
    assert read.secret_ref is not None
    assert read.secret_ref.key_names == ["password", "username"]
    assert read.secret_ref.version == 1
    assert read.secret_ref.path.startswith(SECRET_PATH_MARKER)


async def test_账本里没有凭据值(async_session: AsyncSession, vault: FakeVault) -> None:
    """证据账本是**不可变**的：漏一次就永久留在那里，且会被导出给审计。"""
    _, _, ctx, project_id = await _bootstrap(async_session)
    await async_session.commit()
    await ds.create_data_source(async_session, ctx, _create(project_id))

    created = await _events(async_session, EvidenceEventType.DATASOURCE_CREATED)
    materialized = await _events(async_session, EvidenceEventType.SECRET_MATERIALIZED)
    assert len(created) == 1
    assert len(materialized) == 1

    blob = "".join(e.payload.__repr__() for e in created + materialized)
    assert "s3cr3t-value" not in blob
    # 键名不是秘密（它会出现在 API 响应里），值是
    assert materialized[0].payload["key_names"] == ["password", "username"]


async def test_endpoint禁止携带凭据(async_session: AsyncSession, vault: FakeVault) -> None:
    """端点的 extra=forbid 是第一道闸，entity 层再兜一层。"""
    _, _, ctx, project_id = await _bootstrap(async_session)
    await async_session.commit()
    with pytest.raises(Exception) as exc:
        await ds.create_data_source(
            async_session,
            ctx,
            _create(project_id, endpoint={"host": "h", "database": "d", "password": "oops"}),
        )
    assert "oops" not in str(exc.value)


def test_凭据键名白名单() -> None:
    with pytest.raises(ValueError, match="不支持的凭据字段"):
        _create(1, credentials={"passwd": "x"})


def test_端点类型校验会归一化默认值() -> None:
    """``exec_host`` / ``exec_port`` 会以显式 ``None`` 落进归一化结果。

    这两个键是「数据面视角的地址」（控制面走隧道时，容器看到的地址不是 127.0.0.1）。
    缺省时它们**照样出现在 dict 里**而不是被丢掉：落库的形状与最终渲染 JDBC 连接串时
    读的形状必须一致，否则「少一个键」与「值为 None」在下游是两种代码路径
    （``endpoint.get("exec_host")`` 两种都兜得住，但 ``endpoint["exec_host"]`` 只兜得住一种）。
    """
    assert validate_endpoint(DataSourceType.MYSQL, {"host": "h", "database": "d"}) == {
        "host": "h",
        "port": 3306,
        "database": "d",
        "charset": "utf8mb4",
        "exec_host": None,
        "exec_port": None,
    }


# ---------------------------------------------------------------- 类型与凭据匹配


async def test_不支持凭据的类型拒绝凭据(async_session: AsyncSession, vault: FakeVault) -> None:
    """给 CSV 数据源塞密码：当场拒绝，而不是安静写进 Vault 再没人说得清它有什么用。"""
    _, _, ctx, project_id = await _bootstrap(async_session)
    await async_session.commit()
    with pytest.raises(ValidationError, match="不支持这些凭据字段"):
        await ds.create_data_source(
            async_session,
            ctx,
            _create(
                project_id,
                type=DataSourceType.CSV,
                endpoint={"path": "/data/a.csv"},
                credentials={"password": "x"},
            ),
        )
    assert vault.writes == []  # 校验在写 Vault 之前


async def test_无凭据数据源也能建(async_session: AsyncSession, vault: FakeVault) -> None:
    _, _, ctx, project_id = await _bootstrap(async_session)
    await async_session.commit()
    read = await ds.create_data_source(
        async_session,
        ctx,
        _create(project_id, type=DataSourceType.CSV, endpoint={"path": "/data/a.csv"}, credentials=None),
    )
    assert read.secret_ref is None
    assert vault.writes == []


# ---------------------------------------------------------------- 顺序不变量


async def test_库提交失败时vault里只多一个孤儿版本(
    async_session: AsyncSession, vault: FakeVault, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Vault 先写：失败方向是「多一个没人引用的版本」而不是「指向空密钥的数据源」。

    后者在列表页看起来完全正常，一测就炸，且没有任何字段能表达「这条其实没凭据」。
    """
    _, _, ctx, project_id = await _bootstrap(async_session)
    await async_session.commit()
    original_flush = async_session.flush
    calls = {"n": 0}

    async def flaky_flush(*args: Any, **kwargs: Any) -> None:
        calls["n"] += 1
        # 第 2 次 flush 恰好是 data_source 行落库那一步
        if calls["n"] == 2:
            raise RuntimeError("db down")
        await original_flush(*args, **kwargs)

    monkeypatch.setattr(async_session, "flush", flaky_flush)
    with pytest.raises(RuntimeError, match="db down"):
        await ds.create_data_source(async_session, ctx, _create(project_id))

    assert len(vault.writes) == 1  # 孤儿版本，无害
    rows = (await async_session.execute(select(DataSource))).scalars().all()
    assert rows == []  # 但库里没有一条指向空密钥的记录


async def test_重设凭据产生新版本而不是新路径(async_session: AsyncSession, vault: FakeVault) -> None:
    """版本号是回退的唯一单位；换路径就回不去了。"""
    _, _, ctx, project_id = await _bootstrap(async_session)
    await async_session.commit()
    first = await ds.create_data_source(async_session, ctx, _create(project_id))
    second = await ds.set_credentials(
        async_session, ctx, first.id, {"username": "etl2", "password": "new-secret"}
    )
    assert second.secret_ref is not None
    assert second.secret_ref.path == first.secret_ref.path
    assert second.secret_ref.version == 2


# ---------------------------------------------------------------- 权限


async def test_非成员读写都被拒(async_session: AsyncSession, vault: FakeVault) -> None:
    tenant, _, _, project_id = await _bootstrap(async_session)
    outsider = AppUser(
        tenant_id=tenant.id,
        username="mallory",
        display_name="Mallory",
        email="m@acme.test",
        password_hash="x",
        status="active",
    )
    async_session.add(outsider)
    await async_session.flush()
    await async_session.commit()
    other = SubjectContext(
        user_id=outsider.id,
        tenant_id=tenant.id,
        username="mallory",
        display_name="Mallory",
        request_id="r",
    )
    with pytest.raises(RoleRequiredError):
        await ds.create_data_source(async_session, other, _create(project_id))
    with pytest.raises(RoleRequiredError):
        await ds.list_data_sources(async_session, other, DataSourceFilter(project_id=project_id))


async def test_跨租户一律404(async_session: AsyncSession, vault: FakeVault) -> None:
    """越权与不存在回同一个码：回 403 等于确认了这个 id 真实存在。"""
    _, _, ctx, project_id = await _bootstrap(async_session)
    await async_session.commit()
    read = await ds.create_data_source(async_session, ctx, _create(project_id))

    other_tenant = Tenant(code="evil", name="Evil", status="active")
    async_session.add(other_tenant)
    await async_session.flush()
    intruder = AppUser(
        tenant_id=other_tenant.id,
        username="eve",
        display_name="Eve",
        email="e@evil.test",
        password_hash="x",
        status="active",
    )
    async_session.add(intruder)
    await async_session.flush()
    evil_ctx = SubjectContext(
        user_id=intruder.id,
        tenant_id=other_tenant.id,
        username="eve",
        display_name="Eve",
        request_id="r",
    )
    with pytest.raises(NotFoundError):
        await ds.get_data_source(async_session, evil_ctx, read.id)


# ---------------------------------------------------------------- 列表与更新


async def test_列表筛选与分页(async_session: AsyncSession, vault: FakeVault) -> None:
    _, _, ctx, project_id = await _bootstrap(async_session)
    await async_session.commit()
    for name, ds_type, endpoint in (
        ("mysql-a", DataSourceType.MYSQL, {"host": "h", "database": "d"}),
        ("mysql-b", DataSourceType.MYSQL, {"host": "h2", "database": "d"}),
        ("csv-a", DataSourceType.CSV, {"path": "/a.csv"}),
    ):
        await ds.create_data_source(
            async_session,
            ctx,
            _create(project_id, name=name, type=ds_type, endpoint=endpoint, credentials=None),
        )

    items, total = await ds.list_data_sources(
        async_session, ctx, DataSourceFilter(project_id=project_id, type="mysql")
    )
    assert total == 2
    assert {i.name for i in items} == {"mysql-a", "mysql-b"}

    items, total = await ds.list_data_sources(
        async_session, ctx, DataSourceFilter(project_id=project_id, q="csv")
    )
    assert total == 1
    assert items[0].name == "csv-a"

    items, total = await ds.list_data_sources(
        async_session, ctx, DataSourceFilter(project_id=project_id, page=2, page_size=2)
    )
    assert total == 3
    assert len(items) == 1


async def test_列表带出最近探查概要(async_session: AsyncSession, vault: FakeVault) -> None:
    """列表页的「最近探查」列；不 join 的话前端只能对每行再发一次请求。"""
    _, _, ctx, project_id = await _bootstrap(async_session)
    await async_session.commit()
    read = await ds.create_data_source(async_session, ctx, _create(project_id))
    async_session.add(
        MetadataProfile(
            tenant_id=ctx.tenant_id,
            project_id=project_id,
            data_source_id=read.id,
            version=1,
            status="ready",
            tables_count=7,
        )
    )
    await async_session.flush()

    items, _ = await ds.list_data_sources(async_session, ctx, DataSourceFilter(project_id=project_id))
    assert items[0].latest_profile is not None
    assert items[0].latest_profile.version == 1
    assert items[0].latest_profile.tables_count == 7


async def test_更新不带凭据则secret_ref不变(async_session: AsyncSession, vault: FakeVault) -> None:
    _, _, ctx, project_id = await _bootstrap(async_session)
    await async_session.commit()
    read = await ds.create_data_source(async_session, ctx, _create(project_id))
    updated = await ds.update_data_source(
        async_session, ctx, read.id, DataSourceUpdate(name="源库改", tags=["x"])
    )
    assert updated.name == "源库改"
    assert updated.secret_ref is not None
    assert updated.secret_ref.version == 1
    assert len(vault.writes) == 1


async def test_重名被拒(async_session: AsyncSession, vault: FakeVault) -> None:
    _, _, ctx, project_id = await _bootstrap(async_session)
    await async_session.commit()
    await ds.create_data_source(async_session, ctx, _create(project_id))
    with pytest.raises(ConflictError, match="同名"):
        await ds.create_data_source(async_session, ctx, _create(project_id))


# ---------------------------------------------------------------- 凭据重设


async def test_重设凭据必须全量(async_session: AsyncSession, vault: FakeVault) -> None:
    """部分更新会让同一个 Vault 版本里混着新旧值，而版本是回退的唯一单位。"""
    _, _, ctx, project_id = await _bootstrap(async_session)
    await async_session.commit()
    read = await ds.create_data_source(async_session, ctx, _create(project_id))
    with pytest.raises(ValidationError, match="全部凭据字段"):
        await ds.set_credentials(async_session, ctx, read.id, {"password": "only-pw"})
    assert len(vault.writes) == 1


# ---------------------------------------------------------------- 停用 / 删除


async def test_停用保留凭据(async_session: AsyncSession, vault: FakeVault) -> None:
    _, _, ctx, project_id = await _bootstrap(async_session)
    await async_session.commit()
    read = await ds.create_data_source(async_session, ctx, _create(project_id))
    off = await ds.set_status(async_session, ctx, read.id, enable=False)
    assert off.status == "disabled"
    assert off.secret_ref is not None  # 停用不是忘掉
    assert vault.deleted == []

    on = await ds.set_status(async_session, ctx, read.id, enable=True)
    assert on.status == "active"


async def test_被流水线引用时拒绝删除(async_session: AsyncSession, vault: FakeVault) -> None:
    """pipeline_version 是不可变制品，删掉它引用的数据源会让历史版本失去解释力。"""
    _, _, ctx, project_id = await _bootstrap(async_session)
    await async_session.commit()
    read = await ds.create_data_source(async_session, ctx, _create(project_id))

    async_session.add(
        Pipeline(
            tenant_id=ctx.tenant_id,
            project_id=project_id,
            name="p",
            status="draft",
            source_data_source_id=read.id,
        )
    )
    await async_session.flush()

    with pytest.raises(ConflictError) as exc:
        await ds.delete_data_source(async_session, ctx, read.id)
    assert exc.value.details["reference_count"] == 1
    assert vault.deleted == []


async def test_删除先库后vault(async_session: AsyncSession, vault: FakeVault) -> None:
    """反过来的话，Vault 删成功而库提交失败，会留下指向空路径的数据源。"""
    _, _, ctx, project_id = await _bootstrap(async_session)
    await async_session.commit()
    read = await ds.create_data_source(async_session, ctx, _create(project_id))
    path = read.secret_ref.path if read.secret_ref else None

    await ds.delete_data_source(async_session, ctx, read.id)
    assert vault.deleted == [path]
    remaining = (
        await async_session.execute(select(DataSource).where(DataSource.id == read.id))
    ).scalar_one_or_none()
    assert remaining is None
    refs = (await async_session.execute(select(SecretReference))).scalars().all()
    assert refs == []


# ---------------------------------------------------------------- 连通性测试


async def test_发起测试是202语义且不等待探测(
    async_session: AsyncSession, vault: FakeVault, monkeypatch: pytest.MonkeyPatch
) -> None:
    """请求只交出 test_id；探测在 worker 里跑。"""
    _, _, ctx, project_id = await _bootstrap(async_session)
    await async_session.commit()
    read = await ds.create_data_source(async_session, ctx, _create(project_id))

    dispatched: list[tuple[list[Any], str | None]] = []

    class FakeTask:
        def apply_async(self, *, args: list[Any], queue: str | None = None) -> None:
            dispatched.append((args, queue))

    monkeypatch.setattr("tasks.datasource.run_connectivity_test", FakeTask())

    payload = await ds.start_connectivity_test(async_session, ctx, read.id)
    assert payload["state"] == "pending"
    assert len(payload["test_id"]) == 32
    assert dispatched and dispatched[0][1] == "etl.default"

    # 结果立即可查，且是 pending 而非 404
    result = await ds.get_connectivity_test(async_session, ctx, read.id, payload["test_id"])
    assert result["state"] == "pending"


async def test_不支持的连接器拒绝测试(async_session: AsyncSession, vault: FakeVault) -> None:
    _, _, ctx, project_id = await _bootstrap(async_session)
    await async_session.commit()
    read = await ds.create_data_source(
        async_session,
        ctx,
        _create(project_id, type=DataSourceType.PARQUET, endpoint={"path": "/a.parquet"}, credentials=None),
    )
    with pytest.raises(ValidationError, match="尚未启用"):
        await ds.start_connectivity_test(async_session, ctx, read.id)


async def test_测试结果过期后查不到(async_session: AsyncSession, vault: FakeVault) -> None:
    _, _, ctx, project_id = await _bootstrap(async_session)
    await async_session.commit()
    read = await ds.create_data_source(async_session, ctx, _create(project_id))
    with pytest.raises(NotFoundError):
        await ds.get_connectivity_test(async_session, ctx, read.id, "0" * 32)


# ---------------------------------------------------------------- 引擎层掩码


def test_掩码目标不含库名() -> None:
    """库名在演示场景里常是业务实体名，泄漏它等于泄漏内部数据模型。"""
    from clients.datasource_probe import mask_target

    assert mask_target({"host": "10.0.0.5", "port": 3306, "database": "orders"}) == "10.**.**.5:3306"
    assert mask_target({"endpoint": "http://minio:9000", "bucket": "b"}) == "http://minio:9000"
    assert mask_target({"path": "/data/x.csv"}) == "/data/x.csv"


def test_驱动异常分类只按类名() -> None:
    """不 import 各驱动的异常类型：新加驱动忘了登记也不会 import 失败。"""
    from clients.datasource_probe import _classify
    from core.errors import ErrorCode

    class OperationalAuthError(Exception):
        pass

    assert _classify(OperationalAuthError("Access denied for user")).error_code is (
        ErrorCode.DATASOURCE_AUTH_FAILED
    )
    assert _classify(TimeoutError("timed out")).error_code is ErrorCode.DATASOURCE_TIMEOUT
    assert _classify(OSError("Connection refused")).error_code is ErrorCode.DATASOURCE_UNREACHABLE
