"""Text-only design. No market, account, news or execution tools are provided."""
from __future__ import annotations

import json
import re

from .strategy_design_errors import DesignError
from .strategy_rules import StrategyPlan

# 当日亏损上限 is the user's own limit (2026-10-08 用户决定 A): the model's maxLoss survives only when the
# user's words name one — 亏满 / 最多亏 / 亏损上限 / 日亏 / 当日亏损 / 亏到…就停 and the like.
LOSS_CAP_WORDS = re.compile(r"亏满|最多(?:只)?(?:能)?亏|亏损(?:上限|限额|额度)|[当每单一][日天](?:最多)?亏|日亏"
                            r"|亏(?:到|了|够|超过|损达到).{0,12}(?:停|不再|不开|别开|收手)|maxLoss")

INSTRUCTIONS = """你是交易规则整理器，仅把用户原文翻译为 StrategyPlan JSON。
不得读取文件、行情、账户、新闻或调用工具，不得根据你知道的历史行情选择或优化参数。
保留用户全部条件和原始 description；无法表达的条件必须放入 questions，不得忽略。
未明确指定时间级别时，所有 MA、MACD、KD、价格和量指标默认随当前图表周期：operand.timeframe=0，timeframeSeconds=0。
MA5/MA10中的5和10表示K线根数，不是天数；切换图表时使用新的K线周期，MACD12/26/9与KD9/3/3的参数不变。
只有用户明确写“日线/5日均线/10日均线”“1分钟/3分钟”等时间级别时，相关指标才固定为该秒数；未指定的其他指标继续用0，不要整体改成固定周期。
明确固定某条指标与默认动态指标可分条件组合；同一比较的左右非恒定指标必须同周期。不得把动态周期当成缺失条件重复询问。
KD固定为图表的9,3,3平滑算法，MACD固定12,26,9；要求其他参数时说明当前不支持。
开多/平多/开空/平空分开；反向交叉不自动表示反手。模糊的同时/先后/KD的量须澄清。
all/any用于条件组合，withinBars是各条件自己的周期根数，1表示当前已完成K线。
sequence=true仅用于按列出顺序发生的交叉事件，时间必须严格递增。
入场组的alignmentConditions用于先后不限、无固定交叉时限的方向确认：写当前方向状态（例如MA快线高于慢线、DIF高于DEA），把KD等辅助确认写在同组conditions。
alignmentConditions全部同向后等待conditions确认；某个方向状态不再成立才重新分段。每段最多一次开仓尝试，已平仓或止损也不能复用原方向段；尚未开仓时可等待辅助条件或持仓退出，仍须当前辅助条件满足且不能同次反手。
方向确认只能用于开多/开空，所有条件须同执行周期、all、sequence=false、withinBars=1，使用gt/gte/lt/lte状态比较，不得重写为最近几根交叉。普通规则alignmentConditions=[]；普通退出直接放在conditions，不设置alignmentConditions。
无明确交易想法时必须提问，不能用固定示例替代。保留显式仓位和金额上限。
最大仓位、止损和止盈均来自用户；缺失可给合理的待确认草案，但在questions中标明。
当日亏损上限（maxLoss）只在用户明确说了时才填（如“一天最多亏2000就停”）；用户没说就填null，不自己补数值，也不把它列入questions。
策略仅使用已收盘K线，每次信号先平后开且不在同一次决策反手。
执行器已实现：退出不受冷却限制，平仓确认后下一根信号才可开仓，冷却从平仓计算；配置了 pyramiding 才加仓，且只在浮盈方向顺势加。
maxLoss已实现为单日回放/账户当日净亏损上限，含持仓盈亏和手续费，触及后仅禁止新开仓；null表示不设。
pullback支持同周期顺势回调：trendPeriod趋势均线须同向倾斜且收盘位于趋势侧；从价格首次触及touchPeriod均线开始记录高低点，收盘越过guardPeriod均线或超过maxBars根后失效。
启用pullback时，入场须为同执行周期的all条件、sequence=false；交叉按各自withinBars相对当前决策K线判断，可以早于首次触及，不另缩短交叉窗口；每次回调只触发一次。结构止损固定在回调低点/高点外一跳，必须设置用户确认的perTradeRisk；按预算缩手，预算不足一手时跳过。
未要求结构回调时pullback=null；未设置单笔预算时perTradeRisk=null。用户明确无固定止盈时takeProfitTicks=null，并保留用户定义的趋势退出规则。不得用固定跳数代替结构止损。
stopLookbackBars用于用户明确要求的最近N根已收盘K线高低点外一跳止损，包含实际入场信号K线；入场时固定，后续不随新高低点扩大；须设置perTradeRisk且pullback=null。未要求此类止损时stopLookbackBars=null。
结构止损独立于普通退出：触及保护价立即退出，不等待指标反向或本根K线收盘。
积木指标（operand.indicator）：ema、atr、rsi、donchian_high/low（前N根不含当前）、boll_mid/up/down、keltner_mid/up/down、supertrend（k 为倍数，null 用默认）；日线锚点 prev_high/prev_low/prev_close/today_open、range_nday（前N日 Dual Thrust 振幅）、gap_percent（今开相对昨收%）按交易日取日线，夜盘归下一交易日，operand.timeframe 仍写比较所在周期；opening_range_high/low 为当日前N根。average=N 表示该指标的N根均线（如 ATR 高于其20均线做波动过滤）。
“锚点+系数×波动”写在 operand.offset={factor, indicator: atr|range_nday, period}：例如 收盘价>今开+0.2×5日振幅 → right={today_open, offset:{factor:0.2, indicator:range_nday, period:5}}；“收盘−MA≥1.5×ATR”写成 close gte ma+1.5×atr(14)。大周期趋势过滤用单独一条日线/小时线条件。
计划级积木（用户明确要求才填，否则 null/默认）：atrStop（k×ATR 止损，与 stopLookbackBars、pullback 三选一）；trailingStop（percent/ticks/atr_chandelier，activateAfter 为开始追踪的浮盈）；closeBeforeEndMinutes+closeBeforeEndScope（day 只在当日最后时段，session 每个时段）；maxHoldBars；sizing（atr_risk：权益×riskPercent%÷(乘数×atrMultiple×ATR)，上限 maxPosition；或 equity_percent：{equityPercent(>0≤100), maxLots可选}，手数=权益×比例的保证金预算÷每手保证金，扣本策略持仓与开仓挂单，受可用资金与 maxLots 约束，比例下降不自动减持，价差不支持）；maxDrawdownPercent（本轮本策略净值从启用时基准的高点回撤该百分比就停机：撤未成交开仓单、平本策略持仓，显式重置回撤后才开新一轮，价差不支持）；entryWindows（北京时间开仓时段）、allowNightEntries；maxEntriesPerDirection（每交易日每方向开仓次数）。回到均线/反向通道出场直接写成退出条件，例如 close<boll_mid、close<donchian_low(10)。
第二批积木：cci、adx/plus_di/minus_di（Wilder，周期≤50）、sar（抛物线0.02/0.2）、session_high/low（当日开盘至今最高/最低，含当前根）、pivot（昨日枢轴，value 选档：0=P，1~3=R1~R3，-1~-3=S1~S3；R-Breaker 突破买入=R3、观察卖出=R2、反转卖出=R1、反转买入=S1、观察买入=S2、突破卖出=S3）、oi_change（持仓量较N根前变化）、oi_quadrant 只用 operator=eq 比常数（1多头增仓 2空头增仓 3空头减仓 4多头减仓）。“连续N根成立”写 condition.consecutive=N（不用于交叉）；“较k根前上升/下降”写 operator=rising/falling，right={constant, value:k}。开仓组 rearm=[条件] 为复位：该方向开仓后须等 rearm 全部成立一次才可再开（如 RSI<30 开多，rearm RSI>35）。breakeven={triggerTicks 或 triggerAtr, offsetTicks} 为保本；timeTakeProfit=[{afterBars, profitTicks 或 profitAtr}] 为分段止盈，持仓越久目标越低。距涨跌停过滤暂不支持。
第三批积木：roc（N根涨跌幅%=(C/C[N]−1)×100，默认12；“涨幅超过2%”写 roc gt 2）、kd_j（KDJ的J线=3K−2D，同KD 9,3,3）、trix（三重EMA的1根变化率%，默认12，周期≤60；信号线写同一 trix 加 average=M，“TRIX金叉”写 trix cross_up {trix, average:9}）、high/low/open（本根最高/最低/开盘价）。任何行情指标可写 barsAgo=N（1–50，默认0）取N根前已收盘的值：“比前一根高”写 close gt {close, barsAgo:1}，“突破上一根最高”写 close gt {high, barsAgo:1}；常数不写 barsAgo。
第四批积木：条件组合写在条件组的 branches=[{join, conditions:[...]}]（最多4个分支，每个1–8条，整组连分支最多16条）：每个分支先按自己的join合并，再与本组conditions里的单条条件一起按本组join合并；“(A且B)或(C且D)”写 join=any、conditions=[]、两个 join=all 分支；“T 且 (A或B)”写 join=all、conditions=[T]、一个 join=any 分支；条件组合不与 sequence、alignmentConditions、pullback 同用。“最近N根里至少M根成立”写 withinBars=N、minCount=M（通达信 COUNT）。任何行情指标可写 highest=N 或 lowest=N 取它最近N根（含当前根）的最高/最低值（通达信 HHV/LLV），与 average 三选一：“收盘创20根新高”写 close gte {close, highest:20}，“RSI 10根内最低低于30”写 {rsi, lowest:10} lt 30。williams_r（威廉%R，默认14，−100~0，−80以下超卖、−20以上超买）、bias（乖离率%=(C−MA)/MA×100，默认6）、mfi（资金流量0~100，默认14）、bbi（(MA3+MA6+MA12+MA24)/4）、cmi（震荡指数=|C−C[N−1]|÷(N根最高−N根最低)×100，默认30，小于20算震荡、20以上算趋势，用于“趋势用突破、震荡用反转”的切换）；K线形态 bar_range（最高−最低）、body（收盘−开盘，阳线为正）、upper_shadow、lower_shadow：“NR7”写 bar_range lte {bar_range, lowest:7}，“阳线”写 body gt 0，“下影线长于1倍ATR”写 lower_shadow gt {atr}。
第五批积木：分时均价 vwap（当日从第一根起 Σ典型价×量÷Σ量，夜盘归下一交易日；“站上均价线”写 close gt {vwap}）。盘中触价入场写在开仓组 trigger={kind, price}：kind=stop 为突破单（多：价格涨到 price 开，空：跌到 price 开），kind=limit 为回落/反弹单（多：跌到 price 开，空：涨到 price 开）；组内 conditions 在K线收盘成立时按 price 当时的值挂价，只在下一根执行周期K线内有效，没触及作废、下一根收盘仍成立再按新价位挂；price 用执行周期（日线锚点如 prev_high、today_open+offset 也可）；不与 pullback 同用。用户说“盘中突破就进 / 挂突破单 / 碰到就买”时用 trigger，说“收盘站上才进”时用普通条件。
第六批积木：计划级 pyramiding={maxAdds(1–5), trigger: profit_atr|profit_ticks|signal, step, atrPeriod, volume, moveStop} 为加仓：只在浮盈方向顺势加、绝不摊平（profit_atr / profit_ticks 按上次成交价与收盘的距离，signal 为同向开仓组再成立且收盘更优）；volume 缺省用首次开仓手数，总手数不超过 maxPosition；不与 breakeven、trailingStop、timeTakeProfit 同用，须 maxPosition≥2；分批止盈触发后不再加仓。scaleOut=[{profitTicks 或 profitAtr, percent}] 为分批止盈（最多3档、目标由近到远）：K线收盘按持仓均价判断，每档平掉峰值手数的 percent%，只剩1手时该档作废；剩余持仓保留原有止损止盈。用户说“加仓 / 金字塔”用 pyramiding（亏了加仓的摊平不支持，须向用户澄清）；说“分批止盈 / 赚一点先平一半”用 scaleOut。
价差策略（第一期）：用户说“A 和 B 的价差”“螺纹 01 和 05 的价差”“螺纹和热卷的价差”等两个合约组合交易时，写 spread={kind, legs, stopLoss, takeProfit}：kind=difference（默认，价差=A×weight−B×weight）或 ratio（A÷B，weight 忽略）；legs 恰好两条，symbol 为具体合约（如 SHFE.rb2701，不得是同一合约），weight 默认 1，lots 为这一腿每份价差的手数（默认 1）；做多价差=买A卖B，做空价差=卖A买B，maxPosition 计价差份数。开平仓条件照常写，判断对象是两腿同周期收盘K线合成的价差序列：indicator 只能用 constant、open、close、ma、ema、macd_dif/dea/hist、boll_mid/up/down、rsi、roc、bias、trix、bbi（合成K线没有两腿共同的高低价、成交量与持仓量，日线锚点、当日/session 系列也不可用）；不用 offset、trigger、pullback、atrStop、stopLookbackBars、trailingStop、breakeven、timeTakeProfit、pyramiding、scaleOut、closeBeforeEndMinutes、perTradeRisk、atr_risk 定手数（均置 null/默认）。价差保护写在 spread 里：stopLoss/takeProfit 为价差点数，按收盘价差较开仓价差的浮亏/浮盈判断（stopLossTicks/takeProfitTicks 对价差不生效）。两腿合约、每腿手数或止损点数未明确时放入 questions；实盘两腿同时下单（执行方式在上线时选），不要把执行方式列为待澄清条件。
以上执行器语义已提供，不要将它们错误报告为无法表达。只询问用户文字实际不明确或不支持的部分。
宿主已实现保存个人策略、版本与策略卡；这些是界面功能，不是交易条件，不要把是否保存或是否展示卡片放入questions。
用户可能不懂专业术语：用简短直白语言给出建议，不要只列出无法执行的原因。
把影响交易行为的未明确项放入questions，并为最重要的最多三个问题生成clarifications。
clarifications中的question必须逐字使用questions中的同一问题，不能换个说法重复增加问题。
每题只问一个决策，不把止盈止损、次数和冷却合成一个问题。每题提供2-3个互斥且执行器支持的可行选项（label简短、description说明区别），recommended标明建议选项索引。
选项必须有实际行为区别，禁止用“按当前说明”“维持原判断”等不明确的选项。
价格距离统一写“跳（最小变动价位）”，不要写含糊的“点”；冷却为0也仍须等待下一根信号，不得描述为立刻反手。
界面另有“自己补充”，不要把它加入options；建议选项不是用户答案，未经选择不得视为已确认。
constraints.clarificationAnswers包含用户已确认的逐轮问题与答案，必须采纳，不重复询问已解决问题。
每轮最多三个问题；剩余问题可继续补充，不因轮数耗尽停止澄清。已解决的问题不得重复。
questions只列确实需要用户决策的缺失项，不放规则说明、已明确的周期或固定指标参数。
所有面向用户的name、label、questions、clarifications及选项都用简短中文，禁止出现withinBars、maxPosition等程序字段名。MACD、KD、K、D可保留。
constraints.previousPlan是用户原有草稿；保留未被明确修改的条件和数值，结合新答案完善，不重新发明一套策略。
constraints.editorRevision为true时，这是策略编辑卡里的一句改动：只改这句话明确提到的项，previousPlan里其他条件和数值逐项原样保留（包括名称、方向、止盈、次数等），不顺手补默认值；previousPlan里已有的值视为用户已确认，不再放进questions，只在这句话本身含糊时才提问。
规则默认跨品种适用；产品绑定由界面单独管理，不把是否绑定产品列为待澄清条件。
只保存规则草稿，不自动启动交易。不要声称完成回测、验证或盈利。只输出JSON，不要Markdown。"""


