"""The built-in skill registry: every SKILL.md under `agent_skills/` is the
single source of truth for what the product's AI can do.

One skill directory carries an Agent Skills open-format SKILL.md (YAML
frontmatter + Markdown body; the product's extensions live in `metadata` under
`ai-trader-*` keys, list values space-separated). The registry derives, at
runtime and with no second handwritten list:

- which AI tools exist and are hidden for a user (`hidden_tool_names`);
- which tier-gated skills a plan opens (`Plan.skill_ids`, `FEATURE_TO_SKILLS`);
- the HTTP write-entry gates (`require_skill`) and the capability catalog's
  skill attribution;
- the catalog/progressive-disclosure surfaces (name+description listing,
  `activate_skill`, `read_skill_resource`) for the built-in agent, MCP and CLI.

Parsing is deliberately lenient per the spec's client guidance
(agentskills.io/client-implementation): a malformed field warns and loads
anyway; only a missing description or an unparseable frontmatter skips the
skill, with a diagnostic logged. No new dependency: the frontmatter subset
here is flat scalars plus one `metadata:` mapping.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException, Request

from .plans import (
    FEATURE_TO_SKILLS,
    FEATURES,
    STRATEGY_TOOLS_FEATURES,
    plan_for_user,
)

if TYPE_CHECKING:
    from .store import RuntimeStore

log = logging.getLogger("uvicorn.error.skills")

# Spec: 1–64 chars, lowercase alphanumerics and hyphens, no leading/trailing
# hyphen, no double hyphen, and the name must equal the directory name.
SKILL_NAME_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
DESCRIPTION_MAX = 1024
COMPATIBILITY_MAX = 500
BODY_MAX_LINES = 500
RESOURCE_MAX_BYTES = 128 * 1024
RESOURCE_SUFFIXES = {".md", ".txt", ".json", ".csv", ".yaml", ".yml"}

# The metadata keys the product defines, `ai-trader-` prefix stripped.
_META_PREFIX = "ai-trader-"
_META_LABEL = "label"
_META_CATEGORY = "category"
_META_PARENT = "parent"
_META_SUMMARY = "summary"
_META_TOOLS = "tools"
_META_ROUTES = "routes"
_META_TIER_GATED = "tier-gated"
_META_OWNER_ONLY = "owner-only"
_META_UI_ENTRIES = "ui-entries"
_META_QUICK_ACTION = "quick-action"
_META_DEFAULT_TIERS = "default-tiers"
# 按技能延迟挂载 (2026-10-14): words in a typed or delegated sentence that mount this skill (its
# deferred tools and its manual) for that turn, without a model round spent on activate_skill.
_META_MOUNT_WORDS = "mount-words"

_PHRASES_HEADING = "典型说法"
_COUNTER_HEADING = "反例"


def skills_root() -> Path:
    """The packaged skill tree. Works in the source checkout, a wheel install
    and the PyInstaller bundle (data staged at trader_runtime/agent_skills)."""
    return Path(__file__).resolve().parent / "agent_skills"


@dataclass(frozen=True)
class Skill:
    """One parsed SKILL.md. `skill_id` is the spec name (= directory name)."""

    skill_id: str
    description: str
    label: str
    category: str
    parent: str
    summary: str
    tools: tuple[str, ...]
    routes: tuple[str, ...]
    tier_gated: bool
    owner_only: bool
    ui_entries: tuple[str, ...]
    quick_action: str
    default_tiers: tuple[str, ...]
    phrases: tuple[str, ...]
    counter_examples: tuple[str, ...]
    body: str
    references: tuple[str, ...]
    directory: Path
    mount_words: tuple[str, ...] = ()

    @property
    def name(self) -> str:
        """The spec's `name` (identical to the id by validation)."""
        return self.skill_id

    def catalog_line(self) -> str:
        """The tier-1 progressive-disclosure entry: name + description."""
        return f"- {self.skill_id}: {self.description}"


