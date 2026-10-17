"""HOCON 确定性渲染的用例（be-10 §6.3 七条规则逐条守一遍）。

**这里的每条断言都是契约**（be-10 §6.7）：HOCON 全文参与 ``pipeline_version.sha256``，
渲染器任何一处非确定性都会让「同一份 Plan 在不同时刻算出不同指纹」，
而指纹是审批与 Capability 的绑定点。所以确定性不是「顺便测一下」，是主要被测对象。
"""

from __future__ import annotations

import re
from decimal import Decimal

import pytest

from engines.hocon import render_hocon, render_sql
from engines.hocon.connectors import UnknownConnectorError
from engines.hocon.connectors.base import render_value
from engines.hocon.types import (
    SECRET_FIELDS,
    RenderContext,
    assert_no_unresolved_tokens,
    find_tokens,
    secret_token,
)
from engines.plan.schema import (
    EtlFilter,
    EtlPlanDag,
    EtlPlanModel,
    EtlTransform,
    FieldMapping,
    FieldMasking,
    PlanInput,
    PlanOutput,
    RuntimeBudget,
)

BUDGET = RuntimeBudget(
    max_read_rows=1_000_000,
    max_write_bytes=10_000_000,
    max_duration_sec=1800,
    max_amplification_ratio="1.5000",
    max_reject_rate="0.0500",
    warn_threshold_pct=80,
)

MYSQL = 1
DORIS = 2


def _ctx(**overrides: object) -> RenderContext:
    return RenderContext(
        data_source_types={MYSQL: "mysql", DORIS: "doris"},
        **overrides,  # type: ignore[arg-type]
    )


def _mapping(source: str, target: str, target_type: str = "varchar", **kwargs: object) -> FieldMapping:
    return FieldMapping(
        source_field=source,
        source_type="varchar",
        target_field=target,
        target_type=target_type,
        **kwargs,  # type: ignore[arg-type]
    )


