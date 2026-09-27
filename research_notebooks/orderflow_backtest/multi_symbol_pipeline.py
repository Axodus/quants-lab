"""
Multi-symbol causal research pipeline orchestrator.

Enforces:
  - InstrumentSpec resolution before frame building
  - BTC regression hash check as baseline invariant
  - Temporal selection leakage guard
  - Fail-closed on unknown symbol or missing metadata
  - No silent fallback to BTC on new symbol failure
"""
from __future__ import annotations
import hashlib
import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .instrument_spec import InstrumentSpec
from .scanner_selection_manifest import ScannerSelectionManifest, SelectionMode
from .bootstrap_resolver import BootstrapResolution, BootstrapResolver

# Canonical BTC frame-stream hash that must be reproduced by any refactored builder.
BTC_CANONICAL_FRAME_HASH = "fae40ba4190bd13a9a0fb89c3b6f5a8d953de0c8a604ff3c7e5e174a3997ddbf"

PIPELINE_STATE_TRANSITIONS = [
    "NOT_STARTED",
    "SCANNED",
    "ACQUISITION_REQUIRED",
    "ACQUIRING",
    "ACQUIRED",
    "QUALIFICATION_REQUIRED",
    "QUALIFYING",
    "QUALIFIED",
    "REPLAY_REQUIRED",
    "REPLAYING",
    "COMPLETE",
    "BLOCKED",
]


@dataclass(frozen=True)
class DatasetQualificationRecord:
    """Per-symbol replay authority derived from a frozen dataset artifact."""

    symbol: str
    status: str
    coverage_pct: float
    dataset_id: str
    dataset_revision: str
    source_path: str

    @property
    def replay_authorized(self) -> bool:
        return self.status == "QUALIFIED" and self.coverage_pct == 100.0


def _resolve_freeze_artifact(freeze_path: Path, relative_path: Path) -> Path:
    """Resolve a freeze artifact relative to its owning Trading workspace."""
    for ancestor in (freeze_path.parent, *freeze_path.parents):
        candidate = ancestor / relative_path
        if candidate.is_file():
            return candidate
    return freeze_path.parent / relative_path


class DatasetQualificationRegistry:
    """Reads the frozen dataset decision instead of trusting a symbol allowlist."""

    def __init__(self, records: dict[str, DatasetQualificationRecord], source_sha256: str) -> None:
        self._records = records
        self.source_sha256 = source_sha256

    @classmethod
    def from_freeze(cls, freeze_path: Path) -> "DatasetQualificationRegistry":
        source = Path(freeze_path)
        raw = source.read_bytes()
        payload = json.loads(raw)
        if not payload.get("datasetId") or not payload.get("datasetRevision"):
            raise ValueError("DATASET_FREEZE_INCOMPLETE")
        for name, relative_path in payload.get("artifacts", {}).items():
            if name == "rawRoot":
                continue
            candidate = Path(relative_path)
            if not candidate.is_absolute():
                candidate = _resolve_freeze_artifact(source, candidate)
            if not candidate.is_file():
                raise FileNotFoundError(f"DATASET_EVIDENCE_MISSING: {name}: {candidate}")
        records = {
            symbol: DatasetQualificationRecord(
                symbol=symbol,
                status=detail["status"],
                coverage_pct=float(detail["qualifiedCoveragePct"]),
                dataset_id=payload["datasetId"],
                dataset_revision=payload["datasetRevision"],
                source_path=str(source),
            )
            for symbol, detail in payload["symbolQualification"].items()
        }
        return cls(records, hashlib.sha256(raw).hexdigest())

    def record(self, symbol: str) -> DatasetQualificationRecord | None:
        return self._records.get(symbol)

    def require_replay_authority(self, symbol: str) -> DatasetQualificationRecord:
        return self.require_dataset_replay_authority(symbol)

    def require_dataset_replay_authority(
        self, symbol: str, dataset_id: str | None = None, dataset_revision: str | None = None
    ) -> DatasetQualificationRecord:
        record = self.record(symbol)
        if record is None:
            raise ValueError(f"SYMBOL_NOT_QUALIFIED: no dataset qualification record for {symbol}")
        if dataset_id is not None and record.dataset_id != dataset_id:
            raise ValueError(f"DATASET_ID_MISMATCH: {symbol} is qualified only for {record.dataset_id}")
        if dataset_revision is not None and record.dataset_revision != dataset_revision:
            raise ValueError(f"DATASET_REVISION_MISMATCH: {symbol} is qualified only for {record.dataset_revision}")
        if not record.replay_authorized:
            raise ValueError(
                f"SYMBOL_NOT_QUALIFIED: {symbol} is {record.status} at "
                f"{record.coverage_pct}% causal coverage; replay is blocked"
            )
        return record


