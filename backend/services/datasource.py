"""数据源、凭据引用与连通性测试的业务编排（be-03）。

本模块是全仓库唯一把**明文凭据**写进 Vault 的地方。三条不变量：

1. **写 Vault 在写库之前**。反过来的话，库提交成功而 Vault 写失败，
   会留下一个指向不存在密钥的数据源——列表页显示正常、一测就炸，
   而且没有任何字段能表达「这条记录其实没凭据」。Vault 先写则最坏是
   留一个没人引用的版本，属于无害垃圾。
2. **凭据只进不出**。``DataSourceRead`` 里只有 ``SecretRefView``（路径+版本+键名），
   没有任何形如 ``dict[str, str]`` 的凭据回显字段。
3. **连通性测试必须走 ToolBroker**。它是出网动作，绕开 broker 就绕开了
   工具注册表与证据账本——而「副作用唯一出口」的价值在于没有例外。

本层禁止 ``import fastapi``；跨层只传 Pydantic 模型，不传 ORM 实例。
"""

from __future__ import annotations

from typing import Any

import structlog
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from clients.vault import VaultClient, build_secret_path
from core.errors import ConflictError, NotFoundError, RoleRequiredError, ValidationError
from core.ids import utcnow
from engines.policy import can_manage_datasource, can_read_datasource
from harness.broker import ExecutionContext, ToolBroker
from harness.ledger import EvidenceLedger, LedgerEvent
from models.datasource import DataSource, MetadataProfile, SecretReference
from models.enums import DataSourceType, EvidenceEventType, ToolName
from models.pipeline import Pipeline
from models.tenant import AppUser
from schemas.auth import SubjectContext
from schemas.datasource import (
    CONNECTOR_REGISTRY,
    DataSourceCreate,
    DataSourceFilter,
    DataSourceRead,
    DataSourceUpdate,
    MetadataProfileBrief,
    SecretRefView,
    validate_endpoint,
)
from services.project import active_roles

log = structlog.get_logger(__name__)

_ledger = EvidenceLedger()


# ---------------------------------------------------------------- 权限


async def _require_project_roles(
    session: AsyncSession, ctx: SubjectContext, project_id: int, *, write: bool
) -> None:
    """取当前主体在该项目的职责槽并做能力位判定。

    读与写分开：`can_read_datasource` 允许任何职责槽，`can_manage_datasource`
    不含 auditor。**判定在服务端闭合**，不依赖前端置灰按钮（CLAUDE.md §5.8）。
    """
    roles = await active_roles(session, project_id=project_id, user_id=ctx.user_id)
    allowed = can_manage_datasource(roles) if write else can_read_datasource(roles)
    if not allowed:
        # 抛 RoleRequired 而不是 NotFound：跨租户已经在 _load 的 tenant 过滤里
        # 一律做成 404 了，能走到这里说明「项目里确实没有你」，此时明说差什么职责槽
        # 才有用——含糊其辞只会让人去猜自己是不是被移出了项目
        needed = "maker / operator / checker_security" if write else "本项目任一职责槽"
        raise RoleRequiredError(f"该操作需要 {needed}")


# ---------------------------------------------------------------- 取行


