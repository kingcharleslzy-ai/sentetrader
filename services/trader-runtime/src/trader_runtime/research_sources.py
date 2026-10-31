"""Public research sources read over plain HTTP, each with its own timeout.

Eastmoney's news search (per product keyword), the National Bureau of
Statistics' release list and its ten-day production-material price table,
CFTC's weekly Commitments of Traders, and — only when their free keys are
configured — FRED and EIA series. None of them needs pandas or AKShare, so the
desktop Runtime reads them too. Every call raises `SourceError` with a stable
code; callers decide how a missing source is shown, never guess a value.

Terms (checked 2026-09-28): NBS, CFTC, FRED and EIA allow reuse with the source
named (FRED also asks for its not-endorsed notice). Eastmoney requires written
consent for reproduction; the owner accepted showing its headlines with the
media name and original link (2026-09-28), and the summaries are only model
input.
"""
from __future__ import annotations

import asyncio
import html
import json
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from .host_throttle import host_of, throttle_for
from .secret_values import secret_value

CHINA = timezone(timedelta(hours=8))
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36"
TIMEOUT_SECONDS = 8.0
EASTMONEY_SEARCH = "https://search-api-web.eastmoney.com/search/jsonp"
EASTMONEY_DATACENTER = "https://datacenter-web.eastmoney.com/api/data/v1/get"
NBS_RELEASES = "https://www.stats.gov.cn/sj/zxfb/"
CFTC_LEGACY = "https://publicreporting.cftc.gov/resource/6dca-aqww.json"
FRED_OBSERVATIONS = "https://api.stlouisfed.org/fred/series/observations"
EIA_WEEKLY_STOCKS = "https://api.eia.gov/v2/petroleum/stoc/wstk/data/"
NBS_PRICE_TITLE = "流通领域重要生产资料市场价格变动情况"
FRED_NOTICE = "This product uses the FRED® API but is not endorsed or certified by the Federal Reserve Bank of St. Louis."

