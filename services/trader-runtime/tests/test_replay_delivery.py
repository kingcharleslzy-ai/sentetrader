"""回放提醒投递的真实装配验收 (2026-10-03)：线上 4fb0fd3e 验收发现到价提醒触发了但 SSE 收不到
消息、收盘前提醒在 1 步/秒轮询下也不出现。这里不用测试替身模拟监控循环，而是走生产同一条装配
（POST /api/replays → create_replay_child → 子进程自己的 lifespan/监控循环），在真实 uvicorn
服务上用真实 SSE（GET /api/events?sessionId=）收事件，先复现再守住修好后的行为：

- 到价提醒：子进程的监控循环触发后，message.completed 与 feed.price-alert 必须真的到达
  这条 SSE 流（订单卡能到，提醒也要能到）。
- 收盘前提醒：回放时钟快速走过收盘窗口（10 步/秒）时，不做任何 HTTP 轮询也要提醒——判定
  跟着回放时钟的推进走，不靠界面读取顺风车。
- 到价提醒在 10 步/秒下不漏过只存在一个回放分钟的价格低点。

行情源是测试替身，但形状贴生产：回放导出（instrument/market/workbench 报价）不带交易时段，
父进程的实时目录（/v1/instruments）带——收盘时段从那里取。"""
from __future__ import annotations

import asyncio
import json
import socket
from typing import Any

import httpx
import uvicorn

from test_replay import IDENTIFIER, SYMBOL, HistoricalSource
from trader_runtime.main import create_app

HEADERS = {"Origin": "http://127.0.0.1:5173", "Authorization": "Bearer test-replay-token"}

# 沪金的时段：日盘三段 + 夜盘 21:00–次日 02:30（收盘窗口 02:25–02:30）。
AU_SCHEDULE = {"day": [["09:00:00", "10:15:00"], ["10:30:00", "11:30:00"], ["13:30:00", "15:00:00"]],
               "night": [["21:00:00", "02:30:00"]]}


class DeliverySource(HistoricalSource):
    """生产形状的回放源：回放数据不带时段、带一个已有持仓；父进程的实时目录带时段。
    `script` 给每次 advance 后的分钟价（缺省保持当前价）——用它构造只存在一个回放分钟
    的价格低点。"""

    def __init__(self, *, clock: str = "2026-09-03T13:00:00+00:00",
                 positions: list[dict[str, Any]] | None = None,
                 script: list[float] | None = None):
        super().__init__()
        self.data["clock"] = clock
        self.data["market"]["quoteDatetime"] = clock
        self.data["positions"] = positions or []
        self.advances = 0
        self.script = script or []

    def _request(self, method, path, payload=None, **kwargs):
        if path == "/v1/instruments":
            return {"schemaVersion": 1, "capturedAt": "2026-10-03T01:00:00+00:00", "dataStatus": "normal",
                    "items": [{"symbol": SYMBOL, "exchangeId": "SHFE", "instrumentId": "au2612",
                               "instrumentName": "沪金2612", "priceTick": .02, "volumeMultiple": 1000,
                               "tradingSchedule": AU_SCHEDULE}]}
        result = super()._request(method, path, payload, **kwargs)
        if path.endswith("/advance"):
            self.advances += 1
            if self.advances <= len(self.script):
                price = self.script[self.advances - 1]
                result["market"]["price"] = price
                result["market"]["lastPrice"] = price
        return result


class Server:
    """A real uvicorn socket for the assembled app (lifespan off — the test owns it)."""

    def __init__(self, app):
        self.app = app
        self._server: uvicorn.Server | None = None
        self._task: asyncio.Task | None = None
        self._sock: socket.socket | None = None

    async def __aenter__(self) -> str:
        self._sock = socket.socket()
        self._sock.bind(("127.0.0.1", 0)); self._sock.listen(); self._sock.setblocking(False)
        self._server = uvicorn.Server(uvicorn.Config(self.app, lifespan="off", log_level="critical",
                                                     timeout_graceful_shutdown=2))
        self._task = asyncio.create_task(self._server.serve(sockets=[self._sock]))
        for _ in range(300):
            if self._server.started:
                return f"http://127.0.0.1:{self._sock.getsockname()[1]}"
            await asyncio.sleep(.01)
        raise RuntimeError("uvicorn_did_not_start")

    async def __aexit__(self, *exc) -> None:
        assert self._server is not None and self._task is not None
        self._server.should_exit = True
        await asyncio.wait_for(self._task, 4)
        assert self._sock is not None
        self._sock.close()


