from __future__ import annotations

import os
import secrets
from pathlib import Path

from .main import serve


def _secret_file(name: str) -> str:
    raw_path = os.getenv(f"{name}_FILE", "")
    path = Path(raw_path)
    if not raw_path or not path.is_absolute():
        raise RuntimeError(f"{name.lower()}_file_required")
    try:
        payload = path.read_bytes()
    except OSError as error:
        raise RuntimeError(f"{name.lower()}_file_unreadable") from error
    if len(payload) > 64 * 1024:
        raise RuntimeError(f"{name.lower()}_file_too_large")
    try:
        value = payload.decode("utf-8").rstrip("\r\n")
    except UnicodeDecodeError as error:
        raise RuntimeError(f"{name.lower()}_file_invalid") from error
    if not value:
        raise RuntimeError(f"{name.lower()}_required")
    return value


def main() -> int:
    mode = os.getenv("AI_TRADER_TQKQ_MODE", "tqkq-simulation")
    if mode not in {"tqkq-readonly", "tqkq-simulation"}:
        raise RuntimeError("sidecar_mode_invalid")
    symbols = [
        item.strip()
        for item in os.getenv(
            "AI_TRADER_TQKQ_SYMBOLS", "SHFE.au2612,SHFE.rb2610"
        ).split(",")
        if item.strip()
    ]
    return serve({
        "token": _secret_file("AI_TRADER_READONLY_SIDECAR_TOKEN"),
        "port": int(os.getenv("AI_TRADER_TQKQ_SIDECAR_PORT", "19001")),
        "symbols": symbols,
        "instanceId": secrets.token_hex(32),
        "parentPid": 0,
        "mode": mode,
        "username": _secret_file("AI_TRADER_TQKQ_USERNAME"),
        "password": _secret_file("AI_TRADER_TQKQ_PASSWORD"),
        # Credentials come from files, so a wedged process can re-exec itself and read them again.
        "reexecOnWedge": True,
    })


if __name__ == "__main__":
    raise SystemExit(main())
