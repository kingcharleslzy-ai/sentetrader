"""回答卡片化 (2026-10-14): read results as cards, the answer card's number check, the text cut and the
numeric-claim test behind the 'answered figures without a card' count."""
from __future__ import annotations

import json

from trader_runtime.answer_cards import (
    ANSWER_CARD_DEFINITION,
    ANSWER_CARD_EXAMPLES,
    TurnCards,
    answer_card_wanted,
    conclusion_text,
    has_numeric_claim,
    tool_data,
)
from trader_runtime.contracts import validate_event


def _event(name: str, payload: dict) -> dict:
    return {"schemaVersion": 1, "sessionId": "s", "id": "e", "kind": "artifact", "name": name,
            "timestamp": "2026-10-14T05:00:00Z", "payload": payload}



def test_has_numeric_claim_ignores_times_dates_and_contract_codes():
    assert has_numeric_claim("螺纹现在 3072，跌 0.42%")
    assert has_numeric_claim("涨了 1.5%")
    assert not has_numeric_claim("10-13 夜盘 21:00 开盘，rb2701 两手")
    assert has_numeric_claim("有 2 手持仓，还有 1 笔挂单")
    assert has_numeric_claim("收在 3072.5") and has_numeric_claim("区间 3072-3100")
    assert not has_numeric_claim("不客气，有事随时说。") and not has_numeric_claim("截至 2026.10.14")


def test_conclusion_text_keeps_the_first_two_sentences_and_never_a_table():
    text = "沪金夜盘偏弱，冲高回落。减仓下行。\n\n- 开在 904.36\n| 项 | 值 |\n|---|---|"
    assert conclusion_text(text) == "沪金夜盘偏弱，冲高回落。减仓下行。"
    assert conclusion_text("**短线偏强**") == "短线偏强"


def test_answer_card_is_offered_on_analysis_questions_only():
    wanted = lambda text, kind="general", product=True: answer_card_wanted(  # noqa: E731
        text, route_kind=kind, names_product=product, screen_reference=False)
    assert wanted("沪金夜盘咋样") and wanted("螺纹和热卷哪个强") and wanted("铜最近波动大不大")
    assert wanted("沪铜怎么看", "opinion", False)
    assert wanted("帮我看看玻璃走势", "market_query", False)
    assert not wanted("螺纹现在多少钱", "market_query")         # the quote card answers it
    assert wanted("沪金K线现在能买吗")                            # an analytical buy question (#878 review)
    assert not wanted("沪金能买吗，买两手")                       # an order instruction is not
    assert not wanted("我的持仓怎么样")                           # the positions card answers it
    assert not wanted("黄金最近有啥新闻")                         # the news card answers it
    assert not wanted("策略回测效果怎么样")
    assert not wanted("今天天气怎么样", product=False)
    assert not wanted("你好")


