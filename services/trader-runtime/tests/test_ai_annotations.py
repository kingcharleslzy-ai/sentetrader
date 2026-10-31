"""AI annotation layer, step one: the highlight / chartAnnotations / chartFocus commands, their
receipts, and the agent tools that read the chart and draw on it through the caller's window."""
import asyncio
import json
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError

from trader_runtime.main import create_app
from trader_runtime.message_context import encode_instruction
from trader_runtime.trader_tools import TraderReadOnlyTools
from trader_runtime.ui_control import ChartOptions, UiChanges, changes_match

HEADERS = {"Authorization": "Bearer annotation-test-token", "Origin": "http://127.0.0.1:5173"}
FAKE_START = 1_788_192_000  # fake_market_workbench's first bar, epoch seconds


def ui_state(**overrides):
    state = dict(symbol="SHFE.rb2610", duration=60, view="trade", panel=None, settingsOpen=False,
        theme="dark", dockSide="right", dockWidth=400, replay=False, settingsPage="connection",
        fontScale="standard", density="standard", chart={k: True for k in ChartOptions.model_fields},
        chartSizes=dict(main=58, kd=19, macd=23), strategy=None, highlights=[], chartAnnotations=[], chartFocus=None)
    state.update(overrides)
    return state


def with_tools(app):
    """The fake adapter has no native tools; give it the real tool set bound to the app's capability bridge."""
    tools = TraderReadOnlyTools(app.state.data_provider, app.state.adapter.runtime_status)
    tools.app_capabilities = app.state.app_capabilities
    app.state.adapter.trader_tools = tools
    return tools


def observation(context=None):
    return {"capturedAt": "2026-09-18T00:00:00Z", "viewport": {"width": 1280, "height": 720},
            "text": "交易台", "truncated": False, "regions": [], "controls": [], "context": context or {}}


