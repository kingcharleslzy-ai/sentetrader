"""锁屏推送 (2026-10-06): standard Web Push (VAPID, aes128gcm-encrypted payloads) to the devices a user
turned it on for — an iPhone running Sente from its home-screen icon, Safari on a Mac, Edge, Firefox.
Chrome and Android browsers subscribe through Google's FCM, which this server cannot reach: the client
offers them no switch and the subscription route refuses an FCM endpoint.

What goes out is decided in one place (`PushHub._notify`): the user's own subscriptions, the event's
switch in 通知哪些事 (shared with the desktop notifications; 急停 always), never a device that has an
online window whose page is in front (that page shows the event itself), and for an account's event
only that account's owners. A push carries a title, a body (a figure-free short line when 锁屏上 ·
显示金额和手数 is off), a tag and a deep link into the app — never an order action.

Sending runs on worker threads off the event loop with a 10 s timeout and is never retried: 404/410
removes the subscription, anything else is logged on the row. Without a VAPID private key
(AI_TRADER_WEBPUSH_VAPID_PRIVATE_KEY_FILE) push is "not configured": the routes say so and every
other feature works as before. The key's public half reaches clients through GET /api/app/push."""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import math
import re
import time
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import quote, urlparse

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from .auth import OwnerAuth, OwnerIdentity
from .conversation_cards import SIDE_LABELS
from .instruments import foreign_display_name, is_foreign_paper
from .reminder_kinds import reminder_enabled
from .user_preferences import preference_value

log = logging.getLogger("uvicorn.error.webpush")

PRIVATE_KEY_SECRET = "AI_TRADER_WEBPUSH_VAPID_PRIVATE_KEY"
# Apple requires the VAPID subject to be a mailto: or an https URL, and py_vapid accepts an https
# subject only as a bare origin — a path ("/trader/") fails signing with VapidException (10-06).
SUBJECT = "https://qilinshuzhi.com"
SEND_TIMEOUT_SECONDS = 10.0
TTL_SECONDS = 1800
# A fill older than this when first seen is history (a provider that came back late), not news.
FILL_RECENT_SECONDS = 600
# The first read after a start tells a cancel only for an order placed this recently (#932).
CANCEL_RECENT_SECONDS = 1800
SEEN_CAP = 5000

# The push services the server delivers to (Apple, Microsoft, Mozilla — each reachable from the
# cloud host, checked 2026-10-06). Anything else is refused: the server POSTs to this URL.
_ALLOWED_HOSTS = ("web.push.apple.com", "updates.push.services.mozilla.com")
_ALLOWED_SUFFIXES = (".notify.windows.com", ".push.services.mozilla.com")
_FCM_HOSTS = ("fcm.googleapis.com", "android.googleapis.com")

# 通知哪些事: a push message's kind → the reminder kind whose one switch gates it (reminder_kinds.KINDS;
# the conversation, desktop notification and voice obey the same switch, #932). A newer reminder pushes
# under its own kind; the older message kinds keep their names (their tags and links are unchanged).
PUSH_KINDS = {
    "morning": "morning_desk", "movement": "market_movement", "price_alert": "price_alert",
    "market_close": "market_close", "risk": "risk_ratio", "fill": "fill", "cancel": "cancel",
    "strategy": "strategy", "review": "daily_review", "breakeven": "breakeven", "emergency": "emergency_stop",
}
ALWAYS_KINDS = frozenset({"emergency", "emergency_stop", "test", "admin_alert"})


# -- pure rules ----------------------------------------------------------------------------------

def endpoint_refusal(endpoint: str) -> str | None:
    """None for a push service the server delivers to; `push_endpoint_unsupported` for Google's FCM
    (unreachable from here); `push_endpoint_invalid` for anything else."""
    try:
        url = urlparse(str(endpoint))
    except ValueError:
        return "push_endpoint_invalid"
    host = (url.hostname or "").lower()
    if url.scheme != "https" or not host or url.username or url.password or url.port not in (None, 443):
        return "push_endpoint_invalid"
    if host in _FCM_HOSTS or host.endswith(".googleapis.com"):
        return "push_endpoint_unsupported"
    if host in _ALLOWED_HOSTS or host.endswith(_ALLOWED_SUFFIXES):
        return None
    return "push_endpoint_invalid"


