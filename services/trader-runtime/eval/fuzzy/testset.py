"""Fuzzy, colloquial test set for the agent's tool selection through the real Runtime.
Each item: text, category, expectation. Modes: typed (chat:send) and voice (chat:send origin=voice,
after the Runtime's own deterministic routing — route_utterance / match_intent — which is scored too).

expect keys (all optional):
  must:   set of tool names, at least one must be called
  allow:  set of tool names allowed besides `must` (any other call = extra tool)
  none:   True → no business tool may be called (reply only)
  card:   "trade" | "close_batch" → a proposal card of that type must be left waiting
  args:   dict checked against the card: symbol (substring, case-insens), side, volume, priceIntent, limitPrice
  ui:     dict of window-state keys the turn must set (typed: via the direct fast path or app_call; voice: via intent)
  pref:   dict of preference leaves (dotted) the turn must set
  refuse: True → no card, no preference/UI change, reply says it cannot / will not
  intent: kind name the voice fast path must hit (voice mode only)
  route:  route_utterance result expected in voice mode ("confirm"/"cancel"/"stop"/"hangup"), and the
          typed deterministic route (turn_router.classify_turn kind: ui_intent/order_intent/order/
          market_query/general) expected in typed mode — the routing table this set pins down. order_intent = the
          order-intent parser was sure and the Runtime's planner made the card without a model round (its card and
          reply are scored, its tool steps stand for propose_order); order = the trading AI's propose_order round
  pending: True → the sentence arrives with a proposal card waiting (never narrowed away from the model)
  seq:    list of prior utterances to send first in the same session (context for follow-ups)

The typed `route` labels are pinned by tests/test_turn_router.py against the fake catalog
(沪深300股指 / 铁矿石 / 沪金 / 螺纹钢 / productId IF); a production catalog can only route more
market sentences, never fewer.
"""
READ = {"get_runtime_data_status", "get_market_snapshot", "get_market_analysis", "get_positions", "get_account_summary",
        "get_account_activity", "search_instruments", "get_chart_bars", "get_futures_decision_context"}
# A routed market read may use any read/annotation tool except the web and the brief.
MARKET_ALLOW = {"get_market_analysis", "search_instruments", "get_chart_bars", "get_positions",
                "get_account_summary", "get_futures_decision_context", "annotate_chart"}

T = []
def add(cat, text, **expect):
    T.append({"id": f"{cat}-{len([t for t in T if t['category'] == cat]) + 1:02d}", "category": cat, "text": text, "expect": expect})

# 查询类
add("query", "现在螺纹多少钱", route="market_query", must={"get_market_snapshot"}, allow={"search_instruments"})
add("query", "今天赚了没", route="market_query", must={"get_account_summary", "get_positions"}, allow={"get_account_activity"})
add("query", "我手里有啥", route="market_query", must={"get_positions"}, allow=set())
add("query", "账户还剩多少钱能用", route="market_query", must={"get_account_summary"}, allow=set())
# 新闻允许走 get_research_brief(news)；web_search 只作时效核验，两者任答其一即可
add("query", "有啥新闻没", route="general", must={"web_search", "get_research_brief"}, allow=READ | {"web_search", "get_research_brief"})
add("query", "棉花最近咋样", route="general", must={"search_instruments"}, allow=READ | {"web_search"}, honest_missing=True)
add("query", "帮我看看沪金", route="market_query", must={"get_market_snapshot", "get_market_analysis", "get_futures_decision_context"}, allow=MARKET_ALLOW)
add("query", "这个怎么样", route="market_query", must={"get_market_snapshot", "get_market_analysis", "get_futures_decision_context"}, allow=MARKET_ALLOW)
add("query", "螺纹现在是涨还是跌", route="market_query", must={"get_market_snapshot", "get_market_analysis"}, allow={"search_instruments", "get_chart_bars"})
add("query", "我那两手金子怎么样了", route="general", must={"get_positions"}, allow={"get_market_snapshot", "get_account_summary"})
add("query", "亏了多少", route="market_query", must={"get_positions", "get_account_summary"}, allow={"get_account_activity"})
add("query", "铁矿今天量大不大", route="market_query", must={"get_market_snapshot", "get_market_analysis"}, allow={"search_instruments", "get_chart_bars"})
# 讲图/标注
add("chart", "给我讲讲这根线", route="general", must={"get_market_analysis", "get_chart_bars"}, allow={"get_market_analysis", "get_chart_bars"})
add("chart", "这波是不是要跌", route="general", must={"get_market_analysis", "get_futures_decision_context"}, allow={"get_chart_bars", "get_market_snapshot", "web_search"})
add("chart", "帮我在图上标一下关键位置", route="general", must={"annotate_chart"}, allow={"get_market_analysis", "get_chart_bars"})
add("chart", "哪里能进", route="general", must={"get_market_analysis", "get_futures_decision_context"}, allow={"get_chart_bars", "annotate_chart", "get_market_snapshot", "web_search"})
add("chart", "把刚才画的擦了", route="ui_intent", intent="chartAnnotations", ui={"chartAnnotations": "clear"})
add("chart", "看看MACD有没有背离", route="general", must={"get_market_analysis", "get_chart_bars"}, allow={"get_chart_bars", "get_market_analysis"})  # 读 K 线自己算背离与调分析工具都算对
add("chart", "KD现在金叉了吗", route="general", must={"get_market_analysis"}, allow={"get_chart_bars", "get_preferences"})
# 界面（模糊）：typed 直接走确定性快路径——无模型回合，效果就是窗口命令本身
add("ui", "太亮了", route="ui_intent", intent="theme", ui={"theme": "dark"})
add("ui", "换个颜色", route="ui_intent", intent="theme", ui_any={"theme"})
add("ui", "字太小", route="ui_intent", ui={"fontScale": "large"}, ui_any={"fontScale"})
add("ui", "把那个边上的东西收起来", route="ui_intent", ui_any={"panel", "positionsBar"}, start_ui={"panel": "order"})
add("ui", "我要看持仓那个页", route="ui_intent", intent="panel", ui_any={"panel", "positionsBar", "dialog"})
add("ui", "开一下下单的地方", route="ui_intent", intent="panel", ui={"panel": "order"})
add("ui", "回到K线", route="ui_intent", ui_any={"view", "chartFocus", "panel"})
add("ui", "看看日线", route="ui_intent", intent="duration", ui={"duration": 86400})
add("ui", "帮我开个新的聊天", route="ui_intent", intent="chatSession", ui_any={"chatSession"})
# 历史对话=AI面板会话列表（chat-history 折叠开关，意图表新增），不再让模型绕 app_call/highlight
add("ui", "上一次聊的在哪", route="ui_intent", intent="chatHistory", ui={"disclosure": "chat-history"})
add("ui", "把设置打开", route="ui_intent", intent="settings", ui={"settingsOpen": True})
add("ui", "切到五分钟的", route="ui_intent", intent="duration", ui={"duration": 300})
# 设置
add("pref", "说话快一点", route="general", must={"update_preferences"}, allow={"get_preferences"}, pref={"voice.speed": "fast"})
add("pref", "你回答短点", route="general", must={"update_preferences"}, allow={"get_preferences"}, pref={"chat.replyStyle": "concise"}, start_prefs={"chat": {"replyStyle": "detailed"}})
add("pref", "以后下单默认两手", route="general", must={"update_preferences"}, allow={"get_preferences"}, pref={"trade.defaultVolume": 2})
add("pref", "下单别挂单直接成交", route="general", must={"update_preferences"}, allow={"get_preferences"}, pref={"trade.defaultPriceIntent": "aggressive"}, start_prefs={"trade": {"defaultPriceIntent": "passive"}})
add("pref", "别再念新闻了", route="general", must={"update_preferences"}, allow={"get_preferences"}, pref={"voice.announceBriefs": False})
add("pref", "换个男声", route="general", must={"update_preferences"}, allow={"get_preferences", "app_call"}, pref_any={"voice.voice": "male"})
add("pref", "提醒我成交", route="general", must={"update_preferences"}, allow={"get_preferences"}, pref_any={"notify.desktop.fills": True, "voice.announceFills": True}, start_prefs={"notify": {"desktop": {"fills": False}}, "voice": {"announceFills": False}})
# 下单类（到卡为止，意图重构第 3 步）：解析器有把握的 typed 走 order_intent（规划器直接出卡，无模型轮）；其余由交易 AI 用 propose_order
add("order", "搞一手螺纹多的", route="order_intent", must={"propose_order"}, allow={"search_instruments"},
    card="trade", args={"symbol": "rb", "side": "buy_open", "volume": 1})
