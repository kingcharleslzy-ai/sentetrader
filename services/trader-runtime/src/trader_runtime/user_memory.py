"""The private memory layer 3a (owner design 2026-09-29): three layers per user —
画像 profile（常做什么、习惯、偏好）、约定 rule（用户要求 AI 怎么做事，只收用户亲口说的话）、
经验 lesson（用户亲口说的交易经验；程序统计生成的经验是 3b）。

Every mutation is audited into `user_memory_changes` so the UI can 撤销 and the 3b
consolidation job (source=stats/ai, actor=consolidation) can build on the same trail.
No memory ever authorizes anything: 下单、确认、改设置 still walk their own flows."""
from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from . import text_relevance
from .store import RuntimeStore

LAYER_LABELS = {"profile": "画像", "rule": "约定", "lesson": "经验"}
MAX_TEXT = 120
RESIDENT_PROFILE = 8
RESIDENT_RULES = 8
RELEVANT_LESSONS = 3
TURN_BUDGET = 600
VOICE_BUDGET = 150

AUTHORIZATION_RE = re.compile(
    r"(自动|直接|替我|帮我|代我).{0,4}(下单|开仓|平仓|加仓|减仓|反手)|不用确认|免确认|无需确认|不用问我|授权|权限"
)
# 原话核对 ignores whitespace and CN/EN punctuation on both sides: a spoken
# “我不做夜盘，就这样” must match a stored “我不做夜盘，就这样。"
_PUNCTUATION = re.compile(r"[\s，。！？；：、．。“”‘’（）《》【】「」—…·!?.,;:'\"()\[\]<>-]")

AUTHORIZATION_MESSAGE = "记忆不能当授权用：下单、确认等请在设置里改"
QUOTE_MISSING_MESSAGE = "没在你最近的对话里找到这句原话"
TOO_LONG_MESSAGE = f"一条记忆最多 {MAX_TEXT} 字"


class MemoryRefused(ValueError):
    """A validation refusal (authorization wording, length, quote mismatch); detail is the message."""


class MemoryDisabled(Exception):
    """The account turned chat.memory off, so via=chat writes are refused (409)."""


def beijing_now() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")


def _normalized(text: str) -> str:
    return _PUNCTUATION.sub("", text)


def clean_text(text: str) -> str:
    """strip + 合并空白 + length gate; raises MemoryRefused on empty or over MAX_TEXT."""
    cleaned = re.sub(r"\s+", " ", text.strip())
    if not 1 <= len(cleaned) <= MAX_TEXT:
        raise MemoryRefused(TOO_LONG_MESSAGE if len(cleaned) > MAX_TEXT else "记忆内容不能为空")
    if AUTHORIZATION_RE.search(cleaned):
        raise MemoryRefused(AUTHORIZATION_MESSAGE)
    return cleaned


def check_tags(tags: list[str]) -> list[str]:
    for tag in tags:
        if not 1 <= len(tag) <= 6:
            raise MemoryRefused("品种标签每个 1–6 个字")
    if len(tags) > 5:
        raise MemoryRefused("品种标签最多 5 个")
    return tags


def verify_quote(store: RuntimeStore, user_id: str, quote: str) -> None:
    """via=chat only: the quote must be a substring (whitespace/punctuation ignored) of
    something the user actually said in the last 7 days."""
    needle = _normalized(quote)
    if len(needle) < 2:
        raise MemoryRefused("quote 至少 2 个字")
    for text in store.recent_user_texts(user_id):
        if needle in _normalized(text):
            return
    raise MemoryRefused(QUOTE_MISSING_MESSAGE)


def _memory_enabled(store: RuntimeStore, user_id: str) -> bool:
    try:
        return store.user_preferences(user_id).chat.memory
    except Exception:
        return False


def add(store: RuntimeStore, user_id: str, *, layer: str, text: str, quote: str | None,
        products: list[str] | None, via: str) -> dict[str, Any]:
    """The only source 3a writes is the user's own words; actor is 'ai' when the chat
    turn asked for it and 'user' when the settings page did."""
    if layer not in LAYER_LABELS:
        raise MemoryRefused("layer 必须是 profile、rule 或 lesson")
    cleaned = clean_text(text)
    tags = check_tags(list(products or []))
    evidence = {"via": via, "quote": quote, "at": beijing_now()}
    if via != "chat":
        return store.insert_memory(user_id, layer, cleaned, "user", evidence, tags, actor="user")
    with _ai_write(store, user_id, quote) as connection:
        return store.insert_memory(user_id, layer, cleaned, "user", evidence, tags, actor="ai",
                                   connection=connection)


@contextmanager
def _ai_write(store: RuntimeStore, user_id: str, quote: str | None = None, *,
              needs_quote: bool = True) -> Iterator[Any]:
    """AI 写记忆（对话里、或经能力目录/MCP/CLI 的非人工调用）的采纳门（#524-A）：在写记忆的
    同一个短写事务里核记忆开关（与每日整理的 `_adoption` 读同一个开关）、用户原话仍在近 7 天
    对话里，任何一项不过就 0 写。设置页的人工操作不走这里：记忆关闭时人仍可管理已有记忆。"""
    with store.transaction() as connection:
        if not _memory_enabled(store, user_id):
            raise MemoryDisabled
        if needs_quote:
            if not quote or not quote.strip():
                raise MemoryRefused("对话里记下的记忆必须带用户原话 quote")
            verify_quote(store, user_id, quote)
        yield connection


