"""Protects the lock-screen push surface: the unconfigured state, subscribe / list / test / remove (404/410 drops a dead device), FCM refusal, guests refused, the agent catalog without the subscribe route, delivery skipping the visible device, switched-off kinds and other people's accounts, and a device pushing only to whoever is signed in on it (sign-out and another login retire that device's old subscription, nothing else)."""
from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from httpx import ASGITransport, AsyncClient

from test_account_reminders import login
from test_ai_annotations import observation, ui_state
from test_cloud_accounts import ORIGIN, PASSWORD, PASSWORD_HASH, SESSION_SECRET
from test_simulation_orders import USER, Gateway, application
from trader_runtime.data_providers import FakeReadOnlyDataProvider
from trader_runtime.main import create_app
from trader_runtime.web_push import PushHub, emergency_message, fill_message

APPLE = "https://web.push.apple.com/QGxV1-device-a"
WINDOWS = "https://wns2-pn1p.notify.windows.com/w/?token=device-b"
KEYS = {"p256dh": "B" + "x" * 86, "auth": "a" * 22}


class Sender:
    """The blocking send stand-in: records each (device, payload) and answers a status per endpoint."""

    def __init__(self):
        self.sent: list[tuple[str, dict]] = []
        self.status: dict[str, int] = {}

    def __call__(self, subscription, payload):
        self.sent.append((subscription["deviceId"], payload))
        return self.status.get(subscription["endpoint"], 201)


def configure(app) -> Sender:
    sender = Sender()
    hub = PushHub(app, None, sender=sender)
    app.state.push = hub
    app.state.store.push_hub = hub
    return sender


def body(device: str, endpoint: str, platform: str = "iPhone · 桌面图标") -> dict:
    return {"deviceId": device, "platform": platform, "subscription": {"endpoint": endpoint, "keys": KEYS, "expirationTime": None}}


async def test_without_a_key_push_is_not_configured_and_nothing_else_changes(tmp_path: Path):
    app = application(tmp_path / "push-off.sqlite3", Gateway())
    http = await login(app)
    assert (await http.get("/api/app/push")).json() == {"configured": False, "publicKey": None, "devices": []}
    refused = await http.put("/api/app/push/subscription", json=body("device-a-0001", APPLE))
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "push_not_configured"
    assert (await http.post("/api/app/push/test", json={})).status_code == 409
    assert (await http.get("/api/preferences")).status_code == 200
    await http.aclose()
    app.state.store.close()


async def test_a_device_subscribes_tests_and_is_removed_and_a_dead_one_drops_itself(tmp_path: Path):
    app = application(tmp_path / "push-routes.sqlite3", Gateway())
    sender = configure(app)
    http = await login(app)
    fcm = await http.put("/api/app/push/subscription", json=body("device-a-0001", "https://fcm.googleapis.com/fcm/send/x"))
    assert fcm.status_code == 422 and fcm.json()["detail"]["code"] == "push_endpoint_unsupported"
    internal = await http.put("/api/app/push/subscription", json=body("device-a-0001", "https://10.0.0.5/push"))
    assert internal.status_code == 422 and internal.json()["detail"]["code"] == "push_endpoint_invalid"
    assert (await http.put("/api/app/push/subscription", json=body("device-a-0001", APPLE + "-old"))).status_code == 200
    # Subscribing again from the same device replaces its row instead of adding a second one.
    assert (await http.put("/api/app/push/subscription", json=body("device-a-0001", APPLE))).status_code == 200
    state = (await http.put("/api/app/push/subscription", json=body("device-b-0001", WINDOWS, "Windows · Edge"))).json()
    assert state["configured"] is True
    assert [(item["deviceId"], item["platform"]) for item in state["devices"]] == [
        ("device-a-0001", "iPhone · 桌面图标"), ("device-b-0001", "Windows · Edge")]
    assert [item["endpoint"] for item in app.state.store.list_push_subscriptions(USER)] == [APPLE, WINDOWS]

    results = (await http.post("/api/app/push/test", json={})).json()["results"]
    assert sorted(item["status"] for item in results) == ["sent", "sent"]
    assert {device for device, _ in sender.sent} == {"device-a-0001", "device-b-0001"}
    assert sender.sent[0][1]["title"] == "Sente 试一条"
    assert all(item["lastOkAt"] for item in (await http.get("/api/app/push")).json()["devices"])

    # The push service says device b is gone (410): it is removed; a 500 is only recorded, not retried.
    sender.status = {WINDOWS: 410, APPLE: 500}
    results = {item["deviceId"]: item for item in (await http.post("/api/app/push/test", json={})).json()["results"]}
    assert results["device-b-0001"]["status"] == "removed" and results["device-a-0001"]["status"] == "failed"
    devices = (await http.get("/api/app/push")).json()["devices"]
    assert [(item["deviceId"], item["lastError"]) for item in devices] == [("device-a-0001", "HTTP 500")]
    before = len(sender.sent)
    sender.status = {}
    one = (await http.post("/api/app/push/test", json={"deviceId": "device-a-0001"})).json()["results"]
    assert [item["status"] for item in one] == ["sent"] and len(sender.sent) == before + 1

    assert (await http.delete("/api/app/push/subscription", params={"deviceId": "device-a-0001"})).json()["devices"] == []
    # A visitor never reaches the push surface.
    guest = await http.get("/api/app/push", headers={"Authorization": "Bearer guest.nope"})
    assert guest.status_code == 403
    await http.aclose()
    app.state.store.close()