# Product → the row name in NBS's ten-day price table (the text before the spec in brackets).
NBS_PRICE_ROWS = {
    "rb": "螺纹钢", "wr": "线材", "hc": "热轧普通板卷", "cu": "电解铜", "al": "铝锭", "pb": "铅锭",
    "zn": "锌锭", "ma": "甲醇", "bz": "纯苯", "l": "聚乙烯", "pp": "聚丙烯", "br": "顺丁胶",
    "pg": "液化石油气", "jm": "焦煤", "j": "焦炭", "fg": "浮法平板玻璃", "ps": "多晶硅", "c": "玉米",
    "cf": "棉花", "lh": "生猪", "a": "大豆", "m": "豆粕", "pk": "花生", "sr": "白糖", "ur": "尿素",
    "ru": "天然橡胶", "sp": "纸浆", "sh": "烧碱", "wh": "小麦",
}
# Product → CFTC contract market code of the related US futures (legacy, futures only).
CFTC_MARKETS = {
    "cu": ("085692", "COMEX 铜"), "au": ("088691", "COMEX 黄金"), "ag": ("084691", "COMEX 白银"),
    "sc": ("067651", "NYMEX WTI 原油"), "a": ("005602", "CBOT 大豆"), "b": ("005602", "CBOT 大豆"),
    "m": ("026603", "CBOT 豆粕"), "y": ("007601", "CBOT 豆油"), "c": ("002602", "CBOT 玉米"),
    "cf": ("033661", "ICE 棉花"), "sr": ("080732", "ICE 11号糖"),
}
# FRED series shown for every product: the broad dollar index and the 10-year Treasury yield.
FRED_SERIES = (("DTWEXBGS", "美元指数（广义，FRED）", ""), ("DGS10", "美国10年期国债收益率（FRED）", "%"))
# Energy products also get EIA's weekly US commercial crude stocks.
EIA_PRODUCTS = frozenset({"sc", "fu", "lu", "bu", "pg"})
# China monthly macro series from Eastmoney's data center (NBS / PBoC figures): key → (report, columns).
MACRO_SERIES = {
    "pmi": ("RPT_ECONOMY_PMI", "REPORT_DATE,TIME,MAKE_INDEX,NMAKE_INDEX"),
    "cpi": ("RPT_ECONOMY_CPI", "REPORT_DATE,TIME,NATIONAL_SAME,NATIONAL_SEQUENTIAL"),
    "ppi": ("RPT_ECONOMY_PPI", "REPORT_DATE,TIME,BASE_SAME"),
    "m2": ("RPT_ECONOMY_CURRENCY_SUPPLY", "REPORT_DATE,TIME,BASIC_CURRENCY_SAME,CURRENCY_SAME"),
    "fai": ("RPT_ECONOMY_ASSET_INVEST", "REPORT_DATE,TIME,BASE_SAME,BASE_ACCUMULATE"),
}
# SHFE's weekly warehouse stock report (Fridays): the real inventory, not the registered receipts.
SHFE_WEEKLY_URL = "https://www.shfe.com.cn/data/tradedata/future/stockdata/weeklystock_{date}/ZH/all.html"
SHFE_WEEKLY_NAMES = {
    "cu": "铜", "al": "铝", "zn": "锌", "pb": "铅", "ni": "镍", "sn": "锡", "ao": "氧化铝", "ad": "铸造铝合金",
    "ru": "天然橡胶", "br": "丁二烯橡胶", "sp": "纸浆", "op": "胶版印刷纸", "fu": "燃料油", "bu": "石油沥青",
    "au": "黄金", "ag": "白银", "rb": "螺纹钢", "wr": "线材", "hc": "热轧卷板", "ss": "不锈钢",
    "sc": "中质含硫原油", "lu": "低硫燃料油", "nr": "20号胶", "bc": "铜(BC)",
}
# News that names the product only in passing: company research visits, earnings calls, stock lists.
NEWS_NOISE = re.compile(r"调研快报|业绩说明会|股份有限公司|龙虎榜|融资融券|机构调研|股东|增持|减持")
NEWS_MIN_TITLE_MATCHES = 3
ORG_NOTE_DAYS = 3
ORG_NOTE_LIMIT = 4
ORG_PASSAGE_CHARS = 400
# 机构观点（2026-10-13）：东方财富期货频道的两个栏目一次读全部品种（全品种共用一份，30 分钟一刷），再按品种
# 挑：516“期货机构观点”——二十多家期货公司的早评/日评（标题“板块 | 品种：观点”）、期货日报、东方财富研究中心
# 的期市收评；513“国内期货”——只取其中期货日报的市场分析。只取标题、摘要、链接、发布时间与机构名，不抓正文。
EASTMONEY_COLUMNS = "https://np-listapi.eastmoney.com/comm/web/getNewsByColumns"
ORG_FEED_COLUMNS = (516, 513)
ORG_FEED_PAGE = 100
# 一个品种的机构观点合计最多这么多条（每家机构一条，按搜索到的与栏目里的合在一起取最新）。
ORG_NOTE_TOTAL = 6


class SourceError(RuntimeError):
    """A public source failed; the code is stable and safe to show."""


def _client(transport: httpx.AsyncBaseTransport | None, proxy: str | None = None) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=TIMEOUT_SECONDS, transport=transport, proxy=proxy, follow_redirects=True,
                             headers={"User-Agent": USER_AGENT})


# One kept-alive client per event loop and proxy for the paced (real) hosts: Eastmoney sees one
# session, not a fresh connection per request.
_SHARED_CLIENTS: dict[tuple[int, str | None], httpx.AsyncClient] = {}


def _shared_client(proxy: str | None) -> httpx.AsyncClient:
    key = (id(asyncio.get_running_loop()), proxy)
    client = _SHARED_CLIENTS.get(key)
    if client is None or client.is_closed:
        client = _SHARED_CLIENTS[key] = _client(None, proxy)
    return client


async def _get(transport: httpx.AsyncBaseTransport | None, url: str, source: str, *, proxy: str | None = None,
               **kwargs: Any) -> httpx.Response:
    """One GET. A real host (no injected transport) is paced through `host_throttle` — every
    Eastmoney request queues on the Eastmoney lane, ≥ 1 s apart — and reuses the shared session."""
    try:
        if transport is None:
            async with throttle_for(url).async_turn(host_of(url)):
                response = await _shared_client(proxy).get(url, **kwargs)
        else:
            async with _client(transport, proxy) as client:
                response = await client.get(url, **kwargs)
    except httpx.TimeoutException as error:
        raise SourceError(f"{source}_timeout") from error
    except httpx.HTTPError as error:
        raise SourceError(f"{source}_unreachable") from error
    if response.status_code != 200:
        raise SourceError(f"{source}_http_{response.status_code}")
    return response


