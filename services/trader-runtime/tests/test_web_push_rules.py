"""Protects who a lock-screen push may go to and what it may say: FCM and foreign endpoints refused, switches and 急停, figure-free text, the visible-device rule, owners only, and one push per new fill or cancel."""
from __future__ import annotations

from trader_runtime.user_preferences import UserPreferences
from trader_runtime.web_push import (
    activity_events,
    cancel_message,
    endpoint_refusal,
    fill_message,
    kind_enabled,
    morning_message,
    owns_account,
    push_payload,
    push_targets,
    reminder_message,
)

NS = 1_000_000_000
NOW = 1_791_000_000 * NS


def prefs(**notify):
    document = UserPreferences().model_dump()
    for key, value in notify.items():
        if key in document["notify"]["desktop"]:
            document["notify"]["desktop"][key] = value
        else:
            document["notify"][key] = value
    return document


def test_only_the_reachable_push_services_are_accepted():
    assert endpoint_refusal("https://web.push.apple.com/QGxV1-abc") is None
    assert endpoint_refusal("https://wns2-pn1p.notify.windows.com/w/?token=abc") is None
    assert endpoint_refusal("https://updates.push.services.mozilla.com/wpush/v2/abc") is None
    # Chrome / Android: Google's FCM is unreachable from the server — the client shows why instead.
    assert endpoint_refusal("https://fcm.googleapis.com/fcm/send/abc") == "push_endpoint_unsupported"
    assert endpoint_refusal("https://android.googleapis.com/gcm/send/abc") == "push_endpoint_unsupported"
    # The server POSTs to this URL: anything else (internal hosts, http, odd ports, userinfo) is refused.
    for bad in ("http://web.push.apple.com/x", "https://127.0.0.1/x", "https://evil.example/push",
                "https://web.push.apple.com:8443/x", "https://user@web.push.apple.com/x", "not a url"):
        assert endpoint_refusal(bad) == "push_endpoint_invalid", bad


def test_switches_gate_each_kind_and_emergency_always_goes():
    defaults = prefs()
    assert kind_enabled(defaults, "fill") and kind_enabled(defaults, "morning")
    assert not kind_enabled(defaults, "market_close")          # 收盘前提醒 stays off by default
    assert not kind_enabled(defaults, "movement")              # 盘中异动 too, since 2026-10-14 (owner: too frequent)
    off = prefs(fills=False, movement=False, priceAlerts=False, strategySignalAlerts=False, riskReminder=False,
                morningDesk=False, cancels=False, emergencyStop=False)
    for kind in ("fill", "cancel", "movement", "price_alert", "strategy", "risk", "morning"):
        assert not kind_enabled(off, kind), kind
    # 急停 has no switch any more (the legacy key is ignored) and 发一条试试 ignores them all.
    assert kind_enabled(off, "emergency") and kind_enabled(off, "test")
    assert not kind_enabled(defaults, "unknown-kind")
    # A document without the key (an older one) falls back to the default; a stored value is the user's.
    old = prefs(movement=True)
    assert kind_enabled(old, "movement")
    del old["notify"]["movement"]
    assert not kind_enabled(old, "movement")


def test_hiding_amounts_leaves_no_figure_on_the_lock_screen():
    fill = fill_message(name="螺纹2610", direction="SELL", offset="CLOSE", volume=2, price=3188, order_id="o1")
    shown = push_payload(fill, amounts=True)
    assert shown["title"] == "成交 · 螺纹2610 卖平 2 手" and "3,188" in shown["body"]
    hidden = push_payload(fill, amounts=False)
    assert hidden == {"title": "成交 · 螺纹2610", "body": "螺纹2610有新成交", "tag": "fill-o1",
                      "url": "?open=mobilePage:positions", "kind": "fill"}
    reminder = reminder_message(kind="risk_ratio", text="风险度提醒：现在风险度 83%（保证金占用 41.5 万）", card_id="reminder-x")
    assert reminder.body.startswith("现在风险度 83%") and reminder.url == "?open=card:reminder-x"
    # Two accounts' reminders keep separate lock-screen notifications (the tag carries the account).
    assert {reminder_message(kind="market_close_day", text="收盘提醒：x", card_id="c", account_id=account).tag
            for account in ("a1", "a2")} == {"reminder-market_close_day-a1", "reminder-market_close_day-a2"}
    # 今日作战表 (第 5 批): names only, opening the 调研 panel's first page.
    morning = morning_message(lines=["螺纹钢", "铁矿石"])
    assert morning.title == "今日作战表" and morning.url == "?open=research:sheet" and "偏" not in morning.body
    for message in (fill, reminder, morning,
                    cancel_message(name="甲醇2701", direction="BUY", offset="OPEN", volume=3, reason="", order_id="o2")):
        private = push_payload(message, amounts=False)
        words = (private["title"] + private["body"]).replace("螺纹2610", "").replace("甲醇2701", "")  # the contract's own name
        assert not any(char.isdigit() for char in words), private


