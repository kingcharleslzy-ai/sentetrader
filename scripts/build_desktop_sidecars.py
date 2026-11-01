from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BINARIES = ROOT / "apps" / "desktop" / "src-tauri" / "binaries"


def target_triple() -> str:
    output = subprocess.check_output(["rustc", "-vV"], text=True)
    for line in output.splitlines():
        if line.startswith("host: "):
            return line.removeprefix("host: ").strip()
    raise RuntimeError("rust_target_not_found")


def executable_suffix(triple: str) -> str:
    return ".exe" if "windows" in triple else ""


def source_digest(paths: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in sorted(paths):
        digest.update(str(path.relative_to(ROOT)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def project_sources(project: Path) -> list[Path]:
    return [
        item for item in project.rglob("*")
        if item.is_file() and (
            # The agent_skills SKILL.md files are packaged data: a registry
            # change must rebuild the sidecar like a code change would.
            item.suffix in {".py", ".toml", ".lock"}
            or (item.suffix == ".md" and "agent_skills" in item.parts)
            or (item.suffix == ".json" and ("strategy_templates" in item.parts or "help" in item.parts))
            or item.name == "ui_surfaces.json"
        )
        and ".venv" not in item.parts and "tests" not in item.parts
    ]


def shared_dependency_environment(project: Path) -> Path | None:
    lock = (project / "uv.lock").read_bytes()
    environment = Path.home() / ".cache/ai-trader/python-deps" / hashlib.sha256(lock).hexdigest()
    marker = environment / "ai-trader-uv.lock"
    if marker.is_file() and marker.read_bytes() == lock and (environment / "pyvenv.cfg").is_file():
        return environment
    return None


def dependency_project(project: Path) -> Path:
    """Use an existing exact-lock environment; source always comes from this checkout."""
    if (project / ".venv").is_dir():
        return project
    if shared_dependency_environment(project) is not None:
        return project
    relative = project.relative_to(ROOT)
    # Only the self-hosted runner checkout may lend its exact-lock environment;
    # PYTHONPATH below still points PyInstaller at this checkout's sources.
    candidates = [
        Path.home() / ".local/share/ai-trader-actions-runner/_work/ai-trader/ai-trader",
    ]
    for root in candidates:
        candidate = root / relative
        if (candidate / ".venv").is_dir() and (candidate / "uv.lock").read_bytes() == (project / "uv.lock").read_bytes():
            return candidate
    # release:all builds it in preflight (#932 B7); by hand: node scripts/shared_python_env.mjs <project>.
    raise RuntimeError(f"sidecar_exact_lock_environment_missing: run `node scripts/shared_python_env.mjs {relative.as_posix()}` first")


def build(
    name: str, project: Path, entry: Path, *, exclude_modules: tuple[str, ...] = (),
    data_files: tuple[tuple[Path, str], ...] = (),
) -> None:
    triple = target_triple()
    suffix = executable_suffix(triple)
    output = BINARIES / f"{name}-{triple}{suffix}"
    fingerprint_path = BINARIES / f".{name}-{triple}.sha256"
    sources = project_sources(project) + [entry, Path(__file__)]
    fingerprint = source_digest(sources)
    shared = Path(os.environ.get("AI_TRADER_SIDECAR_CACHE", Path.home() / ".cache" / "ai-trader" / "sidecars")) / triple / name
    cached = shared / fingerprint
    if output.is_file() and fingerprint_path.is_file() and fingerprint_path.read_text() == fingerprint:
        print(f"{name}: up to date")
        return
    if cached.is_file():
        BINARIES.mkdir(parents=True, exist_ok=True)
        shutil.copy2(cached, output)
        fingerprint_path.write_text(fingerprint, encoding="utf-8")
        print(f"{name}: reused shared artifact")
        return

    BINARIES.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f"ai-trader-{name}-"))
    try:
        command = [
            "uv", "run", "--project", str(dependency_project(project)),
            "--with", "pyinstaller==6.22.2",
        ]
        shared_environment = shared_dependency_environment(project) if not (project / ".venv").is_dir() else None
        if shared_environment is not None:
            command.insert(2, "--no-sync")
        command.extend([
            "pyinstaller", "--clean", "--noconfirm", "--onefile",
            "--python-option", "X utf8=1",
            "--name", name,
            "--distpath", str(temporary / "dist"),
            "--workpath", str(temporary / "work"),
            "--specpath", str(temporary / "spec"),
            str(entry),
        ])
        for source, destination in data_files:
            # Static data the module analysis cannot see (the skill registry's
            # SKILL.md tree); destination is inside the bundle's package dir.
            command[command.index(str(entry)):command.index(str(entry))] = [
                "--add-data", f"{source}{os.pathsep}{destination}",
            ]
        for module in exclude_modules:
            command[command.index(str(entry)):command.index(str(entry))] = ["--exclude-module", module]
        environment = dict(os.environ)
        if shared_environment is not None:
            environment["UV_PROJECT_ENVIRONMENT"] = str(shared_environment)
        environment["PYTHONPATH"] = str(project / "src")
        environment["PYTHONHASHSEED"] = "0"
        # Fixed archive timestamps make unchanged dependency bytes useful to delta updates.
        environment["SOURCE_DATE_EPOCH"] = "315532800"
        subprocess.run(command, cwd=ROOT, env=environment, check=True)
        built = temporary / "dist" / f"{name}{suffix}"
        if not built.is_file():
            raise RuntimeError(f"sidecar_build_missing:{name}")
        staging = BINARIES / f".{name}-{triple}.tmp{suffix}"
        shutil.copy2(built, staging)
        if os.name != "nt":
            staging.chmod(0o755)
        os.replace(staging, output)
        fingerprint_path.write_text(fingerprint, encoding="utf-8")
        shared.mkdir(parents=True, exist_ok=True)
        shared_staging = shared / f".{fingerprint}-{os.getpid()}"
        shutil.copy2(output, shared_staging)
        os.replace(shared_staging, cached)
        # One current artifact and one rollback artifact per component/architecture.
        previous = sorted((p for p in shared.iterdir() if not p.name.startswith(".")),
                          key=lambda p: p.stat().st_mtime, reverse=True)
        for stale in previous[2:]:
            if stale != cached:
                if stale.is_dir():
                    shutil.rmtree(stale)
                else:
                    stale.unlink(missing_ok=True)
    finally:
        shutil.rmtree(temporary, ignore_errors=True)


def main() -> int:
    # Only the Runtime is bundled: it is the packaged CLI / MCP entry (`ai-trader-runtime cli …`,
    # `ai-trader-runtime mcp`). The TqKq sidecar and the local Runtime server left the desktop with
    # the local mode (owner decision 2026-09-15); they run on the cloud host.
    triple = target_triple()
    suffix = executable_suffix(triple)
    build(
        "ai-trader-runtime",
        ROOT / "services" / "trader-runtime",
        ROOT / "scripts" / "runtime_sidecar_entry.py",
        exclude_modules=("tqsdk", "pandas", "numpy", "scipy", "matplotlib", "pytest", "tkinter", "IPython"),
        data_files=(
            (ROOT / "services" / "trader-runtime" / "src" / "trader_runtime" / "agent_skills",
             "trader_runtime/agent_skills"),
            # 策略模板库: the template JSON files are packaged data read at startup.
            (ROOT / "services" / "trader-runtime" / "src" / "trader_runtime" / "strategy_templates",
             "trader_runtime/strategy_templates"),
            # 帮助目录: app_help.py loads help/catalog.json at import.
            (ROOT / "services" / "trader-runtime" / "src" / "trader_runtime" / "help",
             "trader_runtime/help"),
            # 界面表面表: ui_surfaces.py loads ui_surfaces.json at import.
            (ROOT / "services" / "trader-runtime" / "src" / "trader_runtime" / "ui_surfaces.json",
             "trader_runtime"),
        ),
    )
    for stale in (BINARIES / "tqkq-runtime", *BINARIES.glob("ai-trader-tqkq-sidecar-*"), *BINARIES.glob(".ai-trader-tqkq-sidecar-*")):
        if stale.is_dir():
            shutil.rmtree(stale)
        elif stale.exists():
            stale.unlink()
    manifest = {
        "schemaVersion": 1,
        "target": triple,
        "binaries": ["ai-trader-runtime"],
        "artifacts": [f"ai-trader-runtime-{triple}{suffix}"],
        "liveTrading": "disabled",
    }
    (BINARIES / "build-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
