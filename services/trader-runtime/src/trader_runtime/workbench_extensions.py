"""Declarative extensions; data only, never executable code or core patches."""
from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ChartExtension(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,39}$")
    name: str = Field(min_length=1, max_length=40)
    kind: Literal["sma", "ema", "price_channel"]
    period: int = Field(ge=2, le=200)
    color: str = Field(default="#8d9eff", pattern=r"^#[0-9a-fA-F]{6}$")
    enabled: bool = True


class WorkbenchConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    showAiActivity: bool = True
    showTradeMarkers: bool = True
    showMarketBriefs: bool = True
    extensions: list[ChartExtension] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def unique_ids(self):
        if len({item.id for item in self.extensions}) != len(self.extensions):
            raise ValueError("扩展标识不能重复")
        return self


class WorkbenchExtensions:
    def __init__(self, root: Path):
        self.root = root / "workbench-extensions"
        self.lock = threading.RLock()

    def _path(self, user):
        return self.root / (hashlib.sha256(user.encode()).hexdigest() + ".json")

    def get(self, user):
        with self.lock:
            path = self._path(user)
            if not path.exists():
                return {"revision": 0, "config": WorkbenchConfig().model_dump(), "canRollback": False}
            saved = json.loads(path.read_text())
            return {"revision": saved["revision"], "config": WorkbenchConfig.model_validate(saved["config"]).model_dump(),
                    "canRollback": saved.get("previous") is not None}

    def save(self, user, config, revision, rollback=False):
        with self.lock:
            current = self.get(user)
            if type(revision) is not int or revision != current["revision"]:
                raise ValueError("配置已变化，请先重新读取再修改")
            path = self._path(user)
            if rollback:
                if not current["canRollback"]:
                    raise ValueError("没有可恢复的上一版本")
                config = json.loads(path.read_text())["previous"]
            next_config = WorkbenchConfig.model_validate(config).model_dump()
            self.root.mkdir(parents=True, exist_ok=True)
            temp = path.with_suffix(f".{uuid4().hex}.tmp")
            try:
                with temp.open("x") as stream:
                    temp.chmod(0o600)
                    json.dump({"revision": revision + 1, "config": next_config,
                               "previous": current["config"]}, stream, ensure_ascii=False)
                temp.replace(path)
            finally:
                temp.unlink(missing_ok=True)
            return self.get(user)
