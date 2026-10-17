"""制品与版本的门面（``api/pipeline_versions.py`` 与 ``api/pipelines.py`` 调的就是这里）。

十个入口，按「读 / 改 / 冻结 / 比对」四类：

    读    get_plan / get_hocon / get_gate / get_version / list_versions / get_pipeline / list_pipelines
    改    run_autofix
    冻结  freeze_version
    比对  diff_versions

## 三条贯穿本模块的规矩

1. **候选行按 id 可读，但不出现在版本列表里**。前端在冻结前就要按 id 拉 Plan/HOCON/门禁
   （``loadArtifacts(versionId)``），所以草案必须可寻址；而 ``list_versions`` 只列冻结版本——
   「v3」这个称呼一旦出现在版本历史里，就意味着它能被审批、被执行、被回滚，而草案三样都不是。
2. **``get_version`` 拒绝草案**。``PipelineVersionSummary.frozen_at`` 是必填（前端 DTO 已冻结），
   草案没有冻结时间，硬塞一个默认值等于给草案伪造一个「已冻结」的身份。
   草案的完整内容走 ``/plan`` + ``/hocon`` + ``/gate`` 三个接口，那正是 Studio 需要的。
3. **指纹以库为准，不以内存为准**。``freeze_version`` 从**落库的四个制品行**重算指纹
   （``parts.parts_from_rows``），跟候选行上的 ``sha256`` 对不上就拒绝冻结。
   这是「审批看到的」与「执行拿到的」是同一份东西的**唯一**保证。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.errors import ConflictError, RoleRequiredError, ValidationError
from engines.autofix import arun_autofix_loop
from engines.autofix.loop import AutofixRequest
from engines.diff import version_diff
from engines.gate.gate import evaluate
from engines.gate.types import GateResult
from engines.hocon import (
    PARSER_VERSION,
    RenderContext,
    parse_tokens,
    render_hocon,
    secret_token_problems,
)
from engines.plan.schema import EtlPlanModel
from engines.policy import can_manage_pipeline, can_read_pipeline
from models.datasource import DataSource, MetadataProfile
from models.enums import PipelineStatus
from models.pipeline import Pipeline, PipelineVersion
from models.tenant import AppUser
from schemas.auth import SubjectContext
from schemas.pipeline import (
    EtlPlanRead,
    HoconArtifactRead,
    PipelineRead,
    PlanValidationError,
)
from schemas.version import (
    FreezeArtifactManifest,
    GenerationContext,
    ManifestHocon,
    ManifestMetadataProfile,
    ManifestPlan,
    ManifestQualityContract,
    PipelineVersionDetail,
    PipelineVersionSummary,
    VersionDiff,
    VersionDiffEntry,
)
from services.pipeline import parts, repository
from services.pipeline.gating import build_gate_context
from services.pipeline.repair import make_repair_fn

log = structlog.get_logger(__name__)

#: 自动修复的轮次上限（be-11 §6.6 约束 1）。超了就停手交给人——
#: 无限修下去最坏的结果是「模型把方案改到能过门禁，但它已经不是用户要的东西」。
_AUTOFIX_MAX_ROUNDS = 3


# ---------------------------------------------------------------- 权限


async def require_roles(session: AsyncSession, ctx: SubjectContext, project_id: int, *, write: bool) -> None:
    """服务端闭合的能力位判定（CLAUDE.md §5.8：不依赖前端置灰按钮）。

    读与写分开：``can_read_pipeline`` 允许项目里任何职责槽（checker 不看方案就没法批），
    ``can_manage_pipeline`` 不含 checker / auditor（能改制品的人不该同时是核证制品的人）。

    公开（不带下划线）是因为 ``generation`` 也要用它：同一个判定若在两个文件里各写一遍，
    「读接口放行、写接口拦截」这类差异就会在某次改动中悄悄出现。
    """
    from services.project import active_roles

    roles = await active_roles(session, project_id=project_id, user_id=ctx.user_id)
    allowed = can_manage_pipeline(roles) if write else can_read_pipeline(roles)
    if not allowed:
        needed = "maker / operator" if write else "本项目任一职责槽"
        raise RoleRequiredError(f"该操作需要 {needed}")


# ---------------------------------------------------------------- 读：流程


async def list_pipelines(
    *,
    session: AsyncSession,
    ctx: SubjectContext,
    project_id: int | None,
    page: int,
    page_size: int,
) -> tuple[list[PipelineRead], int]:
    """流程列表。``project_id`` 为空时列出该租户下**全部**项目的流程。

    **不做按角色过滤**：读权限已经由「必须是本项目职责槽」这一层保证，
    再按角色藏行会让「为什么我看不到这条流程」变成一道需要查三张表才能回答的题。
    ``project_id`` 为空时**跳过项目级角色检查**——没有项目，检查没有对象；
    租户隔离仍然由 repository 的 ``tenant_id`` 条件兜住。
    """
    rows, total = await repository.list_pipelines(
        session=session,
        tenant_id=ctx.tenant_id,
        project_id=project_id,
        offset=(page - 1) * page_size,
        limit=page_size,
    )
    if project_id is not None:
        await require_roles(session, ctx, project_id, write=False)
    return [_pipeline_read(row) for row in rows], total


async def get_pipeline(*, session: AsyncSession, ctx: SubjectContext, pipeline_id: int) -> PipelineRead:
    row = await repository.load_pipeline(session=session, tenant_id=ctx.tenant_id, pipeline_id=pipeline_id)
    await require_roles(session, ctx, row.project_id, write=False)
    return _pipeline_read(row)


# ---------------------------------------------------------------- 读：版本


async def list_versions(
    *, session: AsyncSession, ctx: SubjectContext, pipeline_id: int
) -> list[PipelineVersionSummary]:
    """**只列冻结版本**（见模块 docstring 第 1 条）。

    ``total`` 不在本函数的返回值里：前端要的 ``Page.total`` 由路由按本页条数兜底。
    这里不额外做一次 ``count`` 查询——版本列表天然按页短（一个流程几十版顶天），
    而多一次 count 就是多一次「计数与列表用了不同过滤条件」的机会。
    """
    pipeline = await repository.load_pipeline(
        session=session, tenant_id=ctx.tenant_id, pipeline_id=pipeline_id
    )
    await require_roles(session, ctx, pipeline.project_id, write=False)
    rows = await repository.list_versions(
        session=session, tenant_id=ctx.tenant_id, pipeline_id=pipeline_id, frozen_only=True
    )
    names = await _display_names(session=session, versions=rows)
    return [_summary(row, names) for row in rows]


async def get_version(
    *, session: AsyncSession, ctx: SubjectContext, version_id: int
) -> PipelineVersionDetail:
    """版本详情（含冻结清单与两份只读制品）。**草案走不通这条路**，理由见模块 docstring。"""
    version = await repository.load_version(session=session, tenant_id=ctx.tenant_id, version_id=version_id)
    pipeline = await repository.load_pipeline(
        session=session, tenant_id=ctx.tenant_id, pipeline_id=version.pipeline_id
    )
    await require_roles(session, ctx, pipeline.project_id, write=False)
    if version.frozen_at is None:
        raise ConflictError("这是尚未冻结的草案：它还没有冻结时间与冻结人，走 /plan、/hocon、/gate 读内容。")
    plan_row, hocon_row, quality_row, supervision_row = await repository.artifact_rows(
        session=session, version=version
    )
    profile = await _profile_row(session=session, version=version)
    names = await _display_names(session=session, versions=[version])
    return PipelineVersionDetail(
        **_summary(version, names).model_dump(),
        manifest=_manifest(
            version=version,
            plan_row=plan_row,
            hocon_row=hocon_row,
            quality_row=quality_row,
            supervision_row=supervision_row,
            profile=profile,
        ),
        plan=_plan_read(plan_row),
        hocon=_hocon_read(hocon_row),
    )


async def get_plan(*, session: AsyncSession, ctx: SubjectContext, version_id: int) -> EtlPlanRead:
    """读 Plan。**草案也能读**——Studio 就是靠它把映射表画出来的。"""
    _version, plan_row, _hocon, _q, _s = await _load_bundle(session=session, ctx=ctx, version_id=version_id)
    return _plan_read(plan_row)


async def get_hocon(*, session: AsyncSession, ctx: SubjectContext, version_id: int) -> HoconArtifactRead:
    _version, _plan, hocon_row, _q, _s = await _load_bundle(session=session, ctx=ctx, version_id=version_id)
    return _hocon_read(hocon_row)


async def get_gate(*, session: AsyncSession, ctx: SubjectContext, version_id: int) -> GateResult:
    """读**落库的**门禁结论，不重跑。

    重跑会得到一个「现在」的结论，而前端要的是「这份制品当时被判成了什么」——
    策略表改过之后，两者会不一样，而把后者当成前者，等于让用户看到一份
    与他审批时不同的结论。策略变更后要重新判定是 M3 的 Prepare 阶段的事。
    """
    _version, _plan, hocon_row, _q, _s = await _load_bundle(session=session, ctx=ctx, version_id=version_id)
    stored = (hocon_row.validation_result or {}).get("gate")
    if not stored:
        raise ConflictError("这份制品没有落库的门禁结论（可能是早于门禁落库的制品）。请重新生成一次。")
    return GateResult.model_validate(stored)


# ---------------------------------------------------------------- 自动修复


async def run_autofix(*, session: AsyncSession, ctx: SubjectContext, version_id: int) -> GateResult:
    """跑有限自动修复，**把结果落回候选行**，返回修复后的门禁结论。

    只对**草案**开放：冻结版本是不可变的，对它跑修复要么改历史、要么产出一份
    谁也审批不到的制品。用户想改进已冻结的版本，正确路径是基于它再生成一个草案。
    """
    version, plan_row, hocon_row, _q, _s = await _load_bundle(
        session=session, ctx=ctx, version_id=version_id, write=True
    )
    if version.frozen_at is not None:
        raise ConflictError("已冻结的版本不可再修复；请基于它重新生成一份草案。")

    plan = _plan_model(plan_row)
    hocon_content = hocon_row.content
    pipeline = await repository.load_pipeline(
        session=session, tenant_id=ctx.tenant_id, pipeline_id=version.pipeline_id
    )
    now = datetime.now(UTC)

    context = await build_gate_context(
        session=session,
        subject=ctx,
        project_id=pipeline.project_id,
        plan=plan,
        hocon_content=hocon_content,
        now=now,
    )
    render_ctx = RenderContext(
        data_source_types=dict(context.data_source_types),
        parallelism=int((version.generation_context or {}).get("parallelism", 1) or 1),
    )

    def recheck(candidate: EtlPlanModel, candidate_hocon: str) -> GateResult:
        """复检：**纯函数**，不碰库。

        做法是拿本次判定用的 ctx 换掉 ``plan`` 与 ``hocon_content`` 两个字段——
        档案、策略结论、数据源类型都用同一份。若这里重新去建 ctx，
        就要在同步回调里 ``await``（做不到），而且会在 3 轮修复中查 3 次库、
        拿到 3 个可能不同的档案快照，让「为什么第 2 轮结论变了」无从解释。
        """
        from engines.hocon import parse_and_validate

        refreshed = context.model_copy(
            update={
                "plan": candidate,
                "hocon_content": candidate_hocon,
                "parse_result": parse_and_validate(candidate_hocon),
            }
        )
        return evaluate(refreshed, now=now)

    outcome = await arun_autofix_loop(
        request=AutofixRequest(
            round_no=1,
            plan=plan,
            hocon_content=hocon_content,
            findings=list((await _gate_result(session=session, ctx=ctx, version=version)).findings),
        ),
        repair=make_repair_fn(
            requirement=pipeline.description or pipeline.name,
            render_ctx=render_ctx,
            request_id=f"{ctx.request_id}:autofix:{version_id}",
        ),
        recheck=recheck,
        max_rounds=_AUTOFIX_MAX_ROUNDS,
        now_factory=lambda: now,
    )

    if outcome.rounds_used:
        await _persist_repaired(
            session=session,
            ctx=ctx,
            pipeline=pipeline,
            version=version,
            outcome=outcome,
            render_ctx=render_ctx,
            now=now,
        )
    # 事务边界在这里收口（依赖注入只借连接、不提交）。修复回合没落库时这一句也没害处：
    # 空事务提交是 no-op。**不提交的后果比「多一次提交」重得多**：门禁结论会显示成修好了，
    # 而用户一刷新，制品又变回修复前那份——他会以为修复是「只在这次响应里生效」的。
    await session.commit()
    return outcome.result


# ---------------------------------------------------------------- 冻结


async def _assert_tokens_resolvable(
    *,
    session: AsyncSession,
    ctx: SubjectContext,
    content: str,
    version_id: int,
) -> None:
    """冻结前确认制品里的每一枚密钥令牌**到时候解得开**。

    **不要用 ``assert_no_unresolved_tokens`` 做这件事**：那道闸问的是
    「执行前替换了没有」，令牌必须一个不剩；而冻结发生在执行之前很久，
    制品本来就该带着令牌（令牌正是「口令不进制品」的手段）。
    早先这里用错了闸，后果是**每一份合法制品都冻不了**，报错文案还写着
    「仍有未替换的密钥令牌」——读起来像渲染器漏了替换，实际是拿错了判据。

    这里判两件事：① 字段名在白名单里（制品被人手改过 / 渲染器写错字）；
    ② 令牌指向的数据源在本租户里存在、未停用、且配了密钥引用
    （没配 = 执行时取不到口令，同样是「审批通过也跑不起来」）。
    """
    tokens = parse_tokens(content)
    resolvable: frozenset[int] = frozenset()
    if tokens:
        ds_ids = {token.data_source_id for token in tokens}
        rows = (
            await session.execute(
                select(DataSource.id, DataSource.status, DataSource.secret_ref_id).where(
                    DataSource.tenant_id == ctx.tenant_id, DataSource.id.in_(ds_ids)
                )
            )
        ).all()
        # 「可解析」= 存在 + 启用 + 有密钥引用。三者缺一条，执行期都拿不到口令
        resolvable = frozenset(
            int(row.id) for row in rows if row.status == "active" and row.secret_ref_id is not None
        )

    problems = secret_token_problems(content, resolvable_data_source_ids=resolvable)
    if problems:
        raise ConflictError(
            f"pipeline_version#{version_id} 的密钥令牌不可解析：" + "；".join(problems)
        )


async def freeze_version(
    *,
    session: AsyncSession,
    ctx: SubjectContext,
    version_id: int,
    note: str,
) -> PipelineVersionDetail:
    """把候选行冻结成不可变版本。

    六道闸，顺序不能换：

    1. **必须在草案上冻结**（``frozen_at IS NULL``）——已冻结的再冻一次没有语义；
    2. **指纹必须与落库制品一致**——不一致说明制品在生成之后被动过，此时冻结等于
       把「审批看到的 A」与「执行拿到的 B」绑在一起；
    3. **HOCON 里的密钥令牌必须都解得开**——令牌是「口令不进制品」的手段，
       冻结则意味着这份配置即将被审批、被执行，所以这里要确认每一枚令牌指向的数据源
       还在、还有凭据（详见 ``_assert_tokens_resolvable``）；
    4. **门禁不得有阻塞项**——block 就是「不许上生产」，冻结是上生产的第一步；
    5. **写入方必须是当前用户**（``frozen_by``），四眼的后置条件靠它判定；
    6. 冻结后 ``pipeline.current_version_id`` 指向它、状态转 ``version_frozen``。
    """
    version, plan_row, hocon_row, quality_row, supervision_row = await _load_bundle(
        session=session, ctx=ctx, version_id=version_id, write=True
    )
    pipeline = await repository.load_pipeline(
        session=session, tenant_id=ctx.tenant_id, pipeline_id=version.pipeline_id
    )
    now = datetime.now(UTC)

    if version.frozen_at is not None:
        raise ConflictError("该版本已经冻结过了；冻结是不可逆的，要改请重新生成一份草案。")

    stored_parts = parts.parts_from_rows(
        plan_row=plan_row,
        hocon_row=hocon_row,
        quality_row=quality_row,
        supervision_row=supervision_row,
        source_metadata_sha256=await _source_metadata_sha256(
            session=session, tenant_id=ctx.tenant_id, plan=_plan_model(plan_row)
        ),
    )
    if not parts.fingerprint_matches(stored=version.sha256, parts=stored_parts):
        raise ConflictError("制品指纹与候选版本记录的不一致：制品在生成之后被改动过。拒绝冻结，请重新生成。")

    await _assert_tokens_resolvable(
        session=session, ctx=ctx, content=hocon_row.content, version_id=version.id
    )

    gate = await get_gate(session=session, ctx=ctx, version_id=version_id)
    if any(f.severity == "block" for f in gate.findings):
        raise ValidationError(
            f"门禁未通过（{len(gate.blocking)} 项阻塞），不能冻结。请先修复或跑一次自动修复。"
        )

    version.frozen_at = now
    version.frozen_by = ctx.user_id
    version.note = note
    await session.flush()
    await repository.set_current_version(
        session=session,
        pipeline_id=pipeline.id,
        version_id=version.id,
        status=PipelineStatus.VERSION_FROZEN,
    )

    log.info(
        "pipeline_version_frozen",
        pipeline_id=pipeline.id,
        version_id=version.id,
        version_no=version.version_no,
        tenant_id=ctx.tenant_id,
    )
    # **冻结必须真的落库才算冻结**：`frozen_at` 是一个不可逆的事实，
    # 不提交的话响应里带着「已冻结」，库里还是草案，下一次生成会心安理得地
    # 复用/覆盖这个候选——「不可变版本」这套保证就成了一句空话。
    await session.commit()
    return await get_version(session=session, ctx=ctx, version_id=version.id)


# ---------------------------------------------------------------- 比对


async def diff_versions(
    *,
    session: AsyncSession,
    ctx: SubjectContext,
    base_version_id: int,
    target_version_id: int,
) -> VersionDiff:
    """两份版本的差异（前端版本对比弹窗）。

    两侧各自重算 ``ArtifactParts`` 再比——**不**比数据库行：
    行里的 ``id`` / ``created_at`` 每版都不同，比行会让每一版都显示「全变了」。
    ``parts_from_rows`` 里已经剥掉了这些排除项（``engines/fingerprint/parts.py``）。
    """
    if base_version_id == target_version_id:
        # 自己跟自己比：返回空 diff 而不是报错。前端在「选中的两版恰好相同」时
        # 会走到这里，报错会让一个纯粹的界面状态变成一条红色提示
        return VersionDiff(entries=[], base_version_id=base_version_id, target_version_id=target_version_id)

    base = await _bundle_for_diff(session=session, ctx=ctx, version_id=base_version_id)
    target = await _bundle_for_diff(session=session, ctx=ctx, version_id=target_version_id)
    result = version_diff(base.parts, target.parts)
    return VersionDiff(
        entries=[
            VersionDiffEntry(
                path=entry.path,
                kind=entry.kind,
                before=entry.before,
                after=entry.after,
            )
            for entry in result.entries
        ],
        base_version_id=base_version_id,
        target_version_id=target_version_id,
    )


# ---------------------------------------------------------------- 内部


class _Bundle:
    """一次取齐「版本行 + 四份制品 + 档案」，省掉每个入口各写一遍。"""

    __slots__ = ("hocon_row", "parts", "plan_row", "profile", "quality_row", "supervision_row", "version")

    def __init__(self, **kwargs: Any) -> None:
        for key, value in kwargs.items():
            setattr(self, key, value)


async def _load_bundle(
    *, session: AsyncSession, ctx: SubjectContext, version_id: int, write: bool = False
) -> tuple[PipelineVersion, Any, Any, Any, Any]:
    version = await repository.load_version(session=session, tenant_id=ctx.tenant_id, version_id=version_id)
    pipeline = await repository.load_pipeline(
        session=session, tenant_id=ctx.tenant_id, pipeline_id=version.pipeline_id
    )
    await require_roles(session, ctx, pipeline.project_id, write=write)
    rows = await repository.artifact_rows(session=session, version=version)
    return (version, *rows)


async def _bundle_for_diff(*, session: AsyncSession, ctx: SubjectContext, version_id: int) -> _Bundle:
    version, plan_row, hocon_row, quality_row, supervision_row = await _load_bundle(
        session=session, ctx=ctx, version_id=version_id
    )
    profile = await _profile_row(session=session, version=version)
    return _Bundle(
        version=version,
        plan_row=plan_row,
        hocon_row=hocon_row,
        quality_row=quality_row,
        supervision_row=supervision_row,
        profile=profile,
        parts=parts.parts_from_rows(
            plan_row=plan_row,
            hocon_row=hocon_row,
            quality_row=quality_row,
            supervision_row=supervision_row,
            source_metadata_sha256=await _source_metadata_sha256(
                session=session, tenant_id=ctx.tenant_id, plan=_plan_model(plan_row)
            ),
        ),
    )


async def _profile_row(*, session: AsyncSession, version: PipelineVersion) -> MetadataProfile | None:
    if version.source_metadata_profile_id is None:
        return None
    return await session.get(MetadataProfile, version.source_metadata_profile_id)


async def _source_metadata_sha256(*, session: AsyncSession, tenant_id: int, plan: EtlPlanModel) -> str:
    """算指纹第五部分：按 Plan 读到的**每个源端**取当前最新就绪档案的 sha256，拼起来。

    为什么不用 ``version.source_metadata_profile_id`` 那一行：那个外键只指向**第一个**
    源端（既有表结构只有一个档案外键），而跨源 join 的方案要能被**任一**源表的结构变化
    击穿。所以这里按 Plan 重查一遍（规则见 ``parts.source_metadata_sha256``）。

    代价是「生成到冻结之间重新探查过源表」会让指纹对不上、冻结被拒。这是要的行为：
    档案变了就说明门禁当初看的那份结构已经不在了，该重新生成。
    """
    digests = await repository.profile_digests(
        session=session,
        tenant_id=tenant_id,
        data_source_ids=[item.data_source_id for item in plan.inputs],
    )
    return parts.source_metadata_sha256(digests.values())


async def source_metadata_digest(*, session: AsyncSession, tenant_id: int, plan: EtlPlanModel) -> str:
    """指纹第五部分（源端档案摘要）的**对外入口**。

    be-13 的 Prepare 要重算 ``input_fingerprint``，而那份指纹的第一个输入就是本函数的值；
    门禁建 ctx 时算的、冻结时重算的、Prepare 与 Commit 校验时算的，必须是**同一个串**。
    三处各自去 ``repository.profile_digests`` 拼一遍，就成了三份实现——
    而分叉的症状是「刚发起的准备单立刻报指纹不符」，最难查的那一类。
    """
    return await _source_metadata_sha256(session=session, tenant_id=tenant_id, plan=plan)


async def _display_names(*, session: AsyncSession, versions: list[PipelineVersion]) -> dict[int, str]:
    """批量取冻结人显示名。

    一次查完而不是逐行查：版本列表有 20 行时，逐行查就是 20 次往返。
    ``frozen_by`` 为空（草案）不进查询集合——``IN ()`` 会让部分驱动直接报语法错。
    """
    ids = sorted({v.frozen_by for v in versions if v.frozen_by is not None})
    if not ids:
        return {}
    rows = await session.execute(select(AppUser.id, AppUser.display_name).where(AppUser.id.in_(ids)))
    return {row[0]: row[1] for row in rows}


def _summary(version: PipelineVersion, names: dict[int, str]) -> PipelineVersionSummary:
    """版本行 → 列表摘要。

    ``frozen_at`` 在这里**断言非空**：调用方（``list_versions`` / ``get_version``）
    都已排除草案。少一个断言，草案就会以 ``frozen_at=None`` 撞在 Pydantic 校验上，
    报出一条与真实原因（读了草案列表）毫无关系的错误。
    """
    if version.frozen_at is None:  # pragma: no cover - 由调用方保证
        raise ConflictError("草案没有冻结时间，不能作为版本摘要返回。")
    return PipelineVersionSummary(
        id=version.id,
        version_no=version.version_no,
        sha256=version.sha256,
        # DB 列叫 note，对外一律叫 status_note（前端口径）
        status_note=version.note or "",
        frozen_at=version.frozen_at,
        frozen_by_display_name=names.get(version.frozen_by or -1),
        source_metadata_profile_version=None,
        generation_context=GenerationContext(**(version.generation_context or {})),
    )


def _manifest(
    *,
    version: PipelineVersion,
    plan_row: Any,
    hocon_row: Any,
    quality_row: Any,
    supervision_row: Any,
    profile: MetadataProfile | None,
) -> FreezeArtifactManifest:
    """冻结清单：**冻结那一刻到底冻了什么**的对外承诺。五项都必填。

    ``metadata_profile`` 在没绑档案时回退成「版本 0 / 空指纹」会违背
    「五项都必填」的初衷——那等于说「这份制品绑了档案」而其实没有。
    所以这里在没有档案时**直接报错**：一个读不出清单的版本不该出现在冻结列表里。
    """
    if profile is None:
        raise ConflictError(f"版本 {version.id} 没有绑定的元数据档案，冻结清单不完整。")
    return FreezeArtifactManifest(
        plan=ManifestPlan(schema_version=plan_row.schema_version, valid=plan_row.valid),
        hocon=ManifestHocon(sha256=hocon_row.sha256, parser_version=hocon_row.parser_version),
        quality_contract=ManifestQualityContract(
            rule_count=len(quality_row.rules or []),
            reject_rate_budget=float(quality_row.reject_rate_budget),
        ),
        supervision=repository.budget_of(supervision_row),
        metadata_profile=ManifestMetadataProfile(
            version=profile.version, sha256=profile.sha256, expires_at=profile.expires_at
        ),
    )


def _plan_read(plan_row: Any) -> EtlPlanRead:
    """``etl_plan`` 行 → 只读 DTO。

    ``validation_errors`` 要**显式映射**：库里存的是 ``PlanIssue``（``path/code/message/severity``），
    前端要的是 ``{path, reason}``。把 code 拼进 reason 是最省的守法——
    丢掉 code 会让用户看到一句没有归类的问题，而 code 正是自动修复与统计的键。
    """
    return EtlPlanRead(
        id=plan_row.id,
        schema_version=plan_row.schema_version,
        plan=EtlPlanModel.model_validate(plan_row.plan),
        valid=plan_row.valid,
        validation_errors=[
            PlanValidationError(
                path=str(item.get("path") or "/")[:512],
                reason=f"[{item.get('code', 'unknown')}] {item.get('message', '')}"[:1024],
            )
            for item in (plan_row.validation_errors or [])
        ],
    )


def _hocon_read(hocon_row: Any) -> HoconArtifactRead:
    return HoconArtifactRead(
        id=hocon_row.id,
        content=hocon_row.content,
        sha256=hocon_row.sha256,
        parser_version=hocon_row.parser_version,
        validation_result=dict(hocon_row.validation_result or {}),
    )


def _plan_model(plan_row: Any) -> EtlPlanModel:
    return EtlPlanModel.model_validate(plan_row.plan)


def _pipeline_read(row: Pipeline) -> PipelineRead:
    return PipelineRead(
        id=row.id,
        project_id=row.project_id,
        name=row.name,
        description=row.description or "",
        status=row.status,
        current_version_id=row.current_version_id,
        source_data_source_id=row.source_data_source_id,
        target_data_source_id=row.target_data_source_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


async def _gate_result(*, session: AsyncSession, ctx: SubjectContext, version: PipelineVersion) -> GateResult:
    return await get_gate(session=session, ctx=ctx, version_id=version.id)


async def _persist_repaired(
    *,
    session: AsyncSession,
    ctx: SubjectContext,
    pipeline: Pipeline,
    version: PipelineVersion,
    outcome: Any,
    render_ctx: RenderContext,
    now: datetime,
) -> None:
    """把修复后的制品落成**新的一组行**，并让候选行指向它们。

    自动修复的产物同样是不可变制品（CLAUDE.md §5.4）：原地改老的
    ``etl_plan`` 行会让「第 1 轮门禁看到的是哪份 Plan」永远问不出来。
    """
    from engines.plan.validate import validate_plan
    from services.pipeline import gating

    plan = outcome.plan
    issues = validate_plan(plan)
    rendered = render_hocon(plan, render_ctx)
    hocon_sha = _sha256_text(rendered.content)

    # 修复后的门禁结论要重新算一遍并存下来：``outcome.result`` 是从磁盘上的
    # 旧制品推出来的（recheck 用的是内存 ctx），而落库的那份必须以**新制品**为输入
    fresh = await gating.run_gate(
        session=session,
        subject=ctx,
        project_id=pipeline.project_id,
        plan=plan,
        hocon_content=rendered.content,
        now=now,
        autofix_rounds=outcome.result.autofix_rounds,
        autofix_exhausted=outcome.result.autofix_exhausted,
    )

    ids = await repository.save_artifacts(
        session=session,
        tenant_id=ctx.tenant_id,
        pipeline_id=pipeline.id,
        plan=plan,
        plan_valid=not [i for i in issues if i.severity == "block"],
        validation_errors=[i.model_dump(mode="json") for i in issues],
        hocon_content=rendered.content,
        hocon_sha256=hocon_sha,
        # 记**解析器**版本（与生成路径同一个来源）：两处写法不同的话，
        # 同一张表里会出现两种格式的 ``parser_version``，
        # 而这一列的用途正是「分辨是哪一端变了」。
        parser_version=PARSER_VERSION,
        # 渲染段 ⊕ 解析/门禁段——与生成路径的形状完全一致（见 ``gating.render_meta``）
        hocon_meta={**gating.render_meta(rendered), **fresh.artifact_meta()},
    )
    fingerprint = parts.fingerprint(
        parts.parts_from_plan(
            plan=plan,
            hocon_content=rendered.content,
            pipeline_id=pipeline.id,
            source_metadata_sha256=await _source_metadata_sha256(
                session=session, tenant_id=ctx.tenant_id, plan=plan
            ),
        )
    )
    await repository.get_or_create_candidate(
        session=session,
        tenant_id=ctx.tenant_id,
        project_id=pipeline.project_id,
        pipeline_id=pipeline.id,
        ids=ids,
        sha256=fingerprint,
        generation_context={
            **(version.generation_context or {}),
            "autofix_rounds": outcome.rounds_used,
            "parallelism": render_ctx.parallelism,
        },
        now=now,
    )


def _sha256_text(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


__all__ = [
    "diff_versions",
    "freeze_version",
    "get_gate",
    "get_hocon",
    "get_pipeline",
    "get_plan",
    "get_version",
    "list_pipelines",
    "list_versions",
    "run_autofix",
]
