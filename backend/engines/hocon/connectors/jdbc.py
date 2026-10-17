"""JDBC 连接器模板（be-10 §6.5 的 MySQL source / 通用 JDBC sink）。

四个库族（MySQL / PostgreSQL / Doris / StarRocks）都落到 ``Jdbc`` 插件：
它们都讲 JDBC 协议，差异只在 ``driver`` 与连接串（连接串是**令牌**，由 be-15 物化，
渲染器不知道也不需要知道它长什么样）。

**与设计文档 §6.5 的偏离（登记在此，不要静默改回）**：§6.5 给 Doris sink 单列了一张
``Doris`` 插件模板（``fenodes`` / ``sink.label-prefix`` / ``sink.enable-2pc``）。
be-10 的本次实现按「mysql/doris/postgres/starrocks → Jdbc」的约定统一走 JDBC 写入口，
Doris 专有插件模板留待 M3 按锁定镜像版本再定——它引入的 ``sink.label-prefix`` 需要
由运行期生成（否则重复提交会撞标签），与「渲染期不得含随机数」直接冲突，
先把冲突摆明比先写一张需要运行期特判的模板便宜。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from engines.hocon.connectors.base import BlockTemplate, ConnectorKind

#: ds_type → JDBC 驱动类名。**写死而不是让模型给**：驱动类名是连接器的知识，
#: 让 LLM 填一个不存在的类名会在运行期才炸（且报错在 SeaTunnel 容器里，最难查）。
JDBC_DRIVERS: dict[str, str] = {
    "mysql": "com.mysql.cj.jdbc.Driver",
    "postgres": "org.postgresql.Driver",
    "postgresql": "org.postgresql.Driver",
    "doris": "com.mysql.cj.jdbc.Driver",  # Doris 走 FE 的 MySQL 协议端口
    "starrocks": "com.mysql.cj.jdbc.Driver",
}


#: ds_type → JDBC URL 的 scheme 前缀。与上面的驱动表**必须同时改**：
#: 驱动类名与连接串协议是同一件知识的两半（``com.mysql.cj.jdbc.Driver`` 认识的
#: 只有 ``jdbc:mysql:``）。新增一个库族时漏改这里，症状是「驱动加载成功、
#: 连接串协议不认识」——报错来自 JVM 深处，最难查。
JDBC_URL_SCHEMES: dict[str, str] = {
    "mysql": "jdbc:mysql",
    "postgres": "jdbc:postgresql",
    "postgresql": "jdbc:postgresql",
    "doris": "jdbc:mysql",
    "starrocks": "jdbc:mysql",
}


def build_jdbc_url(ds_type: str, endpoint: Mapping[str, Any]) -> str:
    """由 ``data_source.endpoint``（非密定位信息）拼出 JDBC 连接串。

    **为什么不在渲染期拼、而要在执行期拼**：HOCON 制品里这条 URL 是令牌
    （``{{secret:1:jdbc_url}}``）。URL 本身不含口令，但它是「连接到哪里」——
    它出现在制品里等于把演示环境的主机与库名固化进一份要被审批、被留档、
    被反复拉取的不可变制品。令牌化之后，制品的字节与「库搬到哪儿」解耦。

    **为什么由 ``engines/`` 提供而不是写在执行器里**：驱动、协议、端口默认值
    是同一份连接器知识（``JDBC_DRIVERS`` 就在上面）。执行器里再写一遍
    ``f"jdbc:mysql://{host}:{port}"``，两处迟早分叉，而分叉的症状是
    「Probe 能连通、作业连不上」。

    ``endpoint`` 的键按各类型 DTO（``schemas/datasource.py``）取：
    mysql/postgres/doris/clickhouse 用 ``host``/``port``/``database``。
    缺 ``host`` 当场抛错——回一个 ``jdbc:mysql://None:3306/None`` 会在
    SeaTunnel 容器里报「未知主机 None」，排查方向被带到 DNS 上。
    """
    scheme = JDBC_URL_SCHEMES.get(ds_type)
    if scheme is None:
        raise ValueError(f"没有 JDBC 连接串模板的数据源类型：{ds_type!r}")
    host = endpoint.get("host")
    if not host:
        raise ValueError("endpoint 缺少 host，无法拼 JDBC 连接串")
    port = endpoint.get("port")
    database = endpoint.get("database")
    authority = f"{host}:{int(port)}" if port else str(host)
    url = f"{scheme}://{authority}"
    if database:
        url = f"{url}/{database}"
    return url


def build_exec_jdbc_url(ds_type: str, endpoint: Mapping[str, Any]) -> str:
    """**数据面视角**的 JDBC 连接串：``exec_host``/``exec_port`` 存在时覆盖控制面地址。

    为什么单独一个函数而不是给 ``build_jdbc_url`` 加开关：这两个函数回答的是两个
    不同的问题——「控制面怎么连到这台库」（探测、指纹、对账）与「SeaTunnel 容器怎么
    连到这台库」（作业读写）。用一个 ``prefer_exec=True`` 的布尔开关，调用点读起来
    仍然分不清自己在问哪一个，而**分不清的后果是作业连到控制面才通的地址**：
    探测绿、提交成功、作业在容器里 30 秒后报连接超时。

    覆盖只作用于这一条 URL；凭据、库名、类型一律不变。
    """
    overlay = dict(endpoint)
    if endpoint.get("exec_host"):
        overlay["host"] = endpoint["exec_host"]
    if endpoint.get("exec_port"):
        overlay["port"] = endpoint["exec_port"]
    return build_jdbc_url(ds_type, overlay)


class JdbcSource(BlockTemplate):
    """JDBC 读端。``query`` 与 ``table`` 二选一（渲染器在组装参数时强制）。

    ``database`` 只在 ``table`` 形态下出现：它决定这张表在作业内的**限定名**
    （``TablePath.of(database, table)``），而内层 SELECT 用的是 ``schema.table``。
    两边必须写同一份 schema——少一边就变成「SQL 里的表不存在」。
    """

    plugin_name: str = "Jdbc"
    kind: ConnectorKind = "source"
    key_order: tuple[str, ...] = (
        "url",
        "driver",
        "user",
        "password",
        "database",
        "query",
        "table",
        "where_condition",
        "partition_column",
        "partition_num",
        "connection_check_timeout_sec",
        "fetch_size",
        "properties",
    )
    #: url/driver 一定在；query 或 table 由渲染器单独保证（「或」关系表达不成集合）。
    required_keys: frozenset[str] = frozenset({"url", "driver"})
    secret_keys: tuple[str, ...] = ("user", "password")


class JdbcSink(BlockTemplate):
    """JDBC 写端。``table`` 是必填——没有目标表名就没有「写到哪儿」。"""

    plugin_name: str = "Jdbc"
    kind: ConnectorKind = "sink"
    key_order: tuple[str, ...] = (
        "url",
        "driver",
        "user",
        "password",
        "database",
        "table",
        "generate_sink_sql",
        "schema_save_mode",
        "data_save_mode",
        "batch_size",
        "primary_keys",
        "properties",
    )
    required_keys: frozenset[str] = frozenset({"url", "driver", "table"})
    secret_keys: tuple[str, ...] = ("user", "password")
