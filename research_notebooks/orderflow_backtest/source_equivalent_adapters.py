"""Adapters that preserve the frozen OpenClaw order-flow semantics.

The functions intentionally mirror the source ``observe`` functions and use
the same Decimal frame/configuration types. They are research-only signals;
they have no execution or portfolio authority.
"""

from decimal import Decimal
from typing import Sequence

from .orderflow_contracts import OrderFlowFrameV1, SourceSignalV1, SourceStrategyConfigV1, signal


def _eligible(frame: OrderFlowFrameV1, history: Sequence[OrderFlowFrameV1], config: SourceStrategyConfigV1, extra: int = 0):
    if len(history) < config.window + extra:
        return "warmup"
    if frame.volume == 0:
        return "zero_volume"
    if frame.spread_ticks > config.max_spread_ticks:
        return "wide_spread"
    return None


def _mean(values: Sequence[Decimal]) -> Decimal:
    return sum(values, Decimal(0)) / len(values)


def _slope_z(frame: OrderFlowFrameV1, history: Sequence[OrderFlowFrameV1]):
    values = [item.delta for item in history]
    mean_value = _mean(values)
    variance = _mean([(value - mean_value) ** 2 for value in values])
    standard_deviation = variance.sqrt()
    return None if standard_deviation == 0 else (frame.delta - mean_value) / standard_deviation


def momentum_observe(frame, history, cvds, cvd, config: SourceStrategyConfigV1) -> SourceSignalV1:
    reason = _eligible(frame, history, config)
    if reason:
        return signal("NO_SIGNAL", reason)
    prior = history[-config.window:]
    z = _slope_z(frame, prior)
    if z is None:
        return signal("NO_SIGNAL", "zero_historical_delta_variance")
    if (z > config.slope_z and frame.delta > 0 and frame.obi > config.imbalance
            and frame.price > max(item.price for item in prior)):
        return signal("LONG", "positive_aggression_breakout")
    if (z < -config.slope_z and frame.delta < 0 and frame.obi < -config.imbalance
            and frame.price < min(item.price for item in prior)):
        return signal("SHORT", "negative_aggression_breakout")
    return signal("NO_SIGNAL", "conditions_not_met")


def absorption_observe(frame, history, cvds, cvd, config: SourceStrategyConfigV1) -> SourceSignalV1:
    reason = _eligible(frame, history, config, extra=1)
    if reason:
        return signal("NO_SIGNAL", reason)
    candidate = history[-1]
    baseline = history[-(config.window + 1):-1]
    average = _mean([item.volume for item in baseline])
    if average == 0 or candidate.volume == 0:
        return signal("NO_SIGNAL", "zero_baseline_or_candidate_volume")
    spike = candidate.volume > average * config.volume_multiple
    stalled = abs(candidate.price - baseline[-1].price) <= config.absorption_move_ticks * config.tick_size
    if not (spike and stalled) or candidate.spread_ticks > config.max_spread_ticks:
        return signal("NO_SIGNAL", "no_absorption_candidate")
    if (candidate.sell / candidate.volume > config.aggression_fraction
            and frame.delta >= config.confirmation_delta and frame.price > candidate.price):
        return signal("LONG", "sell_absorption_hypothesis_with_counter_buying")
    if (candidate.buy / candidate.volume > config.aggression_fraction
            and frame.delta <= -config.confirmation_delta and frame.price < candidate.price):
        return signal("SHORT", "buy_absorption_hypothesis_with_counter_selling")
    return signal("NO_SIGNAL", "counter_aggression_not_confirmed")


def divergence_observe(frame, history, cvds, cvd, config: SourceStrategyConfigV1) -> SourceSignalV1:
    reason = _eligible(frame, history, config)
    if reason:
        return signal("NO_SIGNAL", reason)
    prior = history[-config.window:]
    prior_cvds = cvds[-config.window:]
    if (frame.price <= min(item.price for item in prior) - config.divergence_price_ticks * config.tick_size
            and cvd >= min(prior_cvds) + config.divergence_delta and frame.delta > 0):
        return signal("LONG", "lower_price_low_with_higher_cvd_and_positive_delta")
    if (frame.price >= max(item.price for item in prior) + config.divergence_price_ticks * config.tick_size
            and cvd <= max(prior_cvds) - config.divergence_delta and frame.delta < 0):
        return signal("SHORT", "higher_price_high_with_lower_cvd_and_negative_delta")
    return signal("NO_SIGNAL", "trailing_divergence_not_confirmed")


ADAPTERS = {
    "orderflow.momentum.aggression": momentum_observe,
    "orderflow.absorption.fade": absorption_observe,
    "orderflow.cvd.divergence.reversal": divergence_observe,
}