def test_positions_and_orders_become_data_cards_that_validate():
    cards = TurnCards("t1")
    positions = {"dataStatus": "normal", "positions": [
        {"symbol": "SHFE.rb2701", "instrumentId": "rb2701", "direction": "long", "volume": 2, "todayVolume": 1,
         "historicalVolume": 1, "openPrice": 3512.0, "unrealizedPnl": 1240.0},
        {"symbol": "DCE.m2701", "instrumentId": "m2701", "direction": "short", "volume": 1, "unrealizedPnl": -300.0}]}
    card = cards.data_card("get_positions", {}, positions, account_word="本站模拟")
    assert card is not None and card["cardId"] == "data-positions-t1" and card["kind"] == "positions"
    assert [figure["value"] for figure in card["figures"]] == ["+940", "3"]
    assert card["rows"][0]["primary"] == "rb2701 多 2 手" and card["rows"][0]["dir"] == "up"
    assert card["rows"][1]["value"] == "-300" and card["rows"][1]["dir"] == "down"
    assert card["actions"][0]["changes"] == {"panel": "positions"}
    validate_event(_event("card.data", card))
    empty = cards.data_card("get_positions", {}, {"dataStatus": "normal", "positions": []}, account_word="本站模拟")
    assert empty["empty"] == "当前没有持仓" and empty["rows"] == []

    activity = {"dataStatus": "normal", "orders": [
        {"symbol": "SHFE.rb2701", "instrumentId": "rb2701", "direction": "BUY", "offset": "OPEN", "volume": 2,
         "volumeLeft": 1, "limitPrice": 3500.0, "status": "ALIVE"},
        {"symbol": "SHFE.rb2701", "direction": "SELL", "offset": "CLOSE", "volume": 1, "volumeLeft": 0, "status": "FINISHED"}],
        "trades": [{"id": "x", "symbol": "SHFE.rb2701", "direction": "BUY", "offset": "OPEN", "volume": 1, "price": 3500.0,
                    "tradeDateTime": 1760403900000000000}], "protections": [
        {"symbol": "SHFE.rb2701", "positionDirection": "long", "triggerPrice": 3450.0, "takeProfitPrice": 3600.0,
         "remainingVolume": 2, "effective": True},
        {"symbol": "SHFE.rb2701", "positionDirection": "long", "triggerPrice": 3400.0, "effective": False}]}
    orders = cards.data_card("get_account_activity", {}, activity, account_word="本站模拟")
    # Today's fills are on the card one per row (#878 review), not only counted.
    assert [row["tag"] for row in orders["rows"]] == ["委托", "保护", "成交"]
    assert orders["rows"][0]["primary"] == "rb2701 买开 1 手"
    assert orders["rows"][1]["secondary"] == "止损 3450 · 止盈 3600 · 2 手"
    assert [figure["value"] for figure in orders["figures"]] == ["1", "1", "1"]
    validate_event(_event("card.data", orders))


def test_two_analyses_in_a_turn_merge_into_one_card_and_quotes_ride_as_peers():
    cards = TurnCards("t2")
    base = {"durationSeconds": 300, "barCount": 60, "periodReturnPercent": 1.2345, "rangePercent": 2.5,
            "sma5": 3080.12345, "sma20": 3070.5, "volumeRatio": 1.3, "openInterestChange": -1500.0, "trend": "up",
            "capturedAt": "2026-10-14T05:00:00Z", "dataStatus": "normal",
            "indicators": {"rsi": {"value": 61.2, "reading": "中性区间"}, "boll": {"reading": "价格在布林带内运行"}}}
    one = cards.data_card("get_market_analysis", {"symbol": "SHFE.rb2701"}, {**base, "symbol": "SHFE.rb2701"}, account_word="")
    assert one["title"] == "走势 · rb2701 · 5 分钟 · 上行" and one["figures"][0]["value"] == "+1.23%"
    assert {"primary": "RSI", "secondary": "中性区间", "value": "61.2"}.items() <= one["rows"][0].items()
    two = cards.data_card("get_market_analysis", {"symbol": "SHFE.hc2701"}, {**base, "symbol": "SHFE.hc2701", "trend": "down",
                                                                          "periodReturnPercent": -0.5}, account_word="")
    assert two["cardId"] == one["cardId"] and len(two["rows"]) == 2 and two["figures"] == []
    assert two["rows"][1]["dir"] == "down" and two["rows"][1]["tag"] == "下行"
    validate_event(_event("card.data", two))

    snapshot = {"turnId": "t2", "symbol": "SHFE.rb2701", "name": "螺纹钢", "price": 3072.0, "changePercent": -0.42}
    first = cards.market_card(snapshot)
    second = cards.market_card({**snapshot, "symbol": "SHFE.hc2701", "name": "热卷", "price": 3220.0, "changePercent": 0.3})
    assert first["cardId"] == second["cardId"] == "market-t2" and second["symbol"] == "SHFE.rb2701"
    assert second["peers"] == [{"symbol": "SHFE.hc2701", "name": "热卷", "price": 3220.0, "changePercent": 0.3}]


