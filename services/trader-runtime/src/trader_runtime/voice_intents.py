"""Zero-LLM fast path for simple interface requests spoken during a call (2026-09-18, user's call
015c71f4: "打开设置" took three fillers and five seconds). The ASR final is matched against a fixed
intent table; a hit becomes one UI command with a real receipt and a one-line spoken result, well
under a second. Anything the table does not recognise still goes to the agent (delegate).

`match_intent(text)` is pure and synchronous; contract names ("切到螺纹") are resolved by the caller
through the instrument catalog and only take the fast path when they resolve to one contract."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .ui_surfaces import SURFACES


@dataclass(frozen=True)
class Intent:
    kind: str
    changes: dict[str, Any]
    say: str
    # "切到 XX 合约": the spoken contract name, resolved by the caller; `changes` is filled in then.
    symbol_query: str | None = None
    # Extra commands sent after the first one (a panel that needs a page first); each has its own receipt.
    follow: list[dict[str, Any]] = field(default_factory=list)
    # 画线第二版 (2026-10-09): a drawing change on the window's contract through the catalog route
    # (/api/market/drawings) before any command — {"op": "create", "kind", "points"} or {"op": "clear"}.
    drawing: dict[str, Any] | None = None
    # The existing contract grammar named the chart surface, not merely a contract candidate.
    explicit_chart: bool = False


_STRIP = re.compile(r"[\s，。,.!！?？、~～…·\"'“”‘’]")
_PREFIX = r"(?:请|帮我|帮忙|麻烦|给我|我要|我想|能不能|可以|能|先|再|就|然后|把|将)*"
_SUFFIX_WORDS = r"一下|下|吧|啊|呀|呢|了|好吗|可以吗|行吗|哦|嘛"
_SUFFIX = rf"(?:{_SUFFIX_WORDS})*"
_OPEN = r"(?:打开|开一下|开|进|进入|去|切到|切换到|切换|看看|看一下|看|显示|调出|调出来|弹出|展开|我要看|我想看|给我看|要看)"
_CLOSE = r"(?:关闭|关掉|关上|关|收起来|收起|收一下|收掉|收|隐藏|退出|取消)"
# "把持仓收起来" / "设置关掉": the object comes first and the verb last.
_CLOSE_TAIL = rf"(?:给)?{_CLOSE}(?:掉|上|起来|起|了)?"


def _rx(core: str) -> re.Pattern[str]:
    return re.compile(rf"^{_PREFIX}{core}{_SUFFIX}$")


# 设置页的说法和念出来的名字在界面表面表里（ui_surfaces.json 的 settingsPages：words / say，没有 say 用 label）。
_SETTINGS_PAGES = {page: entry["words"] for page, entry in SURFACES["settingsPages"].items() if "words" in entry}
_PAGE_LABELS = {page: entry.get("say", entry["label"]) for page, entry in SURFACES["settingsPages"].items() if "words" in entry}
_DURATIONS = [(r"(?:1|一)分(?:钟)?", 60, "一分钟"), (r"(?:3|三)分(?:钟)?", 180, "三分钟"), (r"(?:5|五)分(?:钟)?", 300, "五分钟"),
              (r"(?:15|十五)分(?:钟)?", 900, "十五分钟"), (r"(?:30|三十)分(?:钟)?", 1800, "三十分钟"), (r"(?:1|一)小时", 3600, "一小时"),
              (r"日线|日K|天线", 86400, "日线"), (r"周线|周K", 604800, "周线")]
_PANELS = {
    "ai": (r"AI(?:助手|面板)?", {"panel": "ai"}, "AI 面板打开了"),
    # 2026-09-25: 盯盘 is the 策略面板's 运行中 tab (the 对话面板 holds the conversation only).
    "monitor": (r"AI盯盘|盯盘|监控|运行中(?:的)?策略", {"panel": "strategy"}, "运行中的策略打开了"),
    "chat": (r"对话|聊天|语音|文字|AI对话|对话与语音", {"panel": "ai"}, "对话打开了"),
    # 2026-10-07: the 价格阶梯 panel is gone; its words (阶梯 / 五档 / 盘口) open the 下单面板 with its 五档.
    "order": (r"下单(?:台|区|面板|的地方|那个地方|那里|界面)?|模拟下单|交易台?下单|(?:价格)?阶梯|五档|盘口|深度", {"panel": "order"}, "下单台打开了"),
    # 1.6.6: 持仓 is a side panel again. Only a sentence that names the bottom bar means the bar.
    # #759: 持仓清单 is the same panel (清单 is already a POSITION_UI_NOUNS interface noun).
    "positions": (r"持仓(?:与订单|和订单|那个页|那页|页|列表|清单|面板)?|持仓订单|仓位", {"panel": "positions"}, "持仓面板打开了"),
    "positions-bar": (r"底栏|持仓栏|持仓底栏|下面那条", {"positionsBar": "expanded"}, "底栏展开了"),
    "strategy": (r"策略(?:面板|配置|页)?", {"panel": "strategy"}, "策略面板打开了"),
    # 辩论页 (2026-10-24): the right rail's 辩论 (“打开辩论”); “辩一辩 / 让 AI 们辩一轮” start one (`_debate_intent`).
    "debate": (r"辩论|AI辩论|多AI辩论|研讨|多AI研讨", {"panel": "debate"}, "辩论页打开了"),
}
# 手机版底部标签的说法（“我的”必须带动词：单说“我的”太常见）。2026-10-06 (手机版新结构): the 策略 tab takes
# the 策略面板's words (`intent_for_window`). 2026-10-15 (统一提醒 #932): 通知 / 提醒 / 消息中心 open the
# 提醒中心 (`_REMINDER_WORDS`, both layouts); 时间线 still opens AI 首页's timeline (`_NOTIFY_WORDS`).
# 画线第二版 (2026-10-09): a spoken price for “画一条水平线在 3070” (a decimal point survives as "D").
_DRAW_PRICE = r"(\d{1,6}(?:D\d{1,4})?)"
_MOBILE_TAB_WORDS = (("ai", r"AI动态|动态|AI首页"), ("research", r"调研|调研台"), ("market", r"看盘"),
                     ("me", r"我的|个人中心"))
_NOTIFY_WORDS = r"时间线"
_REMINDER_WORDS = r"提醒中心|提醒列表|通知中心|通知列表|通知|消息中心"
_MOBILE_SAY = {"ai": "到 AI 首页了", "chat": "对话打开了", "research": "调研打开了", "market": "到看盘了",
               "strategy": "策略打开了", "me": "到“我的”了"}
# A phone-tab intent on a desktop-layout window: the desktop surface that shows the same thing
# (我的 and the AI 首页 timeline's filter have none — the caller falls back to the agent).
_DESKTOP_FOR_TAB: dict[str, tuple[dict[str, Any], str]] = {"ai": ({"aiActivityCard": "open"}, "AI 动态打开了"), "chat": ({"panel": "ai"}, "对话打开了"),
                                                             "research": ({"news": {"tab": "sheet"}}, "调研打开了"),
                                                             "market": ({"view": "trade"}, "回交易台了"),
                                                             "strategy": ({"panel": "strategy"}, "策略面板打开了")}
_CLOSE_SAY = {"ai": "AI 面板收起了", "monitor": "策略面板收起了", "chat": "对话收起了", "order": "下单台收起了",
              "positions": "持仓面板收起了", "positions-bar": "底栏收起了", "strategy": "策略面板收起了", "debate": "辩论页收起了"}


# 手机看盘 (2026-10-09): the 看盘 tab's 报价 / 多图 / 单图 and the sub pane under the single chart.
_SUBPANE_WORDS = (("macd", r"MACD"), ("kd", r"KDJ|KD"), ("rsi", r"RSI"), ("cci", r"CCI"),
                  ("oi", r"持仓量|持仓"), ("volume", r"成交量|量能|量"))
_SUBPANE_SAY = {"macd": "MACD", "kd": "KDJ", "rsi": "RSI", "cci": "CCI", "oi": "持仓量", "volume": "成交量"}
_MOBILE_MARKET_SAY = {"quotes": "报价列表打开了", "multi": "多图打开了", "single": "回到单图了"}


def _mobile_market_intent(s: str) -> Intent | None:
    """“打开报价列表”“看多图”“多周期同列”“单图”“副图换成 MACD”“副图设置”: the phone's 看盘 switch and
    sub pane; `intent_for_window` gives a computer window the board for 报价列表 and the agent otherwise."""
    if _rx(rf"(?:{_OPEN}|回到|回|返回)?(?:报价列表|报价单|行情列表)(?:页|页面)?").match(s):
        return Intent("mobileMarket", {"mobileMarket": {"view": "quotes"}}, _MOBILE_MARKET_SAY["quotes"])
    if _rx(rf"(?:{_OPEN}|回到|回|返回|切到|换成)?(?:同合约)?(?:多周期|多周期同列|同列多周期)(?:图|页|页面)?").match(s):
        return Intent("mobileMarket", {"mobileMarket": {"layout": "periods"}}, "多周期同列打开了")
    if _rx(rf"(?:{_OPEN}|回到|回|返回|切到|换成)?(?:多图|多图同列|多个图|多合约)(?:看盘|页|页面)?").match(s):
        return Intent("mobileMarket", {"mobileMarket": {"view": "multi"}}, _MOBILE_MARKET_SAY["multi"])
    if _rx(rf"(?:{_OPEN}|回到|回|返回|切到|换成)?(?:单图|单个图)(?:页|页面)?").match(s):
        return Intent("mobileMarket", {"mobileMarket": {"view": "single"}}, _MOBILE_MARKET_SAY["single"])
    if _rx(rf"{_OPEN}?(?:副图设置|副图面板|指标设置|选副图)(?:页|页面)?").match(s):
        return Intent("mobileMarket", {"mobileMarket": {"subPanes": True}}, "副图设置打开了")
    if _rx(rf"(?:副图设置|副图面板|指标设置){_CLOSE_TAIL}").match(s) or _rx(rf"{_CLOSE}(?:副图设置|副图面板|指标设置)").match(s):
        return Intent("mobileMarket", {"mobileMarket": {"subPanes": False}}, "副图设置关了")
    for pane, words in _SUBPANE_WORDS:
        if _rx(rf"(?:副图|下面的图)(?:换成|换到|切到|切成|改成|改看|看|显示|用)(?:{words})(?:指标)?").match(s):
            return Intent("mobileMarket", {"mobileMarket": {"subPane": pane}}, f"副图换成{_SUBPANE_SAY[pane]}了")
    return None


_MOBILE_PAGE_SAY = {"order": "下单页打开了", "positions": "持仓与委托打开了", "review": "复盘摘要打开了", "briefs": "简报订阅打开了",
                    "install": "安装引导打开了，按上面的步骤放到桌面", "push": "锁屏提醒打开了"}


def _phone_page(intent: Intent, state: dict[str, Any]) -> Intent | None:
    """手机版第二期: the desktop panel words as the phone's full-screen pages — "打开持仓" / "看委托" →
    持仓与委托, "去下单" → 下单页, "复盘" → 复盘摘要, "订阅简报" → AI 简报推送, "收起持仓" closes the
    open page; a contract, period or 分时 request also lands on the 看盘 tab, where the chart is."""
    changes = intent.changes
    if intent.kind == "install":
        return Intent("mobilePage", {"mobilePage": "install"}, _MOBILE_PAGE_SAY["install"])
    if intent.kind == "briefs":
        return Intent("mobilePage", {"mobilePage": "briefs"}, _MOBILE_PAGE_SAY["briefs"])
    # 锁屏提醒 (2026-10-06): this phone's push switch and devices live on its 锁屏提醒 page; 设置 → 提醒
    # (2026-10-15, every kind's switch) opens as itself on a phone too.
    if intent.kind == "push":
        return Intent("mobilePage", {"mobilePage": "push"}, _MOBILE_PAGE_SAY["push"])
    if changes.get("panel") in _MOBILE_PAGE_SAY:
        return Intent("mobilePage", {"mobilePage": changes["panel"]}, _MOBILE_PAGE_SAY[changes["panel"]])
    if changes.get("view") == "review":
        return Intent("mobilePage", {"mobilePage": "review"}, _MOBILE_PAGE_SAY["review"])
    # 报价 / 自选 (2026-10-09): the phone's own 报价列表 (自选 its 自选板块).
    if changes.get("view") == "board":
        board = next((item["boardExchange"] for item in intent.follow if "boardExchange" in item), None)
        return Intent("mobileMarket", {"mobileMarket": {"board": "watchlist"} if board == "watchlist" else {"view": "quotes"}},
                      "到自选报价了" if board == "watchlist" else _MOBILE_MARKET_SAY["quotes"])
    if intent.kind == "positionsTab":
        return Intent("mobilePage", {"mobilePage": "positions"}, intent.say, follow=list(intent.follow))
    if "panel" in changes and changes["panel"] is None and state.get("mobilePage") is not None:
        return Intent("mobilePage", {"mobilePage": None}, intent.say)
    if intent.kind in {"symbol", "duration", "chartView"}:
        # 2026-10-09: the 看盘 tab's single chart, whichever of 报价 / 多图 / 单图 it showed.
        return Intent(intent.kind, dict(changes), intent.say, symbol_query=intent.symbol_query,
                      follow=[*intent.follow, {"mobileMarket": {"view": "single"}}], explicit_chart=intent.explicit_chart)
    if changes.get("view") == "trade":
        return Intent("mobileMarket", {"mobileMarket": {"view": "single"}}, "到看盘的单图了")
    return None


def intent_for_window(intent: Intent, state: dict[str, Any]) -> Intent | None:
    """The intent as the target window can take it (手机版, 2026-10-04). A phone-layout window (its state
    reports `mobileTab`) has no side panels or views: "打开对话" becomes the chat tab, "回图表" / "报价"
    the 看盘 tab, the research desk the 调研 tab, 持仓 / 下单 / 复盘 their full-screen pages. A desktop
    window gets a phone-tab intent as the surface that shows the same thing; None when it has none
    (我的, the AI 首页 timeline)."""
    tab = intent.changes.get("mobileTab")
    if (intent.kind == "install" or "mobileNotifyFilter" in intent.changes) and state.get("mobileTab") is None:
        # 安装到桌面 is the phone web page's guide and the timeline is the phone's AI 首页; a desktop
        # window hands the request to the agent.
        return None
    if "strategyEditor" in intent.changes:
        # 2026-10-08: the phone has the 策略编辑卡 too (a full-screen page); the same command opens it.
        return intent
    if "mobileMarket" in intent.changes:
        # 手机看盘 (2026-10-09): a computer window has the 报价总表 for 报价列表; the rest is the agent's.
        if state.get("mobileTab") is not None:
            return intent
        return Intent("view", {"view": "board"}, "到报价总表了") if intent.changes["mobileMarket"].get("view") == "quotes" else None
    if state.get("mobileTab") is not None:
        page = _phone_page(intent, state) if tab is None else None
        if page is not None:
            return page
        if tab is None:
            changes = intent.changes
            tab = ("chat" if changes.get("panel") == "ai" else "strategy" if changes.get("panel") == "strategy"
                   else "research" if changes.get("panel") == "debate"
                   else "market" if changes.get("view") in ("trade", "board")
                   else "research" if "news" in changes else "ai" if changes.get("aiActivityCard") == "open" else None)
        return Intent("mobileTab", {"mobileTab": tab}, _MOBILE_SAY[tab]) if tab else intent
    if tab is None:
        return intent
    desktop = _DESKTOP_FOR_TAB.get(tab)
    return Intent(intent.kind, dict(desktop[0]), desktop[1]) if desktop else None


# 口语的“带我去 / 帮我打开 / 我想看看 / 去看看 / 看一下 + (我的) + 页面名” (2026-10-06 实测：“带我去看看我的
# 策略”走模型 15 秒): only when what follows is exactly a page name is it rewritten to the canonical
# “打开…” — a sentence that asks about something ("带我看看螺纹为什么跌") never matches and stays the model's.
_NAV_PREFIX = (r"(?:请|麻烦|能不能|可以)?(?:带我去|带我到|带我|领我去|帮我打开|帮我开|帮我切到|帮我去|我想去|我要去|"
               r"我想|我要|想|去|打开|切到|进)?(?:看看|看一下|瞧瞧|瞅瞅|看)?")
_NAV_PAGES = (
    (r"(?:我的)?(?:策略|策略列表|那些策略)", "打开我的策略"),
    (r"(?:我的)?复盘", "打开复盘"),
    (r"调研|调研台", "打开调研"),
    (r"看盘", "打开看盘"),
    (r"(?:我的)?(?:通知|提醒|消息)", "打开提醒"),
    (r"时间线", "打开时间线"),
    (r"(?:我的)?流程", "打开我的流程"),
    (r"我的|个人中心", "打开我的"),
)


def _nav_page(s: str) -> str | None:
    """The canonical command for a spoken page jump, or None."""
    for words, command in _NAV_PAGES:
        if re.fullmatch(rf"(?:{words})(?:页面|页)?{_SUFFIX}", s):
            return None  # the bare page name: the table below reads it as it is ("我的" alone is no jump)
        if re.fullmatch(rf"{_PREFIX}{_NAV_PREFIX}(?:{words})(?:页面|页)?{_SUFFIX}", s):
            return command
    return None


_DEBATE_LEAD = r"(?:请|帮我|麻烦)?(?:让|叫|请)?(?:AI们|几个AI|AI|你们|大家)?"
_DEBATE_VERB = r"(?:讨论一下|讨论下|讨论讨论|讨论|研讨一下|研讨|辩一辩|辩一下|辩一轮|辩论一下|辩论)"
DEBATE_SAY = "AI 们开始研讨了，大约四十秒，好了会提醒你"


def _debate_intent(s: str) -> Intent | None:
    if re.fullmatch(rf"{_DEBATE_LEAD}{_DEBATE_VERB}(?:一下)?(?:这条|这个|这条快讯|这条消息|这条新闻|这件事)(?:快讯|消息|新闻)?{_SUFFIX}", s):
        return Intent("researchDebate", {"researchDebate": {"start": "event"}}, DEBATE_SAY)
    if re.fullmatch(rf"{_DEBATE_LEAD}(?:辩一辩|辩一轮|辩一下|研讨一下|辩论一下){_SUFFIX}", s):
        return Intent("researchDebate", {"researchDebate": {"start": "desk"}}, DEBATE_SAY)
    named = re.fullmatch(rf"{_DEBATE_LEAD}(?:辩一辩|辩一下|辩一轮|研讨一下|辩论一下|讨论一下)([\u4e00-\u9fa5A-Za-z]{{1,6}}?)(?:怎么看|的走势|走势)?{_SUFFIX}", s)
    if named and not re.search(r"这|那|今天|行情|为什么|怎么|如何|原因|新闻|消息|吗|呢", named.group(1)):
        return Intent("researchDebate", {}, DEBATE_SAY, symbol_query=named.group(1),
                      follow=[{"researchDebate": {"start": "desk"}}])
    return None


def match_intent(text: str) -> Intent | None:
    """The fixed table, longest/most specific patterns first. Returns None when the sentence is not a
    plain interface request (or carries anything else — a number, a price, a reason)."""
    # A decimal point inside a number survives the strip as "D" (画一条水平线在 826.5); no other pattern
    # carries one.
    s = _STRIP.sub("", re.sub(r"(?<=\d)[.．](?=\d)", "D", text or "")).replace("ＡＩ", "AI").upper().replace("Ａ", "A")
    s = s.replace("AI", "AI")
    if not s or len(s) > 24:
        return None
    editor = editor_intent(s)
    if editor is not None:
        return editor
    nav = _nav_page(s)
    if nav and nav != s:
        return match_intent(nav)
    # 语音通话 (AI 审计 2026-10-13): "开个语音通话，我想直接跟你说" starts the AI orb's call (disclosure
    # voice-call); the line also says what to do when the browser wants the user's own tap first.
    if _rx(r"(?:开个|开一个|开|打开|来个|来一个|打个|打一个|发起|接通)(?:语音)?(?:通话|电话)"
           r"(?:我想|我要)?(?:直接)?(?:跟你|和你)?(?:说|聊|说话|聊聊|聊天)?").match(s):
        return Intent("disclosure", {"disclosure": {"name": "voice-call", "open": True}},
                      "正在接通语音通话；要是没接通或听不到声音，点一下右下角的 AI 球"
                      "（浏览器要你亲手点一下才放开麦克风和声音）")
    # 设置：打开 / 关闭 / 某一类
    if _rx(rf"{_OPEN}?(?:系统)?设置(?:页|页面|面板|抽屉)?").match(s) or _rx(r"(?:系统)?设置(?:页|页面|面板|抽屉)?(?:给)?(?:打开|开一下|开|调出来|弹出来)").match(s):
        return Intent("settings", {"settingsOpen": True}, "设置打开了")
    if _rx(rf"{_CLOSE}(?:系统)?设置(?:页|页面|面板|抽屉)?").match(s) or _rx(rf"(?:系统)?设置(?:页|页面|面板|抽屉)?{_CLOSE_TAIL}").match(s):
        return Intent("settings", {"settingsOpen": False}, "设置关了")
    # 图表 / K线 / 看图: the trading view. Before the settings pages, whose "图表" needs the 设置 prefix
    # or an explicit "图表设置" (both anchored, so neither reads "打开图表" as the settings page).
    # The filler between the target and the verb ("图表那个东西调出来", 09-21 评测 ui-25) is allowed.
    if _rx(rf"(?:{_OPEN}|回到|回|返回|切回)?(?:图表|大图|行情图|K线图|K线|图)(?:页|页面|视图)?(?:那个|这个|那|个)?(?:东西|玩意|玩意儿|页面|页)?(?:调出来|弄出来|拿出来|显示出来|出来)?").match(s):
        return Intent("view", {"view": "trade"}, "回图表了")
    # 安装到桌面 (2026-10-06): "教我装到桌面" / "添加到主屏幕" → the phone's install guide (mobilePage install).
    if _rx(r"(?:教我|怎么|如何|我想|我要)?(?:把(?:它|这个|SENTE|软件|应用|网页|APP)?)?(?:装到|安装到|添加到|加到|放到)(?:手机)?(?:桌面|主屏幕|主屏)(?:上)?(?:去)?").match(s):
        return Intent("install", {"mobilePage": "install"}, _MOBILE_PAGE_SAY["install"])
    # 锁屏提醒 (2026-10-06): "打开锁屏提醒" / "锁屏推送设置" → 设置 → 提醒 (its 锁屏推送 section, last); a phone
    # window opens 我的 → 锁屏提醒. "发一条试试" is the agent's (POST /api/app/push/test).
    if _rx(rf"{_OPEN}?(?:锁屏提醒|锁屏推送|锁屏通知|推送通知|消息推送)(?:设置|页|页面)?").match(s):
        return Intent("push", {"settingsPage": "notify"}, "提醒设置打开了，锁屏推送在最下面")
    # AI 简报订阅 (2026-10-05): 设置 → 提醒's 订阅品种 block; a phone window opens 我的 → AI 简报推送.
    if _rx(rf"{_OPEN}?(?:AI)?(?:订阅简报|简报订阅|简报品种|订阅品种|简报推送)(?:品种|设置|页|页面)?").match(s):
        return Intent("briefs", {"settingsPage": "notify"}, "简报订阅打开了",
                      follow=[{"disclosure": {"name": "brief-subscriptions", "open": True}}])
    # 我的流程 (2026-10-06): "打开我的流程" / "看看我的流程" → 设置 → AI 与记忆, where the list lives.
    if _rx(rf"{_OPEN}?(?:我的流程|流程列表)(?:设置|页|页面)?").match(s):
        return Intent("settingsPage", {"settingsPage": "memory"}, "我的流程在 AI 与记忆这一页")
    if _rx(rf"{_OPEN}(?:问题与建议|问题和建议|反馈)(?:页|页面)?").match(s):
        return Intent("settingsPage", {"settingsPage": "feedback"}, "问题与建议打开了")
    for page, words in _SETTINGS_PAGES.items():
        # "对话" alone is the 对话面板's ("打开对话页" included); only with the 设置 prefix
        # ("设置里的对话") does it mean the ai page, so it joins the prefixed pattern only.
        prefixed = rf"{words}|对话" if page == "ai" else words
        if _rx(rf"{_OPEN}?(?:设置(?:里|里的|中|中的|的)?)?(?:{words})(?:设置|页|页面|设置页|这一页|那一页)").match(s) \
                or _rx(rf"{_OPEN}?设置(?:里|里的|中|中的|的)?(?:{prefixed})").match(s):
            return Intent("settingsPage", {"settingsPage": page}, f"{_PAGE_LABELS[page]}设置打开了")
    # 主题（含模糊说法：太亮 / 太刺眼 → 深色；太暗 / 看不清 → 浅色）
    if _rx(r"(?:屏幕|界面|颜色|背景)?(?:太亮|太刺眼|晃眼|刺眼|好亮|太白)(?:了)?").match(s):
        return Intent("theme", {"theme": "dark"}, "换成深色了")
    if _rx(r"(?:屏幕|界面|颜色|背景)?(?:太暗|太黑|好暗|看不清|太深)(?:了)?").match(s):
        return Intent("theme", {"theme": "light"}, "换成浅色了")
    if _rx(r"(?:换个|换一个|换换|换|切换|切一下|改个|调个)(?:颜色|色|主题|配色|皮肤)").match(s):
        # Toggle dark/light; resolved against the window's current theme by run_intent.
        return Intent("theme", {"theme": "toggle"}, "主题换了，要换涨跌颜色就说“换涨跌色”")
    if _rx(rf"(?:{_OPEN}|换成|换|切成|改成|改|用|调成|调|换到|切)?(?:深色|黑色|暗色|夜间|黑暗)(?:主题|模式|的)?").match(s):
        return Intent("theme", {"theme": "dark"}, "换成深色了")
    if _rx(rf"(?:{_OPEN}|换成|换|切成|改成|改|用|调成|调|换到|切)?(?:浅色|白色|亮色|日间|明亮)(?:主题|模式|的)?").match(s):
        return Intent("theme", {"theme": "light"}, "换成浅色了")
    if _rx(rf"(?:{_OPEN}|换成|换|切成|改成|改|用|调成|调|换到|切)?(?:跟随系统|跟系统|系统主题|自动主题)(?:主题|模式|的)?").match(s):
        return Intent("theme", {"theme": "system"}, "主题跟随系统了")
    # 字号（模糊说法）
    # One step down / up from the window's current size, resolved by run_intent (standard ↔ large ↔ extra-large).
    if (_rx(r"(?:字|字体|字号|文字)(?:太大|好大|大了)(?:了)?").match(s)
            or _rx(r"(?:把)?(?:字|字体|字号)(?:调|缩|弄|搞)?小(?:一点|点|些|一些)?").match(s)
            or _rx(r"(?:把)?小(?:一点|点|些|一些)?字(?:调小|缩小|弄小)?").match(s)):
        return Intent("fontScale", {"fontScale": "smaller"}, "字调小了")
    if (_rx(r"(?:字|字体|字号|文字)(?:太小|好小|有点小|看不清|小了)(?:了)?").match(s)
            or _rx(r"(?:把)?(?:字|字体|字号)(?:调|放|弄|搞)?大(?:一点|点|些|一些)?").match(s)
            or _rx(r"(?:把)?大(?:一点|点|些|一些)?字(?:调大|放大|弄大)?").match(s)):
        return Intent("fontScale", {"fontScale": "larger"}, "字放大了")
    market = _mobile_market_intent(s)
    if market is not None:
        return market
    # 交易台 / 报价总表 / 复盘
    if _rx(rf"(?:{_OPEN}|回到|回|返回)?(?:复盘)(?:页|页面|视图)?").match(s):
        return Intent("view", {"view": "review"}, "到复盘页了")
    # 自选 (2026-10-02): the board filtered to the starred products; “自选” never names a contract,
    # so it must land here before any product-word matching could take it.
    if _rx(rf"(?:{_OPEN}|回到|回|返回|只看)?(?:我的自选|自选)(?:品种|列表)?(?:页|页面|视图)?").match(s):
        return Intent("view", {"view": "board"}, "到自选了", follow=[{"boardExchange": "watchlist"}])
    if _rx(rf"(?:{_OPEN}|回到|回|返回)?(?:看全部品种|全部品种|报价表|报价总表|总表|行情表|报价)(?:页|页面|视图)?").match(s):
        return Intent("view", {"view": "board"}, "到报价总表了")
    # 分时图 (2026-10-02): a chart view, not a duration — matched before the periods so "切到分时"
    # never reads as one. Leaving it goes through the periods ("看周线") or the plain 回K线 view
    # words below; every duration command returns the chart to the K 线 form on the client.
    if _rx(rf"(?:{_OPEN}|换成|换|切成|改成|用|调成|调到|切)?分时(?:图)?").match(s):
        return Intent("chartView", {"chartView": "timeshare"}, "分时图打开了")
    if _rx(rf"(?:{_OPEN}|回到|回|返回)?(?:交易台|交易页|盯盘页|行情页|主界面|主页|K线|K线图|图表页)(?:页|页面|视图)?").match(s):
        return Intent("view", {"view": "trade"}, "回交易台了")
    # 策略 (2026-10-06; 第八批 2026-10-08): "看看我的策略" / "打开策略库" → the 策略面板's 我的策略, "看回测" /
    # "打开实验室" → the same tab (every card shows its latest backtest, a card opens its 结果页 with the
    # 试几组参数比一比 that replaced the 实验室); a phone window opens its 策略 tab.
    if _rx(rf"{_OPEN}?(?:我的策略|策略列表|我的那些策略|策略库)(?:页|页面)?").match(s):
        return Intent("panel", {"panel": "strategy"}, "策略面板打开了", follow=[{"strategyTab": "library"}])
    if (_rx(rf"{_OPEN}(?:回测|回测结果|最近的回测|回测记录|实验室|策略实验室)(?:页|页面)?").match(s)
            or _rx(r"(?:回测结果|最近的回测)").match(s)):
        return Intent("panel", {"panel": "strategy"}, "策略面板打开了，每张策略卡上有最近一次回测，点开看结果页",
                      follow=[{"strategyTab": "library"}])
    # 提醒中心 (统一提醒 #932, 2026-10-15): "打开提醒" / "看看通知" / "未读的提醒" / "关掉提醒中心" — the bell's
    # popover on a computer, the 提醒 page on a phone (one command, either layout).
    if _rx(rf"{_CLOSE}(?:提醒中心|提醒列表|通知中心|通知列表|消息中心)(?:页|页面)?").match(s) or _rx(rf"(?:提醒中心|提醒列表|通知中心|通知列表|消息中心)(?:页|页面)?{_CLOSE_TAIL}").match(s):
        return Intent("reminderCenter", {"reminderCenter": {"open": False}}, "提醒中心关了")
    if _rx(rf"{_OPEN}?(?:未读的?|没读的?|没看的?)(?:{_REMINDER_WORDS}|提醒)").match(s) or _rx(rf"{_OPEN}?(?:{_REMINDER_WORDS}|提醒)(?:里)?(?:的)?(?:未读|没读的|没看的)").match(s):
        return Intent("reminderCenter", {"reminderCenter": {"open": True, "filter": "unread"}}, "提醒中心打开了，只看未读")
    if _rx(rf"(?:{_OPEN}|回到|回|返回)?(?:我的)?(?:{_REMINDER_WORDS})(?:页|页面)?").match(s) or _rx(rf"(?:{_OPEN}|回到|回|返回|我的)(?:我的)?提醒(?:页|页面)?").match(s):
        return Intent("reminderCenter", {"reminderCenter": {"open": True}}, "提醒中心打开了")
    # 时间线 (2026-10-06): the phone's AI 首页 timeline, all of it (a desktop window falls back to the agent).
    if _rx(rf"(?:{_OPEN}|回到|回|返回)?(?:{_NOTIFY_WORDS})(?:页|页面)?").match(s):
        return Intent("mobileTab", {"mobileNotifyFilter": "all"}, "到 AI 首页的时间线了")
    # 手机版底部标签 (2026-10-04): "打开调研" / "回到看盘" / "打开我的"; `intent_for_window` maps them onto
    # a desktop window's own surfaces (or refuses), and the desktop words above onto the phone's tabs.
    for tab, words in _MOBILE_TAB_WORDS:
        # The words are alternatives: grouped, or "通知…" / "调研…" would match any sentence they start.
        if _rx(rf"(?:{_OPEN}|回到|回|返回)(?:{words})(?:页|页面)?").match(s) or (tab != "me" and _rx(rf"(?:{words})(?:页|页面)?").match(s)):
            return Intent("mobileTab", {"mobileTab": tab}, _MOBILE_SAY[tab])
    # 周期
    for words, seconds, label in _DURATIONS:
        if _rx(rf"(?:{_OPEN}|换成|换|切成|改成|用|调成|调到|切)?(?:{words})(?:线|K线|周期|图|的图|的)?").match(s):
            return Intent("duration", {"duration": seconds}, f"切到{label}了")
    # 侧栏 / 面板
    for key, (words, changes, say) in _PANELS.items():
        if _rx(rf"{_OPEN}(?:{words})(?:面板|区|页)?").match(s):
            follow = [{"strategyTab": "running"}] if key == "monitor" else []
            return Intent("panel", changes, say, follow=follow)
        if _rx(rf"{_CLOSE}(?:{words})(?:面板|区|页)?").match(s) or _rx(rf"(?:{words})(?:面板|区|页)?{_CLOSE_TAIL}").match(s):
            return Intent("panel", {"positionsBar": "collapsed"} if key == "positions-bar" else {"panel": None}, _CLOSE_SAY[key])
    # 挡着的那个东西：不点名面板的收起（"把那个边上的东西收起来" / "旁边的关掉"）。语义歧义交给
    # 模型时它改了停靠宽度而不是收面板（09-20 评测 ui 类失败）：这里是关当前侧栏。
    if (_rx(rf"(?:那个)?(?:边上|旁边|侧边|侧栏|右边|左边|那个面板|那玩意)(?:的)?(?:东西|玩意|面板|栏|页|那个)?{_CLOSE_TAIL}").match(s)
            or _rx(rf"{_CLOSE}(?:那个)?(?:边上|旁边|侧边|侧栏)(?:的)?(?:东西|玩意|面板|栏|页)?").match(s)):
        return Intent("panel", {"panel": None}, "面板收起了")
    # 持仓分页
    for tab, words, label in (("positions", r"持仓(?:列表|分页|页|这一页)", "持仓"), ("orders", r"委托(?:列表|分页|页|单)?", "委托"), ("fills", r"(?:平仓记录|成交记录|成交|平仓)(?:列表|分页|页)?", "成交")):
        if _rx(rf"{_OPEN}(?:{words})").match(s):
            follow = [{"positionsTab": tab}]
            return Intent("positionsTab", {"positionsBar": "expanded"}, f"切到{label}了", follow=follow)
    # 帮助
    if _rx(rf"{_OPEN}?(?:帮助|说明|使用说明|帮助卡)(?:页|卡片|面板)?").match(s):
        return Intent("dialog", {"dialog": {"name": "help", "open": True}}, "说明打开了")
    if _rx(rf"{_CLOSE}(?:帮助|说明|使用说明|帮助卡)(?:页|卡片|面板)?").match(s):
        return Intent("dialog", {"dialog": {"name": "help", "open": False}}, "说明关了")
    # 图表：返回原图 / 清除标注
    if _rx(r"(?:返回|回到|回|恢复)(?:原图|最新|全部|全图|现在|当前)(?:的图|图|位置)?").match(s):
        return Intent("chartFocus", {"chartFocus": None}, "回到最新了")
    if (_rx(r"(?:清除|清掉|清空|删掉|删除|去掉|擦掉|擦了|把)?(?:AI)?(?:刚才|刚刚|之前)?(?:标注|画的线|画的|标记|图上的线|画的东西)(?:清除|清掉|清空|删掉|删除|去掉|擦掉|擦了|都清掉|都去掉)?").match(s)
            and re.search(r"清|删|去掉|擦", s)):
        return Intent("chartAnnotations", {"chartAnnotations": {"op": "clear"}}, "标注清掉了")
    # 新对话
    if _rx(r"(?:开|开个|开一个|新建|新开|开始|来个|来一个|帮我开个|帮我开)?(?:一个)?新(?:的)?(?:对话|会话|聊天)").match(s):
        return Intent("chatSession", {"chatSession": {"new": True}}, "新对话开好了")
    # 历史对话：AI 面板的会话列表（chat-history 折叠开关），typed 和 voice 共用这张表。
    # 09-20 评测 ui-10（"上一次聊的在哪"）绕去 app_call/highlight_ui 一整轮：固定表一刀解决。
    if (_rx(rf"(?:{_OPEN})?历史(?:的)?(?:对话|会话|聊天)(?:列表|记录)?(?:在哪|在哪里|哪里|看看|看下|看一下|调出来)?").match(s)
            or _rx(r"(?:上一次|上次|之前|以前)(?:聊|说|谈)的?(?:天|话|内容|对话|记录)?(?:在哪|在哪里|哪里|打开|看看|看下|调出来)?").match(s)):
        return Intent("chatHistory", {"disclosure": {"name": "chat-history", "open": True}}, "历史对话打开了")
    # 合约信息卡 (2026-10-02)：行情头部的 ⓘ 弹层（一手/资金/时间/交割）。必须排在合约名
    # 解析之前——“打开合约信息”的“合约信息”长得就像一个合约名。
    if (_rx(rf"{_OPEN}(?:这个)?合约信息(?:卡|卡片)?").match(s)
            or _rx(rf"{_OPEN}(?:这个合约|当前合约|该合约)的?信息(?:卡|卡片)?").match(s)
            or _rx(r"(?:这个合约|当前合约|该合约)?的?合约信息(?:卡|卡片)?(?:在哪|在哪里|哪里|看看|看一下|调出来)").match(s)):
        return Intent("disclosure", {"disclosure": {"name": "contract-info", "open": True}}, "合约信息打开了")
    if (_rx(rf"{_CLOSE}(?:这个)?合约信息(?:卡|卡片)?").match(s)
            or _rx(rf"(?:这个合约|当前合约|该合约)?的?合约信息(?:卡|卡片)?{_CLOSE_TAIL}").match(s)):
        return Intent("disclosure", {"disclosure": {"name": "contract-info", "open": False}}, "合约信息关了")
    # 画线工具 (2026-10-02)：图表工具条的画线浮层（水平线/趋势线/区间）。排在合约名解析之前
    # ——“打开画线”的“画线”长得就像一个合约名。要 AI 替画的话（“在 3150 画一条线”）带着价位，
    # 不匹配这里的锚定形状，仍交给 agent 的 chart-drawings 技能。
    # 画线第二版 (2026-10-09)：“画一条水平线在 3070”直接画（只认点名“水平线”带价位的固定形状，
    # “帮我在 3150 画一条线”这类仍交给 chart-drawings 技能）；“删掉所有画线”清空本合约；“隐藏/显示画线”
    # 是工具栏的全部隐藏开关。都排在“打开/关掉画线工具”之前（_OPEN 含“显示”、_CLOSE 含“隐藏”）。
    hline = (re.match(rf"^{_PREFIX}(?:画|画一条|画条|画个|加一条|加条|标一条)水平线(?:在|到|于)?{_DRAW_PRICE}(?:的位置|那里|这里|处|这个价)?{_SUFFIX}$", s)
             or re.match(rf"^{_PREFIX}(?:在|到|于){_DRAW_PRICE}(?:的位置|那里|这里|处|这个价)?(?:画|画一条|画条|画个|加一条|加条|标一条)水平线{_SUFFIX}$", s))
    if hline:
        words = hline.group(1).replace("D", ".")
        price = float(words)
        if price > 0:
            return Intent("drawing", {}, f"画好了，{words} 一条水平线",
                          drawing={"op": "create", "kind": "hline", "points": {"price": price}})
    if (_rx(r"(?:删掉|删除|删了|清空|清除|清掉|去掉)(?:这个合约的?|本合约的?|图上的?)?(?:所有|全部)(?:的)?画线").match(s)
            or _rx(r"(?:清空|清除|清掉)(?:这个合约的?|本合约的?|图上的?)?画线").match(s)
            or _rx(r"(?:这个合约的?|本合约的?|图上的?)?(?:所有|全部)?画线(?:都|全部|全)(?:删掉|删了|删除|清空|清掉|去掉)").match(s)):
        return Intent("drawing", {}, "", drawing={"op": "clear"})
    if (_rx(r"(?:隐藏|藏起|藏起来|先隐藏)(?:所有|全部)?(?:的)?画线").match(s)
            or _rx(r"(?:所有|全部)?(?:的)?画线(?:都|全部|先)?(?:隐藏|藏起来|藏起)(?:起来)?").match(s)):
        return Intent("disclosure", {"disclosure": {"name": "chart-drawings-hidden", "open": True}}, "画线都隐藏了")
    if (_rx(r"(?:显示|恢复显示|重新显示|恢复|取消隐藏)(?:所有|全部)?(?:的)?画线").match(s)
            or _rx(r"(?:所有|全部)?(?:的)?画线(?:都|全部)?(?:显示出来|显示|露出来)").match(s)):
        return Intent("disclosure", {"disclosure": {"name": "chart-drawings-hidden", "open": False}}, "画线显示出来了")
    if _rx(rf"{_OPEN}(?:画线|画图)(?:工具|工具栏)?").match(s):
        return Intent("disclosure", {"disclosure": {"name": "chart-drawing-toolbar", "open": True}}, "画线工具栏出来了")
    if (_rx(rf"{_CLOSE}(?:画线|画图)(?:工具|工具栏)?").match(s)
            or _rx(rf"(?:画线|画图)(?:工具|工具栏|浮层|面板)?{_CLOSE_TAIL}").match(s)):
        return Intent("disclosure", {"disclosure": {"name": "chart-drawing-toolbar", "open": False}}, "画线工具栏收起了")
    # 今日作战表 (2026-10-25 第 5 批)：调研面板的第一页。排在合约名解析之前。
    if _rx(rf"{_OPEN}(?:今日|今天)?(?:的)?作战表").match(s):
        return Intent("news", {"news": {"tab": "sheet"}}, "今日作战表打开了")
    # 快讯 (2026-10-03)：新闻面板的快讯页签。排在合约名解析之前——“看快讯”的“快讯”长得就像一个合约名。
    if _rx(rf"{_OPEN}(?:最新|最近)?(?:的)?快讯(?:页|页签|列表)?").match(s):
        return Intent("news", {"news": {"tab": "flash"}}, "快讯打开了")
    # 今日新闻要点 (2026-10-07) 在快讯页最上面：“看新闻要点”“打开今日新闻要点”同样打开快讯页。
    if _rx(rf"{_OPEN}(?:今日|今天)?(?:的)?新闻要点").match(s):
        return Intent("news", {"news": {"tab": "flash"}}, "今日新闻要点在快讯页最上面")
    # 多 AI 研讨 (2026-10-11)：“让 AI 们讨论一下这条”研讨当前品种最新一条重要快讯；“辩一辩螺纹”切到该品种再辩今日议题；
    # 不带品种的“让 AI 们辩一轮”辩正在看的品种。排在合约名解析之前——“辩一辩螺纹”不是一个合约名。
    debate = _debate_intent(s)
    if debate is not None:
        return debate
    # 合约：只认“切到/换到/看 XX（合约）”，名称交给目录解析
    contract = re.match(rf"^{_PREFIX}(?:切换到|切换成|切到|换到|换成|切|看看|看一下|我要看|我想看|给我看|要看|看|打开|选)(.{{1,12}}?)(?:这个)?(?P<surface>合约|的合约|的图|图)?{_SUFFIX}$", s)
    if contract:
        query = contract.group(1)
        # A contract name is a product word plus an optional month: nothing else may ride along.
        if query and re.fullmatch(r"[\u4e00-\u9fa5A-Za-z]{1,6}(?:\d{3,4})?", query) \
                and not re.search(r"设置|主题|分钟|小时|日线|持仓|委托|对话|帮助|复盘|交易台|标注|深色|浅色|然后|再|买|卖|平|手"
                                  # 指标是图表偏好（2026-10-02），不是合约名："打开布林带"交给 agent 改偏好。
                                  # 画线是用户画线工具（2026-10-02）："打开画线"是 disclosure，其余画线话交给 agent。
                                  r"|均线|布林|副图|指标|画线|画的线|画图|(?i:rsi|macd|kdj?|boll)", query):
            return Intent("symbol", {}, "", symbol_query=query,
                          explicit_chart=contract.group("surface") in {"的图", "图"})
    return None


def resolve_symbol(query: str, items: list[dict[str, Any]]) -> dict[str, Any] | None:
    """One contract for a spoken name, or None (then the agent decides). Exact symbol / instrumentId
    first, then a name that contains the query; among several the main contract wins, ties lose."""
    q = query.strip()
    if not q:
        return None
    lower = q.lower()
    exact = [i for i in items if str(i.get("symbol", "")).lower() == lower or str(i.get("instrumentId", "")).lower() == lower]
    if len(exact) == 1:
        return exact[0]
    named = [i for i in items if q in str(i.get("instrumentName", "")) or lower == str(i.get("productId", "")).lower()]
    if len(named) == 1:
        return named[0]
    main = [i for i in named if i.get("isMain") is True]
    if len(main) == 1:
        return main[0]
    return None


# Spoken aliases for catalog product names, shared by the quick-query word check and the Runtime's
# product pre-resolution (one table, no copies). The value must be a catalog name to count.
PRODUCT_ALIASES = {"金子": "沪金", "黄金": "沪金", "银子": "沪银", "白银": "沪银", "股指": "沪深300股指"}


def match_product_word(text: str, product_names: list[str] | None) -> tuple[str, str] | None:
    """(spoken word, catalog product name) when the sentence names exactly one catalog product (see
    product_words for what counts as naming one); None when it names none, or two or more."""
    hits = product_words(text, product_names)
    return hits[0] if len(hits) == 1 else None


def product_words(text: str, product_names: list[str] | None) -> list[tuple[str, str]]:
    """Every catalog product the sentence names, once each, as (spoken word, catalog product name):
    the whole name ("螺纹钢现在多少钱"), a prefix of a ≥3-char name with ≥2 non-digit chars ("螺纹" →
    螺纹钢, "铁矿" → 铁矿石; a digit-led name like 20号胶 answers only to its whole name, so
    "止损20跳" / "20手" name nothing), or an alias ("金子" → 沪金).
    Uniqueness is over the hit products, not the spoken words: two products sharing a prefix
    ("中证500股指" / "中证1000股指" both answer to "中证") name two, while one product hit by both
    its whole name and an alias ("金子" 与 "沪金" 同句) still names one. A prefix that only occurs
    inside another product's whole-name hit ("菜籽" in "菜籽油咋样") is not a second name."""
    if not text or not product_names:
        return []
    lowered = text.lower()
    hits: list[tuple[str, str]] = []  # (the word the user said, catalog product name)
    claimed: set[str] = set()
    spans: list[tuple[int, int]] = []  # text ranges already explained by a whole-name hit
    for product in product_names:
        catalog = str(product).lower()
        if catalog and catalog in lowered:
            hits.append((product, product))
            claimed.add(product)
            start = lowered.find(catalog)
            spans.append((start, start + len(catalog)))
    for product in product_names:
        if product in claimed or len(product) < 3:
            continue
        for size in range(2, len(product)):
            prefix = product[:size]
            if sum(not char.isdigit() for char in prefix) < 2:
                continue  # "20" / "20号" of 20号胶 is a size ("止损20跳", "20手"), not a product word
            at = text.find(prefix)
            if at < 0:
                break  # a longer prefix contains this one; it cannot occur either
            if any(start <= at < end for start, end in spans):
                continue  # this occurrence sits inside another product's whole-name hit ("菜籽" in "菜籽油")
            hits.append((prefix, product))
            claimed.add(product)
            break
    for alias, product in PRODUCT_ALIASES.items():
        if alias in text and product in product_names and product not in claimed:
            hits.append((alias, product))
    named: dict[str, tuple[str, str]] = {}
    for word, product in hits:
        named.setdefault(product, (word, product))
    return sorted(named.values(), key=lambda hit: lowered.find(hit[0].lower()))  # in the order they were said


