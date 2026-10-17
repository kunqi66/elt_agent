"""版本间结构化 Diff（be-12 §6.4）。

**只做展示**：不回写任何指纹、不判门禁、不碰数据库。两个版本的指纹是否相同由
``artifact_sha256`` 那一侧决定；这里回答的是「人看起来，到底改了什么」。

## 与 be-12 §6.4 的差异登记（以冻结的前端为准）

``frontend/src/api/pipeline.ts`` 的 ``fetchVersionDiff`` 已经把返回形状钉死：

```ts
GET /pipeline-versions/diff?base_version_id=&target_version_id=
  -> { entries: Array<{ path: string; kind: 'added' | 'removed' | 'changed';
                        before?: string; after?: string }> }
```

``frontend/src/views/pipeline/components/VersionDiffDialog.vue`` 按 ``path`` / ``kind`` /
``before`` / ``after`` 四列渲染。be-12 §6.4 的 ``VersionDiff``（带 ``sections`` /
``from_version_no`` / ``sha_changed`` / ``locator`` 联合类型）与它对不上，按仓库既有口径
（见 ``engines/plan/schema.py`` 开头的差异表）**以前端为准**，差异逐条登记如下：

| be-12 §6.4 的写法 | 本文件的写法 | 原因 |
|---|---|---|
| `VersionDiff.sections` 分段数组 | `VersionDiff.entries` 一维平表 | 分段靠 `path` 前缀表达 |
| `from_version_no` / `sha_changed` 等 | **不设** | 版本号与指纹是路由层的事 |
| `FieldDiff.locator`（可辨识联合） | `DiffEntry.path: str` | 前端渲染的是字符串路径列 |
| `kind: added / removed / changed` | 同左 | 一致。别写成 be-11 §6.7 的 `modified` |
| `hocon` 段带 `hocon_path` | 每个 hunk 一条，`path = "hocon:<旧行号>"` | 全文比对由前端自己做 |

## `path` 的约定

- Plan 字段：**无前缀**，如 `field_mappings[3].target_type`、`inputs[0].table`
  （与 be-12 §6.4 的 locator 示例一致）。
- 数组：元素带 `id` 时按 `id` 匹配（`quality_contract.rules[E_NULL_PK].params`），
  否则按下标（`inputs[0]`）。
- 其余部分：带段名前缀，`quality_contract.*` / `supervision.*` /
  `source_metadata_profile_sha256`；HOCON 用 `hocon:<行号>`。

Plan 段**跳过** `quality_rules` 与 `budget` 两个顶层键：它们分别由
`quality_contract` 与 `supervision` 段表达，不跳会让同一处改动在 Diff 里出现两次。
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from core.ids import canonical_json
from engines.fingerprint.parts import ArtifactParts

#: 变更种类。必须与 ``frontend/src/types`` 里 DiffEntry 的联合逐字一致。
DiffKind = Literal["added", "removed", "changed"]

#: 段名。``all`` 之外都可单独请求（be-12 §3.1 的 ``?section=``，前端目前不发这个参数）。
DiffSection = Literal["etl_plan", "hocon", "quality_contract", "supervision", "source_metadata"]

_SECTIONS: tuple[str, ...] = ("etl_plan", "hocon", "quality_contract", "supervision", "source_metadata")
_ALL = "all"

#: be-12 §3.1 路由用的简写 → 本模块段名。
_SECTION_ALIASES: dict[str, str] = {
    "plan": "etl_plan",
    "quality": "quality_contract",
}

#: Plan 段要跳过的顶层键（由专门的两段表达，理由见模块 docstring）。
_PLAN_KEYS_HANDLED_ELSEWHERE: frozenset[str] = frozenset({"quality_rules", "budget"})

#: ``@@ -42,3 +42,5 @@`` 里的旧文件起始行。
_HUNK_HEADER_RE = re.compile(r"^@@ -(\d+)(?:,\d+)? \+\d+(?:,\d+)? @@")

#: HOCON hunk 折叠成一个 entry 时，before/after 的最大行数（防止一个 huge hunk 撑爆前端表格）。
_HUNK_MAX_LINES = 200


class DiffEntry(BaseModel):
    """一行差异（前端 ``entries[i]``）。

    ``before`` / ``after`` 用 ``None`` 表示「这一侧不存在」；序列化时按前端的可选字段
    （``before?`` / ``after?``）省略。值为 ``null`` 时会以字符串 ``"null"`` 出现——
    「键不存在」与「键的值是 null」是两件事，不能都用一个 ``None`` 表示。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    kind: DiffKind
    before: str | None = None
    after: str | None = None