def kind_enabled(preferences: dict[str, Any], kind: str) -> bool:
    """Whether the user's 通知哪些事 lets this kind out (急停 and 发一条试试 always)."""
    if kind in ALWAYS_KINDS:
        return True
    return reminder_enabled(preferences, PUSH_KINDS.get(kind, kind))


def admin_push_user_id(app: Any) -> str | None:
    """The configured OwnerAuth owner (production: charles), using the admin catalog's no-plan rule.
    Never enumerate subscribers or treat every missing user-plan row as an administrator."""
    auth = getattr(app.state, "auth", None)
    if not isinstance(auth, OwnerAuth):
        return None
    user_id = OwnerIdentity(auth.username).scoped_user_id
    return user_id if app.state.store.get_user_plan(user_id) is None else None


def show_amounts(preferences: dict[str, Any]) -> bool:
    return preference_value(preferences, "notify.lockScreenAmounts") is not False


@dataclass(frozen=True)
class PushMessage:
    """One lock-screen notification. `short` / `short_title` are the figure-free versions shown when
    锁屏上 · 显示金额和手数 is off; `url` is the deep link (a query on the app's root)."""
    kind: str
    title: str
    body: str
    short: str
    tag: str
    url: str = ""
    short_title: str | None = None


def push_payload(message: PushMessage, *, amounts: bool) -> dict[str, Any]:
    return {"title": message.title if amounts else (message.short_title or message.title),
            "body": message.body if amounts else message.short,
            "tag": message.tag, "url": message.url, "kind": message.kind}


def push_targets(subscriptions: list[dict[str, Any]], visible_devices: set[str]) -> list[dict[str, Any]]:
    """The devices to push to: every subscription except a device whose page is in front right now."""
    return [item for item in subscriptions if item["deviceId"] not in visible_devices]


def deep_link(kind: str, value: str = "") -> str:
    return f"?open={quote(kind + (':' + value if value else ''), safe=':.-_')}"


# -- the messages ----------------------------------------------------------------------------------

def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _price(value: Any) -> str:
    amount = _number(value)
    if amount is None:
        return "—"
    return f"{amount:,.0f}" if amount == int(amount) else f"{amount:,.4f}".rstrip("0").rstrip(".")


def _side(direction: Any, offset: Any) -> str:
    return SIDE_LABELS.get((str(direction or "").lower(), str(offset or "").upper()), "成交")


def fill_message(*, name: str, direction: Any, offset: Any, volume: int, price: Any, order_id: str) -> PushMessage:
    side = _side(direction, offset)
    return PushMessage(kind="fill", title=f"成交 · {name} {side} {volume} 手", short_title=f"成交 · {name}",
                       body=f"{side} {volume} 手，成交价 {_price(price)}。", short=f"{name}有新成交",
                       tag=f"fill-{order_id}", url=deep_link("mobilePage", "positions"))


# The gateway's own words for a cancel (“已撤单”) say nothing the reminder's title does not (#932: “已撤单：已撤单”).
_PLAIN_CANCEL = re.compile(r"^(?:已|全部)?撤(?:单|销)(?:成功)?[。.!！]?$")


def cancel_detail(*, left: int, total: int, reason: str) -> str:
    """撤单提醒的一句 (提醒中心与锁屏同一口径): how much of the order filled before the rest was cancelled, and the
    gateway's reason when it says more than “已撤单”."""
    filled = max(0, total - left)
    reason = (reason or "").strip()
    text = f"已成交 {filled} 手，余 {left} 手已撤" if filled else f"{left} 手未成交，已撤"
    return text + (f"：{reason[:60]}" if reason and not _PLAIN_CANCEL.match(reason) else "")


