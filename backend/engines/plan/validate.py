"""Plan 的结构校验（be-10 §6.2 / §6.4）。

**与门禁的分工**（很重要，别把两者混在一起）：

| | 本模块 ``validate_plan`` | 门禁 ``engines.gate`` |
|---|---|---|
| 回答的问题 | Plan **自洽**吗（引用闭合、无环、无重复） | 这方案**能上生产**吗（列存在、类型兼容） |
| 需要外部信息 | **不需要**（纯看 Plan 自己） | 需要档案、PDP 结论、HOCON 解析结果 |
| 时机 | 生成后立刻跑（LLM 输出的第一道闸） | 渲染出 HOCON 之后 |

所以这里**不查**「列存不存在」——那是对着 Profile 查的，归门禁。
这里查的是「你自己说的话有没有自相矛盾」。

## 定位粒度

每条 issue 的 ``path`` 是 JSON Pointer 风格（``/field_mappings/3/target_field``），
前端据此高亮字段、自动修复据此产出补丁。**没有 path 的问题不许报**——
报一条「Plan 有问题」给不出落点，用户除了重生成没有别的选择。
"""

from __future__ import annotations

from engines.plan.schema import EtlPlanModel, PlanIssue


def _issue(path: str, code: str, message: str, severity: str = "block") -> PlanIssue:
    return PlanIssue(path=path, code=code, message=message, severity=severity)  # type: ignore[arg-type]


def validate_plan(plan: EtlPlanModel) -> list[PlanIssue]:
    """返回**全部**问题（不短路）。生成链路会把它落进 ``etl_plan.validation_errors``。

    刻意返回全量而不是第一条：这些问题的修复成本很低（大多是 LLM 一次重生成），
    一次列全能让重生成提示词带上完整反馈；而门禁那边短路是因为它的检查**互相依赖**，
    这里没有依赖关系，短路只是白丢信息。
    """
    issues: list[PlanIssue] = []

    issues.extend(_check_sections(plan))
    issues.extend(_check_duplicate_ids(plan))
    issues.extend(_check_mappings(plan))
    issues.extend(_check_transforms(plan))
    issues.extend(_check_quality_rules(plan))
    issues.extend(_check_dag(plan))
    issues.extend(_check_budget(plan))

    return issues


# ---------------------------------------------------------------- 分段


def _check_sections(plan: EtlPlanModel) -> list[PlanIssue]:
    out: list[PlanIssue] = []
    if not plan.inputs:
        out.append(_issue("/inputs", "required_missing", "方案没有任何源端，无从搬运数据"))
    if not plan.outputs:
        out.append(_issue("/outputs", "required_missing", "方案没有任何目标端"))
    if not plan.field_mappings:
        out.append(
            _issue(
                "/field_mappings",
                "required_missing",
                "方案没有任何字段映射；若确实要整表复制，也请显式列出字段——"
                "隐式整表复制会在源表加列时静默改变目标表结构",
            )
        )
    for idx, item in enumerate(plan.inputs):
        if item.kind == "table" and not item.table:
            out.append(_issue(f"/inputs/{idx}/table", "required_missing", "源端形态是表，但没给表名"))
        if item.kind == "query" and not item.query:
            out.append(
                _issue(f"/inputs/{idx}/query", "required_missing", "源端形态是自定义查询，但查询是空的")
            )
        if item.kind == "file" and not item.path:
            out.append(_issue(f"/inputs/{idx}/path", "required_missing", "源端形态是文件，但没给路径"))
    for idx, out_item in enumerate(plan.outputs):
        if not out_item.table:
            out.append(_issue(f"/outputs/{idx}/table", "required_missing", "目标端没给表名"))
    return out


