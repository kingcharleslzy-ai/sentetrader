from __future__ import annotations

import asyncio
import base64
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import scrypt, sha256
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
import jwt
from fastapi import HTTPException, Request, status
from jwt import PyJWKClient
from jwt.exceptions import (
    ExpiredSignatureError,
    ImmatureSignatureError,
    InvalidTokenError,
    PyJWKClientConnectionError,
    PyJWKClientError,
)

from .secret_values import secret_value

COOKIE_NAME = "ai_trader_local_auth"
CLOUD_COOKIE_NAME = "ai_trader_cloud_auth"
OWNER_COOKIE_NAME = "ai_trader_owner_auth"
LOCAL_USER_ID = "local-user"
# Every cloud-jwt subject resolves to `cloud-<sha256(tenant\0subject)>`.
CLOUD_USER_ID_PREFIX = "cloud-"
DEFAULT_ALLOWED_ORIGINS = {
    "http://127.0.0.1:5173", "http://localhost:5173",
    "http://127.0.0.1:4173", "http://localhost:4173",
    "tauri://localhost", "http://tauri.localhost", "https://tauri.localhost",
}
OIDC_ALGORITHMS = ("RS256", "ES256")
OIDC_JWKS_CACHE_SECONDS = 300
OIDC_UNKNOWN_KID_REFRESH_SECONDS = 30
OWNER_SESSION_SECONDS = 30 * 24 * 60 * 60  # owner decision 2026-09-15: 7 days logged users out too often
OWNER_LOGIN_WINDOW_SECONDS = 15 * 60
OWNER_LOGIN_MAX_FAILURES = 5
OWNER_LOGIN_IP_MAX_FAILURES = 20
OWNER_LOGIN_FAILURE_KEY_LIMIT = 1024
OWNER_SCRYPT_N = 2**15
OWNER_SCRYPT_R = 8
OWNER_SCRYPT_P = 1
# M1b: TqKq direct login — invite gate (default on) and the static main account
# whose username must never be re-bound as a pool account.
TQKQ_LOGIN_INVITE_DEFAULT = True
# Opens the TqKq sign-up form directly (its login page switches on `action_type=REGISTER`).
TQKQ_REGISTER_URL = (
    "https://auth.shinnytech.com/auth/realms/shinnytech/protocol/openid-connect/auth"
    "?client_id=shinny_account_dashboard&redirect_uri=https%3A%2F%2Faccount.shinnytech.com%2F"
    "&response_type=code&scope=openid&action_type=REGISTER"
)
TQKQ_PASSWORD_MAX_BYTES = 400
# Safe read methods: a browser's same-origin GET/HEAD may carry no Origin
# header, so pure reads must not demand one. An explicitly carried Origin and
# every write keep the existing _require_origin check.
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def allowed_origins(*, include_local_defaults: bool = True) -> set[str]:
    configured = {
        item.strip().rstrip("/")
        for item in os.getenv("AI_TRADER_ALLOWED_ORIGINS", "").split(",")
        if item.strip()
    }
    return (DEFAULT_ALLOWED_ORIGINS if include_local_defaults else set()) | configured


def visitor_ip(request: Request) -> str:
    """The visitor address per-visitor keys count on (login/register failure
    budgets, guest budgets) — the one shared responsibility entry for owner
    auth and guests: always the canonical `request.client`.

    The cloud Runtime's host ports publish on loopback only, so every public
    request passes the 8443 nginx (the website 443 path through the site nginx
    in front of it, the desktop path that one layer directly). That nginx
    restores the real visitor via set_real_ip_from 112.124.42.19 and then
    *overwrites* X-Forwarded-For with that address, so uvicorn's
    --forwarded-allow-ips=* reports exactly that address as request.client on
    both paths — a client-supplied XFF never survives the proxy. Request
    headers are never an identity here; without a client the key keeps the
    existing "unknown" semantics.
    """
    return request.client.host if request.client else "unknown"


