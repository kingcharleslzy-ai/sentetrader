"""语音前台直接查询（AI_TRADER_VOICE_FRONT_QUERY，2026-10-13）：哪些句子留给前台、query 工具各类的返回形状、
前台轮次记入对话并同步交易 AI 的内存历史，开关关时与之前一致。"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime

import pytest

from test_voice_realtime import WINDOW, _scripted_call, _voice_app
from trader_runtime.agent_api import CHAT_SEND
from trader_runtime.voice_front import (
    CHINA,
    FRONT_QUERY_ENV,
    analysis_fields,
    brief_fields,
    classify_front,
    front_arguments,
    front_query_enabled,
    news_fields,
    orders_fields,
    session_note,
    spoken_line,
    takeover_line,
)
from trader_runtime.voice_realtime import FRONT_TOOLS, INSTRUCTIONS, TOOLS, CallState
from trader_runtime.voice_route import route_utterance

NAMES = ["螺纹钢", "沪金", "沪银", "白糖", "铁矿石", "焦炭", "原油"]


def _front(text, last_symbol="螺纹钢", last_what=None):
    route = route_utterance(text, pending=False, product_names=NAMES)
    return classify_front(text, route, NAMES, last_symbol=last_symbol, last_what=last_what)


def test_the_switch_is_off_unless_named(monkeypatch):
    monkeypatch.delenv(FRONT_QUERY_ENV, raising=False)
    assert not front_query_enabled("owner-1")
    monkeypatch.setenv(FRONT_QUERY_ENV, "owner-1, owner-2")
    assert front_query_enabled("owner-2") and not front_query_enabled("owner-3")
    monkeypatch.setenv(FRONT_QUERY_ENV, "all")
    assert front_query_enabled("anyone") and not front_query_enabled("")


def test_queries_concepts_small_talk_and_half_sentences_stay_with_the_front_desk():
    stays = {
        "呃，我想看看那个，螺纹钢，现在多少钱一吨啊": ("query", "quote", "螺纹钢"),
        "它今天是涨了还是跌了呀": ("query", "quote", "螺纹钢"),  # "它" = the contract named earlier
        "那个，我账户里头，现在还有多少钱": ("query", "account", None),
        "我现在手上有啥仓位": ("query", "positions", None),
        "我有哪些委托没成交": ("query", "orders", None),
        "螺纹有什么新闻": ("query", "news", "螺纹钢"),  # the catalog name, never the spoken prefix or alias
        "黄金有什么新闻": ("query", "news", "沪金"),
        "白银有哪些委托没成交": ("query", "orders", "沪银"),
        "今天有什么简讯": ("query", "brief", None),
        "沪金夜盘咋样，波动大不大": ("query", "analysis", "沪金"),
        "什么是保证金": ("explain", None, None),  # r4254730760: a definition, not the account balance
        "什么是持仓": ("explain", None, None),
        "啥是委托": ("explain", None, None),
        "止损是啥意思啊，我不太懂": ("explain", None, None),
        "保证金是什么意思": ("explain", None, None),
        "平今是什么": ("explain", None, None),
        "我想问一下那个": ("half", None, None),
        "帮我看看": ("half", None, None),
        "喂？喂，能听到吗": ("chat", None, None),
        "谢谢": ("chat", None, None),
    }
    for text, (kind, what, symbol) in stays.items():
        front = _front(text)
        assert front is not None and (front.kind, front.what, front.symbol_query) == (kind, what, symbol), text
    # "那沪银呢" after a positions question asks for positions.
    assert _front("那沪银呢", last_what="positions").what == "positions"


def test_orders_strategies_judgments_settings_and_interface_words_still_go_to_the_trading_ai():
    for text in ("那帮我买一手螺纹吧，就买一手", "空两手白银，挂对手价", "不做了，撤了吧", "开三手，不对，两手沪金多单",
                 "就是那个白糖，能不能买", "白糖为什么跌", "铁矿跟焦炭哪个强", "说说今天黑色系整体什么情况",
                 "止损设在3050", "把语速调快一点", "帮我新建一个策略", "把边上那个收起来", "切到螺纹",
                 "螺纹跌破3000提醒我", "螺纹和沪金都多少钱", "这条新闻是什么意思", "这个快讯什么意思",
                 # r4254939628: the trend summary has no MACD / KD and drops RSI — the trading AI reads indicators.
                 "原油的MACD怎么样", "KD金叉了吗", "螺纹RSI怎么样"):
        assert _front(text) is None, text


def test_query_arguments_take_the_words_over_the_models_guess():
    front = {"kind": "query", "what": "quote", "symbol_query": "螺纹钢"}
    assert front_arguments(front, {"what": "positions", "query": "铁矿"}) == {"what": "quote", "query": "螺纹钢"}
    assert front_arguments({"kind": "explain", "what": None, "symbol_query": None}, {"what": "account", "query": "x"}) == {"what": "account"}
    assert front_arguments({"kind": "query", "what": "quote", "symbol_query": None}, {"query": "沪金"}) == {"what": "quote", "query": "沪金"}


def test_a_quote_that_is_not_current_says_why_and_when_it_opens():
    def at(text):
        return datetime.fromisoformat(text).replace(tzinfo=CHINA)
    assert session_note(closed=False, stale=False, has_night=True, exchange="SHFE") is None
    assert "晚上九点开夜盘" in session_note(closed=True, stale=True, has_night=True, exchange="SHFE", now=at("2026-10-13T16:00"))
    assert "下个交易日上午九点" in session_note(closed=True, stale=True, has_night=False, exchange="DCE", now=at("2026-10-13T16:00"))
    assert "午间休市" in session_note(closed=True, stale=True, has_night=True, exchange="SHFE", now=at("2026-10-13T12:10"))
    assert "上午九点半" in session_note(closed=True, stale=True, has_night=False, exchange="CFFEX", now=at("2026-10-17T22:00"))
    assert "没有更新" in session_note(closed=False, stale=True, has_night=True, exchange="SHFE", now=at("2026-10-13T10:00"))
    # Friday has its night session too (#851 review r4258419999 over r4254939636): 21:00 that evening.
    assert "下个交易日上午九点" in session_note(closed=True, stale=True, has_night=False, exchange="SHFE", now=at("2026-10-16T16:00"))
    friday = session_note(closed=True, stale=True, has_night=True, exchange="SHFE", now=at("2026-10-16T16:00"))
    assert "晚上九点开夜盘" in friday
    from trader_runtime.voice_front import price_unit, reopen_words
    assert reopen_words(has_night=True, exchange="SHFE", now=at("2026-10-16T16:00")) == "晚上九点开夜盘"
    # r4254939641: fiberboard is quoted per cubic metre, plywood per sheet.
    assert price_unit("DCE.fb2701") == "元/立方米" and price_unit("DCE.bb2701") == "元/张"


def test_result_shapes_are_compact_chinese_and_never_raw_json():
    names = {"shfe.rb2701": "螺纹钢2701"}
    orders = orders_fields({"dataStatus": "normal", "ordersInitialized": True, "orders": [
        {"symbol": "SHFE.rb2701", "direction": "BUY", "offset": "OPEN", "volume": 2, "volumeLeft": 1, "limitPrice": 3080.0, "status": "ALIVE"},
        {"symbol": "SHFE.rb2701", "direction": "SELL", "offset": "CLOSE", "volume": 1, "volumeLeft": 0, "limitPrice": 3100.0, "status": "FINISHED"}]}, names)
    assert orders == {"委托": "螺纹钢2701 买开 2 手，限价 3080 元/吨，还有 1 手没成交", "共": "1 笔"}
    assert orders_fields({"dataStatus": "normal", "orders": []}, names) == {"委托": "现在没有挂着没成交的委托"}
    assert "error" in orders_fields({"dataStatus": "unavailable", "ordersInitialized": False}, names)
    feed = {"status": "ok", "brief": {"status": "ready", "take": "库存去化，偏多"}, "events": [
        {"eventLabel": "钢厂限产", "publishedAt": "2026-10-13T02:32:00Z"}, {"title": "螺纹社库下降", "publishedAt": "2026-10-13T01:00:00Z"}]}
    news = news_fields(feed, "螺纹钢")
    assert news["要点"] == "库存去化，偏多" and news["新闻"] == "10:32 钢厂限产；09:00 螺纹社库下降" and len(news) <= 6
    assert news_fields({"status": "ok", "items": []}, "螺纹钢") == {"新闻": "最近一天没有螺纹钢相关的快讯"}
    # Rows still being screened are hidden from events: read the raw items, or say they are coming.
    assert news_fields({"status": "ok", "events": [], "items": [{"title": "螺纹社库下降", "publishedAt": "2026-10-13T01:00:00Z"}]},
                       "螺纹钢")["新闻"] == "09:00 螺纹社库下降"
    assert news_fields({"status": "ok", "events": [], "items": [], "screen": {"pending": 3}}, "螺纹钢")["新闻"].startswith("有 3 条")
    assert "error" in news_fields({"status": "unavailable"}, None)
    brief = brief_fields({"items": [{"publishedAt": "2026-10-13T02:00:00Z", "productName": "螺纹钢", "text": "螺纹急跌 1%"}]})
    assert brief == {"简讯": "10:00 螺纹钢 螺纹急跌 1%", "共": "1 条"}
    assert brief_fields({"items": [], "status": "disabled"})["简讯"].startswith("简讯没有开")
    analysis = analysis_fields({"symbol": "SHFE.rb2701", "trend": "down", "barCount": 60, "periodReturnPercent": -8.3, "sma5": 3080.4,
                                "sma20": 3120.2, "indicators": {"atr": {"reading": "波动较近期收敛"}, "rsi": {"reading": "中性区间"}}}, "螺纹钢2701")
    assert analysis["走势"].startswith("日线偏弱") and analysis["近期涨跌"] == "近 60 个交易日跌 8.30%" and analysis["均价"] == "五日 3080、二十日 3120 元/吨"
    assert len(analysis) <= 6
    # What the Runtime says itself: units and spoken signs, no slash pairs or raw keys.
    line = spoken_line({"合约": "螺纹钢2701", "最新价": "3083 元/吨", "涨跌": "跌 0.23%", "说明": "现在已收盘，晚上九点开夜盘，这是收盘价"})
    assert line == "螺纹钢2701，最新价 3083 元/吨，跌 0.23%，现在已收盘，晚上九点开夜盘，这是收盘价。"
    assert spoken_line({"账户": "模拟账户", "权益": "992,887 元", "今日盈亏": "-6,086 元", "浮动盈亏": "+0 元"}) == \
        "模拟账户，权益 992,887 元，今天亏 6,086 元，浮动盈亏持平。"
    assert spoken_line({"error": "这个暂时查不到，稍后再问"}) == "这个暂时查不到，稍后再问。"
    assert spoken_line({"ok": False, "error": "TimeoutError", "say": "后台出了点问题，请稍后再说一遍。"}) == "后台出了点问题，请稍后再说一遍。"
    assert takeover_line("half", "帮我看看").startswith("你想问什么") and takeover_line("chat", "嗯") == ""


async def test_the_query_tool_reads_every_kind_through_the_formal_routes(tmp_path):
    app, _, service = await _voice_app(tmp_path, "front-query.sqlite3")
    state = CallState(window_id=WINDOW)
    quote = await service.run_tool("query", {"what": "quote", "query": "沪金"}, state)
    assert quote == {"合约": "沪金2612", "最新价": "824 元/克", "涨跌": "涨 0.68%", "买一卖一": "823 / 824"}
    assert (await service.run_tool("query", {"what": "quote"}, state))["合约"] == "沪金2612"  # the contract on screen
    assert (await service.run_tool("query", {"what": "quote", "query": "不存在的品种"}, state))["error"].startswith("没找到")
    positions = await service.run_tool("query", {"what": "positions"}, state)
    assert "沪金2612 多 2 手" in positions["持仓"] and "更新于" not in positions
    account = await service.run_tool("query", {"what": "account"}, state)
    assert account["权益"] == "1,026,580 元" and len(account) <= 6
    assert await service.run_tool("query", {"what": "orders"}, state) == {"委托": "现在没有挂着没成交的委托"}
    assert await service.run_tool("query", {"what": "brief"}, state) == {"简讯": "现在没有新的简讯"}
    analysis = await service.run_tool("query", {"what": "analysis", "query": "沪金"}, state)
    assert analysis["合约"] == "沪金2612" and analysis["最新价"].startswith("824 元/克") and len(analysis) <= 6
    assert "error" in await service.run_tool("query", {"what": "news", "query": "沪金"}, state)  # no news source in this app
    assert await service.run_tool("query", {"what": "weather"}, state) == {"error": "这个查不了"}
    # A quote that is no longer current is still read, with the reason instead of the bare refusal.
    from datetime import timedelta
    provider = app.state.data_provider
    channel = app.state.shared_market.markets[("SHFE.au2612", 300)]
    changed = channel.changed
    provider.age_seconds = 30.0
    provider.quote_datetime = (datetime.fromisoformat(provider.quote_datetime) - timedelta(seconds=30)).isoformat()
    await asyncio.wait_for(changed.wait(), timeout=2)
    stale = await service.run_tool("query", {"what": "quote", "query": "沪金"}, state)
    assert stale["最新价"] == "824 元/克" and "说明" in stale and "买一卖一" not in stale
    # The old quick tool keeps refusing the same stale quote (switch off = unchanged).
    assert await service.run_tool("get_quote", {"query": "沪金"}, state) == {"error": "行情数据过时/不可用"}
    await app.state.shared_market.close()
    app.state.store.close()


def test_the_trading_ai_history_takes_a_front_turn_without_losing_its_own():
    from trader_runtime.native_agent import NativeAgentAdapter
    agent = NativeAgentAdapter.__new__(NativeAgentAdapter)
    agent._history, agent._history_loaded, agent._turns = {"s1": [{"role": "user", "content": "旧问题"}]}, {"s1"}, {}
    agent.append_voice_turn("s1", "止损是啥意思", "止损就是提前定好认亏的价。")
    assert agent._history["s1"][-2:] == [{"role": "user", "content": "止损是啥意思"},
                                         {"role": "assistant", "content": "止损就是提前定好认亏的价。"}]
    agent.append_voice_turn("s2", "螺纹多少钱", "3083")  # not loaded: the events rebuild it later
    assert "s2" not in agent._history
    agent.append_voice_turn("s1", "没回答的", "")  # an unanswered question is not history
    assert agent._history["s1"][-1]["content"] == "止损就是提前定好认亏的价。"

    class Running:
        session_id = "s1"
    agent._turns = {"run-1": Running()}
    agent.append_voice_turn("s1", "又一句", "又一答")  # a running turn's marks must not shift: rebuilt instead
    assert "s1" not in agent._history and "s1" not in agent._history_loaded


def _front_call(monkeypatch, utterance, reply_text=None, tool_call=None, finish=True):
    """One scripted call (switch on for local-user): Doubao finalises `utterance`, then answers with
    `reply_text` (its own words; `finish=False` leaves that reply open until it is cancelled) or calls
    `tool_call`; Runtime speech is acknowledged."""
    monkeypatch.setenv(FRONT_QUERY_ENV, "local-user")
    sent_final = False

    async def on_send(payload, emit):
        nonlocal sent_final
        kind = payload["type"]
        if kind == "session.create":
            await emit({"type": "session.created", "session": {"id": "s1"}})
        elif kind == "input_audio_buffer.append" and not sent_final:
            sent_final = True
            await emit({"type": "conversation.item.input_audio_transcription.completed", "text": utterance})
            await asyncio.sleep(0.05)
            if tool_call is not None:
                await emit({"type": "response.function_call_arguments.done", "items": [tool_call]})
            elif reply_text is not None:
                await emit({"type": "response.output_text.delta", "delta": reply_text})
                await emit({"type": "response.output_audio.started", "tts_type": "default"})
                await emit({"type": "response.output_audio.delta", "delta": "QUJD"})
                if finish:
                    await emit({"type": "response.output_audio.done"})
                    await emit({"type": "response.done", "response": {"usage": {"total_tokens": 3}}})
            else:
                await emit({"type": "response.done", "response": {"usage": {"total_tokens": 1}}})
        elif kind == "response.cancel":
            await emit({"type": "response.canceled"})
            await emit({"type": "response.done", "response": {"usage": {"total_tokens": 1}}})
        elif kind == "speech_text_buffer.replacement.commit":
            # The injected line is this turn's reply: Doubao reads it and ends the turn.
            await emit({"type": "response.output_audio.started", "tts_type": "default", "response_id": "r1"})
            await emit({"type": "response.output_audio.delta", "delta": "QUJD", "response_id": "r1"})
            await emit({"type": "response.output_audio.done", "response_id": "r1"})
            await emit({"type": "response.done", "response": {"usage": {"total_tokens": 4}}})
        elif kind == "speech_text_buffer.commit":
            await emit({"type": "response.output_audio.started", "tts_type": "chat_tts_text", "response_id": "rt"})
            await emit({"type": "response.output_audio.delta", "delta": "QUJD", "response_id": "rt"})
            await emit({"type": "response.output_audio.done", "response_id": "rt"})
            await emit({"type": "response.done", "response": {"usage": {"total_tokens": 2}}})
        elif kind == "session.close":
            await emit({"type": "session.closed"})
    return _scripted_call(monkeypatch, on_send)


async def _run_front(monkeypatch, tmp_path, name, utterance, *, until, **script):
    app, _, service = await _voice_app(tmp_path, name)
    session = app.state.broker.create_session("local", "native", "local-user")
    chat_sends = []
    original = service._call

    async def call(operation, **kwargs):
        if operation == CHAT_SEND:
            chat_sends.append(kwargs.get("body"))
        return await original(operation, **kwargs)
    service._call = call
    upstream_sent, client_out, inbound, run = _front_call(monkeypatch, utterance, **script)
    await inbound.put({"type": "websocket.receive", "text": json.dumps({"type": "hello", "windowId": WINDOW, "sessionId": session.session_id})})
    await inbound.put({"type": "websocket.receive", "bytes": b"\x00" * 640})
    await run(service, until=lambda: until(upstream_sent, client_out))
    events = [e["payload"] for _, e in app.state.store.list_events(session.session_id) if e["name"] == "message.completed"]
    timeline = client_out[-1]["timeline"]
    from datetime import UTC
    # Every reply's tokens reach the daily meter, the muted (cancelled) ones included.
    assert app.state.store.token_usage("local-user", datetime.now(UTC).date().isoformat()) == client_out[-1]["usage"]["total_tokens"]
    await app.state.shared_market.close()
    app.state.store.close()
    return upstream_sent, client_out, events, timeline, chat_sends


async def test_a_concept_question_is_doubaos_own_answer_recorded_as_a_voice_turn(tmp_path, monkeypatch):
    upstream, client_out, events, timeline, chat_sends = await _run_front(
        monkeypatch, tmp_path, "front-explain.sqlite3", "止损是啥意思啊，我不太懂。",
        reply_text="止损就是提前定一个认亏的价，跌到就卖掉。",
        until=lambda up, out: any(e.get("type") == "usage" for e in out))
    created = next(p for p in upstream if p["type"] == "session.create")
    assert [tool["name"] for tool in created["session"]["tools"]] == ["confirm", "cancel", "read_screen", "query"]
    assert not created["session"]["instructions"].startswith(INSTRUCTIONS)
    assert not chat_sends and not any(p["type"] == "response.cancel" for p in upstream)  # no trading AI, never muted
    routed = next(e for e in timeline if e["event"] == "routed")
    assert routed["by"] == "front" and routed["front"] == "explain"
    assert any(e["event"] == "frontDone" for e in timeline)
    assert events[-2]["role"] == "user" and events[-2]["text"] == "止损是啥意思啊，我不太懂。" and events[-2]["origin"] == "voice"
    assert events[-1]["role"] == "assistant" and events[-1]["text"] == "止损就是提前定一个认亏的价，跌到就卖掉。" and events[-1]["origin"] == "voice"


async def test_a_named_query_is_read_in_the_runtimes_words_inside_doubaos_tool_turn(tmp_path, monkeypatch):
    """"沪金现在多少钱" → Doubao calls query (the model guessed another kind and contract) → the Runtime serves
    the read it started on the final, with the kind and contract the words named, and its own line replaces
    Doubao's wording in the same turn. Never the trading AI, never muted."""
    upstream, client_out, events, timeline, chat_sends = await _run_front(
        monkeypatch, tmp_path, "front-quote.sqlite3", "沪金现在多少钱？",
        tool_call={"call_id": "c1", "name": "query", "arguments": json.dumps({"what": "analysis", "query": "螺纹"})},
        until=lambda up, out: any(e.get("type") == "usage" for e in out))
    assert not chat_sends and not any(p["type"] == "response.cancel" for p in upstream)
    tool_reply = next(p for p in upstream if p["type"] == "conversation.item.create" and p["items"][0].get("role") == "tool")
    result = json.loads(tool_reply["items"][0]["content"][0]["text"])
    assert result["合约"] == "沪金2612" and result["最新价"] == "824 元/克"
    line = "".join(p["text"] for p in upstream if p["type"] == "speech_text_buffer.replacement.append")
    assert line.startswith("沪金2612，最新价 824 元/克，涨 0.68%") and result["spoken"] == line
    assert next(e for e in timeline if e["event"] == "routed")["what"] == "quote"
    assert next(e for e in timeline if e["event"] == "frontToolRemapped")["used"] == json.dumps({"what": "quote", "query": "沪金"}, ensure_ascii=False)
    assert any(e["event"] == "frontDone" for e in timeline)
    assert events[-1]["text"] == line and events[-2]["text"] == "沪金现在多少钱？"


