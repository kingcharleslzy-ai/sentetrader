"""Real-time voice (V1): Doubao Seeduplex is the front desk, the text agent is the only brain.

The Runtime holds the Doubao API key and proxies audio: client PCM16/16 kHz → Runtime → Doubao,
Doubao float32 PCM/24 kHz → Runtime → client (tagged "encoding"). Doubao listens, speaks, handles
barge-in, small talk and clarification; every business request is `delegate`d as one turn of the
user's own chat session (`chat:send`, origin "voice"), so the voice has exactly the agent's
capabilities and boundaries — the simulated account and "an order needs an explicit confirmation".
The agent answers with `content` (rendered in the conversation as usual) plus `spoken` (1–3
sentences); the voice reads `spoken` and keeps the content as its tool result for follow-ups.
Orders come back as proposal cards; "确认" / "算了" confirm or drop the card the same way a click does.

The chain, stage by stage (one owner each, 2026-11-01):
1. 接入 — the call socket, auth, the per-user slot, the Doubao session and its watchdog: here (`_run_call`).
2. 听 — recognizer words, the cleaned transcript and "this partial only replays what was answered": voice_asr;
   when a partial counts as final (early / stall / Doubao's own): here.
3. 分 — which route a final takes: voice_route (`route_utterance`, `classify_final`), reading the words through
   voice_intents (interface table, quick queries), order_intent_parse (orders) and voice_front (front-desk picks).
4. 做 — interface words: turn_router `run_window_intent` / `send_ui_command` / `run_editor_ask`, the typed turn's
   own path; queries and order cards the front desk answers itself: `front_query` / `front_action` here (contracts
   by order_planner `resolve_words` / voice_intents `resolve_symbol`, like every other caller); everything else:
   `_delegate` → chat:send (native_agent `classify_turn`); the call's waiting cards (确认 / 取消 / 一张一张问):
   here, over the card routes a click uses.
5. 说 — sentence streaming and interim lines: here; push-to-talk speech: voice_speak; push-to-talk transcription:
   voice_transcribe.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import contextvars
import difflib
import inspect
import json
import logging
import math
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect

from . import user_memory
from .agent_api import CHAT_SEND, AppCall, _proposal_summary
from .auth import COOKIE_NAME, LOCAL_USER_ID, OWNER_COOKIE_NAME, LocalAuth
from .instruments import foreign_codes_named
from .native_agent import PROCESSING_PLACEHOLDER
from .order_intent import OrderIntent
from .order_intent_parse import commands_order
from .order_planner import (
    Ask,
    Cards,
    Refuse,
    catalog_names,
    needs_account,
    product_of,
    resolve_scope,
    resolve_words,
    side_unsettled,
)
from .plans import daily_token_limit, plan_for_user
from .routes.requests import VoicePreferenceRequest
from .skill_registry import (
    hidden_tool_names,
    parent_block_reason,
    skill_allowed,
    skill_block_reason,
    skill_by_id,
    unavailable_line,
)
from .strategy_workbench import catalog_contract_label
from .trader_tools import _market_analysis, card_events, card_skill_block
from .turn_router import (
    catalog_product_names,
    classify_turn,
    run_editor_ask,
    run_tour_step,
    run_window_intent,
    send_ui_command,
)
from .user_preferences import UserPreferences
from .voice_asr import (
    ASR_CORRECT_WORDS,
    TRADING_HOTWORDS,
    after_bare,
    answered_prefix,
    bare_form,
    clean_transcript,
    prefix_match,
    replays_answered,
)
from .voice_front import (
    ACTION_TOOL,
    FRONT_ACTION_INSTRUCTIONS,
    FRONT_INSTRUCTIONS,
    QUERY_TOOL,
    analysis_fields,
    brief_fields,
    card_line,
    change_words,
    front_action_enabled,
    front_arguments,
    front_order,
    front_query_enabled,
    news_fields,
    orders_fields,
    other_legs_line,
    pnl_words,
    price_unit,
    refusal_line,
    reopen_words,
    session_note,
    spoken_contract,
    spoken_line,
    takeover_line,
)
from .voice_intents import (
    Intent,
    looks_like_ui,
    match_intent,
    match_quick_query,
    match_replay_control,
    resolve_symbol,
    review_control,
    review_offer_answer,
    review_start_scope,
)
from .voice_route import (
    CANCEL_ALL_WORDS,
    CANCEL_RUN_WORDS,
    EARLY_ROUTES,
    MERGEABLE_ROUTES,
    REVIEW_ROUTES,
    ROUTED_ACTION_SKILLS,
    classify_final,
    sentence_intent,
    strip_punctuation,
)

log = logging.getLogger("uvicorn.error.voice")

DOUBAO_URL = "wss://openspeech.bytedance.com/api/v3/duplex/realtime/dialogue"
DEFAULT_KEY_FILE = Path.home() / ".codex/local-secrets/volcengine-ark-api-key"
KEY_ENV = "AI_TRADER_VOICE_REALTIME_KEY"
# A signed-in cloud user holds at most this many calls at once; the local runtime has one user anyway.
CLOUD_CALLS_PER_USER = 1
AUTH_SUBPROTOCOL = "ai-trader-token"
AUTH_TOKEN_PREFIX = "bearer."

MODEL = "1.2.6.1"
# Measured on 2026-09-16 against the live service: every output_audio.delta decodes to a multiple of
# 4 bytes and only reads as speech when interpreted as float32 (int16 gives white noise).
OUTPUT_ENCODING = "f32le"

# Doubao O-line voices (docs.volcengine.com/docs/6561/1594356, "音色"); the first is the default.
VOICES: list[dict[str, str]] = [
    {"id": "zh_female_vv_jupiter_bigtts", "label": "Vivi（女声，活泼）"},
    {"id": "zh_female_xiaohe_jupiter_bigtts", "label": "小何（女声，甜美，台湾口音）"},
    {"id": "zh_male_yunzhou_jupiter_bigtts", "label": "云舟（男声，沉稳）"},
    {"id": "zh_male_xiaotian_jupiter_bigtts", "label": "小天（男声，磁性）"},
]
VOICE = VOICES[0]["id"]
FRAME_BYTES = 640  # 20 ms of PCM16 at 16 kHz
ANNOUNCE_THROTTLE_SECONDS = 60.0
ANNOUNCE_POLL_SECONDS = 5.0
# A spoken read-back waits for "确认" for `preferences.voice.confirmWindowSeconds` (default 15 s) after it
# finished playing; then the card is dropped.
PENDING_HARD_CAP_SECONDS = 600.0
# The agent turn a delegate waits for; the Runtime keeps the call alive meanwhile and, every
# PROGRESS_SECONDS, tells the client that the answer is still coming. Injecting a spoken "还在查" into
# Doubao while the tool is pending is off by default: measured 2026-09-17, the injected line closes the
# pending response (response.done), and the result injected afterwards is never spoken (pitfall ⑬).
# The model says "好，我去查" itself before the call; the client shows the working state.
DELEGATE_TIMEOUT_SECONDS = 150.0
# Endpoint watchdog — a safety net only (09-21). Doubao's own dynamic endpointing decides when a
# sentence ends; the Runtime forces the endpoint (`input_audio_buffer.commit`, 强制判停) only when a
# transcript is open and no delta has arrived for `preferences.voice.endpointStallSeconds`
# (2.0–5.0, default 3.0). The 09-17 measurement that introduced it (a 0.5 s "确认" clip fed ten
# times stopped producing `transcription.completed`) is still covered at the longer window, while a
# mid-sentence pause no longer ends the turn — the second half is merged back (utteranceMerged below).
# Doubao `audio.output.speed`, range [-50, 100] (0 = normal); the three spoken-speed choices map here.
SPEECH_SPEEDS = {"slow": -20, "normal": 0, "fast": 30}
ASR_WATCHDOG_TICK = 0.1
# #523 R4 (observation only): control messages whose completed upstream send is recorded in the
# timeline (type and turn, never the text) and the provider events recorded right after JSON parse,
# before any local filtering — the two together tell "the provider never sent audio" apart from
# "the Runtime dropped it here" on the next live reproduction.
UPSTREAM_SENT_TRACE_TYPES = frozenset({"speech_text_buffer.commit", "response.cancel", "session.close"})
PROVIDER_TRACE_EVENTS = frozenset({"response.output_audio.started", "response.output_audio.done",
                                   "response.done", "response.canceled", "error", "session.closed"})


def _answers_expectation(expected: str, event: dict[str, Any]) -> bool:
    """上游看门狗: does this provider event show the session answered what the Runtime is waiting for? Only an
    event of that request counts — after a commit, its read-out starting (Doubao's own replies are tts_type
    `default`) or the user's recognised words; a wordless `transcription.started` (Doubao sends one as any response
    starts or is cancelled) or the acknowledgement of an earlier cancel does not (#904 review: either one can
    arrive right after the commit of a session that then stays silent, as in f1353ada). The user saying something
    new also releases a commit — decided where the words are read, since Doubao re-sending the sentence the Runtime
    already answered (a late completed or its partials) is not new (10-15 retest d5902f9b). A session that closes
    answers nothing: it is lost, and its unheard lines are said again on the next one (#754)."""
    kind = event.get("type")
    if kind == "error":
        return True
    if expected == "session":
        return kind == "session.created"
    if kind == "response.output_audio.started":
        return event.get("tts_type") != "default"
    return False


def _trace_short(value: Any) -> str | None:
    """A provider id/status/enum kept to a bounded length, and only as a plain string — an absent
    or non-string value (a nested object must never be flattened into timeline text) stays None."""
    return value[:40] if isinstance(value, str) and value else None


def _provider_trace_fields(event: dict[str, Any]) -> dict[str, Any]:
    """What a provider event says about itself — its type, ids and enums only, each a bounded
    plain string; never the event's own text or audio. The provider's formal status lives on the
    nested `response.status` (response.done), with a top-level `status` only as a fallback."""
    response = event.get("response")
    response = response if isinstance(response, dict) else {}
    status = _trace_short(response.get("status")) or _trace_short(event.get("status"))
    return {"msg": _trace_short(event.get("type")), "rid": _trace_short(event.get("response_id")),
            "nid": _trace_short(response.get("id")), "status": status,
            "tts": _trace_short(event.get("tts_type"))}


def _audio_frame_bytes(delta: Any) -> int:
    """The decoded byte count of one delivered audio data frame — the base64 is decoded
    transiently for the length only and never kept. A delta that is not a plain string, or not
    valid base64, counts 0 bytes (its frame still counts); counting never feeds the business path."""
    if not isinstance(delta, str) or not delta:
        return 0
    try:
        return len(base64.b64decode(delta, validate=True))
    except Exception:
        return 0


# Uplink microphone accounting (10-14: a Windows web call where Doubao heard nothing — the timeline
# had no transcription and only the downlink was counted, so "the client sent no audio" could not be
# told from "it sent silence"). Levels are PCM16 RMS on a sample of the frames: every
# MIC_LEVEL_EVERY-th frame (20 ms frames → 10 a second), every MIC_LEVEL_STRIDE-th sample of it.
# A frame below MIC_SILENT_RMS (≈ −70 dBFS) is digital silence: a muted or wrong input device
# delivers that, a real room never does.
MIC_LEVEL_EVERY = 5
MIC_LEVEL_STRIDE = 4
MIC_SILENT_RMS = 10


def _pcm16_rms(pcm: bytes) -> float:
    """RMS of little-endian PCM16 on every MIC_LEVEL_STRIDE-th sample; 0 for an empty frame."""
    count = len(pcm) // 2
    if not count:
        return 0.0
    picked = [int.from_bytes(pcm[i:i + 2], "little", signed=True) for i in range(0, count * 2, 2 * MIC_LEVEL_STRIDE)]
    return math.sqrt(sum(value * value for value in picked) / len(picked))


@dataclass
class MicStats:
    """One call's microphone frames as the Runtime received them: counts, first arrival and sampled
    levels only — never the audio. Frames sent while the caller muted are counted apart and left out
    of the levels (a muted track delivers zeros on purpose)."""
    started: float
    frames: int = 0
    byte_count: int = 0
    first_at: float | None = None
    muted: bool = False
    muted_frames: int = 0
    sampled: int = 0
    silent: int = 0
    rms_sum: float = 0.0
    rms_peak: float = 0.0

    def add(self, pcm: bytes, at: float) -> None:
        self.frames += 1
        self.byte_count += len(pcm)
        if self.first_at is None:
            self.first_at = round(at - self.started, 2)
        if self.muted:
            self.muted_frames += 1
            return
        if (self.frames - 1) % MIC_LEVEL_EVERY:
            return
        rms = _pcm16_rms(pcm)
        self.sampled += 1
        self.rms_sum += rms
        self.rms_peak = max(self.rms_peak, rms)
        if rms < MIC_SILENT_RMS:
            self.silent += 1

    def add_base64(self, data: Any, at: float) -> None:
        """The `{"type": "audio"}` form: decoded transiently for the count and level only."""
        try:
            pcm = base64.b64decode(data, validate=True) if isinstance(data, str) else b""
        except Exception:
            pcm = b""
        self.add(pcm, at)

    def summary(self) -> dict[str, Any]:
        return {"micFrames": self.frames, "micBytes": self.byte_count, "micFirstAt": self.first_at,
                "micMutedFrames": self.muted_frames,
                "micRmsAvg": round(self.rms_sum / self.sampled) if self.sampled else None,
                "micRmsPeak": round(self.rms_peak) if self.sampled else None,
                "silentRatio": round(self.silent / self.sampled, 2) if self.sampled else None}
# The server-side endpointing window: the old protocol's `asr.extra.end_smooth_window_ms` (needs
# `enable_custom_vad: true`, range [500, 50000], default 1500). The 3.0 接入必读 says the extension's
# internal structure matches the old StartSessionPayload, so existing config carries over; measured
# 09-21 on the live service the keys are accepted (session.created, no error event).
def _env_number(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


# Off by default (09-21 real calls): the keys are accepted but the ASR then stalls on the next
# utterance — a `transcription.started` on pure silence and no `completed` even after a forced
# `input_audio_buffer.commit`; without the keys the same phrases finalise cleanly. Kept behind the
# env for model builds that honour the window.
END_SMOOTH_MS = 0 if _env_number("AI_TRADER_VOICE_END_SMOOTH_MS", 0) <= 0 \
    else min(max(int(_env_number("AI_TRADER_VOICE_END_SMOOTH_MS", 0)), 500), 50000)
# `extension.dialog.extra.enable_loudness_norm`: even out the read-out's loudness. `…LOUDNESS_NORM=0`
# leaves it off.
LOUDNESS_NORM = os.getenv("AI_TRADER_VOICE_LOUDNESS_NORM", "1") != "0"
# 续句：the user pauses mid-sentence, Doubao ends the first half as its own final; when the second
# final arrives this soon, the not-yet-answered round is cancelled and both halves routed as one
# sentence (chained merges allowed up to this many characters in all).
# Real-call measure (09-21): Doubao finalises the first half ~1.2 s after its speech ends, so the gap
# between the two finals is the pause plus ~1.2 s — 3.0 s covers pauses up to ~1.8 s.
CONTINUATION_SECONDS = _env_number("AI_TRADER_VOICE_CONTINUATION_SECONDS", 3.0)
CONTINUATION_MAX_CHARS = 60
# 提前判停 (09-28, measured against the live service): Doubao's partials already carry the second-pass
# sentence ("确认。") ~0.3 s after the speech ends, but its own completed follows only 1.3 s later, and
# after the Runtime has spoken a line (speech_text_buffer.commit) often 2.5–4 s later; neither a forced
# `input_audio_buffer.commit` nor a mute pulse brings it forward (`input_audio_buffer.clear` is refused).
# A turn the Runtime answers itself is therefore routed from the partial once its words stayed the same
# this long after a sentence-ending delta; Doubao's late completed of the same words is then dropped.
EARLY_FINAL_SECONDS = _env_number("AI_TRADER_VOICE_EARLY_FINAL_SECONDS", 0.6)
# Doubao's completed of an early-answered segment is recognised this long after the early final.
LATE_FINAL_WINDOW_SECONDS = 10.0
# 强制判停后仍无定稿 (10-08, the owner's desktop calls on c37ee88): after the Runtime has read an answer
# out, Doubao often leaves the user's next sentence open — partials, a forced commit, then no
# completed for 1.4–6.5 s until the user hung up. The Runtime then takes the words from the last
# partial this long after the forced commit (no new words in between) and answers them itself.
STALL_FINAL_SECONDS = _env_number("AI_TRADER_VOICE_STALL_FINAL_SECONDS", 1.2)
SENTENCE_END = ("。", "？", "！", "?", "!")
# An answer is recorded once the user has been quiet this long (a pause mid-answer splits it into two
# finals, merged within CONTINUATION_SECONDS): the merged sentence cancels the first round before it saves.
REVIEW_SETTLE_SECONDS = 1.2
# Inside a review a question back to the AI ("为什么螺纹今天跌了？") is not an answer: it keeps its route.
REVIEW_QUESTION = re.compile(r"^(为什么|为啥|怎么|什么|哪|多少|是不是|能不能|要不要|会不会)|[吗呢嘛]$")
# After the day session closes and before the night session: the first call offers the review once a day.
REVIEW_OFFER_WINDOW = ((15, 0), (20, 55))
CHINA = timezone(timedelta(hours=8))
# The replay engine's own speed steps (control route contract); a relative "快一点" moves within them.
_SPEED_ORDER = (1, 2, 5, 10)
PROGRESS_SECONDS = 8.0
PROGRESS_INJECT = os.getenv("AI_TRADER_VOICE_PROGRESS_INJECT", "0") == "1"
# The stale-quote watch (09-21): a confirm that failed on a quiet quote keeps its card; while the
# quote is the reason, the confirm window follows the quote instead of the clock, and the first
# current quote says "有新报价了" once. The card re-send is throttled to one per 10 s.
QUOTE_WATCH_POLL_SECONDS = 2.0
QUOTE_REMARK_SECONDS = 10.0
# Measurement switch: hand Doubao no tools at all, so every action goes through the Runtime router
# and its out-of-turn speech (the path that matters when the model only says the filler).
MODEL_TOOLS = os.getenv("AI_TRADER_VOICE_MODEL_TOOLS", "1") != "0"

# V1.4 (09-19): Doubao's context window is 12K, so the brief and every tool description is kept short
# and says each thing once. Read-only lookups stay with the front desk; interface work and
# business requests share the Runtime agent, which routes and speaks for the whole request.
INSTRUCTIONS = (
    "你是 Sente 的语音前台，中文口语，一次一两句，不编数字、不做分析。"
    "你只做四件事：寒暄；听不清就反问；查行情持仓资金（get_quote / get_positions / get_account）；"
    "口头确认或放弃（confirm / cancel）。"
    "听到查询请求立刻调对应工具，调工具之前一个字都不要说。查询拿到结果只念返回的数字；"
    "界面操作由系统处理并播报，你不要自行操作或再出声。工具返回 error 就照原话说数据过时或不可用。"
    "下单、平仓、撤单、新闻、研究、策略、走势判断由后台交易 AI 处理，你听到这类请求时一声不吭，系统会自己回答，也不要复述它念出的内容。"
    "系统念完待确认的下单内容后，用户说“确认/是/好”就调 confirm，说“取消/算了”就调 cancel；没有等确认的操作时不要调。"
    "read_screen 只在分不清用户指界面哪一处时用。"
)

# `delegate` is the Runtime's own action (route_utterance → run_routed), no longer a model tool: with it
# declared, Doubao kept calling it — three paraphrased calls on the 1.6.5 replay — and its turn stayed open
# until the agent finished, which held the read-out back. run_tool still accepts "delegate" for tests
# and the routed path.
TOOLS: list[dict[str, Any]] = [
    {"type": "function", "name": "confirm",
     "description": "确认刚念出的待确认方案（等于点方案卡的确认）。",
     "parameters": {"type": "object", "properties": {}, "required": []}},
    {"type": "function", "name": "cancel",
     "description": "放弃刚念出的待确认方案（等于点方案卡的放弃）。",
     "parameters": {"type": "object", "properties": {}, "required": []}},
    {"type": "function", "name": "read_screen",
     "description": "读当前界面：合约、页面、打开的面板与可见文字。只用于分清用户指哪里。",
     "parameters": {"type": "object", "properties": {"focus": {"type": "string", "description": "想看的部分，可留空"}}, "required": []}},
    # V1.3: three read-only lookups Doubao answers itself (the Runtime routes such questions as `quick`).
    {"type": "function", "name": "get_quote",
     "description": "查一个合约的最新价、涨跌、买卖一价。问价格、涨跌、行情时先调它。",
     "parameters": {"type": "object", "properties": {"query": {"type": "string", "description": "用户说的品种名，如 螺纹、沪金2612；没说就留空，表示在看的合约"}}, "required": []}},
    {"type": "function", "name": "get_positions",
     "description": "查持仓：每个合约的方向、手数、浮动盈亏。问手里有什么、亏赚多少时调它。",
     "parameters": {"type": "object", "properties": {"query": {"type": "string", "description": "只看某个品种时填品种名，可留空"}}, "required": []}},
    {"type": "function", "name": "get_account",
     "description": "查账户：权益、今日盈亏、浮动盈亏、保证金占用。",
     "parameters": {"type": "object", "properties": {}, "required": []}},
]
QUICK_TOOLS = frozenset({"get_quote", "get_positions", "get_account"})
# 前台直接查询 (AI_TRADER_VOICE_FRONT_QUERY, 2026-10-13): the three lookups become one `query` tool
# (what + contract) that also reads orders, news, 简讯 and the deterministic trend summary.
FRONT_TOOLS: list[dict[str, Any]] = [tool for tool in TOOLS if tool["name"] in {"confirm", "cancel", "read_screen"}] + [QUERY_TOOL]
# 下单类直接出卡 (AI_TRADER_VOICE_FRONT_ACTION, 2026-10-13): `action` is declared for the whole call. Mounting it
# by session.update only when a partial showed trade words was measured against the live Doubao the same
# day and lost: the update mid-sentence cut the ASR segment ("空两手白银，挂对手价" → "挂对手价。", "开三手，
# 不对，两手沪金多单" → "三手不对。") and 3 of 5 action calls after a swap carried another tool's arguments
# ({"query": "白银", "what": "quote"}); declared from the start, 7 of 7 calls had the action schema and the
# ASR stayed whole (~/ai-trader-release-artifacts/voice-action-20261013/static1.log, dynamic1.log).
FRONT_ACTION_TOOLS: list[dict[str, Any]] = [*FRONT_TOOLS, ACTION_TOOL]
# Tools whose turn the Runtime may already have taken over: answered silently instead of run twice.
MODEL_ROUTED_TOOLS = QUICK_TOOLS | {"query", "action"}
QUICK_TOOL_BY_KIND = {"quote": "get_quote", "positions": "get_positions", "account": "get_account"}
QUICK_MAX_AGE_SECONDS = 15.0

SPOKEN_LIMIT = 320


def _order_outcome_line(order: dict[str, Any], label: str = "") -> str:
    """What the call says about one sent order. 已受理 is a resting limit order, not a fill: on the
    user's 1.6.0 call two orders sat 52 s and 6 min at their limit price and the position only
    changed on the fill, which read as "the app is slow". The 09-21 call added the promise the
    watchdog keeps: 15 s unfilled and the voice says so itself. The contract is said by its catalog
    name (`label`, 白银2612), not read out as a code (10-16: "SHFE.ag2612 已挂单")."""
    symbol = label or str(order.get("symbol") or "")
    status = str(order.get("displayStatus") or "结果未知")
    price = order.get("limitPrice")
    if status == "已受理":
        return f"{symbol} 已挂单{f'，限价 {_price_text(price)}' if price else ''}，还没成交，15 秒没成交我会提醒你".strip()
    if status == "待发送":
        # Confirmed but never sent (an interrupted batch, or a check that failed before
        # the gateway call). Saying "已确认" alone would read as "it went out".
        if str(order.get("error") or "") == "paper_settlement_pending":
            # 本站模拟在等交易所公布结算价（2026-10-08 结算改版）：确定性拒收，
            # 念出可读原因而不是让用户以为软件坏了。
            return f"{symbol} 没有发出去：本站模拟正在等交易所公布结算价（约 19:00），公布后可以在委托栏点继续发送"
        return f"{symbol} 没有发出去，可以在委托栏继续发送或放弃".strip()
    return f"{symbol} {status}".strip()


REFERENTIAL_WORDS = re.compile(r"刚才|刚刚|那个|这个|它|上一个|前面")
DIALOG_CONTEXT_ROUNDS = 6
DIALOG_CONTEXT_CHARS = 200
DIALOG_CONTEXT_TOTAL = 2000
DIALOG_CONTEXT_EVENTS = 300  # events read from the tail of the session to find the last rounds
# ASR context wire format: the 3.0 doc types `extension.asr.extra.context` as a string while its example
# shows an object; "object" is what the 09-19 real call accepted (see CHANGELOG), "string" is the fallback.
ASR_CONTEXT_FORMAT = os.getenv("AI_TRADER_VOICE_ASR_CONTEXT_FORMAT", "object")
# `enable_asr_twopass` (the 3.0 doc's non-streaming recognition pass; also required by a console
# boosting table). On since 09-24: in the synthesized-voice A/B (release artifacts voice-asr-ab-20260924)
# it raised all-keywords-right sentences from 10/24 to 18/24 (clear) and 9/24 to 12/24 (mumbled), with
# the same ~1.3 s final latency, and a 48-utterance session kept every final and its latency flat — the
# 09-19 slowdown (voice-v13) did not reproduce. `…ASR_TWOPASS=0` turns it off.
ASR_TWOPASS = os.getenv("AI_TRADER_VOICE_ASR_TWOPASS", "1") == "1"
async def asr_settings(service: VoiceRealtimeService) -> tuple[dict[str, Any], dict[str, Any]]:
    """The ASR context (hotwords + replacement words) and the `extension.asr.extra` that a call and a
    push-to-talk transcription both send: one recognizer setup for the same words."""
    asr_context = await service.asr_context() if ASR_CONTEXT_FORMAT != "none" else {"hotwords": [], "correct_words": {}}
    context_value: Any = asr_context if ASR_CONTEXT_FORMAT == "object" else json.dumps(asr_context, ensure_ascii=False)
    extra = {**({"enable_asr_twopass": True} if ASR_TWOPASS else {}), **({"context": context_value} if ASR_CONTEXT_FORMAT != "none" else {}),
             # The server-side endpointing window (09-21): Doubao's dynamic VAD decides when the
             # sentence ends; the call's Runtime watchdog is only the safety net.
             **({"enable_custom_vad": True, "end_smooth_window_ms": END_SMOOTH_MS} if END_SMOOTH_MS else {})}
    return asr_context, extra


def _usd(value: Any, *, signed: bool = False) -> str:
    """An amount of the 外盘 USD ledger: the same digits as ``_money``, in 美元."""
    return _money(value, signed=signed).removesuffix(" 元").removesuffix("元") + " 美元"


def _money(value: Any, *, signed: bool = False) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    text = f"{number:+,.0f}" if signed else f"{number:,.0f}"
    return f"{text} 元"


def _context_text(text: str) -> str:
    """One conversation message as Doubao context: markdown tables and code fences dropped, markdown
    marks stripped, cut to DIALOG_CONTEXT_CHARS."""
    kept: list[str] = []
    fenced = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            fenced = not fenced
            continue
        if fenced or stripped.startswith("|") or not stripped:
            continue
        kept.append(re.sub(r"[*_`#>]+", "", stripped))
    return " ".join(kept)[:DIALOG_CONTEXT_CHARS]


def _price_text(price: Any) -> str:
    try:
        value = float(price)
    except (TypeError, ValueError):
        return str(price)
    return str(int(value)) if value == int(value) else f"{value:g}"


def _quote_age_seconds(market: dict[str, Any], *, now: datetime | None = None) -> float | None:
    """Seconds since the snapshot's own quoteDatetime (the exchange's idea of the quote's age, the same
    clock order_boundaries.quote_is_fresh uses), None when it cannot be read. A quiet night-session
    contract has an old quoteDatetime while the data feed itself is fine."""
    quoted = str(market.get("quoteDatetime") or "").strip()
    if not quoted:
        return None
    try:
        quoted_at = datetime.fromisoformat(quoted.replace("Z", "+00:00"))
    except ValueError:
        return None
    if quoted_at.tzinfo is None:
        quoted_at = quoted_at.replace(tzinfo=timezone(timedelta(hours=8)))
    age = ((now or datetime.now(UTC)) - quoted_at.astimezone(UTC)).total_seconds()
    return age if math.isfinite(age) else None


class SentenceStreamer:
    """Turns the cumulative text of a streaming answer into spoken sentences: the first paragraph, one
    sentence at a time (。！？!?; or a line break), markdown stripped. `push(text)` returns the new
    complete sentences; `finish(text)` also flushes the unterminated tail. `truncated` is set once the
    answer went past its first paragraph (a table, a blank line, a code block); `skipped_line()` is the
    one closing remark, and only when something worth mentioning was left out — a table, or at least
    SKIPPED_MIN_CHARS of prose. On the user's 09-18 call "细节在对话里" closed almost every answer because
    a blank line or a folded progress block counted as details."""
    SENTENCE_END = re.compile(r"[。！？!?；;]+|\n")
    # A first sentence the model keeps extending is cut at a comma once it is long enough to say: the
    # caller hears something within seconds instead of waiting for the full stop.
    LONG_CLAUSE = 36
    SKIPPED_MIN_CHARS = 80
    TABLE_LINE = re.compile(r"^\s*\|", re.M)

    def __init__(self) -> None:
        self.spoken = 0  # characters of the paragraph already handed out
        self.paragraph_end: int | None = None
        self.truncated = False
        self.sentences: list[str] = []
        self.rest = ""  # what follows the first paragraph, kept for skipped_line()

    def _paragraph(self, text: str) -> str:
        if self.paragraph_end is not None:
            return text[:self.paragraph_end]
        for match in re.finditer(r"\n\s*\n|\n\s*\||\n\s*```|^\s*```|^\s*\|", text):
            self.paragraph_end = match.start()
            self.truncated = True
            return text[:self.paragraph_end]
        return text

    def push(self, text: str, *, final: bool = False) -> list[str]:
        paragraph = self._paragraph(text)
        out: list[str] = []
        while True:
            rest = paragraph[self.spoken:]
            match = self.SENTENCE_END.search(rest)
            if match is None:
                if final and rest.strip():
                    out.append(rest); self.spoken = len(paragraph)
                    break
                comma = rest.rfind("，", 12, self.LONG_CLAUSE + 24) if len(rest) >= self.LONG_CLAUSE else -1
                if comma < 0:
                    break
                out.append(rest[:comma + 1]); self.spoken += comma + 1
                continue
            if match.end() > self.LONG_CLAUSE + 24:
                comma = rest.rfind("，", 12, self.LONG_CLAUSE + 24)
                if comma >= 0:
                    out.append(rest[:comma + 1]); self.spoken += comma + 1
                    continue
            out.append(rest[:match.end()]); self.spoken += match.end()
        cleaned = [sentence for sentence in (_speakable(chunk) for chunk in out) if sentence]
        self.sentences.extend(cleaned)
        return cleaned

    def finish(self, text: str) -> list[str]:
        out = self.push(text, final=True)
        if self.paragraph_end is not None:
            self.rest = text[self.paragraph_end:]
        return out

    def skipped_line(self) -> str | None:
        """"表格我放对话里了" when a table was skipped, "后面的我放对话里了" when ≥80 chars of prose were;
        nothing for a short answer or a tail of blank lines / a folded block."""
        if not self.truncated:
            return None
        if self.TABLE_LINE.search(self.rest):
            return "表格我放对话里了。"
        prose = _speakable(re.sub(r"```.*?```", "", self.rest, flags=re.S))
        return "后面的我放对话里了。" if len(prose) >= self.SKIPPED_MIN_CHARS else None


INTERIM_AFTER_SECONDS = 3.0
# How long the read-out waits on Doubao's own turn before it speaks anyway. Bounds only — the
# events normally arrive first; the tests patch every one of these to ~0 so a scripted call
# never waits a bound out.
FILLER_QUIET_SECONDS = 3.0  # "Doubao said nothing for this turn" after this long
FILLER_WAIT_SECONDS = 8.0  # the filler's audio/response done (run_routed waits the same for the reply)
BRIDGE_AUDIO_WAIT_SECONDS = 8.0  # the bridge line's own audio
SENTENCE_AUDIO_WAIT_SECONDS = 12.0  # one streamed sentence's audio
ROUTED_RESPONSE_WAIT_SECONDS = 2.0  # run_fast/run_review: Doubao's (cancelled) reply ends
# 下单类直接出卡: who reads the card. "direct" (default): the Runtime reads it the moment it is made and Doubao's
# own reply to that final is cancelled like a late reply. Measured 10-13 on the live Doubao (local call, same
# clips): direct 2.43 / 2.00 / 2.25 s from the end of speech to the first audio, against 3.19 / 2.75 / 2.95 s
# when Doubao's `action` tool turn reads it in turn (the call comes ~0.75 s after the final, its replacement
# TTS ~0.9 s later); with the commit sent at the final Doubao did not answer at all. "turn" keeps the in-turn
# path (measurement switch): Doubao gets ACTION_MODEL_WAIT_SECONDS to call `action` before the Runtime reads.
ACTION_MODEL_WAIT_SECONDS = 2.5
ACTION_SPEAK_DIRECT = os.getenv("AI_TRADER_VOICE_ACTION_SPEAK", "direct") != "turn"
# How long the read waits for Doubao's cancelled reply (canceled → done measured ~0.5 s) before speaking anyway.
ACTION_CANCEL_WAIT_SECONDS = 2.0
HANGUP_GOODBYE_WAIT_SECONDS = 1.0  # Doubao's own goodbye before the Runtime speaks one
HANGUP_AUDIO_WAIT_SECONDS = 5.0  # the goodbye's audio (the daily-limit line waits the same)
HANGUP_TAIL_SECONDS = 2.0  # the goodbye's playback after its audio arrived
SESSION_CLOSE_WAIT_SECONDS = 5.0  # the upstream session.close acknowledgement at call end
LATE_REPLY_CANCEL_SECONDS = 6.0  # a late final's reply is cancelled within this window
# A commit waits for Doubao to acknowledge the Runtime's own response.cancel (`canceled`, measured 0.03–0.05 s)
# so Doubao's reply is cancelled before the Runtime's line is committed — bounded: a cancel of a reply that had
# already ended may never be acknowledged.
SPEAK_CANCEL_WAIT_SECONDS = 1.0
# 上游看门狗 (10-15, 315c0ee1): a Doubao session can stop answering altogether — after a commit nothing came
# back for 55 s, not even the next sentence's transcription, while session.close was still acknowledged; a direct
# probe also met sessions that never sent session.created (5 % in two runs, in bursts, a ~1 min window where a
# fresh connection did not answer either). Normally the first event after a commit arrives within 1 s (425
# commits: p99 0.59 s, max 0.98 s) and session.created within 0.4 s. Past these bounds with no answer to the
# request (_answers_expectation), the Runtime opens a new session (with the conversation's context) and says the line again; after
# UPSTREAM_RECONNECT_ATTEMPTS failed sessions it tells the user and ends the call instead of going silent.
UPSTREAM_REPLY_WAIT_SECONDS = 4.0
SESSION_CREATED_WAIT_SECONDS = 5.0
UPSTREAM_RECONNECT_ATTEMPTS = 3
UPSTREAM_RECONNECT_BACKOFF_SECONDS = (0.0, 1.0, 3.0)
UPSTREAM_WATCH_TICK_SECONDS = 0.25
UPSTREAM_GIVE_UP_LINE = "语音通道没有响应，请稍后重拨。"
# An empty catalog is read again before routing at most this often (ensure_product_names).
CATALOG_RETRY_SECONDS = 30.0

# -- V1.4: the opening line, chosen by what was asked ---------------------------------------------
# Until 1.6.5 the bridge was "我看一下" for a chart word and "我查一下" for everything else, so an interface
# command was answered with "我查一下" (user, 09-19: "这明明只是个命令，打开东西就该说'好的，正在打开 XXX'").
# The line now comes from the request's own type. Pure, table-driven, every line ≤ OPENING_LIMIT 字
# (punctuation not counted).
OPENING_LIMIT = 8
_NOT_A_CHARACTER = re.compile(r"[\s，。,.!！?？、~～…·\"'“”‘’]")
# An interface sentence: the verb that is spoken back, then what it acts on. First match wins.
_UI_VERB_SAID: tuple[tuple[str, str], ...] = (
    (r"收起|收一下|收掉|隐藏|折叠|收", "收起"),
    (r"关闭|关掉|关上|关", "关"),
    (r"回到|返回|回", "回到"),
    (r"切到|切换到|切换|切|换成|换到|换个|换|调成|调到", "切到"),
    (r"打开|开一下|开|调出|弹出|展开|显示|进入|看看|看一下|看|调", "打开"),
)
_UI_OBJECT_SAID: tuple[tuple[str, str], ...] = (
    (r"图表|K线图|K线|大图|行情图|看图|图", "图表"),
    (r"交易台|主界面|主页|行情页", "交易台"),
    (r"复盘|回放", "复盘"),
    (r"设置", "设置"),
    (r"对话|聊天", "对话"),
    (r"策略", "策略"),
    (r"下单|阶梯|五档|盘口|深度", "下单台"),
    (r"持仓|仓位", "持仓"),
    (r"委托", "委托"),
    (r"底栏", "底栏"),
    (r"帮助|说明", "说明"),
    (r"深色|暗色|黑色", "深色"), (r"浅色|亮色|白色", "浅色"),
    (r"主题|配色|颜色|皮肤", "主题"),
    (r"字号|字体|字", "字号"),
    (r"日线|天线", "日线"), (r"(?:1|一)分|(?:3|三)分|(?:5|五)分|(?:15|十五)分", "分钟线"), (r"(?:1|一)小时", "小时线"),
    (r"周期", "周期"),
    (r"合约|品种", "合约"),
)
# A question about numbers is never an interface command, whatever verb it uses.
_ASKING = re.compile(r"多少|怎么样|怎样|如何|几手|有没有|有什么|有哪些|有啥|什么价|报价|涨了没|跌了没|吗|呢")
# "搞一手螺纹多的" names no verb of ours but is an order all the same: a lot size is enough ("手里" is not).
_ORDER_WORDS = re.compile(r"买|卖|开多|开空|做多|做空|下单|平仓(?!记录)|平掉|全平|清仓|撤单|撤掉|加仓|减仓|止损|止盈|挂单|[一二三四五两\d]+手(?!里)")
_PREFERENCE_WORDS = re.compile(r"语速|音色|默认手数|手数|播报|推送|通知|判停|说慢|说快|念快|念慢")
_CHART_WORDS = re.compile(r"KD|MACD|均线|走势|形态|指标|基差|标注|画线|图上|讲讲|讲一下", re.I)
_RESEARCH_WORDS = re.compile(r"新闻|消息|为什么|原因|影响|政策|库存|仓单|研报|传闻")
_QUERY_WORDS = re.compile(r"多少钱|价格|行情|怎么样|持仓|仓位|手里|账户|资金|权益|盈亏|亏|赚|保证金|委托|成交|平仓记录")


def _ui_opening(utterance: str) -> str | None:
    """"好，正在打开图表" for an interface request, None when the sentence is not one."""
    if _ASKING.search(utterance):
        return None
    said_verb = next((said for pattern, said in _UI_VERB_SAID if re.search(pattern, utterance)), None)
    if said_verb is None:
        return None
    said_object = next((said for pattern, said in _UI_OBJECT_SAID if re.search(pattern, utterance)), None)
    if said_object is None:
        # Interface work whose object has no short name of ours ("把那个面板打开"): say nothing wrong.
        return "好的，马上" if looks_like_ui(utterance) else None
    for line in (f"好，正在{said_verb}{said_object}", f"正在{said_verb}{said_object}"):
        if len(_NOT_A_CHARACTER.sub("", line)) <= OPENING_LIMIT:
            return line
    return "好的，马上"


def interim_line(utterance: str, route: str | None = None) -> str:
    """The one short line a Runtime-handled turn opens with, by request type: an interface command says
    what is being opened, an order says it is coming, a lookup says it is being read, research says it is
    being looked up, a preference says it is being changed. Pure; ≤ OPENING_LIMIT 字.

    `route` is the routed tool's name when the formal caller (`read_out`) passes it: a sentence still in
    semantic judgment (`delegate` / `general`) opens with the neutral waiting line only — what the words
    resemble (close-all, an order, a panel) is not yet decided, so the bridge line neither echoes an
    action nor promises a card. Without `route` the word-based lines stand for the dispatched kinds."""
    if route in ("delegate", "general"):
        return "我先看看"
    text = utterance or ""
    # 偏好在下单之前判：“默认手数改成两手”是设置，不是下单。
    if _PREFERENCE_WORDS.search(text):
        return "好，这就改"
    if _ORDER_WORDS.search(text):
        return "好，这就来"
    opening = _ui_opening(text)
    if opening is not None:
        return opening
    if _CHART_WORDS.search(text):
        return "我看一下图"
    if _RESEARCH_WORDS.search(text):
        return "我查一下"
    if _QUERY_WORDS.search(text):
        return "我看一下"
    return "好的"


# A contract code in parentheses ("螺纹钢（SHFE.rb2610）") adds nothing aloud and reads as letter soup:
# the whole parenthesis goes. Other parentheses keep their content ("（今仓可平 2 手）" is read).
CONTRACT_CODE = re.compile(r"[（(]\s*(?:[A-Z]{2,6}\.[A-Za-z]{1,3}\d{3,4}|KQD\.[A-Za-z]@[A-Za-z]+\.[A-Za-z0-9]+"
                           r"|[A-Z]{2,6}\.[A-Za-z0-9]+[FGHJKMNQUVXZ]\d{2})\s*[)）]")


def _speakable(text: str) -> str:
    """Markdown-ish agent output → something a voice can read: drop headings, bullets, links, code."""
    text = re.sub(r"```.*?```", "", text, flags=re.S)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)  # [label](url) → label
    text = re.sub(r"https?://\S+", "", text)
    text = foreign_codes_named(CONTRACT_CODE.sub("", text))  # 外盘代码念中文名（CME.GCEZ26 → COMEX黄金2612）
    text = re.sub(r"[*_`#>|()（）\[\]]+", "", text)
    text = re.sub(r"^\s*[-•]\s*", "", text, flags=re.M)
    text = re.sub(r"\s*\n+\s*", "。", text).strip("。 ")
    text = re.sub(r"。{2,}", "。", text)
    return text[:SPOKEN_LIMIT]


def first_sentence(text: str) -> str:
    """The fallback for a missing `spoken`: the content's first sentence, in a speakable form."""
    cleaned = _speakable(text)
    if not cleaned:
        return ""
    match = re.match(r"(.+?[。！？!?；;])", cleaned)
    sentence = match.group(1) if match else cleaned + "。"
    return sentence[:SPOKEN_LIMIT]


@dataclass(frozen=True)
class CallIdentity:
    """Who the call acts as: every tool goes through the same authenticated interfaces with these
    headers, so a cloud user's windows, chats, orders and research stay their own."""
    user_id: str
    headers: dict[str, str]


@dataclass(frozen=True)
class TurnBinding:
    """#523 R1c: where a front turn was accepted — the chat session, the trading account bound to it and the
    call's session generation. A result is published, spoken, written to history or handed to the provider only
    while the binding still holds: the session still bound to that same account. Only a real account change counts
    as a move (#932 follow-up): the session row's account revision is the switches' write-order token and also
    advances on a repeat of the same account, which must not retire an answer; a full A→B→A within one turn ends on
    the account the answer was made for and is not told apart from staying."""
    session_id: str | None
    account_id: str
    session_gen: int


# Set for the duration of one call; tool tasks inherit it, so the service's business calls
# never need the state threaded through.
_identity: contextvars.ContextVar[CallIdentity | None] = contextvars.ContextVar("voice_identity", default=None)
# A call opened through a replay namespace ("/api/replays/<id>/runtime/api/voice/realtime") is bound
# to that replay: business calls carry its replayId and direct state access goes to the child app.
_replay_binding: contextvars.ContextVar[str | None] = contextvars.ContextVar("voice_replay_binding", default=None)
# The upstream session generation a task was derived from: set in each session's reader, so the tool handlers
# and rounds it starts carry it. A handler of a session the watchdog has replaced never acts inside the new
# session's turn (#754 / #904 review): no replacement, no response.cancel, no tool result there.
_upstream_gen: contextvars.ContextVar[int | None] = contextvars.ContextVar("voice_upstream_gen", default=None)


@dataclass
class CallState:
    user_id: str = LOCAL_USER_ID
    # The account's preferences at call start (voice, speed, watchdog, confirm window, announcements).
    preferences: UserPreferences = field(default_factory=UserPreferences)
    session_id: str | None = None
    speaking: bool = False
    chat_session_id: str | None = None  # the chat session the delegate turns join (client "hello", else the user's recent one)
    usage: list[dict[str, Any]] = field(default_factory=list)
    last_announce: dict[str, float] = field(default_factory=dict)
    latency: list[dict[str, Any]] = field(default_factory=list)
    speech_end_at: float | None = None
    audio_first_at: float | None = None
    tool_log: list[dict[str, Any]] = field(default_factory=list)
    window_id: str | None = None  # the caller's own UI window (client "hello"); tools act on it, never on another device
    # The server streams a reply's audio far faster than real time: the client is still playing
    # long after output_audio.done. Estimated playback end = start + seconds delivered.
    play_started_at: float | None = None
    play_seconds: float = 0.0
    # Proposal cards a delegate turn left waiting for the spoken "确认", plus the deadline once the
    # read-back has played.
    pending: dict[str, Any] | None = None
    # 一句两单 (#950, #932 复核): the cards queued behind the waiting one ({item, line, number}, oldest first; their
    # items stay in `unseen_cards`), and the next one whose question has been said but not yet committed. The call
    # owns both — never a local copy across an await — so a hang-up or a sentence that moves on retires them
    # (`_abandon_pending`), and the next card becomes the waiting one only when its question is committed
    # (`arm_next_card`): until then a spoken 确认 cannot reach it.
    card_queue: list[dict[str, Any]] = field(default_factory=list)
    next_card: dict[str, Any] | None = None
    # Cards a cancelled retire had taken off the call but not yet rejected: the next abandon (the teardown's) does.
    stranded_cards: list[dict[str, Any]] = field(default_factory=list)
    # Bumped by every new user utterance (ASR final) and every barge-in cancel: a tool result that
    # belongs to an earlier serial is handed to the model silently, never injected as speech — the
    # injection would close the newer, still-pending response and its own result would be dropped.
    turn_serial: int = 0
    # The Runtime-routed action of the current utterance: its serial, tool name and running task, whether
    # Doubao also called the tool this turn (then the in-turn path speaks the result) and whether
    # Doubao's response for the turn has ended (then the Runtime speaks it out of turn).
    routed: dict[str, Any] | None = None
    # Whether Doubao has started answering the current utterance (audio, text or a tool call). A
    # `response.done` before that belongs to the previous out-of-turn speech, whose done event only
    # arrives with the next input, and must not count as this turn's end.
    turn_response_seen: bool = False
    # 续句 (09-21): same, but only what the user could actually hear — a muted Doubao filler or its
    # cancelled audio is not an answer, so the half-sentence may still merge with its continuation.
    turn_answer_seen: bool = False
    # Open transcript (deltas seen, no completed yet) and when the last delta arrived.
    asr_open: bool = False
    asr_last_delta_at: float = 0.0
    # The open segment's latest partial as Doubao sent it, and when the watchdog forced its endpoint
    # (0 = not forced, or new words came since): the stall final reads both.
    asr_partial: str = ""
    stall_commit_at: float = 0.0
    # 提前判停 (09-28): the open segment's text without punctuation, when it last changed, and the latest
    # delta that ended a sentence (Doubao's second pass puts the full stop in). `segment_final_text` is
    # what of the segment the Runtime already answered from a partial, as its transcript — separators
    # and all, so the answered words keep their boundaries; Doubao's own completed for it arrives
    # later and is dropped, and its reply to that late final is cancelled (`late_reply_until`).
    asr_bare: str = ""
    asr_changed_at: float = 0.0
    asr_sentence: str | None = None
    segment_final_text: str = ""
    segment_final_at: float = 0.0
    late_reply_until: float = 0.0
    # The current turn was routed from a partial: Doubao has not answered it yet, so the Runtime does not
    # wait for Doubao's filler or response before speaking.
    turn_early: bool = False
    # The response id of Doubao's late reply being cancelled: only its audio is dropped, not the Runtime's
    # read-out that may stream at the same moment (None = unknown, drop everything as before).
    late_muted_response: str | None = None
    # Response ids of the Runtime's own read-outs (tts_type chat_tts_text): never dropped as a muted reply.
    runtime_responses: set[str] = field(default_factory=set)
    # Read-outs the user cancelled (#523: "停"/打断/客户端 cancel): the frames the synthesiser still
    # delivers afterwards are dropped instead of playing out — cancelling a committed TTS stops it.
    cancelled_readouts: set[str] = field(default_factory=set)
    early_checked_bare: str = ""
    # Milestone timeline for the "said something, nothing happened" diagnosis: ASR start/final,
    # response audio start/first delta/done, tool done — flat, in order, with seconds since the call began.
    timeline: list[dict[str, Any]] = field(default_factory=list)
    # Set on every response.output_audio.done: the sentence streamer commits the next sentence only after
    # the previous one's audio has been delivered (the client keeps playing from its queue meanwhile).
    audio_done: asyncio.Event = field(default_factory=asyncio.Event)
    # The delegate turn whose answer is being read out right now (serial); a new utterance during it
    # is a follow-up ("A 未完先说 B").
    reading_serial: int | None = None
    # A spoken "关闭语音" is being honoured: the model's own reaction to it (a tool call, a reply) is dropped.
    hanging_up: bool = False
    # V1.3 quick query turn: {serial, text, called, fallback, spoken}. Doubao must call a quick tool before
    # it says anything; words first = fallback to delegate (the turn is then muted like any delegate turn).
    quick: dict[str, Any] | None = None
    # 前台直接查询 (AI_TRADER_VOICE_FRONT_QUERY): whether this call runs it (decided at call start), the
    # current front turn {serial, text, kind, what, symbol_query, called, fallback, spoken, t0, prefetch}
    # and the last query kind ("那沪金呢" after a positions question asks for positions).
    front_on: bool = False
    front: dict[str, Any] | None = None
    last_what: str | None = None
    # 下单类直接出卡 (AI_TRADER_VOICE_FRONT_ACTION): whether this call runs it (decided at call start, only with
    # front_on) and the current order turn {serial, text, action, called, fallback, t0, prefetch, read}.
    action_on: bool = False
    action: dict[str, Any] | None = None
    # #523 R1c: the chat session's generation in this call (bumped whenever the call moves to another
    # conversation — a hello, the window's shown session), whether the call has ended, and the cards
    # an action made that the user has not heard yet (never confirmable by voice until read out).
    session_gen: int = 0
    closed: bool = False
    # The response id of the output that started last (audio frames carry none of their own).
    current_output_id: str | None = None
    unseen_cards: set[str] = field(default_factory=set)
    # The contract the user last named in this call (any turn): "刚才那个多少钱" resolves to it.
    last_symbol_query: str | None = None
    # Single-filler policy (09-18): for a turn the Runtime answers itself (delegate / intent), Doubao's own
    # reply is cancelled the moment it starts — the user hears one voice, not "好" + "我查一下" + the answer.
    # `committed_serial` marks that the Runtime already committed speech for the turn (never cancel that);
    # `mute_cancel_pending` tells the canceled event apart from a barge-in (which starts a new turn).
    mute_serial: int | None = None
    committed_serial: int | None = None
    mute_cancel_pending: bool = False
    # When the Runtime's last response.cancel went out, until Doubao acknowledges it (canceled / done); 0 = none open.
    # `cancel_done_owed`: the canceled came, its response.done is still to follow — both only acknowledge the cancel.
    cancel_sent_at: float = 0.0
    cancel_done_owed: bool = False
    # 上游看门狗: the moment by which some upstream event must have arrived (0 = nothing awaited), what was awaited
    # ("session" / "commit", or why the session was lost: "closed" / "ended" / "send").
    upstream_expect_by: float = 0.0
    upstream_expect: str = ""
    # The Runtime's lines committed and not started yet, oldest first, one record each ({text, at, retried}: when it
    # was committed and whether it already had its one retry on a live session) — Doubao reads commits in order, so
    # an audio start answers the oldest; a second line never overwrites the first (#904 review).
    unheard: list[dict[str, Any]] = field(default_factory=list)
    # When the session last sent anything besides a wordless transcription.started or the acknowledgement of
    # the Runtime's own cancel (it is alive, not stalled).
    upstream_seen_at: float = 0.0
    reconnecting: bool = False
    gave_up: bool = False
    # Replacement sessions opened over the whole call: one budget of UPSTREAM_RECONNECT_ATTEMPTS, not per recovery.
    reconnects: int = 0
    # The call's own tasks (rounds, tool handlers, readers, watchdogs): cancelled when the call ends. A business
    # call they wait on is shielded and finishes on its own (cancelling the waiter is not cancelling the order).
    tasks: set[asyncio.Task] = field(default_factory=set)
    # 续句 (09-21): the run_routed / run_fast coroutine task of the current utterance (cancelled when the
    # sentence continues), and the last ASR final — its text, route and time — the merge is decided from.
    routed_task: asyncio.Task | None = None
    last_final: dict[str, Any] | None = None
    # 引导复盘: the running review ({picks, index, followed}), the one-shot "要复盘一下吗？" offer waiting for
    # its answer, and the client's "hello" asking to start with the review (复盘页的“语音复盘”).
    review: dict[str, Any] | None = None
    review_offer: bool = False
    review_intent: bool = False
    # 09-21 打断: the agent run behind the in-flight delegate round (task.started's taskId), so "停" can
    # cancel it server-side (POST /api/runs/{id}/cancel) instead of only dropping the read-out.
    inflight_run_id: str | None = None
    # 保本提醒 (2026-10-06): the question this call last read out ({id, at}: its notice id and when the
    # read-out was handed to the synthesiser); 移 / 不用 answer it inside the confirm window only.
    breakeven_heard: dict[str, Any] | None = None
    # The replay this call is bound to (a socket opened through "/api/replays/<id>/runtime"): quotes,
    # positions, the account, cards and window commands all act on that replay's child runtime, never
    # on the live simulation account.
    replay_id: str | None = None

    def still_playing(self) -> bool:
        return self.play_started_at is not None and time.monotonic() < self.play_started_at + self.play_seconds + 0.3


def _awaits_confirm(payload: dict[str, Any]) -> bool:
    """A card the user can confirm (a risk-blocked card has no approval and is only read out)."""
    return bool(payload.get("approvalId")) and payload.get("status") == "approval_required"


class VoiceRealtimeService:
    def __init__(self, app: FastAPI, *, key_file: Path | None = None):
        self.app = app
        self.key_file = key_file or Path(os.getenv("AI_TRADER_VOICE_REALTIME_KEY_FILE") or DEFAULT_KEY_FILE)
        self.calls: dict[str, CallState] = {}
        self._pending_announcements: dict[str, list[dict[str, str]]] = {}
        self.product_names: list[str] = []  # catalog product names, longest first (asr_context fills it)
        self._names_read_at: float | None = None  # the last catalog read for them (ensure_product_names)
        self._review_offered: dict[str, str] = {}  # user → the date the review was last offered on

    # -- 引导复盘 (2026-09-29) ---------------------------------------------

    def review_route(self, state: CallState, text: str, route: str) -> str:
        """Where a final goes while a review runs or is offered: a card's 确认/取消, hanging up, the
        read-out stop, the 策略编辑卡's 补一句 or a tour, the order watch and replay keep their routes; inside a
        review 跳过 / 不聊了 steer it, a quote, an interface command or an order keeps its own path and
        everything else answers the current question. "复盘一下 / 复盘这周" starts one."""
        if (route in {"hangup", "stop", "confirm", "cancel", "chase", "watch_cancel", "replay",
                      "breakeven_move", "breakeven_decline"}
                or route.startswith(("editor_", "tour_")) or state.replay_id):
            return route
        stripped = strip_punctuation(text)
        if state.review is not None:
            control = review_control(stripped)
            if control is not None:
                return f"review_{control}"
            if (route == "quick" or match_intent(stripped) is not None or commands_order(stripped, self.product_names)
                    or text.strip().endswith(("？", "?")) or REVIEW_QUESTION.search(stripped)):
                return route
            return "review_answer"
        if state.review_offer:
            answer = review_offer_answer(stripped)
            if answer is not None:
                return "review_start" if answer == "accept" else "review_decline"
        if route in {"delegate", "chat"} and review_start_scope(stripped) is not None:
            return "review_start"
        return route

    async def _review_picks(self, scope: str) -> dict[str, Any] | None:
        result = await self._call("GET /api/account/review-picks", query={"scope": scope})
        data = result.get("data") if result.get("ok") else None
        return data if isinstance(data, dict) else None

    async def _daily_summary(self, state: CallState) -> str:
        """The latest 今日复盘's summary when it is from the last three days ("" otherwise), and the caller's
        window opened on it (best effort: a window that refuses the command never stops the review)."""
        result = await self._call("GET /api/account/daily-review")
        review = ((result.get("data") or {}).get("review") if result.get("ok") else None) or {}
        day = str(review.get("tradingDay") or "")
        text = str((review.get("summary") or {}).get("text") or "").strip()
        today = datetime.now(UTC).astimezone(CHINA).date()
        try:
            recent = day and (today - datetime.strptime(day, "%Y-%m-%d").date()).days <= 3
        except ValueError:
            recent = False
        if not recent or not text:
            return ""
        try:
            window = await self._window(state)
            if window is not None:
                await send_ui_command(self._call, window, {"reviewDay": day}, wait_ms=0)
        except Exception as error:  # noqa: BLE001
            log.debug("voice review page not opened: %s", error)
        return f"先说{'今天' if day == today.isoformat() else day[5:]}的复盘：{text.rstrip('。')}。"

    async def review_step(self, state: CallState, action: str, text: str) -> str:
        """One step of the voice review, and the line the Runtime speaks for it."""
        if action == "review_decline":
            return "好，想复盘的时候说一声“复盘一下”就行。"
        if action == "review_start":
            scope = review_start_scope(text) or "day"
            # 今日复盘 (2026-10-06): a day review opens with the review's summary (and the review page),
            # then asks about the picks as before.
            summary = await self._daily_summary(state) if scope == "day" else ""
            data = await self._review_picks(scope)
            when = "这周" if scope == "week" else "今天"
            if data is None:
                return summary or "复盘的数据暂时读不到，稍后再试。"
            picks = data.get("picks") or []
            if not picks:
                if summary:
                    return f"{summary}没有特别需要再聊的交易。"
                if not data.get("total"):
                    return f"{when}还没有平仓的交易，没什么可复盘的。"
                if not data.get("unnoted"):
                    return f"{when}的交易都补过想法了，没有要再聊的。"
                return f"{when}的交易没有特别需要聊的。想聊哪一笔，可以在复盘页选中它补一句。"
            state.review = {"picks": picks, "index": 0, "followed": None}
            count = "一笔" if len(picks) == 1 else f" {len(picks)} 笔"
            return f"{summary}好，{when}有{count}值得聊聊，一笔一笔来。{picks[0]['question']}"
        review = state.review
        if review is None:
            return ""
        if action == "review_stop":
            state.review = None
            return "好，先聊到这儿。记下的在复盘页每笔交易的“你的补充”里。"
        lead = "好，跳过。"
        if action == "review_answer" and len(re.sub(r"[\s，。,.!！?？、]", "", text)) <= 2:
            # "嗯" / "好" is not what the user thought: say how to answer once; the second time it skips.
            if not review.get("nudged"):
                review["nudged"] = True
                return "可以说说当时怎么想的；不想聊这笔就说“跳过”。"
            action = "review_skip"
        if action == "review_answer":
            pick = review["picks"][review["index"]]
            answer = text.strip().rstrip("。")
            full = f"{review['followed']}；{answer}" if review["followed"] else answer
            saved = await self._call("PUT /api/account/trade-notes/{round_id}", path={"round_id": pick["id"]},
                                     body={"text": full[:500], "via": "voice"})
            if not saved.get("ok"):
                lead = "这句没记上，稍后可以在复盘页补。"
            elif review["followed"] is None and len(re.sub(r"[\s，。,.!！?？、]", "", answer)) <= 6:
                review["followed"] = full
                return "能多说一句吗？比如当时看到了什么，或者现在回头看怎么想？"
            else:
                lead = "记下了。"
        review["index"] += 1
        review["followed"] = None
        review["nudged"] = False
        if review["index"] < len(review["picks"]):
            return f"{lead}下一笔，{review['picks'][review['index']]['question']}"
        state.review = None
        return f"{lead}就这些了，补充都记在复盘页对应的交易里。"

    async def review_offer_line(self, state: CallState, now: datetime | None = None) -> str | None:
        """After the day session (15:00–20:55), the first call of the day asks once whether to review the
        day's notable trades — only when there are some."""
        local = (now or datetime.now(UTC)).astimezone(CHINA)
        (start_h, start_m), (end_h, end_m) = REVIEW_OFFER_WINDOW
        minutes = local.hour * 60 + local.minute
        if not start_h * 60 + start_m <= minutes < end_h * 60 + end_m:
            return None
        today = local.date().isoformat()
        if self._review_offered.get(state.user_id) == today:
            return None
        data = await self._review_picks("day")
        picks = (data or {}).get("picks") or []
        if not picks:
            return None
        self._review_offered[state.user_id] = today
        state.review_offer = True
        count = "一笔" if len(picks) == 1 else f" {len(picks)} 笔"
        return f"今天有{count}交易值得聊聊，要复盘一下吗？"

    # -- availability ------------------------------------------------------

    @property
    def local_mode(self) -> bool:
        return isinstance(self.app.state.auth, LocalAuth)

    def routine_named(self, user_id: str, text: str) -> bool:
        """Whether the sentence runs one of the caller's routines (我的流程: its exact name or "跑一下 X")."""
        from .voice_intents import routine_request
        try:
            names = self.app.state.store.routine_names(user_id)
        except Exception:
            return False
        return bool(names) and routine_request(text, names) is not None

    def user_calls(self, user_id: str) -> int:
        return sum(1 for call in self.calls.values() if call.user_id == user_id)

    def status(self, user_id: str | None = None) -> dict[str, Any]:
        """`enabled` says whether this Runtime offers voice (a key exists). The per-user call limit is
        reported through `reason`/`activeCalls` but never flips `enabled`: the client polls this during
        its own call (1.5.1–1.5.6 read their own call as "unavailable" and hung up). The limit is
        enforced at socket accept (4429); the tier's daily minute cap at accept (4430) and mid-call."""
        local = self.local_mode
        source = self._key_source()
        limited = not local and user_id is not None and self.user_calls(user_id) >= CLOUD_CALLS_PER_USER
        daily_exhausted = not local and user_id is not None and self.daily_voice_exhausted(user_id)
        # The voice-call skill (tier-withheld or globally disabled) keeps the
        # legacy reason string: 1.6.x clients map it to their unavailable text.
        voice_skill = skill_by_id("voice-call")
        feature_off = not local and user_id is not None and voice_skill is not None and not skill_allowed(
            self.app.state.store, user_id, voice_skill)
        return {"enabled": source is not None, "localMode": local, "keyConfigured": source is not None,
                "keySource": source, "model": MODEL, "voice": self.voice_for(user_id), "activeCalls": len(self.calls),
                # 按住说话的回答由页面自动读出（/api/voice/speak），不依赖通话（10-05）。
                "pushToTalkReadOut": "按住说话的回答会自动读出来，不需要接通通话",
                "reason": "voice_key_missing" if source is None else ("cloud_call_limit" if limited else ("voice_daily_limit" if daily_exhausted else ("feature_not_in_plan" if feature_off else None)))}

    def daily_voice_exhausted(self, user_id: str) -> bool:
        """M2: the tier's per-day voice minutes are spent (recorded + in-call
        seconds of already-closed calls today)."""
        limit = plan_for_user(self.app.state.store, user_id).voice_seconds_per_day
        if limit is None:
            return False
        used = self.app.state.store.voice_seconds(user_id, datetime.now(UTC).date().isoformat())
        return used >= limit

    def voice_ids(self) -> set[str]:
        return {item["id"] for item in VOICES}

    def voice_for(self, user_id: str | None) -> str:
        chosen = self.app.state.store.user_preferences(user_id).voice.voice if user_id else None
        return chosen if chosen in self.voice_ids() else VOICE

    def voices(self, user_id: str) -> dict[str, Any]:
        return {"current": self.voice_for(user_id), "voices": VOICES}

    def _key_source(self) -> str | None:
        if os.getenv(KEY_ENV, "").strip():
            return "environment"
        return "file" if self._read_key() is not None else None

    def _read_key(self) -> str | None:
        """Environment first (the cloud Runtime's env file), then the key file; the value is only ever
        handed to the upstream connection."""
        value = os.getenv(KEY_ENV, "").strip()
        if value:
            return value
        try:
            value = self.key_file.read_text().strip()
        except OSError:
            return None
        return value or None

    # -- business calls (same authenticated interfaces as the UI / CLI) ----

    def _identity(self) -> CallIdentity:
        identity = _identity.get()
        if identity is not None:
            return identity
        if not self.local_mode:
            raise HTTPException(401, "voice_identity_required")
        return CallIdentity(LOCAL_USER_ID, {"authorization": "Bearer " + self.app.state.auth.token})

    def _bound_app(self, state: CallState | None = None):
        """The app this call's tools act on: the replay child when the socket is bound to a replay
        (fail closed — a closed replay raises, it never falls back to the live runtime), else the
        main runtime."""
        replay_id = (state.replay_id if state is not None else None) or _replay_binding.get()
        if not replay_id:
            return self.app
        router = getattr(self.app.state, "replay_router", None)
        if router is None:
            raise HTTPException(404, "replay_not_found")
        return router.owned(str(replay_id), self._identity().user_id).child

    async def _call(self, operation: str, *, path=None, query=None, body=None, replay: bool | None = None, pending_card_reply: bool = False) -> dict[str, Any]:
        bridge = self.app.state.app_capabilities
        identity = self._identity()
        # A replay-bound call acts on the replay's child runtime (AppCall.replayId retargets the
        # bridge); only operations on /api/replays itself — the control route — must stay on the
        # main app, which owns the replay router.
        replay_id = _replay_binding.get() if replay is None or replay else None
        result = await bridge.call(AppCall(operation=operation, path=path or {}, query=query or {}, body=body,
                                           **({"replayId": replay_id} if replay_id else {})),
                                   user=identity.user_id, headers=identity.headers,
                                   **({"pending_card_reply": True} if pending_card_reply else {}))
        return result if isinstance(result, dict) else {"data": result}

    async def _http(self, method: str, path: str, body: dict[str, Any] | None = None, *,
                    account_id: str = "") -> tuple[int, dict[str, Any]]:
        """The proposal confirm route is UI-only (not in the capability catalog): the spoken "确认" is the
        user's own click, so it goes over the same authenticated HTTP surface the desktop uses. The
        spoken "算了" takes the same surface to the reject route — the one the catalog also lists for
        the typed turn's `reject_proposal` (#756), so both paths share one Owner."""
        bridge = self.app.state.app_capabilities
        headers = {**self._identity().headers, "origin": bridge.origin}
        replay_id = _replay_binding.get()
        if replay_id:
            # The card a replay call confirms lives in the replay's namespace; the live account
            # header must not ride along into it.
            path = f"/api/replays/{replay_id}/runtime{path}"
            account_id = ""
        if account_id:
            headers["x-ai-trader-account"] = account_id
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://ai-trader.local",
                                     trust_env=False, headers=headers, timeout=30.0) as client:
            response = await client.request(method, path, json=body)
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        return response.status_code, payload if isinstance(payload, dict) else {"data": payload}

    async def _window(self, state: CallState | None = None) -> dict[str, Any] | None:
        """The window the voice call belongs to. A signed-in user may have several devices online;
        without the client's window id the first online window could be another Mac (09-17: a
        test call opened the settings on the user's machine)."""
        wanted = state.window_id if state is not None else None
        if not wanted:
            # No "hello" (a pre-1.5.7 client or a bare harness): no window at all, never a guess.
            return None
        result = await self._call("GET /api/app/ui/windows", query={"deviceId": "*"})
        windows = [w for w in (result.get("data") or {}).get("windows", []) if w.get("online")]
        return next((w for w in windows if w.get("windowId") == wanted), None)

    def _own_session(self, state: CallState, session_id: str | None) -> str | None:
        """The client's hello names a chat session by id alone; only this user's own live one counts.
        A foreign user's id, or one deleted/expired since the client saw it, is ignored — never
        written into, and the call falls back to the user's own recent session (#523 R1a)."""
        if not session_id:
            return None
        try:
            record = self._bound_app(state).state.broker.get_session(session_id, state.user_id)
        except HTTPException:
            return None
        return session_id if record is not None else None

    def _chat_session(self, state: CallState) -> str | None:
        """The session the delegate turns join: the one the client named in "hello", else the user's
        most recent chat session (what the app restores on launch) — the same conversation either way."""
        if state.chat_session_id:
            if self._own_session(state, state.chat_session_id):
                return state.chat_session_id
            # Deleted, expired or another user's since the client named it: nothing is written to it.
            state.chat_session_id = None
        store = self._bound_app(state).state.store
        recent = store.recent_chat_session(user_id=state.user_id, mode="local" if self.local_mode else "cloud")
        if recent is not None:
            state.chat_session_id = str(recent["sessionId"])
        return state.chat_session_id

    def _chat_account_id(self, state: CallState) -> str:
        """The trading account the call's chat session is bound to: the quick 权益/持仓 questions read
        the account of the conversation the call joins (the window switched it), not the user's default
        one (#523 R1b). "" keeps the default; a replay call reads its own namespace via _http."""
        if state.replay_id or _replay_binding.get():
            return ""
        session_id = self._chat_session(state)
        if not session_id:
            return ""
        stream = self.app.state.broker.get_session(session_id, state.user_id)
        return str(stream.trading_account_id or "") if stream is not None else ""

    def _foreign_ledger(self, account_id: str) -> str | None:
        """本站模拟两本账（2026-10-21）：人民币本站模拟账户背后的美元子账户（外盘），没有时 None。语音问权益、持仓、
        委托与撤单都把这一本一起读，和网页、交易 AI 同一口径。"""
        if not account_id.startswith("paper:"):
            return None
        foreign_of = getattr(getattr(self.app.state, "accounts", None), "foreign_account_of", None)
        return foreign_of(account_id) if foreign_of is not None else None

    # -- tools ---------------------------------------------------------------

    async def run_tool(self, name: str, arguments: dict[str, Any], state: CallState, *, progress=None,
                       on_sentence=None, follow_up: bool = False) -> dict[str, Any]:
        # The registry gate for the routed actions: a routed utterance whose
        # skill is unavailable (globally disabled or tier-withheld) gets the
        # one unified spoken line instead of a half-run turn.
        routed_skill = ROUTED_ACTION_SKILLS.get(name)
        if routed_skill is not None:
            skill = skill_by_id(routed_skill)
            reason = skill_block_reason(self.app.state.store, state.user_id, skill) if skill else "disabled"
            if reason is not None:
                assert skill is not None
                return {"ok": False, "refused": True, "say": unavailable_line(skill, reason, parent_block_reason(self.app.state.store, state.user_id, skill))}
        try:
            if name == "delegate":
                return await self._delegate(str(arguments.get("utterance", "")).strip(), state, progress=progress,
                                            on_sentence=on_sentence, follow_up=follow_up)
            if name == "confirm":
                confirmed = await self._confirm(state)
                # This card is done either way: the next one's question is said (it becomes the waiting card only
                # once that line is committed, `arm_next_card`).
                return self._with_next_card(state, confirmed, nothing=confirmed.get("error") == "nothing_pending")
            if name == "cancel":
                # The same source as 确认 (#717): while the router hears a card (state.pending or the
                # server's), a spoken 取消 rejects that card instead of denying anything is waiting.
                self._resolve_pending(state)
                every = bool(arguments.get("all"))  # “都不要 / 全部取消”: the cards queued behind it go too
                if every:
                    # …and every card the conversation still holds open, whichever the call asked (#952 Codex).
                    current = state.pending or {"proposals": [], "summary": "", "created_at": time.monotonic(),
                                                "deadline": None}
                    known = {str(item.get("proposalId")) for item in current.get("proposals") or []}
                    extra = [item for item in self._server_cards(state) if str(item["proposalId"]) not in known]
                    if extra:
                        state.pending = {**current, "proposals": [*(current.get("proposals") or []), *extra]}
                # The queued cards stay with the call while this one is rejected (a hang-up meanwhile retires them).
                dropped = await self._abandon_pending(state, keep_queue=not every)
                several = "" if dropped or arguments.get("retracted") else self._several_waiting_line(state)
                say = ("好，都放弃了。" if every and dropped else "好，已放弃。" if dropped
                       else "好，这单不下了。" if arguments.get("retracted")
                       else several or "现在没有等确认的操作。")
                return self._with_next_card(state, {"ok": True, "dropped": dropped, "say": say},
                                            nothing=not dropped and not arguments.get("retracted") and not several)
            if name == "chase":
                return await self._chase_order(state)
            if name == "watch_cancel":
                return await self._cancel_watched_order(state)
            if name in {"breakeven_move", "breakeven_decline"}:
                return await self._answer_breakeven(state, name.removeprefix("breakeven_"))
            if name in {"tour_next", "tour_prev", "tour_stop"}:
                return await self._tour(name.removeprefix("tour_"), state)
            if name == "replay":
                return await self._replay_control(str(arguments.get("utterance", "")).strip(), state)
            if name == "editor_ask":
                return await self._editor_ask(str(arguments.get("utterance") or ""), state)
            if name in QUICK_TOOLS:
                return await self._quick_tool(name, arguments, state)
            if name == "query":
                return await self.front_query(arguments, state)
            if name == "action":
                # Doubao's own action call on a turn the Runtime did not read as a complete order: never
                # run from its arguments (measured 10-13: 空 came back long, 820 沪金 as 3082 螺纹).
                return {"ok": False, "note": "这句不能直接出卡，不要调 action，照常回答"}
            if name == "read_screen":
                window = await self._window(state)
                if window is None:
                    return {"ok": False, "error": "no_window", "say": "软件窗口现在不在线。"}
                detail = await self._call("GET /api/app/ui/windows/{window_id}", path={"window_id": window["windowId"]})
                data = detail.get("data") or {}
                st = data.get("state") or {}
                obs = data.get("observation") or {}
                return {"ok": True, "symbol": st.get("symbol"), "view": st.get("view"), "panel": st.get("panel"), "aiFilter": st.get("aiFilter"),
                        "dialog": st.get("dialog"), "positionsTab": st.get("positionsTab"), "settingsOpen": st.get("settingsOpen"),
                        "activeDialog": obs.get("activeDialog"), "text": str(obs.get("text") or "")[:1200]}
            return {"ok": False, "refused": True, "say": "这个我不能做。"}
        except HTTPException as error:
            if str(error.detail) == "replay_not_found":
                return {"ok": False, "error": "replay_not_found", "say": "回放已经退出了。"}
            return {"ok": False, "error": str(error.detail), "say": "后台没有回应，请稍后再说一遍。"}
        except Exception as error:  # a tool failure is spoken, never fatal to the call
            log.warning("voice tool %s failed: %s", name, error)
            return {"ok": False, "error": type(error).__name__, "say": "后台出了点问题，请稍后再说一遍。"}

    # -- 09-23: a replay-bound call drives the replay's own control route -------------------------

    async def _replay_control(self, utterance: str, state: CallState) -> dict[str, Any]:
        """Playback words ("暂停", "继续播放", "加速到 5 倍") drive the SAME control route the replay window
        uses — no new engine actions; a relative "快一点" resolves the next step from the current speed.
        A time jump ("快进到下午") is refused honestly: the engine only plays, pauses, steps and shifts
        the four speeds."""
        if not state.replay_id:
            return {"ok": False, "error": "not_in_replay", "say": "现在不在回放里。"}
        parsed = match_replay_control(utterance)
        if parsed is None:
            return {"ok": False, "error": "replay_control_unsupported",
                    "say": "回放只能播放、暂停、单步和调每秒 1、2、5、10 步，一步一根 1 分钟线，不能按时间跳。"}
        action, speed = parsed
        if action in {"speed_up", "speed_down"}:
            status = (await self._call("GET /api/replays/{identifier}", path={"identifier": state.replay_id},
                                       replay=False)).get("data") or {}
            current = int(status.get("speed") or 1)
            steps = [s for s in _SPEED_ORDER if s > current] if action == "speed_up" else [s for s in reversed(_SPEED_ORDER) if s < current]
            if not steps:
                return {"ok": True, "say": "已经是最%s速度了。" % ("高" if action == "speed_up" else "慢")}
            action, speed = "speed", steps[0]
        body: dict[str, Any] = {"action": action}
        if speed is not None:
            body["speed"] = speed
        try:
            result = await self._call("POST /api/replays/{identifier}/control", path={"identifier": state.replay_id},
                                      body=body, replay=False)
        except HTTPException as error:
            lines = {"feature_not_in_plan": "当前套餐不含回放功能。",
                     "skill_disabled": "「回放控制」已停用。",
                     "replay_control_not_ready": "回放还没就绪或已经结束，先等一下。",
                     "replay_not_found": "回放已经退出了。"}
            return {"ok": False, "error": str(error.detail),
                    "say": lines.get(str(error.detail), "回放控制没有成功，请再说一遍。")}
        data = (result.get("data") if isinstance(result, dict) else None) or {}
        say = {"pause": "回放已暂停。", "play": "回放继续播放。", "step": "走了一步。",
               "speed": f"速度调到每秒 {data.get('speed') or speed} 步。"}.get(action, "好了。")
        return {"ok": True, "say": say, "replay": data if isinstance(data, dict) else {}}

    # -- V1.3 quick queries: Doubao's own read-only tools, on the same authenticated routes -------------

    async def _quick_tool(self, name: str, arguments: dict[str, Any], state: CallState, *, front: bool = False) -> dict[str, Any]:
        """Compact Chinese fields (≤6 per kind, numbers formatted), never raw JSON. Stale or unavailable
        data returns only {"error": ...} so the model can do nothing but say so (fail closed).

        `front` (前台直接查询, AI_TRADER_VOICE_FRONT_QUERY): the same reads for the `query` tool — prices
        carry their unit and a spoken change ("跌 0.23%"), and a quote that is not current is still read
        with its reason (已收盘、几点开盘) in 说明 instead of the bare refusal."""
        # The account header does not ride through the capability bridge (capture_identity strips
        # it), so the 权益/持仓 reads go over the same authenticated HTTP surface the desktop uses,
        # carrying the chat session's own account (#523 R1b).
        chat_account = self._chat_account_id(state)
        code, account = await self._http("GET", "/api/account/summary", account_id=chat_account)
        if code == 404 and str(account.get("detail")) == "replay_not_found":
            # Raw HTTP reports the closed replay as a code; surface it as the run_tool catch does —
            # fail closed, never papered over with 账户数据过时/不可用.
            raise HTTPException(404, "replay_not_found")
        account = account if code == 200 else {}
        status, age = str(account.get("dataStatus") or "unavailable"), float(account.get("ageSeconds") or 0)
        query = str(arguments.get("query") or "").strip()
        if name == "get_quote":
            resolved = await self._resolve_contract(query, state)
            if "error" in resolved:
                return resolved
            symbol, label, match = resolved["symbol"], resolved["label"], resolved["item"]
            result = await self._call("GET /api/market/{symbol}", path={"symbol": symbol})
            market = result.get("data") or {}
            fresh = (result.get("ok") and market.get("dataStatus") in {"normal", "simulated"}
                     and float(market.get("ageSeconds") or 0) <= QUICK_MAX_AGE_SECONDS)
            if front:
                if not result.get("ok") or market.get("price") is None:
                    return {"error": "这个合约的行情暂时取不到，稍后再问"}
                closed = market.get("marketSessionStatus") == "closed"
                note = session_note(closed=closed, stale=not fresh, has_night=bool((match or {}).get("hasNight")),
                                    exchange=str((match or {}).get("exchangeId") or symbol.split(".")[0]))
                out = {"合约": label or market.get("name") or symbol,
                       "最新价": f"{_price_text(market.get('price'))} {price_unit(symbol)}",
                       "涨跌": change_words(market.get("changePercent"))}
                if note:
                    out["说明"] = note
                elif market.get("bidPrice1") is not None and market.get("askPrice1") is not None:
                    out["买一卖一"] = f"{_price_text(market['bidPrice1'])} / {_price_text(market['askPrice1'])}"
                return out
            if not fresh:
                return {"error": "行情数据过时/不可用"}
            change = market.get("changePercent")
            out = {"合约": label or market.get("name") or symbol, "最新价": _price_text(market.get("price")),
                   "涨跌": f"{float(change):+.2f}%" if isinstance(change, (int, float)) else "—"}
            if market.get("bidPrice1") is not None and market.get("askPrice1") is not None:
                out["买一卖一"] = f"{_price_text(market['bidPrice1'])} / {_price_text(market['askPrice1'])}"
            if market.get("marketSessionStatus") == "closed":
                out["状态"] = "已收盘"
            out["更新于"] = str(market.get("updatedAt") or "刚刚")
            return out
        if status not in {"normal", "simulated"} or age > QUICK_MAX_AGE_SECONDS:
            return {"error": "账户数据这会儿没有更新，稍后再问"} if front else {"error": "账户数据过时/不可用"}
        if name == "get_account":
            equity, today, floating = account.get("equity"), account.get("todayPnl"), account.get("floatProfit")
            summary = {"账户": str(account.get("accountName") or "模拟账户"), "权益": _money(equity), "今日盈亏": _money(today, signed=True),
                       "浮动盈亏": _money(floating, signed=True), "保证金占用": f"{float(account.get('marginRatio') or 0):.1f}%",
                       "持仓": f"{int(account.get('positionCount') or 0)} 个", "更新于": str(account.get("updatedAt") or "刚刚")}
            if front:
                summary.pop("更新于")  # ≤6 fields; a current account needs no time
            foreign = self._foreign_ledger(chat_account)
            if foreign:
                usd_code, usd = await self._http("GET", "/api/account/summary", account_id=foreign)
                if usd_code == 200 and str(usd.get("dataStatus") or "") in {"normal", "simulated"}:
                    summary["美元账（外盘）"] = f"权益 {_usd(usd.get('equity'))}，今日 {_usd(usd.get('todayPnl'), signed=True)}"
                else:
                    summary["美元账（外盘）"] = "暂时读不到"
                summary.pop("持仓", None)  # the two ledgers' positions are the 持仓 question's
            return summary
        code, positions_payload = await self._http("GET", "/api/positions", account_id=chat_account)
        if code == 404 and str(positions_payload.get("detail")) == "replay_not_found":
            raise HTTPException(404, "replay_not_found")
        positions = list((positions_payload or {}).get("items") or []) if code == 200 else []
        foreign = self._foreign_ledger(chat_account)
        foreign_unread = False
        if foreign:
            usd_code, usd_payload = await self._http("GET", "/api/positions", account_id=foreign)
            if usd_code == 200:
                positions += [{**item, "currency": "USD"} for item in (usd_payload or {}).get("items") or []
                              if isinstance(item, dict)]
            else:
                foreign_unread = True
        catalog_items = await self._catalog()
        names = {str(item.get("symbol", "")).lower(): str(item.get("instrumentName") or "") for item in catalog_items}
        if query:
            items = [item for item in catalog_items if query.lower() in " ".join(str(item.get(k, "")) for k in ("symbol", "instrumentId", "instrumentName", "productId")).lower()]
            match = resolve_symbol(query, items)
            wanted = {str(match.get("productId") or "").lower()} if match else {str(i.get("productId") or "").lower() for i in items}
            positions = [p for p in positions if str(p.get("symbol", "")).split(".")[-1].rstrip("0123456789").lower() in wanted]
            if not positions:
                return {"持仓": f"没有{query}的持仓"}
        if not positions:
            return {"持仓": "现在没有持仓" + ("（外盘美元账暂时读不到）" if foreign_unread else "")}
        lines = []
        for item in positions[:6]:
            direction = {"long": "多", "short": "空"}.get(str(item.get("direction")), str(item.get("direction") or ""))
            pnl = (pnl_words(item.get("unrealizedPnl")) if front
                   else f"浮动盈亏 {_money(item.get('unrealizedPnl'), signed=True)}")
            ledger = "（外盘，美元）" if item.get("currency") == "USD" else ""
            lines.append(f"{names.get(str(item.get('symbol', '')).lower()) or item.get('instrumentName') or item.get('symbol')}{ledger} {direction} {int(item.get('volume') or 0)} 手，{pnl}")
        out = {"持仓": "；".join(lines)}
        if len(positions) > 6:
            out["另有"] = f"{len(positions) - 6} 个"
        # 两本账不相加：人民币一个合计，美元另一个（金额是美元）。
        total = sum(float(item.get("unrealizedPnl") or 0) for item in positions if item.get("currency") != "USD")
        out["合计浮动盈亏"] = _money(total, signed=True)
        usd_rows = [item for item in positions if item.get("currency") == "USD"]
        if usd_rows:
            out["外盘浮动盈亏"] = _usd(sum(float(item.get("unrealizedPnl") or 0) for item in usd_rows), signed=True)
        if foreign_unread:
            out["说明"] = "外盘美元账暂时读不到"
        if not front:
            out["更新于"] = str(account.get("updatedAt") or "刚刚")
        return out

    async def _catalog(self, search: str | None = None) -> list[dict[str, Any]]:
        """The instrument catalog (optionally the `search` matches) every voice read resolves names with.
        10-13 production: the call's catalog read came back without items (hotwords were the 75 trading
        words alone, "螺纹" resolved to nothing). The catalog is public market data: when the formal read
        fails, the shared market's own catalog — the one that route serves — is read with the route's
        search rule, and the reason is logged."""
        failure = None
        try:
            result = await self._call("GET /api/instruments", query={"search": search} if search else None)
            if result.get("ok", True) is not False:
                items = list((result.get("data") or {}).get("items") or [])
                if items or search:
                    return items  # a search may honestly match nothing; the whole catalog never is empty
                failure = "ok but no items"  # #840 review r4255069324: the empty answer is the failure too
            else:
                failure = f"status={result.get('status')} detail={str((result.get('data') or {}).get('detail'))[:160]}"
        except HTTPException as error:
            if str(error.detail) == "replay_not_found":
                raise
            failure = f"HTTPException {error.status_code}: {str(error.detail)[:160]}"
        except Exception as error:
            failure = f"{type(error).__name__}: {error}"
        shared = getattr(self.app.state, "shared_market", None)
        if shared is None or _replay_binding.get():
            log.warning("voice catalog read failed (%s), no shared catalog to fall back on", failure)
            return []
        items = [item for item in (await shared.instruments()).get("items") or [] if isinstance(item, dict)]
        needle = (search or "").strip().lower()
        if needle:
            items = [item for item in items if needle in " ".join(
                str(item.get(key, "")) for key in ("symbol", "instrumentId", "instrumentName", "productId")).lower()]
        log.warning("voice catalog read failed (%s); shared catalog served %d items", failure, len(items))
        return items

    async def _resolve_contract(self, query: str, state: CallState) -> dict[str, Any]:
        """{symbol, label, item} for a spoken contract name, or the contract the caller's window shows when
        none was said; {"error": ...} (the words the voice reads) when neither gives one."""
        if query:
            match = resolve_symbol(query, await self._catalog(query))
            if match is None:
                return {"error": f"没找到叫“{query}”的合约，请说品种名"}
            return {"symbol": match["symbol"], "label": match.get("instrumentName"), "item": match}
        window = await self._window(state)
        symbol = ((window or {}).get("state") or {}).get("symbol")
        if not symbol:
            return {"error": "没说品种，软件窗口也不在线"}
        match = resolve_symbol(symbol, await self._catalog(symbol))
        # The catalog's spelling, like a spoken name's: every read below names the contract one way.
        return {"symbol": str(match.get("symbol") or symbol) if match else symbol,
                "label": match.get("instrumentName") if match else None, "item": match}

    async def front_query(self, arguments: dict[str, Any], state: CallState) -> dict[str, Any]:
        """The front desk's one read-only tool (`query`, AI_TRADER_VOICE_FRONT_QUERY): what = quote /
        positions / account / orders / news / brief / analysis, query = the spoken contract. Every read is
        the authenticated route the page and the CLI use (catalog operations); the answer is compact
        Chinese, ≤6 fields, numbers with units — the voice reads it, never infers from it."""
        what = str(arguments.get("what") or "").strip()
        query = str(arguments.get("query") or "").strip()
        if what in {"quote", "positions", "account"}:
            tool = {"quote": "get_quote", "positions": "get_positions", "account": "get_account"}[what]
            return await self._quick_tool(tool, {"query": query} if query and what != "account" else {}, state, front=True)
        try:
            if what == "orders":
                code, activity = await self._http("GET", "/api/account/activity", account_id=self._chat_account_id(state))
                if code == 404 and str(activity.get("detail")) == "replay_not_found":
                    raise HTTPException(404, "replay_not_found")
                if code != 200:
                    return {"error": "委托数据暂时取不到，稍后再问"}
                foreign = self._foreign_ledger(self._chat_account_id(state))
                if foreign:
                    usd_code, usd_activity = await self._http("GET", "/api/account/activity", account_id=foreign)
                    if usd_code == 200 and isinstance(usd_activity.get("orders"), list):
                        activity = {**activity, "orders": [*(activity.get("orders") or []), *usd_activity["orders"]]}
                catalog = await self._catalog()
                names = {str(item.get("symbol", "")).lower(): str(item.get("instrumentName") or "") for item in catalog}
                product = None
                if query:
                    match = resolve_symbol(query, [item for item in catalog if query.lower() in " ".join(
                        str(item.get(k, "")) for k in ("symbol", "instrumentId", "instrumentName", "productId")).lower()])
                    product = str((match or {}).get("productId") or "") or None
                return orders_fields(activity, names, product=product)
            if what == "news":
                # #838 review r4254939616: news is the web-research skill's (get_news_flash) — the same gate the
                # trading AI, MCP and CLI apply (basic tier, guests, globally disabled).
                refused = self._skill_refusal(state.user_id, "get_news_flash")
                if refused is not None:
                    return {"error": "skill_unavailable", "say": refused}
            if what in {"news", "brief"}:
                resolved = await self._resolve_contract(query, state) if query else None
                if resolved is not None and "error" in resolved:
                    return resolved
                item = (resolved or {}).get("item") or {}
                if what == "brief":
                    result = await self._call("GET /api/app/briefs", query={"symbol": resolved["symbol"]} if resolved else None)
                    return brief_fields(result.get("data") or {})
                key = f"{item.get('exchangeId')}.{item.get('productId')}" if item.get("exchangeId") and item.get("productId") else None
                label = re.sub(r"\d+$", "", str(item.get("instrumentName") or query or "")) or None
                result = await self._call("GET /api/research/flash",
                                          query={"hours": 24, "limit": 20, **({"product": key} if key else {})})
                return news_fields(result.get("data") or {}, label)
            if what == "analysis":
                resolved = await self._resolve_contract(query, state)
                if "error" in resolved:
                    return resolved
                symbol, item = resolved["symbol"], resolved["item"] or {}
                result = await self._call("GET /api/markets/{symbol}/workbench", path={"symbol": symbol},
                                          query={"duration": 86400})
                workbench = result.get("data") or {}
                try:
                    analysis = _market_analysis(workbench, lookback=60)
                except (ValueError, KeyError, TypeError):
                    return {"error": "这个合约的日线数据不够，暂时看不了走势"}
                quote = workbench.get("quote") or {}
                closed = workbench.get("marketSessionStatus") == "closed" or quote.get("marketSessionStatus") == "closed"
                fresh = workbench.get("dataStatus") in {"normal", "simulated"} and float(workbench.get("ageSeconds") or 0) <= QUICK_MAX_AGE_SECONDS
                note = session_note(closed=closed, stale=not fresh, has_night=bool(item.get("hasNight")),
                                    exchange=str(item.get("exchangeId") or symbol.split(".")[0]))
                fields = analysis_fields(analysis, str(resolved["label"] or symbol), note=note)
                price = quote.get("lastPrice", quote.get("price"))
                if price is not None:
                    fields = {"合约": fields.pop("合约"),
                              "最新价": f"{_price_text(price)} {price_unit(symbol)}，今天{change_words(quote.get('changePercent'))}",
                              **fields}
                    fields.pop("RSI", None)  # ≤6 fields: the price matters more than the RSI reading
                    if note:
                        fields.pop("均价", None)
                return fields
        except HTTPException as error:
            if str(error.detail) == "replay_not_found":
                raise
            log.info("voice front query %s unavailable: %s", what, error.detail)
            return {"error": "这个暂时查不到，稍后再问"}
        return {"error": "这个查不了"}

    # -- #523 R1c: a front turn keeps the session and account it was accepted on ----------------------

    def binding(self, state: CallState) -> TurnBinding:
        session_id = self._chat_session(state)
        return TurnBinding(session_id, self._chat_account_id(state), state.session_gen)

    async def binding_holds(self, state: CallState, frozen: TurnBinding | None) -> bool:
        """The turn's session and account are still the call's (no hello / window move, the session still bound
        to the account the turn started on — a repeat switch of the same account is not a move), the session still exists and is the user's, and the user still holds
        the account. Anything else retires the result: nothing published, spoken or recorded."""
        if frozen is None or state.closed:
            return False
        if self.binding(state) != frozen:
            return False
        if not frozen.session_id:
            return True
        bridge = self.app.state.app_capabilities
        ctx = getattr(bridge, "ctx", None)
        if ctx is None:
            return True
        identity = self._identity()
        try:
            context = await ctx.account_context(bridge._account_request(identity.headers), identity.user_id,
                                                session_id=frozen.session_id)
        except HTTPException:
            return False  # revoked, or the session is gone
        if state.closed or self.binding(state) != frozen:
            return False  # the call ended or the binding moved while the account was looked up
        return not frozen.account_id or context.account.account_id == frozen.account_id

    def _skill_refusal(self, user_id: str, tool: str) -> str | None:
        """The unified refusal line when the skill owning `tool` is unavailable to this user (globally
        disabled, tier-withheld) — the same gate the trading AI applies at execution time."""
        from .skill_registry import tool_owner_map
        owner = tool_owner_map().get(tool)
        skill = skill_by_id(owner) if owner else None
        if skill is None:
            return None
        reason = skill_block_reason(self.app.state.store, user_id, skill)
        if reason is None:
            return None
        return unavailable_line(skill, reason, parent_block_reason(self.app.state.store, user_id, skill))

    # -- 下单类直接出卡 (AI_TRADER_VOICE_FRONT_ACTION, 2026-10-13 第 2 步) ------------------------------

    async def front_action(self, action: OrderIntent, state: CallState, text: str, *,
                           settle: Callable[[], Any] | None = None, accepted: TurnBinding | None = None) -> dict[str, Any]:
        """The card the words stated, made by the order planner exactly as the trading AI's propose_order makes
        it, on the conversation the call joins — the same contract reading, 今昨 split, readiness and freshness gate
        and refusals as a typed order, behind the same per-act skill gate. `action` is the sentence as the parser read it
        (order_intent_parse): its contract still in words — a catalog name, a spoken month, or none (the
        window's). Returns {"line", "card": (event, payload) | None, "more": [card…] (a sentence the planner makes
        several cards of, asked one by one), "cancel": order | None, "delegate": bool, "binding"}. The conversation the window shows is read first (as a delegate turn does)
        and the session and account are frozen for the turn: the card is made, published and confirmed there or
        nowhere (#523 R1c). Nothing is published or confirmed here: the turn joins the conversation when its line
        is read (`adopt_action`); until then the card is unseen — never confirmable by voice — and a dropped
        round's card is rejected and verified. 一句多单 (the parser's `legs`): each leg's contract is read from its own
        words, the planner cards them in order (or relays them on one contract), each read by its own leg."""
        window = await self._window(state)
        if accepted is not None and self.binding(state) != accepted:
            # A hello / account switch landed while the window was read: the sentence belongs to where it was
            # said, not to where the call is now — retired (#851 review).
            return {"line": None, "card": None, "retired": True, "binding": accepted}
        shown = str(((window or {}).get("state") or {}).get("chatSessionId") or "")
        if shown and shown != state.chat_session_id and self._own_session(state, shown):
            state.chat_session_id = shown
            state.session_gen += 1
        session_id = self._chat_session(state)
        if not session_id and action.act != "cancel":
            created = await self._call("POST /api/sessions", query={"chat": "true"},
                                       body={"mode": "local" if self.local_mode else "cloud"})
            session_id = str((created.get("data") or {}).get("sessionId") or "") or None
            if session_id:
                state.chat_session_id = session_id
                state.session_gen += 1
        binding = self.binding(state)
        if action.act == "cancel":
            refused = self._skill_refusal(state.user_id, "cancel_simulation_order")
            if refused is not None:  # the same order-ops gate the trading AI's cancel tool has
                return {"line": refused, "card": None, "error": "skill_unavailable", "binding": binding}
            return {**await self._front_cancel_lookup(action, state, binding), "binding": binding}
        if not binding.session_id:
            return {"line": "对话还没准备好，请再说一遍。", "card": None, "binding": binding}
        read = [await self._words_contract(leg, window, state) for leg in (action.legs or (action,))]
        unread = next((leg for leg in read if "line" in leg), None)
        if unread is not None:
            return {"line": unread["line"], "card": None, "binding": binding}
        resolved = read[0]  # the first leg names the contract a refusal speaks of
        symbol, label = str(resolved["symbol"]), str(resolved["label"])
        item: dict[str, Any] = resolved["item"]
        intent = replace(replace(action, legs=tuple(leg["intent"] for leg in read)) if action.legs else resolved["intent"],
                         reason=f"语音：{text.strip()}"[:200])
        tool = "propose_order"
        refused = card_skill_block(self.app.state.store, state.user_id, action)
        if refused is not None:
            return {"line": refused, "card": None, "error": "skill_unavailable", "binding": binding}
        identity = self._identity()
        record = self.app.state.broker.get_session(binding.session_id, identity.user_id)
        mode = str(getattr(record, "mode", "") or ("local" if self.local_mode else "cloud"))
        tools = await self._card_tools(binding.session_id, identity)
        if intent.bare and side_unsettled(intent, await tools.account_snapshot()):
            return {"delegate": True, "binding": binding}  # 卖一手 with the long side held: 开空还是平多？
        if needs_account(intent) and not intent.legs:  # legs: the planner settles each leg from one account cut
            scoped = resolve_scope(intent, await tools.account_snapshot())
            if isinstance(scoped, Ask):
                return {"delegate": True, "binding": binding}  # two months / long and short both held: the AI asks
            if isinstance(scoped, Refuse):  # the positions could not be read: never scoped from a failed read
                return {"line": refusal_line(list(scoped.issues)), "card": None, "error": scoped.code,
                        "binding": binding}
            intent = scoped
            symbol = str(intent.symbol or symbol)
        if settle is not None and await self._may_replace_plan(action, intent, read, symbol, state, binding, tools):
            # A new target supersedes the plan in progress (its card is rejected): only once the sentence
            # is settled — a "算了" or a correction must not have cancelled the old plan already.
            if not await settle() or not await self.binding_holds(state, binding):
                # moved on, or the conversation/account moved during the wait: the old plan stays
                return {"line": None, "card": None, "binding": binding, "retired": True}
        if not await self.binding_holds(state, binding):
            # the account moved while the contract, positions and tools were looked up: nothing is created
            return {"line": None, "card": None, "binding": binding, "retired": True}
        turn_id = f"voice-{uuid4().hex[:12]}"
        outcome = await tools.plan_order(intent, mode=mode, session_id=binding.session_id, turn_id=turn_id,
                                         user_id=identity.user_id, origin="ai", intent_source="parser")
        if isinstance(outcome, Ask):
            return {"delegate": True, "binding": binding}
        _, made_cards = tools.outcome_response(tool, outcome, intent=intent)
        events = card_events(made_cards)
        card = events[0] if events else None
        if card is not None:
            more = events[1:]
            for _, payload in [card, *more]:
                if payload.get("proposalId"):
                    state.unseen_cards.add(str(payload["proposalId"]))  # not confirmable by voice until read out
            try:  # naming is decoration: the card exists now and must reach the read-out or the drop, never get lost
                names = catalog_names(await self._catalog())
            except Exception as error:
                log.warning("voice front action catalog names unavailable: %s", error)
                names = {}
            made = [card, *more]
            # Several cards of one sentence (今仓全平 over several contracts): all on screen at once, each read as its
            # own card and asked one by one (所有者口径“各自确认”) — up to the first awaiting confirmation (a card the
            # risk check stopped says why), the next once it is confirmed or dropped (`next_card_line`).
            # Each card said by its own leg (一句多单: its own price words; a relay's first card is the first leg's).
            first_leg = action.legs[0] if action.legs else action
            sources = [made_card.source or first_leg for made_card in outcome.cards] if isinstance(outcome, Cards) else []
            lines = [card_line(sources[index] if index < len(sources) else first_leg, name, payload, names)
                     for index, (name, payload) in enumerate(made)]
            if more:
                asking = next((index for index, (_, payload) in enumerate(made) if _awaits_confirm(payload)), len(made) - 1)
                line = f"一共 {len(lines)} 张卡，一张一张确认。" + "".join(
                    f"第 {index + 1} 张：{lines[index]}" for index in range(asking + 1))
            else:
                line = lines[0]
            if isinstance(outcome, Cards):
                line += other_legs_line(outcome.notes)
            return {"line": line, "card": card, "more": more, "lines": lines, "tool": tool, "binding": binding}
        assert isinstance(outcome, Refuse)
        user_facing = outcome.facing() or {}
        issues = [str(issue) for issue in user_facing.get("candidateIssues") or [] if str(issue).strip()]
        code = "" if outcome.form == "unchanged" else outcome.code  # 已是目标持仓：no card, and no error
        log.info("voice front action %s refused: %s", tool, code or "no_card")
        reopen = None
        reasons = {code, *outcome.readiness_reasons()}
        if reasons & {"market_session_closed", "quote_stale", "quote_not_current", "market_not_fresh"}:
            # Only a refusal about the session itself becomes "开盘后再下"; any other reason keeps the tool's words.
            # After the close the card tool says proposal_gate_not_ready with market_not_fresh among its decision
            # context reasons (#754: 23:00 螺纹 read "行情不是最新状态" aloud) — the market's own session decides.
            market = await self._market_for(symbol)
            if market.get("marketSessionStatus") == "closed":
                reopen = reopen_words(has_night=bool(item.get("hasNight")), exchange=str(item.get("exchangeId") or symbol.split(".")[0]))
        if not issues and reopen is None:
            if user_facing.get("candidateStatus"):
                issues = [str(user_facing["candidateStatus"])]
            else:
                return {"line": "这会儿出不了卡，稍后再说一遍。", "card": None, "error": code or "no_card", "binding": binding}
        return {"line": refusal_line(issues, label=re.sub(r"\d+$", "", label), reopen=reopen), "card": None,
                "error": code or None, "binding": binding}

    async def _words_contract(self, leg: OrderIntent, window: dict[str, Any] | None, state: CallState) -> dict[str, Any]:
        """One leg's contract as the planner takes it: {symbol, label, item, intent}, or {line} when it cannot be
        named. A named month ("螺纹2701") is that contract only; a product name (or the window's contract) is
        whichever month is held — the planner settles that from the account cut (ORDER_INTENT_V2 第 1 步)."""
        spoken = spoken_contract(leg)
        if leg.act == "close_all" and spoken is None:
            return {"symbol": "", "label": "", "item": {}, "intent": leg}  # 全平 / 空单都平了: every position
        # No product named: the contract the window showed in the same snapshot that chose the conversation.
        chart = str(((window or {}).get("state") or {}).get("symbol") or "") or None
        wanted = spoken or chart
        if not wanted:
            return {"line": "没说品种，软件窗口也不在线，请说品种名。"}
        items = await self._catalog(wanted)
        resolved = resolve_words(leg, items, chart) if items else Refuse(
            "instrument_not_found", (f"没找到叫“{wanted}”的合约，请说品种名",))
        if isinstance(resolved, Refuse):
            return {"line": refusal_line(list(resolved.issues))}
        symbol = str(resolved.symbol or "")
        item = next((row for row in items if str(row.get("symbol") or "").lower() == symbol.lower()), {})
        return {"symbol": symbol, "label": str(item.get("instrumentName") or symbol), "item": item, "intent": resolved}

    async def _may_replace_plan(self, action: OrderIntent, intent: OrderIntent, read: list[dict[str, Any]], symbol: str,
                                state: CallState, binding: TurnBinding, tools: Any) -> bool:
        """Whether planning this sentence may supersede a 目标持仓 plan in progress (its card rejected, #518) — the caller
        then waits until the sentence is settled, so a “算了” or a correction never has cancelled the old plan already:
        a target / reverse on a contract with a plan, or (#932 A10) 一句多单 legs that close and touch one contract again
        — the planner relays those as a new plan on that contract — while a plan on that contract is active."""
        store = self._bound_app(state).state.store
        account = binding.account_id or None

        def active(names: list[str]) -> bool:
            return any(store.active_target_plan(user_id=state.user_id, symbol=name, trading_account_id=account)
                       is not None for name in dict.fromkeys(names) if name)
        if action.act in {"target", "reverse"}:
            return active([symbol])
        if not intent.legs:
            return False
        # The planner's own reading of which legs touch one book (`_touched`): the exact contract — two months of a
        # product are two books (rb2701 and rb2705 are independent cards, never a relay, so no wait) — and a close
        # named by its product alone (“平掉螺纹”) reaches whichever month of it is held.
        legs = [(leg, str(entry.get("symbol") or ""), bool(leg.product)) for leg, entry in zip(intent.legs, read, strict=False)]

        def same_book(one: tuple[OrderIntent, str, bool], other: tuple[OrderIntent, str, bool]) -> bool:
            if not one[1] or not other[1]:
                return False
            if one[2] or other[2]:
                return product_of(one[1]) == product_of(other[1])
            return one[1].split(".")[-1].casefold() == other[1].split(".")[-1].casefold()
        relayed = [leg for index, leg in enumerate(legs) for other in legs[index + 1:] + legs[:index]
                   if same_book(leg, other) and not (leg[0].act == other[0].act == "open")]
        if not relayed:
            return False
        names = [symbol for _, symbol, _ in relayed]
        loose = {product_of(symbol) for _, symbol, by_product in relayed if by_product}
        if loose:
            snapshot = await tools.account_snapshot()
            months = [str(row.get("symbol") or "") for row in snapshot.positions if product_of(row.get("symbol")) in loose]
            if not snapshot.readable or not months:
                # The months held cannot be read now, but the planner reads them again (PR #997 review): never skip
                # the wait on a read that says nothing — the sentence settles first.
                return True
            names += months
        return active(names)

    async def _card_tools(self, session_id: str, identity: CallIdentity):
        """The card tools of the conversation's own account: the set the trading AI's turn in that session
        uses (the native adapter builds one per account), else — an adapter without them — the same
        construction the web 目标持仓 route uses. A read-only (viewer) account makes no card."""
        from .trader_tools import account_card_tools
        from .wiring.accounts import require_account_write
        bridge = self.app.state.app_capabilities
        context = await bridge.ctx.account_context(bridge._account_request(identity.headers), identity.user_id,
                                                   session_id=session_id)
        require_account_write(context)
        return account_card_tools(self.app, identity.user_id, context.account,
                                  lambda mode: bridge.ctx.status_for_user(identity.user_id, mode))

    async def _front_cancel_lookup(self, action: OrderIntent, state: CallState, binding: TurnBinding) -> dict[str, Any]:
        """撤单: the one working order the words point at (a named month: that contract; a product: its
        months; nothing named: any), on the turn's account. None working says so; two or more go to the
        trading AI, which asks which one."""
        from .simulation_orders import order_is_working
        working: list[dict[str, Any]] = []
        # 本站模拟两本账：美元账（外盘）的挂单也在找的范围里，每张记着自己的账户，撤单发到那本账。
        for account in [binding.account_id, self._foreign_ledger(binding.account_id or "")]:
            if account is None:
                continue
            code, payload = await self._http("GET", "/api/simulation/orders?recoveryWait=false&active=true",
                                            account_id=account)
            if code != 200:
                return {"line": "委托数据暂时取不到，稍后再说一遍。", "card": None}
            working += [{**order, "_account": account} for order in (payload or {}).get("items") or []
                        if isinstance(order, dict) and order_is_working(order)]
        spoken = spoken_contract(action)
        if spoken:
            resolved = await self._resolve_contract(spoken, state)
            exact = spoken[-1].isdigit()
            wanted = None if "error" in resolved else (str(resolved["symbol"]).lower() if exact else product_of(resolved["symbol"]))
            working = [order for order in working if wanted is not None and (
                str((order.get("request") or {}).get("symbol") or "").lower() if exact
                else product_of((order.get("request") or {}).get("symbol"))) == wanted]
        if not working:
            return {"line": f"{spoken or ''}现在没有挂着没成交的委托。", "card": None}
        if len(working) > 1:
            return {"delegate": True}
        return {"line": None, "card": None, "cancel": working[0]}

    async def cancel_order_now(self, order: dict[str, Any], account_id: str, *,
                               guard: Callable[[], Any] | None = None) -> tuple[str | None, bool]:
        """The looked-up order cancelled through the order panel's own route (once; an unknown outcome is
        never re-sent): (what the read-back says, whether the cancel went through)."""
        request = order.get("request") or {}
        catalog = {str(item.get("symbol", "")).lower(): str(item.get("instrumentName") or "") for item in await self._catalog()}
        symbol = str(request.get("symbol") or "")
        side = {"BUY": "买", "SELL": "卖"}.get(str(request.get("direction") or "").upper(), "")
        offset = "开" if str(request.get("offset") or "").upper() == "OPEN" else "平"
        what = f"{catalog.get(symbol.lower()) or symbol} {side}{offset} {int(request.get('volume') or 0)} 手的挂单"
        if guard is not None and not await guard():
            return None, False  # the call ended or the account moved while the order was named: nothing sent
        code, result = await self._http("POST", f"/api/simulation/orders/{order['clientOrderId']}/cancel", account_id=account_id)
        if code >= 400:
            detail = result.get("detail")
            reason = str(detail.get("code") if isinstance(detail, dict) else detail or code)
            log.warning("voice front cancel failed: %s", reason)
            return (f"{what}这会儿已经不能撤了，可能刚成交或已撤。" if reason == "order_not_cancelable"
                    else f"{what}没撤成，稍后再说一遍撤单。"), False
        status = str(result.get("displayStatus") or "")
        watch = getattr(self.app.state, "order_watch", None)
        if watch is not None:
            await watch.settle(str(order["clientOrderId"]), result)
        if status == "已撤销":
            return f"{what}已撤销。", True
        if status in self.WATCH_UNKNOWN_STATUSES:
            return f"{what}撤单发出去了，结果还没回报，我不再重复撤。", True
        return f"{what}：{status or '撤单已发出'}。", True

    async def adopt_action(self, state: CallState, text: str, outcome: dict[str, Any], *,
                           settle: Callable[[], Any] | None = None) -> str | None:
        """The moment the action's line is read. First the turn's session and account must still be the
        call's (#523 R1c) — otherwise the card is dropped and None returned (nothing spoken or recorded).
        Then a looked-up cancel is sent and, with 语音下单免口头确认 on, the card confirmed — both
        irreversible, so only after `settle()` (the continuation window closed and the turn still current;
        falsy = retired). The card becomes the call's waiting card (spoken 确认 / 算了 act on it), and the
        turn — the words, the final line and the card — joins the frozen conversation. Sets
        outcome["ok"]: a card made, or a cancel that went through."""
        binding = outcome.get("binding")
        card = outcome.get("card")
        if not await self.binding_holds(state, binding):
            await self.drop_action_card(state, outcome)
            return None
        assert isinstance(binding, TurnBinding)
        line = str(outcome.get("line") or "")
        ok = card is not None
        auto = (card is not None and bool(card[1].get("approvalId")) and card[1].get("status") == "approval_required"
                and not card[1].get("skipped")  # a batch that leaves positions out waits for the user's own 确认
                and not outcome.get("more")  # several cards (一句两单) wait for the user's own 确认
                and card[1].get("intentSource") == "parser"  # a card of a sentence of several (parser_legs) too
                and not state.preferences.trade.voiceOrderConfirm and self._simulation_only())
        if (outcome.get("cancel") is not None or auto) and settle is not None:
            if not await settle() or not await self.binding_holds(state, binding):
                await self.drop_action_card(state, outcome)
                return None
        if outcome.get("cancel") is not None:
            cancelled, ok = await self.cancel_order_now(outcome["cancel"], outcome["cancel"].get("_account") or binding.account_id,
                                                        guard=lambda: self.binding_holds(state, binding))
            if cancelled is None:
                return None
            line = cancelled
        more = list(outcome.get("more") or [])
        made_all = [card, *more] if card is not None else []
        # 一句两单：只有念到的第一张要确认的卡在等确认（前面被风控拦下的已念出原因）；其余排在后面，未念到的保持
        # “没听过”、口头确认碰不到，这一张确认或取消后逐张念、逐张问（`next_card_line`，#950 审查）。
        current = next((index for index, made in enumerate(made_all) if _awaits_confirm(made[1])), None)
        if current is not None:
            payload = made_all[current][1]
            lines = list(outcome.get("lines") or [])
            items = [{"proposalId": made[1]["proposalId"], "approvalId": made[1].get("approvalId"),
                      "version": int(made[1].get("version") or 1), "type": made[0].split(".", 1)[1],
                      "summary": _proposal_summary(made[1])} for made in made_all]
            queue = [{"item": items[index] if _awaits_confirm(made_all[index][1]) else None,
                      "line": lines[index] if index < len(lines) else items[index]["summary"], "number": index + 1}
                     for index in range(current + 1, len(made_all))]
            summary = items[current]["summary"]
            outcome["pending"] = {"proposals": [items[current]], "summary": summary, "created_at": time.monotonic(),
                                  "deadline": None, **({"queue": queue} if queue else {})}
            if auto:
                self.arm_action_card(state, outcome)
                # 设置「语音下单免口头确认」(simulation only): confirmed on the user's behalf at once, as for the
                # trading AI's card (_delegate). The words and the card go out first (the confirm's result then
                # updates that card — never a stale actionable copy after it), the final line after.
                turn_id = str(payload.get("turnId") or "") or None
                try:
                    await self.record_quick_turn(state, text, "", card=card, session_id=binding.session_id, turn_id=turn_id)
                except Exception as error:
                    log.warning("voice front action turn not recorded: %s", error)
                confirmed = await self._confirm(state)
                line = (f"{summary}，已按你的设置直接下单。" if confirmed.get("ok")
                        else str(confirmed.get("say") or "下单没有成功。"))
                outcome["ok"] = ok
                try:
                    await self.record_quick_turn(state, text, line, session_id=binding.session_id, turn_id=turn_id,
                                                 publish_user=False)
                except Exception as error:
                    log.warning("voice front action turn not recorded: %s", error)
                return line
        elif card is not None:  # nothing here can be confirmed (risk-blocked): none of it stays unheard
            for _, made in made_all:
                state.unseen_cards.discard(str(made.get("proposalId")))
        outcome["ok"] = ok
        try:
            await self.record_quick_turn(state, text, line, card=card, session_id=binding.session_id,
                                         turn_id=str(card[1].get("turnId") or "") or None if card is not None else None,
                                         more_cards=more)
        except Exception as error:  # the transcript is a record, never the call's lifeline
            log.warning("voice front action turn not recorded: %s", error)
        return line

    def next_card_line(self, state: CallState) -> str:
        """一句两单、各自确认：the next queued card's question — or, when the user already settled it on screen, the
        one after's. The card is only prepared (`state.next_card`, still unheard): it becomes the waiting one when
        this line is committed (`arm_next_card`); a line never read puts it back (`requeue_next_card`) — #932 复核: a
        second 确认 said before the user heard the card must not reach it. "" when none is left, or the call ended
        (its teardown retires the queue)."""
        store = self._bound_app(state).state.store
        blocked = ""
        while state.card_queue and not state.closed:
            entry = state.card_queue.pop(0)
            item = entry["item"]
            if item is None:  # a risk-blocked card: its reason is read, nothing is asked
                blocked += f"第 {entry['number']} 张：{entry['line']}"
                continue
            try:
                waiting = str(store.get(str(item["proposalId"])).get("status")) == "approval_required"
            except Exception:
                waiting = False
            if not waiting:
                continue  # clicked (or expired) meanwhile: nothing to ask
            state.next_card = {"proposals": [item], "summary": item.get("summary") or "",
                               "created_at": time.monotonic(), "deadline": None, "entry": entry, "token": uuid4().hex}
            return f"{blocked}第 {entry['number']} 张：{entry['line']}"
        return blocked

    def _with_next_card(self, state: CallState, result: dict[str, Any], *, nothing: bool = False) -> dict[str, Any]:
        """A spoken 确认 / 取消's result, followed by the next queued card's question once no card waits; `nextCard` is
        the token the line's commit arms (`arm_next_card`). A question an earlier round said but never committed (the
        user talked over it) is asked again here. `nothing`: the result only said no card was waiting — the next
        card's question replaces it."""
        if state.pending is not None:
            return result
        self.requeue_next_card(state, None)
        following = self.next_card_line(state) if state.card_queue else ""
        if not following:
            return result
        prepared = state.next_card
        return {**result, "say": following if nothing else f"{str(result.get('say') or '')}{following}",
                **({"nextCard": prepared["token"]} if prepared else {})}

    def _prepared(self, state: CallState, outcome: dict[str, Any] | None) -> bool:
        prepared, wanted = state.next_card, (outcome or {}).get("nextCard")
        return bool(prepared and wanted and prepared["token"] == wanted)

    def arm_next_card(self, state: CallState, outcome: dict[str, Any] | None) -> None:
        """The line asking the next card is committed (speak / inject): that card becomes the call's waiting card —
        the first card's prepare → commit contract (`arm_action_card`, #851) for every card after it."""
        if not self._prepared(state, outcome) or state.closed:
            return
        prepared, state.next_card = state.next_card, None
        assert prepared is not None
        for item in prepared["proposals"]:
            state.unseen_cards.discard(str(item.get("proposalId")))
        state.pending = {key: value for key, value in prepared.items() if key not in {"entry", "token"}}

    def requeue_next_card(self, state: CallState, outcome: dict[str, Any] | None) -> None:
        """The line asking the next card was never read (the user moved on or barged in): the card was never heard,
        so it stays unheard and goes back to the head of the queue — asked again by the next 确认 / 取消, retired with
        the queue when the call ends or moves on. `outcome` None: whichever question is prepared."""
        if state.next_card is not None and (outcome is None or self._prepared(state, outcome)):
            prepared, state.next_card = state.next_card, None
            state.card_queue.insert(0, prepared["entry"])

    def _take_queue(self, state: CallState) -> list[dict[str, Any]]:
        """The call's queued and prepared cards, taken from the call at once (before any await), as card items."""
        entries, state.card_queue = state.card_queue, []
        prepared, state.next_card = state.next_card, None
        return [*((prepared or {}).get("proposals") or []), *(entry["item"] for entry in entries if entry.get("item"))]

    async def _retire_cards(self, items: list[dict[str, Any]]) -> None:
        for item in items:
            await self._mark_card(item, None)
            await self._reject(item)

    def arm_action_card(self, state: CallState, outcome: dict[str, Any]) -> None:
        """The card becomes the call's waiting card — only when its read-out is committed (speak / inject), so
        a "好" said before the user heard it never confirms it (#851 review). The cards made with it are queued on
        the call (`card_queue`), each asked after it."""
        pending = outcome.pop("pending", None)
        if pending is None:
            return
        for item in pending.get("proposals") or []:
            state.unseen_cards.discard(str(item.get("proposalId")))
        queue = pending.pop("queue", None) or []
        state.card_queue = [*queue, *state.card_queue]
        state.pending = pending

    async def drop_action_card(self, state: CallState, outcome: dict[str, Any]) -> bool:
        """A card made for a round the user never heard (续句, 打断, 算了 while it was being made, the
        conversation or account moved, the call ended): rejected through the card's reject route and read
        back. Until the read-back says rejected it stays unseen — never confirmable by this call's voice."""
        card = outcome.get("card")
        if card is None or not card[1].get("proposalId"):
            return True
        if outcome.get("more"):  # 一句两单: every card of the round goes
            results = [await self.drop_action_card(state, {"card": made}) for made in [card, *outcome["more"]]]
            return all(results)
        proposal_id = str(card[1]["proposalId"])
        state.unseen_cards.add(proposal_id)
        store = self._bound_app(state).state.store

        def rejected() -> bool:
            try:
                return str(store.get(proposal_id).get("status")) != "approval_required"
            except Exception:
                return False
        await self._reject({"proposalId": proposal_id, "version": card[1].get("version", 1)})
        if rejected():
            return True
        # The route failed: the card's Owner itself, once more — the rejection is server state every later
        # call sees (a call-local block alone would let the next call's 确认 find it, #851 review).
        try:
            await self._bound_app(state).state.proposals.reject(
                proposal_id, user_id=state.user_id, proposal_version=int(card[1].get("version") or 1),
                idempotency_key=str(uuid4()))
        except Exception as error:
            log.warning("voice front action card %s owner reject failed: %s", proposal_id, error)
        if rejected():
            return True
        log.error("voice front action card %s not rejected; kept unconfirmable on this call", proposal_id)
        return False

    async def asr_context(self) -> dict[str, Any]:
        """Hotwords for Doubao's ASR: every Chinese product name in the instrument catalog (deduplicated,
        ≤200) plus the trading words, and the replacement pairs for the mishearings measured on 09-19."""
        names: list[str] = []
        items: list[dict[str, Any]] = []
        try:
            items = await self._catalog()
        except Exception as error:
            log.warning("voice hotwords: catalog unavailable: %s: %s", type(error).__name__, error)
        names = self._learn_product_names(items)
        # A colloquial trading word that is itself a catalog product name (沪金) is sent once.
        known = set(names)
        hotwords = [word for word in TRADING_HOTWORDS if word not in known]
        words = [*names[:200 - len(hotwords)], *hotwords]
        return {"hotwords": [{"word": word} for word in words], "correct_words": dict(ASR_CORRECT_WORDS)}

    def _learn_product_names(self, items: list[dict[str, Any]]) -> list[str]:
        """Every Chinese product name in catalog `items` (deduplicated, in catalog order); kept as the routers'
        names when there are any."""
        names = [name for name in catalog_product_names(items) or [] if re.search(r"[\u4e00-\u9fa5]", name)]
        if names:
            self.product_names = sorted(names, key=len, reverse=True)
        return names

    async def ensure_product_names(self) -> None:
        """#754 (10-13 night regression): in the first minutes after a deploy the catalog can be empty when a call
        starts (products=0) — the whole call then names no product: orders went to the trading AI, "螺纹多少钱" read
        the window's contract. Before a sentence is routed with no names, the catalog is read again, at most once
        per CATALOG_RETRY_SECONDS."""
        if self.product_names or (self._names_read_at is not None and time.monotonic() - self._names_read_at < CATALOG_RETRY_SECONDS):
            return
        self._names_read_at = time.monotonic()
        try:
            names = self._learn_product_names(await self._catalog())
        except Exception as error:
            log.warning("voice product names: catalog unavailable: %s: %s", type(error).__name__, error)
            return
        log.info("voice product names reread before routing: products=%d", len(names))

    def named_contract(self, text: str) -> str | None:
        """The catalog product name the utterance mentions (longest first), if any."""
        return next((name for name in self.product_names if name in text), None)

    def dialog_context(self, state: CallState) -> list[dict[str, str]]:
        """The last DIALOG_CONTEXT_ROUNDS user/assistant pairs of the conversation the call joins, each
        cut to DIALOG_CONTEXT_CHARS, cards and tables skipped, ≤DIALOG_CONTEXT_TOTAL characters in all."""
        session_id = self._chat_session(state)
        if not session_id:
            return []
        pairs: list[tuple[str, str]] = []
        pending_user: str | None = None
        try:
            store = self._bound_app(state).state.store
            row = store.connection.execute("SELECT MAX(sequence) AS last FROM events WHERE session_id = ?", (session_id,)).fetchone()
            last = int(row["last"] or 0) if row else 0
            # Only the tail of a long conversation is read (a year of chat must not slow the connect).
            events = store.list_events(session_id, after_sequence=max(0, last - DIALOG_CONTEXT_EVENTS))
        except Exception:
            return []
        for _, event in events:
            if event.get("name") != "message.completed":
                continue
            payload = event.get("payload") or {}
            text = _context_text(str(payload.get("text") or ""))
            if payload.get("role") == "user" and str(payload.get("turnId") or "").endswith("-user"):
                pending_user = text
            elif payload.get("role") == "assistant" and pending_user and text and not payload.get("ended"):
                pairs.append((pending_user, text))
                pending_user = None
        items: list[dict[str, str]] = []
        total = 0
        for user_text, assistant_text in reversed(pairs[-DIALOG_CONTEXT_ROUNDS:]):
            size = len(user_text) + len(assistant_text)
            if total + size > DIALOG_CONTEXT_TOTAL:
                break
            total += size
            items[:0] = [{"role": "user", "text": user_text}, {"role": "assistant", "text": assistant_text}]
        return items

    async def record_quick_turn(self, state: CallState, user_text: str, spoken: str, *,
                                card: tuple[str, dict[str, Any]] | None = None, session_id: str | None = None,
                                turn_id: str | None = None, publish_user: bool = True,
                                more_cards: list[tuple[str, dict[str, Any]]] | None = None) -> None:
        """A turn the front desk answered itself (a quick query; with AI_TRADER_VOICE_FRONT_QUERY every front
        turn — query, explanation, small talk, a half sentence; with AI_TRADER_VOICE_FRONT_ACTION an order
        card, published after the line like a trading-AI card) is part of the conversation like a delegate
        turn: the user's words and what was read out, tagged origin=voice, appended to the session the call
        joins (the daily memory reads these user messages too), and the agent's in-memory history of that
        session takes the same pair, so a typed follow-up after the call continues from it. `session_id`:
        the session the turn was accepted on (#523 R1c) — never re-chosen at write time."""
        session_id = session_id or self._chat_session(state)
        if not session_id or not user_text.strip():
            return
        app = self._bound_app(state)
        broker = app.state.broker
        # An action card's own turn id: the spoken line and its card are one assistant turn (#851 review).
        turn_id = turn_id or f"voice-{uuid4().hex[:12]}"
        if publish_user:
            await broker.publish(session_id, kind="message", name="message.completed",
                                 payload={"turnId": f"{turn_id}-user", "role": "user", "text": user_text.strip(), "origin": "voice"})
        if spoken.strip():
            await broker.publish(session_id, kind="message", name="message.completed",
                                 payload={"turnId": turn_id, "role": "assistant", "agentId": "native", "text": spoken.strip(), "origin": "voice"})
        for made in ([card] if card is not None else []) + list(more_cards or []):
            await broker.publish(session_id, kind="approval", name=made[0], payload=made[1])
        adapter = app.state.adapter
        target = adapter.adapter_for_user(state.user_id) if hasattr(adapter, "adapter_for_user") else adapter
        append = getattr(target, "append_voice_turn", None)
        if callable(append):
            append(session_id, user_text.strip(), spoken.strip())

    async def run_intent(self, intent: Intent, state: CallState) -> dict[str, Any]:
        """The zero-LLM fast path: one UI command (plus its follow-ups) on the caller's window with a real
        receipt, and the one-line result to speak. Anything not `applied` reports ok=False so the caller
        falls back to the agent. A contract name is resolved through the catalog first.
        The executor (`turn_router.run_window_intent`) is shared with the typed fast path, so a spoken
        and a typed "把设置打开" take literally the same command path."""
        window = await self._window(state)
        if window is None:
            return {"ok": False, "error": "no_window"}
        return await run_window_intent(intent, window=window, call=self._call)

    async def tour_active(self, state: CallState) -> bool:
        """Whether the caller's window is in a guided tour (the window's own `tour.active`)."""
        window = await self._window(state)
        return bool(((window or {}).get("state") or {}).get("tour", {}).get("active"))

    async def editor_open(self, state: CallState) -> bool:
        """Whether the caller's window shows the 策略编辑卡 (the window's own `strategyEditor.open`)."""
        from .strategy_editor import editor_open
        window = await self._window(state)
        return editor_open((window or {}).get("state"))

    async def _editor_ask(self, utterance: str, state: CallState) -> dict[str, Any]:
        """补一句 by voice through the shared `run_editor_ask` (the typed turn's too); the voice says so."""
        from .strategy_editor import EDITOR_ASK_FAILED, EDITOR_ASK_LINE
        window = await self._window(state)
        if window is None:
            return {"ok": False, "error": "no_window", "say": "软件窗口现在不在线。", "spoken": "软件窗口现在不在线。"}
        applied = await run_editor_ask(utterance, window=window, call=self._call)
        line = EDITOR_ASK_LINE if applied else EDITOR_ASK_FAILED
        return {"ok": applied, "say": line, "spoken": line}

    async def _tour(self, op: str, state: CallState) -> dict[str, Any]:
        """Move the guided tour the user is in: one UI command with a real receipt, then the new step's
        `say` (or its label) is what the voice reads; past the last step, or on stop, the tour ends."""
        window = await self._window(state)
        if window is None:
            return {"ok": False, "error": "no_window", "say": "软件窗口现在不在线。"}
        return await run_tour_step(op, window=window, call=self._call)

    async def _delegate(self, utterance: str, state: CallState, *, progress=None, on_sentence=None,
                        follow_up: bool = False) -> dict[str, Any]:
        if not utterance:
            return {"ok": False, "error": "empty", "say": "没听清，请再说一遍。"}
        if state.replay_id:
            # The replay product boundary (its adapter refuses general AI: no future information may
            # enter a historical replay). Everything the call CAN do stays: quotes, positions, the
            # account, cards and the playback words.
            return {"ok": False, "error": "delegate_disabled_in_replay",
                    "say": "回放中不连接通用 AI，以免引入未来信息。行情、持仓、账户和回放控制可以直接说。"}
        window = await self._window(state)
        if window is None:
            return {"ok": False, "error": "no_window", "say": "软件窗口现在不在线。"}
        preserve_pending = False
        card_reply = False
        if self._has_pending(state):
            self._resolve_pending(state)  # a fresh call may only know the server's awaiting card
            # Use the same full turn judgment as Native, not a keyword shortcut around confirmation.
            # With a waiting card Native also leaves editor routing off: non-UI replies keep its
            # full card toolbox. Catalog failure has the router's existing shapes-only semantics.
            try:
                catalog = await self._call("GET /api/instruments")
                product_names = catalog_product_names(list((catalog.get("data") or {}).get("items") or []))
            except Exception:
                product_names = None
            route = classify_turn(
                utterance, has_pending_card=True, ui_available=True, catalog=product_names,
                strategies_available="explain_strategy_signal" not in hidden_tool_names(
                    self._bound_app(state).state.store, self._identity().user_id),
                editor_open=False)
            preserve_pending = route.kind == "ui_intent"
            card_reply = route.pending_card_reply
        # The cards this delegate retires: every waiting card unless the turn is fixed-table navigation.
        retired_cards = [] if preserve_pending else list((state.pending or {}).get("proposals") or [])
        reply_session = self._chat_session(state) if card_reply else None
        reply_account = (self._chat_account_id(state) or self._bound_app(state).state.accounts.default.account_id) if card_reply else None

        async def cards_verified(*, rejected: bool) -> bool:
            # A card reply also proves the card's owner and version; any retirement proves the
            # server-side rejection (#756: "取消这张卡，切到日线" must not leave it confirmable).
            try:
                if not retired_cards:
                    return False
                for item in retired_cards:
                    code, record = await self._http("GET", f"/api/proposals/{item['proposalId']}")
                    if (code != 200
                            or (card_reply and (
                                record.get("sessionId") != reply_session
                                or str((window.get("state") or {}).get("chatSessionId") or reply_session) != reply_session
                                or record.get("tradingAccountId") != reply_account
                                or record.get("version") != item.get("version")
                                or record.get("approvalId") != item.get("approvalId")))
                            or (rejected and (record.get("status") != "rejected"
                                             or not record.get("approvalUsedAt")))):
                        return False
                return True
            except Exception:
                return False

        if card_reply and not await cards_verified(rejected=False):
            return {"ok": False, "error": "pending_reject_unconfirmed",
                    "say": "旧卡归属或状态尚未确认，尚未处理这次修改。请先处理原卡。"}
        if not preserve_pending:
            await self._abandon_pending(state)
        if (card_reply or retired_cards) and not await cards_verified(rejected=True):
            return {"ok": False, "error": "pending_reject_unconfirmed",
                    "say": "旧卡还未确认作废，尚未处理这次修改。请先处理原卡。" if card_reply
                    else "旧卡还未确认作废，这句话没有处理。请先处理原卡。"}
        body: dict[str, Any] = {"text": utterance, "mode": "local" if self.local_mode else "cloud", "origin": "voice",
                                "uiWindowId": window["windowId"], "timeoutSeconds": DELEGATE_TIMEOUT_SECONDS,
                                **({"followUp": True} if follow_up else {})}
        # The conversation the window shows right now (its pulse carries `chatSessionId` since 1.6.3):
        # a user who pressed 新对话 mid-call expects the next spoken turn there, not in the session
        # named at hello time.
        shown = str(((window.get("state") or {}).get("chatSessionId")) or "")
        if shown:
            if shown != state.chat_session_id:
                state.session_gen += 1  # #523 R1c: front results accepted on the old conversation retire
            state.chat_session_id = shown
        session_id = self._chat_session(state)
        if not session_id and on_sentence is not None:
            # Streaming needs the session before the turn starts (the listener is per session).
            try:
                created = await self._call("POST /api/sessions", query={"chat": "true"}, body={"mode": body["mode"]})
                session_id = str((created.get("data") or {}).get("sessionId") or "") or None
                state.chat_session_id = session_id or state.chat_session_id
            except Exception as error:
                log.warning("voice delegate could not open a chat session for streaming: %s", error)
        if session_id:
            body["sessionId"] = session_id
        unsubscribe = (self._subscribe_turn_text(session_id, on_sentence, utterance,
                                                 on_run_id=lambda run_id: setattr(state, "inflight_run_id", run_id))
                       if session_id and on_sentence is not None else None)
        task = asyncio.ensure_future(self._call(CHAT_SEND, body=body,
                                               **({"pending_card_reply": True} if card_reply else {})))
        try:
            while True:
                done, _ = await asyncio.wait({task}, timeout=PROGRESS_SECONDS)
                if done:
                    break
                if progress is not None:
                    await progress()
        except asyncio.CancelledError:
            task.cancel()
            raise
        finally:
            if unsubscribe is not None:
                unsubscribe()
        try:
            result = task.result()
        except HTTPException as error:
            if error.status_code == 404 and session_id:
                # The remembered session is gone (released or expired): start a fresh one, once.
                state.chat_session_id = None
                body.pop("sessionId", None)
                result = await self._call(CHAT_SEND, body=body,
                                          **({"pending_card_reply": True} if card_reply else {}))
            else:
                raise
        data = result.get("data") or {}
        state.chat_session_id = data.get("sessionId") or state.chat_session_id
        reply = str(data.get("reply") or "")
        run_status = data.get("runStatus")
        pending = list(data.get("pendingProposals") or [])
        if data.get("timedOut"):
            line = "这个还在处理，好了会显示在对话里，你可以先问别的。"
            return {"ok": False, "runStatus": run_status, "timedOut": True, "sessionId": data.get("sessionId"), "spoken": line, "say": line}
        if run_status != "completed":
            spoken = first_sentence(reply) if reply else "后台没有处理成功，请再说一遍。"
            return {"ok": False, "runStatus": run_status, "sessionId": data.get("sessionId"), "spoken": spoken, "say": spoken,
                    "warnings": data.get("warnings")}
        # No `spoken` from the agent: read the answer itself (bounded), never just its first sentence.
        # A contract code in parentheses ("（SHFE.rb2610）") is dropped from the agent's line too.
        spoken = (foreign_codes_named(CONTRACT_CODE.sub("", str(data.get("spoken") or "").strip()))
                  or _speakable(reply) or "好了，结果在对话里。")
        pending = list({str(item.get("proposalId")): item for item in pending}.values())  # a card re-sent is one card
        if len(pending) > 1:
            # Several cards from one trading-AI turn (今仓全平几个合约): asked one by one, like the front desk's — only the
            # first waits for 确认; the rest stay unheard (no spoken 确认 reaches them) until each is read in turn,
            # and a call that ends or moves on drops them (`_abandon_pending`). Never confirmed on the user's behalf.
            first, rest = pending[0], pending[1:]
            for item in rest:
                state.unseen_cards.add(str(item.get("proposalId")))
            queue = [{"item": item, "line": str(item.get("summary") or ""), "number": index}
                     for index, item in enumerate(rest, 2)]
            if state.closed:  # the call ended while the turn ran: nobody will ask the rest, its teardown is past
                await self._retire_cards([entry["item"] for entry in queue])
            else:
                state.card_queue = [*queue, *state.card_queue]
            state.pending = {"proposals": [first], "summary": str(first.get("summary") or ""),
                             "created_at": time.monotonic(), "deadline": None}
            spoken = (f"{spoken.rstrip('。')}。一共 {len(pending)} 张卡，一张一张确认。"
                      f"第 1 张：{first.get('summary') or ''}，确认吗？")
            return {"ok": True, "runStatus": run_status, "sessionId": data.get("sessionId"), "spoken": spoken,
                    "content": reply[:1500], "pendingConfirmation": True,
                    "tools": [s.get("tool") for s in data.get("toolSteps") or [] if s.get("tool")]}
        if pending:
            summary = "；".join(str(item.get("summary") or "") for item in pending if item.get("summary"))
            if not spoken.rstrip("。.").endswith(("吗", "吗？", "吗?", "？", "?")):
                # The read-back always ends as a question: what to confirm, then "确认吗".
                spoken = f"{spoken.rstrip('。')}。{summary}，确认吗？" if summary and summary not in spoken else f"{spoken.rstrip('。')}，确认吗？"
            state.pending = {"proposals": pending, "summary": summary, "created_at": time.monotonic(), "deadline": None}
            # 意图重构第 3 步: only a card the fast parser read from the words may be confirmed for the user; the trading
            # AI's propose_order card (a model's reading) is read out and waits for the user's own 确认.
            parsed = all(item.get("intentSource") == "parser" for item in pending)
            if (not state.preferences.trade.voiceOrderConfirm and self._simulation_only() and not data.get("routine")
                    and parsed):
                # 设置「语音下单免口头确认」(simulation only): the card is confirmed on the user's behalf at once.
                confirmed = await self._confirm(state)
                spoken = (f"{summary}，已按你的设置直接下单。" if confirmed.get("ok") else str(confirmed.get("say") or "下单没有成功。"))
                return {"ok": bool(confirmed.get("ok")), "runStatus": run_status, "sessionId": data.get("sessionId"), "spoken": spoken,
                        "say": spoken, "content": reply[:1500], "pendingConfirmation": False, "autoConfirmed": True,
                        "tools": [s.get("tool") for s in data.get("toolSteps") or [] if s.get("tool")]}
        return {"ok": True, "runStatus": run_status, "sessionId": data.get("sessionId"), "spoken": spoken,
                "content": reply[:1500], "pendingConfirmation": bool(pending),
                "tools": [s.get("tool") for s in data.get("toolSteps") or [] if s.get("tool")]}

    def _simulation_only(self) -> bool:
        """Live trading is never a Runtime capability; the flag is read, not assumed, before an order is
        confirmed without the spoken word."""
        from .runtime_presentation import describe_runtime
        try:
            return describe_runtime(self._bound_app().state.data_provider.runtime_status())["liveTrading"] is False
        except Exception:
            return False

    def _subscribe_turn_text(self, session_id: str, on_sentence, utterance: str, *, on_run_id=None) -> Callable[[], None] | None:
        """Read the agent's answer as it streams: the first assistant turn to appear after this call is
        the delegate's; its first paragraph goes out sentence by sentence through `on_sentence(text)`,
        then `on_sentence(None)` marks the end (with `streamer.truncated` when details were skipped).
        `on_run_id(run_id)` gets the turn's run id from its task.started event, when the adapter
        offers that callback (native_agent does), so an interrupt can cancel the run server-side."""
        adapter = getattr(self.app.state, "adapter", None)
        subscribe = getattr(adapter, "subscribe_turn_text", None)
        if subscribe is None:
            return None
        streamer = SentenceStreamer()
        # Turns queue on one session: bind to the assistant turn that follows OUR user message, not to
        # whatever answer is streaming when we subscribe (an earlier question's, when the user spoke over it).
        # The text match is the fallback binder (#523): a keyboard message with the same words can be
        # claimed by mistake, so the run's own user turn id (`{run_id}-user`, from task.started) rechecks
        # the claim once the run starts — not ours is withdrawn, and the real user turn binds after it.
        turn: dict[str, Any] = {"id": None, "asked": False, "asked_ids": set(), "checked": False}

        def started(run_id: str) -> None:
            if turn["asked"] and on_run_id is not None:
                on_run_id(run_id)
            if (turn["asked"] and not turn["checked"]
                    and f"{run_id}-user" not in turn["asked_ids"]):
                turn["asked"] = False  # a same-text typed message was mis-claimed; ours is still queued

        def listener(update: dict[str, Any]) -> None:
            if update.get("role") == "user":
                if update["text"] == utterance:
                    turn["asked"] = True
                    turn["asked_ids"].add(str(update["turnId"]))
                return
            if not turn["asked"]:
                return
            if turn["id"] is None:
                turn["id"] = update["turnId"]
                turn["checked"] = True
            if update["turnId"] != turn["id"]:
                return
            text = update["text"]
            if text == PROCESSING_PLACEHOLDER:
                return
            sentences = streamer.finish(text) if update["final"] else streamer.push(text)
            for sentence in sentences:
                on_sentence(sentence)
            if update["final"]:
                skipped = streamer.skipped_line()
                if skipped:
                    on_sentence(skipped)
                on_sentence(None)
        if "on_run_id" in inspect.signature(subscribe).parameters:
            return subscribe(session_id, listener, on_run_id=started)
        return subscribe(session_id, listener)

    # -- spoken confirmation of the agent's proposal cards ---------------------------------------

    # Codes the server itself declares final for the card: the approval is spent or the card moved on.
    # Anything else (a stale quote, a closed session, a network error, a 5xx) leaves the card waiting
    # for the next "确认" — the 09-21 call lost a perfectly good card to a 2 ms quote_stale (a2c11123).
    CONFIRM_TERMINAL_CODES = frozenset({"approval_already_used", "approval_expired", "invalid_state_transition",
                                        "stale_proposal_version", "proposal_not_found"})
    QUOTE_STALE_CODES = frozenset({"quote_stale", "quote_not_current"})

    def _server_cards(self, state: CallState) -> list[dict[str, Any]]:
        """Every card the server still holds open for this conversation (approval_required, its approval unused and
        unexpired, not a voice action's card the user never heard), newest first, as the call's pending items."""
        session_id = state.chat_session_id or self._chat_session(state)
        if not session_id:
            return []
        try:
            records = self._bound_app(state).state.store.pending_proposals(session_id)
        except Exception:
            return []
        now = datetime.now(UTC)
        waiting: list[dict[str, Any]] = []
        for record in records:  # newest first
            if record.get("approvalUsedAt") or str(record.get("id")) in state.unseen_cards:
                continue  # used, or a voice action's card the user never heard (not confirmable by voice)
            expires = str(record.get("approvalExpiresAt") or "")
            if expires:
                try:
                    if datetime.fromisoformat(expires.replace("Z", "+00:00")).astimezone(UTC) <= now:
                        continue
                except ValueError:
                    continue
            waiting.append(record)
        return [{"proposalId": record["id"], "approvalId": record.get("approvalId"),
                 "version": int(record.get("version") or 1), "type": record.get("proposalType"),
                 "summary": _proposal_summary(record.get("payload") or {})} for record in waiting]

    def _server_pending(self, state: CallState) -> dict[str, Any] | None:
        """The one card the server still holds open for this conversation: what a spoken 确认 acts on when the call's
        own record is gone — a card made from the keyboard, or one whose earlier confirm failed (09-21: two further
        "确认" landed as chat because state.pending had been cleared upfront). Several cards nobody read out or
        pointed at (一句多单 made on screen) are no single target: a bare 确认 picks none of them (意图重构第 3 步)."""
        cards = self._server_cards(state)
        if len(cards) != 1:
            return None
        return {"proposals": cards, "summary": cards[0]["summary"], "created_at": time.monotonic(), "deadline": None}

    def _several_waiting_line(self, state: CallState) -> str | None:
        """What a bare 确认 / 取消 says when several cards wait and none is the call's: which way out there is."""
        count = len(self._server_cards(state))
        return f"有 {count} 张卡在等确认，请在卡上点，或说“都不要”全部作废。" if count > 1 else None

    def _has_pending(self, state: CallState) -> bool:
        """A "确认" is routable when the call holds a live card or the server does (the same test the
        router uses, so the word never falls through to chat while a card waits anywhere)."""
        if state.pending and not self._pending_expired(state):
            return True
        if state.next_card is not None or any(entry.get("item") for entry in state.card_queue):
            return True  # 一句两单: the next card is still to be asked — the words are about the cards, never chat
        return bool(self._server_cards(state))  # one or several: 确认 / 取消 / 都不要 are about cards, never chat

    def _resolve_pending(self, state: CallState) -> dict[str, Any] | None:
        """The card a spoken 确认 or 取消 acts on — the one source for both (#717): the call's own
        record, and when it is gone (a card made from the keyboard, a record cleared after a failed
        confirm — the 09-21 cases `_server_pending` was written for) the server's waiting card. The
        router hears a card whenever `_has_pending` does, so a spoken 取消 must land on that same
        card, not be told "现在没有等确认的操作" while it still waits."""
        if not state.pending:
            state.pending = self._server_pending(state)
        return state.pending

    def _pending_contract(self, item: dict[str, Any]) -> tuple[str, str]:
        """(display name, symbol) of a waiting card's contract, from the stored payload."""
        try:
            payload = self._bound_app().state.store.get(str(item.get("proposalId"))).get("payload") or {}
        except Exception:
            payload = {}
        return str(payload.get("name") or payload.get("symbol") or ""), str(payload.get("symbol") or "")

    async def _market_for(self, symbol: str) -> dict[str, Any]:
        if not symbol:
            return {}
        try:
            result = await self._call("GET /api/market/{symbol}", path={"symbol": symbol})
        except Exception:
            return {}
        return result.get("data") or {}

    async def _confirm_failure_line(self, state: CallState, failed: list[str]) -> str:
        """What a failed confirm says, in Chinese, by cause (09-21: "确认没有成功：quote_stale" told the
        user nothing). The English codes stay in the tool result for the transcript."""
        code = str(failed[0]) if failed else ""
        pending = state.pending or {}
        items = pending.get("proposals") or []
        if code in self.QUOTE_STALE_CODES:
            name, symbol = self._pending_contract(items[0]) if items else ("", "")
            market = await self._market_for(symbol)
            age = _quote_age_seconds(market)
            label = name or symbol or "这个合约"
            quiet = f"已 {int(age)} 秒" if age is not None and age > 0 else ""
            if symbol:
                # the quote watcher (below) picks this up and says so the moment the quote is current
                pending["stale_quote"] = {"symbol": symbol, "quoted_at": str(market.get("quoteDatetime") or ""), "reminded": False}
            return f"{label} {quiet}没有新报价，这会儿不能下单，卡还在，有新报价我提醒你再说确认。".replace("  ", " ")
        if code == "market_session_closed":
            # #717: the card cannot survive to the next session (prices move; the approval expires), so
            # the line promises nothing and `_confirm` rejects the card right away — copy and card agree.
            return "现在不在交易时段，这张卡下不了，开盘后再跟我说一遍。"
        if code in self.CONFIRM_TERMINAL_CODES:
            return {"approval_already_used": "这张卡已经确认过了。",
                    "approval_expired": "确认超时，这张卡已经失效。"}.get(code, "这张卡的状态变了，请看对话里的卡片。")
        if code == "network_error":
            return "后台没有回应，卡还在，稍后再说一遍确认。"
        return "确认没有成功，卡还在，稍后再说一遍确认。"

    async def _confirm(self, state: CallState) -> dict[str, Any]:
        # The call lost its own record (a race, a restart): the server's waiting card is the truth —
        # the same source a spoken 取消 reads (#717), never two different truths for one card.
        pending = self._resolve_pending(state)
        if pending and any(str(item.get("proposalId")) in state.unseen_cards for item in pending.get("proposals") or []):
            pending = state.pending = None  # a card the user never heard is never confirmed by voice
        if not pending:
            several = self._several_waiting_line(state)
            return {"ok": False, "error": "ambiguous_pending" if several else "nothing_pending",
                    "say": several or "现在没有等确认的操作。"}
        if self._pending_expired(state):
            await self._abandon_pending(state, keep_queue=True)  # run_tool asks the next queued card
            return {"ok": False, "error": "confirmation_expired", "say": "确认超时，已放弃，需要的话再说一遍。"}
        results: list[dict[str, Any]] = []
        failed: list[str] = []
        finished: list[str] = []  # done either way: confirmed, or terminal server-side
        mismatched = False
        linked_done: set[str] = set()  # 全平两本账: the other ledger's card the server confirmed with this one
        for item in pending["proposals"]:
            if str(item.get("proposalId")) in linked_done:
                finished.append(str(item.get("proposalId")))
                continue
            body = {"approvalId": item.get("approvalId"), "proposalVersion": int(item.get("version") or 1), "idempotencyKey": str(uuid4())}
            # The countdown leaves the card before the click lands: a confirmed card is terminal and
            # can no longer be written to (terminal_proposal_immutable). A confirm that fails for a
            # repairable reason re-opens the window below, so the card never loses its countdown.
            await self._mark_card(item, None)
            pending["deadline"] = None
            try:
                code, result = await self._http("POST", f"/api/proposals/{item['proposalId']}/confirm", body)
            except Exception as error:
                log.warning("voice confirm failed: %s", error)
                failed.append("network_error")
                continue
            if code >= 400:
                detail = result.get("detail")
                error_code = str(detail.get("code") if isinstance(detail, dict) else detail or code)
                failed.append(error_code)
                if error_code in self.CONFIRM_TERMINAL_CODES:
                    finished.append(str(item.get("proposalId")))
                    await self._reject(item)  # no zombie card behind a verdict the server calls final
                elif error_code == "approval_mismatch":
                    mismatched = True  # the card was re-approved since we read it; re-read below
                continue
            for proposal_id, answer in [(item.get("proposalId"), result), *(
                    (linked.get("proposalId"), linked.get("result") or {}) for linked in result.get("linkedResults") or []
                    if isinstance(linked, dict) and linked.get("status") == "ok")]:
                orders = answer.get("simulationOrders") or ([answer["simulationOrder"]] if answer.get("simulationOrder") else [])
                results.append({"proposalId": proposal_id, "type": item.get("type"), "status": answer.get("status"),
                                "message": answer.get("message"),
                                "orders": [{"clientOrderId": o.get("clientOrderId"), "displayStatus": o.get("displayStatus"),
                                            "symbol": (o.get("request") or {}).get("symbol"), "limitPrice": (o.get("request") or {}).get("limitPrice"),
                                            "error": o.get("lastReadError")} for o in orders]})
                if proposal_id != item.get("proposalId"):
                    linked_done.add(str(proposal_id))
            finished.append(str(item.get("proposalId")))
        if state.pending:
            remaining = [item for item in state.pending.get("proposals") or [] if str(item.get("proposalId")) not in finished]
            if remaining:
                state.pending["proposals"] = remaining
                state.pending["summary"] = "；".join(str(i.get("summary") or "") for i in remaining if i.get("summary"))
            else:
                state.pending = None
        if not results:
            if mismatched and state.pending is None:
                state.pending = self._server_pending(state)  # re-read the re-approved card for the next 确认
            line = await self._confirm_failure_line(state, failed)
            if failed and failed[0] == "market_session_closed":
                # #717: out-of-session the card can never be confirmed (prices move by the open, and
                # the approval itself expires) — reject it now instead of reopening a window that
                # abandons it 15 s later behind a line that promised it was still there.
                await self._abandon_pending(state)
                return {"ok": False, "error": "confirm_failed", "detail": failed, "say": line}
            # the card stays: the window restarts so the user can say 确认 again once the cause is gone
            await self._open_confirm_window(state, time.monotonic() + state.preferences.voice.confirmWindowSeconds)
            return {"ok": False, "error": "confirm_failed", "detail": failed, "say": line}
        items: list[dict[str, Any]] = []
        if any(entry["orders"] for entry in results):
            try:
                items = await self._catalog()
            except Exception as error:  # the read-back still says what happened, by code
                log.warning("voice confirm: catalog unavailable for names: %s: %s", type(error).__name__, error)
        spoken_parts: list[str] = []
        for entry in results:
            if entry["orders"]:
                spoken_parts.append("；".join(
                    _order_outcome_line(o, catalog_contract_label(items, str(o.get("symbol") or "")) if o.get("symbol") else "")
                    for o in entry["orders"]))
            else:
                spoken_parts.append(str(entry.get("message") or "已记录，结果在对话里"))
        say = "已确认，" + "。".join(spoken_parts) + "。" + ("另有一部分没成功，请看对话里的卡片。" if failed else "")
        return {"ok": True, "results": results, "failed": failed or None, "say": say}

    async def _mark_card(self, item: dict[str, Any], confirm_by: str | None) -> None:
        """Write the spoken confirmation window to the card and re-send the card so the open
        conversation shows the countdown; the client keys cards by proposalId, so this is an update."""
        try:
            app = self._bound_app()
            record = app.state.store.mark_voice_confirm(str(item["proposalId"]), confirm_by)
            name = {"trade": "proposal.trade", "close_batch": "proposal.close_batch",
                    "spread": "proposal.spread"}.get(str(record.get("proposalType")))
            if name:
                await app.state.broker.publish(record["sessionId"], kind="approval", name=name, payload=record["payload"])
        except Exception as error:
            log.debug("voice card mark skipped: %s", error)

    async def _reject(self, item: dict[str, Any]) -> None:
        try:
            await self._http("POST", f"/api/proposals/{item.get('proposalId')}/reject",
                             {"proposalVersion": int(item.get("version") or 1), "idempotencyKey": str(uuid4())})
        except Exception as error:
            log.debug("voice reject skipped: %s", error)

    async def _abandon_pending(self, state: CallState, *, keep_queue: bool = False) -> bool:
        """The waiting card is rejected — and the cards queued behind it (一句两单, not yet read) too, unless the
        caller goes on to ask them (`keep_queue`): a call that ends or moves on leaves none of them confirmable by a
        later call (#950 review). Both are taken from the call before the first await, so a hang-up during a
        取消 / 超时 hand-over still finds the queue on the call and its teardown retires it (#932 复核). A retire cut off
        part-way (the teardown cancels the announcer rejecting a timed-out card) leaves the cards it had not finished
        on the call (`stranded_cards`), and the next abandon — the teardown's — retires them: a card taken off the
        call is never only a local of a cancelled task (#932 复核 10-16 16:56)."""
        pending, state.pending = state.pending, None
        queued = [] if keep_queue else self._take_queue(state)
        stranded, state.stranded_cards = state.stranded_cards, []
        items = [*((pending or {}).get("proposals") or []), *queued]
        if not items and not stranded:
            return False
        left = [*stranded, *items]
        try:
            while left:
                await self._mark_card(left[0], None)
                await self._reject(left[0])
                left.pop(0)
        except asyncio.CancelledError:
            state.stranded_cards.extend(left)
            raise
        return bool(items)

    async def _open_confirm_window(self, state: CallState, deadline: float) -> None:
        """The confirm window (preferences.voice.confirmWindowSeconds, default 15 s) belongs to the
        last thing the user heard: it (re)starts whenever a line finishes playing or the user barges
        in while a card waits, so a multi-sentence read-back or a spoken failure line always leaves a
        full window. A deadline that moved less than a second is not re-sent — streaming sentences
        must not flood the conversation with card updates."""
        pending = state.pending
        if not pending:
            return
        current = pending.get("deadline")
        if current is not None and deadline - current < 1.0:
            return
        pending["deadline"] = deadline
        pending["marked_at"] = time.monotonic()
        confirm_by = (datetime.now(UTC) + timedelta(seconds=max(0.0, deadline - time.monotonic()))).isoformat()
        for item in pending.get("proposals") or []:
            await self._mark_card(item, confirm_by)

    def _pending_expired(self, state: CallState) -> bool:
        pending = state.pending
        if not pending:
            return False
        deadline = pending.get("deadline")
        if deadline is None:
            return time.monotonic() > pending["created_at"] + PENDING_HARD_CAP_SECONDS
        return time.monotonic() > deadline

    # -- the unfilled-order watchdog's two spoken commands (09-21) ------------------------------

    # The read says the gateway itself lost the answer: only ever queried again, never re-sent.
    WATCH_UNKNOWN_STATUSES = frozenset({"结果未知", "身份异常", "回报异常"})
    CHASE_CANCEL_WAIT_SECONDS = 12.0

    def order_watch_target(self, user_id: str) -> dict[str, Any] | None:
        """The confirmed-but-unfilled order "追单" / "撤单" act on, when the watchdog holds one."""
        watch = getattr(self._bound_app().state, "order_watch", None)
        return watch.target_for(user_id) if watch is not None else None

    async def _watched_order_call(self, target: dict[str, Any], action: str) -> tuple[str, dict[str, Any] | None]:
        """One action on the watched order over its own account; returns (displayStatus, view). The
        explicit account header is only sent for a non-default account: a caller without grant rows
        resolves to the default anyway, and an explicit ungranted target is refused outright."""
        accounts = getattr(self._bound_app().state, "accounts", None)
        account_id = str(target.get("accountId") or "")
        if accounts is not None and account_id == accounts.default.account_id:
            account_id = ""
        code, result = await self._http("POST", f"/api/simulation/orders/{target['clientOrderId']}/{action}",
                                        account_id=account_id)
        if code >= 400:
            detail = result.get("detail")
            raise HTTPException(status_code=code, detail=detail.get("code") if isinstance(detail, dict) else detail)
        view = result if isinstance(result.get("displayStatus"), str) else None
        return str(view.get("displayStatus")) if view else "", view

    async def _cancel_watched_order(self, state: CallState) -> dict[str, Any]:
        """撤单: one cancel through the normal route (never re-sent while unconfirmed or UNKNOWN),
        then what the read-back says, spoken plainly."""
        target = self.order_watch_target(state.user_id)
        if target is None:
            return {"ok": False, "error": "nothing_watched", "say": "现在没有看着的挂单。"}
        try:
            status, view = await self._watched_order_call(target, "cancel")
        except HTTPException as error:
            code = str(error.detail)
            if code == "order_not_cancelable":
                return {"ok": False, "error": code, "say": "这张单已经不在挂着的单子里了，我在对话里看了一下。"}
            log.warning("voice watch cancel failed: %s", code)
            return {"ok": False, "error": code, "say": "撤单没有成功，稍后再说一遍撤单。"}
        watch = getattr(self.app.state, "order_watch", None)
        if watch is not None:
            # 结果未知 is read on until it settles (#932 F3), never cancelled again; anything else ends the watch.
            await watch.settle(str(target["clientOrderId"]), view)
        if status == "已撤销":
            return {"ok": True, "say": f"{target['label']} 已撤销。"}
        if status in self.WATCH_UNKNOWN_STATUSES:
            return {"ok": True, "unknown": True, "say": f"{target['label']} 撤单发出去了，结果还没回报，我不再重复撤。"}
        return {"ok": True, "say": f"{target['label']} {status}。"}

    async def _chase_order(self, state: CallState) -> dict[str, Any]:
        """追单: cancel the resting order, wait until the read-back says it is really gone (a cancel
        that reads UNKNOWN or a fill that raced in stops the chase — nothing is re-sent), then ask the
        agent for the same order at the other side of the book. The new card waits for 确认 like any
        other (the 免口头确认 setting applies as usual)."""
        target = self.order_watch_target(state.user_id)
        if target is None:
            return {"ok": False, "error": "nothing_watched", "say": "现在没有看着的挂单。"}
        try:
            status, view = await self._watched_order_call(target, "cancel")
        except HTTPException as error:
            log.warning("voice watch cancel (chase) failed: %s", error.detail)
            return {"ok": False, "error": str(error.detail), "say": "先撤旧单没有成功，先不追，稍后再说追单。"}
        deadline = time.monotonic() + self.CHASE_CANCEL_WAIT_SECONDS
        from .simulation_orders import PARTIAL_ENDED
        while status not in {"已撤销", *self.WATCH_UNKNOWN_STATUSES, "已成交", PARTIAL_ENDED, "已拒绝", "已放弃"} \
                and time.monotonic() < deadline:
            await asyncio.sleep(0.5)
            try:
                status, view = await self._watched_order_call(target, "query")
            except HTTPException:
                break
        if status == "已成交":
            return {"ok": False, "error": "filled_while_chasing", "say": f"{target['label']} 刚刚成交了，不用追。"}
        if status != "已撤销":
            watch = getattr(self.app.state, "order_watch", None)
            if watch is not None:
                await watch.settle(str(target["clientOrderId"]), view)
            reason = "撤单结果还没回报" if status in self.WATCH_UNKNOWN_STATUSES else f"单子现在是{status}"
            return {"ok": False, "error": "cancel_unconfirmed", "say": f"{target['label']} {reason}，先不追，确认了再说。"}
        watch = getattr(self.app.state, "order_watch", None)
        if watch is not None:
            await watch.settle(str(target["clientOrderId"]), view)
        outcome = await self._delegate(f"以对手价下单：{target['name'] or target['symbol']} {target['sidePhrase']} {target['volume']} 手", state)
        spoken = str(outcome.get("spoken") or outcome.get("say") or "")
        if outcome.get("ok") and spoken and not spoken.startswith("已撤销"):
            spoken = f"旧单已撤，{spoken}"
        return {**outcome, "spoken": spoken or outcome.get("say"), "say": spoken or outcome.get("say")}

    # -- 保本提醒's spoken answer (2026-10-06) ---------------------------------------------------

    def breakeven_target(self, state: CallState) -> dict[str, Any] | None:
        """The 保本提醒 a spoken 移 / 不用 answers: the one this call read out last, still open, and the
        voice confirm window not over — counted from the end of the read-out (main runtime only)."""
        heard = state.breakeven_heard
        if state.replay_id or not heard:
            return None
        if not state.still_playing():
            played_until = max(float(heard["at"]), (state.play_started_at or 0.0) + state.play_seconds)
            if time.monotonic() > played_until + float(state.preferences.voice.confirmWindowSeconds):
                return None
        try:
            notice = self.app.state.store.breakeven_notice(str(heard["id"]))
        except Exception:
            return None
        return notice if notice is not None and notice["userId"] == state.user_id and notice["status"] == "pending" else None

    async def _answer_breakeven(self, state: CallState, choice: str) -> dict[str, Any]:
        """移 / 不用 on the newest open 保本提醒, through the same route the card's buttons call."""
        target = self.breakeven_target(state)
        if target is None:
            return {"ok": False, "error": "nothing_to_answer", "say": "现在没有等你回答的保本提醒。"}
        code, result = await self._http("POST", f"/api/ai-execution/breakeven/{target['id']}/{choice}")
        if code >= 400:
            detail = result.get("detail")
            message = detail.get("message") if isinstance(detail, dict) else None
            return {"ok": False, "error": str(detail.get("code") if isinstance(detail, dict) else detail),
                    "say": f"没移：{message}。" if message else "这会儿没办成，请看对话里的保本提醒卡。"}
        status, words = str(result.get("status") or ""), str(result.get("result") or "")
        if status == "moved":
            return {"ok": True, "say": f"好，{words}。"}
        if status == "declined":
            return {"ok": True, "say": "好，不移了，这笔今天不再问。"}
        return {"ok": status != "failed", "say": f"{words or '没移'}。"}

    # -- proactive announcements -------------------------------------------

    def announce(self, kind: str, text: str, *, user_id: str = LOCAL_USER_ID,
                 replay_id: str | None = None, key: str | None = None) -> dict[str, Any]:
        """Queue an announcement for that user's active calls (also the acceptance hook for fake
        events). A reminder raised inside a replay carries that replay's id: only the call bound to
        the same replay plays it — a live call never speaks the replay's world (#521 R2). `key` names
        the object the line is about (#932): the 60 s repeat throttle is per kind AND object, so two
        strategies' signals, two orders' unfilled reminders, are both heard."""
        item: dict[str, str] = {"kind": kind, "text": text}
        if replay_id:
            item["replayId"] = replay_id
        if key:
            item["throttleKey"] = f"{kind}:{key}"
        self._pending_announcements.setdefault(user_id, []).append(item)
        calls = self.user_calls(user_id)
        result: dict[str, Any] = {"queued": True, "activeCalls": calls}
        if not calls:
            # 10-05: the model read "no call" as "I cannot speak" — a push-to-talk answer is read out by
            # the page itself, not through this queue.
            result["note"] = "没有通话时这条播报不会发出；按住说话的回答由软件自动读出，不需要调用播报。"
        return result

    def _throttled(self, state: CallState, kind: str) -> bool:
        now = time.monotonic()
        last = state.last_announce.get(kind)
        if last is not None and now - last < ANNOUNCE_THROTTLE_SECONDS:
            return True
        state.last_announce[kind] = now
        return False


