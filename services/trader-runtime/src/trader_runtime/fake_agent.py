from __future__ import annotations

import asyncio
import re
from copy import deepcopy
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import uuid4

from . import __version__
from .backtests import BacktestService
from .broker import EventBroker
from .data_providers import (
    FakeReadOnlyDataProvider,
    ReadOnlyDataProvider,
    fake_account_summary,
    fake_market_snapshot,
)
from .message_context import decode_instruction
from .proposals import ProposalService
from .runtime_presentation import describe_runtime
from .wiring.accounts import session_account_status_payload

_CHINESE_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3,
                   "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CHINESE_UNITS = {"十": 10, "百": 100, "千": 1000, "万": 10000}


def _integer_value(token: str) -> int | None:
    if token.isdigit():
        return int(token)
    total = section = number = 0
    for character in token:
        if character.isdigit():
            number = number * 10 + int(character)
        elif character in _CHINESE_DIGITS:
            number = _CHINESE_DIGITS[character]
        elif character in _CHINESE_UNITS:
            unit = _CHINESE_UNITS[character]
            if unit == 10000:
                total += (section + number) * unit
                section = number = 0
            else:
                section += (number or 1) * unit
                number = 0
        else:
            return None
    return total + section + number


def _amount_after(text: str, prefix: str) -> str | None:
    match = re.search(
        rf"{prefix}([^，。；,;]{{0,20}}?)\s*元",
        text,
    )
    if match is None:
        return None
    token = re.search(
        r"(?<![0-9.])([0-9]+(?:\.[0-9]{1,2})?|[零〇一二两三四五六七八九十百千万]+)$",
        match.group(1).strip(),
    )
    if token is None:
        return None
    raw_value = token.group(1)
    if re.fullmatch(r"[0-9]+(?:\.[0-9]{1,2})?", raw_value):
        try:
            return raw_value if Decimal(raw_value) > 0 else None
        except InvalidOperation:
            return None
    value = _integer_value(raw_value)
    return str(value) if value is not None and value > 0 else None


def _cutoff_after(text: str) -> str | None:
    clock = re.search(r"(?:以后|之后)不再开仓", text)
    if clock is None:
        return None
    prefix = text[:clock.start()]
    colon = re.search(r"([01]?\d|2[0-3]):([0-5]\d)\s*$", prefix)
    if colon:
        return f"{int(colon.group(1)):02d}:{int(colon.group(2)):02d}"
    match = re.search(
        r"(晚上|下午|中午|夜里|夜间)?\s*([0-9零〇一二两三四五六七八九十]+)\s*点"
        r"(?:\s*([0-9零〇一二两三四五六七八九十]+)\s*分?)?\s*$",
        prefix,
    )
    if match is None:
        return None
    hour, minute = _integer_value(match.group(2)), _integer_value(match.group(3) or "零")
    if hour is None or minute is None or hour > 23 or minute > 59:
        return None
    if match.group(1) in {"晚上", "下午", "夜里", "夜间", "中午"} and 1 <= hour < 12:
        hour += 12
    return f"{hour:02d}:{minute:02d}"


def parse_fake_risk_rule(text: str) -> dict[str, Any] | None:
    symbols: list[str] = []
    if any(word in text.upper() for word in ("黄金", "沪金", "AU2612")):
        symbols.append("AU2612")
    if any(word in text.upper() for word in ("螺纹", "RB2610")):
        symbols.append("RB2610")
    per_trade = _amount_after(text, r"每笔")
    daily = _amount_after(text, r"(?:每天|每日)")
    cutoff = _cutoff_after(text)
    if not symbols or per_trade is None or daily is None or cutoff is None:
        return None
    return {
        "allowed_symbols": symbols, "max_loss_per_trade": per_trade,
        "max_daily_loss": daily, "no_new_positions_after": cutoff,
    }


def account_summary(turn_id: str) -> dict[str, Any]:
    return fake_account_summary(turn_id)


def market_snapshot(turn_id: str) -> dict[str, Any]:
    return fake_market_snapshot(turn_id)


