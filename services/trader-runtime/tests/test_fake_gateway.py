"""The browser-test gateway double is reachable only from the local fake runtime."""
from __future__ import annotations

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from trader_runtime.main import create_app
from trader_runtime.testing.fake_gateway import ENV_FLAG, FakeSimulationGateway

ORIGIN = "http://127.0.0.1:5173"


@pytest.mark.parametrize("kwargs", [
    {"auth_mode": "owner-password", "owner_username": "owner", "owner_password_hash": "x", "owner_session_secret": "s" * 40,
     "cors_origins": {ORIGIN}},
    {"data_mode": "tqkq-sidecar", "data_provider": None},
])
def test_fake_gateway_refuses_every_non_local_fake_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kwargs) -> None:
    monkeypatch.setenv(ENV_FLAG, "1")
    monkeypatch.setenv("AI_TRADER_READONLY_SIDECAR_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("AI_TRADER_READONLY_SIDECAR_TOKEN", "t" * 40)
    with pytest.raises(RuntimeError, match="fake_simulation_gateway_requires_local_fake_runtime"):
        create_app(tmp_path / "runtime.sqlite3", local_token="token", agent_mode="fake", **kwargs)


def test_fake_gateway_refuses_to_replace_an_injected_gateway(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_FLAG, "1")
    with pytest.raises(RuntimeError, match="fake_simulation_gateway_requires_local_fake_runtime"):
        create_app(tmp_path / "runtime.sqlite3", local_token="token", agent_mode="fake",
                   simulation_gateway=FakeSimulationGateway())


async def test_local_fake_runtime_with_the_flag_activates_and_stops_ai_execution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_FLAG, "1")
    app = create_app(tmp_path / "runtime.sqlite3", local_token="token", agent_mode="fake")
    assert isinstance(app.state.simulation_orders.gateway, FakeSimulationGateway)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers={"Origin": ORIGIN}) as client:
            login = await client.post("/api/auth/local", headers={"Authorization": "Bearer token"})
            assert login.status_code == 200
            session = (await client.post("/api/sessions", json={"mode": "local"})).json()["sessionId"]
            started = await client.post("/api/ai-execution/start", json={
                "sessionId": session, "symbols": ["CFFEX.IF2609"], "maxOrderVolume": 1, "maxOrders": 10})
            assert started.status_code == 200, started.text
            assert started.json()["status"] == "ACTIVE"
            stopped = await client.post("/api/ai-execution/stop", json={"emergency": True})
            assert stopped.status_code == 200
            state = (await client.get("/api/ai-execution")).json()
            assert state["authorization"]["status"] == "EMERGENCY_STOP"
            assert state["liveTrading"] == "disabled"


def test_without_the_flag_the_local_fake_runtime_has_no_gateway(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_FLAG, raising=False)
    app = create_app(tmp_path / "runtime.sqlite3", local_token="token", agent_mode="fake")
    assert app.state.simulation_orders.gateway is None
