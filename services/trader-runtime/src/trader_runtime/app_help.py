"""Shared product explanations for the UI, API and embedded agent.

The text lives in `help/catalog.json` next to this module (edit it there; keep it
`json.dumps(..., ensure_ascii=False, indent=2)` so diffs stay readable); this module loads,
validates and expands it once at import. Two layers per topic: the plain-language layer
people read first — `summary` (one sentence), `steps` (how to use it, in order) and `faq` —
and `detail`, the facts behind it. The plain layer never names routes, fields or UI commands.
Topics are listed group by group (`groups`; `ownerOnly` groups show only in the owner's
window, `collapsed` groups start folded — the agent and the API still read them in full).
Old topic ids keep working: most are still the ids of the topics that took over their
content, the rest resolve through `aliases` (`workspace`: an older `dialog: help` without a
topic; `appearance`: the old settings page name; `depth`: the 价格阶梯 panel merged into 下单
2026-10-07; `strategy-guide`: the guided design replaced by the 策略编辑卡 2026-10-08).

Generated content is an explicit placeholder in the data, never a frozen copy:
`{"generated": "<name>"}` as a list item splices that generator's paragraphs
(`skills`: the skill registry), `{"template": "…{<name>}…"}` is a string with that
generator's text filled in (`review_tags`: the review tag rules).
"""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .review_tags import TAG_RULES
from .skill_registry import all_skills

CATALOG = Path(__file__).resolve().parent / "help" / "catalog.json"


def _skills_paragraphs() -> list[str]:
    """The skills topic, generated from the registry (SKILL.md stays the single
    source of truth): one paragraph per category, every skill with its label,
    id, description and the 典型说法 bullets from its body. A child skill rides
    under its parent's line (它随上级一起开关)."""
    groups: dict[str, list[str]] = {}
    skills = all_skills()
    for skill in skills:
        if skill.parent:
            continue
        line = f"{skill.label}（{skill.skill_id}）：{skill.description}"
        children = [child for child in skills if child.parent == skill.skill_id]
        if children:
            line += " 下属技能（随它一起开关）：" + "；".join(
                f"{child.label}（{child.skill_id}）{f'——{child.summary}' if child.summary else ''}" for child in children
            ) + "。"
        if skill.phrases:
            line += " 典型说法：" + "；".join(skill.phrases) + "。"
        groups.setdefault(skill.category, []).append(line)
    return [f"{category}——" + " ".join(lines) for category, lines in groups.items()]


# Placeholder name → generator: paragraph lists splice into a list, text fills a template.
GENERATED_PARAGRAPHS: dict[str, Callable[[], list[str]]] = {"skills": _skills_paragraphs}
GENERATED_TEXT: dict[str, Callable[[], str]] = {"review_tags": lambda: "；".join(TAG_RULES)}

_TOPIC_FIELDS = {"title": str, "group": str, "summary": str, "steps": list, "faq": list, "detail": list}
_OPTIONAL_FIELDS = {"terms": list, "tour": list}


class HelpCatalogError(ValueError):
    """The help catalog file is malformed; the message names the topic and field."""


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise HelpCatalogError(f"{CATALOG.name}: duplicate key {key!r}")
        out[key] = value
    return out


def _expand(value: Any, where: str) -> Any:
    """Resolve placeholders anywhere in the tree, keeping key and list order."""
    if isinstance(value, list):
        out: list[Any] = []
        for index, item in enumerate(value):
            if isinstance(item, dict) and set(item) == {"generated"}:
                name = item["generated"]
                if name not in GENERATED_PARAGRAPHS:
                    raise HelpCatalogError(f"{where}[{index}]: unknown generated paragraphs {name!r}")
                out.extend(GENERATED_PARAGRAPHS[name]())
            else:
                out.append(_expand(item, f"{where}[{index}]"))
        return out
    if isinstance(value, dict):
        if set(value) == {"template"}:
            text = value["template"]
            if not isinstance(text, str) or not any(f"{{{name}}}" in text for name in GENERATED_TEXT):
                raise HelpCatalogError(f"{where}: template without a known placeholder {sorted(GENERATED_TEXT)}")
            for name, generate in GENERATED_TEXT.items():
                text = text.replace(f"{{{name}}}", generate())
            return text
        if set(value) == {"generated"}:
            raise HelpCatalogError(f"{where}: generated paragraphs only go inside a list")
        return {key: _expand(item, f"{where}.{key}") for key, item in value.items()}
    return value


