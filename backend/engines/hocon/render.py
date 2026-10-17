"""EtlPlan → SeaTunnel HOCON 的**确定性**渲染（be-10 §6.3）。

**同 Plan 必得同字节**（§6.3 七条规则），理由在 §6.7：HOCON 全文参与
``pipeline_version.sha256``，任何非确定性（字典序漂移、时间戳、随机 job 名）都会让
同一份 Plan 在不同时刻算出不同指纹，直接破坏不可变版本、审批绑定与 Capability 校验。
所以这里的每一条都是**契约**，不是实现细节。

七条规则的落点：

| 规则 | 落点 |
|---|---|
| 1 键序来自模板声明 | ``connectors.base.BlockTemplate.key_order``，本文件只按它遍历 |
| 2 缩进 2 空格 | ``connectors.base.INDENT`` |
| 3 无注释 | 本文件与模板里**一个 ``#`` / ``//`` 都不写**（模板注释也会被语法处理器吞掉） |
| 4 数值定标 / 转义 | ``connectors.base.render_value`` / ``connectors.base.quote`` |
| 5 数组保序 | ``connectors.base.render_value``（不 ``sorted``） |
| 6 无时间 / 随机 | 本文件不 import ``datetime`` / ``random``；``job.name`` 只由表名派生 |
| 7 恰好一个结尾换行 | :func:`render_hocon` 末尾的 ``+ "\\n"`` |

**抛错而不是返回问题列表**：本模块的输入按 be-10 §5.2 的顺序是**已通过 ``validate_plan``**
的 Plan，走到这里还缺表名、缺类型，说明是上游编排出错而不是用户方案有问题——
返回一份「可定位的问题列表」只会让调用方把它当业务错误展示给用户。
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any

from engines.hocon.connectors import (
    HoconValue,
    UnknownConnectorError,
    family_of,
    template_for,
)
from engines.hocon.connectors.base import render_key
from engines.hocon.connectors.jdbc import JDBC_DRIVERS
from engines.hocon.types import RenderContext, RenderedHocon, find_tokens, secret_token
from engines.plan.schema import EtlPlanModel, EtlTransform, FieldMapping, FieldMasking, PlanInput

# ---------------------------------------------------------------- 校验与转义原语

#: 标识符白名单。**必须**做这道校验：标识符来自 Plan（LLM 产物），
#: 直接用 f-string 拼进 SQL 就是一条注入通路（``a; DROP TABLE b`` 会被原样送进源库）。
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")

#: 类型名白名单：``decimal(18,4)`` / ``varchar(64)`` 这类带精度参数的形式也要放行。
_TYPE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_ ]*(\(\s*\d+\s*(,\s*\d+\s*)?\))?$")

#: 过滤表达式的最小防线：不允许语句分隔与注释符。滤器表达式按设计是「受限表达式」
#: （列名 + 运算符 + 字面量），出现 ``;`` 或注释符只可能是注入或模型跑偏。
_SQL_INJECTION_RE = re.compile(r";|--|/\*|\*/")

#: 目标库族 → 标识符引号。MySQL 族用反引号；PostgreSQL 与文件族用双引号。
_BACKTICK_TYPES = frozenset({"mysql", "doris", "starrocks"})

#: 掩码长度参数可接受的键名（Plan 里两种写法都出现过）。
_LENGTH_KEYS = ("length", "n")


def _ident(name: str, quote_char: str) -> str:
    """给标识符加引号。非法字符当场抛错，而不是拼进 SQL 再让数据库报错。"""
    if not _IDENT_RE.match(name):
        raise ValueError(f"非法标识符 {name!r}：只允许 [A-Za-z_][A-Za-z0-9_$]*")
    return f"{quote_char}{name}{quote_char}"


def _sql_type(data_type: str) -> str:
    """类型名白名单校验。类型名会被拼进 ``CAST(x AS <T>)``，同样不能是任意字符串。"""
    if not _TYPE_RE.match(data_type):
        raise ValueError(f"非法类型名 {data_type!r}")
    return data_type


def _guard_expression(expression: str) -> str:
    """过滤 / 子查询表达式的注入最小防线。"""
    if _SQL_INJECTION_RE.search(expression):
        raise ValueError(f"表达式含被禁止的语句分隔符或注释符: {expression!r}")
    return expression


def _literal(value: Any) -> str:
    """Plan 里的参数值 → SQL 字面量。字符串用单引号并按 SQL 规则把 ``'`` 翻倍。"""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    if value is None:
        return "NULL"
    raise ValueError(f"不支持的 SQL 字面量类型: {type(value).__name__}")


