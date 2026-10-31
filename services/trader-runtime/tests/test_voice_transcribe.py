"""按住说话 `/api/voice/transcribe`: transcription only, with the call's recognizer settings, the
same identity / skill / daily-minute rules, never while the account is in a call."""
from __future__ import annotations

import asyncio
import json
import sys
import types
from datetime import UTC, datetime

import pytest

from test_voice_realtime import _voice_app
from trader_runtime import voice_transcribe
from trader_runtime.main import create_app
from trader_runtime.voice_realtime import CallIdentity, CallState, VoiceRealtimeService

ORIGIN = "http://127.0.0.1:5173"
LEAK = '<FunctionCall>| "name : "getMusicEntity, "parameters : "content :"螺纹钢]| <FunctionCall>| 买两手。'


def _scripted(monkeypatch, on_send):
    """A fake Doubao (`on_send(payload, emit)`) and a fake composer socket around one run_transcribe."""
    upstream_sent: list[dict] = []
    to_model: asyncio.Queue = asyncio.Queue()

    class FakeUpstream:
        async def send(self, raw: str) -> None:
            payload = json.loads(raw)
            upstream_sent.append(payload)
            await on_send(payload, to_model.put)

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
    closed: list[bool] = []

    class FakeClient:
        async def send_text(self, text: str) -> None:
            client_out.append(json.loads(text))
        async def receive(self) -> dict:
            return await inbound.get()
        async def close(self) -> None:
            closed.append(True)

    async def run(service, timeout=10):
        await asyncio.wait_for(voice_transcribe.run_transcribe(service, FakeClient(), "key", CallIdentity("local-user", {})), timeout=timeout)
    return upstream_sent, client_out, inbound, run, closed


def _appended(sent: list[dict]) -> list[str]:
    return [p["audio"] for p in sent if p["type"] == "input_audio_buffer.append"]


def _b64(frame: bytes) -> str:
    import base64
    return base64.b64encode(frame).decode()


async def _later(seconds, emit, event):
    await asyncio.sleep(seconds)
    await emit(event)


async def test_frames_before_session_created_are_replayed_in_order_and_the_final_is_cleaned(tmp_path, monkeypatch):
    app, _, service = await _voice_app(tmp_path, "ptt-order.sqlite3")
    created_at: list[float] = []

    async def on_send(payload, emit):
        kind = payload["type"]
        if kind == "session.create":
            asyncio.create_task(_later(0.3, emit, {"type": "session.created", "session": {"id": "s1"}}))
            created_at.append(0)
        elif kind == "input_audio_buffer.commit":
            await emit({"type": "conversation.item.input_audio_transcription.delta", "delta": "螺纹钢买"})
            asyncio.create_task(_later(0.35, emit, {"type": "conversation.item.input_audio_transcription.completed", "text": LEAK}))
            # Doubao's own reply events are ignored.
            asyncio.create_task(_later(0.1, emit, {"type": "response.output_audio.delta", "delta": "QUJD"}))
    upstream_sent, client_out, inbound, run, closed = _scripted(monkeypatch, on_send)
    frames = [bytes([n]) * 640 for n in (1, 2, 3, 4)]

    async def composer():
        for frame in frames[:3]:
            await inbound.put({"type": "websocket.receive", "bytes": frame})
        for _ in range(50):  # the fourth frame only after Doubao is ready
            await asyncio.sleep(0.05)
            if any(p["type"] == "input_audio_buffer.append" for p in upstream_sent):
                break
        await inbound.put({"type": "websocket.receive", "bytes": frames[3]})
        await inbound.put({"type": "websocket.receive", "text": json.dumps({"type": "end"})})
    asyncio.create_task(composer())
    await run(service)

    created = next(p for p in upstream_sent if p["type"] == "session.create")
    # Transcribe only: no tools, the call's recognizer settings (hotwords, replacement words, two-pass).
    assert created["session"]["tools"] == [] and "只做语音转写" in created["session"]["instructions"]
    extra = created["extension"]["asr"]["extra"]
    assert extra["enable_asr_twopass"] is True and extra["context"]["correct_words"]["全屏"] == "全平"
    appended = _appended(upstream_sent)
    assert appended[:4] == [_b64(frame) for frame in frames]
    commit = [p["type"] for p in upstream_sent].index("input_audio_buffer.commit")
    # After the commit the uplink keeps flowing (a noise floor) until the final comes.
    pads = [p for p in upstream_sent[commit + 1:] if p["type"] == "input_audio_buffer.append"]
    assert len(pads) >= 2 and all(len(p["audio"]) > 1000 for p in pads)
    assert {"type": "partial", "text": "螺纹钢买"} in client_out
    assert client_out[-1] == {"type": "final", "text": "螺纹钢买两手。"}
    assert not any(e.get("type") not in {"partial", "final"} for e in client_out)
    # The upstream is closed as soon as the final is known; the composer socket too.
    assert upstream_sent[-1]["type"] == "session.close" and closed
    app.state.store.close()


