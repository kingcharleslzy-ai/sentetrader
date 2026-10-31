"""快讯游标增量 (2026-10-12): the compact flash feed — items by id + rev, changed rows once, a cursor that never
goes back — rebuilds to exactly the full feed on the reader's side (apps/client/src/flashCache.ts)."""
from __future__ import annotations

import copy
from typing import Any

from httpx import ASGITransport, AsyncClient

from test_research_live import TOKEN, service
from trader_runtime.main import create_app
from trader_runtime.research_live import FLASH_ROW_FIELDS


def expand(compact: dict[str, Any], held: dict[str, dict[str, Any]]) -> tuple[dict[str, Any], set[str]]:
    """The reader's side: keep the sent rows, rebuild every reduced item from the row with its exact rev."""
    for row in compact["rows"]:
        held[row["id"]] = row
    missing: set[str] = set()

    def rebuild(item: dict[str, Any]) -> dict[str, Any]:
        if "rev" not in item:
            return item
        row = held.get(item["id"])
        if row is None or row["rev"] != item["rev"]:
            missing.add(item["id"])
            return item
        own = {key: value for key, value in item.items() if key != "rev"}
        return {**{key: row[key] for key in FLASH_ROW_FIELDS}, **own}

    full = {key: value for key, value in compact.items() if key not in ("compact", "cursor", "rows")}
    full["items"] = [rebuild(item) for item in compact["items"]]
    if compact.get("screen"):
        dropped = compact["screen"]["dropped"]
        full["screen"] = {**compact["screen"], "dropped": {**dropped, "items": [rebuild(item) for item in dropped["items"]]}}
    if compact.get("events") is not None:
        events = []
        for event in compact["events"]:
            rebuilt = {**event, "items": [rebuild(item) for item in event["items"]]}
            if "headline" in rebuilt:
                named = rebuilt.pop("headline")
                headline = next(item for item in rebuilt["items"] if item["id"] == named)
                rebuilt.update({key: headline[key] for key in ("title", "text", "origTitle")})
            events.append(rebuilt)
        full["events"] = events
    return full, missing


