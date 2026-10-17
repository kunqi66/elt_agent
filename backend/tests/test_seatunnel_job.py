"""运行期配置渲染的用例（be-15 §6.1 / §9）。

被守的是三件事，每一件写错的后果都不是「报错」而是「静默地干错事」：

1. **影子表改写只动 sink 段**——全文替换会把 transform SQL 里的同名字符串一起改掉，
   如果那是源表名，作业变成「读空影子表」：成功、零行，最难查。
2. **令牌全部替换、一个不剩**——漏一个就是拿字面量 ``{{secret:1:password}}`` 当口令去连库。
3. **敏感值不泄漏**——用一枚 canary 串跑完整流程，断言它不出现在任何日志里。

用例直接跑**真渲染器**产出的 HOCON（``render_hocon``），不是手写的样例：
手写样例会和渲染器分叉，而分叉正是这里要防的那件事。
"""

from __future__ import annotations

import logging

import pytest

from clients.seatunnel.job import InjectionReport, build_job_name, iter_sink_tables, render_config
from clients.secrets.materialize import MaterializedSecrets, SecretUnavailable
from engines.hocon import render_hocon
from engines.hocon.types import RenderContext
from engines.plan.schema import (
    EtlPlanDag,
    EtlPlanModel,
    FieldMapping,
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

MYSQL, DORIS = 1, 2

#: 出现在**源表所在的库**与目标表之间的同名陷阱：源表也叫 ``sales_wide``。
#: 这是刻意构造的最坏情形——全文替换会把它一起改掉。
CANARY_PASSWORD = "CANARY-P@ssw0rd-MUST-NOT-LEAK-9f3a"


def _ctx() -> RenderContext:
    return RenderContext(data_source_types={MYSQL: "mysql", DORIS: "doris"})


def _mapping(source: str, target: str, **kwargs: object) -> FieldMapping:
    return FieldMapping(
        source_field=source, source_type="varchar", target_field=target, target_type="varchar", **kwargs
    )


def _plan(*, source_table: str = "orders", target_table: str = "sales_wide") -> EtlPlanModel:
    return EtlPlanModel(
        inputs=[PlanInput(id="src1", data_source_id=MYSQL, schema_name="sales", table=source_table)],
        outputs=[PlanOutput(id="out1", data_source_id=DORIS, database="dw", table=target_table)],
        field_mappings=[_mapping("id", "order_id"), _mapping("amount", "pay_amount")],
        dag=EtlPlanDag(),
        budget=BUDGET,
    )


def _artifact(*, source_table: str = "orders", target_table: str = "sales_wide") -> str:
    return render_hocon(_plan(source_table=source_table, target_table=target_table), _ctx()).content


def _secrets() -> MaterializedSecrets:
    """两个数据源的全套令牌值。

    ``jdbc_url`` 也在这里（由执行器从 endpoint 拼好塞进 ``extra_values``）——
    渲染器对源与目标都发这枚令牌，只给 username/password 会让本用例的渲染必然失败。
    """
    return MaterializedSecrets(
        values={
            f"{MYSQL}:jdbc_url": "jdbc:mysql://10.0.0.1:3306/sales",
            f"{MYSQL}:username": "reader",
            f"{MYSQL}:password": CANARY_PASSWORD,
            f"{DORIS}:jdbc_url": "jdbc:mysql://10.0.0.2:9030/dw",
            f"{DORIS}:username": "writer",
            f"{DORIS}:password": "writer-pw",
        }
    )


# ---------------------------------------------------------------- 作业名


def test_作业名由三个整数拼成_不含任何用户输入() -> None:
    """作业名只允许 ``[A-Za-z0-9_.-]``：它要进 Zeta 的控制台与 URL 查询串。"""
    name = build_job_name(pipeline_id=7, version_no=3, run_id=42)
    assert name == "etl-7-v3-run42"
    assert set(name) <= set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-")


def test_作业名对同一个_run_是确定性的() -> None:
    """重投时的「按名查重」全靠它：两次算出不同名字 = 同一次运行在 Zeta 里起两个作业。"""
    args = {"pipeline_id": 1, "version_no": 2, "run_id": 3}
    assert build_job_name(**args) == build_job_name(**args)


# ---------------------------------------------------------------- 表名改写


def test_影子表改写只动_sink_段_源表同名时不被误伤() -> None:
    """**本文件最重要的一条**。构造源表与目标表同名（都是 ``sales_wide``）：

    全文 ``str.replace`` 会把 source 段里的表名也改成影子表，于是作业去读
    ``sales_wide__shadow_v3``（此刻不存在或为空）→ 作业成功、写出零行。
    """
    artifact = _artifact(source_table="sales_wide", target_table="sales_wide")
    assert artifact.count('"sales_wide"') >= 2, "构造失败：源表与目标表没有同时出现在制品里"

    rendered, report = render_config(
        hocon_content=artifact,
        secrets=_secrets(),
        shadow_tables={"sales_wide": "sales_wide__shadow_v3"},
        job_name="etl-1-v3-run9",
    )

    source_part, _, sink_part = rendered.partition("sink {")
    assert 'table = "sales_wide__shadow_v3"' in sink_part, "sink 段没有改到影子表"
    assert 'table = "sales_wide"' in source_part, "源表名被误改成了影子表"
    assert report.retargeted_tables == ["sales_wide->sales_wide__shadow_v3"]


def test_未登记的影子表映射不改变任何表名() -> None:
    """映射里没有的表**原样保留**——改写器不做猜测。

    拦截「目标表没有影子表」是执行器 ``_assert_writes_covered`` 的职责：
    在这里悄悄补一个默认名，等于把「准备单缺一条写」变成一个名字看起来很正常的作业。
    """
    artifact = _artifact()
    rendered, _ = render_config(
        hocon_content=artifact, secrets=_secrets(), shadow_tables={}, job_name="etl-1-v3-run9"
    )
    assert 'table = "sales_wide"' in rendered


def test_作业名同时改_env_段与_sink_无关的行() -> None:
    """``env.job.name`` 必须与提交时用的名字一致：Zeta 的 running-jobs 显示的是它。"""
    rendered, _ = render_config(
        hocon_content=_artifact(),
        secrets=_secrets(),
        shadow_tables={"sales_wide": "sales_wide__shadow_v3"},
        job_name="etl-1-v3-run9",
    )
    assert 'job.name = "etl-1-v3-run9"' in rendered
    assert "elt_sync_sales_wide" not in rendered


def test_iter_sink_tables_只给_sink_段里的表() -> None:
    artifact = _artifact(source_table="sales_wide", target_table="sales_wide")
    assert iter_sink_tables(artifact) == {"sales_wide"}


# ---------------------------------------------------------------- 令牌替换


def test_全部令牌被替换且渲染结果里一个不剩() -> None:
    artifact = _artifact()
    rendered, report = render_config(
        hocon_content=artifact,
        secrets=_secrets(),
        shadow_tables={"sales_wide": "sales_wide__shadow_v3"},
        job_name="etl-1-v3-run9",
    )
    assert "{{secret:" not in rendered
    assert CANARY_PASSWORD in rendered, "口令没有被替换进去，作业会拿字面量去连库"
    assert report.replaced_keys, "替换报告为空 = 报告没被填"
    assert len(report.replaced_keys) == len(set(report.replaced_keys))


def test_缺一个令牌值就整体失败_不做部分替换() -> None:
    """``jdbc_url`` 缺失时**一个字都不能替**。

    部分替换的产物是一份「一半真值一半字面量」的配置，交给 SeaTunnel 之后
    它会拿 ``{{secret:2:jdbc_url}}`` 当主机名去连——报错在 JVM 里，离这里很远。
    """
    secrets = _secrets()
    del secrets.values[f"{DORIS}:jdbc_url"]
    with pytest.raises(SecretUnavailable):
        render_config(
            hocon_content=_artifact(), secrets=secrets, shadow_tables={}, job_name="etl-1-v3-run9"
        )


def test_未物化的令牌指向的数据源会被报出来() -> None:
    """报的是**引用**（ds id + 字段名），不是值——它要进日志与异常消息。"""
    secrets = MaterializedSecrets(values={})
    with pytest.raises(SecretUnavailable) as caught:
        render_config(
            hocon_content=_artifact(), secrets=secrets, shadow_tables={}, job_name="etl-1-v3-run9"
        )
    message = str(caught.value)
    assert "secret:" in message
    assert CANARY_PASSWORD not in message


def test_没有转义语法_字面量令牌会撞上执行前的闸() -> None:
    """设计文档 §6.1 写了 ``$${secret:...}`` 转义，**本实现没有**（令牌语法不同）。

    把一个字面量令牌留在配置里的后果由执行前的 ``assert_no_unresolved_tokens`` 兜住：
    它抛错而不是放行。这条用例钉住「没有转义」这个事实——将来若真要加转义，
    得先让 ``TOKEN_RE`` 认得出转义形态，否则加了也只是把失败推迟到 Zeta 里。
    """
    from engines.hocon.types import assert_no_unresolved_tokens

    with pytest.raises(ValueError):
        assert_no_unresolved_tokens('url = "{{secret:1:jdbc_url}}"', where="测试")


# ---------------------------------------------------------------- 泄漏


def test_口令不出现在日志里(caplog: pytest.LogCaptureFixture) -> None:
    """canary 串跑完整渲染，断言它没被任何一行日志带出去。"""
    artifact = _artifact()
    with caplog.at_level(logging.DEBUG):
        rendered, report = render_config(
            hocon_content=artifact,
            secrets=_secrets(),
            shadow_tables={"sales_wide": "sales_wide__shadow_v3"},
            job_name="etl-1-v3-run9",
        )
    blob = "\n".join(record.getMessage() for record in caplog.records)
    assert CANARY_PASSWORD not in blob
    assert rendered  # 结果本身当然含真值，它在内存里
    # 报告是**唯一**允许出圈的形式：只有键名与计数
    payload = report.as_payload()
    assert CANARY_PASSWORD not in str(payload)
    assert set(payload) == {"placeholders_total", "replaced_keys", "retargeted_tables", "job_name"}


def test_替换报告里只有引用层信息() -> None:
    _, report = render_config(
        hocon_content=_artifact(),
        secrets=_secrets(),
        shadow_tables={"sales_wide": "sales_wide__shadow_v3"},
        job_name="etl-1-v3-run9",
    )
    dumped = str(report.as_payload())
    for secret_value in ("reader", "writer", CANARY_PASSWORD, "jdbc:mysql://10.0.0.1:3306/sales"):
        assert secret_value not in dumped


def test_injection_report_键集合是闭集合() -> None:
    """``as_payload`` 的形状只有一份定义（它要进日志与证据事件）。"""
    assert InjectionReport().as_payload() == {
        "placeholders_total": 0,
        "replaced_keys": [],
        "retargeted_tables": [],
        "job_name": "",
    }


def test_transform_段的_SQL_里的表名不被改写() -> None:
    """transform 段是**第三个**可能出现同名表名的地方（渲染器产出 ``FROM `库`.`表```）。

    这里改写的后果同样是静默的：SQL 里的表名是**读**的来源，改成影子表就是读空表。
    前一条用例已经覆盖了「源表同名」，这条额外钉住反引号限定的那种形态——
    它以 ``.`` 连着库名，任何按「词边界」做的替换都会在里面动手。
    """
    artifact = _artifact(source_table="sales_wide", target_table="sales_wide")
    rendered, _ = render_config(
        hocon_content=artifact,
        secrets=_secrets(),
        shadow_tables={"sales_wide": "sales_wide__shadow_v3"},
        job_name="etl-1-v3-run9",
    )
    transformed, _, sink_part = rendered.partition("sink {")
    assert "FROM `sales`.`sales_wide`" in transformed, "transform SQL 里的表名被改写了"
    assert 'table = "sales_wide__shadow_v3"' in sink_part
