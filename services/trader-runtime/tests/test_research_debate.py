"""多 AI 研讨: four spoken turns one after another (streamed), the program's sentence check, the anonymous verdict,
caching and limits, writing back through the desk, and the frozen five-day reckoning. A scripted adapter stands in
for the models."""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import threading
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from trader_runtime.data_providers import FakeReadOnlyDataProvider
from trader_runtime.native_agent import NativeAgentError
from trader_runtime.research_debate import (
    TURN_LIMIT,
    USAGE_USER,
    DebateUnavailable,
    DebateVerdict,
    EventProductError,
    ResearchDebateService,
    SearchPlan,
    check_turn,
    confidence,
    settle_outcome,
    sheets,
)
from trader_runtime.research_desk import ResearchDeskService, debate_entry
from trader_runtime.web_search import WebSearchError

FACTS: list[dict[str, Any]] = [
    {"id": "M.price", "section": "market", "kind": "data", "label": "主力 RB2610 最新价", "value": "3100，日涨跌 +0.55%",
     "source": "快期行情", "asOf": "2026-10-09T10:58+08:00"},
    {"id": "M.trend", "section": "market", "kind": "data", "label": "近20个交易日", "value": "涨跌 -1.78%，振幅 4.2%，5日均线在20日均线下方",
     "source": "快期行情", "asOf": "2026-10-09"},
    {"id": "M.indicators", "section": "market", "kind": "data", "label": "日线指标", "value": "RSI 29（超卖），布林下轨附近",
     "source": "快期行情", "asOf": "2026-10-09"},
    {"id": "K.levels", "section": "market", "kind": "data", "label": "近20个交易日收盘价", "value": "最高 3200，最低 3060，均价 3130",
     "source": "快期行情", "asOf": "2026-10-09"},
    {"id": "D.position_rank", "section": "fundamental", "kind": "data", "label": "前20会员净多持仓",
     "value": "+107000（多 500000，空 393000）", "source": "AKShare", "asOf": "20261009"},
    {"id": "D.spot_basis", "section": "fundamental", "kind": "data", "label": "现货与基差", "value": "现货 3192，主力基差 -92",
     "source": "AKShare", "asOf": "20261009"},
    {"id": "D.warehouse_receipt", "section": "fundamental", "kind": "data", "label": "仓单", "value": "191000，较上日 +5000",
     "source": "AKShare", "asOf": "20261009"},
    {"id": "N.ab12cd34", "section": "flash", "kind": "report", "label": "快讯", "value": "10:40 财联社：钢厂检修增加",
     "source": "跨源快讯", "asOf": "2026-10-09T10:40:00+08:00"},
]
EVENT = {"id": "e0123456789ab", "title": "多地优化住房公积金政策", "text": "多地积极优化调整住房公积金政策 房地产市场成交活跃",
         "publishedAt": "2026-10-09T11:00:00+08:00", "tier": "major", "reports": 3, "sources": ["财联社", "新浪"],
         "ai": {"direction": "偏多", "strength": "中", "transmission": "成交回暖带动用钢预期"},
         "items": [{"id": "i1", "link": "https://example.com/1"}]}
NOW = datetime(2026, 10, 9, 3, 0, tzinfo=UTC)  # 11:00 Beijing, a Friday

# Each side's two turns; {fact id} becomes that fact's [F n] marker from the material the model was given.
BULL_TURNS = ("RSI 29 进入超卖，空头动能在减弱{M.indicators}。前20会员净多 +107000，资金偏多{D.position_rank}。\n总体看法：偏多",
              "你说仓单在增，那是检修期的季节性，RSI 29 说明跌得够多{M.indicators}。\n总体看法：偏多")
BEAR_TURNS = ("仓单较上日 +5000，库存还在累{D.warehouse_receipt}。现货基差 -92，现货比盘面弱{D.spot_basis}。"
              "去年同期螺纹跌了 8%{M.trend}。\n总体看法：偏空",  # 8 is in no fact: 未核对
              "净多来自换月移仓，不是新资金{D.position_rank}。\n总体看法：偏空")


class _Result:
    def __init__(self, value: Any, tokens: int) -> None:
        self.value, self.tokens = value, tokens


# An analyst's report: its first fact cited (the marker filled in from the analyst's own 资料), a key point.
REPORT = "结论：多空交织\n{fact}说明资金在观望，供需的变化还没有传到价格上。\n要点：\n资金：观望"


