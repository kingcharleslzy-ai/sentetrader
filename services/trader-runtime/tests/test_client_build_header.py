"""Client build reporting: header parsing, the compat verdict and the response headers."""
from uuid import uuid4

import anyio
import httpx
import pytest

from trader_runtime import __version__
from trader_runtime.client_compat import (
    MIN_CLIENT_VERSION,
    ClientBuild,
    compat_state,
    parse_client_header,
    runtime_build,
)
from trader_runtime.main import create_app
from trader_runtime.ui_control import ChartOptions

RUNTIME_SHA = "f8f50e5c0b9d7a2e4d6c8a1b3e5f7d9c0a2b4e6f"


def test_parse_accepts_platform_version_and_optional_build() -> None:
    assert parse_client_header(None) is None
    assert parse_client_header("") is None
    assert parse_client_header("web/1.6.5") == ClientBuild(platform="web", version="1.6.5", commit=None)
    assert parse_client_header(" desktop/1.6.5+deadbeef ") == ClientBuild(
        platform="desktop", version="1.6.5", commit="deadbeef")
    assert parse_client_header("cli/2.0.0+abcdef0123456789abcdef0123456789abcdef01") is not None
    for bad in ("web/1.6", "web/1.6.5.7", "ios/1.6.5", "web/1.6.5+short1", "web/1.6.5+zzzzzzz",
                "Web/1.6.5", "web/1.6.5+toolong" + "a" * 41, "web 1.6.5", "1.6.5"):
        assert parse_client_header(bad) is None, bad


def test_compat_state_only_judges_the_client_version() -> None:
    assert compat_state(None) == "unknown"
    assert compat_state(parse_client_header("web/1.6.4")) == "outdated"
    assert compat_state(parse_client_header("web/1.6.4+deadbee")) == "outdated"
    # The build sha plays no part here: whether a web page lags the deployed web build is
    # the page's own comparison against version.json, not the Runtime's (separate deploys).
    assert compat_state(parse_client_header(f"web/{MIN_CLIENT_VERSION}+deadbee")) == "ok"
    assert compat_state(parse_client_header(f"web/{MIN_CLIENT_VERSION}+{RUNTIME_SHA[:7]}")) == "ok"
    assert compat_state(parse_client_header(f"desktop/{MIN_CLIENT_VERSION}+deadbee")) == "ok"
    assert compat_state(parse_client_header("cli/2.0.0")) == "ok"


