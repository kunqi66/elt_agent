"""HashiCorp Vault 客户端（KV v2 + AppRole）。

**本模块是全仓库唯一能读到明文凭据的地方**（CLAUDE.md §5.2）。
它暴露的每个方法都只回「调用方自己要求的那一份」，没有任何「列出全部」的接口——
一个能列 secrets 的方法迟早会被用在日志或调试接口里。

设计取舍：

1. **不缓存**。Vault 每次读是一次 HTTP 往返（本地网络约 1ms），
   而我们读取的频率极低（每次连通性测试/每次作业提交）。
   缓存换来的性能在这个量级上没有意义，代价却是「密钥轮换后旧值还在内存里」。
2. **hvac 是同步库**，所以所有调用都过 ``asyncio.to_thread``：
   在事件循环里直接发 HTTP 会阻塞整个 worker 的所有协程。
3. **错误只上抛类型化的 VaultError**，绝不把 hvac 的异常原样透出——
   它的异常消息里可能带 URL 与请求体。
"""

from __future__ import annotations

import asyncio
from typing import Any

import hvac
import structlog

from core.config import get_settings
from core.errors import AppError, ErrorCode

log = structlog.get_logger(__name__)


class VaultError(AppError):
    """Vault 不可用或读取失败。

    刻意**不带原始异常文本**：hvac 的报错里可能包含请求路径甚至响应体，
    而响应体就是凭据本身。原始异常仍通过 ``__cause__`` 保留，供服务端日志查看。
    """

    def __init__(
        self,
        message: str = "密钥服务不可用",
        *,
        code: ErrorCode = ErrorCode.INTERNAL_ERROR,
        http_status: int = 503,
    ) -> None:
        super().__init__(message, code=code, http_status=http_status)


class SecretNotFoundError(VaultError):
    def __init__(self, path: str, version: int | None = None) -> None:
        suffix = f"（版本 {version}）" if version is not None else ""
        super().__init__(f"密钥不存在：{path}{suffix}", code=ErrorCode.NOT_FOUND, http_status=404)


def build_secret_path(*, tenant_id: int, project_id: int, name: str) -> str:
    """``elt/{tenant}/{project}/{name}``。

    租户与项目**都在路径里**（be-03 §9 与 §3.2 的分歧在此收敛）：
    只按项目分路径的话，两个租户各自的「项目 11」会撞进同一个 Vault 路径，
    而 Vault 的 policy 只按路径前缀授权——那就等于把租户隔离交给了命名习惯。
    """
    return f"elt/{tenant_id}/{project_id}/{name}"


