"""上游看门狗与念句前的取消确认（2026-10-15，生产通话 315c0ee1）。

豆包会话会整条停止应答：Runtime 念句（speech_text_buffer.commit）之后 55 s 没有任何事件，连下一句话的转写都没有。
直连探针还见到会话建好后连 session.created 都不回、成串出现、一分钟内新连接也不回。Runtime 的做法：
① 每次念句前先等它自己的 response.cancel 被确认（canceled），有上限；
② 念句或建会话后若干秒内没有任何上游事件，就新开一条豆包会话（带对话上下文）把没听到的那句再念一遍；
③ 连续几条会话都不回，就明确告诉用户“语音通道没有响应，请稍后重拨”并结束通话，不再静默。
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
import types

from test_voice_realtime import WINDOW, _voice_app
from trader_runtime.voice_front import FRONT_ACTION_ENV, FRONT_QUERY_ENV

RETRACTED = "开三手，不对，两手沪金多单，算了。"
LINE = "好，这单不下了。"


def _fast_watchdog(monkeypatch):
    import trader_runtime.voice_realtime as module
    monkeypatch.setattr(module, "UPSTREAM_REPLY_WAIT_SECONDS", 0.3)
    monkeypatch.setattr(module, "SESSION_CREATED_WAIT_SECONDS", 0.3)
    monkeypatch.setattr(module, "UPSTREAM_WATCH_TICK_SECONDS", 0.02)
    monkeypatch.setattr(module, "UPSTREAM_RECONNECT_BACKOFF_SECONDS", (0.0, 0.05, 0.05))
    monkeypatch.setattr(module, "SESSION_CLOSE_WAIT_SECONDS", 0.2)


def _doubao(monkeypatch, behaviours, handshakes=()):
    """A fake Doubao where every connection is a new session. `behaviours[i](payload, emit, conn)` scripts
    connection i (the last one repeats); `conn` keeps that session's own record; `handshakes[i]` delays
    connection i's handshake. Returns (connections, client_out, inbound, run)."""
    connections: list[dict] = []

    class Session:
        def __init__(self, index: int):
            self.queue: asyncio.Queue = asyncio.Queue()
            self.record = {"index": index, "sent": [], "silent": False}
            self.behaviour = behaviours[min(index, len(behaviours) - 1)]

        async def send(self, raw: str) -> None:
            payload = json.loads(raw)
            self.record["sent"].append(payload)
            if not self.record["silent"]:
                await self.behaviour(payload, self.queue.put, self.record)

        def __aiter__(self):
            return self

        async def __anext__(self):
            return json.dumps(await self.queue.get())

    class Connect:
        def __init__(self, *a, **k):
            self.session = Session(len(connections))
            connections.append(self.session.record)

        async def __aenter__(self):
            index = self.session.record["index"]
            if index < len(handshakes) and handshakes[index]:
                await asyncio.sleep(handshakes[index])
            return self.session

        async def __aexit__(self, *a):
            return False
    monkeypatch.setitem(sys.modules, "websockets", types.SimpleNamespace(connect=Connect))
    client_out: list[dict] = []
    inbound: asyncio.Queue = asyncio.Queue()

    class FakeClient:
        async def send_text(self, text: str) -> None:
            client_out.append(json.loads(text))

        async def receive(self) -> dict:
            return await inbound.get()

        async def close(self) -> None: ...

    async def run(service, *, until, timeout=8):
        from trader_runtime.voice_realtime import CallIdentity, _run_call

        async def finish_later():
            for _ in range(int(timeout * 20)):
                await asyncio.sleep(0.05)
                if until():
                    break
            await inbound.put({"type": "websocket.disconnect"})
        asyncio.create_task(finish_later())
        await asyncio.wait_for(_run_call(service, FakeClient(), "key", CallIdentity(user_id="local-user", headers={})), timeout=timeout + 5)
    return connections, client_out, inbound, run


async def _answering(payload, emit, conn, *, utterance=None, cancel_ack_after=0.0, ack_cancel=True, tool_after=0.05):
    """A healthy session: answers session.create, says `utterance` on the first audio (then calls `action`, as
    Doubao did in production), acknowledges cancels and reads commits out."""
    kind = payload["type"]
    if kind == "session.create":
        await emit({"type": "session.created", "session": {"id": f"s{conn['index']}"}})
    elif kind == "input_audio_buffer.append" and utterance and not conn.get("said"):
        conn["said"] = True
        await emit({"type": "conversation.item.input_audio_transcription.completed", "text": utterance})
        await asyncio.sleep(tool_after)
        await emit({"type": "response.function_call_arguments.done", "items": [
            {"call_id": "c1", "name": "action", "arguments": json.dumps({"kind": "open", "direction": "long", "product": "沪金", "volume": 2})}]})
    elif kind == "response.cancel" and ack_cancel:
        async def ack():
            await asyncio.sleep(cancel_ack_after)
            conn.setdefault("order", []).append("canceled")
            await emit({"type": "response.canceled"})
            await emit({"type": "response.done", "response": {"usage": {"total_tokens": 1}}})
        asyncio.ensure_future(ack())
    elif kind == "speech_text_buffer.commit":
        conn.setdefault("order", []).append("commit")
        rid = f"r{conn['index']}-{len(conn['sent'])}"
        await emit({"type": "response.output_audio.started", "tts_type": "chat_tts_text", "response_id": rid})
        await emit({"type": "response.output_audio.delta", "delta": "QUJD"})
        await emit({"type": "response.output_audio.done", "response_id": rid})
        await emit({"type": "response.done", "response": {"usage": {"total_tokens": 2}}})
    elif kind == "session.close":
        await emit({"type": "session.closed"})


