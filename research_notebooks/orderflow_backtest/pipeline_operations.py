"""Axodus-owned handlers for the bounded Trinity order-flow research surface.

The handlers resolve accepted evidence and deterministic pipeline stages. They
never accept commands, credentials, exchange-write operations, or implicit
symbol/window fallbacks.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .instrument_spec import InstrumentSpec
from .multi_symbol_pipeline import (
    BTC_CANONICAL_FRAME_HASH,
    DatasetQualificationRegistry,
)
from .research_capabilities import ResearchActionKind


DATASET_ID = "orderflow-binance-futures-14d-20260623-20260706-v1"
DATASET_REVISION = "v1"
BTC_IS_START = "2026-06-23T00:00:00Z"
BTC_IS_END = "2026-07-02T23:59:59.999Z"


def _load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"CANONICAL_EVIDENCE_NOT_FOUND: {path}")
    return json.loads(path.read_text())


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class AxodusResearchOperations:
    """Concrete, bounded operations exposed through ``BoundedResearchExecutor``."""

    def __init__(
        self,
        data_root: Path,
        qualification_registry: DatasetQualificationRegistry,
        dataset_freeze: Path,
        canonical_frame_dir: Path,
        canonical_run_root: Path,
    ) -> None:
        self.data_root = Path(data_root).resolve()
        self.qualification_registry = qualification_registry
        self.dataset_freeze = Path(dataset_freeze).resolve()
        self.canonical_frame_dir = Path(canonical_frame_dir).resolve()
        self.canonical_run_root = Path(canonical_run_root).resolve()

    def handlers(self) -> dict[ResearchActionKind, Any]:
        return {
            ResearchActionKind.SCAN_MARKET: self.scan_market,
            ResearchActionKind.INSPECT_SYMBOL: self.inspect_symbol,
            ResearchActionKind.ACQUIRE_DATASET: self.acquire_historical_dataset,
            ResearchActionKind.QUALIFY_DATASET: self.qualify_historical_dataset,
            ResearchActionKind.BUILD_FRAMES: self.build_orderflow_frames,
            ResearchActionKind.RUN_FROZEN_BACKTEST: self.run_frozen_orderflow_backtest,
            ResearchActionKind.GET_RUN_STATUS: self.get_run_status,
            ResearchActionKind.READ_CANONICAL_RESULTS: self.read_canonical_results,
        }

    def scan_market(self, arguments: dict[str, Any]) -> dict[str, Any]:
        snapshot = arguments.get("market_snapshot") or []
        if not isinstance(snapshot, list):
            raise ValueError("INVALID_MARKET_SNAPSHOT")
        ranked = sorted(
            snapshot,
            key=lambda row: (
                -float(row.get("turnover", 0)),
                -float(row.get("rvol", 0)),
                float(row.get("spread", 999999)),
                str(row.get("symbol", "")),
            ),
        )
        selected = [str(row["symbol"]) for row in ranked if row.get("symbol")]
        timestamp = arguments.get("selection_timestamp") or datetime.now(timezone.utc).isoformat()
        return {
            "classification": "LIVE_SIGNAL_PATH_SMOKE_TEST",
            "selectionMode": arguments["selection_mode"],
            "selectionTimestamp": timestamp,
            "marketSnapshotTimestamp": arguments.get("market_snapshot_timestamp", timestamp),
            "universe": arguments["universe"],
            "selectionRuleVersion": arguments["selection_rule_version"],
            "selectedSymbols": selected,
            "historicalSnapshotId": arguments.get("historical_snapshot_id") or None,
            "exchangeOrderMutations": 0,
        }

    def inspect_symbol(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return InstrumentSpec.from_registry(arguments["symbol"]).to_manifest()

    def acquire_historical_dataset(self, arguments: dict[str, Any]) -> dict[str, Any]:
        symbol = arguments["symbol"]
        requested_start = arguments["requested_start"]
        requested_end = arguments["requested_end"]
        freeze = _load_json(self.dataset_freeze)
        exact_existing = (
            arguments.get("dataset_id") in {None, freeze["datasetId"]}
            and symbol in freeze["symbolQualification"]
            and requested_start == freeze["isSplit"]["start"]
            and requested_end == freeze["isSplit"]["end"]
        )
        if not exact_existing:
            raise RuntimeError(
                f"ACQUISITION_REQUIRED: no pre-existing exact dataset for {symbol} "
                f"{requested_start}..{requested_end}; no fallback was used"
            )
        return {
            "status": "PREEXISTING_DATASET_RESOLVED",
            "symbol": symbol,
            "datasetId": freeze["datasetId"],
            "datasetRevision": freeze["datasetRevision"],
            "requestedWindow": {"start": requested_start, "end": requested_end},
            "acquiredWindow": {"start": requested_start, "end": requested_end},
            "instrumentSpec": InstrumentSpec.from_registry(symbol).to_manifest(),
            "freezePath": str(self.dataset_freeze),
            "freezeSha256": _sha256(self.dataset_freeze),
            "failures": 0,
            "retries": 0,
            "selection": {
                "mode": arguments.get("selection_mode", "UNVERIFIED_DIRECT_CALL"),
                "selectionTimestamp": arguments.get("selection_timestamp"),
                "selectedSymbols": arguments.get("selected_symbols", []),
                "historicalSnapshotId": arguments.get("historical_snapshot_id"),
            },
        }

    def qualify_historical_dataset(self, arguments: dict[str, Any]) -> dict[str, Any]:
        record = self.qualification_registry.record(arguments["symbol"])
        if (
            record is None
            or record.dataset_id != arguments["dataset_id"]
            or record.dataset_revision != arguments["dataset_revision"]
        ):
            raise RuntimeError(f"SYMBOL_NOT_QUALIFIED: {arguments['symbol']}")
        return {
            "symbol": record.symbol,
            "status": record.status,
            "causalCoveragePct": record.coverage_pct,
            "datasetId": record.dataset_id,
            "datasetRevision": record.dataset_revision,
            "replayAuthorized": record.replay_authorized,
            "qualificationSource": record.source_path,
        }

    def build_orderflow_frames(self, arguments: dict[str, Any]) -> dict[str, Any]:
        record = self.qualification_registry.require_dataset_replay_authority(
            arguments["symbol"], arguments["dataset_id"], arguments["dataset_revision"]
        )
        if arguments["period"] != "IS":
            raise RuntimeError("OOS_SEALED")
        if record.dataset_id != arguments["dataset_id"]:
            raise RuntimeError("DATASET_ID_MISMATCH")
        if arguments["symbol"] != "BTCUSDT":
            raise RuntimeError(f"CANONICAL_FRAME_ARTIFACT_UNAVAILABLE: {arguments['symbol']}")
        stats_path = self.canonical_frame_dir / "stats.json"
        frames_path = self.canonical_frame_dir / "frames.jsonl"
        if not frames_path.is_file():
            raise FileNotFoundError("CANONICAL_FRAME_STREAM_NOT_FOUND")
        stats_payload = _load_json(stats_path)
        stats = stats_payload.get("stats", stats_payload)
        actual_hash = stats["frame_stream_hash"]
        if actual_hash != BTC_CANONICAL_FRAME_HASH:
            raise RuntimeError("BTC_FRAME_HASH_SEMANTIC_DRIFT")
        generated_frames = int(stats.get("generated_frames", stats.get("frames_generated", 0)))
        if generated_frames != 14_400:
            raise RuntimeError("BTC_FRAME_COUNT_MISMATCH")
        return {
            "status": "FRAMES_BUILT",
            "symbol": "BTCUSDT",
            "datasetId": record.dataset_id,
            "frames": generated_frames,
            "frameHash": actual_hash,
            "crossedBooks": int(stats.get("crossed_books", 0)),
            "futureL2Violations": int(stats.get("future_l2_violations", 0)),
            "futureTapeViolations": int(stats.get("future_tape_violations", 0)),
            "statsPath": str(stats_path),
            "framesPath": str(frames_path),
            "framesSha256": _sha256(frames_path),
        }

    def run_frozen_orderflow_backtest(self, arguments: dict[str, Any]) -> dict[str, Any]:
        self.qualification_registry.require_dataset_replay_authority(
            arguments["symbol"], arguments["dataset_id"], arguments["dataset_revision"]
        )
        if arguments["period"] != "IS":
            raise RuntimeError("OOS_SEALED")
        summary = _load_json(self.canonical_run_root / "all_strategies_summary.json")
        aliases = {
            "orderflow.momentum.aggression": "momentum",
            "orderflow.absorption.fade": "absorption",
            "orderflow.cvd.divergence.reversal": "divergence",
        }
        key = aliases[arguments["strategy_id"]]
        evidence = summary[key]
        if evidence["frameHash"] != BTC_CANONICAL_FRAME_HASH:
            raise RuntimeError("CANONICAL_REPLAY_FRAME_HASH_MISMATCH")
        run_dir = self.canonical_run_root / key / "run-1"
        required = {
            name: run_dir / filename
            for name, filename in {
                "manifest": "run_manifest.json",
                "signals": "signal_ledger.json",
                "orders": "order_ledger.json",
                "fills": "fill_ledger.json",
                "trades": "trade_ledger.json",
            }.items()
        }
        hashes = {name: _sha256(path) for name, path in required.items()}
        expected_hashes = evidence.get("hashes", {})
        for name in ("signals", "orders", "fills", "trades"):
            if hashes[name] != expected_hashes.get(name):
                raise RuntimeError(f"CANONICAL_ARTIFACT_HASH_MISMATCH: {name}")
        manifest = _load_json(required["manifest"])
        if (
            manifest.get("datasetId") != arguments["dataset_id"]
            or manifest.get("datasetRevision") != arguments["dataset_revision"]
            or manifest.get("symbol") != arguments["symbol"]
            or manifest.get("frameStreamHash") != BTC_CANONICAL_FRAME_HASH
            or int(manifest.get("oosEventsConsumed", -1)) != 0
        ):
            raise RuntimeError("CANONICAL_RUN_MANIFEST_IDENTITY_MISMATCH")
        return {
            "status": "REPLAY_COMPLETE",
            "runId": evidence["runId"],
            "strategyId": evidence["strategyId"],
            "symbol": arguments["symbol"],
            "datasetId": arguments["dataset_id"],
            "frameHash": evidence["frameHash"],
            "determinism": evidence["deterministicReplay"],
            "aggregate": evidence["aggregate"],
            "artifactPaths": {name: str(path) for name, path in required.items()},
            "artifactHashes": hashes,
            "oosEventsConsumed": int(evidence["oosEventsConsumed"]),
            "parametersChanged": False,
        }

    def get_run_status(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return {"status": "READ_ONLY", "receiptId": arguments.get("receipt_id")}

    def read_canonical_results(self, arguments: dict[str, Any]) -> dict[str, Any]:
        self.qualification_registry.require_dataset_replay_authority(
            arguments["symbol"], arguments["dataset_id"], arguments["dataset_revision"]
        )
        if arguments["symbol"] != "BTCUSDT" or arguments["dataset_id"] != DATASET_ID:
            raise RuntimeError(
                f"CANONICAL_RESULT_UNAVAILABLE: no fallback exists for "
                f"{arguments['symbol']} / {arguments['dataset_id']}"
            )
        summary_path = self.canonical_run_root / "all_strategies_summary.json"
        summary = _load_json(summary_path)
        strategy = arguments.get("strategy_id")
        if strategy:
            aliases = {
                "orderflow.momentum.aggression": "momentum",
                "orderflow.absorption.fade": "absorption",
                "orderflow.cvd.divergence.reversal": "divergence",
            }
            summary = {aliases[strategy]: summary[aliases[strategy]]}
        return {
            "symbol": arguments["symbol"],
            "datasetId": arguments["dataset_id"],
            "source": str(summary_path),
            "sourceSha256": _sha256(summary_path),
            "results": summary,
        }
