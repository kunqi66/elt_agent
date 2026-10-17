"""制品指纹的参与集合与规范化规则（be-12 §6.1 / §6.2）。

## 一句话

指纹 = ``SHA256(canonical_json(信封))``。信封由**五部分**拼成，本模块定义「哪五部分」、
「每部分里哪些键必须剥掉」以及「数值怎么定标」。规范化只走 ``core.ids.canonical_json``，
第二处 ``json.dumps`` 就是指纹系统的第一次分叉（be-12 §6.1「唯一入口」）。

## 行 id 排除、语义引用保留（本模块最容易做错的一条）

be-12 §6.2 说「所有 id 排除」。这里的 id 指的是**数据库行 id**——
``etl_plan.id`` / ``hocon_artifact.id`` / ``version.id`` / ``tenant_id`` / ``project_id``。
它们是自增的编排事实，与制品内容无关，纳入会让「同一份制品」在不同 Pipeline 下算出不同指纹。

它**不**指 Plan 内容里的语义引用，下面这两类必须进指纹：

- ``inputs[0].data_source_id`` / ``outputs[0].data_source_id`` —— 把源库从 A 换成 B
  而指纹不变，等于放行一份「连的是 A 库」的方案带着旧指纹走完治理。这正是
  be-12 §6.5 要抓的指纹失效场景，所以 ``data_source_id`` **保留**。
- ``inputs[0].id`` / ``transforms[0].id`` / ``dag.edges[0].id`` 等**字符串节点 id** ——
  DAG 的边靠这些 id 互相引用，改 id 就是改图，必须进指纹。

所以排除**不是**「递归地删掉名为 ``id`` 的键」，而是：只在**每部分的顶层**剥离行级 id
（那里才是数据库行 id），其下的一律原样参与。``*_by`` 后缀（操作者）与时间戳则在任意深度剥离。
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import BaseModel, ConfigDict

from engines.hocon.types import RenderedHocon
from engines.plan.schema import EtlPlanModel, dump_plan

#: 规范化的格式版本，写进信封的 ``"v"``。改动规范化规则必须**递增**它——
#: 否则新旧规则会算出同一个键下的不同值，而调用方无从分辨。
FINGERPRINT_FORMAT_VERSION: int = 1

#: 定标位数。be-12 §6.1：比率 / 字节以外的小数一律 4 位小数的**字符串**。
_QUANTUM = Decimal("0.0001")

#: be-12 §6.2 的排除清单（常量形式）。这些键**不参与**指纹。逐项理由见模块 docstring
#: 与 be-12 §6.2 的表；``id`` 特殊——它只在每部分顶层才是行 id，故在 :func:`_is_excluded`
#: 里单独处理。
EXCLUDED_KEYS: frozenset[str] = frozenset(
    {
        # 数据库行 id（自增、可变的编排事实）
        "id",
        "pipeline_id",
        "tenant_id",
        "project_id",
        "version_id",
        "version_no",
        "current_version_id",
        "etl_plan_id",
        "hocon_artifact_id",
        "quality_contract_id",
        "supervision_contract_id",
        "source_metadata_profile_id",
        # 时间戳（审计事实，不是制品内容；纳入会让「重算指纹」永远失败）
        "created_at",
        "updated_at",
        "frozen_at",
        "generated_at",
        "deleted_at",
        # 操作者与备注
        "frozen_by",
        "note",
        # 来源信息（模型 / 提示词版本）：同一产物的复制应得到同一指纹
        "generation_context",
        # 派生校验结果：校验器 / 解析器升级不应让历史指纹失效
        "valid",
        "validation_errors",
        "sha256",
        "parser_version",
        "validation_result",
        # 可变编排状态
        "status",
    }
)

#: 形如 ``frozen_by`` / ``created_by`` / ``approved_by`` 的操作者列，按**后缀**排除。
#: 无法穷举成字面量，故单列。Plan 的字段名不以 ``_by`` 结尾（已核对 ``plan/schema.py``）。
EXCLUDED_KEY_SUFFIXES: tuple[str, ...] = ("_by",)


class ArtifactParts(BaseModel):
    """参与指纹的**完整**集合（be-12 §6.2）。

    ``frozen=True``：指纹输入是「冻结那一刻的事实」，构造后不许再被改——
    否则会出现「算指纹时算进去的」与「落库时写进去的」不是同一份数据。

    五部分都是普通 ``dict`` 而不是 ORM 行：engines 是纯函数层，不碰 ORM；
    由 service 把行映射成 dict 再传进来。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: ``dump_plan(plan)`` 的产物（含 ``schema`` 别名的原始键名）。
    etl_plan: dict[str, Any]
    #: HOCON **全文，逐字节**。不做 trim / 换行归一——HOCON 的字节就是它的身份（be-10 §6.3）。
    hocon_content: str
    #: ``quality_contract`` 的 ``rules`` / ``error_table_target`` / ``reject_rate_budget``。
    quality_contract: dict[str, Any]
    #: 五个运行预算 + ``warn_threshold_pct``。
    supervision: dict[str, Any]
    #: 源档案指纹。只取**指纹**，不取 snapshot 全文（全文会让指纹随采样波动）。
    source_metadata_sha256: str