def cancel_message(*, name: str, direction: Any, offset: Any, volume: int, reason: str, order_id: str,
                   total: int = 0) -> PushMessage:
    side = _side(direction, offset)
    return PushMessage(kind="cancel", title=f"已撤单 · {name} {side} {max(volume, total)} 手", short_title=f"已撤单 · {name}",
                       body=f"{cancel_detail(left=volume, total=total, reason=reason)}。".replace("。。", "。"),
                       short=f"{name}有挂单被撤", tag=f"cancel-{order_id}", url=deep_link("mobilePage", "positions"))


def reminder_message(*, kind: str, text: str, card_id: str, account_id: str = "") -> PushMessage:
    """收盘前持仓 (market_close_day / _night) and 风险度 (risk_ratio): the reminder's own text; the tag
    carries the account, so two accounts' reminders do not replace each other on the lock screen."""
    close = kind.startswith("market_close")
    body = re.sub(r"^.*?(收盘提醒|风险度提醒)：", "", text)
    return PushMessage(kind="market_close" if close else "risk", title="收盘前持仓" if close else "风险度",
                       body=body, short="快收盘了，还有持仓未平" if close else "风险度偏高，留意仓位",
                       tag=f"reminder-{kind}-{account_id}" if account_id else f"reminder-{kind}", url=deep_link("card", card_id))


def price_alert_message(*, name: str, symbol: str, text: str, alert_id: str) -> PushMessage:
    return PushMessage(kind="price_alert", title=f"到价提醒 · {name}", body=re.sub(r"^到价提醒：", "", text),
                       short=f"{name}到了你设的价位", tag=f"price-{alert_id}", url=deep_link("symbol", symbol))


def strategy_message(*, name: str, text: str, card_id: str | None) -> PushMessage:
    body = text.split("：", 1)[1] if "：" in text else text
    return PushMessage(kind="strategy", title=f"策略信号 · {name}", body=body, short=f"{name}有新信号",
                       tag=f"strategy-{name}", url=deep_link("card", card_id) if card_id else deep_link("chat"))


def emergency_message() -> PushMessage:
    return PushMessage(kind="emergency", title="急停", body="AI 已全局急停：模拟执行已停止，恢复前不会再下单。",
                       short="AI 已全局急停", tag="emergency", url=deep_link("mobilePage", "positions"))


def test_message() -> PushMessage:
    return PushMessage(kind="test", title="Sente 试一条", body="锁屏提醒已打开，这台设备能收到。",
                       short="锁屏提醒已打开", tag="test", url="")


def movement_message(*, name: str, body: str, card_id: str) -> PushMessage:
    return PushMessage(kind="movement", title=f"盘中异动 · {name}", body=body, short=f"{name}有新异动",
                       tag=f"movement-{name}", url=deep_link("card", card_id))


def review_message(*, trading_day: str, legs: int, planned: int, pnl: str | None, account_id: str) -> PushMessage:
    """收盘复盘 (2026-10-06): the day's counts and net P&L; without amounts the line carries no figure at all."""
    money = f"，当日 {pnl}" if pnl else ""
    # 美元子账户（外盘）的复盘在 CME 收盘后（美东 17:15）才到，金额是美元（pnl 已带单位）。
    foreign = is_foreign_paper(account_id)
    return PushMessage(kind="review", title="外盘复盘好了" if foreign else "今天的复盘好了",
                       body=f"{legs} 笔成交，{planned} 笔计划内{money}。点开看每一笔。",
                       short=f"{'外盘' if foreign else '收盘'}复盘好了，点开看每一笔。", tag=f"review-{account_id}-{trading_day}",
                       # 外盘复盘的链接带美元子账户（review:<交易日>:<账户>），点开先切到那个账户再看那天的复盘。
                       url=deep_link("review", f"{trading_day}:{account_id}" if foreign else trading_day))


