"""把本地文件传到 ECS 的指定路径（SFTP，纯本地进程）。

**为什么需要它**：ECS 上的 ``/opt/elt_agent`` 是**拷贝**过去的（不是 git 仓库），
而部署动作（compose、init SQL、nginx 配置）都在那边执行。没有这个工具，唯一的
替代是用 ``ssh_exec.py`` 套一层 heredoc 把文件内容打过去——那份内容会进远端
shell 的 stdin，遇到 ``$`` / 引号 / 反引号就要靠多层转义，而转义错了的后果是
**写出一个语法正确但内容被悄悄改过的配置文件**（例如 compose 里的 ``$$`` 变成 ``$``）。

认证与 ``ssh_exec.py`` 一致：SSH 公钥，不走口令（CLAUDE.md §5.2：口令不进命令行、
不进文件）。

用法：
    uv run --with paramiko python deploy/push_file.py docker-compose.yml /opt/elt_agent/
    uv run --with paramiko python deploy/push_file.py deploy/doris/init/01.sql /opt/elt_agent/deploy/doris/init/

远端参数以 ``/`` 结尾表示目录（自动创建缺失的父目录）。
"""

from __future__ import annotations

import os
import posixpath
import sys

import paramiko

HOST = os.environ.get("ELT_ECS_HOST", "60.205.138.167")
USER = os.environ.get("ELT_ECS_USER", "root")
SSH_PORT = int(os.environ.get("ELT_ECS_PORT", "22"))
KEY = os.environ.get("ELT_ECS_KEY", os.path.expanduser("~/.ssh/id_rsa"))


def _mkdirs(sftp: paramiko.SFTPClient, directory: str) -> None:
    """逐级建目录（SFTP 没有 mkdir -p）。"""
    parts: list[str] = []
    current = directory
    while current and current != "/":
        parts.append(current)
        current = posixpath.dirname(current)
    for path in reversed(parts):
        try:
            sftp.stat(path)
        except FileNotFoundError:
            sftp.mkdir(path)


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        sys.stderr.write("用法: push_file.py <本地文件> <远端路径>\n")
        return 2

    local, remote = argv[1], argv[2]
    if not os.path.isfile(local):
        sys.stderr.write(f"本地文件不存在：{local}\n")
        return 2
    # Git Bash 会把以 ``/`` 开头的参数改写成 Windows 路径（``/opt/x`` → ``C:/Program Files/Git/opt/x``），
    # 于是文件被传到一个**没人看的地方**，而进程退出码仍是 0。这个判据看着像在防愚蠢错误，
    # 但它的失败模式是「部署说成功了、文件不在」，值得用一行换掉。
    if len(remote) > 1 and remote[1] == ":":
        sys.stderr.write(
            f"远端路径 {remote!r} 看着像被 Git Bash 改写过的 Windows 路径；"
            "请用 MSYS_NO_PATHCONV=1 重跑\n"
        )
        return 2
    if remote.endswith("/"):
        remote = posixpath.join(remote, os.path.basename(local))

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        # key_filename 让 paramiko 自己认密钥类型（RSA / Ed25519 都走这条），
        # 手工按文件名分支迟早会漏掉一种——而漏掉的症状是「连接被拒」，读起来像密码错
        client.connect(HOST, port=SSH_PORT, username=USER, key_filename=KEY)
        sftp = client.open_sftp()
        _mkdirs(sftp, posixpath.dirname(remote))
        sftp.put(local, remote)
        size = sftp.stat(remote).st_size
        sftp.close()
    finally:
        client.close()

    if size != os.path.getsize(local):
        # 只比大小就够发现「传了一半」；内容一致性由调用方的后续命令自证（compose config 等）
        sys.stderr.write(f"传输长度不一致：远端 {size} ≠ 本地 {os.path.getsize(local)}\n")
        return 1
    print(f"pushed {local} -> {remote} ({size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