def _validate(groups: Any, aliases: Any, topics: Any) -> None:
    if not isinstance(groups, list) or not all(isinstance(g, dict) and g.get("id") and g.get("title") for g in groups):
        raise HelpCatalogError("groups: every group needs an id and a title")
    group_ids = [group["id"] for group in groups]
    if len(set(group_ids)) != len(group_ids):
        raise HelpCatalogError(f"groups: duplicate id in {group_ids}")
    if not isinstance(topics, dict) or not topics:
        raise HelpCatalogError("topics: missing or empty")
    for topic, item in topics.items():
        if not isinstance(item, dict):
            raise HelpCatalogError(f"topics.{topic}: not an object")
        for field, kind in _TOPIC_FIELDS.items():
            if not isinstance(item.get(field), kind) or not item[field]:
                raise HelpCatalogError(f"topics.{topic}.{field}: missing or empty")
        unknown = set(item) - set(_TOPIC_FIELDS) - set(_OPTIONAL_FIELDS)
        if unknown:
            raise HelpCatalogError(f"topics.{topic}: unknown field(s) {sorted(unknown)}; "
                                   f"allowed {sorted({**_TOPIC_FIELDS, **_OPTIONAL_FIELDS})}")
        for field, kind in _OPTIONAL_FIELDS.items():
            if field in item and not isinstance(item[field], kind):
                raise HelpCatalogError(f"topics.{topic}.{field}: must be a {kind.__name__}")
        if item["group"] not in group_ids:
            raise HelpCatalogError(f"topics.{topic}.group: unknown group {item['group']!r}")
        if not all(isinstance(step, str) and step for step in item["steps"]):
            raise HelpCatalogError(f"topics.{topic}.steps: every step is a non-empty string")
        if not all(isinstance(entry, dict) and set(entry) == {"q", "a"} for entry in item["faq"]):
            raise HelpCatalogError(f"topics.{topic}.faq: every entry is {{q, a}}")
        if not all(isinstance(term, dict) and {"term", "plain", "when"} <= set(term) for term in item.get("terms", [])):
            raise HelpCatalogError(f"topics.{topic}.terms: every term has term, plain and when")
        if not all(isinstance(line, str) and line for line in item["detail"]):
            raise HelpCatalogError(f"topics.{topic}.detail: every paragraph is a non-empty string")
    if not isinstance(aliases, dict):
        raise HelpCatalogError("aliases: not an object")
    for old, topic in aliases.items():
        if old in topics or topic not in topics:
            raise HelpCatalogError(f"aliases.{old}: must be a non-topic id pointing at a topic (got {topic!r})")


def load_catalog(path: Path = CATALOG) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], dict[str, str]]:
    """Read, expand and validate the catalog: (groups, topics, aliases)."""
    raw = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_no_duplicates)
    if not isinstance(raw, dict) or set(raw) != {"groups", "aliases", "topics"}:
        raise HelpCatalogError(f"{path.name}: top level must be exactly groups, aliases and topics")
    groups, aliases, topics = (_expand(raw[key], key) for key in ("groups", "aliases", "topics"))
    _validate(groups, aliases, topics)
    return groups, topics, aliases


HELP_GROUPS: list[dict[str, Any]]
HELP: dict[str, dict[str, Any]]
HELP_ALIASES: dict[str, str]
HELP_GROUPS, HELP, HELP_ALIASES = load_catalog()


def resolve_topic(topic: str) -> str | None:
    """A topic id or an old alias to the topic id; None when there is no such topic."""
    topic = HELP_ALIASES.get(topic, topic)
    return topic if topic in HELP else None


