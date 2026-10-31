from pathlib import Path

import pytest

from tqkq_readonly_sidecar.container_entry import _secret_file


def test_container_secret_file_removes_only_line_ending(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "secret"
    path.write_text(" value with spaces \n", encoding="utf-8")
    monkeypatch.setenv("BOUND_PASSWORD_FILE", str(path))

    assert _secret_file("BOUND_PASSWORD") == " value with spaces "


def test_container_secret_file_requires_absolute_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BOUND_PASSWORD_FILE", "relative-secret")

    with pytest.raises(RuntimeError, match="bound_password_file_required"):
        _secret_file("BOUND_PASSWORD")


def test_running_main_as_a_module_runs_the_canonical_module(monkeypatch):
    """`python -m tqkq_readonly_sidecar.main` (pool children) must not run a second copy of the module:
    public_market raises the canonical ReaderGone, which only the canonical handler catches."""
    import runpy

    import tqkq_readonly_sidecar.main as canonical

    called = []
    monkeypatch.setattr(canonical, "main", lambda: called.append(True) or 0)
    try:
        runpy.run_module("tqkq_readonly_sidecar.main", run_name="__main__")
    except SystemExit as stop:
        assert stop.code == 0
    else:
        raise AssertionError("the module entry must exit")
    assert called == [True]
