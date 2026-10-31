"""Pick the authentication mode and expose the request → user resolution used by every route."""
from __future__ import annotations

import asyncio
import os

from fastapi import Request

from ..auth import LOCAL_USER_ID, CloudAuth, LocalAuth, OwnerAuth, OwnerIdentity
from ..store import RuntimeStore
from .config import RuntimeSettings

AuthService = LocalAuth | CloudAuth | OwnerAuth


def default_execution_owner_user_id(settings: RuntimeSettings, owner_username: str | None) -> str:
    """Who owns the default trading account: the resolved owner in owner-password
    mode, the configured owner (if any) in cloud-jwt mode, otherwise the local user."""
    username = (owner_username or os.getenv("AI_TRADER_OWNER_USERNAME") or "").strip()
    if settings.auth_mode == "owner-password":
        if not username:
            raise RuntimeError("owner_username_required")
        return OwnerIdentity(username).scoped_user_id
    if settings.auth_mode == "cloud-jwt" and username:
        return OwnerIdentity(username).scoped_user_id
    return LOCAL_USER_ID


def build_auth(
    settings: RuntimeSettings, store: RuntimeStore, *, local_token: str | None, cloud_jwt_secret: str | None,
    owner_username: str | None, owner_password_hash: str | None, owner_session_secret: str | None,
    registration_enabled: bool | None, replay_auth: AuthService | None,
) -> AuthService:
    if settings.auth_mode == "cloud-jwt":
        auth: LocalAuth | CloudAuth | OwnerAuth = CloudAuth(
            cloud_jwt_secret,
            cross_site_cookie_origins=settings.origins,
        )
    elif settings.auth_mode == "owner-password":
        auth = OwnerAuth(
            username=owner_username,
            password_hash=owner_password_hash,
            session_secret=owner_session_secret,
            allowed_login_origins=settings.origins,
            session_store=store,
            # The store backs the classic share-code registration (still gated
            # by its flag inside OwnerAuth) and the M1b TqKq direct login,
            # which works with registration off.
            user_store=store,
            registration_enabled=registration_enabled,
        )
    elif settings.auth_mode == "local-token":
        auth = LocalAuth(local_token)
    else:
        raise ValueError("invalid_auth_mode")
    if replay_auth is not None:
        auth = replay_auth
    return auth


def current_user_resolver(auth: AuthService):
    async def current_user(request: Request) -> str:
        if isinstance(auth, CloudAuth):
            # OIDC verification can refresh JWKS after cache expiry or for a
            # previously unseen kid. Keep that bounded network I/O away from
            # the single Runtime event loop for every protected route.
            return await asyncio.to_thread(auth.user, request)
        return auth.user(request)
    return current_user
