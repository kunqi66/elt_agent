"""五类确定性门禁检查的单测（be-11 §6、§9）。

全部是**纯函数单测**：不连 PG、不连 Redis、不调 LLM，只喂 ``GateContext`` 看 findings。
两条最要紧的断言散在各处、但意图一致：

- **确定性**：同一 ``ctx`` 跑两次，finding 的 id 列表必须逐字相同
  （id 由 ``check_id.code.locator_key`` 派生，带时间/随机数就会破坏自动修复的去抖）。
- **文案不外泄**：明文口令的值**不许**出现在任何 ``message`` / ``suggestion`` 里。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from engines.gate.checks import (
    check_mapping,
    check_policy,
    check_resource,
    check_schema,
    check_syntax,
)
from engines.gate.context import GateContext, ProfileColumn, TableProfile
from engines.gate.types import (
    DagNodeLocator,
    HoconLocator,
    PlanLocator,
    SourceTableLocator,
    compose_conclusion,
)
from engines.hocon.types import HoconParseError, HoconParseResult
from engines.plan.schema import (
    EtlPlanModel,
    EtlTransform,
    FieldMapping,
    FieldMasking,
    PlanInput,
    PlanOutput,
    QualityRule,
    RuntimeBudget,
)
from engines.policy.models import (
    BudgetRequest,
    PolicyDecision,
    ResourceRef,
    ResourceScope,
    RuleHit,
)
from models.enums import (
    ApprovalSlot,
    DataSourceType,
    Env,
    ErrorCodeLabel,
    RiskLevel,
    SensitiveLevel,
)

_NOW = datetime(2026, 10, 16, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------- 构造夹具


def _budget(**over: object) -> RuntimeBudget:
    base: dict[str, object] = {
        "max_read_rows": 1_000_000,
        "max_write_bytes": 10_000_000,
        "max_duration_sec": 1800,
        "max_amplification_ratio": Decimal("1.0"),
        "max_reject_rate": Decimal("0.05"),
        "warn_threshold_pct": 80,
    }
    base.update(over)
    return RuntimeBudget(**base)  # type: ignore[arg-type]


def _mapping(
    source: str = "id",
    target: str = "order_id",
    *,
    source_type: str = "int",
    target_type: str = "bigint",
    transform: str = "none",
    masking: FieldMasking | None = None,
    quality_rule_ids: list[str] | None = None,
) -> FieldMapping:
    return FieldMapping(
        source_field=source,
        source_type=source_type,
        target_field=target,
        target_type=target_type,
        transform=transform,  # type: ignore[arg-type]
        masking=masking,
        quality_rule_ids=list(quality_rule_ids or []),
    )


def _plan(
    *,
    mappings: list[FieldMapping],
    inputs: list[PlanInput] | None = None,
    outputs: list[PlanOutput] | None = None,
    transforms: list[EtlTransform] | None = None,
    quality_rules: list[QualityRule] | None = None,
    budget: RuntimeBudget | None = None,
) -> EtlPlanModel:
    return EtlPlanModel(
        inputs=inputs
        if inputs is not None
        else [PlanInput(id="src", data_source_id=1, schema="sales", table="orders")],
        outputs=outputs
        if outputs is not None
        else [PlanOutput(id="sink", data_source_id=2, database="sales_wide", table="dim_orders")],
        field_mappings=list(mappings),
        transforms=list(transforms or []),
        quality_rules=list(quality_rules or []),
        budget=budget or _budget(),
    )


def _col(
    name: str,
    data_type: str,
    *,
    nullable: bool = True,
    level: SensitiveLevel = SensitiveLevel.PUBLIC,
) -> ProfileColumn:
    return ProfileColumn(name=name, data_type=data_type, nullable=nullable, sensitive_level=level)


def _source_profile(
    columns: list[ProfileColumn],
    *,
    table: str = "orders",
    schema: str = "sales",
    data_source_id: int = 1,
    expires_at: datetime | None = None,
) -> TableProfile:
    return TableProfile(
        data_source_id=data_source_id,
        data_source_type="mysql",
        schema=schema,
        table=table,
        columns=tuple(columns),
        expires_at=expires_at,
    )


def _target_profile(
    columns: list[ProfileColumn],
    *,
    table: str = "dim_orders",
    schema: str = "sales_wide",
    data_source_id: int = 2,
) -> TableProfile:
    return TableProfile(
        data_source_id=data_source_id,
        data_source_type="doris",
        schema=schema,
        table=table,
        columns=tuple(columns),
    )


def _default_sources() -> dict[tuple[int, str, str], TableProfile]:
    return {(1, "sales", "orders"): _source_profile([_col("id", "int")])}


def _ctx(
    plan: EtlPlanModel,
    *,
    hocon: str = "",
    parse_result: HoconParseResult | None = None,
    source_tables: dict[tuple[int, str, str], TableProfile] | None = None,
    target_table: TableProfile | None = None,
    policy_decision: PolicyDecision | None = None,
    policy_scope: ResourceScope | None = None,
    data_source_types: dict[int, str] | None = None,
) -> GateContext:
    return GateContext(
        plan=plan,
        hocon_content=hocon,
        parse_result=parse_result or HoconParseResult(ok=True, config={}),
        source_tables=source_tables if source_tables is not None else _default_sources(),
        target_table=target_table,
        policy_decision=policy_decision,
        policy_scope=policy_scope,
        data_source_types=data_source_types or {},
    )


def _decision(
    *,
    level: RiskLevel = RiskLevel.P2,
    blocked: bool = False,
    hits: tuple[RuleHit, ...] = (),
    required_slots: tuple[ApprovalSlot, ...] = (),
    budget: BudgetRequest | None = None,
    evaluated_at: datetime = _NOW,
) -> PolicyDecision:
    return PolicyDecision(
        level=level,
        blocked=blocked,
        required_slots=required_slots,
        hardening_required=False,
        budget=budget
        or BudgetRequest(
            max_read_rows=1_000_000,
            max_write_bytes=10_000_000,
            max_duration_sec=1800,
            max_amplification_ratio=Decimal("1.0"),
            max_reject_rate=Decimal("0.05"),
        ),
        hits=hits,
        reasons=(),
        policy_version=1,
        evaluated_at=evaluated_at,
    )


# ---------------------------------------------------------------- syntax


def test_syntax_解析失败产出可定位的_block() -> None:
    plan = _plan(mappings=[_mapping()])
    parse_result = HoconParseResult(
        ok=False,
        errors=(HoconParseError(message="expect '}'", line=7, column=3),),
    )
    findings = check_syntax(_ctx(plan, hocon="source {\n", parse_result=parse_result))

    assert len(findings) == 1
    finding = findings[0]
    assert finding.code == "syntax"
    assert finding.severity == "block"
    assert isinstance(finding.locator, HoconLocator)
    assert (finding.locator.line, finding.locator.column) == (7, 3)


def test_syntax_明文口令被拦下且文案不回显口令() -> None:
    secret = "Sup3rSecret-Pa55w0rd"
    hocon = f'JDBC {{\n  url = "jdbc:mysql://db:3306/sales"\n  password = "{secret}"\n}}\n'
    findings = check_syntax(_ctx(_plan(mappings=[_mapping()]), hocon=hocon))

    plaintext = [f for f in findings if f.code == "plaintext_secret"]
    assert plaintext, "明文口令必须被 syntax 检查拦下"
    assert all(f.severity == "block" for f in plaintext)
    assert any(isinstance(f.locator, HoconLocator) and f.locator.line == 3 for f in plaintext)

    for finding in findings:
        assert secret not in finding.message
        assert secret not in (finding.suggestion or "")


def test_syntax_合法令牌不产出_finding() -> None:
    hocon = (
        "JDBC {\n"
        '  url = "{{secret:1:jdbc_url}}"\n'
        '  user = "{{secret:1:username}}"\n'
        '  password = "{{secret:1:password}}"\n'
        "}\n"
    )
    assert check_syntax(_ctx(_plan(mappings=[_mapping()]), hocon=hocon)) == []


def test_syntax_令牌字段不在白名单时报_bad_placeholder() -> None:
    hocon = 'password = "{{secret:1:apikey}}"\n'
    findings = check_syntax(_ctx(_plan(mappings=[_mapping()]), hocon=hocon))

    assert any(f.code == "bad_placeholder" and f.severity == "block" for f in findings)


# ---------------------------------------------------------------- schema


def test_schema_源字段不存在被_block() -> None:
    plan = _plan(mappings=[_mapping(source="no_such_column")])
    findings = check_schema(_ctx(plan))

    absent = [f for f in findings if f.code == "source_field_absent"]
    assert absent and absent[0].severity == "block"
    assert all(isinstance(f.locator, PlanLocator) for f in findings)


def test_schema_类型兼容时不产出_finding() -> None:
    plan = _plan(mappings=[_mapping("id", "order_id", source_type="int", target_type="bigint")])
    ctx = _ctx(
        plan,
        source_tables={(1, "sales", "orders"): _source_profile([_col("id", "int")])},
        target_table=_target_profile([_col("order_id", "bigint")]),
    )
    assert check_schema(ctx) == []


def test_schema_不兼容类型被_block() -> None:
    plan = _plan(mappings=[_mapping("id", "created_at", source_type="int", target_type="date")])
    ctx = _ctx(
        plan,
        source_tables={(1, "sales", "orders"): _source_profile([_col("id", "int")])},
        target_table=_target_profile([_col("created_at", "date")]),
    )
    findings = check_schema(ctx)

    incompatible = [f for f in findings if f.code == "type_incompatible"]
    assert incompatible and incompatible[0].severity == "block"


def test_schema_需显式_cast_时报_warn_加了_cast_就不报() -> None:
    sources = {(1, "sales", "orders"): _source_profile([_col("id", "int")])}
    target = _target_profile([_col("name_out", "varchar")])

    without = check_schema(
        _ctx(
            _plan(mappings=[_mapping("id", "name_out", source_type="int", target_type="varchar")]),
            source_tables=sources,
            target_table=target,
        )
    )
    assert any(f.code == "type_needs_cast" and f.severity == "warn" for f in without)

    with_cast = check_schema(
        _ctx(
            _plan(
                mappings=[
                    _mapping(
                        "id",
                        "name_out",
                        source_type="int",
                        target_type="varchar",
                        transform="cast",
                    )
                ]
            ),
            source_tables=sources,
            target_table=target,
        )
    )
    assert with_cast == []


def test_schema_目标表还不存在时不报目标字段缺失() -> None:
    plan = _plan(mappings=[_mapping("id", "brand_new_col")])
    ctx = _ctx(plan, target_table=None)

    assert not [f for f in check_schema(ctx) if f.code == "target_field_absent"]


def test_schema_声明类型与档案不一致时报_warn() -> None:
    plan = _plan(mappings=[_mapping("id", "order_id", source_type="varchar", target_type="bigint")])
    ctx = _ctx(plan)

    mismatch = [f for f in check_schema(ctx) if f.code == "source_type_mismatch"]
    assert mismatch and mismatch[0].severity == "warn"


def test_schema_档案过期只报_warn_且定位到源表() -> None:
    plan = _plan(mappings=[_mapping("id", "order_id")])
    ctx = _ctx(
        plan,
        source_tables={
            (1, "sales", "orders"): _source_profile([_col("id", "int")], expires_at=_NOW - timedelta(days=1))
        },
        policy_decision=_decision(),
    )
    findings = check_schema(ctx)

    expired = [f for f in findings if f.code == "profile_expired"]
    assert len(expired) == 1
    assert expired[0].severity == "warn"
    assert isinstance(expired[0].locator, SourceTableLocator)


# ---------------------------------------------------------------- mapping


def test_mapping_两个映射写同一目标字段被_block() -> None:
    plan = _plan(
        mappings=[
            _mapping("id", "order_id"),
            _mapping("name", "order_id", source_type="varchar", target_type="varchar"),
        ]
    )
    findings = check_mapping(_ctx(plan))

    duplicates = [f for f in findings if f.code == "duplicate_target_field"]
    assert duplicates and duplicates[0].severity == "block"
    assert all(isinstance(f.locator, PlanLocator) for f in duplicates)


def test_mapping_敏感字段没有脱敏被_block() -> None:
    plan = _plan(mappings=[_mapping("id_card", "id_card_out", source_type="varchar", target_type="varchar")])
    ctx = _ctx(
        plan,
        source_tables={
            (1, "sales", "orders"): _source_profile(
                [_col("id_card", "varchar", level=SensitiveLevel.RESTRICTED)]
            )
        },
    )
    findings = check_mapping(ctx)

    sensitive = [f for f in findings if f.code == "mask_missing_for_sensitive"]
    assert len(sensitive) == 1
    assert sensitive[0].severity == "block"
    assert isinstance(sensitive[0].locator, PlanLocator)


def test_mapping_敏感字段有脱敏则放行() -> None:
    masked = FieldMasking(method="hash")
    for mapping in (
        _mapping(
            "id_card",
            "id_card_out",
            source_type="varchar",
            target_type="varchar",
            masking=masked,
        ),
        _mapping(
            "id_card",
            "id_card_out",
            source_type="varchar",
            target_type="varchar",
            transform="mask",
        ),
    ):
        ctx = _ctx(
            _plan(mappings=[mapping]),
            source_tables={
                (1, "sales", "orders"): _source_profile(
                    [_col("id_card", "varchar", level=SensitiveLevel.RESTRICTED)]
                )
            },
        )
        assert check_mapping(ctx) == []


def test_mapping_派生列引用不存在的字段被_block() -> None:
    plan = _plan(
        mappings=[_mapping()],
        transforms=[
            EtlTransform(
                id="t1", kind="concat", input_fields=["no_such"], output_field="full_name", params={}
            )
        ],
    )
    findings = check_mapping(_ctx(plan))

    missing = [f for f in findings if f.code == "transform_missing_input"]
    assert missing and missing[0].severity == "block"


def test_mapping_质量规则挂在不存在的字段上被_block() -> None:
    plan = _plan(
        mappings=[_mapping()],
        quality_rules=[
            QualityRule(id="q1", field="no_such", rule_type="not_null", error_code=ErrorCodeLabel.E_NULL_PK)
        ],
    )
    findings = check_mapping(_ctx(plan))

    missing = [f for f in findings if f.code == "quality_rule_field_missing"]
    assert missing and missing[0].severity == "block"


def test_mapping_质量规则_id_悬空被_block() -> None:
    plan = _plan(mappings=[_mapping(quality_rule_ids=["q_missing"])])
    findings = check_mapping(_ctx(plan))

    dangling = [f for f in findings if f.code == "dangling_quality_rule_id"]
    assert dangling and dangling[0].severity == "block"


def test_mapping_同一源字段映射到多个目标字段报_warn() -> None:
    plan = _plan(
        mappings=[
            _mapping("id", "order_id"),
            _mapping("id", "order_id_backup"),
        ]
    )
    findings = check_mapping(_ctx(plan))

    amplified = [f for f in findings if f.code == "source_field_mapped_multiple_times"]
    assert amplified and amplified[0].severity == "warn"


def test_mapping_干净的映射不产出_finding() -> None:
    plan = _plan(mappings=[_mapping()])

    assert check_mapping(_ctx(plan)) == []


# ---------------------------------------------------------------- resource


def test_resource_越界数据源被_block_且定位到源表() -> None:
    scope = ResourceScope(
        tenant_id=1,
        project_id=1,
        out_of_scope=(
            ResourceRef(
                data_source_id=1,
                data_source_type=DataSourceType.MYSQL,
                database="sales",
                schema="sales",
                table="orders",
                env=Env.DEV,
            ),
        ),
    )
    findings = check_resource(_ctx(_plan(mappings=[_mapping()]), policy_scope=scope))

    unauthorized = [f for f in findings if f.code == "scope_unauthorized"]
    assert unauthorized and unauthorized[0].severity == "block"
    assert isinstance(unauthorized[0].locator, SourceTableLocator)
    assert unauthorized[0].locator.data_source_id == 1


def test_resource_跨库被_block_且定位到_dag_node() -> None:
    plan = _plan(
        mappings=[_mapping()],
        inputs=[
            PlanInput(id="a", data_source_id=1, schema="sales", table="orders"),
            PlanInput(id="b", data_source_id=3, schema="crm", table="customers"),
        ],
    )
    findings = check_resource(_ctx(plan))

    cross = [f for f in findings if f.code == "cross_database"]
    assert cross and cross[0].severity == "block"
    assert isinstance(cross[0].locator, DagNodeLocator)
    assert cross[0].locator.node_id == "b"


def test_resource_同一数据源内跨_schema_被_block() -> None:
    plan = _plan(
        mappings=[_mapping()],
        inputs=[
            PlanInput(id="a", data_source_id=1, schema="sales", table="orders"),
            PlanInput(id="b", data_source_id=1, schema="archive", table="orders_2024"),
        ],
    )
    findings = check_resource(_ctx(plan))

    cross = [f for f in findings if f.code == "cross_schema"]
    assert cross and isinstance(cross[0].locator, DagNodeLocator)


def test_resource_源库与目标库不同不算跨库() -> None:
    plan = _plan(mappings=[_mapping()])

    assert check_resource(_ctx(plan)) == []


def test_resource_未登记密钥被_block_且定位到_hocon() -> None:
    scope = ResourceScope(tenant_id=1, project_id=1, unregistered_secret_refs=(1,))
    ctx = _ctx(
        _plan(mappings=[_mapping()]),
        hocon='url = "{{secret:1:jdbc_url}}"\n',
        policy_scope=scope,
    )
    findings = check_resource(ctx)

    unregistered = [f for f in findings if f.code == "unregistered_secret"]
    assert unregistered and unregistered[0].severity == "block"
    assert isinstance(unregistered[0].locator, HoconLocator)
    assert unregistered[0].locator.line == 1


def test_resource_数据源类型未注册被_block_注册后不报() -> None:
    plan = _plan(mappings=[_mapping()])

    missing = check_resource(_ctx(plan, data_source_types={1: "mysql"}))
    assert any(f.code == "data_source_not_registered" for f in missing)

    complete = check_resource(_ctx(plan, data_source_types={1: "mysql", 2: "doris"}))
    assert not [f for f in complete if f.code == "data_source_not_registered"]


# ---------------------------------------------------------------- policy


def test_policy_没有判定结论时默认拒绝() -> None:
    findings = check_policy(_ctx(_plan(mappings=[_mapping()])))

    assert len(findings) == 1
    assert findings[0].code == "missing_policy_decision"
    assert findings[0].severity == "block"
    assert isinstance(findings[0].locator, PlanLocator)


def test_policy_阻断判定变成_block_finding() -> None:
    hit = RuleHit(code="p0.sensitive_plaintext_egress", level=RiskLevel.P0, title="敏感字段明文出域")
    decision = _decision(level=RiskLevel.P0, blocked=True, hits=(hit,))

    findings = check_policy(_ctx(_plan(mappings=[_mapping()]), policy_decision=decision))

    blocks = [f for f in findings if f.severity == "block"]
    assert blocks
    assert any(f.code == "p0.sensitive_plaintext_egress" for f in blocks)


def test_policy_P1_命中同样阻断() -> None:
    hit = RuleHit(code="p1.cross_database", level=RiskLevel.P1, title="跨库作业")
    decision = _decision(level=RiskLevel.P1, blocked=False, hits=(hit,))

    findings = check_policy(_ctx(_plan(mappings=[_mapping()]), policy_decision=decision))

    assert any(f.code == "p1.cross_database" and f.severity == "block" for f in findings)


def test_policy_阻断但无命中明细时兜底_block() -> None:
    decision = _decision(level=RiskLevel.P0, blocked=True, hits=())

    findings = check_policy(_ctx(_plan(mappings=[_mapping()]), policy_decision=decision))

    assert any(f.code == "policy_blocked" and f.severity == "block" for f in findings)


def test_policy_P2_判定不产出_finding() -> None:
    hit = RuleHit(code="p2.default_write", level=RiskLevel.P2, title="写入类作业")
    decision = _decision(level=RiskLevel.P2, blocked=False, hits=(hit,))

    assert check_policy(_ctx(_plan(mappings=[_mapping()]), policy_decision=decision)) == []


def test_policy_需要审批槽时给出_info_提示且不影响结论() -> None:
    decision = _decision(
        level=RiskLevel.P2,
        required_slots=(ApprovalSlot.CHECKER_DATA, ApprovalSlot.CHECKER_SECURITY),
    )
    findings = check_policy(_ctx(_plan(mappings=[_mapping()]), policy_decision=decision))

    info = [f for f in findings if f.code == "required_approval_slots"]
    assert info and info[0].severity == "info"
    assert compose_conclusion(findings) == "pass"


def test_policy_预算超过_PDP_上限被_block() -> None:
    plan = _plan(mappings=[_mapping()], budget=_budget(max_read_rows=5_000_000))
    findings = check_policy(_ctx(plan, policy_decision=_decision()))

    exceeded = [f for f in findings if f.code == "budget_exceeds_policy"]
    assert exceeded and exceeded[0].severity == "block"
    assert isinstance(exceeded[0].locator, PlanLocator)


# ---------------------------------------------------------------- 横切


def test_同输入两次运行_finding_id_完全一致() -> None:
    """确定性：同一 ctx 跑两次，每条检查的 id 列表逐字相同（自动修复去抖依赖它）。"""
    scope = ResourceScope(
        tenant_id=1,
        project_id=1,
        unregistered_secret_refs=(1,),
        out_of_scope=(
            ResourceRef(
                data_source_id=1,
                data_source_type=DataSourceType.MYSQL,
                database="sales",
                schema="sales",
                table="orders",
                env=Env.DEV,
            ),
        ),
    )
    plan = _plan(
        mappings=[
            _mapping("id", "order_id"),
            _mapping("id", "order_id_dup"),
            _mapping("no_such_column", "extra"),
        ],
        transforms=[
            EtlTransform(id="t1", kind="concat", input_fields=["nope"], output_field="full", params={})
        ],
    )
    ctx = _ctx(
        plan,
        hocon='password = "plainTextValue"\n',
        policy_decision=_decision(level=RiskLevel.P0, blocked=True, hits=()),
        policy_scope=scope,
        data_source_types={1: "mysql", 2: "doris"},
    )

    for check in (check_policy, check_syntax, check_schema, check_mapping, check_resource):
        first = [f.id for f in check(ctx)]
        second = [f.id for f in check(ctx)]
        assert first == second, f"{check.__name__} 不确定"
        assert len(first) == len(set(first)), f"{check.__name__} 出现重复 id"


def test_finding_的_locator_类型与问题类别匹配() -> None:
    """字段问题用 plan、HOCON 行问题用 hocon、越界表用 source_table、DAG 问题用 dag_node。"""
    plan = _plan(
        mappings=[_mapping("no_such_column", "order_id")],
        inputs=[
            PlanInput(id="a", data_source_id=1, schema="sales", table="orders"),
            PlanInput(id="b", data_source_id=3, schema="crm", table="customers"),
        ],
    )
    schema_findings = check_schema(_ctx(plan))
    assert any(isinstance(f.locator, PlanLocator) for f in schema_findings)

    syntax_findings = check_syntax(_ctx(plan, hocon='password = "plainTextValue"\n'))
    assert any(isinstance(f.locator, HoconLocator) for f in syntax_findings)

    resource_findings = check_resource(_ctx(plan))
    assert any(isinstance(f.locator, DagNodeLocator) for f in resource_findings)

    scope = ResourceScope(
        tenant_id=1,
        project_id=1,
        out_of_scope=(
            ResourceRef(
                data_source_id=1,
                data_source_type=DataSourceType.MYSQL,
                database="sales",
                schema="sales",
                table="orders",
                env=Env.DEV,
            ),
        ),
    )
    scoped = check_resource(_ctx(_plan(mappings=[_mapping()]), policy_scope=scope))
    assert any(isinstance(f.locator, SourceTableLocator) for f in scoped)


def test_finding_超过上限时截断并追加一条_warn() -> None:
    plan = _plan(mappings=[_mapping(f"no_{index}") for index in range(60)])
    findings = check_schema(_ctx(plan))

    assert len(findings) == 51
    assert findings[-1].code == "findings_truncated"
    assert findings[-1].severity == "warn"
