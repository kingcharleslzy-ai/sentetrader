"""浸泡测试报告：读一个结果目录，写出 report.md 与 report.json（只用标准库）。

    python3 scripts/soak/report.py results/<ts> [--warmup-minutes 30]

读：locust_stats.csv / locust_failures.csv（Locust --csv 前缀 locust）、minutes.jsonl（locustfile 的逐分钟直方图
与计数器）、samples.jsonl（sample.py）、sse.json、meta.json（soak.sh 写的运行参数）。
出：每接口请求数、错误率、p50/p95/p99；SSE 连接/断线/轮换次数与首帧时延；各容器内存首末值、峰值与
预热后线性回归斜率（MB/小时，斜率显著 >0 且后段均值高于前段 → 疑似泄漏）；TCP 连接/fd 是否单调累积；
每项资源的证据状态分开写：无观测（none）、预热后样本不足（insufficient）、有效观察（observed，含疑似 suspect）；
总结论只看关键资源，稀疏的 TCP 状态不参与；没有观测、关键资源缺失或样本不足时不写“未见累积”；
北京时间分时段（日盘 09:00–15:00、夜盘 21:00–01:00、换日 00:00 与 20:00 前后 10 分钟、其余）的时延与错误对比。
"""
from __future__ import annotations

import argparse
import csv
import json
import math
from collections import defaultdict
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

BEIJING = timezone(timedelta(hours=8))
BUCKET_BOUNDS = (10, 25, 50, 75, 100, 150, 200, 300, 500, 750, 1000, 1500, 2000, 3000, 5000, 8000,
                 13000, 20000, 30000, 60000)
MIN_POINTS = 20
# 关键资源：总结论只由它们决定（runtime 进程的 RSS/fd/线程，加至少一个容器内存）。其余指标（TCP 状态、
# VM/宿主）只列表，稀疏时不拖累总结论。
KEY_RESOURCES = ("runtime RSS MiB", "runtime fd", "runtime 线程")
CONTAINER_MEMORY = "容器内存"
SEGMENTS = ("日盘 09:00-15:00", "夜盘 21:00-01:00", "换日 00:00±10m", "交易日切换 20:00±10m", "盘外")


def segment(ts: float) -> str:
    local = datetime.fromtimestamp(ts, BEIJING)
    minutes = local.hour * 60 + local.minute
    if minutes >= 23 * 60 + 50 or minutes < 10:
        return SEGMENTS[2]
    if 19 * 60 + 50 <= minutes < 20 * 60 + 10:
        return SEGMENTS[3]
    if 9 * 60 <= minutes < 15 * 60:
        return SEGMENTS[0]
    if minutes >= 21 * 60 or minutes < 60:
        return SEGMENTS[1]
    return SEGMENTS[4]


def quantile(buckets: list[int], q: float) -> float | None:
    """与 Runtime rum.py 相同：桶内线性插值，最后一个开口桶给下界。"""
    total = sum(buckets)
    if total == 0:
        return None
    target = q * total
    seen = 0
    for index, count in enumerate(buckets):
        if count and seen + count >= target:
            if index >= len(BUCKET_BOUNDS):
                return float(BUCKET_BOUNDS[-1])
            low = 0.0 if index == 0 else float(BUCKET_BOUNDS[index - 1])
            high = float(BUCKET_BOUNDS[index])
            return round(low + (high - low) * (target - seen) / count, 1)
        seen += count
    return float(BUCKET_BOUNDS[-1])


def regression(points: list[tuple[float, float]]) -> dict[str, float] | None:
    """最小二乘斜率（单位/小时）、标准误、t 值与 r²。"""
    n = len(points)
    if n < 3:
        return None
    xs = [x for x, _ in points]
    ys = [y for _, y in points]
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in points)
    slope = sxy / sxx
    residual = sum((y - (my + slope * (x - mx))) ** 2 for x, y in points)
    syy = sum((y - my) ** 2 for y in ys)
    se = math.sqrt(residual / (n - 2) / sxx) if n > 2 and residual > 0 else 0.0
    # 严格线性（残差为 0）时标准误为 0，t 值没有有限值：JSON 里写 null，`perfect` 标明（不写 Infinity）。
    perfect = not se and bool(slope)
    return {"slopePerHour": round(slope * 3600, 3), "stderr": round(se * 3600, 3),
            "t": round(slope / se, 1) if se else (None if perfect else 0.0), "perfect": perfect,
            "r2": round(1 - residual / syy, 3) if syy else 1.0}


