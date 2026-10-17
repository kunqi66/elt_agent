"""``SeatunnelClient.metrics()`` 的解析用例。

这个函数的错误形状很特别：**它不抛异常、也不报错，只是把某个计数器读成 0**。
而 0 在 be-16 的判定里是**真读数**（「数据面报了 0 行」），于是

- 读行数读成 0 → 看着像作业空跑；
- 读字节数读成 0 → 放大比（写字节 / 读字节）算不出来 → 那一维恒判 ``unknown``，
  也就是**一条永远不会开火的阈值**。

第二种情况在本次实现 be-16 时真的存在过：DTO 里根本没有读字节字段，
放大比维度从落地那天起就没可能触发，而「笛卡尔积 / join 写错」这类逻辑错误
只有它拦得住。所以这里用 ``httpx.MockTransport`` 把几种真实的响应形状喂进去，
把「哪个键进来、哪个字段出去」钉住。

（``metrics()`` 此前**一条用例都没有**——HTTP 客户端要造响应才测得了，
而它是唯一一处「键名写错就静默变成 0」的地方。）
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from clients.seatunnel.client import SeatunnelClient

#: 假地址：单测不许连真东西，用保留域让「不小心真发请求」直接失败。
_BASE_URL = "http://seatunnel.invalid:8080"


def _client_returning(payload: Any, status_code: int = 200) -> SeatunnelClient:
    """造一个把 ``/metrics`` 答成给定内容的客户端。

    用 ``httpx.MockTransport``（httpx 自带，不引新依赖）替换传输层，
    而不是替换 ``metrics()`` 本身——被守的正是这个方法里的键名映射。
    """

    def handler(request: httpx.Request) -> httpx.Response:
        body = payload if isinstance(payload, str) else json.dumps(payload)
        return httpx.Response(status_code, text=body)

    client = SeatunnelClient(_BASE_URL)

    def _fake() -> httpx.AsyncClient:
        # base_url 必须跟着给：真客户端的 ``_client()`` 是带 base_url 建的，
        # 少了它请求里只剩相对路径，httpx 的 cookie 处理会先炸在 URL 解析上
        # （报 ``unknown url type``，看起来像客户端的 bug，其实是夹具没仿全）。
        return httpx.AsyncClient(base_url=_BASE_URL, transport=httpx.MockTransport(handler))

    client._client = _fake  # type: ignore[method-assign]
    return client


async def test_读字节数进DTO而不是被丢掉() -> None:
    """``readBytes`` 必须落到 ``read_bytes``：放大比维度唯一的来源。"""
    client = _client_returning(
        {"readRows": 1000, "writtenRows": 1000, "writtenBytes": 5_000_000, "readBytes": 2_000_000}
    )
    metrics = await client.metrics("job-1")

    assert metrics is not None
    assert metrics.read_rows == 1000
    assert metrics.written_bytes == 5_000_000
    assert metrics.read_bytes == 2_000_000


@pytest.mark.parametrize("key", ["readBytes", "readBytesTotal", "sourceReceivedBytes"])
async def test_读字节数的几个候选键名都认(key: str) -> None:
    """Zeta 各版本给的键名不一致，所以接受候选项——与其余计数器同一套容忍度。"""
    client = _client_returning({"writtenBytes": 10, key: 4})
    metrics = await client.metrics("job-1")

    assert metrics is not None
    assert metrics.read_bytes == 4


async def test_带表名前缀的计数器也能认出来() -> None:
    """有的版本按表摊平命名（``TableSourceReceivedBytes``）。

    这里认不出来也不会错——它只是回 0、放大比判 ``unknown``；
    但能认出来就别浪费，因为这一维正是为了抓「写出去的比读进来的多一个数量级」。
    """
    client = _client_returning(
        {"writtenBytes": 900, "TableSourceReceivedBytes": 120, "TableSinkWrittenBytes": 900}
    )
    metrics = await client.metrics("job-1")

    assert metrics is not None
    assert metrics.read_bytes == 120
    # 汇总键与摊平键同时存在时，具名汇总键优先（``TableSinkWrittenBytes`` 不参与
    # ``written_bytes``，它走的是 ``writtenBytes``/``writeBytes``）
    assert metrics.written_bytes == 900


async def test_未识别的计数器只留数字_配置片段与错误原文不许进extra() -> None:
    """``extra`` 是「下次对着真 Zeta 校准映射」的线索，不是原样回显。

    Zeta 的响应里可能带上配置片段或错误原文（``seatunnel.stop`` 那边有同样的
    教训：它的错误里常带整段配置，所以只记 ``failure_kind``）。把字符串收进
    ``extra``，就等于给这些内容开了一条进日志 / 进库的路。
    """
    client = _client_returning(
        {
            "readRows": 1,
            "unknownCounter": 42,
            "anotherCounter": 3.5,
            "configFragment": "jdbc:mysql://root:pw@host/db",
            "errorSummary": "Table 'x' doesn't exist",
            "nested": {"a": 1},
            "flag": True,
        }
    )
    metrics = await client.metrics("job-1")

    assert metrics is not None
    assert metrics.extra == {"unknownCounter": 42, "anotherCounter": 3.5}
    # 字符串、嵌套结构、布尔都不收——布尔排除是因为 ``isinstance(True, int)`` 为真，
    # 不显式排除它就会以 ``1`` 的形状混进来，看着像个计数器
    assert "configFragment" not in metrics.extra
    assert "errorSummary" not in metrics.extra
    assert "nested" not in metrics.extra
    assert "flag" not in metrics.extra
    assert "pw@host" not in json.dumps(metrics.extra, ensure_ascii=False)


async def test_指标接口不可用回None而不是全0的DTO() -> None:
    """``None`` 与「全 0」必须分得清。

    两者对 be-16 的含义完全不同：``None`` → 走 §6.3 的退化路径（这些维度
    不参与判定）；全 0 的 DTO → 「读到 0 行、写 0 字节」，看着像作业空跑，
    还可能让放大比算成「读为零」。所以非 200、空体、非 JSON 一律回 ``None``。
    """
    assert await _client_returning({"readRows": 5}, status_code=500).metrics("job-1") is None
    assert await _client_returning("").metrics("job-1") is None
    assert await _client_returning("not json at all").metrics("job-1") is None
    assert await _client_returning([1, 2, 3]).metrics("job-1") is None


async def test_认不出读字节时日志里留键名不留值(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """映射没认出来时，日志是**校准线索**，不是把响应原文倒出来。

    「放大比恒 unknown」这条症状不会指向 ``client.metrics``——所以认不出时必须留一句
    「引擎报了哪些计数器」。但留的只能是**键名**：值里可能有配置片段或错误原文
    （``extra`` 已按数字过滤，这里是第二道，防止以后有人放宽那个过滤）。

    用 ``capsys`` 而不是 ``caplog``：``core.logging`` 给 structlog 配的是
    自己的 handler 且不向 root 传播，日志落在 stdout 上——``caplog`` 会**什么都收不到**，
    于是断言退化成永远通过（本用例第一版就是这样，靠 stdout 里的实文才发现）。
    """
    client = _client_returning({"writtenBytes": 900, "StreamReadBytez": 12345})
    metrics = await client.metrics("job-1")

    assert metrics is not None and metrics.read_bytes == 0
    blob = capsys.readouterr().out
    assert "StreamReadBytez" in blob
    assert "12345" not in blob


async def test_没有读字节时是0_由调用方翻成未知而不是无穷大() -> None:
    """客户端如实回 0，翻成「未知」是调用方（``services/supervision``）的责任。

    这条用例钉住**边界属于谁**：把 0 当成真字节数送进放大比，
    会得到「写了很多、读进来 0」的无穷大——一次凭空的硬中断。
    """
    client = _client_returning({"writtenBytes": 5_000_000})
    metrics = await client.metrics("job-1")

    assert metrics is not None
    assert metrics.read_bytes == 0

    from services.supervision import _sample

    sample = _sample(metrics, elapsed_sec=10.0)
    assert sample.read_bytes is None
    assert sample.written_bytes == 5_000_000
