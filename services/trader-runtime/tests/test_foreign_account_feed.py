"""The USD ledger uses the account stream and the old browser's shared read budget."""
from unittest.mock import AsyncMock, Mock

import pytest

from test_foreign_paper import MAIN, foreign_app
from test_paper_accounts import close_paper_app, open_session, register_user, wait_paper_ready
from trader_runtime.contracts import FeedAccount
from trader_runtime.runtime_feed import FeedReadError


@pytest.mark.parametrize("busy", [False, True])
async def test_usd_account_frame_shares_reads_touches_and_rechecks_grants(tmp_path, monkeypatch, busy):
    app, engine = foreign_app(tmp_path, monkeypatch, "foreign-feed.sqlite3")
    friend = None
    try:
        friend, user = await register_user(app)
        store = app.state.store
        paper_id = str(store.find_user_paper_account(user_id=user)["id"])
        usd_id = paper_id.replace("paper:", "paperusd:")
        await friend.get("/api/account/summary")
        await wait_paper_ready(app, paper_id)
        await friend.get("/api/account/summary", headers={"X-AI-Trader-Account": usd_id})
        await wait_paper_ready(app, usd_id)
        usd = app.state.accounts.get(usd_id)
        if busy:
            engine.book(usd_id)["positions"] = [{"symbol": MAIN, "direction": "long", "volume": 1}]
            usd.provider._poll_once(usd.provider._fetch)
        sessions = {await open_session(friend), await open_session(friend)}
        feed = app.state.runtime_feed
        broker = app.state.broker
        published = []

        async def publish(session_id, *, name, payload, **_kwargs):
            published.append((session_id, name, payload))

        monkeypatch.setattr(broker, "subscribed_session_ids", lambda: sessions)
        monkeypatch.setattr(broker, "publish_transient", publish)
        clock = [100.0]
        monkeypatch.setattr("trader_runtime.runtime_feed.monotonic", lambda: clock[0])
        monkeypatch.setattr("trader_runtime.wiring.feed.monotonic", lambda: clock[0])
        await usd.account_ledger.stop()
        reads = Mock(wraps=usd.provider.account_summary)
        monkeypatch.setattr(usd.provider, "account_summary", reads)
        recovery = AsyncMock(wraps=usd.simulation_orders.recover_once)
        monkeypatch.setattr(usd.simulation_orders, "recover_once", recovery)
        for session in sessions:
            feed.set_focus(session, symbol=None, duration=300, feed_touch=True)
        await feed.tick()
        frames = [payload for _, name, payload in published if name == "feed.account"]
        assert len(frames) == 2
        assert frames[0] is frames[1]  # the tick's existing per-account/user frame cache
        half = FeedAccount.model_validate(frames[0]).foreign
        assert half is not None and half.accountId == usd_id
        assert half.orders == usd.simulation_orders.list(user_id=user)
        assert half.positionsStatus["snapshotAvailable"] is True
        if busy:
            assert half.positions == [{**engine.book(usd_id)["positions"][0], "accountId": usd_id,
                                       "currency": "USD", "instrumentName": "COMEX黄金2612"}]
        else:
            assert half.positions == []
        reads.assert_called_once()
        recovery.assert_awaited_once_with(user_id=user, attempts=1, interval=0)

        clock[0] += 5
        await feed.tick()
        assert sum(name == "feed.account" for _, name, _ in published) == 2
        assert sum(name == "feed.touch" and payload["name"] == "feed.account"
                   for _, name, payload in published) == 2
        assert reads.call_count == (2 if busy else 1)
        clock[0] += 10
        await feed.tick()
        assert reads.call_count == (3 if busy else 2)

        # An already cached USD frame cannot outlive its reader's grant.
        monkeypatch.setattr("trader_runtime.wiring.feed.user_may_use_account",
                            lambda _store, *, account_id, **_kwargs: account_id != usd_id)
        with pytest.raises(FeedReadError, match="account_not_granted"):
            await feed._account_overview(paper_id, user, {})
        # A session directly on the USD ledger has no nested foreign half.
        monkeypatch.setattr("trader_runtime.wiring.feed.user_may_use_account", lambda *_args, **_kwargs: True)
        own = await feed._account_overview(usd_id, user, {})
        assert "foreign" not in own
        clock[0] += 20
        failed_read = Mock(side_effect=RuntimeError("foreign_account_unavailable"))
        monkeypatch.setattr(usd.provider, "account_summary", failed_read)
        for _ in range(2):
            with pytest.raises(FeedReadError, match="foreign_account_unavailable"):
                await feed._account_overview(paper_id, user, {})
        failed_read.assert_called_once()  # a failed read also keeps the polling budget
    finally:
        if friend is not None:
            await friend.aclose()
        await close_paper_app(app)
