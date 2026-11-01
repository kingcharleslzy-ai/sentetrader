"""用户画线 (2026-10-02; 画线第二版 2026-10-09): the shape rules and the per-contract cap at the HTTP
route (a real contract from the catalog, finite positive prices, the 200-per-contract cap), the
guest refusal, the replay catalog boundary (2026-10-03: only the replayed contract is drawable),
persistence across a restart, a first-version database read unchanged after the schema rebuild,
every second-version kind's anchors, the style round trip and the lock, and the AI tools going
through the same capability-catalog routes the chart toolbar uses."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from httpx import ASGITransport, AsyncClient

from test_price_alerts import AlertProvider, login
from test_simulation_orders import USER, Gateway, application, quote_now
from trader_runtime.chart_drawings import MAX_DRAWINGS_PER_SYMBOL
from trader_runtime.skill_registry import skill_by_id
from trader_runtime.trader_tools import TOOL_ARGUMENT_MODELS, TraderReadOnlyTools


async def create(client: AsyncClient, **overrides) -> Any:
    body: dict[str, Any] = {"symbol": "SHFE.au2612", "kind": "hline", "points": {"price": 826.0}}
    body.update(overrides)
    return await client.post("/api/market/drawings", json=body)


def detail(response: Any) -> str:
    return str(response.json().get("detail") or "")


def tool_payload(result: dict[str, Any]) -> dict[str, Any]:
    return json.loads(result["contentItems"][0]["text"])


async def test_create_list_update_delete_clear_and_every_creation_refusal(tmp_path: Path) -> None:
    app = application(tmp_path / "drawings-rules.sqlite3", Gateway(), AlertProvider())
    http = await login(app)

    # The refusals are Chinese sentences the UI shows inline. Prices are visual: not on the
    # tick grid is fine (au2612 ticks by 0.02), anything not a finite positive number is not.
    assert "目录里没有" in detail(await create(http, symbol="KQ.m@SHFE.rb"))  # 主连 is not a storable contract
    assert "目录里没有" in detail(await create(http, symbol="SHFE.xx9999"))
    assert "水平线只需要" in detail(await create(http, points={"price": 826.0, "low": 1.0}))
    assert "趋势线需要" in detail(await create(http, kind="trendline", points={"t1": 100, "p1": 820.0}))
    assert "区间需要" in detail(await create(http, kind="zone", points={"low": 820.0}))
    assert "上沿要不低于下沿" in detail(await create(http, kind="zone", points={"low": 830.0, "high": 820.0}))
    assert "价位要是大于 0" in detail(await create(http, points={"price": 0}))
    assert "价位看不懂" in detail(await create(http, points={"price": "高"}))
    bad_label = await create(http, label="一二三四五六七八九十一二三")
    assert bad_label.status_code == 422  # label ≤12 chars (pydantic)
    unknown = await http.post("/api/market/drawings",
                              json={"symbol": "SHFE.au2612", "kind": "fib", "points": {"price": 1}})
    assert unknown.status_code == 422  # kind is a closed set
    extra = await create(http, wat=1)
    assert extra.status_code == 422  # extra=forbid

    hline = await create(http, label="压力")
    assert hline.status_code == 201, hline.text
    made = hline.json()["drawing"]
    assert made["symbol"] == "SHFE.au2612" and made["kind"] == "hline"
    assert made["points"] == {"price": 826.0} and made["label"] == "压力"
    off_grid = await create(http, points={"price": 824.55})
    assert off_grid.status_code == 201  # a visual tool: the tick grid is not enforced
    trendline = await create(http, kind="trendline",
                             points={"t1": 1790000000, "p1": 820.0, "t2": 1790003600, "p2": 828.0})
    assert trendline.status_code == 201 and trendline.json()["drawing"]["points"]["t1"] == 1790000000
    zone = await create(http, kind="zone", points={"low": 820.0, "high": 824.0})
    assert zone.status_code == 201

    listed = (await http.get("/api/market/drawings", params={"symbol": "SHFE.au2612"})).json()
    assert [item["kind"] for item in listed["drawings"]] == ["hline", "hline", "trendline", "zone"]  # drawing order
    everything = (await http.get("/api/market/drawings")).json()
    assert len(everything["drawings"]) == 4

    # Edit: points and/or label; someone else's row is a 404, and so is a repeat delete.
    edited = await http.put(f"/api/market/drawings/{made['id']}",
                            json={"points": {"price": 830.0}, "label": "更强的压力"})
    assert edited.status_code == 200 and edited.json()["drawing"]["points"] == {"price": 830.0}
    # 无效参数改线与创建同一条 422 中文原因，不是 500（#521 R4）。
    bad_points = await http.put(f"/api/market/drawings/{made['id']}", json={"points": {"price": "高"}})
    assert bad_points.status_code == 422 and "价位看不懂" in detail(bad_points)
    bad_shape = await http.put(f"/api/market/drawings/{made['id']}", json={"points": {"low": 820.0, "high": 824.0}})
    assert bad_shape.status_code == 422 and "水平线只需要" in detail(bad_shape)
    stranger = app.state.store.create_chart_drawing(
        user_id="someone-else", symbol="SHFE.au2612", kind="hline", points={"price": 9999.0},
        label="", max_per_symbol=30)
    assert (await http.put(f"/api/market/drawings/{stranger['id']}", json={"label": "x"})).status_code == 404
    assert (await http.delete(f"/api/market/drawings/{stranger['id']}")).status_code == 404
    deleted = await http.delete(f"/api/market/drawings/{made['id']}")
    assert deleted.status_code == 200 and deleted.json()["drawing"]["id"] == made["id"]
    assert (await http.delete(f"/api/market/drawings/{made['id']}")).status_code == 404

    # 清空本合约 clears the user's own rows on that contract only, and reports how many went.
    other = await create(http, symbol="SHFE.hc2610", points={"price": 3300.0})
    assert other.status_code == 201
    cleared = await http.delete("/api/market/drawings", params={"symbol": "SHFE.au2612"})
    assert cleared.status_code == 200 and cleared.json()["cleared"] == 3
    assert (await http.delete("/api/market/drawings", params={"symbol": "SHFE.au2612"})).json()["cleared"] == 0
    assert len((await http.get("/api/market/drawings")).json()["drawings"]) == 1  # the other contract stays

    # The cap is a count per user × contract (200 since 画线第二版): the 201st line is refused until
    # one is removed. The store fills the first 199 (the route's own rules are covered above).
    for index in range(MAX_DRAWINGS_PER_SYMBOL - 1):
        app.state.store.create_chart_drawing(user_id=USER, symbol="SHFE.au2612", kind="hline",
                                             points={"price": 700.0 + index}, label="", style={},
                                             max_per_symbol=MAX_DRAWINGS_PER_SYMBOL)
    assert (await create(http, points={"price": 899.0})).status_code == 201
    capped = await create(http, points={"price": 900.0})
    assert capped.status_code == 422 and "上限" in detail(capped)
    other_contract = await create(http, symbol="SHFE.hc2610", points={"price": 3301.0})
    assert other_contract.status_code == 201  # the cap is per contract
    holding = (await http.get("/api/market/drawings", params={"symbol": "SHFE.au2612"})).json()["drawings"]
    await http.delete(f"/api/market/drawings/{holding[0]['id']}")
    assert (await create(http, points={"price": 824.5})).status_code == 201
    await http.aclose()
    app.state.store.close()


async def test_guest_windows_have_no_drawings_and_replay_draws_its_own_contract(tmp_path: Path) -> None:
    app = application(tmp_path / "drawings-guest.sqlite3", Gateway(), AlertProvider())
    guest = AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                        headers={"Authorization": "Bearer guest.looker", "Origin": "http://127.0.0.1:5173"})
    listed = await guest.get("/api/market/drawings")
    assert listed.status_code == 403 and listed.json()["detail"] == "guest_drawings_unavailable"
    # Writes never reach the route as a guest (the same middleware that guards the alerts).
    made = await guest.post("/api/market/drawings",
                            json={"symbol": "SHFE.au2612", "kind": "hline", "points": {"price": 1.0}})
    assert made.status_code == 401 and made.json()["detail"] == "local_auth_required"
    await guest.aclose()

    http = await login(app)
    # A replay window's catalog is the replay's own (2026-10-03): only the replayed contract is
    # drawable; the store isolation of the real replay child lives in test_replay_alerts.py.
    app.state.replay_context = SimpleNamespace(
        provider=SimpleNamespace(instrument_catalog=lambda: {"items": [{"symbol": "SHFE.au2612"}]}))
    try:
        assert (await http.get("/api/market/drawings")).status_code == 200  # 回放里列表照常（子进程自己的库）
        refused = await create(http, symbol="SHFE.hc2610", points={"price": 3300.0})
        assert refused.status_code == 422
        assert refused.json()["detail"] == "回放里只能画本次回放的合约；要画其他合约，回主窗口"
        assert (await create(http, points={"price": 826.0})).status_code == 201
        clearing = await http.delete("/api/market/drawings", params={"symbol": "SHFE.au2612"})
        assert clearing.status_code == 200
    finally:
        del app.state.replay_context
    await http.aclose()
    app.state.store.close()


async def test_drawings_survive_a_restart_of_the_runtime(tmp_path: Path) -> None:
    database = tmp_path / "drawings-restart.sqlite3"
    first = application(database, Gateway(), AlertProvider())
    http = await login(first)
    made = (await create(http, kind="zone", points={"low": 820.0, "high": 824.0}, label="震荡区")).json()["drawing"]
    await http.aclose()
    first.state.store.close()

    second = application(database, Gateway(), AlertProvider())
    again = await login(second)
    listed = (await again.get("/api/market/drawings", params={"symbol": "SHFE.au2612"})).json()["drawings"]
    assert [item["id"] for item in listed] == [made["id"]]
    assert listed[0]["points"] == {"low": 820.0, "high": 824.0} and listed[0]["label"] == "震荡区"
    await again.aclose()
    second.state.store.close()


async def test_the_three_ai_tools_use_the_same_catalog_routes(tmp_path: Path) -> None:
    app = application(tmp_path / "drawings-tools.sqlite3", Gateway(), AlertProvider())
    app.state.data_provider.quote_datetime = quote_now()
    app.state.data_provider.last = 824.0
    http = await login(app)
    session = (await http.post("/api/sessions", json={"mode": "local", "agentId": "native"})).json()
    tools = TraderReadOnlyTools(app.state.data_provider, app.state.adapter.runtime_status)
    tools.app_capabilities = app.state.app_capabilities

    async def invoke(tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        result, _ = await tools.call_async(tool=tool, arguments=arguments, turn_id="drawings-tools",
                                           mode="local", session_id=session["sessionId"], user_id=USER)
        return result

    made = await invoke("create_chart_drawing",
                        {"symbol": "沪金2612", "kind": "hline", "points": {"price": 826.0}, "label": "压力"})
    assert made["success"]
    body = tool_payload(made)
    assert body["readOnly"] is False
    drawing = body["data"]["drawing"]
    assert drawing["symbol"] == "SHFE.au2612"  # the spoken name resolved to the contract
    assert body["data"]["summary"] == "SHFE.au2612 826 一条水平线（压力），现价 824，在 826 下方 2"
    zone = await invoke("create_chart_drawing",
                        {"symbol": "SHFE.au2612", "kind": "zone", "points": {"low": 820.0, "high": 822.0}})
    assert zone["success"]

    listed = await invoke("list_chart_drawings", {"symbol": "SHFE.au2612"})
    assert listed["success"]
    payload = tool_payload(listed)
    assert payload["readOnly"] is True
    assert payload["data"]["lines"] == [
        "SHFE.au2612 826 一条水平线（压力），现价 824，在 826 下方 2",
        "SHFE.au2612 820–822 的区间，现价 824，在 822 上方 2",
    ]

    deleted = await invoke("delete_chart_drawing", {"drawingId": drawing["id"]})
    assert deleted["success"]
    remaining = tool_payload(await invoke("list_chart_drawings", {}))["data"]["lines"]
    assert len(remaining) == 1 and "820–822" in remaining[0]

    refused = await invoke("create_chart_drawing",
                           {"symbol": "SHFE.au2612", "kind": "hline", "points": {"low": 1.0}})
    assert not refused["success"] and "水平线只需要" in tool_payload(refused)["warning"]
    await http.aclose()
    app.state.store.close()


def test_the_chart_drawings_skill_claims_exactly_its_tools_and_routes() -> None:
    skill = skill_by_id("chart-drawings")
    assert skill is not None and skill.category == "行情"
    assert set(skill.tools) == {"list_chart_drawings", "create_chart_drawing", "update_chart_drawing",
                                "delete_chart_drawing", "clear_chart_drawings"}
    for name in skill.tools:
        assert name in TOOL_ARGUMENT_MODELS
    assert {"GET /api/market/drawings", "POST /api/market/drawings", "PUT /api/market/drawings/{drawing_id}",
            "DELETE /api/market/drawings/{drawing_id}", "DELETE /api/market/drawings"} <= set(skill.routes)


async def test_every_second_version_kind_stores_its_anchors_and_refuses_a_wrong_count(tmp_path: Path) -> None:
    app = application(tmp_path / "drawings-kinds.sqlite3", Gateway(), AlertProvider())
    http = await login(app)
    a, b, c = {"t": 1790000000, "p": 820.0}, {"t": 1790003600, "p": 828.0}, {"t": 1790007200, "p": 824.0}
    counts = {"ray": [a, b], "extended": [a, b], "hray": [a], "channel": [a, b, c], "polyline": [a, b, c],
              "rect_zone": [a, b], "fib": [a, b], "percent": [a, b], "gann": [a, b], "rect": [a, b],
              "ellipse": [a, b], "triangle": [a, b, c], "arrow": [a, b], "price_label": [a], "measure": [a, b]}
    for kind, anchors in counts.items():
        made = await create(http, kind=kind, points={"anchors": anchors})
        assert made.status_code == 201, (kind, made.text)
        assert made.json()["drawing"]["points"] == {"anchors": anchors}, kind
        wrong = [a] if kind == "polyline" else [*anchors, a]   # one point too few / one too many
        refused = await create(http, kind=kind, points={"anchors": wrong})
        assert refused.status_code == 422 and "anchors" in detail(refused), kind
    # 垂直线 needs only the time; 文字 carries its words (≤60) in the label.
    vline = await create(http, kind="vline", points={"anchors": [{"t": 1790000000}]})
    assert vline.json()["drawing"]["points"] == {"anchors": [{"t": 1790000000}]}
    words = "突破后回踩确认再进场，止损放在前低下方两跳"
    assert (await create(http, kind="text", points={"anchors": [a]}, label=words)).json()["drawing"]["label"] == words
    too_long = await create(http, kind="hline", points={"price": 826.0}, label="一二三四五六七八九十一二三")
    assert too_long.status_code == 422 and "标签最多 12" in detail(too_long)
    # fib levels: sorted, unique, within ±5; a first-version kind written as anchors keeps its own shape.
    fib = await create(http, kind="fib", points={"anchors": [a, b], "levels": [1, 0.618, 0, 1.618]})
    assert fib.json()["drawing"]["points"]["levels"] == [0.0, 0.618, 1.0, 1.618]
    assert "不能重复" in detail(await create(http, kind="fib", points={"anchors": [a, b], "levels": [0.5, 0.5]}))
    assert (await create(http, kind="hline", points={"anchors": [a]})).json()["drawing"]["points"] == {"price": 820.0}
    assert (await create(http, kind="zone", points={"anchors": [b, a]})).json()["drawing"]["points"] == {
        "low": 820.0, "high": 828.0}
    assert (await create(http, kind="trendline", points={"anchors": [a, b]})).json()["drawing"]["points"] == {
        "t1": 1790000000, "p1": 820.0, "t2": 1790003600, "p2": 828.0}
    assert "价格要是大于 0" in detail(await create(http, kind="ray", points={"anchors": [a, {"t": 1, "p": -1}]}))
    await http.aclose()
    app.state.store.close()


async def test_style_round_trips_merges_and_a_locked_line_does_not_move(tmp_path: Path) -> None:
    app = application(tmp_path / "drawings-style.sqlite3", Gateway(), AlertProvider())
    http = await login(app)
    style = {"color": "#FF4A3D", "width": 3, "dash": "dashed", "extendLeft": False, "extendRight": True,
             "showPrice": True, "locked": False, "hidden": False, "allPeriods": False, "period": 300}
    made = (await create(http, kind="trendline", points={"t1": 1, "p1": 820.0, "t2": 2, "p2": 828.0},
                         style=style)).json()["drawing"]
    assert made["style"] == {**style, "color": "#ff4a3d"}
    listed = (await http.get("/api/market/drawings", params={"symbol": "SHFE.au2612"})).json()["drawings"]
    assert listed[0]["style"] == made["style"]
    # A patch changes the keys it names; null returns one to the kind's own default.
    patched = await http.put(f"/api/market/drawings/{made['id']}",
                             json={"style": {"color": "green", "dash": None, "locked": True}})
    assert patched.status_code == 200
    stored = patched.json()["drawing"]["style"]
    assert stored["color"] == "green" and stored["locked"] is True and "dash" not in stored and stored["width"] == 3
    for bad, reason in (({"width": 5}, "粗细"), ({"dash": "wavy"}, "线型"), ({"color": "pink"}, "颜色"),
                        ({"period": 7}, "period"), ({"glow": True}, "样式里没有"), ({"locked": "yes"}, "true 或 false")):
        refused = await http.put(f"/api/market/drawings/{made['id']}", json={"style": bad})
        assert refused.status_code == 422 and reason in detail(refused), bad
    # Locked: moving is refused, the label and the style still change; unlocking in the same write moves it.
    moved = await http.put(f"/api/market/drawings/{made['id']}",
                           json={"points": {"t1": 1, "p1": 821.0, "t2": 2, "p2": 829.0}})
    assert moved.status_code == 409 and detail(moved) == "这条线已锁定，先解锁再移动"
    assert (await http.put(f"/api/market/drawings/{made['id']}", json={"label": "通道上沿"})).status_code == 200
    unlocked = await http.put(f"/api/market/drawings/{made['id']}",
                              json={"points": {"t1": 1, "p1": 821.0, "t2": 2, "p2": 829.0}, "style": {"locked": False}})
    assert unlocked.status_code == 200 and unlocked.json()["drawing"]["points"]["p1"] == 821.0
    assert unlocked.json()["drawing"]["label"] == "通道上沿"
    await http.aclose()
    app.state.store.close()


async def test_period_only_style_requires_the_merged_period_and_keeps_legacy_reading(tmp_path: Path) -> None:
    app = application(tmp_path / "drawings-period.sqlite3", Gateway(), AlertProvider())
    http = await login(app)
    legacy = (await create(http)).json()["drawing"]
    assert legacy["style"] == {}
    refused_create = await create(http, style={"allPeriods": False})
    assert refused_create.status_code == 422 and "period" in detail(refused_create)
    refused_update = await http.put(f"/api/market/drawings/{legacy['id']}", json={"style": {"allPeriods": False}})
    assert refused_update.status_code == 422 and "period" in detail(refused_update)
    # Older APIs accepted this shape. GET retains it; any new write must supply the missing period.
    app.state.store.update_chart_drawing(user_id=USER, drawing_id=legacy["id"], style={"allPeriods": False})
    historical = (await http.get("/api/market/drawings")).json()["drawings"][0]
    assert historical["style"] == {"allPeriods": False}
    refused_move = await http.put(f"/api/market/drawings/{legacy['id']}", json={"points": {"price": 830}})
    assert refused_move.status_code == 422 and "period" in detail(refused_move)
    for period in (0, 300, 900):
        pinned = await http.put(f"/api/market/drawings/{legacy['id']}", json={"style": {"allPeriods": False, "period": period}})
        assert pinned.status_code == 200 and pinned.json()["drawing"]["style"]["period"] == period
        # An unrelated style patch merges with, and retains, the already stored period.
        assert (await http.put(f"/api/market/drawings/{legacy['id']}", json={"style": {"color": "red"}})).status_code == 200
        refused = await http.put(f"/api/market/drawings/{legacy['id']}", json={"style": {"period": None}})
        assert refused.status_code == 422 and "period" in detail(refused)
        listed = (await http.get("/api/market/drawings")).json()["drawings"]
        assert listed[0]["style"]["period"] == period and listed[0]["points"] == legacy["points"]
    # Returning to all periods can remove the period; no historical rows are migrated.
    unpinned = await http.put(f"/api/market/drawings/{legacy['id']}", json={"style": {"allPeriods": True, "period": None}})
    assert unpinned.status_code == 200 and "period" not in unpinned.json()["drawing"]["style"]
    await http.aclose()
    app.state.store.close()


async def test_the_change_and_clear_tools_go_through_the_routes_and_receipts_name_the_levels(tmp_path: Path) -> None:
    app = application(tmp_path / "drawings-more-tools.sqlite3", Gateway(), AlertProvider())
    app.state.data_provider.quote_datetime = quote_now()
    app.state.data_provider.last = 824.0
    http = await login(app)
    session = (await http.post("/api/sessions", json={"mode": "local", "agentId": "native"})).json()
    tools = TraderReadOnlyTools(app.state.data_provider, app.state.adapter.runtime_status)
    tools.app_capabilities = app.state.app_capabilities

    async def invoke(tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        result, _ = await tools.call_async(tool=tool, arguments=arguments, turn_id="drawings-tools",
                                           mode="local", session_id=session["sessionId"], user_id=USER)
        return tool_payload(result)

    fib = await invoke("create_chart_drawing", {"symbol": "SHFE.au2612", "kind": "fib",
                                                "points": {"anchors": [{"t": 1, "p": 800.0}, {"t": 2, "p": 840.0}]},
                                                "style": {"color": "amber"}})
    assert fib["data"]["drawing"]["style"] == {"color": "amber"}
    assert "黄金分割" in fib["data"]["summary"] and "61.8% 815.28" in fib["data"]["summary"]
    changed = await invoke("update_chart_drawing", {"drawingId": fib["data"]["drawing"]["id"],
                                                    "style": {"width": 2, "hidden": True}})
    assert changed["data"]["drawing"]["style"] == {"color": "amber", "width": 2, "hidden": True}
    assert "已隐藏" in changed["data"]["summary"]
    empty = await invoke("update_chart_drawing", {"drawingId": fib["data"]["drawing"]["id"]})
    assert "没有要改的内容" in empty["warning"]
    await invoke("create_chart_drawing", {"symbol": "沪金2612", "kind": "hline", "points": {"price": 826.0}})
    cleared = await invoke("clear_chart_drawings", {"symbol": "沪金2612"})
    assert cleared["data"] == {"cleared": 2, "summary": "清空了这个合约的 2 条画线"}
    assert (await invoke("clear_chart_drawings", {"symbol": "SHFE.au2612"}))["data"]["summary"] == "这个合约没有画过线"
    await http.aclose()
    app.state.store.close()


async def test_points_only_update_does_not_rewrite_style_from_its_validation_read(tmp_path: Path, monkeypatch) -> None:
    app = application(tmp_path / "drawings-independent-style.sqlite3", Gateway(), AlertProvider())
    http = await login(app)
    made = (await create(http, style={"color": "red", "allPeriods": False, "period": 300})).json()["drawing"]
    # Independent SQLite writer between the route's validation read and update, not two async
    # HTTP calls interleaving in the same event loop (there is no await between those operations).
    from trader_runtime.store import RuntimeStore
    other = RuntimeStore(tmp_path / "drawings-independent-style.sqlite3")
    original = app.state.store.get_chart_drawing

    def read_then_other_style(*args, **kwargs):
        drawing = original(*args, **kwargs)
        other.update_chart_drawing(user_id=USER, drawing_id=made["id"],
                                  style={"color": "blue", "allPeriods": False, "period": 300})
        return drawing

    monkeypatch.setattr(app.state.store, "get_chart_drawing", read_then_other_style)
    updated = await http.put(f"/api/market/drawings/{made['id']}", json={"points": {"price": 830}, "label": "新价"})
    assert updated.status_code == 200
    assert updated.json()["drawing"]["style"]["color"] == "blue"
    assert updated.json()["drawing"]["points"]["price"] == 830
    other.close()
    await http.aclose()
    app.state.store.close()
