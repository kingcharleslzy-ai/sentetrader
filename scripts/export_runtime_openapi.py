"""Export the full Runtime REST contract (OpenAPI) for the old-client compatibility baseline.

This builds the real application through ``trader_runtime.main.create_app`` in fake
agent/data mode — no accounts are opened, nothing connects to the network, and the
lifespan is never entered — so every route (workbench, /api/ai-execution, /api/proposals,
…) is captured, unlike ``scripts/export_workbench_openapi.py``, which assembles only
the two workbench route groups for client type generation and stays as-is.

The output feeds ``services/trader-runtime/contracts/openapi-baseline.json``; see that
directory's README for the compatibility rules and when the baseline is regenerated.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "services" / "trader-runtime" / "src"))

from trader_runtime import __version__
from trader_runtime.main import create_app


def resolve_commit(explicit: str | None) -> str:
    """--commit wins, then AI_TRADER_BUILD_SHA, then the current checkout."""
    if explicit:
        return explicit
    from_env = os.getenv("AI_TRADER_BUILD_SHA")
    if from_env:
        return from_env
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, text=True
    ).strip()


def export_openapi(commit: str) -> dict[str, Any]:
    with TemporaryDirectory(prefix="ai-trader-openapi-") as temporary:
        root = Path(temporary)
        app = create_app(
            root / "openapi.sqlite3",
            local_token="openapi-export",
            agent_mode="fake",
            data_mode="fake",
            engineering_root=root / "project",
        )
        spec: dict[str, Any] = app.openapi()
    info = spec.setdefault("info", {})
    info["x-ai-trader"] = {"version": __version__, "commit": commit}
    return spec


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", help="output path (default: stdout)")
    parser.add_argument("--commit", help="source commit recorded in info.x-ai-trader")
    arguments = parser.parse_args()
    spec = export_openapi(resolve_commit(arguments.commit))
    text = json.dumps(spec, sort_keys=True, ensure_ascii=False, indent=1) + "\n"
    if arguments.out:
        target = Path(arguments.out)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