def breakeven_message(*, name: str, body: str, card_id: str) -> PushMessage:
    """保本提醒 (2026-10-06): the card's question; without amounts the line carries no price."""
    return PushMessage(kind="breakeven", title=f"保本提醒 · {name}", body=body,
                       short=f"{name}的浮盈够了，要不要把止损移到成本？", tag=f"breakeven-{card_id}",
                       url=deep_link("card", card_id))


def morning_message(*, lines: list[str]) -> PushMessage:
    shown = "、".join(lines[:3]) + ("…" if len(lines) > 3 else "")
    return PushMessage(kind="morning", title="今日作战表", body=f"{len(lines)} 个自选品种的作战表排好了：{shown}",
                       short="今日作战表排好了", tag="morning", url=deep_link("research", "sheet"))


def activity_events(seen: dict[str, set[str]] | None, activity: dict[str, Any], *, now_ns: int,
                    since_ns: int | None = None, retrying: set[str] | frozenset[str] = frozenset(),
                    ) -> tuple[dict[str, set[str]], list[dict[str, Any]], list[dict[str, Any]]]:
    """成交与撤单's one detector (the account ledger calls it on every activity read): the fills and
    the cancelled orders this read shows for the first time. A fill older than FILL_RECENT_SECONDS when
    first seen is history. The first read after a start only seeds what is already there — except, with
    `since_ns` (#932: when this database's reminder claims began), a fill made after it and an order placed
    after it (within CANCEL_RECENT_SECONDS) cancelled since: a restart or a slot switch then still tells
    them, and the reminder claims keep the ones the previous holder already told quiet. Fills of one order
    in one read become one event (with their trade ids). `retrying` are fills whose reminder failed before
    (#932 R2): the caller bounds them from the failure, so the age rule does not drop them. Returns (seen, fills,
    cancels)."""
    trades = [item for item in activity.get("trades") or [] if isinstance(item, dict) and item.get("tradeId")]
    orders = [item for item in activity.get("orders") or [] if isinstance(item, dict) and item.get("clientOrderId")]
    cancelled = [item for item in orders if str(item.get("status")) == "FINISHED" and int(_number(item.get("volumeLeft")) or 0) > 0]
    if seen is None:
        if since_ns is None:
            return ({"trades": {str(item["tradeId"]) for item in trades},
                     "cancelled": {str(item["clientOrderId"]) for item in cancelled}}, [], [])
        recent = now_ns - CANCEL_RECENT_SECONDS * 1_000_000_000
        seen = {"trades": {str(item["tradeId"]) for item in trades
                           if int(_number(item.get("tradeDateTime")) or 0) < since_ns},
                "cancelled": {str(item["clientOrderId"]) for item in cancelled
                              if int(_number(item.get("insertDateTime")) or 0) < max(since_ns, recent)}}
    fresh = [item for item in trades if str(item["tradeId"]) not in seen["trades"]]
    grouped: dict[str, dict[str, Any]] = {}
    for trade in fresh:
        at = int(_number(trade.get("tradeDateTime")) or 0)
        if at and now_ns - at > FILL_RECENT_SECONDS * 1_000_000_000 and str(trade["tradeId"]) not in retrying:
            continue
        key = str(trade.get("clientOrderId") or trade["tradeId"])
        entry = grouped.setdefault(key, {"orderId": key, "symbol": str(trade.get("symbol") or ""),
                                         "direction": trade.get("direction"), "offset": trade.get("offset"),
                                         "volume": 0, "price": trade.get("price"), "tradeIds": [], "trades": []})
        entry["volume"] += int(_number(trade.get("volume")) or 0)
        entry["price"] = trade.get("price")
        entry["tradeIds"].append(str(trade["tradeId"]))
        # Each fill on its own (#932 P2): the ledger tells only the ones no reminder told yet.
        entry["trades"].append({"id": str(trade["tradeId"]), "volume": int(_number(trade.get("volume")) or 0),
                                "price": trade.get("price")})
    cancels = [{"orderId": str(item["clientOrderId"]), "symbol": str(item.get("symbol") or ""),
                "direction": item.get("direction"), "offset": item.get("offset"),
                "volume": int(_number(item.get("volumeLeft")) or 0),
                "total": int(_number(item.get("volume")) or 0),
                "reason": str(item.get("lastMsg") or "")}
               for item in cancelled if str(item["clientOrderId"]) not in seen["cancelled"]]
    next_seen = {"trades": seen["trades"] | {str(item["tradeId"]) for item in trades},
                 "cancelled": seen["cancelled"] | {str(item["clientOrderId"]) for item in cancelled}}
    for name, current in (("trades", {str(item["tradeId"]) for item in trades}),
                          ("cancelled", {str(item["clientOrderId"]) for item in cancelled})):
        if len(next_seen[name]) > SEEN_CAP:
            next_seen[name] = current
    return next_seen, list(grouped.values()), cancels