def _param(transform: EtlTransform, key: str) -> Any:
    try:
        return transform.params[key]
    except KeyError:
        raise ValueError(f"transform {transform.id!r}（kind={transform.kind}）缺少参数 {key!r}") from None


def _length_param(params: dict[str, Any]) -> int:
    """取掩码长度。缺参数**抛错**而不是默认一个长度——默认值会让「截断到几位」
    变成一个没人知道的口径，而它直接决定脱敏强度。"""
    for key in _LENGTH_KEYS:
        if key in params:
            value = params[key]
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"掩码长度 {key}={value!r} 必须是正整数")
            return value
    raise ValueError(f"掩码方式需要长度参数，可接受 {list(_LENGTH_KEYS)}，实到 {sorted(params)}")


def _masking_expr(expression: str, masking: FieldMasking) -> str:
    """掩码方法 → SQL 表达式（be-10 §6.3 的 ``mask_middle`` / ``hash`` / ``truncate``）。"""
    if masking.method == "mask_middle":
        return f"CONCAT(LEFT({expression}, 2), '****', RIGHT({expression}, 2))"
    if masking.method == "hash":
        return f"MD5({expression})"
    if masking.method == "truncate":
        return f"LEFT({expression}, {_length_param(masking.params)})"
    raise ValueError(f"未知的掩码方式: {masking.method!r}")  # pragma: no cover - Literal 已封闭


# ---------------------------------------------------------------- 内层 SELECT


def _quote_char(plan: EtlPlanModel, ctx: RenderContext) -> str:
    """引号字符由**目标库族**决定：内层 SELECT 的别名最终落在 sink 那一侧。"""
    ds_type = ctx.ds_type(plan.outputs[0].data_source_id if plan.outputs else plan.inputs[0].data_source_id)
    family_of(ds_type)  # 未登记的类型在这里就抛错，不留到拼 SQL 的时候
    return "`" if ds_type in _BACKTICK_TYPES else '"'


def _mapping_expr(mapping: FieldMapping, quote_char: str) -> str:
    """一个字段映射 → 一列表达式。"""
    expression = _ident(mapping.source_field, quote_char)
    if mapping.transform == "cast":
        expression = f"CAST({expression} AS {_sql_type(mapping.target_type)})"
    elif mapping.transform == "trim":
        expression = f"TRIM({expression})"
    elif mapping.transform == "none":
        pass
    elif mapping.transform == "concat":
        # 字段级 concat 需要「和谁拼」，而 FieldMapping 里没有第二个字段。
        # 静默当成恒等变换会让用户以为拼了、实际没拼——所以抛错，让它去 transforms 里表达。
        raise ValueError(
            f"字段映射 {mapping.source_field!r} 的 transform=concat 无法表达"
            "（FieldMapping 没有可拼接的其它字段），请改用 transforms 里 kind=concat 的派生列"
        )
    elif mapping.transform == "mask":
        if mapping.masking is None:
            raise ValueError(f"字段映射 {mapping.source_field!r} 声明了 mask 但没给 masking.method")
        expression = _masking_expr(expression, mapping.masking)
    elif mapping.transform == "hash":
        expression = f"MD5({expression})"
    else:  # pragma: no cover - Literal 已封闭取值域，兜底只为防上游塞进非法 dict
        raise ValueError(f"未知的字段变换: {mapping.transform!r}")

    # masking 与 transform 是两条独立声明（前端表单里就是两个控件），
    # 同时给且 transform 不是 mask 时，masking 作为**后置**步骤追加。
    if mapping.masking is not None and mapping.transform != "mask":
        expression = _masking_expr(expression, mapping.masking)
    return expression


