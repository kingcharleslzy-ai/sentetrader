"""Ephemeral public reader identities. Never enters account, agent or trading assembly."""
from __future__ import annotations

import asyncio
import json
import math
import os
import secrets
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from fastapi import HTTPException, Request, Response, WebSocket
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, ValidationError

from .auth import visitor_ip
from .plans import plan_for_user
from .shared_market import DURATIONS, SharedMarketService, event
from .skill_registry import skill_allowed, skill_by_id
from .ui_control import ChartOptionChange, MobileMarketChange, PinnedSymbolsChange
from .voice_intents import match_intent

COOKIE = "ai_trader_guest"
# Guests see one product only: the current 螺纹 main contract. The main follows the market
# source's main-contract flag (TqSdk query_cont_quotes, by volume/open interest), never a fixed code.
GUEST_PRODUCT = ("SHFE", "rb")
DESKTOP_ORIGINS = {"tauri://localhost", "http://tauri.localhost", "https://tauri.localhost"}


def guest_token(request):
    authorization = request.headers.get("authorization", "")
    if authorization:
        return authorization.removeprefix("Bearer guest.") if authorization.startswith("Bearer guest.") else ""
    offered = [part.strip() for part in request.headers.get("sec-websocket-protocol", "").split(",")]
    if "ai-trader-token" in offered:
        bearer = next((part for part in offered if part.startswith("bearer.guest.")), "")
        return bearer.removeprefix("bearer.guest.")
    return request.cookies.get(COOKIE, "")


def guest_request(request):
    authorization = request.headers.get("authorization", "")
    if authorization:
        return authorization.startswith("Bearer guest.")
    return bool(request.cookies.get(COOKIE)) and not any(request.cookies.get(name) for name in
        ("ai_trader_local_auth", "ai_trader_owner_auth", "ai_trader_cloud_auth"))


@dataclass
class Guest:
    token: str
    id: str
    window: str
    expires: float
    changes: dict = field(default_factory=dict)
    snapshot: dict = field(default_factory=dict)
    revision: int = 0
    streams: int = 0
    voice_active: bool = False
    changed: asyncio.Event = field(default_factory=asyncio.Event)


class GuestStreamResponse(StreamingResponse):
    """Release even if ASGI disconnects before consuming the first body frame."""

    def __init__(self, frames, service, guest, channel):
        super().__init__(frames, media_type="text/event-stream", headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})
        self.service, self.guest, self.channel = service, guest, channel

    async def __call__(self, scope, receive, send):
        expired = False
        try:
            async with asyncio.timeout(max(0, self.guest.expires - self.service.clock())):
                await super().__call__(scope, receive, send)
        except TimeoutError:
            expired = True
        finally:
            self.guest.streams -= 1
            self.service.release_market(self.channel)
        if expired:
            # Even the final ASGI send can be backpressured; subscriptions are already released.
            try:
                async with asyncio.timeout(.1):
                    await send({"type": "http.response.body", "body": b"", "more_body": False})
            except TimeoutError:
                pass


class UiRequest(BaseModel):
    windowId: str
    changes: dict | None = None
    snapshot: dict | None = None


class IntentRequest(BaseModel):
    windowId: str
    text: str = Field(max_length=256)