async def _start(app, inbound):
    session = app.state.broker.create_session("local", "native", "local-user")
    await inbound.put({"type": "websocket.receive", "text": json.dumps({"type": "hello", "windowId": WINDOW, "sessionId": session.session_id})})
    await inbound.put({"type": "websocket.receive", "bytes": b"\x00" * 640})
    return session


def _commits(conn):
    return [p["text"] for p in conn["sent"] if p["type"] == "speech_text_buffer.commit"]


async def _app(tmp_path, monkeypatch, name):
    monkeypatch.setenv(FRONT_QUERY_ENV, "local-user")
    monkeypatch.setenv(FRONT_ACTION_ENV, "local-user")
    app, _, service = await _voice_app(tmp_path, name)
    service.product_names = ["沪金"]
    return app, service


async def _close(app):
    await app.state.shared_market.close()
    app.state.store.close()


async def test_a_session_that_goes_silent_after_the_line_is_replaced_and_the_line_said_again(tmp_path, monkeypatch):
    """315c0ee1: 撤回 → Doubao's `action` dropped and its reply cancelled → the line committed → nothing ever again.
    A new session takes over (the stalled one is never heard from again) and says the same line."""
    _fast_watchdog(monkeypatch)
    app, service = await _app(tmp_path, monkeypatch, "stall-commit.sqlite3")

    # The first session reads nothing out: its commit is swallowed and it goes quiet for good.

    async def first(payload, emit, conn):
        if payload["type"] == "speech_text_buffer.commit":
            conn["silent"] = True
            conn.setdefault("order", []).append("commit")
            return
        await _answering(payload, emit, conn, utterance=RETRACTED)

    async def second(payload, emit, conn):
        await _answering(payload, emit, conn)
    connections, client_out, inbound, run = _doubao(monkeypatch, [first, second])
    await _start(app, inbound)
    await run(service, until=lambda: len(connections) > 1 and _commits(connections[1]) and any(e.get("type") == "audio" for e in client_out))
    timeline = client_out[-1]["timeline"]
    assert _commits(connections[0]) == [LINE] and _commits(connections[1]) == [LINE]
    # The order the user hears it in: Doubao's cancelled reply acknowledged, then the line (on the first session).
    assert connections[0]["order"] == ["canceled", "commit"]
    stalled = next(e for e in timeline if e["event"] == "upstreamStalled")
    assert stalled["waiting"] == "commit"
    assert next(e for e in timeline if e["event"] == "upstreamReconnected")["respeak"] is True
    # The client saw "接通中" while the session was swapped, then listening again, and the line's audio.
    states = [e.get("state") for e in client_out if e.get("type") == "status"]
    assert "connecting" in states[states.index("listening") + 1:]
    assert next(e for e in timeline if e["event"] == "audioPath")["fwdFrames"] == 1
    # The new session was made like the first (same brief and tools), and only one swap happened.
    created = [next(p for p in c["sent"] if p["type"] == "session.create") for c in connections]
    assert created[0]["session"] == created[1]["session"] and len(connections) == 2
    assert not any(e.get("type") == "error" for e in client_out)
    await _close(app)


async def test_a_ghost_start_or_an_old_cancels_acknowledgement_after_the_line_does_not_count_as_an_answer(tmp_path, monkeypatch):
    """#904 review (P1): f1353ada got a wordless transcription.started right after the commit, and an in-reader line
    ("已停止。") can be followed by the acknowledgement of an earlier cancel — neither is the line being read, so the
    stalled session is still replaced."""
    _fast_watchdog(monkeypatch)
    app, service = await _app(tmp_path, monkeypatch, "stall-ghost.sqlite3")

    async def first(payload, emit, conn):
        if payload["type"] == "speech_text_buffer.commit":
            await emit({"type": "conversation.item.input_audio_transcription.started"})
            await emit({"type": "response.canceled"})
            await emit({"type": "response.done", "response": {"usage": {"total_tokens": 1}}})
            conn["silent"] = True
            return
        await _answering(payload, emit, conn, utterance=RETRACTED)

    async def second(payload, emit, conn):
        await _answering(payload, emit, conn)
    connections, client_out, inbound, run = _doubao(monkeypatch, [first, second])
    await _start(app, inbound)
    await run(service, until=lambda: len(connections) > 1 and _commits(connections[1]) == [LINE])
    timeline = client_out[-1]["timeline"]
    assert next(e for e in timeline if e["event"] == "upstreamStalled")["waiting"] == "commit"
    # #905 review: an acknowledgement of a cancel is no sign of a live session — no retry on the dead one first.
    assert _commits(connections[0]) == [LINE] and not any(e["event"] == "speakRetry" for e in timeline)
    await _close(app)


