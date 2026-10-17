"""配置物化：作业命名、令牌替换、影子表改写（be-15 §3.2 / §6.1）。

## 这个文件存在的理由

制品（``hocon_artifact.content``）是**不可变**的：审批人看到的是「写 sales_wide」，
而执行时必须写影子表。于是运行期要做三件制品里没有的事：

1. 把 ``{{secret:1:password}}`` 换成真值（**只在本进程内存与临时文件里**）；
2. 把 sink 段的目标表由正式表改成影子表；
3. 给作业起一个带 ``run_id`` 的名字（重投时靠它查重）。

三件事都只改**这一份运行配置**，制品本身一个字节不动——这正是「不可变版本 + 运行期
参数注入」的分工：审批的对象是制品，执行的对象是制品 ⊕ 运行参数。

## 为什么表名改写是「按行定位」而不是全文字符串替换

``sales_wide`` 这个名字在制品里可能出现三次：``transform`` 段的 SQL、``sink`` 段的
``table``、以及 ``env.job.name``（``elt_sync_sales_wide``）。全文 ``str.replace``
会把 SQL 里的同名字符串一起改掉——如果那是源表名，作业就变成「读影子表」，
而影子表此刻是空的：**作业成功、数据为零**，是最难查的一类静默错误。
所以这里按行扫，只在 ``sink {`` 段内、且形如 ``table = "..."`` 的行上动手。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field

from clients.secrets.materialize import MaterializedSecrets, SecretUnavailable
from engines.hocon.types import assert_no_unresolved_tokens, find_tokens

#: 作业名里允许出现的字符。``run_id`` 是 int、版本号与流程 id 也是 int，
#: 所以这个集合永远够用——**用户输入（需求文本、表名）一个都不进作业名**。
_JOB_NAME_SAFE = re.compile(r"[^A-Za-z0-9_.-]+")

_SINK_OPEN = re.compile(r"^\s*sink\s*\{")
_ENV_OPEN = re.compile(r"^\s*env\s*\{")
_TABLE_LINE = re.compile(r'^(\s*)table\s*=\s*"([^"]*)"\s*$')
_JOB_NAME_LINE = re.compile(r'^(\s*)job\.name\s*=\s*"([^"]*)"\s*$')


def build_job_name(*, pipeline_id: int, version_no: int, run_id: int) -> str:
    """``etl-{pipeline_id}-v{version_no}-run{run_id}``。

    **全项目唯一定义**（``services/run.run_job_name`` 只是它的转发）：
    名字要同时被三处用到——提交给 Zeta、``find_by_name`` 查重、证据事件 payload。
    三处各拼一次字符串，改一处漏一处，症状是「重投时查不到自己的作业」，
    于是同一次运行在 Zeta 里起了两个作业。
    """
    raw = f"etl-{int(pipeline_id)}-v{int(version_no)}-run{int(run_id)}"
    return _JOB_NAME_SAFE.sub("-", raw)


@dataclass(slots=True)
class InjectionReport:
    """脱敏的替换记录。**这是唯一允许进日志/账本的形式**：只记键名与计数。"""

    placeholders_total: int = 0
    replaced_keys: list[str] = field(default_factory=list)
    retargeted_tables: list[str] = field(default_factory=list)
    job_name: str = ""

    def as_payload(self) -> dict[str, object]:
        return {
            "placeholders_total": self.placeholders_total,
            "replaced_keys": list(self.replaced_keys),
            "retargeted_tables": list(self.retargeted_tables),
            "job_name": self.job_name,
        }


def iter_sink_tables(hocon_content: str) -> set[str]:
    """制品 ``sink`` 段里出现的表名。

    与 ``render_config`` 共用同一套行级扫描常量，**不另写一份正则**：
    执行器要用它判「每张目标表都有影子表可换」，而如果这个判断用的是另一份
    「大概是 sink 段」的解析，两次解析对同一份制品的结论可能不同——
    于是「改写时漏了那张表」与「检查时说它没问题」可以同时成立。
    """
    tables: set[str] = set()
    in_sink = False
    depth = 0
    for line in hocon_content.splitlines():
        if _SINK_OPEN.match(line):
            in_sink, depth = True, 1
            continue
        if not in_sink:
            continue
        depth += line.strip().count("{") - line.strip().count("}")
        if depth <= 0:
            in_sink = False
            continue
        match = _TABLE_LINE.match(line)
        if match:
            tables.add(match.group(2))
    return tables


def render_config(
    *,
    hocon_content: str,
    secrets: MaterializedSecrets,
    shadow_tables: Mapping[str, str],
    job_name: str,
) -> tuple[str, InjectionReport]:
    """把制品渲染成可提交的运行配置。

    四步，顺序不可换：**先改表名与作业名，再替换令牌**。反过来的话，
    令牌替换会把一份**还没改表名**的内容交给下游；而表名改写是按行扫的，
    它对已经含真口令的行同样会扫一遍（虽然不会误伤，但那时真口令已经在这段字符串里，
    中间任何一次异常打印都会把它带出去）。令牌替换放最后，等于让「含真值的字符串」
    的生存期最短。

    ``shadow_tables`` 是 ``{正式表名: 影子表名}``：改名的映射由 be-14/be-13 决定
    （``resource_scope.writes``），这里只负责应用。传空映射表示不改写——
    用于「只是想看看渲染结果」的自检路径，正常提交路径必须传。
    """
    report = InjectionReport(job_name=job_name)
    lines = hocon_content.splitlines()
    in_sink = False
    depth = 0

    for index, line in enumerate(lines):
        stripped = line.strip()
        if _SINK_OPEN.match(line):
            in_sink, depth = True, 1
            continue
        if in_sink:
            depth += stripped.count("{") - stripped.count("}")
            if depth <= 0:
                in_sink = False
                continue
            match = _TABLE_LINE.match(line)
            if match and match.group(2) in shadow_tables:
                target = shadow_tables[match.group(2)]
                lines[index] = f'{match.group(1)}table = "{target}"'
                report.retargeted_tables.append(f"{match.group(2)}->{target}")
        if _ENV_OPEN.match(line):
            continue
        name_match = _JOB_NAME_LINE.match(line)
        if name_match:
            # env.job.name 也改：Zeta 的控制台与 running-jobs 都显示它，
            # 显示的是制品里的旧名而我们按新名查重时，对账会「查不到自己的作业」
            lines[index] = f'{name_match.group(1)}job.name = "{job_name}"'

    rendered = "\n".join(lines)
    if hocon_content.endswith("\n"):
        rendered += "\n"  # 制品恰好一个结尾换行，渲染结果保持一致（HOCON 无关但便于 diff）

    rendered, replaced = _substitute_tokens(rendered, secrets)
    report.replaced_keys = replaced
    # **执行前**的那道闸，正是它该在的位置（CLAUDE.md §9 专门写过：这道闸不能拿去卡冻结，
    # 但提交执行时必须过）。上面那个 for 循环已经逐个替换过，这里再查一遍不是冗余：
    # 它拦的是「正则与替换逻辑将来分叉」——例如令牌语法加了一种形态而替换没跟上，
    # 结果就是拿着字面量去连库。多一次全量正则的代价可以忽略。
    assert_no_unresolved_tokens(rendered, where="渲染后的运行配置")
    return rendered, report


def _substitute_tokens(text: str, secrets: MaterializedSecrets) -> tuple[str, list[str]]:
    """令牌 → 真值。**任何一个令牌解不开就整体失败**（fail-closed）。

    不做部分替换：留下一半令牌的配置交给 SeaTunnel，行为是「拿字面量
    ``{{secret:1:password}}`` 当口令去连库」——它会连不上，或者更糟，
    连上了一个恰好用这个名字作口令的库。
    """
    tokens = find_tokens(text)
    replaced: list[str] = []
    for token in tokens:
        parts = token[2:-2].split(":")  # {{secret:1:password}} → ["secret", "1", "password"]
        key = f"{int(parts[1])}:{parts[2]}"
        value = secrets.values.get(key)
        if value is None:
            # 报引用不报值；引用（ds id + 字段）是配置事实，不是秘密
            raise SecretUnavailable(f"未物化的密钥令牌 {token}（refs: {len(secrets.refs)} 条）")
        text = text.replace(token, value)
        replaced.append(token[2:-2])
    return text, replaced


__all__ = ["InjectionReport", "build_job_name", "iter_sink_tables", "render_config"]