async def test_the_agent_catalog_can_read_and_test_but_never_subscribe(tmp_path: Path):
    app = application(tmp_path / "push-catalog.sqlite3", Gateway())
    http = await login(app)
    operations = {item["operation"] for item in (await http.get("/api/agent/capabilities")).json()["operations"]}
    assert {"GET /api/app/push", "POST /api/app/push/test", "DELETE /api/app/push/subscription"} <= operations
    assert "PUT /api/app/push/subscription" not in operations
    await http.aclose()
    app.state.store.close()


async def pulse(http, window: str, device: str, *, visible: bool):
    response = await http.post("/api/ui/pulse", json={
        "windowId": window, "revision": 0, "state": ui_state(), "observation": observation(),
        "device": {"deviceId": device, "platform": "web", "version": "1.7.2"}, "visible": visible})
    assert response.status_code == 200, response.text


async def test_delivery_skips_the_device_in_front_switched_off_kinds_and_other_accounts(tmp_path: Path):
    app = application(tmp_path / "push-delivery.sqlite3", Gateway())
    sender = configure(app)
    hub = app.state.push
    http = await login(app)
    await http.put("/api/app/push/subscription", json=body("phone-0001", APPLE))
    await http.put("/api/app/push/subscription", json=body("mac-0001", WINDOWS, "Mac · Safari"))
    account = app.state.store.default_trading_account_id
    assert app.state.store.account_owner_ids(account) == [USER]
    fill = fill_message(name="螺纹2610", direction="SELL", offset="CLOSE", volume=2, price=3188, order_id="o1")

    window = str(uuid4())
    await pulse(http, window, "phone-0001", visible=True)
    hub.notify(USER, fill, account_id=account)
    await hub.drain()
    assert [device for device, _ in sender.sent] == ["mac-0001"]        # the phone is showing Sente
    await pulse(http, window, "phone-0001", visible=False)                # the page went to the background
    sender.sent.clear()
    hub.notify(USER, fill, account_id=account)
    await hub.drain()
    assert sorted(device for device, _ in sender.sent) == ["mac-0001", "phone-0001"]

    # 成交与撤单 off: no fill push; 急停 still goes; 锁屏不显示金额: the figure-free line.
    assert (await http.put("/api/preferences", json={"notify": {"desktop": {"fills": False, "emergencyStop": False},
                                                                "lockScreenAmounts": False}})).status_code == 200
    sender.sent.clear()
    hub.notify(USER, fill, account_id=account)
    hub.notify(USER, emergency_message(), account_id=account)
    await hub.drain()
    assert {payload["kind"] for _, payload in sender.sent} == {"emergency"} and len(sender.sent) == 2
    assert sender.sent[0][1]["body"] == "AI 已全局急停"
    # Someone else's account: never pushed to this user, even when called with their id directly.
    sender.sent.clear()
    hub.notify(USER, emergency_message(), account_id="someone-elses-account")
    await hub.drain()
    assert sender.sent == []
    await http.aclose()
    app.state.store.close()


