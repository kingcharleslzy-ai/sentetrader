"""The typed fuzzy fast path's routing table, asserted over the whole in-repo fuzzy test set
(eval/fuzzy/testset.py) with the fake data provider's catalog — no model, no window, no store.
Every sentence the evaluation will send is pinned here to exactly one route and one tool subset."""
from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path
from typing import Any

import pytest

from trader_runtime.order_intent_parse import states_order
from trader_runtime.turn_router import (
    GENERAL_ROUTE,
    MARKET_QUERY_TOOLS,
    ORDER_TOOLS,
    UI_FALLBACK_TOOLS,
    classify_turn,
    run_window_intent,
    strip_filler_prefix,
)
from trader_runtime.voice_intents import Intent, intent_for_window, match_product_word

# FakeReadOnlyDataProvider's product names (Chinese names plus the productId IF — the Runtime's
# router catalog carries both, so "IF现在多少钱" is a market question): the catalog the router
# matches market questions against.
FAKE_CATALOG = ["沪深300股指", "铁矿石", "沪金", "螺纹钢", "IF"]

_spec = importlib.util.spec_from_file_location(
    "fuzzy_testset", Path(__file__).resolve().parents[1] / "eval" / "fuzzy" / "testset.py")
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
TESTSET = _module.T


def _route_for(item):
    expect = item["expect"]
    return classify_turn(item["text"], has_pending_card=bool(expect.get("pending")),
                         ui_available=True, catalog=FAKE_CATALOG)


def test_testset_covers_all_categories():
    assert len(TESTSET) >= 110
    categories = {item["category"] for item in TESTSET}
    assert {"query", "ui", "order", "guard", "pref"} <= categories


def test_match_product_word_counts_hit_products_not_spoken_words():
    """A shared prefix names two products, not one (第二期审查): "中证" answers to both 中证500股指
    and 中证1000股指, "菜籽" to both 菜籽油 and 菜籽粕 — pre-resolution must stay out and leave the
    sentence to the model's own search instead of injecting one of them. A prefix that only occurs
    inside another product's whole-name hit is not a second name, and one product hit by both its
    whole name and an alias still counts once."""
    index = ["中证500股指", "中证1000股指"]
    assert match_product_word("中证现在多少钱", index) is None
    oils = ["菜籽油", "菜籽粕"]
    assert match_product_word("菜籽咋样", oils) is None
    assert match_product_word("菜籽油咋样", oils) == ("菜籽油", "菜籽油")
    assert match_product_word("金子现在多少钱，沪金呢", ["沪金"]) == ("沪金", "沪金")


def test_a_number_before_a_unit_never_names_20号胶():
    """Production 2026-09-27: "止损20跳" in the guide's first answer resolved to 20号胶 because "20"
    was taken as a prefix of the name. A digit-led product answers only to its whole name; sizes such
    as 20跳 / 20手 / 20根 / 20点 name nothing, and the 策略编辑卡's 补一句 still reads "20手" as a field."""
    from trader_runtime.strategy_editor import editor_utterance, names_contract
    catalog = ["螺纹钢", "20号胶", "天然橡胶"]
    for text in ("5分钟均线金叉就开多，死叉平仓，止损20跳", "每次20手", "看20根K线的高点", "止损20点", "止盈20%"):
        assert match_product_word(text, catalog) is None, text
        assert not names_contract(text, catalog), text
    assert editor_utterance("20手", catalog)
    assert match_product_word("20号胶现在多少钱", catalog) == ("20号胶", "20号胶")
    assert match_product_word("做20号胶主力，止损20跳", catalog) == ("20号胶", "20号胶")
    assert match_product_word("螺纹止损20跳", catalog) == ("螺纹", "螺纹钢")


def test_every_sentence_takes_its_expected_route():
    # Only sentences that pin a route (the handwritten set and the skill positives); registry
    # counter examples deliberately leave it unset — they pin tool ownership, not the routing table.
    mismatches = [(item["id"], item["text"], _route_for(item).kind, item["expect"].get("route"))
                  for item in TESTSET
                  if item["expect"].get("route") and _route_for(item).kind != item["expect"]["route"]]
    assert not mismatches, mismatches


def test_order_routes_plan_sure_orders_and_hand_the_rest_to_propose_order():
    """意图重构第 3 步: an order the parser is sure of is planned by the Runtime with no model round (`order_intent`,
    the parser's intent on the route); an order it could not settle is the trading AI's round with its one card
    tool, the contract search and the positions read — no market, K-line or web tool rides along."""
    sure = [item for item in TESTSET if item["expect"].get("route") == "order_intent"]
    assert len(sure) >= 25
    for item in sure:
        route = _route_for(item)
        assert route.order is not None and route.tool_names is None and not route.no_card, item["text"]
    assert ORDER_TOOLS == {"propose_order", "search_instruments", "get_positions"}
    for text in ("持仓两手的多单平掉一手", "帮我把这个多单平了", "平仓", "买两手螺纹然后再买一手"):
        route = classify_turn(text, has_pending_card=False, ui_available=True, catalog=FAKE_CATALOG)
        assert route.kind == "order" and route.tool_names == ORDER_TOOLS and route.order is None, text
        assert route.one_round and route.temperature == 0.0 and route.reasoning == "minimal"


# 对话面板的示例说法（apps/client/src/components/futures/chatExamples.ts，2026-10-03）：按页面给用户的
# 每一句都必须是现有技能能办的话。不带价位的是所属技能 SKILL.md 的典型说法原文；K 线两句带客户端按
# 最新价取整的价位，是 price-alerts / chart-drawings 典型说法去掉品种名后的形状。路由钉在这里，改一边要改另一边。
CHAT_EXAMPLES = [
    # K 线图（有报价 / 无报价）
    ("帮我看看KD和MACD", "chart-analysis", "general"),
    ("涨到 835 提醒我", None, "general"),
    ("帮我在 815 画一条线", None, "general"),
    ("均线现在是什么排列", "chart-analysis", "general"),
    ("我有哪些到价提醒", "price-alerts", "general"),
    ("我画的线被突破了吗", "chart-drawings", "general"),
    # 报价总表
    ("报价表按成交量排", "preferences", "general"),
    ("只看自选", "ui-control", "ui_intent"),
    ("今天哪些品种涨得最多", "market-lookup", "general"),
    # 复盘页
    ("帮我复盘一下今天", "review-summary", "general"),
    ("今天赚了还是亏了", "review-summary", "general"),
    ("回测一下这个策略", "strategy-backtests", "general"),
    # 策略面板
    ("帮我设计一个策略", "strategy-management", "ui_intent"),
    ("我的策略出信号了吗", "strategy-signals", "general"),
    # 持仓面板
    ("我手里有什么持仓", "review-summary", "market_query"),
    ("给这个持仓挂个止损", "order-ops", "general"),
    ("挂单成交了吗", "order-ops", "general"),
]


@pytest.mark.parametrize("text,skill,route", CHAT_EXAMPLES)
def test_chat_examples_are_skill_phrases_on_their_route(text, skill, route):
    from trader_runtime.skill_registry import all_skills
    phrases = {item.skill_id: item.phrases for item in all_skills()}
    if skill:
        assert text in phrases[skill], text
    else:
        # 价位句：同形的典型说法在 price-alerts / chart-drawings 里（只差品种名与价位）
        shape = re.sub(r"\d+", "N", text)
        assert any(shape in re.sub(r"\d+", "N", phrase) for owner in ("price-alerts", "chart-drawings")
                   for phrase in phrases[owner]), text
        assert not states_order(text, FAKE_CATALOG), text
    result = classify_turn(text, has_pending_card=False, ui_available=True, catalog=FAKE_CATALOG)
    assert result.kind == route, text
    if text == "只看自选":
        assert result.intent.changes == {"view": "board"} and result.intent.follow == [{"boardExchange": "watchlist"}]


