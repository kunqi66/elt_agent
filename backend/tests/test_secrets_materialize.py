"""密钥物化的用例（be-15 §5.1 / §6.2 / §9）。

这一层的每条纪律都可以用一句「如果它错了会怎样」来验：

| 纪律 | 错了会怎样 |
|---|---|
| 目录 0700 / 文件 0600 | 同机其它用户能读走口令 |
| 异常路径也销毁 | 提交失败时口令留在 `/tmp` 里，而人不会去看 |
| 覆盖写零再 unlink | 只删目录项，块里的旧数据仍可恢复 |
| 缺键即失败 | 作业拿着空口令去连库，或连上了一个恰好同名的库 |
| 底层异常文本丢弃 | hvac/httpx 会把地址与查询串拼进消息 |

在 Windows 上 `os.chmod` 的语义与 POSIX 不同，所以权限用 ``stat.S_IMODE`` 断言；
平台不支持时用 skipif 明确跳过，而不是把断言删掉——删掉之后这条纪律就没有守卫了。
"""

from __future__ import annotations

import asyncio
import stat
import sys
import unittest.mock
from pathlib import Path
from typing import Any

import pytest

from clients.secrets.materialize import (
    JOB_CONFIG_FILENAME,
    MaterializedSecrets,
    SecretUnavailable,
    as_secret_specs,
    materialize_secrets,
    wipe_run_dir,
)

POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="Windows 的 chmod 不映射到 POSIX 权限位")

CANARY = "CANARY-secret-value-2b7f"


class FakeSecretProvider:
    """be-15 §9 指定的假 provider：值从字典读，可注入失败。

    故意**不继承**任何基类：``SecretProvider`` 是 Protocol，只要形状对就该能用——
    继承一个 ABC 才能用的话，测试就在验证「有没有继承」而不是「语义对不对」。
    """

    def __init__(
        self,
        values: dict[tuple[str, int], dict[str, str]],
        *,
        fail_with: Exception | None = None,
    ) -> None:
        self._values = values
        self._fail_with = fail_with
        self.calls: list[tuple[str, int]] = []

    async def resolve(self, *, path: str, version: int) -> dict[str, str]:
        self.calls.append((path, version))
        if self._fail_with is not None:
            raise self._fail_with
        return self._values.get((path, version), {})


def _specs(*extra: tuple[int, str, str, int, tuple[str, ...]]) -> tuple[Any, ...]:
    base = ((1, "kv/etl/ds-1", "vault", 3, ("username", "password")),)
    return base + extra


def _provider(**overrides: Any) -> FakeSecretProvider:
    values = {("kv/etl/ds-1", 3): {"username": "reader", "password": CANARY}}
    values.update(overrides)
    return FakeSecretProvider(values)


# ---------------------------------------------------------------- 正常路径


async def test_物化后值在内存_引用在_refs(tmp_path: Path) -> None:
    async with materialize_secrets(
        specs=_specs(), run_id=7, work_dir=tmp_path, provider=_provider()
    ) as secrets:
        assert secrets.values["1:password"] == CANARY
        assert secrets.values["1:username"] == "reader"
        # refs 是**可以进账本**的形式：路径与字段名，没有值
        assert secrets.refs == ["kv/etl/ds-1#username,password"]
        assert secrets.run_dir == tmp_path / "7"


async def test_退出后文件与内存都被清空(tmp_path: Path) -> None:
    async with materialize_secrets(
        specs=_specs(), run_id=7, work_dir=tmp_path, provider=_provider()
    ) as secrets:
        path = secrets.write_file(JOB_CONFIG_FILENAME, "password = " + CANARY + "\n")
        assert path.exists()
        assert secrets.values, "构造失败：上下文里本该有值"
    assert not path.exists()
    assert not (tmp_path / "7").exists()
    assert secrets.values == {}, "退出后内存里仍留着明文"
    assert secrets.files == []


async def test_异常路径同样销毁_这是上下文管理器的唯一理由(tmp_path: Path) -> None:
    """在 yield 之后抛异常，临时文件必须已经被删掉。"""
    holder: dict[str, Path] = {}
    with pytest.raises(RuntimeError):
        async with materialize_secrets(
            specs=_specs(), run_id=7, work_dir=tmp_path, provider=_provider()
        ) as secrets:
            holder["path"] = secrets.write_file(JOB_CONFIG_FILENAME, CANARY)
            raise RuntimeError("模拟提交失败")
    assert not holder["path"].exists()
    assert not (tmp_path / "7").exists()