# -- V1.3 quick queries: three read-only questions Doubao answers itself with a Runtime tool -----------
# A spoken "螺纹多少钱" / "手里有什么" / "还剩多少钱" needs no agent turn: the Runtime routes it as `quick`,
# Doubao calls get_quote / get_positions / get_account and reads the numbers. Anything that asks for an
# action, an opinion or an explanation stays with the agent (delegate).

@dataclass(frozen=True)
class QuickQuery:
    kind: str  # quote | positions | account
    symbol_query: str | None = None  # the spoken contract name for a quote; None = the contract on screen


# 到价提醒的说法（2026-10-02；“…叫我 / 喊我”见 AI 审计 2026-10-13）：“螺纹到3500提醒我”要建提醒，不是行情快查。
# 语音快查、语音前台与打字快查共用这一份，命中就交给有 create_price_alert 的完整回合。
ALERT_WORDS = r"提醒|预警|通知我|叫我|喊我|告诉我一声"

QUICK_EXCLUDE = re.compile(
    r"买|卖|开|平|撤|挂|下单|止损|止盈|加仓|减仓|设置|打开|关|切|画|标|新闻|为什么|原因|影响|能不能|该不该|建议|看法|分析|走势|形态|指标|K线|"
    rf"{ALERT_WORDS}|"
    r"是不是|会不会|要不要|还会|还能|要跌|要涨|接下来|后面|预测|判断|这波|行不行|"
    # 指到某个界面（页 / 面板 / 调出 / 显示）是界面请求，先过意图表：09-20 评测 ui-05
    # ("我要看持仓那个页") 被当成行情快查、意图落空。
    r"页|面板|调出|显示")