@pytest.mark.parametrize("text", [
    "开一手螺纹要多少保证金",
    "我最多还能开几手螺纹",
    "买两手手续费几块",
    "开一手螺纹大概要多少钱",
    "开两手螺纹的手续费是多少",
])
def test_money_questions_are_not_order_turns(text):
    """问“钱”不是下单：这些句子带着开仓动词和手数，但答案是保证金/手续费/可开手数，
    由通用回合用 estimate_order 回答，绝不出卡（解析器读成问句，出卡工具不给）。"""
    route = classify_turn(text, has_pending_card=False, ui_available=True, catalog=FAKE_CATALOG)
    assert route.kind == "general" and route.no_card and not states_order(text, FAKE_CATALOG)


def _kind(text: str, **kwargs: Any) -> str:
    return classify_turn(text, has_pending_card=False, ui_available=True, catalog=FAKE_CATALOG, **kwargs).kind


def test_close_all_and_cancel_all_wording_is_not_interface_work():
    """#713: 「关掉/关闭 + 所有/全部/都 + 持仓/仓位」口语是批量平仓（仍出卡确认），解析器读出 close_all 就直接交规划器；
    「关闭全部委托」类说法是撤单，交给有全部工具的通用回合；只说「关掉持仓 / 关闭持仓面板」仍是收起面板的界面话。"""
    for said in ("关掉所有持仓", "把持仓都关了", "关闭所有仓位", "持仓全部关掉", "把仓位都关掉", "关掉所有持仓吧",
                 "关闭所有仓位。", "关掉所有持仓一下", "关掉所有持仓了吧", "关掉所有持仓哦"):
        route = classify_turn(said, has_pending_card=False, ui_available=True, catalog=FAKE_CATALOG)
        assert route.kind == "order_intent" and route.order.act == "close_all" and route.order.symbol is None, said
    for said in ("关闭全部委托", "把委托都关了", "挂单都撤了", "关掉所有委托"):
        route = classify_turn(said, has_pending_card=False, ui_available=True, catalog=FAKE_CATALOG)
        assert route.kind == "general" and route.tool_names is None and not route.no_card, said
    # #722: 「关」也是收起面板、关提醒的动词——持仓后面还跟着别的词，解析器就不认，绝不是确定性平仓卡。
    for said in ("关掉所有持仓的面板", "关掉所有持仓的窗口", "关闭所有持仓提醒", "关闭所有持仓通知",
                 "关掉全部持仓预警", "退出所有持仓视图", "关掉所有持仓列表", "关闭全部持仓图层",
                 "关掉所有持仓的显示", "持仓都关上面板", "关掉所有持仓线", "关掉所有持仓标签",
                 "关掉所有持仓里盈利的", "关掉所有仓位里螺纹的"):
        assert _kind(said) != "order_intent", said
    # 客气的句尾（好吗 / 可以吗 / 好不好）：命令还是问句，解析器不猜，交通用回合（有出卡工具，模型判断）。
    for said in ("关掉所有持仓好吗", "关掉所有持仓可以吗", "把持仓都关了好不好", "关掉所有持仓好吗？"):
        route = classify_turn(said, has_pending_card=False, ui_available=True, catalog=FAKE_CATALOG)
        assert route.kind == "general" and not route.no_card, said
    for said in ("关掉持仓", "关闭持仓面板", "关掉所有持仓面板", "打开全部委托", "看看所有持仓", "显示所有持仓",
                 # #738 审查: 界面对象带指示/位置修饰时句子的实际对象是界面。
                 "关掉所有持仓这个面板", "关掉所有持仓那个面板", "关掉所有持仓右边的栏", "关掉所有持仓那栏",
                 "关掉所有持仓旁边那个列表", "关掉所有持仓我眼前的这个面板",
                 # 平仓记录 / 清单是界面名词，不是平仓动词。
                 "打开所有持仓清单", "打开所有持仓平仓记录", "关掉所有持仓清单", "关掉所有持仓平仓记录",
                 "关掉所有持仓页", "关掉所有持仓页面"):
        assert _kind(said) == "ui_intent", said
    for said in ("打开所有持仓清单", "关掉所有持仓平仓记录"):
        assert _kind(said, editor_open=True) == "ui_intent", said
    # 混合交易+界面的复合句、带场景的清仓话：解析器不认，交给交易 AI（有出卡工具），不退回 UI-only。
    for said in ("关掉所有持仓然后打开面板", "关掉所有持仓再看看螺纹", "关掉所有持仓并买一手螺纹再打开面板",
                 "在持仓面板里把所有仓位都关掉", "平仓所有持仓然后看平仓记录", "把所有持仓平掉然后看平仓记录"):
        route = classify_turn(said, has_pending_card=False, ui_available=True, catalog=FAKE_CATALOG)
        assert route.kind == "general" and not route.no_card, said
    assert _kind("在持仓面板里把所有仓位都关掉", editor_open=True) == "general"


def test_negated_questioned_or_conditional_order_wording_never_makes_a_card():
    """#754/#510 → 意图重构第 3 步: a negation, a question, a report or a condition is no instruction — the parser
    reads the stance, the turn answers and its card tool is withheld (a voice call without spoken confirmation
    would otherwise confirm the card at once). The plain orders still card, straight from the planner."""
    for said in ("不要全部平仓", "别全部平掉", "能不能全部平仓", "先不清仓", "我不想全平", "持仓不用都平了",
                 "要不要全部平仓", "该不该清仓", "全部平仓吗", "都平了吗", "为什么全平了",
                 "全部平仓要多少手续费", "全部平仓？", "如果跌破3000就全部平仓", "收盘前全部平仓", "涨到3500的话全平",
                 "明天开盘清仓", "等下把持仓全部平掉", "不要平掉螺纹", "螺纹平了吗", "我已经平了", "别把螺纹平了",
                 "螺纹买两手吗", "能不能买两手螺纹", "跌破3000就买两手螺纹", "我刚买了两手螺纹", "螺纹买两手，算了",
                 "全部平仓，算了", "做到3100就买两手"):
        route = classify_turn(said, has_pending_card=False, ui_available=True, catalog=FAKE_CATALOG)
        assert route.kind in {"general", "opinion"} and route.no_card and route.order is None, said
    conditional = classify_turn("跌破3000就买两手螺纹", has_pending_card=False, ui_available=True, catalog=FAKE_CATALOG)
    assert "价格提醒" in conditional.hint and conditional.mount == "price-alerts"
    timed = classify_turn("明天开盘清仓", has_pending_card=False, ui_available=True, catalog=FAKE_CATALOG)
    assert "到时候再说一遍" in timed.hint and timed.mount is None
    for said in ("全部平仓", "清仓", "把持仓全部平掉", "平掉所有空单", "都给我平了", "平掉螺纹", "把沪金平了"):
        route = classify_turn(said, has_pending_card=False, ui_available=True, catalog=FAKE_CATALOG)
        assert route.kind == "order_intent" and route.order.act == "close_all", said


def test_close_all_wording_with_tails_is_never_interface_work():
    """#730: 清仓口语带句尾（啦、呗、谢谢你、马上、两个合约、一分钟后）从来不是界面话：解析器认得的（呗）直接交规划器，
    不认得的交给交易 AI 的通用回合（有出卡工具），都不进 UI 兜底。"""
    from trader_runtime.voice_intents import looks_like_ui
    bases = ["关掉所有持仓", "关闭所有仓位", "退出所有持仓", "把持仓都关掉", "持仓全部关掉", "全部仓位都关掉"]
    tails = ["啦", "呗", "嘞", "吧拜托", "马上", "立刻", "好的", "嗯", "，谢谢你", "谢谢您", "两个合约", "一分钟后"]
    for said in [base + tail for base in bases for tail in tails] + ["帮我关掉所有持仓好的", "把仓位都关掉啦"]:
        assert looks_like_ui(said, FAKE_CATALOG) is False, said
        assert _kind(said) in {"general", "order_intent"}, said


@pytest.mark.parametrize("said", ["在设置里关掉自主执行", "打开设置把自主执行关掉", "语音下单免口头确认打开",
                                  "把运行中的策略执行方式切到只提醒", "把策略切到自动下单", "开始监控螺纹"])
