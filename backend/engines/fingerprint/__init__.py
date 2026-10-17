"""制品指纹（be-12 §6）。

对外只有一件事：把五部分制品算成一个稳定的 ``sha256``。分层铁律要求它落在
``engines/``（纯函数、不碰 DB / Redis / 网络），因为指纹是审批与 Capability 共同引用的
信任根——算它的地方不能有副作用，否则「同一份制品」这个概念就没了。

``parts.py`` 定义参与集合与排除规则，``artifact.py`` 定义算法。
"""

from engines.fingerprint.artifact import (
    artifact_envelope,
    artifact_sha256,
    canonical_bytes,
    fingerprint_of,
    matches,
)
from engines.fingerprint.parts import (
    EXCLUDED_KEY_SUFFIXES,
    EXCLUDED_KEYS,
    FINGERPRINT_FORMAT_VERSION,
    ArtifactParts,
    build_parts,
    normalize_part,
    normalize_scalar,
)

__all__ = [
    "EXCLUDED_KEYS",
    "EXCLUDED_KEY_SUFFIXES",
    "FINGERPRINT_FORMAT_VERSION",
    "ArtifactParts",
    "artifact_envelope",
    "artifact_sha256",
    "build_parts",
    "canonical_bytes",
    "fingerprint_of",
    "matches",
    "normalize_part",
    "normalize_scalar",
]
