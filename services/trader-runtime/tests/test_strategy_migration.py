import hashlib

from fastapi.testclient import TestClient

from test_strategy_workbench import plan
from trader_runtime.main import create_app
from trader_runtime.strategy_migration import migrate_local_plans


def test_migration_preserves_source_and_deduplicates_beyond_first_page(tmp_path):
    source_path = tmp_path / "source.sqlite3"
    source = create_app(source_path, runtime_state_root=tmp_path / "source", agent_mode="fake", local_token="migration-source")
    destination = create_app(tmp_path / "destination.sqlite3", runtime_state_root=tmp_path / "destination", agent_mode="fake", local_token="migration-target")
    source_headers = {"Authorization": "Bearer migration-source", "Origin": "http://127.0.0.1:5173"}
    target_headers = {**source_headers, "Authorization": "Bearer migration-target"}
    body = {"plan": plan().model_dump(mode="json"), "instrumentScope": {"mode": "selected", "symbols": ["SHFE.rb2610"]}}
    with TestClient(source, headers=source_headers) as client:
        response = client.post("/api/strategy-plans", json=body)
        assert response.status_code == 201, response.text
    before = hashlib.sha256(source_path.read_bytes()).digest()
    with TestClient(destination, headers=target_headers) as client:
        preview = migrate_local_plans(client, source_path, apply=False)
        assert preview["count"] == 1
        first = migrate_local_plans(client, source_path, apply=True)
        assert first["items"][0]["status"] == "draft"
        for number in range(51):
            unrelated = {**body, "plan": {**body["plan"], "name": f"独立策略 {number}"}}
            assert client.post("/api/strategy-plans", json=unrelated).status_code == 201
        repeated = migrate_local_plans(client, source_path, apply=True)
        assert repeated["items"][0]["targetId"] == first["items"][0]["targetId"]
        library = client.get("/api/strategy-plans").json()
        assert not library["executions"]
        # The save response itself has already verified the normalized full definition.
        assert repeated["items"][0]["digest"] == preview["items"][0]["digest"]
    assert hashlib.sha256(source_path.read_bytes()).digest() == before