def test_trading_controls_are_never_interface_work(said):
    """#932 复核: AI 自主执行, 语音下单免口头确认 and a strategy's 执行方式 (or its start) change only through
    update_preferences / start_strategy_monitoring, never a UI command — a request for one goes to the general turn
    that has those tools."""
    from trader_runtime.voice_intents import looks_like_ui
    assert looks_like_ui(said, FAKE_CATALOG) is False
    assert _kind(said) == "general"


def test_target_position_sentences_are_planned_with_their_net_target():
    """“把螺纹调到三手多单 / 做到多 2 手 / 加到 / 减到 / 清零” state the net position to hold: the parser reads the
    target and the planner computes the difference (#940: “做到多 2 手” is no 做多 2 手). A close with a stated
    size, a P&L question and a pending-card follow-up are not targets."""
    for said, net in (("螺纹做到多 2 手，挂买一价", 2), ("螺纹做到空1手，挂卖一价", -1), ("螺纹加到三手多单", 3),
                      ("空单减到一手", -1), ("把螺纹调到三手多单", 3), ("把沪金的目标持仓清零", 0)):
        route = classify_turn(said, has_pending_card=False, ui_available=True, catalog=FAKE_CATALOG)
        assert route.kind == "order_intent" and route.order.act == "target" and route.order.target == net, said
    for said, expected in (
        ("持仓两手的多单平掉一手", "order"),      # two lot counts: the trading AI reads it
        ("我现在持有两手多单，亏了多少", "general"),  # a question about existing holdings
        ("改成三手", "general"),                 # a pending-card follow-up shape
        ("什么是目标持仓", "general"),            # asking what the feature is
        ("搞一手螺纹多的", "order_intent"),
        ("做多两手螺纹", "order_intent"),
        ("做到3100就买两手", "general"),         # a conditional order, not a target: no card now
    ):
        assert _kind(said) == expected, said


def test_market_query_route_reads_but_never_searches_the_web():
    routed = [item for item in TESTSET if item["expect"].get("route") == "market_query"]
    assert len(routed) >= 15
    for item in routed:
        route = _route_for(item)
        assert route.tool_names == MARKET_QUERY_TOOLS, item["text"]
        assert "web_search" not in route.tool_names and "get_research_brief" not in route.tool_names
        assert route.temperature == 0.0 and route.reasoning == "minimal"
        assert not route.one_round  # a read may still take a search round, then the answer


def test_general_leaves_the_full_toolbox():
    for item in TESTSET:
        if item["expect"].get("route") == "general":
            route = _route_for(item)
            assert route.tool_names is None and route.intent is None, item["text"]
            assert route is not None


def test_ui_intent_direct_and_fallback_shapes():
    direct = 0
    for item in TESTSET:
        if item["expect"].get("route") != "ui_intent":
            continue
        route = _route_for(item)
        assert route.kind == "ui_intent", item["text"]
        expected_kind = item["expect"].get("intent")
        if expected_kind:
            # a pinned intent must come from the fixed table (no model round, no toolbox)
            assert route.intent is not None and route.intent.kind == expected_kind, item["text"]
            assert route.tool_names is None, item["text"]
        else:
            # unpinned ui wording may also take the fallback shape: one app_call round, no
            # business tools (app-catalog's “帮我看看有哪些界面命令”, ui-25 style)
            assert (route.intent is not None and route.tool_names is None) or route.one_round, item["text"]
        direct += 1
    assert direct >= 20
    # Interface wording the fixed table misses: one app_call round, no business tools.
    route = classify_turn("把界面切到图表那一页", has_pending_card=False, ui_available=True, catalog=FAKE_CATALOG)
    assert route.kind == "ui_intent" and route.intent is None and route.tool_names == UI_FALLBACK_TOOLS
    assert route.one_round and route.temperature == 0.0


@pytest.mark.parametrize("text", ["开个语音通话，我想直接跟你说", "打个电话", "开语音通话吧", "帮我开个通话"])
def test_voice_call_words_start_the_orb_call_and_say_when_a_tap_is_needed(text):
    """AI 审计 2026-10-13：“开个语音通话”只把面板切到了对话。固定表直接开 AI 球的通话（disclosure
    voice-call），同一句话说清浏览器要求亲手点一下时该点哪里。"""
    route = classify_turn(text, has_pending_card=False, ui_available=True, catalog=FAKE_CATALOG)
    assert route.kind == "ui_intent" and route.intent is not None
    assert route.intent.changes == {"disclosure": {"name": "voice-call", "open": True}}
    assert "AI 球" in route.intent.say


def test_applied_ui_changes_are_said_with_what_and_where():
    from trader_runtime.ui_control import describe_ui_change
    assert describe_ui_change({"panel": "positions"}) == "打开持仓面板"
    assert describe_ui_change({"positionsTab": "fills"}) == "持仓栏切到「成交」页签"
    assert describe_ui_change({"reviewDay": "2026-10-12"}) == "复盘切到 2026-10-12 这一天"
    assert describe_ui_change({"settingsPage": "chart-advanced"}) == "设置切到「图表高级设置」页"
    assert "AI 球" in describe_ui_change({"disclosure": {"name": "voice-call", "open": True}})


def test_pending_card_turn_is_never_narrowed():
    # "搞一手螺纹多的" alone is a sure order (planned without a model); with a card waiting the next sentence answers it.
    assert classify_turn("搞一手螺纹多的", has_pending_card=False, ui_available=True,
                         catalog=FAKE_CATALOG).kind == "order_intent"
    for follow_up in ("确认", "改成三手", "算了不下了", "不对，是卖", "换成螺纹"):
        route = classify_turn(follow_up, has_pending_card=True, ui_available=True, catalog=FAKE_CATALOG)
        assert route.kind == "general" and route.tool_names is None, follow_up

    # Only the fixed table vouches for pure navigation: those keep the card (#523/#753).
    for navigation in ("把设置打开", "打开持仓清单", "看螺纹图", "切到螺纹的图", "切到日线"):
        route = classify_turn(navigation, has_pending_card=True, ui_available=True, catalog=FAKE_CATALOG)
        assert route.kind == "ui_intent" and route.intent is not None and route.tool_names is None, navigation
    for business in ("切到螺纹然后买一手", "螺纹现在多少钱", "止损改成20跳"):
        route = classify_turn(business, has_pending_card=True, ui_available=True, catalog=FAKE_CATALOG)
        assert route.kind == "general" and route.tool_names is None, business
    # #952 Codex: a question or a condition while a card waits is still no new card (the card tool stays withheld);
    # a correction of the card keeps the full toolbox.
    for question in ("螺纹买两手吗", "跌破3000再买两手", "跌破3000就买两手螺纹", "我刚买了两手螺纹"):
        route = classify_turn(question, has_pending_card=True, ui_available=True, catalog=FAKE_CATALOG)
        assert route.kind == "general" and route.no_card and route.tool_names is None, question
    conditional = classify_turn("跌破3000就买两手螺纹", has_pending_card=True, ui_available=True, catalog=FAKE_CATALOG)
    assert "价格提醒" in conditional.hint and conditional.mount == "price-alerts"
    for answer in ("改成三手", "不对，是卖", "算了"):
        assert not classify_turn(answer, has_pending_card=True, ui_available=True, catalog=FAKE_CATALOG).no_card, answer


def test_pending_card_heuristic_interface_sentences_keep_the_full_toolbox():
    """#756: answering the card plus an interface tail ("取消这张卡，切到日线") matched the keyword
    heuristic and became a UI-only round — the card could be neither confirmed, edited nor
    cancelled, and voice kept it alive. With a card waiting, the heuristic never narrows; the
    known cost is that heuristic-only navigation ("打开所有持仓清单") is a general turn too."""
    mixed = ("确认下单并切到五分钟", "确认并切到日线", "改成限价3500，切到五分钟", "取消这张卡，切到日线",
             "算了，切到五分钟", "确认，然后切到五分钟", "改成两手，切到日线", "止损改成3400并切到日线",
             "打开所有持仓清单", "帮我看看我的模拟盘持仓风险")
    for said in mixed:
        assert classify_turn(said, has_pending_card=False, ui_available=True, catalog=FAKE_CATALOG).intent is None, said
        route = classify_turn(said, has_pending_card=True, ui_available=True, catalog=FAKE_CATALOG)
        assert route == GENERAL_ROUTE, said


