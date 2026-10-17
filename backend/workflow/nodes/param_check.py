"""参数完备性检查（纯函数，be-09 §6.2）。

``find_missing_params`` 是**纯函数**：不依赖会话、不依赖 LLM、不读时钟。
理由是提问文案要稳定——同一次需求解析出同一个缺失集合，
才能对「用户答完一题就刷一次页面」这种交互给出同样的问卷，否则界面会跳。

返回顺序固定为 :data:`RequiredParam` 的声明顺序，与「字典序」无关：
顺序变了会让同一次澄清里问题的先后顺序变化，而用户在照着念。

``EtlIntent`` 定义在这里而不是 ``intent_parse.py``：它是**参数检查的输入契约**，
而 ``intent_parse`` 只是它的一个生产者。放在消费者一侧，
``param_check`` 就不必反向 import 一个会 import LLM 客户端的模块——
纯函数层只要碰一下带网络依赖的模块，就再也回不到「无 IO 可单测」。
"""

from __future__ import annotations

from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "EtlIntent",
    "MissingParam",
    "REQUIRED_PARAM_ORDER",
    "RequiredParam",
    "find_missing_params",
    "param_check_node",
]

#: 五个必需项（be-09 §6.2）。声明顺序 = 提问顺序 = 状态里 ``missing_params`` 的顺序。
RequiredParam = Literal[
    "primary_key",
    "incremental_column",
    "dedup_rule",
    "target_table",
    "quality_threshold",
]

REQUIRED_PARAM_ORDER: Final[tuple[RequiredParam, ...]] = (
    "primary_key",
    "incremental_column",
    "dedup_rule",
    "target_table",
    "quality_threshold",
)

_BASE = ConfigDict(extra="forbid", frozen=True)


class EtlIntent(BaseModel):
    """意图解析（LLM）的结构化产物。

    ``collected_params`` 与它**并存**而不是二选一：``intent`` 是「模型这一次读到了什么」，
    ``collected_params`` 是「用户到目前为止确认了什么」。用户显式回答的优先级更高，
    所以 :func:`find_missing_params` 先看 ``collected``、再回落到 ``intent``。
    把两者合并成一个 dict 就再也分不清「模型猜的」与「人定的」，
    而「让模型猜的参数直接进 HOCON」是这条链路上最不该发生的事。
    """

    model_config = _BASE

    #: 需求原文。落 state 是必要的（后续每轮 Prompt 都要带上它），
    #: 但它**不进日志、不进证据账本**（be-09 §8）。
    requirement: str = ""
    #: 一句话概括的目的，用于前端展示与 Prompt 摘要
    goal: str = ""

    primary_key: list[str] = Field(default_factory=list)
    incremental_column: str | None = None
    dedup_rule: str | None = None
    target_table: str | None = None
    #: 质量阈值（拒绝率上限）。定标字符串（``"0.0500"``）而不是 float：
    #: 它最终要写进 ``quality_contract.reject_rate_budget``，中间不允许出现
    #: 「0.05 与 0.0500 是不同的两个 float 值」这类二进制浮点差异。
    quality_threshold: str | None = None

    def dump(self) -> dict[str, Any]:
        """入 state 的唯一出口（``mode="json"`` 保证无 datetime/Decimal）。"""
        return self.model_dump(mode="json")


class MissingParam(BaseModel):
    """一个缺失的必需项 + 它的提问文案。

    带上 ``prompt`` 而不是让调用方按 ``param`` 查一张文案表：
    文案表会有两个消费点（后端 interrupt 载荷、前端问卷），迟早分叉。
    """

    model_config = _BASE

    param: RequiredParam
    prompt: str
    kind: str = "text"
    options: list[dict[str, str]] | None = None
    hint: str | None = None
    meta: dict[str, Any] | None = None


#: 提问文案（稳定，不随机、不拼时间）。逐项写死而不是拼模板：
#: 「请指定 XXX」这种模板对 ``quality_threshold`` 会给出一个不像人话的问题。
_PROMPTS: Final[dict[RequiredParam, str]] = {
    "primary_key": "请指定去重主键",
    "incremental_column": "增量切片使用哪一列？",
    "dedup_rule": "重复数据如何取舍（去重规则）？",
    "target_table": "目标表叫什么名字？",
    "quality_threshold": "可接受的拒绝率上限是多少？",
}