async def test_doubaos_reply_is_cancelled_once_per_response_and_the_final_is_unaffected(tmp_path, monkeypatch):
    """Doubao answers each endpointed sentence; the transcription cancels the reply at its first event
    (`response.cancel`, 3.0) so no speech is generated or billed — once per response."""
    app, _, service = await _voice_app(tmp_path, "ptt-reply.sqlite3")

    async def on_send(payload, emit):
        kind = payload["type"]
        if kind == "session.create":
            await emit({"type": "session.created", "session": {"id": "s1"}})
        elif kind == "input_audio_buffer.append" and payload["audio"] == _b64(b"\x07" * 640):
            # Doubao's own endpoint mid-hold: the words, then it starts answering (two events of one reply).
            await emit({"type": "conversation.item.input_audio_transcription.completed", "text": "螺纹钢"})
            await emit({"type": "response.created", "response": {"id": "r1"}})
            await emit({"type": "response.output_audio.delta", "response_id": "r1", "delta": "QUJD"})
        elif kind == "input_audio_buffer.commit":
            await emit({"type": "conversation.item.input_audio_transcription.started"})
            await emit({"type": "conversation.item.input_audio_transcription.completed", "text": "买两手。"})
            # The next sentence's reply starts right after the final: a new response, a new cancel.
            await emit({"type": "response.created", "response": {"id": "r2"}})
    upstream_sent, client_out, inbound, run, _ = _scripted(monkeypatch, on_send)
    await inbound.put({"type": "websocket.receive", "bytes": b"\x07" * 640})

    async def release():
        for _ in range(50):
            await asyncio.sleep(0.02)
            if any(p["type"] == "response.cancel" for p in upstream_sent):
                break
        await inbound.put({"type": "websocket.receive", "text": json.dumps({"type": "end"})})
    asyncio.create_task(release())
    await run(service)
    kinds = [p["type"] for p in upstream_sent]
    # r1 was cancelled once although two of its events arrived, before the release's commit.
    assert kinds.count("response.cancel") >= 1 and kinds.index("response.cancel") < kinds.index("input_audio_buffer.commit")
    assert kinds[:kinds.index("input_audio_buffer.commit")].count("response.cancel") == 1
    assert client_out[-1] == {"type": "final", "text": "螺纹钢买两手。"}
    app.state.store.close()


async def test_a_final_that_never_comes_answers_empty_with_timeout(tmp_path, monkeypatch):
    app, _, service = await _voice_app(tmp_path, "ptt-timeout.sqlite3")
    monkeypatch.setattr(voice_transcribe, "FINAL_TIMEOUT_SECONDS", 0.6)

    async def on_send(payload, emit):
        if payload["type"] == "session.create":
            await emit({"type": "session.created", "session": {"id": "s1"}})
    upstream_sent, client_out, inbound, run, _ = _scripted(monkeypatch, on_send)
    await inbound.put({"type": "websocket.receive", "bytes": b"\x00" * 640})
    await inbound.put({"type": "websocket.receive", "text": json.dumps({"type": "end"})})
    await run(service)
    assert client_out[-1] == {"type": "final", "text": "", "reason": "timeout"}
    commit = [p["type"] for p in upstream_sent].index("input_audio_buffer.commit")
    assert len([p for p in upstream_sent[commit + 1:] if p["type"] == "input_audio_buffer.append"]) >= 4
    app.state.store.close()


