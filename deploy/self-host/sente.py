"""Sente 自建版 (owner 2026-10-31): run the same Runtime, TqKq market sidecar and 本站模拟 engine as the
official cloud, as plain processes on the user's own computer, behind one background service.

    python deploy/self-host/sente.py setup              ask for the login, 快期 account and model key
    python deploy/self-host/sente.py serve              run all three and restart any that exits
    python deploy/self-host/sente.py service install    start `serve` at login (launchd / systemd --user / schtasks)
    python deploy/self-host/sente.py service uninstall
    python deploy/self-host/sente.py status

install.sh / install.ps1 call these with the Runtime's Python (it imports trader_runtime for the password hash).
Data, secrets and logs live in SENTE_HOME (default ~/.sente); nothing is written into the checkout.
"""
from __future__ import annotations

import getpass
import json
import os
import re
import secrets
import signal
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
HOME = Path(os.environ.get("SENTE_HOME") or Path.home() / ".sente").expanduser()
CONFIG = HOME / "config.json"
SECRETS = HOME / "secrets"
LOGS = HOME / "logs"
WEB_ROOT = REPO / "apps" / "client" / "dist"
SIDECAR_PORT, PAPER_PORT = 19001, 19011
SERVICE = "com.sente.selfhost"
WINDOWS = os.name == "nt"


def venv_python(project: str) -> Path:
    venv = REPO / "services" / project / ".venv"
    return venv / ("Scripts/python.exe" if WINDOWS else "bin/python")


def write_secret(name: str, value: str) -> None:
    SECRETS.mkdir(parents=True, exist_ok=True)
    if not WINDOWS:
        SECRETS.chmod(0o700)
    path = SECRETS / name
    path.write_text(value + "\n", encoding="utf-8")
    if not WINDOWS:
        path.chmod(0o600)


def ask(prompt: str, default: str = "") -> str:
    answer = input(f"{prompt}{f' [{default}]' if default else ''}: ").strip()
    return answer or default


def ask_secret(prompt: str, *, keep: str | None = None) -> str:
    """Hidden input; Enter keeps the saved value when there is one."""
    hint = "（直接回车保留原值）" if keep else ""
    while True:
        value = getpass.getpass(f"{prompt}{hint}: ").strip()
        if value:
            return value
        if keep:
            return keep
        print("不能为空。")


def saved_secret(name: str) -> str | None:
    path = SECRETS / name
    return path.read_text(encoding="utf-8").strip() if path.exists() else None


def setup() -> int:
    from trader_runtime.auth import hash_owner_password

    config = json.loads(CONFIG.read_text(encoding="utf-8")) if CONFIG.exists() else {}
    print(f"Sente 自建版设置（数据目录 {HOME}）\n")
    while True:
        username = ask("登录用户名", config.get("username", "sente"))
        # The login sheet's rule (CloudLoginSheet): anything else cannot be typed in to log in.
        if re.fullmatch(r"[A-Za-z0-9_.@+-]{3,64}", username):
            break
        print("用户名需 3-64 位字母、数字或 _ . @ + - 组合。")
    if saved_secret("owner_password_hash") and ask("修改登录密码？(y/N)", "n").lower() != "y":
        password_hash = saved_secret("owner_password_hash") or ""
    else:
        while True:
            password = ask_secret("登录密码")
            if password == getpass.getpass("再输一次: ").strip():
                break
            print("两次不一致，请重输。")
        password_hash = hash_owner_password(password)
    print("\n行情与模拟成交用你自己的快期账号（免费注册：https://account.shinnytech.com/）。")
    tqkq_username = ask("快期账号", saved_secret("tqkq_username") or "")
    tqkq_password = ask_secret("快期密码", keep=saved_secret("tqkq_password"))
    print("\nAI 用 DeepSeek（https://platform.deepseek.com/ 申请密钥）；留空则先用离线演示 AI。")
    deepseek = getpass.getpass(
        "DeepSeek 密钥" + ("（直接回车保留原值）" if saved_secret("deepseek_api_key") else "") + ": ").strip()
    deepseek = deepseek or saved_secret("deepseek_api_key") or ""
    port = int(ask("网页端口", str(config.get("port", 8787))))

    write_secret("owner_password_hash", password_hash)
    write_secret("owner_session_secret", saved_secret("owner_session_secret") or secrets.token_urlsafe(48))
    write_secret("sidecar_token", saved_secret("sidecar_token") or secrets.token_urlsafe(48))
    write_secret("tqkq_username", tqkq_username)
    write_secret("tqkq_password", tqkq_password)
    write_secret("deepseek_api_key", deepseek)
    HOME.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps({"username": username, "port": port}, indent=2) + "\n", encoding="utf-8")
    print(f"\n已保存。网页地址：http://127.0.0.1:{port}/")
    return 0


