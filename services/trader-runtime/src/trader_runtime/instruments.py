"""统一品种目录（2026-10-18，#976 第 2 步）：外盘品种在各数据源里叫什么，只在这里写一次。

国内合约目录仍由快期合约服务实时给出（shared_market.instruments），本模块只放快期目录里没有、或
各数据源叫法不同的外盘品种。一个外盘品种是一条“主连”（不可交易的连续序列）；具体月份合约（二期
模拟下单用）只有 MT5 给出，由它的数据源按根代码 + 月份拼出，不写进本表。

源代码绑定的都是同一条主连序列：

* ``kqd``：快期合约目录里的外盘主连代码（``KQD.m@COMEX.GC``）；没有的为 None。
* ``sina``：新浪外盘行情代码（``hq.sinajs.cn/list=hf_GC``）；没有的为 None。
* ``mt5``：AMP MT5 的根代码（``GCE`` → ``GCEZ26``），月份由数据源选主力；没有的为 None。

``unit`` 是本软件统一的报价单位；某个源的报价单位不同时由 ``sina_scale`` 换算（新浪 COMEX 铜报美分/磅，
×0.01 成美元/磅，与快期、MT5 一致）。``decimals`` 是展示精度（按交易所最小变动价位）。

``domestic`` 是关联的国内品种（调研“外盘联动”和 AI 按品种查外盘时用）：只表示相关，不表示可互换，
交易单位与国内合约不同，不能直接比价。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# The contract code a read or context field takes (a chart, the window's positions, recent actions, the push focus,
# suggestion context): any catalog symbol, including 快期外盘主连 with “@” (KQD.m@COMEX.GC, 外盘图表 2026-10-19).
# Fields that write orders or strategies keep their own stricter formats.
CONTEXT_SYMBOL_PATTERN = r"^[A-Za-z0-9_.@-]+$"
# 一个合约代码的统一写法（外盘代码统一，2026-10-21）：交易所.代码（国内 SHFE.rb2610、外盘月份合约 CME.GCEZ26），
# 或快期主连 / 外盘主连的 KQ.m@SHFE.rb、KQD.m@CME.NQ。切图表、画线、多图格、上线框、简报这些“指一个合约”的
# 字段都引用它，不在各处另写正则。
SYMBOL_PATTERN = r"^(?:KQD?\.[A-Za-z]@)?[A-Za-z]+\.[A-Za-z0-9]+$"
# 一个品种的键（自选、合约抽屉的品种行）：国内 交易所.品种（SHFE.rb），外盘是品种目录的键（COMEX.GC、CME.6E），
# 也就是快期外盘主连 @ 后面那段。
PRODUCT_KEY_PATTERN = r"^[A-Z]{2,6}\.[A-Za-z0-9]{1,10}$"

@dataclass(frozen=True)
class ForeignProduct:
    key: str             # 本软件的外盘品种键，“交易所.品种”，如 COMEX.GC
    name: str            # 展示名
    exchange: str        # 展示用交易所
    currency: str        # 计价货币
    unit: str            # 报价单位
    domestic: tuple[str, ...]
    kqd: str | None = None
    sina: str | None = None
    mt5: str | None = None
    decimals: int = 2
    sina_scale: float = 1.0
    # 看这个外盘品种时读哪个国内品种的快讯（快讯、事件、日历都按国内品种标注）；省略取 domestic 的第一个。
    # 没有国内对照的（美股指数、外汇、美债、天然气）用自己的快讯键（nasdaq、spx、ust、natgas…，别名在
    # research_live.FLASH_ALIASES），不再显示不筛的全部快讯（2026-10-21 所有者：纳指的快讯跟外盘无关）。
    # domestic 是研究台“外盘对照”用的（国内品种页列出哪些外盘），两者分开：COMEX铜不进沪铜的外盘对照，但看它时读沪铜快讯。
    news: str | None = None
    # CME Globex 交易时段的种类（``trading_calendar.CME_HOURS``，F2 2026-10-21 按 CME Group 合约细则核实）：globex 是
    # 23 小时（美东 18:00–次日 17:00）的金属、能源，equity / rates / fx（股指、美债、外汇）平日同 globex、只是节假日
    # 停盘时刻不同（CME_HOLIDAY_HOURS），grains 是 CBOT 谷物两段（芝加哥 19:00–次日 07:45、08:30–13:20），livestock
    # 是瘦肉猪白天一段（芝加哥 08:30–13:05）。快照、策略、收盘提醒都按它判开没开。
    hours: str = "globex"

    @property
    def news_product(self) -> str | None:
        return self.news or (self.domestic[0] if self.domestic else None)


ENERGY = ("sc", "fu", "lu", "bu", "pg", "ta", "eg", "eb", "ma", "l", "pp", "v")

FOREIGN_PRODUCTS: tuple[ForeignProduct, ...] = (
    ForeignProduct("COMEX.GC", "COMEX黄金", "COMEX", "USD", "美元/盎司", ("au",), "KQD.m@COMEX.GC", "GC", "GCE", decimals=1),
    ForeignProduct("COMEX.SI", "COMEX白银", "COMEX", "USD", "美元/盎司", ("ag",), "KQD.m@COMEX.SI", "SI", "SIE", decimals=3),
    ForeignProduct("COMEX.HG", "COMEX铜", "COMEX", "USD", "美元/磅", (), "KQD.m@COMEX.HG", "HG", "CPE", decimals=4, sina_scale=0.01,
                   news="cu"),
    ForeignProduct("NYMEX.CL", "WTI原油", "NYMEX", "USD", "美元/桶", ENERGY, "KQD.m@NYMEX.CL", "CL", "CLE", decimals=2),
    ForeignProduct("ICE.BRN", "布伦特原油", "ICE", "USD", "美元/桶", ENERGY, None, "OIL", None, decimals=2),
    ForeignProduct("NYMEX.NG", "美国天然气", "NYMEX", "USD", "美元/百万英热", (), "KQD.m@NYMEX.NG", "NG", "NGE", decimals=3, news="natgas"),
    ForeignProduct("LMEOTC.CA", "伦铜", "LME", "USD", "美元/吨", ("cu", "bc"), "KQD.m@LMEOTC.CA", "CAD", None, decimals=1),
    ForeignProduct("LMEOTC.AH", "伦铝", "LME", "USD", "美元/吨", ("al",), "KQD.m@LMEOTC.AH", "AHD", None, decimals=1),
    ForeignProduct("LMEOTC.ZS", "伦锌", "LME", "USD", "美元/吨", ("zn",), "KQD.m@LMEOTC.ZS", "ZSD", None, decimals=1),
    ForeignProduct("LMEOTC.NI", "伦镍", "LME", "USD", "美元/吨", ("ni",), "KQD.m@LMEOTC.NI", "NID", None, decimals=0),
    ForeignProduct("LMEOTC.PB", "伦铅", "LME", "USD", "美元/吨", ("pb",), "KQD.m@LMEOTC.PB", "PBD", None, decimals=1),
    ForeignProduct("LMEOTC.SN", "伦锡", "LME", "USD", "美元/吨", ("sn",), "KQD.m@LMEOTC.SN", "SND", None, decimals=0),
    ForeignProduct("CBOT.ZS", "美豆", "CBOT", "USD", "美分/蒲式耳", ("a", "b"), "KQD.m@CBOT.ZS", "S", "ZSE", decimals=2,
                   hours="grains"),
    ForeignProduct("CBOT.ZM", "美豆粕", "CBOT", "USD", "美元/短吨", ("m",), "KQD.m@CBOT.ZM", "SM", "ZME", decimals=1, hours="grains"),
    ForeignProduct("CBOT.ZL", "美豆油", "CBOT", "USD", "美分/磅", ("y",), "KQD.m@CBOT.ZL", "BO", "ZLE", decimals=2, hours="grains"),
    ForeignProduct("CBOT.ZC", "美玉米", "CBOT", "USD", "美分/蒲式耳", ("c",), "KQD.m@CBOT.ZC", "C", "ZCE", decimals=2, hours="grains"),
    ForeignProduct("NYBOT.CT", "美棉", "ICE", "USD", "美分/磅", ("cf",), "KQD.m@NYBOT.CT", "CT", None, decimals=2),
    # 10-20 所有者“MT5 里其他品种接一下”：MT5（AMP）与快期外盘主连都有的 CME 集团美元计价品种（EUREX 按欧元计价、
    # 美元子账户记不了账，不接；燕麦、稻米、活牛等快期没有图表，暂不接）。
    ForeignProduct("NYMEX.PL", "美铂金", "NYMEX", "USD", "美元/盎司", (), "KQD.m@NYMEX.PL", None, "PLE", decimals=1, news="pt"),
    ForeignProduct("COMEX.MGC", "微型美黄金", "COMEX", "USD", "美元/盎司", (), "KQD.m@COMEX.MGC", None, "MGC", decimals=1, news="au"),
    ForeignProduct("COMEX.SIL", "微型美白银", "COMEX", "USD", "美元/盎司", (), "KQD.m@COMEX.SIL", None, "SIL", decimals=3, news="ag"),
    ForeignProduct("NYMEX.HO", "美燃油", "NYMEX", "USD", "美元/加仑", (), "KQD.m@NYMEX.HO", None, "HOE", decimals=4, news="sc"),
    ForeignProduct("NYMEX.RB", "美汽油", "NYMEX", "USD", "美元/加仑", (), "KQD.m@NYMEX.RB", None, "RBE", decimals=4, news="sc"),
    ForeignProduct("NYMEX.MCL", "微型WTI原油", "NYMEX", "USD", "美元/桶", (), "KQD.m@NYMEX.MCL", None, "MCLE", decimals=2, news="sc"),
    ForeignProduct("NYMEX.QM", "迷你WTI原油", "NYMEX", "USD", "美元/桶", (), "KQD.m@NYMEX.QM", None, "NQM", decimals=3, news="sc"),
    ForeignProduct("NYMEX.QG", "迷你天然气", "NYMEX", "USD", "美元/百万英热", (), "KQD.m@NYMEX.QG", None, "NQG", decimals=3, news="natgas"),
    ForeignProduct("CBOT.ZW", "美麦", "CBOT", "USD", "美分/蒲式耳", (), "KQD.m@CBOT.ZW", None, "ZWA", decimals=2, news="wh",
                   hours="grains"),
    ForeignProduct("CME.ES", "迷你标普500", "CME", "USD", "点", (), "KQD.m@CME.ES", None, "EP", decimals=2, news="spx", hours="equity"),
    ForeignProduct("CME.NQ", "迷你纳指100", "CME", "USD", "点", (), "KQD.m@CME.NQ", None, "ENQ", decimals=2, news="nasdaq", hours="equity"),
    ForeignProduct("CBOT.YM", "迷你道指", "CBOT", "USD", "点", (), "KQD.m@CBOT.YM", None, "YM", decimals=0, news="dji", hours="equity"),
    ForeignProduct("CME.RTY", "迷你罗素2000", "CME", "USD", "点", (), "KQD.m@CME.RTY", None, "RTY", decimals=1, news="russell", hours="equity"),
    ForeignProduct("CME.MES", "微型标普500", "CME", "USD", "点", (), "KQD.m@CME.MES", None, "MES", decimals=2, news="spx", hours="equity"),
    ForeignProduct("CME.MNQ", "微型纳指100", "CME", "USD", "点", (), "KQD.m@CME.MNQ", None, "MNQ", decimals=2, news="nasdaq", hours="equity"),
    ForeignProduct("CBOT.MYM", "微型道指", "CBOT", "USD", "点", (), "KQD.m@CBOT.MYM", None, "MYM", decimals=0, news="dji", hours="equity"),
    ForeignProduct("CME.M2K", "微型罗素2000", "CME", "USD", "点", (), "KQD.m@CME.M2K", None, "M2K", decimals=1, news="russell", hours="equity"),
    ForeignProduct("CME.6E", "欧元期货", "CME", "USD", "美元/欧元", (), "KQD.m@CME.6E", None, "EU6", decimals=5, news="eur", hours="fx"),
    ForeignProduct("CME.6J", "日元期货", "CME", "USD", "美元/日元", (), "KQD.m@CME.6J", None, "JY6", decimals=7, news="jpy", hours="fx"),
    ForeignProduct("CME.6B", "英镑期货", "CME", "USD", "美元/英镑", (), "KQD.m@CME.6B", None, "BP6", decimals=4, news="gbp", hours="fx"),
    ForeignProduct("CME.6A", "澳元期货", "CME", "USD", "美元/澳元", (), "KQD.m@CME.6A", None, "DA6", decimals=5, news="aud", hours="fx"),
    ForeignProduct("CME.6C", "加元期货", "CME", "USD", "美元/加元", (), "KQD.m@CME.6C", None, "CA6", decimals=5, news="cad", hours="fx"),
    ForeignProduct("CME.6S", "瑞郎期货", "CME", "USD", "美元/瑞郎", (), "KQD.m@CME.6S", None, "SF6", decimals=5, news="chf", hours="fx"),
    ForeignProduct("CME.6N", "纽元期货", "CME", "USD", "美元/纽元", (), "KQD.m@CME.6N", None, "NE6", decimals=5, news="nzd", hours="fx"),
    ForeignProduct("CBOT.ZN", "10年美债", "CBOT", "USD", "点", (), "KQD.m@CBOT.ZN", None, "TYA", decimals=6, news="ust", hours="rates"),
    ForeignProduct("CBOT.ZB", "30年美债", "CBOT", "USD", "点", (), "KQD.m@CBOT.ZB", None, "USA", decimals=5, news="ust", hours="rates"),
    ForeignProduct("CBOT.ZF", "5年美债", "CBOT", "USD", "点", (), "KQD.m@CBOT.ZF", None, "FVA", decimals=7, news="ust", hours="rates"),
    ForeignProduct("CBOT.ZT", "2年美债", "CBOT", "USD", "点", (), "KQD.m@CBOT.ZT", None, "TUA", decimals=8, news="ust", hours="rates"),
    ForeignProduct("CME.HE", "CME瘦肉猪", "CME", "USD", "美分/磅", ("lh",), None, "LHC", "HE", decimals=3,
                   hours="livestock"),
    ForeignProduct("BMD.FCPO", "马棕油", "BMD", "MYR", "林吉特/吨", ("p",), None, "FCPO", None, decimals=0),
)

FOREIGN_BY_KEY: dict[str, ForeignProduct] = {item.key: item for item in FOREIGN_PRODUCTS}

# 策略的具体合约代码（外盘三期，2026-10-20）：国内 交易所.品种+3/4 位年月（SHFE.rb2610），外盘 MT5 月份合约
# CME.<根代码><月份字母><两位年份>（CME.GCEZ26）。品种范围写法去掉月份部分（SHFE.rb、CME.GCE）。
# 外盘根代码可以带数字（外汇 CME.EU6Z26、微型罗素 CME.M2KZ26），只要求字母开头。
CONTRACT_MONTH_PATTERN = r"(?:\d{3,4}|[FGHJKMNQUVXZ]\d{2})"
CONTRACT_PRODUCT_PATTERN = r"[A-Za-z]+\.[A-Za-z][A-Za-z0-9]*"
STRATEGY_CONTRACT_PATTERN = rf"{CONTRACT_PRODUCT_PATTERN}{CONTRACT_MONTH_PATTERN}"

# 外盘本站模拟（外盘二期，2026-10-19）：可下单的是 MT5 的具体月份合约，在外盘撮合引擎里叫
# ``CME.<MT5 合约名>``（AMP 把 CME 集团各交易所都放在 ``Exchange-Futures\CME`` 下；与
# services/tqkq-sidecar 的 paper_foreign.engine_symbol 同一写法）。只有带 ``mt5`` 根代码的品种能下单。
FOREIGN_TRADE_EXCHANGE = "CME"
# 美元子账户的账户编号前缀（paper_accounts.py）：编号前缀定币种，账户块据此标 currency。
PAPER_FOREIGN_PREFIX = "paperusd:"
FOREIGN_MONTHS = "FGHJKMNQUVXZ"
# 说法里的外盘品种别名（展示名之外）：只认这些明确指外盘的叫法，不把“黄金”“原油”从国内抢走。
FOREIGN_ALIASES: dict[str, tuple[str, ...]] = {
    "COMEX.GC": ("美黄金", "美金价", "纽约金", "纽金", "COMEX金"),
    "COMEX.SI": ("美白银", "纽约银", "纽银", "COMEX银"),
    "COMEX.HG": ("美铜", "纽约铜", "纽铜"),
    "NYMEX.CL": ("美原油", "美油", "WTI"),
    "NYMEX.NG": ("美天然气", "天然气"),
    "CBOT.ZS": ("美大豆",),
    "NYMEX.PL": ("纽约铂金",),
    "COMEX.MGC": ("微黄金", "微型黄金"),
    "COMEX.SIL": ("微白银", "微型白银"),
    "NYMEX.HO": ("美取暖油", "取暖油", "美柴油"),
    "NYMEX.RB": ("RBOB汽油",),
    "NYMEX.MCL": ("微原油", "微型原油"),
    "NYMEX.QM": ("小原油", "迷你原油"),
    "NYMEX.QG": ("小天然气",),
    "CBOT.ZW": ("美小麦", "美国小麦"),
    "CME.ES": ("标普", "标普500", "标普期货"),
    "CME.NQ": ("纳指", "纳斯达克", "纳指期货"),
    "CBOT.YM": ("道指", "道琼斯", "道指期货"),
    "CME.RTY": ("罗素", "罗素2000"),
    "CME.MES": ("微标普",),
    "CME.MNQ": ("微纳指",),
    "CBOT.MYM": ("微道指",),
    "CME.M2K": ("微罗素",),
    "CBOT.ZN": ("十年美债", "美债10年"),
    "CBOT.ZB": ("三十年美债", "长期美债"),
    "CBOT.ZF": ("五年美债",),
    "CBOT.ZT": ("两年美债", "二年美债"),
}


def is_foreign_paper(account_id: object) -> bool:
    return str(account_id or "").startswith(PAPER_FOREIGN_PREFIX)


def account_currency(account_id: object) -> str:
    """账户的记账币种：外盘本站模拟的美元子账户 USD，其余（国内期货账户）CNY。"""
    return "USD" if is_foreign_paper(account_id) else "CNY"


# 金额的中文单位（复盘、统计、提醒里写“元”的地方按账户或合约的币种写）。
MONEY_UNITS = {"CNY": "元", "USD": "美元"}


def money_unit(currency: object) -> str:
    """一个币种的金额单位：CNY 元、USD 美元；不认识的写代码本身。"""
    return MONEY_UNITS.get(str(currency or "CNY").upper(), str(currency))


def symbol_currency(symbol: object) -> str:
    """一个合约的记账币种：外盘（月份合约、快期外盘主连，或策略适用范围里的品种写法 CME.GCE）按品种目录的计价货币，
    其余（国内）CNY。"""
    text = str(symbol or "").strip()
    product = foreign_product_of(text)
    if product is None and text.upper().startswith(f"{FOREIGN_TRADE_EXCHANGE}."):
        root = text.partition(".")[2].upper()
        product = next((item for item in FOREIGN_PRODUCTS if item.mt5 and item.mt5.upper() == root), None)
    return product.currency if product is not None else "CNY"


def foreign_tradable(product: ForeignProduct) -> bool:
    """能在美元子账户下单的外盘品种：有 MT5 月份合约（撮合行情）且有快期外盘主连（图表与开仓前的行情判断都读它）。"""
    return bool(product.mt5 and product.kqd)


def product_scope(symbol: str) -> str:
    """一个具体合约的品种范围写法（策略适用范围、按品种归并）：SHFE.rb2701 → SHFE.rb，外盘 CME.GCEZ26 → CME.GCE。"""
    text = str(symbol or "").strip()
    if re.fullmatch(rf"{re.escape(FOREIGN_TRADE_EXCHANGE)}\.[A-Za-z0-9]+[{FOREIGN_MONTHS}]\d{{2}}", text, re.I):
        return text[:-3]
    return re.sub(r"\d+$", "", text)


def strategy_scope_symbol(symbol: str) -> str:
    """策略适用范围里的写法（外盘三期）：外盘图表的快期外盘主连（KQD.m@COMEX.GC）是“这个外盘品种的全部月份”，
    写成 CME.<MT5 根代码>（CME.GCE）；其余原样。"""
    text = str(symbol or "").strip()
    product = foreign_product_of(text) if text.upper().startswith("KQD.") else None
    return f"{FOREIGN_TRADE_EXCHANGE}.{product.mt5}" if product is not None and product.mt5 else text


def foreign_trade_symbol(month: str) -> str:
    """MT5 月份合约名 → 外盘撮合引擎里的合约代码（``GCEZ26`` → ``CME.GCEZ26``）。"""
    return f"{FOREIGN_TRADE_EXCHANGE}.{month}"


def foreign_trade_name(symbol: str) -> str | None:
    """一个外盘月份合约的中文名（``CME.GCEZ26`` → ``COMEX黄金2612``，``CME.ENQZ26`` → ``迷你纳指100 2612``），只由代码推出、不读任何行情；不是外盘月份为 None。"""
    product = foreign_product_of(symbol)
    month = str(symbol or "").strip().upper().partition(".")[2]
    if product is None or not str(symbol).upper().startswith(f"{FOREIGN_TRADE_EXCHANGE}.") or len(month) < 4:
        return None
    # 名字以数字结尾的（迷你纳指100）与年月之间空一格，不读成“迷你纳指1002612”。
    gap = " " if product.name[-1].isdigit() else ""
    return f"{product.name}{gap}{int(month[-2:]):02d}{FOREIGN_MONTHS.index(month[-3]) + 1:02d}"


def foreign_display_name(symbol: str) -> str | None:
    """外盘代码给人看、给人听的名字（推送、提醒、语音、回执都用它）：月份合约 ``CME.GCEZ26`` → ``COMEX黄金2612``，
    快期外盘主连 ``KQD.m@COMEX.GC`` → ``COMEX黄金``；不是外盘代码为 None。"""
    text = str(symbol or "").strip()
    named = foreign_trade_name(text)
    if named:
        return named
    product = foreign_product_of(text) if text.upper().startswith("KQD.") else None
    return product.name if product is not None else None


# 外盘代码在一段中文里的样子：快期外盘主连、带或不带 CME. 的月份合约（GCEZ26）。前后不能紧挨字母数字（中文字算
# \w，所以不用 \b）。
_FOREIGN_CODE = re.compile(r"(?<![A-Za-z0-9.@])(?:KQD\.[A-Za-z]@[A-Za-z]+\.[A-Za-z0-9]+"
                           r"|(?:[A-Za-z]+\.)?[A-Z][A-Z0-9]*[FGHJKMNQUVXZ]\d{2})(?![A-Za-z0-9])")


def _code_name(code: str) -> str:
    bare = "." not in code
    return foreign_display_name(f"{FOREIGN_TRADE_EXCHANGE}.{code}" if bare else code) or code


def foreign_codes_named(text: str) -> str:
    """一段话里的外盘代码换成中文名（``CME.GCEZ26`` / ``GCEZ26`` → ``COMEX黄金2612``，``KQD.m@COMEX.GC`` →
    ``COMEX黄金``）：语音念的不再是字母串。认不出的代码原样留着。"""
    return _FOREIGN_CODE.sub(lambda match: _code_name(match.group(0)), str(text or ""))


def foreign_product_of(symbol: str) -> ForeignProduct | None:
    """一个外盘合约代码属于哪个外盘品种：快期外盘主连（``KQD.m@COMEX.GC``）或可下单的月份合约
    （``CME.GCEZ26``）；国内合约与认不出的为 None。"""
    text = str(symbol or "").strip().upper()
    for item in FOREIGN_PRODUCTS:
        if item.kqd and text == item.kqd.upper():
            return item
    exchange, _, month = text.partition(".")
    if exchange != FOREIGN_TRADE_EXCHANGE or len(month) < 4:
        return None
    root, letter, year = month[:-3], month[-3], month[-2:]
    if letter not in FOREIGN_MONTHS or not year.isdigit():
        return None
    return next((item for item in FOREIGN_PRODUCTS if item.mt5 and item.mt5.upper() == root), None)


def cme_hours_of(symbol: str) -> str:
    """一个外盘合约（主连或月份合约）的 CME 时段种类（``ForeignProduct.hours``）；认不出的按 23 小时的 globex。"""
    product = foreign_product_of(symbol)
    return product.hours if product is not None else "globex"


def foreign_for_domestic(product: str) -> tuple[ForeignProduct, ...]:
    """The foreign products related to a domestic product id (``cu`` → 伦铜, COMEX铜); none for most."""
    key = product.lower()
    return tuple(item for item in FOREIGN_PRODUCTS if key in item.domestic)


def foreign_words(product: ForeignProduct) -> tuple[str, ...]:
    """A tradable foreign product's names in words and codes (展示名、别名、品种键、快期主连代码), longest first."""
    names = {product.name, product.key, *(FOREIGN_ALIASES.get(product.key) or ())}
    if product.kqd:
        names.add(product.kqd)
    return tuple(sorted(names, key=len, reverse=True))


