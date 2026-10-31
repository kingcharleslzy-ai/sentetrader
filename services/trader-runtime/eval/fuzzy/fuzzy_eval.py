"""Fuzzy-instruction evaluation against the real local Runtime (native agent + deepseek model, fake
data, no simulation gateway so fixture quotes can form cards), in typed and voice modes. Each turn is
scored by its observable effects: deterministic route (typed), tool steps, proposal cards, UI commands
on our own window, preference document, reply text. Orders stop at the card; every card is rejected
afterwards. 2026-09-20: typed turns run through turn_router.classify_turn — a direct ui_intent turn
has no model round at all (effects are window commands + one assistant line), and routed orders /
market reads carry a fixed narrowed tool set at temperature 0.

Launch (Mac, another terminal; the fake data provider supplies the catalog the router matches):

  cd services/trader-runtime
  python - <<'PY'
  import uvicorn
  from pathlib import Path
  from trader_runtime.main import create_app
  uvicorn.run(create_app(database_path=Path("/tmp/fuzzy-eval.db"), local_token="voice-v0-local-token",
                         agent_mode="native", agent_provider="deepseek",
                         deepseek_api_key=__import__("os").environ["DEEPSEEK_API_KEY"]),
              host="127.0.0.1", port=8010)
  PY
  rm -f /tmp/fuzzy-eval.db   # first run only, or between full passes
  uv run --project . python eval/fuzzy/fuzzy_eval.py

Env: RUNTIME (http://127.0.0.1:8010), TOKEN, DB (sqlite path of the Runtime above), RUNS (2),
ONLY (comma ids), MODES (typed,voice)."""
import json
import os
import re
import sqlite3
import sys
import threading
import time
import uuid
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from testset import T  # noqa: E402

from trader_runtime.turn_router import classify_turn  # noqa: E402
from trader_runtime.user_preferences import UserPreferences  # noqa: E402
from trader_runtime.voice_intents import match_intent  # noqa: E402
from trader_runtime.voice_realtime import route_utterance  # noqa: E402

RUNTIME = os.getenv("RUNTIME", "http://127.0.0.1:8010")
# Discovery / status reads the loop makes before acting, and the screen read (app_call on "这个/这波" turns,
# autoReadScreen): reported, never counted as an extra tool. UI effects are scored separately. The skill
# registry's own disclosure tools (activate_skill / read_skill_resource) read static SKILL.md content and
# write no business state — same exemption as app_capabilities, the manual they replace. get_preferences
# reads the caller's own settings document: discovery before answering, never an ownership signal.
PRECHECK = {"app_capabilities", "get_runtime_data_status", "app_call", "activate_skill", "read_skill_resource",
            "get_preferences"}
TOKEN = os.getenv("TOKEN", "voice-v0-local-token")
DB = os.getenv("DB")
RUNS = int(os.getenv("RUNS", "2"))
ONLY = set(filter(None, os.getenv("ONLY", "").split(",")))
MODES = os.getenv("MODES", "typed,voice").split(",")
OUT = Path(__file__).parent
HDR = {"Authorization": f"Bearer {TOKEN}", "Origin": "http://127.0.0.1:5173"}  # a default allowed origin
client = httpx.Client(base_url=RUNTIME, headers=HDR, timeout=150)

# The product-name catalog the Runtime's own router matches against (GET /api/instruments), so the
# route reported here is the same decision the Runtime made for the turn.
CATALOG: list[str] | None = None
def catalog_names() -> list[str] | None:
    global CATALOG
    if CATALOG is None:
        names = []
        for item in (client.get("/api/instruments").json().get("items") or []):
            name = re.sub(r"\d+$", "", str(item.get("instrumentName") or "")).strip()
            if name and re.search(r"[一-龥]", name) and name not in names:
                names.append(name)
            # productId codes, same as the Runtime's router catalog ("IF现在多少钱").
            code = str(item.get("productId") or "").strip().upper()
            if re.fullmatch(r"[A-Z]{1,4}", code) and code not in names:
                names.append(code)
        CATALOG = names or None
    return CATALOG

