from __future__ import annotations

import re

import pytest

from trader_runtime.message_context import (
    FuturesWorkspaceContext,
    decode_instruction,
    encode_instruction,
    requires_fresh_market_search,
)


def context() -> FuturesWorkspaceContext:
    return FuturesWorkspaceContext.model_validate({
        "view": "trade",
        "rightDock": "order",
        "symbol": "SHFE.au2612",
        "instrumentName": "沪金2612",
        "durationSeconds": 300,
        "selectedPrice": 823.5,
        "quoteDatetime": "2026-09-02 09:31:02.000000",
        "capturedAt": "2026-09-02T01:31:02Z",
        "ageSeconds": 0.4,
        "dataStatus": "normal",
        "marketSessionStatus": "trading",
        "lastPrice": 823.5,
        "bidPrice1": 823.0,
        "askPrice1": 824.0,
        "priceTick": 0.5,
        "positions": [{
            "symbol": "SHFE.au2612",
            "direction": "long",
            "volume": 2,
            "todayVolume": 1,
            "historicalVolume": 1,
            "frozenToday": 0,
            "frozenHistorical": 0,
            "openPrice": 810.0,
            "margin": 100_000.0,
            "unrealizedPnl": 2_340.0,
        }],
        "selectedPosition": {
            "symbol": "SHFE.au2612", "direction": "long", "volume": 2,
            "todayVolume": 1, "historicalVolume": 1, "openPrice": 810.0,
            "unrealizedPnl": 2_340.0,
        },
        "orderDraft": {
            "side": "buy_open", "volume": 1, "limitPrice": 823.5,
            "source": "context-menu",
        },
        "activeOrderCount": 1,
        "activeOrders": [{
            "clientOrderId": "order-1", "symbol": "SHFE.au2612",
            "direction": "BUY", "offset": "OPEN", "volume": 1,
            "volumeLeft": 1, "filledVolume": 0, "limitPrice": 823.5,
            "status": "ALIVE", "insertDateTime": 1,
        }],
        "recentTrades": [{
            "tradeId": "trade-1", "clientOrderId": "order-0",
            "symbol": "SHFE.au2612", "direction": "BUY", "offset": "OPEN",
            "price": 821.0, "volume": 1, "tradeDateTime": 1,
        }],
        "recentActions": [{
            "action": "position_selected", "occurredAt": "2026-09-02T01:30:00Z",
            "symbol": "SHFE.au2612", "view": "trade", "positionDirection": "long",
        }],
    })


def test_context_instruction_keeps_visible_question_small_and_adds_safety_boundary() -> None:
    encoded = encode_instruction("这张多仓应该平今还是平昨？", context())
    decoded = decode_instruction(encoded)

    assert decoded.user_text == "这张多仓应该平今还是平昨？"
    assert '"symbol":"SHFE.au2612"' in decoded.model_text
    assert '"view":"trade"' in decoded.model_text
    assert '"rightDock":"order"' in decoded.model_text
    assert '"action":"position_selected"' in decoded.model_text
    assert '"clientOrderId":"order-1"' in decoded.model_text
    # Owner 2026-10-16: in the user's own turn the AI never fills an order directly — cards only.
    assert "AI 一律不直接成交" in decoded.model_text and "本轮没有直接下单工具" in decoded.model_text
    assert "先用正式工具重新读取最新" in decoded.model_text
    assert "没有平台默认手数或次数上限" in decoded.model_text
    assert "再次明确说恢复或继续" in decoded.model_text
    assert "不得从普通分析问题中推断恢复" in decoded.model_text
    assert "恢复本身不下单" in decoded.model_text
    assert "UNKNOWN 只查询原 clientOrderId，绝不重新提交" in decoded.model_text
    assert "未经正式历史验证的策略不得启动" in decoded.model_text
    assert "已经验证通过的策略" in decoded.model_text
    assert "直接启动模拟监控，不再询问额外授权" in decoded.model_text
    assert "用户没提到策略的别的请求里不得顺带启动" in decoded.model_text
    assert "不得启动策略自动执行" not in decoded.model_text
    assert "liveTrading 始终 disabled" in decoded.model_text


