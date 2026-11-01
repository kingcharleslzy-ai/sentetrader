"""界面表面一张表 (owner 2026-11-01): every view, panel, page, tab, dialog and disclosure id a UI command can
name, with its old aliases, labels, spoken words and guest flag, lives in `ui_surfaces.json` next to this
module (its "about" says what each field means). The client imports the same file (uiSurfaces.ts), so a
surface is added there once; ui_control.py, guest.py and voice_intents.py read it through these helpers."""
from __future__ import annotations

import json
from pathlib import Path

SURFACES: dict = json.loads((Path(__file__).resolve().parent / "ui_surfaces.json").read_text(encoding="utf-8"))


def surface_ids(family: str, *, state: bool | None = False) -> tuple[str, ...]:
    """The family's ids in schema order: the ones a command may name, with `state` only the ones an older
    client may still report (stateOnly), with `state=None` all of them."""
    return tuple(name for name, entry in SURFACES[family].items() if state is None or bool(entry.get("stateOnly")) == state)


def surface_aliases(family: str) -> dict[str, str]:
    """Old id → the id it stands for now."""
    return {name: entry["aliasOf"] for name, entry in SURFACES[family].items() if "aliasOf" in entry}


def surface_labels(family: str, field: str = "label") -> dict[str, str]:
    """id → its label (or another text field such as `words`); an alias reads its target's."""
    entries = SURFACES[family]
    return {name: entries[entry.get("aliasOf", name)][field] for name, entry in entries.items()
            if field in entries[entry.get("aliasOf", name)]}


def surfaces_with(family: str, flag: str) -> frozenset[str]:
    """The ids whose entry sets `flag` (guest, closeOnly, card, sparse …)."""
    return frozenset(name for name, entry in SURFACES[family].items() if entry.get(flag))