async def test_a_line_swallowed_by_a_live_session_is_said_again_on_it_and_the_late_final_does_not_release_it(tmp_path, monkeypatch):
    """10-15 retest d5902f9b: Doubao never finalised the sentence (the Runtime answered the stall final), the line
    was committed while Doubao still held that sentence open, and it answered with a bare response.done; its late
    completed of the same sentence is not the user saying something new. The live session gets the line once more."""
    _fast_watchdog(monkeypatch)
    import trader_runtime.voice_realtime as module
    monkeypatch.setattr(module, "STALL_FINAL_SECONDS", 0.05)
    monkeypatch.setattr(module, "ASR_WATCHDOG_TICK", 0.01)
    monkeypatch.setattr(module, "FILLER_WAIT_SECONDS", 0.05)
    app, service = await _app(tmp_path, monkeypatch, "swallowed.sqlite3")
    preferences = app.state.store.user_preferences("local-user")
    fast = preferences.model_copy(update={"voice": preferences.voice.model_copy(update={"endpointStallSeconds": 0.2})})
    monkeypatch.setattr(app.state.store, "user_preferences", lambda user_id: fast)

    async def swallows_the_first_line(payload, emit, conn):
        kind = payload["type"]
        if kind == "input_audio_buffer.append" and not conn.get("said"):
            conn["said"] = True
            await emit({"type": "conversation.item.input_audio_transcription.started"})
            await emit({"type": "conversation.item.input_audio_transcription.delta", "delta": RETRACTED})
        elif kind == "speech_text_buffer.commit" and conn.get("swallowed"):
            # The retry plays — and Doubao, which held the answered sentence open all along, re-sends it (a
            # growing partial in the second pass's words, then its completed) in the middle of the read-out.
            conn.setdefault("order", []).append("commit")
            await emit({"type": "response.output_audio.started", "tts_type": "chat_tts_text", "response_id": "retry"})
            if not conn.get("replayed"):
                conn["replayed"] = True
                for partial in ("开三手", "开 3 手，不对，两手"):
                    await emit({"type": "conversation.item.input_audio_transcription.delta", "delta": partial})
                await emit({"type": "conversation.item.input_audio_transcription.completed", "text": RETRACTED})
            await emit({"type": "response.output_audio.delta", "delta": "QUJD"})
            await emit({"type": "response.output_audio.done", "response_id": "retry"})
            await emit({"type": "response.done", "response": {"usage": {}}})
        elif kind == "speech_text_buffer.commit":
            conn["swallowed"] = True
            conn.setdefault("order", []).append("commit")
            await emit({"type": "response.done", "response": {"usage": {}}})
        else:
            await _answering(payload, emit, conn)
    connections, client_out, inbound, run = _doubao(monkeypatch, [swallows_the_first_line])
    await _start(app, inbound)
    await run(service, until=lambda: len(_commits(connections[0])) == 2 and any(e.get("type") == "audio" for e in client_out))
    timeline = client_out[-1]["timeline"]
    assert any(e["event"] == "asrStallFinal" for e in timeline) and any(e["event"] == "asrFinalLate" for e in timeline)
    assert _commits(connections[0]) == [LINE, LINE] and len(connections) == 1
    assert any(e["event"] == "speakRetry" for e in timeline) and not any(e["event"] == "upstreamStalled" for e in timeline)
    assert not any(e["event"] == "bargeIn" for e in timeline) and not any(e.get("type") == "interrupt" for e in client_out)
    assert not any(p["type"] == "response.cancel" for p in connections[0]["sent"])
    await _close(app)


async def test_a_session_that_never_answers_session_create_is_replaced_and_the_call_goes_on(tmp_path, monkeypatch):
    """The direct probe (10-15): 6 of 120 sessions never sent session.created. A new session is opened and the
    call works on it — the first sentence is answered there."""
    _fast_watchdog(monkeypatch)
    app, service = await _app(tmp_path, monkeypatch, "stall-create.sqlite3")

    async def dead(payload, emit, conn):
        conn["silent"] = True

    async def healthy(payload, emit, conn):
        await _answering(payload, emit, conn, utterance=RETRACTED)
    connections, client_out, inbound, run = _doubao(monkeypatch, [dead, healthy])
    await _start(app, inbound)
    # The microphone keeps sending (the real client streams 20 ms frames): the new session hears the sentence.

    async def mic():
        for _ in range(40):
            await inbound.put({"type": "websocket.receive", "bytes": b"\x00" * 640})
            await asyncio.sleep(0.03)
    asyncio.ensure_future(mic())
    await run(service, until=lambda: len(connections) > 1 and _commits(connections[1]) == [LINE])
    timeline = client_out[-1]["timeline"]
    assert next(e for e in timeline if e["event"] == "upstreamStalled")["waiting"] == "session"
    assert next(e for e in timeline if e["event"] == "upstreamReconnected")["respeak"] is False
    assert [e.get("sessionId") for e in client_out if e.get("type") == "status" and e.get("sessionId")] == ["s1"]
    assert _commits(connections[0]) == []
    await _close(app)


async def test_when_no_session_answers_the_user_is_told_and_the_call_ends(tmp_path, monkeypatch, caplog):
    """The probe's one-minute window: every fresh connection stayed silent too. After the bounded tries the client
    shows the error (not standby: no closing summary follows it) and the call ends."""
    _fast_watchdog(monkeypatch)
    app, service = await _app(tmp_path, monkeypatch, "stall-all.sqlite3")

    async def dead(payload, emit, conn):
        conn["silent"] = True
    connections, client_out, inbound, run = _doubao(monkeypatch, [dead])
    await _start(app, inbound)
    monkeypatch.setattr(logging.getLogger("uvicorn.error.voice"), "propagate", True)
    caplog.set_level(logging.INFO, logger="uvicorn.error.voice")
    await run(service, until=lambda: False, timeout=4)
    import trader_runtime.voice_realtime as module
    assert len(connections) == 1 + module.UPSTREAM_RECONNECT_ATTEMPTS
    errors = [e for e in client_out if e.get("type") == "error"]
    assert errors and errors[-1]["error"]["message"] == module.UPSTREAM_GIVE_UP_LINE
    assert client_out[-1]["type"] == "error"  # no closing summary turns the orb back to standby
    log = caplog.text
    assert "upstreamGaveUp" in log and "hangupByStall" in log
    await _close(app)


