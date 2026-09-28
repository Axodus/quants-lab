"""
Research capability boundary — typed allowed operations for bounded AI research.

Trinity (or any AI orchestrator) must only call explicitly typed ResearchActions.
No generic shell execution.  Each action has an explicit authorization check,
input schema, and fail-closed behavior on unknown or out-of-scope operations.
"""
from __future__ import annotations
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from .multi_symbol_pipeline import DatasetQualificationRegistry


class ResearchActionKind(str, Enum):
    SCAN_MARKET            = "scan_market"
    INSPECT_SYMBOL         = "inspect_symbol"
    ACQUIRE_DATASET        = "acquire_historical_dataset"
    QUALIFY_DATASET        = "qualify_historical_dataset"
    BUILD_FRAMES           = "build_orderflow_frames"
    RUN_FROZEN_BACKTEST    = "run_frozen_orderflow_backtest"
    GET_RUN_STATUS         = "get_run_status"
    READ_CANONICAL_RESULTS = "read_canonical_results"
    CHECK_EVIDENCE_REUSE   = "check_evidence_reuse"
    EVALUATE_DEPLOYMENT_ROUTING = "evaluate_deployment_routing"


# ------------------------------------------------------------------ #
# Capability definitions                                                #
# ------------------------------------------------------------------ #

ALLOWED_BACKTEST_STRATEGIES = frozenset({
    "orderflow.momentum.aggression",
    "orderflow.absorption.fade",
    "orderflow.cvd.divergence.reversal",
})

# Symbols approved for forward/paper collection only.
ALLOWED_FORWARD_SYMBOLS = frozenset({
    "BTCUSDT", "ETHUSDC", "ZECUSDT", "SUIUSDT", "WLDUSDT", "SOLUSDT",
})

APPROVED_DATA_ROOT = Path("/run/media/mzfshark/Storage/Axodus/Trading/market-data")
APPROVED_VENV = APPROVED_DATA_ROOT / "venv"

CAPABILITY_DEFINITIONS: dict[ResearchActionKind, dict[str, Any]] = {
    ResearchActionKind.SCAN_MARKET: {
        "required": {"selection_mode", "universe", "selection_rule_version"},
        "optional": {"selection_timestamp", "market_snapshot_timestamp", "market_snapshot", "historical_snapshot_id"},
        "side_effects": ["persist scanner evidence"],
    },
    ResearchActionKind.INSPECT_SYMBOL: {"required": {"symbol"}, "optional": set(), "side_effects": []},
    ResearchActionKind.ACQUIRE_DATASET: {
        "required": {"symbol", "selection_mode", "selection_timestamp", "selected_symbols", "requested_start", "requested_end"},
        "optional": {"historical_snapshot_id", "dataset_id", "dataset_revision", "data_root"},
        "side_effects": ["public market-data read", "persist raw data and manifest"],
    },
    ResearchActionKind.QUALIFY_DATASET: {
        "required": {"symbol", "dataset_id", "dataset_revision"}, "optional": {"manifest_path", "dataset_freeze_sha256"},
        "side_effects": ["persist qualification evidence"],
    },
    ResearchActionKind.BUILD_FRAMES: {
        "required": {"symbol", "dataset_id", "dataset_revision", "period"}, "optional": {"data_root", "dataset_root", "dataset_freeze_sha256"},
        "side_effects": ["persist compact frame identity"],
    },
    ResearchActionKind.RUN_FROZEN_BACKTEST: {
        "required": {"strategy_id", "symbol", "period", "dataset_id", "dataset_revision"}, "optional": {"output_root", "dataset_freeze_sha256"},
        "side_effects": ["persist canonical research ledgers"],
    },
    ResearchActionKind.GET_RUN_STATUS: {"required": set(), "optional": {"receipt_id"}, "side_effects": []},
    ResearchActionKind.READ_CANONICAL_RESULTS: {
        "required": {"symbol", "dataset_id", "dataset_revision"}, "optional": {"strategy_id", "dataset_freeze_sha256"}, "side_effects": [],
    },
    ResearchActionKind.CHECK_EVIDENCE_REUSE: {
        "required": {"symbol", "strategy_id", "strategy_revision"},
        "optional": {"venue", "market_type", "is_stale", "is_redesigned", "current_fee_model", "current_execution_model", "current_parameter_fingerprint", "current_frame_builder_revision"},
        "side_effects": [],
    },
    ResearchActionKind.EVALUATE_DEPLOYMENT_ROUTING: {
        "required": {"symbol", "strategy_id", "strategy_revision"},
        "optional": {"scanner_status", "venue", "market_type", "market_info", "operational_context", "current_fee_model", "current_execution_model", "current_parameter_fingerprint", "current_frame_builder_revision"},
        "side_effects": [],
    },
}

