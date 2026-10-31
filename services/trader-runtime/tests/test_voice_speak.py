"""按住说话的语音回复 `/api/voice/speak` (09-28): the agent's spoken summary is read out in the
call's voice and speed through Doubao's 打招呼 event, relayed like a call's audio."""
from __future__ import annotations

import asyncio
import json
import sys
import types

from test_voice_realtime import _voice_app
from trader_runtime import voice_speak
from trader_runtime.voice_realtime import CallIdentity


def _scripted(monkeypatch):
    upstream_sent: list[dict] = []
    to_model: asyncio.Queue = asyncio.Queue()

    async def on_send(payload):
        kind = payload["type"]
        if kind == "session.create":
            await to_model.put({"type": "session.created", "session": {"id": "s1"}})
        elif kind == "speech_text_buffer.commit":
            await to_model.put({"type": "response.output_audio.started", "tts_type": "chat_tts_text"})
            await to_model.put({"type": "response.output_audio.delta", "delta": "QUJD"})
            await to_model.put({"type": "response.output_audio.done"})
        elif kind == "session.close":
            await to_model.put(None)

    class FakeUpstream:
        async def send(self, raw: str) -> None:
            payload = json.loads(raw)
            upstream_sent.append(payload)
            await on_send(payload)

        def __aiter__(self):
            return self

        async def __anext__(self):
            event = await to_model.get()
            if event is None:
                raise StopAsyncIteration
            return json.dumps(event)

    class Connect:
        def __init__(self, *a, **k): ...
        async def __aenter__(self): return FakeUpstream()
        async def __aexit__(self, *a): return False
    monkeypatch.setitem(sys.modules, "websockets", types.SimpleNamespace(connect=Connect))
    client_out: list[dict] = []
    inbound: asyncio.Queue = asyncio.Queue()

    class FakeClient:
        async def send_text(self, text: str) -> None:
            client_out.append(json.loads(text))
        async def receive(self) -> dict:
            return await inbound.get()
        async def close(self) -> None: ...

    async def run(service, timeout=10):
        await asyncio.wait_for(voice_speak.run_speak(service, FakeClient(), "key", CallIdentity("local-user", {})), timeout=timeout)
    return upstream_sent, client_out, inbound, run


async def test_the_spoken_summary_is_read_out_in_the_call_voice_and_speed(tmp_path, monkeypatch):
    app, _, service = await _voice_app(tmp_path, "speak.sqlite3")
    store = app.state.store
    store.update_preferences("local-user", {"voice": {"speed": "fast"}})
    upstream_sent, client_out, inbound, run = _scripted(monkeypatch)
    await inbound.put({"type": "websocket.receive", "text": json.dumps({"type": "speak", "text": "螺纹现在 3100。"})})
    await run(service)

    created = next(p for p in upstream_sent if p["type"] == "session.create")
    assert created["session"]["tools"] == [] and created["session"]["audio"]["output"]["speed"] == 30
    assert [p["text"] for p in upstream_sent if p["type"] == "speech_text_buffer.commit"] == ["螺纹现在 3100。"]
    assert client_out == [{"type": "audio", "data": "QUJD", "encoding": "f32le"}, {"type": "audio_end"}]
    store.close()


async def test_a_cancelled_or_abandoned_reply_says_nothing(tmp_path, monkeypatch):
    app, _, service = await _voice_app(tmp_path, "speak-cancel.sqlite3")
    upstream_sent, client_out, inbound, run = _scripted(monkeypatch)
    await inbound.put({"type": "websocket.receive", "text": json.dumps({"type": "cancel"})})
    await run(service)
    assert not any(p["type"] == "speech_text_buffer.commit" for p in upstream_sent) and client_out == []

    upstream_sent, client_out, inbound, run = _scripted(monkeypatch)
    await inbound.put({"type": "websocket.disconnect"})
    await run(service)
    assert not any(p["type"] == "speech_text_buffer.commit" for p in upstream_sent) and client_out == []
    app.state.store.close()