def build_sha() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True, text=True,
                              check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "self-host"


def components(config: dict) -> dict[str, tuple[list[str], dict[str, str]]]:
    """Each process's command and environment: the production compose's settings for one owner."""
    port = int(config["port"])
    token = str(SECRETS / "sidecar_token")
    tqkq = {
        "AI_TRADER_READONLY_SIDECAR_TOKEN_FILE": token,
        "AI_TRADER_TQKQ_USERNAME_FILE": str(SECRETS / "tqkq_username"),
        "AI_TRADER_TQKQ_PASSWORD_FILE": str(SECRETS / "tqkq_password"),
    }
    has_model = bool(saved_secret("deepseek_api_key"))
    runtime_env = {
        "AI_TRADER_AUTH_MODE": "owner-password",
        "AI_TRADER_OWNER_USERNAME": str(config["username"]),
        "AI_TRADER_OWNER_PASSWORD_HASH_FILE": str(SECRETS / "owner_password_hash"),
        "AI_TRADER_OWNER_SESSION_SECRET_FILE": str(SECRETS / "owner_session_secret"),
        "AI_TRADER_ALLOWED_ORIGINS": f"http://127.0.0.1:{port},http://localhost:{port}",
        "AI_TRADER_SECURE_COOKIES": "0",
        "AI_TRADER_BUILD_SHA": build_sha(),
        "AI_TRADER_DB_PATH": str(HOME / "data" / "ai-trader.sqlite3"),
        "AI_TRADER_STATE_ROOT": str(HOME / "data"),
        "AI_TRADER_WEB_ROOT": str(WEB_ROOT),
        "AI_TRADER_AGENT_MODE": "native" if has_model else "fake",
        "AI_TRADER_AGENT_PROVIDER": "deepseek",
        "AI_TRADER_AGENT_MODEL": "deepseek-flash",
        "DEEPSEEK_API_KEY_FILE": str(SECRETS / "deepseek_api_key"),
        "AI_TRADER_DATA_MODE": "tqkq-sidecar",
        "AI_TRADER_READONLY_SIDECAR_URL": f"http://127.0.0.1:{SIDECAR_PORT}",
        "AI_TRADER_READONLY_SIDECAR_TOKEN_FILE": token,
        "AI_TRADER_STATIC_TQKQ_USERNAME_FILE": str(SECRETS / "tqkq_username"),
        "AI_TRADER_SIMULATION_TRADING": "1",
        "AI_TRADER_PAPER_ENGINE_URL": f"http://127.0.0.1:{PAPER_PORT}",
    }
    sidecar = venv_python("tqkq-sidecar")
    return {
        "sidecar": ([str(sidecar), "-m", "tqkq_readonly_sidecar.container_entry"],
                    {**tqkq, "AI_TRADER_TQKQ_MODE": "tqkq-simulation",
                     "AI_TRADER_TQKQ_SIDECAR_PORT": str(SIDECAR_PORT)}),
        "paper": ([str(sidecar), "-m", "tqkq_readonly_sidecar.paper", "--serve"],
                  {**tqkq, "AI_TRADER_PAPER_PORT": str(PAPER_PORT),
                   "AI_TRADER_PAPER_DB": str(HOME / "data" / "paper.sqlite3"),
                   "AI_TRADER_PAPER_FILL_MODE": "realistic"}),
        "runtime": ([str(venv_python("trader-runtime")), "-m", "uvicorn", "trader_runtime.main:app",
                     "--host", "127.0.0.1", "--port", str(port), "--timeout-graceful-shutdown", "5"],
                    runtime_env),
    }


def serve() -> int:
    if not CONFIG.exists():
        raise SystemExit(f"还没设置：先运行 setup（{CONFIG} 不存在）")
    config = json.loads(CONFIG.read_text(encoding="utf-8"))
    (HOME / "data").mkdir(parents=True, exist_ok=True)
    LOGS.mkdir(parents=True, exist_ok=True)
    plan = components(config)
    (HOME / "serve.pid").write_text(str(os.getpid()), encoding="utf-8")
    children: dict[str, subprocess.Popen] = {}
    started: dict[str, float] = {}
    stopping = False

    def stop(*_: object) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    while not stopping:
        for name, (command, extra) in plan.items():
            child = children.get(name)
            if child is not None and child.poll() is None:
                continue
            # A process that keeps dying (wrong 快期 password, port taken) is retried every 30 s, not spun.
            if child is not None and time.monotonic() - started[name] < 30:
                continue
            with open(LOGS / f"{name}.log", "a", encoding="utf-8") as log:
                # No console window per child under the windowless pythonw of the Windows task.
                children[name] = subprocess.Popen(
                    command, cwd=HOME, env={**os.environ, **extra}, stdout=log, stderr=subprocess.STDOUT,
                    creationflags=subprocess.CREATE_NO_WINDOW if WINDOWS else 0)
            started[name] = time.monotonic()
        time.sleep(1)
    for child in children.values():
        child.terminate()
    for child in children.values():
        try:
            child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            child.kill()
    return 0