def _transform_expr(transform: EtlTransform, quote_char: str) -> str:
    """一个派生列 → 一列表达式。未知 kind 抛错。"""
    kind = transform.kind

    def field(index: int = 0) -> str:
        try:
            name = transform.input_fields[index]
        except IndexError:
            raise ValueError(
                f"transform {transform.id!r}（kind={kind}）缺少第 {index + 1} 个输入字段"
            ) from None
        return _ident(name, quote_char)

    if kind == "cast":
        return f"CAST({field()} AS {_sql_type(str(_param(transform, 'type')))})"
    if kind == "concat":
        parts = [_ident(name, quote_char) for name in transform.input_fields]
        if not parts:
            raise ValueError(f"transform {transform.id!r}（kind=concat）没有输入字段")
        separator = transform.params.get("separator")
        if separator is None:
            return f"CONCAT({', '.join(parts)})"
        # 分隔符夹在相邻字段之间，顺序与 input_fields 一致（§6.3 规则 5）
        interleaved: list[str] = []
        for part in parts:
            interleaved.extend((_literal(str(separator)), part))
        return f"CONCAT({', '.join(interleaved[1:])})"
    if kind == "date_format":
        return f"DATE_FORMAT({field()}, {_literal(str(_param(transform, 'format')))})"
    if kind == "case_when":
        return _case_when_expr(transform)
    if kind == "constant":
        return _literal(_param(transform, "value"))
    if kind == "hash":
        return f"MD5({field()})"
    if kind == "truncate":
        return f"LEFT({field()}, {_length_param(transform.params)})"
    if kind == "mask":
        raw: dict[str, Any] = dict(transform.params)
        method = raw.pop("method", None)
        if method is None:
            raise ValueError(f"transform {transform.id!r}（kind=mask）缺少参数 'method'")
        return _masking_expr(field(), FieldMasking(method=method, params=raw))
    raise ValueError(f"未知的派生列 kind: {kind!r}")


def _case_when_expr(transform: EtlTransform) -> str:
    """``{"when": [{"if": ..., "then": ...}], "else": ...}`` → ``CASE WHEN ... END``。"""
    branches = _param(transform, "when")
    if not isinstance(branches, list) or not branches:
        raise ValueError(f"transform {transform.id!r}（kind=case_when）的 when 必须是非空数组")
    pieces: list[str] = []
    for index, branch in enumerate(branches):
        if not isinstance(branch, dict) or "if" not in branch or "then" not in branch:
            raise ValueError(f"transform {transform.id!r} 的 when[{index}] 必须是 {{'if': ..., 'then': ...}}")
        # 条件里的列名不重写（它可能是任意合法表达式），只做注入最小防线；
        # then/else 是**值**，按字面量转义——两者的信任级别不同。
        condition = _guard_expression(str(branch["if"]))
        pieces.append(f"WHEN {condition} THEN {_literal(branch['then'])}")
    if "else" in transform.params:
        pieces.append(f"ELSE {_literal(transform.params['else'])}")
    return "CASE " + " ".join(pieces) + " END"


def _input_ref(input_: PlanInput, quote_char: str, index: int) -> str:
    """一个输入 → FROM 子句里的一项。"""
    if input_.kind == "query":
        query = (input_.query or "").strip()
        if not query:
            raise ValueError(f"inputs[{index}] 是 query 形态但 query 为空")
        _guard_expression(query)
        return f"({query}) AS {_ident(_safe_alias(f'src_{input_.id}'), quote_char)}"
    if input_.kind == "file":
        # 文件源在 SeaTunnel 里以上游插件的输出表身份出现在 SQL 中，
        # 该名字由 Plan 的 table 给出（缺省退化为 input.id）。
        return _ident(_safe_alias(input_.table or input_.id), quote_char)
    parts = [part for part in (input_.schema_name, input_.table) if part]
    if not parts:
        raise ValueError(f"inputs[{index}] 是 table 形态但缺少 table 名")
    return ".".join(_ident(part, quote_char) for part in parts)


