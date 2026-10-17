"""be-17 ``engines/quality``：契约编译、谓词渲染、摘要参考实现、``passed`` 判定。

**这个文件要钉住的一句话**：质量结论的错法不是「报错」，而是「看起来一切正常」。
一份算错的报告会以完全合法的形态出现——``passed=true``、比率好看、前端一片绿，
然后影子表被 Swap 上去。所以判定与渲染的每一格边界都在这里逐条写死。

四组断言：

1. **谓词语义 ``True == 违规``**：同一份规则在两种方言上渲染出的表达式，
   以及边界（``==`` 算不算违规、NULL 归谁管、``warn`` 进不进路由）。
2. **注入面收敛**：字段名/枚举值/正则里的引号与分号在**编译期**就被拒，
   渲染出来的字面量里也不会出现逃逸出来的引号。
3. **摘要口径**：黄金向量（手算的 SHA256 前 56 位 + 8 位长度后缀）、
   布尔列显式拒绝、长度后缀大写对齐 MySQL ``HEX()``。
4. **``passed`` 判定**：等于预算算通过、零容忍码无条件拦截、
   「读到了行却一行都没分流」必须拦（这一条**只看数字会判通过**）。

本机没有 Doris / MySQL（CLAUDE.md §9），所以 SQL **不执行**：断言的是文本形状与语义。
真正的方言验证在 ECS 上做，这里是「同一份规则两边都拼得出来、而且拼对了」的那道闸。
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest

from engines.quality import (
    DIGEST_LEN,
    ERROR_COLUMNS,
    FIELD_SEP_CHARCODE,
    HARD_BLOCK_CODES,
    MAX_FIELD_LEN,
    MAX_RULE_ID,
    NULL_CANON,
    SUGGESTIONS,
    TAGGED_TABLE,
    CompiledRule,
    CompiledRules,
    ContractError,
    QualityContractSpec,
    QualityDiagnostics,
    QualityDiagnosticSample,
    QualityReportDTO,
    QualityRuleSpec,
    budget_used_ratio_of,
    build_diagnostic_items,
    build_report,
    canonical_field,
    canonicalize_row,
    compile_contract,
    contract_sha256,
    decide_passed,
    digest_content_length,
    evaluate_rule,
    format_rate,
    payload_digest,
    quote_literal,
    reject_rate_of,
    render_digest_expr,
    render_predicate,
    render_reason,
    render_route_selects,
    render_tagged_select,
    supported_typecast_types,
    target_columns,
)
from models.enums import DataSourceType

DORIS = DataSourceType.DORIS
MYSQL = DataSourceType.MYSQL
BOTH = (DORIS, MYSQL)

#: 用例里的**唯一基准时刻**（CLAUDE.md §9.1：涉及时间的断言两端用同一个基准）。
_T0 = datetime(2026, 10, 16, 12, 0, 0, tzinfo=UTC)

COLUMNS = ("order_id", "phone", "amount", "created_at")


# ---------------------------------------------------------------- 构造


def _spec(*rules: dict[str, Any], budget: str = "0.05") -> QualityContractSpec:
    return QualityContractSpec(
        rules=[QualityRuleSpec.model_validate(item) for item in rules],
        error_table_target={"suffix": "_elt_reject", "pipeline_id": 1},
        reject_rate_budget=Decimal(budget),
    )


def _rule(
    rule_id: str = "r1",
    field: str = "order_id",
    rule_type: str = "not_null",
    *,
    on_violation: str = "reject",
    error_code: str = "E_NULL_PK",
    **params: Any,
) -> dict[str, Any]:
    return {
        "id": rule_id,
        "field": field,
        "rule_type": rule_type,
        "params": params,
        "error_code": error_code,
        "on_violation": on_violation,
    }


def _range(**params: Any) -> dict[str, Any]:
    """区间规则挂在 ``amount`` 上（渲染出来的表达式里字段名必须是它）。"""
    return _rule("r1", "amount", "range", **params)


def _compile(*rules: dict[str, Any], budget: str = "0.05") -> CompiledRules:
    return compile_contract(_spec(*rules, budget=budget), valid_columns=COLUMNS, target_type=DORIS)


def _one(rule: dict[str, Any]) -> CompiledRule:
    return _compile(rule).rules[0]


# ================================================================ 谓词渲染


@pytest.mark.parametrize("target", BOTH)
def test_空值规则渲染成_IS_NULL(target: DataSourceType) -> None:
    assert render_predicate(_one(_rule()), target) == "order_id IS NULL"


@pytest.mark.parametrize("target", BOTH)
def test_空值规则带_allow_blank_时空串与全空白都算违规(target: DataSourceType) -> None:
    sql = render_predicate(_one(_rule(allow_blank=True)), target)
    assert sql == "order_id IS NULL OR TRIM(order_id) = ''"
    # 只写 ``= ''`` 会让 ``'   '`` 蒙混过关，所以 TRIM 必须出现
    assert "TRIM(order_id)" in sql


@pytest.mark.parametrize("target", BOTH)
def test_正则规则默认是_不匹配即违规(target: DataSourceType) -> None:
    """默认 ``expect=match``：值**应当**匹配模式，于是「不匹配」才是违规。"""
    sql = render_predicate(_one(_rule(rule_type="regex", pattern=r"^1[3-9]\d{9}$")), target)
    # 空值归 not_null 管：没有这道守卫，NULL 在 CASE 里会走 ELSE 分支被静默判成合规
    assert sql == r"order_id IS NOT NULL AND NOT REGEXP(order_id, '^1[3-9]\\d{9}$')"


@pytest.mark.parametrize("target", BOTH)
def test_正则规则_expect_not_match_时反过来(target: DataSourceType) -> None:
    """``expect=not_match``：值**不该**匹配模式，于是「匹配上了」才是违规。"""
    sql = render_predicate(_one(_rule(rule_type="regex", pattern="^OK", expect="not_match")), target)
    assert sql == "order_id IS NOT NULL AND REGEXP(order_id, '^OK')"
    assert "NOT REGEXP" not in sql


def test_正则里的反斜杠被转义_而不是被编译期拒绝() -> None:
    """反斜杠在 SQL 字面量里是转义符，``quote_literal`` 负责把它翻倍。

    编译期只拒引号与分号（那两个会逃出字面量）；反斜杠是**纵深防御**那一层的事，
    所以这里断言的是「渲染出来是双反斜杠」，而不是「编译报错」。
    """
    sql = render_predicate(_one(_rule(rule_type="regex", pattern=r"a\b")), MYSQL)
    assert r"REGEXP(order_id, 'a\\b')" in sql
    assert quote_literal(r"a\b") == r"'a\\b'"


@pytest.mark.parametrize("target", BOTH)
def test_区间规则的四个边界(target: DataSourceType) -> None:
    both = _compile(_range(min=1, max=10, inclusive_min=True, inclusive_max=True))
    assert render_predicate(both.rules[0], target) == "amount IS NOT NULL AND (amount < 1 OR amount > 10)"

    # 默认：下界闭、上界开（「小于 max」是绝大多数业务的含义）
    default = _compile(_range(min=1, max=10))
    assert (
        render_predicate(default.rules[0], target) == "amount IS NOT NULL AND (amount < 1 OR amount >= 10)"
    )

    # 只给上界：违规 = 超出上界。默认上界**开**（``>=``），所以 10 本身不算违规
    lower = _compile(_range(max=10))
    assert render_predicate(lower.rules[0], target) == "amount IS NOT NULL AND (amount >= 10)"

    upper = _compile(_range(min=1))
    assert render_predicate(upper.rules[0], target) == "amount IS NOT NULL AND (amount < 1)"


@pytest.mark.parametrize("target", BOTH)
def test_枚举规则用_IN_判定(target: DataSourceType) -> None:
    sql = render_predicate(_one(_rule(rule_type="enum", values=["paid", "refunded"])), target)
    assert sql == "order_id IS NOT NULL AND order_id NOT IN ('paid', 'refunded')"


def test_枚举取值排序去重_让同一份契约产出同一条字面量() -> None:
    a = _compile(_rule(rule_type="enum", values=["b", "a", "b"]))
    b = _compile(_rule(rule_type="enum", values=["a", "b"]))
    assert a.rules[0].params["values"] == ["a", "b"]
    assert a.contract_sha256 == b.contract_sha256


@pytest.mark.parametrize("target", BOTH)
def test_类型规则在两种方言上的判法不同(target: DataSourceType) -> None:
    """Doris 的 ``CAST`` 失败返回 NULL（天然就是「转不动」的信号），
    MySQL 的 ``CAST`` **对坏值直接报错**——一个坏值会让整个作业崩掉，
    而我们要的是把这一行分流出去。所以 MySQL 走前置 REGEXP 形状校验，不调 CAST。"""
    rule = _one(_rule("r1", "created_at", "type_cast", target_type="date"))
    sql = render_predicate(rule, target)
    if target is DORIS:
        assert sql == "created_at IS NOT NULL AND (CAST(created_at AS date))"
    else:
        assert sql == (
            "created_at IS NOT NULL AND (NOT REGEXP(created_at, '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'))"
        )


def test_类型规则非_strict_时容忍首尾空白() -> None:
    """``CAST`` 本身容忍空白，用严格形状去卡它会把合法值判成违规。"""
    rule = _one(_rule("r1", "created_at", "type_cast", target_type="date", strict=False))
    assert r"\s*" in render_predicate(rule, MYSQL)


@pytest.mark.parametrize("target", BOTH)
def test_五种规则类型都能渲染出非空谓词与原因(target: DataSourceType) -> None:
    cases = [
        _rule("a", "order_id", "not_null", error_code="E_NULL_PK"),
        _rule("b", "phone", "regex", pattern="^1", error_code="E_REGEX_MISMATCH"),
        _rule("c", "amount", "range", min=0, error_code="E_RANGE_VIOLATION"),
        _rule("d", "amount", "enum", values=[1, 2], error_code="E_ENUM_INVALID"),
        _rule("e", "created_at", "type_cast", target_type="datetime", error_code="E_TYPE_CAST"),
    ]
    compiled = _compile(*cases)
    assert {r.rule_type for r in compiled.rules} == set(SUGGESTIONS)
    for rule in compiled.rules:
        assert render_predicate(rule, target).strip()
        assert render_reason(rule, target).strip()
    # reason 描述「值哪里不对」：NULL 守卫与最外层括号必须被剥掉，且不含任何字段值
    assert render_reason(compiled.rules[2], target) == "amount < 0"


def test_原因模板超宽时按列宽截断() -> None:
    """``error_record.reason`` 是 varchar(255)：超宽的症状是数据面写得进、控制面读不出。

    这里用「值多得离谱的 enum」而不是超长正则：正则在编译期就被长度闸拦下了，
    而 ``reason`` 的截断是**渲染期**的事，两者不是同一条防线。
    """
    values = [f"status_{index:03d}_" + "x" * 20 for index in range(12)]
    rule = _one(_rule("r1", "order_id", "enum", values=values, error_code="E_ENUM_INVALID"))
    reason = render_reason(rule, MYSQL)
    assert len(reason) <= 255
    assert reason.endswith("…")


def test_正则超长在编译期拒绝_而不是靠原因截断() -> None:
    with pytest.raises(ContractError):
        _compile(_rule("r1", "phone", "regex", pattern="a" * 200))


def test_warn_规则不进路由分支() -> None:
    """``warn`` 命中**不改变路由**：行照常进影子表，所以它不能出现在 CASE 里。

    这条一旦写错，``warn`` 就变成了事实上的 ``reject``——而报告上的计数一切正常，
    只有影子表里少行。
    """
    compiled = _compile(
        _rule("keep", "order_id", "not_null", error_code="E_NULL_PK"),
        _rule("advise", "amount", "range", on_violation="warn", min=0, error_code="E_RANGE_VIOLATION"),
    )
    assert [r.rule_id for r in compiled.warn_rules] == ["advise"]
    assert [r.rule_id for r in compiled.reject_rules] == ["keep"]

    # 路由 CASE 只由 reject 规则生成：warn 的错误码/字段名一次都不该出现
    tagged = render_tagged_select("SELECT 1", compiled, run_id=1, target_type=DORIS)
    assert "'E_NULL_PK'" in tagged
    assert "'E_RANGE_VIOLATION'" not in tagged
    assert "'keep'" in tagged
    assert "'advise'" not in tagged


def test_只有_warn_规则时路由是常量字符串_valid() -> None:
    """没有 reject 分支时 ``CASE WHEN`` 会没有 WHEN——语法错。

    而且这里必须回字符串 ``'valid'`` 而不是布尔 ``FALSE``：MySQL 会把
    ``FALSE = 'valid'`` 隐式转成 ``0 = 0`` 从而**判真**，影子表会一行都没有。
    """
    compiled = _compile(_rule("advise", "amount", "range", on_violation="warn", min=0))
    tagged = render_tagged_select("SELECT 1", compiled, run_id=7, target_type=MYSQL)
    assert "'valid' AS _quality_route" in tagged
    assert "NULL AS _error_code" in tagged
    assert "CASE" not in tagged  # 一条 reject 规则都没有 → 不该有任何 CASE

    valid_sql, rejected_sql = render_route_selects(compiled, valid_columns=COLUMNS, target_type=MYSQL)
    assert "WHERE _quality_route = 'valid'" in valid_sql
    assert "WHERE _quality_route = 'reject'" in rejected_sql


# ================================================================ 编译期拒绝


@pytest.mark.parametrize(
    "field",
    ["order; DROP TABLE x", "order_id'", "order id", "a.b", "1order", "", "x" * (MAX_FIELD_LEN + 1)],
)
def test_字段名不过白名单就在编译期拒绝(field: str) -> None:
    with pytest.raises(ContractError):
        _compile(_rule(field=field))


@pytest.mark.parametrize("value", ["paid'; DROP TABLE x --", "it's", "a,b;c", "x" * 129])
def test_枚举值里的引号分号与超长都在编译期拒绝(value: str) -> None:
    with pytest.raises(ContractError):
        _compile(_rule(rule_type="enum", values=[value]))


@pytest.mark.parametrize("pattern", ["a'b", "a;b", "(", "["])
def test_正则里的引号分号与不可编译的模式都在编译期拒绝(pattern: str) -> None:
    with pytest.raises(ContractError):
        _compile(_rule("r1", "phone", "regex", pattern=pattern))


def test_区间上下界倒置在编译期拒绝() -> None:
    with pytest.raises(ContractError):
        _compile(_range(min=10, max=1))


def test_区间没有任何边界拒绝() -> None:
    """两侧都无界的规则永远不命中，只会让前端显示一条规则、数据面什么都不做。"""
    with pytest.raises(ContractError):
        _compile(_range())


def test_布尔参数不是布尔时拒绝() -> None:
    with pytest.raises(ContractError):
        _compile(_range(min=0, inclusive_max="maybe"))


def test_未知规则类型与未知违规处置都拒绝() -> None:
    with pytest.raises(ContractError):
        _compile(_rule("r1", rule_type="not_a_rule"))
    with pytest.raises(ContractError):
        _compile(_rule("r1", on_violation="ignore"))


def test_未知错误码拒绝_并在错误里列出冻结的错误码表() -> None:
    with pytest.raises(ContractError) as exc:
        _compile(_rule("r1", error_code="E_MADE_UP"))
    assert "E_NULL_PK" in str(exc.value)


def test_未给错误码时按规则类型取默认值() -> None:
    compiled = _compile(
        {
            "id": "a",
            "field": "order_id",
            "rule_type": "not_null",
            "params": {},
            "on_violation": "reject",
        },
        {
            "id": "b",
            "field": "phone",
            "rule_type": "regex",
            "params": {"pattern": "^1"},
            "on_violation": "reject",
        },
    )
    assert [r.error_code.value for r in compiled.rules] == ["E_NULL_PK", "E_REGEX_MISMATCH"]


def test_关掉的规则不编译() -> None:
    """留着它会让「前端看到的规则数」与「数据面真正执行的规则数」不一致。"""
    spec = _spec(
        _rule("on", "order_id", "not_null"),
        {**_rule("off", "amount", "range", min=0), "enabled": False},
    )
    compiled = compile_contract(spec, valid_columns=COLUMNS, target_type=DORIS)
    assert [r.rule_id for r in compiled.rules] == ["on"]


def test_规则引用了不存在的列_拒绝() -> None:
    with pytest.raises(ContractError) as exc:
        compile_contract(_spec(_rule("r1", "nope", "not_null")), valid_columns=COLUMNS, target_type=DORIS)
    assert "nope" in str(exc.value)


def test_规则没给_id_时按字段与方法生成() -> None:
    compiled = _compile(
        {
            "field": "amount",
            "rule_type": "range",
            "params": {"min": 0},
            "error_code": "E_RANGE_VIOLATION",
            "on_violation": "reject",
        }
    )
    assert compiled.rules[0].rule_id == "amount.range"


def test_规则_id_的字符集被收进安全集() -> None:
    compiled = _compile(_rule("q/../etc/passwd", "order_id", "not_null"))
    assert compiled.rules[0].rule_id == "q_.._etc_passwd"


def test_超长的规则_id_被安全截断且不撞车() -> None:
    long_id = "q_" + "x" * 200
    compiled = _compile(_rule(long_id), _rule(long_id + "_2", "amount", "range", min=0))
    ids = [rule.rule_id for rule in compiled.rules]
    assert all(len(item) <= MAX_RULE_ID for item in ids), ids
    assert len(ids) == len(set(ids)), ids


def test_同名规则_id_追加序号而不是互相覆盖() -> None:
    """``rule_id → 规则`` 必须是单射：撞名的两条规则会把 ``rule_hits`` 加在一起，
    而报告上看不出异常。"""
    compiled = _compile(
        _rule("dup", "order_id", "not_null"),
        _rule("dup", "phone", "regex", pattern="^1"),
    )
    assert [r.rule_id for r in compiled.rules] == ["dup", "dup#2"]


def test_规则_id_是稳定映射() -> None:
    """同一条规则编译两次得到同一个 id：``error_record.rule_id`` 是追溯的依据。"""
    first = _compile(_rule("q.orders.amount.range", "amount", "range", min=0))
    second = _compile(_rule("q.orders.amount.range", "amount", "range", min=0))
    assert first.rules[0].rule_id == second.rules[0].rule_id == "q.orders.amount.range"


def test_不支持的方言显式报错而不是降级成_MySQL() -> None:
    """降级出来的 SQL 在生成期拼得出来、到数据面才报语法错——那时作业已经跑了一半。"""
    with pytest.raises(ContractError):
        render_predicate(_one(_rule()), DataSourceType.POSTGRESQL)
    with pytest.raises(ContractError):
        render_route_selects(_compile(_rule()), valid_columns=COLUMNS, target_type=DataSourceType.MINIO)


def test_运行号必须是正整数() -> None:
    with pytest.raises(ContractError):
        render_tagged_select("SELECT 1", _compile(_rule()), run_id=0, target_type=DORIS)


# ================================================================ 影子表列


def test_影子表列清单取自计划_映射目标字段加派生列_按首次出现去重() -> None:
    from engines.plan.schema import EtlPlanModel

    plan = EtlPlanModel.model_validate(
        {
            "field_mappings": [
                {"source_field": "a", "source_type": "varchar", "target_field": "order_id", "target_type": "bigint"},
                {"source_field": "b", "source_type": "decimal", "target_field": "amount", "target_type": "decimal"},
                {"source_field": "c", "source_type": "varchar", "target_field": "order_id", "target_type": "bigint"},
            ],
            "transforms": [
                {"id": "t1", "kind": "date_format", "output_field": "ts_norm"},
                {"id": "t2", "kind": "cast", "output_field": "amount"},
            ],
            "budget": {
                "max_read_rows": 1000,
                "max_write_bytes": 1000,
                "max_duration_sec": 60,
                "max_amplification_ratio": "1",
                "max_reject_rate": "0.05",
                "warn_threshold_pct": 80,
            },
        }
    )
    assert target_columns(plan) == ("order_id", "amount", "ts_norm")


def test_列名不过白名单拒绝() -> None:
    with pytest.raises(ContractError):
        compile_contract(_spec(_rule()), valid_columns=("bad name",), target_type=DORIS)


# ================================================================ 指纹


def test_契约指纹对参数顺序不敏感_对取值敏感() -> None:
    a = _compile(_rule("r1", "amount", "range", min=0, max=10))
    b = _compile(_rule("r1", "amount", "range", max=10, min=0))
    c = _compile(_rule("r1", "amount", "range", min=0, max=11))
    assert a.contract_sha256 == b.contract_sha256
    assert a.contract_sha256 != c.contract_sha256


def test_契约指纹对数字的书写形态不敏感() -> None:
    """``0`` / ``0.0`` / ``"0"`` 收成同一个十进制文本，否则「重新生成一次得到不同指纹」
    会被当成制品被篡改。"""
    a = _compile(_rule("r1", "amount", "range", min=0))
    b = _compile(_rule("r1", "amount", "range", min=0.0))
    c = _compile(_rule("r1", "amount", "range", min="0"))
    assert a.contract_sha256 == b.contract_sha256 == c.contract_sha256


def test_契约指纹对规则声明顺序敏感() -> None:
    """顺序是**语义**（路由按声明序取第一条命中的错误码），所以要进指纹。"""
    a = _compile(_rule("r1", "order_id", "not_null"), _rule("r2", "amount", "range", min=0))
    b = _compile(_rule("r2", "amount", "range", min=0), _rule("r1", "order_id", "not_null"))
    assert a.contract_sha256 != b.contract_sha256
    assert [r.rule_id for r in a.rules] == ["r1", "r2"]


def test_契约指纹直接可由规则算出() -> None:
    compiled = _compile(_rule())
    assert contract_sha256(compiled.rules) == compiled.contract_sha256
    assert len(compiled.contract_sha256) == 64


def test_规则索引按声明序() -> None:
    compiled = _compile(_rule("a"), _rule("b", "amount", "range", min=0))
    assert [(r.rule_id, r.order) for r in compiled.index().values()] == [("a", 0), ("b", 1)]


def test_错误列清单与严重码是固定的() -> None:
    """``HARD_BLOCK_CODES`` 是「零容忍」清单：改它等于改安全口径，所以钉在用例里。"""
    assert {code.value for code in HARD_BLOCK_CODES} == {"E_SENSITIVE_PLAINTEXT", "E_SCHEMA_DRIFT"}
    assert ERROR_COLUMNS == (
        "run_id",
        "rule_id",
        "field_name",
        "error_code",
        "payload_digest",
        "reason",
        "occurred_at",
    )


# ================================================================ SQL 组装形状


def test_tagged_中间结果带路由列与运行号() -> None:
    tagged = render_tagged_select(
        "SELECT order_id, phone, amount FROM orders", _compile(_rule()), run_id=142, target_type=DORIS
    )
    assert tagged.startswith(f"SELECT {', '.join(COLUMNS)}, ")
    assert "142 AS etl_run_id" in tagged
    assert "AS _error_code" in tagged
    assert "AS _rule_id" in tagged
    assert "AS _reason" in tagged
    assert "AS _field_name" in tagged
    assert "THEN 'reject' ELSE 'valid' END AS _quality_route" in tagged
    # 源查询被套成子查询别名 ``src_tagged``；``tagged`` 这个名字由装配方（HOCON）赋给整段
    assert tagged.endswith(") AS src_tagged")


def test_有效行_select_显式列列且不带内部路由列() -> None:
    """内部列（``_quality_route`` / ``etl_run_id`` …）一旦落进影子表，
    Swap 时的列一致性校验必然失败——而且失败在最后一步，前面全绿。"""
    valid_sql, _rejected = render_route_selects(
        _compile(_rule()), valid_columns=COLUMNS, target_type=DORIS
    )
    assert valid_sql == f"SELECT {', '.join(COLUMNS)} FROM {TAGGED_TABLE} WHERE _quality_route = 'valid'"
    projection = valid_sql.split(" FROM ", 1)[0]
    for internal in ("_quality_route", "_error_code", "_rule_id", "_reason", "_field_name", "etl_run_id"):
        assert internal not in projection


def test_违规行_select_改名而不是重算谓词_并且内联摘要() -> None:
    _valid, rejected = render_route_selects(
        _compile(_rule(rule_type="regex", pattern="^1")), valid_columns=COLUMNS, target_type=DORIS
    )
    assert "etl_run_id AS run_id" in rejected
    assert "_rule_id AS rule_id" in rejected
    assert "_error_code AS error_code" in rejected
    assert "AS payload_digest" in rejected
    assert "AS occurred_at" in rejected
    # 摘要表达式与单独渲染的一致（同一份口径，不许在下游重算）
    assert render_digest_expr(COLUMNS, DORIS) in rejected
    # 谓词只出现一次（在 tagged 里），这里只改名
    assert "REGEXP" not in rejected
    assert rejected.endswith("WHERE _quality_route = 'reject'")


def test_摘要表达式在两种方言上只差文本类型名() -> None:
    doris = render_digest_expr(("phone",), DORIS)
    mysql = render_digest_expr(("phone",), MYSQL)
    assert "CAST(phone AS STRING)" in doris
    assert "CAST(phone AS CHAR)" in mysql
    assert "CONCAT_WS(CHAR(31)" in doris
    assert "SUBSTRING(SHA2(" in doris
    assert "LPAD(HEX(" in doris
    # NULL 占位符是显式字面量：CONCAT_WS 会**跳过** NULL 参数，
    # 不显式转占位符会让「整行都是 NULL」与「只有两个字段」拼出同一个串
    assert quote_literal(NULL_CANON) in doris
    assert NULL_CANON == "\\N"


def test_摘要字段集不能为空() -> None:
    with pytest.raises(ContractError):
        render_digest_expr((), DORIS)


# ================================================================ 摘要参考实现


def test_摘要黄金向量_ASCII() -> None:
    """手算：``sha256("x" + US + "\\N")`` 前 56 位 + ``00000003``（1 + 2 字节）。

    长度后缀**只数字段本身**，不含分隔符——与 SQL 侧的 ``Σ LENGTH(canon(f))`` 一致。
    """
    digest = payload_digest({"a": "x", "b": None}, ["a", "b"])
    assert digest == "0cf7584006f0873819eff94620fd84a5f31c819586231f393131090200000003"
    assert len(digest) == DIGEST_LEN == 64


def test_摘要黄金向量_中文按字节算() -> None:
    """``LENGTH()`` 在两种方言上都是**字节**语义，所以前缀和长度后缀都用 UTF-8：
    ``张三`` 6 字节 + ``\\N`` 2 字节 = 8。"""
    digest = payload_digest({"a": "张三", "b": None}, ["a", "b"])
    assert digest == "69ebca901d27ee12120596ef413d44d924e196be4b61fd2b8009486200000008"
    assert digest_content_length(digest) == 6 + 2


def test_摘要用不可见分隔符_不会让不同行撞车() -> None:
    """分隔符若可能出现在值里，两行不同的数据会拼出同一个串。

    ``("ab", "c")`` 与 ``("a", "bc")`` 在**无**分隔符时都是 ``abc``；换成逗号后，
    值里带逗号的行又会让 ``("a,b", "c")`` 与 ``("a", "b,c")`` 撞车。0x1F 是控制字符，
    正常业务文本里不会出现，这才是它的价值。
    """
    assert FIELD_SEP_CHARCODE == 31
    assert "".join(["ab", "c"]) == "".join(["a", "bc"])  # 无分隔符：撞车
    assert ",".join(["a,b", "c"]) == ",".join(["a", "b,c"])  # 可见分隔符：值里带它就撞车

    left = payload_digest({"a": "a,b", "b": "c"}, ["a", "b"])
    right = payload_digest({"a": "a", "b": "b,c"}, ["a", "b"])
    assert left != right


def test_字段顺序进摘要() -> None:
    """同一行按不同字段顺序算，摘要必须不同——否则「列顺序变了」这件事不会留下痕迹。"""
    row = {"a": "x", "b": "y"}
    assert payload_digest(row, ["a", "b"]) != payload_digest(row, ["b", "a"])


def test_空串与_NULL_是两种不同的规范化() -> None:
    assert canonical_field(None) == NULL_CANON == "\\N"
    assert canonical_field("") == ""
    assert payload_digest({"a": None}, ["a"]) != payload_digest({"a": ""}, ["a"])


def test_规范化去首尾空白_并且对数字与时间有确定形态() -> None:
    assert canonical_field("  x  ") == "x"
    assert canonical_field(12) == "12"
    assert canonical_field(Decimal("12.50")) == "12.50"
    assert canonical_field(date(2026, 10, 16)) == "2026-10-16"
    assert canonical_field(datetime(2026, 10, 16, 8, 1, 2, 3000, tzinfo=UTC)) == "2026-10-16 08:01:02.003"


def test_布尔列的规范化显式拒绝而不是猜() -> None:
    """MySQL ``CAST(true AS CHAR)`` 得 ``'1'``，Doris 得 ``'true'``——猜一个就会得到
    「摘要对不上但不报错」。参考实现拒绝，让人换一列做黄金向量。"""
    with pytest.raises(TypeError):
        canonical_field(True)
    with pytest.raises(TypeError):
        canonical_field(object())


def test_一行的规范化_用不可见分隔符拼接() -> None:
    assert canonicalize_row({"a": "x", "b": None, "c": " z "}, ["a", "b", "c"]) == "x\x1f\\N\x1fz"


def test_摘要长度后缀是大写_对齐_MySQL_HEX() -> None:
    assert payload_digest({"a": "abcdefgh"}, ["a"])[-8:] == f"{8:08X}"
    # 长度 10 = 0x0A：小写会是 ``0000000a``，肉眼几乎看不出差别
    assert payload_digest({"a": "a" * 10}, ["a"])[-8:] == "0000000A"


def test_摘要长度字段不对时显式报错() -> None:
    with pytest.raises(ValueError):
        digest_content_length("short")


# ================================================================ 单条规则求值


def test_空值规则_True_表示违规() -> None:
    rule = _one(_rule())
    assert evaluate_rule(rule, {"order_id": None}) is True
    assert evaluate_rule(rule, {"order_id": ""}) is False
    assert evaluate_rule(rule, {"order_id": "  "}) is False


def test_空值规则_allow_blank_时空白也算违规() -> None:
    rule = _one(_rule(allow_blank=True))
    assert evaluate_rule(rule, {"order_id": "  "}) is True
    assert evaluate_rule(rule, {"order_id": "x"}) is False


def test_空值只由_not_null_管_其它规则对_NULL_不报违规() -> None:
    """与 SQL 侧的 ``f IS NOT NULL AND …`` 守卫同一语义：一条 NULL 只该产生一条结论，
    否则同一行会在分布里被数两次（错误码分布是前端图表的分子）。"""
    assert evaluate_rule(_one(_rule(rule_type="regex", pattern="^1")), {"order_id": None}) is False
    assert evaluate_rule(_one(_range(min=0)), {"amount": None}) is False
    assert evaluate_rule(_one(_rule(rule_type="enum", values=["a"])), {"order_id": None}) is False


def test_正则用_search_语义与_SQL_的_REGEXP_一致() -> None:
    """``re.match`` 会让 ``'abc123' REGEXP '\\d'`` 在两边得到相反结论。"""
    rule = _one(_rule("r1", "phone", "regex", pattern=r"\d"))
    assert evaluate_rule(rule, {"phone": "abc123"}) is False  # 命中了模式 → 不违规
    assert evaluate_rule(rule, {"phone": "abc"}) is True


def test_正则_expect_not_match_在参考实现里也要反过来() -> None:
    rule = _one(_rule("r1", "phone", "regex", pattern=r"^\d+$", expect="not_match"))
    assert evaluate_rule(rule, {"phone": "123"}) is True  # 匹配上了 → 违规
    assert evaluate_rule(rule, {"phone": "abc"}) is False


def test_区间规则对转不动的值不报违规() -> None:
    """类型问题归 ``type_cast`` / ``not_null``：在区间规则里报违规，
    会把一条类型问题伪装成一条取值问题，排查方向直接跑偏。"""
    rule = _one(_range(min=0, max=10))
    assert evaluate_rule(rule, {"amount": "abc"}) is False
    assert evaluate_rule(rule, {"amount": "5"}) is False
    assert evaluate_rule(rule, {"amount": "-1"}) is True
    assert evaluate_rule(rule, {"amount": 10}) is True  # 上界默认开
    assert evaluate_rule(rule, {"amount": 9}) is False


def test_区间规则的闭区间边界() -> None:
    rule = _one(_range(min=0, max=10, inclusive_min=False, inclusive_max=True))
    assert evaluate_rule(rule, {"amount": 0}) is True  # 下界开 → 等于下界即违规
    assert evaluate_rule(rule, {"amount": 10}) is False  # 上界闭 → 等于上界不算违规


def test_枚举规则对数字与字符串都能比对() -> None:
    rule = _one(_rule("r1", "amount", "enum", values=[1, 2]))
    assert evaluate_rule(rule, {"amount": 1}) is False
    assert evaluate_rule(rule, {"amount": "1"}) is False
    assert evaluate_rule(rule, {"amount": 3}) is True


def test_类型规则识别形状() -> None:
    rule = _one(_rule("r1", "created_at", "type_cast", target_type="date"))
    assert evaluate_rule(rule, {"created_at": "2026-10-16"}) is False
    assert evaluate_rule(rule, {"created_at": "16/10/2026"}) is True


def test_类型规则的可选目标类型是显式的() -> None:
    assert "date" in supported_typecast_types()
    assert "varchar" in supported_typecast_types()
    with pytest.raises(ContractError):
        _compile(_rule("r1", "created_at", "type_cast", target_type="jsonb"))


# ================================================================ 拒绝率与判定


def test_拒绝率的定标与边界() -> None:
    assert reject_rate_of(5, 100) == Decimal("0.0500")
    assert reject_rate_of(0, 0) == Decimal("0.0000")  # 不除零
    assert reject_rate_of(1, 6) == Decimal("0.1667")  # ROUND_HALF_UP
    assert reject_rate_of(2, 8) == Decimal("0.2500")
    assert reject_rate_of(1, 3) == Decimal("0.3333")


def test_等于预算算通过() -> None:
    """预算即**闭区间上界**：``0.05 <= 0.05``。用 float 判这一条偶尔会判反，
    所以整条链上都是 Decimal。"""
    contract = _spec(budget="0.05")
    assert decide_passed(Decimal("0.0500"), {}, contract, processed_rows=100, read_rows=100) == (True, None)
    assert decide_passed(Decimal("0.0501"), {}, contract, processed_rows=100, read_rows=100) == (
        False,
        "over_budget",
    )


def test_零容忍错误码与拒绝率无关() -> None:
    contract = _spec(budget="0.50")
    assert decide_passed(
        Decimal("0.0000"),
        {"E_SENSITIVE_PLAINTEXT": 1},
        contract,
        processed_rows=100,
        read_rows=100,
    ) == (False, "hard_block_code")
    assert decide_passed(
        Decimal("0.0000"), {"E_SCHEMA_DRIFT": 3}, contract, processed_rows=100, read_rows=100
    ) == (False, "hard_block_code")
    # 普通错误码不阻断
    assert decide_passed(
        Decimal("0.0000"), {"E_NULL_PK": 1}, contract, processed_rows=100, read_rows=100
    ) == (True, None)
    # 计数为 0 的严重码不算命中（分布表里可能有 0 值行）
    assert decide_passed(
        Decimal("0.0000"), {"E_SCHEMA_DRIFT": 0}, contract, processed_rows=100, read_rows=100
    ) == (True, None)


def test_读到了行却一行都没分流必须拦() -> None:
    """``processed == 0`` 时拒绝率是 0、硬阻断码也没有，**只看数字会判通过**。
    它意味着整批数据在数据面丢了，而报告会显示一次干净的成功。"""
    contract = _spec(budget="0.05")
    assert decide_passed(Decimal("0.0000"), {}, contract, processed_rows=0, read_rows=100) == (
        False,
        "zero_routed_rows",
    )
    # 源端本来就是空的：不是错误，放行（另有 no_rows 警告）
    assert decide_passed(Decimal("0.0000"), {}, contract, processed_rows=0, read_rows=0) == (True, None)


def test_零路由优先于硬阻断码() -> None:
    contract = _spec(budget="0.05")
    assert decide_passed(
        Decimal("0.0000"), {"E_SCHEMA_DRIFT": 5}, contract, processed_rows=0, read_rows=10
    ) == (False, "zero_routed_rows")


def test_不传计数时退回两条基本规则() -> None:
    """§3.2 的原始签名只有三个位置参数（生成期的调用就是那个形态）。"""
    contract = _spec(budget="0.05")
    assert decide_passed(Decimal("0.0100"), {}, contract) == (True, None)
    assert decide_passed(Decimal("0.9000"), {}, contract) == (False, "over_budget")


def test_比率文本对齐账本要求() -> None:
    """账本 payload 不允许 float；``str(float)`` 会出现 ``0.30000000000000004`` 这种
    「同一个语义两个文本」，链上就会看到两个不一样的值。"""
    assert format_rate(0.05) == "0.0500"
    assert format_rate(Decimal("0.0500")) == "0.0500"
    assert format_rate(0.1 + 0.2) == "0.3000"


def test_预算使用率的三个取值() -> None:
    assert budget_used_ratio_of(0.05, Decimal("0.05")) == 1.0
    assert budget_used_ratio_of(Decimal("0.0100"), Decimal("0.0500")) == 0.2
    # 预算为 0 时比值没有意义：回 None 让前端显示「无预算」，
    # 而不是显示 0%（看起来像「预算还剩很多」）
    assert budget_used_ratio_of(Decimal("0.0000"), Decimal("0.0000")) is None


# ================================================================ 报告合成

_R1 = _rule("r1", "order_id", "not_null", error_code="E_NULL_PK")
_R2 = _rule("r2", "amount", "range", min=0, on_violation="warn", error_code="E_RANGE_VIOLATION")


def _report(**overrides: Any) -> QualityReportDTO:
    compiled = _compile(_R1, _R2)
    kwargs: dict[str, Any] = {
        "valid_rows": 95,
        "rejected_rows": 5,
        "read_rows": 100,
        "error_code_dist": {"E_NULL_PK": 5},
        "rule_hits": {"r1": 5, "r2": 9},
        "contract": _spec(_R1, _R2),
        "rule_index": compiled.index(),
        "interrupted": False,
        "generated_at": _T0,
        "contract_sha256": compiled.contract_sha256,
    }
    kwargs.update(overrides)
    return build_report(7, **kwargs)


def test_报告的基本形状() -> None:
    dto = _report()
    assert (dto.run_id, dto.valid_rows, dto.rejected_rows, dto.processed_rows) == (7, 95, 5, 100)
    assert dto.reject_rate == 0.05
    assert dto.reject_rate_budget == 0.05
    assert dto.budget_used_ratio == 1.0
    assert dto.passed is True
    assert dto.blocked_reason is None
    assert dto.failed_rules == ["r1"]
    assert dto.generated_at == _T0
    assert dto.error_code_dist == {"E_NULL_PK": 5}


def test_分布里计数为零的条目不进报告() -> None:
    dto = _report(error_code_dist={"E_NULL_PK": 5, "E_SCHEMA_DRIFT": 0}, rule_hits={"r1": 5, "r2": 0})
    assert dto.error_code_dist == {"E_NULL_PK": 5}
    assert dto.rule_hits == {"r1": 5}


def test_失败的规则只算_reject_且按声明序() -> None:
    assert _report().failed_rules == ["r1"]  # warn 命中不计入「哪几条规则没过」

    both = _compile(
        _rule("b", "amount", "range", min=0, error_code="E_RANGE_VIOLATION"),
        _rule("a", "order_id", "not_null", error_code="E_NULL_PK"),
    )
    dto2 = _report(rule_index=both.index(), rule_hits={"a": 1, "b": 2})
    assert dto2.failed_rules == ["b", "a"]  # 声明序，不是字典序


def test_诊断占比的分母按违规处置分开() -> None:
    """``reject`` 的分母是**拒绝行数**，``warn`` 的分母是**处理行数**：
    warn 命中的行照常进影子表，把它们算进「拒绝」会让占比加起来超过 100%。"""
    detail = _report().diagnostics_detail
    assert detail is not None
    by_id = {rule.rule_id: rule for rule in detail.rules}
    assert by_id["r1"].share == pytest.approx(1.0)
    assert by_id["r2"].share == pytest.approx(0.09)
    assert by_id["r1"].on_violation == "reject"
    assert by_id["r2"].on_violation == "warn"


def test_每条规则都带一条固定建议文案() -> None:
    detail = _report().diagnostics_detail
    assert detail is not None
    assert all(rule.suggestion == SUGGESTIONS[rule.rule_type] for rule in detail.rules)
    assert all(rule.suggestion for rule in detail.rules)


def test_错误码_top_n_按计数降序_并列按码升序_最多五条() -> None:
    dist = {"E_A": 1, "E_B": 5, "E_C": 5, "E_D": 2, "E_E": 3, "E_F": 4}
    detail = _report(error_code_dist=dist, rejected_rows=20).diagnostics_detail
    assert detail is not None
    assert [item.code for item in detail.top_error_codes] == ["E_B", "E_C", "E_F", "E_E", "E_D"]
    assert all(item.count > 0 for item in detail.top_error_codes)


def test_中断的报告一律不可发布_但原因码保留事实() -> None:
    dto = _report(interrupted=True)
    assert dto.passed is False
    assert dto.interrupted is True
    assert dto.diagnostics_detail is not None
    assert "interrupted" in dto.diagnostics_detail.warnings


def test_超预算的报告带上原因码() -> None:
    dto = _report(valid_rows=80, rejected_rows=20, read_rows=100)
    assert dto.passed is False
    assert dto.blocked_reason == "over_budget"
    assert dto.diagnostics_detail is not None
    assert dto.diagnostics_detail.blocked_reason == "over_budget"


def test_空源的警告与放行() -> None:
    """be-17 §6.4：空源不算失败，只留警告。

    ⚠️ §10 第 7 条：空影子表做 Swap 会**清空正式表**，产品上是否允许「空源发布」
    尚未拍板。这里按 §6.4 的现行算法放行——要改成拦下的话，
    改的是 ``engines/quality/report.py::build_report`` 里 ``read_rows == 0`` 那一段。
    """
    dto = _report(valid_rows=0, rejected_rows=0, read_rows=0, error_code_dist={}, rule_hits={})
    assert dto.passed is True
    assert dto.diagnostics_detail is not None
    assert "no_rows" in dto.diagnostics_detail.warnings


def test_注入的警告去重且排在算出来的后面() -> None:
    dto = _report(
        valid_rows=0,
        rejected_rows=0,
        read_rows=0,
        error_code_dist={},
        rule_hits={},
        warnings=["no_rows", "error_records_truncated", ""],
    )
    assert dto.diagnostics_detail is not None
    # 由本模块算出的 ``no_rows`` 在最前，注入的重复项被丢掉，空串被丢掉
    assert dto.diagnostics_detail.warnings == ["no_rows", "error_records_truncated"]


def test_报告里的诊断锚点与契约锚点一致() -> None:
    """重算一份锚点 = 两套独立计算，哪天归一化逻辑变了就会悄悄分叉。"""
    dto = _report()
    assert dto.contract_sha256 == _compile(_R1, _R2).contract_sha256
    assert dto.diagnostics_detail is not None
    assert dto.diagnostics_detail.contract_sha256 == dto.contract_sha256
    assert dto.diagnostics_detail.failed_rules == dto.failed_rules


def test_诊断结构能_JSON_往返_时间戳与预算都不丢() -> None:
    dto = _report(
        samples={"r1": [QualityDiagnosticSample(payload_digest="a" * 64, lengths=[12], reason="order_id 为空")]}
    )
    assert dto.diagnostics_detail is not None
    payload = dto.diagnostics_detail.model_dump(mode="json")
    back = QualityDiagnostics.model_validate(payload)
    assert back.generated_at == _T0
    assert back.reject_rate_budget == dto.reject_rate_budget
    assert back.rules[0].samples[0].payload_digest == "a" * 64
    assert back.version == "v1"


def test_每条规则的样本最多三条() -> None:
    samples = [QualityDiagnosticSample(payload_digest=f"{i:064d}", lengths=[1], reason="x") for i in range(9)]
    dto = _report(samples={"r1": samples})
    assert dto.diagnostics_detail is not None
    assert len(dto.diagnostics_detail.rules[0].samples) == 3


def test_前端数组形状由同一份诊断投影出来() -> None:
    dto = _report()
    assert dto.diagnostics_detail is not None
    items = build_diagnostic_items(dto.diagnostics_detail)
    assert [(item.rule_id, item.field, item.error_code) for item in items] == [
        ("r1", "order_id", "E_NULL_PK"),
        ("r2", "amount", "E_RANGE_VIOLATION"),
    ]
    assert all(item.summary for item in items)
    # 数组是对象投影的同一次产出，不会各写一套
    assert [item.rule_id for item in dto.diagnostics] == [item.rule_id for item in items]


def test_诊断样本里只有摘要与长度_没有任何原值字段() -> None:
    """脱敏是结构性的：``QualityDiagnosticSample`` 的字段表里**不存在**值的位置。"""
    assert set(QualityDiagnosticSample.model_fields) == {"payload_digest", "lengths", "reason"}
    dumped = QualityDiagnostics(generated_at=_T0, rules=[]).model_dump(mode="json")
    assert set(dumped) == {
        "version",
        "blocked_reason",
        "interrupted",
        "warnings",
        "top_error_codes",
        "rules",
        "failed_rules",
        "contract_sha256",
        "reject_rate_budget",
        "generated_at",
    }