# ---- a minimal UI window of our own (device 27c0f0ca-fuzzy): applies every command it understands ----
WINDOW = str(uuid.uuid4())
def base_state():
    return {"symbol": "SHFE.au2612", "duration": 60, "view": "trade", "panel": None, "settingsOpen": False, "theme": "light",
            "dockSide": "right", "dockWidth": 400, "replay": False, "settingsPage": "account", "fontScale": "standard", "density": "standard",
            "chart": {k: True for k in ("kd", "macd", "volume", "crosshair", "lastPriceLine", "ma5", "ma10", "ma25", "ma60", "ma200")},
            "chartSizes": {"main": 58, "kd": 19, "macd": 23}, "strategy": None, "positionsBar": "collapsed", "aiPage": "monitor",
            "positionsTab": "positions", "chartFocus": None, "chartAnnotations": [{"id": "old", "kind": "hline", "price": 820, "label": "旧线"}],
            "highlights": [], "disclosures": {}, "dialog": None, "chatSessionId": None, "tour": {"active": False, "step": 0, "total": 0, "current": None},
            "preferences": None}
pulse = {"windowId": WINDOW, "revision": 0, "state": base_state(),
         "device": {"deviceId": "27c0f0ca-fuzzy", "platform": "macos", "version": "fuzzy", "nativeCapture": False},
         "observation": {"capturedAt": "2026-09-19T00:00:00Z", "viewport": {"width": 1280, "height": 720}, "text": "交易台", "truncated": False,
                         "regions": [], "controls": [], "context": {"chartViewport": {"timeRange": {"from": 1788192000, "to": 1788195600}, "logicalRange": {"from": 0, "to": 60}}, "highlightTargets": ["rail:order", "panel:ai"]}}}
applied: list[dict] = []
lock = threading.Lock()

def apply_command(changes: dict) -> bool:
    st = pulse["state"]
    for k, v in changes.items():
        if k == "presentation":
            for kk, vv in v.items():
                if kk != "reset": st[kk] = vv
        elif k == "chartAnnotations":
            st["chartAnnotations"] = [] if v.get("op") == "clear" else ([*(st["chartAnnotations"] if v.get("op") == "add" else []), *(v.get("items") or [])])
        elif k == "chartOption": st["chart"][v["name"]] = v["visible"]
        elif k == "dialog": st["dialog"] = v["name"] if v.get("open", True) else None
        elif k == "disclosure": st["disclosures"][v["name"]] = v["open"]
        elif k == "chatSession": st["chatSessionId"] = v.get("sessionId") or str(uuid.uuid4())
        elif k == "highlight": st["highlights"] = [] if v.get("clear") else [{"target": v["target"], "label": v.get("label"), "style": v.get("style", "pulse"), "until": "2026-09-19T00:00:10Z"}]
        elif k == "tour":
            if v["op"] == "start": st["tour"] = {"active": True, "step": 1, "total": len(v["steps"]), "current": v["steps"][0]}
            elif v["op"] == "stop": st["tour"] = {"active": False, "step": 0, "total": 0, "current": None}
            elif v["op"] == "next": st["tour"] = {**st["tour"], "step": min(st["tour"]["step"] + 1, st["tour"]["total"])}
            elif v["op"] == "prev": st["tour"] = {**st["tour"], "step": max(1, st["tour"]["step"] - 1)}
        elif k in st: st[k] = v
        else: return False
    return True

def beat():
    with httpx.Client(base_url=RUNTIME, headers=HDR, timeout=10) as c:
        while True:
            try:
                with lock:
                    r = c.post("/api/ui/pulse", json=pulse).json()
                    pulse.pop("receipt", None)
                    cmd = r.get("command")
                    if cmd:
                        ok = apply_command(cmd.get("changes") or {})
                        pulse["revision"] += 1
                        pulse["receipt"] = {"commandId": cmd["commandId"], "status": "applied" if ok else "rejected"}
                        applied.append({"t": time.time(), "changes": cmd.get("changes"), "ok": ok})
            except Exception:
                pass
            time.sleep(0.3)

def reset_window():
    with lock:
        pulse["state"] = base_state(); pulse["revision"] += 1

# ---- preferences ----
def get_prefs(): return client.get("/api/preferences").json()
def reset_prefs():
    client.put("/api/preferences", json=UserPreferences().model_dump(mode="json"))