class GuestService:
    def __init__(self, ctx):
        self.ctx = ctx
        self.enabled = os.getenv("AI_TRADER_GUEST_ENABLED", "1" if ctx.settings.data_mode == "fake" else "0") == "1"
        self.market_service = ctx.application.state.shared_market
        self.sessions: dict[str, Guest] = {}
        self.budgets = OrderedDict()
        self.ttl = 3600
        self.capacity = 1000
        self.clock = time.time
        self._market_limit_cache = None

    def ip(self, request):
        # Same responsibility entry as the owner login/register failure budget
        # (auth.visitor_ip): the canonical request.client only — the nginx
        # boundary names the real visitor there; no request header is an identity.
        return visitor_ip(request)

    async def catalog(self):
        """The guest catalog: only the current 螺纹 main contract."""
        catalog = await self.market_service.instruments()
        items = [item for item in catalog["items"] if item.get("isMain") is True
                 and (item.get("exchangeId"), item.get("productId")) == GUEST_PRODUCT][:1]
        return {**catalog, "items": items}

    async def symbol(self, symbol):
        catalog = await self.catalog()
        if not catalog["items"]:
            raise HTTPException(503, "market_series_not_ready" if catalog.get("catalogStatus") in {"loading", "partial"} else "guest_market_source_unavailable")
        item = catalog["items"][0]
        if symbol.lower() not in (item["symbol"].lower(), str(item.get("instrumentId", "")).lower()):
            raise HTTPException(403, "guest_symbol_forbidden")
        return item["symbol"]

    def charge(self, key, limit, period=60, amount=1):
        now = self.clock()
        for old in list(self.budgets):
            if self.budgets[old][0] <= now:
                del self.budgets[old]
        expiry, used = self.budgets.get(key, (now + period, 0))
        if used + amount > limit:
            raise HTTPException(429, "guest_rate_limit")
        if key not in self.budgets and len(self.budgets) >= 10000:
            raise HTTPException(503, "guest_capacity")
        self.budgets[key] = (expiry, used + amount)

    def require(self, request, *, meter=True):
        if not self.enabled:
            raise HTTPException(404, "guest_disabled")
        token = guest_token(request)
        guest = self.sessions.get(token)
        if guest is None or guest.expires <= self.clock():
            self.sessions.pop(token, None)
            raise HTTPException(401, "guest_session_expired")
        if meter:
            self.charge(("request", guest.id), 180)
            self.charge(("request-ip", self.ip(request)), 1800)
        return guest

    def plan(self):
        return plan_for_user(self.ctx.services.store, "guest:reader")

    def voice_limit(self):
        skill = skill_by_id("voice-call")
        if skill is None or not skill_allowed(self.ctx.services.store, "guest:reader", skill):
            return 0
        return self.plan().voice_seconds_per_day or 0

    def market_projection(self, channel):
        # Cache only the public history limit: all SSE readers share a lookup
        # per second. Voice and other authorization still read current state.
        now = self.clock()
        if self._market_limit_cache is None or self._market_limit_cache[0] <= now:
            self._market_limit_cache = (now + 1, self.plan().market_history_trading_days)
        return self.market_service.project_packet(channel, self._market_limit_cache[1])

    async def market(self, symbol, duration):
        channel = await self.acquire_market(await self.symbol(symbol), duration)
        try:
            return self.market_projection(channel)["value"]
        finally:
            self.release_market(channel)

    def status(self, guest):
        voice = getattr(self.ctx.application.state, "voice_realtime", None)
        return {"role": "guest", "guestId": guest.id, "windowId": guest.window,
                "expiresAt": guest.expires, "readOnly": True,
                "voiceEnabled": bool(self.voice_limit() and voice and voice._read_key()),
                "marketHistoryTradingDays": self.plan().market_history_trading_days}

    def __getattr__(self, name):
        # Retain the guest read API while all roles use the application's one service.
        return getattr(self.market_service, name)

    def ui(self, guest):
        return {"windowId": guest.window, "changes": guest.changes, "snapshot": guest.snapshot, "revision": guest.revision}

    async def check_symbols(self, changes):
        """Symbols in guest UI changes must be the guest main contract; runs after the shape checks."""
        if isinstance(changes.get("symbol"), str):
            changes["symbol"] = await self.symbol(changes["symbol"])
        pinned = changes.get("pinnedSymbols")
        if isinstance(pinned, dict) and isinstance(pinned.get("add"), str):
            pinned["add"] = await self.symbol(pinned["add"])
        news = changes.get("news")
        if isinstance(news, dict) and isinstance(news.get("pinnedSymbol"), str):
            news["pinnedSymbol"] = await self.symbol(news["pinnedSymbol"])

    async def change(self, guest, window, changes):
        if window != guest.window:
            raise HTTPException(403, "guest_foreign_window")
        # Not here on purpose: researchDebate (多 AI 研讨, 2026-10-11) — a visitor reads the desk's facts but may
        # not start a debate (model calls on the platform's tokens) or open one.
        # Nor reminderCenter / mobilePage reminders (提醒中心, 2026-10-15): reminders are a member's own record.
        allowed = {"symbol", "duration", "theme", "newsOpen", "loginOpen", "kd", "macd", "volume", "news", "paneSizes", "researchExpanded",
                   "panel", "settingsOpen", "settingsPage", "dockSide", "dockWidth", "fontScale", "density",
                   "chartSizes", "chartOption", "pinnedSymbols", "dialog", "disclosure", "presentation", "mobileTab", "mobilePage", "mobileMarket",
                   "contractCatalogRetry"}
        # 手机版: of the full-screen pages a visitor may open only the 安装到桌面 guide (and close it).
        if set(changes) - allowed or ("mobilePage" in changes and changes["mobilePage"] not in (None, "install")):
            raise HTTPException(403, "guest_ui_action_forbidden")
        if "theme" in changes and changes["theme"] not in ("light", "dark", "system"):
            raise HTTPException(422, "guest_invalid_theme")
        if "duration" in changes and (not isinstance(changes["duration"], int) or changes["duration"] not in DURATIONS):
            raise HTTPException(422, "guest_invalid_duration")
        if "symbol" in changes and (not isinstance(changes["symbol"], str) or len(changes["symbol"]) > 40):
            raise HTTPException(422, "guest_invalid_symbol")
        # 合约抽屉的“重试” (2026-10-11): the client checks the drawer is open and shows 重试.
        if "contractCatalogRetry" in changes and changes["contractCatalogRetry"] is not True:
            raise HTTPException(422, "guest_invalid_ui_value")
        if any(k in changes and not isinstance(changes[k], bool) for k in ("newsOpen", "loginOpen", "kd", "macd", "volume", "researchExpanded", "settingsOpen")):
            raise HTTPException(422, "guest_invalid_ui_value")
        for key in ("paneSizes", "chartSizes"):
            if key not in changes:
                continue
            sizes = changes[key]
            if (not isinstance(sizes, dict) or set(sizes) != {"main", "kd", "macd"}
                or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not ((34 <= v <= 76) if k == "main" else (12 <= v <= 36)) for k, v in sizes.items())):
                raise HTTPException(422, "guest_invalid_pane_sizes")
        if "news" in changes:
            news = changes["news"]
            if (not isinstance(news, dict) or set(news) - {"tab", "pinnedSymbol"}
                or ("tab" in news and news["tab"] not in ("desk", "news", "data"))
                or (news.get("pinnedSymbol") is not None and (not isinstance(news["pinnedSymbol"], str) or len(news["pinnedSymbol"]) > 40))):
                raise HTTPException(422, "guest_invalid_news")
        for key, values in (
            # The 价格阶梯 ("depth") was removed 2026-10-07; its 五档 live in the 下单面板, which needs a login. The
            # 辩论 page (debate, 2026-10-24) needs one too: guests do not run or open debates.
            ("panel", (None, "news")),
            ("settingsPage", ("chart", "ui", "chart-advanced", "about", "feedback")),
            ("dockSide", ("left", "right")),
            ("fontScale", ("standard", "large", "extra-large")),
            ("density", ("compact", "standard", "comfortable")),
            # 手机版: a visitor's tabs; AI 对话 (chat) needs a login. notify is the retired 通知 tab (= ai).
            ("mobileTab", ("ai", "research", "market", "strategy", "me", "notify")),
        ):
            if key in changes and changes[key] not in values:
                raise HTTPException(422, "guest_invalid_ui_value")
        if "dockWidth" in changes and (type(changes["dockWidth"]) is not int or not 280 <= changes["dockWidth"] <= 1600):
            raise HTTPException(422, "guest_invalid_ui_value")
        # 手机看盘 (2026-10-09): a visitor switches 报价 / 多图 / 单图 (报价 and 多图 show 登录后使用) and the single
        # chart's sub pane and its sheet; the 报价 / 多图 settings are a member's account preferences.
        if "mobileMarket" in changes:
            market = changes["mobileMarket"]
            if not isinstance(market, dict) or set(market) - {"view", "subPane", "subPanes"}:
                raise HTTPException(403, "guest_ui_action_forbidden")
            try:
                MobileMarketChange.model_validate(market, strict=True)
            except ValidationError as error:
                raise HTTPException(422, "guest_invalid_ui_value") from error
        for key, model in (("chartOption", ChartOptionChange), ("pinnedSymbols", PinnedSymbolsChange)):
            if key not in changes:
                continue
            value = changes[key]
            if not isinstance(value, dict) or (key == "pinnedSymbols" and len(value) != 1):
                raise HTTPException(422, "guest_invalid_ui_value")
            try:
                model.model_validate(value, strict=True)
            except ValidationError as error:
                raise HTTPException(422, "guest_invalid_ui_value") from error
        if "dialog" in changes:
            dialog = changes["dialog"]
            if (not isinstance(dialog, dict) or set(dialog) != {"name", "open"}
                or dialog["name"] not in ("contract-drawer", "market-session") or type(dialog["open"]) is not bool):
                raise HTTPException(422, "guest_invalid_ui_value")
        if "disclosure" in changes:
            disclosure = changes["disclosure"]
            if (not isinstance(disclosure, dict) or set(disclosure) != {"name", "open"}
                or disclosure["name"] != "feedback-diagnostics" or type(disclosure["open"]) is not bool):
                raise HTTPException(422, "guest_invalid_ui_value")
        if "presentation" in changes:
            presentation = changes["presentation"]
            if not isinstance(presentation, dict) or set(presentation) != {"reset"} or presentation["reset"] is not True:
                raise HTTPException(422, "guest_invalid_ui_value")
        await self.check_symbols(changes)
        guest.changes.update(changes)
        guest.revision += 1
        changed, guest.changed = guest.changed, asyncio.Event()
        changed.set()


