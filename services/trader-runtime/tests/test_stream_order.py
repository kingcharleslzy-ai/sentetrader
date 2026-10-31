"""2026-09-24: a (re)connecting SSE reader must end on the live snapshot.

A replay session stores its startup ``runtime.status`` events (``connecting``) and
publishes the current status (``normal``) as a transient. The stream used to send the
transient first and the stored backlog after it, so the client's last applied status
was the oldest one and the replay chart never loaded.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from trader_runtime.broker import EventBroker
from trader_runtime.store import RuntimeStore


class _Request:
    def __init__(self) -> None:
        self.calls = 0

    async def is_disconnected(self) -> bool:
        self.calls += 1
        return self.calls > 1


async def _allow() -> None:
    return None


def _message(text: str) -> dict:
    # Any valid event shape carries the ordering; runtime.status needs the full status body.
    return {"turnId": f"t-{text}", "role": "assistant", "text": text}


async def test_backlog_is_sent_before_the_live_transient(tmp_path: Path):
    broker = EventBroker(RuntimeStore(tmp_path / "runtime.sqlite3"))
    session = broker.create_session("local", "native", "local-user")
    for _ in range(3):
        await broker.publish(session.session_id, kind="message", name="message.completed",
                             payload=_message("stored-connecting"))

    async def publish_live() -> None:
        await broker.publish_transient(session.session_id, kind="message", name="message.completed",
                                       payload=_message("live-normal"))

    frames = [frame async for frame in broker.stream(
        session.session_id, _Request(), authorize=_allow, on_subscribed=publish_live)]
    texts = [json.loads(frame.split("data: ", 1)[1])["payload"]["text"]
             for frame in frames if "message.completed" in frame]
    assert texts == ["stored-connecting", "stored-connecting", "stored-connecting", "live-normal"]
    assert frames[-1].startswith("event: transient")
    await asyncio.sleep(0)
