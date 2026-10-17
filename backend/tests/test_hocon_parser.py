"""HOCON 解析与定位的用例（be-10 §6.4）。

解析器只有两条硬要求：

1. **对任何输入都不抛异常**——它被门禁的 ``check_syntax`` 调用，语义是「产出一条 finding」
   而不是「让门禁崩掉」；
2. **错误要可定位**（PRD §6.3.3）——行列号是前端在 CodeMirror 里高亮与滚动定位的唯一依据。
"""

from __future__ import annotations

import pytest

from engines.hocon import parse_and_validate, render_hocon
from engines.hocon.types import PARSER_VERSION, RenderContext
from engines.plan.schema import (
    EtlPlanDag,
    EtlPlanModel,
    FieldMapping,
    PlanInput,
    PlanOutput,
    RuntimeBudget,
)

_BAD_MISSING_BRACE = """env {
  job.mode = "BATCH"
}
source {
  Jdbc {
    url = "jdbc:mysql://host/db"
"""

_BAD_EXTRA_EQUALS = """source {
  Jdbc {
    url = = "jdbc:mysql://host/db"
  }
}
"""


def _rendered_hocon() -> str:
    plan = EtlPlanModel(
        inputs=[PlanInput(id="src1", data_source_id=1, schema_name="sales", table="orders")],
        outputs=[PlanOutput(id="out1", data_source_id=2, database="dw", table="sales_wide")],
        field_mappings=[
            FieldMapping(
                source_field="id",
                source_type="bigint",
                target_field="order_id",
                target_type="bigint",
            )
        ],
        dag=EtlPlanDag(),
        budget=RuntimeBudget(
            max_read_rows=1000,
            max_write_bytes=1000,
            max_duration_sec=60,
            max_amplification_ratio="1.5",
            max_reject_rate="0.05",
            warn_threshold_pct=80,
        ),
    )
    ctx = RenderContext(data_source_types={1: "mysql", 2: "doris"})
    return render_hocon(plan, ctx).content


def test_渲染器自己的产物解析回来是_ok() -> None:
    result = parse_and_validate(_rendered_hocon())
    assert result.ok, result.errors
    assert result.errors == ()
    assert result.parser_version == PARSER_VERSION


def test_config_是普通_dict_且点号键被展开成嵌套() -> None:
    result = parse_and_validate(_rendered_hocon())
    assert result.config is not None
    assert type(result.config) is dict
    assert type(result.config["env"]) is dict
    assert result.config["env"]["job"]["mode"] == "BATCH"
    assert result.config["sink"]["Jdbc"]["table"] == "sales_wide"


def test_缺右括号返回_ok_false_且行号指向出错处附近() -> None:
    result = parse_and_validate(_BAD_MISSING_BRACE)
    assert result.ok is False
    assert result.config is None
    assert len(result.errors) >= 1
    error = result.errors[0]
    # 缺的是 source 块的右括号：pyparsing 在文本末尾报错（第 7 行前后），
    # 断言「落在最后几行内」而不是一个精确值——精确值依赖 pyparsing 的版本
    assert 4 <= error.line <= 9
    assert error.column >= 1
    assert error.message


def test_错位的等号报出精确到行的位置() -> None:
    result = parse_and_validate(_BAD_EXTRA_EQUALS)
    assert result.ok is False
    error = result.errors[0]
    assert error.line == 3
    assert error.column >= 1


def test_未定义的占位符不在解析期求值_仍判为合法() -> None:
    """be-10 §6.4：``resolve=False``。把 ``${ENV}`` 判成语法错误就是典型的**假阳性**——
    用户手上的配置在 SeaTunnel 里跑得通，却被自家门禁挡住且无从下手。"""
    result = parse_and_validate('sink {\n  path = "/out/${STAGE}/x.csv"\n  bucket = ${BUCKET}\n}\n')
    assert result.ok, result.errors
    assert result.config is not None
    assert result.config["sink"]["path"] == "/out/${STAGE}/x.csv"
    assert result.config["sink"]["bucket"] == "${BUCKET}"


def test_错误行含有疑似凭据时被抹掉() -> None:
    """解析错误会回显原文，而被回显的是**用户手写过的**文本——
    他完全可能把真口令写进去（这正是门禁要拦的形态），所以回显必须脱敏（CLAUDE.md §5.2）。"""
    bad = 'password = "S3cr3t-Pa55" =\nsink {\n'
    result = parse_and_validate(bad)
    assert result.ok is False
    assert "S3cr3t-Pa55" not in result.errors[0].message


@pytest.mark.parametrize("content", ["", "   \n\n", "env {}", "#!/bin/sh\n", "a = [1,", "{{{"])
def test_任何输入都不抛异常(content: str) -> None:
    result = parse_and_validate(content)
    assert isinstance(result.ok, bool)
    if not result.ok:
        assert result.errors


@pytest.mark.parametrize("content", [None, b"a = 1", 123])
def test_非字符串输入也变成一条错误而不是异常(content: object) -> None:
    result = parse_and_validate(content)  # type: ignore[arg-type]
    assert result.ok is False
    assert result.errors[0].line >= 1


def test_空文件是合法的_hocon() -> None:
    result = parse_and_validate("")
    assert result.ok is True
    assert result.config == {}
