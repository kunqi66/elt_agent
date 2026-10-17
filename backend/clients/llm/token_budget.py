"""prompt token 预算与裁剪（be-08 §6.3）。纯函数：同输入必得同输出。

为什么不用第三方 tokenizer：它带来一份模型相关的词表与几百 KB 依赖，
而这里的用途只是「别把请求发得太肥」——一个偏保守的启发式估算就够了，
真正的用量以厂商回传的 ``usage.prompt_tokens`` 为准（be-08 §10.1 登记了偏差验证方式）。

裁剪是**留痕**的：返回 ``dropped_keys`` / ``trimmed_keys``，让调用方把
「这一轮生成因为预算丢掉了哪些块」写进日志与诊断，而不是让人对着一个残缺的提示词
猜为什么模型没看见某个字段。
"""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from core.errors import AppError, ErrorCode

#: be-08 §6.3 的默认预算（``settings.LLM_PROMPT_TOKEN_BUDGET`` 的取值）。
DEFAULT_PROMPT_BUDGET = 8000

#: 启发式系数：``ceil(len(text) / 2.2)``（中文按 1 字 ≈ 0.45 token 估，偏保守）。
_CHARS_PER_TOKEN = 2.2

#: 裁剪停止线：留 5% 余量给输出契约与厂商侧计数差异（be-08 §6.3 第 3 步）。
_HEADROOM = 0.95

_BASE = ConfigDict(extra="forbid", frozen=True)

#: 每个优先级对应的「裁剪动作阶梯」，按序施加。``half`` = 非围栏行减半，``drop`` = 整块丢弃。
#: priority 0 **没有**阶梯——它的内容不可裁剪，超了只能报错。
_LADDERS: dict[int, tuple[str, ...]] = {
    0: (),
    1: ("half", "half", "drop"),
    2: ("half", "drop"),
    3: ("half", "half", "half", "drop"),
    4: ("drop",),
}


class PromptTooLargeError(AppError):
    """``priority 0`` 本身就超预算，或所有裁剪动作做完仍超（be-08 §7）。"""

    code = ErrorCode.VALIDATION_ERROR
    http_status = 422
    message = "提示词超出 token 预算，请缩短需求或减少绑定的表"


class PromptBlock(BaseModel):
    """一块提示词素材。``priority`` 取值 0~4，语义见 :data:`_LADDERS` 与 be-08 §6.3。"""

    model_config = _BASE

    key: str = Field(min_length=1, max_length=64)
    priority: int = Field(ge=0, le=4)
    text: str


class FittedPrompt(BaseModel):
    """裁剪结果。``blocks`` 是**存活且已裁剪**的块，顺序与输入一致。"""

    model_config = _BASE

    blocks: list[PromptBlock] = Field(default_factory=list)
    #: 被整块丢弃的 key（按丢弃顺序）——留痕用，调用方应写进日志
    dropped_keys: list[str] = Field(default_factory=list)
    #: 被部分削减（未丢弃）的 key
    trimmed_keys: list[str] = Field(default_factory=list)
    token_estimate: int = Field(default=0, ge=0)
    budget: int = Field(gt=0)

    def render(self, separator: str = "\n\n") -> str:
        """把存活的块拼成一整段文本。"""
        return separator.join(b.text for b in self.blocks)

    def get(self, key: str) -> str | None:
        for block in self.blocks:
            if block.key == key:
                return block.text
        return None


def estimate_tokens(text: str) -> int:
    """启发式估算：``ceil(len(text) / 2.2)``，空串为 0。

    刻意**不含** 5% 安全系数：be-08 §6.3 里那 5% 由 :func:`fit_prompt` 的停止线
    （``total <= budget * 0.95``）承担。两处都乘一次，等于把预算白白缩掉 10%，
    而「到底按哪个数算」会在单测与生产之间产生分歧。
    """
    if not text:
        return 0
    return math.ceil(len(text) / _CHARS_PER_TOKEN)


def _is_fence_line(line: str) -> bool:
    return line.startswith("<<") and (line.endswith(">>") or line.endswith(">>\r"))