_ACCOUNT_WORDS = re.compile(r"账户|资金|余额|权益|保证金|还剩多少钱|还有多少钱|可用")
_POSITION_WORDS = re.compile(r"手里|持仓|仓位|亏|赚|盈亏|浮盈|浮亏")
_QUOTE_WORDS = re.compile(r"多少钱|多少|价格|价位|价|涨了没|跌了没|涨|跌|成交量|量|行情|怎么样|什么价|报价")
# What is left of a quote question once the question words are gone should be a contract name.
_QUOTE_NOISE = re.compile(
    r"现在|今天|目前|当前|最新|请问|问一下|帮我|给我|看看|看一下|查一下|查查|查|报一下|报|说一下|说|告诉我|一下|多少钱|价格|价位|"
    r"什么价|报价|涨了没|跌了没|涨了多少|跌了多少|涨跌|涨|跌|成交量|量|行情|怎么样|刚才|刚刚|那个|这个|它|是|的|呢|吧|啊|呀|了|什么|多少|吗|嘛|那|个|价")
_MULTI = re.compile(r"和|与|跟|还有|都|或")
_NAME = re.compile(r"^[一-龥A-Za-z]{1,6}(?:\d{3,4})?$")


def match_quick_query(text: str, product_names: list[str] | None = None) -> QuickQuery | None:
    """`QuickQuery` for a plain quote / positions / account question, else None. Pure and synchronous.
    With `product_names` (the catalog's Chinese product names) a non-empty `symbol_query` must be one of
    them, or contain one: "铁矿今天量大不大" leaves "铁矿大不大", which is not a contract, so the agent
    answers. Without the list only the shape is checked."""
    s = _STRIP.sub("", text or "")
    if not s or len(s) > 24 or QUICK_EXCLUDE.search(s):
        return None

    def known(name: str) -> bool:
        # Case-insensitive: the catalog carries Chinese names and productId codes ("IF"), and the
        # ASR may hand back either case ("if现在多少钱"). A spoken alias counts only as the whole
        # leftover ("金子"), never inside a longer phrase ("我两手金子" is not a quote question).
        if product_names is None:
            return True
        lowered = name.lower()
        for product in product_names:
            catalog = product.lower()
            if lowered == catalog or lowered in catalog:
                return True
            if catalog in lowered and len(lowered) <= len(product) + 4 and lowered.replace(catalog, "").isdigit():
                return True
        return any(lowered == alias and product in product_names for alias, product in PRODUCT_ALIASES.items())
    if _ACCOUNT_WORDS.search(s):
        return QuickQuery("account")
    if _POSITION_WORDS.search(s):
        rest = re.sub(r"有什么|有啥|有哪些|有没有|情况|怎样|如何|多少|几手|几个|什么|啥|现在|我|的|呢|吧|啊|呀|了|吗|有|赚了没|亏了没|没", "", _POSITION_WORDS.sub("", s))
        rest = _QUOTE_NOISE.sub("", rest)
        if rest and (not _NAME.match(rest) or _MULTI.search(rest) or not known(rest)):
            return None
        return QuickQuery("positions", rest or None)
    if _QUOTE_WORDS.search(s):
        rest = _QUOTE_NOISE.sub("", s)
        if rest and (not _NAME.match(rest) or _MULTI.search(rest) or not known(rest)):
            return None  # something else rides along ("螺纹和沪金都多少钱", "铁矿大不大"): the agent decides
        return QuickQuery("quote", rest or None)
    # A follow-up on the previous quote: "那沪金呢" / "沪金呢".
    follow = re.fullmatch(r"那?(.{1,8}?)(?:呢|现在呢|的呢|又呢)", s)
    if follow and _NAME.match(follow.group(1)) and known(follow.group(1)) and not re.search(r"持仓|账户|设置|对话|帮助|复盘|交易台|标注|深色|浅色", follow.group(1)):
        return QuickQuery("quote", follow.group(1))
    return None


