"""3b 的程序统计（2026-09-30）：近 90 天已平仓交易算出的画像与经验条目。纯函数，不碰
数据库也不调模型——每个数字都由程序算出，文字只由下面的模板拼出，模板外不许出现数字。
规律不再成立时由 memory_consolidation 按同一 statKey 标失效，历史保留。"""
from __future__ import annotations

import math
import re
from datetime import datetime
from statistics import median, quantiles
from typing import Any
from zoneinfo import ZoneInfo

from .instruments import foreign_product_of, money_unit, symbol_currency

CHINA = ZoneInfo("Asia/Shanghai")
INVALID_RECOMPUTE_REASON = "规律不再成立（近 90 天重算）"
WINDOW_TEXT = "近 90 天"

# 成交来源（round_source 的 source 值）→ 分组说明里的标签；止损单不是用户决策，不分组。
SOURCE_LABELS = {"manual": "手动", "card": "AI 建议", "ai": "AI 自主", "strategy": "策略"}
GROUP_MIN_TRADES = 5  # 分组经验至少 5 笔
PROFILE_MIN_TRADES = 10  # 画像至少 10 笔
LOW_WIN_RATE, HIGH_WIN_RATE, MIN_GAP = 35.0, 65.0, 15.0  # 显著：胜率 ≤35 或 ≥65，且与总体差 ≥15 个百分点
MAX_PRODUCTS = 3
MIN_PRODUCT_SHARE = 0.20
MAX_ROUND_IDS = 20
BUSY_MIN_DAYS = 4  # 忙闲日对比至少要有 4 个有平仓的交易日
BUSY_MIN_GAP = 0.3  # 忙日每笔平均比闲日差出两者较大绝对值的 30% 以上才算


def _product(symbol: str, names: dict[str, str]) -> str:
    """品种名：外盘合约是品种目录的中文名（CME.GCEZ26 / CME.GCEH27 → COMEX黄金，外盘按品种聚合，不按月份拆）；
    国内目录里有就用中文名；已到期、目录里没有的合约用去掉交易所和月份的代码
    （"SHFE.rb2701" → "rb"），同一品种的不同月份不会被拆成几组。"""
    foreign = foreign_product_of(symbol)
    if foreign is not None:
        return foreign.name
    return names.get(symbol) or re.sub(r"\d+$", "", symbol.split(".")[-1])


def _pnl(trade: dict[str, Any]) -> float:
    value = trade.get("net")
    return float(value if value is not None else (trade.get("gross") or 0.0))


def _wins(trades: list[dict[str, Any]]) -> int:
    return sum(1 for trade in trades if _pnl(trade) > 0)


def _count(value: float) -> str:
    """中位数可能是 .5：整数就写成整数，否则保留一位小数。"""
    return str(int(value)) if value == int(value) else f"{value:.1f}".rstrip("0").rstrip(".")


def _average_text(trades: list[dict[str, Any]]) -> str:
    """平均每笔盈亏，按币种分开写（外盘美元子账户的成交与人民币的不相加）：只有人民币时“+120 元”，
    混着时“+120 元、+35 美元”。"""
    by_currency: dict[str, list[float]] = {}
    for trade in trades:
        by_currency.setdefault(symbol_currency(trade.get("symbol")), []).append(_pnl(trade))
    order = sorted(by_currency, key=lambda code: (code != "CNY", code))
    return "、".join(f"{round(sum(by_currency[code]) / len(by_currency[code])):+,} {money_unit(code)}" for code in order)


def _held_minutes(trades: list[dict[str, Any]]) -> int:
    minutes = [int(trade.get("holdingMinutes") or 0) for trade in trades]
    return round(median(minutes)) if minutes else 0


def _is_night(open_time_ns: int) -> bool:
    hour = datetime.fromtimestamp(open_time_ns / 1e9, CHINA).hour
    return hour >= 21 or hour < 3


