"""scripts/soak/report.py：资源证据状态与总结论（#832 审查）。

无观测、预热后样本不足、有效观察、疑似累积分开表达；空集或关键资源缺失不写“未见累积”；稀疏的 TCP
状态不拖累总结论；严格线性增长的 t 值在 JSON 里是 null（不是 Infinity）；运行短于预热报样本不足。
"""
from __future__ import annotations

import importlib.util
import json
import math
import random
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "soak" / "report.py"
START = 1_800_000_000.0


@pytest.fixture(scope="module")
def report_module():
    spec = importlib.util.spec_from_file_location("soak_report", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sample(index: int, *, rss: float = 300.0, fd: int = 120, threads: int = 40, tcp: dict | None = None,
           proc: bool = True) -> dict:
    row: dict = {"ts": START + index * 60, "containers": {"runtime": {"memMiB": rss}}}
    if proc:
        row["proc"] = {"trader-runtime": {"rssMiB": rss, "fd": fd, "threads": threads,
                                          "tcp": tcp if tcp is not None else {"ESTABLISHED": 6}},
                       "client": {"tcp": {"ESTABLISHED": 3}}}
    return row


def write(directory: Path, rows: list[dict] | None, *, raw: str | None = None) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    if raw is not None:
        (directory / "samples.jsonl").write_text(raw)
    elif rows is not None:
        (directory / "samples.jsonl").write_text("\n".join(json.dumps(row) for row in rows))
    return directory


def run(module, directory: Path, warmup: float = 30) -> tuple[dict, str]:
    report = module.build(directory, warmup)
    json.loads(json.dumps(report, allow_nan=False, default=str))   # strict JSON: no Infinity / NaN
    return report, module.markdown(report)


def test_no_observation_is_not_reported_as_no_accumulation(report_module, tmp_path: Path) -> None:
    for name, directory in (("none", write(tmp_path / "none", None)), ("empty", write(tmp_path / "empty", [])),
                            ("garbage", write(tmp_path / "garbage", None, raw="not json\n{broken"))):
        report, text = run(report_module, directory)
        assert report["verdict"]["status"] == "no_observation", name
        assert "未见资源单调累积" not in text, name
        assert report["evidence"]["observed"] == 0 and report["evidence"]["none"] >= 1


def test_missing_key_resources_are_not_reported_as_no_accumulation(report_module, tmp_path: Path) -> None:
    # Containers were sampled for two hours but the runtime process was never read.
    rows = [sample(i, proc=False) for i in range(120)]
    report, text = run(report_module, write(tmp_path / "noproc", rows))
    assert report["verdict"]["status"] == "key_missing"
    assert "runtime RSS MiB" in report["verdict"]["text"] and "未见资源单调累积" not in text
    states = {item["metric"]: item["status"] for item in report["resources"]}
    assert states["runtime RSS MiB"] == "none" and states["runtime 内存 MiB"] == "observed"


def test_a_run_shorter_than_the_warmup_reports_insufficient_samples(report_module, tmp_path: Path) -> None:
    rows = [sample(i, rss=300 + i * 5) for i in range(25)]   # 25 minutes, warmup 30: nothing counts
    report, text = run(report_module, write(tmp_path / "short", rows))
    assert report["verdict"]["status"] == "insufficient" and "短于预热" in report["verdict"]["text"]
    runtime = next(item for item in report["resources"] if item["metric"] == "runtime RSS MiB")
    assert runtime["status"] == "insufficient" and runtime["afterWarmup"] == 0 and runtime["suspect"] is None
    assert report["suspects"] == [] and "未见资源单调累积" not in text


def test_fewer_than_twenty_samples_after_the_warmup_are_insufficient(report_module, tmp_path: Path) -> None:
    rows = [sample(i) for i in range(30 + 19)]   # 19 samples at or after minute 30
    report, _ = run(report_module, write(tmp_path / "thin", rows))
    assert report["verdict"]["status"] == "insufficient"
    rows = [sample(i) for i in range(30 + 20)]   # 20 samples: observed
    report, text = run(report_module, write(tmp_path / "enough", rows))
    assert report["verdict"]["status"] == "none_seen" and "未见资源单调累积" in text
    assert report["evidence"]["suspect"] == 0


def test_sparse_tcp_states_do_not_hold_back_the_overall_verdict(report_module, tmp_path: Path) -> None:
    # CLOSE_WAIT shows up in only a few samples; the state table itself was read every time.
    rows = [sample(i, tcp={"ESTABLISHED": 6, **({"CLOSE_WAIT": 1} if i in {40, 71} else {})}) for i in range(120)]
    report, text = run(report_module, write(tmp_path / "sparse", rows))
    close_wait = next(item for item in report["resources"] if item["metric"] == "runtime TCP CLOSE_WAIT")
    assert close_wait["status"] == "observed" and close_wait["samples"] == 120   # absent state = 0 when the table was read
    assert report["verdict"]["status"] == "none_seen" and "未见资源单调累积" in text
    # A failed read of the table is not a zero: those rows give no point, and the overall verdict ignores them.
    rows = [{**sample(i), "proc": {"trader-runtime": {"rssMiB": 300.0, "fd": 120, "threads": 40}}} for i in range(120)]
    report, _ = run(report_module, write(tmp_path / "notable", rows))
    close_wait = next((item for item in report["resources"] if item["metric"] == "runtime TCP CLOSE_WAIT"), None)
    assert close_wait is None   # no point at all: an optional metric with no data is not listed
    assert report["verdict"]["status"] == "none_seen"


def test_a_known_growth_is_flagged_and_a_strictly_linear_one_writes_null_not_infinity(report_module, tmp_path: Path) -> None:
    rng = random.Random(7)
    noisy = [sample(i, rss=300 + i * 0.5 + rng.uniform(-0.3, 0.3)) for i in range(180)]
    report, text = run(report_module, write(tmp_path / "noisy", noisy))
    assert report["verdict"]["status"] == "suspect" and "runtime RSS MiB" in report["suspects"]
    assert "疑似累积" in text
    linear = [sample(i, rss=300 + i * 0.5) for i in range(180)]   # exactly linear: zero residual
    report, text = run(report_module, write(tmp_path / "linear", linear))
    fit = next(item for item in report["resources"] if item["metric"] == "runtime RSS MiB")["fit"]
    assert fit["t"] is None and fit["perfect"] is True and math.isfinite(fit["slopePerHour"])
    assert "runtime RSS MiB" in report["suspects"]   # strictly linear growth is still significant
    assert "∞" in text   # `run` already dumped the report as strict JSON (allow_nan=False)