async def _load(session: AsyncSession, ctx: SubjectContext, data_source_id: int) -> DataSource:
    """按 id 取数据源，**强制带 tenant_id**（CLAUDE.md §4.7）。

    少这一个条件，跨租户就能靠猜 id 读到别人的连接信息（host/database 也是敏感信息）。
    """
    row = (
        await session.execute(
            select(DataSource).where(DataSource.id == data_source_id, DataSource.tenant_id == ctx.tenant_id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFoundError("数据源不存在")
    return row


async def _latest_profile(session: AsyncSession) -> Any:
    """最近一条探查概要的子查询，供列表 join。"""
    return select(
        MetadataProfile.data_source_id,
        MetadataProfile.version,
        MetadataProfile.status,
        MetadataProfile.tables_count,
        MetadataProfile.created_at,
        MetadataProfile.expires_at,
        func.row_number()
        .over(
            partition_by=MetadataProfile.data_source_id,
            order_by=MetadataProfile.version.desc(),
        )
        .label("rn"),
    ).subquery()


async def _to_read(
    session: AsyncSession, row: DataSource, *, latest: MetadataProfileBrief | None = None
) -> DataSourceRead:
    owner_name: str | None = None
    if row.owner_id is not None:
        # 显式带 tenant：owner_id 来自请求体，理论上可以被填成别的租户的用户
        owner_name = (
            await session.execute(
                select(AppUser.display_name).where(
                    AppUser.id == row.owner_id, AppUser.tenant_id == row.tenant_id
                )
            )
        ).scalar_one_or_none()

    ref = row.secret_ref
    return DataSourceRead(
        id=row.id,
        project_id=row.project_id,
        name=row.name,
        type=DataSourceType(row.type),
        env=row.env,
        sensitive_level=row.sensitive_level,
        endpoint=dict(row.endpoint or {}),
        secret_ref=(
            SecretRefView(
                provider="vault",
                path=ref.path,
                version=ref.version,
                key_names=list(ref.key_names or []),
                last_rotated_at=ref.last_rotated_at,
            )
            if ref is not None
            else None
        ),
        tags=list(row.tags or []),
        owner_id=row.owner_id,
        owner_display_name=owner_name,
        status=str(row.status),
        last_tested_at=row.last_tested_at,
        last_test_ok=row.last_test_ok,
        latest_profile=latest,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


# ---------------------------------------------------------------- 查询


async def list_data_sources(
    session: AsyncSession, ctx: SubjectContext, flt: DataSourceFilter
) -> tuple[list[DataSourceRead], int]:
    """返回 (页内条目, 总数)。分页由 API 层组装成 ``Page``。"""
    await _require_project_roles(session, ctx, flt.project_id, write=False)

    conditions = [
        DataSource.tenant_id == ctx.tenant_id,
        DataSource.project_id == flt.project_id,
    ]
    types = DataSourceFilter.split_csv(flt.type)
    if types:
        conditions.append(DataSource.type.in_([DataSourceType(t) for t in types]))
    envs = DataSourceFilter.split_csv(flt.env)
    if envs:
        conditions.append(DataSource.env.in_(envs))
    levels = DataSourceFilter.split_csv(flt.sensitive_level)
    if levels:
        conditions.append(DataSource.sensitive_level.in_(levels))
    if flt.status:
        conditions.append(DataSource.status == flt.status)
    if flt.q:
        # 只搜名字与标签。**刻意不搜 host**：那会让「按关键字搜索」
        # 变成一个能枚举内部主机名与网段的接口
        pattern = f"%{flt.q}%"
        conditions.append(or_(DataSource.name.ilike(pattern)))

    total = (
        await session.execute(select(func.count()).select_from(DataSource).where(*conditions))
    ).scalar_one()

    rows = (
        (
            await session.execute(
                select(DataSource)
                .where(*conditions)
                .order_by(DataSource.id.desc())
                .offset((flt.page - 1) * flt.page_size)
                .limit(flt.page_size)
            )
        )
        .scalars()
        .all()
    )

    latest_map = await _latest_profiles(session, [r.id for r in rows])
    items = [await _to_read(session, r, latest=latest_map.get(r.id)) for r in rows]
    return items, int(total)


async def _latest_profiles(
    session: AsyncSession, data_source_ids: list[int]
) -> dict[int, MetadataProfileBrief]:
    """批量取每个数据源的最新 profile。

    一条窗口函数查询取全部，而不是每个数据源查一次：列表页 20 条就是 20 次往返，
    在数据源详情页看起来「有点慢」，在列表页就是明显的卡顿。
    """
    if not data_source_ids:
        return {}
    sub = await _latest_profile(session)
    rows = (
        await session.execute(select(sub).where(sub.c.data_source_id.in_(data_source_ids), sub.c.rn == 1))
    ).all()
    return {
        int(r.data_source_id): MetadataProfileBrief(
            version=int(r.version),
            status=r.status,
            tables_count=int(r.tables_count),
            created_at=r.created_at,
            expires_at=r.expires_at,
        )
        for r in rows
    }


async def get_data_source(session: AsyncSession, ctx: SubjectContext, data_source_id: int) -> DataSourceRead:
    row = await _load(session, ctx, data_source_id)
    await _require_project_roles(session, ctx, row.project_id, write=False)
    latest = (await _latest_profiles(session, [row.id])).get(row.id)
    return await _to_read(session, row, latest=latest)


# ---------------------------------------------------------------- 写：凭据


async def _write_credentials(
    session: AsyncSession,
    ctx: SubjectContext,
    *,
    project_id: int,
    name: str,
    credentials: dict[str, str],
    existing: SecretReference | None,
) -> SecretReference:
    """把凭据写进 Vault，并 upsert ``secret_reference`` 行。

    **密钥路径按 (租户, 项目, 数据源名) 生成**。第三次重设凭据时，
    若沿用同一个数据源名，KV v2 会自然产生新版本——这正是我们要的：
    版本号是回退的依据，而不是靠另起一个路径。
    """
    path = (
        existing.path
        if existing is not None
        else build_secret_path(tenant_id=ctx.tenant_id, project_id=project_id, name=name)
    )
    vault = VaultClient.from_settings()
    version = await vault.write_secret(path=path, values=credentials)
    key_names = sorted(credentials)

    if existing is None:
        ref = SecretReference(
            tenant_id=ctx.tenant_id,
            project_id=project_id,
            provider="vault",
            path=path,
            version=version,
            key_names=key_names,
            created_by=ctx.user_id,
        )
        session.add(ref)
    else:
        ref = existing
        ref.version = version
        ref.key_names = key_names
        ref.last_rotated_at = utcnow()

    await session.flush()
    # ⚠️ 这里**不记任何凭据值**，连键名列表之外的信息都不进账本。
    # 键名不是秘密（它们会出现在 API 响应的 secret_ref 里），值是。
    await _ledger.append(
        session,
        LedgerEvent(
            tenant_id=ctx.tenant_id,
            project_id=project_id,
            actor_id=ctx.user_id,
            event_type=EvidenceEventType.SECRET_MATERIALIZED,
            subject_type="secret_reference",
            subject_id=str(ref.id),
            payload={"path": path, "version": version, "key_names": key_names},
        ),
    )
    return ref


# ---------------------------------------------------------------- 写：CRUD


async def create_data_source(
    session: AsyncSession, ctx: SubjectContext, payload: DataSourceCreate
) -> DataSourceRead:
    await _require_project_roles(session, ctx, payload.project_id, write=True)
    endpoint = validate_endpoint(payload.type, payload.endpoint)

    spec = CONNECTOR_REGISTRY[payload.type]
    if payload.credentials:
        unknown = sorted(set(payload.credentials) - set(spec.credential_keys))
        if unknown:
            # 只报键名。同时这条校验让「给 CSV 数据源塞密码」当场暴露，
            # 而不是安静地写进 Vault 然后谁也说不清它有什么用
            raise ValidationError(
                f"{spec.label} 不支持这些凭据字段：{', '.join(unknown)}",
                details={"allowed": list(spec.credential_keys)},
            )

    exists = (
        await session.execute(
            select(func.count())
            .select_from(DataSource)
            .where(
                DataSource.tenant_id == ctx.tenant_id,
                DataSource.project_id == payload.project_id,
                DataSource.name == payload.name,
            )
        )
    ).scalar_one()
    if exists:
        # 同项目内重名会让「选数据源」的下拉框出现两条一模一样的东西
        raise ConflictError("同一项目下已存在同名数据源")

    ref = None
    if payload.credentials:
        # Vault 先写、库后提交：见模块头的第 1 条不变量
        ref = await _write_credentials(
            session,
            ctx,
            project_id=payload.project_id,
            name=payload.name,
            credentials=payload.credentials,
            existing=None,
        )

    row = DataSource(
        tenant_id=ctx.tenant_id,
        project_id=payload.project_id,
        name=payload.name,
        type=payload.type,
        env=payload.env,
        endpoint=endpoint,
        secret_ref_id=ref.id if ref is not None else None,
        sensitive_level=payload.sensitive_level,
        tags=list(payload.tags),
        owner_id=payload.owner_id,
        status="active",
    )
    session.add(row)
    await session.flush()

    await _ledger.append(
        session,
        LedgerEvent(
            tenant_id=ctx.tenant_id,
            project_id=row.project_id,
            actor_id=ctx.user_id,
            event_type=EvidenceEventType.DATASOURCE_CREATED,
            subject_type="data_source",
            subject_id=str(row.id),
            # endpoint 里**只有非凭据字段**（extra=forbid 已保证），可以进账本
            payload={"name": row.name, "type": row.type.value, "env": row.env.value, "endpoint": endpoint},
        ),
    )
    await session.commit()
    return await _to_read(session, await _load(session, ctx, row.id))


async def update_data_source(
    session: AsyncSession, ctx: SubjectContext, data_source_id: int, payload: DataSourceUpdate
) -> DataSourceRead:
    row = await _load(session, ctx, data_source_id)
    await _require_project_roles(session, ctx, row.project_id, write=True)

    if payload.name is not None and payload.name != row.name:
        clash = (
            await session.execute(
                select(func.count())
                .select_from(DataSource)
                .where(
                    DataSource.tenant_id == ctx.tenant_id,
                    DataSource.project_id == row.project_id,
                    DataSource.name == payload.name,
                    DataSource.id != row.id,
                )
            )
        ).scalar_one()
        if clash:
            raise ConflictError("同一项目下已存在同名数据源")
        row.name = payload.name

    if payload.endpoint is not None:
        # type 不可改，所以 endpoint 仍按原类型校验
        row.endpoint = validate_endpoint(DataSourceType(row.type), payload.endpoint)
    if payload.env is not None:
        row.env = payload.env
    if payload.sensitive_level is not None:
        row.sensitive_level = payload.sensitive_level
    if payload.tags is not None:
        row.tags = list(payload.tags)
    if payload.owner_id is not None:
        row.owner_id = payload.owner_id

    if payload.credentials:
        spec = CONNECTOR_REGISTRY[DataSourceType(row.type)]
        unknown = sorted(set(payload.credentials) - set(spec.credential_keys))
        if unknown:
            raise ValidationError(f"{spec.label} 不支持这些凭据字段：{', '.join(unknown)}")
        await _write_credentials(
            session,
            ctx,
            project_id=row.project_id,
            name=row.name,
            credentials=payload.credentials,
            existing=row.secret_ref,
        )

    await session.commit()
    return await _to_read(session, await _load(session, ctx, data_source_id))


async def set_credentials(
    session: AsyncSession,
    ctx: SubjectContext,
    data_source_id: int,
    credentials: dict[str, str],
) -> DataSourceRead:
    """重新设置凭据（唯一接收凭据的接口之一）。

    前端契约要求**必须提交全部键**，不接受部分更新：部分更新会让同一个 Vault
    版本里混着新旧值，而版本号是唯一的回退单位——混值就等于无法回退。
    """
    row = await _load(session, ctx, data_source_id)
    await _require_project_roles(session, ctx, row.project_id, write=True)

    spec = CONNECTOR_REGISTRY[DataSourceType(row.type)]
    provided = set(credentials)
    allowed = set(spec.credential_keys)
    unknown = sorted(provided - allowed)
    if unknown:
        raise ValidationError(f"{spec.label} 不支持这些凭据字段：{', '.join(unknown)}")
    if row.secret_ref is not None:
        missing = sorted(allowed - provided)
        if missing:
            raise ValidationError("必须提交全部凭据字段（不允许部分更新）", details={"missing": missing})

    await _write_credentials(
        session,
        ctx,
        project_id=row.project_id,
        name=row.name,
        credentials=credentials,
        existing=row.secret_ref,
    )
    await session.commit()
    return await _to_read(session, await _load(session, ctx, data_source_id))


async def set_status(
    session: AsyncSession, ctx: SubjectContext, data_source_id: int, *, enable: bool
) -> DataSourceRead:
    """启用 / 停用。**不删凭据**：停用是「先别用它」，不是「忘掉它」。"""
    row = await _load(session, ctx, data_source_id)
    await _require_project_roles(session, ctx, row.project_id, write=True)
    row.status = "active" if enable else "disabled"
    await session.commit()
    return await _to_read(session, await _load(session, ctx, data_source_id))


async def _reference_count(session: AsyncSession, ctx: SubjectContext, data_source_id: int) -> int:
    """有多少个流水线引用了这个数据源（源或目标）。

    引用关系落在 ``pipeline`` 上（``source_data_source_id`` / ``target_data_source_id``），
    不是 ``pipeline_version``——版本行只记 plan/artifact，数据源归属在 pipeline 层。
    而 pipeline 已冻结的版本是**不能因为数据源没了就作废**的，所以两个都算引用：
    这里数 pipeline，是因为即使只有一个草稿状态的项目，删掉源库也会让它无法继续。
    """
    return int(
        (
            await session.execute(
                select(func.count())
                .select_from(Pipeline)
                .where(
                    Pipeline.tenant_id == ctx.tenant_id,
                    or_(
                        Pipeline.source_data_source_id == data_source_id,
                        Pipeline.target_data_source_id == data_source_id,
                    ),
                )
            )
        ).scalar_one()
    )


async def delete_data_source(session: AsyncSession, ctx: SubjectContext, data_source_id: int) -> None:
    """真删。被流水线引用时拒绝。

    **不做级联删除**：pipeline 一旦有了冻结版本，删掉它引用的数据源
    会让历史版本永久失去解释力（「这个版本当时读的是哪个库」变成未知）。
    所以这里回 409 并带上引用计数，让人先去停用而不是删。
    """
    row = await _load(session, ctx, data_source_id)
    await _require_project_roles(session, ctx, row.project_id, write=True)

    refs = await _reference_count(session, ctx, data_source_id)
    if refs:
        raise ConflictError(
            f"该数据源被 {refs} 个流水线引用，无法删除；请先停用",
            details={"reference_count": refs},
        )

    # 先删库、后删 Vault：反过来的话，Vault 删成功而库提交失败，
    # 会留下一条指向空路径的数据源——比留一个孤儿密钥版本糟糕得多
    ref = row.secret_ref
    path = ref.path if ref is not None else None
    await session.delete(row)
    if ref is not None:
        # secret_reference 只被这一条 data_source 引用（FK 是 RESTRICT 且没有别的表指向它）。
        # 不删的话会留下一条**声称 Vault 里某个路径还存在**的记录——
        # 而我们已经把那个路径删了，等于让审计线索指向一个不存在的凭据。
        await session.delete(ref)
    await session.commit()

    if path:
        try:
            await VaultClient.from_settings().delete_metadata(path=path)
        except Exception:
            log.warning("vault_cleanup_failed", data_source_id=data_source_id)


# ---------------------------------------------------------------- 连通性测试


async def start_connectivity_test(
    session: AsyncSession, ctx: SubjectContext, data_source_id: int
) -> dict[str, Any]:
    """发起一次连通性测试，立刻返回 pending，真正的探测投给 Celery。

    **为什么不同步做**：探测目标可能是不可达的主机，同步做就要在 API 请求里
    等满 ``connect_timeout_sec``。前端契约本身就是 202 + 轮询，
    所以这里只需把 test_id 交出去，探测在 worker 里跑。
    """
    row = await _load(session, ctx, data_source_id)
    await _require_project_roles(session, ctx, row.project_id, write=True)

    spec = CONNECTOR_REGISTRY[DataSourceType(row.type)]
    if not spec.connectable:
        raise ValidationError(f"{spec.label} 连接器尚未启用，无法测试连通性")

    test_id = new_test_id()
    await store_test_result(
        test_id,
        {
            "test_id": test_id,
            "state": "pending",
            "latency_ms": None,
            "target": None,
            "server_version": None,
            "error_code": None,
            "message": None,
            "tested_at": None,
        },
    )

    # 投递到 default 队列：api 进程的探测不该和 SeaTunnel 执行抢 etl.execute
    from tasks.datasource import run_connectivity_test

    run_connectivity_test.apply_async(
        # project_id 取自行本身而不是 ctx：SubjectContext 里没有 project_id
        # （同一个用户在多个项目里各有职责槽，项目是资源维度而不是令牌维度）
        args=[data_source_id, test_id, ctx.tenant_id, row.project_id, ctx.user_id, ctx.request_id],
        queue="etl.default",
    )
    return {"test_id": test_id, "state": "pending"}


async def get_connectivity_test(
    session: AsyncSession, ctx: SubjectContext, data_source_id: int, test_id: str
) -> dict[str, Any]:
    row = await _load(session, ctx, data_source_id)
    await _require_project_roles(session, ctx, row.project_id, write=False)

    cached = await load_test_result(test_id)
    if cached is None:
        # 结果 TTL 只有几分钟，查不到多半是「等太久」而不是「不存在」
        raise NotFoundError("测试结果已过期或不存在")
    return cached


async def run_probe(
    session: AsyncSession,
    data_source_id: int,
    *,
    tenant_id: int,
    project_id: int,
    user_id: int,
    request_id: str,
) -> dict[str, Any]:
    """执行一次连通性探测并把结论落库 + 写账本。

    由 Celery 任务调用（它自己开会话），所以这里收的是**标量参数**而不是
    ``SubjectContext``:一个已经跨进程边界的调用不该假装还握着请求上下文,
    传一个伪造的 ctx 进来只会让「谁在调用」这件事变得含糊。

    ``caller="worker"``：本函数确实跑在 Celery 进程里。之前写成 ``"api"`` 是为了
    凑合当时的「进程必须相等」检查，那让调用方在代码里说了假话；
    现在 broker 承认 worker→api 这个方向，就没有理由再写假身份。
    """
    broker = ToolBroker(guard=_build_guard(), ledger=_ledger)
    exec_ctx = ExecutionContext(
        request_id=request_id,
        tenant_id=tenant_id,
        project_id=project_id,
        actor_id=user_id,
        caller="worker",
    )
    result = await broker.execute(
        ToolName.DATASOURCE_CONNECTIVITY_TEST,
        None,  # P3 免 Capability
        {"data_source_id": data_source_id, "connect_timeout_sec": 5},
        exec_ctx,
        session=session,
    )

    detail = dict(result.detail)
    state = str(detail.get("state", "failed"))
    row = (
        await session.execute(
            select(DataSource).where(DataSource.id == data_source_id, DataSource.tenant_id == tenant_id)
        )
    ).scalar_one_or_none()
    if row is not None:
        row.last_tested_at = utcnow()
        row.last_test_ok = state == "succeeded"
        await _ledger.append(
            session,
            LedgerEvent(
                tenant_id=tenant_id,
                project_id=project_id,
                actor_id=user_id,
                event_type=EvidenceEventType.DATASOURCE_CONNECTIVITY_TESTED,
                subject_type="data_source",
                subject_id=str(data_source_id),
                payload={"state": state, "target": detail.get("target")},
            ),
        )
        await session.commit()
    return detail


# ---------------------------------------------------------------- 测试结果缓存


def new_test_id() -> str:
    from uuid import uuid4

    return uuid4().hex


def _test_key(test_id: str) -> str:
    return f"ds:test:{test_id}"


# 结果 TTL。前端轮询间隔是秒级，这个时长足够任何一次探测跑完并被抓到；
# 而它很短，所以不需要为「谁有权看这次结果」再做一层归属校验——
# uuid4 的 test_id 猜不中，且几分钟后自动消失。
_TEST_RESULT_TTL_SEC = 600


async def store_test_result(test_id: str, payload: dict[str, Any]) -> None:
    from core.redis import get_redis

    # 只存结构化结论，且这些字段本身都已是掩码后的形态（见 mask_target）
    await get_redis().set(_test_key(test_id), _json(payload), ex=_TEST_RESULT_TTL_SEC)


async def load_test_result(test_id: str) -> dict[str, Any] | None:
    from core.redis import get_redis

    raw = await get_redis().get(_test_key(test_id))
    return _unjson(raw) if raw else None


def _json(payload: dict[str, Any]) -> str:
    import json

    return json.dumps(payload, ensure_ascii=False)


def _unjson(raw: str | bytes) -> dict[str, Any]:
    import json

    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    parsed: dict[str, Any] = json.loads(raw)
    return parsed


def _build_guard() -> Any:
    """构造 ReplayGuard（P3 免签工具其实用不到它，但 broker 需要它非空）。"""
    from core.config import settings
    from core.redis import get_redis
    from harness.replay import ReplayGuard

    return ReplayGuard(redis=get_redis(), ttl_sec=settings.CAPABILITY_TTL_SEC)


__all__ = [
    "create_data_source",
    "load_test_result",
    "store_test_result",
    "delete_data_source",
    "get_connectivity_test",
    "get_data_source",
    "list_data_sources",
    "run_probe",
    "set_credentials",
    "set_status",
    "start_connectivity_test",
    "update_data_source",
]