# 时段 (所有者 2026-10-25)：按国内交易小节切，夜盘每小时一段；按开仓时刻（北京时间）归段。
TIME_BLOCKS = ("夜盘 21–22 点", "夜盘 22–23 点", "夜盘 23 点后", "9:00–10:15", "10:30–11:30", "13:30–15:00")
WEEKDAYS = ("周一", "周二", "周三", "周四", "周五")


def time_block(trade: dict[str, Any]) -> str | None:
    """A domestic round trip's 交易小节 by its open; foreign (CME) trades have none."""
    if foreign_product_of(str(trade["symbol"])) is not None:
        return None
    moment = datetime.fromtimestamp(int(trade["openTime"]) / 1e9, CHINA)
    minute = moment.hour * 60 + moment.minute
    if 21 * 60 <= minute < 22 * 60:
        return TIME_BLOCKS[0]
    if 22 * 60 <= minute < 23 * 60:
        return TIME_BLOCKS[1]
    if minute >= 23 * 60 or minute < 3 * 60:
        return TIME_BLOCKS[2]
    if minute < 10 * 60 + 15:
        return TIME_BLOCKS[3]
    if minute < 11 * 60 + 30:
        return TIME_BLOCKS[4]
    return TIME_BLOCKS[5]


def weekday(trade: dict[str, Any]) -> str | None:
    """周一…周五 by the trading day the round trip closed on (a night session belongs to the next one)."""
    index = datetime.fromisoformat(_close_day(trade)).weekday()
    return WEEKDAYS[index] if index < len(WEEKDAYS) else None


def _row(label: str, trades: list[dict[str, Any]]) -> dict[str, Any]:
    wins, total = _wins(trades), sum(map(_pnl, trades))
    return {"label": label, "trades": len(trades), "wins": wins, "winRate": round(wins / len(trades) * 100),
            "net": round(total, 2), "average": round(total / len(trades), 2)}


