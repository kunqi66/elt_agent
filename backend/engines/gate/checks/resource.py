"""``check_resource``：资源范围、跨库、密钥登记与预算自洽（be-11 §5.1 第 5 步 / §6.4）。

判据来自 ``ctx.policy_scope``（be-07 的 PDP 已经在它上面标出 ``out_of_scope`` /
``unregistered_secret_refs``）与 ``ctx.data_source_types``。**门禁不自己跑 PDP**——
它只消费结论，否则「同一份制品 + 同一份策略 → 同一份结论」就不成立了。

## be-11 §6.4 里在本 DSL / 本上下文不可实现的两条（登记，不实现）

- ``unknown_table``：前提是 ``write_mode="overwrite"``，而本仓库 ``PlanOutput.mode``
  固定为 ``shadow_then_swap``（``engines/plan/schema.py`` 的差异表），无对应输入。
- ``unregistered_secret`` **部分实现**：be-11 §3.2 的 ``evaluate()`` 签名里有
  ``secret_refs: Mapping[int, frozenset[str]]``，但 ``GateContext`` 没有这个字段
  （不属我可以改的文件）。因此改用 PDP 已经算好的
  ``policy_scope.unregistered_secret_refs`` 作为数据源——结论等价，
  且少一次「门禁自己判密钥登记」的重复逻辑。

## 新增/借用的 code（be-11 §6.4 未定义的）

- ``data_source_not_registered``（**新增**）：数据源类型不在 ``ctx.data_source_types`` 里。
  ``RenderContext.ds_type`` 遇到这种输入直接抛 ``KeyError``；门禁必须把它变成一条
  **可定位**的 finding，而不是让渲染/执行阶段崩掉。
- ``budget_nonpositive``（**借用 be-10**）：``engines/plan/schema.py`` 的 ``PlanIssueCode``
  已逐字定义。注意 ``RuntimeBudget`` 的六个字段都有 ``gt=0``，正常情况下根本构造不出
  非正值——这段是防御性守卫，防的是「约束被谁改松了」。
"""

from __future__ import annotations

from engines.gate.checks.base import finalize_findings, iter_secret_tokens, safe_message
from engines.gate.context import GateContext
from engines.gate.types import (
    DagNodeLocator,
    GateFinding,
    HoconLocator,
    PlanLocator,
    SourceTableLocator,
    make_finding,
)
from engines.policy.models import ResourceRef

#: ``RuntimeBudget`` 的数值字段，任一非正即异常。
_BUDGET_FIELDS = (
    "max_read_rows",
    "max_write_bytes",
    "max_duration_sec",
    "max_amplification_ratio",
    "max_reject_rate",
    "warn_threshold_pct",
)


def check_resource(ctx: GateContext) -> list[GateFinding]:
    """资源范围与预算。纯函数：只看 ``ctx``。"""
    findings: list[GateFinding] = []
    findings.extend(_data_source_type_findings(ctx))
    findings.extend(_scope_findings(ctx))
    findings.extend(_cross_boundary_findings(ctx))
    findings.extend(_unregistered_secret_findings(ctx))
    findings.extend(_budget_findings(ctx))
    return finalize_findings("resource", findings)


def _data_source_type_findings(ctx: GateContext) -> list[GateFinding]:
    """数据源类型必须已注册。

    ``data_source_types`` **为空时跳过**：空表既可能是「这台库没登记类型」，
    也可能是「service 忘了注入」。把后者当成「全部越界」会让每一次门禁都 block 整份方案——
    宁可少判一条，也不要制造一个必然误报。
    """
    if not ctx.data_source_types:
        return []
    findings: list[GateFinding] = []
    for inp in ctx.plan.inputs:
        if inp.data_source_id in ctx.data_source_types:
            continue
        findings.append(
            make_finding(
                check_id="resource",
                code="data_source_not_registered",
                severity="block",
                message=safe_message(
                    f"输入 {inp.id} 引用的数据源 {inp.data_source_id} 没有登记类型，"
                    "渲染器无法为它选择连接器模板。"
                ),
                locator=SourceTableLocator(
                    data_source_id=inp.data_source_id,
                    schema=inp.schema_name,
                    table=inp.table,
                ),
                suggestion="先登记该数据源的类型，或从方案里去掉这个输入。",
            )
        )
    for out in ctx.plan.outputs:
        if out.data_source_id in ctx.data_source_types:
            continue
        findings.append(
            make_finding(
                check_id="resource",
                code="data_source_not_registered",
                severity="block",
                message=safe_message(
                    f"输出 {out.id} 引用的数据源 {out.data_source_id} 没有登记类型，"
                    "渲染器无法为它选择连接器模板。"
                ),
                locator=SourceTableLocator(
                    data_source_id=out.data_source_id,
                    schema=out.database,
                    table=out.table,
                ),
                suggestion="先登记该数据源的类型，或从方案里去掉这个输出。",
            )
        )
    return findings


