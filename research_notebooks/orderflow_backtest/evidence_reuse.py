"""
Evidence Reuse Registry & Deployment Routing for Order Flow Research Pipeline.

Implements the decision point after hot-asset selection:
  HOT ASSET SELECTION
          ↓
  PAIR × STRATEGY EVIDENCE LOOKUP
          │
          ├── VALIDATED + REUSABLE
          │        ↓
          │   DEPLOYMENT READINESS GATE
          │        ↓
          │   TESTNET / MAINNET eligibility
          │
          └── NEW / STALE / INCOMPATIBLE
                   ↓
             HISTORICAL DATA
                   ↓
              72h IS GATE
                   ↓
               OOS VALIDATION
                   ↓
            DEPLOYMENT READINESS

Unit of reuse: PAIR × STRATEGY REVISION × VENUE × MARKET TYPE.
Enforces the Axodus invariant:
  AI evaluates.
  Axodus authorities authorize.
  Deterministic code executes.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any

from .instrument_spec import InstrumentSpec


class ReuseClassification(str, Enum):
    VALIDATED_REUSABLE = "VALIDATED_REUSABLE"
    VALIDATED_BUT_STALE = "VALIDATED_BUT_STALE"
    VALIDATED_BUT_INCOMPATIBLE = "VALIDATED_BUT_INCOMPATIBLE"
    NOT_VALIDATED = "NOT_VALIDATED"


class DeploymentState(str, Enum):
    HISTORICAL_VALIDATION_REQUIRED = "HISTORICAL_VALIDATION_REQUIRED"
    HISTORICAL_VALIDATION_REUSABLE = "HISTORICAL_VALIDATION_REUSABLE"
    TESTNET_ELIGIBLE = "TESTNET_ELIGIBLE"
    TESTNET_VALIDATION_REQUIRED = "TESTNET_VALIDATION_REQUIRED"
    MAINNET_ELIGIBLE = "MAINNET_ELIGIBLE"
    DEPLOYMENT_BLOCKED = "DEPLOYMENT_BLOCKED"


@dataclass
class ValidationCellRecord:
    """Canonical validation record for one (pair, strategy_revision, venue, market_type) cell."""

    symbol: str
    strategy_id: str
    strategy_revision: str
    venue: str = "Binance USD-M Futures"
    market_type: str = "USD-M Futures"
    instrument_spec_hash: str = ""
    fee_model: str = "BTCUSDT maker=0.0002 taker=0.0005"
    execution_model: str = "execution-model-v1-conservative"
    position_sizing_model: str = "fixed-notional-1000-usd"
    parameter_fingerprint: str = ""
    frame_builder_revision: str = ""
    is_evidence: dict[str, Any] = field(default_factory=dict)
    oos_evidence: dict[str, Any] | None = None
    validation_status: str = "NOT_VALIDATED"
    validation_date: str = ""
    dataset_id: str = ""
    dataset_window: dict[str, str] = field(default_factory=dict)
    canonical_ledgers: dict[str, str] = field(default_factory=dict)
    artifact_hashes: dict[str, str] = field(default_factory=dict)
    testnet_status: str = "NOT_EVALUATED"
    mainnet_status: str = "BLOCKED"
    active_blockers: list[str] = field(default_factory=list)
    invalidation_conditions: list[str] = field(default_factory=list)

    @property
    def cell_key(self) -> str:
        return f"{self.symbol}:{self.strategy_id}:{self.strategy_revision}:{self.venue}:{self.market_type}"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ValidationCellRecord:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class CurrentMarketInfo:
    """Current live/market state for lightweight compatibility checking."""

    symbol: str
    venue: str = "Binance USD-M Futures"
    market_type: str = "USD-M Futures"
    is_listed: bool = True
    tick_size: Decimal | str | None = None
    step_size: Decimal | str | None = None
    instrument_spec_hash: str | None = None
    fee_schedule: dict[str, Any] | None = None
    max_leverage: int | None = None
    bid_ask_spread_ticks: float | None = None
    depth_top_levels: float | None = None
    liquidity_24h_usd: float | None = None
    strategy_revision: str | None = None

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        if self.tick_size is not None:
            result["tick_size"] = str(self.tick_size)
        if self.step_size is not None:
            result["step_size"] = str(self.step_size)
        return result


@dataclass
class OperationalDeploymentContext:
    """Operational and governance prerequisites for Testnet/Mainnet routing."""

    testnet_credentials_available: bool = False
    mainnet_credentials_available: bool = False
    safety_supervisor_active: bool = False
    risk_authority_approved: bool = False
    capital_authority_approved: bool = False
    real_capital_authorized: bool = False
    execution_readiness_passed: bool = False
    protection_readiness_passed: bool = False
    active_governance_blockers: list[str] = field(default_factory=lambda: ["DEBT-AUD-A-03"])
    active_operational_blockers: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DeploymentRecommendation:
    """Deterministic routing decision for one Pair × Strategy Revision."""

    symbol: str
    strategy_id: str
    strategy_revision: str
    scanner_status: str
    reuse_classification: str
    deployment_state: str
    current_compatibility: str
    current_compatibility_details: dict[str, Any]
    testnet: str
    mainnet: str
    blockers: list[str]
    recommended_next_action: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class EvidenceReuseRegistry:
    """Canonical registry and router for historical validation evidence reuse."""

    def __init__(self, records: dict[str, ValidationCellRecord] | None = None) -> None:
        self._records: dict[str, ValidationCellRecord] = records or {}

    @classmethod
    def cell_key(
        cls,
        symbol: str,
        strategy_id: str,
        strategy_revision: str,
        venue: str = "Binance USD-M Futures",
        market_type: str = "USD-M Futures",
    ) -> str:
        return f"{symbol}:{strategy_id}:{strategy_revision}:{venue}:{market_type}"

    def register_cell(self, record: ValidationCellRecord) -> None:
        self._records[record.cell_key] = record

    def lookup(
        self,
        symbol: str,
        strategy_id: str,
        strategy_revision: str,
        venue: str = "Binance USD-M Futures",
        market_type: str = "USD-M Futures",
    ) -> ValidationCellRecord | None:
        key = self.cell_key(symbol, strategy_id, strategy_revision, venue, market_type)
        return self._records.get(key)

    def classify(
        self,
        symbol: str,
        strategy_id: str,
        strategy_revision: str,
        current_spec: InstrumentSpec | None = None,
        current_fee_model: str | None = None,
        current_execution_model: str | None = None,
        current_parameter_fingerprint: str | None = None,
        current_frame_builder_revision: str | None = None,
        venue: str = "Binance USD-M Futures",
        market_type: str = "USD-M Futures",
        is_stale: bool = False,
        is_redesigned: bool = False,
    ) -> tuple[ReuseClassification, str, list[str]]:
        """Classify a pair × strategy cell against frozen validation invariants."""
        record = self.lookup(symbol, strategy_id, strategy_revision, venue, market_type)
        if record is None:
            return (
                ReuseClassification.NOT_VALIDATED,
                "FIRST_TIME_PAIR_OR_STRATEGY: no historical validation record found in registry",
                ["NO_PREVIOUS_VALIDATION"],
            )

        blockers: list[str] = list(record.active_blockers)

        if is_stale or record.validation_status == "STALE":
            return (
                ReuseClassification.VALIDATED_BUT_STALE,
                "VALIDATION_STALE: methodology or dataset horizon requires refresh",
                ["EVIDENCE_STALE", *blockers],
            )

        if is_redesigned:
            return (
                ReuseClassification.VALIDATED_BUT_INCOMPATIBLE,
                "STRATEGY_REDESIGNED: strategy revision parameters or semantics mutated",
                ["STRATEGY_REDESIGNED_SINCE_VALIDATION"],
            )

        if current_spec is not None:
            if record.instrument_spec_hash and record.instrument_spec_hash != current_spec.canonical_hash():
                return (
                    ReuseClassification.VALIDATED_BUT_INCOMPATIBLE,
                    f"INSTRUMENT_SPEC_MISMATCH: record hash {record.instrument_spec_hash} != current {current_spec.canonical_hash()}",
                    ["INSTRUMENT_SPEC_CHANGED"],
                )

        if current_fee_model is not None and record.fee_model != current_fee_model:
            return (
                ReuseClassification.VALIDATED_BUT_INCOMPATIBLE,
                f"FEE_MODEL_MISMATCH: record {record.fee_model} != current {current_fee_model}",
                ["FEE_MODEL_CHANGED"],
            )

        if current_execution_model is not None and record.execution_model != current_execution_model:
            return (
                ReuseClassification.VALIDATED_BUT_INCOMPATIBLE,
                f"EXECUTION_MODEL_MISMATCH: record {record.execution_model} != current {current_execution_model}",
                ["EXECUTION_MODEL_CHANGED"],
            )

        if current_parameter_fingerprint is not None and record.parameter_fingerprint != current_parameter_fingerprint:
            return (
                ReuseClassification.VALIDATED_BUT_INCOMPATIBLE,
                "PARAMETER_FINGERPRINT_MISMATCH: frozen strategy parameters changed",
                ["STRATEGY_PARAMETERS_CHANGED"],
            )

        if current_frame_builder_revision is not None and record.frame_builder_revision != current_frame_builder_revision:
            return (
                ReuseClassification.VALIDATED_BUT_INCOMPATIBLE,
                "FRAME_BUILDER_REVISION_MISMATCH: causal frame semantics changed",
                ["FRAME_BUILDER_CHANGED"],
            )

        if record.oos_evidence is None:
            return (
                ReuseClassification.VALIDATED_BUT_INCOMPATIBLE,
                "OOS_NOT_VALIDATED: canonical evidence is IS-only and cannot skip full validation",
                ["OOS_NOT_VALIDATED"],
            )

        if record.invalidation_conditions:
            return (
                ReuseClassification.VALIDATED_BUT_INCOMPATIBLE,
                f"INVALIDATION_TRIGGERED: {', '.join(record.invalidation_conditions)}",
                record.invalidation_conditions,
            )

        return (
            ReuseClassification.VALIDATED_REUSABLE,
            "VALIDATED_REUSABLE: canonical validation matches frozen invariants; historical replay can be skipped",
            blockers,
        )

    def check_current_compatibility(
        self,
        record: ValidationCellRecord,
        market_info: CurrentMarketInfo,
    ) -> dict[str, Any]:
        """Perform a lightweight current-market compatibility check."""
        failures: list[str] = []
        checks: dict[str, bool] = {}

        checks["symbol_listed"] = market_info.is_listed
        if not market_info.is_listed:
            failures.append("SYMBOL_DELISTED_OR_INACTIVE")

        checks["venue_match"] = market_info.venue == record.venue
        if not checks["venue_match"]:
            failures.append(f"VENUE_CHANGED: {market_info.venue} != {record.venue}")

        checks["market_type_match"] = market_info.market_type == record.market_type
        if not checks["market_type_match"]:
            failures.append(f"MARKET_TYPE_CHANGED: {market_info.market_type} != {record.market_type}")

        if market_info.instrument_spec_hash is not None and record.instrument_spec_hash:
            checks["instrument_spec_hash_match"] = (
                market_info.instrument_spec_hash == record.instrument_spec_hash
            )
            if not checks["instrument_spec_hash_match"]:
                failures.append("CURRENT_INSTRUMENT_SPEC_HASH_DRIFT")

        if market_info.tick_size is not None and InstrumentSpec.is_registered(record.symbol):
            spec = InstrumentSpec.from_registry(record.symbol)
            checks["tick_size_match"] = Decimal(str(market_info.tick_size)) == spec.tick_size
            if not checks["tick_size_match"]:
                failures.append(f"TICK_SIZE_CHANGED: current={market_info.tick_size} spec={spec.tick_size}")

        if market_info.step_size is not None and InstrumentSpec.is_registered(record.symbol):
            spec = InstrumentSpec.from_registry(record.symbol)
            checks["step_size_match"] = Decimal(str(market_info.step_size)) == spec.step_size
            if not checks["step_size_match"]:
                failures.append(f"STEP_SIZE_CHANGED: current={market_info.step_size} spec={spec.step_size}")

        if market_info.strategy_revision is not None:
            checks["strategy_revision_match"] = market_info.strategy_revision == record.strategy_revision
            if not checks["strategy_revision_match"]:
                failures.append(f"STRATEGY_REVISION_MISMATCH: current={market_info.strategy_revision} != {record.strategy_revision}")

        if market_info.bid_ask_spread_ticks is not None:
            checks["spread_within_bounds"] = market_info.bid_ask_spread_ticks <= 5.0
            if not checks["spread_within_bounds"]:
                failures.append(f"SPREAD_EXCEEDS_OPERATIONAL_LIMIT: {market_info.bid_ask_spread_ticks} ticks")

        compatible = len(failures) == 0
        return {
            "compatible": compatible,
            "status": "PASS" if compatible else "FAIL",
            "checks": checks,
            "failures": failures,
        }

    def evaluate_deployment_readiness(
        self,
        symbol: str,
        strategy_id: str,
        strategy_revision: str,
        scanner_status: str = "HOT",
        market_info: CurrentMarketInfo | None = None,
        operational_context: OperationalDeploymentContext | None = None,
        current_spec: InstrumentSpec | None = None,
        current_fee_model: str | None = None,
        current_execution_model: str | None = None,
        current_parameter_fingerprint: str | None = None,
        current_frame_builder_revision: str | None = None,
        venue: str = "Binance USD-M Futures",
        market_type: str = "USD-M Futures",
    ) -> DeploymentRecommendation:
        """Evaluate deployment readiness and routing for one Pair × Strategy cell."""
        market_info = market_info or CurrentMarketInfo(symbol=symbol, venue=venue, market_type=market_type)
        operational_context = operational_context or OperationalDeploymentContext()
        current_spec = current_spec or (InstrumentSpec.from_registry(symbol) if InstrumentSpec.is_registered(symbol) else None)

        classification, reason, cell_blockers = self.classify(
            symbol=symbol,
            strategy_id=strategy_id,
            strategy_revision=strategy_revision,
            current_spec=current_spec,
            current_fee_model=current_fee_model,
            current_execution_model=current_execution_model,
            current_parameter_fingerprint=current_parameter_fingerprint,
            current_frame_builder_revision=current_frame_builder_revision,
            venue=venue,
            market_type=market_type,
        )

        all_blockers: list[str] = list(cell_blockers)
        record = self.lookup(symbol, strategy_id, strategy_revision, venue, market_type)

        if record is not None:
            compat = self.check_current_compatibility(record, market_info)
            if not compat["compatible"]:
                classification = ReuseClassification.VALIDATED_BUT_INCOMPATIBLE
                all_blockers.extend(compat["failures"])
        else:
            compat = {"compatible": False, "status": "NOT_CHECKED", "checks": {}, "failures": ["NO_VALIDATED_RECORD"]}

        testnet_eligible = False
        testnet_blockers: list[str] = []
        if classification != ReuseClassification.VALIDATED_REUSABLE:
            testnet_blockers.append("HISTORICAL_VALIDATION_NOT_REUSABLE")
        if not compat.get("compatible", False):
            testnet_blockers.append("CURRENT_MARKET_INCOMPATIBLE")
        if record is not None and record.validation_status in {"IS_EVIDENCE_NEGATIVE", "ACCEPTED_AS_NEGATIVE_IS", "FAILED"}:
            testnet_blockers.append(f"STRATEGY_VALIDATION_STATUS_{record.validation_status}")

        if not testnet_blockers:
            testnet_eligible = True
            testnet_status = "ELIGIBLE"
        else:
            testnet_status = "BLOCKED"
            all_blockers.extend([b for b in testnet_blockers if b not in all_blockers])

        mainnet_eligible = False
        mainnet_blockers: list[str] = []
        if classification != ReuseClassification.VALIDATED_REUSABLE:
            mainnet_blockers.append("HISTORICAL_VALIDATION_NOT_REUSABLE")
        if record is None or record.oos_evidence is None or record.validation_status != "ACCEPTED":
            mainnet_blockers.append("OOS_PERFORMANCE_VALIDATION_REQUIRED")
        if not compat.get("compatible", False):
            mainnet_blockers.append("CURRENT_MARKET_INCOMPATIBLE")
        if not operational_context.safety_supervisor_active:
            mainnet_blockers.append("SAFETY_SUPERVISOR_NOT_ACTIVE")
        if not operational_context.risk_authority_approved:
            mainnet_blockers.append("RISK_AUTHORITY_APPROVAL_REQUIRED")
        if not operational_context.capital_authority_approved:
            mainnet_blockers.append("CAPITAL_AUTHORITY_APPROVAL_REQUIRED")
        if not operational_context.real_capital_authorized:
            mainnet_blockers.append("REAL_CAPITAL_NOT_AUTHORIZED")
        if not operational_context.execution_readiness_passed:
            mainnet_blockers.append("EXECUTION_READINESS_GATE_REQUIRED")
        if not operational_context.protection_readiness_passed:
            mainnet_blockers.append("PROTECTION_READINESS_GATE_REQUIRED")
        if operational_context.active_governance_blockers:
            mainnet_blockers.extend(operational_context.active_governance_blockers)
        if operational_context.active_operational_blockers:
            mainnet_blockers.extend(operational_context.active_operational_blockers)

        if not mainnet_blockers and testnet_eligible:
            mainnet_eligible = True
            mainnet_status = "ELIGIBLE"
        else:
            mainnet_status = "BLOCKED"
            all_blockers.extend([b for b in mainnet_blockers if b not in all_blockers])

        if classification == ReuseClassification.NOT_VALIDATED:
            deployment_state = DeploymentState.HISTORICAL_VALIDATION_REQUIRED
            recommended_action = "EXECUTE_FULL_HISTORICAL_PIPELINE: acquire data -> qualify -> 72h IS trigger gate -> OOS"
        elif classification in {ReuseClassification.VALIDATED_BUT_STALE, ReuseClassification.VALIDATED_BUT_INCOMPATIBLE}:
            deployment_state = DeploymentState.HISTORICAL_VALIDATION_REQUIRED
            recommended_action = f"REVALIDATION_REQUIRED: {reason}"
        elif mainnet_eligible:
            deployment_state = DeploymentState.MAINNET_ELIGIBLE
            recommended_action = "RECOMMEND_MAINNET_DEPLOYMENT: all gates satisfied; await explicit Axodus authorization"
        elif testnet_eligible:
            deployment_state = DeploymentState.TESTNET_ELIGIBLE
            recommended_action = "RECOMMEND_TESTNET_DEPLOYMENT: historical validation reusable; deploy to Testnet for operational soak"
        elif classification == ReuseClassification.VALIDATED_REUSABLE:
            deployment_state = DeploymentState.HISTORICAL_VALIDATION_REUSABLE
            recommended_action = "PROCEED_TO_DEPLOYMENT_READINESS_GATE: resolve operational/governance prerequisites"
        else:
            deployment_state = DeploymentState.DEPLOYMENT_BLOCKED
            recommended_action = f"DEPLOYMENT_BLOCKED: {reason}"

        deduped_blockers: list[str] = []
        for b in all_blockers:
            if b not in deduped_blockers:
                deduped_blockers.append(b)

        return DeploymentRecommendation(
            symbol=symbol,
            strategy_id=strategy_id,
            strategy_revision=strategy_revision,
            scanner_status=scanner_status,
            reuse_classification=classification.value,
            deployment_state=deployment_state.value,
            current_compatibility=compat.get("status", "FAIL"),
            current_compatibility_details=compat,
            testnet=testnet_status,
            mainnet=mainnet_status,
            blockers=deduped_blockers,
            recommended_next_action=recommended_action,
        )

    def route_hot_asset(
        self,
        symbol: str,
        strategies: list[tuple[str, str]] | None = None,
        scanner_status: str = "HOT",
        market_info: CurrentMarketInfo | None = None,
        operational_context: OperationalDeploymentContext | None = None,
        current_spec: InstrumentSpec | None = None,
    ) -> dict[str, DeploymentRecommendation]:
        """Route all evaluated strategies for a scanner-selected hot pair."""
        strategies = strategies or [
            ("orderflow.momentum.aggression", "freeze-2026-09-24-adapter-v1"),
            ("orderflow.absorption.fade", "freeze-2026-09-24-adapter-v1"),
            ("orderflow.cvd.divergence.reversal", "freeze-2026-09-24-adapter-v1"),
        ]
        results: dict[str, DeploymentRecommendation] = {}
        for strategy_id, strategy_revision in strategies:
            results[strategy_id] = self.evaluate_deployment_readiness(
                symbol=symbol,
                strategy_id=strategy_id,
                strategy_revision=strategy_revision,
                scanner_status=scanner_status,
                market_info=market_info,
                operational_context=operational_context,
                current_spec=current_spec,
            )
        return results

    def save(self, path: Path) -> str:
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": "1.0",
            "cells": {key: record.to_dict() for key, record in self._records.items()},
        }
        raw = json.dumps(payload, indent=2, sort_keys=True)
        destination.write_text(raw + "\n")
        return hashlib.sha256(raw.encode()).hexdigest()

    @classmethod
    def from_file(cls, path: Path) -> EvidenceReuseRegistry:
        source = Path(path)
        if not source.is_file():
            raise FileNotFoundError(f"EVIDENCE_REUSE_REGISTRY_NOT_FOUND: {source}")
        payload = json.loads(source.read_text())
        records = {
            key: ValidationCellRecord.from_dict(cell)
            for key, cell in payload.get("cells", {}).items()
        }
        return cls(records)

    @classmethod
    def create_default_registry(cls, canonical_run_root: Path | None = None) -> EvidenceReuseRegistry:
        """Create registry pre-populated with canonical BTCUSDT IS validation evidence."""
        btc_spec_hash = (
            InstrumentSpec.from_registry("BTCUSDT").canonical_hash()
            if InstrumentSpec.is_registered("BTCUSDT")
            else ""
        )
        registry = cls()

        # BTCUSDT × Momentum
        registry.register_cell(
            ValidationCellRecord(
                symbol="BTCUSDT",
                strategy_id="orderflow.momentum.aggression",
                strategy_revision="freeze-2026-09-24-adapter-v1",
                venue="Binance USD-M Futures",
                market_type="USD-M Futures",
                instrument_spec_hash=btc_spec_hash,
                fee_model="BTCUSDT maker=0.0002 taker=0.0005",
                execution_model="execution-model-v1-conservative",
                position_sizing_model="fixed-notional-1000-usd",
                is_evidence={
                    "trade_count": 139,
                    "wins": 51,
                    "losses": 88,
                    "net_pnl": "-137.1873645389723",
                    "disposition": "IS_EVIDENCE_NEGATIVE",
                    "robustness": "ECONOMICALLY_FRAGILE",
                },
                oos_evidence=None,
                validation_status="ACCEPTED_AS_NEGATIVE_IS",
                validation_date="2026-09-25",
                dataset_id="orderflow-binance-futures-14d-20260623-20260706-v1",
                dataset_window={"start": "2026-06-23T00:00:00Z", "end": "2026-07-02T23:59:59.999Z"},
                canonical_ledgers={
                    "runId": "momentum-btcusdt-is-fae40ba4190bd13a",
                    "frameHash": "fae40ba4190bd13a9a0fb89c3b6f5a8d953de0c8a604ff3c7e5e174a3997ddbf",
                },
                artifact_hashes={
                    "signals": "cb5f00c6f49b2e09ebd148c42b6e7909ca6657225fcfede8d0c89506083508a0",
                    "orders": "0a9af338d17fc541a177b0d199759bf2d25f3166ac5f15843552d72a1b5c2887",
                    "fills": "eaf30b935b00be181468fb6ce148abc58e27f0385f60c1da7e100a69600c13e3",
                    "trades": "2161e7f393e7c83f5dabfbe276ec87c21e956f27b5e69ea25bf6c6472aa74875",
                },
                testnet_status="BLOCKED_NEGATIVE_IS",
                mainnet_status="BLOCKED",
                active_blockers=["IS_EVIDENCE_NEGATIVE", "OOS_NOT_VALIDATED", "DEBT-AUD-A-03", "NO_MAINNET_AUTHORITY"],
                invalidation_conditions=[],
            )
        )

        # BTCUSDT × Absorption
        registry.register_cell(
            ValidationCellRecord(
                symbol="BTCUSDT",
                strategy_id="orderflow.absorption.fade",
                strategy_revision="freeze-2026-09-24-adapter-v1",
                venue="Binance USD-M Futures",
                market_type="USD-M Futures",
                instrument_spec_hash=btc_spec_hash,
                fee_model="BTCUSDT maker=0.0002 taker=0.0005",
                execution_model="execution-model-v1-conservative",
                position_sizing_model="fixed-notional-1000-usd",
                is_evidence={
                    "trade_count": 1,
                    "wins": 0,
                    "losses": 1,
                    "net_pnl": "-3.071633348158",
                    "disposition": "IS_EVIDENCE_INCONCLUSIVE",
                    "sample": "VERY_LOW_SAMPLE",
                    "limitation": "MAKER_QUEUE_NOT_HISTORICALLY_PROVEN",
                },
                oos_evidence=None,
                validation_status="ACCEPTED_AS_INCONCLUSIVE_IS",
                validation_date="2026-09-25",
                dataset_id="orderflow-binance-futures-14d-20260623-20260706-v1",
                dataset_window={"start": "2026-06-23T00:00:00Z", "end": "2026-07-02T23:59:59.999Z"},
                canonical_ledgers={
                    "runId": "absorption-btcusdt-is-fae40ba4190bd13a",
                    "frameHash": "fae40ba4190bd13a9a0fb89c3b6f5a8d953de0c8a604ff3c7e5e174a3997ddbf",
                },
                artifact_hashes={
                    "signals": "4c7875e679299e24fdf968eac38a560b9a5b5a76aaa42d7c3d982075862f09cd",
                    "orders": "5650265bd7e9dd5d54a3b31bc68df74f1938a901a9d0e2bbd8bb06919a058868",
                    "fills": "24f4da6318d2353d38431f8c4ae255c17498411525b4d0a13b430db93d4002e0",
                    "trades": "e12492b81ac687b308097d3fca2d939517c32a4a3ee0b703aa46aec3fc29a0b6",
                },
                testnet_status="BLOCKED_INCONCLUSIVE_IS",
                mainnet_status="BLOCKED",
                active_blockers=["IS_EVIDENCE_INCONCLUSIVE", "VERY_LOW_SAMPLE", "OOS_NOT_VALIDATED", "DEBT-AUD-A-03", "NO_MAINNET_AUTHORITY"],
                invalidation_conditions=[],
            )
        )

        # BTCUSDT × CVD Divergence
        registry.register_cell(
            ValidationCellRecord(
                symbol="BTCUSDT",
                strategy_id="orderflow.cvd.divergence.reversal",
                strategy_revision="freeze-2026-09-24-adapter-v1",
                venue="Binance USD-M Futures",
                market_type="USD-M Futures",
                instrument_spec_hash=btc_spec_hash,
                fee_model="BTCUSDT maker=0.0002 taker=0.0005",
                execution_model="execution-model-v1-conservative",
                position_sizing_model="fixed-notional-1000-usd",
                is_evidence={
                    "trade_count": 198,
                    "wins": 67,
                    "losses": 131,
                    "net_pnl": "-247.7282467457188",
                    "disposition": "IS_EVIDENCE_NEGATIVE",
                    "robustness": "ECONOMICALLY_FRAGILE",
                },
                oos_evidence=None,
                validation_status="ACCEPTED_AS_NEGATIVE_IS",
                validation_date="2026-09-25",
                dataset_id="orderflow-binance-futures-14d-20260623-20260706-v1",
                dataset_window={"start": "2026-06-23T00:00:00Z", "end": "2026-07-02T23:59:59.999Z"},
                canonical_ledgers={
                    "runId": "divergence-btcusdt-is-fae40ba4190bd13a",
                    "frameHash": "fae40ba4190bd13a9a0fb89c3b6f5a8d953de0c8a604ff3c7e5e174a3997ddbf",
                },
                artifact_hashes={
                    "signals": "cee4965b219627e18d59f712699b742e04f03fa965f86a303de45aa789ad5bf3",
                    "orders": "427b00871442adef6e5300eeda70f897e715db13e3e61b7f82d03c1310996863",
                    "fills": "a776c2375a2c128c6092a4f45e0ca8e1958e110a0cac0b4259ab8225787c9146",
                    "trades": "6ceed5c6a80eb11fff17a4121f83ad799c9da1914dbd84a577ca3399e6e1d950",
                },
                testnet_status="BLOCKED_NEGATIVE_IS",
                mainnet_status="BLOCKED",
                active_blockers=["IS_EVIDENCE_NEGATIVE", "OOS_NOT_VALIDATED", "DEBT-AUD-A-03", "NO_MAINNET_AUTHORITY"],
                invalidation_conditions=[],
            )
        )

        return registry