def without_products(event_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # An event's report carries every row field but `products`; the rebuilt one carries that too.
    return [{key: value for key, value in item.items() if key != "products"} for item in event_items]


def test_compact_feed_rebuilds_exactly_and_sends_only_changed_rows(tmp_path):
    svc = service(tmp_path)
    svc.poll_flash_once()
    feed = svc.flash_feed()
    held: dict[str, dict[str, Any]] = {}
    first = svc.compact_flash(feed, "0")
    assert first["compact"] is True and {row["id"] for row in first["rows"]} == {item["id"] for item in feed["items"]}
    assert all(set(item) == {"id", "rev"} for item in first["items"])  # the raw feed's items are rows and nothing else
    rebuilt, missing = expand(first, held)
    assert not missing and rebuilt == feed
    cursor = first["cursor"]
    # Nothing changed: no row content travels, the cursor stays.
    again = svc.compact_flash(svc.flash_feed(), cursor)
    assert again["rows"] == [] and again["cursor"] == cursor and expand(again, held)[0] == feed

    # A translation changes a row in place (the translator's own UPDATE): only that row travels, with a new rev.
    target = feed["items"][0]
    with svc.lock, svc.connection:
        svc.connection.execute("UPDATE news_flash SET title = ?, orig_title = title WHERE id = ?", ("译文标题", target["id"]))
    changed = svc.compact_flash(svc.flash_feed(), cursor)
    assert [row["id"] for row in changed["rows"]] == [target["id"]] and changed["rows"][0]["title"] == "译文标题"
    assert changed["cursor"] != cursor
    rebuilt, missing = expand(changed, held)
    assert not missing and rebuilt["items"][0]["title"] == "译文标题" and rebuilt["items"][0]["origTitle"] == target["title"]

    # A reader holding an older copy of a row (changed while it read another view) sees a rev it lacks — never
    # the stale content — and gets the row back through include.
    stale = copy.deepcopy(held)
    stale[target["id"]] = {**first["rows"][0], **{key: target[key] for key in FLASH_ROW_FIELDS}, "id": target["id"],
                           "rev": next(row["rev"] for row in first["rows"] if row["id"] == target["id"])}
    later = svc.compact_flash(svc.flash_feed(), changed["cursor"])
    _, missing = expand(later, stale)
    assert missing == {target["id"]}
    repaired = svc.compact_flash(svc.flash_feed(), changed["cursor"], frozenset(missing))
    rebuilt, missing = expand(repaired, stale)
    assert not missing and rebuilt["items"][0]["title"] == "译文标题"

    # Another epoch (a rebuilt table) or a cursor that is not ours: every row again.
    assert len(svc.compact_flash(svc.flash_feed(), "someone-else:999")["rows"]) == len(feed["items"])
    assert len(svc.compact_flash(svc.flash_feed(), "garbage")["rows"]) == len(feed["items"])


def test_merged_source_and_insert_take_new_numbers_and_a_prune_never_lowers_the_counter(tmp_path):
    svc = service(tmp_path)
    svc.poll_flash_once()
    cursor = svc.compact_flash(svc.flash_feed(), "0")["cursor"]
    epoch, number = cursor.split(":")
    oldest = svc.flash_feed()["items"][-1]
    with svc.lock, svc.connection:
        # The merge's upsert of a row that gained a source.
        svc.connection.execute("UPDATE news_flash SET sources_json = ? WHERE id = ?", ('["新浪", "金十"]', oldest["id"]))
        rev = svc.connection.execute("SELECT rev FROM news_flash WHERE id = ?", (oldest["id"],)).fetchone()[0]
        assert rev == int(number) + 1
        # The newest-numbered row goes (a prune): the next write still takes a higher number.
        svc.connection.execute("DELETE FROM news_flash WHERE id = ?", (oldest["id"],))
        svc.connection.execute(
            "INSERT INTO news_flash (id, source, sources_json, published_at, title, text, link, products_json, tags_json)"
            " VALUES ('fresh', '新浪', '[\"新浪\"]', '2026-10-03T13:00:00+08:00', '新', '新到正文', NULL, '[]', '[]')")
        assert svc.connection.execute("SELECT rev FROM news_flash WHERE id = 'fresh'").fetchone()[0] == rev + 1
    delta = svc.compact_flash(svc.flash_feed(), cursor)
    assert [row["id"] for row in delta["rows"]] == ["fresh"] and delta["cursor"] == f"{epoch}:{rev + 1}"


async def test_flash_route_answers_compact_with_since_and_full_without(tmp_path, monkeypatch):
    """The screened view (fake model): items, dropped and events all reduce and rebuild to the full feed."""
    from trader_runtime.testing.fake_research_live import ENV_FLAG
    monkeypatch.setenv(ENV_FLAG, "1")
    app = create_app(tmp_path / "runtime.sqlite3", runtime_state_root=tmp_path / "state", local_token=TOKEN,
                     agent_mode="fake", data_mode="fake")
    try:
        svc = app.state.research_live
        svc.poll_flash_once()
        svc.flash_feed(product="SHFE.au")
        await svc.screener.burst()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                               headers={"Origin": "http://127.0.0.1:5173"}) as client:
            await client.post("/api/auth/local", headers={"Authorization": f"Bearer {TOKEN}"})
            params = {"hours": 24, "product": "SHFE.au", "limit": 50}
            full = (await client.get("/api/research/flash", params=params)).json()
            assert "compact" not in full and full["events"]
            compact = (await client.get("/api/research/flash", params={**params, "since": "0"})).json()
            held: dict[str, dict[str, Any]] = {}
            rebuilt, missing = expand(compact, held)
            assert not missing
            for key in ("items", "screen", "layers", "brief", "tonight", "status"):
                assert rebuilt.get(key) == full.get(key), key
            assert [{**event, "items": without_products(event["items"])} for event in rebuilt["events"]] == full["events"]
            # An event names its headline report instead of repeating its title and text.
            assert all("headline" in event and "title" not in event and "text" not in event for event in compact["events"])
            # Unchanged since: no row content at all.
            second = (await client.get("/api/research/flash", params={**params, "since": compact["cursor"]})).json()
            assert second["rows"] == [] and not expand(second, held)[1]
            ids = ",".join(item["id"] for item in full["items"][:2])
            named = (await client.get("/api/research/flash", params={**params, "since": compact["cursor"], "include": ids})).json()
            assert {row["id"] for row in named["rows"]} == set(ids.split(","))
    finally:
        app.state.research.close()
        app.state.store.close()
