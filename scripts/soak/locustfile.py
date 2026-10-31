"""24 小时浸泡测试的模拟用户（Locust 2.46.7，不进 CI）。

被测：docker-compose.cloud.yml 的 fake 栈（cloud-jwt + development-hs256，假数据/假 agent），经
client 容器 nginx 的 /api 反代访问 Runtime。只用模拟盘接口，绝不碰实盘；healthz 的
liveTrading 必须是 disabled，否则开跑即退出。

通常由 `scripts/soak/soak.sh run` 启动；手动跑：

    AI_TRADER_CLOUD_JWT_SECRET=... locust -f scripts/soak/locustfile.py --headless \
        --host http://127.0.0.1:18180 --users 25 --spawn-rate 1 --run-time 5m \
        --csv out/locust --csv-full-history --only-summary --soak-out out [--no-rum]

自定义参数（也可用环境变量）：
    --rum / --no-rum        每用户每 60 秒 POST /api/telemetry/rum（默认开；主分支没有该接口时关掉）
    --soak-out DIR          逐分钟聚合 minutes.jsonl 与 sse.json 的输出目录（默认当前目录）
    --jwt-ttl SEC           测试 JWT 的 exp（默认 3600 = Runtime 设置的 cookie max_age）。
                            Runtime 的 cookie 就是 JWT 本身，每个请求与 SSE 流的每一帧都会重新校验 exp，
                            所以在 80% 寿命处重新 POST /api/auth/cloud 并主动重开 SSE（记为 rotate，不算断线）。
    --order-every MIN-MAX   每用户模拟下单周期（分钟，默认 10-20）
    --backtest-every MIN-MAX 每用户回测周期（分钟，默认 20-40，平均 30）

行为与频率依据（apps/client/src 的实际调用）：
    * 进页：POST /api/auth/cloud → GET /api/workspace → POST /api/sessions/restore（useRuntimeSession.ts，
      客户端用 restore 而不是 POST /api/sessions）→ 打开 GET /api/events?sessionId=（sse.ts 的 fetch 流，
      断开 1 秒后带 Last-Event-ID 重连；20 秒无任何帧视为死流）→ 流打开后 PUT /feed 与 /feed/view
      （feedView.ts：每次流重开都重发 view）。
    * 推送是主路径，轮询只是兜底：所以浏览动作的思考时间取 6–20 秒，大部分时间用户只是挂着 SSE 看盘。
    * 自选报价 GET /api/market/quotes 最常见；看详情 GET /api/market/{symbol}；K 线刷新带 since
      （marketCache.ts 持有已收盘 K 线）；切合约/周期先整取 workbench 再 PUT /feed；报价总表
      GET /api/market/board 并改 /feed/view 的 board；账户页 summary/positions/orders；
      偶尔切到后台标签页（/feed/view visible=false 30–180 秒）。
    * 低频：回测每用户约 30 分钟一次（POST /api/backtests/reproducible 再 GET 结果）；下单每用户 10–20 分钟
      一次：healthz.simulationTrading 为真则建卡→确认→限价挂单→撤单，否则（fake 栈默认）建卡→拒绝。
"""
from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import itertools
import json
import math
import os
import random
import secrets
import threading
import time
import uuid
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import gevent
import locust.stats
import requests
from locust import HttpUser, between, events, task
from locust.exception import StopUser

# 历史 CSV 每 30 秒一行（默认 1 秒，24 小时会写出上百万行）。
locust.stats.CSV_STATS_INTERVAL_SEC = 30

# 与 Runtime rum.py 的 BUCKET_BOUNDS（v1）一致：桶 i 收 (bound[i-1], bound[i]]，最后一桶收其余。
BUCKET_BOUNDS = (10, 25, 50, 75, 100, 150, 200, 300, 500, 750, 1000, 1500, 2000, 3000, 5000, 8000,
                 13000, 20000, 30000, 60000)
DURATIONS = (60, 300, 900, 3600, 86400)
SSE_IDLE_SECONDS = 20  # api.ts openEventStream idleMs
SSE_RETRY_SECONDS = 1  # sse.ts retryMs
SSE_TYPE = "SSE"
USER_SEQ = itertools.count(1)


