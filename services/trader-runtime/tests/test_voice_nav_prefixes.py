"""保护：“带我去 / 帮我打开 / 我想看看 / 去看看 / 看一下 + 页面名”秒开对应页面，带具体问题的句子仍交给模型（10-06 实测）。"""
from __future__ import annotations

import pytest

from trader_runtime.voice_intents import match_intent

PAGES = [
    ("带我去看看我的策略", "panel", {"panel": "strategy"}),
    ("看一下策略", "panel", {"panel": "strategy"}),
    ("帮我打开复盘", "view", {"view": "review"}),
    ("我想看看我的复盘", "view", {"view": "review"}),
    ("我想看看调研", "mobileTab", {"mobileTab": "research"}),
    ("带我去看盘", "mobileTab", {"mobileTab": "market"}),
    ("去看看通知", "reminderCenter", {"reminderCenter": {"open": True}}),
    ("带我去提醒", "reminderCenter", {"reminderCenter": {"open": True}}),
    ("带我去时间线", "mobileTab", {"mobileNotifyFilter": "all"}),
    ("我想看看我的", "mobileTab", {"mobileTab": "me"}),
    ("带我去我的流程", "settingsPage", {"settingsPage": "memory"}),
]


@pytest.mark.parametrize(("said", "kind", "changes"), PAGES)
def test_a_spoken_page_jump_opens_the_page_at_once(said: str, kind: str, changes: dict) -> None:
    intent = match_intent(said)
    assert intent is not None and (intent.kind, intent.changes) == (kind, changes)


@pytest.mark.parametrize("said", [
    "带我看看螺纹为什么跌", "带我去看看我的策略为什么没开仓", "我想看看复盘里昨天那笔", "帮我看一下行情", "我的",
])
def test_a_sentence_with_its_own_question_stays_with_the_model(said: str) -> None:
    intent = match_intent(said)
    assert intent is None or intent.kind not in {"panel", "view", "mobileTab", "settingsPage"}, said
