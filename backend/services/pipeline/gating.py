"""门禁的 service 侧：把数据库里的事实凑成 ``GateContext``，再把结论交给纯函数门禁。

    build_gate_context(...)  →  GateContext      （async，读库 + 跑 PDP）
    run_gate(...)            →  GateOutcome      （上面两步 + evaluate）

**为什么 PDP 在这里跑、不在门禁里跑**：``engines/gate/gate.py`` 是纯同步函数，
这是「同一份制品 + 同一份策略 → 同一份结论」这条可复现性的前提。
PDP 要读策略规则表（be-07）、要 ``await``，所以它在 service 里先算好，
以 ``PolicyDecision`` 的形态塞进 ctx。门禁只做「把结论翻译成 finding」。

## 本模块最容易出错的三个地方（都在下面的代码里标了）

1. **档案过期不等于没有档案**。档案过期时**照样把 ``TableProfile`` 建出来**
   （带上 ``expires_at``），由 ``check_schema`` 去报「档案已过期」。
   在这里提前 return 一个空 ctx 的后果是：门禁会报「源表不存在」——
   一句把人带偏的错话，因为表明明存在，只是我们对它的认识过期了。
2. **``contains_plaintext`` / ``masked`` 只能看 Plan，不能看档案**。
   档案里的 ``masked`` 说的是「探查样本脱敏了没有」，与「作业会不会明文写出」
   是两件事。把档案字段拿来当 Plan 的脱敏结论，会让每一列都被判成「已脱敏」，
   于是 P0 敏感明文出域**永远不会命中**——安静地绕过了 P0。
3. **敏感类别按列名重新推断**（``infer_sensitivity``），不只看档案里的分级。
   ``_is_p0_sensitive_egress`` 的白名单分支就是为「标注成 internal 的身份证」
   准备的：分级是人工标注的产物，而「身份证算不算敏感」不是一道需要人判断的题。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.ids import sha256_hex
from engines.gate.context import GateContext, ProfileColumn, TableProfile
from engines.gate.gate import evaluate
from engines.gate.types import AutofixRound, GateResult
from engines.hocon import PARSER_VERSION, RenderedHocon, parse_and_validate
from engines.masking.policy import infer_sensitivity
from engines.plan.schema import EtlPlanModel
from engines.policy import (
    BudgetRequest,
    ResourceRef,
    ResourceScope,
    ToolIntent,
    to_policy_kind,
)
from engines.policy import SensitiveColumn as PolicySensitiveColumn
from models.datasource import DataSource, MetadataProfile
from models.enums import DataSourceType, Env, RiskLevel, SensitiveLevel, ToolName
from schemas.auth import SubjectContext
from services.pipeline import parts
from services.pipeline.profile_view import tables_of
from services.pipeline.repository import latest_profile
from services.policy import decide_for_project

#: 敏感级别由低到高。用来算「本次作业触碰的最高敏感级别」——
#: PDP 的 level 参数是**单个**值，而一个方案会碰到很多列。
_LEVEL_ORDER: dict[SensitiveLevel, int] = {
    SensitiveLevel.PUBLIC: 0,
    SensitiveLevel.INTERNAL: 1,
    SensitiveLevel.CONFIDENTIAL: 2,
    SensitiveLevel.RESTRICTED: 3,
}

#: Plan 里表达「这一列的裸值不落目标端」的两种写法：
#: 字段级 ``masking``，或派生列算子是 ``mask``/``hash``（后者把整个字段换成摘要）。
_MASK_TRANSFORMS = frozenset({"mask", "hash"})


def render_meta(rendered: RenderedHocon) -> dict[str, Any]:
    """渲染段：写进 ``hocon_artifact.validation_result`` 的渲染侧事实。

    与 ``GateOutcome.artifact_meta()``（解析/门禁段）拼在一起才是完整的
    ``validation_result``。**两段各自只有一份实现**，由生产者调用、
    由消费方按段读（``parse`` / ``gate`` / ``sha256``）——
    早先渲染节点与落库路径各拼一份，形状慢慢就不一样了，症状是
    ``get_gate`` 读到的是套了一层的信封，前端「门禁」页 500。

    ``parser_version`` 记的是**解析器**版本（``pyhocon+elt-render-1``），
    不是渲染器版本：落地后要能回答「同一份 HOCON 昨天过今天不过，是哪一端变了」，
    只记渲染器回答不了这个问题。渲染器版本单独在 ``renderer_version``。
    """
    return {
        "sha256": sha256_hex(rendered.content),
        "parser_version": PARSER_VERSION,
        "renderer_version": str(rendered.renderer_version or ""),
        "job_name": str(rendered.job_name or ""),
        # 令牌只记数量：令牌本身不含明文，但逐个记录会把「哪个库用哪个键」
        # 这类结构信息写进 state 快照与日志。
        "token_count": len(rendered.tokens or ()),
    }


@dataclass(frozen=True, slots=True)
class GateOutcome:
    """门禁一次运行的全部产物。

    ``context`` 一并返回而不是丢掉，是因为**自动修复回路需要它**：
    ``RecheckFn`` 的签名是同步的 ``(plan, hocon) -> GateResult``（engines 不许做 IO），
    所以重判只能靠「拿旧 ctx 换掉 plan/hocon 两个字段再跑一次」——
    没有 ctx 就没有重判，自动修复就退化成「修完不知道好没好」。
    """

    result: GateResult
    context: GateContext

    @property
    def conclusion(self) -> str:
        return self.result.conclusion

    def artifact_meta(self) -> dict[str, Any]:
        """落进 ``hocon_artifact.validation_result`` 的内容。

        门禁结论没有独立的表，而它是**判断这份 HOCON 能不能用**的那个结论，
        放在 HOCON 制品上是最贴的位置。``parse`` 与 ``gate`` 分开存：
        事后要能分辨「解析没过」与「解析过了但门禁没过」，
        这两种情况的排查方向完全不同。
        """
        parse = self.context.parse_result
        return {
            "parse": {
                "ok": parse.ok,
                "parser_version": parse.parser_version,
                "errors": [e.model_dump(mode="json") for e in parse.errors],
            },
            "gate": self.result.model_dump(mode="json"),
            "policy_decision_id": None,
            "policy_version": getattr(self.context.policy_decision, "policy_version", 0),
        }


# ---------------------------------------------------------------- 建 ctx


async def build_gate_context(
    *,
    session: AsyncSession,
    subject: SubjectContext,
    project_id: int,
    plan: EtlPlanModel,
    hocon_content: str,
    now: datetime,
) -> GateContext:
    """读档案 + 读数据源 + 跑 PDP，凑出一份完整的门禁输入。"""
    ds_ids = {i.data_source_id for i in plan.inputs} | {o.data_source_id for o in plan.outputs}
    sources = await _load_data_sources(session=session, tenant_id=subject.tenant_id, ids=ds_ids)

    source_tables: dict[tuple[int, str, str], TableProfile] = {}
    profiles_sha: list[str] = []
    for item in plan.inputs:
        ds = sources.get(item.data_source_id)
        profile = await latest_profile(
            session=session, tenant_id=subject.tenant_id, data_source_id=item.data_source_id
        )
        if profile is not None and profile.sha256 not in profiles_sha:
            profiles_sha.append(profile.sha256)
        for table in _snapshot_tables(profile=profile, data_source_id=item.data_source_id, ds=ds):
            source_tables[(table.data_source_id, table.schema_name, table.table)] = table

    target_profile = await _target_profile(
        session=session, tenant_id=subject.tenant_id, plan=plan, sources=sources
    )

    parse_result = parse_and_validate(hocon_content)
    # 拼法**只在 parts.py 里有一份**：门禁这里算的、生成时进指纹的、冻结时重算的
    # 必须是同一个值。三处各写一次 `"|".join(sorted(...))` 就是指纹系统的第一次分叉——
    # 而且分叉的症状是「偶尔报指纹失效」，最难查的那一类。
    source_metadata_sha256 = parts.source_metadata_sha256(profiles_sha)

    scope = _resource_scope(
        subject=subject,
        project_id=project_id,
        plan=plan,
        sources=sources,
        source_tables=source_tables,
        target_profile=target_profile,
    )
    intent = _tool_intent(plan=plan, sources=sources)
    level = _touched_level(scope=scope, plan=plan, source_tables=source_tables)
    env = _resolve_env(plan=plan, sources=sources)

    # **走 `services.policy` 的判定入口，不要直接调 `engines.policy.decide`**。
    # 直接调会漏掉三件只有那一层知道的事：① 生效规则集（内置基线 ⊕ 租户覆盖，
    # 尤其是「谁关掉了哪条基线」）；② 预算默认值（来自 Settings）——
    # 漏了它的后果不是判松，而是 **`decide` 落到 `_ZERO_DEFAULTS`，把上限判成 0**，
    # 于是五项预算条条超限、门禁永远 block，而报错文案是「Plan 不能自行放宽治理预算」，
    # 看起来像 Prompt 写坏了；③ 被忽略的越权覆盖要入账。
    decision = await decide_for_project(
        session,
        tenant_id=subject.tenant_id,
        project_id=project_id,
        actor_id=subject.user_id,
        intent=intent,
        scope=scope,
        env=env,
        level=level,
        now=now,
    )

    return GateContext(
        plan=plan,
        hocon_content=hocon_content,
        parse_result=parse_result,
        source_tables=source_tables,
        target_table=target_profile,
        policy_decision=decision,
        policy_scope=scope,
        env=env,
        level=level,
        data_source_types={ds_id: str(ds.type) for ds_id, ds in sources.items()},
        source_metadata_sha256=source_metadata_sha256,
    )


async def run_gate(
    *,
    session: AsyncSession,
    subject: SubjectContext,
    project_id: int,
    plan: EtlPlanModel,
    hocon_content: str,
    now: datetime,
    autofix_rounds: list[AutofixRound] | tuple[AutofixRound, ...] = (),
    autofix_exhausted: bool = False,
) -> GateOutcome:
    """门禁的 service 入口：建 ctx、跑检查、把轮次信息一并合成结论。"""
    context = await build_gate_context(
        session=session,
        subject=subject,
        project_id=project_id,
        plan=plan,
        hocon_content=hocon_content,
        now=now,
    )
    result = evaluate(context, now=now, autofix_rounds=autofix_rounds, autofix_exhausted=autofix_exhausted)
    return GateOutcome(result=result, context=context)


async def _load_data_sources(
    *, session: AsyncSession, tenant_id: int, ids: set[int]
) -> dict[int, DataSource]:
    """一次取全本次方案涉及的数据源。

    **空集合要提前返回**：``IN ()`` 在多数驱动上是语法错误，
    而 Plan 完全可能一个源端都没有（那是 ``validate_plan`` 该报的事，
    不该在这里变成一条 SQL 报错）。
    """
    if not ids:
        return {}
    rows = (
        await session.scalars(
            select(DataSource).where(DataSource.id.in_(sorted(ids)), DataSource.tenant_id == tenant_id)
        )
    ).all()
    return {row.id: row for row in rows}


def _snapshot_tables(
    *, profile: MetadataProfile | None, data_source_id: int, ds: DataSource | None
) -> list[TableProfile]:
    """档案 → 一组 ``TableProfile``（**按 (data_source_id, schema, table) 打平**）。

    打平的理由：门禁要按「Plan 里写的那个 (库, 表)」去查列，
    而档案是「一个数据源一份快照」的形态。查询侧不该知道快照的内部结构。

    **读 JSON 那一步在 ``profile_view.tables_of``**（与提示词侧共用一次解析）
    ——快照的键名只有一处解释，探查侧改键名时不会出现「门禁认得出、提示词认不出」。
    本函数只做「打平 + 补上门禁特有的两样东西」：数据源类型与脱敏标记。
    """
    if profile is None:
        return []
    ds_type = str(ds.type.value) if ds is not None else ""
    return [
        TableProfile(
            data_source_id=data_source_id,
            data_source_type=ds_type,
            schema_name=table.schema_name,
            table=table.name,
            columns=tuple(
                ProfileColumn(
                    name=column.name,
                    data_type=column.data_type,
                    nullable=column.nullable,
                    sensitive_level=column.sensitive_level,
                    sample_masked=column.masked,
                )
                for column in table.columns
            ),
            profile_sha256=profile.sha256,
            profile_version=profile.version,
            expires_at=profile.expires_at,
            truncated=bool((profile.sampling or {}).get("truncated", False)),
        )
        for table in tables_of(profile)
    ]


def _level_of(raw: Any) -> SensitiveLevel:
    """快照里的分级 → 枚举。认不出来的一律按 ``INTERNAL``。

    **不能**退回 ``PUBLIC``：``PUBLIC`` 等于「随便出域」，
    而认不出来的分级说明「档案里的值我们不认识」——那种情况下保守才是对的。
    """
    try:
        return SensitiveLevel(str(raw))
    except ValueError:
        return SensitiveLevel.INTERNAL


async def _target_profile(
    *, session: AsyncSession, tenant_id: int, plan: EtlPlanModel, sources: dict[int, DataSource]
) -> TableProfile | None:
    """目标表的档案（**很可能为 None，这不是错误**）。

    本项目固定「影子表 + 原子 Swap」：目标表在第一次执行前根本不存在，
    所以「档案里查不到目标表」是常态。``None`` 的语义是「我们对目标端一无所知」，
    由 ``check_mapping`` 去决定该怎么处理（它认 Plan 里声明的 ``target_type``）。
    """
    if not plan.outputs:
        return None
    output = plan.outputs[0]
    ds = sources.get(output.data_source_id)
    profile = await latest_profile(session=session, tenant_id=tenant_id, data_source_id=output.data_source_id)
    for table in _snapshot_tables(profile=profile, data_source_id=output.data_source_id, ds=ds):
        if table.table.lower() == output.table.lower():
            return table
    return None


def _resource_scope(
    *,
    subject: SubjectContext,
    project_id: int,
    plan: EtlPlanModel,
    sources: dict[int, DataSource],
    source_tables: dict[tuple[int, str, str], TableProfile],
    target_profile: TableProfile | None,
) -> ResourceScope:
    """把 Plan 翻译成 PDP 的 ``ResourceScope``。

    ``writes_shadow_table=True`` 且 ``writes_formal_table=False`` 是**如实描述**：
    这份制品本身只写影子表，正式表的变更由 ``target.publish_swap`` 另一次
    工具调用完成，那次调用会在 M3 里被单独判定。把这里写成「写正式表」
    会让每一次生成都命中「直写生产表」的 P0——那不是严格，是把规则说过头了：
    一条永远误报的规则，最后一定会被人关掉。
    """
    reads = tuple(
        ResourceRef(
            data_source_id=item.data_source_id,
            data_source_type=sources[item.data_source_id].type
            if item.data_source_id in sources
            else DataSourceType.MYSQL,
            database=item.schema_name,
            schema=item.schema_name,
            table=item.table,
            env=sources[item.data_source_id].env if item.data_source_id in sources else Env.DEV,
        )
        for item in plan.inputs
    )
    writes = tuple(
        ResourceRef(
            data_source_id=item.data_source_id,
            data_source_type=sources[item.data_source_id].type
            if item.data_source_id in sources
            else DataSourceType.DORIS,
            database=item.database,
            schema="",
            table=item.table,
            env=sources[item.data_source_id].env if item.data_source_id in sources else Env.DEV,
        )
        for item in plan.outputs
    )

    masked_fields = _masked_source_fields(plan)
    columns: list[PolicySensitiveColumn] = []
    for mapping in plan.field_mappings:
        column = _find_column(source_tables, plan=plan, field=mapping.source_field)
        if column is None:
            # 列不在档案里：那是 ``check_schema`` 的活（它会报 source_field_absent）。
            # 这里硬造一个 PUBLIC 的 SensitiveColumn 会**降低**判定强度，
            # 凭空多出一条「这列不敏感」的结论，比缺一条记录危险。
            continue
        kind, level = infer_sensitivity(
            mapping.source_field,
            comment=None,
            declared=column.sensitive_level,
        )
        is_masked = mapping.source_field in masked_fields
        columns.append(
            PolicySensitiveColumn(
                name=mapping.source_field,
                level=level,
                # **翻译，不能 str() 透传**：脱敏引擎的类别词表与 PDP 的不一样
                # （credential/free_text 在计划侧没有对应词），透传会让
                # SensitiveColumn 校验失败、整个生成在最后一步回 422。
                kind=to_policy_kind(kind),
                # 明文 = 「Plan 没给它安排任何脱敏」，这是 Plan 的性质，不是档案的性质
                contains_plaintext=not is_masked,
                masked=is_masked,
                # 源列被搬去目标端 = 出域。同库同表的原地改写才不算，
                # 而本项目固定跨数据源搬运
                egress=True,
            )
        )

    target_database = plan.outputs[0].database if plan.outputs else ""
    return ResourceScope(
        tenant_id=subject.tenant_id,
        project_id=project_id,
        reads=reads,
        writes=writes,
        authorized_databases=frozenset({target_database}) if target_database else frozenset(),
        out_of_scope=(),
        cross_tenant_refs=(),
        unregistered_secret_refs=(),
        writes_formal_table=False,
        writes_shadow_table=True,
        columns=tuple(columns),
    )


def _find_column(
    source_tables: dict[tuple[int, str, str], TableProfile], *, plan: EtlPlanModel, field: str
) -> ProfileColumn | None:
    """在所有源表里找同名列。

    不按 ``PlanInput.id`` 精确归属：``FieldMapping`` 只有 ``source_field``
    没有来源表（前端冻结的契约如此），而多源同名同类型是常态。
    找到第一个同名列即可——**它的分级只会取较高者**，
    所以「找错了一张表」最坏是判定过严，不会漏放。
    """
    best: ProfileColumn | None = None
    best_rank = -1
    for table in source_tables.values():
        column = table.column(field)
        if column is None:
            continue
        rank = _LEVEL_ORDER.get(column.sensitive_level, 0)
        if rank > best_rank:
            best, best_rank = column, rank
    return best


def _masked_source_fields(plan: EtlPlanModel) -> frozenset[str]:
    """被 Plan 安排过脱敏的**源字段**集合。"""
    masked = {m.source_field for m in plan.field_mappings if m.masking is not None}
    masked |= {m.source_field for m in plan.field_mappings if m.transform in _MASK_TRANSFORMS}
    for transform in plan.transforms:
        if transform.kind in _MASK_TRANSFORMS:
            masked |= set(transform.input_fields)
    return frozenset(masked)


def _tool_intent(plan: EtlPlanModel, *, sources: dict[int, DataSource]) -> ToolIntent:
    """本次作业在 PDP 眼里是什么工具调用。

    ``tool=SEATUNNEL_SUBMIT``：生成阶段判定的就是「这份制品将来会以哪个工具执行」。
    之所以不在生成阶段用 ``None``，是因为规则表里大量规则挂在 ``intent.tool`` 上——
    不给工具名等于让这些规则全部落空。
    """
    target = plan.outputs[0].table if plan.outputs else ""
    inputs = "、".join(f"{i.schema_name}.{i.table}" for i in plan.inputs) or "（无源端）"
    return ToolIntent(
        kind="etl_write",
        is_write=True,
        tool=ToolName.SEATUNNEL_SUBMIT,
        description_summary=f"{inputs} → {target}"[:512],
        bypass_approval=False,
        disable_audit=False,
        export_plaintext_sensitive=False,
        cross_tenant=False,
        # 影子表 + Swap：制品自带回滚路径（不 Swap 即放弃）。
        # 这一条让「没有回滚方案」的规则不被误命中——它本来就是我们的默认交付方式
        has_rollback_plan=True,
        budget=BudgetRequest(
            max_read_rows=plan.budget.max_read_rows,
            max_write_bytes=plan.budget.max_write_bytes,
            max_duration_sec=plan.budget.max_duration_sec,
            max_amplification_ratio=plan.budget.max_amplification_ratio,
            max_reject_rate=plan.budget.max_reject_rate,
        ),
    )


def _touched_level(
    *,
    scope: ResourceScope,
    plan: EtlPlanModel,
    source_tables: dict[tuple[int, str, str], TableProfile],
) -> SensitiveLevel:
    """本次作业触碰的最高敏感级别。

    取**读到的列的级别**与数据源自身标注的级别中较高者：
    一个标注为 ``CONFIDENTIAL`` 的数据源，即使这次只搬了一列 ``id``，
    也仍然是「碰了机密数据源」。
    """
    top = SensitiveLevel.PUBLIC
    for column in scope.columns:
        level = column.level if isinstance(column.level, SensitiveLevel) else _level_of(column.level)
        if _LEVEL_ORDER[level] > _LEVEL_ORDER[top]:
            top = level
    for table in source_tables.values():
        # 档案里没有「数据源级别」，用表内列的最高级别近似——比引入一个新字段老实
        for column in table.columns:
            if _LEVEL_ORDER[column.sensitive_level] > _LEVEL_ORDER[top]:
                top = column.sensitive_level
    return top


def _resolve_env(*, plan: EtlPlanModel, sources: dict[int, DataSource]) -> Env:
    """环境取**源数据源**的 env：判定的是「动的是哪套环境的数据」。

    多源时常态是混的（dev 读、prod 写），那种情况取**最高**的那个
    （``prod`` 的判定显著更严，取最高而不是取第一个）。
    """
    envs = [sources[i.data_source_id].env for i in plan.inputs if i.data_source_id in sources] + [
        sources[o.data_source_id].env for o in plan.outputs if o.data_source_id in sources
    ]
    if not envs:
        return Env.DEV
    return max(envs, key=lambda env: {Env.DEV: 0, Env.STAGING: 1, Env.PROD: 2}[env])


def highest_risk(result: GateResult) -> RiskLevel | None:
    """结论里出现过的最高风险等级（没有 P0/P1 时返回 None）。

    给 Interface 层做状态流转用：``block`` 时 pipeline 该落到 ``gate_failed``
    还是 ``pending_approval``，取决于是不是策略级阻断。
    """
    if result.has_policy_block:
        return RiskLevel.P0
    if any(f.severity == "block" for f in result.findings):
        return RiskLevel.P1
    return None


__all__ = ["GateOutcome", "build_gate_context", "highest_risk", "run_gate"]