def _decode_segment(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _encode_segment(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def hash_owner_password(password: str, *, salt: bytes | None = None) -> str:
    if not password:
        raise ValueError("owner_password_required")
    selected_salt = salt or secrets.token_bytes(16)
    digest = scrypt(
        password.encode(), salt=selected_salt,
        n=OWNER_SCRYPT_N, r=OWNER_SCRYPT_R, p=OWNER_SCRYPT_P,
        dklen=32, maxmem=64 * 1024 * 1024,
    )
    return "$".join((
        "scrypt", str(OWNER_SCRYPT_N), str(OWNER_SCRYPT_R), str(OWNER_SCRYPT_P),
        _encode_segment(selected_salt), _encode_segment(digest),
    ))


def verify_owner_password(password: str, encoded: str) -> bool:
    try:
        algorithm, raw_n, raw_r, raw_p, raw_salt, raw_digest = encoded.split("$")
        if algorithm != "scrypt":
            return False
        n, r, p = int(raw_n), int(raw_r), int(raw_p)
        if (n, r, p) != (OWNER_SCRYPT_N, OWNER_SCRYPT_R, OWNER_SCRYPT_P):
            return False
        salt = _decode_segment(raw_salt)
        expected = _decode_segment(raw_digest)
        if len(salt) != 16 or len(expected) != 32:
            return False
        actual = scrypt(
            password.encode(), salt=salt, n=n, r=r, p=p,
            dklen=len(expected), maxmem=64 * 1024 * 1024,
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def _valid_owner_password_hash(encoded: str) -> bool:
    try:
        algorithm, raw_n, raw_r, raw_p, raw_salt, raw_digest = encoded.split("$")
        return (
            algorithm == "scrypt"
            and (int(raw_n), int(raw_r), int(raw_p))
            == (OWNER_SCRYPT_N, OWNER_SCRYPT_R, OWNER_SCRYPT_P)
            and len(_decode_segment(raw_salt)) == 16
            and len(_decode_segment(raw_digest)) == 32
        )
    except (ValueError, TypeError):
        return False


# Scope key of a server-built request carrying its prefetched `OwnerCallFacts`;
# an ASGI scope is the server's own, so no client request can set it.
OWNER_CALL_FACTS_SCOPE = "ai_trader.owner_call_facts"


@dataclass(frozen=True)
class OwnerCallFacts:
    """The store reads one owner-password identity check needs, taken in one
    query (`RuntimeStore.owner_call_facts`) for the token they were read for."""
    token_hash: str
    subject: str | None
    revoked: bool
    user: dict[str, Any] | None
    session: dict[str, Any] | None


@dataclass(frozen=True)
class OwnerIdentity:
    username: str
    role: str = "user"

    @property
    def scoped_user_id(self) -> str:
        digest = sha256(self.username.encode()).hexdigest()
        return f"owner-{digest}"


class OwnerAuth:
    """Single-owner authentication for the personal cloud Runtime.

    Account-bound TqKq and model credentials stay in server-side secret storage;
    the browser receives only a signed, expiring session cookie.
    """

    auth_mode = "owner-password"
    provider = "owner-password"

    def __init__(
        self, *, username: str | None = None, password_hash: str | None = None,
        session_secret: str | None = None, session_seconds: int = OWNER_SESSION_SECONDS,
        allowed_login_origins: set[str] | None = None,
        session_store: Any = None,
        user_store: Any = None,
        registration_enabled: bool | None = None,
    ) -> None:
        self.session_store = session_store
        self.user_store = user_store
        self._revoked_sessions: dict[str, int] = {}
        self.username = (username or os.getenv("AI_TRADER_OWNER_USERNAME") or "").strip()
        self.password_hash = password_hash or secret_value("AI_TRADER_OWNER_PASSWORD_HASH")
        self.session_secret = session_secret or secret_value("AI_TRADER_OWNER_SESSION_SECRET")
        # M1b: classic share-code registration keeps its own switch; the user
        # store itself now also backs the TqKq direct login, which works with
        # registration off (its invite gate is `tqkq_invite_required`).
        self.registration_enabled = (
            registration_enabled
            if registration_enabled is not None
            else os.getenv("AI_TRADER_REGISTRATION_ENABLED") == "1"
        )
        self.tqkq_invite_required = (
            os.getenv("AI_TRADER_TQKQ_LOGIN_INVITE", "1" if TQKQ_LOGIN_INVITE_DEFAULT else "0") != "0"
        )
        # The static main account's TqKq username, when the deployment mounts it;
        # login with it is refused so the owner keeps using the owner login.
        static_tqkq = secret_value("AI_TRADER_STATIC_TQKQ_USERNAME").strip()
        self.static_tqkq_username = static_tqkq or None
        if not self.username or not self.password_hash:
            raise RuntimeError("owner_credentials_required")
        if not _valid_owner_password_hash(self.password_hash):
            raise RuntimeError("owner_password_hash_invalid")
        if len(self.session_secret.encode()) < 32:
            raise RuntimeError("owner_session_secret_too_short")
        if not 300 <= session_seconds <= 30 * 24 * 60 * 60:
            raise RuntimeError("owner_session_duration_invalid")
        self.session_seconds = session_seconds
        self.allowed_login_origins = {
            origin.rstrip("/") for origin in (allowed_login_origins or set())
        }
        self._failures: dict[str, list[float]] = {}
        self._failure_lock = threading.Lock()

    def _rate_limit_keys(self, request: Request, username: str) -> tuple[str, ...]:
        host = visitor_ip(request)
        ip_key = f"ip\0{host}"
        if username.casefold() == self.username.casefold():
            return ip_key, f"owner\0{host}"
        return (ip_key,)

    def _tqkq_rate_limit_keys(self, request: Request, username: str) -> tuple[str, ...]:
        """TqKq direct login throttles per IP and per attempted account, so one
        account cannot be hammered from rotating IPs behind one address."""
        host = visitor_ip(request)
        return (f"ip\0{host}", f"tqkq\0{username.casefold()}")

    def _landing_plan(self, invite_plan: str | None) -> str | None:
        """The tier a first login lands on: a tier-carrying invite whose tier
        the owner deleted before the code was used falls back to the default
        tier — a dangling tier name must never reach cloud_users."""
        if invite_plan and self.user_store.get_plan_tier(invite_plan) is None:
            return None
        return invite_plan

    def _prune_failures_locked(self, now: float) -> None:
        cutoff = now - OWNER_LOGIN_WINDOW_SECONDS
        for key, attempts in tuple(self._failures.items()):
            recent = [attempt for attempt in attempts if attempt >= cutoff]
            if recent:
                self._failures[key] = recent
            else:
                self._failures.pop(key, None)

    def _check_rate_limit(self, keys: tuple[str, ...]) -> None:
        with self._failure_lock:
            self._prune_failures_locked(time.monotonic())
            for key in keys:
                limit = (
                    OWNER_LOGIN_IP_MAX_FAILURES
                    if key.startswith("ip\0")
                    else OWNER_LOGIN_MAX_FAILURES
                )
                if len(self._failures.get(key, [])) >= limit:
                    raise HTTPException(
                        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                        detail="owner_login_temporarily_locked",
                    )

    def _record_failure(self, keys: tuple[str, ...]) -> None:
        with self._failure_lock:
            now = time.monotonic()
            self._prune_failures_locked(now)
            for key in keys:
                while (
                    key not in self._failures
                    and len(self._failures) >= OWNER_LOGIN_FAILURE_KEY_LIMIT
                ):
                    self._failures.pop(next(iter(self._failures)))
                self._failures.setdefault(key, []).append(now)

    def _clear_failures(self, keys: tuple[str, ...]) -> None:
        with self._failure_lock:
            for key in keys:
                self._failures.pop(key, None)

    def _require_origin(self, request: Request) -> None:
        origin = request.headers.get("origin", "").rstrip("/")
        if origin not in self.allowed_login_origins:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="origin_not_allowed"
            )

    def _issue_session(self, username: str, password_hash: str) -> str:
        now = int(time.time())
        payload = _encode_segment(json.dumps({
            "sub": username,
            "nonce": os.urandom(16).hex(),
            "iat": now,
            "exp": now + self.session_seconds,
            "pwd": sha256(password_hash.encode()).hexdigest()[:24],
        }, separators=(",", ":"), sort_keys=True).encode())
        signature = _encode_segment(hmac.new(
            self.session_secret.encode(), f"v1.{payload}".encode(), sha256
        ).digest())
        return f"v1.{payload}.{signature}"

    def _apply_session(self, request: Request, response: Any, token: str) -> None:
        secure_request = (
            request.url.scheme == "https" or os.getenv("AI_TRADER_SECURE_COOKIES") == "1"
        )
        response.headers["Cache-Control"] = "no-store"
        response.set_cookie(
            OWNER_COOKIE_NAME, token, httponly=True,
            samesite="lax", secure=secure_request, path="/",
            max_age=self.session_seconds,
        )

    def _cloud_user_disabled(self, username: str) -> bool:
        if self.user_store is None:
            return False
        record = self.user_store.get_cloud_user(username)
        return record is not None and bool(record.get("disabled"))

    _OWNER_HASH_TTL = 5.0

    def _owner_hash(self) -> str:
        """管理员的登录密码：在设置里改过就用改过的（库里一行），否则部署配置里的。每次会话校验都要读，
        缓存 5 秒（另一个进程改了密码，这边最多 5 秒后旧会话也失效）。"""
        cached = getattr(self, "_owner_hash_cache", None)
        now = time.monotonic()
        if cached is not None and now - cached[0] < self._OWNER_HASH_TTL:
            return cached[1]
        getter = getattr(self.user_store, "get_owner_password_hash", None)
        saved = getter() if getter is not None else None
        value = saved if saved and _valid_owner_password_hash(saved) else self.password_hash
        self._owner_hash_cache = (now, value)
        return value

    _PASSWORD_MIN, _PASSWORD_MAX = 10, 200

    def _session_answer(self, request: Request, response: Any, username: str, password_hash: str) -> dict[str, Any]:
        """换了密码后给这台设备一个新会话（旧会话带旧密码指纹，全部失效）；桌面端拿 accessToken。"""
        token = self._issue_session(username, password_hash)
        self._apply_session(request, response, token)
        result: dict[str, Any] = {"status": "password_changed"}
        if request.headers.get("origin", "").rstrip("/") in {
            "tauri://localhost", "http://tauri.localhost", "https://tauri.localhost"
        }:
            result["accessToken"] = token
        return result

    def change_password(self, request: Request, response: Any, *, username: str,
                        current_password: str, new_password: str) -> dict[str, Any]:
        """本人改登录密码（设置 → 账户，2026-10-21）：管理员与本站账号；快期登录的账号密码在快期，不在这里。
        当前密码输错与登录同一套失败计数与锁定。改好后这台设备拿新会话，其他设备的会话随旧密码一起失效。"""
        self._require_origin(request)
        keys = self._rate_limit_keys(request, username)
        self._check_rate_limit(keys)
        if not self._is_password_account(username):
            raise HTTPException(status_code=409, detail="password_managed_by_tqkq")
        resolved = self._resolve_session_user(username)
        if resolved is None or not verify_owner_password(current_password, resolved[0]):
            self._record_failure(keys)
            raise HTTPException(status_code=401, detail="invalid_current_password")
        if not self._PASSWORD_MIN <= len(new_password or "") <= self._PASSWORD_MAX:
            raise HTTPException(status_code=422, detail="invalid_password")
        if new_password == current_password:
            raise HTTPException(status_code=422, detail="password_unchanged")
        password_hash = hash_owner_password(new_password)
        if resolved[1] == "owner":
            if self.user_store is None:
                raise HTTPException(status_code=409, detail="owner_password_not_storable")
            self.user_store.set_owner_password_hash(password_hash)
            self._owner_hash_cache = (time.monotonic(), password_hash)
        elif self.user_store is None or not self.user_store.set_cloud_user_password_hash(username, password_hash):
            raise HTTPException(status_code=404, detail="user_unknown")
        self._clear_failures(keys)
        return self._session_answer(request, response, username, password_hash)

    def reset_password(self, *, username: str, new_password: str) -> dict[str, Any]:
        """管理员重置一个本站账号的登录密码（后台 → 成员）：那个成员所有设备的会话随即失效。管理员自己的密码
        走 change_password（要当前密码），快期登录的账号密码在快期。"""
        if self.user_store is None or username.casefold() == self.username.casefold():
            raise HTTPException(status_code=409, detail="use_change_password")
        record = self.user_store.get_cloud_user(username)
        if record is None:
            raise HTTPException(status_code=404, detail="user_unknown")
        if record.get("invitedBy") == "tqkq-login":
            raise HTTPException(status_code=409, detail="password_managed_by_tqkq")
        if not self._PASSWORD_MIN <= len(new_password or "") <= self._PASSWORD_MAX:
            raise HTTPException(status_code=422, detail="invalid_password")
        self.user_store.set_cloud_user_password_hash(username, hash_owner_password(new_password))
        return {"status": "password_reset", "username": username}

    def _resolve_session_user(
        self, username: str, facts: OwnerCallFacts | None = None,
    ) -> tuple[str, str] | None:
        """Return (password_hash, role) backing the named account, if any."""
        if hmac.compare_digest(username, self.username):
            return self._owner_hash(), "owner"
        if self.user_store is not None:
            record = (facts.user if facts is not None and facts.subject == username
                      else self.user_store.get_cloud_user(username))
            if record is not None:
                # M2: a disabled cloud user resolves to nothing, so the next
                # request of an existing session fails exactly like a revoked one.
                if record.get("disabled"):
                    return None
                return record["passwordHash"], record["role"]
        return None

    def validate(self, token: str | None, facts: OwnerCallFacts | None = None) -> OwnerIdentity:
        if not token:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="owner_auth_required"
            )
        token_hash = sha256(token.encode()).hexdigest()
        if facts is not None and facts.token_hash != token_hash:
            facts = None
        if ((self.session_store is not None and (
                facts.revoked if facts is not None else self.session_store.owner_session_revoked(token_hash)))
                or token_hash in self._revoked_sessions):
            raise HTTPException(status_code=401, detail="owner_session_revoked")
        try:
            version, payload_segment, signature_segment = token.split(".")
            expected = hmac.new(
                self.session_secret.encode(), f"{version}.{payload_segment}".encode(), sha256
            ).digest()
            if version != "v1" or not hmac.compare_digest(
                _decode_segment(signature_segment), expected
            ):
                raise ValueError("signature")
            payload = json.loads(_decode_segment(payload_segment))
            now = int(time.time())
            subject = payload.get("sub")
            if not isinstance(subject, str):
                raise ValueError("subject")
            resolved = self._resolve_session_user(subject, facts)
            if resolved is None:
                raise ValueError("subject")
            password_hash, role = resolved
            if not password_hash:
                # A cloud account without a password (created but never set up) backs no session.
                raise ValueError("password-version")
            if not isinstance(payload.get("iat"), int) or payload["iat"] > now + 30:
                raise ValueError("issued-at")
            if not isinstance(payload.get("exp"), int) or payload["exp"] <= now:
                raise ValueError("expired")
            if payload.get("pwd") != sha256(password_hash.encode()).hexdigest()[:24]:
                raise ValueError("password-version")
        except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid_owner_session"
            ) from None
        return OwnerIdentity(subject, role)

    @staticmethod
    def session_token(request: Request) -> str | None:
        authorization = request.headers.get("authorization")
        if authorization is not None:
            scheme, _, token = authorization.partition(" ")
            return token if scheme.lower() == "bearer" and token else None
        return request.cookies.get(OWNER_COOKIE_NAME)

    def user(self, request: Request) -> str:
        if request.method not in SAFE_METHODS:
            self._require_origin(request)
        return self.validate(self.session_token(request), request.scope.get(OWNER_CALL_FACTS_SCOPE)).scoped_user_id

    def call_facts(self, request: Request, session_id: str, user_id: str) -> OwnerCallFacts | None:
        """Everything `user(request)` and the session check read from the store,
        in one query; the checks themselves run unchanged on the result. The
        subject is the token's claim, only to pick the user row: `validate`
        still verifies the signature before it trusts it."""
        if self.session_store is None or self.user_store is not self.session_store:
            return None
        token = self.session_token(request)
        token_hash = sha256(token.encode()).hexdigest() if token else ""
        subject: str | None = None
        try:
            claimed = json.loads(_decode_segment((token or "").split(".")[1])).get("sub")
            subject = claimed if isinstance(claimed, str) else None
        except (ValueError, TypeError, IndexError, AttributeError, json.JSONDecodeError, UnicodeDecodeError):
            subject = None
        found = self.session_store.owner_call_facts(
            token_hash=token_hash, username=subject, session_id=session_id, user_id=user_id)
        return OwnerCallFacts(token_hash, subject, found["revoked"], found["user"], found["session"])

    def session_status(self, request: Request) -> dict[str, str | bool]:
        try:
            identity = self.validate(self.session_token(request))
        except HTTPException:
            return {
                "authenticated": False,
                "authMode": self.auth_mode,
                "authProvider": self.provider,
            }
        return {
            "authenticated": True,
            "userId": identity.scoped_user_id,
            "username": identity.username,
            "role": identity.role,
            # 设置 → 账户：本站密码登录的账号能在这里改密码；快期登录的密码在快期（2026-10-21）。
            "passwordLogin": self._is_password_account(identity.username),
            "authMode": self.auth_mode,
            "authProvider": self.provider,
        }

    def login(self, request: Request, response: Any, *, username: str, password: str) -> dict[str, str]:
        self._require_origin(request)
        rate_limit_keys = self._rate_limit_keys(request, username)
        self._check_rate_limit(rate_limit_keys)
        if self._cloud_user_disabled(username):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="account_disabled")
        resolved = self._resolve_session_user(username)
        password_valid = bool(resolved) and verify_owner_password(password, resolved[0])
        username_valid = resolved is not None
        if not username_valid or not password_valid:
            self._record_failure(rate_limit_keys)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid_owner_credentials"
            )
        self._clear_failures(rate_limit_keys)
        role = resolved[1]
        identity = OwnerIdentity(username, role)
        token = self._issue_session(username, resolved[0])
        self._apply_session(request, response, token)
        result = {
            "status": "authenticated",
            "userId": identity.scoped_user_id,
            "username": identity.username,
            "role": identity.role,
            "authMode": self.auth_mode,
            "authProvider": self.provider,
        }

        # Desktop WebViews cannot rely on cross-site cookies. Return only the
        # expiring application session for explicitly registered native origins.
        if request.headers.get("origin", "").rstrip("/") in {
            "tauri://localhost", "http://tauri.localhost", "https://tauri.localhost"
        }:
            result["accessToken"] = token
        return result

    _USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]{3,32}$")
    # TqKq accounts allow phone numbers, e-mails and plain usernames.
    _TQKQ_USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9_.@+-]{3,64}$")
    # A custom invite code (2026-10-01): 4–32 chars of letters, digits, - and _.
    _CUSTOM_INVITE_CODE_PATTERN = re.compile(r"^[A-Za-z0-9_-]{4,32}$")

    def _is_password_account(self, username: str) -> bool:
        """The owner (any letter case — a TqKq account named like the owner must never
        resolve to it) and share-code accounts log in with their own password; TqKq-login
        users have only an unusable hash and go to the pool."""
        if username.casefold() == self.username.casefold():
            return True
        record = self.user_store.get_cloud_user(username) if self.user_store is not None else None
        return record is not None and record.get("invitedBy") != "tqkq-login"

    async def login_tqkq(
        self, request: Request, response: Any, *, username: str, password: str,
        invite_code: str, pool_available: bool,
        verify: Callable[[str, str], Awaitable[dict[str, Any]]],
        bind: Callable[[str, str], Awaitable[dict[str, Any]]],
    ) -> dict[str, object]:
        """Log in with the user's own TqKq account (M1b). The pool verifies the
        credentials first; the first login creates the cloud user behind an
        invite code, and every login re-binds the sealed password to the pool
        account before the session is issued. The TqKq password is never stored
        as the session's password hash — a fresh unusable hash is."""
        self._require_origin(request)
        username = (username or "").strip()
        rate_limit_keys = self._tqkq_rate_limit_keys(request, username)
        self._check_rate_limit(rate_limit_keys)
        if not self._TQKQ_USERNAME_PATTERN.fullmatch(username):
            self._record_failure(rate_limit_keys)
            raise HTTPException(status_code=422, detail="invalid_username")
        if not password or len(password.encode()) > TQKQ_PASSWORD_MAX_BYTES:
            self._record_failure(rate_limit_keys)
            raise HTTPException(status_code=422, detail="invalid_password")
        if self._is_password_account(username):
            # One login form (owner 2026-09-30): the administrator and share-code accounts type
            # their own name and password into the TqKq form and are checked here with the
            # password login — they never reach the pool.
            return await asyncio.to_thread(
                self.login, request, response, username=username, password=password)
        if (self.static_tqkq_username is not None
                and username.casefold() == self.static_tqkq_username.casefold()):
            # The static main account is the owner's own TqKq login — checked
            # before the owner-name collision below so the user gets the exact
            # "log in as the administrator instead" answer.
            self._record_failure(rate_limit_keys)
            raise HTTPException(
                status_code=403, detail={"code": "use_owner_login"},
            )
        if self.user_store is None:
            raise HTTPException(status_code=503, detail="account_pool_unavailable")
        existing = self.user_store.get_cloud_user(username)
        can_register = bool(self.registration_enabled and (invite_code or "").strip())
        if not pool_available:
            if existing is None:
                # One login form (owner 2026-10-04): with no TqKq pool an unknown name is a new
                # site account — the client asks before registering it with these credentials.
                raise HTTPException(status_code=404, detail={"code": "account_not_found",
                                                             "canRegister": can_register})
            raise HTTPException(status_code=503, detail="account_pool_unavailable")
        if existing is not None and existing.get("disabled"):
            # M2: the owner disabled this account; refuse before the pool sees
            # the credentials, exactly like the password login does.
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="account_disabled")
        first_login = existing is None
        if first_login and self.tqkq_invite_required and (not invite_code or len(invite_code) > 200):
            # No (or malformed) invite code: refuse before the pool ever sees
            # the credentials — the runtime must not become a password oracle.
            self._record_failure(rate_limit_keys)
            raise HTTPException(status_code=403, detail="invite_code_invalid")
        verified = await verify(username, password)
        if not verified.get("ok"):
            self._record_failure(rate_limit_keys)
            raise HTTPException(
                status_code=401,
                detail={"code": "tqkq_login_failed",
                        "reason": str(verified.get("reason") or "auth_failed"),
                        # A brand-new name that is not a TqKq account (or a mistyped one): the
                        # one login form offers to create a site account with these credentials,
                        # after the person confirms (owner 2026-10-04).
                        "canRegister": first_login and can_register},
            )
        if first_login:
            password_hash = hash_owner_password(secrets.token_urlsafe(24))
            if self.tqkq_invite_required:
                # Consumed only after the password verified: a typo must not
                # burn the user's invite code. The code is spent and the user
                # created in one transaction (#932 X4).
                code_hash = sha256(invite_code.strip().encode()).hexdigest()
                invite_plan = self.user_store.register_invited_user(
                    code_hash=code_hash, username=username, password_hash=password_hash,
                    invited_by="tqkq-login", landing_plan=self._landing_plan,
                )
                if invite_plan is None:
                    self._record_failure(rate_limit_keys)
                    raise HTTPException(status_code=403, detail="invite_code_invalid")
            else:
                self.user_store.create_cloud_user(
                    username=username,
                    password_hash=password_hash,
                    invited_by="tqkq-login",
                )
            role = "user"
        else:
            password_hash = existing["passwordHash"]
            role = existing["role"]
        identity = OwnerIdentity(username, role)
        bound = await bind(identity.scoped_user_id, password)
        token = self._issue_session(username, password_hash)
        self._apply_session(request, response, token)
        self._clear_failures(rate_limit_keys)
        result: dict[str, object] = {
            "status": "logged_in",
            "userId": identity.scoped_user_id,
            "username": identity.username,
            "role": identity.role,
            "accountId": bound["accountId"],
            "assembly": bound["assembly"],
            "firstLogin": first_login,
            "authMode": self.auth_mode,
            "authProvider": self.provider,
        }

        # Desktop WebViews cannot rely on cross-site cookies; same contract as
        # the owner login for explicitly registered native origins.
        if request.headers.get("origin", "").rstrip("/") in {
            "tauri://localhost", "http://tauri.localhost", "https://tauri.localhost"
        }:
            result["accessToken"] = token
        return result

    def register(
        self, request: Request, response: Any, *,
        username: str, password: str, invite_code: str,
    ) -> dict[str, str]:
        """Create an invited account and log it in immediately."""
        if self.user_store is None or not self.registration_enabled:
            raise HTTPException(status_code=404, detail="registration_disabled")
        self._require_origin(request)
        rate_limit_keys = self._rate_limit_keys(request, username)
        self._check_rate_limit(rate_limit_keys)
        if not self._USERNAME_PATTERN.fullmatch(username or ""):
            self._record_failure(rate_limit_keys)
            raise HTTPException(status_code=422, detail="invalid_username")
        if hmac.compare_digest(username.casefold(), self.username.casefold()):
            self._record_failure(rate_limit_keys)
            raise HTTPException(status_code=409, detail="username_taken")
        if not password or len(password) < 10 or len(password) > 200:
            self._record_failure(rate_limit_keys)
            raise HTTPException(status_code=422, detail="invalid_password")
        if not invite_code or len(invite_code) > 200:
            self._record_failure(rate_limit_keys)
            raise HTTPException(status_code=422, detail="invalid_invite_code")
        if self.user_store.get_cloud_user(username) is not None:
            self._record_failure(rate_limit_keys)
            raise HTTPException(status_code=409, detail="username_taken")
        code_hash = sha256(invite_code.strip().encode()).hexdigest()
        password_hash = hash_owner_password(password)
        try:
            # One transaction (#932 X4): a failed insert leaves the code unspent.
            invite_plan = self.user_store.register_invited_user(
                code_hash=code_hash, username=username, password_hash=password_hash,
                invited_by="invite-code", landing_plan=self._landing_plan,
            )
        except sqlite3.IntegrityError:
            # The name was taken by a registration that landed after the check above.
            self._record_failure(rate_limit_keys)
            raise HTTPException(status_code=409, detail="username_taken") from None
        if invite_plan is None:
            self._record_failure(rate_limit_keys)
            raise HTTPException(status_code=403, detail="invite_code_invalid")
        self._clear_failures(rate_limit_keys)
        identity = OwnerIdentity(username, "user")
        token = self._issue_session(username, password_hash)
        self._apply_session(request, response, token)
        return {
            "status": "registered",
            "userId": identity.scoped_user_id,
            "username": identity.username,
            "role": identity.role,
            "authMode": self.auth_mode,
            "authProvider": self.provider,
        }

    def create_invite_code(
        self, request: Request, *, created_by: str, days_valid: int = 14,
        plan: str | None = None, code: str | None = None,
        uses: str = "once", never_expires: bool = False,
    ) -> dict[str, object]:
        if self.user_store is None:
            raise HTTPException(status_code=404, detail="registration_disabled")
        self._require_origin(request)
        if not 1 <= days_valid <= 90:
            raise HTTPException(status_code=422, detail="invalid_expiry")
        text = code.strip() if code is not None else ""
        if text:
            # Case-sensitive on purpose: logins hash the code exactly as typed.
            if not self._CUSTOM_INVITE_CODE_PATTERN.fullmatch(text):
                raise HTTPException(status_code=422, detail="invite_code_format")
        else:
            text = secrets.token_urlsafe(12)
        expires = datetime.fromtimestamp(time.time() + days_valid * 86400, tz=UTC)
        created = self.user_store.create_invite_code(
            code_hash=sha256(text.encode()).hexdigest(),
            created_by=created_by, expires_at=expires, plan=plan,
            code=text, unlimited=uses == "unlimited", never_expires=never_expires,
        )
        if not created:
            raise HTTPException(status_code=409, detail="invite_code_exists")
        return {
            "inviteCode": text,
            "expiresAt": None if never_expires else expires.isoformat() + "Z",
            "plan": plan or "",
            "uses": uses,
        }

    def list_invite_codes(self, request: Request) -> list[dict[str, Any]]:
        if self.user_store is None:
            raise HTTPException(status_code=404, detail="registration_disabled")
        # A pure read follows the shared safe-method Origin contract: a missing
        # Origin is the same-origin browser default (#529 — the web build moved
        # behind the same-origin /trader-runtime relay in #733); an explicitly
        # carried Origin is still checked. Writes keep the strict check below.
        if request.method not in SAFE_METHODS or request.headers.get("origin") is not None:
            self._require_origin(request)
        return self.user_store.list_invite_codes()

    def disable_invite_code(self, request: Request, code_hash: str) -> dict[str, Any]:
        """Stop a code from admitting anyone; disabling twice returns the same
        row, an unknown id is the caller's 404."""
        if self.user_store is None:
            raise HTTPException(status_code=404, detail="registration_disabled")
        self._require_origin(request)
        return self.user_store.disable_invite_code(code_hash)

    def logout(self, request: Request, response: Any) -> dict[str, str]:
        self._require_origin(request)
        token = self.session_token(request)
        if token:
            try:
                self.validate(token)
            except HTTPException:
                pass
            else:
                payload = json.loads(_decode_segment(token.split(".")[1]))
                digest = sha256(token.encode()).hexdigest()
                now = int(time.time())
                if self.session_store is not None:
                    self.session_store.revoke_owner_session(digest, payload["exp"], now)
                else:
                    self._revoked_sessions = {k: v for k, v in self._revoked_sessions.items() if v > now}
                    self._revoked_sessions[digest] = payload["exp"]
        response.headers["Cache-Control"] = "no-store"
        response.delete_cookie(OWNER_COOKIE_NAME, path="/")
        return {"status": "signed_out", "authMode": self.auth_mode}