def _scope_findings(ctx: GateContext) -> list[GateFinding]:
    """越界资源：PDP 标出的 ``out_of_scope``，以及不在 ``authorized_databases`` 里的库。"""
    scope = ctx.policy_scope
    if scope is None:
        # 没有范围就判不了「是否越界」。默认拒绝的那一刀由 check_policy 砍（缺判定结论即 block）。
        return []

    findings: list[GateFinding] = []
    for inp in ctx.plan.inputs:
        if _matches_any(scope.out_of_scope, inp.data_source_id, inp.schema_name, inp.table):
            findings.append(
                make_finding(
                    check_id="resource",
                    code="scope_unauthorized",
                    severity="block",
                    message=safe_message(
                        f"输入 {inp.id} 引用的表 {inp.schema_name}.{inp.table}"
                        f"（数据源 {inp.data_source_id}）不在项目授权范围内。"
                    ),
                    locator=SourceTableLocator(
                        data_source_id=inp.data_source_id,
                        schema=inp.schema_name,
                        table=inp.table,
                    ),
                    suggestion="从授权范围里去掉这个输入，或先把这个资源纳入项目授权。",
                )
            )
        elif scope.authorized_databases and inp.schema_name not in scope.authorized_databases:
            findings.append(
                make_finding(
                    check_id="resource",
                    code="scope_unauthorized",
                    severity="block",
                    message=safe_message(
                        f"输入 {inp.id} 所在的库 {inp.schema_name} 不在项目授权的库列表内。"
                    ),
                    locator=SourceTableLocator(
                        data_source_id=inp.data_source_id,
                        schema=inp.schema_name,
                        table=inp.table,
                    ),
                    suggestion="确认项目授权范围，或换用已授权的库。",
                )
            )

    for out in ctx.plan.outputs:
        if _matches_any(scope.out_of_scope, out.data_source_id, out.database, out.table):
            findings.append(
                make_finding(
                    check_id="resource",
                    code="scope_unauthorized",
                    severity="block",
                    message=safe_message(
                        f"输出 {out.id} 写入的表 {out.database}.{out.table}"
                        f"（数据源 {out.data_source_id}）不在项目授权范围内。"
                    ),
                    locator=SourceTableLocator(
                        data_source_id=out.data_source_id,
                        schema=out.database,
                        table=out.table,
                    ),
                    suggestion="换用已授权的目标表，或先把该资源纳入项目授权。",
                )
            )
        elif scope.authorized_databases and out.database not in scope.authorized_databases:
            findings.append(
                make_finding(
                    check_id="resource",
                    code="scope_unauthorized",
                    severity="block",
                    message=safe_message(f"输出 {out.id} 写入的库 {out.database} 不在项目授权的库列表内。"),
                    locator=SourceTableLocator(
                        data_source_id=out.data_source_id,
                        schema=out.database,
                        table=out.table,
                    ),
                    suggestion="确认项目授权范围，或换用已授权的目标库。",
                )
            )
    return findings