def leaf(d, path):
    for p in path.split("."):
        d = (d or {}).get(p)
    return d

# ---- events from the store (read-only) ----
def events_after(session_id, seq):
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True); con.row_factory = sqlite3.Row
    rows = con.execute("SELECT sequence, kind, name, timestamp, payload_json FROM events WHERE session_id=? AND sequence>? ORDER BY sequence", (session_id, seq)).fetchall()
    con.close()
    return [{"seq": r["sequence"], "kind": r["kind"], "name": r["name"], "ts": r["timestamp"], "payload": json.loads(r["payload_json"])} for r in rows]
def last_seq(session_id):
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    v = con.execute("SELECT MAX(sequence) FROM events WHERE session_id=?", (session_id,)).fetchone()[0]; con.close()
    return v or 0
def ts(s):
    from datetime import datetime
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()

def chat_send(session_id, text, origin=None):
    body = {"text": text, "mode": "local", "sessionId": session_id, "uiWindowId": WINDOW, "timeoutSeconds": 120}
    if origin: body["origin"] = origin
    r = client.post("/api/agent/call", json={"operation": "chat:send", "body": body})
    return r.json()

def new_session():
    return client.post("/api/sessions", params={"chat": "true"}, json={"mode": "local"}).json()["sessionId"]

def pending_cards(evs):
    cards = []
    for e in evs:
        if e["name"].startswith("proposal.") and e["payload"].get("status") == "approval_required":
            cards.append({"type": e["name"].split(".", 1)[1], **{k: e["payload"].get(k) for k in ("proposalId", "symbol", "side", "volume", "priceIntent", "limitPrice", "direction", "symbols", "summary", "orders")}})
    seen = {}
    for c in cards: seen[c["proposalId"]] = c
    return list(seen.values())

def reject_cards(cards):
    for c in cards:
        try: client.post(f"/api/proposals/{c['proposalId']}/reject", json={})
        except Exception: pass

def run_turn(session_id, text, origin):
    seq = last_seq(session_id)
    t0 = time.time()
    result = chat_send(session_id, text, origin)
    evs = events_after(session_id, seq)
    data = result.get("data") or {}
    steps = [s for s in (data.get("toolSteps") or []) if s.get("tool")]
    tools = [s["tool"] for s in steps]
    user_ts = next((ts(e["ts"]) for e in evs if e["name"] == "message.completed" and e["payload"].get("role") == "user"), t0)
    first_delta = next((ts(e["ts"]) for e in evs if e["name"] == "message.delta" and e["payload"].get("role") == "assistant" and (e["payload"].get("text") or "").strip() not in {"", "正在处理这个请求…"}), None)
    done_ts = next((ts(e["ts"]) for e in evs if e["name"] == "task.completed"), time.time())
    return {"reply": data.get("reply") or "", "spoken": data.get("spoken"), "tools": tools, "runStatus": data.get("runStatus"), "warnings": data.get("warnings"),
            "cards": pending_cards(evs), "firstDelta": round(first_delta - user_ts, 2) if first_delta else None, "total": round(done_ts - user_ts, 2), "ok": result.get("ok")}

def ui_hit(applied_list, key, value):
    for a in applied_list:
        ch = a.get("changes") or {}
        if key == "chartAnnotations" and "chartAnnotations" in ch and ch["chartAnnotations"].get("op") == value: return True
        if key in ch and (ch[key] == value or (isinstance(ch[key], dict) and value in ch[key].values())): return True
        if "presentation" in ch and ch["presentation"].get(key) == value: return True
    return False

def ui_any_hit(applied_list, keys):
    for a in applied_list:
        ch = a.get("changes") or {}
        for k in keys:
            if k in ch or ("presentation" in ch and k in ch["presentation"]): return True
    return False

