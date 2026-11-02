"""The built web client served by the Runtime itself (自建版, `AI_TRADER_WEB_ROOT`)."""
from __future__ import annotations

import re

from starlette.datastructures import Headers
from starlette.exceptions import HTTPException
from starlette.responses import Response
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

# The official site's page policy (deploy/web/nginx-ai-trader-path.conf), on one origin: no OSS asset
# host, the Runtime is 'self', and no upgrade-insecure-requests (a home install is plain http on
# 127.0.0.1). The voice WebSocket is listed by host: older WebKit does not match ws:/wss: with 'self'.
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'; object-src 'none'; "
    "img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self' 'wasm-unsafe-eval'; "
    "worker-src 'self'; font-src 'self' data:; connect-src 'self'{sockets}; manifest-src 'self'")
_HOST = re.compile(r"(?:[A-Za-z0-9.-]+|\[[0-9A-Fa-f:.]+\])(?::\d{1,5})?")


def content_security_policy(scope: Scope) -> str:
    host = Headers(scope=scope).get("host", "")
    return CONTENT_SECURITY_POLICY.format(sockets=f" ws://{host} wss://{host}" if _HOST.fullmatch(host) else "")


class WebClientFiles(StaticFiles):
    """Static files with the single-page fallback: an unknown page path (a reload on /admin) gets
    index.html; an unknown /api path stays a 404. Pages revalidate so an update shows on reload."""

    async def get_response(self, path: str, scope: Scope) -> Response:
        try:
            response = await super().get_response(path, scope)
        except HTTPException as error:
            if error.status_code != 404 or path == "api" or path.startswith("api/"):
                raise
            response = await super().get_response("index.html", scope)
        if response.media_type == "text/html":
            response.headers["Cache-Control"] = "no-cache"
            response.headers["Content-Security-Policy"] = content_security_policy(scope)
        return response