def test_risk_questions_never_take_a_fixed_interface_command():
    """#759: 「看看 + 持仓 + 风险」 is a question. The heuristic may still offer it a UI round (which
    escalates to a general turn when it attempts no write, see test_native_agent), never a fixed
    command — and never a UI-only round while a card waits."""
    for said in ("帮我看看我的模拟盘持仓风险", "看看我的持仓风险", "看看持仓风险", "帮我看看持仓风险大不大",
                 "看看持仓有没有风险", "我的持仓风险怎么样", "帮我检查一下持仓风险"):
        route = classify_turn(said, has_pending_card=False, ui_available=True, catalog=FAKE_CATALOG)
        assert route.intent is None and route.tool_names in {None, UI_FALLBACK_TOOLS}, said
        waiting = classify_turn(said, has_pending_card=True, ui_available=True, catalog=FAKE_CATALOG)
        assert waiting.kind == "general" and waiting.tool_names is None and waiting.intent is None, said


def test_position_list_words_open_the_positions_panel():
    """#759 P4: 「持仓清单」 is the positions panel in the fixed table, like 持仓列表; 清单 stays an
    interface noun for the close-all boundary."""
    from trader_runtime.order_intent_parse import book_words
    from trader_runtime.voice_intents import match_intent

    for said in ("打开持仓清单", "打开持仓列表", "看看持仓清单"):
        intent = match_intent(said)
        assert intent is not None and intent.kind == "panel" and intent.changes == {"panel": "positions"}, said
    closed = match_intent("关掉持仓清单")
    assert closed is not None and closed.changes == {"panel": None}
    for said in ("关掉所有持仓", "清掉所有持仓"):
        assert book_words(said) is not None and match_intent(said) is None, said
    assert book_words("关掉所有持仓清单") is None


def test_ui_request_without_a_window_stays_general():
    # The interface table hit is worthless with no window to command: the model answers instead.
    route = classify_turn("把设置打开", has_pending_card=False, ui_available=False, catalog=FAKE_CATALOG)
    assert route.kind == "general" and route.tool_names is None


def test_drawing_tool_words_open_the_popover_not_a_contract_switch():
    # 画线 (2026-10-02): "打开画线"是画线浮层的 disclosure；带着价位的"替我画"不是界面话，
    # 交给 chart-drawings 技能（general）。"打开合约信息"同理不落到合约名解析。
    # 画线第二版 (2026-10-09)：浮层换成画线工具栏（10-14 起平时隐藏），“打开画线”让它出来。
    route = classify_turn("打开画线", has_pending_card=False, ui_available=True, catalog=FAKE_CATALOG)
    assert route.kind == "ui_intent" and route.intent is not None
    assert route.intent.changes == {"disclosure": {"name": "chart-drawing-toolbar", "open": True}}
    assert classify_turn("帮我在3150画一条线", has_pending_card=False, ui_available=True,
                         catalog=FAKE_CATALOG).kind == "general"
    assert classify_turn("我画的线被突破了吗", has_pending_card=False, ui_available=True,
                         catalog=FAKE_CATALOG).kind == "general"


def test_drawing_fast_path_words_draw_clear_and_hide() -> None:
    """画线第二版 (2026-10-09)：点名“水平线”带价位直接画（小数点不丢）；“删掉所有画线”清空本合约；
    “隐藏/显示画线”是全部隐藏开关；“收起画线工具”收工具栏。泛泛的“画一条线”仍交给技能。"""
    from trader_runtime.voice_intents import match_intent

    for said, price in (("画一条水平线在3070", 3070.0), ("在 3070 画一条水平线", 3070.0), ("帮我画条水平线到826.5", 826.5)):
        intent = match_intent(said)
        assert intent is not None and intent.kind == "drawing", said
        assert intent.drawing == {"op": "create", "kind": "hline", "points": {"price": price}}, said
        assert intent.changes == {}
    for said in ("删掉所有画线", "清空画线", "把画线全部删掉", "删除全部画线"):
        intent = match_intent(said)
        assert intent is not None and intent.drawing == {"op": "clear"}, said
    for said, hidden in (("隐藏画线", True), ("画线都藏起来", True), ("显示画线", False), ("显示所有画线", False)):
        intent = match_intent(said)
        assert intent is not None and intent.changes == {"disclosure": {"name": "chart-drawings-hidden", "open": hidden}}, said
    assert match_intent("收起画线工具").changes == {"disclosure": {"name": "chart-drawing-toolbar", "open": False}}
    assert match_intent("画线工具收起来").changes == {"disclosure": {"name": "chart-drawing-toolbar", "open": False}}
    assert match_intent("帮我在3150画一条线") is None
    assert match_intent("删掉画的线").kind == "chartAnnotations"   # the AI's own annotations, as before


async def test_a_drawing_intent_runs_through_the_catalog_route_on_the_window_contract() -> None:
    from trader_runtime.turn_router import run_window_intent
    from trader_runtime.voice_intents import match_intent

    calls: list[tuple[str, Any]] = []

    async def call(operation: str, *, path=None, query=None, body=None) -> dict[str, Any]:
        calls.append((operation, query or body))
        if operation == "DELETE /api/market/drawings":
            return {"ok": True, "data": {"cleared": 3}}
        if body and body["points"]["price"] > 5000:
            return {"ok": False, "data": {"detail": "这个合约图上已有 200 条线，达到上限 200 条；先删掉一条再画"}}
        return {"ok": True, "data": {"drawing": {"id": "d1"}}}

    window = {"windowId": "w1", "revision": 4, "state": {"symbol": "SHFE.rb2701"}}
    drawn = await run_window_intent(match_intent("画一条水平线在3070"), window=window, call=call)
    assert drawn == {"ok": True, "changes": {}, "say": "画好了，3070 一条水平线"}
    assert calls[-1] == ("POST /api/market/drawings", {"symbol": "SHFE.rb2701", "kind": "hline", "points": {"price": 3070.0}})
    cleared = await run_window_intent(match_intent("删掉所有画线"), window=window, call=call)
    assert cleared["ok"] and cleared["say"] == "删掉了这个合约的 3 条画线"
    assert calls[-1] == ("DELETE /api/market/drawings", {"symbol": "SHFE.rb2701"})
    refused = await run_window_intent(match_intent("画一条水平线在9999"), window=window, call=call)
    assert refused["ok"] is False and "上限" in refused["error"]   # the agent takes over with the reason
    assert (await run_window_intent(match_intent("删掉所有画线"), window={**window, "state": {}}, call=call))["ok"] is False


def test_flash_words_open_the_news_panel_flash_tab_not_a_contract_switch():
    # 快讯页签 (2026-10-03)：“看快讯”“打开快讯”是新闻面板的快讯页；问快讯内容的话仍交给 agent。
    for said in ("看快讯", "打开快讯", "打开最新快讯", "帮我看一下快讯", "看新闻要点", "打开今日新闻要点"):
        route = classify_turn(said, has_pending_card=False, ui_available=True, catalog=FAKE_CATALOG)
        assert route.kind == "ui_intent" and route.intent is not None, said
        assert route.intent.changes == {"news": {"tab": "flash"}}, said
    assert classify_turn("最近有什么快讯", has_pending_card=False, ui_available=True,
                         catalog=FAKE_CATALOG).kind != "ui_intent"


