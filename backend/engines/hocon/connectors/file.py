"""文件族连接器模板（be-10 §6.5 的文件 / MinIO source）。

两族、四个模板：

- ``LocalFile``：本地/挂载路径（csv / json / parquet）；
- ``S3File``：MinIO 与 S3 兼容对象存储。

**为什么 S3File 不渲染 access_key / secret_key**：``engines/hocon/types.py`` 的令牌
白名单是**闭集合** ``(jdbc_url, username, password)``——只有这三个字段名能被令牌化，
换成 ``access_key`` 会当场抛错。把对象存储凭据塞进白名单等于给出一条「把任意字符串
变成真口令」的通路，代价远大于收益；MinIO 凭据改为由 be-15 从环境变量注入
（SeaTunnel 的 ``S3File`` 本来就支持从环境读 ``AWS_ACCESS_KEY_ID`` 一类变量）。

本地演示用不到这四个模板，但注册表要完整——否则一份「文件源 → Doris」的 Plan 会
在渲染期被判成「数据源类型不认识」，而那其实是**已知且可渲染**的形状。
"""

from __future__ import annotations

from engines.hocon.connectors.base import BlockTemplate, ConnectorKind

#: 文件族共有的参数顺序前缀。抽出来是为了让 LocalFile / S3File 的顺序同源，
#: 改一处不会只改一半（顺序表分叉 = 同一份 Plan 在不同 plugin 下字节不同）。
_COMMON_HEAD = ("path", "file_format_type", "delimiter", "read_columns", "schema", "encoding")


class FileSource(BlockTemplate):
    """本地/挂载路径读端。"""

    plugin_name: str = "LocalFile"
    kind: ConnectorKind = "source"
    key_order: tuple[str, ...] = (
        *_COMMON_HEAD,
        "skip_header_row_number",
        "parse_partition_from_path",
    )
    required_keys: frozenset[str] = frozenset({"path", "file_format_type"})
    secret_keys: tuple[str, ...] = ()


class FileSink(BlockTemplate):
    """本地/挂载路径写端。"""

    plugin_name: str = "LocalFile"
    kind: ConnectorKind = "sink"
    key_order: tuple[str, ...] = (
        *_COMMON_HEAD,
        "have_partition",
        "partition_by",
    )
    required_keys: frozenset[str] = frozenset({"path", "file_format_type"})
    secret_keys: tuple[str, ...] = ()


class S3FileSource(BlockTemplate):
    """MinIO / S3 兼容对象存储读端。"""

    plugin_name: str = "S3File"
    kind: ConnectorKind = "source"
    key_order: tuple[str, ...] = (
        *_COMMON_HEAD,
        "bucket",
        "skip_header_row_number",
        "parse_partition_from_path",
    )
    required_keys: frozenset[str] = frozenset({"path", "file_format_type", "bucket"})
    secret_keys: tuple[str, ...] = ()


class S3FileSink(BlockTemplate):
    """MinIO / S3 兼容对象存储写端。"""

    plugin_name: str = "S3File"
    kind: ConnectorKind = "sink"
    key_order: tuple[str, ...] = (
        *_COMMON_HEAD,
        "bucket",
        "have_partition",
        "partition_by",
    )
    required_keys: frozenset[str] = frozenset({"path", "file_format_type", "bucket"})
    secret_keys: tuple[str, ...] = ()