# -- Pure interface work: shared UI Owner --------------------------------------------------------
# The fixed table owns known commands; other pure interface requests share the Native UI owner.
# This boundary distinguishes interface work from trading, editing and analysis, without mapping
# the user's words to a second action vocabulary.

_UI_VERBS = re.compile(r"打开|开一下|开|关闭|关掉|关上|关|收起|收一下|收|隐藏|退出|切到|切换|切|换成|换到|换个|换|调出|调到|调成|调|显示|看看|看一下|看|回到|返回|进入|弹出|展开")
_UI_TARGETS = re.compile(
    r"对话|聊天|语音|策略|阶梯|五档|盘口|深度|下单|持仓|仓位|委托|成交|平仓记录|设置|图表|K线|图|复盘|回放|主题|配色|颜色|皮肤|深色|浅色|亮色|暗色|"
    r"跟随系统|字号|字体|字|周期|分钟|小时|日线|周线|分时|合约|面板|页面|界面|底栏|侧栏|标注|帮助|说明|交易台|行情页|主界面|盯盘|监控")
# Research and preference words: these belong to the trading agent, never to a UI command. Order words are not
# listed here: the order-intent parser decides them (looks_like_ui → order_intent_parse.trade_talk, 意图重构第 3–4 步),
# including the spoken 全平 / 全撤 shapes (「关掉所有持仓 / 把委托都撤了」, `order_intent_parse.book_words`).
# 界面部件词（#738）：判断「还是界面话」的证据只认真实界面部件，不是 `_UI_TARGETS` 的全部目标词——合约/周期/分钟这类
# 泛词出现在动作之后是限定范围或时限的清仓话。「页面?」同时收下「持仓页」这个 `_PANELS` 既有别名。清单 / 平仓记录是
# 持仓的界面名词，不是平、清。
POSITION_UI_NOUNS = r"清单|平仓记录"
UI_SURFACE_WORDS = re.compile(rf"面板|页面?|界面|窗口|栏|视图|列表|图层|显示|{POSITION_UI_NOUNS}")
POSITION_SURFACE_NAMES = re.compile(rf"(?:持仓|仓位)(?:的)?(?:{UI_SURFACE_WORDS.pattern})")
_UI_EXCLUDE = re.compile(
    r"止损|止盈|新闻|消息|为什么|原因|影响|分析|走势|形态|指标|基差|库存|仓单|建议|看法|该不该|要不要|会不会|预测|判断|回测|语速|音色|播报|推送|"
    r"什么|怎么|如何|多少|几|盈亏|亏了|赚了|浮盈|浮亏|账户|资金|权益|保证金|"
    # 图表指标的增减与参数是账户偏好（2026-10-02 指标可配置）：交给 agent 的偏好工具，不是界面命令。
    r"均线|布林|副图|持仓量|(?i:rsi|macd|kdj?|boll)|"
    # AI 自主执行、语音下单免口头确认与策略的执行方式是交易开关（#932 复核）：界面命令改不了，交给 agent 的
    # update_preferences / start_strategy_monitoring。
    r"自主|口头确认|执行方式|自动下单|出卡确认|"
    # 复合句（"切到铁矿然后看看持仓"）一半是界面一半是业务：交给交易 agent，不当纯界面话。
    r"然后|先|再|接着|顺便|同时|之后|并且|"
    # 持仓的提醒/通知/预警是提醒设置，界面工具做不了，交给 agent（#722）。
    r"(?:持仓|仓位)(?:的)?(?:提醒|通知|预警)|"
    # "看看图上这根线" / "帮我看看这波" 要的是讲解，不是把图表打开：动词和对象都像界面话，只有这些词能分开。
    # 「线」前面是周期或均线的字时不算（"切换一下K线的周期" 仍是界面话）。
    r"(?<![K日月周均分时])线|根|这段|这波|那波|讲|解释|说说|标|画|指给")


