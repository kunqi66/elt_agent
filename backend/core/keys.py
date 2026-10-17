"""Ed25519 签名密钥生成工具。

用途：Capability 执行令牌签名（harness/capability.py）与证据账本检查点签名（harness/ledger.py）。
两条密钥**必须分开**——用途隔离，且可独立轮转（账本密钥轮转后历史检查点仍可验证）。

用法：
    uv run python -m core.keys generate --kind capability --out ../secrets/capability_signing_key.pem
    uv run python -m core.keys generate --kind ledger     --out ../secrets/ledger_signing_key.pem
    uv run python -m core.keys show --key ../secrets/capability_signing_key.pem   # 查看公钥与指纹

安全约定：私钥文件权限 0600、目录不进仓库；容器内以只读方式挂载到 /run/secrets。
"""

from __future__ import annotations

import argparse
import os
import stat
import sys
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519


def _write_private(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # 先以 0600 创建再写入，避免中间态被其他用户读到
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    path.chmod(0o600)


def generate(kind: str, out: Path, force: bool) -> int:
    if out.exists() and not force:
        print(f"[拒绝] 文件已存在：{out}（覆盖会作废所有既有签名，确需重生成请加 --force）", file=sys.stderr)
        return 1

    private_key = ed25519.Ed25519PrivateKey.generate()
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )

    _write_private(out, private_pem)
    pub_path = out.with_suffix(".pub.pem")
    pub_path.write_bytes(public_pem)

    print(f"[完成] {kind} 私钥 → {out} (0600)")
    print(f"[完成] {kind} 公钥 → {pub_path}")
    print("[提示] 请把私钥挂载进容器：./secrets:/run/secrets:ro")
    if kind == "capability":
        print("[提示] 若已签发过令牌，轮转密钥会使其全部失效——应在无活跃执行时轮转。")
    return 0


def show(key: Path) -> int:
    private_key = serialization.load_pem_private_key(key.read_bytes(), password=None)
    if not isinstance(private_key, ed25519.Ed25519PrivateKey):
        print("[错误] 不是 Ed25519 私钥", file=sys.stderr)
        return 1
    public_raw = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    import hashlib

    print(f"文件      : {key}")
    print(f"公钥(raw) : {public_raw.hex()}")
    print(f"公钥指纹  : sha256:{hashlib.sha256(public_raw).hexdigest()[:32]}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="core.keys", description="Ed25519 密钥管理")
    sub = parser.add_subparsers(dest="cmd", required=True)

    gen = sub.add_parser("generate", help="生成密钥对")
    gen.add_argument("--kind", choices=["capability", "ledger"], required=True)
    gen.add_argument("--out", type=Path, required=True)
    gen.add_argument("--force", action="store_true")

    sho = sub.add_parser("show", help="显示公钥与指纹")
    sho.add_argument("--key", type=Path, required=True)

    args = parser.parse_args(argv)
    if args.cmd == "generate":
        return generate(args.kind, args.out, args.force)
    return show(args.key)


if __name__ == "__main__":
    raise SystemExit(main())
