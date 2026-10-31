"""Account preferences (设置重做第 2 步, Runtime side): one document per user, partial PUT, the
capability catalog, the agent tools, the UI command/snapshot pair, and every Runtime consumer."""
import json
import sqlite3
import time
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from test_ai_annotations import HEADERS, ui_state, with_tools
from test_voice_realtime import WINDOW, _voice_app
from trader_runtime.main import create_app
from trader_runtime.store import RuntimeStore
from trader_runtime.ui_control import UiChanges, changes_match
from trader_runtime.user_preferences import UserPreferences, merge_preferences, preference_instruction
from trader_runtime.voice_realtime import SPEECH_SPEEDS, CallState


def _app(tmp_path, name="prefs.sqlite3"):
    return create_app(tmp_path / name, runtime_state_root=tmp_path, agent_mode="fake", local_token="annotation-test-token")


async def test_put_is_partial_and_returns_the_whole_document_with_a_version(tmp_path):
    app = _app(tmp_path)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://runtime", headers=HEADERS) as client:
            initial = (await client.get("/api/preferences")).json()
            assert initial["version"] == 0 and initial["preferences"] == UserPreferences().model_dump()
            assert initial["preferences"]["trade"]["aiAutonomy"] is True  # AI 自主执行 defaults to on
            changed = await client.put("/api/preferences", json={"voice": {"speed": "fast"}, "trade": {"defaultVolume": 2}})
            assert changed.status_code == 200, changed.text
            document = changed.json()
            assert document["version"] == 1
            assert document["preferences"]["voice"]["speed"] == "fast" and document["preferences"]["trade"]["defaultVolume"] == 2
            # Untouched keys keep their values; a second partial write only bumps the version.
            assert document["preferences"]["voice"]["confirmWindowSeconds"] == 15 and document["preferences"]["chat"]["replyStyle"] == "concise"
            again = (await client.put("/api/preferences", json={"notify": {"desktop": {"briefs": True}}})).json()
            assert again["version"] == 2 and again["preferences"]["voice"]["speed"] == "fast" and again["preferences"]["notify"]["desktop"]["briefs"] is True
            assert (await client.get("/api/preferences")).json() == again
            # Unknown keys and out-of-range values are rejected as a whole: nothing half-applied.
            for bad in ({"voice": {"speed": "turbo"}}, {"voice": {"endpointStallSeconds": 6}}, {"data": {"sessionRetentionDays": 3}},
                        {"shortcuts": {"x": "y"}}, {"trade": {"defaultVolume": 2, "nope": 1}},
                        {"trade": {"aiAutonomy": "maybe"}}, {}):
                assert (await client.put("/api/preferences", json=bad)).status_code == 422, bad
            assert (await client.get("/api/preferences")).json()["version"] == 2
            # Optional optimistic lock: a stale version is refused, the matching one applies.
            assert (await client.put("/api/preferences", params={"version": 1}, json={"voice": {"speed": "slow"}})).status_code == 409
            assert (await client.put("/api/preferences", params={"version": 2}, json={"voice": {"speed": "fast"}})).json()["version"] == 3
            # An unknown voice id is refused like the voice route does; a known one is accepted by both routes.
            assert (await client.put("/api/preferences", json={"voice": {"voice": "zh_nobody"}})).status_code == 404
            chosen = (await client.get("/api/voice/realtime/voices")).json()["voices"][1]["id"]
            assert (await client.put("/api/voice/realtime/voice", json={"voice": chosen})).status_code == 200
            assert (await client.get("/api/preferences")).json()["preferences"]["voice"]["voice"] == chosen
            assert (await client.get("/api/voice/realtime/status")).json()["voice"] == chosen
            # The catalog carries both routes and both tools; an agent reads and writes through the same door.
            catalog = (await client.get("/api/agent/capabilities")).json()
            operations = {item["operation"]: item for item in catalog["operations"]}
            assert operations["GET /api/preferences"]["readOnly"] and not operations["PUT /api/preferences"]["readOnly"]
            assert "tool:get_preferences" not in operations  # the fake adapter has no native tools until with_tools()
            with_tools(app)
            catalog = (await client.get("/api/agent/capabilities")).json()
            operations = {item["operation"]: item for item in catalog["operations"]}
            assert operations["tool:get_preferences"]["readOnly"] and not operations["tool:update_preferences"]["readOnly"]
            read = (await client.post("/api/agent/call", json={"operation": "tool:get_preferences"})).json()
            assert read["ok"] and read["data"]["data"]["preferences"]["voice"]["speed"] == "fast"
            written = (await client.post("/api/agent/call", json={"operation": "tool:update_preferences",
                                                                  "body": {"changes": {"chat": {"replyStyle": "detailed"}}}})).json()
            assert written["ok"] and written["data"]["data"]["version"] == 5 and written["data"]["data"]["preferences"]["chat"]["replyStyle"] == "detailed"
            refused = (await client.post("/api/agent/call", json={"operation": "tool:update_preferences",
                                                                  "body": {"changes": {"trade": {"defaultVolume": 500}}}})).json()
            assert not refused["ok"] and "defaultVolume" in refused["data"]["warning"]
    app.state.store.close()


