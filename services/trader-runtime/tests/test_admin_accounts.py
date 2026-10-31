"""The owner's read-only account & grant check (`GET /api/admin/accounts`).

Same owner-password Runtime shape as the tier tests: the assertions describe
what a deployment observes — the owner reads every trading account with its
grants as usernames, a member does not even learn the surface exists, and the
listing never changes anything.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from trader_runtime.auth import hash_owner_password
from trader_runtime.data_providers import FakeReadOnlyDataProvider
from trader_runtime.main import create_app
from trader_runtime.store.trading_accounts import scoped_owner_user_id

ORIGIN = "https://qilinshuzhi.com"
PASSWORD = "correct-demo-password"
PASSWORD_HASH = hash_owner_password(PASSWORD, salt=b"0123456789abcdef")
SESSION_SECRET = "owner-session-secret-for-tests-more-than-32-bytes"
FRIEND_PASSWORD = "a-solid-password-123"


def owner_headers() -> dict[str, str]:
    return {"origin": ORIGIN}


@pytest.fixture
def accounts_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ZHIPU_API_KEY", "sk-zhipu-fixture")
    application = create_app(
        tmp_path / "accounts.sqlite3",
        agent_mode="fake",
        auth_mode="owner-password",
        owner_username="charles",
        owner_password_hash=PASSWORD_HASH,
        owner_session_secret=SESSION_SECRET,
        cors_origins={ORIGIN},
        data_mode="tqkq-sidecar",
        data_provider=FakeReadOnlyDataProvider(),
        agent_provider="deepseek",
        registration_enabled=True,
        deepseek_api_key="sk-test-deepseek-key-000001",
    )
    yield application
    application.state.store.close()


@pytest.fixture
async def friend_and_owner(accounts_app):
    transport = ASGITransport(app=accounts_app)
    async with (
        AsyncClient(transport=transport, base_url="http://test") as friend,
        AsyncClient(transport=transport, base_url="http://test") as owner,
    ):
        login = await owner.post(
            "/api/auth/owner", json={"username": "charles", "password": PASSWORD},
            headers=owner_headers(),
        )
        assert login.status_code == 200
        code = (await owner.post(
            "/api/auth/owner/invite-codes", json={"daysValid": 14}, headers=owner_headers(),
        )).json()["inviteCode"]
        registered = await friend.post(
            "/api/auth/owner/register",
            json={"username": "friend", "password": FRIEND_PASSWORD, "inviteCode": code},
            headers=owner_headers(),
        )
        assert registered.status_code == 200
        # An isolation check that shows something needs a grant to show: the
        # same explicit path an accounts-file declaration takes.
        accounts_app.state.store.grant_account(
            account_id=accounts_app.state.store.default_trading_account_id,
            user_id=scoped_owner_user_id("friend"), role="viewer",
            granted_by=scoped_owner_user_id("charles"),
        )
        yield friend, owner


async def test_admin_accounts_lists_accounts_and_grants_for_the_owner(accounts_app, friend_and_owner):
    friend, owner = friend_and_owner
    response = await owner.get("/api/admin/accounts", headers=owner_headers())
    assert response.status_code == 200
    items = response.json()["items"]
    account = next(item for item in items
                   if item["id"] == accounts_app.state.store.default_trading_account_id)
    assert account["displayName"] == "TqKq 模拟账户"
    assert account["providerKind"] == "tqkq-sidecar"
    assert account["status"] == "active"
    # Usernames, not the opaque scoped ids the grant table stores.
    assert account["executionOwner"] == "charles"
    assert {"user": "charles", "role": "owner"} in account["grants"]
    assert {"user": "friend", "role": "viewer"} in account["grants"]


async def test_admin_accounts_is_owner_only(accounts_app, friend_and_owner):
    friend, owner = friend_and_owner
    # Not even discoverable for a member; anonymous gets 401.
    assert (await friend.get("/api/admin/accounts", headers=owner_headers())).status_code == 404
    async with AsyncClient(transport=ASGITransport(app=accounts_app), base_url="http://test") as anonymous:
        assert (await anonymous.get("/api/admin/accounts")).status_code == 401
    # Nothing about the read changed the grant table.
    grants = accounts_app.state.store.list_account_grants(
        user_id=scoped_owner_user_id("friend"))
    assert [grant["role"] for grant in grants] == ["viewer"]