def _safe_alias(raw: str) -> str:
    """把 Plan 里的 id/别名收敛成合法标识符。非法字符替换为 ``_``——
    id 不在标识符白名单里属于**编排噪音**，不该让用户看到一句 SQL 语法错误。"""
    cleaned = re.sub(r"[^A-Za-z0-9_$]", "_", raw)
    return cleaned if cleaned and not cleaned[0].isdigit() else f"n_{cleaned}"


def _source_filters(plan: EtlPlanModel) -> list[str]:
    """只有 ``stage == "source"`` 的滤器进内层 WHERE。

    ``post_transform`` 的**不进内层**：语义上它是「变换之后的过滤」，
    放进内层会让它看到变换前的值——那是一次静默的语义改写。
    """
    expressions: list[str] = []
    for filter_ in plan.filters:
        if filter_.stage != "source":
            continue
        expressions.append(_guard_expression(filter_.expression))
    return expressions


def render_sql(plan: EtlPlanModel, ctx: RenderContext) -> str:
    """``field_mappings`` + ``transforms`` + ``filters(source)`` → 一条 SELECT。

    输出形态固定（一列一行、4 空格续行）：

    .. code-block:: sql

        SELECT
            `id` AS `order_id`,
            CONCAT(LEFT(`phone`, 2), '****', RIGHT(`phone`, 2)) AS `phone`
        FROM `sales`.`orders`
        WHERE `status` = 'paid' AND `amount` > 0

    ``ctx`` 只用于取目标库族（决定引号），SQL 文本本身不含任何外部事实——
    这是「同一份 Plan 换个数据源仍得同一份 HOCON」的前提之一。
    """
    if not plan.inputs:
        raise ValueError("Plan 没有 inputs，无法生成 SELECT")
    quote_char = _quote_char(plan, ctx)

    columns = [
        f"{_mapping_expr(m, quote_char)} AS {_ident(m.target_field, quote_char)}" for m in plan.field_mappings
    ]
    columns += [
        f"{_transform_expr(t, quote_char)} AS {_ident(t.output_field, quote_char)}" for t in plan.transforms
    ]
    if not columns:
        # 空列的 SELECT 是语法错误。与其渲染出来让门禁报「syntax」，不如在这里说清
        # 是编排漏了字段映射——门禁的 syntax 报错没有这个信息量。
        raise ValueError("Plan 没有可渲染的列（field_mappings 与 transforms 均为空）")

    references = [_input_ref(input_, quote_char, index) for index, input_ in enumerate(plan.inputs)]
    lines = ["SELECT"]
    lines += [
        f"    {column}{',' if index < len(columns) - 1 else ''}" for index, column in enumerate(columns)
    ]
    lines.append(f"FROM {', '.join(references)}")

    filters = _source_filters(plan)
    if filters:
        lines.append("WHERE " + " AND ".join(filters))
    return "\n".join(lines)


# ---------------------------------------------------------------- 顶层渲染


def _job_name(plan: EtlPlanModel) -> str:
    """作业名由**内容**派生（§6.3 规则 6）。

    刻意不带 pipeline 行 id 与时间：job 名会进 HOCON 全文、进而进制品指纹，
    带上这两样就等于每生成一次换一个指纹，不可变版本无从谈起。
    """
    table = re.sub(r"[^A-Za-z0-9_]+", "_", plan.outputs[0].table).strip("_")
    return f"elt_sync_{table or 'pipeline'}"


