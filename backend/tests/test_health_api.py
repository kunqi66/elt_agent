"""健康探针的 ASGI 冒烟测试。

用 httpx 的 ASGITransport **在进程内**打真实请求，不起 uvicorn、不连数据库。
之所以不靠检查 ``app.routes``：新版 FastAPI 用 ``_IncludedRouter`` 惰性包裹子路由，
按 ``route.path`` 遍历会看不到它们——只有真发一次请求才能确认路由确实可达。
"""

from __future__ import annotations

import httpx
import pytest

from main import create_app


@pytest.fixture
def client() -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=create_app())
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")


async def test_healthz_不需要任何依赖即返回200(client: httpx.AsyncClient) -> None:
    """存活探针不能依赖 PG/Redis——依赖挂了不该导致容器被反复重启。"""
    async with client as c:
        resp = await c.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["app"]


async def test_请求ID被透传并在响应头回写(client: httpx.AsyncClient) -> None:
    async with client as c:
        resp = await c.get("/healthz", headers={"X-Request-ID": "req_abcd1234"})
    assert resp.headers["X-Request-ID"] == "req_abcd1234"


async def test_请求ID缺省时自动生成(client: httpx.AsyncClient) -> None:
    async with client as c:
        resp = await c.get("/healthz")
    rid = resp.headers.get("X-Request-ID", "")
    assert rid.startswith("req_")
    assert len(rid) > len("req_")


async def test_readyz_结论与依赖检查一致(client: httpx.AsyncClient) -> None:
    """本机无 PG/Redis，因此这里断言「状态码与 checks 自洽」而非固定值——
    这样同一条用例在有依赖的 ECS 上也成立，不会变成一个只会本地绿的假测试。"""
    async with client as c:
        resp = await c.get("/readyz")

    body = resp.json()
    checks = body["checks"]
    assert set(checks) == {"postgres", "redis"}
    healthy = all(v == "ok" for v in checks.values())
    assert resp.status_code == (200 if healthy else 503)
    assert body["status"] == ("ok" if healthy else "degraded")