async def test_a_hold_that_already_ended_in_a_final_does_not_wait_for_the_timeout(tmp_path, monkeypatch):
    """The user paused before releasing: Doubao's own endpoint already gave the final; the forced
    endpoint shows no new speech, so the held finals are the answer after a short grace."""
    app, _, service = await _voice_app(tmp_path, "ptt-settled.sqlite3")
    monkeypatch.setattr(voice_transcribe, "SETTLED_GRACE_SECONDS", 0.3)

    async def on_send(payload, emit):
        if payload["type"] == "session.create":
            await emit({"type": "session.created", "session": {"id": "s1"}})
        elif payload["type"] == "input_audio_buffer.append" and payload["audio"] == _b64(b"\x05" * 640):
            await emit({"type": "conversation.item.input_audio_transcription.completed", "text": "沪金多少钱？"})
    _, client_out, inbound, run, _ = _scripted(monkeypatch, on_send)
    await inbound.put({"type": "websocket.receive", "bytes": b"\x05" * 640})

    async def release():
        await asyncio.sleep(0.2)
        await inbound.put({"type": "websocket.receive", "text": json.dumps({"type": "end"})})
    asyncio.create_task(release())
    started = asyncio.get_running_loop().time()
    await run(service)
    assert client_out[-1] == {"type": "final", "text": "沪金多少钱？"}
    assert asyncio.get_running_loop().time() - started < 2.0
    app.state.store.close()


async def test_cancel_closes_at_once_without_a_result(tmp_path, monkeypatch):
    app, _, service = await _voice_app(tmp_path, "ptt-cancel.sqlite3")

    async def on_send(payload, emit):
        if payload["type"] == "session.create":
            await emit({"type": "session.created", "session": {"id": "s1"}})
    upstream_sent, client_out, inbound, run, closed = _scripted(monkeypatch, on_send)
    await inbound.put({"type": "websocket.receive", "bytes": b"\x00" * 640})
    await inbound.put({"type": "websocket.receive", "text": json.dumps({"type": "cancel"})})
    await run(service, timeout=3)
    assert not any(p["type"] == "input_audio_buffer.commit" for p in upstream_sent)
    assert not any(e.get("type") == "final" for e in client_out)
    assert upstream_sent[-1]["type"] == "session.close" and closed
    app.state.store.close()


async def test_voice_seconds_are_booked_to_the_account_in_the_cloud(tmp_path, monkeypatch):
    app, _, service = await _voice_app(tmp_path, "ptt-meter.sqlite3")
    monkeypatch.setattr(VoiceRealtimeService, "local_mode", property(lambda self: False))

    async def on_send(payload, emit):
        if payload["type"] == "session.create":
            await emit({"type": "session.created", "session": {"id": "s1"}})
        elif payload["type"] == "input_audio_buffer.commit":
            await emit({"type": "conversation.item.input_audio_transcription.completed", "text": "确认"})
    _, client_out, inbound, run, _ = _scripted(monkeypatch, on_send)
    today = datetime.now(UTC).date().isoformat()
    before = app.state.store.voice_seconds("local-user", today)
    await inbound.put({"type": "websocket.receive", "text": json.dumps({"type": "end"})})
    await run(service)
    assert client_out[-1]["text"] == "确认"
    assert app.state.store.voice_seconds("local-user", today) == before + 1
    app.state.store.close()


@pytest.mark.parametrize(("headers", "busy", "code"), [
    ({}, False, 4401),                                                     # not signed in
    ({"Sec-WebSocket-Protocol": "ai-trader-token, bearer.wrong"}, False, 4401),
    ({"Sec-WebSocket-Protocol": "ai-trader-token, bearer.voice-test-token"}, True, 4432),  # the account is in a call
])
async def test_the_socket_needs_a_signed_in_caller_who_is_not_in_a_call(tmp_path, headers, busy, code):
    from starlette.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect
    app = create_app(tmp_path / "ptt-auth.sqlite3", runtime_state_root=tmp_path, agent_mode="fake", local_token="voice-test-token")
    (tmp_path / "key").write_text("k"); app.state.voice_realtime.key_file = tmp_path / "key"
    if busy:
        app.state.voice_realtime.calls["busy"] = CallState(user_id="local-user")
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect) as refused:
            with client.websocket_connect("/api/voice/transcribe", headers={"Origin": ORIGIN, **headers}):
                pass
        assert refused.value.code == code
    # A call in progress is refused, but push-to-talk itself never takes the call slot.
    assert app.state.voice_realtime.user_calls("local-user") == (1 if busy else 0)
    app.state.store.close()


