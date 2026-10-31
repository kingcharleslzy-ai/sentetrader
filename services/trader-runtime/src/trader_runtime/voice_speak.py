"""Read an answer aloud (`/api/voice/speak`, 09-28): the voice reply of a push-to-talk message.

按住说话 sends the held words as a chat message with origin "voice"; the agent answers with a short
spoken summary, which the client reads out here in the call's own voice and speed. The client opens
the socket right after sending (Doubao's session takes 1–2 s to start, hidden behind the agent's
turn), then sends `{"type": "speak", "text": ...}` once the answer arrives, or `{"type": "cancel"}`.
The Runtime says the text through Doubao's "打招呼" event (`speech_text_buffer.commit`, measured
09-28: first audio ~0.5 s after the commit, no microphone audio needed) and relays the audio frames
exactly like a call (`audio` with its encoding, then `audio_end`). Nothing here routes or trades.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from fastapi import WebSocket

from .voice_realtime import DOUBAO_URL, MODEL, OUTPUT_ENCODING, SPEECH_SPEEDS, CallIdentity

if TYPE_CHECKING:
    from .voice_realtime import VoiceRealtimeService

log = logging.getLogger("uvicorn.error.voice")

INSTRUCTIONS = "你只朗读系统给你的文字，不回答、不调用工具。"
# The agent's turn the socket waits through, and the longest text read in one go.
WAIT_TEXT_SECONDS = 150.0
MAX_CHARS = 400
AUDIO_TIMEOUT_SECONDS = 30.0


async def run_speak(service: VoiceRealtimeService, client: WebSocket, key: str, identity: CallIdentity) -> None:
    import websockets

    session = uuid4().hex[:8]
    started = time.monotonic()
    created = asyncio.Event()
    done = asyncio.Event()
    state: dict[str, Any] = {"seconds": 0.0, "ended_by": "done"}

    async def to_client(payload: dict[str, Any]) -> None:
        try:
            await client.send_text(json.dumps(payload, ensure_ascii=False))
        except Exception:
            pass

    try:
        preferences = service.app.state.store.user_preferences(identity.user_id)
        async with websockets.connect(DOUBAO_URL, additional_headers={"X-Api-Key": key}, max_size=8 * 1024 * 1024,
                                      ping_interval=20, ping_timeout=90) as upstream:
            async def to_upstream(payload: dict[str, Any]) -> None:
                await upstream.send(json.dumps(payload, ensure_ascii=False))

            await to_upstream({"type": "session.create", "event_id": f"evt_speak_{session}", "session": {
                "model": MODEL, "instructions": INSTRUCTIONS,
                "audio": {"input": {"format": {"type": "pcm", "rate": 16000}},
                          "output": {"format": {"type": "pcm", "rate": 24000}, "speed": SPEECH_SPEEDS[preferences.voice.speed],
                                     "loudness": 0, "voice": service.voice_for(identity.user_id)}},
                "tools": []},
                "extension": {"asr": {}, "tts": {}, "dialog": {}}})

            async def upstream_reader() -> None:
                try:
                    async for raw in upstream:
                        try:
                            event = json.loads(raw)
                        except Exception:
                            continue
                        kind = event.get("type")
                        if kind == "session.created":
                            created.set()
                        elif kind == "response.output_audio.delta":
                            state["seconds"] += len(event.get("delta") or "") * 3 / 4 / (4 * 24000)
                            await to_client({"type": "audio", "data": event.get("delta"), "encoding": OUTPUT_ENCODING})
                        elif kind == "response.output_audio.done":
                            done.set()
                        elif kind == "error":
                            log.warning("voice speak %s upstream error: %s", session, json.dumps(event, ensure_ascii=False)[:300])
                            done.set()
                        elif kind == "session.closed":
                            break
                finally:
                    done.set()

            reader = asyncio.create_task(upstream_reader())
            try:
                text = ""
                try:
                    while not text:
                        message = await asyncio.wait_for(client.receive(), timeout=WAIT_TEXT_SECONDS)
                        if message.get("type") == "websocket.disconnect":
                            state["ended_by"] = "disconnect"
                            return
                        try:
                            command = json.loads(message.get("text") or "")
                        except json.JSONDecodeError:
                            continue
                        if not isinstance(command, dict) or command.get("type") == "cancel":
                            state["ended_by"] = "cancel"
                            return
                        if command.get("type") == "speak":
                            text = str(command.get("text") or "").strip()[:MAX_CHARS]
                            if not text:
                                state["ended_by"] = "empty"
                                return
                except TimeoutError:
                    state["ended_by"] = "no_text"
                    return
                await asyncio.wait_for(created.wait(), timeout=AUDIO_TIMEOUT_SECONDS)
                await to_upstream({"type": "speech_text_buffer.commit", "text": text})
                try:
                    await asyncio.wait_for(done.wait(), timeout=AUDIO_TIMEOUT_SECONDS)
                except TimeoutError:
                    state["ended_by"] = "audio_timeout"
                await to_client({"type": "audio_end"})
            finally:
                reader.cancel()
                try:
                    await to_upstream({"type": "session.close", "event_id": f"evt_speak_close_{session}"})
                except Exception:
                    pass
    except Exception as error:
        state["ended_by"] = type(error).__name__
        log.warning("voice speak %s ended with %s: %s", session, type(error).__name__, error)
    finally:
        if not service.local_mode:
            # The same daily voice meter as a call: the seconds read out, at least one.
            service.app.state.store.add_voice_seconds(
                identity.user_id, datetime.now(UTC).date().isoformat(), max(1, math.ceil(state["seconds"])))
        log.info("voice speak %s closed after %.1fs audio=%.1fs by=%s", session, time.monotonic() - started,
                 state["seconds"], state["ended_by"])
        try:
            await client.close()
        except Exception:
            pass
