"""``seatunnel.submit`` / ``seatunnel.stop`` 的工具执行器（be-15 §5.2 / §5.3）。

**这里是整条链路上唯一一处「口令变成字节」的地方**，所以它的每一条纪律都不是风格问题：

1. 只认 ``preparation.resource_scope.secrets`` 里登记过的引用。``args.secret_refs``
   由 worker 从准备单复制而来，但**执行器不信任调用方**：多出一个未登记的 ref
   就等于开了一条「读任意 Vault 路径」的通路（``harness/tools.py`` 在
   ``SeatunnelSubmitArgs.secret_refs`` 上留的那段注释说的正是这件事）。
2. 渲染结果只写进 0600 的临时文件，**永不**落库、进日志、进异常、进返回 dict。
   日志只记长度与哈希；返回 dict 还要过 ``harness/broker.py::_ALLOWED_DETAIL_KEYS``，
   未知键会被静默丢弃——所以这里只回白名单里真实存在的键。
3. ``finally`` 里销毁（物化上下文负责文件与内存）。

**与设计文档 §5.2 的一处偏离（登记在此，不要静默改回）**：设计把 ``config_path``
放进 ``SeatunnelSubmitArgs``，让 worker 先物化、再把路径递给 broker。本实现里
args 只有引用，路径由执行器按 ``ctx.run_id`` 自己拼。

理由是「越权在类型上不成立」：路径若由调用方给，「不许读到别的 run 的配置」
就退化成对调用方的一行校验；由执行器自己拼，它只可能打开自己刚写的那个文件。
附带好处是 broker 的工具契约里不再出现文件系统路径。
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from clients.seatunnel.client import SeatunnelClient, job_id_for_run
from clients.seatunnel.job import build_job_name, iter_sink_tables, render_config
from clients.seatunnel.models import SeatunnelError, SubmitResult
from clients.secrets import (
    JOB_CONFIG_FILENAME,
    MaterializedSecrets,
    SecretUnavailable,
    VaultSecretProvider,
    materialize_secrets,
)
from core.config import get_settings
from engines.hocon.connectors.jdbc import JDBC_URL_SCHEMES, build_exec_jdbc_url
from harness.tools import (
    SeatunnelStopArgs,
    SeatunnelSubmitArgs,
    ToolExecutor,
    register_executor,
)
from models.enums import ToolName

if TYPE_CHECKING:
    from harness.broker import ExecutionContext

log = structlog.get_logger(__name__)

#: 需要由 ``data_source.endpoint`` 拼、而不是从 Vault 取的令牌字段。
#: ``jdbc_url`` 不在 ``ALLOWED_SECRET_KEYS`` 里（端点白名单根本没有这个键），
#: 所以它只能拼出来——Vault 里也没有它可读。
_ENDPOINT_FIELDS = frozenset({"jdbc_url"})


# ---------------------------------------------------------------- 事实加载


class _Facts:
    """执行器要用到的全部外部事实，**一次取齐**。

    集中在一处是为了让「少查了一样」变成一眼可见的事：这些字段分散在
    run / preparation / pipeline_version / hocon_artifact / data_source 五张表上，
    逐个 ``session.get`` 的写法会让人以为它们都属于同一张表。
    """

    __slots__ = ("artifact", "job_name", "run", "scope", "scope_secrets", "shadow_tables", "version")

    def __init__(
        self,
        *,
        run: Any,
        version: Any,
        artifact: Any,
        scope: dict[str, Any],
        job_name: str,
    ) -> None:
        self.run = run
        self.version = version
        self.artifact = artifact
        self.scope = scope
        self.scope_secrets: list[dict[str, Any]] = list(scope.get("secrets") or [])
        self.shadow_tables = _shadow_map(scope)
        self.job_name = job_name


async def _load_facts(session: AsyncSession, ctx: ExecutionContext, args: SeatunnelSubmitArgs) -> _Facts:
    """按 tenant + project 取齐全部事实，任一项缺失都 fail-closed。"""
    from models.execution import ExecutionRun
    from models.governance import Preparation
    from models.pipeline import HoconArtifact, PipelineVersion

    run = (
        await session.execute(
            select(ExecutionRun).where(
                ExecutionRun.id == int(ctx.run_id or 0),
                ExecutionRun.tenant_id == ctx.tenant_id,
                ExecutionRun.project_id == ctx.project_id,
            )
        )
    ).scalar_one_or_none()
    if run is None:
        raise SecretUnavailable("运行实例不存在或不属于本项目")

    preparation = (
        await session.execute(
            select(Preparation).where(
                Preparation.id == run.preparation_id,
                Preparation.tenant_id == ctx.tenant_id,
                Preparation.project_id == ctx.project_id,
            )
        )
    ).scalar_one_or_none()
    if preparation is None:
        raise SecretUnavailable("准备单不存在或不属于本项目")

    version = (
        await session.execute(select(PipelineVersion).where(PipelineVersion.id == run.pipeline_version_id))
    ).scalar_one_or_none()
    if version is None:
        raise SecretUnavailable("运行实例指向的版本不存在")

    artifact = (
        await session.execute(select(HoconArtifact).where(HoconArtifact.id == args.hocon_artifact_id))
    ).scalar_one_or_none()
    if artifact is None:
        raise SecretUnavailable("HOCON 制品不存在")

    _assert_same_artifact(preparation=preparation, version=version, artifact=artifact, args=args)

    job_name = build_job_name(
        pipeline_id=int(getattr(version, "pipeline_id", 0) or 0),
        version_no=int(version.version_no),
        run_id=int(ctx.run_id or 0),
    )
    return _Facts(
        run=run,
        version=version,
        artifact=artifact,
        scope=dict(preparation.resource_scope or {}),
        job_name=job_name,
    )


def _assert_same_artifact(
    *, preparation: Any, version: Any, artifact: Any, args: SeatunnelSubmitArgs
) -> None:
    """**审批的对象必须就是执行的对象**——用三个指纹互相对齐。

    三个来源各有一条链路能指向制品：准备单记 ``artifact_sha256``（审批冻结时的）、
    版本记自己的 ``sha256``（全链路锚点，覆盖 Plan+HOCON+契约）、制品行记自己内容的
    ``sha256``；args 又单独带一个 ``hocon_artifact_id``。

    任意两者不一致，说明「人批准的东西」与「机器要跑的东西」不是同一份——
    这一条不查，制品表被换一行就能让一次审批执行另一份配置，而且全程无异常。
    三个来源都要：只比其中两个的话，第三个正好是那个被改的。
    """
    preparation_sha = str(getattr(preparation, "artifact_sha256", "") or "")
    version_sha = str(getattr(version, "sha256", "") or "")
    content_sha = str(getattr(artifact, "sha256", "") or "")

    if args.pipeline_version_id != int(getattr(version, "id", 0) or 0):
        raise SecretUnavailable("提交参数里的版本与运行实例指向的版本不一致")
    if int(getattr(version, "hocon_artifact_id", 0) or 0) != int(args.hocon_artifact_id):
        raise SecretUnavailable("提交参数里的制品与该版本记录的制品不一致")
    if preparation_sha and version_sha and preparation_sha != version_sha:
        # 报哈希前缀而不是全文：够定位是哪一份，又不至于把指纹全文铺进日志
        raise SecretUnavailable(
            f"准备单的制品指纹与版本指纹不一致（{preparation_sha[:12]}… ≠ {version_sha[:12]}…）"
        )
    if content_sha and version_sha and content_sha != version_sha:
        raise SecretUnavailable("HOCON 制品内容指纹与版本指纹不一致")


def _shadow_map(scope: dict[str, Any]) -> dict[str, str]:
    """``{正式表: 影子表}``，来自准备单的 ``writes``（be-13 在 Prepare 时固化）。"""
    mapping: dict[str, str] = {}
    for item in scope.get("writes") or []:
        formal, shadow = item.get("formal_table"), item.get("shadow_table")
        if formal and shadow:
            mapping[str(formal)] = str(shadow)
    return mapping


def _assert_refs_registered(args: SeatunnelSubmitArgs, scope_secrets: list[dict[str, Any]]) -> None:
    """``args.secret_refs`` 必须**逐条**能在准备单的 secrets 里找到。

    比对 ``(provider, path, version)`` 三元组：只比 ``path`` 会漏掉版本，
    而「指向另一个版本」正是轮转之后的常见形态——那时审批人看旧版本、
    执行用新版本，两边都合法、都对不上账。

    多出来的 ref **抛异常**而不是忽略：忽略等于「调用方可以让我去读任何路径，
    我只是不去读」，而下一行代码被人改成「顺手读一下」时就没有东西拦着了。
    """
    registered = {
        (str(item.get("provider") or "vault"), str(item.get("path")), int(item.get("version") or 0))
        for item in scope_secrets
    }
    for ref in args.secret_refs:
        if (ref.provider, ref.path, ref.version) not in registered:
            raise SecretUnavailable(f"提交参数里的密钥引用 {ref.path}#{ref.version} 未在准备单中登记")


def _assert_writes_covered(shadow_tables: dict[str, str], artifact_content: str) -> None:
    """制品里出现的每个目标表都要有影子表可换。

    漏掉的那张会被原样写进**正式表**（制品 sink 段写的本来就是正式表名）。
    即「影子表 + 原子 Swap」这条 P0 红线在那一张表上静默失效：作业照样成功、
    质量报告照样出，只是数据已经上了正式表、没法回滚。
    """
    missing = sorted(table for table in iter_sink_tables(artifact_content) if table not in shadow_tables)
    if missing:
        raise SecretUnavailable(f"目标表 {missing} 在准备单里没有对应的影子表，拒绝提交")


async def _endpoint_values(
    session: AsyncSession, ctx: ExecutionContext, tokens: tuple[tuple[int, str], ...]
) -> dict[str, str]:
    """拼出 ``jdbc_url`` 这一类「不来自 Vault」的令牌值。"""
    from models.datasource import DataSource

    values: dict[str, str] = {}
    for ds_id in sorted({ds for ds, field in tokens if field in _ENDPOINT_FIELDS}):
        row = (
            await session.execute(
                select(DataSource).where(
                    DataSource.id == ds_id,
                    DataSource.tenant_id == ctx.tenant_id,
                    DataSource.project_id == ctx.project_id,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise SecretUnavailable(f"数据源 {ds_id} 不存在或不属于本项目")
        if row.status != "active":
            # 停用的数据源仍能提交作业，等于「停用」只是前端的一个标签
            raise SecretUnavailable(f"数据源 {ds_id} 已停用，拒绝提交")
        ds_type = str(getattr(row.type, "value", row.type))
        if ds_type not in JDBC_URL_SCHEMES:
            raise SecretUnavailable(f"数据源 {ds_id}（{ds_type}）不支持 JDBC 连接串，无法渲染作业配置")
        # 作业是在 SeaTunnel 容器里连库的，用**数据面**视角的地址（exec_host/exec_port
        # 存在时覆盖），与控制面探测用的 host/port 分开——否则本地开发（控制面走隧道）
        # 会渲染出 127.0.0.1，容器里连的是它自己。
        values[f"{ds_id}:jdbc_url"] = build_exec_jdbc_url(ds_type, dict(row.endpoint or {}))
    return values


def _secret_specs(
    tokens: tuple[tuple[int, str], ...], scope_secrets: list[dict[str, Any]]
) -> tuple[tuple[int, str, str, int, tuple[str, ...]], ...]:
    """令牌 → 物化入参，**只物化令牌里真正出现的字段**。

    多读一个 ``key_names`` 里的字段，等于让一份用不到的明文在内存里多活一会儿。
    未登记的数据源在这里就报出来，不必等到后面查库。
    """
    by_ds = {int(item["data_source_id"]): item for item in scope_secrets}
    specs: list[tuple[int, str, str, int, tuple[str, ...]]] = []
    for ds_id in sorted({ds for ds, _ in tokens}):
        entry = by_ds.get(ds_id)
        if entry is None:
            raise SecretUnavailable(f"制品引用了未登记的数据源 {ds_id}")
        fields = tuple(
            sorted({field for ds, field in tokens if ds == ds_id and field not in _ENDPOINT_FIELDS})
        )
        if fields:
            specs.append(
                (
                    ds_id,
                    str(entry["path"]),
                    str(entry.get("provider") or "vault"),
                    int(entry["version"]),
                    fields,
                )
            )
    return tuple(specs)


# ---------------------------------------------------------------- 提交


def build_client() -> SeatunnelClient:
    """按配置构造客户端。构造点收在这里，测试替换 base_url 时不必碰执行器主体。"""
    return SeatunnelClient(get_settings().seatunnel_api_base)


async def seatunnel_submit_executor(
    args: Any, ctx: ExecutionContext, session: AsyncSession
) -> dict[str, Any]:
    """``seatunnel.submit`` 执行器。返回体只含 broker 白名单里的键。"""
    if not isinstance(args, SeatunnelSubmitArgs):  # pragma: no cover - 注册表已保证
        raise SeatunnelError("执行器收到的入参类型不符")
    if ctx.run_id is None:
        raise SecretUnavailable("提交作业需要 run_id")

    facts = await _load_facts(session, ctx, args)
    _assert_refs_registered(args, facts.scope_secrets)
    _assert_writes_covered(facts.shadow_tables, facts.artifact.content)

    from engines.hocon.types import parse_tokens

    tokens = tuple((token.data_source_id, token.field) for token in parse_tokens(facts.artifact.content))
    if not tokens:
        # 令牌一个都没有 = 制品里写着明文口令，或者这份制品不是本渲染器产的。
        # 两种都绝不该被执行，且第二种会让「无明文 Secret」的检查失去着力点。
        log.error("seatunnel_artifact_has_no_tokens", run_id=ctx.run_id)
        raise SecretUnavailable("制品里没有任何密钥令牌，拒绝执行（疑似非本系统生成的制品）")

    settings = get_settings()
    run_id = int(ctx.run_id)
    # endpoint 拼出来的值在进上下文之前就算好：它们是**非密**的连接定位信息，
    # 与「去 Vault 取口令」是两条不同的来源，混在一个循环里会让人以为 endpoint
    # 也是密钥来源（而 endpoint 白名单恰恰禁止放凭据）。
    extra_values = await _endpoint_values(session, ctx, tokens)
    provider = VaultSecretProvider.from_settings()
    try:
        async with materialize_secrets(
            specs=_secret_specs(tokens, facts.scope_secrets),
            run_id=run_id,
            work_dir=Path(settings.SEATUNNEL_WORK_DIR),
            provider=provider,
            extra_values=extra_values,
        ) as secrets:
            submit = await _submit_rendered(
                secrets=secrets,
                content=facts.artifact.content,
                shadow_tables=facts.shadow_tables,
                job_name=facts.job_name,
                run_id=run_id,
            )
    finally:
        # best-effort 撤 token；与文件销毁是两件事（文件由上下文管），所以分开写
        await provider.close()

    return {
        # 只回 ``_ALLOWED_DETAIL_KEYS`` 里存在的键：多一个键会被 broker 静默丢掉，
        # 而「该登记的键没登记」正是「连通性成功却显示 failed」的成因（CLAUDE.md §9.1）
        "job_id": submit.job_id,
        "status": "running",
        "state": "reused" if submit.reused else "submitted",
        "target": facts.job_name,
    }


async def _submit_rendered(
    *,
    secrets: MaterializedSecrets,
    content: str,
    shadow_tables: dict[str, str],
    job_name: str,
    run_id: int,
) -> SubmitResult:
    """渲染 → 落临时文件 → 提交。渲染产物只活在这个作用域与磁盘上的 0600 文件里。"""
    rendered, report = render_config(
        hocon_content=content,
        secrets=secrets,
        shadow_tables=shadow_tables,
        job_name=job_name,
    )
    config_path = secrets.write_file(JOB_CONFIG_FILENAME, rendered)
    log.info(
        "seatunnel_config_rendered",
        run_id=run_id,
        # 只记**长度与哈希**：渲染结果永不出现在日志里（be-15 §8 的日志行）
        rendered_len=len(rendered),
        rendered_sha256=hashlib.sha256(rendered.encode("utf-8")).hexdigest(),
        **report.as_payload(),
    )
    submit = await build_client().submit(config_path, job_name=job_name, job_id=job_id_for_run(run_id))
    log.info("seatunnel_submitted", run_id=run_id, job_id=submit.job_id, reused=submit.reused)
    return submit


# ---------------------------------------------------------------- 停止


async def seatunnel_stop_executor(args: Any, ctx: ExecutionContext, session: AsyncSession) -> dict[str, Any]:
    """``seatunnel.stop`` 执行器。

    「作业不存在」按成功处理（幂等，见 ``SeatunnelClient.stop``）：停止的语义是
    「让它别再跑」，而一个已经不存在的作业正满足这个语义。报错会让 be-16 的硬中断在
    「作业刚好已经自己失败了」时反复重试，越停越乱。
    """
    if not isinstance(args, SeatunnelStopArgs):  # pragma: no cover
        raise SeatunnelError("执行器收到的入参类型不符")
    try:
        await build_client().stop(args.job_id)
    except SeatunnelError as exc:
        log.warning(
            "seatunnel_stop_failed",
            run_id=ctx.run_id,
            job_id=args.job_id,
            reason=args.reason,
            # 只记分类，不记原文：Zeta 的错误里常带整段配置
            failure_kind=str(exc.kind),
        )
        raise
    return {"job_id": args.job_id, "status": "stopped", "state": "stopped"}


# ---------------------------------------------------------------- 注册

_EXECUTORS: tuple[tuple[ToolName, ToolExecutor], ...] = (
    (ToolName.SEATUNNEL_SUBMIT, seatunnel_submit_executor),
    (ToolName.SEATUNNEL_STOP, seatunnel_stop_executor),
)


def register() -> None:
    """注入执行器。由 ``main`` / ``celery`` 的启动钩子各调一次（CLAUDE.md §9.1）。"""
    from harness.tools import registered_executors

    done = registered_executors()
    for tool, fn in _EXECUTORS:
        if tool not in done:
            register_executor(tool, fn)


__all__ = ["build_client", "register", "seatunnel_stop_executor", "seatunnel_submit_executor"]
