"""``Idempotency-Key`` 中间件。

对应 CLAUDE.md §10 的「未授权/重放成功率 = 0」：本文件证明同一个 key 第二次提交
**不会**再执行一次副作用（用「计数器只加了一次」来断言，而不是「没报错」）。
"""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

import api.idempotency as idem
from api.idempotency import IDEMPOTENCY_HEADER, IdempotencyMiddleware
from core.security import create_access_token
from models.support import IdempotencyKey


@pytest.fixture
def calls() -> list[str]:
    """副作用记录器：每次路由真的执行就追加一项。"""
    return []


@pytest.fixture
def app(calls: list[str]) -> FastAPI:
    """最小应用：一个会留下副作用的写接口 + 一个匿名写接口。"""
    application = FastAPI()
    application.add_middleware(IdempotencyMiddleware)

    @application.post("/things")
    async def create_thing() -> dict[str, object]:
        calls.append("executed")
        return {"code": "OK", "message": f"第 {len(calls)} 次", "data": {"n": len(calls)}, "request_id": "r"}

    @application.post("/boom")
    async def boom() -> dict[str, object]:
        calls.append("boom")
        from fastapi import HTTPException

        raise HTTPException(status_code=400, detail="bad")

    @application.get("/things")
    async def list_things() -> dict[str, object]:
        calls.append("read")
        return {"code": "OK", "message": "", "data": [], "request_id": "r"}

    return application


@pytest.fixture
def token() -> str:
    access, _ = create_access_token("1", tenant_id=1, project_ids=[])
    return access


@pytest.fixture
async def client(app: FastAPI, async_session_factory, monkeypatch) -> httpx.AsyncClient:
    """把中间件用的 session 工厂换成测试库，避免它去连真实 PG。"""
    monkeypatch.setattr(idem, "SessionFactory", async_session_factory)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_同一key重复提交只执行一次(client, token, calls) -> None:
    key = "11111111-1111-1111-1111-111111111111"
    headers = {**_auth(token), IDEMPOTENCY_HEADER: key}

    first = await client.post("/things", headers=headers)
    second = await client.post("/things", headers=headers)

    assert first.status_code == 200
    assert second.status_code == 200
    # 核心断言：副作用只发生了一次
    assert calls == ["executed"]
    # 重放返回与首次**完全相同**的响应体
    assert second.json() == first.json()


async def test_不同key各执行一次(client, token, calls) -> None:
    for key in ("key-a", "key-b"):
        r = await client.post("/things", headers={**_auth(token), IDEMPOTENCY_HEADER: key})
        assert r.status_code == 200
    assert calls == ["executed", "executed"]


async def test_无key时不启用幂等(client, token, calls) -> None:
    """不带 key 就是普通请求，重复提交会真的执行两次——这是调用方的选择。"""
    await client.post("/things", headers=_auth(token))
    await client.post("/things", headers=_auth(token))
    assert calls == ["executed", "executed"]


async def test_同key不同内容被拒(client, token, calls) -> None:
    """客户端复用 key 却改了 body：若放行，第二次的内容会被第一次的响应悄悄吞掉。"""
    headers = {**_auth(token), IDEMPOTENCY_HEADER: "same-key"}
    first = await client.post("/things", headers=headers, json={"a": 1})
    assert first.status_code == 200

    second = await client.post("/things", headers=headers, json={"a": 2})
    assert second.status_code == 409
    assert second.json()["code"] == "IDEMPOTENCY_CONFLICT"
    assert calls == ["executed"]  # 第二次没有执行


async def test_GET不记账(client, token, calls, async_session_factory) -> None:
    """读接口天然幂等，给它们记账纯属浪费一次写库。"""
    await client.get("/things", headers={**_auth(token), IDEMPOTENCY_HEADER: "get-key"})
    await client.get("/things", headers={**_auth(token), IDEMPOTENCY_HEADER: "get-key"})
    assert calls == ["read", "read"]

    async with async_session_factory() as s:
        assert (await s.execute(select(func.count()).select_from(IdempotencyKey))).scalar_one() == 0


async def test_失败响应不记账(client, token, calls) -> None:
    """4xx 不缓存，否则用户改好参数重试时会被上一轮的错误响应粘住。"""
    headers = {**_auth(token), IDEMPOTENCY_HEADER: "boom-key"}
    assert (await client.post("/boom", headers=headers)).status_code == 400
    assert (await client.post("/boom", headers=headers)).status_code == 400
    # 两次都真的执行了（没有被重放成第一次的 400）
    assert calls == ["boom", "boom"]


async def test_匿名写请求不做幂等(client, calls) -> None:
    """未认证请求交由路由层回 401，中间件不抢先报错、也不记账。"""
    await client.post("/things", headers={IDEMPOTENCY_HEADER: "anon"})
    await client.post("/things", headers={IDEMPOTENCY_HEADER: "anon"})
    assert calls == ["executed", "executed"]


async def test_进行中的请求被拒(client, token, calls, async_session_factory) -> None:
    """占位已存在但未完成 → 409 让客户端稍后重试，而不是替它猜一个结果。"""
    async with async_session_factory() as s:
        s.add(
            IdempotencyKey(
                tenant_id=1,
                scope="POST /things",
                key="inflight",
                request_fingerprint="whatever",
                response_snapshot={},
                completed=False,
                expires_at=__import__("datetime").datetime.now(__import__("datetime").UTC)
                + __import__("datetime").timedelta(hours=1),
            )
        )
        await s.commit()

    r = await client.post("/things", headers={**_auth(token), IDEMPOTENCY_HEADER: "inflight"})
    assert r.status_code == 409
    assert calls == []


async def test_体被正确回灌给路由(client, token, calls, async_session_factory) -> None:
    """中间件读过 body 之后必须把它灌回 receive，否则处理器拿到空 body。

    这里用「同 key 不同 body 被识别为不同内容」间接证明 body 真的被读到了：
    若 body 读不到，两次请求的指纹会相同，第二次就会被当成重放而放行。
    """
    headers = {**_auth(token), IDEMPOTENCY_HEADER: "body-key"}
    await client.post("/things", headers=headers, json={"payload": "aaa"})
    r = await client.post("/things", headers=headers, json={"payload": "bbb"})
    assert r.status_code == 409


async def test_会话与租户维度隔离(client, token, calls, async_session_factory) -> None:
    """scope 含方法与路径：同一个 key 用在两个接口上不该互相冲突。"""
    headers = {**_auth(token), IDEMPOTENCY_HEADER: "shared-key"}
    assert (await client.post("/things", headers=headers)).status_code == 200
    assert (await client.post("/boom", headers=headers)).status_code == 400
    assert calls == ["executed", "boom"]


async def test_唯一约束是唯一裁决者(async_session: AsyncSession) -> None:
    """并发下「先查后写」必然双写；原子性必须由 DB 唯一约束提供。"""
    from datetime import UTC, datetime, timedelta

    from sqlalchemy.exc import IntegrityError

    def _row() -> IdempotencyKey:
        return IdempotencyKey(
            tenant_id=1,
            scope="POST /things",
            key="dup",
            request_fingerprint="f",
            response_snapshot={},
            completed=False,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )

    async_session.add(_row())
    await async_session.commit()

    async_session.add(_row())
    with pytest.raises(IntegrityError):
        await async_session.commit()
