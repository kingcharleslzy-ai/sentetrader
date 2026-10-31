"""交割提醒结果缓存（2026-10-13 第二轮省 CPU）：缓存只换算法开销，不改任何一条提醒。

GOLDEN 是改前代码（main 4a16fff8）对同一矩阵算出的结果整体的 sha256：每种合约形状（商品、
股指现金交割、郑商所三位年月、目录字段年月、主连）× 18 个交割月 × 84 个“今天”（每周一天，9-20 至 10-31 逐日） × 四种日历
（无、完整、只覆盖前段、只覆盖后段）。改后逐字节相同才通过。"""
from __future__ import annotations

import hashlib
import json
from datetime import date, timedelta

from trader_runtime import delivery_rules
from trader_runtime.delivery_rules import delivery_notice

GOLDEN = "32544be6dd0c1e50510dcb975509e9b1a698da36222e29fad4795e08446a295d"


def _calendars():
    start = date(2026, 6, 1)
    holidays = ({date(2026, 10, d) for d in range(1, 9)}
                | {date(2027, 1, 1), date(2027, 2, 8), date(2027, 2, 9)})
    full = [start + timedelta(days=i) for i in range(400)]
    days = [d.isoformat() for d in full if d.weekday() < 5 and d not in holidays]
    return [("none", None), ("full", days), ("short", days[:120]), ("late", days[60:])]


def _items():
    for year, month in [(2026, m) for m in range(1, 13)] + [(2027, m) for m in range(1, 7)]:
        yy = f"{year % 100:02d}{month:02d}"
        yield {"symbol": f"SHFE.rb{yy}", "instrumentId": f"rb{yy}", "exchangeId": "SHFE", "productId": "rb"}
        yield {"symbol": f"CFFEX.IF{yy}", "instrumentId": f"IF{yy}", "exchangeId": "CFFEX", "productId": "IF"}
        yield {"symbol": f"CZCE.CF{yy[1:]}", "instrumentId": f"CF{yy[1:]}", "exchangeId": "CZCE", "productId": "CF"}
        yield {"symbol": f"DCE.m{yy}", "exchangeId": "DCE", "productId": "m",
               "deliveryYear": year, "deliveryMonth": month}
    yield {"symbol": "KQ.m@SHFE.rb", "instrumentId": "KQ.m@SHFE.rb", "exchangeId": "KQ", "productId": "rb"}


def _matrix() -> list:
    out = []
    items = list(_items())
    for name, days in _calendars():
        for offset in sorted(set(range(0, 330, 7)) | set(range(81, 123))):  # 每周一天，国庆前后逐日
            today = date(2026, 7, 1) + timedelta(days=offset)
            for item in items:
                out.append([name, today.isoformat(), item["symbol"], delivery_notice(item, today, days)])
    return out


def test_results_match_the_uncached_code_byte_for_byte():
    first = json.dumps(_matrix(), ensure_ascii=False)
    second = json.dumps(_matrix(), ensure_ascii=False)  # 第二遍全部命中缓存
    assert hashlib.sha256(first.encode()).hexdigest() == GOLDEN
    assert second == first


def test_cache_follows_the_day_and_the_calendar():
    rb = {"symbol": "SHFE.rb2611", "instrumentId": "rb2611", "exchangeId": "SHFE", "productId": "rb"}
    weekdays = [d.isoformat() for d in (date(2026, 10, 1) + timedelta(days=i) for i in range(60)) if d.weekday() < 5]
    before = delivery_notice(rb, date(2026, 10, 19), weekdays)
    assert before["tradingDaysLeft"] == 10
    # 跨日：换个“今天”就是另一把键
    assert delivery_notice(rb, date(2026, 10, 20), weekdays)["tradingDaysLeft"] == 9
    # 日历更新（sidecar 送来新的节假日）：新日历立刻生效，不吃旧结果
    holiday = [day for day in weekdays if day != "2026-10-21"]
    assert delivery_notice(rb, date(2026, 10, 19), holiday)["tradingDaysLeft"] == 9
    # 同一份日历以列表或元组传入，结果相同
    assert delivery_notice(rb, date(2026, 10, 19), tuple(weekdays)) == before


def test_callers_get_their_own_copy():
    rb = {"symbol": "SHFE.rb2611", "instrumentId": "rb2611", "exchangeId": "SHFE", "productId": "rb"}
    first = delivery_notice(rb, date(2026, 10, 19))
    first["text"] = "被调用方改坏"
    assert delivery_notice(rb, date(2026, 10, 19))["text"] != "被调用方改坏"


def test_memo_is_bounded(monkeypatch):
    monkeypatch.setattr(delivery_rules, "MEMO_LIMIT", 20)
    rb = {"symbol": "SHFE.rb2611", "instrumentId": "rb2611", "exchangeId": "SHFE", "productId": "rb"}
    calendar = delivery_rules._calendar(())
    for offset in range(50):
        delivery_notice(rb, date(2026, 9, 1) + timedelta(days=offset), ())
    assert 0 < len(calendar.memo) <= 20
