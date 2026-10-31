"""Client build reporting: every client request says who it is, the Runtime answers with a verdict.

The request header is `<platform>/<semver>[+<build sha>]` (see the client's `clientBuild.ts`);
the answer rides on every `/api/` and `/healthz` response as `X-AI-Trader-Client-Compat`. The
verdict only informs — nothing is blocked here, and it only says whether the client is too old:
whether a web page lags the deployed web build is the page's own comparison against version.json
(the Runtime and the site deploy separately, so the Runtime cannot know the web build).
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Literal

from . import __version__

MIN_CLIENT_VERSION = "1.6.5"
CompatState = Literal["ok", "outdated", "unknown"]
_PLATFORMS = "web|desktop|cli|mcp|agent"
_HEADER = re.compile(rf"^({_PLATFORMS})/(\d+)\.(\d+)\.(\d+)(?:\+([0-9a-f]{{7,40}}))?$")


@dataclass(frozen=True)
class ClientBuild:
    platform: str
    version: str
    commit: str | None


def runtime_build() -> str:
    """What `X-AI-Trader-Runtime` reports: version plus short build sha, `dev` when unset."""
    sha = os.getenv("AI_TRADER_BUILD_SHA")
    return f"{__version__}+{sha[:7]}" if sha else f"{__version__}+dev"


def version_tuple(value: str) -> tuple[int, ...]:
    return tuple(int(part) for part in value.split("."))


def parse_client_header(value: str | None) -> ClientBuild | None:
    match = _HEADER.fullmatch((value or "").strip())
    if match is None:
        return None
    platform, major, minor, patch, commit = match.groups()
    return ClientBuild(platform=platform, version=f"{major}.{minor}.{patch}", commit=commit)


def compat_state(client: ClientBuild | None) -> CompatState:
    if client is None:
        return "unknown"
    if version_tuple(client.version) < version_tuple(MIN_CLIENT_VERSION):
        return "outdated"
    return "ok"