def test_annotation_commands_validate_their_shapes():
    ok = UiChanges.model_validate({"highlight": {"target": "rail:order", "label": "在这里下单", "durationMs": 3000}})
    assert ok.highlight.style == "pulse"
    UiChanges.model_validate({"highlight": {"clear": True}})
    UiChanges.model_validate({"highlight": {"target": "chart.main", "durationMs": 0}})
    for bad in ({"highlight": {}}, {"highlight": {"target": "x", "clear": True}}, {"highlight": {"target": "x", "durationMs": 500}},
                {"highlight": {"target": "bad target", "durationMs": 2000}}, {"highlight": {"target": "x", "label": "字" * 41}}):
        with pytest.raises(ValidationError):
            UiChanges.model_validate(bad)
    items = [{"id": "p1", "kind": "point", "role": "sell", "price": 3120, "time": 12, "label": "空 3120"},
             {"id": "s1", "kind": "hline", "price": 3100, "label": "压力"},
             {"id": "z1", "kind": "zone", "price": 3050, "price2": 3080, "tone": "good"},
             {"id": "t1", "kind": "trendline", "price": 3000, "time": 10, "price2": 3090, "time2": 20},
             {"id": "v1", "kind": "vline", "time": 15}, {"id": "m1", "kind": "point", "pane": "macd", "price": -0.5, "time": 16, "label": "背离"}]
    parsed = UiChanges.model_validate({"chartAnnotations": {"op": "set", "items": items}})
    assert [item.kind for item in parsed.chartAnnotations.items] == ["point", "hline", "zone", "trendline", "vline", "point"]
    assert parsed.chartAnnotations.items[0].pane == "main" and parsed.chartAnnotations.items[-1].pane == "macd"
    for kind, extra in (("circle", {}), ("arrow", {}), ("text", {"label": "这里"})):
        UiChanges.model_validate({"chartAnnotations": {"op": "add", "items": [{"id": kind, "kind": kind, "price": 3070, "time": 16, **extra}]}})
    UiChanges.model_validate({"chartAnnotations": {"op": "add", "items": [{"id": "k", "kind": "hline", "pane": "kd", "price": 80, "label": "超买"}]}})
    UiChanges.model_validate({"chartAnnotations": {"op": "clear", "symbol": "SHFE.au2612"}})
    for bad in ({"chartAnnotations": {"op": "set"}}, {"chartAnnotations": {"op": "clear", "items": items[:1]}},
                {"chartAnnotations": {"op": "add", "items": [{"id": "z", "kind": "zone", "price": 1}]}},
                {"chartAnnotations": {"op": "add", "items": [{"id": "x", "kind": "text", "price": 1, "time": 1}]}},
                {"chartAnnotations": {"op": "add", "items": [items[1], items[1]]}},
                # Seven on one screen, a nine-character label, a zone on the MACD pane, a role on a line,
                # a non-positive main-pane price, an unknown tone.
                {"chartAnnotations": {"op": "set", "items": [{**items[1], "id": f"i{i}"} for i in range(7)]}},
                {"chartAnnotations": {"op": "add", "items": [{**items[1], "label": "字" * 9}]}},
                {"chartAnnotations": {"op": "add", "items": [{"id": "mz", "kind": "zone", "pane": "macd", "price": 0, "price2": 1}]}},
                {"chartAnnotations": {"op": "add", "items": [{**items[1], "role": "buy"}]}},
                {"chartAnnotations": {"op": "add", "items": [{"id": "neg", "kind": "hline", "price": -1}]}},
                {"chartAnnotations": {"op": "add", "items": [{"id": "n", "kind": "hline", "price": 1, "tone": "purple"}]}}):
        with pytest.raises(ValidationError):
            UiChanges.model_validate(bad)
    focus = UiChanges.model_validate({"chartFocus": {"from": 100, "to": 200}})
    assert focus.model_dump(mode="json", exclude_unset=True) == {"chartFocus": {"from": 100, "to": 200}}
    assert UiChanges.model_validate({"chartFocus": None}).model_dump(exclude_unset=True) == {"chartFocus": None}
    with pytest.raises(ValidationError):
        UiChanges.model_validate({"chartFocus": {"from": 5, "to": 5}})
    # Coverage-audit gaps closed the same day: candidate price, layout reset, activity clear.
    assert UiChanges.model_validate({"selectedPrice": 3100.5}).model_dump(exclude_unset=True) == {"selectedPrice": 3100.5}
    assert UiChanges.model_validate({"selectedPrice": None}).model_dump(exclude_unset=True) == {"selectedPrice": None}
    with pytest.raises(ValidationError):
        UiChanges.model_validate({"selectedPrice": 0})
    assert UiChanges.model_validate({"presentation": {"reset": True}}).presentation.reset is True
    UiChanges.model_validate({"disclosure": {"name": "activity-clear", "open": True}})
    # Guided tour: start carries 1–20 steps with an optional focus subset; next/prev/stop carry none.
    tour = UiChanges.model_validate({"tour": {"op": "start", "steps": [
        {"target": "rail:order", "label": "这里下单", "say": "下单区在这里", "focus": {"panel": "order", "ticketMode": "open"}},
        {"target": "positions-bar", "label": "底栏看持仓"}]}})
    assert tour.tour.steps[0].focus.panel == "order" and tour.tour.steps[1].focus is None
    for op in ("next", "prev", "stop"):
        UiChanges.model_validate({"tour": {"op": op}})
    for bad in ({"tour": {"op": "start"}}, {"tour": {"op": "next", "steps": [{"target": "x", "label": "y"}]}},
                {"tour": {"op": "start", "steps": [{"target": "x", "label": "字" * 41}]}},
                {"tour": {"op": "start", "steps": [{"target": "x", "label": "y", "focus": {"symbol": "SHFE.rb2610"}}]}},
                {"tour": {"op": "start", "steps": [{"target": f"t{i}", "label": "y"} for i in range(21)]}}):
        with pytest.raises(ValidationError):
            UiChanges.model_validate(bad)


