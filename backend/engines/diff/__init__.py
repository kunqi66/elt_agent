"""版本间 Diff（be-12 §6.4）。

纯展示用途：给定两份 ``ArtifactParts``，产出前端版本对比弹窗要的一维 ``entries``。
**不**计算指纹、**不**回写任何状态。
"""

from engines.diff.version_diff import (
    DiffEntry,
    DiffKind,
    DiffSection,
    VersionDiff,
    hocon_unified_diff,
    unified_text_diff,
    version_diff,
)

__all__ = [
    "DiffEntry",
    "DiffKind",
    "DiffSection",
    "VersionDiff",
    "hocon_unified_diff",
    "unified_text_diff",
    "version_diff",
]