def _check_duplicate_ids(plan: EtlPlanModel) -> list[PlanIssue]:
    """同一个命名空间内 id 不得重复。

    **命名空间分开算**是有意的：``inputs`` 与 ``outputs`` 都叫 ``t1``、
    或者一个 filter 与一个 quality rule 同名，都不构成歧义（引用处语法不同），
    把它们一起报会制造出并不存在的错误。
    """
    out: list[PlanIssue] = []
    namespaces: list[tuple[str, list[str]]] = [
        ("inputs", [i.id for i in plan.inputs]),
        ("outputs", [o.id for o in plan.outputs]),
        ("transforms", [t.id for t in plan.transforms]),
        ("filters", [f.id for f in plan.filters]),
        ("quality_rules", [r.id for r in plan.quality_rules]),
        ("dag.nodes", [n.id for n in plan.dag.nodes]),
        ("dag.edges", [e.id for e in plan.dag.edges]),
    ]
    for section, ids in namespaces:
        seen: set[str] = set()
        for idx, item_id in enumerate(ids):
            if item_id in seen:
                out.append(
                    _issue(f"/{section}/{idx}/id", "duplicate_id", f"{section} 里 id={item_id!r} 出现了多次")
                )
            seen.add(item_id)
    return out


def _check_mappings(plan: EtlPlanModel) -> list[PlanIssue]:
    """映射自身的自洽性：目标字段不得重复、质量规则引用必须存在。"""
    out: list[PlanIssue] = []
    rule_ids = {r.id for r in plan.quality_rules}
    seen_targets: dict[str, int] = {}

    for idx, mapping in enumerate(plan.field_mappings):
        # 两个源字段写进同一个目标字段 = **静默丢数据**：后写的覆盖先写的，
        # 而两边的 Transform 都「执行成功」了。这类错误不报出来最难查。
        if mapping.target_field in seen_targets:
            out.append(
                _issue(
                    f"/field_mappings/{idx}/target_field",
                    "dangling_ref",
                    f"目标字段 {mapping.target_field!r} 已被"
                    f"第 {seen_targets[mapping.target_field]} 条映射占用",
                )
            )
        else:
            seen_targets[mapping.target_field] = idx

        for rid in mapping.quality_rule_ids:
            if rid not in rule_ids:
                out.append(
                    _issue(
                        f"/field_mappings/{idx}/quality_rule_ids",
                        "dangling_ref",
                        f"引用了不存在的质量规则 id={rid!r}",
                    )
                )
    return out


def _check_transforms(plan: EtlPlanModel) -> list[PlanIssue]:
    """派生列：输入要么是源字段、要么是别的派生列的输出；输出必须有人消费。"""
    out: list[PlanIssue] = []
    produced = {t.output_field for t in plan.transforms}
    source_fields = {m.source_field for m in plan.field_mappings}
    consumed = {m.target_field for m in plan.field_mappings}
    for other in plan.transforms:
        consumed |= set(other.input_fields)

    for idx, transform in enumerate(plan.transforms):
        if not transform.input_fields and transform.kind != "constant":
            out.append(
                _issue(
                    f"/transforms/{idx}/input_fields",
                    "required_missing",
                    f"派生列 {transform.output_field!r}（{transform.kind}）没有输入字段",
                )
            )
        for field in transform.input_fields:
            if field not in source_fields and field not in produced:
                out.append(
                    _issue(
                        f"/transforms/{idx}/input_fields",
                        "dangling_ref",
                        f"输入字段 {field!r} 既不是源字段，也不是任何派生列的输出",
                    )
                )
        if transform.output_field not in consumed:
            out.append(
                _issue(
                    f"/transforms/{idx}/output_field",
                    "transform_unused",
                    f"派生列 {transform.output_field!r} 算出来了却没人用——"
                    "它会被渲染成 SQL 再被丢弃，白跑一遍且掩盖真实意图",
                )
            )
    return out


def _check_quality_rules(plan: EtlPlanModel) -> list[PlanIssue]:
    out: list[PlanIssue] = []
    known = {m.target_field for m in plan.field_mappings} | {t.output_field for t in plan.transforms}
    for idx, rule in enumerate(plan.quality_rules):
        if rule.field not in known:
            out.append(
                _issue(
                    f"/quality_rules/{idx}/field",
                    "quality_rule_field_missing",
                    f"质量规则挂在 {rule.field!r} 上，但它不在结果集里",
                )
            )
    return out