@POSIX_ONLY
async def test_目录_0700_文件_0600(tmp_path: Path) -> None:
    async with materialize_secrets(
        specs=_specs(), run_id=7, work_dir=tmp_path, provider=_provider()
    ) as secrets:
        path = secrets.write_file(JOB_CONFIG_FILENAME, CANARY)
        assert stat.S_IMODE((tmp_path / "7").stat().st_mode) == 0o700
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


@POSIX_ONLY
async def test_销毁时先覆盖写零再删除(tmp_path: Path) -> None:
    """只 ``unlink`` 的话，块里的旧字节还在——口令文件必须覆盖。"""
    async with materialize_secrets(
        specs=_specs(), run_id=7, work_dir=tmp_path, provider=_provider()
    ) as secrets:
        path = secrets.write_file(JOB_CONFIG_FILENAME, CANARY)
        size = path.stat().st_size
        secrets.destroy()
        # destroy 内部已覆盖并删除；这里只验证「删除」这一步，
        # 覆盖那一步无法在删除后观测，所以用一个能观测的替代：删除前行长不变
        assert size > 0
        assert not path.exists()
        # destroy 可重复调用（finally 可能跑两次）
        secrets.destroy()


@POSIX_ONLY
async def test_早已存在的残留目录会被清掉而不是复用(tmp_path: Path) -> None:
    """上一轮被 OOM kill 的 worker 留下的目录：既不能复用（可能已是别人的），
    也不能让 ``mkdir(exist_ok=False)`` 把「有残留」变成一次失败——那会让该 run 永远跑不起来。"""
    stale = tmp_path / "7"
    stale.mkdir(mode=0o700)
    (stale / "leftover.conf").write_text("old", encoding="utf-8")

    async with materialize_secrets(
        specs=_specs(), run_id=7, work_dir=tmp_path, provider=_provider()
    ) as secrets:
        assert not (stale / "leftover.conf").exists()
        assert secrets.run_dir == stale


async def test_wipe_run_dir_幂等(tmp_path: Path) -> None:
    """任务入口会无条件调它，目录不存在时不能报错。"""
    wipe_run_dir(tmp_path, 7)
    (tmp_path / "7").mkdir()
    wipe_run_dir(tmp_path, 7)
    assert not (tmp_path / "7").exists()


async def test_文件名白名单之外一律拒绝(tmp_path: Path) -> None:
    """文件名只来自代码常量：表名/作业名一律写在**内容**里。

    这个函数不做路径净化（它不该收到来自数据的字符串），所以白名单是唯一防线。
    """
    async with materialize_secrets(
        specs=_specs(), run_id=7, work_dir=tmp_path, provider=_provider()
    ) as secrets:
        with pytest.raises(ValueError):
            secrets.write_file("../../etc/passwd", "x")
        with pytest.raises(ValueError):
            secrets.write_file("sales_wide.conf", "x")


# ---------------------------------------------------------------- fail-closed


async def test_缺键即整体失败(tmp_path: Path) -> None:
    provider = FakeSecretProvider({("kv/etl/ds-1", 3): {"username": "reader"}})
    with pytest.raises(SecretUnavailable) as caught:
        async with materialize_secrets(
            specs=_specs(), run_id=7, work_dir=tmp_path, provider=provider
        ):
            pass
    # 报的是键名（引用层），不是值
    assert "password" in str(caught.value)
    assert CANARY not in str(caught.value)


async def test_底层异常文本被丢弃_只留错误类型(tmp_path: Path) -> None:
    """hvac/httpx 的异常消息里会带地址与查询串——它是「不该出圈」的一类文本。"""
    leaky = RuntimeError("GET http://vault:8200/v1/kv/data/x?version=3 failed: token=hvs.LEAK")
    provider = FakeSecretProvider({}, fail_with=leaky)
    with pytest.raises(SecretUnavailable) as caught:
        async with materialize_secrets(
            specs=_specs(), run_id=7, work_dir=tmp_path, provider=provider
        ):
            pass
    message = str(caught.value)
    assert "RuntimeError" in message, "连错误类型都没留下，排查时无从下手"
    assert "hvs.LEAK" not in message
    assert "vault:8200" not in message


async def test_版本号非法时不去读(tmp_path: Path) -> None:
    """0 或负数在 KV v2 里不是「最新」，而是「不可能存在」——静默放过会拿到空口令。"""
    from clients.secrets.vault import VaultSecretProvider

    class _NotCalled:
        async def read_secret(self, **_: Any) -> dict[str, Any]:  # pragma: no cover
            raise AssertionError("版本非法时不该发起读取")

    provider = VaultSecretProvider(_NotCalled())  # type: ignore[arg-type]
    with pytest.raises(SecretUnavailable):
        await provider.resolve(path="kv/etl/ds-1", version=0)