add("order", "空两手棉花", route="general", must={"search_instruments"}, allow={"propose_order"}, honest_missing=True, nocard=True)
add("order", "按现价来一手", route="order_intent", must={"propose_order"}, allow={"search_instruments"},
    card="trade", args={"symbol": "au", "volume": 1, "priceIntent": {"aggressive", "last"}}, clarify_ok=r"多|空|方向")
add("order", "820挂一手多", route="order_intent", must={"propose_order"}, allow={"search_instruments"},
    card="trade", args={"side": "buy_open", "volume": 1, "limitPrice": 820, "symbol": "rb"}, start_ui={"symbol": "SHFE.rb2610"})  # fixture prices sit near 822
# 开仓顺带保护 (2026-10-02)：止损/止盈价随卡走 protection（fixture 价 ~822，818/830 方向才成立）
add("order", "开多一手螺纹，止损 818 止盈 830", route="order", must={"propose_order"},
    allow={"search_instruments"}, card="trade", args={"symbol": "rb", "side": "buy_open", "volume": 1})
# 平仓句由 Runtime 附上持仓行：fake 无螺纹持仓 → 模型直接中文说明，不调工具也不出卡
add("order", "把螺纹那一手平了", route="order_intent", allow={"propose_order", "search_instruments"}, nocard=True, honest_missing=True)
add("order", "平一半", route="order_intent", must={"propose_order"}, allow={"search_instruments"}, card="trade", args={"symbol": "au", "side": "sell_close", "volume": 1}, clarify_ok="哪|品种|合约|沪金|沪银|指")  # 没说品种且有两个持仓：追问哪一个也算对
add("order", "全平", route="order_intent", must={"propose_order"}, allow=set(), card="close_batch")
add("order", "空单都平了", route="order_intent", must={"propose_order"}, allow=set(), card="close_batch", args={"symbol": "hc"})  # the fixture's only short is hc2610 (交割月样例持仓, 10-02)
# 目标持仓类：差额（平/开/反手两段、今昨仓）由 Runtime 的规划器拆；“保持 N 手多/空”也是目标持仓（第 4 步）；解析器不认的说法（持有）交给交易 AI
add("order", "把螺纹调到三手多单", route="order_intent", must={"propose_order"}, allow={"search_instruments"},
    card="trade", args={"symbol": "rb", "side": "buy_open", "volume": 3})  # fixture 无螺纹持仓：0 → +3 一段买开
add("order", "把沪金的目标持仓清零", route="order_intent", must={"propose_order"}, allow={"search_instruments"},
    card="close_batch", args={"symbol": "au"})  # 沪金多 2：清零 = 平掉全部
add("order", "沪金调整到空两手", route="order_intent", must={"propose_order"}, allow={"search_instruments"},
    card="close_batch", args={"symbol": "au"})  # +2 → -2 反手：第一段是平掉全部，第二段成交后接力出卡
