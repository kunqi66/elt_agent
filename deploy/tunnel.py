"""把本地端口转发到 ECS 的 127.0.0.1 端口（SSH 隧道，纯本地进程）。

**这是「控制面在本地、基础设施在 ECS」这条开发路径的第一块**：API 与 worker
在本机跑（改一行代码就重启，不用重建镜像），PG / Redis / Vault / 演示 MySQL
维持 ECS 上的容器不动。部署路径仍是 compose（见 README），两者共用同一份 .env
口径，区别只在地址是不是隧道的本地端口。

为什么需要它：ECS 的安全组只放行了 22 / 8000 / 19530，PG、Redis 这些
服务虽然监听 0.0.0.0，但从公网**连不上**（这正是应该的）。而本机要跑控制面
就必须连 PG/Redis，所以只有两条路：改安全组（等于把数据库开到公网）或走隧道。
选后者。

为什么不用原生 `ssh -L`：本机没有 sshpass/plink，原生 ssh 的口令输入是
交互式的，没法脚本化。paramiko 能在内存里持有口令，不落任何文件，
也不需要在服务器上装任何东西。

**口令只从环境变量读**（``ELT_ECS_PASSWORD``），既不接受命令行参数也不写默认值——
写进参数会进 shell 历史与进程列表，写进默认值会进仓库（CLAUDE.md §5.2）。

用法：
    # 端口写 ECS 上容器发布到宿主机的端口，本地同号监听
    ELT_ECS_PASSWORD=... uv run --with paramiko python deploy/tunnel.py 5432 6380 8200 3307
    # 全部就绪后打印 READY，然后一直运行；Ctrl-C 结束

注意 Redis 是 **6380**、演示 MySQL 是 **3307**（compose 里避开了本机可能已有的
6379 / 3306）。隧道断掉时本地端口会变成"能连上但不通"，症状是登录超时而不是
连接被拒——先看这个进程还在不在，再看 ECS 上的容器。
"""

from __future__ import annotations

import contextlib
import os
import select
import socket
import socketserver
import sys
import threading

import paramiko

HOST = os.environ.get("ELT_ECS_HOST", "60.205.138.167")
USER = os.environ.get("ELT_ECS_USER", "root")
SSH_PORT = int(os.environ.get("ELT_ECS_PORT", "22"))
# 远端从 ECS 自己看出去的地址：容器把端口发布到宿主机上，所以 127.0.0.1 就够
REMOTE_HOST = "127.0.0.1"

_transport: paramiko.Transport | None = None
_lock = threading.Lock()


def _channel(port: int) -> paramiko.Channel:
    assert _transport is not None
    # Transport 的 open_channel 不是线程安全的，多客户端并发时要串行化
    with _lock:
        return _transport.open_channel(
            "direct-tcpip", (REMOTE_HOST, port), ("127.0.0.1", 0)
        )


class _Forward(socketserver.BaseRequestHandler):
    remote_port: int

    def handle(self) -> None:
        try:
            chan = _channel(self.remote_port)
        except Exception as exc:  # 隧道断了/端口没监听：让对方连接直接失败
            print(f"[tunnel] {self.remote_port} 打开通道失败: {type(exc).__name__}", file=sys.stderr)
            return
        try:
            with contextlib.suppress(Exception):  # 客户端断开是常态，不值得打日志
                while True:
                    readable, _, _ = select.select([self.request, chan], [], [])
                    if self.request in readable:
                        data = self.request.recv(16384)
                        if not data:
                            break
                        chan.sendall(data)
                    if chan in readable:
                        data = chan.recv(16384)
                        if not data:
                            break
                        self.request.sendall(data)
        finally:
            chan.close()


class _Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


def _serve(port: int) -> _Server:
    handler = type(f"Forward{port}", (_Forward,), {"remote_port": port})
    server = _Server(("127.0.0.1", port), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def main() -> int:
    ports = [int(p) for p in sys.argv[1:]]
    if not ports:
        sys.exit("用法: python tunnel.py <本地端口>...")

    password = os.environ.get("ELT_ECS_PASSWORD")
    if not password:
        sys.exit("缺少 ELT_ECS_PASSWORD 环境变量")

    global _transport
    sock = socket.create_connection((HOST, SSH_PORT), timeout=15)
    _transport = paramiko.Transport(sock)
    _transport.connect(username=USER, password=password)
    _transport.set_keepalive(30)  # 面试演示会挂很久，别被中间设备静默掐断

    for port in ports:
        try:
            _serve(port)
        except OSError as exc:
            print(f"[tunnel] 本地 {port} 监听失败: {exc}", file=sys.stderr)
            return 1

    print("READY " + " ".join(str(p) for p in ports), flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        _transport.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
