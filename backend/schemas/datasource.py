"""数据源 DTO 与连接器注册表（be-03）。

**契约以 ``frontend/src/api/datasource.ts`` 与 ``types/datasource.ts`` 为准**——
前端已按 fe-03 建成，后端改名会让界面静默读不到值。与 be-03 §3.1 原设计的差异：

| 原设计 | 实际（前端权威） |
|---|---|
| ``PATCH /{id}`` | ``PUT /{id}`` |
| ``DELETE`` = 软删 | ``DELETE`` = 真删（被引用则 409）；软删是 ``POST /{id}/disable`` |
| ``secret`` 字段 | ``credentials`` 字段 |
| ``POST /{id}/connectivity-test`` | ``POST /{id}/connectivity-tests``（202 + 轮询） |
| ``GET /{id}/secret-reference`` | 无此接口，``secret_ref`` 内嵌在 DataSource DTO |

**本文件里没有任何可以保存凭据值的字段**——这是结构性保证，不是纪律要求。
``endpoint`` 的每个类型模型都是 ``extra="forbid"``，多传一个 ``password`` 键
会在 Pydantic 层直接报错。
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from engines.security import assert_no_credential_keys
from models.enums import DataSourceType, Env, ProbeStatus, SensitiveLevel

# 允许写入 Vault 的键名。白名单而非黑名单：Vault 里的键名是运维排查时的路标，
# 允许自由命名会很快出现 "passwd"/"pwd"/"pass" 三种拼写指同一个东西。
ALLOWED_SECRET_KEYS: frozenset[str] = frozenset(
    {
        "username",
        "password",
        "token",
        "api_key",
        "access_key",
        "secret_key",
        "private_key",
        "passphrase",
        "cert",
        "client_id",
        "client_secret",
    }
)


class _Endpoint(BaseModel):
    """endpoint 基类：**禁止额外字段**。

    这条 ``extra="forbid"`` 是「无明文 Secret」红线的第一道闸：
    没有它，调用方可以往 endpoint 里塞 ``password`` —— 它会被原样存进 jsonb 列。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)


class _JdbcEndpoint(_Endpoint):
    """JDBC 类数据源的地址基类：**控制面地址之外，允许再给一套数据面地址**。

    **为什么必须有这两列**：``host``/``port`` 是**控制面**（探测、指纹、对账）连的地址，
    而真正读写的 SeaTunnel 容器在**另一张网**里。本地开发就是活例子：控制面在本机、
    经 SSH 隧道连 ECS，于是 MySQL 是 ``127.0.0.1:3307``；而 SeaTunnel 容器在 compose
    网络里，同一台库它看到的是 ``mysql:3306``。一个字段装不下两个视角——
    装不下就会有人去改代码里某个常量，而症状是「探测连通、作业连不上」，
    排查方向被带到 DNS 与安全组上。

    两列都可缺省，缺省即「两边用同一套地址」（容器编排里 worker 与 SeaTunnel
    同网时就是这种情形，也是线上最常见的情形）。

    它们不是 Secret：地址是定位信息，凭据仍走 ``secret_ref_id`` → Vault。
    """

    exec_host: str | None = Field(default=None, max_length=255, description="数据面视角的主机名")
    exec_port: int | None = Field(default=None, ge=1, le=65535, description="数据面视角的端口")


class MysqlEndpoint(_JdbcEndpoint):
    host: str = Field(min_length=1, max_length=255)
    port: int = Field(default=3306, ge=1, le=65535)
    database: str = Field(min_length=1, max_length=128)
    charset: str = Field(default="utf8mb4", max_length=32)


class PostgresEndpoint(_JdbcEndpoint):
    host: str = Field(min_length=1, max_length=255)
    port: int = Field(default=5432, ge=1, le=65535)
    database: str = Field(min_length=1, max_length=128)
    schema_: str | None = Field(default=None, alias="schema", max_length=128)

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class DorisEndpoint(_JdbcEndpoint):
    host: str = Field(min_length=1, max_length=255)
    # Doris 走 FE 的 MySQL 协议查询端口（默认 9030），不是 BE 的 8040
    port: int = Field(default=9030, ge=1, le=65535)
    database: str = Field(min_length=1, max_length=128)
    warehouse: str | None = Field(default=None, max_length=128)


