"""生成 EtlPlan（be-09 §5.1，调 LLM）与「生成成功」的证据事件。

两件事在这里发生，都是 be-09 的份内事：

1. **生成**：用 be-08 §6.3 的 ``plan_blocks`` / ``plan_request`` 拼出请求，
   调 ``get_provider()`` 拿结构化 ``EtlPlanModel``，再交给 be-10 的
   ``validate_plan`` 做结构校验（be-10 §3.2 的纯函数契约）。
   ``validate_plan`` 的非空问题清单**不抛异常**——它是自动修复的输入；
   抛 ``PLAN_INVALID`` 就等于把「可修」变成了「终局」。
2. **证据**：写 ``PIPELINE_GENERATED``（be-09 §4 表格明确「生成成功写」）。
   payload 只放**引用与元数据**（模型名/版本、提示词版本、修复轮次、档案 id），
   不放需求原文、不放任何样本（账本永久保留，见 be-06 §6.2）。

**``draft_plan`` 与 ``plan_errors`` 同时写**：前者是候选制品，后者是它的体检报告。
只写制品会让门禁/修复回路拿不到结构问题；只写问题会让用户在中间面板看不到草稿
（前端 Studio 的中部面板渲染的就是 ``draft_plan``，be-09 §5.4）。

**不落库**：草稿只活在 Workflow State 里（be-09 §5.4）——``etl_plan`` 是制品表，
把每一轮自动修复的中间产物都插进去会产生大量永不使用的不可变行。

## 提示词为什么必须借 be-08 的路（而不是自己拼一条）

本节点曾经自己拼 ``CompletionRequest``。那有四个后果，每一个都只在真机上才暴露：

1. **``FakeProvider`` 直接失效**。be-08 的离线通路按**围栏内容**分派
   （``<<SOURCE_PROFILE>>`` / ``<<BUDGET>>`` / ``<<PLAN>>`` …），
   而围栏只由 ``plan_blocks`` 产出。于是「本机没有模型凭据」这个最需要能跑通的环境里，
   生成链第一步就断在 ``StructuredOutputError: 提示词里没有 <<SOURCE_PROFILE>>``。
2. **绕过裁剪**。``fit_prompt`` 按优先级砍块（样本先走、档案最后走），
   自己拼的提示词没有优先级可言，只能靠人肉保证不超预算。
3. **绕过 ``assert_outbound_safe``**（be-08 §6.6 的禁投喂断言）。
4. 与 be-08 §6.3 重复实现，模板一改就分叉。

## 缺注入就当场 422（不静默兜底）

``source_profile`` / ``budget`` / ``data_source_types`` 都由 orchestrator 注入
（见 ``workflow/state.py`` 的字段注释）。缺任一项**抛 422 并说清是编排层没注入**：
静默兜底（比如自己造一份预算、或跳过档案块）会让模型在缺少列信息的情况下**猜列名**，
而猜出来的列名一路走到 HOCON，直到门禁才报——排查方向会被带到模型质量上，
而真因是装配漏了一步。
"""

from __future__ import annotations

from typing import Any

from core.errors import AppError, ErrorCode
from core.logging import get_logger
from workflow.nodes import llm_provider, runtime_of
from workflow.nodes.intent_parse import requirement_of
from workflow.state import already_done, input_fingerprint

__all__ = ["generate_plan_node"]

log = get_logger(__name__)

_NODE = "generate_plan"

#: 缺注入时的统一文案前缀。前端与日志里都靠它一眼认出「这是装配问题，不是用户输入问题」。
_MISSING_CONTEXT = "编排层未注入"


def _draft_source(state: dict[str, Any]) -> Any:
    """``source_profile`` → ``DraftSourceTable``。缺失即 422。"""
    from engines.plan.draft import DraftSourceTable

    raw = state.get("source_profile")
    if not isinstance(raw, dict) or not raw:
        raise AppError(
            f"源表档案缺失，无法生成方案：{_MISSING_CONTEXT} source_profile",
            code=ErrorCode.VALIDATION_ERROR,
            http_status=422,
            details={"missing": "source_profile"},
        )
    return _validated(DraftSourceTable, raw, field="source_profile")


