"""MT5 outages use real source results, fake time, and only the owner's existing push devices."""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from test_admin_servers import PASSWORD_HASH, SESSION_SECRET
from test_web_push import APPLE, KEYS, WINDOWS, configure
from trader_runtime.account_reminders import AccountReminderLoop
from trader_runtime.auth import OwnerIdentity
from trader_runtime.foreign_market import ForeignMarketService
from trader_runtime.last_good import LastGood
from trader_runtime.main import create_app
from trader_runtime.mt5_bridge_alerts import Mt5BridgeAlerts
from trader_runtime.web_push import PushMessage, admin_push_user_id


class Source:
    def __init__(self, name, *, failing=False, empty=False):
        self.name, self.failing, self.empty = name, failing, empty

    def quotes(self, products):
        if self.failing:
            raise RuntimeError("mt5_bridge_http_503")
        return {} if self.empty else {item.key: {"key": item.key, "last": 100, "source": self.name}
                                      for item in products if self.name != "mt5" or item.mt5}


@pytest.fixture
def rig(tmp_path):
    app = create_app(tmp_path / "alerts.sqlite3", agent_mode="fake", auth_mode="owner-password",
                     owner_username="charles", owner_password_hash=PASSWORD_HASH,
                     owner_session_secret=SESSION_SECRET, cors_origins={"https://qilinshuzhi.com"})
    sender = configure(app)
    owner = OwnerIdentity("charles").scoped_user_id
    friend = OwnerIdentity("friend").scoped_user_id
    for user, device, endpoint in ((owner, "owner-phone", APPLE), (owner, "owner-visible", WINDOWS),
                                   (friend, "friend-phone", APPLE + "-friend")):
        app.state.store.save_push_subscription(user_id=user, device_id=device, endpoint=endpoint,
                                               p256dh=KEYS["p256dh"], auth=KEYS["auth"], platform="iPhone")
    app.state.ui_visible_devices = lambda user: {"owner-visible"} if user == owner else set()
    clock = SimpleNamespace(tick=0.0, at=datetime(2026, 10, 23, 12, tzinfo=UTC))
    mt5, fallback = Source("mt5", failing=True), Source("sina")
    foreign = ForeignMarketService(LastGood(), [mt5, fallback], now=lambda: clock.at)

    async def board():
        return foreign._fetch()  # same source seam, no network or actual waiting in these tests

    foreign.board = board
    app.state.ctx.services.foreign_market = foreign
    alerts = Mt5BridgeAlerts(app, clock=lambda: clock.tick, now=lambda: clock.at)

    async def step(seconds=0):
        clock.tick += seconds
        clock.at += timedelta(seconds=seconds)
        await alerts.check()
        await app.state.push.drain()

    yield SimpleNamespace(app=app, sender=sender, owner=owner, friend=friend, clock=clock,
                          mt5=mt5, foreign=foreign, alerts=alerts, step=step)
    foreign.close()
    app.state.store.close()


async def test_ten_minutes_once_then_one_real_recovery_only_owner_device(rig):
    await rig.step()
    await rig.step(599)
    assert rig.sender.sent == []
    await rig.step(1)
    assert [(device, payload["kind"]) for device, payload in rig.sender.sent] == [("owner-phone", "admin_alert")]
    assert "10 分钟" in rig.sender.sent[0][1]["body"]
    assert rig.sender.sent[0][1]["url"] == "admin?section=servers"
    await rig.step(600)
    assert len(rig.sender.sent) == 1
    rig.mt5.failing = False
    await rig.step(60)
    assert len(rig.sender.sent) == 2
    assert rig.sender.sent[1][1]["title"] == "外盘行情已恢复"
    assert "中断 21 分钟" in rig.sender.sent[1][1]["body"]
    await rig.step(60)
    assert len(rig.sender.sent) == 2
    # No ordinary reminder, conversation, or subscriber fanout was used.
    assert rig.app.state.store.connection.execute("SELECT count(*) FROM reminders").fetchone()[0] == 0


async def test_success_before_threshold_resets_continuous_failure(rig):
    await rig.step()
    await rig.step(599)
    rig.mt5.failing = False
    await rig.step()
    rig.mt5.failing = True
    await rig.step()
    await rig.step(599)
    assert rig.sender.sent == []
    await rig.step(1)
    assert len(rig.sender.sent) == 1


