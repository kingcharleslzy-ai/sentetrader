"""Research desk: public-source parsing, fact collection, anchored judgments and their shared lifecycle."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from trader_runtime.data_providers import FakeReadOnlyDataProvider
from trader_runtime.research_desk import (
    PLATFORM_USAGE_USER,
    SLOW_FAILURE_TTL_SECONDS,
    DeskJudgment,
    DeskPoint,
    ResearchDeskService,
    validate_judgment,
)
from trader_runtime.research_sources import (
    CHINA,
    PublicSources,
    SourceError,
    parse_cftc,
    parse_eastmoney_news,
    parse_nbs_price_row,
    parse_nbs_releases,
)

NOW = datetime(2026, 9, 28, 15, 0, tzinfo=CHINA)


def _jsonp(items: list[dict[str, Any]]) -> str:
    return "cb(" + json.dumps({"result": {"cmsArticleWebOld": items}}, ensure_ascii=False) + ")"


def test_eastmoney_news_keeps_recent_items_naming_the_product() -> None:
    text = _jsonp([
        {"date": "2026-09-28 09:12:53", "title": "螺纹钢：短期震荡", "content": "<em>钢厂</em>减产", "mediaName": "光大期货", "code": "1"},
        {"date": "2026-09-28 08:00:00", "title": "螺纹钢：短期震荡", "content": "重复标题", "mediaName": "别家", "code": "2"},
        {"date": "2026-09-27 18:30:00", "title": "新钢股份调研", "content": "与品种无关", "mediaName": "Choice", "code": "3"},
        {"date": "2026-09-10 10:00:00", "title": "螺纹钢旧闻", "content": "", "mediaName": "旧", "code": "4"},
        {"date": "日期坏了", "title": "螺纹钢无日期", "content": "", "mediaName": "x", "code": "5"},
        {"date": "2026-09-28 10:00:00", "title": "期货夜盘", "content": "螺纹钢收涨", "mediaName": "第一财经",
         "url": "http://futures.eastmoney.com/a/6.html"},
    ])
    entries = parse_eastmoney_news(text, "螺纹钢", NOW)
    assert [entry["title"] for entry in entries] == ["期货夜盘", "螺纹钢：短期震荡"]
    assert entries[0]["link"] == "https://futures.eastmoney.com/a/6.html"
    assert entries[1]["summary"] == "钢厂减产" and entries[1]["media"] == "光大期货"
    assert entries[1]["link"] == "https://finance.eastmoney.com/a/1.html"
    with pytest.raises(SourceError, match="eastmoney_response_invalid"):
        parse_eastmoney_news("not jsonp", "螺纹钢", NOW)


NBS_LIST = """
<li><a class="fl pc_1600" href="./202609/t20260928_1.html" target="_blank" title='2026年1—8月份全国规模以上工业企业利润增长15.7%'>x</a>
<a class="fl pchide" href="./202609/t20260928_1.html" title='2026年1—8月份全国规模以上工业企业利润增长15.7%'>x</a><span> 2026-09-28 </span></li>
<li><a href="./202609/t20260923_2.html" target="_blank" title='2026年9月中旬流通领域重要生产资料市场价格变动情况'>x</a><span>2026-09-24</span></li>
<li><a href="./202608/t20260801_3.html" target="_blank" title='2026年7月份居民消费价格上涨0.1%'>x</a><span>2026-08-01</span></li>
"""
NBS_PRICES = """<table><tr><td>产品名称</td><td>单位</td><td>本期价格（元）</td><td>比上期价格涨跌（元）</td><td>涨跌幅（%）</td></tr>
<tr><td>螺纹钢（Φ20mm，HRB400E）</td><td>吨</td><td>3165.1</td><td>-17.6</td><td>-0.6</td></tr>
<tr><td>电解铜（1#）</td><td>吨</td><td>108770.0</td><td>-1722.5</td><td>-1.6</td></tr></table>"""


def test_nbs_release_list_and_price_table() -> None:
    releases = parse_nbs_releases(NBS_LIST)
    assert [item["date"] for item in releases] == ["2026-09-28", "2026-09-24", "2026-08-01"]
    assert releases[0]["url"] == "https://www.stats.gov.cn/sj/zxfb/202609/t20260928_1.html"
    row = parse_nbs_price_row(NBS_PRICES, "螺纹钢")
    assert row == {"name": "螺纹钢（Φ20mm，HRB400E）", "unit": "吨", "price": 3165.1, "change": -17.6, "changePercent": -0.6}
    assert parse_nbs_price_row(NBS_PRICES, "焦炭") is None


def test_cftc_net_position_and_invalid_rows() -> None:
    row = {"noncomm_positions_long_all": "129398", "noncomm_positions_short_all": "38876",
           "change_in_noncomm_long_all": "12233", "change_in_noncomm_short_all": "-3155",
           "report_date_as_yyyy_mm_dd": "2026-09-22T00:00:00.000"}
    assert parse_cftc([row]) == {"netLong": 90522, "netChange": 15388, "long": 129398, "short": 38876, "date": "2026-09-22"}
    with pytest.raises(SourceError, match="cftc_response_invalid"):
        parse_cftc([])


async def test_keyed_sources_skip_without_a_key_and_send_it_when_set(monkeypatch) -> None:
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    monkeypatch.delenv("FRED_API_KEY_FILE", raising=False)
    seen: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url)
        return httpx.Response(200, json={"response": {"data": [
            {"period": "2026-09-19", "value": "415000", "units": "MBBL"}, {"period": "2026-09-12", "value": "417500"}]}})

    sources = PublicSources(transport=httpx.MockTransport(handler))
    with pytest.raises(SourceError, match="fred_key_missing"):
        await sources.fred("DGS10")
    assert seen == []
    monkeypatch.setenv("EIA_API_KEY", "test-eia-key")
    stocks = await sources.eia_crude_stocks()
    assert stocks == {"value": 415000.0, "change": -2500.0, "date": "2026-09-19", "unit": "MBBL"}
    assert seen[0].params["api_key"] == "test-eia-key" and seen[0].params["facets[series][]"] == "WCESTUS1"


async def test_only_the_cftc_request_uses_the_configured_proxy(monkeypatch) -> None:
    monkeypatch.setenv("AI_TRADER_CFTC_PROXY", "socks5h://172.17.0.1:1080")
    proxies: list[str | None] = []
    real = httpx.AsyncClient

    def client(*args: Any, proxy: str | None = None, **kwargs: Any) -> httpx.AsyncClient:
        proxies.append(proxy)
        return real(*args, **kwargs)

    row = {"noncomm_positions_long_all": "2", "noncomm_positions_short_all": "1", "change_in_noncomm_long_all": "0",
           "change_in_noncomm_short_all": "0", "report_date_as_yyyy_mm_dd": "2026-09-22T00:00:00.000"}
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=[row]) if "cftc" in request.url.host
                                    else httpx.Response(200, text=NBS_LIST))
    monkeypatch.setattr("trader_runtime.research_sources.httpx.AsyncClient", client)
    sources = PublicSources(transport=transport)
    assert (await sources.cftc("085692"))["netLong"] == 1
    await sources.nbs_releases()
    assert proxies == ["socks5h://172.17.0.1:1080", None]


def test_aliases_notes_and_passages() -> None:
    from trader_runtime.research_sources import extract_passage, parse_org_notes, product_aliases
    assert product_aliases("螺纹钢") == ["螺纹钢", "螺纹"] and product_aliases("沪铜") == ["沪铜", "铜"]
    assert product_aliases("沪金") == ["沪金", "黄金"] and product_aliases("豆粕") == ["豆粕"]
    text = _jsonp([
        {"date": "2026-09-28 08:56:00", "title": "黑色系 | 双焦偏弱", "content": "螺纹热卷震荡", "mediaName": "铜冠金源期货", "code": "9"},
        {"date": "2026-09-28 08:50:00", "title": "螺纹：旧的一篇", "content": "", "mediaName": "铜冠金源期货", "code": "8"},
        {"date": "2026-09-28 08:40:00", "title": "期市收评", "content": "螺纹钢收跌", "mediaName": "东方财富研究中心", "code": "7"},
        {"date": "2026-09-28 08:30:00", "title": "有色 | 铜价震荡", "content": "沪铜", "mediaName": "正信期货", "code": "6"},
        {"date": "2026-09-20 08:30:00", "title": "螺纹：太旧", "content": "", "mediaName": "南华期货", "code": "5"},
    ])
    notes = parse_org_notes(text, ["螺纹钢", "螺纹"], NOW)
    # One per institution, futures companies only, product named, within three days.
    assert [(note["media"], note["link"]) for note in notes] == [("铜冠金源期货", "https://finance.eastmoney.com/a/9.html")]
    page = '<div id="ContentBody"><p>锰硅：震荡。</p><p>螺纹钢：昨日成交回落。</p><p>责任编辑：某人</p></div>'
    assert extract_passage(page, ["螺纹钢", "螺纹"]) == "螺纹钢：昨日成交回落。"
    assert extract_passage(page, ["沪铜", "铜"]) == "" and extract_passage("<p>无正文</p>", ["螺纹"]) == ""


def test_macro_rows_and_facts() -> None:
    from trader_runtime.research_desk import macro_facts
    from trader_runtime.research_sources import parse_macro
    row = parse_macro("cpi", {"result": {"data": [{"REPORT_DATE": "2026-08-01 00:00:00", "TIME": "2026年08月份",
                                                   "NATIONAL_SAME": 0.8, "NATIONAL_SEQUENTIAL": 0.4}]}})
    assert row == {"key": "cpi", "period": "2026年08月份", "date": "2026-08-01", "NATIONAL_SAME": 0.8, "NATIONAL_SEQUENTIAL": 0.4}
    with pytest.raises(SourceError, match="macro_response_invalid"):
        parse_macro("cpi", {"result": None})
    fai = {"period": "2026年08月份", "date": "2026-08-01", "BASE_SAME": -13.51}
    assert [fact["id"] for fact in macro_facts({"cpi": row, "fai": fai}, "rb")] == ["E.cpi", "E.fai"]
    assert [fact["id"] for fact in macro_facts({"cpi": row, "fai": fai}, "cu")] == ["E.cpi"]
    assert macro_facts({"cpi": row}, "m")[0]["value"] == "同比 +0.8%，环比 +0.4%"


# -- judgment validation

FACTS = [
    {"id": "M.price", "section": "market", "kind": "data", "label": "主力 RB2610 最新价", "value": "3106，日涨跌 -0.42%",
     "source": "快期行情", "asOf": "2026-09-28"},
    {"id": "R.i", "section": "related", "kind": "data", "label": "铁矿石 I2609（上游原料）", "value": "日涨跌 +0.8%",
     "source": "快期行情", "asOf": "2026-09-28"},
    {"id": "N.abc", "section": "report", "kind": "report", "label": "新华财经", "value": "螺纹钢社库连续第四周减少",
     "detail": "全国螺纹钢产量165.35万吨，较上周减少8.72万吨", "source": "eastmoney", "asOf": "2026-09-24", "url": "https://e.com/1"},
]


def test_judgment_keeps_only_anchored_points_and_copies_their_facts() -> None:
    judgment = DeskJudgment(stance="震荡", confidence="低", headline="主力日跌 0.42%，原料偏强", headlineBasis=["M.price", "R.i"], points=[
        DeskPoint(text="主力日涨跌 -0.42%，弱于铁矿石的 +0.8%", basis=["M.price", "R.i"], quote="日涨跌 +0.8%"),
        DeskPoint(text="成材跌幅达 5%", basis=["M.price"], quote="3106"),                 # 5 is in no cited fact
        DeskPoint(text="据报道社库连续四周减少", basis=[]),                    # cites nothing
        DeskPoint(text="库存下降", basis=["D.inventory"]),                    # unknown id
        DeskPoint(text="建议逢低买入", basis=["M.price"], quote="3106"),                     # order advice
        DeskPoint(text="据报道产量165.35万吨、减少8.72万吨", basis=["N.abc"], kind="推测", quote="产量165.35万吨"),  # numbers from the summary
        DeskPoint(text="据报道社库下降", basis=["N.abc"], quote="社库连续第五周减少"),  # quote not in the fact
        DeskPoint(text="据报道社库下降", basis=["N.abc"], quote="社库"),  # quote too short
    ], risks=[DeskPoint(text="原料 +0.8% 或抬升成本", basis=["R.i"], quote="铁矿石 I2609")], watch=[DeskPoint(text="下周社库", basis=["nope"])])
    dropped: dict[str, int] = {}
    result = validate_judgment(judgment, FACTS, dropped)
    assert [point["text"] for point in result["points"]] == ["主力日涨跌 -0.42%，弱于铁矿石的 +0.8%", "据报道产量165.35万吨、减少8.72万吨"]
    assert result["points"][1]["kind"] == "推测" and result["headlineBasis"] == ["M.price", "R.i"]
    assert [point["grounds"] for point in result["points"]] == ["data", "report"]
    assert dropped == {"number_mismatch": 1, "no_basis": 1, "unknown_basis": 2, "advice": 1, "quote_mismatch": 2}
    assert result["points"][0]["quote"] == "日涨跌 +0.8%"
    assert len(result["risks"]) == 1 and result["watch"] == []
    assert set(result["basis"]) == {"M.price", "R.i", "N.abc"}
    assert result["basis"]["N.abc"]["kind"] == "report" and result["basis"]["N.abc"]["url"] == "https://e.com/1"


