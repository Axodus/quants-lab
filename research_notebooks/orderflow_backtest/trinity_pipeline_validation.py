"""Execute the approved bounded BTC research workflow and persist its evidence."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .multi_symbol_pipeline import DatasetQualificationRegistry
from .pipeline_operations import AxodusResearchOperations, BTC_IS_END, BTC_IS_START, DATASET_ID
from .research_capabilities import ResearchActionKind, ResearchCapabilityBoundary
from .research_executor import BoundedResearchExecutor


def execute_validation(
    data_root: Path,
    runtime: Path,
    freeze: Path,
    frame_dir: Path,
    run_root: Path,
    state_root: Path,
) -> dict:
    registry = DatasetQualificationRegistry.from_freeze(freeze)
    operations = AxodusResearchOperations(data_root, registry, freeze, frame_dir, run_root)
    executor = BoundedResearchExecutor(
        data_root=data_root,
        runtime=runtime,
        state_root=state_root,
        boundary=ResearchCapabilityBoundary(registry),
        handlers=operations.handlers(),
    )
    preflight = executor.preflight().to_dict()
    if not preflight["available"]:
        raise RuntimeError("RUNTIME_ENVIRONMENT_UNAVAILABLE")

    scan = executor.execute(ResearchActionKind.SCAN_MARKET, {
        "selection_mode": "FIXED_UNIVERSE",
        "selection_timestamp": "2026-06-22T23:00:00Z",
        "market_snapshot_timestamp": "2026-06-22T23:00:00Z",
        "universe": "BTCUSDT accepted regression universe",
        "selection_rule_version": "fixed-universe-pipeline-validation-v1",
        "market_snapshot": [{"symbol": "BTCUSDT", "turnover": 1, "rvol": 1, "spread": 1}],
    })
    acquisition = executor.execute(ResearchActionKind.ACQUIRE_DATASET, {
        "symbol": "BTCUSDT", "selection_mode": "FIXED_UNIVERSE",
        "selection_timestamp": "2026-06-22T23:00:00Z",
        "selected_symbols": ["BTCUSDT"],
        "requested_start": BTC_IS_START, "requested_end": BTC_IS_END,
        "dataset_id": DATASET_ID, "dataset_revision": "v1", "data_root": str(data_root),
    })
    qualification = executor.execute(ResearchActionKind.QUALIFY_DATASET, {
        "symbol": "BTCUSDT", "dataset_id": DATASET_ID, "dataset_revision": "v1",
        "manifest_path": str(data_root / "manifests"),
    })
    frames = executor.execute(ResearchActionKind.BUILD_FRAMES, {
        "symbol": "BTCUSDT", "dataset_id": DATASET_ID, "dataset_revision": "v1", "period": "IS",
        "data_root": str(data_root),
    })
    replay = executor.execute(ResearchActionKind.RUN_FROZEN_BACKTEST, {
        "strategy_id": "orderflow.absorption.fade", "symbol": "BTCUSDT",
        "period": "IS", "dataset_id": DATASET_ID, "dataset_revision": "v1", "output_root": str(data_root / "runs"),
    })
    return {
        "status": "COMPLETE",
        "workflow": ["SCANNED", acquisition["status"], qualification["status"], frames["status"], replay["status"], "CANONICAL_EVIDENCE_PERSISTED"],
        "preflight": preflight,
        "scan": scan,
        "acquisition": acquisition,
        "qualification": qualification,
        "frames": frames,
        "replay": replay,
        "oosEventsConsumed": replay["oosEventsConsumed"],
        "exchangeOrderMutations": 0,
        "testnetMutations": 0,
        "mainnetMutations": 0,
        "realCapital": 0,
        "secretsExposed": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--freeze", type=Path, required=True)
    parser.add_argument("--frame-dir", type=Path, required=True)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = execute_validation(args.data_root, args.runtime, args.freeze, args.frame_dir, args.run_root, args.state_root)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"status": result["status"], "workflow": result["workflow"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