SECRET_ARGUMENT_NAMES = frozenset({
    "api_key", "token", "secret", "password", "private_key", "credential", "credentials",
})


@dataclass
class ResearchCapabilityResult:
    action: str
    authorized: bool
    reason: str
    payload: dict[str, Any]


class ResearchCapabilityBoundary:
    """
    Gate that validates every AI-initiated research request.

    Usage:
        boundary = ResearchCapabilityBoundary()
        result = boundary.authorize(kind, kwargs)
        if not result.authorized:
            raise PermissionError(result.reason)
    """

    PROHIBITED = {
        "run_shell", "exec", "os.system", "subprocess",
        "place_order", "cancel_order", "modify_position",
    }

    def __init__(self, qualification_registry: DatasetQualificationRegistry | None = None) -> None:
        self.qualification_registry = qualification_registry

    def _require_qualified_symbol(self, symbol: str) -> ResearchCapabilityResult | None:
        if not symbol:
            return None
        if self.qualification_registry is None:
            return ResearchCapabilityResult(
                action="qualification", authorized=False,
                reason="QUALIFICATION_REGISTRY_UNAVAILABLE", payload={},
            )
        try:
            self.qualification_registry.require_replay_authority(symbol)
        except ValueError as exc:
            return ResearchCapabilityResult(
                action="qualification", authorized=False, reason=str(exc), payload={},
            )
        return None

    def authorize(
        self, kind: str | ResearchActionKind, kwargs: dict[str, Any]
    ) -> ResearchCapabilityResult:
        if not isinstance(kwargs, dict):
            return ResearchCapabilityResult(
                action=str(kind), authorized=False, reason="ARGUMENTS_MUST_BE_OBJECT", payload={}
            )
        # Reject arbitrary execution immediately
        if kind in self.PROHIBITED or (isinstance(kind, str) and kind.lower() in self.PROHIBITED):
            return ResearchCapabilityResult(
                action=str(kind), authorized=False,
                reason=f"ARBITRARY_EXECUTION_REJECTED: {kind!r} is not a typed research capability",
                payload={},
            )

        try:
            action_kind = ResearchActionKind(kind)
        except ValueError:
            return ResearchCapabilityResult(
                action=str(kind), authorized=False,
                reason=f"UNKNOWN_ACTION: {kind!r} is not in the ResearchActionKind registry",
                payload={},
            )

        secret_fields = _secret_keys(kwargs)
        if secret_fields:
            return ResearchCapabilityResult(
                action=action_kind.value, authorized=False,
                reason=f"CREDENTIAL_ARGUMENT_REJECTED: {sorted(secret_fields)}", payload={},
            )
        definition = CAPABILITY_DEFINITIONS[action_kind]
        supplied = set(kwargs)
        missing = definition["required"] - supplied
        unknown = supplied - definition["required"] - definition["optional"]
        if missing:
            return ResearchCapabilityResult(
                action=action_kind.value, authorized=False,
                reason=f"MISSING_ARGUMENTS: {sorted(missing)}", payload={},
            )
        if unknown:
            return ResearchCapabilityResult(
                action=action_kind.value, authorized=False,
                reason=f"UNSUPPORTED_ARGUMENTS: {sorted(unknown)}", payload={},
            )
        method = getattr(self, f"_check_{action_kind.value.lower()}", None)
        if method is None:
            return ResearchCapabilityResult(
                action=action_kind.value, authorized=False,
                reason=f"NOT_IMPLEMENTED: no authorization handler for {action_kind.value!r}",
                payload={},
            )
        return method(kwargs)

    # ------------------------------------------------------------------ #
    # Per-action handlers                                                  #
    # ------------------------------------------------------------------ #

    def _check_scan_market(self, kw: dict) -> ResearchCapabilityResult:
        mode = kw.get("selection_mode")
        if mode not in {"FORWARD_SELECTED", "FIXED_UNIVERSE", "POINT_IN_TIME"}:
            return ResearchCapabilityResult(
                action=ResearchActionKind.SCAN_MARKET.value, authorized=False,
                reason="INVALID_SELECTION_MODE", payload={},
            )
        if mode == "POINT_IN_TIME":
            return ResearchCapabilityResult(
                action=ResearchActionKind.SCAN_MARKET.value, authorized=False,
                reason="POINT_IN_TIME_UNAVAILABLE: historical scanner reconstruction is not implemented",
                payload={},
            )
        for field in ("selection_timestamp", "market_snapshot_timestamp"):
            if field in kw:
                try:
                    _parse_utc(kw[field])
                except (TypeError, ValueError):
                    return ResearchCapabilityResult(
                        action=ResearchActionKind.SCAN_MARKET.value, authorized=False,
                        reason="INVALID_SCANNER_TIMESTAMP", payload={},
                    )
        if "market_snapshot" in kw and not isinstance(kw["market_snapshot"], list):
            return ResearchCapabilityResult(
                action=ResearchActionKind.SCAN_MARKET.value, authorized=False,
                reason="INVALID_MARKET_SNAPSHOT", payload={},
            )
        return ResearchCapabilityResult(
            action=ResearchActionKind.SCAN_MARKET.value, authorized=True,
            reason="PASS: read-only public market data",
            payload={"allowed_venue": "Binance USD-M Futures", "mutations": 0},
        )

    def _check_inspect_symbol(self, kw: dict) -> ResearchCapabilityResult:
        symbol = kw.get("symbol", "")
        if not symbol:
            return ResearchCapabilityResult(
                action=ResearchActionKind.INSPECT_SYMBOL.value, authorized=False,
                reason="MISSING_SYMBOL", payload={},
            )
        return ResearchCapabilityResult(
            action=ResearchActionKind.INSPECT_SYMBOL.value, authorized=True,
            reason="PASS: read-only instrument metadata",
            payload={"symbol": symbol, "mutations": 0},
        )

    def _check_acquire_historical_dataset(self, kw: dict) -> ResearchCapabilityResult:
        symbol = kw.get("symbol", "")
        mode = kw.get("selection_mode", "")
        if mode == "FORWARD_SELECTED":
            return ResearchCapabilityResult(
                action=ResearchActionKind.ACQUIRE_DATASET.value, authorized=False,
                reason="TEMPORAL_SELECTION_LEAKAGE: FORWARD_SELECTED symbol cannot be used for historical acquisition",
                payload={},
            )
        if mode not in {"FIXED_UNIVERSE", "POINT_IN_TIME"}:
            return ResearchCapabilityResult(
                action=ResearchActionKind.ACQUIRE_DATASET.value, authorized=False,
                reason="INVALID_SELECTION_MODE", payload={},
            )
        if not isinstance(kw.get("selected_symbols"), list) or symbol not in kw["selected_symbols"]:
            return ResearchCapabilityResult(
                action=ResearchActionKind.ACQUIRE_DATASET.value, authorized=False,
                reason="SYMBOL_NOT_IN_FROZEN_SELECTION", payload={},
            )
        try:
            selection_time = _parse_utc(kw["selection_timestamp"])
            window_start = _parse_utc(kw["requested_start"])
            _parse_utc(kw["requested_end"])
        except (TypeError, ValueError, KeyError):
            return ResearchCapabilityResult(
                action=ResearchActionKind.ACQUIRE_DATASET.value, authorized=False,
                reason="INVALID_SELECTION_OR_WINDOW_TIMESTAMP", payload={},
            )
        if selection_time > window_start:
            return ResearchCapabilityResult(
                action=ResearchActionKind.ACQUIRE_DATASET.value, authorized=False,
                reason="TEMPORAL_SELECTION_LEAKAGE: selection is later than requested historical window",
                payload={},
            )
        if mode == "POINT_IN_TIME":
            return ResearchCapabilityResult(
                action=ResearchActionKind.ACQUIRE_DATASET.value, authorized=False,
                reason="POINT_IN_TIME_UNAVAILABLE: historical scanner reconstruction is not implemented",
                payload={},
            )
        if not symbol:
            return ResearchCapabilityResult(
                action=ResearchActionKind.ACQUIRE_DATASET.value, authorized=False,
                reason="MISSING_SYMBOL", payload={},
            )
        # Acquisition precedes qualification.  Require a registered metadata
        # contract and a causally valid selection, never prior qualification.
        from .instrument_spec import InstrumentSpec
        if not InstrumentSpec.is_registered(symbol):
            return ResearchCapabilityResult(
                action=ResearchActionKind.ACQUIRE_DATASET.value, authorized=False,
                reason=f"MISSING_INSTRUMENT_SPEC: {symbol!r} must be frozen before acquisition",
                payload={},
            )
        return ResearchCapabilityResult(
            action=ResearchActionKind.ACQUIRE_DATASET.value, authorized=True,
            reason="PASS: bounded historical acquisition request; qualification remains required before replay",
            payload={"data_root": str(APPROVED_DATA_ROOT), "mutations": 0},
        )

    def _check_qualify_historical_dataset(self, kw: dict) -> ResearchCapabilityResult:
        if self.qualification_registry is None:
            return ResearchCapabilityResult(ResearchActionKind.QUALIFY_DATASET.value, False, "QUALIFICATION_REGISTRY_UNAVAILABLE", {})
        try:
            self.qualification_registry.require_dataset_replay_authority(
                kw.get("symbol", ""), kw.get("dataset_id"), kw.get("dataset_revision")
            )
        except ValueError as exc:
            return ResearchCapabilityResult(ResearchActionKind.QUALIFY_DATASET.value, False, str(exc), {})
        if kw.get("dataset_freeze_sha256") not in {None, self.qualification_registry.source_sha256}:
            return ResearchCapabilityResult(ResearchActionKind.QUALIFY_DATASET.value, False, "DATASET_FREEZE_HASH_MISMATCH", {})
        return ResearchCapabilityResult(
            action=ResearchActionKind.QUALIFY_DATASET.value, authorized=True,
            reason="PASS: qualification is read-only over acquired raw data",
            payload={"mutations": 0},
        )

    def _check_build_orderflow_frames(self, kw: dict) -> ResearchCapabilityResult:
        symbol = kw.get("symbol", "")
        if self.qualification_registry is None:
            return ResearchCapabilityResult(
                action=ResearchActionKind.BUILD_FRAMES.value, authorized=False,
                reason="QUALIFICATION_REGISTRY_UNAVAILABLE", payload={},
            )
        try:
            self.qualification_registry.require_dataset_replay_authority(symbol, kw.get("dataset_id"), kw.get("dataset_revision"))
        except ValueError as exc:
            return ResearchCapabilityResult(
                action=ResearchActionKind.BUILD_FRAMES.value, authorized=False,
                reason=str(exc), payload={},
            )
        if kw.get("period") != "IS":
            return ResearchCapabilityResult(
                action=ResearchActionKind.BUILD_FRAMES.value, authorized=False,
                reason="PERIOD_NOT_AUTHORIZED: only a frozen qualified IS period is accepted",
                payload={},
            )
        if kw.get("dataset_freeze_sha256") not in {None, self.qualification_registry.source_sha256}:
            return ResearchCapabilityResult(ResearchActionKind.BUILD_FRAMES.value, False, "DATASET_FREEZE_HASH_MISMATCH", {})
        return ResearchCapabilityResult(
            action=ResearchActionKind.BUILD_FRAMES.value, authorized=True,
            reason="PASS",
            payload={"mutations": 0},
        )

    def _check_run_frozen_orderflow_backtest(self, kw: dict) -> ResearchCapabilityResult:
        strategy_id = kw.get("strategy_id", "")
        symbol = kw.get("symbol", "")
        period = kw.get("period", "IS")
        if period == "OOS":
            return ResearchCapabilityResult(
                action=ResearchActionKind.RUN_FROZEN_BACKTEST.value, authorized=False,
                reason="OOS_SEALED: out-of-sample access is prohibited without explicit CTO authorization",
                payload={},
            )
        if period != "IS":
            return ResearchCapabilityResult(
                action=ResearchActionKind.RUN_FROZEN_BACKTEST.value, authorized=False,
                reason="PERIOD_NOT_AUTHORIZED: only a frozen qualified IS period is accepted",
                payload={},
            )
        if strategy_id and strategy_id not in ALLOWED_BACKTEST_STRATEGIES:
            return ResearchCapabilityResult(
                action=ResearchActionKind.RUN_FROZEN_BACKTEST.value, authorized=False,
                reason=f"UNKNOWN_STRATEGY: {strategy_id!r} is not in the frozen strategy registry",
                payload={},
            )
        if self.qualification_registry is None:
            return ResearchCapabilityResult(
                action=ResearchActionKind.RUN_FROZEN_BACKTEST.value, authorized=False,
                reason="QUALIFICATION_REGISTRY_UNAVAILABLE", payload={},
            )
        try:
            self.qualification_registry.require_dataset_replay_authority(symbol, kw.get("dataset_id"), kw.get("dataset_revision"))
        except ValueError as exc:
            return ResearchCapabilityResult(
                action=ResearchActionKind.RUN_FROZEN_BACKTEST.value, authorized=False,
                reason=str(exc), payload={},
            )
        if kw.get("dataset_freeze_sha256") not in {None, self.qualification_registry.source_sha256}:
            return ResearchCapabilityResult(ResearchActionKind.RUN_FROZEN_BACKTEST.value, False, "DATASET_FREEZE_HASH_MISMATCH", {})
        return ResearchCapabilityResult(
            action=ResearchActionKind.RUN_FROZEN_BACKTEST.value, authorized=True,
            reason="PASS",
            payload={"mutations": 0, "exchange_order_mutations": 0},
        )

    def _check_get_run_status(self, kw: dict) -> ResearchCapabilityResult:
        return ResearchCapabilityResult(
            action=ResearchActionKind.GET_RUN_STATUS.value, authorized=True,
            reason="PASS: read-only status check",
            payload={"mutations": 0},
        )

    def _check_read_canonical_results(self, kw: dict) -> ResearchCapabilityResult:
        if self.qualification_registry is None:
            return ResearchCapabilityResult(
                action=ResearchActionKind.READ_CANONICAL_RESULTS.value,
                authorized=False,
                reason="QUALIFICATION_REGISTRY_UNAVAILABLE",
                payload={},
            )
        try:
            self.qualification_registry.require_dataset_replay_authority(
                kw.get("symbol", ""), kw.get("dataset_id"), kw.get("dataset_revision")
            )
        except ValueError as exc:
            return ResearchCapabilityResult(
                action=ResearchActionKind.READ_CANONICAL_RESULTS.value,
                authorized=False,
                reason=str(exc),
                payload={},
            )
        if kw.get("dataset_freeze_sha256") not in {None, self.qualification_registry.source_sha256}:
            return ResearchCapabilityResult(ResearchActionKind.READ_CANONICAL_RESULTS.value, False, "DATASET_FREEZE_HASH_MISMATCH", {})
        return ResearchCapabilityResult(
            action=ResearchActionKind.READ_CANONICAL_RESULTS.value, authorized=True,
            reason="PASS: read-only result retrieval",
            payload={"mutations": 0},
        )

    def _check_check_evidence_reuse(self, kw: dict) -> ResearchCapabilityResult:
        strategy_id = kw.get("strategy_id", "")
        if strategy_id not in ALLOWED_BACKTEST_STRATEGIES:
            return ResearchCapabilityResult(
                action=ResearchActionKind.CHECK_EVIDENCE_REUSE.value,
                authorized=False,
                reason=f"UNKNOWN_STRATEGY: {strategy_id!r} is not in the frozen strategy registry",
                payload={},
            )
        if not kw.get("symbol"):
            return ResearchCapabilityResult(
                action=ResearchActionKind.CHECK_EVIDENCE_REUSE.value,
                authorized=False,
                reason="MISSING_SYMBOL",
                payload={},
            )
        return ResearchCapabilityResult(
            action=ResearchActionKind.CHECK_EVIDENCE_REUSE.value,
            authorized=True,
            reason="PASS: read-only evidence reuse check",
            payload={"mutations": 0},
        )

    def _check_evaluate_deployment_routing(self, kw: dict) -> ResearchCapabilityResult:
        strategy_id = kw.get("strategy_id", "")
        if strategy_id not in ALLOWED_BACKTEST_STRATEGIES:
            return ResearchCapabilityResult(
                action=ResearchActionKind.EVALUATE_DEPLOYMENT_ROUTING.value,
                authorized=False,
                reason=f"UNKNOWN_STRATEGY: {strategy_id!r} is not in the frozen strategy registry",
                payload={},
            )
        if not kw.get("symbol"):
            return ResearchCapabilityResult(
                action=ResearchActionKind.EVALUATE_DEPLOYMENT_ROUTING.value,
                authorized=False,
                reason="MISSING_SYMBOL",
                payload={},
            )
        return ResearchCapabilityResult(
            action=ResearchActionKind.EVALUATE_DEPLOYMENT_ROUTING.value,
            authorized=True,
            reason="PASS: read-only deployment readiness evaluation",
            payload={"mutations": 0, "exchange_order_mutations": 0},
        )


def _parse_utc(value: Any) -> datetime:
    if not isinstance(value, str):
        raise TypeError("timestamp must be ISO-8601 string")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must include timezone")
    return parsed.astimezone(timezone.utc)


def _secret_keys(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).lower()
            if normalized in SECRET_ARGUMENT_NAMES:
                found.add(normalized)
            found.update(_secret_keys(child))
    elif isinstance(value, (list, tuple)):
        for child in value:
            found.update(_secret_keys(child))
    return found
