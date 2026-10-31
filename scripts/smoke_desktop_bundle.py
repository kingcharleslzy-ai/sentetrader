"""Smoke the packaged Runtime binary as what it is in the cloud-only desktop: the CLI / MCP entry.

The desktop no longer bundles a local Runtime server or the TqKq sidecar (owner decision
2026-09-15); `ai-trader-runtime cli …` must still start, print its usage and emit the MCP launch
configuration without credentials.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TARGET = Path(os.environ.get("AI_TRADER_DESKTOP_ARTIFACT_DIR") or os.environ.get("CARGO_TARGET_DIR", ROOT / "apps/desktop/src-tauri/target"))
MACOS_BINARIES = TARGET / "release/bundle/macos/AI Trader.app/Contents/MacOS"
WINDOWS_BINARIES = TARGET / "release"
TIMEOUT_SECONDS = 60.0


def binary_root() -> Path:
    override = os.environ.get("AI_TRADER_DESKTOP_BINARY_ROOT")
    if override:
        return Path(override).resolve()
    return WINDOWS_BINARIES if os.name == "nt" else MACOS_BINARIES


def executable_name(name: str) -> str:
    return f"{name}.exe" if os.name == "nt" else name


def run(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(binary_root() / executable_name("ai-trader-runtime")), *arguments],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=TIMEOUT_SECONDS, check=False,
    )


def main() -> int:
    root = binary_root()
    # What Tauri bundles is what `prepare:sidecars` left in binaries/ (the Windows cargo `release`
    # dir keeps stale files from older builds, so it is not the place to look).
    binaries = ROOT / "apps/desktop/src-tauri/binaries"
    manifest = json.loads((binaries / "build-manifest.json").read_text(encoding="utf-8"))
    if manifest.get("binaries") != ["ai-trader-runtime"] or any(binaries.glob("ai-trader-tqkq-sidecar-*")) \
            or (root.parent / "Resources" / "tqkq-runtime").exists():
        raise RuntimeError("desktop_bundle_still_ships_tqkq_sidecar")
    usage = run("cli", "--help")
    if usage.returncode != 0 or "capabilities" not in usage.stdout or "mcp-config" not in usage.stdout:
        raise RuntimeError(f"desktop_cli_usage_invalid:{usage.returncode}:{usage.stderr[-300:]}")
    config = run("cli", "mcp-config")
    if config.returncode != 0:
        raise RuntimeError(f"desktop_cli_mcp_config_failed:{config.returncode}:{config.stderr[-300:]}")
    payload = json.loads(config.stdout)
    server = (payload.get("mcpServers") or {}).get("ai-trader") or {}
    if "ai-trader-runtime" not in str(server.get("command", "")) or server.get("args") != ["mcp"] or "token" in json.dumps(payload).lower():
        raise RuntimeError("desktop_cli_mcp_config_invalid")
    print(json.dumps({"status": "ok", "entry": "cli", "bundledSidecars": ["ai-trader-runtime"], "liveTrading": "disabled"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