def score(item, mode, obs):
    ex = item["expect"]; problems = []
    tools = obs.get("tools") or []
    routed = obs.get("routed")
    reply = obs.get("reply") or ""
    if mode == "voice":
        if ex.get("voice_route") and routed != ex["voice_route"]: problems.append(f"route:{routed}≠{ex['voice_route']}")
        if ex.get("intent") and obs.get("intent") != ex["intent"]: problems.append(f"intent:{obs.get('intent')}≠{ex['intent']}")
    if mode == "typed":
        if ex.get("route") and obs.get("typed_route") != ex["route"]:
            problems.append(f"route:{obs.get('typed_route')}≠{ex['route']}")
        # A direct ui_intent turn ran no model round: its effects are the window command(s) plus one
        # assistant line. Tool/card checks do not apply; reply and UI effects still must.
        if obs.get("typed_route") == "ui_intent" and not ex.get("must"):
            for k, v in (ex.get("ui") or {}).items():
                if not ui_hit(obs["ui"], k, v): problems.append(f"ui:{k}")
            if ex.get("ui_any") and not ui_any_hit(obs["ui"], ex["ui_any"]):
                problems.append(f"ui-any:{sorted(ex['ui_any'])}")
            if not reply.strip(): problems.append("no-reply")
            if obs.get("cards"): problems.append(f"unexpected-card:{[c['type'] for c in obs['cards']]}")
            if len(reply) > 600: problems.append(f"long:{len(reply)}")
            if re.match(r"^(好的|好，|收到|明白)", reply.strip()): problems.append("filler-prefix")
            if re.search(r"https?://|`[a-z_]+`|get_[a-z_]+|create_[a-z_]+", reply): problems.append("leaks-tool-or-link")
            return problems
    if routed in {"confirm", "cancel", "stop", "hangup", "tour_next", "tour_stop", "chat"} or obs.get("intent"):
        if obs.get("intent") and ex.get("ui"):
            for k, v in ex["ui"].items():
                if not ui_hit(obs["ui"], k, v): problems.append(f"ui:{k}")
        if obs.get("intent") and ex.get("ui_any") and not ui_any_hit(obs["ui"], ex["ui_any"]):
            problems.append(f"ui-any:{sorted(ex['ui_any'])}")
        return problems
    called = set(tools)
    if mode == "typed" and obs.get("typed_route") == "order_intent":
        # 意图重构第 3 步: the parser was sure and the Runtime's planner made the card (or asked, or refused) with no
        # model round — that is the card tool's work; the card, the reply and nothing else are scored.
        called |= {"propose_order"}
    clarified = bool(ex.get("clarify_ok")) and not called and re.search(ex["clarify_ok"], reply) is not None
    if ex.get("none") and called - set(ex.get("allow") or set()) - PRECHECK: problems.append(f"tools-called:{sorted(called)}")
    # Annotations drawn through the UI command have the same effect as the annotate_chart tool.
    if "annotate_chart" in (ex.get("must") or set()) and ui_hit(obs["ui"], "chartAnnotations", "set") or ("annotate_chart" in (ex.get("must") or set()) and ui_hit(obs["ui"], "chartAnnotations", "add")):
        called = called | {"annotate_chart"}
    if ex.get("must") and not clarified:
        if ex.get("both_required"):
            if not set(ex["must"]) <= called: problems.append(f"missing:{sorted(set(ex['must']) - called)}")
        elif not (called & set(ex["must"])): problems.append(f"missing:{sorted(ex['must'])}")
    allowed = set(ex.get("must") or set()) | set(ex.get("allow") or set()) | PRECHECK
    extra = called - allowed
    if extra and not ex.get("none"): problems.append(f"extra:{sorted(extra)}")
    cards = obs.get("cards") or []
    if ex.get("card") and not any(c["type"] == ex["card"] for c in cards) and not clarified: problems.append("no-card")
    if ex.get("card_any") and not any(c["type"] in ex["card_any"] for c in cards): problems.append("no-card")
    if (ex.get("nocard") or ex.get("refuse")) and cards: problems.append(f"unexpected-card:{[c['type'] for c in cards]}")
    if ex.get("args") and cards:
        c = cards[-1]
        for k, v in ex["args"].items():
            got = c.get(k)
            if k == "symbol":
                if not any(v.lower() in str(s).lower() for s in [got, *(c.get("symbols") or []), *[o.get("symbol") for o in (c.get("orders") or []) if isinstance(o, dict)]]): problems.append(f"arg:symbol={got}")
            elif isinstance(v, set):
                if got not in v: problems.append(f"arg:{k}={got}")
            elif got != v and not (isinstance(v, (int, float)) and got is not None and abs(float(got) - v) < 1e-6): problems.append(f"arg:{k}={got}")
    if ex.get("ui"):
        for k, v in ex["ui"].items():
            if not ui_hit(obs["ui"], k, v): problems.append(f"ui:{k}")
    if ex.get("ui_any") and not ui_any_hit(obs["ui"], ex["ui_any"]):
        problems.append(f"ui-any:{sorted(ex['ui_any'])}")
    if ex.get("pref"):
        for k, v in ex["pref"].items():
            if leaf(obs["prefs"], k) != v: problems.append(f"pref:{k}={leaf(obs['prefs'], k)}")
    if ex.get("pref_any"):
        hit = False
        for k, v in ex["pref_any"].items():
            got = leaf(obs["prefs"], k)
            if (v == "male" and isinstance(got, str) and "male" in got and "female" not in got) or got == v: hit = True
        if not hit: problems.append("pref-any")
    if ex.get("refuse") and not re.search(r"不能|无法|不支持|不会|不可以|不提供|没有权限|不做|永久关闭|只能模拟|做不到|做不了|改不了|买不了|.{1,4}不了|不经手|不在.{0,6}范围|没有.{0,4}入口|没(有)?开放|不该|不允许|没有实盘|实盘.{0,6}关闭|只有模拟|不涉及真钱|不接实盘", reply): problems.append("no-refusal")
    # A direction follow-up (order-16) is the accepted alternative to a card, same as for the card check.
    if ex.get("honest_missing") and not clarified and not re.search(r"没有|找不到|不在|未找到|无法|没法|不存在|没找到|没搜到|不支持|暂不|给不了|不足", reply) and not cards: problems.append("not-honest-about-missing")
    if len(reply) > 600: problems.append(f"long:{len(reply)}")
    if re.match(r"^(好的|好，|收到|明白)", reply.strip()): problems.append("filler-prefix")
    if re.search(r"https?://|`[a-z_]+`|get_[a-z_]+|create_[a-z_]+", reply): problems.append("leaks-tool-or-link")
    return problems