add("order", "把沪金变成两手空单", route="order_intent", must={"propose_order"}, allow={"search_instruments"},
    card="close_batch", args={"symbol": "au"})  # 同上，换一种说法
add("order", "螺纹保持五手多", route="order_intent", must={"propose_order"}, allow={"search_instruments"},
    card="trade", args={"symbol": "rb", "side": "buy_open", "volume": 5})
add("order", "我要持有三手多单", route="general", must={"propose_order"}, allow={"search_instruments"})  # 没说品种：窗口合约，出卡方向随当前持仓
# 不带手数的整品种平仓 (2026-10-04)：走 close_all 路由，批量平仓卡只含该品种
add("order", "把金子平了", route="order_intent", must={"propose_order"}, allow=set(), card="close_batch", args={"symbol": "au"})
add("order", "撤了刚才那单", route="general", must={"get_account_activity", "app_call", "query_simulation_order"}, allow=READ | {"app_call", "query_simulation_order"}, nocard=True)
add("order", "改成三手", route="general", pending=True, seq=["搞一手螺纹多的"], must={"propose_order"}, allow={"get_market_snapshot", "get_futures_decision_context", "get_runtime_data_status", "search_instruments"},
    card="trade", args={"symbol": "rb", "side": "buy_open", "volume": 3})
add("order", "不对，是卖", route="general", pending=True, seq=["搞一手螺纹多的"], must={"propose_order"}, allow={"get_market_snapshot", "get_futures_decision_context", "get_runtime_data_status", "search_instruments"},
    card="trade", args={"symbol": "rb", "side": "sell_open", "volume": 1})
add("order", "算了不下了", route="general", pending=True, seq=["搞一手螺纹多的"], route_voice="cancel", none=True, nocard=True)
add("order", "确认", route="general", pending=True, seq=["搞一手螺纹多的"], route_voice="confirm", none=True)
# 追问与纠正
add("follow", "再说一遍", route="general", seq=["我手里有啥"], none=True, allow={"get_positions"})
add("follow", "换个说法", route="general", seq=["我手里有啥"], none=True, allow={"get_positions"})
add("follow", "刚才那个什么意思", route="general", seq=["看看MACD有没有背离"], none=True, allow={"get_market_analysis"})
add("follow", "然后呢", route="general", seq=["这波是不是要跌"], none=True, allow=READ)
add("follow", "继续", route="general", seq=["这波是不是要跌"], none=True, allow=READ, voice_route="chat")
# 多意图
add("multi", "切到铁矿然后看看持仓", route="general", must={"app_call", "get_positions"}, allow={"search_instruments"}, ui={"symbol": "DCE.i2609"}, both_required=True)
add("multi", "换深色再把设置打开", route="general", must={"app_call"}, allow=set(), ui={"theme": "dark", "settingsOpen": True})
add("multi", "先看行情再帮我标关键位", route="general", must={"annotate_chart"}, allow={"get_market_analysis", "get_chart_bars", "get_market_snapshot", "web_search"})
# 不该动的
add("guard", "我今天心情不好", route="general", none=True)
add("guard", "别下单啊", route="general", none=True, nocard=True)
add("guard", "你觉得能不能买", route="opinion", must={"get_market_analysis", "get_futures_decision_context", "get_market_snapshot"}, allow=READ | {"web_search"}, nocard=True)
add("guard", "帮我实盘买一手", route="general", refuse=True, nocard=True, allow=READ)
add("guard", "把我账号密码改了", route="general", refuse=True, none=True)
add("guard", "帮我把止损规则加上", route="general", nocard=True, none=True, refuse_soft=True, allow={"get_positions"})
# 引导
add("tour", "带我看看这个软件", route="general", must={"start_tour"}, allow={"app_call"})
add("tour", "教我怎么下单", route="general", must={"start_tour"}, allow={"app_call", "get_positions"})
add("tour", "下一步", route="general", voice_route="tour_next", none=True, allow={"app_call"}, tour_context=True)
add("tour", "结束", route="general", voice_route="tour_stop", none=True, allow={"app_call"}, tour_context=True)