def _plain(value: Any) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", str(value or "")))).strip()


def parse_eastmoney_news(text: str, keyword: str, now: datetime, *, days: int = 7, limit: int = 8) -> list[dict[str, Any]]:
    """Newest first, published in the last `days`, one per title, no company/stock noise. Items naming the
    product in the title come first; items naming it only in the text fill in when titles give fewer than three."""
    aliases = product_aliases(keyword)
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in _search_items(text):
        title, summary = _plain(item.get("title")), _plain(item.get("content"))
        if not title or title in seen or NEWS_NOISE.search(title) or not any(alias in title + summary for alias in aliases):
            continue
        try:
            published = datetime.strptime(str(item.get("date", "")), "%Y-%m-%d %H:%M:%S").replace(tzinfo=CHINA)
        except ValueError:
            continue
        if not timedelta(0) <= now - published <= timedelta(days=days):
            continue
        code = str(item.get("code") or "")
        link = str(item.get("url") or (f"https://finance.eastmoney.com/a/{code}.html" if code else ""))
        seen.add(title)
        entries.append({"title": title, "summary": summary[:200], "media": _plain(item.get("mediaName")),
                        "publishDate": published.isoformat(timespec="seconds"),
                        "link": link.replace("http://", "https://", 1), "source": "eastmoney",
                        "inTitle": any(alias in title for alias in aliases)})
    titled = [entry for entry in entries if entry["inTitle"]]
    chosen = titled if len(titled) >= NEWS_MIN_TITLE_MATCHES else entries
    chosen.sort(key=lambda entry: entry["publishDate"], reverse=True)
    return [{key: value for key, value in entry.items() if key != "inTitle"} for entry in chosen[:limit]]


def product_aliases(name: str) -> list[str]:
    """How futures notes name a product: 螺纹钢 → 螺纹, 沪铜 → 铜, 沪金 → 黄金."""
    aliases = [name]
    if name in {"沪金", "沪银"}:
        aliases.append("黄金" if name == "沪金" else "白银")
    elif name.startswith("沪") and len(name) > 1:
        aliases.append(name[1:])
    elif len(name) >= 3 and name[-1] in "钢石":
        aliases.append(name[:-1])
    return aliases


def _search_items(text: str) -> list[dict[str, Any]]:
    try:
        payload = json.loads(text[text.index("(") + 1:text.rindex(")")])
        items = payload["result"]["cmsArticleWebOld"]
    except (ValueError, KeyError, TypeError) as error:
        raise SourceError("eastmoney_response_invalid") from error
    return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []


def parse_org_notes(text: str, aliases: list[str], now: datetime, *, days: int = ORG_NOTE_DAYS,
                    limit: int = ORG_NOTE_LIMIT) -> list[dict[str, Any]]:
    """Futures companies' notes naming the product: the newest per institution."""
    notes: list[dict[str, Any]] = []
    for item in _search_items(text):
        media, title, summary = _plain(item.get("mediaName")), _plain(item.get("title")), _plain(item.get("content"))
        if "期货" not in media or any(note["media"] == media for note in notes):
            continue
        if not any(alias in title + summary for alias in aliases):
            continue
        try:
            published = datetime.strptime(str(item.get("date", "")), "%Y-%m-%d %H:%M:%S").replace(tzinfo=CHINA)
        except ValueError:
            continue
        if not timedelta(0) <= now - published <= timedelta(days=days):
            continue
        code = str(item.get("code") or "")
        link = str(item.get("url") or (f"https://finance.eastmoney.com/a/{code}.html" if code else ""))
        notes.append({"title": title, "summary": summary[:200], "media": media, "link": link.replace("http://", "https://", 1),
                      "publishDate": published.isoformat(timespec="seconds"), "source": "eastmoney"})
        if len(notes) == limit:
            break
    return notes


def _feed_keeps(column: int, media: str, title: str) -> bool:
    if column == 513:
        return media == "期货日报"
    return ("期货" in media and "东方财富" not in media) or (media == "东方财富研究中心" and title.startswith("期市收评"))