def test_plain_instruction_remains_backward_compatible() -> None:
    decoded = decode_instruction("打开设置")
    assert decoded.user_text == "打开设置"
    assert decoded.model_text == "打开设置"


@pytest.mark.parametrize("workspace", [None, context()])
def test_message_window_binding_survives_encoding_without_changing_market_context(workspace):
    from uuid import uuid4
    window = str(uuid4())
    decoded = decode_instruction(encode_instruction("打开设置", workspace, window))
    assert decoded.ui_window_id == window
    assert decoded.user_text == "打开设置"
    assert window in decoded.model_text
    assert decoded.requires_web_search is False


def test_plain_fresh_market_question_flags_freshness_for_post_hoc_check() -> None:
    decoded = decode_instruction("螺纹2610怎么看？")
    assert decoded.user_text == "螺纹2610怎么看？"
    assert decoded.requires_web_search is True
    assert "由你自行判断是否需要以及需要哪种核验" in decoded.model_text


def test_meta_questions_about_the_app_bypass_the_freshness_gate() -> None:
    for question in ("你现在是什么模型？", "你用的什么模型", "软件是什么版本？", "你是谁？"):
        decoded = decode_instruction(question)
        assert decoded.requires_web_search is False, question
        assert decoded.model_text == question or "新鲜度" not in decoded.model_text


def test_context_instruction_uses_semantic_intent_and_silent_fresh_search_policy() -> None:
    encoded = encode_instruction("螺纹2610怎么看？", context())
    decoded = decode_instruction(encoded)

    assert "不要依赖固定问法或关键词表" in decoded.model_text
    assert "按问题的完整语义判断意图" in decoded.model_text
    assert "由你自行判断需要" in decoded.model_text
    assert "当前交易日或过去24小时" in decoded.model_text
    assert "没有合格信息时直接省略新闻内容" in decoded.model_text
    assert "不要说搜索过、没搜到、暂无新闻或任何搜索状态" in decoded.model_text
    assert "两者都没有时明确说明缺少最新信息" in decoded.model_text
    assert "每段只保留会改变判断或操作的内容" in decoded.model_text
    assert "通常四到七个短句足够，不是强制字数上限" in decoded.model_text
    assert "现实市场的价格或新闻不能用于印证造数图表" in decoded.model_text
    assert "不得凭经验估算" in decoded.model_text
    assert "成交量和持仓量变化也不能被改写成" in decoded.model_text
    assert "不得重复调用同类工具" in decoded.model_text
    assert "最后一个‘来源’短段落" in decoded.model_text
    assert decoded.requires_web_search is True
    assert "由你自行判断是否需要以及需要哪种核验" in decoded.model_text
    assert "不得承诺等待分钟数" in decoded.model_text
    # Question routing: how-to reads the manual, live facts read tools, research
    # questions take the research brief, external info searches, common sense
    # answers directly.
    assert "先用 app_capabilities 读取操作手册" in decoded.model_text
    assert "先调用 get_research_brief（topic=news）再引用" in decoded.model_text
    assert "用 get_research_brief 的对应主题" in decoded.model_text
    assert "直接回答，不需要联网或读数" in decoded.model_text


def test_fresh_market_search_classifier_is_intent_based_and_honors_opt_out() -> None:
    assert requires_fresh_market_search("你对我这一手操作怎么看？") is True
    assert requires_fresh_market_search("这个持仓什么时候平？") is True
    assert requires_fresh_market_search("只看技术面，螺纹现在怎么走") is False
    assert requires_fresh_market_search("打开设置") is False


@pytest.mark.parametrize("question", [
    "现在急停", "请撤掉当前全部委托", "取消今天挂的订单",
    "立即停止当前模拟执行", "先暂停当前策略监控",
])
def test_direct_risk_controls_do_not_wait_for_news(question: str) -> None:
    for workspace in (None, context()):
        decoded = decode_instruction(encode_instruction(question, workspace))
        assert decoded.user_text == question
        assert decoded.requires_web_search is False


@pytest.mark.parametrize("question", [
    "现在是否应该急停？", "为什么今天撤单？", "撤单后行情会涨吗？",
    "先急停，再分析今天走势", "暂停策略后现在应该如何开仓？",
])
def test_risk_control_mentions_do_not_exempt_market_advice(question: str) -> None:
    assert requires_fresh_market_search(question) is True


