from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

import uvicorn

from .data_providers import SidecarReadOnlyDataProvider
from .model_provider import DEEPSEEK_MODELS, DEFAULT_DEEPSEEK_MODEL


def process_is_alive(pid: int) -> bool:
    if pid <= 1:
        return False
    if os.name == "nt":
        import ctypes

        synchronize = 0x00100000
        wait_timeout = 0x00000102
        handle = ctypes.windll.kernel32.OpenProcess(synchronize, False, pid)
        if not handle:
            return False
        try:
            return ctypes.windll.kernel32.WaitForSingleObject(handle, 0) == wait_timeout
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except ProcessLookupError:
        return False


def start_parent_watchdog(parent_pid: int) -> None:
    def watch() -> None:
        while process_is_alive(parent_pid):
            time.sleep(0.5)
        os._exit(0)

    threading.Thread(target=watch, name="desktop-parent-watchdog", daemon=True).start()


def read_config() -> dict[str, Any]:
    line = sys.stdin.readline(64 * 1024)
    if not line:
        raise RuntimeError("runtime_config_missing")
    config = json.loads(line)
    required = {
        "port", "runtimeToken", "sidecarUrl", "sidecarToken",
        "engineeringRoot", "stateRoot", "symbols", "instanceId", "parentPid",
    }
    if not isinstance(config, dict) or not required.issubset(config):
        raise RuntimeError("runtime_config_invalid")
    if not isinstance(config["runtimeToken"], str) or len(config["runtimeToken"]) < 32:
        raise RuntimeError("runtime_token_invalid")
    if not isinstance(config["sidecarToken"], str) or len(config["sidecarToken"]) < 32:
        raise RuntimeError("sidecar_token_invalid")
    if not isinstance(config["port"], int) or not 0 <= config["port"] <= 65535:
        raise RuntimeError("runtime_port_invalid")
    if not isinstance(config["instanceId"], str) or len(config["instanceId"]) < 32:
        raise RuntimeError("runtime_instance_invalid")
    if not isinstance(config["parentPid"], int) or config["parentPid"] <= 1:
        raise RuntimeError("runtime_parent_invalid")
    if config.get("dataMode", "tqkq-readonly") not in {
        "fixture",
        "tqkq-readonly",
        "tqkq-simulation",
    }:
        raise RuntimeError("runtime_data_mode_invalid")
    provider = config.get("agentProvider", "deepseek")
    if provider != "deepseek":
        raise RuntimeError("runtime_agent_provider_invalid")
    model = str(config.get("agentModel", DEFAULT_DEEPSEEK_MODEL))
    if provider == "deepseek" and model not in DEEPSEEK_MODELS:
        raise RuntimeError("runtime_agent_model_invalid")
    api_key = config.get("agentApiKey")
    if api_key is not None and (
        provider != "deepseek"
        or not isinstance(api_key, str)
        or not api_key.startswith("sk-")
        or len(api_key) < 12
        or any(character.isspace() for character in api_key)
    ):
        raise RuntimeError("runtime_agent_key_invalid")
    return config


def serve(config: dict[str, Any]) -> int:
    from .main import create_app

    start_parent_watchdog(int(config["parentPid"]))
    provider = SidecarReadOnlyDataProvider(
        base_url=str(config["sidecarUrl"]),
        token=str(config["sidecarToken"]),
        symbols=tuple(str(item) for item in config["symbols"]),
        mode="fake" if config.get("dataMode") == "fixture" else "tqkq-readonly",
    )
    gateway = provider if config.get("dataMode") == "tqkq-simulation" else None
    agent_model = str(config.get("agentModel", DEFAULT_DEEPSEEK_MODEL))
    agent_api_key = config.pop("agentApiKey", None)
    requested_mode = str(config.get("agentMode") or "native")
    agent_mode: Literal["fake", "native"] = "fake" if requested_mode == "fake" else "native"
    # The desktop's local DeepSeek key path: the key never touches disk or a
    # subprocess; the native loop reads it from the endpoint table only.
    os.environ.setdefault("AI_TRADER_AGENT_MODEL", agent_model)
    application = create_app(
        local_token=str(config["runtimeToken"]),
        agent_mode=agent_mode,
        deepseek_api_key=agent_api_key if isinstance(agent_api_key, str) else None,
        engineering_root=Path(str(config["engineeringRoot"])),
        runtime_state_root=Path(str(config["stateRoot"])),
        data_provider=provider,
        runtime_instance_id=str(config["instanceId"]),
        simulation_gateway=gateway,
        agent_provider="deepseek",
    )
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", int(config["port"])))
    listener.listen(2048)
    actual_port = int(listener.getsockname()[1])
    cli_connection = Path(str(config["stateRoot"])) / "private-credentials" / "agent-cli.json"
    cli_connection.parent.mkdir(parents=True, exist_ok=True)
    temporary = cli_connection.with_name("agent-cli-" + uuid4().hex + ".tmp")
    with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as stream:
        json.dump({"url": f"http://127.0.0.1:{actual_port}", "token": config["runtimeToken"]}, stream)
    temporary.replace(cli_connection)
    print(json.dumps({
        "status": "ready", "port": actual_port,
        "instanceId": config["instanceId"], "liveTrading": "disabled",
    }), flush=True)
    uvicorn.Server(uvicorn.Config(
        application,
        host="127.0.0.1",
        port=actual_port,
        access_log=False,
        log_level="warning",
        timeout_graceful_shutdown=3,
    )).run(sockets=[listener])
    return 0


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "mcp":
        from .app_mcp import main as mcp_main
        return mcp_main(sys.argv[2:])
    if len(sys.argv) > 1 and sys.argv[1] == "cli":
        from .app_cli import main as cli_main
        return cli_main(sys.argv[2:])
    try:
        return serve(read_config())
    except Exception as error:
        # Deliberately report only the type. Startup configuration contains secrets.
        print(json.dumps({"status": "failed", "error": type(error).__name__}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
