import base64
from uuid import uuid4

import httpx
import pytest

from trader_runtime.main import create_app
from trader_runtime.ui_control import ChartOptions


@pytest.mark.parametrize("changed", [False, True])
async def test_capture_requires_native_receipt_and_matching_window_revision(tmp_path, changed):
    app = create_app(tmp_path / "runtime.sqlite3", runtime_state_root=tmp_path,
        agent_mode="fake", local_token="capture-test-token")
    headers = {"Authorization": "Bearer capture-test-token", "Origin": "http://127.0.0.1:5173"}
    identifier, request_id = str(uuid4()), str(uuid4())
    state = dict(symbol="SHFE.rb2610", duration=60, view="trade", panel=None, settingsOpen=False,
        theme="dark", dockSide="right", dockWidth=400, replay=False, settingsPage="connection",
        fontScale="large", density="standard", chart={k: True for k in ChartOptions.model_fields},
        chartSizes=dict(main=58, kd=19, macd=23), strategy=None)
    body = {"windowId": identifier, "revision": 0, "state": state,
        "device": {"deviceId": "device-a", "platform": "macos", "version": "test", "nativeCapture": True},
        "observation": {"capturedAt": "2026-09-08T00:00:00Z", "viewport": {"width": 1280, "height": 720},
            "text": "交易台", "truncated": False, "regions": [], "controls": [], "context": {}}}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://runtime", headers=headers) as client:
        assert (await client.post("/api/ui/pulse", json=body)).status_code == 200
        assert not (await client.get("/api/app/ui/windows?deviceId=device-b")).json()["windows"]
        queued = await client.post(f"/api/app/ui/windows/{identifier}/captures", json={"requestId": request_id, "expectedRevision": 0})
        assert queued.json()["status"] == "queued" and "image" not in queued.json()
        assert (await client.post("/api/ui/pulse", json=body)).json()["capture"]["requestId"] == request_id
        receipt = {"requestId": request_id, "observedRevision": int(changed), "capturedAt": "2026-09-08T00:00:01Z",
            "image": {"mimeType": "image/png", "data": base64.b64encode(b"\x89PNG\r\n\x1a\nprobe").decode()}}
        assert (await client.post("/api/ui/pulse", json={**body, "revision": int(changed), "captureReceipt": receipt})).status_code == 200
        result = (await client.get(f"/api/app/ui/captures/{request_id}")).json()
        assert result["status"] == ("rejected" if changed else "ready")
        assert ("image" in result) is (not changed)
        assert (await client.post("/api/agent/call", json={"operation": "POST /api/ui/pulse", "body": body})).status_code == 404
        await client.post("/api/ui/pulse", json={**body, "revision": int(changed), "closing": True})
        assert (await client.get(f"/api/app/ui/captures/{request_id}")).status_code == 410
    app.state.store.close()


async def test_pool_reuses_connection_and_rotates_auth_without_interrupting_inflight_request(tmp_path):
    import asyncio
    import json

    from trader_runtime.app_connection import HostHttpPool
    path = tmp_path / "connection.json"
    def save(token):
        path.write_text(json.dumps({"url": "http://127.0.0.1:8000", "token": token}))
        path.chmod(0o600)
    started, release = asyncio.Event(), asyncio.Event()
    clients, seen = [], []
    async def handler(request):
        seen.append(request.headers["authorization"])
        if request.url.path == "/slow":
            started.set()
            await release.wait()
        return httpx.Response(200, json={"ok": True})
    def factory(**options):
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler), **options)
        clients.append(client)
        return client
    save("first-token")
    pool = HostHttpPool(path, client_factory=factory)
    try:
        await pool.request("GET", "/one")
        pending = asyncio.create_task(pool.request("GET", "/slow"))
        await started.wait()
        save("second-token")
        await pool.request("GET", "/two")
        assert len(clients) == 2 and not clients[0].is_closed
        release.set()
        await pending
        assert clients[0].is_closed
        assert seen == ["Bearer first-token", "Bearer first-token", "Bearer second-token"]
    finally:
        release.set()
        await pool.aclose()
    assert all(client.is_closed for client in clients)


async def test_page_changed_receipt_names_the_visible_parts_that_moved(tmp_path):
    app = create_app(tmp_path / "runtime.sqlite3", runtime_state_root=tmp_path,
        agent_mode="fake", local_token="capture-test-token")
    headers = {"Authorization": "Bearer capture-test-token", "Origin": "http://127.0.0.1:5173"}
    identifier, request_id = str(uuid4()), str(uuid4())
    state = dict(symbol="SHFE.rb2610", duration=60, view="trade", panel=None, settingsOpen=False,
        theme="dark", dockSide="right", dockWidth=400, replay=False, settingsPage="connection",
        fontScale="large", density="standard", chart={k: True for k in ChartOptions.model_fields},
        chartSizes=dict(main=58, kd=19, macd=23), strategy=None)
    body = {"windowId": identifier, "revision": 0, "state": state,
        "device": {"deviceId": "device-a", "platform": "macos", "version": "test", "nativeCapture": True},
        "observation": {"capturedAt": "2026-09-08T00:00:00Z", "viewport": {"width": 1280, "height": 720},
            "text": "交易台", "truncated": False, "regions": [], "controls": [], "context": {}}}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://runtime", headers=headers) as client:
        assert (await client.post("/api/ui/pulse", json=body)).status_code == 200
        await client.post(f"/api/app/ui/windows/{identifier}/captures", json={"requestId": request_id, "expectedRevision": 0})
        assert (await client.post("/api/ui/pulse", json=body)).json()["capture"]["requestId"] == request_id
        receipt = {"requestId": request_id, "observedRevision": 0, "capturedAt": "2026-09-08T00:00:01Z",
                   "error": "page_changed", "detail": "text,controls"}
        assert (await client.post("/api/ui/pulse", json={**body, "captureReceipt": receipt})).status_code == 200
        result = (await client.get(f"/api/app/ui/captures/{request_id}")).json()
        assert result["status"] == "rejected" and result["error"] == "截图期间页面已变化（text,controls）"
        # A receipt without detail (older client) keeps the plain message.
        request_id = str(uuid4())
        await client.post(f"/api/app/ui/windows/{identifier}/captures", json={"requestId": request_id, "expectedRevision": 0})
        await client.post("/api/ui/pulse", json=body)
        await client.post("/api/ui/pulse", json={**body, "captureReceipt": {**receipt, "requestId": request_id, "detail": None}})
        assert (await client.get(f"/api/app/ui/captures/{request_id}")).json()["error"] == "截图期间页面已变化"
    app.state.store.close()
