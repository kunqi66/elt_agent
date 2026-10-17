"""目标端发布与清理的抽象层（be-18 §3.2）。

三条不可动摇的约定，写在最显眼的位置：

1. **失败路径绝不对正式表做任何写操作。** 影子表是本次运行的产物，正式表是业务
   正在读的表；两者在 Swap 之前没有任何共享状态。所以「发布失败」的正确含义是
   「正式表原样」，而**不是**「把正式表改回去」。本层每一条前置校验都在 DDL 之前返回。
2. **判定以执行后对账为准，不以 DDL 返回值为准。** DDL 的超时 / 连接中断 / 客户端
   被杀，都可能发生在「语句已经生效」之后——这三种情况下返回值全都不可信。
   唯一的判据是执行前后的指纹（见 :func:`reconcile_final_state`）。
3. **表名只在服务端产生。** 本层收下的 ``shadow_table`` / ``formal_table`` 一律来自
   ``execution_run`` 行；请求体里的任何表名都走不到这里。

与 be-18 §3.2 的差异（都是**加严**，逐条在代码处注明）：

- ``TableInfo`` 增加 ``primary_key_columns``：指纹公式需要主键，而它与列清单必须来自
  **同一次观测**——分两次读会有窗口，窗口里换了主键就会算出两个不可比的口径。
- ``PublishPlan`` 增加 ``allow_empty_shadow``：§6.3 第 4 条硬性前提（空影子表发布会
  把正式表清空）要靠 ``execution_run.read_rows`` 才能判，而 ``TargetPublishSwapArgs``
  由 be-05 冻结、字段不可加——由服务侧判好后经这一个布尔量传进来。
- ``PublishPlan.expected_columns`` 为空时**以正式表当前列为期望**（见
  :func:`assert_columns_replacable` 的说明）。设计里期望列由服务侧从冻结制品构造，
  但工具入参（冻结模型）承载不了它，执行器又不该为此多读一次目标库。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from core.errors import AppError, ErrorCode

#: 所有模型 ``extra="forbid"`` + ``frozen``：这一包里的对象会被写进证据 payload
#: 与工具 detail，多一个字段就多一处未经审阅的持久化面（与 ``harness/tools.py`` 同一取舍）。
_FORBID = ConfigDict(extra="forbid", frozen=True)

#: 执行后对账的三种结论。``unknown`` 一律按**失败**处置（要人核对），
#: 不许当成「大概成功」——在未知态上叠加第二次 Swap 才是真正的事故。
FinalState = Literal["published", "not_published", "unknown"]

#: 指纹口径。写进 detail 与 evidence payload：只覆盖行数与主键集合，
#: 不覆盖全部列内容（§6.2 的已知局限，必须随结论一起被看到）。
FINGERPRINT_SCOPE = "rowcount+pk_crc32"
#: 无主键时的降级口径：此时指纹只剩行数，判定力显著变弱，必须显式暴露。
FINGERPRINT_SCOPE_ROWCOUNT = "rowcount"


# ---------------------------------------------------------------- 匿名（无凭据）模型


class ColumnInfo(BaseModel):
    """一列的形态。``ordinal`` 参与比对：列顺序变化会让 ``SELECT *`` 的调用方读错列。"""

    model_config = _FORBID

    name: str
    ordinal: int
    type_text: str
    nullable: bool


class TableInfo(BaseModel):
    """一张表的只读快照。``exists=False`` 时其余字段为默认值。"""

    model_config = _FORBID

    name: str
    exists: bool
    columns: tuple[ColumnInfo, ...] = ()
    row_count: int | None = None
    table_fingerprint: str | None = None
    #: 主键列（按序号）。空集 = 该表没有主键，此时指纹退化为「只有行数」。
    primary_key_columns: tuple[str, ...] = ()

    @property
    def fingerprint_scope(self) -> str:
        return FINGERPRINT_SCOPE if self.primary_key_columns else FINGERPRINT_SCOPE_ROWCOUNT

    @property
    def shape(self) -> tuple[tuple[str, int, str, bool], ...]:
        """用于结构比对的规范化形态（去掉注释、字符集等与语义无关的差异）。"""
        return tuple((c.name, c.ordinal, c.type_text, c.nullable) for c in self.columns)


class RunRef(BaseModel):
    """一次运行的目标端身份。**全部来自服务端**（``execution_run`` 与 ``preparation``）。"""

    model_config = _FORBID

    run_id: int
    tenant_id: int
    project_id: int
    shadow_table: str
    formal_table: str
    error_table: str = ""


class PublishPlan(BaseModel):
    """发布计划。``shadow_table`` / ``formal_table`` 必须与 ``execution_run`` 一致。"""

    model_config = _FORBID

    run_id: int
    shadow_table: str
    formal_table: str
    #: 期望列。空 = 以正式表当前列为期望（见模块 docstring 第 3 条差异）。
    expected_columns: tuple[str, ...] = ()
    primary_key_columns: tuple[str, ...] = ()
    #: 允许影子表为空。只有 ``read_rows == 0``（源端本来就空）时才允许——
    #: 否则一次失败的装载会把正式表发布成空表，而质量报告看不出这个错误。
    allow_empty_shadow: bool = False


class RollbackPlan(BaseModel):
    """回滚计划。**只清影子表**：``restore_scope="shadow_only"``（be-13 §5.5）。"""

    model_config = _FORBID

    shadow_table: str
    formal_table: str
    #: 错误表在回滚时不动：它是本次运行唯一保留下来的诊断材料。
    keep_error_rows: bool = True


class PublishResult(BaseModel):
    """一次 Swap 的结论。``ok`` 与 ``final_state`` 必须自洽（``ok == (final_state=="published")``）。"""

    model_config = _FORBID

    ok: bool
    final_state: FinalState
    #: 交换后**持有本次数据**的表名（正常情况下就是正式表名，业务侧无感）。
    published_table: str | None = None
    #: 交换后持有**原正式表数据**的表名（影子表名下即为天然备份，§6.3）。
    previous_data_table: str | None = None
    pre_fingerprint: str | None = None
    post_fingerprint: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)


class CleanupResult(BaseModel):
    """一次回滚清理的结论。影子表原本不存在 → ``shadow_dropped=False`` 且 ``ok=True``。"""

    model_config = _FORBID

    ok: bool
    shadow_dropped: bool
    formal_unchanged: bool
    detail: dict[str, Any] = Field(default_factory=dict)


class ProvisionResult(BaseModel):
    """一次「备表」的结论（``target.ensure_tables``）。

    ``shadow_state`` 的三种取值都是**成功**：影子表这次建出来了 / 上次留下的是空表
    可以直接用 / 本来就在且为空。真正要区分的是它**有没有可能已经有数据**——
    那种情况不走这个模型，而是抛 ``TargetPrecheckError``（见 ``assert_provisionable``）：
    往一张已有数据的影子表里续写，会让质量报告与 Swap 都基于一份说不清来源的数据。
    """

    model_config = _FORBID

    ok: bool
    shadow_state: Literal["created", "exists_empty"]
    error_state: Literal["created", "exists"]
    #: 影子表是否与正式表逐列同形（建表后当场核对，不留到 Swap 那一步才发现）。
    shadow_matches_formal: bool
    detail: dict[str, Any] = Field(default_factory=dict)


class TargetEndpointRef(BaseModel):
    """目标端连接参数。

    ``password`` 用 ``SecretStr``：本对象会随异常栈与日志流动，明文口令一旦进了
    ``repr`` 就等于进了日志（CLAUDE.md §5.2）。取值只允许在建立连接的那一刻。
    """

    model_config = _FORBID

    kind: Literal["doris", "mysql"]
    data_source_id: int
    host: str
    port: int
    database: str
    username: str
    password: SecretStr
    charset: str = "utf8mb4"
    connect_timeout_sec: int = 8


class TargetBinding(BaseModel):
    """运行 → 目标数据源的解析结果（服务侧范围校验要用 ``database`` 与 ``kind``）。"""

    model_config = _FORBID

    data_source_id: int
    kind: Literal["doris", "mysql"]
    database: str
    endpoint: TargetEndpointRef


# ---------------------------------------------------------------- 接口


class TargetPublisher(Protocol):
    """目标端发布器（be-18 §3.2）。

    **失败路径不得对正式表做任何写操作。** 这句话是这一层的全部要点：
    影子表怎么写、写多少、写错没有，都是「本次运行」的问题；
    正式表是业务正在读的表，任何一次「为了修好而改回去」的尝试都会把它推进一个
    谁也没审批过的状态。所以本接口没有 ``restore``，只有 ``publish`` 与 ``cleanup``——
    缺一个方法就是缺一条危险路径。

    **所有判定以执行后对账为准，不以 DDL 返回值为准**（``reconcile_final_state``）。
    实现者唯一要保证的是：``publish`` 返回时，``pre_*`` / ``post_*`` 四个观测都取自
    **真实的表状态**，而不是某个语句的返回值。

    实现类的构造函数收一个 :class:`TargetEndpointRef`（构造入口只有
    ``clients.target.registry.build_publisher``）。
    """

    async def describe(self, table: str) -> TableInfo:
        """只读观测：表结构 + 行数 + 指纹。表不存在时返回 ``exists=False`` 而不是抛错。"""
        ...

    async def exists(self, table: str) -> bool:
        """表是否存在。"""
        ...

    async def provision(self, run: RunRef) -> ProvisionResult:
        """备齐本次运行要写的两张表：影子表（克隆正式表）与错误表。

        它**不动正式表**，因此是本接口里唯一可以在「还没跑作业」时就调用的写动作；
        也正因为如此，它必须把「正式表不存在」当成硬错误报出来——那说明目标端还没
        由仓管建好表，此时建出来的影子表没有可交换的对象，跑完也发布不了。
        """
        ...

    async def publish(self, run: RunRef, plan: PublishPlan) -> PublishResult:
        """原子交换影子表与正式表。"""
        ...

    async def rollback_cleanup(self, run: RunRef, plan: RollbackPlan) -> CleanupResult:
        """删除影子表，并断言正式表在本次动作中未被改动。"""
        ...


# ---------------------------------------------------------------- 异常


class TargetError(AppError):
    """目标端动作失败的基类。子类只给默认码，调用方按码分支。"""

    code = ErrorCode.SWAP_FAILED
    http_status = 502
    message = "目标端操作失败"


class UnsupportedTargetError(TargetError):
    """``registry`` 无法为该数据源类型构造 publisher（§7 最后一行：INTERNAL_ERROR）。

    归 500 而不是 4xx：调用方没有传错什么，是**生成期门禁**漏放了这一版制品。
    """

    code = ErrorCode.INTERNAL_ERROR
    http_status = 500
    message = "该目标数据源类型不支持原子发布"


class TargetScopeError(TargetError):
    """回滚范围校验不通过（§6.4 四连）。

    单独一个类而不是复用 ``ValidationError``：这条错误是**安全边界**上的一次
    拦截（「一次误操作最多删掉一个影子表」就是靠它成立的），
    需要一个能被单独统计与告警的名字。
    """

    code = ErrorCode.VALIDATION_ERROR
    http_status = 422
    message = "回滚范围校验未通过，拒绝执行任何清理动作"


class TargetPrecheckError(TargetError):
    """目标端只读预检未通过（表不存在 / 结构不一致 / 期望列缺失 / 空影子表）。

    此时**还没有执行任何 DDL**，正式表原样；调用方据此把 run 置 failed 并注明原因。
    """

    code = ErrorCode.SWAP_FAILED
    http_status = 500
    message = "目标端预检未通过，未对正式表做任何写操作"


class TargetDDLError(TargetError):
    """DDL 执行本身报错。**不代表没生效**——调用方仍必须走对账（§6.2）。"""

    code = ErrorCode.SWAP_FAILED
    http_status = 500
    message = "目标端 DDL 执行失败"


class TargetCleanupError(TargetError):
    """回滚清理失败（§7：ROLLBACK_FAILED 500）。"""

    code = ErrorCode.ROLLBACK_FAILED
    http_status = 500
    message = "影子表清理失败"


def classify_target_error(exc: Exception, *, failure: type[TargetError] = TargetDDLError) -> TargetError:
    """把驱动异常翻译成 ``AppError``。

    只按**异常类名与文本**分类，不 import 各驱动的异常类型（与
    ``clients/datasource_probe._classify`` 同一取舍）：新驱动没登记时最坏落到
    「不可达」——一个仍然正确的降级方向。

    ``KeyError`` / ``TypeError`` / ``AttributeError`` / ``IndexError`` 归
    ``INTERNAL_ERROR``：那是**我们自己的代码缺陷**（解析结果集写错了键名），
    把它译成「连接失败」会把排查方向从代码带到网络与凭据上去。
    """
    if isinstance(exc, TargetError):
        return exc
    if isinstance(exc, KeyError | TypeError | AttributeError | IndexError):
        return TargetError(
            f"目标端客户端内部错误：{type(exc).__name__}", code=ErrorCode.INTERNAL_ERROR, http_status=500
        )
    name = type(exc).__name__.lower()
    text = str(exc).lower()
    if "timeout" in name or "timed out" in text:
        return TargetError("目标端操作超时", code=ErrorCode.DATASOURCE_TIMEOUT, http_status=504)
    if "auth" in name or "access denied" in text or "password" in text:
        return TargetError("目标端认证失败（账号或口令错误）", code=ErrorCode.DATASOURCE_AUTH_FAILED)
    if "refused" in text or "unreachable" in text or "getaddrinfo" in text or "unknown host" in text:
        return TargetError("目标端不可达", code=ErrorCode.DATASOURCE_UNREACHABLE)
    # 消息只带类型名：驱动异常文本里常裹着连接串与主机名（CLAUDE.md §6）
    return failure(f"目标端操作失败：{type(exc).__name__}")


# ---------------------------------------------------------------- 纯规则


def reconcile_final_state(
    *, pre_shadow: str | None, pre_formal: str | None, post_formal: str | None, post_shadow: str | None
) -> FinalState:
    """§6.2 的对账表。**纯函数**：同一个结论在任何实现（Doris / MySQL）上都成立。

    | 观测 | 判定 |
    |---|---|
    | ``post_formal == pre_shadow`` 且 ``post_shadow == pre_formal`` | ``published`` |
    | ``post_formal == pre_formal`` | ``not_published`` |
    | 其余（指纹读不到 / 两表本来就一样 / 只换了一半） | ``unknown`` |

    「两表发布前指纹相同」单列为 ``unknown``：此时交换前后无法区分，
    判定 ``published`` 会让一次**根本没生效**的 Swap 被记成成功。
    宁可让人核对一次，也不许在正式表的实际内容上赌一次。
    """
    if pre_shadow is None or pre_formal is None or post_formal is None or post_shadow is None:
        return "unknown"
    if pre_shadow == pre_formal:
        return "unknown"
    if post_formal == pre_shadow and post_shadow == pre_formal:
        return "published"
    if post_formal == pre_formal:
        return "not_published"
    return "unknown"


def assert_columns_replacable(shadow: TableInfo, formal: TableInfo) -> None:
    """Swap 前的结构断言：两张表的列形态必须**逐列一致**。

    §6.3 对 Doris 要求「列名、列顺序、列类型、null 属性完全一致」，§6.4 对 MySQL
    只写了「视图依赖检查」，本实现对 MySQL **同样要求逐列一致**（加严）：
    MySQL 的 ``RENAME TABLE`` 技术上不在乎结构差异，于是结构差异会**静默**变成
    业务侧的表结构变更——正是「静默错」的定义。要放宽必须先有「目标端表结构
    由谁负责」的结论（be-18 §10 未决项），在那之前一律拒绝。

    ``expected_columns`` 的语义因此收敛为「正式表当前列」，由本函数用
    ``formal.columns`` 表达（见模块 docstring 第 3 条差异）：空影子表 / 缺列
    都在这里一次性判掉，不需要再单独传一份期望列清单。
    """
    if not formal.exists:
        raise TargetPrecheckError(
            f"正式表不存在：{formal.name}", details={"reason": "formal_missing", "table": formal.name}
        )
    if not shadow.exists:
        raise TargetPrecheckError(
            f"影子表不存在：{shadow.name}", details={"reason": "shadow_missing", "table": shadow.name}
        )
    if shadow.shape != formal.shape:
        raise TargetPrecheckError(
            "影子表与正式表的列形态不一致，拒绝交换",
            details={
                "reason": "column_mismatch",
                "shadow_table": shadow.name,
                "formal_table": formal.name,
                "diffs": column_diffs(shadow, formal),
            },
        )


def assert_publishable(shadow: TableInfo, formal: TableInfo, *, allow_empty_shadow: bool) -> None:
    """Swap 前的全套只读预检（§6.1 条件 5 + §6.3 的硬性前提）。

    **服务侧与执行器都调它**：服务侧调用是为了「门禁不过就不签发令牌」（比事后
    拒签更强），执行器调用是因为它不该相信调用方——两次调用之间隔着一个队列，
    而这个窗口里目标端可能已经变了。同一个函数 = 同一套严格度。
    """
    assert_columns_replacable(shadow, formal)
    # 空影子表是最危险的一种成功：装载一条没读进来，质量报告可能照样 passed
    # （0 行没有违约行），于是 Swap 会把正式表换成空表——而「换成功」这件事
    # 在库里看起来完全正常。只有 read_rows == 0（源端本来就是空的）才允许。
    if shadow.row_count == 0 and not allow_empty_shadow:
        raise TargetPrecheckError(
            f"影子表 {shadow.name} 为空，拒绝发布（会清空正式表）",
            details={
                "reason": "empty_shadow",
                "shadow_table": shadow.name,
                "formal_table": formal.name,
                "formal_row_count": formal.row_count,
            },
        )


def column_diffs(shadow: TableInfo, formal: TableInfo) -> list[str]:
    """逐列差异的人话描述（进 detail 与日志，供排查「为什么被拦下」）。"""
    diffs: list[str] = []
    by_name = {c.name: c for c in shadow.columns}
    for column in formal.columns:
        other = by_name.get(column.name)
        if other is None:
            diffs.append(f"正式表列 {column.name} 在影子表中缺失")
            continue
        if other.type_text != column.type_text:
            diffs.append(f"{column.name}: 类型 {other.type_text} != {column.type_text}")
        if other.nullable != column.nullable:
            diffs.append(f"{column.name}: nullable {other.nullable} != {column.nullable}")
        if other.ordinal != column.ordinal:
            diffs.append(f"{column.name}: 列序 {other.ordinal} != {column.ordinal}")
    extra = sorted(set(by_name) - {c.name for c in formal.columns})
    if extra:
        diffs.append(f"影子表多出列：{'、'.join(extra)}")
    return diffs


def split_qualified(name: str) -> tuple[str | None, str]:
    """``db.table`` → ``("db", "table")``；``table`` → ``(None, "table")``。

    多段（``a.b.c``）直接拒绝：目标库只有一层库名，多出来的层级说明这个字符串
    不是我们构造的——放行等于把「表名只在服务端产生」这条约定交给运气。
    """
    parts = [p.strip() for p in name.split(".")]
    if len(parts) == 1:
        return None, parts[0]
    if len(parts) == 2 and all(parts):
        return parts[0], parts[1]
    raise TargetScopeError(f"表名不是合法的 [库.]表 形态：{name!r}")


def table_of(name: str) -> str:
    """取表名部分（不带库名前缀）。"""
    return split_qualified(name)[1]


def assert_unqualified_or_same_database(name: str, *, database: str) -> None:
    """§6.4 的范围校验之三：库名前缀（若有）必须就是目标数据源的库。

    「影子表在 A 库、正式表在 B 库」这种输入一次也不许到达 DDL——
    跨库的 ``REPLACE`` / ``RENAME`` 会删掉一个我们从未审批过的对象。
    """
    prefix, _table = split_qualified(name)
    if prefix is not None and prefix != database:
        raise TargetScopeError(
            f"表 {name!r} 的库名与目标数据源不一致（期望 {database!r}）",
            details={"reason": "cross_database", "table": name, "expected_database": database},
        )


def assert_iterable_of_str(values: Iterable[Any], *, field: str) -> tuple[str, ...]:
    """把外部来的列表收成字符串元组（``None`` 直接拒绝，不许静默变成空集）。"""
    out: tuple[str, ...] = tuple(str(item) for item in values)
    if not all(item.strip() for item in out):
        raise TargetPrecheckError(f"{field} 里有空元素", details={"reason": "empty_column_name"})
    return out