def test_news_reads_merge_and_research_news_becomes_a_news_card():
    cards = TurnCards("t3")
    flash = {"events": [{"title": "美联储降息预期升温", "publishedAt": "2026-10-14T01:05:00Z", "tier": "major", "reports": 3,
                         "ai": {"direction": "偏多", "strength": "中"}}],
             "brief": {"status": "ready", "take": "宏观偏暖"}}
    card = cards.data_card("get_news_flash", {"product": "SHFE.au"}, flash, account_word="")
    assert card["title"] == "快讯 · au" and card["rows"][0]["tag"] == "偏多 · 中" and card["rows"][0]["dir"] == "up"
    assert card["rows"][0]["secondary"] == "09:05 · 重要 · 3 家报道" and card["note"] == "宏观偏暖"
    more = cards.data_card("get_news_flash", {}, {"items": [{"title": "金价创新高", "publishedAt": "2026-10-14T02:00:00Z",
                                                            "source": "金十"}]}, account_word="")
    assert [row["primary"] for row in more["rows"]] == ["金价创新高", "美联储降息预期升温"]
    research = cards.data_card("get_research_brief", {"topic": "news"}, {
        "topic": "news", "status": "ok", "query": "黄金", "results": [{"title": "黄金走强", "media": "东方财富"}]}, account_word="")
    assert research["title"] == "消息面与快讯 · au、全部、黄金" and research["rows"][0] == {"primary": "黄金走强", "secondary": "黄金 · 东方财富"}
    assert cards.data_card("get_research_brief", {"topic": "desk"}, {"topic": "desk"}, account_word="") is None
    validate_event(_event("card.data", research))


def test_answer_card_keeps_only_figures_this_turn_read_back():
    cards = TurnCards("t4")
    cards.record_read("get_market_snapshot", {"symbol": "SHFE.au2612"},
                      json.dumps({"data": {"price": 823.5, "changePercent": 0.68}}))
    card, dropped, error = cards.answer_card({
        "title": "沪金短线", "conclusion": "短线偏强。",
        "figures": ["price", "day", "target"],
        "reasons": ["均线多头", "放量", "外盘走强", "第四条"], "risk": "跌破就失效", "symbol": "SHFE.fake9999"})
    assert error is None and card is not None
    # The Runtime fills each key from this turn's read (value, unit, direction); a field it has no reading of goes.
    assert card["figures"] == [{"label": "最新价", "value": "823.5"}, {"label": "当日涨跌", "value": "+0.68%", "dir": "up"}]
    assert dropped[0].startswith("target：没有字段 target；au2612 本轮可用：")
    # A reading word in a reason's own text (not a key) takes the reason off: tests/answer_corpus has the rest.
    assert dropped[1].startswith("放量：“放量”是读数说法") and cards.rejections == ["ref_field", "text_label"]
    assert card["compare"] is None and card["reasons"] == ["均线多头", "外盘走强"]
    # The model's symbol is not one this turn read: the chart button uses the contract that was read.
    assert card["symbol"] == "SHFE.au2612"
    assert [action["label"] for action in card["actions"]] == ["看图表", "问 AI 怎么办"]
    assert card["basis"]["sources"] == ["行情快照 au2612"]
    validate_event(_event("card.answer", card))
    # Checked is not shown: the conclusion is the turn's answer only once the card is accepted (published).
    assert not cards.answered and cards.conclusion is None
    cards.accept(card)
    assert cards.answered and cards.conclusion == "短线偏强。"
    assert TurnCards("t5").answer_card({"title": "x"})[2] is not None


