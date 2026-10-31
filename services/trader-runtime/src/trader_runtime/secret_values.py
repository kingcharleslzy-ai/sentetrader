from __future__ import annotations

import os
from pathlib import Path

MAX_SECRET_BYTES = 64 * 1024


def secret_value(name: str, *, required: bool = False) -> str:
    """Read a secret from NAME or NAME_FILE without logging its value."""
    direct = os.getenv(name)
    file_name = os.getenv(f"{name}_FILE")
    if direct is not None and file_name:
        raise RuntimeError(f"{name.lower()}_source_ambiguous")
    if file_name:
        path = Path(file_name)
        if not path.is_absolute():
            raise RuntimeError(f"{name.lower()}_file_must_be_absolute")
        try:
            payload = path.read_bytes()
        except OSError as error:
            raise RuntimeError(f"{name.lower()}_file_unreadable") from error
        if len(payload) > MAX_SECRET_BYTES:
            raise RuntimeError(f"{name.lower()}_file_too_large")
        try:
            value = payload.decode("utf-8").rstrip("\r\n")
        except UnicodeDecodeError as error:
            raise RuntimeError(f"{name.lower()}_file_invalid") from error
    else:
        value = direct or ""
    if required and not value:
        raise RuntimeError(f"{name.lower()}_required")
    return value