def register_voice_realtime(application: FastAPI, ctx) -> None:
    service = VoiceRealtimeService(application)
    application.state.voice_realtime = service
    current_user = ctx.current_user

    @application.get("/api/voice/realtime/status")
    async def voice_status(request: Request) -> dict[str, Any]:
        return service.status(await current_user(request))

    @application.get("/api/voice/realtime/voices")
    async def voice_voices(request: Request) -> dict[str, Any]:
        """可选音色与当前账号的选择；`PUT /api/voice/realtime/voice` 更改，下一次通话生效。"""
        return service.voices(await current_user(request))

    @application.put("/api/voice/realtime/voice")
    async def voice_set_voice(request: Request, body: VoicePreferenceRequest) -> dict[str, Any]:
        """改本账号的语音音色（id 见 GET /api/voice/realtime/voices），下一次通话生效。"""
        user_id = await current_user(request)
        if body.voice not in service.voice_ids():
            raise HTTPException(404, "voice_unavailable")
        application.state.store.update_preferences(user_id, {"voice": {"voice": body.voice}})
        return service.voices(user_id)

    @application.post("/api/voice/realtime/announce")
    async def voice_announce(request: Request, body: dict) -> dict[str, Any]:
        # The voice-call skill gates this POST at the app-level write gate.
        user_id = await current_user(request)
        if service._key_source() is None:
            raise HTTPException(409, "voice_realtime_disabled")
        kind, text = str(body.get("kind") or "event"), str(body.get("text") or "").strip()
        if not text:
            raise HTTPException(422, "text_required")
        return service.announce(kind, text[:300], user_id=user_id)

    def protocol_token(websocket: WebSocket) -> str | None:
        """The session token a browser client offers as a WebSocket subprotocol
        (`Sec-WebSocket-Protocol: ai-trader-token, bearer.<token>`): browsers cannot set Authorization,
        and the token must stay out of the query string, which the access log prints."""
        offered = [item.strip() for item in websocket.headers.get("sec-websocket-protocol", "").split(",")]
        if AUTH_SUBPROTOCOL not in offered:
            return None
        return next((item[len(AUTH_TOKEN_PREFIX):] for item in offered if item.startswith(AUTH_TOKEN_PREFIX)), None)

    def socket_identity(websocket: WebSocket) -> CallIdentity | None:
        """Local mode: the token rides the subprotocol, the query or its cookie. Cloud: subprotocol
        (desktop), session cookie (web) or Authorization (packaged CLI) — never the query string."""
        auth = application.state.auth
        offered_token = protocol_token(websocket)
        if isinstance(auth, LocalAuth):
            token = offered_token or websocket.query_params.get("token") or websocket.cookies.get(COOKIE_NAME)
            return CallIdentity(LOCAL_USER_ID, {"authorization": "Bearer " + auth.token}) if auth.valid_token(token) else None
        authorization = websocket.headers.get("authorization", "")
        bearer = authorization.removeprefix("Bearer ") if authorization.startswith("Bearer ") else offered_token
        token = bearer or websocket.cookies.get(OWNER_COOKIE_NAME)
        if not token:
            return None
        try:
            identity = auth.validate(token)
        except HTTPException:
            return None
        headers = {"authorization": "Bearer " + bearer} if bearer else {"cookie": websocket.headers.get("cookie", "")}
        return CallIdentity(identity.scoped_user_id, headers)

    def cloud_refusal(user_id: str, *, counts_as_call: bool) -> tuple[int, str | None] | None:
        """Cloud admission shared by the call and push-to-talk: the voice-call skill (4431), one call
        per account (4429, calls only), the tier's daily voice minutes (4430) and daily tokens (4429)."""
        if service.local_mode:
            return None
        voice_skill = skill_by_id("voice-call")
        if voice_skill is not None and not skill_allowed(application.state.store, user_id, voice_skill):
            # No voice skill for this user at all (tier-withheld or globally
            # disabled) — same business code as every other skill refusal.
            return 4431, "feature_not_in_plan"
        if counts_as_call and service.user_calls(user_id) >= CLOUD_CALLS_PER_USER:
            return 4429, None
        if service.daily_voice_exhausted(user_id):
            # M2: the tier's voice minutes for today are spent.
            return 4430, None
        daily_limit = daily_token_limit(
            plan_for_user(application.state.store, user_id),
            env_limit=int(os.getenv("AI_TRADER_DAILY_TOKEN_LIMIT", "200000000")))
        if daily_limit is not None and application.state.store.token_usage(user_id, datetime.now(UTC).date().isoformat()) >= daily_limit:
            return 4429, None
        return None

    @application.websocket("/api/voice/transcribe")
    async def voice_transcribe_socket(websocket: WebSocket):
        """Push-to-talk (按住说话): the held audio is transcribed only — no reply, no tools — and the
        final text goes back to the composer, which sends it as an ordinary typed message. Same
        identity, origin, skill and daily-minute rules as the call; it does not take the per-account
        call slot, but is refused (4432 `call_active`) while this account has a call running. A socket
        forwarded from a replay namespace is accepted as is: transcription touches no account."""
        identity = socket_identity(websocket) if websocket.headers.get("origin") in ctx.settings.origins else None
        if identity is None:
            await websocket.close(code=4401)
            return
        key = service._read_key()
        if key is None:
            await websocket.close(code=4409)
            return
        refusal = cloud_refusal(identity.user_id, counts_as_call=False)
        if refusal is None and service.user_calls(identity.user_id):
            refusal = (4432, "call_active")
        if refusal is not None:
            await websocket.close(code=refusal[0], reason=refusal[1])
            return
        await websocket.accept(subprotocol=AUTH_SUBPROTOCOL if protocol_token(websocket) is not None else None)
        _identity.set(identity)
        from .voice_transcribe import run_transcribe
        await run_transcribe(service, websocket, key, identity)

    @application.websocket("/api/voice/speak")
    async def voice_speak_socket(websocket: WebSocket):
        """The spoken reply of a push-to-talk message (09-28): the client sends the agent's spoken
        summary and gets the call's voice back. Same identity, origin and daily-minute rules as
        按住说话; refused (4432 `call_active`) while this account has a call running (the call speaks)."""
        identity = socket_identity(websocket) if websocket.headers.get("origin") in ctx.settings.origins else None
        if identity is None:
            await websocket.close(code=4401)
            return
        key = service._read_key()
        if key is None:
            await websocket.close(code=4409)
            return
        refusal = cloud_refusal(identity.user_id, counts_as_call=False)
        if refusal is None and service.user_calls(identity.user_id):
            refusal = (4432, "call_active")
        if refusal is not None:
            await websocket.close(code=refusal[0], reason=refusal[1])
            return
        await websocket.accept(subprotocol=AUTH_SUBPROTOCOL if protocol_token(websocket) is not None else None)
        _identity.set(identity)
        from .voice_speak import run_speak
        await run_speak(service, websocket, key, identity)

    @application.websocket("/api/voice/realtime")
    async def voice_socket(websocket: WebSocket):
        identity = socket_identity(websocket) if websocket.headers.get("origin") in ctx.settings.origins else None
        if identity is None:
            await websocket.close(code=4401)
            return
        key = service._read_key()
        if key is None:
            await websocket.close(code=4409)
            return
        # A socket forwarded from a replay namespace ("/api/replays/<id>/runtime/…") binds the whole
        # call to that replay: quotes, positions, cards and window commands act on the replay's child
        # runtime, never on the live account. A closed, foreign or vanished replay refuses the call.
        replay_id = websocket.scope.get("ai_trader.replay_id")
        if replay_id:
            router = getattr(application.state, "replay_router", None)
            try:
                if router is None:
                    raise HTTPException(404, "replay_not_found")
                router.owned(str(replay_id), identity.user_id)
            except HTTPException:
                await websocket.close(code=4404)
                return
        refusal = cloud_refusal(identity.user_id, counts_as_call=True)
        if refusal is not None:
            await websocket.close(code=refusal[0], reason=refusal[1])
            return
        # A client that offered the auth subprotocol needs it echoed back or the browser drops the socket.
        await websocket.accept(subprotocol=AUTH_SUBPROTOCOL if protocol_token(websocket) is not None else None)
        _identity.set(identity)
        if replay_id:
            _replay_binding.set(str(replay_id))
        await _run_call(service, websocket, key, identity, replay_id=str(replay_id) if replay_id else None)