def test_the_examples_in_the_tool_definition_pass_the_runtime_check_and_a_reading_word_names_its_key():
    """10-17 成本: the model copies the definition's examples, so each one is a card the Runtime takes as it is."""
    def summary(symbol: str, trend: str, ratio: float, interest: float) -> str:
        return json.dumps({"data": {"symbol": symbol, "durationSeconds": 300, "periodReturnPercent": 0.3, "sma5": 10.2,
                                    "sma20": 10.0, "volumeRatio": ratio, "openInterestChange": interest, "trend": trend,
                                    "indicators": {"rsi": {"value": 55.0, "reading": "中性区间"}}}})

    def quote(symbol: str, change: float) -> str:
        return json.dumps({"data": {"symbol": symbol, "price": 10.5, "changePercent": change}})

    single = TurnCards("ex1")
    single.record_read("get_market_snapshot", {"symbol": "SHFE.rb2701"}, quote("SHFE.rb2701", 0.46))
    single.record_read("get_market_analysis", {"symbol": "SHFE.rb2701"}, summary("SHFE.rb2701", "up", 1.26, -18918))
    pair = TurnCards("ex2")
    for symbol, change, trend in (("DCE.i2701", 1.63, "up"), ("DCE.j2701", -0.5, "down")):
        pair.record_read("get_market_snapshot", {"symbol": symbol}, quote(symbol, change))
        pair.record_read("get_market_analysis", {"symbol": symbol}, summary(symbol, trend, 1.6, 100))
    for cards, example in zip((single, pair), ANSWER_CARD_EXAMPLES, strict=True):
        assert json.dumps(example, ensure_ascii=False) in ANSWER_CARD_DEFINITION["description"]
        card, dropped, error = cards.answer_card(example)
        assert error is None and dropped == [] and cards.rejections == [], (error, dropped)
        validate_event(_event("card.answer", card))
    # A reading word in a sentence is sent back with the key that says it, not a fixed example.
    _, _, error = single.answer_card({"title": "螺纹", "conclusion": "螺纹减仓上行"})
    assert error is not None and "{oiDayState=down}或{oiPeriodState=down}" in error and "{trend=up}" in error


def test_tool_data_reads_the_serialized_result():
    assert tool_data(json.dumps({"tool": "get_positions", "data": {"positions": []}})) == {"positions": []}
    assert tool_data("not json") is None


def test_unknown_orders_or_protections_are_never_shown_as_none():
    card = TurnCards("t6").data_card("get_account_activity", {}, {
        "dataStatus": "normal", "ordersInitialized": False, "orders": [], "trades": [], "protections": None}, account_word="")
    assert card["empty"] is None and "读不准" in card["note"] and "保护单暂时读不到" in card["note"]
    assert [figure["value"] for figure in card["figures"]] == ["读不到", "读不到"]
    validate_event(_event("card.data", card))


def test_stale_reads_warn_on_the_card_and_pending_news_stays_hidden():
    cards = TurnCards("t7")
    stale = cards.data_card("get_positions", {}, {"dataStatus": "stale", "positions": []}, account_word="本站模拟")
    assert stale["warning"] == "数据陈旧"
    pending = cards.data_card("get_news_flash", {"product": "SHFE.au"}, {
        "events": [], "items": [{"title": "未筛的原文"}], "screen": {"pending": 7}}, account_word="")
    assert pending["rows"] == [] and pending["empty"] == "AI 正在看新到的 7 条，看完再列出"


def test_news_of_two_products_keeps_each_row_product():
    cards = TurnCards("t8")
    cards.data_card("get_news_flash", {"product": "SHFE.au"}, {"events": [{"title": "金价走强"}]}, account_word="")
    both = cards.data_card("get_news_flash", {"product": "INE.sc"}, {"events": [{"title": "油价回落"}]}, account_word="")
    assert both["title"] == "快讯 · au、sc"
    assert [(row["primary"], row["secondary"]) for row in both["rows"]] == [("油价回落", "sc"), ("金价走强", "au")]