def significant(fit: dict[str, Any] | None) -> bool:
    """斜率显著：t>3，或严格线性（t 无有限值）。"""
    return bool(fit and (fit["perfect"] or (fit["t"] is not None and fit["t"] > 3)))


def trend(name: str, points: list[tuple[float, float]], warmup_end: float, *, min_slope: float,
          key: bool = False) -> dict[str, Any]:
    """首末值、峰值、预热后斜率与证据状态。status：none 无观测 / insufficient 预热后样本不足（运行短于预热
    时预热后一个样本都没有，不回退去分析预热段）/ observed 有效观察。斜率显著且 >min_slope、后三分之一均值高于
    前三分之一 → suspect；只有 observed 才下 suspect 结论，其余为 None。"""
    if not points:
        return {"metric": name, "key": key, "status": "none", "samples": 0, "afterWarmup": 0, "suspect": None}
    after = [point for point in points if point[0] >= warmup_end]
    result: dict[str, Any] = {
        "metric": name, "key": key, "samples": len(points), "afterWarmup": len(after),
        "first": points[0][1], "last": points[-1][1], "peak": max(y for _, y in points),
        "earlyMean": None, "lateMean": None, "fit": None, "suspect": None,
        "status": "observed" if len(after) >= MIN_POINTS else "insufficient"}
    if not after:
        return result
    third = max(1, len(after) // 3)
    early = sum(y for _, y in after[:third]) / third
    late = sum(y for _, y in after[-third:]) / third
    fit = regression(after)
    result.update(earlyMean=round(early, 2), lateMean=round(late, 2), fit=fit)
    # 少于 MIN_POINTS 个预热后样本（采样 60 秒一次，约 MIN_POINTS 分钟）不下累积结论。
    if result["status"] == "observed":
        result["suspect"] = bool(significant(fit) and fit["slopePerHour"] > min_slope and late > early)
    return result


def verdict(resources: list[dict[str, Any]], window: dict[str, Any], warmup_minutes: float) -> dict[str, Any]:
    """总结论 {status, text}：证据状态分开说，没有观测 / 关键资源缺失 / 样本不足都不是“未见累积”。"""
    suspects = [item["metric"] for item in resources if item.get("suspect")]
    keys = [item for item in resources if item.get("key")]
    missing = [item["metric"] for item in keys if item["status"] == "none"]
    thin = [item["metric"] for item in keys if item["status"] == "insufficient"]
    ran = (window["end"] - window["start"]) / 60
    if suspects:
        text = "疑似累积：" + "、".join(suspects)
        if missing:
            text += f"（另有关键资源无观测：{'、'.join(missing)}）"
        return {"status": "suspect", "text": text}
    if not any(item["samples"] for item in resources):
        return {"status": "no_observation", "text": "没有任何资源观测（samples.jsonl 缺失或为空），不判断资源累积。"}
    if missing:
        return {"status": "key_missing", "text": f"关键资源无观测：{'、'.join(missing)}，不判断资源累积。"}
    if thin:
        reason = f"运行 {ran:.0f} 分钟，短于预热 {warmup_minutes:g} 分钟" if ran <= warmup_minutes else \
            f"预热后样本不足 {MIN_POINTS} 个"
        return {"status": "insufficient", "text": f"{reason}（{'、'.join(thin)}），不判断资源累积。"}
    return {"status": "none_seen", "text": f"未见资源单调累积（{len(keys)} 项关键资源有效观察，预热后斜率不显著或后段不高于前段）。"}


def read_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text().splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


def locust_table(directory: Path) -> list[dict[str, Any]]:
    path = directory / "locust_stats.csv"
    if not path.exists():
        return []
    rows = []
    with path.open() as handle:
        for row in csv.DictReader(handle):
            requests, failures = int(row["Request Count"]), int(row["Failure Count"])
            rows.append({"type": row["Type"], "name": row["Name"], "requests": requests, "failures": failures,
                         "errorRate": round(failures / requests, 4) if requests else 0.0,
                         "p50": row.get("50%"), "p95": row.get("95%"), "p99": row.get("99%"),
                         "max": round(float(row.get("Max Response Time") or 0), 1), "rps": row.get("Requests/s")})
    return rows


def failures_table(directory: Path) -> list[dict[str, Any]]:
    path = directory / "locust_failures.csv"
    if not path.exists():
        return []
    with path.open() as handle:
        return [{"method": row["Method"], "name": row["Name"], "error": row["Error"][:160],
                 "occurrences": int(row["Occurrences"])} for row in csv.DictReader(handle)]


def build(directory: Path, warmup_minutes: float) -> dict[str, Any]:
    meta = json.loads((directory / "meta.json").read_text()) if (directory / "meta.json").exists() else {}
    minutes = list(read_jsonl(directory / "minutes.jsonl"))
    samples = list(read_jsonl(directory / "samples.jsonl"))
    start = min([row["t"] for row in minutes] + [row["ts"] for row in samples] or [0])
    warmup_end = start + warmup_minutes * 60

    # --- 分时段（HTTP 合并，SSE 单列）
    seg: dict[str, dict[str, Any]] = {name: {"n": 0, "fail": 0, "buckets": [0] * (len(BUCKET_BOUNDS) + 1),
                                             "pushAge": [0] * (len(BUCKET_BOUNDS) + 1), "sseDisconnects": 0,
                                             "minutes": set()} for name in SEGMENTS}
    counters: dict[str, int] = defaultdict(int)
    for row in minutes:
        part = seg[segment(row["t"])]
        part["minutes"].add(row["t"])
        if row["type"] == "COUNTER":
            for key, value in row["counters"].items():
                counters[key] += value
                if key == "sse_disconnect":
                    part["sseDisconnects"] += value
            continue
        if row["type"] == "SSE":
            if row["name"] == "push age":
                part["pushAge"] = [a + b for a, b in zip(part["pushAge"], row["buckets"])]
            continue
        part["n"] += row["n"]
        part["fail"] += row["fail"]
        part["buckets"] = [a + b for a, b in zip(part["buckets"], row["buckets"])]
    segments = []
    for name, part in seg.items():
        if not part["minutes"]:
            continue
        segments.append({"segment": name, "minutes": len(part["minutes"]), "requests": part["n"],
                         "errorRate": round(part["fail"] / part["n"], 4) if part["n"] else 0.0,
                         "p50": quantile(part["buckets"], 0.5), "p95": quantile(part["buckets"], 0.95),
                         "p99": quantile(part["buckets"], 0.99), "pushAgeP95": quantile(part["pushAge"], 0.95),
                         "sseDisconnects": part["sseDisconnects"]})

    # --- 资源趋势
    def series(path: Iterable[str], scale: float = 1.0, *, absent_is_zero: bool = False) -> list[tuple[float, float]]:
        """`absent_is_zero`：TCP 状态表只列出出现过的状态——这一行的状态表本身读到了（采样成功），
        某状态不在其中就是 0；表读不到（采样失败）则这一行不产生点，不能补 0。"""
        keys = tuple(path)
        points = []
        for row in samples:
            value: Any = row
            for key in keys[:-1] if absent_is_zero else keys:
                value = value.get(key) if isinstance(value, dict) else None
            if absent_is_zero:
                value = value.get(keys[-1], 0) if isinstance(value, dict) else None
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                points.append((row["ts"], value * scale))
        return points

    resources = []
    services = sorted({service for row in samples for service in row.get("containers", {})})
    for service in services:
        resources.append(trend(f"{service} 内存 MiB", series(("containers", service, "memMiB")), warmup_end,
                               min_slope=2.0, key=True))
    if not services:   # 没有任何容器内存观测：关键资源缺失要显式列出，不是静默少一行
        resources.append(trend(CONTAINER_MEMORY, [], warmup_end, min_slope=2.0, key=True))
    resources += [
        trend("runtime RSS MiB", series(("proc", "trader-runtime", "rssMiB")), warmup_end, min_slope=2.0, key=True),
        trend("runtime fd", series(("proc", "trader-runtime", "fd")), warmup_end, min_slope=1.0, key=True),
        trend("runtime 线程", series(("proc", "trader-runtime", "threads")), warmup_end, min_slope=0.5, key=True),
        trend("runtime TCP ESTABLISHED", series(("proc", "trader-runtime", "tcp", "ESTABLISHED"), absent_is_zero=True),
              warmup_end, min_slope=1.0),
        trend("runtime TCP CLOSE_WAIT", series(("proc", "trader-runtime", "tcp", "CLOSE_WAIT"), absent_is_zero=True),
              warmup_end, min_slope=0.5),
        trend("client TCP ESTABLISHED", series(("proc", "client", "tcp", "ESTABLISHED"), absent_is_zero=True),
              warmup_end, min_slope=1.0),
        trend("client TCP CLOSE_WAIT", series(("proc", "client", "tcp", "CLOSE_WAIT"), absent_is_zero=True),
              warmup_end, min_slope=0.5),
        trend("SQLite /data MiB", series(("proc", "trader-runtime", "dataKiB"), 1 / 1024), warmup_end, min_slope=50.0),
        trend("VM available MiB", series(("vm", "availableMiB")), warmup_end, min_slope=math.inf),
        trend("宿主 free %", series(("host", "memFreePercent")), warmup_end, min_slope=math.inf),
        trend("locust RSS MiB", series(("host", "locustRssMiB")), warmup_end, min_slope=math.inf),
    ]
    # 关键资源即使没有观测也保留（显示“无观测”）；可选指标没有数据就不列。
    resources = [item for item in resources if item.get("samples") or item.get("key")]
    cpu = {service: round(sum(p[1] for p in series(("containers", service, "cpu"))) / max(1, len(samples)), 1)
           for service in services}
    sse = json.loads((directory / "sse.json").read_text()) if (directory / "sse.json").exists() else {}
    window = {"start": start, "end": max([row["t"] + 60 for row in minutes] + [row["ts"] for row in samples] or [start]),
              "warmupMinutes": warmup_minutes}
    evidence = {name: sum(1 for item in resources if item["status"] == name) for name in ("none", "insufficient", "observed")}
    evidence["suspect"] = sum(1 for item in resources if item.get("suspect"))
    return {"directory": str(directory), "meta": meta, "window": window,
            "endpoints": locust_table(directory), "failures": failures_table(directory),
            "counters": dict(counters), "sse": sse, "segments": segments, "resources": resources,
            "cpuMeanPercent": cpu, "suspects": [item["metric"] for item in resources if item.get("suspect")],
            "evidence": evidence, "verdict": verdict(resources, window, warmup_minutes)}


def fmt(value: Any) -> str:
    if value is None or value == "":
        return "-"
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def markdown(report: dict[str, Any]) -> str:
    window = report["window"]
    start = datetime.fromtimestamp(window["start"], BEIJING).strftime("%Y-%m-%d %H:%M")
    end = datetime.fromtimestamp(window["end"], BEIJING).strftime("%Y-%m-%d %H:%M")
    hours = (window["end"] - window["start"]) / 3600
    meta = report["meta"]
    lines = [f"# 浸泡测试报告 {start} → {end}（北京时间，{hours:.1f} 小时）", ""]
    if meta:
        lines.append("运行参数：" + "，".join(f"{key}={meta[key]}" for key in sorted(meta) if not isinstance(meta[key], dict)))
        lines.append("")
    lines.append("**结论**：" + report["verdict"]["text"])
    evidence = report["evidence"]
    lines.append(f"资源证据：有效观察 {evidence['observed']} 项（其中疑似累积 {evidence['suspect']}），"
                 f"预热后样本不足 {evidence['insufficient']} 项，无观测 {evidence['none']} 项。")
    total = sum(row["requests"] for row in report["endpoints"] if row["name"] == "Aggregated")
    failed = sum(row["failures"] for row in report["endpoints"] if row["name"] == "Aggregated")
    if total:
        lines.append(f"总请求 {total}，失败 {failed}（{failed / total:.3%}）。")
    lines += ["", "## 接口", "", "| 类型 | 名称 | 请求 | 失败 | 错误率 | p50 | p95 | p99 | max |", "|---|---|---:|---:|---:|---:|---:|---:|---:|"]
    for row in report["endpoints"]:
        lines.append(f"| {row['type']} | {row['name']} | {row['requests']} | {row['failures']} | {row['errorRate']:.2%} | "
                     f"{fmt(row['p50'])} | {fmt(row['p95'])} | {fmt(row['p99'])} | {fmt(row['max'])} |")
    counters = report["counters"]
    lines += ["", "## SSE", "",
              (f"断线 {counters.get('sse_disconnect', 0)} 次，计划轮换（重新登录后重开）{counters.get('sse_rotate', 0)} 次，"
              f"收到 {sum(v for k, v in counters.items() if k.startswith('frame:'))} 帧 / "
              f"{counters.get('sse_bytes', 0) / 2**20:.1f} MiB，结束时打开的流 {report['sse'].get('openStreams', '-')}。")]
    frames = sorted(((k[6:], v) for k, v in counters.items() if k.startswith("frame:")), key=lambda item: -item[1])
    if frames:
        lines.append("帧分布：" + "，".join(f"{name} {count}" for name, count in frames[:12]))
    orders = {key: value for key, value in counters.items() if key.startswith("order_")}
    if orders:
        lines.append("下单流程：" + "，".join(f"{key} {value}" for key, value in sorted(orders.items())))
    if report["failures"]:
        lines += ["", "## 失败明细", "", "| 方法 | 名称 | 次数 | 错误 |", "|---|---|---:|---|"]
        for row in sorted(report["failures"], key=lambda item: -item["occurrences"])[:20]:
            lines.append(f"| {row['method']} | {row['name']} | {row['occurrences']} | {row['error'].replace('|', '/')} |")
    lines += ["", "## 分时段（HTTP 合并；push age 为 SSE 行情帧服务端时间戳到收到的时延）", "",
              "| 时段 | 分钟 | 请求 | 错误率 | p50 | p95 | p99 | push age p95 | SSE 断线 |", "|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for row in report["segments"]:
        lines.append(f"| {row['segment']} | {row['minutes']} | {row['requests']} | {row['errorRate']:.2%} | {fmt(row['p50'])} | "
                     f"{fmt(row['p95'])} | {fmt(row['p99'])} | {fmt(row['pushAgeP95'])} | {row['sseDisconnects']} |")
    lines += ["", f"## 资源（预热 {window['warmupMinutes']:g} 分钟后回归；斜率单位/小时，t>3 视为显著）", "",
              "| 指标 | 样本 | 首 | 末 | 峰值 | 前段均值 | 后段均值 | 斜率/h | t | r² | 疑似累积 |", "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|"]
    states = {"none": "无观测", "insufficient": "样本不足", "observed": ""}
    for item in report["resources"]:
        fit = item.get("fit") or {}
        t = "∞（严格线性）" if fit.get("perfect") else fmt(fit.get("t"))
        mark = "是" if item["suspect"] else states[item["status"]]
        lines.append(f"| {item['metric']}{' *' if item['key'] else ''} | {item['samples']} | {fmt(item.get('first'))} | "
                     f"{fmt(item.get('last'))} | {fmt(item.get('peak'))} | {fmt(item.get('earlyMean'))} | "
                     f"{fmt(item.get('lateMean'))} | {fmt(fit.get('slopePerHour'))} | {t} | {fmt(fit.get('r2'))} | {mark} |")
    lines += ["", "（* 关键资源，决定总结论；其余指标只列出，样本稀疏不影响总结论。）"]
    lines.append("")
    lines.append("平均 CPU%：" + "，".join(f"{service} {value}" for service, value in report["cpuMeanPercent"].items()))
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("directory")
    parser.add_argument("--warmup-minutes", type=float, default=30)
    args = parser.parse_args()
    directory = Path(args.directory)
    report = build(directory, args.warmup_minutes)
    (directory / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1, default=str, allow_nan=False))
    text = markdown(report)
    (directory / "report.md").write_text(text)
    print(text)


if __name__ == "__main__":
    main()