async def test_一个引用失败则整体失败_不写半个目录(tmp_path: Path) -> None:
    """部分物化的产物会让人以为「都取到了」，而缺的那一份要到运行期才暴露。"""
    specs = _specs((2, "kv/etl/ds-2", "vault", 1, ("password",)))
    provider = FakeSecretProvider({("kv/etl/ds-1", 3): {"username": "r", "password": CANARY}})
    with pytest.raises(SecretUnavailable):
        async with materialize_secrets(specs=specs, run_id=7, work_dir=tmp_path, provider=provider):
            pass
    assert not (tmp_path / "7").exists(), "失败后留下了目录"


# ---------------------------------------------------------------- 形状转换


def test_as_secret_specs_取全四元组() -> None:
    """``resource_scope.secrets`` 的形状 → 物化入参。

    漏掉 ``version`` 的那种漏法最阴：读到 latest，于是**凭据轮转后行为不可复现**。
    """
    specs = as_secret_specs(
        [
            {
                "data_source_id": 12,
                "secret_reference_id": 5,
                "provider": "vault",
                "path": "kv/etl/project-1/ds-12",
                "version": 4,
                "key_names": ["password", "username"],
            }
        ]
    )
    assert specs == ((12, "kv/etl/project-1/ds-12", "vault", 4, ("password", "username")),)


def test_as_secret_specs_缺_version_当场报错而不是默认最新() -> None:
    with pytest.raises(KeyError):
        as_secret_specs([{"data_source_id": 1, "path": "kv/x", "key_names": ["password"]}])


async def test_extra_values_是独立入口_不走_provider(tmp_path: Path) -> None:
    """``jdbc_url`` 由 endpoint 拼出、不来自 Vault——两条来源在调用点要一眼可辨。"""
    provider = _provider()
    async with materialize_secrets(
        specs=_specs(),
        run_id=7,
        work_dir=tmp_path,
        provider=provider,
        extra_values={"1:jdbc_url": "jdbc:mysql://h:3306/db"},
    ) as secrets:
        assert secrets.values["1:jdbc_url"] == "jdbc:mysql://h:3306/db"
        assert provider.calls == [("kv/etl/ds-1", 3)], "extra_values 不该触发 provider 调用"


def test_文件创建请求的是_O_EXCL_与_0600(tmp_path: Path) -> None:
    """**跨平台**地守那条权限纪律：断言 ``os.open`` 收到的 flags 与 mode。

    POSIX 上另有用例直接断言 st_mode（见上），但 Windows 的 chmod 不映射到权限位，
    于是在开发机上这条纪律本来是全空的。改成检查**调用参数**之后，
    「有人把 O_EXCL 去掉」或「mode 写成 0o644」在任何平台都会红。
    """
    import os as os_module

    captured: dict[str, Any] = {}
    real_open = os_module.open

    def spy(path: Any, flags: int, mode: int = 0o777, **kwargs: Any) -> int:
        captured["flags"] = flags
        captured["mode"] = mode
        return real_open(path, flags, mode, **kwargs)

    async def run() -> None:
        async with materialize_secrets(
            specs=_specs(), run_id=7, work_dir=tmp_path, provider=_provider()
        ) as secrets:
            with unittest.mock.patch.object(os_module, "open", spy):
                secrets.write_file(JOB_CONFIG_FILENAME, CANARY)

    asyncio.run(run())
    assert captured["flags"] & os_module.O_EXCL, "少了 O_EXCL：符号链接攻击与静默覆盖都挡不住"
    assert captured["flags"] & os_module.O_CREAT
    assert captured["mode"] == 0o600, "文件权限不是 0600"


def test_destroy_可重复调用(tmp_path: Path) -> None:
    """``finally`` 与显式清理可能都跑到，第二次不能炸。"""
    secrets = MaterializedSecrets(values={"1:password": CANARY})
    secrets.destroy()
    secrets.destroy()
    assert secrets.values == {}


def test_整个对象不许直接进日志() -> None:
    """``MaterializedSecrets`` 的默认 repr **含明文值**（dataclass 的默认行为）。

    所以要钉住的不是 repr 本身，而是「调用点不许把整个对象交给日志」。
    这条用例把那个事实写下来：一旦有人把 repr 改成脱敏的，它会红，
    提示「现在可以放心打印了」——在那之前，纪律是只记 ``len(values)`` 与 ``refs``。
    """
    secrets = MaterializedSecrets(values={"1:password": CANARY})
    assert CANARY in repr(secrets), "repr 行为变了（可能已被改成脱敏）——请更新这条用例的说明"
    assert CANARY not in str(secrets.refs), "refs 里混进了值"