def looks_like_ui(text: str, products: list[str] | tuple[str, ...] | None = None) -> bool:
    """Whether the sentence is interface work for the shared UI owner: an interface verb
    and an interface target, and nothing that belongs to the trading agent — words about an order are the
    order-intent parser's to tell (`products`: the catalog's product names it reads them against). Pure."""
    s = _STRIP.sub("", text or "")
    if not s or len(s) > 20 or _UI_EXCLUDE.search(s):
        return False
    if not (_UI_VERBS.search(s) and _UI_TARGETS.search(s)):
        return False
    from .order_intent_parse import trade_talk
    return not trade_talk(s, products)


# -- 策略编辑卡 (2026-10-08): the words that open it ---------------------------------------------------
# "新建策略" / "帮我做一个策略" / "设计个策略" open the card on a new strategy (起点默认按我的习惯);
# "根据我的交易生成策略" / "按我的习惯做个策略" open it from the habits prefill; "继续编辑策略" reopens the
# unsaved draft. A sentence that also carries an idea ("帮我做一个均线突破策略") is not one of these: it
# stays with the agent. The retired guided design's own words ("引导我设计策略") open the card too.
_EDITOR_LEADIN = r"(?:请|帮我|帮忙|麻烦|给我|我想|我要|我想要|想|要|来|能不能|可以|能|你|再)?"
_EDITOR_ONE = r"(?:一个新的?|一个|一条|个新的?|个|条|新的?)?"
EDITOR_NEW_WORDS = re.compile(
    rf"^{_EDITOR_LEADIN}(?:生成|新建|新增|设计|做|建|写|弄|搞|创建|编|开)(?:一下)?{_EDITOR_ONE}(?:的)?(?:交易)?策略"
    rf"(?:吧|呀|啊|呢)?$"
    rf"|^{_EDITOR_LEADIN}(?:语音|问着|提问|聊天|带着|陪)?引导(?:我|我们)?(?:来|一起)?(?:设计|做|写|建|弄)?{_EDITOR_ONE}(?:交易)?策略$"
    rf"|^(?:打开|开)?新建策略$")
