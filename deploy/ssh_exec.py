"""在 ECS 上执行一段脚本并回显输出（纯本地进程）。

**为什么需要它**：本机（Windows 开发机）没有 Docker / WSL / Java，凡是「起容器、看容器、
进库、跑迁移」的动作都只能在 ECS 上做。`tunnel.py` 解决的是「端口转发」（控制面在本机、
基础设施在 ECS），本脚本解决的是「跑命令」。

**认证走 SSH 公钥**（`C:\\Users\\<user>\\.ssh\\id_rsa`，ECS 上已在
`/root/.ssh/authorized_keys` 里）：本机没有 sshpass/plink，原生 ssh 用口令是交互式的、
没法脚本化，而把口令写进命令行会进 shell 历史与进程列表（CLAUDE.md §5.2）。
公钥登录两个问题都没有。若哪天公钥失效，**不要**退回把口令写进参数的方式。

用法：
    uv run python deploy/ssh_exec.py "docker ps"
    uv run python deploy/ssh_exec.py - < 脚本文件      # 或 heredoc 喂 stdin
    echo 'free -g' | uv run python deploy/ssh_exec.py -

退出码 = 远端命令的退出码（远端失败时本进程也非 0，便于 `&&` 串联）。
"""

from __future__ import annotations

import os
import subprocess
import sys

HOST = os.environ.get("ELT_ECS_HOST", "60.205.138.167")
KEY = os.environ.get("ELT_ECS_KEY", os.path.expanduser("~/.ssh/id_rsa"))


def main() -> int:
    if len(sys.argv) < 2:
        sys.exit("用法: python ssh_exec.py <命令>   （命令为 - 时从 stdin 读）")

    # Windows 控制台默认 GBK：从 stdin 读脚本必须按字节读，交给 shell 决定编码
    if sys.argv[1] == "-":
        raw = sys.stdin.buffer.read()
    else:
        raw = " ".join(sys.argv[1:]).encode("utf-8", "surrogateescape")

    if not os.path.exists(KEY):
        sys.exit(f"找不到私钥 {KEY}（可用 ELT_ECS_KEY 指定）")

    # 远端用 `bash -s` 从 stdin 读脚本：这样多行脚本、引号、$ 都不会被本地 shell 先解释一遍
    proc = subprocess.run(
        [
            "ssh",
            "-i", KEY,
            "-o", "BatchMode=yes",           # 不弹口令提示：认证失败就直接失败
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", "ConnectTimeout=15",
            "-o", "ServerAliveInterval=30",
            HOST,
            "bash -s",
        ],
        input=raw,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    out = proc.stdout.decode("utf-8", "replace")
    err = proc.stderr.decode("utf-8", "replace")
    if out:
        sys.stdout.write(out if out.endswith("\n") else out + "\n")
    if err:
        sys.stderr.write(err if err.endswith("\n") else err + "\n")
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