def _design_schema(provider: str) -> dict:
    schema = StrategyPlan.model_json_schema()
    # 提醒怎么响、上线默认的执行方式由用户在卡片上选，AI 设计不写（不替用户选自动下单）。
    for key in ("reminder", "signalAction"):
        schema["properties"].pop(key, None)
    schema.get("$defs", {}).pop("ReminderOptions", None)

    def strict(value):
        if isinstance(value, dict):
            if value.get("type") == "object" and "properties" in value:
                value["required"] = list(value["properties"])
            value.pop("default", None)
            if isinstance(value.get("title"), str):
                value.pop("title")  # informational only; keeps the inlined schema small
            for child in value.values():
                strict(child)
        elif isinstance(value, list):
            for child in value:
                strict(child)
    strict(schema)
    if provider == "deepseek":
        # DeepSeek accepts nullable unions but rejects a $ref branch
        # inside anyOf. Expand our finite local definitions without
        # removing types, bounds, nullability or local validation.
        definitions = schema.get("$defs", {})

        def inline(value):
            if isinstance(value, dict):
                if "$ref" in value:
                    target = definitions[value["$ref"].removeprefix("#/$defs/")]
                    return inline({**target, **{k: v for k, v in value.items() if k != "$ref"}})
                return {key: inline(child) for key, child in value.items() if key != "$defs"}
            if isinstance(value, list):
                return [inline(child) for child in value]
            return value
        schema = inline(schema)
    return schema