def test_a_device_with_its_page_in_front_gets_no_push():
    subscriptions = [{"deviceId": "phone-1"}, {"deviceId": "mac-1"}]
    assert push_targets(subscriptions, {"phone-1"}) == [{"deviceId": "mac-1"}]
    assert push_targets(subscriptions, set()) == subscriptions


class Grants:
    idp_fallback = False
    default_trading_account_id = "default"

    def __init__(self, roles):
        self.roles = roles

    def account_role(self, *, account_id, user_id):
        return self.roles.get((account_id, user_id))


def test_only_the_accounts_own_owner_is_pushed():
    store = Grants({("acct", "owner-user"): "owner", ("acct", "viewer-user"): "viewer"})
    assert owns_account(store, "acct", "owner-user")
    assert not owns_account(store, "acct", "viewer-user") and not owns_account(store, "acct", "stranger")
    # A single-tenant runtime without grants: its one user on the default account only.
    store.idp_fallback = True
    assert owns_account(store, "default", "anyone") and not owns_account(store, "acct", "stranger")


def test_new_fills_and_cancels_are_found_once_and_the_first_read_only_seeds():
    old_fill = {"tradeId": "t0", "clientOrderId": "o0", "symbol": "SHFE.rb2610", "direction": "BUY", "offset": "OPEN",
                "price": 3100, "volume": 1, "tradeDateTime": NOW - 60 * NS}
    seen, fills, cancels = activity_events(None, {"trades": [old_fill], "orders": []}, now_ns=NOW)
    assert fills == [] and cancels == []
    # Two fills of one order in one read are one event; a fill seen 20 minutes late is history.
    partials = [{**old_fill, "tradeId": f"t{i}", "clientOrderId": "o1", "volume": 1, "price": 3101 + i,
                 "tradeDateTime": NOW - 5 * NS} for i in (1, 2)]
    late = {**old_fill, "tradeId": "t9", "clientOrderId": "o9", "tradeDateTime": NOW - 1200 * NS}
    cancelled = {"clientOrderId": "o5", "symbol": "SHFE.rb2610", "direction": "SELL", "offset": "CLOSE",
                 "status": "FINISHED", "volumeLeft": 2, "lastMsg": "已撤单"}
    activity = {"trades": [old_fill, *partials, late],
                "orders": [cancelled, {**cancelled, "clientOrderId": "o6", "volumeLeft": 0},
                           {**cancelled, "clientOrderId": "o7", "status": "ALIVE"}]}
    seen, fills, cancels = activity_events(seen, activity, now_ns=NOW)
    assert [(item["orderId"], item["volume"], item["price"]) for item in fills] == [("o1", 2, 3103)]
    assert [(item["orderId"], item["volume"]) for item in cancels] == [("o5", 2)]
    # The next read of the same activity finds nothing new; a read missing them does not forget them.
    assert activity_events(seen, activity, now_ns=NOW)[1:] == ([], [])
    seen, _, _ = activity_events(seen, {"trades": [], "orders": []}, now_ns=NOW)
    assert activity_events(seen, activity, now_ns=NOW)[1:] == ([], [])


def test_the_first_read_after_a_restart_still_tells_what_happened_since_the_claims_began():
    """#932: with `since_ns` (when this database's reminder claims began) the first read after a start
    tells a fill made after it and a cancel of an order placed after it — the claims keep a repeat quiet;
    anything older is only seeded, as before."""
    since = NOW - 120 * NS
    before = {"tradeId": "t0", "clientOrderId": "o0", "symbol": "SHFE.rb2610", "direction": "BUY",
              "offset": "OPEN", "price": 3100, "volume": 1, "tradeDateTime": since - NS}
    after = {**before, "tradeId": "t1", "clientOrderId": "o1", "tradeDateTime": since + 30 * NS}
    cancelled = {"clientOrderId": "o5", "symbol": "SHFE.rb2610", "direction": "SELL", "offset": "CLOSE",
                 "status": "FINISHED", "volumeLeft": 1, "lastMsg": "", "insertDateTime": since + 10 * NS}
    old_cancel = {**cancelled, "clientOrderId": "o4", "insertDateTime": since - 10 * NS}
    activity = {"trades": [before, after], "orders": [cancelled, old_cancel]}
    seen, fills, cancels = activity_events(None, activity, now_ns=NOW, since_ns=since)
    assert [item["orderId"] for item in fills] == ["o1"] and fills[0]["tradeIds"] == ["t1"]
    assert [item["orderId"] for item in cancels] == ["o5"]
    assert activity_events(seen, activity, now_ns=NOW)[1:] == ([], [])