async def test_a_line_waits_for_doubaos_cancel_to_be_acknowledged_and_never_for_long(tmp_path, monkeypatch):
    """Every Runtime-spoken line goes through one entry: the Runtime's own response.cancel is acknowledged first
    (here 0.3 s late), and a cancel that is never acknowledged holds the line only SPEAK_CANCEL_WAIT_SECONDS."""
    _fast_watchdog(monkeypatch)
    import trader_runtime.voice_realtime as module
    monkeypatch.setattr(module, "UPSTREAM_REPLY_WAIT_SECONDS", 5.0)
    monkeypatch.setattr(module, "FILLER_WAIT_SECONDS", 0.05)
    app, service = await _app(tmp_path, monkeypatch, "cancel-ack.sqlite3")

    async def late_ack(payload, emit, conn):
        # Doubao's call comes with the final, so its cancel is open before the Runtime's line is ready.
        await _answering(payload, emit, conn, utterance=RETRACTED, cancel_ack_after=0.3, tool_after=0.0)
    connections, client_out, inbound, run = _doubao(monkeypatch, [late_ack])
    await _start(app, inbound)
    await run(service, until=lambda: _commits(connections[0]) == [LINE])
    assert connections[0]["order"] == ["canceled", "commit"]
    await _close(app)

    monkeypatch.setattr(module, "SPEAK_CANCEL_WAIT_SECONDS", 0.6)
    app, service = await _app(tmp_path, monkeypatch, "cancel-noack.sqlite3")

    async def no_ack(payload, emit, conn):
        await _answering(payload, emit, conn, utterance=RETRACTED, ack_cancel=False, tool_after=0.0)
    connections, client_out, inbound, run = _doubao(monkeypatch, [no_ack])
    await _start(app, inbound)
    await run(service, until=lambda: _commits(connections[0]) == [LINE])
    assert _commits(connections[0]) == [LINE]
    wait = next(e for e in client_out[-1]["timeline"] if e["event"] == "speakWait")
    assert wait["cancelAcked"] is False and wait["seconds"] < 1.0
    await _close(app)


async def test_a_healthy_call_never_reconnects(tmp_path, monkeypatch):
    _fast_watchdog(monkeypatch)
    app, service = await _app(tmp_path, monkeypatch, "healthy.sqlite3")

    async def healthy(payload, emit, conn):
        await _answering(payload, emit, conn, utterance=RETRACTED)
    connections, client_out, inbound, run = _doubao(monkeypatch, [healthy])
    await _start(app, inbound)
    seen: list[float] = []

    def settled():
        if not seen and _commits(connections[0]):
            seen.append(asyncio.get_running_loop().time())
        return bool(seen) and asyncio.get_running_loop().time() - seen[0] > 1.0  # > 3× the patched reply wait
    await run(service, until=settled)
    assert len(connections) == 1
    assert not any(e["event"].startswith("upstream") and e["event"] != "upstreamSent" for e in client_out[-1]["timeline"])
    await _close(app)


def test_a_replay_of_the_answered_sentence_is_not_new_words():
    from trader_runtime.voice_realtime import _replays_answered
    first_pass = "开三首不对两首互金多单算了"  # what the stall final answered (d5902f9b)
    for partial in ("开", "开三手", "开 3 手，不对", "开三手，不对，两手沪金多单，算了。", first_pass, ""):
        assert _replays_answered(partial, first_pass), partial
        assert _replays_answered(partial, RETRACTED), partial
    for partial in ("螺纹多少钱", "确认", "开三手，不对，两手沪金多单，算了。还是开两手", "开五手", "开三手，不对，五手",
                    # #905 review: another action or direction is another order, however alike the words read
                    "平三手", "开三手，不对，两手沪金空单"):
        assert not _replays_answered(partial, RETRACTED), partial
    assert not _replays_answered("开三手", "")  # nothing answered early: any words are new
    products = ["沪金", "白银"]
    assert not _replays_answered("开三手，不对，两手白银", RETRACTED, products)  # another product
    # A misheard name in the answered first pass (互金) says nothing either way: the second pass is still a replay.
    assert _replays_answered(RETRACTED, first_pass, products)
    assert _replays_answered("开三手，不对，两手沪金", RETRACTED, products)


# -- #754 / #904 / #905 review: the call owns its sessions, its lines and its tasks --------------------------


async def _swallowing(payload, emit, conn, *, utterance=RETRACTED, read=0):
    """A session that answers its first `read` commits and then nothing at all (its later commits included)."""
    if payload["type"] == "speech_text_buffer.commit":
        conn["commits"] = conn.get("commits", 0) + 1
        if conn["commits"] > read:
            conn["silent"] = True
            return
    await _answering(payload, emit, conn, utterance=utterance)


async def _dead(payload, emit, conn):
    conn["silent"] = True


async def _healthy(payload, emit, conn):
    await _answering(payload, emit, conn)


def _mic(inbound, frames=60):
    async def stream():
        for _ in range(frames):
            await inbound.put({"type": "websocket.receive", "bytes": b"\x00" * 640})
            await asyncio.sleep(0.03)
    return asyncio.ensure_future(stream())


def _quiet_announcer(monkeypatch, service):
    import trader_runtime.voice_realtime as module
    monkeypatch.setattr(module, "ANNOUNCE_POLL_SECONDS", 0.05)
    # #932: the announcer only plays queued reminders (the call no longer polls events itself).
    service._pending_announcements.clear()