def test_receipts_are_judged_by_the_reported_layer_state():
    state = ui_state(highlights=[{"target": "rail:ai", "label": None, "style": "pulse", "until": "2026-09-18T00:00:06Z"}],
                     chartAnnotations=[{"id": "s1", "kind": "hline", "price": 3100, "tone": "info"}], chartFocus={"from": 1, "to": 2})
    dump = lambda changes: UiChanges.model_validate(changes).model_dump(exclude_unset=True)  # noqa: E731
    assert changes_match(state, dump({"highlight": {"target": "rail:ai", "durationMs": 2000}}))
    assert not changes_match(state, dump({"highlight": {"target": "rail:chat", "durationMs": 2000}}))
    assert not changes_match(state, dump({"highlight": {"clear": True}}))
    assert changes_match({**state, "highlights": []}, dump({"highlight": {"clear": True}}))
    assert changes_match(state, dump({"chartAnnotations": {"op": "add", "items": [{"id": "s1", "kind": "hline", "price": 1}]}}))
    assert not changes_match(state, dump({"chartAnnotations": {"op": "set", "items": [{"id": "s2", "kind": "hline", "price": 1}]}}))
    assert not changes_match(state, dump({"chartAnnotations": {"op": "clear"}}))
    assert changes_match(state, dump({"chartAnnotations": {"op": "clear", "symbol": "SHFE.au2612"}}))
    assert changes_match(state, dump({"chartFocus": {"from": 1, "to": 2}}))
    assert not changes_match(state, dump({"chartFocus": None}))
    # A client too old to report the layer is not contradicted.
    old = {key: value for key, value in state.items() if key not in {"highlights", "chartAnnotations", "chartFocus"}}
    assert changes_match(old, dump({"highlight": {"clear": True}})) and changes_match(old, dump({"chartFocus": None}))
    # selectedPrice is a plain field; presentation.reset is judged by the layout it restores.
    assert changes_match({**state, "selectedPrice": 3100.5, "ticketPrice": 3100.5, "ticketPriceMode": "limit"}, dump({"selectedPrice": 3100.5}))
    assert not changes_match({**state, "selectedPrice": 3100.5}, dump({"selectedPrice": None}))
    assert changes_match({**state, "dockSide": "right", "positionsBar": "collapsed"}, dump({"presentation": {"reset": True}}))
    assert not changes_match({**state, "dockSide": "left"}, dump({"presentation": {"reset": True}}))
    assert changes_match({**state, "dockSide": "right", "positionsBar": "collapsed", "theme": "light"}, dump({"presentation": {"reset": True, "theme": "light"}}))
    # A tour receipt: start = active at step 1 of N; stop = inactive; next/prev = the client's word.
    start = dump({"tour": {"op": "start", "steps": [{"target": "a", "label": "b"}, {"target": "c", "label": "d"}]}})
    assert changes_match({**state, "tour": {"active": True, "step": 1, "total": 2, "current": None}}, start)
    assert not changes_match({**state, "tour": {"active": True, "step": 1, "total": 3, "current": None}}, start)
    assert changes_match({**state, "tour": {"active": False, "step": 0, "total": 0}}, dump({"tour": {"op": "stop"}}))
    assert changes_match({**state, "tour": {"active": True, "step": 2, "total": 2}}, dump({"tour": {"op": "next"}}))
    assert changes_match(old, dump({"tour": {"op": "stop"}}))


async def _acknowledge(client, pulse, state_after):
    """Play the client: dispatch the queued command and receipt it as applied with `state_after`."""
    for _ in range(200):
        await asyncio.sleep(.01)
        command = (await client.post("/api/ui/pulse", json=pulse)).json()["command"]
        if command:
            pulse["revision"] += 1
            pulse["state"] = state_after(command["changes"])
            pulse["receipt"] = {"commandId": command["commandId"], "status": "applied"}
            assert (await client.post("/api/ui/pulse", json=pulse)).status_code == 200
            pulse.pop("receipt")
            return command
    raise AssertionError("command never dispatched")


