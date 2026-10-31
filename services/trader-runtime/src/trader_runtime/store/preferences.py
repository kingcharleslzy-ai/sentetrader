from __future__ import annotations

import json
from typing import Any

from fastapi import HTTPException
from pydantic import ValidationError

from ..user_preferences import (
    UserPreferences,
    merge_preferences,
    restore_unknown_fields,
    split_known_fields,
    without_retired_skills,
)
from .base import StoreBase, utc_now


class PreferencesStoreMixin(StoreBase):
    """`user_preferences` rows: one JSON document and a version per user (user_preferences.py owns the schema)."""

    def _stored_preferences(self, user_id: str) -> tuple[int, UserPreferences, dict[str, Any]] | None:
        """The row's version, the fields this Runtime knows and the stranger fields.

        A document written by a newer Runtime keeps its known fields — an explicit
        off included — instead of falling back to whole-document defaults (522-F);
        the unknown keys ride along untouched for the next write-back."""
        row = self.connection.execute(
            "SELECT version, preferences_json FROM user_preferences WHERE user_id = ?", (user_id,)).fetchone()
        if row is None:
            return None
        try:
            document = json.loads(row["preferences_json"])
        except ValueError:
            document = {}
        known, unknown = split_known_fields(UserPreferences, document if isinstance(document, dict) else {})
        known = without_retired_skills(known)
        try:
            preferences = UserPreferences.model_validate(known)
        except (ValueError, ValidationError):
            # Damaged values in the fields we know: defaults, never a crash on read.
            preferences = UserPreferences()
        return int(row["version"]), preferences, unknown

    def get_preferences(self, user_id: str) -> dict[str, Any]:
        stored = self._stored_preferences(user_id)
        if stored is None:
            return {"version": 0, "preferences": UserPreferences().model_dump()}
        version, preferences, _unknown = stored
        return {"version": version, "preferences": preferences.model_dump()}

    def user_preferences(self, user_id: str) -> UserPreferences:
        return UserPreferences.model_validate(self.get_preferences(user_id)["preferences"])

    def update_preferences(self, user_id: str, patch: dict[str, Any], *, expected_version: int | None = None) -> dict[str, Any]:
        with self.lock, self.connection:
            stored = self._stored_preferences(user_id)
            version = 0 if stored is None else stored[0]
            current = UserPreferences() if stored is None else stored[1]
            if expected_version is not None and version != expected_version:
                raise HTTPException(409, "其他设备已更新设置，请读取最新设置后再修改")
            merged = merge_preferences(current, patch)
            unknown = {} if stored is None else stored[2]
            version += 1
            stored_json = restore_unknown_fields(merged.model_dump(), unknown)
            self.connection.execute(
                "INSERT INTO user_preferences (user_id, version, preferences_json, updated_at) VALUES (?, ?, ?, ?)"
                " ON CONFLICT(user_id) DO UPDATE SET version = excluded.version,"
                " preferences_json = excluded.preferences_json, updated_at = excluded.updated_at",
                (user_id, version, json.dumps(stored_json, ensure_ascii=False), utc_now()))
            return {"version": version, "preferences": merged.model_dump()}

