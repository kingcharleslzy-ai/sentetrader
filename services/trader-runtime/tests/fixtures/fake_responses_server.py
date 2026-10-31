"""In-process fake of an OpenAI Responses streaming endpoint.

The fake answers `POST /responses` over an httpx MockTransport so the native
agent loop can be exercised without a network, a subprocess or a real key.
Scenarios are keyed by phrases in the latest user message so the behavior
tests read like the conversations they describe.
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import Iterable
from typing import Any

import httpx

from fixtures.card_tools import legacy_card_args
from trader_runtime.turn_router import ROUTE_TOOLSETS

CORE_TOOLS = {
    "get_runtime_data_status", "get_account_summary", "get_positions",
    "get_account_activity", "search_instruments", "get_market_snapshot",
    "get_market_analysis", "get_futures_decision_context",
}


# A turn routed by the deterministic router (turn_router.classify_turn) carries exactly one of these
# fixed subsets instead of the full catalogue; a routed request is valid when its tools are one of
# them (or a superset, so a general turn that happens to include them all still passes).
def _routed(names: set[str]) -> bool:
    return any(tools <= names for tools in ROUTE_TOOLSETS)


def sse(event: dict[str, Any]) -> bytes:
    return (
        f"event: {event['type']}\n"
        f"data: {json.dumps(event, ensure_ascii=False, separators=(',', ':'))}\n\n"
    ).encode()


class FakeResponsesServer:
    """Scenario router returning Responses SSE streams for the native agent."""

    def __init__(
        self, *, reasoning_summary: bool = True, include_usage: bool = True,
        expected_keys: dict[str, str] | None = None,
    ) -> None:
        self.reasoning_summary = reasoning_summary
        self.include_usage = include_usage
        self.expected_keys = expected_keys or {}
        self.requests: list[dict[str, Any]] = []
        self.authorizations: list[str] = []
        self.counter = 0

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    # -- request routing -----------------------------------------------------

    async def handle(self, request: httpx.Request) -> httpx.Response:
        if not request.url.path.endswith("/responses"):
            return httpx.Response(404, json={"error": {"message": "not found"}})
        body = json.loads(request.content or b"{}")
        self.requests.append(body)
        self.authorizations.append(request.headers.get("authorization", ""))
        expected = self.expected_keys.get(str(body.get("model") or ""))
        if expected is not None and request.headers.get("authorization") != f"Bearer {expected}":
            return httpx.Response(401, json={"error": {"message": "invalid api key"}})
        if not self._contract_valid(body):
            return httpx.Response(400, json={"error": {"message": "invalid native agent request"}})
        text = self._latest_user_text(body)
        outputs = self._tool_outputs(body)
        if "服务端错误" in text:
            return httpx.Response(500, json={"error": {"message": "upstream exploded"}})
        if "流内限流" in text:
            # GLM's 429 on a streamed request (2026-10-12): HTTP 200, then one response.failed event.
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse({
                "type": "response.failed", "sequence_number": 0, "response": {
                    "id": "resp-limited", "object": "response", "created_at": 0, "model": str(body.get("model")),
                    "status": "failed", "output": [], "error": {"code": "rate_limit_exceeded", "message": "已达到使用上限"}}}))
        if "永不返回" in text and not outputs:
            await asyncio.sleep(30)
        events = list(self._scenario(text, outputs, body))
        stream = b"".join(sse(event) for event in events)
        if "流中断" in text and not outputs:
            stream = stream[: len(stream) // 2]
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=stream)

    def _contract_valid(self, body: dict[str, Any]) -> bool:
        tools = body.get("tools") or []
        names = {tool.get("name") for tool in tools if isinstance(tool, dict)}
        instructions = str(body.get("instructions") or "")
        if body.get("stream") is not True or not body.get("model"):
            return False
        if self._schema_name(body):
            return bool(instructions)
        if names and not all(
            tool.get("type") == "function" and isinstance(tool.get("parameters"), dict)
            for tool in tools
        ):
            return False
        # A conversation turn always carries the developer contract and the core tools;
        # side-channel calls (explanations, briefs, design) carry their own instructions.
        return bool(instructions) and (not names or CORE_TOOLS <= names or "web_search" in names
                                       or _routed(names))

    @staticmethod
    def _schema_name(body: dict[str, Any]) -> str:
        text = body.get("text") or {}
        fmt = text.get("format") if isinstance(text, dict) else None
        if isinstance(fmt, dict) and fmt.get("type") == "json_schema":
            return str(fmt.get("name") or "schema")
        return ""

    @staticmethod
    def _latest_user_text(body: dict[str, Any]) -> str:
        for item in reversed(body.get("input") or []):
            if isinstance(item, dict) and item.get("role") == "user":
                content = item.get("content")
                if isinstance(content, str):
                    return content
                if isinstance(content, list):
                    return " ".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
        return ""

    @staticmethod
    def _tool_outputs(body: dict[str, Any]) -> list[dict[str, Any]]:
        outputs = []
        for item in body.get("input") or []:
            if isinstance(item, dict) and item.get("type") == "function_call_output":
                try:
                    payload = json.loads(str(item.get("output") or "{}"))
                except json.JSONDecodeError:
                    payload = {}
                outputs.append({"call_id": item.get("call_id"), "payload": payload})
        return outputs

    # -- stream builders -----------------------------------------------------

    def _ids(self) -> tuple[str, str]:
        self.counter += 1
        return f"resp-{self.counter}", f"msg-{self.counter}"

    def _text_stream(self, text: str, *, chunks: Iterable[str] | None = None,
                     with_reasoning: bool = False, incomplete: bool = False) -> list[dict[str, Any]]:
        response_id, item_id = self._ids()
        events: list[dict[str, Any]] = [{"type": "response.created", "response": {"id": response_id, "status": "in_progress"}}]
        if with_reasoning and self.reasoning_summary:
            events.append({"type": "response.output_item.added", "output_index": 0,
                           "item": {"id": f"rs-{response_id}", "type": "reasoning", "summary": []}})
            for delta in ("先核对账户", "，再看盘面。"):
                events.append({"type": "response.reasoning_summary_text.delta", "item_id": f"rs-{response_id}",
                               "output_index": 0, "summary_index": 0, "delta": delta})
            events.append({"type": "response.output_item.done", "output_index": 0,
                           "item": {"id": f"rs-{response_id}", "type": "reasoning",
                                    "summary": [{"type": "summary_text", "text": "先核对账户，再看盘面。"}]}})
        events.append({"type": "response.output_item.added", "output_index": 1,
                       "item": {"id": item_id, "type": "message", "role": "assistant", "content": []}})
        for delta in (chunks if chunks is not None else [text]):
            events.append({"type": "response.output_text.delta", "item_id": item_id,
                           "output_index": 1, "content_index": 0, "delta": delta})
        message = {"id": item_id, "type": "message", "role": "assistant", "status": "completed",
                   "content": [{"type": "output_text", "text": text, "annotations": []}]}
        events.append({"type": "response.output_text.done", "item_id": item_id, "output_index": 1,
                       "content_index": 0, "text": text})
        events.append({"type": "response.output_item.done", "output_index": 1, "item": message})
        final = {"id": response_id, "status": "incomplete" if incomplete else "completed",
                 "output": [message]}
        if incomplete:
            final["incomplete_details"] = {"reason": "max_output_tokens"}
        if self.include_usage:
            final["usage"] = {"input_tokens": 120, "output_tokens": 40, "total_tokens": 160,
                              "input_tokens_details": {"cached_tokens": 96}}
        events.append({"type": "response.incomplete" if incomplete else "response.completed", "response": final})
        return events

    def _tool_stream(self, calls: list[tuple[str, dict[str, Any]]], *, with_reasoning: bool = False) -> list[dict[str, Any]]:
        response_id, _ = self._ids()
        events: list[dict[str, Any]] = [{"type": "response.created", "response": {"id": response_id, "status": "in_progress"}}]
        items = []
        if with_reasoning:
            # DeepSeek's thinking mode: an opaque reasoning item that must come
            # back verbatim with the tool outputs, or the follow-up is rejected.
            reasoning = {"id": f"rs-{response_id}", "type": "reasoning", "summary": [],
                         "reasoning_text": f"thinking-{response_id}"}
            events.append({"type": "response.output_item.added", "output_index": 0, "item": reasoning})
            events.append({"type": "response.output_item.done", "output_index": 0, "item": reasoning})
            items.append(reasoning)
        for index, (name, arguments) in enumerate(calls, start=len(items)):
            call_id = f"call-{response_id}-{index}"
            item = {"id": f"fc-{response_id}-{index}", "type": "function_call", "call_id": call_id,
                    "name": name, "arguments": ""}
            events.append({"type": "response.output_item.added", "output_index": index, "item": item})
            encoded = json.dumps(arguments, ensure_ascii=False)
            events.append({"type": "response.function_call_arguments.delta", "item_id": item["id"],
                           "output_index": index, "delta": encoded})
            events.append({"type": "response.function_call_arguments.done", "item_id": item["id"],
                           "output_index": index, "arguments": encoded})
            done = {**item, "arguments": encoded, "status": "completed"}
            items.append(done)
            events.append({"type": "response.output_item.done", "output_index": index, "item": done})
        final: dict[str, Any] = {"id": response_id, "status": "completed", "output": items}
        if self.include_usage:
            final["usage"] = {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120,
                              "input_tokens_details": {"cached_tokens": 64},
                              "output_tokens_details": {"reasoning_tokens": 5}}
        events.append({"type": "response.completed", "response": final})
        return events

    def _scenario(self, text: str, outputs: list[dict[str, Any]], body: dict[str, Any]) -> list[dict[str, Any]]:
        schema = self._schema_name(body)
        if schema == "ModelSummary" and "推理吃满额度" in str(body.get("instructions") or ""):
            # A reasoning model spending the whole max_output_tokens on thinking: cut off, no text.
            return self._text_stream("", incomplete=True)
        if schema == "ModelSummary" and ("一直交错" in str(body.get("instructions") or "") or (
                "先交错" in str(body.get("instructions") or "") and "没有通过校验" not in text)):
            return self._text_stream(json.dumps({"summery": "字段名写错"}, ensure_ascii=False))
        if schema == "ModelSummary":
            events = self._text_stream(json.dumps({"summary": "成交来自策略信号，价格与记录一致。"}, ensure_ascii=False),
                                       with_reasoning="带推理摘要" in str(body.get("instructions") or ""))
            if "带推理摘要" in str(body.get("instructions") or ""):
                # Raw reasoning text must never be surfaced, only the summary.
                events.insert(1, {"type": "response.reasoning_text.delta", "item_id": "rs-raw", "output_index": 0,
                                  "content_index": 0, "delta": "raw private reasoning"})
            return events
        if schema == "NewsCandidate":
            if "越权调用" in str(body.get("instructions") or ""):
                return self._tool_stream([("get_account_summary", {})])
            if not outputs:
                return self._tool_stream([("web_search", {"query": "螺纹钢 交易所 公告", "count": 5})])
            return self._text_stream(json.dumps({"excerpt": "", "url": "", "eventAt": ""}))
        if schema:
            return self._text_stream("{}")
        if "改成中文口语" in str(body.get("instructions") or ""):
            # The spoken summary of a voice turn: a fixed short line, or a failure on request.
            if "口语失败" in text:
                return []
            return self._text_stream("口语版：" + text[:12].replace("\n", " "))
        if "永不返回" in text:
            return self._text_stream("不应到达。")
        if "口语失败也要回答" in text:
            return self._text_stream("口语失败：已完成只读分析。" + "这段说明足够长，只有超过两百字的回答才会走口语概括，概括请求会失败。" * 8)
        if "服务端错误二次" in text and outputs:
            return []
        if "两个并行工具" in text:
            if not outputs:
                return self._tool_stream([("get_account_summary", {}), ("get_positions", {})])
            names = [output["payload"].get("tool") for output in outputs]
            return self._text_stream(f"并行工具已返回：{'+'.join(str(name) for name in names)}。")
        if "读取Trader数据" in text:
            if not outputs:
                return self._tool_stream([("get_account_summary", {})], with_reasoning=True)
            if not any(item.get("type") == "reasoning" and item.get("reasoning_text") for item in body.get("input") or []):
                return []  # a thinking-mode provider drops the stream (400) without its reasoning item
            payload = outputs[-1]["payload"]
            data = payload.get("data") or {}
            return self._text_stream(
                f"Trader只读工具返回：{data.get('accountName', '无账户')}。"
                if not payload.get("errorCode") else "Trader只读工具明确返回数据不可用。"
            )
        if "长回答" in text:
            # Long enough that the voice condenses it instead of reading it as written.
            return self._text_stream("结论：能力很多，先说三条。第一，看行情。第二，管策略。第三，下模拟单。\n\n" + "\n".join(f"- 第{i}项：这是一条足够长的说明文字，用来把回答撑过两百字的阈值。" for i in range(1, 9)))
        if "行情不可用短答" in text:
            if not outputs:
                return self._tool_stream([("get_futures_decision_context", {
                    "symbol": "BAD SYMBOL", "durationSeconds": 300, "lookbackBars": 40})])
            return self._text_stream("行情没好，但我还是写一大段新闻、风险和等待几十秒再问。")
        if "完整期货建议" in text:
            if not outputs:
                return self._tool_stream([("get_futures_decision_context", {
                    "symbol": "SHFE.au2612", "durationSeconds": 300, "lookbackBars": 40})])
            payload = outputs[-1]["payload"]
            user_facing = (payload.get("data") or {}).get("userFacing") or {}
            return self._text_stream(
                "结论：当前走势可分析，但先观望，不追涨也不抄底。\n"
                "为什么：价格方向尚未明确；账户和委托信息已核对。\n"
                f"风险：{user_facing.get('safetyBoundary', '实盘功能未开启')}。\n"
                "现在怎么做：等价格突破近期高点或低点后再重新判断。"
                if not payload.get("errorCode") else "完整期货建议因关键事实不可用而失败关闭。"
            )
        if "螺纹买两手吗" in text:
            # #929 → 意图重构第 3 步: a question turn withholds the card tool, yet a model calls it anyway. Refused
            # (tool_not_offered), it answers instead.
            refused = [output for output in outputs if (output["payload"] or {}).get("errorCode") == "tool_not_offered"]
            if refused:
                return self._text_stream("这是问句，我先不出卡。")
            return self._tool_stream([("propose_order", {
                "act": "open", "symbol": "SHFE.rb2610", "dir": "long", "vol": 2, "reason": "螺纹买两手"})])
        if "一句两单" in text:
            # propose_order legs on two products: refused (one order at a time), the model says the tool's line.
            if outputs:
                return self._text_stream("这句话里有几单，请一单一单说，每单各自出卡确认。")
            return self._tool_stream([("propose_order", {"legs": [
                {"act": "open", "symbol": "SHFE.rb2610", "dir": "long", "vol": 1},
                {"act": "open", "symbol": "SHFE.au2612", "dir": "short", "vol": 1}], "reason": "一句两单"})])
        if "交易AI平掉沪金三手" in text:
            # A close the planner refuses (au2612 holds 2 lots); the scenario always re-issues the call, so the second
            # identical refusal ends the turn on its line.
            return self._tool_stream([("propose_order", {
                "act": "close", "symbol": "SHFE.au2612", "dir": "long", "vol": 3, "reason": "沪金三手平仓"})])
        if "生成交易候选" in text:
            # One round (2026-09-19): the candidate tool runs the readiness gate itself; the model
            # explains a refusal with the Chinese labels the tool returns.
            if not outputs:
                return self._tool_stream([("propose_order", legacy_card_args("create_trade_proposal", {
                    "symbol": "SHFE.au2612", "side": "sell_open", "volume": 1, "priceIntent": "passive",
                    "reason": "仅生成候选，等待本人确认。"}))])
            last = outputs[-1]["payload"]
            if last.get("errorCode"):
                issues = ((last.get("data") or {}).get("userFacing") or {}).get("candidateIssues") or []
                return self._text_stream("交易候选没有创建：" + "；".join(issues) + "。" if issues
                                         else "交易候选创建失败，没有发送任何委托。")
            return self._text_stream("模拟交易方案已生成，正在等待你确认；尚未发送任何委托。")
        if "重复拒绝场景" in text:
            # A general turn (no route) whose model keeps calling the batch close after its refusal —
            # the AI audit 2026-10-13 shape: three refusals, then a closing round that blamed the data.
            if body.get("tool_choice") == "none":
                return self._text_stream("系统这次没能取到持仓数据。")
            return self._tool_stream([("propose_order", legacy_card_args("create_close_batch_proposal", {"reason": "用户要求清仓"}))])
        if "全部平仓休市" in text:
            # The close_all route over a closed market: every position is skipped, the card tool
            # refuses with the skipped contracts' own reasons and the Runtime relays that line —
            # the scenario always re-issues the call, so a broken relay would loop the route's
            # three rounds and end “未完成。” (#721).
            return self._tool_stream([("propose_order", legacy_card_args("create_close_batch_proposal", {"reason": "休市也要全部平仓，等待确认。"}))])
        if "收尾轮吐标记" in text:
            # A general turn whose model keeps calling a read tool until the rounds run out; the
            # closing round (tool_choice="none") answers with DeepSeek's raw DSML markup instead of
            # prose — what the Runtime must never publish as the user's answer (#721).
            if body.get("tool_choice") == "none":
                return self._text_stream(
                    '<｜｜DSML｜｜ calls>\n<｜｜DSML｜｜ invoke name="propose_order">'
                    '>\n{"reason": "全部平仓"}</｜｜DSML｜｜>')
            return self._tool_stream([("get_market_snapshot", {"symbol": "SHFE.au2612"})])
        if "收尾轮正常收尾" in text:
            # The same exhausted turn, but the closing round answers in ordinary prose: published as-is.
            if body.get("tool_choice") == "none":
                return self._text_stream("行情读了很多轮，还没凑齐结论，稍后再问一次。")
            return self._tool_stream([("get_market_snapshot", {"symbol": "SHFE.au2612"})])
        if "来一手豆粕" in text:
            # 豆粕 is not in the fake catalog, so every search comes back empty — what the real model
            # looped on. The Runtime ends the routed turn honestly after the first empty result (第三轮).
            return self._tool_stream([("search_instruments", {"search": "豆粕"})])
        # 回答卡片化 (2026-10-14).
        if "回答卡场景" in text:
            if not outputs:
                return self._tool_stream([("get_market_snapshot", {"symbol": "SHFE.au2612"}),
                                          ("get_market_analysis", {"symbol": "SHFE.au2612", "durationSeconds": 300})])
            if len(outputs) == 2:
                return self._tool_stream([("answer_card", {
                    "title": "沪金短线", "conclusion": "短线偏强，回踩不破可以继续看多。",
                    "figures": ["price", "madeUp"],
                    "compare": {"left": {"title": "多", "lines": ["站在{price}上方", "编的 12345"]},
                                "right": {"title": "空", "lines": ["量能一般"]}},
                    "reasons": ["{maOrder=up}", "{price}，回踩有支撑"], "risk": "跌破均线就失效", "symbol": "SHFE.au2612"})])
            return self._text_stream("不该再请求这一轮。")
        if "结论无出处场景" in text:
            # The card's own conclusion states a number nothing read: the model gets the round to correct it.
            if not outputs:
                return self._tool_stream([("get_market_snapshot", {"symbol": "SHFE.au2612"})])
            if len(outputs) == 1:
                return self._tool_stream([("answer_card", {"title": "沪金", "conclusion": "沪金涨到 99999.9，偏强。"})])
            return self._text_stream("沪金短线偏强。回踩不破可以继续看多。最新价 823.5，涨 0.68%，五日均线在上方。\n\n| 项 | 值 |\n|---|---|\n| 价 | 823.5 |")
        if "定性核对场景" in text:
            # 定性判断核对 (#886 跟进): the model says the opposite of the volume ratio it read — what the 10-14 19:05
            # card did.
            if not outputs:
                return self._tool_stream([("get_market_snapshot", {"symbol": "SHFE.au2612"}),
                                          ("get_market_analysis", {"symbol": "SHFE.au2612", "durationSeconds": 300})])
            analysis = next(data for output in outputs if "volumeRatio" in (data := (output.get("payload") or {}).get("data") or {}))
            right, wrong = ("放量", "缩量") if analysis["volumeRatio"] > 1 else ("缩量", "放量")
            keys = {"放量": "up", "缩量": "down"}
            if len(outputs) == 2:  # the reversed reading: the card goes back to the model
                return self._tool_stream([("answer_card", {"title": "沪金", "conclusion": f"沪金{{volumeState={keys[wrong]}}}"})])
            if len(outputs) == 3:
                return self._tool_stream([("answer_card", {
                    "title": "沪金", "conclusion": f"沪金{{volumeState={keys[right]}}}",
                    "reasons": [f"{{volumeState={keys[wrong]}}}，资金离场", "资金回流"],
                    "risk": f"若继续{wrong}，走势会转弱", "symbol": "SHFE.au2612"})])
            return self._text_stream("不该再请求这一轮。")
        if "同轮重读场景" in text:
            # The same read twice in one turn: the second gets a pointer to the first result, not the result again.
            if len(outputs) < 2:
                return self._tool_stream([("get_market_snapshot", {"symbol": "SHFE.au2612"})])
            return self._text_stream("读到了。")
        if "收尾出卡场景" in text:
            if not outputs:
                return self._tool_stream([("get_market_snapshot", {"symbol": "SHFE.au2612"})])
            if len(outputs) == 1:
                return self._tool_stream([("answer_card", {"title": "沪金", "conclusion": "沪金短线偏强。",
                                                           "reasons": ["资金回流"]})])
            return self._text_stream("不该再请求这一轮。")
        if "两个合约报价" in text:
            if not outputs:
                return self._tool_stream([("get_market_snapshot", {"symbol": "SHFE.au2612"}),
                                          ("get_market_snapshot", {"symbol": "CFFEX.IF2609"})])
            return self._text_stream("两个都读到了。")
        if "持仓卡场景" in text:
            if not outputs:
                return self._tool_stream([("get_positions", {})])
            return self._text_stream("你有三个持仓，整体浮盈。沪金 2 手浮盈 2670 元。白银 1 手浮盈 750 元。第三个快到交割月了。")
        if "纯文字报数" in text:
            return self._text_stream("螺纹大概在 3100 附近，涨了 0.5%。")
        if "这个怎么样" in text:
            # A bare reference the market_query route cannot resolve either: one search with no result
            # and the Runtime ends the turn honestly (第二期: market_query cuts off at the first miss).
            return self._tool_stream([("search_instruments", {"search": "这个"})])
        if "棉花最近咋样" in text:
            # 棉花 is not in the fake catalog. The general route may rephrase once (第二期): the first
            # empty search is tolerated, the second consecutive one ends the turn honestly. The query
            # word changes on the retry so the assertion can tell which round closed the turn.
            if len(outputs) >= 2:
                return self._text_stream("不该到这里。")
            query = "棉花" if not outputs else "棉花期货"
            return self._tool_stream([("search_instruments", {"search": query})])
        if "一直查工具不回答" in text:
            # Never answers, tool after tool: the round cap hits, and the forced tool_choice="none"
            # round (第二期) must produce the one-sentence close instead of “未完成。”. The tools stay
            # declared (the history's function items need them), so "none" is the discriminator.
            if body.get("tool_choice") == "none":
                return self._text_stream("查了几轮行情但没凑齐依据，这次先说不了结论，缺最新的豆粕报价。")
            return self._tool_stream([("get_positions", {})])
        if "把界面切到图表那一页" in text:
            # Interface wording the fixed table and the model both miss: the UI round only reads the
            # capabilities (no write), so the Runtime re-runs it as a general turn, which asks (#748).
            if {tool.get("name") for tool in body.get("tools") or []} <= {"app_call", "app_capabilities", "highlight_ui"}:
                return self._tool_stream([("app_capabilities", {})])
            return self._text_stream("你说的图表那一页是主图还是复盘页？")
        if "读使用说明目录" in text:
            # 2026-10-14 瘦身: a manual read without a topic, then one topic.
            if not outputs:
                return self._tool_stream([("app_call", {"requestJson": json.dumps({"operation": "GET /api/app/help"})})])
            if len(outputs) == 1:
                return self._tool_stream([("app_call", {"requestJson": json.dumps(
                    {"operation": "GET /api/app/help", "query": {"topic": "agent"}})})])
            return self._text_stream("说明已读。")
        if "搜使用说明" in text:
            if not outputs:
                return self._tool_stream([("app_call", {"requestJson": json.dumps(
                    {"operation": "GET /api/app/help", "query": {"search": "手续费"}})})])
            return self._text_stream("搜到了。")
        if "读策略计划定义" in text:
            if not outputs:
                return self._tool_stream([("app_capabilities", {"operation": "POST /api/strategy-plans"})])
            return self._text_stream("定义已读。")
        if "挂上那个技能" in text:
            # 按技能延迟挂载 (2026-10-14): the model mounts a skill whose tools this turn does not carry yet.
            if not outputs:
                return self._tool_stream([("activate_skill", {"skillId": "price-alerts"})])
            return self._text_stream("提醒技能已挂上。")
        if "可取消回测" in text:
            if not outputs:
                return self._tool_stream([("run_reproducible_backtest", {})])
            return self._text_stream("回测已处理。")
        if "读回场景" in text:
            # #872: protection writes in sequence, each re-reading the account (accountAfter), then an
            # answer that states the remaining position — the one the turn's last read may not back.
            plan: list[tuple[str, dict[str, Any]]] = [("cancel_simulation_stop_loss", {"stopId": "stop-take-a"})]
            if "〔两写〕" in text:
                plan.append(("cancel_simulation_stop_loss", {"stopId": "stop-take-b"}))
            if "〔报价〕" in text:
                plan.insert(0, ("get_market_snapshot", {"symbol": "SHFE.rb2610"}))
            if "〔补读" in text:
                plan.append(("get_positions", {}))
            if "〔补读〕" in text:
                plan.append(("get_account_activity", {}))
            if "〔行情坏〕" in text:
                plan.append(("get_futures_decision_context", {
                    "symbol": "BAD SYMBOL", "durationSeconds": 300, "lookbackBars": 40}))
            if len(outputs) < len(plan):
                return self._tool_stream([plan[len(outputs)]])
            return self._text_stream("止盈已撤，当前持仓还是 2 手。", chunks=["止盈已撤，", "当前持仓还是 2 手。"])
        if "现在急停" in text:
            if not outputs:
                return self._tool_stream([("stop_simulation_execution", {"emergency": True})])
            return self._text_stream("已发出急停。")
        if "联网搜索后回答" in text:
            if not outputs:
                return self._tool_stream([("web_search", {"query": "螺纹钢 最新消息", "count": 3})])
            return self._text_stream("已联网核验后的回答。")
        if "带推理摘要" in text:
            return self._text_stream("推理摘要之后的回答。", with_reasoning=True)
        if "大量流式" in text:
            return self._text_stream("大量流式输出已合并完成。", chunks=[str(index % 10) for index in range(1000)] + ["大量流式输出已合并完成。"])
        if "超长输出" in text:
            return self._text_stream("这段回答被模型输出上限截断", incomplete=True)
        if "第二轮引用" in text:
            history = [item for item in body.get("input") or [] if isinstance(item, dict) and item.get("role") == "assistant"]
            return self._text_stream(f"上一轮我说过：{history[-1]['content'] if history else '没有历史'}")
        if "上一轮答案未通过运行时新鲜度门" in text:
            return self._text_stream("已通过搜索重做。")
        if "只读操作手册后断言行情" in text:
            # Reading the manual is not a live fact: the answer still asserts prices from memory.
            if not outputs:
                return self._tool_stream([("app_capabilities", {})])
            return self._text_stream("操作手册读完了。最新价 3126 元，昨天的消息说要涨。")
        if "行情读取失败后断言行情" in text:
            if not outputs:
                return self._tool_stream([("get_market_snapshot", {"symbol": "BAD SYMBOL"})])
            return self._text_stream("行情工具失败了。最新价 3126 元，昨天的消息说要涨。")
        if "思考外泄" in text:
            # 2026-10-13 夜盘：模型把自言自语写进正文（思考通道本身是分开的）。
            if "把思考过程写进了正文" in text:  # the Runtime's rewrite round
                if "两次" in text:
                    return self._text_stream("Hmm, 用户此前说过要保留止损，我再想想。")
                return self._text_stream("3050 那张螺纹多单已撤销，没成交。")
            if "think块" in text:
                return self._text_stream("<think>先核对委托</think>3050 那张单已撤销。")
            leaky = ("3050 那张螺纹多单已撤销，未成交。止损 3060 还在。\n\nWait — 用户此前设的止损是 3076，"
                     "实际上止损情况我不确定。不要赘述。保持简短。\n\n重新组织：3050 那张单已撤销。")
            return self._text_stream(leaky, chunks=[leaky[:30], leaky[30:60], leaky[60:]])
        if "强制遗漏首次搜索，只聊天气" in text:
            return self._text_stream("今天天气不错，适合出门走走。")
        if "强制遗漏首次搜索" in text:
            return self._text_stream("这条未搜索答案不应显示。最新价 3126 元，昨天的消息说要涨。")
        return self._text_stream("已完成只读分析。")
