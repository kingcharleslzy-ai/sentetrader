"""AI 指令栏 (改版第二步, owner 2026-11-01): what one of the AI's tool calls changed on the user's screen or
account, in one line, and how to take it back. The line rides the turn's task step (`step.action`), so the
command bar can list "AI 做了 N 件事" with 撤销 — the undo is the same authenticated business call the UI makes
(the operation's DELETE), never a hidden interface. Reads, orders and cards are not actions here: an order only
ever comes as a confirmation card the user decides on."""
from __future__ import annotations

import json
from typing import Any

from .chart_drawings import KIND_WORDS
from .price_alerts import alert_line
from .ui_control import UI_COMMAND_OPERATION, describe_ui_change


def step_action(tool: str, arguments: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any] | None:
    """`{text, undo}` for a successful app_call that created a price alert or a drawing, or applied an interface
    command; None for anything else. `payload` is the tool's result object ({success, data: {ok, data, …}})."""
    if tool != "app_call":
        return None
    try:
        request = json.loads(arguments.get("requestJson") or "{}")
    except (TypeError, ValueError):
        return None
    if not isinstance(request, dict):
        return None
    data = payload.get("data")
    result: dict[str, Any] = data if isinstance(data, dict) else {}
    if not result.get("ok"):
        return None
    body: dict[str, Any] = result["data"] if isinstance(result.get("data"), dict) else {}
    operation = request.get("operation")
    if operation == "POST /api/market/alerts" and isinstance(body.get("alert"), dict) and body["alert"].get("id"):
        alert = body["alert"]
        return {"text": "建了到价提醒：" + alert_line(alert),
                "undo": {"method": "DELETE", "path": f"/api/market/alerts/{alert['id']}"}}
    if operation == "POST /api/market/drawings" and isinstance(body.get("drawing"), dict) and body["drawing"].get("id"):
        drawing = body["drawing"]
        label = f"「{drawing['label']}」" if drawing.get("label") else ""
        return {"text": f"在 {drawing.get('symbol') or '图'} 上画了{KIND_WORDS.get(str(drawing.get('kind')), '一条线')}{label}",
                "undo": {"method": "DELETE", "path": f"/api/market/drawings/{drawing['id']}"}}
    if operation == UI_COMMAND_OPERATION:
        changes = (request.get("body") or {}).get("changes") if isinstance(request.get("body"), dict) else None
        if isinstance(changes, dict) and changes:
            # Moving the screen is undone by moving it back: listed, no undo.
            return {"text": "；".join(describe_ui_change({key: value}) for key, value in changes.items()), "undo": None}
    return None


assert step_action("app_call", {"requestJson": json.dumps({"operation": "POST /api/market/drawings"})},
                   {"data": {"ok": True, "data": {"drawing": {"id": "d1", "symbol": "SHFE.rb2701", "kind": "hline", "label": ""}}}}
                   ) == {"text": "在 SHFE.rb2701 上画了" + KIND_WORDS["hline"],
                         "undo": {"method": "DELETE", "path": "/api/market/drawings/d1"}}
assert step_action("app_call", {"requestJson": json.dumps({"operation": "GET /api/market/alerts"})},
                   {"data": {"ok": True, "data": {}}}) is None