def test_judgment_without_anchored_points_is_rejected_and_bad_headlines_are_neutralised() -> None:
    no_points = DeskJudgment(stance="偏多", confidence="高", headline="偏多", headlineBasis=["R.i"],
                             points=[DeskPoint(text="会涨", basis=[])])
    with pytest.raises(ValueError, match="judgment_unanchored"):
        validate_judgment(no_points, FACTS)
    # An invented or uncited headline gives way to a neutral one; the anchored points stay.
    dropped: dict[str, int] = {}
    invented = DeskJudgment(stance="偏多", confidence="高", headline="目标 3300", headlineBasis=["M.price"],
                            points=[DeskPoint(text="原料偏强", basis=["R.i"], quote="上游原料")])
    kept = validate_judgment(invented, FACTS, dropped)
    # The neutral headline names the computed confidence (one direct reason: 中), not the model's 高.
    assert kept["headline"] == "倾向偏多，信心中" and kept["headlineBasis"] == ["R.i"]
    uncited = DeskJudgment(stance="偏多", confidence="高", headline="高库存压制", points=[DeskPoint(text="原料偏强", basis=["R.i"], quote="上游原料")])
    assert validate_judgment(uncited, FACTS, dropped)["headline"] == "倾向偏多，信心中"
    assert dropped == {"headline_number_mismatch": 1, "headline_no_basis": 1}


# -- confidence computed from the judgment's evidence (owner 2026-10-12)

CONF_FACTS = [
    {"id": "M.price", "section": "market", "kind": "data", "label": "主力 RB2610 最新价", "value": "3106，日涨跌 +1.2%",
     "source": "快期行情", "asOf": "2026-10-12"},
    {"id": "M.trend", "section": "market", "kind": "data", "label": "近20个交易日", "value": "价格上升，均线向上",
     "source": "快期行情", "asOf": "2026-10-12"},
    {"id": "D.spot_basis", "section": "fundamental", "kind": "data", "label": "现货与基差", "value": "现货升水，基差走强",
     "source": "100ppi", "asOf": "2026-10-12"},
    {"id": "D.warehouse_receipt", "section": "fundamental", "kind": "data", "label": "仓单", "value": "仓单连续增加",
     "source": "AKShare", "asOf": "2026-10-12"},
    {"id": "N.abc", "section": "report", "kind": "report", "label": "新华财经", "value": "螺纹钢社库连续第四周减少",
     "source": "eastmoney", "asOf": "2026-10-11"},
]
QUOTES = {"M.price": "主力 RB2610", "M.trend": "价格上升", "D.spot_basis": "基差走强", "D.warehouse_receipt": "仓单连续增加",
          "N.abc": "社库连续第四周"}


def _reason(fact_id: str, direction: str, kind: str = "推断") -> DeskPoint:
    return DeskPoint(text=f"{QUOTES[fact_id]}的理由", basis=[fact_id], quote=QUOTES[fact_id], direction=direction, kind=kind)  # type: ignore[arg-type]


def _rated(stance: str, points: list[DeskPoint], risks: list[DeskPoint] = (), facts: list[dict[str, Any]] = CONF_FACTS,  # type: ignore[assignment]
           model: str = "中") -> dict[str, Any]:
    value = DeskJudgment(stance=stance, confidence=model, headline="主力 RB2610 走势", headlineBasis=["M.price"],  # type: ignore[arg-type]
                         points=points, risks=list(risks))
    return validate_judgment(value, facts)


def test_two_kinds_of_fresh_data_agreeing_with_the_stance_rate_high_and_keep_the_models_level() -> None:
    rated = _rated("偏多", [_reason("M.trend", "偏多"), _reason("D.spot_basis", "偏多")], [_reason("D.warehouse_receipt", "偏空")])
    assert rated["confidence"] == "高" and rated["modelConfidence"] == "中"
    assert rated["confidenceBasis"] == "2 条直接依据（行情、基差）同向，反向风险 1 条" and len(rated["confidenceBasis"]) <= 40
    # A second reverse risk, or one reverse point, keeps it at 中.
    two_risks = _rated("偏多", [_reason("M.trend", "偏多"), _reason("D.spot_basis", "偏多"), _reason("M.price", "偏多")],
                       [_reason("D.warehouse_receipt", "偏空"), _reason("N.abc", "偏空")])
    assert (two_risks["confidence"], two_risks["confidenceBasis"]) == ("中", "反向风险有 2 条")


def test_agreeing_inferences_from_one_kind_of_data_or_mostly_reports_stay_medium() -> None:
    rated = _rated("偏空", [_reason("M.price", "偏空"), _reason("M.trend", "偏空")], model="高")
    assert (rated["confidence"], rated["confidenceBasis"], rated["modelConfidence"]) == ("中", "同向依据只来自一类数据（行情）", "高")
    # A report is not a kind of data: 行情 + a report is still one kind.
    assert _rated("偏空", [_reason("M.price", "偏空"), _reason("N.abc", "偏空")])["confidence"] == "中"


def test_low_confidence_conditions() -> None:
    # Only 推测 for the stance.
    guess = _rated("偏多", [_reason("M.trend", "偏多", "推测"), _reason("D.spot_basis", "偏多", "推测")], model="高")
    assert (guess["confidence"], guess["confidenceBasis"]) == ("低", "没有直接依据支持倾向，只有推测")
    # More than half of the cited facts are old values.
    stale = [{**fact, "status": "stale"} if fact["id"] in {"M.trend", "D.spot_basis"} else fact for fact in CONF_FACTS]
    old = _rated("偏多", [_reason("M.trend", "偏多"), _reason("D.spot_basis", "偏多"), _reason("M.price", "偏多")], facts=stale)
    assert (old["confidence"], old["confidenceBasis"]) == ("低", "引用的 3 条事实里 2 条是旧值")
    # Reverse reasons as many as the stance's inferences.
    split = _rated("偏多", [_reason("M.trend", "偏多"), _reason("D.spot_basis", "偏空")])
    assert (split["confidence"], split["confidenceBasis"]) == ("低", "反向理由 1 条，不少于同向直接依据 1 条")
    # 不明 is always 低.
    assert _rated("不明", [_reason("M.trend", "偏多"), _reason("D.spot_basis", "偏多")], model="高")["confidence"] == "低"
    # Fewer than three current facts in the input.
    thin = [CONF_FACTS[1], CONF_FACTS[2], {**CONF_FACTS[0], "status": "stale"}]
    few = _rated("偏多", [_reason("M.trend", "偏多"), _reason("D.spot_basis", "偏多")], facts=thin)
    assert (few["confidence"], few["confidenceBasis"]) == ("低", "可用的新鲜事实只有 2 条")


def test_a_range_judgment_is_at_most_medium() -> None:
    ranged = _rated("震荡", [_reason("M.trend", "偏多"), _reason("D.spot_basis", "偏空"), _reason("D.warehouse_receipt", "中性")], model="高")
    assert (ranged["confidence"], ranged["confidenceBasis"]) == ("中", "震荡判断信心最高为中")
    assert _rated("震荡", [_reason("M.trend", "中性", "推测")])["confidence"] == "低"


def test_the_computed_confidence_is_stored_and_old_history_without_it_still_reads() -> None:
    research = _Research(db=True)
    desk, _ = _desk(research=research)
    entry = {"value": {"stance": "偏多", "confidence": "中", "headline": "旧判断", "reason": "daily"},
             "generatedAt": "2026-09-28T09:01:00+08:00", "researchDay": "2026-09-28", "model": "old"}
    with research.connection:
        research.connection.execute("INSERT INTO research_desk_judgment_history (product_key, generated_at, research_day, payload_json)"
                                    " VALUES (?, ?, ?, ?)", ("SHFE.rb", entry["generatedAt"], entry["researchDay"], json.dumps(entry)))
    then = desk.view_at("SHFE.rb2701", datetime(2026, 9, 28, 10, 0, tzinfo=CHINA))
    assert then is not None and then["confidence"] == "中" and then["confidenceBasis"] is None
    value = DeskJudgment(stance="偏多", confidence="低", headline="主力 RB2610 走势", headlineBasis=["M.price"],
                         points=[_reason("M.trend", "偏多"), _reason("D.spot_basis", "偏多")])
    stored, _ = desk.adopt("SHFE.rb2701", value, CONF_FACTS, model="多 AI 研讨", debate_id="d1")
    assert stored["value"]["confidence"] == "高" and stored["value"]["modelConfidence"] == "低"
    payload = json.loads(research.connection.execute("SELECT payload_json FROM research_desk_judgment_history"
                                                     " WHERE json_extract(payload_json, '$.value.debateId') = 'd1'").fetchone()[0])
    assert payload["value"]["confidenceBasis"] == "2 条直接依据（行情、基差）同向，无反向论点"


# -- desk lifecycle

class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class _Research:
    def __init__(self, *, db: bool = False) -> None:
        self.calls = 0
        if db:
            self.connection = sqlite3.connect(":memory:", check_same_thread=False)
            self.lock = threading.Lock()

    def summary(self, symbol: str, names: Any = None) -> dict[str, Any]:
        self.calls += 1
        return {"datasets": {
            "spot_basis": {"status": "ok", "asOf": "20260926", "source": "100ppi",
                           "data": {"spotPrice": 3165.0, "dominantBasis": 59.0, "dominantBasisRate": 0.019}},
            "inventory": {"status": "unavailable", "asOf": None, "source": None, "data": None},
            "fees_margin": {"status": "ok", "asOf": "20260926", "source": "9qihuo", "data": {"feePerLot": 3.2}},
        }}


class _Sources:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[str] = []

    async def nbs_releases(self) -> list[dict[str, str]]:
        self.calls.append("nbs")
        if self.fail:
            raise SourceError("nbs_timeout")
        return parse_nbs_releases(NBS_LIST)

    async def nbs_price_page(self, url: str) -> str:
        self.calls.append("nbs_page")
        return NBS_PRICES

    async def cftc(self, code: str) -> dict[str, Any]:
        self.calls.append("cftc")
        raise SourceError("cftc_unreachable")

    async def fred(self, series: str) -> dict[str, Any]:
        raise SourceError("fred_key_missing")

    async def eia_crude_stocks(self) -> dict[str, Any]:
        raise SourceError("eia_key_missing")

    async def shfe_weekly(self, now: Any) -> tuple[str, dict[str, dict[str, Any]]]:
        self.calls.append("shfe_weekly")
        if self.fail:
            raise SourceError("shfe_weekly_unavailable")
        return "2026-09-18", {"螺纹钢(仓库)": {"unit": "吨", "stock": 74312.0, "change": -3834.0}}

    async def macro(self) -> dict[str, dict[str, Any]]:
        self.calls.append("macro")
        return {"pmi": {"period": "2026年08月份", "date": "2026-08-01", "MAKE_INDEX": 49.8, "NMAKE_INDEX": 49},
                "fai": {"period": "2026年08月份", "date": "2026-08-01", "BASE_SAME": -13.51}}

    async def org_feed(self, now: Any) -> list[dict[str, Any]]:
        self.calls.append("orgfeed")
        return []

    async def org_notes(self, name: str, now: Any) -> list[dict[str, Any]]:
        self.calls.append("notes:" + name)
        # The second note is the same article the news search found: it shows only here.
        return [{"title": "黑色系 | 双焦偏弱", "summary": "螺纹钢：现货成交11.5万吨。钢厂提降预期升温，期价震荡。", "media": "铜冠金源期货",
                 "link": "https://futures.eastmoney.com/a/9.html", "publishDate": "2026-09-29T08:56:00+08:00", "source": "eastmoney"},
                {"title": "螺纹：社库下降", "summary": "螺纹钢社库连续第四周减少，截至9月24日当周。", "media": "南华期货",
                 "link": "https://finance.eastmoney.com/a/1.html", "publishDate": "2026-09-24T14:54:23+08:00", "source": "eastmoney"}]


