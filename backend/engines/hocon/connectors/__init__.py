"""连接器模板注册表（be-10 §6.5）。

注册表的键是 ``(ds_type, 角色)`` 而不是 ``ds_type``：同一个类型在两端可以是**不同插件**
（MinIO 做源是 ``S3File``，做目标是本地文件写法）。用单键表的话，「哪个方向」这件事
就只能靠调用点传对了——那是一个静默错的形状。

``ds_type`` 的取值来自 ``models.enums.DataSourceType``，另加两个**别名**：
``postgres``（枚举里写的是 ``postgresql``，但 Plan/对话里两种写法都会出现）与
``s3``（MinIO 的通用写法）。别名是显式登记的，不是「兜底匹配前缀」。

**没有登记的取值一律抛错**：``oracle`` / ``clickhouse`` / ``excel`` / ``rest`` 在枚举里
存在但「仅注册接口，不真连」，回落到 JDBC 模板会渲染出一份看起来能跑的 HOCON。
"""

from __future__ import annotations

from typing import Literal

from engines.hocon.connectors.base import (
    ConnectorKind,
    ConnectorTemplate,
    HoconScalar,
    HoconValue,
    UnknownConnectorError,
)
from engines.hocon.connectors.file import FileSink, FileSource, S3FileSink, S3FileSource
from engines.hocon.connectors.jdbc import JDBC_DRIVERS, JdbcSink, JdbcSource

ConnectorFamily = Literal["jdbc", "local_file", "s3_file"]

#: ds_type → 连接器族。三族决定了「渲染器怎么组装参数」，也决定了标识符引号（见 render.py）。
_FAMILY_BY_DS_TYPE: dict[str, ConnectorFamily] = {
    **dict.fromkeys(("mysql", "postgres", "postgresql", "doris", "starrocks"), "jdbc"),
    **dict.fromkeys(("csv", "file", "json", "parquet"), "local_file"),
    **dict.fromkeys(("minio", "s3"), "s3_file"),
}

_TEMPLATE_BY_FAMILY: dict[ConnectorFamily, dict[ConnectorKind, ConnectorTemplate]] = {
    "jdbc": {"source": JdbcSource(), "sink": JdbcSink()},
    "local_file": {"source": FileSource(), "sink": FileSink()},
    "s3_file": {"source": S3FileSource(), "sink": S3FileSink()},
}

#: 扁平化的注册表视图，键是 ``(ds_type, 角色)``。**只读**用途：
#: 门禁的 ``check_resource`` 据此判断「这个连接器是否已注册」。
CONNECTOR_TEMPLATES: dict[tuple[str, ConnectorKind], ConnectorTemplate] = {
    (ds_type, kind): template
    for ds_type, family in _FAMILY_BY_DS_TYPE.items()
    for kind, template in _TEMPLATE_BY_FAMILY[family].items()
}


def family_of(ds_type: str) -> ConnectorFamily:
    """取连接器族。未登记的类型抛 :class:`UnknownConnectorError`。"""
    try:
        return _FAMILY_BY_DS_TYPE[ds_type]
    except KeyError as exc:
        known = ", ".join(sorted(_FAMILY_BY_DS_TYPE))
        raise UnknownConnectorError(f"未登记的数据源类型 {ds_type!r}；已登记：{known}") from exc


def template_for(ds_type: str, kind: ConnectorKind) -> ConnectorTemplate:
    """取 ``(ds_type, 角色)`` 对应的模板。未登记 → 抛错，**不回落默认模板**。"""
    return _TEMPLATE_BY_FAMILY[family_of(ds_type)][kind]


def registered_ds_types() -> tuple[str, ...]:
    """已登记的类型（有序，供错误文案与前端下拉对齐）。"""
    return tuple(_FAMILY_BY_DS_TYPE)


__all__ = [
    "CONNECTOR_TEMPLATES",
    "JDBC_DRIVERS",
    "ConnectorFamily",
    "ConnectorKind",
    "ConnectorTemplate",
    "FileSink",
    "FileSource",
    "HoconScalar",
    "HoconValue",
    "JdbcSink",
    "JdbcSource",
    "S3FileSink",
    "S3FileSource",
    "UnknownConnectorError",
    "family_of",
    "registered_ds_types",
    "template_for",
]