async def test_tools_read_the_visible_chart_and_draw_through_the_callers_window(tmp_path):
    app = create_app(tmp_path / "runtime.sqlite3", runtime_state_root=tmp_path, agent_mode="fake", local_token="annotation-test-token")
    window_id = str(uuid4())
    viewport = {"timeRange": {"from": FAKE_START + 10 * 60, "to": FAKE_START + 30 * 60}, "logicalRange": {"from": 10, "to": 30}}
    pulse = {"windowId": window_id, "revision": 0, "state": ui_state(chart={**{k: True for k in ChartOptions.model_fields}, "ma200": False}),
             "observation": observation({"chartViewport": viewport, "highlightTargets": ["rail:order", "chart.main"]})}
    with_tools(app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://runtime", headers=HEADERS) as client:
            assert (await client.post("/api/ui/pulse", json=pulse)).status_code == 200
            catalog = (await client.get("/api/agent/capabilities")).json()
            names = {item["operation"]: item for item in catalog["operations"]}
            assert names["tool:get_chart_bars"]["readOnly"] and not names["tool:annotate_chart"]["readOnly"] and "tool:highlight_ui" in names

            # get_chart_bars: the visible span of the window's own contract/period, in epoch seconds, with the MAs it shows.
            result = (await client.post("/api/agent/call", json={"operation": "tool:get_chart_bars", "body": {"uiWindowId": window_id}})).json()
            assert result["ok"], result
            data = result["data"]["data"]
            assert data["symbol"] == "SHFE.rb2610" and data["durationSeconds"] == 60 and data["timeUnit"] == "epoch_seconds"
            assert data["range"]["source"] == "visible" and data["barCount"] == 21
            assert data["bars"][0]["time"] == FAKE_START + 10 * 60 and data["bars"][-1]["time"] == FAKE_START + 30 * 60
            assert set(data["bars"][-1]["ma"]) == {"5", "10", "25", "60"} and data["bars"][-1]["ma"]["5"] is not None
            assert data["analysis"]["method"] == "deterministic_ohlcv_oi_summary_v1"
            # recent N of an explicit contract needs no window at all.
            result = (await client.post("/api/agent/call", json={"operation": "tool:get_chart_bars",
                "body": {"symbol": "SHFE.au2612", "durationSeconds": 300, "range": "recent", "limit": 20}})).json()
            data = result["data"]["data"]
            assert data["range"]["source"] == "recent" and data["barCount"] == 20 and set(data["bars"][0]["ma"]) == {"5", "10", "25", "60", "200"}

            # annotate_chart goes through POST /api/app/ui/commands and returns the applied state.
            items = [{"id": "s1", "kind": "hline", "price": 823, "label": "压力", "tone": "warn"},
                     {"id": "z1", "kind": "zone", "price": 815, "price2": 818, "label": "支撑区", "tone": "good"}]
            call = client.post("/api/agent/call", json={"operation": "tool:annotate_chart",
                "body": {"uiWindowId": window_id, "op": "set", "items": items, "focus": {"from": FAKE_START, "to": FAKE_START + 1200}}})
            pending = asyncio.create_task(call)
            drawn = await _acknowledge(client, pulse, lambda changes: {**pulse["state"], "chartAnnotations": changes["chartAnnotations"]["items"]})
            assert drawn["changes"]["chartAnnotations"]["op"] == "set"
            focused = await _acknowledge(client, pulse, lambda changes: {**pulse["state"], "chartFocus": changes["chartFocus"]})
            assert focused["changes"]["chartFocus"] == {"from": FAKE_START, "to": FAKE_START + 1200}
            result = (await pending).json()
            assert result["ok"], result
            data = result["data"]["data"]
            assert data["status"] == "applied" and [item["id"] for item in data["chartAnnotations"]] == ["s1", "z1"]
            assert data["focus"]["status"] == "applied" and data["focus"]["chartFocus"] == {"from": FAKE_START, "to": FAKE_START + 1200}

            # highlight_ui: applied when the window reports the target lit.
            pending = asyncio.create_task(client.post("/api/agent/call", json={"operation": "tool:highlight_ui",
                "body": {"uiWindowId": window_id, "target": "rail:order", "label": "在这里下单", "durationMs": 3000}}))
            lit = await _acknowledge(client, pulse, lambda changes: {**pulse["state"], "highlights": [
                {"target": changes["highlight"]["target"], "label": changes["highlight"].get("label"), "style": "pulse", "until": "2026-09-18T00:00:03Z"}]})
            assert lit["changes"]["highlight"]["target"] == "rail:order"
            data = (await pending).json()["data"]["data"]
            assert data["status"] == "applied" and data["highlights"][0]["target"] == "rail:order"

            # start_tour: one command, the tour's first step lit and reported.
            pending = asyncio.create_task(client.post("/api/agent/call", json={"operation": "tool:start_tour",
                "body": {"uiWindowId": window_id, "steps": [{"target": "rail:order", "label": "这里下单", "say": "下单区", "focus": {"panel": "order"}}, {"target": "positions-bar", "label": "底栏"}]}}))
            started = await _acknowledge(client, pulse, lambda changes: {**pulse["state"], "tour": {"active": True, "step": 1, "total": len(changes["tour"]["steps"]), "current": changes["tour"]["steps"][0]}})
            assert started["changes"]["tour"]["op"] == "start"
            data = (await pending).json()["data"]["data"]
            assert data["status"] == "applied" and data["tour"]["step"] == 1 and data["tour"]["total"] == 2

            # A window the caller does not name and the turn did not bind is never guessed.
            result = (await client.post("/api/agent/call", json={"operation": "tool:highlight_ui", "body": {"target": "rail:order"}})).json()
            assert not result["ok"] and "no_window" in result["data"]["warning"]
            await client.post("/api/ui/pulse", json={**pulse, "closing": True})
    app.state.store.close()


async def test_embedded_turn_acts_on_its_own_window(tmp_path):
    """The built-in agent's turn carries `uiWindowId`; the tools resolve it from the run like app_call does."""
    app = create_app(tmp_path / "runtime.sqlite3", runtime_state_root=tmp_path, agent_mode="fake", local_token="annotation-test-token")
    window_id = str(uuid4())
    pulse = {"windowId": window_id, "revision": 0, "state": ui_state(), "observation": observation()}
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://runtime", headers=HEADERS) as client:
            assert (await client.post("/api/ui/pulse", json=pulse)).status_code == 200
            tools = with_tools(app)
            session_id = app.state.broker.create_session("local", "native", "local-user").session_id
            tools.proposals = SimpleNamespace(store=SimpleNamespace(get_run=lambda task_id, user_id: {"instruction": encode_instruction("讲讲这段", None, ui_window_id=window_id)}))
            result, _ = await tools.call_async(tool="get_chart_bars", arguments={"range": "recent", "limit": 30}, turn_id="t", mode="local",
                                               session_id=session_id, user_id="local-user", task_id="run-1")
            data = json.loads(result["contentItems"][0]["text"])["data"]
            assert result["success"] and data["windowId"] == window_id and data["barCount"] == 30
            tools.proposals = SimpleNamespace(store=SimpleNamespace(get_run=lambda task_id, user_id: {"instruction": "讲讲这段"}))
            result, _ = await tools.call_async(tool="annotate_chart", arguments={"op": "clear"}, turn_id="t", mode="local",
                                               session_id=session_id, user_id="local-user", task_id="run-2")
            assert not result["success"] and "no_window" in json.loads(result["contentItems"][0]["text"])["warning"]
            await client.post("/api/ui/pulse", json={**pulse, "closing": True})
    app.state.store.close()


async def test_reported_state_is_never_refused_by_the_command_schema(tmp_path):
    """2026-09-18 production incident: a 1.6.5 web client still holding v1 drawings (labels up to 40 chars,
    up to 20 items) got 422 on every heartbeat after the v2 annotation contract shipped, dropped out of
    the window list and could not be commanded. The state is a report, so it must be accepted as-is;
    the command input keeps its strict shape. The new 1.6.6 web client's real pulse shape (preferences,
    the settings context with the three Notification states and an empty shortcut map) is covered too."""
    app = create_app(tmp_path / "runtime.sqlite3", runtime_state_root=tmp_path, agent_mode="fake", local_token="annotation-test-token")
    v1_items = [{"id": f"lvl{i}", "kind": "hline", "price": 15400 + i, "label": "压力位 15420 附近注意回落风险", "tone": "warn"} for i in range(12)]
    legacy = {"windowId": str(uuid4()), "revision": 3, "state": ui_state(chartAnnotations=v1_items,
        highlights=[{"target": "rail:order", "label": "这里下单（说明比较长，超过四十个字符也不应该让整个心跳失败……）", "style": "pulse", "until": "2026-09-18T00:00:06Z"}]),
        "observation": observation()}
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://runtime", headers=HEADERS) as client:
            response = await client.post("/api/ui/pulse", json=legacy)
            assert response.status_code == 200, response.text
            detail = (await client.get(f"/api/app/ui/windows/{legacy['windowId']}")).json()
            assert detail["online"] and len(detail["state"]["chartAnnotations"]) == 12
            # Receipts for the layer still read the reported ids.
            assert changes_match(detail["state"], {"chartAnnotations": {"op": "add", "items": [{"id": "lvl3"}]}})
            # The command input itself stays strict.
            with pytest.raises(ValidationError):
                UiChanges.model_validate({"chartAnnotations": {"op": "set", "items": v1_items[:1]}})
            for permission in ("granted", "denied", "default", "unsupported"):
                current = {"windowId": str(uuid4()), "revision": 0,
                    "state": ui_state(duration=300, settingsPage="notify", preferences={"voice": {"voice": None, "speed": "normal"}, "notify": {"desktop": {"fills": True}, "sound": True}}),
                    "observation": observation({"settings": {"preferencesVersion": 4, "preferencesStatus": None, "shortcuts": {},
                        "notificationPermission": permission}, "highlightTargets": []}),
                    "device": {"deviceId": "web-device-0001", "platform": "web", "version": "1.6.6"}}
                assert (await client.post("/api/ui/pulse", json=current)).status_code == 200, permission


def test_selected_price_receipt_requires_the_actual_limit_ticket():
    from trader_runtime.ui_control import changes_match

    assert not changes_match({"selectedPrice": 824, "ticketPrice": 824, "ticketPriceMode": "opponent"}, {"selectedPrice": 824})
    assert not changes_match({"selectedPrice": 824, "ticketPrice": 823, "ticketPriceMode": "limit"}, {"selectedPrice": 824})


def test_remote_settings_validate_atomic_local_device_changes():
    for value in (None, {}, {"enabled": None}, {"bindings": None}, {"bindings": {"power": "ptt"}},
                  {"bindings": {"unknown": "none"}}, {"diagnostic": True, "enabled": False},
                  {"diagnostic": True, "disconnect": True}, {"disconnect": True, "enabled": True},
                  {"resetBindings": True, "bindings": {"voice": "ptt"}}):
        with pytest.raises(ValidationError):
            UiChanges.model_validate({"remoteSettings": value})
    command = UiChanges.model_validate({"remoteSettings": {"bindings": {"voice": "ptt", "power": "none"}}})
    assert command.remoteSettings.bindings == {"voice": "ptt", "power": "none"}


def test_remote_settings_receipt_requires_observed_default_bindings_and_ready_diagnostic():
    reset = {"remoteSettings": {"resetBindings": True}}
    assert not changes_match(ui_state(), reset)
    assert not changes_match(ui_state(remoteSettings={"bindingsDefault": False}), reset)
    assert changes_match(ui_state(remoteSettings={"bindingsDefault": True}), reset)
    diagnostic = {"remoteSettings": {"diagnostic": True}}
    assert not changes_match(ui_state(remoteSettings={"phase": "idle", "diagnostic": True}), diagnostic)
    assert changes_match(ui_state(remoteSettings={"phase": "ready", "diagnostic": True}), diagnostic)
