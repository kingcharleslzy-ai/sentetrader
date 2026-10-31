from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from .broker import EventBroker, utc_now
from .data_providers import provider_read_snapshot
from .indicators import cross_signal, ema
from .instruments import account_currency, foreign_product_of, is_foreign_paper
from .risk import (
    InstrumentSpecProvider,
    RiskPolicyProvider,
    calculate_trade_risk,
    ceil_to_tick,
    decimal_value,
    floor_to_tick,
    instrument_spec,
)
from .simulation_orders import (
    CLOSE_TODAY_EXCHANGES,
    SimulationOrderService,
    closable_buckets,
    close_allocation,
    close_batch_execution,
    close_yesterday_first_split,
)
from .store import RuntimeStore, utc_after
from .strategy_intents import build_strategy_intent
from .trading_costs import FeeSettings, effective_fee_schedule, trade_fee

if TYPE_CHECKING:
    from .wiring.accounts import AccountRegistry, AccountServices

# `close_batch_orders` reads order facts through the account's own order service;
# the sentinel keeps an explicit `None` (an account without a gateway) distinct
# from "not resolved, use the constructor's".
_UNSET = object()

# 下单前的“钱”口径：保证金与手续费都是事前估算，成交回报才是事实。
# 手续费默认按交易所标准（openctp 汇总），自定义了费率按自设；保证金读行情源。
ESTIMATE_BASIS = "手续费按交易所标准估算（设了自定义费率时按自设费率），保证金按行情源，实际以成交后为准。"
# 估算各金额的单位（#770）：模型复述时照这里带单位，不自己换算。
ESTIMATE_UNITS = {
    "price": "元/单位（报价）", "marginPerLot": "元/手", "commissionPerLot": "元/手（开仓一手，按现价）",
    "margin": "元（本次手数合计）", "fee": "元（本次手数合计）", "available": "元",
    "releasedMargin": "元", "maxOpenVolume": "手",
}
# 外盘二期：美元子账户的估算金额是美元。
ESTIMATE_UNITS_USD = {key: value.replace("元", "美元") for key, value in ESTIMATE_UNITS.items()}
# commissionSource：custom 自定义费率；exchange_standard 交易所标准（openctp 汇总，与本站模拟实扣
# 同一份）；market_source 行情源每手参考值（交易所标准暂不可用时的兜底，按成交额收费的品种可能
# 偏差很大，回答须说明是参考值）。


def _estimate_number(value: Any) -> float | None:
    """A finite positive-allowed float for estimates; anything else is None."""
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def close_preference(store: RuntimeStore, user_id: str) -> str:
    """设置 → 交易 → 平今平昨（today / historical / auto），读不到按 auto。"""
    try:
        return str(store.user_preferences(user_id).trade.closePreference)
    except Exception:
        return "auto"


def _close_fee_estimate(
    effective: dict | None, *, symbol: str, price: float | None, volume: int, multiplier: float | None,
    position: dict[str, Any], per_lot: float | None, preference: str, bucket: str | None,
) -> tuple[float | None, dict[str, dict[str, float]] | None]:
    """平仓手续费，按实际可平手数的今昨拆分算，今/昨两段都进 feeBreakdown：上期所/能源中心
    按指令的今/昨（`close_allocation`，`bucket` 显式给 today/historical 时只算那一侧）；
    其他交易所的平仓指令不分今昨，但费用按撮合的“先昨后今”拆（`close_yesterday_first_split`，
    #711）——昨仓可平量收平昨费率、剩余从今仓收平今费率，与本站模拟盘实扣
    `paper_book._fee_rate_commission` 同一口径。可平量之外的手数不虚构。`effective`
    是实际生效的费率表（自定义或交易所标准），None 时退回行情源每手手续费 `per_lot`。"""
    exchange = symbol.split(".", 1)[0].upper()
    if effective is not None and (price is None or not multiplier or multiplier <= 0):
        return None, None
    allocation = (close_allocation(position, volume=volume, preference=preference, bucket=bucket)
                  if exchange in CLOSE_TODAY_EXCHANGES
                  else close_yesterday_first_split(position, volume=volume))
    parts: dict[str, dict[str, float]] = {"today": {"volume": 0, "fee": 0.0},
                                          "historical": {"volume": 0, "fee": 0.0}}
    total = 0.0
    for name in ("today", "historical"):
        used = allocation[name]
        parts[name]["volume"] = used
        if used:
            fee = (round(per_lot * used, 2) if per_lot is not None else None) if effective is None else trade_fee(
                effective, symbol=symbol, price=price, volume=used, multiplier=multiplier,
                offset="CLOSE", today=name == "today")
            if fee is None:
                return None, None
            parts[name]["fee"] = fee
            total += fee
    return round(total, 2), parts


# Why a strategy's open card was not sent (出卡确认): the user's own stops, in the user's words.
STRATEGY_CARD_REFUSALS = {
    "ai_execution_stopped": "AI 模拟执行已停止或急停，这张策略开仓卡没有发单；恢复模拟执行后再确认。",
    "ai_new_opens_halted": "已只停新开仓，这张策略开仓卡没有发单；恢复模拟执行后再确认。",
    "strategy_not_monitoring": "策略已暂停或停止，这张开仓卡没有发单。",
    "strategy_position_already_protected": "这条策略已有持仓在保护中，这张开仓卡没有发单。",
    "strategy_pullback_signal_expired": "这张卡的开仓信号已过期（新的 K 线已收盘），没有发单。",
    "strategy_alignment_already_used": "本段方向已开过仓，这张开仓卡没有发单。",
    "strategy_trade_budget_exceeded": "按策略止损算，这笔超出策略的单笔预算，没有发单。",
    "invalid_strategy_protection": "策略止损价不成立，这张开仓卡没有发单。",
}

# 手动开仓卡顺带保护 (2026-10-02) 确认时的拒绝文案（价格类拒绝带自己的数字，走 message）。
MANUAL_CARD_REFUSALS = {
    "ai_execution_stopped": "AI 模拟执行已停止或急停，这张开仓卡没有发单；恢复模拟执行后再确认。",
    "ai_new_opens_halted": "已只停新开仓，这张开仓卡没有发单；恢复模拟执行后再确认。",
    "ai_execution_not_active": "保护性止损的执行通道没能建立，这张开仓卡没有发单。",
}

# 停止并平仓确认前重验 (#654) 的拒绝文案：这个策略自己的仓还配得上这张卡才发单，
# 账户里剩下的（手动或别的策略的）手数绝不代平。
STOP_CLOSE_CARD_REFUSALS = {
    "stop_close_card_unattributed": "这张停止平仓卡是在归属修复前出的，已作废；请重新停止并平仓，用新出的卡确认。",
    "stop_close_card_unbound": "这张停止平仓卡和它登记的策略执行记录对不上，已作废；请重新停止并平仓出新卡。",
    "stop_close_baseline_missing": "这张停止平仓卡没有停止时的成交基线，已作废；请重新停止并平仓出新卡。",
    "stop_close_position_gone": "停止平仓卡开出后这个策略已经没有持仓，这张卡没有发单；需要平仓请在持仓里处理。",
    "stop_close_protection_in_flight": "这个合约同方向还有在途或结果未知的平仓单（保护、手动或其它策略），这张卡没有发单；先等它结束，再重新停止并平仓。",
    "stop_close_position_changed": "停止平仓卡开出后这个策略的持仓手数或今/昨仓已变化，这张卡没有发单；请重新停止并平仓出新卡。",
    "stop_close_leg_consumed": "这张卡要平的仓已经被其它平仓单动过，这张卡没有发单；请核对持仓后重新停止并平仓。",
    "stop_close_position_unreadable": "这个策略的持仓暂时读不到，这张卡没有发单；稍后再确认或重新停止并平仓。",
}


def _card_protection_payload(
    protection: dict[str, Any], payload: dict[str, Any], *, side: str,
) -> dict[str, Any]:
    """Validate an open card's user-given stop / take against the card's own price and tick
    (生成卡时校验): 多仓 止损<委托价<止盈, 空仓相反; both must sit on the tick grid. Returns the
    normalized prices carried on the card; any violation raises with the Chinese reason."""
    from .autonomy import protection_price_text
    from .order_boundaries import price_on_tick
    long = side == "buy_open"
    price = Decimal(str(payload["limitPrice"]))
    tick = Decimal(str(payload["priceTick"]))
    normalized: dict[str, Any] = {}
    for key, word in (("stopLossPrice", "止损"), ("takeProfitPrice", "止盈")):
        value = protection.get(key)
        if value is None:
            continue
        try:
            level = Decimal(str(value))
        except ArithmeticError:
            raise ProposalConflict("protection_price_invalid", f"{word}价要是数字") from None
        if not level.is_finite() or level <= 0 or not price_on_tick(level, tick):
            raise ProposalConflict(
                "protection_price_invalid",
                f"{word}价 {protection_price_text(level) if level.is_finite() and level > 0 else value}"
                f" 不是这个合约最小变动价位的整数倍")
        if long and key == "stopLossPrice" and level >= price:
            raise ProposalConflict(
                "protection_stop_wrong_side",
                f"止损要低于委托价 {protection_price_text(price)}")
        if long and key == "takeProfitPrice" and level <= price:
            raise ProposalConflict(
                "protection_take_wrong_side",
                f"止盈要高于委托价 {protection_price_text(price)}")
        if not long and key == "stopLossPrice" and level <= price:
            raise ProposalConflict(
                "protection_stop_wrong_side",
                f"止损要高于委托价 {protection_price_text(price)}")
        if not long and key == "takeProfitPrice" and level >= price:
            raise ProposalConflict(
                "protection_take_wrong_side",
                f"止盈要低于委托价 {protection_price_text(price)}")
        normalized[key] = format(level, "f")
    # 追踪止损 / 浮盈 1R 后移到成本 (2026-10-25): both move the stop, so both need one; the fill arms them with it.
    if protection.get("trailing") or protection.get("breakevenAtR"):
        if "stopLossPrice" not in normalized:
            raise ProposalConflict("protection_trailing_needs_stop", "追踪止损和浮盈 1R 后移到成本都要先填止损价")
        if protection.get("trailing"):
            normalized["trailing"] = dict(protection["trailing"])
        if protection.get("breakevenAtR"):
            normalized["breakevenAtR"] = True
    return normalized


class ProposalConflict(Exception):
    def __init__(self, code: str, message: str | None = None) -> None:
        super().__init__(message or code)
        self.code = code
        # The user-facing sentence (下单顺带保护 etc.): str() keeps the code for logs and tests,
        # while the refusal paths read `.message` the way they read AutonomyError's.
        self.message = message


class ProposalInvalid(ProposalConflict):
    """A card request the Runtime cannot carry as asked (HTTP 422), with its Chinese sentence."""
    status_code = 422


class CloseBatchRefused(ProposalConflict):
    """A close-batch card that produced no order (#721): `skipped` carries each position's refusal
    code so the tool layer can tell the user which contract and why in Chinese; an empty list means
    there was no position to close at all. str() keeps the code — the log and HTTP shapes stay."""
    def __init__(self, code: str, skipped: list[dict[str, Any]] | None = None) -> None:
        super().__init__(code)
        self.skipped = list(skipped or [])


class RiskViolation(ProposalConflict):
    def __init__(self, proposal_id: str, risk_check: dict[str, Any]) -> None:
        super().__init__("risk_check_failed")
        self.proposal_id = proposal_id
        self.risk_check = risk_check


# 确认卡的确认窗口（utc_after(600)）过了：卡作废，没有下单。定时清理与迟到的确认说同一句话。
APPROVAL_EXPIRED_TEXT = "确认已超时（10 分钟），这张卡已作废，没有下单；需要的话重新下单。"
# 被新的目标持仓计划替代：旧计划的卡随计划一起作废（#518），迟到的确认说同一句话。
SUPERSEDED_CARD_TEXT = "已有新的目标持仓计划，这张旧确认卡已作废。"
# A 目标持仓 card given up or timed out ends its plan at once (#932): the result line says so.
PLAN_ENDED_TEXT = "这张卡所属的目标持仓计划也已取消，后面的步骤不会再来卡。"


def awaiting_confirmation(record: dict[str, Any], now: datetime | None = None) -> bool:
    """A card the user can still confirm: awaiting approval and inside its approval window."""
    expires = record.get("approvalExpiresAt")
    return (record.get("status") == "approval_required" and bool(expires)
            and datetime.fromisoformat(str(expires)) > (now or datetime.now(UTC)))


def card_account_fields(order_service: Any) -> dict[str, Any]:
    """一张卡写着下到哪类账户、按什么币种记账（外盘二期：美元子账户的卡金额是美元）。"""
    provider = order_service.data_provider
    account_id = getattr(provider, "account_id", None)
    if provider.mode != "paper":
        return {"accountLabel": "快期模拟账户", "currency": "CNY"}
    # 美元子账户同是本站模拟账户（卡上“以本站模拟查询为准”照旧）；它的名字由卡上的账户编号从账户目录读。
    return {"accountLabel": "本站模拟账户", "currency": "USD" if is_foreign_paper(account_id) else "CNY"}