class SseReader:
    """一条真实的事件流：流式 GET /api/events，收到的每帧记成 (事件名, 负载)。"""

    def __init__(self, client: httpx.AsyncClient, url: str, params: dict[str, str]):
        self._client, self._url, self._params = client, url, params
        self.events: list[tuple[str, Any]] = []
        self.opened = asyncio.Event()
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        try:
            async with self._client.stream("GET", self._url, params=self._params) as response:
                assert response.status_code == 200
                self.opened.set()
                async for line in response.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    try:
                        event = json.loads(line[len("data: "):])
                    except ValueError:
                        continue
                    name = str(event.get("name") or "")
                    if name and name != "heartbeat":
                        self.events.append((name, event.get("payload")))
        except httpx.HTTPError:
            pass  # the test closes the stream at teardown

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    def texts(self) -> list[str]:
        return [str(payload.get("text") or "") for _, payload in self.events]

    def has(self, name: str) -> bool:
        return any(item == name for item, _ in self.events)


async def wait_for(predicate, timeout: float = 20.0) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.05)
    return False


async def build(client: httpx.AsyncClient, source: DeliverySource) -> tuple[str, SseReader]:
    """真实装配一次回放：创建 → 子进程会话 → 真实 SSE 读取器（等它真连上）。"""
    created = await client.post("/api/replays", json={"symbol": SYMBOL, "tradingDay": "2026-09-04",
                                                      "initialBalance": 1_000_000})
    assert created.status_code == 201, created.text
    prefix = created.json()["runtimePath"]
    session = (await client.post(prefix + "/api/sessions",
                                 json={"mode": "local", "agentId": "native"})).json()
    reader = SseReader(client, prefix + "/api/events", {"sessionId": session["sessionId"]})
    reader.start()
    await asyncio.wait_for(reader.opened.wait(), 5)
    assert await wait_for(lambda: reader.has("runtime.status")), "the stream never delivered its status"
    return prefix, reader


async def test_price_alert_delivery_reaches_the_real_sse_stream(tmp_path) -> None:
    source = DeliverySource()
    app = create_app(tmp_path / "alert-delivery.sqlite3", runtime_state_root=tmp_path,
                     data_mode="tqkq-sidecar", data_provider=source, agent_mode="fake",
                     local_token="test-replay-token")
    async with app.router.lifespan_context(app):
        async with Server(app) as base:
            async with httpx.AsyncClient(base_url=base, headers=HEADERS, trust_env=False, timeout=10) as client:
                prefix, reader = await build(client, source)
                try:
                    made = await client.post(prefix + "/api/market/alerts",
                                             json={"symbol": SYMBOL, "price": 824.0, "direction": "above"})
                    assert made.status_code == 201, made.text
                    # 子进程自己的循环读价：暂停态 0.75 秒刷一次快照、监控 1 秒一轮，全走真实路径。
                    source.data["market"]["price"] = 826.0
                    source.data["market"]["lastPrice"] = 826.0
                    arrived = await wait_for(lambda: reader.has("feed.price-alert"))
                    assert arrived, f"feed.price-alert never reached the stream: {reader.events!r}"
                    assert any("到价提醒" in text for text in reader.texts()), reader.texts()
                    listed = (await client.get(prefix + "/api/market/alerts")).json()
                    assert not listed["active"] and listed["triggered"][0]["status"] == "triggered"
                finally:
                    await reader.stop()
                    await client.post(f"/api/replays/{IDENTIFIER}/control", json={"action": "close"})