def test_the_legacy_voice_table_migrates_into_the_document(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE user_voice_prefs (user_id TEXT PRIMARY KEY, voice TEXT NOT NULL)")
    connection.execute("INSERT INTO user_voice_prefs VALUES ('alice', 'zh_male_xiaotian_jupiter_bigtts')")
    connection.commit()
    connection.close()
    store = RuntimeStore(path)
    assert store.user_preferences("alice").voice.voice == "zh_male_xiaotian_jupiter_bigtts"
    assert store.user_preferences("alice").voice.speed == "normal"
    assert store.get_preferences("bob") == {"version": 0, "preferences": UserPreferences().model_dump()}
    assert store.connection.execute("SELECT 1 FROM sqlite_master WHERE name = 'user_voice_prefs'").fetchone() is None
    # A document from a newer Runtime (unknown key) reads as defaults instead of failing every consumer.
    store.connection.execute("UPDATE user_preferences SET preferences_json = ? WHERE user_id = 'alice'", (json.dumps({"future": 1}),))
    store.connection.commit()
    assert store.user_preferences("alice") == UserPreferences()
    store.close()


OLD_DEFAULT_PINS = ["strategy-signals", "strategy-guided-design"]


def _seed_preferences_row(path, user_id: str, document: dict, version: int = 3) -> None:
    """Seed a user_preferences row before any RuntimeStore opens the database."""
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE IF NOT EXISTS user_preferences ("
                       " user_id TEXT PRIMARY KEY, version INTEGER NOT NULL,"
                       " preferences_json TEXT NOT NULL, updated_at TEXT NOT NULL)")
    connection.execute("INSERT INTO user_preferences VALUES (?, ?, ?, '2026-10-04T00:00:00+00:00')",
                       (user_id, version, json.dumps(document, ensure_ascii=False)))
    connection.commit()
    connection.close()


def test_startup_migration_clears_the_old_default_pins_once(tmp_path):
    """快捷技能默认不置顶 (owner 2026-10-05): the first store start clears rows still carrying
    exactly the old default pins (same order); user-modified lists are untouched, and the
    quick_skills_default_cleared marker keeps a re-pinned list from being cleared again."""
    path = tmp_path / "quick-skills-migration.sqlite3"
    _seed_preferences_row(path, "untouched-hand-me-down", {"trade": {"defaultVolume": 3},
                                                          "ui": {"quickSkills": OLD_DEFAULT_PINS}})
    _seed_preferences_row(path, "reordered", {"ui": {"quickSkills": list(reversed(OLD_DEFAULT_PINS))}})
    _seed_preferences_row(path, "added-one", {"ui": {"quickSkills": ["help", *OLD_DEFAULT_PINS]}})
    _seed_preferences_row(path, "dropped-one", {"ui": {"quickSkills": OLD_DEFAULT_PINS[:1]}})
    _seed_preferences_row(path, "no-ui", {"trade": {"defaultVolume": 2}})

    store = RuntimeStore(path)
    cleared = store.connection.execute(
        "SELECT version, preferences_json, updated_at FROM user_preferences WHERE user_id = 'untouched-hand-me-down'"
    ).fetchone()
    document = json.loads(cleared["preferences_json"])
    # The old default becomes [] with version + 1 and a fresh updated_at; other keys in the
    # document survive the rewrite.
    assert document["ui"]["quickSkills"] == [] and document["trade"]["defaultVolume"] == 3
    assert cleared["version"] == 4 and cleared["updated_at"] > "2026-10-04"
    for user, expected in (("reordered", list(reversed(OLD_DEFAULT_PINS))),
                           ("added-one", ["help", *OLD_DEFAULT_PINS]),
                           ("dropped-one", OLD_DEFAULT_PINS[:1])):
        row = store.connection.execute(
            "SELECT version, preferences_json FROM user_preferences WHERE user_id = ?", (user,)).fetchone()
        assert json.loads(row["preferences_json"])["ui"]["quickSkills"] == expected and row["version"] == 3
    # A fresh user reads the new default: no pins.
    assert store.get_preferences("newcomer")["preferences"]["ui"]["quickSkills"] == []
    assert store.connection.execute(
        "SELECT 1 FROM quick_skills_default_cleared WHERE id = 1").fetchone() is not None
    store.close()


def test_a_pin_of_a_retired_skill_never_resets_the_other_preferences(tmp_path):
    """策略编辑卡 (2026-10-08) retired strategy-guided-design: a stored row that still pins it reads
    without that pin — every other setting kept, not the whole document reset to defaults because the
    validator refuses unknown skill ids — and the next write stores the list without it."""
    path = tmp_path / "retired-pin.sqlite3"
    _seed_preferences_row(path, "alice", {"trade": {"defaultVolume": 3},
                                          "ui": {"quickSkills": ["help", "strategy-guided-design", "strategy-signals"]}})
    store = RuntimeStore(path)
    preferences = store.get_preferences("alice")["preferences"]
    assert preferences["ui"]["quickSkills"] == ["help", "strategy-signals"]
    assert preferences["trade"]["defaultVolume"] == 3
    store.update_preferences("alice", {"trade": {"defaultVolume": 4}})
    row = store.connection.execute("SELECT preferences_json FROM user_preferences WHERE user_id = 'alice'").fetchone()
    assert json.loads(row["preferences_json"])["ui"]["quickSkills"] == ["help", "strategy-signals"]
    with pytest.raises(HTTPException):
        store.update_preferences("alice", {"ui": {"quickSkills": ["strategy-guided-design"]}})
    store.close()


def test_a_document_with_future_keys_keeps_known_settings_and_the_unknown_keys_survive_writes(tmp_path):
    """522-F: a rollback to an older Runtime must not reset the settings a newer
    version wrote (an explicit off stays off) and must not destroy the newer
    fields — they ride along untouched until an upgrade reads them again."""
    store = RuntimeStore(tmp_path / "future-keys.sqlite3")
    future_document = {
        "trade": {"defaultVolume": 3, "aiAutonomy": False},
        "chat": {"memory": False, "replyStyle": "detailed"},
        "chart": {"maLines": [{"period": 5, "enabled": False, "futureFlag": True}, {"period": 10}]},
        "voice": {"speed": "fast", "futureKey": {"nested": True}},
        "wholeFutureCategory": {"anything": 1},
    }
    store.connection.execute(
        "INSERT INTO user_preferences (user_id, version, preferences_json, updated_at)"
        " VALUES ('alice', 4, ?, '2026-10-04T00:00:00+00:00')",
        (json.dumps(future_document),))
    store.connection.commit()

    # Reading keeps every known setting — the explicit offs all stay off.
    document = store.get_preferences("alice")
    assert document["version"] == 4
    preferences = document["preferences"]
    assert preferences["trade"]["defaultVolume"] == 3 and preferences["trade"]["aiAutonomy"] is False
    assert preferences["chat"]["memory"] is False and preferences["chat"]["replyStyle"] == "detailed"
    assert preferences["chart"]["maLines"][0] == {"period": 5, "enabled": False}
    assert preferences["voice"]["speed"] == "fast"

    # A later write keeps the unknown keys exactly as they were stored.
    saved = store.update_preferences("alice", {"ui": {"autoHidePositionsBar": False}})
    assert saved["version"] == 5 and saved["preferences"]["ui"]["autoHidePositionsBar"] is False
    stored = json.loads(store.connection.execute(
        "SELECT preferences_json FROM user_preferences WHERE user_id = 'alice'").fetchone()[0])
    assert stored["wholeFutureCategory"] == {"anything": 1}
    assert stored["voice"]["futureKey"] == {"nested": True}
    assert stored["chart"]["maLines"][0]["futureFlag"] is True
    assert stored["chart"]["maLines"][0]["period"] == 5 and stored["chart"]["maLines"][1]["enabled"] is True
    # And the known settings still read back the same after that write.
    again = store.get_preferences("alice")
    assert again["preferences"]["trade"]["aiAutonomy"] is False
    assert again["preferences"]["chart"]["maLines"][0] == {"period": 5, "enabled": False}
    store.close()


def test_the_preference_ui_command_validates_like_a_put_and_matches_the_snapshot():
    command = UiChanges.model_validate({"preference": {"key": "voice.speed", "value": "fast"}})
    assert command.preference.key == "voice.speed"
    for bad in ({"key": "voice.speed", "value": "turbo"}, {"key": "shortcuts.x", "value": 1}, {"key": "voice", "value": "fast"},
                {"key": "trade.defaultVolume", "value": 0}):
        with pytest.raises(ValidationError):
            UiChanges.model_validate({"preference": bad})
    nested = UiChanges.model_validate({"preference": {"key": "notify.desktop.fills", "value": False}}).model_dump(exclude_none=True)
    document = UserPreferences().model_dump()
    assert changes_match(ui_state(), nested) is True  # a client without the document: its receipt stands
    assert changes_match(ui_state(preferences=document), nested) is False
    document["notify"]["desktop"]["fills"] = False
    assert changes_match(ui_state(preferences=document), nested) is True
    fast = command.model_dump(exclude_none=True)
    assert changes_match(ui_state(preferences=document), fast) is False
    document["voice"]["speed"] = "fast"
    assert changes_match(ui_state(preferences=document), fast) is True


def test_a_ui_command_never_carries_a_trading_control():
    """#932 复核 (P1): AI 自主执行, 语音下单免口头确认 and a running strategy's 执行方式 change only through their own
    interfaces (update_preferences / PUT /api/preferences, start_strategy_monitoring); a UI command carrying one is
    refused, saying which interface to use. Other trade settings stay settable from the settings page."""
    for change, says in (({"preference": {"key": "trade.aiAutonomy", "value": False}}, "update_preferences"),
                         ({"preference": {"key": "trade.voiceOrderConfirm", "value": False}}, "update_preferences"),
                         ({"preference": {"key": "trade.aiAutonomy", "value": True}}, "update_preferences"),
                         ({"strategyInstance": {"strategyId": "s", "signalAction": "auto"}}, "start_strategy_monitoring"),
                         ({"strategyInstance": {"strategyId": "s", "signalAction": "alert"}}, "start_strategy_monitoring")):
        with pytest.raises(ValidationError, match=says):
            UiChanges.model_validate(change)
    assert UiChanges.model_validate({"preference": {"key": "trade.defaultVolume", "value": 2}}).preference.value == 2
    assert UiChanges.model_validate({"preference": {"key": "trade.quickOrder", "value": True}}).preference.value is True


def test_the_default_chart_view_preference_accepts_the_new_periods():
    """默认周期 (2026-10-02): 30 分与周线进白名单；分时写在 defaultView（客户端把数据周期钉到 60 秒）。"""
    doc = UserPreferences.model_validate({"chart": {"defaultDuration": 604800, "defaultView": "timeshare"}})
    assert (doc.chart.defaultDuration, doc.chart.defaultView) == (604800, "timeshare")
    assert UserPreferences.model_validate({"chart": {"defaultDuration": 1800}}).chart.defaultDuration == 1800
    assert UserPreferences().chart.defaultView == "kline"
    for bad in ({"defaultDuration": 1200}, {"defaultView": "tick"}):
        with pytest.raises(ValidationError):
            UserPreferences.model_validate({"chart": bad})
    # The preference command validates like a PUT and its receipt follows the document.
    command = UiChanges.model_validate({"preference": {"key": "chart.defaultView", "value": "timeshare"}})
    with pytest.raises(ValidationError):
        UiChanges.model_validate({"preference": {"key": "chart.defaultView", "value": "tick"}})
    document = UserPreferences().model_dump()
    changes = command.model_dump(exclude_none=True)
    assert changes_match(ui_state(preferences=document), changes) is False
    document["chart"]["defaultView"] = "timeshare"
    assert changes_match(ui_state(preferences=document), changes) is True


def test_the_agent_reads_its_account_settings_at_the_start_of_a_turn():
    assert preference_instruction(UserPreferences()) == ""
    changed = UserPreferences.model_validate({"trade": {"defaultPriceIntent": "passive", "defaultVolume": 3, "closePreference": "today"},
                                              "chat": {"replyStyle": "detailed", "autoReadScreen": False}, "voice": {"autoAnnotate": False}})
    line = preference_instruction(changed)
    assert line.startswith("本账号设置") and "挂单价" in line and "默认 3 手" in line and "平今仓" in line
    assert "可以展开" in line and "不必先读页面快照" in line and "annotate_chart" not in line
    assert "annotate_chart" in preference_instruction(changed, origin="voice")
    # AI 自主执行 off is stated up front, so the model reaches for the card tool instead of the
    # execution tool the Runtime would refuse (外审 09-19 A5).
    off = preference_instruction(UserPreferences.model_validate({"trade": {"aiAutonomy": False}}))
    assert "关闭了 AI 自主执行" in off and "propose_order" in off


async def test_quick_skill_pins_validate_through_the_same_doors(tmp_path):
    """快捷技能栏钉选 (`ui.quickSkills`): the document defaults to no pins (owner
    2026-10-05, AI 建议问题顶上), the registry's own rules refuse unknown /
    quick-action-less / duplicated ids as a whole, and the cap of 8 fires on the
    length itself."""
    app = _app(tmp_path, "quick-skills.sqlite3")
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://runtime", headers=HEADERS) as client:
            initial = (await client.get("/api/preferences")).json()
            assert initial["preferences"]["ui"]["quickSkills"] == []
            pinned = await client.put("/api/preferences", json={"ui": {"quickSkills": ["help", "review-summary"]}})
            assert pinned.status_code == 200, pinned.text
            assert pinned.json()["preferences"]["ui"]["quickSkills"] == ["help", "review-summary"]
            # An empty list is a valid state: the bar hides, nothing else is implied.
            hidden = await client.put("/api/preferences", json={"ui": {"quickSkills": []}})
            assert hidden.status_code == 200 and hidden.json()["preferences"]["ui"]["quickSkills"] == []
            for bad in ({"ui": {"quickSkills": ["nope"]}},                 # unknown skill id
                        {"ui": {"quickSkills": ["ai-execution"]}},         # real skill, no quick action
                        {"ui": {"quickSkills": ["help", "help"]}}):        # duplicates
                assert (await client.put("/api/preferences", json=bad)).status_code == 422, bad
            with pytest.raises(ValidationError):
                UserPreferences.model_validate({"ui": {"quickSkills": ["help"] * 9}})  # over the cap of 8
            assert (await client.get("/api/preferences")).json()["preferences"]["ui"]["quickSkills"] == []
    app.state.store.close()


def test_quick_skill_pins_travel_the_ui_command_channel():
    # The one list-valued preference rides the `preference` command and validates like a PUT.
    pins = UiChanges.model_validate({"preference": {"key": "ui.quickSkills", "value": ["help"]}})
    document = UserPreferences().model_dump()
    assert changes_match(ui_state(preferences=document), pins.model_dump(exclude_none=True)) is False
    document["ui"]["quickSkills"] = ["help"]
    assert changes_match(ui_state(preferences=document), pins.model_dump(exclude_none=True)) is True
    for bad in (["nope"], ["ai-execution"], ["help", "help"], ["help"] * 9):
        with pytest.raises(ValidationError):
            UiChanges.model_validate({"preference": {"key": "ui.quickSkills", "value": bad}})


def test_the_ai_figure_is_one_of_three_and_travels_the_preference_command():
    """设置 → 界面 → AI 形象 (`ui.aiAvatar`): K 线音柱 by default; only the three figures are
    accepted, through a PUT-shaped document and the `preference` UI command alike."""
    assert UserPreferences().ui.aiAvatar == "kline"
    assert UserPreferences.model_validate({"ui": {"aiAvatar": "pixel"}}).ui.aiAvatar == "pixel"
    with pytest.raises(ValidationError):
        UserPreferences.model_validate({"ui": {"aiAvatar": "robot"}})
    command = UiChanges.model_validate({"preference": {"key": "ui.aiAvatar", "value": "eyes"}})
    document = UserPreferences().model_dump()
    assert changes_match(ui_state(preferences=document), command.model_dump(exclude_none=True)) is False
    document["ui"]["aiAvatar"] = "eyes"
    assert changes_match(ui_state(preferences=document), command.model_dump(exclude_none=True)) is True
    with pytest.raises(ValidationError):
        UiChanges.model_validate({"preference": {"key": "ui.aiAvatar", "value": "robot"}})


async def test_chart_indicators_are_configurable_with_legacy_subpane_compat(tmp_path):
    """图表指标可配置 (2026-10-02): the MA lines over the price pane (≤8, unique periods), the
    Bollinger overlay, the sub-pane list (0–3 panes, unique kd/macd/rsi/oi types) and the RSI
    params; a write that names `subPaneList` syncs the 1.6.12-era kd/macd switches back."""
    app = _app(tmp_path, "chart-indicators.sqlite3")
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://runtime", headers=HEADERS) as client:
            chart = (await client.get("/api/preferences")).json()["preferences"]["chart"]
            assert [line["period"] for line in chart["maLines"]] == [5, 10, 25, 60, 200]
            assert all(line["enabled"] for line in chart["maLines"])
            assert chart["subPaneList"] == ["kd", "macd"]
            assert chart["boll"] == {"enabled": False, "period": 20, "width": 2.0}
            assert chart["rsiParams"] == {"period": 14}
            rewritten = await client.put("/api/preferences", json={"chart": {
                "maLines": [{"period": 5}, {"period": 20}, {"period": 120, "enabled": False}],
                "boll": {"enabled": True, "period": 26, "width": 2.5},
                "subPaneList": ["macd", "rsi", {"type": "oi"}],   # names; the {type} spelling parses the same
                "rsiParams": {"period": 6}}})
            assert rewritten.status_code == 200, rewritten.text
            chart = rewritten.json()["preferences"]["chart"]
            assert chart["maLines"] == [{"period": 5, "enabled": True}, {"period": 20, "enabled": True}, {"period": 120, "enabled": False}]
            assert chart["boll"] == {"enabled": True, "period": 26, "width": 2.5} and chart["rsiParams"] == {"period": 6}
            assert chart["subPaneList"] == ["macd", "rsi", "oi"]
            # The 1.6.12-era switches stay in step with the list, so an installed desktop shows the same panes.
            assert chart["subPanes"] == {"kd": False, "macd": True}
            for bad in ({"maLines": [{"period": period} for period in range(2, 11)]},   # over the cap of 8
                        {"maLines": []},                                              # at least one line stays
                        {"maLines": [{"period": 1}]}, {"maLines": [{"period": 501}]},
                        {"maLines": [{"period": 5}, {"period": 5}]},                    # duplicate period
                        {"boll": {"width": 5}}, {"boll": {"period": 2}},
                        {"subPaneList": ["kd", "macd", "rsi", "oi"]},                    # over the cap of 3
                        {"subPaneList": ["rsi", "rsi"]},                                # duplicate type
                        {"subPaneList": ["boll"]}, {"rsiParams": {"period": 1}}):
                assert (await client.put("/api/preferences", json={"chart": bad})).status_code == 422, bad
    app.state.store.close()


async def test_the_phone_market_and_the_new_indicators_are_account_preferences(tmp_path):
    """手机看盘 (2026-10-09): 报价 / 多图 / 单图 and their settings live in ui.mobileMarket (a partial write keeps the
    rest — the view switch never clobbers the cells); cci / volume join the shared sub-pane list and SAR the
    price pane, with the same validation for the phone and the computer."""
    app = _app(tmp_path, "mobile-market.sqlite3")
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://runtime", headers=HEADERS) as client:
            preferences = (await client.get("/api/preferences")).json()["preferences"]
            market = preferences["ui"]["mobileMarket"]
            assert market["view"] == "single" and market["board"] == "watchlist" and market["layout"] == "four"
            assert len(market["cells"]) == 6 and market["periods"] == [300, 900, 3600, 86400]
            assert preferences["chart"]["sar"] == {"enabled": False, "step": 0.02, "max": 0.2}
            cells = [{"symbol": "SHFE.rb2701", "duration": 900}] + [{"symbol": None, "duration": 300}] * 5
            written = await client.put("/api/preferences", json={"ui": {"mobileMarket": {"layout": "six", "cells": cells}},
                                                                 "chart": {"subPaneList": ["volume", "cci"], "sar": {"enabled": True}}})
            assert written.status_code == 200, written.text
            partial = await client.put("/api/preferences", json={"ui": {"mobileMarket": {"view": "multi"}}})
            market = partial.json()["preferences"]["ui"]["mobileMarket"]
            assert market["view"] == "multi" and market["layout"] == "six" and market["cells"][0]["symbol"] == "SHFE.rb2701"
            assert partial.json()["preferences"]["chart"]["subPaneList"] == ["volume", "cci"]
            for bad in ({"columns": ["last", "last"]}, {"columns": []}, {"columns": ["last", "bid", "ask", "volume", "change"]},
                        {"cells": [{"duration": 300}] * 7}, {"cells": [{"symbol": "rb 2701", "duration": 300}]},
                        {"periods": [300, 900]}, {"view": "chart"}, {"subPane": "boll"}):
                assert (await client.put("/api/preferences", json={"ui": {"mobileMarket": bad}})).status_code == 422, bad
            assert (await client.put("/api/preferences", json={"chart": {"sar": {"step": 0.5}}})).status_code == 422
    app.state.store.close()


async def test_the_market_board_columns_sort_and_filter_are_preferences(tmp_path):
    """报价总表列可自定义 (2026-10-02): the column list (1–12, unique, order = display order),
    the sort and the exchange filter are one preference category; a partial write only changes the
    keys it names, so the client's debounced sort/filter write-back never clobbers the columns."""
    app = _app(tmp_path, "board-columns.sqlite3")
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://runtime", headers=HEADERS) as client:
            board = (await client.get("/api/preferences")).json()["preferences"]["marketBoard"]
            assert board == {"columns": ["last", "change", "changePercent", "volume",
                                         "openInterest", "openInterestChange", "high", "low"],
                             "sort": {"key": "changePercent", "order": "desc"}, "exchange": "all"}
            rewritten = await client.put("/api/preferences", json={"marketBoard": {
                "columns": ["last", "preSettlement", "upperLimit", "lowerLimit", "quoteTime"],
                "sort": {"key": "volume", "order": "asc"}, "exchange": "CZCE"}})
            assert rewritten.status_code == 200, rewritten.text
            board = rewritten.json()["preferences"]["marketBoard"]
            assert board["columns"] == ["last", "preSettlement", "upperLimit", "lowerLimit", "quoteTime"]
            assert board["sort"] == {"key": "volume", "order": "asc"} and board["exchange"] == "CZCE"
            # The window writes back a sort/filter change alone: the columns survive it.
            partial = await client.put("/api/preferences", json={"marketBoard": {"exchange": "all"}})
            assert partial.status_code == 200, partial.text
            board = partial.json()["preferences"]["marketBoard"]
            assert board["exchange"] == "all"
            assert board["columns"] == ["last", "preSettlement", "upperLimit", "lowerLimit", "quoteTime"]
            assert board["sort"] == {"key": "volume", "order": "asc"}
            for bad in ({"columns": []},                        # at least one column stays
                        {"columns": ["last"] * 13},             # over the cap of 12
                        {"columns": ["last", "last"]},          # duplicate column
                        {"columns": ["nope"]},
                        {"sort": {"key": "last"}},              # only the four sortable keys
                        {"sort": {"order": "sideways"}},
                        {"exchange": "NYSE"}):
                assert (await client.put("/api/preferences", json={"marketBoard": bad})).status_code == 422, bad
    app.state.store.close()


async def test_the_watchlist_is_one_preference_list_of_products(tmp_path):
    """自选品种 (2026-10-02): the starred products are one account list ("<交易所>.<品种>", newest
    last) the board's 自选 filter, the contract drawer and the contract head share; a star writes
    the whole list, so the format check, the dedupe and the cap of 50 keep it sane."""
    app = _app(tmp_path, "watchlist.sqlite3")
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://runtime", headers=HEADERS) as client:
            assert (await client.get("/api/preferences")).json()["preferences"]["watchlist"] == {"products": []}
            starred = await client.put("/api/preferences", json={"watchlist": {"products": ["SHFE.rb", "CZCE.CF"]}})
            assert starred.status_code == 200, starred.text
            assert starred.json()["preferences"]["watchlist"]["products"] == ["SHFE.rb", "CZCE.CF"]
            # A whole-list write replaces it (order = the order written, newest last); 自选 is one
            # of the marketBoard exchange codes the board's filter remembers.
            replaced = await client.put("/api/preferences", json={"watchlist": {"products": ["DCE.m", "SHFE.rb"]},
                                                                  "marketBoard": {"exchange": "watchlist"}})
            assert replaced.status_code == 200, replaced.text
            assert replaced.json()["preferences"]["watchlist"]["products"] == ["DCE.m", "SHFE.rb"]
            assert replaced.json()["preferences"]["marketBoard"]["exchange"] == "watchlist"
            # A double star is one product (order-preserving dedupe).
            deduped = await client.put("/api/preferences", json={"watchlist": {"products": ["SHFE.rb", "SHFE.rb"]}})
            assert deduped.status_code == 200, deduped.text
            assert deduped.json()["preferences"]["watchlist"]["products"] == ["SHFE.rb"]
            for bad in ({"products": ["rb"]},                                    # the exchange is part of the key
                        {"products": ["shfe.rb"]},                               # case as the catalog spells it
                        {"products": [f"SHFE.p{i:02d}" for i in range(51)]},     # over the cap of 50
                        {"products": [], "extra": 1}):                           # no keys beyond the list
                assert (await client.put("/api/preferences", json={"watchlist": bad})).status_code == 422, bad
    app.state.store.close()


def test_old_documents_derive_the_subpane_list_and_old_patches_still_win():
    # A document saved before the list existed: the kd/macd switches decide it (kd first when both).
    document = UserPreferences.model_validate({"chart": {"subPanes": {"kd": False, "macd": True}}})
    assert document.chart.subPaneList == ["macd"]
    assert UserPreferences.model_validate({"chart": {"subPanes": {"kd": False, "macd": False}}}).chart.subPaneList == []
    assert UserPreferences.model_validate({"chart": {"subPanes": {"kd": True, "macd": False}}}).chart.subPaneList == ["kd"]
    # A 1.6.12 patch toggles the old switches: it must beat a stored list that says otherwise.
    current = UserPreferences.model_validate({"chart": {"subPaneList": ["kd", "rsi"]}})
    merged = merge_preferences(current, {"chart": {"subPanes": {"kd": False, "macd": True}}})
    assert merged.chart.subPaneList == ["macd"]
    assert merged.chart.subPanes.kd is False and merged.chart.subPanes.macd is True
    # A patch that names the list syncs the switches for the old client instead.
    merged = merge_preferences(current, {"chart": {"subPaneList": [{"type": "rsi"}, {"type": "oi"}]}})
    assert merged.chart.subPanes.kd is False and merged.chart.subPanes.macd is False


async def test_voice_consumers_read_the_call_preferences(tmp_path):
    app, gateway, service = await _voice_app(tmp_path)
    store = app.state.store
    store.update_preferences("local-user", {"voice": {"speed": "slow", "endpointStallSeconds": 0.8, "confirmWindowSeconds": 5,
                                                      "announceFills": False, "announceBriefs": False}})
    state = CallState(window_id=WINDOW, preferences=store.user_preferences("local-user"))
    # The watchdog is the safety net behind Doubao's own endpointing: the old 0.8 s value an account
    # may still carry is lifted to the new floor instead of 422-ing the stored document.
    assert SPEECH_SPEEDS[state.preferences.voice.speed] == -20 and state.preferences.voice.endpointStallSeconds == 2.0
    # The confirm window is the account's 5 s, shown on the card as voiceConfirmBy ≈ now + 5 s.
    prepared = await service.run_tool("delegate", {"utterance": "买入一手黄金"}, state)
    assert prepared["pendingConfirmation"] is True and gateway.submit_count == 0
    await service._open_confirm_window(state, time.monotonic() + state.preferences.voice.confirmWindowSeconds)
    proposal_id = state.pending["proposals"][0]["proposalId"]
    confirm_by = datetime.fromisoformat(store.get(proposal_id)["payload"]["voiceConfirmBy"])
    assert timedelta(seconds=3) < confirm_by - datetime.now(UTC) <= timedelta(seconds=5)
    await service._abandon_pending(state)
    # #932: the retired voice-only switches decide nothing — with them false, a fill, a cancel and an AI 简讯
    # are still spoken while their own reminder switches are on, and silenced by those switches alone.
    from trader_runtime.reminder_kinds import voice_allowed
    store.update_preferences("local-user", {"notify": {"desktop": {"briefs": True}}})
    preferences = store.user_preferences("local-user").model_dump()
    assert not preferences["voice"]["announceFills"] and not preferences["voice"]["announceBriefs"]
    assert all(voice_allowed(preferences, kind) for kind in ("fill", "cancel", "brief", "order_unfilled", "target_plan"))
    store.update_preferences("local-user", {"notify": {"desktop": {"fills": False, "cancels": False, "briefs": False}}})
    preferences = store.user_preferences("local-user").model_dump()
    assert not any(voice_allowed(preferences, kind) for kind in ("fill", "cancel", "brief"))
    assert voice_allowed(preferences, "strategy") and voice_allowed(preferences, "stop_triggered")
    app.state.store.close()


async def test_a_spoken_order_skips_the_spoken_confirmation_only_when_the_account_says_so(tmp_path, monkeypatch):
    app, gateway, service = await _voice_app(tmp_path, "auto-confirm.sqlite3")
    store = app.state.store
    store.update_preferences("local-user", {"trade": {"voiceOrderConfirm": False}})
    state = CallState(window_id=WINDOW, preferences=store.user_preferences("local-user"))
    # A card that is not the fast parser's reading of the words (here the demo agent's) keeps its spoken 确认.
    asked = CallState(window_id=WINDOW, preferences=store.user_preferences("local-user"))
    modelled = await service.run_tool("delegate", {"utterance": "买入一手黄金"}, asked)
    assert modelled["pendingConfirmation"] is True and "autoConfirmed" not in modelled and gateway.submit_count == 0
    await service._abandon_pending(asked)
    # The parser's card (intentSource parser) is confirmed for the user at once.
    from trader_runtime.proposals import ProposalService
    create = ProposalService.create_trade
    monkeypatch.setattr(ProposalService, "create_trade",
                        lambda self, *args, **kwargs: create(self, *args, **{**kwargs, "intent_source": "parser"}))
    result = await service.run_tool("delegate", {"utterance": "买入一手黄金"}, state)
    assert result["ok"] is True and result["autoConfirmed"] is True and result["pendingConfirmation"] is False
    assert "已按你的设置直接下单" in result["spoken"] and state.pending is None
    assert gateway.submit_count == 1
    # The default keeps the spoken confirmation.
    default = CallState(window_id=WINDOW)
    again = await service.run_tool("delegate", {"utterance": "买入一手黄金"}, default)
    assert again["pendingConfirmation"] is True and gateway.submit_count == 1
    await service._abandon_pending(default)
    app.state.store.close()


async def test_retained_chats_are_released_after_the_users_retention_days(tmp_path):
    app = _app(tmp_path, "retention.sqlite3")
    store = app.state.store
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://runtime", headers=HEADERS) as client:
            old = (await client.post("/api/sessions", params={"chat": "true"}, json={"mode": "local"})).json()["sessionId"]
            fresh = (await client.post("/api/sessions", params={"chat": "true"}, json={"mode": "local"})).json()["sessionId"]
            ancient = (datetime.now(UTC) - timedelta(days=30)).isoformat()
            store.connection.execute("UPDATE sessions SET created_at = ? WHERE id = ?", (ancient, old))
            # The fake agent seeds a demo exchange (a user message included): it is as old as the chat.
            store.connection.execute("UPDATE events SET timestamp = ? WHERE session_id = ?", (ancient, old))
            store.connection.commit()
            # Default 365 days: nothing goes.
            assert await app.state.lifecycle.cleanup_expired() == 0
            store.update_preferences("local-user", {"data": {"sessionRetentionDays": 7}})
            assert await app.state.lifecycle.cleanup_expired() == 1
            assert (await client.get(f"/api/sessions/{old}")).status_code == 404
            assert (await client.get(f"/api/sessions/{fresh}")).status_code == 200
    store.close()


async def test_quick_voice_turns_count_as_activity_for_chat_retention(tmp_path):
    """#498: a chat the user keeps talking to by quick voice (no agent run) is not idle; a pushed
    reminder is not the user's turn; a turn landing between the scan and the deletion keeps its chat."""
    app, _, service = await _voice_app(tmp_path, "voice-retention.sqlite3")
    store, lifecycle = app.state.store, app.state.lifecycle
    store.update_preferences("local-user", {"data": {"sessionRetentionDays": 7}})

    def retained(days: int) -> str:
        session_id = store.create_session(user_id="local-user", mode="local", agent_id="native")["sessionId"]
        store.retain_chat_session(session_id, user_id="local-user")
        with store.connection:
            store.connection.execute("UPDATE sessions SET created_at = ? WHERE id = ?",
                                     ((datetime.now(UTC) - timedelta(days=days)).isoformat(), session_id))
        return session_id

    idle, reminded, spoken, late = retained(10), retained(9), retained(8), retained(8)
    await service.record_quick_turn(CallState(window_id=WINDOW, chat_session_id=spoken), "看一下黄金", "黄金现价六百")
    await app.state.broker.publish(reminded, kind="message", name="message.completed",
                                   payload={"turnId": "remind-1", "role": "assistant", "text": "策略提醒"})
    release = lifecycle.release

    async def racing_release(session_id: str, *, stale_before: str | None = None) -> bool:
        if session_id == idle:  # while the first stale chat is released, the user speaks in another one
            await service.record_quick_turn(CallState(window_id=WINDOW, chat_session_id=late), "还在吗", "在")
        return await release(session_id, stale_before=stale_before)

    lifecycle.release = racing_release
    assert await lifecycle.cleanup_expired() == 2
    left = {row["id"] for row in store.connection.execute("SELECT id FROM sessions")}
    assert spoken in left and late in left and idle not in left and reminded not in left
    texts = [event["payload"]["text"] for _, event in store.list_events(spoken) if event["name"] == "message.completed"]
    assert texts == ["看一下黄金", "黄金现价六百"]
    store.close()
