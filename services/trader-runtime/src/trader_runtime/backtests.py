from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

from .backtest_coordinator import BacktestCoordinator, BacktestLease
from .execution_boundaries import LocalExecutionBoundary, TenantExecutionBoundary
from .release_manager import StrategyReleaseManager
from .store import RuntimeStore, utc_now

ENGINE_VERSION = "ai-trader-deterministic-threshold-v1"


def _limit_worker_resources() -> None:
    if os.name != "posix":
        return
    import resource

    resource.setrlimit(resource.RLIMIT_CPU, (10, 10))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    if sys.platform == "linux":
        memory = 512 * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (memory, memory))


class BacktestService:
    """Runs deterministic, execution-free backtests from immutable inputs."""

    def __init__(
        self, *, store: RuntimeStore, releases: StrategyReleaseManager,
        engineering_root: Path, state_root: Path,
        execution_boundary: TenantExecutionBoundary | None = None,
        coordinator: BacktestCoordinator | None = None,
        artifact_namespace: str | None = None,
        worker_path: Path | None = None,
    ) -> None:
        self.store = store
        self.releases = releases
        self.engineering_root = engineering_root.resolve()
        self.root = state_root.resolve() / "backtests"
        if artifact_namespace:
            self.root /= artifact_namespace
        self.root.mkdir(parents=True, exist_ok=True)
        self.candidates_root = self.root / "candidates"
        self.candidates_root.mkdir(parents=True, exist_ok=True)
        self.execution_boundary = execution_boundary or LocalExecutionBoundary()
        self.coordinator = coordinator or BacktestCoordinator()
        self.worker_path = (worker_path or Path(__file__).with_name("backtest_worker.py")).resolve()
        if self.engineering_root == state_root.resolve() or self.engineering_root in state_root.resolve().parents:
            raise ValueError("backtest_state_must_be_outside_engineering_root")

    def _allowed_file(self, relative_path: str) -> Path:
        path = (self.engineering_root / relative_path).resolve()
        if self.engineering_root not in path.parents or not path.is_file():
            raise ValueError("backtest_input_not_allowed")
        return path

    def _engine_source(self) -> tuple[bytes, str]:
        source = Path(sys.executable) if getattr(sys, "frozen", False) else self.worker_path
        content = source.read_bytes()
        return content, hashlib.sha256(content).hexdigest()

    @staticmethod
    def _result_bytes(result: dict[str, Any]) -> bytes:
        try:
            return (
                json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
            ).encode()
        except ValueError as error:
            raise RuntimeError("backtest_result_non_finite") from error

    @staticmethod
    def _canonical_bytes(value: dict[str, Any]) -> bytes:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()

    @staticmethod
    def _loads_strict(content: bytes | str) -> Any:
        def reject_constant(value: str) -> None:
            raise ValueError(f"non_finite:{value}")

        return json.loads(content, parse_constant=reject_constant)

    def run(
        self, *, session_id: str, user_id: str,
        strategy_path: str = "workspace/strategies/rb_trend.py",
        data_path: str = "workspace/backtests/fixtures/rb_monthly_v1.csv",
        baseline_threshold: str = "1.00", candidate_threshold: str = "0.90",
        request_id: str | None = None,
    ) -> dict[str, Any]:
        result: dict[str, Any] | None = None
        try:
            with self.coordinator.lease(user_id=user_id, request_id=request_id) as lease:
                result = self._run_with_lease(
                    session_id=session_id,
                    user_id=user_id,
                    strategy_path=strategy_path,
                    data_path=data_path,
                    baseline_threshold=baseline_threshold,
                    candidate_threshold=candidate_threshold,
                    lease=lease,
                )
        except RuntimeError as error:
            if result is not None and str(error) in {"backtest_cancelled", "backtest_timeout"}:
                self.store.discard_backtest_run(result["backtestId"], user_id=user_id)
            raise
        if result is None:
            raise RuntimeError("backtest_result_missing")
        return result

    def _run_with_lease(
        self, *, session_id: str, user_id: str, strategy_path: str,
        data_path: str, baseline_threshold: str, candidate_threshold: str,
        lease: BacktestLease,
    ) -> dict[str, Any]:
        dataset = self._allowed_file(data_path)
        release = self.releases.ensure_release(strategy_path=strategy_path)
        release_id = str(release["releaseId"])
        strategy_digest = str(release["artifactDigest"])
        strategy_artifact = self.releases.release_artifact_path(release_id)
        dataset_bytes = dataset.read_bytes()
        data_digest = hashlib.sha256(dataset_bytes).hexdigest()
        engine_bytes, engine_digest = self._engine_source()
        self.execution_boundary.authorize(
            strategy_path=strategy_path, strategy_digest=strategy_digest,
            data_path=data_path, data_digest=data_digest,
        )
        parameters = {
            "baselineThreshold": baseline_threshold,
            "candidateThreshold": candidate_threshold,
        }
        candidate_config = {
            "schemaVersion": 1,
            "baseStrategyReleaseId": release_id,
            "baseStrategyDigest": strategy_digest,
            "candidateParameters": parameters,
            "executionEnabled": False,
            "orderCapabilities": [],
        }
        candidate_config_bytes = self._canonical_bytes(candidate_config)
        candidate_config_digest = hashlib.sha256(candidate_config_bytes).hexdigest()
        data_source = f"synthetic:{data_path}"
        identity = json.dumps({
            "engineVersion": ENGINE_VERSION,
            "engineDigest": engine_digest,
            "strategySource": strategy_path,
            "strategyDigest": strategy_digest,
            "dataSource": data_source,
            "dataDigest": data_digest,
            "parameters": parameters,
            "candidateConfigDigest": candidate_config_digest,
        }, sort_keys=True, separators=(",", ":")).encode()
        artifact_digest = hashlib.sha256(identity).hexdigest()
        artifact_dir = self.root / f"sha256-{artifact_digest}"
        manifest_path = artifact_dir / "manifest.json"
        result_path = artifact_dir / "result.json"
        if not artifact_dir.exists():
            temporary = Path(tempfile.mkdtemp(prefix="backtest-", dir=self.root))
            try:
                immutable_data = temporary / "dataset.csv"
                immutable_data.write_bytes(dataset_bytes)
                (temporary / "candidate.json").write_bytes(candidate_config_bytes)
                immutable_worker: Path | None = None
                if not getattr(sys, "frozen", False):
                    immutable_worker = temporary / "worker.py"
                    immutable_worker.write_bytes(engine_bytes)
                result = self._execute(
                    strategy_path=strategy_artifact,
                    data_path=immutable_data,
                    worker_path=immutable_worker,
                    parameters=parameters,
                    lease=lease,
                )
                result_bytes = self._result_bytes(result)
                manifest = self._manifest(
                    artifact_digest=artifact_digest,
                    strategy_digest=strategy_digest,
                    data_digest=data_digest,
                    engine_digest=engine_digest,
                    release_id=release_id,
                    strategy_source=strategy_path,
                    data_source=data_source,
                    parameters=parameters,
                    candidate_config_digest=candidate_config_digest,
                    result_bytes=result_bytes,
                )
                (temporary / "result.json").write_bytes(result_bytes)
                (temporary / "manifest.json").write_text(
                    json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
                )
                for path in temporary.iterdir():
                    path.chmod(0o444)
                try:
                    os.replace(temporary, artifact_dir)
                except OSError:
                    if not artifact_dir.is_dir():
                        raise
                    shutil.rmtree(temporary, ignore_errors=True)
                artifact_dir.chmod(0o555)
            except Exception:
                shutil.rmtree(temporary, ignore_errors=True)
                raise
        manifest_bytes = manifest_path.read_bytes()
        result_bytes = result_path.read_bytes()
        immutable_dataset_bytes = (artifact_dir / "dataset.csv").read_bytes()
        immutable_candidate_path = artifact_dir / "candidate.json"
        immutable_worker_path = artifact_dir / "worker.py"
        try:
            manifest = self._loads_strict(manifest_bytes)
            result = self._loads_strict(result_bytes)
            if not isinstance(manifest, dict):
                raise RuntimeError("backtest_manifest_invalid")
            self._validate_finite_result(result)
        except (TypeError, ValueError, json.JSONDecodeError, RuntimeError) as error:
            raise RuntimeError("backtest_artifact_integrity_failed") from error
        if (
            manifest.get("artifactDigest") != artifact_digest
            or manifest.get("engineVersion") != ENGINE_VERSION
            or manifest.get("strategySource") != strategy_path
            or manifest.get("strategyDigest") != strategy_digest
            or manifest.get("strategyReleaseId") != release_id
            or manifest.get("dataSource") != data_source
            or manifest.get("dataDigest") != data_digest
            or manifest.get("engineDigest") != engine_digest
            or manifest.get("parameters") != parameters
            or manifest.get("candidateConfigDigest") != candidate_config_digest
            or hashlib.sha256(immutable_dataset_bytes).hexdigest() != data_digest
            or not immutable_candidate_path.is_file()
            or hashlib.sha256(immutable_candidate_path.read_bytes()).hexdigest()
            != candidate_config_digest
            or immutable_candidate_path.read_bytes() != candidate_config_bytes
            or (
                not getattr(sys, "frozen", False)
                and (
                    not immutable_worker_path.is_file()
                    or hashlib.sha256(immutable_worker_path.read_bytes()).hexdigest()
                    != engine_digest
                )
            )
            or manifest.get("resultDigest") != hashlib.sha256(result_bytes).hexdigest()
        ):
            raise RuntimeError("backtest_artifact_integrity_failed")
        backtest_id = str(uuid4())
        candidate_binding = {
            **candidate_config,
            "backtestId": backtest_id,
            "backtestArtifactDigest": artifact_digest,
            "candidateConfigDigest": candidate_config_digest,
            "engineDigest": engine_digest,
            "dataDigest": data_digest,
        }
        candidate_variant_id = self._write_candidate_binding(candidate_binding)
        persisted_manifest = {
            **manifest,
            "backtestId": backtest_id,
            "candidateVariantId": candidate_variant_id,
            "candidateConfigDigest": candidate_config_digest,
            "candidateParameters": candidate_config["candidateParameters"],
        }
        lease.check()
        self.store.save_backtest_run(
            backtest_id=backtest_id,
            session_id=session_id,
            user_id=user_id,
            artifact_digest=artifact_digest,
            strategy_release_id=release_id,
            data_digest=data_digest,
            parameters=parameters,
            manifest=persisted_manifest,
            artifact_path=str(artifact_dir),
        )
        try:
            lease.check()
        except RuntimeError:
            self.store.discard_backtest_run(backtest_id, user_id=user_id)
            raise
        return self._card(
            backtest_id=backtest_id,
            artifact_digest=artifact_digest,
            strategy_digest=strategy_digest,
            data_digest=data_digest,
            engine_digest=engine_digest,
            release_id=release_id,
            candidate_variant_id=candidate_variant_id,
            candidate_config_digest=candidate_config_digest,
            candidate_parameters=candidate_config["candidateParameters"],
            strategy_path=strategy_path,
            manifest=manifest,
            result=result,
        )

    def _write_candidate_binding(self, candidate: dict[str, Any]) -> str:
        content = self._canonical_bytes(candidate)
        digest = hashlib.sha256(content).hexdigest()
        candidate_variant_id = f"sha256-{digest}"
        target = self.candidates_root / candidate_variant_id
        if not target.exists():
            temporary = Path(tempfile.mkdtemp(prefix="candidate-", dir=self.candidates_root))
            try:
                manifest = temporary / "manifest.json"
                manifest.write_bytes(content)
                manifest.chmod(0o444)
                try:
                    os.replace(temporary, target)
                except OSError:
                    if not target.is_dir():
                        raise
                    shutil.rmtree(temporary, ignore_errors=True)
                target.chmod(0o555)
            except Exception:
                shutil.rmtree(temporary, ignore_errors=True)
                raise
        persisted = (target / "manifest.json").read_bytes()
        if hashlib.sha256(persisted).hexdigest() != digest or persisted != content:
            raise RuntimeError("backtest_candidate_integrity_failed")
        return candidate_variant_id

    def run_tqsdk(
        self, *, session_id: str, user_id: str, symbol: str,
        start_date: str, end_date: str, duration_seconds: int,
        baseline_threshold: str = "1.00", candidate_threshold: str = "0.90",
        initial_balance: float = 1_000_000,
        runner: Callable[[dict[str, Any]], dict[str, Any]],
        request_id: str | None = None,
    ) -> dict[str, Any]:
        card: dict[str, Any] | None = None
        try:
            with self.coordinator.lease(
                user_id=user_id, request_id=request_id, timeout_seconds=130.0,
            ) as lease:
                request = {
                    "symbol": symbol,
                    "startDate": start_date,
                    "endDate": end_date,
                    "durationSeconds": duration_seconds,
                    "baselineThreshold": baseline_threshold,
                    "candidateThreshold": candidate_threshold,
                    "initialBalance": initial_balance,
                }
                release = self.releases.ensure_release(
                    strategy_path="workspace/strategies/rb_trend.py"
                )
                lease.check()
                result = runner(request)
                lease.check()
                card = self._persist_tqsdk(
                    session_id=session_id,
                    user_id=user_id,
                    request=request,
                    result=result,
                    release=release,
                    lease=lease,
                )
        except RuntimeError as error:
            # The lease's closing check can still cancel/time out after the row was saved: the
            # caller gets the error, so the saved run must not stay completed (as run() does).
            if card is not None and str(error) in {"backtest_cancelled", "backtest_timeout"}:
                self.store.discard_backtest_run(card["backtestId"], user_id=user_id)
            raise
        if card is None:
            raise RuntimeError("backtest_result_missing")
        return card

    def _persist_tqsdk(
        self, *, session_id: str, user_id: str, request: dict[str, Any],
        result: dict[str, Any], release: dict[str, Any], lease: BacktestLease,
    ) -> dict[str, Any]:
        self._validate_finite_result(result)
        bars = result.get("bars")
        data_digest = result.get("dataDigest")
        engine_digest = result.get("engineDigest")
        engine_version = result.get("engineVersion")
        contract_multiplier = result.get("contractMultiplier")
        price_tick = result.get("priceTick")
        if (
            not isinstance(bars, list)
            or not bars
            or not isinstance(data_digest, str)
            or len(data_digest) != 64
            or not isinstance(engine_digest, str)
            or len(engine_digest) != 64
            or not isinstance(engine_version, str)
            or not engine_version.startswith("tqsdk-3.10.2-")
            or result.get("accountType") != "TqSim"
            or result.get("executionEnabled") is not False
            or result.get("orderCapabilities") != []
            or not isinstance(contract_multiplier, (int, float))
            or contract_multiplier <= 0
            or not isinstance(price_tick, (int, float))
            or price_tick <= 0
            or result.get("fillMode") != "ACTIVE"
            or result.get("commissionModel") != "tqsdk_default"
        ):
            raise RuntimeError("tqsdk_backtest_result_invalid")
        bars_bytes = self._canonical_bytes({"bars": bars})
        expected_data_digest = hashlib.sha256(
            json.dumps(bars, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        ).hexdigest()
        if data_digest != expected_data_digest:
            raise RuntimeError("tqsdk_backtest_data_integrity_failed")
        release_id = str(release["releaseId"])
        strategy_digest = str(release["artifactDigest"])
        parameters = {
            "baselineThreshold": str(request["baselineThreshold"]),
            "candidateThreshold": str(request["candidateThreshold"]),
            "symbol": str(request["symbol"]),
            "startDate": str(request["startDate"]),
            "endDate": str(request["endDate"]),
            "durationSeconds": str(request["durationSeconds"]),
            "initialBalance": str(request["initialBalance"]),
            "fillMode": str(result.get("fillMode") or "ACTIVE"),
            "commissionModel": str(result.get("commissionModel") or "tqsdk_default"),
            "contractMultiplier": str(result.get("contractMultiplier") or ""),
            "priceTick": str(result.get("priceTick") or ""),
        }
        candidate_config = {
            "schemaVersion": 1,
            "baseStrategyReleaseId": release_id,
            "baseStrategyDigest": strategy_digest,
            "candidateParameters": {
                "baselineThreshold": parameters["baselineThreshold"],
                "candidateThreshold": parameters["candidateThreshold"],
            },
            "executionEnabled": False,
            "orderCapabilities": [],
            # Hashed into the artifact identity, so it stays although nothing deploys
            # from a backtest any more: dropping it would change every artifact digest.
            "deploymentEligible": False,
        }
        candidate_bytes = self._canonical_bytes(candidate_config)
        candidate_digest = hashlib.sha256(candidate_bytes).hexdigest()
        identity = self._canonical_bytes({
            "engineVersion": engine_version,
            "engineDigest": engine_digest,
            "strategyDigest": strategy_digest,
            "dataDigest": data_digest,
            "parameters": parameters,
            "candidateConfigDigest": candidate_digest,
        })
        artifact_digest = hashlib.sha256(identity).hexdigest()
        artifact_dir = self.root / f"sha256-{artifact_digest}"
        result_bytes = self._result_bytes(result)
        manifest = {
            "schemaVersion": 1,
            "artifactDigest": artifact_digest,
            "engineVersion": engine_version,
            "engineDigest": engine_digest,
            "strategyReleaseId": release_id,
            "strategySource": "workspace/strategies/rb_trend.py",
            "strategyDigest": strategy_digest,
            "dataDigest": data_digest,
            "dataSource": str(result.get("dataSource") or "tqsdk:unknown"),
            "dataPointCount": len(bars),
            "parameters": parameters,
            "candidateConfigDigest": candidate_digest,
            "resultDigest": hashlib.sha256(result_bytes).hexdigest(),
            "barsDigest": hashlib.sha256(bars_bytes).hexdigest(),
            "generatedAt": utc_now(),
            "reproducible": True,
            "sourceKind": "tqsdk_history",
            "accountType": "TqSim",
            "deploymentEligible": False,
            "executionEnabled": False,
            "orderCapabilities": [],
        }
        if not artifact_dir.exists():
            temporary = Path(tempfile.mkdtemp(prefix="tqsdk-backtest-", dir=self.root))
            try:
                (temporary / "request.json").write_bytes(self._result_bytes(request))
                (temporary / "bars.json").write_bytes(bars_bytes)
                (temporary / "candidate.json").write_bytes(candidate_bytes)
                (temporary / "result.json").write_bytes(result_bytes)
                (temporary / "manifest.json").write_bytes(self._result_bytes(manifest))
                for path in temporary.iterdir():
                    path.chmod(0o444)
                os.replace(temporary, artifact_dir)
                artifact_dir.chmod(0o555)
            except Exception:
                shutil.rmtree(temporary, ignore_errors=True)
                raise
        persisted_result = (artifact_dir / "result.json").read_bytes()
        persisted_bars = (artifact_dir / "bars.json").read_bytes()
        persisted_manifest = (artifact_dir / "manifest.json").read_bytes()
        try:
            stored_manifest = self._loads_strict(persisted_manifest)
            stored_result = self._loads_strict(persisted_result)
        except (ValueError, json.JSONDecodeError) as error:
            raise RuntimeError("backtest_artifact_integrity_failed") from error
        if (
            not isinstance(stored_manifest, dict)
            or not isinstance(stored_result, dict)
            or stored_manifest.get("artifactDigest") != artifact_digest
            or stored_manifest.get("engineDigest") != engine_digest
            or stored_manifest.get("strategyDigest") != strategy_digest
            or stored_manifest.get("dataDigest") != data_digest
            or stored_manifest.get("parameters") != parameters
            or stored_manifest.get("candidateConfigDigest") != candidate_digest
            or hashlib.sha256(persisted_result).hexdigest()
            != stored_manifest.get("resultDigest")
            or hashlib.sha256(persisted_bars).hexdigest()
            != stored_manifest.get("barsDigest")
        ):
            raise RuntimeError("backtest_artifact_integrity_failed")
        manifest = stored_manifest
        result = stored_result
        backtest_id = str(uuid4())
        binding = {
            **candidate_config,
            "backtestId": backtest_id,
            "backtestArtifactDigest": artifact_digest,
            "candidateConfigDigest": candidate_digest,
            "engineDigest": engine_digest,
            "dataDigest": data_digest,
        }
        candidate_variant_id = self._write_candidate_binding(binding)
        stored_manifest = {
            **manifest,
            "backtestId": backtest_id,
            "candidateVariantId": candidate_variant_id,
            "candidateParameters": candidate_config["candidateParameters"],
        }
        lease.check()
        self.store.save_backtest_run(
            backtest_id=backtest_id,
            session_id=session_id,
            user_id=user_id,
            artifact_digest=artifact_digest,
            strategy_release_id=release_id,
            data_digest=data_digest,
            parameters=parameters,
            manifest=stored_manifest,
            artifact_path=str(artifact_dir),
        )
        return self._tqsdk_card(
            backtest_id=backtest_id,
            artifact_digest=artifact_digest,
            strategy_digest=strategy_digest,
            data_digest=data_digest,
            engine_digest=engine_digest,
            release_id=release_id,
            candidate_variant_id=candidate_variant_id,
            candidate_config_digest=candidate_digest,
            request=request,
            manifest=manifest,
            result=result,
        )

    @staticmethod
    def _tqsdk_card(
        *, backtest_id: str, artifact_digest: str, strategy_digest: str,
        data_digest: str, engine_digest: str, release_id: str,
        candidate_variant_id: str, candidate_config_digest: str,
        request: dict[str, Any], manifest: dict[str, Any], result: dict[str, Any],
    ) -> dict[str, Any]:
        old, new = result["baseline"], result["candidate"]
        improved = (
            new["returnPercent"] >= old["returnPercent"]
            and new["maxDrawdownPercent"] <= old["maxDrawdownPercent"] + 1
        )
        tone = "positive" if improved else "warning"
        return {
            "backtestId": backtest_id,
            "artifactDigest": artifact_digest,
            "strategyDigest": strategy_digest,
            "dataDigest": data_digest,
            "engineDigest": engine_digest,
            "strategyReleaseId": release_id,
            "candidateVariantId": candidate_variant_id,
            "candidateConfigDigest": candidate_config_digest,
            "candidateParameters": {
                "baselineThreshold": str(request["baselineThreshold"]),
                "candidateThreshold": str(request["candidateThreshold"]),
            },
            "engineVersion": manifest["engineVersion"],
            "dataSource": manifest["dataSource"],
            "generatedAt": manifest["generatedAt"],
            "reproducible": True,
            "sourceKind": "tqsdk_history",
            "dataPointCount": int(result["dataPointCount"]),
            "strategyName": "量价趋势研究",
            "symbol": str(request["symbol"]),
            "oldVersion": BacktestService._threshold_label(str(request["baselineThreshold"])),
            "newVersion": BacktestService._threshold_label(str(request["candidateThreshold"])),
            "period": f"{request['startDate']} 至 {request['endDate']}",
            "metrics": [
                {"label": "累计收益", "before": f"{old['returnPercent']:.1f}%", "after": f"{new['returnPercent']:.1f}%", "tone": tone},
                {"label": "最大回撤", "before": f"{old['maxDrawdownPercent']:.1f}%", "after": f"{new['maxDrawdownPercent']:.1f}%", "tone": tone},
                {"label": "信号次数", "before": str(old["signalCount"]), "after": str(new["signalCount"]), "tone": "neutral"},
                {"label": "成交次数", "before": str(old.get("tradeCount", 0)), "after": str(new.get("tradeCount", 0)), "tone": "neutral"},
                {"label": "胜率", "before": f"{old['winRatePercent']:.1f}%", "after": f"{new['winRatePercent']:.1f}%", "tone": tone},
            ],
            "oldCurve": old["curve"],
            "newCurve": new["curve"],
            "conclusion": "候选参数优于基线" if improved else "候选参数没有优于基线",
            "recommendation": "官方 TqSdk 历史回放，仅供研究；不会部署策略或提交账户委托。",
        }

    def tqsdk_history_artifact(
        self, *, backtest_id: str, user_id: str,
    ) -> dict[str, Any]:
        record = self.store.get_backtest_run(backtest_id, user_id=user_id)
        manifest = record["manifest"]
        artifact_dir = Path(record["artifactPath"]).resolve()
        bars_path = artifact_dir / "bars.json"
        if (
            manifest.get("sourceKind") != "tqsdk_history"
            or self.root not in artifact_dir.parents
            or artifact_dir.name != f"sha256-{record['artifactDigest']}"
            or not bars_path.is_file()
            or hashlib.sha256(bars_path.read_bytes()).hexdigest() != manifest.get("barsDigest")
        ):
            raise RuntimeError("backtest_history_artifact_invalid")
        payload = self._loads_strict(bars_path.read_bytes())
        bars = payload.get("bars") if isinstance(payload, dict) else None
        if not isinstance(bars, list) or not bars:
            raise RuntimeError("backtest_history_artifact_invalid")
        return {
            "bars": bars,
            "dataSource": manifest["dataSource"],
            "dataDigest": record["dataDigest"],
            "priceTick": float(record["parameters"]["priceTick"]),
            "contractMultiplier": float(record["parameters"]["contractMultiplier"]),
            "barCount": len(bars),
        }

    def _execute(
        self, *, strategy_path: Path, data_path: Path, worker_path: Path | None,
        parameters: dict[str, str], lease: BacktestLease,
    ) -> dict[str, Any]:
        config = {
            "strategyPath": str(strategy_path),
            "dataPath": str(data_path),
            **parameters,
        }
        command = (
            [sys.executable, "--backtest-worker"]
            if getattr(sys, "frozen", False)
            else [sys.executable, "-I", str(worker_path)]
        )
        environment = {
            "PATH": os.environ.get("PATH", ""),
            "PYTHONHASHSEED": "0",
            "LANG": "C.UTF-8",
        }
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(data_path.parent),
            env=environment,
            preexec_fn=_limit_worker_resources if os.name == "posix" else None,
        )
        payload: str | None = json.dumps(config, separators=(",", ":")) + "\n"
        try:
            while True:
                lease.check()
                try:
                    stdout, _ = process.communicate(input=payload, timeout=0.05)
                    break
                except subprocess.TimeoutExpired:
                    payload = None
        except Exception:
            process.kill()
            process.communicate()
            raise
        if process.returncode != 0:
            raise RuntimeError("backtest_worker_failed")
        try:
            result = self._loads_strict(stdout)
        except (ValueError, json.JSONDecodeError) as error:
            raise RuntimeError("backtest_result_invalid") from error
        if not isinstance(result, dict) or set(result) != {"baseline", "candidate"}:
            raise RuntimeError("backtest_result_invalid")
        self._validate_finite_result(result)
        return result

    @staticmethod
    def _validate_finite_result(result: dict[str, Any]) -> None:
        import math

        for variant in ("baseline", "candidate"):
            metrics = result.get(variant)
            if not isinstance(metrics, dict):
                raise RuntimeError("backtest_result_invalid")
            for field in ("returnPercent", "maxDrawdownPercent", "winRatePercent"):
                value = metrics.get(field)
                if not isinstance(value, (int, float)) or not math.isfinite(value):
                    raise RuntimeError("backtest_result_non_finite")
            if not isinstance(metrics.get("signalCount"), int) or metrics["signalCount"] < 0:
                raise RuntimeError("backtest_result_invalid")
            curve = metrics.get("curve")
            if (
                not isinstance(curve, list)
                or not curve
                or any(
                    not isinstance(value, (int, float)) or not math.isfinite(value)
                    for value in curve
                )
            ):
                raise RuntimeError("backtest_result_non_finite")

    @staticmethod
    def _manifest(
        *, artifact_digest: str, strategy_digest: str, data_digest: str,
        engine_digest: str, release_id: str, data_source: str,
        strategy_source: str, parameters: dict[str, str],
        candidate_config_digest: str, result_bytes: bytes,
    ) -> dict[str, Any]:
        return {
            "schemaVersion": 1,
            "artifactDigest": artifact_digest,
            "engineVersion": ENGINE_VERSION,
            "engineDigest": engine_digest,
            "strategyReleaseId": release_id,
            "strategySource": strategy_source,
            "strategyDigest": strategy_digest,
            "dataDigest": data_digest,
            "dataSource": data_source,
            "parameters": parameters,
            "candidateConfigDigest": candidate_config_digest,
            "resultDigest": hashlib.sha256(result_bytes).hexdigest(),
            "generatedAt": utc_now(),
            "reproducible": True,
            "executionEnabled": False,
            "orderCapabilities": [],
        }

    @staticmethod
    def _threshold_label(value: str) -> str:
        percentage = (Decimal(value) * 100).normalize()
        return f"阈值 {format(percentage, 'f')}%"

    @staticmethod
    def _card(
        *, backtest_id: str, artifact_digest: str, strategy_digest: str,
        data_digest: str, engine_digest: str, release_id: str,
        strategy_path: str, manifest: dict[str, Any], result: dict[str, Any],
        candidate_variant_id: str, candidate_config_digest: str,
        candidate_parameters: dict[str, str],
    ) -> dict[str, Any]:
        old, new = result["baseline"], result["candidate"]
        parameters = manifest["parameters"]
        strategy_ref = Path(strategy_path).stem
        data_ref = str(manifest["dataSource"]).removeprefix("synthetic:")
        improved = (
            new["returnPercent"] >= old["returnPercent"]
            and new["maxDrawdownPercent"] <= old["maxDrawdownPercent"] + 1
        )
        tone = "positive" if improved else "warning"
        return {
            "backtestId": backtest_id,
            "artifactDigest": artifact_digest,
            "strategyDigest": strategy_digest,
            "dataDigest": data_digest,
            "engineDigest": engine_digest,
            "strategyReleaseId": release_id,
            "candidateVariantId": candidate_variant_id,
            "candidateConfigDigest": candidate_config_digest,
            "candidateParameters": candidate_parameters,
            "engineVersion": ENGINE_VERSION,
            "dataSource": manifest["dataSource"],
            "generatedAt": manifest["generatedAt"],
            "reproducible": True,
            "strategyName": "螺纹钢趋势策略" if strategy_ref == "rb_trend" else f"策略 {strategy_ref}",
            "symbol": "RB" if strategy_ref == "rb_trend" else strategy_ref.split("_", 1)[0].upper(),
            "oldVersion": BacktestService._threshold_label(str(parameters["baselineThreshold"])),
            "newVersion": BacktestService._threshold_label(str(parameters["candidateThreshold"])),
            "period": f"数据集 · {Path(data_ref).name}",
            "metrics": [
                {"label": "累计收益", "before": f"{old['returnPercent']:.1f}%", "after": f"{new['returnPercent']:.1f}%", "tone": tone},
                {"label": "最大回撤", "before": f"{old['maxDrawdownPercent']:.1f}%", "after": f"{new['maxDrawdownPercent']:.1f}%", "tone": tone},
                {"label": "信号次数", "before": str(old["signalCount"]), "after": str(new["signalCount"]), "tone": "neutral"},
                {"label": "胜率", "before": f"{old['winRatePercent']:.1f}%", "after": f"{new['winRatePercent']:.1f}%", "tone": tone},
            ],
            "oldCurve": old["curve"],
            "newCurve": new["curve"],
            "conclusion": "候选参数优于基线" if improved else "候选参数没有优于基线",
            "recommendation": "合成回测，仅供研究；该结果不是实盘收益承诺。",
        }