@pytest.mark.parametrize("text,expected", [
    ("好的，已切到日线", "已切到日线"),
    ("好，挂单了", "挂单了"),
    ("收到。已完成", "已完成"),
    ("明白，这就去办", "这就去办"),
    ("嗯，我看看", "我看看"),
    ("好的！马上", "马上"),
    ("好的：如下", "如下"),
    ("好的 已切到日线", "已切到日线"),
    ("好的，好的，双份套话只剥一层", "好的，双份套话只剥一层"),
    ("先说结论", "先说结论"),
    ("好价格", "好价格"),            # 好 here is part of the word, no punctuation follows
    ("好的是很重要的", "好的是很重要的"),  # bare 好的 followed by a word: not a filler prefix
    ("", ""),
    (None, ""),
])
def test_strip_filler_prefix(text, expected):
    assert strip_filler_prefix(text) == expected


async def test_run_window_intent_font_stepping_and_receipts():
    bodies = []

    async def call(operation, *, path=None, query=None, body=None):
        bodies.append(body)
        return {"data": {"status": "applied", "observedRevision": body["expectedRevision"] + 1}}

    larger = Intent(kind="fontScale", changes={"fontScale": "larger"}, say="字放大了")
    outcome = await run_window_intent(larger, window={"windowId": "w", "revision": 4,
                                                      "state": {"fontScale": "standard"}}, call=call)
    assert outcome["ok"] and outcome["changes"] == {"fontScale": "large"} and outcome["say"] == "字放大了"
    assert bodies[0]["expectedRevision"] == 4 and bodies[0]["changes"] == {"fontScale": "large"}

    # already at the largest step: applied nothing, still ok — nothing left to do
    outcome = await run_window_intent(larger, window={"windowId": "w", "revision": 1,
                                                      "state": {"fontScale": "extra-large"}}, call=call)
    assert outcome["ok"] and outcome["changes"] == {} and outcome["say"] == "字已经最大了"

    # a window that refuses the command reports not ok, so the caller falls back to the model
    async def refused(operation, *, path=None, query=None, body=None):
        return {"data": {"status": "rejected"}}
    outcome = await run_window_intent(Intent(kind="settings", changes={"settingsOpen": True}, say="设置打开了"),
                                      window={"windowId": "w", "revision": 2, "state": {}}, call=refused)
    assert outcome["ok"] is False


async def test_run_window_intent_theme_toggle_follows_the_window_state():
    async def call(operation, *, path=None, query=None, body=None):
        return {"data": {"status": "applied", "observedRevision": body["expectedRevision"] + 1}}

    toggle = Intent(kind="theme", changes={"theme": "toggle"}, say="换色了")
    dark_window = {"windowId": "w", "revision": 0, "state": {"theme": "dark"}}
    outcome = await run_window_intent(toggle, window=dark_window, call=call)
    assert outcome["changes"] == {"theme": "light"} and "浅色" in outcome["say"]
    light_window = {"windowId": "w", "revision": 0, "state": {"theme": "light"}}
    outcome = await run_window_intent(toggle, window=light_window, call=call)
    assert outcome["changes"] == {"theme": "dark"} and "深色" in outcome["say"]


@pytest.mark.parametrize("text", ["平掉螺纹", "把沪金平了", "螺纹全平", "把沪金的多单平仓", "帮我把金子平掉", "平掉沪金吧"])
def test_closing_a_whole_product_without_a_size_is_the_batch_card(text):
    """“平掉螺纹 / 把沪金平了” (2026-10-04): no size = every position of that product — the planner's batch card for
    it (the product word stays a product: whichever months are held)."""
    route = classify_turn(text, has_pending_card=False, ui_available=True, catalog=FAKE_CATALOG)
    assert route.kind == "order_intent" and route.order.act == "close_all" and route.order.product, text


@pytest.mark.parametrize("text", ["我已经平了", "别把螺纹平了", "螺纹平了吗", "帮我把这个多单平了", "清掉所有标注",
                                  "螺纹平仓了没", "平仓"])
def test_questions_negations_and_pointing_words_never_close_a_product(text):
    route = classify_turn(text, has_pending_card=False, ui_available=True, catalog=FAKE_CATALOG)
    assert route.kind != "order_intent", text


def test_only_sure_orders_skip_the_model():
    kinds = {}
    for text in ("搞一手螺纹多的", "全平", "把螺纹调到三手多单", "现在螺纹多少钱", "螺纹怎么看", "我今天心情不好",
                 "买两手螺纹然后买一手沪金", "买两手螺纹然后再买一手"):
        route = classify_turn(text, has_pending_card=False, ui_available=True, catalog=FAKE_CATALOG)
        kinds.setdefault(route.kind, []).append(text)
    # 一句多单 split clause by clause (owner 2026-10-16) is a sure order; one whose second clause names no contract is not.
    assert kinds == {"order_intent": ["搞一手螺纹多的", "全平", "把螺纹调到三手多单", "买两手螺纹然后买一手沪金"],
                     "market_query": ["现在螺纹多少钱"], "opinion": ["螺纹怎么看"], "general": ["我今天心情不好"],
                     "order": ["买两手螺纹然后再买一手"]}


@pytest.mark.parametrize("text", ["螺纹怎么看", "沪金能不能做", "铁矿看多还是看空", "你觉得能不能买", "沪金后市如何"])
def test_an_opinion_on_one_product_takes_the_judgment_card_route(text):
    route = classify_turn(text, has_pending_card=False, ui_available=True, catalog=FAKE_CATALOG)
    assert route.kind == "opinion" and route.tool_names is None


@pytest.mark.parametrize("text", ["螺纹和沪金怎么看", "螺纹为什么跌了", "我的螺纹持仓怎么看", "这波是不是要跌", "螺纹最近有什么消息"])
def test_two_products_reasons_positions_and_chart_reading_stay_general(text):
    assert classify_turn(text, has_pending_card=False, ui_available=True, catalog=FAKE_CATALOG).kind == "general"


@pytest.mark.parametrize("text", ["来一手螺纹", "按现价来一手", "开一手螺纹"])
def test_an_opening_order_without_a_direction_is_never_guessed(text):
    """Under a forced tool choice the model guessed 买开 for “来一手螺纹” on every bench run (10-04): the direction is
    the user's. The parser leaves it empty and the planner asks (a question card whose quick answers are whole
    orders) — no model round guesses it."""
    route = classify_turn(text, has_pending_card=False, ui_available=True, catalog=FAKE_CATALOG)
    assert route.kind == "order_intent" and route.order.dir is None, text


@pytest.mark.parametrize("text,direction", [("820挂一手多", "long"), ("螺纹上买两手", "long"), ("帮我搞两手沪金空的", "short"),
                                            ("平掉沪金两手", None)])
def test_an_order_with_its_words_goes_straight_to_the_planner(text, direction):
    route = classify_turn(text, has_pending_card=False, ui_available=True, catalog=FAKE_CATALOG)
    assert route.kind == "order_intent" and route.order.dir == direction, text


def test_direct_fills_and_auto_strategy_starts_are_judged_by_the_call_not_the_words():
    """Owner 2026-10-16: the user's own turn never fills an order directly, whatever the words — the call decides:
    execute_simulation_trade or confirming a card for the user (directly or through app_call) is a direct fill; a
    strategy start that may order by itself (signalAction auto, or left out) is named for the user-request check."""
    from trader_runtime.turn_router import auto_strategy_start, fills_directly

    def app(operation, body=None):
        return {"requestJson": json.dumps({"operation": operation, **({"body": body} if body else {})})}

    assert fills_directly("execute_simulation_trade") and fills_directly("app_call", app("tool:execute_simulation_trade"))
    assert fills_directly("app_call", app("POST /api/proposals/{proposal_id}/confirm"))
    assert not fills_directly("propose_order") and not fills_directly("cancel_simulation_order")
    assert auto_strategy_start("start_strategy_monitoring", {"strategyId": "s", "signalAction": "auto"}) == "s"
    assert auto_strategy_start("start_strategy_monitoring", {"strategyId": "s"}) == "s"
    assert auto_strategy_start("start_strategy_monitoring", {"strategyId": "s", "signalAction": "confirm"}) is None
    route = "POST /api/strategies/{strategy_id}/monitor/start"
    started = {"requestJson": json.dumps({"operation": route, "path": {"strategy_id": "s"}, "body": {"signalAction": "auto"}})}
    assert auto_strategy_start("app_call", started) == "s"
    assert auto_strategy_start("app_call", app(route, {"signalAction": "alert"})) is None
    assert auto_strategy_start("app_call", app("GET /api/strategies")) is None
    # #932 复核: the catalog's tool alias is the same start, read from its body like the tool's own arguments.
    assert auto_strategy_start("app_call", app("tool:start_strategy_monitoring", {"strategyId": "s"})) == "s"
    assert auto_strategy_start("app_call", app("tool:start_strategy_monitoring",
                                               {"strategyId": "s", "signalAction": "auto"})) == "s"
    assert auto_strategy_start("app_call", app("tool:start_strategy_monitoring",
                                               {"strategyId": "s", "signalAction": "confirm"})) is None
    assert auto_strategy_start("app_call", app("tool:pause_strategy_monitoring", {"strategyId": "s"})) is None
    assert auto_strategy_start("app_call", {"requestJson": "{not json"}) == ""  # unreadable: fail closed


