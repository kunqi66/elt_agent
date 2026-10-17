"""元数据脱敏（纯函数）。"""

from engines.masking.policy import (
    SensitiveKind,
    default_masking_policy,
    digest,
    infer_sensitivity,
    mask_rows,
    mask_value,
    merge_policy,
    narrow,
)

__all__ = [
    "SensitiveKind",
    "default_masking_policy",
    "digest",
    "infer_sensitivity",
    "mask_rows",
    "mask_value",
    "merge_policy",
    "narrow",
]
