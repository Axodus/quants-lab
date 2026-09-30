"""Causal microprice-displacement baseline strategy.

The adapter emits only canonical ``SimulatedDecision`` values. It consumes the
feature state available at the current observation and keeps no future labels,
markouts, or evaluation results in its decision path.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping

from core.quant_simulation.models import SimulatedDecision
from core.quant_optimization.provenance import file_hash

STRATEGY_ID = "microstructure.microprice_displacement.scalper"
BASE_REVISION = "microprice-disp-v1"
BASELINE_HOLDING_PERIOD_MS = 1000


def source_hash() -> str:
    return file_hash(Path(__file__))


def _decimal(value: Any, name: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be numeric") from exc
    if not result.is_finite():
        raise ValueError(f"{name} must be finite")
    return result


def _feature_map(market_state: Mapping[str, Any]) -> dict[str, Any]:
    values = dict(market_state.get("featureValues", {}))
    for feature in market_state.get("features", ()):
        if feature.get("featureId") is not None:
            values[feature["featureId"]] = feature.get("value")
    values.update({key: value for key, value in market_state.items() if key in {
        "microprice_displacement_bps", "signed_queue_imbalance_L1", "spread_bps", "microprice_valid", "book_validity",
    }})
    return values


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "valid"}:
            return True
        if normalized in {"false", "0", "no", "invalid", "unavailable"}:
            return False
    return default


def _timestamp_ms(market_state: Mapping[str, Any]) -> int | None:
    value = market_state.get("marketTime")
    if value is None:
        return None
    if isinstance(value, (int, float, Decimal)):
        return int(value)
    return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp() * 1000)


@dataclass(frozen=True)
class MicropriceDisplacementConfig:
    long_displacement_threshold_bps: Decimal = Decimal("1")
    short_displacement_threshold_bps: Decimal = Decimal("-1")
    long_queue_imbalance_threshold: Decimal = Decimal("0.2")
    short_queue_imbalance_threshold: Decimal = Decimal("-0.2")
    max_spread_bps: Decimal = Decimal("5")
    quantity_or_notional: Decimal = Decimal("1")
    holding_period_ms: int = BASELINE_HOLDING_PERIOD_MS

    def __post_init__(self) -> None:
        for name in (
            "long_displacement_threshold_bps", "short_displacement_threshold_bps", "long_queue_imbalance_threshold",
            "short_queue_imbalance_threshold", "max_spread_bps", "quantity_or_notional",
        ):
            value = _decimal(getattr(self, name), name)
            if name == "long_displacement_threshold_bps" and value < 0:
                raise ValueError("long displacement threshold must be non-negative")
            if name == "short_displacement_threshold_bps" and value > 0:
                raise ValueError("short displacement threshold must be non-positive")
            if name == "long_queue_imbalance_threshold" and value < 0:
                raise ValueError("long queue threshold must be non-negative")
            if name == "short_queue_imbalance_threshold" and value > 0:
                raise ValueError("short queue threshold must be non-positive")
            if name in {"max_spread_bps", "quantity_or_notional"} and value <= 0:
                raise ValueError(f"{name} must be positive")
        if self.holding_period_ms != BASELINE_HOLDING_PERIOD_MS:
            raise ValueError("microprice-disp-v1 requires holding_period_ms=1000")


class MicropriceDisplacementStrategy:
    """Single-position, taker-only signal adapter for the frozen baseline."""

    def __init__(self, config: MicropriceDisplacementConfig | None = None, *, segment_length: int | None = None):
        self.config = config or MicropriceDisplacementConfig()
        if segment_length is not None and segment_length < 2:
            raise ValueError("segment_length must be at least two states")
        self.segment_length = segment_length
        self._state_index = -1
        self._pending: str | None = None
        self._opened_at_ms: int | None = None

    @classmethod
    def from_parameters(cls, parameters: Mapping[str, Any], notional: Any, slippage_bps: Any, count: int) -> "MicropriceDisplacementStrategy":
        del slippage_bps
        quantity = parameters.get("quantity_or_notional", notional)
        return cls(MicropriceDisplacementConfig(
            long_displacement_threshold_bps=_decimal(parameters.get("long_displacement_threshold_bps", "1"), "long threshold"),
            short_displacement_threshold_bps=_decimal(parameters.get("short_displacement_threshold_bps", "-1"), "short threshold"),
            long_queue_imbalance_threshold=_decimal(parameters.get("long_queue_imbalance_threshold", "0.2"), "long queue threshold"),
            short_queue_imbalance_threshold=_decimal(parameters.get("short_queue_imbalance_threshold", "-0.2"), "short queue threshold"),
            max_spread_bps=_decimal(parameters.get("max_spread_bps", "5"), "max spread"),
            quantity_or_notional=_decimal(quantity, "quantity_or_notional"),
            holding_period_ms=int(parameters.get("holding_period_ms", BASELINE_HOLDING_PERIOD_MS)),
        ), segment_length=count)

    def _signal_side(self, market_state: Mapping[str, Any]) -> str | None:
        features = _feature_map(market_state)
        if not _as_bool(features.get("microprice_valid"), False):
            return None
        validity = features.get("book_validity", market_state.get("bookValidity", "VALID"))
        if str(validity).upper() != "VALID":
            return None
        required = ("microprice_displacement_bps", "signed_queue_imbalance_L1", "spread_bps")
        if any(features.get(name) is None for name in required):
            return None
        displacement = _decimal(features[required[0]], required[0])
        imbalance = _decimal(features[required[1]], required[1])
        spread = _decimal(features[required[2]], required[2])
        if spread > self.config.max_spread_bps:
            return None
        if displacement >= self.config.long_displacement_threshold_bps and imbalance >= self.config.long_queue_imbalance_threshold:
            return "BUY"
        if displacement <= self.config.short_displacement_threshold_bps and imbalance <= self.config.short_queue_imbalance_threshold:
            return "SELL"
        return None

    def decide(self, market_state: Mapping[str, Any], position_quantity: Decimal) -> SimulatedDecision:
        self._state_index += 1
        position_quantity = _decimal(position_quantity, "position_quantity")
        stamp = str(market_state.get("marketTime", "unknown"))
        current_time = _timestamp_ms(market_state)

        if position_quantity == 0 and self._pending == "EXIT":
            self._pending = None
            self._opened_at_ms = None
        elif position_quantity != 0 and self._pending == "ENTRY":
            self._pending = None
            if self._opened_at_ms is None:
                self._opened_at_ms = current_time

        if self._pending is not None:
            return SimulatedDecision.no_action(f"decision:{stamp}:pending")

        if self.segment_length is not None:
            final_decision_index = self.segment_length - 2
            if self._state_index > final_decision_index:
                return SimulatedDecision.no_action(f"decision:{stamp}:segment-end")
            if self._state_index == final_decision_index:
                if position_quantity == 0:
                    return SimulatedDecision.no_action(f"decision:{stamp}:segment-end-flat")
                self._pending = "EXIT"
                exit_side = "SELL" if position_quantity > 0 else "BUY"
                return SimulatedDecision("EXIT", exit_side, abs(position_quantity), f"exit:segment-boundary:{stamp}")

        signal_side = self._signal_side(market_state)
        if position_quantity == 0:
            if signal_side is None:
                return SimulatedDecision.no_action(f"decision:{stamp}")
            self._pending = "ENTRY"
            return SimulatedDecision("ENTER", signal_side, self.config.quantity_or_notional, f"entry:{stamp}")

        if self._opened_at_ms is None:
            self._opened_at_ms = current_time
        held_ms = current_time - self._opened_at_ms if current_time is not None and self._opened_at_ms is not None else None
        current_side = "BUY" if position_quantity > 0 else "SELL"
        exit_side = "SELL" if position_quantity > 0 else "BUY"
        holding_expired = held_ms is not None and held_ms >= self.config.holding_period_ms
        opposite_signal = signal_side == exit_side
        if holding_expired or opposite_signal:
            self._pending = "EXIT"
            reason = "holding" if holding_expired else "opposite-signal"
            return SimulatedDecision("EXIT", exit_side, abs(position_quantity), f"exit:{reason}:{stamp}")
        return SimulatedDecision.no_action(f"decision:{stamp}:{current_side.lower()}")