class _Generator:
    agent_authenticated = True
    endpoints = {"deepseek-flash": object()}

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.inputs: list[dict[str, Any]] = []
        self.models: list[str | None] = []
        self.users: list[str | None] = []

    async def complete_structured(self, *, instructions: str, input_text: str, schema: type, timeout_seconds: float,
                                  model: str | None = None, usage_user: str | None = None) -> Any:
        self.inputs.append(json.loads(input_text))
        self.models.append(model)
        self.users.append(usage_user)
        if self.fail:
            raise RuntimeError("model_request_failed")
        return SimpleNamespace(value=DeskJudgment(stance="震荡", confidence="中", headline="原料与成材分化", headlineBasis=["R.i"],
                                                  points=[DeskPoint(text="国家统计局旬度螺纹价格下跌", basis=["O.nbs_price"], quote="较上期 -17.6")]))


async def _news(symbol: str) -> dict[str, Any]:
    return {"status": "ok", "source": "eastmoney", "results": [
        {"title": "螺纹钢社库连续第四周减少", "summary": "截至9月24日当周", "media": "新华财经",
         "publishDate": "2026-09-24T14:54:23+08:00", "link": "https://finance.eastmoney.com/a/1.html", "source": "eastmoney"},
        {"title": "钢厂检修增加", "summary": "螺纹钢产量下降", "media": "第一财经",
         "publishDate": "2026-09-28T10:00:00+08:00", "link": "https://finance.eastmoney.com/a/2.html", "source": "eastmoney"}]}


class _Now:
    def __init__(self) -> None:
        self.value = datetime(2026, 9, 28, 7, 0, tzinfo=UTC)  # 15:00 Beijing

    def __call__(self) -> datetime:
        return self.value


class _Provider(FakeReadOnlyDataProvider):
    """The fake market with an adjustable day change for the main contract."""

    change = 0.68

    def market_snapshot(self, turn_id: str, symbol: str) -> dict[str, Any]:
        return {**super().market_snapshot(turn_id, symbol), "changePercent": self.change}


def _desk(*, sources: _Sources | None = None, provider: Any = None, research: _Research | None = None,
          daily_calls: int = 10, now: _Now | None = None) -> tuple[ResearchDeskService, _Clock]:
    clock = _Clock()
    fake = provider or FakeReadOnlyDataProvider()
    desk = ResearchDeskService(research=research or _Research(), news=_news, provider=lambda: fake,
                               sources=sources or _Sources(), clock=clock,
                               now=now or (lambda: datetime(2026, 9, 28, 7, 0, tzinfo=UTC)), daily_calls=daily_calls)
    return desk, clock


async def _collected(desk: ResearchDeskService) -> None:
    await asyncio.gather(*list(desk._facts_pending.values()), return_exceptions=True)


async def _settle(desk: ResearchDeskService) -> None:
    await asyncio.gather(*list(desk._judging.values()))


async def test_report_collects_every_section_with_stable_ids_and_sources() -> None:
    sources = _Sources()
    desk, _ = _desk(sources=sources)
    report = await desk.report("SHFE.rb2701")
    assert report["symbol"] == "SHFE.rb2701" and report["mainSymbol"] == "SHFE.rb2610" and report["productName"] == "螺纹钢"
    sections = {item["key"]: item for item in report["sections"]}
    assert {key: item["status"] for key, item in sections.items()} == {
        "market": "ok", "related": "ok", "fundamental": "ok", "official": "ok", "macro": "ok", "research": "ok", "report": "ok"}
    assert "error" not in sections["official"]  # rebar has no CFTC market, so no CFTC call or error
    ids = [fact["id"] for fact in report["facts"]]
    assert ids[:4] == ["M.price", "M.trend", "M.position", "M.indicators"]
    # Only related products present in the catalog appear (the fake catalog has iron ore, not coke or coil).
    assert [fact["id"] for fact in report["facts"] if fact["section"] == "related"] == ["R.i", "R.linkage"]
    linked = next(fact for fact in report["facts"] if fact["id"] == "R.linkage")
    assert linked["label"] == "关联品种联动度" and linked["source"] == "快期行情（程序计算）"
    assert linked["value"] == "近 20 个交易日日收益相关系数：与铁矿石 1.00"  # two products: no 抱团 clause
    assert "D.spot_basis" in ids and not any(item.startswith("D.fees") for item in ids)
    assert "D.inventory" not in ids  # Eastmoney's inventory repeats the receipts
    weekly = next(fact for fact in report["facts"] if fact["id"] == "O.shfe_stock")
    assert weekly["label"] == "上期所仓库库存（周报 09-18）" and weekly["value"] == "74312 吨，较上周 -3834"
    nbs = next(fact for fact in report["facts"] if fact["id"] == "O.nbs_price")
    assert nbs["value"] == "3165.1 元/吨，较上期 -17.6（-0.6%）" and nbs["label"].startswith("国家统计局 2026年9月中旬")
    # Macro: monthly figures (fixed-asset investment for a construction product), then the NBS releases;
    # the 08-01 release is over 20 days old and left out.
    macro = [fact for fact in report["facts"] if fact["section"] == "macro"]
    assert [fact["id"] for fact in macro[:2]] == ["E.pmi", "E.fai"]
    assert macro[0]["value"] == "49.8，非制造业 49" and macro[1]["value"] == "同比 -13.51%"
    assert [fact["value"] for fact in macro if fact["label"] == "国家统计局发布"] == ["2026年1—8月份全国规模以上工业企业利润增长15.7%"]
    # 机构观点: the passage is the value (cut at a sentence end), the note's own title rides along.
    research = [fact for fact in report["facts"] if fact["section"] == "research"]
    assert [fact["label"] for fact in research] == ["铜冠金源期货", "南华期货"]
    assert research[0]["value"] == "螺纹钢：现货成交11.5万吨。钢厂提降预期升温，期价震荡。" and research[0]["title"] == "黑色系 | 双焦偏弱"
    assert research[0]["kind"] == "report" and research[0]["detail"].startswith("螺纹钢")
    # The article already under 机构观点 is not repeated under 相关报道.
    news = [fact for fact in report["facts"] if fact["section"] == "report"]
    assert [fact["label"] for fact in news] == ["第一财经"] and news[0]["kind"] == "report"
    assert all(fact["kind"] == "data" for fact in report["facts"] if fact["section"] not in {"research", "report"})

    assert report["judgment"] == {"status": "disabled"}
    # Two sections read the NBS list at once: one fetch.
    assert sources.calls.count("nbs") == 1 and sources.calls.count("notes:螺纹钢") == 1


async def test_failing_sources_stay_in_their_section_and_facts_are_shared_for_five_minutes() -> None:
    sources = _Sources(fail=True)
    research = _Research()
    desk, clock = _desk(sources=sources, research=research)
    first, second = await asyncio.gather(desk.report("SHFE.rb2701"), desk.report("SHFE.rb2705"))
    assert research.calls == 1 and sources.calls.count("nbs") == 1
    official = next(item for item in first["sections"] if item["key"] == "official")
    assert official["status"] == "unavailable" and official["error"] == "nbs_timeout"
    assert next(item for item in first["sections"] if item["key"] == "market")["status"] == "ok"
    assert second["symbol"] == "SHFE.rb2705"
    # A forced refresh re-reads at most once a minute.
    await desk.report("SHFE.rb2701", force=True)
    assert research.calls == 1
    with pytest.raises(ValueError, match="research_symbol_unknown"):
        await desk.report("SHFE.zz2701")
    assert "SHFE.zz" not in desk._facts
    clock.now += 4 * 60
    await desk.report("SHFE.rb2701")
    assert research.calls == 1
    clock.now += 2 * 60
    stale = await desk.report("SHFE.rb2701")
    # After five minutes the recent facts go out at once and the re-read runs behind.
    assert stale["generatedAt"] == first["generatedAt"]
    await _collected(desk)
    # The failed official source waits out its own ten-minute backoff.
    assert research.calls == 2 and sources.calls.count("nbs") == 1


async def test_one_judgment_per_research_day_shared_persisted_and_capped() -> None:
    now, research, provider = _Now(), _Research(db=True), _Provider()
    desk, clock = _desk(research=research, provider=provider, now=now, daily_calls=2)
    generator = _Generator()
    first = await desk.report("SHFE.rb2701", generator=generator, user_id="owner-1")
    assert first["judgment"]["status"] == "pending" and first["judgment"]["stage"] == "model"
    assert first["judgment"]["expectedSeconds"] == 12 and first["judgment"]["startedAt"]
    await _settle(desk)
    # The judgment is shared per contract, so its tokens book against the platform
    # marker, never the reader who happened to start it (529-B2); the model sees facts only.
    assert generator.users == [PLATFORM_USAGE_USER] and generator.models == ["deepseek-flash"]
    assert set(generator.inputs[0]) == {"product", "mainSymbol", "now", "facts"}
    ready = (await desk.report("SHFE.rb2705"))["judgment"]  # another reader, no generator
    assert ready["status"] == "ready" and ready["today"] is True and ready["researchDay"] == "2026-09-28"
    assert ready["reason"] == "daily" and ready["movedSincePercent"] == 0.0
    # Later the same day, new facts and a forced refresh inside the hour: still the same judgment.
    now.value += timedelta(minutes=50)
    clock.now += 50 * 60
    await desk.report("SHFE.rb2701", generator=generator, force=True)
    assert len(generator.inputs) == 1
    # After an hour a manual refresh re-runs it.
    now.value += timedelta(minutes=11)
    clock.now += 11 * 60
    manual = await desk.report("SHFE.rb2701", generator=generator, force=True)
    assert manual["judgment"]["refreshing"] is True
    await _settle(desk)
    assert len(generator.inputs) == 2 and (await desk.report("SHFE.rb2701"))["judgment"]["reason"] == "manual"
    # The daily cap (2 here) holds for any product that day.
    assert (await desk.report("DCE.i2701", generator=generator))["judgment"] == {"status": "budget_exhausted"}
    # It survives a restart: a new desk on the same database reads it back.
    again, _ = _desk(research=research, provider=provider, now=now)
    assert (await again.report("SHFE.rb2701"))["judgment"]["reason"] == "manual"
    # Every judgment stays in the history (the latest-only table seeds it once, never twice).
    history = research.connection.execute("SELECT product_key, research_day, payload_json FROM"
                                          " research_desk_judgment_history ORDER BY generated_at").fetchall()
    assert [(key, day, json.loads(payload)["value"]["reason"]) for key, day, payload in history] == [
        ("SHFE.rb", "2026-09-28", "daily"), ("SHFE.rb", "2026-09-28", "manual")]
    assert json.loads(history[0][2])["symbol"] == "SHFE.rb2610" and json.loads(history[0][2])["price"]
    # 08:29 next morning is still the same research day; 08:30 starts a new one.
    now.value = datetime(2026, 9, 29, 0, 29, tzinfo=UTC)
    clock.now += 6 * 60
    assert (await desk.report("SHFE.rb2701", generator=generator))["judgment"]["today"] is True
    now.value = datetime(2026, 9, 29, 0, 31, tzinfo=UTC)
    clock.now += 6 * 60
    stale = await desk.report("SHFE.rb2701")  # a reader who may not generate sees yesterday's, marked
    assert stale["judgment"]["today"] is False and stale["judgment"]["researchDay"] == "2026-09-28"
    await desk.report("SHFE.rb2701", generator=generator)
    await _settle(desk)
    assert len(generator.inputs) == 3  # a new calendar day, a new cap
    days = research.connection.execute("SELECT research_day FROM research_desk_judgment_history"
                                       " ORDER BY generated_at").fetchall()
    assert [day for (day,) in days] == ["2026-09-28", "2026-09-28", "2026-09-29"]