def _cross_boundary_findings(ctx: GateContext) -> list[GateFinding]:
    """跨库 / 跨 schema（be-11 §6.4）。

    **只在同一侧统计**：源端读 MySQL 的 ``sales``、写 Doris 的 ``sales_wide``
    是「一次同步」的常态（演示场景一就是这样），把源库与目标库比较会把它误判成跨库。
    所以：源端看 ``schema_name``、目标端看 ``database``，各自内部比较。
    """
    findings: list[GateFinding] = []
    inputs = ctx.plan.inputs
    schemas = {inp.schema_name for inp in inputs if inp.schema_name}
    if len(schemas) > 1:
        # 同一个数据源内的多个 schema 是「跨 schema」，跨数据源才是「跨库」。
        code = "cross_schema" if len({inp.data_source_id for inp in inputs}) == 1 else "cross_database"
        first_schema = next(inp.schema_name for inp in inputs if inp.schema_name)
        for inp in inputs:
            if inp.schema_name and inp.schema_name != first_schema:
                findings.append(
                    make_finding(
                        check_id="resource",
                        code=code,
                        severity="block",
                        message=safe_message(
                            f"输入 {inp.id} 读的是 {inp.schema_name}.{inp.table}，与本方案其它输入"
                            f"（首个为 {first_schema}）不在同一个库/schema；单次作业只能连一个源库。"
                        ),
                        locator=DagNodeLocator(node_id=inp.id),
                        suggestion="把跨库的读取拆成多个方案分别执行。",
                    )
                )

    outputs = ctx.plan.outputs
    databases = {out.database for out in outputs if out.database}
    if len(databases) > 1:
        first_database = next(out.database for out in outputs if out.database)
        for out in outputs:
            if out.database and out.database != first_database:
                findings.append(
                    make_finding(
                        check_id="resource",
                        code="cross_database",
                        severity="block",
                        message=safe_message(
                            f"输出 {out.id} 写向 {out.database}.{out.table}，与本方案其它输出"
                            f"（首个为 {first_database}）不在同一个库；影子表 + 原子 Swap 要求目标库唯一。"
                        ),
                        locator=DagNodeLocator(node_id=out.id),
                        suggestion="把写入拆成多个方案，保证每个方案只有一个目标库。",
                    )
                )
    return findings


def _unregistered_secret_findings(ctx: GateContext) -> list[GateFinding]:
    """HOCON 里用到、但 PDP 判定其密钥未登记的数据源 → block。"""
    scope = ctx.policy_scope
    if scope is None or not scope.unregistered_secret_refs:
        return []
    unregistered = set(scope.unregistered_secret_refs)
    findings: list[GateFinding] = []
    for line, data_source_id, field in iter_secret_tokens(ctx.hocon_content):
        if data_source_id not in unregistered:
            continue
        findings.append(
            make_finding(
                check_id="resource",
                code="unregistered_secret",
                severity="block",
                message=safe_message(
                    f"HOCON 第 {line} 行引用了数据源 {data_source_id} 的 {field} 令牌，"
                    "但该数据源没有已登记的密钥引用（secret_reference），执行时无法物化。"
                ),
                locator=HoconLocator(line=line, column=1),
                suggestion="先在数据源上登记密钥引用（Vault 路径 + 版本），再重跑门禁。",
            )
        )
    return findings


def _budget_findings(ctx: GateContext) -> list[GateFinding]:
    """预算必须为正（防御性：``RuntimeBudget`` 的 ``gt=0`` 通常已挡住）。"""
    findings: list[GateFinding] = []
    for name in _BUDGET_FIELDS:
        value = getattr(ctx.plan.budget, name)
        if value <= 0:
            findings.append(
                make_finding(
                    check_id="resource",
                    code="budget_nonpositive",
                    severity="block",
                    message=safe_message(f"运行预算 /budget/{name} 的取值为 {value}，必须为正数。"),
                    locator=PlanLocator(path=f"/budget/{name}"),
                    suggestion="给出一个正的预算值，否则运行期监督无法判定超限。",
                )
            )
    return findings


def _matches_any(refs: tuple[ResourceRef, ...], data_source_id: int, database: str, table: str) -> bool:
    """越界引用是否命中该资源。

    **要求 ``data_source_id`` 相等**：若只按库表匹配，一条 ``data_source_id=None`` 的空引用
    会命中全部资源，把整份方案误判成越界。
    """
    for ref in refs:
        if ref.data_source_id is not None and ref.data_source_id != data_source_id:
            continue
        if ref.data_source_id is None and ref.table != table:
            continue
        if ref.table and table and ref.table != table:
            continue
        if ref.database and database and ref.database != database:
            continue
        return True
    return False


__all__ = ["check_resource"]