class VaultClient:
    """KV v2 读写。

    生命周期：应用启动时建一次（``app.state``），或由 Celery 任务按需建。
    内部无状态，可安全并发使用。
    """

    def __init__(
        self,
        *,
        addr: str,
        token: str | None = None,
        role_id: str | None = None,
        secret_id: str | None = None,
        mount: str = "secret",
        timeout: float = 5.0,
    ) -> None:
        self._addr = addr
        self._mount = mount
        self._timeout = timeout
        self._token = token
        self._role_id = role_id
        self._secret_id = secret_id

    # ------------------------------------------------------------ 构造

    @classmethod
    def from_settings(cls) -> VaultClient:
        settings = get_settings()
        return cls(
            addr=settings.VAULT_ADDR,
            # SecretStr 必须显式解包：直接传会把 "**********" 当 token 发出去
            token=settings.VAULT_TOKEN.get_secret_value() or None,
            role_id=settings.VAULT_ROLE_ID or None,
            secret_id=settings.VAULT_SECRET_ID.get_secret_value() or None,
            mount=settings.VAULT_KV_MOUNT,
            timeout=float(settings.VAULT_TIMEOUT_SEC),
        )

    def _client_sync(self) -> hvac.Client:
        """构造 hvac 客户端并完成认证。**同步**，只能在 to_thread 里调。"""
        client = hvac.Client(url=self._addr, timeout=self._timeout)
        if self._token:
            client.token = self._token
        elif self._role_id and self._secret_id:
            # AppRole：生产用这条。token 是运维手动排查时才用的逃生通道
            resp = client.auth.approle.login(role_id=self._role_id, secret_id=self._secret_id)
            client.token = resp["auth"]["client_token"]
        else:
            raise VaultError("Vault 未配置认证方式（token 或 AppRole 二选一）")
        return client

    async def _call(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        """把同步 hvac 调用挪到线程池。

        ``to_thread`` 而不是直接 await：hvac 没有异步实现，
        在事件循环里跑它会把整个进程卡住一个 RTT。
        """
        try:
            return await asyncio.to_thread(fn, *args, **kwargs)
        except hvac.exceptions.InvalidPath as exc:
            raise SecretNotFoundError(str(kwargs.get("path", "?"))) from exc
        except hvac.exceptions.Forbidden as exc:
            # 权限不足与「路径不存在」必须分开：前者是配置问题，后者是数据问题，
            # 混成一个错误码会让运维去查错方向
            raise VaultError("无权访问该密钥路径", code=ErrorCode.FORBIDDEN) from exc
        except hvac.exceptions.VaultError as exc:
            log.error("vault_call_failed", error_type=type(exc).__name__)
            raise VaultError() from exc

    # ------------------------------------------------------------ 读

    async def read_secret(self, *, path: str, version: int | None = None) -> dict[str, Any]:
        """读取一份密钥的**全部键值**。

        调用方必须紧接着把这些值用掉，不要存进任何对象属性或日志。
        """

        def _read() -> dict[str, Any]:
            client = self._client_sync()
            resp = client.secrets.kv.v2.read_secret_version(
                path=path, version=version, mount_point=self._mount, raise_on_deleted_version=True
            )
            data = resp["data"]
            return {
                "data": dict(data.get("data", {})),
                "version": int(data.get("metadata", {}).get("version", 1)),
            }

        # ``_call`` 回的是 Any（它要转发任意 hvac 调用的结果），
        # 这里显式标回真实类型，否则 mypy 会把 Any 一路漏给所有调用方
        result: dict[str, Any] = await self._call(_read)
        if not result["data"]:
            raise SecretNotFoundError(path, version)
        return result

    async def read_fields(
        self, *, path: str, version: int | None = None, key_names: tuple[str, ...] | list[str]
    ) -> dict[str, str]:
        """只取需要的几个键。

        比 ``read_secret`` 更常用于执行路径：执行器只需要 username/password，
        让它拿到整份密钥只会扩大泄漏面（多一个字段就多一处可能被写进日志的地方）。
        """
        payload = await self.read_secret(path=path, version=version)
        data: dict[str, Any] = payload["data"]
        missing = [k for k in key_names if k not in data]
        if missing:
            # 只报键名：Vault 里的路径与键名不是秘密，值才是
            raise VaultError(f"密钥缺少字段：{', '.join(missing)}", code=ErrorCode.VALIDATION_ERROR)
        return {k: str(data[k]) for k in key_names}

    # ------------------------------------------------------------ 写

    async def write_secret(self, *, path: str, values: dict[str, str], cas: int | None = None) -> int:
        """写入一份新版本，返回版本号。

        KV v2 天然是「写新版本」而不是覆盖，所以轮换天然可回退。
        ``cas``（check-and-set）可用于防并发覆盖，默认不传。
        """

        def _write() -> int:
            client = self._client_sync()
            resp = client.secrets.kv.v2.create_or_update_secret(
                path=path, secret=dict(values), mount_point=self._mount, cas=cas
            )
            return int(resp["data"]["version"])

        result = await self._call(_write)
        if not isinstance(result, int):  # pragma: no cover - 防御写法
            raise VaultError("Vault 未返回版本号")
        return result

    async def delete_metadata(self, *, path: str) -> None:
        """删除该路径的全部版本与元数据（数据源被真删时调用）。"""

        def _delete() -> None:
            client = self._client_sync()
            client.secrets.kv.v2.delete_metadata_and_all_versions(path=path, mount_point=self._mount)

        await self._call(_delete)

    async def health(self) -> bool:
        """Vault 是否可用且已解封。启动自检与 /health 用它。"""

        def _health() -> bool:
            client = hvac.Client(url=self._addr, timeout=self._timeout)
            return bool(client.sys.is_initialized() and not client.sys.is_sealed())

        try:
            return await asyncio.to_thread(_health)
        except Exception:
            return False


__all__ = ["VaultClient", "VaultError", "SecretNotFoundError", "build_secret_path"]