async def _run_call(service: VoiceRealtimeService, client: WebSocket, key: str, identity: CallIdentity,
                    replay_id: str | None = None) -> None:
    import websockets

    call_id = uuid4().hex[:8]
    # M1a: a live call is being online; keep the user's on-demand account from
    # being idle-reclaimed mid-conversation.
    service.app.state.presence.touch(identity.user_id)
    state = CallState(user_id=identity.user_id, preferences=service.app.state.store.user_preferences(identity.user_id),
                      replay_id=replay_id)
    # 前台直接查询: decided once per call; a replay-bound call keeps the old front desk (its news and
    # trend reads would bring today's information into a historical replay).
    state.front_on = front_query_enabled(identity.user_id) and not replay_id
    # 下单类直接出卡 rides on the front desk's brief and tools: on only where the front desk is.
    state.action_on = state.front_on and front_action_enabled(identity.user_id)
    service.calls[call_id] = state
    voice = service.voice_for(identity.user_id)
    speed = SPEECH_SPEEDS[state.preferences.voice.speed]
    log.info("voice call %s started user=%s voice=%s speed=%s", call_id, identity.user_id, voice, state.preferences.voice.speed)
    started = time.monotonic()

    async def to_client(payload: dict[str, Any]) -> bool:
        try:
            await client.send_text(json.dumps(payload, ensure_ascii=False))
        except Exception:
            return False
        return True

    def mark(kind: str, **fields: Any) -> None:
        """One line per ASR / response milestone, so a real call can be read afterwards (an ASR final
        without a response, a response without audio, a tool that never came back)."""
        at = round(time.monotonic() - started, 2)
        state.timeline.append({"at": at, "event": kind, **fields})
        log.info("voice call %s %s at=%.2fs%s", call_id, kind, at, "".join(f" {k}={v}" for k, v in fields.items()))

    released: list[float] = []

    # #523 R4 (observation only): per-call audio frame/byte accounting — frames delivered over the
    # upstream WebSocket (counted before any filter, bytes = the frames' real decoded size), frames
    # the Runtime dropped (a cancelled or muted reply) and frames whose to_client WebSocket send
    # completed (a completed send, not proof the client received or played them). Counts only,
    # never the audio itself; summarised once in the closing timeline mark.
    audio_diag = {"recvFrames": 0, "recvBytes": 0, "dropFrames": 0, "dropBytes": 0, "fwdFrames": 0, "fwdBytes": 0}
    # The uplink side of the same line: what the caller's microphone actually delivered (MicStats).
    mic = MicStats(started=started)

    def release_slot() -> float:
        """The call's slot (one per account in the cloud) and its voice seconds end when the person leaves
        — the client gone or the spoken hang-up — not when Doubao acknowledges session.close: a redial
        inside that wait (up to SESSION_CLOSE_WAIT_SECONDS) was refused with 4429 → HTTP 403, "语音通道
        连接失败" (10-08). The old session's tail runs on in the background with no microphone behind it."""
        if not released:
            service.calls.pop(call_id, None)
            released.append(round(time.monotonic() - started, 1))
            if not service.local_mode:
                # M2: the tier's daily voice minutes. A call crossing midnight
                # books its whole length to the day it ended — the cap stays simple.
                service.app.state.store.add_voice_seconds(
                    identity.user_id, datetime.now(UTC).date().isoformat(), int(released[0]))
        return released[0]

    prefetched: list[Any] = []
    try:
        # The client's "hello" (its window and chat session) is sent right after the socket opens: read it
        # before session.create so the conversation's recent turns can seed Doubao's context.
        try:
            first = await asyncio.wait_for(client.receive(), timeout=0.5)
            if first.get("type") == "websocket.disconnect":
                raise WebSocketDisconnect()
            hello = json.loads(first["text"]) if first.get("text") else None
            if isinstance(hello, dict) and hello.get("type") == "hello":
                state.window_id = str(hello.get("windowId") or "") or None
                state.chat_session_id = service._own_session(state, str(hello.get("sessionId") or "")) or state.chat_session_id
                state.review_intent = hello.get("intent") == "review"
            else:
                prefetched.append(first)
        except (TimeoutError, ValueError):
            pass
        asr_context, asr_extra = await asr_settings(service)
        dialog_extra = {"extra": {"enable_loudness_norm": True}} if LOUDNESS_NORM else {}
        dialog_context = service.dialog_context(state)
        mark("sessionContext", hotwords=len(asr_context["hotwords"]),
             products=sum(1 for item in asr_context["hotwords"] if item["word"] not in TRADING_HOTWORDS),
             rounds=len(dialog_context) // 2, twopass=ASR_TWOPASS, format=ASR_CONTEXT_FORMAT,
             endSmoothMs=END_SMOOTH_MS, loudnessNorm=LOUDNESS_NORM)
        # From an overseas host the pong to a keepalive ping can take a while; the default 20 s timeout
        # killed a cloud call mid-order ("keepalive ping timeout") while the dialogue itself was fine.
        def dial() -> Any:
            return websockets.connect(DOUBAO_URL, additional_headers={"X-Api-Key": key}, max_size=8 * 1024 * 1024,
                                      ping_interval=20, ping_timeout=90)

        async def close_quietly(connection: Any) -> None:
            """A session left behind (stalled, or at call end): closed without waiting out a dead peer."""
            try:
                await asyncio.wait_for(connection.__aexit__(None, None, None), timeout=3.0)
            except BaseException:
                pass

        def spawn(coro: Any) -> asyncio.Task:
            """A task of this call (a round, a tool handler, a reader, a watchdog): cancelled when the call ends, and a
            failure is logged instead of vanishing (#523 / #754: work the call started outlived it, unowned)."""
            task = asyncio.create_task(coro)
            state.tasks.add(task)

            def finished(done: asyncio.Task) -> None:
                state.tasks.discard(done)
                if not done.cancelled() and done.exception() is not None:
                    log.warning("voice call %s task %s failed: %s: %s", call_id, getattr(done.get_coro(), "__name__", "?"),
                                type(done.exception()).__name__, done.exception())
            task.add_done_callback(finished)
            return task

        async def business(coro: Any) -> Any:
            """A business call a voice task waits on (a tool, a confirm, the trading AI's turn): shielded, so the call
            ending (or a barge-in) cancels the wait, never the operation half-way."""
            return await asyncio.shield(asyncio.ensure_future(coro))

        async with contextlib.AsyncExitStack() as upstreams:
            # The Doubao session in use: `ws` changes when the upstream watchdog swaps in a new session; `reader`
            # is that session's reader task, `ready` is set by its session.created, `lost` once it closed or its
            # reader ended, `calls` the tool call ids it made (a replayed id is never run twice).
            first_connection = dial()
            link: dict[str, Any] = {"ws": await first_connection.__aenter__(), "connection": first_connection,
                                    "reader": None, "ready": asyncio.Event(), "gen": 0, "opened": False, "calls": set(),
                                    "lost": False}
            connections = [first_connection]

            async def close_all() -> None:
                await asyncio.gather(*(close_quietly(connection) for connection in connections))
            upstreams.push_async_callback(close_all)

            def stale() -> bool:
                """The running task was derived from a session the watchdog has since replaced."""
                origin = _upstream_gen.get()
                return origin is not None and origin != link["gen"]

            def upstream_lost(why: str) -> None:
                """The session in use closed, its reader ended or a send to it failed: the watchdog replaces it at once
                and says its unheard lines again (#523: a session.closed only set `stop` and left the call alive with
                no upstream). Never while the call is ending or a replacement is being made."""
                if state.closed or state.reconnecting or link["lost"]:
                    return
                link["lost"] = True
                mark("upstreamLost", why=why, gen=link["gen"])
                if not state.unheard:
                    state.upstream_expect = why
                state.upstream_expect_by = time.monotonic()

            async def to_upstream(payload: dict[str, Any]) -> bool:
                """Send to the session in use. A send that fails loses the session (never the caller's task: a mute
                pressed mid-swap must not end the call); an in-turn effect of a replaced session's handler is dropped."""
                sent_turn = state.turn_serial
                kind = payload.get("type")
                if stale() and (kind in {"response.cancel", "speech_text_buffer.replacement.append",
                                         "speech_text_buffer.replacement.commit"}
                                or (kind == "conversation.item.create"
                                    and any(item.get("call_id") for item in payload.get("items") or []))):
                    # A replaced session's handler: its turn, tool results and cancels are not the new session's.
                    mark("staleUpstreamDropped", msg=kind)
                    return False
                if kind == "response.cancel":
                    state.cancel_sent_at = time.monotonic()
                try:
                    await link["ws"].send(json.dumps(payload, ensure_ascii=False))
                except Exception as error:
                    mark("upstreamSendFailed", msg=kind, error=type(error).__name__)
                    upstream_lost("send")
                    return False
                if kind in UPSTREAM_SENT_TRACE_TYPES:
                    # #523 R4: the send itself completed (the pre-send speakCommit line cannot say
                    # whether the commit ever left the Runtime).
                    mark("upstreamSent", msg=kind, turn=sent_turn)
                return True

            def expect_upstream(what: str, seconds: float) -> None:
                """上游看门狗: some upstream event must arrive within `seconds` (any event clears it)."""
                state.upstream_expect, state.upstream_expect_by = what, time.monotonic() + seconds

            def watch_lines() -> None:
                """上游看门狗 for the Runtime's lines: the oldest unheard line must start within UPSTREAM_REPLY_WAIT_SECONDS
                of its commit; with none left nothing is awaited (a session expectation stays as it is)."""
                if state.unheard:
                    state.upstream_expect = "commit"
                    state.upstream_expect_by = state.unheard[0]["at"] + UPSTREAM_REPLY_WAIT_SECONDS
                elif state.upstream_expect == "commit":
                    state.upstream_expect, state.upstream_expect_by = "", 0.0

            def lines_give_way() -> None:
                """The user is saying something new: the lines still waiting to start give way to it."""
                if state.unheard:
                    state.unheard.clear()
                    watch_lines()

            # 私人记忆 (3a): the front desk carries the 画像 profile only, appended at send time so
            # the INSTRUCTIONS contract (≤450 chars) stays untouched. Any failure to read the store
            # falls back to the plain instructions — a call must never fail to start over this.
            try:
                profile = user_memory.voice_profile(service.app.state.store, state.user_id)
            except Exception:
                profile = ""

            def session_create(context: list[dict[str, Any]]) -> dict[str, Any]:
                return {"type": "session.create", "event_id": f"evt_{call_id}" + (f"_{link['gen']}" if link["gen"] else ""), "session": {
                    "model": MODEL, "instructions": (FRONT_ACTION_INSTRUCTIONS if state.action_on else FRONT_INSTRUCTIONS if state.front_on
                                                     else INSTRUCTIONS) + (f"\n{profile}" if profile else ""),
                    "audio": {"input": {"format": {"type": "pcm", "rate": 16000}},
                              "output": {"format": {"type": "pcm", "rate": 24000}, "speed": speed, "loudness": 0, "voice": voice}},
                    "tools": (FRONT_ACTION_TOOLS if state.action_on else FRONT_TOOLS if state.front_on else TOOLS) if MODEL_TOOLS else []},
                    "extension": {"asr": {"extra": asr_extra} if asr_extra else {}, "tts": {},
                                  "dialog": {**({"dialog_context": context} if context else {}), **dialog_extra}}}

            expect_upstream("session", SESSION_CREATED_WAIT_SECONDS)
            await to_upstream(session_create(dialog_context))

            async def append_context(user_text: str, assistant_text: str) -> None:
                """After a Runtime-spoken turn (delegate / intent / confirm): Doubao heard the question but
                never saw the answer; both go into its context as a plain pair (no timestamps)."""
                if not user_text.strip() or not assistant_text.strip():
                    return
                await to_upstream({"type": "conversation.item.create", "items": [
                    {"id": f"ctx_{uuid4().hex[:10]}", "type": "message", "role": "user", "content": [{"type": "input_text", "text": user_text.strip()[:DIALOG_CONTEXT_CHARS]}]},
                    {"id": f"ctx_{uuid4().hex[:10]}", "type": "message", "role": "assistant", "content": [{"type": "input_text", "text": _context_text(assistant_text)}]}]})
                mark("contextAppended", chars=len(user_text) + len(assistant_text))
            await to_client({"type": "status", "state": "connecting"})
            stop = asyncio.Event()  # the session.close sent at call end is acknowledged (only that: a lost session is replaced)
            hangup = asyncio.Event()  # set by a spoken "关闭语音": the call ends from the Runtime side

            async def inject(text: str, *, on_commit: Callable[[], None] | None = None) -> None:
                """Inside a turn (a tool result): REPLACES the model's spoken reply for that turn. `on_commit` runs once
                the commit has been sent — never for a line that is not (a replaced session, a failed or dropped send,
                #932 B4: armed before the send, a card stayed confirmable although its question never left)."""
                if stale():
                    mark("staleUpstreamDropped", msg="speech_text_buffer.replacement")
                    return
                if not await to_upstream({"type": "speech_text_buffer.replacement.append", "text": text}):
                    return
                if await to_upstream({"type": "speech_text_buffer.replacement.commit", "text": ""}):
                    if on_commit is not None:
                        on_commit()
                    return
                # The append went out but its commit did not (a failed send, a session swapped meanwhile): the line was
                # never said. It joins the unheard lines, which the upstream watchdog says again on the live session
                # (`recover_upstream`) — its card stays prepared, asked again by the next 确认 / 取消 (PR #997 review).
                if not (state.closed or state.gave_up):
                    state.unheard.append({"text": text, "at": time.monotonic(), "retried": False})
                    watch_lines()

            async def speak(text: str, *, retry: bool = False, on_commit: Callable[[], None] | None = None) -> None:
                """Outside a turn (a timeout, an announcement): the "打招呼" event synthesises the text as
                its own response. A replacement sent between turns is silently dropped (measured 09-17).

                The one entry for every Runtime-spoken line (10-15): Doubao's reply the Runtime cancelled is
                acknowledged first (at most SPEAK_CANCEL_WAIT_SECONDS), a session being swapped in by the upstream
                watchdog is waited for until it is ready or given up (#904 review: a capped wait sent the line to a
                session not ready yet), and the commit is recorded as one more unheard line — one not starting within
                UPSTREAM_REPLY_WAIT_SECONDS (_answers_expectation) means a stalled session, and it is said again.

                `on_commit` runs once the commit has been sent — a card a line asks becomes confirmable there and
                nowhere sooner (#932 复核: armed before the cancel-ack wait, a second 确认 said meanwhile confirmed a
                card the user had not heard; #932 B4: armed before the send, a failed or dropped send left it
                confirmable). A line never committed (the call ended or gave up, the round was cancelled while waiting,
                the send failed) runs nothing: its card stays prepared and the next 确认 / 取消 asks it again."""
                in_reader = asyncio.current_task() is link["reader"]
                if not in_reader:
                    # Never inside the upstream reader: it is the one that reads the acknowledgement.
                    waited_from = time.monotonic()
                    while ((state.cancel_sent_at and time.monotonic() - state.cancel_sent_at < SPEAK_CANCEL_WAIT_SECONDS)
                           or state.reconnecting):
                        if state.gave_up or state.closed:
                            break
                        await asyncio.sleep(0.02)
                    if time.monotonic() - waited_from > 0.05:
                        mark("speakWait", seconds=round(time.monotonic() - waited_from, 2), cancelAcked=not state.cancel_sent_at)
                if state.gave_up or state.closed:
                    return
                state.committed_serial = state.turn_serial
                state.unheard.append({"text": text, "at": time.monotonic(), "retried": retry})
                watch_lines()
                if await to_upstream({"type": "speech_text_buffer.commit", "text": text}) and on_commit is not None:
                    on_commit()

            async def line_played(seconds: float) -> bool:
                """Whether the audio of the line just spoken was delivered (audio_done), waited `seconds` — and longer
                while a line is still unheard or the session is being swapped: the watchdog either sees it start or
                says it again on a new session, and that audio ends the wait (#904 review: a stalled goodbye or
                daily-limit line counted as played and the call ended before it was heard)."""
                while not state.gave_up:
                    try:
                        await asyncio.wait_for(state.audio_done.wait(), timeout=seconds)
                        return True
                    except TimeoutError:
                        pass
                    if not (state.reconnecting or state.unheard):
                        return False
                    while (state.reconnecting or state.unheard) and not state.gave_up and not state.audio_done.is_set():
                        await asyncio.sleep(0.05)
                return False

            def dropping(event: dict[str, Any]) -> bool:
                """Output of the reply being cancelled (all output, unless the late reply's id is known)."""
                if (event.get("type") in {"response.output_audio.delta", "response.output_audio.done"}
                        and not event.get("response_id") and state.current_output_id in state.cancelled_readouts):
                    # Audio frames carry no response id: they belong to the output that started last. A read-out the
                    # user cancelled keeps delivering them — they stop here too (#523: only id-carrying ones did).
                    return True
                if (state.action_on and event.get("type") in {"response.output_audio.delta", "response.output_audio.done"}
                        and not event.get("response_id") and state.current_output_id
                        and state.current_output_id in state.runtime_responses
                        and state.current_output_id not in state.cancelled_readouts):
                    # Audio frames carry no response id: they belong to the output that started last. While that
                    # is the Runtime's own read-out, a cancel of Doubao's reply (its late `action` call) must not
                    # drop it (10-13 production: a card line lost frame by frame).
                    return False
                if event.get("response_id") in state.cancelled_readouts:
                    return True  # a read-out the user cancelled: its remaining frames stop here (#523)
                if event.get("response_id") in state.runtime_responses:
                    return False
                return state.mute_cancel_pending and (
                    state.late_muted_response is None or event.get("response_id") in (None, state.late_muted_response))

            async def mute_model_reply(reason: str, *, runtime_speech: bool = False, response_id: str | None = None) -> bool:
                """Cancel Doubao's own reply for a turn the Runtime speaks for, once, before any of its audio
                reaches the client; the Runtime's committed speech for the same turn is never cancelled."""
                if stale():
                    return False  # a replaced session's turn: there is nothing of it to cancel on the new one
                if state.late_reply_until > time.monotonic() and not runtime_speech:
                    # Doubao answering its own late final of a turn the Runtime already answered early:
                    # always cancelled, even after the Runtime spoke (its speech is tts_type chat_tts_text).
                    # The window stays open until it ends or the next sentence: response.done carries no
                    # id, so the Runtime's read-out ending can close the mute before the late reply's
                    # words arrive — they are muted again here.
                    if state.mute_cancel_pending and state.late_muted_response in (None, response_id):
                        return False  # already cancelling it: `dropping` drops the output
                    state.mute_cancel_pending = True
                    state.late_muted_response = response_id
                    mark("lateReplyMuted", reason=reason)
                    await to_upstream({"type": "response.cancel"})
                    return True
                if state.mute_serial != state.turn_serial or state.committed_serial == state.turn_serial or state.mute_cancel_pending:
                    return False
                if state.routed and state.routed["serial"] == state.turn_serial and state.routed["claimed"]:
                    return False
                state.mute_cancel_pending = True
                mark("modelReplyMuted", reason=reason)
                await to_upstream({"type": "response.cancel"})
                return True

            async def quick_fallback(reason: str) -> bool:
                """A quick turn on which Doubao starts talking before calling a quick tool: cancel its reply
                and hand the utterance to the agent (the turn is muted from here like a delegate turn)."""
                quick = state.quick
                if stale() or not quick or quick["serial"] != state.turn_serial or quick["called"] or quick["fallback"]:
                    return False
                quick["fallback"] = True
                state.mute_serial = state.turn_serial
                state.mute_cancel_pending = True
                mark("quickQueryFallback", reason=reason, seconds=round(time.monotonic() - quick["t0"], 2))
                await to_upstream({"type": "response.cancel"})
                spawn(run_routed("delegate", quick["text"], quick["serial"]))
                return True

            async def front_fallback(reason: str) -> bool:
                """A query the words named, on which Doubao starts talking before calling `query` (numbers from
                memory): its reply is cancelled before any of it plays and the Runtime reads the result itself."""
                front = state.front
                if (stale() or not front or front["serial"] != state.turn_serial or not front.get("read")
                        or front["called"] or front["fallback"]):
                    return False
                front["fallback"] = True
                state.mute_serial = state.turn_serial
                state.mute_cancel_pending = True
                routed = {"serial": front["serial"], "name": "front", "task": None, "claimed": False, "response_done": asyncio.Event()}
                state.routed = routed
                mark("frontQueryFallback", reason=reason, seconds=round(time.monotonic() - front["t0"], 2))
                await to_upstream({"type": "response.cancel"})
                state.routed_task = spawn(run_front_takeover(front, routed))
                return True

            async def run_front_takeover(front: dict[str, Any], routed: dict[str, Any] | None = None) -> None:
                """The Runtime answers a front turn itself, never through the trading AI: a query the words
                named that Doubao answered from memory or not at all, or that went early (read from the `query`
                result), or a sentence Doubao left open (stall final) or ended silently — those other kinds
                get one short line (TAKEOVER_LINES). `routed`: wait for Doubao's cancelled reply to be
                cancelled first (`response.canceled`), so the user hears one voice."""
                serial, t0 = front["serial"], time.monotonic()
                if front["kind"] == "query":
                    prefetch = front.get("prefetch")
                    outcome = await prefetch if prefetch is not None else await service.run_tool("query", front_arguments(front), state)
                    line = spoken_line(outcome)
                    if not await service.binding_holds(state, front.get("binding")):
                        mark("frontRetired", front=front["kind"])  # #523 R1c: the conversation or account moved
                        return
                    await to_client({"type": "tool_result", "name": "query", "arguments": front_arguments(front), "ok": "error" not in outcome,
                                     "summary": outcome, "elapsedSeconds": round(time.monotonic() - t0, 3)})
                else:
                    line = takeover_line(front["kind"], front["text"])
                if routed is not None:
                    try:
                        await asyncio.wait_for(routed["response_done"].wait(), timeout=ROUTED_RESPONSE_WAIT_SECONDS)
                    except TimeoutError:
                        mark("routedResponseWait", tool="front")
                if serial != state.turn_serial:
                    mark("toolResultSuperseded", tool="front", routedBy="front")
                    return
                if not await service.binding_holds(state, front.get("binding")):
                    mark("frontRetired", front=front["kind"])
                    return
                front["recorded"] = True
                state.tool_log.append({"name": "front", "arguments": {"kind": front["kind"], "text": front["text"]}, "ok": True,
                                       "elapsedSeconds": round(time.monotonic() - front["t0"], 3), "routedBy": "runtime"})
                mark("frontTakeover", front=front["kind"], chars=len(line), seconds=round(time.monotonic() - t0, 2))
                if not line:
                    await to_client({"type": "status", "state": "listening"})
                    return
                state.reading_serial = serial
                await to_client({"type": "text", "delta": line})
                mark("speakCommit", tool="front", chars=len(line))
                await speak(line)
                state.reading_serial = None
                if not await service.binding_holds(state, front.get("binding")):
                    mark("frontRetired", front=front["kind"])
                    return
                try:
                    await service.record_quick_turn(state, front["text"], line, session_id=front["binding"].session_id)
                except Exception as error:  # the transcript is a record, never the call's lifeline
                    log.warning("voice call %s front turn not recorded: %s", call_id, error)
                await append_context(front["text"], line)

            async def action_fallback(reason: str) -> bool:
                """An order turn on which Doubao talks (or reaches for another tool) before calling `action`:
                its reply is cancelled before any of it plays and the Runtime reads the card itself."""
                act = state.action
                if (stale() or not act or act["serial"] != state.turn_serial or act["direct"] or act["called"]
                        or act["fallback"] or act["read"]):
                    return False
                act["fallback"] = True
                state.mute_serial = state.turn_serial
                state.mute_cancel_pending = True
                state.routed = act["routed"] = {"serial": act["serial"], "name": "action", "task": None, "claimed": False,
                                                "response_done": asyncio.Event()}
                mark("actionFallback", reason=reason, seconds=round(time.monotonic() - act["t0"], 2))
                await to_upstream({"type": "response.cancel"})
                act["handover"].set()
                return True

            async def drop_unread(act: dict[str, Any]) -> None:
                """The round was dropped before its line was read (续句, 打断, a new sentence, 算了 while the card
                was being made): the card it made is rejected unseen."""
                if act.get("dropped") or act.get("armed"):
                    return
                act["dropped"] = True
                try:
                    outcome = await asyncio.shield(act["prefetch"])
                except Exception:
                    return
                if act.get("armed"):
                    return
                mark("actionDropped", order=act["action"].act, card=outcome.get("card") is not None)
                await service.drop_action_card(state, outcome)

            async def read_action(act: dict[str, Any], outcome: dict[str, Any]) -> None:
                """The Runtime reads the action's line out of turn (early / stall final, or Doubao spoke first
                or stayed silent): the card becomes the waiting card, then the line is spoken."""
                serial = act["serial"]
                routed = act.get("routed")
                if routed is not None:
                    try:
                        await asyncio.wait_for(routed["response_done"].wait(), timeout=ROUTED_RESPONSE_WAIT_SECONDS)
                    except TimeoutError:
                        mark("routedResponseWait", tool="action")
                # Doubao's own reply to this final (often its `action` call, 0.1 s after the final) is being
                # cancelled: while that cancel is pending every output without its own id is dropped, and
                # the Runtime's line would be dropped with it (10-13 production: the card line committed
                # 0.2 s after the cancel was never heard). Speak once Doubao's cancelled turn is over.
                waited_from = time.monotonic()
                while state.mute_cancel_pending and time.monotonic() - waited_from < ACTION_CANCEL_WAIT_SECONDS:
                    await asyncio.sleep(0.05)
                if time.monotonic() - waited_from > 0.05:
                    mark("actionCancelWait", seconds=round(time.monotonic() - waited_from, 2),
                         pending=state.mute_cancel_pending)
                if serial != state.turn_serial or act.get("dropped"):
                    mark("toolResultSuperseded", tool="action", routedBy="runtime")
                    return
                act["read"] = True
                line = await business(service.adopt_action(state, act["text"], outcome, settle=act["settle"]))
                if line is None:
                    mark("actionRetired", order=act["action"].act)  # moved conversation/account, or a 续句 came
                    return
                state.tool_log.append({"name": "action", "arguments": {"kind": act["action"].act, "text": act["text"]},
                                       "ok": bool(outcome.get("ok")), "routedBy": "runtime",
                                       "elapsedSeconds": round(time.monotonic() - act["t0"], 3)})
                await to_client({"type": "tool_result", "name": "action", "arguments": {"kind": act["action"].act},
                                 "ok": bool(outcome.get("ok")), "summary": {"spoken": line},
                                 "elapsedSeconds": round(time.monotonic() - act["t0"], 3)})
                mark("actionRead", order=act["action"].act, card=outcome.get("card") is not None, inTurn=False,
                     seconds=round(time.monotonic() - act["t0"], 2))
                if serial != state.turn_serial:
                    if "pending" in outcome:  # never armed: the user moved on before hearing it
                        await service.drop_action_card(state, outcome)
                    return
                if not await service.binding_holds(state, outcome.get("binding")):
                    await service.drop_action_card(state, outcome)  # moved while the turn was published: never armed
                    mark("actionRetired", order=act["action"].act)
                    return
                state.reading_serial = serial
                service.arm_action_card(state, outcome)
                act["armed"] = True
                await to_client({"type": "text", "delta": line})
                mark("speakCommit", tool="action", chars=len(line))
                await speak(line)
                state.reading_serial = None
                await append_context(act["text"], line)

            async def action_outcome(act: dict[str, Any]) -> dict[str, Any]:
                """The card the action made (shielded: a cancelled waiter never stops the card tool half-way);
                a failure is one spoken line, never a dead turn."""
                try:
                    return await asyncio.shield(act["prefetch"])
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    log.warning("voice call %s front action failed: %s: %s", call_id, type(error).__name__, error)
                    return {"line": "后台出了点问题，请稍后再说一遍。", "card": None, "binding": act.get("binding")}

            async def run_action(act: dict[str, Any]) -> None:
                """下单类直接出卡: the card is made from the words as soon as the sentence ends. A sentence routed
                early (or left open by Doubao) is read by the Runtime at once; otherwise Doubao gets its tool turn
                (`action`, read in turn with the Runtime's line) and its words or silence hand the read to the
                Runtime. What needs the trading AI after all (two months held, two working orders) goes there."""
                try:
                    outcome = await action_outcome(act)
                    mark("actionReady", order=act["action"].act, card=outcome.get("card") is not None,
                         delegate=bool(outcome.get("delegate")), seconds=round(time.monotonic() - act["t0"], 2))
                    if outcome.get("delegate"):
                        act["read"] = True  # nothing of its own to read: the trading AI answers this turn
                        if act["serial"] != state.turn_serial or not await service.binding_holds(state, outcome.get("binding")):
                            mark("actionRetired", order=act["action"].act)  # moved on: the stale order goes nowhere
                            return
                        state.mute_serial = act["serial"]
                        await run_routed("delegate", act["text"], act["serial"])
                        return
                    if outcome.get("retired"):
                        act["read"] = True  # a target held back for the continuation window, and the user moved on
                        mark("actionRetired", order=act["action"].act)
                        return
                    if not act["direct"]:
                        try:
                            await asyncio.wait_for(act["handover"].wait(), timeout=ACTION_MODEL_WAIT_SECONDS)
                        except TimeoutError:
                            await action_fallback("timeout")
                        if act["read"]:
                            return  # Doubao's tool turn read it
                    await read_action(act, outcome)
                except asyncio.CancelledError:
                    asyncio.ensure_future(drop_unread(act))
                    raise

            async def cancel_open_round() -> None:
                """续句: the user went on with the same sentence, so the round it started is not wanted —
                stop its routed task (the agent turn and its read-out) and any response still generating,
                and wait for the task to unwind before the merged sentence is routed."""
                task = state.routed_task
                state.routed_task = None
                if task is not None and not task.done():
                    task.cancel()
                    try:
                        await task
                    except (asyncio.CancelledError, Exception):
                        pass
                if state.speaking:
                    state.cancelled_readouts |= state.runtime_responses
                    await to_upstream({"type": "response.cancel"})
                    state.speaking = False
                state.reading_serial = None

            async def handle_tool_calls(items: list[dict[str, Any]]) -> None:
                names = [item.get("name") for item in items]
                serial = state.turn_serial
                if stale():
                    mark("staleToolHandler", tool=",".join(str(n) for n in names))
                    return  # the session that called it was replaced before the handler ran
                if state.hanging_up:
                    # "关闭语音" is not a confirm/cancel: answer the model with nothing to do and stay silent.
                    mark("toolDroppedOnHangup", tool=",".join(str(n) for n in names))
                    await to_upstream({"type": "conversation.item.create", "items": [
                        {"call_id": item.get("call_id"), "role": "tool", "content": [{"type": "input_text", "text": json.dumps({"ok": True, "say": ""}, ensure_ascii=False)}]}
                        for item in items]})
                    return
                if state.late_reply_until > time.monotonic():
                    # Doubao's reply to its late final of an early-answered turn (a confirm the Runtime
                    # already ran, a quote for a delegated question): nothing to do, nothing to say.
                    mark("lateToolDropped", tool=",".join(str(n) for n in names))
                    await to_upstream({"type": "conversation.item.create", "items": [
                        {"call_id": item.get("call_id"), "role": "tool", "content": [{"type": "input_text", "text": json.dumps({"ok": True, "say": ""}, ensure_ascii=False)}]}
                        for item in items]})
                    await mute_model_reply("tool")
                    return
                quick = state.quick if state.quick and state.quick["serial"] == serial else None
                front = state.front if state.front and state.front["serial"] == serial and not state.front["fallback"] else None
                act = (state.action if state.action and state.action["serial"] == serial and not state.action["direct"]
                       and not state.action["fallback"] and not state.action["read"] else None)
                if act is not None and "action" not in names:
                    # An order turn on which Doubao reached for another tool (a quote, a cancel): answered with
                    # nothing to do, and the Runtime reads the card itself.
                    mark("modelToolDropped", tool=",".join(str(n) for n in names))
                    await to_upstream({"type": "conversation.item.create", "items": [
                        {"call_id": item.get("call_id"), "role": "tool", "content": [{"type": "input_text", "text": json.dumps({"ok": True, "note": "系统已在处理这句，不要回答"}, ensure_ascii=False)}]}
                        for item in items]})
                    await action_fallback("tool")
                    return
                if act is not None:
                    act["called"] = True
                    asked = next((item.get("arguments") for item in items if item.get("name") == "action"), "")
                    mark("actionToolCall", seconds=round(time.monotonic() - act["t0"], 2), asked=str(asked)[:80])
                    results = []
                    spoken_line_ = ""
                    act_outcome: dict[str, Any] | None = None
                    for item in items:
                        if item.get("name") != "action" or spoken_line_:
                            results.append({"call_id": item.get("call_id"), "role": "tool", "content": [
                                {"type": "input_text", "text": json.dumps({"ok": True, "note": "系统已在处理这句，不要回答"}, ensure_ascii=False)}]})
                            continue
                        act_outcome = await action_outcome(act)
                        if act_outcome.get("delegate") or act_outcome.get("retired") or serial != state.turn_serial or act.get("dropped"):
                            # The trading AI answers this one (run_action hands it over), or the user moved on.
                            reply = {"ok": True, "note": "系统已在处理这句，不要回答"}
                        else:
                            act["read"] = True
                            adopted = await business(service.adopt_action(state, act["text"], act_outcome, settle=act["settle"]))
                            if adopted is None:
                                mark("actionRetired", order=act["action"].act)
                                results.append({"call_id": item.get("call_id"), "role": "tool", "content": [
                                    {"type": "input_text", "text": json.dumps({"ok": True, "note": "用户已经说了别的，不要回答"}, ensure_ascii=False)}]})
                                continue
                            spoken_line_ = adopted
                            reply = {"ok": bool(act_outcome.get("ok")), "spoken": spoken_line_}
                            state.tool_log.append({"name": "action", "arguments": {"kind": act["action"].act, "text": act["text"]},
                                                   "ok": reply["ok"], "routedBy": "front",
                                                   "elapsedSeconds": round(time.monotonic() - act["t0"], 3)})
                            mark("actionRead", order=act["action"].act, card=act_outcome.get("card") is not None, inTurn=True,
                                 seconds=round(time.monotonic() - act["t0"], 2))
                            await to_client({"type": "tool_result", "name": "action", "arguments": {"kind": act["action"].act},
                                             "ok": reply["ok"], "summary": {"spoken": spoken_line_},
                                             "elapsedSeconds": round(time.monotonic() - act["t0"], 3)})
                        results.append({"call_id": item.get("call_id"), "role": "tool",
                                        "content": [{"type": "input_text", "text": json.dumps(reply, ensure_ascii=False)}]})
                    if stale():
                        # The session was replaced while the card was made: its line is never read in that turn, so the
                        # card is never armed (a card not read out is not confirmable by voice).
                        mark("staleToolHandler", tool="action")
                        if spoken_line_ and act_outcome is not None and "pending" in act_outcome:
                            await service.drop_action_card(state, act_outcome)
                        act["handover"].set()
                        return
                    await to_upstream({"type": "conversation.item.create", "items": results})
                    if (spoken_line_ and serial == state.turn_serial and act_outcome is not None
                            and not await service.binding_holds(state, act_outcome.get("binding"))):
                        await service.drop_action_card(state, act_outcome)
                        mark("actionRetired", order=act["action"].act)
                        state.mute_serial = serial
                        await mute_model_reply("tool")
                        spoken_line_ = ""
                    if spoken_line_ and serial == state.turn_serial and act_outcome is not None:
                        service.arm_action_card(state, act_outcome)
                        act["armed"] = True
                        await to_client({"type": "text", "delta": spoken_line_})
                        mark("speakCommit", tool="action", chars=len(spoken_line_), inTurn=True)
                        await inject(spoken_line_)
                    elif not spoken_line_ and serial == state.turn_serial:
                        state.mute_serial = serial
                        await mute_model_reply("tool")
                    if spoken_line_ and serial != state.turn_serial and act_outcome is not None and "pending" in act_outcome:
                        await service.drop_action_card(state, act_outcome)  # adopted, never armed: dropped
                    act["handover"].set()
                    return
                if front is not None and "query" in names:
                    front["called"] = True
                    mark("frontToolCall", tool=",".join(str(n) for n in names), seconds=round(time.monotonic() - front["t0"], 2))
                elif quick is not None and any(name in QUICK_TOOLS for name in names):
                    quick["called"] = True
                    mark("quickToolCall", tool=",".join(str(n) for n in names), seconds=round(time.monotonic() - quick["t0"], 2))
                elif quick is None and front is None and all(name in MODEL_ROUTED_TOOLS for name in names):
                    # A quote/positions/account call on a turn the Runtime already routed
                    # (delegate / intent): answered silently so the agent's read-out is the only voice.
                    mark("modelToolDropped", tool=",".join(str(n) for n in names))
                    await to_upstream({"type": "conversation.item.create", "items": [
                        {"call_id": item.get("call_id"), "role": "tool", "content": [{"type": "input_text", "text": json.dumps({"ok": True, "note": "系统已在处理这句，不要回答"}, ensure_ascii=False)}]}
                        for item in items]})
                    # Cancel here rather than waiting for the words that follow the tool result: measured
                    # The Runtime's own line can only be committed once the muted model turn closes.
                    await mute_model_reply("tool")
                    return
                await to_client({"type": "status", "state": "tool", "tools": names})
                results = []
                spoken: list[str] = []
                asking: list[dict[str, Any]] = []  # results whose line asks the next queued card (armed when committed)
                for item in items:
                    if quick is None and front is None and item.get("name") in MODEL_ROUTED_TOOLS:
                        # A mixed batch may retain read_screen; its business query still belongs to
                        # the already-routed turn, rather than becoming a second execution.
                        mark("modelToolDropped", tool=item.get("name"))
                        results.append({"call_id": item.get("call_id"), "role": "tool", "content": [
                            {"type": "input_text", "text": json.dumps({"ok": True, "note": "系统已在处理这句，不要回答"}, ensure_ascii=False)}]})
                        continue
                    try:
                        arguments = json.loads(item.get("arguments") or "{}")
                    except json.JSONDecodeError:
                        arguments = {}
                    t0 = time.monotonic()

                    async def progress() -> None:
                        await to_client({"type": "status", "state": "tool", "tools": names, "elapsedSeconds": round(time.monotonic() - t0, 1)})
                        if PROGRESS_INJECT:
                            await inject("还在查，稍等。")
                    routed = state.routed
                    if routed and routed["serial"] == serial and routed["name"] == item.get("name") and not routed["claimed"]:
                        # confirm / cancel: serve the model the same result instead of running it twice.
                        routed["claimed"] = True
                        outcome = await asyncio.shield(routed["task"])
                        mark("toolClaimedByModel", tool=item.get("name"))
                    else:
                        tool_name = str(item.get("name"))
                        if quick is not None and tool_name in QUICK_TOOLS:
                            # The Runtime heard the question too: the tool is the one the words asked for
                            # ("权益多少" is get_account even when Doubao reached for get_quote), and the contract
                            # named in the words wins over the model's guess.
                            expected = QUICK_TOOL_BY_KIND[quick["kind"]]
                            if tool_name != expected:
                                mark("quickToolRemapped", asked=tool_name, used=expected)
                                tool_name, arguments = expected, {}
                            if quick["symbol_query"]:
                                arguments = {**(arguments if isinstance(arguments, dict) else {}), "query": quick["symbol_query"]}
                            elif tool_name != "get_account" and not (isinstance(arguments, dict) and arguments.get("query")) \
                                    and state.last_symbol_query and REFERENTIAL_WORDS.search(quick["text"]):
                                # "刚才那个多少钱": the contract named earlier in the call, not the one on screen.
                                arguments = {"query": state.last_symbol_query}
                        outcome = None
                        if front is not None and tool_name == "query":
                            # The Runtime heard the words too: the kind and contract they named win over the
                            # model's guess, and the read it already started on the final is served.
                            asked = arguments if isinstance(arguments, dict) else {}
                            wanted = front_arguments(front, asked)
                            if wanted != {k: asked.get(k) for k in ("what", "query") if asked.get(k)}:
                                mark("frontToolRemapped", asked=json.dumps(asked, ensure_ascii=False)[:80],
                                     used=json.dumps(wanted, ensure_ascii=False)[:80])
                            arguments = wanted
                            prefetch = front.get("prefetch")
                            if prefetch is not None and front.get("prefetch_args") == wanted:
                                outcome = await prefetch
                            elif front["kind"] == "explain" and not re.search(r"多少|几|价|占用|现在", front["text"]):
                                # Local call 10-13: on "止损是啥意思" Doubao fetched a quote and then explained the
                                # price. A concept question asks for no data.
                                outcome = {"提示": "用户问的是概念，直接用大白话解释这个词，不念行情或账户数字"}
                        if outcome is None:
                            outcome = await business(service.run_tool(tool_name, arguments if isinstance(arguments, dict) else {}, state,
                                                                      progress=progress))
                        if front is not None and tool_name == "query" and not await service.binding_holds(state, front.get("binding")):
                            # #523 R1c: the conversation or account moved while the read ran — the result is not
                            # handed to the provider, spoken or recorded on the new one.
                            mark("frontRetired", front=front["kind"])
                            front["fallback"] = True
                            outcome = {"ok": False, "note": "用户已切换到别的对话或账户，这句不再回答"}
                        elif front is not None and tool_name == "query" and front.get("read"):
                            # A query the words named: the Runtime's line replaces Doubao's own wording of the
                            # result inside this turn (one injection, after the tool result).
                            front["spoken"] = spoken_line(outcome)
                            outcome = {**outcome, "spoken": front["spoken"]}
                    elapsed = round(time.monotonic() - t0, 3)
                    state.tool_log.append({"name": item.get("name"), "arguments": arguments, "ok": outcome.get("ok"), "elapsedSeconds": elapsed})
                    mark("toolDone", tool=item.get("name"), ok=outcome.get("ok"), toolSeconds=elapsed)
                    await to_client({"type": "tool_result", "name": item.get("name"), "arguments": arguments, "ok": outcome.get("ok"),
                                     "summary": {k: v for k, v in outcome.items() if k not in {"text", "content"}}, "elapsedSeconds": elapsed})
                    results.append({"call_id": item.get("call_id"), "role": "tool",
                                    "content": [{"type": "input_text", "text": json.dumps(outcome, ensure_ascii=False)[:6000]}]})
                    # The Runtime's own words replace this turn's speech: the agent's spoken summary, a
                    # confirmation result, a refusal. Left to the model they got paraphrased or re-acted on.
                    line = str(outcome.get("spoken") or outcome.get("say") or "")
                    if line:
                        spoken.append(line)
                        await to_client({"type": "text", "delta": line, "superseded": serial != state.turn_serial})
                        if outcome.get("nextCard"):
                            asking.append(outcome)
                await to_upstream({"type": "conversation.item.create", "items": results})
                def arm_asked() -> None:
                    for outcome in asking:  # the next card's question is committed with this line
                        service.arm_next_card(state, outcome)
                if spoken and serial == state.turn_serial and stale():
                    # The session whose turn this was has been replaced: the result is said out of turn on the new one.
                    await speak("。".join(spoken), on_commit=arm_asked)
                elif spoken and serial == state.turn_serial:
                    await inject("。".join(spoken), on_commit=arm_asked)
                elif spoken:
                    # The user moved on (barge-in or a new utterance) while this ran: the result stays in
                    # the model's context and in the conversation, but is not read out over the new turn — nor is
                    # the next card it asked, which was never heard and goes back to the queue.
                    mark("toolResultSuperseded", tool=",".join(str(n) for n in names))
                    for outcome in asking:
                        service.requeue_next_card(state, outcome)
                if front is not None and front["fallback"] and front.get("read") and serial == state.turn_serial:
                    state.mute_serial = serial  # a retired query (#523 R1c): Doubao says nothing either
                    await mute_model_reply("tool")
                await to_client({"type": "status", "state": "thinking"})

            async def hang_up_after_goodbye() -> None:
                """One goodbye (Doubao's own, or the Runtime's if it stays silent; a tool call it makes for
                "关闭语音" — on the replay `cancel` — is dropped), then the call ends once that audio has been
                delivered plus its playback."""
                state.hanging_up = True
                # Doubao's own goodbye ("已帮你关闭语音啦") usually starts within a second; only when it says
                # nothing does the Runtime speak one, so the user does not hear two.
                for _ in range(10):
                    if state.turn_response_seen:
                        break
                    await asyncio.sleep(HANGUP_GOODBYE_WAIT_SECONDS / 10)
                if not state.turn_response_seen:
                    state.audio_done.clear()
                    await to_client({"type": "text", "delta": "好，挂了。"})
                    await speak("好，挂了。")
                await line_played(HANGUP_AUDIO_WAIT_SECONDS)
                await asyncio.sleep(min(HANGUP_TAIL_SECONDS, max(0.0, state.play_seconds)))
                hangup.set()

            async def run_fast(intent: Intent, text: str, serial: int) -> None:
                """The zero-LLM path: the UI command lands with a receipt and one line is spoken, typically
                well under a second. Not applied (a panel the window cannot open, an unknown contract): the
                sentence goes to the agent as before."""
                t0 = time.monotonic()
                response_done = asyncio.Event()
                state.routed = {"serial": serial, "name": "intent", "task": None, "claimed": False, "response_done": response_done}
                outcome = await service.run_intent(intent, state)
                elapsed = round(time.monotonic() - t0, 3)
                if not outcome.get("ok"):
                    mark("intentFallback", intent=intent.kind, error=outcome.get("error"), seconds=elapsed)
                    await run_routed("delegate", text, serial)
                    return
                state.tool_log.append({"name": "intent", "arguments": {"kind": intent.kind, "changes": outcome.get("changes")}, "ok": True,
                                       "elapsedSeconds": elapsed, "routedBy": "intent"})
                mark("intentApplied", intent=intent.kind, seconds=elapsed)
                await to_client({"type": "tool_result", "name": "intent", "arguments": {"kind": intent.kind, "changes": outcome.get("changes")},
                                 "ok": True, "summary": {"say": outcome.get("say")}, "elapsedSeconds": elapsed})
                if serial != state.turn_serial:
                    mark("toolResultSuperseded", tool="intent", routedBy="intent")
                    return
                line = str(outcome.get("say") or "")
                if line:
                    # Doubao's own reply for the turn is cancelled as it starts (or ends empty); speak after
                    # that so the user hears one voice — at most a short wait.
                    try:
                        await asyncio.wait_for(response_done.wait(), timeout=ROUTED_RESPONSE_WAIT_SECONDS)
                    except TimeoutError:
                        mark("routedResponseWait", tool="intent")
                    if serial != state.turn_serial:
                        return
                    state.reading_serial = serial
                    await to_client({"type": "text", "delta": line})
                    mark("speakCommit", tool="intent", chars=len(line), seconds=round(time.monotonic() - t0, 2))
                    await speak(line)
                    state.reading_serial = None
                await to_client({"type": "status", "state": "listening"})

            async def run_review(action: str, text: str, serial: int) -> None:
                """引导复盘: once Doubao's own reply for the turn is cancelled (the route is muted), the Runtime
                takes the step (records the answer, moves on) and speaks the next line itself — no agent turn."""
                response_done = asyncio.Event()
                state.routed = {"serial": serial, "name": "review", "task": None, "claimed": False, "response_done": response_done}
                try:
                    await asyncio.wait_for(response_done.wait(), timeout=ROUTED_RESPONSE_WAIT_SECONDS)
                except TimeoutError:
                    mark("routedResponseWait", tool="review")
                if action == "review_answer":
                    # Wait out a pause mid-answer: its second half merges into this sentence and cancels
                    # this round before anything is recorded.
                    final_at = state.speech_end_at or time.monotonic()
                    while (time.monotonic() < final_at + CONTINUATION_SECONDS
                           and (state.asr_open or time.monotonic() < final_at + REVIEW_SETTLE_SECONDS)):
                        await asyncio.sleep(0.1)
                if serial != state.turn_serial:
                    return
                t0 = time.monotonic()
                line = await service.review_step(state, action, text)
                mark("reviewStep", action=action, active=state.review is not None, seconds=round(time.monotonic() - t0, 2))
                if serial != state.turn_serial or not line:
                    await to_client({"type": "status", "state": "listening"})
                    return
                await say_review(line, serial)

            async def say_review(line: str, serial: int | None = None) -> None:
                state.reading_serial = serial
                await to_client({"type": "text", "delta": line})
                await speak(line)
                state.reading_serial = None
                await to_client({"type": "status", "state": "listening"})

            async def open_review() -> None:
                """The call opened for the review (复盘页的“语音复盘”), or the review offer after the day session."""
                try:
                    if state.replay_id:
                        return   # a replay-bound call reviews nothing: the review reads the live account
                    if state.review_intent:
                        state.review_intent = False
                        line = await service.review_step(state, "review_start", "")
                    else:
                        line = await service.review_offer_line(state) or ""
                    if line:
                        mark("reviewOpened", offer=state.review_offer)
                        await say_review(line)
                except Exception as error:  # the call goes on without the review
                    log.warning("voice review open failed: %s: %s", type(error).__name__, error)

            async def run_routed(action: str, text: str, serial: int) -> None:
                """The Runtime's own handling of an utterance: run the tool now and read the answer out as it
                streams (first paragraph, sentence by sentence, each committed after the previous one's audio
                arrived). Doubao's own reply for the turn (the filler) is allowed to finish first; if Doubao
                calls the same tool itself the in-turn path speaks the result instead."""
                # 同句撤回 runs as the cancel tool (Doubao's own `cancel` for the turn is served the same result).
                name = "cancel" if action == "retract" else action
                arguments = ({"utterance": text} if name in {"delegate", "replay", "editor_ask"}
                             else {"retracted": True} if action == "retract"
                             else {"all": True} if name == "cancel" and CANCEL_ALL_WORDS.fullmatch(bare_form(text)) else {})
                # A take-back with nothing to drop is answered by the Runtime alone: the pair is recorded in the
                # conversation it was said in, like a front turn (#754 / #900 review).
                retract_binding = service.binding(state) if action == "retract" else None
                t0 = time.monotonic()
                # Routed from a partial: Doubao has not even finalised the sentence, so there is no filler
                # or response of its own to wait out (its late reply is cancelled when it comes).
                early = state.turn_early and serial == state.turn_serial
                earlier = state.routed
                follow_up = name == "delegate" and (
                    (state.reading_serial is not None and state.reading_serial != serial)
                    or (earlier is not None and earlier["name"] == "delegate" and earlier["serial"] != serial and not earlier["task"].done()))
                if follow_up:
                    mark("followUp", supersedes=state.reading_serial if state.reading_serial is not None else (earlier or {}).get("serial"))
                sentences: asyncio.Queue[str | None] = asyncio.Queue()
                streamed: dict[str, Any] = {"count": 0, "first_at": None, "text": ""}

                def on_sentence(sentence: str | None) -> None:
                    sentences.put_nowait(sentence)

                async def progress() -> None:
                    await to_client({"type": "status", "state": "tool", "tools": [name], "elapsedSeconds": round(time.monotonic() - t0, 1)})
                task = asyncio.ensure_future(service.run_tool(name, arguments, state, progress=progress,
                                                              on_sentence=on_sentence if name == "delegate" else None, follow_up=follow_up))
                response_done = asyncio.Event()
                routed: dict[str, Any] = {"serial": serial, "name": name, "task": task, "claimed": False, "response_done": response_done}
                state.routed = routed
                await to_client({"type": "status", "state": "tool", "tools": [name]})

                async def filler_done() -> None:
                    if early:
                        return
                    started_wait = time.monotonic()
                    while not (response_done.is_set() or (state.turn_response_seen and not state.speaking and state.audio_done.is_set())):
                        if not state.turn_response_seen and time.monotonic() - started_wait > FILLER_QUIET_SECONDS:
                            return  # Doubao said nothing for this turn: nothing to wait for
                        await asyncio.sleep(0.1)

                async def read_out() -> None:
                    """Commit each sentence once the filler ended and the previous sentence's audio is in.
                    When the first sentence is more than INTERIM_AFTER_SECONDS away, one bridge line
                    ("我看一下 KD 和 MACD") fills the silence — once, and not for a follow-up."""
                    waited = False
                    while True:
                        try:
                            sentence = await asyncio.wait_for(sentences.get(), timeout=max(0.1, t0 + INTERIM_AFTER_SECONDS - time.monotonic()))
                        except TimeoutError:
                            if streamed["count"] or follow_up or streamed.get("interim") or routed["claimed"] or serial != state.turn_serial:
                                sentence = await sentences.get()
                            else:
                                streamed["interim"] = True
                                try:
                                    await asyncio.wait_for(filler_done(), timeout=FILLER_WAIT_SECONDS)
                                except TimeoutError:
                                    pass
                                waited = True
                                if not sentences.empty() or routed["claimed"] or serial != state.turn_serial:
                                    sentence = await sentences.get()
                                else:
                                    # `name` is the routed tool: a semantic-judgment turn opens with the
                                    # neutral waiting line; a dispatched action keeps its own line.
                                    bridge = interim_line(text, route=name)
                                    state.audio_done.clear()
                                    await to_client({"type": "text", "delta": bridge})
                                    mark("interimSpoken", tool=name, text=bridge, seconds=round(time.monotonic() - t0, 2))
                                    await speak(bridge)
                                    await line_played(BRIDGE_AUDIO_WAIT_SECONDS)
                                    sentence = await sentences.get()
                        if sentence is None:
                            return
                        if routed["claimed"] or serial != state.turn_serial:
                            mark("toolResultSuperseded", tool=name, routedBy="runtime", streamed=streamed["count"])
                            return
                        if not waited:
                            waited = True
                            # Doubao's filler ("好，我去查") must be out of the way: its audio has ended or its
                            # response is done (the done event lags the audio by ~2 s); at most FILLER_WAIT_SECONDS.
                            try:
                                await asyncio.wait_for(filler_done(), timeout=FILLER_WAIT_SECONDS)
                            except TimeoutError:
                                mark("routedResponseWait", tool=name)
                            if routed["claimed"] or serial != state.turn_serial:
                                return
                        state.reading_serial = serial
                        state.audio_done.clear()
                        await to_client({"type": "text", "delta": sentence})
                        if streamed["first_at"] is None:
                            first_at = time.monotonic()
                            streamed["first_at"] = first_at
                            mark("speakFirst", tool=name, seconds=round(first_at - t0, 2))
                        streamed["count"] += 1
                        streamed["text"] += sentence
                        mark("speakCommit", tool=name, chars=len(sentence), n=streamed["count"])
                        await speak(sentence)
                        if not await line_played(SENTENCE_AUDIO_WAIT_SECONDS):
                            mark("speakAudioWait", tool=name)

                reader_task = asyncio.ensure_future(read_out()) if name == "delegate" else None
                try:
                    outcome = await asyncio.shield(task)
                except asyncio.CancelledError:
                    # 续句: the sentence continued, so this round is dropped — stop the agent turn and the
                    # read-out with it; the merged sentence is routed as a fresh round. The call ending stops only
                    # the read-out: the tool (a confirm, the trading AI's answer for the conversation) finishes.
                    if not state.closed:
                        task.cancel()
                    if reader_task is not None:
                        reader_task.cancel()
                    raise
                elapsed = round(time.monotonic() - t0, 3)
                if routed["claimed"]:
                    if reader_task is not None:
                        reader_task.cancel()
                    return  # handle_tool_calls logs, reports and speaks it inside the model's turn
                state.tool_log.append({"name": name, "arguments": arguments, "ok": outcome.get("ok"), "elapsedSeconds": elapsed, "routedBy": "runtime"})
                mark("toolDone", tool=name, ok=outcome.get("ok"), toolSeconds=elapsed, routedBy="runtime", streamed=streamed["count"])
                await to_client({"type": "tool_result", "name": name, "arguments": arguments, "ok": outcome.get("ok"),
                                 "summary": {k: v for k, v in outcome.items() if k not in {"text", "content"}}, "elapsedSeconds": elapsed})
                if reader_task is not None:
                    sentences.put_nowait(None)  # the listener already sent its own end; this guards a turn without deltas
                    try:
                        await reader_task
                    except asyncio.CancelledError:
                        pass
                if name == "delegate":
                    # Doubao stayed silent for this turn: give it the question and the answer it never heard.
                    await append_context(text, str(outcome.get("spoken") or outcome.get("say") or ""))
                if routed["claimed"]:
                    return
                if serial != state.turn_serial:
                    mark("toolResultSuperseded", tool=name, routedBy="runtime")
                    if state.reading_serial == serial:
                        state.reading_serial = None
                    service.requeue_next_card(state, outcome)  # its question was never read
                    return
                # The read-out (streamed sentences) ends here. What is left to say: the answer when nothing
                # streamed (a gated turn publishes no deltas; confirm/cancel results), or the confirmation
                # question of a proposal card the stream did not end with.
                line = str(outcome.get("spoken") or outcome.get("say") or "")
                if streamed["count"]:
                    tail = ""
                    if outcome.get("pendingConfirmation") and state.pending and "确认吗" not in streamed["text"]:
                        summary = str(state.pending.get("summary") or "")
                        tail = f"{summary}，确认吗？" if summary else "确认吗？"
                    elif outcome.get("autoConfirmed"):
                        # The streamed read-back ended with "确认吗"; say what the setting did instead of waiting.
                        tail = str(outcome.get("say") or "")
                    line = tail
                if not line:
                    state.reading_serial = None
                    return
                # Let Doubao's own reply for this turn (the filler) finish, then speak out of turn; a
                # model tool call arriving late still gets the same result through conversation.item.create.
                if not early:
                    try:
                        await asyncio.wait_for(response_done.wait(), timeout=FILLER_WAIT_SECONDS)
                    except TimeoutError:
                        mark("routedResponseWait", tool=name)
                if routed["claimed"]:
                    state.reading_serial = None
                    return  # the model's own call of the tool speaks (and arms) the same result
                if serial != state.turn_serial:
                    state.reading_serial = None
                    service.requeue_next_card(state, outcome)  # its question was never read
                    return
                state.reading_serial = serial
                await to_client({"type": "text", "delta": line})
                mark("speakCommit", tool=name, chars=len(line))
                # The next card's question is committed with this line: it becomes the waiting card there, not before.
                await speak(line, on_commit=lambda: service.arm_next_card(state, outcome))
                state.reading_serial = None
                if (retract_binding is not None and not outcome.get("dropped")
                        and await service.binding_holds(state, retract_binding)):
                    try:
                        await service.record_quick_turn(state, text, line, session_id=retract_binding.session_id)
                    except Exception as error:  # the transcript is a record, never the call's lifeline
                        log.warning("voice call %s take-back not recorded: %s", call_id, error)

            async def late_final(text: str) -> tuple[str | None, bool]:
                """Doubao's completed of a segment the Runtime already answered from its partial: the same
                words (or a second-pass rewording) are dropped (None) and Doubao's reply to them cancelled;
                words the user added after the early final come back as the text still to answer. The
                flag: the completed corrected the answered numeral — it replaces the early round, it is
                not a 续句 suffix to merge onto it."""
                done_raw, state.segment_final_text = state.segment_final_text, ""
                state.asr_bare, state.asr_sentence, state.early_checked_bare, state.asr_partial = "", None, "", ""
                done = bare_form(done_raw)
                if not done:
                    return text, False
                bare = bare_form(text)
                answered, numeral_changed = prefix_match(text, done_raw)
                if answered is not None and answered < len(bare):
                    # The answered words lead the completed: only what follows is new, however long the
                    # read-out in between took. A 10 s window here re-asked the answered question
                    # together with the next one ("帮我看看黄金的走势。现在螺纹钢多少钱？", 10-08) — and the
                    # second pass may rewrite the numeral (止损改成二十跳 → "止损改成 20 跳。打开画线工具栏。",
                    # 10-09), so the prefix matches by numeral value, separator boundaries included
                    # ("止损改成二十，点一下画线工具栏" keeps 点一下 the click, 10-10).
                    tail = after_bare(text, answered)
                    mark("asrFinalLate", text=text[:60], tail=tail[:60])
                    return tail, False
                if time.monotonic() - state.segment_final_at > LATE_FINAL_WINDOW_SECONDS:
                    return text, False  # the same words this much later: the user said them again
                if bare != done and answered != len(bare) and (numeral_changed
                        or difflib.SequenceMatcher(None, bare, done).ratio() < 0.5):
                    return text, numeral_changed  # a new sentence — or the second pass corrected the
                    # number (1 → 一点五跳): similar words, another value; the similarity dedup must not
                    # swallow it (10-10), and the correction replaces the round the early final started
                state.asr_open = False
                state.late_reply_until = time.monotonic() + LATE_REPLY_CANCEL_SECONDS
                mark("asrFinalLate", text=text[:60])
                return None, False

            async def early_final() -> None:
                """提前判停: the open segment's words stayed the same EARLY_FINAL_SECONDS after a sentence-
                ending partial — route it now when the Runtime answers it itself (see EARLY_ROUTES)."""
                sentence = state.asr_sentence
                if (EARLY_FINAL_SECONDS <= 0 or not sentence or not state.asr_open
                        or time.monotonic() - state.asr_changed_at < EARLY_FINAL_SECONDS):
                    return
                bare, done = state.asr_bare, state.segment_final_text
                if done and answered_prefix(state.asr_partial, done) is None and time.monotonic() - state.segment_final_at > LATE_FINAL_WINDOW_SECONDS:
                    # Doubao closed the answered segment without a completed and started a new one. While
                    # its partials still lead with the answered words they are stripped, however late.
                    done = state.segment_final_text = ""
                answered = answered_prefix(state.asr_partial, done) if done else None
                if bare == state.early_checked_bare or (done and (answered is None or len(bare) <= answered)):
                    return
                state.early_checked_bare = bare
                sentence_answered = answered_prefix(sentence, done) if done else None
                text = after_bare(sentence, sentence_answered) if sentence_answered is not None else sentence
                if not bare_form(text):
                    return
                route, _, _, front_pick = await classify_final(service, state, text, consume=False)
                if route not in EARLY_ROUTES and not (route == "front" and front_pick is not None and front_pick.kind == "query"):
                    return  # a front query is read by the Runtime too, so it may go early
                if route == "delegate" and state.action_on and front_order(sentence_intent(text, service.product_names, False)) is not None:
                    return  # an order card waits for the completed sentence: "撤单…不对" must not act on half of it
                intent = match_intent(text)
                if route == "delegate" and (looks_like_ui(text, service.product_names) or (intent is not None and intent.symbol_query is None)):
                    # Shared UI ownership changes the final's destination, not its endpointing:
                    # wait for the whole request before its first interface write. A symbol
                    # candidate is not an interface action until the symbol owner resolves it.
                    return
                state.segment_final_text, state.segment_final_at = state.asr_partial, time.monotonic()
                mark("asrEarlyFinal", route=route, heldSeconds=round(time.monotonic() - state.asr_changed_at, 2))
                await on_final(text, early=True)

            async def stall_final() -> None:
                """强制判停后仍无定稿 (STALL_FINAL_SECONDS): the last partial, minus what was already answered,
                is the sentence. Doubao never finalised it, so it would not answer it either — the Runtime
                answers whatever the route; Doubao's own completed, if it still comes, is dropped as late."""
                partial, bare, done = state.asr_partial, state.asr_bare, state.segment_final_text
                answered = answered_prefix(partial, done) if done else None
                if answered is not None:
                    text = after_bare(partial, answered) if answered < len(bare) else ""
                else:
                    text = partial
                if not bare_form(text):
                    return
                state.segment_final_text, state.segment_final_at = partial, time.monotonic()
                mark("asrStallFinal", heldSeconds=round(time.monotonic() - state.asr_changed_at, 2))
                await on_final(text, early=True, takeover=True)

            async def on_final(text: str, *, early: bool = False, takeover: bool = False, correction: bool = False) -> None:
                """One finished utterance (Doubao's completed, or a partial the Runtime finalised early):
                merge with a half sentence, route, and start the round. `takeover`: Doubao left the
                sentence open (stall final), so its own turns (chat / quick) go to the agent.
                `correction`: the completed re-states the answered sentence with its numeral put right —
                it replaces the early round instead of merging with it."""
                state.late_reply_until = 0.0  # a new sentence: Doubao's reply from here on is its own
                state.stall_commit_at = 0.0
                # 续句 (09-21): the user paused mid-sentence and Doubao cut the sentence into two
                # finals. The first round is merged back when it has not started answering (no
                # reply audio or response seen) and was itself a routed round; a round already
                # speaking, or a pause longer than the window, answers as its own question.
                previous, parts = state.last_final, 1
                # 下单类直接出卡: "算了" right after an order is the user taking it back, not the order's second half.
                taken_back = state.action_on and CANCEL_RUN_WORDS.match(strip_punctuation(text)) is not None
                if (previous is not None and previous.get("route") in MERGEABLE_ROUTES and not taken_back
                        # strictly inside the window: a window of 0 never merges (tests set it, #932 B5 — no real sleep)
                        and (correction or time.monotonic() - previous["at"] < CONTINUATION_SECONDS)
                        and state.audio_first_at is None and not state.turn_answer_seen
                        and (correction or len(str(previous.get("text") or "")) + len(text) <= CONTINUATION_MAX_CHARS)):
                    await cancel_open_round()
                    if correction:
                        # 数字纠正 (10-10): the corrected completed re-states the early round's own
                        # sentence, number put right (止损改成1 → 止损改成一点五跳) — the round it replaces
                        # is cancelled and its question superseded, but the sentence is not glued onto
                        # the words it corrects.
                        mark("utteranceCorrected", chars=len(text))
                    else:
                        # Doubao closes the first half with its own full stop ("帮我看看。"): dropped, so
                        # the merged sentence reads as one ("帮我看看沪金的持仓") for the routers and the
                        # transcript.
                        text = str(previous["text"]).rstrip("。．.，,！!？?；;、 ") + text
                        parts = int(previous.get("parts", 1)) + 1
                        mark("utteranceMerged", parts=parts, chars=len(text))
                    # The half-question's agent run keeps going server-side; the merged sentence is
                    # the round that gets answered. Cancel the run and mark the half user message
                    # superseded so the conversation hides it (message.superseded) instead of
                    # leaving a duplicate question with a 已停止 stub behind it.
                    run_id = state.inflight_run_id
                    state.inflight_run_id = None
                    if run_id:
                        session_id = state.chat_session_id or service._chat_session(state)
                        if session_id:
                            try:
                                await service.app.state.broker.publish(
                                    session_id, kind="message", name="message.superseded",
                                    payload={"turnId": f"{run_id}-user", "supersededBy": text},
                                )
                            except Exception as error:
                                log.debug("voice superseded publish failed: %s", error)
                        try:
                            await service._http("POST", f"/api/runs/{run_id}/cancel")
                        except Exception as error:
                            log.debug("voice run cancel failed: %s", error)
                state.speech_end_at = time.monotonic(); state.audio_first_at = None
                state.asr_open = False
                state.turn_serial += 1
                state.turn_early = early
                state.turn_response_seen = False
                state.turn_answer_seen = False
                state.audio_done.clear()  # this turn's filler audio has not ended yet
                mark("asrFinal", text=text[:60])
                await to_client({"type": "transcript", "text": text, "final": True})
                await to_client({"type": "status", "state": "thinking"})
                # Force the endpoint instead of waiting for the trailing silence.
                await to_upstream({"type": "input_audio_buffer.commit"})
                named = service.named_contract(text)
                if named:
                    state.last_symbol_query = named
                route, pending_now, names_known, front_pick = await classify_final(service, state, text, consume=True)
                action_pick = front_pick if route == "action" else None
                front_pick = front_pick if route == "front" else None
                if takeover and route in {"chat", "quick"}:
                    route = "delegate"
                stripped_final = strip_punctuation(text)
                state.last_final = {"text": text, "route": route, "at": time.monotonic(), "parts": parts}
                intent = match_intent(text) if route == "delegate" and not service.routine_named(state.user_id, text) else None
                if (pending_now and intent is not None and intent.symbol_query is not None
                        and not intent.explicit_chart):
                    intent = None  # a card's unresolved contract reply belongs to the shared Owner
                mark("routed", by="intent" if intent else "quick" if route == "quick" else "front" if route == "front"
                     else "action" if route == "action" else "runtime" if route != "chat" else "model",
                     action=intent.kind if intent else route,
                     **({"front": front_pick.kind, "what": front_pick.what} if front_pick is not None else {}),
                     **({"order": action_pick.act} if action_pick is not None else {}))
                previous_act = state.action
                if previous_act is not None and not previous_act.get("armed"):
                    # The user moved on before the last order's line was read: its card is dropped unseen.
                    asyncio.ensure_future(drop_unread(previous_act))
                # 打断 (09-21): "停"/"算了" with a round still running cancels it — the local task,
                # the agent run server-side, and any audio still generating; a business sentence
                # supersedes the running round the same way, without extra words. Chat and small
                # talk cancel nothing.
                inflight = state.routed_task is not None and not state.routed_task.done()
                # an editor_ask supersedes a running round like any new business sentence.
                stopping = route in {"stop", "retract"} or (not route.startswith(("editor_", "review_")) and not pending_now
                                                            and CANCEL_RUN_WORDS.match(stripped_final))
                if inflight and (stopping or route in {"delegate", "quick", "replay", "chase", "watch_cancel", "editor_ask",
                                                      "breakeven_move", "breakeven_decline", "action"}
                                 or (front_pick is not None and front_pick.kind in {"query", "explain"})
                                 or route in REVIEW_ROUTES or intent is not None):
                    run_id = state.inflight_run_id
                    state.inflight_run_id = None
                    mark("turnCancelled" if stopping else "turnSuperseded", by="stop" if route == "stop" else "cancel" if stopping else "new")
                    await cancel_open_round()
                    if run_id:
                        try:
                            await service._http("POST", f"/api/runs/{run_id}/cancel")
                        except Exception as error:
                            log.debug("voice run cancel failed: %s", error)
                    if stopping:
                        state.reading_serial = None
                        await to_client({"type": "text", "delta": "已停止。"})
                        await speak("已停止。")
                        await to_client({"type": "status", "state": "listening"})
                        return
                # 前台直接查询: a sentence routed from a partial or left open by Doubao (stall final) is answered by
                # the Runtime at once — Doubao has no reply of its own to wait out. Neither goes to the trading AI.
                direct_front = route == "front" and front_pick is not None and (takeover or early)
                direct_action = route == "action" and (takeover or early or ACTION_SPEAK_DIRECT)
                if direct_action and not (takeover or early):
                    # The Runtime reads the card as soon as it is made; Doubao's own reply to this final (its
                    # `action` call or words) is cancelled whenever it comes, like a late reply.
                    state.late_reply_until = time.monotonic() + LATE_REPLY_CANCEL_SECONDS
                if route in {"delegate", "confirm", "cancel", "retract", "replay", "chase", "watch_cancel",
                             "breakeven_move", "breakeven_decline", "editor_ask"} or route in REVIEW_ROUTES or intent \
                        or direct_front or direct_action:
                    # The Runtime speaks for this turn: Doubao's own reply is cancelled when it starts.
                    state.mute_serial = state.turn_serial
                    state.mute_cancel_pending = False
                state.quick = None
                state.front = None
                state.action = None
                if route == "action" and action_pick is not None:
                    # 下单类直接出卡: the card is made from the words now (the trading AI's own card tool); Doubao's
                    # `action` call reads it in its tool turn, or the Runtime does (early / stall final, Doubao spoke
                    # first or stayed silent). A card whose round is dropped before its line is read is rejected.
                    act_serial, final_at = state.turn_serial, time.monotonic()

                    async def settle() -> bool:
                        """Irreversible steps (a cancel, an auto-confirmed card) wait out the continuation window:
                        "撤单…不对" must not act on the first half."""
                        await asyncio.sleep(max(0.0, final_at + CONTINUATION_SECONDS - time.monotonic()))
                        return act_serial == state.turn_serial and not (state.action or {}).get("dropped")
                    accepted = service.binding(state)  # #523 R1c: where the sentence was said
                    if spoken_contract(action_pick):
                        state.last_symbol_query = spoken_contract(action_pick)
                    state.action = {"serial": act_serial, "text": text, "action": action_pick, "direct": direct_action,
                                    "called": False, "fallback": False, "read": False, "t0": final_at,
                                    "handover": asyncio.Event(), "routed": None, "settle": settle,
                                    "prefetch": asyncio.ensure_future(service.front_action(action_pick, state, text, settle=settle, accepted=accepted)),
                                    "binding": accepted}
                    state.routed_task = spawn(run_action(state.action))
                if route == "front" and front_pick is not None:
                    # Explanations, small talk and half sentences: Doubao answers this turn itself (calling `query`
                    # if it wants data); the Runtime serves the tool and records the pair. A query the words named
                    # (`read`): the read starts here, and when Doubao calls `query` the Runtime's own line replaces
                    # its reply inside the turn (measured 10-13: Doubao's own wording after the tool result took
                    # 1.1–1.4 s more to its first word; cancelling it and speaking afterwards ~1 s more).
                    state.front = {"serial": state.turn_serial, "text": text, "kind": front_pick.kind, "what": front_pick.what,
                                   "symbol_query": front_pick.symbol_query, "called": False, "fallback": direct_front,
                                   "read": front_pick.kind == "query", "spoken": "", "t0": time.monotonic(),
                                   "prefetch": None, "prefetch_args": None,
                                   # #523 R1c: the session and account this turn was accepted on
                                   "binding": service.binding(state)}
                    if front_pick.kind == "query":
                        state.last_what = front_pick.what
                        if front_pick.symbol_query:
                            # The catalog name the words meant ("黄金" → 沪金): "它多少钱" next means this product.
                            state.last_symbol_query = front_pick.symbol_query
                        args = front_arguments(state.front)
                        state.front["prefetch_args"] = args
                        state.front["prefetch"] = asyncio.ensure_future(service.run_tool("query", args, state))
                    if direct_front:
                        state.routed_task = spawn(run_front_takeover(state.front))
                if route == "quick":
                    # Doubao answers with a quick tool; if it starts talking without calling one, the
                    # turn falls back to the agent (see the text/audio handlers).
                    asked = match_quick_query(text, names_known)
                    state.quick = {"serial": state.turn_serial, "text": text, "called": False, "fallback": False, "spoken": "", "t0": time.monotonic(),
                                   "kind": asked.kind if asked else "quote", "symbol_query": asked.symbol_query if asked else None}
                if intent:
                    state.routed_task = spawn(run_fast(intent, text, state.turn_serial))
                elif route in REVIEW_ROUTES:
                    state.routed_task = spawn(run_review(route, text, state.turn_serial))
                elif route == "hangup":
                    # The Runtime ends the call itself: one short goodbye, then session.close (the
                    # client's `closed` summary puts the orb back to standby).
                    state.reading_serial = None
                    spawn(hang_up_after_goodbye())
                elif route == "stop":
                    # Only the read-out stops: cancel what Doubao is still generating, drop the client's
                    # queue; the answer stays in the conversation.
                    if state.speaking:
                        state.cancelled_readouts |= state.runtime_responses
                        await to_upstream({"type": "response.cancel"})
                    state.speaking = False; state.play_started_at = None
                    state.reading_serial = None
                    await to_client({"type": "interrupt"})
                    await to_client({"type": "status", "state": "listening"})
                elif route not in {"chat", "quick", "front", "action"}:
                    state.routed_task = spawn(run_routed(route, text, state.turn_serial))

            async def run_reader(ws: Any, ready: asyncio.Event, gone: asyncio.Event, gen: int) -> None:
                """One session's reader, tagged with its generation (`_upstream_gen`, inherited by every handler it
                starts). When it ends — the session closed, the connection dropped, the reader failed — `gone` is set
                (a candidate's failure, for recover_upstream: never the whole call's `stop`, #904 review); for the
                session in use that is the close acknowledged at call end, or else the session is lost and replaced
                (#523: a session.closed mid-call left the call alive with no upstream)."""
                _upstream_gen.set(gen)
                why = "ended"
                try:
                    why = await upstream_reader(ws, ready, gen) or why
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    why = "failed"
                    log.warning("voice call %s upstream reader gen=%s failed: %s: %s", call_id, gen, type(error).__name__, error)
                gone.set()
                if gen != link["gen"]:
                    return
                if state.closed:
                    link["lost"] = True
                    stop.set()  # the session.close sent at call end is acknowledged
                else:
                    upstream_lost(why)

            async def upstream_reader(ws: Any, ready: asyncio.Event, gen: int) -> str | None:
                async for raw in ws:
                    try:
                        event = json.loads(raw)
                    except Exception:
                        continue
                    kind = event.get("type")
                    if state.upstream_expect_by and _answers_expectation(state.upstream_expect, event):
                        if state.upstream_expect == "commit" and kind == "response.output_audio.started" and state.unheard:
                            state.unheard.pop(0)  # the oldest line started; a later one keeps its own deadline
                            watch_lines()
                        else:
                            state.unheard.clear()
                            state.upstream_expect, state.upstream_expect_by = "", 0.0
                    # A cancel's acknowledgement (a canceled, and the response.done after it or after an open cancel)
                    # says nothing about the line committed since — the Runtime never cancels its own line by itself,
                    # and an earlier cancel's tail arrives late: it is no sign of a live session (#905 review).
                    acknowledges = kind == "response.canceled" or (
                        kind == "response.done" and bool(state.cancel_sent_at or state.cancel_done_owed))
                    if kind in {"response.canceled", "response.done"}:
                        state.cancel_done_owed = kind == "response.canceled"
                    if kind != "conversation.item.input_audio_transcription.started" and not acknowledges:
                        state.upstream_seen_at = time.monotonic()
                    if kind in {"response.canceled", "response.done"}:
                        state.cancel_sent_at = 0.0
                    # #523 R4: the provider boundary events as they arrived, before any local
                    # filtering — an event dropped below still shows here with its ids and the
                    # audio accounted so far. Never marked per audio.delta.
                    if isinstance(kind, str) and kind in PROVIDER_TRACE_EVENTS:
                        mark("providerEvent", **_provider_trace_fields(event), turn=state.turn_serial,
                             mute=state.mute_cancel_pending, commit=state.committed_serial,
                             audio="recv {recvFrames}/{recvBytes}B drop {dropFrames}/{dropBytes}B fwd {fwdFrames}/{fwdBytes}B".format(**audio_diag))
                    frame_bytes = 0
                    if kind == "response.output_audio.delta":
                        # The frame's real decoded size; a non-string or invalid delta counts 0
                        # bytes and changes nothing in the business path below.
                        frame_bytes = _audio_frame_bytes(event.get("delta"))
                        audio_diag["recvFrames"] += 1
                        audio_diag["recvBytes"] += frame_bytes
                    if kind == "session.created":
                        state.session_id = (event.get("session") or {}).get("id")
                        ready.set()
                        if gen:
                            continue  # a swapped-in session: recover_upstream says the line and resumes listening
                        await to_client({"type": "status", "state": "listening", "sessionId": state.session_id})
                        link["opened"] = True
                        spawn(open_review())
                    elif kind == "conversation.item.input_audio_transcription.started":
                        # Not a barge-in by itself: Doubao also sends it, wordless, when a reply starts or
                        # is cancelled (the ghost start, 09-28) — it cut off answers the client was still
                        # playing (10-08). The first recognised words interrupt (the delta below).
                        mark("asrStart")
                        if not (state.speaking or state.still_playing()):
                            await to_client({"type": "status", "state": "listening"})
                    elif kind == "conversation.item.input_audio_transcription.delta":
                        partial = clean_transcript(str(event.get("delta") or ""))
                        # Doubao re-sending the sentence the Runtime already answered (early / stall final) is not
                        # the user talking over the answer: it neither interrupts the read-out nor releases it.
                        replay = replays_answered(partial, state.segment_final_text, service.product_names)
                        if bare_form(partial) and not replay and (state.speaking or state.still_playing()):
                            # The user talks over the assistant (words, as push-to-talk counts speech):
                            # barge-in. Cancel upstream if it is still generating and tell the client to
                            # drop its playback queue.
                            if state.speaking:
                                state.cancelled_readouts |= state.runtime_responses
                                await to_upstream({"type": "response.cancel"})
                            state.speaking = False
                            state.play_started_at = None
                            # Talking over the read-back ("确认" early): the window starts now.
                            await service._open_confirm_window(state, time.monotonic() + state.preferences.voice.confirmWindowSeconds)
                            mark("bargeIn")
                            await to_client({"type": "interrupt"})
                            await to_client({"type": "status", "state": "listening"})
                        now = time.monotonic()
                        state.asr_open, state.asr_last_delta_at = True, now
                        bare = bare_form(partial)
                        if bare != state.asr_bare:
                            state.asr_bare, state.asr_changed_at, state.asr_sentence = bare, now, None
                            state.stall_commit_at = 0.0  # new words: the user is still talking
                        state.asr_partial = partial
                        if partial.rstrip().endswith(SENTENCE_END):
                            state.asr_sentence = partial
                        # A segment Doubao kept open past an answered sentence still leads with it: the
                        # client shows only the words after it.
                        done = state.segment_final_text
                        answered = answered_prefix(partial, done) if done else None
                        shown = after_bare(partial, answered) if answered is not None and answered < len(bare) else partial
                        if not replay:
                            lines_give_way()  # the user is saying something new
                        await to_client({"type": "transcript", "text": shown, "final": False})
                    elif kind == "conversation.item.input_audio_transcription.completed":
                        state.stall_commit_at = 0.0
                        text = clean_transcript(str(event.get("text") or event.get("transcript") or ""))
                        still, correction = await late_final(text)
                        if still is not None:
                            if bare_form(still):
                                lines_give_way()
                            await on_final(still, correction=correction)
                    elif kind == "conversation.item.input_audio_transcription.failed":
                        state.asr_open = False
                        mark("asrFailed", error=json.dumps(event.get("error") or {}, ensure_ascii=False)[:120])
                        await to_client({"type": "status", "state": "listening"})
                    elif kind == "response.function_call_arguments.done":
                        # A call id this session already made is a replay (Doubao re-sends answered sentences, #905): the
                        # tool never runs twice (#523).
                        fresh = [item for item in event.get("items") or []
                                 if not item.get("call_id") or str(item["call_id"]) not in link["calls"]]
                        if len(fresh) < len(event.get("items") or []):
                            mark("toolCallReplayDropped", tool=",".join(str(item.get("name")) for item in event.get("items") or []
                                                                       if item not in fresh))
                        if not fresh:
                            continue
                        event = {**event, "items": fresh}
                        link["calls"].update(str(item.get("call_id")) for item in fresh if item.get("call_id"))
                        state.turn_response_seen = True
                        state.turn_answer_seen = True  # a quick/ui tool call is the turn in flight: do not merge into it
                        act = state.action
                        if (act and act["serial"] == state.turn_serial and not act["fallback"]
                                and any(item.get("name") == "action" for item in event.get("items") or [])):
                            # Marked here, not in the task: the tool turn's response.done can be read before the
                            # task runs, and must not count as a silent turn.
                            act["called"] = True
                        spawn(handle_tool_calls(list(event.get("items") or [])))
                    elif kind == "response.output_text.delta":
                        state.turn_response_seen = True
                        if await quick_fallback("text") or await front_fallback("text") or await action_fallback("text"):
                            continue
                        if await mute_model_reply("text", response_id=event.get("response_id")) or dropping(event):
                            continue  # the filler's words never reach the transcript
                        if state.quick and state.quick["serial"] == state.turn_serial:
                            state.quick["spoken"] += str(event.get("delta") or "")
                        if state.front and state.front["serial"] == state.turn_serial and not state.front["fallback"]:
                            if state.front.get("read") and state.front["called"]:
                                continue  # the Runtime's line replaced this wording (already sent and recorded)
                            state.front["spoken"] += str(event.get("delta") or "")
                        state.turn_answer_seen = True  # unmuted words reached the user
                        await to_client({"type": "text", "delta": event.get("delta")})
                    elif kind == "response.output_audio.started":
                        state.turn_response_seen = True
                        state.current_output_id = str(event.get("response_id") or "") or None
                        if event.get("tts_type") == "chat_tts_text" and event.get("response_id"):
                            state.runtime_responses.add(str(event["response_id"]))
                        if await quick_fallback("audio") or (event.get("tts_type") != "chat_tts_text" and (
                                await front_fallback("audio") or await action_fallback("audio"))):
                            continue
                        if await mute_model_reply("audio", runtime_speech=event.get("tts_type") == "chat_tts_text",
                                                  response_id=event.get("response_id")) or dropping(event):
                            continue
                        state.speaking = True
                        state.turn_answer_seen = True  # unmuted audio reached the user
                        # The client plays responses back to back: a sentence committed while the previous
                        # one is still playing starts after it, so the playback estimate continues the queue.
                        now = time.monotonic()
                        queued_until = (state.play_started_at or 0.0) + state.play_seconds
                        state.play_started_at = queued_until if state.reading_serial is not None and queued_until > now else now
                        state.play_seconds = 0.0
                        mark("audioStart")
                        await to_client({"type": "status", "state": "speaking", "ttsType": event.get("tts_type")})
                    elif kind == "response.output_audio.delta":
                        if dropping(event):
                            audio_diag["dropFrames"] += 1
                            audio_diag["dropBytes"] += frame_bytes
                            continue  # audio of the reply being cancelled
                        if state.audio_first_at is None and state.speech_end_at is not None:
                            state.audio_first_at = time.monotonic()
                            sample = {"latencySeconds": round(state.audio_first_at - state.speech_end_at, 3), "at": round(time.monotonic() - started, 1)}
                            state.latency.append(sample)
                            mark("audioFirst", latencySeconds=sample["latencySeconds"])
                            await to_client({"type": "latency", **sample})
                        # Doubao's "pcm" output is float32 little-endian at 24 kHz: 4 bytes per sample.
                        state.play_seconds += len(event.get("delta") or "") * 3 / 4 / (4 * 24000)
                        if await to_client({"type": "audio", "data": event.get("delta"), "encoding": OUTPUT_ENCODING}):
                            audio_diag["fwdFrames"] += 1
                            audio_diag["fwdBytes"] += frame_bytes
                    elif kind == "response.output_audio.done":
                        if dropping(event):
                            continue  # the cancelled reply's audio tail (Doubao still delivers what it synthesised)
                        state.speaking = False
                        state.audio_done.set()
                        state.current_output_id = None  # this output is over: later id-less events are not its
                        mark("audioDone", playSeconds=round(state.play_seconds, 1))
                        if state.play_started_at is not None:
                            # The confirmation window starts when the read-back has finished playing.
                            await service._open_confirm_window(state, state.play_started_at + state.play_seconds + state.preferences.voice.confirmWindowSeconds)
                        await to_client({"type": "audio_end"})
                        await to_client({"type": "status", "state": "listening"})
                    elif kind == "response.done":
                        usage = (event.get("response") or {}).get("usage") or {}
                        state.usage.append(usage)
                        try:  # the same daily meter as chat turns — a muted (cancelled) reply used tokens too
                            service.app.state.store.add_token_usage(identity.user_id, datetime.now(UTC).date().isoformat(), int(usage.get("total_tokens") or 0))
                        except Exception:
                            log.exception("voice token usage record failed")
                        if state.mute_cancel_pending:
                            # The muted reply is over (its audio kept arriving after `canceled`; all dropped):
                            # the Runtime may speak for the turn now.
                            state.mute_cancel_pending = False
                            state.late_muted_response = None
                            state.speaking = False
                            state.audio_done.set()
                            if state.routed and state.routed["serial"] == state.turn_serial:
                                state.routed["response_done"].set()
                            mark("modelReplyCanceled")
                            continue
                        mark("responseDone")
                        quick = state.quick
                        if (quick and quick["serial"] == state.turn_serial and not quick["called"]
                                and not quick["fallback"]):
                            # 静默的快捷轮次 (#523): 工具没调、话也没说就把轮次结束——这句照样转给
                            # agent（与先说话的回退同路）；轮次已结束，无需再取消什么。
                            quick["fallback"] = True
                            state.mute_serial = state.turn_serial
                            state.audio_done.set()  # the model's turn is already over: nothing to wait out
                            mark("quickQueryFallback", reason="no_tool_call",
                                 seconds=round(time.monotonic() - quick["t0"], 2))
                            spawn(run_routed("delegate", quick["text"], quick["serial"]))
                        if quick and quick["serial"] == state.turn_serial and quick["called"] and not quick["fallback"] and not quick.get("recorded"):
                            quick["recorded"] = True
                            state.tool_log.append({"name": "quick", "arguments": {"text": quick["text"]}, "ok": True,
                                                   "elapsedSeconds": round(time.monotonic() - quick["t0"], 3), "routedBy": "quick"})
                            mark("quickDone", seconds=round(time.monotonic() - quick["t0"], 2), chars=len(quick["spoken"]))
                            try:
                                await service.record_quick_turn(state, quick["text"], quick["spoken"])
                            except Exception as error:  # the transcript is a record, never the call's lifeline
                                log.warning("voice call %s quick turn not recorded: %s", call_id, error)
                        front = state.front
                        if front and front["serial"] == state.turn_serial and not front["fallback"] and not front.get("recorded"):
                            if not front["called"] and not front["spoken"].strip():
                                # Doubao ended the turn without a word or a tool call. Its brief keeps it silent on
                                # business, so a concept question it would not answer goes to the agent; a query
                                # or a half sentence is answered by the Runtime (no trading AI); small talk rests.
                                front["fallback"] = True
                                state.audio_done.set()
                                mark("frontSilent", front=front["kind"], seconds=round(time.monotonic() - front["t0"], 2))
                                if front["kind"] in {"query", "half"}:
                                    state.routed_task = spawn(run_front_takeover(front))
                                elif front["kind"] == "explain":
                                    state.mute_serial = state.turn_serial
                                    state.routed_task = spawn(run_routed("delegate", front["text"], front["serial"]))
                            elif front["spoken"].strip():
                                front["recorded"] = True
                                state.tool_log.append({"name": "front", "arguments": {"kind": front["kind"], "text": front["text"]}, "ok": True,
                                                       "elapsedSeconds": round(time.monotonic() - front["t0"], 3), "routedBy": "front"})
                                mark("frontDone", front=front["kind"], called=front["called"], chars=len(front["spoken"]),
                                     seconds=round(time.monotonic() - front["t0"], 2))
                                if await service.binding_holds(state, front.get("binding")):
                                    try:
                                        await service.record_quick_turn(state, front["text"], front["spoken"],
                                                                        session_id=front["binding"].session_id)
                                    except Exception as error:  # the transcript is a record, never the call's lifeline
                                        log.warning("voice call %s front turn not recorded: %s", call_id, error)
                                else:
                                    mark("frontRetired", front=front["kind"])  # #523 R1c: not written to the new conversation
                        act = state.action
                        if (act and act["serial"] == state.turn_serial and not act["direct"] and not act["called"]
                                and not act["fallback"] and not act["read"]):
                            # Doubao ended the order turn without calling `action` or saying a word: the Runtime
                            # reads the card itself (nothing to cancel).
                            act["fallback"] = True
                            state.mute_serial = state.turn_serial
                            state.audio_done.set()
                            mark("actionSilent", seconds=round(time.monotonic() - act["t0"], 2))
                            act["handover"].set()
                        if state.routed and state.routed["serial"] == state.turn_serial:
                            state.routed["response_done"].set()  # the model's turn ended (with words, or silent)
                        await to_client({"type": "usage", "usage": usage, "turns": len(state.usage)})
                    elif kind == "response.canceled":
                        state.speaking = False
                        if state.mute_cancel_pending:
                            routed = state.routed
                            if routed and routed["name"] in {"front", "action"} and routed["serial"] == state.turn_serial:
                                # 前台查询的接管念句不等 response.done（实测取消后还要约 0.5 s 才到）：回复已取消，
                                # 剩下的尾巴由 `dropping` 丢掉，Runtime 自己的念句不受影响。
                                routed["response_done"].set()
                            continue  # the Runtime's own cancel of Doubao's reply: the turn goes on until its response.done
                        if state.routed and state.routed["serial"] == state.turn_serial:
                            state.routed["response_done"].set()
                        state.turn_serial += 1
                        mark("canceled")
                    elif kind == "error":
                        log.warning("voice call %s upstream error: %s", call_id, json.dumps(event, ensure_ascii=False)[:500])
                        await to_client({"type": "error", "error": event})
                    elif kind == "session.closed":
                        log.info("voice call %s upstream session gen=%s closed: %s", call_id, gen,
                                 json.dumps(event, ensure_ascii=False)[:300])
                        return "closed"  # run_reader decides what this session's end means
                return None

            async def client_reader() -> None:
                while True:
                    message = prefetched.pop(0) if prefetched else await client.receive()
                    if message.get("type") == "websocket.disconnect":
                        raise WebSocketDisconnect()
                    if message.get("bytes"):
                        mic.add(message["bytes"], time.monotonic())
                        if not state.reconnecting:  # a session being swapped in hears nothing before it is ready
                            await to_upstream({"type": "input_audio_buffer.append", "audio": base64.b64encode(message["bytes"]).decode()})
                        continue
                    text = message.get("text")
                    if not text:
                        continue
                    try:
                        command = json.loads(text)
                    except json.JSONDecodeError:
                        continue
                    kind = command.get("type")
                    if kind == "audio":
                        mic.add_base64(command.get("data"), time.monotonic())
                        if not state.reconnecting:
                            await to_upstream({"type": "input_audio_buffer.append", "audio": command.get("data", "")})
                    elif kind == "hello":
                        # The caller names its own UI window (tools act on this device only) and the chat
                        # session it is showing (delegate turns join that conversation); a session that
                        # is not this user's own live one is ignored, not adopted.
                        state.window_id = str(command.get("windowId") or "") or None
                        named = service._own_session(state, str(command.get("sessionId") or "")) or state.chat_session_id
                        if named != state.chat_session_id:
                            state.session_gen += 1  # #523 R1c: front results accepted on the old conversation retire
                            mark("sessionMoved")
                        state.chat_session_id = named
                        if command.get("intent") == "review" and state.review is None:
                            state.review_intent = True   # 语音复盘 pressed during a call: start it now
                            spawn(open_review())
                    elif kind in {"mute", "unmute"}:
                        # Kept here; a session being swapped in gets it once it is ready (#904 review: sent mid-swap it
                        # went to a closing or not-yet-ready session).
                        mic.muted = kind == "mute"
                        if not state.reconnecting:
                            await to_upstream({"type": f"input_audio_{kind}.commit"})
                    elif kind == "cancel":
                        # Client-side voice activity while playing: the client already flushed its queue.
                        if state.speaking and not state.reconnecting:
                            state.cancelled_readouts |= state.runtime_responses
                            await to_upstream({"type": "response.cancel"})
                        state.speaking = False; state.play_started_at = None
                    elif kind == "stop":
                        raise WebSocketDisconnect()

            async def endpoint_watchdog() -> None:
                while not state.closed:
                    await asyncio.sleep(ASR_WATCHDOG_TICK)
                    await early_final()
                    stall_seconds = state.preferences.voice.endpointStallSeconds
                    if state.asr_open and time.monotonic() - state.asr_last_delta_at > stall_seconds:
                        state.asr_open = False
                        state.stall_commit_at = state.stall_commit_at or time.monotonic()
                        mark("asrForcedCommit", stallSeconds=stall_seconds)
                        await to_upstream({"type": "input_audio_buffer.commit"})
                    elif state.stall_commit_at and time.monotonic() - state.stall_commit_at >= STALL_FINAL_SECONDS:
                        state.stall_commit_at = 0.0
                        await stall_final()

            async def quote_watchdog() -> None:
                while not state.closed:
                    await asyncio.sleep(QUOTE_WATCH_POLL_SECONDS)
                    pending = state.pending
                    if not pending:
                        continue
                    stale = pending.get("stale_quote")
                    if not stale:
                        continue
                    market = await service._market_for(str(stale.get("symbol") or ""))
                    age = _quote_age_seconds(market)
                    if (not stale.get("reminded") and age is not None and age <= 15.0
                            and str(market.get("quoteDatetime") or "") != str(stale.get("quoted_at") or "")):
                        stale["reminded"] = True
                        stale["quoted_at"] = str(market.get("quoteDatetime") or "")
                        line = "有新报价了，说确认就下。"
                        await to_client({"type": "text", "delta": line})
                        mark("quoteFreshAgain", ageSeconds=round(age, 1))
                        await speak(line)
                        await service._open_confirm_window(state, time.monotonic() + state.preferences.voice.confirmWindowSeconds)
                        continue
                    # The quiet quote is the card's own reason to wait: the window follows it (bounded
                    # by the hard cap, so a quote that never comes back still ends the card).
                    if time.monotonic() - float(pending.get("created_at") or 0.0) < PENDING_HARD_CAP_SECONDS:
                        pending["deadline"] = time.monotonic() + state.preferences.voice.confirmWindowSeconds
                        if time.monotonic() - float(pending.get("marked_at") or 0.0) >= QUOTE_REMARK_SECONDS:
                            pending["marked_at"] = time.monotonic()
                            confirm_by = (datetime.now(UTC) + timedelta(seconds=state.preferences.voice.confirmWindowSeconds)).isoformat()
                            for item in pending.get("proposals") or []:
                                await service._mark_card(item, confirm_by)

            async def announcer() -> None:
                while not state.closed:
                    await asyncio.sleep(ANNOUNCE_POLL_SECONDS)
                    items = service._pending_announcements.pop(identity.user_id, [])
                    # A reminder tagged with a replay id is that replay's own; an untagged one is
                    # the live world's. Neither crosses over: a live call plays no replay
                    # reminders, a replay call plays none of the live ones (#521 R2).
                    items = [item for item in items if item.get("replayId") == state.replay_id]
                    if state.pending and service._pending_expired(state) and not state.speaking:
                        # The queue stays on the call across the reject (a hang-up meanwhile retires it).
                        await service._abandon_pending(state, keep_queue=True)
                        await to_client({"type": "tool_result", "name": "cancel", "ok": True, "summary": {"reason": "confirmation_timeout"}, "elapsedSeconds": 0})
                        timed_out = service._with_next_card(state, {"say": "没有听到确认，已放弃。"})
                        await speak(str(timed_out["say"]), on_commit=lambda: service.arm_next_card(state, timed_out))
                    for item in items:
                        if service._throttled(state, item.get("throttleKey") or item["kind"]):
                            continue
                        await to_client({"type": "announce", "kind": item["kind"], "text": item["text"]})
                        await speak(item["text"])
                        if str(item["kind"]).startswith("breakeven-"):
                            # 保本提醒 read out: its 移 / 不用 window starts with this read-out.
                            state.breakeven_heard = {"id": item["kind"], "at": time.monotonic()}

            async def announcer_guarded() -> None:
                try:
                    await announcer()
                except asyncio.CancelledError:
                    raise
                except Exception as error:  # the loop must outlive one bad poll
                    log.warning("voice announcer stopped: %s: %s", type(error).__name__, error)

            async def voice_budget_watchdog() -> None:
                """M2: the tier's daily voice minutes, checked once a minute — a
                long call is cut off shortly after the cap with one spoken line,
                reusing the spoken-hangup path."""
                limit = (None if service.local_mode
                         else plan_for_user(service.app.state.store, identity.user_id).voice_seconds_per_day)
                if limit is None:
                    return
                while not state.closed:
                    await asyncio.sleep(60.0)
                    used = service.app.state.store.voice_seconds(
                        identity.user_id, datetime.now(UTC).date().isoformat())
                    if used + (time.monotonic() - started) < limit:
                        continue
                    mark("voiceDailyLimit", usedSeconds=used, limitSeconds=limit)
                    await to_client({"type": "text", "delta": "今天的语音时长用完了。"})
                    await speak("今天的语音时长用完了。")
                    await line_played(HANGUP_AUDIO_WAIT_SECONDS)
                    hangup.set()
                    return

            async def session_answered(ready: asyncio.Event, gone: asyncio.Event, deadline: float) -> bool:
                """A replacement session sent session.created before `deadline` and is still there — not one that
                closed first, nor one that sent created and closed in the same breath (#912 review: its reader had set
                both, the loss was ignored while swapping, and the call was handed to a dead session)."""
                waiters = [asyncio.ensure_future(ready.wait()), asyncio.ensure_future(gone.wait())]
                try:
                    await asyncio.wait(waiters, timeout=max(0.0, deadline - time.monotonic()), return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for waiter in waiters:
                        waiter.cancel()
                return ready.is_set() and not gone.is_set()

            async def create_session(deadline: float) -> bool:
                """session.create sent within the candidate's one bound: the handshake, this send and session.created
                share it (#912 review: a send that never returned held the recovery and its lines for good)."""
                try:
                    return await asyncio.wait_for(to_upstream(session_create(service.dialog_context(state))),
                                                  timeout=max(0.0, deadline - time.monotonic()))
                except TimeoutError:
                    return False

            async def recover_upstream() -> None:
                """上游看门狗 (10-15): the Doubao session stopped answering (no session.created, or nothing at all
                after a commit) or was lost (closed, its reader ended, a send failed). A new session — carrying the
                conversation's recent turns as its context — replaces it, and the lines the user never heard are said
                again, in order. The call has UPSTREAM_RECONNECT_ATTEMPTS replacement sessions in all (#904 review: not
                per recovery); when they are used up, the user is told and the call ends: never a silent, dead call."""
                waiting = state.upstream_expect
                lines = list(state.unheard)
                state.unheard.clear()
                state.upstream_expect, state.upstream_expect_by = "", 0.0
                state.reconnecting = True
                mark("upstreamStalled", waiting=waiting, gen=link["gen"])
                attempts = max(0, UPSTREAM_RECONNECT_ATTEMPTS - state.reconnects)
                await to_client({"type": "status", "state": "connecting"})
                stalled_reader = link["reader"]
                if stalled_reader is not None:
                    stalled_reader.cancel()  # whatever the old session sends later is never played
                asyncio.create_task(close_quietly(link["connection"]))
                # The old session's turn bookkeeping goes with it: nothing is pending on the new one. A round waiting
                # for Doubao's reply to end is released; a line waiting for its audio is not — it is said again below
                # and its audio ends it (#904 review: releasing audio_done here ended a goodbye before it was heard).
                state.mute_cancel_pending, state.late_muted_response, state.cancel_sent_at = False, None, 0.0
                state.cancel_done_owed = False
                state.speaking, state.current_output_id, state.asr_open = False, None, False
                if state.routed is not None:
                    state.routed["response_done"].set()
                for attempt in range(attempts):
                    state.reconnects += 1
                    await asyncio.sleep(UPSTREAM_RECONNECT_BACKOFF_SECONDS[min(attempt, len(UPSTREAM_RECONNECT_BACKOFF_SECONDS) - 1)])
                    # The handshake and session.created share one bound (#904 review: each had its own).
                    deadline = time.monotonic() + SESSION_CREATED_WAIT_SECONDS
                    connection, ready, gone = dial(), asyncio.Event(), asyncio.Event()
                    try:
                        ws = await asyncio.wait_for(connection.__aenter__(), timeout=SESSION_CREATED_WAIT_SECONDS)
                    except Exception as error:
                        mark("upstreamReconnectFailed", attempt=attempt + 1, error=type(error).__name__)
                        continue
                    connections.append(connection)
                    link.update(ws=ws, connection=connection, ready=ready, gen=link["gen"] + 1, calls=set(), lost=False)
                    link["reader"] = spawn(run_reader(ws, ready, gone, link["gen"]))
                    taken = await create_session(deadline) and await session_answered(ready, gone, deadline)
                    if taken:
                        await to_client({"type": "status", "state": "listening", "sessionId": state.session_id})
                        # Closed while the client was told: while swapping a loss is not reported (upstream_lost),
                        # so it is checked here, with no await before the swap ends (#912 review).
                        taken = not gone.is_set()
                    if not taken:
                        mark("upstreamReconnectFailed", attempt=attempt + 1, error="closed" if gone.is_set() else "no_session")
                        link["reader"].cancel()
                        asyncio.create_task(close_quietly(connection))
                        continue
                    mark("upstreamReconnected", attempt=attempt + 1, gen=link["gen"], respeak=bool(lines))
                    state.reconnecting = False
                    if mic.muted:
                        await to_upstream({"type": "input_audio_mute.commit"})  # pressed during the swap
                    if not link["opened"]:
                        link["opened"] = True
                        spawn(open_review())
                    for line in lines:
                        # First on the new session, in their order: lines queued behind the swap follow them. A line
                        # keeps its own retry: it is said on a live session at most twice in all.
                        await speak(line["text"], retry=line["retried"])
                    return
                state.gave_up, state.reconnecting = True, False
                mark("upstreamGaveUp", attempts=state.reconnects, gen=link["gen"])
                await to_client({"type": "error", "error": {"message": UPSTREAM_GIVE_UP_LINE}})
                hangup.set()

            async def upstream_watchdog() -> None:
                while not state.gave_up:
                    await asyncio.sleep(UPSTREAM_WATCH_TICK_SECONDS)
                    if not state.upstream_expect_by or time.monotonic() <= state.upstream_expect_by or state.reconnecting:
                        continue
                    try:
                        waiting = list(state.unheard) if state.upstream_expect == "commit" else []
                        if (waiting and not any(line["retried"] for line in waiting) and not link["lost"]
                                and state.upstream_seen_at > waiting[0]["at"]):
                            # The session is alive but swallowed the line (10-15 d5902f9b: committed while Doubao
                            # still held the sentence open, answered with a bare response.done): say it once more
                            # on the same session before replacing it — with every line committed after it, in
                            # order. Doubao reads commits in order and an audio start is taken as the oldest line's:
                            # a retry queued behind a later line still waiting took that line's place, and the
                            # later line was never said again (#912 review). A later line that was only slow is
                            # heard twice at worst, never lost.
                            mark("speakRetry", chars=sum(len(line["text"]) for line in waiting), lines=len(waiting))
                            state.unheard.clear()
                            watch_lines()
                            for line in waiting:
                                await speak(line["text"], retry=True)
                            continue
                        await recover_upstream()
                    except asyncio.CancelledError:
                        raise
                    except Exception as error:  # the watchdog outlives one failed retry or recovery (#905 review)
                        log.warning("voice call %s upstream recovery failed: %s: %s", call_id, type(error).__name__, error)
                        state.reconnecting = False

            link["reader"] = spawn(run_reader(link["ws"], link["ready"], asyncio.Event(), 0))
            spawn(announcer_guarded())
            spawn(endpoint_watchdog())
            spawn(quote_watchdog())
            spawn(voice_budget_watchdog())
            spawn(upstream_watchdog())
            try:
                client_task = asyncio.ensure_future(client_reader())
                hangup_task = asyncio.ensure_future(hangup.wait())
                done, _ = await asyncio.wait({client_task, hangup_task}, return_when=asyncio.FIRST_COMPLETED)
                hangup_task.cancel()
                if client_task in done:
                    client_task.result()
                else:
                    client_task.cancel()
                    if state.gave_up:
                        mark("hangupByStall")
                        log.info("voice call %s ended: the voice channel did not answer", call_id)
                    else:
                        mark("hangupByVoice")
                        log.info("voice call %s hung up by voice", call_id)
            except WebSocketDisconnect:
                log.info("voice call %s client disconnected", call_id)
            finally:
                # #523 R1c: nothing accepted on this call is published, spoken or recorded after hang-up — and nothing
                # it started goes on: its rounds, tool handlers and watchdogs end here (the session's reader stays for
                # the close acknowledgement). Business calls they waited on finish on their own (`business`).
                state.closed = True
                state.upstream_expect_by = 0.0
                owned = [task for task in state.tasks if task is not link["reader"]]
                for task in owned:
                    task.cancel()
                if owned:
                    await asyncio.wait(owned, timeout=1.0)
                await service._abandon_pending(state)  # a call that ends mid-confirmation leaves no open card behind
                if state.action is not None and not state.action.get("armed"):
                    try:  # an order card still being made when the call ended is dropped unseen
                        # shielded: past the wait the drop keeps running until the card tool returns and is rejected
                        await asyncio.wait_for(asyncio.shield(asyncio.ensure_future(drop_unread(state.action))), timeout=5.0)
                    except Exception as error:
                        log.warning("voice call %s unread action card not dropped: %s", call_id, error)
                release_slot()
                if not (link["lost"] or state.gave_up or state.reconnecting) and await to_upstream({"type": "session.close", "event_id": "evt_close"}):
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(stop.wait(), SESSION_CLOSE_WAIT_SECONDS)
                for task in list(state.tasks):
                    task.cancel()
    except Exception as error:
        log.warning("voice call %s ended with %s: %s", call_id, type(error).__name__, error)
        await to_client({"type": "error", "error": {"message": type(error).__name__}})
    finally:
        duration = release_slot()
        # #523 R4: the one-line audio accounting of the whole call — frames the provider delivered
        # vs the ones the Runtime dropped vs the ones whose client WebSocket send completed. This
        # mark logs on its own line right here, so it stays readable even when the one-line
        # timeline JSON further down is cut at its existing 4000-char prefix (a long call's tail is
        # not fully readable from that line). Each mark is logged independently; a client that
        # actively disconnects may miss the closing summary and this final mark in its export.
        # The uplink fields ride the same line: micFrames 0 = the client sent no audio; frames with a
        # micRmsPeak of 0 or a few = it sent digital silence (a muted or wrong input device). silentRatio is
        # only a hint: browser noise suppression flattens the pauses, a normal call reads about 0.8 (#875).
        # micFirstAt counts from when client_reader starts, so frames queued while the session opened read late.
        mark("audioPath", **audio_diag, **mic.summary())
        summary = {"type": "closed", "durationSeconds": duration, "turns": len(state.usage),
                   "usage": {"total_tokens": sum(int(u.get("total_tokens") or 0) for u in state.usage),
                             "input_tokens": sum(int(u.get("input_tokens") or 0) for u in state.usage),
                             "output_tokens": sum(int(u.get("output_tokens") or 0) for u in state.usage)},
                   "latency": state.latency, "tools": state.tool_log, "timeline": state.timeline}
        # One line per call for the server log (who hung up vs a failure) plus the turn timeline
        # (ASR final → audio) for the "said something, nothing happened" question.
        log.info("voice call %s closed after %.1fs turns=%d tools=%s", call_id, summary["durationSeconds"], summary["turns"],
                 ",".join(f"{t.get('name')}:{t.get('elapsedSeconds')}s" for t in state.tool_log) or "-")
        log.info("voice call %s timeline %s", call_id, json.dumps(state.timeline, ensure_ascii=False)[:4000])
        if not state.gave_up:  # the channel did not answer: the client keeps showing that error, not standby
            await to_client(summary)
        try:
            await client.close()
        except Exception:
            pass
