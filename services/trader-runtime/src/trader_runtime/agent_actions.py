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

# The agent's own tools for these (what the model actually picks, 2026-11-01 live check) call the same route and
# hand its JSON back as the result's `data`; app_call wraps it once more ({ok, data}).
DEDICATED_TOOLS = {"create_price_alert": "POST /api/market/alerts", "create_chart_drawing": "POST /api/market/drawings",
                   "create_strategy_draft": "POST /api/strategy-plans",
                   "create_strategy_from_template": "POST /api/strategy-templates/{template_id}/instantiate"}
# A strategy or 条件提醒 the AI saves is a draft (it never starts one): taken back through 我的策略's own delete, which
# refuses while it monitors or has orders / positions — the refusal shows beside 撤销.
STRATEGY_SAVES = {"POST /api/strategy-plans", "POST /api/strategy-templates/{template_id}/instantiate"}


def step_action(tool: str, arguments: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any] | None:
    """`{text, undo}` for a successful tool call that created a price alert, a drawing or a strategy / 条件提醒 draft
    (its own tool or app_call), or an app_call that applied an interface command; None for anything else. `payload` is the tool's result
    object ({success, data, …}); the caller passes only calls that succeeded."""
    data = payload.get("data")
    result: dict[str, Any] = data if isinstance(data, dict) else {}
    request: dict[str, Any] = {}
    if tool in DEDICATED_TOOLS:
        operation, body = DEDICATED_TOOLS[tool], result
    elif tool == "app_call":
        try:
            parsed = json.loads(arguments.get("requestJson") or "{}")
        except (TypeError, ValueError):
            return None
        if not isinstance(parsed, dict) or not result.get("ok"):
            return None
        request = parsed
        operation = str(request.get("operation") or "")
        body = result["data"] if isinstance(result.get("data"), dict) else {}
    else:
        return None
    if operation == "POST /api/market/alerts" and isinstance(body.get("alert"), dict) and body["alert"].get("id"):
        alert = body["alert"]
        return {"text": "建了到价提醒：" + alert_line(alert),
                "undo": {"method": "DELETE", "path": f"/api/market/alerts/{alert['id']}"}}
    if operation == "POST /api/market/drawings" and isinstance(body.get("drawing"), dict) and body["drawing"].get("id"):
        drawing = body["drawing"]
        label = f"「{drawing['label']}」" if drawing.get("label") else ""
        return {"text": f"在 {drawing.get('symbol') or '图'} 上画了{KIND_WORDS.get(str(drawing.get('kind')), '一条线')}{label}",
                "undo": {"method": "DELETE", "path": f"/api/market/drawings/{drawing['id']}"}}
    if operation in STRATEGY_SAVES and body.get("strategyId"):
        plan = (body.get("definition") or {}).get("plan") if isinstance(body.get("definition"), dict) else None
        reminder = bool(body.get("reminderOnly") or (isinstance(plan, dict) and plan.get("reminderOnly")))
        what = f"存了提醒「{body.get('name') or ''}」（还没开始提醒）" if reminder else f"存了策略草稿「{body.get('name') or ''}」（没上线）"
        return {"text": what, "undo": {"method": "DELETE", "path": f"/api/strategy-plans/{body['strategyId']}"}}
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
assert (step_action("create_price_alert", {"symbol": "螺纹", "price": 3036},
                    {"data": {"alert": {"id": "a1", "symbol": "SHFE.rb2701", "price": 3036, "direction": "up"}}}
                    ) or {}).get("undo") == {"method": "DELETE", "path": "/api/market/alerts/a1"}
assert step_action("create_strategy_draft", {}, {"data": {"strategyId": "s1", "name": "突破提醒", "reminderOnly": True}}) == {
    "text": "存了提醒「突破提醒」（还没开始提醒）", "undo": {"method": "DELETE", "path": "/api/strategy-plans/s1"}}