class ProposalService:
    TERMINAL = {"candidate_recorded", "risk_rules_applied", "rejected", "cancelled"}

    def __init__(
        self, store: RuntimeStore, broker: EventBroker,
        policies: RiskPolicyProvider | None = None,
        instruments: InstrumentSpecProvider | None = None,
        data_provider: Any | None = None,
        simulation_orders: SimulationOrderService | None = None,
        accounts: AccountRegistry | None = None,
        fee_settings: FeeSettings | None = None,
    ) -> None:
        self.store = store
        self.broker = broker
        self.policies = policies or RiskPolicyProvider(store)
        self.instruments = instruments or InstrumentSpecProvider()
        self.data_provider = data_provider
        self.simulation_orders = simulation_orders
        self.accounts = accounts
        self.fee_settings = fee_settings

    def _user(self, session_id: str) -> str:
        session = self.broker.get_session(session_id)
        if session is None:
            raise KeyError(session_id)
        return session.user_id

    def _view(self, account: AccountServices | None) -> tuple[Any, SimulationOrderService | None]:
        """The provider and simulation gateway a proposal acts on: the resolved
        account's, else the constructor's (the default account's)."""
        if account is None:
            return self.data_provider, self.simulation_orders
        return account.provider, account.simulation_orders

    def _session_account(self, session_id: str | None) -> AccountServices | None:
        """The account the request's session acts on, when account wiring is present."""
        if self.accounts is None or not session_id:
            return None
        session = self.broker.get_session(session_id)
        if session is None or not session.trading_account_id:
            return None
        return self._account_by_id(session.trading_account_id)

    def _card_account(self, session_id: str | None, account_id: str | None) -> AccountServices | None:
        """The account a new card acts on: the one the caller named explicitly (already checked by the route:
        granted and writable), else the session's. Never a silent fallback when the named one is missing."""
        if account_id:
            if self.accounts is None:
                raise ProposalConflict("account_not_assembled")
            return self._account_by_id(account_id)
        return self._session_account(session_id)

    def _account_note(self, session_id: str | None, account: AccountServices | None) -> dict[str, Any]:
        """A card made on another account than its conversation's says so (调用方显式指定账户)."""
        session = self.broker.get_session(session_id) if session_id else None
        session_account_id = str(getattr(session, "trading_account_id", "") or "")
        if account is None or not session_account_id or account.account_id == session_account_id:
            return {}
        return {"accountOverride": {"accountId": account.account_id, "sessionAccountId": session_account_id,
                                    "note": f"按调用方指定的账户 {account.account_id} 出卡（这个对话的账户是 {session_account_id}）"}}

    def _account_by_id(self, account_id: Any) -> AccountServices | None:
        """The account a persisted row (strategy draft, proposal) belongs to."""
        if self.accounts is None or not account_id:
            return None
        account = self.accounts.get(str(account_id))
        if account is None:
            # A row bound to an account this boot no longer assembles must not
            # act on the default account instead — confirm/dispatch would trade
            # the wrong book.
            raise ProposalConflict("account_not_assembled")
        return account

    def _risk(
        self, payload: dict[str, Any], user_id: str, *,
        provider: Any | None = None, simulation_orders: SimulationOrderService | None = None,
    ) -> dict[str, Any]:
        data_provider = provider if provider is not None else self.data_provider
        order_service = simulation_orders if simulation_orders is not None else self.simulation_orders
        today_pnl: Decimal | None = None
        trusted_payload = dict(payload)
        if data_provider is not None:
            try:
                account = data_provider.account_summary("")
                current = (
                    account.get("dataStatus") in {"normal", "simulated"}
                    and not bool(account.get("isCached"))
                )
                value = account.get("todayNetPnl", account.get("todayPnl")) if current else None
                today_pnl = Decimal(str(value)) if value is not None else None
                market = data_provider.market_snapshot("", str(payload["symbol"]))
                trusted_payload["contractMultiplier"] = market["volumeMultiple"]
                trusted_payload["priceTick"] = market["priceTick"]
            except (KeyError, RuntimeError, TypeError, ValueError):
                today_pnl = None
        return calculate_trade_risk(
            trusted_payload, user_id=user_id, policies=self.policies,
            instruments=self.instruments, today_pnl=today_pnl,
            now=order_service.trading_now() if order_service else None,
        )

    @staticmethod
    def _risk_rules(
        *, allowed_symbols: list[str], max_loss_per_trade: str,
        max_daily_loss: str, no_new_positions_after: str,
    ) -> dict[str, Any]:
        symbols = sorted({symbol.strip().upper() for symbol in allowed_symbols if symbol.strip()})
        if not symbols or len(symbols) > 20:
            raise ProposalConflict("invalid_allowed_symbols")
        if any(not re.fullmatch(r"[A-Z]{1,4}\d{3,4}", symbol) for symbol in symbols):
            raise ProposalConflict("invalid_allowed_symbols")
        per_trade, daily = Decimal(max_loss_per_trade), Decimal(max_daily_loss)
        if not per_trade.is_finite() or not daily.is_finite() or per_trade <= 0 or daily <= 0:
            raise ProposalConflict("invalid_risk_amount")
        if per_trade > daily:
            raise ProposalConflict("per_trade_limit_exceeds_daily_limit")
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", no_new_positions_after):
            raise ProposalConflict("invalid_cutoff_time")
        return {
            "allowedSymbols": symbols,
            "maxLossPerTrade": format(per_trade.quantize(Decimal("0.01")), "f"),
            "maxDailyLoss": format(daily.quantize(Decimal("0.01")), "f"),
            "noNewPositionsAfter": no_new_positions_after,
            "timezone": "Asia/Shanghai",
        }

    def create_risk_rule(
        self, session_id: str, turn_id: str, *, allowed_symbols: list[str],
        max_loss_per_trade: str, max_daily_loss: str,
        no_new_positions_after: str,
    ) -> dict[str, Any]:
        user_id, proposal_id, approval_id = self._user(session_id), str(uuid4()), str(uuid4())
        rules = self._risk_rules(
            allowed_symbols=allowed_symbols, max_loss_per_trade=max_loss_per_trade,
            max_daily_loss=max_daily_loss, no_new_positions_after=no_new_positions_after,
        )
        current = self.store.active_risk_policy(user_id=user_id)
        base_version = int(current["version"]) if current else 0
        digest = hashlib.sha256(
            json.dumps(rules, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        names = {"AU2612": "沪金", "RB2610": "螺纹钢"}
        payload = {
            "turnId": turn_id, "proposalId": proposal_id, "version": 1,
            "basePolicyVersion": base_version, "policyVersion": base_version + 1,
            "allowedInstruments": [
                {"symbol": symbol, "name": names.get(symbol, symbol)} for symbol in rules["allowedSymbols"]
            ],
            "maxLossPerTrade": rules["maxLossPerTrade"],
            "maxDailyLoss": rules["maxDailyLoss"],
            "noNewPositionsAfter": rules["noNewPositionsAfter"],
            "timezone": rules["timezone"], "rulesDigest": digest,
            "deterministic": True, "executionEnabled": False,
            "status": "approval_required", "approvalId": approval_id,
        }
        self.store.create(
            proposal_id=proposal_id, session_id=session_id, proposal_type="risk_rule",
            status="approval_required", payload=payload, user_id=user_id, version=1,
            approval_id=approval_id, approval_expires_at=utc_after(600),
        )
        return payload

    @staticmethod
    def _strategy_condition_labels(
        template: str, parameters: dict[str, int | float],
    ) -> list[dict[str, str]]:
        labels: list[dict[str, str]] = []
        if template in {"ma_cross", "combined"}:
            labels.append({
                "label": "均线交叉",
                "detail": f"{parameters['fastPeriod']} 根均线与 {parameters['slowPeriod']} 根均线交叉",
            })
        if template in {"breakout", "combined"}:
            labels.append({
                "label": "区间突破",
                "detail": f"突破最近 {parameters['breakoutPeriod']} 根已完成K线区间",
            })
        if template in {"kd_cross", "combined"}:
            labels.append({
                "label": "KD 交叉",
                "detail": f"使用 {parameters['kdPeriod']} 根K线计算的 K、D 交叉",
            })
        if template in {"macd_cross", "combined"}:
            labels.append({
                "label": "MACD 交叉",
                "detail": (
                    f"快线 {parameters['macdFast']}、慢线 {parameters['macdSlow']}、"
                    f"信号线 {parameters['macdSignal']}"
                ),
            })
        return labels

    def create_strategy_draft(
        self, session_id: str, turn_id: str, *, name: str, symbol: str,
        template: str, timeframe_seconds: int, direction: str,
        parameters: dict[str, int | float], max_position: int,
        max_orders_per_day: int, stop_loss_ticks: int,
        take_profit_ticks: int, cooldown_bars: int, rationale: str,
    ) -> dict[str, Any]:
        """An old-format (schemaVersion 1, template-parameter) draft. Nothing in the product saves one any more — the
        AI's create_strategy_draft saves a StrategyPlan through POST /api/strategy-plans (#932 F5); drafts saved
        before stay readable, validatable and deletable, and tests build them with this."""
        user_id = self._user(session_id)
        clean_name = " ".join(name.strip().split())
        if not clean_name or len(clean_name) > 80:
            raise ProposalConflict("invalid_strategy_name")
        allowed_templates = {"ma_cross", "breakout", "kd_cross", "macd_cross", "combined"}
        if template not in allowed_templates:
            raise ProposalConflict("invalid_strategy_template")
        if timeframe_seconds not in {60, 180, 300, 900, 3600, 86400}:
            raise ProposalConflict("invalid_strategy_timeframe")
        if direction not in {"long", "short", "both"}:
            raise ProposalConflict("invalid_strategy_direction")
        fast, slow = int(parameters["fastPeriod"]), int(parameters["slowPeriod"])
        macd_fast, macd_slow = int(parameters["macdFast"]), int(parameters["macdSlow"])
        if not 2 <= fast < slow <= 240:
            raise ProposalConflict("invalid_ma_periods")
        if not 2 <= macd_fast < macd_slow <= 240:
            raise ProposalConflict("invalid_macd_periods")
        if not 2 <= int(parameters["macdSignal"]) <= 60:
            raise ProposalConflict("invalid_macd_signal")
        if not 3 <= int(parameters["breakoutPeriod"]) <= 240:
            raise ProposalConflict("invalid_breakout_period")
        if not 3 <= int(parameters["kdPeriod"]) <= 120:
            raise ProposalConflict("invalid_kd_period")
        if not 0.1 <= float(parameters["minimumVolumeRatio"]) <= 10:
            raise ProposalConflict("invalid_volume_ratio")
        if not 1 <= max_position <= 20 or not 1 <= max_orders_per_day <= 50:
            raise ProposalConflict("invalid_strategy_limits")
        if not 1 <= stop_loss_ticks <= 5000 or not 1 <= take_profit_ticks <= 10000:
            raise ProposalConflict("invalid_strategy_exit")
        if not 0 <= cooldown_bars <= 100:
            raise ProposalConflict("invalid_strategy_cooldown")

        session_account = self._session_account(session_id)
        catalog_provider, _ = self._view(session_account)
        catalog = catalog_provider.instrument_catalog() if catalog_provider else None
        if not isinstance(catalog, dict) or catalog.get("dataStatus") not in {"normal", "simulated"}:
            raise ProposalConflict("instrument_catalog_not_fresh")
        requested_symbol = symbol.strip()
        item = next(
            (
                candidate for candidate in catalog.get("items", [])
                if isinstance(candidate, dict)
                and str(candidate.get("symbol", "")).casefold() == requested_symbol.casefold()
            ),
            None,
        )
        if item is None or int(item.get("expireRestDays", 0)) <= 0:
            raise ProposalConflict("actual_delivery_symbol_required")
        canonical_symbol = str(item["symbol"])
        normalized_parameters: dict[str, int | float] = {
            "fastPeriod": fast,
            "slowPeriod": slow,
            "breakoutPeriod": int(parameters["breakoutPeriod"]),
            "kdPeriod": int(parameters["kdPeriod"]),
            "macdFast": macd_fast,
            "macdSlow": macd_slow,
            "macdSignal": int(parameters["macdSignal"]),
            "minimumVolumeRatio": round(float(parameters["minimumVolumeRatio"]), 4),
        }
        definition = {
            "schemaVersion": 1,
            "template": template,
            "timeframeSeconds": timeframe_seconds,
            "direction": direction,
            "parameters": normalized_parameters,
            "riskLimits": {
                "maxPosition": max_position,
                "maxOrdersPerDay": max_orders_per_day,
                "stopLossTicks": stop_loss_ticks,
                "takeProfitTicks": take_profit_ticks,
                "cooldownBars": cooldown_bars,
            },
            "rationale": rationale.strip(),
            "signalInput": "completed_bars_only",
            "executionMode": "simulation_only",
            "liveTrading": "disabled",
        }
        digest = hashlib.sha256(
            json.dumps(definition, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        version = self.store.next_strategy_draft_version(user_id=user_id, name=clean_name)
        strategy_id = str(uuid4())
        saved = self.store.save_strategy_draft(
            strategy_id=strategy_id,
            user_id=user_id,
            source_session_id=session_id,
            name=clean_name,
            symbol=canonical_symbol,
            version=version,
            status="draft",
            definition=definition,
            definition_digest=digest,
            trading_account_id=session_account.account_id if session_account is not None else None,
        )
        conditions = [
            {**condition, "status": "waiting"}
            for condition in self._strategy_condition_labels(template, normalized_parameters)
        ]
        conditions.extend([
            {
                "label": "风险边界",
                "status": "passed",
                "detail": f"最多 {max_position} 手、每交易日最多开仓 {max_orders_per_day} 次",
            },
            {
                "label": "历史回测",
                "status": "waiting",
                "detail": "尚未完成，不允许启动自动执行",
            },
        ])
        return {
            "turnId": turn_id,
            "strategyId": strategy_id,
            "name": clean_name,
            "symbol": canonical_symbol,
            "version": f"草案 v{version}",
            "status": "draft",
            "summary": "AI 已整理为确定性策略草案；完成回测并确认前只保存，不执行。",
            "conditions": conditions,
            "template": template,
            "timeframeSeconds": timeframe_seconds,
            "direction": direction,
            "riskLimits": definition["riskLimits"],
            "definitionDigest": digest,
            "backtestStatus": "not_run",
            "executionMode": "simulation_only",
            "createdAt": saved["createdAt"],
        }

    def prepare_strategy_validation(
        self, *, strategy_id: str, user_id: str, turn_id: str,
    ) -> dict[str, Any]:
        strategy = self.store.get_strategy_draft(strategy_id, user_id=user_id)
        if strategy["status"] not in {"draft", "validating"}:
            raise ProposalConflict("strategy_validation_state_invalid")
        existing = self.store.latest_strategy_validation_report(
            strategy_id, user_id=user_id
        )
        if (
            strategy["status"] == "validating"
            and existing is not None
            and existing["strategyDigest"] == strategy["definitionDigest"]
            and existing["status"] == "incomplete"
        ):
            report = existing
        else:
            definition = strategy["definition"]
            checks = [
                {
                    "id": "definition_integrity",
                    "label": "策略定义完整",
                    "status": "passed",
                    "detail": "合约、周期、方向、信号和风险参数均已固化",
                },
                {
                    "id": "completed_bars_only",
                    "label": "只使用已完成K线",
                    "status": "passed" if definition.get("signalInput") == "completed_bars_only" else "failed",
                    "detail": "避免未完成K线导致信号闪烁",
                },
                {
                    "id": "lookahead_bias",
                    "label": "未来数据偏差检查",
                    "status": "pending",
                    "detail": "等待历史回测逐时点重放验证",
                },
                {
                    "id": "out_of_sample",
                    "label": "样本外验证",
                    "status": "pending",
                    "detail": "训练、验证和最终测试区间必须相互分离",
                },
                {
                    "id": "trading_costs",
                    "label": "交易成本",
                    "status": "pending",
                    "detail": "等待加入手续费、滑点、最小变动价位和合约乘数",
                },
                {
                    "id": "risk_protections",
                    "label": "风险保护器",
                    "status": "pending",
                    "detail": "等待验证连续止损、最大回撤和冷却暂停",
                },
                {
                    "id": "minimum_signals",
                    "label": "有效信号数量",
                    "status": "pending",
                    "detail": "等待确认样本内外均有足够信号，不靠偶然单次交易",
                },
            ]
            report_id = str(uuid4())
            with self.store.transaction() as connection:
                report = self.store.save_strategy_validation_report(
                    report_id=report_id,
                    strategy_id=strategy_id,
                    user_id=user_id,
                    strategy_digest=strategy["definitionDigest"],
                    status="incomplete",
                    checks=checks,
                    evidence={
                        "required": [
                            "historical_replay", "lookahead_analysis", "out_of_sample_metrics",
                            "cost_model", "protection_results",
                        ],
                        "backtestId": None,
                        "liveTrading": "disabled",
                    },
                    connection=connection,
                )
                if strategy["status"] == "draft":
                    strategy = self.store.update_strategy_draft_status(
                        strategy_id,
                        user_id=user_id,
                        expected_status="draft",
                        status="validating",
                        connection=connection,
                    )
        return {
            "turnId": turn_id,
            "strategyId": strategy_id,
            "name": strategy["name"],
            "symbol": strategy["symbol"],
            "version": f"草案 v{strategy['version']}",
            "status": "validating",
            "summary": "基础检查已通过，仍需历史回放、样本外和成本验证；当前不能启动策略。",
            "conditions": [
                {
                    "label": check["label"],
                    "status": "passed" if check["status"] == "passed" else "waiting",
                    "detail": check["detail"],
                }
                for check in report["checks"]
            ],
            "template": strategy["definition"]["template"],
            "timeframeSeconds": strategy["definition"]["timeframeSeconds"],
            "direction": strategy["definition"]["direction"],
            "riskLimits": strategy["definition"]["riskLimits"],
            "definitionDigest": strategy["definitionDigest"],
            "backtestStatus": "not_run",
            "executionMode": "simulation_only",
            "createdAt": strategy["createdAt"],
            "validationReportId": report["reportId"],
            "validationStatus": report["status"],
        }

    def transition_strategy_status(
        self, *, strategy_id: str, user_id: str, target: str,
        validation_report_id: str | None = None,
    ) -> dict[str, Any]:
        strategy = self.store.get_strategy_draft(strategy_id, user_id=user_id)
        current = str(strategy["status"])
        allowed = {
            "draft": {"validating", "archived"},
            "validating": {"ready", "archived"},
            "ready": {"monitoring", "archived"},
            # 停止 (2026-09-25): a running or paused instance goes back to ready — no more signal
            # judgements, its history and any open position stay exactly as they are.
            "monitoring": {"paused", "ready"},
            "paused": {"monitoring", "archived", "ready"},
            "archived": set(),
        }
        if target not in allowed.get(current, set()):
            raise ProposalConflict("strategy_transition_invalid")
        # Starting needs the current version's passed validation, and so does promoting a draft;
        # stopping back to ready never does (a stop must always succeed).
        if target == "monitoring" or (target == "ready" and current in {"draft", "validating"}):
            report = (
                self.store.get_strategy_validation_report(
                    validation_report_id, user_id=user_id
                )
                if validation_report_id
                else self.store.latest_strategy_validation_report(strategy_id, user_id=user_id)
            )
            if (
                report is None
                or report["strategyId"] != strategy_id
                or report["strategyDigest"] != strategy["definitionDigest"]
                or report["status"] != "passed"
            ):
                raise ProposalConflict("strategy_validation_not_passed")
        return self.store.update_strategy_draft_status(
            strategy_id,
            user_id=user_id,
            expected_status=current,
            status=target,
        )

    def pause_monitoring_strategies(self, *, user_id: str | None, trading_account_id: str) -> list[str]:
        """Pause monitoring strategies on one trading account (all actors for an account stop): a stop or emergency
        stop acts on the account it was issued for, not the user's other accounts (owner 2026-10-04)."""
        with self.store.transaction() as connection:
            rows = connection.execute(
                """SELECT id FROM strategy_drafts
                WHERE (? IS NULL OR user_id = ?) AND trading_account_id = ? AND status = 'monitoring'
                ORDER BY updated_at, id""",
                (user_id, user_id, trading_account_id),
            ).fetchall()
            connection.execute(
                """UPDATE strategy_drafts SET status = 'paused', updated_at = ?
                WHERE (? IS NULL OR user_id = ?) AND trading_account_id = ? AND status = 'monitoring'""",
                (utc_now(), user_id, user_id, trading_account_id),
            )
        return [str(row["id"]) for row in rows]

    def complete_strategy_validation(
        self, *, strategy_id: str, report_id: str, user_id: str, turn_id: str,
        result: dict[str, Any], source_backtest_id: str,
    ) -> dict[str, Any]:
        strategy = self.store.get_strategy_draft(strategy_id, user_id=user_id)
        report = self.store.get_strategy_validation_report(report_id, user_id=user_id)
        checks = list(result.get("checks") or [])
        evidence_result = dict(result.get("evidence") or {})
        required_checks = {
            "definition_integrity", "completed_bars_only", "lookahead_bias",
            "out_of_sample", "trading_costs", "risk_protections",
            "minimum_signals", "official_history",
        }
        checks_by_id = {
            str(check.get("id")): str(check.get("status"))
            for check in checks if isinstance(check, dict)
        }
        passed_evidence_valid = (
            required_checks.issubset(checks_by_id)
            and all(checks_by_id[item] == "passed" for item in required_checks)
            and str(evidence_result.get("dataSource") or "").startswith("tqsdk:")
            and len(str(evidence_result.get("dataDigest") or "")) == 64
            and len(str(evidence_result.get("engineDigest") or "")) == 64
            and evidence_result.get("liveTrading") == "disabled"
        )
        if (
            strategy["status"] != "validating"
            or report["strategyId"] != strategy_id
            or report["strategyDigest"] != strategy["definitionDigest"]
            or result.get("status") not in {"passed", "failed"}
            or (result.get("status") == "passed" and not passed_evidence_valid)
        ):
            raise ProposalConflict("strategy_validation_result_invalid")
        evidence = {
            **evidence_result,
            "sourceBacktestId": source_backtest_id,
            "sourceBacktestRole": "immutable_history_artifact_only",
            "strategyDigest": strategy["definitionDigest"],
            "executionEnabled": False,
            "orderCapabilities": [],
        }
        with self.store.transaction() as connection:
            completed = self.store.complete_strategy_validation_report(
                report_id,
                user_id=user_id,
                strategy_digest=strategy["definitionDigest"],
                status=str(result["status"]),
                checks=checks,
                evidence=evidence,
                connection=connection,
            )
            if completed["status"] == "passed":
                strategy = self.store.update_strategy_draft_status(
                    strategy_id,
                    user_id=user_id,
                    expected_status="validating",
                    status="ready",
                    connection=connection,
                )
        status = "ready" if completed["status"] == "passed" else "validating"
        summary = (
            "历史回放、样本外和成本验证已通过；策略仍未启动，也没有提交模拟委托。"
            if completed["status"] == "passed"
            else "历史验证未通过；策略保持验证中，不能启动。"
        )
        return {
            "turnId": turn_id,
            "strategyId": strategy_id,
            "name": strategy["name"],
            "symbol": strategy["symbol"],
            "version": f"草案 v{strategy['version']}",
            "status": status,
            "summary": summary,
            "conditions": [
                {
                    "label": check["label"],
                    "status": check["status"],
                    "detail": check["detail"],
                }
                for check in completed["checks"]
            ],
            "template": strategy["definition"]["template"],
            "timeframeSeconds": strategy["definition"]["timeframeSeconds"],
            "direction": strategy["definition"]["direction"],
            "riskLimits": strategy["definition"]["riskLimits"],
            "definitionDigest": strategy["definitionDigest"],
            "backtestStatus": completed["status"],
            "executionMode": "simulation_only",
            "createdAt": strategy["createdAt"],
            "validationReportId": report_id,
            "validationStatus": completed["status"],
        }

    def evaluate_strategy_draft(
        self, *, strategy_id: str, user_id: str, turn_id: str,
    ) -> dict[str, Any]:
        stored = self.store.get_strategy_draft(strategy_id, user_id=user_id)
        definition = stored["definition"]
        # The market the strategy is judged on is its own account's market.
        strategy_provider, _ = self._view(self._account_by_id(stored.get("tradingAccountId")))
        if definition.get("schemaVersion") == 2:
            from .strategy_rules import evaluate_plan
            try:
                result = evaluate_plan(strategy_provider, definition, stored["symbol"])
            except ValueError as error:
                raise ProposalConflict(getattr(error, "code", str(error)), str(error)) from error
            return {**result, "turnId": turn_id, "strategyId": strategy_id, "name": stored["name"],
                    "signal": "wait", "dataStatus": "normal", "template": "combined",
                    "timeframeSeconds": definition["timeframeSeconds"], "direction": definition["direction"],
                    "riskLimits": definition["riskLimits"], "definitionDigest": stored["definitionDigest"]}
        timeframe = int(definition["timeframeSeconds"])
        workbench = strategy_provider.market_workbench(stored["symbol"], timeframe)
        if (
            not isinstance(workbench, dict)
            or workbench.get("dataStatus") not in {"normal", "simulated"}
            or not isinstance(workbench.get("bars"), list)
        ):
            raise ProposalConflict("strategy_market_not_fresh")
        # TqSdk's final K-line is still forming. Strategy signals only read the
        # previous completed bar, matching the official update-loop semantics.
        bars = [item for item in workbench["bars"] if isinstance(item, dict)][:-1]
        parameters = definition["parameters"]
        required = max(
            int(parameters["slowPeriod"]) + 2,
            int(parameters["breakoutPeriod"]) + 2,
            int(parameters["kdPeriod"]) + 5,
            int(parameters["macdSlow"]) + int(parameters["macdSignal"]) + 2,
        )
        if len(bars) < required:
            raise ProposalConflict("strategy_bars_insufficient")
        closes = [float(item["close"]) for item in bars]
        highs = [float(item["high"]) for item in bars]
        lows = [float(item["low"]) for item in bars]
        volumes = [float(item["volume"]) for item in bars]

        fast_period, slow_period = int(parameters["fastPeriod"]), int(parameters["slowPeriod"])
        current_fast = sum(closes[-fast_period:]) / fast_period
        previous_fast = sum(closes[-fast_period - 1:-1]) / fast_period
        current_slow = sum(closes[-slow_period:]) / slow_period
        previous_slow = sum(closes[-slow_period - 1:-1]) / slow_period
        signals = {
            "ma_cross": cross_signal(current_fast, current_slow, previous_fast, previous_slow)
        }

        breakout_period = int(parameters["breakoutPeriod"])
        reference_high = max(highs[-breakout_period - 1:-1])
        reference_low = min(lows[-breakout_period - 1:-1])
        signals["breakout"] = (
            "buy" if closes[-1] > reference_high
            else "sell" if closes[-1] < reference_low
            else "wait"
        )

        kd_period = int(parameters["kdPeriod"])
        k_values: list[float] = []
        for index in range(kd_period - 1, len(closes)):
            low = min(lows[index - kd_period + 1:index + 1])
            high = max(highs[index - kd_period + 1:index + 1])
            k_values.append(50.0 if high == low else (closes[index] - low) / (high - low) * 100)
        d_values = [
            sum(k_values[max(0, index - 2):index + 1]) / min(3, index + 1)
            for index in range(len(k_values))
        ]
        signals["kd_cross"] = cross_signal(
            k_values[-1], d_values[-1], k_values[-2], d_values[-2]
        )

        macd_fast = ema(closes, int(parameters["macdFast"]))
        macd_slow = ema(closes, int(parameters["macdSlow"]))
        dif = [fast - slow for fast, slow in zip(macd_fast, macd_slow, strict=True)]
        dea = ema(dif, int(parameters["macdSignal"]))
        signals["macd_cross"] = cross_signal(dif[-1], dea[-1], dif[-2], dea[-2])

        average_volume = sum(volumes[-21:-1]) / 20
        volume_ratio = volumes[-1] / average_volume if average_volume else 0
        volume_ready = volume_ratio >= float(parameters["minimumVolumeRatio"])
        template = str(definition["template"])
        selected = [signals[key] for key in (
            [template] if template != "combined"
            else ["ma_cross", "breakout", "kd_cross", "macd_cross"]
        )]
        signal = (
            selected[0] if template != "combined"
            else "buy" if all(item == "buy" for item in selected)
            else "sell" if all(item == "sell" for item in selected)
            else "wait"
        )
        if not volume_ready:
            signal = "wait"
        direction = str(definition["direction"])
        if (signal == "buy" and direction == "short") or (signal == "sell" and direction == "long"):
            signal = "wait"

        condition_labels = self._strategy_condition_labels(template, parameters)
        conditions = []
        for condition, key in zip(
            condition_labels,
            [template] if template != "combined" else ["ma_cross", "breakout", "kd_cross", "macd_cross"],
            strict=True,
        ):
            current = signals[key]
            conditions.append({
                **condition,
                "status": "passed" if current in {"buy", "sell"} else "waiting",
                "detail": f"{condition['detail']} · 当前{'买入信号' if current == 'buy' else '卖出信号' if current == 'sell' else '未交叉或未突破'}",
            })
        conditions.append({
            "label": "成交量过滤",
            "status": "passed" if volume_ready else "waiting",
            "detail": f"当前为近期均量的 {volume_ratio:.2f} 倍，要求至少 {float(parameters['minimumVolumeRatio']):.2f} 倍",
        })
        action_label = "出现买入信号" if signal == "buy" else "出现卖出信号" if signal == "sell" else "条件尚未同时满足"
        return {
            "turnId": turn_id,
            "strategyId": strategy_id,
            "name": stored["name"],
            "symbol": stored["symbol"],
            "version": f"草案 v{stored['version']}",
            "status": "waiting",
            "summary": f"{action_label}；这只是已完成K线的信号预览，不会自动下单。",
            "conditions": conditions,
            "template": template,
            "timeframeSeconds": timeframe,
            "direction": direction,
            "riskLimits": definition["riskLimits"],
            "definitionDigest": stored["definitionDigest"],
            "backtestStatus": "not_run",
            "executionMode": "simulation_only",
            "createdAt": stored["createdAt"],
            "signal": signal,
            "evaluatedBarTime": str(bars[-1].get("datetime") or ""),
            "dataStatus": workbench["dataStatus"],
        }

    def _engine_inputs(self, strategy: dict[str, Any], symbol: str, provider: Any, orders: Any,
                       account: dict[str, Any]) -> dict[str, Any]:
        """The strategy engine's live inputs for a v2 strategy: its position state from the store
        and the trading clock (the replay clock in a replay, else now) for 收盘前平仓. 按资金比例
        定手数 adds the shared funds core's budget off the same fresh facts."""
        if (strategy.get("definition") or {}).get("schemaVersion") != 2:
            return {}
        from .strategy_protection import live_position_state
        clock = orders.trading_now() if orders else None
        inputs: dict[str, Any] = {"engine_state": live_position_state(self.store, provider, strategy=strategy,
                                                                      symbol=symbol, account=account),
                                  "decision_time": clock or datetime.now(UTC)}
        sizing = ((strategy.get("definition") or {}).get("plan") or {}).get("sizing") or {}
        if sizing.get("mode") == "equity_percent" and sizing.get("equityPercent") is not None:
            from .strategy_funds import live_budget
            inputs["funds"] = live_budget(
                self.store, provider, user_id=str(strategy["userId"]),
                strategy_id=str(strategy["strategyId"]), symbol=symbol, account=account,
                fee_settings=self.fee_settings, gateway=orders,
                equity_percent=float(sizing["equityPercent"]),
                max_lots=(int(sizing["maxLots"]) if sizing.get("maxLots") is not None else None))
        return inputs

    def preview_strategy_intent(
        self, *, strategy_id: str, user_id: str, turn_id: str,
        own_position: tuple[str | None, int] | None = None,
    ) -> dict[str, Any]:
        strategy = self.store.get_strategy_draft(strategy_id, user_id=user_id)
        if strategy["status"] not in {"ready", "monitoring"}:
            raise ProposalConflict("strategy_not_ready_for_intent")
        report = self.store.latest_strategy_validation_report(
            strategy_id, user_id=user_id
        )
        if (
            report is None
            or report["status"] != "passed"
            or report["strategyDigest"] != strategy["definitionDigest"]
        ):
            raise ProposalConflict("strategy_validation_not_passed")
        evaluation = self.evaluate_strategy_draft(
            strategy_id=strategy_id, user_id=user_id, turn_id=turn_id
        )
        strategy_provider, strategy_orders = self._view(
            self._account_by_id(strategy.get("tradingAccountId")))
        market = strategy_provider.market_snapshot(turn_id, strategy["symbol"])
        account = strategy_provider.account_summary(turn_id)
        positions = strategy_provider.positions()
        activity = strategy_provider.account_activity()
        if (
            not isinstance(market, dict)
            or not isinstance(account, dict)
            or not isinstance(positions, list)
            or not isinstance(activity, dict)
        ):
            raise ProposalConflict("strategy_intent_context_invalid")
        intent = build_strategy_intent(
            strategy=strategy,
            evaluation=evaluation,
            market=market,
            account=account,
            positions=positions,
            activity=activity,
            user_id=user_id,
            policies=self.policies,
            instruments=self.instruments,
            now=strategy_orders.trading_now() if strategy_orders else None,
            own_position=own_position,
            **self._engine_inputs(strategy, strategy["symbol"], strategy_provider, strategy_orders, account),
        )
        saved = self.store.save_strategy_intent_snapshot(
            intent_id=str(uuid4()),
            strategy_id=strategy_id,
            user_id=user_id,
            strategy_digest=strategy["definitionDigest"],
            signal_bar_time=str(intent["signalSnapshot"]["barTime"]),
            context_digest=str(intent["contextDigest"]),
            payload=intent,
        )
        payload = saved["payload"]
        adjusted = payload["riskAdjustedIntent"]
        action_labels = {
            "hold": "继续等待",
            "open_long": "准备开多",
            "open_short": "准备开空",
            "close_long": "优先平多",
            "close_short": "优先平空",
            "reduce_long": "降低多仓",
            "reduce_short": "降低空仓",
        }
        action_label = action_labels.get(str(adjusted["action"]), "继续等待")
        if adjusted["status"] == "blocked":
            summary = f"{action_label}意图被风险规则拦截；不会创建委托。"
        elif adjusted["status"] == "no_action":
            summary = "当前没有新的开平仓意图，继续等待下一根已完成K线。"
        else:
            summary = (
                f"{action_label} {adjusted['volume']} 手；这里只保存风险裁剪后的意图，"
                "不会创建委托。"
            )
        conditions = list(evaluation["conditions"])
        conditions.extend({
            "label": check["label"],
            "status": "passed" if check["passed"] else "failed",
            "detail": "已通过" if check["passed"] else "未通过，停止扩大风险",
        } for check in adjusted["checks"])
        return {
            **evaluation,
            "status": strategy["status"],
            "summary": summary,
            "conditions": conditions,
            "backtestStatus": "passed",
            "validationReportId": report["reportId"],
            "validationStatus": "passed",
            "intentId": saved["intentId"],
            "signalSnapshot": payload["signalSnapshot"],
            "positionIntent": payload["positionIntent"],
            "riskAdjustedIntent": adjusted,
        }

    def explain_strategy_signal(
        self, *, user_id: str, turn_id: str, strategy_id: str | None = None,
        symbol: str | None = None, chart_timeframe_seconds: int | None = None,
    ) -> dict[str, Any]:
        """Read one strategy's signal on the current data (the replay clock in a replay) and explain it:
        the conclusion (开多/开空/平多/平空/观望), each condition's state and gap, the reference entry
        price and the strategy's stop/take, plus the chart annotations of the read. Read-only — no
        intent snapshot, no candidate, no order; an unvalidated draft is still explained, flagged.
        With no strategy_id the monitoring strategies on that symbol win (several → the caller is
        given the candidates to choose from), else the most recently edited one."""
        if strategy_id:
            stored = self.store.get_strategy_draft(strategy_id, user_id=user_id)
        else:
            candidates = [
                row for row in self.store.list_strategy_drafts(user_id=user_id, limit=50)
                if row.get("status") != "archived"
                and (not symbol or row.get("symbol") in {symbol, "*"})
            ]
            monitoring = [row for row in candidates if row.get("status") == "monitoring"]
            if len(monitoring) > 1:
                return {
                    "requiresSelection": True,
                    "reason": "这个合约上有多张监控中的策略，请说明用哪一张。",
                    "candidates": [
                        {"strategyId": row["strategyId"], "name": row["name"], "status": row["status"]}
                        for row in monitoring
                    ],
                }
            pool = monitoring or candidates
            if not pool:
                raise ProposalConflict(
                    "strategy_signal_no_candidate",
                    "这个合约上没有可解读的策略：先在策略工坊保存或启动一个。",
                )
            stored = pool[0]
        if stored.get("status") == "archived":
            raise ProposalConflict("strategy_archived", "这张策略已归档，不能解读。")
        eval_symbol = str(stored["symbol"] or "")
        if eval_symbol == "*":
            if not symbol:
                raise ProposalConflict(
                    "strategy_symbol_required",
                    "这张策略没有固定合约，请说明要看哪个合约（或先打开那个合约的图表）。",
                )
            eval_symbol = symbol
        definition = stored["definition"]
        if chart_timeframe_seconds is not None:
            from .strategy_chart_period import bind_definition
            try:
                definition = bind_definition(definition, int(chart_timeframe_seconds))
            except ValueError as error:
                raise ProposalConflict(getattr(error, "code", "chart_timeframe_invalid"), str(error)) from error
        strategy_provider, strategy_orders = self._view(
            self._account_by_id(stored.get("tradingAccountId")))
        if definition.get("schemaVersion") == 2:
            from .strategy_rules import evaluate_plan
            try:
                result = evaluate_plan(strategy_provider, definition, eval_symbol)
            except ValueError as error:
                raise ProposalConflict(getattr(error, "code", str(error)), str(error)) from error
            evaluation: dict[str, Any] = {
                **result, "turnId": turn_id, "strategyId": stored["strategyId"],
                "name": stored["name"], "symbol": eval_symbol, "signal": "wait",
                "dataStatus": "normal", "definitionDigest": stored["definitionDigest"],
            }
        else:
            if eval_symbol != stored["symbol"]:
                raise ProposalConflict(
                    "strategy_symbol_unsupported",
                    "这张旧版策略绑定在它自己的合约上，请在那个合约上解读。",
                )
            evaluation = self.evaluate_strategy_draft(
                strategy_id=stored["strategyId"], user_id=user_id, turn_id=turn_id
            )
        market = strategy_provider.market_snapshot(turn_id, eval_symbol)
        account = strategy_provider.account_summary(turn_id)
        positions = strategy_provider.positions()
        activity = strategy_provider.account_activity()
        if (
            not isinstance(market, dict) or not isinstance(account, dict)
            or not isinstance(positions, list) or not isinstance(activity, dict)
        ):
            raise ProposalConflict("strategy_intent_context_invalid")
        intent = build_strategy_intent(
            strategy={**stored, "definition": definition, "symbol": eval_symbol},
            evaluation=evaluation,
            market=market,
            account=account,
            positions=positions,
            activity=activity,
            user_id=user_id,
            policies=self.policies,
            instruments=self.instruments,
            now=strategy_orders.trading_now() if strategy_orders else None,
            **self._engine_inputs({**stored, "definition": definition}, eval_symbol, strategy_provider,
                                  strategy_orders, account),
        )
        report = self.store.latest_strategy_validation_report(
            stored["strategyId"], user_id=user_id
        )
        validated = bool(
            report is not None and report["status"] == "passed"
            and report["strategyDigest"] == stored["definitionDigest"]
        )
        from .strategy_intents import signal_action_label, signal_annotation_items
        position = intent["positionIntent"]
        adjusted = intent["riskAdjustedIntent"]
        action = str(position["action"])
        label = signal_action_label(intent, action)
        annotations = signal_annotation_items(
            action=action, evaluation=evaluation, intent=intent, market=market
        )
        conditions = list(evaluation.get("conditions") or [])
        conditions.extend({
            "label": check["label"],
            "status": "passed" if check["passed"] else "failed",
            "detail": "已通过" if check["passed"] else "未通过，停止扩大风险",
        } for check in adjusted["checks"])
        reference = market.get("price")
        if action == "hold":
            passed = sum(1 for c in conditions if c.get("status") == "passed")
            summary = f"{stored['name']}：条件尚未同时满足（{passed}/{len(conditions)}），当前观望。"
        else:
            summary = (
                f"{stored['name']}：策略信号为{label}（{int(position['requestedVolume'])} 手意图，"
                f"参考价 {reference}）。"
            )
        trigger = intent["signalSnapshot"].get("entryTrigger")
        if trigger and action in {"open_long", "open_short"} and not trigger.get("reached"):
            from .strategy_monitor import trigger_words
            summary += f"这是盘中触价入场：挂价 {trigger['price']}，价格{trigger_words(trigger, action)}才开仓，只在当前这根K线内有效。"
        if adjusted["status"] == "blocked" and action in {"open_long", "open_short"}:
            summary += "开仓意图当前被风险检查拦截，只解读不下单。"
        if not validated:
            summary += "策略未通过验证，解读仅供参考。"
        return {
            "turnId": turn_id,
            "strategyId": stored["strategyId"],
            "name": stored["name"],
            "symbol": eval_symbol,
            "status": stored["status"],
            "validated": validated,
            "conclusion": label,
            "action": action,
            "reason": position["reason"],
            "signal": intent["signalSnapshot"]["signal"],
            "evaluatedBarTime": evaluation["evaluatedBarTime"],
            "dataStatus": evaluation["dataStatus"],
            "referencePrice": reference,
            "stopPrice": adjusted["stopPrice"],
            "takeProfitPrice": adjusted["takeProfitPrice"],
            "volume": int(position["requestedVolume"]),
            "riskStatus": adjusted["status"],
            "riskReason": adjusted["reason"],
            "entryTrigger": trigger,
            "conditions": conditions,
            "annotations": annotations,
            "summary": summary,
            "executionMode": "simulation_only",
            "liveTrading": "disabled",
        }

    def open_fee_basis(
        self, *, user_id: str, symbol: str, price: float | None, multiplier: float | None,
        market_per_lot: float | None,
    ) -> dict[str, Any]:
        """每手开仓手续费的唯一估算入口（#770）：下单票估算、AI 的 estimate_order 与
        get_market_snapshot 的 contract.commissionPerLot 都读这里。实际生效费率（用户自定义 →
        交易所标准）按现价 × 乘数算；两边都拿不到才退回行情源每手值 `market_per_lot`
        （按成交额收费的品种它可能只有约 1/乘数，所以标 market_source）。返回
        schedule/effective 供平仓拆分复用，perLot/source 缺数据为 None 并带 missing 原因码。"""
        missing: list[str] = []
        schedule = self.fee_settings.get(user_id) if self.fee_settings is not None else None
        state_root = Path(self.fee_settings.root).parent if self.fee_settings is not None else None
        effective = (effective_fee_schedule(schedule, symbol, root=state_root)
                     if schedule is not None else None)
        per_lot: float | None = None
        source: str | None = None
        if schedule is None:
            missing.append("fee_schedule_unavailable")
        elif effective is None:
            missing.append("exchange_fees_unavailable:交易所标准暂不可用，按行情源每手手续费估算")
            if market_per_lot is None:
                missing.append("commission_per_lot_unavailable")
            else:
                per_lot, source = market_per_lot, "market_source"
        elif price is None:
            missing.append("price_unavailable")
        elif multiplier is None or multiplier <= 0:
            missing.append("volume_multiple_unavailable")
        else:
            per_lot = trade_fee(effective, symbol=symbol, price=price, volume=1,
                                multiplier=multiplier, offset="OPEN")
            source = "custom" if effective is schedule else "exchange_standard"
        return {"schedule": schedule, "effective": effective, "perLot": per_lot,
                "source": source, "missing": missing}

    def contract_fee(self, *, user_id: str, market: dict[str, Any]) -> dict[str, Any]:
        """行情快照 contract 的每手手续费（AI 工具用）：与下单票同一入口 `open_fee_basis`。"""
        symbol = str(market.get("symbol") or "")
        basis = self.open_fee_basis(
            user_id=user_id, symbol=symbol,
            price=_estimate_number(market.get("price")),
            multiplier=_estimate_number(market.get("volumeMultiple")),
            market_per_lot=_estimate_number(market.get("commissionPerLot")))
        return {"commissionPerLot": basis["perLot"], "commissionSource": basis["source"],
                "commissionUnit": (ESTIMATE_UNITS_USD if foreign_product_of(symbol) else ESTIMATE_UNITS)["commissionPerLot"]}

    def order_estimate(
        self, *, user_id: str, symbol: str, side: str, offset: str, volume: int,
        price: float | None = None, provider: Any | None = None,
        position_bucket: str | None = None,
    ) -> dict[str, Any]:
        """下单前的“钱”估算（只读，不做任何校验或拦截）：开仓给保证金、手续费、
        可开手数与资金是否不够；平仓给预估释放的保证金与今昨拆分的手续费
        （`position_bucket` today/historical/all，all 时按账户平今平昨偏好拆；上期所/
        能源中心之外指令不分今昨，费用按“先昨后今”拆）——两者共用同一份实际可平手数
        分配（今昨各扣冻结），请求超出可平量时按可平量估算并在 `missing` 里写明。
        缺数据返回 null 加 `missing` 原因码，绝不虚构数字。"""
        data_provider = provider if provider is not None else self.data_provider
        if data_provider is None:
            raise RuntimeError("estimate_provider_unavailable")
        market = data_provider.market_snapshot("", symbol)
        resolved_symbol = str(market.get("symbol") or symbol)
        effective_price = _estimate_number(price) or _estimate_number(market.get("price"))
        multiplier = _estimate_number(market.get("volumeMultiple"))
        margin_per_lot = _estimate_number(market.get("marginPerLot"))
        commission_per_lot = _estimate_number(market.get("commissionPerLot"))
        if margin_per_lot is not None and margin_per_lot <= 0:
            margin_per_lot = None
        missing: list[str] = []

        account = data_provider.account_summary("")
        available: float | None = None
        if str(account.get("dataStatus")) in {"normal", "simulated", "stale"}:
            available = _estimate_number(account.get("available"))
        if available is None:
            missing.append("account_unavailable")

        fee: float | None = None
        fee_breakdown: dict[str, dict[str, float]] | None = None
        # 每手开仓手续费：可开手数的分母（per_lot 与 notional 两种费率对手数都是线性的）。
        # None = 手续费未知（原因码在 missing 里），不能当 0 算进可开手数。
        basis = self.open_fee_basis(user_id=user_id, symbol=resolved_symbol, price=effective_price,
                                    multiplier=multiplier, market_per_lot=commission_per_lot)
        schedule, effective = basis["schedule"], basis["effective"]
        fee_per_lot: float | None = basis["perLot"]
        missing.extend(basis["missing"])
        if offset == "open" and fee_per_lot is not None:
            fee = (round(fee_per_lot * volume, 2) if effective is None else trade_fee(
                effective, symbol=resolved_symbol, price=effective_price, volume=volume,
                multiplier=multiplier, offset="OPEN"))

        margin: float | None = None
        if margin_per_lot is None:
            missing.append("margin_per_lot_unavailable")
        else:
            # 开仓占用保证金；平仓不新增占用（释放的保证金看 releasedMargin）。
            margin = round(margin_per_lot * volume, 2) if offset == "open" else 0.0

        max_open: int | None = None
        if offset == "open" and margin_per_lot and available is not None and fee_per_lot is not None:
            # 可开手数与输入手数无关：可用资金 ÷（每手保证金 + 每手手续费）。
            max_open = max(0, math.floor(available / (margin_per_lot + fee_per_lot)))

        released: float | None = None
        if offset == "close":
            try:
                positions = data_provider.positions()
            except RuntimeError:
                positions = []
                missing.append("positions_unavailable")
            # 卖平多、买平空：先算这次实际可平的手数分配（今昨各扣冻结），手续费与
            # 释放保证金共用这一份；请求超出可平量时按实际可平量估算并说明，不拒绝。
            wanted = "long" if side == "sell" else "short"
            match = next(
                (item for item in positions
                 if (str(item.get("symbol") or "").upper() == resolved_symbol.upper()
                     or str(item.get("symbol") or "").split(".")[-1].upper() == resolved_symbol.upper())
                 and str(item.get("direction")) == wanted), None,
            )
            held = int(match.get("volume") or 0) if match else 0
            position_margin = _estimate_number(match.get("margin")) if match else None
            preference = close_preference(self.store, user_id)
            if held <= 0 or position_margin is None:
                missing.append("position_margin_unavailable")
            else:
                today_closable, historical_closable = closable_buckets(match)
                if resolved_symbol.split(".", 1)[0].upper() in CLOSE_TODAY_EXCHANGES:
                    allocation = close_allocation(match, volume=volume, preference=preference,
                                                   bucket=position_bucket)
                    usable = allocation["today"] + allocation["historical"]
                    if position_bucket == "historical":
                        note = f"昨仓只有 {historical_closable} 手可平"
                    elif position_bucket == "today":
                        note = f"今仓只有 {today_closable} 手可平"
                    else:
                        note = (f"今仓 {today_closable} 手、昨仓 {historical_closable} 手可平，"
                                f"合计只有 {today_closable + historical_closable} 手")
                else:
                    # 非上期所的平仓指令不分今昨：可平量与手续费同一份“先昨后今”拆分
                    # （#711；今昨与冻结字段都缺失时按全仓估算，见拆分函数自身）。
                    split = close_yesterday_first_split(match, volume=volume)
                    closable = split["today"] + split["historical"]
                    usable = min(volume, closable)
                    note = f"持仓只有 {closable} 手可平"
                if usable < volume:
                    missing.append(f"close_volume_exceeds_closable:{note}，按 {usable} 手估算")
                released = round(position_margin * usable / held, 2)
            if match is not None and schedule is not None:
                fee, fee_breakdown = _close_fee_estimate(
                    effective, symbol=resolved_symbol, price=effective_price, volume=volume,
                    multiplier=multiplier, position=match, per_lot=commission_per_lot,
                    preference=preference, bucket=position_bucket)

        return {
            "schemaVersion": 1,
            "symbol": resolved_symbol,
            "side": side, "offset": offset, "volume": volume,
            "price": effective_price,
            "marginPerLot": margin_per_lot,
            # 每手开仓手续费 = 下单票“手续费约”同一个数（#770）：实际生效费率（自定义 →
            # 交易所标准）按现价算；两边都没有才是行情源每手参考值，来源见 commissionSource。
            "commissionPerLot": fee_per_lot,
            "commissionSource": basis["source"],
            "units": ESTIMATE_UNITS_USD if account_currency(getattr(data_provider, "account_id", None)) == "USD"
            else ESTIMATE_UNITS,
            "margin": margin,
            "fee": fee,
            "feeBreakdown": fee_breakdown,
            "available": available,
            "maxOpenVolume": max_open,
            "insufficient": bool(max_open is not None and volume > max_open),
            "releasedMargin": released,
            "basis": ESTIMATE_BASIS,
            "missing": missing,
            "dataStatus": market.get("dataStatus"),
        }

    def create_trade(
        self, session_id: str, turn_id: str, *, symbol: str, side: str,
        volume: int, price_intent: str, reason: str, limit_price: float | None = None,
        position_bucket: str | None = None, readiness: dict[str, Any] | None = None,
        target_position: dict[str, Any] | None = None,
        strategy_source: dict[str, Any] | None = None, origin: str | None = None,
        protection: dict[str, Any] | None = None,
        remaining_protection: dict[str, Any] | None = None,
        account_id: str | None = None, intent_source: str | None = None,
    ) -> dict[str, Any]:
        session_account = self._card_account(session_id, account_id)
        provider, order_service = self._view(session_account)
        if order_service is None:
            raise ProposalConflict("simulation_order_service_unavailable")
        user_id, proposal_id = self._user(session_id), str(uuid4())
        payload: dict[str, Any] = {
            "turnId": turn_id, "proposalId": proposal_id, "version": 1,
            **order_service.build_candidate(
                user_id=user_id, symbol=symbol, side=side, volume=volume,
                price_intent=price_intent, limit_price=limit_price,
                position_bucket=position_bucket,
                reason=reason,
            ),
            "updatedAt": utc_now(),
            "environment": "simulation", "status": "draft",
            **card_account_fields(order_service),
        }
        if protection is not None and any(value is not None for value in protection.values()):
            # 开仓顺带保护 (2026-10-02): validated against the card's own price now; the fill
            # then arms them through protection_json (a close card's own levels are remainingProtection).
            if not side.endswith("_open"):
                raise ProposalConflict("protection_not_supported_on_close",
                                       "只有开仓能顺带挂止损/止盈；平仓后剩下仓位的止损止盈用 remainingProtection")
            card_protection = _card_protection_payload(protection, payload, side=side)
            payload["protection"] = card_protection
            if card_protection.get("stopLossPrice"):
                # The user's own stop replaces the budget-derived 风控参考价 on the card.
                payload["stopPrice"] = float(Decimal(card_protection["stopLossPrice"]))
        if remaining_protection:
            # 合并卡 (第四批): a close card's second step — the remaining position's stop / take.
            plan = self._remaining_protection(
                remaining_protection, user_id=user_id, session_account=session_account, order_service=order_service,
                side=side, symbol=str(payload["symbol"]), close_volume=int(payload["volume"]))
            if plan is not None:
                payload["remainingProtection"] = plan
        if target_position is not None:
            # The 目标持仓 annotation (summary / 第几步 / 是否还有下一段), so the card keeps it across
            # re-publishes and the client renders the plan line.
            payload["targetPosition"] = target_position
        if strategy_source is not None:
            # 出卡确认 (2026-09-25): a monitored strategy's signal as a card ({strategyId, name,
            # attemptId}). Confirming it is the ordinary confirm; the fill then reads as the strategy's.
            payload["strategySource"] = strategy_source
        if origin is not None:
            # "ai": the conversation's AI proposed this card (its tool, not the ticket or the REST route);
            # the fill then reads as 「AI 建议」 with the card's reason in the trade review.
            payload["origin"] = origin
        if intent_source is not None:
            # 意图重构第 3 步: who read the order from the words — "parser" (the fast rules were sure) or "model"
            # (the trading AI's propose_order). Only a parser card may be confirmed for the user (语音免口头确认).
            payload["intentSource"] = intent_source
        if readiness is not None:
            # The readiness gate the AI tool ran for this card (market / bars / instrument / account /
            # positions / orders fresh, price inside the limits): kept on the card and shown in its
            # 详情 through the 风控 line, which is the card's only free-text detail row.
            payload["readiness"] = readiness
            summary = str(readiness.get("summary") or "").strip()
            # A failed risk check keeps its own reason alone: "就绪检查已通过；<failure>" would read as a
            # contradiction on the card.
            if summary and payload["riskCheck"].get("passed") is True:
                payload["riskCheck"] = {**payload["riskCheck"],
                                        "reason": f"{summary}；{payload['riskCheck'].get('reason') or ''}".rstrip("；")}
        if side.endswith("_open"):
            # 开仓卡随卡带“钱”的估算（保证金约 / 手续费约 / 可开手数），只展示，
            # 不参与校验与确认；估算缺数据不影响出卡，卡片就不带这一行。
            try:
                payload["estimate"] = self.order_estimate(
                    user_id=user_id, symbol=symbol, side=side.split("_")[0], offset="open",
                    volume=volume, price=limit_price, provider=provider,
                )
            except (KeyError, RuntimeError, ValueError):
                pass
        payload.update(self._account_note(session_id, session_account))
        approval_id = str(uuid4()) if payload["riskCheck"]["passed"] else None
        payload["status"] = "approval_required" if approval_id else "risk_blocked"
        if approval_id:
            payload["approvalId"] = approval_id
        self.store.create(
            proposal_id=proposal_id, session_id=session_id, proposal_type="trade",
            status=payload["status"], payload=payload, user_id=user_id, version=1,
            approval_id=approval_id, approval_expires_at=utc_after(600) if approval_id else None,
            trading_account_id=session_account.account_id if session_account is not None else None,
        )
        return payload

    def create_close_batch(
        self, session_id: str, turn_id: str, *, reason: str,
        symbols: list[str] | None = None, direction: str | None = None,
        max_volume: int | None = None, target_position: dict[str, Any] | None = None,
        remaining_protection: dict[str, Any] | None = None,
        price_intent: str = "aggressive", limit_price: float | None = None,
        account_id: str | None = None, intent_source: str | None = None,
    ) -> dict[str, Any]:
        """One card that closes every open position (optionally only some symbols or one direction):
        one confirmation, then the orders are sent one by one and each reports back. SHFE/INE
        positions split into today/historical the way the manual smart close does.
        max_volume caps the total volume across the split (the auto-bucket close of one
        request: today's leg + yesterday's leg together equal the volume the user asked for).
        Every order is priced by `price_intent` (全平 defaults to the 对手价) or the user's own
        `limit_price`: a 今+昨 split close or a 目标持仓 leg keeps the price the user asked for (#941)."""
        session_account = self._card_account(session_id, account_id)
        _, order_service = self._view(session_account)
        if order_service is None:
            raise ProposalConflict("simulation_order_service_unavailable")
        user_id, proposal_id = self._user(session_id), str(uuid4())
        preference = close_preference(self.store, user_id)
        wanted = {item.strip().upper() for item in (symbols or []) if item and item.strip()}
        orders: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for position in order_service.data_provider.positions():
            symbol = str(position.get("symbol") or "")
            side_direction = str(position.get("direction") or "")
            if wanted and symbol.upper() not in wanted and symbol.split(".", 1)[-1].upper() not in wanted:
                continue
            if direction in {"long", "short"} and side_direction != direction:
                continue
            if side_direction not in {"long", "short"}:
                continue
            side = "sell_close" if side_direction == "long" else "buy_close"
            exchange = symbol.split(".", 1)[0].upper()
            if exchange in CLOSE_TODAY_EXCHANGES:
                # 限量时按用户偏好分今昨（与估算、AI 平仓候选同一份分配）；全平则两侧取满。
                parts = close_allocation(
                    position, preference=preference, bucket=None,
                    volume=sum(closable_buckets(position)) if max_volume is None else max_volume)
                buckets = [("today", parts["today"]), ("historical", parts["historical"])]
            else:
                # 非上期所的平仓指令不分今昨，仍是一张 CLOSE；可平量与估算共用同一份
                # “先昨后今”拆分（#711），今仓那段实扣按平今费率（sidecar 撮合同一顺序）。
                full = close_yesterday_first_split(position, volume=int(position.get("volume") or 0))
                closable = full["today"] + full["historical"]
                buckets = [("all", closable if max_volume is None else min(closable, max_volume))]
            for bucket, volume in buckets:
                if volume <= 0:
                    continue
                try:
                    candidate = order_service.build_candidate(
                        user_id=user_id, symbol=symbol, side=side, volume=volume, price_intent=price_intent,
                        limit_price=limit_price, position_bucket=bucket, reason=reason)
                except Exception as error:  # one contract without a fresh quote must not hide the others
                    skipped.append({"symbol": symbol, "side": side, "volume": volume, "positionBucket": bucket, "reason": str(error)})
                    continue
                orders.append(candidate)
        if not orders:
            # The refusal carries the skipped contracts' own codes (休市/停更的行情、涨跌停缺数据):
            # without them the tool layer could only say a bare code, the model retried the call
            # three rounds and the turn ended on raw DSML markup (线上 #721).
            raise CloseBatchRefused(
                "no_position_to_close" if not skipped else "close_batch_no_order_available", skipped)
        payload: dict[str, Any] = {
            "turnId": turn_id, "proposalId": proposal_id, "version": 1, "environment": "simulation",
            "reason": reason.strip(), "orders": orders, "skipped": skipped,
            "totalOrders": len(orders), "totalVolume": sum(int(o["volume"]) for o in orders),
            "riskCheck": {"passed": all(o.get("riskCheck", {}).get("passed") for o in orders), "reason": "仅减少现有持仓；每张发送前再核对持仓和冻结量。"},
            "updatedAt": utc_now(), "status": "draft",
            **card_account_fields(order_service),
            # How the card's prices were set, so the card says so (#942 review): 对手价 / 排队价 / 最新价,
            # or the user's own limit price.
            "priceIntent": price_intent, "userLimitPrice": limit_price is not None,
        }
        if remaining_protection:
            # 合并卡 on a 今+昨 split close: only when the card closes one contract in one direction.
            legs = {(str(order["symbol"]), str(order["side"])) for order in orders}
            if len(legs) != 1:
                raise ProposalInvalid("remaining_protection_single_position",
                                      "剩余仓位的止损/止盈只能跟一个合约、一个方向的平仓一起带")
            (leg_symbol, leg_side), = legs
            plan = self._remaining_protection(
                remaining_protection, user_id=user_id, session_account=session_account, order_service=order_service,
                side=leg_side, symbol=leg_symbol, close_volume=int(payload["totalVolume"]))
            if plan is not None:
                payload["remainingProtection"] = plan
        if target_position is not None:
            # Same 目标持仓 annotation as the single-order card (this card is the plan's first leg).
            payload["targetPosition"] = target_position
        if intent_source is not None:
            payload["intentSource"] = intent_source  # parser / model, as on the single-order card
        payload.update(self._account_note(session_id, session_account))
        approval_id = str(uuid4()) if payload["riskCheck"]["passed"] else None
        payload["status"] = "approval_required" if approval_id else "risk_blocked"
        if approval_id:
            payload["approvalId"] = approval_id
        self.store.create(
            proposal_id=proposal_id, session_id=session_id, proposal_type="close_batch",
            status=payload["status"], payload=payload, user_id=user_id, version=1,
            approval_id=approval_id, approval_expires_at=utc_after(600) if approval_id else None,
            trading_account_id=session_account.account_id if session_account is not None else None,
        )
        return payload

    def create_spread(
        self, session_id: str, turn_id: str, *, spec: Any, action: str, units: int,
        reason: str, strategy_source: dict[str, Any] | None = None,
        current_spread: float | None = None, entry_spread: float | None = None,
        exit_reason: str | None = None, account_id: str | None = None,
    ) -> dict[str, Any]:
        """出卡确认 (价差): ONE card for both legs of one spread action — the spread definition, the
        current spread, one unit's composition and the spread stop / take. The card is atomic: if any
        leg's candidate cannot be built, nothing is offered. Confirming is the ordinary confirm route,
        which hands both legs to the spread executor together; the legs' real results come back on
        the card through the executor's own readback, never a claim that a leg was sent when it was
        not. `account_id`: the strategy instance's own account (#1011), else the session's."""
        from .spread_execution import spread_leg_plans
        from .strategy_spread import spread_legs_words, spread_words
        session_account = self._card_account(session_id, account_id)
        _, order_service = self._view(session_account)
        if order_service is None:
            raise ProposalConflict("simulation_order_service_unavailable")
        user_id, proposal_id = self._user(session_id), str(uuid4())
        orders = [
            # positionBucket always present (null for an open leg): the client's card schema reads it,
            # and a leg without the key dropped the whole card (线上 09-29).
            {**order_service.build_candidate(
                user_id=user_id, symbol=item["symbol"], side=item["side"], volume=item["volume"],
                price_intent="aggressive", position_bucket=item["bucket"], reason=reason),
             "positionBucket": item["bucket"]}
            for item in spread_leg_plans(spec, action, units, order_service.data_provider.positions(),
                                         preference=close_preference(self.store, user_id))
        ]
        if not orders:
            raise ProposalConflict("no_position_to_close", "没有可平的持仓")
        payload: dict[str, Any] = {
            "turnId": turn_id, "proposalId": proposal_id, "version": 1, "environment": "simulation",
            "reason": reason.strip(),
            "spread": {"kind": spec.kind,
                       "legs": [{"symbol": leg.symbol, "lots": leg.lots, "weight": leg.weight}
                                for leg in spec.legs],
                       "stopLoss": spec.stopLoss, "takeProfit": spec.takeProfit},
            "spreadWords": spread_words(spec), "legsWords": spread_legs_words(spec, action, max(1, units)),
            "action": action, "units": max(1, units),
            "orders": orders, "totalOrders": len(orders),
            "totalVolume": sum(int(o["volume"]) for o in orders),
            "riskCheck": {"passed": all(o.get("riskCheck", {}).get("passed") for o in orders),
                          "reason": "一份价差的两腿同时以对手价发送；瘸腿自动撤单补一次，仍不成回退并暂停策略。"},
            "updatedAt": utc_now(), "status": "draft",
            **card_account_fields(order_service),
        }
        # Always present (null when unknown): the client's card schema reads these keys, and an event
        # missing one was dropped whole — the card never showed (线上 09-29).
        payload["currentSpread"] = float(current_spread) if current_spread is not None else None
        payload["entrySpread"] = float(entry_spread) if entry_spread is not None else None
        payload["exitReason"] = exit_reason
        if strategy_source is not None:
            payload["strategySource"] = strategy_source
        payload.update(self._account_note(session_id, session_account))
        approval_id = str(uuid4()) if payload["riskCheck"]["passed"] else None
        payload["status"] = "approval_required" if approval_id else "risk_blocked"
        if approval_id:
            payload["approvalId"] = approval_id
        self.store.create(
            proposal_id=proposal_id, session_id=session_id, proposal_type="spread",
            status=payload["status"], payload=payload, user_id=user_id, version=1,
            approval_id=approval_id, approval_expires_at=utc_after(600) if approval_id else None,
            trading_account_id=session_account.account_id if session_account is not None else None,
        )
        return payload

    def _remaining_protection(
        self, spec: dict[str, Any], *, user_id: str, session_account: AccountServices | None,
        order_service: SimulationOrderService, side: str, symbol: str, close_volume: int,
    ) -> dict[str, Any] | None:
        """The close card's `remainingProtection` (card_protection.plan_remaining_protection): checked
        against the account's positions, a fresh quote and the position's current user-set protection."""
        from .card_protection import RemainingProtectionInvalid, plan_remaining_protection
        account = session_account
        if account is None and self.accounts is not None:
            account = self.accounts.default
        autonomy = getattr(account, "autonomy", None)
        direction = "long" if side == "sell_close" else "short"
        try:
            rows = (autonomy.manual_protection_snapshot_rows(user_id=user_id,
                                                            symbol=symbol, direction=direction)
                    if autonomy is not None and side.endswith("_close") else [])
            positions = order_service.data_provider.positions() if side.endswith("_close") else []
            market = order_service.data_provider.market_snapshot("", symbol)
            plan = plan_remaining_protection(spec, side=side, symbol=symbol, close_volume=close_volume,
                                             positions=positions, market=market, rows=rows)
            note = (autonomy.strategy_protection_note(user_id=user_id, symbol=symbol, direction=direction)
                    if plan is not None and autonomy is not None else None)
            return {**plan, "strategyNote": note} if plan is not None and note else plan
        except RemainingProtectionInvalid as error:
            raise ProposalInvalid(error.code, error.message) from None

    def _validate_owner(self, record: dict[str, Any], *, user_id: str) -> None:
        if record["userId"] != user_id:
            raise KeyError(record["id"])

    # -- batch close: the card's state is its children's facts, not its approval ----------

    def close_batch_orders(
        self, payload: dict[str, Any], *, user_id: str,
        connection: sqlite3.Connection | None = None,
        simulation_orders: Any = _UNSET,
    ) -> list[dict[str, Any]]:
        """Every child order of a confirmed batch, read back from its persisted link.
        A child with no link was never prepared, so it was certainly not sent."""
        order_service = self.simulation_orders if simulation_orders is _UNSET else simulation_orders
        facts: list[dict[str, Any]] = []
        for order in payload.get("orders") or []:
            name, symbol = order.get("name"), order.get("symbol")
            client_order_id = order.get("clientOrderId")
            fact: dict[str, Any] | None = None
            if client_order_id and order_service is not None:
                try:
                    fact = order_service.get(
                        str(client_order_id), user_id=user_id, connection=connection)
                except KeyError:
                    fact = None
            if fact is None:
                fact = {"clientOrderId": client_order_id, "displayStatus": "待发送",
                        "submitPhase": "PREPARED", "lastReadError": "order_not_prepared"}
            facts.append({**fact, "name": name, "symbol": symbol})
        return facts

    def close_batch_view(
        self, payload: dict[str, Any], *, user_id: str,
        connection: sqlite3.Connection | None = None,
        simulation_orders: Any = _UNSET,
    ) -> dict[str, Any]:
        """The confirmation answer for a batch: the card payload plus each child order's
        current fact and their roll-up. An idempotent replay reads the same facts back,
        so a retry never reports "sent" for orders this Runtime never sent."""
        orders = self.close_batch_orders(payload, user_id=user_id, connection=connection,
                                         simulation_orders=simulation_orders)
        return {**payload, "simulationOrders": orders, "execution": close_batch_execution(orders)}

    def close_batch_state(self, record: dict[str, Any]) -> str:
        """One line about a batch card for the model's system status, built from the
        child orders. Before confirmation there are none, so the card status stands."""
        payload = record.get("payload") or {}
        if record.get("status") != "candidate_recorded" or not payload.get("orders"):
            return ""
        try:
            orders = self.close_batch_orders(
                payload, user_id=str(record["userId"]),
                simulation_orders=self._view(
                    self._account_by_id(record.get("tradingAccountId")))[1])
        except Exception:
            return ""
        return str(close_batch_execution(orders)["text"])

    async def adjust_trade_risk(
        self, proposal_id: str, *, user_id: str, proposal_version: int, idempotency_key: str,
    ) -> dict[str, Any]:
        action = "adjust-risk"
        with self.store.transaction() as connection:
            cached = self.store.get_idempotent(user_id=user_id, key=idempotency_key, action=action,
                                               proposal_id=proposal_id, connection=connection)
            if cached is not None:
                return cached
            record = self.store.get(proposal_id, connection=connection)
            self._validate_owner(record, user_id=user_id)
            if record["proposalType"] != "trade":
                raise ProposalConflict("wrong_proposal_type")
            if record["payload"].get("offset", "OPEN") != "OPEN":
                raise ProposalConflict("close_risk_adjustment_not_supported")
            if record["status"] != "risk_blocked":
                raise ProposalConflict("invalid_state_transition")
            if record["version"] != proposal_version:
                raise ProposalConflict("stale_proposal_version")
            payload = record["payload"]
            policy = self.policies.get(user_id=user_id, symbol=payload["symbol"])
            spec = instrument_spec(payload, self.instruments)
            if policy.max_loss_per_trade is None:
                raise ProposalConflict("risk_adjustment_not_required")
            distance = policy.max_loss_per_trade / (spec.contract_multiplier * decimal_value(payload["volume"]))
            if payload["side"] == "buy_open":
                stop_price = ceil_to_tick(decimal_value(payload["limitPrice"]) - distance, spec.price_tick)
            else:
                stop_price = floor_to_tick(decimal_value(payload["limitPrice"]) + distance, spec.price_tick)
            payload["stopPrice"] = float(stop_price.quantize(spec.price_tick))
            payload["updatedAt"], payload["version"] = utc_now(), record["version"] + 1
            risk_provider, risk_orders = self._view(self._account_by_id(record.get("tradingAccountId")))
            payload["riskCheck"] = self._risk(payload, user_id, provider=risk_provider,
                                              simulation_orders=risk_orders)
            if not payload["riskCheck"]["passed"]:
                raise RiskViolation(proposal_id, payload["riskCheck"])
            approval_id, expires_at = str(uuid4()), utc_after(600)
            payload.update({"status": "approval_required", "approvalId": approval_id})
            updated = self.store.update(
                proposal_id, status="approval_required", payload=payload, version=payload["version"],
                approval_id=approval_id, approval_expires_at=expires_at, connection=connection,
            )["payload"]
            self.store.audit(proposal_id, action, {"status": "approval_required", "version": payload["version"]}, connection=connection)
            self.store.save_idempotent(user_id=user_id, key=idempotency_key, action=action,
                                       proposal_id=proposal_id, response=updated, connection=connection)
            self.broker.stage(record["sessionId"], kind="approval", name="proposal.trade",
                              payload=updated, connection=connection)
            self.broker.stage(
                record["sessionId"], kind="approval", name="trade.approval.required",
                payload={"proposalId": proposal_id, "approvalId": approval_id,
                         "proposalVersion": updated["version"], "proposalType": "trade"},
                connection=connection,
            )
        await self.broker.flush()
        return updated

    async def confirm(
        self, proposal_id: str, *, user_id: str, approval_id: str,
        proposal_version: int, idempotency_key: str,
    ) -> dict[str, Any]:
        action = "confirm"
        # 出卡确认: a strategy's open card carries the strategy's protection, built before the
        # transaction (it reads the market and the lease) and written onto the order link below,
        # before the order is dispatched — the same protection the monitor's own order gets.
        strategy_protection = await self._strategy_card_protection(proposal_id, user_id=user_id)
        # 开仓顺带保护: a manual open card with a user-set stop / take carries its own protection,
        # built the same way (the lease it fences on is the user-driven one).
        manual_protection = (None if strategy_protection is not None
                             else await self._manual_card_protection(proposal_id, user_id=user_id))
        # A spread card dispatches both legs the moment the confirmation commits, so a strategy that
        # is no longer monitoring is refused before it — the same 口径 as the single-contract card.
        await self._spread_card_guard(proposal_id, user_id=user_id)
        # 停止并平仓 (#654): the stop-close card re-checks its strategy's own position before
        # anything is prepared or sent — the stopped (ready) strategy's exit must not spend the
        # account's remaining manual lots.
        stop_close_cut = await self._stop_close_card_guard(proposal_id, user_id=user_id)
        with self.store.transaction() as connection:
            cached = self.store.get_idempotent(user_id=user_id, key=idempotency_key, action=action,
                                               proposal_id=proposal_id, connection=connection)
            if cached is not None:
                # A replayed answer re-reads its orders through the account the
                # proposal belongs to, exactly like the first answer did.
                cached_orders: SimulationOrderService | None = None
                if cached.get("orders") or cached.get("clientOrderId"):
                    _, cached_orders = self._view(self._account_by_id(
                        self.store.get(proposal_id, connection=connection).get("tradingAccountId")
                    ))
                if cached.get("orders") and cached.get("totalOrders"):
                    if str(self.store.get(proposal_id, connection=connection).get("proposalType")) == "spread":
                        # Same idempotency key again on a spread card: the legs' orders re-read from
                        # the attempt's leg rows (a confirmed card is terminal and immutable).
                        return self._spread_confirmed_view(cached, user_id=user_id,
                                                           connection=connection)
                    # Same idempotency key again: answer with what the child orders are
                    # now, exactly as the single-order path re-reads its own order.
                    return self.close_batch_view(cached, user_id=user_id, connection=connection,
                                                 simulation_orders=cached_orders)
                if cached.get("clientOrderId") and cached_orders is not None:
                    return {
                        **cached,
                        "simulationOrder": cached_orders.get(
                            str(cached["clientOrderId"]), user_id=user_id,
                        ),
                    }
                return cached
            record = self.store.get(proposal_id, connection=connection)
            self._validate_owner(record, user_id=user_id)
            # Everything this confirmation does — risk, prepare, dispatch — runs on
            # the trading account the proposal was opened on.
            confirm_provider, order_service = self._view(
                self._account_by_id(record.get("tradingAccountId")))
            if record["status"] == "risk_blocked":
                risk = self._risk(record["payload"], user_id, provider=confirm_provider,
                                  simulation_orders=order_service)
                raise RiskViolation(proposal_id, risk)
            if record["status"] != "approval_required":
                raise ProposalConflict("invalid_state_transition")
            if record["version"] != proposal_version:
                raise ProposalConflict("stale_proposal_version")
            if record["approvalId"] != approval_id:
                raise ProposalConflict("approval_mismatch")
            if record["approvalUsedAt"] is not None:
                raise ProposalConflict("approval_already_used")
            if not awaiting_confirmation(record):
                raise ProposalConflict("approval_expired", APPROVAL_EXPIRED_TEXT)
            payload, proposal_type = record["payload"], record["proposalType"]
            if proposal_type == "trade":
                if stop_close_cut is not None:
                    # The snapshot stays coherent; BEGIN IMMEDIATE rechecks persisted
                    # orders/fills before prepare reserves this leg for any Runtime process.
                    source = payload["strategySource"]
                    stop_close_cut["strategy"] = self.store.get_strategy_draft(
                        str(source["strategyId"]), user_id=user_id)
                    stop_close_cut["attempt"] = self.store.strategy_stop_close_attempt(
                        str(source["attemptId"]), user_id=user_id)
                    code = self._spread_stop_close_leg_code(**stop_close_cut)
                    if code is not None:
                        raise ProposalConflict(code, STOP_CLOSE_CARD_REFUSALS[code])
                if order_service is not None and order_service.enabled:
                    simulation_order = order_service.prepare(
                        proposal_id=proposal_id, user_id=user_id, payload=payload,
                        connection=connection,
                    )
                    payload["clientOrderId"] = simulation_order["clientOrderId"]
                    stop_close_source = payload.get("strategySource")
                    if (isinstance(stop_close_source, dict)
                            and str(stop_close_source.get("purpose") or "") == "stop_close"
                            and stop_close_source.get("attemptId")):
                        # 停止并平仓 (#654): the prepared order binds to its stop_close attempt
                        # inside this transaction — before dispatch — so a fill the dispatch
                        # window already observes still reads as this strategy's close and never
                        # falls through to the account's older (manual) protection rows. The
                        # route's post-confirm link then records the dispatched status.
                        from .strategy_monitor import _execution_status
                        prepared_status, prepared_detail, _ = _execution_status(
                            str(simulation_order.get("displayStatus") or ""))
                        self.store.confirm_strategy_card_order(
                            attempt_id=str(stop_close_source["attemptId"]), user_id=user_id,
                            proposal_id=proposal_id, client_order_id=str(payload["clientOrderId"]),
                            status=prepared_status, detail=f"停止平仓卡已确认：{prepared_detail}",
                            connection=connection)
                    if strategy_protection is not None:
                        connection.execute(
                            "UPDATE tqkq_sim_order_links SET protection_json = ? WHERE client_order_id = ?",
                            (json.dumps(strategy_protection), simulation_order["clientOrderId"]))
                    elif manual_protection is not None:
                        # The user's stop / take ride the order link; sync_protection arms them
                        # on the fill (partial fills arm their own volume; no fill arms nothing).
                        connection.execute(
                            "UPDATE tqkq_sim_order_links SET protection_json = ? WHERE client_order_id = ?",
                            (json.dumps(manual_protection), simulation_order["clientOrderId"]))
                    trade_action = "开仓" if payload.get("offset", "OPEN") == "OPEN" else "平仓"
                    account = "本站模拟" if confirm_provider.mode == "paper" else "快期模拟"
                    terminal, message = "candidate_recorded", f"已确认一张{account}限价{trade_action}委托。"
                    execution = "tqkq-simulation"
                else:
                    payload["riskCheck"] = self._risk(payload, user_id, provider=confirm_provider,
                                                      simulation_orders=order_service)
                    if not payload["riskCheck"]["passed"]:
                        raise RiskViolation(proposal_id, payload["riskCheck"])
                    terminal, message = (
                        "candidate_recorded", "模拟交易候选已记录；未连接模拟委托网关。"
                    )
                    execution = "none"
            elif proposal_type == "close_batch":
                if order_service is None or not order_service.enabled:
                    raise ProposalConflict("simulation_order_service_unavailable")
                # Every order is prepared inside this transaction: one stale quote or a position that
                # shrank since the card was made fails the whole confirmation, nothing is half-sent.
                # Each order gets its own child trade proposal (one order ↔ one proposal, as the order
                # link table requires), already confirmed by this card.
                for order in payload["orders"]:
                    child_id = str(uuid4())
                    order.update({"proposalId": child_id, "parentProposalId": proposal_id, "turnId": payload["turnId"],
                                  "version": 1, "environment": "simulation", "status": "candidate_recorded"})
                    self.store.create(proposal_id=child_id, session_id=record["sessionId"], proposal_type="trade",
                                      status="candidate_recorded", payload=order, user_id=user_id, connection=connection,
                                      trading_account_id=record.get("tradingAccountId"))
                    prepared = order_service.prepare(proposal_id=child_id, user_id=user_id, payload=order, connection=connection)
                    order["clientOrderId"] = prepared["clientOrderId"]
                    self.store.audit(child_id, "confirm", {"status": "candidate_recorded", "parentProposalId": proposal_id}, connection=connection)
                # `candidate_recorded` on the parent records the user's approval only.
                # What each order became is read back from its own link afterwards.
                account = "本站模拟" if confirm_provider.mode == "paper" else "快期模拟"
                terminal, message = "candidate_recorded", f"已确认 {len(payload['orders'])} 张{account}平仓委托，逐张发送。"
                execution = "tqkq-simulation"
            elif proposal_type == "spread":
                # The transaction records the user's approval only. Both legs dispatch after it
                # commits, through the spread executor (its own follow handles 瘸腿补单 / 回退 /
                # 暂停 — a fill wait never holds this transaction).
                terminal, message = "candidate_recorded", "已确认价差两腿委托，两腿同时以对手价发送。"
                execution = "tqkq-simulation"
            elif proposal_type == "risk_rule":
                rules = self._risk_rules(
                    allowed_symbols=[item["symbol"] for item in payload["allowedInstruments"]],
                    max_loss_per_trade=payload["maxLossPerTrade"],
                    max_daily_loss=payload["maxDailyLoss"],
                    no_new_positions_after=payload["noNewPositionsAfter"],
                )
                policy = self.store.apply_risk_policy(
                    user_id=user_id, expected_version=int(payload["basePolicyVersion"]),
                    proposal_id=proposal_id, rules=rules,
                    rules_digest=str(payload["rulesDigest"]), connection=connection,
                )
                payload["policyId"], payload["policyVersion"] = policy["policyId"], policy["version"]
                terminal, message = "risk_rules_applied", "风险规则已由 Runtime 确定性程序生效。"
                execution = "none"
            else:
                raise ProposalConflict("wrong_proposal_type")
            if (proposal_type in {"trade", "close_batch"} and execution == "tqkq-simulation"
                    and isinstance(payload.get("remainingProtection"), dict)):
                # 合并卡: the one confirmation also authorizes step ② — recorded here, in the same
                # transaction, and carried out by the relay once the close has finished (card_protection).
                payload["remainingProtection"] = {**payload["remainingProtection"], "status": "awaiting_close"}
                self.store.create_card_protection_step(
                    connection=connection, proposal_id=proposal_id, user_id=user_id,
                    trading_account_id=str(record.get("tradingAccountId") or self.store.default_trading_account_id),
                    session_id=str(record["sessionId"]),
                    symbol=str(payload["symbol"] if proposal_type == "trade" else payload["orders"][0]["symbol"]),
                    direction=str(payload["remainingProtection"]["direction"]), plan=payload["remainingProtection"])
            result = {"status": terminal, "message": message, "execution": execution}
            if payload.get("clientOrderId"):
                result["clientOrderId"] = payload["clientOrderId"]
            audit_id = self.store.audit(proposal_id, action, result, connection=connection)
            payload.update({"status": terminal, "auditId": audit_id})
            self.store.update(proposal_id, status=terminal, payload=payload, version=record["version"],
                              approval_id=record["approvalId"], approval_expires_at=record["approvalExpiresAt"],
                              approval_used_at=utc_now(), connection=connection)
            self.store.save_idempotent(user_id=user_id, key=idempotency_key, action=action,
                                       proposal_id=proposal_id, response=payload, connection=connection)
            if proposal_type == "close_batch":
                # Republish the card now that every child order has an id: that is what
                # lets the card show each order's real state, and lets it still do so
                # after a restart replays the conversation.
                self.broker.stage(record["sessionId"], kind="approval", name="proposal.close_batch",
                                  payload=payload, connection=connection)
            self.broker.stage(
                record["sessionId"], kind="result", name="proposal.result",
                payload={"proposalId": proposal_id, "status": terminal, "auditId": audit_id,
                         "message": message}, connection=connection,
            )
        await self.broker.flush()
        if proposal_type == "spread":
            # The account's own spread executor dispatches both legs together and follows them to a
            # definite outcome; its result carries each leg's real order (simulationOrders).
            return await self._launch_confirmed_spread(record, payload=payload, user_id=user_id)
        if proposal_type == "close_batch" and order_service is not None:
            for order in payload["orders"]:
                client_order_id = str(order["clientOrderId"])
                try:
                    await order_service.dispatch(client_order_id, user_id=user_id)
                except Exception as error:
                    # The next order still goes out. A refusal before the gateway call
                    # leaves this one PREPARED — not sent, and not "unknown" — and the
                    # reason is persisted with it. Nothing is ever re-sent from here.
                    order_service.note_dispatch_error(
                        client_order_id, user_id=user_id, reason=str(error))
            # A crash anywhere in this loop leaves the same durable truth behind: the
            # orders already sent keep their reports, the rest stay PREPARED for the
            # user's own continue/abandon. The answer is read back, never assembled.
            return self.close_batch_view(payload, user_id=user_id, simulation_orders=order_service)
        if (
            proposal_type == "trade" and payload.get("clientOrderId")
            and order_service is not None
        ):
            # 单笔确认与批量共用同一条持久事实 (#518): 派发抛错时先记下原因再抛，
            # 这单留下 PREPARED + 原因，由用户自己的继续/放弃接手，绝不被重发。
            try:
                order = await order_service.dispatch(
                    str(payload["clientOrderId"]), user_id=user_id,
                )
            except Exception as error:
                order_service.note_dispatch_error(
                    str(payload["clientOrderId"]), user_id=user_id, reason=str(error))
                raise
            return {**payload, "simulationOrder": order}
        return payload

    async def _launch_confirmed_spread(
        self, record: dict[str, Any], *, payload: dict[str, Any], user_id: str,
    ) -> dict[str, Any]:
        """The confirmed spread card's one creative act: hand both legs to the account's spread
        executor (the monitor service of the account the card was opened on), which dispatches them
        together and follows them to a definite outcome."""
        executor = self._spread_executor(record)
        if executor is None:
            raise ProposalConflict("spread_executor_unavailable")
        return await executor.launch_confirmed(
            payload=payload, user_id=user_id, session_id=str(record["sessionId"]))

    def _spread_confirmed_view(
        self, payload: dict[str, Any], *, user_id: str, connection: sqlite3.Connection | None = None,
    ) -> dict[str, Any]:
        """The confirmed spread card's answer (and its idempotent replay): each leg's order read back
        from the attempt's leg rows, with the same roll-up a close batch uses — 逐笔真实结果，永不冒充
        已发送."""
        try:
            record = self.store.get(str(payload.get("proposalId")), connection=connection)
        except KeyError:
            return {**payload}
        executor = self._spread_executor(record)
        if executor is None:
            return {**payload}
        return {**payload, **executor.confirmed_view(payload, user_id=user_id, connection=connection)}

    def _spread_executor(self, record: dict[str, Any]) -> Any:
        """The spread executor of the account the card was opened on (the default account's when the
        record carries none)."""
        account = self._account_by_id(record.get("tradingAccountId"))
        if account is None and self.accounts is not None:
            account = self.accounts.default
        monitor = getattr(account, "strategy_monitor", None)
        return getattr(monitor, "spread_execution", None) if monitor is not None else None

    async def _spread_card_guard(self, proposal_id: str, *, user_id: str) -> None:
        """A spread card only dispatches while its strategy is monitoring: a paused or stopped
        strategy's card is refused before the confirmation transaction, with the Chinese reason on
        the card — the same 口径 as a single-contract strategy card (nothing is prepared, nothing
        is sent; re-enabling monitoring lets a still-valid card be confirmed again)."""
        try:
            record = self.store.get(proposal_id)
        except KeyError:
            return
        payload = record["payload"]
        if (record["proposalType"] != "spread" or record["userId"] != user_id
                or record["status"] != "approval_required"
                or not isinstance(payload.get("strategySource"), dict)):
            return
        strategy_id = str(payload["strategySource"].get("strategyId") or "")
        if not strategy_id:
            return
        try:
            strategy = self.store.get_strategy_draft(strategy_id, user_id=user_id)
        except KeyError:
            strategy = None
        if strategy is not None and strategy["status"] == "monitoring":
            return
        reason = "策略已暂停或停止，这张价差卡没有发单。"
        payload = {**payload, "confirmBlocked": {"code": "strategy_not_monitoring", "reason": reason, "at": utc_now()}}
        # The same conditional write as _refuse_card (#700 review): a card another
        # confirmation already took keeps that winner's facts; the refusal only says why.
        if self.store.refuse_pending(proposal_id, expected_version=int(record["version"]),
                                     payload=payload, approval_id=record["approvalId"]):
            await self.broker.publish(str(record["sessionId"]), kind="approval", name="proposal.spread", payload=payload)
        raise ProposalConflict("strategy_not_monitoring", reason)

    async def _stop_close_card_guard(
        self, proposal_id: str, *, user_id: str,
    ) -> dict[str, Any] | None:
        """停止并平仓 (#654/#675): a stop-close card carries its strategy's attribution (a
        persisted stop_close attempt + intent snapshot). Confirming first proves that binding
        against what 停止并平仓 wrote — the attempt belongs to this user, strategy and account,
        issued this very card, and its intent still matches the card's symbol / direction /
        bucket / volume — never the strategySource text alone. A card that already dispatched
        reads back through the ordinary idempotent confirm (it is no longer
        approval_required); a card still awaiting confirmation whose attempt already holds an
        order is a binding inconsistency and is refused rather than re-prepared. It then
        re-checks that the strategy's own position still backs the card: gone, shrunk, rolled
        across today/historical, rolled across the trading day itself, re-edited (价差换代),
        an unresolved close already sitting on the same leg, or the card's lots already spent
        by another close since the stop's persisted fill baseline (#675) refuses the card
        before anything is prepared or sent; the account's remaining (manual or another
        strategy's) lots are never spent instead. A legacy stop-close card from before the
        attribution fix (no strategySource) is invalidated the same way — its ownership is not
        guessed from the reason, the session or the oldest protection row. 普通手动平仓卡
        (no strategySource, any other reason) and ordinary signal cards are not this guard's
        business."""
        cut: dict[str, Any] | None = None
        try:
            record = self.store.get(proposal_id)
        except KeyError:
            return None
        payload = record["payload"]
        if (record["proposalType"] != "trade" or record["userId"] != user_id
                or record["status"] != "approval_required"
                or payload.get("offset", "OPEN") == "OPEN"):
            return None
        source = payload.get("strategySource")
        reason = str(payload.get("reason") or "")
        if not isinstance(source, dict):
            if not (reason.startswith("停止策略「") and reason.endswith("」后平掉它的持仓")):
                return None  # an ordinary manual close card — not a stop-close card
            code = "stop_close_card_unattributed"
            await self._refuse_card(proposal_id, record, payload, code=code,
                                    reason=STOP_CLOSE_CARD_REFUSALS[code])
            raise ProposalConflict(code, STOP_CLOSE_CARD_REFUSALS[code])
        if str(source.get("purpose") or "") != "stop_close":
            return None
        account = self._account_by_id(record.get("tradingAccountId"))
        if account is None and self.accounts is not None:
            account = self.accounts.default
        symbol = str(payload.get("symbol") or "")
        volume = int(payload.get("volume") or 0)
        bucket = str(payload.get("positionBucket") or "all")
        direction = "short" if str(payload.get("side") or "") == "buy_close" else "long"
        code = None
        try:
            # 绑定核实 (#675): the card's attribution is proven against the persisted attempt
            # and its intent snapshot — user, strategy, trading account, the card it issued and
            # the leg target it was made from. The store's join already holds the intent to the
            # attempt's own user and strategy; here the payload restates them.
            attempt = self.store.strategy_stop_close_attempt(
                str(source.get("attemptId") or ""), user_id=user_id)
            intent = dict((attempt or {}).get("intentPayload") or {})
            if (attempt is None
                    or str(source.get("strategyId") or "") != attempt["strategyId"]
                    or attempt["tradingAccountId"] != account.account_id
                    or proposal_id not in attempt["cardProposalIds"]
                    or attempt["clientOrderIds"]
                    or str(intent.get("kind") or "") != "stop_close"
                    or str(intent.get("strategyId") or "") != attempt["strategyId"]
                    or str(intent.get("symbol") or "").casefold() != symbol.casefold()
                    or str(intent.get("direction") or "") != direction
                    or str(intent.get("positionBucket") or "all") != bucket
                    or int(intent.get("volume") or 0) != volume):
                # `clientOrderIds` on a card still awaiting confirmation is a binding
                # inconsistency: a confirmed card leaves approval_required and replays
                # through the idempotent read-back, so this shape must never prepare a
                # second order for the same attempt.
                code = "stop_close_card_unbound"
            else:
                try:
                    strategy = self.store.get_strategy_draft(str(source.get("strategyId") or ""),
                                                             user_id=user_id)
                except KeyError:
                    strategy = None
                if strategy is None:
                    code = "stop_close_position_gone"
                elif bool(((strategy.get("definition") or {}).get("plan") or {}).get("spread")):
                    # 一致的账户上下文 (#675): activity, positions and the account day are
                    # obtained inside ONE fresh provider snapshot and only these same-source
                    # values feed the leg check — a stale or half-initialized read is a
                    # refusal, never a guess, and the day is never paired with another
                    # snapshot's positions.
                    from .strategy_protection import require_current_activity
                    # 同一份采集事实 (#700 review): activity, positions and the account day
                    # come from ONE combined sidecar read (account=True) — never a fresh
                    # activity paired with the poll cache's positions. A sidecar without
                    # the combined read fails the prefetch and the card is refused below,
                    # never judged across two snapshots.
                    order_service = getattr(account, "simulation_orders", None)
                    local_fill_volumes = {
                        str(order["clientOrderId"]): int(order.get("filledVolume") or 0)
                        for order in (order_service.list_on_account() if order_service is not None else [])}
                    async with provider_read_snapshot(account.provider, refresh=True, account=True):
                        activity = account.provider.account_activity()
                        summary = account.provider.account_summary("")
                        positions = list(account.provider.positions() or [])
                    require_current_activity(activity)
                    cut = dict(strategy=strategy, intent=intent, attempt=attempt, account=account,
                               symbol=symbol, direction=direction, bucket=bucket, volume=volume,
                               activity=activity, summary=summary, positions=positions,
                               local_fill_volumes=local_fill_volumes)
                    code = self._spread_stop_close_leg_code(**cut)

                else:
                    monitor = getattr(account, "strategy_monitor", None)
                    held = (monitor.held_position(strategy, list(account.provider.positions() or []))
                            if monitor is not None else None)
                    if held is not None and held["direction"] not in {None, direction}:
                        # 换代: the strategy's own position flipped while the card was out.
                        code = "stop_close_position_changed"
                    else:
                        day = str(account.provider.account_summary("").get("tradingDay") or "")
                        with self.store.lock:
                            rows = self.store.connection.execute(
                                """SELECT * FROM ai_stop_orders WHERE user_id = ? AND trading_account_id = ?
                                   AND symbol = ? AND position_direction = ? AND remaining_volume > 0
                                   AND json_extract(strategy_metadata_json, '$.strategyId') = ?""",
                                (user_id, account.account_id, symbol, direction,
                                 strategy["strategyId"])).fetchall()
                        if not rows:
                            code = "stop_close_position_gone"
                        elif any(str(row["status"]) in {"TRIGGERING", "SUBMITTED", "UNKNOWN"} for row in rows):
                            code = "stop_close_protection_in_flight"
                        else:
                            # The same bucket read _close uses: entry day against the current trading day.
                            if symbol.split(".", 1)[0].upper() in {"SHFE", "INE"} and bucket in {"today", "historical"}:
                                available = sum(
                                    int(row["remaining_volume"]) for row in rows
                                    if ("today" if str(json.loads(row["strategy_metadata_json"]).get("entryTradingDay")) == day
                                        else "historical") == bucket)
                            else:
                                available = sum(int(row["remaining_volume"]) for row in rows)
                            if available < volume:
                                code = "stop_close_position_changed"
        except (KeyError, RuntimeError, TypeError, ValueError):
            code = "stop_close_position_unreadable"
        if code is not None:
            await self._refuse_card(proposal_id, record, payload, code=code,
                                    reason=STOP_CLOSE_CARD_REFUSALS[code])
            raise ProposalConflict(code, STOP_CLOSE_CARD_REFUSALS[code])
        return cut

    def _stop_close_in_flight_code(
        self, *, account: Any, symbol: str, direction: str, bucket: str,
        own_order_ids: set[str], local_fill_volumes: dict[str, int] | None = None,
    ) -> str | None:
        offsets = {"today": {"CLOSETODAY"}, "historical": {"CLOSE"}}.get(
            bucket, {"CLOSE", "CLOSETODAY"})
        close_direction = "SELL" if direction == "long" else "BUY"
        # 同腿在途/结果未知 (#675): any close of this leg and bucket that is not provably
        # finished — another stop group's card, an executor close / rollback leg, a manual
        # close card, or an armed protection that has triggered. Judged by the order's own
        # lifecycle (#700 review), never the 展示文案: 待发送 (PREPARED — an occupied id that
        # may yet be sent), 已受理 / 还在场上的部分成交 (ALIVE), 结果未知 / 身份异常 / 回报异常
        # all block; a settled order — including a FINISHED partial (2 手成 1、余量已撤) whose
        # display text says “部分成交” — stands aside, its fills read back through the position
        # and the fill check below. The account is read across every user it is granted to
        # (#699) — another authorized user's unresolved close on the same book occupies the
        # same lots; the internal read itself is never exposed.
        from .simulation_orders import ANOMALOUS_REMOTE_STATUSES, order_is_active
        order_service = getattr(account, "simulation_orders", None)
        for order in (order_service.list_on_account() if order_service is not None else []):
            request = order.get("request") or {}
            if (order.get("clientOrderId") in own_order_ids
                    or str(request.get("symbol") or "").casefold() != symbol.casefold()
                    or str(request.get("direction") or "") != close_direction
                    or str(request.get("offset") or "") not in offsets):
                continue
            if order_is_active(order) or order.get("remoteStatus") in ANOMALOUS_REMOTE_STATUSES:
                return "stop_close_protection_in_flight"
            # A sibling can fill before this transaction starts, before trade polling
            # persists it. Its report's increased fill already proves the cut consumed.
            if (local_fill_volumes is not None and int(order.get("filledVolume") or 0)
                    > local_fill_volumes.get(str(order["clientOrderId"]), 0)):
                return "stop_close_leg_consumed"
        with self.store.lock:
            protections = self.store.connection.execute(
                """SELECT 1 FROM ai_stop_orders WHERE trading_account_id = ? AND symbol = ?
                   AND position_direction = ? AND remaining_volume > 0
                   AND status IN ('TRIGGERING', 'SUBMITTED', 'UNKNOWN')""",
                (account.account_id, symbol, direction)).fetchone()
        if protections is not None:
            return "stop_close_protection_in_flight"
        return None

    def _spread_stop_close_leg_code(
        self, *, strategy: dict[str, Any], intent: dict[str, Any], attempt: dict[str, Any],
        account: Any, activity: dict[str, Any],
        summary: dict[str, Any], positions: list[dict[str, Any]],
        symbol: str, direction: str, bucket: str, volume: int,
        local_fill_volumes: dict[str, int] | None = None,
    ) -> str | None:
        """价差 (#675): the card is one leg of the stop's persisted target, so it is verified
        per leg — never against the whole-combo projection (spread_position), which the first
        leg's fill legitimately breaks. The strategy's definition must be the one the stop saw
        (换代), the account must still be on the trading day the card's buckets were read on
        (a new day's today lots are a new generation, not this card's target), the leg must
        still be closable in its own bucket under the same 可平 rule the order build uses,
        no unresolved close may sit on the same leg (this strategy's, another's, a manual
        card or a protection — judged through the order service's own normalized states), and
        the fills since the persisted baseline may not include another close of this leg and
        bucket (外部平掉再原数重开 reads as consumed — the card never spends the new lots).
        `activity`/`summary`/`positions` come from ONE provider snapshot; nothing here reads
        the provider again."""
        if (str(intent.get("strategyDigest") or "") != str(strategy.get("definitionDigest") or "")
                or str(intent.get("strategyVersion") or "") != str(strategy.get("version") or "")):
            return "stop_close_position_changed"
        from .trading_costs import account_fills
        # 交易日/代次 (#675): the card is scoped to the day its buckets were read on; a day
        # roll makes every today lot a new generation even when the volume still matches.
        day = str(summary.get("tradingDay") or "")
        datetime.strptime(day, "%Y-%m-%d")
        if summary.get("dataStatus") != "normal" or summary.get("isCached"):
            raise RuntimeError("strategy_account_not_current")
        if day != str(intent.get("tradingDay") or ""):
            return "stop_close_position_changed"
        positions = [item for item in positions
                     if isinstance(item, dict)
                     and str(item.get("symbol") or "").casefold() == symbol.casefold()
                     and item.get("direction") == direction]
        if sum(max(0, int(item.get("volume") or 0)) for item in positions) <= 0:
            return "stop_close_position_gone"
        if bucket == "all":
            available = sum(max(0, int(item.get("volume") or 0)) - int(item.get("frozenToday") or 0)
                            - int(item.get("frozenHistorical") or 0) for item in positions)
        else:
            available = sum(closable_buckets(item)[0 if bucket == "today" else 1] for item in positions)
        if available < volume:
            return "stop_close_position_changed"
        offsets = {"today": {"CLOSETODAY"}, "historical": {"CLOSE"}}.get(bucket, {"CLOSE", "CLOSETODAY"})
        close_direction = "SELL" if direction == "long" else "BUY"
        own_order_ids = set(attempt["clientOrderIds"])
        code = self._stop_close_in_flight_code(
            account=account, symbol=symbol, direction=direction, bucket=bucket,
            own_order_ids=own_order_ids, local_fill_volumes=local_fill_volumes)
        if code is not None:
            return code
        # 已被消费 (#675): every fill of this leg the account knows now (provider activity —
        # fresher than the ledger — plus the kept fills) is compared against the baseline the
        # stop persisted. A close of this leg and bucket that is not in the baseline and not
        # this card's own order means the lots the card targets are not provably the ones
        # still standing; an old card with no baseline is invalidated outright, never
        # backfilled with a guess. A fill without a readable tradingDay (成交先到、交易日晚补的
        # 缺日, or a malformed / non-calendar value — #700 review) is never skipped: it cannot
        # be proven to belong to another trading day, so it counts as this day's — unknown is
        # never guessed safe.
        if "baselineFillIds" not in intent:
            return "stop_close_baseline_missing"
        from .trading_costs import normalized_trading_day
        baseline = {str(item) for item in intent.get("baselineFillIds") or []}
        for fill in account_fills(self.store, [item for item in activity.get("trades") or []
                                               if isinstance(item, dict)],
                                  symbol, trading_account_id=account.account_id):
            if (str(fill.get("tradeId") or "") in baseline
                    or str(fill.get("clientOrderId") or "") in own_order_ids
                    or normalized_trading_day(fill.get("tradingDay")) not in (None, day)
                    or str(fill.get("direction") or "") != close_direction
                    or str(fill.get("offset") or "") not in offsets):
                continue
            return "stop_close_leg_consumed"
        return None

    async def _strategy_card_protection(self, proposal_id: str, *, user_id: str) -> dict[str, Any] | None:
        """The strategy protection for a pending strategy open card (None for any other card, or a
        card no longer awaiting confirmation — its replay needs nothing new)."""
        try:
            record = self.store.get(proposal_id)
        except KeyError:
            return None
        payload = record["payload"]
        if (record["proposalType"] != "trade" or record["status"] != "approval_required"
                or record["userId"] != user_id or not isinstance(payload.get("strategySource"), dict)
                or payload.get("offset", "OPEN") != "OPEN"):
            return None
        account = self._account_by_id(record.get("tradingAccountId"))
        if account is None and self.accounts is not None:
            account = self.accounts.default
        autonomy = getattr(account, "autonomy", None)
        if autonomy is None:
            raise ProposalConflict("strategy_protection_unavailable")
        from .autonomy import AutonomyError
        try:
            return await autonomy.strategy_card_protection(
                user_id=user_id, session_id=str(record["sessionId"]), payload=payload)
        except AutonomyError as error:
            # Refused before anything is prepared or sent: the card says why and stays unconfirmed.
            code = str(error)
            reason = STRATEGY_CARD_REFUSALS.get(code, f"策略保护没能建立（{code}），这张卡没有发单。")
            await self._refuse_card(proposal_id, record, payload, code=code, reason=reason)
            raise ProposalConflict(code, reason) from error

    async def _refuse_card(
        self, proposal_id: str, record: dict[str, Any], payload: dict[str, Any], *, code: str, reason: str,
    ) -> None:
        """Write a refusal onto a still-unconfirmed card and republish it (shared by every
        confirm-time protection refusal). The write is conditional (#700 review): a card
        another confirmation already confirmed — terminal, its approval spent, its version
        moved — keeps that winner's facts untouched; the late refusal writes nothing and
        publishes nothing, and the caller's conflict still says why this confirmation is
        refused while the winner's answer stays readable through its own idempotent key."""
        blocked = {**payload, "confirmBlocked": {"code": code, "reason": reason, "at": utc_now()}}
        if not self.store.refuse_pending(proposal_id, expected_version=int(record["version"]),
                                         payload=blocked, approval_id=record["approvalId"]):
            return
        await self.broker.publish(str(record["sessionId"]), kind="approval", name="proposal.trade", payload=blocked)

    async def _manual_card_protection(self, proposal_id: str, *, user_id: str) -> dict[str, Any] | None:
        """The order-link protection for a pending manual open card whose user set a stop / take on
        it (None for any other card). The levels were validated when the card was made; here the
        user-driven lease is ensured and the account's stop fences get their say."""
        try:
            record = self.store.get(proposal_id)
        except KeyError:
            return None
        payload = record["payload"]
        if (record["proposalType"] != "trade" or record["status"] != "approval_required"
                or record["userId"] != user_id or isinstance(payload.get("strategySource"), dict)
                or payload.get("offset", "OPEN") != "OPEN" or not isinstance(payload.get("protection"), dict)):
            return None
        account = self._account_by_id(record.get("tradingAccountId"))
        if account is None and self.accounts is not None:
            account = self.accounts.default
        autonomy = getattr(account, "autonomy", None)
        if autonomy is None:
            raise ProposalConflict("protection_unavailable")
        from .autonomy import AutonomyError
        try:
            return await autonomy.manual_card_protection(
                user_id=user_id, session_id=str(record["sessionId"]), payload=payload)
        except AutonomyError as error:
            code = str(error)
            reason = (getattr(error, "message", None)
                      or MANUAL_CARD_REFUSALS.get(code, f"保护没能建立（{code}），这张卡没有发单。"))
            await self._refuse_card(proposal_id, record, payload, code=code, reason=reason)
            raise ProposalConflict(code, reason) from error

    async def reject(
        self, proposal_id: str, *, user_id: str, proposal_version: int, idempotency_key: str,
    ) -> dict[str, Any]:
        action = "reject"
        with self.store.transaction() as connection:
            cached = self.store.get_idempotent(user_id=user_id, key=idempotency_key, action=action,
                                               proposal_id=proposal_id, connection=connection)
            if cached is not None:
                return cached
            record = self.store.get(proposal_id, connection=connection)
            self._validate_owner(record, user_id=user_id)
            if record["status"] not in {"risk_blocked", "approval_required"}:
                raise ProposalConflict("invalid_state_transition")
            if record["version"] != proposal_version:
                raise ProposalConflict("stale_proposal_version")
            payload = record["payload"]
            audit_id = self.store.audit(proposal_id, action, {"status": "rejected"}, connection=connection)
            payload.update({"status": "rejected", "auditId": audit_id})
            source = payload.get("strategySource")
            if isinstance(source, dict) and source.get("attemptId"):
                self.store.settle_strategy_card_attempt(
                    attempt_id=str(source["attemptId"]), detail="确认卡已放弃，未下单", connection=connection)
            self.store.update(proposal_id, status="rejected", payload=payload, version=record["version"],
                              approval_id=record["approvalId"], approval_expires_at=record["approvalExpiresAt"],
                              approval_used_at=utc_now(), connection=connection)
            plan_ended = self.store.end_plan_of_card(proposal_id, connection=connection)
            self.store.save_idempotent(user_id=user_id, key=idempotency_key, action=action,
                                       proposal_id=proposal_id, response=payload, connection=connection)
            self.broker.stage(
                record["sessionId"], kind="result", name="proposal.result",
                payload={"proposalId": proposal_id, "status": "rejected", "auditId": audit_id,
                         "message": "候选已拒绝，未执行任何操作。" + (PLAN_ENDED_TEXT if plan_ended else "")},
                connection=connection,
            )
        await self.broker.flush()
        return payload

    def supersede_stale_card(self, proposal_id: str, *, user_id: str) -> bool:
        """#518: 作废被替代计划还挂着的确认卡。新目标替代旧计划时，旧卡在 10 分钟确认窗内仍
        可被确认——现在它随计划一起作废（超时作废同款：状态 rejected + superseded 标记），点它
        不再发单；已结束的卡不受影响。同步入口（下单工具的 call 是同步的）：事件只落 outbox，
        由 broker 的投递循环送出。"""
        with self.store.transaction() as connection:
            try:
                record = self.store.get(proposal_id, connection=connection)
            except KeyError:
                return False
            self._validate_owner(record, user_id=user_id)
            if record["status"] != "approval_required":
                return False
            payload = record["payload"]
            audit_id = self.store.audit(proposal_id, "supersede", {"status": "rejected"}, connection=connection)
            payload.update({"status": "rejected", "auditId": audit_id, "superseded": True})
            self.store.update(proposal_id, status="rejected", payload=payload, version=record["version"],
                              approval_id=record["approvalId"], approval_expires_at=record["approvalExpiresAt"],
                              approval_used_at=utc_now(), connection=connection)
            self.broker.stage(
                record["sessionId"], kind="result", name="proposal.result",
                payload={"proposalId": proposal_id, "status": "rejected", "auditId": audit_id,
                         "message": SUPERSEDED_CARD_TEXT, "reason": "superseded"}, connection=connection,
            )
        return True