def test_a_database_from_before_the_history_seeds_it_once() -> None:
    research = _Research(db=True)
    entry = {"value": {"stance": "偏多", "reason": "daily"}, "generatedAt": "2026-09-29T09:01:00+08:00",
             "researchDay": "2026-09-29", "model": "deepseek-flash", "price": 3120.0}
    research.connection.execute("CREATE TABLE research_desk_judgments (product_key TEXT PRIMARY KEY, payload_json TEXT NOT NULL)")
    research.connection.execute("INSERT INTO research_desk_judgments VALUES (?, ?)", ("SHFE.rb", json.dumps(entry)))
    research.connection.execute("INSERT INTO research_desk_judgments VALUES (?, ?)", ("DCE.m", "{}"))  # no dates: skipped
    for _ in range(2):
        _desk(research=research)
    rows = research.connection.execute("SELECT product_key, generated_at, research_day FROM"
                                       " research_desk_judgment_history").fetchall()
    assert rows == [("SHFE.rb", "2026-09-29T09:01:00+08:00", "2026-09-29")]


async def test_a_black_swan_re_runs_the_judgment_once_that_day() -> None:
    now, provider = _Now(), _Provider()
    desk, clock = _desk(provider=provider, now=now)
    generator = _Generator()
    await desk.report("SHFE.rb2701", generator=generator)
    await _settle(desk)
    provider.change = -3.4  # ±3% on the day and 1.5 points past the judgment's
    clock.now += 6 * 60
    await desk.report("SHFE.rb2701", generator=generator)  # recent facts at once, the re-read behind
    await _collected(desk)
    swan = await desk.report("SHFE.rb2701", generator=generator)
    assert swan["judgment"]["refreshing"] is True
    await _settle(desk)
    after = (await desk.report("SHFE.rb2701"))["judgment"]
    assert after["reason"] == "swan" and len(generator.inputs) == 2
    provider.change = -6.0
    clock.now += 6 * 60
    await desk.report("SHFE.rb2701", generator=generator)
    await _collected(desk)
    await desk.report("SHFE.rb2701", generator=generator)
    await _settle(desk)
    assert len(generator.inputs) == 2  # once a day


async def test_nearing_the_daily_limit_re_runs_the_judgment_once() -> None:
    class _Limited(_Provider):
        upper = 900.0

        def market_snapshot(self, turn_id: str, symbol: str) -> dict[str, Any]:
            return {**super().market_snapshot(turn_id, symbol), "upperLimit": self.upper, "lowerLimit": 750.0}

    provider = _Limited()
    desk, clock = _desk(provider=provider)
    generator = _Generator()
    first = await desk.report("SHFE.rb2701", generator=generator)
    limit = next(fact for fact in first["facts"] if fact["id"] == "M.limit")
    assert limit["value"].startswith("涨停 900，跌停 750，距最近停板 ")
    await _settle(desk)
    provider.upper = 825.0  # the fake's last price is 822.45: within 1% of the upper limit
    for _ in range(2):
        clock.now += 6 * 60
        await desk.report("SHFE.rb2701", generator=generator)
        await _collected(desk)
    await desk.report("SHFE.rb2701", generator=generator)
    await _settle(desk)
    assert len(generator.inputs) == 2 and (await desk.report("SHFE.rb2701"))["judgment"]["reason"] == "swan"


async def test_notes_bring_their_inventory_figures_and_shfe_weekly_steps_back_to_thursday() -> None:
    from trader_runtime.research_desk import research_facts
    from trader_runtime.research_sources import inventory_mentions
    assert inventory_mentions("豆粕库存102.32万吨，较上周增加2.68万吨，较去年同期减少6.88万吨。", ["豆粕"]) == [
        "豆粕库存102.32万吨，较上周增加2.68万吨"]
    assert inventory_mentions("本周玻璃库存6448万重箱，环比增加8万重箱。纯碱厂家库存152万吨", ["纯碱"]) == ["纯碱厂家库存152万吨"]
    notes = [{"title": "农产品", "summary": "豆粕库存102.32万吨，较上周增加2.68万吨。", "media": "五矿期货",
              "link": "https://e.com/1", "publishDate": "2026-09-28T09:14:00+08:00"}]
    facts = research_facts(notes, ["豆粕"])
    stock = next(fact for fact in facts if fact["id"].startswith("V.inv"))
    assert stock["label"] == "五矿期货引用的库存" and stock["kind"] == "report" and stock["value"] == "豆粕库存102.32万吨，较上周增加2.68万吨"
    asked: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        asked.append(request.url.path.split("_")[-1].split("/")[0])
        if "20260924" in request.url.path:
            return httpx.Response(200, text="<tr><td>铜</td><td>单位：吨</td></tr><tr><td>总计</td><td>1</td><td>2</td><td>3</td></tr>")
        return httpx.Response(404)

    day, sections = await PublicSources(transport=httpx.MockTransport(handler)).shfe_weekly(
        datetime(2026, 9, 26, 10, 0, tzinfo=CHINA))  # Saturday after the Mid-Autumn Friday
    assert day == "2026-09-24" and asked == ["20260925", "20260924"] and "铜" in sections


async def test_failed_judgment_backs_off_and_missing_market_means_no_judgment() -> None:
    desk, clock = _desk()
    failing = _Generator(fail=True)
    await desk.report("SHFE.rb2701", generator=failing)
    await _settle(desk)
    assert (await desk.report("SHFE.rb2701", generator=failing))["judgment"] == {
        "status": "failed", "error": "model_request_failed", "retryAt": "2026-09-28T07:05:00+00:00"}
    assert len(failing.inputs) == 1
    # After the five-minute backoff (and a facts re-read) one new attempt runs.
    clock.now += 6 * 60
    await desk.report("SHFE.rb2701", generator=failing)
    await _settle(desk)
    assert len(failing.inputs) == 2

    class _NoMarket(FakeReadOnlyDataProvider):
        def market_history(self, symbol: str, duration_seconds: int, length: int) -> dict[str, Any]:
            raise RuntimeError("sidecar_market_history_unavailable")

    blind, blind_clock = _desk(provider=_NoMarket())
    report = await blind.report("SHFE.rb2701", generator=_Generator())
    assert report["judgment"] == {"status": "insufficient"}
    assert next(item for item in report["sections"] if item["key"] == "market")["error"] == "market_unavailable"
    # Missing market data is re-read after 30 seconds, not held for five minutes.
    assert blind._facts["SHFE.rb"][0] - blind_clock.now == 30


async def test_a_failed_re_run_keeps_the_old_judgment_and_shows_the_failure_and_retry_time() -> None:
    """#494: a failed manual re-run must not hide behind the earlier judgment's ready status."""
    now = _Now()
    desk, clock = _desk(now=now)
    generator = _Generator()
    await desk.report("SHFE.rb2701", generator=generator)
    await _settle(desk)
    first = (await desk.report("SHFE.rb2701"))["judgment"]
    now.value += timedelta(minutes=61)
    clock.now += 61 * 60
    generator.fail = True
    assert (await desk.report("SHFE.rb2701", generator=generator, force=True))["judgment"]["refreshing"] is True
    await _settle(desk)
    after = (await desk.report("SHFE.rb2701"))["judgment"]
    # The earlier judgment still reads, with its own time; the failure and when to retry are beside it.
    assert after["status"] == "ready" and after["generatedAt"] == first["generatedAt"] and not after.get("refreshing")
    assert after["refreshError"] == "model_request_failed" and after["retryAt"] == "2026-09-28T08:06:00+00:00"
    # Refreshing inside the backoff says so again and starts no model call.
    for _ in range(10):
        held = (await desk.report("SHFE.rb2701", generator=generator, force=True))["judgment"]
        assert held["refreshError"] == "model_request_failed" and not held.get("refreshing")
    assert len(generator.inputs) == 2
    # After the backoff an explicit retry runs once; while it runs and after it lands the failure is gone.
    now.value += timedelta(minutes=6)
    clock.now += 6 * 60
    generator.fail = False
    retry = (await desk.report("SHFE.rb2701", generator=generator, force=True))["judgment"]
    assert retry["refreshing"] is True and "refreshError" not in retry
    await _settle(desk)
    done = (await desk.report("SHFE.rb2701"))["judgment"]
    assert len(generator.inputs) == 3 and done["reason"] == "manual" and done["generatedAt"] != first["generatedAt"]
    assert "refreshError" not in done and "retryAt" not in done


async def test_a_slow_first_history_download_shows_loading_and_lands_without_another_request(monkeypatch) -> None:
    monkeypatch.setattr("trader_runtime.research_desk.SOURCE_DEADLINE_SECONDS", 0.05)

    class _SlowHistory(FakeReadOnlyDataProvider):
        def market_history(self, symbol: str, duration_seconds: int, length: int) -> dict[str, Any]:
            import time
            time.sleep(0.3)
            return super().market_history(symbol, duration_seconds, length)

    desk, _ = _desk(provider=_SlowHistory())
    generator = _Generator()
    report = await desk.report("SHFE.rb2701", generator=generator)
    market = next(item for item in report["sections"] if item["key"] == "market")
    assert market["status"] == "loading" and report["judgment"]["status"] == "pending"
    assert report["judgment"]["stage"] == "facts"
    monkeypatch.setattr("trader_runtime.research_desk.SOURCE_DEADLINE_SECONDS", 5)
    for _ in range(60):
        await asyncio.sleep(0.05)
        if generator.inputs:
            break
    await _settle(desk)
    assert len(generator.inputs) == 1 and any(fact["id"] == "M.price" for fact in generator.inputs[0]["facts"])


async def test_research_reads_history_bars_and_only_the_main_quote_never_live_charts() -> None:
    class _Watched(FakeReadOnlyDataProvider):
        def __init__(self) -> None:
            super().__init__()
            self.calls: list[tuple[str, str]] = []

        def market_workbench(self, symbol: str, duration_seconds: int) -> dict[str, Any]:
            self.calls.append(("workbench", symbol))  # a live-session chart subscription
            return super().market_workbench(symbol, duration_seconds)

        def market_history(self, symbol: str, duration_seconds: int, length: int) -> dict[str, Any]:
            self.calls.append(("history", symbol))
            return super().market_history(symbol, duration_seconds, length)

        def market_snapshot(self, turn_id: str, symbol: str) -> dict[str, Any]:
            self.calls.append(("quote", symbol))
            return super().market_snapshot(turn_id, symbol)

    provider = _Watched()
    desk, _ = _desk(provider=provider)
    report = await desk.report("SHFE.rb2701")
    assert next(item for item in report["sections"] if item["key"] == "market")["status"] == "ok"
    assert not any(kind == "workbench" for kind, _ in provider.calls)
    assert sorted(provider.calls) == [("history", "DCE.i2609"), ("history", "SHFE.rb2610"), ("quote", "SHFE.rb2610")]


async def test_a_loading_section_defers_the_judgment_until_it_lands(monkeypatch) -> None:
    monkeypatch.setattr("trader_runtime.research_desk.SOURCE_DEADLINE_SECONDS", 0.05)

    class _SlowResearch(_Research):
        def summary(self, symbol: str, names: Any = None) -> dict[str, Any]:
            import time
            time.sleep(0.3)
            return super().summary(symbol, names)

    desk, _ = _desk(research=_SlowResearch())
    generator = _Generator()
    report = await desk.report("SHFE.rb2701", generator=generator)
    assert next(item for item in report["sections"] if item["key"] == "fundamental")["status"] == "loading"
    assert report["judgment"]["status"] == "pending" and report["judgment"]["stage"] == "facts"
    assert report["judgment"]["expectedSeconds"] == 20 and generator.inputs == []
    assert report["facts"][0]["asOf"].startswith("20") and "T" in report["facts"][0]["asOf"]
    assert report["facts"][1]["asOf"] == "2026-11-19"  # the last daily bar, as a China date
    # No second request: once the section lands the facts are re-read and the judgment starts.
    monkeypatch.setattr("trader_runtime.research_desk.SOURCE_DEADLINE_SECONDS", 5)
    for _ in range(50):
        await asyncio.sleep(0.05)
        if generator.inputs:
            break
    await _settle(desk)
    assert len(generator.inputs) == 1 and any(fact["id"] == "D.spot_basis" for fact in generator.inputs[0]["facts"])


