"""自动修复的补丁形态（be-11 §6.6 规则 3）。

**补丁是自动修复唯一的变更形态。** 模型不能自由重写整份 Plan，只能产出受限的
JSON Pointer 操作：

- 每轮改动可被精确 Diff（「它到底改了什么」有答案）；
- 夹带与 finding 无关的改动无处藏身（想改字段 A 就得写一条打到 A 的 op）；
- 策略类阻断不可能被「顺手绕开」——绕开需要一条语法上写不出来的 op。

``apply_plan_patch`` 是**纯函数**：Plan 是 ``frozen=True`` 的，改不动，所以它从
``model_dump`` 出的普通 dict 上打补丁，再 ``model_validate`` 回一份**新** Plan。
入参 Plan 一律不被触碰。

## 为什么「下手轻」而不是实现完整 RFC 6902

只支持 ``add`` / ``replace`` / ``remove`` 三种 op，且**要求路径在 Plan 里真实存在**
（``add`` 只放开「父容器不存在」这一种情况）。路径解析失败、下标越界、穿过标量——
一律抛 :class:`PatchError`，**绝不静默跳过**。理由：静默跳过会把「修复没生效」伪装成
「模型改了但门禁不认」，于是门禁反复报同一个问题、轮次被白白烧光，而日志里一片正常。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from engines.plan.schema import EtlPlanModel

#: 允许的 JSON Pointer 操作。故意不含 ``move`` / ``copy`` / ``test``：
#: 它们表达不了「修好这个 finding」之外的意图，却给了夹带改动的空间。
PatchOperation = Literal["add", "remove", "replace"]


class PatchError(ValueError):
    """补丁无法应用（非法 Pointer / 路径不存在 / 下标越界 / 穿过标量）。

    它是 ``engines`` 的内部异常，由 :func:`engines.autofix.loop.run_autofix_loop` 捕获后
    记为「本轮补丁无效」（be-11 §6.6 规则 3），不会冒到 API 层。
    """


class PatchOp(BaseModel):
    """一条补丁操作。``path`` 是 JSON Pointer，如 ``/field_mappings/3/target_type``。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    op: PatchOperation
    #: JSON Pointer（RFC 6901）。空串与不以 ``/`` 开头的都视为非法——
    #: 空串意味着「替换整份 Plan」，那已经不是补丁而是自由重写。
    path: str
    #: ``add`` / ``replace`` 用；``remove`` 忽略它。
    value: Any | None = None


class PlanPatch(BaseModel):
    """一组补丁操作，按顺序原子地应用（任一 op 失败则整份补丁无效）。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ops: list[PatchOp] = Field(default_factory=list)


class RepairProposal(BaseModel):
    """模型对一轮修复的产出（be-11 §6.6）。

    ``hocon_content`` 是**可选的 HOCON 全文覆写**：有些 finding 只在 HOCON 层可修
    （例如连接器键名），Plan 里没有对应字段，``PlanPatch`` 表达不了。``None`` 表示
    「HOCON 不变，沿用当前那份」——不是空 HOCON。

    ``rationale`` 只留痕、不作判据：模型说自己「修好了」不算修好，只有重新过门禁才算。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    patch: PlanPatch = Field(default_factory=PlanPatch)
    rationale: str = ""
    hocon_content: str | None = None


def apply_plan_patch(plan: EtlPlanModel, patch: PlanPatch) -> EtlPlanModel:
    """应用补丁，返回**新** Plan。原对象不变（Plan 本身也是 frozen）。

    走 ``model_dump(by_alias=True)`` 而不是 :func:`engines.plan.schema.dump_plan`：
    后者是 json 模式，``Decimal`` 会先变成 float/字符串再被读回，预算里的
    ``0.05`` 可能在往返中丢掉精度；打补丁要的是**无损往返**，故用 python 模式
    （``by_alias`` 与 ``exclude_none`` 与 ``dump_plan`` 保持一致，键名不会漂）。
    """
    data: dict[str, Any] = plan.model_dump(by_alias=True, exclude_none=False)
    for op in patch.ops:
        _apply_op(data, op)
    return EtlPlanModel.model_validate(data)


# ---------------------------------------------------------------- 内部


def _apply_op(root: dict[str, Any], op: PatchOp) -> None:
    tokens = _parse_pointer(op.path)
    parent = _resolve_parent(root, tokens, op.path)
    leaf = tokens[-1]

    if isinstance(parent, dict):
        if op.op == "add":
            # RFC 6902：对象上的 add 是「新增或覆盖」，两者都不违反「只改已有结构」
            parent[leaf] = op.value
            return
        if leaf not in parent:
            raise PatchError(f"路径不存在：{op.path}")
        if op.op == "replace":
            parent[leaf] = op.value
        else:
            del parent[leaf]
        return

    if not isinstance(parent, list):
        raise PatchError(f"路径的父级既不是对象也不是数组：{op.path}")

    index = _list_index(parent, leaf, op.path, allow_end=op.op == "add")
    if op.op == "add":
        parent.insert(index, op.value)
    elif op.op == "replace":
        parent[index] = op.value
    else:
        parent.pop(index)


def _parse_pointer(pointer: str) -> list[str]:
    if not pointer.startswith("/"):
        raise PatchError(f"JSON Pointer 必须以 '/' 开头：{pointer!r}")
    # RFC 6901：~1 → '/'，~0 → '~'，顺序不能反（先解 ~1 会把 ~01 解错）
    return [token.replace("~1", "/").replace("~0", "~") for token in pointer.split("/")[1:]]


def _resolve_parent(root: dict[str, Any], tokens: list[str], pointer: str) -> Any:
    current: Any = root
    for token in tokens[:-1]:
        if isinstance(current, dict):
            if token not in current:
                raise PatchError(f"路径不存在：{pointer}")
            current = current[token]
        elif isinstance(current, list):
            current = current[_list_index(current, token, pointer, allow_end=False)]
        else:
            raise PatchError(f"路径穿过标量，无法继续下钻：{pointer}")
    return current


def _list_index(items: list[Any], segment: str, pointer: str, *, allow_end: bool) -> int:
    if segment == "-":
        # RFC 6902 的追加语法，只在 add 下有意义
        if not allow_end:
            raise PatchError(f"'-' 只能用于 add：{pointer}")
        return len(items)
    if not segment.isdigit():
        raise PatchError(f"数组下标不是非负整数：{pointer}")
    index = int(segment)
    if index > len(items) or (index == len(items) and not allow_end):
        raise PatchError(f"数组下标越界：{pointer}")
    return index
