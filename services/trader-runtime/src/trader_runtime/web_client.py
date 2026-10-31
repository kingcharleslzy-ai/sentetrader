"""The built web client served by the Runtime itself (自建版, `AI_TRADER_WEB_ROOT`)."""
from __future__ import annotations

from starlette.exceptions import HTTPException
from starlette.responses import Response
from starlette.staticfiles import StaticFiles
from starlette.types import Scope


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
        return response