async def test_pregeneration_covers_the_products_read_this_week() -> None:
    now, research = _Now(), _Research(db=True)
    desk, clock = _desk(research=research, now=now)
    generator = _Generator()
    await desk.report("SHFE.rb2701", user_id="owner-1")  # read, no generation allowed then
    await desk.report("DCE.i2701", user_id="owner-1")
    desk.generator_for = lambda user: generator if user == "owner-1" else None
    now.value = datetime(2026, 9, 29, 0, 45, tzinfo=UTC)  # 08:45 next morning
    clock.now += 3600
    done = await desk.pregenerate()
    assert sorted(done) == ["DCE.i", "SHFE.rb"] and len(generator.inputs) == 2
    assert generator.users == [PLATFORM_USAGE_USER] * 2  # platform usage, not the reader's quota
    # Nothing left to do the same morning.
    await desk.pregenerate()
    assert len(generator.inputs) == 2


async def test_a_holder_starting_after_0845_catches_up_and_the_other_slot_reads_its_judgments(tmp_path) -> None:
    """2026-10-12: a lease holder that starts (or takes over) after 08:45 runs the day's pre-generation at
    once instead of waiting a day; a second start the same day calls no model again; the other slot's desk
    (same state database) shows the judgment without a model, and the day's call count is shared."""
    def shared() -> _Research:
        research = _Research()
        research.connection = sqlite3.connect(tmp_path / "desk.sqlite3", check_same_thread=False)
        research.lock = threading.Lock()
        return research
    now = _Now()
    holder, clock = _desk(research=shared(), now=now)
    reader, _ = _desk(research=shared(), now=now)
    await holder.report("SHFE.rb2701", user_id="owner-1")  # read yesterday: on the watch list
    generator = _Generator()
    holder.generator_for = lambda user: generator
    pushed: list[tuple[dict[str, Any], str]] = []

    async def morning(judgments: dict[str, Any], day: str) -> None:
        pushed.append((judgments, day))
    holder.on_pregenerated = morning
    now.value = datetime(2026, 9, 29, 2, 0, tzinfo=UTC)  # 10:00 Beijing, a Tuesday: past 08:45
    clock.now += 3600
    holder.start()
    async with asyncio.timeout(10):
        while not pushed:
            await asyncio.sleep(0.01)
    await holder.stop_loop()
    assert len(generator.inputs) == 1 and pushed[0][1] == "2026-09-29"
    assert pushed[0][0]["SHFE.rb"]["researchDay"] == "2026-09-29"
    # The other slot reads the judgment from the shared table: no model, no generator of its own.
    shown = (await reader.report("SHFE.rb2701"))["judgment"]
    assert shown["status"] == "ready" and shown["researchDay"] == "2026-09-29" and shown["today"] is True
    assert reader._budget.used("2026-09-29") == 1
    # Taken over again the same morning: nothing left to generate.
    holder.start()
    async with asyncio.timeout(10):
        while len(pushed) < 2:
            await asyncio.sleep(0.01)
    await holder.stop_loop()
    assert len(generator.inputs) == 1


async def test_losing_the_lease_cancels_the_pregenerations_model_call(tmp_path) -> None:
    """The judgment pre-generation started runs as its own task: stop_loop() (the lease moving away) cancels it
    with the loop, so the old slot writes no judgment after the new holder's catch-up starts."""
    research = _Research()
    research.connection = sqlite3.connect(tmp_path / "desk.sqlite3", check_same_thread=False)
    research.lock = threading.Lock()
    now = _Now()
    desk, clock = _desk(research=research, now=now)
    await desk.report("SHFE.rb2701", user_id="owner-1")
    entered, release = asyncio.Event(), asyncio.Event()

    class _Slow(_Generator):
        async def complete_structured(self, **kwargs: Any) -> Any:
            entered.set()
            await release.wait()
            return await super().complete_structured(**kwargs)
    generator = _Slow()
    desk.generator_for = lambda user: generator
    now.value = datetime(2026, 9, 29, 2, 0, tzinfo=UTC)  # 10:00 Beijing: the catch-up runs at once
    clock.now += 3600
    desk.start()
    async with asyncio.timeout(10):
        await entered.wait()
    call = desk._judging["SHFE.rb"]
    await desk.stop_loop()
    assert call.cancelled() and "SHFE.rb" not in desk._judging
    release.set()
    await asyncio.sleep(0)
    assert research.connection.execute("SELECT COUNT(*) FROM research_desk_judgments").fetchone() == (0,)


async def test_the_daily_budget_is_shared_by_every_desk_on_the_database(tmp_path) -> None:
    research = _Research()
    research.connection = sqlite3.connect(tmp_path / "budget.sqlite3", check_same_thread=False)
    research.lock = threading.Lock()
    first, _ = _desk(research=research, daily_calls=1)
    second, _ = _desk(research=research, daily_calls=1)  # a restart, or the other slot
    assert first._budget.take("2026-09-28", 1) is True
    assert second._budget.take("2026-09-28", 1) is False and second._budget.used("2026-09-28") == 1


async def test_desk_routes_gate_generation_and_guests_get_facts_only(tmp_path, monkeypatch) -> None:
    from trader_runtime.main import create_app

    app = create_app(tmp_path / "runtime.sqlite3", local_token="research-test-token-with-32-chars!!", agent_mode="fake", data_mode="fake")
    calls: list[tuple[str, bool, bool]] = []

    async def report(self, symbol, *, generator=None, force=False, user_id=None, fast=False):
        calls.append((symbol, generator is not None, force, user_id))
        return {"symbol": symbol, "facts": [], "sections": [], "judgment": {"status": "ready", "headline": "x"}}

    monkeypatch.setattr("trader_runtime.research_desk.ResearchDeskService.report", report)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers={"Origin": "http://127.0.0.1:5173"}) as client:
            assert (await client.get("/api/research/desk", params={"symbol": "SHFE.rb2610"})).status_code == 401
            assert (await client.post("/api/auth/local", headers={"Authorization": "Bearer research-test-token-with-32-chars!!"})).status_code == 200
            got = await client.get("/api/research/desk", params={"symbol": "SHFE.rb2610"})
            assert got.status_code == 200 and got.json()["judgment"]["status"] == "ready"
            assert (await client.post("/api/research/desk/refresh", params={"symbol": "SHFE.rb2610"})).status_code == 200
            assert [call[0] for call in calls] == ["SHFE.rb2610", "SHFE.rb2610"] and calls[1][2] is True
            assert calls[0][3] and calls[0][3] == calls[1][3]  # the signed-in user is passed for usage
            catalog = (await client.get("/api/agent/capabilities")).json()
            operations = {item["operation"]: item for item in catalog["operations"]}
            # 529-A2c：当天判断缺失时这次读取会触发一次模型生成并计入用量。
            assert operations["GET /api/research/desk"]["readOnly"] is False
            assert "POST /api/research/desk/refresh" in operations
    finally:
        app.state.research.close()
        app.state.store.close()


def test_shfe_weekly_report_and_title_first_news() -> None:
    from pathlib import Path

    from trader_runtime.research_sources import parse_shfe_weekly, shfe_weekly_row
    page = """<table><tr><td>地区</td><td>仓库</td><td>上周库存</td></tr><tr><td>小计</td><td>期货</td><td>小计</td><td>期货</td></tr>
<tr><td>铜</td><td>单位：吨</td></tr><tr><td>合计</td><td>1</td><td>1</td><td>2</td><td>2</td><td>1</td><td>1</td></tr>
<tr><td>总计</td><td>54780</td><td>20854</td><td>56073</td><td>26655</td><td>1293</td><td>5801</td></tr>
<tr><td>螺纹钢(仓库)</td><td>单位：吨</td></tr><tr><td>总计</td><td>78146</td><td>74312</td><td>-3834</td><td>896854</td></tr></table>"""
    sections = parse_shfe_weekly(page)
    assert shfe_weekly_row(sections, "cu") == {"unit": "吨", "total": "总计", "stock": 56073.0, "change": 1293.0}
    assert shfe_weekly_row(sections, "rb")["stock"] == 74312.0 and shfe_weekly_row(sections, "zn") is None
    assert Path(__file__).exists()
    items = [{"date": "2026-09-28 09:00:00", "title": f"螺纹钢早评{i}", "content": "", "mediaName": "期货", "code": str(i)} for i in range(3)]
    items += [{"date": "2026-09-28 10:00:00", "title": "【调研快报】新钢股份接待投资者", "content": "螺纹钢", "mediaName": "Choice", "code": "8"},
              {"date": "2026-09-28 11:00:00", "title": "商品夜盘收盘", "content": "螺纹钢收涨", "mediaName": "第一财经", "code": "9"}]
    titled = parse_eastmoney_news(_jsonp(items), "螺纹钢", NOW)
    # Three items name the product in the title: text-only mentions and company noise stay out.
    assert [entry["title"] for entry in titled] == ["螺纹钢早评0", "螺纹钢早评1", "螺纹钢早评2"]
    fewer = parse_eastmoney_news(_jsonp(items[:1] + items[3:]), "螺纹钢", NOW)
    assert [entry["title"] for entry in fewer] == ["商品夜盘收盘", "螺纹钢早评0"]  # too few titles: text matches fill in


async def test_stale_sources_keep_their_status_and_own_time_in_the_facts() -> None:
    """#491: a dataset that failed to refresh, or a quote the provider marks stale, reaches the facts,
    the sections, the model input and the judgment's basis as an old value with its own time."""
    class _StaleResearch(_Research):
        def summary(self, symbol: str, names: Any = None) -> dict[str, Any]:
            self.calls += 1
            return {"datasets": {
                "spot_basis": {"status": "stale", "error": "research_source_outage", "asOf": "20260925", "source": "100ppi",
                               "fetchedAt": "2026-09-25T10:00:00+00:00",
                               "data": {"spotPrice": 3165.0, "dominantBasis": 59.0, "dominantBasisRate": 0.019}},
                "warehouse_receipt": {"status": "ok", "asOf": "20260928", "source": "上期所", "fetchedAt": "2026-09-28T10:00:00+00:00",
                                      "data": {"total": 85813.0, "change": 12396.0}},
            }}

    class _StaleQuote(_Provider):
        def market_snapshot(self, turn_id: str, symbol: str) -> dict[str, Any]:
            return {**super().market_snapshot(turn_id, symbol), "price": 3200.0, "dataStatus": "stale",
                    "capturedAt": "2026-09-27T10:00:00+00:00", "quoteDatetime": "2026-09-27T10:00:00+00:00"}

    desk, _ = _desk(research=_StaleResearch(), provider=_StaleQuote())
    generator = _Generator()
    report = await desk.report("SHFE.rb2701", generator=generator)
    facts = {fact["id"]: fact for fact in report["facts"]}
    sections = {item["key"]: item for item in report["sections"]}
    basis = facts["D.spot_basis"]
    assert basis["status"] == "stale" and basis["error"] == "research_source_outage"
    assert basis["fetchedAt"] == "2026-09-25T10:00:00+00:00" and basis["asOf"] == "20260925"
    assert facts["D.warehouse_receipt"]["fetchedAt"] == "2026-09-28T10:00:00+00:00" and "status" not in facts["D.warehouse_receipt"]
    assert sections["fundamental"]["status"] == "stale" and sections["fundamental"]["error"] == "research_source_outage"
    price = facts["M.price"]
    assert price["value"].startswith("3200") and price["status"] == "stale"
    assert price["asOf"] == "2026-09-27T18:00+08:00"  # the quote's own time, not the history read's
    assert sections["market"]["status"] == "stale"
    await _settle(desk)
    sent = {fact["id"]: fact for fact in generator.inputs[0]["facts"]}
    assert sent["D.spot_basis"]["status"] == "stale" and sent["D.spot_basis"]["fetchedAt"] == "2026-09-25T10:00:00+00:00"
    kept = validate_judgment(DeskJudgment(stance="震荡", confidence="低", headline="基差偏强", headlineBasis=["D.spot_basis"],
                                          points=[DeskPoint(text="主力基差 +59", basis=["D.spot_basis"], quote="主力基差 +59")]),
                             report["facts"])
    assert kept["basis"]["D.spot_basis"]["status"] == "stale" and kept["basis"]["D.spot_basis"]["fetchedAt"] == basis["fetchedAt"]


