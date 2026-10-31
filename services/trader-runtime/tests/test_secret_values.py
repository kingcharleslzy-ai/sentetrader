from pathlib import Path

import pytest

from trader_runtime.secret_values import secret_value


def test_reads_secret_from_absolute_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    path = tmp_path / "credential"
    path.write_text("secret-value\n", encoding="utf-8")
    monkeypatch.setenv("EXAMPLE_SECRET_FILE", str(path))

    assert secret_value("EXAMPLE_SECRET") == "secret-value"


def test_rejects_ambiguous_secret_sources(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    path = tmp_path / "credential"
    path.write_text("file-value", encoding="utf-8")
    monkeypatch.setenv("EXAMPLE_SECRET", "environment-value")
    monkeypatch.setenv("EXAMPLE_SECRET_FILE", str(path))

    with pytest.raises(RuntimeError, match="example_secret_source_ambiguous"):
        secret_value("EXAMPLE_SECRET")