@pytest.mark.parametrize("text", ["开多一手螺纹，止损 818 止盈 830", "螺纹开多一手止损3100", "买螺纹，止盈3200"])
def test_an_order_with_its_stop_riding_along_is_the_trading_ais_order_even_with_the_editor_open(text):
    """#952 Codex: 下单主体 + 随单止损止盈从句 is an order (gap protection): the trading AI's order round carries the
    protection on its card — with the 策略编辑卡 open it is not the card's 补一句 (止损 is a field word there)."""
    from trader_runtime.strategy_editor import editor_utterance
    assert states_order(text, FAKE_CATALOG), text
    assert not editor_utterance(text, FAKE_CATALOG), text
    for editor_open in (False, True):
        route = classify_turn(text, has_pending_card=False, ui_available=True, catalog=FAKE_CATALOG,
                              editor_open=editor_open)
        assert route.kind == "order" and "propose_order" in (route.tool_names or ()), (text, editor_open)
    assert editor_utterance("止损改成 3100", FAKE_CATALOG)  # the field itself stays the card's


def test_a_conditional_order_with_its_stop_is_still_a_conditional_and_no_order_write_runs_on_it():
    """#952 Codex: “跌到3000就开多一手螺纹，止损2900” is a conditional order (no card now, a price alert offered), and
    the no-card turn withholds every order write — the card tool, execution, protections — through app_call too."""
    from trader_runtime.turn_router import ORDER_WRITE_TOOLS, order_write
    route = classify_turn("跌到3000就开多一手螺纹，止损2900", has_pending_card=False, ui_available=True,
                          catalog=FAKE_CATALOG)
    assert route.kind == "general" and route.no_card and route.mount == "price-alerts"
    assert {"propose_order", "execute_simulation_trade", "set_simulation_stop_loss"} <= ORDER_WRITE_TOOLS
    assert order_write("app_call", {"requestJson": json.dumps({"operation": "tool:execute_simulation_trade"})})
    assert order_write("app_call", {"requestJson": json.dumps({"operation": "POST /api/simulation/proposals"})})
    assert not order_write("app_call", {"requestJson": json.dumps({"operation": "POST /api/price-alerts"})})
    assert order_write("app_call", {"requestJson": json.dumps({"operation": "POST /api/routines/{routine_id}/run"})})
    assert not order_write("stop_simulation_execution")  # “别再下单了” asks for exactly this
    assert classify_turn("开多一手螺纹，止损3100", has_pending_card=False, ui_available=True,
                         catalog=FAKE_CATALOG).card_required


@pytest.mark.parametrize("text,tab", [("打开调研", "research"), ("看盘", "market"), ("回到看盘", "market"),
                                      ("打开我的", "me"), ("打开AI动态", "ai"), ("回到AI首页", "ai")])
def test_phone_tab_words_are_interface_intents(text, tab):
    route = classify_turn(text, has_pending_card=False, ui_available=True, catalog=FAKE_CATALOG)
    assert route.kind == "ui_intent" and route.intent.changes == {"mobileTab": tab}
    # 2026-10-15 (统一提醒 #932): 通知 / 提醒 open the 提醒中心 (either layout); 时间线 is AI 首页's timeline.
    for words in ("看看通知", "打开通知", "通知列表", "打开提醒", "提醒中心"):
        assert classify_turn(words, has_pending_card=False, ui_available=True, catalog=FAKE_CATALOG).intent.changes == {"reminderCenter": {"open": True}}, words
    assert classify_turn("看未读提醒", has_pending_card=False, ui_available=True, catalog=FAKE_CATALOG).intent.changes == {
        "reminderCenter": {"open": True, "filter": "unread"}}
    assert classify_turn("打开时间线", has_pending_card=False, ui_available=True, catalog=FAKE_CATALOG).intent.changes == {"mobileNotifyFilter": "all"}
    # A reminder to create is never the center; turning reminders off is the agent's (a preference), not a close.
    for sentence in ("螺纹到3500提醒我", "提醒我收盘前平仓", "关掉提醒", "关掉通知"):
        route = classify_turn(sentence, has_pending_card=False, ui_available=True, catalog=FAKE_CATALOG)
        assert route.intent is None or "reminderCenter" not in route.intent.changes, sentence
    # The settings page keeps its own words; a bare "我的" is not a command.
    for words in ("打开通知设置", "提醒设置"):
        assert classify_turn(words, has_pending_card=False, ui_available=True, catalog=FAKE_CATALOG).intent.changes == {"settingsPage": "notify"}
    assert classify_turn("我的", has_pending_card=False, ui_available=True, catalog=FAKE_CATALOG).kind != "ui_intent"
    # A sentence that only starts with a tab word is not the bare tab (10-05: "通知里只看螺纹的简报"
    # opened 通知 without the product filter; "调研一下…" would have opened 调研).
    for sentence in ("通知里只看螺纹的简报", "调研一下螺纹今天怎么看", "动态里有什么新消息"):
        route = classify_turn(sentence, has_pending_card=False, ui_available=True, catalog=FAKE_CATALOG)
        assert route.intent is None or "mobileTab" not in route.intent.changes


async def test_run_window_intent_maps_tabs_and_panels_onto_the_window_layout():
    bodies = []

    async def call(operation, *, path=None, query=None, body=None):
        bodies.append(body["changes"])
        return {"data": {"status": "applied", "observedRevision": body["expectedRevision"] + 1}}

    phone = {"windowId": "w", "revision": 0, "state": {"mobileLayout": True, "mobileTab": "ai"}}
    desktop = {"windowId": "w", "revision": 0, "state": {"view": "trade"}}
    chat = Intent(kind="panel", changes={"panel": "ai"}, say="对话打开了")
    chart = Intent(kind="view", changes={"view": "board"}, say="到报价总表了", follow=[{"boardExchange": "watchlist"}])
    research = Intent(kind="mobileTab", changes={"mobileTab": "research"}, say="调研打开了")
    notify = Intent(kind="mobileTab", changes={"mobileNotifyFilter": "all"}, say="到 AI 首页的时间线了")
    # A phone window has no side panels or views: the desktop words land on its tabs (follow-ups dropped).
    assert (await run_window_intent(chat, window=phone, call=call))["changes"] == {"mobileTab": "chat"}
    assert (await run_window_intent(chart, window=phone, call=call))["say"] == "到自选报价了"
    assert bodies == [{"mobileTab": "chat"}, {"mobileMarket": {"board": "watchlist"}}]
    # A desktop window opens the surface that shows the same thing, or refuses without sending anything.
    assert (await run_window_intent(research, window=desktop, call=call))["changes"] == {"news": {"tab": "sheet"}}
    outcome = await run_window_intent(notify, window=desktop, call=call)
    assert outcome == {"ok": False, "error": "mobile_only", "uiCommandAttempted": False} and len(bodies) == 3
    assert (await run_window_intent(chat, window=desktop, call=call))["changes"] == {"panel": "ai"}
    # "打开时间线" on a phone: AI 首页's timeline (the filter command also selects the AI tab).
    assert (await run_window_intent(notify, window=phone, call=call))["changes"] == {"mobileNotifyFilter": "all"}
    # 提醒中心 (2026-10-15): the same command on either layout (the bell's popover / the 提醒 page).
    reminders = Intent(kind="reminderCenter", changes={"reminderCenter": {"open": True}}, say="提醒中心打开了")
    assert (await run_window_intent(reminders, window=desktop, call=call))["changes"] == {"reminderCenter": {"open": True}}
    assert (await run_window_intent(reminders, window=phone, call=call))["changes"] == {"reminderCenter": {"open": True}}


