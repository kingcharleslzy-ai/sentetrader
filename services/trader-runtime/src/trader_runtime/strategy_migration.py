"""Read legacy local plans without starting a Runtime; save through the account API."""
import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path

from .auth import LOCAL_USER_ID
from .strategy_requests import SavePlanRequest


def read_local_plans(database: Path) -> list[dict]:
    if not database.is_file():
        raise ValueError("本机策略数据库不存在")
    with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute("""SELECT id, definition_json FROM strategy_drafts
            WHERE user_id = ? AND status != 'archived'
            AND json_extract(definition_json, '$.schemaVersion') = 2
            AND json_extract(definition_json, '$.libraryStrategyId') IS NULL ORDER BY created_at, id""", (LOCAL_USER_ID,)).fetchall()
    plans = []
    for row in rows:
        definition = json.loads(row["definition_json"])
        body = SavePlanRequest.model_validate({"plan": definition["plan"], "instrumentScope": definition.get("instrumentScope", {"mode": "any", "symbols": []})}).model_dump(mode="json")
        plans.append({"sourceId": row["id"], "body": body,
                      "digest": hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()})
    return plans


def migrate_local_plans(client, database: Path, *, apply: bool) -> dict:
    plans = read_local_plans(database)  # Validate every plan before the first write.
    results = []
    for plan in plans:
        item = {"sourceId": plan["sourceId"], "name": plan["body"]["plan"]["name"], "digest": plan["digest"]}
        if apply:
            response = client.post("/api/agent/call", json={"operation": "POST /api/strategy-plans", "body": plan["body"]})
            response.raise_for_status()
            result = response.json()
            if not result.get("ok"):
                raise ValueError("策略保存未确认，请读取策略列表；不要启动监控")
            saved = result["data"]
            definition = saved["definition"]
            actual = SavePlanRequest.model_validate({"plan": definition["plan"], "instrumentScope": definition["instrumentScope"]}).model_dump(mode="json")
            if actual != plan["body"]:
                raise ValueError("策略保存结果与原定义不一致，已停止迁移")
            item.update(targetId=saved["strategyId"], status=saved["status"])
        results.append(item)
    return {"sourceRetained": True, "applied": apply, "count": len(results), "items": results,
            "message": "策略已按内容去重保存；没有启动监控或迁移交易授权" if apply else "只读预览；尚未保存策略"}