class MinioEndpoint(_Endpoint):
    endpoint: str = Field(min_length=1, max_length=255, description="含 scheme，如 http://minio:9000")
    bucket: str = Field(min_length=1, max_length=128)
    path: str = Field(default="", max_length=512)


class CsvEndpoint(_Endpoint):
    path: str = Field(min_length=1, max_length=1024)
    encoding: str = Field(default="utf-8", max_length=32)
    delimiter: str = Field(default=",", min_length=1, max_length=4)
    # 前端表单给的是字符串 "true"/"false"（el-select 的取值），两种都收
    has_header: bool = True

    @field_validator("has_header", mode="before")
    @classmethod
    def _coerce_bool(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "y"}
        return value


class JsonEndpoint(_Endpoint):
    path: str = Field(min_length=1, max_length=1024)
    encoding: str = Field(default="utf-8", max_length=32)
    json_path: str | None = Field(default=None, max_length=255)


class PathEndpoint(_Endpoint):
    """parquet / excel：只登记不真连（连接器未启用）。"""

    path: str = Field(min_length=1, max_length=1024)


class OracleEndpoint(_JdbcEndpoint):
    host: str = Field(min_length=1, max_length=255)
    port: int = Field(default=1521, ge=1, le=65535)
    service_name: str = Field(min_length=1, max_length=128)


class ClickhouseEndpoint(_JdbcEndpoint):
    host: str = Field(min_length=1, max_length=255)
    port: int = Field(default=8123, ge=1, le=65535)
    database: str = Field(min_length=1, max_length=128)


class RestEndpoint(_Endpoint):
    base_url: str = Field(min_length=1, max_length=512)
    auth_type: Literal["none", "basic", "bearer", "api_key"] = "none"


class ConnectorSpec(BaseModel):
    """连接器注册表的一行。"""

    model_config = ConfigDict(frozen=True)

    type: DataSourceType
    label: str
    # False = 「仅注册不真连」：禁测试与探查，避免出现「能建不能连」的僵尸数据源
    connectable: bool
    endpoint_model: type[_Endpoint]
    credential_keys: tuple[str, ...]


CONNECTOR_REGISTRY: dict[DataSourceType, ConnectorSpec] = {
    DataSourceType.MYSQL: ConnectorSpec(
        type=DataSourceType.MYSQL,
        label="MySQL",
        connectable=True,
        endpoint_model=MysqlEndpoint,
        credential_keys=("username", "password"),
    ),
    DataSourceType.POSTGRESQL: ConnectorSpec(
        type=DataSourceType.POSTGRESQL,
        label="PostgreSQL",
        connectable=True,
        endpoint_model=PostgresEndpoint,
        credential_keys=("username", "password"),
    ),
    DataSourceType.DORIS: ConnectorSpec(
        type=DataSourceType.DORIS,
        label="Doris",
        connectable=True,
        endpoint_model=DorisEndpoint,
        credential_keys=("username", "password"),
    ),
    DataSourceType.MINIO: ConnectorSpec(
        type=DataSourceType.MINIO,
        label="MinIO / 对象存储",
        connectable=True,
        endpoint_model=MinioEndpoint,
        credential_keys=("access_key", "secret_key"),
    ),
    DataSourceType.CSV: ConnectorSpec(
        type=DataSourceType.CSV,
        label="CSV 文件",
        connectable=True,
        endpoint_model=CsvEndpoint,
        credential_keys=(),
    ),
    DataSourceType.JSON: ConnectorSpec(
        type=DataSourceType.JSON,
        label="JSON 文件",
        connectable=True,
        endpoint_model=JsonEndpoint,
        credential_keys=(),
    ),
    DataSourceType.PARQUET: ConnectorSpec(
        type=DataSourceType.PARQUET,
        label="Parquet 文件",
        connectable=False,
        endpoint_model=PathEndpoint,
        credential_keys=(),
    ),
    DataSourceType.ORACLE: ConnectorSpec(
        type=DataSourceType.ORACLE,
        label="Oracle",
        connectable=False,
        endpoint_model=OracleEndpoint,
        credential_keys=("username", "password"),
    ),
    DataSourceType.CLICKHOUSE: ConnectorSpec(
        type=DataSourceType.CLICKHOUSE,
        label="ClickHouse",
        connectable=False,
        endpoint_model=ClickhouseEndpoint,
        credential_keys=("username", "password"),
    ),
    DataSourceType.EXCEL: ConnectorSpec(
        type=DataSourceType.EXCEL,
        label="Excel 文件",
        connectable=False,
        endpoint_model=PathEndpoint,
        credential_keys=(),
    ),
    DataSourceType.REST: ConnectorSpec(
        type=DataSourceType.REST,
        label="REST 接口",
        connectable=False,
        endpoint_model=RestEndpoint,
        credential_keys=("username", "password", "token", "api_key"),
    ),
}


