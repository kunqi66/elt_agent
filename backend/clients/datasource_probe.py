"""数据源探测执行器：连通性测试与元数据探查（be-03 / be-04）。

**这里是明文凭据唯一被物化的地方**（CLAUDE.md §5.2：仅允许 Worker/API 内存中的临时物化）。
凭据从 Vault 取出后只活在本次函数调用的局部变量里：

* 不写进 ``self``、不写进模块级缓存、不放进返回 dict；
* 异常里只回错误类型与掩码后的主机端口，不回 DSN；
* 探测失败时驱动抛出的原始异常**不向上传播**，只留在服务端日志。

为什么要走 ``ToolBroker`` 而不是 service 直接连库：连通性测试与元数据探查都是
**出网**动作。若允许 service 直接连，就会出现一条完全绕开工具注册表与证据账本的
出网路径——而「副作用唯一出口」的价值恰恰在于**没有例外**。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.errors import AppError, ErrorCode
from harness.tools import (
    DatasourceConnectivityTestArgs,
    MetadataProbeArgs,
    ToolExecutor,
    register_executor,
)
from models.enums import DataSourceType, ToolName

if TYPE_CHECKING:
    from harness.broker import ExecutionContext

log = structlog.get_logger(__name__)

# 演示数据面主机的掩码规则见 ``mask_target``。探测超时统一走 args 里的字段
_DEFAULT_TIMEOUT_SEC = 5


class ProbeFailure(Exception):
    """探测失败的统一内部异常。

    ``error_code`` 只取字典 §5.7 已定义的那三个（不可达 / 认证失败 / 超时），
    不按驱动各自的错误类型自造新码——前端已经按这三个做了文案映射。

    唯一的例外是 ``INTERNAL_ERROR``：它不表示「探测的结论」，而表示**我们自己的
    代码出错了**（见 ``_classify``）。前端同样有它的文案映射，所以不会漏文案。
    """

    def __init__(self, error_code: ErrorCode, message: str, *, server_version: str | None = None) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message
        self.server_version = server_version


def mask_target(endpoint: dict[str, Any]) -> str:
    """把探测目标压成可对外展示的形状：``10.**.**.5:3306``。

    **不返回库名**：库名在演示场景里常是业务实体名（``dw_demo.orders``），
    对外暴露它等于泄漏了内部数据模型。IP 中间段掩掉是因为连通性结果会被
    写进审计与前端提示，而它并不需要精确定位到主机。
    """
    host = str(endpoint.get("host") or endpoint.get("endpoint") or endpoint.get("path") or "?")
    port = endpoint.get("port")
    if host.count(".") == 3:
        parts = host.split(".")
        host = ".".join([parts[0], "**", "**", parts[-1]])
    elif "://" in host:
        # MinIO 之类的 endpoint：只留 scheme 与主机名，去掉 userinfo 与 path
        scheme, _, rest = host.partition("://")
        host = f"{scheme}://{rest.split('/')[0].split('@')[-1]}"
    return f"{host}:{port}" if port else host


# ---------------------------------------------------------------- 各类型探测


async def _probe_mysql(endpoint: dict[str, Any], creds: dict[str, str], timeout_sec: int) -> dict[str, Any]:
    """MySQL / Doris 都用 MySQL 协议，差别只在默认端口与版本串。"""
    import aiomysql

    try:
        conn = await asyncio.wait_for(
            aiomysql.connect(
                host=endpoint["host"],
                port=int(endpoint.get("port", 3306)),
                user=creds.get("username", ""),
                password=creds.get("password", ""),
                db=endpoint.get("database"),
                connect_timeout=timeout_sec,
                charset=endpoint.get("charset", "utf8mb4"),
            ),
            timeout=timeout_sec + 1,
        )
    except TimeoutError as exc:
        raise ProbeFailure(ErrorCode.DATASOURCE_TIMEOUT, f"连接超时（>{timeout_sec}s）") from exc
    except Exception as exc:
        raise _classify(exc) from exc

    try:
        async with conn.cursor() as cur:
            await cur.execute("SELECT VERSION()")
            row = await cur.fetchone()
            version = str(row[0]) if row else None
        return {"server_version": version}
    finally:
        conn.close()


async def _probe_postgres(
    endpoint: dict[str, Any], creds: dict[str, str], timeout_sec: int
) -> dict[str, Any]:
    import asyncpg

    try:
        conn = await asyncio.wait_for(
            asyncpg.connect(
                host=endpoint["host"],
                port=int(endpoint.get("port", 5432)),
                user=creds.get("username", ""),
                password=creds.get("password", ""),
                database=endpoint.get("database"),
                timeout=timeout_sec,
            ),
            timeout=timeout_sec + 1,
        )
    except TimeoutError as exc:
        raise ProbeFailure(ErrorCode.DATASOURCE_TIMEOUT, f"连接超时（>{timeout_sec}s）") from exc
    except Exception as exc:
        raise _classify(exc) from exc

    try:
        version = await conn.fetchval("SELECT version()")
        return {"server_version": str(version).split(" ")[0] if version else None}
    finally:
        await conn.close()


async def _probe_minio(endpoint: dict[str, Any], creds: dict[str, str], timeout_sec: int) -> dict[str, Any]:
    """MinIO 用 HTTP 探测而不是 minio SDK。

    理由是超时可控：SDK 的 ``bucket_exists`` 在目标不可达时会用自己的重试策略，
    探测接口不能因为目标库网络抖动就把 API 请求挂到网关超时。
    """
    import httpx

    url = f"{str(endpoint['endpoint']).rstrip('/')}/{endpoint['bucket']}"
    try:
        async with httpx.AsyncClient(timeout=timeout_sec) as client:
            resp = await client.head(url)
    except httpx.TimeoutException as exc:
        raise ProbeFailure(ErrorCode.DATASOURCE_TIMEOUT, f"连接超时（>{timeout_sec}s）") from exc
    except Exception as exc:
        raise ProbeFailure(ErrorCode.DATASOURCE_UNREACHABLE, "对象存储不可达") from exc

    # 403 说明**服务是活的**、只是这个 key 没权限——对「连通性」而言这是成功。
    # 把它判成失败会让用户以为地址填错了，实际是权限配置问题。
    if resp.status_code in (200, 403):
        return {"server_version": resp.headers.get("server", "minio")}
    if resp.status_code == 404:
        raise ProbeFailure(ErrorCode.DATASOURCE_UNREACHABLE, "存储桶不存在")
    raise ProbeFailure(ErrorCode.DATASOURCE_UNREACHABLE, f"对象存储返回 {resp.status_code}")


async def _probe_file(endpoint: dict[str, Any], creds: dict[str, str], timeout_sec: int) -> dict[str, Any]:
    """本地文件（CSV / JSON）：只判存在与可读，不解析内容。

    解析放在元数据探查里做——连通性测试的语义是「这个数据源现在能用吗」，
    对文件而言就是「文件在不在、读不读得到」。
    """
    from pathlib import Path

    path = Path(str(endpoint["path"]))

    def _check() -> None:
        # 文件 IO 是阻塞的：在 async 函数里直接做会卡住事件循环上所有并发请求。
        # 这里挪进同步函数再交给线程池，代价是一次线程切换，换来的是不阻塞别人。
        if not path.exists():
            raise ProbeFailure(ErrorCode.DATASOURCE_UNREACHABLE, "文件不存在")
        if not path.is_file():
            raise ProbeFailure(ErrorCode.DATASOURCE_UNREACHABLE, "路径不是文件")
        try:
            with path.open("rb") as fh:
                fh.read(1)
        except PermissionError as exc:
            raise ProbeFailure(ErrorCode.DATASOURCE_AUTH_FAILED, "文件不可读（权限不足）") from exc

    await asyncio.to_thread(_check)
    return {"server_version": None}


_PROBES: dict[DataSourceType, Any] = {
    DataSourceType.MYSQL: _probe_mysql,
    DataSourceType.DORIS: _probe_mysql,  # Doris FE 走 MySQL 协议
    DataSourceType.POSTGRESQL: _probe_postgres,
    DataSourceType.MINIO: _probe_minio,
    DataSourceType.CSV: _probe_file,
    DataSourceType.JSON: _probe_file,
}


def _classify(exc: Exception) -> ProbeFailure:
    """把驱动异常翻译成字典里的错误码。

    只按**异常类名**分类，不 import 各驱动的异常类型：这样新加驱动时忘了登记
    也不会 import 失败，最坏情况是归到「不可达」——一个仍然正确的降级。

    **例外：我们自己的代码缺陷不归到「不可达」。** ``KeyError`` / ``TypeError``
    / ``AttributeError`` / ``IndexError`` 只可能来自解析驱动返回值的代码，它们
    与「目标库连不上」毫无关系。混为一谈的代价实测过：``_schema_mysql`` 里一处
    大小写键名写错（``row["table_name"]`` vs MySQL 回的大写 ``TABLE_NAME``）被
    归成 ``DATASOURCE_UNREACHABLE``/「连接失败」，排查方向被带去查网络、查凭据、
    查 Vault，而真因在解析代码里。归 ``INTERNAL_ERROR`` 至少让前端显示
    「服务内部错误」，与「数据源不可达」区分开。
    """
    if isinstance(exc, (KeyError, TypeError, AttributeError, IndexError)):
        # 消息只带异常类型，不带内容：异常文本里可能裹着源库的值
        return ProbeFailure(ErrorCode.INTERNAL_ERROR, f"探测内部错误：{type(exc).__name__}")

    name = type(exc).__name__.lower()
    text = str(exc).lower()
    if "timeout" in name or "timed out" in text:
        return ProbeFailure(ErrorCode.DATASOURCE_TIMEOUT, "连接超时")
    if "auth" in name or "password" in text or "access denied" in text:
        return ProbeFailure(ErrorCode.DATASOURCE_AUTH_FAILED, "认证失败（账号或口令错误）")
    if "refused" in text or "unreachable" in text:
        return ProbeFailure(ErrorCode.DATASOURCE_UNREACHABLE, "目标拒绝连接")
    if "unknown host" in text or "name or service not known" in text or "getaddrinfo" in text:
        return ProbeFailure(ErrorCode.DATASOURCE_UNREACHABLE, "主机名无法解析")
    return ProbeFailure(ErrorCode.DATASOURCE_UNREACHABLE, "连接失败")


# ---------------------------------------------------------------- 执行器

# 标识符白名单。asyncpg 的 ``$1`` 只能绑**值**，表名与模式名必须内联，
# 所以这一处拼 SQL 只能靠「只允许字母数字下划线」把住，没有别的办法。
# MySQL 那边同理，用反引号包裹 + 拒绝名字里的反引号。
_SAFE_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


async def _resolve_target(
    args: Any, ctx: ExecutionContext, session: AsyncSession
) -> tuple[DataSourceType, dict[str, Any], dict[str, str]]:
    """按 ``data_source_id`` 取连接信息与凭据。

    **带 tenant + project 过滤**（CLAUDE.md §4.7）。少一个过滤条件，
    跨租户就能靠猜 id 探测别人的库——这是本模块最容易写漏的一行。
    """
    from models.datasource import DataSource

    row = (
        await session.execute(
            select(DataSource).where(
                DataSource.id == args.data_source_id,
                DataSource.tenant_id == ctx.tenant_id,
                DataSource.project_id == ctx.project_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise AppError("数据源不存在", code=ErrorCode.NOT_FOUND, http_status=404)
    if row.status != "active":
        raise AppError("数据源已停用，无法探测", code=ErrorCode.CONFLICT, http_status=409)

    endpoint = dict(row.endpoint or {})
    creds = await _materialize_credentials(row)
    return DataSourceType(row.type), endpoint, creds


async def _materialize_credentials(row: Any) -> dict[str, str]:
    """从 Vault 取凭据 —— **明文只在此处存在**。

    没有 secret_ref 就回空 dict（文件类数据源本就不需要凭据），
    而不是报错——否则 CSV 数据源永远测不了连通性。
    """
    ref = row.secret_ref
    if ref is None:
        return {}
    from clients.vault import VaultClient

    client = VaultClient.from_settings()
    return await client.read_fields(path=ref.path, version=ref.version, key_names=list(ref.key_names or []))


async def connectivity_test_executor(
    args: Any, ctx: ExecutionContext, session: AsyncSession
) -> dict[str, Any]:
    """``datasource.connectivity_test`` 执行器。

    返回体是 ``_safe_detail`` 白名单里的键（见 ``harness.broker``），
    所以这里刻意只回探测结论，不回任何与连接串同形的东西。
    """
    if not isinstance(args, DatasourceConnectivityTestArgs):  # pragma: no cover - 注册表已保证
        raise AppError("执行器收到的入参类型不符", code=ErrorCode.INTERNAL_ERROR)

    data_source_type, endpoint, creds = await _resolve_target(args, ctx, session)
    timeout_sec = args.connect_timeout_sec
    target = mask_target(endpoint)

    probe = _PROBES.get(data_source_type)
    if probe is None:
        # 「仅注册不真连」的类型：给一个明确的结论，而不是假装成功
        return {
            "state": "denied",
            "target": target,
            "latency_ms": 0,
            "message": f"{data_source_type.value} 连接器尚未启用",
        }

    started = time.perf_counter()
    try:
        result = await probe(endpoint, creds, timeout_sec)
    except ProbeFailure as exc:
        latency = int((time.perf_counter() - started) * 1000)
        log.info(
            "datasource_probe_failed",
            data_source_id=args.data_source_id,
            ds_type=data_source_type.value,
            error_code=exc.error_code.value,
            latency_ms=latency,
        )
        return {
            "state": "failed",
            "target": target,
            "latency_ms": latency,
            "error_code": exc.error_code.value,
            "message": exc.message,
        }

    latency = int((time.perf_counter() - started) * 1000)
    return {
        "state": "succeeded",
        "target": target,
        "latency_ms": latency,
        "server_version": result.get("server_version"),
    }


async def metadata_probe_executor(args: Any, ctx: ExecutionContext, session: AsyncSession) -> dict[str, Any]:
    """``metadata.probe`` 执行器：探测 → 脱敏 → 落 ``metadata_profile``。

    **返回值只有扁平摘要**（profile_id / version / tables_count / target），
    因为 ``ToolBroker._safe_detail`` 的白名单刻意只放扁平标量键。
    表结构与样本走领域表，不走工具 detail——后者会进账本与 outbox，
    把一棵可能几百行的 schema 树塞进去会让账本失去可读性，
    也会让「账本里出现过什么」这件事变得无法审计。

    样本的**明文只活在本函数的局部变量里**：``_sample_*`` 取回来，
    ``mask_rows`` 立刻就地替换，之后再没有任何一条路径能拿到原文。
    """
    if not isinstance(args, MetadataProbeArgs):  # pragma: no cover - 注册表已保证
        raise AppError("执行器收到的入参类型不符", code=ErrorCode.INTERNAL_ERROR)

    from engines.masking import (
        SensitiveKind,
        infer_sensitivity,
        mask_rows,
        merge_policy,
    )
    from models.datasource import MetadataProfile
    from models.enums import ProbeStatus, SensitiveLevel

    data_source_type, endpoint, creds = await _resolve_target(args, ctx, session)
    timeout_sec = args.connect_timeout_sec

    profile = (
        await session.execute(
            select(MetadataProfile).where(
                MetadataProfile.id == args.profile_id,
                MetadataProfile.tenant_id == ctx.tenant_id,
                MetadataProfile.data_source_id == args.data_source_id,
            )
        )
    ).scalar_one_or_none()
    if profile is None:
        raise AppError("元数据档案不存在", code=ErrorCode.NOT_FOUND, http_status=404)

    # 盐按 (数据源, 版本) 固定：同一个 profile 内同值同显（样本看起来才像真的），
    # 不同 profile 之间不可比（否则两次探查能靠比对推断出某个值有没有变）
    salt = f"{ctx.tenant_id}:{args.data_source_id}:{profile.version}"
    policy = merge_policy(dict(args.masking_policy or {}))

    try:
        tables, raw_samples = await _collect(data_source_type, endpoint, creds, args, timeout_sec)
    except Exception as exc:
        # 探测期的**任何**异常都在这里收敛。只捕 ProbeFailure 是不够的：
        # 连接成功后的游标报错（权限不足读 information_schema、表在查询中途被 drop）
        # 会以驱动原生异常逃逸，一路穿过 broker 进到 Celery，
        # 而档案行永远停在 probing——前端轮询一辈子也等不到终态
        failure = exc if isinstance(exc, ProbeFailure) else _classify(exc)
        if failure.error_code is ErrorCode.INTERNAL_ERROR:
            # 归一化的文案会把真因抹平（栈、异常类型全丢），而这里恰恰是代码缺陷：
            # 不留栈的话，下一个人只能对着「连接失败」去查网络与凭据。
            # 只堆栈、不带异常文本：文本里可能裹着源库的值。
            log.exception(
                "metadata_probe_internal_error",
                data_source_id=args.data_source_id,
                error_type=type(exc).__name__,
            )
        profile.status = ProbeStatus.FAILED
        profile.error_message = failure.message[:512]
        profile.masking_policy = policy
        profile.sampling = {"scope": args.scope, "rows": args.sample_rows, "tables": list(args.tables)}
        await session.commit()
        log.info(
            "metadata_probe_failed",
            data_source_id=args.data_source_id,
            profile_id=profile.id,
            error_code=failure.error_code.value,
        )
        return {
            "profile_id": profile.id,
            "version": profile.version,
            "tables_count": 0,
            "target": mask_target(endpoint),
            "error_code": failure.error_code.value,
            "message": failure.message,
        }

    keep_public_plain = bool(policy.get("keep_public_plain", True))

    # 判定做一次、复用到所有行：逐行判会出现「第一行脱了、第二行没脱」
    for table in tables:
        for column in table["columns"]:
            kind, level = infer_sensitivity(str(column["name"]), comment=column.get("comment"))
            column["sensitive_level"] = level.value
            # ``masked`` 必须**如实**回答「这列的样本值脱了吗」，判定条件要与
            # ``mask_value`` 里那条出口完全一致：只有 public（且策略保留明文）才不脱。
            # 写成「列名命中了敏感词才算 masked」会给出一个与事实相反的标记——
            # 前端据此显示「未脱敏」，而库里存的其实是摘要。
            column["masked"] = level is not SensitiveLevel.PUBLIC or not keep_public_plain
            column["_kind"] = kind.value

    masked_samples: dict[str, Any] = {}
    for table_name, rows in raw_samples.items():
        columns = {
            str(c["name"]): (SensitiveKind(c["_kind"]), SensitiveLevel(c["sensitive_level"]))
            for c in _columns_of(tables, table_name)
        }
        masked_samples[table_name] = mask_rows(
            rows, columns=columns, salt=salt, keep_public_plain=keep_public_plain
        )
        # 脱敏完成后立刻丢掉明文引用，别让它在这个作用域里多活几行。
        # 这里 clear 的是本函数持有的那个 list；执行器的调用栈一返回，
        # 原始结果集就没有任何引用者了
        rows.clear()

    profile.status = ProbeStatus.READY
    # 敏感级别只是**列级元数据**（不含任何值），可以进快照
    profile.schema_snapshot = {"tables": [_narrow_table(t) for t in tables]}
    profile.masked_samples = masked_samples
    profile.masking_policy = policy
    profile.sampling = {
        "scope": args.scope,
        "rows": args.sample_rows,
        "tables": list(args.tables),
        "sampled_tables": sorted(masked_samples),
    }
    profile.tables_count = len(tables)
    profile.sha256 = _snapshot_digest(profile.schema_snapshot)
    profile.error_message = None
    await session.commit()

    log.info(
        "metadata_probed",
        data_source_id=args.data_source_id,
        profile_id=profile.id,
        tables_count=profile.tables_count,
        sampled=len(masked_samples),
    )
    return {
        "profile_id": profile.id,
        "version": profile.version,
        "tables_count": profile.tables_count,
        "target": mask_target(endpoint),
    }


async def _collect(
    data_source_type: DataSourceType,
    endpoint: dict[str, Any],
    creds: dict[str, str],
    args: MetadataProbeArgs,
    timeout_sec: int,
) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    """取结构 + 抽样。返回的样本**是明文**，只允许 mask_rows 消费。"""
    if data_source_type in (DataSourceType.MYSQL, DataSourceType.DORIS):
        tables = await _schema_mysql(endpoint, creds, timeout_sec, args.tables)
        return tables, await _sample_mysql(endpoint, creds, timeout_sec, _sample_targets(args, tables))
    if data_source_type is DataSourceType.POSTGRESQL:
        tables = await _schema_postgres(endpoint, creds, timeout_sec, args.tables)
        return tables, await _sample_postgres(
            endpoint, creds, timeout_sec, _sample_targets(args, tables), args.sample_rows
        )
    # 文件类与「仅注册不真连」的类型：M1 不产样本，但**仍然算探查成功**。
    # 报失败会让人以为数据源坏了，而这里真实的语义是「这类源还没有采样器」
    return [], {}


def _columns_of(tables: list[dict[str, Any]], table_name: str) -> list[dict[str, Any]]:
    """按表名取列。表不存在时返回空列表而不是抛 StopIteration——

    ``_sample_mysql`` 里 ```` 包裹失败会 continue 掉，理论上样本与结构同源；
    但真要出现不一致，脱敏应当降级为「整行都按 masked 处理」，而不是让探查崩掉。
    """
    for table in tables:
        if table["name"] == table_name:
            return list(table["columns"])
    return []


def _sample_targets(args: MetadataProbeArgs, tables: list[dict[str, Any]]) -> list[str]:
    """哪些表要抽样本。``schema_only`` 一张都不抽。"""
    if args.scope == "schema_only" or args.sample_rows <= 0:
        return []
    names = [str(t["name"]) for t in tables]
    if args.scope == "selected_tables":
        return [n for n in names if n in set(args.tables)]
    # full_tables：只取前 20 张。全库抽样在宽表场景下会让一次探查读掉几百万行，
    # 而「有哪些列、长什么样」这个目的不需要那么多行
    return names[:20]


def _narrow_table(table: dict[str, Any]) -> dict[str, Any]:
    """把表结构压成 JSON 存的形状。注释截断，避免把一整段业务说明抄进快照。"""
    from engines.masking import narrow

    return {
        "schema": table.get("schema"),
        "name": table["name"],
        "comment": narrow(table.get("comment")),
        "row_count_approx": table.get("row_count_approx"),
        "columns": [
            {
                "name": c["name"],
                "data_type": c["data_type"],
                "nullable": bool(c.get("nullable", True)),
                "is_primary_key": bool(c.get("is_primary_key", False)),
                "comment": narrow(c.get("comment")),
                "sensitive_level": c["sensitive_level"],
                "masked": bool(c["masked"]),
            }
            for c in table["columns"]
        ],
    }


def _snapshot_digest(snapshot: dict[str, Any]) -> str:
    """快照指纹。PipelineVersion 生成时绑定它，所以必须是**稳定**的。

    ``sort_keys`` + 固定分隔符：字典序不稳的话，同一个库两次探查会得到不同的
    sha256，于是「元数据没变」这件事永远无法证明，指纹也就失去了意义。
    """
    canonical = json.dumps(snapshot, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


async def _schema_mysql(
    endpoint: dict[str, Any], creds: dict[str, str], timeout_sec: int, only: tuple[str, ...]
) -> list[dict[str, Any]]:
    import aiomysql

    try:
        conn = await aiomysql.connect(
            host=endpoint["host"],
            port=int(endpoint.get("port", 3306)),
            user=creds.get("username", ""),
            password=creds.get("password", ""),
            db=endpoint.get("database"),
            connect_timeout=timeout_sec,
        )
    except Exception as exc:
        raise _classify(exc) from exc

    database = endpoint.get("database")
    try:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                "SELECT table_name, table_comment, table_rows FROM information_schema.tables "
                "WHERE table_schema = %s AND table_type = 'BASE TABLE' ORDER BY table_name",
                (database,),
            )
            table_rows = await cur.fetchall()
            await cur.execute(
                "SELECT table_name, column_name, column_type, is_nullable, column_key, column_comment "
                "FROM information_schema.columns WHERE table_schema = %s "
                "ORDER BY table_name, ordinal_position",
                (database,),
            )
            column_rows = await cur.fetchall()
    finally:
        conn.close()

    by_table: dict[str, list[dict[str, Any]]] = {}
    for row in column_rows:
        # 必须走 _pick：DictCursor 的行是普通 dict，键名大小写**敏感**，
        # 而 MySQL 8 回的是大写 TABLE_NAME。写成 `row["table_name"] or row["TABLE_NAME"]`
        # 时前者直接抛 KeyError，`or` 的兜底根本没机会执行——
        # 真机上表现为「探查莫名失败」，排查成本极高（见 _classify 里那段注释）。
        by_table.setdefault(str(_pick(row, "table_name")), []).append(row)

    tables: list[dict[str, Any]] = []
    for row in table_rows:
        name = str(_pick(row, "table_name"))
        if only and name not in set(only):
            continue
        tables.append(
            {
                "schema": str(database),
                "name": name,
                "comment": _pick(row, "table_comment"),
                "row_count_approx": int(_pick(row, "table_rows") or 0),
                "columns": [
                    {
                        "name": str(_pick(c, "column_name")),
                        "data_type": str(_pick(c, "column_type")),
                        "nullable": str(_pick(c, "is_nullable")).upper() == "YES",
                        "is_primary_key": str(_pick(c, "column_key")).upper() == "PRI",
                        "comment": _pick(c, "column_comment"),
                    }
                    for c in by_table.get(name, [])
                ],
            }
        )
    return tables


async def _schema_postgres(
    endpoint: dict[str, Any], creds: dict[str, str], timeout_sec: int, only: tuple[str, ...]
) -> list[dict[str, Any]]:
    import asyncpg

    schema = endpoint.get("schema") or "public"
    try:
        conn = await asyncpg.connect(
            host=endpoint["host"],
            port=int(endpoint.get("port", 5432)),
            user=creds.get("username", ""),
            password=creds.get("password", ""),
            database=endpoint.get("database"),
            timeout=timeout_sec,
        )
    except Exception as exc:
        raise _classify(exc) from exc

    try:
        tables = await conn.fetch(
            "SELECT c.relname AS table_name, obj_description(c.oid) AS table_comment, "
            "  c.reltuples::bigint AS row_count "
            "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = $1 AND c.relkind = 'r' ORDER BY c.relname",
            schema,
        )
        columns = await conn.fetch(
            "SELECT table_name, column_name, data_type, is_nullable, column_default, "
            "  col_description((table_schema||'.'||table_name)::regclass::oid, ordinal_position) AS comment, "
            "  EXISTS (SELECT 1 FROM information_schema.table_constraints tc "
            "          JOIN information_schema.key_column_usage kcu "
            "            ON kcu.constraint_name = tc.constraint_name "
            "          WHERE tc.constraint_type='PRIMARY KEY' AND tc.table_name = c.table_name "
            "            AND kcu.column_name = c.column_name AND tc.table_schema = c.table_schema) AS is_pk "
            "FROM information_schema.columns c WHERE table_schema = $1 ORDER BY table_name, ordinal_position",
            schema,
        )
    finally:
        await conn.close()

    by_table: dict[str, list[Any]] = {}
    for row in columns:
        by_table.setdefault(str(row["table_name"]), []).append(row)

    out: list[dict[str, Any]] = []
    for row in tables:
        name = str(row["table_name"])
        if only and name not in set(only):
            continue
        out.append(
            {
                "schema": schema,
                "name": name,
                "comment": row["table_comment"],
                "row_count_approx": int(row["row_count"] or 0),
                "columns": [
                    {
                        "name": str(c["column_name"]),
                        "data_type": str(c["data_type"]),
                        "nullable": str(c["is_nullable"]).upper() == "YES",
                        "is_primary_key": bool(c["is_pk"]),
                        "comment": c["comment"],
                    }
                    for c in by_table.get(name, [])
                ],
            }
        )
    return out


def _pick(row: Any, key: str) -> Any:
    """aiomysql 的 DictCursor 在不同版本里大小写回显不一致，两种都试。

    这不是防御性编程：MySQL 8 在部分配置（``lower_case_table_names``）下
    返回的列名是大写的，而演示源库正是 MySQL 8。

    两种都取不到时**抛错**而不是返回 None：返回 None 会被 ``str()`` 变成表名
    ``"None"``、列名 ``"None"``，档案看起来是「成功」的，只是内容悄悄错了。
    抛错则会被 ``_classify`` 归到 ``INTERNAL_ERROR`` 并留下栈——错得响亮好过错得安静。
    """
    try:
        return row[key]
    except (KeyError, TypeError):
        pass
    upper = key.upper()
    try:
        return row[upper]
    except (KeyError, TypeError):
        raise KeyError(f"结果集里既没有 {key!r} 也没有 {upper!r}") from None


# 抽样里最多读多少行。args.sample_rows 更大时按这个封顶——
# 样本进的是 JSONB 列，一份 1000 行 × 30 列的样本是十几 MB，
# 而它的用途只是「让人看看这列长什么样」
_MAX_SAMPLE_ROWS = 200


async def _sample_mysql(
    endpoint: dict[str, Any], creds: dict[str, str], timeout_sec: int, targets: list[str]
) -> dict[str, list[dict[str, Any]]]:
    """抽样。**返回的原始值只应被 mask_rows 消费**。"""
    if not targets:
        return {}
    import aiomysql

    try:
        conn = await aiomysql.connect(
            host=endpoint["host"],
            port=int(endpoint.get("port", 3306)),
            user=creds.get("username", ""),
            password=creds.get("password", ""),
            db=endpoint.get("database"),
            connect_timeout=timeout_sec,
        )
    except Exception as exc:
        raise _classify(exc) from exc
    try:
        out: dict[str, list[dict[str, Any]]] = {}
        async with conn.cursor(aiomysql.DictCursor) as cur:
            for name in targets:
                # 表名不能参数化（它是标识符不是值），所以用反引号包裹并拒掉反引号本身：
                # 这是唯一一处拼 SQL 的地方，必须在这里堵死注入
                # 表名是标识符不能参数化，只能包裹 + 拒绝包裹符本身。
                # 这不是「忘了用参数」，是无参数可用（S608 在此是误报）
                if "`" in name:
                    continue
                await cur.execute(f"SELECT * FROM `{name}` LIMIT {_MAX_SAMPLE_ROWS}")  # noqa: S608
                out[name] = [dict(r) for r in await cur.fetchall()]
        return out
    finally:
        conn.close()


async def _sample_postgres(
    endpoint: dict[str, Any],
    creds: dict[str, str],
    timeout_sec: int,
    targets: list[str],
    sample_rows: int,
) -> dict[str, list[dict[str, Any]]]:
    if not targets:
        return {}
    import asyncpg

    schema = endpoint.get("schema") or "public"
    # schema 也是标识符、也来自用户填的连接配置，同样必须过白名单，
    # 否则「内联表名但内联模式名」等于只堵了一半
    if not _SAFE_IDENT.fullmatch(schema):
        schema = "public"
    try:
        conn = await asyncpg.connect(
            host=endpoint["host"],
            port=int(endpoint.get("port", 5432)),
            user=creds.get("username", ""),
            password=creds.get("password", ""),
            database=endpoint.get("database"),
            timeout=timeout_sec,
        )
    except Exception as exc:
        raise _classify(exc) from exc
    try:
        limit = min(sample_rows or _MAX_SAMPLE_ROWS, _MAX_SAMPLE_ROWS)
        out: dict[str, list[dict[str, Any]]] = {}
        for name in targets:
            if not _SAFE_IDENT.fullmatch(name):
                continue
            # 同上：标识符内联，值全部走参数
            rows = await conn.fetch(
                f'SELECT * FROM "{schema}"."{name}" LIMIT {limit}'  # noqa: S608
            )
            out[name] = [dict(r) for r in rows]
        return out
    finally:
        await conn.close()


# ---------------------------------------------------------------- 注册

_EXECUTORS: tuple[tuple[ToolName, ToolExecutor], ...] = (
    (ToolName.DATASOURCE_CONNECTIVITY_TEST, connectivity_test_executor),
    (ToolName.METADATA_PROBE, metadata_probe_executor),
)


def register() -> None:
    """注入执行器。由 ``main`` / ``celery`` 的启动钩子调用。

    **不在 import 期自动注册**：``harness.tools`` 的注册表禁止覆盖，
    而测试要注入假执行器。让注册变成一次显式调用，测试就能先注册假的、
    应用再注册真的——顺序由调用方决定，而不是由 import 顺序这种隐式事实决定。
    """
    from harness.tools import registered_executors

    done = registered_executors()
    for tool, fn in _EXECUTORS:
        if tool not in done:
            register_executor(tool, fn)


__all__ = ["ProbeFailure", "connectivity_test_executor", "mask_target", "metadata_probe_executor", "register"]