def _draft_target(state: dict[str, Any]) -> Any | None:
    """``target_profile`` → ``DraftTargetTable``；``None`` 是**正常值**（新建宽表）。

    目标表还不存在时没有任何东西可注入，这不是装配错误——``plan_blocks`` 与
    ``draft_plan`` 都把它当可选项，HOCON 侧会按源列形状建表。
    """
    from engines.plan.draft import DraftTargetTable

    raw = state.get("target_profile")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise AppError(
            f"目标表画像形状不对：应为对象或 null，实得 {type(raw).__name__}",
            code=ErrorCode.VALIDATION_ERROR,
            http_status=422,
            details={"field": "target_profile"},
        )
    if not raw:
        return None
    return _validated(DraftTargetTable, raw, field="target_profile")


def _runtime_budget(state: dict[str, Any]) -> Any:
    """``budget`` → ``RuntimeBudget``。缺失即 422（预算默认值是策略，本节点不许自己造）。"""
    from engines.plan.schema import RuntimeBudget

    raw = state.get("budget")
    if not isinstance(raw, dict) or not raw:
        raise AppError(
            f"运行预算缺失，无法生成方案：{_MISSING_CONTEXT} budget"
            "（应由调度层从 services.policy.budget_defaults() 取）",
            code=ErrorCode.VALIDATION_ERROR,
            http_status=422,
            details={"missing": "budget"},
        )
    return _validated(RuntimeBudget, raw, field="budget")


def _data_source_types(state: dict[str, Any]) -> dict[int, str]:
    """``dict[str, str]``（state 的键只能是字符串）→ ``Mapping[int, str]``。

    非数字键**报错而不是丢弃**：``11`` 与 ``"11"`` 混在一个 dict 里说明注入方用的是
    另一套键空间（例如数据源的 ``name``），那种情况下渲染与提示词会各自拿到一半的类型信息，
    而「少了一个数据源类型」的表现是 HOCON 里的插件写成默认值——静默错，不报错。
    """
    raw = state.get("data_source_types")
    if not isinstance(raw, dict) or not raw:
        raise AppError(
            f"数据源类型表缺失，无法生成方案：{_MISSING_CONTEXT} data_source_types",
            code=ErrorCode.VALIDATION_ERROR,
            http_status=422,
            details={"missing": "data_source_types"},
        )
    types: dict[int, str] = {}
    for key, value in raw.items():
        text = str(key).strip()
        if not text.isdigit():
            raise AppError(
                f"数据源类型表的键必须是数据源 id：{_MISSING_CONTEXT} 的键 {text!r} 不是数字",
                code=ErrorCode.VALIDATION_ERROR,
                http_status=422,
                details={"field": "data_source_types", "key": text[:64]},
            )
        types[int(text)] = str(value)
    return types


def _validated(schema: Any, raw: dict[str, Any], *, field: str) -> Any:
    """按 Pydantic 校验注入的画像；失败译成 422 并**只报错在哪**（不带值）。"""
    from pydantic import ValidationError

    try:
        return schema.model_validate(raw)
    except ValidationError as exc:
        paths = [".".join(str(p) for p in err.get("loc", ())) for err in exc.errors()]
        raise AppError(
            f"{field} 的形状不符合 {getattr(schema, '__name__', field)}：{_MISSING_CONTEXT} 的内容有问题",
            code=ErrorCode.VALIDATION_ERROR,
            http_status=422,
            details={"field": field, "error_paths": paths[:20]},
        ) from exc


def _prompt_token_budget() -> int:
    """``settings.LLM_PROMPT_TOKEN_BUDGET``（be-08 §6.3 的裁剪预算）。"""
    from core.config import settings

    return int(getattr(settings, "LLM_PROMPT_TOKEN_BUDGET", 8000))


def _build_request(
    *,
    requirement: str,
    source: Any,
    target: Any | None,
    budget: Any,
    types: dict[int, str],
    request_id: str,
) -> Any:
    """按 be-08 §6.3 的三步拼请求：出块 → 裁剪 → 组装。

    入参是**已校验的模型**（而不是再传一遍 state）：校验与拼装各读一次 state 意味着
    两处可以读到不同的值（校验通过的那份可能不是你真正发出去的那份），
    而「发出去的提示词」与「被校验的输入」必须是同一批对象。

    ``masked_samples`` / ``field_comments`` 传 ``None``：state 里没有这两个来源，
    而「为了填满参数现造一份」正是最不该做的事——样本即使已脱敏也只在 ``plan_blocks``
    的最低优先级上，为它去读一次库（或更糟：把需求原文当样本）得不偿失。
    """
    from clients.llm.prompts.plan import plan_blocks, plan_request
    from clients.llm.token_budget import fit_prompt

    blocks = plan_blocks(
        requirement=requirement,
        source=source,
        target=target,
        data_source_types=types,
        budget=budget,
        masked_samples=None,
        field_comments=None,
    )
    fitted = fit_prompt(blocks, budget=_prompt_token_budget())
    # ``plan_request`` 自带 json_mode=True / temperature=0.0 / prompt_version=PROMPT_VERSION_PLAN，
    # 本节点不再拼 CompletionRequest —— 拼一层的后果是这两处参数各有一份自己的默认值。
    return plan_request(fitted, request_id=request_id)


