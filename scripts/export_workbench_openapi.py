"""Export actual route schemas without constructing Runtime or opening accounts."""
from pathlib import Path
import json
import sys
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services/trader-runtime/src"))

from fastapi import FastAPI
from trader_runtime.workbench_api import register_workbench_api
from trader_runtime.strategy_workbench import register_workbench


async def no_identity(_request):
    raise RuntimeError("schema_export_has_no_runtime_identity")


class _SchemaContext:
    """`register_workbench_api` reads `ctx.current_user` while registering; the
    handlers themselves never run during a schema export."""

    current_user = staticmethod(no_identity)


schema_app = FastAPI(title="AI Trader Workbench API", version="1")
register_workbench_api(schema_app, _SchemaContext(), None, None)
with TemporaryDirectory(prefix="ai-trader-schema-") as temporary:
    register_workbench(schema_app, no_identity, Path(temporary), None, None, None, None, None)
    json.dump(schema_app.openapi(), sys.stdout, ensure_ascii=False)