def test_runtime_build_reports_version_and_short_sha(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AI_TRADER_BUILD_SHA", RUNTIME_SHA)
    assert runtime_build() == f"{__version__}+{RUNTIME_SHA[:7]}"
    monkeypatch.delenv("AI_TRADER_BUILD_SHA")
    assert runtime_build() == f"{__version__}+dev"


async def test_responses_carry_compat_headers(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("AI_TRADER_BUILD_SHA", RUNTIME_SHA)
    app = create_app(tmp_path / "runtime.sqlite3", runtime_state_root=tmp_path,
        agent_mode="fake", local_token="compat-token")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://runtime") as client:
        # No client header → unknown, no min-client header.
        health = await client.get("/healthz")
        assert health.headers["x-ai-trader-client-compat"] == "unknown"
        assert health.headers["x-ai-trader-runtime"] == f"{__version__}+{RUNTIME_SHA[:7]}"
        assert "x-ai-trader-min-client" not in health.headers
        assert health.json()["minClientVersion"] == MIN_CLIENT_VERSION

        client_headers = {"Authorization": "Bearer compat-token",
                          "X-AI-Trader-Client": f"web/{MIN_CLIENT_VERSION}+deadbee"}
        windows = await client.get("/api/app/ui/windows", headers=client_headers)
        assert windows.status_code == 200
        assert windows.headers["x-ai-trader-client-compat"] == "ok"
        assert "x-ai-trader-min-client" not in windows.headers

        outdated = await client.get("/api/app/ui/windows",
                                    headers={**client_headers, "X-AI-Trader-Client": "web/1.6.4"})
        assert outdated.headers["x-ai-trader-client-compat"] == "outdated"
        assert outdated.headers["x-ai-trader-min-client"] == MIN_CLIENT_VERSION

        # First-seen logging notes each distinct build once; the set stays bounded.
        seen = app.state.client_builds_seen
        assert ("web", MIN_CLIENT_VERSION, "deadbee") in seen
        seen.clear()
        assert (await client.get("/healthz", headers=client_headers)).status_code == 200
        assert len(seen) == 1
        assert (await client.get("/healthz", headers=client_headers)).status_code == 200
        assert len(seen) == 1
    app.state.store.close()


async def test_cors_preflight_allows_client_header_and_exposes_answers(tmp_path) -> None:
    origin = "http://127.0.0.1:5173"
    app = create_app(tmp_path / "runtime.sqlite3", runtime_state_root=tmp_path,
        agent_mode="fake", local_token="compat-token", cors_origins={origin})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://runtime") as client:
        preflight = await client.options("/api/app/ui/windows", headers={
            "Origin": origin, "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "x-ai-trader-client, authorization"})
        assert preflight.status_code == 200
        allow = {name.strip().lower() for name in preflight.headers["access-control-allow-headers"].split(",")}
        assert "x-ai-trader-client" in allow

        # Starlette puts expose-headers on actual responses (where browsers read it), not preflight.
        response = await client.get("/healthz", headers={"Origin": origin})
        assert response.headers["access-control-allow-origin"] == origin
        exposed = {name.strip().lower() for name in response.headers["access-control-expose-headers"].split(",")}
        assert exposed >= {"x-ai-trader-client-compat", "x-ai-trader-runtime", "x-ai-trader-min-client"}
        assert response.headers["x-ai-trader-client-compat"] == "unknown"
    app.state.store.close()


async def test_pulse_device_build_rides_the_window_snapshot(tmp_path) -> None:
    app = create_app(tmp_path / "runtime.sqlite3", runtime_state_root=tmp_path,
        agent_mode="fake", local_token="compat-token")
    headers = {"Authorization": "Bearer compat-token", "Origin": "http://127.0.0.1:5173"}
    identifier = str(uuid4())
    state = dict(symbol=None, duration=60, view="trade", panel=None, settingsOpen=False,
        theme="dark", dockSide="right", dockWidth=400, replay=False, settingsPage="account",
        fontScale="standard", density="standard", chart={k: True for k in ChartOptions.model_fields},
        chartSizes=dict(main=58, kd=19, macd=23), strategy=None)
    observation = {"capturedAt": "2026-09-20T00:00:00Z", "viewport": {"width": 1280, "height": 720},
        "text": "交易台", "truncated": False, "regions": [], "controls": [], "context": {}}

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://runtime",
                                headers=headers) as client:
        # An older client omits `build`; the pulse must still validate.
        legacy = {"windowId": identifier, "revision": 0, "state": state,
            "device": {"deviceId": "device-legacy", "platform": "macos", "version": "1.6.4", "nativeCapture": False},
            "observation": observation}
        assert (await client.post("/api/ui/pulse", json=legacy)).status_code == 200
        windows = (await client.get("/api/app/ui/windows?deviceId=device-legacy")).json()["windows"]
        assert windows and windows[0]["device"]["build"] is None

        current = {**legacy, "device": {"deviceId": "device-current", "platform": "web",
            "version": "1.6.5", "build": "deadbee", "nativeCapture": False}}
        assert (await client.post("/api/ui/pulse", json=current)).status_code == 200
        windows = (await client.get("/api/app/ui/windows?deviceId=device-current")).json()["windows"]
        assert windows[0]["device"]["build"] == "deadbee"
    app.state.store.close()


@pytest.mark.parametrize("when", ["start", "body"])
@pytest.mark.parametrize("error", [ConnectionResetError("peer closed the socket"),
                                   anyio.BrokenResourceError("stream closed")])
async def test_a_client_disconnecting_mid_response_is_not_a_server_error(
        monkeypatch: pytest.MonkeyPatch, tmp_path, when: str, error: Exception) -> None:
    # The hourly 500 (`ExceptionGroup` through starlette's BaseHTTPMiddleware, no project
    # frame) was a client vanishing mid-response. With pure-ASGI middlewares such a request
    # completes quietly: no exception escapes the app, and no 500 response is attempted.
    monkeypatch.setenv("AI_TRADER_BUILD_SHA", RUNTIME_SHA)
    app = create_app(tmp_path / "runtime.sqlite3", runtime_state_root=tmp_path,
        agent_mode="fake", local_token="compat-token")
    sent: list[str] = []

    async def disconnecting_send(message) -> None:
        sent.append(message["type"])
        if message["type"] == f"http.response.{when}":
            raise error

    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    scope = {"type": "http", "asgi": {"version": "2.3", "spec_version": "2.3"},
             "http_version": "1.1", "method": "GET", "scheme": "http", "path": "/healthz",
             "raw_path": b"/healthz", "query_string": b"", "root_path": "",
             "headers": [(b"host", b"runtime")], "client": ("127.0.0.1", 1), "server": ("runtime", 80)}
    await app(scope, receive, disconnecting_send)  # no raise, no 500 answer
    # Exactly one response start was attempted — a 500 rebuild would add another.
    if when == "start":
        assert sent == ["http.response.start"]
    else:
        assert sent == ["http.response.start", "http.response.body"]
    app.state.store.close()


async def test_state_changing_calls_still_require_an_allowed_origin(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    # The origin gate moved from a function middleware into OriginAndClientCompat;
    # the protection itself must be unchanged: foreign origins are refused on
    # every write, allowed origins pass, and the refusal still carries the verdict.
    monkeypatch.setenv("AI_TRADER_BUILD_SHA", RUNTIME_SHA)
    app = create_app(tmp_path / "runtime.sqlite3", runtime_state_root=tmp_path,
        agent_mode="fake", local_token="compat-token")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://runtime") as client:
        refused = await client.post("/api/sessions", json={"mode": "local"},
                                    headers={"Origin": "https://evil.example",
                                             "Authorization": "Bearer compat-token",
                                             "X-AI-Trader-Client": "web/1.6.5"})
        assert refused.status_code == 403
        assert refused.json() == {"detail": "origin_not_allowed"}
        assert refused.headers["x-ai-trader-client-compat"] == "ok"  # the 403 answers too
        allowed = await client.post("/api/sessions", json={"mode": "local"},
                                    headers={"Origin": "http://127.0.0.1:5173",
                                             "Authorization": "Bearer compat-token",
                                             "X-AI-Trader-Client": "web/1.6.5"})
        assert allowed.status_code != 403
    app.state.store.close()