# ---- 第二期新增（2026-09-20）：把每类路由的边界钉死 ----
# market_query：问法与品种的种种口语变体（品种须在目录里）
add("query", "现在沪金多少钱", route="market_query", must={"get_market_snapshot"}, allow=MARKET_ALLOW)
add("query", "沪金现在什么价", route="market_query", must={"get_market_snapshot"}, allow=MARKET_ALLOW)
add("query", "帮我看看螺纹", route="market_query", must={"get_market_snapshot", "get_market_analysis"}, allow=MARKET_ALLOW)
add("query", "看看铁矿石", route="market_query", must={"get_market_snapshot", "get_market_analysis"}, allow=MARKET_ALLOW)
add("query", "螺纹报价多少", route="market_query", must={"get_market_snapshot"}, allow=MARKET_ALLOW)
# 涨没涨/跌没跌是二选一：最新价快照或行情摘要都能回答，不再强求快照
add("query", "沪金涨了没", route="market_query", must={"get_market_snapshot", "get_market_analysis"}, allow=MARKET_ALLOW)
add("query", "螺纹跌了没", route="market_query", must={"get_market_snapshot", "get_market_analysis"}, allow=MARKET_ALLOW)
add("query", "沪金最近咋样", route="market_query", must={"get_market_snapshot", "get_market_analysis", "get_chart_bars", "get_futures_decision_context"}, allow=MARKET_ALLOW)
add("query", "铁矿石现在怎么样", route="market_query", must={"get_market_snapshot", "get_market_analysis", "get_chart_bars", "get_futures_decision_context"}, allow=MARKET_ALLOW)
add("query", "螺纹钢现在什么价", route="market_query", must={"get_market_snapshot"}, allow=MARKET_ALLOW)
add("query", "这个现在多少钱", route="market_query", must={"get_market_snapshot"}, allow=MARKET_ALLOW)
add("query", "看看沪金现在咋样", route="market_query", must={"get_market_snapshot", "get_market_analysis"}, allow=MARKET_ALLOW)
add("query", "帮我瞅瞅螺纹", route="market_query", must={"get_market_snapshot", "get_market_analysis"}, allow=MARKET_ALLOW)
# market_query 的反例：新闻/消息/为什么留给模型
add("query", "螺纹为什么跌了", route="general", must={"get_market_analysis"}, allow=READ | {"web_search", "get_research_brief"})
add("query", "有什么新闻", route="general", must={"web_search", "get_research_brief"}, allow=READ | {"web_search", "get_research_brief"})
add("query", "帮我看看沪金的新闻", route="general", must={"get_research_brief"}, allow=READ | {"web_search", "get_research_brief"})
add("query", "螺纹最近有什么消息", route="general", must={"get_research_brief"}, allow=READ | {"web_search", "get_research_brief"})
# ui_intent：语义歧义收面板 + 主题/字号/周期/面板的口语变体
add("ui", "旁边的关掉", route="ui_intent", ui_any={"panel", "positionsBar"}, start_ui={"panel": "order"})
add("ui", "把侧边的东西收起来", route="ui_intent", ui_any={"panel", "positionsBar"}, start_ui={"panel": "order"})
add("ui", "屏幕太刺眼了", route="ui_intent", ui={"theme": "dark"})
add("ui", "好暗", route="ui_intent", ui={"theme": "light"}, start_ui={"theme": "dark"})
add("ui", "太黑了", route="ui_intent", ui={"theme": "light"}, start_ui={"theme": "dark"})
add("ui", "字大点", route="ui_intent", ui_any={"fontScale"})
add("ui", "切到十五分钟", route="ui_intent", ui={"duration": 900})
add("ui", "打开对话", route="ui_intent", ui={"panel": "ai"})
add("ui", "收起策略面板", route="ui_intent", ui={"panel": None}, ui_any={"panel"}, start_ui={"panel": "strategy"})
add("ui", "打开复盘", route="ui_intent", ui={"view": "review"})
add("ui", "把对话关了", route="ui_intent", ui={"panel": None}, ui_any={"panel"}, start_ui={"panel": "ai"})
add("ui", "深色模式", route="ui_intent", ui={"theme": "dark"})
# ui_intent 的兜底形态：界面句式但意图表不认——一轮 app_call，不给业务工具
add("ui", "把图表那个东西调出来", route="ui_intent", intent="view", ui={"view": "trade"})
# order：开仓与指定手数平仓的口语变体
add("order", "帮我搞两手沪金空的", route="order_intent", must={"propose_order"}, allow={"search_instruments"},
    card="trade", args={"symbol": "au", "side": "sell_open", "volume": 2})
add("order", "来一手豆粕", route="general", must={"search_instruments", "propose_order"}, allow={"search_instruments", "propose_order"}, honest_missing=True, nocard=True, clarify_ok=r"买开|卖开|做多|做空|多|空|方向")  # 豆粕不在 fake 目录：搜索无果由 Runtime 诚实收尾；没说方向先问方向也算对
add("order", "螺纹上买两手", route="order_intent", must={"propose_order"}, allow={"search_instruments"},
    card="trade", args={"symbol": "rb", "side": "buy_open", "volume": 2})
add("order", "平掉沪金两手", route="order_intent", must={"propose_order"}, allow={"search_instruments"}, card="trade", args={"symbol": "au", "side": "sell_close", "volume": 2})
add("order", "螺纹那两手给我平了", route="order_intent", allow={"search_instruments", "propose_order"}, honest_missing=True, nocard=True)  # fake 无螺纹持仓：持仓行已附上，诚实拒绝，不出卡
add("order", "把螺纹平一半", route="order_intent", allow={"search_instruments", "propose_order"}, honest_missing=True, nocard=True)  # fake 无螺纹持仓：持仓行已附上，诚实拒绝，不出卡
# 沪金持仓只有今仓2手：平3手超仓 → 工具拒绝（close_volume_exceeds_position）被直接转述，不出卡
add("order", "沪金三手平仓", route="order_intent", must={"propose_order"}, allow={"search_instruments"}, nocard=True, honest_missing=True)
# close_all：整仓/整侧的口语变体
add("order", "清仓", route="order_intent", must={"propose_order"}, allow=set(), card="close_batch")
add("order", "全部平掉", route="order_intent", must={"propose_order"}, allow=set(), card="close_batch")
add("order", "持仓都平了", route="order_intent", must={"propose_order"}, allow=set(), card="close_batch")
# 不带手数的整品种平仓（2026-10-04）：“平掉X/把X平了/X全平”= 平掉该品种全部持仓，批量平仓卡只含该品种
add("order", "平掉沪金", route="order_intent", must={"propose_order"}, allow=set(), card="close_batch", args={"symbol": "au"})
add("order", "把沪金平了", route="order_intent", must={"propose_order"}, allow=set(), card="close_batch", args={"symbol": "au"})
add("order", "沪金全平", route="order_intent", must={"propose_order"}, allow=set(), card="close_batch", args={"symbol": "au"})
add("order", "帮我把银子平掉", route="general", must={"propose_order"}, allow=set(), card="close_batch", args={"symbol": "ag"})
add("order", "平掉螺纹", route="order_intent", allow={"propose_order"}, nocard=True, honest_missing=True)  # fake 无螺纹持仓：诚实说明，不出卡
# 缺方向：规划器追问（“需要确认的信息”卡，选项是补全方向后的整句），文字追问也算对
add("order", "来一手螺纹", route="order_intent", allow={"propose_order", "search_instruments"}, nocard=True, clarify_ok=r"多|空|方向")
# 判断卡（2026-10-04）：问看法走 opinion 路由（Runtime 先附当日调研判断卡，模型照常回答；不出方案卡）
add("query", "螺纹怎么看", route="opinion", allow=READ | {"web_search", "get_research_brief"}, nocard=True)
add("query", "沪金能不能做", route="opinion", allow=READ | {"web_search", "get_research_brief"}, nocard=True)
add("query", "铁矿看多还是看空", route="opinion", allow=READ | {"web_search", "get_research_brief"}, nocard=True)
# general 的守卫：偏好/叮嘱/否定/账户代号
add("pref", "默认开仓两手", route="general", must={"update_preferences"}, allow={"get_preferences"}, pref={"trade.defaultVolume": 2})
add("pref", "以后买默认两手", route="general", must={"update_preferences"}, allow={"get_preferences"}, pref={"trade.defaultVolume": 2})
add("guard", "别平仓", route="general", none=True, nocard=True)
# 允许先看一眼持仓再软拒绝（模型确实这么做了）；仍不建卡、不建风险规则
add("guard", "帮我自动止损", route="general", allow={"get_positions"}, nocard=True, refuse_soft=True)
add("guard", "我那两手金子亏了多少", route="general", must={"get_positions", "get_account_summary"}, allow={"get_market_snapshot", "get_account_activity"})
# IF（沪深300股指）在目录里（productId 也进路由目录）：市场查询直接报价，不再是"没找到"
add("guard", "IF现在多少钱", route="market_query", must={"get_market_snapshot", "get_market_analysis"}, allow=MARKET_ALLOW)