def register(app, ctx):
    app.state.shared_market = SharedMarketService(ctx)
    # 新浪备用: every runtime.status a client receives says so while the market serves it.
    ctx.services.broker.present_runtime_status = app.state.shared_market.present_status
    service = GuestService(ctx)
    app.state.guests = service
    # Reserved public-reader tier, no cloud user row and no fallback to UNLIMITED.
    store = ctx.services.store
    with store.lock, store.connection:
        store.connection.execute("INSERT OR IGNORE INTO plan_tiers (name,label,config,is_default,sort_order,created_at,updated_at) VALUES (?,?,?,0,99,datetime('now'),datetime('now'))",
            ("guest", "游客", json.dumps({"models": [], "dailyTokens": 0, "voiceSecondsPerDay": 60, "marketHistoryTradingDays": 3, "skills": ["voice-call"]})))

    @app.post("/api/auth/guest")
    async def bootstrap(request: Request, response: Response):
        if not service.enabled:
            raise HTTPException(404, "guest_disabled")
        origin = request.headers.get("origin")
        if origin and origin not in ctx.settings.origins:
            raise HTTPException(403, "guest_origin_forbidden")
        try:
            guest = service.require(request)
        except HTTPException as error:
            if error.status_code != 401:
                raise
            service.charge(("create", service.ip(request)), 30, 3600)
            now = service.clock()
            service.sessions = {k: v for k, v in service.sessions.items() if v.expires > now}
            if len(service.sessions) >= service.capacity:
                raise HTTPException(503, "guest_capacity")
            token = secrets.token_urlsafe(32)
            guest = Guest(token, "guest:" + secrets.token_hex(16), secrets.token_hex(16), now + service.ttl)
            service.sessions[token] = guest
        response.set_cookie(COOKIE, guest.token, max_age=max(0, int(guest.expires - service.clock())), httponly=True,
                            secure=request.url.scheme == "https", samesite="strict", path="/")
        payload = service.status(guest)
        if origin in DESKTOP_ORIGINS:
            payload["accessToken"] = "guest." + guest.token
        response.headers["Cache-Control"] = "no-store"
        return payload

    @app.get("/api/guest/session")
    async def session(request: Request):
        return service.status(service.require(request))

    @app.get("/api/guest/instruments")
    async def instruments(request: Request):
        service.require(request)
        return await service.catalog()

    @app.get("/api/guest/market")
    async def market(request: Request, symbol: str, duration: int = 300):
        service.require(request)
        return await service.market(symbol, duration)

    @app.get("/api/guest/stream")
    async def stream(request: Request, symbol: str, duration: int = 300):
        guest = service.require(request)
        if guest.streams >= 2:
            raise HTTPException(429, "guest_stream_limit")
        guest.streams += 1
        channel = None

        async def disconnected():
            while (await request.receive())["type"] != "http.disconnect":
                pass

        try:
            symbol = await service.symbol(symbol)
        except BaseException:
            guest.streams -= 1
            raise
        acquisition = asyncio.create_task(service.acquire_market(symbol, duration))
        disconnect = asyncio.create_task(disconnected())
        try:
            try:
                async with asyncio.timeout(max(0, guest.expires - service.clock())):
                    done, _ = await asyncio.wait((acquisition, disconnect), return_when=asyncio.FIRST_COMPLETED)
                    if acquisition not in done:
                        raise HTTPException(499, "guest_disconnected")
            finally:
                acquisition.cancel()
                disconnect.cancel()
                await asyncio.gather(acquisition, disconnect, return_exceptions=True)
            channel = acquisition.result()
        except BaseException as error:
            guest.streams -= 1
            # Cancellation can race the first successful frame before assignment.
            if acquisition.done() and not acquisition.cancelled() and acquisition.exception() is None:
                service.release_market(acquisition.result())
            if isinstance(error, TimeoutError):
                raise HTTPException(401, "guest_session_expired") from None
            raise
        async def frames():
            initial = service.market_projection(channel)
            previous_id = initial["id"]
            first_snapshot = initial["snapshot"]
            initial = None
            yield first_snapshot
            first_snapshot = None
            ui_revision = guest.revision
            yield event("ui", service.ui(guest))
            while guest.expires > service.clock() and not service.closed:
                market_changed, ui_changed = channel.changed, guest.changed
                if guest.revision != ui_revision:
                    ui_revision = guest.revision
                    yield event("ui", service.ui(guest))
                current = service.market_projection(channel)
                if current is not None and current["id"] != previous_id:
                    # One shared latest packet: lagging readers replace state, never replay a queue.
                    payload = current["update"] if current["previous"] == previous_id else current["snapshot"]
                    previous_id = current["id"]
                    current = None
                    yield payload
                    payload = None
                current = None
                waits = [asyncio.create_task(signal.wait()) for signal in (market_changed, ui_changed)]
                try:
                    done, _ = await asyncio.wait(waits, timeout=min(15, max(0, guest.expires - service.clock())), return_when=asyncio.FIRST_COMPLETED)
                    if not done:
                        yield ": heartbeat\n\n"
                finally:
                    for task in waits:
                        task.cancel()
                    await asyncio.gather(*waits, return_exceptions=True)
        return GuestStreamResponse(frames(), service, guest, channel)

    @app.get("/api/guest/research/{dataset}")
    async def research(request: Request, dataset: str, symbol: str):
        guest = service.require(request)
        service.charge(("research", guest.id), 20)
        service.charge(("research-ip", service.ip(request)), 100)
        symbol = await service.symbol(symbol)
        if dataset == "news":
            return await service.shared(("news", symbol), lambda: ctx.services.research_brief.brief(symbol, "news"), 60)
        if dataset == "desk":
            # Visitors see the desk's facts only; the AI judgment is for signed-in readers.
            report = await service.shared(("desk", symbol), lambda: ctx.services.research_desk.report(symbol, fast=True), 60)
            # …and no 新闻面 line: it is the AI's reading of the 快讯, which visitors do not get either.
            return {**{key: value for key, value in report.items() if key != "news"}, "judgment": {"status": "signin_required"}}
        if dataset == "summary":
            return await service.shared(("summary", symbol), lambda: ctx.services.research.summary(symbol), 60)
        from .research_data import DATASETS
        if dataset not in DATASETS:
            raise HTTPException(404, "research_dataset_unknown")
        return await service.shared((dataset, symbol), lambda: ctx.services.research.dataset(dataset, symbol), 60)

    @app.get("/api/guest/ui")
    async def ui_get(request: Request):
        return service.ui(service.require(request))

    @app.post("/api/guest/ui")
    async def ui_set(request: Request, body: UiRequest):
        guest = service.require(request)
        if body.windowId != guest.window:
            raise HTTPException(403, "guest_foreign_window")
        # Five Chinese news summaries plus controls exceed 16K ASCII-escaped JSON.
        # Bound actual UTF-8 storage and reject before applying any accompanying command.
        if body.snapshot is not None and len(json.dumps(body.snapshot, ensure_ascii=False).encode("utf-8")) > 64 * 1024:
            raise HTTPException(413, "guest_snapshot_too_large")
        if body.changes is not None:
            await service.change(guest, body.windowId, body.changes)
        if body.snapshot is not None:
            guest.snapshot = body.snapshot
            applied = body.snapshot.get("appliedRevision")
            if type(applied) is int and applied == guest.revision:
                guest.changes.clear()
        return service.ui(guest)

    @app.post("/api/guest/voice/intent")
    async def intent(request: Request, body: IntentRequest):
        guest = service.require(request)
        if body.windowId != guest.window:
            raise HTTPException(403, "guest_foreign_window")
        if not service.voice_limit():
            raise HTTPException(403, "guest_voice_unavailable")
        service.charge(("intent", guest.id), 30)
        text = body.text.strip()
        changes: dict[str, Any] | None = {"newsOpen": True} if text in ("打开新闻", "看看新闻", "查看新闻") else None
        match = match_intent(text)
        if changes is None and match and (match.kind == "researchDebate" or match.changes.get("panel") == "debate"
                                          or any("researchDebate" in step for step in match.follow)):
            return {"accepted": False, "changes": {}, "say": "多 AI 研讨要登录后才能用。", "reason": "guest_debate_forbidden"}
        if changes is None and match:
            changes = dict(match.changes)
            if match.symbol_query:
                items = (await service.instruments())["items"]
                found = [i for i in items if match.symbol_query.lower() in (i.get("symbol", "") + i.get("instrumentName", "")).lower()]
                changes = {"symbol": found[0]["symbol"]} if len(found) == 1 else None
                allowed = (await service.catalog())["items"]
                if changes and not (allowed and allowed[0]["symbol"] == changes["symbol"]):
                    return {"accepted": False, "changes": {}, "say": "游客只能看螺纹主力合约，登录后可看其他品种。", "reason": "guest_symbol_forbidden"}
            if match.follow:
                changes = None
        if not changes or set(changes) - {"symbol", "duration", "theme", "newsOpen"}:
            return {"accepted": False, "changes": {}, "say": "游客语音仅支持切换周期、主题和新闻。", "reason": "guest_intent_not_supported"}
        if changes.get("theme") == "toggle":
            changes["theme"] = "light" if guest.changes.get("theme") == "dark" else "dark"
        await service.change(guest, body.windowId, changes)
        return {"accepted": True, "changes": changes, "say": "界面操作已提交"}

    @app.websocket("/api/guest/voice/transcribe")
    async def transcribe(socket: WebSocket):
        try:
            guest = service.require(socket)
            if socket.headers.get("origin") not in ctx.settings.origins:
                raise HTTPException(403, "guest_origin_forbidden")
            voice = getattr(app.state, "voice_realtime", None)
            key = voice._read_key() if voice else None
            if not key or not service.voice_limit() or guest.voice_active:
                raise HTTPException(403, "guest_voice_unavailable")
            # Reserve full maximum up front: disconnect/cookie renewal cannot erase cost.
            service.charge(("voice", guest.id), service.voice_limit(), 86400, 15)
            service.charge(("voice-ip", service.ip(socket)), 600, 86400, 15)
        except HTTPException as error:
            await socket.close(code=4429 if error.status_code == 429 else 4403, reason=str(error.detail))
            return
        from .voice_realtime import CallIdentity
        from .voice_transcribe import run_transcribe
        assert voice is not None
        guest.voice_active = True
        await socket.accept(subprotocol="ai-trader-token" if "ai-trader-token" in socket.headers.get("sec-websocket-protocol", "") else None)
        try:
            await asyncio.wait_for(run_transcribe(voice, socket, key, CallIdentity(guest.id, {}), meter=False), timeout=15)
        except TimeoutError:
            await socket.close(code=4408, reason="guest_voice_duration_limit")
        finally:
            guest.voice_active = False