async def test_close_reminder_fires_on_the_replay_clock_without_any_polling(tmp_path) -> None:
    # 02:21（沪金夜盘 02:30 收盘）：5 个回放分钟后进入收盘前 5 分钟窗口；10 步/秒下窗口
    # 只有 0.5 秒真实时间——不做任何持仓/账户轮询，判定必须跟着回放时钟的推进走。
    source = DeliverySource(clock="2026-09-03T18:21:00+00:00",
                            positions=[{"symbol": SYMBOL, "direction": "long", "volume": 1,
                                        "unrealizedPnl": 260.0}])
    app = create_app(tmp_path / "close-delivery.sqlite3", runtime_state_root=tmp_path,
                     data_mode="tqkq-sidecar", data_provider=source, agent_mode="fake",
                     local_token="test-replay-token")
    async with app.router.lifespan_context(app):
        async with Server(app) as base:
            async with httpx.AsyncClient(base_url=base, headers=HEADERS, trust_env=False, timeout=10) as client:
                # 收盘提醒开关在主进程的偏好里（回放子进程读的正是这份）。
                prefs = await client.put("/api/preferences", json={"notify": {"marketCloseReminder": True}})
                assert prefs.status_code == 200, prefs.text
                prefix, reader = await build(client, source)
                try:
                    assert (await client.post(f"/api/replays/{IDENTIFIER}/control",
                                              json={"action": "speed", "speed": 10})).status_code == 200
                    assert (await client.post(f"/api/replays/{IDENTIFIER}/control",
                                              json={"action": "play"})).status_code == 200
                    arrived = await wait_for(lambda: any("收盘提醒" in text for text in reader.texts()))
                    assert arrived, f"close reminder never reached the stream: {reader.texts()!r}"
                    # 消息走 outbox→事件表、feed.reminder 是其后发布的瞬时事件——两条通道各自
                    # 必达，但不承诺同帧（_deliver 先 flush 再 publish，中间是可被并行负载拉开
                    # 的调度间隙）。这里按事件等它到达，不在看到消息的同一刻断言它已在。
                    assert await wait_for(lambda: reader.has("feed.reminder")), \
                        f"feed.reminder never reached the stream: {reader.events!r}"
                    assert "沪金2612 多 1 手" in "".join(reader.texts())
                finally:
                    await client.post(f"/api/replays/{IDENTIFIER}/control", json={"action": "pause"})
                    await reader.stop()
                    await client.post(f"/api/replays/{IDENTIFIER}/control", json={"action": "close"})


async def test_fast_play_does_not_skip_a_one_minute_price_dip(tmp_path) -> None:
    # 提醒跌破 820；10 步/秒播放里第 3 个回放分钟的价格恰好是 818，此后回到 823.5——
    # 这个低点只存在 0.1 秒真实时间，墙上的秒级监控可能整段跳过，判定必须跟着推进走。
    source = DeliverySource(script=[823.5, 823.5, 818.0, 823.5, 823.5, 823.5, 823.5, 823.5])
    app = create_app(tmp_path / "dip-delivery.sqlite3", runtime_state_root=tmp_path,
                     data_mode="tqkq-sidecar", data_provider=source, agent_mode="fake",
                     local_token="test-replay-token")
    async with app.router.lifespan_context(app):
        async with Server(app) as base:
            async with httpx.AsyncClient(base_url=base, headers=HEADERS, trust_env=False, timeout=10) as client:
                prefix, reader = await build(client, source)
                try:
                    made = await client.post(prefix + "/api/market/alerts",
                                             json={"symbol": SYMBOL, "price": 820.0, "direction": "below"})
                    assert made.status_code == 201, made.text
                    assert (await client.post(f"/api/replays/{IDENTIFIER}/control",
                                              json={"action": "speed", "speed": 10})).status_code == 200
                    assert (await client.post(f"/api/replays/{IDENTIFIER}/control",
                                              json={"action": "play"})).status_code == 200
                    arrived = await wait_for(lambda: any("到价提醒" in text for text in reader.texts()))
                    assert arrived, f"the one-minute dip was skipped: {reader.texts()!r}"
                    listed = (await client.get(prefix + "/api/market/alerts")).json()
                    assert listed["triggered"][0]["status"] == "triggered"
                    assert float(listed["triggered"][0]["triggerPrice"]) == 818.0
                finally:
                    await client.post(f"/api/replays/{IDENTIFIER}/control", json={"action": "pause"})
                    await reader.stop()
                    await client.post(f"/api/replays/{IDENTIFIER}/control", json={"action": "close"})
