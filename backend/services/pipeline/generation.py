"""生成会话的 service 层（be-09）：把 HTTP 请求接到 LangGraph 上，并把每一轮的产物落库。

## 本模块只做四件事

1. **建流程**：第一次生成时先落一行 ``pipeline``（后面所有制品都挂在它下面）；
2. **凑初始 state**：把 ``pipeline_id`` 与数据源的类型/环境**查库**填进去——
   ``engines`` 不许查库，图里也就没有「自己去取数据源」的能力，
   凡是图需要而库里才有的东西，都得在这里先放好；
3. **驱动一轮**：调 ``workflow.graph`` 的门面（``start`` / ``resume`` / ``snapshot`` / ``abort``），
   拿回 ``TurnResult``；
4. **把这一轮的产物落成不可变制品**，并让候选版本行指向它们。

## 落库这一步为什么在 service 而不在节点里

节点内落库会让「这一轮的 Plan 到底存没存下来」取决于图执行到哪一步；
而中断（澄清）是**正常路径**——图会在 ``param_check`` 处停下等人回答。
如果落库写在节点里，用户看到澄清问卷时库里可能是「有 conversations、没有 plan」这种半截状态。
放这里之后，规则变成一句话：**一轮结束（正常结束或中断），把手上有的东西整体落一次**。

## 线程与锁

``thread_id`` 由 ``pipeline_id`` 推出（``workflow.state.thread_id_for``）而不是随机生成：
随机 id 一旦丢失（页面刷新、接口超时），这次生成就再也找不回来了——
而「刷新页面能接着答」正是用 Checkpoint 的全部理由。
并发生成由 ``workflow.graph`` 内部的红锁（``gen:lock:{pipeline_id}``）挡，
这里不重复加锁：两处锁的 TTL 不一致时，先失效的那把会变成假的安全感。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from core.errors import NotFoundError, ValidationError
from engines.plan.draft import DraftColumn, DraftSourceTable, DraftTable, DraftTargetTable
from engines.plan.schema import EtlPlanModel, RuntimeBudget
from engines.plan.validate import validate_plan
from models.enums import PipelineStatus
from schemas.auth import SubjectContext
from schemas.pipeline import AnswerInput, WorkflowSnapshot
from services import policy as policy_service
from services.pipeline import parts, profile_view, repository
from services.pipeline.artifacts import require_roles

log = structlog.get_logger(__name__)

#: 流程名的长度上限（``pipeline.name`` 是 String(128)）。
_NAME_LIMIT = 64


# ---------------------------------------------------------------- 对外入口


async def start_generation(
    *,
    session: AsyncSession,
    ctx: SubjectContext,
    project_id: int,
    requirement: str,
    source_data_source_id: int,
    target_data_source_id: int,
) -> WorkflowSnapshot:
    """发起一次生成。

    **先建流程再入图**，顺序不能反：图的每个节点都要 ``pipeline_id`` 才能落制品，
    而中间中断时前端需要 ``thread_id`` 去接着轮询——两者的来源都是这一行 pipeline。
    万一图第一步就失败，留下的是一行没有版本的 pipeline（状态 ``draft``），
    这在界面上是「一条没生成出东西的流程」，比「什么都没有但报了一次错」好排查。
    """
    await require_roles(session, ctx, project_id, write=True)
    sources = await _load_sources(
        session=session,
        ctx=ctx,
        ids=[source_data_source_id, target_data_source_id],
        project_id=project_id,
    )

    now = datetime.now(UTC)
    name = _name_from(requirement)
    pipeline = await repository.create_pipeline(
        session=session,
        tenant_id=ctx.tenant_id,
        project_id=project_id,
        name=name,
        description=requirement,
        source_data_source_id=source_data_source_id,
        target_data_source_id=target_data_source_id,
        created_by=ctx.user_id,
    )
    await repository.append_message(
        session=session,
        tenant_id=ctx.tenant_id,
        project_id=project_id,
        pipeline_id=pipeline.id,
        role="user",
        content=requirement,
        message_kind="requirement",
        meta={"thread_id": _thread_id(pipeline.id)},
        created_by=ctx.user_id,
    )

    from workflow import start

    turn = await start(
        _graph(),
        await _initial_state(
            session=session,
            ctx=ctx,
            pipeline=pipeline,
            project_id=project_id,
            requirement=requirement,
            sources=sources,
            now=now,
        ),
        session=session,
        ctx=ctx,
    )
    await _persist_turn(session=session, ctx=ctx, pipeline=pipeline, turn=turn, now=now)
    # 事务边界归 service（``core.db.get_session`` 的 docstring：依赖注入只借连接、不提交）。
    # 提交放在**所有写入之后、构造响应之前**：先提交再返回快照，是因为快照里的
    # ``pipeline_id`` 要能被下一个请求（轮询 / 答题）真的查到——忘了这一行，
    # 症状是「发起生成返回 200，紧接着答题报『生成会话不存在』」，
    # 而图那边其实已经跑完并把 checkpoint 写进了库（checkpointer 用独立的连接池、
    # 自己提交），于是**看上去像是图的问题**。整条链路里只有这一处能保证「图跑完了，
    # 结果也留下了」，所以它必须在这里，而不是等依赖注入收尾。
    await session.commit()
    return _snapshot(turn)


async def resume_generation(
    *,
    session: AsyncSession,
    ctx: SubjectContext,
    thread_id: str,
    answers: list[AnswerInput],
) -> WorkflowSnapshot:
    """提交澄清回答并继续。

    ``answers`` 转成 ``{key: value}`` 的映射再进图：``interrupt`` 的恢复值是
    **一个映射**（``Command(resume=...)``），列表形式在这里就地归一——
    归一放在 service 而不是让图吃两种形状，是因为「问卷的 key 是什么」这件事
    由 ``param_check`` 节点定义，图那边已经有一份唯一实现。
    """
    pipeline = await _pipeline_of_thread(session=session, ctx=ctx, thread_id=thread_id)
    await require_roles(session, ctx, pipeline.project_id, write=True)
    if not answers:
        raise ValidationError("没有提交任何回答。")
    # 这一轮不重新取数据源：澄清只是补参数，源/目标在发起时就定下了并在 checkpoint 里，
    # 中途重取一次会让「用户答题期间数据源被停用」把一次澄清变成一次失败——
    # 那是发起校验该管的事，不是答题该管的事
    now = datetime.now(UTC)

    from workflow import resume

    turn = await resume(
        _graph(),
        thread_id,
        {"answers": {answer.question_id: answer.value for answer in answers}},
        session=session,
        ctx=ctx,
        pipeline_id=pipeline.id,
    )
    await _persist_turn(session=session, ctx=ctx, pipeline=pipeline, turn=turn, now=now)
    # 与 start_generation 同一个理由：这一轮的产物（可能是冻结后的版本）必须落住，
    # 否则「答完题刷新页面，回答全丢了、候选版本也没有」。
    await session.commit()
    return _snapshot(turn)


async def get_generation(*, session: AsyncSession, ctx: SubjectContext, thread_id: str) -> WorkflowSnapshot:
    """读快照（断线恢复）。**只读 checkpoint，不跑图**。

    前端刷新后靠它重建整个 Studio：如果这里顺手把图往前推一步，
    刷新页面就成了一个会改变系统状态的动作——不幂等、也不可预期。
    """
    pipeline = await _pipeline_of_thread(session=session, ctx=ctx, thread_id=thread_id)
    await require_roles(session, ctx, pipeline.project_id, write=False)

    from workflow import snapshot

    return WorkflowSnapshot.model_validate(await snapshot(_graph(), thread_id))


async def abort_generation(*, session: AsyncSession, ctx: SubjectContext, thread_id: str) -> WorkflowSnapshot:
    """用户主动取消。

    这是**用户的取消**，不是 M4 的运行期硬中断：不产生证据事件、不改已冻结的制品，
    只把这一轮的线程标成中止并把流程状态落成 ``draft``。
    """
    pipeline = await _pipeline_of_thread(session=session, ctx=ctx, thread_id=thread_id)
    await require_roles(session, ctx, pipeline.project_id, write=True)

    from workflow import abort

    turn = await abort(_graph(), thread_id, reason="用户取消")
    await repository.set_pipeline_status(
        session=session, pipeline_id=pipeline.id, status=PipelineStatus.DRAFT
    )
    log.info(
        "pipeline_generation_aborted",
        pipeline_id=pipeline.id,
        tenant_id=ctx.tenant_id,
        thread_id=thread_id,
    )
    # 中止也要落住：不提交的话前端看到「已取消」，刷新之后流程还是原来的状态，
    # 用户会以为取消没生效而再点一次。
    await session.commit()
    return _snapshot(turn)


# ---------------------------------------------------------------- 图


def _graph() -> Any:
    """编译好的图。**每次现编译**。

    不做模块级缓存：checkpointer 由 lifespan 打开、测试里会开关，
    缓存住图就等于缓存住一个可能已经关闭的 saver——症状是「重启 lifespan 之后
    第一个请求报连接已关闭」，而它看起来像是连接池的问题。
    编译本身的代价是一次字典组装，与一次 LLM 调用相比可以忽略。
    """
    from workflow import build_graph
    from workflow.checkpointer import get_checkpointer

    return build_graph(get_checkpointer().saver)


def _thread_id(pipeline_id: int) -> str:
    from workflow.state import thread_id_for

    return thread_id_for(pipeline_id)


async def _initial_state(
    *,
    session: AsyncSession,
    ctx: SubjectContext,
    pipeline: Any,
    project_id: int,
    requirement: str,
    sources: dict[int, Any],
    now: datetime,
) -> dict[str, Any]:
    """入图前的初始 state。

    这里填的每个键都是「图查不到、只有库里有」的东西：数据源类型（渲染器要按连接器
    出模板）、环境（PDP 按 prod/staging/dev 定级）、档案概要（参数完备性检查要知道
    源表结构探过没有）。**不放 session**——state 会被序列化进 PG checkpoint，
    塞一个连接进去会在第一次序列化时就炸。

    ``source_profile`` / ``target_profile`` / ``budget`` 三个键是**给提示词用的**：
    be-08 的 ``plan_blocks`` 要靠它们产出 ``<<SOURCE_PROFILE>>`` / ``<<BUDGET>>`` 围栏，
    而 engines 不许查库——所以只能在这里备好。少注入一个的后果不是「退化成简单方案」，
    而是节点拼不出提示词：`FakeProvider` 会直接抛「没有围栏」，真模型那边则是
    「模型凭表名猜列名」——两种都比一次明确的失败更糟。
    """
    source = sources[pipeline.source_data_source_id]
    profiles = {
        str(ds_id): await repository.profile_summary(
            session=session, tenant_id=ctx.tenant_id, data_source_id=ds_id
        )
        for ds_id in sorted(sources)
    }
    draft_source = await _draft_source(
        session=session, ctx=ctx, data_source_id=pipeline.source_data_source_id
    )
    return {
        "thread_id": _thread_id(pipeline.id),
        "pipeline_id": pipeline.id,
        "project_id": project_id,
        "tenant_id": ctx.tenant_id,
        "user_id": ctx.user_id,
        "requirement": requirement,
        "source_data_source_id": pipeline.source_data_source_id,
        "target_data_source_id": pipeline.target_data_source_id,
        "env": str(getattr(source.env, "value", source.env)),
        "data_source_types": {
            str(ds_id): str(getattr(ds.type, "value", ds.type)) for ds_id, ds in sources.items()
        },
        "profile_summaries": profiles,
        "source_profile": draft_source.model_dump(mode="json", by_alias=True),
        "target_profile": await _draft_target(
            session=session, ctx=ctx, data_source_id=pipeline.target_data_source_id
        ),
        "budget": _budget().model_dump(mode="json"),
        "started_at": now.isoformat(),
    }


async def _draft_source(
    *, session: AsyncSession, ctx: SubjectContext, data_source_id: int
) -> DraftSourceTable:
    """源端 → be-08 提示词要的 ``DraftSourceTable``。

    **没有可用的档案就直接拒绝**，不回落到「空列画像」：模型拿到一张没有列的表，
    唯一能做的是编列名，而编出来的列名过不了门禁的 ``check_mapping``，
    用户看到的是「方案又错了」，真正的原因（源端还没探查）却不在任何提示里。
    """
    profile = await repository.latest_profile(
        session=session, tenant_id=ctx.tenant_id, data_source_id=data_source_id
    )
    tables = profile_view.tables_of(profile)
    if profile is None or not tables:
        raise ValidationError(
            "源数据源还没有可用的元数据档案，请先完成一次元数据探查再生成方案。"
        )
    return DraftSourceTable(
        data_source_id=data_source_id,
        # 快照里的 schema 按「一个数据源一个 schema」处理：DraftSourceTable 只有一个
        # schema 字段，跨 schema 的源要拆成两个数据源注册（探查器的既定假设）
        schema_name=tables[0].schema_name,
        tables=[
            DraftTable(
                name=table.name,
                row_count_approx=table.row_count_approx,
                columns=[
                    DraftColumn(
                        name=column.name,
                        data_type=column.data_type,
                        nullable=column.nullable,
                        is_primary_key=column.is_primary_key,
                        sensitive_level=column.sensitive_level,
                    )
                    for column in table.columns
                ],
            )
            for table in tables
        ],
    )


async def _draft_target(
    *, session: AsyncSession, ctx: SubjectContext, data_source_id: int
) -> dict[str, Any]:
    """目标端 → ``DraftTargetTable`` 的 dump。

    ``columns`` 留空是**常态**而不是缺陷：本项目固定「影子表 + 原子 Swap」，
    目标表在第一次执行前不存在，所以目标端通常**没有档案**。
    ``DraftTargetTable`` 的契约写明「列为空 = 按源列形状建宽表」，
    正好对上这条路径——所以这里不因为「没档案」就报错（那会把正常的首次生成拦下来）。
    """
    profile = await repository.latest_profile(
        session=session, tenant_id=ctx.tenant_id, data_source_id=data_source_id
    )
    tables = profile_view.tables_of(profile)
    first = tables[0] if tables else None
    target = DraftTargetTable(
        data_source_id=data_source_id,
        database=first.schema_name if first is not None else "",
        table=first.name if first is not None else "",
        columns=[
            DraftColumn(
                name=column.name,
                data_type=column.data_type,
                nullable=column.nullable,
                is_primary_key=column.is_primary_key,
                sensitive_level=column.sensitive_level,
            )
            for column in (first.columns if first is not None else ())
        ],
    )
    return target.model_dump(mode="json", by_alias=True)


def _budget() -> RuntimeBudget:
    """默认预算：来自 ``services.policy.budget_defaults()``（be-07 的策略默认值）。

    **不许在这里写死数字**：预算默认值是策略，`budget_defaults()` 读的是 settings，
    而 settings 是部署时能改的。往这里抄一份 ``max_read_rows=10_000_000``
    就等于把策略焊死在代码里，改配置不再生效——且没有任何报错。
    """
    return RuntimeBudget(**policy_service.budget_defaults().model_dump())


# ---------------------------------------------------------------- 落库


async def _persist_turn(
    *,
    session: AsyncSession,
    ctx: SubjectContext,
    pipeline: Any,
    turn: Any,
    now: datetime,
) -> None:
    """把这一轮的产物落成制品（有 Plan 才落）。

    三个「不落」的情况，每一个都有理由：

    - **还没到出方案**（停在澄清）：``draft_plan`` 为空 → 什么都不落，
      只把流程状态推进一步（``draft``）；
    - **Plan 结构校验没过**：仍然落——但 ``valid=False`` 且带上问题清单。
      不落的话前端看到的是「方案为空」，而真实情况是「方案生成了但有 3 处问题」；
    - **重放**（同一轮被 Celery/重试跑第二遍）：候选行是**原地更新**，
      所以重复落不会产生第二个候选，也不会改任何已冻结的东西。
    """
    values: dict[str, Any] = dict(getattr(turn, "values", {}) or {})
    status = _status_of(values)
    plan_body = values.get("draft_plan")
    hocon_content = values.get("draft_hocon") or ""

    if values.get("frozen_version_id"):
        # 「这一轮里已经冻结成版本了」——`freeze` 节点自己落过制品、也自己冻结了候选行，
        # 这里再走一遍 save_artifacts 会插出**第二组**制品行（不可变表，插了就删不掉），
        # 而 get_or_create_candidate 因为候选已被冻结、查不到候选行，会再新建一个空候选。
        # 症状是：冻结一次，版本列表里多一个没有制品的候选版本。
        await repository.set_pipeline_status(session=session, pipeline_id=pipeline.id, status=status)
        log.info(
            "pipeline_turn_already_frozen",
            pipeline_id=pipeline.id,
            tenant_id=ctx.tenant_id,
            version_id=values.get("frozen_version_id"),
        )
        return

    if not plan_body or not hocon_content:
        await repository.set_pipeline_status(session=session, pipeline_id=pipeline.id, status=status)
        return

    plan = EtlPlanModel.model_validate(plan_body)
    issues = validate_plan(plan)
    blocking = [issue for issue in issues if issue.severity == "block"]
    # ``hocon_meta`` 就是 ``hocon_artifact.validation_result`` 的全部内容，形状由
    # 生产者定：渲染段 ``gating.render_meta()`` ⊕ 解析/门禁段 ``GateOutcome.artifact_meta()``，
    # 由 ``generate_hocon`` 与 ``gate`` 两个节点拼好后放在 state 里，这里**原样落库**。
    # 早先这里又拼了一次（``{**hocon_meta, "gate": values["gate_result"]}``）——
    # 「同一份语义两处组装」的经典下场：与节点拼出的形状不一致，
    # 于是 ``get_gate`` 从库里的 ``["gate"]`` 读到的不是 ``GateResult``。
    hocon_meta = dict(values.get("hocon_meta") or {})

    ids = await repository.save_artifacts(
        session=session,
        tenant_id=ctx.tenant_id,
        pipeline_id=pipeline.id,
        plan=plan,
        plan_valid=not blocking,
        validation_errors=[issue.model_dump(mode="json") for issue in issues],
        hocon_content=hocon_content,
        hocon_sha256=str(hocon_meta.get("sha256") or _sha256(hocon_content)),
        parser_version=str(hocon_meta.get("parser_version") or ""),
        hocon_meta=hocon_meta,
    )
    fingerprint = parts.fingerprint(
        parts.parts_from_plan(
            plan=plan,
            hocon_content=hocon_content,
            pipeline_id=pipeline.id,
            source_metadata_sha256=await _source_metadata_sha256(session=session, ctx=ctx, plan=plan),
        )
    )
    await repository.get_or_create_candidate(
        session=session,
        tenant_id=ctx.tenant_id,
        project_id=pipeline.project_id,
        pipeline_id=pipeline.id,
        ids=ids,
        sha256=fingerprint,
        generation_context=dict(values.get("generation_context") or {}),
        now=now,
    )
    await repository.set_pipeline_status(session=session, pipeline_id=pipeline.id, status=status)


async def _source_metadata_sha256(*, session: AsyncSession, ctx: SubjectContext, plan: EtlPlanModel) -> str:
    """与 ``artifacts``、``gating`` 同一个拼法（唯一实现在 ``parts``）。"""
    digests = await repository.profile_digests(
        session=session,
        tenant_id=ctx.tenant_id,
        data_source_ids=[item.data_source_id for item in plan.inputs],
    )
    return parts.source_metadata_sha256(digests.values())


def _status_of(values: dict[str, Any]) -> PipelineStatus:
    """state 里的状态 → 枚举。

    ``status`` 可能是 ``PipelineStatus`` 也可能是它的值（state 要能过 JSON 序列化）。
    **认不出来就地报错**而不是回退到 ``draft``：一个拼错的状态名会把「门禁没过」
    显示成「刚创建」，而后者看起来完全正常。
    """
    raw = values.get("status")
    if isinstance(raw, PipelineStatus):
        return raw
    if isinstance(raw, str) and raw:
        try:
            return PipelineStatus(raw)
        except ValueError as exc:
            raise ValidationError(f"工作流给出的状态名无效：{raw}") from exc
    return PipelineStatus.DRAFT


# ---------------------------------------------------------------- 杂项


async def _pipeline_of_thread(*, session: AsyncSession, ctx: SubjectContext, thread_id: str) -> Any:
    """``thread_id`` → pipeline。

    ``thread_id`` 就是 ``str(pipeline_id)``，但仍然走一次**带 tenant 过滤**的查询：
    直接 ``int(thread_id)`` 再去取，会让一个越权的 ``thread_id`` 与一个不存在的
    ``thread_id`` 得到不同的错误信息——那是把「有没有这条流程」泄漏给了别的租户。
    """
    try:
        pipeline_id = int(thread_id)
    except (TypeError, ValueError) as exc:
        raise NotFoundError(f"生成会话不存在：{thread_id}") from exc
    try:
        return await repository.load_pipeline(
            session=session, tenant_id=ctx.tenant_id, pipeline_id=pipeline_id
        )
    except NotFoundError as exc:
        raise NotFoundError(f"生成会话不存在：{thread_id}") from exc


async def _load_sources(
    *, session: AsyncSession, ctx: SubjectContext, ids: list[int | None], project_id: int
) -> dict[int, Any]:
    """取源/目标数据源，并校验「都选了、都在本项目里、都是启用状态」。

    三种情况分开报：只选了一个、选了别人的、选了已停用的。
    合并成一句「数据源无效」会让用户在三件完全不同的事之间反复试。
    """
    wanted = sorted({int(i) for i in ids if i})
    if len(wanted) < 2:
        raise ValidationError("必须同时选定源端与目标端数据源。")
    # 复用数据源模块的取行：那里已经强制带了 tenant_id 过滤（跨租户一律 404），
    # 自己再写一份 select 就是第二个「忘记加 tenant_id」的机会
    from services.datasource import _load

    found: dict[int, Any] = {}
    for ds_id in wanted:
        row = await _load(session=session, ctx=ctx, data_source_id=ds_id)
        if row.project_id != project_id:
            # 不区分「不存在」与「不是本项目」的对外文案（越权探测），
            # 但日志里能看出是哪一种
            log.warning(
                "pipeline_datasource_cross_project",
                data_source_id=ds_id,
                project_id=project_id,
                owner_project_id=row.project_id,
                tenant_id=ctx.tenant_id,
            )
            raise ValidationError(f"数据源 {ds_id} 不属于当前项目。")
        found[ds_id] = row
    inactive = [ds_id for ds_id, row in found.items() if str(getattr(row, "status", "active")) != "active"]
    if inactive:
        raise ValidationError(f"数据源 {inactive} 已停用，不能用于生成。")
    return found


def _name_from(requirement: str) -> str:
    """从需求里截一个流程名。

    取第一行、去掉结尾标点、限长。**不做「智能起名」**：名字是给人在列表里认的，
    一个由模型起的长名字反而更难扫；用户想改就改，成本远低于让模型再调一次。
    """
    first = requirement.strip().splitlines()[0].strip() if requirement.strip() else "未命名流程"
    trimmed = first.rstrip("。.,；;!！?？").strip()
    return (trimmed or "未命名流程")[:_NAME_LIMIT]


def _sha256(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _snapshot(turn: Any) -> WorkflowSnapshot:
    """``TurnResult.snapshot`` → DTO。

    ``TurnResult`` 是 ``workflow`` 的 dataclass，这里做一次 ``model_validate``
    而不是直接返回 dict：路由的 ``response_model`` 迟早会校验它，
    在 service 里校验能把「图给的键与前端要的键对不上」暴露在这一层。
    """
    return WorkflowSnapshot.model_validate(turn.snapshot)


__all__ = [
    "abort_generation",
    "get_generation",
    "resume_generation",
    "start_generation",
]