def guest_manifest(app, operation=None):
    from fastapi.routing import APIRoute
    operations = {f"{method} {route.path}": route for route in app.routes if isinstance(route, APIRoute)
                  if (route.path.startswith("/api/guest/") or route.path == "/api/auth/guest") and route.path != "/api/guest/stream"
                  for method in route.methods}
    if operation and operation not in operations:
        raise HTTPException(403, "guest_operation_forbidden")
    schema = app.openapi()
    selected = [operation] if operation else list(operations)
    return {"role": "guest", "readOnly": True,
            "uiControls": {"windowId": "own session window only", "changes": {"symbol": "the 螺纹 main contract from GET /api/guest/instruments (the only guest symbol)", "duration": sorted(DURATIONS), "theme": ["light", "dark", "system"], "newsOpen": "boolean", "loginOpen": "boolean", "kd": "boolean", "macd": "boolean", "volume": "boolean", "researchExpanded": "boolean", "news": {"tab": ["desk", "news", "data"], "pinnedSymbol": "guest symbol or null"}, "paneSizes": {"main": "34..76", "kd": "12..36", "macd": "12..36"},
                "panel": [None, "news"], "settingsOpen": "boolean", "settingsPage": ["chart", "ui", "chart-advanced", "about", "feedback"],
                "dockSide": ["left", "right"], "dockWidth": "integer 280..1600", "fontScale": ["standard", "large", "extra-large"],
                "density": ["compact", "standard", "comfortable"], "chartSizes": {"main": "34..76", "kd": "12..36", "macd": "12..36"},
                "chartOption": {"name": ["kd", "macd", "volume", "crosshair", "lastPriceLine", "ma5", "ma10", "ma25", "ma60", "ma200"], "visible": "boolean"},
                "pinnedSymbols": {"add": "guest symbol; exactly one of add/remove", "remove": "catalog symbol; exactly one of add/remove"},
                "dialog": {"name": ["contract-drawer", "market-session"], "open": "boolean"},
                "contractCatalogRetry": "true: the contract drawer's 重试 after a failed or timed-out GET /api/guest/instruments (status error) or an empty public catalog (status empty); needs the drawer open and snapshot contractCatalog.canRetry (status loading|error|ready|empty|expired; rejected while loading, once read, or when the guest session expired — use 重新连接 then); re-reads the catalog only, never creates a guest session",
                "disclosure": {"name": ["feedback-diagnostics"], "open": "boolean"}, "presentation": {"reset": True},
                "mobileTab": "phone-layout windows only: market 看盘 / research 调研 (read-only); ai / strategy / me show the 登录后使用 page (the retired notify = ai; snapshot mobileLayout, mobileTab)",
                "mobilePage": "phone-layout windows only: install opens the 安装到桌面 guide, null closes it (snapshot mobilePage, installEnvironment); other pages need a login",
                "mobileMarket": "phone-layout windows only, one key: view quotes / multi / single (报价 and 多图 show 登录后使用), subPane kd / macd / rsi / oi / cci / volume (the pane under the single chart), subPanes boolean (副图设置 sheet; disclosures.mobile-subpanes)"}, "receipt": "POST snapshot with appliedRevision after applying GET changes/revision"},
            "operations": [{"operation": op, "schema": schema["paths"][operations[op].path][op.split()[0].lower()]} for op in selected],
            "schemas": {k: v for k, v in schema.get("components", {}).get("schemas", {}).items() if k in {"UiRequest", "IntentRequest", "HTTPValidationError", "ValidationError"}},
            "streams": [{"operation": "GET /api/guest/stream", "transport": "SSE"},
                        {"operation": "WS /api/guest/voice/transcribe", "transport": "PCM16/16000; end; partial/final"}]}


async def guest_call(app, request, call):
    from urllib.parse import quote

    import httpx
    guest_manifest(app, call.operation)
    if call.replayId:
        raise HTTPException(403, "guest_operation_forbidden")
    method, path = call.operation.split(" ", 1)
    for key, value in call.path.items():
        path = path.replace("{" + key + "}", quote(value, safe=""))
    if "{" in path or ".." in path:
        raise HTTPException(422, "guest_invalid_path")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=(request.client.host, request.client.port) if request.client else ("unknown", 0)), base_url="http://guest-internal") as client:
        response = await client.request(method, path, params=call.query, json=call.body,
                                        headers={key: request.headers[key] for key in ("cookie", "authorization", "origin", "x-real-ip") if key in request.headers})
    return {"ok": response.is_success, "status": response.status_code, "operation": call.operation, "data": response.json()}