def _foreign_mentions(text: str) -> list[tuple[int, int, ForeignProduct]]:
    """Every tradable foreign product the text names, as (start, end, product) spans: the longest name wins its span
    (美豆粕 is 美豆粕, not 美豆), so one product's name inside another's never counts twice."""
    upper = text.upper()
    words = sorted(((word.upper(), product) for product in FOREIGN_PRODUCTS if foreign_tradable(product)
                    for word in foreign_words(product)), key=lambda pair: len(pair[0]), reverse=True)
    taken: list[tuple[int, int, ForeignProduct]] = []
    for word, product in words:
        start = upper.find(word)
        while start >= 0:
            end = start + len(word)
            if not any(start < other_end and other_start < end for other_start, other_end, _ in taken):
                taken.append((start, end, product))
            start = upper.find(word, start + 1)
    for match in re.finditer(r"[A-Za-z]+\.?[A-Za-z0-9]+", text):
        token = match.group(0)
        coded = foreign_product_of(token if "." in token else f"{FOREIGN_TRADE_EXCHANGE}.{token}")
        if coded is not None and foreign_tradable(coded) and not any(
                match.start() < end and start < match.end() for start, end, _ in taken):
            taken.append((match.start(), match.end(), coded))
    return sorted(taken, key=lambda span: span[0])