def owns_account(store: Any, account_id: str, user_id: str) -> bool:
    """只推本人账户的事: the account's owner (or, in a single-tenant runtime without grants, its user)."""
    role = store.account_role(account_id=account_id, user_id=user_id)
    if role is not None:
        return role == "owner"
    return bool(getattr(store, "idp_fallback", False)) and account_id == store.default_trading_account_id


# -- VAPID ---------------------------------------------------------------------------------------

def load_vapid(text: str | None) -> Any | None:
    """The VAPID key from its PEM (PKCS#8 or SEC1 "EC PRIVATE KEY"); None when empty or unreadable
    (the reason is logged, never the key)."""
    if not text or not text.strip():
        return None
    try:
        from py_vapid import Vapid
        key = serialization.load_pem_private_key(text.strip().encode(), password=None)
        if not isinstance(key, ec.EllipticCurvePrivateKey) or key.curve.name != "secp256r1":
            raise ValueError("vapid_key_not_p256")
        return Vapid(private_key=key)
    except Exception as error:  # noqa: BLE001 — a bad key leaves push unconfigured, nothing else
        log.warning("webpush_vapid_key_unusable reason=%s", type(error).__name__ if not isinstance(error, ValueError) else error)
        return None


def public_key_text(vapid: Any) -> str:
    """The applicationServerKey a browser subscribes with: the uncompressed P-256 point, base64url."""
    raw = vapid.public_key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


# -- the hub -------------------------------------------------------------------------------------