async def test_a_candidate_that_closes_is_only_a_failed_try_and_the_call_has_three_replacements_in_all(tmp_path, monkeypatch, caplog):
    """#904 review 1 + 8: the second session closes before session.created — a failed candidate, not the end of the
    call (it used to set the call's `stop`, and no later stall was ever recovered). The third says the line and,
    stalling on the next one, is replaced once more — the last of three replacements over the whole call."""
    _fast_watchdog(monkeypatch)
    app, service = await _app(tmp_path, monkeypatch, "candidate-closed.sqlite3")

    async def closes(payload, emit, conn):
        if payload["type"] == "session.create":
            await emit({"type": "session.closed"})

    async def reads_one_then_stalls(payload, emit, conn):
        await _swallowing(payload, emit, conn, read=1)
    connections, client_out, inbound, run = _doubao(monkeypatch, [_swallowing, closes, reads_one_then_stalls, _dead])
    await _start(app, inbound)
    _mic(inbound)
    monkeypatch.setattr(logging.getLogger("uvicorn.error.voice"), "propagate", True)
    caplog.set_level(logging.INFO, logger="uvicorn.error.voice")
    await run(service, until=lambda: False, timeout=4)
    assert _commits(connections[2]) == [LINE, LINE]  # the line said again, then the next one said there stalled
    assert len(connections) == 4  # one budget for the call: a per-recovery budget opened three more
    import trader_runtime.voice_realtime as module
    assert client_out[-1]["type"] == "error" and client_out[-1]["error"]["message"] == module.UPSTREAM_GIVE_UP_LINE
    assert "upstreamReconnectFailed" in caplog.text and "error=closed" in caplog.text
    assert "upstreamGaveUp" in caplog.text
    await _close(app)


async def test_a_replacement_session_has_one_bound_for_its_handshake_and_session_created(tmp_path, monkeypatch):
    """#904 review 5: a slow handshake and a slow session.created used to get a bound each."""
    _fast_watchdog(monkeypatch)
    import trader_runtime.voice_realtime as module
    monkeypatch.setattr(module, "SESSION_CREATED_WAIT_SECONDS", 0.45)
    app, service = await _app(tmp_path, monkeypatch, "slow-handshake.sqlite3")

    async def slow_created(payload, emit, conn):
        if payload["type"] == "session.create":
            async def later():
                await asyncio.sleep(0.3)  # 0.6 s after the dial: within each bound alone, past the shared one
                await emit({"type": "session.created", "session": {"id": "late"}})
            asyncio.ensure_future(later())
        else:
            await _answering(payload, emit, conn)
    connections, client_out, inbound, run = _doubao(monkeypatch, [_swallowing, slow_created, _healthy], handshakes=(0, 0.3))
    await _start(app, inbound)
    await run(service, until=lambda: len(connections) > 2 and _commits(connections[2]) == [LINE])
    assert _commits(connections[1]) == [] and _commits(connections[2]) == [LINE]
    assert next(e for e in client_out[-1]["timeline"] if e["event"] == "upstreamReconnectFailed")["error"] == "no_session"
    await _close(app)


async def test_a_line_queued_behind_a_long_swap_waits_for_the_ready_session(tmp_path, monkeypatch):
    """#904 review 4: speak() waited for the swap at most 15 s (here 0.9 s) and then committed to a session that was
    not ready. Now a line queued meanwhile follows the unheard line on the session that answered."""
    _fast_watchdog(monkeypatch)
    import trader_runtime.voice_realtime as module
    monkeypatch.setattr(module, "UPSTREAM_RECONNECT_BACKOFF_SECONDS", (0.0, 0.3, 0.3))
    app, service = await _app(tmp_path, monkeypatch, "queued-line.sqlite3")
    _quiet_announcer(monkeypatch, service)

    async def dead_but_announces(payload, emit, conn):
        conn["silent"] = True
        if conn["index"] == 1:
            service.announce("fill", "螺纹成交了一手。")
    connections, client_out, inbound, run = _doubao(monkeypatch, [_swallowing, dead_but_announces, _dead, _healthy])
    await _start(app, inbound)
    await run(service, until=lambda: len(connections) > 3 and len(_commits(connections[3])) == 2)
    assert [_commits(c) for c in connections[1:]] == [[], [], [LINE, "螺纹成交了一手。"]]
    await _close(app)


async def test_two_swallowed_lines_are_both_said_again_in_order(tmp_path, monkeypatch):
    """#904 review 6: the second line overwrote the first one's record — only the last was said again."""
    _fast_watchdog(monkeypatch)
    app, service = await _app(tmp_path, monkeypatch, "two-lines.sqlite3")
    _quiet_announcer(monkeypatch, service)
    service.announce("one", "第一句。")
    service.announce("two", "第二句。")

    async def swallows(payload, emit, conn):
        if payload["type"] == "speech_text_buffer.commit":
            return
        await _answering(payload, emit, conn)
    connections, client_out, inbound, run = _doubao(monkeypatch, [swallows, _healthy])
    await _start(app, inbound)
    await run(service, until=lambda: len(connections) > 1 and len(_commits(connections[1])) == 2)
    assert _commits(connections[0]) == ["第一句。", "第二句。"] and _commits(connections[1]) == ["第一句。", "第二句。"]
    await _close(app)