def endpoint_model_for(data_source_type: DataSourceType) -> type[_Endpoint]:
    spec = CONNECTOR_REGISTRY.get(data_source_type)
    if spec is None:
        raise KeyError(f"未知数据源类型：{data_source_type}")
    return spec.endpoint_model


def validate_endpoint(data_source_type: DataSourceType, endpoint: Any) -> dict[str, Any]:
    """按类型强校验并**归一化** endpoint，顺带跑凭据禁键校验。

    顺序刻意是「先禁键、后类型校验」：若反过来，一个含 ``password`` 的 payload
    会先因为「缺 host」报错，用户改完 host 才被告知不许放密码——
    提示信息和真正的问题错开一轮，排查体验很差。
    """
    assert_no_credential_keys(endpoint, where="endpoint")
    model = endpoint_model_for(data_source_type)
    parsed = model.model_validate(endpoint)
    # by_alias 让 PostgresEndpoint 的 schema 键落回它本来的名字（不是 schema_）
    return parsed.model_dump(mode="json", by_alias=True)


# ---------------------------------------------------------------- DTO


class SecretRefView(BaseModel):
    """服务端只回引用与键名，**永不回值**。"""

    provider: Literal["vault"] = "vault"
    path: str
    version: int
    key_names: list[str] = Field(default_factory=list)
    last_rotated_at: datetime | None = None


class MetadataProfileBrief(BaseModel):
    version: int
    status: ProbeStatus
    tables_count: int
    created_at: datetime
    expires_at: datetime | None = None


class DataSourceRead(BaseModel):
    id: int
    project_id: int
    name: str
    type: DataSourceType
    env: Env
    sensitive_level: SensitiveLevel
    endpoint: dict[str, Any]
    secret_ref: SecretRefView | None = None
    tags: list[str] = Field(default_factory=list)
    owner_id: int | None = None
    owner_display_name: str | None = None
    status: str
    last_tested_at: datetime | None = None
    last_test_ok: bool | None = None
    latest_profile: MetadataProfileBrief | None = None
    created_at: datetime
    updated_at: datetime


Credentials = Annotated[dict[str, str], Field(default_factory=dict)]


def _validate_credentials(value: dict[str, str] | None) -> dict[str, str] | None:
    """凭据白名单校验。create / update 共用——两处各写一份迟早会分叉。"""
    if value is None:
        return None
    if not value:
        raise ValueError("credentials 不能为空对象；不需要凭据就不要提交该字段")
    unknown = sorted(set(value) - ALLOWED_SECRET_KEYS)
    if unknown:
        # 只报键名，绝不回显值
        raise ValueError(f"不支持的凭据字段：{', '.join(unknown)}")
    for key, raw in value.items():
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError(f"凭据 {key} 必须是非空字符串")
    return value


class DataSourceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: int = Field(ge=1)
    name: str = Field(min_length=1, max_length=128)
    type: DataSourceType
    env: Env = Env.DEV
    sensitive_level: SensitiveLevel = SensitiveLevel.INTERNAL
    endpoint: dict[str, Any]
    tags: list[str] = Field(default_factory=list)
    owner_id: int | None = None
    # 凭据：**瞬时对象**。写 Vault 后即丢，不落库、不进日志、不进账本
    credentials: dict[str, str] | None = None

    _check_credentials = field_validator("credentials")(_validate_credentials)