async def test_phone_market_words_take_the_phone_and_leave_the_computer_to_the_board_or_the_agent():
    """手机看盘 (2026-10-09): “打开报价列表 / 看多图 / 多周期同列 / 单图 / 副图换成 MACD / 打开副图设置” are one
    `mobileMarket` command on a phone window; a computer window opens the 报价总表 for 报价列表 and hands the rest
    to the agent without sending anything."""
    bodies = []

    async def call(operation, *, path=None, query=None, body=None):
        bodies.append(body["changes"])
        return {"data": {"status": "applied", "observedRevision": body["expectedRevision"] + 1}}

    phone = {"windowId": "w", "revision": 0, "state": {"mobileLayout": True, "mobileTab": "ai", "mobilePage": None}}
    desktop = {"windowId": "w", "revision": 0, "state": {"view": "trade"}}
    expected = {"打开报价列表": {"view": "quotes"}, "看多图": {"view": "multi"}, "多周期同列": {"layout": "periods"},
                "看单图": {"view": "single"}, "副图换成 MACD": {"subPane": "macd"}, "副图看成交量": {"subPane": "volume"},
                "打开副图设置": {"subPanes": True}}
    for text, change in expected.items():
        intent = classify_turn(text, has_pending_card=False, ui_available=True, catalog=FAKE_CATALOG).intent
        assert intent is not None and intent.changes == {"mobileMarket": change}, text
        assert (await run_window_intent(intent, window=phone, call=call))["ok"], text
    assert bodies == [{"mobileMarket": change} for change in expected.values()]
    board = classify_turn("打开报价列表", has_pending_card=False, ui_available=True, catalog=FAKE_CATALOG).intent
    assert (await run_window_intent(board, window=desktop, call=call))["changes"] == {"view": "board"}
    macd = classify_turn("副图换成 MACD", has_pending_card=False, ui_available=True, catalog=FAKE_CATALOG).intent
    assert await run_window_intent(macd, window=desktop, call=call) == {"ok": False, "error": "mobile_only", "uiCommandAttempted": False}
    assert len(bodies) == len(expected) + 1


async def test_strategy_words_open_the_phone_strategy_tab_or_the_strategy_panel():
    """手机版新结构 (2026-10-06): "打开策略" / "看看我的策略" / "看回测" land on the phone's 策略 tab
    (the desktop follow-up dropped); a desktop window opens the 策略面板 at 我的策略 (第八批, 2026-10-08: the
    实验室 tab is gone — every card shows its latest backtest, so "看回测" / "打开实验室" land there too)."""
    bodies = []

    async def call(operation, *, path=None, query=None, body=None):
        bodies.append(body["changes"])
        return {"data": {"status": "applied", "observedRevision": body["expectedRevision"] + 1}}

    phone = {"windowId": "w", "revision": 0, "state": {"mobileLayout": True, "mobileTab": "ai", "mobilePage": None}}
    desktop = {"windowId": "w", "revision": 0, "state": {"view": "trade"}}
    for text in ("打开策略", "看看我的策略", "看回测"):
        intent = classify_turn(text, has_pending_card=False, ui_available=True, catalog=FAKE_CATALOG).intent
        assert intent is not None and intent.changes == {"panel": "strategy"}, text
        outcome = await run_window_intent(intent, window=phone, call=call)
        assert outcome["changes"] == {"mobileTab": "strategy"} and outcome["say"] == "策略打开了"
    assert bodies == [{"mobileTab": "strategy"}] * 3
    for text in ("看回测", "打开实验室", "打开策略库"):
        intent = classify_turn(text, has_pending_card=False, ui_available=True, catalog=FAKE_CATALOG).intent
        assert intent is not None, text
        await run_window_intent(intent, window=desktop, call=call)
        assert bodies[-2:] == [{"panel": "strategy"}, {"strategyTab": "library"}], text


async def test_run_window_intent_opens_the_phone_pages_for_panel_words():
    bodies = []

    async def call(operation, *, path=None, query=None, body=None):
        bodies.append(body["changes"])
        return {"data": {"status": "applied", "observedRevision": body["expectedRevision"] + 1}}

    phone = {"windowId": "w", "revision": 0, "state": {"mobileLayout": True, "mobileTab": "market", "mobilePage": None}}
    # 手机版第二期: 持仓 / 下单 / 复盘 open their full-screen pages; a 持仓分页 keeps its tab as a follow-up.
    for text, page in (("打开持仓", "positions"), ("去下单", "order"), ("打开复盘", "review")):
        intent = classify_turn(text, has_pending_card=False, ui_available=True, catalog=FAKE_CATALOG).intent
        assert (await run_window_intent(intent, window=phone, call=call))["changes"] == {"mobilePage": page}
    orders = classify_turn("看委托列表", has_pending_card=False, ui_available=True, catalog=FAKE_CATALOG).intent
    assert (await run_window_intent(orders, window=phone, call=call))["ok"]
    assert bodies[-2:] == [{"mobilePage": "positions"}, {"positionsTab": "orders"}]
    # A period lands on the 看盘 tab's single chart; "收起持仓" closes an open page.
    daily = classify_turn("切到日线", has_pending_card=False, ui_available=True, catalog=FAKE_CATALOG).intent
    assert (await run_window_intent(daily, window=phone, call=call))["ok"]
    assert bodies[-2:] == [{"duration": 86400}, {"mobileMarket": {"view": "single"}}]
    close = classify_turn("收起持仓", has_pending_card=False, ui_available=True, catalog=FAKE_CATALOG).intent
    open_page = {**phone, "state": {**phone["state"], "mobilePage": "positions"}}
    assert (await run_window_intent(close, window=open_page, call=call))["changes"] == {"mobilePage": None}


async def test_install_words_open_the_phone_guide_and_go_to_the_agent_on_a_desktop():
    bodies = []

    async def call(operation, *, path=None, query=None, body=None):
        bodies.append(body["changes"])
        return {"data": {"status": "applied", "observedRevision": body["expectedRevision"] + 1}}

    # 安装到桌面 (2026-10-06): the phone web page's guide; a desktop window has none.
    phone = {"windowId": "w", "revision": 0, "state": {"mobileLayout": True, "mobileTab": "ai", "mobilePage": None}}
    for text in ("教我装到桌面", "添加到桌面", "把它放到主屏幕", "怎么添加到手机桌面"):
        intent = classify_turn(text, has_pending_card=False, ui_available=True, catalog=FAKE_CATALOG).intent
        assert intent is not None and intent.kind == "install", text
        assert (await run_window_intent(intent, window=phone, call=call))["changes"] == {"mobilePage": "install"}
    assert intent_for_window(intent, {"view": "trade"}) is None