@dataclass
class SymbolPipelineState:
    symbol: str
    state: str = "NOT_STARTED"
    qualification_status: str = ""  # QUALIFIED / PARTIAL / REJECTED
    acquisition_manifest: dict = field(default_factory=dict)
    frame_count: int = 0
    frame_hash: str = ""
    error: str = ""
    bootstrap_status: str = ""
    bootstrap_resolution: dict = field(default_factory=dict)
    expected_frames_for_qualified_window: int = 0

    def advance(self, new_state: str) -> None:
        if new_state not in PIPELINE_STATE_TRANSITIONS:
            raise ValueError(f"Unknown pipeline state: {new_state!r}")
        self.state = new_state


@dataclass
class PipelineManifest:
    pipeline_id: str
    created_at: str
    selection_manifest_fingerprint: str
    selection_mode: str
    data_root: str
    symbols: list[str]
    symbol_states: dict[str, dict]
    is_start: str
    is_end: str
    oos_events_consumed: int = 0
    exchange_mutations: int = 0
    testnet_mutations: int = 0
    mainnet_mutations: int = 0
    real_capital_used: bool = False
    secrets_exposed: bool = False


class MultiSymbolResearchPipeline:
    """
    Bounded, deterministic orchestrator for multi-symbol order-flow research.

    No symbol may proceed to IS evaluation unless:
      1. InstrumentSpec is registered for that symbol.
      2. Historical data is acquired and qualified independently.
      3. The ScannerSelectionManifest declares a mode other than FORWARD_SELECTED.
      4. The selection_timestamp precedes is_start (temporal causality).

    BTC regression equivalence is verified by calling verify_btc_regression()
    after any builder refactor.
    """

    def __init__(
        self,
        data_root: Path,
        is_start: str,
        is_end: str,
        selection_manifest: ScannerSelectionManifest,
        qualification_registry: DatasetQualificationRegistry | None = None,
    ) -> None:
        self.data_root = Path(data_root)
        self.is_start = is_start
        self.is_end = is_end
        self.selection_manifest = selection_manifest
        self.qualification_registry = qualification_registry
        self.symbol_states: dict[str, SymbolPipelineState] = {}

        # Temporal causality guard
        self._validate_selection_causality()

    # ------------------------------------------------------------------ #
    # Causality guard                                                       #
    # ------------------------------------------------------------------ #

    def _validate_selection_causality(self) -> None:
        mode = self.selection_manifest.selection_mode
        if mode == "FORWARD_SELECTED":
            # Forward selection is allowed to exist; we just block IS eval.
            return
        if mode == "POINT_IN_TIME":
            raise ValueError(
                "POINT_IN_TIME_UNAVAILABLE: historical scanner reconstruction is not implemented; "
                "use a frozen FIXED_UNIVERSE"
            )
        sel = datetime.fromisoformat(
            self.selection_manifest.selection_timestamp.replace("Z", "+00:00")
        )
        is_start = datetime.fromisoformat(self.is_start.replace("Z", "+00:00"))
        if sel > is_start:
            raise ValueError(
                f"TEMPORAL_SELECTION_LEAKAGE: selection_timestamp ({sel}) "
                f"is after is_start ({is_start}). "
                "Use FORWARD_SELECTED mode or freeze a causally valid universe."
            )
        if mode == "POINT_IN_TIME" and not self.selection_manifest.historical_snapshot_id:
            raise ValueError(
                "POINT_IN_TIME_SNAPSHOT_REQUIRED: point-in-time selection needs "
                "an immutable historical scanner snapshot identifier"
            )

    def register_symbol(self, symbol: str) -> None:
        """Resolve InstrumentSpec; fail closed if missing."""
        if not InstrumentSpec.is_registered(symbol):
            raise KeyError(
                f"MISSING_INSTRUMENT_SPEC: {symbol!r} is not in the InstrumentSpec registry. "
                "Acquire and freeze metadata before adding it to the pipeline."
            )
        self.symbol_states[symbol] = SymbolPipelineState(symbol=symbol)
        self.symbol_states[symbol].advance("SCANNED")

    def mark_acquired(self, symbol: str, manifest: dict) -> None:
        self._require_symbol(symbol)
        if self.selection_manifest.selection_mode == "FORWARD_SELECTED":
            raise ValueError(
                f"TEMPORAL_SELECTION_LEAKAGE: {symbol!r} was FORWARD_SELECTED; "
                "it cannot participate in historical IS evaluation."
            )
        state = self.symbol_states[symbol]
        state.acquisition_manifest = manifest
        state.advance("ACQUIRED")

    def resolve_bootstrap(self, symbol: str, events: list[dict], requested_start: str | None = None, requested_end: str | None = None) -> BootstrapResolution:
        self._require_symbol(symbol)
        resolution = BootstrapResolver().resolve(
            symbol,
            events,
            requested_start or self.is_start,
            requested_end or self.is_end,
        )
        state = self.symbol_states[symbol]
        state.bootstrap_status = resolution.status
        state.bootstrap_resolution = resolution.to_dict()
        state.expected_frames_for_qualified_window = resolution.window.expected_frames_for_qualified_window
        if resolution.status == "BOOTSTRAP_QUALIFIED":
            state.advance("QUALIFICATION_REQUIRED")
        elif resolution.status == "BOOTSTRAP_PARTIAL":
            state.state = "BLOCKED"
            state.error = "PARTIAL_CAUSAL_WINDOW"
        else:
            state.state = "BLOCKED"
            state.error = resolution.bridge.get("reason", resolution.status)
        return resolution

    def mark_acquisition_required(self, symbol: str) -> None:
        self._require_symbol(symbol)
        self.symbol_states[symbol].advance("ACQUISITION_REQUIRED")

    def mark_qualified(self, symbol: str, status: str) -> None:
        self._require_symbol(symbol)
        state = self.symbol_states[symbol]
        state.qualification_status = status
        if status == "QUALIFIED":
            state.advance("QUALIFIED")
            return
        state.advance("BLOCKED")
        state.error = f"QUALIFICATION_REJECTED: {status}"

    def load_frozen_qualification(self, symbol: str) -> DatasetQualificationRecord:
        """Apply an immutable per-symbol qualification decision to pipeline state."""
        self._require_symbol(symbol)
        if self.qualification_registry is None:
            raise RuntimeError("QUALIFICATION_REGISTRY_UNAVAILABLE")
        record = self.qualification_registry.record(symbol)
        if record is None:
            self.symbol_states[symbol].advance("BLOCKED")
            self.symbol_states[symbol].error = "SYMBOL_NOT_QUALIFIED"
            raise ValueError(f"SYMBOL_NOT_QUALIFIED: {symbol}")
        self.mark_qualified(symbol, record.status)
        return record

    def mark_qualification_required(self, symbol: str) -> None:
        self._require_symbol(symbol)
        self.symbol_states[symbol].advance("QUALIFICATION_REQUIRED")

    def mark_frames_built(self, symbol: str, frame_count: int, frame_hash: str) -> None:
        self._require_symbol(symbol)
        if self.qualification_registry is not None:
            self.qualification_registry.require_replay_authority(symbol)
        state = self.symbol_states[symbol]
        state.frame_count = frame_count
        state.frame_hash = frame_hash
        state.advance("REPLAY_REQUIRED")

    def mark_complete(self, symbol: str) -> None:
        self._require_symbol(symbol)
        if self.symbol_states[symbol].state != "REPLAYING":
            self.symbol_states[symbol].advance("REPLAYING")
        self.symbol_states[symbol].advance("COMPLETE")

    def pipeline_manifest(self, pipeline_id: str) -> PipelineManifest:
        return PipelineManifest(
            pipeline_id=pipeline_id,
            created_at=datetime.now(timezone.utc).isoformat(),
            selection_manifest_fingerprint=self.selection_manifest.fingerprint(),
            selection_mode=self.selection_manifest.selection_mode,
            data_root=str(self.data_root),
            symbols=self.selection_manifest.selected_symbols,
            symbol_states={s: asdict(v) for s, v in self.symbol_states.items()},
            is_start=self.is_start,
            is_end=self.is_end,
        )

    def _require_symbol(self, symbol: str) -> None:
        if symbol not in self.symbol_states:
            raise KeyError(
                f"Symbol {symbol!r} is not registered in the pipeline. "
                "Call register_symbol() first."
            )

    # ------------------------------------------------------------------ #
    # BTC regression check                                                  #
    # ------------------------------------------------------------------ #

    @staticmethod
    def verify_btc_regression(actual_hash: str) -> dict[str, str]:
        """
        Compare a newly generated BTC frame-stream hash against the canonical one.
        Returns a dict with status=PASS or FAIL and the expected/actual hashes.
        """
        ok = actual_hash == BTC_CANONICAL_FRAME_HASH
        return {
            "status": "PASS" if ok else "FAIL",
            "expected_hash": BTC_CANONICAL_FRAME_HASH,
            "actual_hash": actual_hash,
            "note": (
                "Frame semantics preserved" if ok
                else "SEMANTIC_DRIFT: frame hash changed — diagnose before continuing"
            ),
        }


# ------------------------------------------------------------------ #
# Forward research path helper                                          #
# ------------------------------------------------------------------ #

@dataclass
class ForwardResearchCohort:
    """
    Container for forward-selected symbols.  Data collected after the
    selection_timestamp is strictly labelled FORWARD/PAPER and must not
    be merged into historical IS evidence.
    """
    selection_timestamp: str
    symbols: list[str]
    selection_fingerprint: str
    notes: str = "FORWARD_SELECTED — paper/prospective only"

    def assert_not_historical(self, is_start: str) -> None:
        sel = datetime.fromisoformat(self.selection_timestamp.replace("Z", "+00:00"))
        is_dt = datetime.fromisoformat(is_start.replace("Z", "+00:00"))
        if sel >= is_dt:
            raise ValueError(
                f"TEMPORAL_LEAKAGE_GUARD: cohort selected at {sel} is not available at IS start {is_dt}. "
                "Do not mix with historical IS datasets."
            )