async def test_without_a_quote_the_price_fact_is_the_last_close_on_its_bar_date() -> None:
    """#491: when the live quote fails the price comes from the daily bars and says so."""
    class _NoQuote(FakeReadOnlyDataProvider):
        def market_snapshot(self, turn_id: str, symbol: str) -> dict[str, Any]:
            raise RuntimeError("sidecar_market_quote_unavailable")

    desk, _ = _desk(provider=_NoQuote())
    report = await desk.report("SHFE.rb2701")
    price = next(fact for fact in report["facts"] if fact["id"] == "M.price")
    trend = next(fact for fact in report["facts"] if fact["id"] == "M.trend")
    assert price["label"] == "主力 RB2610 最近收盘价" and price["asOf"] == trend["asOf"]


async def test_a_cited_fact_that_changed_under_its_id_is_flagged_with_the_snapshot() -> None:
    """#492: the same id refreshed to a different value keeps the judgment, but the judgment names
    the citations whose current fact no longer reads as it did; the snapshot stays in basis."""
    now = _Now()
    desk, _ = _desk(research=_Research(db=True), now=now)
    report = await desk.report("SHFE.rb2701")
    current = next(fact for fact in report["facts"] if fact["id"] == "D.spot_basis")
    then = {**current, "value": "现货 3074，主力基差 +74"}
    value = validate_judgment(DeskJudgment(stance="偏多", confidence="中", headline="基差走强", headlineBasis=["R.i"],
                                           points=[DeskPoint(text="主力基差 +74", basis=["D.spot_basis"], quote="主力基差 +74")]),
                              [then, next(fact for fact in report["facts"] if fact["id"] == "R.i")])
    desk._judgments["SHFE.rb"] = {"value": value, "generatedAt": now().isoformat(timespec="seconds"),
                                  "researchDay": desk.research_day(now()), "model": "test", "price": None}
    shown = (await desk.report("SHFE.rb2701"))["judgment"]
    assert shown["status"] == "ready" and shown["changedBasis"] == ["D.spot_basis"]
    assert shown["basis"]["D.spot_basis"]["value"] == "现货 3074，主力基差 +74"


async def test_close_fences_late_loaded_and_generate_callbacks(monkeypatch) -> None:
    """#516-5：close() 之后迟到的 done-callback（_loaded）不再重读事实，迟到的 _generate 不再写判断。"""
    import time

    class _Anchored(_Generator):
        async def complete_structured(self, **kwargs: Any) -> Any:
            return SimpleNamespace(value=DeskJudgment(stance="偏多", confidence="低", headline="原料与成材分化",
                                                      headlineBasis=["R.i"],
                                                      points=[DeskPoint(text="原料日涨 +0.8%", basis=["R.i"],
                                                                        quote="日涨跌 +0.8%")]))

    monkeypatch.setattr("trader_runtime.research_desk.SOURCE_DEADLINE_SECONDS", 0.05)

    class _SlowHistory(FakeReadOnlyDataProvider):
        def market_history(self, symbol: str, duration_seconds: int, length: int) -> dict[str, Any]:
            time.sleep(0.15)
            return super().market_history(symbol, duration_seconds, length)

    research = _Research(db=True)
    desk, _ = _desk(research=research, provider=_SlowHistory())
    report = await desk.report("SHFE.rb2701")  # the market section outruns the deadline: a callback is armed
    assert next(item for item in report["sections"] if item["key"] == "market")["status"] == "loading"
    await desk.close()
    calls_after_close = research.calls
    monkeypatch.setattr("trader_runtime.research_desk.SOURCE_DEADLINE_SECONDS", 5)
    await asyncio.sleep(0.3)  # the slow fetch lands after close: the armed callback fires into the fence
    assert research.calls == calls_after_close and desk._facts_pending == {} and desk._judging == {}

    facts = {"productName": "螺纹钢", "product": "rb", "mainSymbol": "SHFE.rb2610", "facts": FACTS, "sections": []}
    await desk._generate("SHFE.rb", facts, _Anchored(), "daily")
    assert "SHFE.rb" not in desk._judgments
    with research.lock:
        assert research.connection.execute("SELECT COUNT(*) FROM research_desk_judgment_history").fetchone()[0] == 0


# -- 第七批: directions, conclusion, change, stale, 重新判断, tiles and the macro by category

def test_reasons_carry_a_direction_and_the_conclusion_is_anchored_or_dropped() -> None:
    judgment = DeskJudgment(stance="震荡", confidence="中", headline="原料偏强", headlineBasis=["R.i"], points=[
        DeskPoint(text="铁矿石日涨 +0.8%", basis=["R.i"], quote="日涨跌 +0.8%", direction="偏多"),
        DeskPoint(text="主力日跌 0.42%", basis=["M.price"], quote="3106", direction="大涨"),  # not a direction: inferred
        DeskPoint(text="主力日跌 5%", basis=["M.price"], quote="3106", direction="偏空"),     # numbers still checked
    ], risks=[DeskPoint(text="主力日涨跌 -0.42%", basis=["M.price"], quote="日涨跌 -0.42%", direction="偏空")],
        conclusion="两边接近判震荡；若铁矿石再涨 0.8%，倾向转多。")
    dropped: dict[str, int] = {}
    result = validate_judgment(judgment, FACTS, dropped)
    assert [(point["text"], point["direction"]) for point in result["points"]] == [("铁矿石日涨 +0.8%", "偏多"), ("主力日跌 0.42%", "中性")]
    assert result["risks"][0]["direction"] == "偏空" and dropped == {"number_mismatch": 1}
    assert result["conclusion"] == "两边接近判震荡；若铁矿石再涨 0.8%，倾向转多。"
    invented = judgment.model_copy(update={"conclusion": "目标看到 3300，倾向转多"})
    assert validate_judgment(invented, FACTS, dropped)["conclusion"] == "" and dropped["conclusion_dropped"] == 1
    advice = judgment.model_copy(update={"conclusion": "建议逢低买入"})
    assert validate_judgment(advice, FACTS)["conclusion"] == ""
    # A model that gives no directions: points with the stance, risks against it (偏多 → 偏空).
    bare = DeskJudgment(stance="偏多", confidence="中", headline="原料偏强", headlineBasis=["R.i"],
                        points=[DeskPoint(text="原料偏强", basis=["R.i"], quote="上游原料")],
                        risks=[DeskPoint(text="主力走弱", basis=["M.price"], quote="3106")])
    kept = validate_judgment(bare, FACTS)
    assert kept["points"][0]["direction"] == "偏多" and kept["risks"][0]["direction"] == "偏空" and kept["conclusion"] == ""


async def test_a_judgment_stored_before_directions_is_sorted_into_columns_without_a_re_run() -> None:
    research = _Research(db=True)

    def stored(stance: str) -> dict[str, Any]:
        point = {"text": "原料偏强", "basis": ["R.i"], "kind": "推断", "grounds": "data", "quote": "上游原料"}
        return {"value": {"stance": stance, "confidence": "中", "headline": "旧判断", "headlineBasis": ["R.i"],
                          "points": [point], "risks": [{**point, "text": "主力走弱"}], "watch": [], "basis": {}},
                "generatedAt": "2026-09-28T06:30:00+00:00", "researchDay": "2026-09-28", "model": "old", "dayChange": 0.0, "price": None}
    with research.connection:
        research.connection.execute("CREATE TABLE research_desk_judgments (product_key TEXT PRIMARY KEY, payload_json TEXT NOT NULL)")
        research.connection.execute("INSERT INTO research_desk_judgments VALUES (?, ?), (?, ?)",
                                    ("SHFE.rb", json.dumps(stored("偏多")), "SHFE.au", json.dumps(stored("震荡"))))
    desk, _ = _desk(research=research)
    generator = _Generator()
    rebar = (await desk.report("SHFE.rb2701", generator=generator))["judgment"]
    assert rebar["status"] == "ready" and rebar["headline"] == "旧判断" and "conclusion" not in rebar
    assert rebar["points"][0]["direction"] == "偏多" and rebar["risks"][0]["direction"] == "偏空"
    gold = (await desk.report("SHFE.au2612"))["judgment"]
    assert gold["points"][0]["direction"] == "中性" and gold["risks"][0]["direction"] == "中性"
    assert generator.inputs == []  # nothing re-ran
    payload = research.connection.execute("SELECT payload_json FROM research_desk_judgments WHERE product_key = 'SHFE.rb'").fetchone()[0]
    assert "direction" not in payload  # the stored judgment is read as it is, not rewritten


class _StanceGenerator(_Generator):
    """A model whose stance the test sets, with a direction on each reason and a conclusion."""

    stance = "偏多"

    async def complete_structured(self, **kwargs: Any) -> Any:
        await super().complete_structured(**kwargs)
        return SimpleNamespace(value=DeskJudgment(
            stance=self.stance, confidence="中", headline="原料与成材分化", headlineBasis=["R.i"],
            points=[DeskPoint(text="国家统计局旬度螺纹价格下跌", basis=["O.nbs_price"], quote="较上期 -17.6", direction="偏空")],
            risks=[DeskPoint(text="原料偏强", basis=["R.i"], quote="上游原料", direction="偏多")], conclusion="两边接近。"))


async def test_change_compares_with_the_previous_research_days_judgment() -> None:
    now, research = _Now(), _Research(db=True)
    desk, clock = _desk(research=research, now=now)
    generator = _StanceGenerator()
    await desk.report("SHFE.rb2701", generator=generator)
    await _settle(desk)
    first = (await desk.report("SHFE.rb2701"))["judgment"]
    assert first["conclusion"] == "两边接近。" and "change" not in first  # no earlier day: nothing to compare
    now.value = datetime(2026, 9, 29, 1, 0, tzinfo=UTC)
    clock.now += 6 * 60
    generator.stance = "震荡"
    await desk.report("SHFE.rb2701", generator=generator)
    await _settle(desk)
    assert (await desk.report("SHFE.rb2701"))["judgment"]["change"] == {
        "previousDay": "2026-09-28", "previousStance": "偏多", "same": False, "text": "上次（09-28）偏多 → 今天转震荡"}
    now.value = datetime(2026, 9, 30, 1, 0, tzinfo=UTC)
    clock.now += 6 * 60
    await desk.report("SHFE.rb2701", generator=generator)
    await _settle(desk)
    assert (await desk.report("SHFE.rb2701"))["judgment"]["change"]["text"] == "与上次（09-29）相同"


class _Live:
    """The live service as the desk reads it for 第七批: the product's events, the news brief, nothing else."""

    def __init__(self) -> None:
        self.majors: list[str] = []
        self.brief_value: dict[str, Any] = {"status": "none"}
        self.analyst = self

    def brief(self, product: str) -> dict[str, Any]:
        return self.brief_value

    def product_events(self, product: str) -> tuple[str, list[dict[str, Any]]]:
        return "螺纹钢", [{"tier": "major", "publishedAt": at} for at in self.majors] + [
            {"tier": "other", "publishedAt": "2026-09-28T16:00:00+08:00"}]

    def flash_feed(self, *args: Any) -> dict[str, Any]:
        return {"status": "unavailable", "error": "research_flash_unavailable"}

    def flash_pick(self, product: str) -> list[dict[str, Any]]:
        return []

    async def settle_screen(self, product: str, timeout: float) -> None:
        return None

    def calendar_feed(self, days: int) -> dict[str, Any]:
        return {"status": "unavailable"}

    def rates(self) -> dict[str, Any]:
        return {"status": "unavailable"}


