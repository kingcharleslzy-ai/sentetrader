"""后台「服务器」的数据源：运维台 metrics 接口的只读客户端。

The Runtime keeps no host catalog of its own — names, roles and service lists
belong to the console's inventory. This client only shapes what the console
returns into the admin payload, caching each range for 60 seconds and serving
the last good copy (marked stale) when the console cannot be reached.
"""
from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Protocol

import httpx

from .secret_values import secret_value

DEFAULT_CONSOLE_URL = "https://qilinshuzhi.com/houtai"
DEFAULT_INFRA_HOSTS = ("tencent-ai-trader", "aliyun-qilinshuzhi", "mac-mini-cc", "nas-ci", "gpu-4090d", "zgo-la")
RANGE_KEYS = ("1h", "24h", "7d")
CACHE_TTL_SECONDS = 60.0
FETCH_TIMEOUT_SECONDS = 10.0
MAX_HOSTS = 64
MAX_SERIES_POINTS = 2000


class InfraConsoleUnavailable(RuntimeError):
    """The console could not be read and no cached copy exists to fall back on."""


class MetricsFetch(Protocol):
    """The HTTP seam; tests inject a fake instead of touching the network."""

    async def __call__(self, url: str, *, params: dict[str, str],
                       headers: dict[str, str]) -> dict[str, Any]: ...


async def _http_fetch_metrics(url: str, *, params: dict[str, str],
                              headers: dict[str, str]) -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=FETCH_TIMEOUT_SECONDS) as client:
            response = await client.get(url, params=params, headers=headers)
    except httpx.HTTPError as error:
        raise InfraConsoleUnavailable("infra_console_unreachable") from error
    if response.status_code >= 400:
        # 404 is an unconfigured read token there; every 4xx/5xx is a console
        # side the Runtime cannot fix from here.
        raise InfraConsoleUnavailable("infra_console_error")
    try:
        body: Any = response.json()
    except ValueError as error:
        raise InfraConsoleUnavailable("infra_console_protocol_error") from error
    if not isinstance(body, dict):
        raise InfraConsoleUnavailable("infra_console_protocol_error")
    return body


def _console_settings() -> tuple[str, str | None, tuple[str, ...]]:
    try:
        token: str | None = secret_value("AI_TRADER_INFRA_CONSOLE_TOKEN") or None
    except RuntimeError:
        # An unreadable or ambiguous token source leaves the surface
        # unconfigured rather than breaking startup.
        token = None
    base_url = (os.getenv("AI_TRADER_INFRA_CONSOLE_URL") or DEFAULT_CONSOLE_URL).strip().rstrip("/")
    hosts = tuple(dict.fromkeys(
        part.strip() for part in (os.getenv("AI_TRADER_INFRA_HOSTS") or ",".join(DEFAULT_INFRA_HOSTS)).split(",")
        if part.strip()))
    return base_url, token, hosts


def infra_console_from_env() -> InfraConsoleMetrics | None:
    """None when no token is configured; the surface then answers unconfigured."""
    base_url, token, hosts = _console_settings()
    if not token or not hosts:
        return None
    return InfraConsoleMetrics(base_url, token, hosts)


class InfraConsoleMetrics:
    """One cached payload per range; a failed refresh serves the last good copy."""

    def __init__(self, base_url: str, token: str, hosts: tuple[str, ...], *,
                 fetch: MetricsFetch | None = None,
                 ttl_seconds: float = CACHE_TTL_SECONDS,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.base_url = base_url
        self.token = token
        self.hosts = hosts
        self._fetch: MetricsFetch = fetch or _http_fetch_metrics
        self.ttl_seconds = ttl_seconds
        self._clock = clock
        self._lock = asyncio.Lock()
        self._cache: dict[str, tuple[float, dict[str, object]]] = {}

    async def servers_payload(self, range_key: str) -> dict[str, object]:
        cached = self._cache.get(range_key)
        if cached is not None and self._clock() - cached[0] < self.ttl_seconds:
            return cached[1]
        async with self._lock:
            cached = self._cache.get(range_key)
            if cached is not None and self._clock() - cached[0] < self.ttl_seconds:
                return cached[1]
            try:
                payload = await self._fetch_and_shape(range_key)
            except (InfraConsoleUnavailable, ValueError, KeyError, TypeError) as error:
                if cached is not None:
                    stale = dict(cached[1])
                    stale["stale"] = True
                    return stale
                raise InfraConsoleUnavailable("infra_console_unreachable") from error
            self._cache[range_key] = (self._clock(), payload)
            return payload

    async def _fetch_and_shape(self, range_key: str) -> dict[str, object]:
        body = await self._fetch(
            self.base_url + "/api/metrics",
            params={"hosts": ",".join(self.hosts), "range": range_key},
            headers={"Authorization": "Bearer " + self.token},
        )
        fetched_at = datetime.now(UTC).isoformat(timespec="seconds")
        hosts = body.get("hosts")
        if not isinstance(hosts, list) or len(hosts) > MAX_HOSTS:
            raise ValueError("infra_console_protocol_error")
        by_id = {str(item.get("id")): item for item in hosts if isinstance(item, dict) and item.get("id")}
        # The configured order is the display order; a host the console does not
        # know (renamed, retired) is skipped rather than invented here.
        return {
            "status": "ok",
            "updatedAt": str(body.get("generatedAt") or fetched_at),
            "fetchedAt": fetched_at,
            "hosts": [self._host_item(by_id[identifier]) for identifier in self.hosts if identifier in by_id],
        }

    def _host_item(self, raw: dict[str, Any]) -> dict[str, object]:
        series = raw.get("series")
        points = [point for point in (series if isinstance(series, list) else [])
                  if isinstance(point, dict)][:MAX_SERIES_POINTS]
        services = raw.get("services")
        entries = services if isinstance(services, list) else []
        resources = raw.get("resources")
        return {
            "id": str(raw.get("id") or ""),
            "displayName": str(raw.get("name") or raw.get("id") or ""),
            "platform": raw.get("platform") if isinstance(raw.get("platform"), str) else None,
            "role": raw.get("role") if isinstance(raw.get("role"), str) else None,
            "checkedAt": raw.get("checkedAt"),
            "reachability": raw.get("reachability"),
            "resources": resources if isinstance(resources, dict) else None,
            "series": points,
            "servicesSummary": {
                "total": len(entries),
                "running": sum(1 for entry in entries
                               if isinstance(entry, dict) and entry.get("status") == "running"),
                "failed": sum(1 for entry in entries
                              if isinstance(entry, dict) and entry.get("status") == "failed"),
            },
        }
