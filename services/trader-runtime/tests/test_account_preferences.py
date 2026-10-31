import httpx
import pytest

from test_owner_auth import ORIGIN, PASSWORD, owner_app  # noqa: F401  # pytest fixture
from trader_runtime.preferences import PreferenceStore, PreferencesUpdate
from trader_runtime.runtime_presentation import describe_runtime

PREFERENCES = {"theme": "light", "fontScale": "large", "density": "comfortable", "dockSide": "left",
    "chart": {"kd": True, "macd": False, "volume": True, "crosshair": True, "lastPriceLine": False,
              "ma5": True, "ma10": True, "ma25": False, "ma60": True, "ma200": True},
    "chartSizes": {"main": 58, "kd": 19, "macd": 23}}
# What the Runtime returns for that payload: an old client omits the positions bar state and the default fills it.
STORED = {**PREFERENCES, "positionsBar": "collapsed", "recentSymbols": [], "pinnedSymbols": [], "initialBalance": None}


async def test_two_devices_conflict_and_legacy_config_cannot_erase_preferences(owner_app):
    options = {"transport": httpx.ASGITransport(owner_app), "base_url": "https://runtime.example.test", "headers": {"Origin": ORIGIN}}
    async with httpx.AsyncClient(**options) as first, httpx.AsyncClient(**options) as second:
        for client in (first, second):
            assert (await client.post("/api/auth/owner", json={"username": "charles", "password": PASSWORD})).status_code == 200
        assert (await first.get("/api/app/preferences")).json() == {"revision": 0, "preferences": None}
        assert (await first.put("/api/app/preferences", json={"revision": 0, "preferences": PREFERENCES})).status_code == 200
        conflict = await second.put("/api/app/preferences", json={"revision": 0, "preferences": {**PREFERENCES, "theme": "dark"}})
        assert conflict.status_code == 409
        old_config = (await second.get("/api/app/config")).json()
        assert (await second.put("/api/app/config", json={"revision": old_config["revision"], "config": old_config["config"]})).status_code == 200
        assert (await second.get("/api/app/preferences")).json() == {"revision": 1, "preferences": STORED}
        assert (await first.put("/api/app/preferences", json={"revision": 1, "preferences": {**PREFERENCES, "symbol": "SHFE.rb2610"}})).status_code == 422


def test_preferences_are_owner_scoped_and_survive_restart(tmp_path):
    store = PreferenceStore(tmp_path)
    store.save("alice", PreferencesUpdate(revision=0, preferences=PREFERENCES))
    restarted = PreferenceStore(tmp_path)
    assert restarted.get("alice")["preferences"] == STORED
    assert restarted.get("bob")["preferences"] is None


@pytest.mark.parametrize("enabled", [True, False])
def test_data_read_only_is_distinct_from_simulation_execution(enabled):
    result = describe_runtime({"accountMode": "tqkq-readonly", "accountStatus": "normal",
                              "tradingGatewayStatus": "read_only", "simulationTrading": enabled})
    assert result["dataSource"] == "tqkq"
    assert result["executionMode"] == ("simulation" if enabled else "read_only")
    assert result["liveTrading"] is False
    assert result["executionLabel"] == ("模拟执行可用" if enabled else "仅查看数据")
