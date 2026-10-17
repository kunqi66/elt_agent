"""意图解析：自然语言需求 → 结构化意图（be-09 §5.1，调 LLM，task=``generate``）。

**只写 ``intent``，不写 ``collected_params``**——与 be-09 §5.1 的节点表不同，
理由在 ``param_check.find_missing_params`` 的两级回落上：模型**猜**的参数进
``intent``，**用户明确给出**的参数进 ``collected_params``。若把模型抽取结果也写进
``collected_params``，那么第二级回落到 ``intent`` 就永远不会被用到，
而且 ``param_check`` 会因为 ``collected`` 恒非空而一次都问不出来——
澄清环节静默消失，模型猜错的字段直接进 HOCON。这是本模块与设计文档唯一的实质偏离。

模型输出**一律过 Pydantic 校验**（``EtlIntent``）：模型返回的 JSON 少一个引号、
多一个中文标点都是常态，让它在入口处失败并落 ``PLAN_INVALID``，
比带着半个解析结果走到渲染层再炸要好定位得多。

## 用户消息必须包进 ``<<REQUIREMENT>>`` 围栏

be-08 的 ``FakeProvider``（``LLM_PROVIDER=fake``，**是正式的离线通路而不是测试替身**）
按**围栏内容**分派：它从提示词里 ``extract`` 出 ``<<REQUIREMENT>>`` /
``<<SOURCE_PROFILE>>`` / ``<<PLAN>>`` 等块再决定怎么应答。``<<SOURCE_PROFILE>>`` 那一支
是起草，``<<PLAN>>`` 那一支是修复，而「只有 ``<<REQUIREMENT>>``」那一支是**意图解析**。

所以本节点的用户消息是 ``fences.wrap(TAG_REQUIREMENT, ...)``，而不是一段散文。
不包围栏的后果不是「退化成真实模型」，而是无 key 环境下**第一步就 500**——
而本机（以及任何没有模型凭据的环境）恰恰只有这条假通路可走。

Prompt 版本号常量在**本文件**而不是 be-08 的注册表里：注册表由 be-08 拥有，
本模块是它的消费方之一。这里的取值刻意与 be-08 的 ``PROMPT_VERSION_PLAN`` 对齐，
它**仍然是一个占位**：be-08 的 ``PROMPT_VERSIONS`` 落地后应改为从它读取
（``prompt_version`` 会写进 ``generation_context``，是「同一需求为何生成不同方案」的追溯依据）。
"""

from __future__ import annotations

from typing import Any, Final

from core.errors import AppError, ErrorCode
from core.logging import get_logger
from workflow.nodes import llm_provider, runtime_of
from workflow.nodes.param_check import EtlIntent
from workflow.state import already_done, input_fingerprint

__all__ = ["INTENT_PROMPT_VERSION", "intent_parse_node"]

log = get_logger(__name__)

#: 与 be-08 ``PROMPT_VERSION_PLAN`` 对齐的**占位**版本号（见模块 docstring 末段）：
#: 它不是一个独立的、可以单独演进的版本号——be-08 的 ``PROMPT_VERSIONS`` 落地后改为从它读。
INTENT_PROMPT_VERSION: Final[str] = "1.0"

_NODE = "intent_parse"

_SYSTEM = (
    "你是数据集成平台的需求解析器。用户用一句自然语言描述一次数据同步需求，"
    "请把它解析成结构化字段。**不要臆造**源表结构、字段名或阈值："
    "没提到的字段就留空，平台会就缺失项再向用户提问。"
)


def requirement_of(state: dict[str, Any]) -> str:
    """从 state 取需求原文。

    ``requirement`` 优先（``services/pipeline/generation._initial_state`` 注入的键），
    ``messages`` 兜底（be-09 §5.2 的对话数组，多轮场景下最后一条即本轮需求）。
    两个都读是**必要的**：服务层的形状与设计文档不同，只认一个就会在某次
    调用方式变化时表现成「需求描述不能为空」，而用户明明填了。
    """
    text = str(state.get("requirement") or "").strip()
    if text:
        return text
    messages = state.get("messages") or []
    if messages:
        return str(messages[-1].get("content") or "").strip()
    return ""


def _build_prompt(state: dict[str, Any]) -> str:
    """拼用户消息。

    **只放需求原文与已确认的数据源 id**，不放档案列名与样本——
    档案内容的投喂由 be-08 §6.6 的禁投喂断言统一把关，本模块不自己拼装
    「哪些能发、哪些不能发」的判断（第二份判断迟早与第一份不一致）。
    """
    lines = [f"需求：{requirement_of(state)}"]
    src = state.get("source_data_source_id")
    tgt = state.get("target_data_source_id")
    if src is not None:
        lines.append(f"源数据源 id：{src}")
    if tgt is not None:
        lines.append(f"目标数据源 id：{tgt}")
    return "\n".join(lines)


async def intent_parse_node(state: dict[str, Any], config: Any = None) -> dict[str, Any]:
    """图节点：解析需求。

    幂等：``intent`` 已在且**需求文本指纹未变**时直接 no-op。
    指纹必须覆盖需求原文——恢复会把本节点重放，只看「intent 在不在」会让
    「用户改了需求再生成」复用上一次的解析结果，而界面显示的是新需求。
    """
    runtime = runtime_of(config)
    requirement = requirement_of(state)
    if not requirement:
        raise AppError("需求描述不能为空", code=ErrorCode.VALIDATION_ERROR, http_status=422)

    fingerprint = input_fingerprint(requirement=requirement)
    if already_done(state, _NODE, fingerprint, "intent"):
        return {}

    from clients.llm.base import ChatMessage, CompletionRequest
    from clients.llm.prompts import fences
    from clients.llm.prompts.plan import TAG_REQUIREMENT

    provider = llm_provider()
    provider_name = str(getattr(provider, "name", "unknown"))
    log.info(
        "intent_parse_started",
        request_id=runtime.request_id,
        tenant_id=runtime.tenant_id,
        project_id=runtime.project_id,
        pipeline_id=runtime.pipeline_id,
        requirement_chars=len(requirement),  # 只记长度，需求原文不落日志（be-09 §8）
        provider=provider_name,
    )
    request = CompletionRequest(
        task="generate",
        messages=[
            ChatMessage(role="system", content=_SYSTEM),
            # 用户侧必须是围栏块：be-08 的假 Provider 按围栏内容分派（见模块 docstring）。
            # 需求原文留在 user（不在 system）——它对平台是**不可信输入**，
            # 与「指令」在位置上分开，是 be-08 §6.3 的同一条要求。
            ChatMessage(role="user", content=fences.wrap(TAG_REQUIREMENT, _build_prompt(state))),
        ],
        json_mode=True,
        prompt_version=INTENT_PROMPT_VERSION,
        request_id=runtime.request_id,
    )
    intent = await provider.complete_json(request, EtlIntent)
    return {
        "intent": intent.dump(),
        "node_fingerprints": {_NODE: fingerprint},
    }