async def test_the_first_lines_late_audio_does_not_answer_the_second_line(tmp_path, monkeypatch):
    """#904 review 6: Doubao reads commits in order, so an audio start answers the oldest line only — the second
    line, never started, is still said again on a new session."""
    _fast_watchdog(monkeypatch)
    app, service = await _app(tmp_path, monkeypatch, "late-first.sqlite3")
    _quiet_announcer(monkeypatch, service)
    service.announce("one", "第一句。")
    service.announce("two", "第二句。")

    async def reads_first_late(payload, emit, conn):
        if payload["type"] == "speech_text_buffer.commit":
            conn["commits"] = conn.get("commits", 0) + 1
            if conn["commits"] == 2:  # the first line starts only now, and the second never
                await emit({"type": "response.output_audio.started", "tts_type": "chat_tts_text", "response_id": "first"})
                await emit({"type": "response.output_audio.delta", "delta": "QUJD"})
                await emit({"type": "response.output_audio.done", "response_id": "first"})
                conn["silent"] = True
            return
        await _answering(payload, emit, conn)
    connections, client_out, inbound, run = _doubao(monkeypatch, [reads_first_late, _healthy])
    await _start(app, inbound)
    await run(service, until=lambda: len(connections) > 1 and _commits(connections[1]))
    assert _commits(connections[1]) == ["第二句。"]
    await _close(app)


async def test_a_goodbye_that_stalls_is_said_on_the_new_session_before_the_call_ends(tmp_path, monkeypatch):
    """#904 review 3: recovery set audio_done, so the hang-up took the stalled goodbye for played and ended the call
    before it was said again."""
    _fast_watchdog(monkeypatch)
    import trader_runtime.voice_realtime as module
    monkeypatch.setattr(module, "HANGUP_GOODBYE_WAIT_SECONDS", 0.05)
    monkeypatch.setattr(module, "HANGUP_AUDIO_WAIT_SECONDS", 0.3)
    monkeypatch.setattr(module, "HANGUP_TAIL_SECONDS", 0.0)
    app, service = await _app(tmp_path, monkeypatch, "goodbye.sqlite3")

    async def hears_goodbye(payload, emit, conn):
        if payload["type"] == "input_audio_buffer.append" and not conn.get("said"):
            conn["said"] = True
            await emit({"type": "conversation.item.input_audio_transcription.completed", "text": "关闭语音"})
        elif payload["type"] == "speech_text_buffer.commit":
            conn["silent"] = True
        else:
            await _answering(payload, emit, conn)
    connections, client_out, inbound, run = _doubao(monkeypatch, [hears_goodbye, _healthy])
    await _start(app, inbound)
    await run(service, until=lambda: False, timeout=3)
    assert _commits(connections[0]) == ["好，挂了。"] and _commits(connections[1]) == ["好，挂了。"]
    timeline = client_out[-1]["timeline"]
    assert any(e["event"] == "hangupByVoice" for e in timeline)
    assert next(e for e in timeline if e["event"] == "audioPath")["fwdFrames"] == 1  # heard before the call ended
    await _close(app)


async def test_a_replaced_sessions_tool_handler_never_acts_in_the_new_sessions_turn(tmp_path, monkeypatch):
    """#904 review 2 + #523: the session closes while its tool call is running. The call goes on on a new session
    (a session.closed used to stop the call's watchdogs and leave it with no upstream), and the old handler's
    result is said out of turn there — never as a replacement, a response.cancel or a tool result in its turn."""
    _fast_watchdog(monkeypatch)
    app, service = await _app(tmp_path, monkeypatch, "stale-tool.sqlite3")
    runs: list[str] = []

    async def slow_tool(name, arguments, state, **kwargs):
        runs.append(name)
        await asyncio.sleep(0.4)
        return {"ok": True, "spoken": "现在在看螺纹。"}
    monkeypatch.setattr(service, "run_tool", slow_tool)

    async def calls_then_closes(payload, emit, conn):
        if payload["type"] == "session.create":
            await emit({"type": "session.created", "session": {"id": "s0"}})
            await emit({"type": "response.function_call_arguments.done", "items": [
                {"call_id": "c9", "name": "read_screen", "arguments": "{}"}]})

            async def close_soon():
                await asyncio.sleep(0.1)
                await emit({"type": "session.closed"})
            asyncio.ensure_future(close_soon())
    connections, client_out, inbound, run = _doubao(monkeypatch, [calls_then_closes, _healthy])
    await _start(app, inbound)
    await run(service, until=lambda: len(connections) > 1 and _commits(connections[1]))
    sent = [p["type"] for p in connections[1]["sent"]]
    assert _commits(connections[1]) == ["现在在看螺纹。"] and runs == ["read_screen"]
    assert not any(kind.startswith("speech_text_buffer.replacement") or kind == "response.cancel" for kind in sent)
    assert not any(p["type"] == "conversation.item.create" and any(i.get("call_id") for i in p.get("items") or [])
                   for p in connections[1]["sent"])
    timeline = client_out[-1]["timeline"]
    assert next(e for e in timeline if e["event"] == "upstreamLost")["why"] == "closed"
    assert any(e["event"] == "staleUpstreamDropped" for e in timeline)
    await _close(app)