class _Models:
    """The adapter, scripted per model: each side's turns in order, streamed in two pieces. A turn that starts
    while another is still being written fails the test (the turns are one after another). The analysts (their own
    instructions) answer from `reports` (analyst title → text or exception), recorded apart in `analyst_calls`."""
    agent_authenticated = True

    def __init__(self, *, verdict: Any = None, bull: tuple[Any, Any] = BULL_TURNS, bear: tuple[Any, Any] = BEAR_TURNS,
                 endpoints: set[str] | None = None, reports: dict[str, Any] | None = None, plan: Any = None) -> None:
        self.script = {"deepseek-flash": bull, "glm-5.3-flash": bear}
        self.verdict = verdict
        self.endpoints = {name: object() for name in (endpoints or {"deepseek-flash", "judge-fixture", "glm-5.3-flash"})}
        self.calls: list[dict[str, Any]] = []
        self.analyst_calls: list[dict[str, Any]] = []
        self.reports = reports or {}
        self.plan = plan
        self.writing = 0  # analysts writing at this moment
        self.most_writing = 0
        self.spoken: dict[str, int] = {}
        self.speaking = False
        self.hold: asyncio.Event | None = None  # given: the third turn waits after its first piece until it is set

    def _record(self, **call: Any) -> None:
        self.calls.append({"model": call["model"], "input": call["input_text"], "instructions": call["instructions"],
                           "user": call["usage_user"], "max": call["max_output_tokens"], "timeout": call["timeout_seconds"],
                           "light": call["light_reasoning"]})

    async def _analyst(self, on_text: Any, call: dict[str, Any]) -> Any:
        self.analyst_calls.append({"input": call["input_text"], "instructions": call["instructions"], "max": call["max_output_tokens"]})
        title = re.search(r"里的(.+?)分析师", call["instructions"])[1]  # type: ignore[index]
        answer = self.reports.get(title, REPORT)
        if isinstance(answer, BaseException):
            raise answer
        rows = json.loads(call["input_text"].split("\n资料：", 1)[1].split("\n", 1)[0])
        first = next((row for row in rows if row["id"] != "M.price"), rows[0])
        web = next((row for row in rows if row["id"].startswith("W.")), None)  # {W}: the first web item it was given
        text = answer.replace("{fact}", f"{first['label']}是 {str(first['value']).split('，')[0]}[F{first['n']}]").replace(
            "{W}", f"[F{web['n']}]" if web else "")
        self.writing += 1
        self.most_writing = max(self.most_writing, self.writing)
        try:
            await on_text(text[: len(text) // 2])
            await asyncio.sleep(0)  # the other analysts write meanwhile
            await on_text(text)
        finally:
            self.writing -= 1
        return _Result(text, 300)

    async def stream_text(self, *, on_text: Any = None, **call: Any) -> Any:
        if "分析师，为接下来" in call["instructions"]:
            return await self._analyst(on_text, call)
        self._record(**call)
        assert not self.speaking, "two turns at once"
        model = str(call["model"])
        index = self.spoken.get(model, 0)
        answer = self.script[model][index % 2]  # a second debate speaks the same turns again
        if isinstance(answer, BaseException):
            raise answer  # the same turn again on a retry
        self.spoken[model] = index + 1
        numbers = {row["id"]: row["n"] for row in json.loads(call["input_text"].split("\n议题：", 1)[0].removeprefix("底稿："))["底稿"]}
        text = re.sub(r"\{([A-Z]\.[a-z_0-9]+)\}", lambda found: f"[F{numbers[found[1]]}]", answer)
        self.speaking = True
        try:
            await asyncio.sleep(0)  # a real model's reply takes a while: the loop runs other work meanwhile
            await on_text(text[: len(text) // 2])
            if self.hold is not None and len([call for call in self.calls if "交锋记录" in call["input"] or "开场" in call["input"]]) == 3:
                await self.hold.wait()  # the third turn stops after its first piece until released
            await on_text(text)
        finally:
            self.speaking = False
        return _Result(text, 400)

    async def complete_structured(self, *, schema: type, **call: Any) -> Any:
        if schema is SearchPlan:
            self.analyst_calls.append({"input": call["input_text"], "instructions": call["instructions"], "plan": True})
            return _Result(self.plan(call["input_text"]) if callable(self.plan) else self.plan or SearchPlan(), 100)
        self._record(**call)
        assert schema is DebateVerdict
        if isinstance(self.verdict, Exception):
            raise self.verdict
        return _Result(self.verdict(call["input_text"]) if callable(self.verdict) else self.verdict, 900)


def _cases(input_text: str) -> dict[str, list[dict[str, Any]]]:
    return json.loads(input_text.rsplit("\n", 1)[-1])


def verdict_for_bull(input_text: str) -> DebateVerdict:
    """The judge sees A/B: find which letter carries the bull's RSI sentence and let it win by a little."""
    cases = _cases(input_text)
    bull = "A" if any("RSI" in item["text"] for item in cases["A 方"]) else "B"
    bear = "B" if bull == "A" else "A"
    return DebateVerdict.model_validate({
        "winner": bull, "margin": "略", "headline": "超卖叠加净多，短线略偏多",
        "reason": f"{bull} 方的超卖有数据，{bear} 方的仓单也在增，但还不够把价格压下去",
        "adopted": [{"id": f"{bull}1", "reason": "超卖有数据"}, {"id": f"{bear}1", "reason": "仓单在增"}, {"id": "Z9", "reason": "x"}],
        "rejected": [{"id": f"{bull}2", "reason": "换月移仓"}],
        "swing": "仓单增加会不会持续", "change_mind": "收盘跌破 3060 且仓单继续增加", "invalidation": 3060,
        "watch": ["下周仓单", "买入螺纹"]})  # the second watch item is advice: dropped


class _History(FakeReadOnlyDataProvider):
    closes: list[float] = []

    def market_history(self, symbol: str, duration_seconds: int, length: int) -> dict[str, Any]:
        start = datetime(2026, 10, 9, tzinfo=UTC) - timedelta(hours=8)
        bars = [{"datetime": int((start + timedelta(days=index)).timestamp() * 1e9), "close": close, "tradingDay": day}
                for index, (day, close) in enumerate(zip(
                    ["2026-10-09", "2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16"],
                    [3100.0, *self.closes], strict=False))]
        return {"bars": bars}


class _Research:
    def __init__(self) -> None:
        self.connection = sqlite3.connect(":memory:", check_same_thread=False)
        self.lock = threading.Lock()
        self.sources = type("Sources", (), {"trading_days": staticmethod(
            lambda: frozenset({"20261009", "20261012", "20261013", "20261014", "20261015", "20261016"}))})()


class _Now:
    def __init__(self) -> None:
        self.value = NOW

    def __call__(self) -> datetime:
        return self.value


def _service(models: _Models, *, daily_runs: int = 30, judgment: dict[str, Any] | None = None,
             history: _History | None = None, fallbacks: dict[str, str] | None = None,
             ) -> tuple[ResearchDebateService, ResearchDeskService, _Now]:
    now = _Now()
    research = _Research()
    provider = history or _History()
    desk = ResearchDeskService(research=research, news=lambda symbol: asyncio.sleep(0, {}), provider=lambda: provider,  # type: ignore[arg-type]
                               now=now)
    material_calls: list[str] = []

    async def material(symbol: str, **_: Any) -> dict[str, Any]:
        material_calls.append(symbol)
        return {"symbol": symbol, "mainSymbol": "SHFE.rb2610", "exchange": "SHFE", "product": "rb", "productName": "螺纹钢",
                "facts": [dict(fact) for fact in FACTS], "sections": [], "price": 3100.0, "researchDay": desk.research_day(now()),
                "judgment": {"researchDay": desk.research_day(now()), **(judgment or {
                    "status": "ready", "stance": "震荡", "confidence": "中", "headline": "超卖、去库放缓",
                    "generatedAt": "2026-10-09T02:22:00+00:00"})}}
    desk.debate_material = material  # type: ignore[method-assign]
    service = ResearchDebateService(desk=desk, connection=research.connection, lock=research.lock,
                                    events=lambda product: ("螺纹钢", [EVENT]),
                                    event_products=lambda event_id: ["rb"] if event_id == EVENT["id"] else [],
                                    now=now, daily_runs=daily_runs,
                                    models={"bull": "deepseek-flash", "bear": "glm-5.3-flash", "judge": "judge-fixture"},
                                    # The switching tests keep a distinct stand-in (Kimi) so they can tell it from the bull.
                                    fallbacks=fallbacks if fallbacks is not None else {"bear": "k3.1-pro"})
    service.generator = lambda: models
    service.rng.seed(7)
    return service, desk, now


async def _finish(service: ResearchDebateService) -> None:
    await asyncio.gather(*list(service._tasks.values()))


def _sentences(debate: dict[str, Any], n: int) -> list[dict[str, Any]]:
    return debate["turns"][n - 1]["sentences"]


async def test_four_turns_in_order_each_seeing_the_exchange_then_an_anonymous_verdict_mapped_back() -> None:
    models = _Models(verdict=verdict_for_bull)
    service, _, _ = _service(models)
    started = await service.start("SHFE.rb2701", user_id="u1")
    assert started["status"] == "queued" and started["reused"] is False and started["topic"]["kind"] == "today"
    await _finish(service)
    debate = service.get(started["id"])
    assert debate is not None and debate["status"] == "done"
    # The bull opens, the bear answers, the bull rebuts, the bear rebuts, then the judge — all bounded and booked
    # to the platform marker.
    assert [call["model"] for call in models.calls] == ["deepseek-flash", "glm-5.3-flash", "deepseek-flash", "glm-5.3-flash", "judge-fixture"]
    assert [(turn["n"], turn["side"]) for turn in debate["turns"]] == [(1, "bull"), (2, "bear"), (3, "bull"), (4, "bear")]
    assert {call["user"] for call in models.calls} == {USAGE_USER}
    assert all(call["max"] and call["timeout"] and call["light"] for call in models.calls)
    assert [call["max"] for call in models.calls] == [6000] * 4 + [8000]
    # Each side sees the whole exchange so far (a 未核对 sentence marked), the opener nothing.
    inputs = [call["input"] for call in models.calls[:4]]
    assert "交锋记录" not in inputs[0]
    assert "多方：RSI 29 进入超卖，空头动能在减弱[F7]。" in inputs[1]
    assert "空方：仓单较上日 +5000" in inputs[2] and "去年同期螺纹跌了 8%[F6]。（未核对）" in inputs[2]
    assert inputs[3].count("多方：") == 2 and "你说仓单在增" in inputs[3]
    assert "总体看法" not in "".join(inputs)  # a side's own view never reaches the other side
    # Every request starts with the same 底稿 text (the providers' prefix caches can hit).
    head = models.calls[0]["input"].split("\n议题：", 1)[0]
    assert all(call["input"].startswith(head) for call in models.calls)
    # The judge sees A and B only: no side names, no model names, no 多1 / 空1, no markers.
    judge = models.calls[-1]["input"]
    assert "A 方" in judge and "B 方" in judge
    assert not any(word in judge.rsplit("\n", 1)[-1] for word in ("多方", "空方", "多1", "空1", "deepseek", "glm", "[F"))
    assert set(debate["mapping"].values()) == {"bull", "bear"}
    verdict = debate["verdict"]
    assert verdict["stance"] == "略偏多" and verdict["winner"] == "bull" and verdict["margin"] == "略"
    # A/B ids come back as 多/空; an unknown id is dropped; the reason paragraph names the real sides.
    assert [item["id"] for item in verdict["adopted"]] == ["多1", "空1"] and [item["id"] for item in verdict["rejected"]] == ["多2"]
    assert verdict["reason"] == "多方的超卖有数据，空方的仓单也在增，但还不够把价格压下去"
    assert verdict["invalidation"] == 3060 and verdict["watch"] == ["下周仓单"]
    assert verdict["confidence"] in ("低", "中", "高") and verdict["confidenceBasis"]["refuted"] == 1
    assert [step["key"] for step in debate["steps"]][:2] == ["collect", "analysts"]
    assert [step["status"] for step in debate["steps"]] == ["done"] * 7
    # The four analysts' reports (300 tokens each) count with the turns and the verdict.
    assert debate["tokens"] == 4 * 300 + 4 * 400 + 900 and len(debate["calls"]) == 9 and debate["compare"]["stance"] == "震荡"
    # Each side's own view (the reckoning's lean) is its last turn's 总体看法 line, never shown in the turn.
    assert debate["leans"] == {"bull": "偏多", "bear": "偏空"}
    assert all("总体看法" not in turn["text"] for turn in debate["turns"])


async def test_the_verdicts_own_words_name_the_real_sides_never_a_or_b() -> None:
    # #768 A: the judge writes "A方" / "A1"; the reader sees 空方 / 空1. A=bear here, so a plain letter swap is wrong.
    def verdict(input_text: str) -> DebateVerdict:
        return DebateVerdict.model_validate({
            "winner": "B", "margin": "略", "headline": "A方仓单论据被驳，B 方超卖逻辑更站得住",
            "adopted": [{"id": "B1", "reason": "超卖有数据，A3 没驳倒"}, {"id": "A1", "reason": "仓单在增，已被B1 捕捉"}],
            "rejected": [{"id": "B2", "reason": "换月移仓，A4 说得对"}],
            "swing": "A方说的仓单增加会不会持续", "change_mind": "收盘跌破 3060 且 A 方的仓单继续增加", "invalidation": 3060,
            "watch": ["B方提到的超卖修复", "A股情绪"]})
    service, _, _ = _service(_Models(verdict=verdict))
    service.rng = type("Rng", (), {"random": staticmethod(lambda: 0.9)})()  # type: ignore[assignment]  # A = bear
    debate_id = (await service.start("SHFE.rb2701", user_id="u1"))["id"]
    await _finish(service)
    debate = service.get(debate_id)
    assert debate is not None and debate["mapping"] == {"A": "bear", "B": "bull"} and debate["status"] == "done"
    result = debate["verdict"]
    assert result["winner"] == "bull" and result["headline"] == "空方仓单论据被驳，多方超卖逻辑更站得住"
    assert [(item["id"], item["reason"]) for item in result["adopted"]] == [
        ("多1", "超卖有数据，空3 没驳倒"), ("空1", "仓单在增，已被多1 捕捉")]
    assert result["rejected"] == [{"id": "多2", "side": "bull", "reason": "换月移仓，空4 说得对"}]
    assert result["swing"] == "空方说的仓单增加会不会持续" and result["changeMind"] == "收盘跌破 3060 且 空方的仓单继续增加"
    assert result["watch"] == ["多方提到的超卖修复", "A股情绪"]  # a bare letter (A股) is not a side
    shown = json.dumps({key: result[key] for key in ("headline", "adopted", "rejected", "swing", "changeMind", "watch")},
                       ensure_ascii=False)
    assert not any(code in shown for code in ("A方", "B方", "A 方", "B 方", "A1", "B1", "B2", "A3"))
    # The judge's own words stay as written in the record.
    assert "A方仓单论据被驳" in json.dumps(debate["calls"][-1]["output"], ensure_ascii=False)


async def test_an_unchecked_figure_is_marked_stays_visible_and_never_reaches_the_judge() -> None:
    models = _Models(verdict=verdict_for_bull)
    service, _, _ = _service(models)
    debate_id = (await service.start("SHFE.rb2701", user_id="u1"))["id"]
    await _finish(service)
    debate = service.get(debate_id)
    assert debate is not None
    bear = _sentences(debate, 2)
    assert [item["id"] for item in bear] == ["空1", "空2", "空3"] and [item["verified"] for item in bear] == [True, True, False]
    assert bear[2] == {"id": "空3", "side": "bear", "text": "去年同期螺纹跌了 8%[F6]。", "factIds": ["M.trend"], "verified": False,
                       "problem": "这个数在所引资料里找不到"}
    # Sentence ids go on across a side's turns; the markers name the cited facts.
    assert [item["id"] for item in _sentences(debate, 3)] == ["多3"] and _sentences(debate, 3)[0]["factIds"] == ["M.indicators"]
    assert "去年同期" not in models.calls[-1]["input"]
    assert debate["checks"] == {"rounds": 2, "arguments": 7, "unverified": 1, "cited": 4}


def test_the_sentence_check() -> None:
    by_id = {fact["id"]: fact for fact in FACTS}
    by_n = {7: "M.indicators", 3: "D.warehouse_receipt"}
    checked = check_turn("bull", "超卖不等于见底。RSI 29[F7]。仓单 +5000。【F3】 建议买入。RSI 29[F99]。", 4, by_n, by_id)
    assert [(item["id"], item["verified"], item.get("problem")) for item in checked] == [
        ("多4", True, None),                       # no figure, no citation: an argument the judge may weigh
        ("多5", True, None),                       # its number is in the fact it cites
        ("多6", True, None),                       # a marker after the stop belongs to that sentence (full-width too)
        ("多7", False, "含交易建议"),
        ("多8", False, "引用的资料编号不存在")]
    assert checked[2]["factIds"] == ["D.warehouse_receipt"]
    # A turn that runs past TURN_LIMIT is cut at a sentence and ends in "…"; length never marks a sentence 未核对.
    long = check_turn("bear", "仓单在增。" * 200, 1, by_n, by_id)
    assert all(item["verified"] for item in long) and sum(len(item["text"].strip("。…")) for item in long) <= TURN_LIMIT
    assert long[-1]["text"].endswith("…") and len(long) < 200


@pytest.mark.parametrize("code", ["model_output_invalid", "model_output_empty"])
async def test_an_unreadable_verdict_needs_review_without_a_default_tier(code: str) -> None:
    # A verdict that does not fit the schema, or that came back cut off / empty, gets no default tier either way.
    models = _Models(verdict=NativeAgentError(code))
    service, _, _ = _service(models)
    debate_id = (await service.start("SHFE.rb2701", user_id="u1"))["id"]
    await _finish(service)
    debate = service.get(debate_id)
    assert debate is not None and debate["status"] == "review" and debate["review"] is True and debate["verdict"] is None
    # 需复核 is never counted: a terminal void, and nobody's view of this run enters the record.
    assert debate["settlement"]["status"] == "void" and debate["settlement"]["stance"] is None
    assert debate["settlement"]["leans"] == {} and debate["settlement"]["deskStance"] is None
    assert service.stats()["pending"] == 0
    with pytest.raises(ValueError, match="research_debate_not_done"):
        service.adopt(debate_id)


async def test_a_failed_side_fails_the_debate_and_deepseek_is_retried_once() -> None:
    models = _Models(verdict=verdict_for_bull, bull=(RuntimeError("model_output_invalid"), BULL_TURNS[1]))
    service, _, _ = _service(models)
    debate_id = (await service.start("SHFE.rb2701", user_id="u1"))["id"]
    await asyncio.gather(*list(service._tasks.values()), return_exceptions=True)
    debate = service.get(debate_id)
    assert debate is not None and debate["status"] == "failed" and "多方模型 deepseek-flash" in debate["error"]
    assert [call["model"] for call in models.calls].count("deepseek-flash") == 2  # the one retry
    assert [call.get("error") for call in debate["calls"] if call["role"] == "bull"] == ["model_output_invalid"] * 2


def _http_failure(code: str, status: int) -> NativeAgentError:
    error = NativeAgentError(code)
    error.__cause__ = type("APIStatusError", (Exception,), {"status_code": status})()
    return error


def _with_kimi(models: _Models, bear: tuple[Any, Any] = BEAR_TURNS) -> _Models:
    models.script["k3.1-pro"] = bear
    models.endpoints["k3.1-pro"] = object()
    return models


@pytest.mark.parametrize(("failure", "reason"), [
    (NativeAgentError("model_rate_limited"), "model_rate_limited"),
    (NativeAgentError("model_auth_failed"), "model_auth_failed"),
    (TimeoutError(), "TimeoutError"),
    (_http_failure("model_request_failed", 503), "model_request_failed"),
    (NativeAgentError("model_stream_interrupted"), "model_stream_interrupted"),
])
async def test_an_unavailable_bear_hands_the_debate_to_its_fallback(failure: BaseException, reason: str) -> None:
    """所有者 2026-10-12：空方 GLM 不可用（限流、认证、超时、上游 5xx、断流）时自动换 Kimi K3，本场后续都用它；
    记录里写清实际模型和切换原因，界面显示的 models 是实际说话的那个。"""
    models = _with_kimi(_Models(verdict=verdict_for_bull, bear=(failure, BEAR_TURNS[1])))
    service, _, _ = _service(models)
    debate_id = (await service.start("SHFE.rb2701", user_id="u1"))["id"]
    await _finish(service)
    debate = service.get(debate_id)
    assert debate is not None and debate["status"] == "done" and debate["verdict"] is not None
    assert debate["models"] == {"bull": "deepseek-flash", "bear": "k3.1-pro", "judge": "judge-fixture", "analyst": "deepseek-flash"}
    assert debate["fallbacks"] == [{"role": "bear", "stage": "turn2", "from": "glm-5.3-flash", "to": "k3.1-pro", "reason": reason}]
    bear_calls = [call for call in debate["calls"] if call["role"] == "bear"]
    assert [(call["stage"], call["model"], call.get("error")) for call in bear_calls] == [
        ("turn2", "glm-5.3-flash", reason), ("turn2", "k3.1-pro", None), ("turn4", "k3.1-pro", None)]
    # The stand-in calls say whom they replaced and why; GLM is not asked again in its second turn.
    assert all(call["fallbackFrom"] == "glm-5.3-flash" and call["fallbackReason"] == reason for call in bear_calls[1:])
    assert [call["model"] for call in models.calls].count("glm-5.3-flash") == 1
    assert all(call["light"] for call in models.calls if call["model"] == "k3.1-pro")


async def test_a_deepseek_judge_retries_once_before_the_debate_fails() -> None:
    """裁判 DeepSeek 一次断流重试一次；两次都断（没有兜底时）才失败。"""
    failures = [NativeAgentError("model_stream_interrupted")]

    def flaky(input_text: str) -> DebateVerdict:
        if failures:
            raise failures.pop()
        return verdict_for_bull(input_text)
    models = _Models(verdict=flaky)
    service, _, _ = _service(models)
    service.models["judge"] = "deepseek-flash"
    debate_id = (await service.start("SHFE.rb2701", user_id="u1"))["id"]
    await _finish(service)
    debate = service.get(debate_id)
    assert debate is not None and debate["status"] == "done" and debate["fallbacks"] == []
    assert [call.get("error") for call in debate["calls"] if call["role"] == "judge"] == ["model_stream_interrupted", None]
    always = _Models(verdict=NativeAgentError("model_stream_interrupted"))
    service, _, _ = _service(always)
    service.models["judge"] = "deepseek-flash"
    debate_id = (await service.start("SHFE.rb2701", user_id="u1"))["id"]
    await asyncio.gather(*list(service._tasks.values()), return_exceptions=True)
    failed = service.get(debate_id)
    assert failed is not None and failed["status"] == "failed"
    assert [call.get("error") for call in failed["calls"] if call["role"] == "judge"] == ["model_stream_interrupted"] * 2


@pytest.mark.parametrize("failure", [
    NativeAgentError("model_output_invalid"), NativeAgentError("model_output_empty"),
    _http_failure("model_request_failed", 400)])
async def test_a_bad_answer_or_a_malformed_request_does_not_switch_models(failure: BaseException) -> None:
    """输出不合法是提示词或解析问题，请求被判为格式错误（400）也不是模型不可用：都不换模型，照原样报错。"""
    models = _with_kimi(_Models(verdict=verdict_for_bull, bear=(failure, BEAR_TURNS[1])))
    service, _, _ = _service(models)
    debate_id = (await service.start("SHFE.rb2701", user_id="u1"))["id"]
    await asyncio.gather(*list(service._tasks.values()), return_exceptions=True)
    debate = service.get(debate_id)
    assert debate is not None and debate["status"] == "failed"
    assert debate["error"] == f"空方模型 glm-5.3-flash 没有完成（{failure}）"
    assert debate["fallbacks"] == [] and debate["models"]["bear"] == "glm-5.3-flash"
    assert "k3.1-pro" not in [call["model"] for call in models.calls]


async def test_an_unconfigured_fallback_keeps_the_primary_failure() -> None:
    """兜底没配密钥就不算兜底：GLM 限流时照原样报 GLM 的错，不去调一个没有密钥的 Kimi。"""
    models = _Models(verdict=verdict_for_bull, bear=(NativeAgentError("model_rate_limited"), BEAR_TURNS[1]))
    service, _, _ = _service(models)
    debate_id = (await service.start("SHFE.rb2701", user_id="u1"))["id"]
    await asyncio.gather(*list(service._tasks.values()), return_exceptions=True)
    debate = service.get(debate_id)
    assert debate is not None and debate["status"] == "failed"
    assert debate["error"] == "空方模型 glm-5.3-flash 没有完成（model_rate_limited）" and debate["fallbacks"] == []
    assert "k3.1-pro" not in [call["model"] for call in models.calls]


async def test_both_bear_models_failing_fails_the_debate_naming_both() -> None:
    models = _with_kimi(_Models(verdict=verdict_for_bull, bear=(NativeAgentError("model_rate_limited"), BEAR_TURNS[1])),
                        bear=(TimeoutError(), BEAR_TURNS[1]))
    service, _, _ = _service(models)
    debate_id = (await service.start("SHFE.rb2701", user_id="u1"))["id"]
    await asyncio.gather(*list(service._tasks.values()), return_exceptions=True)
    debate = service.get(debate_id)
    assert debate is not None and debate["status"] == "failed"
    assert debate["error"] == "空方模型 glm-5.3-flash、k3.1-pro 都没有完成（TimeoutError）"
    assert [(call["model"], call["error"]) for call in debate["calls"] if call["role"] == "bear"] == [
        ("glm-5.3-flash", "model_rate_limited"), ("k3.1-pro", "TimeoutError")]
    assert debate["steps"][3]["key"] == "turn2" and debate["steps"][3]["status"] == "failed" and debate["models"]["bear"] == "k3.1-pro"


async def test_a_bear_model_without_a_key_starts_on_the_fallback_and_one_without_either_is_refused() -> None:
    models = _with_kimi(_Models(verdict=verdict_for_bull, endpoints={"deepseek-flash", "judge-fixture"}))
    service, _, _ = _service(models)
    debate_id = (await service.start("SHFE.rb2701", user_id="u1"))["id"]
    await _finish(service)
    debate = service.get(debate_id)
    assert debate is not None and debate["status"] == "done" and debate["models"]["bear"] == "k3.1-pro"
    assert debate["fallbacks"] == [{"role": "bear", "stage": "turn2", "from": "glm-5.3-flash", "to": "k3.1-pro",
                                    "reason": "model_not_configured"}]
    assert "glm-5.3-flash" not in [call["model"] for call in models.calls]
    # The bull and the judge have no fallback: a missing key still refuses before any call.
    service.fallbacks = {}
    with pytest.raises(DebateUnavailable) as refused:
        await service.start("SHFE.rb2701", force=True, user_id="u1")
    assert refused.value.role == "bear"


async def test_same_topic_and_facts_reuse_the_result_and_force_runs_again() -> None:
    models = _Models(verdict=verdict_for_bull)
    service, _, _ = _service(models)
    first = await service.start("SHFE.rb2701", user_id="u1")
    await _finish(service)
    again = await service.start("SHFE.rb2610", user_id="u2")  # another contract of the same product
    assert again["id"] == first["id"] and again["reused"] is True and len(models.calls) == 5
    rerun = await service.start("SHFE.rb2701", force=True, user_id="u1")
    assert rerun["id"] != first["id"] and rerun["reused"] is False and rerun["previousId"] == first["id"]
    await _finish(service)
    assert len(models.calls) == 10
    listing = service.listing(product="SHFE.rb")
    assert [item["id"] for item in listing["items"]] == [rerun["id"], first["id"]]
    assert listing["stats"] == {"settled": 0, "pending": 2, "minSamples": 30, "rates": None}


async def test_an_event_is_debated_once_per_product_and_day_and_unknown_events_are_refused() -> None:
    models = _Models(verdict=verdict_for_bull)
    service, _, _ = _service(models)
    first = await service.start("SHFE.rb2701", event_id=EVENT["id"], user_id="u1")
    assert first["topic"]["kind"] == "event" and first["topic"]["question"].startswith("这条消息对 螺纹钢")
    assert any(fact["id"] == "T.event" and "多地优化住房公积金政策" in fact["value"] for fact in first["facts"])
    # Running: a second click returns the same run instead of starting another.
    assert (await service.start("SHFE.rb2701", event_id=EVENT["id"], user_id="u2"))["id"] == first["id"]
    await _finish(service)
    assert (await service.start("SHFE.rb2701", event_id=EVENT["id"], user_id="u2"))["id"] == first["id"]
    assert [item["id"] for item in service.listing(events=[EVENT["id"]])["items"]] == [first["id"]]
    with pytest.raises(LookupError, match="research_debate_event_unknown"):
        await service.start("SHFE.rb2701", event_id="e000000000000", user_id="u1")


async def test_the_day_cap_and_a_missing_role_model_refuse_before_any_call() -> None:
    models = _Models(verdict=verdict_for_bull)
    service, _, _ = _service(models, daily_runs=1)
    await service.start("SHFE.rb2701", user_id="u1")
    await _finish(service)
    with pytest.raises(DebateUnavailable) as limited:
        await service.start("SHFE.rb2701", force=True, user_id="u1")
    assert limited.value.code == "research_debate_budget" and "1 次" in limited.value.message
    missing = _Models(verdict=verdict_for_bull, endpoints={"deepseek-flash", "judge-fixture"})
    other, _, _ = _service(missing)
    with pytest.raises(DebateUnavailable) as unavailable:
        await other.start("SHFE.rb2701", user_id="u1")
    assert unavailable.value.role == "bear" and unavailable.value.message.startswith("空方模型 glm-5.3-flash 不可用")
    assert missing.calls == []


async def test_writing_into_today_goes_through_the_desk_check_and_history() -> None:
    models = _Models(verdict=verdict_for_bull)
    service, desk, _ = _service(models)
    debate_id = (await service.start("SHFE.rb2701", user_id="u1"))["id"]
    await _finish(service)
    adopted = service.adopt(debate_id)
    assert adopted["judgment"]["stance"] == "偏多"  # 略偏多 folds to the desk's 偏多
    entry = desk._judgments["SHFE.rb"]
    value = entry["value"]
    assert value["debateId"] == debate_id and value["debateStance"] == "略偏多" and value["reason"] == "debate"
    # The winner's adopted argument is a point, the other side's a risk, each with its fact snapshot.
    assert [point["text"] for point in value["points"]] == ["RSI 29 进入超卖，空头动能在减弱"]
    assert [risk["text"] for risk in value["risks"]] == ["仓单较上日 +5000，库存还在累"]
    assert set(value["basis"]) == {"M.indicators", "D.warehouse_receipt"}
    assert set(value["headlineBasis"]) == {"M.indicators", "D.warehouse_receipt"}  # the adopted risk's figures count too
    with desk._db_lock:  # type: ignore[union-attr]
        rows = desk._db.execute("SELECT COUNT(*) FROM research_desk_judgment_history WHERE product_key = 'SHFE.rb'").fetchone()  # type: ignore[union-attr]
    assert rows[0] == 1
    assert service.get(debate_id)["adopted"]["stance"] == "偏多"  # type: ignore[index]


async def test_yesterdays_debate_cannot_become_todays_judgment() -> None:
    models = _Models(verdict=verdict_for_bull)
    service, _, now = _service(models)
    debate_id = (await service.start("SHFE.rb2701", user_id="u1"))["id"]
    await _finish(service)
    now.value += timedelta(days=1)
    with pytest.raises(ValueError, match="research_debate_not_today"):
        service.adopt(debate_id)


def test_the_reckoning_rules() -> None:
    closes = [3110.0, 3090.0, 3120.0, 3130.0, 3150.0]
    assert settle_outcome("略偏多", 3100.0, 3060.0, closes) == "right"
    assert settle_outcome("偏多", 3100.0, 3095.0, closes) == "wrong"  # one close fell below the 失效位
    assert settle_outcome("偏多", 3100.0, None, [3110.0, 3100.0, 3090.0, 3080.0, 3090.0]) == "wrong"
    assert settle_outcome("略偏空", 3100.0, 3200.0, [3090.0, 3080.0, 3070.0, 3060.0, 3050.0]) == "right"
    assert settle_outcome("偏空", 3100.0, 3120.0, [3090.0, 3125.0, 3070.0, 3060.0, 3050.0]) == "wrong"
    assert settle_outcome("震荡", 3100.0, None, [3110.0, 3000.0, 3200.0, 3120.0, 3140.0]) == "right"  # +1.29%
    assert settle_outcome("震荡", 3100.0, None, [3110.0, 3000.0, 3200.0, 3120.0, 3160.0]) == "wrong"  # +1.94%
    assert settle_outcome("看不清", 3100.0, None, closes) is None
    assert settle_outcome(None, 3100.0, None, closes) is None
    assert settle_outcome("偏多", 3100.0, 3060.0, closes[:4]) is None


async def test_five_closes_later_the_debate_is_settled_for_every_participant() -> None:
    history = _History()
    history.closes = [3110.0, 3090.0, 3120.0, 3130.0, 3150.0]
    models = _Models(verdict=verdict_for_bull)
    service, _, now = _service(models, history=history)
    debate_id = (await service.start("SHFE.rb2701", user_id="u1"))["id"]
    await _finish(service)
    settlement = service.get(debate_id)["settlement"]  # type: ignore[index]
    assert settlement["due"] == "2026-10-16" and settlement["entryPrice"] == 3100.0 and settlement["invalidation"] == 3060
    assert settlement["deskStance"] == "震荡" and settlement["leans"] == {"bull": "偏多", "bear": "偏空"}
    assert await service.settle_due() == 0  # not due yet
    now.value = datetime(2026, 10, 16, 8, 0, tzinfo=UTC)  # 16:00 Beijing on the due day
    assert await service.settle_due() == 1
    settled = service.get(debate_id)["settlement"]  # type: ignore[index]
    assert settled["status"] == "settled" and settled["closes"] == history.closes
    assert settled["outcomes"] == {"judge": "right", "bull": "right", "bear": "wrong", "desk": "wrong"}
    assert service.stats()["settled"] == 1 and service.stats()["rates"] is None  # counts only before 30


async def test_a_due_day_estimated_too_early_never_settles_on_a_forming_bar() -> None:
    history = _History()
    history.closes = [3110.0, 3090.0, 3120.0, 3130.0, 3150.0]  # 10-12 … 10-16; the 10-16 bar is in the history all day
    service, _, now = _service(_Models(verdict=verdict_for_bull), history=history)
    debate_id = (await service.start("SHFE.rb2701", user_id="u1"))["id"]
    await _finish(service)
    # No calendar reached that far (a holiday miscounted as weekdays): the stored due day is two days early.
    record = service._stored(debate_id)
    assert record is not None
    record["settlement"]["due"] = "2026-10-14"
    service._save(record)
    now.value = datetime(2026, 10, 16, 3, 0, tzinfo=UTC)  # 11:00 Beijing on the real fifth day: its bar is still forming
    assert await service.settle_due() == 0
    assert service.get(debate_id)["settlement"]["status"] == "pending"  # type: ignore[index]
    now.value = datetime(2026, 10, 16, 8, 0, tzinfo=UTC)  # 16:00: closed
    assert await service.settle_due() == 1
    assert service.get(debate_id)["settlement"]["closes"] == history.closes  # type: ignore[index]


async def test_a_slot_whose_history_read_failed_never_voids_another_slots_settlement() -> None:
    history = _History()
    history.closes = [3110.0, 3090.0, 3120.0, 3130.0, 3150.0]
    service, desk, now = _service(_Models(verdict=verdict_for_bull), history=history)
    debate_id = (await service.start("SHFE.rb2701", user_id="u1"))["id"]
    await _finish(service)
    # The other blue-green slot on the same table.
    other = ResearchDebateService(desk=desk, connection=service.connection, lock=service.lock, events=service.events,
                                  now=now, models=service.models)
    now.value = datetime(2026, 10, 30, 8, 0, tzinfo=UTC)  # long past the give-up day

    def settle_elsewhere_then_fail(symbol: str, trading_day: str) -> list[float]:
        # Both slots picked the pending row; this slot settles it while the other's history read fails.
        assert asyncio.run_coroutine_threadsafe(service.settle_due(), loop).result() == 1
        raise RuntimeError("history_unavailable")
    loop = asyncio.get_running_loop()
    other._closes = settle_elsewhere_then_fail  # type: ignore[method-assign]
    assert await other.settle_due() == 0
    settled = service.get(debate_id)["settlement"]  # type: ignore[index]
    assert settled["status"] == "settled" and settled["closes"] == history.closes
    with service.lock:
        assert service.connection.execute("SELECT outcome FROM research_debates WHERE id = ?", (debate_id,)).fetchone()[0] == "settled"
    # A late duplicate write of the same row is refused as well.
    assert other._settle_write(debate_id, {**settled, "status": "void", "reason": "合约收盘价读不到"}) is False


def test_confidence_is_computed_and_capped_by_the_sheets() -> None:
    by_id = {fact["id"]: fact for fact in FACTS}
    full = sheets([*FACTS, {"id": "D.term_structure", "kind": "data"}])
    data_only = [{"factIds": ["M.indicators", "D.position_rank"]}]
    assert confidence("略偏多", "bull", "略", data_only, 0, by_id, full)[0] == "高"
    assert confidence("略偏多", "bull", "略", [{"factIds": ["N.ab12cd34"]}], 0, by_id, full)[0] == "低"
    assert confidence("偏多", "bull", "明显", data_only, 2, by_id, full)[0] == "中"
    gaps = sheets(FACTS)  # no 期限结构: missing
    level, basis = confidence("略偏多", "bull", "略", data_only, 0, by_id, gaps)
    assert level == "中" and basis["missingSheets"] == 1 and basis["cap"] == "中"
    assert confidence("震荡", None, "略", data_only, 0, by_id, full)[0] == "中"
    assert confidence("看不清", None, "略", data_only, 0, by_id, full)[0] == "低"
    states = {sheet["key"]: sheet["status"] for sheet in gaps}
    assert states == {"position": "partial", "basis": "complete", "term": "missing", "news": "complete", "technical": "complete"}
    assert next(sheet for sheet in gaps if sheet["key"] == "position")["missing"] == ["持仓变化"]
    stale = sheets([{**FACTS[0], "status": "stale"}, *FACTS[1:]])
    assert next(sheet for sheet in stale if sheet["key"] == "technical")["status"] == "stale"


async def test_routes_gate_who_starts_and_read_without_starting(tmp_path) -> None:
    from trader_runtime.main import create_app

    app = create_app(tmp_path / "runtime.sqlite3", local_token="research-test-token-with-32-chars!!", agent_mode="fake", data_mode="fake")
    starts: list[tuple[str, str | None, bool]] = []

    async def start(self, symbol, *, event_id=None, force=False, user_id=None, generator=None):
        starts.append((symbol, event_id, force))
        if symbol is None:
            raise EventProductError("research_debate_event_ambiguous", ["rb", "hc"])
        raise DebateUnavailable("research_debate_model_unavailable", "裁判模型 judge-fixture 不可用", role="judge", model="judge-fixture")

    try:
        app.state.ctx.services.research_debate.start = start.__get__(app.state.ctx.services.research_debate)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers={"Origin": "http://127.0.0.1:5173"}) as client:
            # Signed out (a visitor's guest cookie included): nothing to read, nothing to start.
            assert (await client.post("/api/research/debates", json={"symbol": "SHFE.rb2610"})).status_code == 401
            assert (await client.post("/api/auth/guest")).status_code in (200, 404)
            assert (await client.post("/api/research/debates", json={"symbol": "SHFE.rb2610"})).status_code == 401
            assert (await client.post("/api/auth/local", headers={"Authorization": "Bearer research-test-token-with-32-chars!!"})).status_code == 200
            refused = await client.post("/api/research/debates", json={"symbol": "SHFE.rb2610", "eventId": "e0123456789ab"})
            assert refused.status_code == 503 and refused.json()["detail"]["role"] == "judge"
            assert starts == [("SHFE.rb2610", "e0123456789ab", False)]
            # An event id alone is enough (its product comes from the event); one about two products asks which.
            ambiguous = await client.post("/api/research/debates", json={"eventId": "e0123456789ab"})
            assert ambiguous.status_code == 409 and ambiguous.json()["detail"]["products"] == ["rb", "hc"]
            assert (await client.post("/api/research/debates", json={})).status_code == 422
            listed = await client.get("/api/research/debates", params={"product": "SHFE.rb"})
            assert listed.status_code == 200 and listed.json()["items"] == []
            assert (await client.get("/api/research/debates/db000000000000")).status_code == 404
            catalog = (await client.get("/api/agent/capabilities")).json()
            operations = {item["operation"]: item for item in catalog["operations"]}
            assert operations["GET /api/research/debates"]["readOnly"] is True
            assert operations["GET /api/research/debates/{debate_id}"]["readOnly"] is True
            assert operations["POST /api/research/debates"]["readOnly"] is False
            assert "POST /api/research/debates/{debate_id}/adopt" in operations
    finally:
        app.state.research.close()
        app.state.store.close()


def test_the_ui_command_its_receipt_the_spoken_words_and_the_chat_focus() -> None:
    from pydantic import ValidationError

    from trader_runtime.message_context import screen_snapshot_instruction
    from trader_runtime.ui_control import UiChanges, _surface_matches
    from trader_runtime.voice_intents import match_intent

    assert UiChanges.model_validate({"researchDebate": {"start": "desk"}}).researchDebate.start == "desk"  # type: ignore[union-attr]
    for bad in ({"start": "desk", "close": True}, {}, {"open": "nope"}, {"rerun": False}):
        with pytest.raises(ValidationError):
            UiChanges.model_validate({"researchDebate": bad})
    view = {"open": True, "id": "db0123456789ab", "status": "running", "request": {"kind": "start", "status": "started",
                                                                                   "eventId": "e0123456789ab"}}
    assert _surface_matches({"researchDebate": view}, "researchDebate", {"start": "e0123456789ab"})
    assert _surface_matches({"researchDebate": view}, "researchDebate", {"start": "desk"})
    assert not _surface_matches({"researchDebate": view}, "researchDebate", {"start": "e999999999999"})
    assert _surface_matches({"researchDebate": view}, "researchDebate", {"open": "db0123456789ab"})
    assert not _surface_matches({"researchDebate": view}, "researchDebate", {"rerun": True})
    assert _surface_matches({}, "researchDebate", {"close": True})
    assert not _surface_matches({}, "researchDebate", {"open": "db0123456789ab"})
    said = match_intent("让AI们讨论一下这条")
    assert said is not None and said.changes == {"researchDebate": {"start": "event"}}
    named = match_intent("辩一辩螺纹")
    assert named is not None and named.symbol_query == "螺纹" and named.follow == [{"researchDebate": {"start": "desk"}}]
    assert match_intent("讨论一下螺纹为什么跌") is None
    # 辩论页 (2026-10-24): its own rail page — a panel command, spoken as “打开辩论”, closed as “关掉辩论页”.
    assert UiChanges.model_validate({"panel": "debate"}).panel == "debate"
    page = match_intent("打开辩论")
    assert page is not None and page.changes == {"panel": "debate"}
    closing = match_intent("关掉辩论页")
    assert closing is not None and closing.changes == {"panel": None}
    assert match_intent("辩论一下") is not None and match_intent("辩论一下").changes == {"researchDebate": {"start": "desk"}}  # type: ignore[union-attr]
    focus = screen_snapshot_instruction({"state": {"panel": "news", "researchDebate": {**view, "stance": "略偏多"}}})
    assert "researchDebate.id=db0123456789ab；researchDebate.status=running；researchDebate.stance=略偏多" in focus


def test_the_line_is_a_nearby_price_from_a_price_fact() -> None:
    from trader_runtime.research_debate import invalidation_problem, price_numbers
    assert price_numbers(FACTS) == {3100.0, 3200.0, 3060.0, 3130.0}  # M.price and K.levels values, no percentages
    assert invalidation_problem(3060, "略偏多", 3100.0, FACTS, 8.0) is None
    assert invalidation_problem(29, "略偏多", 3100.0, FACTS, 8.0) == "not_price"      # the RSI
    assert invalidation_problem(5000, "偏多", 3100.0, FACTS, 8.0) == "not_price"      # a stock figure
    assert invalidation_problem(3200, "偏多", 3100.0, FACTS, 8.0) == "wrong_side"
    assert invalidation_problem(3060, "偏多", 3100.0, FACTS, 1.0) == "too_far"
    assert invalidation_problem(3060, "震荡", 3100.0, FACTS, 8.0) == "no_stance"
    assert invalidation_problem(3060, "看不清", None, FACTS, 8.0) == "no_stance"
    # #809: a 略偏空 with no 发起价 has a stance; what it lacks is the price to measure the line from.
    assert invalidation_problem(3200, "略偏空", None, FACTS, 8.0) == "no_entry"


async def test_a_far_line_is_dropped_and_the_record_says_why() -> None:
    def far(input_text: str) -> DebateVerdict:
        return verdict_for_bull(input_text).model_copy(update={"invalidation": 3060.0})
    models = _Models(verdict=far)
    service, _, _ = _service(models)
    service.invalidation_max_percent = 1.0
    debate_id = (await service.start("SHFE.rb2701", user_id="u1"))["id"]
    await _finish(service)
    verdict = service.get(debate_id)["verdict"]  # type: ignore[index]
    assert verdict["invalidation"] is None
    assert verdict["confidenceBasis"]["invalidationDropped"] == {"value": 3060.0, "reason": "too_far", "maxPercent": 1.0}


async def test_a_line_without_an_entry_price_is_dropped_as_no_entry() -> None:
    def lined(input_text: str) -> DebateVerdict:
        return verdict_for_bull(input_text).model_copy(update={"invalidation": 3060.0})
    service, desk, _ = _service(_Models(verdict=lined))
    material = desk.debate_material

    async def unpriced(symbol: str, **kwargs: Any) -> dict[str, Any]:
        return {**(await material(symbol, **kwargs)), "price": None,
                "entry": {"price": None, "source": None, "day": None, "reason": "close_missing"}}
    desk.debate_material = unpriced  # type: ignore[method-assign]
    debate_id = (await service.start("SHFE.rb2701", user_id="u1"))["id"]
    await _finish(service)
    record = service.get(debate_id)
    assert record is not None and record["verdict"]["stance"] in ("偏多", "略偏多")
    assert record["verdict"]["confidenceBasis"]["invalidationDropped"]["reason"] == "no_entry"
    assert record["settlement"]["status"] == "void" and record["settlement"]["reason"] == "休市中，没有最近一个交易日的收盘价"


def test_the_cache_key_covers_every_field_the_models_see() -> None:
    from trader_runtime.research_debate import facts_hash
    base = facts_hash(FACTS)
    for name, value in (("asOf", "2026-10-08"), ("source", "别家"), ("status", "stale"), ("detail", "多一句"), ("title", "t"), ("kind", "report")):
        changed = [{**FACTS[0], name: value}, *FACTS[1:]]
        assert facts_hash(changed) != base, name


async def test_two_clicks_on_one_event_make_one_run() -> None:
    models = _Models(verdict=verdict_for_bull)
    service, _, _ = _service(models)
    first, second = await asyncio.gather(service.start("SHFE.rb2701", event_id=EVENT["id"], user_id="u1"),
                                         service.start("SHFE.rb2701", event_id=EVENT["id"], user_id="u2"))
    assert first["id"] == second["id"] and second["reused"] is True
    await _finish(service)
    assert len(models.calls) == 5
    with service.lock:
        assert service.connection.execute("SELECT COUNT(*) FROM research_debates").fetchone()[0] == 1


async def test_another_slots_live_run_survives_a_start_and_a_silent_one_is_failed() -> None:
    """Blue-green: two processes on one table. The new one leaves a run the old one is still beating alone,
    fails one whose owner went silent, and a cancel it receives reaches the owner on its next beat."""
    models = _Models(verdict=verdict_for_bull)
    old, desk, now = _service(models)
    running = await old.start("SHFE.rb2701", user_id="u1")
    old.beat()
    new = ResearchDebateService(desk=desk, connection=old.connection, lock=old.lock, now=now)
    assert new.get(running["id"])["status"] in ("queued", "running")  # type: ignore[index]
    asked = await new.cancel(running["id"])
    assert asked["cancelRequested"] is True
    old.beat()  # the owner sees the request
    await asyncio.gather(*list(old._tasks.values()), return_exceptions=True)
    assert new.get(running["id"])["status"] == "cancelled"  # type: ignore[index]
    # An owner that stopped beating: after STALE_SECONDS the run is failed with the reason.
    with old.lock, old.connection:
        old.connection.execute("UPDATE research_debates SET status = 'running', owner = 'gone', heartbeat_at = ? WHERE id = ?",
                               ((now.value - timedelta(minutes=5)).isoformat(timespec="seconds"), running["id"]))
    failed = new.get(running["id"])
    assert failed is not None and failed["status"] == "failed" and "中断" in failed["error"]


async def test_a_cloud_router_resolves_the_readers_model_adapter_and_runs_a_debate(tmp_path, monkeypatch) -> None:
    """Cloud mode: application.state.adapter is a SessionAgentRouter (no complete_structured of its own); the
    route resolves the reader's adapter like the desk and the debate runs through to its verdict."""
    from trader_runtime.main import create_app
    from trader_runtime.tenant_adapters import SessionAgentRouter

    app = create_app(tmp_path / "runtime.sqlite3", local_token="research-test-token-with-32-chars!!", agent_mode="fake", data_mode="fake")
    models = _with_kimi(_Models(verdict=verdict_for_bull))
    made: list[str] = []

    def factory(user_id: str) -> _Models:
        made.append(user_id)
        return models
    router = SessionAgentRouter(store=app.state.store, adapter_factory=factory, adapter_id="native-agent",
                                runtime_status_factory=lambda mode, connected: {})
    assert not hasattr(router, "complete_structured")
    app.state.adapter = router
    service = app.state.ctx.services.research_debate

    async def material(symbol: str, **_: Any) -> dict[str, Any]:
        return {"symbol": symbol, "mainSymbol": "SHFE.rb2610", "exchange": "SHFE", "product": "rb", "productName": "螺纹钢",
                "facts": [dict(fact) for fact in FACTS], "sections": [], "price": 3100.0,
                "researchDay": service.desk.research_day(service.now()), "judgment": {}}
    monkeypatch.setattr(service.desk, "debate_material", material)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers={"Origin": "http://127.0.0.1:5173"}) as client:
            assert (await client.post("/api/auth/local", headers={"Authorization": "Bearer research-test-token-with-32-chars!!"})).status_code == 200
            started = await client.post("/api/research/debates", json={"symbol": "SHFE.rb2610"})
            assert started.status_code == 200, started.text
            await asyncio.gather(*list(service._tasks.values()))
            done = (await client.get(f"/api/research/debates/{started.json()['id']}")).json()
            assert done["status"] == "done" and done["verdict"]["stance"] == "略偏多"
            assert made and len(models.calls) == 5 and {call["user"] for call in models.calls} == {USAGE_USER}
    finally:
        app.state.research.close()
        app.state.store.close()


async def test_a_reusable_result_is_answered_even_when_a_role_model_is_down() -> None:
    models = _Models(verdict=verdict_for_bull)
    service, _, _ = _service(models)
    first = await service.start("SHFE.rb2701", user_id="u1")
    await _finish(service)
    down = _Models(verdict=verdict_for_bull, endpoints={"deepseek-flash", "judge-fixture"})
    again = await service.start("SHFE.rb2701", user_id="u2", generator=down)
    assert again["id"] == first["id"] and again["reused"] is True and down.calls == []
    with pytest.raises(DebateUnavailable):  # a new run does need every role's model
        await service.start("SHFE.rb2701", force=True, user_id="u2", generator=down)


async def test_the_losers_adopted_sentence_is_a_risk_and_all_adopted_evidence_weighs_on_confidence() -> None:
    def judge(input_text: str) -> DebateVerdict:
        bull = "A" if any("RSI" in item["text"] for item in _cases(input_text)["A 方"]) else "B"
        return DebateVerdict.model_validate({"winner": bull, "margin": "略", "headline": "略偏多",
                                             "adopted": [{"id": f"{bull}1"}, {"id": f"{bull}3"}, {"id": f"{'B' if bull == 'A' else 'A'}2"}]})
    service, desk, _ = _service(_Models(verdict=judge))
    debate_id = (await service.start("SHFE.rb2701", user_id="u1"))["id"]
    await _finish(service)
    verdict = service.get(debate_id)["verdict"]  # type: ignore[index]
    assert {item["id"]: item["side"] for item in verdict["adopted"]} == {"多1": "bull", "多3": "bull", "空2": "bear"}
    service.adopt(debate_id)
    value = desk._judgments["SHFE.rb"]["value"]
    # Sentences as 今日判断 reads them: no markers, no closing stop; the quote is the cited fact's own words.
    assert [point["text"] for point in value["points"]] == ["RSI 29 进入超卖，空头动能在减弱", "你说仓单在增，那是检修期的季节性，RSI 29 说明跌得够多"]
    assert value["risks"][0]["text"] == "现货基差 -92，现货比盘面弱" and value["risks"][0]["direction"] == "偏空"
    assert value["points"][0]["quote"] == "日线指标RSI29（超卖），布林下轨附近"
    # Every adopted sentence's evidence counts: 2 facts cited, both data → share 1.
    assert verdict["confidenceBasis"]["evidenceShare"] == 1.0


async def test_a_new_research_day_does_not_reuse_yesterdays_result() -> None:
    models = _Models(verdict=verdict_for_bull)
    service, _, now = _service(models)
    first = await service.start("SHFE.rb2701", user_id="u1")
    await _finish(service)
    now.value += timedelta(days=3)  # a weekend later, the same facts
    later = await service.start("SHFE.rb2701", user_id="u1")
    assert later["id"] != first["id"] and later["reused"] is False
    await _finish(service)


async def test_the_day_cap_holds_for_different_topics_started_at_once() -> None:
    models = _Models(verdict=verdict_for_bull)
    service, _, _ = _service(models, daily_runs=1)
    outcomes = await asyncio.gather(service.start("SHFE.rb2701", user_id="u1"),
                                    service.start("SHFE.rb2701", event_id=EVENT["id"], user_id="u2"), return_exceptions=True)
    assert sum(isinstance(item, dict) for item in outcomes) == 1
    assert sum(isinstance(item, DebateUnavailable) and item.code == "research_debate_budget" for item in outcomes) == 1
    await asyncio.gather(*list(service._tasks.values()), return_exceptions=True)
    with service.lock:
        assert service.connection.execute("SELECT COUNT(*) FROM research_debates").fetchone()[0] == 1


async def test_everyone_who_joined_a_running_debate_hears_when_it_ends() -> None:
    models = _Models(verdict=verdict_for_bull)
    service, _, _ = _service(models)
    heard: list[tuple[str, str]] = []

    async def notify(user: str, event: dict[str, Any]) -> None:
        heard.append((user, event["kind"]))
    service.notify = notify
    first = await service.start("SHFE.rb2701", event_id=EVENT["id"], user_id="u1")
    joined = await service.start("SHFE.rb2701", event_id=EVENT["id"], user_id="u2")
    assert joined["id"] == first["id"]
    await _finish(service)
    assert ("u1", "done") in heard and ("u2", "done") in heard


async def test_writing_the_same_debate_twice_does_not_write_again() -> None:
    models = _Models(verdict=verdict_for_bull)
    service, desk, _ = _service(models)
    debate_id = (await service.start("SHFE.rb2701", user_id="u1"))["id"]
    await _finish(service)
    first = service.adopt(debate_id)
    again = service.adopt(debate_id)
    assert again["repeated"] is True and again["judgment"]["generatedAt"] == first["judgment"]["generatedAt"]
    with desk._db_lock:  # type: ignore[union-attr]
        assert desk._db.execute("SELECT COUNT(*) FROM research_desk_judgment_history").fetchone()[0] == 1  # type: ignore[union-attr]


async def test_a_winner_without_an_adopted_argument_of_its_own_needs_review() -> None:
    def empty(input_text: str) -> DebateVerdict:
        cases = json.loads(input_text.rsplit("\n", 1)[-1])
        bull = "A" if any("RSI" in item["text"] for item in cases["A 方"]) else "B"
        return DebateVerdict.model_validate({"winner": bull, "margin": "明显", "headline": "偏多", "adopted": [{"id": "Z1"}]})
    models = _Models(verdict=empty)
    service, _, _ = _service(models)
    debate_id = (await service.start("SHFE.rb2701", user_id="u1"))["id"]
    await _finish(service)
    debate = service.get(debate_id)
    assert debate is not None and debate["status"] == "review" and debate["verdict"] is None
    assert debate["unsupportedVerdict"]["stance"] == "偏多" and debate["settlement"]["status"] == "void"


async def test_a_voice_start_says_what_the_runtime_answered(monkeypatch) -> None:
    from trader_runtime import turn_router
    monkeypatch.setattr(turn_router, "DEBATE_POLL_SECONDS", 0)
    answers = iter([{"status": "sending"}, {"status": "refused", "message": "今天的研讨次数（30 次）已用完，明天再来"}])

    async def call(operation: str, **_: Any) -> dict[str, Any]:
        return {"data": {"state": {"researchDebate": {"request": next(answers)}}}}
    line = await turn_router.debate_start_line({"researchDebate": {"request": {"status": "sending"}}}, "w1", call, "开始了")
    assert line == "没能开始研讨：今天的研讨次数（30 次）已用完，明天再来"
    assert await turn_router.debate_start_line({"researchDebate": {"request": {"status": "started"}}}, "w1", call, "开始了") == "开始了"
    assert "已经研讨过" in await turn_router.debate_start_line({"researchDebate": {"request": {"status": "reused"}}}, "w1", call, "开始了")


async def test_only_the_starter_may_cancel() -> None:
    models = _Models(verdict=verdict_for_bull)
    service, _, _ = _service(models)
    running = await service.start("SHFE.rb2701", user_id="u1")
    await service.start("SHFE.rb2701", user_id="u2")  # joined, shares the result
    with pytest.raises(PermissionError, match="research_debate_not_yours"):
        await service.cancel(running["id"], "u2")
    stopped = await service.cancel(running["id"], "u1")
    assert stopped["status"] == "cancelled"


async def test_a_draw_with_no_checked_argument_adopted_needs_review() -> None:
    def draw(input_text: str) -> DebateVerdict:
        return DebateVerdict.model_validate({"winner": "平", "headline": "两边相当", "adopted": [{"id": "Q9"}]})
    service, _, _ = _service(_Models(verdict=draw))
    debate_id = (await service.start("SHFE.rb2701", user_id="u1"))["id"]
    await _finish(service)
    assert service.get(debate_id)["status"] == "review"  # type: ignore[index]


async def test_a_debate_is_written_once_even_when_the_marker_was_lost_or_two_slots_adopt_at_once() -> None:
    service, desk, now = _service(_Models(verdict=verdict_for_bull))
    debate_id = (await service.start("SHFE.rb2701", user_id="u1"))["id"]
    await _finish(service)
    # Another slot on the same database, which never saw this debate's "adopted" marker.
    other = ResearchDebateService(desk=ResearchDeskService(research=desk.research, news=desk.news, provider=desk.provider, now=now),
                                  connection=service.connection, lock=service.lock, events=service.events, now=now,
                                  models=service.models)
    first = service.adopt(debate_id)
    # A crash after the desk write but before the debate's marker was saved: the marker is gone.
    stored = service._stored(debate_id)
    assert stored is not None
    stored.pop("adopted")
    service._save(stored)
    now.value += timedelta(seconds=3)
    again = service.adopt(debate_id)
    elsewhere = other.adopt(debate_id)
    assert again["repeated"] is True and elsewhere["repeated"] is True
    assert again["judgment"]["generatedAt"] == elsewhere["judgment"]["generatedAt"] == first["judgment"]["generatedAt"]
    # A newer debate's adoption stays the judgment; retrying the old one does not put it back.
    newer = (await service.start("SHFE.rb2701", user_id="u1", force=True))["id"]
    await _finish(service)
    now.value += timedelta(seconds=3)
    service.adopt(newer)
    stored = service._stored(debate_id)
    assert stored is not None
    stored.pop("adopted")
    service._save(stored)
    assert service.adopt(debate_id)["repeated"] is True
    with desk._db_lock:  # type: ignore[union-attr]
        assert desk._db.execute("SELECT COUNT(*) FROM research_desk_judgment_history").fetchone()[0] == 2  # type: ignore[union-attr]
        latest = desk._db.execute("SELECT json_extract(payload_json, '$.value.debateId') FROM research_desk_judgments").fetchone()[0]  # type: ignore[union-attr]
    assert latest == newer


async def test_an_adoption_is_not_overwritten_by_a_model_call_that_was_already_running() -> None:
    service, desk, now = _service(_Models(verdict=verdict_for_bull))
    debate_id = (await service.start("SHFE.rb2701", user_id="u1"))["id"]
    await _finish(service)
    begun = now.value
    now.value += timedelta(seconds=5)
    service.adopt(debate_id)
    assert desk._adopted_since("SHFE.rb", begun)  # a desk generation that began before it will not store
    assert not desk._adopted_since("SHFE.rb", now.value + timedelta(seconds=5))
    # The write itself refuses too (one upsert statement), whatever was cached in memory.
    stale = {**desk._judgments["SHFE.rb"], "value": {**desk._judgments["SHFE.rb"]["value"], "reason": "daily"}, "generatedAt": "2026-10-09T03:00:10+00:00"}
    desk._judgments.pop("SHFE.rb")
    assert desk._store("SHFE.rb", stale, unless_adopted_since=begun) is False
    with desk._db_lock:  # type: ignore[union-attr]
        row = desk._db.execute("SELECT json_extract(payload_json, '$.value.reason') FROM research_desk_judgments").fetchone()  # type: ignore[union-attr]
    assert row[0] == "debate"


async def test_the_benchmark_skips_a_todays_judgment_that_came_from_a_debate() -> None:
    models = _Models(verdict=verdict_for_bull)
    service, _, _ = _service(models, judgment={"status": "ready", "stance": "偏多", "confidence": "中", "headline": "x",
                                                "generatedAt": "2026-10-09T02:00:00+00:00", "reason": "debate"})
    debate_id = (await service.start("SHFE.rb2701", user_id="u1"))["id"]
    await _finish(service)
    debate = service.get(debate_id)
    assert debate is not None and debate["compare"]["fromDebate"] is True and debate["settlement"]["deskStance"] is None


async def test_an_unreadable_contract_long_after_its_due_day_is_void() -> None:
    class _Gone(_History):
        def market_history(self, symbol: str, duration_seconds: int, length: int) -> dict[str, Any]:
            raise RuntimeError("market_series_unknown")
    service, _, now = _service(_Models(verdict=verdict_for_bull), history=_Gone())
    debate_id = (await service.start("SHFE.rb2701", user_id="u1"))["id"]
    await _finish(service)
    now.value = datetime(2026, 10, 20, 8, 0, tzinfo=UTC)
    await service.settle_due()
    assert service.get(debate_id)["settlement"]["status"] == "pending"  # type: ignore[index]  # still retried
    now.value = datetime(2026, 11, 5, 8, 0, tzinfo=UTC)
    await service.settle_due()
    assert service.get(debate_id)["settlement"]["status"] == "void"  # type: ignore[index]


async def test_a_reaped_run_tells_its_waiters() -> None:
    models = _Models(verdict=verdict_for_bull)
    old, desk, now = _service(models)
    running = await old.start("SHFE.rb2701", user_id="u1")
    for task in list(old._tasks.values()):
        task.cancel()
    await asyncio.gather(*list(old._tasks.values()), return_exceptions=True)
    with old.lock, old.connection:
        old.connection.execute("UPDATE research_debates SET status = 'running', owner = 'gone', heartbeat_at = ? WHERE id = ?",
                               ((now.value - timedelta(minutes=5)).isoformat(timespec="seconds"), running["id"]))
    heard: list[tuple[str, str]] = []

    async def notify(user: str, event: dict[str, Any]) -> None:
        heard.append((user, event["kind"]))
    new = ResearchDebateService(desk=desk, connection=old.connection, lock=old.lock, now=now, notify=notify)
    await new._announce_reaped()
    assert ("u1", "done") in heard and new.get(running["id"])["status"] == "failed"  # type: ignore[index]


async def test_the_entry_price_is_a_current_quote_or_in_a_closed_market_the_last_close() -> None:
    from trader_runtime.research_desk import ResearchDeskService as Desk
    now = _Now()
    desk = Desk(research=_Research(), news=lambda symbol: asyncio.sleep(0, {}), provider=lambda: None, now=now)  # type: ignore[arg-type]
    quote: dict[str, Any] = {}
    bars = [{"close": 103.0 + index * 0.005, "tradingDay": f"2026-09-{index + 10:02d}"} for index in range(18)] + [
        {"close": 103.08, "tradingDay": "2026-10-08"}, {"close": 103.1, "tradingDay": "2026-10-09"}]

    async def report(symbol: str, **_: Any) -> dict[str, Any]:
        return {"symbol": symbol, "mainSymbol": "CFFEX.T2612", "exchange": "CFFEX", "product": "T", "productName": "十年国债",
                "sections": [], "facts": [{"id": "M.price", "value": "103.125，日涨跌 +0.1%"}], "judgment": {}}

    async def workbench(symbol: str, *, with_quote: bool = False) -> dict[str, Any]:
        return {"quote": dict(quote), "bars": bars, "capturedAt": None}
    desk.report = report  # type: ignore[method-assign]
    desk._workbench = workbench  # type: ignore[method-assign]
    # Trading, a current quote (the order path's own freshness rule): its price, decimals kept, today's trading day.
    quote.update(lastPrice=103.125, dataStatus="normal", ageSeconds=1, quoteDatetime=NOW.isoformat(), marketSessionStatus="trading")
    material = await desk.debate_material("CFFEX.T2612")
    assert material["price"] == 103.125 and material["entry"] == {"price": 103.125, "source": "quote", "day": "2026-10-09", "at": None}
    # K.levels (a 失效位 may be taken from them) read only closed bars: at 11:00 today's forming 103.1 is not one.
    levels = next(fact for fact in material["facts"] if fact["id"] == "K.levels")
    assert levels["label"] == "近19个交易日收盘价" and levels["value"].startswith("最高 103.085，最低 103")
    # Trading, but the quote is ten minutes old: no entry price (the M.price fact is never used instead).
    quote.update(dataStatus="stale", ageSeconds=600, quoteDatetime=(NOW - timedelta(minutes=10)).isoformat())
    material = await desk.debate_material("CFFEX.T2612")
    assert material["price"] is None and material["entry"]["source"] is None and material["entry"]["reason"] == "quote_not_current"
    # The lunch break (closed): today has traded (its bar is forming), so its last price, never yesterday's close
    # (#761) — and with only a stale quote that cannot say at what price, no entry price at all.
    quote.update(marketSessionStatus="closed")
    now.value = NOW + timedelta(hours=1)  # 12:00
    assert (await desk.debate_material("CFFEX.T2612"))["entry"] == {"price": None, "source": None, "day": None,
                                                                    "reason": "closed_price_missing"}
    quote.update(dataStatus="normal", ageSeconds=1800, quoteDatetime="2026-10-09T11:30:00+08:00", quotedAt="2026-10-09T11:30:00+08:00")
    assert (await desk.debate_material("CFFEX.T2612"))["entry"] == {"price": 103.125, "source": "last", "day": "2026-10-09",
                                                                    "at": "2026-10-09T11:30:00+08:00"}
    # After the day close (its bar settled) and on the weekend (no night session): Friday's close.
    for moment in (NOW + timedelta(hours=5), NOW + timedelta(days=1)):  # Fri 16:00, Sat 11:00
        now.value = moment
        material = await desk.debate_material("CFFEX.T2612")
        assert material["entry"] == {"price": 103.1, "source": "close", "day": "2026-10-09", "at": "2026-10-09"}
        assert next(fact for fact in material["facts"] if fact["id"] == "K.levels")["value"].startswith("最高 103.1，")


CST = timezone(timedelta(hours=8))
DAYS = ["2026-10-08", "2026-10-09", "2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16", "2026-10-19"]


def _bar(day: str, close: float) -> dict[str, Any]:
    return {"datetime": int(datetime.fromisoformat(day).replace(tzinfo=CST).timestamp() * 1e9), "close": close}


def _closed_quote(at: str, price: float, **extra: Any) -> dict[str, Any]:
    return {"marketSessionStatus": "closed", "dataStatus": "normal", "isCached": False, "ageSeconds": 600,
            "quoteDatetime": at, "quotedAt": at, "lastPrice": price, **extra}


@pytest.mark.parametrize(("moment", "traded", "price", "forming"), [
    ("2026-10-12T12:00:00+08:00", "2026-10-12T11:30:00+08:00", 3100.0, 3100.0),  # Monday lunch break
    ("2026-10-12T10:20:00+08:00", "2026-10-12T10:15:00+08:00", 3100.0, 3100.0),  # the 10:15 break
    ("2026-10-12T15:10:00+08:00", "2026-10-12T15:00:00+08:00", 3100.0, 3100.0),  # closed, the bar not settled till 15:30
    ("2026-10-10T10:00:00+08:00", "2026-10-10T02:30:00+08:00", 3060.0, 3060.0),  # Saturday after Friday's night session
    ("2026-10-12T02:50:00+08:00", "2026-10-10T02:30:00+08:00", 3060.0, None),    # Monday 02:50, the bar not synced yet
])
def test_a_closed_market_after_the_current_day_traded_enters_at_its_last_price(
        moment: str, traded: str, price: float, forming: float | None) -> None:
    # #761: the 底稿 already shows today's price; entering at Friday's 3010 would score the move made before the debate.
    bars = [_bar("2026-10-08", 3000.0), _bar("2026-10-09", 3010.0)] + ([_bar("2026-10-12", forming)] if forming else [])
    entry = debate_entry(_closed_quote(traded, price), bars, datetime.fromisoformat(moment), DAYS)
    assert entry == {"price": price, "source": "last", "day": "2026-10-12", "at": traded}


def test_a_closed_market_before_the_current_day_traded_enters_at_the_last_close() -> None:
    bars = [_bar("2026-10-08", 3000.0), _bar("2026-10-09", 3010.0)]
    friday = _closed_quote("2026-10-09T15:00:00+08:00", 3010.0)  # no night session
    previous = {"price": 3010.0, "source": "close", "day": "2026-10-09", "at": "2026-10-09"}
    # Monday 08:30 before the open; Saturday with no night session; Friday 18:30 before the night session.
    for moment in ("2026-10-12T08:30:00+08:00", "2026-10-10T10:00:00+08:00", "2026-10-09T18:30:00+08:00"):
        assert debate_entry(friday, bars, datetime.fromisoformat(moment), DAYS) == previous
    # A placeholder bar for Monday is no trade: the quote's own last trade says Friday.
    assert debate_entry(friday, [*bars, _bar("2026-10-12", 3010.0)], datetime.fromisoformat("2026-10-12T08:30:00+08:00"), DAYS) == previous
    # After 15:30 the current day's own bar has settled: its close.
    monday = [*bars, _bar("2026-10-12", 3100.0)]
    assert debate_entry(_closed_quote("2026-10-12T15:00:00+08:00", 3100.0), monday, datetime.fromisoformat("2026-10-12T15:40:00+08:00"),
                        DAYS) == {"price": 3100.0, "source": "close", "day": "2026-10-12", "at": "2026-10-12"}


@pytest.mark.parametrize("quote", [
    _closed_quote("2026-10-12T11:30:00+08:00", 3100.0, dataStatus="stale"),
    _closed_quote("2026-10-12T11:30:00+08:00", 3100.0, isCached=True),
    _closed_quote("", 3100.0),
    _closed_quote("2026-10-12T12:30:00+08:00", 3100.0),  # a trade time after now
])
def test_today_traded_without_a_quote_that_can_say_at_what_price_has_no_entry(quote: dict[str, Any]) -> None:
    # Today's bar is forming, but no quote the source vouches for: no entry price (void), never Friday's close.
    bars = [_bar("2026-10-08", 3000.0), _bar("2026-10-09", 3010.0), _bar("2026-10-12", 3100.0)]
    entry = debate_entry(quote, bars, datetime.fromisoformat("2026-10-12T12:00:00+08:00"), DAYS)
    assert entry["price"] is None and entry["reason"] == "closed_price_missing"


async def test_a_lunch_break_debate_is_reckoned_from_its_last_price_and_the_five_closes_after_today() -> None:
    # Friday lunch break: today's forming bar has the 3100 the 底稿 shows. The closes after Friday fall all week.
    history = _History()
    history.closes = [3090.0, 3080.0, 3070.0, 3065.0, 3061.0]
    service, desk, now = _service(_Models(verdict=verdict_for_bull), history=history)
    material = desk.debate_material
    lunch = NOW + timedelta(hours=1)
    entry = debate_entry(_closed_quote("2026-10-09T11:30:00+08:00", 3100.0),
                         [_bar("2026-10-08", 3010.0), _bar("2026-10-09", 3100.0)], lunch, desk.trading_days())
    assert entry["source"] == "last" and entry["day"] == "2026-10-09"

    async def at_lunch(symbol: str, **kwargs: Any) -> dict[str, Any]:
        return {**(await material(symbol, **kwargs)), "price": entry["price"], "entry": entry}
    desk.debate_material = at_lunch  # type: ignore[method-assign]
    now.value = lunch
    debate_id = (await service.start("SHFE.rb2701", user_id="u1"))["id"]
    await _finish(service)
    settlement = service.get(debate_id)["settlement"]  # type: ignore[index]
    assert settlement["entryPrice"] == 3100.0 and settlement["entrySource"] == "last"
    assert settlement["tradingDay"] == "2026-10-09" and settlement["due"] == "2026-10-16"
    now.value = datetime(2026, 10, 16, 8, 0, tzinfo=UTC)
    assert await service.settle_due() == 1
    settled = service.get(debate_id)["settlement"]  # type: ignore[index]
    # Friday's own close is not one of the five; from 3100 the week fell, so 偏多 is wrong (from 3010 it was "right").
    assert settled["closes"] == history.closes and settled["outcomes"]["judge"] == "wrong" and settled["outcomes"]["bear"] == "right"


async def test_the_reckoning_counts_five_closes_after_the_entry_prices_own_day() -> None:
    service, desk, _ = _service(_Models(verdict=verdict_for_bull))
    material = desk.debate_material

    async def weekend(symbol: str, **kwargs: Any) -> dict[str, Any]:
        found = await material(symbol, **kwargs)
        return {**found, "price": 3090.0, "entry": {"price": 3090.0, "source": "close", "day": "2026-10-09", "at": "2026-10-09"}}
    desk.debate_material = weekend  # type: ignore[method-assign]
    debate_id = (await service.start("SHFE.rb2701", user_id="u1"))["id"]
    await _finish(service)
    settlement = service.get(debate_id)["settlement"]  # type: ignore[index]
    assert settlement["entryPrice"] == 3090.0 and settlement["entrySource"] == "close"
    assert settlement["tradingDay"] == "2026-10-09" and settlement["due"] == "2026-10-16"
    # A trading-hours quote gone stale: nothing to score from.
    async def stale(symbol: str, **kwargs: Any) -> dict[str, Any]:
        found = await material(symbol, **kwargs)
        return {**found, "price": None, "entry": {"price": None, "source": None, "day": None, "reason": "quote_stale"}}
    desk.debate_material = stale  # type: ignore[method-assign]
    debate_id = (await service.start("SHFE.rb2701", user_id="u1", force=True))["id"]
    await _finish(service)
    settlement = service.get(debate_id)["settlement"]  # type: ignore[index]
    assert settlement["status"] == "void" and settlement["entryPrice"] is None and "行情过期" in settlement["reason"]


async def test_yesterdays_judgment_is_not_todays_benchmark() -> None:
    service, desk, now = _service(_Models(verdict=verdict_for_bull), judgment={
        "status": "ready", "stance": "偏多", "confidence": "中", "headline": "x", "generatedAt": "2026-10-08T02:00:00+00:00",
        "researchDay": "2026-10-08", "today": False})
    debate_id = (await service.start("SHFE.rb2701", user_id="u1"))["id"]
    await _finish(service)
    debate = service.get(debate_id)
    assert debate is not None and debate["compare"] is None
    assert debate["settlement"]["deskStance"] is None and debate["settlement"]["deskMissing"] is True
    assert debate["settlement"]["status"] == "pending"  # the debate itself is still reckoned


async def test_an_events_product_comes_from_the_event_for_every_caller() -> None:
    service, desk, _ = _service(_Models(verdict=verdict_for_bull))

    async def main_symbol(product: str) -> str | None:
        return {"rb": "SHFE.rb2701", "hc": "SHFE.hc2701"}.get(product)
    desk.main_symbol = main_symbol  # type: ignore[method-assign]
    # No symbol (a UI command before any 快讯 row was drawn, voice, an agent): the event's own product.
    started = await service.start(None, event_id=EVENT["id"], user_id="u1")
    assert started["productKey"] == "SHFE.rb" and started["topic"]["eventId"] == EVENT["id"]
    await _finish(service)
    # A symbol of another product is refused, never debated as that product.
    with pytest.raises(EventProductError) as refused:
        await service.start("DCE.i2701", event_id=EVENT["id"], user_id="u1")
    assert refused.value.code == "research_debate_event_product" and refused.value.products == ["rb"]
    # One event about two products: the caller must say which, and may pick either.
    service.event_products = lambda event_id: ["rb", "hc"]
    with pytest.raises(EventProductError) as ambiguous:
        await service.start(None, event_id=EVENT["id"], user_id="u1")
    assert ambiguous.value.code == "research_debate_event_ambiguous" and ambiguous.value.products == ["rb", "hc"]
    assert await service.event_symbol(EVENT["id"], "SHFE.hc2701") == "SHFE.hc2701"
    service.event_products = lambda event_id: []
    with pytest.raises(LookupError, match="research_debate_event_unknown"):
        await service.start(None, event_id=EVENT["id"], user_id="u1")


async def test_an_uncited_sentence_alone_does_not_carry_a_verdict() -> None:
    # The bull's opening line cites nothing (an argument, not evidence): adopted alone, the tier would stand on nothing.
    bull = ("跌到这儿，空头该收手了。RSI 29 进入超卖{M.indicators}。\n总体看法：偏多", BULL_TURNS[1])

    def judge(input_text: str) -> DebateVerdict:
        side = "A" if any("RSI" in item["text"] for item in _cases(input_text)["A 方"]) else "B"
        return DebateVerdict.model_validate({"winner": side, "headline": "略偏多", "adopted": [{"id": f"{side}1"}]})
    service, _, _ = _service(_Models(verdict=judge, bull=bull))
    debate_id = (await service.start("SHFE.rb2701", user_id="u1"))["id"]
    await _finish(service)
    debate = service.get(debate_id)
    assert debate is not None and _sentences(debate, 1)[0]["verified"] and not _sentences(debate, 1)[0]["factIds"]
    assert debate["status"] == "review"


async def test_a_late_settlement_still_reads_the_five_closes_after_the_debate() -> None:
    asked: list[int] = []

    class _Long(_History):
        def market_history(self, symbol: str, duration_seconds: int, length: int) -> dict[str, Any]:
            asked.append(length)
            start = datetime(2026, 10, 9, tzinfo=UTC)
            days = [(start + timedelta(days=index)).date().isoformat() for index in range(90)]
            return {"bars": [{"datetime": 0, "close": 3100.0 + index, "tradingDay": day} for index, day in enumerate(days)][-length:]}
    service, _, now = _service(_Models(verdict=verdict_for_bull), history=_Long())
    debate_id = (await service.start("SHFE.rb2701", user_id="u1"))["id"]
    await _finish(service)
    now.value = datetime(2027, 1, 5, 8, 0, tzinfo=UTC)  # three months late
    await service.settle_due()
    assert asked[-1] > 80
    assert service.get(debate_id)["settlement"]["closes"] == [3101.0, 3102.0, 3103.0, 3104.0, 3105.0]  # type: ignore[index]


async def test_a_stalled_owner_cannot_write_over_a_run_another_slot_reaped() -> None:
    gate = asyncio.Event()

    class _Slow(_Models):
        async def complete_structured(self, **kwargs: Any) -> Any:
            await gate.wait()
            return await super().complete_structured(**kwargs)
    service, desk, now = _service(_Slow(verdict=verdict_for_bull))
    running = await service.start("SHFE.rb2701", user_id="u1")
    await asyncio.sleep(0)
    with service.lock, service.connection:  # another slot reaps it (owner cleared, failed)
        service.connection.execute("UPDATE research_debates SET status = 'failed', owner = NULL WHERE id = ?", (running["id"],))
    gate.set()
    await asyncio.gather(*list(service._tasks.values()), return_exceptions=True)
    with service.lock:
        status = service.connection.execute("SELECT status FROM research_debates WHERE id = ?", (running["id"],)).fetchone()[0]
    assert status == "failed"  # the resumed owner wrote nothing back


async def test_the_question_keeps_czces_three_digit_contract_code() -> None:
    service, desk, _ = _service(_Models(verdict=verdict_for_bull))
    material = desk.debate_material

    async def apple(symbol: str, **kwargs: Any) -> dict[str, Any]:
        return {**await material(symbol, **kwargs), "mainSymbol": "CZCE.AP701", "exchange": "CZCE", "product": "AP", "productName": "苹果"}
    desk.debate_material = apple  # type: ignore[method-assign]
    service.event_products = lambda event_id: ["ap"]
    debate = await service.start("CZCE.AP701", event_id=EVENT["id"], user_id="u1")
    assert debate["topic"]["question"] == "这条消息对 苹果701 意味着什么？"
    await _finish(service)


# #809: whether the market is shut is read from the contract's trading schedule and the exchange calendar, not only
# from the quote — between sessions the source may serve no quote, or one whose session status is unknown.
CU = {"day": [["09:00:00", "10:15:00"], ["10:30:00", "11:30:00"], ["13:30:00", "15:00:00"]], "night": [["21:00:00", "25:00:00"]]}
DAY_ONLY = {"day": [["09:00:00", "10:15:00"], ["10:30:00", "11:30:00"], ["13:30:00", "15:00:00"]], "night": []}
HOLIDAY_DAYS = ["2026-09-28", "2026-09-29", "2026-09-30", "2026-10-09", "2026-10-12", "2026-10-13"]
NO_QUOTE: dict[str, Any] = {}  # the snapshot failed (market_quote_not_ready): nothing but the symbol
UNKNOWN = {"marketSessionStatus": "unknown", "dataStatus": "stale", "isCached": False, "ageSeconds": 19800,
           "quoteDatetime": "2026-10-12T15:00:00+08:00", "quotedAt": "2026-10-12T15:00:00+08:00", "lastPrice": 110640.0}
MONDAY_CLOSE = {"price": 110630.0, "source": "close", "day": "2026-10-12", "at": "2026-10-12"}


@pytest.mark.parametrize("quote", [NO_QUOTE, UNKNOWN, {**UNKNOWN, "marketSessionStatus": "trading"}, {**UNKNOWN, "marketSessionStatus": None}])
@pytest.mark.parametrize("moment", ["2026-10-12T15:40:00+08:00", "2026-10-12T18:30:00+08:00", "2026-10-12T20:15:00+08:00",
                                    "2026-10-12T20:59:59+08:00"])
def test_between_the_day_close_and_the_night_session_enters_at_the_last_close(quote: dict[str, Any], moment: str) -> None:
    # The live 10-12 20:15 case: the trading day had turned to Tuesday, the quote could not vouch for anything.
    bars = [_bar("2026-10-09", 110110.0), _bar("2026-10-12", 110630.0)]
    assert debate_entry(quote, bars, datetime.fromisoformat(moment), DAYS, CU) == MONDAY_CLOSE
    # A placeholder bar for Tuesday before its night session opens is no trade.
    assert debate_entry(quote, [*bars, _bar("2026-10-13", 110630.0)], datetime.fromisoformat(moment), DAYS, CU) == MONDAY_CLOSE


def test_a_product_with_no_night_session_enters_at_the_last_close_until_the_day_session() -> None:
    bars = [_bar("2026-10-09", 3010.0), _bar("2026-10-12", 3100.0)]
    for moment in ("2026-10-12T20:15:00+08:00", "2026-10-12T22:00:00+08:00", "2026-10-13T08:59:00+08:00"):
        assert debate_entry(NO_QUOTE, [*bars, _bar("2026-10-13", 3100.0)], datetime.fromisoformat(moment), DAYS, DAY_ONLY) == {
            "price": 3100.0, "source": "close", "day": "2026-10-12", "at": "2026-10-12"}


def test_after_the_night_session_until_the_day_session_enters_at_the_nights_last_price() -> None:
    bars = [_bar("2026-10-12", 110630.0), _bar("2026-10-13", 111020.0)]  # Tuesday's bar formed by Monday night
    night = {**UNKNOWN, "dataStatus": "normal", "quoteDatetime": "2026-10-13T01:00:00+08:00", "quotedAt": "2026-10-13T01:00:00+08:00",
             "lastPrice": 111020.0}
    for moment in ("2026-10-13T01:30:00+08:00", "2026-10-13T08:30:00+08:00"):
        assert debate_entry(night, bars, datetime.fromisoformat(moment), DAYS, CU) == {
            "price": 111020.0, "source": "last", "day": "2026-10-13", "at": "2026-10-13T01:00:00+08:00"}
        # Tuesday has traded, but no quote can say at what price: no entry, and the reason says the market is shut.
        assert debate_entry(NO_QUOTE, bars, datetime.fromisoformat(moment), DAYS, CU)["reason"] == "closed_price_missing"


def test_the_lunch_break_with_an_unknown_session_status_enters_at_the_last_price() -> None:
    lunch = {**UNKNOWN, "dataStatus": "normal", "quoteDatetime": "2026-10-12T11:30:00+08:00", "quotedAt": "2026-10-12T11:30:00+08:00",
             "lastPrice": 110500.0}
    bars = [_bar("2026-10-09", 110110.0), _bar("2026-10-12", 110500.0)]
    for moment in ("2026-10-12T11:30:01+08:00", "2026-10-12T12:30:00+08:00", "2026-10-12T13:29:59+08:00"):
        assert debate_entry(lunch, bars, datetime.fromisoformat(moment), DAYS, CU) == {
            "price": 110500.0, "source": "last", "day": "2026-10-12", "at": "2026-10-12T11:30:00+08:00"}


@pytest.mark.parametrize(("moment", "days", "schedule", "expected"), [
    ("2026-10-10T10:00:00+08:00", DAYS, DAY_ONLY, ("2026-10-09", 3010.0)),           # Saturday, no night session
    ("2026-10-11T20:30:00+08:00", DAYS, CU, ("2026-10-09", 3010.0)),                 # Sunday evening
    ("2026-10-02T10:00:00+08:00", HOLIDAY_DAYS, CU, ("2026-09-30", 2990.0)),         # National Day, a Friday
    ("2026-10-08T21:30:00+08:00", HOLIDAY_DAYS, CU, ("2026-09-30", 2990.0)),         # no night session after a holiday
])
def test_weekends_and_holidays_enter_at_the_last_close_even_when_the_source_says_trading(
        moment: str, days: list[str], schedule: dict[str, Any], expected: tuple[str, float]) -> None:
    bars = [_bar("2026-09-30", 2990.0)] + ([_bar("2026-10-09", 3010.0)] if days is DAYS else [])
    # The sidecar's own schedule fallback knows no holidays and may say trading with an old quote.
    quote = {**UNKNOWN, "marketSessionStatus": "trading", "quoteDatetime": "2026-09-30T15:00:00+08:00", "lastPrice": expected[1]}
    day, close = expected
    assert debate_entry(quote, bars, datetime.fromisoformat(moment), days, schedule) == {
        "price": close, "source": "close", "day": day, "at": day}


@pytest.mark.parametrize("quote", [NO_QUOTE, UNKNOWN, {**UNKNOWN, "marketSessionStatus": "trading"}])
@pytest.mark.parametrize("moment", ["2026-10-12T10:00:00+08:00", "2026-10-12T14:59:00+08:00", "2026-10-12T21:30:00+08:00"])
def test_a_stale_quote_inside_a_session_has_no_entry(quote: dict[str, Any], moment: str) -> None:
    bars = [_bar("2026-10-09", 110110.0), _bar("2026-10-12", 110630.0), _bar("2026-10-13", 110700.0)]
    entry = debate_entry(quote, bars, datetime.fromisoformat(moment), DAYS, CU)
    assert entry["price"] is None and entry["reason"].startswith("quote_")


def test_without_a_schedule_only_the_quote_can_say_the_market_is_shut() -> None:
    bars = [_bar("2026-10-09", 110110.0), _bar("2026-10-12", 110630.0)]
    moment = datetime.fromisoformat("2026-10-12T20:15:00+08:00")
    assert debate_entry(NO_QUOTE, bars, moment, DAYS, None)["reason"] == "quote_not_current"
    assert debate_entry({**UNKNOWN, "marketSessionStatus": "closed"}, bars, moment, DAYS, None) == MONDAY_CLOSE


def test_the_void_reason_says_whether_the_market_was_shut_or_the_quote_stale() -> None:
    from trader_runtime.research_debate import void_reason
    assert void_reason("quote_not_current") == "交易时段内行情过期，没有发起价"
    assert void_reason("quote_stale") == "交易时段内行情过期，没有发起价"
    assert void_reason("quote_price_missing") == "交易时段内行情缺价格或时间，没有发起价"
    assert void_reason("quote_age_missing") == void_reason("quote_datetime_missing") == "交易时段内行情缺价格或时间，没有发起价"
    assert "休市" in void_reason("closed_price_missing") and "过期" not in void_reason("closed_price_missing")
    assert void_reason("close_missing") == "休市中，没有最近一个交易日的收盘价"
    assert void_reason("market_unavailable") == "没有发起时的价格"


async def test_the_material_reads_the_main_contracts_schedule_for_the_entry() -> None:
    from trader_runtime.research_desk import ResearchDeskService as Desk
    now = _Now()
    now.value = datetime.fromisoformat("2026-10-12T20:15:00+08:00").astimezone(UTC)
    desk = Desk(research=_Research(), news=lambda symbol: asyncio.sleep(0, {}), provider=lambda: None, now=now)  # type: ignore[arg-type]
    bars = [_bar(f"2026-09-{day:02d}", 110000.0 + day) for day in range(14, 30)] + [_bar("2026-10-09", 110110.0), _bar("2026-10-12", 110630.0)]

    async def report(symbol: str, **_: Any) -> dict[str, Any]:
        return {"symbol": symbol, "mainSymbol": "SHFE.cu2611", "exchange": "SHFE", "product": "cu", "productName": "沪铜",
                "sections": [], "facts": [{"id": "M.price", "value": "110640，日涨跌 +0.4%"}], "judgment": {}}

    async def workbench(symbol: str, *, with_quote: bool = False) -> dict[str, Any]:
        return {"quote": {"symbol": symbol}, "bars": bars, "capturedAt": None}

    async def catalog() -> list[dict[str, Any]]:
        return [{"symbol": "SHFE.cu2611", "isMain": True, "productId": "cu", "exchangeId": "SHFE", "tradingSchedule": CU}]
    desk.report = report  # type: ignore[method-assign]
    desk._workbench = workbench  # type: ignore[method-assign]
    desk._catalog_items = catalog  # type: ignore[method-assign]
    material = await desk.debate_material("SHFE.cu2611")
    assert material["price"] == 110630.0 and material["entry"] == MONDAY_CLOSE


def test_a_night_end_written_past_midnight_still_counts_as_the_night_session() -> None:
    # Codex review on #809: a catalog night of 21:00–02:30 is 21:00–26:30, so a stale quote at 01:00 is in session.
    written = {"day": CU["day"], "night": [["21:00:00", "02:30:00"]]}
    bars = [_bar("2026-10-12", 110630.0), _bar("2026-10-13", 110700.0)]
    entry = debate_entry({**UNKNOWN, "dataStatus": "normal", "quoteDatetime": "2026-10-13T00:59:00+08:00", "lastPrice": 110700.0},
                         bars, datetime.fromisoformat("2026-10-13T01:00:00+08:00"), DAYS, written)
    assert entry["price"] is None and entry["reason"] == "quote_stale"


@pytest.mark.parametrize("moment", ["2026-09-30T21:30:00+08:00", "2026-10-02T10:00:00+08:00", "2026-10-08T22:00:00+08:00"])
def test_no_night_session_across_a_holiday_so_a_placeholder_bar_is_no_trade(moment: str) -> None:
    # The night before 10-09 would sit on 09-30's evening, which the exchanges do not hold before a holiday.
    bars = [_bar("2026-09-29", 2980.0), _bar("2026-09-30", 2990.0), _bar("2026-10-09", 2990.0)]
    quote = {**UNKNOWN, "marketSessionStatus": "trading", "quoteDatetime": "2026-09-30T15:00:00+08:00", "lastPrice": 2990.0}
    assert debate_entry(quote, bars, datetime.fromisoformat(moment), HOLIDAY_DAYS, CU) == {
        "price": 2990.0, "source": "close", "day": "2026-09-30", "at": "2026-09-30"}


def test_a_fresh_pre_open_packet_is_not_the_entry() -> None:
    # #809 review: at 20:59:50 the call auction refreshes the packet, but the price is still Monday's last.
    bars = [_bar("2026-10-09", 110110.0), _bar("2026-10-12", 110630.0)]
    auction = {"marketSessionStatus": "trading", "dataStatus": "normal", "isCached": False, "ageSeconds": 1,
               "quoteDatetime": "2026-10-12T20:59:49+08:00", "quotedAt": "2026-10-12T20:59:49+08:00", "lastPrice": 110640.0}
    assert debate_entry(auction, bars, datetime.fromisoformat("2026-10-12T20:59:50+08:00"), DAYS, CU) == MONDAY_CLOSE
    # From the bell the same packet is the quote, on Tuesday's trading day.
    opened = {**auction, "quoteDatetime": "2026-10-12T21:00:01+08:00", "quotedAt": "2026-10-12T21:00:01+08:00"}
    assert debate_entry(opened, bars, datetime.fromisoformat("2026-10-12T21:00:02+08:00"), DAYS, CU)["source"] == "quote"


def test_a_calendar_that_ends_early_infers_no_holiday_beyond_it() -> None:
    # #809 review: a stale calendar ending 10-09 must not turn Monday night's session into a holiday.
    stale = ["2026-10-08", "2026-10-09"]
    bars = [_bar("2026-10-09", 110110.0), _bar("2026-10-12", 110630.0), _bar("2026-10-13", 110700.0)]
    entry = debate_entry(UNKNOWN, bars, datetime.fromisoformat("2026-10-12T21:30:00+08:00"), stale, CU)
    assert entry["price"] is None and entry["reason"].startswith("quote_")


def test_only_the_ids_the_judge_was_shown_are_renamed() -> None:
    """#771/#768：富时A50、中证A500 不是论点编号，B方案、A 方面不是一方；只改裁判真看到的编号与“A 方/B 方”。"""
    from trader_runtime.research_debate import named
    mapping = {"A": "bear", "B": "bull"}
    shown_ids = {"A3": "空3", "B驳1": "多驳1"}
    assert named("富时A50 期指走弱，A3 成立", mapping, shown_ids) == ("富时A50 期指走弱，空3 成立", "富时A50 期指走弱， 成立")
    assert named("中证A500 回调，认同B驳1", mapping, shown_ids) == ("中证A500 回调，认同多驳1", "中证A500 回调，认同")
    assert named("B方案不成立，A 方面看仓单，A方更站得住", mapping, shown_ids)[0] == "B方案不成立，A 方面看仓单，空方更站得住"
    assert named("A股偏弱", mapping, shown_ids)[0] == "A股偏弱"


def test_a_line_exactly_eight_percent_away_stands_on_either_side() -> None:
    """#749：3100 发起、空方 3348 恰好 +8%，浮点比值 8.000000000000007 不能被当成超过 8%。"""
    from trader_runtime.research_debate import invalidation_problem
    facts = [{"id": "K.levels", "value": "最高 3348，最低 2852，均价 3100"}]
    assert invalidation_problem(3348, "略偏空", 3100.0, facts, 8.0) is None
    assert invalidation_problem(2852, "略偏多", 3100.0, facts, 8.0) is None
    assert invalidation_problem(3348.5, "略偏空", 3100.0, [{"id": "K.levels", "value": "最高 3348.5"}], 8.0) == "too_far"


class _RenewAfterSelect:
    """A connection whose owner renews a run's heartbeat right after the reaper's SELECT read it as silent (the race
    between the reaper's two statements, across processes)."""

    def __init__(self, connection: sqlite3.Connection, renew: Any) -> None:
        self._connection, self._renew = connection, renew

    def execute(self, sql: str, *args: Any) -> Any:
        result = self._connection.execute(sql, *args)
        if sql.startswith("SELECT id FROM research_debates WHERE status IN ('queued', 'running')"):
            rows = result.fetchall()
            self._renew(self._connection)
            return iter(rows)
        return result

    def __enter__(self) -> Any:
        return self._connection.__enter__()

    def __exit__(self, *exc: Any) -> Any:
        return self._connection.__exit__(*exc)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)


async def test_a_run_its_owner_renewed_after_the_reapers_read_is_not_reaped() -> None:
    service, desk, now = _service(_Models(verdict=verdict_for_bull))
    running = await service.start("SHFE.rb2701", user_id="u1")
    for task in list(service._tasks.values()):
        task.cancel()
    await asyncio.gather(*list(service._tasks.values()), return_exceptions=True)
    old = (now.value - timedelta(minutes=5)).astimezone(UTC).isoformat(timespec="seconds")
    with service.lock, service.connection:
        service.connection.execute("UPDATE research_debates SET status = 'running', owner = 'other', heartbeat_at = ? WHERE id = ?",
                                   (old, running["id"]))

    def renew(connection: sqlite3.Connection) -> None:
        connection.execute("UPDATE research_debates SET heartbeat_at = ? WHERE id = ?",
                           (now.value.astimezone(UTC).isoformat(timespec="seconds"), running["id"]))
    reaper = ResearchDebateService(desk=desk, connection=_RenewAfterSelect(service.connection, renew),  # type: ignore[arg-type]
                                   lock=service.lock, now=now)
    assert reaper._reap() == 0
    with service.lock:
        assert service.connection.execute("SELECT status FROM research_debates WHERE id = ?", (running["id"],)).fetchone()[0] == "running"


async def test_a_run_reaped_by_another_slot_never_announces_its_own_late_end() -> None:
    """#749：被另一槽回收（已通知“没能完成”）的旧 worker 迟到完成时不再发“研讨完成”。"""
    gate = asyncio.Event()

    class _Slow(_Models):
        async def complete_structured(self, **kwargs: Any) -> Any:
            await gate.wait()
            return await super().complete_structured(**kwargs)
    heard: list[dict[str, Any]] = []

    async def notify(user: str, event: dict[str, Any]) -> None:
        heard.append(event)
    service, _, _ = _service(_Slow(verdict=verdict_for_bull))
    service.notify = notify
    running = await service.start("SHFE.rb2701", user_id="u1")
    await asyncio.sleep(0)
    with service.lock, service.connection:
        service.connection.execute("UPDATE research_debates SET status = 'failed', owner = NULL WHERE id = ?", (running["id"],))
    gate.set()
    await asyncio.gather(*list(service._tasks.values()), return_exceptions=True)
    assert not any(event["kind"] == "done" for event in heard)


async def test_a_debate_cut_by_a_shutdown_is_announced_by_the_next_slot_once() -> None:
    """#744：关停时取消进行中的研讨存为 failed，等的人由下一个活着的槽位提醒一次（不是没人提醒）。"""
    gate = asyncio.Event()

    class _Slow(_Models):
        async def complete_structured(self, **kwargs: Any) -> Any:
            await gate.wait()
            return await super().complete_structured(**kwargs)
    service, desk, now = _service(_Slow(verdict=verdict_for_bull))
    running = await service.start("SHFE.rb2701", user_id="u1")
    await asyncio.sleep(0)
    await service.close()
    assert service.get(running["id"])["status"] == "failed"  # type: ignore[index]
    assert "pendingNotice" not in service.get(running["id"])  # type: ignore[operator]
    heard: list[tuple[str, str, str]] = []

    async def notify(user: str, event: dict[str, Any]) -> None:
        heard.append((user, event["kind"], event.get("title", "")))
    following = ResearchDebateService(desk=desk, connection=service.connection, lock=service.lock, now=now, notify=notify)
    await following._announce_reaped()
    following._reap()
    await following._announce_reaped()
    assert heard == [("u1", "done", "研讨没能完成")]


async def test_gathering_across_0830_answers_with_the_new_research_days_result() -> None:
    """#744：发起时还在 08:30 前、资料收集跨过 08:30：缓存与最新一场按资料的研究日查，不另起一场。"""
    service, desk, now = _service(_Models(verdict=verdict_for_bull))
    now.value = datetime.fromisoformat("2026-10-09T08:31:00+08:00").astimezone(UTC)
    first = await service.start("SHFE.rb2701", user_id="u1")
    await _finish(service)
    assert service.get(first["id"])["status"] == "done"  # type: ignore[index]
    gather = desk.debate_material

    async def slow_material(symbol: str, **kwargs: Any) -> dict[str, Any]:
        now.value = datetime.fromisoformat("2026-10-09T08:31:30+08:00").astimezone(UTC)
        return await gather(symbol, **kwargs)
    desk.debate_material = slow_material  # type: ignore[method-assign]
    now.value = datetime.fromisoformat("2026-10-09T08:29:50+08:00").astimezone(UTC)
    again = await service.start("SHFE.rb2701", user_id="u1")
    assert again["reused"] is True and again["id"] == first["id"]


async def test_a_quote_read_after_a_slow_read_is_not_from_the_future() -> None:
    """#749：读行情花了几秒，读回的新鲜报价按读回时刻判，不被判成“来自未来”而作废对账。"""
    from trader_runtime.research_desk import ResearchDeskService as Desk
    now = _Now()
    desk = Desk(research=_Research(), news=lambda symbol: asyncio.sleep(0, {}), provider=lambda: None, now=now)  # type: ignore[arg-type]
    bars = [{"close": 103.0 + index * 0.005, "tradingDay": f"2026-09-{index + 10:02d}"} for index in range(18)] + [
        {"close": 103.08, "tradingDay": "2026-10-08"}, {"close": 103.1, "tradingDay": "2026-10-09"}]

    async def report(symbol: str, **_: Any) -> dict[str, Any]:
        return {"symbol": symbol, "mainSymbol": "CFFEX.T2612", "exchange": "CFFEX", "product": "T", "productName": "十年国债",
                "sections": [], "facts": [{"id": "M.price", "value": "103.125，日涨跌 +0.1%"}], "judgment": {}}

    async def workbench(symbol: str, *, with_quote: bool = False) -> dict[str, Any]:
        now.value = NOW + timedelta(seconds=5)  # the read took five seconds; the quote is from its end
        return {"quote": {"lastPrice": 103.125, "dataStatus": "normal", "ageSeconds": 0, "quoteDatetime": now.value.isoformat(),
                          "marketSessionStatus": "trading"}, "bars": bars, "capturedAt": None}
    desk.report = report  # type: ignore[method-assign]
    desk._workbench = workbench  # type: ignore[method-assign]
    material = await desk.debate_material("CFFEX.T2612")
    assert material["entry"]["source"] == "quote" and material["price"] == 103.125
    assert material["researchDay"] == "2026-10-09"


async def test_a_voice_start_without_an_answer_never_claims_a_start(monkeypatch) -> None:
    from trader_runtime import turn_router
    monkeypatch.setattr(turn_router, "DEBATE_POLL_SECONDS", 0)

    async def call(operation: str, **_: Any) -> dict[str, Any]:
        return {"data": {"state": {}}}
    for state in ({}, {"researchDebate": {"open": False}}, None):
        line = await turn_router.debate_start_line(state, "w1", call, "AI 们开始研讨了，好了会提醒你")
        assert "开始研讨了" not in line and "没确认" in line


async def test_each_turn_streams_to_its_watchers_and_the_checked_version_replaces_it() -> None:
    models = _Models(verdict=verdict_for_bull)
    service, _, _ = _service(models)
    heard: list[dict[str, Any]] = []

    async def notify(user: str, event: dict[str, Any]) -> None:
        heard.append({"user": user, **event})
    service.notify = notify
    ticks = iter(range(0, 10_000))
    service.clock = lambda: float(next(ticks))  # every delta is past DELTA_SECONDS
    debate_id = (await service.start("SHFE.rb2701", user_id="u1"))["id"]
    await _finish(service)
    deltas = [event for event in heard if event["kind"] == "delta" and "turn" in event]
    # Every turn streamed (half, then all), in order, the whole text so far each time and without the 总体看法 line.
    assert [(event["turn"], event["side"]) for event in deltas] == [(1, "bull"), (1, "bull"), (2, "bear"), (2, "bear"),
                                                                     (3, "bull"), (3, "bull"), (4, "bear"), (4, "bear")]
    assert all(event["user"] == "u1" and event["id"] == debate_id and "总体看法" not in event["text"] for event in deltas)
    assert deltas[1]["text"].startswith(deltas[0]["text"]) and deltas[1]["text"].endswith("[F1]。")
    # Each turn's end is a progress nudge; the record then holds the checked sentences, not the stream.
    kinds = [event["kind"] for event in heard]
    assert kinds.index("progress", kinds.index("delta")) < [index for index, event in enumerate(heard)
                                                            if event["kind"] == "delta" and event.get("turn") == 2][0]
    turn = service.get(debate_id)["turns"][0]  # type: ignore[index]
    assert turn["status"] == "done" and turn["text"] == deltas[1]["text"] and [item["id"] for item in turn["sentences"]] == ["多1", "多2"]
    # Deltas are never saved (only progress is): the stored record has no half turn.
    stored = service._stored(debate_id)
    assert stored is not None and all(item["status"] == "done" for item in stored["turns"])


async def test_a_reload_mid_turn_shows_the_finished_turns_and_the_text_so_far() -> None:
    models = _Models(verdict=verdict_for_bull)
    models.hold = asyncio.Event()
    service, _, _ = _service(models)
    debate_id = (await service.start("SHFE.rb2701", user_id="u1"))["id"]
    for _ in range(200):  # until the third turn is held after its first piece
        await asyncio.sleep(0)
        if models.speaking and len(models.calls) == 3:
            break
    await asyncio.sleep(0)
    shown = service.get(debate_id)
    assert shown is not None and shown["status"] == "running"
    assert [turn["status"] for turn in shown["turns"]] == ["done", "done", "run"]
    assert [item["id"] for item in shown["turns"][1]["sentences"]] == ["空1", "空2", "空3"]
    assert shown["turns"][2]["text"] and "你说仓单在增".startswith(shown["turns"][2]["text"][:3])
    assert [step["status"] for step in shown["steps"]] == ["done", "done", "done", "done", "run", "wait", "wait"]
    models.hold.set()
    await _finish(service)
    assert service.get(debate_id)["status"] == "done"  # type: ignore[index]


async def test_a_record_from_before_the_turns_still_reads_and_adopts() -> None:
    """Debates stored before 2026-10-24 have two parallel rounds of structured arguments: read as four turns."""
    service, desk, _ = _service(_Models(verdict=verdict_for_bull))
    debate_id = (await service.start("SHFE.rb2701", user_id="u1"))["id"]
    await _finish(service)
    record = service._stored(debate_id)
    assert record is not None
    argument = {"id": "多1", "side": "bull", "text": "RSI 29 进入超卖", "factIds": ["M.indicators"], "quote": "RSI 29（超卖）", "verified": True}
    record.pop("turns")
    record["rounds"] = {"opening": {"bull": [argument], "bear": []}, "rebuttal": None, "round2Skipped": "same_direction"}
    record["verdict"]["adopted"] = [{"id": "多1", "side": "bull", "reason": ""}]
    # …and from before the analysts (2026-10-24): no reports, no 上次复盘, no disagreements or scenarios.
    for key in ("analysts", "lesson", "searches"):
        record.pop(key)
    for key in ("disagreements", "scenarios"):
        record["verdict"].pop(key)
    service._save(record)
    shown = service.get(debate_id)
    assert shown is not None and [(turn["side"], len(turn["sentences"])) for turn in shown["turns"]] == [("bull", 1), ("bear", 0)]
    assert "analysts" not in shown and shown["verdict"]["disagreements"] == [] and shown["verdict"]["scenarios"] == []
    service.adopt(debate_id)
    assert desk._judgments["SHFE.rb"]["value"]["points"][0]["quote"] == "RSI 29（超卖）"


async def test_a_foreign_product_can_be_debated_and_is_not_reckoned_on_the_domestic_calendar() -> None:
    """2026-10-21 所有者：外盘也要能“让 AI 们辩一轮”。纳指的研讨照常两轮加裁判、能写进今日判断；对账按国内日历与 15:30
    收盘取五个交易日的收盘价，套不到 CME 上——外盘明说不计入，不拿错的口径打分。快讯事件按它的快讯键（nasdaq）归属。"""
    from trader_runtime.research_debate import FOREIGN_SETTLEMENT_VOID

    models = _Models(verdict=verdict_for_bull)
    service, desk, now = _service(models)

    async def foreign_material(symbol: str, **_: Any) -> dict[str, Any]:
        return {"symbol": symbol, "mainSymbol": "KQD.m@CME.NQ", "exchange": "FOREIGN", "product": "CME.NQ",
                "productName": "迷你纳指100", "facts": [dict(fact) for fact in FACTS], "sections": [], "price": 29900.75,
                "entry": {"price": 29900.75, "source": "quote", "day": None, "reason": None},
                "researchDay": desk.research_day(now()), "judgment": {}}
    desk.debate_material = foreign_material  # type: ignore[method-assign]
    started = await service.start("KQD.m@CME.NQ", user_id="u1")
    assert started["status"] == "queued"
    await _finish(service)
    debate = service.get(started["id"])
    assert debate is not None and debate["status"] == "done"
    assert debate["settlement"]["status"] == "void" and debate["settlement"]["reason"] == FOREIGN_SETTLEMENT_VOID
    assert service.listing(product="KQD.m@CME.NQ")["items"][0]["id"] == started["id"]
    service.event_products = lambda event_id: ["nasdaq"]  # type: ignore[assignment]
    assert await service.event_symbol("e-nq", "KQD.m@CME.NQ") == "KQD.m@CME.NQ"


def test_the_judge_is_deepseek_flash_and_falls_back_to_kimi():
    """所有者 2026-10-25：裁判默认 DeepSeek Flash，Kimi K3.1 Pro 退为备用；空方仍回退到 DeepSeek Flash。"""
    from trader_runtime.model_provider import KIMI_MODEL
    from trader_runtime.research_debate import DEFAULT_FALLBACKS, DEFAULT_MODELS
    assert DEFAULT_MODELS["judge"] == "deepseek-flash"
    assert DEFAULT_FALLBACKS == {"bear": "deepseek-flash", "judge": KIMI_MODEL}


async def test_a_judge_that_cannot_answer_hands_the_verdict_to_its_stand_in() -> None:
    class _KimiDown(_Models):
        async def complete_structured(self, *, schema: type, **call: Any) -> Any:
            if call.get("model") == "judge-fixture":
                self._record(**call)
                raise NativeAgentError("model_stream_interrupted")
            return await super().complete_structured(schema=schema, **call)
    models = _KimiDown(verdict=verdict_for_bull)
    service, _, _ = _service(models, fallbacks={"bear": "k3.1-pro", "judge": "deepseek-flash"})
    debate_id = (await service.start("SHFE.rb2701", user_id="u1"))["id"]
    await _finish(service)
    debate = service.get(debate_id)
    assert debate is not None and debate["status"] == "done" and debate["models"]["judge"] == "deepseek-flash"
    assert debate["fallbacks"] == [{"role": "judge", "stage": "verdict", "from": "judge-fixture", "to": "deepseek-flash",
                                    "reason": "model_stream_interrupted"}]
    assert [call["model"] for call in debate["calls"] if call["role"] == "judge"] == ["judge-fixture", "deepseek-flash"]


def test_a_bare_a_or_b_the_judge_wrote_reads_as_the_real_side() -> None:
    """Live 10-24: the judge wrote "A抓住…", "B的期货…", "可改判B" without 方; the reader sees 空方 / 多方. A股 / B级 stay."""
    from trader_runtime.research_debate import named
    mapping = {"A": "bear", "B": "bull"}
    assert named("A抓住表需新低，B的期货低100.5，收盘站上3118可改判B。", mapping, {})[0] == (
        "空方抓住表需新低，多方的期货低100.5，收盘站上3118可改判多方。")
    assert named("A股情绪、B级债与A 方面都不是一方", mapping, {})[0] == "A股情绪、B级债与A 方面都不是一方"


# -- 分析师 (owner 2026-10-24): four reports before the turns, gap-only web search, 上次复盘, the judge's new fields

class _Search:
    """The shared web search: one page with a figure and one without per query; a 利润 query fails."""

    def __init__(self) -> None:
        self.queries: list[str] = []

    async def search(self, query: str, *, count: int = 5) -> dict[str, Any]:
        self.queries.append(query)
        if "利润" in query:
            raise WebSearchError("web_search_unavailable")
        return {"query": query, "results": [
            {"title": "247家钢厂高炉开工率 82.1%", "content": "247 家钢厂高炉开工率 82.1%，环比下降 0.6 个百分点",
             "link": f"https://example.com/{len(self.queries)}/a", "media": "我的钢铁网", "publishDate": "2026-10-08"},
            {"title": "钢厂开工情况", "content": "开工情况一般", "link": "https://example.com/plain", "media": "某网", "publishDate": ""}]}


async def test_the_four_analysts_write_at_once_stream_and_their_checked_reports_reach_both_sides_and_the_judge() -> None:
    models = _Models(verdict=verdict_for_bull)
    service, _, _ = _service(models)
    heard: list[dict[str, Any]] = []

    async def notify(user: str, event: dict[str, Any]) -> None:
        heard.append(event)
    service.notify = notify
    ticks = iter(range(0, 10_000))
    service.clock = lambda: float(next(ticks))  # every delta is past DELTA_SECONDS
    debate_id = (await service.start("SHFE.rb2701", user_id="u1"))["id"]
    await _finish(service)
    debate = service.get(debate_id)
    assert debate is not None and debate["status"] == "done"
    # All four at the same time, each streamed to the windows under its own key, before the first turn.
    assert models.most_writing == 4
    streamed = [index for index, event in enumerate(heard) if event["kind"] == "delta" and "analyst" in event]
    assert {heard[index]["analyst"] for index in streamed} == {"technical", "supply", "flow", "news"}
    first_turn = next(index for index, event in enumerate(heard) if event["kind"] == "delta" and "turn" in event)
    assert max(streamed) < first_turn
    reports = debate["analysts"]
    assert [(report["key"], report["title"], report["status"]) for report in reports] == [
        ("technical", "技术面", "done"), ("supply", "供需基本面", "done"), ("flow", "资金持仓", "done"), ("news", "新闻宏观", "done")]
    technical = reports[0]
    assert technical["conclusion"] == {"text": "多空交织", "factIds": [], "verified": True}
    assert technical["sentences"][0]["id"] == "技1" and technical["sentences"][0]["verified"] and technical["sentences"][0]["factIds"] == ["M.trend"]
    assert technical["points"] == [{"label": "资金", "text": "观望", "factIds": [], "verified": True}]
    assert all(call["max"] == 3000 for call in models.analyst_calls)
    # Each analyst reads its own area (and the price): the 资金持仓 analyst never sees the 技术面 trend.
    flow_input = next(call["input"] for call in models.analyst_calls if "资金持仓分析师" in call["instructions"])
    assert "前20会员净多持仓" in flow_input and "近20个交易日" not in flow_input
    # The checked reports go to every turn and to the judge (before the A/B cases, which stay the last line).
    assert all("【技术面分析师】结论：多空交织。" in call["input"] and "【资金持仓分析师】" in call["input"] for call in models.calls)
    assert "多方" not in models.calls[-1]["input"].rsplit("\n", 1)[-1]
    assert next(step for step in debate["steps"] if step["key"] == "analysts")["detail"] == {"done": 4, "failed": 0, "searches": 0}


async def test_an_analyst_that_fails_is_marked_and_the_debate_goes_on_without_it() -> None:
    models = _Models(verdict=verdict_for_bull, reports={"资金持仓": NativeAgentError("model_rate_limited")})
    service, _, _ = _service(models)
    debate_id = (await service.start("SHFE.rb2701", user_id="u1"))["id"]
    await _finish(service)
    debate = service.get(debate_id)
    assert debate is not None and debate["status"] == "done" and debate["verdict"] is not None
    flow = next(report for report in debate["analysts"] if report["key"] == "flow")
    assert flow["status"] == "failed" and flow["error"] == "model_rate_limited" and flow["conclusion"] is None
    assert [report["status"] for report in debate["analysts"]].count("done") == 3
    assert all("【资金持仓分析师】没写成" in call["input"] for call in models.calls)
    assert next(step for step in debate["steps"] if step["key"] == "analysts")["detail"]["failed"] == 1


async def test_analysts_search_only_their_gaps_and_every_kept_figure_becomes_a_checkable_fact() -> None:
    def plan(input_text: str) -> SearchPlan:
        return SearchPlan.model_validate({"queries": [
            {"gap": "现货基差", "query": "螺纹钢 现货基差"},  # the desk holds it: not a gap, never searched
            {"gap": "开工率", "query": "螺纹钢 高炉开工率"},
            {"gap": "开工率", "query": "螺纹钢 开工率 again"},  # one query per gap
            {"gap": "利润", "query": "螺纹钢 利润 after:2026-10-01"}]})
    report = "结论：开工回落\n{fact}。据报道高炉开工率 82.1%{W}。高炉开工率 90%{W}。\n要点：\n开工：回落"
    models = _Models(verdict=verdict_for_bull, plan=plan, reports={"供需基本面": report})
    service, _, _ = _service(models)
    search = _Search()
    service.search = search
    debate_id = (await service.start("SHFE.rb2701", user_id="u1"))["id"]
    await _finish(service)
    debate = service.get(debate_id)
    assert debate is not None and debate["status"] == "done"
    # Only an area with gaps plans (技术面 never searches: prices come from the market provider). The plan is told
    # the gaps and what the desk already holds.
    plans = [call["input"] for call in models.analyst_calls if call.get("plan")]
    assert len(plans) == 3
    supply_plan = next(text for text in plans if "开工率" in text)
    assert "缺口：开工率、利润" in supply_plan and "调研页已有（不要再搜）：" in supply_plan and "现货与基差" in supply_plan
    assert "缺口：持仓与成交量" in "".join(plans) and "缺口：宏观数据" in "".join(plans)
    # Only the listed gaps were searched, one query each, recent results only.
    assert search.queries == ["螺纹钢 高炉开工率 after:2026-09-25", "螺纹钢 利润 after:2026-10-01"]
    supply = next(item for item in debate["analysts"] if item["key"] == "supply")
    assert [(entry["gap"], entry["found"], entry.get("error")) for entry in supply["searches"]] == [
        ("开工率", 1, None), ("利润", 0, "web_search_unavailable")]
    # The page with a figure is a fact now (source, date, link, a 资料来源 number); the one without is not kept.
    found = [fact for fact in debate["facts"] if fact["id"].startswith("W.")]
    assert len(found) == 1 and supply["found"] == [found[0]["id"]]
    assert found[0] | {"id": "", "n": 0} == {
        "id": "", "n": 0, "section": "web", "kind": "report", "label": "247家钢厂高炉开工率 82.1%",
        "value": "247 家钢厂高炉开工率 82.1%，环比下降 0.6 个百分点", "source": "我的钢铁网", "asOf": "2026-10-08",
        "url": "https://example.com/1/a", "sheet": "basis", "found": {"analyst": "supply", "gap": "开工率"}}
    assert found[0]["n"] == max(fact["n"] for fact in debate["facts"])
    # The report's figure from that page checks against it; one the page never said does not.
    assert [(item["text"][:7], item["verified"]) for item in supply["sentences"][1:]] == [("据报道高炉开工", True), ("高炉开工率 9", False)]
    # The sides and the judge can cite it: it is in the 底稿 every turn starts with.
    assert all(found[0]["id"] in call["input"] for call in models.calls)
    assert next(step for step in debate["steps"] if step["key"] == "analysts")["detail"]["searches"] == 2


async def test_the_search_cap_holds_across_the_analysts(monkeypatch: pytest.MonkeyPatch) -> None:
    from trader_runtime import research_debate
    monkeypatch.setattr(research_debate, "SEARCHES_PER_DEBATE", 1)

    def plan(input_text: str) -> SearchPlan:
        gaps = re.search(r"缺口：(.+)", input_text)[1].split("、")  # type: ignore[index]
        return SearchPlan.model_validate({"queries": [{"gap": gap, "query": f"螺纹钢 {gap}"} for gap in gaps]})
    models = _Models(verdict=verdict_for_bull, plan=plan)
    service, _, _ = _service(models)
    search = _Search()
    service.search = search
    debate_id = (await service.start("SHFE.rb2701", user_id="u1"))["id"]
    await _finish(service)
    debate = service.get(debate_id)
    assert debate is not None and debate["status"] == "done" and len(search.queries) == 1 and debate["searches"] == 1


async def test_the_lesson_comes_from_the_products_last_settled_debate_and_reaches_both_sides_and_the_judge() -> None:
    history = _History()
    history.closes = [3110.0, 3090.0, 3120.0, 3130.0, 3150.0]
    models = _Models(verdict=verdict_for_bull)
    service, _, now = _service(models, history=history)
    first = (await service.start("SHFE.rb2701", user_id="u1"))["id"]
    await _finish(service)
    assert service.get(first)["lesson"] is None  # type: ignore[index]  # nothing settled yet
    now.value = datetime(2026, 10, 16, 8, 0, tzinfo=UTC)
    assert await service.settle_due() == 1  # the bull won and the price rose: the judge was right
    models.calls.clear()
    second = (await service.start("SHFE.rb2701", user_id="u1"))["id"]
    await _finish(service)
    lesson = service.get(second)["lesson"]  # type: ignore[index]
    assert lesson == {"id": first, "researchDay": "2026-10-09", "outcome": "right",
                      "text": "上次（10-09）裁判判多方略胜（略偏多），5 个交易日后说对了：多方看对了，空方当时说“仓单较上日 +5000，库存还在累”，没有兑现。"}
    assert models.calls and all(f"上次复盘：{lesson['text']}" in call["input"] for call in models.calls)


def test_a_lesson_needs_a_scored_verdict() -> None:
    from trader_runtime.research_debate import lesson_of
    assert lesson_of({"id": "db1", "settlement": {"status": "void"}, "verdict": {"winner": "bull"}}) is None
    draw = {"id": "db2", "researchDay": "2026-10-09", "settlement": {"status": "settled", "outcomes": {"judge": "wrong"}},
            "verdict": {"winner": None, "stance": "震荡"}, "turns": []}
    assert lesson_of(draw)["text"] == "上次（10-09）裁判判双方相当（震荡），5 个交易日后说错了，价格走出了方向。"  # type: ignore[index]


def test_a_longer_turn_reasons_in_full_and_still_passes_the_checks() -> None:
    by_id = {fact["id"]: fact for fact in FACTS}
    by_n = {7: "M.indicators", 3: "D.warehouse_receipt"}
    chain = "RSI 29 进入超卖，说明前面跌得急，空头的力气在一点点耗掉，再往下砸的人越来越少[F7]。"
    turn = chain * 9 + "仓单较上日 +5000，看着是累库，可这是检修期的季节性，传不到价格上[F3]。"
    checked = check_turn("bull", turn, 1, by_n, by_id)
    assert 400 < sum(len(item["text"]) for item in checked) and all(item["verified"] for item in checked)
    assert not checked[-1]["text"].endswith("…") and len(checked) == 10
    assert not check_turn("bull", "仓单较上日 +6000，库存在累[F3]。", 1, by_n, by_id)[0]["verified"]


async def test_the_judge_names_the_key_disagreements_and_if_then_scenarios() -> None:
    def verdict(input_text: str) -> DebateVerdict:
        base = verdict_for_bull(input_text)
        bull = base.winner
        return base.model_copy(update={
            "disagreements": [f"{bull}方看超卖，另一方看仓单", "仓单增加是季节性还是趋势"],
            "scenarios": ["如果仓单继续增加，那么超卖反弹的逻辑会被削弱", f"如果收盘跌破 3060，那么{bull}方的理由不再成立",
                          "如果跌到 2999，那么谁都说不清"]})  # 2999 is in no fact: dropped
    service, _, _ = _service(_Models(verdict=verdict))
    debate_id = (await service.start("SHFE.rb2701", user_id="u1"))["id"]
    await _finish(service)
    result = service.get(debate_id)["verdict"]  # type: ignore[index]
    assert result["disagreements"] == ["多方看超卖，另一方看仓单", "仓单增加是季节性还是趋势"]
    assert result["scenarios"] == ["如果仓单继续增加，那么超卖反弹的逻辑会被削弱", "如果收盘跌破 3060，那么多方的理由不再成立"]