# ---- the skill registry corpus (2026-09-24) --------------------------------
# Derived from agent_skills/<name>/SKILL.md — the same single source of truth the product derives
# its routing from. Each skill's 典型说法 asserts that skill's own route and effects; each 反例
# names the phrase's real owner with "走 <skill-id>" and is scored against THAT skill's
# expectation (route label unpinned for counters: positives pin the routing table, counters pin
# tool ownership). Regenerating here keeps the eval corpus from drifting out of SKILL.md.
import re as _re  # noqa: E402

from trader_runtime.skill_registry import all_skills as _registry_skills  # noqa: E402

READ_PLUS = READ | {"get_chart_bars", "app_call", "web_search", "get_research_brief",
                    "query_simulation_order", "get_ai_execution_status", "highlight_ui"}

# Per skill: the typed expectation its own 典型说法 must meet.
SKILL_POSITIVE = {
    # 本地单用户模式没有成员/档位/技能开关可操作：答复不碰业务工具（管理面是多人版 owner 能力）
    "admin-console": {"route": "general", "none": True, "nocard": True},
    # 启停与限额走 /api/ai-execution（app_call）；读取执行状态不算越界
    "ai-execution": {"route": "general", "must": {"app_call"},
                     "allow": READ | {"get_ai_execution_status"}, "nocard": True},
    # 能力问题读目录（app_call 预检不计分）后作答
    "app-catalog": {"route": "general", "none": True},
    "batch-close": {"route": "order_intent", "must": {"propose_order"}, "allow": set(),
                    "card": "close_batch"},
    "chart-analysis": {"route": "general", "must": {"get_market_analysis", "get_chart_bars"},
                       "allow": READ | {"get_chart_bars", "annotate_chart"}},
    "chart-annotations": {"route": "general", "must": {"annotate_chart"},
                          "allow": {"get_market_analysis", "get_chart_bars", "get_positions",
                                    "get_market_snapshot"}},
    # 用法问题读操作手册（GET /api/app/help，app_call）或直接作答，不动业务工具
    "help": {"route": "general", "none": True},
    # 简讯的读与刷新都走 /api/app/briefs（app_call）
    "market-briefs": {"route": "general", "none": True},
    "market-lookup": {"must": {"get_market_snapshot", "get_market_analysis", "get_chart_bars",
                               "get_futures_decision_context"},
                      "allow": READ | {"get_chart_bars", "get_settlement_prices", "get_main_contract_history",
                                       "get_symbol_ranking"}},
    # 开回放走 POST /api/replays（app_call）；选回放窗口先读主力变迁/结算价是同域只读
    "market-replay": {"route": "general", "must": {"app_call"},
                      "allow": READ | {"get_main_contract_history", "get_settlement_prices", "get_symbol_ranking"},
                      "nocard": True},
    # 撤改挂查与止损单：fake 环境常无活动委托，查后诚实说明也算对（get_account_activity 读委托
    # 与成交列表，是"查挂单"的同域读法）
    "order-ops": {"route": "general", "must": {"query_simulation_order", "app_call", "cancel_simulation_order",
                                               "get_account_activity"},
                  "allow": READ | {"query_simulation_order", "cancel_simulation_order",
                                   "set_simulation_stop_loss", "get_ai_execution_status"},
                  "nocard": True, "honest_missing": True},
    "order-proposal": {"route": "order_intent", "must": {"propose_order"}, "allow": {"search_instruments"}},
    # 到价提醒：设/查/删都走 /api/market/alerts 的同名工具（must 按说法在 delta 里钉）
    "price-alerts": {"route": "general", "honest_missing": True,
                     "allow": READ | {"create_price_alert", "list_price_alerts", "cancel_price_alert",
                                      "search_instruments"}, "nocard": True},
    # 用户画线 (2026-10-02)：读/画/删都走 /api/market/drawings 的同名工具；fake 空库先查后如实说也算对
    "chart-drawings": {"route": "general", "honest_missing": True,
                       "allow": READ | {"list_chart_drawings", "create_chart_drawing", "delete_chart_drawing",
                                        "search_instruments"}, "nocard": True},
    "preferences": {"route": "general", "must": {"update_preferences"}, "allow": {"get_preferences"}},
    # control_replay 工具与 HTTP 控制口同一接口；无回放时如实报 replay_not_found
    "replay-control": {"route": "general", "must": {"control_replay", "app_call"},
                       "allow": READ | {"get_main_contract_history", "get_settlement_prices", "get_symbol_ranking"},
                       "nocard": True},
    # 我的流程 (2026-10-06)：定义/修改出流程卡（propose_routine，只出卡不保存），查列表读 list_routines
    "routines": {"route": "general", "must": {"propose_routine"},
                 "allow": READ | {"propose_routine", "list_routines", "run_routine"}, "nocard": True},
    "review-summary": {"route": "general", "must": {"get_positions", "get_account_summary", "get_account_activity"},
                       "allow": READ},
    # fake 库里没有已存策略：回测的正解是读草稿列表后如实说明（工具归属仍由 allow 钉住）
    "strategy-backtests": {"route": "general", "honest_missing": True,
                           "allow": READ | {"list_strategy_drafts", "get_strategy_draft",
                                            "run_reproducible_backtest", "run_tqsdk_historical_backtest"},
                           "nocard": True},
    "strategy-management": {"route": "general", "must": {"create_strategy_draft", "app_call"},
                            "allow": READ | {"list_strategy_drafts", "get_strategy_draft", "evaluate_strategy_draft",
                                             "start_strategy_monitoring", "pause_strategy_monitoring",
                                             "get_strategy_monitor_status"}, "nocard": True},
    # explain_strategy_signal 只读解读；eval 库没有监控中的策略时如实收尾
    "strategy-signals": {"route": "general", "must": {"explain_strategy_signal"},
                         "allow": READ | {"annotate_chart"}, "nocard": True},
    "target-position": {"route": "order_intent", "must": {"propose_order"},
                        "allow": {"search_instruments"}},
    "ui-control": {"route": "ui_intent"},
    # 通话入口是界面开关（AI 球/通话栏），打字说"开始通话"答复指引，不自行发起
    "voice-call": {"route": "general", "none": True, "nocard": True},
    "web-research": {"route": "general", "must": {"web_search", "get_research_brief"},
                     "allow": READ | {"web_search", "get_research_brief"}},
}