def parse_org_feed(text: str, column: int, now: datetime, *, days: int = ORG_NOTE_DAYS) -> list[dict[str, Any]]:
    """One Eastmoney futures column (getNewsByColumns JSON) as institution notes of the last `days`: title,
    the list summary, institution (mediaName), link and time — every product at once."""
    try:
        payload = json.loads(text)
        items = payload["data"]["list"]
    except (ValueError, KeyError, TypeError) as error:
        raise SourceError("eastmoney_response_invalid") from error
    notes: list[dict[str, Any]] = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        media, title, summary = _plain(item.get("mediaName")), _plain(item.get("title")), _plain(item.get("summary"))
        if not title or not _feed_keeps(column, media, title):
            continue
        try:
            published = datetime.strptime(str(item.get("showTime", "")), "%Y-%m-%d %H:%M:%S").replace(tzinfo=CHINA)
        except ValueError:
            continue
        if not timedelta(0) <= now - published <= timedelta(days=days):
            continue
        link = str(item.get("uniqueUrl") or item.get("url") or "")
        notes.append({"title": title, "summary": summary[:600], "media": media, "link": link.replace("http://", "https://", 1),
                      "publishDate": published.isoformat(timespec="seconds"), "source": "eastmoney"})
    return notes


def feed_notes(feed: list[dict[str, Any]], aliases: list[str], now: datetime, *, days: int = ORG_NOTE_DAYS,
               limit: int = ORG_NOTE_LIMIT) -> list[dict[str, Any]]:
    """The product's notes out of the shared institution feed, newest per institution. A note names the
    product in its title (“能化 | 甲醇：伊朗多套装置胀库停车”: the view is the part after the sector) or in its
    summary (a sector note covering several products: the passage from where the product is first named)."""
    notes: list[dict[str, Any]] = []
    for note in sorted(feed, key=lambda entry: entry["publishDate"], reverse=True):
        if any(kept["media"] == note["media"] for kept in notes):
            continue
        try:
            published = datetime.fromisoformat(note["publishDate"])
        except (KeyError, ValueError):
            continue
        if not timedelta(0) <= now - published <= timedelta(days=days):
            continue
        summary = note.get("summary") or ""
        hits = [index for alias in aliases if (index := summary.find(alias)) >= 0]
        passage = summary[min(hits):min(hits) + ORG_PASSAGE_CHARS].strip() if hits else ""
        if any(alias in note["title"] for alias in aliases):
            view = note["title"].rsplit("|", 1)[-1].strip()
            text = f"{view}。{passage}" if passage and not passage.startswith(view) else view
        elif passage:
            text = passage
        else:
            continue
        notes.append({**note, "summary": text})
        if len(notes) == limit:
            break
    return notes


def merge_notes(*groups: list[dict[str, Any]], limit: int = ORG_NOTE_TOTAL) -> list[dict[str, Any]]:
    """Notes from several sources: one per institution (its newest), one per link, newest first."""
    chosen: list[dict[str, Any]] = []
    for note in sorted((note for group in groups for note in group), key=lambda entry: entry["publishDate"], reverse=True):
        if any(kept["media"] == note["media"] or (note["link"] and kept["link"] == note["link"]) for kept in chosen):
            continue
        chosen.append(note)
        if len(chosen) == limit:
            break
    return chosen


def extract_passage(page: str, aliases: list[str], chars: int = ORG_PASSAGE_CHARS) -> str:
    """The article body from where the product is first named (notes cover several products)."""
    start = page.find('id="ContentBody"')
    if start < 0:
        return ""
    body = _plain(page[start:start + 40000].split(">", 1)[-1])
    body = re.split(r"责任编辑|郑重声明|文章来源|（来源", body)[0]
    hits = [index for alias in aliases if (index := body.find(alias)) >= 0]
    return body[min(hits):min(hits) + chars].strip() if hits else ""