def strategy_explanation(turn_id: str) -> dict[str, Any]:
    return {
        "turnId": turn_id,
        "strategyId": "rb-trend-v32",
        "name": "螺纹钢趋势策略",
        "symbol": "RB2610",
        "version": "v3.2",
        "status": "waiting",
        "summary": "价格方向已满足，但成交量过滤条件还差 7.4%，因此保持观望。",
        "conditions": [
            {"label": "价格站上短期均线", "status": "passed", "detail": "已满足"},
            {"label": "中期趋势方向一致", "status": "passed", "detail": "已满足"},
            {"label": "成交量达到策略阈值", "status": "waiting", "detail": "还差 7.4%"},
        ],
    }


class FakeAgentAdapter:
    adapter_id = "fake-agent"

    def __init__(
        self, broker: EventBroker, proposals: ProposalService,
        data_provider: ReadOnlyDataProvider | None = None,
        backtests: BacktestService | None = None,
        status_overrides: dict[str, Any] | None = None,
        backtest_progress_delay: float = 0.65,
    ) -> None:
        self.broker = broker
        self.proposals = proposals
        self.data_provider = data_provider or FakeReadOnlyDataProvider()
        self.backtests = backtests
        self.status_overrides = status_overrides or {}
        self.backtest_progress_delay = max(0.0, backtest_progress_delay)
        self.tasks: dict[str, dict[str, Any]] = {}

    def runtime_status(self, mode: str) -> dict[str, Any]:
        return describe_runtime({"mode": mode, "adapter": self.adapter_id,
                "runtimeConnected": True, "agentConnected": True, "connected": True,
                "version": __version__, **self.data_provider.runtime_status(),
                **self.status_overrides})

    async def _message(self, session_id: str, role: str, text: str, *, origin: str | None = None,
                       focus: dict[str, str] | None = None) -> str:
        turn_id = str(uuid4())
        payload: dict[str, Any] = {"turnId": turn_id, "role": role, "text": text}
        if role == "assistant":
            payload["agentId"] = "native"
        if origin:
            payload["origin"] = origin
        if focus:
            payload["focus"] = {"kind": focus["kind"], "label": focus["label"]}
        await self.broker.publish(
            session_id, kind="message", name="message.completed", payload=payload
        )
        return turn_id

    async def bootstrap(self, session_id: str) -> None:
        session = self.broker.get_session(session_id)
        if session is None:
            return
        await self.broker.publish(
            session_id,
            kind="result",
            name="session.ready",
            payload={"mode": session.mode, "agentId": "native"},
        )
        # 4.4b: the account block rides on every runtime.status, fake adapter included, so
        # the client's switcher and `X-AI-Trader-Account` header have one source in every mode.
        account = session_account_status_payload(
            self.proposals.store, account_id=str(session.trading_account_id or ""),
            user_id=session.user_id,
        ) if session.trading_account_id else None
        await self.broker.publish(
            session_id,
            kind="result",
            name="runtime.status",
            payload={**self.runtime_status(session.mode), **({"account": account} if account else {})},
        )
        data_status = self.data_provider.runtime_status()["accountStatus"]
        if self.data_provider.mode == "fake":
            account_message = "当前使用内置假数据，可直接体验账户、行情、回测和风控流程。"
        elif data_status == "normal":
            account_message = "TqKq 只读账户快照已就绪；以下内容仅展示 Runtime 校验后的数据。"
        elif data_status == "stale":
            account_message = "TqKq 只读账户快照已经陈旧，不得据此确认交易。"
        else:
            account_message = "TqKq 只读账户数据当前不可用；页面不会编造账户盈亏结论。"
        turn_id = await self._message(session_id, "assistant", account_message)
        await self.broker.publish(
            session_id,
            kind="artifact",
            name="card.account",
            payload=self.data_provider.account_summary(turn_id),
        )
        if self.data_provider.mode != "fake":
            return
        await self._message(session_id, "user", "为什么螺纹钢今天没有开仓？")
        strategy_turn = await self._message(
            session_id,
            "assistant",
            "结论：继续观望。\n"
            "为什么：价格方向已满足，但成交量过滤条件还差 7.4%。\n"
            "风险：现在开仓会绕过策略条件。\n"
            "现在怎么做：等成交量达标后再评估。",
        )
        await self.broker.publish(
            session_id,
            kind="artifact",
            name="card.strategy",
            payload=strategy_explanation(strategy_turn),
        )

    async def handle_message(self, session_id: str, text: str, run_id: str, *, user_id: str | None = None, model_slug: str | None = None) -> None:
        decoded = decode_instruction(text)
        await self._message(session_id, "user", decoded.user_text, origin=decoded.origin, focus=decoded.focus)
        normalized = decoded.user_text
        owner = user_id or self._session_user(session_id)
        if owner and await self._routine(session_id, owner, normalized, decoded.routine_id):
            return
        if any(word in normalized for word in ("每笔", "每天亏", "停止开新仓", "不再开仓")):
            await self._risk_rule(session_id, normalized)
        elif any(word in normalized for word in ("回测", "阈值", "修改策略")):
            await self._backtest(session_id, run_id)
        elif any(word in normalized for word in ("交易", "买入", "下单", "黄金方案")):
            await self._trade(session_id)
        elif any(word in normalized for word in ("咋样", "怎么看")):
            await self._answer(session_id, normalized)
        elif "行情" in normalized:
            await self._market(session_id)
        elif any(word in normalized for word in ("账户", "盈亏", "资金", "今天")):
            await self._account(session_id)
        elif any(word in normalized for word in ("风险", "风控")):
            await self._risk(session_id)
        else:
            await self._message(
                session_id,
                "assistant",
                "演示 Agent 已收到请求。当前可演示账户、行情、策略回测和交易候选流程。",
            )

    def _session_user(self, session_id: str) -> str | None:
        session = self.broker.get_session(session_id)
        return getattr(session, "user_id", None) if session is not None else None

    async def _routine(self, session_id: str, user_id: str, text: str, routine_id: str | None = None) -> bool:
        """我的流程 in the demo: "以后我说「X」就……" drafts a real 流程卡 (保存 works for real), and the
        routine's name shows a finished run with one step waiting for 确认 — no model, no order."""
        from .message_context import routine_define_request
        from .routines import GONE_LINE, refusal_reason, routine_card, run_card, run_summary
        from .voice_intents import routine_request
        store = self.proposals.store
        names = store.routine_names(user_id)
        # A picked run (#522) runs its routine by id; one deleted meanwhile runs nothing.
        name = None if routine_id else (routine_request(text, names) if names else None)
        routine = store.get_routine(user_id, routine_id) if routine_id else (store.get_routine(user_id, names[name]) if name else None)
        if routine_id and routine is None:
            await self._message(session_id, "assistant", GONE_LINE)
            return True
        if routine is not None:
            store.mark_routine_run(user_id, routine["id"])
            turn_id = str(uuid4())
            states = [{"status": "confirm" if step["kind"] == "card" else "done",
                       "result": "出了 1 张开仓卡，等你确认" if step["kind"] == "card" else None}
                      for step in routine["steps"]]
            now = datetime.now(UTC).isoformat()
            await self.broker.publish(session_id, kind="artifact", name="card.routine_run", payload=run_card(
                turn_id=turn_id, routine=routine, states=states, status="completed", started_at=now, elapsed=3.2,
                finished_at=now))
            symbols = tuple(getattr(self.data_provider, "symbols", ()))
            if symbols and any(step["kind"] == "card" for step in routine["steps"]):
                try:  # the demo's card step: a real waiting card (a gateway refusing fixture quotes just skips it)
                    proposal = self.proposals.create_trade(session_id, turn_id, symbol=symbols[0], side="buy_open", volume=1,
                                                           price_intent="passive", reason=f"流程「{routine['name']}」出卡", origin="ai")
                    await self.broker.publish(session_id, kind="approval", name="proposal.trade", payload=proposal)
                except Exception:
                    pass
            await self.broker.publish(session_id, kind="message", name="message.completed", payload={
                "turnId": turn_id, "role": "assistant", "agentId": "native", "text": run_summary(routine, states)})
            return True
        if not routine_define_request(text):
            return False
        quoted = re.search(r"[「『“\"]([^」』”\"]{1,12})[」』”\"]", text)
        body = re.split(r"就(?:帮我|给我)?[：:]?", text, maxsplit=1)[-1]
        words = [part.strip() for part in re.split(r"[；;，,、。]", body) if part.strip()][:10]
        kinds = (("画", "draw"), ("回测", "backtest"), ("保证金", "margin"), ("手数", "margin"), ("卡", "card"),
                 ("开仓", "card"), ("平仓", "card"), ("到价", "alert"), ("提醒", "alert"), ("异动", "watch"),
                 ("盯", "watch"), ("复盘", "review"))
        steps = [{"kind": next((kind for word, kind in kinds if word in part), "read"), "text": part[:60]} for part in words]
        if not steps:
            return False
        refusal = refusal_reason("", steps, text)
        turn_id = await self._message(session_id, "assistant", refusal or f"拆成了 {len(steps)} 步，点“保存”就存成流程。")
        name = (quoted.group(1) if quoted else "我的流程")[:12]
        draft = store.insert_routine_draft(user_id, routine_id=names.get(name), name=name,
                                           steps=steps, session_id=session_id, turn_id=turn_id, refusal=refusal)
        await self.broker.publish(session_id, kind="artifact", name="card.routine", payload=routine_card(draft))
        return True

    async def _account(self, session_id: str) -> None:
        await asyncio.sleep(0.12)
        data_status = self.data_provider.runtime_status()["accountStatus"]
        if self.data_provider.mode == "fake":
            message = "今天盈利主要来自黄金持仓，风险预算使用 31%，暂时没有需要立即处理的事项。"
        elif data_status == "normal":
            message = "已读取 Runtime 校验后的 TqKq 只读账户快照。"
        elif data_status == "stale":
            message = "TqKq 只读账户快照已经陈旧，不据此生成财务结论。"
        else:
            message = "TqKq 只读账户数据当前不可用，无法给出账户盈亏结论。"
        turn_id = await self._message(session_id, "assistant", message)
        # A reply's account read carries its cardId, as in the native turn, so the conversation shows it
        # (the session's opening snapshot, without one, stays out).
        from .answer_cards import TurnCards
        await self.broker.publish(
            session_id,
            kind="artifact",
            name="card.account",
            payload=TurnCards(turn_id).account_card(self.data_provider.account_summary(turn_id)),
        )

    async def _market(self, session_id: str) -> None:
        await asyncio.sleep(0.12)
        data_status = self.data_provider.runtime_status()["marketDataStatus"]
        if self.data_provider.mode == "fake":
            message = "沪金当前保持短线上行，我只展开了本次判断需要的价格、涨跌和方向。"
        elif data_status == "normal":
            message = "已读取 Runtime 校验后的 TqKq 只读行情快照。"
        elif data_status == "stale":
            message = "TqKq 只读行情快照已经陈旧，不据此生成方向结论。"
        else:
            await self._message(
                session_id, "assistant", "TqKq 只读行情当前不可用，无法给出价格或方向结论。"
            )
            return
        turn_id = await self._message(session_id, "assistant", message)
        await self.broker.publish(
            session_id,
            kind="artifact",
            name="card.market",
            payload=self.data_provider.market_snapshot(turn_id, "AU2612"),
        )

    async def _answer(self, session_id: str, text: str) -> None:
        """回答卡片化 (2026-10-14) in the demo: the analysis read as a 走势 data card and the answer as a
        回答卡, built through the same TurnCards path as the native agent (figures are field keys into the read)."""
        import json

        from .answer_cards import TurnCards
        from .trader_tools import _market_analysis
        await asyncio.sleep(0.12)
        symbol = "SHFE.au2612"
        try:
            analysis = _market_analysis(self.data_provider.market_workbench(symbol, 300), lookback=60)
        except (KeyError, RuntimeError, TypeError, ValueError):
            await self._message(session_id, "assistant", "行情暂时读不到，先不判断。")
            return
        trend = {"up": "短线上行", "down": "短线回落"}.get(str(analysis["trend"]), "横盘震荡")
        turn_id = await self._message(session_id, "assistant", f"沪金{trend}，详细数字见卡片。")
        cards = TurnCards(turn_id)
        arguments = {"symbol": symbol, "durationSeconds": 300}
        cards.record_read("get_market_analysis", arguments, json.dumps({"data": analysis}, ensure_ascii=False))  # a tool result's shape
        data_card = cards.data_card("get_market_analysis", arguments, analysis, account_word="演示")
        if data_card is not None:
            cards.note_card(data_card["cardId"])  # folded into the answer card's 依据, as in the native turn
            await self.broker.publish(session_id, kind="artifact", name="card.data", payload=data_card)
        # The new format's own example (结构化第 2 步): readings only as keys, no number or reading word of its own —
        # the old “沪金 5 分钟” / “最近 60 根” wording is refused since #963 and the demo showed no answer card.
        card, _dropped, _error = cards.answer_card({
            "title": "沪金短线", "conclusion": f"沪金{{trend={analysis['trend']}}}，演示行情",
            "figures": ["period", "ma5", "ma20"],  # field keys: the Runtime fills them from the read
            "reasons": [f"短均价{'高于' if analysis['sma5'] > analysis['sma20'] else '低于'}长均价"],
            "risk": "演示数据，不代表真实行情", "symbol": symbol})
        if card is not None:
            await self.broker.publish(session_id, kind="artifact", name="card.answer", payload=card)

    async def _risk(self, session_id: str) -> None:
        await asyncio.sleep(0.1)
        turn_id = await self._message(
            session_id,
            "assistant",
            "确定性风控仍在 Trader Runtime 中生效，Agent 无权绕过。",
        )
        await self.broker.publish(
            session_id,
            kind="warning",
            name="warning",
            payload={
                "turnId": turn_id,
                "title": "实盘能力未开放",
                "message": "当前只允许假数据、回测和候选记录；不会提交订单。",
                "severity": "warning",
                "deterministic": True,
            },
        )

    async def _risk_rule(self, session_id: str, text: str) -> None:
        await asyncio.sleep(0.1)
        parsed = parse_fake_risk_rule(text)
        if parsed is None:
            await self._message(
                session_id, "assistant",
                "我没有完整识别合约、单笔上限、每日上限和停止开仓时间，因此没有创建候选。请按“黄金每笔最多亏五百元，每天亏到两千元，下午三点以后不再开仓”重述。",
            )
            return
        turn_id = await self._message(
            session_id,
            "assistant",
            "我已把你的要求整理为确定性风险规则。确认后由 Trader Runtime 执行，Agent 无权绕过。",
        )
        proposal = self.proposals.create_risk_rule(
            session_id, turn_id, **parsed,
        )
        await self.broker.publish(
            session_id, kind="approval", name="proposal.risk_rule", payload=proposal,
        )
        await self.broker.publish(
            session_id, kind="approval", name="risk_rule.approval.required",
            payload={
                "proposalId": proposal["proposalId"], "approvalId": proposal["approvalId"],
                "proposalVersion": proposal["version"], "proposalType": "risk_rule",
            },
        )

    async def shutdown(self) -> None:
        self.tasks.clear()

    async def cancel_run(self, run_id: str) -> None:
        if self.backtests is None:
            return
        user_id = self.backtests.store.run_user_id(run_id)
        self.backtests.coordinator.cancel(run_id, user_id=user_id)

    async def release_session(self, session_id: str) -> None:
        del session_id

    async def _backtest(self, session_id: str, task_id: str) -> None:
        await asyncio.sleep(min(0.12, self.backtest_progress_delay))
        turn_id = await self._message(
            session_id,
            "assistant",
            "我会保留旧版本，从不可变策略与数据快照运行可复现的合成回测。",
        )
        task = {
            "turnId": turn_id,
            "taskId": task_id,
            "title": "AI 工作任务",
            "subtitle": "修改策略并验证结果",
            "progress": 8,
            "status": "running",
            "steps": [
                {"id": "read", "label": "读取策略与最近日志", "status": "active", "detail": "处理中"},
                {"id": "variant", "label": "创建成交量阈值 90% 的新版本", "status": "waiting", "detail": "等待"},
                {"id": "backtest", "label": "运行最近三年回测", "status": "waiting", "detail": "等待"},
                {"id": "compare", "label": "比较收益、回撤与交易质量", "status": "waiting", "detail": "等待"},
            ],
        }
        self.tasks[task_id] = deepcopy(task)
        await self.broker.publish(
            session_id, kind="progress", name="task.started", payload=deepcopy(task)
        )
        try:
            for index, progress in enumerate((25, 50)):
                await asyncio.sleep(self.backtest_progress_delay)
                task["steps"][index]["status"] = "done"
                task["steps"][index]["detail"] = "完成"
                task["steps"][index + 1]["status"] = "active"
                task["steps"][index + 1]["detail"] = "处理中"
                task["progress"] = progress
                self.tasks[task_id] = deepcopy(task)
                await self.broker.publish(
                    session_id, kind="progress", name="task.progress", payload=deepcopy(task)
                )

            await asyncio.sleep(self.backtest_progress_delay)
            task["progress"] = 78
            task["steps"][2]["detail"] = "正在运行并验证回测"
            self.tasks[task_id] = deepcopy(task)
            await self.broker.publish(
                session_id, kind="progress", name="task.progress", payload=deepcopy(task)
            )

            session = self.broker.get_session(session_id)
            if session is None:
                raise KeyError(session_id)
            if self.backtests is None:
                raise RuntimeError("backtest_service_unavailable")
            comparison = await asyncio.to_thread(
                self.backtests.run,
                session_id=session_id,
                user_id=session.user_id,
                request_id=task_id,
            )
            task["steps"][2].update({"status": "done", "detail": "完成"})
            task["steps"][3].update({"status": "active", "detail": "正在生成结果卡片"})
            task["progress"] = 90
            self.tasks[task_id] = deepcopy(task)
            await self.broker.publish(
                session_id, kind="progress", name="task.progress", payload=deepcopy(task)
            )
            result_turn = await self._message(
                session_id,
                "assistant",
                "可复现合成回测已完成。结果包含策略、数据和参数摘要，仅供研究。",
            )
            await self.broker.publish(
                session_id,
                kind="chart",
                name="card.backtest",
                payload={**comparison, "turnId": result_turn, "taskId": task_id},
            )
            task["steps"][3].update({"status": "done", "detail": "完成"})
            task.update({"progress": 100, "status": "completed"})
            self.tasks[task_id] = deepcopy(task)
            await self.broker.publish(
                session_id, kind="progress", name="task.completed", payload=deepcopy(task)
            )
        except asyncio.CancelledError:
            task["status"] = "cancelled"
            for step in task["steps"]:
                if step["status"] == "active":
                    step.update({"status": "failed", "detail": "已停止"})
            self.tasks[task_id] = deepcopy(task)
            raise
        except Exception:
            task["status"] = "failed"
            for step in task["steps"]:
                if step["status"] == "active":
                    step.update({"status": "failed", "detail": "失败"})
            self.tasks[task_id] = deepcopy(task)
            await self.broker.publish(
                session_id, kind="progress", name="task.completed", payload=deepcopy(task)
            )
            raise

    async def _trade(self, session_id: str) -> None:
        await asyncio.sleep(0.12)
        turn_id = await self._message(
            session_id,
            "assistant",
            "我会按当前报价生成 1 手模拟限价开仓候选；确认前仍由 Runtime 复核。",
        )
        symbols = tuple(getattr(self.data_provider, "symbols", ()))
        if not symbols:
            await self._message(
                session_id, "assistant", "当前没有可用合约，未创建交易候选。"
            )
            return
        proposal = self.proposals.create_trade(
            session_id,
            turn_id,
            symbol=symbols[0],
            side="buy_open",
            volume=1,
            price_intent="passive",
            reason="用户请求生成模拟开仓候选。",
            origin="ai",
        )
        await self.broker.publish(
            session_id, kind="approval", name="proposal.trade", payload=proposal
        )
