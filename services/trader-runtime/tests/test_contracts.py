import json
from pathlib import Path

import pytest

from trader_runtime.contracts import validate_event

FIXTURES = Path(__file__).resolve().parents[3] / "packages" / "ui-events" / "fixtures" / "events.v1.json"


def test_shared_v1_fixtures_satisfy_python_runtime_contract():
    for event in json.loads(FIXTURES.read_text(encoding="utf-8")):
        validate_event(event)


def test_unknown_events_fail_closed_before_persistence():
    event = json.loads(FIXTURES.read_text(encoding="utf-8"))[0]
    event["name"] = "future.card"
    with pytest.raises(ValueError, match="unknown_event_name"):
        validate_event(event)


def test_deep_event_validation_rejects_invalid_nested_values():
    event = {
        "schemaVersion": 1,
        "id": "deep-contract",
        "sessionId": "session-contract",
        "kind": "progress",
        "name": "task.progress",
        "timestamp": "2026-08-25T12:00:00+00:00",
        "payload": {
            "turnId": "turn", "taskId": "task", "title": "任务", "subtitle": "",
            "progress": 120, "status": "running",
            "steps": [{"id": "one", "label": "步骤", "status": "not-a-status", "detail": ""}],
        },
    }
    with pytest.raises(ValueError, match="invalid_event_payload"):
        validate_event(event)


def test_deep_event_validation_rejects_non_finite_risk_amounts():
    event = {
        "schemaVersion": 1,
        "id": "risk-contract",
        "sessionId": "session-contract",
        "kind": "approval",
        "name": "proposal.trade",
        "timestamp": "2026-08-25T12:00:00+00:00",
        "payload": {
            "turnId": "turn", "proposalId": "proposal", "version": 1,
            "symbol": "AU2612", "name": "沪金", "side": "buy_open", "volume": 1,
            "limitPrice": 800, "stopPrice": 799, "contractMultiplier": 1000,
            "updatedAt": "now", "environment": "simulation", "status": "risk_blocked",
            "riskCheck": {"passed": False, "limit": 1000, "estimatedLoss": float("inf"),
                          "reason": "invalid", "contractMultiplier": 1000, "priceTick": "0.02"},
        },
    }
    with pytest.raises(ValueError, match="invalid_event_payload"):
        validate_event(event)