def _strip_quotes(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def _parse_frontmatter(text: str) -> dict[str, Any] | None:
    """The flat `key: value` (+ one indented `metadata:` block) subset the
    skill files use. None when the file has no frontmatter at all."""
    match = re.match(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", text, re.S)
    if match is None:
        return None
    fields: dict[str, Any] = {"metadata": {}}
    in_metadata = False
    for raw_line in match.group(1).splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        indented = raw_line[0] in {" ", "\t"}
        stripped = raw_line.strip()
        if ":" not in stripped:
            continue
        key, _, value = stripped.partition(":")
        key = key.strip()
        if not key:
            continue
        if indented:
            if in_metadata:
                fields["metadata"][key] = _strip_quotes(value)
            continue
        in_metadata = key == "metadata" and not value.strip()
        if not in_metadata:
            fields[key] = _strip_quotes(value)
    return fields


def _meta_bool(meta: dict[str, str], key: str) -> bool:
    return str(meta.get(key, "false")).strip().lower() in {"1", "true", "yes", "on"}


def _split_list(value: str) -> tuple[str, ...]:
    return tuple(item for item in value.split() if item)


_HTTP_METHODS = {"GET", "POST", "PUT", "DELETE", "PATCH"}


def _split_routes(value: str) -> tuple[str, ...]:
    """`METHOD path METHOD path …` pairs. A route token carries a space, so
    plain whitespace-splitting does not work: pair each uppercase verb with
    the token after it."""
    routes: list[str] = []
    method: str | None = None
    for token in value.split():
        if method is None:
            if token.upper() in _HTTP_METHODS:
                method = token.upper()
                continue
            # A bare path (the skill does not care about the verb).
            routes.append(token)
        else:
            routes.append(f"{method} {token}")
            method = None
    if method is not None:
        log.warning("route list ends with a dangling method %s", method)
    return tuple(routes)


def _section_bullets(body: str, heading_prefix: str) -> tuple[str, ...]:
    """The `- ` bullets of the `## <heading_prefix>…` section (its text is the
    routing-eval corpus and the UI's 典型说法)."""
    bullets: list[str] = []
    inside = False
    for line in body.splitlines():
        if line.startswith("#"):
            inside = line[2:].strip().startswith(heading_prefix) and line.startswith("## ")
            continue
        if not inside:
            continue
        stripped = line.strip()
        if stripped.startswith(("- ", "• ")):
            bullets.append(stripped[2:].strip())
        elif stripped and not stripped.startswith(("|", ">", "```")) and not bullets:
            continue  # prose before the first bullet is not a phrase
        elif stripped:
            continue
    return tuple(bullets)


def _parse_skill(directory: Path) -> Skill | None:
    """One directory → one Skill, leniently. Only a missing/unreadable
    description skips the skill entirely."""
    skill_file = directory / "SKILL.md"
    try:
        text = skill_file.read_text(encoding="utf-8")
    except OSError as error:
        log.warning("skill %s skipped: unreadable SKILL.md (%s)", directory.name, error)
        return None
    fields = _parse_frontmatter(text)
    if fields is None:
        log.warning("skill %s skipped: no YAML frontmatter", directory.name)
        return None
    name = str(fields.get("name") or "").strip()
    if not name:
        name = directory.name
        log.warning("skill %s: frontmatter name missing, using directory name", directory.name)
    if not SKILL_NAME_PATTERN.match(name) or not (1 <= len(name) <= 64):
        log.warning("skill %s: name %r violates the spec charset/length", directory.name, name)
    if name != directory.name:
        log.warning("skill %s: name %r does not match the directory, using %s",
                    directory.name, name, directory.name)
    description = str(fields.get("description") or "").strip()
    if not description:
        log.warning("skill %s skipped: description is required by the spec", directory.name)
        return None
    if len(description) > DESCRIPTION_MAX:
        log.warning("skill %s: description over %d chars (kept, truncated nowhere)", name, DESCRIPTION_MAX)
    compatibility = str(fields.get("compatibility") or "")
    if len(compatibility) > COMPATIBILITY_MAX:
        log.warning("skill %s: compatibility over %d chars", name, COMPATIBILITY_MAX)
    meta: dict[str, str] = {}
    for key, value in (fields.get("metadata") or {}).items():
        if not isinstance(value, str):
            log.warning("skill %s: metadata %s is not a string, coerced", name, key)
            value = str(value)
        # The product's extensions ride in metadata under `ai-trader-*`; strip
        # the prefix once here so the readers below use the bare names.
        meta[str(key).removeprefix(_META_PREFIX)] = value

    body_match = re.search(r"\A---[ \t]*\r?\n.*?\r?\n---[ \t]*(?:\r?\n|\Z)", text, re.S)
    body = text[body_match.end():] if body_match else text
    if len(body.splitlines()) > BODY_MAX_LINES:
        log.warning("skill %s: body over %d lines", name, BODY_MAX_LINES)

    references_dir = directory / "references"
    references: tuple[str, ...] = ()
    if references_dir.is_dir():
        references = tuple(
            f"references/{item.name}"
            for item in sorted(references_dir.iterdir())
            if item.is_file() and item.suffix.lower() in RESOURCE_SUFFIXES
        )

    return Skill(
        skill_id=directory.name,
        description=description,
        label=str(meta.get(_META_LABEL) or directory.name),
        category=str(meta.get(_META_CATEGORY) or "未分类"),
        parent=str(meta.get(_META_PARENT) or ""),
        summary=str(meta.get(_META_SUMMARY) or ""),
        tools=_split_list(str(meta.get(_META_TOOLS) or "")),
        routes=_split_routes(str(meta.get(_META_ROUTES) or "")),
        tier_gated=_meta_bool(meta, _META_TIER_GATED),
        owner_only=_meta_bool(meta, _META_OWNER_ONLY),
        ui_entries=_split_list(str(meta.get(_META_UI_ENTRIES) or "")),
        quick_action=str(meta.get(_META_QUICK_ACTION) or ""),
        default_tiers=_split_list(str(meta.get(_META_DEFAULT_TIERS) or "")),
        phrases=_section_bullets(body, _PHRASES_HEADING),
        counter_examples=_section_bullets(body, _COUNTER_HEADING),
        body=body.strip(),
        references=references,
        directory=directory,
        mount_words=_split_list(str(meta.get(_META_MOUNT_WORDS) or "")),
    )


@lru_cache(maxsize=1)
def _load(root_str: str) -> tuple[Skill, ...]:
    root = Path(root_str)
    if not root.is_dir():
        log.error("skill registry empty: %s is not a directory", root)
        return ()
    skills: list[Skill] = []
    seen_tools: dict[str, str] = {}
    seen_routes: dict[str, str] = {}
    for directory in sorted(root.iterdir()):
        if not directory.is_dir() or not (directory / "SKILL.md").is_file():
            continue
        skill = _parse_skill(directory)
        if skill is None:
            continue
        for tool in skill.tools:
            if tool in seen_tools:
                log.error("tool %s claimed by both %s and %s", tool, seen_tools[tool], skill.skill_id)
            seen_tools[tool] = skill.skill_id
        for route in skill.routes:
            if route in seen_routes:
                log.error("route %s claimed by both %s and %s", route, seen_routes[route], skill.skill_id)
            seen_routes[route] = skill.skill_id
        skills.append(skill)
    # One level of hierarchy only: a parent that does not exist or is itself a
    # child warns and degrades to standalone (the child keeps working alone).
    by_id = {skill.skill_id: skill for skill in skills}
    for index, skill in enumerate(skills):
        parent = by_id.get(skill.parent) if skill.parent else None
        if skill.parent and (parent is None or parent.parent):
            reason = "does not exist" if parent is None else "is itself a child (one level only)"
            log.warning("skill %s: parent %s %s, treated as standalone", skill.skill_id, skill.parent, reason)
            skills[index] = replace(skill, parent="")
    log.info("skill registry loaded: %d skills from %s", len(skills), root)
    return tuple(skills)


def all_skills(root: Path | None = None) -> tuple[Skill, ...]:
    """The registry, category-stable (definition order: category then id)."""
    if root is not None:
        return _load(str(root))
    return _load(str(skills_root()))


def skill_by_id(skill_id: str, root: Path | None = None) -> Skill | None:
    return next((skill for skill in all_skills(root) if skill.skill_id == skill_id), None)


def tool_owner_map(root: Path | None = None) -> dict[str, str]:
    """AI tool name → the one skill that owns it (the audit basis)."""
    return {tool: skill.skill_id for skill in all_skills(root) for tool in skill.tools}


def route_owner_map(root: Path | None = None) -> dict[str, str]:
    """`METHOD path` → the one skill that owns it."""
    return {route: skill.skill_id for skill in all_skills(root) for route in skill.routes}


def seed_tier_configs() -> dict[str, list[str]]:
    """The `skills` list each built-in tier seeds with: a tier-gated skill is
    in the tier's list when its `ai-trader-default-tiers` metadata names it."""
    return {
        tier_name: sorted(
            skill.skill_id for skill in all_skills()
            if skill.tier_gated and tier_name in skill.default_tiers
        )
        for tier_name in ("basic", "pro", "test")
    }


def migrate_config_features(config: dict[str, Any]) -> dict[str, Any] | None:
    """The one-time features→skills conversion for a tier row's config, in
    place; returns the rewritten config when something changed, None when the
    row is already migrated (idempotent). Legacy `features` was a list of the
    enabled feature keys (`Plan.config()`'s old shape); a dict {key: bool} is
    accepted too. Switches on convert to the union of their skill groups —
    exactly the pre-registry availability."""
    features = config.get("features")
    if isinstance(features, dict):
        enabled = {str(key) for key, on in features.items() if on}
    elif isinstance(features, (list, tuple)):
        enabled = {str(key) for key in features}
    elif "features" in config:
        log.warning("plan migration: unrecognized features shape %r", features)
        enabled = set()
    elif "skills" in config:
        return None
    else:
        # A row from neither era: treat every legacy switch as on — the
        # pre-registry default when the key was missing.
        config["skills"] = sorted({s for group in FEATURE_TO_SKILLS.values() for s in group})
        return config
    skills = set(str(item) for item in config.get("skills", ()) if str(item))
    for key in enabled:
        group = FEATURE_TO_SKILLS.get(key)
        if group is None:
            log.warning("plan migration: unknown legacy feature key %s dropped", key)
            continue
        skills |= set(group)
    config["skills"] = sorted(skills)
    del config["features"]
    return config


# -- availability --------------------------------------------------------------


# The owner's global skill switch governs the AI (owner decision 2026-09-27: "只影响 AI").
# A skill that IS an AI feature stops for everyone when switched off; for every other skill
# the switch only takes it away from the AI (tools, capability catalog, agent/call, voice and
# typed routing), and the user's own screens and their HTTP routes keep working.
AI_FEATURE_SKILLS = frozenset({
    "ai-execution", "market-briefs", "voice-call", "web-research", "chart-analysis",
    "app-catalog",  # /api/agent/call itself: the external AI's entry
})


def switched_off(store: RuntimeStore | None, skill: Skill, *, human: bool = False) -> bool:
    if store is None or store.skill_enabled(skill.skill_id):
        return False
    return not human or skill.skill_id in AI_FEATURE_SKILLS


def human_request(request) -> bool:
    """A request the user's own UI or client made, not an AI call through /api/agent/call."""
    return "ai_trader.call_identity" not in request.scope


def own_block_reason(store: RuntimeStore | None, user_id: str | None, skill: Skill, *, human: bool = False) -> str | None:
    """The skill's own availability checks, parent inheritance excluded — the
    piece `skill_block_reason` adds on top of and `parent_block_reason` needs
    to tell an inherited block from the skill's own."""
    if switched_off(store, skill, human=human):
        return "disabled"
    if user_id and user_id.startswith("guest:") and skill.skill_id != "voice-call":
        return "guest_read_only"
    if skill.tier_gated or skill.owner_only:
        plan = plan_for_user(store, user_id)
        if skill.owner_only and not plan.unlimited:
            return "owner_only"
        if skill.tier_gated and not plan.has_skill(skill.skill_id):
            return "not_in_plan"
    return None


def skill_block_reason(store: RuntimeStore | None, user_id: str | None, skill: Skill, *, human: bool = False) -> str | None:
    """Why a skill is unavailable: "disabled" (the owner switched it off; see
    AI_FEATURE_SKILLS for what that covers when `human`), "not_in_plan" (a member's
    tier does not open it) or "owner_only" (an admin-surface skill for someone
    else). None = available.

    Tier checks apply only to tier-gated skills — a non-gated skill is every
    user's by definition. The owner is never tier-blocked but IS bound by the
    global switch. A skill with a parent inherits the parent's reason when its
    own is clear (an unavailable parent makes its children unavailable)."""
    own = own_block_reason(store, user_id, skill, human=human)
    if own is not None:
        return own
    if skill.parent:
        parent = skill_by_id(skill.parent)
        if parent is not None:
            return skill_block_reason(store, user_id, parent, human=human)
    return None


def parent_block_reason(store: RuntimeStore | None, user_id: str | None, skill: Skill, *, human: bool = False) -> str | None:
    """The parent's block reason when the skill's own is clear — the inherited
    case whose refusal line names the parent (`「子」随「上级」关闭` etc.)."""
    if not skill.parent or own_block_reason(store, user_id, skill, human=human) is not None:
        return None
    parent = skill_by_id(skill.parent)
    return skill_block_reason(store, user_id, parent, human=human) if parent is not None else None


def skill_allowed(store: RuntimeStore | None, user_id: str | None, skill: Skill) -> bool:
    return skill_block_reason(store, user_id, skill) is None


def availability_map(store: RuntimeStore | None, user_id: str | None) -> dict[str, str | None]:
    """Every skill's block reason for one user, from one read of the skill
    switches and one of the user's plan. The per-skill `skill_block_reason`
    above fits single checks; the whole-registry consumers (the directory,
    the capability catalog, the tool hiding) build this map once per request
    instead of hitting the store once per skill."""
    switches = store.skill_settings_all() if store is not None else {}
    plan = plan_for_user(store, user_id)

    def reason(skill: Skill) -> str | None:
        if store is not None and not switches.get(skill.skill_id, True):
            return "disabled"
        if user_id and user_id.startswith("guest:") and skill.skill_id != "voice-call":
            return "guest_read_only"
        if skill.tier_gated or skill.owner_only:
            if skill.owner_only and not plan.unlimited:
                return "owner_only"
            if skill.tier_gated and not plan.has_skill(skill.skill_id):
                return "not_in_plan"
        return None

    reasons = {skill.skill_id: reason(skill) for skill in all_skills()}
    # Children follow their parent: a clear own reason stays, otherwise the
    # parent's (already final — a validated parent has no parent itself).
    for skill in all_skills():
        if skill.parent and reasons[skill.skill_id] is None:
            reasons[skill.skill_id] = reasons.get(skill.parent)
    return reasons


def allowed_skills(store: RuntimeStore | None, user_id: str | None) -> tuple[Skill, ...]:
    reasons = availability_map(store, user_id)
    return tuple(skill for skill in all_skills() if reasons.get(skill.skill_id) is None)


def tier_gated_skills() -> tuple[Skill, ...]:
    """The skills a tier's checkbox matrix offers (the admin plan editor)."""
    return tuple(skill for skill in all_skills() if skill.tier_gated)


def hidden_tool_names(store: RuntimeStore | None, user_id: str | None) -> frozenset[str]:
    """The AI tool names a user must not see: the union of the tools of every
    skill not available to them (globally disabled, tier-withheld or
    owner-only)."""
    reasons = availability_map(store, user_id)
    return frozenset(
        tool for skill in all_skills()
        if reasons.get(skill.skill_id) is not None for tool in skill.tools)


def _features_from(reasons: dict[str, str | None]) -> dict[str, bool]:
    """The camel-case `features` view of a reasons map: a legacy switch is on
    when every skill of its group is available (a group naming an unknown id
    stays off, exactly as when the per-skill checks ran)."""
    values = {
        feature.camel_key: all(
            skill_id in reasons and reasons[skill_id] is None
            for skill_id in FEATURE_TO_SKILLS.get(feature.key, ())
        )
        for feature in FEATURES
    }
    values["strategyTools"] = all(
        values[feature.camel_key] for feature in FEATURES if feature.key in STRATEGY_TOOLS_FEATURES
    )
    return values


def derived_features(store: RuntimeStore | None, user_id: str | None) -> dict[str, bool]:
    """The camel-case `features` object for a *user* (status payload) —
    global switches included, which the plan-level `Plan.features()` cannot
    see."""
    return _features_from(availability_map(store, user_id))


def status_payload(store: RuntimeStore | None, user_id: str | None) -> dict[str, Any]:
    """The auth-status skill facts: the available skill ids plus the legacy
    `features` object derived from them (one availability read for both)."""
    reasons = availability_map(store, user_id)
    return {
        "skills": [skill.skill_id for skill in all_skills() if reasons.get(skill.skill_id) is None],
        "features": _features_from(reasons),
    }


def require_skill(store: RuntimeStore | None, user_id: str | None, skill: Skill | str, *, human: bool = False) -> None:
    """The single skill gate at HTTP/voice entry points. Two distinguishable
    refusals: `skill_disabled` (the owner's global switch — everyone, owner
    included) and the legacy `feature_not_in_plan` wire code for a tier
    refusal, kept verbatim for 1.6.x clients (the machine-readable reason
    lives in GET /api/agent/skills as `not_in_plan`)."""
    if isinstance(skill, str):
        resolved = skill_by_id(skill)
        if resolved is None:
            raise HTTPException(status_code=404, detail="skill_unknown")
        skill = resolved
    reason = skill_block_reason(store, user_id, skill, human=human)
    if reason == "disabled":
        raise HTTPException(status_code=403, detail="skill_disabled")
    if reason is not None:
        raise HTTPException(status_code=403, detail="feature_not_in_plan")


def unavailable_line(skill: Skill, reason: str | None, parent_reason: str | None = None) -> str:
    """The one unified spoken/typed refusal line for a routed request whose
    skill is unavailable (voice and typed routers both use it). `parent_reason`
    is set when the skill itself is open and only its parent blocks it: the
    line then says so with the parent's name (设置拆页 2026-09-30 review)."""
    label = skill.label or skill.skill_id
    if parent_reason is not None:
        parent = skill_by_id(skill.parent) if skill.parent else None
        parent_label = (parent.label if parent else None) or skill.parent or "上级"
        if parent_reason == "disabled":
            return f"「{label}」随「{parent_label}」关闭"
        if parent_reason == "owner_only":
            return f"「{parent_label}」仅主账号可用"
        return f"当前档位未开通「{parent_label}」"
    if reason == "disabled":
        return f"「{label}」已停用"
    if reason == "owner_only":
        return f"「{label}」仅主账号可用"
    return f"当前档位未开通「{label}」"


def skills_block_line(store: RuntimeStore | None, user_id: str | None, skill_ids: list[str]) -> str | None:
    """The unified refusal line of the first of `skill_ids` unavailable to this user, or None when all are open —
    the card acts' gate (order_intent.card_skills) every order entry applies: the trading AI's propose_order, the
    typed fast path and the voice front desk."""
    for skill_id in skill_ids:
        skill = skill_by_id(skill_id)
        if skill is None:
            continue
        reason = skill_block_reason(store, user_id, skill)
        if reason is not None:
            return unavailable_line(skill, reason, parent_block_reason(store, user_id, skill))
    return None


# -- the app-level write gate ---------------------------------------------------

_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# Owned write routes the app-level gate deliberately leaves to their handler,
# each with its written reason (the registry still owns them for attribution
# and hiding — only the gate reads this set):
# - replays control gates per action: `close` stays open like every stop/exit
#   action, the rest follow the skill;
# - invite codes (create and disable) authenticate at the owner-session layer
#   (identity.role), not the skill layer: a legacy database can carry a
#   cloud_users row under the owner's own name, which the owner_only verdict
#   would misread as a member;
# - briefs refresh keeps the 1.6.8 client contract: a tier without the skill
#   reads a gentle `status: disabled` instead of an error; only the owner's
#   global switch is a hard 403.
HANDLER_GATED_WRITE_ROUTES = frozenset({
    "POST /api/replays/{identifier}/control",
    "POST /api/auth/owner/invite-codes",
    "POST /api/auth/owner/invite-codes/{id}/disable",
    "POST /api/app/briefs/refresh",
})


async def write_skill_gate(request: Request = None) -> None:  # type: ignore[assignment]
    """The one app-level skill gate, registered as an app-wide FastAPI
    dependency: a request whose matched route belongs to a skill
    (`route_owner_map`, `METHOD path`) and whose method writes is refused with
    that skill unavailable — the registry's route table is the gate, no
    per-route checks. Reads, unowned routes and the handler-gated routes above
    pass through. The None default is for the voice WebSocket route, which
    bakes app dependencies in but solves no Request — its own auth applies."""
    if request is None or request.method not in _WRITE_METHODS:
        return
    path = getattr(request.scope.get("route"), "path", None)
    if not path:
        return
    key = f"{request.method} {path}"
    if key in HANDLER_GATED_WRITE_ROUTES:
        return
    from .guest import guest_request
    if key == "POST /api/agent/call" and guest_request(request):
        request.app.state.guests.require(request)
        return  # The guest envelope is restricted to its own explicit HTTP catalog.
    owner = route_owner_map().get(key)
    if owner is None:
        return
    app = request.app
    user = await app.state.ctx.current_user(request)
    require_skill(app.state.store, user, owner, human=human_request(request))


# -- progressive disclosure (tiers 2 and 3) ------------------------------------


def skill_content_block(skill: Skill) -> str:
    """Tier 2: the full SKILL.md as the `<skill_content>` instruction block the
    spec's clients keep verbatim (its `<skill_resources>` lists tier 3)."""
    resources = "\n".join(f"- {path}" for path in skill.references) or "（无附加资源）"
    return (f'<skill_content name="{skill.skill_id}">\n{skill.body}\n'
            f"<skill_resources>\n{resources}\n</skill_resources>\n</skill_content>")


def read_skill_resource(skill: Skill, path: str) -> str:
    """Tier 3: one file from the skill's own directory (references ride first,
    but SKILL.md itself is readable too). Relative paths only — `..`, absolute
    paths and anything escaping the directory are refused, as is anything over
    RESOURCE_MAX_BYTES or outside the allowed suffixes."""
    target = (Path(path) if path else Path("."))
    if target.is_absolute() or ".." in target.parts:
        raise HTTPException(status_code=404, detail="skill_resource_not_found")
    resolved = (skill.directory / target).resolve()
    if resolved != skill.directory.resolve() and skill.directory.resolve() not in resolved.parents:
        raise HTTPException(status_code=404, detail="skill_resource_not_found")
    if not resolved.is_file() or resolved.suffix.lower() not in RESOURCE_SUFFIXES:
        raise HTTPException(status_code=404, detail="skill_resource_not_found")
    if resolved.stat().st_size > RESOURCE_MAX_BYTES:
        raise HTTPException(status_code=404, detail="skill_resource_too_large")
    return resolved.read_text(encoding="utf-8")


__all__ = [
    "Skill", "all_skills", "skill_by_id", "tool_owner_map", "route_owner_map",
    "seed_tier_configs", "migrate_config_features", "skill_allowed",
    "skill_block_reason", "own_block_reason", "parent_block_reason", "availability_map",
    "allowed_skills", "tier_gated_skills",
    "hidden_tool_names", "derived_features", "status_payload", "require_skill",
    "unavailable_line", "write_skill_gate", "HANDLER_GATED_WRITE_ROUTES",
    "AI_FEATURE_SKILLS", "human_request",
    "skill_content_block", "read_skill_resource",
    "skills_root", "SKILL_NAME_PATTERN", "DESCRIPTION_MAX", "COMPATIBILITY_MAX",
    "BODY_MAX_LINES",
]