async def test_a_send_that_fails_replaces_the_session_and_a_mute_reaches_the_new_one(tmp_path, monkeypatch):
    """#904 review 7: a mute sent to a session that cannot take it ended the whole call (the client reader died).
    Now the session is replaced and the mute is sent once the new one is ready."""
    _fast_watchdog(monkeypatch)
    app, service = await _app(tmp_path, monkeypatch, "mute-swap.sqlite3")

    async def breaks_on_mute(payload, emit, conn):
        if payload["type"] == "input_audio_mute.commit":
            raise ConnectionError("gone")
        await _answering(payload, emit, conn)
    connections, client_out, inbound, run = _doubao(monkeypatch, [breaks_on_mute, _healthy])
    await _start(app, inbound)
    await inbound.put({"type": "websocket.receive", "text": json.dumps({"type": "mute"})})
    await run(service, until=lambda: len(connections) > 1
              and any(p["type"] == "input_audio_mute.commit" for p in connections[1]["sent"]))
    sent = [p["type"] for p in connections[1]["sent"]]
    assert sent.index("session.create") < sent.index("input_audio_mute.commit")
    assert next(e for e in client_out[-1]["timeline"] if e["event"] == "upstreamLost")["why"] == "send"
    assert not any(e.get("type") == "error" for e in client_out)
    await _close(app)


async def test_a_retry_that_cannot_be_sent_replaces_the_session(tmp_path, monkeypatch):
    """#905 review: the same-session retry raised outside the watchdog's guard and the watchdog task ended silently."""
    _fast_watchdog(monkeypatch)
    app, service = await _app(tmp_path, monkeypatch, "retry-send.sqlite3")

    async def swallows_then_breaks(payload, emit, conn):
        if payload["type"] == "speech_text_buffer.commit":
            conn["commits"] = conn.get("commits", 0) + 1
            if conn["commits"] == 1:
                await emit({"type": "response.done", "response": {"usage": {}}})  # alive, the line swallowed
                return
            raise ConnectionError("gone")
        await _answering(payload, emit, conn, utterance=RETRACTED)
    connections, client_out, inbound, run = _doubao(monkeypatch, [swallows_then_breaks, _healthy])
    await _start(app, inbound)
    await run(service, until=lambda: len(connections) > 1 and _commits(connections[1]) == [LINE])
    timeline = client_out[-1]["timeline"]
    assert any(e["event"] == "speakRetry" for e in timeline)
    assert next(e for e in timeline if e["event"] == "upstreamLost")["why"] == "send"
    await _close(app)


async def test_a_replayed_tool_call_id_runs_the_tool_once(tmp_path, monkeypatch):
    """#523: Doubao re-sending a call it already made (its replays of answered sentences, #905) ran the tool again."""
    _fast_watchdog(monkeypatch)
    app, service = await _app(tmp_path, monkeypatch, "call-replay.sqlite3")
    runs: list[str] = []

    async def tool(name, arguments, state, **kwargs):
        runs.append(name)
        return {"ok": True}
    monkeypatch.setattr(service, "run_tool", tool)

    async def calls_twice(payload, emit, conn):
        if payload["type"] == "session.create":
            await emit({"type": "session.created", "session": {"id": "s0"}})
            for _ in range(2):
                await emit({"type": "response.function_call_arguments.done", "items": [
                    {"call_id": "c5", "name": "read_screen", "arguments": "{}"}]})
        else:
            await _answering(payload, emit, conn)
    connections, client_out, inbound, run = _doubao(monkeypatch, [calls_twice])
    await _start(app, inbound)
    await run(service, until=lambda: any(p["type"] == "conversation.item.create" for p in connections[0]["sent"]))
    assert runs == ["read_screen"]
    assert any(e["event"] == "toolCallReplayDropped" for e in client_out[-1]["timeline"])
    await _close(app)


async def test_a_cancelled_read_outs_frames_without_an_id_are_dropped(tmp_path, monkeypatch):
    """#523: audio frames carry no response id, so the frames of a read-out the user cancelled kept playing."""
    _fast_watchdog(monkeypatch)
    import trader_runtime.voice_realtime as module
    monkeypatch.setattr(module, "UPSTREAM_REPLY_WAIT_SECONDS", 5.0)
    app, service = await _app(tmp_path, monkeypatch, "cancelled-frames.sqlite3")

    async def keeps_streaming(payload, emit, conn):
        kind = payload["type"]
        if kind == "speech_text_buffer.commit":
            conn["reading"] = True
            await emit({"type": "response.output_audio.started", "tts_type": "chat_tts_text", "response_id": "line"})
            await emit({"type": "response.output_audio.delta", "delta": "QUJD"})
        elif kind == "response.cancel" and conn.get("reading"):
            for _ in range(2):  # what the synthesiser had made already, delivered after the cancel
                await emit({"type": "response.output_audio.delta", "delta": "QUJD"})
            await emit({"type": "response.output_audio.done"})
            await emit({"type": "response.canceled"})
            await emit({"type": "response.done", "response": {"usage": {}}})
            conn["done"] = True
        else:
            await _answering(payload, emit, conn, utterance=RETRACTED)
    connections, client_out, inbound, run = _doubao(monkeypatch, [keeps_streaming])
    await _start(app, inbound)

    async def barge_in():
        while not any(e.get("type") == "audio" for e in client_out):
            await asyncio.sleep(0.01)
        await inbound.put({"type": "websocket.receive", "text": json.dumps({"type": "cancel"})})
    asyncio.ensure_future(barge_in())
    await run(service, until=lambda: connections[0].get("done"))
    path = next(e for e in client_out[-1]["timeline"] if e["event"] == "audioPath")
    assert path["fwdFrames"] == 1 and path["dropFrames"] == 2
    await _close(app)


