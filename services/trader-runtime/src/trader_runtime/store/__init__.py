"""SQLite is the runtime source of truth; in-memory objects are only waiters.

`RuntimeStore` is the one facade every service uses. Its methods live in one
mixin per domain; `StoreBase` owns the connection and the migrations.
"""
from __future__ import annotations

from .accounts import AccountStore
from .backtests import StrategyBacktestStore
from .base import StoreBase, utc_after, utc_now
from .chart_drawings import ChartDrawingStore
from .daily_reviews import DailyReviewStore
from .delivery_notices import DeliveryNoticeStore
from .instances import InstanceLeaseStore
from .labs import StrategyLabStore
from .market_sources import MarketSourceStore
from .memory import MemoryStore
from .plan_tiers import PlanTierStore
from .playbooks import PlaybookStore
from .preferences import PreferencesStoreMixin
from .price_alerts import PriceAlertStore
from .proposals import ProposalStore as ProposalStoreMixin
from .protection_steps import ProtectionStepStore
from .push_subscriptions import PushSubscriptionStore
from .reminder_notices import ReminderNoticeStore
from .reminders import ReminderStore
from .routines import RoutineStore
from .sessions import SessionStore
from .skill_settings import SkillSettingsStore
from .strategies import StrategyStore
from .target_plans import TargetPlanStore
from .trades import TradeStore
from .trading_accounts import TradingAccountStore


class RuntimeStore(SessionStore, StrategyStore, ProposalStoreMixin, AccountStore, TradeStore, PreferencesStoreMixin, InstanceLeaseStore, TradingAccountStore, TargetPlanStore, PlanTierStore, SkillSettingsStore, MemoryStore, StrategyLabStore, StrategyBacktestStore, PriceAlertStore, ChartDrawingStore, DeliveryNoticeStore, ReminderNoticeStore, MarketSourceStore, PushSubscriptionStore, DailyReviewStore, ProtectionStepStore, RoutineStore, ReminderStore, PlaybookStore):
    """SQLite is the runtime source of truth; in-memory objects are only waiters."""


ProposalStore = RuntimeStore

__all__ = ["ProposalStore", "RuntimeStore", "StoreBase", "utc_after", "utc_now"]