def parse_shfe_weekly(page: str) -> dict[str, dict[str, Any]]:
    """Section name → this week's warehouse stock and its change, from the 总计 (else 合计) row. Metals list
    小计/期货 per week (the stock is 小计); the rest one figure per week."""
    sections: dict[str, dict[str, Any]] = {}
    current: str | None = None
    split = pending = False
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", page, re.S):
        cells = [_plain(cell) for cell in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.S)]
        if cells[:2] == ["小计", "期货"]:
            pending = True  # the column header precedes the section's name row
            continue
        if len(cells) == 2 and cells[1].startswith("单位"):
            current, split, pending = cells[0], pending, False
            sections[current] = {"unit": cells[1].split("：")[-1]}
            continue
        if current is None or not cells:
            continue
        if cells[0] in {"总计", "合计"} and ("total" not in sections[current] or cells[0] == "总计"):
            try:
                week, change = (float(cells[3]), float(cells[5])) if split else (float(cells[2]), float(cells[3]))
            except (IndexError, ValueError):
                continue
            sections[current].update(total=cells[0], stock=week, change=change)
    return {name: value for name, value in sections.items() if "stock" in value}


def shfe_weekly_row(sections: dict[str, dict[str, Any]], product: str) -> dict[str, Any] | None:
    name = SHFE_WEEKLY_NAMES.get(product)
    if name is None:
        return None
    return sections.get(name) or sections.get(f"{name}(仓库)")


INVENTORY_MENTION = re.compile(
    r"[^，。；！？\s]{0,14}(?:库存|社库|厂库)[^，。；！？]{0,20}?[\d.]+\s*(?:亿吨|万吨|吨|万桶|万张|万头|万箱|万重箱)"
    r"(?:，?[^，。；！？]{0,10}?(?:增加|减少|下降|上升|持平|去化|累库)[^，。；！？]{0,12})?")


def inventory_mentions(passage: str, aliases: list[str], limit: int = 2) -> list[str]:
    """Inventory figures a note quotes about the product (social, port, plant stocks from Mysteel and the
    like): sentences naming the product first, any other stock figure only when none does."""
    sentences = [sentence for sentence in re.split(r"[。；！？]", passage) if INVENTORY_MENTION.search(sentence)]
    named = [sentence for sentence in sentences if any(alias in sentence for alias in aliases)]
    found: list[str] = []
    for sentence in named or sentences[:1]:
        for match in INVENTORY_MENTION.finditer(sentence):
            text = match.group(0).strip("，, ")
            if text not in found:
                found.append(text)
            if len(found) == limit:
                return found
    return found


def parse_macro(key: str, payload: Any) -> dict[str, Any]:
    rows = (payload.get("result") or {}).get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
        raise SourceError("macro_response_invalid")
    row = rows[0]
    return {"key": key, "period": str(row.get("TIME") or ""), "date": str(row.get("REPORT_DATE") or "")[:10],
            **{name: row[name] for name in row if name not in {"TIME", "REPORT_DATE"}}}


def parse_nbs_releases(text: str) -> list[dict[str, str]]:
    """The latest-release list: title, date and absolute URL, in page order (newest first)."""
    releases: list[dict[str, str]] = []
    for href, title, date in re.findall(
            r"<a[^>]+href=\"(\./[^\"]+\.html)\"[^>]*title='([^']+)'[^>]*>.*?</a>.*?<span>\s*(\d{4}-\d{2}-\d{2})\s*</span>",
            text, re.S):
        url = NBS_RELEASES + href[2:]
        if not any(item["url"] == url for item in releases):
            releases.append({"title": html.unescape(title).strip(), "date": date, "url": url})
    return releases