def test_voice_turn_instruction_asks_for_a_spoken_first_paragraph_and_request_only_annotations() -> None:
    from trader_runtime.message_context import decode_instruction, encode_instruction
    decoded = decode_instruction(encode_instruction("讲讲这段走势", None, None, origin="voice"))
    assert decoded.origin == "voice"
    for fragment in ("第一段用口语直接回答", "用户要求标注、画图或讲解某段走势时才调 annotate_chart", "一轮内只调一次",
                     "不调 get_market_snapshot、get_futures_decision_context", "查持仓只调 get_positions"):
        assert fragment in decoded.model_text
    follow = decode_instruction(encode_instruction("那 MACD 呢", None, None, origin="voice", follow_up=True))
    assert "优先回答这句" in follow.model_text and "优先回答这句" not in decoded.model_text


def test_order_words_carry_no_keyword_hint_any_more() -> None:
    """意图重构第 3 步: the four keyword-decided order hints (批量平仓 / 开仓 / 平一个持仓 / 目标持仓) are gone — the
    typed router reads order words once with the order-intent parser, and the trading AI has one card tool. Neither
    voice nor typed turns carry any of the old instructions or tool names."""
    from trader_runtime.message_context import decode_instruction, encode_instruction
    for said in ("把持仓全部平掉", "搞一手螺纹多的", "平一半", "把螺纹调到三手多单"):
        for origin in ("voice", None):
            text = decode_instruction(encode_instruction(said, context() if origin is None else None, None,
                                                         origin=origin)).model_text
            assert "本轮是开仓请求" not in text and "本轮是目标持仓请求" not in text and "本轮请直接用" not in text, said
            assert not re.search(r"create_(trade|close_batch|target_position)_proposal|ask_order_details", text), said


def test_a_market_question_accepts_a_catalog_productId_in_either_case() -> None:
    """09-20 eval guard-10: "IF现在多少钱" names the product by its code; the router catalog carries
    Chinese names and productId codes, matched case-insensitively (the ASR may lower them)."""
    from trader_runtime.message_context import market_query_request
    names = ["沪深300股指", "铁矿石", "沪金", "螺纹钢", "IF"]
    for said in ("IF现在多少钱", "if现在多少钱", "沪金现在多少钱"):
        assert market_query_request(said, names), said
    assert not market_query_request("棉花现在多少钱", names)  # not in the catalog: the model decides


def test_order_requests_are_one_round_and_the_model_is_not_asked_to_read_the_screen() -> None:
    """Round reduction (2026-09-19): the card tool (propose_order since 意图重构第 3 步) runs the readiness gate
    itself, and the window snapshot is attached by Runtime when the sentence points at the screen."""
    from trader_runtime.message_context import decode_instruction, encode_instruction
    voice = decode_instruction(encode_instruction("来一手沪金多的", None, None, origin="voice")).model_text
    assert "下单直接调 propose_order 出卡" in voice and "不先调 get_futures_decision_context" in voice
    assert "最多先调一次 get_futures_decision_context" not in voice
    assert "先读本对话窗口的页面快照" not in voice
    typed = decode_instruction(encode_instruction("来一手沪金多的", context(), None)).model_text
    assert "本轮直接调用 propose_order 出卡" in typed
    assert "先用 app_call 读本次对话窗口的页面快照" not in typed


def test_screen_reference_is_decided_from_the_words() -> None:
    from trader_runtime.message_context import screen_reference
    for said in ("这个数是多少", "屏幕上写的啥", "现在显示的是哪个合约", "我看到有条红线", "这张卡什么意思", "这里怎么点", "这条线是啥", "讲讲这根线", "这波是不是要跌"):
        assert screen_reference(said), said
    for said in ("沪金现在多少钱", "全部平仓", "帮我看看螺纹", ""):
        assert not screen_reference(said), said