def rows(app) -> set[tuple[str, str]]:
    return {(row["user_id"], row["device_id"]) for row in
            app.state.store.connection.execute("SELECT user_id, device_id FROM push_subscriptions").fetchall()}


async def test_a_device_pushes_only_to_whoever_is_signed_in_on_it(tmp_path: Path):
    """#521: sign-out with the device id drops that device's row; another user's first heartbeat on a
    device retires the previous user's row there; the same user's heartbeat and other devices stay."""
    app = create_app(tmp_path / "push-owner.sqlite3", agent_mode="fake", auth_mode="owner-password",
                     owner_username="charles", owner_password_hash=PASSWORD_HASH, owner_session_secret=SESSION_SECRET,
                     cors_origins={ORIGIN}, data_mode="tqkq-sidecar", data_provider=FakeReadOnlyDataProvider(),
                     registration_enabled=True)
    configure(app)
    transport = ASGITransport(app=app)

    def client() -> AsyncClient:
        return AsyncClient(transport=transport, base_url="http://test", headers={"Origin": ORIGIN})

    owner = client()
    assert (await owner.post("/api/auth/owner", json={"username": "charles", "password": PASSWORD})).status_code == 200
    code = (await owner.post("/api/auth/owner/invite-codes")).json()["inviteCode"]
    friend = client()
    registered = await friend.post("/api/auth/owner/register", json={
        "username": "friend", "password": "a-solid-password-123", "inviteCode": code})
    assert registered.status_code == 200
    a = str((await owner.get("/api/auth/owner/status")).json()["userId"])
    b = str(registered.json()["userId"])
    assert a != b

    # A turns push on for the phone and the Mac; a plain heartbeat from A's own phone keeps both.
    assert (await owner.put("/api/app/push/subscription", json=body("phone-0001", APPLE))).status_code == 200
    assert (await owner.put("/api/app/push/subscription", json=body("mac-0001", WINDOWS, "Mac · Safari"))).status_code == 200
    await pulse(owner, str(uuid4()), "phone-0001", visible=False)
    assert rows(app) == {(a, "phone-0001"), (a, "mac-0001")}

    # B signs in on A's phone (a new window there): A's phone row goes, A's Mac stays.
    await pulse(friend, str(uuid4()), "phone-0001", visible=True)
    assert rows(app) == {(a, "mac-0001")}
    # B's own subscription on that phone is not touched by B's later heartbeats.
    assert (await friend.put("/api/app/push/subscription", json=body("phone-0001", APPLE + "-b"))).status_code == 200
    await pulse(friend, str(uuid4()), "phone-0001", visible=False)
    assert rows(app) == {(a, "mac-0001"), (b, "phone-0001")}

    # B signs out on the phone, naming it: only B's phone row goes. An older client's bare logout keeps rows.
    assert (await friend.post("/api/auth/owner/logout", json={"deviceId": "phone-0001"})).status_code == 200
    assert rows(app) == {(a, "mac-0001")}
    assert (await owner.post("/api/auth/owner/logout")).status_code == 200
    assert rows(app) == {(a, "mac-0001")}
    # A signed-out (or never signed-in) caller naming a device removes nothing.
    assert (await owner.post("/api/auth/owner/logout", json={"deviceId": "mac-0001"})).status_code == 200
    assert rows(app) == {(a, "mac-0001")}
    await owner.aclose()
    await friend.aclose()
    app.state.store.close()


def test_the_real_vapid_signer_accepts_the_subject():
    """The production signer, not the stand-in: py_vapid refuses an https subject with a path, which
    made every real send fail with VapidException on 10-06 while the faked sender stayed green."""
    from py_vapid import Vapid

    from trader_runtime.web_push import SUBJECT

    vapid = Vapid()
    vapid.generate_keys()
    headers = vapid.sign({"sub": SUBJECT, "aud": "https://web.push.apple.com"})
    assert headers["Authorization"].startswith("vapid t=")
