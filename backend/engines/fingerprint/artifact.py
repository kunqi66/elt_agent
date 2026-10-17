"""制品指纹算法（be-12 §6.3）。

    sha256 = SHA256( canonical_json( artifact_envelope(parts) ) )

三个函数的分工：

- :func:`artifact_envelope` —— 拼信封：剥排除项、把 ``Decimal`` 转定标字符串（be-12 §6.3
  的 (a)(b)(c)）。它**不做**键排序。
- :func:`canonical_bytes` —— 唯一入口 ``canonical_json`` 的字节形式（键排序、无空白、
  UTF-8 不转义）。
- :func:`artifact_sha256` —— 上面两步的组合。

``artifact_sha256`` 是本系统**唯一**被「审批结论 / Capability / 执行」共同引用的制品标识，
全仓 grep 只应命中本目录一处（be-12 §8「指纹即信任根」）。任何绕过它手工拼指纹的代码
都视为安全缺陷。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from core.ids import canonical_json, sha256_hex
from engines.fingerprint.parts import (
    FINGERPRINT_FORMAT_VERSION,
    ArtifactParts,
    build_parts,
    normalize_part,
)
from engines.hocon.types import RenderedHocon
from engines.plan.schema import EtlPlanModel


def canonical_bytes(payload: Any) -> bytes:
    """规范化 JSON 的字节形式。指纹的**唯一**输入形态。"""
    return canonical_json(payload).encode("utf-8")


def artifact_envelope(parts: ArtifactParts) -> dict[str, Any]:
    """把五部分拼成信封（**规范化前**的结构，键序由 ``canonical_json`` 决定）。

    键名对齐 be-12 §6.2 的信封示例：``v`` / ``etl_plan`` / ``hocon`` /
    ``quality_contract`` / ``supervision_contract`` / ``source_metadata_profile_sha256``。

    ``hocon_content`` 原样进入，**不做任何 trim / 规范化**——HOCON 的字节就是它的身份。
    """
    return {
        "v": FINGERPRINT_FORMAT_VERSION,
        "etl_plan": normalize_part(parts.etl_plan),
        "hocon": parts.hocon_content,
        "quality_contract": normalize_part(parts.quality_contract),
        "supervision_contract": normalize_part(parts.supervision),
        "source_metadata_profile_sha256": parts.source_metadata_sha256,
    }


def artifact_sha256(parts: ArtifactParts) -> str:
    """制品指纹。确定性：同一份 :class:`ArtifactParts` 永远得到同一个十六进制串。"""
    return sha256_hex(canonical_bytes(artifact_envelope(parts)))


def matches(stored_sha256: str, parts: ArtifactParts) -> bool:
    """复核：库里的 ``sha256`` 与按**库内制品**重算的是否一致（``/verify`` 的判据）。

    注意它验证的是「库内制品未被篡改」，**不是**「用当前代码能复现同样的制品」
    （渲染器升级后历史版本不再能重渲染，但指纹仍然应当对得上，be-12 §6.5）。
    """
    return stored_sha256 == artifact_sha256(parts)


def fingerprint_of(
    plan: EtlPlanModel | Mapping[str, Any],
    hocon_content: str | RenderedHocon,
    quality_contract: Mapping[str, Any],
    supervision: Mapping[str, Any],
    source_metadata_sha256: str,
) -> str:
    """便捷入口：直接从五部分的原始形态算指纹，省掉调用方手工 ``ArtifactParts(...)``。"""
    return artifact_sha256(
        build_parts(plan, hocon_content, quality_contract, supervision, source_metadata_sha256)
    )