def test_screen_snapshot_instruction_is_bounded_and_names_focus_dialog_and_targets() -> None:
    from trader_runtime.message_context import SCREEN_SNAPSHOT_CHARS, screen_snapshot_instruction
    window = {"windowId": "w", "online": True,
              "state": {"view": "trade", "symbol": "SHFE.au2612", "duration": 300, "panel": "order", "dialog": "settings",
                        "settingsOpen": True, "settingsPage": "trade", "positionsBar": "collapsed"},
              "observation": {"activeDialog": "设置", "text": "沪金2612  823.5  " + "持仓 多 2 手 " * 400,
                              "context": {"highlightTargets": ["rail:order", "positions-bar"]}}}
    line = screen_snapshot_instruction(window)
    assert line.startswith("页面快照（本对话窗口现在显示的内容，系统已附上；不要再用 app_call 读窗口快照")
    assert "焦点 view=trade；symbol=SHFE.au2612；duration=300；panel=order；dialog=settings；settingsOpen=True；settingsPage=trade；positionsBar=collapsed。" in line
    assert "当前弹窗 设置。" in line and "可高亮的界面目标 rail:order、positions-bar。" in line
    assert "可见文字：沪金2612 823.5 持仓 多 2 手" in line and line.endswith("…\n")
    assert len(line) <= SCREEN_SNAPSHOT_CHARS + 1
    assert screen_snapshot_instruction({"state": {}, "observation": {"text": ""}}).startswith("页面快照（")


def test_screen_snapshot_focus_names_the_news_tab_and_flash_filter() -> None:
    from trader_runtime.message_context import screen_snapshot_instruction
    window = {"state": {"view": "trade", "panel": "news", "news": {"tab": "flash", "flashFilter": "macro"}},
              "observation": {"text": "快讯 美联储官员"}}
    assert "焦点 view=trade；panel=news；news.tab=flash；news.flashFilter=macro。" in screen_snapshot_instruction(window)
    window["state"]["news"] = {"tab": "desk", "flashFilter": "product"}
    assert "panel=news；news.tab=desk。" in screen_snapshot_instruction(window)


def test_screen_snapshot_focus_names_the_open_reminder_center() -> None:
    """提醒中心 (2026-10-15): open, its filter and the unread count ride the focus line; closed, nothing."""
    from trader_runtime.message_context import screen_snapshot_instruction
    center = {"open": True, "filter": "unread", "unread": 6, "today": 13, "earlier": False}
    window = {"state": {"view": "trade", "reminderCenter": center}, "observation": {"text": "提醒中心"}}
    assert "焦点 view=trade；reminderCenter.filter=unread；reminderCenter.unread=6。" in screen_snapshot_instruction(window)
    window["state"]["reminderCenter"] = center | {"open": False}
    assert "reminderCenter" not in screen_snapshot_instruction(window)
    # #932 R5: a day past its first 500 says so, so “还有更多 / 接着看” reaches reminderCenter {more: true}.
    window["state"]["reminderCenter"] = center | {"moreToday": True}
    assert "reminderCenter.unread=6；reminderCenter.moreToday=true。" in screen_snapshot_instruction(window)


def test_reminder_center_more_is_a_typed_command_applied_on_the_open_center() -> None:
    """#932 R5 (Codex r4307948004): 继续加载当天提醒 is a UI command — accepted by the schema, applied once the center
    is open (the read follows and moreToday / today report it), never on a closed one; its receipt says what it did."""
    from trader_runtime.ui_control import ReminderCenterState, UiChanges, changes_match, describe_ui_change
    assert UiChanges.model_validate({"reminderCenter": {"more": True}}).reminderCenter.more is True
    with pytest.raises(ValueError):
        UiChanges.model_validate({"reminderCenter": {"more": False}})
    assert ReminderCenterState(open=True, filter="all", unread=0, today=500, moreToday=True).moreToday
    # Codex r4308006076: applied only once this command's read landed (moreDone caught up, no moreError).
    center = {"open": True, "filter": "all", "unread": 2, "today": 500, "moreToday": True, "moreAsked": 3, "moreDone": 2,
              "moreError": ""}
    assert not changes_match({"reminderCenter": center}, {"reminderCenter": {"more": True}})
    assert not changes_match({"reminderCenter": center | {"moreDone": 3, "moreError": "当天提醒刚刷新过，再点一次接着读"}},
                             {"reminderCenter": {"more": True}})
    opened = {"reminderCenter": center | {"moreDone": 3}}
    assert changes_match(opened, {"reminderCenter": {"more": True}})
    # A client before these fields: its open center stands.
    assert changes_match({"reminderCenter": {"open": True, "filter": "all", "unread": 0, "today": 1}}, {"reminderCenter": {"more": True}})
    assert changes_match(opened, {"reminderCenter": {"more": True, "filter": "all"}})
    assert not changes_match(opened, {"reminderCenter": {"more": True, "filter": "unread"}})
    assert not changes_match({"reminderCenter": {**opened["reminderCenter"], "open": False}}, {"reminderCenter": {"more": True}})
    assert describe_ui_change({"reminderCenter": {"more": True}}) == "继续加载当天提醒"