def bucket_index(value: float) -> int:
    for index, bound in enumerate(BUCKET_BOUNDS):
        if value <= bound:
            return index
    return len(BUCKET_BOUNDS)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def issue_token(secret: str, *, subject: str, tenant: str, ttl: int) -> str:
    now = int(time.time())
    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    payload = _b64(json.dumps({
        "iss": os.getenv("AI_TRADER_CLOUD_JWT_ISSUER", "ai-trader-cloud"),
        "aud": os.getenv("AI_TRADER_CLOUD_JWT_AUDIENCE", "ai-trader"),
        "tid": tenant, "sub": subject, "iat": now, "exp": now + ttl,
    }, separators=(",", ":")).encode())
    signature = _b64(hmac.new(secret.encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest())
    return f"{header}.{payload}.{signature}"


def _range(text: str) -> tuple[float, float]:
    low, _, high = text.partition("-")
    return float(low), float(high or low)


@events.init_command_line_parser.add_listener
def _arguments(parser) -> None:
    parser.add_argument("--rum", dest="rum", action="store_true", default=os.getenv("SOAK_RUM", "on") != "off",
                        help="report RUM batches every 60 s (default on)")
    parser.add_argument("--no-rum", dest="rum", action="store_false", help="do not report RUM")
    parser.add_argument("--soak-out", default=os.getenv("SOAK_OUT", "."), help="directory for minutes.jsonl / sse.json")
    parser.add_argument("--jwt-ttl", type=int, default=int(os.getenv("SOAK_JWT_TTL", "3600")))
    parser.add_argument("--soak-tenant", default=os.getenv("AI_TRADER_CLOUD_TENANT_ID", "soak-tenant"))
    parser.add_argument("--order-every", default=os.getenv("SOAK_ORDER_EVERY", "10-20"), help="minutes, MIN-MAX")
    parser.add_argument("--backtest-every", default=os.getenv("SOAK_BACKTEST_EVERY", "20-40"), help="minutes, MIN-MAX")


# ---------------------------------------------------------------------------------------------
# 逐分钟聚合：每个 (分钟, 类型, 名称) 一行，带与 RUM 同一套桶的直方图，报告据此按时段算分位数。

class MinuteLog:
    def __init__(self) -> None:
        self.rows: dict[tuple[int, str, str], dict[str, Any]] = {}
        self.counters: dict[tuple[int, str], int] = defaultdict(int)
        self.lock = threading.Lock()
        self.path: Path | None = None
        self.sse_path: Path | None = None
        self.totals: dict[str, int] = defaultdict(int)

    def add(self, request_type: str, name: str, response_time: float, failed: bool) -> None:
        minute = int(time.time()) // 60 * 60
        row = self.rows.get((minute, request_type, name))
        if row is None:
            row = self.rows[(minute, request_type, name)] = {"n": 0, "fail": 0, "sum": 0.0, "buckets": [0] * (len(BUCKET_BOUNDS) + 1)}
        row["n"] += 1
        row["fail"] += int(failed)
        row["sum"] += float(response_time or 0)
        row["buckets"][bucket_index(float(response_time or 0))] += 1

    def count(self, name: str, amount: int = 1) -> None:
        self.counters[(int(time.time()) // 60 * 60, name)] += amount
        self.totals[name] += amount

    def flush(self, *, final: bool = False) -> None:
        if self.path is None:
            return
        current = int(time.time()) // 60 * 60
        done = [key for key in self.rows if final or key[0] < current]
        done_counters = [key for key in self.counters if final or key[0] < current]
        with self.path.open("a") as handle:
            for key in sorted(done):
                row = self.rows.pop(key)
                handle.write(json.dumps({"t": key[0], "type": key[1], "name": key[2], **row}, separators=(",", ":")) + "\n")
            by_minute: dict[int, dict[str, int]] = defaultdict(dict)
            for key in done_counters:
                by_minute[key[0]][key[1]] = self.counters.pop(key)
            for minute in sorted(by_minute):
                handle.write(json.dumps({"t": minute, "type": "COUNTER", "counters": by_minute[minute]}, separators=(",", ":")) + "\n")
        if self.sse_path is not None:
            self.sse_path.write_text(json.dumps({"totals": dict(self.totals), "openStreams": SSE_OPEN[0],
                                                 "writtenAt": time.time()}, indent=1))


MINUTES = MinuteLog()
SSE_OPEN = [0]
SIMULATION = {"trading": False}


@events.request.add_listener
def _record(request_type, name, response_time, response_length, exception=None, **_kwargs) -> None:
    MINUTES.add(request_type, name, response_time, exception is not None)


def _flusher() -> None:
    while True:
        gevent.sleep(60 - time.time() % 60 + 1)
        MINUTES.flush()


@events.init.add_listener
def _init(environment, **_kwargs) -> None:
    """开跑前核对被测栈：必须是 fake + cloud-jwt + liveTrading disabled，否则直接退出。"""
    options = environment.parsed_options
    if options is None or not environment.host:
        return
    out = Path(options.soak_out)
    out.mkdir(parents=True, exist_ok=True)
    MINUTES.path = out / "minutes.jsonl"
    MINUTES.sse_path = out / "sse.json"
    if not os.getenv("AI_TRADER_CLOUD_JWT_SECRET"):
        raise SystemExit("AI_TRADER_CLOUD_JWT_SECRET is required")
    health = requests.get(f"{environment.host.rstrip('/')}/healthz", timeout=20).json()
    if health.get("liveTrading") != "disabled" or health.get("mode") != "fake" or health.get("authMode") != "cloud-jwt":
        raise SystemExit(f"refusing to soak a non-fake or live-capable stack: {health}")
    SIMULATION["trading"] = bool(health.get("simulationTrading"))
    (out / "healthz-start.json").write_text(json.dumps(health, ensure_ascii=False, indent=1))


@events.test_start.add_listener
def _start(environment, **_kwargs) -> None:
    gevent.spawn(_flusher)


@events.test_stop.add_listener
def _stop(environment, **_kwargs) -> None:
    MINUTES.flush(final=True)


# ---------------------------------------------------------------------------------------------

class WebTrader(HttpUser):
    """一个开着网页客户端看盘的用户：一条 SSE 长连接 + 低频的读取与偶发的回测/模拟下单。"""

    wait_time = between(6, 20)

    def on_start(self) -> None:
        options = self.environment.parsed_options
        self.index = next(USER_SEQ)
        self.subject = f"soak-user-{self.index:02d}"
        self.secret = os.environ["AI_TRADER_CLOUD_JWT_SECRET"]
        self.tenant = options.soak_tenant
        self.ttl = max(120, int(options.jwt_ttl))
        self.origin = self.host.rstrip("/")
        self.client.headers.update({"Origin": self.origin})
        self.rum_on = bool(options.rum)
        self.rum_session = secrets.token_urlsafe(24)[:32]
        self.order_every = _range(options.order_every)
        self.backtest_every = _range(options.backtest_every)
        self.greenlets: list[gevent.Greenlet] = []
        self.sse_response: requests.Response | None = None
        self.sse_rotate = False
        self.sse_down_since: float | None = None
        self.last_event_id = ""
        self.visible = True
        self.minute = self._new_minute()
        self.bars: dict[tuple[str, int], int] = {}
        self.age_floor: list[tuple[int, float]] = []
        self.lcp_sent = False
        self.login()
        workspace = self.client.get("/api/workspace", name="/api/workspace")
        if workspace.status_code != 200:
            raise StopUser()
        restored = self.client.post("/api/sessions/restore", json={"mode": "cloud", "agentId": "native"},
                                    name="/api/sessions/restore")
        if restored.status_code != 200:
            raise StopUser()
        self.session_id = restored.json()["sessionId"]
        catalog = self.client.get("/api/instruments", name="/api/instruments").json().get("items", [])
        mains = [item["symbol"] for item in catalog if item.get("isMain")] or [item["symbol"] for item in catalog]
        self.symbols = mains or ["SHFE.au2612"]
        self.watchlist = random.sample(self.symbols, k=min(len(self.symbols), random.randint(3, 5)))
        self.symbol = random.choice(self.symbols)
        self.duration = random.choice(DURATIONS[:3])
        self.board_want = self._random_board()
        self.client.get("/api/runtime/status", params={"mode": "cloud"}, name="/api/runtime/status")
        self._load_chart(full=True)
        self.client.get("/api/account/summary", name="/api/account/summary")
        self.client.get("/api/positions", name="/api/positions")
        now = time.time()
        # 首次回测/下单错开到各自周期内的随机时刻，避免 25 个用户同一分钟扎堆。
        self.next_backtest = now + random.uniform(60, self.backtest_every[1] * 60)
        self.next_order = now + random.uniform(60, self.order_every[1] * 60)
        self.greenlets.append(gevent.spawn(self._sse_loop))
        self.greenlets.append(gevent.spawn(self._relogin_loop))
        if self.rum_on:
            self.greenlets.append(gevent.spawn(self._rum_loop))

    def on_stop(self) -> None:
        for greenlet in self.greenlets:
            greenlet.kill(block=False)
        self._close_sse()

    # -- auth ---------------------------------------------------------------------------------

    def login(self) -> None:
        token = issue_token(self.secret, subject=self.subject, tenant=self.tenant, ttl=self.ttl)
        with self.client.post("/api/auth/cloud", headers={"Authorization": f"Bearer {token}"},
                              name="/api/auth/cloud", catch_response=True) as response:
            if response.status_code != 200:
                response.failure(f"login {response.status_code}")
                raise StopUser()
        self.login_at = time.time()

    def _relogin_loop(self) -> None:
        while True:
            gevent.sleep(max(30.0, self.ttl * 0.8 - (time.time() - self.login_at)) + random.uniform(0, 20))
            try:
                self.login()
            except StopUser:
                continue
            # 旧流带着旧 cookie，到 exp 会被 Runtime 剪断：换新 cookie 后主动重开（rotate，不算断线）。
            self.sse_rotate = True
            self._close_sse()
            MINUTES.count("sse_rotate")

    # -- SSE ----------------------------------------------------------------------------------

    def _close_sse(self) -> None:
        response, self.sse_response = self.sse_response, None
        if response is not None:
            with contextlib.suppress(Exception):  # closing a socket that already died
                response.close()

    def _fire(self, name: str, ms: float, exception: Exception | None = None, length: int = 0) -> None:
        self.environment.events.request.fire(request_type=SSE_TYPE, name=name, response_time=ms,
                                             response_length=length, exception=exception, context={})

    def _sse_loop(self) -> None:
        stream = requests.Session()
        url = f"{self.host.rstrip('/')}/api/events"
        while True:
            headers = {"Accept": "text/event-stream", "Origin": self.origin}
            if self.last_event_id:
                headers["Last-Event-ID"] = self.last_event_id
            started = time.time()
            reason = "closed"
            try:
                response = stream.get(url, params={"sessionId": self.session_id}, headers=headers,
                                      cookies=self.client.cookies, stream=True,
                                      timeout=(10, SSE_IDLE_SECONDS))
                if response.status_code != 200:
                    response.close()
                    failure = RuntimeError(f"sse_status_{response.status_code}")
                    self._fire("connect", (time.time() - started) * 1000, exception=failure)
                    raise failure
                self.sse_response = response
                self._fire("connect", (time.time() - started) * 1000)
                SSE_OPEN[0] += 1
                if self.sse_down_since is not None:
                    self.minute["sse_down"].append((time.time() - self.sse_down_since) * 1000)
                    self.sse_down_since = None
                # 每次流（重）开都重发 focus 与 view（useRuntimeSession → resendFeedView）。
                gevent.spawn(self._send_feed)
                try:
                    self._read_stream(response, started)
                finally:
                    SSE_OPEN[0] -= 1
            except requests.exceptions.ReadTimeout:
                reason = "idle_timeout"
            except (requests.exceptions.ConnectionError, requests.exceptions.ChunkedEncodingError) as error:
                reason = "rotate" if self.sse_rotate else f"connection:{type(error).__name__}"
            except Exception as error:  # noqa: BLE001 - every failure reconnects like the client
                reason = "rotate" if self.sse_rotate else str(error)[:80]
            if self.sse_rotate:
                self.sse_rotate = False
                continue
            self._fire("disconnect", (time.time() - started) * 1000, exception=RuntimeError(reason))
            MINUTES.count("sse_disconnect")
            self.minute["sse_reconnect"] += 1
            if self.sse_down_since is None:
                self.sse_down_since = time.time()
            gevent.sleep(SSE_RETRY_SECONDS)

    def _read_stream(self, response: requests.Response, started: float) -> None:
        buffer = b""
        first = True
        last_frame = time.time()
        last_age_sample = 0.0
        for chunk in response.iter_content(chunk_size=None):
            if not chunk:
                continue
            now = time.time()
            buffer += chunk
            MINUTES.count("sse_bytes", len(chunk))
            while b"\n\n" in buffer:
                block, buffer = buffer.split(b"\n\n", 1)
                event, data, frame_id = "message", [], None
                for line in block.decode("utf-8", "replace").splitlines():
                    if line.startswith("id:"):
                        frame_id = line[3:].strip()
                    elif line.startswith("event:"):
                        event = line[6:].strip()
                    elif line.startswith("data:"):
                        data.append(line[5:].lstrip())
                if frame_id is not None:
                    self.last_event_id = frame_id
                if first:
                    self._fire("first frame", (now - started) * 1000)
                    first = False
                gap = now - last_frame
                last_frame = now
                if gap > 5:
                    # Runtime 空闲时每秒一个心跳：超过 5 秒没有任何帧就是一次卡顿。
                    self._fire("frame gap >5s", gap * 1000)
                name = event
                if data and event in ("message", "transient"):
                    try:
                        parsed = json.loads("\n".join(data))
                    except ValueError:
                        MINUTES.count("sse_bad_frame")
                        continue
                    name = str(parsed.get("name") or event)
                    if name in ("feed.market", "feed.market-patch", "feed.board", "feed.board-patch"):
                        age = self._push_age(parsed.get("timestamp"), now)
                        if age is not None:
                            if name.startswith("feed.market"):
                                self.minute["price_latency"].append(age)
                            if now - last_age_sample >= 5:
                                last_age_sample = now
                                self._fire("push age", age)
                MINUTES.count(f"frame:{name}")
            if self.sse_rotate:
                return

    def _push_age(self, stamp: Any, now: float) -> float | None:
        """行情帧服务端时间戳到收到的时延（ms）。VM 与宿主时钟差约 ±100 ms，所以减去近 10 分钟里
        观察到的最小差值（即“最快一帧”记为 0）：得到的是排队/推送延迟，不含固定的时钟偏差。"""
        if not isinstance(stamp, str):
            return None
        try:
            raw = (now - datetime.fromisoformat(stamp).timestamp()) * 1000
        except ValueError:
            return None
        minute = int(now // 60)
        if not self.age_floor or self.age_floor[-1][0] != minute:
            self.age_floor.append((minute, raw))
            while self.age_floor and self.age_floor[0][0] <= minute - 10:
                self.age_floor.pop(0)
        elif raw < self.age_floor[-1][1]:
            self.age_floor[-1] = (minute, raw)
        return max(0.0, raw - min(value for _, value in self.age_floor))

    def _send_feed(self) -> None:
        self.client.put(f"/api/sessions/{self.session_id}/feed", name="/api/sessions/[id]/feed",
                        json={"symbol": self.symbol, "durationSeconds": self.duration,
                              "marketPatches": True, "feedTouch": True})
        self._send_view()

    def _send_view(self) -> None:
        self.client.put(f"/api/sessions/{self.session_id}/feed/view", name="/api/sessions/[id]/feed/view",
                        json={"visible": self.visible, "board": self.board_want})

    def _random_board(self) -> dict[str, Any]:
        if random.random() < 0.3:
            return {"all": True, "exchanges": [], "products": [], "symbols": [], "key": "all"}
        products = sorted({symbol.split(".", 1)[1].rstrip("0123456789").lower() for symbol in self.watchlist})
        key = "w:" + hashlib.sha1(json.dumps(products).encode()).hexdigest()[:8]
        return {"all": False, "exchanges": [], "products": products, "symbols": [], "key": key}

    # -- RUM ----------------------------------------------------------------------------------

    @staticmethod
    def _new_minute() -> dict[str, Any]:
        return {"t": int(time.time()) // 60 * 60, "price_latency": [], "sse_down": [], "kline_first_paint": [],
                "sse_reconnect": 0}

    @staticmethod
    def _histogram(values: list[float]) -> dict[str, Any]:
        buckets: dict[str, int] = defaultdict(int)
        for value in values:
            buckets[str(bucket_index(value))] += 1
        return {"n": len(values), "sum": round(sum(values), 1), "buckets": dict(buckets)}

    def _rum_loop(self) -> None:
        gevent.sleep(60 - time.time() % 60 + random.uniform(1, 30))
        while True:
            minute, self.minute = self.minute, self._new_minute()
            metrics: dict[str, Any] = {}
            if not self.lcp_sent:  # 页面加载指标只在进页后的第一批里报一次
                self.lcp_sent = True
                metrics["lcp"] = self._histogram([random.lognormvariate(math.log(1400), 0.35)])
                metrics["ttfb"] = self._histogram([random.lognormvariate(math.log(120), 0.4)])
            for name in ("price_latency", "sse_down", "kline_first_paint"):
                if minute[name]:
                    metrics[name] = self._histogram(minute[name])
            metrics["sse_reconnect"] = {"n": minute["sse_reconnect"]}
            visible_ms = 60000 if self.visible else 0
            metrics["visible"] = {"n": 1 if self.visible else 0, "sum": visible_ms}
            body = {"v": 1, "session": self.rum_session, "client": "web", "network": "wifi",
                    "minutes": [{"t": minute["t"], "metrics": metrics}]}
            with self.client.post("/api/telemetry/rum", data=json.dumps(body, separators=(",", ":")),
                                  headers={"Content-Type": "text/plain"}, name="/api/telemetry/rum",
                                  catch_response=True) as response:
                if response.status_code != 200:
                    response.failure(f"rum {response.status_code}: {response.text[:120]}")
                else:
                    try:
                        if response.json().get("accepted") is not True:
                            response.failure(f"rum not accepted: {response.text[:120]}")
                    except ValueError:
                        response.failure("rum answer is not JSON")
            gevent.sleep(60 - time.time() % 60 + random.uniform(1, 30))

    # -- browsing -----------------------------------------------------------------------------

    def _load_chart(self, *, full: bool) -> None:
        params: dict[str, Any] = {"duration": self.duration}
        since = None if full else self.bars.get((self.symbol, self.duration))
        if since is not None:
            params["since"] = since
        started = time.time()
        response = self.client.get(f"/api/markets/{self.symbol}/workbench", params=params,
                                   name="/api/markets/[symbol]/workbench" + ("" if since is None else "?since"))
        if response.status_code == 200:
            self.minute["kline_first_paint"].append((time.time() - started) * 1000 + random.uniform(30, 120))
            bars = response.json().get("bars") or []
            if len(bars) >= 2 and isinstance(bars[-2].get("datetime"), int):
                self.bars[(self.symbol, self.duration)] = bars[-2]["datetime"]

    @task
    def tick(self) -> None:
        now = time.time()
        if now >= self.next_order:
            self.next_order = now + random.uniform(*self.order_every) * 60
            self.order_flow()
            return
        if now >= self.next_backtest:
            self.next_backtest = now + random.uniform(*self.backtest_every) * 60
            self.backtest()
            return
        action = random.choices(
            (self.quotes, self.market_detail, self.kline_refresh, self.switch_contract, self.board,
             self.account, self.runtime_status, self.background_tab),
            weights=(6, 4, 3, 3, 2, 3, 1, 1))[0]
        action()

    def quotes(self) -> None:
        self.client.get("/api/market/quotes", params={"symbols": ",".join(self.watchlist)}, name="/api/market/quotes")

    def market_detail(self) -> None:
        self.client.get(f"/api/market/{random.choice(self.watchlist)}", name="/api/market/[symbol]")

    def kline_refresh(self) -> None:
        self._load_chart(full=False)

    def switch_contract(self) -> None:
        self.symbol = random.choice([symbol for symbol in self.symbols if symbol != self.symbol] or self.symbols)
        if random.random() < 0.3:
            self.duration = random.choice(DURATIONS)
        self._load_chart(full=(self.symbol, self.duration) not in self.bars)
        self.client.put(f"/api/sessions/{self.session_id}/feed", name="/api/sessions/[id]/feed",
                        json={"symbol": self.symbol, "durationSeconds": self.duration,
                              "marketPatches": True, "feedTouch": True})

    def board(self) -> None:
        self.client.get("/api/market/board", name="/api/market/board")
        self.board_want = self._random_board()
        self._send_view()

    def account(self) -> None:
        self.client.get("/api/account/summary", name="/api/account/summary")
        self.client.get("/api/positions", name="/api/positions")
        self.client.get("/api/simulation/orders", params={"recoveryWait": "false"}, name="/api/simulation/orders")

    def runtime_status(self) -> None:
        self.client.get("/api/runtime/status", params={"mode": "cloud"}, name="/api/runtime/status")

    def background_tab(self) -> None:
        """切到别的标签页 30–180 秒：Runtime 暂停图表与总表推送，账户与事件照常。"""
        self.visible = False
        self._send_view()
        gevent.sleep(random.uniform(30, 180))
        self.visible = True
        self._send_view()

    # -- low-frequency jobs -------------------------------------------------------------------

    def backtest(self) -> None:
        with self.client.post("/api/backtests/reproducible", name="/api/backtests/reproducible",
                              json={"sessionId": self.session_id, "requestId": f"soak-{uuid.uuid4().hex[:16]}"},
                              catch_response=True) as response:
            if response.status_code != 201:
                response.failure(f"backtest {response.status_code}: {response.text[:120]}")
                return
            backtest_id = response.json().get("backtestId")
        if backtest_id:
            self.client.get(f"/api/backtests/{backtest_id}", name="/api/backtests/[id]")

    def order_flow(self) -> None:
        symbol = random.choice(self.watchlist)
        body: dict[str, Any] = {"sessionId": self.session_id, "symbol": symbol, "side": random.choice(("buy_open", "sell_open")),
                                "volume": 1, "priceIntent": "passive", "reason": "浸泡测试：模拟挂单后撤单"}
        if SIMULATION["trading"]:
            # 远离盘口的限价，挂得住、不成交，随后撤单。
            quote = self.client.get(f"/api/market/{symbol}", name="/api/market/[symbol]").json()
            tick = float(quote.get("priceTick") or 1)
            last = float(quote.get("lastPrice") or 0)
            if last > 0:
                offset = 20 * tick
                price = last - offset if body["side"] == "buy_open" else last + offset
                lower, upper = quote.get("lowerLimit"), quote.get("upperLimit")
                if lower and upper:
                    price = min(max(price, float(lower)), float(upper))
                body["limitPrice"] = round(round(price / tick) * tick, 6)
        with self.client.post("/api/simulation/proposals", json=body, name="/api/simulation/proposals",
                              catch_response=True) as response:
            if response.status_code != 201:
                response.failure(f"proposal {response.status_code}: {response.text[:120]}")
                return
            proposal = response.json()
        proposal_id, version = proposal["proposalId"], int(proposal.get("version") or 1)
        gevent.sleep(random.uniform(3, 10))  # 用户读卡
        if not SIMULATION["trading"]:
            self.client.post(f"/api/proposals/{proposal_id}/reject", name="/api/proposals/[id]/reject",
                             json={"proposalVersion": version, "idempotencyKey": uuid.uuid4().hex})
            MINUTES.count("order_card_rejected")
            return
        with self.client.post(f"/api/proposals/{proposal_id}/confirm", name="/api/proposals/[id]/confirm",
                              json={"approvalId": proposal.get("approvalId"), "proposalVersion": version,
                                    "idempotencyKey": uuid.uuid4().hex}, catch_response=True) as response:
            if response.status_code != 200:
                response.failure(f"confirm {response.status_code}: {response.text[:120]}")
                return
            order = (response.json() or {}).get("simulationOrder") or {}
        client_order_id = order.get("clientOrderId")
        if not client_order_id:
            MINUTES.count("order_not_sent")
            return
        MINUTES.count("order_sent")
        gevent.sleep(random.uniform(5, 30))
        self.client.post(f"/api/simulation/orders/{client_order_id}/cancel", data="{}",
                         headers={"Content-Type": "application/json"}, name="/api/simulation/orders/[id]/cancel")