def test_a_long_board_is_not_treated_as_fully_carded_and_signed_figures_set_direction():
    cards = TurnCards("t9")
    board = {"rows": [{"instrumentName": f"品种{i}", "instrumentId": f"x{i}", "changePercent": i} for i in range(15)]}
    card = cards.data_card("get_market_board", {}, board, account_word="")
    assert len(card["rows"]) == 12 and cards.partial == 1 and cards.uncarded == 0
    cards.record_read("get_market_snapshot", {"symbol": "SHFE.au2612"}, json.dumps({"data": {"changePercent": -0.68}}))
    answer, dropped, _ = cards.answer_card({"title": "沪金", "conclusion": "偏弱。", "figures": ["day", "day=up"]})
    assert [figure["dir"] for figure in answer["figures"]] == ["down"] and cards.rejections == ["ref_mismatch"]
    # The three rows beyond the board card stay partial: an answer card does not offset them (#754).
    cards.accept(answer)
    assert cards.partial == 1


def test_truncated_accounts_and_repeated_boards_keep_the_text_and_news_opens_the_news_tab():
    cards = TurnCards("t10")
    many = {"dataStatus": "normal", "positions": [
        {"symbol": f"SHFE.x{i}", "instrumentId": f"x{i}", "direction": "long", "volume": 1, "unrealizedPnl": 1.0}
        for i in range(13)]}
    assert len(cards.data_card("get_positions", {}, many, account_word="")["rows"]) == 12 and cards.partial == 1
    cards.data_card("get_market_board", {}, {"rows": []}, account_word="")
    cards.data_card("get_market_board", {"sort": "volume"}, {"rows": []}, account_word="")
    assert cards.partial == 2
    news = cards.data_card("get_news_flash", {}, {"items": [{"title": "一条"}]}, account_word="")
    assert news["actions"][0]["changes"] == {"news": {"tab": "flash"}}


def test_news_outage_and_many_reads_stay_honest_and_valid():
    cards = TurnCards("t11")
    down = cards.data_card("get_news_flash", {}, {"status": "unavailable", "items": []}, account_word="")
    assert down["empty"] == "快讯暂时读不到，不代表没有消息" and cards.partial == 1
    for index in range(9):
        card = cards.data_card("get_market_analysis", {}, {"symbol": f"SHFE.x{index}", "durationSeconds": 300,
                                                          "periodReturnPercent": 0.1, "trend": "up"}, account_word="")
    assert len(card["basis"]["sources"]) == 8
    validate_event(_event("card.data", card))


def test_a_stale_peer_quote_is_marked_and_keeps_the_text():
    cards = TurnCards("t12")
    cards.market_card({"turnId": "t12", "symbol": "SHFE.rb2701", "name": "螺纹", "price": 3072.0, "changePercent": -0.4,
                       "dataStatus": "normal"})
    card = cards.market_card({"turnId": "t12", "symbol": "SHFE.hc2701", "name": "热卷", "price": 3220.0,
                              "changePercent": 0.3, "dataStatus": "backup"})
    assert card["peers"][0]["dataStatus"] == "backup" and cards.partial == 1


def test_answer_card_carries_read_freshness():
    cards = TurnCards("t13")
    cards.record_read("get_chart_bars", {"symbol": "SHFE.au2612"}, json.dumps({"data": {
        "dataStatus": "stale", "capturedAt": "2026-10-14T01:00:00+00:00", "close": 823.5}}))
    card, dropped, _ = cards.answer_card({"title": "沪金", "conclusion": "偏强。"})
    assert card["warning"] == "数据陈旧" and card["basis"]["at"] == "2026-10-14T01:00:00+00:00" and dropped == []
    validate_event(_event("card.answer", card))


def test_signed_figures_round_before_sign_and_direction():
    from trader_runtime.answer_cards import direction, signed
    assert signed(-0.004, 2, "%") == "0.00%" and direction(-0.004) is None
    assert signed(0.8926578888840929, 2, "%") == "+0.89%" and signed(-1.2, 2, "%") == "-1.20%"
    assert signed(1240.0, 0) == "+1240" and direction(-300) == "down"



