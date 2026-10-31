from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import time
from typing import Any
from urllib.parse import urlsplit

import httpx


def encode(value: dict[str, Any]) -> str:
    payload = json.dumps(value, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(payload).rstrip(b"=").decode()


def issue_token(*, subject: str) -> str:
    secret = os.environ.get("AI_TRADER_CLOUD_JWT_SECRET", "")
    if len(secret.encode()) < 32:
        raise RuntimeError("cloud_smoke_secret_too_short")
    now = int(time.time())
    header = encode({"alg": "HS256", "typ": "JWT"})
    payload = encode({
        "iss": os.getenv("AI_TRADER_CLOUD_JWT_ISSUER", "ai-trader-cloud"),
        "aud": os.getenv("AI_TRADER_CLOUD_JWT_AUDIENCE", "ai-trader"),
        "tid": os.getenv("AI_TRADER_CLOUD_TENANT_ID", "demo-tenant"),
        "sub": subject,
        "iat": now,
        "exp": now + 600,
    })
    signature = base64.urlsafe_b64encode(hmac.new(
        secret.encode(), f"{header}.{payload}".encode(), hashlib.sha256
    ).digest()).rstrip(b"=").decode()
    return f"{header}.{payload}.{signature}"


def require(condition: bool, code: str) -> None:
    if not condition:
        raise RuntimeError(code)


def authenticate(base_url: str, origin: str, subject: str) -> httpx.Client:
    client = httpx.Client(
        base_url=base_url,
        headers={"Origin": origin},
        timeout=httpx.Timeout(20, read=20),
        follow_redirects=False,
    )
    response = client.post(
        "/api/auth/cloud",
        headers={"Authorization": f"Bearer {issue_token(subject=subject)}"},
    )
    require(response.status_code == 200, f"cloud_login_failed:{response.status_code}")
    require(response.json().get("authProvider") == "development-hs256", "unexpected_auth_provider")
    return client


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--issue-token", action="store_true")
    parser.add_argument("--subject", default="compose-browser")
    args = parser.parse_args()
    if args.issue_token:
        print(issue_token(subject=args.subject))
        return

    base_url = os.getenv("AI_TRADER_CLOUD_SMOKE_URL", "http://127.0.0.1:8080").rstrip("/")
    parsed = urlsplit(base_url)
    require(parsed.scheme in {"http", "https"} and bool(parsed.netloc), "cloud_smoke_url_invalid")
    origin = os.getenv("AI_TRADER_CLOUD_SMOKE_ORIGIN", base_url)
    health = httpx.get(f"{base_url}/healthz", timeout=20)
    require(health.status_code == 200, "cloud_health_failed")
    health_payload = health.json()
    require(health_payload.get("liveTrading") == "disabled", "live_trading_boundary_failed")
    require(health_payload.get("mode") == "fake", "cloud_agent_must_be_fake")
    require(health_payload.get("authMode") == "cloud-jwt", "cloud_auth_mode_failed")
    version_response = httpx.get(f"{base_url}/version.json", timeout=20)
    require(version_response.status_code == 200, "cloud_version_file_missing")
    version = version_response.json()
    require(version.get("liveTrading") == "disabled", "cloud_version_boundary_failed")
    expected_commit = os.getenv("AI_TRADER_BUILD_SHA")
    if expected_commit:
        require(version.get("commit") == expected_commit, "cloud_deployed_commit_mismatch")

    alice = authenticate(base_url, origin, "compose-alice")
    bob = authenticate(base_url, origin, "compose-bob")
    try:
        created = alice.post("/api/sessions", json={"mode": "cloud", "agentId": "native"})
        require(created.status_code == 201, "cloud_session_create_failed")
        session_id = created.json()["sessionId"]
        status = alice.get("/api/runtime/status", params={"mode": "cloud"})
        require(status.status_code == 200, "cloud_status_failed")
        runtime = status.json()
        require(runtime.get("liveTrading") is False, "runtime_live_trading_boundary_failed")
        require(runtime.get("accountMode") == "fake", "cloud_account_must_be_fake")
        require(bool(runtime.get("agentNotice")), "cloud_fake_agent_must_report_unavailable")

        require(alice.get("/api/account/summary").status_code == 200, "cloud_account_failed")
        require(alice.get("/api/positions").status_code == 200, "cloud_positions_failed")
        require(
            alice.get("/api/market/SHFE.au2612").status_code == 200,
            "cloud_market_failed",
        )

        backtest = alice.post(
            "/api/backtests/reproducible",
            json={"sessionId": session_id, "requestId": "compose-smoke-backtest"},
        )
        require(backtest.status_code == 201, f"cloud_backtest_failed:{backtest.status_code}")
        backtest_id = backtest.json()["backtestId"]
        persisted = alice.get(f"/api/backtests/{backtest_id}")
        require(persisted.status_code == 200, "cloud_backtest_persistence_failed")
        require(persisted.json().get("status") == "completed", "cloud_backtest_not_completed")

        forbidden = bob.get(f"/api/sessions/{session_id}")
        require(forbidden.status_code == 404, "cloud_session_tenant_isolation_failed")
        forbidden_backtest = bob.get(f"/api/backtests/{backtest_id}")
        require(forbidden_backtest.status_code == 404, "cloud_backtest_tenant_isolation_failed")
        require(
            alice.post("/api/orders", json={"symbol": "SHFE.au2612"}).status_code == 404,
            "order_endpoint_must_not_exist",
        )
        deleted = alice.delete(f"/api/sessions/{session_id}")
        require(deleted.status_code == 200, "cloud_session_cleanup_failed")
    finally:
        alice.close()
        bob.close()

    print(json.dumps({
        "status": "passed",
        "environment": "docker-compose-cloud",
        "authProvider": "development-hs256",
        "agent": "fake",
        "data": "fake",
        "tenantIsolation": "passed",
        "backtest": "completed",
        "commit": version.get("commit"),
        "liveTrading": "disabled",
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