async def test_a_judgment_is_marked_possibly_stale_by_later_major_news_or_a_newer_brief_against_it() -> None:
    live = _Live()
    desk, clock = _desk()
    desk.extra = live  # type: ignore[assignment]
    # Two 重要 events after the 15:00 judgment and one before it: not yet.
    live.majors = ["2026-09-28T15:20:00+08:00", "2026-09-28T15:40:00+08:00", "2026-09-28T14:10:00+08:00"]
    first = await desk.report("SHFE.rb2701", generator=_Generator())
    assert "news" not in first and first["judgment"]["status"] == "pending"
    await _settle(desk)

    async def judged() -> dict[str, Any]:
        clock.now += 6 * 60
        await desk.report("SHFE.rb2701")
        await _collected(desk)  # the facts (and the events) are re-read behind
        return await desk.report("SHFE.rb2701")
    assert "stale" not in (await judged())["judgment"]
    live.majors.append("2026-09-28T16:05:00+08:00")
    assert (await judged())["judgment"]["stale"] == {"count": 3, "newsNet": None}
    # A news brief: shown as the 新闻面 line; written after the judgment and pointing the other way, it marks it too
    # (the judgment is 震荡, so a 偏空 brief differs); an older brief, or a 中性 one, does not.
    live.majors = []
    live.brief_value = {"status": "ready", "net": "偏空", "take": "美债收益率创新高压制金价", "generatedAt": "2026-09-28T07:20:00+00:00",
                        "points": []}
    report = await judged()
    assert report["news"] == {"net": "偏空", "take": "美债收益率创新高压制金价", "generatedAt": "2026-09-28T07:20:00+00:00"}
    assert report["judgment"]["stale"] == {"count": 0, "newsNet": "偏空"}
    live.brief_value = {**live.brief_value, "generatedAt": "2026-09-28T06:50:00+00:00"}
    assert "stale" not in (await judged())["judgment"]
    live.brief_value = {**live.brief_value, "net": "中性", "generatedAt": "2026-09-28T07:20:00+00:00"}
    assert "stale" not in (await judged())["judgment"]
    live.brief_value = {"status": "writing"}
    assert "news" not in await judged()


async def test_news_against_the_judgment_re_runs_it_by_itself_at_most_twice_a_day() -> None:
    """今日判断自动更新 (2026-10-24): the 判断可能过时 hint re-runs the judgment, not sooner than 30 minutes after the
    last one and twice a research day at most; the card names why and lists the day's runs."""
    live, now = _Live(), _Now()
    desk, clock = _desk(now=now, research=_Research(db=True))
    desk.extra = live  # type: ignore[assignment]
    generator = _Generator()
    await desk.report("SHFE.rb2701", generator=generator)
    await _settle(desk)

    async def read() -> dict[str, Any]:
        clock.now += 6 * 60
        await desk.report("SHFE.rb2701", generator=generator)
        await _collected(desk)
        await desk.report("SHFE.rb2701", generator=generator)
        await _settle(desk)
        return (await desk.report("SHFE.rb2701"))["judgment"]
    live.majors = ["2026-09-28T15:20:00+08:00", "2026-09-28T15:40:00+08:00", "2026-09-28T16:05:00+08:00"]
    assert (await read())["stale"]["count"] == 3 and len(generator.inputs) == 1  # inside 30 minutes: the hint only
    now.value += timedelta(minutes=31)
    judgment = await read()
    assert len(generator.inputs) == 2 and judgment["reason"] == "news"
    assert judgment["trigger"] == "判断后又出了 3 条重要新闻" and "stale" not in judgment
    assert [item["reason"] for item in judgment["updates"]] == ["news", "daily"]
    live.majors = ["2026-09-28T16:00:00+08:00", "2026-09-28T16:01:00+08:00", "2026-09-28T16:02:00+08:00"]
    now.value += timedelta(minutes=31)
    assert (await read())["reason"] == "news" and len(generator.inputs) == 3
    assert (await desk.report("SHFE.rb2701"))["judgment"]["nextAuto"]["news"] is False
    live.majors = ["2026-09-28T16:40:00+08:00", "2026-09-28T16:41:00+08:00", "2026-09-28T16:42:00+08:00"]
    now.value += timedelta(minutes=31)
    assert (await read())["stale"]["count"] == 3 and len(generator.inputs) == 3  # the day's two news runs are used


async def test_a_night_session_product_is_judged_again_once_after_2045() -> None:
    now = _Now()
    desk, clock = _desk(now=now)
    generator = _Generator()
    await desk.report("SHFE.rb2701", generator=generator)
    await _settle(desk)
    first = (await desk.report("SHFE.rb2701"))["judgment"]
    assert first["nextAuto"] == {"nightAt": "2026-09-28T20:45:00+08:00", "news": True}
    now.value = datetime(2026, 9, 28, 12, 50, tzinfo=UTC)  # 20:50 Beijing, a Monday
    for _ in range(2):
        clock.now += 6 * 60
        await desk.report("SHFE.rb2701", generator=generator)
        await _collected(desk)
        await desk.report("SHFE.rb2701", generator=generator)
        await _settle(desk)
    judgment = (await desk.report("SHFE.rb2701"))["judgment"]
    assert judgment["reason"] == "night" and len(generator.inputs) == 2  # once, not on every later read
    assert judgment["nextAuto"]["nightAt"] is None


async def test_rejudge_keeps_the_hourly_limit_and_says_why_it_cannot_run() -> None:
    now = _Now()
    desk, clock = _desk(now=now, daily_calls=2)
    generator = _Generator()
    started = await desk.report("SHFE.rb2701", generator=generator, force=True)
    assert started["rejudge"] == {"status": "started", "message": "已按最新消息重新判断，完成后在这里更新"}
    assert (await desk.report("SHFE.rb2701", generator=generator, force=True))["rejudge"]["status"] == "running"
    await _settle(desk)
    assert "rejudge" not in await desk.report("SHFE.rb2701", generator=generator)  # only the manual refresh answers it
    # Inside the hour: the same judgment, and the reason with the time it may run again (16:00 Beijing).
    now.value += timedelta(minutes=20)
    clock.now += 20 * 60
    limited = await desk.report("SHFE.rb2701", generator=generator, force=True)
    assert limited["rejudge"] == {"status": "limited", "availableAt": "2026-09-28T08:00:00+00:00",
                                  "message": "1 小时内已重判过，16:00 后可再判"}
    assert len(generator.inputs) == 1 and limited["judgment"]["status"] == "ready"
    # After the hour a reader who may not generate is told so; one who may starts it.
    now.value += timedelta(minutes=45)
    clock.now += 45 * 60
    assert (await desk.report("SHFE.rb2701", force=True))["rejudge"] == {
        "status": "unavailable", "message": "当前档位未开通「联网研究」，不能重新判断"}
    assert (await desk.report("SHFE.rb2701", generator=generator, force=True))["rejudge"]["status"] == "started"
    await _settle(desk)
    # The day's cap (2 here) is a reason too.
    now.value += timedelta(minutes=61)
    clock.now += 61 * 60
    assert (await desk.report("SHFE.rb2701", generator=generator, force=True))["rejudge"] == {
        "status": "limited", "message": "今天的判断次数已用完，明天 08:30 后再判"}
    assert len(generator.inputs) == 2


class _GoldSources(_Sources):
    async def macro(self) -> dict[str, dict[str, Any]]:
        return {**await super().macro(),
                "cpi": {"period": "2026年08月份", "date": "2026-08-01", "NATIONAL_SAME": 0.8, "NATIONAL_SEQUENTIAL": 0.4},
                "m2": {"period": "2026年08月份", "date": "2026-08-01", "BASIC_CURRENCY_SAME": 8.8, "CURRENCY_SAME": 6.0}}

    async def fred(self, series: str) -> dict[str, Any]:
        return {"DGS10": {"value": 4.1, "change": 0.05, "date": "2026-09-25"},
                "DTWEXBGS": {"value": 120.4, "change": -0.3, "date": "2026-09-25"}}[series]


async def test_tiles_pick_the_key_figures_and_macro_keeps_only_the_category() -> None:
    desk, _ = _desk(sources=_GoldSources())
    rebar = await desk.report("SHFE.rb2701")
    ids = {fact["id"] for fact in rebar["facts"]}
    assert {"E.pmi", "E.m2", "E.fai"} <= ids and not {"E.cpi", "O.fred_DGS10", "O.fred_DTWEXBGS"} & ids
    tiles = {tile["label"]: tile for tile in rebar["tiles"]}
    # Rebar has no foreign counterpart and the stub research has no top-20 rank: those tiles are left out, not zero.
    assert list(tiles) == ["主力 RB2610", "近 20 日", "日线 RSI", "现货基差", "20 日持仓变化", "中国制造业 PMI"]
    assert tiles["现货基差"] == {"label": "现货基差", "value": "+59", "color": "up", "note": "期货比现货高 · 09-26", "ref": "D.spot_basis"}
    assert tiles["中国制造业 PMI"] == {"label": "中国制造业 PMI", "value": "49.8", "color": "down", "note": "收缩 · 08月份", "ref": "E.pmi"}
    assert tiles["主力 RB2610"]["ref"] == "M.price" and tiles["日线 RSI"]["color"] == "none"
    assert all(tile["ref"] in ids for tile in rebar["tiles"]) and all("figures" not in fact for fact in rebar["facts"])
    gold = await desk.report("SHFE.au2612")
    macro = [fact["id"] for fact in gold["facts"] if fact["section"] == "macro"]
    # 沪金 no longer lists PMI, CPI, M2, fixed-asset investment or the NBS headlines: only the dollar and US yields.
    assert macro == ["O.fred_DTWEXBGS", "O.fred_DGS10"]
    assert gold["tiles"][-1] == {"label": "美 10 年期国债", "value": "4.1%", "color": "up", "note": "较前值 +0.05 · 09-25",
                                 "ref": "O.fred_DGS10"}


class _DownSources(_Sources):
    """Every slow source failing, the note search answering nothing (2026-10-12 数据获取审查)."""

    def __init__(self) -> None:
        super().__init__(fail=True)

    async def org_notes(self, name: str, now: Any) -> list[dict[str, Any]]:
        self.calls.append("notes:" + name)
        return []


async def _refreshed(desk: ResearchDeskService) -> dict[str, Any]:
    await desk.report("SHFE.rb2701")
    await _collected(desk)
    return await desk.report("SHFE.rb2701")


async def test_slow_sources_keep_the_last_good_value_stale_and_a_restart_reads_it_back() -> None:
    """A failed slow source (or an empty note search) serves its last good value marked stale instead of
    caching the failure; the value lives in research_live_cache, so a restarted desk has it too."""
    research = _Research(db=True)
    desk, clock = _desk(sources=_Sources(), research=research)
    first = await desk.report("SHFE.rb2701")
    assert "status" not in next(fact for fact in first["facts"] if fact["id"] == "O.shfe_stock")

    down = _DownSources()
    desk.sources = down
    clock.now += 13 * 3600  # past every slow TTL
    second = await _refreshed(desk)
    stock = next(fact for fact in second["facts"] if fact["id"] == "O.shfe_stock")
    assert stock["value"] == "74312 吨，较上周 -3834"
    assert stock["status"] == "stale" and stock["error"] == "shfe_weekly_unavailable" and stock["fetchedAt"]
    sections = {item["key"]: item for item in second["sections"]}
    assert sections["official"]["status"] == "stale" and sections["research"]["status"] == "stale"
    notes = [fact for fact in second["facts"] if fact["section"] == "research"]
    assert [fact["label"] for fact in notes] == ["铜冠金源期货", "南华期货"]
    assert {fact["error"] for fact in notes} == {"research_notes_none_recent"}
    # Retried after the short failure interval, not after each source's long TTL.
    calls = down.calls.count("shfe_weekly")
    clock.now += SLOW_FAILURE_TTL_SECONDS + 1
    await _refreshed(desk)
    assert down.calls.count("shfe_weekly") == calls + 1

    # A restart (or the other blue-green slot) inside the stored value's TTL reuses it without fetching (#788).
    # (It serves the stored facts at once and re-reads them behind — 2026-10-22 — so look after the re-read.)
    fresh = _DownSources()
    reused, _ = _desk(sources=fresh, research=research)
    await _refreshed(reused)
    assert "shfe_weekly" not in fresh.calls
    # A restart past the TTL: a fresh desk over the same database, every source still down — stale, not lost.
    down_again = _DownSources()
    restarted, _ = _desk(sources=down_again, research=research)
    started = time.time()
    restarted._slow.wall = lambda: started + 13 * 3600
    third = await _refreshed(restarted)
    assert "shfe_weekly" in down_again.calls
    stock = next(fact for fact in third["facts"] if fact["id"] == "O.shfe_stock")
    assert stock["status"] == "stale" and stock["value"] == "74312 吨，较上周 -3834"


