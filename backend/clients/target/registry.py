"""目标端解析与构造：``run → 目标数据源 → publisher``（be-18 §3.2 的 ``build_publisher``）。

解析链是**唯一**一条把「运行」与「要动的库」连起来的路：

```
execution_run.pipeline_version_id → pipeline_version.pipeline_id
   → pipeline（tenant/project 过滤）→ target_data_source_id
      → data_source（tenant/project 过滤 + status='active'）→ endpoint + Vault 凭据
```

三处刻意收紧：

1. **没有一步可以省掉租户过滤。** ``execution_run`` 与 ``data_source`` 都带
   ``tenant_id``，``pipeline`` 带 ``tenant_id + project_id``；漏掉任何一处，
   猜一个 id 就能去动别的租户的库（CLAUDE.md §4.7）。
2. **凭据只在这里物化，且立刻收进 ``SecretStr``。** 明文口令不进日志、不进异常、
   不进 ``repr``；它唯一的消费点是 ``wire.connect`` 里那一次取值。
3. **目标端类型不在实现范围内就直接拒。** ``data_source.type`` 是 oracle/clickhouse
   这类「仅注册不真连」的类型时，宁可回一个重要错误，也不去试一条没人验证过的路径。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from clients.target.base import (
    TargetBinding,
    TargetEndpointRef,
    TargetError,
    TargetPublisher,
    UnsupportedTargetError,
)
from clients.target.doris import DorisTargetPublisher
from clients.target.mysql import MySQLTargetPublisher
from core.errors import ErrorCode, NotFoundError
from models.datasource import DataSource
from models.enums import DataSourceType, RunStatus
from models.execution import ExecutionRun
from models.pipeline import Pipeline, PipelineVersion

#: 实现了原子发布的类型 → publisher 类。**只登记真实现过的**：
#: 让 oracle 落到一个「能构造但没人验证过」的对象上，比直接拒绝危险得多。
_PUBLISHERS: dict[str, type[DorisTargetPublisher] | type[MySQLTargetPublisher]] = {
    DataSourceType.DORIS.value: DorisTargetPublisher,
    DataSourceType.MYSQL.value: MySQLTargetPublisher,
}


@dataclass(frozen=True, slots=True)
class RunTargetFacts:
    """一次发布/回滚要用到的**全部外部事实**，一次取齐（照 ``clients/seatunnel`` 的 ``_Facts``）。

    集中在一次调用里是为了让「少查了一样」一眼可见：这些字段横跨
    run / pipeline_version / pipeline / data_source 四张表，
    分散在调用方逐个 ``session.get`` 会让人以为它们出自同一处。
    """

    run_id: int
    tenant_id: int
    project_id: int
    status: RunStatus
    shadow_table: str
    formal_table: str
    error_table: str
    read_rows: int
    triggered_by: int | None
    preparation_id: int
    pipeline_version_id: int
    pipeline_id: int
    version_no: int
    #: 冻结版本的制品指纹。Capability 与 §6.1 条件 4 都以它为锚点。
    artifact_sha256: str
    target_data_source_id: int
    binding: TargetBinding


def build_publisher(endpoint: TargetEndpointRef) -> TargetPublisher:
    """按端点类型构造 publisher。**这是本包唯一的构造入口**（含测试替换点）。"""
    factory = _PUBLISHERS.get(endpoint.kind)
    if factory is None:  # pragma: no cover - 类型是 Literal，正常构造不出别的值
        raise UnsupportedTargetError(f"未实现原子发布的目标端类型：{endpoint.kind}")
    return factory(endpoint)


async def load_run_target(session: AsyncSession, *, tenant_id: int, run_id: int) -> RunTargetFacts:
    """解析一次运行的目标端（含凭据物化）。**表名全部来自 ``execution_run``**。"""
    run = (
        await session.execute(
            select(ExecutionRun).where(ExecutionRun.id == run_id, ExecutionRun.tenant_id == tenant_id)
        )
    ).scalar_one_or_none()
    if run is None:
        # 跨租户访问与「不存在」在这里是**同一个**答案：能区分它们，
        # 就等于给了「这个 id 在别的租户里存在」这个信息。
        raise NotFoundError("运行不存在")

    version = await session.get(PipelineVersion, run.pipeline_version_id)
    if version is None:
        raise TargetError(
            "运行引用的版本不存在（数据不一致）", code=ErrorCode.INTERNAL_ERROR, http_status=500
        )
    pipeline = (
        await session.execute(
            select(Pipeline).where(
                Pipeline.id == version.pipeline_id,
                Pipeline.tenant_id == tenant_id,
                Pipeline.project_id == run.project_id,
            )
        )
    ).scalar_one_or_none()
    if pipeline is None:
        raise NotFoundError("运行所属的 pipeline 不存在")

    if not run.shadow_table or not run.formal_table:
        # 表名在 Commit 时就该落库（be-14）。这里为空说明运行**没走完提交**，
        # 而「没有影子表名」的发布请求无法被安全地执行——没有可动的对象，也没有边界可审
        raise TargetError(
            "运行缺少影子表/正式表信息，无法发布",
            code=ErrorCode.INTERNAL_ERROR,
            http_status=500,
            details={"reason": "run_tables_missing"},
        )
    if pipeline.target_data_source_id is None:
        raise TargetError(
            "该 pipeline 没有配置目标数据源",
            code=ErrorCode.INTERNAL_ERROR,
            http_status=500,
            details={"reason": "target_data_source_missing"},
        )

    data_source = (
        await session.execute(
            select(DataSource).where(
                DataSource.id == pipeline.target_data_source_id,
                DataSource.tenant_id == tenant_id,
                DataSource.project_id == run.project_id,
            )
        )
    ).scalar_one_or_none()
    if data_source is None:
        raise NotFoundError("目标数据源不存在")
    if data_source.status != "active":
        raise TargetError("目标数据源已停用，无法发布", code=ErrorCode.CONFLICT, http_status=409)

    binding = await _binding_of(data_source)
    return RunTargetFacts(
        run_id=int(run.id),
        tenant_id=int(run.tenant_id),
        project_id=int(run.project_id),
        status=run.status,
        shadow_table=run.shadow_table,
        formal_table=run.formal_table,
        error_table=run.error_table or "",
        read_rows=int(run.read_rows or 0),
        triggered_by=run.triggered_by,
        preparation_id=int(run.preparation_id),
        pipeline_version_id=int(run.pipeline_version_id),
        pipeline_id=int(pipeline.id),
        version_no=int(version.version_no),
        artifact_sha256=str(version.sha256),
        target_data_source_id=int(data_source.id),
        binding=binding,
    )


async def _binding_of(data_source: DataSource) -> TargetBinding:
    """数据源行 → 目标端点（含凭据物化）。"""
    raw_kind = str(data_source.type)
    if raw_kind == DataSourceType.DORIS.value:
        kind: Literal["doris", "mysql"] = "doris"
    elif raw_kind == DataSourceType.MYSQL.value:
        kind = "mysql"
    else:
        raise UnsupportedTargetError(
            f"目标数据源类型 {raw_kind} 不支持原子发布（见 be-18 §8 的最小权限说明）",
            details={"data_source_id": int(data_source.id), "type": raw_kind},
        )
    endpoint = dict(data_source.endpoint or {})
    creds = await materialize_credentials(data_source)
    username = str(creds.get("username") or "")
    password = str(creds.get("password") or "")
    if not username or not password:
        # 少凭据时必须**在这里**失败：往下走会得到一个「认证失败」，
        # 而那个错误看起来像口令写错了，实际是引用里就没登记这两个键
        raise TargetError(
            f"目标数据源 {data_source.name} 的凭据缺少 username/password",
            code=ErrorCode.INTERNAL_ERROR,
            http_status=500,
            details={"reason": "credential_incomplete", "data_source_id": int(data_source.id)},
        )
    ref = TargetEndpointRef(
        kind=kind,
        data_source_id=int(data_source.id),
        host=str(endpoint.get("host") or ""),
        port=int(endpoint.get("port") or (9030 if kind == "doris" else 3306)),
        database=str(endpoint.get("database") or ""),
        username=username,
        password=SecretStr(password),
        charset=str(endpoint.get("charset") or "utf8mb4"),
    )
    return TargetBinding(
        data_source_id=ref.data_source_id,
        kind=ref.kind,
        database=ref.database,
        endpoint=ref,
    )


async def materialize_credentials(row: Any) -> dict[str, str]:
    """从 Vault 取凭据 —— **明文只在此处存在**（与 ``clients/datasource_probe`` 同一做法）。

    没有 ``secret_ref`` 就回空 dict：目标端是文件类数据源时本就无需凭据，
    报错会把「不适用」变成「故障」。
    """
    ref = row.secret_ref
    if ref is None:
        return {}
    from clients.vault import VaultClient

    client = VaultClient.from_settings()
    return await client.read_fields(path=ref.path, version=ref.version, key_names=list(ref.key_names or []))


__all__ = ["RunTargetFacts", "build_publisher", "load_run_target", "materialize_credentials"]
