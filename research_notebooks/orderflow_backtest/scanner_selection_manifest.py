"""
Scanner selection manifest — carries timestamped scanner output with
explicit causal mode declaration.

A FORWARD_SELECTED cohort may only be used for prospective/paper research.
A FIXED_UNIVERSE cohort is a pre-committed universe frozen before any
historical evaluation.
A POINT_IN_TIME cohort must reference the historical scanner snapshot that
would have been available at the declared selection_timestamp.

Mixing historical data with FORWARD_SELECTED symbols is temporal leakage and
is explicitly rejected by MultiSymbolResearchPipeline.
"""
from __future__ import annotations
import hashlib, json
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Literal
from pathlib import Path


SelectionMode = Literal[
    "FORWARD_SELECTED",    # symbols chosen after now; prospective only
    "FIXED_UNIVERSE",      # symbols chosen before looking at IS outcomes
    "POINT_IN_TIME",       # symbols chosen using only information <= selection_timestamp
]


@dataclass
class ScannerSelectionManifest:
    """Typed, hash-addressable scanner output."""

    selection_mode: SelectionMode
    selection_timestamp: str     # ISO 8601 UTC; when the selection was made
    universe: str                # e.g. "Binance USD-M USDT Perpetuals top-100"
    selection_rule_version: str
    selected_symbols: list[str]
    selection_scores: dict[str, float] = field(default_factory=dict)
    notes: str = ""
    historical_snapshot_id: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    def fingerprint(self) -> str:
        canonical = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()

    def persist(self, path: Path) -> str:
        """Persist the immutable selection evidence before downstream work."""
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        payload = {**self.to_dict(), "selectionFingerprint": self.fingerprint()}
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        temporary.replace(destination)
        return payload["selectionFingerprint"]

    def is_usable_for_historical_is(self, is_start: str) -> bool:
        """Return True only when selection pre-dates the IS start."""
        sel_dt = datetime.fromisoformat(self.selection_timestamp.replace("Z", "+00:00"))
        is_dt = datetime.fromisoformat(is_start.replace("Z", "+00:00"))
        if self.selection_mode == "FORWARD_SELECTED":
            return False
        # A caller-provided identifier is not proof that the scanner inputs
        # existed at that historical time. Point-in-time mode remains closed
        # until a hash-verified snapshot loader is implemented.
        if self.selection_mode == "POINT_IN_TIME":
            return False
        return sel_dt <= is_dt