async def test_an_empty_page_is_a_failed_read_that_keeps_the_last_good_value() -> None:
    """#788：统计局价格页 200 空正文不是“好值”，不覆盖上次的页，事实照旧给出并标 stale。"""
    class EmptyPage(_Sources):
        async def nbs_price_page(self, url: str) -> str:
            self.calls.append("nbs_page")
            return ""

    research = _Research(db=True)
    desk, clock = _desk(sources=_Sources(), research=research)
    first = await desk.report("SHFE.rb2701")
    before = next((fact for fact in first["facts"] if fact["id"] == "O.nbs_price"), None)
    desk.sources = EmptyPage()
    clock.now += 13 * 3600
    desk._slow.wall = lambda: time.time() + 13 * 3600
    second = await _refreshed(desk)
    after = next((fact for fact in second["facts"] if fact["id"] == "O.nbs_price"), None)
    assert before is not None and "status" not in before
    assert after is not None and after["value"] == before["value"] and after["status"] == "stale"
    assert after["error"] == "nbs_page_empty"


async def test_a_cold_slow_source_failure_is_unavailable_and_not_cached_long() -> None:
    desk, clock = _desk(sources=_DownSources(), research=_Research(db=True))
    report = await desk.report("SHFE.rb2701")
    sections = {item["key"]: item for item in report["sections"]}
    assert sections["research"]["status"] == "unavailable" and sections["research"]["error"] == "research_notes_none_recent"
    assert not any(fact["id"] == "O.shfe_stock" for fact in report["facts"])
    desk.sources = _Sources()
    clock.now += SLOW_FAILURE_TTL_SECONDS + 1
    recovered = await _refreshed(desk)
    assert "status" not in next(fact for fact in recovered["facts"] if fact["id"] == "O.shfe_stock")


async def test_a_deferred_rejudge_that_cannot_run_says_why_instead_of_passing_for_done() -> None:
    """#516：重新判断等资料到齐（deferred）后因为当日额度用完没跑，判断上带回执 rejudgeSkipped，界面不再报“已重新判断”。"""
    now = _Now()
    desk, _ = _desk(now=now, daily_calls=1)
    generator = _Generator()
    await desk.report("SHFE.rb2701", generator=generator)
    await _settle(desk)
    assert (await desk.report("SHFE.rb2701"))["judgment"]["status"] == "ready"
    now.value += timedelta(hours=2)
    facts = desk._facts["SHFE.rb"][2]
    loading = {**facts, "sections": [{**facts["sections"][0], "status": "loading"}, *facts["sections"][1:]]}
    _, outcome = desk._judgment("SHFE.rb", loading, generator, True)
    assert outcome == "started"  # deferred until the loading section lands

    async def collect(key: str, exchange: str, product: str) -> dict[str, Any]:
        return facts
    desk._collect = collect  # type: ignore[method-assign]
    await desk._loaded("SHFE.rb", "SHFE", "rb")
    judgment = (await desk.report("SHFE.rb2701"))["judgment"]
    assert judgment["status"] == "ready"
    assert judgment["rejudgeSkipped"] == {"status": "limited", "message": "今天的判断次数已用完，明天 08:30 后再判"}
    assert len(generator.inputs) == 1


class _ForeignLive(_Live):
    """The live service with the foreign quote board (the 外盘 page's rows, MT5 first)."""

    def foreign_markets(self, product: str | None = None) -> dict[str, Any]:
        assert product is None
        return {"status": "ok", "items": [
            {"code": "CME.NQ", "name": "迷你纳指100", "price": 29900.75, "changePercent": -0.42, "unit": "点",
             "source": "MT5（AMP）", "quotedAt": "2026-09-28 14:58"},
            {"code": "COMEX.GC", "name": "COMEX黄金", "price": 4180.4, "changePercent": 0.31, "unit": "美元/盎司",
             "source": "MT5（AMP）", "quotedAt": "2026-09-28 14:58"}]}


async def test_a_foreign_product_has_its_own_desk_market_domestic_counterpart_and_news() -> None:
    """2026-10-21 所有者：打开迷你纳指，调研显示不可用（研究台只认国内合约目录）。所有者定外盘三块：行情和走势（外盘报价
    + 快期外盘主连日线）、对应的国内品种（有的话）、按外盘名字搜的报道；没有国内对照的不出那一块，也没有涨跌停、持仓变化。"""
    desk, _ = _desk()
    desk.extra = _ForeignLive()  # type: ignore[assignment]
    nasdaq = await desk.report("KQD.m@CME.NQ")
    assert nasdaq["productName"] == "迷你纳指100" and nasdaq["mainSymbol"] == "KQD.m@CME.NQ"
    assert [section["key"] for section in nasdaq["sections"]] == ["market", "report"]
    price = next(fact for fact in nasdaq["facts"] if fact["id"] == "M.price")
    assert price["label"] == "迷你纳指100 最新价（点）" and price["source"] == "MT5（AMP）" and "29900.75" in price["value"]
    assert any(fact["id"] == "M.trend" for fact in nasdaq["facts"])
    assert not any(fact["id"] in {"M.limit", "M.position"} for fact in nasdaq["facts"])
    # 月份合约与主连是同一个品种、同一份研究；COMEX黄金带出国内对应的沪金
    gold = await desk.report("CME.GCEZ26")
    assert [section["key"] for section in gold["sections"]] == ["market", "related", "report"]
    related = [fact for fact in gold["facts"] if fact["section"] == "related"]
    assert related and "沪金" in related[0]["label"] and "国内对应品种" in related[0]["label"]
    assert gold is not nasdaq and (await desk.report("KQD.m@COMEX.GC"))["product"] == "COMEX.GC"


class _FlashedForeignLive(_ForeignLive):
    """The same board plus one screened 纳指 快讯 pick."""

    def flash_feed(self, *args: Any) -> dict[str, Any]:
        return {"status": "ok", "items": []}

    def flash_pick(self, product: str) -> list[dict[str, Any]]:
        assert product == "nasdaq"
        return [{"id": "f1", "publishedAt": "2026-09-28T14:30:00+08:00", "sources": ["财联社"],
                 "text": "美股股指期货扩大涨幅，纳指期货涨超1%", "link": None}]


async def test_a_foreign_product_without_a_domestic_counterpart_is_judged_on_its_market_news_and_flash() -> None:
    """2026-10-21 回读：迷你纳指调研只有行情与报道两块，判断门槛（至少三类依据）永远够不着、一直“资料不足”。
    筛过的快讯本来就是判断的输入，算作一类依据：行情 + 报道 + 快讯就开始判断。"""
    desk, _ = _desk()
    desk.extra = _FlashedForeignLive()  # type: ignore[assignment]
    report = await desk.report("KQD.m@CME.NQ", generator=_Generator())
    assert report["judgment"]["status"] == "pending"
    assert any(fact["section"] == "flash" for fact in report["facts"])
    await _settle(desk)


def test_research_data_says_foreign_products_have_none_of_the_domestic_datasets() -> None:
    from trader_runtime.research_data import FOREIGN_DATA_NOTICE, ResearchDataService
    service = ResearchDataService.__new__(ResearchDataService)
    service.clock = lambda: datetime(2026, 9, 28, 7, 0, tzinfo=UTC)  # type: ignore[method-assign]
    summary = service.summary("KQD.m@CME.NQ")
    assert summary["foreign"] is True and summary["notice"] == FOREIGN_DATA_NOTICE
    assert all(item["error"] == "research_foreign_not_applicable" for item in summary["datasets"].values())


async def test_the_panel_never_waits_for_a_collection(monkeypatch: pytest.MonkeyPatch) -> None:
    """调研台一秒内打开 (所有者 2026-10-22)：面板的读取不等资料收集。一次都没收集过的品种等一小会儿，没好就先给
    “加载中”的各块并标 factsRefreshing（客户端随后再读），判断等资料到了才开始；收集过的资料存进共享表，重启或另一个
    槽位立刻给出，旧了（超过 30 分钟）照样先给、后台重读，旧资料上不开新判断。"""
    monkeypatch.setattr("trader_runtime.research_desk.FIRST_READ_WAIT_SECONDS", 0.0)
    research = _Research(db=True)
    now = _Now()
    desk, _ = _desk(research=research, now=now)
    generator = _Generator()
    first = await desk.report("SHFE.rb2701", generator=generator, fast=True)
    assert first["factsRefreshing"] is True and first["facts"] == []
    assert {section["status"] for section in first["sections"]} == {"loading"}
    assert first["judgment"]["status"] != "ready" and not generator.inputs and "SHFE.rb" not in desk._judging
    await _collected(desk)
    await _settle(desk)
    assert generator.inputs  # the deferred judgment started once the facts landed
    second = await desk.report("SHFE.rb2701", fast=True)
    assert "factsRefreshing" not in second and second["facts"] and second["productName"] == "螺纹钢"

    # A restart two hours later: the stored facts at once, flagged and re-read behind; no judgment on them.
    now.value += timedelta(hours=2)
    restarted, _ = _desk(research=research, now=now)
    later = _Generator()
    served = await restarted.report("SHFE.rb2701", generator=later, fast=True)
    assert served["factsRefreshing"] is True and served["facts"] == second["facts"]
    assert not later.inputs and "SHFE.rb" not in restarted._judging
    await _collected(restarted)
    again = await restarted.report("SHFE.rb2701", fast=True)
    assert "factsRefreshing" not in again


async def test_price_fact_inherits_stale_release_directory_even_when_price_page_refreshes() -> None:
    class IndexDown(_Sources):
        async def nbs_releases(self):
            self.calls.append("nbs")
            raise SourceError("nbs_timeout")

    research = _Research(db=True)
    desk, clock = _desk(sources=_Sources(), research=research)
    try:
        before = next(fact for fact in (await desk.report("SHFE.rb2701"))["facts"] if fact["id"] == "O.nbs_price")
        sources = IndexDown()
        desk.sources = sources
        clock.now += 13 * 3600
        desk._slow.wall = lambda: time.time() + 13 * 3600
        second = await _refreshed(desk)
        after = next(fact for fact in second["facts"] if fact["id"] == "O.nbs_price")
        assert "nbs_page" in sources.calls  # the historical price page itself refreshed successfully
        assert after["value"] == before["value"] and after["status"] == "stale"
        assert after["error"] == "nbs_timeout" and after["fetchedAt"]
        assert next(section for section in second["sections"] if section["key"] == "official")["status"] == "stale"
    finally:
        await desk.close()
        research.connection.close()


def test_linkage_fact_correlates_each_related_product_and_reads_the_basket_state() -> None:
    """关联品种联动度 (linkage.py, adapted from Vibe-Trading's regime.py): per-pair correlation of daily returns over
    the last 20 shared days, and the hysteresis 抱团 state of the whole basket."""
    import math as _math

    from trader_runtime import linkage
    from trader_runtime.research_desk import linkage_fact

    def bench(values: list[float]) -> dict[str, Any]:
        return {"bars": [{"datetime": index, "tradingDay": f"2026-{9 + index // 28:02d}-{index % 28 + 1:02d}", "close": value}
                         for index, value in enumerate(values)]}

    wave = [100 + 5 * _math.sin(index / 3) + index * 0.1 for index in range(60)]
    echo = [value * 2 + 1 for value in wave]
    noise = [100 + (7 if index % 3 == 0 else -3 if index % 3 == 1 else 1) for index in range(60)]
    fact = linkage_fact("螺纹钢", bench(wave), [("铁矿石", bench(echo)), ("焦炭", bench(noise))])
    assert fact is not None and fact["id"] == "R.linkage" and fact["asOf"] == "2026-11-04"
    assert fact["value"].startswith("近 20 个交易日日收益相关系数：与铁矿石 1.00、与焦炭 ")
    assert fact["value"].endswith("；3 个品种没有抱团")  # one tight pair of three: density 1/3 < 0.65
    together = linkage_fact("螺纹钢", bench(wave), [("铁矿石", bench(echo)), ("热卷", bench([v + 50 for v in wave]))])
    assert together is not None and together["value"].endswith("3 个品种整体抱团（至少自 09-21 起）")
    # Too few shared days, or a contract that never moves: no fact rather than a guess.
    assert linkage_fact("螺纹钢", bench(wave[:20]), [("铁矿石", bench(echo[:20]))]) is None
    assert linkage_fact("螺纹钢", bench(wave), [("铁矿石", bench([100.0] * 60))]) is None
    # Hysteresis: opens at 0.65, holds through 0.5, closes at 0.45.
    states = linkage.regimes([None, 0.7, 0.5, 0.5, 0.4, 0.4], smooth=1)
    assert [fused for _, fused in states] == [False, True, True, True, False, False]