async def test_push_words_open_the_phone_page_or_the_notify_settings_on_a_desktop():
    bodies = []

    async def call(operation, *, path=None, query=None, body=None):
        bodies.append(body["changes"])
        return {"data": {"status": "applied", "observedRevision": body["expectedRevision"] + 1}}

    # 锁屏提醒 (2026-10-06): the phone's push switch and devices are its 锁屏提醒 page; 设置 → 提醒 (2026-10-15,
    # every kind's switch) opens as itself on a phone too.
    phone = {"windowId": "w", "revision": 0, "state": {"mobileLayout": True, "mobileTab": "me", "mobilePage": None}}
    for text in ("打开锁屏提醒", "锁屏推送设置"):
        intent = classify_turn(text, has_pending_card=False, ui_available=True, catalog=FAKE_CATALOG).intent
        assert intent is not None, text
        assert (await run_window_intent(intent, window=phone, call=call))["changes"] == {"mobilePage": "push"}, text
    intent = classify_turn("通知设置", has_pending_card=False, ui_available=True, catalog=FAKE_CATALOG).intent
    assert (await run_window_intent(intent, window=phone, call=call))["changes"] == {"settingsPage": "notify"}
    desktop = classify_turn("打开锁屏提醒", has_pending_card=False, ui_available=True, catalog=FAKE_CATALOG).intent
    assert intent_for_window(desktop, {"view": "trade"}).changes == {"settingsPage": "notify"}


async def test_run_window_intent_opens_the_brief_subscriptions_on_either_layout():
    bodies = []

    async def call(operation, *, path=None, query=None, body=None):
        bodies.append(body["changes"])
        return {"data": {"status": "applied", "observedRevision": body["expectedRevision"] + 1}}

    # AI 简报订阅 (2026-10-05): the phone's 我的 → AI 简报推送 page, the desktop's 设置 → 通知 → 订阅品种 block.
    phone = {"windowId": "w", "revision": 0, "state": {"mobileLayout": True, "mobileTab": "me", "mobilePage": None}}
    desktop = {"windowId": "d", "revision": 0, "state": {"view": "trade"}}
    for text in ("订阅简报", "打开简报品种", "AI简报推送"):
        intent = classify_turn(text, has_pending_card=False, ui_available=True, catalog=FAKE_CATALOG).intent
        assert (await run_window_intent(intent, window=phone, call=call))["changes"] == {"mobilePage": "briefs"}
    intent = classify_turn("简报订阅", has_pending_card=False, ui_available=True, catalog=FAKE_CATALOG).intent
    assert (await run_window_intent(intent, window=desktop, call=call))["ok"]
    assert bodies[-2:] == [{"settingsPage": "notify"}, {"disclosure": {"name": "brief-subscriptions", "open": True}}]


async def test_strategy_editor_opens_by_words_and_takes_field_sentences_only_while_open():
    """策略编辑卡 (2026-10-08): "帮我设计一个策略" opens it (one command, no model turn) and the line says what
    the habits prefill filled — or, from "根据我的交易生成策略" with too few trades, why it could not and
    that the entry conditions are the user's this phase. While it is open a field sentence is its 补一句;
    an order naming its contract stays an order; a phone window (2026-10-08) opens its full-screen page."""
    receipts = []
    card: dict = {}

    async def call(operation, *, path=None, query=None, body=None):
        receipts.append(body["changes"])
        return {"data": {"status": "applied", "observedRevision": body["expectedRevision"] + 1, "state": card}}

    desk = {"windowId": "w", "revision": 0, "state": {"view": "trade"}}
    new = classify_turn("帮我设计一个策略", has_pending_card=False, ui_available=True, catalog=FAKE_CATALOG)
    assert new.kind == "ui_intent" and new.intent.changes == {"strategyEditor": {"new": True}}
    card = {"strategyEditor": {"open": True, "start": "habits", "aiFields": ["symbol", "direction", "lots"],
                               "prefill": {"eligible": True}}}
    said = (await run_window_intent(new.intent, window=desk, call=call))["say"]
    assert said.startswith("我在右边打开了策略编辑卡") and "预填了 3 项" in said and "开仓条件" not in said
    habits = classify_turn("根据我的交易生成策略", has_pending_card=False, ui_available=True, catalog=FAKE_CATALOG)
    assert habits.intent.changes == {"strategyEditor": {"new": True, "start": "habits"}}
    card = {"strategyEditor": {"open": True, "start": "blank",
                               "prefill": {"eligible": False, "reason": "近 20 个交易日已平仓成交 2 笔，不到 5 笔，不按习惯预填。"}}}
    said = (await run_window_intent(habits.intent, window=desk, call=call))["say"]
    assert "2 笔" in said and "开仓条件要你自己选" in said and "下一期" in said
    phone = {"windowId": "w", "revision": 0, "state": {"mobileLayout": True, "mobileTab": "ai"}}
    card = {"mobileLayout": True, "mobileTab": "strategy", "strategyEditor": {"open": True, "start": "blank", "prefill": {"eligible": False}}}
    outcome = await run_window_intent(new.intent, window=phone, call=call)
    assert outcome["changes"] == {"strategyEditor": {"new": True}} and outcome["say"].startswith("我打开了策略编辑卡")

    for text in ("止损改成20跳", "每次开两手", "仓位改成两手", "开仓时段改成只做上午", "做空条件加一个MACD死叉",
                 # #738 审查: 形状之外还有字段/条件语义的清仓形是策略规则的补一句（原 main 即 editor_ask）。
                 "收盘前所有仓位都平掉", "每次触发止损把仓位都关掉",
                 "最长持仓20根后把所有仓位都关掉", "持仓满20根后把所有仓位都关掉"):
        assert classify_turn(text, has_pending_card=False, ui_available=True, catalog=FAKE_CATALOG,
                             editor_open=True).kind == "editor_ask", text
        assert classify_turn(text, has_pending_card=False, ui_available=True, catalog=FAKE_CATALOG).kind != "editor_ask"
    # #738 审查: 「把仓位都关掉啦」是本轮清仓口语，`仓位` 是编辑卡字段也不能把它当补一句——仍走
    # general（#730；原 main 被编辑入口截走，本批把承诺修通），出卡与否由交易回合决定。
    for text in ("把仓位都关掉啦", "全部仓位都关掉呗"):
        assert classify_turn(text, has_pending_card=False, ui_available=True, catalog=FAKE_CATALOG,
                             editor_open=True).kind in {"general", "order_intent"}, text
    for text in ("在仓位面板里把所有持仓都关掉", "在仓位的面板里把所有持仓都关掉"):
        assert classify_turn(text, has_pending_card=False, ui_available=True, catalog=FAKE_CATALOG,
                             editor_open=True).kind == "general", text  # the trading AI reads it, never the card
    # 清仓+界面的混合句（关掉所有持仓然后打开面板）也不是补一句，走 general。
    assert classify_turn("关掉所有持仓然后打开面板", has_pending_card=False, ui_available=True,
                         catalog=FAKE_CATALOG, editor_open=True).kind == "general"
    assert classify_turn("螺纹买两手", has_pending_card=False, ui_available=True, catalog=FAKE_CATALOG,
                         editor_open=True).kind == "order_intent"
    assert classify_turn("打开设置", has_pending_card=False, ui_available=True, catalog=FAKE_CATALOG,
                         editor_open=True).kind == "ui_intent"


def test_a_command_beside_a_question_and_a_bare_price_reach_the_tools_that_do_them():
    """#932: “撤掉螺纹的止盈，现在持仓还剩几手？” is not a question as a whole — the full round (write tools offered,
    orders still carded) does both; a question with only a hypothetical order stays a no-card answer. “做到多 2 手
    3066” (a price said bare) is the order round's, not words the router drops. “直接启动，不用问我”: the 不用 is about
    asking — the start stands; “别启动，不用问我” still negates it."""
    from trader_runtime.order_intent_parse import commands_start

    def route(text):
        return classify_turn(text, has_pending_card=False, ui_available=True, catalog=FAKE_CATALOG)

    mixed = route("撤掉螺纹钢的止盈，现在持仓还剩几手？")
    assert (mixed.kind, mixed.no_card, mixed.tool_names) == ("general", False, None)
    assert route("如果开一手螺纹钢，保证金要多少？").no_card
    assert route("螺纹钢做到多 2 手 3066").tool_names == ORDER_TOOLS
    assert commands_start("直接启动，不用问我") and commands_start("启动螺纹策略，不用再确认了")
    assert not commands_start("别启动，不用问我") and not commands_start("先别上线")