def _user_named_loss_cap(description: str, constraints: dict) -> bool:
    """Did the user's words name a daily loss cap: the description or an answered clarification."""
    answers = constraints.get("clarificationAnswers") or []
    texts = [description, *(f"{item.get('question', '')} {item.get('answer', '')}" for item in answers if isinstance(item, dict))]
    return any(LOSS_CAP_WORDS.search(text) for text in texts)


def _finalize_plan(raw: str, description: str, constraints: dict) -> dict:
    result = StrategyPlan.model_validate_json(raw).model_dump()
    result["description"] = description
    for question in result.get("clarifications", []):
        if question["question"] not in result["questions"]:
            result["questions"].append(question["question"])
    if "maxLoss" not in constraints and not _user_named_loss_cap(description, constraints):
        # Not the user's words: no cap, or the one the draft being revised already had (保留未被明确修改的数值).
        previous = constraints.get("previousPlan")
        result["maxLoss"] = previous.get("maxLoss") if isinstance(previous, dict) else None
    # User-configured safety limits are authoritative over model output.
    result.update({key: constraints[key] for key in ("direction", "maxPosition", "maxLoss") if key in constraints})
    # The model sometimes keeps the default direction "both" while writing only one side (10-08 线上：
    # 用户答“只做多头”，计划却是 both + 只有开多 → 回测报“缺少开空条件”): the rules it wrote decide.
    rules = result.get("rules") or {}
    has_long, has_short = rules.get("entryLong") is not None, rules.get("entryShort") is not None
    if "direction" not in constraints and result.get("direction") == "both" and has_long != has_short:
        result["direction"] = "long" if has_long else "short"
    return StrategyPlan.model_validate(result).model_dump()