def _plan(**overrides: object) -> EtlPlanModel:
    defaults: dict[str, object] = {
        "inputs": [PlanInput(id="src1", data_source_id=MYSQL, schema_name="sales", table="orders")],
        "outputs": [PlanOutput(id="out1", data_source_id=DORIS, database="dw", table="sales_wide")],
        "field_mappings": [_mapping("id", "order_id"), _mapping("amount", "pay_amount")],
        "dag": EtlPlanDag(),
        "budget": BUDGET,
    }
    defaults.update(overrides)
    return EtlPlanModel(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------- 确定性


def test_同一份_plan_渲染一百次逐字节相同() -> None:
    plan, ctx = _plan(), _ctx()
    first = render_hocon(plan, ctx)
    for _ in range(99):
        again = render_hocon(plan, ctx)
        assert again.content == first.content
        assert again.tokens == first.tokens
        assert again.job_name == first.job_name


def test_field_mappings_顺序被尊重_打乱后_SQL_变但结构仍然合法() -> None:
    """§6.3 规则 5：数组保序。反例是「顺手 sorted 一下」——那会让 SELECT 列序漂移，
    而列序在 SeaTunnel 的 Sql→sink 之间是有语义的（按位置对齐）。"""
    forward = _plan(field_mappings=[_mapping("id", "order_id"), _mapping("amount", "pay_amount")])
    reversed_ = _plan(field_mappings=[_mapping("amount", "pay_amount"), _mapping("id", "order_id")])

    first = render_hocon(forward, _ctx()).content
    second = render_hocon(reversed_, _ctx()).content

    assert first != second
    assert first.index("order_id") < first.index("pay_amount")
    assert second.index("pay_amount") < second.index("order_id")


def test_文件末尾恰好一个换行() -> None:
    content = render_hocon(_plan(), _ctx()).content
    assert content.endswith("\n")
    assert not content.endswith("\n\n")
    assert content == content.rstrip("\n") + "\n"


def test_段顺序固定且不含任何注释() -> None:
    content = render_hocon(_plan(), _ctx()).content
    positions = [content.index(f"{name} {{") for name in ("env", "source", "transform", "sink")]
    assert positions == sorted(positions)
    assert "#" not in content
    assert "//" not in content


def test_缩进两空格且块头独占一行() -> None:
    content = render_hocon(_plan(), _ctx()).content
    for line in content.splitlines():
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        assert indent % 2 == 0, line
    assert content.startswith("env {\n")
    assert "\nsource {\n" in content  # 四个顶级段都在第 0 列
    assert "\n    url = " in content  # 插件内的键缩进 4 空格


def test_decimal_按四位定标输出且整数原样() -> None:
    """§6.3 规则 4。用 float 的话 0.1+0.2 那类二进制噪声会进文本、进而进指纹。"""
    assert render_value(Decimal("0.05")) == "0.0500"
    assert render_value(Decimal("1")) == "1.0000"
    assert render_value(7) == "7"
    assert render_value(True) == "true"
    assert render_value(False) == "false"


def test_数组保持原序_不排序() -> None:
    """§6.3 规则 5：顺序即语义（SELECT 列序、主键列序）。"""
    assert render_value(["z", "a", "m"]) == '["z", "a", "m"]'
    assert render_value([3, 1, 2]) == "[3, 1, 2]"


def test_env_段的键顺序与字面量都固定() -> None:
    content = render_hocon(_plan(), _ctx(parallelism=3)).content
    env_lines = content.split("source {")[0].splitlines()[1:4]
    assert env_lines == [
        '  job.mode = "BATCH"',
        "  parallelism = 3",
        '  job.name = "elt_sync_sales_wide"',
    ]


def test_job_name_只由表名派生_不含行_id_与时间() -> None:
    rendered = render_hocon(_plan(), _ctx())
    assert rendered.job_name == "elt_sync_sales_wide"
    assert "out1" not in rendered.job_name
    assert re.search(r"\d{4}-\d{2}-\d{2}", rendered.content) is None


# ---------------------------------------------------------------- 密钥红线


def test_渲染产物里只有令牌_没有明文口令() -> None:
    """be-10 §8：单测断言产物里 ``password =`` 后面跟的**只能是** ``${...}`` 形态的令牌。"""
    content = render_hocon(_plan(), _ctx()).content
    for key in SECRET_FIELDS:
        hocon_key = {"jdbc_url": "url", "username": "user", "password": "password"}[key]
        assigned = re.search(rf'^    {hocon_key} = "(.+)"$', content, re.MULTILINE)
        assert assigned is not None, f"缺少 {hocon_key}"
        assert assigned.group(1) in find_tokens(content)
    assert re.search(r'(?:password|user|url)\s*=\s*"(?!\{\{secret:)', content) is None


def test_令牌字段白名单外的名字当场抛错() -> None:
    assert secret_token(MYSQL, "password") == "{{secret:1:password}}"
    with pytest.raises(ValueError, match="不允许令牌化"):
        secret_token(MYSQL, "access_key")


def test_未替换的令牌在提交执行前硬失败() -> None:
    """「忘了替换令牌」是**静默地把字面量口令当口令发出去**的故障（types.py 的设计说明），
    所以这里刻意抛异常而不是返回 bool——返回 bool 迟早会被写成 log.warning 然后继续跑。"""
    assert_no_unresolved_tokens('url = "jdbc:mysql://h/db"', where="测试")
    with pytest.raises(ValueError, match="仍有未替换的密钥令牌"):
        assert_no_unresolved_tokens('url = "{{secret:1:jdbc_url}}"', where="提交 SeaTunnel 前")


def test_未知数据源类型抛错而不是回落默认模板() -> None:
    ctx = RenderContext(data_source_types={MYSQL: "kafka", DORIS: "doris"})
    with pytest.raises(UnknownConnectorError):
        render_hocon(_plan(), ctx)


def test_渲染上下文缺少数据源类型也抛错() -> None:
    with pytest.raises(KeyError):
        render_hocon(_plan(), RenderContext(data_source_types={DORIS: "doris"}))


# ---------------------------------------------------------------- render_sql


def test_render_sql_按目标库族加反引号且一列一行() -> None:
    sql = render_sql(_plan(), _ctx())
    assert sql == "\n".join(
        [
            "SELECT",
            "    `id` AS `order_id`,",
            "    `amount` AS `pay_amount`",
            "FROM `sales`.`orders`",
        ]
    )


def test_render_sql_对_postgres_目标用双引号() -> None:
    ctx = RenderContext(data_source_types={MYSQL: "postgresql", DORIS: "postgresql"})
    sql = render_sql(_plan(), ctx)
    assert '"sales"."orders"' in sql
    assert '"order_id"' in sql
    assert "`" not in sql


def test_render_sql_实现_cast_truncate_hash_与_concat_掩码() -> None:
    plan = _plan(
        field_mappings=[
            _mapping("amount", "pay_amount", transform="cast", target_type="decimal(18,4)"),
            _mapping("name", "short_name", masking=FieldMasking(method="truncate", params={"length": 4})),
            _mapping("email", "email_hash", masking=FieldMasking(method="hash", params={})),
            _mapping("phone", "phone", masking=FieldMasking(method="mask_middle", params={})),
        ]
    )
    sql = render_sql(plan, _ctx())
    assert "CAST(`amount` AS decimal(18,4)) AS `pay_amount`" in sql
    assert "LEFT(`name`, 4) AS `short_name`" in sql
    assert "MD5(`email`) AS `email_hash`" in sql
    assert "CONCAT(LEFT(`phone`, 2), '****', RIGHT(`phone`, 2)) AS `phone`" in sql


def test_render_sql_的_truncate_缺长度参数要报错而不是默认一个值() -> None:
    plan = _plan(
        field_mappings=[_mapping("name", "short_name", masking=FieldMasking(method="truncate", params={}))]
    )
    with pytest.raises(ValueError, match="掩码方式需要长度参数"):
        render_sql(plan, _ctx())


def test_render_sql_只把_source_阶段的滤器放进内层_WHERE() -> None:
    plan = _plan(
        filters=[
            EtlFilter(id="f1", expression="`status` = 'paid'", stage="source"),
            EtlFilter(id="f2", expression="`amount` > 0", stage="source"),
            EtlFilter(id="f3", expression="`masked` IS NOT NULL", stage="post_transform"),
        ]
    )
    sql = render_sql(plan, _ctx())
    assert "WHERE `status` = 'paid' AND `amount` > 0" in sql
    assert "masked" not in sql


def test_render_sql_实现派生列_concat_date_format_case_when_constant() -> None:
    plan = _plan(
        transforms=[
            EtlTransform(
                id="t1",
                kind="concat",
                input_fields=["first", "last"],
                output_field="full",
                params={"separator": "-"},
            ),
            EtlTransform(
                id="t2",
                kind="date_format",
                input_fields=["created_at"],
                output_field="day",
                params={"format": "%Y-%m-%d"},
            ),
            EtlTransform(
                id="t3",
                kind="case_when",
                input_fields=[],
                output_field="tier",
                params={"when": [{"if": "`amount` > 100", "then": "high"}], "else": "low"},
            ),
            EtlTransform(
                id="t4",
                kind="constant",
                input_fields=[],
                output_field="source_system",
                params={"value": "mysql"},
            ),
        ]
    )
    sql = render_sql(plan, _ctx())
    assert "CONCAT(`first`, '-', `last`) AS `full`" in sql
    assert "DATE_FORMAT(`created_at`, '%Y-%m-%d') AS `day`" in sql
    assert "CASE WHEN `amount` > 100 THEN 'high' ELSE 'low' END AS `tier`" in sql
    assert "'mysql' AS `source_system`" in sql


def test_render_sql_的派生列缺参数要报错() -> None:
    plan = _plan(
        transforms=[EtlTransform(id="t1", kind="constant", input_fields=[], output_field="x", params={})]
    )
    with pytest.raises(ValueError, match="缺少参数 'value'"):
        render_sql(plan, _ctx())


def test_render_sql_遇到未知的_derived_kind_抛错() -> None:
    """``kind`` 是 Literal，正常构造进不来未知值；这里用 ``model_construct`` 绕过校验，
    守的是「万一上游塞了一份没走强校验的 dict」时渲染器的兜底。"""
    rogue = EtlTransform.model_construct(
        id="t1", kind="explode", input_fields=[], output_field="x", params={}
    )
    plan = _plan(transforms=[rogue])
    with pytest.raises(ValueError, match="未知的派生列 kind"):
        render_sql(plan, _ctx())


def test_标识符注入当场抛错() -> None:
    """表名来自 LLM 产物，直接用 f-string 拼进 SQL 就是一条注入通路。"""
    plan = _plan(
        inputs=[PlanInput(id="src1", data_source_id=MYSQL, schema_name="sales", table="a; DROP TABLE b")]
    )
    with pytest.raises(ValueError, match="非法标识符"):
        render_sql(plan, _ctx())

    plan = _plan(field_mappings=[_mapping("id` = 1 OR 1=1 --", "order_id")])
    with pytest.raises(ValueError, match="非法标识符"):
        render_sql(plan, _ctx())


def test_滤器表达式里的语句分隔符被拒() -> None:
    plan = _plan(filters=[EtlFilter(id="f1", expression="1=1; DROP TABLE t", stage="source")])
    with pytest.raises(ValueError, match="被禁止的语句分隔符"):
        render_sql(plan, _ctx())


# ---------------------------------------------------------------- 嵌入与转义


def test_多行_SQL_用三引号嵌入且能解析回来() -> None:
    from engines.hocon import parse_and_validate

    content = render_hocon(_plan(), _ctx()).content
    assert 'query = """SELECT' in content
    result = parse_and_validate(content)
    assert result.ok, result.errors
    assert result.config is not None
    assert result.config["transform"]["Sql"]["query"].startswith("SELECT\n    `id`")


def test_引用形态由是否含换行决定_且两种形态都能被解析回来() -> None:
    """锁定「多行用三引号、单行转义」这条选择（be-10 §6.3 规则 4）。"""
    from engines.hocon import parse_and_validate
    from engines.hocon.connectors.base import quote

    assert quote('has "quote" and \\ backslash') == '"has \\"quote\\" and \\\\ backslash"'
    assert quote("SELECT\n  a\nFROM t") == '"""SELECT\n  a\nFROM t"""'
    # 值自身含三引号、或**以引号结尾** → 退回单行转义形态（否则三引号会提前闭合）
    assert quote('a """ b\nc') == '"a \\"\\"\\" b\\nc"'
    assert quote('SELECT\n  1 AS "x"') == '"SELECT\\n  1 AS \\"x\\""'
    # 末尾是换行而不是引号时，三引号仍然安全（`"*` 吃掉不了内容）
    assert quote('SELECT 1 AS "x"\n') == '"""SELECT 1 AS "x"\n"""'
    assert parse_and_validate("a = " + quote('SELECT\n  1 AS "x"')).config == {"a": 'SELECT\n  1 AS "x"'}
    assert parse_and_validate("a = " + quote('SELECT 1 AS "x"\n')).config == {"a": 'SELECT 1 AS "x"\n'}

    content = render_hocon(_plan(), _ctx()).content
    assert parse_and_validate(content).ok


def test_单行值里的引号与反斜杠被转义() -> None:
    # 内层 SQL 一定含换行 → 走三引号形态：SQL 里的 ``"`` 与 ``\`` 因此**原样保留**，
    # 解析回来必须与渲染出的 SQL 逐字符相同（转义与否由 quote() 的分支决定，见上一个用例）
    from engines.hocon import parse_and_validate

    plan = _plan(
        filters=[EtlFilter(id="f1", expression="`p` = 'a\"b' OR `q` = 'c\\d'", stage="source")],
    )
    content = render_hocon(plan, _ctx()).content
    result = parse_and_validate(content)
    assert result.ok, result.errors
    assert result.config is not None
    assert """WHERE `p` = 'a"b' OR `q` = 'c\\d'""" in result.config["transform"]["Sql"]["query"]


def test_文件源与_minio_源都能渲染出对应插件() -> None:
    local = _plan(
        inputs=[
            PlanInput(
                id="f1",
                data_source_id=9,
                kind="file",
                path="/data/orders.csv",
                format="csv",
                table="orders",
            )
        ]
    )
    ctx = RenderContext(data_source_types={9: "csv", DORIS: "doris"})
    content = render_hocon(local, ctx).content
    assert "LocalFile {" in content
    assert 'path = "/data/orders.csv"' in content
    assert 'file_format_type = "csv"' in content

    minio = _plan(
        inputs=[
            PlanInput(
                id="m1",
                data_source_id=9,
                kind="file",
                path="bucket/orders.csv",
                format="csv",
                schema_name="raw",
                table="orders",
            )
        ]
    )
    minio_content = render_hocon(minio, RenderContext(data_source_types={9: "minio", DORIS: "doris"})).content
    assert "S3File {" in minio_content
    assert 'bucket = "raw"' in minio_content