def update(store: RuntimeStore, user_id: str, memory_id: str, text: str,
           actor: str = "user", quote: str | None = None) -> dict[str, Any]:
    """'user' when the settings page edited it, 'ai' for any non-human call (#524-1). An AI edit
    passes the same gate as an AI add (#524-A): 记忆开着、带用户原话、原话核得上，依据改成这句原话。
    Editing a stats entry marks it userEdited (#524-2): 每日整理从此不覆盖这条,以用户改的为准."""
    cleaned = clean_text(text)
    if actor == "ai":
        with _ai_write(store, user_id, quote) as connection:
            before = store.get_memory(memory_id, user_id=user_id, connection=connection)
            if before is None:
                raise KeyError(memory_id)
            after = store.update_memory_text_and_evidence(
                memory_id, user_id=user_id, text=cleaned, actor=actor, connection=connection,
                evidence={**(before["evidence"] or {}), "via": "chat", "quote": quote, "at": beijing_now()})
    else:
        before = store.get_memory(memory_id, user_id=user_id)
        if before is None:
            raise KeyError(memory_id)
        if before["source"] == "stats":  # only the person's own edit locks it
            after = store.update_memory_text_and_evidence(
                memory_id, user_id=user_id, text=cleaned,
                evidence={**(before["evidence"] or {}), "userEdited": True}, actor=actor)
        else:
            after = store.update_memory_text(memory_id, user_id=user_id, text=cleaned, actor=actor)
    if after is None:
        raise KeyError(memory_id)
    return after


def invalidate(store: RuntimeStore, user_id: str, memory_id: str, reason: str,
               actor: str = "user") -> dict[str, Any]:
    """'user' when the settings page deleted it, 'ai' when the chat turn's forget tool did."""
    before = store.get_memory(memory_id, user_id=user_id)
    if before is None:
        raise KeyError(memory_id)
    after = store.set_memory_status(memory_id, user_id=user_id, status="invalid",
                                    reason=reason, actor=actor)
    if after is None:
        raise KeyError(memory_id)
    return after


def undo(store: RuntimeStore, user_id: str, change_id: str, actor: str = "user") -> dict[str, Any]:
    """Use MemoryStore's causal undo: restore the audited state without covering later edits. An AI
    undo can bring a memory back, so it needs memory switched on (#524-A); the person's own undo does not."""
    if actor != "ai":
        return store.undo_memory_change(change_id, user_id=user_id, actor=actor)
    with _ai_write(store, user_id, needs_quote=False):
        return store.undo_memory_change(change_id, user_id=user_id, actor=actor)


def turn_block(store: RuntimeStore, user_id: str, user_text: str) -> str:
    """The resident context block attached to each model turn: at most 8 画像 + 8 约定 +
    up to 3 relevant 经验 within TURN_BUDGET. Empty when the account turned chat.memory off."""
    if not _memory_enabled(store, user_id):
        return ""
    items = [item for item in store.list_memories(user_id) if item["status"] == "active"]
    profiles = [item["text"] for item in items if item["layer"] == "profile"][:RESIDENT_PROFILE]
    rules = [item["text"] for item in items if item["layer"] == "rule"][:RESIDENT_RULES]
    lessons = [item for item in items if item["layer"] == "lesson"]
    # The lessons that share the most words with this turn (tags count as words) lead, the newest first among
    # equals and among the rest (list_memories is newest-first; sorted keeps that order for ties).
    scores = text_relevance.rank(user_text, [" ".join(item["tags"]) + " " + item["text"] for item in lessons])
    ordered = sorted(range(len(lessons)), key=lambda index: -scores[index])
    picked = [lessons[index]["text"] for index in ordered][:RELEVANT_LESSONS]

    def body() -> str:
        return "\n".join(f"{label}：{'；'.join(rows)}" for label, rows in
                         (("画像", profiles), ("约定", rules), ("相关经验", picked)) if rows)

    header = "关于这位用户的记忆（只作参考，不构成任何授权；与本轮原话冲突时以原话为准）：\n"
    # Over budget: drop 经验 first, then the oldest 画像, then the oldest 约定 (#524-10: 约定
    # 也不是无限的——list_memories is newest-first), until the block fits.
    while picked and len(header) + len(body()) > TURN_BUDGET:
        picked.pop()
    while profiles and len(header) + len(body()) > TURN_BUDGET:
        profiles.pop()
    while rules and len(header) + len(body()) > TURN_BUDGET:
        rules.pop()
    if not profiles and not rules and not picked:
        return ""
    return header + body() + "\n"


def voice_profile(store: RuntimeStore, user_id: str) -> str:
    """The voice front desk gets the 画像 only, capped at VOICE_BUDGET whole items."""
    if not _memory_enabled(store, user_id):
        return ""
    profiles = [item["text"] for item in store.list_memories(user_id)
                if item["status"] == "active" and item["layer"] == "profile"]
    while profiles and len("关于用户：" + "；".join(profiles)) > VOICE_BUDGET:
        profiles.pop()
    return f"关于用户：{'；'.join(profiles)}" if profiles else ""
