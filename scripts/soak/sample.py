"""浸泡测试资源采样器：每 INTERVAL 秒往 JSONL 追加一行（只用标准库，在浸泡宿主上跑）。

    python3 scripts/soak/sample.py --project ai-trader-soak --profile sente-linux-trial \
        --out results/<ts>/samples.jsonl [--interval 60] [--watch-pid <locust pid>] [--once]

docker 命令继承调用者的 DOCKER_HOST / DOCKER_CONFIG（soak.sh 指向浸泡 VM 的 socket，从不改默认 context）。
每行：ts、各容器 docker stats（CPU%、内存 MiB、PIDs）、runtime/client 容器内 /proc/net/tcp(6) 的
TCP 状态计数（容器根只读、没有 ss）、runtime PID 1（uvicorn）的 fd 数/RSS/线程数、/data（SQLite 卷）
大小与文件明细、VM 的 free -m 与 loadavg、宿主 memory_pressure 与 locust 进程 RSS。
--watch-pid：该进程退出后再采 1 次就结束。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any

TCP_STATES = {"01": "ESTABLISHED", "02": "SYN_SENT", "03": "SYN_RECV", "04": "FIN_WAIT1", "05": "FIN_WAIT2",
              "06": "TIME_WAIT", "07": "CLOSE", "08": "CLOSE_WAIT", "09": "LAST_ACK", "0A": "LISTEN", "0B": "CLOSING"}
UNITS = {"B": 1 / 2**20, "KIB": 1 / 1024, "KB": 1 / 1024, "MIB": 1, "MB": 1, "GIB": 1024, "GB": 1024}


def run(args: list[str], timeout: float = 30) -> str:
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def mib(text: str) -> float | None:
    match = re.match(r"\s*([\d.]+)\s*([A-Za-z]+)", text or "")
    if not match:
        return None
    return round(float(match.group(1)) * UNITS.get(match.group(2).upper(), 0), 2)


def containers(project: str) -> dict[str, str]:
    """service -> container name for the compose project."""
    names: dict[str, str] = {}
    for line in run(["docker", "ps", "--filter", f"label=com.docker.compose.project={project}",
                     "--format", '{{.Names}}\t{{.Label "com.docker.compose.service"}}']).splitlines():
        name, _, service = line.partition("\t")
        if service:
            names[service] = name
    return names


def docker_stats(names: dict[str, str]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    if not names:
        return result
    by_name = {name: service for service, name in names.items()}
    for line in run(["docker", "stats", "--no-stream", "--format", "{{json .}}", *names.values()]).splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        service = by_name.get(row.get("Name"))
        if not service:
            continue
        usage, _, limit = (row.get("MemUsage") or "").partition("/")
        result[service] = {"cpu": float((row.get("CPUPerc") or "0%").rstrip("%") or 0), "memMiB": mib(usage),
                           "memLimitMiB": mib(limit), "pids": int(row.get("PIDs") or 0),
                           "netIO": row.get("NetIO"), "blockIO": row.get("BlockIO")}
    return result


def inside(container: str, with_process: bool) -> dict[str, Any]:
    script = "cat /proc/net/tcp /proc/net/tcp6 2>/dev/null; echo @@@"
    if with_process:
        script += ("; ls /proc/1/fd | wc -l; echo @@@; grep -E '^(VmRSS|Threads):' /proc/1/status; echo @@@;"
                   " du -sk /data 2>/dev/null; ls -l /data 2>/dev/null")
    parts = run(["docker", "exec", container, "sh", "-c", script]).split("@@@")
    states: dict[str, int] = {}
    for line in parts[0].splitlines():
        fields = line.split()
        if len(fields) > 3 and fields[0].endswith(":") and fields[0] != "sl":
            name = TCP_STATES.get(fields[3].upper(), fields[3])
            states[name] = states.get(name, 0) + 1
    data: dict[str, Any] = {"tcp": states}
    if with_process and len(parts) >= 4:
        data["fd"] = int(parts[1].strip() or 0) if parts[1].strip().isdigit() else None
        status = dict(re.findall(r"^(\w+):\s+(\d+)", parts[2], re.MULTILINE))
        data["rssMiB"] = round(int(status.get("VmRSS", 0)) / 1024, 1)
        data["threads"] = int(status.get("Threads", 0))
        lines = parts[3].strip().splitlines()
        if lines and lines[0].split()[0].isdigit():
            data["dataKiB"] = int(lines[0].split()[0])
        data["dataFiles"] = {fields[-1]: int(fields[4]) for fields in (line.split() for line in lines[1:])
                             if len(fields) >= 9 and fields[4].isdigit()}
    return data


def vm(profile: str) -> dict[str, Any]:
    text = run(["colima", "ssh", "-p", profile, "--", "sh", "-c", "free -m; cat /proc/loadavg"])
    data: dict[str, Any] = {}
    for line in text.splitlines():
        fields = line.split()
        if fields[:1] == ["Mem:"] and len(fields) >= 7:
            data.update(totalMiB=int(fields[1]), usedMiB=int(fields[2]), availableMiB=int(fields[6]))
        elif fields[:1] == ["Swap:"] and len(fields) >= 3:
            data["swapUsedMiB"] = int(fields[2])
        elif len(fields) == 5 and "/" in fields[3]:
            data["load"] = [float(value) for value in fields[:3]]
    return data


def host(watch_pid: int | None) -> dict[str, Any]:
    data: dict[str, Any] = {}
    match = re.search(r"free percentage:\s*(\d+)%", run(["memory_pressure", "-Q"]) or run(["memory_pressure"]))
    if match:
        data["memFreePercent"] = int(match.group(1))
    load = run(["sysctl", "-n", "vm.loadavg"]).strip("{} \n").split()
    if len(load) >= 3:
        data["load"] = [float(value) for value in load[:3]]
    if watch_pid:
        rss = run(["ps", "-o", "rss=", "-p", str(watch_pid)]).strip()
        data["locustRssMiB"] = round(int(rss) / 1024, 1) if rss.isdigit() else None
    data["colima"] = {fields[0]: fields[1] for fields in (line.split() for line in run(["colima", "list"]).splitlines()[1:])
                      if len(fields) >= 2}
    return data


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def sample(args: argparse.Namespace) -> dict[str, Any]:
    names = containers(args.project)
    row: dict[str, Any] = {"ts": round(time.time(), 1), "containers": docker_stats(names)}
    for service, name in names.items():
        row.setdefault("proc", {})[service] = inside(name, with_process=service == "trader-runtime")
    row["vm"] = vm(args.profile)
    row["host"] = host(args.watch_pid)
    return row


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", default="ai-trader-soak")
    parser.add_argument("--profile", default="sente-linux-trial")
    parser.add_argument("--out", required=True)
    parser.add_argument("--interval", type=float, default=60)
    parser.add_argument("--watch-pid", type=int)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    while True:
        started = time.time()
        row = sample(args)
        with out.open("a") as handle:
            handle.write(json.dumps(row, separators=(",", ":")) + "\n")
        if args.once or (args.watch_pid and not alive(args.watch_pid)):
            return
        time.sleep(max(1.0, args.interval - (time.time() - started)))


if __name__ == "__main__":
    main()
