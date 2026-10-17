"""制品指纹的五部分怎么拼（be-12 §6.1）——**全项目唯一的一份定义**。

指纹是审批、Capability、执行三方共同引用的信任根。它一旦有两处定义，
就会出现「生成时算的指纹」与「校验时算的指纹」不一致，而**症状是随机的**：
同一份制品，有时报「指纹失效」，有时正常。这类 bug 的排查成本极高，
所以这里把「指纹的输入是什么」收成一个模块、一组函数。

## 一个必须守住的形状：指纹只能由**落库的制品**重算

be-12 §6 要求「从零独立验证」。若指纹是从 Plan 现推质量契约、现推督导契约，
那么**落库的那两行就退出信任链了**——库里存的是什么就不重要了，
只要 Plan 一样，指纹就一样。于是有人改了 ``quality_contract.rules``（本该不可变）
不会有任何东西报错。

所以这里的两个方向共用同一对构造器：

    quality_contract_body(plan, pipeline_id) ──┬─→ QualityContract(**body)   落库
                                               └─→ body                      进指纹
    quality_body_from_row(row)  ─────────────────→ body                      校验时重算

``save_artifacts`` 用前者建行、``parts_from_rows`` 用后者重算。
两边的键名与值来自同一个 dict，**在结构上就不可能漂移**。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from engines.fingerprint import ArtifactParts, artifact_sha256, build_parts, matches
from engines.plan.schema import EtlPlanModel
from models.pipeline import EtlPlan, HoconArtifact, QualityContract, RuntimeSupervisionContract

#: 拒绝行的落点表名后缀（``<target_table>_elt_reject``）。
#: 它是**契约的一部分**：换后缀就等于换了一份质量契约，
#: 所以它必须进指纹，不能由执行期临时决定。
REJECT_SUFFIX = "_elt_reject"


def error_table_name(table: str) -> str:
    """目标表 → 拒绝行落点表名。**全项目唯一的命名定义**。

    与 ``preparation.shadow_table_name`` 同一个道理：名字在 Commit 时写进
    ``execution_run.error_table``、在 be-15 渲染 HOCON 时替换 ``${error_table}``、
    在 be-17 分流时建表。三处各拼一次字符串，症状是「建的表和写的表不是同一张」——
    错误行安静地落进一张没人查的表里，而质量报告显示一切正常。
    """
    return f"{table}{REJECT_SUFFIX}"

#: 督导契约的列名，与 ``RuntimeSupervisionContract`` 一一对应。
SUPERVISION_FIELDS: tuple[str, ...] = (
    "max_read_rows",
    "max_write_bytes",
    "max_duration_sec",
    "max_amplification_ratio",
    "max_reject_rate",
    "warn_threshold_pct",
)


def supervision_body(plan: EtlPlanModel) -> dict[str, Any]:
    """Plan 的预算 → 督导契约体（六列）。"""
    return {name: getattr(plan.budget, name) for name in SUPERVISION_FIELDS}


def quality_contract_body(plan: EtlPlanModel, *, pipeline_id: int) -> dict[str, Any]:
    """Plan 的质量规则 → 质量契约体。

    ``rules`` 里的每条就是 ``QualityRule`` 的序列化（``by_alias=True``，
    与 Plan 走同一个出口）——**不做裁剪**。裁掉 ``enabled`` 之类的字段看似省空间，
    实际是让「契约体」与「Plan 里的规则」成为两个概念，于是指纹算的是一份、
    执行用的是另一份。
    """
    return {
        "rules": [
            rule.model_dump(mode="json", by_alias=True, exclude_none=False) for rule in plan.quality_rules
        ],
        "error_table_target": {"suffix": REJECT_SUFFIX, "pipeline_id": pipeline_id},
        "reject_rate_budget": plan.budget.max_reject_rate,
    }


# ---------------------------------------------------------------- 落库行 → 契约体


def supervision_body_from_row(row: RuntimeSupervisionContract) -> dict[str, Any]:
    """督导契约行 → 契约体。键名与 :func:`supervision_body` 完全一致。"""
    return {name: getattr(row, name) for name in SUPERVISION_FIELDS}


def quality_body_from_row(row: QualityContract) -> dict[str, Any]:
    """质量契约行 → 契约体。

    刻意**不**从 Plan 现推：见模块 docstring——那样等于让库里那两行退出信任链。
    """
    return {
        "rules": list(row.rules or []),
        "error_table_target": dict(row.error_table_target or {}),
        "reject_rate_budget": row.reject_rate_budget,
    }


# ---------------------------------------------------------------- 指纹


def parts_from_plan(
    *,
    plan: EtlPlanModel,
    hocon_content: str,
    pipeline_id: int,
    source_metadata_sha256: str,
) -> ArtifactParts:
    """**生成期**：手上只有 Plan（制品还没落库）时拼五部分。"""
    return build_parts(
        plan,
        hocon_content,
        quality_contract_body(plan, pipeline_id=pipeline_id),
        supervision_body(plan),
        source_metadata_sha256,
    )


def parts_from_rows(
    *,
    plan_row: EtlPlan,
    hocon_row: HoconArtifact,
    quality_row: QualityContract,
    supervision_row: RuntimeSupervisionContract,
    source_metadata_sha256: str,
) -> ArtifactParts:
    """**校验期 / 回读期**：从**落库的制品**重算五部分。

    Plan 取 ``etl_plan.plan``（已落库的 jsonb），不是内存里那份对象——
    这是「从零独立验证」的前提：验链脚本手上只有库。
    """
    return build_parts(
        plan_row.plan,
        hocon_row.content,
        quality_body_from_row(quality_row),
        supervision_body_from_row(supervision_row),
        source_metadata_sha256,
    )


def fingerprint(parts: ArtifactParts) -> str:
    """五部分 → 指纹。薄封装只是为了「算指纹」这个动作在本仓库只有一个入口。"""
    return artifact_sha256(parts)


def source_metadata_sha256(digests: Iterable[str]) -> str:
    """第五部分「源档案指纹」怎么由多个源的档案算出来——**唯一的一份定义**。

    一个方案可能读多张表（跨源 join），而指纹要能被「任一源表结构变化」击穿，
    所以这里把**全部**源端档案的 sha256 排序后拼成一个值。排序是必需的：
    ``parts`` 的顺序取决于 Plan 里 ``inputs`` 的书写顺序，而同一个方案换个顺序
    写出来是同一份方案，指纹不该因此改变。

    ## 校验侧必须用同一条规则，因此校验侧要重查一次「当前档案」

    校验（``freeze_version``、验链脚本）手上只有库，重算这一步要按 Plan 里
    ``inputs[*].data_source_id`` 去取**当前最新的 READY 档案**。于是：

    - 生成到冻结之间如果重新探查过源表 → 指纹对不上，冻结被拒。
      这是**要的**行为：档案变了就说明门禁当初看的那份结构已经不在了，
      该重新生成而不是把旧的批下去。故障方向是「多拒一次」，不是「静默放行」。
    - 取不到档案的源**跳过**（与 ``gating`` 建 ctx 时同一规则），
      而不是塞一个空串——塞空串会让「没探查过」和「探查过、sha 恰好是空」
      两种状态产生同一个指纹。
    """
    return "|".join(sorted(digest for digest in digests if digest))


def fingerprint_matches(*, stored: str, parts: ArtifactParts) -> bool:
    """比对指纹。用 ``engines.fingerprint.matches`` 而不是 ``==``。

    那里做了「长度 / 大小写 / 前缀」的归一：指纹会经过 jsonb、前端、
    审批意见的复制粘贴，``0xAB``、大写、前后空格都不该让一份好制品被判成失效。
    """
    return matches(stored, parts)


def fingerprint_matches_dict(stored: str, parts: Mapping[str, Any] | ArtifactParts) -> bool:
    """``parts`` 已经是 dict 时的入口（给验链脚本与后台核对任务用）。"""
    if isinstance(parts, ArtifactParts):
        return matches(stored, parts)
    return matches(stored, ArtifactParts(**parts))


__all__ = [
    "REJECT_SUFFIX",
    "SUPERVISION_FIELDS",
    "error_table_name",
    "fingerprint",
    "fingerprint_matches",
    "fingerprint_matches_dict",
    "parts_from_plan",
    "parts_from_rows",
    "quality_body_from_row",
    "quality_contract_body",
    "source_metadata_sha256",
    "supervision_body",
    "supervision_body_from_row",
]
