"""中断载荷的构造与恢复输入的解析（be-09 §3.1 / §5.3）。

一次澄清的往返有两个形状，两者服务的消费者不同，**都要**：

- **简版**（:func:`interrupt_payload`）：``{"reason", "questions": [{"param", "prompt"}]}``，
  给后端 ``PipelineTurnResponse.interrupt``（be-09 §3.1）用。
- **富版**（:func:`question_for`）：前端 ``ClarificationQuestion``
  （``key / question / kind / options / required / hint``），
  给 Studio 的澄清问卷（``WorkflowSnapshot.pending_questions``）用。

两者都由 ``param_check.MissingParam`` **单向派生**——各写一份必然会漂移成
「后端说缺 A，前端问的是 B」。

**恢复输入也要兼容两种形状**：be-09 §5.3 规定 ``{"content": ..., "params": {...}}``，
而前端 ``POST /pipelines/generations/{thread_id}/answers`` 发的是
``{"answers": {"incremental_column": "updated_at"}}``（值一律字符串）。
:func:`parse_resume_input` 把两者归一成 ``(params, content)``，节点只认归一后的结果。

**``interrupt()`` 之前不能写 state**：LangGraph 在恢复时会把整个节点重跑，
``interrupt()`` 之前的所有写入都随那次重跑丢弃。所以 ``clarify`` 节点除了
「调 ``interrupt`` 并把答案合并进 ``collected_params``」之外什么都不做——
问卷本身由前一个节点（``param_check``）产出并落检查点。这条坑写在这里，
因为它的症状是「澄清问题点进去是空的」，而原因在另一个文件里。
"""

from __future__ import annotations

from typing import Any, Final

__all__ = [
    "CLARIFICATION_KINDS",
    "INTERRUPT_REASON_MISSING_PARAMS",
    "merge_resume_params",
    "interrupt_payload",
    "parse_interrupt",
    "parse_resume_input",
    "question_for",
]

#: 唯一的中断原因。取值即对外契约（前端按 ``reason`` 分支渲染）。
INTERRUPT_REASON_MISSING_PARAMS: Final[str] = "missing_params"

#: 逐字来自前端 ``ClarificationKind``。
CLARIFICATION_KINDS: Final[tuple[str, ...]] = (
    "text",
    "single_choice",
    "multi_choice",
    "field_ref",
    "threshold",
)


def interrupt_payload(questions: list[dict[str, Any]]) -> dict[str, Any]:
    """``interrupt()`` 的载荷（**简版**，be-09 §3.1）。

    入参是富版 ``ClarificationQuestion`` 列表（即 ``state["pending_questions"]``），
    输出是后端契约的 ``{reason, questions:[{param, prompt}]}``。
    只取 ``param`` 与 ``prompt`` 两个键：恢复方（前端）真正要的是
    「问的是哪一项」和「问什么」，其余渲染细节在快照里已经有了。
    """
    return {
        "reason": INTERRUPT_REASON_MISSING_PARAMS,
        "questions": [
            {"param": str(q.get("key", "")), "prompt": str(q.get("question", ""))} for q in questions
        ],
    }


def parse_interrupt(value: Any) -> dict[str, Any] | None:
    """从 ``Interrupt.value`` / ``__interrupt__`` 里取出标准形状的载荷。

    ``None`` 与「形状不对」都返回 ``None``：这两个来源都是**外部输入**
    （检查点里可能存着上一个版本的代码写的载荷），在这里把形状差异
    收敛成「没有中断」比让调用方各自 ``.get()`` 更安全——后者会写出
    ``payload["questions"]`` 这种在旧载荷上直接 KeyError 的代码。
    """
    if not isinstance(value, dict):
        return None
    reason = value.get("reason")
    questions = value.get("questions")
    if not isinstance(reason, str) or not isinstance(questions, list):
        return None
    clean: list[dict[str, str]] = []
    for item in questions:
        if not isinstance(item, dict):
            continue
        param = item.get("param")
        prompt = item.get("prompt")
        if isinstance(param, str) and isinstance(prompt, str):
            clean.append({"param": param, "prompt": prompt})
    return {"reason": reason, "questions": clean}


def question_for(
    *,
    key: str,
    question: str,
    kind: str = "text",
    options: list[dict[str, str]] | None = None,
    required: bool = True,
    hint: str | None = None,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """构造前端 ``ClarificationQuestion``（快照里 ``pending_questions`` 的元素）。

    ``meta`` 只在非空时出现：前端的类型是 ``meta?: {...}``，
    塞一个 ``null`` 进去等于给它一个「有 meta 但是空」的中间态，
    而它下面的代码会去读 ``meta.min`` —— 那是 undefined 与 null 的经典分叉。
    """
    if kind not in CLARIFICATION_KINDS:
        raise ValueError(f"未知的澄清问题类型：{kind!r}（合法取值 {CLARIFICATION_KINDS}）")
    item: dict[str, Any] = {
        "key": key,
        "question": question,
        "kind": kind,
        "options": options,
        "required": required,
        "hint": hint,
    }
    if meta:
        item["meta"] = meta
    return item


def parse_resume_input(raw: Any) -> tuple[dict[str, Any], str]:
    """恢复输入 → ``(params, content)``。

    兼容三种形状：
    - ``{"params": {...}, "content": "..."}``（be-09 §5.3）
    - ``{"answers": {key: value}}``（前端 answers 接口）
    - ``None`` / 其它 → ``({}, "")``

    ``answers`` 的值一律转成字符串保留（前端 ``Record<string, string>``），
    不做「看起来像数字就转 int」的猜谜：``"0755"`` 猜成 ``755`` 会静默改掉
    一个区号、一个 ``primary_key`` 或是别的什么，而这类值会一路走到 HOCON 里。
    数值型参数的类型归一由 ``param_check`` / 渲染层按**参数名**决定，
    那里知道字段的语义，这里不知道。
    """
    if not isinstance(raw, dict):
        return {}, ""
    content = raw.get("content")
    params: dict[str, Any] = {}
    raw_params = raw.get("params")
    if isinstance(raw_params, dict):
        params.update(raw_params)
    raw_answers = raw.get("answers")
    if isinstance(raw_answers, dict):
        for key, value in raw_answers.items():
            params[str(key)] = value if isinstance(value, str) else str(value)
    # ``answers`` 里也可能直接挂着需求正文（「其它补充说明」）。它属于 content，
    # 不该混进参数表——否则会多出一个名字由前端文案决定的参数。
    if not isinstance(content, str):
        content = ""
    return params, content


def merge_resume_params(collected: dict[str, Any] | None, fresh: dict[str, Any]) -> dict[str, Any]:
    """合并已收集参数与本次回答。``None`` 值**不覆盖**已有值。

    「跳过这一题」在前端是提交一个 ``null``/空串；若让它覆盖，
    用户跳过一题等于把上一轮答对的值擦掉，而界面看起来只是「没回答」。
    显式清空参数走别的手段（重新描述需求），不靠「提交空值」这个副作用。
    """
    merged: dict[str, Any] = dict(collected or {})
    for key, value in fresh.items():
        if value is None:
            continue
        if isinstance(value, str) and value.strip() == "":
            continue
        merged[key] = value
    return merged