def parse_nbs_price_row(text: str, row_name: str) -> dict[str, Any] | None:
    """One product's row in the ten-day price table (the first table; the page repeats it)."""
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", text, re.S):
        cells = [_plain(cell).replace(" ", "") for cell in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
        if len(cells) != 5 or cells[0].split("（")[0] != row_name:
            continue
        try:
            return {"name": cells[0], "unit": cells[1], "price": float(cells[2]),
                    "change": float(cells[3]), "changePercent": float(cells[4])}
        except ValueError:
            return None
    return None


def parse_cftc(rows: Any) -> dict[str, Any]:
    if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
        raise SourceError("cftc_response_invalid")
    row = rows[0]
    try:
        long, short = int(row["noncomm_positions_long_all"]), int(row["noncomm_positions_short_all"])
        change = int(row["change_in_noncomm_long_all"]) - int(row["change_in_noncomm_short_all"])
        date = str(row["report_date_as_yyyy_mm_dd"])[:10]
    except (KeyError, ValueError, TypeError) as error:
        raise SourceError("cftc_response_invalid") from error
    return {"netLong": long - short, "netChange": change, "long": long, "short": short, "date": date}


def parse_fred(payload: Any) -> dict[str, Any]:
    observations = payload.get("observations") if isinstance(payload, dict) else None
    values = [item for item in observations or [] if isinstance(item, dict) and item.get("value") not in (None, ".")]
    if len(values) < 2:
        raise SourceError("fred_response_invalid")
    try:
        latest, previous = float(values[0]["value"]), float(values[1]["value"])
    except (KeyError, ValueError) as error:
        raise SourceError("fred_response_invalid") from error
    return {"value": latest, "change": round(latest - previous, 4), "date": str(values[0].get("date", ""))}


def parse_eia(payload: Any) -> dict[str, Any]:
    rows = payload.get("response", {}).get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or len(rows) < 2:
        raise SourceError("eia_response_invalid")
    try:
        latest, previous = float(rows[0]["value"]), float(rows[1]["value"])
    except (KeyError, ValueError, TypeError) as error:
        raise SourceError("eia_response_invalid") from error
    return {"value": latest, "change": round(latest - previous, 1), "date": str(rows[0].get("period", "")),
            "unit": str(rows[0].get("units", ""))}


class PublicSources:
    """Async fetchers; `transport` is for tests, keys are read per call so a rotated secret applies."""

    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.transport = transport

    @staticmethod
    def fred_key() -> str:
        return secret_value("FRED_API_KEY").strip()

    @staticmethod
    def eia_key() -> str:
        return secret_value("EIA_API_KEY").strip()

    async def eastmoney_news(self, keyword: str, now: datetime) -> list[dict[str, Any]]:
        param = {"uid": "", "keyword": keyword, "type": ["cmsArticleWebOld"], "client": "web",
                 "clientType": "web", "clientVersion": "curr",
                 "param": {"cmsArticleWebOld": {"searchScope": "default", "sort": "time", "pageIndex": 1,
                                                "pageSize": 20, "preTag": "", "postTag": ""}}}
        response = await _get(self.transport, EASTMONEY_SEARCH, "eastmoney",
                              params={"cb": "cb", "param": json.dumps(param, ensure_ascii=False)},
                              headers={"Referer": "https://so.eastmoney.com/"})
        return parse_eastmoney_news(response.text, keyword, now)

    async def org_notes(self, keyword: str, now: datetime) -> list[dict[str, Any]]:
        """Futures companies' daily/weekly notes on the product, each with the passage about it."""
        aliases = product_aliases(keyword)
        param = {"uid": "", "keyword": keyword, "type": ["cmsArticleWebOld"], "client": "web",
                 "clientType": "web", "clientVersion": "curr",
                 "param": {"cmsArticleWebOld": {"searchScope": "default", "sort": "time", "pageIndex": 1,
                                                "pageSize": 50, "preTag": "", "postTag": ""}}}
        response = await _get(self.transport, EASTMONEY_SEARCH, "eastmoney",
                              params={"cb": "cb", "param": json.dumps(param, ensure_ascii=False)},
                              headers={"Referer": "https://so.eastmoney.com/"})
        notes = parse_org_notes(response.text, aliases, now)

        async def passage(note: dict[str, Any]) -> None:
            try:
                page = await _get(self.transport, note["link"], "eastmoney_article")
            except SourceError:
                return  # the search summary stays
            text = extract_passage(page.content.decode("utf-8", errors="replace"), aliases)
            if text:
                note["summary"] = text

        await asyncio.gather(*(passage(note) for note in notes if note["link"]))
        return notes

    async def org_feed(self, now: datetime) -> list[dict[str, Any]]:
        """The futures institutions' notes of the last days from Eastmoney's futures columns, every product
        at once (the desk picks a product's notes out with `feed_notes`). One column failing keeps the other."""
        notes: list[dict[str, Any]] = []
        errors: list[SourceError] = []
        for column in ORG_FEED_COLUMNS:
            try:
                response = await _get(self.transport, EASTMONEY_COLUMNS, "eastmoney", params={
                    "client": "web", "biz": "web_news_col", "column": str(column), "order": "1", "needInteractData": "0",
                    "page_index": "1", "page_size": str(ORG_FEED_PAGE), "req_trace": "ai-trader",
                    "fields": "code,showTime,title,mediaName,summary,url,uniqueUrl", "types": "1,20"},
                    headers={"Referer": "https://futures.eastmoney.com/"})
                notes += parse_org_feed(response.text, column, now)
            except SourceError as error:
                errors.append(error)
        if len(errors) == len(ORG_FEED_COLUMNS):
            raise errors[0]
        # A note filed under both columns is kept once.
        unique: dict[str, dict[str, Any]] = {}
        for note in notes:
            unique.setdefault(note["link"] or note["title"], note)
        return list(unique.values())

    async def shfe_weekly(self, now: datetime) -> tuple[str, dict[str, dict[str, Any]]]:
        """The latest weekly report. It comes out on the week's last trading day after the close — a Friday,
        or the Thursday before a Friday holiday (2026-09-24) — so weekdays are tried newest first."""
        local = now.astimezone(CHINA)
        day = local.date() if local.hour >= 16 else local.date() - timedelta(days=1)
        tried = 0
        while tried < 10:
            if day.weekday() < 5:
                tried += 1
                try:
                    response = await _get(self.transport, SHFE_WEEKLY_URL.format(date=day.strftime("%Y%m%d")), "shfe_weekly")
                except SourceError as error:
                    if str(error) != "shfe_weekly_http_404":
                        raise
                else:
                    sections = parse_shfe_weekly(response.content.decode("utf-8", errors="replace"))
                    if sections:
                        return day.isoformat(), sections
            day -= timedelta(days=1)
        raise SourceError("shfe_weekly_unavailable")

    async def macro(self) -> dict[str, dict[str, Any]]:
        """The latest month of each China macro series; a failing series is left out."""
        async def one(key: str, report: str, columns: str) -> tuple[str, dict[str, Any] | None]:
            try:
                response = await _get(self.transport, EASTMONEY_DATACENTER, "macro", params={
                    "reportName": report, "columns": columns, "sortColumns": "REPORT_DATE", "sortTypes": "-1",
                    "pageNumber": "1", "pageSize": "1", "source": "WEB", "client": "WEB"})
                return key, parse_macro(key, response.json())
            except (SourceError, ValueError):
                return key, None
        results = await asyncio.gather(*(one(key, *spec) for key, spec in MACRO_SERIES.items()))
        series = {key: value for key, value in results if value is not None}
        if not series:
            raise SourceError("macro_unavailable")
        return series

    async def nbs_releases(self) -> list[dict[str, str]]:
        response = await _get(self.transport, NBS_RELEASES, "nbs")
        releases = parse_nbs_releases(response.content.decode("utf-8", errors="replace"))
        if not releases:
            raise SourceError("nbs_response_invalid")
        return releases

    async def nbs_price_page(self, url: str) -> str:
        """The release page; one without a table row (an HTTP 200 with an empty or cut body) is a failed read,
        never a good page that replaces the last one (#788)."""
        text = (await _get(self.transport, url, "nbs")).content.decode("utf-8", errors="replace")
        if not re.search(r"<td[^>]*>", text):
            raise SourceError("nbs_page_empty")
        return text

    async def cftc(self, market_code: str) -> dict[str, Any]:
        # CFTC refuses the production host's mainland IP (HTTP 403); AI_TRADER_CFTC_PROXY
        # (socks5h://…) sends only this request through the overseas egress.
        proxy = os.getenv("AI_TRADER_CFTC_PROXY", "").strip() or None
        response = await _get(self.transport, CFTC_LEGACY, "cftc", proxy=proxy, params={
            "cftc_contract_market_code": market_code, "$order": "report_date_as_yyyy_mm_dd DESC", "$limit": "1"})
        return parse_cftc(response.json())

    async def fred(self, series: str) -> dict[str, Any]:
        key = self.fred_key()
        if not key:
            raise SourceError("fred_key_missing")
        response = await _get(self.transport, FRED_OBSERVATIONS, "fred", params={
            "series_id": series, "api_key": key, "file_type": "json", "sort_order": "desc", "limit": "10"})
        return parse_fred(response.json())

    async def eia_crude_stocks(self) -> dict[str, Any]:
        key = self.eia_key()
        if not key:
            raise SourceError("eia_key_missing")
        response = await _get(self.transport, EIA_WEEKLY_STOCKS, "eia", params={
            "api_key": key, "frequency": "weekly", "data[0]": "value", "facets[series][]": "WCESTUS1",
            "sort[0][column]": "period", "sort[0][direction]": "desc", "length": "2"})
        return parse_eia(response.json())