def foreign_named(*texts: str | None) -> ForeignProduct | None:
    """The one tradable foreign product (one with an MT5 month) these words name — a name, an alias, a key, a 快期
    主连 code or an MT5 month code (``GCEZ26`` / ``CME.GCEZ26``); None when they name none, or two different ones."""
    found = {product.key for text in texts for _, _, product in _foreign_mentions(str(text or ""))}
    return FOREIGN_BY_KEY[found.pop()] if len(found) == 1 else None


def foreign_spoken(text: str, product: ForeignProduct) -> str:
    """The words with the product's other names put as its display name (美黄金 → COMEX黄金, WTI → WTI原油): the
    USD sub-account's catalog names the product by its display name, and the card's grounding reads the words
    against it. Month codes stay as they are (the catalog knows them)."""
    out, cursor = [], 0
    for start, end, named in _foreign_mentions(str(text or "")):
        if named.key != product.key or text[start:end].upper() == product.name.upper():
            continue
        if foreign_product_of(text[start:end] if "." in text[start:end] else f"{FOREIGN_TRADE_EXCHANGE}.{text[start:end]}"):
            continue  # a month code (GCEZ26): the catalog reads it as it is
        out.extend([text[cursor:start], product.name])
        cursor = end
    out.append(text[cursor:])
    return "".join(out)
