import httpx2 as httpx
import pytest

from trader_runtime.data_providers import (
    SidecarReadOnlyDataProvider,
    fake_market_snapshot,
    provider_read_snapshot,
)
from trader_runtime.sidecar_http import SidecarHttpError

SYMBOL = "SHFE.au2612"


@pytest.mark.parametrize("entry", ["direct", "fresh-snapshot"])
@pytest.mark.parametrize("outcome", ["quote", "not-found", "server-error", "connection-error", "timeout", "invalid-json"])
async def test_quote_errors_keep_their_classification(entry, outcome):
    provider = SidecarReadOnlyDataProvider(
        base_url="http://127.0.0.1:19999", token="offline-quote-fixture-" * 2, symbols=[SYMBOL],
    )
    provider._status["marketDataStatus"] = "normal"
    if entry == "fresh-snapshot":
        # Refresh must use its task-local result, including errors, over this cache.
        provider._markets[SYMBOL] = {"symbol": SYMBOL, "price": 999999}
    calls = []
    quote = {**fake_market_snapshot("", SYMBOL), "price": 824, "dataStatus": "normal", "ageSeconds": 0}

    def respond(request, transport):
        calls.append(transport)
        assert request.method == "GET" and request.url.path == f"/v1/market/{SYMBOL}/quote"
        if outcome == "connection-error":
            raise httpx.ConnectError("offline connection failure", request=request)
        if outcome == "timeout":
            raise httpx.ReadTimeout("offline read timeout", request=request)
        if outcome == "invalid-json":
            return httpx.Response(200, content=b"invalid JSON")
        if outcome in {"not-found", "server-error"}:
            return httpx.Response(404 if outcome == "not-found" else 500, json={"detail": outcome})
        return httpx.Response(200, json=quote)

    async def async_respond(request):
        return respond(request, "async")

    # Both pools are replaced: no entry in this test can reach a socket.
    await provider._http.close()
    provider._http.sync = httpx.Client(base_url=provider._base_url,
        transport=httpx.MockTransport(lambda request: respond(request, "sync")))
    provider._http.async_client = httpx.AsyncClient(base_url=provider._base_url,
        transport=httpx.MockTransport(async_respond))

    def read_and_check():
        if outcome == "quote":
            actual = provider.market_snapshot("quote-turn", SYMBOL)
            assert actual["symbol"] == quote["symbol"] and actual["price"] == quote["price"]
            assert actual["dataStatus"] == "normal"
        else:
            expected = KeyError if outcome == "not-found" else RuntimeError
            with pytest.raises(expected) as caught:
                provider.market_snapshot("quote-turn", SYMBOL)
            assert caught.value.args == ((SYMBOL,) if outcome == "not-found" else ("sidecar_market_quote_unavailable",))
            cause = caught.value.__cause__
            assert isinstance(cause, SidecarHttpError)
            assert cause.status == {"not-found": 404, "server-error": 500, "invalid-json": 200}.get(outcome)

    try:
        if entry == "direct":
            read_and_check()
        else:
            async with provider_read_snapshot(provider, symbols=[SYMBOL], activity=False, refresh=True):
                read_and_check()
        assert calls == ["sync" if entry == "direct" else "async"]
    finally:
        await provider.stop()


@pytest.mark.parametrize("upstream", [404, 503, "connection", "invalid-json", "ready"])
async def test_chart_http_failure_is_controlled_at_the_real_rest_entry(tmp_path, upstream):
    from trader_runtime.main import create_app
    provider = SidecarReadOnlyDataProvider(base_url="http://127.0.0.1:19999", token="offline-chart-fixture-" * 2, symbols=[SYMBOL])
    calls = []
    def respond(request):
        if request.url.path == "/v1/instruments":
            from trader_runtime.data_providers import fake_instrument_catalog
            return httpx.Response(200, json=fake_instrument_catalog())
        if request.url.path == "/v1/public-market/calendar":  # the catalog's delivery reminders (2026-10-12)
            from trader_runtime.data_providers import fake_market_calendar
            return httpx.Response(200, json=fake_market_calendar())
        calls.append(request.url.path)
        assert request.url.path == f"/v1/market/{SYMBOL}/workbench"
        if upstream == "connection":
            raise httpx.ConnectError("offline", request=request)
        if upstream == "invalid-json":
            return httpx.Response(200, content=b"invalid")
        if upstream == "ready":
            return httpx.Response(200, json={"schemaVersion": 1, "quote": {"symbol": SYMBOL}, "bars": []})
        return httpx.Response(upstream, json={"error": "market_quote_not_ready"})
    await provider._http.close()
    provider._http.sync = httpx.Client(base_url=provider._base_url, transport=httpx.MockTransport(respond))
    provider._http.async_client = httpx.AsyncClient(base_url=provider._base_url, transport=httpx.MockTransport(respond))
    app = create_app(tmp_path / "runtime.sqlite3", runtime_state_root=tmp_path, agent_mode="fake", data_provider=provider, local_token="chart-entry-test-token")
    try:
        # No lifespan/real Sidecar connection; the actual route uses this actual
        # provider and its HTTPX transport. No copy of the exception block.
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:8000",
                headers={"Authorization": "Bearer chart-entry-test-token", "Origin": "http://127.0.0.1:5173"}) as client:
            response = await client.get(f"/api/markets/{SYMBOL}/workbench?duration=60")
        assert response.status_code == (200 if upstream == "ready" else 404 if upstream == 404 else 503)
        if response.status_code == 503:
            # A per-contract upstream code reaches the client; transport failures stay generic.
            assert response.json()["detail"] == ("market_quote_not_ready" if isinstance(upstream, int) else "sidecar_market_workbench_unavailable")
        assert len(calls) == 1  # Mapping must never introduce retries.
    finally:
        await app.state.shared_market.close()
        await provider.stop()
        app.state.store.close()