EDITOR_HABITS_WORDS = re.compile(
    rf"^{_EDITOR_LEADIN}(?:根据|按照|按|照着|用|从|依照)我的?(?:交易习惯|交易记录|历史交易|交易|成交|习惯|操作)(?:来|里|中)?"
    rf"(?:归纳|总结|生成|新建|设计|做|建|写|弄|搞|出|整理)?(?:出|一下)?{_EDITOR_ONE}(?:交易)?策略(?:吧|呀|啊|呢)?$")
EDITOR_RESUME_WORDS = re.compile(r"^(?:继续|接着)(?:编辑|设计|改|做)(?:一下)?(?:那个|刚才的|我的)?(?:策略)?$"
                                 r"|^(?:打开|开|调出|调出来)?(?:策略编辑卡|策略编辑器|策略编辑)$")
EDITOR_OPEN_SAY = "我打开了策略编辑卡。"


def editor_intent(s: str) -> Intent | None:
    """The 策略编辑卡's opening words as an interface intent (see EDITOR_*_WORDS); `s` is stripped."""
    if EDITOR_HABITS_WORDS.match(s):
        return Intent("strategyEditor", {"strategyEditor": {"new": True, "start": "habits"}}, EDITOR_OPEN_SAY)
    if EDITOR_RESUME_WORDS.match(s):
        return Intent("strategyEditor", {"strategyEditor": {"open": True}}, EDITOR_OPEN_SAY)
    if EDITOR_NEW_WORDS.match(s):
        return Intent("strategyEditor", {"strategyEditor": {"new": True}}, EDITOR_OPEN_SAY)
    return None


