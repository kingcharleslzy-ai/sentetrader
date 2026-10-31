from __future__ import annotations

import base64
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from jwt.algorithms import RSAAlgorithm

from trader_runtime.auth import CloudAuth
from trader_runtime.main import create_app


class MockOidcIssuer:
    def __init__(self) -> None:
        self.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        jwk = json.loads(RSAAlgorithm.to_jwk(self.private_key.public_key()))
        self.key_id = "phase-2-test-key"
        self.jwks = {"keys": [{**jwk, "kid": self.key_id, "use": "sig", "alg": "RS256"}]}
        self.server: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None
        self.issuer = ""
        self.jwks_requests = 0

    def __enter__(self) -> MockOidcIssuer:
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - stdlib handler contract
                if self.path == "/.well-known/openid-configuration":
                    body: dict[str, Any] = {
                        "issuer": fixture.issuer,
                        "jwks_uri": f"{fixture.issuer.rstrip('/')}/jwks",
                        "id_token_signing_alg_values_supported": ["RS256"],
                    }
                elif self.path == "/jwks":
                    fixture.jwks_requests += 1
                    body = fixture.jwks
                else:
                    self.send_error(404)
                    return
                payload = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "public, max-age=300")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *_: object) -> None:
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.issuer = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        assert self.server is not None
        self.server.shutdown()
        self.server.server_close()
        assert self.thread is not None
        self.thread.join(timeout=2)

    def issue(
        self,
        *,
        tenant: str = "tenant-a",
        subject: str = "alice",
        audience: str = "ai-trader",
        expires_in: int = 300,
        algorithm: str = "RS256",
        kid: str | None = None,
    ) -> str:
        now = int(time.time())
        key: object = self.private_key
        headers = {"kid": kid or self.key_id, "typ": "JWT"}
        if algorithm == "HS256":
            key = "not-an-allowed-oidc-secret-with-32-bytes"
            headers = {"typ": "JWT"}
        return jwt.encode(
            {
                "iss": self.issuer,
                "aud": audience,
                "tid": tenant,
                "sub": subject,
                "iat": now,
                "exp": now + expires_in,
            },
            key,
            algorithm=algorithm,
            headers=headers,
        )


@pytest.mark.asyncio
async def test_oidc_discovery_jwks_and_cookie_exchange(tmp_path: Path, monkeypatch) -> None:
    origin = "https://app.example.test"
    with MockOidcIssuer() as issuer:
        monkeypatch.setenv("AI_TRADER_CLOUD_AUTH_PROVIDER", "oidc")
        monkeypatch.setenv("AI_TRADER_OIDC_ISSUER", issuer.issuer)
        monkeypatch.setenv("AI_TRADER_OIDC_AUDIENCE", "ai-trader")
        monkeypatch.setenv("AI_TRADER_OIDC_JWKS_URL", "")
        monkeypatch.setenv("AI_TRADER_ALLOW_INSECURE_LOCAL_OIDC", "1")
        monkeypatch.delenv("AI_TRADER_CLOUD_JWT_SECRET", raising=False)
        application = create_app(
            tmp_path / "oidc.sqlite3",
            agent_mode="fake",
            auth_mode="cloud-jwt",
            cors_origins={origin},
        )
        try:
            async with AsyncClient(
                transport=ASGITransport(app=application),
                base_url="https://runtime.example.test",
                headers={"Origin": origin},
            ) as client:
                login = await client.post(
                    "/api/auth/cloud",
                    headers={"Authorization": f"Bearer {issuer.issue()}"},
                )
                assert login.status_code == 200
                assert login.json()["authProvider"] == "oidc"
                assert "HttpOnly" in login.headers["set-cookie"]
                assert "Secure" in login.headers["set-cookie"]
                status = await client.get("/api/runtime/status", params={"mode": "cloud"})
                assert status.status_code == 200
                assert status.json()["authProvider"] == "oidc"
                assert status.json()["liveTrading"] is False
        finally:
            application.state.store.close()