def _dumps(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _shrink_json(payload: Any) -> tuple[Any, bool]:
    """把结构里**最大的那个列表**减半（保留前一半）。改动是原地进行的，返回是否改动了。

    挑最大的列表从大到小依次削，效果是「几千列的宽表画像会一轮轮缩到能塞进预算」，
    而不会因为找不到可削的地方直接整块消失。

    同长时按「先遇到者优先」定序，而 ``json.loads`` 保留文本顺序、
    我们的模板又是 ``sort_keys`` 出来的——所以整条路径是确定性的。
    """
    best: list[Any] = []
    stack: list[tuple[Any, Any, Any]] = [(payload, None, None)]
    while stack:
        node, parent, key = stack.pop()
        if isinstance(node, list):
            if len(node) > 1:
                size = len(_dumps(node))
                if not best or size > best[0]:
                    best[:] = [size, parent, key, node]
            stack.extend((item, node, index) for index, item in enumerate(node))
        elif isinstance(node, dict):
            stack.extend((value, node, name) for name, value in node.items())

    if not best:
        return payload, False
    _, parent, key, node = best
    kept = node[: max(1, math.ceil(len(node) / 2))]
    if parent is None:
        return kept, True
    parent[key] = kept
    return payload, True


def _half(text: str) -> str:
    """削减一块文本：围栏行永远原样保留。

    保住围栏是刻意的：``<<SOURCE_PROFILE>>…<<END>>`` 一旦被切断，
    下游的 ``fences.extract`` 就取不到档案，于是一次「预算不够」会伪装成
    「模型看不懂提示词」。

    正文是单行 JSON 时走**结构化削减**（把最大的列表减半再重新序列化）：
    单行块按行减半是减不动的，而减不动就意味着阶梯直接跳到 ``drop``——
    于是「画像太大」变成「画像整个消失」。对真实生成来说，后者比前者坏得多：
    一张 200 列的宽表在预算不够时，正确的处置是带着前 100 列去生成，
    而不是把表结构整块撤走、让模型凭空猜列名。

    其余情况按行减半。这条路径保不住 JSON 的完整性（半个对象解析不了），
    所以模板里的档案块一律压成单行——见 ``prompts/plan.py::_compact``。
    """
    lines = text.split("\n")
    fence_flags = [_is_fence_line(line) for line in lines]
    payload = [i for i, flag in enumerate(fence_flags) if not flag]

    if len(payload) == 1:
        raw = lines[payload[0]]
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = None
        if parsed is not None:
            shrunk, changed = _shrink_json(parsed)
            rendered = _dumps(shrunk) if changed else raw
            if len(rendered) < len(raw):
                lines[payload[0]] = rendered
                return "\n".join(lines)
        # 既不是 JSON、又只有一行：再减就是空，交给阶梯的下一步（通常是 drop）
        return text

    if len(payload) <= 1:
        return text
    keep = max(1, math.ceil(len(payload) / 2))
    keep_indices = set(payload[:keep])
    return "\n".join(line for index, line in enumerate(lines) if fence_flags[index] or index in keep_indices)


def _total(texts: Sequence[str | None]) -> int:
    return sum(estimate_tokens(t) for t in texts if t is not None)


def fit_prompt(blocks: Sequence[PromptBlock], *, budget: int = DEFAULT_PROMPT_BUDGET) -> FittedPrompt:
    """按优先级 0~4 装配与裁剪（be-08 §6.3）。

    算法：
    1. ``T0`` = priority 0 各块之和；``T0 > budget`` → 抛 ``PromptTooLargeError``
       （此时**不发网络请求**，这是「超预算」反例用例的断言点）。
    2. 从 priority 4 到 1，逐块按阶梯施加裁剪动作，每步重算总量。
    3. 总量 ``<= budget * 0.95`` 即停手。
    4. 走完全部动作仍超 → 抛 ``PromptTooLargeError``。

    已知简化：priority 1 的规范动作是「只保留用户已提及或已映射的字段」，
    而块的内容对纯函数不可见（它只有 key/priority/text），所以这里退化为
    「JSON 块按最大的列表减半、文本块按行减半」。要真正做到「只留已提及字段」，
    应由调用方在构造 ``PromptBlock`` 时就把未提及的字段摘掉——
    那比让裁剪器去猜字段语义更可靠。
    """
    if budget <= 0:
        raise PromptTooLargeError("提示词预算必须为正", details={"budget": budget})

    mandatory = [b for b in blocks if b.priority == 0]
    t0 = sum(estimate_tokens(b.text) for b in mandatory)
    if t0 > budget:
        raise PromptTooLargeError(
            "需求与输出契约本身已超出 token 预算，无法生成",
            details={"priority0_tokens": t0, "budget": budget},
        )

    # 用「只读的 blocks + 可变的 texts 快照」而不是一份 dict：
    # 块 key 重复时 dict 会静默丢块，而重复 key 恰恰是调用方的 bug，不该被吞掉
    texts: list[str | None] = [b.text for b in blocks]
    dropped: list[str] = []
    trimmed: list[str] = []
    target = budget * _HEADROOM

    for priority in (4, 3, 2, 1):
        if _total(texts) <= target:
            break
        for index, block in enumerate(blocks):
            if block.priority != priority or texts[index] is None:
                continue
            for action in _LADDERS[priority]:
                if texts[index] is None or _total(texts) <= target:
                    break
                if action == "drop":
                    texts[index] = None
                    dropped.append(block.key)
                    # 被丢弃的块不再算「削减过」：两个列表各说各的会让「这轮丢了什么」
                    # 读起来像两件事
                    if block.key in trimmed:
                        trimmed.remove(block.key)
                else:
                    shrunk = _half(texts[index] or "")
                    if shrunk != texts[index]:
                        texts[index] = shrunk
                        if block.key not in trimmed:
                            trimmed.append(block.key)

    final = _total(texts)
    if final > target:
        raise PromptTooLargeError(
            "提示词经全部裁剪后仍超出 token 预算",
            details={
                "token_estimate": final,
                "budget": budget,
                "droppable_tokens": sum(estimate_tokens(b.text) for b in blocks if b.priority > 0),
            },
        )

    kept = [
        PromptBlock(key=block.key, priority=block.priority, text=texts[index] or "")
        for index, block in enumerate(blocks)
        if texts[index] is not None
    ]
    return FittedPrompt(
        blocks=kept,
        dropped_keys=dropped,
        trimmed_keys=trimmed,
        token_estimate=final,
        budget=budget,
    )


__all__ = [
    "DEFAULT_PROMPT_BUDGET",
    "FittedPrompt",
    "PromptBlock",
    "PromptTooLargeError",
    "estimate_tokens",
    "fit_prompt",
]