def service(action: str) -> int:
    python = venv_python("trader-runtime")
    command = [str(python.with_name("pythonw.exe") if WINDOWS else python), str(Path(__file__).resolve()), "serve"]
    if sys.platform == "darwin":
        plist = Path.home() / "Library" / "LaunchAgents" / f"{SERVICE}.plist"
        domain = f"gui/{os.getuid()}"
        subprocess.run(["launchctl", "bootout", f"{domain}/{SERVICE}"], capture_output=True, check=False)
        if action == "uninstall":
            plist.unlink(missing_ok=True)
            return 0
        LOGS.mkdir(parents=True, exist_ok=True)
        import plistlib
        plist.parent.mkdir(parents=True, exist_ok=True)
        plist.write_bytes(plistlib.dumps({
            "Label": SERVICE, "ProgramArguments": command, "RunAtLoad": True, "KeepAlive": True,
            "EnvironmentVariables": {"SENTE_HOME": str(HOME)},
            "StandardOutPath": str(LOGS / "service.log"), "StandardErrorPath": str(LOGS / "service.log"),
        }))
        subprocess.run(["launchctl", "bootstrap", domain, str(plist)], check=True)
    elif WINDOWS:
        # schtasks /End kills only `serve`; the tree kill takes its three children with it.
        pid = HOME / "serve.pid"
        if pid.exists():
            subprocess.run(["taskkill", "/PID", pid.read_text().strip(), "/T", "/F"], capture_output=True, check=False)
        subprocess.run(["schtasks", "/End", "/TN", "Sente"], capture_output=True, check=False)
        subprocess.run(["schtasks", "/Delete", "/TN", "Sente", "/F"], capture_output=True, check=False)
        if action == "uninstall":
            return 0
        if "SENTE_HOME" in os.environ:
            print("提示：Windows 的开机任务只用默认数据目录 %USERPROFILE%\\.sente。")
        task = subprocess.list2cmdline(command)
        subprocess.run(["schtasks", "/Create", "/TN", "Sente", "/SC", "ONLOGON", "/RL", "LIMITED",
                        "/TR", task, "/F"], check=True)
        subprocess.run(["schtasks", "/Run", "/TN", "Sente"], check=True)
    else:
        unit = Path.home() / ".config" / "systemd" / "user" / "sente.service"
        subprocess.run(["systemctl", "--user", "disable", "--now", "sente.service"], capture_output=True, check=False)
        if action == "uninstall":
            unit.unlink(missing_ok=True)
            subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
            return 0
        unit.parent.mkdir(parents=True, exist_ok=True)
        unit.write_text(
            "[Unit]\nDescription=Sente 自建版\nAfter=network-online.target\n\n"
            f"[Service]\nEnvironment=SENTE_HOME={HOME}\nExecStart={subprocess.list2cmdline(command)}\n"
            "Restart=always\nRestartSec=5\n\n[Install]\nWantedBy=default.target\n", encoding="utf-8")
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "--user", "enable", "--now", "sente.service"], check=True)
    return status(open_browser=True)


def status(*, open_browser: bool = False) -> int:
    config = json.loads(CONFIG.read_text(encoding="utf-8")) if CONFIG.exists() else {"port": 8787}
    url = f"http://127.0.0.1:{config['port']}/"
    for _ in range(60 if open_browser else 1):
        try:
            with urllib.request.urlopen(url + "healthz", timeout=2) as response:
                health = json.load(response)
            print(f"Sente 正在运行：{url}（实盘 {health.get('liveTrading')}）")
            if open_browser:
                webbrowser.open(url)
            return 0
        except OSError:
            time.sleep(2)
    print(f"Sente 没有响应：{url}。日志在 {LOGS}")
    return 1


def main(argv: list[str]) -> int:
    if argv[:1] == ["setup"]:
        return setup()
    if argv[:1] == ["serve"]:
        return serve()
    if argv[:1] == ["status"]:
        return status()
    if argv[:1] == ["service"] and argv[1:2] in (["install"], ["uninstall"]):
        return service(argv[1])
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
