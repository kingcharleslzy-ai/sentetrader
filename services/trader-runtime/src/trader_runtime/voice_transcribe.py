"""Push-to-talk transcription (按住说话, `/api/voice/transcribe`).

The composer's microphone: the client streams PCM16/16 kHz frames while the key or button is held,
sends `{"type": "end"}` on release (or `{"type": "cancel"}`), and gets `partial` lines and one
`final` back. The Runtime opens a Doubao session with the call's recognizer settings (hotwords,
replacement words, two-pass) but no tools and a transcribe-only instruction; Doubao's replies are
cancelled at their first event (`response.cancel`, once per reply) so no speech is generated or
billed, and the upstream closes as soon as the final is known. The text is then sent by the client
as an ordinary typed message: nothing here routes, answers or trades.

Doubao 3.0 facts this relies on (measured 09-24, release artifacts voice-asr-ab-20260924; the ghost
`started` at a reply's start 09-28):
`session.created` takes 1–2 s (frames held before it are buffered and replayed in order);
`input_audio_buffer.commit` forces the endpoint; the uplink must keep flowing after it or the
completed event never comes, so a noise floor is streamed until it does.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import math
import random
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from fastapi import WebSocket

from .voice_realtime import DOUBAO_URL, MODEL, VOICE, CallIdentity, asr_settings, clean_transcript

if TYPE_CHECKING:
    from .voice_realtime import VoiceRealtimeService

log = logging.getLogger("uvicorn.error.voice")

INSTRUCTIONS = "你只做语音转写。不要回答、不要说话、不要调用任何工具。"
# After the release: how long the final may take, and the pause between two noise-floor frames.
FINAL_TIMEOUT_SECONDS = 6.0
PAD_INTERVAL_SECONDS = 0.1
# The held speech already ended in a final of its own (a pause before the release): the
# forced endpoint gets this long to show new speech before those finals are the answer.
SETTLED_GRACE_SECONDS = 1.0
# Nothing was heard during the whole hold (no transcription started / delta): after the release the
# forced endpoint gets this long to show speech, then the answer is an empty final `no_speech` —
# a silent hold no longer waits the full FINAL_TIMEOUT_SECONDS (09-25: 1 s of silence took 6.4 s).
# Counted from when the held audio reached Doubao (the release, or session.created if that came
# later), so a short word held before the session existed is still transcribed.
NO_SPEECH_GRACE_SECONDS = 1.5
# A key stuck down (or a client that never says `end`) stops here as if released.
MAX_HOLD_SECONDS = 90.0
PAD_FRAME_SAMPLES = 1600  # 100 ms at 16 kHz
# A reply is over (its own end or our cancel took effect): the next reply event gets its own cancel.
REPLY_ENDED = frozenset({"response.done", "response.canceled", "response.cancelled"})


def _noise_floor(samples: int = PAD_FRAME_SAMPLES) -> bytes:
    """100 ms of quiet noise (σ≈30 of 32767, like a room), PCM16 little-endian."""
    values = [max(-32768, min(32767, int(random.gauss(0, 30)))) for _ in range(samples)]
    return b"".join(value.to_bytes(2, "little", signed=True) for value in values)


async def run_transcribe(service: VoiceRealtimeService, client: WebSocket, key: str, identity: CallIdentity, *, meter: bool = True) -> None:
    import websockets

    session = uuid4().hex[:8]
    started = time.monotonic()
    created = asyncio.Event()
    finished = asyncio.Event()   # a completed (after the release), a failure or the upstream closing
    speech = asyncio.Event()     # new speech began after the release (started / delta)
    audio_lock = asyncio.Lock()
    buffered: list[bytes] = []
    finals: list[str] = []
    # `reply`: the Doubao reply being cancelled right now (its id, "" when unnamed), None between replies.
    state: dict[str, Any] = {"open": False, "released": False, "reason": None, "ended_by": "end", "reply": None,
                             "heard": False, "created_at": None}

    async def to_client(payload: dict[str, Any]) -> None:
        try:
            await client.send_text(json.dumps(payload, ensure_ascii=False))
        except Exception:
            pass

    try:
        asr_context, asr_extra = await asr_settings(service)
        log.info("voice ptt %s started user=%s hotwords=%d twopass=%s", session, identity.user_id,
                 len(asr_context["hotwords"]), bool(asr_extra.get("enable_asr_twopass")))
        async with websockets.connect(DOUBAO_URL, additional_headers={"X-Api-Key": key}, max_size=8 * 1024 * 1024,
                                      ping_interval=20, ping_timeout=90) as upstream:
            async def to_upstream(payload: dict[str, Any]) -> None:
                await upstream.send(json.dumps(payload, ensure_ascii=False))

            async def send_audio(frame: bytes | None) -> None:
                """Frames held before `session.created` are replayed first, in order."""
                async with audio_lock:
                    if not created.is_set():
                        if frame:
                            buffered.append(frame)
                        return
                    while buffered:
                        await to_upstream({"type": "input_audio_buffer.append", "audio": base64.b64encode(buffered.pop(0)).decode()})
                    if frame:
                        await to_upstream({"type": "input_audio_buffer.append", "audio": base64.b64encode(frame).decode()})

            await to_upstream({"type": "session.create", "event_id": f"evt_ptt_{session}", "session": {
                "model": MODEL, "instructions": INSTRUCTIONS,
                "audio": {"input": {"format": {"type": "pcm", "rate": 16000}},
                          "output": {"format": {"type": "pcm", "rate": 24000}, "speed": 0, "loudness": 0, "voice": VOICE}},
                "tools": []},
                "extension": {"asr": {"extra": asr_extra} if asr_extra else {}, "tts": {}, "dialog": {}}})

            async def upstream_reader() -> None:
                try:
                    async for raw in upstream:
                        try:
                            event = json.loads(raw)
                        except Exception:
                            continue
                        kind = event.get("type")
                        if kind == "session.created":
                            state["created_at"] = time.monotonic()
                            created.set()
                            await send_audio(None)
                        elif kind == "conversation.item.input_audio_transcription.started":
                            # Something was heard (the no-speech shortcut no longer applies), but a
                            # segment is open only once words arrive: Doubao also sends `started` when a
                            # reply begins (the ghost start, 09-28). After the reply to a mid-hold
                            # sentence it held the release for the full FINAL_TIMEOUT (6.05 s, 10-08)
                            # waiting for a completed that never came.
                            state["heard"] = True
                        elif kind == "conversation.item.input_audio_transcription.delta":
                            state["open"] = True
                            state["heard"] = True
                            if state["released"]:
                                speech.set()
                            # Doubao's delta carries the utterance so far; earlier finals of the same hold lead it.
                            await to_client({"type": "partial", "text": "".join(finals) + clean_transcript(str(event.get("delta") or ""))})
                        elif kind == "conversation.item.input_audio_transcription.completed":
                            state["open"] = False
                            text = clean_transcript(str(event.get("text") or event.get("transcript") or "")).strip()
                            if text:
                                finals.append(text)
                                await to_client({"type": "partial", "text": "".join(finals)})
                            if state["released"]:
                                finished.set()
                        elif kind == "conversation.item.input_audio_transcription.failed":
                            state["open"] = False
                            if state["released"]:
                                state["reason"] = state["reason"] or "failed"
                                finished.set()
                        elif kind == "error":
                            log.warning("voice ptt %s upstream error: %s", session, json.dumps(event, ensure_ascii=False)[:300])
                            # Fatal only before the session exists; later errors (e.g. a reply that ended
                            # before our cancel arrived) must not cut the wait for the final short.
                            if not created.is_set():
                                state["reason"] = "upstream_error"
                                finished.set()
                        elif kind == "session.closed":
                            break
                        elif kind in REPLY_ENDED:
                            state["reply"] = None
                        elif isinstance(kind, str) and (kind.startswith("response.") or "output_audio" in kind):
                            # Doubao answers every endpointed sentence; a transcription wants none of it.
                            # The first event of each reply is cancelled once (response.cancel, 3.0) so
                            # no speech is generated or billed; its later events are ignored.
                            reply_id = str((event.get("response") or {}).get("id") or event.get("response_id") or "")
                            if state["reply"] is None or (reply_id and state["reply"] and reply_id != state["reply"]):
                                state["reply"] = reply_id
                                log.info("voice ptt %s cancels reply %s (%s)", session, reply_id or "-", kind)
                                await to_upstream({"type": "response.cancel"})
                finally:
                    if state["reason"] is None and not finished.is_set():
                        state["reason"] = "upstream_closed"
                    finished.set()

            reader = asyncio.create_task(upstream_reader())
            try:
                # Hold: forward frames until `end` (or `cancel` / a disconnect, which end with no result).
                deadline = started + MAX_HOLD_SECONDS
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        state["ended_by"] = "max_hold"
                        break
                    try:
                        message = await asyncio.wait_for(client.receive(), timeout=remaining)
                    except TimeoutError:
                        state["ended_by"] = "max_hold"
                        break
                    if message.get("type") == "websocket.disconnect":
                        state["ended_by"] = "disconnect"
                        return
                    if state["reason"] in {"upstream_error", "upstream_closed"}:
                        break  # the upstream is gone: answer now instead of after the release
                    if message.get("bytes"):
                        await send_audio(message["bytes"])
                        continue
                    try:
                        command = json.loads(message.get("text") or "")
                    except json.JSONDecodeError:
                        continue
                    kind = command.get("type") if isinstance(command, dict) else None
                    if kind == "cancel":
                        state["ended_by"] = "cancel"
                        return
                    if kind == "end":
                        break
                # Release: force the endpoint and keep the uplink flowing until the final arrives.
                release_at = time.monotonic()
                state["released"] = True
                heard_while_held = bool(state["heard"])
                final_deadline = release_at + FINAL_TIMEOUT_SECONDS
                if not finished.is_set():
                    try:
                        await asyncio.wait_for(created.wait(), timeout=max(0.0, final_deadline - time.monotonic()))
                    except TimeoutError:
                        pass
                if created.is_set() and not finished.is_set():
                    await send_audio(None)
                    await to_upstream({"type": "input_audio_buffer.commit"})
                    # The hold already ended in a final and nothing is being heard now: the commit gets a
                    # short grace to show new speech; otherwise wait for the final up to the timeout.
                    settled = bool(finals) and not state["open"]
                    while not finished.is_set():
                        now = time.monotonic()
                        if now >= final_deadline:
                            break
                        if settled and not speech.is_set() and now - release_at >= SETTLED_GRACE_SECONDS:
                            break
                        audible_since = max(release_at, state["created_at"] or release_at)
                        if (not heard_while_held and not state["heard"] and not finals
                                and now - audible_since >= NO_SPEECH_GRACE_SECONDS):
                            state["reason"] = state["reason"] or "no_speech"
                            break
                        await send_audio(_noise_floor())
                        try:
                            await asyncio.wait_for(finished.wait(), timeout=PAD_INTERVAL_SECONDS)
                        except TimeoutError:
                            pass
                text = "".join(finals)
                reason = None if text else (state["reason"] or ("empty" if finished.is_set() else "timeout"))
                log.info("voice ptt %s final chars=%d after_release=%.2fs reason=%s", session, len(text),
                         time.monotonic() - release_at, reason or "-")
                await to_client({"type": "final", "text": text, **({"reason": reason} if reason else {})})
            finally:
                reader.cancel()
                try:
                    await to_upstream({"type": "session.close", "event_id": f"evt_ptt_close_{session}"})
                except Exception:
                    pass
    except Exception as error:
        log.warning("voice ptt %s ended with %s: %s", session, type(error).__name__, error)
        await to_client({"type": "final", "text": "", "reason": "upstream_error"})
    finally:
        duration = time.monotonic() - started
        if meter and not service.local_mode:
            # The same daily voice meter as a call; a transcription that happened costs at least a second.
            service.app.state.store.add_voice_seconds(
                identity.user_id, datetime.now(UTC).date().isoformat(), max(1, math.ceil(duration)))
        log.info("voice ptt %s closed after %.1fs by=%s", session, duration, state["ended_by"])
        try:
            await client.close()
        except Exception:
            pass