async def generate_plan_node(state: dict[str, Any], config: Any = None) -> dict[str, Any]:
    """图节点：需求 → ``EtlPlanModel``。"""
    runtime = runtime_of(config)

    # 输入先归一成模型，**再**算指纹。顺序反过来（先看有没有草稿、再校验输入）
    # 会让「上一轮留下的 draft_plan」变成一条绕过注入校验的后门：
    # 缺 ``source_profile`` 的那次调用会直接 no-op 返回，于是「编排层没注入」这件事
    # 在第一次生成时被拦下、在恢复时却悄悄放行。
    params = state.get("collected_params") or {}
    source = _draft_source(state)
    target = _draft_target(state)
    budget = _runtime_budget(state)
    types = _data_source_types(state)
    requirement = requirement_of(state)
    if not requirement.strip():
        raise AppError(
            "需求描述不能为空，无法生成方案",
            code=ErrorCode.VALIDATION_ERROR,
            http_status=422,
            details={"field": "requirement"},
        )

    # 幂等：草稿已在、且**喂给模型的那几样东西**都没变 → no-op。
    #
    # 指纹必须覆盖「实际进提示词的内容」，而不是它们的**间接指代**：``profile_sha256``
    # 是元数据档案的快照哈希，而真正进提示词的是 orchestrator 注入的列画像。
    # 两者可以不一致（用户答题期间源表被重新探查过 → 画像换了、但本节点读到的
    # ``profile_sha256`` 还是入图时那份），那时指纹会说「没变」而提示词其实变了，
    # 于是「同样的输入为什么生成了不同方案」变成一道没有答案的题。
    # 所以这里指纹取**校验后的模型 dump**（列名、类型、可空、主键都在里面），
    # 保留 ``profile_sha256`` 是为了覆盖画像里没有的那些来源（例如目标端档案）。
    fingerprint = input_fingerprint(
        params=params,
        profile_sha256=state.get("profile_sha256") or {},
        source_profile=source.model_dump(mode="json", by_alias=True),
        target_profile=target.model_dump(mode="json", by_alias=True) if target is not None else {},
        budget=budget.model_dump(mode="json"),
    )
    if already_done(state, _NODE, fingerprint, "draft_plan"):
        log.info(
            "generate_plan_skipped",
            request_id=runtime.request_id,
            pipeline_id=runtime.pipeline_id,
            reason="already_generated",
        )
        return {}

    # 惰性导入：be-08（clients/llm 入口）与 be-10（Plan 校验器）都由并行模块提供，
    # 模块级 import 会让本包在它们落地前完全不可用（本机还要用它跑纯函数单测）。
    from clients.llm.prompts.plan import PROMPT_VERSION_PLAN
    from engines.plan.schema import EtlPlanModel, dump_plan
    from engines.plan.validate import validate_plan

    request = _build_request(
        requirement=requirement,
        source=source,
        target=target,
        budget=budget,
        types=types,
        request_id=runtime.request_id,
    )
    provider = llm_provider()
    plan = await provider.complete_json(request, EtlPlanModel)

    # 结构问题**不抛错**：它是自动修复的输入（be-10 §3.2 的异常契约）。
    issues = validate_plan(plan)
    plan_errors = [issue.model_dump(mode="json") for issue in issues]
    generation_context = _generation_context(state, provider)

    # 传本轮刚算出的 ``generation_context`` 而不是回头读 ``state``：本节点还没返回，
    # state 里的那一份是**上一轮**的（首轮为空），照它落账会写出一个空的 model 字段。
    await _record_generated(runtime, state, generation_context, plan_errors)

    log.info(
        "plan_generated",
        request_id=runtime.request_id,
        tenant_id=runtime.tenant_id,
        project_id=runtime.project_id,
        pipeline_id=runtime.pipeline_id,
        prompt_version=PROMPT_VERSION_PLAN,
        issue_count=len(plan_errors),
        field_mapping_count=len(plan.field_mappings),
        quality_rule_count=len(plan.quality_rules),
    )
    return {
        "draft_plan": dump_plan(plan),
        "plan_errors": plan_errors,
        "generation_context": generation_context,
        "node_fingerprints": {_NODE: fingerprint},
    }


