"""User plan tiers (M2): per-user model access, token/voice quotas and the
tier's open skills.

Tiers live in the `plan_tiers` table (seeded once with basic/pro/test when the
table is empty; the owner edits everything through the admin surface and the
next request reads the change). The owner role stays the implicit unlimited
tier and is never a row in `cloud_users`.

Since the skill registry (2026-09-24) a tier's `config` carries `skills` — the
ids of the tier-gated skills it opens. The seven legacy `features` keys exist
only as a one-version compatibility mapping for 1.6.x clients and old database
rows; `FEATURE_TO_SKILLS` translates them, and the status payload still
derives the camel-case `features` object from the skills.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, NamedTuple

from fastapi import HTTPException

from .auth import CLOUD_USER_ID_PREFIX
from .model_provider import KIMI_MODEL, USER_MODEL_CATALOG, canonical_model_slug

GUEST_HISTORY_DAYS = 3
HISTORY_DAY_OPTIONS = (1, 3, 5)

if TYPE_CHECKING:
    # A runtime import would loop: the store mixins read the seed facts from here.
    from .store import RuntimeStore


class Feature(NamedTuple):
    """One legacy switch (1.6.x status/admin compatibility). The live registry
    of capabilities is the SKILL.md set in `agent_skills/`; these keys only
    ride along until the 1.6.x clients are gone."""

    key: str
    camel_key: str
    label: str
    description: str


FEATURES: tuple[Feature, ...] = (
    Feature("ai_execution", "aiExecution", "AI 自主执行", "AI 直接执行模拟下单与止损"),
    Feature("web_research", "webResearch", "联网研究", "在线检索与研究简报工具"),
    Feature("market_briefs", "marketBriefs", "行情简讯", "重要资讯与异动简讯推送"),
    Feature("strategies", "strategies", "策略管理与监控", "策略草案、验证与监控"),
    Feature("backtests", "backtests", "回测", "可复现回测与 TqSdk 历史回测"),
    Feature("replays", "replays", "行情回放", "历史行情回放工作台"),
    Feature("voice", "voice", "语音通话", "实时语音通话与播报"),
)
FEATURE_KEYS = frozenset(feature.key for feature in FEATURES)
FEATURE_BY_CAMEL = {feature.camel_key: feature for feature in FEATURES}
# The 1.6.7 client knows one switch for the whole strategy toolbox; newer keys
# stay alongside it so that client keeps working against a newer Runtime.
STRATEGY_TOOLS_FEATURES = ("strategies", "backtests", "replays")
# The one-time migration (and the 1.6.x admin surface, which still PUTs camel
# feature keys): which registry skills each legacy switch stood for. A switch
# is on for a tier exactly when every skill of its group is open.
FEATURE_TO_SKILLS: dict[str, frozenset[str]] = {
    "ai_execution": frozenset({"ai-execution"}),
    "web_research": frozenset({"web-research"}),
    "market_briefs": frozenset({"market-briefs"}),
    "strategies": frozenset({"strategy-management", "strategy-signals"}),
    "backtests": frozenset({"strategy-backtests"}),
    "replays": frozenset({"market-replay", "replay-control"}),
    "voice": frozenset({"voice-call"}),
}


class Plan:
    """One tier's limits. `None` means unlimited / every configured model.
    `skill_ids` are the tier-gated skills this tier opens; the unlimited plan
    (`unlimited=True`, the owner) opens everything and ignores the set."""

    __slots__ = ("name", "label", "models", "daily_tokens", "voice_seconds_per_day",
                 "skill_ids", "unlimited", "market_history_trading_days", "brief_products")

    def __init__(
        self, name: str, *, label: str = "", models: frozenset[str] | None = None,
        daily_tokens: int | None = None, voice_seconds_per_day: int | None = None,
        skill_ids: frozenset[str] = frozenset(), unlimited: bool = False,
        market_history_trading_days: int | None = None,
        brief_products: int | None = None,
    ) -> None:
        self.name = name
        self.label = label or name
        self.models = models
        self.daily_tokens = daily_tokens
        self.voice_seconds_per_day = voice_seconds_per_day
        self.skill_ids = frozenset(skill_ids)
        self.unlimited = unlimited
        self.market_history_trading_days = market_history_trading_days
        self.brief_products = brief_products

    def allows_model(self, slug: str) -> bool:
        return self.models is None or slug in self.models

    def has_skill(self, skill_id: str) -> bool:
        """Whether this tier opens a tier-gated skill (global enable and the
        owner bypass live in `agent_skills.skill_allowed`)."""
        return self.unlimited or skill_id in self.skill_ids

    def features(self) -> dict[str, bool]:
        """The camel-case `features` object 1.6.x clients read, derived from
        the tier's skills: a legacy switch is on when its whole skill group is
        open."""
        values = {
            feature.camel_key: self.has_skill_in_group(feature.key) for feature in FEATURES
        }
        values["strategyTools"] = all(self.has_skill_in_group(key) for key in STRATEGY_TOOLS_FEATURES)
        return values

    def has_skill_in_group(self, feature_key: str) -> bool:
        group = FEATURE_TO_SKILLS.get(feature_key, frozenset())
        return all(self.has_skill(skill_id) for skill_id in group)

    def config(self) -> dict[str, Any]:
        """The `plan_tiers.config` JSON of this tier."""
        return {
            "models": None if self.models is None else sorted(self.models),
            "dailyTokens": self.daily_tokens,
            "voiceSecondsPerDay": self.voice_seconds_per_day,
            "marketHistoryTradingDays": self.market_history_trading_days,
            "briefProducts": self.brief_products,
            "skills": sorted(self.skill_ids),
        }


UNLIMITED = Plan("owner", label="管理员", unlimited=True)


def plan_from_config(name: str, label: str, config: dict[str, Any]) -> Plan:
    """A tier row as a `Plan`; unknown skill ids are dropped, never fatal."""
    history_days = config.get("marketHistoryTradingDays")
    if name == "guest":
        if type(history_days) is not int or history_days not in HISTORY_DAY_OPTIONS:
            history_days = GUEST_HISTORY_DAYS
        voice = config.get("voiceSecondsPerDay")
        voice = min(300, max(0, voice)) if type(voice) is int else 0
        return Plan(name, label=label, models=frozenset(), daily_tokens=0,
                    voice_seconds_per_day=voice, market_history_trading_days=history_days,
                    brief_products=0,
                    skill_ids=frozenset({"voice-call"}) if "voice-call" in (config.get("skills") or []) else frozenset())
    if history_days is not None:
        # Member history/replay/AI paths are not yet uniformly limited. Never
        # advertise a stored cap while returning unrestricted history.
        raise HTTPException(503, "plan_market_history_limit_unsupported")
    raw_models = config.get("models")
    skills = frozenset(str(skill_id) for skill_id in config.get("skills", ()) if str(skill_id))

    def number(key: str) -> int | None:
        raw = config.get(key)
        return None if raw is None else max(0, int(raw))

    # AI 简报订阅上限 (2026-10-05): a tier row from before the key means 3, not
    # unlimited — only an explicit null lifts the cap.
    return Plan(
        name, label=label,
        models=None if raw_models is None else frozenset(canonical_model_slug(str(slug)) for slug in raw_models if str(slug)),
        daily_tokens=number("dailyTokens"),
        voice_seconds_per_day=number("voiceSecondsPerDay"),
        brief_products=number("briefProducts") if "briefProducts" in config else 3,
        skill_ids=skills,
    )


# The three built-in tiers' non-skill facts; the skill lists are read from the
# registry (`agent_skills.seed_tier_configs`) so SKILL.md stays the single
# source of truth. Seeded exactly once, when `plan_tiers` is empty at startup
# (owner edits and deletions afterwards are never resurrected). Owner-confirmed
# 2026-09-22/23: `basic` is what a TqKq direct login starts on, `pro` the
# upgrade, `test` the owner's disposable middle tier.
TIER_SEED_META: tuple[tuple[str, str, bool, int, dict[str, Any]], ...] = (
    ("basic", "基础档", True, 0, {
        "models": ["deepseek-flash"], "dailyTokens": 300_000, "voiceSecondsPerDay": 30 * 60,
        "briefProducts": 3,
    }),
    ("pro", "高级档", False, 1, {
        "models": None, "dailyTokens": 2_000_000, "voiceSecondsPerDay": 120 * 60,
        "briefProducts": 10,
    }),
    ("test", "测试档", False, 2, {
        "models": None, "dailyTokens": 1_000_000, "voiceSecondsPerDay": 60 * 60,
    }),
)
# The built-in tiers' AI 简报订阅上限 an upgrade also writes into rows that
# predate the key (owner 2026-10-05: 基础 3 / 高级 10) — a row that already
# carries any value is the owner's edit and is never touched.
BRIEF_PRODUCT_DEFAULTS: dict[str, int] = {"basic": 3, "pro": 10}


def plan_for_user(store: RuntimeStore | None, user_id: str | None) -> Plan:
    """The tier a scoped user id runs on. The owner and the local-token user are
    not `cloud_users` rows, so a miss resolves to the unlimited tier; a cloud
    user whose tier row was deleted (or holds a stale name) falls back to the
    default tier. A cloud-jwt subject (`cloud-<sha>`) never has a row either —
    it is a JWT user, not the owner, so it runs on the default tier."""
    if user_id and user_id.startswith("guest:"):
        tier = store.get_plan_tier("guest") if store is not None else None
        return plan_from_config("guest", str(tier["label"]) if tier else "游客", dict(tier["config"]) if tier else {})
    if store is None or not user_id:
        return UNLIMITED
    tier_name = store.get_user_plan(user_id)
    if tier_name is None and not user_id.startswith(CLOUD_USER_ID_PREFIX):
        return UNLIMITED
    tier = store.get_plan_tier(tier_name) if tier_name is not None else None
    if tier is None:
        default = store.get_default_plan_tier()
        # The final fallback (an emptied tier table) is the basic-tier facts
        # with no tier-gated skills — conservative, and only reachable in a
        # broken database state.
        fallback_name = tier_name or "basic"
        tier = default if default is not None else {
            "name": fallback_name, "label": fallback_name, "config": {
                **TIER_SEED_META[0][4], "skills": [],
            }}
    if tier["name"] == "guest":
        raise HTTPException(503, "guest_plan_reserved")
    return plan_from_config(str(tier["name"]), str(tier["label"]), dict(tier["config"]))


def daily_token_limit(plan: Plan, *, env_limit: int) -> int | None:
    """The effective per-day token cap: the tighter of the tier and the global
    `AI_TRADER_DAILY_TOKEN_LIMIT` backstop; `None` = unlimited. An explicit
    tier 0 is the owner switching the tier's AI off — it means 0, not
    unlimited (529-B1)."""
    if plan.name == "guest":
        return 0
    if plan.daily_tokens == 0:
        return 0
    limits = [value for value in (plan.daily_tokens, env_limit) if value and value > 0]
    return min(limits) if limits else None


# -- cost estimates for the owner's user table --------------------------------
#
# Blended CNY per 1M tokens (rough, for the admin page only — not billing):
# input:output ≈ 3:1, peak/off-peak midpoints. Sources, read 2026-09-22:
# - DeepSeek V4.1 Flash / V4 Pro: api-docs.deepseek.com pricing (peak/off-peak
#   since 2026-08-17; flash in 1–2 / out 4–8, pro in 4.5–9 / out 13.5–27).
# - GLM 5.3 Flash: bigmodel.cn (in 0.8 / out 2.8, 2026-08-27 listing).
# - Doubao realtime voice has no official per-minute price (token-billed,
#   volcengine 计费说明 2026-08-20); ~30k tokens per spoken minute at the
#   10 CNY/1M input-text rate gives the 0.3 CNY/minute estimate below.
MODEL_PRICE_CNY_PER_MTOK = {
    "deepseek-flash": 2.6,
    "deepseek-v4-pro": 10.1,
    "glm-5.3-flash": 1.3,
    # Kimi Code is subscription-billed; no per-token list price.
    KIMI_MODEL: None,
}
VOICE_PRICE_CNY_PER_MINUTE = 0.3


def estimated_daily_cost_cny(
    *, tokens_today: int, model_slug: str | None, voice_seconds_today: int,
) -> float | None:
    """A coarse per-day cost for the owner's table; `None` when the current
    model has no listed price (shown as —, never as 0)."""
    price = MODEL_PRICE_CNY_PER_MTOK.get(model_slug or "")
    if price is None:
        return None
    return round(
        tokens_today / 1_000_000 * price + voice_seconds_today / 60 * VOICE_PRICE_CNY_PER_MINUTE,
        2,
    )


def default_allowed_model(
    plan: Plan, configured_slugs: set[str], preferred: str | None = None,
) -> str | None:
    """The model a turn actually runs on: the stored preference when the tier
    still allows it, else the first configured catalog model the tier allows
    (catalog order, so the fallback is deterministic). `None` when no configured
    model is allowed — the caller decides what that means for them."""
    if preferred is not None and preferred in configured_slugs and plan.allows_model(preferred):
        return preferred
    return next(
        (spec.slug for spec in USER_MODEL_CATALOG
         if spec.slug in configured_slugs and plan.allows_model(spec.slug)),
        None,
    )


__all__ = [
    "BRIEF_PRODUCT_DEFAULTS", "FEATURES", "FEATURE_BY_CAMEL", "FEATURE_KEYS", "FEATURE_TO_SKILLS",
    "Plan", "TIER_SEED_META", "UNLIMITED",
    "daily_token_limit", "default_allowed_model", "estimated_daily_cost_cny",
    "plan_from_config", "plan_for_user",
]