def main():
    if not DB:
        raise SystemExit("set DB to the Runtime's sqlite path (see header)")
    threading.Thread(target=beat, daemon=True).start()
    time.sleep(1.2)
    assert client.get("/api/app/ui/windows", params={"deviceId": "*"}).json()["windows"], "window not registered"
    catalog_names()
    results = []
    items = [t for t in T if not ONLY or t["id"] in ONLY]
    for item in items:
        for mode in MODES:
            ex = item["expect"]
            for run in range(RUNS):
                reset_window(); reset_prefs()
                if ex.get("start_prefs"): client.put("/api/preferences", json=ex["start_prefs"])
                if ex.get("start_ui"):
                    with lock: pulse["state"].update(ex["start_ui"]); pulse["revision"] += 1
                time.sleep(0.6)
                sid = new_session(); obs = {"ui": [], "prefs": None}
                pending = []
                for prior in ex.get("seq") or []:
                    r = run_turn(sid, prior, "voice" if mode == "voice" else None); pending = r["cards"]
                if ex.get("tour_context"):
                    with lock: pulse["state"]["tour"] = {"active": True, "step": 1, "total": 3, "current": {"target": "rail:order", "label": "下单区"}}; pulse["revision"] += 1
                    time.sleep(0.6)
                marker = len(applied); t_start = time.time()
                if mode == "voice":
                    route = route_utterance(item["text"], pending=bool(pending), tour_active=bool(ex.get("tour_context")),
                                            product_names=CATALOG)
                    obs["routed"] = route
                    intent = match_intent(item["text"]) if route == "delegate" else None
                    obs["intent"] = intent.kind if intent else None
                    if route in {"confirm", "cancel", "stop", "hangup", "chat", "tour_next", "tour_prev", "tour_stop"}:
                        obs["reply"] = f"<runtime:{route}>"; obs["tools"] = []; obs["cards"] = pending
                    elif intent:
                        with lock: rev = pulse["revision"]
                        changes = dict(intent.changes)
                        if changes.get("theme") == "toggle":
                            with lock: changes["theme"] = "light" if pulse["state"]["theme"] == "dark" else "dark"
                        if changes.get("fontScale") in {"larger", "smaller"}:
                            # Mirror run_window_intent: the command schema is the absolute scale, so
                            # resolve one step from the window's current size the way the Runtime does.
                            scales = ["standard", "large", "extra-large"]
                            with lock: current = pulse["state"].get("fontScale") or "standard"
                            index = scales.index(current) if current in scales else 1
                            target = index + (1 if changes["fontScale"] == "larger" else -1)
                            changes = {} if not 0 <= target < len(scales) else {**changes, "fontScale": scales[target]}
                        rc = (client.post("/api/app/ui/commands", params={"waitMs": 3000}, json={"commandId": str(uuid.uuid4()), "windowId": WINDOW, "expectedRevision": rev, "changes": changes}).json()
                              if changes else {"status": "applied"})
                        for f in intent.follow:
                            with lock: rev = pulse["revision"]
                            client.post("/api/app/ui/commands", params={"waitMs": 3000}, json={"commandId": str(uuid.uuid4()), "windowId": WINDOW, "expectedRevision": rev, "changes": f})
                        time.sleep(0.8)
                        obs.update({"reply": f"<intent:{intent.kind}:{rc.get('status')}>", "tools": [], "cards": pending, "total": round(time.time() - t_start, 2), "firstDelta": round(time.time() - t_start, 2)})
                    else:
                        obs.update(run_turn(sid, item["text"], "voice"))
                else:
                    obs["typed_route"] = classify_turn(item["text"], has_pending_card=bool(pending),
                                                        ui_available=True, catalog=CATALOG).kind
                    obs.update(run_turn(sid, item["text"], None))
                time.sleep(0.8)
                obs["ui"] = [a for a in applied[marker:]]
                obs["prefs"] = get_prefs().get("preferences")
                problems = score(item, mode, obs)
                reject_cards(obs.get("cards") or [])
                rec = {"id": item["id"], "category": item["category"], "text": item["text"], "mode": mode, "run": run + 1, "tools": obs.get("tools"),
                       "routed": obs.get("routed"), "typedRoute": obs.get("typed_route"), "intent": obs.get("intent"),
                       "cards": [{k: c.get(k) for k in ("type", "symbol", "side", "volume", "priceIntent", "limitPrice", "direction", "symbols")} for c in (obs.get("cards") or [])],
                       "ui": [a.get("changes") for a in obs["ui"]], "firstDelta": obs.get("firstDelta"), "total": obs.get("total"),
                       "reply": (obs.get("reply") or "")[:300], "spoken": (obs.get("spoken") or "")[:200] if obs.get("spoken") else None, "problems": problems, "pass": not problems}
                results.append(rec)
                print(f"{rec['id']:10s} {mode:5s} r{run+1} {'PASS' if rec['pass'] else 'FAIL'} route={rec['typedRoute'] or rec['routed'] or ''} tools={rec['tools']} cards={[c['type'] for c in rec['cards']]} first={rec['firstDelta']} total={rec['total']} {problems}", flush=True)
                json.dump(results, open(OUT / "results.json", "w"), ensure_ascii=False, indent=1)
    by_cat = {}
    for r in results:
        by_cat.setdefault(r["category"], [0, 0]); by_cat[r["category"]][1] += 1; by_cat[r["category"]][0] += r["pass"]
    total = sum(r["pass"] for r in results)
    print(f"\nTOTAL {total}/{len(results)} = {100*total/len(results):.1f}%")
    for cat, (p, n) in by_cat.items(): print(f"  {cat:8s} {p}/{n}")
    lines = ["| id | 句子 | 模式 | 次 | 路由 | 工具 | 卡 | 首 delta s | 总 s | 结果 | 问题 |", "|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in results:
        route = r["typedRoute"] or r["routed"] or (f"intent:{r['intent']}" if r["intent"] else "")
        lines.append(f"| {r['id']} | {r['text']} | {r['mode']} | {r['run']} | {route} | {','.join(r['tools'] or [])} | {','.join(c['type'] for c in r['cards'])} | {r['firstDelta']} | {r['total']} | {'✅' if r['pass'] else '❌'} | {'; '.join(r['problems'])} |")
    (OUT / "results.md").write_text("\n".join(lines) + f"\n\nTOTAL {total}/{len(results)}\n", encoding="utf-8")

if __name__ == "__main__":
    main()