class PushHub:
    """The main runtime's one push sender (a replay child has none). `sender` is the blocking send
    (subscription, payload) → HTTP status; tests pass their own."""

    def __init__(self, app: Any, vapid: Any | None, *, sender: Any = None) -> None:
        self.app = app
        self.vapid = vapid
        self.public_key = public_key_text(vapid) if vapid is not None else None
        self._sender = sender or self._webpush_send
        self._test_sender = sender is not None
        self._tasks: set[asyncio.Task[Any]] = set()
        self._names: tuple[float, dict[str, str]] = (0.0, {})

    @property
    def configured(self) -> bool:
        return self.vapid is not None or self._test_sender

    # The entry every source calls: fire and forget, so a slow push service never holds a loop.
    def notify(self, user_id: str, message: PushMessage, *, account_id: str | None = None) -> None:
        if not self.configured or not user_id:
            return
        try:
            task = asyncio.get_running_loop().create_task(self._notify(user_id, message, account_id))
        except RuntimeError:
            return
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _notify(self, user_id: str, message: PushMessage, account_id: str | None) -> None:
        try:
            store = self.app.state.store
            if message.kind == "admin_alert" and user_id != admin_push_user_id(self.app):
                return
            if account_id is not None and not owns_account(store, account_id, user_id):
                return
            subscriptions = store.list_push_subscriptions(user_id)
            if not subscriptions:
                return
            preferences = store.user_preferences(user_id).model_dump()
            if not kind_enabled(preferences, message.kind):
                return
            targets = push_targets(subscriptions, self._visible(user_id))
            if not targets:
                return
            payload = push_payload(message, amounts=show_amounts(preferences))
            await asyncio.gather(*(self._deliver(user_id, item, payload) for item in targets))
        except Exception:  # noqa: BLE001 — a push never breaks the event that caused it
            log.warning("webpush_notify_failed kind=%s", message.kind, exc_info=True)

    async def send_test(self, user_id: str, device_id: str | None = None) -> list[dict[str, Any]]:
        """发一条试试: to every device of the user (or the one named), whatever the switches and the
        page in front say."""
        store = self.app.state.store
        targets = [item for item in store.list_push_subscriptions(user_id)
                   if device_id is None or item["deviceId"] == device_id]
        preferences = store.user_preferences(user_id).model_dump()
        payload = push_payload(test_message(), amounts=show_amounts(preferences))
        return list(await asyncio.gather(*(self._deliver(user_id, item, payload) for item in targets)))

    def _visible(self, user_id: str) -> set[str]:
        lookup = getattr(self.app.state, "ui_visible_devices", None)
        try:
            return set(lookup(user_id)) if lookup is not None else set()
        except Exception:  # noqa: BLE001
            return set()

    async def _deliver(self, user_id: str, subscription: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
        store = self.app.state.store
        result = {"deviceId": subscription["deviceId"], "platform": subscription["platform"]}
        try:
            status = await asyncio.wait_for(asyncio.to_thread(self._sender, subscription, payload),
                                            timeout=SEND_TIMEOUT_SECONDS + 2)
        except Exception as error:  # noqa: BLE001 — timeout, DNS, TLS: logged on the row, not retried
            reason = "timeout" if isinstance(error, TimeoutError) else type(error).__name__
            log.warning("webpush_send_failed device=%s reason=%s", subscription["deviceId"][:12], reason)
            store.record_push_result(user_id=user_id, endpoint=subscription["endpoint"], error=reason)
            return {**result, "status": "failed", "detail": reason}
        if 200 <= status < 300:
            store.record_push_result(user_id=user_id, endpoint=subscription["endpoint"], error=None)
            return {**result, "status": "sent", "detail": None}
        if status in (404, 410):
            # The push service says the subscription is gone (unsubscribed, expired, app removed).
            store.delete_push_subscriptions(user_id=user_id, endpoint=subscription["endpoint"])
            log.info("webpush_subscription_gone device=%s status=%s", subscription["deviceId"][:12], status)
            return {**result, "status": "removed", "detail": f"HTTP {status}"}
        log.warning("webpush_send_refused device=%s status=%s", subscription["deviceId"][:12], status)
        store.record_push_result(user_id=user_id, endpoint=subscription["endpoint"], error=f"HTTP {status}")
        return {**result, "status": "failed", "detail": f"HTTP {status}"}

    def _webpush_send(self, subscription: dict[str, Any], payload: dict[str, Any]) -> int:
        from pywebpush import WebPushException, webpush
        try:
            response = webpush({"endpoint": subscription["endpoint"], "keys": subscription["keys"]},
                               data=json.dumps(payload, ensure_ascii=False), vapid_private_key=self.vapid,
                               vapid_claims={"sub": SUBJECT}, timeout=SEND_TIMEOUT_SECONDS, ttl=TTL_SECONDS)
        except WebPushException as error:
            status = getattr(error.response, "status_code", None)
            if status is None:
                raise
            return int(status)
        return int(getattr(response, "status_code", 201))

    async def instrument_name(self, symbol: str) -> str:
        """The contract's name off the shared catalog (cached a minute); the code when unknown. A foreign contract
        (CME.GCEZ26, KQD.m@COMEX.GC) is named from its code alone (“COMEX黄金2612”) — the 快期 catalog lacks it."""
        if foreign := foreign_display_name(symbol):
            return foreign
        cached_at, names = self._names
        if time.monotonic() - cached_at > 60:
            shared = getattr(self.app.state, "shared_market", None)
            try:
                items = (await shared.instruments()).get("items") or [] if shared is not None else []
                names = {str(item.get("symbol") or "").upper(): str(item.get("instrumentName") or "") for item in items}
            except Exception:  # noqa: BLE001
                names = names or {}
            self._names = (time.monotonic(), names)
        return names.get(symbol.upper()) or symbol.split(".", 1)[-1]

    async def morning_desk(self, judgments: dict[str, dict[str, Any]], research_day: str) -> None:
        """开盘前 · 今日作战表: after the 08:45 pre-generation, one reminder per user naming their 自选品种 (or,
        without any, their AI 简报 products) researched today — no 偏多 / 偏空 (owner 2026-10-25); the push and the
        row open the 调研 panel's 今日作战表 — an 信息 kind (#932): the 提醒中心 row,
        feed.reminder and the push, never a conversation message. Once per user per research day (the
        push_notices row stays the claim a slot running the previous build reads)."""
        from .reminders import ReminderSpec, deliver
        store = self.app.state.store
        today = {key.lower(): value for key, value in judgments.items() if value.get("researchDay") == research_day}
        users = set(store.push_user_ids())
        accounts = getattr(self.app.state, "accounts", None)
        for services in list(accounts.all()) if accounts is not None else []:
            users.update(store.account_owner_ids(services.account_id))
        for user_id in sorted(user for user in users if user):
            try:
                preferences = store.user_preferences(user_id)
                if not kind_enabled(preferences.model_dump(), "morning"):
                    continue
                products = list(preferences.watchlist.products) or list(preferences.briefs.products or [])
                names = [str(today[key.lower()].get("name") or key.split(".", 1)[-1])
                         for key in products if key.lower() in today]
                if not names:
                    continue
                message = morning_message(lines=names)

                def claim(connection: Any, user_id: str = user_id) -> bool:
                    return bool(store.claim_push_notice(user_id=user_id, key=f"morning:{research_day}",
                                                        connection=connection))

                await deliver(self.app, ReminderSpec(
                    kind="morning_desk", user_id=user_id, dedupe_key=research_day, trading_day=research_day,
                    title="今日作战表", body="、".join(names), name=" · ".join(names[:3]), target={"type": "research", "page": "sheet"},
                    push=message, legacy=claim))
            except Exception:  # noqa: BLE001
                log.warning("morning_desk_reminder_failed", exc_info=True)

    async def drain(self) -> None:
        """Wait for the pushes already handed off (tests; shutdown)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def close(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*list(self._tasks), return_exceptions=True)


# -- routes --------------------------------------------------------------------------------------

class PushKeys(BaseModel):
    model_config = ConfigDict(extra="forbid")
    p256dh: str = Field(min_length=16, max_length=200, pattern=r"^[A-Za-z0-9_\-]+=*$")
    auth: str = Field(min_length=8, max_length=100, pattern=r"^[A-Za-z0-9_\-]+=*$")


class PushSubscriptionBody(BaseModel):
    """PushSubscription.toJSON() as the browser gives it."""
    model_config = ConfigDict(extra="forbid")
    endpoint: str = Field(min_length=12, max_length=1000)
    keys: PushKeys
    expirationTime: float | None = None


class PushSubscribe(BaseModel):
    model_config = ConfigDict(extra="forbid")
    deviceId: str = Field(min_length=8, max_length=100, description="本设备 id（与界面心跳的 device.deviceId 相同）")
    platform: str = Field(min_length=1, max_length=40, description="设备名，如“iPhone · 桌面图标”“Mac · Safari”")
    subscription: PushSubscriptionBody


class PushDevice(BaseModel):
    deviceId: str
    platform: str
    createdAt: str
    lastOkAt: str | None = None
    lastError: str | None = None


class PushStatus(BaseModel):
    configured: bool = Field(description="服务端是否已配置推送密钥；false 时不能开启")
    publicKey: str | None = Field(default=None, description="VAPID 公钥（applicationServerKey，base64url）")
    devices: list[PushDevice]


class PushTest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    deviceId: str | None = Field(default=None, min_length=8, max_length=100, description="只发给这台；省略发给本人所有设备")


class PushTestResult(BaseModel):
    deviceId: str
    platform: str
    status: Literal["sent", "removed", "failed"]
    detail: str | None = None


class PushTestResults(BaseModel):
    results: list[PushTestResult]


def _hub(app: Any) -> PushHub | None:
    return getattr(app.state, "push", None)


def register_push(app: Any, current_user: Any) -> None:
    from .guest import guest_request

    async def owner(request: Request) -> str:
        if guest_request(request):
            raise HTTPException(403, "guest_forbidden")
        return await current_user(request)

    def status(user: str) -> dict[str, Any]:
        hub = _hub(app)
        devices = [{key: item[key] for key in ("deviceId", "platform", "createdAt", "lastOkAt", "lastError")}
                   for item in app.state.store.list_push_subscriptions(user)]
        return {"configured": bool(hub and hub.configured), "publicKey": hub.public_key if hub else None, "devices": devices}

    @app.get("/api/app/push", response_model=PushStatus)
    async def push_status(request: Request):
        """锁屏提醒：服务端是否已配置、VAPID 公钥，和本人已开锁屏提醒的设备（deviceId、设备名、开启时间、
        最近一次送达与失败原因）。"""
        return status(await owner(request))

    @app.put("/api/app/push/subscription", response_model=PushStatus)
    async def save_push_subscription(request: Request, body: PushSubscribe):
        """本设备开启锁屏提醒：保存浏览器给的推送订阅（必须由用户在页面上点按钮、浏览器授权后得到）。
        谷歌 FCM 的订阅（Chrome、安卓）国内送不到，422 push_endpoint_unsupported；其他非推送服务地址
        422 push_endpoint_invalid；服务端未配置推送 409 push_not_configured。"""
        user = await owner(request)
        hub = _hub(app)
        if hub is None or not hub.configured:
            raise HTTPException(409, {"code": "push_not_configured"})
        refusal = endpoint_refusal(body.subscription.endpoint)
        if refusal is not None:
            raise HTTPException(422, {"code": refusal})
        app.state.store.save_push_subscription(
            user_id=user, device_id=body.deviceId, endpoint=body.subscription.endpoint,
            p256dh=body.subscription.keys.p256dh, auth=body.subscription.keys.auth, platform=body.platform.strip())
        return status(user)

    @app.delete("/api/app/push/subscription", response_model=PushStatus)
    async def delete_push_subscription(request: Request,
                                       deviceId: str = Query(min_length=8, max_length=100, description="要移除的设备（本机或已开的另一台）")):
        """移除一台设备的锁屏提醒（本机关闭或在设备列表里移除另一台）。"""
        user = await owner(request)
        app.state.store.delete_push_subscriptions(user_id=user, device_id=deviceId)
        return status(user)

    @app.post("/api/app/push/test", response_model=PushTestResults)
    async def test_push(request: Request, body: PushTest):
        """发一条试试：给本人所有已开锁屏提醒的设备（或 deviceId 指定的一台）发一条“Sente 试一条”，
        返回每台的结果（sent / removed＝推送服务说订阅已失效、已删除 / failed）。不受“通知哪些事”和
        页面是否在前台影响。服务端未配置推送 409 push_not_configured。"""
        user = await owner(request)
        hub = _hub(app)
        if hub is None or not hub.configured:
            raise HTTPException(409, {"code": "push_not_configured"})
        return {"results": await hub.send_test(user, body.deviceId)}