# Per (skill, phrase) overrides on top of the skill's base expectation; None drops the base key.
SKILL_PHRASE_DELTA = {
    ("admin-console", "看看成员用量"): {"route": "ui_intent", "none": None},
    ("app-catalog", "帮我看看有哪些界面命令"): {"route": "ui_intent", "none": None},
    ("app-catalog", "用正式接口把配置改一下"): {"must": {"app_call"}, "allow": READ, "none": None,
                                                "clarify_ok": r"哪一项|哪类|说清|改哪"},
    ("batch-close", "把所有空单都平掉"): {"card": None, "nocard": True, "honest_missing": True},  # fake 目录无空单
    ("help", "帮助"): {"route": "ui_intent", "none": None},  # 说明卡（dialog: help）
    ("help", "这个怎么用"): {"none": None, "allow": READ | {"start_tour"}},  # 教用法：说明卡或引导教程都对
    ("market-lookup", "螺纹钢现在什么价"): {"route": "market_query", "must": {"get_market_snapshot"},
                                          "allow": {"search_instruments"}},
    ("market-lookup", "铁矿今天成交量怎么样"): {"route": "market_query"},
    ("market-lookup", "主力合约是哪一个，帮我搜一下"): {"must": {"search_instruments", "get_main_contract_history"}},
    # 到价提醒的三个动作各钉自己的工具；"删掉"在 fake 空库上先查后如实说也算对
    ("price-alerts", "螺纹涨到 3150 提醒我"): {"must": {"create_price_alert"}, "honest_missing": None},
    ("price-alerts", "跌破 3100 告诉我"): {"must": {"create_price_alert"}, "honest_missing": None},
    ("price-alerts", "我有哪些到价提醒"): {"must": {"list_price_alerts"}},
    ("price-alerts", "删掉螺纹那条涨到 3150 的提醒"): {"must": {"list_price_alerts", "cancel_price_alert"},
                                                   "honest_missing": None},
    # 用户画线：查/画各钉各的工具；删先查到刚画的那条再删（前面"帮我在 3150 画一条线"已画）
    ("chart-drawings", "我画的线被突破了吗"): {"must": {"list_chart_drawings"}},
    ("chart-drawings", "我在螺纹上标过哪些价位"): {"must": {"list_chart_drawings"}},
    ("chart-drawings", "帮我在 3150 画一条线"): {"must": {"create_chart_drawing"}},
    ("chart-drawings", "把 3100 到 3130 标成区间"): {"must": {"create_chart_drawing"}},
    ("chart-drawings", "把我画的 3150 那条线删了"): {"must": {"list_chart_drawings", "delete_chart_drawing"},
                                                    "honest_missing": None},
    ("routines", "我有哪些流程"): {"must": {"list_routines"}},
    # 手机看盘 (2026-10-09): a phone window takes it at once (mobileMarket subPane); a computer window refuses the
    # fast path and the turn falls back to the model, which still changes chart.subPaneList.
    ("preferences", "副图换成 RSI"): {"route": "ui_intent", "intent": "mobileMarket"},
    # fake 库里没有「老规矩」：先查后如实说没有这个流程也算对
    ("routines", "老规矩去掉回测那步"): {"must": {"list_routines", "propose_routine"}, "honest_missing": True},
    ("market-replay", "开个回放练练"): {"route": "ui_intent", "must": None},  # 回放设置对话框
    ("market-replay", "重播那天铁矿的走势"): {"clarify_ok": r"哪一天|哪天|哪一日|日期|交易日"},  # 开回放要先有日期
    ("order-ops", "挂单成交了吗"): {"must": {"query_simulation_order", "app_call", "get_account_activity"}},
    ("order-ops", "给这个持仓挂个止损"): {"must": None, "nocard": True, "honest_missing": None,  # 挂上（走 allow
                                         # 内工具）或读完持仓后问触发价都对——追问常先读上下文，不能要求
                                         # "零工具才许问"，也不算"该诚实而没诚实"
                                         "allow": READ | {"set_simulation_stop_loss", "cancel_simulation_stop_loss",
                                                          "query_simulation_order"},
                                         "clarify_ok": r"触发价|报个价|价位|多少"},
    # 下单顺带保护 (2026-10-02)：价位已给的补挂句——fake 环境没有螺纹持仓，读后如实说明或直接挂都对
    ("order-ops", "给我的螺纹多单设个止损 3100"): {"must": None, "nocard": True, "honest_missing": None,
                                                   "allow": READ | {"set_simulation_stop_loss",
                                                                    "cancel_simulation_stop_loss"}},
    ("order-ops", "停止 AI 执行"): {"honest_missing": None},  # 停止永远可用，常是成功动作
    # 开启必须有明确授权范围（品种/手数/笔数/时长）：裸说"开始"时先问范围是正解，不是缺工具
    ("ai-execution", "开始 AI 自主执行"): {"must": None, "nocard": True,
                                       "clarify_ok": r"品种|合约|手数|几笔|范围|有效|多久|盯"},
    ("order-proposal", "买多一手螺纹"): {"card": "trade", "args": {"symbol": "rb", "side": "buy_open", "volume": 1}},
    # 随单止损止盈：解析器认作下单（gap protection），交易 AI 的下单回合 propose_order（protection 随卡）
    ("order-proposal", "开多一手螺纹，止损 3100 止盈 3200"): {"route": "order"},
    ("order-proposal", "开空两手沪金，挂对手价"): {"card": "trade",
                                               "args": {"symbol": "au", "side": "sell_open", "volume": 2,
                                                        "priceIntent": "aggressive"}},
    # 持仓在（沪金多 2 手）：读完持仓出平仓卡；若校验拒绝则如实转述——两种都对
    ("order-proposal", "帮我把这个多单平了"): {"route": "order",
                                           "allow": {"search_instruments", "propose_order",
                                                     "get_positions"},
                                           "card": "trade", "honest_missing": True,
                                           "args": {"symbol": "au", "side": "sell_close", "volume": 2}},
    ("order-proposal", "再加开一手"): {"clarify_ok": r"方向|买多|卖空|多还是空|买还是卖"},  # 没说方向先问
    # 问“钱”不是下单：estimate_order 只读回答（保证金约/手续费约/可开手数），不出卡
    ("order-proposal", "开一手螺纹要多少保证金"): {"route": "general", "must": {"estimate_order"},
                                               "allow": READ | {"estimate_order", "search_instruments"},
                                               "nocard": True},
    ("order-proposal", "我最多还能开几手"): {"route": "general", "must": {"estimate_order"},
                                         "allow": READ | {"estimate_order", "search_instruments"},
                                         "nocard": True},
    ("order-proposal", "平掉这手能腾出多少钱"): {"route": "general", "must": {"estimate_order"},
                                             "allow": READ | {"estimate_order", "search_instruments"},
                                             "nocard": True},
    ("preferences", "默认手数改成两手"): {"pref": {"trade.defaultVolume": 2}},
    ("preferences", "说慢一点"): {"pref": {"voice.speed": "slow"}},
    ("preferences", "关掉成交播报"): {"pref_any": {"voice.announceFills": False, "notify.desktop.fills": False}},
    ("replay-control", "退出回放"): {"route": "ui_intent", "must": None},
    ("review-summary", "我手里有什么持仓"): {"route": "market_query", "must": {"get_positions"}, "allow": set()},
    ("review-summary", "今天赚了还是亏了"): {"must": {"get_account_summary", "get_positions"}},
    # #932 复核: a start command is not interface work — the general turn has start_strategy_monitoring (the UI round
    # could only open the panel, and its escalation refuses the start).
    ("strategy-management", "开始监控这个策略"): {"route": "general", "must": None},
    ("strategy-management", "帮我写个均线策略"): {"must": {"create_strategy_draft"}},
    ("strategy-management", "帮我写个策略"): {"clarify_ok": r"什么样|哪种|什么策略|说一下|描述"},
    # 库里可能已有本 pass 早前用例存的草案：列出并评估、或（空库时）读后如实说明，都算对；
    # SKILL.md 第 3 步的验证（prepare/run_strategy_validation）也是"评估"的正解
    ("strategy-management", "评估一下我这个策略"): {"must": {"evaluate_strategy_draft", "list_strategy_drafts",
                                                            "app_call"},
                                                  "allow": READ | {"list_strategy_drafts", "get_strategy_draft",
                                                                   "evaluate_strategy_draft",
                                                                   "prepare_strategy_validation",
                                                                   "run_strategy_validation",
                                                                   "start_strategy_monitoring",
                                                                   "get_strategy_monitor_status"}},
    # 先确认要暂停哪些（库里有多张）也是正解；无监控中的策略时如实说明
    ("strategy-management", "暂停策略监控"): {"must": None, "nocard": True, "honest_missing": True,
                                              "clarify_ok": r"确认|哪些|哪几",
                                              "allow": READ | {"list_strategy_drafts", "get_strategy_draft",
                                                               "get_strategy_monitor_status",
                                                               "pause_strategy_monitoring"}},
    ("strategy-management", "把我的策略跑起来"): {"must": None, "honest_missing": True,  # fake 库无策略
                                                  "allow": READ | {"list_strategy_drafts", "get_strategy_draft",
                                                                   "create_strategy_draft", "app_call"}},
    ("strategy-signals", "按我的策略现在该怎么做"): {"route": "strategy_signal"},
    ("strategy-signals", "现在能开多吗"): {"route": "strategy_signal"},
    ("strategy-backtests", "回测一下这个策略"): {"honest_missing": None},  # 库里有多张草案：列库反问哪张也算对
    # 真把回测跑完并如实报告结果：不应再要求"没有…"的缺失措辞
    ("strategy-backtests", "回测一下刚才那个草稿"): {"honest_missing": None},
    # 策略编辑卡 (2026-10-08): 打开卡是确定性界面意图（无模型轮）
    ("strategy-management", "帮我设计一个策略"): {"route": "ui_intent", "intent": "strategyEditor", "must": None,
                                                 "ui": {"strategyEditor": True}, "nocard": True},
    ("strategy-management", "根据我的交易生成策略"): {"route": "ui_intent", "intent": "strategyEditor", "must": None,
                                                    "ui": {"strategyEditor": True}, "nocard": True},
    ("target-position", "把螺纹调到三手多单"): {"card": "trade", "args": {"symbol": "rb", "side": "buy_open"}},
    # 已是目标持仓：直接说明"不用动"（说法里没有"没有"字样），或调目标持仓工具被其拒绝，都不出卡
    ("target-position", "沪金清到只剩两手"): {"route": "general", "must": None, "nocard": True,
                                              "allow": {"search_instruments", "get_positions",
                                                        "propose_order"}},
    ("target-position", "这个品种我想持有一手空单"): {"route": "general", "card": "close_batch"},  # 反手：第一段先平掉现有持仓
    ("target-position", "螺纹留两手其他全平"): {"must": {"propose_order"},
                                              "allow": {"search_instruments"}, "card": "close_batch",
                                              "honest_missing": True},  # 无螺纹持仓时=其余全平
    ("ui-control", "打开设置"): {"intent": "settings", "ui": {"settingsOpen": True}},
    ("ui-control", "切到复盘"): {"intent": "view", "ui": {"view": "review"}},
    ("ui-control", "字大一点"): {"ui_any": {"fontScale"}},
    ("ui-control", "打开策略面板"): {"ui": {"panel": "strategy"}},
    ("ui-control", "切到1小时K线"): {"intent": "duration", "ui": {"duration": 3600}},
    ("ui-control", "切到日线"): {"intent": "duration", "ui": {"duration": 86400}},
    ("ui-control", "打开图表，切到 15 分钟线"): {"ui": {"duration": 900}},  # 带空格的复合句语音意图表不认，走模型兜底
}

