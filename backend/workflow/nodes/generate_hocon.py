"""渲染 SeaTunnel HOCON（be-09 §5.1，纯函数）。

不做任何渲染逻辑——be-10 的 ``engines/hocon/render.py`` 才是渲染器，
本节点只负责把 ``RenderContext`` 拼好、调用它、把结果与元信息写进 state。

**为什么渲染上下文由 state 提供**：``RenderContext`` 要 ``data_source_id -> 类型``
（选连接器模板），而 engines 层禁止访问数据库。这个事实既不在 Plan 里、本模块
也没有可用的 service，只能由 orchestrator 在入图时注入（``state["data_source_types"]``，
见 ``services/pipeline/generation._initial_state``）。缺类型时渲染器会**抛错**
而不是回落默认模板（``RenderContext.ds_type`` 的注释），这是对的：
回落到 MySQL 模板去写 Doris 会产生一份语法合法、语义错误的 HOCON。

**渲染失败不抛异常**：渲染器对「Plan 自洽但表达不出 HOCON」的情形会抛 ``ValueError``
（例如 post_transform 阶段的过滤器）。把它抛出去 = 一次 500，而门禁与自动修复
本来就是为了处理「这份方案有问题」而存在的——接住它、落成 ``hocon_errors``、
让门禁去报 finding，用户看到的是「方案第 N 行有问题」，而不是「服务器错误」。
``services/pipeline/repair._proposal`` 对同一个异常做了同样的处置，两处口径一致。

**sha256 在这里算、并且进 state**：它是冻结时 ``save_artifacts`` 需要的事实之一。
放到冻结时再算，等于把「渲染那一刻的文本」交给第二次计算——中间任何一次
state 变形都会让指纹对不上，而那种不一致看起来像「指纹算错了」。
"""

from __future__ import annotations

from typing import Any

from core.errors import AppError, ErrorCode
from core.logging import get_logger
from workflow.nodes import runtime_of
from workflow.state import already_done, input_fingerprint

__all__ = ["generate_hocon_node"]

log = get_logger(__name__)

_NODE = "generate_hocon"

#: 渲染失败时落进 ``hocon_errors`` 的占位行号。
#: 用 0 而不是 1：0 在 HOCON 里不可能是一行，前端据此能一眼区分
#: 「解析器报的第 1 行」与「压根没渲染出来」。
_NO_LINE = 0


def _render_context(state: dict[str, Any]) -> Any:
    """从 state 拼 ``RenderContext``。

    并行度从 ``generation_context`` 取（与 ``services/pipeline/artifacts.run_autofix``
    同一个来源）：它是「生成那一刻」的作业形态，而不是 Plan 里的字段；
    取不到就用 1——放大并行度是在消耗目标库资源，默认值只能取最保守的那个。
    """
    from engines.hocon.types import RenderContext

    mapping = state.get("data_source_types") or {}
    generation_context = state.get("generation_context") or {}
    parallelism = int(generation_context.get("parallelism") or 1)
    return RenderContext(
        data_source_types={int(k): str(v) for k, v in mapping.items()},
        parallelism=max(1, parallelism),
    )


async def generate_hocon_node(state: dict[str, Any], config: Any = None) -> dict[str, Any]:
    """图节点：``draft_plan`` → ``draft_hocon``。

    幂等：草稿 HOCON 已在、且 **Plan 与渲染上下文的指纹未变** → no-op。
    自动修复改了 Plan 就会让指纹变化，于是「修复后必须重新渲染」这条
    由幂等判据本身保证，不需要在修复节点里额外来一次。
    """
    runtime = runtime_of(config)
    plan_dict = state.get("draft_plan")
    if not isinstance(plan_dict, dict):
        raise AppError(
            "尚未生成 EtlPlan，无法渲染 HOCON",
            code=ErrorCode.PLAN_INVALID,
            http_status=422,
        )

    fingerprint = input_fingerprint(plan=plan_dict, data_source_types=state.get("data_source_types"))
    if already_done(state, _NODE, fingerprint, "draft_hocon"):
        log.info(
            "generate_hocon_skipped",
            request_id=runtime.request_id,
            pipeline_id=runtime.pipeline_id,
            reason="already_rendered",
        )
        return {}

    from core.ids import sha256_hex
    from engines.hocon.parser import parse_and_validate
    from engines.hocon.render import render_hocon
    from engines.plan.schema import EtlPlanModel
    from services.pipeline.gating import render_meta

    plan = EtlPlanModel.model_validate(plan_dict)
    try:
        rendered = render_hocon(plan, _render_context(state))
        content = rendered.content
        parsed = parse_and_validate(content)
        errors: list[dict[str, Any]] = (
            [] if parsed.ok else [err.model_dump(mode="json") for err in parsed.errors]
        )
        # 渲染段的形状由 ``services.pipeline.gating.render_meta`` 定义（落库路径
        # 用的是同一个函数）：这里随手拼一份，就成了第二个定义——
        # 早先正是如此，两个形状慢慢分叉，最后在「读门禁结论」时才炸。
        meta = render_meta(rendered)
    except ValueError as exc:
        # 渲染不出来：落一份**空 HOCON + 一条错误**，让门禁按缺制品处理。
        # 空串而不是 None —— ``gate`` 与 ``freeze`` 都以 ``isinstance(hocon, str)``
        # 判「制品在不在」，None 会让它们报「草稿不完整」，把一次渲染失败
        # 伪装成一次装配错误。
        log.warning(
            "hocon_render_failed",
            request_id=runtime.request_id,
            pipeline_id=runtime.pipeline_id,
            error=str(exc)[:200],
        )
        content = ""
        errors = [{"message": str(exc)[:512], "line": _NO_LINE, "column": _NO_LINE}]
        meta = {
            "sha256": sha256_hex(""),
            "parser_version": "",
            "renderer_version": "",
            "job_name": "",
            "token_count": 0,
        }

    log.info(
        "hocon_rendered",
        request_id=runtime.request_id,
        tenant_id=runtime.tenant_id,
        pipeline_id=runtime.pipeline_id,
        hocon_bytes=len(content.encode("utf-8")),
        error_count=len(errors),
    )
    return {
        "draft_hocon": content,
        "hocon_errors": errors,
        "hocon_meta": meta,
        "node_fingerprints": {_NODE: fingerprint},
    }