# -- 我的流程 (2026-10-06): the routine's exact name, or "跑一下 / 运行 + name", runs it --------------
_ROUTINE_MARKS = re.compile(r"[「」『』《》【】]")
_ROUTINE_RUN_PREFIX = re.compile(r"^(?:请|帮我|给我|麻烦)?(?:跑一下|跑一遍|跑下|跑|运行一下|运行|执行一下|执行|走一遍|来一遍|做一遍)"
                                 r"(?:一下|一遍)?(?:我的流程|流程)?")
_ROUTINE_TAIL = re.compile(r"(?:吧|呀|啊|呢|了)+$")


def routine_request(text: str, names: list[str] | tuple[str, ...] | set[str] | dict[str, str]) -> str | None:
    """The routine a sentence runs: the whole sentence is one of `names` ("老规矩"), or a run verb and
    the name ("跑一下老规矩" / "运行「收盘检查」"). Anything else — "老规矩是什么意思", "老规矩去掉回测" —
    is None: a routine only runs when the user asks for exactly it. Pure and synchronous."""
    s = _ROUTINE_MARKS.sub("", _STRIP.sub("", text or ""))
    if not s or len(s) > 24 or not names:
        return None
    known = {_ROUTINE_MARKS.sub("", _STRIP.sub("", name)): name for name in names}
    if s in known:
        return known[s]
    prefix = _ROUTINE_RUN_PREFIX.match(s)
    if prefix is None or prefix.end() == len(s):
        return None
    rest = s[prefix.end():]
    for candidate in (rest, _ROUTINE_TAIL.sub("", rest), _ROUTINE_TAIL.sub("", rest).removesuffix("流程")):
        if candidate in known:
            return known[candidate]
    return None