def test_facts_read_ignores_searches_and_the_manual():
    cards = TurnCards("t15")
    cards.record_read("search_instruments", {"search": "沪金"}, "{}")
    assert not cards.facts_read
    cards.record_read("get_market_snapshot", {"symbol": "SHFE.au2612"}, "{}")
    assert cards.facts_read


# --- 去重 (2026-10-14 跟进) -----------------------------------------------------------------




def test_the_answer_card_folds_the_read_cards_shown_before_it():
    cards = TurnCards("t23")
    cards.note_card("market-t23")
    cards.note_card("data-analysis-t23")
    cards.note_card("market-t23")  # republished in place (a peer joined): still one card
    card, _, _ = cards.answer_card({"title": "铁矿 vs 焦炭", "conclusion": "铁矿相对抗跌。"})
    assert card["folded"] == ["market-t23", "data-analysis-t23"] and cards.folded == []
    cards.accept(card)
    assert cards.folded == ["market-t23", "data-analysis-t23"]
    validate_event(_event("card.answer", card))
    # A card first shown after the answer card is not on it (the next answer card would fold it).
    cards.note_card("data-news-t23")
    assert cards.folded == ["market-t23", "data-analysis-t23"]


# --- #754 回答卡接受链与小项 (2026-10-15) ---------------------------------------------------------



def test_partial_cards_stay_partial_after_any_answer_card():
    cards = TurnCards("t31")
    cards.market_card({"turnId": "t31", "symbol": "SHFE.x0", "name": "x0", "price": 1.0, "changePercent": 0.1})
    for index in range(1, 7):
        cards.market_card({"turnId": "t31", "symbol": f"SHFE.x{index}", "name": f"x{index}", "price": 1.0, "changePercent": 0.1})
    assert cards.partial == 0
    market = cards.market_card({"turnId": "t31", "symbol": "SHFE.x7", "name": "x7", "price": 1.0, "changePercent": 0.1})
    assert len(market["peers"]) == 6 and cards.partial == 1  # the seventh peer pushed the first one off the card
    card, _, _ = cards.answer_card({"title": "x", "conclusion": "偏强。"})
    cards.accept(card)
    assert cards.answered and cards.partial == 1


def test_merged_news_keeps_each_take_every_source_and_says_what_is_hidden():
    cards = TurnCards("t32")
    cards.data_card("get_news_flash", {"product": "SHFE.au"}, {
        "capturedAt": "2026-10-14T02:00:00+00:00",
        "events": [{"title": "美联储降息", "ai": {"direction": "偏多"}}, {"title": "金价走强"}],
        "brief": {"status": "stale", "take": "宏观偏暖"}}, account_word="")
    both = cards.data_card("get_news_flash", {"product": "SHFE.ag"}, {
        "capturedAt": "2026-10-14T01:00:00+00:00", "dataStatus": "stale",
        "events": [{"title": "美联储降息", "ai": {"direction": "偏空"}}, {"title": "金价走强"}]}, account_word="")
    rows = [(row["primary"], row.get("tag"), row["secondary"]) for row in both["rows"]]
    # The same headline read differently for each product stays once per product; the same take is one row.
    assert rows == [("美联储降息", "偏空", "ag"), ("金价走强", None, "ag、au"), ("美联储降息", "偏多", "au")]
    assert both["warning"] == "数据陈旧、要点陈旧" and both["note"] == "au：宏观偏暖"
    assert both["basis"]["at"] == "2026-10-14T01:00:00+00:00"  # the oldest read, not the last
    validate_event(_event("card.data", both))
    # A third product still being screened shows no row: the merged card marks itself truncated.
    before = cards.partial
    pending = cards.data_card("get_news_flash", {"product": "SHFE.cu"}, {"events": [], "screen": {"pending": 4}}, account_word="")
    assert cards.partial == before + 1 and pending["rows"]
    # Flash and 消息面 together: the title and the two buttons name both.
    mixed = cards.data_card("get_research_brief", {"topic": "news"}, {
        "topic": "news", "status": "ok", "query": "黄金", "results": [{"title": "黄金走强"}]}, account_word="")
    assert mixed["title"].startswith("消息面与快讯 · ")
    assert [action["label"] for action in mixed["actions"]] == ["打开快讯", "打开消息面"]
    assert set(mixed["basis"]["sources"]) == {"快讯（第三方，未经核实）", "调研 · 消息面（第三方报道）"}
    validate_event(_event("card.data", mixed))
    # One read of a stale AI take says so on the card face too.
    single = TurnCards("t33").data_card("get_news_flash", {}, {"events": [{"title": "一条"}],
                                                               "brief": {"status": "stale", "take": "旧要点"}}, account_word="")
    assert single["warning"] == "要点陈旧"


