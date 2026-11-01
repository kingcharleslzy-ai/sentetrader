"""Where one finished sentence of a call goes (stage 2 of the voice chain, split from voice_realtime 2026-11-01).

`route_utterance` reads the words alone (hang-up, stop, confirm / cancel a waiting card, tour, 策略编辑卡, replay,
quick lookup, interface words, small talk, else the Runtime); `classify_final` adds what the call knows — a card
waiting, the watched order, the 保本提醒 just read, the review, a retraction, the user's routine, the front desk's
order card and query — and is the one place a final's route is decided (early, stall and Doubao's own final all
ask it). The meaning of the words themselves stays with the shared readers: voice_intents (interface table,
quick queries), order_intent_parse (order words) and voice_front (front-desk query / card picks).
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from .order_intent import OrderIntent
from .order_intent_parse import ParseContext, Parsed
from .order_intent_parse import parse as parse_intent
from .voice_front import classify_front, front_order
from .voice_intents import (
    looks_like_replay_control,
    looks_like_ui,
    match_intent,
    match_quick_query,
    match_replay_control,
)

if TYPE_CHECKING:
    from .voice_realtime import CallState, VoiceRealtimeService

_PUNCTUATION = re.compile(r"[\s，。,.!！?？、~～…·]")


def strip_punctuation(text: str) -> str:
    """The words of a final as the routers compare them: spaces and punctuation dropped."""
    return _PUNCTUATION.sub("", text)


# Routes the Runtime answers itself (Doubao's own reply is muted anyway): only these may be routed from
# a partial. Small talk, quick queries and interface requests wait for its final.
EARLY_ROUTES = frozenset({"delegate", "confirm", "cancel", "stop", "hangup", "chase", "watch_cancel",
                          "breakeven_move", "breakeven_decline"})
MERGEABLE_ROUTES = frozenset({"delegate", "quick", "front", "action", "replay", "editor_ask", "review_answer"})
# 引导复盘 (2026-09-29): the routes the Runtime answers itself while it walks the user through the day's
# notable trades (review_picks); Doubao's own reply to them is muted like any Runtime-spoken turn.
REVIEW_ROUTES = frozenset({"review_start", "review_answer", "review_skip", "review_stop", "review_decline"})
# With a card waiting, these drop it (the call with AI_TRADER_VOICE_FRONT_ACTION on): "不做了" / "先不买了" read as
# 算了 there, measured on the 10-13 regression where they went to the trading AI and took 4 s.
ACTION_CANCEL_WORDS = re.compile(r"^(?:先?(?:不做|不买|不卖|不平|不下|不要|不弄|算了|取消|撤了|撤掉)了?(?:吧|啊|呀)?)+$")

# -- Runtime-side routing of each utterance (2026-09-18) ------------------------------------------
# Leaving "say a filler, then call delegate" to Doubao is unreliable: on the user's 1.6.0 call three
# business requests ("买多一手我现在看的这个", "为什么持仓还是只有一个") got a filler and no tool call.
# The Runtime now decides from the ASR final text: confirm / cancel words act on the waiting card,
# small talk is left to Doubao, everything else is delegated by the Runtime itself. Doubao's own
# tool call for the same turn, if it comes, is served the same result instead of running twice.
CONFIRM_WORDS = re.compile(r"^(确认|确定|是的|是|对|好|好的|好吧|可以|行|下吧|下单|发吧|发|就这样|没问题|OK|ok)+[吧呀啊的了呢]?$")
CANCEL_WORDS = re.compile(r"^(取消|算了|不要|不用|不了|不下了|放弃|别下|别|撤了|不对)+[吧呀啊的了呢]?$")
SMALL_TALK = re.compile(r"^(你好|您好|喂|哈喽|hello|hi|在吗|在不在|能听到吗|听得到吗|谢谢|多谢|辛苦了|再见|拜拜|好的|嗯|哦|行|收到|知道了|明白|没事|没有了|不用了)+$")
# "关闭语音 / 结束通话 / 挂断 / 不说了 / 退出语音": the Runtime hangs up itself. "停 / 等一下 / 别说了": only
# the read-out stops. Both are decided here, never left to the model (a 1.6.5 call ended with "好了，关闭语音"
# delegated to the agent).
HANGUP_WORDS = re.compile(r"^(好了|好的|好|行|嗯|那|就这样|先|先这样|谢谢|ok|OK)*(关闭语音|关掉语音|结束通话|结束语音|退出语音|挂断|挂了|挂掉|挂断吧|不说了|不聊了|先不说了|再见|拜拜|byebye|bye)+(吧|了|啊|呀|吧啊)*$")
# Two-character requests that the "≤2 chars = small talk" rule would otherwise hand to Doubao.
ACTION_WORDS = re.compile(r"^(全平|清仓|撤单|平仓|平掉|买入|卖出|做多|做空|加仓|减仓|止损|止盈|查仓|看仓|持仓|行情|新闻)+[吧呀啊的了呢]?$")
STOP_WORDS = re.compile(r"^(停|停一下|停下|停停|等一下|等等|等一等|先等等|别说了|不用说了|别念了|不用念了|闭嘴|安静|先别说|够了|知道了知道了)+(吧|了|啊|呀)*$")
# 09-21: a waiting card aside, these words cancel the round that is still running (the query the user
# interrupted), not just its read-out.
CANCEL_ALL_WORDS = re.compile(r"(?:都|全部|全都|全)(?:不要|取消|算了|不下|不做|作废)了?(?:吧|啊|呀)?")
CANCEL_RUN_WORDS = re.compile(r"^(算了|不要了|不用了|别查了|不查了|不用查了|取消|取消掉|别弄了|不弄了)+(吧|呀|啊|的了|了呢|了吧)*$")
# 09-22: the unfilled-order watchdog's two spoken commands accept natural short sentences, not just
# fixed spellings: 撤 = a stripped sentence ≤16 chars built around 撤/取消, 追 = the same around 追
# or the 对手价 reprice. Both need a watched order and exclude negation and any other trading
# action — those sentences stay with the agent.
WATCH_COMMAND_MAX_CHARS = 16
WATCH_CANCEL_TRIGGER = re.compile(r"撤|取消")
WATCH_CHASE_TRIGGER = re.compile(r"追|改对手价|改成对手价|用对手价")
WATCH_CANCEL_BLOCKERS = re.compile(r"别|不要|不用|买|卖|开|平|加|减|改价|追|再|然后|顺便")
WATCH_CHASE_BLOCKERS = re.compile(r"别|不要|不用|买|卖|开|平|加|减|改价|撤|取消|再|然后|顺便")


# 保本提醒 (2026-10-06): right after this call read a 保本提醒 out, and only inside the voice confirm window
# (preferences.voice.confirmWindowSeconds, counted from the end of that read-out), an explicit 移 / 移到成本 /
# 保本 / 移吧 moves the stop and 不用 / 不移 declines. Nothing else — "好" included — touches the stop: it
# keeps its normal route (the agent). A proposal card waiting for 确认 always comes first.
BREAKEVEN_MOVE_WORDS = re.compile(r"^(移|移吧|移到成本|移到成本吧|保本|移一下)$")
BREAKEVEN_DECLINE_WORDS = re.compile(r"^(不用|不用了|不移|不移了)$")


def breakeven_route(stripped: str, *, pending: bool, heard: bool) -> str | None:
    """"breakeven_move" / "breakeven_decline" for a short answer to the 保本提醒 this call just read out
    (`heard`: still inside its confirm window); None otherwise. A waiting proposal card wins (`pending`):
    its 确认 / 取消 keep their meaning and a 移 then goes to the agent like any other sentence."""
    if pending or not heard:
        return None
    if BREAKEVEN_MOVE_WORDS.match(stripped):
        return "breakeven_move"
    if BREAKEVEN_DECLINE_WORDS.match(stripped):
        return "breakeven_decline"
    return None


def is_watch_cancel_command(stripped: str) -> bool:
    return (len(stripped) <= WATCH_COMMAND_MAX_CHARS
            and WATCH_CANCEL_TRIGGER.search(stripped) is not None
            and WATCH_CANCEL_BLOCKERS.search(stripped) is None)


def is_watch_chase_command(stripped: str) -> bool:
    return (len(stripped) <= WATCH_COMMAND_MAX_CHARS
            and WATCH_CHASE_TRIGGER.search(stripped) is not None
            and WATCH_CHASE_BLOCKERS.search(stripped) is None)


# The registry skills behind the Runtime-routed actions (2026-09-24): a routed
# utterance whose skill is unavailable gets the unified refusal line before any
# tool runs. editor_ask (2026-10-08) is the 策略编辑卡's 补一句.
ROUTED_ACTION_SKILLS = {
    "editor_ask": "strategy-management",
    "replay": "replay-control",
}


# A guided tour is running (state.tour.active in the window): these move it instead of asking the agent.
TOUR_NEXT_WORDS = re.compile(r"^(下一步|继续|往下|下一个|再往下|接着|接着说|然后呢|下面呢|下面)+[吧呀啊呢]?$")
TOUR_PREV_WORDS = re.compile(r"^(上一步|回上一步|退回去|返回上一步|上一个|回去)+[吧呀啊]?$")
TOUR_STOP_WORDS = re.compile(r"^(结束引导|退出引导|停止引导|不看了|不用引导了|不用教了|结束|结束吧|好了不看了)+[吧呀啊]?$")
TOUR_ROUTES = {"tour_next": TOUR_NEXT_WORDS, "tour_prev": TOUR_PREV_WORDS, "tour_stop": TOUR_STOP_WORDS}


def tour_op(text: str) -> str | None:
    """The tour move ("next" / "prev" / "stop") these words ask for — spoken, held or typed — else None. Whether a
    tour is running is the caller's check (the window's `tour.active`)."""
    stripped = strip_punctuation(text)
    if len(stripped) > 8:
        return None
    return next((route.removeprefix("tour_") for route, words in TOUR_ROUTES.items() if words.match(stripped)), None)


def route_utterance(text: str, *, pending: bool, tour_active: bool = False,
                    product_names: list[str] | None = None, replay_active: bool = False,
                    editor_open: bool = False) -> str:
    """Where an ASR final goes: "confirm" / "cancel" (a card is waiting), "tour_next" / "tour_prev" /
    "tour_stop" (a tour is running), "editor_ask" (the window shows the 策略编辑卡 and the words are about
    the strategy's own fields — its 补一句), "replay" (a replay-bound call's playback words), "quick" (a
    quote / positions / account question Doubao answers with a Runtime tool), "chat" (Doubao answers itself) or "delegate"
    (the Runtime hands it to the agent; "新建策略" and the other fixed interface words take its zero-LLM
    fast path). Punctuation and spaces are ignored."""
    stripped = strip_punctuation(text)
    if not stripped:
        return "chat"
    if len(stripped) <= 12 and HANGUP_WORDS.match(stripped):
        return "hangup"
    if len(stripped) <= 10 and STOP_WORDS.match(stripped):
        return "stop"
    if tour_active and (op := tour_op(stripped)) is not None:
        return f"tour_{op}"
    if pending and len(stripped) <= 8:
        if CONFIRM_WORDS.match(stripped):
            return "confirm"
        if CANCEL_WORDS.match(stripped):
            return "cancel"
    if editor_open and not pending:
        # 策略编辑卡 (2026-10-08): "止损改成 20 跳" / "每次开两手" change the card's form and never reach the
        # order / quick / target routes (voiceOrderConfirm=false would confirm an order card at once);
        # "螺纹买两手" names its contract and keeps its card, interface words stay interface words.
        from .strategy_editor import editor_utterance
        if editor_utterance(stripped, product_names):
            return "editor_ask"
    if replay_active and (match_replay_control(stripped) is not None or looks_like_replay_control(stripped)):
        return "replay"  # playback words (and unplayable asks like a time jump) drive the control route
    if match_quick_query(stripped, product_names) is not None:
        return "quick"  # a plain quote / positions / account question: Doubao answers it with a Runtime tool
    if ACTION_WORDS.match(stripped):
        return "delegate"  # "全平" / "清仓" / "撤单": two characters that are an order, not small talk
    if match_intent(stripped) is not None:
        # "看图" / "深色" are two characters and an interface command: the "≤2 chars = small talk" rule
        # below used to hand them to Doubao (09-19 harness: "看图" got a chat reply and no command).
        return "delegate"  # the caller runs the zero-LLM fast path for it
    if len(stripped) <= 2 or SMALL_TALK.match(stripped):
        return "chat"
    if not pending and len(stripped) <= 4 and (CONFIRM_WORDS.match(stripped) or CANCEL_WORDS.match(stripped)):
        return "chat"
    return "delegate"


def sentence_intent(text: str, products: list[str] | None, pending: bool) -> Parsed:
    """The one reading of the words (order_intent_parse): taken back, an order the front desk can card,
    or anything else (Uncertain, a question, a condition…) that keeps its route."""
    return parse_intent(text, ParseContext(products=tuple(products or ()), pending_card=pending))


async def classify_final(service: VoiceRealtimeService, state: CallState, text: str, *,
                         consume: bool) -> tuple[str, bool, Any, Any]:
    """The route of a final (route_utterance + tour / order-watch overrides), with the pending and
    product-name inputs it was decided from. `consume` is kept for the callers' symmetry (the
    preview of an early final and the final itself route the same way)."""
    pending_now = service._has_pending(state)
    await service.ensure_product_names()
    names_known = service.product_names or None
    # 策略编辑卡: the window is read only for a sentence its 补一句 could take (a cheap word check
    # first), so ordinary speech never waits on it.
    from .strategy_editor import editor_utterance
    editor_now = (not pending_now and editor_utterance(text, names_known)
                  and await service.editor_open(state))
    route = route_utterance(text, pending=pending_now, tour_active=True, product_names=names_known,
                            replay_active=bool(state.replay_id), editor_open=editor_now)
    if route.startswith("tour_") and not await service.tour_active(state):
        route = route_utterance(text, pending=pending_now, product_names=names_known,
                                replay_active=bool(state.replay_id), editor_open=editor_now)  # no tour: the words mean what they usually mean
    # The unfilled-order watchdog's two commands act on the watched order directly;
    # with a card waiting they keep their usual meaning (cancel drops the card).
    watched = None if pending_now else service.order_watch_target(state.user_id)
    stripped_final = strip_punctuation(text)
    if watched is not None and is_watch_chase_command(stripped_final):
        route = "chase"
    elif watched is not None and is_watch_cancel_command(stripped_final):
        route = "watch_cancel"
    elif (answer := breakeven_route(stripped_final, pending=pending_now,
                                    heard=service.breakeven_target(state) is not None)):
        route = answer  # 保本提醒 just read out on this call: 移 / 不用 (a waiting card comes first)
    route = service.review_route(state, text, route)
    if pending_now and CANCEL_ALL_WORDS.fullmatch(stripped_final):
        route = "cancel"  # “都不要 / 全部取消”：作废所有待确认卡（一句两单逐张问时也包括还没问到的）
    parsed = sentence_intent(text, service.product_names, pending_now)
    # A watched order's 撤 / 追 taken back in the same sentence ("撤掉这个单，算了") is withdrawn too (#900).
    if (route in {"chat", "quick", "delegate", "watch_cancel", "chase"} and isinstance(parsed, OrderIntent)
            and parsed.stance == "retracted"):
        # 同句撤回 (#523 / #754): "开三手，不对，两手沪金多单，算了。" as one final is the user taking the
        # order back — the same as saying 算了 after it: a waiting card is dropped, a round still making
        # one is stopped, and nothing goes to the trading AI or becomes a card.
        route = "cancel" if pending_now else "retract"
    routine = (service.routine_named(state.user_id, text)
               if route in {"chat", "quick"} or (state.front_on and route == "delegate") else False)
    if route in {"chat", "quick"} and routine:
        # 我的流程 (2026-10-06): "老规矩" / "跑一下老规矩" is the user's routine — the agent runs it
        # (a two-character name would otherwise be small talk for Doubao).
        route = "delegate"
    action_pick = None
    if state.action_on and route in {"chat", "quick", "delegate"} and not routine:
        # 下单类直接出卡: an order the words state completely becomes its card here, without the
        # trading AI. With a card already waiting, a new order stays with the trading AI (it retires
        # or edits that card); a 撤 / 不做了 then drops the waiting card.
        pick = front_order(parsed) if not looks_like_ui(text, names_known) else None
        if pick is not None and pick.act != "cancel" and not state.preferences.trade.voiceOrderConfirm:
            # 语音下单免口头确认 on: the card would be confirmed the moment it is made, so an order never
            # takes the words-only path — the trading AI's turn handles it as before (the setting unchanged).
            pick = None
        if pending_now and (ACTION_CANCEL_WORDS.match(stripped_final)
                            or (isinstance(parsed, OrderIntent) and parsed.act == "retract")):
            route = "cancel"  # 有卡时“撤了 / 算了”指这张卡
        elif pick is not None and not pending_now:
            route, action_pick = "action", pick
    front_pick = None
    if state.front_on and route in {"chat", "quick", "delegate"} and not routine:
        # 前台直接查询: queries, concept questions, small talk and half sentences stay with Doubao
        # this turn. A fixed-table interface command keeps its zero-LLM path (a bare contract
        # candidate — "帮我看看" — is no interface command when the words ask a question).
        front_pick = classify_front(text, route, names_known, last_symbol=state.last_symbol_query,
                                    last_what=state.last_what)
        if front_pick is not None and route == "delegate":
            intent = match_intent(text)
            if (intent is not None and not (intent.kind == "symbol" and not intent.explicit_chart
                                            and front_pick.kind in {"query", "half"})) or looks_like_ui(text, names_known):
                front_pick = None
        if front_pick is not None:
            route = "front"
        elif route == "quick":
            route = "delegate"  # the old quick lookups are not declared on this call
    if consume:
        state.review_offer = False  # the offer is answered (or passed over) by this sentence
    return route, pending_now, names_known, front_pick or action_pick