def review_breakdown(rounds: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """复盘页“按打法、时段、星期” (第 2 批后半)：每组笔数、赢几笔、胜率、合计与平均每笔（net，缺则 gross）。
    打法组含“未标打法”；时段只算国内合约；星期按平仓交易日。只列有成交的组，顺序固定。"""
    by_playbook: dict[str, list[dict[str, Any]]] = {}
    for trade in rounds:
        by_playbook.setdefault(str(trade.get("playbookName") or "未标打法"), []).append(trade)
    playbooks = sorted(by_playbook, key=lambda name: (name == "未标打法", -len(by_playbook[name]), name))
    blocks = {label: [t for t in rounds if time_block(t) == label] for label in TIME_BLOCKS}
    days = {label: [t for t in rounds if weekday(t) == label] for label in WEEKDAYS}
    return {"byPlaybook": [_row(name, by_playbook[name]) for name in playbooks],
            "byTimeBlock": [_row(label, trades) for label, trades in blocks.items() if trades],
            "byWeekday": [_row(label, trades) for label, trades in days.items() if trades]}


def _against_desk(trade: dict[str, Any]) -> bool | None:
    """True = 逆着当时的调研判断做，False = 顺着；震荡/不明/没有判断时 None。"""
    view = trade.get("deskView")
    stance = view.get("stance") if isinstance(view, dict) else None
    if stance not in ("偏多", "偏空"):
        return None
    return (stance == "偏多") != (trade["direction"] == "long")


def _entry(key: str, layer: str, text: str, trades: list[dict[str, Any]], *,
           wins: int | None = None, tags: list[str] | None = None) -> dict[str, Any]:
    return {"key": key, "layer": layer, "text": text, "n": len(trades),
            "wins": _wins(trades) if wins is None else wins,
            "roundIds": [trade["id"] for trade in trades][:MAX_ROUND_IDS], "tags": list(tags or [])}


def _lesson_entry(key: str, label: str, trades: list[dict[str, Any]], overall_rate: float,
                  tags: list[str] | None, out: list[dict[str, Any]]) -> None:
    """n ≥ 5 且显著（胜率 ≤35 或 ≥65，且与总体差 ≥15 个百分点）才产出。"""
    if len(trades) < GROUP_MIN_TRADES:
        return
    wins = _wins(trades)
    rate = wins / len(trades) * 100
    if not ((rate <= LOW_WIN_RATE or rate >= HIGH_WIN_RATE) and abs(rate - overall_rate) >= MIN_GAP):
        return
    text = f"{WINDOW_TEXT}{label} {len(trades)} 笔，赢 {wins} 笔（胜率 {round(rate)}%），平均每笔 {_average_text(trades)}"
    out.append(_entry(key, "lesson", text, trades, tags=tags))


def _close_day(trade: dict[str, Any]) -> str:
    """平仓所在交易日：复盘链路带 tradingDay（夜盘记到下一个交易日），没有时用平仓时刻的北京日期。"""
    day = trade.get("tradingDay")
    return str(day) if day else datetime.fromtimestamp(int(trade["closeTime"]) / 1e9, CHINA).date().isoformat()


def _busy_days_entry(trades: list[dict[str, Any]], out: list[dict[str, Any]]) -> None:
    """忙闲日对比，改写自 HKUDS/Vibe-Trading（MIT，41f333b6）`agent/src/tools/trade_journal_tool.py` 的 `_overtrading`：
    按平仓笔数把交易日分成最忙的四分之一和最闲的四分之一，比两边每笔平均盈亏。改动：只看人民币合约（外盘美元不相加）；
    两边各至少 5 笔；差距按两边平均的较大绝对值衡量（原版闲日平均为 0 时按 1 元算，几块钱就会触发）。"""
    by_day: dict[str, list[dict[str, Any]]] = {}
    for trade in trades:
        if symbol_currency(trade.get("symbol")) == "CNY":
            by_day.setdefault(_close_day(trade), []).append(trade)
    if len(by_day) < BUSY_MIN_DAYS:
        return
    quiet_cut, _, busy_cut = quantiles([len(day) for day in by_day.values()], n=4, method="inclusive")
    if busy_cut <= quiet_cut:
        return  # 每天笔数差不多，分不出忙闲
    busy = [trade for day in by_day.values() if len(day) >= busy_cut for trade in day]
    quiet = [trade for day in by_day.values() if len(day) <= quiet_cut for trade in day]
    if len(busy) < GROUP_MIN_TRADES or len(quiet) < GROUP_MIN_TRADES:
        return
    busy_average = sum(map(_pnl, busy)) / len(busy)
    quiet_average = sum(map(_pnl, quiet)) / len(quiet)
    base = max(abs(busy_average), abs(quiet_average))
    if busy_average >= quiet_average or not base or (quiet_average - busy_average) / base < BUSY_MIN_GAP:
        return
    text = (f"{WINDOW_TEXT}平仓多的日子（一天 ≥{math.ceil(busy_cut)} 笔）每笔平均 {round(busy_average):+,} 元，"
            f"少的日子（≤{math.floor(quiet_cut)} 笔）每笔平均 {round(quiet_average):+,} 元")
    out.append(_entry("stat:busy-days", "lesson", text, busy))


def trade_stats(rounds: list[dict[str, Any]], *, names: dict[str, str], now_ns: int,
                window_days: int = 90) -> list[dict[str, Any]]:
    """已平仓交易（review_account_trades + journal_fields 的产物）→ 画像/经验条目。
    只看 closeTime 在 (now - window_days, now] 内的交易；盈亏用 net，缺则 gross。"""
    window_ns = window_days * 86_400_000_000_000
    trades = [trade for trade in rounds
              if int(trade["closeTime"]) > now_ns - window_ns and int(trade["closeTime"]) <= now_ns]
    out: list[dict[str, Any]] = []
    if len(trades) >= PROFILE_MIN_TRADES:
        counts: dict[str, int] = {}
        for trade in trades:
            product = _product(trade["symbol"], names)
            counts[product] = counts.get(product, 0) + 1
        frequent = sorted(((product, count / len(trades)) for product, count in counts.items()
                           if count / len(trades) >= MIN_PRODUCT_SHARE), key=lambda item: (-item[1], item[0]))
        if frequent:
            listed = "、".join(f"{product}（占 {round(share * 100)}%）" for product, share in frequent[:MAX_PRODUCTS])
            out.append(_entry("stat:products", "profile", f"常做：{listed}", trades))
        held = _held_minutes(trades)
        style = (f"多为日内短线（持仓中位 {held} 分钟）" if held < 240
                 else f"多为隔夜波段（持仓中位 {max(1, round(held / 1440))} 天）")
        out.append(_entry("stat:holding", "profile", style, trades))
        out.append(_entry("stat:size", "profile", f"一般每笔 {_count(median([int(t['volume']) for t in trades]))} 手",
                          trades))
    if not trades:
        return out

    overall_rate = _wins(trades) / len(trades) * 100
    for source, label in SOURCE_LABELS.items():
        _lesson_entry(f"stat:source:{source}", f"{label}单",
                      [t for t in trades if t.get("source") == source], overall_rate, None, out)
    _lesson_entry("stat:desk:against", "逆着调研判断做的",
                  [t for t in trades if _against_desk(t) is True], overall_rate, None, out)
    _lesson_entry("stat:desk:with", "顺着调研判断做的",
                  [t for t in trades if _against_desk(t) is False], overall_rate, None, out)
    by_product: dict[str, list[dict[str, Any]]] = {}
    for trade in trades:
        by_product.setdefault(_product(trade["symbol"], names), []).append(trade)
    for product in sorted(by_product):
        _lesson_entry(f"stat:product:{product}", product, by_product[product], overall_rate, [product], out)
    # 日盘 / 夜盘是国内的时段；外盘（CME 一个交易日一整段）不进这两组。
    domestic = [t for t in trades if foreign_product_of(str(t["symbol"])) is None]
    _lesson_entry("stat:session:night", "夜盘",
                  [t for t in domestic if _is_night(int(t["openTime"]))], overall_rate, None, out)
    _lesson_entry("stat:session:day", "日盘",
                  [t for t in domestic if not _is_night(int(t["openTime"]))], overall_rate, None, out)
    # 打法本 (第 2 批后半)：按打法、交易小节、星期，同样的门槛（≥5 笔且明显偏离总体胜率）。
    for name in sorted({str(t["playbookName"]) for t in trades if t.get("playbookName")}):
        _lesson_entry(f"stat:playbook:{name}", f"打法「{name}」的单",
                      [t for t in trades if t.get("playbookName") == name], overall_rate, None, out)
    for label in TIME_BLOCKS:
        _lesson_entry(f"stat:block:{label}", f"{label}开的仓", [t for t in trades if time_block(t) == label],
                      overall_rate, None, out)
    for label in WEEKDAYS:
        _lesson_entry(f"stat:weekday:{label}", f"{label}平的仓", [t for t in trades if weekday(t) == label],
                      overall_rate, None, out)

    _busy_days_entry(trades, out)

    losers = [t for t in trades if _pnl(t) <= 0]
    gainers = [t for t in trades if _pnl(t) > 0]
    if len(losers) >= 3 and len(gainers) >= 3:
        held_loss, held_gain = _held_minutes(losers), _held_minutes(gainers)
        if held_gain > 0 and held_loss >= 2 * held_gain:
            text = (f"{WINDOW_TEXT}亏损单持仓中位 {held_loss} 分钟，"
                    f"是盈利单（{held_gain} 分钟）的 {held_loss / held_gain:.1f} 倍")
            out.append(_entry("stat:hold:losers", "lesson", text, losers, wins=0))
    return out
