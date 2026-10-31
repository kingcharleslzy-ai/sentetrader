"""Recorded-shape samples for the 快讯 feed and the economic calendar in browser tests.

The fake-data runtime has no akshare; with this flag the research live service polls these rows
instead, through the same normalise / tag / merge / calendar code as the real sources. Times are
relative to when the runtime started, so the panel always has a fresh last-24-hours feed and a
calendar week that starts today. One 上海金属网 line changes every minute, so a refresh shows a
newly arrived item. Other datasets have no sample and answer like an unreachable source.

The 沪金 rows of 2026-10-07 evening are real headlines (the 第六批 sample): the same 现货黄金 drop
filed by several outlets, the 30 年美债 high, routine 欧央行 remarks. `FakeNewsModel` screens them
by a fixed table and writes the 今日新闻要点 from its input, so the browser tests see merged events,
the three tiers and a brief without a model key.

第七批 (2026-10-07): the 沪金 figures of that evening's 调研 (the approved mockup's numbers — COMEX gold,
the US 10-year yield, the 09-30 basis, receipts, top-20 net long and term structure) stand in for AKShare,
and the same fake model writes the desk's 今日判断 from the facts by fixed rules, so the 调研 page shows its
tiles, both columns and the 新闻面 line in the browser tests.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from datetime import datetime, timedelta
from typing import Any

from ..foreign_market import parse_sina
from ..news_analyst import BriefDraft
from ..news_screen import ScreenBatch
from ..news_translate import TranslationBatch
from ..research_data import CHINA, ResearchDataService
from ..research_desk import DeskJudgment
from ..research_live import LiveSources

ENV_FLAG = "AI_TRADER_FAKE_RESEARCH_LIVE"

# (minutes before start, source, title, text, link?)
_FLASH: list[tuple[int, str, str, str, str | None]] = [
    (3, "财联社", "美联储官员", "【美联储官员：年内仍有降息空间】财联社10月3日电，美联储理事表示，若通胀继续回落，年内仍有一次降息的空间，"
     "但需观察下周公布的非农就业与 CPI 数据；市场对 12 月降息的定价升至六成。", "https://www.cls.cn/detail/1"),
    (9, "东方财富", "沪金夜盘走高", "沪金主力夜盘走高 0.8%，COMEX黄金站上关键阻力位，避险需求与美元回落共同支撑金价。", "https://finance.eastmoney.com/a/1.html"),
    (9, "新浪", "", "新浪财经10月3日讯，沪金主力夜盘走高 0.8%，COMEX黄金站上关键阻力位，避险需求与美元回落共同支撑金价。", None),
    (26, "上海金属网", "", "SHMET 10月3日讯，白银现货升水扩大，沪银主力合约成交放量，库存连续第三周下降。", None),
    (41, "财联社", "铁矿石发运", "【澳巴铁矿石发运量环比回升】财联社10月3日电，本周澳洲、巴西铁矿石发运总量环比增加 180 万吨，"
     "港口库存小幅累积，钢厂补库节奏放缓。", "https://www.cls.cn/detail/2"),
    (68, "东方财富", "螺纹钢表观需求", "本周螺纹钢表观需求回落，钢厂库存小幅增加，市场关注节后补库力度。", "https://finance.eastmoney.com/a/2.html"),
    (95, "新浪", "", "央行今日开展 2000 亿元 7 天期逆回购操作，中标利率持平，当日净投放 800 亿元。", None),
    (130, "财联社", "沪深300", "【沪深300 股指期货升水收窄】财联社10月3日电，沪深300 股指期货主力合约升水收窄至 5 点，"
     "两市成交额回落至万亿元下方，北向资金小幅净流出。", "https://www.cls.cn/detail/3"),
    (190, "东方财富", "欧洲央行", "欧洲央行管委称欧元区通胀已接近目标，下次会议维持利率不变的可能性较大。", "https://finance.eastmoney.com/a/3.html"),
    (12, "同花顺", "苹果新品", "苹果公司发布新一代 iPhone，库克称本季度营收有望创新高。", "https://news.10jqka.com.cn/1.shtml"),
    (48, "富途", "", "山东栖霞冷库红富士出库偏慢，客商采购以质优货为主。", None),
    # Shares 沪金's alias but not its market: the 排除词 drop it from 沪金's view into the fold.
    (75, "同花顺", "黄金周出游", "国庆黄金周出游人次创新高，酒店预订量同比增长两成。", "https://news.10jqka.com.cn/2.shtml"),
    (300, "上海金属网", "", "SHMET 10月3日讯，国内铜精矿加工费继续下行，冶炼厂检修增多，沪铜现货贴水收窄。", None),
    # 2026-10-07 晚的沪金快讯（真实标题，时间改为相对启动）：同一件事被几家各发一条。
    (14, "新浪", "", "现货黄金失守4110美元/盎司，日内跌1.31%。", None),
    (14, "东方财富", "现货黄金失守4110美元", "现货黄金失守4110美元/盎司，日内跌1.31%。", "https://finance.eastmoney.com/a/4.html"),
    (40, "财联社", "", "【现货黄金日内走低1%】财联社10月7日电，现货黄金日内走低1%，现报4122.03美元/盎司。", "https://www.cls.cn/detail/5"),
    (41, "同花顺", "现货黄金日内跌幅达1%", "现货黄金日内跌幅达1%，报4121.22美元/盎司。", "https://news.10jqka.com.cn/3.shtml"),
    (42, "新浪", "", "现货黄金日内跌1%，现报4121.22美元/盎司。", None),
    (35, "富途", "美债收益率", "美国30年期国债收益率升至5.70%，创2002年以来新高。", "https://news.futunn.com/1"),
    (36, "财联社", "", "【美国30年期国债收益率创2002年以来新高】财联社10月7日电，美国30年期国债收益率升至5.70%。", "https://www.cls.cn/detail/6"),
    (16, "同花顺", "德商银行", "德商银行：美联储会议纪要可能已“过时”，市场更关注本周通胀数据。", "https://news.10jqka.com.cn/4.shtml"),
    (20, "新浪", "", "欧洲央行理事桑托斯·佩雷拉：其他商品价格未显示通胀预期脱锚迹象。", None),
    (22, "新浪", "", "欧洲央行委员桑托斯·佩雷拉：目前尚未出现通胀第二轮传导效应。", None),
    (66, "财联社", "", "【欧洲央行管委多伦茨：当前利率水平保留充足政策灵活性】财联社10月7日电。", "https://www.cls.cn/detail/7"),
    (24, "东方财富", "机构：黄金结构性驱动未消失", "机构：黄金结构性驱动未消失，关键看油价、通胀与美债。", "https://finance.eastmoney.com/a/5.html"),
    (38, "富途", "", "美国十月加息概率下降，短端美债获喘息，黄金跌幅收窄。", None),
    (150, "东方财富", "黄金ETF持仓", "黄金ETF持仓创四年新高，但金价仍在低位横盘。", "https://finance.eastmoney.com/a/6.html"),
    (170, "财联社", "", "【多国央行继续增持黄金】财联社10月7日电，世界黄金协会称多国央行9月继续增持黄金。", "https://www.cls.cn/detail/8"),
    (26 * 60, "财联社", "隔夜外盘", "【隔夜外盘】财联社10月2日电，美股三大指数收涨，美元指数回落，国际金价创两周新高。",
     "https://www.cls.cn/detail/4"),
]

# (day offset, HH:MM, region, event, importance, actual?, forecast?, previous?)
_CALENDAR: list[tuple[int, str, str, str, int, float | None, float | None, float | None]] = [
    (0, "09:30", "中国", "9月财新服务业PMI", 2, 50.6, 50.4, 50.3),
    (0, "20:30", "美国", "9月非农就业人数(万人)", 3, None, 14.5, 14.2),
    (0, "20:30", "美国", "9月失业率(%)", 3, None, 4.3, 4.3),
    (0, "22:30", "美国", "EIA原油库存(万桶)", 2, None, -120.0, 180.0),
    (1, "17:00", "欧元区", "8月零售销售月率(%)", 2, None, 0.2, -0.1),
    (2, "22:00", "美国", "9月ISM非制造业PMI", 2, None, 51.8, 52.0),
    (3, "09:30", "中国", "9月CPI年率(%)", 3, None, 0.6, 0.5),
    (3, "09:30", "中国", "9月PPI年率(%)", 2, None, -1.5, -1.8),
    (4, "20:30", "美国", "9月CPI年率(%)", 3, None, 2.9, 2.9),
    (5, "02:00", "美国", "美联储公布会议纪要", 3, None, None, None),
    (6, "17:00", "欧元区", "8月工业产出月率(%)", 2, None, 0.3, -0.2),
]


class _NoSample:
    """The AKShare module stand-in: the dependency check passes; a dataset without a sample here
    (foreign quotes, rates, LPR, hog) fails like an unreachable source, so its feed reports it."""

    def __getattr__(self, name: str) -> Any:
        raise LookupError(f"research_fake_no_sample:{name}")


RECENT_MINUTES = 600


class FakeLiveSources(LiveSources):
    """Stands in for the AKShare calls with the samples above."""

    def __init__(self) -> None:
        super().__init__()
        self.started = datetime.now(CHINA)
        # The samples reach back up to max(_FLASH) minutes; within that long after the 08:30 research-day
        # turn they would fall on yesterday and today's 快讯/要点 would be empty (CI at 08:33 北京, 10-08).
        # Squeeze them into the minutes since the turn, keeping their order.
        turn = self.started.replace(hour=8, minute=30, second=0, microsecond=0)
        since = (self.started - turn).total_seconds() - 30
        # Only today's samples (≤ RECENT_MINUTES old) are squeezed; the deliberately old one stays old.
        oldest = max(minutes for minutes, *_ in _FLASH if minutes <= RECENT_MINUTES) * 60
        self._scale = min(1.0, since / oldest) if 0 < since < oldest else 1.0
        self._module = _NoSample()

    def _flash(self, source: str) -> list[dict[str, Any]]:
        rows = []
        for minutes, name, title, text, link in _FLASH:
            if name != source:
                continue
            scale = self._scale if minutes <= RECENT_MINUTES else 1.0
            moment = self.started - timedelta(seconds=minutes * 60 * scale)
            stamp = moment.strftime("%Y-%m-%d %H:%M:%S")
            # 财联社 splits 发布日期 / 发布时间; 东方财富 and 上海金属网 give one 发布时间, 新浪 a 时间.
            clock = moment.strftime("%H:%M:%S") if source == "财联社" else stamp
            rows.append({"发布日期": moment.strftime("%Y-%m-%d"), "发布时间": clock, "时间": stamp,
                         "标题": title, "内容": text, "摘要": text, "链接": link})
        return rows

    def flash_cls(self) -> Any:
        return self._flash("财联社")

    def flash_em(self) -> Any:
        return self._flash("东方财富")

    def flash_sina(self) -> Any:
        return self._flash("新浪")

    def flash_ths(self) -> Any:
        return self._flash("同花顺")

    def flash_futu(self) -> Any:
        return self._flash("富途")

    def flash_shmet(self, symbol: str) -> Any:
        # One metal's page per call: the minute-changing line rides on 贵金属, the samples on 铜.
        if symbol == "贵金属":
            now = datetime.now(CHINA)
            return [{"发布时间": now.strftime("%Y-%m-%d %H:%M:%S"),
                     "内容": f"SHMET {now.month}月{now.day}日讯，{now:%H:%M} 盘中沪金主力报价小幅震荡，现货成交平稳。"}]
        return self._flash("上海金属网") if symbol == "铜" else []

    def bond_rates(self, start_date: str) -> Any:
        return [{"日期": self.started.strftime("%Y-%m-%d"), "美国国债收益率10年": 5.35}]

    def shibor(self) -> Any:
        return []

    def economic_calendar(self, date: str) -> Any:
        day = datetime.strptime(date, "%Y%m%d").date()
        offset = (day - self.started.date()).days
        return [{"日期": day.isoformat(), "时间": clock, "地区": region, "事件": event, "重要性": importance,
                 "公布": actual, "预期": forecast, "前值": previous}
                for days, clock, region, event, importance, actual, forecast, previous in _CALENDAR if days == offset]


# 沪金's AKShare datasets as the 10-07 调研 read them (dated 09-30, the last trading day before the holiday).
_GOLD_DATASETS: dict[str, dict[str, Any]] = {
    "spot_basis": {"status": "ok", "asOf": "20260930", "source": "生意社（AKShare）",
                   "data": {"spotPrice": 916.6, "dominantBasis": 6.02, "dominantBasisRate": 0.0066}},
    "warehouse_receipt": {"status": "ok", "asOf": "20260930", "source": "上期所（AKShare）", "data": {"total": 116028, "change": -3}},
    "position_rank": {"status": "ok", "asOf": "20260930", "source": "上期所（AKShare）",
                      "data": {"contract": "au2612", "netLong": 219000, "longTotal": 412000, "shortTotal": 193000}},
    "term_structure": {"status": "ok", "asOf": "20260930", "source": "新浪（AKShare）",
                       "data": {"shape": "contango", "nearContract": "au2612", "farContract": "au2706", "spread": 16.16}},
}


class FakeResearchData(ResearchDataService):
    """The research datasets of the browser tests: 沪金's recorded figures; every other product answers
    like the AKShare-less runtime (unavailable)."""

    def summary(self, symbol: str, names: Any = None) -> dict[str, Any]:
        result = super().summary(symbol) if names is None else super().summary(symbol, names)
        if result.get("product", "").lower() == "au":
            result["datasets"] = {**result.get("datasets", {}),
                                  **{name: value for name, value in _GOLD_DATASETS.items() if names is None or name in names}}
        return result


def _sign(text: str) -> float | None:
    match = re.search(r"[+-]?\d+(?:\.\d+)?", text)
    return float(match.group()) if match else None


def _desk_judgment(payload: dict[str, Any]) -> DeskJudgment:
    """The desk's 今日判断 by fixed rules: each known fact is a reason pointing one way, the side with two
    more reasons wins (else 震荡); texts copy the fact so every number and quote is anchored."""
    facts = {fact["id"]: fact for fact in payload["facts"]}
    reasons: list[dict[str, Any]] = []

    def reason(fact_id: str, direction: str) -> None:
        fact = facts[fact_id]
        reasons.append({"text": f"{fact['label']}：{fact['value']}"[:120], "basis": [fact_id], "quote": fact["value"][:30],
                        "direction": direction})
    for fact_id, rule in (("M.indicators", lambda v: "偏多" if (r := _sign(v.split("RSI", 1)[-1])) is not None and r < 30
                           else "偏空" if r is not None and r > 70 else "中性"),
                          ("D.position_rank", lambda v: "偏多" if (_sign(v) or 0) > 0 else "偏空"),
                          ("D.spot_basis", lambda v: "偏多" if (_sign(v.split("基差", 1)[-1]) or 0) > 0 else "偏空"),
                          ("E.rates", lambda v: "偏空"),
                          ("M.foreign", lambda v: "偏空" if "-" in v else "偏多"),
                          ("M.trend", lambda v: "偏空" if (_sign(v) or 0) < 0 else "偏多")):
        if fact_id in facts:
            reason(fact_id, rule(facts[fact_id]["value"]))
    bull = [item for item in reasons if item["direction"] == "偏多"]
    bear = [item for item in reasons if item["direction"] == "偏空"]
    stance = "偏多" if len(bull) - len(bear) >= 2 else "偏空" if len(bear) - len(bull) >= 2 else "震荡"
    points, risks = (bull, bear) if stance != "偏空" else (bear, bull)
    if not points:
        points, risks = [item for item in reasons if item["direction"] == "中性"] or reasons[:1], risks
    headline = {"偏多": "偏多的理由更多，短期偏强", "偏空": "偏空的理由更多，短期偏弱"}.get(stance, "多空理由接近，短期宽幅震荡")
    conclusion = {"偏多": "偏多理由占上风；若外盘转弱，倾向转震荡。", "偏空": "偏空理由占上风；若外盘企稳，倾向转震荡。"}.get(
        stance, "两边力量接近，所以判震荡；若利率继续上行，倾向转空。")
    watch = ([{"text": "接下来的重要经济数据", "basis": ["E.calendar"], "quote": facts["E.calendar"]["value"][:20]}]
             if "E.calendar" in facts else [])
    return DeskJudgment.model_validate({"stance": stance, "confidence": "中", "headline": headline,
                                        "headlineBasis": points[0]["basis"] if points else [], "points": points[:4],
                                        "risks": risks[:3], "watch": watch, "conclusion": conclusion})


_FACT_RULES = (("M.indicators", lambda v: "偏多" if (r := _sign(v.split("RSI", 1)[-1])) is not None and r < 30
                 else "偏空" if r is not None and r > 70 else "中性"),
               ("D.position_rank", lambda v: "偏多" if (_sign(v) or 0) > 0 else "偏空"),
               ("D.spot_basis", lambda v: "偏多" if (_sign(v.split("基差", 1)[-1]) or 0) > 0 else "偏空"),
               ("E.rates", lambda v: "偏空"),
               ("M.foreign", lambda v: "偏空" if "-" in v else "偏多"),
               ("M.trend", lambda v: "偏空" if (_sign(v) or 0) < 0 else "偏多"),
               ("D.term_structure", lambda v: "偏空" if "升水" in v else "偏多"),
               ("T.event", lambda v: "偏多"))


def _debate_turn(instructions: str, input_text: str) -> str:
    """多 AI 研讨's spoken turns by fixed rules: the side's first two facts pointing its way, each cited by its
    number (text copied, so the check passes); the bear's opening answer adds one figure no fact carries (shown
    as 未核对). A rebuttal answers the other side's first fact with the side's own first one."""
    side = "偏多" if "你是多方" in instructions else "偏空"
    material = json.loads(input_text.split("\n议题：", 1)[0].removeprefix("底稿："))
    facts = {fact["id"]: fact for fact in material["底稿"]}
    mine = [fact_id for fact_id, rule in _FACT_RULES if fact_id in facts and rule(facts[fact_id]["value"]) == side]
    other = [fact_id for fact_id, rule in _FACT_RULES if fact_id in facts and rule(facts[fact_id]["value"]) not in (side, "中性")]
    lean = side if len(mine) >= len(other) else "震荡"

    def cite(fact_id: str) -> str:
        fact = facts[fact_id]
        return f"{fact['label']}是 {str(fact['value']).split('，')[0]}[F{fact['n']}]"
    opening = "现在由你开场" in input_text
    lines = [("跌到这儿，空头该收手了。" if side == "偏多" else "超卖不等于见底。") if opening or "多方刚开完场" in input_text
             else f"你说{facts[other[0]]['label']}[F{facts[other[0]]['n']}]，我认。但你漏了一点。" if other else "你说的我不全认。"]
    lines += [f"{cite(fact_id)}。" for fact_id in (mine[:2] if opening or "多方刚开完场" in input_text else mine[:1])]
    if side == "偏空" and "多方刚开完场" in input_text and "M.trend" in facts:
        lines.append(f"去年同期价格跌了 8%[F{facts['M.trend']['n']}]。")
    return "".join(lines) + f"\n总体看法：{lean}"


def _analyst_report(instructions: str, input_text: str) -> str:
    """An analyst's report by fixed rules: its first three facts as the reasoning (each cited, text copied, so the
    check passes), a conclusion and up to three key points from them."""
    title = re.search(r"里的(.+?)分析师", instructions)
    rows = json.loads(input_text.split("\n资料：", 1)[1].split("\n", 1)[0])
    facts = [row for row in rows if row["id"] != "M.price"][:3] or rows[:1]

    def cite(row: dict[str, Any]) -> str:
        return f"{row['label']}是 {str(row['value']).split('，')[0].split('；')[0]}[F{row['n']}]"
    body = "".join(f"先看{cite(row)}，这会通过供需和预期一步步传到价格上。" for row in facts) or "这一块调研页没有资料。"
    points = "\n".join(f"{str(row['label'])[:8]}：{str(row['value']).split('，')[0].split('；')[0]}[F{row['n']}]" for row in facts)
    return f"结论：{title.group(1) if title else ''}这块多空交织，略偏谨慎\n{body}\n要点：\n{points}"


class FakeWebSearch:
    """The debate analysts' web search on the fake-data runtime: two recorded pages with figures, dated today, for
    any query (no key, no network)."""

    async def search(self, query: str, *, count: int = 5, **_: Any) -> dict[str, Any]:
        await asyncio.sleep(float(os.getenv("AI_TRADER_FAKE_DEBATE_SECONDS", "0") or 0) / 4)
        today = datetime.now(CHINA).date().isoformat()
        topic = query.split(" after:", 1)[0][:20]
        return {"query": query, "results": [
            {"title": f"{topic}：样本企业开工率 72.4%，环比 -1.3 个百分点", "content": f"{topic}，本周样本企业开工率 72.4%，较上周下降 1.3 个百分点，库存 18.6 万吨。",
             "link": f"https://example.com/fake/{abs(hash(topic)) % 10_000}/a", "media": "样本资讯", "publishDate": today},
            {"title": f"{topic}：即期利润 -38 元/吨", "content": "即期生产利润 -38 元/吨，连续两周亏损。",
             "link": f"https://example.com/fake/{abs(hash(topic)) % 10_000}/b", "media": "样本行业网", "publishDate": today},
        ][:count], "searchedAt": today}


def _debate_verdict(input_text: str) -> Any:
    """The judge by fixed rules: the side with more checked cited sentences wins by a little (a tie is 平); it
    adopts each side's first cited sentence, rejects the loser's second one, and takes the 20-day low (else the
    day's lower limit) under the price as the 失效位."""
    from ..research_debate import DebateVerdict
    cases = json.loads(input_text.rsplit("\n", 1)[-1])
    material = json.loads(input_text.split("\n议题：", 1)[0].removeprefix("底稿："))
    facts = {fact["id"]: fact for fact in material["底稿"]}
    a = [item for item in cases.get("A 方") or [] if item["fact_ids"]]
    b = [item for item in cases.get("B 方") or [] if item["fact_ids"]]
    winner = "A" if len(a) > len(b) else "B" if len(b) > len(a) else "平"
    win, lose = (a, b) if winner != "B" else (b, a)
    adopted = [{"id": side[0]["id"], "reason": "有数据支撑"} for side in (win, lose) if side]
    rejected = [{"id": item["id"], "reason": "被对方驳倒"} for item in lose[1:2]] if winner != "平" else []
    low = re.search(r"最低 ([\d.]+)", facts.get("K.levels", {}).get("value", ""))
    price = _sign(facts.get("M.price", {}).get("value", ""))
    floor = re.search(r"跌停 ([\d.]+)", facts.get("M.limit", {}).get("value", ""))
    line = (float(low.group(1)) if low and price and float(low.group(1)) < price
            else float(floor.group(1)) if floor and price and float(floor.group(1)) < price else None)
    return DebateVerdict.model_validate({
        "winner": winner, "margin": "略", "insufficient": not (a or b),
        "headline": "两边各有依据，胜方的理由更站得住" if winner != "平" else "两边理由相当，先看震荡",
        "reason": "两边都拿出了数据，胜方引用的资料更多、也更直接；输的一方有一句数字找不到出处，没算进来。",
        "adopted": adopted, "rejected": rejected, "swing": "资金和基差谁先转向", "change_mind": "胜方依据的数据下周反转",
        "invalidation": line, "watch": ["下周仓单变化", "主力持仓变化"],
        "disagreements": ["资金在进场还是离场", "库存变化能不能传到现货"],
        "scenarios": ["如果仓单继续减少，那么现货偏紧的逻辑会加强", "如果主力持仓转为净空，那么胜方的资金理由就站不住"]})


# FakeNewsModel's screening table: (words that must all appear, product, verdict). First match wins;
# anything else is judged unrelated.
_VERDICTS: list[tuple[tuple[str, ...], str, dict[str, Any]]] = [
    (("现货黄金", "4110"), "au", {"event": "价格", "direction": "偏空", "strength": "强", "reason": "外盘金价跌破关键价位",
                                    "transmission": "外盘下跌直接带动沪金走低", "eventLabel": "现货黄金日内下跌"}),
    (("现货黄金", "1%"), "au", {"event": "价格", "direction": "偏空", "strength": "中", "reason": "外盘金价走弱",
                                 "transmission": "外盘下跌直接带动沪金走低", "eventLabel": "现货黄金日内下跌"}),
    (("30年期国债收益率",), "au", {"event": "宏观", "direction": "偏空", "strength": "中", "reason": "长端利率创新高",
                                   "transmission": "收益率越高，不付利息的黄金越吃亏", "eventLabel": "美债收益率创新高"}),
    (("黄金结构性驱动",), "au", {"event": "其他", "direction": "偏多", "strength": "中", "reason": "机构认为驱动仍在",
                                 "transmission": "地缘和央行购金仍在，回调后有支撑", "eventLabel": "机构看好黄金驱动"}),
    (("增持黄金",), "au", {"event": "需求", "direction": "偏多", "strength": "中", "reason": "官方购金延续",
                          "transmission": "官方购金是长期需求支撑", "eventLabel": "多国央行增持黄金"}),
    (("加息概率下降",), "au", {"event": "宏观", "direction": "偏多", "strength": "弱", "reason": "加息预期降温",
                               "transmission": "利率压力减轻，持金成本下降", "eventLabel": "美国加息概率下降"}),
    (("沪金主力夜盘走高",), "au", {"event": "价格", "direction": "偏多", "strength": "中", "reason": "夜盘上涨",
                                   "transmission": "外盘与美元回落带动沪金", "eventLabel": "沪金夜盘走高"}),
    (("ETF持仓",), "au", {"event": "持仓资金", "direction": "中性", "strength": "弱", "reason": "持仓增加但价格未动",
                          "transmission": "资金流入与价格背离，影响不明", "eventLabel": "黄金ETF持仓新高"}),
    (("欧洲央行",), "au", {"event": "宏观", "direction": "中性", "strength": "弱", "reason": "官员例行表态",
                          "transmission": "欧元区政策预期经美元间接影响金价", "eventLabel": "欧央行官员表态"}),
    (("美联储",), "au", {"event": "宏观", "direction": "中性", "strength": "弱", "reason": "纪要前的观点",
                        "transmission": "利率预期变化影响持金成本", "eventLabel": "美联储纪要前瞻"}),
    (("盘中沪金主力报价",), "au", {"event": "价格", "direction": "中性", "strength": "弱", "reason": "盘中窄幅波动",
                                  "transmission": "现货成交平稳，价格无方向", "eventLabel": "沪金盘中震荡"}),
    # 资讯 (Eastmoney's live reports in a local preview): a gold or precious-metals outlook is one shared story.
    (("黄金",), "au", {"event": "其他", "direction": "中性", "strength": "中", "reason": "机构谈后市",
                      "transmission": "机构观点影响市场对沪金的预期", "eventLabel": "机构谈黄金后市"}),
    (("贵金属",), "au", {"event": "其他", "direction": "中性", "strength": "中", "reason": "机构谈后市",
                        "transmission": "机构观点影响市场对沪金的预期", "eventLabel": "机构谈黄金后市"}),
    (("白银",), "ag", {"event": "库存", "direction": "偏多", "strength": "中", "reason": "库存连续下降",
                      "transmission": "库存下降、升水扩大支撑沪银", "eventLabel": "白银库存下降"}),
    (("铁矿石",), "i", {"event": "供给", "direction": "偏空", "strength": "中", "reason": "发运回升",
                       "transmission": "发运增加、港口累库压制矿价", "eventLabel": "澳巴铁矿发运回升"}),
    (("沪深300",), "if", {"event": "持仓资金", "direction": "中性", "strength": "弱", "reason": "升水收窄",
                         "transmission": "基差收窄反映情绪转淡", "eventLabel": "股指期货升水收窄"}),
    (("栖霞冷库",), "ap", {"event": "库存", "direction": "偏空", "strength": "中", "reason": "出库偏慢",
                          "transmission": "冷库出库慢，现货压力传到期货", "eventLabel": "栖霞冷库出库偏慢"}),
    (("铜精矿",), "cu", {"event": "供给", "direction": "偏多", "strength": "中", "reason": "加工费下行",
                        "transmission": "矿端偏紧抬高冶炼成本", "eventLabel": "铜精矿加工费下行"}),
]


class _Result:
    def __init__(self, value: Any, tokens: int) -> None:
        self.value, self.tokens = value, tokens


class FakeNewsModel:
    """The adapter surface the screener, the news analyst and the research desk use, answered by fixed
    rules: no key, no network. Screening reads the table above; the brief cites the input's strongest
    events; the desk's judgment is `_desk_judgment`."""

    agent_authenticated = True
    model = "fake-news"
    # 策略页改版第八批 (2026-10-08): the same fake also designs a strategy from one sentence (a
    # breakout plan named after the sentence) and writes the 回测 verdict from the fact sentences.
    _design_lock = asyncio.Lock()

    async def stream_text(self, *, instructions: str, input_text: str, on_text: Any = None, **_: Any) -> _Result:
        """多 AI 研讨's spoken turns, written out in a few pieces; AI_TRADER_FAKE_DEBATE_SECONDS spreads a turn over
        that many seconds so browser checks see the text stream with its caret."""
        text = _analyst_report(instructions, input_text) if "分析师，为接下来" in instructions else _debate_turn(instructions, input_text)
        pause = float(os.getenv("AI_TRADER_FAKE_DEBATE_SECONDS", "0") or 0) / 8
        for end in range(len(text) // 8 or 1, len(text) + 1, len(text) // 8 or 1):
            await asyncio.sleep(pause)
            if on_text is not None:
                await on_text(text[:end])
        return _Result(text, 1800)

    async def complete_structured(self, *, instructions: str, input_text: str, schema: type, **_: Any) -> _Result:
        from ..research_debate import DebateVerdict, SearchPlan
        if schema is SearchPlan:
            # One query per listed gap: the product's name and the gap.
            name = re.search(r"品种：(\S+?)（", input_text)
            listed = re.search(r"缺口：(.+)", input_text)
            gaps = listed.group(1).split("、") if listed else []
            return _Result(SearchPlan.model_validate({"queries": [{"gap": gap, "query": f"{name.group(1) if name else ''} {gap}"}
                                                                  for gap in gaps if gap][:2]}), 120)
        if schema is DebateVerdict:
            # Browser checks of the 进行中 view: the judge can take a moment, like a real model.
            await asyncio.sleep(float(os.getenv("AI_TRADER_FAKE_DEBATE_SECONDS", "0") or 0))
            return _Result(_debate_verdict(input_text), 2600)
        payload = json.loads(input_text)
        if schema is TranslationBatch:
            # The fake marks translations instead of translating: the browser samples are Chinese,
            # so this only runs when a sample carries an English wire item.
            titles = [{"key": str(item["key"]), "title": f"〔译〕{str(item['title'])[:40]}"}
                      for item in payload.get("titles") or []]
            return _Result(TranslationBatch.model_validate({"translations": titles}), 60)
        if schema is ScreenBatch:
            verdicts = []
            for pair in payload["pairs"]:
                product = (re.search(r"（(\w+)）$", pair["product"]) or re.search(r"(\w+)$", pair["product"]))
                key = product.group(1).lower() if product else ""
                text = f"{pair['title']}{pair['text']}"
                found = next((verdict for words, owner, verdict in _VERDICTS
                              if owner == key and all(word in text for word in words)), None)
                verdicts.append({"key": pair["key"], "relevant": True, **found} if found else
                                {"key": pair["key"], "relevant": False, "dropReason": "无关宏观数据" if "央行" in text else "其他品种新闻"})
            return _Result(ScreenBatch.model_validate({"verdicts": verdicts}), 600)
        if schema is BriefDraft:
            rank = {"强": 3, "中": 2, "弱": 1}
            events = sorted(payload["events"], key=lambda event: (-rank.get(event["strength"], 0), -event["reports"]))
            top = [event for event in events if event["direction"] != "中性"][:3] or events[:3]
            score = sum((1 if event["direction"] == "偏多" else -1 if event["direction"] == "偏空" else 0)
                        * rank.get(event["strength"], 0) * event["reports"] for event in events)
            net = "偏多" if score > 1 else "偏空" if score < -1 else "中性"
            labels = "、".join(event["eventLabel"] for event in top[:2])
            take = f"新闻面{net}：{labels}是今天的主线，{top[0]['transmission']}。" if top else "今天还没有重要消息。"
            points = [{"text": f"{event['eventLabel']}：{event['transmission']}"[:40], "direction": event["direction"],
                       "eventLabel": event["eventLabel"]} for event in top]
            watch = [f"{item['time']} {item['event']}"[:30] for item in payload.get("calendar", [])[:2]]
            return _Result(BriefDraft.model_validate({"net": net, "take": take[:80], "points": points, "watch": watch}), 1500)
        if schema is DeskJudgment:
            return _Result(_desk_judgment(payload), 2400)
        from ..strategy_rules import StrategyPlan
        from ..strategy_verdict import VerdictSentence
        if schema is StrategyPlan:
            from ..strategy_templates import build_plan, templates
            plan, _ = build_plan(templates()["donchian_turtle"], {}, timeframe=900)
            idea = str(payload.get("description") or "").strip()
            return _Result(plan.model_copy(update={"name": "前高突破追单", "description": idea or plan.description,
                                                   "questions": [], "clarifications": []}), 900)
        if schema is VerdictSentence:
            facts = [str(item) for item in payload.get("facts") or []]
            return _Result(VerdictSentence(text="".join(facts[:2])[:120]), 200)
        raise ValueError("fake_news_model_unknown_schema")


class FakeForeignSource:
    """The foreign entry's recorded source on the fake-data runtime: 10-07 21:51 (Beijing) COMEX gold as the
    调研 showed it (the time is today's), through the same Sina row parser as the real source."""

    name = "sina"

    def __init__(self) -> None:
        self.started = datetime.now(CHINA)

    def quotes(self, products: list[Any]) -> dict[str, dict[str, Any]]:
        day = self.started.strftime("%Y-%m-%d")
        return parse_sina(f'var hq_str_hf_GC="4120.870,,4120.5,4121.0,4190.0,4110.0,21:51:00,4187.000,4180.0,0,1,1,{day},纽约黄金,0";',
                          products)