async def test_a_named_query_doubao_answers_from_memory_is_cancelled_and_read_by_the_runtime(tmp_path, monkeypatch):
    upstream, client_out, events, timeline, chat_sends = await _run_front(
        monkeypatch, tmp_path, "front-quote-memory.sqlite3", "沪金现在多少钱？", reply_text="沪金大概八百。", finish=False,
        until=lambda up, out: any(p["type"] == "speech_text_buffer.commit" for p in up))
    assert not chat_sends
    kinds = [p["type"] for p in upstream]
    assert kinds.index("response.cancel") < kinds.index("speech_text_buffer.commit")
    assert "沪金大概八百。" not in [e.get("delta") for e in client_out if e.get("type") == "text"]
    line = next(p for p in upstream if p["type"] == "speech_text_buffer.commit")["text"]
    assert line.startswith("沪金2612，最新价 824 元/克")
    assert next(e for e in timeline if e["event"] == "frontQueryFallback")["reason"] == "text"
    assert events[-1]["text"] == line


async def test_a_stalled_half_sentence_gets_a_short_question_back_without_the_trading_ai(tmp_path, monkeypatch):
    """Doubao never finalises the sentence (asrStallFinal): the Runtime answers it itself — for a half
    sentence a short question back, never the agent (it used to get the whole agent turn)."""
    import trader_runtime.voice_realtime as module
    monkeypatch.setattr(module, "STALL_FINAL_SECONDS", 0.05)
    monkeypatch.setenv(FRONT_QUERY_ENV, "local-user")
    app, _, service = await _voice_app(tmp_path, "front-stall.sqlite3")
    preferences = app.state.store.user_preferences("local-user")
    fast = preferences.model_copy(update={"voice": preferences.voice.model_copy(update={"endpointStallSeconds": 0.2})})
    monkeypatch.setattr(app.state.store, "user_preferences", lambda user_id: fast)
    sent = False

    async def on_send(payload, emit):
        nonlocal sent
        kind = payload["type"]
        if kind == "session.create":
            await emit({"type": "session.created", "session": {"id": "s1"}})
        elif kind == "input_audio_buffer.append" and not sent:
            sent = True
            await emit({"type": "conversation.item.input_audio_transcription.started"})
            await emit({"type": "conversation.item.input_audio_transcription.delta", "delta": "我想问一下那个"})
        elif kind == "speech_text_buffer.commit":
            await emit({"type": "response.output_audio.started", "tts_type": "chat_tts_text", "response_id": "rt"})
            await emit({"type": "response.output_audio.done", "response_id": "rt"})
            await emit({"type": "response.done", "response": {"usage": {}}})
        elif kind == "session.close":
            await emit({"type": "session.closed"})
    upstream, client_out, inbound, run = _scripted_call(monkeypatch, on_send)
    await inbound.put({"type": "websocket.receive", "text": json.dumps({"type": "hello", "windowId": WINDOW})})
    await inbound.put({"type": "websocket.receive", "bytes": b"\x00" * 640})
    await run(service, until=lambda: any(p["type"] == "speech_text_buffer.commit" for p in upstream), timeout=8)
    line = next(p for p in upstream if p["type"] == "speech_text_buffer.commit")["text"]
    assert line.startswith("你想问什么")
    timeline = client_out[-1]["timeline"]
    assert any(e["event"] == "asrStallFinal" for e in timeline)
    assert next(e for e in timeline if e["event"] == "frontTakeover")["front"] == "half"
    assert not any(e["event"] == "toolDone" and e.get("tool") == "delegate" for e in timeline)
    await app.state.shared_market.close()
    app.state.store.close()