_HINTS: Final[dict[RequiredParam, str]] = {
    "primary_key": "用于影子表去重与 Swap 后的唯一性校验",
    "incremental_column": "源表中用于增量切片的时间或自增列",
    "dedup_rule": "例如「保留 updated_at 最新的一条」",
    "target_table": "目标库中承接本次同步的表名",
    "quality_threshold": "范围 0~1 的比率，如 0.05 表示 5%",
}


def _is_blank(value: Any) -> bool:
    """空值判定。

    ``0`` / ``False`` **不算空**：阈值写成 ``0``（不允许任何拒绝）是合法需求，
    把它当空会让用户永远答不完这一题。
    """
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() == ""
    if isinstance(value, (list, tuple, set, dict)):
        return len(value) == 0
    return False


def _from_intent(intent: EtlIntent, param: RequiredParam) -> Any:
    return getattr(intent, param, None)


def find_missing_params(
    intent: EtlIntent,
    collected: dict[str, Any] | None,
    *,
    target_table_hint: str | None = None,
) -> list[MissingParam]:
    """返回**固定顺序**的缺失项列表；全齐则返回空列表。

    ``target_table`` 的例外（be-09 §6.2）：目标档案里已存在同名表且用户没有指定新表时，
    取该表名。``target_table_hint`` 就是那个表名——它由节点从档案摘要里取，
    纯函数不自己去查（查库会让这个函数不再是纯函数）。

    ``quality_threshold`` 的补充规则：它在 ``intent`` 里的默认值**不算数**。
    阈值是治理参数（决定多少脏数据被允许进生产），必须由人拍板，
    所以它只看 ``collected``，不看模型抽取的结果。其余四项允许回落到 ``intent``。
    """
    collected = collected or {}
    missing: list[MissingParam] = []
    for param in REQUIRED_PARAM_ORDER:
        value = collected.get(param)
        if _is_blank(value) and param != "quality_threshold":
            value = _from_intent(intent, param)
        if _is_blank(value) and param == "target_table":
            value = target_table_hint
        if _is_blank(value):
            missing.append(
                MissingParam(
                    param=param,
                    prompt=_PROMPTS[param],
                    kind=_kind_for(param),
                    options=None,
                    hint=_HINTS[param],
                    meta=_meta_for(param),
                )
            )
    return missing


def _kind_for(param: RequiredParam) -> str:
    """问题类型（前端 ``ClarificationKind``）。

    ``primary_key`` 是 ``multi_choice``：复合主键是真实需求，
    单选用它会让「(id, dt) 联合去重」这种方案在建号时就表达不出来。
    """
    if param == "primary_key":
        return "multi_choice"
    if param == "quality_threshold":
        return "threshold"
    if param == "incremental_column":
        return "field_ref"
    return "text"


def _meta_for(param: RequiredParam) -> dict[str, Any] | None:
    """``threshold`` 型问题的取值范围（前端 ``meta?: {min,max,unit,ratio}``）。"""
    if param == "quality_threshold":
        return {"min": 0, "max": 1, "unit": "", "ratio": True}
    return None


async def param_check_node(state: dict[str, Any], config: Any = None) -> dict[str, Any]:
    """图节点：算出「缺什么」与「问什么」。

    **问卷在这里产出、而不是在 ``clarify``**：``interrupt()`` 之前的写入不会被持久化
    （节点在恢复时整体重跑），所以必须由一个**不中断**的节点把问卷写进检查点，
    ``clarify`` 只负责中断与合并答案。这条顺序是整个澄清往返能工作的前提。

    节点本身不需要 ``session``（纯计算），因此不取 runtime——
    少一个依赖就少一处装配错误。
    """
    from workflow.interrupts import question_for

    raw_intent = state.get("intent") or {}
    # 宽容解析：state 可能是**上一个版本的代码**写下的（滚动升级期间的恢复）。
    # extra="forbid" 下这会抛错，所以先剔除未知键再校验——
    # 一个旧字段不该让用户的会话变成 500。
    known = set(EtlIntent.model_fields)
    intent = EtlIntent.model_validate({k: v for k, v in raw_intent.items() if k in known})
    hint = state.get("target_table_hint")
    missing = find_missing_params(
        intent,
        state.get("collected_params"),
        target_table_hint=hint if isinstance(hint, str) else None,
    )
    questions = [
        question_for(
            key=item.param,
            question=item.prompt,
            kind=item.kind,
            options=item.options,
            required=True,
            hint=item.hint,
            meta=item.meta,
        )
        for item in missing
    ]
    return {
        "missing_params": [item.param for item in missing],
        "pending_questions": questions,
    }