@dataclass(frozen=True)
class CloudIdentity:
    tenant_id: str
    subject: str

    @property
    def scoped_user_id(self) -> str:
        digest = sha256(f"{self.tenant_id}\0{self.subject}".encode()).hexdigest()
        return f"{CLOUD_USER_ID_PREFIX}{digest}"


class CloudAuth:
    auth_mode = "cloud-jwt"

    def __init__(
        self, secret: str | None = None, *, issuer: str | None = None,
        audience: str | None = None, required_tenant: str | None = None,
        cross_site_cookie_origins: set[str] | None = None,
        provider: Literal["development-hs256", "oidc"] | None = None,
        oidc_jwks_url: str | None = None,
        tenant_claim: str | None = None,
        allow_insecure_local_oidc: bool | None = None,
    ) -> None:
        configured_secret = secret or os.getenv("AI_TRADER_CLOUD_JWT_SECRET") or ""
        self.provider = provider or os.getenv("AI_TRADER_CLOUD_AUTH_PROVIDER") or (
            "development-hs256" if configured_secret else "oidc"
        )
        if self.provider not in {"development-hs256", "oidc"}:
            raise RuntimeError("cloud_auth_provider_invalid")
        self.secret = configured_secret
        self.tenant_claim = tenant_claim or os.getenv("AI_TRADER_OIDC_TENANT_CLAIM", "tid")
        if not self.tenant_claim:
            raise RuntimeError("cloud_tenant_claim_required")
        self._jwks_client: PyJWKClient | None = None
        self._oidc_signing_keys: dict[str, Any] = {}
        self._oidc_keys_expires_at = 0.0
        self._oidc_unknown_kid_refresh_after = 0.0
        self._oidc_refresh_lock = threading.Lock()
        self.oidc_jwks_url: str | None = None
        if self.provider == "development-hs256":
            if len(self.secret.encode()) < 32:
                raise RuntimeError("cloud_jwt_secret_too_short")
            self.issuer = issuer or os.getenv("AI_TRADER_CLOUD_JWT_ISSUER", "ai-trader-cloud")
            self.audience = audience or os.getenv("AI_TRADER_CLOUD_JWT_AUDIENCE", "ai-trader")
        else:
            self.issuer = issuer or os.getenv("AI_TRADER_OIDC_ISSUER") or ""
            self.audience = audience or os.getenv("AI_TRADER_OIDC_AUDIENCE") or ""
            if not self.issuer or not self.audience:
                raise RuntimeError("cloud_oidc_configuration_required")
            allow_insecure = (
                allow_insecure_local_oidc
                if allow_insecure_local_oidc is not None
                else os.getenv("AI_TRADER_ALLOW_INSECURE_LOCAL_OIDC") == "1"
            )
            self._require_secure_oidc_url(self.issuer, allow_insecure)
            self.oidc_jwks_url = (
                oidc_jwks_url or os.getenv("AI_TRADER_OIDC_JWKS_URL") or None
            )
            if self.oidc_jwks_url is None:
                self.oidc_jwks_url = self._discover_jwks_url(
                    self.issuer, allow_insecure=allow_insecure
                )
            self._require_secure_oidc_url(self.oidc_jwks_url, allow_insecure)
            self._jwks_client = PyJWKClient(
                self.oidc_jwks_url,
                # Cache the complete JWK set for a bounded interval, but never
                # keep an individual kid indefinitely after provider rotation.
                cache_keys=False,
                cache_jwk_set=True,
                lifespan=OIDC_JWKS_CACHE_SECONDS,
                timeout=5,
            )
        self.required_tenant = required_tenant or os.getenv("AI_TRADER_CLOUD_TENANT_ID")
        self.cross_site_cookie_origins = {
            origin.rstrip("/") for origin in (cross_site_cookie_origins or set())
            if self._needs_cross_site_cookie(origin)
        }

    @staticmethod
    def _require_secure_oidc_url(url: str, allow_insecure_local: bool) -> None:
        parsed = urlsplit(url)
        local = parsed.hostname in {"127.0.0.1", "localhost"}
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise RuntimeError("cloud_oidc_endpoint_invalid")
        if parsed.scheme == "https" and parsed.hostname:
            return
        if allow_insecure_local and local and parsed.scheme == "http":
            return
        raise RuntimeError("cloud_oidc_https_required")

    @staticmethod
    def _discover_jwks_url(issuer: str, *, allow_insecure: bool) -> str:
        metadata_url = f"{issuer.rstrip('/')}/.well-known/openid-configuration"
        CloudAuth._require_secure_oidc_url(metadata_url, allow_insecure)
        try:
            response = httpx.get(
                metadata_url,
                headers={"Accept": "application/json"},
                timeout=5,
                follow_redirects=False,
            )
            response.raise_for_status()
            metadata = response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise RuntimeError("cloud_oidc_discovery_failed") from error
        if metadata.get("issuer") != issuer:
            raise RuntimeError("cloud_oidc_issuer_mismatch")
        jwks_uri = metadata.get("jwks_uri")
        if not isinstance(jwks_uri, str) or not jwks_uri:
            raise RuntimeError("cloud_oidc_jwks_required")
        CloudAuth._require_secure_oidc_url(jwks_uri, allow_insecure)
        return jwks_uri

    @staticmethod
    def _needs_cross_site_cookie(origin: str) -> bool:
        parsed = urlsplit(origin)
        return (
            parsed.scheme == "https"
            or (parsed.scheme == "tauri" and parsed.hostname == "localhost")
            or parsed.hostname == "tauri.localhost"
        )

    def validate(self, token: str | None) -> CloudIdentity:
        if not token:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="cloud_auth_required")
        if self.provider == "oidc":
            return self._validate_oidc(token)
        try:
            header_segment, payload_segment, signature_segment = token.split(".")
            header = json.loads(_decode_segment(header_segment))
            payload: dict[str, Any] = json.loads(_decode_segment(payload_segment))
            expected = hmac.new(
                self.secret.encode(), f"{header_segment}.{payload_segment}".encode(), sha256
            ).digest()
            signature = _decode_segment(signature_segment)
        except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid_cloud_token") from None
        if header != {"alg": "HS256", "typ": "JWT"} or not hmac.compare_digest(signature, expected):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid_cloud_token")
        now = int(time.time())
        if payload.get("iss") != self.issuer or payload.get("aud") != self.audience:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid_cloud_token_claims")
        if not isinstance(payload.get("exp"), int) or payload["exp"] <= now:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="cloud_token_expired")
        if isinstance(payload.get("nbf"), int) and payload["nbf"] > now + 30:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="cloud_token_not_active")
        tenant_id, subject = payload.get(self.tenant_claim), payload.get("sub")
        if not isinstance(tenant_id, str) or not tenant_id or not isinstance(subject, str) or not subject:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid_cloud_identity")
        if self.required_tenant and tenant_id != self.required_tenant:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="cloud_tenant_not_allowed")
        return CloudIdentity(tenant_id=tenant_id, subject=subject)

    def _validate_oidc(self, token: str) -> CloudIdentity:
        assert self._jwks_client is not None
        try:
            header = jwt.get_unverified_header(token)
            if header.get("alg") not in OIDC_ALGORITHMS:
                raise InvalidTokenError("oidc_algorithm_not_allowed")
            kid = header.get("kid")
            if not isinstance(kid, str) or not kid:
                raise InvalidTokenError("oidc_kid_required")
            signing_key = self._oidc_signing_key(kid)
            payload = jwt.decode(
                token,
                signing_key,
                algorithms=list(OIDC_ALGORITHMS),
                audience=self.audience,
                issuer=self.issuer,
                leeway=30,
                options={"require": ["exp", "iat", "iss", "aud", "sub"]},
            )
        except ExpiredSignatureError:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="cloud_token_expired"
            ) from None
        except ImmatureSignatureError:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="cloud_token_not_active"
            ) from None
        except PyJWKClientConnectionError:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="cloud_oidc_key_service_unavailable",
            ) from None
        except (InvalidTokenError, PyJWKClientError, ValueError, TypeError):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid_cloud_token"
            ) from None
        tenant_id, subject = payload.get(self.tenant_claim), payload.get("sub")
        if not isinstance(tenant_id, str) or not tenant_id or not isinstance(subject, str) or not subject:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid_cloud_identity"
            )
        if self.required_tenant and tenant_id != self.required_tenant:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="cloud_tenant_not_allowed"
            )
        return CloudIdentity(tenant_id=tenant_id, subject=subject)

    def _oidc_signing_key(self, kid: str) -> Any:
        assert self._jwks_client is not None
        now = time.monotonic()
        cached = self._oidc_signing_keys.get(kid)
        if cached is not None and now < self._oidc_keys_expires_at:
            return cached.key

        # Only one authentication worker may refresh JWKS. Other concurrent
        # requests fail closed immediately instead of occupying the thread
        # pool while waiting on the identity provider.
        if not self._oidc_refresh_lock.acquire(blocking=False):
            raise PyJWKClientConnectionError("oidc_jwks_refresh_in_progress")
        try:
            now = time.monotonic()
            cached = self._oidc_signing_keys.get(kid)
            if cached is not None and now < self._oidc_keys_expires_at:
                return cached.key

            cache_fresh = now < self._oidc_keys_expires_at
            if (
                cache_fresh
                and now < self._oidc_unknown_kid_refresh_after
            ):
                raise InvalidTokenError("oidc_unknown_kid_rate_limited")

            # A missing kid may represent normal provider rotation, so allow
            # one forced refresh per bounded cooldown. Distinct forged kids
            # cannot each trigger an outbound request during that interval.
            refresh = cache_fresh
            if refresh:
                self._oidc_unknown_kid_refresh_after = (
                    now + OIDC_UNKNOWN_KID_REFRESH_SECONDS
                )
            signing_keys = self._jwks_client.get_signing_keys(refresh=refresh)
            self._oidc_signing_keys = {
                key.key_id: key for key in signing_keys if key.key_id
            }
            self._oidc_keys_expires_at = (
                time.monotonic() + OIDC_JWKS_CACHE_SECONDS
            )
            selected = self._oidc_signing_keys.get(kid)
            if selected is None:
                self._oidc_unknown_kid_refresh_after = max(
                    self._oidc_unknown_kid_refresh_after,
                    time.monotonic() + OIDC_UNKNOWN_KID_REFRESH_SECONDS,
                )
                raise InvalidTokenError("oidc_unknown_kid")
            return selected.key
        finally:
            self._oidc_refresh_lock.release()

    def user(self, request: Request) -> str:
        return self.validate(request.cookies.get(CLOUD_COOKIE_NAME)).scoped_user_id

    def session_status(self, request: Request) -> dict[str, str | bool]:
        token = request.cookies.get(CLOUD_COOKIE_NAME)
        if not token:
            return {
                "authenticated": False,
                "authMode": self.auth_mode,
                "authProvider": self.provider,
            }
        try:
            identity = self.validate(token)
        except HTTPException as error:
            if error.status_code not in {
                status.HTTP_401_UNAUTHORIZED,
                status.HTTP_403_FORBIDDEN,
            }:
                raise
            return {
                "authenticated": False,
                "authMode": self.auth_mode,
                "authProvider": self.provider,
            }
        return {
            "authenticated": True,
            "userId": identity.subject,
            "tenantId": identity.tenant_id,
            "authMode": self.auth_mode,
            "authProvider": self.provider,
        }

    def login(self, request: Request, response: Any) -> dict[str, str]:
        authorization = request.headers.get("authorization", "")
        token = authorization.removeprefix("Bearer ") if authorization.startswith("Bearer ") else None
        identity = self.validate(token)
        origin = request.headers.get("origin", "").rstrip("/")
        cross_site_cookie = origin in self.cross_site_cookie_origins
        secure_request = (
            request.url.scheme == "https" or os.getenv("AI_TRADER_SECURE_COOKIES") == "1"
        )
        if cross_site_cookie and not secure_request:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="cloud_cross_site_https_required",
            )
        response.set_cookie(
            CLOUD_COOKIE_NAME,
            token,
            httponly=True,
            samesite="none" if cross_site_cookie else "lax",
            secure=True if cross_site_cookie else secure_request,
            path="/",
            max_age=3600,
        )
        return {
            "status": "authenticated",
            "userId": identity.subject,
            "tenantId": identity.tenant_id,
            "authMode": self.auth_mode,
            "authProvider": self.provider,
        }

    def issue_for_test(self, *, tenant_id: str, subject: str, expires_in: int = 3600) -> str:
        if self.provider != "development-hs256":
            raise RuntimeError("cloud_test_token_issuer_disabled")
        header = _encode_segment(json.dumps(
            {"alg": "HS256", "typ": "JWT"}, separators=(",", ":")
        ).encode())
        payload = _encode_segment(json.dumps({
            "iss": self.issuer,
            "aud": self.audience,
            self.tenant_claim: tenant_id,
            "sub": subject,
            "iat": int(time.time()),
            # Round the whole lifetime, not the current second: int(time.time()) + expires_in
            # truncates and leaves a short-lived token up to a second shorter than asked.
            "exp": int(time.time() + expires_in),
        }, separators=(",", ":")).encode())
        signature = _encode_segment(hmac.new(
            self.secret.encode(), f"{header}.{payload}".encode(), sha256
        ).digest())
        return f"{header}.{payload}.{signature}"


class LocalAuth:
    auth_mode = "local-token"
    def __init__(self, token: str | None = None) -> None:
        self.token = token or os.getenv("AI_TRADER_LOCAL_TOKEN") or secrets.token_urlsafe(32)

    def valid_token(self, supplied: str | None) -> bool:
        return supplied is not None and hmac.compare_digest(supplied, self.token)

    def user(self, request: Request) -> str:
        authorization = request.headers.get("authorization", "")
        bearer = authorization.removeprefix("Bearer ") if authorization.startswith("Bearer ") else None
        if not (
            self.valid_token(request.cookies.get(COOKIE_NAME))
            or self.valid_token(bearer)
        ):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="local_auth_required")
        return LOCAL_USER_ID