class DataSourceUpdate(BaseModel):
    """更新：``type`` 不可改（换类型等于换数据源，应新建）。

    ``credentials`` 留空表示**不变更**；提交新值会写 Vault 新版本并改指向。
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=128)
    env: Env | None = None
    sensitive_level: SensitiveLevel | None = None
    endpoint: dict[str, Any] | None = None
    tags: list[str] | None = None
    owner_id: int | None = None
    credentials: dict[str, str] | None = None

    _check_credentials = field_validator("credentials")(_validate_credentials)


class DataSourceFilter(BaseModel):
    """列表筛选。数组型参数由前端用逗号拼接，后端按 str 收再 split。"""

    model_config = ConfigDict(extra="forbid")

    project_id: int = Field(ge=1)
    type: str | None = None
    env: str | None = None
    sensitive_level: str | None = None
    status: str | None = None
    q: str | None = Field(default=None, max_length=128)
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=200)

    @staticmethod
    def split_csv(raw: str | None) -> list[str]:
        return [part.strip() for part in raw.split(",") if part.strip()] if raw else []


class ConnectivityResult(BaseModel):
    """连通性测试结果。

    业务结果（连不上）**不是请求错误**，所以用 200 + ``state`` 表达，
    而不是把 502 当作「接口失败」——否则前端分不清「后端挂了」和「目标库挂了」。
    """

    test_id: str
    state: Literal["pending", "succeeded", "failed", "timeout", "denied"]
    latency_ms: int | None = None
    # 后端已掩码的主机:端口，如 10.**.**.5:3306。**不含账号、不含库名**
    target: str | None = None
    server_version: str | None = None
    error_code: str | None = None
    message: str | None = None
    tested_at: datetime | None = None


class ConnectivityTestAccepted(BaseModel):
    """202 响应：只回 test_id 与初始态，前端随后轮询。"""

    test_id: str
    state: Literal["pending"] = "pending"


class ProbeSampling(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rows: int = Field(default=100, ge=0, le=1000)
    scope: Literal["full_tables", "selected_tables", "schema_only"] = "schema_only"
    tables: list[str] | None = None

    @model_validator(mode="after")
    def _selected_requires_tables(self) -> ProbeSampling:
        if self.scope == "selected_tables" and not self.tables:
            raise ValueError("scope=selected_tables 时必须给出 tables")
        return self


class ProbeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sampling: ProbeSampling = Field(default_factory=ProbeSampling)
    masking_policy: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------- 探查产物


class SchemaColumnRead(BaseModel):
    """一列的元数据。

    ``null_ratio`` / ``distinct_approx`` 在 M1 恒为 null：它们需要对源表跑
    ``COUNT``/``COUNT(DISTINCT)``，那是全表扫描，探查一次就能把演示库拖满。
    字段留着而不是删掉，是因为前端表格已经按「有值就显示」渲染——
    删字段会让前端改两次。
    """

    name: str
    data_type: str
    nullable: bool
    is_primary_key: bool
    comment: str | None = None
    null_ratio: float | None = None
    distinct_approx: int | None = None
    sensitive_level: SensitiveLevel
    masked: bool


class SchemaTableRead(BaseModel):
    schema_: str = Field(alias="schema")
    name: str
    comment: str | None = None
    row_count_approx: int | None = None
    columns: list[SchemaColumnRead]

    model_config = ConfigDict(populate_by_name=True)


class SchemaSnapshotRead(BaseModel):
    tables: list[SchemaTableRead]


class MetadataProfileRead(BaseModel):
    """探查档案详情。

    含 ``masked_samples``：库里的值**在写入前就已脱敏**，所以这里返回的不是
    「原文 + 前端打码」，而是唯一的形态。没有解敏接口，也不会有。
    """

    id: int
    data_source_id: int
    version: int
    status: ProbeStatus
    sampling: dict[str, Any] = Field(default_factory=dict)
    masking_policy: dict[str, Any] = Field(default_factory=dict)
    masked_samples: dict[str, list[dict[str, str]]] = Field(default_factory=dict)
    tables_count: int
    sha256: str
    expires_at: datetime | None = None
    error_message: str | None = None
    created_at: datetime
    created_by_display_name: str | None = None


class SampleRowsRead(BaseModel):
    """脱敏样本页。``truncated`` 让前端知道「还有更多」而不是以为就这些。"""

    rows: list[dict[str, str]] = Field(default_factory=list)
    truncated: bool = False


__all__ = [
    "ALLOWED_SECRET_KEYS",
    "CONNECTOR_REGISTRY",
    "ConnectivityResult",
    "ConnectivityTestAccepted",
    "ConnectorSpec",
    "DataSourceCreate",
    "DataSourceFilter",
    "DataSourceRead",
    "DataSourceUpdate",
    "MetadataProfileBrief",
    "MetadataProfileRead",
    "ProbeRequest",
    "ProbeSampling",
    "SampleRowsRead",
    "SchemaColumnRead",
    "SchemaSnapshotRead",
    "SchemaTableRead",
    "SecretRefView",
    "endpoint_model_for",
    "validate_endpoint",
]
