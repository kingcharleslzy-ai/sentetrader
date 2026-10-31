"""Shared account/data/execution description; compatibility transport fields stay intact."""


def describe_runtime(status: dict) -> dict:
    simulated = status.get("simulationTrading") is True
    historical = bool(status.get("replayId")) or status.get("adapter") == "historical-replay"
    source = ("historical" if historical
              else "paper" if status.get("accountMode") == "paper"
              else "tqkq" if status.get("accountMode") == "tqkq-readonly"
              else "demo")
    account = {"historical": "历史回放", "paper": "本站模拟账户", "tqkq": "快期模拟账户", "demo": "演示账户"}[source]
    ready = status.get("accountStatus") in {"normal", "simulated"}
    execution = "模拟执行可用" if simulated and ready else "模拟执行等待账户连接" if simulated else "仅查看数据"
    return {**status, "dataSource": source, "executionMode": "simulation" if simulated else "read_only",
            "accountLabel": account, "executionLabel": execution, "liveTrading": False,
            "capabilitySummary": f"{account} · {execution} · 实盘关闭"}
