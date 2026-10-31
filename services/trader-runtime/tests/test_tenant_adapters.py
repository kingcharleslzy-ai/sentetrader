from __future__ import annotations

import pytest

from trader_runtime.tenant_adapters import SessionAgentRouter


class _RecordingAdapter:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def handle_message(self, session_id, text, run_id, *, user_id=None, model_slug=None):
        self.calls.append((session_id, text, run_id, user_id, model_slug))


class _Store:
    def get_session(self, session_id, **kwargs):
        return {"id": session_id, "userId": "cloud-user-1"}


@pytest.mark.asyncio
async def test_router_forwards_user_and_model_selection_to_the_user_adapter():
    adapters: dict[str, _RecordingAdapter] = {}
    router = SessionAgentRouter(
        store=_Store(),
        adapter_factory=lambda user_id: adapters.setdefault(user_id, _RecordingAdapter()),
        adapter_id="fake-agent",
        runtime_status_factory=lambda user_id, connected: {},
    )
    await router.handle_message("session-1", "hello", "run-1", user_id="cloud-user-1", model_slug="glm-5.3-flash")
    assert adapters["cloud-user-1"].calls == [("session-1", "hello", "run-1", "cloud-user-1", "glm-5.3-flash")]
