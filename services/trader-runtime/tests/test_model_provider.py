from __future__ import annotations

import io
import json
import sys

import pytest

from trader_runtime.model_provider import (
    DEEPSEEK_BASE_URL,
    DEFAULT_DEEPSEEK_MODEL,
    KIMI_MODEL,
    MODEL_CATALOG,
    ZHIPU_BASE_URL,
    validate_model_url,
)
from trader_runtime.native_agent import build_model_endpoints
from trader_runtime.sidecar_entry import read_config


def _desktop_runtime_config(**overrides: object) -> dict[str, object]:
    return {
        "port": 0,
        "runtimeToken": "r" * 64,
        "sidecarUrl": "http://127.0.0.1:45123",
        "sidecarToken": "s" * 64,
        "engineeringRoot": "/tmp/project",
        "stateRoot": "/tmp/state",
        "symbols": ["SHFE.au2612"],
        "instanceId": "i" * 64,
        "parentPid": 42,
        "agentProvider": "deepseek",
        "agentModel": DEFAULT_DEEPSEEK_MODEL,
        **overrides,
    }


def test_desktop_runtime_config_accepts_only_bounded_deepseek_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    valid = _desktop_runtime_config(agentApiKey="sk-valid-transient-key-123456")
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(valid) + "\n"))
    assert read_config()["agentApiKey"] == "sk-valid-transient-key-123456"

    invalid = _desktop_runtime_config(agentApiKey="sk-key with whitespace")
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(invalid) + "\n"))
    with pytest.raises(RuntimeError, match="runtime_agent_key_invalid"):
        read_config()


def test_desktop_unknown_model_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    unknown = _desktop_runtime_config(agentModel="deepseek-unknown")
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(unknown) + "\n"))
    with pytest.raises(RuntimeError, match="runtime_agent_model_invalid"):
        read_config()


def test_every_catalog_provider_gets_its_documented_responses_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    for spec in MODEL_CATALOG:
        monkeypatch.setenv(spec.api_key_env, f"sk-pool-{spec.slug}")
    endpoints = build_model_endpoints(default_provider="deepseek")
    assert set(endpoints) == {spec.slug for spec in MODEL_CATALOG}
    assert endpoints["deepseek-flash"].base_url == DEEPSEEK_BASE_URL
    assert endpoints["glm-5.3-flash"].base_url == ZHIPU_BASE_URL
    assert "gpt-6-astra" not in endpoints
    assert endpoints[KIMI_MODEL].base_url == "https://api.kimi.com/coding/v1" and endpoints[KIMI_MODEL].upstream == "k3.1-pro"
    # The Kimi key only goes to an HTTPS (or local) endpoint, without exposing it.
    monkeypatch.setenv("AI_TRADER_KIMI_BASE_URL", "http://kimi.example/v1")
    with pytest.raises(ValueError, match="model_url_invalid"):
        build_model_endpoints(default_provider="deepseek")
    for slug, endpoint in endpoints.items():
        assert endpoint.api_key == f"sk-pool-{slug}"
        assert endpoint.api_key not in repr(endpoint)


def test_models_without_a_key_are_left_out(monkeypatch: pytest.MonkeyPatch) -> None:
    for spec in MODEL_CATALOG:
        monkeypatch.delenv(spec.api_key_env, raising=False)
        monkeypatch.delenv(f"{spec.api_key_env}_FILE", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-proxy-only")
    assert build_model_endpoints(default_provider="deepseek") == {}
    only_local = build_model_endpoints(default_provider="deepseek", deepseek_api_key="sk-local")
    assert set(only_local) == {"deepseek-flash"}


@pytest.mark.parametrize('url', ['http://remote.example/v1', 'https://user:secret@api.example/v1',
                                  'https://api.example/v1?key=secret', 'https://api.example/v1#secret', '', 'file:///tmp/key'])
def test_model_url_rejects_unsafe_endpoints(url: str) -> None:
    with pytest.raises(ValueError, match='model_url_invalid'):
        validate_model_url(url)
    assert validate_model_url('http://127.0.0.1:18317/v1/') == 'http://127.0.0.1:18317/v1'
