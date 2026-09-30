"""Causal Microtrend Scalper v3 research strategy adapter.

All moving-average decisions use completed 1-minute candles. The adapter emits
only canonical ``SimulatedDecision`` values and consumes externally qualified
Jev decisions through the current market state; Jev never receives execution
authority here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any, Mapping, Sequence

from core.quant_foundations.canonical import sha256_digest
from core.quant_optimization.provenance import file_hash
from core.quant_simulation.models import ClosedTradeResult, SimulatedDecision
from core.quant_strategies import indicators
from core.quant_strategies.indicators import distance_bps, ema, step_bps, to_decimal, wma

STRATEGY_ID = "trend.microtrend.scalper"
BASE_REVISION = "microtrend-v3"

EMA_FAST_PERIOD = 9
EMA_MEDIUM_PERIOD = 21
WMA_STRUCTURAL_PERIOD = 50
CONFIRMATION_CANDLES = 3
TIMEFRAME = "1m"
LOSS_STREAK_LIMIT = 2
COOLDOWN_MINUTES = 15


class MADirection(StrEnum):
    UP = "UP"
    DOWN = "DOWN"
    FLAT = "FLAT"


class MicrotrendState(StrEnum):
    UNARMED = "UNARMED"
    ARMED_LONG = "ARMED_LONG"
    ARMED_SHORT = "ARMED_SHORT"
    WEAKENING_LONG = "WEAKENING_LONG"
    WEAKENING_SHORT = "WEAKENING_SHORT"
    PULLBACK_WAIT_LONG = "PULLBACK_WAIT_LONG"
    PULLBACK_WAIT_SHORT = "PULLBACK_WAIT_SHORT"
    JEV_CONFIRMATION_LONG = "JEV_CONFIRMATION_LONG"
    JEV_CONFIRMATION_SHORT = "JEV_CONFIRMATION_SHORT"
    RECONFIRMED_LONG = "RECONFIRMED_LONG"
    RECONFIRMED_SHORT = "RECONFIRMED_SHORT"
    REVERSAL_RISK_LONG = "REVERSAL_RISK_LONG"
    REVERSAL_RISK_SHORT = "REVERSAL_RISK_SHORT"


class JevResult(StrEnum):
    CONTINUATION = "CONTINUATION"
    NEUTRAL = "NEUTRAL"
    REVERSAL_RISK = "REVERSAL_RISK"
    INVALID = "INVALID"


@dataclass(frozen=True)
class MicrotrendConfig:
    """Explicit research configuration.

    Every calibratable or empirically-derived value is required. The class has
    no strategy-threshold defaults that could be mistaken for frozen evidence.
    """

    ma_flat_epsilon_bps: Decimal
    ema9_entry_proximity_bps: Decimal
    ema21_entry_proximity_bps: Decimal
    ema21_pullback_proximity_bps: Decimal
    wma50_pullback_proximity_bps: Decimal
    quantity_or_notional: Decimal
    holding_period_ms: int
    jev_observation_window_ms: int
    jev_min_observations: int
    jev_min_persistence_ratio: Decimal
    jev_aggression_threshold: Decimal
    jev_ofi_threshold: Decimal

    def __post_init__(self) -> None:
        nonnegative = (
            "ma_flat_epsilon_bps",
            "ema9_entry_proximity_bps",
            "ema21_entry_proximity_bps",
            "ema21_pullback_proximity_bps",
            "wma50_pullback_proximity_bps",
            "jev_aggression_threshold",
            "jev_ofi_threshold",
        )
        for name in nonnegative:
            value = to_decimal(getattr(self, name), name)
            object.__setattr__(self, name, value)
            if value < 0:
                raise ValueError(f"{name} must be non-negative")
        if self.ma_flat_epsilon_bps <= 0:
            raise ValueError("ma_flat_epsilon_bps must be positive to preserve disjoint direction states")
        quantity = to_decimal(self.quantity_or_notional, "quantity_or_notional")
        persistence = to_decimal(self.jev_min_persistence_ratio, "jev_min_persistence_ratio")
        object.__setattr__(self, "quantity_or_notional", quantity)
        object.__setattr__(self, "jev_min_persistence_ratio", persistence)
        if quantity <= 0:
            raise ValueError("quantity_or_notional must be positive")
        if self.holding_period_ms <= 0:
            raise ValueError("holding_period_ms must be positive")
        if self.jev_observation_window_ms <= 0 or self.jev_min_observations <= 0:
            raise ValueError("Jev observation window and minimum observations must be positive")
        if persistence <= 0 or persistence > 1:
            raise ValueError("jev_min_persistence_ratio must be in (0, 1]")


@dataclass(frozen=True)
class _MAObservation:
    timestamp_ms: int
    close: Decimal
    ema9: Decimal
    ema21: Decimal
    wma50: Decimal
    ema9_direction: MADirection
    ema21_direction: MADirection
    wma50_direction: MADirection


def source_hash() -> str:
    return sha256_digest(
        {
            "strategy": file_hash(Path(__file__)),
            "indicators": file_hash(Path(indicators.__file__)),
        }
    )


def _timestamp_ms(value: Any) -> int:
    if isinstance(value, (int, float, Decimal)):
        numeric = Decimal(str(value))
        # Condor's JevDecisionV1 uses Unix seconds while market-state adapters
        # conventionally expose milliseconds. Accept both without relying on
        # wall-clock time; modern Unix seconds are below this threshold.
        return int(numeric * 1000) if abs(numeric) < Decimal("100000000000") else int(numeric)
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("market timestamp must include timezone")
    return int(parsed.timestamp() * 1000)


def _feature_map(market_state: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(market_state.get("featureValues", {}))
    for feature in market_state.get("features", ()):
        feature_id = feature.get("featureId")
        if feature_id is not None:
            result[str(feature_id)] = feature.get("value")
    return result


def _is_true(value: Any) -> bool:
    return value is True or (isinstance(value, str) and value.strip().lower() in {"true", "1", "yes", "valid"})


def _candle_close(market_state: Mapping[str, Any]) -> Decimal:
    values = _feature_map(market_state)
    value = market_state.get("marketPrice", values.get("close"))
    if value is None:
        raise ValueError("Microtrend requires a completed candle close")
    return to_decimal(value, "candle close")


def _is_completed_one_minute_candle(market_state: Mapping[str, Any]) -> bool:
    values = _feature_map(market_state)
    closed = market_state.get("candleClosed", values.get("candle_closed", False))
    timeframe = str(market_state.get("timeframe", values.get("timeframe", ""))).lower()
    return _is_true(closed) and timeframe in {"1m", "60s", "60sec", "60000ms"}


def _direction(current: Decimal, previous: Decimal, epsilon: Decimal) -> MADirection:
    change = step_bps(current, previous)
    if change >= epsilon:
        return MADirection.UP
    if change <= -epsilon:
        return MADirection.DOWN
    return MADirection.FLAT


class MicrotrendScalperStrategy:
    """Stateful, single-position Microtrend v3 strategy simulator."""

    def __init__(self, config: MicrotrendConfig, *, segment_length: int | None = None):
        self.config = config
        if segment_length is not None and segment_length < 2:
            raise ValueError("segment_length must be at least two states")
        self.segment_length = segment_length
        self.reset_segment()

    @classmethod
    def from_parameters(
        cls,
        parameters: Mapping[str, Any],
        notional: Any,
        slippage_bps: Any,
        count: int,
    ) -> "MicrotrendScalperStrategy":
        del slippage_bps
        required = (
            "ma_flat_epsilon_bps",
            "ema9_entry_proximity_bps",
            "ema21_entry_proximity_bps",
            "ema21_pullback_proximity_bps",
            "wma50_pullback_proximity_bps",
            "holding_period_ms",
            "jev_observation_window_ms",
            "jev_min_observations",
            "jev_min_persistence_ratio",
            "jev_aggression_threshold",
            "jev_ofi_threshold",
        )
        missing = [name for name in required if name not in parameters]
        if missing:
            raise ValueError(f"Microtrend empirical/calibratable parameters required: {', '.join(missing)}")
        config = MicrotrendConfig(
            ma_flat_epsilon_bps=parameters["ma_flat_epsilon_bps"],
            ema9_entry_proximity_bps=parameters["ema9_entry_proximity_bps"],
            ema21_entry_proximity_bps=parameters["ema21_entry_proximity_bps"],
            ema21_pullback_proximity_bps=parameters["ema21_pullback_proximity_bps"],
            wma50_pullback_proximity_bps=parameters["wma50_pullback_proximity_bps"],
            quantity_or_notional=parameters.get("quantity_or_notional", notional),
            holding_period_ms=int(parameters["holding_period_ms"]),
            jev_observation_window_ms=int(parameters["jev_observation_window_ms"]),
            jev_min_observations=int(parameters["jev_min_observations"]),
            jev_min_persistence_ratio=parameters["jev_min_persistence_ratio"],
            jev_aggression_threshold=parameters["jev_aggression_threshold"],
            jev_ofi_threshold=parameters["jev_ofi_threshold"],
        )
        return cls(config, segment_length=count)

    def reset_segment(self) -> None:
        self.state = MicrotrendState.UNARMED
        self._closes: list[Decimal] = []
        self._ma_history: list[_MAObservation] = []
        self._state_index = -1
        self._pending_action: str | None = None
        self._opened_at_ms: int | None = None
        self._opposing_ema9_steps = 0
        self._zone_reference: str | None = None
        self._zone_entered = False
        self._pending_jev_request: dict[str, Any] | None = None
        self.last_jev_result: JevResult | None = None
        self.consecutive_losses = 0
        self.cooldown_until_ms: int | None = None
        self._closed_trade_ids: set[str] = set()

    @property
    def cooldown_active(self) -> bool:
        return self.cooldown_until_ms is not None

    @property
    def pending_jev_request(self) -> Mapping[str, Any] | None:
        return dict(self._pending_jev_request) if self._pending_jev_request is not None else None

    def on_trade_closed(self, closed_trade: ClosedTradeResult) -> None:
        if closed_trade.trade_id in self._closed_trade_ids:
            raise ValueError(f"duplicate closed-trade notification: {closed_trade.trade_id}")
        self._closed_trade_ids.add(closed_trade.trade_id)
        if closed_trade.net_pnl < 0:
            self.consecutive_losses += 1
            if self.consecutive_losses >= LOSS_STREAK_LIMIT:
                self.cooldown_until_ms = _timestamp_ms(closed_trade.exit_timestamp) + COOLDOWN_MINUTES * 60_000
        elif closed_trade.net_pnl > 0:
            self.consecutive_losses = 0
            self.cooldown_until_ms = None
        # Break-even intentionally preserves both the loss streak and any active cooldown.

    def decide(self, market_state: Mapping[str, Any], position_quantity: Decimal) -> SimulatedDecision:
        self._state_index += 1
        position_quantity = to_decimal(position_quantity, "position_quantity")
        stamp = str(market_state.get("marketTime", "unknown"))
        now_ms = _timestamp_ms(market_state.get("marketTime"))
        self._expire_cooldown(now_ms)
        self._acknowledge_pending(position_quantity, now_ms)

        if self._pending_action is not None:
            return SimulatedDecision.no_action(f"decision:{stamp}:pending")
        if not _is_completed_one_minute_candle(market_state):
            return SimulatedDecision.no_action(f"decision:{stamp}:incomplete-candle")

        close = _candle_close(market_state)
        observation = self._append_observation(close, now_ms)
        if observation is None:
            return SimulatedDecision.no_action(f"decision:{stamp}:warmup")

        if self.segment_length is not None:
            final_decision_index = self.segment_length - 2
            if self._state_index > final_decision_index:
                return SimulatedDecision.no_action(f"decision:{stamp}:segment-end")
            if self._state_index == final_decision_index:
                if position_quantity == 0:
                    return SimulatedDecision.no_action(f"decision:{stamp}:segment-end-flat")
                return self._exit(position_quantity, stamp, "segment-boundary")

        if position_quantity != 0 and self._opened_at_ms is not None:
            if now_ms - self._opened_at_ms >= self.config.holding_period_ms:
                return self._exit(position_quantity, stamp, "holding")

        previous_state = self.state
        self._advance_regime(observation, market_state)
        if position_quantity != 0:
            return SimulatedDecision.no_action(f"decision:{stamp}:position-open")
        if self._cooldown_is_active(now_ms):
            return SimulatedDecision.no_action(f"decision:{stamp}:cooldown")

        # The candle that completes three-candle synchronization arms the
        # regime; a later completed candle creates the first entry opportunity.
        if previous_state is MicrotrendState.UNARMED and self.state in {
            MicrotrendState.ARMED_LONG,
            MicrotrendState.ARMED_SHORT,
        }:
            return SimulatedDecision.no_action(f"decision:{stamp}:armed")

        entry_side = self._entry_side(observation)
        if entry_side is None:
            return SimulatedDecision.no_action(f"decision:{stamp}:{self.state.value.lower()}")
        self._pending_action = "ENTRY"
        return SimulatedDecision("ENTER", entry_side, self.config.quantity_or_notional, f"entry:{self.state.value}:{stamp}")

    def _append_observation(self, close: Decimal, timestamp_ms: int) -> _MAObservation | None:
        self._closes.append(close)
        if len(self._closes) < WMA_STRUCTURAL_PERIOD:
            return None
        ema9_value = ema(self._closes, EMA_FAST_PERIOD)
        ema21_value = ema(self._closes, EMA_MEDIUM_PERIOD)
        wma50_value = wma(self._closes, WMA_STRUCTURAL_PERIOD)
        previous = self._ma_history[-1] if self._ma_history else None
        observation = _MAObservation(
            timestamp_ms=timestamp_ms,
            close=close,
            ema9=ema9_value,
            ema21=ema21_value,
            wma50=wma50_value,
            ema9_direction=(
                _direction(ema9_value, previous.ema9, self.config.ma_flat_epsilon_bps)
                if previous
                else MADirection.FLAT
            ),
            ema21_direction=(
                _direction(ema21_value, previous.ema21, self.config.ma_flat_epsilon_bps)
                if previous
                else MADirection.FLAT
            ),
            wma50_direction=(
                _direction(wma50_value, previous.wma50, self.config.ma_flat_epsilon_bps)
                if previous
                else MADirection.FLAT
            ),
        )
        self._ma_history.append(observation)
        return observation

    def _advance_regime(self, observation: _MAObservation, market_state: Mapping[str, Any]) -> None:
        if self.state == MicrotrendState.UNARMED:
            armed = self._arming_state()
            if armed is not None:
                self.state = armed
            return

        side = self._regime_side()
        if side is None:
            return
        if not self._structure_valid(observation, side):
            self._clear_regime()
            return

        if self.state in {MicrotrendState.ARMED_LONG, MicrotrendState.ARMED_SHORT}:
            if self._ema9_aligned(observation, side):
                self._opposing_ema9_steps = 0
                return
            self._opposing_ema9_steps = 1 if self._ema9_opposes(observation, side) else 0
            crossed = observation.ema9 <= observation.ema21 if side == "LONG" else observation.ema9 >= observation.ema21
            if crossed:
                self.state = MicrotrendState.PULLBACK_WAIT_LONG if side == "LONG" else MicrotrendState.PULLBACK_WAIT_SHORT
                self._initialize_pullback_zone(observation)
                self._maybe_trigger_jev(observation, market_state, side)
                return
            self.state = MicrotrendState.WEAKENING_LONG if side == "LONG" else MicrotrendState.WEAKENING_SHORT
            return

        if self.state in {MicrotrendState.WEAKENING_LONG, MicrotrendState.WEAKENING_SHORT}:
            if self._ema9_aligned(observation, side):
                self._opposing_ema9_steps = 0
                self.state = MicrotrendState.ARMED_LONG if side == "LONG" else MicrotrendState.ARMED_SHORT
                return
            if self._ema9_opposes(observation, side):
                self._opposing_ema9_steps += 1
            crossed = observation.ema9 <= observation.ema21 if side == "LONG" else observation.ema9 >= observation.ema21
            if crossed or self._opposing_ema9_steps >= 2:
                self.state = MicrotrendState.PULLBACK_WAIT_LONG if side == "LONG" else MicrotrendState.PULLBACK_WAIT_SHORT
                self._initialize_pullback_zone(observation)
                self._maybe_trigger_jev(observation, market_state, side)
            return

        if self.state in {MicrotrendState.PULLBACK_WAIT_LONG, MicrotrendState.PULLBACK_WAIT_SHORT}:
            self._maybe_trigger_jev(observation, market_state, side)
            return

        if self.state in {MicrotrendState.JEV_CONFIRMATION_LONG, MicrotrendState.JEV_CONFIRMATION_SHORT}:
            self._consume_jev_decision(market_state, observation, side)
            return

        if self.state in {MicrotrendState.RECONFIRMED_LONG, MicrotrendState.RECONFIRMED_SHORT}:
            if self._ema9_aligned(observation, side):
                self.state = MicrotrendState.ARMED_LONG if side == "LONG" else MicrotrendState.ARMED_SHORT
            return

    def _arming_state(self) -> MicrotrendState | None:
        if len(self._ma_history) < CONFIRMATION_CANDLES:
            return None
        rows = self._ma_history[-CONFIRMATION_CANDLES:]
        long_order = all(row.ema9 > row.ema21 > row.wma50 for row in rows)
        short_order = all(row.ema9 < row.ema21 < row.wma50 for row in rows)
        long_direction = all(
            row.ema9_direction == row.ema21_direction == row.wma50_direction == MADirection.UP
            for row in rows[1:]
        )
        short_direction = all(
            row.ema9_direction == row.ema21_direction == row.wma50_direction == MADirection.DOWN
            for row in rows[1:]
        )
        if long_order and long_direction:
            return MicrotrendState.ARMED_LONG
        if short_order and short_direction:
            return MicrotrendState.ARMED_SHORT
        return None

    def _regime_side(self) -> str | None:
        if self.state.value.endswith("_LONG"):
            return "LONG"
        if self.state.value.endswith("_SHORT"):
            return "SHORT"
        return None

    @staticmethod
    def _structure_valid(observation: _MAObservation, side: str) -> bool:
        return observation.ema21 > observation.wma50 if side == "LONG" else observation.ema21 < observation.wma50

    @staticmethod
    def _ema9_aligned(observation: _MAObservation, side: str) -> bool:
        if side == "LONG":
            return observation.ema9 > observation.ema21 and observation.ema9_direction == MADirection.UP
        return observation.ema9 < observation.ema21 and observation.ema9_direction == MADirection.DOWN

    @staticmethod
    def _ema9_opposes(observation: _MAObservation, side: str) -> bool:
        return observation.ema9_direction == (MADirection.DOWN if side == "LONG" else MADirection.UP)

    def _entry_side(self, observation: _MAObservation) -> str | None:
        if self.state in {MicrotrendState.ARMED_LONG, MicrotrendState.ARMED_SHORT}:
            if not self._ema9_aligned(observation, self._regime_side() or ""):
                return None
            near = (
                distance_bps(observation.close, observation.ema9) <= self.config.ema9_entry_proximity_bps
                or distance_bps(observation.close, observation.ema21) <= self.config.ema21_entry_proximity_bps
            )
            if near:
                return "BUY" if self.state == MicrotrendState.ARMED_LONG else "SELL"
        if self.state in {MicrotrendState.RECONFIRMED_LONG, MicrotrendState.RECONFIRMED_SHORT}:
            near = (
                distance_bps(observation.close, observation.ema21) <= self.config.ema21_pullback_proximity_bps
                or distance_bps(observation.close, observation.wma50) <= self.config.wma50_pullback_proximity_bps
            )
            if near:
                return "BUY" if self.state == MicrotrendState.RECONFIRMED_LONG else "SELL"
        return None

    def _initialize_pullback_zone(self, observation: _MAObservation) -> None:
        self._zone_reference = None
        # Jev is only invoked when the completed close crosses into a pullback
        # band. Preserve the preceding completed-candle state so a regime
        # transition that happens while price was already in-band does not
        # fabricate a new pullback event.
        previous = self._ma_history[-2] if len(self._ma_history) >= 2 else None
        self._zone_entered = previous is not None and self._inside_any_pullback_zone(previous)
        self._pending_jev_request = None
        self.last_jev_result = None

    def _inside_any_pullback_zone(self, observation: _MAObservation) -> bool:
        return (
            distance_bps(observation.close, observation.ema21) <= self.config.ema21_pullback_proximity_bps
            or distance_bps(observation.close, observation.wma50) <= self.config.wma50_pullback_proximity_bps
        )

    def _maybe_trigger_jev(self, observation: _MAObservation, market_state: Mapping[str, Any], side: str) -> None:
        ema21_inside = distance_bps(observation.close, observation.ema21) <= self.config.ema21_pullback_proximity_bps
        wma50_inside = distance_bps(observation.close, observation.wma50) <= self.config.wma50_pullback_proximity_bps
        inside = ema21_inside or wma50_inside
        if not inside:
            self._zone_entered = False
            self._zone_reference = None
            return
        if self._zone_entered:
            return
        self._zone_entered = True
        self._zone_reference = "EMA21" if ema21_inside else "WMA50"
        request, invalid = self._build_jev_request(market_state, observation, side)
        if invalid:
            self.last_jev_result = JevResult.INVALID
            self.state = MicrotrendState.PULLBACK_WAIT_LONG if side == "LONG" else MicrotrendState.PULLBACK_WAIT_SHORT
            return
        self._pending_jev_request = request
        self.state = MicrotrendState.JEV_CONFIRMATION_LONG if side == "LONG" else MicrotrendState.JEV_CONFIRMATION_SHORT
        self._consume_jev_decision(market_state, observation, side)

    def _build_jev_request(
        self,
        market_state: Mapping[str, Any],
        observation: _MAObservation,
        side: str,
    ) -> tuple[dict[str, Any] | None, bool]:
        context = market_state.get("jevContext", {})
        if not isinstance(context, Mapping):
            return None, True
        required_context = (
            "run_id",
            "cell_id",
            "symbol",
            "venue",
            "market_type",
            "deployment_candidate_id",
        )
        if any(not context.get(name) for name in required_context):
            return None, True
        snapshots = market_state.get("microstructureSnapshots", ())
        if isinstance(snapshots, Mapping):
            snapshots = (snapshots,)
        lower = observation.timestamp_ms - self.config.jev_observation_window_ms
        valid: list[dict[str, Any]] = []
        required_features = (
            "signed_queue_imbalance_L1",
            "normalized_OFI_L5",
            "normalized_weighted_pressure",
            "aggression_imbalance",
            "signed_volume_velocity",
            "buy_absorption_score",
            "sell_absorption_score",
            "buy_sweep_score",
            "sell_sweep_score",
        )
        for raw in snapshots:
            if not isinstance(raw, Mapping):
                return None, True
            try:
                timestamp = _timestamp_ms(raw.get("timestamp", raw.get("observedAt")))
            except (TypeError, ValueError):
                return None, True
            if timestamp > observation.timestamp_ms:
                return None, True
            if timestamp < lower:
                continue
            features = raw.get("features", raw)
            if not isinstance(features, Mapping):
                return None, True
            invalid_flags = (
                "sequence_gap",
                "crossed_book",
                "stale_state",
                "stale",
                "invalid_features",
            )
            if any(_is_true(features.get(flag, raw.get(flag, False))) for flag in invalid_flags):
                return None, True
            if str(features.get("book_validity", raw.get("book_validity", "INVALID"))).upper() != "VALID":
                return None, True
            if not _is_true(features.get("microprice_valid", raw.get("microprice_valid", False))):
                return None, True
            if any(features.get(name) is None for name in required_features):
                return None, True
            valid.append({"timestamp": timestamp, "features": {name: str(features[name]) for name in required_features}})
        if len(valid) < self.config.jev_min_observations:
            return None, True
        valid.sort(key=lambda item: item["timestamp"])
        candidate_id = f"microtrend:{sha256_digest({'time': observation.timestamp_ms, 'side': side, 'zone': self._zone_reference, 'snapshots': valid})}"
        return {
            "schema_version": "v1",
            "run_id": str(context["run_id"]),
            "cell_id": str(context["cell_id"]),
            "symbol": str(context["symbol"]),
            "venue": str(context["venue"]),
            "market_type": str(context["market_type"]),
            "candidate_trigger_id": candidate_id,
            "correlation_id": str(context.get("correlation_id") or candidate_id),
            "strategy_id": STRATEGY_ID,
            "strategy_revision": BASE_REVISION,
            "trigger_type": "ENTRY",
            "side": "BUY" if side == "LONG" else "SELL",
            "event_time": observation.timestamp_ms / 1000,
            "market_state_ref": {
                "market_state_id": market_state.get("marketStateId"),
                "market_time": market_state.get("marketTime"),
                "observed_at": market_state.get("observedAt"),
            },
            "zone_reference": self._zone_reference,
            "feature_snapshot": {"observations": valid},
            "trigger_evidence": {
                "jev_observation_window_ms": self.config.jev_observation_window_ms,
                "jev_min_observations": self.config.jev_min_observations,
                "jev_min_persistence_ratio": str(self.config.jev_min_persistence_ratio),
                "jev_aggression_threshold": str(self.config.jev_aggression_threshold),
                "jev_ofi_threshold": str(self.config.jev_ofi_threshold),
                "confidence_type": "NONE_V1",
            },
            "current_logical_position": context.get("current_logical_position"),
            "current_open_orders": list(context.get("current_open_orders", ())),
            "deployment_candidate_id": str(context["deployment_candidate_id"]),
        }, False

    def _consume_jev_decision(self, market_state: Mapping[str, Any], observation: _MAObservation, side: str) -> None:
        if self._pending_jev_request is None:
            return
        decision = market_state.get("jevDecision")
        if not isinstance(decision, Mapping):
            return
        request = self._pending_jev_request
        identity_fields = (
            "run_id",
            "cell_id",
            "candidate_trigger_id",
            "correlation_id",
            "strategy_id",
            "strategy_revision",
        )
        if decision.get("schema_version") != "v1" or not decision.get("decision_id"):
            self._mark_jev_invalid(side)
            return
        if any(decision.get(name) != request.get(name) for name in identity_fields):
            self._mark_jev_invalid(side)
            return
        decided_at = decision.get(
            "decided_at_ms",
            decision.get("decidedAtMs", decision.get("decided_at", decision.get("decidedAt"))),
        )
        if decided_at is None or _timestamp_ms(decided_at) > observation.timestamp_ms:
            self._mark_jev_invalid(side)
            return
        reasons = {str(code).upper() for code in decision.get("reason_codes", ())}
        outer = str(decision.get("decision", ""))
        action = str(decision.get("action", ""))
        expected_side = "BUY" if side == "LONG" else "SELL"
        decision_side = str(decision.get("side", ""))
        if (
            outer == "SIGNAL_QUALIFIED"
            and action == "ENTRY"
            and decision_side == expected_side
            and "MICROTREND_CONTINUATION" in reasons
        ):
            self.last_jev_result = JevResult.CONTINUATION
            self.state = MicrotrendState.RECONFIRMED_LONG if side == "LONG" else MicrotrendState.RECONFIRMED_SHORT
        elif outer == "NO_ACTION" and action == "NONE" and decision_side == "NONE" and "MICROTREND_NEUTRAL" in reasons:
            self.last_jev_result = JevResult.NEUTRAL
            self.state = MicrotrendState.PULLBACK_WAIT_LONG if side == "LONG" else MicrotrendState.PULLBACK_WAIT_SHORT
        elif (
            outer == "SIGNAL_REJECTED"
            and action == "NONE"
            and decision_side == "NONE"
            and "MICROTREND_REVERSAL_RISK" in reasons
        ):
            self.last_jev_result = JevResult.REVERSAL_RISK
            self.state = MicrotrendState.REVERSAL_RISK_LONG if side == "LONG" else MicrotrendState.REVERSAL_RISK_SHORT
        elif (
            outer == "SIGNAL_REJECTED"
            and action == "NONE"
            and decision_side == "NONE"
            and "MICROTREND_INVALID" in reasons
        ):
            self._mark_jev_invalid(side)
            return
        else:
            self._mark_jev_invalid(side)
            return
        self._pending_jev_request = None

    def _mark_jev_invalid(self, side: str) -> None:
        self.last_jev_result = JevResult.INVALID
        self.state = MicrotrendState.PULLBACK_WAIT_LONG if side == "LONG" else MicrotrendState.PULLBACK_WAIT_SHORT
        self._pending_jev_request = None

    def _acknowledge_pending(self, position_quantity: Decimal, now_ms: int) -> None:
        if self._pending_action == "ENTRY" and position_quantity != 0:
            self._pending_action = None
            self._opened_at_ms = now_ms
        elif self._pending_action == "EXIT" and position_quantity == 0:
            self._pending_action = None
            self._opened_at_ms = None

    def _exit(self, position_quantity: Decimal, stamp: str, reason: str) -> SimulatedDecision:
        self._pending_action = "EXIT"
        side = "SELL" if position_quantity > 0 else "BUY"
        return SimulatedDecision("EXIT", side, abs(position_quantity), f"exit:{reason}:{stamp}")

    def _cooldown_is_active(self, now_ms: int) -> bool:
        return self.cooldown_until_ms is not None and now_ms < self.cooldown_until_ms

    def _expire_cooldown(self, now_ms: int) -> None:
        if self.cooldown_until_ms is not None and now_ms >= self.cooldown_until_ms:
            self.cooldown_until_ms = None
            self.consecutive_losses = 0

    def _clear_regime(self) -> None:
        self.state = MicrotrendState.UNARMED
        self._opposing_ema9_steps = 0
        self._zone_reference = None
        self._zone_entered = False
        self._pending_jev_request = None
        self.last_jev_result = None