def _env_block(ctx: RenderContext, job_name: str) -> str:
    # 顺序写成字面量：env 不是连接器，没有模板可依，但仍必须有一个**写死的**顺序来源
    params: dict[str, HoconValue] = {
        "job.mode": "BATCH",
        "parallelism": ctx.parallelism,
        "job.name": job_name,
    }
    body = "\n".join(render_key(key, params[key], 1) for key in ("job.mode", "parallelism", "job.name"))
    return f"env {{\n{body}\n}}"


def _jdbc_params(ds_type: str, ds_id: int, ctx: RenderContext) -> dict[str, HoconValue]:
    """JDBC 连接参数一律写**令牌**，永不写值（CLAUDE.md §5.2）。

    这里不做「有没有 secret_ref 才渲染」的分支：``RenderContext`` 里根本没有「这个
    数据源有没有登记口令」这件事——那是 service 的事实。渲染器一律写令牌，
    无口令的源由门禁的 ``check_schema``（对着 ``secret_reference.key_names`` 判）报出来，
    比在渲染器里猜一个「大概没有口令」可靠。
    """
    params: dict[str, HoconValue] = {
        "url": secret_token(ds_id, "jdbc_url"),
        "driver": JDBC_DRIVERS[ds_type],
        "user": secret_token(ds_id, "username"),
        "password": secret_token(ds_id, "password"),
    }
    extras = ctx.extra_jdbc_params.get(ds_id)
    if extras:
        # 非密附加参数（serverTimezone 之类）。dict 没有顺序契约 → render_key 里按名字排序。
        params["properties"] = dict(extras)
    return params


def _incremental_condition(input_: PlanInput, quote_char: str) -> str:
    """增量抽取条件。值来自 Plan 的 ``start_value``（数据），不是当前时间。"""
    spec = input_.incremental
    if spec is None:  # pragma: no cover - 调用点已判空
        raise ValueError(f"输入 {input_.id!r} 没有增量规格")
    column = _ident(spec.column, quote_char)
    if spec.start_value is None:
        return f"{column} IS NOT NULL"
    return f"{column} >= {_literal(spec.start_value)}"


def _source_plugin(input_: PlanInput, ctx: RenderContext) -> str:
    ds_type = ctx.ds_type(input_.data_source_id)
    family = family_of(ds_type)
    template = template_for(ds_type, "source")

    if family == "jdbc":
        params: dict[str, HoconValue] = _jdbc_params(ds_type, input_.data_source_id, ctx)
        if input_.query:
            params["query"] = _guard_expression(input_.query)
        elif input_.table:
            params["table"] = input_.table
            if input_.schema_name:
                # ``database`` 与 ``table`` 一起决定**上游表在 SQL 里的名字**：
                # Jdbc source 按 ``TablePath.of(database, table)`` 注册输出表，
                # 而内层 SELECT 用的是 ``_input_ref`` 给的限定名（``\`db\`.\`tbl\```）。
                # 少这一个键，SQL 里就是 ``db.tbl``、上游只叫 ``tbl``——
                # SeaTunnel 的 Sql 插件会报「表不存在」，而报错里那个「不存在」的表
                # 名字与我们写的字符串又只差一个前缀，最容易被当成「库名写错了」查半天。
                params["database"] = input_.schema_name
        else:
            raise ValueError(f"输入 {input_.id!r} 是表/查询形态但 table 与 query 都为空")
        if input_.incremental is not None:
            params["where_condition"] = _incremental_condition(
                input_, "`" if ds_type in _BACKTICK_TYPES else '"'
            )
    else:
        if not input_.path:
            raise ValueError(f"输入 {input_.id!r} 是文件源但缺少 path")
        if input_.format is None:
            raise ValueError(f"输入 {input_.id!r} 是文件源但缺少 format（csv/json/parquet）")
        params = {"path": input_.path, "file_format_type": input_.format}
        if family == "s3_file" and input_.schema_name:
            # MinIO/S3 的桶名落在 Plan 的 schema 位（前端只有一个「库/schema」输入框）
            params["bucket"] = input_.schema_name

    return template.render(params, ctx, indent=1)