@pytest.mark.parametrize("closed", ["2026-10-23T21:05:00+00:00", "2026-10-24T12:00:00+00:00",
                                    "2026-12-25T15:00:00+00:00"])
async def test_cme_daily_break_weekend_holiday_cannot_start_outage(rig, closed):
    rig.clock.at = datetime.fromisoformat(closed)
    await rig.step()
    await rig.step(600)
    assert rig.sender.sent == []
    assert rig.alerts.failed_since is None


async def test_close_resets_pending_but_does_not_report_false_recovery(rig):
    await rig.step()
    await rig.step(599)
    rig.clock.at = datetime(2026, 10, 23, 21, 5, tzinfo=UTC)
    await rig.step(1)
    assert rig.sender.sent == [] and rig.alerts.failed_since is None
    rig.clock.at = datetime(2026, 10, 26, 12, tzinfo=UTC)
    await rig.step()
    await rig.step(600)
    assert len(rig.sender.sent) == 1
    rig.clock.at = datetime(2026, 10, 26, 21, 5, tzinfo=UTC)
    await rig.step(60)
    assert len(rig.sender.sent) == 1
    rig.mt5.failing = False
    await rig.step(60)
    assert len(rig.sender.sent) == 2


async def test_restart_retimes_without_startup_recovery(rig):
    await rig.step()
    await rig.step(600)
    rig.alerts.reset()
    await rig.step(599)
    assert len(rig.sender.sent) == 1
    rig.mt5.failing = False
    await rig.step()
    assert len(rig.sender.sent) == 1


async def test_empty_mt5_quotes_fail_even_when_fallback_and_kept_board_succeed(rig):
    rig.mt5.failing, rig.mt5.empty = False, True
    rig.foreign.kept.put("board", {"rows": [{"key": "COMEX.GC", "last": 100, "source": "mt5"}]})
    await rig.step()
    await rig.step(600)
    assert rig.foreign.mt5_health[1] is False
    assert len(rig.sender.sent) == 1


async def test_stale_health_sample_cannot_claim_failure_or_recovery(rig):
    await rig.step()

    async def cached_board():
        return {"status": "ok"}

    rig.foreign.board = cached_board
    await rig.step(600)
    assert rig.sender.sent == [] and rig.alerts.failed_since is None


async def test_push_sender_rejects_non_owner_even_with_no_user_plan(rig):
    hub = rig.app.state.push
    hub.notify(rig.friend, PushMessage(kind="admin_alert", title="告警", body="断线", short="断线", tag="mt5"))
    await hub.drain()
    assert rig.sender.sent == []
    assert admin_push_user_id(rig.app) == rig.owner
    rig.app.state.auth = SimpleNamespace(username="charles")
    assert admin_push_user_id(rig.app) is None


async def test_existing_background_loop_checks_bridge_with_no_assembled_accounts(rig):
    loop = AccountReminderLoop(rig.app)
    loop.mt5_alerts = rig.alerts
    rig.app.state.accounts = SimpleNamespace(all=lambda: [])
    assert await loop.run_once() == 0
    rig.clock.tick += 600
    rig.clock.at += timedelta(seconds=600)
    assert await loop.run_once() == 0
    await rig.app.state.push.drain()
    assert len(rig.sender.sent) == 1


async def test_admin_alert_ignores_ordinary_switches_and_keeps_duration(rig):
    rig.app.state.store.update_preferences(rig.owner, {"notify": {"desktop": {"fills": False},
                                                                 "lockScreenAmounts": False}})
    await rig.step()
    await rig.step(600)
    assert "10 分钟" in rig.sender.sent[0][1]["body"]


async def test_unconfigured_source_or_non_owner_auth_never_polls(rig):
    calls = []

    async def board():
        calls.append(True)

    rig.foreign.board = board
    rig.foreign.sources = [Source("sina")]
    await rig.step(600)
    assert calls == [] and rig.sender.sent == []
    rig.foreign.sources = [rig.mt5]
    rig.app.state.auth = SimpleNamespace(username="charles")
    await rig.step(600)
    assert calls == [] and rig.sender.sent == []
