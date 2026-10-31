"""浏览器引擎与服务端逐字节一致 (规则回测本机优先, 2026-10-12).

The browser runs the Runtime's own engine sources (``GET /api/strategy-engine/bundle``) in Pyodide.
This test runs every template of the template parity fixture through that bundle in the same
Pyodide build under Node (``pyodide_engine.mjs``) and natively, over the same JSON inputs the
inputs route hands out, and requires the results to be byte-identical.

It reaches the engine modules by import, so change-aware selection (scripts/ci_select_checks.mjs)
runs it when an engine or bundle module changes and leaves ordinary PRs alone. The Pyodide files
come from the site's self-hosted copy into ``~/.cache/ai-trader/pyodide/v<version>`` (wheels checked
against the lock file's sha256). Without Node the test is skipped; cache preparation failures fail the check.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import urllib.request
from pathlib import Path

import pytest

from test_backtest_template_parity import parity_history, variants
from trader_runtime.strategy_backtest import setup_inputs
from trader_runtime.strategy_lab import PYODIDE, engine_bundle
from trader_runtime.strategy_lab_engine import run_rule

RUNNER = Path(__file__).with_name("pyodide_engine.mjs")
REPO = Path(__file__).resolve().parents[3]
# The page's side of the same engine: naming them here also selects this test when they change.
CLIENT_ENGINE = REPO / "apps/client/src/strategyEngine/browserEngine.ts"
CLIENT_WORKER = REPO / "apps/client/src/strategyEngine/engineWorker.ts"
SITE = "https://qilinshuzhi.com/trader/pyodide"
CORE = ("pyodide.mjs", "pyodide.asm.mjs", "pyodide.asm.wasm", "python_stdlib.zip", "pyodide-lock.json")


def pyodide_dir() -> Path:
    """The self-hosted Pyodide build, downloaded once (the same files the web page loads)."""
    version = f"v{PYODIDE['version']}"
    target = Path(os.environ.get("AI_TRADER_PYODIDE_DIR") or Path.home() / ".cache/ai-trader/pyodide" / version)
    target.mkdir(parents=True, exist_ok=True)

    def fetch(name: str) -> Path:
        path = target / name
        if not path.exists():
            partial = path.with_suffix(path.suffix + ".part")
            with urllib.request.urlopen(f"{SITE}/{version}/{name}", timeout=120) as response:   # noqa: S310
                partial.write_bytes(response.read())
            partial.rename(path)
        return path

    for name in CORE:
        fetch(name)
    lock = json.loads((target / "pyodide-lock.json").read_text())["packages"]
    wanted, seen = ["pydantic", "tzdata"], set()
    while wanted:
        package = lock[wanted.pop().lower().replace("_", "-")]
        if package["name"] in seen:
            continue
        seen.add(package["name"])
        wheel = fetch(package["file_name"])
        if hashlib.sha256(wheel.read_bytes()).hexdigest() != package["sha256"]:
            wheel.unlink()
            raise RuntimeError(f"{package['file_name']} sha256 mismatch")
        wanted.extend(package.get("depends") or [])
    return target


def test_the_page_loads_the_pyodide_build_and_entries_the_server_ships():
    """The client asks for the Pyodide version the bundle declares and calls the entries the engine has."""
    engine, worker = CLIENT_ENGINE.read_text(encoding="utf-8"), CLIENT_WORKER.read_text(encoding="utf-8")
    assert f'PYODIDE_VERSION = "{PYODIDE["version"]}"' in engine
    assert all(f"_engine.{name}(" in worker for name in ("run_rule", "run_variant"))
    assert '["pydantic", "tzdata"]' in worker and PYODIDE["packages"] == ["pydantic", "tzdata"]


@pytest.mark.timeout(600)
def test_every_template_runs_byte_identical_in_pyodide(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    directory = pyodide_dir()
    cases = {name: json.loads(json.dumps(setup_inputs(setup))) for name, setup in variants(parity_history()).items()}
    bundle = engine_bundle()
    (tmp_path / "cases.json").write_text(json.dumps({
        "bundle": {"modules": bundle["modules"], "entry": "trader_runtime.strategy_lab_engine"}, "cases": cases}))
    subprocess.run([node, str(RUNNER), str(directory), str(tmp_path / "cases.json"), str(tmp_path / "out.json")],
                   check=True, timeout=560)
    browser = json.loads((tmp_path / "out.json").read_text())
    print(f"pyodide ready {browser['readyMs']} ms, {len(cases)} backtests {browser['runMs']} ms")
    assert sorted(browser["results"]) == sorted(cases)
    for name, inputs in cases.items():
        native = json.dumps(run_rule(inputs), sort_keys=True, ensure_ascii=False)
        assert browser["results"][name] == native, name
