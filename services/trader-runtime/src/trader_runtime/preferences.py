"""Account presentation preferences, separate from execution and device geometry."""
import hashlib
import json
import threading
from pathlib import Path
from typing import Literal
from uuid import uuid4

from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from .ui_control import ChartOptions, ChartSizes


class PresentationPreferences(BaseModel):
    model_config = ConfigDict(extra="forbid")
    theme: Literal["dark", "light", "system"] = "dark"
    fontScale: Literal["standard", "large", "extra-large"] = "large"
    density: Literal["compact", "standard", "comfortable"] = "standard"
    dockSide: Literal["left", "right"] = "right"
    chart: ChartOptions
    chartSizes: ChartSizes
    # The positions/orders bar under the chart; older clients omit it. Collapsed by
    # default since round five: the collapsed line is the account overview.
    positionsBar: Literal["expanded", "collapsed"] = "collapsed"
    # Most recently selected contracts, newest first; the contract picker offers them as a group.
    recentSymbols: list[str] = Field(default_factory=list, max_length=5)
    # Contracts pinned as tabs in the header, in display order; older clients omit it.
    pinnedSymbols: list[str] = Field(default_factory=list, max_length=8)
    # Starting equity of the simulation account for the account P&L page; older clients omit it.
    initialBalance: float | None = Field(default=None, ge=0, le=1_000_000_000_000, allow_inf_nan=False)


class PreferencesSnapshot(BaseModel):
    revision: int
    preferences: PresentationPreferences | None


class PreferencesUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=0, strict=True)
    preferences: PresentationPreferences


class PreferenceStore:
    def __init__(self, root: Path):
        self.root = root / "account-preferences"
        self.lock = threading.RLock()

    def _path(self, user: str) -> Path:
        return self.root / (hashlib.sha256(user.encode()).hexdigest() + ".json")

    def get(self, user: str) -> dict:
        with self.lock:
            path = self._path(user)
            if not path.exists():
                return {"revision": 0, "preferences": None}
            return PreferencesSnapshot.model_validate_json(path.read_text()).model_dump()

    def save(self, user: str, update: PreferencesUpdate) -> dict:
        with self.lock:
            if self.get(user)["revision"] != update.revision:
                raise HTTPException(409, "其他设备已更新偏好，请读取最新设置后再修改")
            saved = {"revision": update.revision + 1, "preferences": update.preferences.model_dump()}
            self.root.mkdir(parents=True, exist_ok=True)
            path = self._path(user)
            temporary = path.with_suffix(f".{uuid4().hex}.tmp")
            try:
                with temporary.open("x") as stream:
                    temporary.chmod(0o600)
                    json.dump(saved, stream, ensure_ascii=False)
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)
            return saved


def register_preferences(app, current_user, root: Path | None):
    # Schema export does not create stores or read user data.
    store = PreferenceStore(root) if root is not None else None
    app.state.preference_store = store

    @app.get("/api/app/preferences", response_model=PreferencesSnapshot)
    async def read_preferences(request: Request):
        user = await current_user(request)
        return store.get(user)

    @app.put("/api/app/preferences", response_model=PreferencesSnapshot)
    async def save_preferences(request: Request, update: PreferencesUpdate):
        user = await current_user(request)
        return store.save(user, update)
