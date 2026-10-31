from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import threading
from pathlib import Path
from typing import Any

from .store import RuntimeStore, utc_now


def _native_path(path: Path) -> str:
    resolved = str(path.resolve(strict=False))
    if os.name != "nt" or resolved.startswith("\\\\?\\"):
        return resolved
    if resolved.startswith("\\\\"):
        unc_path = resolved.lstrip("\\")
        return f"\\\\?\\UNC\\{unc_path}"
    return f"\\\\?\\{resolved}"


def _replace_path(source: Path, destination: Path) -> None:
    os.replace(_native_path(source), _native_path(destination))


def _chmod_path(path: Path, mode: int) -> None:
    os.chmod(_native_path(path), mode)


class StrategyReleaseManager:
    """Freezes the strategy source a backtest ran against as a content-addressed
    snapshot outside the editable agent workspace. Backtest runs reference it by
    `strategyReleaseId`; nothing is deployed from it."""

    def __init__(self, *, engineering_root: Path, state_root: Path, store: RuntimeStore) -> None:
        self.engineering_root = engineering_root.resolve()
        self.state_root = state_root.resolve()
        self.store = store
        self._lock = threading.RLock()
        self.releases_root = self.state_root / "strategy-runtime" / "releases"
        self.releases_root.mkdir(parents=True, exist_ok=True)
        if (self.state_root == self.engineering_root
                or self.engineering_root in self.state_root.parents):
            raise ValueError("runtime_state_must_be_outside_engineering_root")
        self._migrate_short_release_directories()
        self._remove_empty_deployments_root()

    def _remove_empty_deployments_root(self) -> None:
        # The deployment-candidate chain is gone (2026-09-25). Its directory is
        # removed only when empty; anything left inside stays for inspection.
        legacy = self.state_root / "strategy-runtime" / "deployments"
        try:
            legacy.rmdir()
        except OSError:
            pass

    def _migrate_short_release_directories(self) -> None:
        rows = self.store.connection.execute(
            "SELECT id, artifact_digest FROM strategy_releases"
        ).fetchall()
        for row in rows:
            canonical = self.releases_root / row["id"]
            legacy = self.releases_root / f"sha256-{row['artifact_digest'][:16]}"
            if not canonical.exists() and legacy.exists():
                _replace_path(legacy, canonical)
            if not canonical.exists():
                continue
            manifest_path = canonical / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            artifacts = [item for item in canonical.iterdir() if item.name != "manifest.json"]
            if len(artifacts) != 1 or hashlib.sha256(artifacts[0].read_bytes()).hexdigest() != row["artifact_digest"]:
                raise RuntimeError("strategy_release_integrity_failed")
            if (manifest.get("releaseId") == row["id"]
                    and manifest.get("artifactName") == artifacts[0].name):
                continue
            _chmod_path(canonical, 0o755)
            _chmod_path(manifest_path, 0o644)
            manifest["releaseId"] = row["id"]
            manifest["artifactName"] = artifacts[0].name
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            _chmod_path(manifest_path, 0o444)
            _chmod_path(canonical, 0o555)

    def ensure_release(self, *, strategy_path: str) -> dict[str, Any]:
        """Create or reuse an immutable strategy artifact."""
        source = (self.engineering_root / strategy_path).resolve()
        if self.engineering_root not in source.parents or not source.is_file():
            raise ValueError("strategy_source_not_allowed")
        content = source.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        release_id = f"sha256-{digest}"
        with self._lock:
            release_manifest = self._ensure_release(
                source=source, source_ref=strategy_path, content=content,
                digest=digest, release_id=release_id,
            )
            self.store.save_strategy_release(
                release_id=release_id, artifact_digest=digest, source_ref=strategy_path,
                manifest=release_manifest,
            )
        return release_manifest

    def release_artifact_path(self, release_id: str) -> Path:
        release = self.store.get_strategy_release(release_id)
        manifest = release["manifest"]
        artifact_name = manifest.get("artifactName")
        if not isinstance(artifact_name, str) or not artifact_name:
            artifacts = [
                item for item in (self.releases_root / release_id).iterdir()
                if item.name != "manifest.json"
            ]
            if len(artifacts) != 1:
                raise RuntimeError("strategy_release_integrity_failed")
            artifact_name = artifacts[0].name
        path = self.releases_root / release_id / artifact_name
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != release["artifactDigest"]:
            raise RuntimeError("strategy_release_integrity_failed")
        return path

    def _ensure_release(
        self, *, source: Path, source_ref: str, content: bytes,
        digest: str, release_id: str,
    ) -> dict[str, Any]:
        release_dir = self.releases_root / release_id
        manifest = {
            "schemaVersion": 1, "releaseId": release_id, "artifactDigest": digest,
            "sourceRef": source_ref, "artifactName": source.name, "createdAt": utc_now(),
            "executionEnabled": False, "orderCapabilities": [],
        }
        if not release_dir.exists():
            temporary = Path(tempfile.mkdtemp(prefix="release-", dir=self.releases_root))
            try:
                artifact = temporary / source.name
                artifact.write_bytes(content)
                (temporary / "manifest.json").write_text(
                    json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
                )
                _chmod_path(artifact, 0o444)
                _chmod_path(temporary / "manifest.json", 0o444)
                try:
                    _replace_path(temporary, release_dir)
                except OSError:
                    if not release_dir.is_dir():
                        raise
                    shutil.rmtree(temporary, ignore_errors=True)
                _chmod_path(release_dir, 0o555)
            except Exception:
                shutil.rmtree(temporary, ignore_errors=True)
                raise
        persisted_manifest = json.loads((release_dir / "manifest.json").read_text(encoding="utf-8"))
        artifact_name = persisted_manifest.get("artifactName")
        if not isinstance(artifact_name, str) or not artifact_name:
            artifacts = [item for item in release_dir.iterdir() if item.name != "manifest.json"]
            if len(artifacts) != 1:
                raise RuntimeError("strategy_release_integrity_failed")
            artifact_name = artifacts[0].name
        persisted_content = (release_dir / artifact_name).read_bytes()
        if (persisted_manifest.get("artifactDigest") != digest
                or hashlib.sha256(persisted_content).hexdigest() != digest):
            raise RuntimeError("strategy_release_integrity_failed")
        return persisted_manifest