@pytest.mark.parametrize("path,upstream_path", [("/api/account/activity", "/v1/activity"), ("/api/instruments", "/v1/instruments")])
async def test_remaining_read_routes_preserve_service_unavailable(tmp_path, path, upstream_path):
    from trader_runtime.main import create_app
    provider = SidecarReadOnlyDataProvider(base_url="http://127.0.0.1:19999", token="offline-read-fixture-" * 2, symbols=[SYMBOL])
    calls = []
    def respond(request):
        calls.append(request.url.path)
        return httpx.Response(503, json={"error": "snapshot_unavailable"})
    await provider._http.close()
    provider._http.sync = httpx.Client(base_url=provider._base_url, transport=httpx.MockTransport(respond))
    provider._http.async_client = httpx.AsyncClient(base_url=provider._base_url, transport=httpx.MockTransport(respond))
    app = create_app(tmp_path / "runtime.sqlite3", runtime_state_root=tmp_path, agent_mode="fake", data_provider=provider, local_token="read-entry-test-token")
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:8000",
                headers={"Authorization": "Bearer read-entry-test-token", "Origin": "http://127.0.0.1:5173"}) as client:
            response = await client.get(path)
        assert response.status_code == 503
        assert calls == [upstream_path]
    finally:
        await provider.stop()
        app.state.store.close()


async def test_incomplete_cached_quote_never_becomes_zero_or_invalidates_account():
    provider = SidecarReadOnlyDataProvider(base_url="http://127.0.0.1:19999",
        token="offline-quote-fixture-" * 2, symbols=[SYMBOL])
    provider._status.update(accountStatus="normal", marketDataStatus="stale")
    provider._markets[SYMBOL] = {"symbol": SYMBOL, "price": None, "dataStatus": "stale"}
    try:
        with pytest.raises(RuntimeError, match="sidecar_market_quote_unavailable"):
            provider.market_snapshot("", SYMBOL)
        assert provider.runtime_status()["accountStatus"] == "normal"
    finally:
        await provider.stop()


async def test_polling_missing_quote_still_publishes_fresh_account():
    provider = SidecarReadOnlyDataProvider(base_url="http://127.0.0.1:19999",
        token="offline-quote-fixture-" * 2, symbols=[SYMBOL])
    def respond(request):
        if request.url.path == "/v1/status":
            data = {"dataStatus": "normal", "accountStatus": "normal", "marketDataStatus": "stale",
                    "dataConnectionStage": "waiting_market", "dataErrorCode": "market_quote_not_ready"}
        elif request.url.path == "/v1/account":
            data = {"equity": 1234567, "ageSeconds": 0}
        elif request.url.path == "/v1/positions":
            data = []
        else:
            assert request.url.path == f"/v1/market/{SYMBOL}"
            data = {"symbol": SYMBOL, "price": None, "dataStatus": "stale"}
            provider._stop.set()
        return httpx.Response(200, json=data)
    await provider._http.close()
    provider._http.sync = httpx.Client(base_url=provider._base_url, transport=httpx.MockTransport(respond))
    try:
        provider._run()
        assert provider.account_summary("")["equity"] == 1234567
        assert provider.account_summary("")["dataStatus"] == "normal"
        assert provider.runtime_status()["marketDataStatus"] == "stale"
        with pytest.raises(RuntimeError, match="sidecar_market_quote_unavailable"):
            provider.market_snapshot("", SYMBOL)
    finally:
        await provider.stop()