def test_screen_snapshot_carries_the_open_strategy_editors_flow() -> None:
    """策略编辑卡第二版: “这个止损触发了几次” points at the card's 流程图 — its blocks and the preview's counts ride
    along (bounded); counts the card does not have are not written, a closed card adds nothing."""
    from trader_runtime.message_context import EDITOR_FLOW_CHARS, screen_snapshot_instruction
    flow = {"basis": "近 60 个交易日", "trigger": {"id": "trigger", "title": "每根 5分 K 线收盘", "count": None},
            "entries": [{"id": "entryLong", "title": "开多 · 1 条", "count": 23}], "action": {"id": "action", "title": "开多 1 手", "count": 23},
            "exits": [{"id": "stop", "title": "止损 15 跳", "count": 12}, {"id": "take", "title": "止盈", "count": None, "pending": True}], "notes": []}
    window = {"state": {"panel": "strategy", "strategyEditor": {"open": True, "name": "突破", "flow": flow}}, "observation": {"text": ""}}
    line = screen_snapshot_instruction(window)
    assert ("strategyEditor=突破 流程（近 60 个交易日）：每根 5分 K 线收盘 → 开多 · 1 条 23 次 → 开多 1 手 23 笔 → 止损 15 跳 12 次 / 止盈（待填）") in line
    window["state"]["strategyEditor"] = {**window["state"]["strategyEditor"], "flow": {**flow, "exits": [{"id": "stop", "title": "止" * 900, "count": 1}]}}
    assert len(screen_snapshot_instruction(window, focus_only=True)) < EDITOR_FLOW_CHARS + 200
    window["state"]["strategyEditor"] = {"open": False}
    assert "strategyEditor" not in screen_snapshot_instruction(window)


def test_focus_only_snapshot_line_carries_just_the_focus_fields() -> None:
    from trader_runtime.message_context import screen_snapshot_instruction
    window = {"state": {"view": "trade", "symbol": "SHFE.rb2610", "duration": 60}, "observation": {"text": "很多可见文字", "activeDialog": "设置"}}
    line = screen_snapshot_instruction(window, focus_only=True)
    assert line == "当前窗口焦点（系统已附上）：view=trade；symbol=SHFE.rb2610；duration=60\n"
    assert screen_snapshot_instruction({"state": {}, "observation": {}}, focus_only=True).endswith("：无\n")


def test_a_push_to_talk_turn_is_spoken_but_carries_none_of_the_call_rules() -> None:
    """按住说话 (09-28): answered with a spoken first paragraph like a call turn, but no call is running —
    the card is confirmed on the card, and the call-only preference lines do not apply."""
    from trader_runtime.user_preferences import UserPreferences, preference_instruction
    decoded = decode_instruction(encode_instruction("买一手螺纹", None, None, origin="ptt"))
    assert decoded.origin == "voice" and decoded.push_to_talk is True and decoded.user_text == "买一手螺纹"
    assert decoded.model_text.startswith("本轮问题是用户按住说话说出来的") and "实时语音通话" not in decoded.model_text
    assert "没有口头确认" in decoded.model_text and "用户会在语音里说“确认”" not in decoded.model_text
    call = decode_instruction(encode_instruction("买一手螺纹", None, None, origin="voice"))
    assert call.push_to_talk is False and "用户会在语音里说“确认”" in call.model_text
    free = UserPreferences.model_validate({"trade": {"voiceOrderConfirm": False}})
    assert "不要问“确认吗”" in preference_instruction(free, origin="voice")
    assert "不要问“确认吗”" not in preference_instruction(free, origin=None)  # what a push-to-talk turn gets