async def test_the_call_ending_stops_its_rounds_and_lets_the_business_call_finish(tmp_path, monkeypatch):
    """#523 (BIG): the rounds a call started outlived it and spoke into a closed session. Now they end with the call;
    the trading AI's turn they waited on is not cut half-way (its answer still lands in the conversation)."""
    _fast_watchdog(monkeypatch)
    app, _, service = await _voice_app(tmp_path, "call-end.sqlite3")
    progress: list[str] = []

    async def slow_delegate(name, arguments, state, **kwargs):
        progress.append("started")
        await asyncio.sleep(0.4)
        progress.append("finished")
        return {"ok": True, "spoken": "螺纹偏强。"}
    monkeypatch.setattr(service, "run_tool", slow_delegate)

    async def asks(payload, emit, conn):
        if payload["type"] == "input_audio_buffer.append" and not conn.get("said"):
            conn["said"] = True
            await emit({"type": "conversation.item.input_audio_transcription.completed", "text": "帮我分析一下螺纹今天为什么涨"})
        else:
            await _answering(payload, emit, conn)
    connections, client_out, inbound, run = _doubao(monkeypatch, [asks])
    await _start(app, inbound)
    await run(service, until=lambda: progress == ["started"])
    await asyncio.sleep(0.6)
    assert progress == ["started", "finished"]
    sent = [p["type"] for p in connections[0]["sent"]]
    assert sent[-1] == "session.close" and "螺纹偏强。" not in _commits(connections[0])
    await _close(app)


# -- #932 P2 (#912 review, 10-15 07:36): the boundaries the #912 round left open ---------------------------------


async def test_a_same_session_retry_says_every_line_still_waiting_so_no_later_line_is_lost(tmp_path, monkeypatch):
    """#912 review 3A: a live session swallowed two lines (a bare response.done each). The retry used to say only
    the first again and queue it behind the second ([B, A-retry]); the retried A's audio was then taken for B's
    start, and B — never heard — was never said again (only A, on a replacement session). Now every line still
    waiting is said again, in order, on the same session."""
    _fast_watchdog(monkeypatch)
    app, service = await _app(tmp_path, monkeypatch, "retry-two-lines.sqlite3")
    _quiet_announcer(monkeypatch, service)
    service.announce("one", "第一句。")
    service.announce("two", "第二句。")

    async def swallows_the_first_two(payload, emit, conn):
        if payload["type"] == "speech_text_buffer.commit":
            conn["commits"] = conn.get("commits", 0) + 1
            if conn["commits"] <= 2:
                await emit({"type": "response.done", "response": {"usage": {}}})  # alive, the line swallowed
                return
        await _answering(payload, emit, conn)
    connections, client_out, inbound, run = _doubao(monkeypatch, [swallows_the_first_two, _healthy])
    await _start(app, inbound)
    await run(service, until=lambda: len(_commits(connections[0])) >= 4 or len(connections) > 1, timeout=3)
    assert _commits(connections[0]) == ["第一句。", "第二句。", "第一句。", "第二句。"]
    assert len(connections) == 1  # nothing was left waiting for a replacement session
    await _close(app)


async def test_a_candidate_that_closes_right_after_session_created_is_a_failed_try(tmp_path, monkeypatch):
    """#912 review 3B: a replacement sent session.created and session.closed in the same breath. Its reader had set
    both `ready` and `gone`, the loss was ignored while swapping, and the call was handed to the dead session —
    with no line waiting nothing ever noticed again. Now it is one more failed try and the next session is used."""
    _fast_watchdog(monkeypatch)
    app, service = await _app(tmp_path, monkeypatch, "created-closed.sqlite3")
    _quiet_announcer(monkeypatch, service)

    async def closes_on_the_first_sound(payload, emit, conn):
        if payload["type"] == "input_audio_buffer.append" and not conn.get("closed"):
            conn["closed"] = True
            await emit({"type": "session.closed"})  # lost mid-call, with no line waiting
            return
        await _answering(payload, emit, conn)

    async def created_then_closed(payload, emit, conn):
        if payload["type"] == "session.create":
            await emit({"type": "session.created", "session": {"id": "dying"}})
            await emit({"type": "session.closed"})
    connections, client_out, inbound, run = _doubao(monkeypatch, [closes_on_the_first_sound, created_then_closed, _healthy])
    await _start(app, inbound)
    await run(service, until=lambda: len(connections) > 2 and any(
        e.get("type") == "status" and e.get("sessionId") == "s2" for e in client_out), timeout=3)
    timeline = client_out[-1]["timeline"]
    assert len(connections) == 3
    assert next(e for e in timeline if e["event"] == "upstreamReconnectFailed")["error"] == "closed"
    listening = [e.get("sessionId") for e in client_out if e.get("type") == "status" and e.get("state") == "listening"]
    assert "dying" not in listening and listening[-1] == "s2"
    await _close(app)


async def test_a_session_create_send_that_hangs_is_bounded_by_the_candidates_deadline(tmp_path, monkeypatch):
    """#912 review 3B: the candidate's handshake and session.created shared one bound, but the session.create send
    itself was outside it — a send that never returned held the recovery (and the unheard line) for good."""
    _fast_watchdog(monkeypatch)
    app, service = await _app(tmp_path, monkeypatch, "create-send-hangs.sqlite3")

    async def hangs(payload, emit, conn):
        if payload["type"] == "session.create":
            await emit({"type": "session.created", "session": {"id": "stuck"}})
            await asyncio.Event().wait()  # the send never completes
    connections, client_out, inbound, run = _doubao(monkeypatch, [_swallowing, hangs, _healthy])
    await _start(app, inbound)
    await run(service, until=lambda: len(connections) > 2 and _commits(connections[2]) == [LINE], timeout=3)
    assert _commits(connections[1]) == [] and _commits(connections[2]) == [LINE]
    assert next(e for e in client_out[-1]["timeline"] if e["event"] == "upstreamReconnectFailed")["error"] == "no_session"
    await _close(app)