# 反例 bullets whose routing note does not name a skill id ("走 <id>") map by text.
COUNTER_FALLBACK = {
    "买多一手螺纹": ("order-proposal", {"card": "trade", "args": {"symbol": "rb", "side": "buy_open", "volume": 1}}),
    "别下单啊，我就是问问": (None, {"none": True, "nocard": True}),      # 明确禁止：只回答不下单
    "帮我设个每日亏损上限": (None, {"none": True, "nocard": True}),        # 产品规则禁止创建风险规则：口头说明
    "我要注册个账号": (None, {"none": True, "nocard": True}),             # 认证入口不向 agent 开放
    "关闭语音": (None, {"voice_route": "hangup", "none": True, "nocard": True}),  # 由通话路由直接挂断
}


def _merge(base: dict, delta: dict | None) -> dict:
    merged = dict(base)
    for key, value in (delta or {}).items():
        if value is None:
            merged.pop(key, None)
        else:
            merged[key] = value
    return merged


def _counter_cases(bullet: str) -> list[tuple[str, dict]]:
    """A counter bullet is "<phrase>（<note naming the real owner>）"; the note's
    "走 <skill-id>" decides whose tools the phrase must hit (route unpinned —
    positives own the routing table, counters own ownership). A "/" splits
    alternative phrasings into separate utterances."""
    clean = _re.split(r"[（(]", bullet, 1)[0].strip()
    texts = [part.strip() for part in clean.split("/") if part.strip()] or [clean]
    stripped = clean.replace(" ", "")
    if stripped in COUNTER_FALLBACK:
        target, delta = COUNTER_FALLBACK[stripped]
        if target is None:
            return [(text, dict(delta)) for text in texts]
    else:
        match = _re.search(r"走 ?([a-z][a-z0-9-]*)", bullet)
        if match is None:
            return []
        target, delta = match.group(1), {}
    base = dict(SKILL_POSITIVE.get(target) or {})
    expect = _merge(base, SKILL_PHRASE_DELTA.get((target, clean)))
    expect = _merge(expect, delta)
    expect.pop("route", None)
    if "none" in expect:
        expect.setdefault("allow", set())
    else:
        expect["allow"] = set(expect.get("allow") or set()) | READ_PLUS
    return [(text, expect) for text in texts]


for _skill in _registry_skills():
    _base = SKILL_POSITIVE.get(_skill.skill_id)
    if _base is None:
        raise SystemExit(f"fuzzy testset: no expectation for skill {_skill.skill_id}")
    for _phrase in _skill.phrases:
        add(f"skill-{_skill.skill_id}", _phrase, **_merge(dict(_base), SKILL_PHRASE_DELTA.get((_skill.skill_id, _phrase))))
    for _counter in _skill.counter_examples:
        for _text, _expect in _counter_cases(_counter):
            add(f"skillc-{_skill.skill_id}", _text, **_expect)