def _generation_context(state: dict[str, Any], provider: Any) -> dict[str, Any]:
    """生成溯源信息（字段名以前端 ``GenerationContext`` 为准）。

    ``model_version`` 取厂商返回的实际版本串——「同一需求为什么生成出不同方案」
    只能靠它追溯。取不到时回落到配置的模型名（``model_version`` 是非空字段，
    空串会让 ``GenerationContext`` 构造失败，而那会把一次成功的生成变成 500）。
    ``prompt_template_version`` 一律取 be-08 的 ``PROMPT_VERSION_PLAN``：
    模板与本字段必须是同一个来源，两处各留一个版本号就是「模板改了但版本没变」的第一现场。

    **构造用 be-08 的模型，不手拼 dict**：这份 dict 会被原样写进
    ``pipeline_version.generation_context``，而读回来时又要过 API 的 DTO——
    手拼的键一旦多一个（早先这里多拼了 ``plan_issue_count``），
    症状是「生成成功、答题 422：Extra inputs are not permitted」，
    报错指向用户提交的答案，真因却在这一行。审计计数照旧进账本
    （``_record_generated`` 的 payload），不必挤进这个模型。
    """
    from clients.llm.base import GenerationContext
    from clients.llm.prompts.plan import PROMPT_VERSION_PLAN
    from core.config import settings

    model_name = str(getattr(provider, "model", "") or settings.LLM_MODEL_GENERATE)
    model_version = str(getattr(provider, "model_version", "") or model_name)
    context = GenerationContext(
        model_name=model_name,
        model_version=model_version,
        prompt_template_version=PROMPT_VERSION_PLAN,
        # 本轮的门禁修复轮次在自动修复回路里回填；此处是「生成那一刻」的计数
        autofix_rounds=int(state.get("autofix_rounds") or 0),
        provider=str(getattr(provider, "name", "") or ""),
        prompt_versions={"plan": PROMPT_VERSION_PLAN},
    )
    return context.model_dump(mode="json")


async def _record_generated(
    runtime: Any,
    state: dict[str, Any],
    generation_context: dict[str, Any],
    plan_errors: list[dict[str, Any]],
) -> None:
    """落 ``PIPELINE_GENERATED`` 证据事件（**只记引用与元数据**）。

    ``actor_id`` 取 ``runtime.user_id``（谁触发了这次生成），``subject_id``（**事件主体**，
    即资源）取 pipeline_id——审计要能回答「谁在什么时候让哪条 pipeline 生成了什么」。
    两个 id 名字像而语义不同：``actor_id`` 是人，``subject_id`` 是被操作的东西。
    """
    from clients.llm.prompts.plan import PROMPT_VERSION_PLAN
    from harness.ledger import EvidenceLedger, LedgerEvent
    from models.enums import EvidenceEventType

    event = LedgerEvent(
        tenant_id=runtime.tenant_id,
        project_id=runtime.project_id,
        actor_id=runtime.user_id,
        event_type=EvidenceEventType.PIPELINE_GENERATED,
        subject_type="pipeline",
        subject_id=str(runtime.pipeline_id),
        # 草稿还没有制品指纹（指纹在冻结时才由 be-12 算出），留空而不是填 profile 指纹：
        # artifact_hash 一旦写上就不是「空」的语义了，会给审计一个「已冻结」的错觉。
        artifact_hash="",
        payload={
            "model": str(generation_context.get("model_name") or ""),
            "prompt_versions": {"plan": PROMPT_VERSION_PLAN},
            "autofix_rounds": int(state.get("autofix_rounds") or 0),
            "profile_ids": dict(state.get("profile_ids") or {}),
            "plan_issue_count": len(plan_errors),
        },
    )
    try:
        await EvidenceLedger().append(runtime.session, event)
    except AppError:
        raise
    except Exception as exc:
        # 账本写失败**必须让本次生成失败**：证据缺失比生成失败严重得多
        # （生成可以重来，漏掉的那条事件永远补不回来）。
        log.error(
            "pipeline_generated_evidence_failed",
            request_id=runtime.request_id,
            pipeline_id=runtime.pipeline_id,
            error_type=type(exc).__name__,
        )
        raise AppError(
            "生成成功但证据事件写入失败，本次生成已放弃",
            code=ErrorCode.INTERNAL_ERROR,
            http_status=500,
        ) from exc