@pytest.mark.parametrize("utterance", ["止损是啥意思啊，我不太懂。", "我想问一下那个。"])
async def test_switch_off_keeps_the_old_front_desk_and_delegates_as_before(tmp_path, monkeypatch, utterance):
    monkeypatch.delenv(FRONT_QUERY_ENV, raising=False)
    app, _, service = await _voice_app(tmp_path, "front-off.sqlite3")
    session = app.state.broker.create_session("local", "native", "local-user")

    async def on_send(payload, emit):
        if payload["type"] == "session.create":
            await emit({"type": "session.created", "session": {"id": "s1"}})
        elif payload["type"] == "input_audio_buffer.append":
            await emit({"type": "conversation.item.input_audio_transcription.completed", "text": utterance})
        elif payload["type"] == "session.close":
            await emit({"type": "session.closed"})
    upstream, client_out, inbound, run = _scripted_call(monkeypatch, on_send)
    await inbound.put({"type": "websocket.receive", "text": json.dumps({"type": "hello", "windowId": WINDOW, "sessionId": session.session_id})})
    await inbound.put({"type": "websocket.receive", "bytes": b"\x00" * 640})
    await run(service, until=lambda: any(e.get("type") == "tool_result" and e.get("name") == "delegate" for e in client_out), timeout=8)
    created = next(p for p in upstream if p["type"] == "session.create")
    assert created["session"]["tools"] == TOOLS and created["session"]["instructions"] == INSTRUCTIONS
    assert next(e for e in client_out[-1]["timeline"] if e["event"] == "routed")["action"] == "delegate"
    assert [tool["name"] for tool in FRONT_TOOLS] == ["confirm", "cancel", "read_screen", "query"]
    # Doubao's 12K context holds the brief, the tools and the history: the front brief stays short too.
    from trader_runtime.voice_front import FRONT_INSTRUCTIONS
    assert len(FRONT_INSTRUCTIONS) <= 450 and all(len(tool["description"]) <= 60 for tool in FRONT_TOOLS)
    await app.state.shared_market.close()
    app.state.store.close()


