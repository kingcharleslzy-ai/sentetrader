"""统一提醒的类别表 (#932, 2026-10-15): every reminder kind, its group and the one switch that gates
all of its channels — the conversation, the desktop notification (feed.reminder), the spoken line on a
live call and the lock-screen push. One table, read by the delivery entry (`reminders.deliver_reminder`),
the push hub (`web_push.kind_enabled`) and the help text; no channel keeps a switch of its own.

Groups: `trade` (交易与账户 · 进对话 — a message and card land in the conversation), `info` (信息 · 只进
提醒中心 — never written into a conversation) and `always` (急停, no switch at all).

The switches reuse the existing `notify` keys (旧键沿用) and add five (stopTriggered,
protectionCancelled, orderUnfilled, delivery, researchDebate, default on). The old voice-only switches
`voice.announceFills` / `voice.announceBriefs` are still read (an older document validates) but decide
nothing any more: 成交 / 撤单 are spoken by their fills / cancels switches, AI 简讯 by its briefs switch."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from .user_preferences import UserPreferences, preference_value


@dataclass(frozen=True)
class ReminderKind:
    label: str
    group: Literal["trade", "info", "always"]
    preference: str | None


KINDS: dict[str, ReminderKind] = {
    "stop_triggered": ReminderKind("止损止盈触发", "trade", "notify.stopTriggered"),
    "protection_cancelled": ReminderKind("保护单撤销", "trade", "notify.protectionCancelled"),
    "order_unfilled": ReminderKind("挂单未成交", "trade", "notify.orderUnfilled"),
    # 目标持仓下一步 (the relay's own notice; its next-leg card is the business and always lands).
    "target_plan": ReminderKind("目标持仓", "trade", "notify.orderUnfilled"),
    "strategy": ReminderKind("策略", "trade", "notify.strategySignalAlerts"),
    "fill": ReminderKind("成交", "trade", "notify.desktop.fills"),
    "cancel": ReminderKind("撤单", "trade", "notify.desktop.cancels"),
    "price_alert": ReminderKind("到价", "trade", "notify.desktop.priceAlerts"),
    "market_close": ReminderKind("收盘前", "trade", "notify.marketCloseReminder"),
    "risk_ratio": ReminderKind("风险度", "trade", "notify.riskReminder"),
    "breakeven": ReminderKind("保本", "trade", "notify.breakEven"),
    "delivery": ReminderKind("交割到期", "trade", "notify.delivery"),
    "market_movement": ReminderKind("盘中异动", "trade", "notify.movement"),
    "daily_review": ReminderKind("每日复盘", "trade", "notify.dailyReview"),
    "morning_desk": ReminderKind("今日作战表", "info", "notify.morningDesk"),
    "research_debate": ReminderKind("研讨完成", "info", "notify.researchDebate"),
    "brief": ReminderKind("AI 简讯", "info", "notify.desktop.briefs"),
    "emergency_stop": ReminderKind("急停", "always", None),
}
# Older names a reader may still send or hold (the card kind 保护触发 was `protection`).
ALIASES = {"protection": "stop_triggered"}

_DEFAULTS = UserPreferences().model_dump()


def canonical(kind: str) -> str:
    return ALIASES.get(kind, kind)


def _switch(preferences: dict[str, Any], key: str) -> bool:
    value = preference_value(preferences, key)
    return value if isinstance(value, bool) else bool(preference_value(_DEFAULTS, key))


def reminder_enabled(preferences: dict[str, Any], kind: str) -> bool:
    """Whether this kind's one switch lets it out (急停 always). An unknown kind never goes out."""
    definition = KINDS.get(canonical(kind))
    if definition is None:
        return False
    if definition.preference is None:
        return True
    return _switch(preferences, definition.preference)


def voice_allowed(preferences: dict[str, Any], kind: str) -> bool:
    """The spoken line: the kind's one switch, nothing else (the old voice-only switches no longer count)."""
    return reminder_enabled(preferences, kind)