def _is_excluded(key: str, *, top: bool) -> bool:
    """``top=True`` 表示该键处在某一部分的**顶层**（也就是数据库行的列位置）。"""
    if key.endswith(EXCLUDED_KEY_SUFFIXES):
        return True
    # 行级 id 才剥离；Plan 内部的 ``id`` 是 DAG 的语义引用，必须留
    if key == "id":
        return top
    return key in EXCLUDED_KEYS


def _scale(value: Decimal) -> str:
    try:
        return str(value.quantize(_QUANTUM))
    except InvalidOperation:
        # NaN / Infinity 无法定标。它们不该出现在制品里，但指纹函数**不抛异常**
        # （be-12 §3.2 异常契约），退化成原样字符串而不是让整次冻结炸掉。
        return str(value)


def normalize_scalar(value: Any) -> Any:
    """标量规范化：``Decimal`` / ``float`` → 4 位小数的字符串，其余原样。

    ``int`` 保持 ``int``（be-12 §6.1「整数原样十进制，无 .0」）。``bool`` 要在 ``int``
    之前判——``bool`` 是 ``int`` 的子类，漏判会把 ``True`` 变成 ``True``（看似没事，
    但 ``True`` 与 ``1`` 就分不开了，而它们语义不同）。
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, Decimal):
        return _scale(value)
    if isinstance(value, float):
        # 必须走 str 中转：Decimal(0.1) 会把 0.1 的二进制误差固化成 0.1000000000000000055…
        return _scale(Decimal(str(value)))
    return value


def _normalize(value: Any, *, top: bool) -> Any:
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for key, item in value.items():
            name = str(key)
            if _is_excluded(name, top=top):
                continue
            out[name] = _normalize(item, top=False)
        return out
    if isinstance(value, (list, tuple)):
        # 数组保持语义顺序，**不排序**：``field_mappings`` 的顺序是有意义的
        return [_normalize(item, top=False) for item in value]
    return normalize_scalar(value)


def normalize_part(part: Any) -> Any:
    """把一部分（Plan / 质量契约 / 督导契约）转成可入指纹的形态：剥排除项 + 数值定标。

    传入 dict 的**直接子键**按行级处理（那里才会剥 ``id``）；再往下递归时 ``id`` 保留。
    """
    return _normalize(part, top=True)


def build_parts(
    plan: EtlPlanModel | Mapping[str, Any],
    hocon_content: str | RenderedHocon,
    quality_contract: Mapping[str, Any],
    supervision: Mapping[str, Any],
    source_metadata_sha256: str,
) -> ArtifactParts:
    """从 service 手上的各种形态拼出 :class:`ArtifactParts`。

    接受 ``EtlPlanModel``（走 :func:`dump_plan`，序列化的唯一出口）或已经是 dict 的 Plan；
    接受 ``RenderedHocon`` 或裸字符串。收敛在这里，是为了让调用方不必各自拆一次。
    """
    plan_body = dict(plan) if isinstance(plan, Mapping) else dump_plan(plan)
    hocon_text = hocon_content.content if isinstance(hocon_content, RenderedHocon) else hocon_content
    return ArtifactParts(
        etl_plan=plan_body,
        hocon_content=hocon_text,
        quality_contract=dict(quality_contract),
        supervision=dict(supervision),
        source_metadata_sha256=source_metadata_sha256,
    )