def topic_text(topic: str) -> list[str]:
    """One topic as ordered text — summary, 怎么用, 常见问题, (术语,) then the complete detail. Agents read
    it in this order, and clients before the two-layer card render it as their paragraphs."""
    item = HELP[topic]
    lines = [item["summary"], "怎么用：" + " ".join(f"{index}. {step}" for index, step in enumerate(item["steps"], 1))]
    lines += [f"常见问题：{entry['q']} {entry['a']}" for entry in item["faq"]]
    lines += [f"术语：{term['term']}——{term['plain']}（什么时候会碰到：{term['when']}）" for term in item.get("terms", [])]
    return lines + list(item["detail"])


HELP_SEARCH_LIMIT = 16
HELP_SEARCH_CONTEXT = 150
# A sentence that says where something is ("设置 → 通知 → …", "二级页“高级交易设置”") answers "在哪" — it ranks first.
_HELP_LOCATION_MARKS = ("设置 →", "设置→", " → ", "二级页")


def help_search(term: str) -> dict[str, Any]:
    """The manual's sentences around a word (2026-10-14): "保本提醒在哪设" finds 设置 → 通知 → 保本提醒
    without reading the whole manual. One sentence per topic (a location sentence preferred), topics that
    say where something is first, then by relevance (2026-10-25: hits in the title, summary or steps, then
    hits in the whole topic, then catalog order), at most HELP_SEARCH_LIMIT — a common word's mentions in
    topics earlier in the catalog must not crowd out the topic that answers it."""
    needle = term.strip().lower()
    best: list[tuple[int, int, int, int, str, str, str]] = []
    for order, (key, item) in enumerate(HELP.items()):
        lines = topic_text(key) if needle else []
        chosen: tuple[int, str, int] | None = None
        for line in lines:
            at = line.lower().find(needle)
            if at < 0:
                continue
            located = int(any(mark in line for mark in _HELP_LOCATION_MARKS))
            if chosen is None or located > chosen[0]:
                chosen = (located, line, at)
            if located:
                break
        if chosen is None:
            continue
        located, line, at = chosen
        start = max(0, at - HELP_SEARCH_CONTEXT)
        text = ("…" if start else "") + line[start:at + len(needle) + HELP_SEARCH_CONTEXT] + (
            "…" if at + len(needle) + HELP_SEARCH_CONTEXT < len(line) else "")
        head = sum(part.lower().count(needle) for part in (item["title"], *lines[:2]))  # title, summary, steps
        hits = sum(line.lower().count(needle) for line in lines)
        best.append((-located, -head, -hits, order, key, item["title"], text))
    best.sort()
    matches = [{"topic": key, "title": title, "text": text} for *_, key, title, text in best[:HELP_SEARCH_LIMIT]]
    return {"search": term, "matches": matches, "more": max(0, len(best) - HELP_SEARCH_LIMIT),
            "next": "要看某个主题全文：同一接口带 query.topic=<主题 id>。没搜到换个说法再搜，不要据此断定没有这个功能。"}


def help_outline() -> dict[str, Any]:
    """The manual as a table of contents (2026-10-14 token 瘦身): groups and every topic's id, title,
    group and one-line summary — what the embedded AI gets for a `GET /api/app/help` without a topic
    (the whole manual is ~300k characters); one topic's full text is the same route with `topic`."""
    return {"groups": HELP_GROUPS,
            "topics": {key: {"title": item["title"], "group": item.get("group"), "summary": item["summary"]}
                       for key, item in HELP.items()},
            "next": "读某个主题的全文：同一接口带 query.topic=<主题 id>（旧 id 也行）；"
                    "找某个设置或功能在哪：带 query.search=<关键词>（如 费率、保本提醒）返回提到它的句子。"}


def help_snapshot(topic: str | None = None) -> dict[str, Any]:
    """`GET /api/app/help`: the groups, the aliases and every topic (or only `topic`) with its
    fields in reading order and `paragraphs` = `topic_text`."""
    ids = list(HELP) if topic is None else [topic]
    return {"groups": HELP_GROUPS, "aliases": HELP_ALIASES,
            "topics": {key: {**HELP[key], "paragraphs": topic_text(key)} for key in ids}}