class VersionDiff(BaseModel):
    """两个版本之间的全部差异。``entries`` 为空即「制品条目完全一致」。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    entries: list[DiffEntry] = Field(default_factory=list)


def version_diff(
    old: ArtifactParts | Mapping[str, Any],
    new: ArtifactParts | Mapping[str, Any],
    *,
    section: str = _ALL,
) -> VersionDiff:
    """逐段比较两份制品。``section`` 取段名或 ``"all"``（也接受 ``plan`` / ``quality`` 简写）。"""
    left = _as_parts(old)
    right = _as_parts(new)
    selected = _resolve_sections(section)

    entries: list[DiffEntry] = []
    if "etl_plan" in selected:
        entries.extend(_diff_plan(left.etl_plan, right.etl_plan))
    if "hocon" in selected and left.hocon_content != right.hocon_content:
        entries.extend(_diff_hocon(left.hocon_content, right.hocon_content))
    if "quality_contract" in selected:
        entries.extend(_diff_value(left.quality_contract, right.quality_contract, "quality_contract"))
    if "supervision" in selected:
        entries.extend(_diff_value(left.supervision, right.supervision, "supervision"))
    if "source_metadata" in selected and left.source_metadata_sha256 != right.source_metadata_sha256:
        entries.append(
            DiffEntry(
                path="source_metadata_profile_sha256",
                kind="changed",
                before=left.source_metadata_sha256,
                after=right.source_metadata_sha256,
            )
        )
    return VersionDiff(entries=entries)


def unified_text_diff(old_text: str, new_text: str, *, fromfile: str, tofile: str) -> str:
    """两份文本的 unified diff（展示用）。相同则返回空串。

    ``lineterm=""`` 让每行不带自己的换行符，拼接后行首的 ``---`` / ``+++`` / ``@@``
    是 unified diff 的**格式标记**，不是内容——看到它们属正常。
    """
    if old_text == new_text:
        return ""
    return "\n".join(
        difflib.unified_diff(
            old_text.splitlines(),
            new_text.splitlines(),
            fromfile=fromfile,
            tofile=tofile,
            lineterm="",
        )
    )


def hocon_unified_diff(old_text: str, new_text: str) -> str:
    """HOCON 全文的 unified diff 文本（自动修复每轮的 ``hocon_diff`` 用它）。"""
    return unified_text_diff(old_text, new_text, fromfile="a/hocon", tofile="b/hocon")


# ---------------------------------------------------------------- 内部


def _as_parts(value: ArtifactParts | Mapping[str, Any]) -> ArtifactParts:
    if isinstance(value, ArtifactParts):
        return value
    return ArtifactParts.model_validate(value)


def _resolve_sections(section: str) -> frozenset[str]:
    if section == _ALL:
        return frozenset(_SECTIONS)
    canonical = _SECTION_ALIASES.get(section, section)
    if canonical not in _SECTIONS:
        raise ValueError(f"未知的 diff 段：{section!r}（可选 {(*_SECTIONS, _ALL)}）")
    return frozenset({canonical})


def _stringify(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list, tuple)):
        return canonical_json(value)
    return str(value)


def _diff_plan(old: Mapping[str, Any], new: Mapping[str, Any]) -> list[DiffEntry]:
    left = {k: v for k, v in old.items() if k not in _PLAN_KEYS_HANDLED_ELSEWHERE}
    right = {k: v for k, v in new.items() if k not in _PLAN_KEYS_HANDLED_ELSEWHERE}
    return _diff_dict(left, right, "")


def _diff_value(old: Any, new: Any, path: str) -> list[DiffEntry]:
    if isinstance(old, dict) and isinstance(new, dict):
        return _diff_dict(old, new, path)
    if isinstance(old, list) and isinstance(new, list):
        return _diff_list(old, new, path)
    if _stringify(old) == _stringify(new):
        return []
    return [DiffEntry(path=path, kind="changed", before=_stringify(old), after=_stringify(new))]


def _diff_dict(old: Mapping[str, Any], new: Mapping[str, Any], path: str) -> list[DiffEntry]:
    entries: list[DiffEntry] = []
    for key in sorted(set(old) | set(new), key=str):
        child = f"{path}.{key}" if path else str(key)
        if key not in old:
            entries.append(DiffEntry(path=child, kind="added", before=None, after=_stringify(new[key])))
        elif key not in new:
            entries.append(DiffEntry(path=child, kind="removed", before=_stringify(old[key]), after=None))
        else:
            entries.extend(_diff_value(old[key], new[key], child))
    return entries


def _diff_list(old: list[Any], new: list[Any], path: str) -> list[DiffEntry]:
    # 带 id 的对象数组按 id 匹配：往中间插一条不该让其后所有元素都变成「变更」
    if _is_id_keyed(old) and _is_id_keyed(new):
        return _diff_list_by_id(old, new, path)
    entries: list[DiffEntry] = []
    for index in range(max(len(old), len(new))):
        child = f"{path}[{index}]"
        if index >= len(old):
            entries.append(DiffEntry(path=child, kind="added", before=None, after=_stringify(new[index])))
        elif index >= len(new):
            entries.append(DiffEntry(path=child, kind="removed", before=_stringify(old[index]), after=None))
        else:
            entries.extend(_diff_value(old[index], new[index], child))
    return entries


def _is_id_keyed(items: list[Any]) -> bool:
    # 空列表视为「与 id 匹配兼容」（``all`` 对空序列为真），这样「整段被删光」仍按 id 报。
    return all(isinstance(item, dict) and "id" in item for item in items)


def _diff_list_by_id(old: list[Any], new: list[Any], path: str) -> list[DiffEntry]:
    old_by_id = {str(item["id"]): item for item in old}
    new_by_id = {str(item["id"]): item for item in new}
    entries: list[DiffEntry] = []
    for item_id in sorted(set(old_by_id) | set(new_by_id)):
        child = f"{path}[{item_id}]"
        if item_id not in old_by_id:
            after = _stringify(new_by_id[item_id])
            entries.append(DiffEntry(path=child, kind="added", before=None, after=after))
        elif item_id not in new_by_id:
            before = _stringify(old_by_id[item_id])
            entries.append(DiffEntry(path=child, kind="removed", before=before, after=None))
        else:
            entries.extend(_diff_value(old_by_id[item_id], new_by_id[item_id], child))
    return entries


def _hocon_hunks(old_text: str, new_text: str) -> list[tuple[str, list[str], list[str]]]:
    """把 unified diff 按 hunk 归组 → ``(hunk 头, 删除行, 新增行)``。"""
    hunks: list[tuple[str, list[str], list[str]]] = []
    header: str | None = None
    removed: list[str] = []
    added: list[str] = []
    for line in difflib.unified_diff(
        old_text.splitlines(),
        new_text.splitlines(),
        fromfile="a/hocon",
        tofile="b/hocon",
        lineterm="",
    ):
        if line.startswith("@@"):
            if header is not None:
                hunks.append((header, removed, added))
            header, removed, added = line, [], []
        elif header is None:
            continue  # --- / +++ 文件头
        elif line.startswith("+"):
            added.append(line[1:])
        elif line.startswith("-"):
            removed.append(line[1:])
    if header is not None:
        hunks.append((header, removed, added))
    return hunks


def _clip(lines: list[str]) -> list[str]:
    if len(lines) <= _HUNK_MAX_LINES:
        return lines
    return [*lines[:_HUNK_MAX_LINES], f"…（另有 {len(lines) - _HUNK_MAX_LINES} 行）"]


def _diff_hocon(old_text: str, new_text: str) -> list[DiffEntry]:
    entries: list[DiffEntry] = []
    for header, removed, added in _hocon_hunks(old_text, new_text):
        match = _HUNK_HEADER_RE.match(header)
        line_no = int(match.group(1)) if match is not None else 1
        if removed and added:
            kind: DiffKind = "changed"
        elif removed:
            kind = "removed"
        else:
            kind = "added"
        entries.append(
            DiffEntry(
                path=f"hocon:{line_no}",
                kind=kind,
                before="\n".join(_clip(removed)) if removed else None,
                after="\n".join(_clip(added)) if added else None,
            )
        )
    return entries