def test_more_than_six_merged_headlines_mark_the_card_truncated():
    cards = TurnCards("t34")
    cards.data_card("get_news_flash", {"product": "SHFE.au"}, {"events": [{"title": f"金{i}"} for i in range(4)]}, account_word="")
    assert cards.partial == 0
    cards.data_card("get_news_flash", {"product": "SHFE.ag"}, {"events": [{"title": f"银{i}"} for i in range(4)]}, account_word="")
    assert cards.partial == 1


def test_the_chart_button_needs_the_whole_contract_code_read():
    cards = TurnCards("t35")
    cards.record_read("get_market_snapshot", {"symbol": "SHFE.au2612"}, json.dumps({"data": {"symbol": "SHFE.au2612", "price": 1}}))
    card, _, _ = cards.answer_card({"title": "沪金", "conclusion": "偏强。", "symbol": "SHFE.au261"})
    assert card["symbol"] == "SHFE.au2612"  # the read contract, not the model's prefix of it
    card, _, _ = cards.answer_card({"title": "沪金", "conclusion": "偏强。", "symbol": "shfe.AU2612"})
    assert card["symbol"] == "SHFE.au2612"


def test_small_card_gaps_of_the_878_reviews():
    from trader_runtime.answer_cards import duration_label
    assert conclusion_text("| 项 | 值 |\n|---|---|\n| 价 | 823.5 |") == ""  # a table only: the caller says the conclusion
    assert duration_label(604800) == "周线"
    board = TurnCards("t36").data_card("get_market_board", {}, {"sort": "openInterest", "rows": [
        {"instrumentName": "螺纹钢2701", "instrumentId": "rb2701", "last": 3100, "openInterest": 1834567, "changePercent": 0.1}]},
        account_word="")
    assert board["rows"][0]["secondary"] == "最新 3100 · 持仓 1834567"
    orders = TurnCards("t37").data_card("get_account_activity", {}, {"orders": [], "protections": [], "trades": [
        {"symbol": "SHFE.rb2701", "direction": "SELL", "offset": "CLOSETODAY", "volume": 2, "price": 3105.0,
         "tradeDateTime": 1760403900000000000}]}, account_word="")
    assert orders["rows"] == [{"primary": "rb2701 卖平 2 手", "secondary": "成交价 3105 · 09:05", "value": "3105", "tag": "成交"}]
    # #932：策略下的单，成交行带来源（get_account_activity 的 sourceLabel），当前对话也认得出是哪个策略。
    strategy = TurnCards("t38").data_card("get_account_activity", {}, {"orders": [], "protections": [], "trades": [
        {"symbol": "SHFE.rb2701", "direction": "BUY", "offset": "OPEN", "volume": 1, "price": 3068.0,
         "tradeDateTime": 1760403900000000000, "sourceLabel": "策略 · 双均线"}]}, account_word="")
    assert strategy["rows"][0]["secondary"] == "成交价 3068 · 09:05 · 策略 · 双均线"
