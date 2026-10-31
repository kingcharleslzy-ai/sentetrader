"""热路径改用 orjson 的几处数据库 JSON（2026-10-13 第二轮省 CPU）：与标准库读出逐字节相同。

委托链接（request_json / last_report_json）、AI 提案 payload、快讯条目的三个列表列都只装本 Runtime
自己写下的字段；orjson 与标准库唯一的读法差异是超过 64 位的整数（orjson 读成浮点），这些列里没有。
这里对真实流程写下的行逐行对照两种读法，并钉住那条差异，免得以后有人把用户自由填写的 JSON 也换过来。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from test_activity_base import _book
from test_research_live import service
from test_simulation_orders import USER
from trader_runtime import agent_activity, fast_json, research_live, simulation_orders
from trader_runtime.agent_activity import activity_snapshot


def _dump(value) -> str:
    return json.dumps(value, ensure_ascii=False)


async def test_order_links_and_ai_payloads_read_the_same_as_the_stdlib(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, provider = await _book(tmp_path)
    try:
        activity = provider.account_activity()
        fast_orders = _dump(app.state.simulation_orders.list(user_id=USER))
        fast_frame = _dump(activity_snapshot(app, USER, "", activity))
        with monkeypatch.context() as patch:
            patch.setattr(simulation_orders.fast_json, "loads", json.loads)
            patch.setattr(agent_activity.fast_json, "loads", json.loads)
            assert _dump(app.state.simulation_orders.list(user_id=USER)) == fast_orders
            assert _dump(activity_snapshot(app, USER, "", activity)) == fast_frame
        rows = app.state.store.connection.execute(
            "SELECT request_json, last_report_json FROM tqkq_sim_order_links").fetchall()
        assert rows and all(row["last_report_json"] for row in rows)
    finally:
        app.state.store.close()


def test_flash_items_read_the_same_as_the_stdlib(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    svc = service(tmp_path, catalog={"items": [
        {"symbol": "SHFE.cu2612", "isMain": True, "productId": "cu", "instrumentName": "沪铜2612"},
        {"symbol": "SHFE.rb2610", "isMain": True, "productId": "rb", "instrumentName": "螺纹钢2610"},
    ]})
    svc.poll_flash_once()
    fast = _dump(svc.flash_feed())
    assert json.loads(fast)["count"] >= 4
    monkeypatch.setattr(research_live.fast_json, "loads", json.loads)
    assert _dump(svc.flash_feed()) == fast


def test_the_one_difference_is_an_integer_beyond_64_bits() -> None:
    """Why only this Runtime's own columns were switched: a user-written document could hold one."""
    assert fast_json.loads("18446744073709551615") == json.loads("18446744073709551615")
    assert fast_json.loads("1760000000000000000") == 1760000000000000000  # nanosecond times stay exact
    assert isinstance(fast_json.loads("18446744073709551616"), float)
    assert isinstance(json.loads("18446744073709551616"), int)
    assert repr(fast_json.loads("[NaN, 1e400]")) == repr(json.loads("[NaN, 1e400]"))  # falls back
