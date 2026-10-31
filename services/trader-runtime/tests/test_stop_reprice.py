"""图上拖线改价 (2026-10-24): the chart's dragged stop / take moves the user's own protection in place through
POST /api/ai-execution/stops/{id}/reprice — the same row, position and volume (no moment with no protection or
two), after the 移到成本 proof that it still guards its position, the `previous` level it was dragged from, and the
new stop's fresh-quote / tick / side checks. A strategy's protection is refused."""
from __future__ import annotations

import json
from pathlib import Path

from test_account_reminders import login
from test_breakeven import _setup
from test_order_protection import stop_row


async def _reprice(http, stop_id: str, level: str, previous: float, price: float):
    return await http.post(f"/api/ai-execution/stops/{stop_id}/reprice",
                           json={"level": level, "previous": previous, "price": price})


async def test_a_dragged_stop_or_take_moves_the_same_row_in_place(tmp_path: Path) -> None:
    app, _, _ = await _setup(tmp_path, "reprice-move.sqlite3")
    stop_id = stop_row(app)[0]["id"]
    http = await login(app)
    response = await _reprice(http, stop_id, "stop", 818.0, 819.0)
    assert response.status_code == 200, response.text
    assert response.json()["stopId"] == stop_id and response.json()["triggerPrice"] == 819.0
    rows = stop_row(app)
    assert len(rows) == 1 and (rows[0]["trigger_price"], rows[0]["take_profit_price"], rows[0]["status"]) == (
        "819.0", "830.0", "ARMED")
    assert (await _reprice(http, stop_id, "take", 830.0, 832.0)).status_code == 200
    assert (stop_row(app)[0]["trigger_price"], stop_row(app)[0]["take_profit_price"]) == ("819.0", "832.0")
    await http.aclose()
    app.state.store.close()


async def test_a_level_past_the_market_off_tick_or_dragged_from_a_stale_line_is_refused(tmp_path: Path) -> None:
    app, provider, _ = await _setup(tmp_path, "reprice-refuse.sqlite3")
    stop_id = stop_row(app)[0]["id"]
    http = await login(app)
    crossed = await _reprice(http, stop_id, "stop", 818.0, 824.0)  # long at 823: a stop above it fires at once
    assert crossed.status_code == 422 and crossed.json()["detail"]["code"] == "long_stop_must_be_below_market"
    off_tick = await _reprice(http, stop_id, "stop", 818.0, 818.013)
    assert off_tick.status_code == 422 and off_tick.json()["detail"]["code"] == "invalid_stop_price"
    stale_line = await _reprice(http, stop_id, "stop", 817.0, 819.0)  # the chart showed a level since changed
    assert stale_line.status_code == 422 and stale_line.json()["detail"]["code"] == "protection_target_changed"
    provider.age_seconds = 40.0
    stale_quote = await _reprice(http, stop_id, "stop", 818.0, 819.0)
    assert stale_quote.status_code in {409, 422}
    assert stop_row(app)[0]["trigger_price"] == "818.0"  # nothing moved
    await http.aclose()
    app.state.store.close()


async def test_a_strategys_protection_or_a_cancelled_stop_is_not_moved(tmp_path: Path) -> None:
    app, _, _ = await _setup(tmp_path, "reprice-strategy.sqlite3")
    stop_id = stop_row(app)[0]["id"]
    http = await login(app)
    app.state.store.connection.execute("UPDATE ai_stop_orders SET strategy_metadata_json = ? WHERE id = ?",
                                       (json.dumps({"strategyId": "s-1", "entryPrice": "820"}), stop_id))
    app.state.store.connection.commit()
    strategy = await _reprice(http, stop_id, "stop", 818.0, 819.0)
    assert strategy.status_code == 422 and strategy.json()["detail"]["code"] == "protection_not_manual"
    app.state.store.connection.execute("UPDATE ai_stop_orders SET strategy_metadata_json = NULL WHERE id = ?", (stop_id,))
    app.state.store.connection.commit()
    assert (await http.post(f"/api/ai-execution/stops/{stop_id}/cancel")).status_code == 200
    gone = await _reprice(http, stop_id, "stop", 818.0, 819.0)
    assert gone.status_code == 422 and gone.json()["detail"]["code"] == "protection_target_gone"
    assert stop_row(app)[0]["trigger_price"] == "818.0"
    await http.aclose()
    app.state.store.close()
