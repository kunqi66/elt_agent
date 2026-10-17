"""DTO 不许比它背后的列更窄——同一个值「存得下、读不出」是一类会反复复发的错。

**这条用例的来历**：``engines.hocon.PARSER_VERSION`` 是 ``pyhocon+elt-render-1``（20 字符）。
它先后撞了三堵墙，每堵墙的症状都不在同一个地方：

1. ``hocon_artifact.parser_version`` 是 ``varchar(16)`` → 落库时 SQLSTATE 22001，
   表现为「图跑完、门禁判完、请求 500」；
2. ``schemas.version.ManifestHocon.parser_version`` 限 16 → 冻结时构造清单报校验错；
3. ``schemas.pipeline.HoconArtifactRead.parser_version`` 限 16 → 拉制品接口 422，
   而库里那一行**好好存着**。

三处的共同点是：**写进去的东西读不出来**，而报错指向读的一侧。
所以这里不再逐个字段写断言，而是**扫一遍所有 DTO**：凡是名字以 ``_version``
结尾的字段，要么不限宽，要么不得窄于库列 / 引擎常量的实际取值。
"""

from __future__ import annotations

import importlib
import pkgutil
from typing import Any

import pytest
from pydantic import BaseModel
from pydantic.fields import FieldInfo
from sqlalchemy import String

from engines.hocon import PARSER_VERSION, RENDERER_VERSION
from engines.plan.schema import SchemaVersion
from models.pipeline import EtlPlan, HoconArtifact

#: 引擎里真实存在的版本串。DTO 的宽度至少要容得下它们。
ENGINE_VERSION_STRINGS: dict[str, str] = {
    "parser_version": PARSER_VERSION,
    "renderer_version": RENDERER_VERSION,
    "schema_version": SchemaVersion.__args__[0],
    "model_version": "deepseek-reasoner-0528",  # 厂商会改版本串，给个偏长的样例
    "prompt_template_version": "plan@1.0+autofix@1.0",
}


def _dto_models() -> list[type[BaseModel]]:
    """列出 ``schemas/`` 下所有 Pydantic 模型。用 import 遍历而不是手抄清单——
    手抄的清单会漏掉新增的 DTO，而漏掉的那一个正是下一个事故现场。"""
    package = importlib.import_module("schemas")
    models: list[type[BaseModel]] = []
    for module_info in pkgutil.iter_modules(package.__path__):
        module = importlib.import_module(f"schemas.{module_info.name}")
        for value in vars(module).values():
            if isinstance(value, type) and issubclass(value, BaseModel) and value is not BaseModel:
                models.append(value)
    return models


def _max_length(field: FieldInfo) -> int | None:
    """取字段的 ``max_length``。旧写法把约束塞在 ``Field(...)`` 里、新写法在 ``Annotated`` 里，
    两种都要认——认不出就返回 None（表示不限宽），而不是让用例炸在取值这一步。"""
    for meta in [*list(field.metadata), field]:
        limit = getattr(meta, "max_length", None)
        if isinstance(limit, int):
            return limit
    return None


def test_所有_dto_的版本字段都装得下引擎的版本串() -> None:
    offenders: list[str] = []
    for model in _dto_models():
        for name, field in model.model_fields.items():
            if not name.endswith("_version"):
                continue
            sample = ENGINE_VERSION_STRINGS.get(name)
            if sample is None:
                continue
            limit = _max_length(field)
            if limit is not None and limit < len(sample):
                offenders.append(
                    f"{model.__module__}.{model.__name__}.{name} 限 {limit} 字符，"
                    f"装不下 {sample!r}（{len(sample)} 字符）"
                )
    assert not offenders, "DTO 比实际值窄（写进去能成、读出来报错）：\n  " + "\n  ".join(offenders)


@pytest.mark.parametrize(
    ("value", "model", "column_name"),
    [
        (PARSER_VERSION, HoconArtifact, "parser_version"),
        (SchemaVersion.__args__[0], EtlPlan, "schema_version"),
    ],
)
def test_版本字段的宽度不超过列宽_否则又会在落库处炸(value: str, model: type, column_name: str) -> None:
    """反向也要守：DTO 比列宽还松，等于把「列装不下」的失败推迟到落库那一步。"""
    column = model.__table__.columns[column_name]
    assert isinstance(column.type, String) and column.type.length is not None
    limit = int(column.type.length)
    assert len(value) <= limit, f"{value!r} 长 {len(value)}，超过 {model.__tablename__}.{column_name}({limit})"


def test_存进_generation_context_的东西能被_api_dto_读回来() -> None:
    """「写的一侧多一个键」必须不能造成「读的一侧报错」。

    ``pipeline_version.generation_context`` 存的是 ``clients.llm.base.GenerationContext``
    的完整转储，而接口返回的是 ``schemas.version.GenerationContext``（前端的四个字段）。
    两份模型的字段集不同，一旦 API DTO 用了 ``extra="forbid"``，
    多出来的审计键（``provider`` / ``prompt_versions`` / ``usage_total``）就会让
    **每一行都读不出来**——而这个错会报在「答题 / 列表」这类毫不相干的接口上。
    """
    from clients.llm.base import GenerationContext as LlmGenerationContext
    from schemas.version import GenerationContext as ApiGenerationContext

    stored: dict[str, Any] = LlmGenerationContext(
        model_name="deepseek-chat",
        model_version="deepseek-chat-0324",
        prompt_template_version="plan@1.0",
        autofix_rounds=1,
        provider="deepseek",
        prompt_versions={"plan": "plan@1.0"},
        generated_at=None,
    ).model_dump(mode="json")
    stored["plan_issue_count"] = 3  # 模拟历史行里多出来的键（真的发生过）

    read_back = ApiGenerationContext.model_validate(stored)
    assert read_back.model_name == "deepseek-chat"
    assert read_back.autofix_rounds == 1
