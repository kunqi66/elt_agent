"""单次执行能力令牌（Capability）的签发与校验。

安全语义（CLAUDE.md §5.6）：**一个 Capability 只能消费一次**。
本模块负责「这张令牌是不是真的、是不是给这次执行用的」；
「有没有被用过」由 :mod:`harness.replay` 负责——两者分离，因为前者是密码学问题
（可离线判断），后者是并发问题（必须落 Redis + PG）。

三条不可退让的规则：

1. **令牌原文永不落库**：入库的只有 ``token_sha256``（见 ``models.governance.Capability``）。
   库被读走时，攻击者拿到的是哈希，不能直接用于执行。
2. **算法白名单**：验签时显式 ``algorithms=["EdDSA"]``。PyJWT 的 ``alg`` 取自
   token header，不写白名单等于让攻击者用 ``alg=none`` 或把 EdDSA 降级成 HS256
   并用公钥当 HMAC 密钥——这是 JWT 最经典的漏洞。
3. **绑定必须逐项比对**：主体、工具、环境、制品指纹、租户。
   少比一项，就等于允许拿 A 项目的批准去执行 B 项目的作业。
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import jwt

from core.errors import (
    CapabilityExpiredError,
    CapabilityInvalidError,
    FingerprintMismatchError,
    TenantMismatchError,
)
from harness.errors import UnauthorizedToolError
from models.enums import Env, ToolName

ALGORITHM = "EdDSA"

# 容忍的时钟漂移。签发方（api）与校验方（worker）可能是不同容器，
# 容器时钟靠 NTP 对齐，秒级漂移是常态。给 5 s 是为了不把「几乎同时」判成「还没生效」；
# 但**绝不放宽 exp**——过期判定必须严格，否则单次令牌的窗口会被漂移悄悄拉长。
_CLOCK_SKEW_SEC = 5

_REQUIRED_CLAIMS = ["exp", "iat", "jti", "sub", "iss"]


@dataclass(frozen=True, slots=True)
class CapabilityClaims:
    jti: uuid.UUID
    sub: str
    subject_id: int
    tenant_id: int
    project_id: int
    tool: ToolName
    env: Env
    artifact_sha256: str
    preparation_id: int
    iss: str
    iat: datetime
    exp: datetime


@dataclass(frozen=True, slots=True)
class ExpectedBinding:
    """校验方**自己算出来**的期望值。

    关键点：这些值不能来自调用方传参，必须由服务端从 ``preparation`` 行重算。
    否则「比对」就退化成「拿调用方说的值去比调用方给的令牌」，毫无意义。

    ⚠️ 与 be-05 §3.2 的差异：这里**多一个** ``project_id``。原设计只列了 5 项，
    但 §7 拒绝矩阵第 10 条要求「``tenant_id``/``project_id`` 与上下文不符 → ``TENANT_MISMATCH``」，
    而 claim 里两者都在。少了它，同租户内跨项目拿令牌就能通过校验——
    项目边界在本系统里是数据隔离边界（每个项目独立数据源与目标库）。
    """

    subject_id: int
    tool: ToolName
    env: Env
    artifact_sha256: str
    tenant_id: int
    project_id: int


@dataclass(frozen=True, slots=True)
class IssuedCapability:
    token: str  # 字段名是领域术语「令牌」，不是硬编码口令
    claims: CapabilityClaims
    token_sha256: str


def _load_private_key(path: str | Path) -> Any:
    """读 PEM 私钥。文件不存在时让异常直接冒出来。

    刻意不 try/except 兜底成一个自签名密钥：那会让「密钥没挂载」表现为
    「签发成功但 worker 全部验签失败」，排查方向完全被带偏。

    返回 ``Any``：``load_pem_private_key`` 返回的是 cryptography 的联合类型，
    而 PyJWT 的 ``encode`` 各自接受一个更宽的联合。在这里写死具体类型只会
    逼着调用点加 ``cast``，没有实际收益。
    """
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    return load_pem_private_key(Path(path).read_bytes(), password=None)


def _load_public_key(path: str | Path) -> Any:
    from cryptography.hazmat.primitives.serialization import load_pem_public_key

    return load_pem_public_key(Path(path).read_bytes())


class CapabilityIssuer:
    """签发侧。只在 ``api`` 进程使用（be-13 Commit 步骤）。"""

    def __init__(self, *, private_key_path: str, key_id: str, issuer: str, ttl_sec: int) -> None:
        self._private_key_path = private_key_path
        self._key_id = key_id
        self._issuer = issuer
        self._ttl_sec = ttl_sec
        self._key: Any = None

    def _private_key(self) -> Any:
        # 懒加载：模块 import 期不读密钥文件，否则单测与 alembic 都会因为
        # 「本机没有 /run/secrets」而直接起不来
        if self._key is None:
            self._key = _load_private_key(self._private_key_path)
        return self._key

    @property
    def ttl_sec(self) -> int:
        return self._ttl_sec

    def issue(
        self,
        *,
        preparation_id: int,
        subject_id: int,
        tool: ToolName,
        env: Env,
        artifact_sha256: str,
        tenant_id: int,
        project_id: int,
        now: datetime,
    ) -> IssuedCapability:
        now = now.astimezone(UTC)
        exp = now + timedelta(seconds=self._ttl_sec)
        jti = uuid.uuid4()
        claims = CapabilityClaims(
            jti=jti,
            sub=f"user:{subject_id}",
            subject_id=subject_id,
            tenant_id=tenant_id,
            project_id=project_id,
            tool=tool,
            env=env,
            artifact_sha256=artifact_sha256,
            preparation_id=preparation_id,
            iss=self._issuer,
            iat=now,
            exp=exp,
        )
        payload = {
            "jti": str(jti),
            "sub": claims.sub,
            "subject_id": subject_id,
            "tenant_id": tenant_id,
            "project_id": project_id,
            "tool": tool.value,
            "env": env.value,
            "artifact_sha256": artifact_sha256,
            "preparation_id": preparation_id,
            "iss": self._issuer,
            # PyJWT 自己按 epoch 秒生成 iat/exp，这里显式传入以保证
            # 「exp - iat == ttl」这条不变量可被校验方复查
            "iat": int(now.timestamp()),
            "exp": int(exp.timestamp()),
        }
        token = jwt.encode(
            payload,
            self._private_key(),
            algorithm=ALGORITHM,
            headers={"kid": self._key_id},
        )
        return IssuedCapability(
            token=token,
            claims=claims,
            token_sha256=hashlib.sha256(token.encode()).hexdigest(),
        )


class CapabilityVerifier:
    """校验侧。

    必须在 ``worker`` 进程独立构造（只读公钥），这样即使 api 进程被攻陷、
    私钥泄漏，也不会顺带让 worker 失去判断能力。
    """

    def __init__(self, *, public_key_path: str, issuer: str, ttl_sec: int | None = None) -> None:
        self._public_key_path = public_key_path
        self._issuer = issuer
        self._ttl_sec = ttl_sec
        self._key: Any = None

    def _public_key(self) -> Any:
        if self._key is None:
            self._key = _load_public_key(self._public_key_path)
        return self._key

    def verify_signature(self, token: str, *, now: datetime) -> CapabilityClaims:
        """只验「这张令牌是不是我们签的、有没有过期」，**不比对绑定**。

        为什么要拆开：绑定里的 ``artifact_sha256`` 与 ``env`` 必须从
        ``preparation`` 行重算，而 ``preparation_id`` 恰恰在 claim 里——
        先验签拿到 claim，才能去查 preparation，再回来比对绑定。
        合成一步就会变成「拿调用方给的期望值去比调用方给的令牌」，等于没验。

        :class:`ToolBroker` 用的就是这条路径；直接调用 :meth:`verify` 走完整校验。
        """
        now = now.astimezone(UTC)
        try:
            raw = jwt.decode(
                token,
                self._public_key(),
                algorithms=[ALGORITHM],  # 算法白名单，见模块 docstring 第 2 条
                issuer=self._issuer,
                options={
                    "require": _REQUIRED_CLAIMS,
                    # exp/iat 的时限由下面显式判断，这里关掉 PyJWT 自带的宽松校验，
                    # 避免两套判定标准并存（一处允许漂移、一处不允许，结论会不一致）
                    "verify_exp": False,
                    "verify_iat": False,
                    "verify_nbf": False,
                },
            )
        except jwt.ExpiredSignatureError as exc:  # pragma: no cover - 已关掉自带校验
            raise CapabilityExpiredError() from exc
        except jwt.InvalidIssuerError as exc:
            raise CapabilityInvalidError("Capability 签发方不受信任") from exc
        except jwt.PyJWTError as exc:
            raise CapabilityInvalidError("Capability 令牌非法或签名校验失败") from exc

        try:
            iat = datetime.fromtimestamp(int(raw["iat"]), tz=UTC)
            exp = datetime.fromtimestamp(int(raw["exp"]), tz=UTC)
        except (KeyError, TypeError, ValueError) as exc:
            raise CapabilityInvalidError("Capability 缺少 iat/exp") from exc

        # 过期判定**严格**，不含漂移容忍
        if now >= exp:
            raise CapabilityExpiredError()
        if iat > now + timedelta(seconds=_CLOCK_SKEW_SEC):
            raise CapabilityInvalidError("Capability 尚未生效（签发时间在未来）")
        if self._ttl_sec is not None and int(exp.timestamp()) - int(iat.timestamp()) != self._ttl_sec:
            # 有效期被改长 = 有人拿着私钥签了一张长期令牌，等同于把「单次」变成「长期通行证」
            raise CapabilityInvalidError("Capability 有效期与配置不符")

        try:
            claims = CapabilityClaims(
                jti=uuid.UUID(str(raw["jti"])),
                sub=str(raw["sub"]),
                subject_id=int(raw["subject_id"]),
                tenant_id=int(raw["tenant_id"]),
                project_id=int(raw["project_id"]),
                tool=ToolName(str(raw["tool"])),
                env=Env(str(raw["env"])),
                artifact_sha256=str(raw["artifact_sha256"]),
                preparation_id=int(raw["preparation_id"]),
                iss=str(raw["iss"]),
                iat=iat,
                exp=exp,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise CapabilityInvalidError("Capability claim 结构非法") from exc

        return claims

    def verify(self, token: str, expected: ExpectedBinding, *, now: datetime) -> CapabilityClaims:
        """验签 + 绑定校验的完整形态。调用方已握有可信的期望值时用它。"""
        claims = self.verify_signature(token, now=now)
        self.assert_binding(claims, expected)
        return claims

    @staticmethod
    def assert_binding(claims: CapabilityClaims, expected: ExpectedBinding) -> None:
        """四项（含租户/项目）绑定逐项比对。单独暴露给 :class:`ToolBroker`。"""
        if claims.tenant_id != expected.tenant_id or claims.project_id != expected.project_id:
            raise TenantMismatchError("Capability 与当前执行上下文不属于同一租户/项目")
        if claims.subject_id != expected.subject_id:
            raise CapabilityInvalidError("Capability 的持有者与执行主体不符")
        if claims.tool != expected.tool:
            # 拿 seatunnel.submit 的令牌去调 publish_swap：这是越权，不是参数错误
            raise UnauthorizedToolError(
                f"Capability 绑定的是 {claims.tool.value}，本次调用 {expected.tool.value}"
            )
        if claims.env != expected.env:
            raise CapabilityInvalidError("Capability 的环境与 preparation 不符")
        if claims.artifact_sha256 != expected.artifact_sha256:
            raise FingerprintMismatchError("审批后制品指纹已变化，Capability 失效")


def verifier_from_settings() -> CapabilityVerifier:
    """按 ``Settings`` 造一个验签器。**全仓库只有这一份**。

    公钥路径由**私钥路径推出**（``core/keys.py generate`` 把 ``<name>.pub.pem``
    写在私钥旁边），而不是新加一个配置项：换了一对密钥时，只改私钥路径就够了；
    两边各配一个路径的话，私钥换了、公钥配置还指着旧文件，症状是
    **每一张令牌都验签失败**，而报出来的是「Capability 非法」——排查方向会被
    带到签发侧去（``harness/broker.py::_verify`` 也是这么报的）。

    为什么收在这里：worker 里的 ``tasks/execution.py`` 与 ``services/publish.py``
    都要造它。两处各推一次路径，就等于上面那个坑有两个落点，而其中一处漏改时
    没有任何用例会红。

    ``core.config`` 在函数内 import：本模块在 alembic / 单测里会被导入，
    那些场景不保证配置齐全（与 :class:`CapabilityIssuer` 懒读密钥文件同理）。
    """
    from core.config import settings

    public_path = Path(settings.CAPABILITY_SIGNING_KEY_PATH).with_suffix(".pub.pem")
    return CapabilityVerifier(
        public_key_path=str(public_path),
        issuer=settings.CAPABILITY_ISSUER,
        ttl_sec=settings.CAPABILITY_TTL_SEC,
    )