def _check_dag(plan: EtlPlanModel) -> list[PlanIssue]:
    """拓扑图：引用闭合（节点/边指向存在的东西）+ 无环。"""
    out: list[PlanIssue] = []
    node_ids = {n.id for n in plan.dag.nodes}
    known_refs = (
        {i.id for i in plan.inputs}
        | {o.id for o in plan.outputs}
        | {t.id for t in plan.transforms}
        | {f.id for f in plan.filters}
        | {r.id for r in plan.quality_rules}
    )

    for idx, node in enumerate(plan.dag.nodes):
        if node.ref and node.ref not in known_refs:
            out.append(_issue(f"/dag/nodes/{idx}/ref", "dangling_ref", f"节点指向不存在的对象 {node.ref!r}"))
    for idx, edge in enumerate(plan.dag.edges):
        for side in ("source", "target"):
            node_id = getattr(edge, side)
            if node_id not in node_ids:
                out.append(
                    _issue(
                        f"/dag/edges/{idx}/{side}",
                        "dangling_ref",
                        f"边引用了不存在的节点 {node_id!r}",
                    )
                )

    cycle = _find_cycle(nodes=node_ids, edges=[(e.source, e.target) for e in plan.dag.edges])
    if cycle:
        # 只报**环上的节点**而不是每一条边：一条边报一次的话，一个三方环会产生
        # 三条内容一样的 finding，反而看不出这是个环
        out.append(
            _issue(
                "/dag/edges",
                "dag_cycle",
                "拓扑图存在环：" + " → ".join(cycle) + "；数据流必须是有向无环图",
            )
        )
    return out


def _find_cycle(*, nodes: set[str], edges: list[tuple[str, str]]) -> list[str]:
    """返回一条环上的节点序列（含回到起点的闭合），无环返回空。

    用迭代式 DFS 而不是递归：Plan 的规模由 LLM 生成，节点数不可控，
    递归深度在恶意/异常输入上会直接把进程打崩（``RecursionError`` 是**未捕获**的
    编程错误类异常，会被上层当成 500 而不是「方案有问题」）。
    """
    adjacency: dict[str, list[str]] = {node: [] for node in nodes}
    for src, dst in edges:
        if src in adjacency:
            adjacency[src].append(dst)

    WHITE, GREY, BLACK = 0, 1, 2
    color: dict[str, int] = dict.fromkeys(nodes, WHITE)
    for root in sorted(nodes):
        if color[root] != WHITE:
            continue
        stack: list[tuple[str, int]] = [(root, 0)]
        path: list[str] = []
        while stack:
            node, idx = stack.pop()
            if idx == 0:
                color[node] = GREY
                path.append(node)
            if idx < len(adjacency[node]):
                stack.append((node, idx + 1))
                nxt = adjacency[node][idx]
                if color[nxt] == GREY:
                    start = path.index(nxt)
                    return [*path[start:], nxt]
                if color[nxt] == WHITE:
                    stack.append((nxt, 0))
            else:
                color[node] = BLACK
                if path and path[-1] == node:
                    path.pop()
    return []


def _check_budget(plan: EtlPlanModel) -> list[PlanIssue]:
    """预算必须为正。比率的上界由模型层（``le=1``/``le=100``）保证，这里只补语义检查。"""
    out: list[PlanIssue] = []
    budget = plan.budget
    checks = (
        ("/budget/max_read_rows", budget.max_read_rows, "最大读取行数"),
        ("/budget/max_write_bytes", budget.max_write_bytes, "最大写入字节数"),
        ("/budget/max_duration_sec", budget.max_duration_sec, "最长运行时长"),
        ("/budget/max_amplification_ratio", budget.max_amplification_ratio, "放大比上限"),
        ("/budget/max_reject_rate", budget.max_reject_rate, "拒绝率上限"),
    )
    for path, value, label in checks:
        if value <= 0:
            out.append(_issue(path, "budget_nonpositive", f"{label}必须大于 0，当前是 {value}"))
    if budget.max_reject_rate >= 1:
        out.append(
            _issue(
                "/budget/max_reject_rate",
                "budget_nonpositive",
                "拒绝率上限不能达到 1——那等于「哪怕全部数据都是脏的也放行」",
            )
        )
    return out
