"""Explicit self-host password resets use the same authority as web changes."""
import importlib.util
import json
from pathlib import Path

import pytest

from trader_runtime.auth import hash_owner_password, verify_owner_password
from trader_runtime.store import RuntimeStore

SCRIPT = Path(__file__).resolve().parents[3] / "deploy/self-host/sente.py"


@pytest.mark.parametrize("reset", [False, True])
def test_setup_preserves_or_explicitly_resets_the_web_password(tmp_path, monkeypatch, reset):
    spec = importlib.util.spec_from_file_location("sente_setup", SCRIPT)
    sente = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sente)
    monkeypatch.setattr(sente, "HOME", tmp_path)
    monkeypatch.setattr(sente, "CONFIG", tmp_path / "config.json")
    monkeypatch.setattr(sente, "SECRETS", tmp_path / "secrets")
    sente.CONFIG.write_text(json.dumps({"username": "sente", "port": 8787}))
    sente.write_secret("owner_password_hash", hash_owner_password("bootstrap-password"))
    sente.write_secret("owner_session_secret", "unchanged-shared-session-key")
    database = tmp_path / "data/ai-trader.sqlite3"
    store = RuntimeStore(database)
    store.set_owner_password_hash(hash_owner_password("web-changed-password"))
    store.close()
    answers = iter(["sente", "y" if reset else "n", "test-market", "8787"])
    passwords = iter((["setup-new-password", "setup-new-password"] if reset else []) + ["market-password", ""])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    monkeypatch.setattr(sente.getpass, "getpass", lambda _: next(passwords))
    assert sente.setup() == 0
    reopened = RuntimeStore(database)
    try:
        password = "setup-new-password" if reset else "web-changed-password"
        assert verify_owner_password(password, reopened.get_owner_password_hash())
        assert not verify_owner_password("bootstrap-password", reopened.get_owner_password_hash())
        assert sente.saved_secret("owner_session_secret") == "unchanged-shared-session-key"
    finally:
        reopened.close()


def test_systemd_values_preserve_paths_spaces_specifiers_and_dollars():
    spec = importlib.util.spec_from_file_location("sente_setup", SCRIPT)
    sente = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sente)
    assert sente.systemd_argument('/home/a b/50%/$data', command=True) == '"/home/a b/50%%/$$data"'
    assert sente.systemd_argument('SENTE_HOME=/home/a b/50%/$data') == '"SENTE_HOME=/home/a b/50%%/$data"'
    assert sente.systemd_argument('a"b\\c\nd') == '"a\\"b\\\\c\\nd"'
