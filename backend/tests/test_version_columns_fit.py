"""引擎产出的版本串必须装得进它要写的那一列。

**这条用例是有来历的**：``engines.hocon.PARSER_VERSION`` 的实际取值是
``pyhocon+elt-render-1``（20 字符），而 ``hocon_artifact.parser_version`` 当初是
``varchar(16)``。宽度不够不会在 import 时发现、不会在渲染时发现，也不会在门禁时发现——
它在**最后一步落库**时才炸（SQLSTATE 22001），表现为「图跑完了、门禁判完了、请求 500」，
日志里前一条还是 ``gate_evaluated``。排查方向会被带向「谁把状态写坏了」，
而真因只是两个字符串的长度。

所以这里把「引擎的版本串」与「ORM 列宽」对起来断言：列宽**从模型元数据里读**，
不写死数字，改模型就会自动跟着比。
"""

from __future__ import annotations

import pytest
from sqlalchemy import String

from engines.hocon import PARSER_VERSION, RENDERER_VERSION
from engines.plan.schema import SchemaVersion
from models.pipeline import EtlPlan, HoconArtifact


def _column_length(model: type, column_name: str) -> int:
    """从 ORM 元数据里取列宽。取不到就直接失败，而不是跳过。"""
    column = model.__table__.columns[column_name]
    assert isinstance(column.type, String), f"{model.__name__}.{column_name} 不是字符串列"
    assert column.type.length is not None, f"{model.__name__}.{column_name} 没有长度上限"
    return int(column.type.length)


@pytest.mark.parametrize(
    ("label", "value", "model", "column_name"),
    [
        ("HOCON 解析器版本", PARSER_VERSION, HoconArtifact, "parser_version"),
        ("HOCON 渲染器版本", RENDERER_VERSION, HoconArtifact, "parser_version"),
        ("Plan DSL 版本", SchemaVersion.__args__[0], EtlPlan, "schema_version"),
    ],
)
def test_版本串装得下列宽(label: str, value: str, model: type, column_name: str) -> None:
    limit = _column_length(model, column_name)
    assert len(value) <= limit, (
        f"{label} {value!r} 长 {len(value)}，装不进 {model.__tablename__}.{column_name}"
        f"（varchar({limit})）——落库时会以 SQLSTATE 22001 在链路最后一步失败"
    )


def test_两个版本号列都比_默认值宽裕() -> None:
    """宽度定小了平时看不出来：默认值 ``1.0`` 永远装得下，只有真写引擎版本时才炸。"""
    for model, column_name in ((EtlPlan, "schema_version"), (HoconArtifact, "parser_version")):
        assert _column_length(model, column_name) >= 32, (
            f"{model.__tablename__}.{column_name} 太窄：版本号是自由格式串，会随需求变长"
        )