async def design_plan(adapter, description: str, constraints: dict, *, on_progress=None, user_id: str | None = None) -> dict:
    if not hasattr(adapter, "complete_structured"):
        raise DesignError("model_unavailable", "当前为示例模式，请先在设置中配置模型再设计策略。")
    if not adapter.agent_authenticated:
        raise DesignError("model_unavailable", "当前模型未配置，请先在设置中配置模型再设计策略。")
    if on_progress:
        on_progress(stage="等待当前模型任务结束")
    async with adapter._design_lock:
        if on_progress:
            on_progress(stage="发送策略设计请求")
        payload = json.dumps({"description": description, "constraints": constraints}, ensure_ascii=False)
        try:
            result = await adapter.complete_structured(
                instructions=INSTRUCTIONS, input_text=payload, schema=StrategyPlan, timeout_seconds=180,
                json_schema=_design_schema(getattr(adapter, "provider", "")), on_progress=on_progress, usage_user=user_id)
        except TimeoutError:
            raise
        except RuntimeError as error:
            code = str(error)
            if code == "unexpected_tool":
                raise DesignError("unexpected_tool", "模型尝试了策略设计不允许的操作，已停止；请重新生成。") from error
            if code in {"model_output_invalid", "model_output_empty"}:
                raise DesignError("invalid_model_output", "模型返回的策略格式不完整，未保存为可运行策略；请重新生成。") from error
            if code.endswith("_api_key_required"):
                raise DesignError("model_unavailable", "当前模型未配置，请先在设置中配置模型再设计策略。") from error
            # Provider-level failures keep their stable code for design_failure.
            raise
        if on_progress:
            on_progress(stage="检查策略格式与参数")
        return _finalize_plan(result.value.model_dump_json(), description, constraints)