async def test_product_names_come_from_the_shared_catalog_when_the_catalog_call_returns_nothing(tmp_path, monkeypatch):
    """10-13 production: every call's hotwords were the trading words alone (75, no product name), so the ASR
    had no 焦炭 and the router knew no contract. A catalog call that fails or comes back empty falls back
    to the shared market's public catalog (the same one the route serves) and says why in the log."""
    app, _, service = await _voice_app(tmp_path, "front-catalog.sqlite3")

    original = service._call
    market_reads: list[str] = []

    async def refused(operation, **kwargs):
        if operation == "GET /api/instruments":
            return {"ok": False, "status": 401, "data": {"detail": "voice_identity_required"}}
        if operation == "GET /api/market/{symbol}":
            market_reads.append(str((kwargs.get("path") or {}).get("symbol")))
        return await original(operation, **kwargs)
    monkeypatch.setattr(service, "_call", refused)
    context = await service.asr_context()
    words = [item["word"] for item in context["hotwords"]]
    assert "沪金" in words and "螺纹钢" in words and "螺纹钢" in service.product_names
    # Every voice read that resolves a name shares the same fallback: a named quote still resolves.
    quote = await service.run_tool("query", {"what": "quote", "query": "沪金"}, CallState(window_id=WINDOW))
    assert quote["合约"] == "沪金2612"
    # ...and the quote is read under the catalog's own spelling (never a re-cased copy: 10-13's
    # SHFE.RB2701 broke the paper engine's market subscription).
    assert market_reads == ["SHFE.au2612"]
    await app.state.shared_market.close()
    app.state.store.close()