# -- 引导复盘 (2026-09-29): the Runtime asks about the day's notable trades, the user answers ----------
REVIEW_START_WORDS = re.compile(
    r"^(?:今天|今日)?(?:请|帮我|帮忙|给我|我想|我要|想|要|来|咱们|我们|一起)?(?:语音|聊聊|一起)?复盘(?:一下|下)?"
    r"(?:今天|今日|这周|本周|这个星期|这星期|最近|这几天)?(?:的)?(?:交易|单子|操作)?(?:吧|呗|啊|呀)?$")
REVIEW_WEEK_WORDS = re.compile(r"这周|本周|星期|最近|这几天")
REVIEW_STOP_WORDS = re.compile(
    r"^(不聊了|不复盘了|先不复盘了|结束复盘|退出复盘|先到这里|先到这儿|就到这里|就到这儿|今天先这样|先这样|够了|停|停一下|先停)(吧|了|啊|呀)?$")
REVIEW_SKIP_WORDS = re.compile(
    r"^(跳过|跳过这笔|下一笔|下一个|这笔不聊了|这笔跳过|不记得了|忘了|没什么想法|没想法|没啥说的|随便做的|不知道|算了)(吧|了|啊|呀)?$")
REVIEW_ACCEPT_WORDS = re.compile(r"^(好|好的|好啊|好呀|可以|行|行啊|来吧|来|复盘吧|复盘一下吧|嗯|嗯嗯|开始吧|开始|聊聊|聊吧|可以啊)(吧|呀|啊)?$")
REVIEW_DECLINE_WORDS = re.compile(r"^(不用|不用了|不了|算了|下次|下次吧|改天|改天吧|先不了|现在不用|不要|不复盘)(吧|了|啊|呀)?$")


def review_start_scope(text: str) -> str | None:
    """"复盘一下 / 复盘今天的交易 / 帮我复盘这周": the voice review's scope ("day" / "week"), else None."""
    s = _STRIP.sub("", text or "")
    if not s or len(s) > 16 or REVIEW_START_WORDS.match(s) is None:
        return None
    return "week" if REVIEW_WEEK_WORDS.search(s) else "day"


def review_control(text: str) -> str | None:
    """Inside a running review: "stop" (不聊了 / 先到这里), "skip" (跳过 / 下一笔 / 不记得了), else None."""
    s = _STRIP.sub("", text or "")
    if not s or len(s) > 10:
        return None
    if REVIEW_STOP_WORDS.match(s):
        return "stop"
    if REVIEW_SKIP_WORDS.match(s):
        return "skip"
    return None


def review_offer_answer(text: str) -> str | None:
    """The answer to "要复盘一下吗？": "accept", "decline", or None (the sentence is about something else)."""
    s = _STRIP.sub("", text or "")
    if not s or len(s) > 8:
        return None
    if REVIEW_ACCEPT_WORDS.match(s):
        return "accept"
    if REVIEW_DECLINE_WORDS.match(s):
        return "decline"
    return None


# -- 09-23: replay control — a replay-bound call's playback words ------------------------------------
# They map onto the replay's existing control actions only (play / pause / step / speed with the
# 1/2/5/10 steps); a time jump ("快进到下午") maps to nothing and the caller refuses honestly.

_SPEED_STEPS = (1, 2, 5, 10)
_SPEED_WORDS = {"1": 1, "一": 1, "2": 2, "两": 2, "二": 2, "5": 5, "五": 5, "10": 10, "十": 10}
_TAIL = re.compile(r"[吧啊了呢嘛哦]+$")
_REPLAY_PAUSE = re.compile(r"^(?:先|把|先吧)?(?:回放|播放)?暂停(?:一下)?(?:回放|播放)?$")
_REPLAY_PLAY = re.compile(r"^(?:(?:继续|恢复|开始|接着|往下)(?:播放|回放|放)?|播放|回放继续|把回放继续)$")
_REPLAY_STEP = re.compile(r"^(?:单步|步进|(?:再|往前|前进)?走?一步|(?:再|往)?下一步)$")
# Absolute speed: a number in the "…倍" tail ("加速到5倍", "5倍速") or "速度调到2"; relative: 快一点/慢一点.
_REPLAY_SPEED_TAIL = re.compile(r"([0-9一二两五十]{1,3})倍(?:速)?$")
_REPLAY_SPEED_TO = re.compile(r"(?:速度|倍速|回放)?(?:调到|调成|调为)([0-9一二两五十]{1,3})$")
_REPLAY_FASTER = re.compile(r"^(?:再快一点|快一点|快点|快一些|加速|提速|快进一点|太慢了|有点慢)$")
_REPLAY_SLOWER = re.compile(r"^(?:再慢一点|慢一点|慢点|慢一些|减速|放慢|放慢一点|太快了|有点快)$")
_REPLAY_SPEED_HINT = re.compile(r"加速|减速|速度|倍速|调到|调成|放快|放慢|快进|提速")
_REPLAY_HINT_WORDS = re.compile(r"回放|播放|倍速|快进|快退|慢放|单步|暂停")
# Trading, research, preference or interface words: the sentence belongs elsewhere even in a replay call.
_REPLAY_EXCLUDE = re.compile(r"买|卖|开仓|平|撤|挂|手|新闻|为什么|原因|分析|走势|设置|打开|关闭|对话|帮助|持仓|账户|权益|资金")


def _speed_value(word: str) -> int | None:
    return _SPEED_WORDS.get(word)


def match_replay_control(text: str) -> tuple[str, int | None] | None:
    """("play"|"pause"|"step", None), ("speed", 1|2|5|10), or the relative ("speed_up"|"speed_down",
    None) for "快一点/慢一点" (the caller resolves the next step from the current speed). None when the
    sentence is not a replay control — including an unsupported speed ("3倍") or a time jump. Pure and
    synchronous; only consulted when the call is replay-bound."""
    s = _TAIL.sub("", _STRIP.sub("", text or ""))
    if not s or len(s) > 16 or _REPLAY_EXCLUDE.search(s):
        return None
    tail = _REPLAY_SPEED_TAIL.search(s)
    if tail and _REPLAY_SPEED_HINT.search(s):
        value = _speed_value(tail.group(1))
        return ("speed", value) if value in _SPEED_STEPS else None
    to = _REPLAY_SPEED_TO.search(s)
    if to:
        value = _speed_value(to.group(1))
        return ("speed", value) if value in _SPEED_STEPS else None
    if _REPLAY_PAUSE.match(s):
        return ("pause", None)
    if _REPLAY_PLAY.match(s):
        return ("play", None)
    if _REPLAY_STEP.match(s):
        return ("step", None)
    if _REPLAY_FASTER.match(s):
        return ("speed_up", None)
    if _REPLAY_SLOWER.match(s):
        return ("speed_down", None)
    return None


def looks_like_replay_control(text: str) -> bool:
    """A short sentence clearly about the replay's playback but not necessarily executable ("快进到下午
    3 点"): it still routes to `replay` so the refusal is specific instead of a delegate round. Pure and
    synchronous."""
    s = _STRIP.sub("", text or "")
    if not s or len(s) > 16 or _REPLAY_EXCLUDE.search(s):
        return False
    return bool(_REPLAY_HINT_WORDS.search(s))