async def test_a_silent_hold_answers_no_speech_soon_after_the_release(tmp_path, monkeypatch):
    """09-25: nothing heard during the hold (no transcription started / delta) → an empty final
    `no_speech` within NO_SPEECH_GRACE_SECONDS of the release, not after the full final timeout."""
    app, _, service = await _voice_app(tmp_path, "ptt-silent.sqlite3")
    monkeypatch.setattr(voice_transcribe, "FINAL_TIMEOUT_SECONDS", 6.0)
    monkeypatch.setattr(voice_transcribe, "NO_SPEECH_GRACE_SECONDS", 0.4)

    async def on_send(payload, emit):
        if payload["type"] == "session.create":
            await emit({"type": "session.created", "session": {"id": "s1"}})
    _, client_out, inbound, run, _ = _scripted(monkeypatch, on_send)
    await inbound.put({"type": "websocket.receive", "bytes": b"\x00" * 640})
    await inbound.put({"type": "websocket.receive", "text": json.dumps({"type": "end"})})
    started = asyncio.get_running_loop().time()
    await run(service)
    assert client_out[-1] == {"type": "final", "text": "", "reason": "no_speech"}
    assert asyncio.get_running_loop().time() - started < 1.5
    app.state.store.close()


async def test_speech_heard_during_the_hold_still_waits_for_its_final(tmp_path, monkeypatch):
    """Speech was detected while held: the no-speech shortcut does not apply; a final that comes
    later than the no-speech grace is still the answer."""
    app, _, service = await _voice_app(tmp_path, "ptt-heard.sqlite3")
    monkeypatch.setattr(voice_transcribe, "NO_SPEECH_GRACE_SECONDS", 0.2)

    async def on_send(payload, emit):
        if payload["type"] == "session.create":
            await emit({"type": "session.created", "session": {"id": "s1"}})
        elif payload["type"] == "input_audio_buffer.append" and payload["audio"] == _b64(b"\x07" * 640):
            await emit({"type": "conversation.item.input_audio_transcription.started"})
        elif payload["type"] == "input_audio_buffer.commit":
            asyncio.create_task(_later(0.6, emit, {"type": "conversation.item.input_audio_transcription.completed",
                                                    "text": "平仓"}))
    _, client_out, inbound, run, _ = _scripted(monkeypatch, on_send)
    await inbound.put({"type": "websocket.receive", "bytes": b"\x07" * 640})

    async def release():
        await asyncio.sleep(0.2)
        await inbound.put({"type": "websocket.receive", "text": json.dumps({"type": "end"})})
    asyncio.create_task(release())
    await run(service)
    assert client_out[-1] == {"type": "final", "text": "平仓"}
    app.state.store.close()


async def test_the_ghost_start_of_doubaos_reply_does_not_hold_the_release_for_the_timeout(tmp_path, monkeypatch):
    """10-08 (production, c37ee88): Doubao finalised the held sentence mid-hold, started its (cancelled)
    reply and with it a `transcription.started` that has no words. That start kept the segment "open",
    so the release waited the full FINAL_TIMEOUT (6.05 s) for a completed that never came — the user
    gave up and nothing was sent. Words open a segment; a bare start does not."""
    app, _, service = await _voice_app(tmp_path, "ptt-ghost.sqlite3")
    monkeypatch.setattr(voice_transcribe, "FINAL_TIMEOUT_SECONDS", 3.0)
    monkeypatch.setattr(voice_transcribe, "SETTLED_GRACE_SECONDS", 0.3)

    async def on_send(payload, emit):
        if payload["type"] == "session.create":
            await emit({"type": "session.created", "session": {"id": "s1"}})
        elif payload["type"] == "input_audio_buffer.append" and payload["audio"] == _b64(b"\x05" * 640):
            await emit({"type": "conversation.item.input_audio_transcription.started"})
            await emit({"type": "conversation.item.input_audio_transcription.delta", "delta": "帮我看看沪金"})
            await emit({"type": "conversation.item.input_audio_transcription.completed", "text": "帮我看看沪金。"})
            await emit({"type": "response.created", "response": {"id": "r1"}})
            await emit({"type": "conversation.item.input_audio_transcription.started"})  # the ghost
    upstream_sent, client_out, inbound, run, _ = _scripted(monkeypatch, on_send)
    await inbound.put({"type": "websocket.receive", "bytes": b"\x05" * 640})

    async def release():
        for _ in range(50):
            await asyncio.sleep(0.02)
            if any(p["type"] == "response.cancel" for p in upstream_sent):
                break
        await inbound.put({"type": "websocket.receive", "text": json.dumps({"type": "end"})})
    asyncio.create_task(release())
    started = asyncio.get_running_loop().time()
    await run(service)
    assert client_out[-1] == {"type": "final", "text": "帮我看看沪金。"}
    assert asyncio.get_running_loop().time() - started < 1.5
    app.state.store.close()