def _sink_plugin(output_ds_type: str, ds_id: int, database: str, table: str, ctx: RenderContext) -> str:
    family = family_of(output_ds_type)
    template = template_for(output_ds_type, "sink")
    params: dict[str, HoconValue]
    if family == "jdbc":
        params = _jdbc_params(output_ds_type, ds_id, ctx)
        params["table"] = table
        if database:
            params["database"] = database
        # 让 SeaTunnel 按**目标表结构**自己生成 ``INSERT INTO db.table (cols) VALUES (…)``。
        #
        # 这个键必须是 true（实测口径：Jdbc sink 在 false 且没有 ``query`` 时，作业提交后
        # 立刻在 writer 初始化阶段失败——它没有「要执行的语句」可执行）。曾经写 false 的
        # 理由是「不想让 sink 从表结构推断 upsert 语句」，那是把 ``generate_sink_sql``
        # 当成了 upsert 开关：它只决定「INSERT 语句由谁拼」，不影响写语义（写模式由
        # be-17 的影子表 + 原子 Swap 负责，与这个键无关）。
        #
        # 与「不写 upsert」真正相关的是列序：SeaTunnel 生成的 INSERT 按目标表列序取值，
        # 而内层 SELECT 的列序就是 ``field_mappings`` 的声明序——两者不一致时不会报错，
        # 只会有整列错位的数据落进去。这是 ``check_schema`` 要拦的东西。
        params["generate_sink_sql"] = True
    else:
        params = {"path": table, "file_format_type": "csv"}
        if family == "s3_file" and database:
            params["bucket"] = database
    return template.render(params, ctx, indent=1)


def _source_block(plan: EtlPlanModel, ctx: RenderContext) -> str:
    inner = "\n".join(_source_plugin(input_, ctx) for input_ in plan.inputs)
    return f"source {{\n{inner}\n}}"


def _transform_block(sql: str) -> str:
    return "transform {\n  Sql {\n" + render_key("query", sql, 2) + "\n  }\n}"


def _sink_block(plan: EtlPlanModel, ctx: RenderContext) -> str:
    inner = "\n".join(
        _sink_plugin(
            ctx.ds_type(output.data_source_id), output.data_source_id, output.database, output.table, ctx
        )
        for output in plan.outputs
    )
    return f"sink {{\n{inner}\n}}"


def render_hocon(plan: EtlPlanModel, ctx: RenderContext) -> RenderedHocon:
    """渲染完整 HOCON。段顺序固定 ``env → source → transform → sink``（§6.3）。

    返回的 ``tokens`` 是**从 final content 里正则抠出来的**（``find_tokens``），
    而不是渲染过程中顺手记的——后者会在「某条分支忘了登记」时悄悄少一个令牌，
    而少一个令牌的后果是 be-15 不会去物化它，作业拿着字面量 ``{{secret:...}}`` 去连库。
    """
    if not plan.inputs:
        raise ValueError("Plan 没有 inputs，无法渲染 source 段")
    if not plan.outputs:
        raise ValueError("Plan 没有 outputs，无法渲染 sink 段")

    job_name = _job_name(plan)
    blocks = [
        _env_block(ctx, job_name),
        _source_block(plan, ctx),
        _transform_block(render_sql(plan, ctx)),
        _sink_block(plan, ctx),
    ]
    content = "\n".join(blocks) + "\n"  # §6.3 规则 7：恰好一个结尾换行
    return RenderedHocon(content=content, tokens=find_tokens(content), job_name=job_name)


#: 渲染期会抛的异常。调用方（service）据此把「编排错了」与「用户方案有问题」分开处理。
RENDER_ERRORS = (ValueError, UnknownConnectorError, KeyError)

__all__ = ["RENDER_ERRORS", "render_hocon", "render_sql"]
