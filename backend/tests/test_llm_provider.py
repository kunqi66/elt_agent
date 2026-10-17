"""LLM Provider 抽象层的回归用例（be-08 §9）。

最重要的一条是 :func:`test_fake_provider_returns_valid_etlplan`：
**没有 API key 也能端到端跑通**——真实提示词 → 确定性起草 → 通过 Plan 契约校验。
它一旦红了，整条生成链路在没有 key 的机器上就断了。
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import httpx
import pytest
from pydantic import BaseModel, SecretStr

from clients.llm.base import (
    ChatMessage,
    CompletionRequest,
    GenerationContext,
    LLMContextViolationError,
    LLMUnavailableError,
    StructuredOutputError,
)
from clients.llm.deepseek import DeepSeekProvider
from clients.llm.fake import FAKE_MODEL_NAME, FakeProvider, apply_autofix
from clients.llm.prompts import PROMPT_VERSIONS
from clients.llm.prompts.autofix import (
    PROMPT_VERSION_AUTOFIX,
    AutofixFinding,
    autofix_blocks,
    autofix_request,
)
from clients.llm.prompts.fences import extract, extract_all, strip_code_fences, wrap
from clients.llm.prompts.plan import PROMPT_VERSION_PLAN, plan_blocks, plan_request
from clients.llm.registry import SUPPORTED_PROVIDERS, build_provider, get_provider
from clients.llm.scrub import assert_outbound_safe, scrub_text
from clients.llm.token_budget import (
    FittedPrompt,
    PromptBlock,
    PromptTooLargeError,
    estimate_tokens,
    fit_prompt,
)
from engines.plan.draft import DraftColumn, DraftSourceTable, DraftTable, DraftTargetTable, draft_plan
from engines.plan.schema import EtlPlanModel, RuntimeBudget, dump_plan
from models.enums import SensitiveLevel


@pytest.fixture(autouse=True)
def _clear_provider_cache():
    """``get_provider`` 是 lru_cache 单例，用例之间必须清掉，否则第一个用例的形状会漏给后面的。"""
    get_provider.cache_clear()
    yield
    get_provider.cache_clear()


# ---------------------------------------------------------------- 围栏


def test_fence_roundtrip():
    payload = '{"tables":[{"name":"orders"}]}'
    assert extract(wrap("SOURCE_PROFILE", payload), "SOURCE_PROFILE") == payload


def test_fence_missing_returns_none():
    assert extract("没有任何围栏的文本", "SOURCE_PROFILE") is None
    assert extract(wrap("OTHER", "x"), "SOURCE_PROFILE") is None


def test_fence_survives_surrounding_text():
    text = f"前面的话\n{wrap('REQUIREMENT', '同步订单')}\n后面的话"
    assert extract(text, "REQUIREMENT") == "同步订单"


def test_fence_multiple_blocks():
    text = f"{wrap('T', 'a')}\n{wrap('T', 'b')}"
    assert extract_all(text, "T") == ["a", "b"]
    assert extract(text, "T") == "a"


def test_fence_tag_must_be_restricted():
    with pytest.raises(ValueError):
        wrap("bad tag!", "x")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('```json\n{"a":1}\n```', '{"a":1}'),
        ('```\n{"a":1}\n```', '{"a":1}'),
        ('{"a":1}', '{"a":1}'),
        ('好的，这是方案：{"a":1} 以上。', '{"a":1}'),
    ],
)
def test_strip_code_fences(raw: str, expected: str):
    assert strip_code_fences(raw) == expected


# ---------------------------------------------------------------- 出站边界


@pytest.mark.parametrize(
    "text",
    [
        "mysql://root:pass@10.0.0.5:3306/biz_demo",
        "password=hunter2",
        "token=abc123def456",
        "-----BEGIN RSA PRIVATE KEY-----",
        "hvs.CAESIabcdefghijklmnop",
        "sk-abcdefghijklmnopqrstuvwxyz",
    ],
)
def test_assert_outbound_safe_raises_for_forbidden_patterns(text: str):
    with pytest.raises(LLMContextViolationError) as excinfo:
        assert_outbound_safe(text)
    # 命中片段是秘密，不许回显
    assert text not in str(excinfo.value.details)


def test_assert_outbound_safe_accepts_normal_schema_text():
    text = '{"tables":[{"name":"orders","columns":[{"name":"amount","data_type":"decimal(10,2)"}]}]}'
    assert assert_outbound_safe(text) is None
    assert assert_outbound_safe([ChatMessage(role="user", content=text)]) is None


def test_scrub_text_blots_connection_string_and_password_kv():
    raw = "conn=mysql://root:pass@10.0.0.5:3306/biz_demo password=hunter2"
    out = scrub_text(raw)
    assert "root:pass@" not in out
    assert "hunter2" not in out
    # 等长覆盖：长度不变，便于一眼看出「这里本来有个东西」
    assert len(out) == len(raw)
    assert "*" in out


def test_scrub_text_blots_personal_information():
    raw = "13800138000 zhangsan@example.com 11010119900307721X"
    out = scrub_text(raw)
    assert "13800138000" not in out
    assert "zhangsan@example.com" not in out
    assert "11010119900307721X" not in out
    assert len(out) == len(raw)


def test_scrub_text_is_lossless_for_plain_schema_text():
    text = "table=orders column=amount type=decimal(10,2)"
    assert scrub_text(text) == text


# ---------------------------------------------------------------- 预算与裁剪


def test_estimate_tokens_basics():
    assert estimate_tokens("") == 0
    assert estimate_tokens("a" * 11) == 5  # ceil(11/2.2)
    assert estimate_tokens("中" * 100) > 0
    assert estimate_tokens("a" * 100) >= estimate_tokens("a" * 50)
    assert estimate_tokens("x") >= 0


def _fittable_blocks() -> list[PromptBlock]:
    return [
        PromptBlock(key="system", priority=0, text="S" * 20),
        PromptBlock(key="schema", priority=1, text="A" * 400),
        PromptBlock(key="stats", priority=2, text="B" * 300),
        PromptBlock(key="samples", priority=3, text="\n".join("C" * 20 for _ in range(10))),
        PromptBlock(key="comments", priority=4, text="D" * 300),
    ]


def test_fit_prompt_drops_by_priority_and_reports_dropped_keys():
    fitted = fit_prompt(_fittable_blocks(), budget=200)
    # 从最低优先级（4）开始挨刀，顺序即优先级顺序
    assert fitted.dropped_keys == ["comments", "samples", "stats", "schema"]
    assert "system" not in fitted.dropped_keys
    assert fitted.token_estimate <= 200 * 0.95
    assert {b.key for b in fitted.blocks} == {"system"}
    # 留痕必须完整：活下来的块 ＋ 丢掉的块 = 输入的块，一块都不许无声消失
    assert {b.key for b in fitted.blocks} | set(fitted.dropped_keys) == {
        "system",
        "schema",
        "stats",
        "samples",
        "comments",
    }
    # 整块丢弃的 key 不再算「削减过」——两个列表各说各的会让人读成两件事
    assert set(fitted.trimmed_keys) & set(fitted.dropped_keys) == set()


def test_fit_prompt_shrinks_json_profile_instead_of_dropping_it():
    """单行 JSON 的画像块被「结构化削减」，而不是整块消失。

    若这里退化成 drop，症状是「模型看不见任何列名」——一个纯预算问题
    会伪装成模型能力问题，而且没有任何一处日志说得出「表结构被撤走了」。
    """
    columns = [{"name": f"col_{i}", "data_type": "varchar(64)"} for i in range(60)]
    profile = wrap("SOURCE_PROFILE", json.dumps({"tables": [{"columns": columns}]}, separators=(",", ":")))
    blocks = [
        PromptBlock(key="system", priority=0, text="S"),
        PromptBlock(key="schema", priority=1, text=profile),
    ]

    fitted = fit_prompt(blocks, budget=400)
    assert "schema" in fitted.trimmed_keys
    assert "schema" not in fitted.dropped_keys

    block = next(b for b in fitted.blocks if b.key == "schema")
    payload = extract(block.text, "SOURCE_PROFILE")
    assert payload is not None  # 围栏还在
    surviving = json.loads(payload)["tables"][0]["columns"]  # 削减后仍是合法 JSON
    assert 0 < len(surviving) < len(columns)
    assert surviving == columns[: len(surviving)]  # 保留的是前一半，不是随机抽样


def test_fit_prompt_is_deterministic():
    assert (
        fit_prompt(_fittable_blocks(), budget=200).model_dump()
        == fit_prompt(_fittable_blocks(), budget=200).model_dump()
    )


def test_fit_prompt_leaves_room_when_under_budget():
    blocks = [
        PromptBlock(key="system", priority=0, text="短"),
        PromptBlock(key="comments", priority=4, text="短"),
    ]
    fitted = fit_prompt(blocks, budget=8000)
    assert fitted.dropped_keys == []
    assert fitted.trimmed_keys == []
    assert [b.key for b in fitted.blocks] == ["system", "comments"]


def test_fit_prompt_raises_when_priority_zero_alone_exceeds_budget():
    blocks = [PromptBlock(key="requirement", priority=0, text="X" * 5000)]
    with pytest.raises(PromptTooLargeError) as excinfo:
        fit_prompt(blocks, budget=100)
    assert excinfo.value.code == "VALIDATION_ERROR"
    assert excinfo.value.details["budget"] == 100


def test_fit_prompt_keeps_fences_intact_while_trimming():
    """裁剪不许把围栏切断：切断了下游的 ``extract`` 就取不到档案，
    于是一次「预算不够」会伪装成「模型看不懂提示词」。"""
    payload = "\n".join(f"列{i}: varchar(64)" for i in range(40))
    blocks = [
        PromptBlock(key="system", priority=0, text="S"),
        PromptBlock(key="schema", priority=1, text=wrap("SOURCE_PROFILE", payload)),
    ]
    fitted = fit_prompt(blocks, budget=120)
    schema_block = next(b for b in fitted.blocks if b.key == "schema")
    assert extract(schema_block.text, "SOURCE_PROFILE") is not None


def test_fitted_prompt_render_and_get():
    fitted = FittedPrompt(blocks=[PromptBlock(key="a", priority=0, text="1")], budget=10)
    assert fitted.render() == "1"
    assert fitted.get("a") == "1"
    assert fitted.get("missing") is None


# ---------------------------------------------------------------- 假 Provider：端到端


def _budget() -> RuntimeBudget:
    return RuntimeBudget(
        max_read_rows=10_000_000,
        max_write_bytes=1_073_741_824,
        max_duration_sec=3600,
        max_amplification_ratio=Decimal("1.5"),
        max_reject_rate=Decimal("0.05"),
        warn_threshold_pct=80,
    )


def _source() -> DraftSourceTable:
    return DraftSourceTable(
        data_source_id=1,
        schema="biz_demo",
        tables=[
            DraftTable(
                name="orders",
                row_count_approx=120000,
                columns=[
                    DraftColumn(name="order_id", data_type="bigint", nullable=False, is_primary_key=True),
                    DraftColumn(
                        name="user_phone",
                        data_type="varchar(20)",
                        sensitive_level=SensitiveLevel.CONFIDENTIAL,
                    ),
                    DraftColumn(name="amount", data_type="decimal(10,2)"),
                ],
            )
        ],
    )


def _target() -> DraftTargetTable:
    return DraftTargetTable(
        data_source_id=2,
        database="dw_demo",
        table="orders_wide",
        columns=[DraftColumn(name="amount", data_type="varchar(64)")],
    )


def _plan_blocks(requirement: str = "把 orders 表同步到 dw_demo.orders_wide"):
    return plan_blocks(
        requirement=requirement,
        source=_source(),
        target=_target(),
        data_source_types={1: "mysql", 2: "doris"},
        budget=_budget(),
        masked_samples={"orders": [{"order_id": "1", "user_phone": "phone:a3f19c04b27e"}]},
        field_comments={"orders.amount": "订单金额"},
    )


def _draft_plan() -> EtlPlanModel:
    return draft_plan(
        requirement="把 orders 表同步到 dw_demo.orders_wide",
        source=_source(),
        target=_target(),
        budget=_budget(),
        data_source_types={1: "mysql", 2: "doris"},
    )


def _plan_request(request_id: str = "req_test_1", budget: int = 8000) -> CompletionRequest:
    fitted = fit_prompt(_plan_blocks(), budget=budget)
    return plan_request(fitted, request_id=request_id)


async def test_fake_provider_returns_valid_etlplan():
    """**没有 key 也能端到端跑通**：提示词 → 起草 → 通过 Plan 契约。"""
    provider = FakeProvider()
    result = await provider.complete(_plan_request())

    plan = EtlPlanModel.model_validate_json(result.text)
    assert plan.outputs[0].table == "orders_wide"
    assert plan.inputs[0].table == "orders"
    mapping = next(m for m in plan.field_mappings if m.source_field == "user_phone")
    assert mapping.masking is not None and mapping.masking.method == "mask_middle"
    assert result.model == FAKE_MODEL_NAME
    assert result.usage.prompt_tokens > 0
    assert result.finish_reason == "stop"


async def test_fake_provider_complete_json_returns_model():
    plan = await FakeProvider().complete_json(_plan_request(), EtlPlanModel)
    assert isinstance(plan, EtlPlanModel)
    assert plan.budget == _budget()


async def test_fake_provider_is_deterministic():
    provider = FakeProvider()
    first = await provider.complete(_plan_request(request_id="req_a"))
    second = await provider.complete(_plan_request(request_id="req_b"))
    assert first.text == second.text


async def test_fake_provider_reads_profiles_from_fences_not_from_nowhere():
    """档案真的来自提示词（而不是被硬编码在假 Provider 里）。

    换一份源画像，输出必须跟着变；否则这个用例只是「它总能返回点什么」。
    """
    text = (await FakeProvider().complete(_plan_request())).text
    assert "orders_wide" in text

    blocks = plan_blocks(
        requirement="把 orders 表同步到 dw_demo.orders_wide",
        source=DraftSourceTable(
            data_source_id=1,
            schema="biz_demo",
            tables=[DraftTable(name="orders", columns=[DraftColumn(name="only_col", data_type="int")])],
        ),
        target=None,
        data_source_types={},
        budget=_budget(),
    )
    other = (
        await FakeProvider().complete(plan_request(fit_prompt(blocks, budget=8000), request_id="r"))
    ).text
    assert "only_col" in other
    assert "user_phone" not in other


async def test_fake_provider_rejects_missing_fences():
    req = CompletionRequest(
        messages=[ChatMessage(role="user", content="一句没有任何围栏的话")],
        prompt_version=PROMPT_VERSION_PLAN,
        request_id="req_bad",
    )
    with pytest.raises(StructuredOutputError):
        await FakeProvider().complete(req)


async def test_fake_provider_applies_outbound_gate():
    """假 Provider 不出网，但闸门照过——否则本地路径就成了「可以绕过闸门」的那条路。"""
    req = _plan_request_with_requirement("把 orders 同步到 mysql://root:pass@10.0.0.5/dw")
    with pytest.raises(LLMContextViolationError):
        await FakeProvider().complete(req)


def _plan_request_with_requirement(requirement: str) -> CompletionRequest:
    fitted = fit_prompt(_plan_blocks(requirement=requirement), budget=8000)
    return plan_request(fitted, request_id="req_secret")


async def test_fake_provider_diagnose_is_explicitly_unsupported():
    req = CompletionRequest(
        task="diagnose",
        messages=[ChatMessage(role="user", content="诊断一下")],
        prompt_version=PROMPT_VERSION_AUTOFIX,
        request_id="req_diag",
    )
    with pytest.raises(LLMUnavailableError):
        await FakeProvider().complete(req)


async def test_fake_provider_stream_yields_full_text():
    chunks = [chunk async for chunk in FakeProvider().stream(_plan_request())]
    assert len(chunks) == 1
    assert EtlPlanModel.model_validate_json(chunks[0])


# ---------------------------------------------------------------- 请求契约


def test_diagnose_with_tools_is_rejected_at_construction():
    with pytest.raises(Exception) as excinfo:  # AppError
        CompletionRequest(
            task="diagnose",
            messages=[ChatMessage(role="user", content="x")],
            tools=[{"name": "t", "description": "", "parameters": {}}],
            prompt_version="1.0",
            request_id="r",
        )
    assert getattr(excinfo.value, "code", None) == "VALIDATION_ERROR"


def test_generation_context_matches_frontend_contract():
    """字段名以前端 ``frontend/src/types/pipeline.ts`` 的 ``GenerationContext`` 为准。"""
    ctx = GenerationContext(
        model_name="deepseek-chat",
        model_version="v1",
        prompt_template_version="1.0",
        autofix_rounds=1,
    )
    dumped = ctx.model_dump(mode="json")
    for field in ("model_name", "model_version", "prompt_template_version", "autofix_rounds"):
        assert field in dumped


def test_prompt_version_registry_matches_template_constants():
    assert PROMPT_VERSIONS["plan"] == PROMPT_VERSION_PLAN
    assert PROMPT_VERSIONS["autofix"] == PROMPT_VERSION_AUTOFIX


def test_plan_blocks_carry_expected_priorities():
    by_key = {b.key: b for b in _plan_blocks()}
    assert by_key["system"].priority == 0
    assert by_key["requirement"].priority == 0
    assert by_key["source_profile"].priority == 1
    assert by_key["source_stats"].priority == 2
    assert by_key["masked_samples"].priority == 3
    assert by_key["field_comments"].priority == 4
    # 档案里只允许结构，不允许样值
    assert "phone:a3f19c04b27e" not in by_key["source_profile"].text


# ---------------------------------------------------------------- 确定性自动修复


def _broken(kind: str) -> EtlPlanModel:
    data = dump_plan(_draft_plan())
    if kind == "duplicate_id":
        data["dag"]["nodes"].append(dict(data["dag"]["nodes"][-1]))
    elif kind == "dangling_ref":
        data["dag"]["edges"].append(
            {"id": "e_ghost", "source": "n_missing", "target": "n_sink", "label": None}
        )
    elif kind == "dag_cycle":
        data["dag"]["edges"].append({"id": "e_back", "source": "n_sink", "target": "n_source", "label": None})
    elif kind == "transform_unused":
        data["transforms"].append(
            {
                "id": "t_ghost",
                "kind": "cast",
                "input_fields": ["amount"],
                "output_field": "ghost",
                "params": {},
            }
        )
    elif kind == "quality_rule_field_missing":
        data["quality_rules"].append(
            {
                "id": "q_ghost",
                "field": "ghost_field",
                "rule_type": "not_null",
                "params": {},
                "error_code": "E_NULL_PK",
                "on_violation": "reject",
                "enabled": True,
            }
        )
    return EtlPlanModel.model_validate(data)


@pytest.mark.parametrize(
    "code",
    ["duplicate_id", "dangling_ref", "dag_cycle", "transform_unused", "quality_rule_field_missing"],
)
def test_autofix_covers_known_codes(code: str):
    fixed, applied, unresolved = apply_autofix(
        _broken(code), [AutofixFinding(code=code, path="/", message="门禁发现的问题")]
    )
    assert applied == [code]
    assert unresolved == []

    if code == "duplicate_id":
        ids = [n.id for n in fixed.dag.nodes]
        assert len(ids) == len(set(ids))
    elif code == "dangling_ref":
        node_ids = {n.id for n in fixed.dag.nodes}
        assert all(e.source in node_ids and e.target in node_ids for e in fixed.dag.edges)
    elif code == "dag_cycle":
        assert "e_back" not in {e.id for e in fixed.dag.edges}
    elif code == "transform_unused":
        assert "ghost" not in {t.output_field for t in fixed.transforms}
    else:
        assert "q_ghost" not in {r.id for r in fixed.quality_rules}


def test_autofix_reports_unfixable_codes():
    _, applied, unresolved = apply_autofix(
        _broken("transform_unused"),
        [AutofixFinding(code="budget_nonpositive", path="/budget")],
    )
    assert applied == []
    assert unresolved == ["budget_nonpositive"]


def test_autofix_is_deterministic():
    first, _, _ = apply_autofix(_broken("dag_cycle"), [AutofixFinding(code="dag_cycle", path="/")])
    second, _, _ = apply_autofix(_broken("dag_cycle"), [AutofixFinding(code="dag_cycle", path="/")])
    assert dump_plan(first) == dump_plan(second)


async def test_fake_provider_autofix_round_trip():
    broken = _broken("transform_unused")
    findings = [AutofixFinding(code="transform_unused", severity="warn", path="/transforms/1")]
    blocks = autofix_blocks(
        requirement="同步 orders", plan=dump_plan(broken), findings=findings, budget=_budget()
    )
    request = autofix_request(fit_prompt(blocks, budget=8000), request_id="req_autofix")

    patched = await FakeProvider().complete_json(request, EtlPlanModel)
    assert "ghost" not in {t.output_field for t in patched.transforms}


# ---------------------------------------------------------------- registry


def test_build_provider_fake_is_a_first_class_name():
    provider = build_provider("fake")
    assert isinstance(provider, FakeProvider)
    assert "fake" in SUPPORTED_PROVIDERS
    assert get_provider is not None


def test_build_provider_rejects_unknown_name():
    with pytest.raises(Exception) as excinfo:  # core.errors.ValidationError
        build_provider("openai")
    assert getattr(excinfo.value, "code", None) == "VALIDATION_ERROR"


def test_build_provider_deepseek_without_key_fails_loudly(monkeypatch):
    """**绝不静默降级**：没有 key 时抛 LLM_UNAVAILABLE，而不是悄悄换成一个假 Provider。"""
    from clients.llm import registry

    monkeypatch.setattr(registry, "get_settings", _settings_factory(provider="deepseek", api_key=""))
    with pytest.raises(LLMUnavailableError) as excinfo:
        build_provider()
    assert excinfo.value.details["reason"] == "missing_api_key"
    # 报错信息里不许出现 key 的任何片段
    assert "api_key" not in str(excinfo.value.details.get("last_status", ""))


def test_build_provider_deepseek_with_key(monkeypatch):
    from clients.llm import registry

    monkeypatch.setattr(
        registry, "get_settings", _settings_factory(provider="deepseek", api_key="unit-test-key")
    )
    assert isinstance(build_provider(), DeepSeekProvider)


def test_build_provider_honours_explicit_name_over_settings(monkeypatch):
    from clients.llm import registry

    monkeypatch.setattr(registry, "get_settings", _settings_factory(provider="deepseek", api_key=""))
    assert isinstance(build_provider("fake"), FakeProvider)


def _settings_factory(*, provider: str, api_key: str):
    """假的设置对象：只带 ``build_provider`` 真正会读的字段，避免测试依赖整个 Settings。"""

    class _Settings:
        LLM_PROVIDER = provider
        LLM_BASE_URL = "https://api.deepseek.com"
        LLM_API_KEY = SecretStr(api_key)
        LLM_MODEL_GENERATE = "deepseek-chat"
        LLM_MODEL_DIAGNOSE = "deepseek-reasoner"
        LLM_TIMEOUT_SEC = 90
        LLM_MAX_RETRY = 2

    return _Settings


# ---------------------------------------------------------------- DeepSeek（MockTransport，无真实网络）


class _Payload(BaseModel):
    ok: bool


def _mock_client(handler: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://api.deepseek.com")


def _request() -> CompletionRequest:
    return CompletionRequest(
        messages=[ChatMessage(role="user", content="你好")],
        json_mode=True,
        prompt_version="1.0",
        request_id="req_ds",
    )


async def test_deepseek_requires_api_key():
    with pytest.raises(LLMUnavailableError):
        DeepSeekProvider(api_key="")


async def test_deepseek_401_is_not_retried_and_hides_body():
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(401, json={"error": {"message": "bad key sk-leaked-secret"}})

    provider = DeepSeekProvider(api_key="unit-test-key", client=_mock_client(handler))
    with pytest.raises(LLMUnavailableError) as excinfo:
        await provider.complete(_request())

    assert len(calls) == 1
    assert excinfo.value.details["last_status"] == 401
    # 响应体可能回显凭据，绝不进异常
    assert "sk-leaked-secret" not in repr(excinfo.value.details)
    # Authorization 头确实发出去了（但从不进日志——日志字段里没有任何 header）
    assert calls[0].headers["authorization"] == "Bearer unit-test-key"


async def test_deepseek_success_maps_usage_and_model():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": "deepseek-chat",
                "system_fingerprint": "fp_2026",
                "choices": [{"message": {"role": "assistant", "content": "done"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
            },
        )

    provider = DeepSeekProvider(api_key="unit-test-key", client=_mock_client(handler))
    result = await provider.complete(_request())
    assert result.text == "done"
    assert result.model == "deepseek-chat"
    assert result.model_version == "fp_2026"
    assert result.usage.total_tokens == 18
    assert result.cached is False


async def test_deepseek_retries_5xx_then_succeeds():
    """5xx 两次后成功 → attempts=3；退避被注入的 sleep 顶掉，用例不真等 3 秒。"""
    seen: list[int] = []
    slept: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(1)
        if len(seen) < 3:
            return httpx.Response(503, json={"error": "boom"})
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}], "usage": {}},
        )

    async def _sleep(wait: float) -> None:
        slept.append(wait)

    provider = DeepSeekProvider(
        api_key="unit-test-key",
        client=_mock_client(handler),
        max_retry=2,
        sleep=_sleep,
        jitter=lambda low, high: 1.0,
    )
    result = await provider.complete(_request())
    assert result.text == "{}"
    assert len(seen) == 3
    assert slept == [1.0, 2.0]


async def test_deepseek_retry_exhausted_raises_llm_unavailable():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "boom"})

    async def _sleep(wait: float) -> None:
        return None

    provider = DeepSeekProvider(
        api_key="unit-test-key", client=_mock_client(handler), max_retry=1, sleep=_sleep
    )
    with pytest.raises(LLMUnavailableError) as excinfo:
        await provider.complete(_request())
    assert excinfo.value.details["last_status"] == 503


async def test_deepseek_complete_json_repairs_without_echoing_model_output():
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        bodies.append(_json.loads(request.content))
        if len(bodies) == 1:
            return httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": '{"ok": "not-a-bool"}'}, "finish_reason": "stop"}],
                    "usage": {},
                },
            )
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": '```json\n{"ok": true}\n```'}, "finish_reason": "stop"}],
                "usage": {},
            },
        )

    provider = DeepSeekProvider(api_key="unit-test-key", client=_mock_client(handler))
    payload = await provider.complete_json(_request(), _Payload)
    assert payload.ok is True
    assert len(bodies) == 2

    repair = bodies[1]["messages"][-1]["content"]
    assert "ok" in repair  # 只说字段路径
    assert "not-a-bool" not in str(bodies[1])  # 不回显模型原文


async def test_deepseek_complete_json_raises_after_two_failures():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "不是 JSON"}}], "usage": {}})

    provider = DeepSeekProvider(api_key="unit-test-key", client=_mock_client(handler))
    with pytest.raises(StructuredOutputError) as excinfo:
        await provider.complete_json(_request(), _Payload)
    assert excinfo.value.details["schema"] == "_Payload"