def test_oidc_rejects_wrong_audience_and_symmetric_algorithms() -> None:
    with MockOidcIssuer() as issuer:
        auth = CloudAuth(
            provider="oidc",
            issuer=issuer.issuer,
            audience="ai-trader",
            allow_insecure_local_oidc=True,
        )
        with pytest.raises(HTTPException) as wrong_audience:
            auth.validate(issuer.issue(audience="another-service"))
        assert wrong_audience.value.status_code == 401
        assert wrong_audience.value.detail == "invalid_cloud_token"

        with pytest.raises(HTTPException) as symmetric:
            auth.validate(issuer.issue(algorithm="HS256"))
        assert symmetric.value.status_code == 401
        assert symmetric.value.detail == "invalid_cloud_token"


def test_oidc_rejects_deeply_nested_header_token() -> None:
    # GHSA-8wjv-2p76-3863: PyJWT 2.13 let a deeply nested header raise an
    # uncaught RecursionError; >= 2.14 raises DecodeError, which the auth
    # entrypoint maps to a 401 rejection instead of a crash.
    with MockOidcIssuer() as issuer:
        auth = CloudAuth(
            provider="oidc",
            issuer=issuer.issuer,
            audience="ai-trader",
            allow_insecure_local_oidc=True,
        )
        depth = 20000
        nested = '{"a":' * depth + "null" + "}" * depth
        header = '{"typ":"JWT","alg":"RS256","kid":"phase-2-test-key","deep":' + nested + "}"
        segment = base64.urlsafe_b64encode(header.encode()).rstrip(b"=").decode()
        token = f"{segment}.e30.ZQ"
        with pytest.raises(HTTPException) as rejected:
            auth.validate(token)
        assert rejected.value.status_code == 401
        assert rejected.value.detail == "invalid_cloud_token"


def test_oidc_preserves_exact_trailing_slash_and_bounds_signing_key_cache() -> None:
    with MockOidcIssuer() as issuer:
        issuer.issuer = f"{issuer.issuer}/"
        auth = CloudAuth(
            provider="oidc",
            issuer=issuer.issuer,
            audience="ai-trader",
            allow_insecure_local_oidc=True,
        )
        assert auth.issuer == issuer.issuer
        assert auth.validate(issuer.issue()).subject == "alice"
        assert auth._jwks_client is not None
        assert not hasattr(auth._jwks_client.get_signing_key, "cache_info")


def test_oidc_unknown_kids_share_one_rate_limited_jwks_refresh() -> None:
    with MockOidcIssuer() as issuer:
        auth = CloudAuth(
            provider="oidc",
            issuer=issuer.issuer,
            audience="ai-trader",
            allow_insecure_local_oidc=True,
        )
        assert auth.validate(issuer.issue()).subject == "alice"
        assert issuer.jwks_requests == 1

        for kid in ("forged-kid-one", "forged-kid-two", "forged-kid-three"):
            with pytest.raises(HTTPException) as rejected:
                auth.validate(issuer.issue(kid=kid))
            assert rejected.value.status_code == 401

        assert issuer.jwks_requests == 2


def test_oidc_configuration_fails_closed_without_https_or_explicit_local_exception(
    monkeypatch,
) -> None:
    monkeypatch.delenv("AI_TRADER_CLOUD_JWT_SECRET", raising=False)
    with pytest.raises(RuntimeError, match="cloud_oidc_configuration_required"):
        CloudAuth(provider="oidc")
    with pytest.raises(RuntimeError, match="cloud_oidc_https_required"):
        CloudAuth(
            provider="oidc",
            issuer="http://identity.example.test",
            audience="ai-trader",
            oidc_jwks_url="http://identity.example.test/jwks",
        )
    with pytest.raises(RuntimeError, match="cloud_oidc_https_required"):
        CloudAuth(
            provider="oidc",
            issuer="http://127.0.0.1:4455",
            audience="ai-trader",
            oidc_jwks_url="http://127.0.0.1:4455/jwks",
        )


def test_development_hs256_is_explicit_and_keeps_test_issuer() -> None:
    auth = CloudAuth(
        "phase-2-development-secret-with-more-than-32-bytes",
        provider="development-hs256",
    )
    token = auth.issue_for_test(tenant_id="tenant-a", subject="alice")
    assert auth.validate(token).subject == "alice"
    assert auth.provider == "development-hs256"
